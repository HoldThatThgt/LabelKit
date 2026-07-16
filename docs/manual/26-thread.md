# 第 26 章　线索缝合 stitch：把被穿插切开的任务缝回完整线索

> stitch 是 v1.9 新增的算子：在会话内把**同一个任务被其他活动穿插切开的 episode 碎片**
> 保守地缝合成完整**线索**（thread），顺手救回因过短被误剔的收尾帧，并给接缝处补上机械占位步。
> 读完本章你应当能回答三个问题：**什么样的流需要缝合？碎片是怎么被保守地缝回去的？
> 线索产物的账怎么对？**本章样例全部来自 `examples/thread` 的真实运行。

## 26.1 为什么要缝合：分段是对的，任务结构还是丢了

第 25 章的分段以「连续做一件事」为单元——这是对的，但真实的人不这么用手机：外卖点到一半切去回消息，回来接着提交订单；订酒店订到一半先叫个车，再记两笔备忘，最后回来支付。对这样的**穿插流**，分段的正确输出恰恰是一堆碎片：外卖任务变成两个互不相识的短段，酒店任务变成三个。碎片各自打分、各自标注、各自落盘，「用户订了一次酒店」这个**任务目标从主输出里结构性消失**了——每个碎片都合法，但没有一行写着完整的事。

还有一个更隐蔽的伤口：任务收尾帧天然容易变成短段（用户在切走前密集执行收尾动作，收尾帧聚集在切换点旁边），而 `segment.min_len` 会把不足最短段长的段以 `below_min_len` 丢进拒绝通道——**支付成功页这一帧被当垃圾扔了**，任务从主输出里缺了最关键的结尾。

stitch 的做法：分段产物不动（episode 平面分区保持互斥、帧永远单一归属——**不做**「一帧属于两个任务」的多重归属），在 segment 之后、dedup 之前加一个缝合工位，把同一线索的碎片**并回一个信封**。产出是三级结构 **thread ⊃ fragment ⊃ step**：线索是「一个完整任务」，碎片记录「它曾被切成几段、每段从哪来」，步骤是跨碎片连续编号的动作序列。链序必须在去重之前——缝合改变成员集，判重面要看到最终线索；也必须在摘取之前——接缝处的「转移」是已知的任务中断，由 extract 机械占位、不烧调用（26.3）。

设计上最重要的一条脾气是**保守偏置**：LLM 面对杂乱屏幕流的系统性偏差方向是**过连接**（业界噪声消融的一致结论：宁可硬连也不愿说「以上皆非」），所以默认配置下并入一条线索要 **LLM 判「恢复」与机械先验命中两票都过**（合取，26.3）——错缝的代价高于漏缝：漏缝只是少拼一条完整记录，错缝是把两个任务的帧搅进同一条轨迹喂给下游。

四类规范场景（也是 `examples/thread` 的 fixture 蓝本）：

| 场景 | 布局 | 预期标定 |
|---|---|---|
| V1 串联 | 前 x 帧任务 A，后 y 帧任务 B | 2 线索，零缝合 |
| V2 单交叉 | A 前半 → B → A 后半 | A = 双碎片线索（1 接缝），B = 单碎片 |
| V3 多交叉 | A₁ → B → A₂ → D → A₃ | A = 三碎片线索（2 接缝），B/D 各单碎片 |
| V4 噪声 + 救援 | A → B → 1 噪声帧 → A 的超短收尾段 | 噪声帧照旧剔除；短段命中救援并回 A |

外加一条**负样本协议**：纯噪声会话必须产出零线索、零缝合——缝合器对着垃圾不能缝出东西来。开关是 `stitch.enabled = true`（默认关，要求 `segment.enabled = true`）；不开时主输出、rejects、report.json 与 v1.8 **逐字节等价**（例外恰两处：dry-run 估算行无条件多打 `stitch_calls=0`，stream×verify 的缺陷词表恒多一行 `wrong_stitch: 0`——词表是闭集，第 13 章）。

## 26.2 快速上手：examples/thread 全流程

仓库自带的 `examples/thread` 把四个规范场景加负样本装进一个工程：47 对帧、五个场景子目录，`[stream] key = ["source_dir"]` 让**每个子目录成为一个会话**（编号全树错开，互不冲突）：

