# 特性开发规格：Console 实时面板（TUI，spec v1.10）

> **状态：已定稿并实现（2026-07-17）**。一轮定稿（需求方 2026-07-17
> 裁决：spec-only 定稿；U4 批 rich；U18 批 T16 有界修订；U14 心跳默认关；U15 键盘
> 交互一期实施）后，经**三路独立审计**修订为本二轮定稿：① 代码可行性/亲和性审计
> （2 blocker + 5 major + 9 minor——协议数据通路缺失 B-1、段棋盘口径不可实现 B-2
> 等，全部裁决并入 U19–U27 与 §3.3）；② 文档修改清单审计（2 blocker + 6 major +
> 8 minor——examples 重组后的回归锚失效、CONTRACTS 入册指位错误等，勘误表 A 已
> 随本文落地、实施清单表 B 并入 §3.8）；③ deep-search refute/elevate（1 推翻 +
> 4 修订 + 6 成立——实跑逐字节回归锚被证伪改三层判据 U24、NO_COLOR 语义修订
> U25、Live 线程模型钉死 U26 等，新增引用 [C-21]–[C-42]，§6 逐 URL 核验表）。
> **U1–U27 全部闭合，无待裁决项**。需求方 2026-07-17 第二指令：立即实施、不允许 defer 任何待实现内容。
> 编号 U*（决策，裸写）沿用；[C-*]（引用）以 `PROPOSAL-tui-console.md`（调研
> 原始记录）与本文 §6（审计增量）为准。实现随 v1.10 提交落地：§3.8 表 B 全项
> 完成（代码/CONTRACTS/测试/手册/指南），验收 = 1200+ 离线 + 32 集成（真实 LLM）
> 全绿、五工程 dry-run 黄金对账（HEAD 基线同输出溯源）、examples/stream 全链
> 实跑 + pty 面板/键盘真机验证。

## 1. 结论与形态

**品类判定**（调研收敛，[C-1]–[C-20]）：LabelKit `run` 是一次性批处理，正确品类
是 **双区内联实时面板**（buck2 superconsole / Docker BuildKit `--progress=tty`
形态）——日志在上方照常滚动、保留 scrollback，终端底部画布按节流频率原地重绘；
运行结束画布定格为静态终版摘要。全屏 alternate-screen（k9s/htop/textual 品类）
与交互 REPL（Claude Code/Codex 品类）均否决（U1）。

**定位**：面板 = spec §7.7「进度显示面」的三态增强实现，是 M12 可观测性的第四个
纯消费面（前三个：stderr 运行日志、trace、report.json）。零业务耦合、**零 §7.2
事件目录改动**、report.json 零新键；渲染器物理上归 CLI 层
（`labelkit/cli/console.py`，rich 懒 import 唯一触点），经 `ProgressListener`
五回调协议（common 层定义，U19）接收 M10/M12 进程内推送；依赖方向
`cli → orchestration → operators → common` 不变。

三态 `console.mode = "auto" | "rich" | "plain"`（config.toml `[console]` 节 +
CLI `--console`）：**plain 与 v1.9 现行 stderr 行为等价（三层回归锚 U24 判定；
`heartbeat_s = 0` 默认下）**；auto 判定链见 §3.1（NO_COLOR 不再降 plain——U25
修订：rich 原生尊重 NO_COLOR 剥色保布局）；rich 档渲染库 = `rich`（U4 已批：
懒 import；连带纯 Python 传递依赖 markdown-it-py + pygments）。

## 2. 设计裁决记录（U1–U27）

一轮裁决 U1–U18（原文保留，两处经二轮审计收窄的以 U19+ 行为准）：

| # | 问题 | 裁决 | 依据 |
|---|---|---|---|
| U1 | 终端 UI 品类 | 双区内联面板；**永不进 alternate screen** | 批任务保 scrollback；全屏丢历史反证 [C-16]；superconsole/BuildKit 同型 [C-1][C-4] |
| U2 | 架构定位 | M12 第四消费面，纯 sink；渲染器归 CLI 层，协议归 common | bazel UI 锁教训——UI 与处理解耦 [C-6]；分层依赖方向不变 |
| U3 | 一期交互范围 | 渲染 + **键盘开关**（U15 裁决并入）；Ctrl-C 语义不动 | 需求方 2026-07-17（U15）；键位规格 §3.4 |
| U4 | 渲染库 | **`rich` 批准入 §2.6 依赖白名单**（懒 import；仅 `labelkit/cli/console.py` 触点） | 需求方 2026-07-17 批准；pip vendored 背书 [C-9]、同品类先例 Curator/distilabel [C-15][C-20]、CJK 宽度正确；手写 ANSI 备选作废 |
| U5 | 模式开关 | 三态 `--console auto\|rich\|plain`；auto 判定链 §3.1 | BuildKit `--progress` 五档收敛 [C-4]、buck2 `--console` [C-3]、tqdm 非 TTY 语义 [C-11] |
| U6 | 信息纪律（红线） | 面板 = stderr 镜像同级：只显示计数/枚举/profile 名/密钥**环境变量名**/file:line 结构字段；**无数据内容、无 LLM 自由文本（reason/task_name/critiques）、无 record id**——执行机制升级见 U22 | spec §7.1 ① 红线；比 trace `none` 档更严 |
| U7 | 失败语义（红线） | 渲染异常自吞 + 一次性 WARN + 当场降级 plain 续跑；**永不影响退出码/数据产出**——sink 侧防护补充见 U23 | 记录级隔离精神（§2.6）；渲染是旁路 |
| U8 | 结束行为 | 画布最后一次重绘后定格（`transient=false` 语义），scrollback 留完整日志 + 终版面板 | 批任务产物可贴工单；superconsole 同行为 [C-1] |
| U9 | 布局 | 六区块（标头/批进度/段棋盘/状态账/LLM/键位+中断，§3.2）；stream/stitch 键仅启用时在场 | report.counts 口径对齐；Nextflow dim/bold 层次手法 [C-12] |
| U10 | 刷新模型 | 事件回调只做 O(1) 内存累加；重绘由节流 tick 驱动（默认 5 Hz）读原子快照；`LLMClient.snapshot()` 只读增量——tick 宿主钉死见 U26 | bazel 临界区教训 [C-6]；uv 闪烁缓冲 [C-10]；rich Live refresh 语义 [C-13] |
| U11 | stage 粒度信号 | 进程内 `MetricsSink.stage_begin(stage, batch_no)` 仅转发 listener，**不产生 TraceEvent、不入 §7.2 目录** | §7.7 既有定性「进度显示不属于日志」；trace 契约只增不改原则零触碰 |
| U12 | 段进度分母 | 复用 dry-run 静态估算公式，UI 标「估算/下界」——批级口径与导出函数见 U20 | 分母现成零新逻辑；S22/R28 下界口径沿用 |
| U13 | run 之外的面 | validate --probe / dry-run 估算 / 终版摘要表格化仅 rich 档（plain 行式 = 逐字节锚，含 v1.8/v1.9 无条件打印的 `segment_calls`/`stitch_calls` 行）；`rubric --show` 恒 plain（stdout 机器消费）；probe 表仅当 stdout TTY 时渲染 | stdout/stderr 职责分离现状；validate 通路修复见 U27 |
| U14 | plain 非 TTY 心跳 | `console.heartbeat_s` **默认 0（关）**——保回归锚；开启时单行数据无关汇总、固定 deadline 自续不漂移；所有权 = CLI 层 plain 监听器（`loop.call_later`，run.start 起 run.end 止，直写 stderr 不经 logging） | 需求方 2026-07-17；terraform/bazel CI 教训 [C-7][C-17] |
| U15 | 键盘交互 | **一期实施**（需求方 2026-07-17，推翻提案暂缓推荐）：buck2 式 stdin 开关，键位封闭集 §3.4；仅 rich ∧ stdin TTY ∧ `console.interactive=true` ∧ termios 可用时生效 | buck2 toggles 先例 [C-3]；生产先例 memray（rich Live + termios cbreak 单键开关，[C-34]） |
| U16 | 全屏交互品类 | 不做；未来 O5 `labelkit analyze` trace 浏览器立项时另行评估（届时重估 textual） | 8.3 O5 现状；本期非目标 |
| U17 | 批总数分母 | UI 模态恒显示 i/N（IngestPlan 配对扫描零额外 I/O）；文本模态默认 `批 i` 无分母、`console.estimate = true` 显式换一遍输入 I/O；**live 预扫复用铁律：UI 模态把既有 P2-4 预扫翻 `estimate=True`，禁止二次 scan** | M10「避免双倍输入读」现状（orchestrator.py:200-206）；ingest.py 配对表免费证据 |
| U18 | T16 有界修订 | **批准**：rich 面板状态账展示 stitched/threads（与 report.counts 口径对齐）；plain 进度行与文本版终版摘要键集**逐字节不动**——T16「固定键集」收窄为 plain 面专属 | 需求方 2026-07-17；spec §1.6 v1.10 行、spec/310:37 与 spec/316:175 收窄括注已落 |

