## 3.9 M9 LLM 客户端 llm-client

### 3.9.1 职责与边界

**做：**按 profile 提供统一异步调用接口：消息构造（文本/多图多模态）、provider 适配（OpenAI 兼容 / Anthropic 原生）、结构化输出参数透传、超时/重试/限流、token 与成本计量、`--probe` 连通性探测。 
**不做：**不解析业务结构（返回原始文本或原生结构化载荷，解析归 M8）；不缓存响应（无状态）；不做模型路由/降级等「智能」行为——profile 选择完全由配置决定。

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

    async def probe(self, profile: str) -> ProbeResult   # validate --probe：1-token 试调用
    @property
    def usage_by_profile(self) -> dict[str, Usage]        # 报告用累计计量
```

### 3.9.3 行为规格

| 机制 | 定义 |
|---|---|
| Provider 适配 | `openai_compatible`：POST `{base_url}/chat/completions`，图像为 `image_url: data:image/png;base64,...`；`anthropic`：POST `{base_url}/v1/messages`，图像为 `source.type="base64"`。两者的结构化输出映射见 3.8.2 L0。v1.2 embedding：`embed()` 仅支持 `openai_compatible`：POST `{base_url}/embeddings`，请求体含 `model` 与 `input`（texts 数组），响应取 `data[*].embedding`（顺序与输入对齐）；`[embedding.*]` profile 的 provider 无 "anthropic" 取值（5.1）。 |
| 重试 | 可重试错误 = 网络错误、超时、HTTP 408/409/429/5xx。第 i 次等待 = `random(0, retry_base_delay_s × 2^i)`（全抖动指数退避，工业标准），封顶 60s，最多 `max_retries` 次。429 响应含 `Retry-After` 时优先遵从。不可重试错误（401/403/400/404）直接抛 `ProviderFatalError`；其中**认证类（401/403）在抛出的同时立即打开熔断器**——凭据/权限故障不会自愈，按连续计数只是烧钱（v1.5）。重试耗尽（`provider_retryable_exhausted`）同样计入熔断窗口（7.6）。 |
| 限流 | 每 profile 一个 `asyncio.Semaphore(max_concurrency)`；全部调用（含修复、评审）共享该信号量。 |
| 图像编码 | 调用时读盘 → 若长边 > `profile.max_image_px`（默认 2048）按比例缩小再编码（Pillow）→ base64 → 请求发出后即释放字节（懒加载契约，2.6 节）。 |
| 计量 | 从响应 usage 字段累计 prompt/completion token；`profile.price_per_mtok_in/out`（可选配置）存在时折算成本入报告。 |

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
