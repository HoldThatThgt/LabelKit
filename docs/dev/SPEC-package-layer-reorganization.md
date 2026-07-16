# LabelKit 包分层重构规范

状态：严格物理归档修订实施中

版本：v1.1

日期：2026-07-16

## 1. 目的

将当前平铺在 `labelkit/` 下的生产代码按职责整理为四个可识别的层：

```text
cli → orchestration → operators → common
```

本次重构只改变包的物理组织、导入路径和开发者导航，不改变 LabelKit 的运行语义、数据格式、错误码、Prompt、并发策略、输出格式或 canonical 模块中的公开函数签名。旧平铺 import path 被明确删除，这是本次唯一有意的破坏性开发者接口变化。

本文件是本次重构的执行契约。所有实施者必须以本文件和 `docs/CONTRACTS.md` 为准；如果实际代码与本文件或冻结契约冲突，停止并报告，不得自行发明替代设计。

## 2. 范围与非目标

### 2.1 必须完成

- 生产代码物理迁移到 `cli/`、`common/`、`operators/`、`orchestration/`。
- 将 CLI 参数解析、命令处理、运行时组装和算子实例化分离。
- 将公共契约、配置、运行时共享能力、可观测性和用户扩展点分开。
- 将所有生产代码内部导入切换到新 canonical path。
- 删除 `labelkit/` 根目录的全部旧平铺模块；根目录只允许 `__init__.py`。
- 删除旧 `labelkit/config/`，配置实现只允许位于 `labelkit/common/config/`。
- 不保留 re-export shim、模块别名或旧路径兼容层；旧 import 必须失败。
- 更新测试、`docs/CONTRACTS.md`、`AGENTS.md`、`CLAUDE.md` 和开发文档中的模块路径。
- 测试目录必须逐项镜像目标职责；`tests/common` 不得继续平铺。
- 增加目录形态和依赖方向回归断言，机械拒绝旧平铺文件复活。
- 通过离线测试、CLI smoke、构建检查和真实 endpoint integration 测试。
- 完成下文全部验收项后才允许创建 PR。

### 2.2 明确不做

- 不拆分 `loader.py`、`ingest.py`、`verify.py`、`orchestrator.py` 内部业务算法。
- 不改变任何业务算法或阶段顺序。
- 不改变 `Stage.run()` 签名、`RunContext` 六字段结构或 `PipelineItem` 状态机。
- 不新增缓存、临时数据持久化、遥测或新的外部依赖。
- 不把 `obslog`、`schema_engine` 或 `hooks` 混入配置加载逻辑。
- 不以“向后兼容”为理由保留任何旧平铺生产模块或测试文件。
- 不修改用户已有的未跟踪文件 `docs/dev/SPEC-activity-structure.md`。

## 3. 目标目录

```text
labelkit/
├── __init__.py
│   └── 版本号和 TOOL_VERSION
│
├── cli/
│   ├── __init__.py
│   │   └── 导出 main、build_parser、exit_code_for 等 CLI 公共符号
│   ├── main.py
│   │   └── 进程入口、异常到退出码映射、最终退出结果
│   ├── parser.py
│   │   └── argparse 参数定义和 CliOverrides 转换
│   └── commands.py
│       └── run、validate、rubric 命令的用户交互处理
│
├── common/
│   ├── contracts/
│   │   ├── types.py
│   │   │   └── Record、PipelineItem、UI 类型、共享 frame/tree helper
│   │   └── stage.py
│   │       └── Stage、RunContext 和阶段调用不变量
│   │
│   ├── errors.py
│   │   └── LabelKitError、ErrorKind、退出码常量和错误分类
│   │
│   ├── config/
│   │   ├── __init__.py
│   │   │   └── load、default_rubric、ResolvedConfig 公共出口
│   │   ├── model.py
│   │   │   └── 所有配置 dataclass
│   │   └── loader.py
│   │       └── TOML 读取、配置合并、字段校验、启动期 hook 校验
│   │
│   ├── runtime/
│   │   ├── llm_client.py
│   │   │   └── LLM/Embedding transport、重试、密钥池、并发、用量计量
│   │   └── schema_engine.py
│   │       └── L0-L3 结构化输出保证、JSON 修复、Schema 校验和 repair 统计
│   │
│   ├── observability/
│   │   └── obslog.py
│   │       └── stderr log、trace JSONL、事件、计数器、阶段耗时和 circuit breaker
│   │
│   └── extensions/
│       └── hooks.py
│           └── module:function 解析、用户 validator 执行和返回值规范化
│
├── operators/
│   ├── ingest.py
│   │   └── JSONL/UI 输入、配对、坏输入、stream sessionization
│   ├── segment.py
│   │   └── stream 会话边界、窗口判断、噪声帧和 episode 组装
│   ├── dedup.py
│   │   └── 精确、MinHash、pHash 和语义去重
│   ├── classify.py
│   │   └── 闭集分类、single/multi assignment 和 fan-out
│   ├── extract.py
│   │   └── 相邻 UI 帧动作提取
│   ├── quality.py
│   │   └── pairwise/pointwise 质量评分和质量门
│   ├── generate.py
│   │   └── 样本生成、种子池、validator 和相似度过滤
│   ├── annotate.py
│   │   └── 用户 Schema 标注、自一致性和标注修复
│   ├── verify.py
│   │   └── 标注审核、repair、episode member surgery
│   └── emitter.py
│       └── main output、rejects、report、sidecar 和原子交付
│
└── orchestration/
    ├── __init__.py
    ├── orchestrator.py
    │   └── 批处理、阶段执行、generation re-flow、生命周期和报告汇总
    ├── factory.py
    │   └── 根据 ResolvedConfig 实例化算子并固定 pipeline 顺序
    ├── profile_usage.py
    │   └── 计算 validate --probe 实际引用的 profile 集合
    └── runtime.py
        └── 组装配置、日志、LLM、SchemaEngine、Ingestor、Emitter 和 Orchestrator
```

