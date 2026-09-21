"""副作用请求的重复提交保护。

- 同一 (tenant, key) 在 TTL 内重复提交不产生第二次副作用。
- 三种情形可区分：合法重放(REPLAY)、标识冲突(CONFLICT)、处理中(IN_PROGRESS)。
- 记录有界（max_entries）且按 TTL 惰性回收，内存不随时间漂移。
"""
from __future__ import annotations

import enum
import threading
from dataclasses import dataclass


class BeginResult(enum.Enum):
    NEW = "new"                    # 首次提交，允许执行副作用
    REPLAY = "replay"              # 合法重放：同租户同指纹，返回缓存结果
    IN_PROGRESS = "in_progress"    # 相同请求正在处理中
    CONFLICT = "conflict"          # 非法复用：同标识但指纹或租户不同


@dataclass(frozen=True)
class BeginOutcome:
    result: BeginResult
    cached_response: tuple[int, bytes] | None = None


@dataclass
class _Record:
    tenant_id: str
    fingerprint: str
    expires_at: float
    response: tuple[int, bytes] | None  # None 表示处理中


class IdempotencyStore:
    def __init__(self, max_entries: int = 10_000) -> None:
        self._lock = threading.Lock()
        self._records: dict[tuple[str, str], _Record] = {}
        self._max_entries = max_entries

    def begin(self, tenant_id: str, key: str, fingerprint: str, ttl: float, now: float) -> BeginOutcome:
        map_key = (tenant_id, key)
        with self._lock:
            self._sweep(now)
            rec = self._records.get(map_key)
            if rec is None:
                self._records[map_key] = _Record(tenant_id, fingerprint, now + ttl, None)
                self._evict_overflow()
                return BeginOutcome(BeginResult.NEW)
            if rec.fingerprint != fingerprint:
                # 同租户同标识但请求体不同 -> 非法复用
                return BeginOutcome(BeginResult.CONFLICT)
            if rec.response is None:
                return BeginOutcome(BeginResult.IN_PROGRESS)
            return BeginOutcome(BeginResult.REPLAY, cached_response=rec.response)

    def complete(self, tenant_id: str, key: str, status: int, body: bytes, now: float) -> None:
        map_key = (tenant_id, key)
        with self._lock:
            rec = self._records.get(map_key)
            # 仅当记录仍有效且属于该租户时落结果，避免半更新状态
            if rec is not None and rec.expires_at > now and rec.tenant_id == tenant_id:
                rec.response = (status, body)

    def size(self) -> int:
        with self._lock:
            return len(self._records)

    def _sweep(self, now: float) -> None:
        expired = [k for k, r in self._records.items() if r.expires_at <= now]
        for k in expired:
            del self._records[k]
        self._evict_overflow()

    def _evict_overflow(self) -> None:
        if len(self._records) > self._max_entries:
            victims = sorted(self._records, key=lambda k: self._records[k].expires_at)
            for k in victims[: len(self._records) - self._max_entries]:
                del self._records[k]