二轮裁决 U19–U27（三路审计发现，2026-07-17 并入）：

| # | 问题 | 裁决 | 依据 |
|---|---|---|---|
| U19 | 渲染器数据通路（一轮 B-1，blocker） | 协议扩为**五回调**：`on_run_context(cfg, snapshot, counters, fatal_streak)` + `on_estimate(est)` + `on_event` + `on_stage` + `on_stop_requested`；MetricsSink 增仅转发方法 `stage_begin` / `run_estimate` / `stop_requested`；渲染器以**惰性壳**传入 `execute_run(..., listener=)`（CLI 在 load 前无 cfg），`on_run_context` 激活；Orchestrator 构造器冻结不动（listener 经 MetricsSink 进 M10） | execute_run 内部 load（runtime.py:31-37）；三回调协议无法送达标头/估算/snapshot/counters/熔断 |
| U20 | 段棋盘口径（一轮 B-2，blocker） | 分子 = **括号归属**：批内阶段串行屏障下，落在 `on_stage(X)` 与下一次 `on_stage` 之间的 `llm.call` 一律记入 X（M8 修复调用记入在途 stage——对进度显示恰当且精确），**运行级累计**；分母 = **运行级估算**：估算公式抽出为纯函数 `estimate_run(cfg, plan)`（orchestration 层导出，dry-run 与 live `on_estimate` 共用），`▶` 进度数 = 累计分子 / 对应 `*_calls` 估算（标「估算」；估算未送达时仅显示分子）；`✓`/`▶`/`·` 三符号反映当前批位置 | llm.call 恒 stage="llm"/batch_no=0（llm_client.py:677），事件面无阶段归属；`_estimate()` 仅运行级——运行级口径直接可用、免批级合成 plan |
| U21 | emitter 让位机制（一轮 M-1） | M1 在 load() 收尾解析**产物字段** `ConsoleConfig.mode_resolved: "rich"\|"plain"`（isatty ∧ log_format ∧ TERM ∧ `importlib.util.find_spec("rich")` 探测，不真 import）；emitter 静态门 `if mode_resolved == "rich": return`（构造器零改动）；**中途翻转（异常降级/`q` 脱离）全部由渲染器承接**——plain 进度行与文本版终版摘要的行格式抽成 common 层纯函数 `labelkit/common/observability/console_format.py`（emitter 与渲染器双方 import，保逐字节等价且不破 cli↛operators 依赖方向测试） | emitter ctor 冻结（emitter.py:57-64）；cli 禁 import operators（test_cli.py:196-209） |
| U22 | 面板隐私由纪律升级为机制（一轮 M-2 × refute R11） | MetricsSink 转发 `on_event` 前对 payload 施 `redact_payload(payload, "none")`（仅 listener 非 None 时执行；浅递归 strip 成本可忽略）——脱敏前 payload 实测含 LLM 自由文本乃至输入摘录（stitch.judge 的 task_name/reason 无条件在场、verify.verdict 的 critiques/defects、quality 的 reason/excerpt），源头剥除后 U6 成为机制保证 | stitch.py:899-900、verify.py:645-657、quality.py:786-857 实测；redact_payload 现成（obslog.py:103-135） |
| U23 | sink 侧 listener 异常防护（一轮 M-3） | MetricsSink 每次转发 `try/except Exception`：首次异常打一条 WARN 并**置 listener 为 None**（EventLog 写失败「warn 一次 + 关通道」同款纪律）——listener 回调在事件源调用栈内执行，未捕获异常会污染记录级/批级 | obslog.py:174-188 先例；U7 红线的 sink 侧补全 |
| U24 | 回归锚三层化（refute R2 推翻原判据） | 实跑 stderr **逐字节 diff 物理不可能**（每行携墙钟时间戳、batch.end 镜像含 duration_ms、温度 0 端点非逐字节确定）。替换为三层：① 单元层——`console_format` 纯函数在固定时钟注入下的黄金快照逐字节断言；② dry-run 层——examples 三目录五工程 `--dry-run --console plain` 估算行（无时变字段）与 v1.9 基线逐字节 diff 为空；③ 实跑层——五工程实跑 stderr **归一化**（时间戳/耗时/token/延迟/计数值→占位符，cargo 测试套 `[ELAPSED]` 遮蔽同法 [C-35]）后与 v1.9 基线结构等价（验收级） | refute 审计证伪 + cargo 归一化先例；E2E 已知温度 0 漂移（E2E-FINDINGS #6）；层①②入离线套件（`test_console_format` 黄金快照 + `test_dry_run_plain_golden_files`） |
| U25 | NO_COLOR 语义（refute R3 修订） | **NO_COLOR 不再降 plain**：no-color.org 定义为「禁用 ANSI 颜色」而非禁用布局；rich / BuildKit / uv / cargo 多数实践均为剥色保布局，rich Console 原生尊重 NO_COLOR（no_color 属性）。auto 判定链删除 NO_COLOR 条件；rich 档下 NO_COLOR = 无色面板（布局/重绘保留）。TERM=dumb 仍降 plain（无光标控制，rich 侧 `Console.is_dumb_terminal` 双保险） | no-color.org 原文 [C-27]；rich 源码 NO_COLOR 处理 [C-26]；BuildKit/uv/cargo 行为核实 [C-28][C-30] |
| U26 | tick 宿主与线程模型（一轮 M-4 × refute R5） | **钉死 `Live(auto_refresh=False, redirect_stdout=False, redirect_stderr=False)`** + asyncio task 作 tick（`await asyncio.sleep(1/refresh_hz)` → 采样 counters/snapshot → `live.update(..., refresh=True)`）；键盘非阻塞 `select` 挂同一 tick。rich 默认 auto_refresh=True 会起内部刷新线程——跨线程遍历事件循环线程正在 `setdefault` 的 dict 会抛 RuntimeError；同线程 tick 下 snapshot/counters 读天然一致（await 点之间），除 rich 自身外**零新线程**字面成立 | rich Live 源码线程模型 [C-13]；llm_client.py:737-738/838 动态插键实测 |
| U27 | validate 通路（一轮 M-5） | `validate_project(config_path, project_path, overrides: CliOverrides = CliOverrides())` 只增尾参，`_cmd_validate` 透传——`--console` 进 M1、jsonl×显式 rich 的 WARN 在 validate 路径生效；probe 结果表**仅当 stdout 为 TTY** 时渲染（脚本消费保持现行行式，stdout 通道职责不变） | runtime.py:88 硬编码 CliOverrides()；commands.py:33-39 probe 打 stdout |

