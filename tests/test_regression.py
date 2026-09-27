"""回归：新增治理行为只作用于受治理请求；开放入口与响应形态保持不变。"""
from __future__ import annotations

import logging

log = logging.getLogger("governance-tests")


def test_open_entries_reachable_without_credential(harness):
    """历史客户端无凭据仍可访问本来就对外开放的入口。"""
    assert harness.client.get("/healthz").status_code == 200
    assert harness.client.get("/admin/config").status_code == 200
    assert harness.client.get("/admin/tenants").status_code == 200
    assert harness.client.get("/admin/audit").status_code == 200
    assert harness.client.get("/admin/usage/acme").status_code == 200


def test_open_entries_not_governed(harness):
    """开放入口不经过治理链路：不落审计、不计量。"""
    harness.client.get("/healthz")
    harness.client.get("/admin/config")
    assert len(harness.audit_log) == 0
    assert harness.quota_tracker.usage("acme", harness.clock.now) == 0


def test_governed_entry_still_requires_credential(harness):
    resp = harness.client.get("/")
    assert resp.status_code == 401
    body = resp.json()
    log.info("missing credential -> %s", body)
    # 响应形态不变：{"error": {"code", "request_id"}}
    assert set(body) == {"error"}
    assert set(body["error"]) == {"code", "request_id"}
    assert body["error"]["code"] == "missing_credential"
    assert resp.headers["x-request-id"] == body["error"]["request_id"]


def test_legacy_credential_format_still_deterministically_rejected(harness):
    resp = harness.client.get("/", headers={"x-tenant-credential": "tenant-acme"})
    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "legacy_format"


def test_reject_response_shape_for_new_reasons(harness):
    """新增的过渡态结论也沿用统一响应形态。"""
    harness.apply_lifecycle({"action": "drain", "tenant_id": "acme", "drain_seconds": 60})
    resp = harness.client.get("/", headers=harness.headers("acme"))
    assert resp.status_code == 403
    body = resp.json()
    assert set(body) == {"error"}
    assert set(body["error"]) == {"code", "request_id"}
    assert body["error"]["code"] == "tenant_draining"


def test_rate_limit_response_shape_unchanged(harness):
    harness.config_store.update({"rate_limit_per_minute": 1})
    assert harness.client.get("/", headers=harness.headers("acme")).status_code == 200
    resp = harness.client.get("/", headers=harness.headers("acme"))
    assert resp.status_code == 429
    assert "retry-after" in resp.headers
    assert resp.json()["error"]["code"] == "rate_limited"


def test_successful_response_shape_unchanged(harness):
    resp = harness.client.get("/", headers=harness.headers("acme"))
    assert resp.status_code == 200
    assert resp.json() == {"Hello": "World"}
    assert "x-request-id" in resp.headers
