# 跨批次序列缝合与上下文容量文件清单

基于开发前 HEAD `56a0ea1` 的实际工作区差异。开发规格定义行为；本清单用于审阅修改范围。
所有生产模块均沿现有分层，不引入第三方依赖、持久化服务或兼容层。
输入夹具与实际运行产物分开：`examples/sequence-context-capacity/out/verification-20260909/` 为本地可复核证据，不属于生产代码。

## 生产模块

| 文件 | 修改类型 |
|---|---|
| `labelkit/common/config/_constraints.py` | 修订现有模块 |
| `labelkit/common/config/_sections.py` | 修订现有模块 |
| `labelkit/common/config/model.py` | 修订现有模块 |
| `labelkit/common/contracts/sequence_capacity.py` | 新增职责模块 |
| `labelkit/common/contracts/stage.py` | 修订现有模块 |
| `labelkit/common/contracts/types.py` | 修订现有模块 |
| `labelkit/common/errors.py` | 修订现有模块 |
| `labelkit/common/inference/budget.py` | 修订现有模块 |
| `labelkit/common/inference/credentials.py` | 修订现有模块 |
| `labelkit/common/inference/llm_client.py` | 修订现有模块 |
| `labelkit/common/inference/schema_engine.py` | 修订现有模块 |
| `labelkit/common/inference/sequence_evidence.py` | 新增职责模块 |
| `labelkit/common/observability/obslog.py` | 修订现有模块 |
| `labelkit/operators/annotate.py` | 修订现有模块 |
| `labelkit/operators/annotate_capacity.py` | 新增职责模块 |
| `labelkit/operators/classify.py` | 修订现有模块 |
| `labelkit/operators/classify_capacity.py` | 新增职责模块 |
| `labelkit/operators/dedup.py` | 修订现有模块 |
| `labelkit/operators/dedup_session.py` | 新增职责模块 |
| `labelkit/operators/emitter.py` | 修订现有模块 |
| `labelkit/operators/extract.py` | 修订现有模块 |
| `labelkit/operators/quality.py` | 修订现有模块 |
| `labelkit/operators/quality_capacity.py` | 新增职责模块 |
| `labelkit/operators/segment.py` | 修订现有模块 |
| `labelkit/operators/segment_capacity.py` | 新增职责模块 |
| `labelkit/operators/stitch.py` | 修订现有模块 |
| `labelkit/operators/stream_verify.py` | 修订现有模块 |
| `labelkit/operators/verify.py` | 修订现有模块 |
| `labelkit/operators/verify_capacity.py` | 新增职责模块 |
| `labelkit/orchestration/process_workflow.py` | 修订现有模块 |
| `labelkit/orchestration/session_capacity.py` | 新增职责模块 |
| `labelkit/orchestration/session_workflow.py` | 新增职责模块 |

## 测试

| 文件 | 修改类型 |
|---|---|
| `tests/cli/goldens/dryrun-mix-text.txt` | 修改 |
| `tests/cli/goldens/dryrun-mix.txt` | 修改 |
| `tests/cli/goldens/dryrun-stream-text.txt` | 修改 |
| `tests/cli/goldens/dryrun-stream.txt` | 修改 |
| `tests/cli/test_cli.py` | 修改 |
| `tests/common/config/test_config.py` | 修改 |
| `tests/common/contracts/test_execution.py` | 修改 |
| `tests/common/contracts/test_generation_contracts.py` | 修改 |
| `tests/common/contracts/test_sequence_capacity.py` | 新增 |
| `tests/common/contracts/test_stage.py` | 修改 |
| `tests/common/inference/test_budget.py` | 修改 |
| `tests/common/inference/test_llm_client.py` | 修改 |
| `tests/common/inference/test_schema_engine.py` | 修改 |
| `tests/common/observability/test_obslog.py` | 修改 |
| `tests/integration/test_sequence_context_capacity_local_llm.py` | 新增 |
| `tests/integration/test_stream_llm.py` | 修改 |
| `tests/operators/generation/test_planner.py` | 修改 |
| `tests/operators/generation/test_program.py` | 修改 |
| `tests/operators/generation/test_project.py` | 修改 |
| `tests/operators/test_annotate.py` | 修改 |
| `tests/operators/test_classify.py` | 修改 |
| `tests/operators/test_dedup_session.py` | 新增 |
| `tests/operators/test_emitter.py` | 修改 |
| `tests/operators/test_emitter_capacity.py` | 新增 |
| `tests/operators/test_extract.py` | 修改 |
| `tests/operators/test_quality.py` | 修改 |
| `tests/operators/test_segment.py` | 修改 |
| `tests/operators/test_sequence_capacity_combinations.py` | 新增 |
| `tests/operators/test_sequence_complete_evidence.py` | 新增 |
| `tests/operators/test_stitch.py` | 修改 |
| `tests/operators/test_verify.py` | 修改 |
| `tests/orchestration/test_process_workflow.py` | 修改 |
| `tests/orchestration/test_session_workflow.py` | 新增 |