## 3. 规格正文

### 3.1 模式判定链与降级矩阵

```
auto → rich 当且仅当：stderr.isatty()
                   ∧ tool.log_format == "text"
                   ∧ TERM 不为 "dumb"/空
                   ∧ rich 可导入（M1 以 find_spec 探测；CLI 层懒 import 失败仍回落）
其余一律 plain。NO_COLOR 不参与判定（U25）——rich 档下由 rich 原生剥色保布局。
产物：M1 在 load() 收尾把判定结果冻结为 ConsoleConfig.mode_resolved ∈ {"rich","plain"}（U21）。
```

| 情形 | 行为 |
|---|---|
| `--console rich` 但非 TTY | 尊重显式档（buck2 `super` 同义 [C-3]）——CI 录 ANSI 回放场景 |
| `tool.log_format = "jsonl"` | **强制 plain，不可被显式 rich（CLI `--console rich` 或 config `console.mode="rich"`）覆盖**（§7.7 铁律：stderr 逐行可 `json.loads`）；显式冲突时 M1 打 WARN 一次 |
| 渲染期任何异常 | 渲染器自吞 + 一次性 WARN + 当场降级 plain 续跑（U7）；sink 侧转发异常同款防护（U23） |
| 终端宽 < 60 列 | 画布退化为现行单行 `\r` 等价形态 |
| `NO_COLOR` | rich 档剥色保布局（rich 原生，U25）；不降 plain |
| `TERM` dumb/空 | 降 plain（终端能力探测，与 isatty 同级；非配置通道——不违反「除 API key 外无环境变量」§2.5；不新设 `LABELKIT_*` 环境变量） |
| plain 档 | `emitter._progress()` 单行 `\r` 与 `_print_summary()` 文本版原样执行（经 `console_format` 纯函数，格式逐字节不变）——v1.9 行为等价（`heartbeat_s=0` 时；三层回归锚 U24） |
| rich 档 | 停用 `emitter._progress()`（静态门 `mode_resolved`，U21）与 `_print_summary()` 文本版（换渲染器表格版，数值来源不变） |

### 3.2 面板布局与数据源（rich 档）

运行中（stderr；上方滚动区照常输出运行日志，行文本与 plain 逐字节一致；下方画布
按 `console.refresh_hz` 原地重绘）：

```
2026-07-17T01:21:12+08:00 WARN  ingest  batch=1 bad_line file=s2.jsonl line=17 reason=missing_text_field
2026-07-17T01:23:40+08:00 INFO  emitter batch=2 批 2 落盘：主输出 +18 行（累计 41），rejects +1（累计 3）
────────────────────────────────────────────────────────────────────────────────────
 labelkit run · f3a9c04b7d21 · process/ui/stream+stitch · seed 42 · 已用 04:12 · ETA ~06:40
 project examples/stream/project.toml → out/stream-labels.jsonl

 批 3/5  ██████████████░░░░░░░░░░  记录 96/160 (scanned)

 段  segment ✓   stitch ✓   dedup ✓   extract ▶ 18/46   quality ·   annotate ·   verify ·

 账  emitted 41   dup 3   lowq 5   verify 1   failed 0   noise 2   absorbed 88   stitched 2   threads 5

 LLM  default  在途 4/4  calls 213  重试 7  tok 412k↑ 96k↓  $0.83  p50 2.1s
      judge    在途 2/4  calls 46   重试 0  tok 88k↑ 12k↓   $0.19  p50 3.4s
      密钥 LABELKIT_KEY_A ok · _B 冷却12s · _C 禁用          熔断 0/20
 [?]帮助 [l]LLM展开 [e]错误条 [p]暂停 [q]脱离
────────────────────────────────────────────────────────────────────────────────────
```

