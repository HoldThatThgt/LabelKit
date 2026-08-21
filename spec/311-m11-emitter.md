## 3.11 M11 输出器 emitter

### 3.11.1 职责与边界

**做：**三通道写出——主输出 JSONL（增量追加、终检、原子改名交付）、rejects 通道、report.json；组装 `_meta`；stderr 进度与结束摘要。 
**不做：**不修改标注内容；不做结构校验之外的任何数据加工（写出前调用 `SchemaEngine.validate_only` 做最后一道终检，失败即 bug——fail loudly，转 rejects 并记 `internal_error`）；报告不含数据内容。

### 3.11.2 写出规格

| 通道 | 规格 |
|---|---|
| 主输出 | 运行期写 `{output}.part`，每批 flush；finalize 时 fsync + 原子 rename 为目标名。行格式见 6.3。仅 `status="active"` 且（annotate 启用时）标注成功的记录写入。v1.6：熔断中止（退出码 4）的 finalize 同样执行交付（3.10.3 熔断交付）——已交付文件中每一行恒完整合法，运行是否完整处理了全部输入以 report.run 判定（interrupted=false 且 circuit_broken=false，3.11.3 ④）。v1.8：`status = "absorbed"`（成员帧已并入 episode，3.14）为**第三路由**——主输出与 rejects 均不写、**仅计数**（成员内容以 `members` 引用随其 episode 的序列行落盘）；分发规则变为 active → 主输出、absorbed → 仅计数、其余非 active 状态 → rejects。v1.9：`status = "stitched"`（被并 episode 壳，3.16）为**第四路由**——同 absorbed 仅计数（其成员随幸存线索信封的序列行落盘）；分发规则变为 active → 主输出、absorbed / stitched → 仅计数、其余非 active 状态 → rejects——壳**不得**落入兜底 else → rejects 分支（否则以 internal_error 之名污染 rejects 且 `--strict` 必退 1）。`_meta` 增**恒在键** `stream`（未启用 segment = null；键位在 source 之后、scores 之前——链序镜像；结构见 6.3）；stream 模式下 `_meta.verification` 另含恒在 `defects` 键（无缺陷 = []，6.3）。v1.9：stitch 启用时 `_meta.stream` 另含 `thread_id` / `fragments`（含 order_span 包络规范句）/ steps 行内 `resumed`（三者**仅启用时在场**——off 时行内容与 v1.8 逐字节等价，6.3、3.16.4 退化锚）。stderr 进度与结束摘要**不增键**（fanout 先例——episodes / absorbed / dropped_noise（及 v1.9 stitched / threads）经 report 与 batch.end 事件可见，3.10.3；v1.10 U18：不增键约束限 plain 面——rich 面板状态账展示 stitched/threads，7.7）。v1.10 console 让位（U21）：`_progress` / `_print_summary` 的行格式改经 common 层纯函数 `console_format`（输出与 v1.9 硬编码逐字节一致，黄金快照钉死）；两方法加静态门 `console.mode_resolved == "rich"` 时直接 return——plain 档原样执行（回归锚），rich 档信息由 CLI 层面板超集覆盖（终版摘要换渲染器表格版，数值来源不变，7.7）；中途降级/`q` 脱离后的 plain 行由渲染器以同一 `console_format` 续打。v1.12 **members 块**（任一帧开关启用时在场）：`_meta.stream` 增 `members[]`——**冻结位 = `member_sources` 之后、`session_split` 之前**（两处有序精确断言测试同步该位置）；逐成员按 `rec.members` 序各一条目，**条目字段序冻结** `index, id[, label][, annotation, status]`（`index` 0 基 = 成员在序列中的位次，`id` = 成员 record.id）。**在场规则**：`label` 键仅 `frame.classify.enabled` 时在场（`member_classifications` dict 为 None 或缺键 ⇒ null——覆盖降格跳过）；`annotation` / `status` 两键仅 `frame.annotate.enabled` 时在场。**status 闭集三值判定**（单一真相 = `member_annotations` dict 形态，3.5.5）：dict 缺键（或 dict 为 None）⇒ `"skipped"`（annotation=null）；值 None ⇒ `"failed"`（annotation=null）；对象 ⇒ **写前** `validate_only(obj, schema=frame_schema)` 兜底（3.8.2）——通过 ⇒ `"annotated"`，不通过 ⇒ `"failed"` + annotation 置 null + `frame_annotate.failed` 计数（非法帧对象**零落盘**；成员失败不改信封状态、不写 `item.errors`）。**沉没成本记账**：终态非 active 的序列信封仍携带 `member_annotations` ⇒ 按**非 None 条目数**累计 `frame_annotate.discarded`（已产出未交付，仅计数、不落盘，6.4）。`_meta` 顶层键序、四路由互斥、守恒恒等式**零改动**。v1.13（时间流生成形态，`generate_stream.enabled`）三处修订、其余零改动：① **写前终检按行取 Schema**——`validate_only` 的 Schema 实参改取**该行的类有效 Schema**（`item.classification.label` → 该类声明的标注 Schema 覆盖，未分类 / 未知类 / 该类未声明 ⇒ 走全局 `output.schema` 的既有缺省路径，字节等价 v1.12；multi 扇出的兄弟信封各带自己的标签，按行天然对齐）；本模块**不跨算子导入** M5 的取值函数，按 §2.2 依赖方向在内部持一份最小镜像（两侧取值语义须一致，测试钉住）。② **`_meta.stream` 块的门扩为 `segment.enabled ∨ generate_stream.enabled`**（`members[]` 在场门与其中的 `label` 列门同步扩）——直装序列行原样复用该块：`order_span` 与 `member_sources` 指向**工件路径与行号**（`order_span` 的两端渲染为 `"<工件路径>:<行号>"`，`member_sources` 每项为 `{file: <工件路径>, line_no: <行号>}`——既有渲染器直接可用）、`members[]` 条目 = `{index, id, label}`（label 取 `member_classifications` 的帧类真值；本形态与 `frame.annotate` 互斥 ⇒ 无 `annotation` / `status` 两列，v1.12 的条件列规则相容）、`session_split=false`、`repaired=false`、`degraded=null`、`steps=null`、无 `thread_id` / `fragments`。③ **rubric 镜像**——`_meta.run.rubric` 的空选择器解析镜像同步扩为 `segment.enabled ∨ generate_stream.enabled ⇒ "default:trajectory"`（与 loader 两处同改，3.1.4 S29 扩展）。**零改动（显式声明）**：`_meta` 顶层键序、四路由互斥（本形态只出现 active 与 dropped_* / failed 两类去向——无 absorbed / stitched：成员帧根本不构造信封）、守恒恒等式（取 generate_only 退化形，6.4）。 |
| 时间流工件（v1.13） | **第五输出通道**，仅时间流生成形态出现：`write_stream_artifact(lines)` 把 M6 交付的、按交织序定稿的工件行写入 `{output_stem}.stream.jsonl.part`（写入 + flush；路径规则 = 主输出路径去末级后缀 + `.stream.jsonl`，与 M6 各自推导同一值——算子间不互导，两侧等式由测试钉住），finalize 时与主输出**同批** fsync + 原子改名（3.11.3 ④ 的时间线对工件同样成立：目标名出现即每行完整合法）。**共用 `_undeliverable` 纪律**——通道不可写或写失败即置位，此后 finalize 一律不改名（半截工件永不冒充成品，exit 4 家族）。**dry-run 天然不触达**（`_run_dry` 不驱动生成、不开 emitter 通道）。写入同时冻结 `report.run.artifact = {path, sha256, lines}`（sha256 按落盘字节计，`config_digest` 同款前缀形态；M10 组报告时读取，6.4）。行内容由 M6 定稿——本模块**不生成、不改写、不校验**工件行（格式规范见 6.5）。 |
| rejects | `output.rejects = "none" \| "refs"（默认）\| "full"`。refs：每行仅 `{"_meta": {id, source, stage, reason, errors}}`——不含数据内容（source 亦不含 `passthrough_fields`，其值属数据内容），贴合不存储原则；full：额外含记录内容与最后一版非法输出（调试用，用户显式选择）。文件名 `{output_stem}.rejects.jsonl`。v1.7：classify 启用时 `_meta` 增 `label` 键（= 该信封路由标签；multi 扇出下同 `id` 的兄弟信封由此消歧，行唯一键 (`_meta.id`, label)，6.3），refs / full 两档均携带。v1.8：`dropped_noise` 行按翻转方留下的 duck-typed reason 标记归因分流（此类帧**不写 `item.errors`**，「归因取 `item.errors[0]`」规则无从服务），(stage, reason) 组合恰增**三种**——(`"segment"`, `"noise"`)（LLM 判噪声帧）、(`"segment"`, `"below_min_len"`)（短段丢弃帧，独立于 noise，S11）、(`"verify"`, `"off_task_member"`)（修复收缩弃帧，S31）；absorbed 信封永不入本文件（第三路由）。`--strict` 交互：stream 工程的噪声帧属预期产物，会因 rejects 非空退出 1（6.4）。v1.9：(stage, reason) 组合再增一种——(`"stitch"`, `"stitch_invalid"`)（仅 `stitch.on_error = "fail"` 时出现：判定修复耗尽的 episode 候选信封，3.16.6）；stitched 壳与被救援帧（dropped_noise → absorbed 翻转）**永不入本文件**——`--strict` 补注：同输入开启 stitch 后（短段被救援而不再落 rejects）strict 结果可能由 1 变 0，属预期（2.4、3.16.6）。full 档对序列 Record 的 `record` 载荷输出 `{"kind": "sequence", "member_ids": [...], "member_sources": [...]}`（S25；`kind="single"` 载荷形态不变）；`raw_last_output` 仍仅 `schema_violation` 行携带——classification_invalid / segmentation_invalid / extraction_invalid 失败行不带原始输出（classify 起的既有缺口，明文接受）。v1.11：reason 词表再增**两值**——`context_overflow`（上下文预算三形态：预检/最小单元不装/反应态降级耗尽，V10/V16/V24）与 `output_truncated`（响应以输出上限截断收尾终局化，V11）；stage = 产生该错误的属主算子（任何 LLM 调用阶段皆可出现，语义与熔断矩阵见 7.6）；refs / full 档行形态不变，`raw_last_output` 门不扩（两 kind 均不携带原始输出，同上缺口口径）。v1.12：rejects 面**零改动**——行键集维持既有闭集（refs 五键 / full 六键，有序精确断言），(stage, reason) 组合、reason 词表均不增；**帧标注失败的成员不产生 rejects 行、不触发 `--strict`**（成员失败非信封失败——只落 members[] 条目 status="failed" + `frame_annotate.failed` 计数，裁决·成员失败不入 rejects；`--strict` 读信封状态计数，帧失败不改信封状态）。 |
| report.json | `{output_stem}.report.json`。结构见 6.4：运行参数摘要（脱敏，无 key）、各阶段计数、分数分布直方图、去重簇统计、结构引擎各层命中、token/成本、耗时、失败分类计数。v1.7：classify 启用时增 `classify` 节（assignment / 逐类分布 / fallback_count / failures，multi 另含 multi_label_records）与 `quality.by_class` 按池视图，multi 时 counts 增 `fanout`（6.4）。v1.8：segment 启用时 counts 增 `episodes` / `absorbed` / `dropped_noise`，counts 之后新增 `stream` 节（sessions / episodes / mean_episode_len / absorbed / dropped_noise / below_min_len / digest_poor_frames / segment_failures + extract / verify 可选子块，6.4）。v1.9：stitch 启用时 counts 增 `stitched` / `threads`（= episodes − stitched 导出式，3.10.3），`stream` 节增 `stitch` 子块（stitched / rescued_short / seams / judgments / repass_judgments / failures，6.4）；全部 v1.9 新键**仅启用时在场**——off 时本模块三通道产物与 v1.8 逐字节等价（3.16.4 退化锚；唯一报告面例外 = stream×verify 缺陷词表 `wrong_stitch: 0` 行，词表为 3.7.2 四处同步闭集、不随开关条件化）。v1.11：上下文预算启用时增 `report.budget` 节（profiles / w_min / truncations / overflow_records / image_cost / degrade_retries / escalations——counts-only，M10 汇总，3.10.3、6.4），`stream` 节增 `windows`（segment 实际窗数，M14 属主——供对账 V12 上界估算，6.4）；预算全体未声明时两处不在场，报告与 v1.10 逐字节等价。 |
| v1.16 规则/窗口联合面 | M11 不解释或复制规则、窗口、correlation、planner 状态和 sequence-validator 结果。M6 交付的时间流 artifact 仍由第五通道原样写入，主输出仍按既有类有效标注 Schema 写前 `validate_only`；整条 attempt 的 planner、realize、sample-validator、declarative 或 sequence-validator 作废都不会构造 failed 信封，因此不会被 M11 重新归因。`report.generate.stream` 的 `rules`、可选 validator 计数和 `windows` 子块由 M10 装配；M11 只写 report，不新增 truth、`_meta.stream`、Record id 或 artifact 行字段，规则配置不进入任何输出。 |
| v1.17 场景规划交付面 | M11 只消费 `ResolvedPaths`（3.1.4.2）——主输出 / rejects / report / sidecar / 时间流工件五个通道的路径派生全部移至 M1（含 live `<output-stem>.report.json` 与 dry-run `<output-stem>.dryrun.report.json` 的命名裁决），emitter 与 console 不再按命令模式追加后缀、不再从字符串推导 cwd-relative 路径。**partial exact delivery 原子交付**：任一交付槽位耗尽或 delivery 期间 SIGINT 时，主输出、时间流工件与 rejects 照常按既有 finalize 纪律原子交付已成功部分（目标名出现即每行完整合法，3.11.3 ④ 时间线同样成立）——运行完整性以 `report.generate.stream.delivery.complete` 判定，退出码分流见 3.10.3。**duplicate 行的 truth 双时间语义**：duplicate 工件行的 payload / tier / frame word / `time_fields` 绑定键是 source 的深拷贝（携带 source 的时间字段值），而行 timestamp 与 resource interval 用 duplicate layout 的新时间——有意的双时间语义表示「在新 wrapper 时刻原样重发一份携带 source 时间字段的 payload」，consumer 经 `truth.duplicate_of` 识别该例外（M6 定稿，3.6.7、6.5）；M11 原样写行、不解释、不改写该语义。 |

