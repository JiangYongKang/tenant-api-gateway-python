"""治理管理接口（/admin/*，豁免治理链路，降级期间仍可操作）。

- POST /admin/tenants            租户在线生命周期：原子批量变更，非法/冲突整批 422
- GET  /admin/tenants[/{id}]     租户清单/详情（状态可见，不含凭据）
- PUT  /admin/policies/{id}      设置租户差异化策略（整体校验，非法 422 保留原策略）
- DELETE /admin/policies/{id}    移除覆盖，回落全局默认
- GET  /admin/policies           覆盖清单
- GET  /admin/usage/{id}         租户用量视图；治理状态不可读时 503 state_unavailable
"""
from __future__ import annotations

from collections.abc import Callable

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from .config import ConfigStore
from .idempotency import IdempotencyStore
from .limits import QuotaTracker, RateLimiter
from .policies import PolicyStore, PolicyValidationError
from .state import StateBackend, StateUnavailableError
from .tenancy import TenantAdminError, TenantRegistry
from .usage import build_usage_view


def _error(status: int, code: str, detail) -> JSONResponse:
    return JSONResponse(status_code=status, content={"error": {"code": code, "detail": detail}})


def register_admin_routes(
    app: FastAPI,
    *,
    registry: TenantRegistry,
    config_store: ConfigStore,
    policy_store: PolicyStore,
    rate_limiter: RateLimiter,
    quota_tracker: QuotaTracker,
    idempotency_store: IdempotencyStore,
    state_backend: StateBackend,
    clock: Callable[[], float],
    state_timeout: float,
) -> None:

    @app.post("/admin/tenants")
    async def apply_tenant_batch(request: Request):
        try:
            body = await request.json()
        except Exception:
            return _error(422, "invalid_tenant_batch", "请求体必须是 JSON 对象")
        operations = body.get("operations") if isinstance(body, dict) else None
        try:
            results = registry.apply_batch(operations)
        except TenantAdminError as e:
            return _error(422, "invalid_tenant_batch", e.errors)
        state_backend.bump_revision()
        return {"applied": True, "results": results}

    @app.get("/admin/tenants")
    def list_tenants():
        # 只暴露状态，不含凭据
        return {"tenants": [
            {"tenant_id": t.tenant_id, "status": t.status.value}
            for t in (registry.lookup(tid) for tid in _tenant_ids(registry))
            if t is not None
        ]}

    @app.get("/admin/tenants/{tenant_id}")
    def get_tenant(tenant_id: str):
        t = registry.lookup(tenant_id)
        if t is None:
            return _error(404, "unknown_tenant", tenant_id)
        return {"tenant_id": t.tenant_id, "status": t.status.value}

    @app.put("/admin/policies/{tenant_id}")
    async def set_policy(tenant_id: str, request: Request):
        if registry.lookup(tenant_id) is None:
            return _error(404, "unknown_tenant", tenant_id)
        try:
            policy = policy_store.set(tenant_id, await request.json())
        except PolicyValidationError as e:
            return _error(422, "invalid_policy", str(e))
        state_backend.bump_revision()
        return {"applied": True, "tenant_id": tenant_id,
                "overrides": {k: v for k, v in policy.__dict__.items() if v is not None}}

    @app.delete("/admin/policies/{tenant_id}")
    def delete_policy(tenant_id: str):
        removed = policy_store.remove(tenant_id)
        if removed:
            state_backend.bump_revision()
        return {"applied": removed, "tenant_id": tenant_id, "policy": "default"}

    @app.get("/admin/policies")
    def list_policies():
        return {"policies": {
            tid: {k: v for k, v in p.__dict__.items() if v is not None}
            for tid, p in policy_store.list_all().items()
        }}

    @app.get("/admin/usage/{tenant_id}")
    def get_usage(tenant_id: str):
        if registry.lookup(tenant_id) is None:
            return _error(404, "unknown_tenant", tenant_id)
        try:
            view = build_usage_view(
                tenant_id,
                config_store=config_store,
                policy_store=policy_store,
                rate_limiter=rate_limiter,
                quota_tracker=quota_tracker,
                idempotency_store=idempotency_store,
                state_backend=state_backend,
                clock=clock,
                state_timeout=state_timeout,
            )
        except StateUnavailableError:
            # 明确、可观测、可本地复现：不抛内部异常，不带其他租户信息
            return _error(503, "state_unavailable", "usage view temporarily unavailable")
        return view


def _tenant_ids(registry: TenantRegistry) -> list[str]:
    # TenantRegistry 不对外暴露遍历；通过其受控接口取快照
    return registry.tenant_ids()
