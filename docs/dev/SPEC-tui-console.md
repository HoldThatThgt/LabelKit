# 特性开发规格：Console 实时面板（TUI，spec v1.10）

> **状态：定稿（2026-07-17，规格先行——实现另行排期）**。需求方 2026-07-17 裁决：
> ① 立项形态 = **spec-only**（本文与 spec/*.md 修订随本次定稿落地，代码/CONTRACTS/
> manual/tests 属实施期，清单见 §3.8）；② U4 批准新增 `rich` 入依赖白名单；
> ③ U18 批准对 v1.9 T16「进度行固定键集」的有界修订；④ U14 心跳默认 0（关）；
> ⑤ **U15 一期实施键盘交互（推翻提案的暂缓推荐）**——键位规格见 §3.4。
> U1–U18 全部闭合，无待裁决项。编号 U1–U18（决策，裸写）沿用提案；工业调研引用
> [C-1]–[C-20] 表以 `PROPOSAL-tui-console.md`（保留为调研原始记录）为准，本文按
> 承重点引用。仓库表面事实经独立勘察复核（M12 事件目录 26 事件/11 通道、report
> 计数器全量清单、密钥池/熔断状态面、T16 键集裁决、估算扫描 I/O 取舍）。

## 1. 结论与形态

**品类判定**（提案 §3 调研收敛，[C-1]–[C-20]）：LabelKit `run` 是一次性批处理，
正确品类是 **双区内联实时面板**（buck2 superconsole / BuildKit `--progress=tty`
形态）——日志在上方照常滚动、保留 scrollback，终端底部画布按节流频率原地重绘；
运行结束画布定格为静态终版摘要。全屏 alternate-screen（k9s/htop/textual 品类）
与交互 REPL（Claude Code/Codex 品类）均否决（U1）。

**定位**：面板 = spec §7.7「进度显示面」的三态增强实现，是 M12 可观测性的第四个
纯消费面（前三个：stderr 运行日志、trace、report.json）。零业务耦合、**零 §7.2
事件目录改动**、report.json 零新键；渲染器物理上归 CLI 层
（`labelkit/cli/console.py`），经 `ProgressListener` 协议（common 层定义）接收
M10/M12 进程内推送；依赖方向 `cli → orchestration → operators → common` 不变。

三态 `console.mode = "auto" | "rich" | "plain"`（config.toml `[console]` 节 +
CLI `--console`）：**plain 与 v1.9 现行 stderr 输出逐字节等价（回归锚，
`heartbeat_s = 0` 默认下）**；auto 判定链见 §3.1；rich 档渲染库 = `rich`
（U4 已批：懒 import、仅 CLI 层单文件触点，连带纯 Python 传递依赖
markdown-it-py + pygments；textual 属应用框架级、评审否决）。

## 2. 设计裁决记录（U1–U18）

| # | 问题 | 裁决 | 依据 |
|---|---|---|---|
| U1 | 终端 UI 品类 | 双区内联面板；**永不进 alternate screen** | 批任务保 scrollback；全屏丢历史反证 [C-16]；superconsole/BuildKit 同型 [C-1][C-4] |
| U2 | 架构定位 | M12 第四消费面，纯 sink；渲染器归 CLI 层，协议归 common | bazel UI 锁教训——UI 与处理解耦 [C-6]；分层依赖方向不变 |
| U3 | 一期交互范围 | 渲染 + **键盘开关**（U15 裁决并入）；Ctrl-C 语义不动 | 需求方 2026-07-17（U15）；键位规格 §3.4 |
| U4 | 渲染库 | **`rich` 批准入 §2.6 依赖白名单**（懒 import；仅 `labelkit/cli/console.py` 触点） | 需求方 2026-07-17 批准；pip vendored 背书 [C-9]、同品类先例 Curator/distilabel [C-15][C-20]、CJK 宽度正确；手写 ANSI 备选作废 |
| U5 | 模式开关 | 三态 `--console auto\|rich\|plain`；auto 判定链 §3.1 | BuildKit `--progress` 五档收敛 [C-4]、buck2 `--console` [C-3]、tqdm 非 TTY 语义 [C-11] |
| U6 | 信息纪律（红线） | 面板 = stderr 镜像同级：只显示计数/枚举/profile 名/密钥**环境变量名**/file:line 结构字段；**无数据内容、无 LLM 自由文本（reason/task_name/critiques）、无 record id** | spec §7.1 ① 红线；比 trace `none` 档更严 |
| U7 | 失败语义（红线） | 渲染异常自吞 + 一次性 WARN + 当场降级 plain 续跑；**永不影响退出码/数据产出** | 记录级隔离精神（§2.6）；渲染是旁路 |
| U8 | 结束行为 | 画布最后一次重绘后定格（`transient=false` 语义），scrollback 留完整日志 + 终版面板 | 批任务产物可贴工单；superconsole 同行为 [C-1] |
| U9 | 布局 | 六区块（标头/批进度/段棋盘/状态账/LLM/中断态，§3.2）；stream/stitch 键仅启用时在场 | report.counts 口径对齐；Nextflow dim/bold 层次手法 [C-12] |
| U10 | 刷新模型 | 事件回调只做 O(1) 内存累加；重绘由节流 tick 驱动（默认 5 Hz）读原子快照；`LLMClient.snapshot()` 只读增量 | bazel 临界区教训 [C-6]；uv 闪烁缓冲 [C-10]；rich Live refresh 语义 [C-13] |
| U11 | stage 粒度信号 | 进程内 `MetricsSink.stage_begin(stage, batch_no)` 仅转发 listener，**不产生 TraceEvent、不入 §7.2 目录** | §7.7 既有定性「进度显示不属于日志」；trace 契约只增不改原则零触碰 |
| U12 | 段进度分母 | 复用 dry-run 静态估算公式（`orchestrator._estimate()` 同款），UI 标「估算/下界」 | 分母现成零新逻辑；S22/R28 下界口径沿用 |
| U13 | run 之外的面 | validate --probe / dry-run / 终版摘要表格化仅 rich 档；`rubric --show` 恒 plain（stdout 机器消费） | stdout/stderr 职责分离现状 |
| U14 | plain 非 TTY 心跳 | `console.heartbeat_s` **默认 0（关）**——保逐字节回归锚；开启时单行数据无关汇总、固定间隔 | 需求方 2026-07-17；terraform/bazel CI 教训 [C-7][C-17] |
| U15 | 键盘交互 | **一期实施**（需求方 2026-07-17，推翻提案暂缓推荐）：buck2 式 stdin 开关，键位封闭集 §3.4；仅 rich ∧ stdin TTY ∧ `console.interactive=true` ∧ termios 可用时生效 | buck2 toggles 先例 [C-3]；raw-mode 复杂度以「cbreak + finally 恢复 + 非阻塞轮询零新线程」封顶 |
| U16 | 全屏交互品类 | 不做；未来 O5 `labelkit analyze` trace 浏览器立项时另行评估（届时重估 textual） | 8.3 O5 现状；本期非目标 |
| U17 | 批总数分母 | UI 模态恒显示 i/N（IngestPlan 配对扫描廉价；stream 批数 = next-fit 仿真精确）；文本模态默认 `批 i` 无分母，`console.estimate = true` 显式换一遍输入 I/O 买 i/N + ETA | M10 现注释明确回避文本行数估算的双倍输入读（orchestrator.py:200-204）；不打破现状 |
| U18 | T16 有界修订 | **批准**：rich 面板状态账展示 stitched/threads（与 report.counts 口径对齐）；plain 进度行与文本版终版摘要键集**逐字节不动**——T16 的「固定键集」约束收窄为 plain 面专属 | 需求方 2026-07-17；spec §1.6 v1.10 行登记本修订 |

