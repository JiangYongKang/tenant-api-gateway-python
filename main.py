"""FastAPI 入口：把治理管线接到 HTTP 层。

- 既有接口 GET / 完全保留，不经过治理；
- /api/* 为受治理接口：GET 读取、POST 写入（副作用，支持幂等）；
- 凭据可从 X-Tenant-Credential 或 Authorization 头进入，走同一校验器，结论一致；
- /governance/* 提供配置热更新（非法配置 422 明确拒绝）与审计检索。
"""
from __future__ import annotations

import json
import threading
import time
from collections.abc import Callable

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from gateway.audit import AuditLog
from gateway.config import ConfigRegistry, GovernanceConfig
from gateway.errors import ConfigError
from gateway.governance import Decision, GovernancePipeline
from gateway.idempotency import IdempotencyStore
from gateway.limiter import TenantMeterRegistry
from gateway.tenancy import TenantAuthenticator, TenantRecord

CREDENTIAL_HEADERS = ("x-tenant-credential", "authorization")
IDEMPOTENCY_HEADER = "x-idempotency-key"

# 本地演示租户：凭据可完全本地签发与校验（见 README），不依赖任何外部服务。
DEFAULT_TENANTS = {
    "tenant-alpha": TenantRecord("tenant-alpha", "alpha-local-secret"),
    "tenant-beta": TenantRecord("tenant-beta", "beta-local-secret"),
    "tenant-disabled": TenantRecord("tenant-disabled", "disabled-secret", enabled=False),
}


def _extract_credential(request: Request) -> str | None:
    for name in CREDENTIAL_HEADERS:
        value = request.headers.get(name)
        if value:
            return value
    return None


def _reject_response(decision: Decision) -> JSONResponse:
    headers = {"x-request-id": decision.request_id}
    if decision.retry_after_seconds is not None:
        headers["retry-after"] = f"{decision.retry_after_seconds:.3f}"
    if decision.degraded:
        headers["x-governance-degraded"] = "true"
    return JSONResponse(
        status_code=decision.status_code,
        content={
            "error": {
                "reason": decision.reason.value if decision.reason else "unknown",
                "request_id": decision.request_id,
            }
        },
        headers=headers,
    )


def create_app(
    *,
    tenants: dict[str, TenantRecord] | None = None,
    credential_clock: Callable[[], float] = time.time,
    meter_clock: Callable[[], float] = time.monotonic,
    config: GovernanceConfig | None = None,
) -> FastAPI:
    registry = ConfigRegistry(config)
    audit = AuditLog()
    pipeline = GovernancePipeline(
        authenticator=TenantAuthenticator(tenants or DEFAULT_TENANTS, clock=credential_clock),
        config=registry,
        meters=TenantMeterRegistry(max_tenants=(config or GovernanceConfig()).max_tracked_tenants),
        idempotency=IdempotencyStore(max_keys=(config or GovernanceConfig()).max_idempotency_keys),
        audit=audit,
    )
    # 副作用状态：按租户隔离的资源表
    resources: dict[str, list[dict]] = {}
    resources_lock = threading.Lock()

    app = FastAPI(title="tenant-api-gateway")
    app.state.pipeline = pipeline
    app.state.config_registry = registry
    app.state.audit = audit

    @app.get("/")
    def read_root():  # 既有接口：可访问性与响应语义保持不变
        return {"Hello": "World"}

    async def _govern(request: Request) -> Decision:
        body = await request.body()
        return pipeline.inspect(
            method=request.method,
            path=request.url.path,
            credential_header=_extract_credential(request),
            idempotency_key=request.headers.get(IDEMPOTENCY_HEADER),
            body=body,
            now=meter_clock(),
        )

    @app.get("/api/resource")
    async def list_resources(request: Request):
        decision = await _govern(request)
        if not decision.allowed:
            return _reject_response(decision)
        with resources_lock:
            items = list(resources.get(decision.tenant_id, []))
        pipeline.complete(decision, 200, json.dumps(items))
        headers = {"x-request-id": decision.request_id}
        if decision.degraded:
            headers["x-governance-degraded"] = "true"
        return JSONResponse(content={"items": items}, headers=headers)

    @app.post("/api/resource", status_code=201)
    async def create_resource(request: Request):
        decision = await _govern(request)
        if decision.replay_record is not None:  # 合法重放：返回首个结果，不产生第二次副作用
            return JSONResponse(
                status_code=decision.replay_record.response_status or 200,
                content=json.loads(decision.replay_record.response_body or "null"),
                headers={"x-request-id": decision.request_id, "x-idempotent-replay": "true"},
            )
        if not decision.allowed:
            return _reject_response(decision)
        try:
            payload = json.loads(await request.body() or b"{}")
            if not isinstance(payload, dict):
                raise ValueError("body must be a JSON object")
            with resources_lock:
                item = {"id": len(resources.setdefault(decision.tenant_id, [])) + 1, **payload}
                resources[decision.tenant_id].append(item)
            body = json.dumps(item)
        except Exception:
            pipeline.fail(decision, 400)
            return JSONResponse(
                status_code=400,
                content={"error": {"reason": "bad_request", "request_id": decision.request_id}},
                headers={"x-request-id": decision.request_id},
            )
        pipeline.complete(decision, 201, body)
        headers = {"x-request-id": decision.request_id}
        if decision.degraded:
            headers["x-governance-degraded"] = "true"
        return JSONResponse(status_code=201, content=item, headers=headers)

    @app.get("/governance/config")
    def get_config():
        return registry.get().__dict__

    @app.post("/governance/config")
    def update_config(changes: dict):
        try:
            updated = registry.update(**changes)
        except ConfigError as exc:
            return JSONResponse(
                status_code=422,
                content={"error": {"reason": "invalid_config", "detail": str(exc)}},
            )
        return updated.__dict__

    @app.get("/governance/audit/{request_id}")
    def get_audit(request_id: str):
        event = audit.find_by_request_id(request_id)
        if event is None:
            return JSONResponse(
                status_code=404, content={"error": {"reason": "not_found"}}
            )
        return event.__dict__

    return app


app = create_app()
