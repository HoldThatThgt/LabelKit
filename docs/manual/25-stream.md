# 第 25 章　流模式 stream：会话化、语义分段与动作摘取

> 流模式是 v1.8 新增的一组能力：把**按时间顺序采集的屏幕状态流**（录屏抽帧 + UI 树）
> 先切成一段段「用户在做一件事」的 episode，再逐帧对推断出中间发生的动作，
> 最后以**序列**为单位完成打分、标注与评审。
> 读完本章你应当能回答三个问题：**什么样的数据该开 stream？边界与噪声是怎么判出来的？
> 序列产物的账怎么对？**本章样例全部来自 `examples/stream` 两个工程的真实运行：
> UI 流工程 `project.toml`（本章借它的 s1 会话讲 v1.8 基线，它同时开着 v1.9 的缝合——
> 缝合层的机制与账目整体放在第 26 章）与纯文本流工程 `project-text.toml`；
> 帧粒度小节 25.6 的样例另取自双粒度工程 `examples/mix` 的真实运行
> （UI 控件树主工程，DeepSeek + z.ai 双端点分工）。

## 25.1 为什么要分段：时间轴上没有「一条记录」

前面所有章节都默认一件事：输入里的**每一行/每一对就是一条独立记录**，标注单位与采集单位天然重合。但屏幕操作流不是这样采的——录屏抽帧得到的是「首页、搜索页、结果页、详情页、弹窗、购物车……」一长串状态截面，**单帧什么都说明不了**：训练侧要的样本是「用户搜索并下单了一次外卖」这样的完整任务段，而任务的边界、中间混入的通知弹窗、乃至「两帧之间用户到底做了什么」，在原始数据里根本没有字段承载。拿 v1.7 的流水线硬跑这种数据，得到的是逐帧的碎片标注：帧级去重在连续 UI 帧上大面积误伤，质量分打在单帧上毫无意义。（v1.12 起流模式内也有帧粒度产物——但那是 opt-in 的**第二层**产物：以段为单元跑完整条链之后，帧级分类与标注挂在 episode 行内随序列一起交付（25.6），与这里说的「把帧当独立记录逐帧硬跑」是两回事。）

流模式把「原始帧流 → 训练样本」拆成一条新的加工链，四层各管一段：

1. **会话化**（`[stream]`，M2 规则层）：按声明的顺序与断开规则，把帧流粗切成候选会话——纯代码、零 LLM；
2. **语义分段**（`[segment]`，M14 算子）：LLM 滑窗逐帧裁决「这一帧相对进行中的活动是什么角色」，代码按固定规则从关系**演绎**出边界与噪声帧，每段拼装成一个 episode（序列记录）；
3. **动作摘取**（`[extract]`，M15 算子）：对 episode 内每对相邻帧，LLM 推断「两帧之间发生的单个语义动作」，写成结构化步骤序列；
4. **下游序列适配**：去重、打分、标注、评审全部改以 episode 为单位——轨迹 rubric 打结构分、标注看动作序列 + 关键帧、评审带缺陷表并能对成员集做「手术」。

四层接进既有链序，就是流模式的完整加工链（本图作本章与第 26 章共用的地图；缝合默认关，机制在第 26 章）：

```mermaid
flowchart LR
    frames["帧流（时间序输入）"] --> sess["会话化（[stream] 规则层）"]
    sess --> seg["语义分段 segment"]
    seg --> sti["线索缝合 stitch（默认关）"]
    sti --> ded["去重 dedup"]
    ded --> cls["序列分类 classify"]
    cls --> ext["动作摘取 extract"]
    ext --> qua["轨迹打分 quality"]
    qua --> ann["序列标注 annotate"]
    ann --> ver["评审 verify"]
```

一条与真实数据打交道时躲不开的指引：用户常在任务间来回切换——外卖点到一半切去回消息、回来接着下单，这种**穿插**会让分段把同一个任务正确地切成多个碎片（分段的单元本来就是「连续做一件事」）。本章通篇讲的是不缝合的基线形态；要把穿插碎片按任务线索缝回完整记录，开 v1.9 的缝合算子（`[stitch]`，第 26 章）。

这套形态不是发明：从状态对反推动作是 OpenAI VPT 的逆动力学模型与 OS-Genesis 逆向任务合成的既有工序，滑窗 LLM 边界裁决是 2026 年 GUI 轨迹量产管线（Video2GUI 等）仍在用的形态之一，LabelKit 按自己的负边界（不训练本地模型）用运行时 LLM 充当这两个角色。**什么时候开**：输入是按时间排好的操作流（UI 模态的截图 + 树对，或带时间戳的文本事件流）、且你要的样本单位是「活动段」而非单条记录。开关是 `segment.enabled = true`，约束：仅 process 模式、必须开 annotate、与 generate 互斥；`extract` 再要求 UI 模态。默认全关——不开时行为与 v1.7 逐字节一致（输出只多一个恒为 null 的 `_meta.stream` 键）。

**手上没有真实流呢？**时间流生成在 `generate_only` 下从零合成多会话流。两者是镜像关系：本章从帧流演绎序列，生成侧直接铺设序列，所以 segment / stitch / extract 不参与。v1.16 有规则或日历窗口时，结构不再由 LLM 蓝图与后置随机织造共同碰撞，而由联合 planner 在 LLM 前冻结。工件仍是本章的合法输入；owner 相邻帧始终满足 `delta <= stream.gap_s`，因此 crossing 同伴作废后重放也不会把幸存 owner 切断。

## 25.2 快速上手：examples/stream 全流程

仓库自带的 `examples/stream`（`project.toml`）是一个 53 帧、五个场景子目录的 UI 操作流工程——时序流格式能开的算子全开（分段、缝合、去重、分类、摘取、轨迹打分、序列标注、评审修复；generate 与 stream 互斥）。本章用它的第一个会话 `s1-serial-noise/`（帧 1–14）讲 v1.8 的分段与摘取基线：任务 A「点外卖」帧 1–8（其中帧 5 是突然插入的社交 App 消息屏——预期噪声）、任务 B「打车」帧 9–13 背靠背、帧 14 回到桌面；其余四个会话是穿插与救援场景，属第 26 章缝合的舞台（本次真跑 s1 自己也贡献了一次缝合——见 25.2 与 26.2）。fixture 由 `tools/gen_fixtures.py` 一次性确定性生成（树是唯一语义源，截图为 PIL 程序化绘制），刻意埋了「实体跨屏延续」的线索：餐厅名「川味麻辣烫」跨帧 3/4/6 出现、金额 ¥32 跨帧 4/6/7。逐节看 `project.toml`。

**第一节：会话化与分段。**

```toml
[stream]
order_by = "input_order"          # UI 模态 = pair_index 升序（meta:* 仅文本模态）
key = ["source_dir"]              # 分区键：每个场景子目录一个会话

[segment]
enabled = true                    # stream 模式总开关
strategy = "hybrid"               # 滑窗 LLM 边界精化 + 逐帧噪声标记
window = 16                       # 窗上限，≥ 最长会话（15 帧）：预算装得下整段 ⇒ 每会话恰一窗（25.5）
min_len = 2                       # 仅作用于 LLM 精化切出的段
context = "…"                     # 域上下文声明（本工程为穿插流写了长版，全文与解读见第 26 章）
```

