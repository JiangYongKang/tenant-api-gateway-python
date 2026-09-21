"""配置：非法明确拒绝、合法即时生效、进行中请求判定不自相矛盾。"""
import logging

import main
from fastapi.testclient import TestClient

from app.config import ConfigStore, GovernanceConfig

log = logging.getLogger("governance-tests")
client = TestClient(main.app)


def _cred(tenant: str = "acme") -> dict[str, str]:
    return {"x-tenant-credential": main.registry.issue(tenant)}


def test_invalid_configs_rejected_with_422_and_old_config_kept():
    before = client.get("/admin/config").json()
    bad_payloads = [
        {"rate_limit_per_minute": 0},
        {"rate_limit_per_minute": "fast"},
        {"rate_limit_per_minute": True},
        {"quota_per_day": -5},
        {"idempotency_ttl_seconds": 0},
        {"state_failure_mode": "ignore"},
        {"unknown_option": 1},
    ]
    for payload in bad_payloads:
        r = client.post("/admin/config", json=payload)
        log.info("invalid config %s -> %d %s", payload, r.status_code, r.json())
        assert r.status_code == 422
        assert r.json()["error"]["code"] == "invalid_config"
    after = client.get("/admin/config").json()
    assert before == after  # 绝不静默回退或部分应用


def test_valid_config_applies_immediately():
    r = client.post("/admin/config", json={"rate_limit_per_minute": 2})
    assert r.status_code == 200 and r.json()["applied"] is True
    # 即时生效：第 3 个请求即被限流
    codes = [client.get("/", headers=_cred()).status_code for _ in range(3)]
    log.info("after config change, codes=%s", codes)
    assert codes == [200, 200, 429]
    assert client.get("/", headers=_cred()).json()["error"]["code"] == "rate_limited"
    # 恢复默认，避免影响其他测试
    client.post("/admin/config", json={"rate_limit_per_minute": 60})


def test_partial_update_keeps_other_fields():
    before = client.get("/admin/config").json()
    client.post("/admin/config", json={"quota_per_day": 123})
    after = client.get("/admin/config").json()
    log.info("partial update before=%s after=%s", before, after)
    assert after["quota_per_day"] == 123
    assert after["rate_limit_per_minute"] == before["rate_limit_per_minute"]
    client.post("/admin/config", json={"quota_per_day": 10000})


def test_snapshot_consistency_for_inflight_request():
    # 单请求持有同一快照：配置更新不会改写已取出的快照
    store = ConfigStore()
    snap = store.get()
    store.update({"rate_limit_per_minute": 1, "quota_per_day": 1})
    assert snap == GovernanceConfig()  # 旧快照不变
    assert store.get().rate_limit_per_minute == 1
    log.info("snapshot=%s current=%s", snap, store.get())
