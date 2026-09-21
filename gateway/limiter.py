"""按租户计量的限流（速率）与配额（累计用量），窗口有界、可回收。

语义约定：
- 限流 = 令牌桶，约束短时间内的请求速率；恢复时间 = 等待下一枚令牌的时长。
- 配额 = 固定窗口计数，约束窗口内累计用量；恢复时间 = 当前窗口剩余时长。
- 两者都只依赖调用方传入的 now（单调时钟由上层保证），恢复语义确定、与并发无关。
- check_and_consume 在单锁内完成"判定 + 扣减"：拒绝时不扣减，通过时恰好扣减一次，
  同一租户并发请求不会超发、漏扣或重复扣减。
"""
from __future__ import annotations

import threading
from dataclasses import dataclass


@dataclass
class TenantMeter:
    """单租户计量状态：令牌桶 + 固定窗口配额计数。"""

    tokens: float
    last_refill: float
    window_start: float
    window_count: int
    last_seen: float


@dataclass(frozen=True)
class MeterDecision:
    allowed: bool
    rate_limited: bool
    quota_exceeded: bool
    retry_after_seconds: float


class TenantMeterRegistry:
    """按租户隔离的计量注册表，容量有界并按期回收空闲租户。"""

    def __init__(self, max_tenants: int = 10000) -> None:
        if max_tenants < 1:
            raise ValueError("max_tenants 必须 >= 1")
        self._lock = threading.Lock()
        self._meters: dict[str, TenantMeter] = {}
        self._max_tenants = max_tenants

    @property
    def tracked_tenants(self) -> int:
        with self._lock:
            return len(self._meters)

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
        with self._lock:
            meter = self._meters.get(tenant_id)
            if meter is None:
                self._evict_if_full()
                meter = TenantMeter(
                    tokens=float(burst),
                    last_refill=now,
                    window_start=now,
                    window_count=0,
                    last_seen=now,
                )
                self._meters[tenant_id] = meter

            # 令牌桶按经过时间补充，封顶 burst
            elapsed = max(0.0, now - meter.last_refill)
            meter.tokens = min(float(burst), meter.tokens + elapsed * rate_per_second)
            meter.last_refill = now
            meter.last_seen = now

            # 配额窗口到期即整体重置，恢复时间确定
            if now - meter.window_start >= quota_window_seconds:
                meter.window_start = now
                meter.window_count = 0

            quota_exceeded = meter.window_count >= quota_limit
            rate_limited = meter.tokens < 1.0

            if quota_exceeded or rate_limited:
                retry_after = 0.0
                if rate_limited:
                    retry_after = max(retry_after, (1.0 - meter.tokens) / rate_per_second)
                if quota_exceeded:
                    retry_after = max(
                        retry_after, meter.window_start + quota_window_seconds - now
                    )
                return MeterDecision(
                    allowed=False,
                    rate_limited=rate_limited,
                    quota_exceeded=quota_exceeded,
                    retry_after_seconds=retry_after,
                )

            meter.tokens -= 1.0
            meter.window_count += 1
            return MeterDecision(
                allowed=True,
                rate_limited=False,
                quota_exceeded=False,
                retry_after_seconds=0.0,
            )

    def reclaim_idle(self, now: float, idle_seconds: float) -> int:
        """回收空闲租户计量状态，返回回收数量。"""
        with self._lock:
            stale = [
                tid for tid, m in self._meters.items() if now - m.last_seen >= idle_seconds
            ]
            for tid in stale:
                del self._meters[tid]
            return len(stale)

    def _evict_if_full(self) -> None:
        """容量满时驱逐最久未活动的租户，保证内存有界。"""
        if len(self._meters) < self._max_tenants:
            return
        oldest = min(self._meters.items(), key=lambda kv: kv[1].last_seen)[0]
        del self._meters[oldest]
