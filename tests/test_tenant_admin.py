"""租户在线生命周期：开通/换发/停用/平滑下线，原子生效，结论一致，有界可回收。"""
import logging
import threading

import pytest

from app.tenancy import (
    TenancyError,
    TenantAdminError,
    TenantRegistry,
    TenantResolver,
    TenantStatus,
)

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


# ---------------------------------------------------------------------------
# 整批原子性：容量超限等"只能在应用时才暴露"的失败也必须零改动、零痕迹
# ---------------------------------------------------------------------------

def _snapshot(harness):
    """提交前可逐项比对的状态快照：租户清单/详情 + 每个租户的凭据世代签名。"""
    listing = harness.client.get("/admin/tenants").json()
    creds = {t["tenant_id"]: harness.registry.signature_for(t["tenant_id"])
             for t in listing["tenants"]}
    details = {tid: harness.client.get(f"/admin/tenants/{tid}").json() for tid in creds}
    return {"listing": listing, "details": details, "creds": creds}


def test_capacity_rejection_leaves_credentials_and_state_untouched(small_harness):
    # 容量=3 恰好满；批次先换发 acme（本会立即作废旧凭据），再新增一个开不出来的租户。
    # 修复前：整批 422 但换发已落库，旧凭据当场 bad_signature；修复后必须完全不变。
    before = _snapshot(small_harness)
    old_cred = small_harness.cred("acme")
    r = _batch(
        small_harness,
        {"op": "rotate_credential", "tenant_id": "acme"},
        {"op": "provision", "tenant_id": "newco"},   # 容量超限，整批开不出来
    )
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "invalid_tenant_batch"

    after = _snapshot(small_harness)
    log.info("capacity reject before=%s after=%s", before, after)
    assert after == before                                    # 清单/详情/凭据签名逐项一致
    assert small_harness.registry.lookup("newco") is None     # 新租户不留痕
    assert small_harness.registry.size() == 2
    # 旧凭据当场仍可正常通过（没有被"半次换发"作废旧凭据）
    assert small_harness.client.get("/", headers={"x-tenant-credential": old_cred}).status_code == 200


def test_capacity_rejection_multiple_provisions_unchanged(small_harness):
    before = _snapshot(small_harness)
    r = _batch(
        small_harness,
        {"op": "suspend", "tenant_id": "acme"},
        {"op": "provision", "tenant_id": "new1"},
        {"op": "provision", "tenant_id": "new2"},
    )
    assert r.status_code == 422
    assert _snapshot(small_harness) == before
    assert small_harness.client.get("/", headers=small_harness.headers("acme")).status_code == 200


def test_capacity_rejection_mixed_with_invalid_op_unchanged(small_harness):
    # 容量超限与非法取值/类型错误同时出现：继续整批拒绝、零改动
    before = _snapshot(small_harness)
    r = _batch(
        small_harness,
        {"op": "rotate_credential", "tenant_id": "globex"},
        {"op": "decommission", "tenant_id": "acme", "drain_seconds": -1},  # 越界
        {"op": "provision", "tenant_id": "newco"},                        # 容量超限
    )
    assert r.status_code == 422
    assert _snapshot(small_harness) == before
    assert small_harness.client.get("/", headers=small_harness.headers("globex")).status_code == 200


def test_registry_capacity_rejection_is_all_or_nothing():
    # 不经过 HTTP 的确定性复现：把容量上限调小，换发+超限开通必须零改动
    registry = TenantRegistry(secret=b"s", max_tenants=2)
    registry.add("aa")
    registry.add("bb")
    old_sig = registry.signature_for("aa")

    with pytest.raises(TenantAdminError) as exc:
        registry.apply_batch([
            {"op": "rotate_credential", "tenant_id": "aa"},
            {"op": "provision", "tenant_id": "cc"},
        ])
    log.info("capacity full rejection -> %s", exc.value.errors)

    assert registry.tenant_ids() == ["aa", "bb"]
    assert registry.signature_for("aa") == old_sig       # 世代未推进，旧凭据仍有效
    resolver = TenantResolver(registry)
    tenant = resolver.resolve({"x-tenant-credential": registry.issue("aa")})
    assert tenant.status is TenantStatus.ACTIVE
    # 新租户根本没留下：任何形态正确的凭据都以 unknown_tenant 拒绝（而非 bad_signature）
    try:
        resolver.resolve({"x-tenant-credential": "tnt_cc.0000000000000000"})
        raise AssertionError("cc should not exist")
    except TenancyError as e:
        assert e.reason.value == "unknown_tenant"


# ---------------------------------------------------------------------------
# 并发冲突批次：最后只能有一个批次整体生效，其余整批拒绝且不留痕
# ---------------------------------------------------------------------------

