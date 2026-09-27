"""租户级差异化策略：覆盖优先级、非法拒绝、即时生效、租户间隔离。"""
from __future__ import annotations

import logging

import pytest

from app.config import ConfigValidationError, GovernanceConfig
from app.overrides import TenantPolicyStore

log = logging.getLogger("governance-tests")


def _set_policy(harness, tenant_id, data):
    return harness.client.put(f"/admin/tenants/{tenant_id}/policy", json=data)


def test_rate_override_only_affects_target_tenant(harness):
    r = _set_policy(harness, "acme", {"rate_limit_per_minute": 2})
    assert r.status_code == 200, r.text
    assert r.json()["overridden_fields"] == ["rate_limit_per_minute"]

    # acme：前 2 个放行，第 3 个被限流
    results = [harness.client.get("/", headers=harness.headers("acme")).status_code for _ in range(3)]
    log.info("acme with override -> %s", results)
    assert results == [200, 200, 429]

    # globex：仍按全局默认 60，不受 acme 覆盖影响
    results = [harness.client.get("/", headers=harness.headers("globex")).status_code for _ in range(3)]
    assert results == [200, 200, 200]


def test_override_precedence_field_by_field(harness):
    """逐字段覆盖：被覆盖字段用专属值，未覆盖字段跟随全局默认及其后续调整。"""
    _set_policy(harness, "acme", {"rate_limit_per_minute": 1})
    # 全局默认配额改小；acme 未覆盖 quota_per_day -> 跟随全局
    harness.config_store.update({"quota_per_day": 2})
    assert [harness.client.get("/", headers=harness.headers("acme")).status_code for _ in range(2)] == [200, 429]
    # 第一次 429 是 rate_limited（覆盖值 1/min），第二次是 quota_exceeded（跟随全局 2/day）
    harness.clock.advance(60)
    codes = []
    for _ in range(2):
        resp = harness.client.get("/", headers=harness.headers("acme"))
        codes.append(resp.json().get("error", {}).get("code"))
        harness.clock.advance(60)
    log.info("acme codes -> %s", codes)
    assert codes == [None, "quota_exceeded"]


def test_quota_override(harness):
    _set_policy(harness, "acme", {"quota_per_day": 2})
    results = [harness.client.get("/", headers=harness.headers("acme")).status_code for _ in range(3)]
    assert results == [200, 200, 429]
    resp = harness.client.get("/", headers=harness.headers("acme"))
    assert resp.json()["error"]["code"] == "quota_exceeded"


def test_invalid_override_rejected_and_previous_kept(harness):
    _set_policy(harness, "acme", {"rate_limit_per_minute": 5})
    for bad in (
        {"rate_limit_per_minute": 0},            # 越界
        {"rate_limit_per_minute": "fast"},       # 类型错
        {"rate_limit_per_minute": True},         # bool 非整数
        {"unknown_field": 1},                    # 未知项
        {},                                      # 空覆盖
        {"quota_per_day": -1},
        {"idempotency_ttl_seconds": 0},
    ):
        r = _set_policy(harness, "acme", bad)
        assert r.status_code == 422, (bad, r.text)
        assert r.json()["error"]["code"] == "invalid_policy"
    # 原有策略保留：仍是 5/min
    results = [harness.client.get("/", headers=harness.headers("acme")).status_code for _ in range(6)]
    log.info("after rejected overrides -> %s", results)
    assert results == [200] * 5 + [429]


def test_override_for_unknown_tenant_rejected(harness):
    r = _set_policy(harness, "ghost", {"rate_limit_per_minute": 1})
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "unknown_tenant"


def test_clear_override_restores_global_default(harness):
    _set_policy(harness, "acme", {"rate_limit_per_minute": 1})
    assert harness.client.get("/", headers=harness.headers("acme")).status_code == 200
    assert harness.client.get("/", headers=harness.headers("acme")).status_code == 429

    r = harness.client.delete("/admin/tenants/acme/policy")
    assert r.json()["cleared"] is True
    harness.clock.advance(60)
    # 回到全局默认 60/min
    results = [harness.client.get("/", headers=harness.headers("acme")).status_code for _ in range(3)]
    assert results == [200, 200, 200]


def test_idempotency_ttl_override_unit():
    """TTL 覆盖即时影响该租户的幂等记录有效期。"""
    from app.idempotency import BeginResult, IdempotencyStore

    store = TenantPolicyStore()
    store.set_override("acme", {"idempotency_ttl_seconds": 10})
    policy = store.effective_for("acme", GovernanceConfig())
    assert policy.idempotency_ttl_seconds == 10
    assert policy.overridden_fields == ("idempotency_ttl_seconds",)

    idem = IdempotencyStore()
    assert idem.begin("acme", "k", "fp", policy.idempotency_ttl_seconds, now=0.0).result is BeginResult.NEW
    idem.complete("acme", "k", 200, b"ok", now=0.0)
    # 10s 后记录过期 -> 同标识重新推进而非重放
    assert idem.begin("acme", "k", "fp", policy.idempotency_ttl_seconds, now=11.0).result is BeginResult.NEW


def test_policy_store_bounded():
    store = TenantPolicyStore(max_tenants=1)
    store.set_override("a", {"rate_limit_per_minute": 1})
    with pytest.raises(ConfigValidationError):
        store.set_override("b", {"rate_limit_per_minute": 1})
    # 已有租户继续调整不受上限影响
    store.set_override("a", {"quota_per_day": 5})
