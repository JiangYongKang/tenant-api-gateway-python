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
| `bad_signature` | 签名校验失败（含凭据换发后的旧凭据） |
| `tenant_suspended` | 租户被临时停用（HTTP 401） |
| `tenant_draining` | 租户处于平滑下线过渡态（HTTP 403，与凭据无效/已停用都不同） |
| `tenant_disabled` | 租户已停用（终态） |

不同租户的计量、幂等记录与审计完全隔离，互不可见、互不影响。

## 租户在线生命周期

通过 `POST /admin/tenants` 在线维护租户（不必重启），请求体为
`{"operations": [...]}` 的批量操作，**整体校验、原子生效**：任一操作非法或互相冲突，
整批返回 HTTP 422 `invalid_tenant_batch`，注册表完全不变，绝不静默退回旧值或部分应用。

### 批量变更的整体成功 / 整体失败语义

一批操作是一个不可分割的事务，调用方只能观察到两种结果之一：

- **整体成功**（HTTP 200，`applied: true`）：批次中每一条都生效，返回每条结果
  （`provision`/`rotate_credential` 含新凭据）；
- **整体失败**（HTTP 422，`invalid_tenant_batch`）：**零改动、零痕迹**，如同该批次从未提交。

实现上，`TenantRegistry.apply_batch` 在注册表锁内先把整批（含前置校验、容量回收判定、
状态机预演）跑在一份**暂存副本**上；任何一条目产生错误（包括只有在"应用时"才能确定的
失败，例如**租户数量已达 `max_tenants` 容量上限且无可回收终态租户、新增租户开不出来**）
都直接丢弃暂存副本并拒绝整批，真实注册表（租户清单、单个租户状态、凭据世代、到期时刻）
在此之前绝不被触碰；全部条目通过后才以一次引用替换原子提交。因此：

- 批次中排在前面的 `rotate_credential`（换发）不会因为后面某条失败而"提前生效"：
  整批被拒时旧凭据**当场仍然有效**，不会出现用户拿旧凭据访问被 `bad_signature` 拒绝；
- 不会新建半成品租户，也不会残留被占用的容量/状态；
- 租户清单、单个租户详情与凭据在拒绝后与提交前**逐项一致**，成功后才是完整的新状态；
- 同一批次中重复针对同一租户、`drain_seconds` 越界或类型错误、未知操作、非法租户名、
  空批次、状态机不允许的迁移等，同样整批拒绝且零改动。

### 并发冲突批次

`apply_batch` 全程持有注册表锁（校验与提交是同一个临界区），多个互相冲突的批次并发
提交时按全序串行判定：**最后只有一个批次整体生效**，其余批次看到获胜批次提交后的状态，
因前置条件不再成立（状态已变、租户已存在、容量已被占满等）而**整批 422 拒绝且不留任何
痕迹**——不会在拒绝后残留半成品改动（如被多推进的凭据世代），也不会留下被占住的状态。

生命周期状态机：

```
provision → ACTIVE --suspend--> SUSPENDED --resume--> ACTIVE
           ACTIVE/SUSPENDED --decommission(drain_seconds)--> DRAINING --过渡期满--> DISABLED(终态)
```

| 状态 | 含义 | 对新请求 | 在途请求 |
|---|---|---|---|
| `active` | 正常服务 | 正常治理 | 正常 |
| `suspended` | 临时停用，可 `resume` 恢复 | 401 `tenant_suspended` | 已通过的请求正常收尾 |
| `draining` | 平滑下线过渡态 | 403 `tenant_draining`（明确区别于凭据无效与已停用） | 正常收尾 |
| `disabled` | 终态（过渡期满自动迁移） | 401 `tenant_disabled` | — |

操作类型：`provision`（开通，响应含新凭据）、`rotate_credential`（换发：凭据世代 +1，
旧凭据立即以 `bad_signature` 失效，响应含新凭据）、`suspend`、`resume`、
`decommission`（`drain_seconds` 后可真正停用，0 表示立即）。

拒绝条件（均 422 且整批不生效、零改动）：租户已存在/不存在、状态机不允许的迁移、
同一租户在一批中出现多次（互相冲突）、`drain_seconds` 越界（0~7 天）或类型错误、
未知操作、非法租户名、空批次、**注册表已满（达到 `max_tenants` 容量上限）且回收终态
租户后仍无空间**——容量判定在提交前的暂存校验阶段完成，因此即使超限的开通排在批次
末尾，也不会让前面的换发/停用等操作部分生效。

一致性：单请求在入口完成一次身份判定后持有该结论快照，变更不改写在途请求；
`authenticate` 在锁内原子完成 存在性+签名+状态 判定，并发变更下结论不自相矛盾。

有界可回收：注册表容量有上限（`max_tenants`），满员时优先回收终态
（含过渡期满的 DRAINING）租户，无回收空间则明确拒绝新开通。

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

## 租户差异化策略

通过 `PUT /admin/policies/{tenant_id}` 为单个租户覆盖治理参数，
`DELETE /admin/policies/{tenant_id}` 移除覆盖，`GET /admin/policies` 查看清单。

- 可覆盖字段：`rate_limit_per_minute`、`quota_per_day`、`idempotency_ttl_seconds`；
  `state_failure_mode` 是全局开关，**不可**按租户覆盖。
- 优先级（唯一且可解释）：**逐字段合并 —— 租户覆盖值 > 全局默认值**；
  未覆盖的字段继续跟随全局配置（含全局配置的后续调整）。作用范围仅限该租户。
