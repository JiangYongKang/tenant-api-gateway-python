"""审计：每类结局都有可检索记录；贯穿标识一致；不含敏感信息。"""
import logging
import threading

from app.audit import AuditOutcome

log = logging.getLogger("governance-tests")


def test_every_outcome_leaves_searchable_record(harness):
    harness.config_store.update({"rate_limit_per_minute": 100, "quota_per_day": 100})
    # processed
    harness.client.get("/", headers=harness.headers("acme"))
    # rejected (tenant)
    harness.client.get("/")
    # rejected (rate limit)
    harness.config_store.update({"rate_limit_per_minute": 1})
    harness.client.get("/", headers=harness.headers("globex"))
    harness.client.get("/", headers=harness.headers("globex"))
    harness.config_store.update({"rate_limit_per_minute": 100})  # 恢复，避免干扰后续步骤
    # replayed
    h = harness.headers("acme", **{"Idempotency-Key": "aud-1"})
    harness.client.post("/orders", headers=h, json={"a": 1})
    harness.client.post("/orders", headers=h, json={"a": 1})
    # degraded
    harness.state_backend.set_failure(RuntimeError("down"))
    harness.client.get("/", headers=harness.headers("acme"))
    harness.state_backend.set_failure(None)

    for outcome in (AuditOutcome.PROCESSED, AuditOutcome.REJECTED, AuditOutcome.REPLAYED, AuditOutcome.DEGRADED):
        records = harness.audit_log.query(outcome=outcome)
        log.info("outcome=%s records=%d reasons=%s", outcome.value, len(records), {r.reason for r in records})
        assert len(records) >= 1, outcome


def test_request_id_threads_through_response_and_audit(harness):
    r = harness.client.get("/", headers=harness.headers("acme", **{"x-request-id": "req-abc-123"}))
    assert r.headers["x-request-id"] == "req-abc-123"
    records = harness.audit_log.query(request_id="req-abc-123")
    assert len(records) == 1
    assert records[0].outcome is AuditOutcome.PROCESSED
    # 拒绝路径同样贯穿
    r2 = harness.client.get("/", headers={"x-request-id": "req-rej-1"})
    assert r2.headers["x-request-id"] == "req-rej-1"
    assert harness.audit_log.query(request_id="req-rej-1")[0].outcome is AuditOutcome.REJECTED


def test_audit_contains_no_credential_material(harness):
    cred = harness.cred("acme")
    harness.client.get("/", headers={"x-tenant-credential": cred})
    harness.client.get("/", headers={"x-tenant-credential": "tnt_acme.0000000000000000"})
    for record in harness.audit_log.query():
        blob = f"{record.request_id}|{record.tenant_id}|{record.reason}|{record.path}"
        assert cred not in blob
        assert "tnt_" not in blob
    log.info("audit records verified free of credential material, count=%d", len(harness.audit_log))


def test_concurrent_same_idempotency_key_single_side_effect(harness):
    # HTTP 级并发：同一幂等标识并发提交，副作用最多发生一次
    harness.config_store.update({"rate_limit_per_minute": 10_000, "quota_per_day": 10_000})
    h = harness.headers("acme", **{"Idempotency-Key": "race-1"})
    results = []

    def worker():
        r = harness.client.post("/orders", headers=h, json={"item": "race"})
        results.append((r.status_code, r.headers.get("x-idempotent-replay")))

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    log.info("concurrent results=%s side_effects=%s", results, harness.side_effects)
    assert harness.side_effects["acme"] == 1  # 不产生第二次副作用
    for status, _ in results:
        assert status in (201, 409)  # 重放或处理中冲突，绝不重复执行
