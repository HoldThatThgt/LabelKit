# 计划书：Console 实时面板（TUI）——运行监控的第四消费面

> 2026-07-17。需求：「deep-search 各种工业项目，为 LabelKit 设计一个 TUI 方案」。
> **状态：已定稿——见 `SPEC-tui-console.md`（spec v1.10，2026-07-17 需求方裁决：
> spec-only 定稿、U4 批 rich、U18 批 T16 有界修订、U14 心跳默认关、U15 键盘交互
> 一期实施——最后一项推翻本文原推荐）。本文保留为需求与调研原始记录**；
> U1–U18 的生效裁决以 SPEC §2 为准，凡与本文不一致处以 SPEC 为准。
> 引用 [C-1]–[C-20] 均为 2026-07-17 真实检索核实；仓库表面经独立勘察复核
> （M12 事件目录 26 事件/11 通道、report 计数器全量清单、密钥池/熔断状态面）。

## 1. 结论先行

**LabelKit `run` 是一次性批处理，不是常驻交互系统——正确的 TUI 品类是
「双区内联实时面板」（buck2 superconsole / Docker BuildKit 形态），不是全屏
alternate-screen 应用（k9s / htop 形态）**：日志在上方照常滚动、保留 scrollback，
终端底部一块固定画布按节流频率原地重绘，展示批进度、流水线阶段棋盘、状态账、
LLM 用量/密钥池/熔断状态；运行结束画布收敛为静态终版摘要。

方案一句话：**TUI = spec §7.7「进度显示面」的增强实现，是 M12 可观测性的第四个
纯消费面**（前三个：stderr 运行日志、trace 事件流、report.json）——零业务耦合、
零事件目录改动（`trace_schema_version=1` 只增不改原则不动）、渲染器物理上归
CLI 层（`labelkit/cli/console.py`），经 `ProgressListener` 协议（定义于 common 层）
被 M10/M12 推送，依赖方向 `cli → orchestration → operators → common` 不变。
渲染库推荐 `rich`（pip 官方供应商化、同品类先例 Bespoke Curator、CJK 宽度正确
处理），三态开关 `--console auto|rich|plain`，非 TTY / `log_format="jsonl"` /
`NO_COLOR` / 渲染异常一律自动降级 plain——**plain 模式与 v1.9 现行 stderr 输出
逐字节等价（回归锚，与 stitch off 同文化）**。

```
现行 §7.7:  TTY 单行 \r 进度（emitter._progress）+ 非 TTY 每批一行摘要 + 终版摘要
本方案:     TTY 多行实时面板（canvas 原地重绘，日志上方滚动）+ 非 TTY 行为不变
            + validate/dry-run/结束摘要的表格化呈现（仅 rich 模式）
```

## 2. 现状事实（勘察结论，file:line）

