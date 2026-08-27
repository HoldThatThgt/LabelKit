# LabelKit 用户手册

> 这本手册按用户任务组织：先跑通普通流水线，再按需要进入流处理、平面生成或序列生成。
> 当前序列生成契约以 v1.20 为准；业务时间由 Planner 与框架机械写入，旧 stream、配置没有 alias、migration 或 fallback。

## 怎么读

- 第一次使用：第 1～3 章。
- 接入自己的数据：第 4～8 章。
- 调整算子：第 9～14、24～26 章。
- 运行、观测与排障：第 15～18 章。
- 从零合成独立样本：第 12、22 章。
- 从零合成可验证序列：第 27 章；它讲 declared、instruction-only、counterfactual set、精确交付和 replay。

## 目录

### Part I　入门

| 章 | 标题 | 一句话 |
|---|---|---|
| 1 | [LabelKit 是什么](01-what-is-labelkit.md) | 产品边界、算子总览与适用场景 |
| 2 | [安装与环境准备](02-install.md) | uv、API key 环境变量与 probe |
| 3 | [五分钟上手](03-quickstart.md) | 跑通第一个工程并读产物 |

### Part II　核心概念

| 章 | 标题 | 一句话 |
|---|---|---|
| 4 | [记录、批、状态与流水线](04-concepts.md) | 状态机、守恒关系与合法组合 |
| 5 | [准备你的数据](05-data-preparation.md) | 文本、UI 与时间序输入 |

### Part III　配置与输出

| 章 | 标题 | 一句话 |
|---|---|---|
| 6 | [config.toml](06-config-toml.md) | LLM 与 embedding profile |
| 7 | [project.toml](07-project-toml.md) | 一次任务的完整配置 |
| 8 | [读懂产物](08-outputs.md) | 主输出、stream、report、manifest、failed report 与 rejects |

### Part IV　算子

| 章 | 标题 | 一句话 |
|---|---|---|
| 9 | [去重 dedup](09-dedup.md) | exact、MinHash、图像与语义去重 |
| 10 | [质量 quality](10-quality.md) | pairwise、pointwise 与质量门 |
| 11 | [标注 annotate](11-annotate.md) | 指令、Schema 与 self-consistency |
| 12 | [生成 generate](12-generate.md) | flat 与 sequence 两种生成形式 |
| 13 | [复核 verify](13-verify.md) | 独立评审、drop 与 repair |
| 14 | [结构引擎](14-schema-engine.md) | 四层结构保证与 Schema 写法 |
| 24 | [分类 classify](24-classify.md) | 闭集分类与按类条件化 |
| 25 | [流模式 stream](25-stream.md) | session、episode、噪声与动作摘取 |
| 26 | [线索缝合 stitch](26-thread.md) | 穿插任务的保守重组 |
| 27 | [序列生成](27-sequence-generation.md) | pattern、世界状态、反事实、精确交付与 replay |

### Part V　运行与运维

| 章 | 标题 | 一句话 |
|---|---|---|
| 15 | [CLI](15-cli.md) | run、validate、rubric 与退出码 |
| 16 | [可观测性](16-observability.md) | 日志、trace、报告与 console |
| 17 | [性能与成本](17-tuning.md) | 调用、时间、内存与上限 |
| 18 | [故障排查](18-troubleshooting.md) | 错误码与症状路径 |

### Part VI　教程

| 章 | 标题 | 练什么 |
|---|---|---|
| 19 | [最小工程](19-tutorial-1-minimal.md) | 从空目录搭纯标注流水线 |
| 20 | [质量门](20-tutorial-2-quality.md) | rubric、阈值与 top ratio |
| 21 | [UI 全流程](21-tutorial-3-ui.md) | 文件配对、视觉标注与复核 |
| 22 | [从零合成独立样本](22-tutorial-4-generate.md) | flat generate-only 的两种输入形态 |
| 23 | [生产级配置](23-tutorial-5-production.md) | strict、归档与运维纪律 |

### 附录

| | 标题 | 一句话 |
|---|---|---|
| A | [全参数速查](appendix-a-cheatsheet.md) | 常用配置和组合约束 |
| B | [默认 Rubric](appendix-b-default-rubrics.md) | 内置准则全文与改造方法 |

## 三条操作纪律

- 先运行 keyless `validate` 和 `--dry-run`；需要端点连通性时再运行 `validate --probe`。
- 正式消费序列生成产物前校验 manifest 中 main、stream、report 的 SHA-256；failed report 不是成功真值。
- API key value 只留在环境变量中；不要把 `.env`、trace 高内容档或 full rejects 当普通日志分发。
