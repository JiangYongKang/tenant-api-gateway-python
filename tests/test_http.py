"""HTTP 层：既有接口不变、多入口结论一致、审计可检索且不含凭据。"""
import logging
import time

import pytest
from fastapi.testclient import TestClient

from gateway.tenancy import issue_credential, issue_legacy_credential
from main import DEFAULT_TENANTS, create_app

logger = logging.getLogger("tests.http")


@pytest.fixture
def client():
    return TestClient(create_app())


@pytest.fixture
def cred():
    return issue_credential(DEFAULT_TENANTS["tenant-alpha"], time.time() + 3600)


def test_existing_root_endpoint_unchanged(client):
    r = client.get("/")
    assert r.status_code == 200 and r.json() == {"Hello": "World"}


def test_credential_entries_give_same_conclusion(client, cred):
    """同一凭据从不同入口（X-Tenant-Credential / Authorization）进入，结论一致。"""
    r1 = client.get("/api/resource", headers={"x-tenant-credential": cred})
    r2 = client.get("/api/resource", headers={"authorization": cred})
    r3 = client.get("/api/resource", headers={"authorization": f"Bearer {cred}"})
    logger.info("entries: x-tenant-credential=%s authorization=%s bearer=%s",
                r1.status_code, r2.status_code, r3.status_code)
    assert r1.status_code == r2.status_code == r3.status_code == 200


def test_legacy_credential_rejected_consistently_across_entries(client):
    legacy = issue_legacy_credential(DEFAULT_TENANTS["tenant-alpha"])
    r1 = client.get("/api/resource", headers={"x-tenant-credential": legacy})
    r2 = client.get("/api/resource", headers={"authorization": legacy})
    assert r1.status_code == r2.status_code == 401
    assert r1.json()["error"]["reason"] == r2.json()["error"]["reason"] == "credential_expired_format"


def test_idempotent_replay_over_http(client, cred):
    h = {"x-tenant-credential": cred, "x-idempotency-key": "order-9"}
    r1 = client.post("/api/resource", headers=h, json={"name": "x"})
    r2 = client.post("/api/resource", headers=h, json={"name": "x"})
    assert r1.status_code == 201 and r2.status_code == 201
    assert r2.headers.get("x-idempotent-replay") == "true"
    assert r1.json() == r2.json()
    items = client.get("/api/resource", headers={"x-tenant-credential": cred}).json()["items"]
    assert len(items) == 1  # 只产生一次副作用


def test_tenant_data_isolation_over_http(client, cred):
    client.post("/api/resource",
                headers={"x-tenant-credential": cred, "x-idempotency-key": "a1"},
                json={"name": "alpha-item"})
    beta = issue_credential(DEFAULT_TENANTS["tenant-beta"], time.time() + 3600)
    r = client.get("/api/resource", headers={"x-tenant-credential": beta})
    assert r.json() == {"items": []}


def test_audit_retrievable_and_contains_no_credential(client, cred):
    r = client.get("/api/resource", headers={"x-tenant-credential": "garbage"})
    assert r.status_code == 401
    rid = r.json()["error"]["request_id"]
    event = client.get(f"/governance/audit/{rid}").json()
    logger.info("audit event: %s", event)
    assert event["outcome"] == "rejected" and event["reason"] == "credential_malformed"
    assert cred not in str(event) and "garbage" not in str(event)  # 不含凭据原文


def test_config_endpoint_rejects_invalid_and_applies_valid(client, cred):
    r = client.post("/governance/config", json={"rate_burst": -3})
    assert r.status_code == 422 and r.json()["error"]["reason"] == "invalid_config"
    assert client.get("/governance/config").json()["rate_burst"] == 20  # 未被污染
    r = client.post("/governance/config", json={"rate_burst": 1, "rate_limit_per_second": 0.0001})
    assert r.status_code == 200
    h = {"x-tenant-credential": cred}
    assert client.get("/api/resource", headers=h).status_code == 200
    r = client.get("/api/resource", headers=h)
    assert r.status_code == 429 and "retry-after" in r.headers


def test_rejection_carries_request_id_header(client):
    r = client.get("/api/resource")
    assert r.headers.get("x-request-id") == r.json()["error"]["request_id"]
