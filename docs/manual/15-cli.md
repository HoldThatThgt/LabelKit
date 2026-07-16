# 第 15 章　CLI 完全参考：三个子命令与五个退出码

> LabelKit 的命令行面只有三个子命令。本章逐个讲清它们的参数、行为与退出码约定，
> 并给出一套「从试到跑」的标准工作流。

## 15.1 `labelkit run`：执行流水线

```
labelkit run --config <config.toml> --project <project.toml>
             [--input PATH] [--output PATH]
             [--limit N] [--dry-run] [--strict]
             [--log-level debug|info|warn|error]
```

| 参数 | 作用 |
|---|---|
| `--config` / `--project` | 两份配置的路径，**必填** |
| `--input` / `--output` | 覆盖 `run.input` / `run.output`（CLI > project.toml）。注意 `generate_only` 模式下传 `--input` 同样是配置错误 |
| `--limit N` | 只处理前 N 条（N ≥ 1；0 或负数在参数解析层就被拒绝）。**试跑神器**：小样本验证配置、rubric、Schema、成本，再放开跑全量 |
| `--dry-run` | 走完全部启动校验 + 输入扫描 + 成本估算，**不发一次 LLM 调用、不写主输出**。报告写 `{stem}.dryrun.report.json`；trace 写「trace 文件名在扩展名前插 .dryrun」（默认即 `{stem}.trace.dryrun.jsonl`），不覆盖上次真实运行的账本 |
| `--strict` | 有任何记录被拒绝（dropped_* / failed 非零）⇒ 退出码 1。给 CI/定时任务用：让「有货被扔」成为可编程的失败信号。v1.9 交互补注：缝合产生的 `stitched` 壳与救援命中的帧**不构成 rejects**——同一份输入开启 `[stitch]` 后 strict 结果可能从 1 变 0（短段被救援、不再落 rejects），属预期（第 26 章） |
| `--log-level` | 覆盖 `tool.log_level`。`debug` 会打出每次 LLM 调用摘要（延迟/token/重试） |

`--dry-run` 的输出示例（拿来做预算审批正合适）：

```
dry-run: mode=process estimated_records=14 batches=1
dry-run: estimated LLM calls — generate_calls=0 segment_calls=0 stitch_calls=0 classify_calls=0 extract_calls=0 quality_calls=56 annotate_calls=14 verify_calls=0 total=70 (excludes retries and repair calls)
dry-run: no LLM calls made, no output written (report and trace only)
```

注意 `(excludes retries and repair calls)`——真实用量会比估算略高（结构修复、重试、verify 的 repair 轮都不在估算里）。配了 `price_per_mtok_*` 时可结合历史运行的 token 均值折算金额。`classify_calls` 是 v1.7 新增字段（分类算子，第 24 章），`segment_calls` / `extract_calls` 是 v1.8 新增字段（时序流，第 25 章），`stitch_calls` 是 v1.9 新增字段（线索缝合，第 26 章），未启用恒为 0；stream 模式下 quality/annotate/verify 的估算以「episode 数 ≈ 会话数」报**下界**、extract 按剔噪前帧数报**上界**（估算公式与真实对账见第 25 章）。`classify.assignment = "multi"` 时，quality/annotate/verify 的估算按每记录标签乘数 1 计——报的是**下界**（扇出后的实际调用数只多不少）；配了 `[class.*]` 按类覆盖时则一律按全局配置估算。后两种情况 stderr 都会多打一行注记（`dry-run: 注：按全局配置估算 / multi 按标签乘数 1 报下界`）。

## 15.2 `labelkit validate`：只体检不跑车

```
labelkit validate --config <config.toml> --project <project.toml> [--probe]
```

执行 M1 全量校验（TOML 语法、字段类型、profile 引用、Schema 元校验、rubric 校验、few-shot 示例校验、环境变量存在性），**校验通过输出 `配置校验通过`，退出码 0；不通过退出码 2**，且所有错误一次性列全：

```
ConfigError: 2 个配置错误（全量聚合反馈）
project.toml:[run].output: 缺失必填键，期望字符串（可用 CLI --output 提供）
project.toml:[quality].llm: 引用的 profile "gpt4" 不存在于 config.toml [llm.*]，可用：default、judge
```