| 区块 | 内容 | 数据源（全部为既有结构字段，唯一新增采集点 = p50 延迟窗） |
|---|---|---|
| 标头 | run_id、mode/modality（stream/stitch 徽标）、seed、耗时、ETA（仅批总数分母可得时显示，U17；EMA 吞吐外推标 `~`） | `on_run_context(cfg, ...)`（U19）；run_id 自 `run.start` 事件 |
| 批进度 | UI 模态 `批 i/N` + scanned（预扫 `estimate=True` 复用，禁二次 scan，U17）；文本模态默认 `批 i`（`console.estimate=true` 换分母）；generate_only 相位内退化为 `生成 ▶ calls i/N · 已产 n 条`（calls 实时自 llm.call；**已产 于生成相位结束一次更新**——produced 为相位末计量） | `batch.start/end` 事件 + `on_estimate`（U19/U20） |
| 段棋盘 | 仅启用 stage 按链序：`✓` 本批已过 / `▶` 进行中（**括号归属**的运行级累计 llm.call 完成数 / `estimate_run` 运行级 `*_calls` 分母，标「估算」；估算未送达时仅显示分子，U20）/ `·` 待走 | `on_stage`（U11）+ `on_event(llm.call)` 括号归集 + `on_estimate` |
| 状态账 | 九态计数，stream/stitch 键仅启用时在场、同 report.counts 口径（stitched/threads 展示 = U18 有界修订）；**emitted 分量 = `batch.end.active`（post-emit 恒等）**；批内随批末更新 | `on_event(batch.end)` + `counters()` 拉取 |
| LLM | 每 profile：在途/上限（在途 = Σ 密钥 in_flight，口径 = 在线 HTTP 请求数，不含驻留/退避）、calls、retries、tok ↑↓、成本（未配价目 `—`）、p50 延迟（成功调用口径，有界窗 256）；密钥池行（**环境变量名** + ok/冷却剩余秒/禁用）；熔断 `fatal_streak/threshold`，打开时整行红 + 顶部横幅 | `snapshot()` 每 tick 一次 + `fatal_streak()`（U19） |
| 键位提示 / 中断态 | 交互启用时恒显示一行（`?` 展开全表）；SIGINT 后画布顶部横幅「正在优雅中断（≤30s）…」 | §3.4；`on_stop_requested`（U19） |

运行结束：最后一次重绘后**定格**（U8）——终版摘要表（counts 逐项 = report.json）+
per-stage 耗时横条 + llm_usage 小表 + rejects/trace 路径行；scrollback 保留完整
日志 + 终版面板。

### 3.3 架构与协议（已入册：CONTRACTS §7.8/§7.11/§7.12 签名节 + §7.9/§7.10 行为段 + §8.4 措辞句）

```
 stages ──RunContext.metrics──▶ MetricsSink.event() ──▶ EventLog(trace 文件)     （既有，不动）
                                      │       └────────▶ stderr 镜像(logging)    （既有，不动）
                                      │（v1.10 新增旁路：redact none 档预脱敏 U22 + 异常自吞 U23）
                                      ▼
                              ProgressListener 五回调协议 ◀── M10 stage 循环 / _request_stop / 估算
                                      ▲ 实现
                               ConsoleRenderer（labelkit/cli/console.py，惰性壳）
                                      │ 只读拉取（每 tick 一次，经 on_run_context 注入的闭包）
                               LLMClient.snapshot() / MetricsSink.counters / fatal_streak
```

```python
# labelkit/common/observability/obslog.py（v1.10 增）
class ProgressListener(Protocol):
    """进程内进度旁路——非 trace 面：不产生 TraceEvent、不受 channels 过滤；
    on_event 的 payload 经 redact_payload(payload, "none") 预脱敏后转发（U22——
    机制保证 U6：无 LLM 自由文本、无输入内容；record_ids 保留为结构字段）。
    全部回调必须 O(1)、无 I/O、无锁等待；重绘由实现方自己的节流 tick 驱动。"""
    def on_run_context(self, cfg: "ResolvedConfig",
                       snapshot: "Callable[[], tuple[ProfileSnapshot, ...]]",
                       counters: "Callable[[], Mapping[str, int]]",
                       fatal_streak: "Callable[[], int]") -> None: ...
    def on_estimate(self, est: Mapping) -> None: ...   # estimate_run() 同构 dict；文本模态未开 estimate 时不发
    def on_event(self, ev: TraceEvent) -> None: ...    # payload 已 none 档预脱敏
    def on_stage(self, stage: str, batch_no: int) -> None: ...
    def on_stop_requested(self) -> None: ...

class MetricsSink:
    def __init__(self, cfg, run_id, event_log,
                 listener: ProgressListener | None = None): ...   # 只增尾参
    def stage_begin(self, stage: str, batch_no: int) -> None: ...  # 仅转发 on_stage；不产生 TraceEvent
    def run_estimate(self, est: Mapping) -> None: ...              # 仅转发 on_estimate
    def stop_requested(self) -> None: ...                          # 仅转发 on_stop_requested
    @property
    def fatal_streak(self) -> int: ...                             # 只读（熔断行数据源，m-5）
    # 转发纪律：每次转发 try/except Exception——首次异常 WARN 一次并置 listener=None（U23）

# labelkit/common/observability/console_format.py（v1.10 新增，U21——plain 行格式单一事实源）
def format_progress_line(batch_no: int, emitted_total: int, totals: Mapping[str, int]) -> str: ...
def format_summary_lines(counts: Mapping[str, int]) -> list[str]: ...
# emitter 与 ConsoleRenderer 双方 import；输出与 v1.9 硬编码字符串逐字节一致（U24 ① 层黄金快照钉死）

# labelkit/common/runtime/llm_client.py（v1.10 增，只读）
@dataclass(frozen=True)
class KeySnapshot:
    env: str                                  # 环境变量名——唯一可展示身份
    state: Literal["ok", "cooldown", "disabled"]
    cooldown_remaining_s: int = 0
    calls: int = 0                            # 逐密钥用量镜像（KeyUsage）——'l' 展开视图
    rate_limited: int = 0                     # 数据源；池未物化时为 0

@dataclass(frozen=True)
class ProfileSnapshot:
    name: str
    kind: Literal["llm", "embedding"]         # _usage 按 name 合桶的既有 quirk 由 kind 消歧
    in_flight: int                            # Σ _KeyState.in_flight（在线 HTTP 请求数）
    max_concurrency: int
    calls: int
    retries: int
    prompt_tokens: int
    completion_tokens: int
    est_cost_usd: float | None
    p50_latency_ms: int | None                # 有界样本窗 deque(maxlen=256) 中位数；成功调用口径
    keys: tuple[KeySnapshot, ...]             # 池=1 时单元素；从 _pool_members 构造，不物化 _pools

class LLMClient:
    def snapshot(self, now: float | None = None) -> tuple[ProfileSnapshot, ...]: ...
    # 纯读、无 await、无锁；仅从渲染 tick（事件循环线程内）调用——U26 下无跨线程争用。
    # p50 喂点：_post_with_retries 成功 return 前 append 到 per-(kind,name) deque(256)——唯一新增采集点。

# labelkit/orchestration/orchestrator.py（v1.10 导出复用，U20）
def estimate_run(cfg: "ResolvedConfig", plan: "IngestPlan | None") -> dict: ...
# _estimate()（dry-run）与渲染器批级分母共用；live 路径传入 P2-4 预扫 plan（UI 模态翻 estimate=True），禁二次 scan

# labelkit/orchestration/runtime.py（v1.10 只增）
def execute_run(config_path, project_path, overrides,
                listener: ProgressListener | None = None) -> int: ...
def validate_project(config_path, project_path,
                     overrides: CliOverrides = CliOverrides()) -> ResolvedConfig: ...   # U27
```

