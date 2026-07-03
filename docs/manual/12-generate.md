# 第 12 章　生成算子 generate：从种子到新数据

> generate 是唯一会**增加**记录数的算子，也是唯一默认关闭的治理路径外算子。
> 本章讲两种工作形态（扩充既有数据 / 从零合成）、多样性的三个来源，
> 以及如何用报告里的桶统计判断"生成的货够不够新"。

## 12.1 直觉：复制车间与它的纪律

把 generate 想成流水线旁边的**仿制车间**：从过了质检的好货里抽几件当样品，让 LLM 照着「风格与题材」仿制新货。纪律有三条，都是为了防止仿品污染货架：

1. **仿品不直接上架**——相似度过滤分两道：第一道**内置在 generate 算子里**、无条件执行（新样本与种子、与同批样本互查，复用 `[dedup]` 的 MinHash 参数——这正是 Self-Instruct 的相似度过滤，`survived_dedup` 统计的就是它的战果）；第二道是生成子批**回流**流水线从 dedup 起重新走一遍（与全部原始记录及先前生成的样本查重）→ 打分 → 标注 → 校验。仿得太像样品的，第一道就被扣下。
2. **只仿一轮**——生成子批不会再触发生成（不递归），杜绝「仿品的仿品」的近亲繁殖。
3. **件件有出处**——每条合成记录带 `generated_from`（种子记录 id 列表）和 `generator`（{"llm", "style"}）双溯源，主输出里 `generator ≠ null` 就是合成货的统一标识。

**仅文本模态可用**（LLM 造不出配套截图），这是启动时的硬约束。

## 12.2 process 模式：给既有数据扩容

在常规流水线上开 `generate.enabled = true`（要求 quality 也开着），每批的流程：

1. **选种子**：批内 `active` 且聚合分 ≥ `seed_min_score` 的记录。`seed_min_score` 不填时自动取 `quality.threshold`；连 threshold 也没有就取批内中位数——**种子必须是好货**，这是仿制质量的根。
2. **算调用数**：`⌈种子数 × num_per_record / num_per_call⌉`。默认 num_per_record=2（每种子期望产 2 条）、num_per_call=4（每次调用要 4 条）。
3. **每次调用**：随机不放回抽 `seeds_per_call`（默认 3）条种子作为示例，system = `generate.instruction`（+ 风格模板，见 12.4），要求输出 `{"samples": ["...", ...]}` 恰好 num_per_call 条，经结构引擎校验。
4. **构造新记录**：每条样本文本包成 `{text_field: 样本}` 的记录（id 规则与 ingest 相同），回流。

调用失败（修复耗尽/重试耗尽）只损失**那一次调用**的样本——种子不受影响，也不产生 failed 记录；该次调用计入报告桶统计（calls 计入、produced 为 0）。

## 12.3 generate_only 模式：无中生有

`run.mode = "generate_only"` 时没有输入数据（`run.input` 必须不设），generate 成为链路起点，产出按 `batch_size` 切批走 dedup → (quality) → (annotate) → (verify) → 输出。两种形态二选一：

**① 种子池形态（Self-Instruct 式）**——你手写几条种子例句：

```toml
[run]
mode = "generate_only"
output = "./out/generated.jsonl"
modality = "text"

[generate]
enabled = true
instruction = """生成中文输入法用户可能向 AI 助手提出的一句话请求。要求贴近真实
使用场景、类型多样（写作协助、翻译、问答、闲聊等），长度 10–60 字。"""
seed_examples = [
  "帮我写一条请假条，明天上午要去医院复查",
  "把这句话翻译成英文：项目进度符合预期",
  "解释一下什么是复利，举个例子",
]
num_per_record = 2      # 调用数 = ⌈3 × 2 / 4⌉ = 2 → 期望产出 8 条
```

种子池就是「样品间」：调用数按 `⌈len(seed_examples) × num_per_record / num_per_call⌉` 算，每次调用从池里抽 `seeds_per_call` 条当示例。Self-Instruct 原文用 175 条人工种子自举出了整个数据集——种子的**多样性**直接决定产出的多样性，写种子时刻意覆盖你想要的类型光谱。

**② 无种子条件化形态（Persona Hub / Cosmopedia 式）**——一条示例都不给，纯靠指令 × 风格驱动：

```toml
[generate]
enabled = true
instruction = """……"""
standalone_count = 500        # 目标产出条数（与 seed_examples 互斥）
# 调用数 = ⌈500 / num_per_call⌉ = 125
```

提示词不含示例段，多样性完全来自 instruction 的开放度和 styles 的分桶（12.4）。适合「我要的类型可以被描述清楚，但没有现成例句」的场景。