## 权威规格与实现设计

| 文件 | 修改类型 |
|---|---|
| `spec/00-frontmatter.md` | 修改 |
| `spec/10-ch1-overview.md` | 修改 |
| `spec/20-ch2-overall-design.md` | 修改 |
| `spec/301-m1-config.md` | 修改 |
| `spec/303-m3-dedup.md` | 修改 |
| `spec/304-m4-qualityqurating.md` | 修改 |
| `spec/305-m5-annotate.md` | 修改 |
| `spec/307-m7-verify.md` | 修改 |
| `spec/308-m8-schema-engine.md` | 修改 |
| `spec/309-m9-llm-client.md` | 修改 |
| `spec/310-m10-orchestration.md` | 修改 |
| `spec/311-m11-emitter.md` | 修改 |
| `spec/312-m12-logging.md` | 修改 |
| `spec/313-m13-classify.md` | 修改 |
| `spec/314-m14-segment.md` | 修改 |
| `spec/315-m15-extract.md` | 修改 |
| `spec/316-m16-stitch.md` | 修改 |
| `spec/40-ch4-data-structures.md` | 修改 |
| `spec/50-ch5-config-spec.md` | 修改 |
| `spec/60-ch6-io-formats.md` | 修改 |
| `spec/70-ch7-logging.md` | 修改 |
| `spec/80-ch8-nongoals-roadmap.md` | 修改 |

## 研究、手册与示例

| 文件或范围 | 修改内容 |
|---|---|
| `docs/dev/SPEC-sequence-context-capacity.md` | 最终开发合同、状态图、错误与验收矩阵 |
| `docs/CONTRACTS.md` | 载体、接口、完整请求、字段顺序、计数及终态合同 |
| `docs/dev/research/sequence-context-capacity-*.md` | 官方方案研究和独立公共层、上游、控制器审查 |
| `docs/dev/SEQUENCE-CONTEXT-CAPACITY-VERIFICATION.md`、`docs/dev/E2E-FINDINGS.md` | 实际验收、历史失败及未执行发布门 |
| `docs/dev/BOB-sequence-context-capacity.md` | 独立变异审查的实际前置检查与阻塞记录 |
| `docs/manual/` | 配置、概念、分类、评分、标注、复审、输出、可观测性、调优、流与缝合手册 |
| `examples/stream/`、`examples/mix/` | 删除裁剪参数、实际UI能力与完整会话说明 |
| `examples/sequence-generation/README.md`、`project-replay.toml` | 删除旧裁帧字段及更新说明 |
| `examples/sequence-context-capacity/` | 六种固定项目、真实4B配置、确定性文本/UI输入、准备器与独立检查器 |
| `tools/design_figures/fig-3-4.svg`、`fig-3-8.svg`、`fig-3-9.svg` | 区分普通记录编排、验证波次和会话容量图 |
| `docs/design/labelkit-design-v1.html`、`labelkit-design-v1.pdf` | 从当前spec重建并检查版式 |
| `AGENTS.md`、`CLAUDE.md` | 同步当前合同与本地4B授权范围，保持字节一致 |