`[stream]` 声明「帧流怎么排、会话在哪断」：本例用分区键 `source_dir` 让**每个子目录成为一个会话**。`[segment]` 是流模式总开关；`window` 自 v1.11 起是**上限**——所引 profile 声明了 `context_window` 时（本仓库示例配置就声明了 131072），窗口按预算**贪心装填**、装满或到上限即封窗，未声明预算时保持定长窗（步长 = window−1；两种形态都重叠 1 帧、接缝帧判决归后窗）。本工程 16 ≥ 最长会话且预算装得下整段（启动 INFO 报最坏也能装 46 帧，25.5），滑窗退化为**每会话恰一窗**。v1.11 的另一处变化：窗口**是否附图没有独立开关**——`segment.llm` 指向的 default profile `supports_vision = true`，UI 模态下窗口自动逐帧附截图（选 profile 即选能力，25.5/25.7）。`context` 只是可选域上下文——**边界判据内置于固定模板，零配置可用**，这行不是必需品。

**第二节：摘取与序列打分。**

```toml
[extract]
enabled = true                    # 逐相邻帧对摘取动作，写入 _meta.stream.steps
llm = "default"

[quality]
enabled = true
mode = "pointwise"
rubric = "default:trajectory"     # 轨迹四准则；无 threshold——只打分不筛
```

注意两件事：rubric 用的是 v1.8 新内置的 `default:trajectory`（完成度/连贯性/目的性/噪声残留四准则，附录 B）——事实上流模式下 rubric 留空也会解析到它；**没配 threshold**——序列样本贵，先把分打出来、下游按分后筛（第 8 章的「门控留宽」策略在这里几乎是标配）。

**第三节：序列标注与带手术的评审。**

```toml
[annotate]
enabled = true
llm = "default"
instruction = """
你是移动端操作序列标注员。根据动作序列与关键帧，
标注该操作序列的任务标签（用户在做什么）、所属应用与一句话摘要。
被打断后恢复的任务请标注其完整任务（接缝步表示任务曾被打断）。
"""

[verify]
enabled = true
llm = "judge"
policy = "repair"                 # 缺陷表路由：成员手术 + 重摘取 + 重标注
max_repair_rounds = 1

[trace]
enabled = true
channels = ["segment", "stitch", "extract", "classify", "verify", "schema"]
content = "refs"

[output]
meta_mode = "inline"
rejects = "full"                  # 噪声帧 rejects 行携带完整载荷（序列 = 成员清单）
# schema_inline = …               # task_label / app / summary 三字段的输出 Schema，略
```

（工程还开着 `[classify]`——episode 序列照常分类：摘要 + 首帧截图入提示词、shopping 类挂了按类标注指令，机制见第 24 章；`[stitch]` 见第 26 章。）trace 通道枚举 v1.8 从 8 值扩到 10 值（v1.9 再加 `"stitch"` 成 11 值）：`"segment"` 与 `"extract"` 都**不在默认订阅集**里，想审计边界判决必须显式加（与第 24 章的 `"classify"` 同款约定）。跑起来：

```bash
cd examples/stream && mkdir -p out
set -a && source ../../.env && set +a
uv run labelkit run --config ../config.toml --project project.toml
```

启动段先看到两行 v1.11 的预算 INFO（第 16 章），stream 工程多出的第二行是 segment 的最坏装填量（省略时间戳）：

```
INFO  run     batch=0 budget: default=131072/113868 judge=131072/115916
INFO  run     batch=0 segment: w_min=46 window=16 (budget)
```

stderr 尾部的终版摘要（真实运行，退出码 0，全程约 248 秒）：

```
   ── final summary (matches report.counts item by item) ──
   scanned=53  ingested=53  bad_input=0  generated=0
   dropped_dup=0  dropped_lowq=0  dropped_verify=0  failed=1  emitted=8
```

53 帧进来，主输出只有 **8 行**——这不是丢了数据，是换了记账单位：45 帧被吸收进 13 个 episode（状态 `absorbed`，其中 4 个 episode 又被缝合并壳，第 26 章）、8 帧成了噪声（`dropped_noise`）；9 条线索里还有 1 条死在轨迹打分上（`failed=1`——一次打分调用把输出上限写满，按 v1.11 的 `output_truncated` 记录级拒收，25.4），守恒恒等式的完整验算见 25.4。聚焦 s1 会话：14 帧产出 2 行，与人工预期一致——但这次分段把任务 A 切成了 4+3 两段（帧 5 的消息屏被剔为噪声后，帧 6 的「切回购物车」被判成新流程的开始——正是本工程 `context` 声明的口径，第 26 章），缝合层随即按实体延续把两段并回一条线索；任务 B 成一段（5 成员）、帧 14 落进 rejects。

## 25.3 机制四层：从帧流到步骤序列

**第一层：会话化规则层（`[stream]`，纯代码）。**`order_by` 声明顺序来源——`"input_order"`（默认：文本 = 文件名字典序→行号，UI = pair_index 升序）或 `"meta:<字段>"`（仅文本模态，按时间戳字段排序校验；epoch 秒/毫秒与 ISO 字符串怎么解析、输入怎么排布见第 5 章）。会话断开由四组规则任一触发：分区键变化（`key`，如 UI 模态的 `"source_dir"`——一次采集一目录）、时间间隙（`gap_s`）或序号间隙（`gap_steps`）、硬上限（`session_max_len` / `session_max_span_s`）。工具**不做全量重排**，只做流式单调性校验：乱序/时间戳解析失败的记录按 `on_disorder = "skip"`（默认，计 bad_input）或 `"fail"`（退出码 3）处置。每个会话闭合发一条 `segment.session` 事件——本次真跑五条：cause=`key` 四条（子目录切换处断开）+ cause=`eof` 一条。时间戳会话化看姊妹工程 `project-text.toml`（13 行带 `ts` 的输入法请求流，`order_by = "meta:ts"` + `gap_s = 900`）：真跑切出三个会话，cause=`gap` 两条 + `eof` 一条——上午的差旅安排与周报（此后静默约 3 小时）、中午的合同翻译（此后静默约 5.5 小时）、晚间对翻译三连的逐字重发（序列判重的活展品，见本节第四层：`dropped_dup=1`）。

**第二层：segment 的三步演绎滑窗。**`strategy = "hybrid"`（默认）时每窗一次 LLM 调用，但 LLM **不直接回答「这里是不是边界」**——模板固定为三步作业：先通读全窗做双向上下文概括，再对每帧做**封闭词表**的关系分类（M8 enum 硬校验，词表外输出在结构层就被拦下），边界与噪声由代码查表演绎。五个关系值的通俗读法：

