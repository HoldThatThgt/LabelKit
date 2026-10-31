## 3.18 segment 图片模式（M14）

本章是 v1.22 `segment.mode = "image"` 的设计规格；3.14 只描述 `segment.sequence_resolved = true` 的时序流模式。阶段名仍是 `segment`，不新增模块编号与链序槽位。名称、错误码、措辞和键序的权威来源是 `docs/dev/SPEC-image-region-segmentation.md`（下称**图片分割规格**）与 `docs/CONTRACTS.md`。

### 3.18.1 职责与边界

**做：**对一张图片一个样本的普通记录，同步调用可选的 `segment.box_parser` 钩子把伴随数据转成参考框，再由已配置 LLM profile 生成候选区域，或在没写 `segment.instruction` 时让参考框直接成为矩形候选；通过校验后把冻结的 `RegionSet` 写入 `item.regions`。候选决定几何，annotate 只按 `region_id` 选择与标注，后处理做确定性的引用解析与格式转换。

**不做：**不解析控件树，也不构造 `UITree`；不嗅探伴随数据的内容，不用正则抽取；不做跨帧身份与追踪；不接入独立分割模型；不做任何几何自动修复（`make_valid`、`buffer(0)`、`orient_polygons`、`set_precision`，静态测试守护）；不读取、不应用、不拒收 EXIF 方向；不把叠图写到磁盘；不提供公开几何 API；不被 verify 重新触发。

阶段位于链首原位置（链序元组不变，D2），走普通批路径（`session_attempt = None`）。实现位于 `labelkit/operators/segment_image.py`（`SegmentImageStage`，`name = "segment"`）与 `labelkit/operators/segment_geometry.py`（Shapely 唯一导入点）。`SegmentStage`、`judge_window` 与 stream_verify 对它们的依赖不变。`build_stages` 按 mode 选择阶段类（3.10.3）。

`segment.mode` 与 `run.mode`、`quality.mode` 是三个不同的键：`run.mode` 选择处理或只生成，`quality.mode` 选择打分方式，`segment.mode` 选择分割的语义（图片平面或时序流）。三者没有派生关系。

图 3-10 segment 图片模式数据流。绿色为图片模式新增部分；虚线表示叠图由候选惰性渲染，只随 annotate 与 verify 的请求发送，不落盘。

### 3.18.2 输入 / 输出

| 方向 | 内容 |
|---|---|
| 输入 | 批内 `status="active"` 的单记录信封，`Record.image` 为 `ImageRef`，`Record.ui_tree = None`，`Record.raw = None`；可选 `Record.companion` 原始字节（3.2.10）；`[segment]` 图片模式键（5.2.2）；写了 `instruction` 时的 LLM profile（`segment.llm`）。 |
| 输出 | 就地写 `item.regions: RegionSet`（载体见 4.1.1）；失败时 `item.regions = None` 且记录 `failed`，`stage = "segment"`。`RegionSet(regions=())` 是合法的空候选，与 `None` 不混用。不追加信封，不重绑 Record，属 Stage 契约 ①④ 的普通行为（4.3）。 |
| 消费方 | annotate（3.5.7）、verify（3.7.6）、后处理与 `output.validator`（3.5.7）、dedup 精确层与参考框文本（3.3.3）、classify 扇出（3.13.4）、主输出 `_meta.regions`（6.3）。全部显式传递同一冻结对象，不复制、不重建。 |

### 3.18.3 坐标与画布

**画布。**原图的存储像素网格；左上角为原点，x 向右，y 向下。宽高由 segment 阶段用 Pillow 只读文件头得到。图片方向不做任何处理（D4）：带方向标签的 JPEG 按普通图片处理，与伴随数据对齐由输入方负责。已知残余：未缩放的 JPEG 原样发送并保留方向标签，若服务端按标签旋转显示，模型看到的方向可能与按存储像素绘制的叠图不同，segment 的网格坐标也会按存储画布投影而错位。