**调用时序**（U19）：CLI 构造惰性壳 renderer（此刻无 cfg）→ `execute_run` 完成
load + 装配后、`asyncio.run` 前调 `listener.on_run_context(cfg, llm.snapshot,
counters, fatal_streak)`（标头即刻可渲染，renderer 据 `cfg.console.mode_resolved`
激活或保持 plain 心跳形态）→ M10 在 P2-4 预扫后经 `metrics.run_estimate(...)` 发
估算（process 模式复用该次 scan；generate_only 走 3.6.2 静态公式无 scan）→ 批
循环内每 stage `run()` 之前 `metrics.stage_begin(stage.name, batch_no)` →
`_request_stop` 内加一行 `metrics.stop_requested()`。类型引用以
`TYPE_CHECKING`/字符串注解规避 obslog↔llm_client 环。

**渲染 tick（U26）**：`Live(auto_refresh=False, redirect_stdout=False,
redirect_stderr=False, transient=False, console=Console(stderr=True,
soft_wrap=False))`；asyncio task 循环 `await asyncio.sleep(1/refresh_hz)` →
采样（counters()/snapshot()/内部累加器）→ `live.update(renderable,
refresh=True)`；键盘轮询（§3.4 非阻塞 select）挂同一 tick。除 rich 自身外零新
线程。

**日志路由（R1 加固）**：renderer 启动时保存 `labelkit` logger 那只
`_labelkit_handler` 标记 handler 的原 stream（`setStream` 返回值记账），把流指到
Live 滚动区代理（`live.console` 包装的 file 接口）；停止/降级路径 finally 恢复原
stream。Formatter 不动 ⇒ 日志行文本与 plain 逐字节一致。依赖集内无裸 fd 写
stderr 的行为（httpx 走 logging；logging.lastResort 动态解析 `sys.stderr`）。

### 3.4 键盘交互（一期，U15 需求方裁决）

生效条件（合取）：rich 档 ∧ `sys.stdin.isatty()` ∧ `console.interactive = true`
∧ `termios` 可用（POSIX）。任一不满足 ⇒ 纯渲染（键位提示行不显示）；Windows
（无 termios）一期纯渲染，msvcrt 支持列演进（§5）。生产先例：memray v1.10.0 起
的 rich Live 单键开关即 termios cbreak + 非阻塞轮询同构（[C-34]）。

| 键 | 行为 |
|---|---|
| `?` / `h` | 帮助行展开/收起（列出全部键位） |
| `l` | LLM 面板展开/收起：展开 = 每密钥一行（env 名、状态、calls、rate_limited） |
| `e` | 最近错误条开/关：环形最近 5 条 `error` 事件的 `stage + kind`（§7.6 封闭词表，数据无关） |
| `+` / `-` | 画布行数上限增/减（4–16 行；默认自适应终端高） |
| `p` | 暂停/恢复画布重绘（日志照常滚动；调试器/复制粘贴友好——Curator 退化开关的交互版 [C-15]） |
| `q` | **面板脱离**：本次运行余下时间降级 plain（不终止运行、不影响退出码） |

- 键位为**封闭集**，未列键一律忽略；**Ctrl-C 不被面板消费**——`tty.setcbreak`
  只清 ECHO|ICANON、**保留 ISIG**（CPython tty 源码确证 [C-31]），SIGINT 仍经
  `loop.add_signal_handler` 走 M10 优雅中断（3.10.3），面板只显示中断横幅。
- 终端状态纪律：进入前 `termios.tcgetattr` 保存 → `tty.setcbreak` → 退出/降级/
  异常路径经 `finally` 以 `TCSADRAIN` 恢复（覆盖 load 阶段 KeyboardInterrupt 直穿
  `main.py` 的窗口——cbreak 仅在 renderer 激活后进入）；键盘轮询在渲染 tick 内
  非阻塞 `select([stdin], [], [], 0)`，**零新线程**。
- stdin 被占用的代价与 buck2 相同（粘贴的后续命令会被面板吞）——
  `console.interactive = false` 即 buck2 `--no-interactive-console` 的对应物 [C-3]。

### 3.5 配置面（config.toml `[console]` 节，工具级）

面板是部署环境属性（本机终端 vs CI），归 config.toml；project.toml 零改动。
优先级不变：CLI `--console` > config.toml > 内置默认。

```toml
[console]                    # v1.10 全部可缺省
mode = "auto"                # "auto" | "rich" | "plain"（判定链 7.7；产物 mode_resolved 由 M1 冻结）
refresh_hz = 5               # rich 画布重绘频率，1–10（越界 = CONFIG_ERROR）
heartbeat_s = 0              # 仅 plain 非 TTY：每 N 秒一行数据无关心跳；0 = 关（默认，U14）；< 0 = CONFIG_ERROR
estimate = false             # 仅文本模态：启动估算扫描换批总数分母 + ETA（多读一遍输入，U17）
interactive = true           # rich ∧ stdin TTY 时启用键盘开关（3.4）；false = 纯渲染
```

CLI 增量：`labelkit run … [--console {auto,rich,plain}]`；`validate` 同参共用
（U27 通路）。`CliOverrides` 增 `console: str | None = None`。M1 校验（3.1.4 行
已落）：`mode`/`refresh_hz` 枚举与范围、`heartbeat_s ≥ 0`（CONFIG_ERROR，错误
聚合不首错即抛）；`log_format="jsonl"` ∧ 显式 rich（CLI 或 config）⇒ WARN 一次 +
强制 plain。心跳行格式（数据无关，固定键集）：
`heartbeat batch=3 stage=quality llm_calls=182 elapsed=312s`。

### 3.6 约束对齐（spec §2.6 逐条）

| 约束 | 对齐 |
|---|---|
| stderr 永不含数据内容/提示词/密钥 | U6 红线 + U22 机制（none 档预脱敏转发）：面板与心跳行均为计数/枚举/结构字段；密钥只显示环境变量名 |
| 无数据持久化 | 面板状态纯内存、不写文件；结束定格只是 stderr 输出 |
| 可复现性 | 渲染不消费 `run.seed` PRNG、不影响任何采样路径；`console.estimate` 只读输入不改流水线 |
| 记录级隔离 / 退出码 | U7 + U23 双层防护：渲染 tick 与 sink 转发异常均自吞降级；exit 0/1/2/3/4 语义零变化 |
| 依赖面 | `rich` 入白名单（U4 已批，§2.6 已修订）；懒 import、operators/common 零 rich 触点（M1 仅 find_spec 探测） |
| 报告只含计数 | report.json 零新键；p50 延迟窗不入 report；面板是 report 的实时预览 |
| 无环境变量（API key 除外） | `TERM` 定性为终端能力探测（§3.1）；`NO_COLOR` 交 rich 原生处理（U25）；不新设环境变量 |

### 3.7 测试与验收

