# 通用推理与返工修复审查

审查日期：2026-09-20。原主线为 `5a359cacd9927ad40eb0b8b96cc22799d721a069`，
对照候选为 `e8f168fe32436f7e6bce1660698c05e488ab7314`。
先确认表现，再审查候选方案及更小实现；不直接搬入候选提交。

## 问题与方案

| 表现 | 旧实现证据 | 候选评估 | 最终决定 |
|---|---|---|---|
| 单次请求可以超过 timeout_s | HTTPX 只限制各网络阶段无进展时间；真实 request hook 阻塞后只能由外部 watchdog 终止 | 在取得 origin 许可后包 asyncio.timeout 的边界正确，独立异常处理重复已有逻辑 | 在 post 外增加总截止，复用既有重试、日志、记账与清理；保留 PoolTimeout 和外部取消语义 |
| 顶层非对象 JSON | 两种 provider 的数组、null、字符串、数字、布尔共十种输入已被旧 parser 归为可重试错误 | 新 Mapping 分支只改变错误文字，没有修复新的行为缺陷 | 不加入重复生产分支；保留不泄漏响应内容的回归 |
| maxItems 错误重复数组 | 旧违规说明包含完整数组，修复提示又包含原候选，同一内容出现两次 | 候选的数量说明已是最小改动 | 只报告最大数量与实际数量，保留完整原候选一次 |
| 普通记录修复失去原始依据 | process annotation 未保留首轮请求；stream 已保留 | 直接扩大 complete_evidence 还会改变 overflow 归因，并让原本允许的无视觉修复模型收到图片 | 分离提示词保留与容量错误传播；普通记录保持 SchemaViolation，新增必要的 UI repair vision 校验 |
| 多轮返工丢失早期批评 | classic 和 stream 的 fail_critiques 每轮覆盖；接缝刷新也会覆盖历史 | 候选纳入 pass 意见且遗漏 stream | 按轮次、评委顺序累计 fail 意见，接缝意见追加；当前候选和独立复审规则保持 |

普通记录原来的紧凑修复是明确写入旧契约的行为。本次将保留原始依据作为契约修订，先同步
`spec/305-m5-annotate.md`、`spec/308-m8-schema-engine.md` 和 `docs/CONTRACTS.md`，
再实现；没有把旧契约与新要求混为同一件事。

`retain_repair_prompt` 是内部调用范围字段，不是新用户配置。处理流的 `complete_evidence`
继续保留完整证据并上抛容量错误；普通记录保留首轮实际请求，但仍在 M8 按原规则归类修复容量失败。
生成路径的独立修复协议保持。累计的批评可能更早触发既有容量边界，不能通过裁剪批评绕过校验。

候选生产差异为 4 文件、增加 39 行、删除 23 行；本轮为 6 文件、增加 39 行、删除 22 行。
总文本规模基本相同。更小的实现主要体现在 M9：复用现有异常路径，省去候选新增的重复响应检查，
该文件从候选的增加 18 行收敛为增加 7 行。额外文件用于补齐 stream 历史和必要的 UI 视觉能力校验，
内部提示词开关则避免扩大普通记录的容量错误语义。

候选的 localhost 服务返回伪造模型响应，与仓库禁止模拟模型服务的规则冲突，没有采用。
离线超时测试使用真实 HTTPX request hook 在发包前阻塞，真实 asyncio 截止取消它；不创建模拟服务、
替换 transport 或返回伪造响应。网络端到端证据单独来自真实 z.ai/DeepSeek 测试。

## 权威要求与验证入口