**模型侧网格。**segment 输出与图片模式所有提示词里的候选坐标一律用 0–1000 整数网格，x 相对宽度、y 相对高度，两轴各自归一化（D3）。x 精度为 `width / 1000` 像素，y 精度为 `height / 1000` 像素（1080 × 2340 约为 1.08 / 2.34 像素）。候选是区域提议而非像素级掩膜。合法性在网格整数坐标上判定，避免浮点误差改变共点、共线等边界情形；正比例的逐轴缩放不改变自交、孔洞包含、部件相交这些拓扑性质。

**几何真值。**唯一真值是原图像素坐标，不取整：

- 参考框 bbox 与由框派生的候选顶点保留钩子给出的 JSON 数值类型（int 或 float）。
- 模型画的多边形顶点经除法投影，恒为 float：`x_px = gx * width / 1000`，`y_px = gy * height / 1000`。不取整、不吸附、不改变顶点顺序。
- 由框派生的矩形顶点顺序冻结为 `(l,t)→(r,t)→(r,b)→(l,b)→(l,t)`，没有内环。矩形候选同样以多边形表达，不维护第二套几何。
- 每个候选只换算一次：模型多边形投影一次，复用参考框不经网格。

**闭环约定。**模型给出的每个环必须闭合（≥ 4 个位置，首末相同），发布时原样投影，不改写。未闭合按违规进入修复，框架不代为闭合。相邻重复顶点在 OGC 下合法，原样保存。

**提示词里的网格显示。**像素真值回到提示词时只是显示投影，不回写真值，全部用 `fractions.Fraction` 精确有理运算。

| 几何来源 | 显示函数 |
|---|---|
| `[参考框]` 块本身；`box_id` 非空的候选（复用与 D15 直通） | 外扩取整：`gl = floor(l*1000/width)`，`gt = floor(t*1000/height)`，`gr = ceil(r*1000/width)`，`gb = ceil(b*1000/height)` |
| `box_id` 为空的模型画多边形顶点 | 就近取偶：`round(Fraction(x) * 1000 / width)`，可精确还原模型原先给出的整数 |

外扩取整保证同一框在两个块中逐值相等、任何正尺寸的框仍有正面积（4000 像素宽、l = 1000、r = 1001 显示为 250 / 251）；网格框恒包含像素框，最大外扩误差一个网格单位。

**参考框渲染。**`render_reference_boxes(regions)` 每框一行，行间 `\n`，无末尾换行，每行恰为 `json.dumps({"box_id": box.box_id, "bbox": [gl, gt, gr, gb], "text": box.text}, ensure_ascii=False)`。text 中的换行、引号与控制字符由 JSON 转义，任何 text 都不能伪造出另一行。

**候选渲染。**`render_candidates(regions)` 是一行 `json.dumps(items, ensure_ascii=False)`（默认分隔符），`items` 每项键序为 `region_id`、`polygons`、`box_id`、`source_box_ids`，几何来自参考框的候选再追加 `text`。`polygons` 每项键序为 `exterior`、`holes`；不是 GeoJSON。空候选渲染为 `[]`。以画布 1080 × 2340、复用 n12（像素框 `[43, 140, 821, 1264]`）为例：

```text
[参考框]
{"box_id": "n12", "bbox": [39, 59, 761, 541], "text": "Button 登录"}
[候选区域]
[{"region_id": "n12", "polygons": [{"exterior": [[39, 59], [761, 59], [761, 541], [39, 541], [39, 59]], "holes": []}], "box_id": "n12", "source_box_ids": ["n12"], "text": "Button 登录"}]
```

**不承诺。**服务端对图片的二次处理不在 LabelKit 的保证内。归一化网格只对逐轴缩放免疫，对旋转不免疫。模型是否遵守网格约定只能靠真实端点证据确认（图片分割规格 §20）。

### 3.18.4 伴随数据：`segment.box_parser`

LabelKit 不判断伴随数据是不是控件树：注册钩子就是工程的声明，钩子的输出是唯一被框架信任的结构。实现位于 `labelkit/common/extensions/box_parser.py`（`resolve_box_parser(reference, project_root) -> ResolvedHook`、`invoke_box_parser(hook, companion, width, height) -> tuple[ReferenceBox, ...] | None`）。

```python
def uiautomator_to_boxes(companion: bytes, image: dict) -> list[dict] | None:
    """把一份 uiautomator XML 转成参考框数组。

    @param companion 伴随文件的原始字节，由钩子自行解码
    @param image {"width": W, "height": H}，原图存储像素尺寸
    @return 参考框数组；数据无法解析时返回 None，拒收本样本
    """
```