## 3. 规格正文

### 3.1 模式判定链与降级矩阵

```
auto → rich 当且仅当：stderr.isatty()
                   ∧ tool.log_format == "text"
                   ∧ 未设 NO_COLOR（no-color.org [C-18]）
                   ∧ TERM 不为 "dumb"/空
                   ∧ rich 可导入（懒 import 成功）
其余一律 plain。
```

| 情形 | 行为 |
|---|---|
| `--console rich` 但非 TTY | 尊重显式档（buck2 `super` 同义 [C-3]）——CI 录 ANSI 回放场景 |
| `tool.log_format = "jsonl"` | **强制 plain，不可被 `--console rich` 覆盖**（§7.7 铁律：stderr 逐行可 `json.loads`）；显式冲突时 M1 打 WARN |
| 渲染期任何异常 | 自吞 + 一次性 WARN + 当场降级 plain 续跑（U7 红线） |
| 终端宽 < 60 列 | 画布退化为现行单行 `\r` 等价形态 |
| `NO_COLOR` / `TERM` | 定性为**终端能力探测**（与 isatty 同级），非配置通道——不违反「除 API key 外无环境变量」（§2.5）；故意不设 `LABELKIT_CONSOLE` 类环境变量 |
| plain 档 | `emitter._progress()` 单行 `\r` 与 `_print_summary()` 文本版原样执行——与 v1.9 逐字节等价（`heartbeat_s=0` 时；回归锚） |
| rich 档 | 停用 `emitter._progress()`（信息被面板超集覆盖）与 `_print_summary()` 文本版（换表格版，数值来源不变） |

