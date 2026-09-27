"""租户在线生命周期：开通/换发/停用/恢复/平滑下线，原子批量与结论一致性。"""
from __future__ import annotations

import logging

import pytest

from app.lifecycle import LifecycleError, LifecycleOperation, LifecycleAction, TenantLifecycleService
from app.tenancy import TenantRegistry, TenantStatus

log = logging.getLogger("governance-tests")


def test_provision_new_tenant_immediately_effective(harness):
    r = harness.apply_lifecycle({"action": "provision", "tenant_id": "newco"})
    assert r.status_code == 200, r.text
    resp = harness.client.get("/", headers=harness.headers("newco"))
    log.info("provisioned tenant access -> %s", resp.status_code)
    assert resp.status_code == 200


def test_provision_existing_rejected_and_unchanged(harness):
    r = harness.apply_lifecycle({"action": "provision", "tenant_id": "acme"})
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "invalid_lifecycle"
    # 原租户不受影响
    assert harness.client.get("/", headers=harness.headers("acme")).status_code == 200


def test_rotate_invalidates_old_credential(harness):
    old = harness.cred("acme")
    r = harness.apply_lifecycle({"action": "rotate", "tenant_id": "acme"})
    assert r.status_code == 200
    resp_old = harness.client.get("/", headers={"x-tenant-credential": old})
    resp_new = harness.client.get("/", headers=harness.headers("acme"))
    log.info("old cred -> %s, new cred -> %s", resp_old.status_code, resp_new.status_code)
    assert resp_old.status_code == 401
    assert resp_old.json()["error"]["code"] == "bad_signature"
    assert resp_new.status_code == 200


def test_disable_then_enable(harness):
    assert harness.apply_lifecycle({"action": "disable", "tenant_id": "acme"}).status_code == 200
    resp = harness.client.get("/", headers=harness.headers("acme"))
    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "tenant_disabled"

    assert harness.apply_lifecycle({"action": "enable", "tenant_id": "acme"}).status_code == 200
    assert harness.client.get("/", headers=harness.headers("acme")).status_code == 200


def test_drain_gives_distinct_conclusion_then_disables(harness):
    assert harness.apply_lifecycle({"action": "drain", "tenant_id": "acme", "drain_seconds": 60}).status_code == 200

    # 过渡态：与凭据无效(401 bad_signature)、已停用(401 tenant_disabled)都不同
    resp = harness.client.get("/", headers=harness.headers("acme"))
    log.info("draining -> %s %s", resp.status_code, resp.json())
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "tenant_draining"

    # 过渡结束 -> 真正停用
    harness.clock.advance(61)
    resp = harness.client.get("/", headers=harness.headers("acme"))
    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "tenant_disabled"


def test_in_flight_request_conclusion_consistent(harness):
    """调整前已解析身份的请求按原快照收尾；调整后新请求按新状态判定，二者不自相矛盾。"""
    from app.tenancy import TenantResolver

    resolver = TenantResolver(harness.registry, clock=harness.clock)
    resolved_before = resolver.resolve({"x-tenant-credential": harness.cred("acme")})
    assert resolved_before.status is TenantStatus.ACTIVE

    assert harness.apply_lifecycle({"action": "drain", "tenant_id": "acme", "drain_seconds": 30}).status_code == 200

    # 已解析的快照不变（在处理请求正常收尾）
    assert resolved_before.status is TenantStatus.ACTIVE
    # 新解析立即得到过渡态结论
    with pytest.raises(Exception) as ei:
        resolver.resolve({"x-tenant-credential": harness.cred("acme")})
    assert ei.value.reason.value == "tenant_draining"


def test_batch_atomic_rejection(harness):
    """一批中任一操作非法 -> 整批拒绝，合法操作也不生效。"""
    r = harness.apply_lifecycle(
        {"action": "provision", "tenant_id": "newco"},
        {"action": "rotate", "tenant_id": "ghost"},  # 不存在
    )
    assert r.status_code == 422
    # newco 不应被开通
    resp = harness.client.get("/", headers=harness.headers("newco"))
    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "unknown_tenant"


def test_batch_conflicting_ops_same_tenant_rejected(harness):
    r = harness.apply_lifecycle(
        {"action": "disable", "tenant_id": "acme"},
        {"action": "enable", "tenant_id": "acme"},
    )
    assert r.status_code == 422
    assert harness.client.get("/", headers=harness.headers("acme")).status_code == 200


def test_invalid_transitions_rejected(harness):
    # enable 一个 ACTIVE 租户
    assert harness.apply_lifecycle({"action": "enable", "tenant_id": "acme"}).status_code == 422
    # drain 一个 DISABLED 租户
    assert harness.apply_lifecycle({"action": "drain", "tenant_id": "oldco", "drain_seconds": 10}).status_code == 422
    # disable 不存在的租户
    assert harness.apply_lifecycle({"action": "disable", "tenant_id": "ghost"}).status_code == 422
    # 非法 drain_seconds
    assert harness.apply_lifecycle({"action": "drain", "tenant_id": "acme", "drain_seconds": -5}).status_code == 422
    assert harness.apply_lifecycle({"action": "drain", "tenant_id": "acme"}).status_code == 422
    # 未知 action / 非法 tenant_id
    assert harness.apply_lifecycle({"action": "explode", "tenant_id": "acme"}).status_code == 422
    assert harness.apply_lifecycle({"action": "provision", "tenant_id": "BAD ID!"}).status_code == 422
    # 状态完全不变
    assert harness.client.get("/", headers=harness.headers("acme")).status_code == 200


def test_rotate_preserves_draining_state(harness):
    assert harness.apply_lifecycle({"action": "drain", "tenant_id": "acme", "drain_seconds": 60}).status_code == 200
    assert harness.apply_lifecycle({"action": "rotate", "tenant_id": "acme"}).status_code == 200
    # 换发后仍是过渡态，且新凭据同样被过渡态拒绝
    resp = harness.client.get("/", headers=harness.headers("acme"))
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "tenant_draining"


def test_registry_bounded():
    """注册表有界：超过上限的开通整批拒绝，运行期不膨胀。"""
    registry = TenantRegistry(secret=b"s")
    svc = TenantLifecycleService(registry, max_tenants=2)
    svc.apply([LifecycleOperation(LifecycleAction.PROVISION, "t1")], now=0.0)
    svc.apply([LifecycleOperation(LifecycleAction.PROVISION, "t2")], now=0.0)
    with pytest.raises(LifecycleError):
        svc.apply([LifecycleOperation(LifecycleAction.PROVISION, "t3")], now=0.0)
    assert registry.tenant_count() == 2


def test_tenant_listing_reflects_effective_status(harness):
    assert harness.apply_lifecycle({"action": "drain", "tenant_id": "acme", "drain_seconds": 30}).status_code == 200
    listing = {t["tenant_id"]: t for t in harness.client.get("/admin/tenants").json()["tenants"]}
    assert listing["acme"]["status"] == "draining"
    harness.clock.advance(31)
    listing = {t["tenant_id"]: t for t in harness.client.get("/admin/tenants").json()["tenants"]}
    assert listing["acme"]["status"] == "disabled"
