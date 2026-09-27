"""治理中间件：编排租户校验、限流、配额、幂等、审计与降级。

每个请求的判定顺序（使用同一份配置快照，保证单请求内判定一致）：
    状态可用性 -> 租户校验 -> 限流 -> 配额 -> 幂等 -> 下游处理 -> 审计

降级策略（state_failure_mode）：
    deny                  -> 状态不可读时拒绝（503 state_unavailable）
    allow_with_degradation-> 放行但跳过全部有状态计量，绝不写半更新状态

内部异常一律不外透：治理链路自身出错按"状态不可用"走降级路径。
"""
from __future__ import annotations

import hashlib
import json
import time
import uuid
from collections.abc import Awaitable, Callable

from starlette.types import ASGIApp, Message, Receive, Scope, Send

from .audit import AuditLog, AuditOutcome, AuditRecord
from .config import ConfigStore
from .idempotency import BeginResult, IdempotencyStore
from .limits import QuotaTracker, RateLimiter
from .overrides import TenantPolicyStore
from .state import StateBackend, StateUnavailableError
from .tenancy import CredentialRejectReason, TenantResolver, TenancyError

REQUEST_ID_HEADER = "x-request-id"
IDEMPOTENCY_HEADER = "idempotency-key"
SIDE_EFFECT_METHODS = {"POST", "PUT", "PATCH", "DELETE"}
DEFAULT_STATE_TIMEOUT = 0.5


