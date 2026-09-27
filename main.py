"""多租户 API 网关示例服务。

- GET  /                原有接口，语义不变（需有效租户凭据）
- POST /orders          演示副作用接口（每租户计数），支持 Idempotency-Key
- /admin/*              治理管理接口（配置、租户生命周期、差异策略、用量视图、审计）
"""
from __future__ import annotations

import threading
import time

from fastapi import FastAPI, Request

from app.admin_api import register_admin_routes
from app.audit import AuditLog
from app.config import ConfigStore
from app.idempotency import IdempotencyStore
from app.lifecycle import TenantLifecycleService
from app.limits import QuotaTracker, RateLimiter
from app.middleware import GovernanceMiddleware
from app.overrides import TenantPolicyStore
from app.state import StateBackend
from app.tenancy import TenantRegistry, TenantResolver, TenantStatus
from app.usage import UsageService

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
policy_store = TenantPolicyStore()
lifecycle = TenantLifecycleService(registry)
resolver = TenantResolver(registry)
usage_service = UsageService(
    registry,
    config_store,
    rate_limiter,
    quota_tracker,
    idempotency_store,
    state_backend,
    clock=time.time,
    policy_store=policy_store,
)

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
    policy_store=policy_store,
)

register_admin_routes(
    app,
    config_store=config_store,
    registry=registry,
    lifecycle=lifecycle,
    policy_store=policy_store,
    usage_service=usage_service,
    audit_log=audit_log,
    state_backend=state_backend,
    clock=time.time,
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
