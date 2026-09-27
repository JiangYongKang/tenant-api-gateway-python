"""回归：新增治理行为只作用于受治理请求；开放入口无凭据可访问，响应形态不变。"""
import logging

import main
from fastapi.testclient import TestClient

log = logging.getLogger("governance-tests")
prod = TestClient(main.app)  # 真实装配（main.py），含全部开放入口


def test_healthz_open_without_credential(harness):
    r = harness.client.get("/healthz")  # 历史客户端：无凭据
    log.info("healthz -> %d %s", r.status_code, r.json())
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


def test_admin_entries_open_without_credential():
    # 治理管理面本来就对外开放（豁免治理链路），无凭据可访问
    for path in ["/admin/config", "/admin/tenants", "/admin/policies", "/admin/audit", "/admin/usage/acme"]:
        r = prod.get(path)
        log.info("%s -> %d", path, r.status_code)
        assert r.status_code == 200, path


def test_open_entries_not_affected_by_new_governance():
    # 即使存在租户覆盖策略与生命周期变更，开放入口也不受治理
    assert prod.put("/admin/policies/globex", json={"rate_limit_per_minute": 1}).status_code == 200
    assert prod.post("/admin/tenants", json={"operations": [{"op": "suspend", "tenant_id": "globex"}]}).status_code == 200
    assert prod.get("/healthz").status_code == 200
    assert prod.get("/admin/config").status_code == 200
    # 收尾：恢复现场，避免影响同进程其他用例
    prod.post("/admin/tenants", json={"operations": [{"op": "resume", "tenant_id": "globex"}]})
    prod.delete("/admin/policies/globex")


def test_governed_entry_still_requires_credential(harness):
    # 受治理入口的拒绝形态不变：401 + error.code + request_id
    r = harness.client.get("/")
    log.info("no credential -> %d %s", r.status_code, r.json())
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "missing_credential"
    assert r.json()["error"]["request_id"]
    assert r.headers["x-request-id"] == r.json()["error"]["request_id"]


def test_root_response_shape_unchanged(harness):
    r = harness.client.get("/", headers=harness.headers("acme"))
    assert r.status_code == 200
    assert r.json() == {"Hello": "World"}  # 原有接口语义与响应形态不变


def test_reject_response_shape_unchanged_for_new_reasons(harness):
    # 新增的拒绝原因沿用既有响应形态：{"error": {"code", "request_id"}}
    harness.client.post("/admin/tenants", json={"operations": [{"op": "decommission", "tenant_id": "acme", "drain_seconds": 60}]})
    r = harness.client.get("/", headers=harness.headers("acme"))
    log.info("draining reject -> %d %s", r.status_code, r.json())
    assert set(r.json()["error"]) == {"code", "request_id"}
