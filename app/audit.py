"""请求级审计与贯穿标识。

- 每个被处理/拒绝/降级/重放的请求都落一条记录，request_id 贯穿全链路。
- 记录只含判定依据与元信息，绝不包含凭据原文等敏感信息。
- 有界环形缓冲：写满后覆盖最旧记录，内存不随运行时间漂移。
"""
from __future__ import annotations

import enum
import threading
from collections import deque
from dataclasses import dataclass


class AuditOutcome(enum.Enum):
    PROCESSED = "processed"
    REJECTED = "rejected"
    DEGRADED = "degraded"
    REPLAYED = "replayed"


@dataclass(frozen=True)
class AuditRecord:
    request_id: str
    tenant_id: str | None
    path: str
    method: str
    outcome: AuditOutcome
    reason: str | None
    status_code: int
    timestamp: float


class AuditLog:
    def __init__(self, max_records: int = 10_000) -> None:
        self._lock = threading.Lock()
        self._records: deque[AuditRecord] = deque(maxlen=max_records)

    def record(self, entry: AuditRecord) -> None:
        # 防御：审计内容不允许夹带凭据原文
        assert "tnt_" not in (entry.reason or ""), "audit reason must not contain credentials"
        with self._lock:
            self._records.append(entry)

    def query(
        self,
        tenant_id: str | None = None,
        outcome: AuditOutcome | None = None,
        request_id: str | None = None,
    ) -> list[AuditRecord]:
        with self._lock:
            items = list(self._records)
        return [
            r
            for r in items
            if (tenant_id is None or r.tenant_id == tenant_id)
            and (outcome is None or r.outcome is outcome)
            and (request_id is None or r.request_id == request_id)
        ]

    def __len__(self) -> int:
        with self._lock:
            return len(self._records)
