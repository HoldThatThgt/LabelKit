# 会话控制器与去重提交独立审查

审查日期：2026-09-09。权威来源为[完整会话容量规格](../SPEC-sequence-context-capacity.md)与
[公共契约](../../CONTRACTS.md)。生产范围为
[session_workflow.py](../../../labelkit/orchestration/session_workflow.py)、
[session_capacity.py](../../../labelkit/orchestration/session_capacity.py)与
[dedup_session.py](../../../labelkit/operators/dedup_session.py)；同时只读核对 process_workflow 的停止入口、
DedupStage 的规划入口与公共终态匹配接口。审查者没有修改这些生产代码、对应测试或执行模型请求。

## 结论与已关闭问题

首次审查发现去重同步规划仍首错返回。两个分别超过真实 embed_budget 的序列进入 reserve_session，
只得到第一个位置的一个 failure；正式索引保持空、成员保持 active，因此这是容量事实遗漏和多余重算，
不是正式数据泄漏。问题已交给模块负责人修复，状态已从“待独立复核”更新为“独立复核通过”。

当前实现先冻结全部静态特征与同步错误，再由会话专用结算门统一收集容量失败；存在此类错误时，
尚未发送的 embedding 请求不派发，普通失败产品也不提前写入。ProviderFatal 保持运行级控制流。
真实预算边界的正反输入顺序测试、普通错误混合测试、致命错误优先测试均通过。已经派发的 embedding
波次继续按声明序收齐全部结果，再统一上抛容量失败。

修复后的本次责任范围没有其他已确认的未修复行为偏差。这个结论不替代其他模块、完整离线套件、
真实模型、覆盖率及 Uncle Bob 变异审查的独立验收。

## 行为与测试映射

下表节点位于
[test_session_workflow.py](../../../tests/orchestration/test_session_workflow.py)、
[test_dedup_session.py](../../../tests/operators/test_dedup_session.py)及
[test_process_workflow.py](../../../tests/orchestration/test_process_workflow.py)。函数名使用仓库原名，
参数化节点的全部参数均由下述实跑命令覆盖。