`--probe` 追加连通性探测：对每个**被实际引用**的 profile 发一次 1-token 试调用（没被任何启用算子引用的 profile 不探测、也不要求密钥存在）：

```
配置校验通过
probe default: ok model=glm-5.2 latency_ms=7291
probe judge: FAIL HTTP 401: {"error":{"message":"token expired or incorrect",...}}
```

**注意：probe 失败不改变退出码**（仍为 0）——它是诊断信息，不是判决。脚本里要判 probe 结果得 grep 输出。为什么仍然值得写进 checklist：密钥错误（401/403）如今会立即熔断（退出码 4），但模型名拼错这类 400/404 错误在小数据量下仍可能「静默失败 + 退出码 0」（第 2 章）——probe 一次把两类问题都免费暴露。

**密钥池逐密钥探测（v1.6）**：profile 以 `api_key_envs` 声明了密钥池（第 6 章）时，`--probe` 会按声明序**对池内每把密钥各发一次** 1-token 试调用，每把密钥打一行——profile 名后以方括号标注该密钥的环境变量名（永远是变量**名**，密钥值不会出现在任何输出里）。单密钥 profile 的输出行格式不变：

```
probe <profile>: ok model=... latency_ms=...             # 单密钥 profile，同上例
probe <profile>[<ENV名>]: ok model=... latency_ms=...    # 池化 profile，每密钥一行
probe <profile>[<ENV名>]: FAIL <错误信息>
```

相应地，探测成本 = **各被引用 profile 的池大小之和**次试调用。两个判读提醒：

- **FAIL ≠ 密钥失效**。正被限流的密钥并没有「死」——运行期的 429 只会让该密钥进入冷却、由池内其余密钥顶上（第 6 章），但 probe 恰好打在冷却窗口里时照样是一行 FAIL。脚本 grep FAIL 时请区分错误内容：401/403 是密钥级确定性故障，429 只是暂时限流。probe 失败从不改变退出码的约定对逐密钥行同样成立——哪怕整池 FAIL，validate 仍以 0 结束；
- 密钥池下 401/403 的熔断语义也随之变化：运行期先按密钥禁用，仅当被禁用的是**最后一把**存活密钥才立即熔断（第 6 章）——所以「probe 里一把密钥 401、其余 ok」的池仍然能跑，只是白白少了一把密钥，建议开跑前修好。

## 15.3 `labelkit rubric`：导出内置评价准则

```
labelkit rubric                        # 列出可用的默认 rubric 名
labelkit rubric --show default:text    # 打印该 rubric 的 TOML 全文到 stdout
```

不带参数运行列出全部可用名（真实输出；`default:trajectory` 是 v1.8 随时序流新增的轨迹准则，第 25 章）：

```
default:text
default:ui
default:trajectory
```

标准用法是「导出为起点，改成自己的」：

```bash
uv run labelkit rubric --show default:text > my-rubric.toml
# 编辑 my-rubric.toml，然后并入 project.toml：设 quality.rubric = "inline"，
# 加顶层 [rubric] 表（其 name 键必填，可沿用导出文件里的 name），
# 再把各 [[criteria]] 改写为 [[rubric.criteria]] 粘入
```

输出是逐字节原样的包内文件（无重排、无附加换行），可直接复制。

## 15.4 退出码：让脚本读懂结局

| 码 | 含义 | 典型触发 |
|---|---|---|
| **0** | 运行完成 | 注意：**可能仍有被拒绝的记录**（看 stderr 摘要与 report.counts）；generate_only 产出 0 条也是 0 |
| **1** | 完成但违反 `--strict`，或报告写出失败 | 有 rejects 且开了 strict；主输出成功但 report.json 写不出 |
| **2** | 配置错误 | TOML 语法错、字段非法、引用的 profile 不存在、Schema/rubric 非法、环境变量缺失、非法 CLI 参数组合 |
| **3** | 输入错误（仅 process 模式） | 输入路径不存在、目录下没有候选文件（无 `.jsonl` / 无 `uitree_*`+`image_*`）、**无任何合法记录**（读完输入 `ingested=0`，如 text_field 全员未命中）、UI index 冲突且策略为 fail、坏行/缺对且策略为 fail |
| **4** | 致命运行错误 | 熔断触发（连续 `fatal_error_threshold` 次不可恢复 API 错误；v1.6 起熔断中止**交付**已完成批，见下方判读要点）、运行期输出写入失败、未预期异常、Ctrl-C 打在启动/收尾阶段（stderr 打印 `interrupted`；运行中的 Ctrl-C 则走优雅中断、正常交付并按 strict 规则返回 0/1） |

