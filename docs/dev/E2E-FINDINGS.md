# E2E 测试发现的系统问题清单

> 2026-07-03，撰写用户手册期间对 LabelKit 1.0.0 做的端到端测试所发现的问题。
> 测试面：三个示例工程真实 LLM 全流程（text / ui / generate）、validate/--probe、--dry-run、
> --limit/--strict、坏密钥、坏输入路径、坏配置等错误路径，以及 116 个审计智能体对
> 手册与实现逐条核对时顺带发现的行为事实。按严重程度排序。

## P1 — 实现与规格的偏差

### 1. `provider_retryable_exhausted` 未计入熔断窗口（spec §7.6 偏差）—— ✅ 已修复（2026-07-03）

**现象**：spec §7.6 规定重试耗尽（`provider_retryable_exhausted`）应「记录 failed，**计入熔断窗口**」。实现中 `llm_client.py` 的 `_post_with_retries()` 重试耗尽分支（约 :643-651）只发 trace 事件并抛 `ProviderRetryableError`，**从不调用** `_record_provider_result(fatal=True)`；全仓库也没有其他地方把该错误计入熔断。

**后果**：端点持续性故障（所有请求 5xx/超时）时熔断永不触发——每条记录都要烧满 `max_retries` 次重试再失败，运行以最慢、最贵的方式爬完全程，最后 exit 0（除非 --strict）。熔断「对坏端点快速止损」的设计目标在这类故障下失效。

**建议**：在重试耗尽分支补 `self._record_provider_result(fatal=True)`（与 401/403 分支对齐）。

### 2. 「无任何合法记录」未按 spec §2.4 触发退出码 3 —— ✅ 已修复（2026-07-03）

**现象（修复前）**：spec §2.4 将「无任何合法记录」列入退出码 3 的触发条件，errors.py 与 CONTRACTS.md §InputError 也有同样表述，但实现缺该检查：`text_field` 全员不命中（默认 `on_bad_line="skip"`）时运行照常收尾——实测 `scanned=2 ingested=0 bad_input=2 emitted=0`、退出码 0。

**修复**：按 spec 实现于 M2（ingest 拥有输入级合法性校验）：`Ingestor.records()` 流耗尽时若 `ingested == 0` 抛 `InputError("无任何合法记录: …（scanned/bad_input/missing_pair/index_conflict 计数）")` → 退出码 3。覆盖全坏行、空文件、UI 全缺对/全冲突四种形态；只要有一条合法记录则行为不变。新增 4 个单测 + 更新 4 个原「空流不抛错」的单测（tests/test_ingest.py），CLI 级实测确认 exit=3；手册第 5/15/18/19 章已同步回改为新行为。

## P2 — 符合规格但伤用户的锐边

### 3. 认证失败 + 记录级隔离 ⇒ 静默全灭、exit 0 —— ✅ 已修复（2026-07-03：401/403 首错立即熔断 → exit 4；spec §3.9.3/§7.6 已同步）

**现象**：坏 API key（z.ai 返回 401 → `provider_fatal`）实测：14 条输入全流程跑完，`failed=13`（1 条先被 dedup 拦下）、主输出 0 行、**退出码 0**。原因：记录级隔离下每条记录在 quality 阶段首次调用失败即标 `failed`，后续阶段不再碰它——连续致命错误计数最多只能攒到「存活记录数」，14 条数据永远够不到默认阈值 20。

**后果**：小批量/试跑场景下，密钥、权限、模型名错误表现为「运行成功但什么都没产出」，只有翻 rejects 或 counts 才能发现。

**建议**：认证类致命错误（HTTP 401/403）不该按「连续 N 次」熔断——第一次出现就几乎不可能自愈，建议直接触发熔断或大幅降低此类错误的阈值。手册侧已用「先跑 `validate --probe`」缓解（第 2、15 章）。

### 4. trace 文件在启动瞬间被截断 —— ✅ 已修复（2026-07-03：EventLog 惰性打开 + run.start 前先输入扫描；dry-run 的报告写 {stem}.dryrun.report.json、trace 写 {stem}.trace.dryrun.jsonl）

