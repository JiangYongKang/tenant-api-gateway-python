"""幂等：合法重放、标识冲突、非法复用可区分；重复提交不产生第二次副作用。"""
import logging
import threading

from app.idempotency import BeginResult, IdempotencyStore

log = logging.getLogger("governance-tests")


def test_replay_returns_cached_response_without_second_side_effect(harness):
    h = harness.headers("acme", **{"Idempotency-Key": "order-1"})
    r1 = harness.client.post("/orders", headers=h, json={"item": "book"})
    r2 = harness.client.post("/orders", headers=h, json={"item": "book"})
    log.info("first=%d %s replay=%d %s side_effects=%s", r1.status_code, r1.json(), r2.status_code, r2.json(), harness.side_effects)
    assert r1.status_code == 201
    assert r2.status_code == 201
    assert r2.headers["x-idempotent-replay"] == "true"
    assert r1.json() == r2.json()
    assert harness.side_effects["acme"] == 1  # 无第二次副作用


def test_same_key_different_body_is_conflict(harness):
    h = harness.headers("acme", **{"Idempotency-Key": "order-2"})
    assert harness.client.post("/orders", headers=h, json={"item": "a"}).status_code == 201
    r = harness.client.post("/orders", headers=h, json={"item": "b"})
    log.info("conflict -> %d %s", r.status_code, r.json())
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "idempotency_conflict"
    assert harness.side_effects["acme"] == 1


def test_key_scoped_per_tenant(harness):
    # 同一标识不同租户：互不影响，各自产生自己的副作用
    h1 = harness.headers("acme", **{"Idempotency-Key": "shared-key"})
    h2 = harness.headers("globex", **{"Idempotency-Key": "shared-key"})
    assert harness.client.post("/orders", headers=h1, json={"item": "x"}).status_code == 201
    assert harness.client.post("/orders", headers=h2, json={"item": "x"}).status_code == 201
    assert harness.side_effects == {"acme": 1, "globex": 1}


def test_key_expires_after_ttl(harness):
    harness.config_store.update({"idempotency_ttl_seconds": 60})
    h = harness.headers("acme", **{"Idempotency-Key": "order-3"})
    assert harness.client.post("/orders", headers=h, json={"item": "a"}).status_code == 201
    harness.clock.advance(61)
    r = harness.client.post("/orders", headers=h, json={"item": "a"})
    log.info("after ttl -> %d replay_header=%s", r.status_code, r.headers.get("x-idempotent-replay"))
    assert r.status_code == 201
    assert "x-idempotent-replay" not in r.headers  # 过期后视为新请求
    assert harness.side_effects["acme"] == 2


def test_no_key_no_protection(harness):
    h = harness.headers("acme")
    assert harness.client.post("/orders", headers=h, json={"item": "a"}).status_code == 201
    assert harness.client.post("/orders", headers=h, json={"item": "a"}).status_code == 201
    assert harness.side_effects["acme"] == 2


def test_get_requests_ignore_idempotency_key(harness):
    h = harness.headers("acme", **{"Idempotency-Key": "k"})
    assert harness.client.get("/", headers=h).status_code == 200
    assert harness.client.get("/", headers=h).status_code == 200


def test_component_in_progress_and_concurrent_single_execution():
    store = IdempotencyStore()
    outcomes = []
    lock = threading.Lock()

    def worker():
        o = store.begin("t", "k", "fp", ttl=100, now=0.0)
        with lock:
            outcomes.append(o.result)

    threads = [threading.Thread(target=worker) for _ in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    log.info("concurrent begin outcomes: NEW=%d IN_PROGRESS=%d", outcomes.count(BeginResult.NEW), outcomes.count(BeginResult.IN_PROGRESS))
    assert outcomes.count(BeginResult.NEW) == 1  # 只有一个请求获准执行副作用
    assert outcomes.count(BeginResult.IN_PROGRESS) == 19


def test_store_bounded_and_reclaims():
    store = IdempotencyStore(max_entries=50)
    for i in range(500):
        store.begin("t", f"k{i}", "fp", ttl=1000, now=float(i))
    log.info("store size after 500 inserts (max=50): %d", store.size())
    assert store.size() <= 50
    # TTL 到期全部回收
    store.begin("t", "trigger", "fp", ttl=1, now=5000.0)
    assert store.size() == 1
