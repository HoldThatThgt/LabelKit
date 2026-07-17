# 第 24 章　分类算子 classify 与按类条件化：先分拣，再按类加工

> classify 是 v1.7 新增的算子：让 LLM 按**你定义的类别表**给每条记录做封闭集分类，
> 然后允许下游的打分、标注、生成、评审**按类取不同的参数**。
> 读完本章你应当能回答三个问题：**什么时候值得开分类？类别表怎么写才分得准？
> 哪些参数能按类覆盖、哪些永远全局？**
> 本章样例全部来自 `examples/text` 的真实运行（三个示例工程全都开了 classify——
> UI 工程的视觉分类见第 21 章、时序流的序列分类见第 25/26 章；本章用文本工程讲机制）。

## 24.1 为什么要分类：一套全局配置治不了混合数据

真实采集来的数据几乎总是**混合**的：同一份输入法日志里，既有「帮我写一条请假条」这样的写作请求，也有「二分查找为什么是 O(log n)」这样的知识问答，还有「哈哈哈哈哈哈哈」这样的纯噪声。而 v1.7 之前的流水线只有**一套全局配置**：一把质量尺子（rubric）、一条质量线（threshold）、一份标注指令（instruction），所有记录一视同仁。

一视同仁的代价在第 20 章你已经亲眼见过：`default:text` 这把尺子的口径偏向知识密度，日常写作请求被系统性压低到生死线边缘——同一条 0.3 的线，对问答类刚好、对写作类就是屠杀。标注侧同理：想让指令同时照顾写作、问答、翻译、闲聊，只能写得面面俱到，结果每一类都只得到稀释过的判据。**不是参数没调好，是「一套参数」这个前提对混合数据不成立。**

classify 算子把这个前提换掉了：先让 LLM 当**分拣员**——按你在 `[[classify.classes]]` 里声明的类别表，给每条存活记录贴一个类标签（写进 `_meta.classification`）；然后用 `[class.<类名>.<节>]` 给某些类**覆盖**某些下游参数——写作类质量线放宽、问答类收严、各类各用各的标注指令。分拣台在流水线上的位置是**去重之后、打分之前**（dedup → classify → quality → generate → annotate → verify）：重复件不浪费分类调用；而「按类打分」要求类归属在打分前就绪。它自己不淘汰任何记录——分类不是质量门，是**路由**。

「先分类、按类走不同加工策略」不是 LabelKit 的发明：NVIDIA 的 Nemotron-CC 在 6.3 万亿 token 级的生产中用质量分档路由不同的合成管线，NeMo Curator 把「分类器打标驱动路由」产品化成了标准管线阶段。LabelKit 把这个模式装进了单机流水线，分类器用的是运行时 LLM API + 内部 Schema 的 enum 硬约束——标签**只可能**出自你的类别表，词表外的输出在结构层就被拦下。

## 24.2 快速上手：examples/text 全流程

仓库自带的 `examples/text` 是一个混合意图工程：14 条输入（写作协助、知识问答、翻译、闲聊混杂，掺 1 条一字不差的重复与 1 条近似改写），把纯文本格式能开的算子全开——dedup → **classify** → quality（pointwise，按类门槛）→ generate（按类种子池回流，第 12 章）→ annotate（按类指令）→ verify。本章只盯分拣台与按类覆盖，逐节看它的 `project.toml`。

**第一节：类别表与兜底类。**

```toml
[classify]
enabled = true
llm = "default"
assignment = "single"          # 锁定一条一类；multi 变体见 24.3
fallback_class = "other"

[[classify.classes]]
name = "writing"
description = "写作协助类请求：代写、改写、模板、文案等需要模型产出一段文本的请求"
examples = ["帮我写一条请假条，明天上午要去医院"]

[[classify.classes]]
name = "qa"
description = "知识问答与解释类请求：询问事实、原理，或要求讲解概念、给出计算过程"

[[classify.classes]]
name = "translation"
description = "翻译类请求：中外互译，含要求保留语气或意境的翻译"

[[classify.classes]]
name = "other"
description = "不属于以上任何一类的请求（闲聊、无明确诉求等）"
```

要点三个：① 类别表**至少两项**，`name` 用 `[a-z0-9_]+`（它会成为配置节名、`_meta` 字段值、报告键，别用中文）；② `description` 是 LLM 能看到的**全部类语义**——分得准不准，八成取决于这句话怎么写（24.7 有一个真实翻车展品）；③ `fallback_class` 必填且必须是表内成员——它既是分类失败时的兜底去向，也是 LLM 可以主动选择的普通类（所以 other 的 description 写成「不属于以上任何一类」的排他形态）。`examples` 可选，是**输入侧** few-shot：给边界样本一个锚点。

