"""限流（速率）与配额（累计用量）：语义可区分、恢复时间确定、并发不超发。"""
import logging
import threading

from app.limits import DAY_SECONDS, QuotaTracker, RateLimiter

log = logging.getLogger("governance-tests")


def _set_limits(harness, per_minute: int, per_day: int):
    harness.config_store.update({"rate_limit_per_minute": per_minute, "quota_per_day": per_day})


def test_rate_limit_rejects_burst_and_recovers_deterministically(harness):
    _set_limits(harness, per_minute=3, per_day=1000)
    h = harness.headers("acme")
    for i in range(3):
        r = harness.client.get("/", headers=h)
        log.info("burst req %d -> %d", i, r.status_code)
        assert r.status_code == 200
    r = harness.client.get("/", headers=h)
    assert r.status_code == 429
    assert r.json()["error"]["code"] == "rate_limited"
    retry_after = float(r.headers["retry-after"])
    log.info("rate limited, retry_after=%s", retry_after)
    assert retry_after == 20  # 3/分钟 -> 每 20 秒恢复 1 个令牌，与并发无关

    harness.clock.advance(19.9)
    assert harness.client.get("/", headers=h).status_code == 429
    harness.clock.advance(0.2)
    assert harness.client.get("/", headers=h).status_code == 200


def test_quota_counts_cumulative_usage_and_resets_on_window(harness):
    _set_limits(harness, per_minute=1000, per_day=3)
    h = harness.headers("acme")
    for i in range(3):
        assert harness.client.get("/", headers=h).status_code == 200, i
    r = harness.client.get("/", headers=h)
    log.info("quota exceeded -> %d %s retry_after=%s", r.status_code, r.json(), r.headers.get("retry-after"))
    assert r.status_code == 429
    assert r.json()["error"]["code"] == "quota_exceeded"  # 与 rate_limited 可区分
    assert float(r.headers["retry-after"]) == DAY_SECONDS - harness.clock.now

    # 窗口对齐日界：跨过窗口即恢复
    harness.clock.now = DAY_SECONDS + 1
    assert harness.client.get("/", headers=h).status_code == 200


def test_rate_limit_failure_does_not_consume_quota(harness):
    _set_limits(harness, per_minute=1, per_day=5)
    h = harness.headers("acme")
    assert harness.client.get("/", headers=h).status_code == 200
    used_before = harness.quota_tracker.usage("acme", harness.clock.now)
    assert harness.client.get("/", headers=h).status_code == 429  # rate limited
    used_after = harness.quota_tracker.usage("acme", harness.clock.now)
    log.info("quota before=%d after=%d (rate-limited request must not consume quota)", used_before, used_after)
    assert used_before == used_after == 1


def test_tenants_do_not_share_limits(harness):
    _set_limits(harness, per_minute=1, per_day=2)
    assert harness.client.get("/", headers=harness.headers("acme")).status_code == 200
    assert harness.client.get("/", headers=harness.headers("acme")).status_code == 429
    # 另一租户不受影响
    assert harness.client.get("/", headers=harness.headers("globex")).status_code == 200


def test_component_concurrency_no_overspend_no_double_deduct():
    quota = QuotaTracker()
    rate = RateLimiter()
    allowed = {"quota": 0, "rate": 0}
    lock = threading.Lock()

    def worker():
        for _ in range(1000):
            d = quota.consume("t", 250, now=5000.0)
            r = rate.check("t", 6000, now=5000.0)  # 容量 6000
            with lock:
                allowed["quota"] += 1 if d.allowed else 0
                allowed["rate"] += 1 if r.allowed else 0

    threads = [threading.Thread(target=worker) for _ in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    log.info("concurrent allowed=%s (total=10000, quota cap=250, rate cap=6000)", allowed)
    assert allowed["quota"] == 250  # 不超发
    assert allowed["rate"] == 6000
    assert quota.usage("t", now=5000.0) == 250  # 不漏扣不重复扣


def test_memory_bounded_under_many_tenants():
    quota = QuotaTracker(max_entries=100)
    rate = RateLimiter(max_tenants=100)
    for i in range(5000):
        quota.consume(f"tenant-{i}", 10, now=5000.0)
        rate.check(f"tenant-{i}", 10, now=5000.0)
    log.info("quota entries=%d rate buckets=%d", len(quota._counts), len(rate._buckets))
    assert len(quota._counts) <= 100
    assert len(rate._buckets) <= 100
    # 大量租户访问后判定仍正确
    assert quota.consume("fresh", 1, now=5000.0).allowed