- `v1-serial/`（101–108）：订机票 4 帧 + 听音乐 4 帧——串联，什么都不该缝；
- `v2-single-cross/`（201–210）：点外卖 4+3 帧，中间被回复李经理消息 3 帧打断——实体「蜀香园麻辣香锅」「¥45」跨断点延续；
- `v3-multi-cross/`（301–315）：订酒店 3+3+3 帧，被打车 3 帧、记备忘 3 帧两度打断——本章的展品；
- `v4-noise-rescue/`（401–409）：网购跑步鞋 4 帧 → 刷新闻 3 帧 → 低电量弹窗 1 帧（预期噪声）→ 支付成功尾帧 1 帧（1 < `min_len`，预期先被剔、再被救援）；
- `neg-pure-noise/`（501–505）：锁屏、广告弹窗、系统更新、误触相机、低电量——互不相关的纯噪声会话（负样本）。

fixture 由 `tools/gen_fixtures.py` 一次性确定性生成（树是唯一语义源，截图为 PIL 程序化绘制），与 `examples/stream` 同一形制。`project.toml` 的骨架（`[segment].context` 全文见 26.5——它是本工程的第一调优抓手）：

```toml
[stream]
order_by = "input_order"          # UI 模态 = pair_index 升序
key = ["source_dir"]              # 分区键：每个场景子目录一个会话

[segment]
enabled = true
strategy = "hybrid"
window = 16                       # ≥ 最长会话（15 帧）：每会话恰一窗
min_len = 2                       # v4 的 1 帧支付尾段以 below_min_len 落选→进救援候选
context = "…"                     # 交叉流的域上下文声明，全文与解读见 26.5

[stitch]
enabled = true                    # 线索缝合总开关（要求 segment.enabled）
llm = "default"                   # 纯文本判定：摘要卡证据，无视觉必需
rescue_short = true               # below_min_len 短段按连续 run 重组先进候选池
repass = true                     # 有界二遍复评修正一遍贪心漏缝
# max_open / bias / votes / stale_gap_steps / digest_max_chars 取默认
# （4 / "conservative" / 1 / 0 / 400）

[extract]
enabled = true                    # 逐相邻帧对摘取动作；接缝序数零 LLM 机械占位
llm = "default"
```

trace 订阅记得加 `"stitch"` 通道（本例 `channels = ["segment", "stitch", "extract", "verify", "schema"]`）——缝合判定的审计全靠它。跑起来：

```bash
cd examples/thread && mkdir -p out
set -a && source ../../.env && set +a
uv run labelkit run --config ../config.toml --project project.toml
```

stderr 尾部（真实运行，退出码 0，全程约 291 秒）：

```
2026-07-16T20:34:49+08:00 INFO  emitter batch=1 批 1 落盘：主输出 +9 行（累计 9），rejects +6（累计 6）
2026-07-16T20:34:49+08:00 INFO  run     batch=1 batch.end active=9 dropped_dup=0 dropped_lowq=0 dropped_verify=0 failed=0 duration_ms=290875 episodes=12 absorbed=41 dropped_noise=6 stitched=3 threads=9
2026-07-16T20:34:49+08:00 INFO  emitter batch=- finalize：fsync + rename  out/thread-labels.jsonl.part → out/thread-labels.jsonl（9 行）
2026-07-16T20:34:49+08:00 INFO  emitter batch=- 已写出 out/thread-labels.rejects.jsonl（6 行）与 out/thread-labels.report.json
   ── 终版摘要（与 report.counts 逐项一致）──
   scanned=47  ingested=47  bad_input=0  generated=0
   dropped_dup=0  dropped_lowq=0  dropped_verify=0  failed=0  emitted=9
2026-07-16T20:34:49+08:00 INFO  run     batch=0 run.end exit_code=0
```

47 帧进来，主输出 **9 行**——每行一条**线索**。`batch.end` 里的两个 v1.9 新字段直接给出缝合总账：`stitched=3`（三个 episode 被并成了壳）、`threads=9`（= episodes 12 − stitched 3）。逐场景对上 26.1 的预期（会话级数字由主输出 + rejects + trace 汇出，report 是合计行）：

