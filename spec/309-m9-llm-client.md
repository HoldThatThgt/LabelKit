## 3.9 M9 LLM 客户端 llm-client

### 3.9.1 职责与边界

**做：**按 profile 提供统一异步调用接口：消息构造（文本/多图多模态）、provider 适配（OpenAI 兼容 / Anthropic 原生）、结构化输出参数透传、超时/重试/限流、token 与成本计量、`--probe` 连通性探测。 
**不做：**不解析业务结构（返回原始文本或原生结构化载荷，解析归 M8）；不缓存响应（无状态）；不做模型路由/降级等「智能」行为——模型与 profile 选择完全由配置决定。v1.6 注：同 profile 内的密钥池轮换（3.9.3）是传输层容错而非模型路由——池内各密钥打同一 base_url、同一 model，密钥选择不改变产出数据的任何内容语义。

### 3.9.2 API 与数据结构

```
@dataclass(frozen=True)
class Part:      kind: Literal["text", "image"]; text: str | None; image: ImageRef | None
@dataclass(frozen=True)
class Message:   role: Literal["system", "user", "assistant"]; parts: tuple[Part, ...]
@dataclass(frozen=True)
class PromptBundle: messages: tuple[Message, ...]; temperature: float | None = None
@dataclass(frozen=True)
class LLMResponse:  text: str; structured: dict | None; usage: Usage; model: str; latency_ms: int

class LLMClient:
    def __init__(self, profiles: dict[str, ProfileConfig]): ...
    async def complete(self, profile: str, prompt: PromptBundle,
                       response_schema: dict | None = None) -> LLMResponse:
        """response_schema 仅在 profile 声明 supports_structured_output 时转为 L0 参数，否则忽略。
           重试后仍失败: 抛 ProviderRetryableError / ProviderFatalError。"""
    async def embed(self, profile: str, texts: list[str]) -> list[list[float]]:
        """v1.2 新增。profile 须为 config.toml [embedding.*] 子表名（5.1），不接受 [llm.*]。
           openai_compatible：POST {base_url}/embeddings（3.9.3）。返回向量与 texts 顺序一一对应；
           profile 配置了 dims 时逐条校验返回维度，不匹配抛 ProviderFatalError。
           token 计量入 usage_by_profile（键 = embedding profile 名）；每次调用发 llm.call
           trace 事件，payload 增可选字段 operation="embedding"（事件目录只增不改，7.2）。
           重试/限流/超时规则与 complete 一致（3.9.3）。"""

    async def probe(self, profile: str) -> ProbeResult   # validate --probe：1-token 试调用（池化 profile 探测首密钥）
    async def probe_all(self, profile: str) -> list[ProbeResult]
        """v1.6：逐密钥探测——按声明序每密钥一个 ProbeResult（增 key_env 字段：所用密钥的环境变量名，
           单密钥 profile 为 None）；单密钥 profile 退化为 [await probe(profile)]。llm 与 embedding
           profile 均适用。validate --probe 使用本方法（2.4），成本 = 各被引用 profile 池大小之和 次探测调用。"""
    @property
    def usage_by_profile(self) -> dict[str, Usage]        # 报告用累计计量（v1.6：池化 profile 另含逐密钥
                                                          # calls/rate_limited/disabled 与驻留统计，6.4）

    def snapshot(self, now: float | None = None) -> tuple[ProfileSnapshot, ...]
        """v1.10（7.7 console 面板数据源，U19/U26）。纯读、无 await、无锁；仅从渲染 tick
           （事件循环线程内）调用——同线程下与并发 gather 无争用。now 注入供离线测试
           （_KeyPool 同风格）。全量枚举 [llm.*] 与 [embedding.*] profile；密钥池未物化时
           从声明构造零值 KeySnapshot（snapshot 不物化池——读操作不改状态）。"""

@dataclass(frozen=True)
class KeySnapshot:                                # v1.10
    env: str                                      # 环境变量名——唯一可展示身份（密钥值任何面均不出现）
    state: Literal["ok", "cooldown", "disabled"]
    cooldown_remaining_s: int = 0
    calls: int = 0                                # 逐密钥用量镜像（KeyUsage）——面板 'l' 展开视图
    rate_limited: int = 0                         # 数据源（7.7）；池未物化时为 0

@dataclass(frozen=True)
class ProfileSnapshot:                            # v1.10
    name: str
    kind: Literal["llm", "embedding"]             # usage 按 name 合桶的既有口径由 kind 消歧
    in_flight: int                                # Σ 密钥 in_flight（在线 HTTP 请求数，不含驻留/退避）
    max_concurrency: int
    calls: int
    retries: int
    prompt_tokens: int
    completion_tokens: int
    est_cost_usd: float | None                    # 未配价目为 None（面板显示 "—"）
    p50_latency_ms: int | None                    # 有界样本窗中位数；无样本为 None
    keys: tuple[KeySnapshot, ...]                 # 池 = 1 时单元素
```

