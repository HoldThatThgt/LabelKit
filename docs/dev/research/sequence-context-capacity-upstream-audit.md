# 上游与评审容量契约独立审查

审查日期：2026-09-09。权威来源为 `docs/dev/SPEC-sequence-context-capacity.md` 和
`docs/CONTRACTS.md`。审查生产范围为 segment、segment_capacity、stitch、verify、stream_verify、
verify_capacity。首次审查只读；随后按主代理授权新增组合测试，并修复另行发现的 dedup 同步容量
收集问题。未调用模型或提交 Git。以下区分最初复现与最终复验结果。

## 结论

本次发现两个可复现的行为偏差：标签克隆参与成员归属会保留错误接缝或制造自我中断；交错片段手术后
未重新排序。前者影响最终动作、标注和评审是否重新执行，后者影响公开片段表及复评提示词的片段顺序。
问题与复现已同步给主代理，由 verify 负责代理修复；本审查随后实跑包含新增回归的窄测试完成复验。
最初发现的组合测试缺口也已补齐，不再保留未验证的待补项。

| 偏差 | 当前实现与可观察结果 | 要求及修正边界 |
|---|---|---|
| 标签克隆保留已释放帧的归属 | `verify_capacity.py:243-246` 的 `current_seams` 纳入所有非 stitched 序列视图。真实驱动输入 A=[0,4]、B 首标签=[2,5]、B 克隆=[2,5]，B 首标签移除 2 后，帧 2 已是 dropped_noise，但克隆仍使 A 保持 seam=(0,)、rounds=1；没有重新抽取、标注或评审 A | 主规格:177-179 要求按最终成员归属重建受影响结果。只让首标签所有者提供归属；克隆仍是需要得到正确派生结果的消费视图。补完整 fanout→SHRINK→依赖复评回归 |
| 同一线索的不同标签视图被当作相互中断 | 同一 `current_seams` 路径：首标签从 [0,4] 回收为 [0,2,4]，克隆保留 [0,4]，得到 `{0: ('same-task',)}`。只过滤非首标签归属仍无法消除该结果 | `stitch.py:641-643` 的接缝要求是“别的线索”。计算当前视图时应排除其自身线索的归属，不能按任务名过滤，因为独立线索可能同名。和前一项属于同一归属缺陷，需同时修复 |
| 交错片段投影后顺序过时 | `verify_capacity.py:210-214` 按旧片段序生成结果。原片段 [[0,8],[4,6]] 移除位置 0 后，公开片段表成为 [[8],[4,6]]，`verify.py:384-389` 原样将此顺序送入复评 | `CONTRACTS.md:752` 定义 session-ordered fragment table；主规格:172-174 要求按明确位置投影。投影后按每个非空片段的首出现位置排序，并保留 cause、source_episode 与完整位置；补首位置跨越另一片段的 SHRINK 回归 |

以上行号记录首次复现时的工作区位置。完整驱动复现使用现有 `test_verify._seam_dependency_batch`、
`_stream_classified_cfg`、`SeqJudgeEngine` 与独立标注/动作叶夹具；并未替换 StreamVerifyDriver。
第一项实际输出如下：

```text
owner_positions=(5,)
clone_positions=(2,5)
frame_2_status=dropped_noise
first_seams=(0,)
first_rounds=1
first_current_seams={0: ('B',)}
extracts=[]
reannotated_ids=['second']
```

最终修复和回归已闭合：

- `tests/operators/test_verify.py::test_foreign_member_shrink_reopens_passed_seam_owner_with_full_repair_and_review`
  现参数化覆盖实际多标签扇出；仅首标签所有者参与全会话归属。
- `tests/operators/test_verify.py::test_multi_owner_reclaim_does_not_invent_a_self_interruption_for_its_clone`
  与 `test_same_task_name_from_an_independent_sequence_still_interrupts_a_clone` 同时覆盖自身线索排除
  和独立同名线索仍构成中断。
- `tests/operators/test_verify.py::test_shrink_reorders_interleaved_fragments_by_first_surviving_position`
  证明片段投影后按首个存活位置重新排序，真实复评消费新顺序。
