# 第 5 章　准备你的数据：格式、排布与常见陷阱

> 本章回答一个具体的问题：「我手里的数据要整理成什么样，LabelKit 才能吃进去？」
> 两种模态分开讲，每种都给出合法样例、判坏规则和排布建议；图片模式（`segment.mode = "image"`，v1.22）的输入规则见 5.6。

## 5.1 文本模态：JSONL 的规矩

### 输入可以是什么

`run.input` 指向：

- **单个 `.jsonl` 文件**，或
- **一个目录**——取目录下所有 `*.jsonl` 文件（按文件名字典序依次读取，不递归子目录）。

### 每一行的规矩

- 文件必须是 **UTF-8** 编码，行与行用 `\n` 分隔；
- **每行一个 JSON object**——`{...}`。合法 JSON 但不是 object 的行（比如一个裸数组、一个字符串）算**坏行**；
- **空行直接跳过**，不算坏行、不计数;
- 每行必须能命中 `input.text_field` 指定的字段（默认 `"text"`），命中的值就是这条记录的「正文」——去重、打分、标注都围绕它进行。

```json
{"instruction": "帮我写一条请假条，明天上午要去医院", "source": "ime-log", "ts": "2026-06-30T10:12:00Z"}
```

配 `text_field = "instruction"`，这行的正文就是那句请假条。**其余字段不会丢**：整行原样保存在记录的 `raw` 里，可以用 `output.passthrough_fields` 挑选透传到输出（第 8 章）。

### text_field 的三种命中形态

`text_field` 支持**点路径**，深入嵌套结构：

| 你的数据长这样 | text_field 写法 | 取到的正文 |
|---|---|---|
| `{"text": "..."}` | `"text"`（默认） | 字符串本身 |
| `{"conversation": {"turns": [...]}}` | `"conversation.turns"` | 命中的是数组/对象时，按规范化 JSON 序列化成文本（键排序、紧凑、中文不转义） |
| `{"query": "..."}` 但你写了 `"text"` | —— | **未命中 ⇒ 坏行** |

第二种形态值得展开：如果你的记录是多轮对话，直接把 `text_field` 指到轮次数组，整个数组会被序列化为一段稳定文本参与去重和打分——两条轮次完全相同的对话必然序列化出相同文本，精确去重照常有效。

### 坏行策略

`input.on_bad_line`：

- `"skip"`（默认）：跳过、计入 `report.counts.bad_input`、在 trace/stderr 留一条 `ingest.bad_line` 警告（含文件名、行号、原因）；
- `"fail"`：遇到第一条坏行立即以退出码 3 终止。

什么时候用 `fail`？当你的上游管道**保证**产出全合法数据时——此时任何坏行都意味着上游出了事故，快速失败比静默跳过更安全。探索阶段用默认的 `skip` 即可。

## 5.2 UI 模态：文件对与配对规则

### 排布形式

`run.input` 指向一个目录，LabelKit **递归**扫描它，把同编号的两个文件配成一条记录：

```
capture/2026-07-01/
├── uitree_1.jsonl      ┐
├── image_1.png         ┘ ⇒ 记录 (index=1)
├── uitree_2.jsonl      ┐
├── sub/image_2.png     ┘ ⇒ 记录 (index=2)   ← 允许跨子目录配对！
└── uitree_9.jsonl        ⇒ 缺图 ⇒ 缺对，默认跳过
```

配对规则的精确定义：

- 文件名模式：`uitree_<index>.jsonl` 与 `image_<index>.png/.jpg/.jpeg`；
- **index 是十进制整数**，允许前导零（`image_007.png` 的 index 是 7）；
- **整个输入目录（含全部子目录）共享同一个 index 命名空间**——这就是跨子目录配对的原理，也是冲突的来源；
- **冲突**：同一 index 出现两个树文件、或两个图像文件（包括同 index 同时有 `.png` 和 `.jpg`）⇒ 冲突。策略 `input.on_index_conflict = "fail"`（**默认，退出码 3**）或 `"skip"`（整个 index 跳过并计数）；
- **缺对**：index 只有单侧文件 ⇒ `input.on_missing_pair = "skip"`（默认）或 `"fail"`。