| 事实 | 证据 | 设计含义 |
|---|---|---|
| spec §7.7 已把「进度显示」定义为**不属于日志**的第三输出面：直接写 stderr、无级别概念；TTY 进度条 / 非 TTY 批摘要；`log_format="jsonl"` 时禁用（保证 stderr 逐行可 `json.loads`） | `spec/70-ch7-logging.md:137` | TUI 有现成的规格落点——增强 §7.7 而非新开日志通道；jsonl → 强制 plain 是既有铁律 |
| 现行进度实现 = 单行 `\r` 重写：批号 + 五状态计数；`sys.stderr.isatty()` 与 jsonl 双闸；§7.7 承诺的「总批数、瞬时成本累计」现实现**未兑现**（emitter 注释明示 accepted reduction）；且 v1.9 T16 把进度行/终版摘要钉为**固定键集（不含 stitched）** | `labelkit/operators/emitter.py:544-566`；`SPEC-activity-structure.md` T16 | 唯一被 TUI **替换**的现存代码面（rich 模式下停用，plain 模式逐字节保留）；面板兑现总批数/成本两项；展示 stitched/threads 需对 T16 做有界修订（U18） |
| 终版摘要两行 counts + 「与 report.counts 逐项一致」 | `emitter.py:568-582` | rich 模式渲染为表格；数值来源不变 |
| M12 是全局事件漏斗：所有 stage 经 `RunContext.metrics` 发事件，`MetricsSink.event()` 单点构造 TraceEvent → EventLog + stderr 镜像 | `labelkit/common/observability/obslog.py:306-322` | TUI 订阅点唯一且现成——挂 MetricsSink 旁路 listener 即可覆盖全部事件 |
| stderr 镜像只打 payload 标量、从不打 record_ids 与嵌套内容 | `obslog.py:324-349` | 面板信息纪律的既有基线：计数/枚举/结构字段，无数据内容 |
| 事件目录 §7.2 为稳定契约（只增不改）；`run.*`/`batch.*` 生命周期事件不受通道过滤；`llm.call` 每调用一事件（debug 镜像恒有） | `spec/70-ch7-logging.md:19,45` | 面板的调用级进度可**免新增事件**——直接消费既有 `llm.call`/`batch.*` 流 |
| 批内无 stage 生命周期事件；M10 在 stage 循环里已逐 stage 计时（`add_stage_time`） | `labelkit/orchestration/orchestrator.py:385-403` | 「当前批走到哪个 stage」需一个**进程内回调**（不入 trace 契约，见 U11） |
| `--dry-run` 已实现全链静态调用量估算：`segment_calls = Σ⌈(L−1)/(w−1)⌉`、`quality_calls`、`stitch_calls` 等，stream 模式批数精确（next-fit 仿真） | `orchestrator.py:972-1076` | stage 进度条的**分母现成**（标注「估算/下界」即可复用，零新逻辑） |
| LLM 侧可观测状态齐备：`ProfileUsage`（calls/tokens/retries/est_cost_usd/parked）、`KeyUsage`、`_KeyState`（in_flight/cooldown_until/disabled）、per-profile `Semaphore`、熔断计数在 MetricsSink | `labelkit/common/runtime/llm_client.py:83-135,445`；`obslog.py:357-370` | LLM 面板只差一个只读 `snapshot()` 聚合方法（小 API 增量，入 CONTRACTS） |
| 运行装配单点在 `execute_run`（orchestration 层），CLI 仅传参 | `labelkit/orchestration/runtime.py:31-79` | 渲染器由 CLI 构造、经参数注入——orchestration 不 import cli |
| `[tool]` 仅 log_level/log_format 两键；全仓库唯一 isatty 检查在 emitter；无任何颜色/宽度处理、无 rich/textual 引用 | `labelkit/common/config/model.py:10-13`；grep 全仓 | `[console]` 配置节从零新增；工具级（config.toml 侧） |
| 约束：除 API key 外**无环境变量**；三方依赖白名单（httpx/jsonschema/datasketch/Pillow+imagehash/json-repair/numpy）+「无框架级依赖」；stderr 永不含数据内容/提示词/密钥 | spec §2.5/§2.6/§7.1 | `NO_COLOR`/`TERM` 只能定性为**终端能力探测**（与 isatty 同级）而非配置通道（§4.3）；新增渲染依赖必须修订 §2.6 并留需求方裁决（U4） |
| E2E 已知痛点 P2-3：坏密钥下「静默全灭、exit 0」——失败只有翻 rejects/counts 才能发现 | `docs/dev/E2E-FINDINGS.md` §3 | TUI 的直接动机之一：failed 计数、密钥禁用、熔断状态在面板常驻可见 |
| 路线图 O5：`labelkit analyze` 读 trace 产出诊断报告（未立项） | `spec/80-ch8-nongoals-roadmap.md:26` | 全屏交互式 trace 浏览器属 O5 的未来形态，**不在本期**（U16） |

## 3. 业界调研（2026-07-17 检索核实）

### 3.1 品类地图——四类终端 UI，LabelKit 属于哪一类

| 品类 | 代表 | 形态 | 适用前提 | 对 LabelKit |
|---|---|---|---|---|
| ① 双区内联面板 | buck2 superconsole、BuildKit `--progress=tty`、Nextflow ANSI log、cargo/uv | 日志上方滚动 + 底部画布原地重绘；非 TTY 自动退化为纯行式 | 一次性批任务、需保留日志 scrollback | **本方案采用** |
| ② 单/多行进度条 | pip（rich）、tqdm、pnpm | 一至数行 `\r` 或 rich Progress | 单一维度进度（下载/安装） | 现状即此（单行）；信息容量不足 |
| ③ 全屏 alternate-screen | k9s、htop、lazygit、Harlequin（textual） | `\x1b[?1049h` 接管整屏，无 scrollback | **常驻**监控/交互系统 | 否决（U1）：批任务用全屏丢日志历史——甚至催生了给全屏 TUI 补录回放的第三方工具 twatch [C-16] |
| ④ 交互 REPL | Claude Code（Ink/React 重度魔改）、Codex CLI（ratatui） | 输入框 + 流式输出 60fps | 人机对话循环 | 否决：品类不符；其性能工程规模（cell 级 diff/双缓冲）反证此路投入不成比例 [C-19] |

### 3.2 双区式的工业实证（本方案直接模板）

**buck2 superconsole**（Meta，Rust）[C-1][C-2][C-3]：把终端分为两区——底部
canvas 每 tick 覆盖重绘（组件化、渲染与状态分离、可测试性为第一设计目标），
上方 emitted 区排队打印一次性日志行、向上滚动。控制台形态五档
`--console auto|simple|simplenotty|simpletty|super|none`，**auto = stderr 是 TTY
时用 superconsole，否则 simple**。交互 toggles（`?` 帮助、`+`/`-` 增减行数等）
仅当 stdin 是 TTY 时启用，可用 `--no-interactive-console` 关闭 [C-3]。