两种形态下合成记录的 `generated_from` 恒为空数组（种子不是记录、没有记录 id；种子本身留在 project.toml 里可审计），`generator` 照常携带——所以**判断一条记录是否合成，只看 `generator ≠ null`**。

计数不变量退化为 `emitted + dropped_* + failed = generated`；产出 0 条不算错误（照常写报告、退出码 0）。

## 12.4 多样性的三个旋钮

单一模型反复自生成会让产出分布收窄、长尾消失（model collapse，Nature 2024）。LabelKit 给了三个对抗手段：

**① 温度**：`generate.temperature` 默认 0.9（覆盖 profile 的 0）——生成本来就要撒开。

**② 多模型混合**：`llms = ["default", "judge", ...]`，每次调用按 `mixture` 选一个：

- `"round_robin"`（默认）：按调用序轮转——严格均匀；
- `"weighted"`：按 `weights`（正数数组，长度须等于 llms）加权随机抽。

抽取由 `run.seed` 播种、按调用序号预先抽定，与并发调度顺序无关——同 seed 重跑，每次调用用哪个模型完全一致。

**③ 风格模板**：`[[generate.styles]]` 子表，每次调用均匀抽一个，其 prompt 以 `[风格要求] …` 追加在 instruction 之后：

```toml
[[generate.styles]]
name = "concise"
prompt = "请求应当简短直接，一句话说清诉求。"

[[generate.styles]]
name = "detailed"
prompt = "请求应包含具体的背景与约束条件（时间、对象、格式要求等）。"
```

这是最便宜也最可控的多样性来源：风格即分桶，桶按你的意图切。

## 12.5 用桶统计验收多样性

开生成后，报告多一个 `generate.buckets` 块——每个「模型×风格」组合一行账：

```json
"generate": {
  "buckets": {
    "default×concise":  {"calls": 5, "produced": 20, "survived_dedup": 19},
    "default×detailed": {"calls": 5, "produced": 20, "survived_dedup": 11}
  }
}
```

读法：`survived_dedup / produced` 是**新颖率**。上例 detailed 桶只有 55% 的样本活过去重——这个桶在产重复货。处置顺序：

1. 改该 style 的 prompt，让它约束出更具体的差异化方向；
2. 提高温度或增加模型（12.4）；
3. 降低 `num_per_call`（一次要 8 条比要 4 条更容易在调用内部自我重复）；
4. **不要**放松 dedup 阈值——那是掩耳盗铃。

另一个要监控的比例是主输出里的**合成占比**（`jq 'select(._meta.source.generator != null)' | wc -l` 除以总行数）。合成数据占比过高有 model collapse 风险——下游训练时建议控制真实:合成比例，靠 `generator` 字段随时可分拣。

## 12.6 配置参考

```toml
[generate]
enabled = false               # 默认关
llms = ["default"]            # profile 数组；每个都须存在于 [llm.*]
mixture = "round_robin"       # "round_robin" | "weighted"
weights = []                  # weighted 时必填，正数、长度=len(llms)
instruction = """……"""        # enabled 时必填
num_per_record = 2            # 每种子期望产出条数
seeds_per_call = 3            # 每次调用抽几条种子当示例
num_per_call = 4              # 每次调用要求产出几条
seed_min_score = 0.5          # 种子门槛；缺省=quality.threshold，再缺省=批中位数
temperature = 0.9             # 生成温度（覆盖 profile）
seed_examples = []            # generate_only 种子池形态专用（process 模式不得设置）
standalone_count = 500        # generate_only 无种子形态专用（与 seed_examples 互斥）
# [[generate.styles]] 子表见 12.4
```

## 12.7 instruction 写作要点

生成指令与标注指令的心法不同：标注要**收**（判据明确、边界清晰），生成要**放**（框定题材与体裁，把具体内容留给模型+风格+种子）：

```toml
# 太收：模型只会产出请假条的一百种变体
instruction = "生成用户请AI写请假条的请求。"

# 恰当：框定"是什么"（输入法一句话请求）+ 质量要求（贴近真实、长度），
#       类型光谱交给种子池和 styles 去撑
instruction = """生成中文输入法用户可能向 AI 助手提出的一句话请求。
要求贴近真实使用场景、口语自然、诉求明确，长度 10–60 字。
只借鉴示例的风格与题材范围，不得复述示例内容。"""
```

最后一句「不得复述示例内容」值得抄——它显著降低生成样本贴着种子抄的概率（抄了也会被 dedup 扣下，但那是白花钱的调用）。