`examples/stream/tools/gen_fixtures.py` 保持在示例目录；它不是生产包的一部分。

## 4. 归属和依赖规则

### 4.1 CLI

CLI 只负责：

- 解析命令行参数；
- 调用 orchestration 的公开运行入口；
- 将异常渲染到 stderr；
- 执行唯一的异常到退出码映射；
- 输出 `rubric` 命令的人类可读内容。

CLI 不得直接实例化算子、LLMClient、SchemaEngine、Emitter 或 Ingestor。

### 4.2 Common

`common` 是跨层共享能力，不包含具体数据处理业务。

- `common/contracts`：共享数据结构和阶段协议。
- `common/errors.py`：跨层错误词汇；不属于配置模块，因为错误涵盖输入、provider、schema、内部状态和熔断。
- `common/config`：启动期配置解析和校验；不得发起网络请求或读取输入记录内容。
- `common/runtime`：运行期 LLM transport 和结构化输出保证。
- `common/observability`：stderr 日志、trace、指标和熔断状态；不得依赖任何算子。
- `common/extensions`：用户 hook 的解析和执行辅助；配置加载器、SchemaEngine、GenerateStage 共用，不归配置目录所有。

Common 不得依赖 `operators` 或 `orchestration`。

### 4.3 Operators

算子只能依赖 common 和标准库/声明的第三方库。算子不得依赖 orchestration，也不得通过导入其他算子来取得业务逻辑。

以下是冻结契约允许的三个例外，必须保留并使用懒加载：

- `verify` 调用 `annotate` 的公开 repair surface；
- `verify` 调用 `segment.judge_window`；
- `verify` 调用 `extract.extract_transition`。

这些例外属于既有 stream repair contract，不得为了目录洁癖改写业务行为。

### 4.4 Orchestration

编排层可以依赖 common 和 operators，负责：

- stage 实例化和顺序；
- process、generate_only、dry-run 分支；
- batch 切分和 pending queue；
- signal、circuit breaker、生命周期清理；
- 报告汇总和最终退出结果。

编排层不复制任何算子算法，不实现 prompt、去重、评分、标注或验证业务。

## 5. 严格物理归档和 import 迁移

新目录是唯一 implementation path。迁移完成后，旧平铺文件和旧 `labelkit/config/` 必须从文件系统、Git 索引、wheel 和 sdist 中全部消失，不允许薄 shim、`sys.modules` alias、动态转发或其他兼容层。

### 5.1 唯一 canonical path

```text
labelkit.cli                  → labelkit/cli/ package
labelkit.common.contracts.*  → labelkit/common/contracts/
labelkit.common.errors       → labelkit/common/errors.py
labelkit.common.config.*     → labelkit/common/config/
labelkit.common.runtime.*    → labelkit/common/runtime/
labelkit.common.observability.obslog
                              → labelkit/common/observability/obslog.py
labelkit.common.extensions.hooks
                              → labelkit/common/extensions/hooks.py
labelkit.operators.*         → labelkit/operators/
labelkit.orchestration.*     → labelkit/orchestration/
```

`labelkit.cli` 是 canonical package 名，不属于旧路径；`labelkit/cli.py` 不得存在，console script 继续指向 `labelkit.cli:main`。

### 5.2 删除要求

