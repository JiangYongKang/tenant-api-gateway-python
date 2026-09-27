"""差异化策略：覆盖优先级唯一、非法当场拒绝并保留原策略、即时生效且只影响该租户。"""
import logging

import pytest

log = logging.getLogger("governance-tests")


def _set(harness, tenant_id, payload):
    r = harness.client.put(f"/admin/policies/{tenant_id}", json=payload)
    log.info("set policy %s %s -> %d %s", tenant_id, payload, r.status_code, r.json())
    return r


def test_override_applies_immediately_and_only_to_that_tenant(harness):
    assert _set(harness, "acme", {"rate_limit_per_minute": 2}).status_code == 200
    # acme：第 3 个请求即被限流
    codes = [harness.client.get("/", headers=harness.headers("acme")).status_code for _ in range(3)]
    log.info("acme codes=%s", codes)
    assert codes == [200, 200, 429]
    # globex：共用默认 60/分，完全不受影响
    codes = [harness.client.get("/", headers=harness.headers("globex")).status_code for _ in range(5)]
    log.info("globex codes=%s", codes)
    assert codes == [200] * 5


def test_override_precedence_over_global_and_field_scoped(harness):
    # acme 只覆盖限流；配额字段仍跟随全局默认
    assert _set(harness, "acme", {"rate_limit_per_minute": 100}).status_code == 200
    harness.config_store.update({"rate_limit_per_minute": 1, "quota_per_day": 2})
    # 限流：acme 用自己的 100/分（覆盖优先于全局 1/分）——第 2 个请求不会被 rate_limited
    r1 = harness.client.get("/", headers=harness.headers("acme"))
    r2 = harness.client.get("/", headers=harness.headers("acme"))
    assert (r1.status_code, r2.status_code) == (200, 200)
    # acme 未覆盖配额：跟随全局 2/天，第 3 个请求是 quota_exceeded 而非 rate_limited
    r3 = harness.client.get("/", headers=harness.headers("acme"))
    log.info("acme 3rd -> %d %s", r3.status_code, r3.json())
    assert r3.status_code == 429 and r3.json()["error"]["code"] == "quota_exceeded"
    # globex 无覆盖：第 2 个请求即被全局 1/分限流
    assert harness.client.get("/", headers=harness.headers("globex")).status_code == 200
    r = harness.client.get("/", headers=harness.headers("globex"))
    log.info("globex 2nd -> %d %s", r.status_code, r.json())
    assert r.status_code == 429 and r.json()["error"]["code"] == "rate_limited"
    harness.config_store.update({"rate_limit_per_minute": 60, "quota_per_day": 10000})


def test_quota_override_scoped_to_tenant(harness):
    assert _set(harness, "acme", {"quota_per_day": 2}).status_code == 200
    codes = [harness.client.get("/", headers=harness.headers("acme")).status_code for _ in range(3)]
    log.info("acme quota override codes=%s", codes)
    assert codes == [200, 200, 429]
    assert harness.client.get("/", headers=harness.headers("acme")).json()["error"]["code"] == "quota_exceeded"
    # 其他租户配额不变
    assert harness.client.get("/", headers=harness.headers("globex")).status_code == 200


def test_idempotency_ttl_override(harness):
    assert _set(harness, "acme", {"idempotency_ttl_seconds": 30}).status_code == 200
    h = harness.headers("acme", **{"Idempotency-Key": "k-ttl"})
    assert harness.client.post("/orders", headers=h, json={"i": 1}).status_code == 201
    harness.clock.advance(31)  # 超过租户专属 TTL（全局默认 86400）
    r = harness.client.post("/orders", headers=h, json={"i": 1})
    log.info("after tenant ttl -> %d replay=%s", r.status_code, r.headers.get("x-idempotent-replay"))
    assert "x-idempotent-replay" not in r.headers  # 视为新请求
    assert harness.side_effects["acme"] == 2


@pytest.mark.parametrize("payload", [
    {"rate_limit_per_minute": 0},              # 越界
    {"quota_per_day": -1},                     # 越界
    {"idempotency_ttl_seconds": 0.5},          # 越界
    {"rate_limit_per_minute": "fast"},         # 类型错
    {"rate_limit_per_minute": True},           # bool 非整数
    {"state_failure_mode": "deny"},            # 不可按租户覆盖的字段
    {"unknown_field": 1},                      # 未知字段
    {},                                        # 空覆盖（无意义，视为矛盾输入）
])
def test_invalid_policy_rejected_and_previous_kept(harness, payload):
    assert _set(harness, "acme", {"rate_limit_per_minute": 5}).status_code == 200
    before = harness.client.get("/admin/policies").json()
    r = _set(harness, "acme", payload)
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "invalid_policy"
    after = harness.client.get("/admin/policies").json()
    log.info("invalid %s -> kept %s", payload, after)
    assert before == after  # 原策略原样保留，绝不静默退回默认
    assert after["policies"]["acme"] == {"rate_limit_per_minute": 5}


def test_delete_policy_falls_back_to_global_default(harness):
    assert _set(harness, "acme", {"rate_limit_per_minute": 1}).status_code == 200
    assert harness.client.get("/", headers=harness.headers("acme")).status_code == 200
    assert harness.client.get("/", headers=harness.headers("acme")).status_code == 429  # 覆盖生效
    r = harness.client.delete("/admin/policies/acme")
    log.info("delete -> %d %s", r.status_code, r.json())
    assert r.status_code == 200 and r.json()["applied"] is True
    harness.clock.advance(60)  # 让旧桶按新速率恢复
    codes = [harness.client.get("/", headers=harness.headers("acme")).status_code for _ in range(5)]
    assert codes == [200] * 5  # 回落全局默认 60/分


def test_policy_for_unknown_tenant_rejected(harness):
    r = _set(harness, "ghost", {"rate_limit_per_minute": 5})
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "unknown_tenant"


def test_policy_store_bounded():
    from app.policies import PolicyStore, PolicyValidationError
    store = PolicyStore(max_overrides=3)
    for i in range(3):
        store.set(f"t{i}", {"quota_per_day": 10})
    try:
        store.set("t3", {"quota_per_day": 10})
        raise AssertionError("should reject")
    except PolicyValidationError as e:
        log.info("bounded store -> %s", e)
    store.set("t0", {"quota_per_day": 20})  # 覆盖已有条目不受上限影响
    assert store.get("t0").quota_per_day == 20