| relation | 通俗含义 | 演绎结果（代码查表） |
|---|---|---|
| `continues` | 同一流程的正常推进 | 非边界 |
| `advances` | 屏幕甚至 App 变了，但任务实体（订单号、餐厅名、验证码）跨屏延续——跨 App 的同一任务属此值 | 非边界 |
| `returns_to_entry` | 回到入口/搜索/桌面后开启新流程（同 App 背靠背任务的断点） | **边界**：该帧是新段第一帧 |
| `context_switch` | 交互对象与环境不连续且无实体延续——「相关但无实体延续的新流程」也取此值 | **边界**：该帧是新段第一帧 |
| `interruption` | 与前后活动均无关的短暂插入：通知、弹窗、误触 | noise（剔除出段） |

`advances` 与 `context_switch` 的分界钉死为**实体延续**——这正是 fixture 埋「川味麻辣烫」跨屏线索的原因。本次真跑的 s1 判决：帧 5 弹进社交 App 被判 `interruption`；帧 9 切进打车 App 被判 `context_switch`（无实体延续、开新段）；帧 6 回到购物车**也**被判 `context_switch` 开了新段——不是 `advances`，因为本工程的 `context` 把「切回被搁置任务收尾」显式钉为新流程的开始（为缝合制造碎片，第 26 章逐要点解读），审核员的理由原文正是这么写的（25.5 的 trace 样例）；实体线索也没白埋——它随后成了缝合判定 `entity_overlap` 先验的证据（第 26 章）。三条硬规则：**会话首帧恒为段首**（rel[0] 的边界值不参与判决，noise[0] 照常生效）；接缝帧（前窗末帧 = 后窗首帧）的判决**整帧归后窗**；`min_len`（默认 2）**只作用于 LLM 精化切出的段**——短段帧以 `below_min_len` 的 reason 进 rejects，**≠ `noise`**：它未经噪声判据裁决，不得污染噪声审计口径，计数也独立（`report.stream.below_min_len`）。规则层的孤帧/短会话（含 `strategy="rules"`）不经 min_len、原样成 episode。单窗结构修复耗尽按 `segment.on_error = "keep"`（默认）降级：该会话整体成一个 episode 并在 `_meta.stream.degraded` 留痕，记录存活。

**第三层：extract 的动作词表与 diff 证据。**对每个 episode 的每对相邻成员帧一次调用（转移数恒 = 成员数 − 1），锚定句移植自 OpenCUA：「前一帧是动作发生前最后一个稳定状态，后一帧是动作完成后的首个稳定状态；推断二者之间的**单个语义动作**；连续滚动、连续键入归并为一步」。`action_type` 是 11 值封闭词表（AndroidControl 全集 ∪ UI-TARS-mobile 增量 + 兜底）：

```
click / long_press / drag        点击 / 长按 / 拖拽（target = 控件文本引用，不用坐标）
input_text                       键入文本（value = 所键入内容；聚焦点击不单独记步）
scroll                           滚动（value = up/down/left/right 四向）
open_app / app_switch            打开应用 / 切换到另一已打开应用（value = 应用名）
navigate_back / navigate_home    系统返回 / 回桌面
wait                             无交互，仅等待界面加载
other                            无法归类（语义写进 description）
```

`include_diff = true`（默认）时提示词额外注入 `[树变更摘要]`——两帧 UI 树的**结构化 diff**（增/删/文本变化节点数、变化比例、App 是否变更），零额外调用。这与像素 diff 是两回事：像素 diff 注入在业界报告里是负结果，结构化 diff 则是确定性归并证据，用来缩短视觉推断距离、压幻觉。单步修复耗尽按 `extract.on_error = "fallback"`（默认）写兜底步：`action_type="other"` + `detail` 留痕——**与 LLM 确证的 other 可区分**（看 detail.kind 在场与否），episode 存活。

**第四层：下游算子的序列适配。**episode 是 `kind="sequence"` 的记录（成员帧转入 `absorbed` 状态、不再独立产出——这是 Stage 契约新增的受控例外「分段吸收例外」，spec §4.3；第 4 章），下游全部换序列口径。v1.9 起 segment 与下游之间还有一个可选的缝合算子（`[stitch]`，第 26 章），把同会话内被穿插切开的 episode 碎片并成线索（Stage 契约的缝合改绑例外，spec §4.3），开启后下面各算子看到的单元相应从 episode 升级为线索：

- **dedup**（第 9 章）：序列的判重文本 = 成员配方按序拼接，episode 级重复 = 「同样的操作流程」；pHash 层自动跳过（序列记录无自己的图）。真实展品在 `project-text.toml` 的真跑里：晚间会话对合同翻译三连的逐字重发，episode 判重配方与中午那段逐字一致——`stage="dedup", reason="exact"` 落拒绝通道（`rejects="full"` 档的载荷是成员清单 `{"kind": "sequence", "member_ids": […], "member_sources": […]}`）；
- **quality**（第 10 章）：证据 = `[步骤序列]`（extract 产物的文字渲染，fallback 步与确证 other 分列）+ `[成员帧摘要]`，**全程无图**——trajectory rubric 的四条准则（完成度/连贯性/目的性/噪声残留）全是结构性判据，不需要逐帧看图（25.7 有展开）。extract 关了也能打：「步骤」退化读作「帧间变化」（M1 会给 warning 提示这个组合）；
- **annotate**（第 11 章）：序列模板 = `[动作序列]` 逐步行渲染 + 关键帧图 + `[成员帧摘要]` 收尾。关键帧数以 `annotate.sequence_frames`（默认 20）为**上限**：v1.11 的预算装填先给足文本块，图片吃剩余份额——实发帧数 `k_eff = min(sequence_frames, 预算余量 ÷ 每图成本)`，首末帧恒保留、中间均匀降采样（预算宽裕时 k_eff 就等于上限，本工程即如此）；
- **verify**（第 13 章）：评审输出在意见/结论之外多一张**缺陷表**（六值：`label_mismatch` 标签不符 / `off_task_members` 混入无关帧 / `missing_head` / `missing_tail` 切头切尾 / `missing_members` 段中缺帧 / `wrong_stitch` 缝合错误——v1.9 增，词表闭集恒在场、仅开缝合时可判），证据段含 `[边界余量]`——段边界外前后各 2 帧的摘要及去向，专防切头切尾。`policy = "repair"` 时按缺陷路由**成员手术**：收缩（把无关帧逐出段，reason=`off_task_member`）与回收（把批内同会话的噪声帧复裁后接回），手术后接缝重摘取、transitions 重编号、重标注复审，全程两阶段批级结构保证并发下确定性；修复过的行带 `_meta.stream.repaired = true`，不重打分。

## 25.4 输出怎么读

**主输出**一行 = 一个 episode（缝合开启时 = 一条线索——单碎片线索就是原样的 episode）。真实运行产物第 1 行（s1 的任务 A，格式化展示；`steps` 的六步全文照录。这条线索由两个碎片缝成——碎片机制在第 26 章，这里先看 `_meta.stream` 的骨架）：