**Docker BuildKit**（Go）[C-4][C-5]：`--progress=auto|plain|tty|quiet|rawjson`
五档（`--console=true/false` 因语义混淆被社区改名为 `--progress`，命名教训
[C-5]）；tty 模式每个活跃 step 默认露出 6 行日志（`BUILDKIT_TTY_LOG_LINES`）；
颜色可经 `BUILDKIT_COLORS` 定制、`NO_COLOR` 一票禁用 [C-4]。

**Nextflow**（数据流水线，与 LabelKit 最近的运行形态）[C-12]：2018 年用
「动态汇总视图」替换逐任务日志刷屏（ANSI log）；`NXF_ANSI_LOG` 默认 =
检测到 ANSI 终端；支持 `NO_COLOR` 与 `TERMINAL_WIDTH` 强制宽度；2024 年配色
重设计的手法值得抄：**次要信息 dim、关键标识 bold、状态用红绿蓝——让人不读数字
就能感知进度**；2026 年新增 `NXF_AGENT_MODE`——面向 AI agent 的标签化极简行式
输出（`[PIPELINE]`/`[PROCESS]`/`[WARN]`/`[ERROR]`/`[SUCCESS|FAILED]`），实现上
抽象出 `LogObserver` 接口、ANSI 与 Agent 两个观察者实现同源事件——与本方案
「M12 漏斗 + 多消费面」同构，佐证架构方向 [C-12]。

### 3.3 逆向教训（别人踩过的坑 → 本方案的规避条款）

| 教训 | 出处 | 规避条款 |
|---|---|---|
| UI 与处理耦合：bazel 的 `UiEventHandler` 在控制台临界区内做远端文件 I/O，锁成全局瓶颈；结论「UI 必须与实际处理解耦」 | bazel UI locking 复盘 [C-6] | 渲染器是纯 sink：回调只做内存累加，重绘在节流 tick 里做；面板永不做 I/O、永不阻塞事件源（§4.2） |
| 无光标控制时进度间隔线性漂移（10 分钟无输出），CI 长跑像死机 | bazel #16119 [C-7] | plain 非 TTY 已有每批一行 `batch.end` info；另设可选心跳行（U14），间隔固定不漂移 |
| 终端 resize 用启动时采样的旧宽度，tmux 缩窄后重复刷行 | bazel PR#29750 [C-8] | rich 每次渲染实测宽度（自带 SIGWINCH 等价处理）；窄于 60 列降为单行模式（§4.3） |
| 并发进度条闪烁；修法是缓冲/限频更新 | uv PR#3252 [C-10] | 固定 refresh 节流（默认 5 Hz，U10），事件只改内存快照、不触发即时重绘 |
| stdlib `StreamHandler` 在 rich Live 启动前捕获了原始 `sys.stderr` 引用，Live 的重定向对它失效、日志打穿画布 | rich #3286 [C-13] | 面板启动时显式接管 labelkit logger 的 handler 流、退出时恢复（§4.2 日志路由段）——日志行内容逐字节不变，只是改经画布上方滚动区输出 |
| 动态输出干扰调试器/管道，需一键退化 | Curator `CURATOR_DISABLE_RICH_DISPLAY` [C-15] | `--console plain` 显式档 + auto 的多重自动降级（§4.3） |
| 全屏 TUI 丢 scrollback，退出后历史蒸发 | k9s/alt-screen 语义、twatch 的存在 [C-16] | U1：永不进 alternate screen |
| CI 心跳刷屏惹恼用户，官方建议 roll-up 成「N operations still in progress」单行 | terraform TF_IN_AUTOMATION 讨论 [C-17] | 心跳（若启用）是单行汇总且频率固定（U14） |

### 3.4 同品类产品与渲染库供应链

- **Bespoke Curator**（LLM 合成数据管线，产品品类最近邻）：本地监控即 rich
  实时显示（tokens/请求统计），带 tqdm 退化开关；数据**内容**查看走另一独立
  Viewer 产品 [C-15]。启示：终端面板管「运行健康度」、数据内容检视另立门户
  ——LabelKit 的对应物是 trace + 未来 O5，不塞进面板（隐私红线也不允许）。
- **distilabel**：`display_progress_bar` 参数 + rich 管线信息 Panel [C-20]。
- **pip 供应商化 rich**（PR #10462）：PyPA 审查后整包 vendored，进度条全面
  rich 化 [C-9]——`rich` 供应链可信度的最强背书。**依赖足迹**：rich →
  `markdown-it-py` + `pygments`（纯 Python）[C-13]；textual → rich +
  `mdit-py-plugins` + `platformdirs` + `typing-extensions`（应用框架级）[C-14]。