`"<python-file>:<attribute>"` 引用，M1 用 `load_hook` 加载一次并冻结。必须是同步函数，恰好两个位置参数且都没有默认值（比后处理更严）。异步函数、异步 `__call__`、生成器、异步生成器、可变参数、额外参数都在启动期拒绝。M1 只做静态签名检查，不在启动期调用。

统一参考框格式（D13、D14；框架严格校验，不裁剪、不取整、不去重、不合并）：

| 字段 | 规则 |
|---|---|
| `id` | 框名。要么每个框都给出非空 `id`，要么每个框都留空（省略该键或给空字符串）；全部留空时框架按数组顺序补为 `"1"`、`"2"`…`"N"`；只命名一部分 → `invalid_box`。`null`、非字符串、超长或含字符集以外的字符一律 `invalid_box`。给出的框名：1–64 个字符，只含 `[A-Za-z0-9_.:-]`，样本内唯一，不得匹配 `^r[1-9][0-9]*$` |
| `bbox` | `[l, t, r, b]`，长度恰为 4，原图像素，整数或有限浮点（不接受布尔、NaN、无穷）；`0 ≤ l < r ≤ width`，`0 ≤ t < b ≤ height` |
| `text` | 字符串，可为空，可含换行；必须能编码为 UTF-8（孤立代理是 `invalid_box`） |

校验顺序（冻结）：

1. 返回值恰为 `None` → `companion_invalid`；不是 `None` 也不是 `list` → `return_type`。
2. 按严格 JSON 复制（元组、集合、非字符串键、NaN、无穷、循环引用都拒绝，与后处理返回值同一规则）→ 失败为 `invalid_json`。
3. 逐项形状：每项是 `dict`，键集合是 `{"bbox", "text"}` 或 `{"id", "bbox", "text"}` → 否则 `invalid_box`，`box_index` 为该项序号。
4. 命名一致性：`id` 键缺省或值为 `""` 视为未命名，否则视为已命名。以第 0 个框的状态为准，第一个状态不同的框 → `invalid_box`，日志 `box_index` 为其 0 基序号。
5. 逐框值校验（上表），第一个违规框 → `invalid_box`。
6. 全部未命名时补名 `"1"…"N"`，构造 `ReferenceBox`。

调用与失败：

| 情况 | 结果 |
|---|---|
| 钩子返回 `list`，每个框都合法 | 冻结进 `RegionSet.boxes` |
| 钩子返回 `None` | 记录 `failed`，`stage = "segment"`，错误码 `companion_invalid`，固定英文原因 `box_parser rejected companion`，不调用模型，不进修复，不计 `bad_input`，`input.on_bad_line` 不适用；报表单独计数 |
| 钩子抛异常、返回既非 `list` 也非 `None`、非严格 JSON、任一框违反上表 | `BoxParserError`（`InternalError` 子类）→ `internal_error`，记录 `failed`；日志只记固定英文原因（`exception` / `return_type` / `invalid_json` / `invalid_box`）、出错框的序号（键 `box_index`）与钩子引用，不记数据；不调用模型，不进修复 |
| 没有伴随文件，写了 `instruction` | 不调用钩子，参考框为空，模型依据图片生成候选 |
| 没有伴随文件，没写 `instruction`（D15） | 摄取时按缺对处理：计 `missing_pair` 与 `bad_input`，遵循 `input.on_missing_pair`，不产生 Record（3.2.10） |
| 零字节伴随文件 | 原样以 `b""` 交给钩子，由钩子决定 |

`[]` 表示数据合法但没有框；`None` 表示数据无法解析，二者不得混用。空树、只有窗口外壳、数据不是控件树，都由钩子决定返回什么。

**时机与信任边界。**在 segment 阶段内、提交任何模型任务之前同步调用，每个样本恰好一次。classify 扇出克隆、L3 修复、verify 修复都不再调用；`validate` 与 `--dry-run` 不调用。钩子不读文件、不联网、不依赖随机性（与后处理同一信任边界）。它在事件循环线程上同步执行，解析很大的伴随文件会推迟同批其他请求。

