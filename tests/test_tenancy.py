"""租户身份：各类拒绝原因必须可区分，且判定完全本地可复现。"""
import logging

from app.audit import AuditOutcome
from app.tenancy import CredentialRejectReason, TenancyError, TenantResolver

log = logging.getLogger("governance-tests")


def _expect_reject(harness, headers, want_code: str, want_status: int = 401):
    r = harness.client.get("/", headers=headers)
    log.info("input headers=%s -> status=%d code=%s", headers, r.status_code, r.json()["error"]["code"])
    assert r.status_code == want_status
    assert r.json()["error"]["code"] == want_code
    return r


def test_missing_credential(harness):
    _expect_reject(harness, {}, CredentialRejectReason.MISSING.value)


def test_legacy_format_deterministically_rejected(harness):
    # 历史客户端凭据形态 tenant-<id>：确定的拒绝行为
    _expect_reject(harness, {"x-tenant-credential": "tenant-acme"}, CredentialRejectReason.LEGACY_FORMAT.value)


def test_malformed_credential(harness):
    for bad in ["garbage", "tnt_", "tnt_acme", "tnt_acme.xyz", "tnt_ACME.0123456789abcdef", "tnt_acme.0123"]:
        _expect_reject(harness, {"x-tenant-credential": bad}, CredentialRejectReason.MALFORMED.value)


def test_unknown_tenant(harness):
    _expect_reject(harness, {"x-tenant-credential": harness.cred("ghost")}, CredentialRejectReason.UNKNOWN_TENANT.value)


def test_bad_signature(harness):
    _expect_reject(harness, {"x-tenant-credential": "tnt_acme.0000000000000000"}, CredentialRejectReason.BAD_SIGNATURE.value)


def test_disabled_tenant(harness):
    _expect_reject(harness, {"x-tenant-credential": harness.cred("oldco")}, CredentialRejectReason.TENANT_DISABLED.value)


def test_valid_credential_accepted_and_audited(harness):
    r = harness.client.get("/", headers=harness.headers("acme"))
    log.info("valid cred -> %d %s", r.status_code, r.json())
    assert r.status_code == 200
    assert r.json() == {"Hello": "World"}  # 原有接口语义不变
    assert r.headers["x-request-id"]

    records = harness.audit_log.query(tenant_id="acme", outcome=AuditOutcome.PROCESSED)
    assert len(records) == 1
    assert records[0].request_id == r.headers["x-request-id"]


def test_tenant_isolation(harness):
    # 不同租户互不可见互不影响：各自请求成功，审计按租户隔离
    assert harness.client.get("/", headers=harness.headers("acme")).status_code == 200
    assert harness.client.get("/", headers=harness.headers("globex")).status_code == 200
    assert len(harness.audit_log.query(tenant_id="acme")) == 1
    assert len(harness.audit_log.query(tenant_id="globex")) == 1


def test_resolver_is_purely_local(harness):
    # 不经过 HTTP，直接本地解析：同样的输入同样的结论（可复现）
    resolver = TenantResolver(harness.registry)
    cred = harness.cred("acme")
    t1 = resolver.resolve({"x-tenant-credential": cred})
    t2 = resolver.resolve({"x-tenant-credential": cred})
    assert t1 == t2
    try:
        resolver.resolve({"x-tenant-credential": "tenant-acme"})
        raise AssertionError("legacy must be rejected")
    except TenancyError as e:
        assert e.reason is CredentialRejectReason.LEGACY_FORMAT