**现象**：`EventLog` 启动即以 `"w"` 打开 trace 路径。实测一次 `--input ./no-such-dir`（未指定 --output）的运行在打印 `InputError` 退出前就把上一次成功运行的 trace 截断成了 2 行。`--dry-run` 同样截断 trace 并覆盖 report.json。（report.json 仅在 finalize 写出，「秒败」运行不会覆盖它；dry-run 会。）

**后果**：一次手滑的失败命令就销毁了上一轮的调优底账（trace 是 rubric 迭代的核心原料）。虽有 WARN 提示「already exists — truncating」，但打印时已经截断了。

**建议**：trace 打开推迟到 M1+输入扫描通过之后；dry-run 的 report/trace 写独立文件名（如 `{stem}.dryrun.report.json`）。

### 5. json-repair 会把「未转义内引号」修复成合法但被截断的内容 —— ✅ 已缓解（2026-07-03：L1 有损修复启发式——re 序列化长度损失 >20% 且 >40 字符时 stderr warn + trace schema.repair 事件带 l1_lossy 标记；根因在 LLM 输出侧，无法在修复层完全消除）

**现象**：examples/ui 运行的 verify 环节，judge 输出的 critique 文本含中文引号内嵌英文引号（如 `页面标题"登录"…`），L1 的 `json_repair` 把字符串在内引号处截断，产出**结构合法但语义残缺**的 JSON——trace 里实测两处：`"opinion": "页面标题"`、`"opinion": "screen_category 为 settings 语义正确；page_title 为"`。全程无任何告警，校验通过。

**后果**：`policy="repair"` 时被截断的批评意见会回喂标注模型，修复质量打折；trace 审计材料失真。

**建议**：内部 Schema 的自由文本字段（reason/opinion）可加 minLength 或对 L1 修复命中且文本骤短的情况发 warn；提示词侧可要求评审「意见文本中不要使用引号」。

### 6. 温度 0 下 pointwise 打分跨运行明显漂移 —— ⏸ 延期（服务端非确定性非工具可消除；「n 次采样取中位数」属算法级新特性，须按 §1.6 流程与需求方对齐后立项；手册已在引用数字处加波动提示）

**现象**：同配置、同 seed、同输入的相邻两次运行（examples/text，threshold=0.25）：`dropped_lowq` 在 5↔8 之间翻转（13 条中 3 条阈值附近记录逐次进出）；`facts_trivia` 均值 0.06↔0.22。temperature=0 不能消除 glm-5.2 服务端的非确定性，而 pointwise 单次打分对措辞极敏感。

**后果**：阈值附近的门控结果不可复现；「可复现性」承诺只覆盖流程路径，不覆盖 LLM 判分本身（spec 已如此声明，但实际波动幅度值得知晓）。

**建议**：产品侧可考虑给 pointwise 增加「n 次采样取均值/中位数」选项（对应 pairwise 已有的 both_orders 思路）；手册已在引用具体数字处加了波动提示。

## P3 — 配置体验的坑

### 7. `threshold` + `top_ratio` 同设时可能被静默忽略 —— ✅ 已修复（2026-07-03：selection=threshold 且设 top_ratio 时 M1 打 warning）

互斥校验位于 `if quality.selection == "top_ratio":` 分支内（loader.py 约 :840-846）。用户设置了 `top_ratio` 但忘了把 `selection` 改成 `"top_ratio"` 时：装载成功、零警告，`top_ratio` 被静默忽略。建议：`selection="threshold"` 且 `top_ratio` 非空时打 warning。

### 8. `verify.judges` 非空时仍强制要求 `verify.llm` 指向存在的 profile —— ✅ 已修复（2026-07-03：judges 非空豁免该校验；spec §5.2 脚注已同步）

loader（约 :802-803）在 `verify.enabled` 时无条件校验 `verify.llm`（默认 `"judge"`）存在于 `[llm.*]`——即便配置了 judges 评审团、`verify.llm` 根本不会被使用（`referenced_profiles()` 里 judges 非空即替代它，probe 与密钥检查都跳过它）。用户必须定义一个用不到的 `[llm.judge]` 才能过校验。建议：judges 非空时豁免该校验（与 quality.judges 的语义对齐）。

## P4 — 观测性小项

### 9. pairwise 模式下 `report.quality.per_criterion_mean` 恒 ≈ 0.5 —— ✅ 已修复（2026-07-03：report 只增字段 per_criterion_tie_rate——每准则平局率，判定失败按平局计；spec §6.4 已同步）