**第二节：按类覆盖。**全局打分是 pointwise + 0.25 的线，然后给两个类各开小灶：

```toml
[quality]
enabled = true
mode = "pointwise"
llm = "default"
threshold = 0.25
rubric = "default:text"

# ── 按类覆盖：未出现的键一律继承全局 ──
[class.writing.quality]
threshold = 0.2                # 写作类门槛放宽

[class.qa.quality]
threshold = 0.4                # 问答类门槛收严
```

标注侧同理——全局指令管兜底，两类各配专属指令：

```toml
[class.writing.annotate]
instruction = """
你是写作协助类请求的意图标注员。这条请求已被判定为写作协助类：
标注 intent（通常为 writing_assist）、主题（要写什么）与写作难度。
"""

[class.qa.annotate]
instruction = """
你是知识问答类请求的意图标注员。这条请求已被判定为知识问答类：
标注 intent（通常为 qa）、主题（问的是什么知识点）与回答难度。
"""
```

注意 `other` 与 `translation` 两类**什么都没配**——未出现的键一律继承全局：这两类记录用全局 0.25 的线、全局标注指令。合并优先级是 `[class.<类名>].<节>.<键>` > `[<节>].<键>` > 内置默认，M1 启动时静态合并冻结，运行期零开销。

**第三节：trace 订阅。**分类判决走独立的 `classify` 通道，**默认不在** `trace.channels` 里（默认值仍是 `["quality","verify","schema"]`），想看分拣员的判决理由必须显式加：

```toml
[trace]
enabled = true
channels = ["classify", "quality", "verify", "schema"]
content = "refs"
```

跑起来：

```bash
cd examples/text && mkdir -p out
set -a && source ../../.env && set +a
uv run labelkit run --config ../config.toml --project project.toml
```

stderr 尾部的终版摘要（真实运行，退出码 0）：

```
   ── 终版摘要（与 report.counts 逐项一致）──
   scanned=14  ingested=14  bad_input=0  generated=12
   dropped_dup=1  dropped_lowq=10  dropped_verify=0  failed=0  emitted=15
```

守恒等式照常成立：`15 + 1 + 10 + 0 + 0 = 26 = 14 + 12`（generate 开着，12 条合成样本回流，第 12 章）。分类本身的账在 `report.json` 新增的 `classify` 节里：

```json
"classify": {
  "assignment": "single",
  "classes": {"writing": 5, "qa": 3, "translation": 2, "other": 3},
  "fallback_count": 0,
  "failures": 0
}
```

读法：去重放行的 13 条输入里，5 条分进 writing、3 条 qa、2 条 translation、3 条 other（5+3+2+3=13，分类不淘汰、账必对齐）；12 条回流的合成样本**不在**这里——它们带着种子的类标签回流（`source="inherited"`，幂等跳过分拣台、零调用，24.7）。`fallback_count=0` 说明没有一条是「分不出来兜底」的——那条「哈哈哈哈哈哈哈」是 LLM **主动**归入 other 的。trace 里能看到它的判决理由（`classify.decision` 事件，本次运行共 13 条，每条存活输入记录一条；`…` 处省略 `run_id`/`batch_no` 字段）：

```json
{"ts": "2026-07-17T02:50:45.573+08:00", …, "stage": "classify", "ev": "classify.decision",
 "record_ids": ["4b9b1283cd63977b"],
 "payload": {"label": "other", "source": "llm",
             "reason": "无明确诉求，仅为无意义的笑声，不属于任何具体类别。"}}
```

分拣之后，质量工位**按类分池**打分、按各自的线门控。trace 的 `quality.gate` 事件多了个 `pool` 字段，三条线同场执法一目了然（从真实 trace 的 25 条门控事件里各池摘一条；`…` 处省略 `ts`/`run_id`/`batch_no`/`stage` 字段）：

```json
{…, "ev": "quality.gate", "record_ids": ["a8aa181766eebd97"],
 "payload": {"aggregate": 0.6, "decision": "keep", "threshold": 0.4, "pool": "qa"}}
{…, "ev": "quality.gate", "record_ids": ["7a5fe0c36babb643"],
 "payload": {"aggregate": 0.2, "decision": "keep", "threshold": 0.2, "pool": "writing"}}
{…, "ev": "quality.gate", "record_ids": ["4b9b1283cd63977b"],
 "payload": {"aggregate": 0.0, "decision": "drop", "threshold": 0.25, "pool": "other"}}
```

