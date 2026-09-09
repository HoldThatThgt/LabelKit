# 公共容量与输出契约独立审查

审查日期：2026-09-09。实施基线：`56a0ea1`；审查对象为当前共享工作区的实际变更。
权威来源为 `docs/dev/SPEC-sequence-context-capacity.md` 与 `docs/CONTRACTS.md`。
审查覆盖 common config、budget、LLMClient、SchemaEngine、MetricsSink、公共容量协议及 emitter。
本审查者未修改这些公共生产代码；下述配置修复由父代理完成并由审查者独立重跑窄测试。
终态投影裁决明确后，按父代理授权新增了 transition 契约测试。

## 已发现并闭合的问题

| 问题 | 原行为及影响 | 当前修复证据 | 独立验证 |
|---|---|---|---|
| 完整证据 L3 使用非视觉 repair profile | UI process 配置显式指定 `output.repair_llm` 为 `supports_vision=false`，静态 load 仍成功且凭据引用集包含该 profile。新增完整证据 L3 会重放原图片，因此运行时会把图片送给不具备视觉能力的 profile。临时目录纯配置复现已确认；没有请求模型。 | `common/config/_constraints.py::_vision_users` 当前第 246 行开始，仅在 process stream、已有实际视觉请求、修复次数大于零时增加 repair profile 视觉要求。 | `tests/common/config/test_config.py::test_complete_evidence_repair_profile_requires_vision_only_when_reachable` 四组：不可达修复、普通旧路径、可达视觉成功与可达非视觉失败。全部通过。 |
| 已禁用 L3 仍触发新增容量和凭据要求 | `max_repair_attempts=0` 时，显式 repair profile 仍无条件进入引用集，从而被新增正 context_window 规则拒绝，并要求物化永远不用的凭据。存在性旧契约与实际调用能力要求应分开。 | `common/config/_constraints.py::_collect_referenced` 当前第 474 行与 `common/inference/credentials.py::referenced_profiles` 当前第 102 行统一排除 process stream 已禁用的 L3 引用；显式 profile 的存在性验证保留。 | `tests/common/config/test_config.py::test_disabled_stream_schema_repair_adds_no_capacity_or_credential_requirement` 已通过，同时证明启用 L3 后恢复正窗口要求。 |

上述两个问题在审查时均有生产代码原因，未把过时夹具的空出现位置、旧注释或父代理正在更新的日志断言当成生产缺陷。
复核当前实现后，本审查范围没有其他已确认的未修复生产违例。

## 已明确的终态测试边界

`common/contracts/sequence_capacity.py::_same_terminal_target` 当前对 frame 按出现位置精确匹配，其他单位按
root、label 和 record_id 投影至序列执行门。sequence 扩张失败投影回原视图已有明确测试，避免再次执行相同扩张。
父代理明确：transition 的不可拆必要相邻对使当前视图无法完成，因此同 stage、profile、lineage、record.id、
label 的其他相邻对也受终态门阻断；原失败证据仍精确保留实际 pair，不改写其出现位置。
新增 `tests/common/contracts/test_sequence_capacity.py::test_terminal_transition_blocks_its_current_view_across_distinct_pairs_only`
覆盖两个不同相邻对共享当前视图的阻断，以及其他子段、root、label、stage、profile、unit 的隔离。
该测试已通过，并同步至 `spec/307-m7-verify.md`。此项已闭合，无待确认项。

## 主规格验收行与可执行测试映射