- **tqdm**：`disable=None` 即非 TTY 自动禁用、默认写 stderr [C-11]——auto 档
  语义的事实标准。
- **textual 的自我定位**（生态一致结论）：「一次性命令要进度条用 rich；textual
  的复杂度只在**持续交互**（dashboard/浏览器/编辑器）时才值回票价」[C-14]。

## 4. 方案设计

### 4.1 形态与界面布局

运行中（rich 模式，stderr；上方为照常滚动的运行日志，下方画布 5 Hz 原地重绘）：

```
2026-07-17T01:20:04+08:00 INFO  run     batch=- run f3a9c04b7d21 开始 examples/thread
2026-07-17T01:21:12+08:00 WARN  ingest  batch=1 bad_line file=s2.jsonl line=17 reason=missing_text_field
2026-07-17T01:23:40+08:00 INFO  emitter batch=2 批完成 emitted=18 rejected=1        ← 滚动区（日志，逐字节同 plain）
────────────────────────────────────────────────────────────────────────────────────
 labelkit run · f3a9c04b7d21 · process/ui/stream · seed 42 · 已用 04:12 · ETA ~06:40
 project examples/thread/project.toml → out/threads.jsonl

 批 3/5  ██████████████░░░░░░░░░░  记录 96/160 (scanned)

 段  segment ✓   stitch ✓   dedup ✓   extract ▶ 18/46   quality ·   annotate ·   verify ·

 账  emitted 41   dup 3   lowq 5   verify 1   failed 0   noise 2   absorbed 88   stitched 2   threads 5

 LLM  default  在途 4/4  calls 213  重试 7  tok 412k↑ 96k↓  $0.83  p50 2.1s
      judge    在途 2/4  calls 46   重试 0  tok 88k↑ 12k↓   $0.19  p50 3.4s
      密钥 LABELKIT_KEY_A ok · _B 冷却12s · _C 禁用          熔断 0/20
────────────────────────────────────────────────────────────────────────────────────
```

要素与数据源（全部为既有结构字段，无一新增采集）：

| 区块 | 内容 | 数据源 |
|---|---|---|
| 标头 | run_id、mode/modality（+stream/stitch 徽标）、seed、耗时、ETA | ResolvedConfig；ETA = EMA 吞吐外推，标 `~`——**仅当批总数分母可得时显示**（U17） |
| 批进度 | UI 模态：批 i/N + scanned（估算分母天然廉价——配对来自目录扫描；stream 批数 = next-fit 仿真精确）。文本模态：**默认无分母**（`批 i`，cargo 未知总数形态）——行计数估算需全量读一遍输入，M10 现为省 I/O 明确回避（`orchestrator.py:200-204` estimate=False 注释）；`console.estimate = true` 显式用一遍输入 I/O 换 i/N 与 ETA（U17） | `batch.start/end` 事件 + `IngestPlan`/`_estimate()` 复用 |
| 段棋盘 | 仅启用 stage，链序展示；`✓` 本批已过 / `▶` 进行中（LLM 调用完成数/估算分母）/ `·` 待走；分母来自 dry-run 同款公式，悬浮标注「估算」 | U11 进程内 stage 回调 + `llm.call` 事件按 stage 累计 |
| 状态账 | 九态计数（stream/stitch 键仅启用时在场，同 report.counts 口径）；批内只随批末更新（counts.* 为 post-emit tally）。**T16 交互**：现行裁决把进度行钉为固定键集、stitched 永不入进度/摘要——rich 面板展示 stitched/threads 构成对 T16 的**有界修订**（仅限 rich 面；plain 进度行与文本版摘要键集逐字节不动），立项时须在 spec §1.6 登记（U18） | MetricsSink counters（emit 后 tally） |
| LLM | 每 profile：在途/上限、calls、retries、tokens、成本（未配价目显示 `—`）、p50 延迟；密钥池行（环境变量**名** + ok/冷却剩余/禁用）；熔断 fatal_streak/threshold，打开时整行变红 + 顶部横幅 | `LLMClient.snapshot()`（U10 新增只读方法）+ `llm.*` 事件 |
| 中断态 | SIGINT 后画布顶部显示「正在优雅中断（≤30s）…」 | M10 `_request_stop` 回调 |

运行结束：画布做最后一次重绘后**定格为静态输出**（rich `Live(transient=False)`
语义）——终版摘要表（counts 逐项 = report.json）+ per-stage 耗时横条 +
llm_usage 小表 + rejects/trace 路径行。scrollback 里留下完整日志 + 最终面板，
符合「批任务跑完要能贴进工单」的运维习惯（superconsole 同行为 [C-1]）。