> **为什么冲突默认 fail 而缺对默认 skip？**
> 缺对通常是采集尾巴上的正常残缺；而同一编号出现两份文件，往往意味着两次采集批次被混进了同一目录——继续跑会让「哪张图配哪棵树」变成掷骰子。遇到冲突，正确动作是回头整理目录（比如每批一个子目录还不够，index 也得错开，或分成多次运行）。

### UI 树文件的格式

`uitree_<index>.jsonl`：每行一个控件节点的 JSON object。两种导出风格都接受，解析器自动探测：

- **平铺风格**：每行一个节点，用 `parent` 字段互相引用（首行没有 `children` 字段即判为此风格）；
- **嵌套风格**：整个文件只有一行，节点带 `children` 数组层层嵌套。

节点字段名有一张**兼容映射表**（覆盖 Android uiautomator dump、accessibility 导出与主流 GUI 数据集的习惯命名），按序取第一个存在的：

| 语义 | 接受的字段名 | 缺省时 |
|---|---|---|
| 节点 id | `id`, `node_id` | 自动编号字符串：平铺风格取节点所在行号；嵌套风格取深度优先遍历序号（"1","2","3"…） |
| 父节点 | `parent`, `parent_id` | null（根节点） |
| 角色/类名 | `class`, `className`, `type`, `role` | `"unknown"` |
| 文本 | `text`, `label` | `""` |
| 描述 | `content_desc`, `contentDescription`, `desc` | `""` |
| 边界框 | `bounds`（`[l,t,r,b]` 数组或 `"[l,t][r,b]"` 字符串） | `[0,0,0,0]` |
| 可见性 | `visible`, `visible_to_user` | true |
| 其余字段 | 全部收进 `extra`（值转字符串） | — |

一个合法的平铺风格样例：

```json
{"id": "0", "class": "FrameLayout", "bounds": [0, 0, 1080, 2340], "visible": true}
{"id": "1", "parent": "0", "class": "TextView", "text": "登录", "bounds": [72, 296, 264, 392], "visible": true}
{"id": "2", "parent": "0", "class": "EditText", "bounds": [72, 520, 1008, 664], "visible": true, "hint": "请输入手机号"}
```

`hint` 不在白名单里，于是进 `extra`——它不会丢，标注时 LLM 照样能看到（`hint=请输入手机号` 会出现在序列化树里）。如果你的导出字段名完全对不上这张表，在采集侧做一次字段重命名即可，不需要改 LabelKit。

空文件或全坏行的树文件 ⇒ 整条记录按坏记录跳过。

**只有图片？**普通 UI 模态下只有图片的编号记为缺对，只有图片的目录以 `no valid records` 退出。纯图片只在 `segment.mode = "image"` 下才能摄取，规则见 5.6。

### 图像的约束

- 格式：PNG / JPEG;
- 单文件大小 ≤ `input.max_image_mb`（默认 20 MB），超限按坏记录跳过；
- 接入时**只验魔数和尺寸，不解码全图**（文件字节会被完整读一次用于计算记录 id，算完即释放，见第 4 章）；开启去重时（默认开启），pHash 层会在去重阶段解码图像计算 64-bit 指纹（第 9 章）；真正的大开销操作——解码后按 profile 的图片采样工作点等比缩放并 base64 编码（v1.11 起工作点 = `default_image_px`，**0/缺省 = 沿用 `max_image_px`**，即旧行为；verify 修复换档时可在两者之间逐档上探，第 6、13 章）——发生在构造 LLM 请求那一刻，请求发出后立即释放。全程图像字节都不驻留内存，所以 10 万张截图不会撑爆内存。

## 5.3 时间序输入：给分段算子准备的流（v1.8）

打算开启分段算子（`[segment]`，第 25 章）时，输入还要满足**时间序**要求——分段是按「先后发生」切段的，顺序错了段就切错了：

