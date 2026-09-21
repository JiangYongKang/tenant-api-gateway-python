"""治理判定结果与可区分的拒绝原因定义。"""
from __future__ import annotations

import enum


class RejectReason(enum.Enum):
    """请求被拒绝的可区分原因。"""

    CREDENTIAL_MISSING = "credential_missing"
    CREDENTIAL_MALFORMED = "credential_malformed"
    CREDENTIAL_EXPIRED_FORMAT = "credential_expired_format"
    CREDENTIAL_INVALID = "credential_invalid"
    TENANT_DISABLED = "tenant_disabled"
    RATE_LIMITED = "rate_limited"
    QUOTA_EXCEEDED = "quota_exceeded"
    IDEMPOTENCY_CONFLICT = "idempotency_conflict"
    DEGRADED = "degraded"


class GovernanceError(Exception):
    """治理层内部错误基类（不直接透出给调用方）。"""


class StateStoreError(GovernanceError):
    """运行状态存储不可读或响应变慢。"""


class ConfigError(GovernanceError):
    """配置非法或不兼容，必须被明确拒绝。"""
