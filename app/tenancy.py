"""租户身份解析、校验与在线生命周期状态。

凭据形态（当前版本 v1）：
    X-Tenant-Credential: tnt_<tenant_id>.<sig>
    sig = HMAC-SHA256(tenant_id, secret(tenant, version)) 的前 16 个十六进制字符

- 每个租户的密钥由 (主密钥, tenant_id, secret_version) 确定性派生；
  换发凭据 = secret_version + 1，旧凭据立即失效，全程本地可复现。
- 生命周期状态：ACTIVE -> DRAINING -> DISABLED；DRAINING 到期自动视为 DISABLED。
  DRAINING 与"凭据无效""已停用"是可区分的第三种明确结论。
- 历史形态（v0）：``tenant-<id>`` —— 一律以 LEGACY_FORMAT 确定性拒绝。
- 注册表提供事务接口：批量调整在锁内先校验后整体替换，要么全成功要么完全不变。
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


class TenantStatus(enum.Enum):
    ACTIVE = "active"
    DRAINING = "draining"      # 平滑下线过渡态：新请求被拒（tenant_draining），在处理请求正常收尾
    DISABLED = "disabled"


@dataclass(frozen=True)
class TenantRecord:
    """注册表内的租户条目（含密钥代数与过渡态截止时刻）。"""

    tenant_id: str
    status: TenantStatus
    secret_version: int = 0
    drain_until: float | None = None


@dataclass(frozen=True)
class Tenant:
    tenant_id: str
    status: TenantStatus  # 生效状态（DRAINING 到期后解析为 DISABLED）


class CredentialRejectReason(enum.Enum):
    MISSING = "missing_credential"
    MALFORMED = "malformed_credential"
    LEGACY_FORMAT = "legacy_format"
    UNKNOWN_TENANT = "unknown_tenant"
    TENANT_DRAINING = "tenant_draining"
    TENANT_DISABLED = "tenant_disabled"
    BAD_SIGNATURE = "bad_signature"


class TenancyError(Exception):
    """租户校验失败，reason 可区分。"""

    def __init__(self, reason: CredentialRejectReason) -> None:
        super().__init__(reason.value)
        self.reason = reason


class TenantRegistry:
    """本地租户注册表：确定性签发与校验，线程安全，支持原子批量调整。"""

    def __init__(
        self,
        secret: bytes,
        tenants: dict[str, TenantStatus] | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._master = secret
        self._clock = clock
        self._lock = threading.Lock()
        self._records: dict[str, TenantRecord] = {
            tid: TenantRecord(tenant_id=tid, status=status) for tid, status in (tenants or {}).items()
        }

    # ---- 基础操作 ----
    def add(self, tenant_id: str, status: TenantStatus = TenantStatus.ACTIVE) -> None:
        with self._lock:
            self._records[tenant_id] = TenantRecord(tenant_id=tenant_id, status=status)

    def _secret_for(self, tenant_id: str, version: int) -> bytes:
        material = f"{tenant_id}:{version}".encode()
        return hmac.new(self._master, material, hashlib.sha256).digest()

    def signature_for(self, tenant_id: str) -> str:
        with self._lock:
            rec = self._records.get(tenant_id)
            version = rec.secret_version if rec else 0
        secret = self._secret_for(tenant_id, version)
        return hmac.new(secret, tenant_id.encode(), hashlib.sha256).hexdigest()[:16]

    def issue(self, tenant_id: str) -> str:
        """为测试/本地使用确定性签发当前代凭据。"""
        return f"tnt_{tenant_id}.{self.signature_for(tenant_id)}"

    @staticmethod
    def effective_status(rec: TenantRecord, now: float) -> TenantStatus:
        """DRAINING 到期即视为 DISABLED（惰性求值，无需后台任务）。"""
        if rec.status is TenantStatus.DRAINING and rec.drain_until is not None and now >= rec.drain_until:
            return TenantStatus.DISABLED
        return rec.status

    def lookup(self, tenant_id: str, now: float | None = None) -> Tenant | None:
        with self._lock:
            rec = self._records.get(tenant_id)
        if rec is None:
            return None
        return Tenant(tenant_id=tenant_id, status=self.effective_status(rec, now if now is not None else self._clock()))

    def verify(self, tenant_id: str, sig: str) -> bool:
        return hmac.compare_digest(self.signature_for(tenant_id), sig)

    # ---- 在线生命周期：事务接口 ----
    def snapshot_records(self) -> dict[str, TenantRecord]:
        with self._lock:
            return dict(self._records)

    def transact(self, fn):
        """fn(当前记录副本) -> 新记录字典；锁内执行，抛错则完全不变，否则整体替换。"""
        with self._lock:
            new_records = fn(dict(self._records))
            self._records = new_records

    def tenant_count(self) -> int:
        with self._lock:
            return len(self._records)


class TenantResolver:
    """从请求头解析并校验租户身份。"""

    def __init__(self, registry: TenantRegistry, clock: Callable[[], float] | None = None) -> None:
        self._registry = registry
        self._clock = clock

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
        now = self._clock() if self._clock is not None else None
        tenant = self._registry.lookup(tenant_id, now=now)
        if tenant is None:
            raise TenancyError(CredentialRejectReason.UNKNOWN_TENANT)
        if not self._registry.verify(tenant_id, sig):
            raise TenancyError(CredentialRejectReason.BAD_SIGNATURE)
        if tenant.status is TenantStatus.DRAINING:
            raise TenancyError(CredentialRejectReason.TENANT_DRAINING)
        if tenant.status is TenantStatus.DISABLED:
            raise TenancyError(CredentialRejectReason.TENANT_DISABLED)
        return tenant
