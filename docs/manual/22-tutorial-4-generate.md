# 第 22 章　教程四：从零合成数据集（generate_only）

> **难度：★★★☆☆**
> 舞台：`examples/text/project-synth.toml`——文本示例工程的纯生成变体：没有一条输入数据，
> 从 3 条手写种子出发合成一个带标注的小数据集。（同目录 `project.toml` 里的 generate 是
> **process 模式**的另一形态——过质量门的记录当种子、扩充样本回流治理，见第 12 章。）
> 目标：理解纯生成模式的完整链路（合成 → 去重 → 打分 → 标注），学会用桶统计验收多样性，
> 并掌握种子池 / 无种子两种形态的选型。

## 22.1 工程配置解剖

`examples/text/project-synth.toml` 的骨架（完整文件见仓库）：

```toml
[run]
output = "./out/text-synth.jsonl"
modality = "text"
mode = "generate_only"            # ← 纯生成：注意没有 run.input（写了反而报错）
batch_size = 8
seed = 7

[generate]
enabled = true
llms = ["default"]
instruction = """
生成中文输入法用户可能向 AI 助手提出的一句话请求。要求贴近真实使用场景、
类型多样（写作协助、翻译、问答、闲聊等），长度 10–60 字。
"""
num_per_call = 4                  # 每次调用要 4 条
num_per_record = 2                # 每条种子期望产 2 条
temperature = 0.9                 # 生成要撒开（其余阶段仍是 0）
seed_examples = [                 # ← 种子池形态：3 条手写例句
  "帮我写一条请假条，明天上午要去医院复查",
  "把这句话翻译成英文：项目进度符合预期",
  "解释一下什么是复利，举个例子",
]

[[generate.styles]]               # 两个风格模板，每次调用随机抽一个
name = "concise"
prompt = "请求应当简短直接，一句话说清诉求。"
[[generate.styles]]
name = "detailed"
prompt = "请求应包含具体的背景与约束条件（时间、对象、格式要求等）。"

[dedup]
enabled = true                    # 只控制产出是否再过一遍去重工位；生成品的 Self-Instruct
                                  # 相似度过滤内置在 generate 算子里、无条件执行（复用本节的
                                  # minhash_threshold 等参数），关掉本开关它也照跑。
                                  # 所以要紧的是别放松本节阈值——过滤器读的就是它
[quality]
enabled = true
mode = "pointwise"                # 只打分不过滤（没设 threshold）
[annotate]
enabled = true                    # 合成品照样标注（intent/topic/difficulty 那套 Schema）
instruction = """……"""
```

先心算一遍账（跑之前就该会算）：调用数 = ⌈种子 3 × num_per_record 2 / num_per_call 4⌉ = **2 次生成调用**，期望产出 **8 条**；每条再花 4 次 pointwise 打分 + 1 次标注。

## 22.2 运行与对账

```bash
cd examples/text && mkdir -p out
set -a && source ../../.env && set +a
uv run labelkit run --config ../config.toml --project project-synth.toml
```

```
scanned=0  ingested=0  bad_input=0  generated=8
dropped_dup=0  dropped_lowq=0  dropped_verify=0  failed=0  emitted=8
```

纯生成模式的账目特征：`scanned=0`（没有输入这回事），守恒等式退化为 `emitted + dropped_* + failed = generated`（8 = 8 ✓）。本次 8 条全部活过去重——种子多样、温度 0.9，两次调用没产出雷同货。

产物长这样（前三行，已剥 `_meta`）：

```json
{"intent": "writing_assist", "topic": "产品介绍文案（智能保温杯）", "difficulty": "medium"}
{"intent": "qa", "topic": "人工智能面试问题及参考答案", "difficulty": "hard"}
{"intent": "translation", "topic": "商务周报中译日（商务敬语）", "difficulty": "hard"}
```

注意：**合成品拿到的是和真实数据完全一样的待遇**——先被打分（`_meta.scores` 俱全）、再被标注、结构照样过 Schema 引擎。这就是「产物照常走全套治理」的含义。

## 22.3 溯源与桶统计

每条合成记录的 `_meta.source`：