### 3.9.3 行为规格

| 机制 | 定义 |
|---|---|
| Provider 适配 | `openai_compatible`：POST `{base_url}/chat/completions`，图像为 `image_url: data:image/png;base64,...`；`anthropic`：POST `{base_url}/v1/messages`，图像为 `source.type="base64"`。两者的结构化输出映射见 3.8.2 L0。v1.2 embedding：`embed()` 仅支持 `openai_compatible`：POST `{base_url}/embeddings`，请求体含 `model` 与 `input`（texts 数组），响应取 `data[*].embedding`（顺序与输入对齐）；`[embedding.*]` profile 的 provider 无 "anthropic" 取值（5.1）。 |
| 重试 | 可重试错误 = 网络错误、超时、HTTP 408/409/429/5xx。第 i 次等待 = `random(0, retry_base_delay_s × 2^i)`（全抖动指数退避，工业标准），封顶 60s，最多 `max_retries` 次——**v1.6 起该退避公式仅适用于网络错误/超时/408/409/5xx；一切 429 等待（含与不含 `Retry-After`）一律落于密钥冷却**而非调用内休眠，时长以密钥池行为唯一规范（含 `Retry-After` 遵从其全时长；缺失按密钥计数指数冷却、封顶 300s）。池内有其他可用密钥时下一次尝试立即轮换、零等待；池大小 1 时驻留至冷却结束——含 `Retry-After` 时净等待与 v1.5 相同但受 `run.max_park_s`（默认 3600s）约束，超长 `Retry-After`（如小时级配额信号）在单密钥配置下不再无界等待，而按重试耗尽让该记录失败（v1.6 行为修订）。不可重试错误（401/403/400/404）直接抛 `ProviderFatalError`；其中**认证类（401/403）在抛出的同时立即打开熔断器**——凭据/权限故障不会自愈，按连续计数只是烧钱（v1.5）；v1.6 密钥池下认证失败先按密钥禁用，仅当禁用的是最后一把存活密钥时才立即熔断（密钥池行）。重试耗尽（`provider_retryable_exhausted`，v1.6 含驻留超限）同样计入熔断窗口（7.6）。 |
| 限流 | 每 profile 一个 `asyncio.Semaphore(max_concurrency)`；全部调用（含修复、评审）共享该信号量。v1.6：池化 profile 仍是**一个**信号量——`max_concurrency` 为池内全部密钥的总在途上限，不随密钥数放大。 |
| 密钥池（v1.6） | profile 以 `api_key_envs = [...]`（5.1，与 `api_key_env` 恰提供其一）声明多把密钥，同 base_url、同 model 构成**同构池**；单密钥配置 = 池大小 1：数据产出、重试记账与熔断/退出语义与 v1.5 一致，429 等待路径为 v1.6 行为修订（见重试行：`Retry-After` 等待受 `run.max_park_s` 约束；无 `Retry-After` 冷却封顶 300s 且按密钥跨调用计数；驻留发 WARN 与事件）。**选择**：每次请求尝试发出前选「在途请求数最少」的可用密钥（并列取声明序靠前者；确定性算法、无 RNG——密钥选择只影响时序不影响数据内容，与重试抖动同属 seed 豁免，2.6 可复现性不受影响），请求头按所选密钥逐次构造。**每密钥 429 冷却**：含 `Retry-After` 时冷却其全时长；缺失时按 `random(0, retry_base_delay_s × 2^c)` 全抖动冷却、封顶 300s（c = 该密钥连续 429 计数，跨逻辑调用累计，**该密钥自身**任一成功清零）——无 `Retry-After` 的持续限流由此以每密钥 ≤ 每 5 分钟一次的探测频率自愈。冷却不改重试记账：该次尝试照常消耗一次重试预算，但下次尝试立即换可用密钥重发（`llm.key_cooldown` 事件，7.2）。**认证禁用**：密钥 401/403 ⇒ 本运行内永久禁用（stderr WARN 一次 + `llm.key_disabled` 事件，携环境变量名）；池内尚有存活密钥 ⇒ 同一尝试立即换密钥重发，不消耗重试预算、不计入熔断（认证失败是密钥级确定性故障，每密钥至多发生一次，轮换次数以池大小为界）；禁用的是**最后一把**存活密钥 ⇒ 等价 v1.5 认证首错：立即熔断、退出码 4（3.10.3）。配额以 403 形态出现的 provider 同按认证禁用处理，不做错误体嗅探（1.6 对齐决策 ④）。**驻留**：全部存活密钥均在冷却 ⇒ 调用驻留至最早冷却结束（`llm.pool_parked` 事件 + stderr WARN；≤60s 分片休眠，每片重查熔断器——v1.5 排队调用熔断复查语义保持）；驻留不消耗重试预算，单次逻辑调用累计驻留（跨多段驻留求和，不含信号量排队等待）> `run.max_park_s`（5.2，默认 3600，0 = 不驻留）⇒ 按重试耗尽抛 `ProviderRetryableError`（记录 failed、计入熔断窗口，1.6 对齐决策 ③）；最早冷却结束时刻已可证超出剩余驻留预算时立即按同路径失败，不空耗墙钟。驻留发生在**已获取的信号量槽内并持有该槽**——全池冷却时吞吐本为零，放槽只会放入更多注定驻留的调用。降级阶梯：**轮换（零等待）→ 驻留（有界）→ 记录失败累积 → 熔断退出 4**。400/404 等请求形错误与密钥无关：不轮换，行为同 v1.5。`embed()` 与 `[embedding.*]` profile 适用同一机制。 |
| 图像编码 | 调用时读盘 → 若长边 > `profile.max_image_px`（默认 2048）按比例缩小再编码（Pillow）→ base64 → 请求发出后即释放字节（懒加载契约，2.6 节）。 |
| 计量 | 从响应 usage 字段累计 prompt/completion token；`profile.price_per_mtok_in/out`（可选配置）存在时折算成本入报告。 |
| 快照（v1.10） | `snapshot()`（3.9.2）为 console 面板的只读拉取面（7.7，每 tick 一次）：`in_flight` = Σ 密钥在途（在线 HTTP 请求数口径）、密钥三态（ok / cooldown 携剩余秒 / disabled）、用量镜像 usage、**p50 延迟 = 每 (kind, profile) 有界样本窗 `deque(maxlen=256)` 的中位数**（成功逻辑调用口径，`_post_with_retries` 成功返回前喂入——v1.10 唯一新增采集点）；窗口与中位数**不入 report.json**（报告零新键，7.7）、不入任何事件。 |