- `tests/operators/test_verify.py::test_clone_seam_dependency_preserves_owner_reclaimed_shared_frame_products`
  覆盖依赖修复引起的克隆重标注，同时保证共享帧产物仅由首标签同步，不删除其新回收的帧键。

## 已核对的执行边界

分段先完整归并会话窗口，再按最终噪声及 min_len 语义发出 episode；容量分区在 `_emit_episode` 中执行。
`segment.py:539-574,657-719` 保留后窗覆盖重叠位置的裁决；`segment.py:327-351` 对完整窗口严格缩小并
保留一帧重叠，最小两帧超限走终态。`segment.py:624-630` 在混合失败集合中优先寻找上下文超限，不能
被较早的普通错误带入 keep。`segment_capacity.py` 对不可分的非 sequence 预判保留语义段，交真正的
下游所属阶段处置。

所有真实缝合变更都经过 `_guard_capacity` 和 `_rebind`：`stitch.py:883-908,1022-1050,1172-1185`。
候选与目标的 sealed 检查、允许范围交集及重复位置检查在 `stitch.py:977-999` 共用；最后提交重新检查
封闭和范围。初次 sealed 候选零判决独立开线索，不进入开放池或 repass 候选/目标；普通池淘汰仍可复评，
对应 `stitch.py:782-794,1116-1130,1244-1306`。交错合并失败只有较早线索封闭，不伪造线性切点；
`stitch.py:1063-1072` 仅在旧尾严格早于新首时收紧范围。

评审在同步计划、panel 结果、回收复判、重抽动作、帧分类、帧标注及重标注各轮先收集所有容量错误，
再业务归并。关键收集点为 `stream_verify.py:379-382,469-475,575-578,614-617,960-963,1115-1117`；
帧结果归并在相邻的 `_reduce_frame_classify`、`_backfill_frame_annotate` 同样使用完整集合。
`stream_verify.py:255-262` 在容量错误逃逸时恢复整个阶段快照，覆盖先前通过的依赖序列及手术帧。
`stream_verify.py:313-341` 实现依赖重建和原修复轮数消耗；上述归属缺陷已由最终组合回归验证修复。

邻帧在 `verify.py:446-458` 先按允许范围选择；其完整文本及归一化树进入边界余量，图片作为实际请求
parts 加入 `verify.py:582-585`。预判与实际请求使用相同完整载荷。边界回收在计划、认领提交、成员重建
各处检查允许范围；人工缺头缺尾豁免要求精确命中真实切点，不能把其他缺陷一并改成通过。

Schema 修复的完整证据开关在 segment、stitch、stream review 调用点显式赋值：
`segment.py:286-289`、`stitch.py:419-420`、`stream_verify.py:448-454`。
回收复判复用 segment 叶；重新标注在 `stream_verify.py:1233-1245` 保留当前输出、完整批评和工作成员。
公共 SchemaEngine 的完整原始请求保留及实际 repair profile 超限传播有独立测试；这与生产端真实
模型的 Schema 修复成功率属于不同证据。

## 主规格验收行到测试节点

节点均为现有 pytest node；参数化节点的全部参数由相应文件级窄命令执行。这里仅覆盖本次审查责任范围，
不能替代 ingest、配置、dedup、运行时、emitter、本地模型、覆盖率及变异审查各自的验收。

