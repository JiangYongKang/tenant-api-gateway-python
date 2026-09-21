"""幂等：合法重放、标识冲突、非法复用必须可区分；并发下只有一个请求产生副作用。"""
import logging
import threading

from gateway.errors import RejectReason
from gateway.idempotency import IdempotencyStore, IdempotencyVerdict
from tests.conftest import log_decision

logger = logging.getLogger("tests.idempotency")

BODY = b'{"item": 1}'


def _post(pipeline, credential, clock, key, body=BODY):
    d = pipeline.inspect(
        method="POST", path="/api/resource", credential_header=credential,
        idempotency_key=key, body=body, now=clock(),
    )
    log_decision("idempotency", d, key=key, body=body)
    return d


def test_replay_returns_first_result_without_second_side_effect(pipeline, credential, clock):
    cred = credential()
    d1 = _post(pipeline, cred, clock, "k1")
    assert d1.allowed
    pipeline.complete(d1, 201, '{"id": 1}')

    d2 = _post(pipeline, cred, clock, "k1")
    assert d2.replay_record is not None
    assert d2.replay_record.response_status == 201
    assert d2.replay_record.response_body == '{"id": 1}'
    logger.info("replay: 重放返回首个结果，未产生第二次副作用")


def test_same_key_different_payload_is_conflict(pipeline, credential, clock):
    cred = credential()
    d1 = _post(pipeline, cred, clock, "k2")
    pipeline.complete(d1, 201, '{"id": 2}')
    d2 = _post(pipeline, cred, clock, "k2", body=b'{"item": 999}')
    assert not d2.allowed
    assert d2.reason == RejectReason.IDEMPOTENCY_CONFLICT and d2.status_code == 409


def test_in_progress_distinguished(pipeline, credential, clock):
    cred = credential()
    d1 = _post(pipeline, cred, clock, "k3")
    assert d1.allowed  # 尚未 complete
    d2 = _post(pipeline, cred, clock, "k3")
    assert not d2.allowed
    assert d2.reason == RejectReason.IDEMPOTENCY_IN_PROGRESS and d2.status_code == 409


def test_key_expires_and_can_be_reused(pipeline, credential, clock, config):
    cred = credential(ttl=1000)
    d1 = _post(pipeline, cred, clock, "k4")
    pipeline.complete(d1, 201, '{"id": 4}')
    clock.advance(301)  # 超过 idempotency_ttl_seconds=300
    d2 = _post(pipeline, cred, clock, "k4")
    assert d2.allowed and d2.replay_record is None  # 过期后视为新提交


def test_failed_handler_releases_placeholder(pipeline, credential, clock):
    """处理失败必须释放占位，后续合法重试不被半更新状态阻塞。"""
    cred = credential()
    d1 = _post(pipeline, cred, clock, "k5")
    assert d1.allowed
    pipeline.fail(d1, 500)
    d2 = _post(pipeline, cred, clock, "k5")
    assert d2.allowed and d2.replay_record is None


def test_rejected_by_rate_limit_releases_placeholder(pipeline, credential, clock, config):
    """被限流的请求不得留下幂等占位。"""
    config.update(rate_limit_per_second=0.0001, rate_burst=1)
    cred = credential(ttl=100_000)
    d1 = _post(pipeline, cred, clock, "k6")
    assert d1.allowed
    pipeline.complete(d1, 201, '{"id": 6}')
    d2 = _post(pipeline, cred, clock, "k7")  # 被限流
    assert d2.reason == RejectReason.RATE_LIMITED
    clock.advance(20000)  # 令牌恢复
    d3 = _post(pipeline, cred, clock, "k7")
    assert d3.allowed and d3.replay_record is None  # k7 未被占位，可正常提交


def test_keys_isolated_between_tenants(pipeline, credential, clock):
    d1 = _post(pipeline, credential("alpha"), clock, "shared-key")
    pipeline.complete(d1, 201, '{"id": "a"}')
    d2 = _post(pipeline, credential("beta"), clock, "shared-key")
    assert d2.allowed and d2.replay_record is None  # 同 key 不同租户互不影响


def test_concurrent_same_key_single_side_effect():
    """100 并发同标识同时提交：恰好一个 NEW，其余 IN_PROGRESS。"""
    store = IdempotencyStore()
    verdicts: list[IdempotencyVerdict] = []
    lock = threading.Lock()

    def race(i):
        v = store.begin("t", "k", "fp", now=0.0, ttl=60.0)
        with lock:
            verdicts.append(v)
            logger.info("concurrent[%d] verdict=%s", i, v.value)

    threads = [threading.Thread(target=race, args=(i,)) for i in range(100)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert verdicts.count(IdempotencyVerdict.NEW) == 1
    assert verdicts.count(IdempotencyVerdict.IN_PROGRESS) == 99


def test_store_bounded_and_swept():
    store = IdempotencyStore(max_keys=10)
    for i in range(100):
        store.begin("t", f"k{i}", "fp", now=float(i), ttl=5.0)
    assert store.size <= 10
    n = store.sweep(now=1000.0)
    assert store.size == 0 and n >= 0
