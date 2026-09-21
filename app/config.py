"""治理配置：外部可调、强校验、原子生效。

- 非法字段、非法类型、越界取值一律抛 ConfigValidationError，绝不静默回退默认。
- ConfigStore.update 先整体校验再原子替换；进行中的请求持有各自快照，判定不自相矛盾。
"""
from __future__ import annotations

import threading
from dataclasses import dataclass

ALLOWED_FAILURE_MODES = ("deny", "allow_with_degradation")


@dataclass(frozen=True)
class GovernanceConfig:
    rate_limit_per_minute: int = 60
    quota_per_day: int = 10_000
    idempotency_ttl_seconds: float = 86_400.0
    state_failure_mode: str = "deny"  # deny | allow_with_degradation


class ConfigValidationError(ValueError):
    pass


def _check_int(name: str, value: object, lo: int, hi: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigValidationError(f"{name}: 期望整数，得到 {type(value).__name__}")
    if not (lo <= value <= hi):
        raise ConfigValidationError(f"{name}: 取值 {value} 越界 [{lo}, {hi}]")
    return value


def _check_float(name: str, value: object, lo: float, hi: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigValidationError(f"{name}: 期望数值，得到 {type(value).__name__}")
    v = float(value)
    if not (lo <= v <= hi):
        raise ConfigValidationError(f"{name}: 取值 {v} 越界 [{lo}, {hi}]")
    return v


def validate_config(data: dict) -> GovernanceConfig:
    if not isinstance(data, dict):
        raise ConfigValidationError(f"配置必须是 dict，得到 {type(data).__name__}")
    known = {"rate_limit_per_minute", "quota_per_day", "idempotency_ttl_seconds", "state_failure_mode"}
    unknown = set(data) - known
    if unknown:
        raise ConfigValidationError(f"未知配置项: {sorted(unknown)}")

    base = GovernanceConfig()
    rate = _check_int("rate_limit_per_minute", data.get("rate_limit_per_minute", base.rate_limit_per_minute), 1, 1_000_000)
    quota = _check_int("quota_per_day", data.get("quota_per_day", base.quota_per_day), 1, 100_000_000)
    ttl = _check_float("idempotency_ttl_seconds", data.get("idempotency_ttl_seconds", base.idempotency_ttl_seconds), 1.0, 7 * 86_400.0)
    mode = data.get("state_failure_mode", base.state_failure_mode)
    if not isinstance(mode, str) or mode not in ALLOWED_FAILURE_MODES:
        raise ConfigValidationError(f"state_failure_mode: 必须是 {ALLOWED_FAILURE_MODES} 之一，得到 {mode!r}")
    return GovernanceConfig(
        rate_limit_per_minute=rate,
        quota_per_day=quota,
        idempotency_ttl_seconds=ttl,
        state_failure_mode=mode,
    )


class ConfigStore:
    """持有当前配置；更新时整体校验、原子替换。"""

    def __init__(self, initial: GovernanceConfig | None = None) -> None:
        self._lock = threading.Lock()
        self._current: GovernanceConfig = initial if initial is not None else GovernanceConfig()

    def get(self) -> GovernanceConfig:
        with self._lock:
            return self._current

    def update(self, data: dict) -> GovernanceConfig:
        new = validate_config(data)  # 校验失败则抛错，旧配置保持不变
        with self._lock:
            self._current = new
        return new