| 场景（会话） | 帧 | episodes | threads | stitched | seams | rescued_short | dropped_noise | absorbed | 冗余校验 threads = episodes − stitched |
|---|--:|--:|--:|--:|--:|--:|--:|--:|---|
| V1 串联 | 8 | 2 | 2 | 0 | 0 | 0 | 0 | 8 | 2 = 2 − 0 ✓ |
| V2 单交叉 | 10 | 3 | 2 | 1 | 1 | 0 | 0 | 10 | 2 = 3 − 1 ✓ |
| V3 多交叉 | 15 | 5 | 3 | 2 | 2 | 0 | 0 | 15 | 3 = 5 − 2 ✓ |
| V4 噪声 + 救援 | 9 | 2 | 2 | 0 | 1 | 1 | 1 | 8 | 2 = 2 − 0 ✓ |
| 负样本 纯噪声 | 5 | 0 | 0 | 0 | 0 | 0 | 5 | 0 | 0 = 0 − 0 ✓ |
| **合计（= report）** | **47** | **12** | **9** | **3** | **4** | **1** | **6** | **41** | **9 = 12 − 3 ✓** |

五行全部与设计预期严丝合缝：串联零缝合、单交叉一缝一接缝、多交叉两缝两接缝、V4 救回 1 帧（`rescued_short=1`）且低电量弹窗照旧进拒绝通道、纯噪声会话零产出。V4 的接缝值得一提：救回的支付尾帧（409）与网购前半的末帧（404）之间隔着新闻线索的 3 帧（外加 1 帧噪声）——按判据构成接缝（`seams=1`），而不是「救援 = 接缝」（辨析见 26.6）。

## 26.3 机制：单调选池两遍缝合、机械先验合取与接缝标定

**按会话独立执行，会话内严格串行。**候选流 = 会话内的 episode（按会话序）+（`rescue_short = true` 时）`below_min_len` 短段按**连续 run 重组**的救援候选——注意 `reason="noise"` 的噪声帧**不入候选池**（V4 的低电量弹窗、负样本会话的五帧从没资格参与缝合）。池是一个串行决策过程（前一个候选的归属影响后一个候选看到的池），所以会话内逐候选处理、零随机；并发面在会话之间。

**一遍：单调贪心选池。**每个候选一次 LLM 调用，提示词呈现池内全部开放线索的**摘要卡**（按最近活跃降序编号）+ 候选摘要卡，要求输出 `resume`（恢复第几条线索）或 `new`。摘要卡是判定证据的全部——逐行是：任务名、App 集合、序号跨度｜帧数｜碎片数、首帧摘要、尾帧摘要，外加一行「接续对（线索尾帧 → 候选首帧）变更」的结构化树 diff。证据面刻意停在**帧摘要级**：链序上 extract 还没跑，缝合时批内没有任何动作序列可用。三条关键规则：

- **池空的 episode 候选照常判定**（呈现零张线索卡，verdict 恒 `new`）——这次调用的 `task_name` 是线索命名的唯一来源（后续滚动更新）；
- **救援候选永不开新线索**：池空直接跳过（零调用）维持 `dropped_noise`；池非空才判定，命中 → 并入 + 成员帧从 `dropped_noise` 翻回 `absorbed`（计 `rescued_short`，单位是帧），未命中（含判定失败）→ 原样留在拒绝通道；
- **池满才封闭**（`max_open`，默认 4）：需要开新线索而池已满时，按「挂起跨度超 `stale_gap_steps` 者优先、LRU 兜底」逐出一条。封闭 ≠ 终结——被逐出的线索不再出现在一遍的卡集里，但仍是二遍复评的目标、照常产出。

**保守合取（`bias = "conservative"`，默认）。**LLM 判 `resume` 只是一票，还要机械先验白名单**至少一腿命中**（析取三腿，确定性代码、零调用）：① `app_overlap`——App 集合交集非空；② `entity_overlap`——线索尾帧与候选首帧的可见文本实体重叠（正是 fixture 埋「蜀香园麻辣香锅」「湖畔云居酒店」跨断点实体的原因）；③ `same_page`——候选首帧回到线索某碎片尾帧的同一页面（页面标识 = app + activity，采集侧没把 activity 写进树的 `extra` 时该腿静默失效）。候选与线索尾的跨度超过 `stale_gap_steps`（默认 0 = 不启用）时先验**降格为须两腿命中**——挂得越久，恢复的证据要求越高。`bias = "llm"` 跳过先验（纯 LLM 判），只该在审计消融时用。