### 3.18.5 算法与流程

**候选来源。**

| 配置 | 候选来源 |
|---|---|
| 有 `instruction`，无 `box_parser` | 模型只依据图片生成候选 |
| 有 `instruction`，有 `box_parser` | 模型同时看到图片与 `[参考框]`。每个候选要么只填 `box_id` 原样复用一个框（几何取框的像素矩形，不经网格），要么画新多边形。没被复用的框不会成为候选 |
| 无 `instruction`，有 `box_parser` | 不调用模型、不提交任何运行时任务：每个参考框按返回顺序直接成为矩形候选（`region_id` 取框名，`box_id` 与 `source_box_ids` 都指向该框，`label_anchor` 取框中心）。此时 `segment.llm` 不在引用集合，运行时也没有这个资源键，任何提交都会让任务组以 `unknown runtime resource key` 失败，所以这条路径完全不碰 runtime（D15） |
| 两者都没有 | `CONFIG_ERROR`（rule 74） |

**流程。**对 active 记录逐条读文件头得到宽高，调用钩子（若注册且有伴随文件）并冻结参考框；钩子失败的记录直接置 `failed`。只有写了 `instruction` 时，才为其余 active 记录各提交一个 `TaskSpec(resource_key=("llm", segment.llm))`，经 `ctx.run_group` 并发。首轮超限预检用 `labelkit/common/inference/sequence_evidence.py` 的 `request_overflow(CapacityRequest(segment.llm, prompt, schema), ctx)`。

**提示词（CONTRACTS §10.20 冻结）。**system 消息是单个 text part：下列 `_SYSTEM_STATIC` 行间 `\n`、无末尾换行，紧接 `\n{segment.instruction}`。

```text
你负责为图片生成候选区域。坐标使用 0–1000 整数网格：x 是相对图片宽度的千分位，y 是相对图片高度的千分位，原点在左上角。
每个区域由一个或多个多边形组成；每个多边形给出闭合的外环（至少 4 个点，最后一个点与第一个点相同），可以给出同样闭合的内环表示孔洞。同一区域的多个多边形之间不能重叠，也不能共用边；相邻的部分请合并成一个多边形。孔洞不能与外环共用边。不要输出区域编号。
提供了参考框时，每个区域二选一：复用一个参考框时只填 box_id，polygons 与 source_box_ids 都填 null；画新多边形时 box_id 填 null，可以在 source_box_ids 中列出参考了哪些框（不要重复）。同一个参考框最多复用一次。未提供参考框时 box_id 与 source_box_ids 都填 null。没有符合指令的区域时输出空列表。
输出必须是符合以下结构的单个 JSON 对象，不输出任何其他内容：
{"regions": [{"box_id": <参考框 box_id|null>, "polygons": [{"exterior": [[x, y], ...], "holes": [[[x, y], ...], ...]}, ...]|null, "source_box_ids": [<参考框 box_id>, ...]|null}, ...]}
[分割指令]
```

user 消息只有一条，图片只放这里（Anthropic 构造器丢弃 system 消息里的图片）：

```text
text part:  [图片]
image part: record.image
text part:  [参考框]\n{render_reference_boxes(regions)}      ← 有参考框时
text part:  本样本没有参考框。                                ← 没有参考框时（含钩子返回 []）
```

`TEMPLATE_HEAD_TOKENS["segment_image"] = 410 = est_text(segment_image._SYSTEM_STATIC)`；其余键不变。静态预算检查用该常数加 `est_text(instruction)`，不估 `segment.context`。

**内部 Schema（CONTRACTS §10.7）。**`segment_image_schema()` 放在 `schema_engine.py` 既有内部 Schema 同区，不引入 `$ref` / `$defs`、全部键必填、可空用联合类型、不用 `uniqueItems`；环定义构造一次，在 `exterior` 与 `holes.items` 两处引用。

