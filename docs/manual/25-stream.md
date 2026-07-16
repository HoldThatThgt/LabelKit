# 第 25 章　时序流 stream：会话化、语义分段与动作摘取

> stream 模式是 v1.8 新增的一组能力：把**按时间顺序采集的屏幕状态流**（录屏抽帧 + UI 树）
> 先切成一段段「用户在做一件事」的 episode，再逐帧对推断出中间发生的动作，
> 最后以**序列**为单位完成打分、标注与评审。
> 读完本章你应当能回答三个问题：**什么样的数据该开 stream？边界与噪声是怎么判出来的？
> 序列产物的账怎么对？**本章样例全部来自 `examples/stream` 的真实运行。

## 25.1 为什么要分段：时间轴上没有「一条记录」

前面所有章节都默认一件事：输入里的**每一行/每一对就是一条独立记录**，标注单位与采集单位天然重合。但屏幕操作流不是这样采的——录屏抽帧得到的是「首页、搜索页、结果页、详情页、弹窗、购物车……」一长串状态截面，**单帧什么都说明不了**：训练侧要的样本是「用户搜索并下单了一次外卖」这样的完整任务段，而任务的边界、中间混入的通知弹窗、乃至「两帧之间用户到底做了什么」，在原始数据里根本没有字段承载。拿 v1.7 的流水线硬跑这种数据，得到的是逐帧的碎片标注：帧级去重在连续 UI 帧上大面积误伤，质量分打在单帧上毫无意义。

stream 模式把「原始帧流 → 训练样本」拆成一条新的加工链，四层各管一段：

1. **会话化**（`[stream]`，M2 规则层）：按声明的顺序与断开规则，把帧流粗切成候选会话——纯代码、零 LLM；
2. **语义分段**（`[segment]`，M14 算子）：LLM 滑窗逐帧裁决「这一帧相对进行中的活动是什么角色」，代码按固定规则从关系**演绎**出边界与噪声帧，每段拼装成一个 episode（序列记录）；
3. **动作摘取**（`[extract]`，M15 算子）：对 episode 内每对相邻帧，LLM 推断「两帧之间发生的单个语义动作」，写成结构化步骤序列；
4. **下游序列适配**：去重、打分、标注、评审全部改以 episode 为单位——轨迹 rubric 打结构分、标注看动作序列 + 关键帧、评审带缺陷表并能对成员集做「手术」。

一条与真实数据打交道时躲不开的指引：用户常在任务间来回切换——外卖点到一半切去回消息、回来接着下单，这种**穿插**会让分段把同一个任务正确地切成多个碎片段（分段的单元本来就是「连续做一件事」）。本章通篇讲的是不缝合的基线形态；要把穿插碎片按任务线索缝回完整记录，开 v1.9 的缝合算子（`[stitch]`，第 26 章）。

这套形态不是发明：从状态对反推动作是 OpenAI VPT 的逆动力学模型与 OS-Genesis 逆向任务合成的既有工序，滑窗 LLM 边界裁决是 2026 年 GUI 轨迹量产管线（Video2GUI 等）仍在用的形态之一，LabelKit 按自己的负边界（不训练本地模型）用运行时 LLM 充当这两个角色。**什么时候开**：输入是按时间排好的操作流（UI 模态的截图 + 树对，或带时间戳的文本事件流）、且你要的样本单位是「活动段」而非单条记录。开关是 `segment.enabled = true`，约束：仅 process 模式、必须开 annotate、与 generate 互斥；`extract` 再要求 UI 模态。默认全关——不开时行为与 v1.7 逐字节一致（输出只多一个恒为 null 的 `_meta.stream` 键）。

## 25.2 快速上手：examples/stream 全流程

仓库自带的 `examples/stream` 是一个 14 帧的 UI 操作流工程：任务 A「点外卖」帧 1–8（其中帧 5 是突然插入的社交 App 消息屏——预期噪声）、任务 B「打车」帧 9–13 背靠背、帧 14 回到桌面。fixture 由 `tools/gen_fixtures.py` 一次性确定性生成（树是唯一语义源，截图为 PIL 程序化绘制），刻意埋了「实体跨屏延续」的线索：餐厅名「川味麻辣烫」跨帧 3/4/6 出现、金额 ¥32 跨帧 4/6/7。逐节看 `project.toml`。

**第一节：会话化与分段。**