**二遍：有界复评（`repass = true`，默认）。**顺序贪心有一类结构性漏缝：处理 A 前半时 B 还没出现，A 后半来时判定官只看到「B 最活跃」的池，可能漏判。二遍把**一遍结束时的单碎片线索**逐个再判一次（池 = 会话内全部其他线索，活视图），命中则**方向反转**——候选并入目标线索、目标幸存。预算有界（≤ 单碎片线索数），这是增量聚类文献里「n=1 局部重聚类即可追平批处理质量」的工程对应物。

**接缝标定（定案后，零 LLM）。**对每条多碎片线索，逐个拼接对查判据：**两成员的会话序间隙里含 ≥ 1 个归属其他线索的帧 ⟺ 接缝**。是接缝的，extract 不调用 LLM，直接写四键机械占位步 `{action_type: "app_switch", target: null, value: null, description: "线索接缝：被〈打断者〉打断后恢复"}` 并把该步 `resumed` 置 true；间隙里只有噪声帧或本线索救援帧的拼接对**不是接缝**——那是真实转移，照常送 LLM 摘取（与 v1.8「剔噪对照常摘取」同一惯例）。接缝是已知的任务中断，推断「中间发生了什么动作」只会制造反事实。

判定失败（结构修复耗尽）的处置见第 18 章 `stitch_invalid` 行：默认 `keep`——episode 候选开新线索存活（未命名）、救援候选维持 dropped_noise，都只留 trace 事件 + `stitch.failures` 计数。

**这套机制在本例里的走线**：19 次判定 = 一遍 13（12 个 episode 候选 + V4 的 1 个救援候选）+ 二遍 6（V1 两条、V2 一条、V3 两条、V4 一条单碎片线索的复评）；4 次并入全部是「LLM resume + `app_overlap` ∧ `entity_overlap` 双腿命中」。V2 的那次并入（真实 trace 行，格式化展示；`…` 处省略 `ts`/`run_id`/`batch_no`）：

```json
{…, "stage": "stitch", "ev": "stitch.judge", "record_ids": ["bb02cb1f4a409be0"],
 "payload": {"session_id": "66b9c1b6e5f3fe2e", "candidate": "episode", "repass": false,
   "verdict": "resume", "thread_ref": 2, "confidence": "high",
   "priors": ["app_overlap", "entity_overlap"], "merged": true,
   "task_name": "在美食外卖App中选购蜀香园麻辣香锅并完成下单",
   "reason": "候选碎片仍在 com.example.food 中操作同一商品蜀香园麻辣香锅，且从购物车\"去结算\"自然延续到\"确认订单→提交订单→下单成功\"，任务实体和操作流程明确承接。",
   "target_thread_id": "6b8ad5a0ff490a76"}}
```

`verdict` 与 `merged` 是两个字段不是一个：LLM 判了 resume 而先验一腿未命中时，`verdict="resume", merged=false`——合取把这一票拦下了。审计漏缝/错缝时先看这两个字段的分离情况（26.5）。

## 26.4 输出怎么读

**主输出**一行 = 一条线索。V3 的展品行（真实运行产物，格式化展示；`run`/`source` 形态同第 25 章、`member_ids` 九项从略）：