| 层 | 要求 |
|---|---|
| 渲染快照（离线） | `Console(width=100, force_terminal=True)` 定宽渲染 → 字符串快照断言（喂 MetricsSink 计数器状态而非 LLM 响应——不违反「禁 mock LLM」指令）。覆盖：九态账、密钥池三态行、熔断横幅、中断横幅、窄终端单行退化、generate_only 形态、`l`/`e` 展开态 |
| 回归锚（三层，U24） | ① `console_format` 固定时钟黄金快照逐字节断言；② examples 三目录五工程（`text/project.toml`、`text/project-synth.toml`（generate_only）、`ui/project.toml`、`stream/project.toml`（stream+stitch 全开）、`stream/project-text.toml`（文本流））`--dry-run --console plain` stderr 与 v1.9 基线逐字节 diff 为空；③ 五工程实跑 stderr 归一化（ts/耗时/token/延迟/计数值→占位符）后与 v1.9 基线结构等价；`log_format="jsonl"` 下 stderr 逐行 `json.loads` 恒真且显式 rich 被拒并 WARN |
| 降级注入 | 打桩使渲染 tick 抛异常 → 断言运行照常完成、退出码不变、恰一条 WARN、**降级落 detached-plain**（rich 所有权下渲染器续打 `console_format` 进度行与文本摘要——emitter 已静态让位，CONTRACTS §7.10；heartbeat 面失败落 inert——emitter 仍拥有 plain；二次失败静默落 inert）；sink 侧注入 listener 异常 → 断言 WARN 一次 + listener 置空 + 运行不受影响（U23） |
| 键盘交互 | 伪 TTY（`pty` 标准库）注入键序：`q` 脱离后 stderr 回到 plain 行式且运行继续；`p` 暂停期间日志仍滚动；退出后 `termios` 属性恢复与进入前逐字节一致；cbreak 下 Ctrl-C 仍触发 SIGINT 优雅中断 |
| 协议契约 | ProgressListener 全回调 O(1) 无 I/O（审查项）；`listener=None` 路径（validate/全部既有测试）零行为变化；`snapshot()` 在并发 gather 中调用不阻塞事件循环；`on_event` payload 断言不含 `_FREE_TEXT_KEYS`/`_DATA_KEYS`/excerpt（U22） |
| snapshot 单测 | 池三态（ok/cooldown/disabled）、cooldown_remaining 注入 now、p50 窗口滚动、零活动 profile 零值快照、不物化 `_pools` |
| 布局冻结 | `tests/cli/test_cli.py` 的 `EXPECTED_PRODUCTION_PY`/`EXPECTED_TEST_PY` 双冻结集**先行**增 `labelkit/cli/console.py`、`labelkit/common/observability/console_format.py`、`tests/cli/test_console.py`（否则布局测试先红） |
| 实跑目检 | examples/stream（`project.toml`，stream+stitch 全开，面板信息最全）与坏密钥场景（P2-3 复现——面板须在 10 秒内红出密钥禁用与熔断横幅） |

### 3.8 文件修改清单

**A. 规格勘误批（文档审计表 A，13 条——随本二轮定稿已全部落地）**：
A-1 spec/70:186 回归锚三层化 + 三目录五工程 ✅；A-2 本文 :89/:248/:252 示例路径 ✅
（随全文重写）；A-3 CONTRACTS 指位改 §7.8/§7.11/§7.12+§7.9/§7.10+§8.4（本文
§3.3 标题 + 下表 + spec/312:46）✅；A-4 spec/70 §7.7 增 U13 句 ✅；A-5 spec/301
3.1.4 增 console 校验行 ✅；A-6 spec/316:175 + spec/310:37 U18 收窄括注 ✅；A-7
spec/20:153 引用自解析化 ✅；A-8 spec/50 显式 rich 措辞 + `h` 同义键 ✅；A-9
spec/70:11 heartbeat 前提 ✅；A-10 spec/20:118 枚举序 ✅；A-11 spec/312:46
stage_begin 点名 ✅（随 §3.12.3 五回调重写）；A-12 spec/80 O5 U16 钩子 + 8.1 三
负边界 ✅；A-13 PROPOSAL 不改（存档）✅。

**B. 实施批（本次执行，不允许 defer）：**