### 3.2 面板布局与数据源（rich 档）

运行中（stderr；上方滚动区照常输出运行日志，行文本与 plain 逐字节一致；下方画布
按 `console.refresh_hz` 原地重绘）：

```
2026-07-17T01:21:12+08:00 WARN  ingest  batch=1 bad_line file=s2.jsonl line=17 reason=missing_text_field
2026-07-17T01:23:40+08:00 INFO  emitter batch=2 批 2 落盘：主输出 +18 行（累计 41），rejects +1（累计 3）
────────────────────────────────────────────────────────────────────────────────────
 labelkit run · f3a9c04b7d21 · process/ui/stream+stitch · seed 42 · 已用 04:12 · ETA ~06:40
 project examples/thread/project.toml → out/threads.jsonl

 批 3/5  ██████████████░░░░░░░░░░  记录 96/160 (scanned)

 段  segment ✓   stitch ✓   dedup ✓   extract ▶ 18/46   quality ·   annotate ·   verify ·

 账  emitted 41   dup 3   lowq 5   verify 1   failed 0   noise 2   absorbed 88   stitched 2   threads 5

 LLM  default  在途 4/4  calls 213  重试 7  tok 412k↑ 96k↓  $0.83  p50 2.1s
      judge    在途 2/4  calls 46   重试 0  tok 88k↑ 12k↓   $0.19  p50 3.4s
      密钥 LABELKIT_KEY_A ok · _B 冷却12s · _C 禁用          熔断 0/20
 [?]帮助 [l]LLM展开 [e]错误条 [p]暂停 [q]脱离
────────────────────────────────────────────────────────────────────────────────────
```

