"""租户用量视图：限流/配额/重放记录的只读快照。

- 各组件在各自锁内读取，单组件数字自洽（不会出现已用超过上限）；
  视图只读，不改变任何计量状态。
- 治理状态不可读或读取超时：抛 StateUnavailableError，由调用方转成明确的
  可观测响应（503 state_unavailable），内部异常细节不外透。
- 视图只含指定租户的聚合数字与生效策略，不含凭据或其他租户信息。
"""
from __future__ import annotations

from collections.abc import Callable

from .config import ConfigStore
from .idempotency import IdempotencyStore
from .limits import QuotaTracker, RateLimiter
from .policies import OVERRIDABLE_FIELDS, PolicyStore, effective_config
from .state import StateBackend


def build_usage_view(
    tenant_id: str,
    *,
    config_store: ConfigStore,
    policy_store: PolicyStore | None,
    rate_limiter: RateLimiter,
    quota_tracker: QuotaTracker,
    idempotency_store: IdempotencyStore,
    state_backend: StateBackend,
    clock: Callable[[], float],
    state_timeout: float,
) -> dict:
    # 治理状态读不到/变慢：与中间件同一判定路径，抛 StateUnavailableError
    state_backend.check_available(timeout=state_timeout)

    now = clock()
    base = config_store.get()
    override = policy_store.get(tenant_id) if policy_store else None
    eff = effective_config(base, override)

    rate = rate_limiter.inspect(tenant_id, eff.rate_limit_per_minute, now)
    quota = quota_tracker.inspect(tenant_id, eff.quota_per_day, now)

    overridden = {
        f: getattr(override, f) for f in OVERRIDABLE_FIELDS
        if override is not None and getattr(override, f) is not None
    }
    return {
        "tenant_id": tenant_id,
        "governance": "ok",
        "rate_limit": {
            "limit_per_minute": eff.rate_limit_per_minute,
            "remaining": math_floor2(rate.remaining),
            "retry_after_seconds": rate.retry_after_seconds,  # 额度耗尽时预计恢复秒数，未耗尽为 None
        },
        "quota": {
            "limit_per_day": quota["limit"],
            "used": quota["used"],
            "remaining": quota["remaining"],
            "reset_at": quota["reset_at"],  # 当前窗口重置时刻（Unix 秒）
        },
        "idempotency": {
            "records": idempotency_store.usage(tenant_id, now),
            "ttl_seconds": eff.idempotency_ttl_seconds,
        },
        "policy": {
            "overridden_fields": overridden,  # 该租户生效的覆盖项；空 dict 表示全部走全局默认
        },
    }


def math_floor2(x: float) -> float:
    # 剩余令牌为浮点，向下保留两位小数，避免浮点噪声且保证 remaining <= limit 自洽
    return int(x * 100) / 100
