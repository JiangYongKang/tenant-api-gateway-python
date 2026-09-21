"""测试装配：可控时钟 + 本地确定性组件，日志打印判定依据与输入状态。"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from app.audit import AuditLog
from app.config import ConfigStore
from app.idempotency import IdempotencyStore
from app.limits import QuotaTracker, RateLimiter
from app.middleware import GovernanceMiddleware
from app.state import StateBackend
from app.tenancy import TenantRegistry, TenantResolver, TenantStatus

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
    clock: MutableClock
    side_effects: dict[str, int] = field(default_factory=dict)

    def cred(self, tenant_id: str) -> str:
        return self.registry.issue(tenant_id)

    def headers(self, tenant_id: str, **extra: str) -> dict[str, str]:
        return {"x-tenant-credential": self.cred(tenant_id), **extra}


def build_harness() -> Harness:
    clock = MutableClock()
    registry = TenantRegistry(secret=b"test-secret")
    registry.add("acme")
    registry.add("globex")
    registry.add("oldco", TenantStatus.DISABLED)

    config_store = ConfigStore()
    audit_log = AuditLog()
    state_backend = StateBackend()
    rate_limiter = RateLimiter()
    quota_tracker = QuotaTracker()
    idempotency_store = IdempotencyStore()
    side_effects: dict[str, int] = {}

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

    app.add_middleware(
        GovernanceMiddleware,
        resolver=TenantResolver(registry),
        config_store=config_store,
        rate_limiter=rate_limiter,
        quota_tracker=quota_tracker,
        idempotency_store=idempotency_store,
        audit_log=audit_log,
        state_backend=state_backend,
        clock=clock,
        state_timeout=0.05,
    )
    return Harness(
        client=TestClient(app),
        registry=registry,
        config_store=config_store,
        audit_log=audit_log,
        state_backend=state_backend,
        rate_limiter=rate_limiter,
        quota_tracker=quota_tracker,
        idempotency_store=idempotency_store,
        clock=clock,
        side_effects=side_effects,
    )


@pytest.fixture()
def harness() -> Harness:
    return build_harness()
