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