`generate_only` 模式：生成阶段无批概念——批进度区退化为
`生成 ▶ calls 87/120 · 已产 348 条`（`llm.call` + `counts.generated`），
批棋盘自再流批次起激活。

### 4.2 架构——第四消费面，零事件目录改动

```
                    （既有，不动）
 stages ──RunContext.metrics──▶ MetricsSink.event() ──▶ EventLog(trace 文件)
                                      │       └────────▶ stderr 镜像(logging)
                                      │（新增旁路，进程内）
                                      ▼
                              ProgressListener 协议 ◀── M10 stage 循环回调
                              （common/observability 定义）      （批号/阶段名）
                                      ▲ 实现
                               ConsoleRenderer（labelkit/cli/console.py）
                                      │ 只读拉取
                               LLMClient.snapshot()（每 tick 一次）
```

- **协议放 common、实现放 CLI、调用在 orchestration**：`execute_run(...)` 增一个
  可选参数 `listener: ProgressListener | None`（CLI 构造 ConsoleRenderer 传入；
  validate/测试传 None）。依赖方向不变：orchestration 只见协议。
- **回调纪律（bazel 教训 [C-6]）**：listener 方法只做纯内存累加（O(1)，无锁无
  I/O）；重绘由渲染器自己的节流 tick 驱动（rich Live auto_refresh 线程），
  tick 读取的是**原子整体替换的快照 dict**，杜绝撕裂与对事件源的反压。
- **事件目录零改动**：面板消费的 `run.*`/`batch.*`/`llm.*`/`error` 均为既有事件；
  唯一的新信息「当前 stage」走 `MetricsSink.stage_begin(stage, batch_no)` →
  仅转发 listener、**不产生 TraceEvent**——落点正当性来自 §7.7 的既有定性：
  进度显示不属于日志（U11）。
- **日志路由（rich #3286 规避 [C-13]）**：ConsoleRenderer 启动时把 labelkit
  logger 那只 `StreamHandler` 的流临时指到 Live 的滚动区代理、停止时恢复
  `sys.stderr`。Formatter 不动 ⇒ **日志行文本与 plain 模式逐字节一致**，只是
  输出位置在画布上方。第三方库侧已核：依赖集内无直接 print 到 stderr 的行为
  （httpx 走 logging，其 lastResort handler 动态解析 `sys.stderr`，天然经过
  Live 的重定向代理）；绕过代理的裸 fd 写入不存在于当前依赖集，接受为已知边界。
- **替换关系**：rich 模式下停用 `emitter._progress()` 单行（信息被面板超集
  覆盖）与 `_print_summary()` 文本版（换表格版）；plain 模式两者原样执行。

### 4.3 模式判定与降级矩阵

三态 `console.mode = "auto" | "rich" | "plain"`（config.toml `[console]`，
CLI `--console` 覆盖）。**auto 的判定链**（借 BuildKit/tqdm/buck2 事实标准
[C-4][C-11][C-3]）：

```
auto → rich 当且仅当：stderr.isatty()
                   ∧ tool.log_format == "text"
                   ∧ 未设 NO_COLOR（no-color.org 标准 [C-18]）
                   ∧ TERM 不为 "dumb"/空
                   ∧ rich 可导入（懒 import 成功）
其余一律 plain。
```

| 情形 | 行为 |
|---|---|
| `--console rich` 但非 TTY | 仍尊重显式档（buck2 `super` 同义 [C-3]）——CI 录 ANSI 回放场景 |
| `log_format="jsonl"` | **强制 plain 且不可被 `--console rich` 覆盖**（§7.7 铁律：stderr 逐行可 `json.loads`）；冲突时 M1 打 WARN |
| 渲染期任何异常 | 吞掉 + 一次性 WARN + 当场降级 plain 续跑；**渲染永不影响退出码与数据产出**（U7） |
| 终端宽 < 60 列 | 画布退化为现行单行 `\r` 等价形态 |
| `NO_COLOR` / `TERM` | 定性为**终端能力探测**（与 isatty 同级），非配置通道——不违反「除 API key 外无环境变量」约束（§2.5）；故意不设 `LABELKIT_CONSOLE` 类环境变量，避免 Nextflow 的 env 优先级纠缠 [C-12] |

### 4.4 与既有非协商约束逐条对齐

| 约束（spec §2.6/§7.1） | 对齐 |
|---|---|
| stderr 永不含数据内容/提示词/密钥 | 面板信息纪律 = stderr 镜像同级（比 trace `none` 档更严）：只显示计数、枚举、profile 名、密钥**环境变量名**、file:line 结构字段；不显示 record id、excerpt、`task_name` 等任何 LLM 自由文本（U6） |
| 无数据持久化 | 面板状态纯内存；不写任何文件；结束定格只是 stderr 输出 |
| 可复现性 | 渲染不消费 `run.seed` PRNG、不影响任何采样路径 |
| 记录级隔离 / 退出码 | 渲染器异常自吞（U7）；exit 0/1/2/3/4 语义零变化 |
| 无框架级依赖 | rich 定性为「终端呈现库」而非应用框架（textual 才是框架，已否决）；仍属白名单修订，须 §2.6 增行 + §1.6 决策记录（U4，需求方裁决） |
| 报告只含计数 | report.json 不新增任何键；面板是 report 的**实时预览**而非扩展 |