**背书：**「主数据 + 拒绝通道 + 统计报告」三分法是 NeMo Curator / Dolma 管线产物的通行组织 [6][9]；原子改名交付为数据工程防半截文件的标准手法。

### 3.11.3 输出示例

贯穿示例沿用 6.1 的输入法中文指令数据（`input.text_field = "instruction"`，用户 Schema 为意图标注三字段：`intent` / `topic` / `difficulty`，全部 required、`additionalProperties:false`），运行设定与数字全部沿用 3.10.4 走查（`run.output = "./out/ime-intent-0630.jsonl"`、`run.batch_size = 256`、`run.seed = 0`、`quality.threshold = 0.3`、`verify.enabled = false`；其 1000 行输入文件此处记为 `ime-2026-06.jsonl`）。

#### ① 主输出：meta_mode = "sidecar" 的一对行

`meta_mode = "inline"` 的完整行示例见 6.3，此处不重复。当 `output.meta_mode = "sidecar"` 且 `output.passthrough_fields = ["source"]` 时，主输出行为纯用户结构，`_meta` 逐行写入 `out/ime-intent-0630.meta.jsonl`，两文件以 `_meta.id` 与行序对齐（主输出第 k 行 ↔ meta 第 k 行）。下例对应输入 `ime-2026-06.jsonl` 首行 `{"instruction": "帮我写一条请假条，明天上午要去医院", "source": "ime-log", "ts": "2026-06-30T10:12:00Z"}`；每行写出前均经 `SchemaEngine.validate_only` 终检（3.11.1）。