- `labelkit/` 根目录的 Python 文件集合必须严格等于 `{__init__.py}`。
- `labelkit/config/` 必须不存在。
- `labelkit/types.py`、`stage.py`、`errors.py`、`llm_client.py`、`schema_engine.py`、`obslog.py`、`hooks.py`、`ingest.py`、`segment.py`、`dedup.py`、`classify.py`、`extract.py`、`quality.py`、`generate.py`、`annotate.py`、`verify.py`、`emitter.py` 和 `orchestrator.py` 必须不存在。
- canonical 模块中的 `TYPE_CHECKING`、懒加载和普通 import 必须全部使用新路径。
- `annotate_record`、`build_*_prompt`、`judge_window`、`extract_transition`、`RunContext`、`LLMClient`、`SchemaEngine` 等 direct-call surface 只在各自 canonical 模块保留既有签名和行为。
- 任何测试、文档或内部代码不得继续通过旧路径 import；`tests/common/test_compat_imports.py` 必须删除。

## 6. 测试和文档迁移

### 6.1 测试目录

测试文件必须按生产职责镜像到以下路径；除共享的 `conftest.py`、`hook_samples.py` 和必要的 package marker 外，不允许出现未列出的 Python 测试模块：

```text
tests/
├── conftest.py
├── hook_samples.py
├── cli/
│   └── test_cli.py
├── common/
│   ├── contracts/
│   │   ├── test_types.py
│   │   └── test_stage.py
│   ├── test_errors.py
│   ├── config/
│   │   └── test_config.py
│   ├── runtime/
│   │   ├── test_llm_client.py
│   │   └── test_schema_engine.py
│   ├── observability/
│   │   └── test_obslog.py
│   └── extensions/
│       └── test_hooks.py
├── operators/
│   ├── test_ingest.py
│   ├── test_segment.py
│   ├── test_dedup.py
│   ├── test_classify.py
│   ├── test_extract.py
│   ├── test_quality.py
│   ├── test_generate.py
│   ├── test_annotate.py
│   ├── test_verify.py
│   └── test_emitter.py
├── orchestration/
│   └── test_orchestrator.py
└── integration/
    ├── test_llm_client_llm.py
    ├── test_schema_engine_llm.py
    ├── test_annotate_llm.py
    ├── test_classify_llm.py
    ├── test_generate_llm.py
    ├── test_quality_llm.py
    ├── test_verify_llm.py
    ├── test_stream_llm.py
    └── test_key_pool_llm.py
```

`test_key_pool.py` 的离线密钥池测试必须并入 `common/runtime/test_llm_client.py`；`test_stream_ingest.py` 必须并入 `operators/test_ingest.py`。迁移不得删除或弱化任何既有测试用例；`test_stage.py` 和 `test_errors.py` 必须覆盖对应公共契约。所有 fixture import 必须切换到新的测试路径。

### 6.2 必须更新的文档

- `docs/CONTRACTS.md`：包布局、import discipline、CLI wiring 和模块路径。
- `AGENTS.md`：架构、模块 map、命令中涉及的模块路径。
- `CLAUDE.md`：与 `AGENTS.md` 保持逐项同步。
- `docs/dev/E2E-FINDINGS.md`：只有当路径相关 finding 受影响时更新。
- 其他 `docs/`、`tests/`、`examples/` 中命中的旧 import path。

用户手册若没有 observable behavior、输出字段、日志内容或命令变化，不得无理由重写；但必须通过旧路径全文搜索确认没有遗漏。

## 7. 执行波次和文件所有权

每个 worker 必须只编辑声明的文件集合；不得重置、覆盖或删除其他 worker 的修改。所有 worker 必须读取本 spec 和当前工作树状态。

### Wave 0：冻结 spec

主 agent 负责：

- 写入本文件；
- 检查本文件中的目标路径、删除清单、测试归档和关键符号；
- 提交前置 spec 变更。

### Wave 1：公共契约和配置

文件所有权：

- `labelkit/common/contracts/`、`labelkit/common/config/`、`labelkit/common/errors.py`；
- 删除旧 `labelkit/config/`；
- 删除旧 `labelkit/types.py`、`labelkit/stage.py`、`labelkit/errors.py`；
- 删除目标树未列出的 common package marker。

交付：common canonical modules 可直接 import，旧路径不可 import，契约内容不变。

### Wave 2：运行时公共能力

文件所有权：

- `labelkit/common/runtime/`；
- `labelkit/common/observability/`；
- `labelkit/common/extensions/`；
- 删除旧 `labelkit/llm_client.py`、`labelkit/schema_engine.py`、`labelkit/obslog.py`、`labelkit/hooks.py`。

