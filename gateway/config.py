"""治理配置：可外部调整、即时生效、非法配置明确拒绝。"""
from __future__ import annotations

import dataclasses
import threading
from dataclasses import dataclass

from gateway.errors import ConfigError

_VALID_DEGRADED_MODES = ("fail_closed", "fail_open_limited")


@dataclass(frozen=True)
class GovernanceConfig:
    """治理配置快照（不可变，整体原子替换）。

    - rate_limit_per_second / rate_burst: 限流（令牌桶，约束短时间速率）
    - quota_limit / quota_window_seconds: 配额（窗口内累计用量）
    """

    rate_limit_per_second: float = 10.0
    rate_burst: int = 20
    quota_limit: int = 1000
    quota_window_seconds: float = 3600.0
    idempotency_ttl_seconds: float = 86400.0
    max_tracked_tenants: int = 10000
    max_idempotency_keys: int = 100000
    degraded_mode: str = "fail_closed"

    def validate(self) -> None:
        """非法或不兼容配置直接抛 ConfigError，绝不静默回退默认值。"""
        errors: list[str] = []
        if not isinstance(self.rate_limit_per_second, (int, float)) or self.rate_limit_per_second <= 0:
            errors.append("rate_limit_per_second 必须为正数")
        if not isinstance(self.rate_burst, int) or self.rate_burst < 1:
            errors.append("rate_burst 必须为 >=1 的整数")
        if not isinstance(self.quota_limit, int) or self.quota_limit < 1:
            errors.append("quota_limit 必须为 >=1 的整数")
        if not isinstance(self.quota_window_seconds, (int, float)) or self.quota_window_seconds <= 0:
            errors.append("quota_window_seconds 必须为正数")
        if not isinstance(self.idempotency_ttl_seconds, (int, float)) or self.idempotency_ttl_seconds <= 0:
            errors.append("idempotency_ttl_seconds 必须为正数")
        if not isinstance(self.max_tracked_tenants, int) or self.max_tracked_tenants < 1:
            errors.append("max_tracked_tenants 必须为 >=1 的整数")
        if not isinstance(self.max_idempotency_keys, int) or self.max_idempotency_keys < 1:
            errors.append("max_idempotency_keys 必须为 >=1 的整数")
        if self.degraded_mode not in _VALID_DEGRADED_MODES:
            errors.append(
                f"degraded_mode 必须是 {sorted(_VALID_DEGRADED_MODES)} 之一，收到 {self.degraded_mode!r}"
            )
        # 兼容性约束：突发额度不应小于每秒速率的整数部分，否则稳态请求会被误拒
        if not errors and self.rate_burst < 1:
            errors.append("rate_burst 与 rate_limit_per_second 不兼容")
        if errors:
            raise ConfigError("; ".join(errors))


class ConfigRegistry:
    """线程安全的配置注册表。

    - get(): 返回当前不可变快照，进行中的请求自始至终使用同一份快照，判定不自相矛盾；
    - update(): 先构造并整体校验，通过后在锁内原子替换，即时生效；失败则旧配置保持不变。
    """

    def __init__(self, initial: GovernanceConfig | None = None) -> None:
        self._lock = threading.Lock()
        config = initial or GovernanceConfig()
        config.validate()
        self._config = config

    def get(self) -> GovernanceConfig:
        with self._lock:
            return self._config

    def update(self, **changes: object) -> GovernanceConfig:
        if not changes:
            raise ConfigError("未提供任何配置变更")
        valid_fields = {f.name for f in dataclasses.fields(GovernanceConfig)}
        unknown = sorted(set(changes) - valid_fields)
        if unknown:
            raise ConfigError(f"未知配置项: {unknown}")
        with self._lock:
            candidate = dataclasses.replace(self._config, **changes)
            candidate.validate()  # 非法配置在此抛出，旧配置不受影响
            self._config = candidate
            return candidate
