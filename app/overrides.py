"""租户级差异化治理策略。

优先级（唯一且可解释）：逐字段覆盖 —— 租户专属设置存在的字段覆盖全局默认，
未设置的字段回落全局配置；作用范围仅限该租户，其他租户不受影响。

- 非法（未知项/类型错/越界/空覆盖）当场抛 ConfigValidationError，原有策略原样保留。
- 生效后立即影响该租户的后续判定；进行中的请求持有各自快照，判定不自相矛盾。
- 存储有界（max_tenants），超出即拒绝，运行期不膨胀。
"""
from __future__ import annotations

import threading
from dataclasses import dataclass

from .config import ConfigValidationError, GovernanceConfig, validate_policy_fields

OVERRIDABLE_FIELDS = ("rate_limit_per_minute", "quota_per_day", "idempotency_ttl_seconds")


@dataclass(frozen=True)
class TenantPolicy:
    """单个租户生效中的策略视图（全局默认与专属覆盖合并后的结果）。"""

    rate_limit_per_minute: int
    quota_per_day: int
    idempotency_ttl_seconds: float
    overridden_fields: tuple[str, ...]


class TenantPolicyStore:
    """持有各租户的策略覆盖；校验失败即抛 ConfigValidationError，旧值不变。"""

    def __init__(self, max_tenants: int = 10_000) -> None:
        self._lock = threading.Lock()
        self._overrides: dict[str, dict[str, object]] = {}
        self._max_tenants = max_tenants

    def set_override(self, tenant_id: str, data: dict) -> TenantPolicy:
        validated = validate_policy_fields(data)  # 先校验，失败则抛错、旧值不变
        with self._lock:
            if tenant_id not in self._overrides and len(self._overrides) >= self._max_tenants:
                raise ConfigValidationError(f"策略覆盖租户数已达上限 {self._max_tenants}")
            merged = {**self._overrides.get(tenant_id, {}), **validated}
            self._overrides[tenant_id] = merged
            return self._build(tenant_id, merged)

    def clear_override(self, tenant_id: str) -> bool:
        with self._lock:
            return self._overrides.pop(tenant_id, None) is not None

    def effective_for(self, tenant_id: str, base: GovernanceConfig) -> TenantPolicy:
        with self._lock:
            override = self._overrides.get(tenant_id, {})
        return TenantPolicy(
            rate_limit_per_minute=int(override.get("rate_limit_per_minute", base.rate_limit_per_minute)),
            quota_per_day=int(override.get("quota_per_day", base.quota_per_day)),
            idempotency_ttl_seconds=float(override.get("idempotency_ttl_seconds", base.idempotency_ttl_seconds)),
            overridden_fields=tuple(sorted(override)),
        )

    def _build(self, tenant_id: str, merged: dict) -> TenantPolicy:
        base = GovernanceConfig()
        return TenantPolicy(
            rate_limit_per_minute=int(merged.get("rate_limit_per_minute", base.rate_limit_per_minute)),
            quota_per_day=int(merged.get("quota_per_day", base.quota_per_day)),
            idempotency_ttl_seconds=float(merged.get("idempotency_ttl_seconds", base.idempotency_ttl_seconds)),
            overridden_fields=tuple(sorted(merged)),
        )