```json
{
  "task_label": "预订杭州酒店并查看入住凭证",
  "app": "com.example.hotel",
  "summary": "在酒店App中搜索并预订杭州湖畔云居酒店大床房，完成支付后查看订单详情和入住凭证。",
  "_meta": {
    "id": "3661a64882a11c79",
    "run": {…}, "source": {…},
    "stream": {
      "episode_id": "3661a64882a11c79",
      "thread_id": "3661a64882a11c79",      ← v1.9 新键：恒等于本行 id（幸存信封的身份链）
      "session_id": "d679e9b196b41fb2",
      "order_span": [301, 315],              ← 包络！读法见下方警句
      "member_count": 9,
      "member_ids": […],
      "member_sources": [{"file": "v3-multi-cross/uitree_301.jsonl", "pair_index": 301}, …,
                          {"file": "v3-multi-cross/uitree_315.jsonl", "pair_index": 315}],
                                             ← 304–306（打车）与 310–312（备忘）缺席：归属别的线索
      "session_split": false,
      "repaired": false,
      "degraded": null,
      "fragments": [                         ← v1.9 新键：碎片跨度表，线索的「装订记录」
        {"order_span": [301, 303], "member_count": 3, "cause": "origin",
         "source_episode": "3661a64882a11c79"},
        {"order_span": [307, 309], "member_count": 3, "cause": "resumed",
         "source_episode": "07299f6d7c066f4e"},
        {"order_span": [313, 315], "member_count": 3, "cause": "resumed",
         "source_episode": "6a0c943729b7a125"}],
      "steps": [                             ← 全线索连续编号 0..n−2（9 成员 ⇒ 8 步）
        {"index": 0, "action_type": "click", "target": "搜索酒店", "value": null,
         "description": "点击\"搜索酒店\"按钮，搜索杭州的酒店并跳转到搜索结果列表页", "resumed": false},
        {"index": 1, "action_type": "click", "target": "湖畔云居酒店", "value": null,
         "description": "点击酒店列表中的\"湖畔云居酒店\"进入酒店详情页", "resumed": false},
        {"index": 2, "action_type": "app_switch", "target": null, "value": null,
         "description": "线索接缝：被在打车App上呼叫快车前往城西银泰城打断后恢复", "resumed": true},
        {"index": 3, "action_type": "click", "target": "下一步", "value": null,
         "description": "在订单填写页面点击\"下一步\"按钮，进入确认订单页面", "resumed": false},
        {"index": 4, "action_type": "click", "target": "去支付", "value": null,
         "description": "在确认订单页面点击\"去支付\"按钮，进入支付订单页面", "resumed": false},
        {"index": 5, "action_type": "app_switch", "target": null, "value": null,
         "description": "线索接缝：被在备忘录App中记录购物清单等日常备忘打断后恢复", "resumed": true},
        {"index": 6, "action_type": "click", "target": "查看订单", "value": null,
         "description": "在支付成功页面点击\"查看订单\"按钮，跳转到订单详情页面", "resumed": false},
        {"index": 7, "action_type": "click", "target": "入住凭证", "value": null,
         "description": "在订单详情页面点击\"入住凭证\"按钮，进入入住凭证页面", "resumed": false}]
    },
    "scores": {"completion": 1.0, "purposefulness": 1.0, "coherence": 1.0,
                "noise_residue": 0.4, "__aggregate__": 0.85, "mode": "pointwise", "batch_no": 1},
    "dedup": {"kind": "unique"},
    "classification": null,
    "annotation": {"model": "glm-5.2", "attempts": 1},
    "verification": {"verdict": "pass", "rounds": 1, "defects": []}
  }
}
```

逐键读法：

- **`fragments`** 是线索的装订记录：`cause` 三值——`origin`（一遍开线索的创始碎片）、`resumed`（判定并入的恢复碎片）、`rescued`（救援回来的短段碎片）；`source_episode` 是碎片并入前的原 episode id（拿它能在 trace 里找回那次判定），`rescued` 碎片的 `source_episode` 恒为 `null`——短段从没成过 episode。V4 那行的 fragments 是 `origin [401,404]` + `rescued [409,409]` 的两碎片形态，可对照着读。
- **接缝步**是 `steps` 里 `resumed: true` 的行：`action_type` 恒为占位的 `app_switch`、`target`/`value` 恒 null、`description` 是模板文案（含打断者任务名）——**下游判别接缝的可靠依据是 `resumed` 标志**，别拿 `action_type == "app_switch"` 当判据（真实的应用切换动作也叫这个名字）。步序号跨碎片连续（0..7），接缝步占一个正常序号。
- **`order_span` 包络警句**：多碎片线索的顶层 `order_span` 是首末成员的**包络**——[301, 315] 里含着打车与备忘的 6 帧异线索帧！按顶层跨度回原始流切片必然切进别人的帧，**下游切片必须用 `fragments[].order_span`**。
- 一处诚实的旁注：三条被打断过的线索 `noise_residue` 分别被打了 0.6 / 0.4 / 0.6——接缝步带着专用后缀「（线索接缝：被 X 打断）」告知了评审（第 10 章），但 trajectory rubric 的噪声残留判据对「曾被打断」仍会压些分。没设 threshold 时这只是随行落盘的信号，后筛裁量权在你。

