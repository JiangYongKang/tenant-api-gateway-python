"""多租户 API 网关示例服务。

- GET  /                原有接口，语义不变（需有效租户凭据）
- POST /orders          演示副作用接口（每租户计数），支持 Idempotency-Key
- GET  /admin/config    查看当前治理配置
- POST /admin/config    调整治理配置（强校验，非法即 422，即时生效）
- GET  /admin/audit     检索审计记录
"""
from __future__ import annotations

import threading

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.audit import AuditLog, AuditOutcome
from app.config import ConfigStore, ConfigValidationError
from app.idempotency import IdempotencyStore
from app.limits import QuotaTracker, RateLimiter
from app.middleware import GovernanceMiddleware
from app.state import StateBackend
from app.tenancy import TenantRegistry, TenantResolver, TenantStatus

# ---- 治理组件装配（本地确定性，无外部依赖） ----
registry = TenantRegistry(secret=b"local-dev-secret")
registry.add("acme")
registry.add("globex")
registry.add("oldco", TenantStatus.DISABLED)

config_store = ConfigStore()
audit_log = AuditLog()
state_backend = StateBackend()
rate_limiter = RateLimiter()
quota_tracker = QuotaTracker()
idempotency_store = IdempotencyStore()
resolver = TenantResolver(registry)

app = FastAPI(title="tenant-api-gateway")
app.add_middleware(
    GovernanceMiddleware,
    resolver=resolver,
    config_store=config_store,
    rate_limiter=rate_limiter,
    quota_tracker=quota_tracker,
    idempotency_store=idempotency_store,
    audit_log=audit_log,
    state_backend=state_backend,
)

# ---- 业务接口 ----
_order_counters: dict[str, int] = {}
_order_lock = threading.Lock()


@app.get("/")
def read_root():
    return {"Hello": "World"}


@app.post("/orders", status_code=201)
async def create_order(request: Request):
    tenant = request.headers.get("x-tenant-credential", "").split(".")[0].removeprefix("tnt_")
    with _order_lock:
        _order_counters[tenant] = _order_counters.get(tenant, 0) + 1
        count = _order_counters[tenant]
    return {"order_seq": count, "tenant": tenant}


# ---- 治理管理接口（豁免治理链路，保证降级期间仍可调配置） ----
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
