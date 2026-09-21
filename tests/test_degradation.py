"""降级：状态不可读/变慢时的可观测行为；不外透异常、不静默放行、不留半更新。"""
import logging

from app.audit import AuditOutcome

log = logging.getLogger("governance-tests")


def test_state_failure_deny_mode_rejects_with_503(harness):
    harness.state_backend.set_failure(RuntimeError("disk corrupted"))
    r = harness.client.get("/", headers=harness.headers("acme"))
    log.info("deny mode degraded -> %d %s", r.status_code, r.json())
    assert r.status_code == 503
    assert r.json()["error"]["code"] == "state_unavailable"
    # 内部异常细节不外透
    assert "disk corrupted" not in r.text
    assert "Traceback" not in r.text
    # 可观测：审计留下 degraded 记录
    records = harness.audit_log.query(outcome=AuditOutcome.DEGRADED)
    assert len(records) == 1 and records[0].reason == "state_unavailable"


def test_state_failure_not_silent_pass_through(harness):
    # deny 模式下绝不静默放行全部流量
    harness.state_backend.set_failure(RuntimeError("down"))
    codes = {harness.client.get("/", headers=harness.headers("acme")).status_code for _ in range(5)}
    log.info("deny mode status codes under failure: %s", codes)
    assert codes == {503}


def test_slow_state_read_treated_as_unavailable(harness):
    harness.state_backend.set_latency(0.2)  # 超过 0.05s 预算
    r = harness.client.get("/", headers=harness.headers("acme"))
    log.info("slow state -> %d %s", r.status_code, r.json())
    assert r.status_code == 503
    assert r.json()["error"]["code"] == "state_unavailable"


def test_allow_with_degradation_mode_passes_without_state_writes(harness):
    harness.config_store.update({"state_failure_mode": "allow_with_degradation"})
    harness.state_backend.set_failure(RuntimeError("down"))
    r = harness.client.get("/", headers=harness.headers("acme"))
    log.info("allow_with_degradation -> %d x-governance=%s", r.status_code, r.headers.get("x-governance"))
    assert r.status_code == 200
    assert r.headers["x-governance"] == "degraded"  # 可观测
    records = harness.audit_log.query(outcome=AuditOutcome.DEGRADED)
    assert len(records) == 1

    # 不留半更新状态：降级期间未写任何计量
    assert harness.quota_tracker.usage("acme", harness.clock.now) == 0
    assert harness.idempotency_store.size() == 0


def test_recovery_after_failure(harness):
    harness.state_backend.set_failure(RuntimeError("down"))
    assert harness.client.get("/", headers=harness.headers("acme")).status_code == 503
    harness.state_backend.set_failure(None)
    r = harness.client.get("/", headers=harness.headers("acme"))
    log.info("after recovery -> %d", r.status_code)
    assert r.status_code == 200


def test_degraded_requests_do_not_consume_quota_after_recovery(harness):
    # 降级(deny)期间的请求不扣额度；恢复后额度完整
    harness.config_store.update({"quota_per_day": 2})
    harness.state_backend.set_failure(RuntimeError("down"))
    assert harness.client.get("/", headers=harness.headers("acme")).status_code == 503
    harness.state_backend.set_failure(None)
    assert harness.client.get("/", headers=harness.headers("acme")).status_code == 200
    assert harness.client.get("/", headers=harness.headers("acme")).status_code == 200
    assert harness.client.get("/", headers=harness.headers("acme")).status_code == 429
    log.info("quota usage=%d (cap=2)", harness.quota_tracker.usage("acme", harness.clock.now))
    assert harness.quota_tracker.usage("acme", harness.clock.now) == 2