| 区块 | 内容 | 数据源（全部为既有结构字段，零新增采集） |
|---|---|---|
| 标头 | run_id、mode/modality（stream/stitch 徽标）、seed、耗时、ETA（仅分母可得时显示，U17；EMA 吞吐外推标 `~`） | ResolvedConfig；`run.start` |
| 批进度 | UI 模态 `批 i/N` + scanned；文本模态默认 `批 i`（U17，`console.estimate=true` 换分母） | `batch.start/end` 事件 + `IngestPlan`/`_estimate()` 复用 |
| 段棋盘 | 仅启用 stage 按链序展示；`✓` 本批已过 / `▶` 进行中（该 stage `llm.call` 完成数 / 估算分母，标「估算」）/ `·` 待走 | `stage_begin` 回调（U11）+ `llm.call` 按 stage 累计 + `_estimate()` 分母（U12） |
| 状态账 | 九态计数，stream/stitch 键仅启用时在场，同 report.counts 口径（stitched/threads 展示 = U18 有界修订）；批内随批末更新（counts.* 为 post-emit tally） | MetricsSink counters |
| LLM | 每 profile：在途/上限、calls、retries、tok ↑↓、成本（未配价目显示 `—`）、p50 延迟；密钥池行（环境变量**名** + ok/冷却剩余/禁用）；熔断 `fatal_streak/threshold`，打开时整行红 + 顶部横幅 | `LLMClient.snapshot()` 每 tick 一次（§3.3）+ `llm.*` 事件 |
| 键位提示行 | 交互启用时恒显示一行（`?` 展开全表） | §3.4 |
| 中断态 | SIGINT 后画布顶部横幅「正在优雅中断（≤30s）…」 | `on_stop_requested` 回调 |

`generate_only`：生成阶段批进度区退化为 `生成 ▶ calls 87/120 · 已产 348 条`
（`llm.call` + `counts.generated`），批棋盘自再流批次起激活。

运行结束：最后一次重绘后定格（U8）——终版摘要表（counts 逐项 = report.json）+
per-stage 耗时横条 + llm_usage 小表 + rejects/trace 路径行。

### 3.3 架构与协议（CONTRACTS 待并文本，实施期入 §8）

```
 stages ──RunContext.metrics──▶ MetricsSink.event() ──▶ EventLog(trace 文件)     （既有，不动）
                                      │       └────────▶ stderr 镜像(logging)    （既有，不动）
                                      │（v1.10 新增旁路，进程内）
                                      ▼
                              ProgressListener 协议 ◀── M10 stage 循环 stage_begin
                                      ▲ 实现
                               ConsoleRenderer（labelkit/cli/console.py）
                                      │ 只读拉取（每 tick 一次）
                               LLMClient.snapshot()
```

```python
# labelkit/common/observability/obslog.py（v1.10 增）
class ProgressListener(Protocol):
    """进程内进度旁路——非 trace 面：不产生 TraceEvent、不受 channels 过滤、
    不经 7.4 脱敏（消费纪律 = stderr 镜像同级：实现只得读取标量结构字段）。
    全部回调必须 O(1)、无 I/O、无锁等待；重绘由实现方自己的节流 tick 驱动。"""
    def on_event(self, ev: TraceEvent) -> None: ...
    def on_stage(self, stage: str, batch_no: int) -> None: ...
    def on_stop_requested(self) -> None: ...

class MetricsSink:
    def __init__(self, cfg, run_id, event_log,
                 listener: ProgressListener | None = None): ...   # v1.10 只增
    def stage_begin(self, stage: str, batch_no: int) -> None: ... # 仅转发 listener

# labelkit/common/runtime/llm_client.py（v1.10 增，只读）
@dataclass(frozen=True)
class KeySnapshot:
    env: str                                  # 环境变量名——唯一可展示身份
    state: Literal["ok", "cooldown", "disabled"]
    cooldown_remaining_s: int = 0

@dataclass(frozen=True)
class ProfileSnapshot:
    name: str
    in_flight: int
    max_concurrency: int
    calls: int
    retries: int
    prompt_tokens: int
    completion_tokens: int
    est_cost_usd: float | None
    p50_latency_ms: int | None                # 有界样本窗（256 次）中位数
    keys: tuple[KeySnapshot, ...]             # 池 =1 时单元素

class LLMClient:
    def snapshot(self) -> tuple[ProfileSnapshot, ...]: ...   # 纯读，无锁竞争敏感段

# labelkit/orchestration/runtime.py（v1.10 只增）
def execute_run(config_path, project_path, overrides,
                listener: ProgressListener | None = None) -> int: ...
```

- **回调纪律**（bazel 教训 [C-6]）：listener 回调只做内存累加；tick 读取**原子整体
  替换的快照 dict**，杜绝撕裂与对事件源的反压。