最终账目：输入侧 writing 池 0.2 的线拦 1 条、other 池全局 0.25 的线拦 3 条；回流侧 qa 池 0.4 的线拦 4 条、writing 池再拦 2 条（合计 `dropped_lowq=10`），活下来的 15 条按各自类的指令完成标注并通过评审。

**这次运行最值得咂摸的两组数字**：其一，writing 池输入侧五条的聚合分是 0.15、0.2、0.25、0.4、0.5——如果没有按类覆盖、全局一条 0.25 的线，贴线的那条请假条改写（0.2）就死了；0.2 的类内线把它捞了回来。这正是第 20 章亲手诊断过的现象（`default:text` 的口径天然压低日常写作请求）；当时的解法是换 rubric，现在你多了一个更轻的选项：**先分拣，再按类画线**。其二在反方向：qa 池 0.4 的严线放行了输入侧全部三条真问答（0.5~0.75），却把回流的四条合成问答（0.2~0.35）全部拦下——同一把尺子、按类两条线，各管各的分布，合成样本也别想搭便车。

## 24.3 单标签与多标签：assignment 开关

`classify.assignment` 决定「一条数据能属于几类」：

**`"single"`（默认）：锁定一条一类。**内部 Schema 是 `{"class": <enum>}`——LLM 必须从类别表里恰好选一个。每条记录一个标签、一行输出，行与记录一一对应，下游一切照旧。上面的快速上手就是 single 模式。

**`"multi"`：允许多类命中，按标签扇出。**内部 Schema 换成 `{"classes": [<enum>, …]}`（至少 1 个、至多 `max_labels` 个），适合天然多归属的数据——「写一段讲解二分查找原理的教程」既是写作又是问答，锁成单类哪边都亏。配置形态：

```toml
[classify]
enabled = true
assignment = "multi"
max_labels = 2                 # 每条最多命中 2 类；缺省 = 类别数
fallback_class = "other"
# [[classify.classes]] 类别表同 single，略
```

multi 模式的机制要讲清楚（本节没有真实运行样例，`examples/text` 是 single 工程；以下是规格语义）：

- **归一化**：LLM 返回的标签集合先映射到类别表声明序并去重；兜底类与具体类同时出现时剔除兜底类（「其余」和具体命中矛盾），只命中兜底类时保留。
- **扇出 = 一条数据多行结果**：归一化后命中 k ≥ 2 类时，原信封拿首标签（声明序），其余 k−1 个标签各克隆一个「兄弟信封」追加到批尾。兄弟信封**共享**原始记录与去重判定（所以各行的 `_meta.dedup` 一致），但质量分、标注、评审结果**各自独立**——每个信封进自己的类池打分、按自己类的指令标注、独立淘汰、独立产出一行。
- **⚠️ 行唯一键变了**：multi 模式下主输出中**同一个 `_meta.id` 可以出现多行**（每类一行），行唯一键是 **(`_meta.id`, `_meta.classification.label`)**。下游任何拿 `_meta.id` 当唯一键的脚本（join、去重、对账）都要改——这是开 multi 前必须通知下游的契约变更。
- **账目**：扇出净增的信封数计入 `counts.fanout`（仅 multi 时出现在报告里），守恒等式右侧同步扩展：`emitted + dropped_* + failed + bad_input = scanned + generated + fanout`。报告的 `classify` 节另多一个 `multi_label_records`（命中多类的记录数）。
- **`max_labels` 是成本旋钮**：∈ [2, 类别数]，缺省 = 类别数。一条记录命中 m 类，就要付 m 份打分 + m 份标注 + m 份评审的钱——`max_labels` 给这个乘数封顶。类别表大而互斥性弱时，别用缺省值裸奔。附带一提：`--dry-run` 的估算静态算不出实际命中数，multi 下按乘数 1 报**下界**并在 stderr 注明，预算要留余量。

选型：数据天然单归属（意图分类、领域分类）用 single；标签语义是「适用于哪些场景」而非「属于哪一类」时才值得上 multi。拿不准就先跑 single，看 trace 里分拣员的 reason 有没有反复在两类之间挣扎。

## 24.4 按类覆盖白名单：什么能按类、什么不能

`[class.<类名>.<节>]` 不是自由天地——能覆盖的键有**白名单**（M1 强校验），白名单外的键直接报配置错误（退出码 2）。这是「未知键报 warning」惯例的显式例外：按类覆盖写错键名如果只是静默不生效，排查代价太高。