```toml
[stream]
order_by = "input_order"          # UI 模态 = pair_index 升序（meta:* 仅文本模态）
# gap_steps = 3                   # 变体示范：pair_index 序号差 > 3 即断开会话（0 = 不启用）

[segment]
enabled = true                    # stream 模式总开关
strategy = "hybrid"               # 滑窗 LLM 边界精化 + 逐帧噪声标记
window = 8                        # 14 帧会话 → 2 窗（重叠 1 帧）
min_len = 2                       # 仅作用于 LLM 精化切出的段
context = "手机屏幕操作录屏流；通知面板、弹窗等与前后操作无关的短暂插入屏属于干扰帧"
```

`[stream]` 声明「帧流怎么排、会话在哪断」：本例没有配分区键与 gap，整个目录就是**一个候选会话**。`[segment]` 是 stream 模式总开关；`window = 8` 让 14 帧会话切成 2 个滑窗（步长 = window−1 = 7，重叠 1 帧）；`context` 只是可选域上下文——**边界判据内置于固定模板，零配置可用**，这行不是必需品。

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

注意两件事：rubric 用的是 v1.8 新内置的 `default:trajectory`（完成度/连贯性/目的性/噪声残留四准则，附录 B）——事实上 stream 模式下 rubric 留空也会解析到它；**没配 threshold**——序列样本贵，先把分打出来、下游按分后筛（第 8 章的「门控留宽」策略在这里几乎是标配）。

**第三节：序列标注与带手术的评审。**

```toml
[annotate]
enabled = true
llm = "default"
instruction = """
你是移动端操作序列标注员。根据动作序列与关键帧，
标注该操作序列的任务标签（用户在做什么）、所属应用与一句话摘要。
"""

[verify]
enabled = true
llm = "judge"
policy = "repair"                 # 缺陷表路由：成员手术 + 重摘取 + 重标注
max_repair_rounds = 1

[trace]
enabled = true
channels = ["segment", "extract", "verify", "schema"]
content = "refs"

[output]
meta_mode = "inline"
rejects = "full"                  # 噪声帧 rejects 行携带完整载荷（序列 = 成员清单）
# schema_inline = …               # task_label / app / summary 三字段的输出 Schema，略
```

trace 通道枚举 v1.8 从 8 值扩到 10 值：`"segment"` 与 `"extract"` 都**不在默认订阅集**里，想审计边界判决必须显式加（与第 24 章的 `"classify"` 同款约定）。跑起来：

```bash
cd examples/stream && mkdir -p out
set -a && source ../../.env && set +a
uv run labelkit run --config ../config.toml --project project.toml
```

stderr 尾部的终版摘要（真实运行，退出码 0，全程约 73 秒）：

```
   ── 终版摘要（与 report.counts 逐项一致）──
   scanned=14  ingested=14  bad_input=0  generated=0
   dropped_dup=0  dropped_lowq=0  dropped_verify=0  failed=0  emitted=2
```

14 帧进来，主输出却只有 **2 行**——这不是丢了数据，是换了记账单位：12 帧被吸收进 2 个 episode（状态 `absorbed`），2 帧成了噪声（`dropped_noise`），守恒等式的完整验算见 25.4。两个 episode 与人工预期严丝合缝：任务 A 成一段（7 成员，噪声帧 5 被剔）、任务 B 成一段（5 成员）、帧 14 落进 rejects。

## 25.3 机制四层：从帧流到步骤序列

**第一层：会话化规则层（`[stream]`，纯代码）。**`order_by` 声明顺序来源——`"input_order"`（默认：文本 = 文件名字典序→行号，UI = pair_index 升序）或 `"meta:<字段>"`（仅文本模态，按时间戳字段排序校验；epoch 秒/毫秒与 ISO 字符串怎么解析、输入怎么排布见第 5 章）。会话断开由四组规则任一触发：分区键变化（`key`，如 UI 模态的 `"source_dir"`——一次采集一目录）、时间间隙（`gap_s`）或序号间隙（`gap_steps`）、硬上限（`session_max_len` / `session_max_span_s`）。工具**不做全量重排**，只做流式单调性校验：乱序/时间戳解析失败的记录按 `on_disorder = "skip"`（默认，计 bad_input）或 `"fail"`（退出码 3）处置。每个会话闭合发一条 `segment.session` 事件——本次真跑只有一条：14 帧、cause=`eof`。