```json
{
  "task_label": "在美食外卖App下单川味麻辣烫招牌麻辣烫×1，合计¥32",
  "app": "com.example.food",
  "summary": "搜索麻辣烫，在川味麻辣烫店下单招牌麻辣烫×1，合计¥32，提交订单成功。",
  "_meta": {
    "id": "adb47af96b0dc69a",
    "run": {…},                              ← 与既有形态一致，从略
    "source": {"file": "s1-serial-noise/uitree_1.jsonl", "pair_index": 1,
                "generated_from": [], "fields": {}, "generator": null},   ← 继承首成员的溯源
    "stream": {
      "episode_id": "adb47af96b0dc69a",      ← 恒等于本行 id（episode 自述）
      "thread_id": "adb47af96b0dc69a",       ← v1.9 键：仅 stitch 开启时在场（第 26 章）
      "session_id": "f00e41052479a460",      ← 所属会话（同会话的段共享此值）
      "order_span": [1, 8],                  ← 首末成员的序键（本例 = pair_index）
      "member_count": 7,
      "member_ids": ["873a403914352fd1", "98a8e0836890fa51", "16ceb575dc626695",
                      "117fda9c33c823fc", "89fccaa682b52227", "e1b72b64b4a7164c",
                      "d565f6f279ebec42"],   ← 成员帧 id，序键升序
      "member_sources": [{"file": "s1-serial-noise/uitree_1.jsonl", "pair_index": 1},
                          {"file": "s1-serial-noise/uitree_2.jsonl", "pair_index": 2},
                          {"file": "s1-serial-noise/uitree_3.jsonl", "pair_index": 3},
                          {"file": "s1-serial-noise/uitree_4.jsonl", "pair_index": 4},
                          {"file": "s1-serial-noise/uitree_6.jsonl", "pair_index": 6},   ← 5 缺席：噪声帧
                          {"file": "s1-serial-noise/uitree_7.jsonl", "pair_index": 7},
                          {"file": "s1-serial-noise/uitree_8.jsonl", "pair_index": 8}],
      "session_split": false,                ← 所属会话曾被 batch_size 硬切过吗（25.7）
      "repaired": false,                     ← verify 手术改写过成员集吗
      "degraded": null,                      ← segment 失败降级留痕（on_error="keep" 时）
      "fragments": [{"order_span": [1, 4], "member_count": 4, "cause": "origin",
                      "source_episode": "adb47af96b0dc69a"},
                     {"order_span": [6, 8], "member_count": 3, "cause": "resumed",
                      "source_episode": "6b3bd10a0de116ea"}],
                                             ← v1.9 键：碎片装订记录——本行两碎片 = 缝合并回的
                                                任务 A；单碎片 = 没缝过（读法在第 26 章）
      "steps": [                             ← extract 产物；关 extract 时恒 null
        {"index": 0, "action_type": "click", "target": "搜索美食", "value": null,
         "description": "点击首页顶部的搜索框，进入搜索页面", "resumed": false},
        {"index": 1, "action_type": "click", "target": "*麻辣烫", "value": null,
         "description": "在搜索页面的热门搜索中点击\"麻辣烫\"标签，进入麻辣烫搜索结果页", "resumed": false},
        {"index": 2, "action_type": "click", "target": "川味麻辣烫", "value": null,
         "description": "在搜索结果列表中点击\"川味麻辣烫\"进入该餐厅详情页", "resumed": false},
        {"index": 3, "action_type": "click", "target": "加入购物车", "value": null,
         "description": "用户点击了\"加入购物车\"按钮，页面从商品详情切换到购物车页面，显示已添加招牌麻辣烫×1，合计¥32", "resumed": false},
        {"index": 4, "action_type": "click", "target": "去结算", "value": null,
         "description": "点击\"去结算\"按钮，从购物车页面进入确认订单页面", "resumed": false},
        {"index": 5, "action_type": "click", "target": "提交订单", "value": null,
         "description": "点击\"提交订单\"按钮，提交订单后页面跳转至下单成功页面", "resumed": false}]
    },
    "scores": {"coherence": 1.0, "purposefulness": 1.0, "noise_residue": 1.0,
                "completion": 1.0, "__aggregate__": 1.0,
                "mode": "pointwise", "batch_no": 1, "pool": "shopping"},
    "dedup": {"kind": "unique"},
    "classification": {"label": "shopping", "labels": ["shopping"], "source": "llm"},
    "annotation": {"model": "glm-5.2", "attempts": 1},
    "verification": {"verdict": "pass", "rounds": 1, "defects": []}   ← stream 行恒带 defects 键
  }
}
```

逐键读 `_meta.stream`：`member_sources` 是完整成员溯源（每帧来自哪个文件哪个 index——`source` 键只继承首成员），拿它能把 episode 还原回原始帧；`order_span` 与 `member_count` 对不上（跨度 8、成员 7）就说明段内有帧被剔了。v1.12 起这里还可能多一个 `members` 键（`member_sources` 之后、`session_split` 之前）：帧粒度任一开关开启时在场，逐成员给出帧类标签、帧级标注与状态位——本工程没开帧粒度所以缺席，读法与真实样例在 25.6。`thread_id`、`fragments` 与步行内的 `resumed` 是 v1.9 增键，**仅本工程开着 `[stitch]` 才在场**（读法在第 26 章；关掉缝合，这三处消失，主输出与 v1.8 逐字节等价）。留意这行的 `steps` 里**没有**接缝占位步（六步全是真实转移、`resumed` 全 false）：两个碎片的间隙里只有噪声帧 5，按判据不构成接缝——这条辨析在第 26 章展开。顶层三个字段仍是你的 Schema 产物——**输出结构照旧由全局 Schema 管**，stream 改变的只是「一行代表什么」。另两处细节：`verification` 在流模式恒带 `defects` 键（无缺陷 = 空数组）；判分噪声这次落在了别的行上——s4 的新闻浏览线索被打了 `noise_residue` 0.0、`completion` 0.4（聚合 0.55），对一条干净的三帧浏览流来说是个可疑判决，但因为没设 threshold，它只是个随行落盘的分数。**stream 工程默认只打分不筛**的价值就在这：判分的噪声不会变成数据的损失，后筛时你还有机会用 trace 复核。

**拒绝通道**是噪声帧的去向（`rejects = "full"` 档；s1 的两行 `_meta` 逐字如下，`record` 载荷——该帧的树文本与图路径——以 `{…}` 略去）：

```json
{"_meta": {"id": "c51c341656eb8447", "source": {"file": "s1-serial-noise/uitree_5.jsonl", "pair_index": 5, "generated_from": []}, "stage": "segment", "reason": "noise", "errors": [], "label": null}, "record": {…}}
{"_meta": {"id": "47d1c7373d1fa7fb", "source": {"file": "s1-serial-noise/uitree_14.jsonl", "pair_index": 14, "generated_from": []}, "stage": "segment", "reason": "below_min_len", "errors": [], "label": null}, "record": {…}}
```

两个 reason 别混：帧 5 是 LLM 判的 `interruption`（reason=`noise`，社交 App 消息屏）；帧 14 是「`returns_to_entry` 开了新段、但段里只有它自己（1 < min_len=2）」的 `below_min_len`——桌面屏不是噪声，只是不够成段。审计噪声率时只数 `noise`，别把 `below_min_len` 算进去。verify 手术收缩逐出的帧是第三种组合：`stage="verify", reason="off_task_member"`；本次真跑还有第四种——那条死于打分调用输出截断的线索以 `stage="quality", reason="output_truncated"` 落 rejects（v1.11 的记录级错误码，第 8、18 章），它的 `record` 载荷同样是成员清单。