| 主规格验收行 | 当前可执行节点 | 最终观察 |
|---|---|---|
| :338 跨批次缝合 | `tests/operators/test_stitch.py::test_vote_groups_limit_leaf_tasks_without_changing_stitch_decision`；`tests/orchestration/test_session_workflow.py::test_real_ingest_segment_dedup_emitter_preserve_full_session_across_compute_sizes` | 分组任务数改变，完整会话成员保持；两个测试均实跑通过 |
| :340 分段接缝 | `tests/operators/test_segment.py::test_segment_computation_groups_preserve_window_overlap_and_episode`；`tests/operators/test_segment.py::test_stitching_seam_frame_belongs_to_later_window`；`tests/operators/test_sequence_capacity_combinations.py::test_group_edges_share_noise_and_short_segment_decisions_before_final_partition` | batch_size=1/2/100 在噪声与短段边界分组；后窗撤销旧噪声、最终噪声/短段归属和完整 episode ID 均一致 |
| :341 初次容量分区 | `tests/operators/test_segment.py::test_capacity_partition_happens_after_min_len_and_keeps_short_tail`；`tests/operators/test_segment.py::test_semantically_short_segment_never_enters_capacity_partition`；`tests/operators/test_segment.py::test_capacity_single_frame_failure_keeps_failed_episode_and_frame_ownership`；`tests/operators/test_segment.py::test_indivisible_preview_keeps_full_semantic_episode_for_owning_stage`；`tests/operators/test_sequence_capacity_combinations.py::test_rules_and_keep_paths_partition_full_evidence_after_semantic_fate` | LLM、rules、普通错误 keep 均经过实际完整标注预算分区；短尾保留且 keep 降级证据复制到每个容量子段，帧全部恰归一次 |
| :342 全部合并入口 | `tests/operators/test_stitch.py::test_capacity_pass_one_failure_seals_target_and_preserves_candidate`；`tests/operators/test_stitch.py::test_capacity_rescue_failure_seals_target_and_keeps_dropped_short_frames`；`tests/operators/test_stitch.py::test_capacity_repass_failure_seals_earliest_candidate_even_when_target_is_later`；`tests/operators/test_sequence_capacity_combinations.py::test_each_stitch_entry_prices_exact_complete_request_and_next_member` | 六个组合用同一真实 AnnotateStage 请求、模型 Schema、完整 UI 和图片成本；输入预算精确等于三帧估算，三种入口均可合并，增加第四帧均拒绝且零成员/救援计数泄漏 |
| :343 sealed 不重开 | `tests/operators/test_stitch.py::test_initial_sealed_episode_never_enters_judgment_or_repass_pool`；`tests/operators/test_stitch.py::test_both_sealed_sides_block_preview_and_commit`；`tests/operators/test_stitch.py::test_capacity_bounds_filter_incompatible_targets_before_judgment`；`tests/operators/test_stitch.py::test_pool_full_eviction_lru_fallback_and_closed_not_terminated` | sealed 候选/目标、末次提交及普通 eviction 区别均有直接观察；所有实际合并入口共用已测提交门 |
| :344 重复内容身份 | `tests/operators/test_stitch.py::test_repeated_content_keeps_distinct_occurrences_and_projectable_interleaved_fragments`；`tests/operators/test_verify.py::test_named_duplicate_content_shrink_removes_only_target_occurrence`；`tests/operators/test_verify.py::test_verify_backfill_duplicate_content_uses_distinct_occurrences`；`tests/operators/test_verify.py::test_shrink_reorders_interleaved_fragments_by_first_surviving_position` | 原始内容 ID 重复不吞帧，命名缺陷、帧补产物及交错片段投影/重排均按出现位置 |
| :345 手术接缝依赖 | `tests/operators/test_verify.py::test_foreign_member_shrink_reopens_passed_seam_owner_with_full_repair_and_review`；`tests/operators/test_verify.py::test_dependency_capacity_failure_restores_surgeon_and_previously_passed_owner`；`tests/operators/test_verify.py::test_seam_dependency_preserves_exhausted_round_budget_and_fails_stale_result`；`tests/operators/test_verify.py::test_clone_seam_dependency_preserves_owner_reclaimed_shared_frame_products` | 单/多标签依赖重抽/标注/复评、容量整轮回滚、共享帧产品保留及修复耗尽均通过；自身归属与独立同名线索的组合节点见修复闭合记录 |
| :346 完整文本与 UI | `tests/operators/test_segment.py::test_segment_request_keeps_late_visible_evidence_and_each_image`；`tests/operators/test_verify.py::test_boundary_preview_includes_allowed_full_tree_and_image_but_never_crosses_cut`；`tests/operators/test_verify.py::test_sequence_step_block_preserves_every_step_under_budget_pressure` | segment 原裁剪位置的事实、完整邻树/图、所有动作和零裁剪有直接断言；stitch 保留语义卡，完整合并预判另过检查器 |
| :348 请求真实预算 | `tests/operators/test_verify.py::test_preview_capacity_prices_full_members_and_actual_schema_without_model`；`tests/operators/test_verify.py::test_actual_review_overflow_escapes_before_model_and_preserves_all_items`；`tests/operators/test_verify.py::test_expanded_reannotation_overflow_reports_working_positions_and_rolls_back`；`tests/operators/test_sequence_capacity_combinations.py::test_every_stream_call_enters_real_l3_with_complete_evidence_and_original_capacity_error` | 完整成员及模型 Schema 预判、扩张超限与回滚通过；八类真实入口经真实 SchemaEngine 触发 L3，保留全部图/树/上下文与原 repair profile/phase/origin |
| :349 有限真实超限恢复 | `tests/operators/test_segment.py::test_degrade_second_level_halving_of_a_half`；`tests/operators/test_segment.py::test_degrade_precheck_phase_splits_without_feeding_breaker`；`tests/operators/test_verify.py::test_review_wave_reports_every_actual_overflow_across_computation_groups`；`tests/operators/test_verify.py::test_review_planning_collects_all_episode_and_judge_capacity_failures_before_calls` | segment 严格缩窗、预判与 reactive、多 episode/panel 容量信号跨计算组完整回收通过。会话切点推进由 coordinator 独立测试验收 |
| :350 最小单位和错误分类 | `tests/operators/test_segment.py::test_minimal_reactive_window_never_uses_keep`；`tests/operators/test_segment.py::test_degrade_minimal_two_frame_window_is_terminal`；`tests/operators/test_segment.py::test_degrade_terminal_finish_origin_never_feeds_breaker`；`tests/operators/test_stitch.py::test_context_overflow_keep_opens_thread_with_precise_event_kind`；`tests/operators/test_verify.py::test_reclaim_rejudgment_reactive_400_feeds_breaker_exactly_once` | 最小窗口终态、origin 精确计数、卡片失败原处置与回收错误事实通过；未以卡片池失败拆分目标序列 |
| :352 全下游隔离（verify 部分） | `tests/operators/test_verify.py::test_each_repair_wave_collects_all_capacity_outcomes_before_any_product_commit`；`tests/operators/test_verify.py::test_frame_planning_collects_every_synchronous_capacity_error`；`tests/operators/test_verify.py::test_dependency_capacity_failure_restores_surgeon_and_previously_passed_owner`；`tests/operators/test_verify.py::test_reseam_failure_rolls_back_reclaim_envelope_atomically` | 五种 repair 叶轮全部收集先于产品提交、同步规划多错、成员及依赖产物回滚通过；未将 usage/trace 误当可回滚业务数据 |
| :354 人工边界 | `tests/operators/test_stitch.py::test_capacity_repass_interleaved_members_seal_without_false_linear_cut`；`tests/operators/test_verify.py::test_capacity_boundary_exemption_is_specific_to_touching_edge`；`tests/operators/test_verify.py::test_capacity_suspicion_does_not_exempt_other_real_defects`；`tests/operators/test_verify.py::test_claim_and_rebuild_recheck_allowed_bounds_before_committing` | 交错封闭不缩错区间、精确切点豁免、其他缺陷维持、计划及最终成员提交边界均有断言 |

