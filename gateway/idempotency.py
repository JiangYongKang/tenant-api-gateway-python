"""幂等记录：重复提交保护，区分冲突、合法重放与非法复用。

语义约定：
- 记录以 (tenant_id, key) 为索引，租户之间天然隔离；
- begin 在单锁内完成"检查 + 占位"，并发下同标识只有一个请求拿到 NEW；
- 处理失败必须 abort 释放占位，避免半更新状态阻塞后续合法重试；
- 记录有 TTL 且有容量上限，过期记录由 sweep 或写入时惰性回收。
"""
from __future__ import annotations

import enum
import threading
from dataclasses import dataclass


class IdempotencyVerdict(enum.Enum):
    NEW = "new"                  # 首次提交，可执行副作用
    REPLAY = "replay"            # 有效期内同标识同载荷，应返回首个结果
    CONFLICT = "conflict"        # 同标识但载荷不同，非法复用
    IN_PROGRESS = "in_progress"  # 同标识正在处理中


@dataclass(frozen=True)
class IdempotencyRecord:
    fingerprint: str
    status: str  # in_progress | completed
    response_body: str | None = None
    response_status: int | None = None
    expires_at: float = 0.0


_IN_PROGRESS = "in_progress"
_COMPLETED = "completed"


class IdempotencyStore:
    """有界、按 TTL 回收的幂等记录存储。"""

    def __init__(self, max_keys: int = 100000) -> None:
        if max_keys < 1:
            raise ValueError("max_keys 必须 >= 1")
        self._lock = threading.Lock()
        self._records: dict[tuple[str, str], IdempotencyRecord] = {}
        self._max_keys = max_keys

    @property
    def size(self) -> int:
        with self._lock:
            return len(self._records)

    def begin(
        self, tenant_id: str, key: str, fingerprint: str, now: float, ttl: float
    ) -> IdempotencyVerdict:
        with self._lock:
            index = (tenant_id, key)
            existing = self._records.get(index)
            if existing is not None and existing.expires_at <= now:
                del self._records[index]  # 过期记录惰性回收
                existing = None

            if existing is None:
                self._evict_if_full(now)
                self._records[index] = IdempotencyRecord(
                    fingerprint=fingerprint, status=_IN_PROGRESS, expires_at=now + ttl
                )
                return IdempotencyVerdict.NEW

            if existing.fingerprint != fingerprint:
                return IdempotencyVerdict.CONFLICT
            if existing.status == _IN_PROGRESS:
                return IdempotencyVerdict.IN_PROGRESS
            return IdempotencyVerdict.REPLAY

    def complete(self, tenant_id: str, key: str, status: int, body: str) -> None:
        """把 in_progress 记录落为已完成并保存响应，供合法重放返回首个结果。"""
        with self._lock:
            index = (tenant_id, key)
            existing = self._records.get(index)
            if existing is None or existing.status != _IN_PROGRESS:
                return
            self._records[index] = IdempotencyRecord(
                fingerprint=existing.fingerprint,
                status=_COMPLETED,
                response_body=body,
                response_status=status,
                expires_at=existing.expires_at,
            )

    def abort(self, tenant_id: str, key: str) -> None:
        """处理失败时释放 in_progress 占位，避免半更新状态。"""
        with self._lock:
            index = (tenant_id, key)
            existing = self._records.get(index)
            if existing is not None and existing.status == _IN_PROGRESS:
                del self._records[index]

    def lookup(self, tenant_id: str, key: str, now: float) -> IdempotencyRecord | None:
        with self._lock:
            record = self._records.get((tenant_id, key))
            if record is None or record.expires_at <= now:
                return None
            return record

    def sweep(self, now: float) -> int:
        """回收过期记录，返回回收数量。"""
        with self._lock:
            expired = [k for k, r in self._records.items() if r.expires_at <= now]
            for k in expired:
                del self._records[k]
            return len(expired)

    def _evict_if_full(self, now: float) -> None:
        """容量满时先清过期，再驱逐最早过期的记录，保证内存有界。"""
        if len(self._records) < self._max_keys:
            return
        expired = [k for k, r in self._records.items() if r.expires_at <= now]
        for k in expired:
            del self._records[k]
        if len(self._records) >= self._max_keys:
            oldest = min(self._records.items(), key=lambda kv: kv[1].expires_at)[0]
            del self._records[oldest]