**报告**多了两块。`counts` 增三键（真实产物；`stitched`/`threads` 是 v1.9 键，第 26 章）：

```json
"counts": {
  "scanned": 53, "ingested": 53, "bad_input": 0,
  "dropped_dup": 0, "dropped_lowq": 0, "dropped_verify": 0,
  "failed": 1, "generated": 0, "emitted": 8,
  "episodes": 13, "absorbed": 45, "dropped_noise": 8,
  "stitched": 4, "threads": 9
}
```

v1.8 的守恒恒等式全展开形（第 4 章原式的超集，未启用项恒 0 时退化回原式；`stitched` 为 v1.9 增项）：

```
emitted + dropped_dup + dropped_lowq + dropped_verify + dropped_noise + failed + bad_input + absorbed + stitched
  = scanned + generated + fanout + episodes
```

代入验算：左 = 8 + 0 + 0 + 0 + **8** + 1 + 0 + **45** + **4** = 66；右 = 53 + 0 + 0 + **13** = 66。✓ 直觉读法：右侧 `+ episodes` 是因为每个 episode 都是凭空追加的新信封（与 classify 扇出的 `fanout` 同构），左侧 `absorbed + dropped_noise` 则是原始帧的两种新去向（`stitched` 的壳记账在第 26 章展开；注意 failed 的那条线索的 45 个成员帧照旧记在 `absorbed` 里——信封死了，帧的去向账不变）。新增的 `stream` 节（真实产物，`by_type` 其余 8 个动作类型本次全为 0、以 `…` 略；`stitch` 子块留给第 26 章）：

```json
"stream": {
  "sessions": 5, "episodes": 13, "mean_episode_len": 3.46,
  "absorbed": 45, "dropped_noise": 8, "below_min_len": 2,
  "digest_poor_frames": 0, "segment_failures": 0,
  "windows": 5,
  "stitch": {…},
  "extract": {"transitions": 32, "fallback_steps": 0, "failures": 0,
               "by_type": {"click": 30, "input_text": 1, "scroll": 1, …}},
  "verify": {"membership_repairs": 0, "boundary_flags": 0,
              "defects": {"label_mismatch": 0, "off_task_members": 0,
                           "missing_head": 0, "missing_tail": 0, "missing_members": 0,
                           "wrong_stitch": 0}}
}
```

对账四连：`windows=5`（v1.11 增键）= segment 实际切出的窗数，拿它对账 dry-run 估算的上界（本工程估 5、实 5——预算装得下整段、装填顶格，25.5 成本账）；`transitions=32` = 各线索 Σ(成员数 − 1) = 36 再减去 4 个接缝占位步（占位不计入摘取账，第 26 章）；`dropped_noise=8` 里有 1 条是 `below_min_len`（独立计数拆给你看——`below_min_len=2` 是**发生**计数，另一次命中的帧被缝合救援翻回了 `absorbed`，第 26 章）；`mean_episode_len=3.46` = 45 成员 ÷ 13 段。`fallback_steps` / `segment_failures` / `verify.defects` 全为 0——分段与摘取是一次干净的运行，这些计数器不为零时的读法在 25.5。

## 25.5 调优与审计闭环

**三个旋钮，按影响面排序。**其一，`gap_s` / `gap_steps`（会话粒度）：gap 偏大 = 欠分割，还有 LLM 精化兜着；gap 偏小 = 过分割，**段一旦切碎就再也拼不回来**（LLM 只在会话内精化，v1.9 的缝合算子同样只在会话内缝——跨会话永远无解，第 26 章）——这就是 `gap_s` 默认给到 300 秒偏大值的结构性理由，宁欠勿过。其二，`segment.window`（单窗帧数上限）：窗内上下文越足判得越稳，业界证据甚至偏向「整段单调用」形态——会话普遍不长时直接把 window 调到 ≥ 会话长度，滑窗天然退化为整段单调用；v1.11 给这句话补了一个**预算前提**：所引 profile 声明 `context_window` 后窗口按预算贪心装填（每窗帧数 ≤ window、装不下就封窗开新窗，溢出还有对半改切的降级重试兜底），「window ≥ 会话长即单窗」只在**整段也装得进输入预算**时成立。看启动 INFO 行心里就有数：本工程 `segment: w_min=46 window=16 (budget)`——最坏也能装 46 帧、远超 16 的上限，装填顶格、行为与定长窗一致（`window=16` 就是这么定的）；反过来 w_min < window 时实际窗会比上限小、窗数变多，事后拿 `report.stream.windows` 对账。窗小步多则调用省不了几个、接缝还多。其三，`segment.context`（域上下文）：告诉审核员「这是什么流」（本工程的长版 context 枚举了低电量弹窗、通知面板等噪声原型，还声明了任务互斥与切回语义——逐要点解读在第 26 章），它不定义边界，但能收敛噪声与切换判定的口径。

**边界审计：抽读 `segment.boundary`。**每窗一条事件，`relations` 是逐帧判决、`reason` 是逐帧理由（订阅 segment 通道 + `content="refs"` 起携带）。抽读法：挑判决密度高的窗，把 relations 与你的人工预期逐帧对——本次真跑的 s1 窗（真实 trace 行，格式化展示；`…` 处省略 `run_id`/`batch_no`/`member_ids` 与其余帧的同构内容）：

```json
{"ts": "2026-07-23T04:46:35.435+08:00", …, "stage": "segment", "ev": "segment.boundary",
 "payload": {"session_id": "f00e41052479a460", "window": [0, 14],
   "relations": [{"index": 0, "relation": "continues"}, …,
                 {"index": 4, "relation": "interruption"},
                 {"index": 5, "relation": "context_switch"}, …,
                 {"index": 8, "relation": "context_switch"}, …,
                 {"index": 13, "relation": "returns_to_entry"}],
   "model": "glm-5.2",
   "reason": […, "切到社交App查看消息，与正在进行的麻辣烫点餐流程无关，是短暂的任务中断",
              "从社交消息切回外卖App购物车页面，是切回被搁置的外卖任务收尾，属于新流程的开始", …,
              "外卖任务已完成后切到出行App开始叫车，是全新任务的开始", …,
              "叫车任务完成后回到主屏幕，是回到入口准备开启新流程"]}}
```

index 4（帧 5）的 `interruption`、index 8（帧 9）的 `context_switch` 与 index 13（帧 14）的 `returns_to_entry` 正是 25.3 那张词表的活例；最值得端详的是 index 5（帧 6）：审核员按本工程 `context` 的声明把「切回被搁置任务收尾」判成了 `context_switch`（理由原文照抄了口径），于是外卖任务被切成两段——多图窗口下的这个判决与纯文本时代的真跑（同一帧曾判 `continues` 不切段）方向相反，属于边界口径的漂移带；本工程下游开着缝合，两段随即被并回（第 26 章），漂移没有伤到产物。对边界不满意的调参循环：改 `context` / 调 `window` / 动 gap → 同 seed 重跑 → diff 两次的 boundary 事件。

