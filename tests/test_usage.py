"""用量视图：字段含义、并发自洽、降级可观测、不泄露凭据与其他租户。"""
import logging
import threading

log = logging.getLogger("governance-tests")


def _usage(harness, tenant_id="acme"):
    r = harness.client.get(f"/admin/usage/{tenant_id}")
    log.info("usage %s -> %d %s", tenant_id, r.status_code, r.json())
    return r


def test_usage_view_reflects_consumption(harness):
    harness.config_store.update({"rate_limit_per_minute": 10, "quota_per_day": 100})
    for _ in range(3):
        assert harness.client.get("/", headers=harness.headers("acme")).status_code == 200
    h = harness.headers("acme", **{"Idempotency-Key": "k1"})
    assert harness.client.post("/orders", headers=h, json={"i": 1}).status_code == 201

    r = _usage(harness)
    assert r.status_code == 200
    body = r.json()
    assert body["governance"] == "ok"
    # 配额：4 个受治理请求（3 GET + 1 POST）
    assert body["quota"]["used"] == 4
    assert body["quota"]["limit_per_day"] == 100
    assert body["quota"]["remaining"] == 96
    assert body["quota"]["used"] + body["quota"]["remaining"] == body["quota"]["limit_per_day"]
    assert body["quota"]["reset_at"] > harness.clock.now  # 重置时刻在未来
    # 限流：剩余额度减少，未耗尽时 retry_after 为 None
    assert body["rate_limit"]["limit_per_minute"] == 10
    assert 0 <= body["rate_limit"]["remaining"] < 10
    assert body["rate_limit"]["retry_after_seconds"] is None
    # 重放记录占用
    assert body["idempotency"]["records"] == 1
    assert body["idempotency"]["ttl_seconds"] == 86400.0
    assert body["policy"]["overridden_fields"] == {}


def test_usage_view_shows_retry_after_when_rate_exhausted(harness):
    harness.config_store.update({"rate_limit_per_minute": 2})
    assert harness.client.get("/", headers=harness.headers("acme")).status_code == 200
    assert harness.client.get("/", headers=harness.headers("acme")).status_code == 200
    assert harness.client.get("/", headers=harness.headers("acme")).status_code == 429
    body = _usage(harness).json()
    log.info("exhausted rate view: %s", body["rate_limit"])
    assert body["rate_limit"]["remaining"] < 1
    assert body["rate_limit"]["retry_after_seconds"] > 0  # 大概多久恢复


def test_usage_view_reflects_tenant_override(harness):
    harness.client.put("/admin/policies/acme", json={"quota_per_day": 7})
    body = _usage(harness).json()
    assert body["quota"]["limit_per_day"] == 7  # 生效的是租户覆盖值
    assert body["policy"]["overridden_fields"] == {"quota_per_day": 7}


def test_usage_view_is_read_only(harness):
    before = _usage(harness).json()
    _usage(harness)
    after = _usage(harness).json()
    log.info("before=%s after=%s", before["quota"], after["quota"])
    assert before["quota"] == after["quota"]  # 查询本身不消耗额度


def test_usage_view_self_consistent_under_concurrency(harness):
    harness.config_store.update({"rate_limit_per_minute": 100000, "quota_per_day": 1000})
    errors = []

    def hammer():
        for _ in range(20):
            harness.client.get("/", headers=harness.headers("acme"))

    def watcher():
        for _ in range(20):
            body = _usage(harness).json()
            q, rl = body["quota"], body["rate_limit"]
            # 自洽性：已用不超上限、已用+剩余=上限、剩余不为负
            if not (0 <= q["used"] <= q["limit_per_day"]):
                errors.append(f"quota used out of range: {q}")
            if q["used"] + q["remaining"] != q["limit_per_day"]:
                errors.append(f"quota inconsistent: {q}")
            if not (0 <= rl["remaining"] <= rl["limit_per_minute"]):
                errors.append(f"rate remaining out of range: {rl}")

    threads = [threading.Thread(target=hammer) for _ in range(4)] + [threading.Thread(target=watcher) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    log.info("consistency errors: %s", errors)
    assert errors == []
    final = _usage(harness).json()
    assert final["quota"]["used"] == 80  # 与计量组件最终一致


def test_usage_view_degraded_on_state_failure(harness):
    harness.state_backend.set_failure(RuntimeError("etcd down"))
    r = _usage(harness)
    log.info("degraded usage -> %d %s", r.status_code, r.json())
    assert r.status_code == 503
    assert r.json()["error"]["code"] == "state_unavailable"  # 明确、可观测
    assert "etcd down" not in r.text and "Traceback" not in r.text  # 内部异常不外透
    # 恢复后立即可用
    harness.state_backend.set_failure(None)
    assert _usage(harness).status_code == 200


def test_usage_view_degraded_on_slow_state(harness):
    harness.state_backend.set_latency(0.2)  # 超过 0.05s 预算
    r = _usage(harness)
    assert r.status_code == 503
    assert r.json()["error"]["code"] == "state_unavailable"


def test_usage_view_no_credential_no_cross_tenant_leak(harness):
    cred = harness.cred("acme")
    harness.client.get("/", headers=harness.headers("acme"))
    harness.client.get("/", headers=harness.headers("globex"))
    r = _usage(harness, "acme")
    assert r.status_code == 200
    # 不带出凭据明文与其他租户标识
    assert cred not in r.text and "tnt_" not in r.text
    assert "globex" not in r.text
    assert r.json()["tenant_id"] == "acme"
    assert r.json()["quota"]["used"] == 1  # 只含本租户的用量


def test_usage_view_unknown_tenant(harness):
    r = _usage(harness, "ghost")
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "unknown_tenant"
