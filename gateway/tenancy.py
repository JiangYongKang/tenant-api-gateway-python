"""租户身份解析与校验：完全本地可复现，不依赖外部服务。

凭据形态（确定性、本地可签发可校验）：
- v2（当前）: ``tnt2.<tenant_id>.<expires_at>.<hmac16>``
  hmac16 = HMAC-SHA256(secret, "tnt2.<tenant_id>.<expires_at>") 的前 16 位十六进制。
- v1（历史）: ``tnt1.<tenant_id>.<hmac16>``，无过期时间。
  该形态已过期：一律以 EXPIRED_FORMAT 拒绝，行为确定、原因可区分。
"""
from __future__ import annotations

import enum
import hashlib
import hmac
import time
from collections.abc import Callable
from dataclasses import dataclass

_V2_PREFIX = "tnt2"
_V1_PREFIX = "tnt1"


class CredentialStatus(enum.Enum):
    OK = "ok"
    MISSING = "missing"
    MALFORMED = "malformed"
    EXPIRED_FORMAT = "expired_format"
    EXPIRED = "expired"
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


def _signature(secret: str, payload: str) -> str:
    return hmac.new(secret.encode(), payload.encode(), hashlib.sha256).hexdigest()[:16]


def issue_credential(record: TenantRecord, expires_at: float) -> str:
    """本地签发 v2 凭据，仅供测试与内部调用方使用。"""
    payload = f"{_V2_PREFIX}.{record.tenant_id}.{int(expires_at)}"
    return f"{payload}.{_signature(record.secret, payload)}"


def issue_legacy_credential(record: TenantRecord) -> str:
    """本地签发 v1（历史形态）凭据，用于验证兼容性拒绝行为。"""
    payload = f"{_V1_PREFIX}.{record.tenant_id}"
    return f"{payload}.{_signature(record.secret, payload)}"


class TenantAuthenticator:
    """基于本地登记表的凭据校验器；时钟可注入，判定完全可复现。"""

    def __init__(
        self,
        tenants: dict[str, TenantRecord],
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._tenants = dict(tenants)
        self._clock = clock

    def authenticate(self, header: str | None) -> tuple[CredentialStatus, TenantIdentity | None]:
        if header is None or not header.strip():
            return CredentialStatus.MISSING, None
        token = header.strip()
        if token.lower().startswith("bearer "):
            token = token[7:].strip()

        parts = token.split(".")
        if not parts or parts[0] not in (_V1_PREFIX, _V2_PREFIX):
            return CredentialStatus.MALFORMED, None

        if parts[0] == _V1_PREFIX:
            # 历史形态：确定的拒绝行为，原因可区分
            return CredentialStatus.EXPIRED_FORMAT, None

        return self._authenticate_v2(token)

    def _authenticate_v2(self, token: str) -> tuple[CredentialStatus, TenantIdentity | None]:
        parts = token.split(".")
        if len(parts) != 4:
            return CredentialStatus.MALFORMED, None
        _, tenant_id, expires_raw, sig = parts
        if not tenant_id or not expires_raw or not sig:
            return CredentialStatus.MALFORMED, None
        try:
            expires_at = int(expires_raw)
        except ValueError:
            return CredentialStatus.MALFORMED, None

        record = self._tenants.get(tenant_id)
        if record is None:
            return CredentialStatus.UNKNOWN_TENANT, None

        payload = f"{_V2_PREFIX}.{tenant_id}.{expires_at}"
        expected = _signature(record.secret, payload)
        if not hmac.compare_digest(expected, sig):
            return CredentialStatus.INVALID_SIGNATURE, None
        if self._clock() >= expires_at:
            return CredentialStatus.EXPIRED, None
        if not record.enabled:
            return CredentialStatus.TENANT_DISABLED, None
        return CredentialStatus.OK, TenantIdentity(tenant_id=tenant_id, credential_version=2)
