"""按租户计量的限流（速率）与配额（累计用量），窗口有界、可回收。"""
from __future__ import annotations

import threading
from dataclasses import dataclass, field


@dataclass
class TenantMeter:
    """单租户计量状态：令牌桶 + 滑动窗口配额计数。"""

    tokens: float = 0.0
    last_refill: float = 0.0
    window_start: float = 0.0
    window_count: int = 0


@dataclass(frozen=True)
class MeterDecision:
    allowed: bool
    rate_limited: bool
    quota_exceeded: bool
    retry_after_seconds: float


class TenantMeterRegistry:
    """按租户隔离的计量注册表，容量有界并按期回收空闲租户。"""

    def __init__(self, max_tenants: int = 10000) -> None:
        self._lock = threading.Lock()
        self._meters: dict[str, TenantMeter] = {}
        self._max_tenants = max_tenants

    def check_and_consume(
        self,
        tenant_id: str,
        *,
        now: float,
        rate_per_second: float,
        burst: int,
        quota_limit: int,
        quota_window_seconds: float,
    ) -> MeterDecision:
        raise NotImplementedError

    def reclaim_idle(self, now: float, idle_seconds: float) -> int:
        """回收空闲租户计量状态，返回回收数量。"""
        raise NotImplementedError