批内百分位的均值按构造恒为 0.5（examples/ui 实测四条准则全是 0.500）——该指标对 pairwise 无信息量，却和 pointwise 的有意义均值共用同一字段。建议：pairwise 下省略或改报 tie 率。

### 10. 熔断时 `report.run.interrupted` 保持 `false` —— ✅ 已修复（2026-07-03：report.run 只增字段 circuit_broken；spec §6.4 已同步）

`interrupted` 仅由 SIGINT/SIGTERM 置位；熔断只体现在 `exit_code: 4` + 主输出不交付。语义上容易误读（「中途死了却说没 interrupted」）。建议：report 增加显式的 `circuit_broken` 字段。

---

### 测试留痕

- 三个示例工程均 exit 0 且账目守恒：text（14→8 emitted）、ui（6 scanned→4 emitted，跨子目录配对/孤儿树/精确重复三机关全部按预期处理）、generate（0 输入→8 emitted，桶统计正确）；
- `validate --probe`、`--dry-run` 成本估算、`--limit`、`--strict`（exit 1）、退出码 2/3 错误路径全部符合规格；
- 离线测试套件 527 passed。

---

## 附注：v1.6 密钥池与熔断交付下的既有修复语义（2026-07-03）

v1.6（多 API Key 负载均衡 + 熔断交付，spec 3.9.3/3.10.3）触及本清单中 P1-1、P2-3、P4-10 的实现位点，语义按如下方式保持并推广——「不回退」的精确含义：

- **P1-1（重试耗尽计入熔断窗口）**：重试耗尽仍 `record_provider_result(fatal=True)`；v1.6 新增的驻留超限（`run.max_park_s`）走同一路径，同样计入。轮换后的耗尽意味着调用在其触及的每把密钥上都失败过——计数含义只强不弱。
- **P2-3（认证类首错立即熔断）**：按密钥泛化——401/403 先禁用该密钥（`llm.key_disabled` WARN），池内尚有存活密钥时同一尝试立即换 key 重发（不耗重试、不喂熔断）；**最后一把存活密钥**被禁用时立即硬熔断。池大小 1 时逐位还原 v1.5 行为（首个 401 → exit 4）；其逆命题同时成立：一把被吊销的 key 不再杀死仍有健康 key 的运行。集成测试 `tests/integration/test_key_pool_llm.py` 以真实 401（故意无效的 key 值）覆盖两个方向。
- **排队调用熔断复查（评审工作流加固项）**：信号量后逐尝试 `_check_breaker` 保留；驻留期间每 ≤60s 分片同样复查。
- **P4-10（circuit_broken 字段）**：仍恒在；v1.6 熔断改为**交付**已完成批，report 另增 `partial_delivery`（仅熔断交付时出现）与 `counts.unprocessed`（差额，守恒等式左侧扩展项）——「熔断 = .part 不交付」的旧断言已随 spec 修订废止（tests/test_orchestrator.py 两处断言同步反转）。

测试留痕（v1.6）：离线 596 passed（新增 21 个密钥池纯逻辑测试 + 2 个熔断交付断言反转）；集成 21 passed（新增 4 个：别名轮换、坏 key 吸收轮换、全池坏 key 硬熔断、逐 key probe_all）；examples/text 真实全流程 exit 0，健康运行 report 结构与 v1.5 逐键一致（无新增字段）。

---

## 追加条目：v1.7 可行性审查发现（2026-07-07）

### 11. report 的 generate.buckets 字段白名单漏 `rejected_by_validator`（spec §6.4 / CONTRACTS §9.3 偏差）—— ✅ 已修复（2026-07-07，随 v1.7 实现顺带修复）

**现象**：v1.5 引入的桶计数器 `generate.buckets.<key>.rejected_by_validator`（`generate.py` 在配置 `generate.sample_validator` 时零初始化、逐违规累加）从未到达 report.json——`orchestrator._build_report` 解析桶计数器时按字段名白名单过滤，白名单只含 `calls` / `produced` / `survived_dedup` 三项，第四个字段被静默丢弃，与 spec §6.4 / CONTRACTS §9.3 的承诺相矛盾。发现于 v1.7 分类算子的 fan-out 可行性审查（inline 复核承重事实时比对 orchestrator 白名单与 generate 计数点）。