```
# ── out/ime-intent-0630.jsonl 第 1 行（纯用户结构，无 _meta 键，剥无可剥）──
{"intent": "writing_assist", "topic": "请假条", "difficulty": "easy"}

# ── out/ime-intent-0630.meta.jsonl 第 1 行（实际为单行 JSONL，此处折行排版）──
{"_meta": {
  "id": "1cda030abc565f17",
  "run": {"tool": "labelkit/1.0.0", "started_at": "2026-07-02T10:27:41+08:00",
          "project_file": "project.toml", "rubric": "default:text", "seed": 0},
  "source": {"file": "ime-2026-06.jsonl", "line_no": 1,
             "generated_from": [], "fields": {"source": "ime-log"}},  // passthrough_fields 落点
  "stream": null,                                                     // v1.8 恒在键：未启用 segment = null（6.3）
  "scores": {"writing_style": 0.72, "facts_trivia": 0.44, "educational_value": 0.61,
             "required_expertise": 0.35, "__aggregate__": 0.53,       // 等权均值 = 2.12/4
             "mode": "pairwise_bt", "batch_no": 1},
  "dedup": {"kind": "unique"},
  "annotation": {"model": "qwen2.5-vl-72b-instruct", "attempts": 1},  // attempts=1：未触发 L3
  "verification": null                                                // verify 未启用
}}
```

