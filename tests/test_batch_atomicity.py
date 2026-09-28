"""批量租户变更的整批原子性与并发冲突语义。

复现两类关键场景（本地把容量上限调小即可稳定复现，不依赖时序运气）：
1. 批次内含容量超限的开通 -> 整批拒绝，租户清单/状态/凭据与提交前逐项一致，
   排在前面的换发不提前生效（旧凭据继续可用），终态租户不被提前回收；
2. 多个互相冲突的批次并发提交 -> 只有一个批次整体生效，其余整批冲突拒绝、零改动；
   租户互不相交的并发批次互不误伤。
"""
from __future__ import annotations

import logging
import threading

import pytest

from app.tenancy import (
    TenantAdminError,
    TenantBatchConflictError,
    TenantRegistry,
    TenantStatus,
)

log = logging.getLogger("governance-tests")


def _snapshot(registry: TenantRegistry) -> dict:
    """可逐项比对的注册表快照：状态 + 凭据世代（换发与否）+ 成员集合。"""
    out = {}
    for tid in registry.tenant_ids():
        rec = registry._records[tid]
        out[tid] = (rec.status, rec.generation, rec.drain_until)
    return out


def _credential_still_accepted(harness, tenant_id: str, cred: str) -> bool:
    return harness.client.get("/", headers={"x-tenant-credential": cred}).status_code == 200


# ---------------------------------------------------------------------------
# 场景 1：含容量超限的批次整批拒绝，状态与凭据完全不变
# ---------------------------------------------------------------------------

def test_batch_with_over_capacity_provision_rejected_without_touching_rotation(harness):
    # 本地把容量上限调小到 2：acme/globex 两个 ACTIVE 占满槽位，oldco 虽 DISABLED，
    # 但开新租户发生在处理到 oldco 之前，暂存表仍是满的（2），没有可回收空间。
    harness.registry._max_tenants = 2

    old_cred = harness.cred("acme")
    before_list = harness.client.get("/admin/tenants").json()
    before_acme = harness.client.get("/admin/tenants/acme").json()

    r = harness.client.post("/admin/tenants", json={"operations": [
        {"op": "rotate_credential", "tenant_id": "acme"},   # 排在前面
        {"op": "provision", "tenant_id": "newco"},          # 容量超限，开不出来
    ]})
    log.info("over-capacity batch -> %d %s", r.status_code, r.json())
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "invalid_tenant_batch"

    # 租户清单与单个租户状态逐项一致，新租户不存在
    assert harness.client.get("/admin/tenants").json() == before_list
    assert harness.client.get("/admin/tenants/acme").json() == before_acme
    assert harness.registry.lookup("newco") is None

    # 关键回归点：被拒批次里的换发没有提前生效，旧凭据当场仍可用
    assert _credential_still_accepted(harness, "acme", old_cred)


def test_over_capacity_rejection_keeps_disabled_tenant_and_all_credentials():
    # 直接在小容量注册表上复现：满员且全是 ACTIVE，无可回收空间
    registry = TenantRegistry(secret=b"s", max_tenants=2)
    registry.add("acme")
    registry.add("globex")
    acme_old = registry.issue("acme")
    globex_old = registry.issue("globex")
    before = _snapshot(registry)

    with pytest.raises(TenantAdminError) as exc:
        registry.apply_batch([
            {"op": "rotate_credential", "tenant_id": "acme"},
            {"op": "suspend", "tenant_id": "globex"},
            {"op": "provision", "tenant_id": "newco"},       # 容量超限
        ])
    log.info("rejected errors=%s", exc.value.errors)

    # 整批零改动：状态/世代/成员与提交前逐项一致；两条旧凭据都继续有效
    assert _snapshot(registry) == before
    assert registry.lookup("newco") is None
    assert registry.authenticate("acme", acme_old.split(".")[1]).status is TenantStatus.ACTIVE
    assert registry.authenticate("globex", globex_old.split(".")[1]).status is TenantStatus.ACTIVE


def test_reclaim_slot_consumed_earlier_in_batch_still_rejects_as_a_whole():
    # 满员且只有 1 个 DISABLED 可回收：批内第一条 provision 吃掉唯一槽位后，
    # 第二条 provision 再无空间 -> 整批拒绝；第一条的开通与终态回收都不得留下
    registry = TenantRegistry(secret=b"s", max_tenants=3)
    registry.add("aa01")
    registry.add("aa02")
    registry.add("deadco", TenantStatus.DISABLED)
    before = _snapshot(registry)

    with pytest.raises(TenantAdminError):
        registry.apply_batch([
            {"op": "provision", "tenant_id": "newone"},
            {"op": "provision", "tenant_id": "newtwo"},
        ])

    assert _snapshot(registry) == before
    assert registry.lookup("deadco") is not None  # 终态租户未被提前回收
    assert registry.lookup("newone") is None       # 第一条开通也不留痕迹
    assert registry.lookup("newtwo") is None


