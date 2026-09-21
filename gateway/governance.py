"""治理编排：把租户校验、计量、幂等、审计与降级策略串成一次请求判定。"""
from __future__ import annotations

from gateway.audit import AuditLog
from gateway.config import ConfigRegistry
from gateway.idempotency import IdempotencyStore
from gateway.limiter import TenantMeterRegistry
from gateway.tenancy import TenantAuthenticator


class GovernancePipeline:
    """对单个请求执行完整治理判定；内部异常一律转为可观测的降级行为。"""

    def __init__(
        self,
        authenticator: TenantAuthenticator,
        config: ConfigRegistry,
        meters: TenantMeterRegistry,
        idempotency: IdempotencyStore,
        audit: AuditLog,
    ) -> None:
        self._auth = authenticator
        self._config = config
        self._meters = meters
        self._idem = idempotency
        self._audit = audit