| 主规格验收行 | 公共层可观察的契约 | pytest node |
|---|---|---|
| 跨批次缝合 | 分组大小改变、完整声明序不变、空组不派发、普通路径不分组 | `tests/common/contracts/test_sequence_capacity.py::test_physical_group_size_changes_without_changing_frozen_task_order` |
| 重复内容身份 | 重复内容独立位置；生成沿用原 ID；容量身份域分离且与分区路径无关 | `tests/common/contracts/test_sequence_capacity.py::test_occurrences_keep_repeated_content_independently_and_generation_keeps_its_id`；`::test_child_identity_cannot_self_reference_a_stitched_single_member_founder`；`::test_invalid_occurrence_partition_cannot_produce_sequence_identity` |
| 能力与上下文配置 | 按类 quality.mode 的实际 profile 引用与运行一致 | `tests/common/config/test_config.py::test_stream_class_quality_modes_validate_every_actual_profile` |
| 能力与上下文配置 | UI segment、quality、frame classify 均要求 vision；语义 embedding 仅启用时要求正窗口 | `tests/common/config/test_config.py::test_segment_llm_requires_vision_for_complete_ui_evidence`；`::test_stream_quality_requires_vision_for_complete_ui_evidence`；`::test_frame_classify_requires_vision_for_complete_ui_evidence`；`::test_stream_semantic_embedding_requires_capacity_only_when_enabled` |
| 能力与上下文配置 | 新 L3 完整媒体修复 profile 的视觉与可达性要求 | `tests/common/config/test_config.py::test_complete_evidence_repair_profile_requires_vision_only_when_reachable`；`::test_disabled_stream_schema_repair_adds_no_capacity_or_credential_requirement` |
| 请求真实预算 | 文本、全图、消息开销和实际上行 Schema 统一计价；未上行 Schema 不计价 | `tests/common/inference/test_budget.py::test_est_prompt_sums_text_images_overhead_and_schema`；`tests/common/inference/test_llm_client.py::test_precheck_raises_context_overflow_before_any_network`；`::test_precheck_counts_images_via_the_calibrator_prior`；`::test_complete_hides_the_schema_from_the_budget_when_l0_is_off` |
| 完整文本与 UI | generic 与 finalized L3 保留原消息和图片、保留 image_px，容量错误原样抛出且不提前喂熔断 | `tests/common/inference/test_schema_engine.py::test_complete_evidence_l3_preserves_original_messages_and_propagates_capacity` |
| 请求真实预算 | finalized L3 的 previous 仅含模型字段；代码负责字段不回流模型 | `tests/common/inference/test_schema_engine.py::test_complete_evidence_finalized_repair_keeps_only_model_space_previous_output`；`::test_complete_finalized_repair_projects_candidate_and_preserves_accounting` |
| 有限真实超限恢复 | 整轮 plural failures 声明序保留、嵌套扁平、只过滤已知终态、删除单数接口 | `tests/common/contracts/test_sequence_capacity.py::test_complete_wave_preserves_all_failures_in_declaration_order_and_filters_only_known_terminals`；`::test_capacity_signal_requires_nonempty_wave` |
| 最小单位和错误分类 | 帧失败精确隔离 stage/profile/label/位置；扩张失败回原序列执行门 | `tests/common/contracts/test_sequence_capacity.py::test_terminal_frame_request_is_isolated_by_stage_profile_label_and_occurrence`；`::test_verify_expansion_failure_projects_to_original_view_without_repeating_expansion` |
| 最小单位和错误分类 | 不可拆 transition 按当前视图阻断相邻对请求，实际失败 pair 证据保留 | `tests/common/contracts/test_sequence_capacity.py::test_terminal_transition_blocks_its_current_view_across_distinct_pairs_only` |
| 最小单位和错误分类 | 真实 400 token 形状与 413/其他 400 分离，输出截断不变成容量失败 | `tests/common/inference/test_llm_client.py::test_overflow_body_matcher_hits_all_five_families`；`::test_overflow_body_matcher_is_case_insensitive_and_selective`；`::test_sniff_gate_requires_budget_and_status_400`；`tests/common/inference/test_budget.py::test_classify_stage_error_vocabulary` |
| 运行证据保留 | 尝试 trace 归属传入叶任务且退出恢复；运行成本保留，最终 overflow_records 随尝试计数提交 | `tests/common/observability/test_obslog.py::test_session_attempt_trace_scope_reaches_leaf_and_restores_after_failure`；`::test_session_final_overflow_count_commits_once_while_request_costs_survive`；`::test_cancelled_session_retains_capacity_and_embedding_failures_only` |
| 运行证据保留 | 捕获不可嵌套，合并不能在捕获区发生，retained_frames 是帧高水位 | `tests/common/observability/test_obslog.py::test_metrics_sink_rejects_nested_capture_and_invalid_merge`；`::test_session_retained_frames_high_water_and_scope_validation` |
| 人工边界 | sealed 无线性切点仍非 null；切点、允许区间、lineage 和键序准确；旧 session_split 删除 | `tests/operators/test_emitter.py::test_meta_stream_ui_order_span_marks_and_steps`；`tests/operators/test_emitter_capacity.py::test_capacity_cut_output_has_exact_position_bounds_and_lineage` |
| 重复内容身份 | wire 中重复 ID 对应不同帧分类和标注，位置与来源保持对齐 | `tests/operators/test_emitter_capacity.py::test_repeated_content_occurrences_keep_distinct_frame_products_in_output` |
| 随机源与校准 | 同一冻结周期内 observe 不改变 cost，观测顺序无关；周期开始/结束时机由会话编排验收 | `tests/common/inference/test_budget.py::test_in_batch_observe_never_affects_current_batch_cost`；`::test_batch_frozen_determinism_is_order_free` |
| 普通及生成路径 | 普通 L3 超限仍按旧修复耗尽；完整证据开关不改变默认路径，finalized 后处理契约保留 | `tests/common/inference/test_schema_engine.py::test_l3_repair_overflow_short_circuits_to_exhaustion`；`::test_complete_finalized_orders_model_finalize_full_and_l25_once`；`::test_complete_finalized_full_l2_failure_never_calls_l25_or_l3` |

