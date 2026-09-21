"""租户身份解析与校验。

凭据形态（当前版本 v1）：
    X-Tenant-Credential: tnt_<tenant_id>.<sig>
    sig = HMAC-SHA256(tenant_id, secret) 的前 16 个十六进制字符

历史形态（v0）：``tenant-<id>`` —— 一律以 LEGACY_FORMAT 确定性拒绝。
所有判定均基于本地注册表与本地密钥，完全可复现，不依赖外部服务。
"""
from __future__ import annotations

import enum
import hashlib
import hmac
import re
from dataclasses import dataclass

CREDENTIAL_HEADER = "x-tenant-credential"

_V1_PATTERN = re.compile(r"^tnt_([a-z0-9][a-z0-9_-]{1,63})\.([0-9a-f]{16})$")
_LEGACY_PATTERN = re.compile(r"^tenant-(.+)$")


class TenantStatus(enum.Enum):
    ACTIVE = "active"
    DISABLED = "disabled"


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
    BAD_SIGNATURE = "bad_signature"


class TenancyError(Exception):
    """租户校验失败，reason 可区分。"""

    def __init__(self, reason: CredentialRejectReason) -> None:
        super().__init__(reason.value)
        self.reason = reason


class TenantRegistry:
    """本地租户注册表：确定性签发与校验。"""

    def __init__(self, secret: bytes, tenants: dict[str, TenantStatus] | None = None) -> None:
        self._secret = secret
        self._tenants: dict[str, TenantStatus] = dict(tenants or {})

    def add(self, tenant_id: str, status: TenantStatus = TenantStatus.ACTIVE) -> None:
        self._tenants[tenant_id] = status

    def signature_for(self, tenant_id: str) -> str:
        return hmac.new(self._secret, tenant_id.encode(), hashlib.sha256).hexdigest()[:16]

    def issue(self, tenant_id: str) -> str:
        """为测试/本地使用确定性签发凭据。"""
        return f"tnt_{tenant_id}.{self.signature_for(tenant_id)}"

    def lookup(self, tenant_id: str) -> Tenant | None:
        status = self._tenants.get(tenant_id)
        if status is None:
            return None
        return Tenant(tenant_id=tenant_id, status=status)

    def verify(self, tenant_id: str, sig: str) -> bool:
        return hmac.compare_digest(self.signature_for(tenant_id), sig)


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
        tenant = self._registry.lookup(tenant_id)
        if tenant is None:
            raise TenancyError(CredentialRejectReason.UNKNOWN_TENANT)
        if not self._registry.verify(tenant_id, sig):
            raise TenancyError(CredentialRejectReason.BAD_SIGNATURE)
        if tenant.status is TenantStatus.DISABLED:
            raise TenancyError(CredentialRejectReason.TENANT_DISABLED)
        return tenant