**背书：**「统一多 provider 异步客户端 + 信号量并发 + 指数退避」与 distilabel 的 LLM 抽象层 [5]、NeMo Curator 的服务客户端 [9] 同构；全抖动退避为 AWS 架构规范确立的工业标准。

### 3.9.4 输入 / 输出示例

以统一 UI 示例贯穿：5.1 的 `[llm.default]` profile + 5.2 的 project.toml，记录为文件对 `capture/2026-07-01/b/uitree_2.jsonl` + `c/image_2.png`（`pair_index=2`，即 6.3 中 id 为 `9f2c31ab52e08d17` 的登录页记录）。M5 按 3.5.2 模板组装 `PromptBundle`，M8 因 `supports_structured_output=true` 启用 L0，M9 适配为如下 `openai_compatible` 请求。

#### ① 标注请求报文（openai_compatible）与响应要点

```
POST https://llm-gw.example.com/v1/chat/completions HTTP/1.1
Authorization: Bearer ${LABELKIT_KEY_DEFAULT}      ← api_key_env 所指环境变量的值
Content-Type: application/json

{
  "model": "qwen2.5-vl-72b-instruct",
  "temperature": 0.0,
  "max_tokens": 4096,
  "messages": [
    {"role": "system",
     "content": "你是移动端 UI 理解标注员。根据屏幕截图与 UI 控件树，\n标注该屏幕的功能类别、页面标题、可交互元素列表与一句话页面描述。\n输出必须是符合以下 JSON Schema 的单个 JSON 对象，不输出任何其他内容：\n{…5.2 用户 Schema 全文，与下方 response_format.json_schema.schema 相同，此处从略…}"},
    {"role": "user", "content": [
      {"type": "text", "text": "[屏幕截图]"},
      {"type": "image_url", "image_url": {"url": "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAA…（截断示意；c/image_2.png 读盘后长边≤2048px 等比缩放再编码，3.9.3）"}},
      {"type": "text", "text": "[UI 控件树]\nFrameLayout [0,0,1080,2340]\n  TextView \"登录\" [72,296,264,392]\n  EditText [72,520,1008,664] hint=请输入手机号\n  EditText [72,712,672,856] hint=请输入验证码\n  Button \"获取验证码\" [704,712,1008,856]\n  Button \"登录\" [72,952,1008,1096]"}
    ]}
  ],
  "response_format": {"type": "json_schema", "json_schema": {"name": "user_schema", "strict": true,
    "schema": {"$schema": "https://json-schema.org/draft/2020-12/schema", "type": "object",
      "properties": {
        "screen_category": {"type": "string", "enum": ["login","home","list","detail","form","settings","dialog","other"]},
        "page_title": {"type": "string"},
        "interactive_elements": {"type": "array", "items": {"type": "object",
          "properties": {"role": {"type": "string"}, "label": {"type": "string"},
                         "bounds": {"type": "array", "items": {"type": "integer"}, "minItems": 4, "maxItems": 4}},
          "required": ["role","label","bounds"], "additionalProperties": false}},
        "description": {"type": "string", "maxLength": 200}},
      "required": ["screen_category","page_title","interactive_elements","description"],
      "additionalProperties": false}}}
}

HTTP/1.1 200 OK（要点字段）
{
  "id": "chatcmpl-7d31a9c04b5e82f6",
  "choices": [{"index": 0, "finish_reason": "stop", "message": {"role": "assistant",
    "content": "{\"screen_category\":\"login\",\"page_title\":\"登录\",\"interactive_elements\":[{\"role\":\"EditText\",\"label\":\"请输入手机号\",\"bounds\":[72,520,1008,664]},{\"role\":\"EditText\",\"label\":\"请输入验证码\",\"bounds\":[72,712,672,856]},{\"role\":\"Button\",\"label\":\"获取验证码\",\"bounds\":[704,712,1008,856]},{\"role\":\"Button\",\"label\":\"登录\",\"bounds\":[72,952,1008,1096]}],\"description\":\"手机号+验证码登录页\"}"}}],
  "usage": {"prompt_tokens": 3184, "completion_tokens": 156, "total_tokens": 3340}
}
```

