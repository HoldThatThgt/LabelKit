## 3.1 M1 配置管理 config

### 3.1.1 职责与边界

**做：**装载并语法/语义校验 config.toml 与 project.toml；合并 CLI 覆盖项；解析 rubric（内联或默认包）；装载并预校验用户 JSON Schema；读取 API Key 环境变量；产出全局唯一的不可变 `ResolvedConfig`。 
**不做：**不接触输入数据；不发起网络请求（`--probe` 连通性探测委托 M9）；运行期不提供任何可变配置。

### 3.1.2 输入 / 输出

| 方向 | 内容 |
|---|---|
| 输入 | config.toml 路径、project.toml 路径、CLI 参数字典（input/output/limit/strict/log_level/dry_run）、进程环境（仅读取 profile 声明的 `api_key_env` / `api_key_envs`（v1.6）所列变量）。 |
| 输出 | `ResolvedConfig`（frozen dataclass 树：第 5 章两文件全部键的类型化镜像，另含 CLI 专属项 limit/strict/dry_run 与 log_level 覆盖），或抛出 `ConfigError`（附带全部而非首个校验错误，一次性反馈）。 |

### 3.1.3 API

```
def load(config_path: Path, project_path: Path, cli_overrides: CliOverrides) -> ResolvedConfig:
    """三源合并 + 全量校验。失败抛 ConfigError(errors: list[str])，CLI 以退出码 2 结束。"""

def default_rubric(name: Literal["default:text", "default:ui"]) -> Rubric:
    """从包内数据文件（labelkit/data/rubrics/*.toml）装载系统默认 rubric。"""
```

### 3.1.4 校验规则（启动时全量执行）

| 类别 | 规则 |
|---|---|
| TOML 结构 | 两文件均须含 `schema_version = 1`；未知键报 warning（前向兼容）、缺失必填键报 error；类型逐字段核对（第 5 章字段表即校验依据）。 |
| Profile 引用 | `quality.llm / annotate.llm / generate.llms（数组，逐元素校验）/ verify.llm / output.repair_llm`，以及 `quality.judges / verify.judges`（数组，非空时须为奇数个）引用的 profile 必须存在于 config.toml `[llm.*]`；启用视觉输入的阶段（UI 模态的 quality/annotate/verify）要求其 profile `supports_vision = true`。v1.2：`dedup.semantic = true` 时 `dedup.semantic_embedding` 必须存在于 config.toml `[embedding.*]` 且其密钥配置通过本表「API Key」行校验（v1.6：`api_key_env` / `api_key_envs` 恰其一；5.1）。 |
| 交叉字段约束（v1.2） | `quality.selection = "top_ratio"` 时 `quality.top_ratio` 必填且 ∈ (0,1]，且不得再设 `quality.threshold`（互斥，报 CONFIG_ERROR）；`annotate.self_consistency` 为 0 或 ≥3 的奇数；`generate.mixture = "weighted"` 时 `generate.weights` 必填、逐项为正且长度 = `generate.llms`；`[[generate.styles]]` 各项 name 表内唯一、prompt 非空（5.2 各行标注的 M1 校验在此汇总执行）。 |
| 运行模式（v1.4） | `run.mode="generate_only"` 时：`run.input` 必须缺省、`run.modality` 必须 "text"、`generate.enabled` 必须 true；`generate.seed_examples`（非空字符串数组，逐项非空）与 `generate.standalone_count`（≥ 1）恰好提供其一（互斥，分别对应种子池 / 无种子形态）。process 模式下这两键均不得设置。 |
| API Key | 每个被引用 profile 的 `api_key_env` 环境变量必须存在且非空。v1.6 密钥池：`api_key_env` 与 `api_key_envs`（5.1）恰提供其一（两者皆有或皆无均报错）；`api_key_envs` 须为非空数组、逐项非空且互异；被引用 profile 的**每个**列出变量均须存在且非空（逐个缺失逐条聚合报错）。M1 归一化：标量形式解析为长度 1 的密钥池，运行时只有一条代码路径（3.9.3 密钥池行）。 |
| 用户 Schema | 必须是合法 JSON 且通过 JSON Schema draft 2020-12 元 Schema 校验（jsonschema 库 `Draft202012Validator.check_schema`）；顶层 type 必须为 object；顶层不得声明保留键 `_meta`。 |
| Rubric | criteria 非空、key 唯一且为 `[a-z0-9_]+`、weight > 0；pointwise 模式要求每条 criterion 提供 `pointwise_levels`（恰好 6 级，0–5）。 |
| 阶段组合 | 2.3.1 节的四条组合约束（①–④；④ 与本表「运行模式」行联动）。 |
| 路径 | process 模式：input 存在且可读，且 output 不得位于 input 目录内部（防止自吞）；generate_only 模式无 input，本行仅执行 output 检查（见「运行模式」行）。两种模式均要求 output 父目录存在且可写。 |

### 3.1.5 错误处理

所有校验错误聚合为一个 `ConfigError`，逐条打印（格式 `config.toml:[llm.default].timeout_s: 期望正整数，得到 "abc"`；数组表元素定位写作 `[[rubric.criteria]][N]`，N 为 1 起序号），退出码 2。不存在运行期配置错误——这是 M1 对其他模块的契约。

**背书：**「声明式配置 + 启动期全量校验 + 运行期只读」是 Data-Juicer 配方（recipe）体系 [4] 与 distilabel Pipeline 先验校验（在任何推理发生前校验 DAG 与列契约）[5] 的共同设计；TOML 双文件分层对应其「系统配置 / 数据配方」分离。