- **日志路由**（rich #3286 规避 [C-13]）：ConsoleRenderer 启动时把 `labelkit`
  logger 的 `StreamHandler` 流临时指到 Live 滚动区代理、停止/降级时恢复
  `sys.stderr`。Formatter 不动 ⇒ 日志行文本与 plain 逐字节一致。依赖集内无裸 fd
  写 stderr 的行为（httpx 走 logging，lastResort 动态解析 `sys.stderr`，天然过代理）。
- M10 在 `_process_batch` 的 stage 循环里、每 stage `run()` 之前调
  `metrics.stage_begin(stage.name, batch_no)`；SIGINT/SIGTERM 的 `_request_stop`
  转发 `on_stop_requested`。

### 3.4 键盘交互（一期，U15 需求方裁决）

生效条件（合取）：rich 档 ∧ `sys.stdin.isatty()` ∧ `console.interactive = true`
∧ `termios` 可用（POSIX）。任一不满足 ⇒ 纯渲染（键位提示行不显示）；Windows
（无 termios）一期纯渲染，msvcrt 支持列演进（§5）。

| 键 | 行为 |
|---|---|
| `?` / `h` | 帮助行展开/收起（列出全部键位） |
| `l` | LLM 面板展开/收起：展开 = 每密钥一行（env 名、状态、calls、rate_limited） |
| `e` | 最近错误条开/关：环形最近 5 条 `error` 事件的 `stage + kind`（§7.6 封闭词表，数据无关） |
| `+` / `-` | 画布行数上限增/减（4–16 行；默认自适应终端高） |
| `p` | 暂停/恢复画布重绘（日志照常滚动；调试器/复制粘贴友好——Curator 退化开关的交互版 [C-15]） |
| `q` | **面板脱离**：本次运行余下时间降级 plain（不终止运行、不影响退出码） |

- 键位为**封闭集**，未列键一律忽略；**Ctrl-C 不被面板消费**——SIGINT 仍走 M10
  优雅中断路径（3.10.3），面板只显示中断横幅。
- 终端状态纪律：进入 `tty.setcbreak`（非全 raw——保留 ISIG，Ctrl-C 产生 SIGINT
  的语义因此天然不变），退出/降级/异常路径经 `finally` 以保存的 `termios` 属性
  恢复；键盘轮询在渲染 tick 内以非阻塞 `select` 完成，**零新线程**。
- stdin 被占用的代价与 buck2 相同（粘贴的后续命令会被面板吞）——`console.interactive = false`
  即 buck2 `--no-interactive-console` 的对应物 [C-3]。

### 3.5 配置面（config.toml `[console]` 节，工具级）

面板是部署环境属性（本机终端 vs CI），归 config.toml；project.toml 零改动。
优先级不变：CLI `--console` > config.toml > 内置默认。

```toml
[console]                    # v1.10 全部可缺省
mode = "auto"                # "auto" | "rich" | "plain"（判定链 7.7）
refresh_hz = 5               # rich 画布重绘频率，1–10（越界 = CONFIG_ERROR）
heartbeat_s = 0              # 仅 plain 非 TTY：每 N 秒一行数据无关心跳；0 = 关（默认，U14）
estimate = false             # 仅文本模态：启动估算扫描换批总数分母 + ETA（多读一遍输入，U17）
interactive = true           # rich ∧ stdin TTY 时启用键盘开关（3.4）；false = 纯渲染
```

CLI 增量：`labelkit run … [--console {auto,rich,plain}]`；`validate` 同参共用
（probe 结果表的 rich 呈现）。`CliOverrides` 增 `console: str | None = None`。
M1 校验：`mode`/`refresh_hz` 枚举与范围；`log_format="jsonl"` ∧ 显式
`mode="rich"` ⇒ WARN + 强制 plain；`heartbeat_s < 0` / `refresh_hz` 越界 =
CONFIG_ERROR。心跳行格式（数据无关，固定键集）：
`heartbeat batch=3 stage=quality llm_calls=182 elapsed=312s`。

