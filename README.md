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
- 拒绝原因可区分（除注明外均为 HTTP 401，`error.code`）：

| code | 含义 |
|---|---|
| `missing_credential` | 未携带凭据 |
| `malformed_credential` | 格式非法 |
| `legacy_format` | 历史形态 `tenant-<id>`，确定性拒绝（兼容策略：明确拒绝而非猜测） |
| `unknown_tenant` | 租户不存在 |
| `bad_signature` | 签名校验失败（含凭据换发后的旧凭据） |
| `tenant_draining` | 租户处于平滑下线过渡态（HTTP 403，与凭据无效/已停用可区分） |
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

## 租户在线生命周期

通过 `POST /admin/tenants` 批量调整（`{"operations": [...]}`），即时生效、原子应用：
任一操作非法或互相冲突，整批拒绝（HTTP 422 `invalid_lifecycle`），注册表完全不变，
绝不悄悄退回旧值。进行中的请求在调整前已解析身份，按原快照收尾，结论不自相矛盾。

状态机与迁移规则：

| 操作 | 前置状态 | 结果状态 | 说明 |
|---|---|---|---|
| `provision` | （不存在） | `active` | 开通新租户，立即可用 |
| `rotate` | `active` / `draining` | 不变 | 换发凭据：密钥代数 +1，旧凭据立即失效（`bad_signature`） |
| `disable` | `active` | `disabled` | 临时停用，新请求 401 `tenant_disabled` |
| `enable` | `disabled` | `active` | 恢复已停用租户 |
| `drain` | `active` | `draining` | 安排平滑下线，需 `drain_seconds`（0, 7d] |
| （到期） | `draining` | `disabled` | 过渡结束自动视为停用（惰性求值，无后台任务） |

- **过渡态 `draining`**：新请求收到与"凭据无效""已停用"都不同的明确结论
  HTTP 403 `tenant_draining`；已在处理的请求正常收尾。
- 冲突示例（均整批拒绝）：同批内对同一租户多个操作、重复开通、对不存在租户做变更、
  非法迁移（如 `enable` 一个 `active` 租户、`drain` 一个 `disabled` 租户）。
- 凭据按 (主密钥, tenant_id, 密钥代数) 本地确定性派生，换发、校验完全可复现。
- 注册表有界（`max_tenants`）：超限的开通整批拒绝，长时间运行不膨胀。
- `GET /admin/tenants` 查看各租户生效状态与密钥代数。

## 租户级差异化策略

通过 `PUT /admin/tenants/{id}/policy` 设置，`DELETE` 同路径清除：

- **优先级（唯一规则）**：逐字段覆盖 —— 专属设置存在的字段覆盖全局默认，
  未设置的字段跟随全局默认（含其后续调整）；作用范围仅限该租户。
- 可覆盖字段：`rate_limit_per_minute`、`quota_per_day`、`idempotency_ttl_seconds`，
  边界与全局配置相同。
- 非法（未知项/类型错/越界/空覆盖）当场拒绝（HTTP 422 `invalid_policy`），
  原有策略原样保留；对不存在的租户设置返回 404 `unknown_tenant`。
- 生效后立即影响该租户的后续判定；存储有界（`max_tenants`）。

## 租户用量视图

`GET /admin/usage/{tenant_id}`，面向运维/支持的只读视图：

| 字段 | 含义 |
|---|---|
| `rate.limit_per_minute` | 该租户生效的速率上限（含专属覆盖） |
| `rate.remaining` | 当前令牌桶剩余额度（只读观察，不消耗） |
| `rate.retry_after_seconds` | 额度耗尽时预计恢复秒数，未耗尽为 `null` |
| `quota.limit_per_day` / `used` / `remaining` | 本周期上限 / 已用 / 剩余 |
| `quota.reset_seconds` | 距窗口重置的秒数（窗口对齐 Unix 日界） |
| `idempotency.records` | 该租户当前有效的幂等记录数 |
| `idempotency.ttl_seconds` | 该租户生效的重放记录有效期 |

- **自洽性**：所有读数取自同一时刻，各组件在各自锁内读取；
  任意并发快照都满足 `used <= limit` 且 `used + remaining == limit`。
- **降级**：治理状态不可读/响应变慢时返回 HTTP 503 `state_unavailable`
  （`status: degraded`），数值字段不给出——明确、可观测，且可用
  `StateBackend.set_failure()/set_latency()` 在本地复现；内部异常细节不外透。
- **隔离**：只含指定租户数据，不夹带其他租户信息或凭据明文；
  未知租户返回 404 `unknown_tenant`。

## 失败后的合法重试

- 下游返回 5xx 或抛异常时，处理中的幂等记录被立即释放（`abort`），
  同一 `Idempotency-Key` 的合法重试可重新推进，不会被长期占住；
  只有 2xx/4xx 的下游响应才会落缓存供重放。
- 降级期间（`allow_with_degradation`）不写幂等记录，恢复后同标识请求按新请求正常推进。

## 验证方法

```bash
uv run pytest tests -q
```

测试以异常链路与并发场景为主，日志打印判定依据与输入状态：

- `tests/test_tenancy.py`：各类凭据拒绝原因、租户隔离、本地可复现判定；
- `tests/test_limits.py`：限流/配额语义区分、恢复时间确定性、并发不超发、内存有界；
- `tests/test_idempotency.py`：重放/冲突/处理中区分、TTL 过期、并发单次副作用；
- `tests/test_idempotency_retry.py`：下游 5xx/异常/降级期间，同标识合法重试可重新推进；
- `tests/test_degradation.py`：故障与慢响应降级、两种模式、恢复后状态完整；
- `tests/test_config_api.py`：非法配置拒绝、即时生效、快照一致性；
- `tests/test_lifecycle.py`：在线开通/换发/停用/恢复/平滑下线、原子批量拒绝、结论一致性；
- `tests/test_overrides.py`：差异策略覆盖优先级、非法拒绝保留原值、租户间隔离；
- `tests/test_usage.py`：用量视图字段语义、并发自洽、降级表现、隔离与无敏感信息；
- `tests/test_regression.py`：开放入口无凭据可访问、响应形态不变、新增治理不外溢；
- `tests/test_audit.py`：四类结局可检索、贯穿标识、无敏感信息、HTTP 级并发幂等。

## 目录结构

```
main.py            # 装配与业务接口
app/tenancy.py     # 租户身份解析、校验与生命周期状态（分代密钥、过渡态）
app/lifecycle.py   # 租户在线生命周期：原子批量调整
app/config.py      # 治理配置：强校验、原子生效
app/overrides.py   # 租户级差异化策略（逐字段覆盖全局默认）
app/limits.py      # 限流（令牌桶）与配额（固定窗口）
app/idempotency.py # 幂等记录存储（失败即释放，合法重试可重推）
app/usage.py       # 租户用量视图（自洽快照、降级结论）
app/audit.py       # 审计日志
app/state.py       # 治理运行状态后端（故障/延迟注入）
app/middleware.py  # 治理编排中间件
app/admin_api.py   # /admin/* 管理接口（main 与测试共用）
tests/             # 自动化测试
```