完整证据 Schema 修复对应主规格:121-124，公共节点为：

- `tests/common/inference/test_schema_engine.py::test_complete_evidence_l3_preserves_original_messages_and_propagates_capacity`
- `tests/common/inference/test_schema_engine.py::test_complete_evidence_finalized_repair_keeps_only_model_space_previous_output`

完整入口组合现由
`tests/operators/test_sequence_capacity_combinations.py::test_every_stream_call_enters_real_l3_with_complete_evidence_and_original_capacity_error`
覆盖 stitch、segment、review、claim、reseam、reannotate、frame_classify、frame_annotate。
该测试使用真实 SchemaEngine 处理未通过 Schema 的首轮数据，只有最底层进程内 LLM 对象返回数据/错误；
没有服务或 transport。每个修复请求都与首轮完整消息逐项对照，校验完整树、全部图片、Schema、原评审
批评及上一标注仍在，并确认原始 repair profile 的 reactive/http_400 没有被转成 SchemaViolation
或提前统计为最终 overflow_records。帧子调用同时验证同轮三个错误全部返回。

## 实跑证据

追加的运行生命周期组合已覆盖以下主规格验收行，均位于
`tests/operators/test_sequence_capacity_combinations.py`：

| 当前主规格验收行 | 完整 pytest 节点 | 最终观察 |
|---|---|---|
| :339 会话边界的空迭代 | `tests/operators/test_sequence_capacity_combinations.py::test_empty_session_iteration_finishes_with_an_empty_delivery` | 零会话不进入算子，正常交付空文件、零计数；无有效输入文件仍由 ingest 原合同拒绝 |
| :355 随机源与校准 | `tests/operators/test_sequence_capacity_combinations.py::test_real_workflow_freezes_calibration_only_after_session_recomputation` | 真实 ProcessWorkflow、SegmentStage、AnnotateStage 预判与 ImageCostCalibrator；batch_size=1/3 和两种强制完成顺序，第一会话 attempt 1/2 始终读取相同先验，第二会话读取已冻结新值，同会话 RNG 相同；24 个样本和最终 report 校准值准确 |
| :357 中断与既有输出 | `tests/operators/test_sequence_capacity_combinations.py::test_second_session_cancellation_preserves_first_real_dedup_and_emitter_commit` | 第一会话真实 DedupStage 与 Emitter 提交成功；第二会话已预留去重增量后实际取消 task，正式索引/输出/标注计数仅第一会话；按已摄取第三会话准确核对 scanned=9、absorbed=2、episodes=1、emitted=1、unprocessed=7 |