节点缩写 `::...` 继承同一单元格中最近出现的测试文件。表中是可执行节点映射，不表示整张表已经由本轮
命令逐一运行；准确执行范围如下。全链会话隔离、释放、中断、真实端点及 Bob 门禁由父代理集成验证。

## 本轮独立运行证据

```bash
uv run --python 3.12 pytest -q tests/common/contracts/test_sequence_capacity.py tests/common/inference/test_budget.py tests/operators/test_emitter.py tests/operators/test_emitter_capacity.py --tb=short
# 172 passed in 0.49s

uv run --python 3.12 pytest -q tests/common/config/test_config.py -k 'stream_class_quality_modes or stream_semantic_embedding or stream_rejects_undeclared or stream_quality_requires or segment_llm_requires or frame_classify_requires or removed or context_window or full_evidence' --tb=short
# 25 passed, 331 deselected in 0.74s

uv run --python 3.12 pytest -q tests/common/inference/test_schema_engine.py tests/common/inference/test_llm_client.py tests/common/observability/test_obslog.py -k 'complete_evidence or l3_repair_overflow or overflow_body or sniff_gate or precheck or session_attempt_trace or session_final_overflow or cancelled_session or session_retained or metrics_sink_rejects_nested' --tb=short
# 21 passed, 363 deselected in 0.43s

uv run --python 3.12 pytest -q tests/common/config/test_config.py -k 'complete_evidence_repair_profile or disabled_stream_schema_repair' --tb=short
# 5 passed, 351 deselected in 0.37s
```

首次独立审查合计 223 个离线测试执行通过。终态裁决及上游独立审查发现修复完成后的窄回归为：

```bash
uv run --python 3.12 pytest -q tests/operators/test_segment.py tests/operators/test_stitch.py tests/operators/test_verify.py tests/common/contracts/test_sequence_capacity.py --tb=short
# 305 passed in 0.65s
```

新增上游回归通过真实 `ClassifyStage._fan_out` 进入 verify，覆盖克隆不拥有成员、同序列视图不形成自身中断、
同名独立线索仍可中断、交错碎片缩段后按首位置排序，以及克隆接缝依赖修复不能删除首标签新回收帧的共享产物。
修改生产文件的 AST 限制检查和 `git diff --check` 均通过。未运行模型、网络集成、全量套件或提交代码。