**后果**：配置了 `generate.sample_validator` 的运行里，回调的实际拦截量在报告中恒不可见——桶统计貌似完整实则少一列，validator 拦截率无法从 report.json 审计（trace 事件与 stderr 不受影响，但 report 是唯一的结构化台账）。

**修复**：`orchestrator._build_report` 桶字段白名单补入 `rejected_by_validator`；零初始化语义保持——三个恒在字段不变，第四字段仅在计数器出现（即配置了 validator）时写入。回归测试 `tests/test_orchestrator.py::test_generate_bucket_whitelist_includes_rejected_by_validator`（先红后绿）覆盖「有 validator 桶带第四字段、无 validator 桶保持三字段形状」两个方向。

---

## 追加条目：v1.8 E2E 发现（2026-07-14）

### 12. `_meta.run.rubric` 对 `default:trajectory` 回落为模态默认（§6.3 偏差）—— ✅ 已修复（2026-07-14，v1.8 合入前）

**现象**：emitter 的 `_rubric_selector` 白名单元组仍为 v1.7 的 `("default:text", "default:ui")`——工程显式配置 `quality.rubric = "default:trajectory"`（或 stream 模式下留空经 loader S29 规则解析为 trajectory）时命中兜底分支，`_meta.run.rubric` 被写成 `default:{modality}`（examples/stream 实跑记为 `"default:ui"`，与实际打分准则不符）。发现于手册 25 章重同步（样例块与产物逐字核对时比对 `_meta.run` 块）。

**后果**：仅溯源字段失真（打分本身用的是正确的 trajectory rubric——loader 解析与 M4 消费不受影响）；但 `_meta.run.rubric` 是行级审计的 rubric 依据，stream 工程的主输出全部行携带错误值。

**修复**：`emitter._rubric_selector` 白名单补 `"default:trajectory"`，空串兜底分支镜像 loader rule 16 的 S29 规则（`segment.enabled` ⇒ trajectory）。回归测试 `tests/test_emitter.py::test_rubric_selector_trajectory`（先红后绿）覆盖显式选择器与 stream 空串解析两个方向。手册无受影响样例（25 章刻意未引 `_meta.run` 块）。

### 13. v1.8 合入前对抗代码评审的七项发现（D1–D7）—— ✅ 全部修复（2026-07-14，v1.8 合入前）

合入前的对抗评审（八攻击面 + 离线 probe 复现）确认 stream 链核心算法（滑窗缝合/成段/②b/两阶段手术/守恒代数）无缺陷，但在观测面契约、会话身份与修复归因上查实 7 项（2 medium / 5 low），全部修复并补回归：

- **D1（medium）`ingest.disorder` 逐事件 stderr 镜像违反「全运行仅一次」契约并外泄输入值**：镜像行携带 reason 内的时间戳/游标值且每记录一条——时间戳字段系统性坏掉时 stderr 被输入派生值淹没。修复：事件改 trace-only（obslog 镜像表删行），M2 自身保留一条 data-free 的全运行单次 WARN；spec §7.2/CONTRACTS §8.1 同步。
- **D2（medium）内容派生 `session_id` 碰撞致批内不同会话被 M14 静默合并**：帧 id 是内容哈希且 stream 下帧不判重，字节级相同的重复行被 max_len 切分即产出同 id 会话、同批装箱后被按 id 归组合并。修复：M2 闭会话时维护每运行重复序数，碰撞时折入哈希（首次出现保持原派生，正常流 id 稳定不变）；回归测试钉住同内容双会话 id 相异且跨运行确定。
- **D3（low）`--limit` 恰耗尽于流末时误发「被截断」WARN**：恰好耗尽与真截断不可区分（消歧需多拉取一条记录、扰动 scanned 台账）。裁决为语义澄清：cause="limit" 定义为「预算耗尽处闭合」、WARN 文案陈述预算耗尽而非断言截断；spec 3.2.8/CONTRACTS §7.1 钉死。
- **D4（low）`verify.defects.<kind>` 只计终局缺陷表**：被成功修复的缺陷从报表消失，与 membership_repairs 的路由时计数口径自相矛盾（可出现「手术数 >0 而缺陷计数全 0」）。修复：改在每轮评审定案时计数（修复掉的缺陷仍入直方图）。
- **D5（low）同轮争帧误标 `suspected="capture_gap"`**：位次序在后的 episode 查到的候选帧已被前序 episode 预定时跳过了三级判定的第二级——终局该帧确实被邻段回收，"采集缺口"属事实性错误标注。修复：claimed 帧按「邻段持有」判 mark-only。
- **D6（low）multi 扇出克隆丢 `session_split`/`segment_degraded` duck 标**：同 episode 兄弟行的 `_meta.stream` 自相矛盾（会话属性非信封属性）。修复：`_fan_out` 复制两标（连同 v1.8 audit 补的 session_id 继承一并回归覆盖）。
- **D7（low）文本 input_order 下「换文件即断会话」未入规范**：行为正确（line_no 顺序语义不跨文件）但 spec/302 闭合条件枚举与 CONTRACTS §7.1 未登记、cause="key" 在 `stream.key=[]` 时无从解释。修复：两处文档登记（含 meta:* 下文件边界透明的对照句）。