def test_concurrent_identical_multiop_batches_exactly_one_applies(harness):
    # 同一批次被多线程同时提交（suspend acme + provision 新租户）：只有一个整体生效
    ops = [
        {"op": "suspend", "tenant_id": "acme"},
        {"op": "provision", "tenant_id": "raceco"},
    ]
    statuses: list[int] = []
    barrier = threading.Barrier(10)

    def worker():
        barrier.wait()
        r = harness.client.post("/admin/tenants", json={"operations": ops})
        statuses.append(r.status_code)

    threads = [threading.Thread(target=worker) for _ in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    log.info("concurrent identical batches: %s", sorted(statuses))
    assert sorted(statuses).count(200) == 1
    assert sorted(statuses).count(422) == 9
    # 只有获胜批次的整体结果：acme 已停用，新租户恰好一个
    assert harness.registry.lookup("acme").status is TenantStatus.SUSPENDED
    assert harness.registry.lookup("raceco").status is TenantStatus.ACTIVE
    assert harness.registry.tenant_ids().count("raceco") == 1


def test_concurrent_tight_capacity_batches_only_one_whole_batch_wins(small_harness):
    # 容量=2 恰好满：每个批次都是 [换发 acme, 立即下线 globex(腾出 1 个位), 新增唯一租户]。
    # 第一个批次整体生效后，globex 进入终态可回收、新租户占住腾出来的位；
    # 其余批次再提交时 globex 已 DISABLED（不能重复下线）且无剩余空间，整批 422。
    # 修复前：失败批次的换发也会推进世代，留下半成品（旧凭据作废、世代叠加）；
    # 修复后：恰好一个批次 200 并整体可见，其余 422 且 acme 世代只被获胜批次推进一次。
    n = 10
    barrier = threading.Barrier(n)
    statuses: list[int] = []
    old_cred = small_harness.cred("acme")

    def worker(k):
        barrier.wait()
        r = small_harness.client.post("/admin/tenants", json={"operations": [
            {"op": "rotate_credential", "tenant_id": "acme"},
            {"op": "decommission", "tenant_id": "globex", "drain_seconds": 0},
            {"op": "provision", "tenant_id": f"race{k:02d}"},
        ]})
        statuses.append(r.status_code)

    threads = [threading.Thread(target=worker, args=(k,)) for k in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    log.info("tight-capacity concurrent batches: %s", sorted(statuses))
    assert sorted(statuses).count(200) == 1
    assert sorted(statuses).count(422) == n - 1

    winners = [tid for tid in small_harness.registry.tenant_ids() if tid.startswith("race")]
    assert len(winners) == 1                     # 只有获胜批次的新租户落库，其余不留痕
    assert small_harness.registry.size() == 2   # 腾 1 进 1：始终有界
    # 获胜批次的整体结果：globex 已立即下线并在开通时被终态回收，不残留在清单中
    assert small_harness.registry.lookup("globex") is None

    # 旧凭据在获胜批次换发后失效（恰好推进一次）；不存在"失败批次也推进世代"的叠加
    assert small_harness.client.get("/", headers={"x-tenant-credential": old_cred}).status_code == 401
    new_cred = small_harness.cred("acme")
    assert new_cred != old_cred
    assert small_harness.client.get("/", headers={"x-tenant-credential": new_cred}).status_code == 200
    detail = small_harness.client.get("/admin/tenants/acme").json()
    assert detail == {"tenant_id": "acme", "status": "active"}


def test_concurrent_capacity_race_losing_batches_leave_no_credential_trace(reclaim_harness):
    # 容量=2：acme ACTIVE + gone DISABLED（可回收 1 个位）。每个批次都是
    # [换发 acme, 开通本线程唯一新租户]。恰好一个批次能回收 gone 后整体成功；
    # 其余批次无位可开，整批 422。
    # 旧代码的应用顺序恰好先换发、再在开通时抛容量错误——失败批次仍把 acme 世代推进，
    # 最终世代被叠加到 n，留下半成品；修复后失败批次零改动，世代恰好为 1。
    n = 10
    barrier = threading.Barrier(n)
    statuses: list[int] = []
    old_cred = reclaim_harness.cred("acme")

    def worker(k):
        barrier.wait()
        r = reclaim_harness.client.post("/admin/tenants", json={"operations": [
            {"op": "rotate_credential", "tenant_id": "acme"},
            {"op": "provision", "tenant_id": f"race{k:02d}"},
        ]})
        statuses.append(r.status_code)

    threads = [threading.Thread(target=worker, args=(k,)) for k in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    log.info("reclaim capacity-race batches: %s", sorted(statuses))
    assert sorted(statuses).count(200) == 1
    assert sorted(statuses).count(422) == n - 1

    winners = [tid for tid in reclaim_harness.registry.tenant_ids() if tid.startswith("race")]
    assert len(winners) == 1                     # 仅获胜批次的新租户落库
    assert reclaim_harness.registry.size() == 2  # 腾 1 进 1，始终有界
    assert reclaim_harness.registry.lookup("gone") is None  # 终态被获胜批次回收

    # acme 凭据只被获胜批次换发一次：旧凭据失效、当前凭据有效、世代恰好 1
    assert reclaim_harness.client.get("/", headers={"x-tenant-credential": old_cred}).status_code == 401
    assert reclaim_harness.client.get("/", headers=reclaim_harness.headers("acme")).status_code == 200
    assert reclaim_harness.registry._records["acme"].generation == 1