#### ② M9 返回的 LLMResponse（3.9.2）

```
{
  "text": "{\"screen_category\":\"login\",\"page_title\":\"登录\",…（与上方 message.content 逐字相同）}",
  "structured": null,
  "usage": {"prompt_tokens": 3184, "completion_tokens": 156},
  "model": "qwen2.5-vl-72b-instruct",
  "latency_ms": 4820
}
```

`structured` 仅在 anthropic provider 以 `tool_choice` 强制工具调用（3.8.2 L0）返回原生结构化载荷时非空；openai_compatible 的 json_schema 模式产物是文本，M9 原样放入 `text` 不做解析（3.9.1 负边界），解析与校验由 M8 L1→L2 完成——本例 content 已是纯 JSON，将计入 `report.schema_engine.resolved_at.l0_or_clean`。

#### ③ 重试时间线（同一次 complete() 调用，profile 默认值：timeout_s=120，max_retries=5，retry_base_delay_s=1.0）

| 尝试 | 时刻 | 结果 | 等待 | 依据（3.9.3 重试规则） |
|---|---|---|---|---|
| attempt 1 | t=0.0s 发出 | HTTP 429，响应头 `Retry-After: 5` | 5.0s | 429 属可重试错误集（408/409/429/5xx）；「429 响应含 `Retry-After` 时优先遵从」，直接等 5s，退避公式不参与。 |
| attempt 2 | t=5.0s 重发 | t=125.0s 达 timeout_s=120 超时 | 2.3s | 超时属可重试错误；全抖动指数退避「第 i 次等待 = `random(0, retry_base_delay_s × 2^i)`」，i=2：random(0, 1.0×2^2)=random(0, 4.0)，本次抽得 2.3s（< 60s，封顶不生效）。 |
| attempt 3 | t=127.3s 重发 | t=132.1s 收到 HTTP 200（即 ① 的响应，耗时 4.82s → latency_ms=4820） | — | 成功返回 LLMResponse。已用重试 2 次 < max_retries=5；若第 5 次重试后仍失败则抛 `ProviderRetryableError`（3.9.2）；本次调用 retries+=2 计入该 profile 计量。 |

