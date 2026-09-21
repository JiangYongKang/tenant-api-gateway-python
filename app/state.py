"""治理运行状态后端抽象。

中间件在每个请求前读取治理心跳（配置版本号），以确认治理状态可用。
本地内存实现支持故障与延迟注入，用于在测试中复现"状态不可读 / 响应变慢"
两类降级触发条件；不依赖任何外部服务。
"""
from __future__ import annotations

import threading
import time


class StateUnavailableError(Exception):
    """运行状态不可读或读取超时。"""


class StateBackend:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._failure: Exception | None = None
        self._latency: float = 0.0
        self._revision = 0

    # ---- 测试/运维注入接口 ----
    def set_failure(self, exc: Exception | None) -> None:
        with self._lock:
            self._failure = exc

    def set_latency(self, seconds: float) -> None:
        with self._lock:
            self._latency = max(0.0, seconds)

    def bump_revision(self) -> int:
        with self._lock:
            self._revision += 1
            return self._revision

    # ---- 治理读取路径 ----
    def read_revision(self) -> int:
        """读取治理状态版本；故障或注入延迟时按原样表现，由调用方判定超时。"""
        with self._lock:
            failure, latency, revision = self._failure, self._latency, self._revision
        if latency > 0:
            time.sleep(latency)
        if failure is not None:
            raise StateUnavailableError(str(failure)) from failure
        return revision

    def check_available(self, timeout: float, clock=time.monotonic) -> int:
        """在超时预算内读取版本；超时或故障统一抛 StateUnavailableError。"""
        start = clock()
        revision = self.read_revision()
        if clock() - start > timeout:
            raise StateUnavailableError(f"state read exceeded {timeout}s budget")
        return revision