| 节 | 可按类覆盖 | 锁定全局（及理由） |
|---|---|---|
| `[class.*.quality]` | `mode`、`rounds`、`rubric`（含 `[class.*.rubric]` 内联子表）、`threshold`、`selection`、`top_ratio` | `llm` / `judges` / `both_orders` / `criteria_per_call` / `on_unscored`——LLM 绑定属部署与成本面，类间差异优先用 rubric 表达 |
| `[class.*.annotate]` | `instruction`、`examples` | `llm` / `self_consistency` / `sc_temperature` |
| `[class.*.generate]` | `instruction`、`styles`、`num_per_record`、`temperature` | `llms` / `mixture` / `weights` / `seeds_per_call` / `num_per_call` / `sample_validator` |
| `[class.*.verify]` | `extra_criteria` | `llm` / `judges` / `policy` / `max_repair_rounds` |
| —— | —— | `run.*` / `input.*` / `dedup.*` / `classify.*` / `output.*` / `trace.*` **从不按类**——尤其输出 Schema 全局唯一，所有类的产出行必须长一个样 |

白名单之外，三条合并细则值得知道：

1. **选择组按组合并**：threshold 和 top_ratio 本是互斥对（第 10 章）。类里显式提供 `selection` / `threshold` / `top_ratio` 任何一个，合并视图就**整组换掉**全局侧的互斥对键——所以「全局 threshold + 某类 top_ratio」是合法组合，不会误报互斥；
2. **rubric 按类重解析**：类可以换整把尺子（`rubric = "default:ui"` 或配 `[class.X.rubric]` 内联子表）；pointwise 的 6 级量表校验跑在「类有效 mode × 类有效 rubric」的组合上。`[class.X.rubric]` 子表在场但该类 selector 不是 `"inline"` 时，子表被忽略并打 warning（与全局同一惯例）；
3. **类 examples 启动干跑**：`[class.X.annotate.examples]` 的 output 一样要过全局 Schema（与 validator）校验，错误信息会精确定位到 `[[class.<类名>.annotate.examples]][N]`。

## 24.5 纯打标模式：一个覆盖都不配

`[class.*]` 全部省略、只开 `[classify]`，就是**纯打标模式**：所有类走同一套全局工艺，classify 的产出只剩标签本身——`_meta.classification`、`_meta.scores.pool`、报告的 `classify.classes` 分布与 `quality.by_class` 分池统计。没有显式开关，零覆盖自然退化。

别小看这个形态，它有三个正经用途：

- **摸底**：第一跑纯打标，看 `report.classify.classes` 的分布合不合预期、rejects 里被淘汰的都是哪类，再决定要不要给谁开小灶——与第 10 章「先打分后画线」是同一个心法：**先看清楚，再动参数**；
- **下游分拣**：训练侧只需要「按类拆文件」时，标签落在 `_meta` 里就够了（jq 姿势见 24.6）；
- **配比审计**：周报里「各意图类产出占比」这种指标，从此不用再靠标注字段间接推。

反过来的形态也合法：**留配置、关开关**。`classify.enabled = false` 而 `[[classify.classes]]` / `[class.*]` 还在场时，只报一次 warning（点名被忽略的表）、不报错——调试期把开关拨来拨去不用来回删配置。关掉后行为与 v1.6 完全一致，唯一可见差异是 `_meta.classification` 恒在、值为 `null`。

## 24.6 输出怎么读

开了 classify 之后，主输出、拒绝通道与报告各多了什么（trace 的新事件与新字段在 24.2 已经见过）。**主输出**每行多两处（真实运行产物第 4 行——那条贴线幸存的请假条改写，格式化展示）：