- **排序依据**：默认 `stream.order_by = "input_order"`（文本 = 文件名字典序 → 行号；UI = 配对编号升序），你按采集顺序排布文件/行即可。文本模态还可以写 `"meta:<字段名>"`，让工具按每行 JSON 里的时间戳字段定序。
- **时间戳格式**（`meta:<field>`）：数值或字符串都收。数值按大小判单位——小于 10¹¹ 判**秒**、10¹¹ 到 10¹⁴ 判**毫秒**（自动换算），负数或过大判解析失败；字符串先按纯数字试上述规则，再按 ISO 8601 解析（含 `Z` 后缀与时区偏移；不带时区的按 UTC 解释）。解析失败的行与乱序的行同走 `stream.on_disorder` 策略（默认跳过并计数）。
- **输入须按分区键成组**：`stream.key` 声明的分区键（如设备号）是「键变即断会话」的 groupby 语义——工具**不做全量重排**，同一个键的记录必须在输入里连续成组。多设备交错混采的流，先在采集侧按设备拆开再喂进来。
- **UI 模态的特殊约定**：UI 记录没有随行 meta 字段，分区键用 `"source_dir"`（= 文件所在父目录，正好契合「一次采集一个目录」的惯例）；断会话用 `stream.gap_steps`（编号差超限即断），而不是时间差。

这些键的完整语义与会话切分规则见第 25 章。

**手上没有真实流？可以先生成一份。** sequence form（第 27 章）会写
`{output_stem}.stream.jsonl`，每行顶层是 `payload` 与 `_meta`。重放工程把
`input.text_field = "payload"`、`stream.order_by = "meta:_meta.event.timestamp"`，M2 会从同一文件重算
primary/noise/replay ID、owner 顺序与 duplicate provenance，并用每行自带的 `duration_us`、`resources` 和
`time_bindings` 复验业务时间。replay 必须是 source 的同一常量平移：非时间 payload 与下游 metadata 保持不变，
时间字段按 replay 起点重新绑定。M2 验证通过后才提供去除时间叶子的 exact dedup carrier；M3 对这类记录只运行
exact 层，不再用 MinHash 或 embedding 猜重复。任何 descriptor、payload、时间、顺序或 provenance 篡改都会在
ingest 阶段 fail closed。

## 5.4 数据排布的实践建议

**按批次/日期分目录。**UI 采集天然适合 `capture/<日期>/` 一天一目录、一次运行；文本数据一个大 JSONL 或按来源拆成几个文件放一个目录都行。

**别把两次采集混进一个 UI 目录**——index 冲突默认直接退出。宁可多跑几次。

**超过 50 万条，切开跑。**这是设计规模上限（内存 2–4 GB）。切分方法：文本按文件切、UI 按目录切，或 `--limit` 分次试探。注意切开跑意味着**全局去重的视野也被切开**（跨运行的重复查不出来），如果跨批重复是主要矛盾，优先保证同一来源的数据在同一次运行里。

**故意留脏数据没关系，但要知道谁来收拾。**坏行 / 缺对 / 超大图由 ingest 按策略处理；重复由 dedup 处理；「合法但低质」（哈哈哈哈）由 quality 处理。三层各管一段，你只需要保证数据在**格式上**过得了 5.1/5.2 的门槛。

**准备一个 20~50 条的小样本。**正式跑大数据前，用小样本 + `--limit` 全流程演练（配置、rubric、Schema、成本）能省下大量真金白银——这个工作流在第 15、16 章展开。

## 5.5 自查清单

开跑前对着这张单子过一遍：

- [ ] 文件是 UTF-8，每行一个 JSON object？
- [ ] `input.text_field` 和数据里的字段名对上了？（最常见的第一坑）
- [ ] UI 目录里 `uitree_*` / `image_*` 编号唯一、成对？
- [ ] `run.modality` 与数据形态一致？
- [ ] 输出目录已创建（`mkdir -p out`）？
- [ ] 跑过 `labelkit validate --probe`？
- [ ] 用 `--dry-run` 看过预计记录数和调用量？

其中「`input.text_field` 和数据里的字段名对上了」这条值得专门强调：`text_field` 写错时，**每一行都是坏行**。默认 `on_bad_line = "skip"` 下你会看到满屏 `ingest.bad_line` 警告（reason 为 `input.text_field "…" missed`），随后运行在读完输入时以**退出码 3** 终止，stderr 死因行形如：

```
InputError: no valid records: input.jsonl (scanned=14 bad_input=14 missing_pair=0 index_conflict=0)
```

「一条合法记录都没有」被视为输入错误而不是一次成功的空跑——看到这行，先查字段名。（配 `on_bad_line = "fail"` 则更早：第一条坏行处即退出码 3。只要有部分行合法，skip 策略照常跳过坏行跑完，坏行只计入 `bad_input`。）

## 5.6 图片模式：图片必需，伴随文件可选（v1.22）