交付：LLM、Schema、logging、trace、metrics、hook 的行为和公共符号不变。

### Wave 3：算子迁移

按不相交文件组执行：

- 输入/流：`ingest.py`、`segment.py`；
- 数据筛选：`dedup.py`、`classify.py`；
- 质量和生成：`quality.py`、`generate.py`；
- 标注和审核：`annotate.py`、`verify.py`、`extract.py`；
- 输出：`emitter.py`。

每组删除对应的旧平铺文件；目标树只保留 `labelkit/operators/*.py` 算子实现，不能修改其他算子组的业务行为。

### Wave 4：编排和 CLI

文件所有权：

- `labelkit/orchestration/`；
- 删除旧 `labelkit/orchestrator.py`；
- `labelkit/cli/`；
- `pyproject.toml` console script（如确有必要）；
- `labelkit/cli.py` 必须转换为 package，不能与 `labelkit/cli/` 同时存在。

交付：CLI 不再直接 import 算子；runtime/factory 承担对象图组装和 stage 顺序。

### Wave 5：测试、契约和开发文档

严格按 §6.1 移动/合并测试，删除兼容导入测试，补目录形态与依赖边界断言，并更新 `docs/CONTRACTS.md`、`AGENTS.md`、`CLAUDE.md` 和命中的开发文档。

### Wave 6：对抗复查

独立 reviewer 必须尝试驳倒以下声明：

- 所有 canonical modules 都位于目标目录；
- `labelkit/` 根目录只剩 `__init__.py`，旧 `labelkit/config/` 和全部 shim 已从 Git 与构建产物删除；
- `tests/` 的业务测试文件逐项符合 §6.1，没有平铺 common 测试、`test_compat_imports.py`、独立 `test_key_pool.py` 或独立 `test_stream_ingest.py`；
- CLI 不再直接创建算子；
- operators 没有新增未授权的相互依赖；
- `verify` 的三个契约例外仍然可用；
- public symbol、签名、事件名、错误码和 CLI entrypoint 没有变化；
- 旧 import path 确实失败，canonical import 和 wheel 安装后 import 确实成功；
- 用户已有未跟踪文件没有被包含或修改。

## 8. 验收门禁

以下命令必须全部成功；任何失败都必须修复后重新运行，不得标记为“后续处理”。

```bash
uv run pytest -q -m 'not integration'
uv run labelkit --help
uv run labelkit rubric
python3 -c 'import labelkit.cli, labelkit.common, labelkit.operators, labelkit.orchestration'
uv run pytest tests/cli/test_cli.py -q -k package_layout
python3 -m build
git diff --check
```

目录形态检查必须证明：

```text
labelkit 根目录 Python 文件 = {__init__.py}
labelkit/config 不存在
§3 中每个 canonical 生产文件存在，且不存在未列出的业务模块
§6.1 中每个测试文件存在，且不存在未列出的测试模块
旧平铺 import path 全部 find_spec(...) is None
构建出的 wheel/sdist 不含旧平铺文件或 labelkit/config
```

如果 `.env` 中存在 `LABELKIT_ZAI_KEY`，还必须运行：

```bash
uv run pytest tests/integration -q -m integration
```

并至少执行 `examples/text`、`examples/ui`、`examples/generate`、`examples/classify` 和 `examples/stream` 的 `validate` 或真实运行路径，确认 import 重构没有破坏 CLI 和运行时组装。没有 key 时，必须明确记录 integration 被环境跳过，不能把 offline 通过冒充 integration 通过。

## 9. 完成定义

只有同时满足以下条件，任务才算完成：

- 本 spec 的所有“必须完成”条目都有代码或验证证据；
- 本 spec 没有未完成、待定、defer、follow-up TODO；
- 所有测试和构建门禁已通过，或环境性跳过已被明确记录；
- `git status` 中除用户已有的 `docs/dev/SPEC-activity-structure.md` 外，没有未解释的变更；
- 变更已提交到 `codex/labelkit-layer-reorganization` 分支；
- 分支已推送到 `origin`；
- 已创建或更新 draft PR，PR 描述包含变更内容、分层原因、旧路径破坏性清理说明和验证结果。

不允许以“目录已建立但旧模块未删除”“测试仍平铺”“部分测试尚未更新”“为了兼容保留 shim”“文档稍后同步”作为完成状态。

## 10. 实施与验证记录

v1.0 的 shim-based 实施记录已被 v1.1 废止，不能作为完成证据。v1.1 只有在旧文件物理删除、测试严格归档、全部门禁重跑和独立对抗复查完成后，才可在此写入最终实测记录并将状态改为“已实施”。
