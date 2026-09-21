# tenant-api-gateway-python

面向多租户的 FastAPI 请求治理网关：同一份部署在租户隔离、额度控制、重复提交与内部故障等场景下给出一致且可解释的结果。

## 快速开始

```bash
uv sync
uv run uvicorn main:app --reload
uv run pytest tests -q        # 运行全部自动化测试
```

## 租户识别

- 请求头：`X-Tenant-Credential: tnt_<tenant_id>.<sig>`，
  其中 `sig = HMAC-SHA256(tenant_id, secret)` 的前 16 个十六进制字符。
- 判定完全本地完成（`app/tenancy.py` 的 `TenantRegistry`），不依赖外部服务或真实凭据；
  测试中可用 `registry.issue(tenant_id)` 确定性签发。
- 拒绝原因可区分（HTTP 401，`error.code`）：

| code | 含义 |
|---|---|
| `missing_credential` | 未携带凭据 |
| `malformed_credential` | 格式非法 |
| `legacy_format` | 历史形态 `tenant-<id>`，确定性拒绝（兼容策略：明确拒绝而非猜测） |
| `unknown_tenant` | 租户不存在 |
| `bad_signature` | 签名校验失败 |
| `tenant_disabled` | 租户已停用 |

不同租户的计量、幂等记录与审计完全隔离，互不可见、互不影响。

## 限流与配额语义

两者语义不同，超限标识可区分（均为 HTTP 429，带 `Retry-After`）：

- **限流（`rate_limited`）**：每租户令牌桶，约束短时间请求速率。
  容量 = `rate_limit_per_minute`，恢复速率 = 配额/60 每秒；
  恢复时间由令牌速率推导，确定且与并发无关。
- **配额（quota_exceeded）**：每租户固定日窗口计数（窗口对齐 Unix 日界），
  约束一段时间内的累计用量；跨窗口自动恢复，恢复时刻确定。
- 判定顺序为 限流 → 配额：被限流的请求不会误扣配额。
- 并发安全：所有计量在锁内完成，同租户并发请求不超发、不漏扣、不重复扣减。

## 幂等（重复提交保护）

- 对副作用方法（POST/PUT/PATCH/DELETE）且携带 `Idempotency-Key` 的请求生效。
- 指纹 = SHA256(method | path | body)，记录按 `(tenant, key)` 隔离，TTL = `idempotency_ttl_seconds`。
- 三种情形可区分：
  - 合法重放：返回缓存响应，响应头 `X-Idempotent-Replay: true`，**不产生第二次副作用**；
  - 标识冲突（同 key 不同请求体）：HTTP 409 `idempotency_conflict`；
  - 处理中：HTTP 409 `idempotency_in_progress`。
- 记录有界（`max_entries`）并按 TTL 惰性回收，内存不随运行时间漂移。

## 审计与贯穿标识

- 每个请求生成/透传 `X-Request-ID`（响应头与审计记录一致）。
- 被处理（`processed`）、被拒绝（`rejected`）、被降级（`degraded`）、被重放（`replayed`）
  的请求都会落审计记录，可通过 `GET /admin/audit?tenant_id=&outcome=` 检索。
- 审计记录只含判定依据与元信息，**不含凭据原文**（写入侧有防御性断言）。

## 降级策略

治理中间件在每个请求前读取治理运行状态（`app/state.py`），读取失败或超过
超时预算（默认 0.5s）即触发降级，行为由 `state_failure_mode` 决定：

- `deny`（默认）：拒绝请求，HTTP 503 `state_unavailable`；绝不静默放行。
- `allow_with_degradation`：放行但**跳过全部有状态计量**（不读不写，不留半更新状态），
  响应头 `X-Governance: degraded` 可观测。

内部异常一律不外透：治理链路自身出错按状态不可用走降级路径，响应中不含异常细节。
`StateBackend.set_failure()/set_latency()` 可在本地复现故障与慢响应（见测试）。

## 配置项

通过 `POST /admin/config` 调整，即时生效（单请求持有同一快照，进行中请求判定不自相矛盾）：

| 配置项 | 默认 | 说明 |
|---|---|---|
| `rate_limit_per_minute` | 60 | 每租户每分钟请求速率上限 |
| `quota_per_day` | 10000 | 每租户每日累计用量上限 |
| `idempotency_ttl_seconds` | 86400 | 幂等记录有效期 |
| `state_failure_mode` | `deny` | 降级策略：`deny` / `allow_with_degradation` |

非法配置（未知项、错误类型、越界取值）返回 HTTP 422 `invalid_config`，
旧配置原样保留，绝不静默回退默认值。

## 验证方法

```bash
uv run pytest tests -q
```

测试以异常链路与并发场景为主，日志打印判定依据与输入状态：

- `tests/test_tenancy.py`：各类凭据拒绝原因、租户隔离、本地可复现判定；
- `tests/test_limits.py`：限流/配额语义区分、恢复时间确定性、并发不超发、内存有界；
- `tests/test_idempotency.py`：重放/冲突/处理中区分、TTL 过期、并发单次副作用；
- `tests/test_degradation.py`：故障与慢响应降级、两种模式、恢复后状态完整；
- `tests/test_config_api.py`：非法配置拒绝、即时生效、快照一致性；
- `tests/test_audit.py`：四类结局可检索、贯穿标识、无敏感信息、HTTP 级并发幂等。

## 目录结构

```
main.py            # 装配与业务/管理接口
app/tenancy.py     # 租户身份解析与校验
app/config.py      # 治理配置：强校验、原子生效
app/limits.py      # 限流（令牌桶）与配额（固定窗口）
app/idempotency.py # 幂等记录存储
app/audit.py       # 审计日志
app/state.py       # 治理运行状态后端（故障/延迟注入）
app/middleware.py  # 治理编排中间件
tests/             # 自动化测试
```
