# 第 3 章　五分钟上手：第一个标注工程

> 本章用仓库自带的 `examples/text` 工程走一遍完整流程：看数据 → 看配置 → 试运行 → 正式运行 → 读产物。
> 所有输出都是真实运行的结果，你在自己机器上会看到几乎一样的东西。

## 3.1 任务与数据

任务就是第 1 章那个贯穿全书的例子：把输入法采集的一句话请求，标注成「意图 / 主题 / 难度」。

看一眼输入数据 `examples/text/data/input.jsonl`（共 14 行，节选）：

```json
{"instruction": "帮我写一条请假条，明天上午要去医院复查，下午回来上班", "source": "ime-log", "ts": "2026-06-30T10:12:00Z"}
{"instruction": "哈哈哈哈哈哈哈", "source": "ime-log", "ts": "2026-06-30T10:19:15Z"}
{"instruction": "帮我写一条请假条，明天上午要去医院复查，下午回来上班", "source": "ime-log", "ts": "2026-06-30T10:21:44Z"}
{"instruction": "帮我写一条请假条，明天上午要去医院做复查，下午回来上班。", "source": "ime-log", "ts": "2026-06-30T10:22:10Z"}
{"instruction": "解释一下二分查找为什么时间复杂度是 O(log n)，最好举一个 8 个元素的例子", "source": "app-feedback", "ts": "2026-06-30T10:25:33Z"}
{"instruction": "在吗", "source": "ime-log", "ts": "2026-06-30T10:26:01Z"}
```

这批数据是故意「脏」的：第 1、6 行**一字不差**（精确重复），第 7 行只多了个「做」字和句号（近似重复），还混着「哈哈哈哈哈哈哈」「在吗」这类低质量记录——正好让流水线的每个工位都有活干。

## 3.2 两份配置

**`examples/config.toml`（工具配置，节选）**——声明 LLM 从哪来：

```toml
schema_version = 1

[tool]
log_level = "info"
log_format = "text"

[llm.default]
provider = "anthropic"
base_url = "https://api.z.ai/api/anthropic"
model = "glm-5.2"
api_key_env = "LABELKIT_ZAI_KEY"
max_concurrency = 4
timeout_s = 120
max_retries = 5
supports_structured_output = true
supports_vision = true
max_output_tokens = 4096
temperature = 0.0
```

（文件里还有一个结构几乎相同的 `[llm.judge]` 档，供第 21 章 UI 教程的 verify 独立评审引用，此处从略。）

**`examples/text/project.toml`（工程配置）**——声明这次任务怎么跑：

```toml
schema_version = 1

[run]
input = "./data/input.jsonl"
output = "./out/text-labels.jsonl"
modality = "text"
batch_size = 16
seed = 42

[input]
text_field = "instruction"        # 每行 JSON 里，哪个字段是"正文"

[dedup]
enabled = true                    # 去重：全部用默认参数

[quality]
enabled = true
mode = "pointwise"                # 单点打分模式（0-5 量表，跨批可比）
llm = "default"
threshold = 0.25                  # 聚合分低于 0.25 的记录淘汰
rubric = "default:text"           # 用系统内置的文本质量评价准则

[annotate]
enabled = true
llm = "default"
instruction = """
你是输入法用户请求的标注员。根据用户输入的一句话请求，标注其意图类别
（writing_assist 写作协助 / qa 问答 / translation 翻译 / chitchat 闲聊 / other 其他）、
主题（简短名词短语）与完成该请求的难度（easy / medium / hard）。
"""

[trace]
enabled = true                    # 打开追踪日志，事后能看 LLM 每次打分的理由
channels = ["quality", "schema"]
content = "refs"

[output]
meta_mode = "inline"              # 元信息（分数、溯源）以 _meta 键随行写出
rejects = "refs"                  # 拒绝通道只写引用，不落原文
passthrough_fields = ["source"]   # 把输入里的 source 字段透传到输出
schema_inline = """
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "type": "object",
  "properties": {
    "intent": {"type": "string",
      "enum": ["writing_assist", "qa", "translation", "chitchat", "other"]},
    "topic": {"type": "string"},
    "difficulty": {"type": "string", "enum": ["easy", "medium", "hard"]}
  },
  "required": ["intent", "topic", "difficulty"],
  "additionalProperties": false
}
"""
```

