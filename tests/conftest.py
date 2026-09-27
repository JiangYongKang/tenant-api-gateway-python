"""测试装配：可控时钟 + 本地确定性组件，日志打印判定依据与输入状态。"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

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

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
log = logging.getLogger("governance-tests")


class MutableClock:
    def __init__(self, start: float = 1_000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@dataclass
class Harness:
    client: TestClient
    registry: TenantRegistry
    config_store: ConfigStore
    audit_log: AuditLog
    state_backend: StateBackend
    rate_limiter: RateLimiter
    quota_tracker: QuotaTracker
    idempotency_store: IdempotencyStore
    policy_store: TenantPolicyStore
    lifecycle: TenantLifecycleService
    clock: MutableClock
    side_effects: dict[str, int] = field(default_factory=dict)
    controls: dict[str, bool] = field(default_factory=lambda: {"flaky_fail": False, "flaky_raise": False})

    def cred(self, tenant_id: str) -> str:
        return self.registry.issue(tenant_id)

    def headers(self, tenant_id: str, **extra: str) -> dict[str, str]:
        return {"x-tenant-credential": self.cred(tenant_id), **extra}

    def apply_lifecycle(self, *ops: dict):
        """通过管理接口批量调整租户生命周期。"""
        return self.client.post("/admin/tenants", json={"operations": list(ops)})


def build_harness() -> Harness:
    clock = MutableClock()
    registry = TenantRegistry(secret=b"test-secret", clock=clock)
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
    side_effects: dict[str, int] = {}
    controls: dict[str, bool] = {"flaky_fail": False, "flaky_raise": False}

    app = FastAPI()

    @app.get("/")
    def root():
        return {"Hello": "World"}

    @app.post("/orders", status_code=201)
    async def orders(request: Request):
        tenant = request.headers["x-tenant-credential"].split(".")[0].removeprefix("tnt_")
        side_effects[tenant] = side_effects.get(tenant, 0) + 1
        log.info("side-effect executed tenant=%s count=%d", tenant, side_effects[tenant])
        return {"seq": side_effects[tenant]}

    @app.post("/flaky", status_code=201)
    async def flaky(request: Request):
        """可控下游：按 controls 注入 500 或异常，用于验证失败后合法重试可重新推进。"""
        tenant = request.headers["x-tenant-credential"].split(".")[0].removeprefix("tnt_")
        if controls["flaky_raise"]:
            raise RuntimeError("downstream exploded")
        if controls["flaky_fail"]:
            return JSONResponse(status_code=500, content={"error": "downstream 5xx"})
        side_effects[tenant] = side_effects.get(tenant, 0) + 1
        return {"seq": side_effects[tenant]}

    @app.get("/healthz")
    def healthz():
        return {"ok": True}

    app.add_middleware(
        GovernanceMiddleware,
        resolver=TenantResolver(registry, clock=clock),
        config_store=config_store,
        rate_limiter=rate_limiter,
        quota_tracker=quota_tracker,
        idempotency_store=idempotency_store,
        audit_log=audit_log,
        state_backend=state_backend,
        policy_store=policy_store,
        clock=clock,
        state_timeout=0.05,
    )
    usage_service = UsageService(
        registry,
        config_store,
        rate_limiter,
        quota_tracker,
        idempotency_store,
        state_backend,
        clock=clock,
        policy_store=policy_store,
        state_timeout=0.05,
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
        clock=clock,
    )
    return Harness(
        client=TestClient(app, raise_server_exceptions=False),
        registry=registry,
        config_store=config_store,
        audit_log=audit_log,
        state_backend=state_backend,
        rate_limiter=rate_limiter,
        quota_tracker=quota_tracker,
        idempotency_store=idempotency_store,
        policy_store=policy_store,
        lifecycle=lifecycle,
        clock=clock,
        side_effects=side_effects,
        controls=controls,
    )


@pytest.fixture()
def harness() -> Harness:
    return build_harness()
