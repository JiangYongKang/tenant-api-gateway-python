"""租户身份解析、校验与在线生命周期管理。

凭据形态（当前版本 v1）：
    X-Tenant-Credential: tnt_<tenant_id>.<sig>
    sig = HMAC-SHA256("<tenant_id>:<generation>", secret) 的前 16 个十六进制字符
    generation 为凭据世代，换发（rotate）时 +1，旧凭据立即失效（bad_signature）。

历史形态（v0）：``tenant-<id>`` —— 一律以 LEGACY_FORMAT 确定性拒绝。

生命周期状态机：
    ACTIVE --suspend--> SUSPENDED --resume--> ACTIVE
    ACTIVE/SUSPENDED --decommission(drain_seconds)--> DRAINING --(过渡期满)--> DISABLED
    provision 直接创建 ACTIVE；DISABLED 为终态，可被注册表回收以保持内存有界。

运行期变更（apply_batch）整体校验、原子生效：任一操作非法或互相冲突则整批拒绝，
注册表保持原样，绝不静默退回旧值。所有判定基于本地注册表与本地密钥，完全可复现。
"""
from __future__ import annotations

import enum
import hashlib
import hmac
import re
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass

CREDENTIAL_HEADER = "x-tenant-credential"

_V1_PATTERN = re.compile(r"^tnt_([a-z0-9][a-z0-9_-]{1,63})\.([0-9a-f]{16})$")
_LEGACY_PATTERN = re.compile(r"^tenant-(.+)$")
TENANT_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]{1,63}$")

MAX_DRAIN_SECONDS = 7 * 86_400.0


class TenantStatus(enum.Enum):
    ACTIVE = "active"
    SUSPENDED = "suspended"   # 临时停用，可 resume
    DRAINING = "draining"     # 平滑下线过渡态：新请求明确拒绝，在途请求正常收尾
    DISABLED = "disabled"     # 终态


@dataclass(frozen=True)
class Tenant:
    tenant_id: str
    status: TenantStatus


class CredentialRejectReason(enum.Enum):
    MISSING = "missing_credential"
    MALFORMED = "malformed_credential"
    LEGACY_FORMAT = "legacy_format"
    UNKNOWN_TENANT = "unknown_tenant"
    TENANT_DISABLED = "tenant_disabled"
    TENANT_SUSPENDED = "tenant_suspended"
    TENANT_DRAINING = "tenant_draining"
    BAD_SIGNATURE = "bad_signature"


class TenancyError(Exception):
    """租户校验失败，reason 可区分。"""

    def __init__(self, reason: CredentialRejectReason) -> None:
        super().__init__(reason.value)
        self.reason = reason


class TenantAdminError(ValueError):
    """租户在线变更非法或互相冲突：整批拒绝，注册表保持原样。"""

    def __init__(self, errors: list[str]) -> None:
        super().__init__("; ".join(errors))
        self.errors = list(errors)


@dataclass
class _TenantRecord:
    tenant_id: str
    status: TenantStatus
    generation: int = 0
    drain_until: float | None = None  # DRAINING 过渡截止时刻（registry 时钟）


_VALID_OPS = {"provision", "rotate_credential", "suspend", "resume", "decommission"}