- 生效时机：写入成功后立即影响该租户的后续请求；单请求内使用同一份生效快照。
- 拒绝条件（HTTP 422 `invalid_policy`，原策略原样保留）：未知或不可覆盖字段、
  类型错误（含 bool 冒充整数）、越界取值（边界与全局配置一致）、空覆盖、
  覆盖条目数超上限；对不存在的租户返回 404 `unknown_tenant`。

## 租户用量视图

`GET /admin/usage/{tenant_id}`（只读，不消耗任何额度）：

| 字段 | 含义 |
|---|---|
| `rate_limit.limit_per_minute` | 生效的速率上限（含租户覆盖） |
| `rate_limit.remaining` | 当前剩余速率额度（令牌数，向下保留两位小数） |
| `rate_limit.retry_after_seconds` | 额度耗尽时预计恢复秒数；未耗尽为 `null` |
| `quota.limit_per_day` / `used` / `remaining` | 本周期累计额度上限 / 已用 / 剩余 |
| `quota.reset_at` | 当前配额窗口重置时刻（Unix 秒） |
| `idempotency.records` | 该租户当前占用（未过期）的重放记录数 |
| `idempotency.ttl_seconds` | 生效的重放记录有效期 |
| `policy.overridden_fields` | 该租户生效的覆盖项；空对象表示全部走全局默认 |

自洽性：各组件在各自锁内取快照，并发查询下 `used ≤ limit`、
`used + remaining = limit` 恒成立；视图只含该租户聚合数字，
**不含凭据明文与其他租户信息**。

降级表现：治理状态读不到或读取超时（与中间件同一判定路径与超时预算）时，
返回 HTTP 503 `state_unavailable` —— 明确、可观测、可用
`StateBackend.set_failure()/set_latency()` 本地复现；内部异常细节不外透。
租户不存在返回 404 `unknown_tenant`。

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
- `tests/test_audit.py`：四类结局可检索、贯穿标识、无敏感信息、HTTP 级并发幂等；
- `tests/test_tenant_admin.py`：租户在线生命周期、**批量整体成功/整体失败（含容量超限
  零改动、凭据不残留）、并发冲突批次只有一个整体生效**、有界回收；
- `tests/test_policies.py`：差异策略优先级、非法拒绝并保留原策略、只影响本租户；
- `tests/test_usage.py`：用量视图字段、并发自洽、降级可观测、无凭据/跨租户泄露；
- `tests/test_idempotency_retry.py`：下游失败/降级后同标识重试可重新推进；
- `tests/test_open_entries.py`：开放入口无凭据可访问、响应形态不回归。

### 本地复现"容量超限导致整批拒绝、状态与凭据完全不变"

把注册表容量上限调小即可稳定复现（无需外部依赖）。`tests/conftest.py` 的
`build_harness(max_tenants=..., seeds=...)` 支持以小容量装配：

- `small_harness`（容量 2，两个 ACTIVE 种子、无可回收终态）：任何新增开通都触发容量
  上限。提交 `[换发 acme, 开通 newco]` 时整批 422，断言租户清单/详情/凭据签名逐项
  不变、旧凭据当场仍可用（`test_capacity_rejection_leaves_credentials_and_state_untouched`）；
- 不经过 HTTP 的确定性复现见 `test_registry_capacity_rejection_is_all_or_nothing`：
  `TenantRegistry(secret=b"s", max_tenants=2)` 加两个租户后提交"换发 + 超限开通"。

并发冲突批次见 `test_concurrent_identical_multiop_batches_exactly_one_applies`
（同一多操作批次并发，只有一个整体生效）与
`test_concurrent_capacity_race_losing_batches_leave_no_credential_trace`
（容量 2：1 ACTIVE + 1 可回收 DISABLED，多批"换发 + 唯一开通"竞争唯一空位；
被拒批次不推进凭据世代，世代恰好为 1）。

也可以直接用一段脚本观察修复前后差异：

```python
from app.tenancy import TenantRegistry, TenantAdminError, TenantResolver

reg = TenantRegistry(secret=b"s", max_tenants=2)
reg.add("aa"); reg.add("bb")
old_cred = reg.issue("aa")
try:
    reg.apply_batch([
        {"op": "rotate_credential", "tenant_id": "aa"},  # 排在前面
        {"op": "provision", "tenant_id": "cc"},          # 容量超限，整批开不出来
    ])
except TenantAdminError as e:
    print("rejected:", e.errors)
# 正确行为：整批拒绝后 old_cred 仍然有效（而非 bad_signature）
TenantResolver(reg).resolve({"x-tenant-credential": old_cred})
```

可重复运行以确认并发用例稳定（无调度依赖）：

```bash
uv run pytest tests/test_tenant_admin.py -q -k "concurrent or capacity"
for i in $(seq 1 20); do uv run pytest tests/test_tenant_admin.py -q -k concurrent; done
```

## 目录结构

```
main.py            # 装配与业务/管理接口
app/tenancy.py     # 租户身份解析、校验与在线生命周期（原子批量变更、有界注册表）
app/config.py      # 治理配置：强校验、原子生效
app/policies.py    # 租户差异化策略（覆盖全局默认）
app/usage.py       # 租户用量视图（只读快照、降级可观测）
app/admin.py       # 治理管理接口（/admin/tenants、/admin/policies、/admin/usage）
app/limits.py      # 限流（令牌桶）与配额（固定窗口），含只读快照
app/idempotency.py # 幂等记录存储（下游失败可放弃记录，重试不被占住）
app/audit.py       # 审计日志
app/state.py       # 治理运行状态后端（故障/延迟注入）
app/middleware.py  # 治理编排中间件
tests/             # 自动化测试
```
