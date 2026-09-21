"""租户身份：拒绝原因必须可区分，判定完全本地可复现。"""
import logging

import pytest

from gateway.errors import RejectReason
from gateway.tenancy import (
    CredentialStatus,
    TenantAuthenticator,
    issue_credential,
    issue_legacy_credential,
)
from tests.conftest import log_decision

logger = logging.getLogger("tests.tenancy")


def _inspect(pipeline, credential, clock):
    d = pipeline.inspect(
        method="GET", path="/api/resource", credential_header=credential,
        idempotency_key=None, body=b"", now=clock(),
    )
    log_decision("tenancy", d, credential=credential)
    return d


def test_missing_credential_rejected(pipeline, clock):
    d = _inspect(pipeline, None, clock)
    assert d.reason == RejectReason.CREDENTIAL_MISSING and d.status_code == 401


def test_malformed_credential_rejected(pipeline, clock):
    d = _inspect(pipeline, "not-a-credential", clock)
    assert d.reason == RejectReason.CREDENTIAL_MALFORMED and d.status_code == 401


def test_legacy_format_rejected_with_distinct_reason(pipeline, tenants, clock, config, audit):
    auth = TenantAuthenticator(tenants, clock=clock)
    pipeline._auth = auth
    legacy = issue_legacy_credential(tenants["alpha"])
    d = _inspect(pipeline, legacy, clock)
    assert d.reason == RejectReason.CREDENTIAL_EXPIRED_FORMAT and d.status_code == 401


def test_invalid_signature_rejected(pipeline, credential, clock):
    bad = credential()[:-1] + ("0" if credential()[-1] != "0" else "1")
    d = _inspect(pipeline, bad, clock)
    assert d.reason == RejectReason.CREDENTIAL_INVALID and d.status_code == 401


def test_unknown_tenant_rejected(pipeline, clock):
    from gateway.tenancy import TenantRecord
    d = _inspect(pipeline, issue_credential(TenantRecord("ghost", "x"), clock() + 100), clock)
    assert d.reason == RejectReason.CREDENTIAL_INVALID and d.status_code == 401


def test_disabled_tenant_rejected(pipeline, credential, clock):
    d = _inspect(pipeline, credential("disabled"), clock)
    assert d.reason == RejectReason.TENANT_DISABLED and d.status_code == 403


def test_expired_credential_rejected_deterministically(pipeline, credential, clock):
    cred = credential(ttl=10)
    assert _inspect(pipeline, cred, clock).allowed
    clock.advance(11)
    d = _inspect(pipeline, cred, clock)
    assert d.reason == RejectReason.CREDENTIAL_INVALID and d.status_code == 401


def test_tenant_isolation(pipeline, credential, clock):
    d1 = _inspect(pipeline, credential("alpha"), clock)
    d2 = _inspect(pipeline, credential("beta"), clock)
    assert d1.allowed and d2.allowed
    assert d1.tenant_id == "alpha" and d2.tenant_id == "beta"
    logger.info("isolation: alpha=%s beta=%s", d1.tenant_id, d2.tenant_id)


def test_bearer_prefix_accepted(pipeline, credential, clock):
    d = _inspect(pipeline, "Bearer " + credential(), clock)
    assert d.allowed