### 4.5 配置面（config.toml `[console]`，工具级）

TUI 是部署环境属性（本机终端 vs CI），归 config.toml 侧；project.toml 零改动。

| 键 | 类型/默认 | 说明 |
|---|---|---|
| `console.mode` | `"auto"`（默认）\| `"rich"` \| `"plain"` | §4.3 判定链；CLI `--console` 覆盖 |
| `console.refresh_hz` | int，默认 `5`，范围 1–10 | 画布重绘频率（pip 用 5–6 [C-9]，rich 默认 4 [C-13]） |
| `console.heartbeat_s` | int，默认 `0`（关） | 仅 plain 非 TTY 生效：每 N 秒一行数据无关汇总心跳 `heartbeat batch=3 stage=quality llm_calls=182 elapsed=312s`（terraform/bazel CI 教训 [C-7][C-17]）；0 = 关（保回归锚） |
| `console.estimate` | bool，默认 `false` | 仅文本模态生效：是否在启动时做估算扫描（`Ingestor.scan(estimate=True)`，全量读一遍输入）以获得批总数分母与 ETA——I/O 代价即 M10 现注释回避的「双倍输入读」，故默认关；UI 模态分母天然廉价、恒显示，本键无效（U17） |

CLI 增量：`labelkit run ... [--console {auto,rich,plain}]`（validate 同参共用）。
精度顺位不变：CLI > project.toml（无此节）> config.toml。

### 4.6 run 之外的呈现面（同一 mode 开关统辖）

| 命令 | rich 模式 | plain 模式 |
|---|---|---|
| `validate --probe` | 每 profile×key 一行的结果表（ok 绿/FAIL 红、latency 列对齐） | 现行逐行 print，逐字节不变 |
| `run --dry-run` | 估算渲染为两张小表（记录/批数；八段 `*_calls` + total），下界注记为脚注样式 | 现行五行 print，逐字节不变 |
| `rubric --show` | **恒 plain**：stdout 输出 TOML 供机器消费，永不加彩 | 同左 |
| 结束摘要 | counts 表 + per-stage 耗时横条 + llm_usage 表 | 现行 `_print_summary` 两行，逐字节不变 |

### 4.7 技术选型

| 方案 | 依赖足迹 | 评估 | 裁决 |
|---|---|---|---|
| **rich（推荐）** | +`rich`、`markdown-it-py`、`pygments`（三者纯 Python，无二进制） | Live 双区模型与 superconsole 同构 [C-1][C-13]；pip 供应商化背书 [C-9]；同品类先例 Curator/distilabel [C-15][C-20]；**CJK/东亚宽度正确**（面板含中文，手写 wcwidth 极易踩宽度错位）；Windows 兼容白拿 | ✅ 推荐（U4，待需求方批白名单） |
| textual | +rich + `mdit-py-plugins`/`platformdirs`/`typing-extensions`，应用框架 | 为一次性批处理引入常驻交互框架不成比例；生态自身建议此场景用 rich [C-14]；违反「无框架级依赖」精神 | ❌ 否决（O5 analyze 立项时重估，U16） |
| 手写 ANSI 双区（superconsole 复刻） | 零新增 | 需自行处理：CJK 宽度、resize、光标控制兼容、Windows VT——估 600–1000 行长尾终端兼容代码，价值密度低（Meta 为此专门开库 [C-1]） | 备选：仅当需求方拒绝任何新依赖 |
| tqdm | +`tqdm` | 单维进度条品类（§3.1 ②），装不下段棋盘/LLM 面板 | ❌ 信息容量不符 |

落地纪律：`rich` **懒 import**——`console.mode` 判定为 rich 才导入，导入失败
自动降级 plain（不 raise）；核心管线路径零 rich 触点（operators/common 不
import rich，仅 `labelkit/cli/console.py` 一处）。

## 5. 触点清单（若立项的文件修改面）

