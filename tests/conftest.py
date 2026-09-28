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
from app.admin import register_admin_routes
from app.middleware import GovernanceMiddleware
from app.policies import PolicyStore
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
    policy_store: PolicyStore
    clock: MutableClock
    side_effects: dict[str, int] = field(default_factory=dict)
    flaky_fail: dict[str, bool] = field(default_factory=lambda: {"fail": False})

    def cred(self, tenant_id: str) -> str:
        return self.registry.issue(tenant_id)

    def headers(self, tenant_id: str, **extra: str) -> dict[str, str]:
        return {"x-tenant-credential": self.cred(tenant_id), **extra}


def build_harness(
    max_tenants: int = 10_000,
    *,
    seed_disabled: bool = True,
    seeds: dict[str, TenantStatus] | None = None,
) -> Harness:
    clock = MutableClock()
    # 本地把容量上限调小即可稳定复现"容量超限导致整批拒绝"
    registry = TenantRegistry(secret=b"test-secret", clock=clock, max_tenants=max_tenants)
    if seeds is not None:
        for tid, status in seeds.items():  # 显式种子：用于精确构造容量/回收场景
            registry.add(tid, status)
    else:
        registry.add("acme")
        registry.add("globex")
        if seed_disabled:
            # 可回收终态租户；零回收空间的场景用 seed_disabled=False 且容量=2
            registry.add("oldco", TenantStatus.DISABLED)

    config_store = ConfigStore()
    audit_log = AuditLog()
    state_backend = StateBackend()
    rate_limiter = RateLimiter()
    quota_tracker = QuotaTracker()
    idempotency_store = IdempotencyStore()
    policy_store = PolicyStore()
    side_effects: dict[str, int] = {}
    flaky_fail = {"fail": False}

    app = FastAPI()

    @app.get("/")
    def root():
        return {"Hello": "World"}

    @app.get("/healthz")
    def healthz():
        return {"status": "ok"}

    @app.post("/orders", status_code=201)
    async def orders(request: Request):
        tenant = request.headers["x-tenant-credential"].split(".")[0].removeprefix("tnt_")
        side_effects[tenant] = side_effects.get(tenant, 0) + 1
        log.info("side-effect executed tenant=%s count=%d", tenant, side_effects[tenant])
        return {"seq": side_effects[tenant]}

    @app.post("/flaky")
    async def flaky():
        # 可控下游：fail=True 时返回 500，模拟下游失败
        if flaky_fail["fail"]:
            from fastapi.responses import JSONResponse
            return JSONResponse(status_code=500, content={"error": "downstream boom"})
        side_effects["flaky"] = side_effects.get("flaky", 0) + 1
        return {"seq": side_effects["flaky"]}

    @app.post("/exploding")
    async def exploding():
        raise RuntimeError("downstream crashed")

    app.add_middleware(
        GovernanceMiddleware,
        resolver=TenantResolver(registry),
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
    register_admin_routes(
        app,
        registry=registry,
        config_store=config_store,
        policy_store=policy_store,
        rate_limiter=rate_limiter,
        quota_tracker=quota_tracker,
        idempotency_store=idempotency_store,
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
        policy_store=policy_store,
        clock=clock,
        side_effects=side_effects,
        flaky_fail=flaky_fail,
    )


@pytest.fixture()
def harness() -> Harness:
    return build_harness()


@pytest.fixture()
def small_harness() -> Harness:
    # 容量=2，两个种子（acme/globex）均为 ACTIVE、无可回收终态：
    # 任何新增开通都稳定触发容量上限，且不存在靠回收"意外成功"的干扰。
    return build_harness(max_tenants=2, seed_disabled=False)


@pytest.fixture()
def reclaim_harness() -> Harness:
    # 容量=2：1 个 ACTIVE（acme）+ 1 个立即到期的终态（gone DISABLED，可回收腾 1 个位）。
    # 并发批量开通时恰好一个批次能靠回收整体成功，其余批次在应用阶段才撞上容量。
    return build_harness(
        max_tenants=2,
        seeds={"acme": TenantStatus.ACTIVE, "gone": TenantStatus.DISABLED},
    )
