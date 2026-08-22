# 第 22 章　教程四：从零合成独立样本

> **难度：★★★☆☆**
> 舞台：`examples/text/project-synth.toml`。它没有输入文件，从三条手写种子生成独立文本，
> 再走 dedup → quality → annotate。

## 22.1 先看入口差异

普通 `examples/text/project.toml` 是 process：输入记录过质量门后可以成为生成种子。
`project-synth.toml` 是 `generate_only`：没有 `run.input`，种子直接写在 `generate.seed_examples`。

```toml
[run]
output = "./out/text-synth.jsonl"
modality = "text"
mode = "generate_only"
batch_size = 8
seed = 7

[generate]
enabled = true
form = "flat"
llms = ["default"]
instruction = """
生成中文输入法用户可能向 AI 助手提出的一句话请求。要求贴近真实使用场景、
类型多样，长度 10–60 字。
"""
num_per_call = 4
num_per_record = 2
temperature = 0.9
seed_examples = [
  "帮我写一条请假条，明天上午要去医院复查",
  "把这句话翻译成英文：项目进度符合预期",
  "解释一下什么是复利，举个例子",
]
```

`form = "flat"` 表示每条输出是独立样本。它与第 27 章的 sequence form 参数互斥。

## 22.2 多样性来自哪里

这份工程同时使用三种多样性来源：

- seed examples 决定任务分布的起点；
- `styles` 每次抽一个风格提示；
- `temperature` 控制内容变化。

```toml
[[generate.styles]]
name = "concise"
prompt = "请求应当简短直接，一句话说清诉求。"

[[generate.styles]]
name = "detailed"
prompt = "请求应包含具体背景与约束条件。"
```

种子、profile 与 style 共同形成报告桶。某个桶 produced 很高但 survived-dedup 很低，说明它在批量制造近重复；
先改 instruction/style，而不是立刻放松 dedup 阈值。

## 22.3 为什么还要回流治理

flat generate 内部先把新样本与种子、同批样本做 MinHash 相似度过滤。存活样本再从 dedup 起回流普通流水线：

```text
generate candidate
  -> built-in similarity filter
  -> dedup against dataset
  -> pointwise quality
  -> annotate with user Schema
  -> main / rejects
```

生成子批不会再次进入 generate，所以不存在无限递归。最终 `_meta.source.generated_from` 记录种子 ID，
`_meta.source.generator` 记录 profile 与 style。

## 22.4 运行与对账

```bash
cd examples/text
mkdir -p out
uv run labelkit validate --config ../config.toml --project project-synth.toml
uv run labelkit run --config ../config.toml --project project-synth.toml --dry-run
uv run labelkit run --config ../config.toml --project project-synth.toml
```

dry-run 不发 LLM，请先用它核对调用估算。正式运行后：

```bash
jq -s 'length' out/text-synth.jsonl
jq -s 'map(._meta.source.generator.style) | group_by(.) |
       map({style: .[0], rows: length})' out/text-synth.jsonl
jq '.generate.buckets, .llm_usage, .schema_engine.resolved_at' out/text-synth.report.json
```

真实 LLM 内容可能变化，所以不要把一次运行的样本数或桶分布抄成永久保证。稳定契约是配置目标、守恒关系、
Schema 合法性与报告字段。

## 22.5 加一个业务 validator

JSON Schema 保证结构，`generate.sample_validator` 可以过滤业务语义：

```toml
[generate]
sample_validator = "hooks.py:validate_sample"
```

回调签名是 `fn(text) -> list[str]`。返回非空列表会直接剔除候选并计入
`generate.buckets.*.rejected_by_validator`；它不是 LLM repair 层。模块引用相对 project root 解析。

## 22.6 无种子生成

如果没有代表性种子，用 `standalone_count` 取代 `seed_examples`：

```toml
[generate]
enabled = true
form = "flat"
llms = ["default"]
standalone_count = 100
instruction = """
生成真实客服用户的一句话问题。覆盖物流、退款、发票与账号四类；
每条必须给出一个具体背景，不要包含个人敏感信息。
"""
```

`seed_examples` 与 `standalone_count` 互斥。无种子时 instruction 要把说话者、场景、体裁、长度和禁止事项写清，
否则模型会自己收窄分布。

## 22.7 什么时候转向 sequence

如果样本的正确性取决于角色知识、状态变化、事件顺序、时间间隔或反事实对照，就不要继续把文本拼接在 flat 里。
改用 `generate.form = "sequence"`，让 pattern、state Schema、counterfactual set、独立 evaluator 与 replay
共同约束完整序列。完整教程见第 27 章。

## 22.8 可迁移结论

- flat 适合独立样本；sequence 适合具有因果和时序关系的完整事件组。
- 生成内容必须重新经过去重、质量与 Schema 约束，不能把“模型生成”当作质量证明。
- 先看 validator rejection 与 survived-dedup，再调整 instruction、styles 和 temperature。
- 先 dry-run，再做小规模真实运行；不要用一次非确定性运行的行数代替配置契约。
