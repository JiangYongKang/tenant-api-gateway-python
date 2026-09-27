"""租户在线生命周期管理。

状态机与迁移规则：
    (不存在) --PROVISION--> ACTIVE
    ACTIVE   --DISABLE-----> DISABLED     （临时停用，可 ENABLE 恢复）
    DISABLED --ENABLE------> ACTIVE
    ACTIVE   --DRAIN-------> DRAINING     （过渡态：新请求得 tenant_draining，在处理请求正常收尾）
    DRAINING --(到期)------> DISABLED     （惰性求值，见 TenantRegistry.effective_status）

约束：
- 批量调整先整体校验、再原子生效：任一操作非法或互相冲突，整批拒绝，状态完全不变。
- 冲突判定：同一批内对同一租户出现多个操作、对不存在租户做变更、重复开通、
  非法状态迁移（如 ENABLE 一个 ACTIVE 租户、DRAIN 一个 DISABLED 租户）均属非法。
- 注册表有界：PROVISION 使总数超过 max_tenants 时整批拒绝，运行期数据不膨胀。
"""
from __future__ import annotations

import enum
import re
from dataclasses import dataclass

from .tenancy import TenantRecord, TenantRegistry, TenantStatus

_TENANT_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]{1,63}$")
MAX_DRAIN_SECONDS = 7 * 86_400.0


class LifecycleAction(enum.Enum):
    PROVISION = "provision"      # 开通新租户
    ROTATE = "rotate"            # 换发凭据（旧凭据立即失效）
    DISABLE = "disable"          # 临时停用（可恢复）
    ENABLE = "enable"            # 恢复已停用租户
    DRAIN = "drain"              # 安排平滑下线：进入过渡态，到期自动停用


class LifecycleError(ValueError):
    """生命周期调整非法或互相冲突；批量操作时整体拒绝。"""


@dataclass(frozen=True)
class LifecycleOperation:
    action: LifecycleAction
    tenant_id: str
    drain_seconds: float | None = None  # 仅 DRAIN：过渡态时长


def parse_operation(data: dict) -> LifecycleOperation:
    """把外部输入（dict）解析为操作；非法输入抛 LifecycleError。"""
    if not isinstance(data, dict):
        raise LifecycleError(f"操作必须是 dict，得到 {type(data).__name__}")
    known = {"action", "tenant_id", "drain_seconds"}
    unknown = set(data) - known
    if unknown:
        raise LifecycleError(f"未知字段: {sorted(unknown)}")
    raw_action = data.get("action")
    try:
        action = LifecycleAction(raw_action)
    except ValueError:
        raise LifecycleError(f"未知 action: {raw_action!r}") from None
    tenant_id = data.get("tenant_id")
    if not isinstance(tenant_id, str) or not _TENANT_ID_PATTERN.match(tenant_id):
        raise LifecycleError(f"非法 tenant_id: {tenant_id!r}")
    drain_seconds = data.get("drain_seconds")
    if action is LifecycleAction.DRAIN:
        if isinstance(drain_seconds, bool) or not isinstance(drain_seconds, (int, float)):
            raise LifecycleError("DRAIN 需要数值型 drain_seconds")
        drain_seconds = float(drain_seconds)
        if not (0.0 < drain_seconds <= MAX_DRAIN_SECONDS):
            raise LifecycleError(f"drain_seconds 越界 (0, {MAX_DRAIN_SECONDS}]")
    elif drain_seconds is not None:
        raise LifecycleError(f"{action.value} 不接受 drain_seconds")
    return LifecycleOperation(action=action, tenant_id=tenant_id, drain_seconds=drain_seconds)


class TenantLifecycleService:
    """对注册表执行原子批量生命周期调整。"""

    def __init__(self, registry: TenantRegistry, max_tenants: int = 10_000) -> None:
        self._registry = registry
        self._max_tenants = max_tenants

    def apply(self, operations: list[LifecycleOperation], now: float) -> None:
        if not operations:
            raise LifecycleError("空操作批次")
        seen: set[str] = set()
        for op in operations:
            if op.tenant_id in seen:
                raise LifecycleError(f"同批内对租户 {op.tenant_id!r} 出现多个操作，互相冲突")
            seen.add(op.tenant_id)

        def txn(records: dict[str, TenantRecord]) -> dict[str, TenantRecord]:
            new_records = dict(records)
            for op in operations:
                new_records = self._apply_one(new_records, op, now)
            if len(new_records) > self._max_tenants:
                raise LifecycleError(f"租户总数将超过上限 {self._max_tenants}，整批拒绝")
            return new_records

        self._registry.transact(txn)  # 抛错则注册表完全不变

    def _apply_one(
        self, records: dict[str, TenantRecord], op: LifecycleOperation, now: float
    ) -> dict[str, TenantRecord]:
        rec = records.get(op.tenant_id)
        effective = TenantRegistry.effective_status(rec, now) if rec else None

        if op.action is LifecycleAction.PROVISION:
            if rec is not None:
                raise LifecycleError(f"租户 {op.tenant_id!r} 已存在，不能重复开通")
            new = TenantRecord(tenant_id=op.tenant_id, status=TenantStatus.ACTIVE)
        elif op.action is LifecycleAction.ROTATE:
            self._require(rec, effective, op, {TenantStatus.ACTIVE, TenantStatus.DRAINING})
            new = TenantRecord(
                tenant_id=rec.tenant_id,
                status=rec.status,
                secret_version=rec.secret_version + 1,
                drain_until=rec.drain_until,
            )
        elif op.action is LifecycleAction.DISABLE:
            self._require(rec, effective, op, {TenantStatus.ACTIVE})
            new = TenantRecord(tenant_id=rec.tenant_id, status=TenantStatus.DISABLED, secret_version=rec.secret_version)
        elif op.action is LifecycleAction.ENABLE:
            self._require(rec, effective, op, {TenantStatus.DISABLED})
            new = TenantRecord(tenant_id=rec.tenant_id, status=TenantStatus.ACTIVE, secret_version=rec.secret_version)
        elif op.action is LifecycleAction.DRAIN:
            self._require(rec, effective, op, {TenantStatus.ACTIVE})
            new = TenantRecord(
                tenant_id=rec.tenant_id,
                status=TenantStatus.DRAINING,
                secret_version=rec.secret_version,
                drain_until=now + (op.drain_seconds or 0.0),
            )
        else:  # pragma: no cover - 枚举穷尽
            raise LifecycleError(f"未支持的 action: {op.action}")
        return {**records, op.tenant_id: new}

    @staticmethod
    def _require(rec: TenantRecord | None, effective: TenantStatus | None, op: LifecycleOperation, allowed: set[TenantStatus]) -> None:
        if rec is None:
            raise LifecycleError(f"租户 {op.tenant_id!r} 不存在，无法执行 {op.action.value}")
        if effective not in allowed:
            raise LifecycleError(
                f"租户 {op.tenant_id!r} 当前为 {effective.value}，不允许 {op.action.value}"
            )