**第二层：segment 的三步演绎滑窗。**`strategy = "hybrid"`（默认）时每窗一次 LLM 调用，但 LLM **不直接回答「这里是不是边界」**——模板固定为三步作业：先通读全窗做双向上下文概括，再对每帧做**封闭词表**的关系分类（M8 enum 硬校验，词表外输出在结构层就被拦下），边界与噪声由代码查表演绎。五个关系值的通俗读法：

| relation | 通俗含义 | 演绎结果（代码查表） |
|---|---|---|
| `continues` | 同一流程的正常推进 | 非边界 |
| `advances` | 屏幕甚至 App 变了，但任务实体（订单号、餐厅名、验证码）跨屏延续——跨 App 的同一任务属此值 | 非边界 |
| `returns_to_entry` | 回到入口/搜索/桌面后开启新流程（同 App 背靠背任务的断点） | **边界**：该帧是新段第一帧 |
| `context_switch` | 交互对象与环境不连续且无实体延续——「相关但无实体延续的新流程」也取此值 | **边界**：该帧是新段第一帧 |
| `interruption` | 与前后活动均无关的短暂插入：通知、弹窗、误触 | noise（剔除出段） |

`advances` 与 `context_switch` 的分界钉死为**实体延续**——这正是 fixture 埋「川味麻辣烫」跨屏线索的原因：帧 5 弹进社交 App 被判 `interruption`，帧 6 回到购物车被判 `advances`（实体延续、不是新段），帧 9 切进打车 App 被判 `context_switch`（无实体延续、开新段）。三条硬规则：**会话首帧恒为段首**（rel[0] 的边界值不参与判决，noise[0] 照常生效）；接缝帧（前窗末帧 = 后窗首帧）的判决**整帧归后窗**；`min_len`（默认 2）**只作用于 LLM 精化切出的段**——短段帧以 `below_min_len` 的 reason 进 rejects，**≠ `noise`**：它未经噪声判据裁决，不得污染噪声审计口径，计数也独立（`report.stream.below_min_len`）。规则层的孤帧/短会话（含 `strategy="rules"`）不经 min_len、原样成 episode。单窗结构修复耗尽按 `segment.on_error = "keep"`（默认）降级：该会话整体成一个 episode 并在 `_meta.stream.degraded` 留痕，记录存活。

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

**第四层：下游算子的序列适配。**episode 是 `kind="sequence"` 的记录（成员帧转入 `absorbed` 状态、不再独立产出——这是 Stage 契约的新受控例外 ②b，第 4 章），下游全部换序列口径。v1.9 起 segment 与下游之间还有一个可选工位：缝合算子（`[stitch]`，第 26 章）把同会话内被穿插切开的 episode 碎片并成线索（契约例外 ②c），开启后下面各算子看到的单元相应从 episode 升级为线索：

- **dedup**（第 9 章）：序列的判重文本 = 成员配方按序拼接，episode 级重复 = 「同样的操作流程」；pHash 层自动跳过（序列记录无自己的图）；
- **quality**（第 10 章）：证据 = `[步骤序列]`（extract 产物的文字渲染，fallback 步与确证 other 分列）+ `[成员帧摘要]`，**全程无图**——trajectory rubric 的四条准则（完成度/连贯性/目的性/噪声残留）全是结构性判据，不需要逐帧看图（25.6 有展开）。extract 关了也能打：「步骤」退化读作「帧间变化」（M1 会给 warning 提示这个组合）；
- **annotate**（第 11 章）：序列模板 = `[动作序列]` 逐步行渲染 + 关键帧图（成员帧均匀降采样到 `annotate.sequence_frames`，默认 20，首末帧恒保留）+ `[成员帧摘要]` 收尾；
- **verify**（第 13 章）：评审输出在意见/结论之外多一张**缺陷表**（六值：`label_mismatch` 标签不符 / `off_task_members` 混入无关帧 / `missing_head` / `missing_tail` 切头切尾 / `missing_members` 段中缺帧 / `wrong_stitch` 缝合错误——v1.9 增，词表闭集恒在场、仅开缝合时可判），证据段含 `[边界余量]`——段边界外前后各 2 帧的摘要及去向，专防切头切尾。`policy = "repair"` 时按缺陷路由**成员手术**：收缩（把无关帧逐出段，reason=`off_task_member`）与回收（把批内同会话的噪声帧复裁后接回），手术后接缝重摘取、transitions 重编号、重标注复审，全程两阶段批级结构保证并发下确定性；修复过的行带 `_meta.stream.repaired = true`，不重打分。

