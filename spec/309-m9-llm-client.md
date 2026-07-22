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
                    image_px: int | None = None   # v1.11 追加字段（V23①，3.9.5）：本次调用的图片生效像素档
                                                  #   （V21 判审升级档的唯一载体；None = 用 profile 工作点。
                                                  #   px 必须随 bundle 而非算子可变状态——build_body 每
                                                  #   attempt 重编码图片，载体在 bundle 才保重试确定性）
@dataclass(frozen=True)
class LLMResponse:  text: str; structured: dict | None; usage: Usage; model: str; latency_ms: int
                    finish: str | None = None     # v1.11 追加字段（V23③，3.9.5）：规范化终止原因（openai
                                                  #   finish_reason / anthropic stop_reason 原值），供 V11/V24
                                                  #   终局化判定；_result_usage 的 len==4 分派随元组形状同步适配

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
    @property
    def calibrator(self) -> ImageCostCalibrator           # v1.11（V19/V23②，3.9.5）：图片成本在线校准器——
                                                          # LLMClient 自持构造（零 factory 改动）；M9 每响应
                                                          # 喂样本、算子经 ctx.llm.calibrator.cost(profile)
                                                          # 读数、M10 批边界 freeze_batch()

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
| 重试 | 可重试错误 = 网络错误、超时、HTTP 408/409/429/5xx。第 i 次等待 = `random(0, retry_base_delay_s × 2^i)`（全抖动指数退避，工业标准），封顶 60s，最多 `max_retries` 次——**v1.6 起该退避公式仅适用于网络错误/超时/408/409/5xx；一切 429 等待（含与不含 `Retry-After`）一律落于密钥冷却**而非调用内休眠，时长以密钥池行为唯一规范（含 `Retry-After` 遵从其全时长；缺失按密钥计数指数冷却、封顶 300s）。池内有其他可用密钥时下一次尝试立即轮换、零等待；池大小 1 时驻留至冷却结束——含 `Retry-After` 时净等待与 v1.5 相同但受 `run.max_park_s`（默认 3600s）约束，超长 `Retry-After`（如小时级配额信号）在单密钥配置下不再无界等待，而按重试耗尽让该记录失败（v1.6 行为修订）。不可重试错误（401/403/400/404）直接抛 `ProviderFatalError`；其中**认证类（401/403）在抛出的同时立即打开熔断器**——凭据/权限故障不会自愈，按连续计数只是烧钱（v1.5）；v1.6 密钥池下认证失败先按密钥禁用，仅当禁用的是最后一把存活密钥时才立即熔断（密钥池行）。重试耗尽（`provider_retryable_exhausted`，v1.6 含驻留超限）同样计入熔断窗口（7.6）。v1.11 修订（V16/V20/V24，3.9.5）：**所用 profile 预算开启（`context_window > 0`）时**，400 响应先于 ProviderFatalError 分类在**完整响应体**上匹配超窗 pattern 集（3.9.5 pattern 行）——命中抛 `ContextOverflowError(phase="reactive")`，M9 **不喂**熔断连击（降级重试与终局补喂归属主算子，熔断交互矩阵见 3.9.5）；未命中或预算关闭走本行 fatal 老路（零回归）。另两类记录级终局同不入本行分类：complete() 分派前终检抛 `ContextOverflowError(phase="precheck")`（零 provider 交互，V16）、成功响应按终止原因归一终局化（`OutputTruncatedError`；200 形态 `model_context_window_exceeded` 同抛 `ContextOverflowError(phase="reactive")`——V11，3.9.5）——上述异常均**不喂 `_record_provider_result(fatal=True)`、不烧常规重试**。 |
| 限流 | 每 profile 一个 `asyncio.Semaphore(max_concurrency)`；全部调用（含修复、评审）共享该信号量。v1.6：池化 profile 仍是**一个**信号量——`max_concurrency` 为池内全部密钥的总在途上限，不随密钥数放大。 |
| 密钥池（v1.6） | profile 以 `api_key_envs = [...]`（5.1，与 `api_key_env` 恰提供其一）声明多把密钥，同 base_url、同 model 构成**同构池**；单密钥配置 = 池大小 1：数据产出、重试记账与熔断/退出语义与 v1.5 一致，429 等待路径为 v1.6 行为修订（见重试行：`Retry-After` 等待受 `run.max_park_s` 约束；无 `Retry-After` 冷却封顶 300s 且按密钥跨调用计数；驻留发 WARN 与事件）。**选择**：每次请求尝试发出前选「在途请求数最少」的可用密钥（并列取声明序靠前者；确定性算法、无 RNG——密钥选择只影响时序不影响数据内容，与重试抖动同属 seed 豁免，2.6 可复现性不受影响），请求头按所选密钥逐次构造。**每密钥 429 冷却**：含 `Retry-After` 时冷却其全时长；缺失时按 `random(0, retry_base_delay_s × 2^c)` 全抖动冷却、封顶 300s（c = 该密钥连续 429 计数，跨逻辑调用累计，**该密钥自身**任一成功清零）——无 `Retry-After` 的持续限流由此以每密钥 ≤ 每 5 分钟一次的探测频率自愈。冷却不改重试记账：该次尝试照常消耗一次重试预算，但下次尝试立即换可用密钥重发（`llm.key_cooldown` 事件，7.2）。**认证禁用**：密钥 401/403 ⇒ 本运行内永久禁用（stderr WARN 一次 + `llm.key_disabled` 事件，携环境变量名）；池内尚有存活密钥 ⇒ 同一尝试立即换密钥重发，不消耗重试预算、不计入熔断（认证失败是密钥级确定性故障，每密钥至多发生一次，轮换次数以池大小为界）；禁用的是**最后一把**存活密钥 ⇒ 等价 v1.5 认证首错：立即熔断、退出码 4（3.10.3）。配额以 403 形态出现的 provider 同按认证禁用处理，不做错误体嗅探（1.6 对齐决策 ④）。**驻留**：全部存活密钥均在冷却 ⇒ 调用驻留至最早冷却结束（`llm.pool_parked` 事件 + stderr WARN；≤60s 分片休眠，每片重查熔断器——v1.5 排队调用熔断复查语义保持）；驻留不消耗重试预算，单次逻辑调用累计驻留（跨多段驻留求和，不含信号量排队等待）> `run.max_park_s`（5.2，默认 3600，0 = 不驻留）⇒ 按重试耗尽抛 `ProviderRetryableError`（记录 failed、计入熔断窗口，1.6 对齐决策 ③）；最早冷却结束时刻已可证超出剩余驻留预算时立即按同路径失败，不空耗墙钟。驻留发生在**已获取的信号量槽内并持有该槽**——全池冷却时吞吐本为零，放槽只会放入更多注定驻留的调用。降级阶梯：**轮换（零等待）→ 驻留（有界）→ 记录失败累积 → 熔断退出 4**。400/404 等请求形错误与密钥无关：不轮换，行为同 v1.5。`embed()` 与 `[embedding.*]` profile 适用同一机制。 |
| 图像编码 | 调用时读盘 → 若长边 > **生效像素档**按比例缩小再编码（Pillow）→ base64 → 请求发出后即释放字节（懒加载契约，2.6 节）。v1.11 生效档链（V18/V21/V23①，3.9.5）：`生效 px = bundle.image_px or profile.default_image_px or profile.max_image_px`，再钳 `min(·, profile.max_image_px)`——`default_image_px` 为采样默认工作点（0/缺省 = 沿用 max_image_px，行为与 v1.10 逐字节一致，5.1）、`bundle.image_px` 为 V21 判审升级档载体、`max_image_px` 恒为升级天花板。 |
| 计量 | 从响应 usage 字段累计 prompt/completion token；`profile.price_per_mtok_in/out`（可选配置）存在时折算成本入报告。 |
| 校准采样（v1.11） | 每个**含图**成功响应向图片成本校准器（V19，3.9.5）喂一个样本：`(usage.prompt_tokens − est_text(本请求文本)) / 本请求图数`；响应无可用 usage（企业网关有 `usage: null`，[C-64]）⇒ 不记样本、WARN 一次/profile（"image-cost calibration inactive"）——校准器停留先验 ×1.2。 |
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