| 规格行为 | 可执行节点 | 独立观察 |
|---|---|---|
| 完整会话跨计算分组，上游只运行一次 | test_real_ingest_segment_dedup_emitter_preserve_full_session_across_compute_sizes；test_complete_session_runs_upstream_once_and_ignores_physical_group_size | 真正 ingest、segment、dedup、emitter 保留重复内容的全部出现位置；计算组大小不改变输出 |
| 完整波次只启动一次重算 | test_all_completed_wave_overflows_split_before_one_recomputation | 两个独立序列的超限在正反完成顺序下都新增两个切点、只重算一次，旧请求身份不重复出现 |
| 同步去重超限完整收集 | test_preparation_collects_all_real_overflows_before_any_embedding_or_item_commit；test_preparation_capacity_barrier_precedes_ordinary_failure_products；test_preparation_fatal_has_priority_after_complete_synchronous_plan | 两个真实预算超限位置均进入 failures；零请求派发、零产品/正式索引/计数写入；非容量程序错误不被伪装成容量 |
| 已完成 embedding 波次容量收集 | test_embedding_wave_collects_all_capacity_failures_in_declaration_order；test_embedding_fatal_control_dominates_capacity_collection | 结果映射的完成顺序不改变失败声明序；致命错误仍优先终止 |
| 过时父请求与重复失败不成为伪最小终态 | test_wave_overlapping_sequence_and_pair_targets_do_not_create_false_terminals；test_wave_discards_unreachable_lineage_without_marking_minimum_failure | 已被切分替代的 sequence/pairwise 目标丢弃，仍独立可达的目标继续推进 |
| 最小请求按允许范围投影 | test_wave_fixed_frame_and_transition_failures_follow_allowed_noise_positions；test_terminal_transition_projection_does_not_poison_different_child_request | 回收但未进入基线成员表的位置仍可归属；跨新切点的相邻对失效；固定请求投影到子视图 |
| 最小失败在所属阶段结算 | test_minimum_sequence_failure_is_projected_once_at_owner_gate；test_terminal_frame_gate_stays_at_leaf_and_known_repeat_fails_fast；test_minimum_pair_marks_both_views_without_repeating_pair_request | 前置去重仍执行；frame 留给叶门；pairwise 两侧终态且不重发；无进度重复被明确拒绝。stage/profile/label 的匹配细则使用公共契约唯一实现 |
| 固定内容不能靠切分补救 | test_fixed_failure_does_not_split_a_large_sequence | 一个固定失败只形成终态，不新增切点。完整空 user 包络边界另由 test_sequence_complete_evidence 的五路径参数测试验证 |
| 全下游与去重增量回滚 | test_reactive_splits_rebuild_whole_downstream_without_leaking_dedup_or_counts；test_control_failure_discards_attempt_and_keeps_formal_state；test_quality_rebuilds_complete_session_pool_after_each_repartition | 上游不重跑，RNG 重建稳定；只保留最终去重身份、质量池与数据计数，调用事实继续累计 |
| global/会话去重边界及并列规则 | test_reservation_queries_prefix_and_local_without_mutating_prefix；test_batch_scope_ignores_previous_session_but_only_resets_on_commit；test_near_and_semantic_queries_choose_highest_score_then_prefix_ties | 前缀只读，局部增量独立；最优相似度与并列前缀优先保持原规则，不深拷贝正式索引 |
| 去重簇、探测便签、向量及消费状态 | test_committed_cluster_is_a_readonly_prefix_for_new_attempts；test_precomputed_vectors_commit_with_admitted_identity_and_empty_commit_keeps_probe；test_stale_reservation_rejects_formal_commit | 废弃尝试不修改正式簇和探测便签；向量随身份提交；重复消费与过期预留被拒绝 |
| 最终去重接受身份及写后计数 | test_final_dedup_admissions_commit_even_when_later_filter_rejects；test_emit_rejection_is_counted_after_commit_without_reopening_capacity_attempt；test_repartition_counts_classification_fanout_separately_with_dedup_disabled | 下游过滤不追溯去重接受；emitter 拒绝以后置状态计量，提交后不回到容量重算；episode 与 fanout 分开 |
| 成员守恒与容量身份 | test_child_ids_depend_on_final_members_not_split_tree_and_clones_share_only_evidence；test_conservation_rejects_cross_cut_claims_and_duplicate_occurrences；test_conservation_rejects_missing_absorbed_claims | 子身份独立于切分树路径，真实成员引用保持，越界或重复认领在提交前拒绝 |
| 停止、取消与已完成会话 | test_control_failure_discards_attempt_and_keeps_formal_state；test_stream_interrupted_run_gains_unprocessed；test_stream_breaker_residual_includes_episodes_and_absorbed；test_request_stop_forwards_stop_requested | 未提交取消不写输出或正式状态；已完成会话仍结算；中断残差只核算未提交输入位置。30 秒定时器沿用 process_workflow._request_stop |
| 会话对象与错误调用栈释放 | test_terminal_traceback_does_not_retain_discarded_attempt；test_upstream_envelopes_and_attempts_release_before_next_session；test_every_error_in_capacity_wave_releases_old_traceback_even_when_superseded | 废弃尝试与所有容量错误的 traceback/context/cause 不保留旧证据；下一会话前释放上游信封。只证明实际对象寿命，不声称 byte/RSS 硬上限 |

## 实跑记录

首次审查执行同一责任范围命令得到 `54 passed, 121 deselected in 2.81s`，其后仍发现同步规划遗漏，
说明当时已有绿测试没有覆盖该组合。负责人修复并增加节点后，由审查者独立重新执行：

```bash
uv run --python 3.12 pytest -q \
  tests/orchestration/test_session_workflow.py \
  tests/operators/test_dedup_session.py \
  tests/orchestration/test_process_workflow.py \
  -k 'session or stream_interrupted or stream_breaker or request_stop'
```

结果：`58 passed, 121 deselected in 1.75s`。没有网络或模型请求。

## 本地容量工件断言修正

主规格明确初始贪心分区只封闭已被后段接替的前段，尾段仍可接续。因此真实 static 门禁原有的
“所有行 sealed”断言与规格不符；生产分区行为无需更改。真实测试与独立 checker 已改为精确断言
`[True] * (段数 - 1) + [False]`、`sealed = splits = 段数 - 1`，并逐段验证 before/after 是相同相邻切点、
allowed_positions 半开范围、完整连续成员覆盖及尾段 after 为空。reactive 仍要求全部段 sealed。

无模型请求地重新读取 `/tmp/labelkit-capacity-local-final/` 的既有真实工件，static 的三行与 reactive
的两行均通过加强后的独立 checker。真实集成文件收集为七个节点；这个工件复验不是一次新的七节点模型执行。