**extract 的可靠性预算：按 70–80%/步做计划。**LLM zero-shot 动作推断的实测可靠性就在这个区间（Watch & Learn 70.5%、Sharingan 70–80% 且按动作类型不均衡）——每步 20–30% 的错误率会沿 episode 级联，**不要把单步 steps 当真值消费**。工具承诺的是缓解链而非单步正确性：`include_diff` 的树 diff 证据（默认开，可关做 A/B——对照读数就是 `extract.by_type` 分布与 verify 缺陷率）、verify 缺陷路由兜底（步骤↔标签不符会被打 `label_mismatch`）、quality 结构分软门（连贯性/噪声残留压分可疑段）。日常盯两个计数：`by_type.other` 占比异常升高或某类型塌缩 = 系统性劣化信号；`fallback_steps` 持续非零 = 摘取输出结构不稳，先查 trace 的 error 事件。

**帧摘要贫瘠与 vision 补偿。**纯文本裁决的第一瓶颈是帧摘要保真度——摘要没抓到的实体，LLM 看不见。摘要贫瘠（可见文本节点为零或摘要长度趋零：画布类屏幕、ghost nodes）会计入 `report.stream.digest_poor_frames` 并打一次 WARN，WARN 文案（`poor frame digest (zero visible text nodes): text-only boundary verdicts lack evidence; attach frame screenshots by pointing segment.llm at a supports_vision=true profile`）给出的补偿动作是 v1.11 的新口径：**为 `segment.llm` 配置 `supports_vision = true` 的 profile**——窗口是否附图由所引 profile 的能力自动推导（选 profile 即选能力），原 `segment.use_vision` 键已随 v1.11 移除，配置里显式写出会直接报配置错误并附迁移指引。本工程的 default profile 支持视觉，多图窗口默认就开着（每帧一图、成本相应上去；想省钱就指向纯文本 profile）；本次真跑贫瘠计数为 0——fixture 的树信息充足，附图属于锦上添花。

**长 episode 的信度注记。**episode 超过 ~20 步后，LLM 对整段的判分信度会衰减（业界同证据）。两个缓解：质量侧改 pairwise（相对比较对长序列比绝对刻度稳）；或对超长段的分数降信任、把裁量交给人工抽检。

**成本账**（形制同第 17 章 §17.1；设会话长 L、窗上限 w、最坏装填量 w_min——启动 INFO 行里那个数）：

| 来源 | 次数 | 本次真跑 |
|---|---|---|
| segment | Σ ceil((L−1)/(w_eff−1))，w_eff = min(w, w_min)——预算装填下报**上界**（实际每窗装得更满就更少）；L≥2 的会话；rules/孤帧计 0 | 估 5；实际 `stream.windows=5`（w_min=46 ≥ w=16，装填顶格、上界收紧为准确值） |
| extract | Σ(L−1) 报**上界**（剔噪后实际 = Σ(成员数−1)，接缝占位不计） | 估 48、实际 32 |
| quality / annotate / verify | 记录基数变为 episodes/线索（估算以会话数报**下界**） | 9 线索 ×4 准则 = 36；8；8（1 条线索死于打分，没走到后两位） |

`--dry-run` 的估算行无条件打印 `segment_calls` / `extract_calls` / `stitch_calls`（v1.9 增，未启用恒 0），本工程估算 `total=98`。实跑合计 130 次调用（default 122 + judge 8）、约 248 秒——高出估算的部分 = 下游按会话数报下界的口径差 + 缝合判定的实际次数（第 26 章）+ 12 次结构修复环调用（trace 里 41 条 `schema.repair` 事件：29 条在确定性修复层零调用解决、12 条走了 LLM 修复环），估算历来不含修复。`per_stage_s` 里 stitch（89.4 秒，账在第 26 章）与 quality（59.8 秒）是两个大头——第 17 章「quality 是大头」的结论在 stream 下依然成立，extract（41.3 秒）第三，episode 越长它占比越高。

**`--strict` × 噪声帧。**stream 工程的噪声帧是**预期产物**——但它们进 rejects，`--strict` 会因 rejects 非空退出 1。CI 里给 stream 工程挂 strict 前想清楚：要么接受「有噪声帧就红」，要么改为解析 report（比如只在 `failed > 0` 或 `verify.defects` 非零时报警）。

## 25.6 帧级分类与标注（v1.12）

**双粒度动机。**到这里为止，stream 的产物粒度是「一行 = 一段活动」：分类、打分、标注全在回答「这一段是什么」。但下游经常还要第二层**原子粒度**——段内每个成员帧各自扮演什么角色（发起任务、追问修改、寒暄插入），各自承载哪些结构化要素（意图、实体、参数）。v1.12 把这层需求做成流模式内 **opt-in 的帧粒度**：`[frame.classify]` 让 classify 处理每个 episode 时顺带对成员帧做**一次批量闭集判决**，`[frame.annotate]` 让 annotate 在序列级标注之后**逐成员**按帧类做结构化标注——一份配置、一次流水线，序列级意图与帧级原子标注同时拿到；帧产物挂在 episode 行内的 `_meta.stream.members[]` 随序列交付，成员帧状态机、链序与守恒恒等式零改动（帧粒度全关时行为与 v1.11 一致，唯 dry-run 估算行无条件多两个估算键，第 15 章）。仓库自带的 `examples/mix` 就是双粒度同开的上手工程，主工程 `project.toml` 是 UI 控件树时间序流：截图 + 控件树 17 帧对、三个会话子目录（s1 外卖下单、s2 订酒店、s3 = s1 的逐字节复刻——episode 级判重埋点），fixture 由 `tools/gen_fixtures.py` 用 PIL 确定性生成。它的 `config.toml` 独立成套且是**双端点**：`[llm.default]`（DeepSeek）承担文本判决面——segment 滑窗判决、帧级批量分类、轨迹打分；`[llm.vision]`（z.ai glm-5.2）承担视觉必需面——序列分类/序列标注/帧级标注/评审四阶段在 UI 模态强制 `supports_vision`（25.7 的逐阶段 vision 分列，在这里落成了「按阶段挑端点」的教学形态）。运行 `cd examples/mix && mkdir -p out && uv run labelkit run --config config.toml --project project.toml`（注意 config 在本目录，非 `../config.toml`）；同目录另有文本姊妹工程 `project-text.toml`——单端点纯 DeepSeek 的最低成本形态与文本帧路径演示，输出 `mix-text-labels.jsonl`（本节后文有其真跑节选）。以下配置与产物均摘自主工程的真实运行。

**配置三节**（摘自 `examples/mix/project.toml`，UI 主工程——帧类表是**屏幕类型**词表）：

