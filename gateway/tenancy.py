"""租户身份解析与校验：完全本地可复现，不依赖外部服务。"""
from __future__ import annotations

import enum
from dataclasses import dataclass


class CredentialStatus(enum.Enum):
    OK = "ok"
    MISSING = "missing"
    MALFORMED = "malformed"
    EXPIRED_FORMAT = "expired_format"
    INVALID_SIGNATURE = "invalid_signature"
    UNKNOWN_TENANT = "unknown_tenant"
    TENANT_DISABLED = "tenant_disabled"


@dataclass(frozen=True)
class TenantIdentity:
    tenant_id: str
    credential_version: int


@dataclass(frozen=True)
class TenantRecord:
    """本地租户登记信息（测试可复现的凭据来源）。"""

    tenant_id: str
    secret: str
    enabled: bool = True


class TenantAuthenticator:
    """基于本地登记表的凭据校验器。"""

    def __init__(self, tenants: dict[str, TenantRecord]) -> None:
        self._tenants = dict(tenants)

    def authenticate(self, header: str | None) -> tuple[CredentialStatus, TenantIdentity | None]:
        raise NotImplementedError
