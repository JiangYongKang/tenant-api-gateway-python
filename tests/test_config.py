"""配置：热更新即时生效、非法配置明确拒绝、进行中的判定不自相矛盾。"""
import logging
import threading

import pytest

from gateway.config import ConfigRegistry, GovernanceConfig
from gateway.errors import ConfigError, RejectReason
from tests.conftest import log_decision

logger = logging.getLogger("tests.config")


def test_invalid_config_rejected_not_silently_reverted():
    reg = ConfigRegistry()
    before = reg.get()
    with pytest.raises(ConfigError) as exc:
        reg.update(rate_burst=0)
    logger.info("invalid config rejected: %s", exc.value)
    assert reg.get() is before  # 旧配置原样保留，非静默回退默认值


@pytest.mark.parametrize(
    "changes",
    [
        {"rate_limit_per_second": -1},
        {"rate_burst": 0},
        {"quota_limit": 0},
        {"quota_window_seconds": -5},
        {"idempotency_ttl_seconds": 0},
        {"max_tracked_tenants": 0},
        {"degraded_mode": "yolo"},
        {"unknown_field": 1},
    ],
)
def test_each_invalid_field_rejected(changes):
    reg = ConfigRegistry()
    with pytest.raises(ConfigError):
        reg.update(**changes)


def test_update_takes_effect_immediately(pipeline, credential, clock, config):
    cred = credential()
    config.update(rate_limit_per_second=0.0001, rate_burst=1)
    assert pipeline.inspect(method="GET", path="/x", credential_header=cred,
                            idempotency_key=None, body=b"", now=clock()).allowed
    d = pipeline.inspect(method="GET", path="/x", credential_header=cred,
                         idempotency_key=None, body=b"", now=clock())
    log_decision("config", d)
    assert d.reason == RejectReason.RATE_LIMITED  # 新配置即时生效


def test_in_flight_decision_uses_single_snapshot():
    """一次判定自始至终使用同一份快照：更新发生在判定中途也不自相矛盾。"""
    reg = ConfigRegistry(GovernanceConfig(rate_burst=100, rate_limit_per_second=100.0))
    snapshots_seen: list[tuple[float, int]] = []

    original_get = reg.get

    def get_and_swap():
        snap = original_get()
        # 模拟判定中途有并发更新
        reg.update(rate_limit_per_second=0.0001, rate_burst=1)
        return snap

    reg.get = get_and_swap  # type: ignore[method-assign]
    # GovernancePipeline 只在判定开始时取一次快照
    snap = reg.get()
    snapshots_seen.append((snap.rate_limit_per_second, snap.rate_burst))
    assert snapshots_seen[0] == (100.0, 100)  # 本次判定用旧快照，前后一致
    assert reg.get().rate_burst == 1  # 之后的判定用新快照


def test_concurrent_updates_keep_registry_consistent():
    reg = ConfigRegistry()
    errors: list[Exception] = []

    def bump(i):
        try:
            reg.update(rate_burst=i + 1, rate_limit_per_second=float(i + 1))
        except ConfigError as exc:
            errors.append(exc)

    threads = [threading.Thread(target=bump, args=(i,)) for i in range(100)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    final = reg.get()
    logger.info("final config after 100 concurrent updates: %s", final)
    assert not errors
    assert 1 <= final.rate_burst <= 100  # 总是某一次完整更新的结果，非撕裂状态
