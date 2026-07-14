## 3.2 M2 数据接入 ingest

### 3.2.1 职责与边界

**做：**把输入路径物化为 `Record` 迭代器：文本模态逐行解析 JSONL 并抽取文本字段；UI 模态递归扫描、按 index 配对文件、解析 UI 树节点并构造惰性图像引用；为每条记录计算确定性 id；对坏数据执行跳过策略并计数。（v1.8）stream 模式（`segment.enabled = true`）下另担 `[stream]` 声明的输入侧排序、流式单调性校验与规则层会话化，向 M10 暴露会话流视图（3.2.8）。 
**不做：**不加载图像像素（只 stat 文件并记录路径/尺寸）；不截断/清洗文本（原样保留，序列化截断是消费方在构造提示词时的职责）；不判重、不打分；`run.mode="generate_only"` 时本模块整体不参与（3.10.3）。

### 3.2.2 输入 / 输出

| 方向 | 内容 |
|---|---|
| 输入 | `run.input` 路径。文本模态：单个 `.jsonl` 文件，或目录（取其下所有 `*.jsonl`，按文件名字典序）。UI 模态：目录（递归扫描）。 |
| 输出 | `Iterator[Record]`（生成器，按需产出，供 M10 切批；v1.8 stream 模式下 M10 改消费 `sessions()` 会话流视图——整会话装箱，3.2.8/3.10.3）；`IngestReport`（总行数、坏行数、缺对数、index 冲突数及其文件位置列表；v1.8 只增会话数 `sessions` 与乱序跳过数 `disorder` 两计数，3.2.8）。 |

### 3.2.3 API

```
class Ingestor:
    def __init__(self, cfg: ResolvedConfig): ...
    def scan(self) -> IngestPlan:
        """只扫描不解析：返回文件清单、配对表、预估记录数。--dry-run 与 validate 用。
           v1.8（S23）：stream 启用时文本模态的行数统计与会话空跑单遍融合（3.2.8）。"""
    def records(self) -> Iterator[Record]:
        """惰性产出 Record。解析错误按 input.on_bad_line / input.on_missing_pair 策略处理。
           非 stream 入口，v1.8 零改动。"""
    def sessions(self) -> Iterator[Session]:
        """v1.8：stream 模式的会话流视图，M10 以之取代 records() 消费（3.2.8）；产出
           Session(session_id, records, cause)——形态冻结于 CONTRACTS §7.1。"""
    @property
    def report(self) -> IngestReport: ...
```

### 3.2.4 算法：UI 模态文件配对

图 3-1 UI 模态文件对扫描与配对流程

配对规则的精确定义：

| 规则 | 定义 |
|---|---|
| index 命名空间 | 整个输入目录（含全部子目录）共享一个 index 空间；index 为文件名正则捕获组按十进制解析的整数（允许前导零，`image_007.png` 的 index 为 7）。 |
| 冲突 | 同一 index 出现 ≥2 个 uitree 文件或 ≥2 个 image 文件 ⇒ 该 index 记为冲突。`input.on_index_conflict = "fail"`（默认，退出码 3）或 `"skip"`（整个 index 跳过并计数）。同 index 同时存在 .png 与 .jpg 亦视为冲突。 |
| 缺对 | index 只有单侧文件 ⇒ `input.on_missing_pair = "skip"`（默认）或 `"fail"`。 |
| UI 树文件格式 | JSONL，每行一个控件节点对象（字段映射见 6.2）。空文件或全坏行 ⇒ 该记录按坏记录跳过。单文件也允许仅一行（整树作为一个节点对象给出、含 children 嵌套）——两种导出风格都支持，解析器先探测：若首行对象含 `children` 数组则按嵌套树解析，否则按平铺节点行解析。 |
| 图像约束 | PNG/JPEG；单文件 ≤ `input.max_image_mb`（默认 20）；超限按坏记录跳过。仅校验魔数与尺寸，不解码全图。 |

### 3.2.5 文本模态解析

