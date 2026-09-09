# 序列上下文容量：本地真实模型门禁设计与实测状态

核对日期：2026-09-09。初始设计阶段只读核验入口和服务，没有发送推理、分词或模板处理请求，
没有停止或修改服务、读取密钥。随后主任务完成真实执行；本次更新按归档工件收口状态，不发送新模型请求。

本地矩阵与真实 static 复跑分别为 `6 passed, 1 failed in 115.97s` 和 `1 passed in 14.21s`。
矩阵唯一失败来自 static 的旧“全部 sealed”断言，实际业务已成功；修正为精确前段封闭、尾段开放并加强
切点和允许范围断言后重新执行模型通过。不能把两次命令记为单轮七项全通过。
文本节点含 batch_size=2、3、64 三次 execute_run，因此最终归档有八份正式运行工件；八份均通过独立 checker。
详见[示例验证记录](../../../examples/sequence-context-capacity/README.md)、
[结构与用量汇总](../../../examples/sequence-context-capacity/out/verification-20260909/summary.json)、
[矩阵日志](../../../examples/sequence-context-capacity/out/verification-20260909/labelkit-capacity-local-final.log)与
[static 复跑日志](../../../examples/sequence-context-capacity/out/verification-20260909/labelkit-capacity-local-static-final.log)。

## 已核验的服务状态

本次通过无认证的只读 GET 请求核验以下字段，未输出整个 `/props`、槽参数或提示词。

| 项目 | 本次实读结果 |
|---|---|
| 健康状态 | `http://127.0.0.1:8082/health` 返回 `status=ok` |
| 物理槽 | `/slots` 只有一个槽，`n_ctx=32768`，`is_processing=false` |
| 模型别名 | `/v1/models` 与 `/props` 均为 `worldmock-qwen35-4b` |
| 服务版本 | `/props.build_info=b10621-c1d0e7a00` |
| 本机二进制 | `/opt/homebrew/bin/llama-server --version` 为 build 10621、commit `c1d0e7a00`，与运行服务一致 |
| 媒体能力 | `/props.modalities` 为 `vision=true`、`video=true`、`audio=false` |

设计阶段未重新计算模型和 projector 哈希。后续模型 SHA-256 已记录为
`fdedd781c9ce676ab66b018ca247ff78e8a33c98098a822c1e2d5075e7718f66`；实际服务 build、槽容量与视觉能力
在每项门禁前重新核对。二十五图请求与绿色像素答案已通过真实执行；服务声明和语义验证仍是不同证据。
发送后续测试前重新检查槽为空闲；若用户请求占用单槽，等待其自然完成，不能停止、重启或驱逐该请求。

## 现有集成入口的适用性

| 现有入口 | 核验结果 | 本次用途 |
|---|---|---|
| `test_postprocessing_local_llm.py` | 真实调用 `execute_run`，透明观察请求序列化器，独立检查工件；固定 18081、另一别名、98304 窗口和每 profile 并发二 | 复用真实入口、无内容观察、临时项目和独立工件 checker 的方式；不能原样指向当前服务 |
| `test_execution_runtime_local_llm.py` | 固定 18081 的 `/metrics`，并断言处理请求高水位恰为四 | 当前单槽服务无法满足；不能降低断言后仍称为四槽门禁 |
| `test_stream_llm.py` | 实际使用 z.ai；设计前的二十五帧抽样到二十帧断言已随主规格删除 | 本地二十五图的完整请求与视觉答案由新独立门禁验证，不把旧结果算作新证据 |
| `tests/conftest.py` | `local_llm` marker 依赖 `LABELKIT_LOCAL_KEY`；导入时自动读取仓库 `.env` | 使用无认证服务的非秘密占位值执行，最终两次命令没有 skip |

来源：[后处理本地测试](../../../tests/integration/test_postprocessing_local_llm.py)、
[四槽本地测试](../../../tests/integration/test_execution_runtime_local_llm.py)、
[真实 stream 测试](../../../tests/integration/test_stream_llm.py)、[集成门控](../../../tests/conftest.py)、
[后处理本地配置](../../../examples/annotation-postprocessing/config-local-4b.toml)、
[四槽本地配置](../../../examples/sequence-generation/config-local-4b.toml)。

本特性独立入口已实现为
[test_sequence_context_capacity_local_llm.py](../../../tests/integration/test_sequence_context_capacity_local_llm.py)，
使用 `integration` 与 `local_llm` markers，经正式 execute_run 运行有限输入文件项目；共七个 pytest 节点。

## 最小实测矩阵

