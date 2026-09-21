"""请求级审计：贯穿标识、可检索、不含凭据原文。"""
from __future__ import annotations

import threading
import uuid
from dataclasses import dataclass, field


@dataclass(frozen=True)
class AuditEvent:
    request_id: str
    tenant_id: str | None
    method: str
    path: str
    outcome: str  # allowed | rejected | degraded | replayed
    reason: str | None
    status_code: int
    latency_ms: float


class AuditLog:
    """线程安全的内存审计日志，支持按请求标识/租户检索。"""

    def __init__(self, max_events: int = 10000) -> None:
        self._lock = threading.Lock()
        self._events: list[AuditEvent] = []
        self._max_events = max_events

    def record(self, event: AuditEvent) -> None:
        raise NotImplementedError

    def find_by_request_id(self, request_id: str) -> AuditEvent | None:
        raise NotImplementedError

    def find_by_tenant(self, tenant_id: str) -> list[AuditEvent]:
        raise NotImplementedError

    @staticmethod
    def new_request_id() -> str:
        return uuid.uuid4().hex