```json
{"type": "object", "additionalProperties": false, "required": ["regions"],
 "properties": {"regions": {"type": "array", "items": {
   "type": "object", "additionalProperties": false,
   "required": ["box_id", "polygons", "source_box_ids"],
   "properties": {
     "box_id": {"type": ["string", "null"]},
     "polygons": {"type": ["array", "null"], "minItems": 1, "items": {
       "type": "object", "additionalProperties": false, "required": ["exterior", "holes"],
       "properties": {
         "exterior": {"type": "array", "minItems": 4, "items": {
           "type": "array", "minItems": 2, "maxItems": 2,
           "items": {"type": "integer", "minimum": 0, "maximum": 1000}}},
         "holes": {"type": "array", "items": {
           "type": "array", "minItems": 4, "items": {
             "type": "array", "minItems": 2, "maxItems": 2,
             "items": {"type": "integer", "minimum": 0, "maximum": 1000}}}}}}},
     "source_box_ids": {"type": ["array", "null"], "items": {"type": "string"}}}}}}}
```

坐标范围、整数类型与顶点数在 L2 判定，违规沿用既有「期望与实际」措辞（会回显越界标量）；`_render_error` 不改。

**几何检查（通用后校验接缝，3.8.5）。**`segment_geometry.validate_regions(obj, boxes, width, height) -> PostValidationResult` 对 `regions` 数组逐区域按序检查；一个区域某步违规后跳过该区域其余步骤，继续检查后续区域，违规按输出顺序全部返回。下列文本是违规项正文（M8 加 `(post-validator) ` 前缀），不回显坐标或模型给出的值。

| 步 | 检查 | 违规文本 |
|---|---|---|
| 1 | `box_id` 与 `polygons` 恰好一个非空 | `/regions/<i>: exactly one of box_id and polygons must be set` |
| 2a | 没有参考框时 `box_id` 必须为 `null` | `/regions/<i>/box_id: must be null when no reference boxes are provided` |
| 2b | `box_id` 必须是本样本的参考框 id | `/regions/<i>/box_id: unknown reference box` |
| 2c | 同一个框最多被复用一次 | `/regions/<i>/box_id: reference box reused more than once` |
| 3 | 每个环首末相同 | `/regions/<i>/polygons/<j>/exterior: ring is not closed`；`/regions/<i>/polygons/<j>/holes/<k>: ring is not closed` |
| 4 | Shapely 构造每个部件并检查 `is_valid` | `/regions/<i>/polygons/<j>: invalid geometry (<类别>)`，类别取 `shapely.is_valid_reason` 在第一个 `[` 之前的部分，例如 `Self-intersection` |
| 5 | 多部件区域整体：`MultiPolygon(parts).is_valid` | `/regions/<i>: parts overlap or share an edge` |
| 6a | `box_id` 非空时 `source_box_ids` 必须为 `null` 或空 | `/regions/<i>/source_box_ids: must be null when box_id is set` |
| 6b | 没有参考框时 `source_box_ids` 必须为 `null` 或空 | `/regions/<i>/source_box_ids: must be null or empty when no reference boxes are provided` |
| 6c | `box_id` 为空时每个值都必须是本样本的参考框 id | `/regions/<i>/source_box_ids/<k>: unknown reference box` |

OGC 合法性下，部件之间单点相接合法，共边非法；孔中岛（外环带孔的部件加孔内另一部件）合法。

全部通过后构造 `RegionSet` 作为 proof：

- **编号**：复用区域的 `region_id` 就是框名；模型新画的多边形按其在模型输出中的出现顺序编号 `r1, r2, …`，复用区域不占编号。框名不会形如 `r<正整数>`，两类编号不相撞（D13）。
- **顺序**：`RegionSet.regions` 的元组顺序就是模型输出 `regions` 数组的顺序，复用与新画按输出位置交错；D15 路径为钩子返回顺序。
- **规范化**：复用区域 `box_id` 取该框，`source_box_ids = (box_id,)`；多边形区域 `box_id = None`，`source_box_ids` 按首次出现顺序确定性去重（重复不算违规、不占 L3 轮次）；`null` 与空列表都规范化为 `()`。
- **锚点**：多边形区域在网格空间对整个几何（Polygon 或 MultiPolygon）取 `point_on_surface`，再按 3.18.3 公式投影为 float；复用框取框中心 `((l+r)/2, (t+b)/2)`。

修复轮恒经 `_build_post_repair_prompt` 重放首轮 prompt（含图片与 `image_px`），L3 轮数为 `min(max_repair_attempts, 2)`；`output.repair_llm` 在图片模式下进入视觉必需集合。