| 层 | 文件 | 改动 |
|---|---|---|
| CLI | `labelkit/cli/console.py`（新） | ConsoleRenderer：Live 画布、快照渲染、日志流接管/恢复、降级 |
| CLI | `labelkit/cli/parser.py` / `commands.py` | `--console` 参数；构造 renderer 传入 `execute_run` |
| 编排 | `labelkit/orchestration/runtime.py` | `execute_run(..., listener=None)` 装配 |
| 编排 | `labelkit/orchestration/orchestrator.py` | stage 循环处 `metrics.stage_begin(...)`；估算分母复用导出 |
| common | `labelkit/common/observability/obslog.py` | `ProgressListener` 协议 + MetricsSink 旁路转发（event/stage_begin/count） |
| common | `labelkit/common/runtime/llm_client.py` | 只读 `snapshot()`（ProfileUsage 聚合 + 池态 + p50） |
| common | `labelkit/common/config/model.py` / `loader.py` | `ConsoleConfig` 四键 + 校验（jsonl×rich 冲突 WARN）；注意 `ResolvedConfig` 为全必填风格——增字段波及全部直接构造点（v1.9 先例 ~19 个测试文件） |
| 算子 | `labelkit/operators/emitter.py` | `_progress`/`_print_summary` 按 mode 让位 |
| 文档 | `spec/50-ch5-config-spec.md` §5.1、`spec/70-ch7-logging.md` §7.7、`spec/312-m12-logging.md`、`spec/20-ch2` §2.6 依赖行、`spec/10-ch1` §1.6 决策记录（含 T16 有界修订登记，U18） | `[console]` 节、§7.7 重写为三态面板规格、依赖白名单增行 |
| 文档 | `docs/CONTRACTS.md` §8 | ProgressListener 签名、LLMClient.snapshot、execute_run 签名 |
| 文档 | `docs/manual/06-config-toml.md`、`15-cli.md`、`16-observability.md` | `[console]` 键表、`--console` 与面板说明（需实跑摘录同步，仓库惯例；ch.3/15 既有 dry-run/进度样例经非 TTY 采集 = plain，逐字节不变故不动） |
| 文档 | `CLAUDE.md` / `AGENTS.md` | 依赖清单与 CLI 行同步（两文件逐字节一致） |
| 测试 | `tests/cli/test_console.py`（新）、`tests/common/observability/` | 见 §6 |

## 6. 测试与验收

- **渲染快照测试（离线，无 LLM——不违反「禁 mock LLM」指令：喂的是 MetricsSink
  计数器状态而非 LLM 响应）**：`Console(width=100, force_terminal=True)` 定宽
  渲染 → 字符串快照断言（rich 官方测试法；superconsole 同样以可测试性立身
  [C-1]）。覆盖：九态账、密钥池三态行、熔断横幅、中断态、窄终端单行退化、
  generate_only 形态。
- **回归锚（验收级）**：`--console plain` 下对 examples 六工程实跑，stderr 与
  v1.9 基线**逐字节 diff 为空**（心跳默认关）；`log_format="jsonl"` 下 stderr
  逐行 `json.loads` 恒真且 `--console rich` 被拒并 WARN。
- **降级注入**：渲染器构造后打桩使其 tick 抛异常 → 断言运行照常完成、退出码
  不变、恰一条 WARN、自动转 plain。
- **协议契约**：ProgressListener 全回调 O(1) 无 I/O（代码审查项）；listener=None
  路径（validate/既有测试）零行为变化。
- **实跑目检**：examples/thread（stream+stitch 全开，面板信息最全）与坏密钥
  场景（P2-3 复现——面板须在 10 秒内红出 `密钥 _A 禁用` 与熔断横幅）。

## 7. 决策清单（U1–U18）