另行修复的 dedup 同步准备容量收集由 `tests/operators/test_dedup_session.py` 验证：

- `test_preparation_collects_all_real_overflows_before_any_embedding_or_item_commit`：真实完整 embedding
  预算产生两个预判错误，可装记录放在不同位置仍零向量派发、零成员或正式索引变更。
- `test_preparation_capacity_barrier_precedes_ordinary_failure_products`：同步普通错误产品也不能越过容量屏障。
- `test_preparation_fatal_has_priority_after_complete_synchronous_plan`：完整同步规划后的致命错误优先。
- `test_ordinary_preparation_error_keeps_existing_failure_and_final_admission`：无容量错误时保留原单记录错误
  产品与其余成员接纳语义。

新增组合测试文件独立执行结果：`25 passed in 0.51s`。最初只读审查的两条窄命令保留如下，
后续包含全部新组合与修复的联合命令见本节末尾。

```bash
uv run --python 3.12 pytest tests/operators/test_segment.py tests/operators/test_stitch.py \
  tests/operators/test_verify.py -q --tb=short
```

结果：`283 passed in 0.58s`。涵盖上述三个算子的全部已有文件级测试。

```bash
uv run --python 3.12 pytest \
  tests/common/inference/test_schema_engine.py::test_complete_evidence_l3_preserves_original_messages_and_propagates_capacity \
  tests/common/inference/test_schema_engine.py::test_complete_evidence_finalized_repair_keeps_only_model_space_previous_output \
  tests/orchestration/test_session_workflow.py::test_real_ingest_segment_dedup_emitter_preserve_full_session_across_compute_sizes \
  tests/orchestration/test_session_workflow.py::test_real_multilabel_fanout_and_verify_shrink_preserve_single_member_owner \
  -q --tb=short
```

结果：`5 passed in 0.42s`。既有多标签会话回归只验证成员唯一所有者，未包含另一条被中断线索，
因此不覆盖本次发现的接缝问题。

最终联合复验命令：

```bash
uv run --python 3.12 pytest tests/operators/test_sequence_capacity_combinations.py \
  tests/operators/test_segment.py tests/operators/test_stitch.py tests/operators/test_verify.py \
  tests/operators/test_dedup_session.py tests/operators/test_dedup.py \
  tests/orchestration/test_session_workflow.py -q --tb=short
```

最终联合复验结果：`447 passed in 3.25s`。dedup 修改另经窄覆盖率验证：
`dedup.py` 行覆盖 96.19%、分支 90.45%；`dedup_session.py` 行/分支均 100%。新增的三个生产函数
`DedupStage._prepare_records`、`DedupStage._settle_preparation_errors`、
`_SessionStage._settle_preparation_errors` 行/分支均 100%，熔断控制在同步准备阶段原样传播另有明确回归。
覆盖率 JSON 为 `/tmp/dedup-preparation-final-report.json`。没有运行完整离线套件、真实模型或 Uncle Bob 变异门禁；
本报告不声称这些验收已经通过。