```toml
[frame.classify]                  # 帧级闭集分类（默认关；仅流模式）
enabled = true
llm = "default"                   # digest-only 帧级批量判决：永不入 vision 必需集——走 DeepSeek
fallback_class = "other"          # 修复穷尽/窗口失败的兜底，须 ∈ 帧类表

[[frame.classify.classes]]        # 帧类表：与 [[classify.classes]] 同构，但两张表互相独立（第 24 章）
name = "form_screen"
description = "表单类屏幕：规格选择、日期人数填写、地址备注输入等以字段填写为主的页面"
# list_screen / detail_screen / confirm_screen / transition / other 五类同构，略

[frame.annotate]                  # 帧级标注（默认关；仅流模式）
enabled = true
llm = "vision"                    # UI 模态 frame.annotate 无条件入 vision 必需集——走 z.ai
instruction = """
你是移动端屏幕帧标注员。根据单帧截图与 UI 控件树，标注该屏幕在流程中的
角色（screen_role，一个名词短语）与关键控件列表（key_widgets，字符串数组：
把承载本屏核心信息或核心操作的控件文本逐项列出；没有则给空数组）。
"""
schema_inline = """…"""           # 独立的帧级输出 Schema：{screen_role, key_widgets} 两字段（第 14 章）
# examples = [...]                # 可选 few-shot，形态镜像 annotate.examples

# ── 按帧类覆盖：表单类屏幕单独强调抽取表单字段与取值 ──
[frame.class.form_screen.annotate]
instruction = """
你是移动端屏幕帧标注员。这一帧已被判定为表单类屏幕：标注其在流程中的
角色（screen_role），并把表单字段与当前取值成对抽入关键控件列表
（key_widgets，如「份量：大份」「辣度：微辣」——逐字段一项，空输入框记
其占位提示）。
"""

# ── 按帧类覆盖：过渡屏跳过标注（省成本示范；members[] 呈现 skipped）──
[frame.class.transition.annotate]
enabled = false
```

**组合约束**（全部启动期配置错误，第 4 章有合订）：帧粒度是流模式的第二层产物——`frame.classify` / `frame.annotate` 任一启用都要求 `segment.enabled = true`（非流工程想按类定制标注，用第 24 章的 `[class.<名>.annotate]`）；帧产物仅经 `_meta` 承载，`output.meta_mode` 不得为 `"none"`；`fallback_class` 必须 ∈ 帧类表；`[frame.class.<名>]` 在场要求帧分类开启、节名必须是帧类表成员，且白名单只有 annotate 一节的 instruction / examples / enabled 三键；帧级**没有**多标签也**没有**自洽采样——在 `[frame.classify]` 里写 `assignment` 或在 `[frame.annotate]` 里写 `self_consistency` 会得到定向配置错误（机制同 v1.11 移除 `segment.use_vision` 时的原始节探针）。v1.13 再加一条**互斥**：帧粒度两开关与时间流生成（`[generate.stream]`，第 27 章）不能同开——合成流的帧类是蓝图定下来的**真值**，再花钱判一遍没有意义，显式开启同样是定向配置错误。那边复用的只是帧类**表**（`[[frame.classify.classes]]` 保持 `enabled = false` 当真值词表用），帧内容契约写在另一族节 `[frame.class.<帧类名>.generate]` 里。

**members 块怎么读。**本次真跑主输出第 1 行（外卖下单的 episode，序列类 food_delivery，6 成员）的 `_meta.stream.members` 全文：

```json
"members": [
  {"index": 0, "id": "7cfb0c25f855b2d7", "label": "list_screen",
   "annotation": {"screen_role": "美食外卖首页",
                  "key_widgets": ["搜索美食", "搜索", "推荐餐厅", "金牌黄焖鸡 4.9 分",
                                  "老面坊牛肉面 4.7 分", "青禾轻食沙拉 4.5 分"]},
   "status": "annotated"},
  {"index": 1, "id": "164b7480ab098de5", "label": "detail_screen",
   "annotation": {"screen_role": "菜品详情页",
                  "key_widgets": ["金牌黄焖鸡", "黄焖鸡米饭 ¥38", "月售 1200+ 好评率 99%",
                                  "招牌黄焖鸡块 配米饭一份", "选规格"]},
   "status": "annotated"},
  {"index": 2, "id": "25ce67ce53d5f1d7", "label": "form_screen",
   "annotation": {"screen_role": "商品规格选择/加入购物车",
                  "key_widgets": ["商品：黄焖鸡米饭 ¥38", "份量：大份", "辣度：微辣",
                                  "米饭：×1", "口味备注（选填）：（空）"]},
   "status": "annotated"},
  {"index": 3, "id": "d77a51064a52f91e", "label": "confirm_screen",
   "annotation": {"screen_role": "订单确认页",
                  "key_widgets": ["确认订单", "金牌黄焖鸡", "黄焖鸡米饭 大份 ×1",
                                  "收货地址：南京市玄武区中山路 18 号", "预计送达 12:40",
                                  "提交订单 ¥38"]},
   "status": "annotated"},
  {"index": 4, "id": "96cb96ed666583b1", "label": "transition", "annotation": null, "status": "skipped"},
  {"index": 5, "id": "347864af1bc54006", "label": "confirm_screen",
   "annotation": {"screen_role": "支付成功结果页",
                  "key_widgets": ["支付成功", "订单号 FD20260812001", "黄焖鸡米饭 大份 ×1 实付 ¥38",
                                  "预计 40 分钟内送达", "查看订单", "返回首页"]},
   "status": "annotated"}
]
```

`index` 0 基、按成员序（与 `member_ids` 对位）；`label` 键仅帧分类开启时在场（segment 降格的 episode 跳过帧粒度两个 pass：全员 label=null、status="skipped"）；`annotation` / `status` 两键仅帧标注开启时在场，`status` 闭集三值——`annotated`（标注在场且过了写前帧 Schema 校验）、`skipped`（该帧类 `enabled = false`，本例 index 4 的支付处理过渡屏：帧类 transition、跳过标注）、`failed`（修复穷尽或写前校验不过，annotation 置 null）。index 2 的规格表单屏吃的是 `form_screen` 的按类覆盖指令——`key_widgets` 按覆盖要求抽成了「份量：大份」「辣度：微辣」这样的字段-取值对。第 2 行（订酒店的 episode）则是四成员全 annotated（form → list → detail → confirm）——s2 那块系统通知插入屏早在 segment 就被剔成噪声（`dropped_noise`），压根没进成员集，自然也没有它的 members 条目。**帧失败不入 rejects、不触发 `--strict`**：成员失败不是信封失败，episode 照常发射，账记在 `report.stream.frame_annotate.failed`（第 8、18 章）；帧分类侧的失败语义同样保守——单窗修复穷尽时全窗成员落 `fallback_class` 并计 `fallback` / `window_failures`，永不使 episode 失败。

文本帧路径长什么样，看姊妹工程 `project-text.toml` 的真跑输出 `out/mix-text-labels.jsonl`（帧类 task_request/followup/chitchat/other + `{intent, entities}` 帧 Schema，全链纯 DeepSeek）——姊妹工程形态（本次真跑，撰写餐厅评价的 episode，节选）：

```json
{"index": 0, "id": "e665eea66d9f0688", "label": "task_request",
 "annotation": {"intent": "撰写餐厅评价", "entities": []}, "status": "annotated"},
{"index": 1, "id": "41984a72fe624e9b", "label": "chitchat", "annotation": null, "status": "skipped"},
{"index": 2, "id": "f9268bd976ca4a4c", "label": "followup",
 "annotation": {"intent": "添加评价内容", "entities": ["蟹粉狮子头"]}, "status": "annotated"}
```

