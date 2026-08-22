# Sequence generation examples

本目录是 v1.18 序列生成的完整教学工程。旧的 blueprint、tier、sequence rule 和机械 weaver 配置已经删除；
这里从“先声明可判定世界，再交付共享世界的反事实序列”开始学习。

```mermaid
flowchart LR
    A[pattern / instruction-only] --> B[GenerationProgram]
    B --> C[CP-SAT ScenarioPlan]
    C --> D[ScenarioSeed 与逐事件状态执行]
    D --> E[positive / missing / reordered / timeout]
    E --> F[pattern / state / semantic 独立判定]
    F --> G[quality / annotate / verify]
    G --> H[main + stream + report]
    H --> I[manifest-last commit]
    H --> J[普通 process replay]
```

## 工程入口

| project | 学习目标 | checker |
|---|---|---|
| `project.toml` | 显式 pattern、共享世界、四种反事实、双噪声与 replay | `check_output.py` |
| `project-instruction-only.toml` | 不声明 role/order/gap，由完整 instruction 与世界状态约束序列 | `check_output.py --instruction-only` |
| `project-frame-only.toml` | 关闭 sequence annotation，只保留逐帧 annotation | `check_output.py --frame-only` |
| `project-replay.toml` | 把最终 stream 当普通输入，验证 segment/noise/dedup | `check_output.py --replay` |

`config.toml` 只保存 DeepSeek profile 和密钥环境变量名。所有工程使用 Anthropic 兼容入口、
`deepseek-v4-flash`、`supports_structured_output = false` 和 `thinking = "disabled"`。

## 先做不读取密钥的计划验证

从本目录运行：

```bash
mkdir -p out
uv run labelkit validate --config config.toml --project project.toml --console plain
uv run labelkit run --config config.toml --project project.toml --dry-run --console plain
```

这两条命令会编译正式运行使用的同一份 `GenerationProgram` 和 `ScenarioPlan`，但不会读取 API key value、
消耗 slot attempt 或替换正式产物。主例的冻结算术是：

| 对象 | 数量 |
|---|---:|
| counterfactual sets | 2 |
| variants per set | 4 |
| primary sequences | 8 |
| primary events | 22 |
| noise events | 2 |
| replay events | 3 |
| stream rows | 27 |

## 阅读 declared 主例

`project.toml` 的阅读顺序是：

- `[class.ticket_booking.generate]` 声明完整 state Schema、ScenarioSeed catalog 与世界构建指令；
- `[generate.pattern.booking_success]` 声明 role 顺序和最大总跨度；
- 每个 `[generate.pattern.booking_success.role.*]` 声明 actor、frame class、状态读写/发布权限与 payload binding；
- role gap 显式声明相邻事件的时间区间；
- `[[generate.counterfactual_sets.variants]]` 声明 positive、missing、reordered 与 interval-exceeded；
- `[generate.timeline]` 冻结 session、timestamp、noise 与 replay 布局；
- `[generate.noise].topics` 按 ordinal 一对一声明两个不同话题。

主例中的 noise 话题固定为“夜空中的月相观察”和“手工面包出炉时的香气”。renderer 只能表达当前
`NoiseSlot.topic`；独立 evaluator 必须同时确认与任务无关、没有可执行诉求、表达自然、忠实计划话题。
文字 MinHash 只作为后置近重门，不替代语义话题判定。

四个变体作为一个 attempt transaction 交付。任一变体、状态重放、独立判定或下游处理失败，整个 set 都不进入
main、stream、dedup index 或 dataset counters；下一次 attempt 从共享世界重新生成完整四分支。

## 运行真实主例与回放

把 `LABELKIT_DEEPSEEK_KEY` 放在仓库根目录 git-ignored 的 `.env`，只加载到当前 shell：

```bash
set -a
source ../../.env
set +a
uv run labelkit run --config config.toml --project project.toml --console plain
uv run python check_output.py
uv run labelkit run --config config.toml --project project-replay.toml --console plain
uv run python check_output.py --replay
```

主 checker 从用户可见工件证明八条 sequence、四种目标违规、main/stream 双向 owner 对账、hidden sentinel
不泄漏、三事件 replay provenance、report/manifest digest 和 27 行精确组成。它不声称从产物读取未公开的 state patch；
patch 重放由真实集成测试从内存 `EventTrace` 独立验证。

replay checker 要求同一 27 行 stream 精确得到 9 个 episode、25 个 absorbed primary frame、2 个
`dropped_noise`、1 个 exact duplicate 和 8 个 emitted sequence。噪声误入 episode 或 replay 未被普通 M3 命中都会失败。

## 学习 instruction-only

`project-instruction-only.toml` 不声明 pattern、role、order、gap 或 variant。它仍显式声明 state Schema、actor、
frame class、长度和世界构建 instruction；LLM 在这些边界内选择事件内容与顺序，状态执行和独立语义门仍然存在。

```bash
uv run labelkit validate --config config.toml --project project-instruction-only.toml --console plain
uv run labelkit run --config config.toml --project project-instruction-only.toml --console plain
uv run python check_output.py --instruction-only
```

checker 要求一条三事件序列，并证明公开 truth 不伪造 declared pattern、variant 或 expected violation。

## 学习 frame-only

`project-frame-only.toml` 关闭 segment 和 sequence annotation，开启 pointwise quality 与 `frame.annotate`。
先验证静态调用预算，再运行真实端点：

```bash
uv run labelkit validate --config config.toml --project project-frame-only.toml --console plain
uv run labelkit run --config config.toml --project project-frame-only.toml --dry-run --console plain
uv run python check_output.py --frame-only --static
uv run labelkit run --config config.toml --project project-frame-only.toml --console plain
uv run python check_output.py --frame-only
```

checker 要求一条三帧 sequence 精确交付，main 的 sequence annotation 为 null、sequence resolved-at 计数为零，
每个 main member 与对应 primary stream 行的 frame annotation 相同且通过
`schemas/frame-annotation.json`。任一帧缺失、失败或 Schema 非法都会使整个 slot attempt 失败。

## 产物真值

成功运行先原子替换 main、stream 与 report，最后写 manifest。只有 manifest 中的 run ID、delivery digest、
路径、SHA-256 和行数都与正式文件一致，消费者才应把这一代视为完整提交。`*.failed.report.json` 是失败诊断，
不能覆盖或否定上一代仍由 manifest 指向的成功四件套。
