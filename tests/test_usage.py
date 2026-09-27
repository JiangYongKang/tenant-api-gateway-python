"""租户用量视图：字段语义、并发自洽、降级表现、隔离与无敏感信息。"""
from __future__ import annotations

import logging
import threading

from app.usage import UsageStatus

log = logging.getLogger("governance-tests")


def _usage(harness, tenant_id):
    return harness.client.get(f"/admin/usage/{tenant_id}")


def test_fresh_tenant_view(harness):
    resp = _usage(harness, "acme")
    assert resp.status_code == 200
    body = resp.json()
    log.info("fresh view -> %s", body)
    assert body["status"] == "ok"
    assert body["rate"]["limit_per_minute"] == 60
    assert body["rate"]["remaining"] == 60
    assert body["rate"]["retry_after_seconds"] is None
    assert body["quota"]["used"] == 0
    assert body["quota"]["remaining"] == body["quota"]["limit_per_day"] == 10_000
    assert body["quota"]["reset_seconds"] > 0
    assert body["idempotency"]["records"] == 0


def test_view_tracks_consumption(harness):
    for _ in range(3):
        assert harness.client.get("/", headers=harness.headers("acme")).status_code == 200
    harness.client.post("/orders", headers=harness.headers("acme", **{"idempotency-key": "k1"}), json={"a": 1})

    body = _usage(harness, "acme").json()
    log.info("after consumption -> %s", body)
    assert body["quota"]["used"] == 4
    assert body["quota"]["remaining"] == body["quota"]["limit_per_day"] - 4
    assert body["rate"]["remaining"] < 60
    assert body["idempotency"]["records"] == 1
    # 自洽：已用 + 剩余 == 上限
    assert body["quota"]["used"] + body["quota"]["remaining"] == body["quota"]["limit_per_day"]


def test_view_reflects_override_limits(harness):
    harness.client.put("/admin/tenants/acme/policy", json={"rate_limit_per_minute": 5, "quota_per_day": 7})
    body = _usage(harness, "acme").json()
    assert body["rate"]["limit_per_minute"] == 5
    assert body["quota"]["limit_per_day"] == 7
    # 其他租户仍是全局默认
    assert _usage(harness, "globex").json()["quota"]["limit_per_day"] == 10_000


def test_rate_exhaustion_view(harness):
    harness.client.put("/admin/tenants/acme/policy", json={"rate_limit_per_minute": 1})
    assert harness.client.get("/", headers=harness.headers("acme")).status_code == 200
    assert harness.client.get("/", headers=harness.headers("acme")).status_code == 429
    body = _usage(harness, "acme").json()
    assert body["rate"]["remaining"] < 1
    assert body["rate"]["retry_after_seconds"] is not None
    assert body["rate"]["retry_after_seconds"] > 0


def test_idempotency_records_expire_from_view(harness):
    harness.client.put("/admin/tenants/acme/policy", json={"idempotency_ttl_seconds": 10})
    harness.client.post("/orders", headers=harness.headers("acme", **{"idempotency-key": "k1"}), json={"a": 1})
    assert _usage(harness, "acme").json()["idempotency"]["records"] == 1
    harness.clock.advance(11)
    assert _usage(harness, "acme").json()["idempotency"]["records"] == 0


def test_unknown_tenant_view(harness):
    resp = _usage(harness, "ghost")
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "unknown_tenant"


def test_view_degraded_on_state_failure(harness):
    harness.state_backend.set_failure(RuntimeError("boom"))
    resp = _usage(harness, "acme")
    log.info("degraded view -> %s %s", resp.status_code, resp.text)
    assert resp.status_code == 503
    assert resp.json()["error"]["code"] == "state_unavailable"
    assert "boom" not in resp.text  # 内部异常细节不外透
    # 恢复后立即可读
    harness.state_backend.set_failure(None)
    assert _usage(harness, "acme").status_code == 200


def test_view_degraded_on_slow_state(harness):
    harness.state_backend.set_latency(1.0)  # 超过 0.05s 超时预算
    try:
        resp = _usage(harness, "acme")
        assert resp.status_code == 503
        assert resp.json()["error"]["code"] == "state_unavailable"
    finally:
        harness.state_backend.set_latency(0.0)


def test_view_isolation_and_no_sensitive_data(harness):
    harness.client.post("/orders", headers=harness.headers("acme", **{"idempotency-key": "k1"}), json={"a": 1})
    resp = _usage(harness, "acme")
    assert resp.status_code == 200
    text = resp.text
    assert "tnt_" not in text          # 凭据明文
    assert "globex" not in text        # 其他租户
    assert "oldco" not in text
    # globex 的视图不受 acme 消费影响
    assert _usage(harness, "globex").json()["quota"]["used"] == 0


def test_view_self_consistent_under_concurrency(harness):
    """并发消费 + 并发查询：每个快照都满足 used <= limit 且 used + remaining == limit。"""
    harness.client.put("/admin/tenants/acme/policy", json={"quota_per_day": 500, "rate_limit_per_minute": 1_000_000})
    from app.usage import UsageService

    svc = UsageService(
        harness.registry,
        harness.config_store,
        harness.rate_limiter,
        harness.quota_tracker,
        harness.idempotency_store,
        harness.state_backend,
        clock=harness.clock,
        policy_store=harness.policy_store,
    )
    barrier = threading.Barrier(9)
    violations: list[str] = []

    def consumer():
        barrier.wait()
        for _ in range(50):
            harness.quota_tracker.consume("acme", 500, harness.clock.now)

    def observer():
        barrier.wait()
        for _ in range(200):
            v = svc.snapshot("acme")
            if v.status is not UsageStatus.OK:
                continue
            if v.quota_used > 500 or v.quota_used + v.quota_remaining != 500 or v.quota_remaining < 0:
                violations.append(f"used={v.quota_used} remaining={v.quota_remaining}")

    threads = [threading.Thread(target=consumer) for _ in range(8)] + [threading.Thread(target=observer)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    log.info("violations -> %s", violations[:3])
    assert not violations
    assert svc.snapshot("acme").quota_used == 400
