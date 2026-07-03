## 3.2 M2 数据接入 ingest

### 3.2.1 职责与边界

**做：**把输入路径物化为 `Record` 迭代器：文本模态逐行解析 JSONL 并抽取文本字段；UI 模态递归扫描、按 index 配对文件、解析 UI 树节点并构造惰性图像引用；为每条记录计算确定性 id；对坏数据执行跳过策略并计数。 
**不做：**不加载图像像素（只 stat 文件并记录路径/尺寸）；不截断/清洗文本（原样保留，序列化截断是消费方在构造提示词时的职责）；不判重、不打分；`run.mode="generate_only"` 时本模块整体不参与（3.10.3）。

### 3.2.2 输入 / 输出

| 方向 | 内容 |
|---|---|
| 输入 | `run.input` 路径。文本模态：单个 `.jsonl` 文件，或目录（取其下所有 `*.jsonl`，按文件名字典序）。UI 模态：目录（递归扫描）。 |
| 输出 | `Iterator[Record]`（生成器，按需产出，供 M10 切批）；`IngestReport`（总行数、坏行数、缺对数、index 冲突数及其文件位置列表）。 |

### 3.2.3 API

```
class Ingestor:
    def __init__(self, cfg: ResolvedConfig): ...
    def scan(self) -> IngestPlan:
        """只扫描不解析：返回文件清单、配对表、预估记录数。--dry-run 与 validate 用。"""
    def records(self) -> Iterator[Record]:
        """惰性产出 Record。解析错误按 input.on_bad_line / input.on_missing_pair 策略处理。"""
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