## 25.4 输出怎么读

**主输出**一行 = 一个 episode。真实运行产物第 1 行（格式化展示；`steps` 的六步全文照录）：

```json
{
  "task_label": "搜索并下单麻辣烫外卖",
  "app": "com.example.food",
  "summary": "在美食外卖App中搜索麻辣烫，选择川味麻辣烫餐厅，将招牌麻辣烫加入购物车并完成下单。",
  "_meta": {
    "id": "71ade2027644e5e2",
    "run": {…},                              ← 与既有形态一致，从略
    "source": {"file": "uitree_1.jsonl", "pair_index": 1, "generated_from": [],
                "fields": {}, "generator": null},        ← 继承首成员的溯源
    "stream": {
      "episode_id": "71ade2027644e5e2",      ← 恒等于本行 id（episode 自述）
      "session_id": "f00e41052479a460",      ← 所属会话（同会话的段共享此值）
      "order_span": [1, 8],                  ← 首末成员的序键（本例 = pair_index）
      "member_count": 7,
      "member_ids": ["873a403914352fd1", "98a8e0836890fa51", "16ceb575dc626695",
                      "117fda9c33c823fc", "89fccaa682b52227", "e1b72b64b4a7164c",
                      "d565f6f279ebec42"],   ← 成员帧 id，序键升序
      "member_sources": [{"file": "uitree_1.jsonl", "pair_index": 1},
                          {"file": "uitree_2.jsonl", "pair_index": 2},
                          {"file": "uitree_3.jsonl", "pair_index": 3},
                          {"file": "uitree_4.jsonl", "pair_index": 4},
                          {"file": "uitree_6.jsonl", "pair_index": 6},   ← 注意 5 缺席：噪声帧
                          {"file": "uitree_7.jsonl", "pair_index": 7},
                          {"file": "uitree_8.jsonl", "pair_index": 8}],
      "session_split": false,                ← 所属会话曾被 batch_size 硬切过吗（25.6）
      "repaired": false,                     ← verify 手术改写过成员集吗
      "degraded": null,                      ← segment 失败降级留痕（on_error="keep" 时）
      "steps": [                             ← extract 产物；关 extract 时恒 null
        {"index": 0, "action_type": "click", "target": "搜索美食", "value": null,
         "description": "点击首页顶部的搜索美食入口，进入搜索页面"},
        {"index": 1, "action_type": "click", "target": "麻辣烫", "value": null,
         "description": "在热门搜索中点击\"麻辣烫\"标签，进入麻辣烫搜索结果页面"},
        {"index": 2, "action_type": "click", "target": "川味麻辣烫", "value": null,
         "description": "在搜索结果列表中点击\"川味麻辣烫\"餐厅，进入该餐厅的详情页面"},
        {"index": 3, "action_type": "click", "target": "加入购物车", "value": null,
         "description": "点击\"加入购物车\"按钮，将招牌麻辣烫加入购物车，页面跳转到购物车界面"},
        {"index": 4, "action_type": "click", "target": "去结算", "value": null,
         "description": "用户点击购物车页面的\"去结算\"按钮，进入确认订单页面"},
        {"index": 5, "action_type": "click", "target": "提交订单 ¥32", "value": null,
         "description": "点击\"提交订单\"按钮，提交订单后页面跳转至下单成功页面"}]
    },
    "scores": {"noise_residue": 1.0, "coherence": 1.0, "completion": 1.0,
                "purposefulness": 0.0, "__aggregate__": 0.75,
                "mode": "pointwise", "batch_no": 1},
    "dedup": {"kind": "unique"},
    "classification": null,
    "annotation": {"model": "glm-5.2", "attempts": 1},
    "verification": {"verdict": "pass", "rounds": 1, "defects": []}   ← stream 行恒带 defects 键
  }
}
```

逐键读 `_meta.stream`：`member_sources` 是完整成员溯源（每帧来自哪个文件哪个 index——`source` 键只继承首成员），拿它能把 episode 还原回原始帧；`order_span` 与 `member_count` 对不上（跨度 8、成员 7）就说明段内有帧被剔了。顶层三个字段仍是你的 Schema 产物——**输出结构照旧由全局 Schema 管**，stream 改变的只是「一行代表什么」。另两处细节：`verification` 在 stream 模式恒带 `defects` 键（无缺陷 = 空数组）；这行的 `purposefulness` 被打了 0 分（聚合分 0.75）——对一条目的明确的下单流来说这是个可疑判决，但因为没设 threshold，它只是个随行落盘的分数。**stream 工程默认只打分不筛**的价值就在这：判分的噪声不会变成数据的损失，后筛时你还有机会用 trace 复核。