测试留痕：离线 1015 passed（较评审前 +5：D2/D6 新回归 + D1/D3/D4/D5 断言修正）；集成 28 passed（真实端点，评审前已跑）。

---

## 追加条目：示例重组 E2E 发现（2026-07-17）

### 14. glm-5.2 温度 0 下 segment 边界判决跨运行漂移（穿插流场景）—— ⏸ 数据侧缓解（context 语义边界句），同 #6 根因

**现象**：examples 按输入格式重组为三工程后，`examples/stream`（53 帧五会话，segment window=16 + stitch）连续五次真跑中两类边界判决出现跨运行翻转：① s1 会话（外卖 8 帧 + 打车 5 帧背靠背 + 桌面尾帧）在早期 context 措辞下偶发整会话 all-continues（14 帧并成 1 个 episode，噪声帧 5 未剔除）；② s4 会话的支付尾帧（409，与 404 的网购实体跨 4 帧呼应）在「背靠背串联」语义句加入后反而被判 `advances`（并进新闻段——该句的逆命题「切 App 且有实体延续 ⇒ 非新流程」被模型采信）。同 seed、temperature=0、同输入。

**后果**：s1 翻转时 emitted 8↔9、s4 翻转时救援路径整条消失（rescued_short 1↔0、stitched 3↔4，错并的新闻线索被 verify 以 label_mismatch 拦截丢弃）——运行级账目守恒恒成立，但穿插流工程的逐场景验收数逐次运行可能不同。

**处置**：根因与 #6 同源（服务端非确定性 + 判决对措辞极敏感），工具侧无案。数据侧缓解落在 `[segment].context` 的三个语义边界句（现 examples/stream/project.toml）：「一笔任务已办完之后紧接着开始的另一笔是新流程」（治 s1 串联欠分割）、「advances 仅适用于当前这一笔进行中的任务；与几帧前被打断任务的实体呼应属切回收尾（context_switch）」（治 s4 尾帧误吸附）。加句后验证运行（run 5）五场景全部命中设计预期（2/2/3/2/0 线索、stitched=3、rescued_short=1、seams=4、错缝 0）；手册 25/26 章样例即取该次运行，并在 26.5 记录了这组措辞的调参因果。残余风险：漂移不能排除，逐场景数字仅作参考锚——两章均保留「照跑数字会有浮动、守恒恒成立」的提示。

---

## 追加条目：v1.11 P6 集成实测（2026-07-23）

> spec v1.11「上下文预算」P6 阶段（SPEC-context-budget.md §3.9）对真端点
> `https://api.z.ai/api/anthropic`（anthropic 协议，glm-5.2）的实测记录。
> 新增集成测试文件 `tests/integration/test_budget_llm.py` 把 #15/#16 的采集
> 钉成永久断言。

### 15. z.ai 超窗形态实测：200 + `model_context_window_exceeded`，非 400（V20/[C-81] 闭合）

**实测**：对未声明 `context_window` 的 profile 构造必超窗请求（est ≈ 1.9M token
的 CJK+ASCII 重复文本，实际 1,333,338 GLM token），端点返回 **HTTP 200**，响应体
逐字（省略 id/request 标识）：

