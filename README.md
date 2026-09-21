# tenant-api-gateway-python

在单份 FastAPI 部署上实现多租户请求治理：租户识别、限流与配额、幂等提交保护、
请求级审计、可观测降级与配置热更新。所有判定完全本地可复现，不依赖外部服务。

## 运行与验证

```bash
uv sync
uv run uvicorn main:app --reload     # 启动服务
uv run pytest                        # 运行全部自动化测试（54 个，含异常链路与并发场景）
```

测试日志默认打印每次判定的输入状态与判定依据，例如：

```
[tests] [limiter] inputs={'i': 4} -> allowed=False status=429 reason=rate_limited retry_after=0.5
```

## 租户识别

受治理接口（`/api/*`）要求请求携带租户凭据，两个入口等价、结论一致：

- `X-Tenant-Credential: <token>`
- `Authorization: <token>` 或 `Authorization: Bearer <token>`

凭据为本地签发、本地校验的 HMAC 令牌（不依赖外部服务或真实凭据）：

- **v2（当前）**：`tnt2.<tenant_id>.<expires_at>.<hmac16>`，
  `hmac16 = HMAC-SHA256(tenant_secret, "tnt2.<tenant_id>.<expires_at>")[:16]`
- **v1（历史形态）**：`tnt1.<tenant_id>.<hmac16>`，已过期，**一律拒绝**，
  原因 `credential_expired_format`，与其他拒绝明确区分。

本地签发（测试/演示用）：

```python
from gateway.tenancy import TenantRecord, issue_credential
token = issue_credential(TenantRecord("tenant-alpha", "alpha-local-secret"), time.time() + 3600)
```

拒绝原因可区分（响应体 `error.reason`，HTTP 状态码配合）：

| reason | 状态码 | 含义 |
| --- | --- | --- |
| `credential_missing` | 401 | 未携带凭据 |
| `credential_malformed` | 401 | 格式无法解析 |
| `credential_expired_format` | 401 | 历史形态（v1），确定拒绝 |
| `credential_invalid` | 401 | 签名无效 / 租户不存在 / 凭据过期 |
| `tenant_disabled` | 403 | 租户被停用 |
| `rate_limited` / `quota_exceeded` | 429 | 限流 / 配额（带 `Retry-After` 头） |
| `idempotency_conflict` / `idempotency_in_progress` | 409 | 标识冲突 / 同标识处理中 |
| `degraded` | 503 | 治理依赖失效（fail-closed） |

演示租户见 `main.DEFAULT_TENANTS`（`tenant-alpha` / `tenant-beta` / `tenant-disabled`）。
不同租户的计量、幂等记录与业务数据完全隔离。

## 限流与配额语义

两者语义不同，可同时配置、独立判定：

- **限流（速率）**：每租户令牌桶（`rate_limit_per_second` + `rate_burst`），
  约束短时间内的请求速率；恢复时间 = 攒出下一枚令牌的等待时长。
- **配额（累计用量）**：每租户固定窗口计数（`quota_limit` + `quota_window_seconds`），
  约束窗口内累计请求数；恢复时间 = 当前窗口的剩余时长，窗口到期整体重置。

关键性质（均有测试覆盖）：

- 判定与扣减在同一锁内完成：并发下不超发、不漏扣、不重复扣减；
- 被拒绝的请求不消耗任何额度；
- 恢复时间只依赖时钟，与并发无关，可用假时钟确定性复现；
- 计量状态容量有界（`max_tracked_tenants`），空闲租户按期回收，内存不漂移。

## 幂等提交保护

副作用请求（`POST /api/resource`）可携带 `X-Idempotency-Key`：

- 同一 `(tenant, key)` 在 `idempotency_ttl_seconds` 内重复提交**不产生第二次副作用**；
- 载荷指纹（方法+路径+请求体的 SHA-256）一致 → **合法重放**，返回首个结果
  （响应头 `x-idempotent-replay: true`）；
- 同标识不同载荷 → **非法复用**，409 `idempotency_conflict`；
- 同标识正在处理中 → 409 `idempotency_in_progress`；
- 处理失败或被限流时占位会被释放，不留半更新状态，合法重试不被阻塞；
- 记录有 TTL 与容量上限（`max_idempotency_keys`），过期惰性/定期回收。

## 审计与贯穿标识

每个请求（无论被处理、被拒绝、被重放还是被降级）都会落一条审计记录，
携带 `request_id`（同时出现在响应头 `x-request-id` 与错误响应体中）：

```bash
curl localhost:8000/governance/audit/<request_id>
```

`AuditEvent` 的字段集合中不存在凭据位，从结构上保证不记录凭据原文；
日志容量有界（环形淘汰）。内部异常细节只进审计与日志，绝不出现在响应体中。

## 降级策略

治理依赖的运行状态不可读或响应变慢（抛出 `StateStoreError` 或任何异常）时，
按 `degraded_mode` 处理，行为明确且可观测：

- `fail_closed`（默认）：一律 503 + `reason=degraded` 拒绝，绝不静默放行；
- `fail_open_limited`：放行但响应头 `x-governance-degraded: true`，
  审计 outcome 为 `degraded`，非静默；
- 降级前释放已占用的幂等记录，不留半更新状态、不泄漏资源；
- 内部异常绝不透出给调用方。

## 配置项与热更新

```bash
curl localhost:8000/governance/config                 # 查看当前配置
curl -X POST localhost:8000/governance/config \       # 热更新，即时生效
  -H 'content-type: application/json' -d '{"rate_burst": 5}'
```

| 配置项 | 默认 | 含义 |
| --- | --- | --- |
| `rate_limit_per_second` | 10.0 | 令牌桶补充速率（次/秒） |
| `rate_burst` | 20 | 令牌桶容量（突发上限） |
| `quota_limit` | 1000 | 配额窗口内最大请求数 |
| `quota_window_seconds` | 3600 | 配额窗口长度（秒） |
| `idempotency_ttl_seconds` | 86400 | 幂等记录有效期（秒） |
| `max_tracked_tenants` | 10000 | 计量状态容量上限 |
| `max_idempotency_keys` | 100000 | 幂等记录容量上限 |
| `degraded_mode` | `fail_closed` | 降级策略 |

非法或不兼容的更新以 422 `invalid_config` **明确拒绝**，旧配置原样保留，
绝不静默回退默认值。配置以不可变快照整体原子替换：进行中的请求自始至终
使用同一份快照，判定不自相矛盾。

## 既有接口兼容性

`GET /` 保持原响应 `{"Hello": "World"}`，不经过治理；治理只作用于 `/api/*`。
历史凭据形态（v1）与 `Bearer` 前缀均有确定行为（前者拒绝、后者兼容），
同一类请求在不同入口下结论一致（见 `tests/test_http.py`）。

## 测试结构

```
tests/
  test_tenancy.py       凭据缺失/失效/停用/过期格式，原因可区分
  test_limiter.py       限流 vs 配额语义、恢复时间、并发不超发、容量回收
  test_idempotency.py   重放/冲突/处理中区分、并发单副作用、占位释放
  test_degradation.py   状态不可读/变慢时的降级、不泄漏、不留半更新状态
  test_config.py        非法配置拒绝、热更新即时生效、快照一致性
  test_http.py          既有接口不变、多入口一致、审计检索、租户隔离
```