### 3.6 约束对齐（spec §2.6 逐条）

| 约束 | 对齐 |
|---|---|
| stderr 永不含数据内容/提示词/密钥 | U6 红线：面板与心跳行均为计数/枚举/结构字段；密钥只显示环境变量名 |
| 无数据持久化 | 面板状态纯内存、不写文件；结束定格只是 stderr 输出 |
| 可复现性 | 渲染不消费 `run.seed` PRNG、不影响任何采样路径；`console.estimate` 只读输入不改流水线 |
| 记录级隔离 / 退出码 | U7 红线：渲染异常自吞降级；exit 0/1/2/3/4 语义零变化 |
| 依赖面 | `rich` 入白名单（U4 已批，§2.6 修订随本次落地）；懒 import、operators/common 零 rich 触点 |
| 报告只含计数 | report.json 零新键；面板是 report 的实时预览 |
| 无环境变量（API key 除外） | `NO_COLOR`/`TERM` 定性为终端能力探测（§3.1）；不新设环境变量 |

### 3.7 测试与验收（实施期）

| 层 | 要求 |
|---|---|
| 渲染快照（离线） | `Console(width=100, force_terminal=True)` 定宽渲染 → 字符串快照断言（喂 MetricsSink 计数器状态而非 LLM 响应——不违反「禁 mock LLM」指令）。覆盖：九态账、密钥池三态行、熔断横幅、中断横幅、窄终端单行退化、generate_only 形态、`l`/`e` 展开态 |
| 回归锚（验收级） | `--console plain` 对 examples 六工程实跑，stderr 与 v1.9 基线**逐字节 diff 为空**（heartbeat 默认关）；`log_format="jsonl"` 下 stderr 逐行 `json.loads` 恒真且显式 rich 被拒并 WARN |
| 降级注入 | 打桩使渲染 tick 抛异常 → 断言运行照常完成、退出码不变、恰一条 WARN、自动转 plain |
| 键盘交互 | 伪 TTY（`pty` 标准库）注入键序：`q` 脱离后 stderr 回到 plain 行式且运行继续；`p` 暂停期间日志仍滚动；退出后 `termios` 属性恢复与进入前逐字节一致；Ctrl-C 在 cbreak 下仍触发 SIGINT 优雅中断 |
| 协议契约 | ProgressListener 全回调 O(1) 无 I/O（审查项）；`listener=None` 路径（validate/全部既有测试）零行为变化；`snapshot()` 在并发 gather 中调用不阻塞事件循环 |
| 实跑目检 | examples/thread（stream+stitch 全开，信息最全）与坏密钥场景（P2-3 复现——面板须在 10 秒内红出密钥禁用与熔断横幅） |

### 3.8 文件修改清单

**本次已落地（规格面，随本文定稿）：**

| 文件 | 改动 |
|---|---|
| `spec/00-frontmatter.md` | 文档版本 v1.10、版本历史增行（规格定稿、实现另行排期注记） |
| `spec/10-ch1-overview.md` | 需求映射表增 v1.10 行；§1.6 增「Console 实时面板（v1.10 对齐，2026-07-17）」决策块（含 U18 对 T16 的有界修订登记） |
| `spec/20-ch2-overall-design.md` | §2.4 run/validate 增 `--console`；§2.5 config.toml 内容行增 console；§2.6 依赖面增 `rich` |
| `spec/50-ch5-config-spec.md` | §5.1 增 `[console]` 五键行 + 示例块 |
| `spec/70-ch7-logging.md` | §7.1 输出面表 ③ 行更新；§7.7 重写为三态 console 规格（判定链/画布/键盘/心跳/降级/T16 收窄）；§7.8 增 console 测试行 |
| `spec/312-m12-logging.md` | 3.12.1 边界句 + 3.12.3 增 ProgressListener 旁路段 |
| `docs/dev/PROPOSAL-tui-console.md` | 状态行改指本文（保留为调研原始记录） |
| `CLAUDE.md` / `AGENTS.md` | 仓库状态行同步 v1.10（规格先行注记；两文件逐字节一致） |