v1.8 序列行说明：stream 工程（`segment.enabled = true`）的主输出行以 episode 为单位，其 `_meta.stream` 携带完整成员溯源与步骤序列（episode_id / session_id / member_ids / member_sources / steps 等，结构与样例见 6.3），行仍以 `_meta.id`（= 序列 Record id）对齐。

v1.12 members 块示例（摘自 `examples/mix` UI 主工程真跑主输出 `out/mix-labels.jsonl` 的一条 episode 行，实际为单行 JSONL，此处折行排版、长值截断；该工程 `frame.classify`（DeepSeek digest-only）与 `frame.annotate`（z.ai 视觉）双开、`[frame.class.transition.annotate].enabled = false`——index 4 成员即跳过类的 skipped 形态）：

```
{"task_label": "在美食外卖App上点一份黄焖鸡米饭", "app": "com.example.food",
 "summary": "在美食外卖App搜索并下单金牌黄焖鸡大份微辣米饭，支付¥38后等待送达。",
 "_meta": {"id": "eccc814c0694fb41", ...,
   "stream": {
     "episode_id": "eccc814c0694fb41", "session_id": "eccc814c0694fb41",
     "order_span": [1, 6], "member_count": 6,
     "member_ids": ["7cfb0c25f855b2d7", "164b7480ab098de5", ...],
     "member_sources": [{"file": "s1-food-order/uitree_1.jsonl", "pair_index": 1}, ...],
     "members": [                                     // ← member_sources 后、session_split 前（冻结位）
       {"index": 0, "id": "7cfb0c25f855b2d7", "label": "list_screen",
        "annotation": {"screen_role": "美食外卖首页",
                       "key_widgets": ["搜索美食", "搜索", "推荐餐厅", "金牌黄焖鸡 4.9 分", ...]},
        "status": "annotated"},
       {"index": 1, "id": "164b7480ab098de5", "label": "detail_screen",
        "annotation": {"screen_role": "菜品详情页",
                       "key_widgets": ["金牌黄焖鸡", "黄焖鸡米饭 ¥38", "月售 1200+ 好评率 99%", ...]},
        "status": "annotated"},
       {"index": 2, "id": "25ce67ce53d5f1d7", "label": "form_screen",
        "annotation": {"screen_role": "商品规格选择/加入购物车",   // [frame.class.form_screen.annotate]
                       "key_widgets": ["商品：黄焖鸡米饭 ¥38", "份量：大份", "辣度：微辣", ...]},
        "status": "annotated"},                        //  覆盖指令：抽取表单字段与取值
       {"index": 3, "id": "d77a51064a52f91e", "label": "confirm_screen",
        "annotation": {"screen_role": "订单确认页",
                       "key_widgets": ["确认订单", "黄焖鸡米饭 大份 ×1", "提交订单 ¥38", ...]},
        "status": "annotated"},
       {"index": 4, "id": "96cb96ed666583b1", "label": "transition",
        "annotation": null, "status": "skipped"},     // 帧类视图 enabled=false ⇒ 缺键推导 skipped
       {"index": 5, "id": "347864af1bc54006", "label": "confirm_screen",
        "annotation": {"screen_role": "支付成功结果页",
                       "key_widgets": ["支付成功", "订单号 FD20260812001", ...]},
        "status": "annotated"}],
     "session_split": false, "repaired": false, "degraded": null, "steps": null}}}
```

