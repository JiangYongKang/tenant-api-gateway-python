"""按租户计量的限流与配额。

- RateLimiter：令牌桶，约束短时间请求速率；恢复时间由令牌速率推导，确定且与并发无关。
- QuotaTracker：固定日窗口计数，约束累计用量；窗口对齐到 Unix 日界，恢复时刻确定。
- 两者均按租户隔离、加锁保证并发下不超发/漏扣，并通过惰性清扫 + 上限保证内存有界。
"""
from __future__ import annotations

import math
import threading
from dataclasses import dataclass

DAY_SECONDS = 86_400.0


@dataclass(frozen=True)
class LimitDecision:
    allowed: bool
    remaining: float
    retry_after_seconds: float | None


class RateLimiter:
    """每租户一个令牌桶。容量 = 每分钟配额，速率 = 配额/60 每秒。"""

    def __init__(self, max_tenants: int = 10_000) -> None:
        self._lock = threading.Lock()
        self._buckets: dict[str, tuple[float, float]] = {}  # tenant -> (tokens, last_ts)
        self._max_tenants = max_tenants

    def check(self, tenant_id: str, per_minute: int, now: float) -> LimitDecision:
        rate = per_minute / 60.0
        with self._lock:
            tokens, last = self._buckets.get(tenant_id, (float(per_minute), now))
            # 惰性补充：只依赖时间差，与并发调用次数无关
            tokens = min(float(per_minute), tokens + max(0.0, now - last) * rate)
            if tokens >= 1.0:
                decision = LimitDecision(True, tokens - 1.0, None)
                self._buckets[tenant_id] = (tokens - 1.0, now)
            else:
                wait = (1.0 - tokens) / rate
                decision = LimitDecision(False, 0.0, math.ceil(wait * 1000) / 1000)
                self._buckets[tenant_id] = (tokens, now)
            self._evict_if_needed()
            return decision

    def _evict_if_needed(self) -> None:
        if len(self._buckets) <= self._max_tenants:
            return
        # 逐出最久未活动的桶，保持内存有界
        for victim in sorted(self._buckets, key=lambda t: self._buckets[t][1])[: len(self._buckets) - self._max_tenants]:
            del self._buckets[victim]

    def inspect(self, tenant_id: str, per_minute: int, now: float) -> LimitDecision:
        """只读快照（不消耗令牌）：剩余额度与预计恢复秒数，供用量视图使用。

        与 check 同一把锁内惰性补充后读取，并发下与扣减结果自洽。
        """
        rate = per_minute / 60.0
        with self._lock:
            tokens, last = self._buckets.get(tenant_id, (float(per_minute), now))
            tokens = min(float(per_minute), tokens + max(0.0, now - last) * rate)
            if tokens >= 1.0:
                return LimitDecision(True, tokens, None)
            wait = (1.0 - tokens) / rate
            return LimitDecision(False, tokens, math.ceil(wait * 1000) / 1000)


class QuotaTracker:
    """每租户每窗口一个计数器；窗口 = floor(now / DAY_SECONDS)。"""

    def __init__(self, max_entries: int = 20_000) -> None:
        self._lock = threading.Lock()
        self._counts: dict[tuple[str, int], int] = {}
        self._max_entries = max_entries

    @staticmethod
    def window_of(now: float) -> int:
        return int(now // DAY_SECONDS)

    def consume(self, tenant_id: str, per_day: int, now: float) -> LimitDecision:
        window = self.window_of(now)
        key = (tenant_id, window)
        with self._lock:
            self._sweep(current_window=window)
            used = self._counts.get(key, 0)
            if used < per_day:
                self._counts[key] = used + 1
                self._evict_overflow()
                return LimitDecision(True, per_day - used - 1, None)
            retry_after = round((window + 1) * DAY_SECONDS - now, 3)
            return LimitDecision(False, 0.0, max(retry_after, 0.0))

    def usage(self, tenant_id: str, now: float) -> int:
        with self._lock:
            return self._counts.get((tenant_id, self.window_of(now)), 0)

    def inspect(self, tenant_id: str, per_day: int, now: float) -> dict:
        """只读快照：已用/剩余/窗口重置时刻，供用量视图使用（锁内读取，并发自洽）。"""
        window = self.window_of(now)
        with self._lock:
            used = self._counts.get((tenant_id, window), 0)
        return {
            "used": used,
            "limit": per_day,
            "remaining": max(per_day - used, 0),
            "reset_at": (window + 1) * DAY_SECONDS,
        }

    def _sweep(self, current_window: int) -> None:
        # 回收历史窗口；窗口判定只依赖时间，回收不影响当前窗口结果
        stale = [k for k in self._counts if k[1] < current_window]
        for k in stale:
            del self._counts[k]
        self._evict_overflow()

    def _evict_overflow(self) -> None:
        if len(self._counts) > self._max_entries:
            for victim in list(self._counts)[: len(self._counts) - self._max_entries]:
                del self._counts[victim]