判读要点：

- **0 不等于「全部记录都成功」**——它只承诺流程走完、账目写清。生产脚本请用 `--strict` 或解析 report.counts；
- 2 与 3 的分界：**还没碰数据**的错都是 2（包括「输出父目录不存在或不可写」——忘了 `mkdir -p out` 是退出码 2，不是 4）；**数据本身**的错才是 3；
- 4 的几种触发里，只有**熔断**会走完收尾：报告照常写出（特征是 `run.exit_code: 4`；注意 `interrupted` 仍为 `false`——该字段只在 SIGINT/SIGTERM 中断时为 true），且 **v1.6 起已完成批次的主输出与 rejects 照常 fsync + 原子改名交付**（v1.5 及以前是「`.part` 残骸留在原地、不交付」——长跑末段配额死亡不再丢弃全部已完成产出）。此时 report.json 的 run 节带 `partial_delivery: true`（仅熔断交付时出现），counts 增列 `unprocessed` 补齐守恒等式（第 8 章）；运行期写盘失败与未预期异常则在收尾之前就抛出，连报告都不会写出；
- （v1.6）**最终文件名出现不再等价「全部输入处理完毕」**：它仍然保证已交付的每一行完整且合法，但熔断中止与优雅中断如今都会交付。判定一次运行是否完整处理了全部输入，要看 report.run 的 `interrupted=false` **且** `circuit_broken=false`——退出码本身不够用：被 SIGINT 优雅中断的运行同样交付且按 strict 规则以 0/1 退出（第 8、18 章）。

## 15.5 标准工作流：从零到全量

```bash
# ① 体检：配置合法 + 端点可达（不花钱）
uv run labelkit validate --config config.toml --project project.toml --probe

# ② 估算：多少条、多少次调用（不花钱）
uv run labelkit run --config config.toml --project project.toml \
    --dry-run --output out/dryrun.jsonl

# ③ 小样本试跑：验证 rubric/instruction/Schema 的实际效果（花小钱）
uv run labelkit run --config config.toml --project project.toml \
    --limit 20 --output out/pilot.jsonl
#    → 人工检查 out/pilot.jsonl 与 rejects；不满意就改配置回到 ③

# ④ 全量：正式输出（花正经钱）
uv run labelkit run --config config.toml --project project.toml

# ⑤ CI/生产变体：让"有淘汰"可被脚本感知
uv run labelkit run ... --strict; echo "exit=$?"
```

第 ③ 步是整个工作流的支点：**instruction、rubric、threshold 的每一轮修改都应该在 --limit 小样本上验证**，全量只跑定稿配置。这套流程在第 20 章的调优教程里会完整演练一遍。

## 15.6 stderr 上会看到什么

运行期间 stderr 的信息分三类（第 16 章细讲）：

- **运行日志**：生命周期与警告（`run.start`、`batch.end`、`ingest.bad_line`……），级别受 `--log-level` 控制；
- **进度显示**：仅 TTY 下有批级进度行（当前批号 + 各状态累计计数；总批数与成本累计未接入进度显示，成本看 report.json）。它不经日志设施、不受 `--log-level` 影响；`tool.log_format = "jsonl"` 时禁用，使经日志模块输出的运行日志可逐行 `json.loads` 解析——注意仍有少量**不经日志模块**的纯文本行（结束时的三行终版摘要、配置装载期的 `warning:` 行、`--dry-run` 的估算行），采集侧需容忍或过滤。非 TTY（重定向、CI）下进度显示不输出——此时看到的每批一行摘要实为 `batch.end` 事件的 INFO 日志行，属于运行日志、受 `--log-level` 控制；
- **结束摘要**：与 report.counts 逐项一致的终版对账单。

```
   ── 终版摘要（与 report.counts 逐项一致）──
   scanned=14  ingested=14  bad_input=0  generated=0
   dropped_dup=1  dropped_lowq=6  dropped_verify=0  failed=0  emitted=7
```

**stderr 永远不含数据内容与提示词**——可以放心接入任何日志采集系统。