**拒绝通道**恰好 6 行，与 `dropped_noise=6` 对齐：V4 的低电量弹窗（408，reason=`noise`）+ 负样本会话的 5 帧（全部 reason=`noise`）。注意**没有** `below_min_len` 的行——409 被救援翻回 `absorbed` 了；而 `report.stream.below_min_len = 1` 仍然是 1（它是发生计数，救援不回退），两个数字讲的是同一帧的两段经历。

**报告**的 v1.9 增量（真实产物）：`counts` 增 `stitched` / `threads` 两键（见 26.2 的表），`stream` 节新增 `stitch` 子块：

```json
"stitch": {
  "stitched": 3,            ← 被并成壳的 episode 数（救援不产生壳，不计入）
  "rescued_short": 1,       ← 救援翻回的帧数（单位 = 帧）
  "seams": 4,               ← 满足判据的接缝数（接缝的唯一计量点）
  "judgments": 13,          ← 一遍判定数（12 episode 候选 + 1 救援候选）
  "repass_judgments": 6,    ← 二遍复评判定数
  "failures": 0             ← 判定修复耗尽次数（keep/fail 两路都计）
}
```

**守恒对账**（第 4 章全式，v1.9 左侧多一项 `stitched`）：

```
emitted + dropped_dup + dropped_lowq + dropped_verify + dropped_noise + failed + bad_input + absorbed + stitched
  = scanned + generated + fanout + episodes
左 = 9 + 0 + 0 + 0 + 6 + 0 + 0 + 41 + 3 = 59；右 = 47 + 0 + 0 + 12 = 59 ✓
```

直觉读法：右侧每个 episode 都是凭空追加的新信封（+12），左侧的壳（+3）把「一个 episode 并进另一个」的抵扣记回来——所以 `threads = episodes − stitched` 恒成立，报告里 `counts.threads` 就是按这个恒等式导出的单点。extract 的账也变了口径：`stream.extract.transitions = 28` = 32 个相邻对 −4 个接缝占位——**占位步不计入 `transitions` 与 `by_type`**（零 LLM 的机械产物不该灌污动作分布；本例 `by_type.click = 28`，没有一个占位的 `app_switch` 混进来）。接缝的唯一计量点在 `stream.stitch.seams`。

## 26.5 调优与审计闭环

**穿插流的两个推荐配置动作**（都是既有配置键，`examples/thread` 实际用到并验证过的）。

**① 给 `segment.context` 讲清楚「这是条穿插流」。**分段在缝合的上游，分段的口径直接决定缝合的输入质量：穿插流里「切回被搁置的任务」必须开新段（否则碎片根本切不出来），而弹窗类插入必须判噪声（否则救援池被垃圾污染）。本工程的实际配置（照抄可改）：

```toml
[segment]
context = "手机屏幕操作录屏流；流中各项活动（购物、通讯、出行、资讯等）互为独立任务、彼此无从属或配合关系——用户常在任务间来回切换，被搁置的任务稍后可能切回收尾：切到另一任务处、切回被搁置任务处（含回到其支付/订单页完成收尾）都是新流程的开始（context_switch），advances 仅适用于为推进当前这一笔任务而发生的跨屏实体延续（如去短信里取验证码）；低电量弹窗、系统弹窗、通知面板、锁屏、广告弹窗等与前后操作无关的短暂插入屏属于干扰帧（interruption），不是任务切换"
```

三个要点全在里面：声明各活动**互为独立任务**（防止 LLM 把「打车去商场」脑补成「订酒店行程的一部分」而判 `advances` 不切段）；声明**切回挂起任务 = 新流程开始**（碎片由此切出，缝合由此有料可缝）；**枚举本域的弹窗噪声原型**（低电量、系统弹窗、通知面板、锁屏、广告——收敛 `interruption` 的口径）。