每行必须是 JSON object（非 object 的合法 JSON 视为坏行）。标注/打分所用文本由 `input.text_field`（支持点路径，如 `"conversation.turns"`；默认 `"text"`）抽取：命中字符串则直接使用；命中数组/对象则按 canonical JSON（`sort_keys=True, ensure_ascii=False`，紧凑分隔符）序列化为文本；未命中字段 ⇒ 坏行。原始对象完整保留在 `Record.raw`，输出时可按 `output.passthrough_fields` 透传（见 6.3）。记录 id = `sha256(canonical_json(raw))[:16]`。坏行策略 `input.on_bad_line = "skip"`（默认）| `"fail"`。

### 3.2.6 配置项

见 5.2 `[input]` 节字段表（本模块消费其全部字段）。

**背书：**「截图 + accessibility tree/视图层级」文件对是 GUI 智能体数据集的标准物理形态：ScreenAI 的 screen schema 即截图与线性化控件树的配对 [13]，OS-Atlas 跨 5 平台的 1300 万控件语料同样以截图+树组织 [16]，AMEX/AndroidControl 等移动端数据集亦然。逐行 JSONL、按文件名索引的组织方式与 Dolma toolkit 的分片 JSONL 惯例一致 [6]。

### 3.2.7 输入 / 输出示例

#### ① 文本模态（`run.modality = "text"`，`input.text_field = "instruction"`）

`run.input = "./ime-logs"`，目录下仅一个文件 `ime-2026-06-30.jsonl`（输入法采集的中文指令数据），共 3 行；第 3 行不含 `instruction` 字段，按 3.2.5 判为坏行：

```
{"instruction": "帮我写一条请假条，明天上午要去医院", "source": "ime-log", "ts": "2026-06-30T10:12:00Z"}
{"instruction": "把这句话翻译成英文：会议改到周五下午三点", "source": "ime-log", "ts": "2026-06-30T10:15:21Z"}
{"query": "今天天气怎么样", "source": "ime-log", "ts": "2026-06-30T10:20:05Z"}   ← text_field 未命中 ⇒ 坏行
```

`records()` 产出 2 个 `Record`（字段见 4.1；id 按 3.2.5 = `sha256(canonical_json(raw))[:16]`，下列值为对 canonical JSON 实际计算的结果，可复算验证）：

```
// Record #1（第 1 行）
{
  "id": "1cda030abc565f17",
  "modality": "text",
  "text": "帮我写一条请假条，明天上午要去医院",
  "raw": {"instruction": "帮我写一条请假条，明天上午要去医院",
          "source": "ime-log", "ts": "2026-06-30T10:12:00Z"},
  "ui_tree": null, "image": null,
  "ref": {"source_file": "ime-2026-06-30.jsonl", "line_no": 1,
          "pair_index": null, "generated_from": []}
}
// Record #2（第 2 行）
{
  "id": "a9bbd04dca155b52",
  "modality": "text",
  "text": "把这句话翻译成英文：会议改到周五下午三点",
  "raw": {"instruction": "把这句话翻译成英文：会议改到周五下午三点",
          "source": "ime-log", "ts": "2026-06-30T10:15:21Z"},
  "ui_tree": null, "image": null,
  "ref": {"source_file": "ime-2026-06-30.jsonl", "line_no": 2,
          "pair_index": null, "generated_from": []}
}
```

第 3 行按默认 `input.on_bad_line = "skip"` 跳过并计数，迭代结束后 `report` 属性给出 `IngestReport`（计数口径与 6.4 `report.json` 的 counts 一致）：

```
{
  "scanned": 3, "ingested": 2, "bad_input": 1,
  "missing_pair": 0, "index_conflict": 0,        // 仅 UI 模态会非零
  "bad_locations": [
    {"file": "ime-2026-06-30.jsonl", "line_no": 3,
     "reason": "input.text_field \"instruction\" 未命中"}
  ]
}
```

#### ② UI 模态（`run.input = "./capture/2026-07-01"`，即 5.2 示例工程）

`b/uitree_2.jsonl` 为平铺节点行风格（首行无 `children` 数组，3.2.4 探测规则）。字段名取 6.2 映射表的源字段名：`id/parent/class/text/bounds/visible`；根节点省略 `parent` ⇒ `parent_id = null`；`hint` 不在白名单 ⇒ 值转字符串后入 `extra`：