### 3.18.6 叠图渲染

`RegionOverlayRef.load_base64(max_px)` 在 M9 编码请求时惰性渲染，字节用完即弃；样式是 `labelkit/common/contracts/regions.py` 的模块级冻结常量，不放在载体实例上。叠图只画候选，不画参考框。原图仍走 `ImageRef.load_base64`，文件本身不改。

| 常量或事项 | 冻结值 |
|---|---|
| 填充不透明度 | `OVERLAY_FILL_ALPHA = 64`（64/255，约 25.1%） |
| 轮廓宽度 | `OVERLAY_OUTLINE_PX = 1`，同色、不透明，填充合成之后绘制 |
| 颜色序列 | `OVERLAY_COLORS[i % 8]`：`(230, 25, 75)`、`(60, 180, 75)`、`(0, 130, 200)`、`(245, 130, 48)`、`(145, 30, 180)`、`(0, 170, 170)`、`(240, 50, 230)`、`(128, 128, 0)`；i 按 `RegionSet.regions` 顺序从 0 计 |
| 底色 | `OVERLAY_BASE_COLOR = (255, 255, 255)` |
| 标识字号 | `max(12, round(long_edge * 0.012))`，`long_edge = max(req_w, req_h)`；字体 `ImageFont.load_default(size=font_size)` |
| 标识样式 | `fill=(255, 255, 255)`，`stroke_fill=(0, 0, 0)`，`stroke_width=max(1, font_size // 8)`，全部不透明 |
| 编码 | 恒为 RGB 模式 PNG，`media_type` 恒为 `image/png`，不用 JPEG |

**请求尺寸。**先用 `scaled_size` 把原图缩到本次请求的工作点，再把像素几何与标注点按同一逐轴比例换算：`x_req = min(round(x_px * req_w / width), req_w - 1)`，`y_req = min(round(y_px * req_h / height), req_h - 1)`（Python 内置 `round`，half-even）。

**合成链路。**原图 → 长边超限时按 `scaled_size` 用 `Image.Resampling.LANCZOS` 缩放 → 含透明度的图（`mode` 为 RGBA、LA、PA，或 `"transparency" in info`）先 `convert("RGBA")` 再 `alpha_composite` 到白底 → `convert("RGB")` → `convert("RGBA")` → 合成填充层 → 描轮廓 → 绘制标识 → `convert("RGB")` → PNG。叠图恒在白底上呈现透明通道；原图请求不经这条链路，服务端怎样呈现透明通道不受 LabelKit 保证，所以带透明通道的原图在两张图里的呈现可能不同，这不影响几何。

**填充（D12）。**所有区域共用一张初始全透明的 RGBA 填充层，整层只与底图做一次 `alpha_composite`，所以任一像素的填充不透明度恒等于 `OVERLAY_FILL_ALPHA`，与重叠、嵌套层数无关。每个区域一张 L 模式掩膜：逐部件在部件掩膜上把外环填 255、该部件的内环填 0，再把各部件掩膜逐像素取最大值（`ImageChops.lighter`），结果与部件顺序无关，孔中岛正确着色。按元组顺序执行 `layer.paste((*color, OVERLAY_FILL_ALPHA), box, mask)`：paste 只写入、不混合，后画区域在重叠处覆盖先画区域的颜色，孔洞处保留其他区域已有的填充。掩膜只覆盖该区域在请求画布上的外接矩形（各边留 1 个请求像素余量并截到画布内），输出与整幅掩膜逐字节相同。

**轮廓。**填充合成之后，按元组顺序对所有外环与内环以该区域颜色 `ImageDraw.line(ring, fill=color, width=1)` 描线，让循环取色时相邻、共边且同色的两个区域仍能分辨。

**标识。**文本恒为完整的 `region_id`，不截断。位置取 `label_anchor` 换算后 `round`，`anchor="mm"`，画在合成与轮廓之后。用 `ImageDraw.textbbox(..., stroke_width=...)` 取含描边的外接框，越出画布则整体平移使其完整落在画布内；外接框比画布还宽（或高）时左（或上）对齐。平移只改变标识位置，不改变几何与 `label_anchor`。

