# LabelKit

LabelKit 是一个基于 LLM API 的单机、单进程、无状态批处理工具。它把输入数据的分段、缝合、去重、分类、
质量打分、结构化标注、生成与复核组织成可配置流水线，输出结构由 JSON Schema 定义并由代码侧校验。

```text
输入 JSONL / UI 文件对
  -> 可选分段与缝合
  -> 去重 -> 可选分类与摘取 -> 质量 -> 可选生成 -> 标注 -> 可选复核
  -> 主输出 + rejects + report + 可选 trace
```

## 核心能力

- 普通处理：文本 JSONL 或“截图 + UI 控件树”文件对进入同一套治理流水线。
- 流处理：按时间组织输入，形成 session 与 episode；可对穿插任务做保守缝合，并为 UI 相邻帧摘取动作。
- 结构保证：供应商结构化输出、确定性 JSON 修复、JSON Schema 校验和有界 LLM 修复组成完整防线。
- 平面生成：从种子池或纯指令生成独立样本，再回流去重、质量、标注与复核。
- 序列生成：命名 sequence pattern 明确 role、actor、状态权限与时间间隔；一个 counterfactual set 共享
  ScenarioSeed，并把 positive、missing、reordered、interval-exceeded 等变体作为一个整体重试和提交。
- 精确交付：规划在任何内容调用前冻结；最终下游结果投影为 main 与 stream，replay 从最终 source rows 派生；
  main、stream、report 成功后才最后提交 manifest。
- 可审计：报告只含计数、用量与摘要；API key value 不进入日志、trace、报告、manifest 或测试失败信息。

## 快速开始

```bash
uv sync
uv run labelkit --help
cd examples/text
mkdir -p out
uv run labelkit validate --config ../config.toml --project project.toml
uv run labelkit run --config ../config.toml --project project.toml --dry-run
```

`config.toml` 声明端点、模型、能力和 API key 的环境变量名；`project.toml` 定义一次任务的输入、输出、算子、
Schema 与指令。密钥值只通过环境变量进入运行时，不要写进配置或命令参数。

序列生成的教学工程在 `examples/sequence-generation`。不读取密钥、不写正式产物的验证路径是：

```bash
cd examples/sequence-generation
mkdir -p out
uv run labelkit validate --config config.toml --project project.toml --console plain
uv run labelkit run --config config.toml --project project.toml --dry-run --console plain
```

当前已验证的 keyless 计划是 2 个 counterfactual sets、8 条 primary sequences、22 个 primary events、
2 个 noise events 和 3 个 replay events，共 27 行 stream。两个 noise event 的话题按 ordinal 显式声明，
不再让模型自行碰运气区分主题。最终真实证据为：DeepSeek 主例、replay、instruction-only 与 frame-only checker
全部通过；DeepSeek sequence integration 5 passed in 119.26s；z.ai structured-output 1 passed in 60.81s；
完整真实端点套件 47 passed in 438.37s；52 条发布样本经两名独立评审均为 52/52，无系统性缺陷。

## 文档

- [用户手册](docs/manual/README.md)：从安装、配置和普通流水线，到 sequence generation 与 replay。
- [v1.18 序列生成规格](docs/dev/SPEC-sequence-generation-redesign.md)：当前序列生成的权威设计。
- [跨模块契约](docs/CONTRACTS.md)：冻结的数据结构、接口、事件和提示词边界。
- [实现规格](spec/)：模块职责、数据结构、配置、输出、日志与验收要求。
- [E2E 发现](docs/dev/E2E-FINDINGS.md)：已验证事实、环境失败和待补证据的分离记录。

## 开发

```bash
uv run --python 3.12 pytest -q -m 'not integration'
uv run --python 3.12 pytest tests/integration -q -m integration
git diff --check
```

离线基线为 2157 tests；当前 v1.18 离线套件已验证为 2606 passed、47 deselected。合并覆盖率为 line 95.71%、
branch 91.30%，1548/1548 个可执行生产函数已进入。真实 LLM 测试不使用 mock server、
mock transport、录制响应或本地替身。实现必须先服从 spec 与 `docs/CONTRACTS.md`，再由测试证明闭包。