### 3.9.5 上下文预算、估算器与图片成本校准（v1.11）

v1.11 新增（决策 V1–V27 见 `docs/dev/SPEC-context-budget.md`，调研引用 [C-1]–[C-84] 见 `docs/dev/PROPOSAL-context-budget.md`）。**不变式**：对每一次 LLM 调用，`est(输入 prompt) + max_output_tokens + margin ≤ context_window`。预算按 profile **声明制**开启（5.1 `context_window` 行：`0` = 未声明 = 本节机制对该 profile 整体关闭，行为与 v1.10 一致；声明实效窗口教义见 5.1/V26）。预算与估算原语落新文件 `labelkit/common/runtime/budget.py`（全部纯函数、零第三方依赖；margin/密度/阶梯常数**冻结于代码、不开配置面**——业界不存在权威保守系数（V7/V8 调研负结果），用户逃生门 = 声明更小的 `context_window`；修改常数即 spec 修订）。数据自适应的贪心装填器属算子逻辑、落在 M14（3.14.4）——budget.py 只提供估算与预算原语 + 校准器（依赖方向 operators → common 不变）。

| 机制 | 定义 |
|---|---|
| 预算公式（V7） | `margin = max(256, ceil(0.10 × context_window))`；`input_budget = context_window − max_output_tokens − margin`（`context_window == 0` ⇒ 0 = 预算关）。embedding 预算 = `context_window − margin`（**无输出预留**，V15）——embed 输入超预算按确定性头部保留截断（嵌入语义主体在前部，3.3.3 第④级）。margin 承担：估算残差 + 消息封装 + provider 侧计数偏差。预算非正 ⇒ M1 CONFIG_ERROR（3.1.4）。 |
| 零依赖估算器（V8 v3） | `est_text(s) = ceil(ascii/3 + cjk×1.0 + other/2)`——ASCII 取 /3 非 /4 = 对 JSON 膨胀的保守化；CJK×1.0 覆盖 GLM 0.67 / o200k 0.8–1.0 / Qwen·DeepSeek 0.77–0.9（t/字）；**已知局限**：cl100k 旧词表中文 1.25–1.4 t/字不被覆盖（记载 + 逃生门同上，per-profile 密度旋钮列 roadmap）。CJK 判定 = Unicode 块 CJK Unified Ideographs 及扩展 + 全角标点。消息封装 +4 t/消息；结构化输出 schema 文本计入 est（它随请求发送）。`est_image_prior(profile, px)` = provider 文档公式于**生效工作点 px 的最坏纵横比**求值：anthropic = `min(⌈px/28⌉², 1568)`（28px patch 制最坏正方形）；openai_compatible = tile 制按 2048→短边 768 归一化后的**最坏纵横比**求值——@2048 长边竖屏 = 85+8×170 = **1445**（正方形 765 是特例，UI 截图纵横比下系统性低估 36%，[C-60]）。图片估算仅作**首批先验**（校准器先验种子 = 本值 × 1.2 保守放大，PRIOR_INFLATION）——正确性由在线校准（下行）与溢出反应（V20 行）承担，公式准确度只影响首批装填效率（V17 测量-反应式范式；est 的语义自始是**预留上界而非精确记账**）。不引 tokenizer 依赖（对主力 GLM 不给真值）。 |
| 在线图片成本校准器（V19/V23②） | `ImageCostCalibrator`（budget.py；运行内存、零持久化——跨运行冷启动是 stateless 约束的固有代价）实例由 `LLMClient` **自持**（构造器内建，零 factory 改动），公开面 `llm.calibrator`（3.9.2）。每 profile 维护每图实际成本估计：样本 = `(usage.prompt_tokens − est_text(该请求文本)) / 本请求图数`（M9 每含图响应喂入，3.9.3 校准采样行）；滤波 = **窗口化最大值，窗口单位 = 批**——样本以 asyncio 完成序到达，M10 于批边界调 `freeze_batch()` 聚合**本批样本的 max**（对无序集取 max，序无关）压入 `deque(maxlen=8)` 批最大值窗口；装填读数 `cost(profile)` = `max(批最大值窗口) ÷ 0.85` 取整（装填安全折扣）；累计样本 < 8 ⇒ 先验 × 1.2（样本不足不做主动升档）。**确定性护栏：校准快照按批冻结**——第 N 批装填只读 < N 批聚合值（批序串行 ⇒ 同输入同配置可复现；逐响应更新 + 逐调用读取会让内容依赖 asyncio 完成序，禁止）。usage 缺失兜底见 3.9.3 校准采样行。校准终值入 `report.budget.image_cost`（6.4，V13⑤）。 |
| complete() 终检（V16） | `complete()` 于 provider 分派前执行不变式终检：`est_prompt + max_output_tokens + margin > context_window` ⇒ 抛 `ContextOverflowError(phase="precheck")`（记录级；**不喂熔断、不烧重试**）。归属理由：complete() 是全部调用——含 M8 L3 修复调用与 `--probe`——的唯一咽喉；probe 经一次性子客户端同走 complete()，`max_output_tokens=1` + V6 正预算校验使其**平凡通过**，无须豁免工程（F13）。**装填层正确时终检永不触发**——它是防御性不变式，不是第二套装填逻辑。 |
| 终止原因归一（V11） | 响应终止原因（`LLMResponse.finish`：openai `finish_reason` / anthropic `stop_reason` 原值）按闭合映射**终局化**，不再把截断 JSON 送 L1–L3 修复循环硬修：① `finish_reason=length`（openai）/ `stop_reason="max_tokens"`（anthropic）⇒ `output_truncated`（输出触到 max_output_tokens 上限；记录级 reject，不喂熔断——预算已为 max_output_tokens 预留完整空间，模型自然写满属输出侧事件）；② **双协议 `model_context_window_exceeded`**（anthropic 4.5+ 系 stop_reason [C-57]；z.ai openai 协议 finish_reason [C-58]）⇒ `context_overflow` 反应态——input+max_tokens > cw 时新款后端不再 400 而是接受请求、生成触墙截断，此值即溢出 oracle 的 200 形态，同抛 `ContextOverflowError(phase="reactive")`（预算开启时可触发属主算子降级重试，不依赖 400 嗅探）；③ z.ai 扩展值 `sensitive` / `network_error` 及其他未知值 ⇒ v1 不做专项处置，沿现行管线流转（内容进 M8 校验，垃圾输出自然走修复/拒收）。**显式拒绝厂商「加大 max_tokens 重试」建议（[C-61]）**——逐调用抬升输出上限即破坏预算不变式与确定性；正确的用户补救 = 配置层提高 `max_output_tokens`。 |
| 超窗错误体 pattern 集（V20） | **按 profile 预算门控**（`context_window == 0` 时嗅探不启用，400 走 v1.10 原路）：400 响应在**完整 resp.text** 上（先于任何截断）匹配溢出 pattern 初始集（[C-75] 实证种子）——OpenAI/Azure `code == "context_length_exceeded"` ∨ 消息含 `"maximum context length"`；vLLM 同消息族（type=BadRequestError、无该 code——只匹 code 会漏）；anthropic 协议 `invalid_request_error` ∧ 消息含 `"prompt is too long"`；z.ai 业务码 `"1261"` / 消息含 `"Prompt too long"`；OpenRouter `error_type == "context_length_exceeded"`。命中 ⇒ 抛 `ContextOverflowError(phase="reactive")`，M9 **不喂** `_record_provider_result(fatal=True)`——降级重试机会与终局补喂归属主算子（下行矩阵）；未命中或预算关闭 ⇒ 走 3.9.3 重试行 fatal 老路（零回归）。嗅探只是「是否给降级机会」的**优化门**，不构成熔断豁免面（A7）：漏判 = 老路零回归，误判 = 浪费 ≤ 2 次有界重试。z.ai 端点错误体样本列入集成测试采集，pattern 集可渐进扩充（E2E-FINDINGS 记录）。 |
| 熔断交互矩阵（V16/V20/V24/A7） | `ContextOverflowError` 带 `phase: Literal["precheck","reactive"]`（V24）。三形态 × 熔断连击：**precheck 不计连击**（发生在任何 provider 交互之前，属客户端决策，与 provider 健康无关；含 V10 最小单元不装——由算子在装填处直接记录，无异常穿越）；**reactive-400 降级耗尽的终局计入连击**（A7 裁决——由属主算子经 `ctx.metrics.record_provider_result(fatal=True)` 补喂**恰一次**，M9 抛出时不喂；成功降级或任何成功调用清零连击——只有「连续 fatal_error_threshold 条记录都无法靠降级挽救」的系统性失配才停机）；**reactive-200**（`model_context_window_exceeded`）终局**不补喂**（HTTP 交互本身成功、streak 已被该次 ok 清零，`llm.call` 事件维持 status="ok"，实现者不得「修正」为 fatal）。`output_truncated` 同不喂熔断（交互成功）。两异常均不烧常规重试预算（V20 降级重试独立计数、有界 ≤ 2 次/调用）。降级机会仅在属主算子有降级机制且预算开启时消费（segment 窗对半改切 3.14.4 / annotate 减帧 / quality-pointwise 文本收紧）；无降级面的调用点（extract / verify 回收复裁 / probe / L3 修复）直接按最小单元语义处置（V10/V25）。 |

**背书**：预算不变式为业界共识形态（LlamaIndex `context_window − prompt − num_output` [C-5]、Claude Code `contextWindow − min(maxOut,20k) − 13k` [C-15]、OpenRouter「输入+补全」合并判定 [C-17]）；「首批先验 + 稳态测量校准」的两段结构对标 ABR 的 measure-don't-model 谱系（BBR windowed-max [C-54]、dash.js DYNAMIC 双阶段 [C-37]、FESTIVE p=0.85 装填折扣 [C-32]）。