| 层 | 文件 | 改动 |
|---|---|---|
| 依赖 | `pyproject.toml` | 增 `rich`；`uv sync` |
| common | `labelkit/common/config/model.py` / `loader.py` | `ConsoleConfig`（五用户键 + 产物 `mode_resolved`）+ `ResolvedConfig.console` 必填字段 + `CliOverrides.console`；loader 解析/校验/WARN + load() 收尾 `mode_resolved` 判定（find_spec 探测）；波及 21 个测试文件 24 个 `ResolvedConfig(` 构造点（机械补参） |
| common | `labelkit/common/observability/obslog.py` | `ProgressListener` 五回调协议 + MetricsSink 尾参与 `stage_begin`/`run_estimate`/`stop_requested`/`fatal_streak` + none 档预脱敏转发（U22）+ 异常自吞（U23） |
| common | `labelkit/common/observability/console_format.py`（新） | plain 进度行/终版摘要行格式纯函数（U21；与 v1.9 硬编码逐字节一致） |
| common | `labelkit/common/runtime/llm_client.py` | `KeySnapshot`/`ProfileSnapshot`/`snapshot(now=None)` + p50 deque(256) 喂点（唯一新增采集点） |
| 编排 | `labelkit/orchestration/orchestrator.py` | `estimate_run(cfg, plan)` 纯函数抽出（_estimate 改薄封装）；stage 循环 `stage_begin`；`_request_stop` 转发；live 预扫 UI 模态翻 `estimate=True` + `run_estimate` 发送；dry-run rich 档 4 行 print 让位（plain 逐字节锚） |
| 编排 | `labelkit/orchestration/runtime.py` | `execute_run(..., listener=None)` 装配 + `on_run_context` 时序；`validate_project(..., overrides=)`（U27） |
| 算子 | `labelkit/operators/emitter.py` | `_progress`/`_print_summary` 改用 `console_format` + `mode_resolved` 静态门（U21） |
| CLI | `labelkit/cli/console.py`（新） | ConsoleRenderer 惰性壳：Live 画布（U26 钉死参数）、六区块渲染、括号归属累计、键盘（cbreak+select）、日志流接管/恢复（R1 加固）、降级、plain 心跳监听器、validate/dry-run/终版表格 |
| CLI | `labelkit/cli/parser.py` / `commands.py` | `--console`（run/validate）；构造 renderer 传入 `execute_run`/`validate_project`；probe 表 stdout-TTY 门（U13/U27） |
| 契约 | `docs/CONTRACTS.md` | 13 锚点（文档审计 B-2 表）：§1 布局树 + §1.2 测试归属；§6.1 ToolConfig 注释/ConsoleConfig/CliOverrides/ResolvedConfig；§6.2 优先级句；§6.3 rule 42 + Warnings v1.10 句；§7.8 snapshot 签名；§7.9 stage_begin/stop 行为句；§7.10 emitter 让位注；§7.11 ProgressListener + MetricsSink；§7.12 CLI 行 + wiring 段；§8.4 措辞重写；§12 冻结件登记（ConsoleConfig 字段序、p50 窗 256、心跳固定键集） |
| 测试 | `tests/cli/test_console.py`（新，含 U24 层② `test_dry_run_plain_golden_files` + `tests/cli/goldens/dryrun-*.txt` 五黄金文件）、`tests/common/observability/test_console_format.py`（新，U24 层①黄金快照）、`tests/cli/test_cli.py`（冻结集+parser）、`tests/common/observability/test_obslog.py`（listener 组）、`tests/common/runtime/test_llm_client.py`（snapshot 组）、`tests/common/config/test_config.py`（[console] 组）、`tests/operators/test_emitter.py`（rich 让位组）、`tests/orchestration/`（estimate_run/wiring） | §3.7 各层 |
| 手册 | `docs/manual/06-config-toml.md`（§6.2 扩题 + [console] 五键表 + §6.1 骨架 + §6.6 速查）、`15-cli.md`（§15.1 用法/参数表 + §15.2 validate 注 + §15.6 三态改写）、`16-observability.md`（§16.1 表行改写 + §16.4 措辞 + 新增 §16.6 面板章含实跑定格样例）、`appendix-a-cheatsheet.md`（A.1 log_format 措辞 + 五键行）、`23-tutorial-5-production.md:27`（jsonl 注释措辞）、`03-quickstart.md`（可选一句指引；样例零改动）、`08-outputs.md`（明确零改动） | 文档审计 B-3 表；§16.6 定格面板样例经 pty 真跑采集（`script -q /dev/null uv run labelkit run ...` 于 examples/stream，剥 ANSI 后收录定格帧） |
| spec 状态翻转 | `spec/00-frontmatter.md`（状态格 + v1.10 行注记）、`spec/10-ch1` §1.6（spec-only 措辞）、`spec/70` §7.7 开头句、本文头部状态行、`docs/dev/PROPOSAL-tui-console.md` 头部一词 | 「规格定稿、实现另行排期」家族 → 「已实现（2026-07-17）」 |
| spec 内容补 | `spec/309-m9-llm-client.md` 3.9.2/3.9.3（snapshot/KeySnapshot/ProfileSnapshot 字段表 + p50 窗行——spec 是字段名单一事实源）、`spec/310-m10-orchestrator.md` 3.10.3 增 v1.10 行（stage_begin/stop 转发/估算复用/dry-run plain 锚限定）、`spec/311-m11-emitter.md` 3.11 让位注 | 文档审计 B-1 表 |
| 指南 | `CLAUDE.md` / `AGENTS.md` | 状态行、依赖清单增 rich、CLI 行增 --console、v1.10 bullet 翻转、删「not yet in pyproject」句；编辑后 `diff` 逐字节一致校验 |

## 4. 非目标

- 不做 web/hosted viewer（Curator 路线 [C-15]）——违反「数据只去配置声明的 LLM
  端点、无遥测」（§2.6）。
- 不做面板内数据内容检视（excerpt/prompt）——trace `excerpt`/`full` 档职责（U6/U22 红线）。
- 不改 §7.2 trace 事件目录、report.json 结构、退出码/输出通道语义。
- 不做跨运行历史面板/持久化仪表——无状态原则。
- 不做全屏交互 trace 浏览器——O5 `labelkit analyze` 议题（8.3，A-12 已登记钩子）。

## 5. 风险与演进

| 项 | 说明 |
|---|---|
| Windows 键盘交互 | 一期 termios-only（无 termios ⇒ 纯渲染）；msvcrt 轮询列演进候选，见到真实 Windows 交互需求再立项 |
| stdin 占用 | 交互启用时粘贴的后续命令被面板吞（buck2 同边界 [C-3]）；`console.interactive=false` 一键回避 |
| 渲染库供应链 | rich 为纯 Python + 两个纯 Python 传递依赖；懒 import + find_spec 探测保证 rich 缺失/损坏时 plain 档零影响 |
| p50 样本窗内存 | 每 profile 有界 deque(256) × int，数量级可忽略；不入 report（报告零新键不变） |
| 心跳默认值回访 | U14 默认关；若无人值守长批的「CI 像死机」投诉成为常态，回访默认值（改默认 = 打破回归锚，须再走需求方裁决） |
| O5 联动 | `labelkit analyze` 立项时重估 textual 与全屏品类（U16）；面板经验（快照渲染/键位）可迁移 |

## 6. 引用（审计增量，编号续接 PROPOSAL [C-1]–[C-20]；每行 URL 均经 refute/elevate 审计实际核验）