**② 给 `verify.extra_criteria` 说明构造事实。**线索是 N 个成员帧配 N−1 步（每相邻对一步，接缝是机械占位步），评审不知道这个构造，容易把「步数比帧数少一」误判成 `missing_members`。本工程的实际配置：

```toml
[verify]
extra_criteria = "补充审核约定：动作序列恒为 成员帧数−1 步（每相邻帧对摘取一步，线索接缝为机械占位步），步数比成员帧数少一是构造使然，不构成 missing_members / missing_tail 的证据；标注摘要允许概括中间成员帧的可见内容（审核证据仅含首末帧截图），除非与所给证据直接矛盾，不因此判 label_mismatch"
```

本次真跑 9 条线索全部一轮 pass、缺陷表全零（含 `wrong_stitch: 0`）——这行约定在早期调参轮里就是为了消掉「构造性误报」加上的，值得照抄进你自己的 stream×stitch 工程。

**审计：抽读 `stitch.judge`。**订阅 `stitch` 通道后每次判定一条事件，抽读法盯三处：`verdict` 与 `merged` 的分离（resume 而未并入 = 先验拦截，看 `priors` 缺了哪条腿——是证据真不足还是采集侧没给 activity）；`repass: true` 的事件（二遍修回来的漏缝有多少）；`task_name` 的滚动演化（V3 的线索名从开线索时的「在杭州搜索并预订湖畔云居酒店大床房」经两次并入滚动到「在杭州预订湖畔云居酒店大床房并完成支付及查看入住凭证」，说明摘要卡在跟着碎片长大）。`stitch.thread` 事件则是每条线索的定案快照（fragments + seam_indexes），拿它跟 `_meta.stream.fragments` 对账。

**负样本协议：拿纯噪声会话当门禁。**`neg-pure-noise` 会话在本次真跑里产生了**零次**缝合判定——五帧全被判 `noise`，噪声帧连候选池都进不去。给自己的采集管线留一段确认无任务的纯噪声流做常驻负样本：它一旦缝出线索，说明分段的噪声口径或缝合的保守面出了问题——这比任何正样本都更早暴露「过连接」倾向。

**验收线：错缝帧数 = 0。**漏缝与错缝不对称：漏缝损失的是完整性（还能靠调参救），错缝污染的是数据本身（下游很难发现）。验收一个 stitch 工程时，人工核对每条多碎片线索的 `fragments`（`source_episode` 能回溯到原段），把「错缝帧数为零」当硬线；日常监控盯 verify 的 `wrong_stitch` 计数。四个旋钮的方向感（详表见第 17、18 章）：错缝倾向 → 保持 `conservative`、开 `votes`（3/5，奇数）、设 `stale_gap_steps`；漏缝倾向 → 补强 `segment.context` 与实体证据、确认 `repass` 开着、必要时上调 `max_open`。

**成本账。**本次真跑 113 次调用（default 103 + judge 10）、291 秒；`per_stage_s` 里 stitch 以 117.3 秒居首——判定本身只有 19 次，但**会话内串行**是结构性的（池是串行决策过程），长会话的判定链不能并行摊薄。对照 `--dry-run` 的估算（真实输出）：

```
dry-run: mode=process estimated_records=47 batches=1
dry-run: estimated LLM calls — generate_calls=0 segment_calls=5 stitch_calls=10 classify_calls=0 extract_calls=42 quality_calls=20 annotate_calls=5 verify_calls=5 total=87 (excludes retries and repair calls)
dry-run: 注：stream 估算：下游按 episodes≈sessions 报下界（LLM 精化只增段数）
dry-run: no LLM calls made, no output written (report and trace only)
```

`stitch_calls=10` = 5 会话 ×（一遍 1 + 二遍 1）× votes 1——估算沿用 stream 的「episodes ≈ sessions」下界口径（实跑 19 次判定）；quality/annotate/verify 同理报下界、extract 报上界（42 = 剔噪前相邻对数）。实跑比估算多出的部分 = 下界口径差 + 7 次结构修复调用（trace 里 30 条 `schema.repair` 事件，23 条在 L1 确定性层免费解决、7 条走了 L3 修复环）。`votes` 的账见 26.6 末条。