| 要求 | 规范 | 回归入口 | 状态 |
|---|---|---|---|
| HTTP attempt 总截止、排队排除、独立重试、取消和 PoolTimeout | spec/309 单次请求超时；CONTRACTS M9 | test_llm_client 的 deadline/cancellation/pool 用例 | 窄测通过 |
| 非对象响应继续可重试且不泄漏内容 | spec/309 provider 故障分类 | test_non_object_provider_json_is_already_retryable | 原主线十例通过 |
| maxItems 数量说明及原候选只出现一次 | spec/308 L2/L3 | TestRepairPrompt 的 max_items 用例 | 旧实现失败，修复后通过 |
| 原始 prompt 保留与普通/stream overflow 分离 | spec/305 process 标注；spec/308 L3 | retained_repair_prompt、complete_evidence 用例 | generic/finalized 与 http_400/finish 矩阵通过 |
| UI repair profile 能力在启动时校验 | spec/305；CONTRACTS M8 | test_config 的 nonstream_ui_repair_llm 用例 | 旧实现失败，修复后通过 |
| classic、stream、成员手术与接缝刷新保留历史 fail 意见 | spec/307 修复环；CONTRACTS M7 | test_verify 的 history/classic/stream/seam 用例 | 最终六文件 1048 项回归通过，含 stream 混合投票 |
| 实际 L3 请求发送原始上下文 | spec/305、spec/308 | test_annotation_repair_context_llm | 真实 z.ai 通过 |

## 验证记录

- 改动前四个相关测试文件：`578 passed`。
- 新 deadline 回归在旧生产代码失败；非对象响应十例在旧生产代码通过。
- Schema/config/contracts 完整窄回归：`569 passed`；六个相关测试文件的父级回归：`1043 passed`。
- 补齐 stream 成员手术和接缝刷新后，六文件回归：`1047 passed in 5.73s`。
- 完整离线覆盖门：`3573 passed, 57 deselected in 671.33s`。57 项为命令明确排除的真实集成测试。
  12/12 修改或文档变化函数均进入；6 个修改生产文件最低行覆盖 89.88%、最低分支覆盖 78.46%。
- 变异首轮发现重试预算翻倍没有被测试识别。补强 request hook：超过配置预算的 1.5 倍尚未被取消就失败，
  每次取消也检查没有提前耗尽预算；它运行真实 asyncio 和 HTTPX，仍不发送请求或模拟模型响应。
  补强后六文件回归 `1047 passed in 5.65s`，生产代码与完整覆盖门逐字节一致。
- 复审又发现 stream 混合投票缺少 pass 意见隔离的断言。新增实际 driver 用例，以 fail/pass/fail
  首轮触发返工，验证完整报告保留全部意见、返工只含按评委顺序排列的 fail 意见、第二轮复审不含历史意见。
  新测试绿色，隔离的错误实现被该断言杀死；最终六文件回归 `1048 passed in 5.37s`。
  两次补强仅修改测试。完整离线门、真实端点测试及最终审查使用的生产代码完全一致。
- 真实 M9 z.ai 客户端测试：`6 passed in 28.98s`，没有跳过。
- 新增真实 annotation L3：`1 passed in 11.49s`。观察实际 HTTP 请求，首轮与修复均包含原始记录，
  system 与原消息前缀相同，最终两个真实模型调用、非零 usage、`l3_1=1`。
- 该 live 用例最初两次失败，错误为 enum SchemaViolation；定位为复用的旧测试配置未填入冻结后的
  model Schema，有效 Schema 为 `{}`。修正测试配置并启用真实结构化输出后通过；没有放宽生产 Schema。
- sequence 示例的无凭据 validate 与 dry-run 均通过：2 个反事实集合、8 条主序列、22 个主事件，
  加 2 条 noise 和 3 条 replay，共 27 条 stream 行；没有把 dry-run 当作模型执行证据。
- 设计 HTML 与 186 页 PDF 已重新生成；视检第 51、65、69、70、71、72、73、77 页，
  修正文档代码块围栏后无文字裁切、重叠或原始围栏泄漏。
