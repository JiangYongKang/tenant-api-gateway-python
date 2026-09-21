"""治理编排：把租户校验、计量、幂等、审计与降级策略串成一次请求判定。

判定顺序（确定、可解释）：
1. 租户凭据校验 —— 失败即拒绝，原因可区分；
2. 幂等检查（仅携带幂等标识的副作用请求）—— 重放直接返回首个结果；
3. 限流 + 配额计量 —— 超限拒绝并给出 Retry-After。

降级约定：
- 治理依赖的运行状态抛出 GovernanceError（或任何未预期异常）时，
  按当前配置快照的 degraded_mode 处理：
  - fail_closed: 一律以 503 + reason=degraded 拒绝（默认，绝不静默放行）；
  - fail_open_limited: 放行但响应与审计都打上 degraded 标记（可观测，非静默）。
- 内部异常细节只进审计与日志，绝不出现在对调用方的响应体中。
"""
from __future__ import annotations

import hashlib
import logging
import time
from dataclasses import dataclass

from gateway.audit import AuditLog
from gateway.config import ConfigRegistry
from gateway.errors import RejectReason
from gateway.idempotency import IdempotencyRecord, IdempotencyStore, IdempotencyVerdict
from gateway.limiter import TenantMeterRegistry
from gateway.tenancy import CredentialStatus, TenantAuthenticator

logger = logging.getLogger("gateway.governance")

_CREDENTIAL_REJECT: dict[CredentialStatus, RejectReason] = {
    CredentialStatus.MISSING: RejectReason.CREDENTIAL_MISSING,
    CredentialStatus.MALFORMED: RejectReason.CREDENTIAL_MALFORMED,
    CredentialStatus.EXPIRED_FORMAT: RejectReason.CREDENTIAL_EXPIRED_FORMAT,
    CredentialStatus.EXPIRED: RejectReason.CREDENTIAL_INVALID,
    CredentialStatus.INVALID_SIGNATURE: RejectReason.CREDENTIAL_INVALID,
    CredentialStatus.UNKNOWN_TENANT: RejectReason.CREDENTIAL_INVALID,
    CredentialStatus.TENANT_DISABLED: RejectReason.TENANT_DISABLED,
}


@dataclass
class Decision:
    """一次请求的治理判定结果；allowed=True 时调用方执行业务逻辑后须 complete/fail。"""

    request_id: str
    allowed: bool
    status_code: int
    reason: RejectReason | None = None
    tenant_id: str | None = None
    retry_after_seconds: float | None = None
    degraded: bool = False
    replay_record: IdempotencyRecord | None = None
    # 内部句柄（不进入响应体）
    idempotency_key: str | None = None
    _started_at: float = 0.0
    _method: str = ""
    _path: str = ""