| # | 决策 | 裁决 |
|---|---|---|
| U1 | 品类：双区内联面板；**永不进 alternate screen** | ✅ 推荐定案（§3.1/[C-16]） |
| U2 | 定位：M12 第四消费面，纯 sink 零业务耦合；渲染器归 CLI 层 | ✅ 推荐定案 |
| U3 | phase-1 **零键盘交互**（纯渲染；Ctrl-C 语义不变） | ✅ 推荐定案；toggles 见 U15 |
| U4 | 渲染库 = rich（懒 import、单文件触点）；修订 spec §2.6 依赖白名单 + §1.6 决策记录 | ⚠️ **待需求方**（新依赖）；拒绝则退手写 ANSI 备选（§4.7） |
| U5 | 三态 `--console auto\|rich\|plain`；auto 判定链见 §4.3 | ✅ 推荐定案 |
| U6 | 面板信息纪律 = stderr 镜像同级：无数据内容、无 LLM 自由文本、无 record id | ✅ 推荐定案（红线） |
| U7 | 渲染异常自吞降级；永不影响退出码/产出 | ✅ 推荐定案（红线） |
| U8 | 结束定格静态摘要（不清屏、留 scrollback） | ✅ 推荐定案 |
| U9 | 布局六区块（§4.1）；stream/stitch 键仅启用时在场 | ✅ 推荐定案 |
| U10 | 刷新 = 事件累加 + 节流 tick（默认 5 Hz）+ 原子快照；`LLMClient.snapshot()` 只读增量 | ✅ 推荐定案 |
| U11 | stage 粒度走进程内 `stage_begin` 回调，**不入 trace 事件目录**（§7.7 定性背书） | ✅ 推荐定案 |
| U12 | 段进度分母复用 dry-run 估算公式，UI 标「估算/下界」 | ✅ 推荐定案 |
| U13 | validate/dry-run/摘要表格化仅 rich 模式；`rubric --show` 恒 plain | ✅ 推荐定案 |
| U14 | plain 非 TTY 心跳行 `console.heartbeat_s`，**默认 0（关）** 保回归锚 | ⚠️ 默认值待需求方（CI 长批可见性 vs 逐字节锚） |
| U15 | phase-2 候选：stdin toggles（buck2 式 `+`/`-`/`?`）——引入 raw-mode 复杂度 | ⏸ 暂缓，见到真实需求再立项 |
| U16 | 全屏交互（textual 品类）只属未来 O5 `labelkit analyze` trace 浏览器；本期非目标 | ✅ 推荐定案 |
| U17 | 批总数分母：UI 模态恒显示 i/N（IngestPlan 廉价）；文本模态默认 `批 i` 无分母、`console.estimate = true` 显式换一遍输入 I/O（不打破 M10「避免双倍输入读」现状） | ✅ 推荐定案 |
| U18 | rich 面板展示 stitched/threads = 对 v1.9 T16「进度行固定键集」的有界修订（仅 rich 面；plain 行/文本版摘要键集不动）；须入 spec §1.6 决策记录 | ⚠️ **待需求方**（改既有已闭合裁决） |

## 8. 非目标

- 不做 web/hosted viewer（Curator 路线 [C-15]）——违反「数据只去配置声明的
  LLM 端点、无遥测」（§2.6）。
- 不做面板内数据内容检视（excerpt/prompt）——那是 trace `excerpt`/`full` 档
  的职责，红线见 U6。
- 不改 trace 事件目录、report.json 结构、任何退出码/输出通道语义。
- 不做跨运行的历史面板/持久化仪表——无状态原则。

## 引用

| # | 出处 |
|---|---|
| [C-1] | Meta Engineering Blog: *Superconsole, a TUI library written in Rust*（2022-07）——双区设计、非交互定位、可测试性 |
| [C-2] | docs.rs/superconsole——canvas/emitted 两区 API 语义 |
| [C-3] | buck2.build *Buck2 Consoles*——`--console` 五档、auto=TTY 判定、stdin toggles 与 `--no-interactive-console` |
| [C-4] | moby/buildkit README——`--progress` 五档、`BUILDKIT_TTY_LOG_LINES=6`、`BUILDKIT_COLORS`、`NO_COLOR` |
| [C-5] | docker/cli PR#1276——`--console` 改名 `--progress` 的语义混淆教训 |
| [C-6] | jmmv.dev *Bazel UI locking and file downloads*（2020）——UI 临界区做 I/O 的锁瓶颈复盘 |
| [C-7] | bazelbuild/bazel #16119——无光标控制时进度间隔线性漂移 |
| [C-8] | bazelbuild/bazel PR#29750——resize 后旧宽度重复刷行，SIGWINCH 补课 |
| [C-9] | pypa/pip PR#10462——vendor rich 并统一进度条 |
| [C-10] | astral-sh/uv PR#3252——并发进度条与闪烁缓冲 |
| [C-11] | tqdm 文档——`disable=None` 非 TTY 自动禁用、默认 stderr |
| [C-12] | nextflow env-vars 参考 + Seqera 配色博文 + `NXF_AGENT_MODE` 提交（LogObserver 抽象） |
| [C-13] | rich Live 文档（refresh/redirect/transient/vertical_overflow）+ Textualize/rich #3286（stdlib handler 与 Live 重定向） |
| [C-14] | textual 定位与依赖（pyproject）；生态共识「一次性命令用 rich、持续交互才用 textual」 |
| [C-15] | bespokelabsai/curator README——rich 本地监控、`CURATOR_DISABLE_RICH_DISPLAY` 退化、内容检视另立 Viewer |
| [C-16] | k9s / alternate-screen 语义；twatch（给全屏 TUI 补录回放的工具，反证全屏丢历史） |
| [C-17] | HashiCorp Discuss *Improving output in automation*——still-creating 心跳刷屏与 roll-up 建议；`TF_IN_AUTOMATION` |
| [C-18] | no-color.org——`NO_COLOR` 标准 |
| [C-19] | *Claude Code from Source* ch.13——Ink 魔改（cell 级 diff/双缓冲/60fps）：交互 REPL 品类的投入规模反证 |
| [C-20] | distilabel 文档——`display_progress_bar` 与 rich 管线 Panel |
