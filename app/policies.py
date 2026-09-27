"""租户级差异化治理策略（覆盖全局默认值）。

- 每个租户可单独覆盖 rate_limit_per_minute / quota_per_day / idempotency_ttl_seconds；
  未覆盖的字段回落到全局配置（ConfigStore）。state_failure_mode 为全局开关，不可按租户覆盖。
- 优先级唯一且可解释：逐字段合并 —— 租户覆盖值 > 全局默认值；作用范围仅限该租户。
- 非法（未知字段、类型错误、越界、空覆盖）一律抛 PolicyValidationError，
  原有策略原样保留，绝不静默退回；生效后立即影响该租户后续请求的判定。
- 覆盖表有界（max_overrides），超出即拒绝，内存不随运行时间膨胀。
"""
from __future__ import annotations

import threading
from dataclasses import dataclass

from .config import GovernanceConfig

OVERRIDABLE_FIELDS = ("rate_limit_per_minute", "quota_per_day", "idempotency_ttl_seconds")

# 与全局配置一致的取值边界（见 app/config.py）
_BOUNDS = {
    "rate_limit_per_minute": (1, 1_000_000, "int"),
    "quota_per_day": (1, 100_000_000, "int"),
    "idempotency_ttl_seconds": (1.0, 7 * 86_400.0, "float"),
}


class PolicyValidationError(ValueError):
    pass


@dataclass(frozen=True)
class TenantPolicy:
    """某租户的覆盖项；None 表示该字段不覆盖、回落全局默认。"""

    rate_limit_per_minute: int | None = None
    quota_per_day: int | None = None
    idempotency_ttl_seconds: float | None = None


def validate_policy(data: dict) -> TenantPolicy:
    if not isinstance(data, dict):
        raise PolicyValidationError(f"策略必须是 dict，得到 {type(data).__name__}")
    unknown = set(data) - set(OVERRIDABLE_FIELDS)
    if unknown:
        raise PolicyValidationError(f"不可覆盖或未知的字段: {sorted(unknown)}（允许 {list(OVERRIDABLE_FIELDS)}）")
    if not data:
        raise PolicyValidationError("空覆盖无意义：至少给出一个可覆盖字段")

    values: dict[str, object] = {}
    for field, raw in data.items():
        lo, hi, kind = _BOUNDS[field]
        if kind == "int":
            if isinstance(raw, bool) or not isinstance(raw, int):
                raise PolicyValidationError(f"{field}: 期望整数，得到 {type(raw).__name__}")
            v: object = raw
        else:
            if isinstance(raw, bool) or not isinstance(raw, (int, float)):
                raise PolicyValidationError(f"{field}: 期望数值，得到 {type(raw).__name__}")
            v = float(raw)
        if not (lo <= v <= hi):  # type: ignore[operator]
            raise PolicyValidationError(f"{field}: 取值 {v} 越界 [{lo}, {hi}]")
        values[field] = v
    return TenantPolicy(**values)  # type: ignore[arg-type]


def effective_config(base: GovernanceConfig, override: TenantPolicy | None) -> GovernanceConfig:
    """逐字段合并：租户覆盖优先，未覆盖字段回落全局默认。"""
    if override is None:
        return base
    return GovernanceConfig(
        rate_limit_per_minute=override.rate_limit_per_minute
        if override.rate_limit_per_minute is not None else base.rate_limit_per_minute,
        quota_per_day=override.quota_per_day
        if override.quota_per_day is not None else base.quota_per_day,
        idempotency_ttl_seconds=override.idempotency_ttl_seconds
        if override.idempotency_ttl_seconds is not None else base.idempotency_ttl_seconds,
        state_failure_mode=base.state_failure_mode,  # 全局开关，不按租户覆盖
    )


class PolicyStore:
    """持有各租户覆盖策略；写入整体校验、原子替换。"""

    def __init__(self, max_overrides: int = 10_000) -> None:
        self._lock = threading.Lock()
        self._overrides: dict[str, TenantPolicy] = {}
        self._max_overrides = max_overrides

    def get(self, tenant_id: str) -> TenantPolicy | None:
        with self._lock:
            return self._overrides.get(tenant_id)

    def set(self, tenant_id: str, data: dict) -> TenantPolicy:
        policy = validate_policy(data)  # 校验失败抛错，原有策略不变
        with self._lock:
            if tenant_id not in self._overrides and len(self._overrides) >= self._max_overrides:
                raise PolicyValidationError(f"覆盖策略条目已达上限（{self._max_overrides}）")
            self._overrides[tenant_id] = policy
        return policy

    def remove(self, tenant_id: str) -> bool:
        """移除覆盖，该租户回落到全局默认。返回是否存在过覆盖。"""
        with self._lock:
            return self._overrides.pop(tenant_id, None) is not None

    def list_all(self) -> dict[str, TenantPolicy]:
        with self._lock:
            return dict(self._overrides)