#### ② rejects = "refs"（默认）的一行

场景：第 213 行的标注输出经 L3 两次修复（`output.max_repair_attempts = 2`）仍未通过用户 Schema，M8 抛 `SchemaViolation`，记录置 `failed`（kind = `schema_violation`，7.6）转入 `out/ime-intent-0630.rejects.jsonl`。`errors` 即 M8 L2 `iter_errors()` 收集的全部违规（JSON Pointer 路径 + 期望 + 实际，3.8.2；违规描述为英文——枚举类由 M8 自渲染，其余直接携带 jsonschema 的原始消息，2026-08-14 起统一）；行内无任何数据内容——记录原文与 `raw_last_output` 仅在 `rejects = "full"` 时才写出。

```
# ── out/ime-intent-0630.rejects.jsonl 中的一行（折行排版）──
{"_meta": {
  "id": "c47d09e2b8a1f350",
  "source": {"file": "ime-2026-06.jsonl", "line_no": 213, "generated_from": []},
  "stage": "annotate",
  "reason": "schema_violation",
  "errors": [
    "/difficulty: expected one of enum [\"easy\", \"medium\", \"hard\"], got \"非常难\"",
    "/: Additional properties are not allowed ('confidence' was unexpected)"
  ]
}}
```

v1.7：classify 启用的工程中该 `_meta` 另含 `"label"` 键（3.11.2 rejects 行）；本例工程未启用 classify，无此键。v1.8：stream 工程另见三种 (stage, reason) 组合的 rejects 行——segment/noise、segment/below_min_len、verify/off_task_member（3.11.2）——refs 档形态同本例（仅 `_meta` 引用行；此类帧不写 item.errors，`errors` 恒 []）。

