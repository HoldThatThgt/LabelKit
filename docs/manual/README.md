# LabelKit 用户手册

> 一本教科书式的使用手册：循序渐进、每章带真实示例与解读，既可通读也可随手翻查。
> 对应 LabelKit 1.0.0。所有运行输出均来自真实执行，照做可复现。

## 这本手册怎么读

- **第一次接触**（30 分钟）：第 1 → 2 → 3 章，跑通第一个工程；
- **准备接自己的数据**：第 4 → 5 → 6 → 7 → 8 章，这是使用者的必修课；
- **想把效果调到最好**：Part IV 逐算子精讲 + 第 16 章的调优闭环；
- **数据是多类混合的**（v1.7）：第 24 章——先分类，再按类覆盖质量线与标注指令，一套全局配置治不了的数据就这么治；
- **数据是按时间采集的操作流**（v1.8）：第 25 章——会话化 → 语义分段 → 动作摘取，把屏幕帧流切成一段段可标注的 episode；
- **操作流里的任务被来回切换切碎了**（v1.9）：第 26 章——把被穿插切开的 episode 碎片按任务线索保守缝合成完整记录，短段救援、接缝占位、错缝零容忍的验收法都在这一章；
- **抄作业**：Part VI 五篇教程从易到难——教程一从空目录手搭（数据与配置全在文中），教程二至四基于仓库内 `examples/`（text、ui、generate）三个可运行工程，教程五是生产级配置模板与运维纪律（虚构场景，供改造套用）；
- **随手查**：附录 A 全参数速查、第 18 章按症状排障、附录 B 默认准则全文。

## 目录

### Part I　入门

| 章 | 标题 | 一句话 |
|---|---|---|
| 1 | [LabelKit 是什么](01-what-is-labelkit.md) | 问题、设计信念、算子总览与边界 |
| 2 | [安装与环境准备](02-install.md) | uv 安装、API 密钥安置、probe 体检 |
| 3 | [五分钟上手](03-quickstart.md) | 跑通第一个标注工程并读懂产物 |

### Part II　核心概念

| 章 | 标题 | 一句话 |
|---|---|---|
| 4 | [记录、批、状态与流水线](04-concepts.md) | 状态机、守恒等式、开关组合、两份配置的分工 |
| 5 | [准备你的数据](05-data-preparation.md) | JSONL 规矩、UI 文件配对、排布建议与自查清单 |

### Part III　配置参考

| 章 | 标题 | 一句话 |
|---|---|---|
| 6 | [config.toml 完全解读](06-config-toml.md) | LLM/embedding profile 逐参数 |
| 7 | [project.toml 完全解读](07-project-toml.md) | run/input/output/trace 逐参数 + 算子节速览 |
| 8 | [读懂四个产物](08-outputs.md) | 主输出、_meta、拒绝通道、report.json 逐字段 |

### Part IV　算子详解（每章：直觉 → 原理 → 配置 → 调优 → 误区）

| 章 | 标题 | 一句话 |
|---|---|---|
| 9 | [去重 dedup](09-dedup.md) | 精确/MinHash/pHash/语义四层与 UI 合成判定 |
| 10 | [质量 quality](10-quality.md) | pairwise 锦标赛 vs pointwise 刻度尺、质量门、rubric 设计 |
| 11 | [标注 annotate](11-annotate.md) | 提示词模板、instruction 写法、self-consistency |
| 12 | [生成 generate](12-generate.md) | 种子自举、两种纯生成形态、多样性三旋钮与桶统计 |
| 13 | [校验 verify](13-verify.md) | 独立评审、drop/repair、多评审团 |
| 14 | [结构引擎](14-schema-engine.md) | 四层防线与「模型容易答对」的 Schema 编写指南 |
| 24 | [分类 classify](24-classify.md) | 类别表分拣、按类覆盖打分与标注参数、multi 扇出（v1.7 追加章） |
| 25 | [时序流 stream](25-stream.md) | 会话化、滑窗语义分段、动作摘取与序列标注（v1.8 追加章） |
| 26 | [线索缝合 stitch](26-thread.md) | 单调选池两遍缝合、短段救援、接缝占位与线索三级结构（v1.9 追加章） |

### Part V　运行与运维

| 章 | 标题 | 一句话 |
|---|---|---|
| 15 | [CLI 完全参考](15-cli.md) | 三个子命令、五个退出码、标准工作流 |
| 16 | [可观测性](16-observability.md) | 双通道日志、trace 事件表、rubric 调优闭环 |
| 17 | [性能与成本调优](17-tuning.md) | 调用账/时间账/内存账与调优决策表 |
| 18 | [故障排查](18-troubleshooting.md) | 错误码全表 + 按症状 FAQ |

### Part VI　实战教程（难度递增）

| 章 | 标题 | 练什么 |
|---|---|---|
| 19 | [教程一：最小工程 ★](19-tutorial-1-minimal.md) | 从空目录搭出纯标注流水线 |
| 20 | [教程二：质量门实战 ★★](20-tutorial-2-quality.md) | 画线三步法、换 rubric 口径、top_ratio |
| 21 | [教程三：UI 全流程 ★★★](21-tutorial-3-ui.md) | 配对机关、双通道去重、视觉标注与评审 |
| 22 | [教程四：从零合成数据集 ★★★](22-tutorial-4-generate.md) | generate_only 两形态、桶统计验收 |
| 23 | [教程五：生产级配置 ★★★★](23-tutorial-5-production.md) | 评审团、strict、归档纪律与五指标周报 |

### 附录

| | 标题 | 一句话 |
|---|---|---|
| A | [全参数速查表](appendix-a-cheatsheet.md) | 所有配置键 + 组合约束，一页查全 |
| B | [默认 Rubric 全文与解读](appendix-b-default-rubrics.md) | 两套内置准则的原文、适用性判断与改造通路 |

## 三条最重要的提醒（来自真实踩坑）

1. **开跑前先 `validate --probe`**——密钥错误（401/403）会立即熔断（退出码 4），但模型名拼错这类 400/404 错误在小数据量下仍可能「成功」地全军覆没（退出码 0、failed 计满），probe 一次把两类问题都提前暴露（第 2、15 章）；
2. **pairwise 的分数是批内百分位**——threshold 是相对线、跨批不可比、均值恒为 0.5；要绝对刻度用 pointwise（第 10 章，教程三有实例）；
3. **trace 在首个事件写出时才截断**——死于配置/输入校验的运行与 dry-run（报告写 `{stem}.dryrun.report.json`、trace 写 `{stem}.trace.dryrun.jsonl`）都不会碰上一次的账本；正常重跑仍会覆盖，正式产物按批次命名、trace 及时归档（第 8、16 章）。