- 最终改动涉及 6 个生产文件、12 个修改或文档变化的函数，符合文件、函数、参数及行长度限制。
- 真实 sequence 套件：`5 passed, 1 failed in 270.99s`。失败节点是
  `test_cross_view_one_shot_retries_whole_real_attempt`，8 次尝试中 7 次被语义审核拒绝，
  另一次通过语义审核、标注与生产 reconcile 后，被测试预期的单次拒绝注入消费。
  没有 deadline/transport 失败；该节点关闭 verify，且 generate_only 不启用本次 process prompt 保留。
  generation、orchestration、runtime 及 common/contracts 与旧主线完全相同，审核 Schema 不含 maxItems。
  当前证据没有把失败指向本次变更，也不能把这次套件记为全部通过。
- 旧失败的 trace/failed report 被后续 pytest 临时目录清理，保留了完整失败日志。
  因此无法区分那 7 次拒绝究竟来自 SchemaViolation，还是结构合法但语义布尔值/reason_codes 未通过。
  旧主线的对照改用固定产物目录，得到 `1 passed in 46.40s`；新快照对应复验仍为
  `1 failed in 85.46s`，八次全部被 semantic_evaluation 拒绝，没有继续重跑到绿。
- 新复验的 144 次真实调用全部 `status=ok`、`retries=0`，最大 latency 为 1691ms，低于 120 秒截止。
  refs trace 中没有 `schema.repair`，因此没有 L1、L3 或最终 SchemaViolation；拒绝来自未修改的
  `_semantic_consistent` 对合法结构的语义一致性判定。annotate、reconcile、context_overflow、
  output_truncated、provider_retryable_exhausted 拒绝计数均为零。
  旧主线对照也没有 Schema 修复，80 次调用全部成功，经历 2 次语义拒绝、1 次预期 reconcile 注入后交付。
  这些证据指向现有生成/审核的语义稳定性，不能因旧主线单次通过就认定新代码回归，
  也不能宣称本次真实序列门已通过；refs trace 不保存模型响应，未进一步断言哪一项语义判据失败。
- 正式 Uncle Bob 最终审查使用干净快照 `8838b618`：独立基线 `1048 passed in 6.43s`，
  全部 23 个语义变体 killed，零 survived、invalid、inconclusive；每项均恢复干净，审查工作树已移除。
  首轮 12 个变体和第二轮完整 23 个变体的历史结果均保留；最终对同一整套 23 个重新验证。
  逐项规范、错误实现、因果断言、三轮记录及恢复证明见
  [Uncle Bob 审查报告](BOB-common-inference-repair.md)。
- 设计、代码规范、覆盖率与变异门完成；真实序列门仍按上述失败结果记录。
- 提交前逐字节核对了交付目录与最终审查快照的 6 个生产文件和 8 个测试文件；覆盖报告中的生产文件
  SHA-256 也全部匹配。父任务创建的 4 个干净对照/审查工作树均已移除，其他已有工作树保持原样。

早期离线命令的两个失败另行保留：新增真实测试文件未同步包布局白名单，以及人工中断时正在运行的
OR-Tools 大规模求解测试。白名单同步后，未受中断的上述完整离线门通过。早期覆盖文件不作为最终证据。

当前原始日志与覆盖文件位于忽略目录 `out/verification-common-fixes-20260920/`。
完整离线命令：

```bash
uv run --python 3.12 pytest -q -m 'not integration' --cov=labelkit --cov-branch \
  --cov-report=json:out/verification-common-fixes-20260920/coverage-final.json --cov-report=term
```

## 成熟实现依据

- [HTTPX timeouts](https://www.python-httpx.org/advanced/timeouts/)：区分 connect/read/write/pool，
  read timeout 约束下一块数据的等待，不提供整个请求的总截止。
- [Python asyncio timeout](https://docs.python.org/3.12/library/asyncio-task.html#timeouts)：
  使用标准库截止上下文，不引入计时器抽象或新依赖。
- [jsonschema validation errors](https://python-jsonschema.readthedocs.io/en/stable/errors/)：
  直接使用 validator、validator_value 和 instance 表达数量边界。

没有增加兼容层、迁移、fallback 或用户配置开关。未调整 WorldMock 的依赖地址或固定提交。
