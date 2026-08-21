# 第 22 章　教程四：从零合成数据集（generate_only）

> **难度：★★★☆☆**
> 舞台：`examples/text/project-synth.toml`——文本示例工程的纯生成变体：没有一条输入数据，
> 从 3 条手写种子出发合成一个带标注的小数据集。（同目录 `project.toml` 里的 generate 是
> **process 模式**的另一形态——过质量门的记录当种子、扩充样本回流治理，见第 12 章。）
> 时间流生成已在 v1.17 收口为有限场景规划与精确交付；完整新键、quota、schedule、duration/resource、structured noise 与 replay 验收见第 27 章。

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
enabled = true                    # 只控制产出是否再过一遍去重算子；生成品的 Self-Instruct
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

纯生成模式的账目特征：`scanned=0`（没有输入这回事），守恒恒等式退化为 `emitted + dropped_* + failed = generated`（8 = 8 ✓）。本次 8 条全部活过去重——种子多样、温度 0.9，两次调用没产出雷同货。

产物长这样（前三行，已剥 `_meta`）：

```json
{"intent": "writing_assist", "topic": "年终总结润色", "difficulty": "medium"}
{"intent": "writing_assist", "topic": "PPT大纲/产品演示", "difficulty": "medium"}
{"intent": "qa", "topic": "儿童科普绘本推荐", "difficulty": "easy"}
```

注意：**合成品拿到的是和真实数据完全一样的待遇**——先被打分（`_meta.scores` 俱全）、再被标注、结构照样过结构引擎。这就是「产物照常走全套治理」的含义。

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

## 22.4 变奏一：规模化 + 质量门

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

合成数据的特殊风险是 **model collapse**（第 12 章）：模型生成的数据再喂给模型，分布会收窄。工程上的三道保险：质量门（threshold）拦住平庸品、dedup 拦住重复品、`generator` 字段让下游随时能控制真实:合成配比。

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

## 22.6 变奏三：合成的不是一条文本，是一条时间流（v1.17）

前两个变奏调的都是「产多少条独立文本」。要的样本单位若是**一段活动**——多轮请求按时间先后连成一条会话、几条会话交织成一条带时间戳的流——就换 `generate_only` 的第三形态 `[generate.stream]`：

```toml
[stream]                          # 生成侧的铺设契约（复用摄取侧词汇，故工件可重放）
order_by = "meta:ts"
gap_s = 3600

[generate.stream]
enabled = true
crossed_sessions = 1                      # 目标会话数；最终 crossed_sessions 按 survivor projection 计
noise_ratio = 0.1                 # 掺入无关干扰帧
duplicates = 1                    # 原样重发一条序列（判重演示位）
frame_gap_s = [5, 60]             # 未被显式 time_s 覆盖的相邻任务帧间隔

[classify]
enabled = true                    # 类表是配额载体；标签生成期已知、直接继承（零判决调用）

[[generate.stream.quotas]]
name = "six_sequences"
period = "schedule"
counts = { ticket_booking = 3, smart_home = 3}                     # 每类 delivery target

[class.ticket_booking.generate]
instruction = """围绕同一次购票请求生成连贯的多帧对话。"""
len_range = [4, 5]

[class.smart_home.generate]
instruction = """围绕同一个居家场景生成连贯的多帧设备指令。"""
len_range = [4, 5]
```

在最小可运行配置中还应显式给出帧间隔与约束面：

```toml
[[generate.stream.frame_rules]]
template = "chain_response"
source = "task_request"
target = "acknowledgement"
time_s = [1200, 2400]
correlation = { operator = "equal", source_field = "subject_id", target_field = "subject_id" }

[[generate.stream.frame_windows]]
frame_class = "task_request"
of_day = [["08:00", "11:00"], ["14:00", "17:00"]]
of_week = ["mon", "tue", "wed", "thu", "fri"]
```

与本教程的种子池形态不同，时间流同时交付序列主输出与逐帧工件。可运行工程 `examples/synth-stream` 到 v1.17 还演示联合规则：request 必须紧邻 acknowledgement，二者相隔 20–40 分钟且 `subject_id` 类型敏感相等；request 只能落在工作日早/下午窗口，ticket_booking 用按类窗口整表覆盖；序列 hook 再检查首尾与位置连续性。每个 owner 内相邻任务帧还须满足闭合 replay guard（`1us ≤ delta < gap_s`），因此同一工件配同一 `[stream]` 声明可重放而不误切会话。帧类词、session、crossing、timestamp 与噪音槽在内容调用前冻结，模型只写 brief 和 payload。真实调用数与工件行数以第 27 章本版验收记录为准，不把旧版本样本数当保证。

## 22.7 本教程的可迁移结论

1. 纯生成的账目先心算再开跑：调用数与产出量都是配置的确定函数；
2. 合成品走全套治理不是仪式——Self-Instruct 相似度过滤由 generate **内置**实现（`survived_dedup` 即其产物，复用 `[dedup]` 的 MinHash 参数、不受 enabled 开关影响），质量门是 collapse 的保险；
3. `generator` / `generated_from` 的语义（后者纯生成下恒空）决定了你下游怎么分拣；
4. 桶统计 = 多样性的验收单：盯 `survived_dedup / produced`；
5. 小规模下随机抽取（style、weighted 模型）有方差，配比敏感就拆工程或上规模；
6. 三种形态按**要什么样本单位**选：一条独立文本（种子池 / 无种子）还是一段有时间维度的活动（时间流生成）——后者的账目、产物与调参口径自成一套，见第 27 章。
