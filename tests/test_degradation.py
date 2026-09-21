"""降级：状态不可读/变慢时的行为必须明确、可观测、不泄漏内部异常、不留半更新状态。"""
import logging
import threading
import time

from gateway.config import ConfigRegistry, GovernanceConfig
from gateway.errors import RejectReason, StateStoreError
from gateway.governance import GovernancePipeline
from gateway.idempotency import IdempotencyStore
from gateway.limiter import TenantMeterRegistry
from gateway.tenancy import TenantAuthenticator
from tests.conftest import log_decision

logger = logging.getLogger("tests.degradation")


class BrokenMeters(TenantMeterRegistry):
    """模拟计量存储不可读。"""

    def check_and_consume(self, *args, **kwargs):
        raise StateStoreError("meter store unreachable")


class SlowMeters(TenantMeterRegistry):
    """模拟计量存储响应变慢（超过调用方可接受的延迟后抛错）。"""

    def __init__(self, delay: float) -> None:
        super().__init__()
        self._delay = delay

    def check_and_consume(self, *args, **kwargs):
        time.sleep(self._delay)
        raise StateStoreError("meter store timeout")


def _make_pipeline(tenants, clock, audit, meters, degraded_mode="fail_closed"):
    config = ConfigRegistry(GovernanceConfig(degraded_mode=degraded_mode))
    return GovernancePipeline(
        authenticator=TenantAuthenticator(tenants, clock=clock),
        config=config,
        meters=meters,
        idempotency=IdempotencyStore(),
        audit=audit,
    )


def _inspect(pipe, credential, clock):
    d = pipe.inspect(
        method="GET", path="/api/resource", credential_header=credential,
        idempotency_key=None, body=b"", now=clock(),
    )
    log_decision("degradation", d)
    return d


def test_fail_closed_rejects_with_503(tenants, clock, audit, credential):
    pipe = _make_pipeline(tenants, clock, audit, BrokenMeters())
    d = _inspect(pipe, credential(), clock)
    assert not d.allowed and d.degraded
    assert d.reason == RejectReason.DEGRADED and d.status_code == 503


def test_degraded_response_does_not_leak_internals(tenants, clock, audit, credential):
    pipe = _make_pipeline(tenants, clock, audit, BrokenMeters())
    d = _inspect(pipe, credential(), clock)
    # 判定结果中不含内部异常细节（异常类型只进审计）
    assert "store" not in (d.reason.value if d.reason else "")
    assert "unreachable" not in str(d.reason)


def test_degradation_is_audited(tenants, clock, audit, credential):
    pipe = _make_pipeline(tenants, clock, audit, BrokenMeters())
    d = _inspect(pipe, credential(), clock)
    event = audit.find_by_request_id(d.request_id)
    assert event is not None and event.outcome == "degraded"
    assert "StateStoreError" in (event.reason or "")  # 内部细节留在审计，可检索
    logger.info("degraded audit event: %s", event)


def test_fail_open_limited_is_observable(tenants, clock, audit, credential):
    """fail_open_limited：放行但必须带降级标记，不是静默放行。"""
    pipe = _make_pipeline(tenants, clock, audit, BrokenMeters(), degraded_mode="fail_open_limited")
    d = _inspect(pipe, credential(), clock)
    assert d.allowed and d.degraded  # 放行但可观测
    event = audit.find_by_request_id(d.request_id)
    assert event.outcome == "degraded"


def test_slow_store_triggers_degradation(tenants, clock, audit, credential):
    pipe = _make_pipeline(tenants, clock, audit, SlowMeters(delay=0.05))
    start = time.monotonic()
    d = _inspect(pipe, credential(), clock)
    elapsed = time.monotonic() - start
    assert d.degraded and d.status_code == 503
    assert elapsed >= 0.05
    logger.info("slow store degraded after %.3fs", elapsed)


def test_unexpected_exception_also_degrades(tenants, clock, audit, credential):
    """未预期异常同样不得透出给调用方。"""

    class WildMeters(TenantMeterRegistry):
        def check_and_consume(self, *a, **k):
            raise RuntimeError("totally unexpected")

    pipe = _make_pipeline(tenants, clock, audit, WildMeters())
    d = _inspect(pipe, credential(), clock)
    assert d.degraded and d.status_code == 503 and d.reason == RejectReason.DEGRADED


def test_no_half_updated_state_after_degradation(tenants, clock, audit, credential):
    """降级路径不得在幂等存储中留下占位。"""
    idem = IdempotencyStore()
    config = ConfigRegistry(GovernanceConfig())
    pipe = GovernancePipeline(
        authenticator=TenantAuthenticator(tenants, clock=clock),
        config=config,
        meters=BrokenMeters(),
        idempotency=idem,
        audit=audit,
    )
    d = pipe.inspect(
        method="POST", path="/api/resource", credential_header=credential(),
        idempotency_key="k-deg", body=b"{}", now=clock(),
    )
    assert d.degraded
    assert idem.lookup("alpha", "k-deg", clock()) is None  # 无半更新状态
    logger.info("idempotency store clean after degradation, size=%d", idem.size)


def test_concurrent_degradation_consistent(tenants, clock, audit, credential):
    """并发下所有请求得到一致的降级结论。"""
    pipe = _make_pipeline(tenants, clock, audit, BrokenMeters())
    cred = credential()
    results = []
    lock = threading.Lock()

    def hit():
        d = _inspect(pipe, cred, clock)
        with lock:
            results.append((d.degraded, d.status_code))

    threads = [threading.Thread(target=hit) for _ in range(50)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert all(r == (True, 503) for r in results), set(results)
