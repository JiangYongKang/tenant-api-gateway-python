"""租户在线生命周期：开通/换发/停用/平滑下线，原子生效，结论一致，有界可回收。"""
import logging
import threading

import pytest

from app.tenancy import TenantAdminError, TenantRegistry, TenantStatus

log = logging.getLogger("governance-tests")


def _batch(harness, *ops):
    r = harness.client.post("/admin/tenants", json={"operations": list(ops)})
    log.info("batch %s -> %d %s", ops, r.status_code, r.json())
    return r


def test_provision_new_tenant_immediately_usable(harness):
    r = _batch(harness, {"op": "provision", "tenant_id": "newco"})
    assert r.status_code == 200 and r.json()["applied"] is True
    cred = r.json()["results"][0]["credential"]
    resp = harness.client.get("/", headers={"x-tenant-credential": cred})
    log.info("new tenant request -> %d", resp.status_code)
    assert resp.status_code == 200


def test_rotate_credential_invalidates_old(harness):
    old = harness.cred("acme")
    r = _batch(harness, {"op": "rotate_credential", "tenant_id": "acme"})
    assert r.status_code == 200
    new = r.json()["results"][0]["credential"]
    assert new != old
    # 旧凭据立即失效（签名不再匹配），新凭据即时生效
    assert harness.client.get("/", headers={"x-tenant-credential": old}).json()["error"]["code"] == "bad_signature"
    assert harness.client.get("/", headers={"x-tenant-credential": new}).status_code == 200


def test_suspend_and_resume(harness):
    assert _batch(harness, {"op": "suspend", "tenant_id": "acme"}).status_code == 200
    r = harness.client.get("/", headers=harness.headers("acme"))
    log.info("suspended -> %d %s", r.status_code, r.json())
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "tenant_suspended"  # 与 disabled/draining 可区分
    assert _batch(harness, {"op": "resume", "tenant_id": "acme"}).status_code == 200
    assert harness.client.get("/", headers=harness.headers("acme")).status_code == 200


def test_decommission_draining_then_disabled(harness):
    assert _batch(harness, {"op": "decommission", "tenant_id": "acme", "drain_seconds": 30}).status_code == 200
    # 过渡态：新请求得到与"凭据无效""已停用"都不同的明确结论
    r = harness.client.get("/", headers=harness.headers("acme"))
    log.info("draining -> %d %s", r.status_code, r.json())
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "tenant_draining"
    assert r.json()["error"]["code"] not in ("bad_signature", "tenant_disabled")
    # 过渡期满：真正停用
    harness.clock.advance(31)
    r = harness.client.get("/", headers=harness.headers("acme"))
    log.info("after drain -> %d %s", r.status_code, r.json())
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "tenant_disabled"


def test_inflight_request_conclusion_not_contradicted(harness):
    # 已解析（通过校验）的请求持有自己的结论：随后的停用不改写它
    tenant = harness.registry.authenticate("acme", harness.registry.signature_for("acme"))
    assert tenant.status is TenantStatus.ACTIVE
    _batch(harness, {"op": "suspend", "tenant_id": "acme"})
    log.info("in-flight snapshot status=%s, registry now=%s", tenant.status, harness.registry.lookup("acme").status)
    assert tenant.status is TenantStatus.ACTIVE  # 快照不变：在途请求正常收尾
    assert harness.registry.lookup("acme").status is TenantStatus.SUSPENDED  # 新判定用新状态


def test_invalid_batch_rejected_atomically(harness):
    before = harness.client.get("/admin/tenants").json()
    # 一批中混有非法操作：整批拒绝，合法操作也不生效
    r = _batch(
        harness,
        {"op": "provision", "tenant_id": "goodco"},
        {"op": "suspend", "tenant_id": "ghost"},  # 不存在
    )
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "invalid_tenant_batch"
    after = harness.client.get("/admin/tenants").json()
    log.info("before=%s after=%s", before, after)
    assert before == after  # 完全不变，不部分应用
    assert harness.registry.lookup("goodco") is None


def test_conflicting_ops_same_tenant_rejected(harness):
    r = _batch(harness, {"op": "suspend", "tenant_id": "acme"}, {"op": "resume", "tenant_id": "acme"})
    assert r.status_code == 422
    assert harness.registry.lookup("acme").status is TenantStatus.ACTIVE  # 原样保留


@pytest.mark.parametrize("ops", [
    [{"op": "provision", "tenant_id": "acme"}],                 # 已存在
    [{"op": "rotate_credential", "tenant_id": "ghost"}],        # 不存在
    [{"op": "suspend", "tenant_id": "oldco"}],                  # 非 ACTIVE
    [{"op": "resume", "tenant_id": "acme"}],                    # 非 SUSPENDED
    [{"op": "decommission", "tenant_id": "oldco"}],             # 已停用
    [{"op": "decommission", "tenant_id": "acme", "drain_seconds": -1}],   # 越界
    [{"op": "decommission", "tenant_id": "acme", "drain_seconds": "soon"}],  # 类型错
    [{"op": "explode", "tenant_id": "acme"}],                   # 未知操作
    [{"op": "provision", "tenant_id": "BAD ID!"}],              # 非法租户名
    [],                                                          # 空批
])
def test_each_invalid_op_rejected_and_state_kept(harness, ops):
    before = harness.client.get("/admin/tenants").json()
    r = _batch(harness, *ops)
    log.info("invalid ops %s -> %d", ops, r.status_code)
    assert r.status_code == 422
    assert harness.client.get("/admin/tenants").json() == before


def test_concurrent_conflicting_batches_exactly_one_applies(harness):
    results = []

    def worker():
        r = harness.client.post("/admin/tenants", json={"operations": [{"op": "suspend", "tenant_id": "acme"}]})
        results.append(r.status_code)

    threads = [threading.Thread(target=worker) for _ in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    log.info("concurrent suspend results: %s", sorted(results))
    assert sorted(results).count(200) == 1          # 只有一个批次生效
    assert sorted(results).count(422) == 9          # 其余被明确拒绝，而非静默退回
    assert harness.registry.lookup("acme").status is TenantStatus.SUSPENDED


def test_registry_bounded_and_reclaims_disabled():
    registry = TenantRegistry(secret=b"s", max_tenants=5)
    for i in range(5):
        registry.apply_batch([{"op": "provision", "tenant_id": f"t{i}"}])
    # 满员且无可回收：明确拒绝
    try:
        registry.apply_batch([{"op": "provision", "tenant_id": "t6"}])
        raise AssertionError("should reject")
    except TenantAdminError as e:
        log.info("full registry -> %s", e.errors)
    # 停用两个后可回收，开通成功，总量有界
    registry.apply_batch([{"op": "decommission", "tenant_id": "t0", "drain_seconds": 0}])
    registry.apply_batch([{"op": "decommission", "tenant_id": "t1", "drain_seconds": 0}])
    registry.apply_batch([{"op": "provision", "tenant_id": "t6"}])
    log.info("registry size after reclaim: %d", registry.size())
    assert registry.size() <= 5
    assert registry.lookup("t0") is None  # 终态租户被回收