### 3.1.6 输入 / 输出示例

贯穿示例（文本模态）：对输入法采集的中文指令做意图标注，输入数据行形如 `{"instruction": "帮我写一条请假条，明天上午要去医院", "source": "ime-log", "ts": "2026-06-30T10:12:00Z"}`（格式规格见 6.1）。注意：M1 不读取数据内容，仅按 3.1.4 校验 input 路径存在且可读。

#### 示例 ①：一次成功装载 —— 三源合并与优先级生效

```
$ export LABELKIT_KEY_DEFAULT=sk-********
$ labelkit run --config config.toml --project project.toml --limit 100
```

config.toml 沿用 5.1 完整示例（含 `[llm.default]` 与 `[llm.judge]` 两个 profile），并在 `[llm.default]` 中显式写入 `temperature = 0.0`；project.toml 节选：

```
# ─── project.toml（意图标注工程，节选）───
schema_version = 1

[run]
input = "./ime-logs/2026-06-30.jsonl"
output = "./out/intent-0630.jsonl"
modality = "text"
batch_size = 128                    # 覆盖内置默认 256

[input]
text_field = "instruction"

[annotate]
llm = "default"
instruction = "你是输入法指令理解标注员。判断每条用户指令的意图类别、话题与完成难度。"

[output]
schema_inline = """
{"$schema": "https://json-schema.org/draft/2020-12/schema", "type": "object",
 "properties": {
   "intent": {"type": "string", "enum": ["writing_assist", "qa", "translation", "chitchat", "other"]},
   "topic": {"type": "string"},
   "difficulty": {"type": "string", "enum": ["easy", "medium", "hard"]}},
 "required": ["intent", "topic", "difficulty"], "additionalProperties": false}
"""
```

三源合并后的 `ResolvedConfig` 摘录（冻结后运行期只读；`# ←` 标注每项的生效来源）：

```
run.batch_size              = 128             # ← project.toml [run]（覆盖内置默认 256）
run.seed                    = 0               # ← 内置默认（两文件与 CLI 均未提供）
limit                       = 100             # ← CLI --limit（最高优先级：CLI 参数 > project.toml > config.toml）
tool.log_level              = "info"          # ← config.toml [tool]（CLI 未传 --log-level，不触发覆盖）
llm.default.temperature     = 0.0             # ← config.toml [llm.default]（project.toml 无对应覆盖键）
llm.default.max_concurrency = 8               # ← config.toml [llm.default]
annotate.llm                = "default"       # ← 内置默认（已校验 profile 存在于 config.toml [llm.*]）
quality.rubric              = "default:text"  # ← 内置默认（缺省按 run.modality = "text" 自动选定）
```

API Key 校验按「被引用 profile」收敛：本工程 generate/verify 均未启用、quality 与 annotate 均引用 default，故 M1 只要求 `LABELKIT_KEY_DEFAULT` 存在且非空；`[llm.judge]` 未被引用，`LABELKIT_KEY_JUDGE` 缺失不报错（3.1.4）。

#### 示例 ②：聚合校验失败 —— 3 个典型错误一次性反馈

在示例 ① 的 project.toml 上引入 3 处错误（节选，其余内容不变）：

```
[quality]
llm = "fast"                        # 错误①：config.toml 只声明了 [llm.default] 与 [llm.judge]
rubric = "inline"

[[rubric.criteria]]
key = "intent_clarity"
weight = 2.0
description = "指令意图是否清晰可辨"
pairwise_prompt = "哪条指令的意图更清晰？"

[[rubric.criteria]]
key = "Topic-Match"                 # 错误③：含大写与连字符，违反 [a-z0-9_]+
weight = 1.0
description = "话题是否明确可归类"
pairwise_prompt = "哪条指令的话题更明确、更易归类？"

[output]
schema_inline = """
{"type": "object",
 "properties": {
   "intent": {"type": "string", "enum": ["writing_assist", "qa", "translation", "chitchat", "other"]},
   "topic": {"type": "string"},
   "difficulty": {"type": "string", "enum": ["easy", "medium", "hard"]},
   "_meta": {"type": "object"}},
 "required": ["intent", "topic", "difficulty"], "additionalProperties": false}
"""
# ↑ 错误②：properties 顶层声明了保留键 _meta（3.1.4 禁止；该键为 6.3 输出信封，由工具写入）
```

M1 按 3.1.5 规定聚合为单个 `ConfigError` 逐条打印（不在首错即停，且未发起任何网络请求）：

```
$ labelkit run --config config.toml --project project.toml --limit 100
ConfigError: 3 个配置错误（全量聚合反馈）
project.toml:[quality].llm: 引用的 profile "fast" 不存在于 config.toml [llm.*]，可用：default、judge
project.toml:[output].schema_inline: 用户 Schema 顶层不得声明保留键 "_meta"（6.3 信封字段由工具写入），得到 properties 含 "_meta"
project.toml:[[rubric.criteria]][2].key: 期望匹配 [a-z0-9_]+，得到 "Topic-Match"
$ echo $?
2
```

三条错误分属 3.1.4 校验表的「Profile 引用」「用户 Schema」「Rubric」三类，按该表行序输出。`labelkit validate --config config.toml --project project.toml` 会产生完全相同的错误清单与退出码 2（2.4），适合在提交长任务前做零成本的纯本地检查。