```json
{"file": "", "pair_index": null, "generated_from": [], "fields": {},
 "generator": {"llm": "default", "style": "detailed"}}
```

- `generator ≠ null` = 合成品的**唯一可靠标识**（第 12 章）；
- `generated_from` 在纯生成模式下**恒为空**——种子是配置文本不是记录，要审计种子直接看 project.toml；
- style 记录了这条出自哪个风格桶。

report.json 的桶统计：

```json
"generate": {"buckets": {"default×detailed": {"calls": 2, "produced": 8, "survived_dedup": 8}}}
```

咦，两次调用都抽中了 `detailed`？——style 是**均匀随机**抽的（由 `run.seed=7` 决定），两次调用同一风格的概率是 50%，本次恰好如此。换个 seed 或加大规模，桶会自然摊开。这提醒我们：**小规模下风格覆盖靠运气，大规模下才靠期望**；如果两个风格的配比对你重要，规模要够大（几十次调用起），或干脆拆成两个工程分别跑。

`survived_dedup / produced = 8/8 = 100%`——新颖率满分。当你看到某桶掉到 60% 以下，按第 12.5 节的顺序处置（改 style prompt → 提温度/加模型 → 降 num_per_call），**别放松 dedup**。

## 22.4 变奏一：规模化 + 质量闸

把这个玩具工程变成能交付的合成数据管线，加三样东西：

```toml
[generate]
# 种子池扩到 20~50 条，覆盖你想要的全部类型光谱
seed_examples = [ "…", "…", … ]
num_per_record = 10               # 每种子产 10 条：50 种子 ⇒ 125 次调用 ⇒ ~500 条
llms = ["default", "judge"]       # 双模型轮转，对抗单模型口味
mixture = "round_robin"

[quality]
mode = "pointwise"
threshold = 0.4                   # 合成品也要过质量线——低质量合成品比低质量真数据更危险

[verify]
enabled = true                    # 标注还要过独立评审
llm = "judge"
policy = "drop"
```

合成数据的特殊风险是 **model collapse**（第 12 章）：模型生成的数据再喂给模型，分布会收窄。工程上的三道保险：质量闸（threshold）拦住平庸品、dedup 拦住重复品、`generator` 字段让下游随时能控制真实:合成配比。

## 22.5 变奏二：无种子形态（standalone_count）

一条例句都不想写？删掉 `seed_examples`，换成：

```toml
[generate]
instruction = """扮演一位刚开始用智能手机的长辈用户，生成他们可能向 AI 助手
提出的一句话请求：操作求助、健康咨询、与子女沟通的代写需求等，口语自然。"""
standalone_count = 200            # 目标产出条数；调用数 = ⌈200/4⌉ = 50
```

两种形态的选型：

| | 种子池（seed_examples） | 无种子（standalone_count） |
|---|---|---|
| 你有什么 | 几条到几十条典型例句 | 只有一段能说清楚的描述 |
| 多样性来源 | 种子的覆盖面 × styles | instruction 的开放度 × styles |
| 风格贴近度 | 高（有样学样） | 看指令写功 |
| 论文原型 | Self-Instruct | Persona Hub / Cosmopedia |

两者互斥（同时设置报配置错误）。无种子形态对 instruction 的写功要求更高——把「谁在说话、什么场景、什么体裁、什么长度」都写进去，再用 styles 分桶（12.7 节的收放心法）。

## 22.6 本教程的可迁移结论

1. 纯生成的账目先心算再开跑：调用数与产出量都是配置的确定函数；
2. 合成品走全套治理不是仪式——Self-Instruct 相似度过滤由 generate **内置**实现（`survived_dedup` 即其产物，复用 `[dedup]` 的 MinHash 参数、不受 enabled 开关影响），质量闸是 collapse 的保险；
3. `generator` / `generated_from` 的语义（后者纯生成下恒空）决定了你下游怎么分拣；
4. 桶统计 = 多样性的验收单：盯 `survived_dedup / produced`；
5. 小规模下随机抽取（style、weighted 模型）有方差，配比敏感就拆工程或上规模。