```json
{"id":"msg_…","type":"message","role":"assistant","model":"glm-5.2",
 "content":[{"type":"text","text":""}],
 "stop_reason":"model_context_window_exceeded","stop_sequence":null,
 "usage":{"input_tokens":0,"output_tokens":0,"cache_read_input_tokens":0}}
```

即 [C-57] 双协议 200 形态——**V24 的 finish 终局化路径正好接住**（抛
`ContextOverflowError(phase="reactive", origin="finish")`，交互本身 ok、不喂
熔断，§7.8 矩阵实测吻合）；超窗拒绝**零计费**（usage 全 0）、数秒返回。
**本端点不存在 400 形态的超窗**——V20 的 400 错误体嗅探在此无触发面，
`_OVERFLOW_BODY_PATTERNS` **无需增补**（且事后验证：该 200 响应体若真以 400
出现，现有 pattern `context_window_exceeded` 已能命中）。声明大而错的窗口
（10M，precheck 放行）时同请求经 `complete()` 全链路抛 reactive，熔断连击
恒 0——集成断言已钉（test_budget_llm.py）。A7 的 reactive-400 终局补喂在本
端点不可达，维持离线钉板（tests/operators/test_annotate.py）。

### 16. 实效窗实测：裸 glm-5.2 = 2^20 = 1,048,576（input+max_tokens 合并判定）；`[1m]` 后缀不存在（V26）

**实测**（阶梯 + 精确二分，全部 max_output_tokens=16）：est 131,072（V26 锚，
集成测试常驻）与 est 400,000 两档接受；实际 1,010,670 / 1,033,338 /
**1,048,554（+16 输出 = 1,048,570）接受**；**1,048,566（+16 = 1,048,582）**、
1,066,662、1,333,338 拒绝——阈值 = **`input_tokens + max_tokens ≤ 1,048,576`**
（接受/拒绝夹逼区间宽 12 token，2^20 为区间内唯一整值；input 单独 1,048,566
< 2^20 仍被拒证明 max_tokens 计入合并判定），与 [C-58] 语义一致。模型名 `glm-5.2[1m]` 被拒：HTTP 400
`{"type":"error","error":{"type":"invalid_request_error","code":"1211",
"message":"[1211][Unknown Model, please check the model code.][…]"}}`——
[C-59]「1M 须 `[1m]` 后缀」在本端点**不成立**（裸名即 1M 窗）。
**examples/config.toml 的 131072 声明维持不上调**：欠声明恒安全（V26），实测
窗大 8 倍只意味着更宽的富余；上调会改变 w_min/dry-run goldens，收益为零。
附带测得估算器保守率：该 CJK+ASCII 混排上 `est_text ≈ 1.44×` 实际 GLM
token（CJK 实测 ≈ 0.667 t/字，V8 预期方向）。

### 17. 校准首批偏差实测：先验 1882 → 收敛 206/79（V19）

examples/stream UI 工程真跑 `report.budget.image_cost`：**default = 206、
judge = 79**（先验 = anthropic patch 公式 @2048 最坏正方形 1568 × 1.2 =
**1882**，首批高估 ≈ 9×——400×800 PIL 合成截图的实际计费远低于最坏先验；
judge 值更低是 est_text 对文本重 prompt 的保守高估把图片残差压小，属 max
滤波 + 0.85 折扣设计内）。examples/ui 工程 judge 样本数 < 8（CALIBRATION_MIN_SAMPLES）
→ `image_cost.judge` 维持先验读数 1882（[C-32] 门槛语义正确在报表可见）。
集成测试以 64×64 PNG 双真调用钉住样本算式 `cost = ceil(max_sample/0.85)`
与批冻结确定性（test_budget_llm.py::test_image_cost_calibration_converges…）。

### 18. `validate --probe` 被 V11 终局化误伤（P6 发现的真 bug）—— ✅ 已修复（2026-07-23）

**现象**：probe 子客户端把 `max_output_tokens` 钳为 1，anthropic 协议下任何
健康响应必然 `stop_reason="max_tokens"` → V11 终局化抛 `OutputTruncatedError`
→ `_probe_one` 的兜底 except 记 `ok=False`——**健康密钥/端点的 probe 全红**
（集成测试 test_key_pool_llm.py::test_probe_all… 首跑即红）。F13「probe 平凡
通过」只考虑了 precheck，漏了 finish 终局化。