**拒绝通道**是噪声帧的去向（`rejects = "full"` 档；两行的 `_meta` 逐字如下，`record` 载荷——该帧的树文本与图路径——以 `{…}` 略去）：

```json
{"_meta": {"id": "c51c341656eb8447", "source": {"file": "uitree_5.jsonl", "pair_index": 5, "generated_from": []}, "stage": "segment", "reason": "noise", "errors": []}, "record": {…}}
{"_meta": {"id": "47d1c7373d1fa7fb", "source": {"file": "uitree_14.jsonl", "pair_index": 14, "generated_from": []}, "stage": "segment", "reason": "below_min_len", "errors": []}, "record": {…}}
```

两个 reason 别混：帧 5 是 LLM 判的 `interruption`（reason=`noise`，社交 App 消息屏）；帧 14 是「`returns_to_entry` 开了新段、但段里只有它自己（1 < min_len=2）」的 `below_min_len`——桌面屏不是噪声，只是不够成段。审计噪声率时只数 `noise`，别把 `below_min_len` 算进去。verify 手术收缩逐出的帧则是第三种组合：`stage="verify", reason="off_task_member"`。

**报告**多了两块。`counts` 增三键（真实产物）：

```json
"counts": {
  "scanned": 14, "ingested": 14, "bad_input": 0,
  "dropped_dup": 0, "dropped_lowq": 0, "dropped_verify": 0,
  "failed": 0, "generated": 0, "emitted": 2,
  "episodes": 2, "absorbed": 12, "dropped_noise": 2
}
```

v1.8 的守恒等式全展开形（第 4 章原式的超集，未启用项恒 0 时退化回原式）：

```
emitted + dropped_dup + dropped_lowq + dropped_verify + dropped_noise + failed + bad_input + absorbed
  = scanned + generated + fanout + episodes
```

代入验算：左 = 2 + 0 + 0 + 0 + **2** + 0 + 0 + **12** = 16；右 = 14 + 0 + 0 + **2** = 16。✓ 直觉读法：右侧 `+ episodes` 是因为每个 episode 都是凭空追加的新信封（与 classify 扇出的 `fanout` 同构），左侧 `absorbed + dropped_noise` 则是原始帧的两种新去向。新增的 `stream` 节（真实产物，`by_type` 其余 10 个动作类型本次全为 0、以 `…` 略）：

```json
"stream": {
  "sessions": 1, "episodes": 2, "mean_episode_len": 6.0,
  "absorbed": 12, "dropped_noise": 2, "below_min_len": 1,
  "digest_poor_frames": 0, "segment_failures": 0,
  "extract": {"transitions": 10, "fallback_steps": 0, "failures": 0,
               "by_type": {"click": 10, …}},
  "verify": {"membership_repairs": 0, "boundary_flags": 0,
              "defects": {"label_mismatch": 0, "off_task_members": 0,
                           "missing_head": 0, "missing_tail": 0, "missing_members": 0,
                           "wrong_stitch": 0}}
}
```

对账三连：`transitions=10` = (7−1) + (5−1)（转移数恒 = 成员数 − 1）；`dropped_noise=2` 里有 1 条是 `below_min_len`（独立计数拆给你看）；`mean_episode_len=6.0` = 12 成员 ÷ 2 段。`fallback_steps` / `segment_failures` / `verify.defects` 全为 0——这是一次干净的运行，这些计数器不为零时的读法在 25.5。

## 25.5 调优与审计闭环

**三个旋钮，按影响面排序。**① `gap_s` / `gap_steps`（会话粒度）：gap 偏大 = 欠分割，还有 LLM 精化兜着；gap 偏小 = 过分割，**段一旦切碎就再也拼不回来**（LLM 只在会话内精化，v1.9 的缝合算子同样只在会话内缝——跨会话永远无解，第 26 章）——这就是 `gap_s` 默认给到 300 秒偏大值的结构性理由，宁欠勿过。② `segment.window`（滑窗宽度）：窗内上下文越足判得越稳，业界证据甚至偏向「整段单调用」形态——会话普遍不长时直接把 window 调到 ≥ 会话长度，滑窗天然退化为整段单调用；窗小步多则调用省不了几个、接缝还多。③ `segment.context`（域上下文）：告诉审核员「这是什么流」（本例声明了「通知面板、弹窗属干扰帧」），它不定义边界，但能收敛噪声判定的口径。