def test_reclaimable_capacity_still_applies_batch_atomically():
    # 对照场景：满员但存在 DISABLED 终态租户时，回收空间在暂存副本上完成，
    # 整批仍应整体成功（换发生效 + 新租户开通 + 终态回收一起提交）
    registry = TenantRegistry(secret=b"s", max_tenants=2)
    registry.add("acme")
    registry.add("deadco", TenantStatus.DISABLED)
    results = registry.apply_batch([
        {"op": "rotate_credential", "tenant_id": "acme"},
        {"op": "provision", "tenant_id": "newco"},
    ])
    assert [r["op"] for r in results] == ["rotate_credential", "provision"]
    assert registry.lookup("deadco") is None
    assert registry.lookup("newco") is not None
    assert registry._records["acme"].generation == 1
    assert registry.size() == 2  # 容量仍有界


@pytest.mark.parametrize("bad_ops", [
    [{"op": "rotate_credential", "tenant_id": "acme"},
     {"op": "suspend", "tenant_id": "acme"}],               # 同批重复租户
    [{"op": "rotate_credential", "tenant_id": "acme"},
     {"op": "decommission", "tenant_id": "acme", "drain_seconds": -1}],   # 越界
    [{"op": "rotate_credential", "tenant_id": "acme"},
     {"op": "decommission", "tenant_id": "acme", "drain_seconds": "x"}],  # 类型错
])
def test_rejected_batch_never_rotates_credential_even_if_rotate_comes_first(bad_ops):
    registry = TenantRegistry(secret=b"s", max_tenants=5)
    registry.add("acme")
    old = registry.issue("acme")
    before = _snapshot(registry)
    with pytest.raises(TenantAdminError):
        registry.apply_batch(bad_ops)
    assert _snapshot(registry) == before  # 包括换发世代在内逐项不变
    assert registry.authenticate("acme", old.split(".")[1]).status is TenantStatus.ACTIVE


# ---------------------------------------------------------------------------
# 场景 2：并发冲突批次只有一个整体生效，其余零改动
# ---------------------------------------------------------------------------

def test_concurrent_rotating_same_tenant_exactly_one_wins():
    registry = TenantRegistry(secret=b"s")
    registry.add("acme")
    n = 8
    barrier = threading.Barrier(n)
    registry.set_batch_gate(barrier.wait)  # 所有线程取完旧快照后再一起进临界区

    outcomes: list[str] = []

    def worker() -> None:
        try:
            registry.apply_batch([{"op": "rotate_credential", "tenant_id": "acme"}])
            outcomes.append("applied")
        except TenantBatchConflictError:
            outcomes.append("conflict")

    threads = [threading.Thread(target=worker) for _ in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    log.info("concurrent rotate outcomes=%s", sorted(outcomes))
    assert outcomes.count("applied") == 1
    assert outcomes.count("conflict") == n - 1
    # 唯一生效批次的痕迹：世代恰好 +1；败者不留半成品
    assert registry._records["acme"].generation == 1


def test_concurrent_provision_same_new_tenant_exactly_one_wins():
    registry = TenantRegistry(secret=b"s", max_tenants=10)
    n = 5
    barrier = threading.Barrier(n)
    registry.set_batch_gate(barrier.wait)
    outcomes: list[str] = []

    def worker() -> None:
        try:
            registry.apply_batch([{"op": "provision", "tenant_id": "newco"}])
            outcomes.append("applied")
        except TenantBatchConflictError:
            outcomes.append("conflict")

    threads = [threading.Thread(target=worker) for _ in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert outcomes.count("applied") == 1
    assert outcomes.count("conflict") == n - 1
    assert registry.size() == 1  # 只有一个新租户，无残留占槽


def test_concurrent_disjoint_batches_all_apply():
    # 租户互不相交的并发批次不是冲突：快照隔离下全部整体生效，互不误伤
    registry = TenantRegistry(secret=b"s", max_tenants=10)
    tenant_ids = [f"tenant{i:02d}" for i in range(6)]
    barrier = threading.Barrier(len(tenant_ids))
    registry.set_batch_gate(barrier.wait)
    outcomes: list[str] = []

    def worker(tid: str) -> None:
        try:
            registry.apply_batch([{"op": "provision", "tenant_id": tid}])
            outcomes.append("applied")
        except TenantBatchConflictError:
            outcomes.append("conflict")

    threads = [threading.Thread(target=worker, args=(tid,)) for tid in tenant_ids]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert outcomes.count("applied") == len(tenant_ids)
    assert sorted(registry.tenant_ids()) == tenant_ids


def test_concurrent_conflict_via_http_returns_distinct_code(harness):
    # HTTP 级：冲突批次得到可区分的 422 tenant_batch_conflict，且只有一个生效
    n = 6
    barrier = threading.Barrier(n)
    harness.registry.set_batch_gate(barrier.wait)
    statuses: list[int] = []
    codes: list[str] = []

    def worker() -> None:
        r = harness.client.post("/admin/tenants",
                                json={"operations": [{"op": "suspend", "tenant_id": "acme"}]})
        statuses.append(r.status_code)
        if r.status_code != 200:
            codes.append(r.json()["error"]["code"])

    threads = [threading.Thread(target=worker) for _ in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    log.info("http concurrent suspend statuses=%s codes=%s", sorted(statuses), codes)
    assert statuses.count(200) == 1
    assert statuses.count(422) == n - 1
    assert codes == ["tenant_batch_conflict"] * (n - 1)
    assert harness.registry.lookup("acme").status is TenantStatus.SUSPENDED
    harness.registry.set_batch_gate(None)