**修复**：`_probe_one` 对 llm probe 捕获 `OutputTruncatedError` 记 ok——1 token
probe 写满输出上限正是活性证明（鉴权通过、模型应答、usage 在场）；spec
3.9.4 probe 语义不变，属实现勘误，无 spec 修订面。

### 19. classify 集成测试的「截断逼出 SchemaViolation」诡计随 V11 失效 —— ✅ 已适配（2026-07-23）

`test_classify_llm.py` 原以 `max_output_tokens=2` 逼真端点产出不可解析 JSON、
穿 L1–L3 耗尽成 SchemaViolation 来钉 `on_error="fallback"`。V11 终局化后同一
响应（`stop_reason="max_tokens"`）不再进修复环——V27① 分类器按
`output_truncated` 记录级 reject 且**绕过 fallback 类**（spec 3.13.4 v1.11 行）。
测试改钉新处置（failed + kind=output_truncated + fallback 未启用 +
overflow_records 不误计）；fallback-on-SchemaViolation 路径由离线
tests/operators/test_classify.py 继续覆盖。行为变化本身是 v1.11 已裁决项
（V11 终局化，不设开关），非偏差。

### 20. 首例真跑 `output_truncated`：quality pointwise 写满 4096（同 #6 根因家族）

examples/stream UI 工程本轮真跑 failed=1：s1 首帧 episode 的 pointwise
`noise_residue` 判分调用把 4096 输出上限写满（glm-5.2 长推理漂移），v1.11 按
`(quality, output_truncated)` 记录级 reject（v1.10 会把截断 JSON 交给修复环
「硬修」）。run 照常 exit 0、守恒成立——新 rejects reason 首次在示例真跑落地。
逐次运行未必复现（同 #6 服务端非确定性）；手册 25/26 章重采时若复现属预期。

### 21. 并发同名输出 stem 的碰撞形态（P3 锐边，观察一次）

P6 期间一次环境性重复启动（同一 `project-synth.toml` 两进程并发）实测出的
碰撞形态：后启动进程以 `"w"` 打开同名 `.part` **截断了先启动进程已写的同
inode**，先启动进程 rename 交付的「主输出」实为后进程内容；后进程 finalize
时 `.part` 已被改名 → `FileNotFoundError` → exit 4。单进程语义下无锁属设计
内（spec §2.6 无跨运行状态），但「第二进程毁掉第一进程的交付物」值得记录；
建议（roadmap 级）：`.part` 以 `O_EXCL` 打开，并发同 stem 快速失败。清理重跑
（单进程）exit 0 正常。

---

### 测试留痕（v1.11 P6）

- 集成套件（真端点）：**38 passed**（新增 test_budget_llm.py 6 例；
  test_verify_llm 的语义断言出现过一次 #6 家族漂移红、复跑两次全绿）；
- 合入前复核（2026-07-23，对抗评审 6 项修复后）：离线套件 **1417 passed**
  （+18 例）；集成全套件复跑一次 36+2——两例 #6 家族漂移红
  （test_l25_unsatisfiable_hook…、test_defect_verdict_schema_roundtrip），
  单测复跑即绿；后者同时暴露测试侧陈旧词表（本地 DEFECT_KIND_VOCAB 缺
  v1.9 闭合词表第六枚 `wrong_stitch`，模型合法漂移到该枚即假红）——已补齐，
  该漂移面消除；
- 离线套件：**1399 passed** 不动（probe 修复与测试登记后复跑确认）；
- 五条示例命令全部 exit 0：text 14→15 emitted（generated 12）、text-synth
  0→8、ui 6 scanned→4 emitted、stream UI 53 帧→8 emitted、stream-text
  13→1 emitted；
- stream UI 工程（vision 翻转后首次验收）：`segment: w_min=46 window=16
  (budget)` 启动行（46 = vision 分支先验算值；文本分支应为 214，见
  stream-text 报表 w_min=[8, 214]——两值互证 vision_resolved 生效）、
  `stream.windows = 5`、`report.budget` 节全键在场（见 #17）、守恒全展开式
  实测 8+0+0+0+1+0+8+45+4 = 53+0+0+13 = 66 ✓、`threads = episodes −
  stitched = 13 − 4 = 9` ✓、rejects 9 行（8 noise + 1 #20）下 exit 0
  （strict 语义不变）。
