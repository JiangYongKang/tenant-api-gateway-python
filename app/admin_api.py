"""治理管理接口（/admin/*，豁免治理链路，降级期间仍可调配置）。

- POST /admin/config                调整全局默认配置（强校验，原子生效）
- GET  /admin/config                查看全局默认配置
- POST /admin/tenants               租户在线生命周期批量调整（原子：整批成功或完全不变）
- GET  /admin/tenants               查看租户及生效状态
- PUT  /admin/tenants/{id}/policy   设置租户级差异化策略（逐字段覆盖全局默认）
- DELETE /admin/tenants/{id}/policy 清除租户级策略覆盖
- GET  /admin/usage/{tenant_id}     租户用量视图（降级时 503，可观测、可本地复现）
- GET  /admin/audit                 检索审计记录
"""
from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from .audit import AuditLog, AuditOutcome
from .config import ConfigStore, ConfigValidationError
from .lifecycle import LifecycleError, TenantLifecycleService, parse_operation
from .overrides import TenantPolicyStore
from .state import StateBackend
from .tenancy import TenantRegistry
from .usage import UsageService, UsageStatus


def register_admin_routes(
    app: FastAPI,
    *,
    config_store: ConfigStore,
    registry: TenantRegistry,
    lifecycle: TenantLifecycleService,
    policy_store: TenantPolicyStore,
    usage_service: UsageService,
    audit_log: AuditLog,
    state_backend: StateBackend,
    clock,
) -> None:
    @app.get("/admin/config")
    def get_config():
        cfg = config_store.get()
        return {
            "rate_limit_per_minute": cfg.rate_limit_per_minute,
            "quota_per_day": cfg.quota_per_day,
            "idempotency_ttl_seconds": cfg.idempotency_ttl_seconds,
            "state_failure_mode": cfg.state_failure_mode,
        }

    @app.post("/admin/config")
    async def update_config(request: Request):
        try:
            cfg = config_store.update(await request.json())
        except ConfigValidationError as e:
            return JSONResponse(status_code=422, content={"error": {"code": "invalid_config", "detail": str(e)}})
        state_backend.bump_revision()
        return {"applied": True, "config": cfg.__dict__}

    @app.post("/admin/tenants")
    async def apply_lifecycle(request: Request):
        body = await request.json()
        raw_ops = body.get("operations") if isinstance(body, dict) else None
        if not isinstance(raw_ops, list):
            return JSONResponse(status_code=422, content={"error": {"code": "invalid_lifecycle", "detail": "operations 必须是数组"}})
        try:
            ops = [parse_operation(o) for o in raw_ops]
            lifecycle.apply(ops, now=clock())
        except LifecycleError as e:
            return JSONResponse(status_code=422, content={"error": {"code": "invalid_lifecycle", "detail": str(e)}})
        state_backend.bump_revision()
        return {"applied": True, "operations": len(ops)}

    @app.get("/admin/tenants")
    def list_tenants():
        now = clock()
        records = registry.snapshot_records()
        return {
            "tenants": [
                {
                    "tenant_id": rec.tenant_id,
                    "status": registry.effective_status(rec, now).value,
                    "secret_version": rec.secret_version,
                    "drain_until": rec.drain_until,
                }
                for rec in sorted(records.values(), key=lambda r: r.tenant_id)
            ]
        }

    @app.put("/admin/tenants/{tenant_id}/policy")
    async def set_policy(tenant_id: str, request: Request):
        if registry.lookup(tenant_id, now=clock()) is None:
            return JSONResponse(status_code=404, content={"error": {"code": "unknown_tenant"}})
        try:
            policy = policy_store.set_override(tenant_id, await request.json())
        except ConfigValidationError as e:
            return JSONResponse(status_code=422, content={"error": {"code": "invalid_policy", "detail": str(e)}})
        state_backend.bump_revision()
        return {
            "applied": True,
            "tenant_id": tenant_id,
            "overridden_fields": list(policy.overridden_fields),
        }

    @app.delete("/admin/tenants/{tenant_id}/policy")
    def clear_policy(tenant_id: str):
        cleared = policy_store.clear_override(tenant_id)
        if cleared:
            state_backend.bump_revision()
        return {"cleared": cleared, "tenant_id": tenant_id}

    @app.get("/admin/usage/{tenant_id}")
    def get_usage(tenant_id: str):
        view = usage_service.snapshot(tenant_id)
        if view.status is UsageStatus.UNKNOWN_TENANT:
            return JSONResponse(status_code=404, content={"error": {"code": "unknown_tenant"}})
        if view.status is UsageStatus.DEGRADED:
            # 明确、可观测的降级结论；不含内部异常细节
            return JSONResponse(status_code=503, content={"error": {"code": "state_unavailable", "status": "degraded"}})
        return {
            "tenant_id": view.tenant_id,
            "status": view.status.value,
            "rate": {
                "limit_per_minute": view.rate_limit_per_minute,
                "remaining": view.rate_remaining,
                "retry_after_seconds": view.rate_retry_after_seconds,
            },
            "quota": {
                "limit_per_day": view.quota_per_day,
                "used": view.quota_used,
                "remaining": view.quota_remaining,
                "reset_seconds": view.quota_reset_seconds,
            },
            "idempotency": {
                "records": view.idempotency_records,
                "ttl_seconds": view.idempotency_ttl_seconds,
            },
        }

    @app.get("/admin/audit")
    def get_audit(tenant_id: str | None = None, outcome: str | None = None):
        oc = AuditOutcome(outcome) if outcome else None
        records = audit_log.query(tenant_id=tenant_id, outcome=oc)
        return {
            "records": [
                {
                    "request_id": r.request_id,
                    "tenant_id": r.tenant_id,
                    "path": r.path,
                    "method": r.method,
                    "outcome": r.outcome.value,
                    "reason": r.reason,
                    "status_code": r.status_code,
                    "timestamp": r.timestamp,
                }
                for r in records
            ]
        }