class GovernanceMiddleware:
    def __init__(
        self,
        app: ASGIApp,
        *,
        resolver: TenantResolver,
        config_store: ConfigStore,
        rate_limiter: RateLimiter,
        quota_tracker: QuotaTracker,
        idempotency_store: IdempotencyStore,
        audit_log: AuditLog,
        state_backend: StateBackend,
        policy_store: TenantPolicyStore | None = None,
        clock: Callable[[], float] = time.time,
        state_timeout: float = DEFAULT_STATE_TIMEOUT,
        exempt_paths: tuple[str, ...] = ("/admin/", "/healthz"),
    ) -> None:
        self.app = app
        self.resolver = resolver
        self.config_store = config_store
        self.rate_limiter = rate_limiter
        self.quota_tracker = quota_tracker
        self.idempotency_store = idempotency_store
        self.audit_log = audit_log
        self.state_backend = state_backend
        self.policy_store = policy_store
        self.clock = clock
        self.state_timeout = state_timeout
        self.exempt_paths = exempt_paths

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        path: str = scope["path"]
        method: str = scope["method"]
        if any(path.startswith(p) for p in self.exempt_paths):
            await self.app(scope, receive, send)
            return

        headers = {k.decode("latin-1"): v.decode("latin-1") for k, v in scope["headers"]}
        request_id = headers.get(REQUEST_ID_HEADER) or uuid.uuid4().hex
        now = self.clock()
        cfg = self.config_store.get()  # 单请求单一快照

        try:
            await self._govern(scope, receive, send, request_id, headers, method, path, cfg, now)
        except Exception:
            # 治理链路内部异常不外透：按状态不可用降级
            await self._degraded(scope, receive, send, request_id, method, path, cfg, now, "internal_error")

    # ------------------------------------------------------------------
    async def _govern(self, scope, receive, send, request_id, headers, method, path, cfg, now) -> None:
        # 1. 治理运行状态可用性
        try:
            self.state_backend.check_available(timeout=self.state_timeout)
        except StateUnavailableError:
            await self._degraded(scope, receive, send, request_id, method, path, cfg, now, "state_unavailable")
            return

        # 2. 租户身份
        try:
            tenant = self.resolver.resolve(headers)
        except TenancyError as e:
            # 过渡态（draining）与"凭据无效""已停用"可区分：403 tenant_draining
            status = 403 if e.reason is CredentialRejectReason.TENANT_DRAINING else 401
            await self._reject(send, request_id, status, e.reason.value)
            self._audit(request_id, None, method, path, AuditOutcome.REJECTED, e.reason.value, status, now)
            return

        # 2.5 该租户的生效策略：专属覆盖逐字段覆盖全局默认，仅作用于本租户
        policy = (
            self.policy_store.effective_for(tenant.tenant_id, cfg)
            if self.policy_store is not None
            else None
        )
        rate_per_minute = policy.rate_limit_per_minute if policy else cfg.rate_limit_per_minute
        quota_per_day = policy.quota_per_day if policy else cfg.quota_per_day
        idem_ttl = policy.idempotency_ttl_seconds if policy else cfg.idempotency_ttl_seconds

        # 3. 限流（速率）
        rate = self.rate_limiter.check(tenant.tenant_id, rate_per_minute, now)
        if not rate.allowed:
            await self._reject(send, request_id, 429, "rate_limited", retry_after=rate.retry_after_seconds)
            self._audit(request_id, tenant.tenant_id, method, path, AuditOutcome.REJECTED, "rate_limited", 429, now)
            return

        # 4. 配额（累计用量）；限流未通过时不会走到这里，不会误扣
        quota = self.quota_tracker.consume(tenant.tenant_id, quota_per_day, now)
        if not quota.allowed:
            await self._reject(send, request_id, 429, "quota_exceeded", retry_after=quota.retry_after_seconds)
            self._audit(request_id, tenant.tenant_id, method, path, AuditOutcome.REJECTED, "quota_exceeded", 429, now)
            return

        # 5. 幂等（仅副作用方法且携带标识）
        idem_key = headers.get(IDEMPOTENCY_HEADER)
        idem_pending: tuple[str, str] | None = None
        body = b""
        if method in SIDE_EFFECT_METHODS and idem_key:
            body, receive = await self._buffer_body(receive)
            fingerprint = hashlib.sha256(method.encode() + b"|" + path.encode() + b"|" + body).hexdigest()
            outcome = self.idempotency_store.begin(tenant.tenant_id, idem_key, fingerprint, idem_ttl, now)
            if outcome.result is BeginResult.CONFLICT:
                await self._reject(send, request_id, 409, "idempotency_conflict")
                self._audit(request_id, tenant.tenant_id, method, path, AuditOutcome.REJECTED, "idempotency_conflict", 409, now)
                return
            if outcome.result is BeginResult.IN_PROGRESS:
                await self._reject(send, request_id, 409, "idempotency_in_progress")
                self._audit(request_id, tenant.tenant_id, method, path, AuditOutcome.REJECTED, "idempotency_in_progress", 409, now)
                return
            if outcome.result is BeginResult.REPLAY:
                status, cached = outcome.cached_response
                await self._send_raw(send, status, cached, request_id, replayed=True)
                self._audit(request_id, tenant.tenant_id, method, path, AuditOutcome.REPLAYED, "idempotent_replay", status, now)
                return
            # NEW：包装 send 以捕获响应，完成后落缓存
            idem_pending = (tenant.tenant_id, idem_key)
            send = self._capturing_send(send, tenant.tenant_id, idem_key, now)

        # 6. 下游处理（注入贯穿标识）；下游异常时释放处理中幂等记录，合法重试可重新推进
        try:
            await self.app(scope, receive, self._request_id_send(send, request_id))
        except Exception:
            if idem_pending is not None:
                self.idempotency_store.abort(*idem_pending)
            raise
        self._audit(request_id, tenant.tenant_id, method, path, AuditOutcome.PROCESSED, None, 200, now)

    def _request_id_send(self, send: Send, request_id: str) -> Send:
        async def wrapped(message: Message) -> None:
            if message["type"] == "http.response.start":
                message = dict(message)
                headers = list(message.get("headers", []))
                headers.append((b"x-request-id", request_id.encode()))
                message["headers"] = headers
            await send(message)

        return wrapped

    # ------------------------------------------------------------------
    async def _degraded(self, scope, receive, send, request_id, method, path, cfg, now, reason: str) -> None:
        if cfg.state_failure_mode == "allow_with_degradation":
            # 放行但跳过全部有状态计量：不读不写，绝不留下半更新状态
            async def degraded_send(message: Message) -> None:
                if message["type"] == "http.response.start":
                    message = dict(message)
                    headers = list(message.get("headers", []))
                    headers.append((b"x-governance", b"degraded"))
                    headers.append((b"x-request-id", request_id.encode()))
                    message["headers"] = headers
                await send(message)

            await self.app(scope, receive, degraded_send)
            self._audit(request_id, None, method, path, AuditOutcome.DEGRADED, reason, 200, now)
        else:
            await self._reject(send, request_id, 503, reason)
            self._audit(request_id, None, method, path, AuditOutcome.DEGRADED, reason, 503, now)

    # ------------------------------------------------------------------
    async def _buffer_body(self, receive: Receive) -> tuple[bytes, Receive]:
        body = b""
        while True:
            message = await receive()
            if message["type"] != "http.request":
                continue
            body += message.get("body", b"")
            if not message.get("more_body"):
                break

        async def replay() -> Message:
            return {"type": "http.request", "body": body, "more_body": False}

        return body, replay

    def _capturing_send(self, send: Send, tenant_id: str, key: str, now: float) -> Send:
        captured: dict[str, object] = {"status": None, "chunks": []}

        async def wrapped(message: Message) -> None:
            if message["type"] == "http.response.start":
                captured["status"] = message["status"]
            elif message["type"] == "http.response.body":
                captured["chunks"].append(message.get("body", b""))
                if not message.get("more_body"):
                    status = int(captured["status"] or 500)
                    if status < 500:
                        self.idempotency_store.complete(tenant_id, key, status, b"".join(captured["chunks"]), self.clock())
                    else:
                        # 下游 5xx 不落缓存：释放记录，同标识合法重试可重新推进
                        self.idempotency_store.abort(tenant_id, key)
            await send(message)

        return wrapped

    async def _reject(self, send: Send, request_id: str, status: int, reason: str, retry_after: float | None = None) -> None:
        payload = json.dumps({"error": {"code": reason, "request_id": request_id}}).encode()
        headers = [(b"content-type", b"application/json"), (b"x-request-id", request_id.encode())]
        if retry_after is not None:
            headers.append((b"retry-after", str(max(0, int(retry_after))).encode()))
        await self._send_raw(send, status, payload, request_id, extra=headers)

    async def _send_raw(self, send: Send, status: int, body: bytes, request_id: str, replayed: bool = False, extra: list | None = None) -> None:
        headers = list(extra or [(b"content-type", b"application/json"), (b"x-request-id", request_id.encode())])
        if replayed:
            headers.append((b"x-idempotent-replay", b"true"))
        await send({"type": "http.response.start", "status": status, "headers": headers})
        await send({"type": "http.response.body", "body": body})

    def _audit(self, request_id, tenant_id, method, path, outcome, reason, status, now) -> None:
        self.audit_log.record(
            AuditRecord(
                request_id=request_id,
                tenant_id=tenant_id,
                path=path,
                method=method,
                outcome=outcome,
                reason=reason,
                status_code=status,
                timestamp=now,
            )
        )
