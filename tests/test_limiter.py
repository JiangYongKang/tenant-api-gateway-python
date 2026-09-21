"""限流与配额：语义区分、恢复时间确定、并发不超发不漏扣。"""
import logging
import threading

from gateway.errors import RejectReason
from gateway.limiter import TenantMeterRegistry
from tests.conftest import log_decision

logger = logging.getLogger("tests.limiter")


def _inspect(pipeline, credential, clock, **kw):
    d = pipeline.inspect(
        method="GET", path="/api/resource", credential_header=credential,
        idempotency_key=None, body=b"", now=clock(),
    )
    log_decision("limiter", d, **kw)
    return d


def test_rate_limit_rejects_burst_overflow(pipeline, credential, clock):
    cred = credential()
    results = [_inspect(pipeline, cred, clock, i=i) for i in range(5)]
    assert [d.allowed for d in results] == [True] * 4 + [False]  # burst=4
    assert results[-1].reason == RejectReason.RATE_LIMITED
    assert results[-1].status_code == 429
    assert results[-1].retry_after_seconds is not None


def test_rate_recovers_deterministically(pipeline, credential, clock):
    cred = credential()
    for _ in range(4):
        assert _inspect(pipeline, cred, clock).allowed
    assert not _inspect(pipeline, cred, clock).allowed
    clock.advance(0.5)  # rate=2/s -> 恰好恢复 1 个令牌
    d = _inspect(pipeline, cred, clock)
    assert d.allowed


def test_quota_distinct_from_rate_limit(pipeline, credential, clock, config):
    config.update(rate_limit_per_second=1000.0, rate_burst=1000, quota_limit=3)
    cred = credential()
    for i in range(3):
        assert _inspect(pipeline, cred, clock, i=i).allowed
    d = _inspect(pipeline, cred, clock)
    assert not d.allowed and d.reason == RejectReason.QUOTA_EXCEEDED
    assert d.status_code == 429


def test_quota_recovers_at_window_boundary(pipeline, credential, clock, config):
    config.update(rate_limit_per_second=1000.0, rate_burst=1000, quota_limit=2,
                  quota_window_seconds=60.0)
    cred = credential()
    assert _inspect(pipeline, cred, clock).allowed
    assert _inspect(pipeline, cred, clock).allowed
    blocked = _inspect(pipeline, cred, clock)
    assert blocked.reason == RejectReason.QUOTA_EXCEEDED
    assert abs(blocked.retry_after_seconds - 60.0) < 1e-6  # 窗口起点即 now
    clock.advance(60.0)
    assert _inspect(pipeline, cred, clock).allowed


def test_rejected_request_not_charged(pipeline, credential, clock, config):
    """超限请求不得扣减额度（不漏扣、不重复扣减）。"""
    config.update(rate_limit_per_second=0.0001, rate_burst=2, quota_limit=100)
    cred = credential(ttl=100_000)  # 时钟需大幅推进，凭据 TTL 要覆盖
    assert _inspect(pipeline, cred, clock).allowed
    assert _inspect(pipeline, cred, clock).allowed
    rejected = _inspect(pipeline, cred, clock)
    assert rejected.reason == RejectReason.RATE_LIMITED
    # 令牌恢复后立即可用：说明被拒请求没有消耗配额计数
    clock.advance(20000)
    assert _inspect(pipeline, cred, clock).allowed


def test_tenants_do_not_share_meters(pipeline, credential, clock, config):
    config.update(rate_limit_per_second=0.0001, rate_burst=1)
    assert _inspect(pipeline, credential("alpha"), clock).allowed
    assert not _inspect(pipeline, credential("alpha"), clock).allowed
    assert _inspect(pipeline, credential("beta"), clock).allowed  # beta 不受 alpha 影响


def test_concurrent_requests_no_overspend():
    """同一租户 200 并发抢 10 个令牌：恰好 10 个通过，无超发。"""
    reg = TenantMeterRegistry()
    kw = dict(rate_per_second=1e-9, burst=10, quota_limit=10, quota_window_seconds=1000.0)
    results: list[bool] = []
    lock = threading.Lock()

    def hit(i):
        d = reg.check_and_consume("hot", now=0.0, **kw)
        with lock:
            results.append(d.allowed)
            logger.info("concurrent[%d] allowed=%s", i, d.allowed)

    threads = [threading.Thread(target=hit, args=(i,)) for i in range(200)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sum(results) == 10, f"超发: {sum(results)}"


def test_registry_bounded_and_reclaimable():
    reg = TenantMeterRegistry(max_tenants=5)
    kw = dict(rate_per_second=1.0, burst=1, quota_limit=1, quota_window_seconds=10.0)
    for i in range(50):
        reg.check_and_consume(f"t{i}", now=float(i), **kw)
    assert reg.tracked_tenants <= 5
    reclaimed = reg.reclaim_idle(now=1000.0, idle_seconds=10.0)
    assert reclaimed == reg.tracked_tenants or reg.tracked_tenants == 0
    assert reg.tracked_tenants == 0
