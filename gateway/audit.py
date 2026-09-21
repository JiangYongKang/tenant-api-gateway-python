"""请求级审计：贯穿标识、可检索、不含凭据原文。

- 每条记录携带 request_id（贯穿标识），被处理/被拒绝/被降级的请求都会落一条；
- AuditEvent 的字段集合中不存在凭据位，从结构上保证不记录凭据原文；
- 日志容量有界（环形淘汰最旧记录），长时间运行内存不漂移。
"""
from __future__ import annotations

import threading
import uuid
from collections import deque
from dataclasses import dataclass


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
    """线程安全的有界审计日志，支持按请求标识/租户检索。"""

    def __init__(self, max_events: int = 10000) -> None:
        if max_events < 1:
            raise ValueError("max_events 必须 >= 1")
        self._lock = threading.Lock()
        self._events: deque[AuditEvent] = deque(maxlen=max_events)

    def record(self, event: AuditEvent) -> None:
        with self._lock:
            self._events.append(event)  # 超过 maxlen 自动淘汰最旧记录

    def find_by_request_id(self, request_id: str) -> AuditEvent | None:
        with self._lock:
            for event in self._events:
                if event.request_id == request_id:
                    return event
            return None

    def find_by_tenant(self, tenant_id: str) -> list[AuditEvent]:
        with self._lock:
            return [e for e in self._events if e.tenant_id == tenant_id]

    def all(self) -> list[AuditEvent]:
        with self._lock:
            return list(self._events)

    def __len__(self) -> int:
        with self._lock:
            return len(self._events)

    @staticmethod
    def new_request_id() -> str:
        return uuid.uuid4().hex