| 已实现的 node 名称 | 输入与运行方式 | 验收证据 |
|---|---|---|
| `test_real_local_context_overflow_shape` | 完整聊天输入略超实际 32768 token，直连同一个真实端点，仅发一次补全请求 | 真实 HTTP 状态、错误类型与容量字段；不能由手工构造响应代替 |
| `test_real_local_text_batch_invariance` | 同一份完整文本会话，首尾有共同任务约束，含相同内容的不同输入出现位置；依次使用 `batch_size=2`、`3`、`64` | 三轮均真实推理；跨两次计算分组变更后，最终成员位置、顺序、任务归属、容量边界和核心标签一致 |
| `test_real_local_ui_full_member_evidence` | 二十五张小尺寸、可辨识、带独立视觉标记的确定性测试图；每个应保留标记都不重复写入控件树或文件名 | 开启的序列级调用真实携带全部成员图，顺序正确；终检与序列化证据相符；视觉专属语义断言通过 |
| `test_real_local_interrupted_task_stitch` | 仓库三帧、聊天两帧、仓库恢复三帧，开启实际 stitch 和 verify | 三个语义段、一个真实合并壳、两条任务输出；恢复任务的最终业务答案正确 |
| `test_real_local_capacity_recovery[static]` | 使用诚实的小窗口声明限制应用容量，六个完整成员贪心分区 | 三段无丢失，两个切点、两个封闭、尾段开放；before/after 与 allowed_positions 逐段正确，零重算 |
| `test_real_local_capacity_recovery[reactive]` | 独立故障注入配置故意高报窗口；原完整请求超过真实服务容量，完整成员子请求能容纳 | 一次真实超窗、一个成员切点、一次重算，最终两段均封闭且零失败；失败请求哈希不重复 |
| `test_real_local_indivisible_capacity_failure` | 先运行正常两成员会话，再输入单个完整超大成员，无法增加有效成员切点 | 先前会话正常交付，随后一次真实超窗、一次重算与一个最小终态；没有裁正文或重复失败请求 |

上述矩阵覆盖机制最小路径，不替代全部离线组合测试或 Uncle Bob 语义变异审查。
文本用例覆盖跨 batch 分段与整体标注，不应伪造控件树证明纯文本通过了仅适用于 UI 的机械缝合先验。
真实缝合由独立八帧 UI 任务验证，二十五图用例验证每个实际完整请求的全图与全文，并保留视觉专属答案。
没有缩减成员数量到旧采样上限以内；没有通过关闭评审绕过实际任务判定。

## 真实输入超窗的可执行路径