class GovernancePipeline:
    """对单个请求执行完整治理判定；内部异常一律转为可观测的降级行为。"""

    def __init__(
        self,
        authenticator: TenantAuthenticator,
        config: ConfigRegistry,
        meters: TenantMeterRegistry,
        idempotency: IdempotencyStore,
        audit: AuditLog,
    ) -> None:
        self._auth = authenticator
        self._config = config
        self._meters = meters
        self._idem = idempotency
        self._audit = audit

    # ------------------------------------------------------------------ API

    def inspect(
        self,
        *,
        method: str,
        path: str,
        credential_header: str | None,
        idempotency_key: str | None,
        body: bytes,
        now: float | None = None,
    ) -> Decision:
        """执行治理判定。now 可注入以保证测试可复现。"""
        now = time.monotonic() if now is None else now
        decision = Decision(
            request_id=AuditLog.new_request_id(),
            allowed=False,
            status_code=500,
            _started_at=now,
            _method=method,
            _path=path,
        )
        try:
            return self._inspect_inner(
                decision, method, path, credential_header, idempotency_key, body, now
            )
        except Exception as exc:  # 治理依赖失效：绝不把内部异常透出给调用方
            return self._degrade(decision, exc, now)

    def complete(self, decision: Decision, status_code: int, response_body: str) -> None:
        """业务处理成功后调用：落幂等结果 + 审计。"""
        if decision.idempotency_key and decision.tenant_id:
            try:
                self._idem.complete(
                    decision.tenant_id, decision.idempotency_key, status_code, response_body
                )
            except Exception as exc:  # 幂等结果落盘失败不翻转已完成的业务响应
                logger.exception("idempotency complete failed: %s", exc)
        self._record(decision, "allowed", None, status_code)

    def fail(self, decision: Decision, status_code: int = 500) -> None:
        """业务处理失败后调用：释放幂等占位（不留半更新状态）+ 审计。"""
        if decision.idempotency_key and decision.tenant_id:
            try:
                self._idem.abort(decision.tenant_id, decision.idempotency_key)
            except Exception as exc:
                logger.exception("idempotency abort failed: %s", exc)
        self._record(decision, "rejected", "handler_error", status_code)

    # -------------------------------------------------------------- 内部步骤

    def _inspect_inner(
        self,
        decision: Decision,
        method: str,
        path: str,
        credential_header: str | None,
        idempotency_key: str | None,
        body: bytes,
        now: float,
    ) -> Decision:
        config = self._config.get()  # 本次判定自始至终使用同一份快照

        # 1. 租户凭据
        status, identity = self._auth.authenticate(credential_header)
        if identity is None:
            reason = _CREDENTIAL_REJECT[status]
            return self._reject(decision, reason, self._status_for(reason), now)
        decision.tenant_id = identity.tenant_id

        # 2. 幂等（仅副作用请求携带标识时）
        if idempotency_key:
            fingerprint = hashlib.sha256(
                method.encode() + b"\n" + path.encode() + b"\n" + body
            ).hexdigest()
            verdict = self._idem.begin(
                identity.tenant_id, idempotency_key, fingerprint, now,
                config.idempotency_ttl_seconds,
            )
            if verdict == IdempotencyVerdict.CONFLICT:
                return self._reject(decision, RejectReason.IDEMPOTENCY_CONFLICT, 409, now)
            if verdict == IdempotencyVerdict.IN_PROGRESS:
                return self._reject(decision, RejectReason.IDEMPOTENCY_IN_PROGRESS, 409, now)
            if verdict == IdempotencyVerdict.REPLAY:
                record = self._idem.lookup(identity.tenant_id, idempotency_key, now)
                decision.replay_record = record
                decision.status_code = record.response_status if record else 200
                self._record(decision, "replayed", None, decision.status_code)
                return decision
            decision.idempotency_key = idempotency_key  # NEW：占位成功

        # 3. 限流 + 配额
        meter = self._meters.check_and_consume(
            identity.tenant_id,
            now=now,
            rate_per_second=config.rate_limit_per_second,
            burst=config.rate_burst,
            quota_limit=config.quota_limit,
            quota_window_seconds=config.quota_window_seconds,
        )
        if not meter.allowed:
            # 请求未被执行：释放幂等占位，避免半更新状态阻塞合法重试
            if decision.idempotency_key:
                self._idem.abort(identity.tenant_id, decision.idempotency_key)
                decision.idempotency_key = None
            reason = (
                RejectReason.QUOTA_EXCEEDED if meter.quota_exceeded else RejectReason.RATE_LIMITED
            )
            rejected = self._reject(decision, reason, 429, now)
            rejected.retry_after_seconds = meter.retry_after_seconds
            return rejected

        decision.allowed = True
        decision.status_code = 200
        return decision

    # -------------------------------------------------------------- 辅助

    def _reject(
        self, decision: Decision, reason: RejectReason, status_code: int, now: float
    ) -> Decision:
        decision.allowed = False
        decision.reason = reason
        decision.status_code = status_code
        self._record(decision, "rejected", reason.value, status_code)
        return decision

    def _degrade(self, decision: Decision, exc: Exception, now: float) -> Decision:
        logger.exception("governance degraded: %s", exc)
        try:
            mode = self._config.get().degraded_mode
        except Exception:
            mode = "fail_closed"
        decision.degraded = True
        if mode == "fail_open_limited":
            decision.allowed = True
            decision.status_code = 200
            self._record(decision, "degraded", f"fail_open_limited:{type(exc).__name__}", 200)
        else:
            decision.allowed = False
            decision.reason = RejectReason.DEGRADED
            decision.status_code = 503
            self._record(decision, "degraded", f"fail_closed:{type(exc).__name__}", 503)
        return decision

    def _record(
        self, decision: Decision, outcome: str, reason: str | None, status_code: int
    ) -> None:
        from gateway.audit import AuditEvent

        try:
            self._audit.record(
                AuditEvent(
                    request_id=decision.request_id,
                    tenant_id=decision.tenant_id,
                    method=decision._method,
                    path=decision._path,
                    outcome=outcome,
                    reason=reason,
                    status_code=status_code,
                    latency_ms=0.0,
                )
            )
        except Exception:
            logger.exception("audit record failed")  # 审计失败不影响已作出的判定

    @staticmethod
    def _status_for(reason: RejectReason) -> int:
        if reason == RejectReason.TENANT_DISABLED:
            return 403
        return 401
