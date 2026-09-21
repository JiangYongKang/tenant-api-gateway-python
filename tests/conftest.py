import logging
import time

import pytest

from gateway.audit import AuditLog
from gateway.config import ConfigRegistry, GovernanceConfig
from gateway.governance import GovernancePipeline
from gateway.idempotency import IdempotencyStore
from gateway.limiter import TenantMeterRegistry
from gateway.tenancy import TenantAuthenticator, TenantRecord, issue_credential

logger = logging.getLogger("tests")


class FakeClock:
    """可手动推进的时钟，保证判定完全本地可复现。"""

    def __init__(self, start: float = 1_000_000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def tenants() -> dict[str, TenantRecord]:
    return {
        "alpha": TenantRecord("alpha", "alpha-secret"),
        "beta": TenantRecord("beta", "beta-secret"),
        "disabled": TenantRecord("disabled", "disabled-secret", enabled=False),
    }


@pytest.fixture
def credential(tenants, clock):
    def _mint(tenant_id: str = "alpha", ttl: float = 3600.0) -> str:
        return issue_credential(tenants[tenant_id], clock() + ttl)

    return _mint


@pytest.fixture
def config() -> ConfigRegistry:
    return ConfigRegistry(
        GovernanceConfig(
            rate_limit_per_second=2.0,
            rate_burst=4,
            quota_limit=10,
            quota_window_seconds=60.0,
            idempotency_ttl_seconds=300.0,
            max_tracked_tenants=100,
            max_idempotency_keys=100,
        )
    )


@pytest.fixture
def audit() -> AuditLog:
    return AuditLog(max_events=1000)


@pytest.fixture
def pipeline(tenants, clock, config, audit) -> GovernancePipeline:
    return GovernancePipeline(
        authenticator=TenantAuthenticator(tenants, clock=clock),
        config=config,
        meters=TenantMeterRegistry(max_tenants=100),
        idempotency=IdempotencyStore(max_keys=100),
        audit=audit,
    )


def log_decision(label: str, decision, **inputs) -> None:
    """单测日志：打印判定依据与输入状态。"""
    logger.info(
        "[%s] inputs=%s -> allowed=%s status=%s reason=%s degraded=%s retry_after=%s",
        label,
        inputs,
        decision.allowed,
        decision.status_code,
        decision.reason.value if decision.reason else None,
        decision.degraded,
        decision.retry_after_seconds,
    )