`segment.mode = "image"` 时一张图片就是一个样本。LabelKit **不解析控件树**，也不判断图片旁边的数据是什么：工程注册 `segment.box_parser` 钩子，把自己的格式转成参考框数组（第 14 章 14.8）。不注册钩子就不读任何伴随文件。功能总览见第 28 章。

```
capture/
├── image_1.png     ┐
├── image_1.xml     ┘ ⇒ 样本 1，带伴随文件
├── sub/image_2.png ┐
├── image_2.xml     ┘ ⇒ 样本 2，允许跨子目录配对
├── image_3.png       ⇒ 写了 instruction：样本 3，无伴随文件；没写：缺对
├── image_9.xml       ⇒ 只有伴随文件：缺对
└── notes.txt         ⇒ 计入 counts.ignored_files
```

**发现规则：**

- 图片沿用 `image_<n>.png/.jpg/.jpeg` 命名与全局 index 命名空间，递归扫描，跨子目录共用。任意文件名的照片目录不在范围内。
- 同一 index 两张以上图片（包括 png 与 jpg 并存）是 `index_conflict`，遵循 `input.on_index_conflict`。
- 伴随文件只在注册了 `box_parser` 时参与发现：文件名是 `image_<n>` 加 `companion_suffix`（大小写严格匹配），按 index 与图片配对。同一 index 两个以上伴随文件也是 `index_conflict`。
- 只有伴随文件是缺对，遵循 `input.on_missing_pair`。只有图片时看配置：写了 `segment.instruction` 合法，模型依据图片生成候选；没写时伴随文件是候选的唯一来源，所以记缺对，计 `missing_pair` 与 `bad_input`，不产生记录，不会写成空候选。
- 既不是图片、也不是已声明伴随文件的文件计入 `counts.ignored_files`，并给一条只含计数的 WARN。它不计入 `scanned` 与 `bad_input`，不参与守恒式。未注册钩子时所有非图片文件都计入。
- `uitree_<n>.jsonl` 在图片模式下没有特殊含义。沿用这批数据时改名为 `image_<n>.jsonl`，声明 `companion_suffix = ".jsonl"`，再写一个钩子解析它。

**接入检查与记录身份：**

- 文件头由 ingest 检查：魔数、`input.max_image_mb`，再用 Pillow 只读文件头，格式必须可识别，`width × height` 不超过 Pillow 的 `MAX_IMAGE_PIXELS`。失败按坏记录计 `bad_input`。
- 伴随文件按原始字节读入，不解码、不解释。没有单文件大小上限，字节随记录驻留到批结束。钩子在事件循环线程上同步执行，解析很大的文件会推迟同批其他请求，所以**钩子要保持轻量**。
- 记录 id 是 `sha256(伴随字节 + 图片字节)` 的前 16 位。没有伴随文件与零字节伴随文件同 id，但后者仍会调用钩子。

**图片方向与对齐由你负责。**LabelKit 不读取、不应用、也不拒收 EXIF 方向标签。画布恒为存储像素网格，钩子收到的宽高、参考框与候选几何都按存储像素解释，参考框是否与图片对齐也由你保证，框架不做配准或缩放推断。

带方向标签的 JPEG 有一个已知残余：未缩放时原样发送并保留标签，服务端若按标签旋转显示，模型看到的方向可能与按存储像素绘制的叠图不同，segment 的网格坐标也会按存储画布投影而错位。**有方向标签的照片，请先把方向写进像素再交给 LabelKit**，并让伴随数据的坐标对应转换后的图片：

```python
from PIL import Image, ImageOps

with Image.open("raw/image_7.jpg") as image:
    ImageOps.exif_transpose(image).save("capture/image_7.jpg", quality=95)
```

实跑一张 400 × 300、Orientation = 6 的 JPEG，转换后为 300 × 400，方向标签消失。

**透明通道。**叠图恒把透明通道合成到白底；原图请求不经这条链路，未缩放时原样发送，透明通道怎么显示由服务端决定。所以带透明通道的原图在原图请求与叠图里可能呈现不同（不影响几何），**建议交付不透明图片**。

自查：`[segment]` 写了 `mode = "image"`？伴随文件后缀与 `companion_suffix` 大小写一致？`counts.ignored_files` 是预期的数吗？