**确定性与成本。**同一输入、同一 Pillow / FreeType 构建、同一 Shapely / GEOS 构建，字节相同；测试校验几何、标识位置与像素判据，不比较跨环境哈希。每条记录的渲染次数 = annotate 的 Σ(自洽样本 × 修复轮 × HTTP 尝试) + verify 的 Σ(评审请求 × HTTP 尝试)。单次成本与各区域外接矩形面积之和成正比，外加一次整幅合成与一次 PNG 编码，渲染在事件循环线程上进行；实测登记见图片分割规格 §11.8。单张叠图字节上界 `3 × req_w × req_h`，base64 后乘 4/3；1080 × 2340 缩到长边 2048（945 × 2048）时约 5.8 MB，base64 后约 7.7 MB。

**不落盘（D1）。**不新增输出通道。可核对的运行证据是 `_meta.regions`；叠图只存在于请求的生命周期内，运行结束后不能复现模型当时看到的叠图字节。

### 3.18.7 配置项

键的模式合法性、组合规则与示例见 5.2.2。解析产物 `SegmentConfig.resolved_box_parser` 与 `sequence_resolved` 永非用户键。rule 72–81 的冻结错误文本见图片分割规格 §6.3。

### 3.18.8 错误处理

记录级，无 `on_error` 键。归类器先调用 `budget.classify_stage_error`，再用标准词表（7.6）。

| 情况 | 结果 |
|---|---|
| `box_parser` 返回 `None` | `companion_invalid`，记录 `failed`；不调用模型、不进修复；不计 `bad_input` |
| `box_parser` 抛异常、返回值不合 3.18.4 契约 | `BoxParserError` → `internal_error`，记录 `failed`；不调用模型、不进修复；日志只记固定原因、框序号与钩子引用 |
| 首轮预检超限或服务端报超限 | `context_overflow`，记录 `failed`，计 `budget.overflow_records`；服务端 400 终局由 segment 阶段经 `budget.feed_reactive_terminal` 恰一次喂熔断 |
| Schema 或几何违规修复穷尽；修复轮溢出（D11） | `segmentation_invalid`，`stage = "segment"` |
| 输出截断 | `output_truncated`，与空候选严格区分 |
| provider 错误 | `provider_retryable_exhausted` / `provider_fatal`；`CircuitBreakerTripped` 与取消原样上抛。不沿用流模式「非预算错误一律 `segmentation_invalid`」的回落 |
| 校验函数异常或非法返回（`post_validator_exception` / `post_validator_invalid`） | 框架程序错误，`internal_error`，不进 L3 |
| 摄取之后图片不可读（文件被改动或删除、像素数据截断、`DecompressionBombError`） | `image_decode_error`，同一归类适用于 `load_base64` 的所有调用方 |
| 模型返回空列表，或 D15 路径下钩子对存在的伴随文件返回 `[]` | `RegionSet(regions=())`，合法结果，不判负、不补整图框、不进 rejects |

### 3.18.9 观测

- trace：每条成功记录一个 `segment.regions` 事件（7.2），payload 全部是计数，不携带几何数组，不携带框名。
- report：`report.segment_image` 块（6.4）；`usage` 用来拆分 segment 与 annotate 共用同一 profile 时的成本，数据来自 `ValidatedPostCall.usage` 与修复穷尽时的 `SchemaViolation.usage`。
- dry-run：写了 `instruction` 时 `segment_calls = estimated_records`（不含修复轮），只用参考框时为 0；不做会话预演（3.10.3）。
- 控制台：不显示 `/stream` 徽标，链段名单含 segment。

### 3.18.10 背书

空间定位与标注分立、文本候选通道与原图加叠图的依据见 1.5 v1.22 各行：Set-of-Mark [137] 与 OmniParser [139] 支持先给候选再按编号选择，UFO [140] 是干净图与标注图同发的最近先例，归一化网格来自 Qwen3-VL [144] 与 GLM-V [145] 的原生约定，几何合法性依据 Shapely [150] 与 RFC 7946 [151]。叠图是否有益是实验问题，由 `[PENDING-EVIDENCE:segment-image-overlay-ablation]` 取证。