```json
{
  "intent": "writing_assist",
  "topic": "请假条（半天病假，去医院复查）",
  "difficulty": "easy",
  "_meta": {
    "id": "7a5fe0c36babb643",
    "run": {"tool": "labelkit/1.0.0", "started_at": "2026-07-17T02:50:37.417380+08:00",
             "project_file": "project.toml", "rubric": "default:text", "seed": 42},
    "source": {"file": "input.jsonl", "line_no": 7, "generated_from": [],
                "fields": {"source": "ime-log"}, "generator": null},
    "stream": null,                      ← v1.8 恒在键（stream 模式未启用恒为 null，第 25 章）
    "scores": {
      "writing_style": 0.4, "facts_trivia": 0.2,
      "educational_value": 0.2, "required_expertise": 0.0,
      "__aggregate__": 0.2,              ← 贴着 writing 类 0.2 的线存活（门控规则是 < 才淘汰）
      "mode": "pointwise", "batch_no": 1,
      "pool": "writing"                  ← 新增：这条记录在哪个类池里打的分
    },
    "dedup": {"kind": "unique"},
    "classification": {                  ← 恒在键：未开 classify 的运行此处为 null
      "label": "writing",                ← 本行的路由标签（这行走的是 writing 类工艺）
      "labels": ["writing"],             ← 命中全集；single 模式恒单元素，multi 下是完整命中列表
      "source": "llm"                    ← "llm" 正常判决 | "fallback" 兜底 | "inherited" 生成样本继承
    },
    "annotation": {"model": "glm-5.2", "attempts": 1},
    "verification": {"verdict": "pass", "rounds": 1}
  }
}
```

三个细节：① `classification` 只落 `label` / `labels` / `source` 三键——判决理由和 sc 统计不落主输出，要看去 trace（`classify.decision` 事件）；② `scores.pool` 与 `classification.label` 恒相等，pool 是打分池的自述，pairwise 模式下「批内相对分」从此变成「**池内**相对分」（见 24.7）；③ 输出 Schema 是全局的——类只改工艺，不改产出结构：本次真跑回流的合成样本带着种子的类标签（`source="inherited"`）、按类指令标注，但 `intent` 字段仍由标注工位从全局枚举里独立选出。

**拒绝通道**每行多一个 `label` 键（真实运行产物第 1、3 行，逐字）：

```json
{"_meta": {"id": "6e60ce3c2d59f04d", "source": {"file": "input.jsonl", "line_no": 1, "generated_from": []}, "stage": "quality", "reason": "below_threshold", "errors": [], "label": "writing"}}
{"_meta": {"id": "6e3ffe368ff0ad29", "source": {"file": "input.jsonl", "line_no": 6, "generated_from": []}, "stage": "dedup", "reason": "exact", "errors": [], "label": null}}
```

第一行：writing 类的记录死于 writing 池的 0.2 线——`label` 让你能按类统计淘汰。第二行 `label` 是 `null`：它死在 dedup 工位，**还没走到分拣台**（链序 dedup → classify），自然没有标签。multi 模式下这个键还承担消歧职责：同 id 的兄弟信封在 rejects 里靠 label 区分。

**报告**的 `quality` 节多了 `by_class`——每个类池一套独立的直方图与准则均值（顶层的直方图/均值仍保留，是全池汇总；真实运行产物按池名字典序排列，`…` 处省略 other / qa / translation 三池的同构内容与其余全为 0 的桶）：

```json
"by_class": {
  "other": {…}, "qa": {…}, "translation": {…},
  "writing": {
    "mode": "pointwise", "rounds": 4,
    "aggregate_histogram": {"0.0-0.1": 1, "0.1-0.2": 2, "0.2-0.3": 3,
                             "0.4-0.5": 1, "0.5-0.6": 2, …其余 5 桶均为 0},
    "per_criterion_mean": {"educational_value": 0.3333333333333333,
                            "facts_trivia": 0.06666666666666668,
                            "required_expertise": 0.2888888888888889,
                            "writing_style": 0.4},
    "per_criterion_tie_rate": {}
  }
}
```

第 10 章「对着直方图画线」的流程从此**按池执行**：给 writing 类画线，看 `by_class.writing` 的直方图（9 条 = 输入 5 + 回流合成 4），别再看全池汇总——混合分布的汇总直方图会把几个类的峰糊在一起。

**下游常用姿势**（延续第 8 章的清单）：

```bash
# 按类拆分主输出
jq -c 'select(._meta.classification.label == "qa")' out/text-labels.jsonl
# 各类产出行数（本次真跑：writing 6、translation 6、qa 3）
jq -r '._meta.classification.label' out/text-labels.jsonl | sort | uniq -c
# 按类统计淘汰去向
jq -r '._meta | "\(.label)\t\(.stage)/\(.reason)"' out/text-labels.rejects.jsonl | sort | uniq -c
```

## 24.7 调优与排障

**三种 `source`，先分清谁是谁。**`"llm"` = 分拣员正常判决（含主动选中兜底类——本次真跑那条笑声进 other 就是 `source="llm"`，所以 `fallback_count=0`）；`"fallback"` = 分类输出经结构修复耗尽仍非法、被**兜底机制**塞进 `fallback_class`；`"inherited"` = generate 按类生成的样本天生带标签、回流时幂等跳过分类（零额外调用）。诊断口径：`report.classify.fallback_count` 持续偏高，说明的不是「数据难分」而是「**分类调用的输出结构不稳**」或类别表口径盖不住数据——先查 trace 的 error 事件，再审 description。