每个键的完整含义后面章节都会讲；现在只需要建立直觉：**config.toml 回答「用什么模型」，project.toml 回答「这批数据怎么加工、输出长什么样」**。

## 3.3 先体检，再试跑，最后开跑

```bash
cd examples/text
mkdir -p out
set -a && source ../../.env && set +a     # 加载密钥（见第 2 章）
```

**第一步：校验配置 + 探测连通**（不花钱，秒级）：

```bash
uv run labelkit validate --config ../config.toml --project project.toml --probe
```

```
配置校验通过
probe default: ok model=glm-5.2 latency_ms=7291
```

**第二步：试运行**（`--dry-run`：把配置和输入完整校验一遍、估算成本，但一次 LLM 都不调）：

```bash
uv run labelkit run --config ../config.toml --project project.toml --dry-run
```

```
dry-run: mode=process estimated_records=14 batches=1
dry-run: estimated LLM calls — generate_calls=0 quality_calls=56 annotate_calls=14 verify_calls=0 total=70 (excludes retries and repair calls)
dry-run: no LLM calls made, no output written (report and trace only)
```

它告诉你：14 条记录、1 个批，预计约 70 次 LLM 调用（质量打分 56 次 = 14 条 × 4 条准则；标注 14 次）。对大任务，这一步是你估算成本和时长的依据。

**第三步：正式运行**：

```bash
uv run labelkit run --config ../config.toml --project project.toml
```

stderr 上会看到（省略时间戳）：

```
INFO  run     batch=0 run.start tool_version=labelkit/1.0.0 config_digest=sha256:9c92... project_digest=sha256:fbf4... trace_schema_version=1
INFO  emitter batch=1 批 1 落盘：主输出 +8 行（累计 8），rejects +6（累计 6）
INFO  run     batch=1 batch.end active=8 dropped_dup=1 dropped_lowq=5 dropped_verify=0 failed=0 duration_ms=87434
INFO  emitter batch=- finalize：fsync + rename  out/text-labels.jsonl.part → out/text-labels.jsonl（8 行）
INFO  emitter batch=- 已写出 out/text-labels.rejects.jsonl（6 行）与 out/text-labels.report.json
   ── 终版摘要（与 report.counts 逐项一致）──
   scanned=14  ingested=14  bad_input=0  generated=0
   dropped_dup=1  dropped_lowq=5  dropped_verify=0  failed=0  emitted=8
INFO  run     batch=0 run.end exit_code=0
```

一分半钟，退出码 0。读一下这份摘要，它就是流水线的「过磅单」：

- 14 条进来（`scanned=14`，全部合法 `ingested=14`）；
- 去重工位拦下 1 条（`dropped_dup=1`——那条一字不差的重复；只多一个「做」字的那条为什么**没**被去重拦下？我们在 3.5 节看账）；
- 质量工位拦下 5 条（`dropped_lowq=5`——「哈哈哈哈哈」「在吗」们，以及所有聚合分低于 0.25 的）；
- 8 条通过全部工位、完成标注、写入主输出（`emitted=8`）。

注意守恒：`8 + 1 + 5 + 0 + 0 = 14`。**每一次运行这个等式都必须成立**——这是 LabelKit 的账目不变量。

> 你本机的具体拦截数可能与这里略有出入：LLM 服务端存在非确定性，聚合分恰在 0.25 阈值附近的两三条记录逐次运行可能进出浮动——但守恒等式永远成立。

## 3.4 读产物

`out/` 下出现四个文件：

```
text-labels.jsonl           # 主输出（8 行）
text-labels.rejects.jsonl   # 拒绝通道（6 行）
text-labels.report.json     # 运行报告
text-labels.trace.jsonl     # 追踪日志（因为开了 trace.enabled）
```

**主输出**每行 = 你的 Schema 字段 + `_meta` 元信息（格式化展示其中一行，文件里的第 3 行）：

```json
{
  "intent": "qa",
  "topic": "光合作用暗反应与卡尔文循环",
  "difficulty": "medium",
  "_meta": {
    "id": "a8aa181766eebd97",
    "run": {"tool": "labelkit/1.0.0", "started_at": "2026-07-03T01:17:35.699878+08:00",
            "project_file": "project.toml", "rubric": "default:text", "seed": 42},
    "source": {"file": "input.jsonl", "line_no": 4, "generated_from": [],
               "fields": {"source": "ime-log"}, "generator": null},
    "scores": {"educational_value": 0.6, "facts_trivia": 0.8, "writing_style": 0.4,
               "required_expertise": 0.6, "__aggregate__": 0.6000000000000001,
               "mode": "pointwise", "batch_no": 1},
    "dedup": {"kind": "unique"},
    "annotation": {"model": "glm-5.2", "attempts": 1},
    "verification": null
  }
}
```