```
{"id": "0", "class": "FrameLayout", "bounds": [0, 0, 1080, 2340], "visible": true}
{"id": "1", "parent": "0", "class": "TextView", "text": "登录", "bounds": [72, 296, 264, 392], "visible": true}
{"id": "2", "parent": "0", "class": "EditText", "bounds": [72, 520, 1008, 664], "visible": true, "hint": "请输入手机号"}
{"id": "3", "parent": "0", "class": "EditText", "bounds": [72, 712, 672, 856], "visible": true, "hint": "请输入验证码"}
{"id": "4", "parent": "0", "class": "Button", "text": "获取验证码", "bounds": [704, 712, 1008, 856], "visible": true}
{"id": "5", "parent": "0", "class": "Button", "text": "登录", "bounds": [72, 952, 1008, 1096], "visible": true}
```

正则从 `b/uitree_2.jsonl` 与 `c/image_2.png` 各提取 index = 2，跨子目录求交集配对成功（图 3-1），构造的 `Record`：

```
{
  "id": "9f2c31ab52e08d17",                  // sha256(树字节 + 图字节)[:16]，与 6.3 _meta 示例同源
  "modality": "ui",
  "text": null, "raw": null,
  "ui_tree": UITree(nodes = 6 × UINode，深度优先序，序列化见下),
  "image": {"path": "capture/2026-07-01/c/image_2.png", "format": "png", "size_bytes": 483112},
  "ref": {"source_file": "b/uitree_2.jsonl", "line_no": null,
          "pair_index": 2, "generated_from": []}
}
```

`UITree.serialize()` 按 4.3 规范（深度缩进 + role + 引号 text + [l,t,r,b] + 非空 extra 的 k=v；此处 `quantize_px = 0`，即 M5 组装提示词时的形态，3.5.2 示例复用本输出）：

```
FrameLayout [0,0,1080,2340]
  TextView "登录" [72,296,264,392]
  EditText [72,520,1008,664] hint=请输入手机号
  EditText [72,712,672,856] hint=请输入验证码
  Button "获取验证码" [704,712,1008,856]
  Button "登录" [72,952,1008,1096]
```

**提示：**两处 id 规则不同——文本模态对 `raw` 的 canonical JSON 取哈希（与行号、文件名无关，内容相同即 id 相同，为 M3 精确判重的基础）；UI 模态对树文件字节与图像文件字节拼接取哈希。图像此时仅 stat（`size_bytes = 483112`，远小于 `input.max_image_mb = 20` 上限），像素到 M5 组装提示词时才经 `ImageRef.load_base64()` 加载。

### 3.2.8 时序流排序与会话化（v1.8）

`segment.enabled = true`（stream 模式，2.3.1）时本模块承担 `[stream]` 节声明的输入侧排序、流式单调性校验与**规则层**会话化（配置键表见 5.2；边界的语义精化属 M14，3.14——会话化留 M2、装箱留 M10 的分工见 3.10.3）。产出面变化：M10 改消费 `sessions()` 会话流视图（3.2.3）而非 `records()`，切批改整会话装箱。

**排序键（`stream.order_by`）。**`"input_order"`（默认）：文本 = 文件名字典序 → 行号，UI = pair_index 升序（即现行 `records()` 顺序）。`"meta:<field>"`（仅文本模态，M1 校验 3.1.4）：`<field>` 为原始行对象上的点路径字段，时间戳解析规格见 6.1（S20）——数值 `v < 0 ∨ v ≥ 1e14` 解析失败、`v < 1e11` 判 epoch 秒、`[1e11, 1e14)` 判毫秒（÷1000）；字符串先试纯数字（走数值规则）再试 `datetime.fromisoformat`（3.11 起原生接受 `Z` 后缀），均败 = 解析失败；aware 值换算 UTC epoch、naive 值按 UTC 解释；内部序键 = float 秒。

