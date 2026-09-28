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
注册表保持原样，绝不静默退回旧值。所有校验（含容量上限）都在暂存副本上预演，
只有整批全部成立时才一次性原子提交；被拒批次中的换发不会提前生效、终态租户不会
被提前回收。并发批次按快照隔离判定冲突（首提交者胜），冲突方整批拒绝、零改动。
所有判定基于本地注册表与本地密钥，完全可复现。
"""
from __future__ import annotations

import enum
import hashlib
import hmac
import re
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, replace

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


class TenantBatchConflictError(TenantAdminError):
    """并发批次互相冲突（快照已被先提交批次改写）：整批拒绝，注册表保持原样。

    首提交者胜：只有一个批次整体生效，其余批次零改动、不留痕迹，调用方可整批重试。
    """


@dataclass
class _TenantRecord:
    tenant_id: str
    status: TenantStatus
    generation: int = 0
    drain_until: float | None = None  # DRAINING 过渡截止时刻（registry 时钟）
    version: int = 0  # 记录版本：任何对该租户的成功变更 +1，供并发批次做乐观冲突检测


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
        self._batch_gate: Callable[[], None] | None = None  # 测试注入：进入临界区前同步
        self._records: dict[str, _TenantRecord] = {
            tid: _TenantRecord(tid, status) for tid, status in (tenants or {}).items()
        }

    # ---- 种子/测试辅助 ----
    def add(self, tenant_id: str, status: TenantStatus = TenantStatus.ACTIVE) -> None:
        with self._lock:
            self._records[tenant_id] = _TenantRecord(tenant_id, status)

    def set_batch_gate(self, gate: Callable[[], None] | None) -> None:
        """注入批次临界区前的同步钩子（如 barrier.wait），用于稳定复现并发冲突。"""
        self._batch_gate = gate

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

        语义：整批要么全部成功、要么完全不变。所有判定（含容量上限、终态回收空间）
        都先在暂存副本上预演，原注册表在提交前不被触碰；被拒批次中的换发不生效、
        旧凭据继续可用，终态租户也不会被提前回收。并发批次按快照隔离检测冲突
        （首提交者胜），冲突方抛 TenantBatchConflictError 且零改动。

        返回每个操作的结果（provision/rotate_credential 含新凭据，供下发）。
        """
        # ---- 阶段 1：锁外静态结构校验（不依赖注册表内容，可先收敛全部结构错误）----
        static_errors, parsed = self._parse_operations(operations)

        # ---- 阶段 2：锁外读取本批租户的版本基线（快照，标准 OCC）----
        records = self._records  # 提交时整体替换引用；锁外取当前引用读快照即可
        baseline = {op["tenant_id"]: records[op["tenant_id"]].version
                    for op in parsed if op["tenant_id"] in records}

        # 快照已取：注入同步点，保证并发批次都基于同一旧快照进入提交（测试复现用）
        if self._batch_gate is not None:
            self._batch_gate()

        # ---- 阶段 3：锁内冲突检测 + 暂存预演 + 原子提交 ----
        with self._lock:
            now = self._clock()

            # 结构非法优先确定性报告（无论是否撞上并发，结论都一致）
            if static_errors:
                raise TenantAdminError(list(static_errors))

            # 快照隔离（首提交者胜）：基线里存在的租户版本变了/被回收，或基线里
            # 不存在的租户已被别的批次先开通，都判冲突，整批拒绝且零改动
            target_ids = {op["tenant_id"] for op in parsed}
            conflicts = [
                tid for tid in target_ids
                if (tid in baseline and (
                        tid not in self._records or self._records[tid].version != baseline[tid]))
                or (tid not in baseline and tid in self._records)
            ]
            if conflicts:
                raise TenantBatchConflictError([
                    f"并发批次冲突：租户 {tid!r} 已被另一批次整体修改，本批整批拒绝，可整批重试"
                    for tid in sorted(conflicts)
                ])

            # 暂存副本：所有状态机判定、容量判定与变更都只作用于副本
            staging = {tid: replace(rec) for tid, rec in self._records.items()}
            results: list[dict] = []
            errors: list[str] = []
            for op in parsed:
                action, tenant_id = op["op"], op["tenant_id"]
                rec = staging.get(tenant_id)
                status = self._effective_status(rec) if rec else None

                if action == "provision":
                    if rec is not None:
                        errors.append(f"operations[{op['index']}]: 租户 {tenant_id!r} 已存在")
                        continue
                    self._reclaim_if_full(staging)
                    if len(staging) >= self._max_tenants:
                        # 容量判定也在提交前：此前本批的换发等变更尚未落到原表
                        errors.append(
                            f"operations[{op['index']}]: 注册表容量上限 {self._max_tenants} "
                            f"已满且无可回收租户，整批拒绝")
                        continue
                    new_rec = _TenantRecord(tenant_id, TenantStatus.ACTIVE)
                    staging[tenant_id] = new_rec
                    results.append({"op": action, "tenant_id": tenant_id,
                                    "status": TenantStatus.ACTIVE.value,
                                    "credential": f"tnt_{tenant_id}.{self._signature(tenant_id, 0)}"})
                elif action == "rotate_credential":
                    if rec is None:
                        errors.append(f"operations[{op['index']}]: 租户 {tenant_id!r} 不存在")
                        continue
                    rec.generation += 1
                    rec.version += 1
                    results.append({"op": action, "tenant_id": tenant_id, "generation": rec.generation,
                                    "credential": f"tnt_{tenant_id}.{self._signature(tenant_id, rec.generation)}"})
                elif action == "suspend":
                    if status is not TenantStatus.ACTIVE:
                        errors.append(f"operations[{op['index']}]: suspend 要求 ACTIVE，"
                                      f"当前 {status.value if status else '不存在'}")
                        continue
                    rec.status = TenantStatus.SUSPENDED
                    rec.version += 1
                    results.append({"op": action, "tenant_id": tenant_id,
                                    "status": TenantStatus.SUSPENDED.value})
                elif action == "resume":
                    if status is not TenantStatus.SUSPENDED:
                        errors.append(f"operations[{op['index']}]: resume 要求 SUSPENDED，"
                                      f"当前 {status.value if status else '不存在'}")
                        continue
                    rec.status = TenantStatus.ACTIVE
                    rec.version += 1
                    results.append({"op": action, "tenant_id": tenant_id,
                                    "status": TenantStatus.ACTIVE.value})
                elif action == "decommission":
                    if status not in (TenantStatus.ACTIVE, TenantStatus.SUSPENDED):
                        errors.append(f"operations[{op['index']}]: decommission 要求 ACTIVE/SUSPENDED，"
                                      f"当前 {status.value if status else '不存在'}")
                        continue
                    if op["drain_error"] is not None:
                        errors.append(op["drain_error"])
                        continue
                    rec.status = TenantStatus.DRAINING
                    rec.drain_until = now + op["drain_seconds"]
                    rec.version += 1
                    results.append({"op": action, "tenant_id": tenant_id,
                                    "status": TenantStatus.DRAINING.value,
                                    "drain_until": rec.drain_until})

            if errors:
                # 暂存副本连同其中的换发/回收一起丢弃；原表自始至终未被修改
                raise TenantAdminError(errors)

            # 全部成立：一次性原子提交（锁内替换引用）
            self._records = staging
            return results

    @staticmethod
    def _parse_operations(operations: list[dict]) -> tuple[list[str], list[dict]]:
        """静态结构校验：只看请求形态，不看注册表；返回 (错误列表, 归一化操作列表)。"""
        errors: list[str] = []
        parsed: list[dict] = []
        if not isinstance(operations, list) or not operations:
            return ["operations: 必须是非空列表"], []

        seen: set[str] = set()
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

            norm = {"index": i, "op": action, "tenant_id": tenant_id,
                    "drain_seconds": 60.0, "drain_error": None}
            if action == "decommission":
                drain = op.get("drain_seconds", 60.0)
                if isinstance(drain, bool) or not isinstance(drain, (int, float)) \
                        or not (0.0 <= float(drain) <= MAX_DRAIN_SECONDS):
                    norm["drain_error"] = (
                        f"operations[{i}]: drain_seconds 越界 [0, {MAX_DRAIN_SECONDS}]: {drain!r}")
                else:
                    norm["drain_seconds"] = float(drain)
            parsed.append(norm)
        return errors, parsed

    def _reclaim_if_full(self, records: dict[str, "_TenantRecord"]) -> None:
        """容量满时在给定（暂存）表上回收终态（DISABLED，含过渡期满的 DRAINING）租户。"""
        if len(records) < self._max_tenants:
            return
        for tid in [t for t, r in records.items() if self._effective_status(r) is TenantStatus.DISABLED]:
            del records[tid]

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