class TenantRegistry:
    """本地租户注册表：确定性签发与校验、在线生命周期、原子批量变更、有界可回收。"""

    def __init__(
        self,
        secret: bytes,
        tenants: dict[str, TenantStatus] | None = None,
        *,
        clock: Callable[[], float] = time.time,
        max_tenants: int = 10_000,
    ) -> None:
        self._secret = secret
        self._clock = clock
        self._max_tenants = max_tenants
        self._lock = threading.Lock()
        self._records: dict[str, _TenantRecord] = {
            tid: _TenantRecord(tid, status) for tid, status in (tenants or {}).items()
        }

    # ---- 种子/测试辅助 ----
    def add(self, tenant_id: str, status: TenantStatus = TenantStatus.ACTIVE) -> None:
        with self._lock:
            self._records[tenant_id] = _TenantRecord(tenant_id, status)

    # ---- 凭据 ----
    def signature_for(self, tenant_id: str, generation: int | None = None) -> str:
        with self._lock:
            rec = self._records.get(tenant_id)
            if generation is not None:
                gen = generation
            else:
                gen = rec.generation if rec is not None else 0
        return self._signature(tenant_id, gen)

    def _signature(self, tenant_id: str, generation: int) -> str:
        # 纯函数：不加锁，供持锁路径（apply_batch/authenticate）内部使用
        msg = f"{tenant_id}:{generation}".encode()
        return hmac.new(self._secret, msg, hashlib.sha256).hexdigest()[:16]

    def issue(self, tenant_id: str) -> str:
        """为测试/本地使用确定性签发当前世代凭据。"""
        return f"tnt_{tenant_id}.{self.signature_for(tenant_id)}"

    # ---- 查询 ----
    def lookup(self, tenant_id: str) -> Tenant | None:
        with self._lock:
            rec = self._records.get(tenant_id)
            if rec is None:
                return None
            return Tenant(tenant_id=rec.tenant_id, status=self._effective_status(rec))

    def _effective_status(self, rec: _TenantRecord) -> TenantStatus:
        # DRAINING 过渡期满即真正停用（惰性迁移，判定只依赖时钟，确定可复现）
        if rec.status is TenantStatus.DRAINING and rec.drain_until is not None and self._clock() >= rec.drain_until:
            rec.status = TenantStatus.DISABLED
            rec.drain_until = None
        return rec.status

    def authenticate(self, tenant_id: str, sig: str) -> Tenant:
        """原子完成 存在性+签名+状态 判定，避免并发变更下结论自相矛盾。"""
        with self._lock:
            rec = self._records.get(tenant_id)
            if rec is None:
                raise TenancyError(CredentialRejectReason.UNKNOWN_TENANT)
            expected = self._signature(tenant_id, rec.generation)
            if not hmac.compare_digest(expected, sig):
                raise TenancyError(CredentialRejectReason.BAD_SIGNATURE)
            status = self._effective_status(rec)
            if status is TenantStatus.DISABLED:
                raise TenancyError(CredentialRejectReason.TENANT_DISABLED)
            if status is TenantStatus.SUSPENDED:
                raise TenancyError(CredentialRejectReason.TENANT_SUSPENDED)
            if status is TenantStatus.DRAINING:
                raise TenancyError(CredentialRejectReason.TENANT_DRAINING)
            return Tenant(tenant_id=tenant_id, status=status)

    # ---- 在线生命周期：原子批量变更 ----
    def apply_batch(self, operations: list[dict]) -> list[dict]:
        """整体校验后原子应用；任一操作非法/冲突则整批拒绝并抛 TenantAdminError。

        返回每个操作的结果（provision/rotate_credential 含新凭据，供下发）。
        """
        with self._lock:
            now = self._clock()
            errors: list[str] = []
            seen: set[str] = set()

            if not isinstance(operations, list) or not operations:
                raise TenantAdminError(["operations: 必须是非空列表"])

            for i, op in enumerate(operations):
                if not isinstance(op, dict):
                    errors.append(f"operations[{i}]: 必须是对象")
                    continue
                action = op.get("op")
                tenant_id = op.get("tenant_id")
                if action not in _VALID_OPS:
                    errors.append(f"operations[{i}]: 未知操作 {action!r}，允许 {sorted(_VALID_OPS)}")
                    continue
                if not isinstance(tenant_id, str) or not TENANT_ID_PATTERN.match(tenant_id):
                    errors.append(f"operations[{i}]: tenant_id 非法: {tenant_id!r}")
                    continue
                if tenant_id in seen:
                    errors.append(f"operations[{i}]: 同一租户 {tenant_id!r} 在一批中只能出现一次（互相冲突）")
                    continue
                seen.add(tenant_id)
                rec = self._records.get(tenant_id)
                status = self._effective_status(rec) if rec else None

                if action == "provision":
                    if rec is not None:
                        errors.append(f"operations[{i}]: 租户 {tenant_id!r} 已存在")
                elif action == "rotate_credential":
                    if rec is None:
                        errors.append(f"operations[{i}]: 租户 {tenant_id!r} 不存在")
                elif action == "suspend":
                    if status is not TenantStatus.ACTIVE:
                        errors.append(f"operations[{i}]: suspend 要求 ACTIVE，当前 {status.value if status else '不存在'}")
                elif action == "resume":
                    if status is not TenantStatus.SUSPENDED:
                        errors.append(f"operations[{i}]: resume 要求 SUSPENDED，当前 {status.value if status else '不存在'}")
                elif action == "decommission":
                    if status not in (TenantStatus.ACTIVE, TenantStatus.SUSPENDED):
                        errors.append(f"operations[{i}]: decommission 要求 ACTIVE/SUSPENDED，当前 {status.value if status else '不存在'}")
                    drain = op.get("drain_seconds", 60.0)
                    if isinstance(drain, bool) or not isinstance(drain, (int, float)) or not (0.0 <= float(drain) <= MAX_DRAIN_SECONDS):
                        errors.append(f"operations[{i}]: drain_seconds 越界 [0, {MAX_DRAIN_SECONDS}]: {drain!r}")

            if errors:
                raise TenantAdminError(errors)

            # 校验全部通过：原子应用（此前注册表未被触碰）
            results: list[dict] = []
            for op in operations:
                action, tenant_id = op["op"], op["tenant_id"]
                if action == "provision":
                    self._reclaim_if_full()
                    if len(self._records) >= self._max_tenants:
                        raise TenantAdminError([f"注册表已满（{self._max_tenants}）且无可回收租户"])
                    self._records[tenant_id] = _TenantRecord(tenant_id, TenantStatus.ACTIVE)
                    results.append({"op": action, "tenant_id": tenant_id, "status": TenantStatus.ACTIVE.value,
                                    "credential": f"tnt_{tenant_id}.{self._signature(tenant_id, 0)}"})
                elif action == "rotate_credential":
                    rec = self._records[tenant_id]
                    rec.generation += 1
                    results.append({"op": action, "tenant_id": tenant_id, "generation": rec.generation,
                                    "credential": f"tnt_{tenant_id}.{self._signature(tenant_id, rec.generation)}"})
                elif action == "suspend":
                    self._records[tenant_id].status = TenantStatus.SUSPENDED
                    results.append({"op": action, "tenant_id": tenant_id, "status": TenantStatus.SUSPENDED.value})
                elif action == "resume":
                    self._records[tenant_id].status = TenantStatus.ACTIVE
                    results.append({"op": action, "tenant_id": tenant_id, "status": TenantStatus.ACTIVE.value})
                elif action == "decommission":
                    rec = self._records[tenant_id]
                    drain = float(op.get("drain_seconds", 60.0))
                    rec.status = TenantStatus.DRAINING
                    rec.drain_until = now + drain
                    results.append({"op": action, "tenant_id": tenant_id, "status": TenantStatus.DRAINING.value,
                                    "drain_until": rec.drain_until})
            return results

    def _reclaim_if_full(self) -> None:
        """容量满时回收终态（DISABLED，含过渡期满的 DRAINING）租户，保持内存有界。"""
        if len(self._records) < self._max_tenants:
            return
        for tid in [t for t, r in self._records.items() if self._effective_status(r) is TenantStatus.DISABLED]:
            del self._records[tid]

    def size(self) -> int:
        with self._lock:
            return len(self._records)

    def tenant_ids(self) -> list[str]:
        with self._lock:
            return sorted(self._records)


class TenantResolver:
    """从请求头解析并校验租户身份。"""

    def __init__(self, registry: TenantRegistry) -> None:
        self._registry = registry

    def resolve(self, headers: dict[str, str]) -> Tenant:
        raw = headers.get(CREDENTIAL_HEADER)
        if raw is None or raw.strip() == "":
            raise TenancyError(CredentialRejectReason.MISSING)
        raw = raw.strip()

        if _LEGACY_PATTERN.match(raw):
            raise TenancyError(CredentialRejectReason.LEGACY_FORMAT)

        m = _V1_PATTERN.match(raw)
        if m is None:
            raise TenancyError(CredentialRejectReason.MALFORMED)

        tenant_id, sig = m.group(1), m.group(2)
        return self._registry.authenticate(tenant_id, sig)
