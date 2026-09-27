"""副作用请求在下游失败/降级期间，同标识合法重试必须能重新推进，不被长期占住。"""
from __future__ import annotations

import logging

log = logging.getLogger("governance-tests")


def _post(harness, tenant="acme", key="k1", payload=None):
    return harness.client.post(
        "/flaky",
        headers=harness.headers(tenant, **{"idempotency-key": key}),
        json=payload if payload is not None else {"a": 1},
    )


def test_retry_after_downstream_5xx(harness):
    harness.controls["flaky_fail"] = True
    r1 = _post(harness)
    log.info("first attempt -> %s", r1.status_code)
    assert r1.status_code == 500
    assert harness.side_effects.get("acme", 0) == 0

    # 下游恢复后，同标识合法重试重新推进（不是 in_progress / conflict / 重放 500）
    harness.controls["flaky_fail"] = False
    r2 = _post(harness)
    assert r2.status_code == 201
    assert r2.headers.get("x-idempotent-replay") is None
    assert harness.side_effects["acme"] == 1

    # 成功后的重放仍走缓存，不产生第二次副作用
    r3 = _post(harness)
    assert r3.status_code == 201
    assert r3.headers.get("x-idempotent-replay") == "true"
    assert harness.side_effects["acme"] == 1


def test_retry_after_downstream_exception(harness):
    harness.controls["flaky_raise"] = True
    r1 = _post(harness)
    # 治理链路内部异常按状态不可用降级（deny 模式 503），异常细节不外透
    assert r1.status_code == 503
    assert "exploded" not in r1.text

    harness.controls["flaky_raise"] = False
    r2 = _post(harness)
    log.info("retry after exception -> %s", r2.status_code)
    assert r2.status_code == 201
    assert harness.side_effects["acme"] == 1


def test_request_during_degradation_not_stuck(harness):
    """降级期间（allow_with_degradation）不写幂等记录；恢复后同标识请求正常推进。"""
    harness.config_store.update({"state_failure_mode": "allow_with_degradation"})
    harness.state_backend.set_failure(RuntimeError("state down"))

    r1 = _post(harness)
    assert r1.status_code == 201
    assert r1.headers.get("x-governance") == "degraded"
    assert harness.side_effects["acme"] == 1
    # 降级期间未占用幂等标识
    assert harness.idempotency_store.size() == 0

    # 状态恢复后：同标识请求作为新请求正常推进（降级期间的副作用未被记录，语义明确）
    harness.state_backend.set_failure(None)
    r2 = _post(harness)
    assert r2.status_code == 201
    assert r2.headers.get("x-idempotent-replay") is None
    assert harness.side_effects["acme"] == 2

    # 恢复后记录重新生效：第三次同标识请求命中重放
    r3 = _post(harness)
    assert r3.headers.get("x-idempotent-replay") == "true"
    assert harness.side_effects["acme"] == 2


def test_conflict_still_rejected_after_failure(harness):
    """失败后释放记录，但同标识不同请求体仍是非法复用。"""
    harness.controls["flaky_fail"] = True
    assert _post(harness, key="k2", payload={"a": 1}).status_code == 500
    harness.controls["flaky_fail"] = False
    # 记录已释放：不同请求体按新请求处理（指纹随记录一起消失）
    r = _post(harness, key="k2", payload={"a": 2})
    assert r.status_code == 201


def test_successful_record_still_conflicts(harness):
    """回归：成功落缓存的记录，同标识不同请求体依旧 409。"""
    assert _post(harness, key="k3", payload={"a": 1}).status_code == 201
    r = _post(harness, key="k3", payload={"a": 2})
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "idempotency_conflict"