顶层三个字段就是你在 Schema 里声明的标注结果；`_meta` 里则是这条记录的「完整履历」：它来自输入文件第 4 行（`source.line_no`）、四条质量准则各得几分、聚合分 0.6（浮点尾数是加权平均的正常产物）、不是重复（`dedup.kind="unique"`）、标注一次成功（`attempts: 1`）。`fields.source` 是被透传的原始字段。不想要 `_meta`？`output.meta_mode` 可以改成 `sidecar`（旁车文件）或 `none`（第 8 章）。

**拒绝通道**记录每条被淘汰记录的去向（`rejects = "refs"` 档不含原文，只有引用）：

```json
{"_meta": {"id": "6e60ce3c2d59f04d", "source": {"file": "input.jsonl", "line_no": 1, "generated_from": []}, "stage": "quality", "reason": "below_threshold", "errors": []}}
```

第 1 行的请假条居然被质量门淘汰了？是的——`default:text` 准则衡量的是「作为训练数据的价值」，一条日常请假条在「事实含量」「专业度」上得分很低。**质量线画在哪、用什么准则量，直接决定你留下什么数据**——这正是第 10 章和第 20 章要精讲的主题。

**运行报告**（`report.json`）是给人和机器看的完整账本，包含质量分直方图、每条准则的均值、结构引擎各层命中数、token 用量、各阶段耗时（第 8 章逐字段解读）。先看两个最有用的：

```json
"quality": {
  "per_criterion_mean": {"educational_value": 0.31, "facts_trivia": 0.22,
                          "required_expertise": 0.32, "writing_style": 0.40}
},
"llm_usage": {
  "default": {"calls": 61, "prompt_tokens": 21683, "completion_tokens": 6831, "retries": 0}
}
```

`facts_trivia`（事实与知识含量）在四条准则里均值最低——这批以闲聊、写作协助为主的输入法数据可核验事实偏少。如果你觉得这条准则对你的场景不公平，第 10 章会教你改 rubric 或调权重。

## 3.5 看一眼流水线的「思考过程」

因为开了 `trace.enabled = true`，**channels 里订阅的通道**（本工程是 `quality` 和 `schema`）的每个关键判定都留了底。去重工位对第 6 行（精确重复）的判定这次没有进 trace——本工程的 `channels` 没包含 `"dedup"`；它的去向记录在 rejects 文件里（`stage="dedup"`、`reason="exact"`）。若把 `"dedup"` 加进 `trace.channels` 重跑，就能在 trace 里看到这样一条事件：

```json
{"ev": "dedup.duplicate", "record_ids": ["..."],
 "payload": {"kind": "exact", "cluster_key": "...", "kept_id": "..."}}
```

而第 7 行「多一个做字」那条，你会发现它**不在**去重的名单里（rejects 中它的 `stage` 是 `quality` 而非 `dedup`）——默认的近似去重阈值（Jaccard ≥ 0.85，字符 5-gram）对这句 20 字的短文本来说，一个字的差异已经让相似度跌破阈值，它是后来才被质量门拦下的。短文本去重要不要收紧、怎么收紧，见第 9 章「调优」。

再比如质量工位给「哈哈哈哈哈哈哈」打分的原始裁决（`quality.pointwise` 事件、`content="refs"` 档带理由）：LLM 在每条准则上都给了 0 分，理由清清楚楚写在 `reason` 字段里。**当你对任何一条记录的去留有疑问时，trace 就是你查账的地方**（第 16 章）。

## 3.6 你已经会了什么，接下来去哪

到这里你已经完成了一次真实的「去重 → 打分 → 过滤 → 标注」全流程，并且知道每个产物怎么读。接下来：

- 想理解流水线的运转规则（批、状态、开关组合）→ **第 4 章**
- 想接自己的数据 → **第 5 章**
- 想逐个吃透配置参数 → **第 6、7 章**
- 想直接抄更复杂的作业（UI 截图标注、从零生成数据集）→ **第 21、22 章**
