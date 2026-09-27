"""幂等重试推进：下游失败或降级期间，同标识的合法重试必须能重新推进、不被长期占住。"""
import logging

log = logging.getLogger("governance-tests")


def test_downstream_500_releases_key_and_retry_progresses(harness):
    h = harness.headers("acme", **{"Idempotency-Key": "retry-1"})
    harness.flaky_fail["fail"] = True
    r1 = harness.client.post("/flaky", headers=h, json={"i": 1})
    log.info("downstream 500 -> %d", r1.status_code)
    assert r1.status_code == 500
    # 记录未被占住：下游恢复后同标识重试按新请求推进
    harness.flaky_fail["fail"] = False
    r2 = harness.client.post("/flaky", headers=h, json={"i": 1})
    log.info("retry -> %d %s", r2.status_code, r2.json())
    assert r2.status_code == 200
    assert "x-idempotent-replay" not in r2.headers
    assert harness.side_effects["flaky"] == 1
    # 成功后的重放走缓存，不再产生副作用
    r3 = harness.client.post("/flaky", headers=h, json={"i": 1})
    assert r3.headers["x-idempotent-replay"] == "true"
    assert harness.side_effects["flaky"] == 1


def test_downstream_exception_releases_key_and_retry_progresses(harness):
    h = harness.headers("acme", **{"Idempotency-Key": "retry-2"})
    r1 = harness.client.post("/exploding", headers=h, json={"i": 1})
    log.info("downstream exception -> %d %s", r1.status_code, r1.json())
    assert r1.status_code == 503  # 治理链路按降级路径处理，异常细节不外透
    assert r1.json()["error"]["code"] == "internal_error"
    assert "downstream crashed" not in r1.text and "Traceback" not in r1.text
    # 记录已被完全释放：同标识重试按新请求推进，不报 in_progress/conflict
    assert harness.idempotency_store.size() == 0
    r2 = harness.client.post("/flaky", headers=h, json={"i": 1})
    log.info("same-key retry on healthy downstream -> %d", r2.status_code)
    assert r2.status_code == 200
    assert "x-idempotent-replay" not in r2.headers
    assert harness.side_effects["flaky"] == 1


def test_key_not_stuck_during_degradation(harness):
    harness.config_store.update({"state_failure_mode": "allow_with_degradation"})
    harness.state_backend.set_failure(RuntimeError("down"))
    h = harness.headers("acme", **{"Idempotency-Key": "retry-3"})
    r1 = harness.client.post("/flaky", headers=h, json={"i": 1})
    log.info("during degradation -> %d x-governance=%s", r1.status_code, r1.headers.get("x-governance"))
    assert r1.status_code == 200
    assert r1.headers["x-governance"] == "degraded"
    assert harness.idempotency_store.size() == 0  # 降级期间不留记录
    # 恢复后同标识重试：不被占住，按新请求正常推进
    harness.state_backend.set_failure(None)
    r2 = harness.client.post("/flaky", headers=h, json={"i": 1})
    log.info("after recovery -> %d", r2.status_code)
    assert r2.status_code == 200
    assert "x-idempotent-replay" not in r2.headers
    harness.config_store.update({"state_failure_mode": "deny"})


def test_abandon_does_not_touch_completed_record(harness):
    # 已完成（成功）的记录不受 abandon 影响：重放仍走缓存
    h = harness.headers("acme", **{"Idempotency-Key": "retry-4"})
    assert harness.client.post("/flaky", headers=h, json={"i": 1}).status_code == 200
    harness.idempotency_store.abandon("acme", "retry-4")  # 直接调用：不应删除已完成记录
    r = harness.client.post("/flaky", headers=h, json={"i": 1})
    assert r.headers["x-idempotent-replay"] == "true"
    assert harness.side_effects["flaky"] == 1