#### ③ 运行结束 stderr 摘要（逐字样例）

非 TTY、`--log-level info` 下的运行尾部。行格式为 7.3 的 `ts level stage batch msg`（日志正文自 2026-08-14 起统一英文，键名、结构与信息集不变）；数字即 3.10.4 走查：1000 条无坏行入流水线（4 批：256×3 + 232），尾批写出 184 行、失败 2 条；rejects 通道含重复 / 低质 / 失败三类（`output.rejects = "refs"`，3.11.2）。

```
2026-07-02T10:41:22+08:00 INFO  emitter batch=4 batch 4 flushed: main output +184 line(s) (total 811), rejects +48 (total 189)
2026-07-02T10:41:23+08:00 INFO  emitter batch=- finalize: fsync + rename  out/ime-intent-0630.jsonl.part -> out/ime-intent-0630.jsonl (811 lines)
2026-07-02T10:41:23+08:00 INFO  emitter batch=- wrote out/ime-intent-0630.rejects.jsonl (189 lines) and out/ime-intent-0630.report.json
2026-07-02T10:41:23+08:00 INFO  run     batch=0 run.end exit_code=0
   ── final summary (matches report.counts item by item) ──
   scanned=1000  ingested=1000  bad_input=0  generated=0
   dropped_dup=97  dropped_lowq=78  dropped_verify=0  failed=14  emitted=811
```

不变量自查（6.4）：emitted 811 + dropped (97+78+0) + failed 14 + bad_input 0 = 1000 = scanned 1000 + generated 0。尾批自查：184 + 28(dup) + 18(lowq) + 2(failed) = 232；尾批 rejects = 28+18+2 = 48，四批累计 189 = 97+78+14。

#### ④ 原子改名交付时间线

运行全程只向 `out/ime-intent-0630.jsonl.part` 追加（每批 flush）；finalize 时 fsync 后一次 rename 为 `out/ime-intent-0630.jsonl`。因此目录中任一时刻要么只有 `.part`（运行中，或未走到 finalize 的硬崩溃 / 输出路径不可写），要么只有最终文件——目标文件名出现即保证**已交付的每一行完整且合法**，永远不会读到半截行。v1.6 起熔断中止同样交付（3.10.3 熔断交付），「目标文件出现」因此不再等价「全部输入处理完毕」：消费方判定运行完整性须看 report.run：`interrupted=false` **且** `circuit_broken=false`（退出码 0/1 不足——被 SIGINT 优雅中断的运行同样交付且以 0 退出，3.10.3 中断行）；熔断交付的主输出是「已完成批的完整前缀」，缺口可由 counts.unprocessed 核对（6.4 不变量扩展）。