同一套 members 语法，换了词表与 Schema——跳过类在这边是 chitchat（index 1 的天气寒暄行），按类覆盖挂在 task_request 上（抽订单/行程要素）。

**成本账两句。**帧分类住 dedup **之后**、每 episode 一次批量调用：本次真跑 3 个 episode 判重掉 1 个后只付 2 次（`frame_classify.calls=2`——s3 复刻会话一分帧分类钱都没付）；帧标注住 quality 质量门**之后**、逐成员一次调用：被淘汰的记录永不付帧标注费，按类跳过再省（本次真跑 `annotated=9`、`skipped=1`——那个 skipped 就是外卖 episode 的 transition 过渡屏；dry-run 估算行报的上界是预扫描帧总数——本工程 `frame_classify_calls=17` / `frame_annotate_calls=17`，实付 2 + 9，第 15、17 章）。审计走 trace 的 `classify.frame` / `annotate.frame` 两事件（第 16 章）；verify 手术改写成员集时帧产物随行增删（第 13 章）。

**双端点成本拆分。**报告的 `llm_usage` 按 profile 分账，本次真跑恰好对半：`default`（DeepSeek）15 次调用——文本判决面（segment 3 窗滑窗判决 + 帧级批量分类 2 + 两条存活 episode 的轨迹四准则打分 8 + 打分输出的 2 次 LLM 修复环调用）；`vision`（z.ai glm-5.2）15 次调用——视觉必需面（序列分类 2 + 序列标注 2 + 逐成员帧标注 9 + 评审 2）。哪个阶段该走哪个端点、为什么这么分，`examples/mix/config.toml` 的文件头注写着完整分工表——想把帧标注也省下来的读法只有换姊妹工程（帧标注在 UI 模态无条件入视觉必需集，25.7）；帧级批量分类反过来**永不**要求 vision（digest-only 判决），把 `frame.classify.llm` 指向纯文本 profile 就是它的省钱面。

## 25.7 常见问题

**任务被打断、切成了两段怎么办？**这是分段的正确行为，不是 bug——分段的单元是「连续做一件事的段」，用户中途切去回消息，外卖任务在时间轴上就是两个碎片。想把它们按任务线索缝回一条完整记录（接缝处机械占位一步），开 v1.9 的缝合算子——`[stitch]`，配置、机制与验收全在第 26 章（本工程就开着它，s2–s4 三个会话是缝合的正戏）。要一个纯 v1.8 基线做对照时，把 `[stitch]` 关掉即可：关缝合时主输出/rejects/report 与 v1.8 逐字节一致（唯缺陷词表恒多一行 `wrong_stitch: 0`）——同目录的 `project-text.toml` 就是一个不开缝合的现成工程。

**孤帧会话去哪了？**不会静默消失。`len(session) == 1` 的会话走 rules 退化：原样成一个单帧 episode（零 LLM 调用），**不经 min_len**——min_len 只砍「LLM 精化切出的短段」。所以帧 14 那条 `below_min_len` 的完整因果是：它在 14 帧大会话里被判 `returns_to_entry`（回到桌面开启新流程）、开了一个只有自己的新段，段长 1 < 2 才被丢（本工程开着缝合，它随后还进了救援候选池、被判 `new` 维持原判——救援候选永不开新线索，第 26 章）——假如它自成一个会话（比如配了 `gap_steps` 且序号断开），反而会原样活成 episode。

**为什么 quality 不看图？**三重原因：trajectory rubric 的四条准则全是**结构性**判据（推进到终态了吗、步步承接吗、朝单一目标吗、混了无关步骤吗），动作序列 + 帧摘要足以裁决；序列打分若逐帧附图，一个 20 帧 episode × 4 准则就是 80 张图的开销；且多图请求有硬上限（见下条）。这是 vision 能力要求的显式放宽——stream（UI 模态）各阶段里 extract 恒要求 vision，annotate/verify/classify 启用时同样要看图；segment **不入视觉必需集**（v1.11）：窗口是否附图由 `segment.llm` 所指 profile 的 `supports_vision` 自动推导——支持就逐帧附图（本工程即多图窗口），不支持就纯文本摘要，原 `use_vision` 独立开关已移除；v1.12 的帧粒度双开关同款分列——`frame.annotate.llm` 在 UI 模态**恒**要求 vision（单帧截图是帧标注的主证据），`frame.classify.llm` **恒不**入视觉必需集（digest-only 批量判决，指向纯文本 profile 即省钱面）——`examples/mix` 主工程按这条分界把两个开关分别指向 z.ai 与 DeepSeek 两个端点（25.6）；quality 与 v1.9 的 stitch 判定则**恒**是纯文本（后者的证据是摘要卡，第 26 章）。

**多图上限与「帧数 × 像素」的联动是怎么回事？**Anthropic 端点对「单请求 >20 张图且任一图 >2000px」直接 400 硬拒（不是自动缩放）。两处会撞上它的配置都有 M1 启动 WARN：序列标注一请求带 ≤ `sequence_frames` 张关键帧图（默认 20，恰在界内），调到 >20 且所引 profile 的 `max_image_px > 2000` 即 WARN；v1.11 起 segment 的多图窗口有同款姊妹校验——`segment.window > 20` 且窗口附图（vision_resolved）且 `max_image_px > 2000` 同样 WARN（本工程 window=16，界内）。出路都一样：像素上限降到 2000，或把帧数/窗上限降回来；v1.11 还把「日常像素工作点」独立成键 `default_image_px`（`max_image_px` 升格为升级天花板与 provider 硬限域，第 6 章）——多图请求按工作点编码，预算与硬限都好算。降采样本身是纯整数公式（首末帧恒保留、均匀取样、零随机），成员数 ≤ sequence_frames 时全量带图。openai_compatible 一侧工具**不设独立上限**：官方口径宽松得多（1500 图/请求、512MB 载荷），但真实约束面在网关——Azure 文档写 10 图、GPT-4o 实测 20 图硬顶，vLLM/SGLang 的多模态上限随部署配置变化——静态校验必然虚警或漏警，建议对自己的端点用 `labelkit validate --probe` 加小样本试跑（`--limit`）实测确认。

**什么是 hard-split（会话硬切）？**单个会话装不进一个批（会话长 > `run.batch_size`）时，M10 按批容量硬切会话并 WARN 一次，切出的帧带 `session_split` 标记（落 `_meta.stream.session_split`）——它是 verify 判「缺帧」时的降级依据（缺的帧可能在隔壁批，不是采集断档）。M1 在 `stream.session_max_len > run.batch_size` 时会提前警告这个组合。正确姿势：让 `batch_size ≥ session_max_len`，从源头避免硬切。

最后一份检查清单，开 stream 前过一遍：输入按时间序排好且（配了分区键时）按键成组；`batch_size ≥ session_max_len`；trace.channels 加了 `"segment"`（边界审计全靠它，调优期必开）；quality 不设 threshold、留给后筛；CI 的 `--strict` 策略想好了噪声帧怎么算；下游知道一行 = 一个 episode、成员溯源在 `_meta.stream.member_sources` 了吗？