**实施期（另行排期；动工前以本表为纲）：**

| 层 | 文件 | 改动 |
|---|---|---|
| 依赖 | `pyproject.toml` | 增 `rich` |
| CLI | `labelkit/cli/console.py`（新） | ConsoleRenderer：Live 画布、快照渲染、键盘轮询（cbreak + select 零新线程）、日志流接管/恢复、降级 |
| CLI | `labelkit/cli/parser.py` / `commands.py` | `--console` 参数（run/validate）；构造 renderer 传入 `execute_run` |
| 编排 | `labelkit/orchestration/runtime.py` | `execute_run(..., listener=None)` 装配 |
| 编排 | `labelkit/orchestration/orchestrator.py` | stage 循环 `stage_begin`；`_request_stop` 转发；估算分母导出复用 |
| common | `labelkit/common/observability/obslog.py` | `ProgressListener` 协议 + MetricsSink 旁路（ctor 只增参数） |
| common | `labelkit/common/runtime/llm_client.py` | `KeySnapshot`/`ProfileSnapshot`/`snapshot()` + p50 有界样本窗 |
| common | `labelkit/common/config/model.py` / `loader.py` | `ConsoleConfig` 五键 + `CliOverrides.console` + M1 校验；注意 `ResolvedConfig` 全必填风格——增字段波及全部直接构造点（v1.9 先例 ~19 个测试文件） |
| 算子 | `labelkit/operators/emitter.py` | `_progress`/`_print_summary` 按 mode 让位（plain 原样） |
| 契约 | `docs/CONTRACTS.md` §8 | §3.3 待并文本入册（ProgressListener/snapshot/execute_run 签名） |
| 测试 | `tests/cli/test_console.py`（新）、`tests/common/observability/`、`tests/common/runtime/test_llm_client.py` | §3.7 各层 |
| 手册 | `docs/manual/06-config-toml.md`、`15-cli.md`、`16-observability.md` | `[console]` 键表、`--console`、面板说明（实跑摘录同步；ch.3/15 既有 dry-run/进度样例经非 TTY 采集 = plain 逐字节不变故不动） |
| 指南 | `CLAUDE.md` / `AGENTS.md` | 依赖清单与 CLI 行随实现落地再同步 |

## 4. 非目标

- 不做 web/hosted viewer（Curator 路线 [C-15]）——违反「数据只去配置声明的 LLM
  端点、无遥测」（§2.6）。
- 不做面板内数据内容检视（excerpt/prompt）——trace `excerpt`/`full` 档职责（U6 红线）。
- 不改 §7.2 trace 事件目录、report.json 结构、退出码/输出通道语义。
- 不做跨运行历史面板/持久化仪表——无状态原则。
- 不做全屏交互 trace 浏览器——O5 `labelkit analyze` 议题（8.3）。

## 5. 风险与演进

| 项 | 说明 |
|---|---|
| Windows 键盘交互 | 一期 termios-only（无 termios ⇒ 纯渲染）；msvcrt 轮询列演进候选，见到真实 Windows 交互需求再立项 |
| stdin 占用 | 交互启用时粘贴的后续命令被面板吞（buck2 同边界 [C-3]）；`console.interactive=false` 一键回避 |
| 渲染库供应链 | rich 为纯 Python + 两个纯 Python 传递依赖；懒 import 保证 rich 缺失/损坏时 plain 档零影响 |
| p50 样本窗内存 | 每 profile 有界 deque(256) × int，数量级可忽略；不入 report（报告零新键不变） |
| 心跳默认值回访 | U14 默认关；若无人值守长批的「CI 像死机」投诉成为常态，回访默认值（改默认 = 打破逐字节锚，须再走需求方裁决） |
| O5 联动 | `labelkit analyze` 立项时重估 textual 与全屏品类（U16）；面板经验（快照渲染/键位）可迁移 |