**边界审计：抽读 `segment.boundary`。**每窗一条事件，`relations` 是逐帧判决、`reason` 是逐帧理由（订阅 segment 通道 + `content="refs"` 起携带）。抽读法：挑判决密度高的窗，把 relations 与你的人工预期逐帧对——本次真跑第一窗（真实 trace 行，格式化展示；`…` 处省略 `run_id`/`batch_no`/`member_ids` 与其余帧的同构内容）：

```json
{"ts": "2026-07-14T04:53:55.881+08:00", …, "stage": "segment", "ev": "segment.boundary",
 "payload": {"session_id": "f00e41052479a460", "window": [0, 8],
   "relations": [{"index": 0, "relation": "continues"}, …,
                 {"index": 4, "relation": "interruption"},
                 {"index": 5, "relation": "advances"}, …],
   "model": "glm-5.2",
   "reason": […, "切换到社交App查看新消息，与外卖点餐任务无关的短暂插入",
              "回到外卖App购物车页，川味麻辣烫等任务实体跨屏延续，继续点餐结算流程", …]}}
```

index 4（帧 5）的 `interruption` 与 index 5（帧 6）的 `advances` 正是 25.3 那张词表的活例：审核员看见了实体延续，噪声帧没有把任务切成两半。对边界不满意的调参循环：改 `context` / 调 `window` / 动 gap → 同 seed 重跑 → diff 两次的 boundary 事件。

**extract 的可靠性预算：按 70–80%/步做计划。**LLM zero-shot 动作推断的实测可靠性就在这个区间（Watch & Learn 70.5%、Sharingan 70–80% 且按动作类型不均衡）——每步 20–30% 的错误率会沿 episode 级联，**不要把单步 steps 当真值消费**。工具承诺的是缓解链而非单步正确性：`include_diff` 的树 diff 证据（默认开，可关做 A/B——对照读数就是 `extract.by_type` 分布与 verify 缺陷率）、verify 缺陷路由兜底（步骤↔标签不符会被打 `label_mismatch`）、quality 结构分软门（连贯性/噪声残留压分可疑段）。日常盯两个计数：`by_type.other` 占比异常升高或某类型塌缩 = 系统性劣化信号；`fallback_steps` 持续非零 = 摘取输出结构不稳，先查 trace 的 error 事件。

**帧摘要贫瘠与 `use_vision`。**纯文本裁决的第一瓶颈是帧摘要保真度——摘要没抓到的实体，LLM 看不见。摘要贫瘠（可见文本节点为零或摘要长度趋零：画布类屏幕、ghost nodes）会计入 `report.stream.digest_poor_frames` 并打一次 WARN；该计数明显非零时给 `segment.use_vision = true` 补偿（窗内逐帧附图，成本相应上去）。本次真跑为 0——fixture 的树信息充足。

**长 episode 的信度注记。**episode 超过 ~20 步后，LLM 对整段的判分信度会衰减（业界同证据）。两个缓解：质量侧改 pairwise（相对比较对长序列比绝对刻度稳）；或对超长段的分数降信任、把裁量交给人工抽检。

**成本账**（形制同第 17 章 §17.1；设会话长 L、窗长 w）：

| 来源 | 次数 | 本次真跑 |
|---|---|---|
| segment | Σ ceil((L−1)/(w−1))，L≥2 的会话；rules/孤帧计 0 | ceil(13/7) = 2 |
| extract | Σ(L−1) 报**上界**（剔噪后实际 = Σ(成员数−1)） | 估 13、实际 10 |
| quality / annotate / verify | 记录基数变为 episodes（估算以会话数报**下界**） | 2×4 准则 = 8；2；2 |

`--dry-run` 的估算行无条件打印 `segment_calls` / `extract_calls` / `stitch_calls`（v1.9 增，未启用恒 0）。本次真跑合计 27 次调用（default 25 + judge 2）、73 秒——比表中各项之和多出的 3 次是结构修复环的调用（trace 里可见三条 `resolved_at=l3_1` 的修复事件），估算历来不含修复。`per_stage_s` 里 quality 仍是大头（39.7 秒）——第 17 章的结论在 stream 下不变，但 extract（12.5 秒）已经是第二大项，episode 越长它占比越高。