**`classification_invalid` 的两副面孔**（对应 `classify.on_error`）：

- `"fallback"`（默认）：记录**存活**、归兜底类，留痕走三条路——trace 的 error 事件（kind = `classification_invalid`）、`fallback_count` 计数、`Classification` 的内部 detail。注意它**不写** `item.errors`，所以这条记录后续如果死在别的工位，rejects 归因不会被分类失败污染；
- `"fail"`：记录 `failed` 进 rejects，`reason` 就是 `classification_invalid`。适合「标签错了比没有标签更糟」的场景（比如标签直接决定下游训练配比）。

**sc 投票：给分拣员上三个臭皮匠。**`classify.self_consistency = n`（≥3 奇数）时每条记录采样 n 次（温度取 `classify.sc_temperature`，默认 0.7），single 按多数票、multi 逐标签按「出现于过半采样」保留；**无过半不回退首样本，归兜底类**——分不出来就该进兜底，这是它与 annotate 字段级投票语义上的不同。票型统计（n 与 agreement_ratio）随 `classify.decision` 事件的 `sc` 字段落 trace。成本 ×n，先确认 fallback_count 和边界类的翻转率真的成问题再上。

**类描述是分类质量的第一杠杆。**示例工程的类别表自己就是一个演化展品：早期版本只有 writing / qa / other 三类，实测那条「把这句话翻译成英文……」被分进了 writing——分拣员的理由是「本质上是产出一段目标语言的文本，属于写作协助类请求」。回头看 writing 的 description 结尾：「……**需要模型产出一段文本的请求**」——这个尾巴写得太宽，几乎所有请求都要模型产出文本，翻译就这样被兜进来了。解法就是现在的类别表：给翻译立类、让 description 互相让地盘（本次真跑两条翻译请求全部正确归入 translation）。写类别表的四条纪律：

1. **描述写判据，不写口号**——「代写、改写、模板、文案」是判据，「高质量写作类」是口号；结尾的兜底性短语（「等」「之类」）尤其危险，宽语义会吸走边界样本；
2. **类间互斥靠描述互相「让地盘」**——想让翻译独立成类，就给它立类；封闭集分类**不会发明新类**，你没声明的意图只会被塞进语义最近的类或兜底类；
3. **边界样本放 `examples`**——它是输入侧锚点，比在 description 里堆形容词有效；
4. **裁决规则放 `classify.instruction`**——类间优先级、「拿不准选 X」这类横跨多类的规则，写在这里而不是某个类的 description 里。

**小类池的 pairwise 会退化。**classify 开启后，pairwise 的比较池从「批」变成「批内类池」——第 10 章的警告按池加倍生效：批 256 条、某类只占 5%，那个池就只有十几条，百分位分数极不稳定；池内只剩 1 条时不发起任何比较、直接给 0.5。两条出路：给小类按类覆盖 `mode = "pointwise"`（绝对刻度不吃池小的亏——本章示例工程全程 pointwise 就是这个考虑），或加大 `batch_size` 把池撑起来。选型细节回第 10 章。

**成本账。**分类给每条存活记录追加 1 次调用（sc 时 ×n；`inherited` 的回流样本零调用），dry-run 估算按 ingested 计上界。它通常是流水线里较轻的一环——本次真跑的分账（`report.timing`，真实产物）：

```json
"per_stage_s": {"dedup": 0.01, "classify": 17.608, "quality": 146.357,
                 "generate": 6.436, "annotate": 18.46, "verify": 41.005}
```

14 条输入 + 12 条回流全程约 230 秒、147 次调用，其中分类只占 13 次——quality 仍是大头（第 10 章的结论不因分类而变）。multi 模式的钱花在扇出的**下游**（m 份打分/标注/评审），不在分类调用本身——控成本先控 `max_labels`。

最后一份检查清单，开 classify 前过一遍：类别表 ≥ 2 项且 name 全小写下划线；fallback_class 在表内、description 是排他形态；边界意图要么有自己的类、要么在 instruction 里写了裁决规则；trace.channels 加了 `"classify"`（调优期必开，判决理由全靠它）；multi 的话——下游知道行唯一键变成 (`_meta.id`, `label`) 了吗？