运行版本对应的官方源码已经核实：普通补全任务在输入 token 数达到或超过槽容量时，发送
`ERROR_TYPE_EXCEED_CONTEXT_SIZE`，随后释放该槽；该分支位于 prompt 求值之前。
错误映射为 HTTP 400、`type=exceed_context_size_error`，错误结果附加 `n_prompt_tokens` 与 `n_ctx`。
来源：[输入容量检查](https://github.com/ggml-org/llama.cpp/blob/c1d0e7a00/tools/server/server-context.cpp#L2827)、
[错误类型映射](https://github.com/ggml-org/llama.cpp/blob/c1d0e7a00/tools/server/server-common.cpp#L49)、
[容量字段序列化](https://github.com/ggml-org/llama.cpp/blob/c1d0e7a00/tools/server/server-task.cpp#L1410)。

上述同版本源码依据已由真实 HTTP 形状节点验证：HTTP 400、`type=exceed_context_size_error` 与容量字段同时存在。
生产客户端已增加明确匹配规则，reactive 完整工作流实际观测到 `n_prompt_tokens=36140`、`n_ctx=32768`；
minimum 实际观测为 `34113 > 32768`。普通 400、413 或 500 不因此泛化为 token 超窗。
来源：[当前错误分类实现](../../../labelkit/common/inference/llm_client.py)。

实际形状节点构造纯合成、无秘密的固定内容，用真实 `/tokenize` 验证正文 token 数已超过 32768，
再只发一次 `POST /v1/chat/completions`，使用当前别名、`stream=false`、`max_tokens=1`。
原计划中的 `/apply-template` 调用没有进入最终实现；验收同时依赖实际端点拒绝及其完整输入容量字段，
不以字符数或估算器宣称真实超窗。如果返回成功或其他错误，该节点失败，不重标为已触发容量错误。
来源：[同版本服务接口](https://github.com/ggml-org/llama.cpp/blob/c1d0e7a00/tools/server/README.md)。

直连超窗用例只证明端点错误识别。完整的 reactive 门禁还必须通过生产请求构造、生产客户端和会话驱动。
其独立测试 profile 可故意声明 `context_window=65536`，而服务保持实际 32768；记录这是一项声明不符的故障注入，
不能把它复制为普通运行配置。先确认完整请求通过应用预检且服务实际计数超窗，再证明确定性切分后的请求成功。
不得 monkeypatch 客户端、预算函数或 HTTP transport 来制造溢出；透明观察只能记录，不能改请求或响应。

失败的容量请求可能没有 prompt/completion usage，这与在 prompt 求值前拒绝一致。
正常及重算成功的请求必须具有真实非零 usage；不能要求失败请求伪造非零 token，也不能以全程零 usage 算通过。

## 正常本地 profile

门禁在临时项目中使用下列 profile，覆盖文本与 UI。所有需要的阶段共享一个逻辑 profile，保持并发为一；
不运行旧四槽用例，不调整现有 server。图片测试需保留完整画面，尺寸按真实模型可读性选择。

```toml
schema_version = 1

[llm.default]
provider = "openai_compatible"
base_url = "http://127.0.0.1:8082/v1"
model = "worldmock-qwen35-4b"
api_key_env = "LABELKIT_LOCAL_KEY"
max_concurrency = 1
timeout_s = 600
max_retries = 0
supports_structured_output = false
supports_vision = true
max_output_tokens = 2048
context_window = 32768
temperature = 0.0
max_image_px = 256
default_image_px = 256

[llm.default.extra_body.chat_template_kwargs]
enable_thinking = false
```

已有 `extra_body` 会原样平铺到 OpenAI-compatible 请求顶层；上游同版本文档明确支持
`chat_template_kwargs.enable_thinking=false`。用它关闭本地思考，不假设 Anthropic `thinking` 参数在此协议中等价。
来源：[请求构造实现](../../../labelkit/common/inference/llm_client.py)、
[上游聊天接口](https://github.com/ggml-org/llama.cpp/blob/c1d0e7a00/tools/server/README.md#post-v1chatcompletions-openai-compatible-chat-completions-endpoint)。

对应项目已通过 keyless 配置加载，文本正式 validate 与 dry-run 通过，文本/UI 实际调用亦已验证。
小窗口静态用例和高报窗口故障用例分别使用独立文件，普通 profile 保持真实容量。

## 工件与检查口径

| 证据层 | 必须保留与独立核对的内容 |
|---|---|
| 输入固定性 | 输入与图片文件哈希、实际输入出现位置、模型与 projector 哈希、server build、有效 profile 参数；不保存凭据 |
| 请求真实性 | 真实 URL、阶段、调用序、成员位置、文本或图片引用哈希、图片数、完整请求估算；观察器返回原请求且不修改 transport |
| 端点处理 | HTTP 状态、错误字段形状、成功 usage、耗时；服务空闲和占用证据与任务时间对应 |
| 语义 | 用事先冻结的 oracle 核对跨首尾约束、任务回归及图像专属内容；不能只检查字段非空或 Schema 合法 |
| 成员与状态 | 按输入出现位置比较成员，不用 ID 集合吞掉内容相同的重复输入；检查顺序、唯一归属、fragments、seam、容量原因与清理 |
| 三轮分组比较 | 固定输入、profile、seed 和模型参数，仅改变 `batch_size`；比较语义和确定性结构，单独报告 run ID、耗时及自然语言表述差异 |
| 正式交付 | 从实际 `ResolvedConfig.paths` 读取 main、report、rejects 和 refs trace；检查总计守恒、无重复行、失败会话无半输出 |

本特性运行 process/stream 路径，不能直接套用 generate_only 的 main+stream+manifest checker。
只有主规范确实定义并实际生成的工件才可作为成功证据；不能为了复用旧 checker 凭空要求生成 manifest。
失败尝试的调用与用量可以累计，最终数据计数和成员归属必须按主规范的事务边界结算。

## 命令建议

以下只读命令可在测试前重新核验；`/slots` 只摘取容量和占用字段，避免输出整组采样参数：

```bash
cd /Users/atishoo/Project/LabelKit
/opt/homebrew/bin/llama-server --version
curl --fail --silent --show-error http://127.0.0.1:8082/health
curl --fail --silent --show-error http://127.0.0.1:8082/slots \
  | python3 -c 'import json,sys; print([{k:s[k] for k in ("id","n_ctx","is_processing")} for s in json.load(sys.stdin)])'
```

以下是已实现门禁的重跑命令，历史执行结果见本文开头。`local-test-key` 是无认证本地服务使用的非秘密测试占位值，
不读取或打印真实凭据；若服务实际启用认证，应由既有安全环境提供授权，不能修改 server 或尝试绕过认证。
每次使用新的临时根目录，避免 pytest 清理旧证据目录。

```bash
LABELKIT_GATE_EVIDENCE=$(mktemp -d /tmp/labelkit-sequence-context-local.XXXXXX)
LABELKIT_LOCAL_KEY=local-test-key uv run --python 3.12 pytest \
  tests/integration/test_sequence_context_capacity_local_llm.py \
  -q -s -m 'integration and local_llm' \
  --basetemp "$LABELKIT_GATE_EVIDENCE/pytest" \
  --junitxml "$LABELKIT_GATE_EVIDENCE/junit.xml"
```

先运行 `::test_real_local_context_overflow_shape` 单项确认实际错误，再运行整份门禁。
单项与整份执行使用各自新的临时根目录，完整测试内部复用本轮错误观测，不以缓存响应替代新请求。
成功要求所有矩阵用例实际执行、无 skip、有新工件并通过独立检查；本地门禁不替代正式 DeepSeek/z.ai 发布门禁。