**`--strict` × 噪声帧。**stream 工程的噪声帧是**预期产物**——但它们进 rejects，`--strict` 会因 rejects 非空退出 1。CI 里给 stream 工程挂 strict 前想清楚：要么接受「有噪声帧就红」，要么改为解析 report（比如只在 `failed > 0` 或 `verify.defects` 非零时报警）。

## 25.6 常见问题

**任务被打断、切成了两段怎么办？**这是分段的正确行为，不是 bug——分段的单元是「连续做一件事的段」，用户中途切去回消息，外卖任务在时间轴上就是两个碎片。想把它们按任务线索缝回一条完整记录（接缝处机械占位一步），开 v1.9 的缝合算子——`[stitch]`，配置、机制与验收全在第 26 章。本章的 `examples/stream` 刻意保持 stitch 关闭：它是 v1.8 行为的字节等价回归锚（关缝合时主输出/rejects/report 与 v1.8 逐字节一致，唯缺陷词表恒多一行 `wrong_stitch: 0`）。

**孤帧会话去哪了？**不会静默消失。`len(session) == 1` 的会话走 rules 退化：原样成一个单帧 episode（零 LLM 调用），**不经 min_len**——min_len 只砍「LLM 精化切出的短段」。所以帧 14 那条 `below_min_len` 的完整因果是：它在 14 帧大会话里被判 `returns_to_entry`、开了一个只有自己的新段，段长 1 < 2 才被丢——假如它自成一个会话（比如配了 `gap_steps` 且序号断开），反而会原样活成 episode。

**为什么 quality 不看图？**三重原因：trajectory rubric 的四条准则全是**结构性**判据（推进到终态了吗、步步承接吗、朝单一目标吗、混了无关步骤吗），动作序列 + 帧摘要足以裁决；序列打分若逐帧附图，一个 20 帧 episode × 4 准则就是 80 张图的开销；且多图请求有硬上限（见下条）。这是 vision 能力要求的显式放宽——stream（UI 模态）各阶段里 extract 恒要求 vision，annotate/verify/classify 启用时同样要看图，segment 仅 `use_vision=true` 时要求，quality 与 v1.9 的 stitch 判定则是纯文本（后者的证据是摘要卡，第 26 章）。

**多图上限与 `sequence_frames` × `max_image_px` 的联动是怎么回事？**Anthropic 端点对「单请求 >20 张图且任一图 >2000px」直接 400 硬拒（不是自动缩放）。序列标注一请求带 ≤ `sequence_frames` 张关键帧图（默认 20，恰在界内）；把它调到 >20 且所引 profile 的 `max_image_px > 2000` 时，M1 启动即 WARN——两条出路：`max_image_px` 降到 2000，或把帧数降回来。降采样本身是纯整数公式（首末帧恒保留、均匀取样、零随机），成员数 ≤ sequence_frames 时全量带图。openai_compatible 一侧工具**不设独立上限**：官方口径宽松得多（1500 图/请求、512MB 载荷），但真实约束面在网关——Azure 文档写 10 图、GPT-4o 实测 20 图硬顶，vLLM/SGLang 的多模态上限随部署配置变化——静态校验必然虚警或漏警，建议对自己的端点用 `labelkit validate --probe` 加小样本试跑（`--limit`）实测确认。

**什么是 hard-split（会话硬切）？**单个会话装不进一个批（会话长 > `run.batch_size`）时，M10 按批容量硬切会话并 WARN 一次，切出的帧带 `session_split` 标记（落 `_meta.stream.session_split`）——它是 verify 判「缺帧」时的降级依据（缺的帧可能在隔壁批，不是采集断档）。M1 在 `stream.session_max_len > run.batch_size` 时会提前警告这个组合。正确姿势：让 `batch_size ≥ session_max_len`，从源头避免硬切。

最后一份检查清单，开 stream 前过一遍：输入按时间序排好且（配了分区键时）按键成组；`batch_size ≥ session_max_len`；trace.channels 加了 `"segment"`（边界审计全靠它，调优期必开）；quality 不设 threshold、留给后筛；CI 的 `--strict` 策略想好了噪声帧怎么算；下游知道一行 = 一个 episode、成员溯源在 `_meta.stream.member_sources` 了吗？