## 26.6 常见问题

**开了缝合，`--strict` 反而从 1 变 0 了？**预期行为。`--strict` 的判据是「rejects 非空」；开缝合前，V4 的支付尾帧以 `below_min_len` 落在拒绝通道里；开缝合后它被救援翻回 `absorbed`，rejects 少了一行。stitched 壳也不写 rejects（只计数）。同一份输入，「拒绝更少」是缝合把该救的救回来了——不是账目错误（第 8、15 章）。

**救援回来的帧和线索紧挨着，为什么有时算接缝、有时不算？**判据只有一条：**拼接对的会话序间隙里有没有归属其他线索的帧**（噪声帧不算数）。V4 的救援帧 409 与网购前半的末帧 404 之间隔着新闻线索的 3 帧（外加 1 帧噪声）——是接缝，占位步的 description 点名了打断者（「被在每日头条浏览台风新闻并查看评论打断后恢复」）。假如短段紧贴着自己线索的尾碎片（间隙里只有噪声帧、或干脆相邻），那是**真实转移**：照常送 LLM 摘取，不占位、不计 seams。「救援」说的是帧的来路，「接缝」说的是缝合处的间隙内容，两个概念正交。

**能跨会话缝吗？断在两个批里的会话呢？**都不能。缝合的作用域钉死在**会话内、批内**：会话是「一次连续采集」的边界，跨会话的「同一任务」缺乏时序连续性证据，缝了就是赌；被 `batch_size` 硬切的会话（`session_split` 标记，第 25 章）同样不可跨切缝合——启动时会有 WARN 提醒你调大 `batch_size`。会话切分期的过分割（gap 偏小）在缝合层面无解——第 25 章「宁欠勿过」的告诫在 v1.9 依然成立。

**线索内部还想标「子任务跨度」（比如把 8 步酒店预订再切成搜索/下单/支付三段）怎么办？**用**标注层模式**，不是引擎特性（这是明确的设计决策：引擎侧的子任务嵌套校验被需求方否决——下游没有消费方，徒增结构复杂度）。做法：在输出 Schema 里自己声明跨度字段，让标注指令按步序号填写：

```json
"subtasks": {"type": "array", "items": {"type": "object",
  "properties": {"label": {"type": "string"},
                 "step_range": {"type": "array", "items": {"type": "integer"},
                                "minItems": 2, "maxItems": 2}},
  "required": ["label", "step_range"], "additionalProperties": false}}
```

标注指令里写明「`step_range` 为 [起始步序号, 结束步序号] 闭区间、按 `steps[].index` 计」即可——结构引擎照常保证它合法，接缝步的序号也在同一坐标系里。

**`votes` 是什么？该开吗？**判定稳定化采样：默认 `1`（单调用，不启用）；设为 ≥3 的**奇数**（偶数直接配置错误）时，同一判定采样 n 次，对 (verdict, thread_ref) 完整判定取**严格多数**——凑不齐严格多数（含 verdict 一致但 thread_ref 分裂）一律回落保守结局（episode 候选判 new、救援候选按未命中）。它的定位是「口头置信度门槛的正规替代」：治**漂移**（同一判定跨运行摇摆），不治**偏差**（系统性过连接）——所以它跟机械先验合取不可互替、只能叠加。成本直白：判定调用 ×n（n=3 时 stitch 全口径占比仍 <8%，同前缀采样吃 prompt 缓存）。开的时机：同 seed 重跑时 `stitch.judge` 的判定翻转可测，再开——本例 fixture 实体证据充足，votes=1 就四缝四中。

最后一份检查清单，开 stitch 前过一遍：`segment.enabled = true` 且分段口径按穿插流调过（26.5 ①）；`trace.channels` 加了 `"stitch"`；verify 开着的话 `extra_criteria` 写了构造约定（26.5 ②）；留了纯噪声负样本会话当门禁；下游知道一行 = 一条线索、切片用 `fragments[].order_span` 而不是顶层包络、接缝步认 `resumed` 标志了吗？