| # | 出处 | 承重点 |
|---|---|---|
| [C-21] | CPython 3.12 `Lib/logging/__init__.py`（L1133-1135/L1170/L1287-1301）https://github.com/python/cpython/blob/3.12/Lib/logging/__init__.py | StreamHandler 构造时捕获流引用；`setStream` 为 3.7+ 公开 API（锁+flush+返回旧流）；`_StderrHandler.stream` 为动态 property（lastResort 天然过 Live 代理） |
| [C-22] | rich issue #3286 https://github.com/Textualize/rich/issues/3286 ；discussion #1578 https://github.com/Textualize/rich/discussions/1578 | `Live(redirect_stderr)` 对既有 stdlib handler 失效的成因；官方认可的 `handler.setStream` workaround——§3.3 日志路由（R1 加固）依据 |
| [C-23] | rich 15.0.0 源码 `live.py`/`file_proxy.py`/`console.py`/`cells.py` https://github.com/Textualize/rich/blob/main/rich/live.py | `auto_refresh=True` 自起 `_RefreshThread` 守护线程（U26 关闭之的依据）；FileProxy 按行代理；transient=False 定格；dumb 终端跳过重绘；`Console.size` 每次访问重读终端尺寸（resize 免处理）；Unicode 版本化 CJK cell 宽度表 |
| [C-24] | rich Live 官方文档 https://rich.readthedocs.io/en/stable/live.html | `auto_refresh=False` + 手动 `refresh()` 官方指引；vertical_overflow 三值语义；非 transient 最终帧可见（U8） |
| [C-25] | rich issue #3263 https://github.com/Textualize/rich/issues/3263 ；PR #3637 https://github.com/Textualize/rich/pull/3637 | `vertical_overflow="visible"` 超高内容复制 bug；维护者裁决「Live 无法更新滚出屏幕的内容」⇒ 画布须 crop + 高度钳制（`+`/`-` 行数上限的动机） |
| [C-26] | rich `console.py` NO_COLOR 处理（L725-728/L2088/L2139，同 [C-23] 仓库）；pip issue #12405 https://github.com/pypa/pip/issues/12405 | rich 对 NO_COLOR 的原生语义 = 剥样式、保布局与光标控制；pip 实测 NO_COLOR 下仍输出非颜色 ANSI 码——U25 依据 |
| [C-27] | no-color.org（NO_COLOR 标准）https://no-color.org/ | 标准字面仅约束 ANSI 颜色的添加；CLI 参数应覆盖环境变量；不涉其它样式与布局——U25 语义修订的一手出处 |
| [C-28] | BuildKit README https://github.com/moby/buildkit ；Docker Build variables https://docs.docker.com/build/building/variables/ ；BuildKit PR #4767 https://github.com/moby/buildkit/pull/4767 | NO_COLOR 只禁色、tty 双区布局保留；`BUILDKIT_TTY_LOG_LINES` 默认 6（源码 termHeightMin=6） |
| [C-29] | Nextflow env-vars 参考 https://github.com/nextflow-io/nextflow/blob/master/docs/reference/env-vars.md ；Nextflow PR #6362 https://github.com/nextflow-io/nextflow/pull/6362 | NO_COLOR → plain logs、NXF_ANSI_LOG 显式设定优先——「NO_COLOR ⇒ 整体退 plain」保守读法的唯一工业先例（被 U25 多数派否决的少数派记录） |
| [C-30] | uv 环境变量文档 https://docs.astral.sh/uv/reference/environment/ ；The Cargo Book https://doc.rust-lang.org/cargo/reference/config.html 与 https://doc.rust-lang.org/cargo/reference/environment-variables.html | 色彩与进度为正交双旋钮（uv：NO_COLOR 只禁色、UV_NO_PROGRESS 另设；cargo：term.progress.when 三态、TERM=dumb 才禁 progress）——U25 多数派证据 |
| [C-31] | Python tty 文档 https://docs.python.org/3/library/tty.html ；CPython 3.12 `Lib/tty.py`（本机 3.12.13 核对）；CPython gh-114328 https://github.com/python/cpython/issues/114328 | `cfmakecbreak` 仅清 ECHO\|ICANON（ISIG 保留 ⇒ cbreak 下 Ctrl-C 仍产生 SIGINT）；`cfmakeraw` 才清 ISIG；3.12.2 起 setcbreak 不再清 ICRNL——§3.4 Ctrl-C 语义不变的一手确证 |
| [C-32] | asyncio 平台支持文档 https://docs.python.org/3/library/asyncio-platforms.html ；CPython #73903 https://github.com/python/cpython/issues/73903 ；mio #1377 https://github.com/tokio-rs/mio/issues/1377 ；nathancraddock.com/blog/macos-dev-tty-polling ；code.saghul.net libuv select-trick | `loop.add_reader` 平台坑单：macOS kqueue 对 /dev/tty 恒 EINVAL（libuv 为此开 select 辅助线程、mio wontfix）、Linux epoll 对常规文件 EPERM、Windows Proactor 不支持 ⇒ §3.4 非阻塞 select-in-tick 是更稳选择 |
| [C-33] | Buck2 Consoles 文档 https://buck2.build/docs/users/build_observability/interactive_console/ | toggles 全键表、生效条件 stdin TTY、`--no-interactive-console` 与 `BUCK_NO_INTERACTIVE_CONSOLE`、auto = stderr TTY 上 superconsole——U15/`console.interactive` 对应物 |
| [C-34] | memray v1.10.0 `src/memray/commands/live.py` https://raw.githubusercontent.com/bloomberg/memray/v1.10.0/src/memray/commands/live.py ；NEWS.rst https://github.com/bloomberg/memray/blob/main/NEWS.rst | rich Live 上 termios 单键的生产先例：cbreak 位组合（ISIG 保留）、finally TCSADRAIN 恢复、封闭键集、Ctrl-C 显式放行；1.11.0 才迁 textual——U15 键盘机制先例 |
| [C-35] | cargo PR #13973（Auto-redact elapsed time）https://github.com/rust-lang/cargo/pull/13973 ；`cargo_test_support/compare.rs`；insta filters https://insta.rs/docs/filters/ ；trycmd https://docs.rs/trycmd | 「输出不变」回归的业界标准做法 = 归一化脱敏（`[ELAPSED]`/`[TS]` 占位）而非裸字节 diff——U24 层③的直接模板 |
| [C-36] | Thinking Machines《Defeating Nondeterminism in LLM Inference》https://thinkingmachines.ai/blog/defeating-nondeterminism-in-llm-inference/ | 温度 0 推理非逐位确定（batch-invariance 缺失；同 prompt 千次采样得 80 种输出）——U24 推翻实跑逐字节锚的权威依据 |
| [C-37] | Dropwizard Metrics 手册 https://www.dropwizard.io/projects/metrics/en/stable/manual/core/ ；ExponentiallyDecayingReservoir Javadoc | 监控分位数行业缺省 = 有界 reservoir（默认 1028 样本；SlidingWindowReservoir = 最近 N 次）——`deque(256)` p50 的正当性；t-digest/HDR 属高分位跨实例聚合场景（不适用） |
| [C-38] | Python importlib 文档（find_spec 条目）https://docs.python.org/3/library/importlib.html | find_spec 为官方推荐的免导入可用性检查；命名空间包假阳性由 CLI 层真导入 try/except + U7 降级吸收（顶层名 "rich" 无父包导入副作用） |
| [C-39] | docker compose v2 `cmd/compose/compose.go` https://github.com/docker/compose/blob/main/cmd/compose/compose.go ；moby/moby #40031 https://github.com/moby/moby/issues/40031 | compose 源码注释「probe **Err() (not Out())** because the renderer writes to stderr」——面板走 stderr、stdout 保留给机器消费（U13）的直接背书 |
| [C-40] | OpenTelemetry Handling sensitive data https://opentelemetry.io/docs/security/handling-sensitive-data/ ；Dash0/Better Stack redaction 指南 | 「尽早脱敏、理想在 instrumentation 层」+ fail-closed allowlist 为业界推荐——U22 源侧（sink 转发前）剥离的依据 |
| [C-41] | dagger #7057 https://github.com/dagger/dagger/issues/7057 ；#7045 https://github.com/dagger/dagger/issues/7045 ；Dagger Observability 文档 | 反例：批处理 CLI 押注重交互前端（BubbleTea）的 bug 尾巴（plain 流式两度回归、Windows 无输出）——U1 双区内联品类不变量经受反例检验 |
| [C-42] | rich issue #1024 https://github.com/Textualize/rich/issues/1024 ；discussion #3477 https://github.com/Textualize/rich/discussions/3477 | Live 刷新率与 CPU/闪烁的已报告经验（勿每事件 update）——U10 节流 tick 依据；辅证：本机实测 15 行 CJK 面板 0.146 ms/帧（5 Hz ≈ 0.07% 单核） |
