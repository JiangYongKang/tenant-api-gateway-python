"""治理配置：可外部调整、即时生效、非法配置明确拒绝。"""
from __future__ import annotations

import threading
from dataclasses import dataclass


@dataclass(frozen=True)
class GovernanceConfig:
    """治理配置快照（不可变，整体原子替换）。"""

    rate_limit_per_second: float = 10.0
    rate_burst: int = 20
    quota_limit: int = 1000
    quota_window_seconds: float = 3600.0
    idempotency_ttl_seconds: float = 86400.0
    max_tracked_tenants: int = 10000
    max_idempotency_keys: int = 100000
    degraded_mode: str = "fail_closed"  # fail_closed | fail_open_limited


class ConfigRegistry:
    """线程安全的配置注册表：读取拿到一致快照，更新整体校验后原子替换。"""

    def __init__(self, initial: GovernanceConfig | None = None) -> None:
        self._lock = threading.Lock()
        self._config = initial or GovernanceConfig()

    def get(self) -> GovernanceConfig:
        raise NotImplementedError

    def update(self, **changes: object) -> GovernanceConfig:
        """校验并原子替换；非法配置抛 ConfigError，不静默回退。"""
        raise NotImplementedError
