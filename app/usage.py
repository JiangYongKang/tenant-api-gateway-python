"""租户用量视图。

面向运维/支持的只读查询：速率额度、周期配额、幂等记录占用。

- 自洽性：所有读数取自同一时刻 now，各组件在各自锁内读取；
  已用数永不超过上限（remaining = max(0, limit - used)）。
- 降级：治理状态不可读/变慢时返回 status=degraded、数值字段为 None 的明确结论，
  绝不外抛内部异常；该表现可通过 StateBackend 注入在本地复现。
- 隔离：只汇总指定租户的数据，不夹带其他租户信息或凭据明文。
"""
from __future__ import annotations

import enum
from dataclasses import dataclass

from .overrides import TenantPolicyStore
from .state import StateUnavailableError


class UsageStatus(enum.Enum):
    OK = "ok"
    DEGRADED = "degraded"          # 治理状态不可读，无法给出可信数字
    UNKNOWN_TENANT = "unknown_tenant"


@dataclass(frozen=True)
class UsageView:
    status: UsageStatus
    tenant_id: str
    rate_limit_per_minute: int | None
    rate_remaining: float | None
    rate_retry_after_seconds: float | None
    quota_per_day: int | None
    quota_used: int | None
    quota_remaining: int | None
    quota_reset_seconds: float | None
    idempotency_records: int | None
    idempotency_ttl_seconds: float | None


class UsageService:
    """从各计量组件汇总单租户用量快照。"""

    def __init__(
        self,
        registry,
        config_store,
        rate_limiter,
        quota_tracker,
        idempotency_store,
        state_backend,
        clock,
        policy_store: TenantPolicyStore | None = None,
        state_timeout: float = 0.5,
    ) -> None:
        self._registry = registry
        self._config_store = config_store
        self._rate_limiter = rate_limiter
        self._quota_tracker = quota_tracker
        self._idempotency_store = idempotency_store
        self._state_backend = state_backend
        self._clock = clock
        self._policy_store = policy_store
        self._state_timeout = state_timeout

    def snapshot(self, tenant_id: str) -> UsageView:
        now = self._clock()

        if self._registry.lookup(tenant_id, now=now) is None:
            return UsageView(UsageStatus.UNKNOWN_TENANT, tenant_id, *([None] * 9))

        # 治理状态不可读/变慢 -> 明确降级结论，不外抛异常
        try:
            self._state_backend.check_available(timeout=self._state_timeout)
        except StateUnavailableError:
            return UsageView(UsageStatus.DEGRADED, tenant_id, *([None] * 9))
        except Exception:
            return UsageView(UsageStatus.DEGRADED, tenant_id, *([None] * 9))

        cfg = self._config_store.get()
        if self._policy_store is not None:
            policy = self._policy_store.effective_for(tenant_id, cfg)
            rate_limit, quota_limit, ttl = (
                policy.rate_limit_per_minute,
                policy.quota_per_day,
                policy.idempotency_ttl_seconds,
            )
        else:
            rate_limit, quota_limit, ttl = (
                cfg.rate_limit_per_minute,
                cfg.quota_per_day,
                cfg.idempotency_ttl_seconds,
            )

        rate = self._rate_limiter.peek(tenant_id, rate_limit, now)
        used = self._quota_tracker.usage(tenant_id, now)
        remaining = max(0, quota_limit - used)  # 自洽：已用数永不超过上限
        return UsageView(
            status=UsageStatus.OK,
            tenant_id=tenant_id,
            rate_limit_per_minute=rate_limit,
            rate_remaining=round(rate.remaining, 3),
            rate_retry_after_seconds=rate.retry_after_seconds,
            quota_per_day=quota_limit,
            quota_used=used,
            quota_remaining=remaining,
            quota_reset_seconds=self._quota_tracker.reset_seconds(now),
            idempotency_records=self._idempotency_store.count_for(tenant_id, now),
            idempotency_ttl_seconds=ttl,
        )
