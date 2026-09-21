"""幂等记录：重复提交保护，区分冲突、合法重放与非法复用。"""
from __future__ import annotations

import enum
import threading
from dataclasses import dataclass


class IdempotencyVerdict(enum.Enum):
    NEW = "new"                # 首次提交，可执行副作用
    REPLAY = "replay"          # 有效期内同标识同载荷，返回首个结果
    CONFLICT = "conflict"      # 同标识但载荷不同，非法复用
    IN_PROGRESS = "in_progress"  # 同标识正在处理中


@dataclass(frozen=True)
class IdempotencyRecord:
    fingerprint: str
    status: str  # in_progress | completed
    response_body: str | None = None
    response_status: int | None = None
    expires_at: float = 0.0


class IdempotencyStore:
    """有界、按 TTL 回收的幂等记录存储。"""

    def __init__(self, max_keys: int = 100000) -> None:
        self._lock = threading.Lock()
        self._records: dict[tuple[str, str], IdempotencyRecord] = {}
        self._max_keys = max_keys

    def begin(self, tenant_id: str, key: str, fingerprint: str, now: float, ttl: float) -> IdempotencyVerdict:
        raise NotImplementedError

    def complete(self, tenant_id: str, key: str, status: int, body: str) -> None:
        raise NotImplementedError

    def abort(self, tenant_id: str, key: str) -> None:
        """处理失败时释放记录，避免半更新状态。"""
        raise NotImplementedError

    def lookup(self, tenant_id: str, key: str, now: float) -> IdempotencyRecord | None:
        raise NotImplementedError

    def sweep(self, now: float) -> int:
        """回收过期记录，返回回收数量。"""
        raise NotImplementedError