注（v1.6）：本表为 v1.5 单密钥基线，⑤ 引用其作对比。v1.6 起 attempt 1 的 5s 等待经**密钥冷却 + 驻留**实现（发 `llm.key_cooldown` / `llm.pool_parked` 事件，计入 `run.max_park_s`）——净时序与本表相同，重试记账不变（3.9.3 密钥池行）。

#### ④ usage_by_profile 累计结果示例

```
>>> client.usage_by_profile        # 快照：完成 3 次标注调用（含上表那次）与 1 次评审调用后
{
  "default": {"calls": 3, "prompt_tokens": 9552, "completion_tokens": 486,
              "retries": 2, "est_cost_usd": 0.0066},
  "judge":   {"calls": 1, "prompt_tokens": 2210, "completion_tokens": 118, "retries": 0}
}
```

数值自洽性：3 次标注请求 prompt 各 3184 token，3×3184=9552；completion 为 156+162+168=486。`[llm.default]` 配置了 price_per_mtok_in=0.6 / price_per_mtok_out=1.8，故 est_cost_usd = 9552÷106×0.6 + 486÷106×1.8 = 0.0057312 + 0.0008748 = 0.006606 ≈ 0.0066；`[llm.judge]` 未配置单价，不产生成本字段（3.9.3 计量）。retries=2 来自上表时间线。该字典在 finalize 时由 M10/M11 落入 `report.json` 的 `llm_usage`（6.4）。

#### ⑤ 密钥池轮换时间线（v1.6；`api_key_envs = ["LABELKIT_KEY_A", "LABELKIT_KEY_B"]`，其余 profile 参数同 ③）

| 尝试 | 时刻 | 密钥 | 结果 | 等待 | 依据（3.9.3 密钥池行） |
|---|---|---|---|---|---|
| attempt 1 | t=0.0s 发出 | KEY_A（两密钥在途数相同，取声明序靠前者） | HTTP 429，响应头 `Retry-After: 30` | 0s | KEY_A 冷却至 t=30.0s（`llm.key_cooldown`，cooldown_s=30、retry_after=true）；本次尝试消耗 1 次重试预算；KEY_B 可用 ⇒ 下次尝试立即发出，限流等待为零。 |
| attempt 2 | t=0.0s 重发 | KEY_B | t=4.6s 收到 HTTP 200 | — | 成功返回。对比 ③ 时间线：v1.5 单密钥同场景须原地等 30s——轮换把限流等待压为零。本调用 retries+=1 计入计量；`llm.call` 携 `key_env="LABELKIT_KEY_B"`（7.2）。 |

边界情形：若 t=0.0s 时 KEY_B 也在冷却（如至 t=12.0s），则调用**驻留**至 t=12.0s 再以 KEY_B 重发（`llm.pool_parked`；驻留不消耗重试预算，累计受 `run.max_park_s` 约束）；若 KEY_B 此前已被 401 禁用，则 KEY_A 即最后一把存活密钥——其 429 冷却照常驻留等待，而其 401 将立即熔断（3.10.3）。