**流式单调性校验（S19/S20）。**不做全量重排：单调性游标**按 `stream.key` 分区键各自维护**（dict，内存 = 键基数）——逐设备/逐来源拼接的输入不会被整体判乱序；键变即断会话（groupby 语义非 keyBy，**输入须按分区键成组**；交错流列演进候选，8.4）。乱序记录与时间戳解析失败**同走** `stream.on_disorder`：`"skip"`（默认）= 跳过 + 计 `bad_input` + `IngestReport.disorder` 子计数 + 每记录一条 `ingest.disorder` 事件（7.2；stderr WARN 每运行仅镜像一次）；`"fail"` = InputError，退出码 3。

**会话装配器（规则层，纯代码零 LLM）。**在（已 `--limit` 截断的）解析流上按下列条件闭合候选会话，任一触发即断（键表 5.2）：

- `stream.key` 键变即断（`"meta:<field>"` 仅文本模态；`"source_dir"` = ref.source_file 父目录派生，UI 模态可用——一次采集一目录惯例，S19）；**文本模态 `order_by = "input_order"` 下源文件变更恒闭合会话（cause = "key"，即使 `stream.key = []`）**——line_no 的顺序语义不跨文件成立、无时间戳可桥接文件边界；`order_by = "meta:*"` 时文件边界透明（轮转日志场景）（v1.8 D7 登记）；
- `stream.gap_s`（相邻记录时间差 > gap_s 秒即断；仅 order_by="meta:*"）/ `stream.gap_steps`（序号差断开，0 = 不启用）——两者可并用，任一触发即断；
- `stream.session_max_len`（默认 200，硬上限）/ `stream.session_max_span_s`（时间跨度硬上限，0 = 不启用；仅 meta:*）；
- 流耗尽（eof）或 `--limit` 截断（limit）。

会话闭合时发一条 `segment.session` trace 事件（**属主 M2**；事件名冠 segment 前缀、按前缀归 segment 通道，S1，7.2）——payload 含 `session_id`、`first` / `last`（首末序键）、`len`、`cause`（∈ `gap`\|`key`\|`max_len`\|`max_span`\|`eof`\|`limit`），并计 `IngestReport.sessions`。会话对象 `Session(session_id, records, cause)` 的形态冻结于 CONTRACTS §7.1：`session_id = sha256("\n".join(会话内记录 id))[:16]`（会话序）、`records` 为按会话序的成员 Record 元组、`cause` 即上述闭合词表。

**--limit 帧级截断（S17）。**`--limit` 的单位不变、仍是**帧**（记录）：islice 位于解析流与会话装配器**之间**；截断视同 EOF——尾部未闭合会话按会话闭合下发（cause = "limit"）+ WARN 一次。`cause = "limit"` 的精确语义是「**该会话在 --limit 预算耗尽处闭合**」：预算恰好在流末耗尽（无真截断）与真截断不可区分——消歧需要多拉取并解析一条记录，会扰动 scanned/bad_input 台账，工具不做（v1.8 D3 裁决）；WARN 文案据此陈述预算耗尽而非断言截断（2.4 --limit 行 stream 子句）。

**IngestReport（v1.8 只增两字段）。**`sessions` = 装配器闭合的候选会话数（stream 模式；`report.stream.sessions` 的数据源，6.4）；`disorder` = 单调性校验跳过的记录数——`bad_input` 的**子计数**，经逐条 `ingest.disorder` 事件可审计，report 不另设键（6.4）。

**dry-run 单遍融合（S23）。**文本模态 stream 启用时，`scan()` 的行数统计与会话空跑**单遍融合**——一次读同时产出预估记录数与会话尺寸序列（供 3.10.3 dry-run 的 next-fit 装箱与调用量估算），不做第二次全量读。

**背书：**规则层会话化是流处理 session window 原语的对应物（Apache Flink `EventTimeSessionWindows` / Apache Beam `Sessions` [55]，1.5）——inactivity gap + 分区键 + 硬上限三件套照抄，纯代码零 LLM 成本；`gap_s` 默认偏大的结构性论证（欠分割可由 M14 的 LLM 边界精化拯救、过分割不可逆）见 5.2 gap_s 行。
