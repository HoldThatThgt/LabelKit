"""v1.18 generation carrier 与 common 接口的手写冻结清单测试。"""
from __future__ import annotations

import dataclasses
import inspect
import re
import typing
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Literal, get_type_hints

import pytest

from labelkit.common.config import generation as config_generation
from labelkit.common.config._collect import _Collector
from labelkit.common.config.model import (
    ClassView,
    FrameClassView,
    LLMProfile,
    ResolvedConfig,
    ResolvedPaths,
)
from labelkit.common.contracts import generation as contracts_generation
from labelkit.common.contracts.execution import TaskExecutor
from labelkit.common.contracts.stage import RunContext
from labelkit.common.contracts.types import PipelineItem, Record, Usage
from labelkit.common.extensions.hooks import ResolvedHook
from labelkit.common.observability.obslog import MetricsSink
from labelkit.common.inference.llm_client import LLMClient, PromptBundle
from labelkit.common.inference.schema_engine import CallScope, SchemaEngine
from labelkit.operators import annotate, dedup, emitter, quality, verify
from labelkit.operators.generation import evaluate, planner, program, project, render, scenario, state
from labelkit.orchestration import sequence_workflow


CONFIG_FIELDS = {
    "SequenceClassGenerationConfig": (
        "instruction", "state_schema", "initial_state_source",
        "initial_state_catalog_path", "initial_state_catalog",
    ),
    "PayloadBindingSpec": ("payload_path", "state_phase", "state_path"),
    "RoleSpec": (
        "name", "frame_class", "actor", "read_roots", "write_roots",
        "publish_roots", "observers", "state_instruction", "pre_state_schema",
        "payload_bindings", "calendar_window",
    ),
    "GapSpec": ("name", "before", "after", "min_gap_us", "max_gap_us"),
    "SequencePattern": (
        "name", "sequence_class", "description", "roles", "order", "gaps",
        "max_span_us", "containments",
    ),
    "VariantSpec": (
        "name", "kind", "target", "outcome_schema", "expected_violation",
        "divergence_role",
    ),
    "CounterfactualSetSpec": ("name", "pattern", "count", "variants"),
    "InstructionOnlySpec": (
        "name", "sequence_class", "count", "len_range", "instruction", "state_schema",
    ),
    "TimelineSpec": (
        "timestamp_start_us", "utc_offset_minutes", "event_gap_us", "primary_sessions",
        "crossed_primary_sessions", "session_max_events", "session_max_span_us",
        "session_gap_us", "noise_events", "duplicate_sequences",
    ),
    "CalendarWindowSpec": ("name", "utc_offset_minutes", "days", "intervals_us"),
    "NoiseSpec": ("frame_class", "instruction", "topics"),
    "GenerationLimits": (
        "pattern_roles", "variants_per_counterfactual_set", "instruction_only_events",
        "scenario_seed_bytes", "state_or_outcome_schema_bytes", "frame_schema_bytes",
        "event_patch_bytes", "rendered_payload_bytes", "prompt_value_bytes",
        "repair_context_bytes", "prompt_text_bytes", "record_units", "stream_rows",
        "retained_content_bytes",
    ),
    "SequenceGenerationConfig": (
        "mode", "semantic_profile", "evaluation_profile", "max_slot_attempts",
        "state_validator", "patterns", "counterfactual_sets", "instruction_only",
        "timeline", "calendar_windows", "noise", "limits",
    ),
}


CONFIG_TYPES = {
    "SequenceClassGenerationConfig": (
        str, typing.Mapping[str, object], Literal["llm", "catalog"], str | None,
        tuple[typing.Mapping[str, object], ...],
    ),
    "PayloadBindingSpec": (str, Literal["before", "after"], str),
    "RoleSpec": (
        str, str, str, tuple[str, ...], tuple[str, ...], tuple[str, ...],
        tuple[str, ...], str, typing.Mapping[str, object] | None,
        tuple[config_generation.PayloadBindingSpec, ...], str | None,
    ),
    "GapSpec": (str, str, str, int, int),
    "SequencePattern": (
        str, str, str, tuple[config_generation.RoleSpec, ...], tuple[str, ...],
        tuple[config_generation.GapSpec, ...], int,
        tuple[config_generation.IntervalContainmentSpec, ...],
    ),
    "VariantSpec": (
        str, Literal["positive", "missing", "reordered", "interval_exceeded"],
        typing.Mapping[str, str | int], typing.Mapping[str, object],
        typing.Mapping[str, str], str | None,
    ),
    "CounterfactualSetSpec": (
        str, str, int, tuple[config_generation.VariantSpec, ...]),
    "InstructionOnlySpec": (
        str, str, int, tuple[int, int], str, typing.Mapping[str, object]),
    "TimelineSpec": (int, int, tuple[int, int], int, int, int, int, int, int, int),
    "CalendarWindowSpec": (
        str, int,
        tuple[Literal["mon", "tue", "wed", "thu", "fri", "sat", "sun"], ...],
        tuple[tuple[int, int], ...],
    ),
    "NoiseSpec": (str, str, tuple[str, ...]),
    "GenerationLimits": (int,) * 14,
    "SequenceGenerationConfig": (
        Literal["declared", "instruction_only"], str, str, int, ResolvedHook | None,
        tuple[config_generation.SequencePattern, ...],
        tuple[config_generation.CounterfactualSetSpec, ...],
        tuple[config_generation.InstructionOnlySpec, ...], config_generation.TimelineSpec,
        typing.Mapping[str, config_generation.CalendarWindowSpec],
        config_generation.NoiseSpec | None, config_generation.GenerationLimits,
    ),
}


CONTRACT_FIELDS = {
    "GenerationProgram": (
        "mode", "semantic_profile", "evaluation_profile", "max_slot_attempts",
        "planner_seed", "class_views", "frame_classes", "frame_schema", "patterns",
        "counterfactual_sets", "instruction_only", "timeline", "calendar_windows",
        "noise", "limits", "state_validator", "digest",
    ),
    "DeliverySlot": (
        "slot_key", "source_name", "scenario_index", "sequence_class", "pattern_name",
        "variant_names", "catalog_row_index",
    ),
    "PlannedEvent": (
        "event_key", "role", "position", "logical_time_us", "timestamp_us", "duration_us",
        "resources", "session_id",
    ),
    "NoiseSlot": (
        "event_key", "ordinal", "frame_class", "topic", "timestamp_us", "duration_us",
        "resources", "session_id",
    ),
    "ReplayLayout": (
        "source_slot_key", "source_variant_name", "replay_ordinal", "session_id",
        "shift_us",
    ),
    "ScenarioPlan": (
        "blocks", "delivery_slots", "noise_slots", "replay_layouts", "primary_sessions",
        "digest",
    ),
    "SequenceTemporalMember": ("event_id", "timestamp_us", "duration_us", "resources"),
    "SequenceTemporalContext": ("members",),
    "ScenarioSeed": ("initial_state", "actors", "shared_facts", "style", "time_context"),
    "ActorView": (
        "actor", "goal", "read_state", "observations", "logical_time_us",
        "wait_since_previous_us",
    ),
    "EventPlan": ("frame_class", "actor", "intent", "patch"),
    "EventExecution": (
        "state_before", "state_after", "state_before_hash", "state_after_hash",
        "publish_snapshot", "normalized_patch",
    ),
    "EventDraft": (
        "event_key", "event_id", "frame_class", "actor", "logical_time_us",
        "timestamp_us", "duration_us", "actor_view", "intent", "patch", "state_before_hash",
        "state_after_hash", "publish_snapshot", "payload",
    ),
    "EventTruth": (
        "event_key", "event_id", "role", "frame_class", "actor", "logical_time_us",
        "timestamp_us", "duration_us", "actor_view", "intent", "patch", "state_before_hash",
        "state_after_hash", "publish_snapshot", "payload",
    ),
    "ObservedEvent": ("event_id", "frame_class", "timestamp_us", "duration_us"),
    "SemanticReviewEvent": (
        "frame_class", "actor", "logical_time_us", "duration_us", "wait_since_previous_us",
        "actor_view",
        "intent", "patch", "state_before_hash", "state_after_hash", "publish_snapshot",
        "payload",
    ),
    "PatternEvaluation": ("actual_bindings", "actual_violations"),
    "StateEvaluation": (
        "replay_hash", "final_state_hash", "bindings_valid", "outcome_valid",
        "protected_prefix_valid",
    ),
    "SemanticEvaluation": (
        "causal_consistency", "actor_knowledge", "goal_consistency", "temporal_plausibility",
        "cross_frame_consistency", "realism", "reason_codes",
    ),
    "NoiseSemanticEvaluation": (
        "unrelated_to_declared_tasks", "no_executable_task", "realism",
        "matches_planned_topic", "reason_codes",
    ),
    "EventTrace": (
        "scenario_id", "world_branch_id", "sequence_class", "pattern_name", "variant_name",
        "scenario_seed", "events", "final_state", "pattern_evaluation", "state_evaluation",
        "semantic_evaluation",
    ),
    "GenerationParseContext": (
        "project_root", "class_views", "frame_classes", "llm_profiles",
        "max_repair_attempts", "repair_profile", "hook_loader", "collector",
    ),
    "ScenarioSeedRequest": ("program", "slot", "attempt_index", "random_seed"),
    "EventPlanRequest": (
        "mode", "semantic_profile", "slot_key", "planned_event", "role",
        "generation_instruction", "sequence_length", "eligible_frame_classes",
        "eligible_actors", "actor_view", "visible_state", "state_schema", "outcome_schema",
        "history", "actor_profiles", "public_facts", "attempt_index", "variation_nonce",
    ),
    "EventExecutionContext": (
        "program", "plan", "slot", "variant_name", "event_index", "scenario_seed",
        "current_state", "history",
    ),
    "StateTransitionInput": (
        "slot_key", "variant", "role", "state_before", "state_after", "patch",
    ),
    "PostValidationResult": ("violations", "event_execution"),
    "PostValidatedCallRequest": ("profile", "prompt", "schema", "scope", "post_validator"),
    "ValidatedGenerationCall": (
        "object", "event_execution", "resolved_at", "usage", "attempts", "model",
    ),
    "RenderEventRequest": (
        "semantic_profile", "slot_key", "planned_event", "event_plan", "actor_view",
        "publish_snapshot", "state_before_hash", "state_after_hash", "binding_values",
        "frame_spec", "role", "public_facts", "attempt_index", "utc_offset_minutes",
        "limits",
    ),
    "StateEvaluationRequest": (
        "program", "slot", "pattern", "variant", "scenario_seed", "events",
        "baseline_events", "final_state",
    ),
    "CouplingEvaluationRequest": (
        "variant", "baseline_events", "events", "frame_classes",
    ),
    "SemanticEvaluationRequest": (
        "evaluation_profile", "mode", "sequence_class", "class_description",
        "pattern_description", "scenario_seed", "review_events", "final_state",
        "attempt_index", "limits",
    ),
    "NoiseRenderRequest": (
        "semantic_profile", "noise_slot", "noise_spec", "frame_spec", "class_descriptions",
        "frame_descriptions", "attempt_index", "utc_offset_minutes", "limits",
    ),
    "NoiseEvaluationRequest": (
        "evaluation_profile", "payload", "planned_topic", "class_descriptions",
        "frame_descriptions", "attempt_index", "limits",
    ),
    "ProjectionRequest": ("program", "plan", "slot", "trace"),
    "NoiseProjectionRequest": ("program", "run_id", "noise_slot", "payload"),
    "ReplayProjectionRequest": ("program", "plan", "layout", "source"),
    "ProjectedSequence": ("main_record", "primary_stream_rows"),
    "SequenceRows": ("main_row", "primary_stream_rows", "retained_content_bytes"),
    "SequenceAssemblyRequest": (
        "program", "schema_engine", "item", "projection", "batch_no",
    ),
    "ReplayRows": ("rows", "retained_content_bytes"),
    "ProjectionWitness": (
        "main_record_id", "generation_digest", "member_sources_digest",
        "primary_base_digests",
    ),
    "PrimaryCandidateReconcileRequest": (
        "program", "plan", "run_id", "slot", "projection_witnesses", "sequences",
        "replay_layouts", "replays", "retained_content_bytes",
    ),
    "NoiseCandidateReconcileRequest": (
        "program", "run_id", "noise_slot", "payload_digest", "row",
        "retained_content_bytes",
    ),
    "ReconcileRequest": (
        "program", "plan", "run_id", "projection_witnesses", "sequences",
        "noise_payload_digests", "noise_rows", "replays", "retained_content_bytes",
    ),
    "GenerationServices": ("config", "schema_engine", "llm", "metrics", "tasks"),
    "DeliveryRequest": ("program", "plan", "paths", "run_attempt_id", "run_id"),
    "DeliveryServices": ("generation", "dedup", "quality", "annotate", "verify", "emitter"),
    "AttemptTransaction": ("items", "class_views", "projected_sequences"),
    "DownstreamAttemptRequest": ("transaction", "run_context"),
    "DownstreamAttemptResult": ("accepted", "rejected_stage", "dataset_counters"),
    "DedupGroupRequest": ("records", "exempt_pairs", "embedding_profile"),
    "DedupReservation": (
        "capability_id", "epoch", "record_digests", "exact_cluster_keys",
    ),
    "PreparedCandidate": (
        "slot", "attempt_index", "projection_witnesses", "sequences", "replays",
        "reservation", "dataset_counters", "retained_content_bytes", "digest",
    ),
    "PreparedNoiseCandidate": (
        "noise_slot", "attempt_index", "payload_digest", "row",
        "similarity_signature", "dataset_counters", "retained_content_bytes", "digest",
    ),
    "ResourceInterval": ("resource", "start_us", "end_us", "event_id", "source_key"),
    "CrossViewDelta": (
        "phase", "ordinal", "event_ids", "timestamps_us", "source_keys",
        "resource_intervals",
    ),
    "GenerationProduct": ("main_rows", "stream_rows", "report"),
}


_CARRIER_DOC_FIELDS = {
    name: CONTRACT_FIELDS[name]
    for name in (
        "GenerationProgram",
        "RenderEventRequest",
        "SemanticEvaluationRequest",
        "NoiseRenderRequest",
        "NoiseEvaluationRequest",
        "ProjectionRequest",
        "ReplayProjectionRequest",
        "SequenceAssemblyRequest",
        "PrimaryCandidateReconcileRequest",
        "NoiseCandidateReconcileRequest",
        "ReconcileRequest",
    )
}


_REPORT_REJECTION_FIELDS = (
    "scenario_schema", "event_schema", "post_validator_invalid",
    "post_validator_exception", "state_transition", "frame_schema",
    "coupling_evaluation", "pattern_evaluation", "state_evaluation",
    "semantic_evaluation", "sequence_memory_budget", "context_overflow",
    "output_truncated", "provider_retryable_exhausted", "dedup", "quality",
    "annotate", "verify", "reconcile", "noise_schema", "noise_semantic",
    "noise_similarity", "noise_memory_budget", "noise_context_overflow",
    "noise_output_truncated", "noise_provider_retryable_exhausted", "noise_reconcile",
)


def _markdown_table_fields(path: Path, name: str) -> tuple[str, ...]:
    """读取一个设计表格中的冻结字段序列。"""
    text = path.read_text(encoding="utf-8")
    pattern = re.compile(rf"^\|\s*`?{re.escape(name)}`?\s*\|\s*(.*?)\s*\|$", re.MULTILINE)
    rows = pattern.findall(text)
    assert len(rows) == 1, (path, name, rows)
    return tuple(re.findall(r"`([a-z_][a-z0-9_]*)`", rows[0]))


def _contracts_class_fields(path: Path, name: str) -> tuple[str, ...]:
    """读取 CONTRACTS 伪代码 dataclass 的冻结字段序列。"""
    text = path.read_text(encoding="utf-8")
    marker = f"class {name}:\n"
    assert text.count(marker) == 1, (path, name)
    block = text.split(marker, 1)[1].split("\n\n", 1)[0]
    return tuple(re.findall(r"^    ([a-z_][a-z0-9_]*):", block, re.MULTILINE))


def _markdown_section(path: Path, start: str, end: str) -> str:
    """读取两个唯一 Markdown 标记之间并折叠空白。"""
    text = path.read_text(encoding="utf-8")
    assert text.count(start) == 1, (path, start)
    assert text.count(end) == 1, (path, end)
    return " ".join(text.split(start, 1)[1].split(end, 1)[0].split())


def _markdown_method_signatures(
    path: Path,
    name: str,
) -> tuple[tuple[tuple[str, str | None], ...], ...]:
    """读取 Markdown Python 代码块中的全部方法参数签名。"""
    text = path.read_text(encoding="utf-8")
    pattern = re.compile(
        rf"^    def {re.escape(name)}\(\n(?P<body>(?:^        .*\n)+)^    \) ->",
        re.MULTILINE,
    )
    parameter = re.compile(r"^        ([a-z_][a-z0-9_]*)(?:: ([^,]+))?,$")
    signatures = []
    for body in pattern.findall(text):
        matches = tuple(parameter.fullmatch(line) for line in body.splitlines())
        assert all(match is not None for match in matches), (path, name, body)
        signatures.append(tuple((match.group(1), match.group(2)) for match in matches))
    return tuple(signatures)


def _report_rejection_fields(path: Path, marker: str) -> tuple[str, ...]:
    """读取一个 sequence report 示例中的拒绝桶序列。"""
    text = path.read_text(encoding="utf-8")
    section = text.split(marker, 1)[1]
    body = section.split('"rejected_attempts": {', 1)[1].split("\n  }", 1)[0]
    return tuple(re.findall(r'^\s+"([a-z_]+)": 0,?$', body, re.MULTILINE))


_JSON_OBJECT = Mapping[str, object]
_VIOLATION = Mapping[str, str]
_SEMANTIC_REASON = Literal[
    "causal_inconsistency", "actor_knowledge_violation", "goal_inconsistency",
    "temporal_implausibility", "cross_frame_inconsistency", "unrealistic",
]
_NOISE_REASON = Literal[
    "related_to_declared_task", "executable_task_present", "unrealistic",
    "planned_noise_topic_mismatch",
]


CONTRACT_TYPES = {
    "GenerationProgram": (
        Literal["declared", "instruction_only"], str, str, int, int,
        Mapping[str, ClassView], Mapping[str, FrameClassView], Mapping[str, object] | None,
        Mapping[str, config_generation.SequencePattern],
        tuple[config_generation.CounterfactualSetSpec, ...],
        tuple[config_generation.InstructionOnlySpec, ...], config_generation.TimelineSpec,
        Mapping[str, config_generation.CalendarWindowSpec], config_generation.NoiseSpec | None,
        config_generation.GenerationLimits, ResolvedHook | None, str,
    ),
    "DeliverySlot": (str, str, int, str, str | None, tuple[str, ...], int | None),
    "PlannedEvent": (str, str, int, int, int, int, tuple[str, ...], str),
    "NoiseSlot": (str, int, str, str, int, int, tuple[str, ...], str),
    "ReplayLayout": (str, str, int, str, int),
    "ScenarioPlan": (
        tuple[Mapping[
            tuple[str, str | None], tuple[contracts_generation.PlannedEvent, ...]
        ], ...],
        tuple[contracts_generation.DeliverySlot, ...],
        tuple[contracts_generation.NoiseSlot, ...],
        tuple[contracts_generation.ReplayLayout, ...], int, str,
    ),
    "SequenceTemporalMember": (str, int, int, tuple[str, ...]),
    "SequenceTemporalContext": (
        tuple[contracts_generation.SequenceTemporalMember, ...],),
    "ScenarioSeed": (
        _JSON_OBJECT, Mapping[str, Mapping[str, object]], _JSON_OBJECT,
        _JSON_OBJECT, _JSON_OBJECT,
    ),
    "ActorView": (
        str, _JSON_OBJECT, _JSON_OBJECT, tuple[_JSON_OBJECT, ...], int, int),
    "EventPlan": (str, str, str, tuple[_JSON_OBJECT, ...]),
    "EventExecution": (
        _JSON_OBJECT, _JSON_OBJECT, str, str, _JSON_OBJECT,
        tuple[_JSON_OBJECT, ...],
    ),
    "EventDraft": (
        str, str, str, str, int, int, int, contracts_generation.ActorView, str,
        tuple[_JSON_OBJECT, ...], str, str, _JSON_OBJECT, _JSON_OBJECT,
    ),
    "EventTruth": (
        str, str, str, str, str, int, int, int, contracts_generation.ActorView, str,
        tuple[_JSON_OBJECT, ...], str, str, _JSON_OBJECT, _JSON_OBJECT,
    ),
    "ObservedEvent": (str, str, int, int),
    "SemanticReviewEvent": (
        str, str, int, int, int, contracts_generation.ActorView, str,
        tuple[_JSON_OBJECT, ...], str, str, _JSON_OBJECT, _JSON_OBJECT,
    ),
    "PatternEvaluation": (Mapping[str, str], tuple[_VIOLATION, ...]),
    "StateEvaluation": (str, str, bool, bool, bool),
    "SemanticEvaluation": (bool, bool, bool, bool, bool, bool, tuple[_SEMANTIC_REASON, ...]),
    "NoiseSemanticEvaluation": (bool, bool, bool, bool, tuple[_NOISE_REASON, ...]),
    "EventTrace": (
        str, str, str, str | None, str | None, contracts_generation.ScenarioSeed,
        tuple[contracts_generation.EventTruth, ...], _JSON_OBJECT,
        contracts_generation.PatternEvaluation | None, contracts_generation.StateEvaluation,
        contracts_generation.SemanticEvaluation,
    ),
    "GenerationParseContext": (
        Path, Mapping[str, ClassView], Mapping[str, FrameClassView],
        Mapping[str, LLMProfile], int, str | None,
        Callable[[str, Path], ResolvedHook], _Collector,
    ),
    "ScenarioSeedRequest": (
        contracts_generation.GenerationProgram, contracts_generation.DeliverySlot, int, int),
    "EventPlanRequest": (
        Literal["declared", "instruction_only"], str, str,
        contracts_generation.PlannedEvent, config_generation.RoleSpec | None, str, int,
        Mapping[str, FrameClassView], tuple[str, ...], contracts_generation.ActorView | None,
        _JSON_OBJECT | None, Mapping[str, object] | None, Mapping[str, object] | None,
        tuple[contracts_generation.EventDraft, ...] | None,
        Mapping[str, Mapping[str, object]] | None, _JSON_OBJECT, int, str,
    ),
    "EventExecutionContext": (
        contracts_generation.GenerationProgram, contracts_generation.ScenarioPlan,
        contracts_generation.DeliverySlot, str | None, int,
        contracts_generation.ScenarioSeed, _JSON_OBJECT,
        tuple[contracts_generation.EventDraft, ...],
    ),
    "StateTransitionInput": (
        str, str | None, str | None, _JSON_OBJECT, _JSON_OBJECT,
        tuple[_JSON_OBJECT, ...],
    ),
    "PostValidationResult": (
        tuple[str, ...], contracts_generation.EventExecution | None),
    "PostValidatedCallRequest": (
        str, PromptBundle, _JSON_OBJECT, CallScope,
        Callable[[Mapping[str, object]], contracts_generation.PostValidationResult],
    ),
    "ValidatedGenerationCall": (
        _JSON_OBJECT, contracts_generation.EventExecution,
        Literal["l0_or_clean", "l1", "l3_1", "l3_2"], Usage, int, str,
    ),
    "RenderEventRequest": (
        str, str, contracts_generation.PlannedEvent, contracts_generation.EventPlan,
        contracts_generation.ActorView, _JSON_OBJECT, str, str, _JSON_OBJECT,
        FrameClassView, config_generation.RoleSpec | None, _JSON_OBJECT, int,
        int, config_generation.GenerationLimits,
    ),
    "StateEvaluationRequest": (
        contracts_generation.GenerationProgram, contracts_generation.DeliverySlot,
        config_generation.SequencePattern | None, config_generation.VariantSpec | None,
        contracts_generation.ScenarioSeed, tuple[contracts_generation.EventTruth, ...],
        tuple[contracts_generation.EventTruth, ...], _JSON_OBJECT,
    ),
    "CouplingEvaluationRequest": (
        config_generation.VariantSpec, tuple[contracts_generation.EventTruth, ...],
        tuple[contracts_generation.EventTruth, ...], Mapping[str, FrameClassView],
    ),
    "SemanticEvaluationRequest": (
        str, Literal["declared", "instruction_only"], str, str, str,
        contracts_generation.ScenarioSeed,
        tuple[contracts_generation.SemanticReviewEvent, ...], _JSON_OBJECT, int,
        config_generation.GenerationLimits,
    ),
    "NoiseRenderRequest": (
        str, contracts_generation.NoiseSlot, config_generation.NoiseSpec, FrameClassView,
        Mapping[str, str], Mapping[str, str], int, int,
        config_generation.GenerationLimits,
    ),
    "NoiseEvaluationRequest": (
        str, _JSON_OBJECT, str, Mapping[str, str], Mapping[str, str], int,
        config_generation.GenerationLimits),
    "ProjectionRequest": (
        contracts_generation.GenerationProgram, contracts_generation.ScenarioPlan,
        contracts_generation.DeliverySlot, contracts_generation.EventTrace,
    ),
    "NoiseProjectionRequest": (
        contracts_generation.GenerationProgram, str, contracts_generation.NoiseSlot,
        _JSON_OBJECT,
    ),
    "ReplayProjectionRequest": (
        contracts_generation.GenerationProgram, contracts_generation.ScenarioPlan,
        contracts_generation.ReplayLayout,
        contracts_generation.SequenceRows,
    ),
    "ProjectedSequence": (Record, tuple[_JSON_OBJECT, ...]),
    "SequenceRows": (_JSON_OBJECT, tuple[_JSON_OBJECT, ...], int),
    "SequenceAssemblyRequest": (
        contracts_generation.GenerationProgram, SchemaEngine, PipelineItem,
        contracts_generation.ProjectedSequence, int,
    ),
    "ReplayRows": (tuple[_JSON_OBJECT, ...], int),
    "ProjectionWitness": (str, str, str, tuple[str, ...]),
    "PrimaryCandidateReconcileRequest": (
        contracts_generation.GenerationProgram, contracts_generation.ScenarioPlan, str,
        contracts_generation.DeliverySlot,
        tuple[contracts_generation.ProjectionWitness, ...],
        tuple[contracts_generation.SequenceRows, ...],
        tuple[contracts_generation.ReplayLayout, ...],
        tuple[contracts_generation.ReplayRows, ...], int,
    ),
    "NoiseCandidateReconcileRequest": (
        contracts_generation.GenerationProgram, str, contracts_generation.NoiseSlot,
        str, _JSON_OBJECT, int,
    ),
    "ReconcileRequest": (
        contracts_generation.GenerationProgram, contracts_generation.ScenarioPlan, str,
        tuple[contracts_generation.ProjectionWitness, ...],
        tuple[contracts_generation.SequenceRows, ...], tuple[str, ...],
        tuple[_JSON_OBJECT, ...], tuple[contracts_generation.ReplayRows, ...], int,
    ),
    "GenerationServices": (ResolvedConfig, SchemaEngine, LLMClient, MetricsSink, TaskExecutor),
    "DeliveryRequest": (
        contracts_generation.GenerationProgram, contracts_generation.ScenarioPlan,
        ResolvedPaths, str, str,
    ),
    "DeliveryServices": (
        contracts_generation.GenerationServices, contracts_generation.DedupIndex,
        contracts_generation.DownstreamAttemptCollaborator | None,
        contracts_generation.DownstreamAttemptCollaborator | None,
        contracts_generation.DownstreamAttemptCollaborator | None,
        contracts_generation.SequenceDeliveryEmitter,
    ),
    "AttemptTransaction": (
        tuple[PipelineItem, ...], Mapping[str, ClassView],
        tuple[contracts_generation.ProjectedSequence, ...],
    ),
    "DownstreamAttemptRequest": (contracts_generation.AttemptTransaction, RunContext),
    "DownstreamAttemptResult": (
        bool, Literal["quality", "annotate", "verify"] | None, Mapping[str, int]),
    "DedupGroupRequest": (
        tuple[Record, ...], frozenset[tuple[str, str]], str | None),
    "DedupReservation": (str, int, tuple[str, ...], tuple[str, ...]),
    "PreparedCandidate": (
        contracts_generation.DeliverySlot, int,
        tuple[contracts_generation.ProjectionWitness, ...],
        tuple[contracts_generation.SequenceRows, ...],
        tuple[contracts_generation.ReplayRows, ...],
        contracts_generation.DedupReservation, Mapping[str, int], int, str,
    ),
    "PreparedNoiseCandidate": (
        contracts_generation.NoiseSlot, int, str, _JSON_OBJECT, tuple[int, ...],
        Mapping[str, int], int, str,
    ),
    "ResourceInterval": (str, int, int, str, str),
    "CrossViewDelta": (
        Literal["primary", "noise"], int, tuple[str, ...], tuple[int, ...], tuple[str, ...],
        tuple[contracts_generation.ResourceInterval, ...],
    ),
    "GenerationProduct": (
        tuple[_JSON_OBJECT, ...], tuple[_JSON_OBJECT, ...], _JSON_OBJECT),
}


INTERFACE_MANIFEST = (
    (config_generation.parse_generation_config, False, ("raw_project", "context"), {
        "raw_project": typing.Mapping[str, object],
        "context": contracts_generation.GenerationParseContext,
        "return": config_generation.SequenceGenerationConfig,
    }),
    (program.compile_generation_program, False, ("config",), {
        "config": ResolvedConfig, "return": contracts_generation.GenerationProgram,
    }),
    (program.generation_program_digest, False, ("program",), {
        "program": contracts_generation.GenerationProgram, "return": str,
    }),
    (planner.compile_scenario_plan, False, ("program",), {
        "program": contracts_generation.GenerationProgram,
        "return": contracts_generation.ScenarioPlan,
    }),
    (scenario.generate_scenario_seed, True, ("request", "services"), {
        "request": contracts_generation.ScenarioSeedRequest,
        "services": contracts_generation.GenerationServices,
        "return": contracts_generation.ScenarioSeed,
    }),
    (scenario.build_event_plan_request, False,
     ("context", "attempt_index", "variation_nonce"), {
         "context": contracts_generation.EventExecutionContext,
         "attempt_index": int, "variation_nonce": str,
         "return": contracts_generation.EventPlanRequest,
     }),
    (state.project_instruction_draft, False, ("draft",), {
        "draft": contracts_generation.EventDraft,
        "return": dict[str, object],
    }),
    (scenario.plan_event, True,
     ("context", "attempt_index", "variation_nonce", "services"), {
         "context": contracts_generation.EventExecutionContext,
         "attempt_index": int, "variation_nonce": str,
         "services": contracts_generation.GenerationServices,
         "return": tuple[contracts_generation.EventPlan, contracts_generation.EventExecution],
     }),
    (scenario.generate_slot_traces, True,
     ("program", "plan", "slot", "attempt_index", "services"), {
         "program": contracts_generation.GenerationProgram,
         "plan": contracts_generation.ScenarioPlan,
         "slot": contracts_generation.DeliverySlot,
         "attempt_index": int,
         "services": contracts_generation.GenerationServices,
         "return": tuple[contracts_generation.EventTrace, ...],
     }),
    (state.outcome_schema_for, False, ("context",), {
        "context": contracts_generation.EventExecutionContext,
        "return": typing.Mapping[str, object] | None,
    }),
    (state.execute_event, False, ("context", "event_plan"), {
        "context": contracts_generation.EventExecutionContext,
        "event_plan": contracts_generation.EventPlan,
        "return": contracts_generation.EventExecution,
    }),
    (state.post_validate_event_plan, False, ("candidate", "context"), {
        "candidate": typing.Mapping[str, object],
        "context": contracts_generation.EventExecutionContext,
        "return": contracts_generation.PostValidationResult,
    }),
    (render.render_event, True, ("request", "services"), {
        "request": contracts_generation.RenderEventRequest,
        "services": contracts_generation.GenerationServices,
        "return": typing.Mapping[str, object],
    }),
    (evaluate.evaluate_pattern, False, ("pattern", "events"), {
        "pattern": config_generation.SequencePattern,
        "events": typing.Sequence[contracts_generation.ObservedEvent],
        "return": contracts_generation.PatternEvaluation,
    }),
    (evaluate.evaluate_state, False, ("request",), {
        "request": contracts_generation.StateEvaluationRequest,
        "return": contracts_generation.StateEvaluation,
    }),
    (evaluate.evaluate_coupling, False, ("request",), {
        "request": contracts_generation.CouplingEvaluationRequest, "return": bool,
    }),
    (evaluate.evaluate_semantics, True, ("request", "services"), {
        "request": contracts_generation.SemanticEvaluationRequest,
        "services": contracts_generation.GenerationServices,
        "return": contracts_generation.SemanticEvaluation,
    }),
    (render.render_noise, True, ("request", "services"), {
        "request": contracts_generation.NoiseRenderRequest,
        "services": contracts_generation.GenerationServices,
        "return": typing.Mapping[str, object],
    }),
    (evaluate.evaluate_noise, True, ("request", "services"), {
        "request": contracts_generation.NoiseEvaluationRequest,
        "services": contracts_generation.GenerationServices,
        "return": contracts_generation.NoiseSemanticEvaluation,
    }),
    (project.project_trace, False, ("request",), {
        "request": contracts_generation.ProjectionRequest,
        "return": contracts_generation.ProjectedSequence,
    }),
    (project.project_noise, False, ("request",), {
        "request": contracts_generation.NoiseProjectionRequest,
        "return": typing.Mapping[str, object],
    }),
    (project.project_replay, False, ("request",), {
        "request": contracts_generation.ReplayProjectionRequest,
        "return": contracts_generation.ReplayRows,
    }),
    (project.projection_witness, False, ("projection",), {
        "projection": contracts_generation.ProjectedSequence,
        "return": contracts_generation.ProjectionWitness,
    }),
    (project.noise_payload_digest, False, ("payload",), {
        "payload": typing.Mapping[str, object], "return": str,
    }),
    (project.scenario_plan_digest, False, ("plan",), {
        "plan": contracts_generation.ScenarioPlan, "return": str,
    }),
    (project.validate_planned_events, False,
     ("program", "slot", "variant_name", "events"), {
        "program": contracts_generation.GenerationProgram,
        "slot": contracts_generation.DeliverySlot,
        "variant_name": str | None,
        "events": typing.Sequence[contracts_generation.PlannedEvent],
        "return": type(None),
    }),
    (project.validate_plan_identity, False, ("program", "plan"), {
        "program": contracts_generation.GenerationProgram,
        "plan": contracts_generation.ScenarioPlan,
        "return": type(None),
    }),
    (project.reconcile_views, False, ("request",), {
        "request": contracts_generation.ReconcileRequest, "return": type(None),
    }),
    (project.reconcile_primary_candidate, False, ("request",), {
        "request": contracts_generation.PrimaryCandidateReconcileRequest,
        "return": type(None),
    }),
    (project.reconcile_noise_candidate, False, ("request",), {
        "request": contracts_generation.NoiseCandidateReconcileRequest,
        "return": type(None),
    }),
    (sequence_workflow.deliver_generation, True, ("request", "services"), {
        "request": contracts_generation.DeliveryRequest,
        "services": contracts_generation.DeliveryServices,
        "return": contracts_generation.GenerationProduct,
    }),
    (project.derive_generation_id, False, ("domain", "components"), {
        "domain": str, "components": typing.Sequence[object], "return": str,
    }),
    (project.canonical_delivery_row, False, ("row",), {
        "row": typing.Mapping[str, object], "return": bytes,
    }),
)


METHOD_MANIFEST = (
    (contracts_generation.DownstreamAttemptCollaborator.run_attempt, True,
     ("self", "request"), {
         "request": contracts_generation.DownstreamAttemptRequest,
         "return": contracts_generation.DownstreamAttemptResult,
     }),
    (contracts_generation.DedupIndex.group_reserve, True,
     ("self", "request", "context"), {
         "request": contracts_generation.DedupGroupRequest, "context": RunContext,
         "return": contracts_generation.DedupReservation,
     }),
    (contracts_generation.DedupIndex.group_revalidate, False,
     ("self", "reservation"), {
         "reservation": contracts_generation.DedupReservation, "return": type(None),
     }),
    (contracts_generation.DedupIndex.group_commit, False,
     ("self", "reservation"), {
         "reservation": contracts_generation.DedupReservation, "return": type(None),
     }),
    (contracts_generation.DedupIndex.group_discard, False,
     ("self", "reservation"), {
         "reservation": contracts_generation.DedupReservation, "return": type(None),
    }),
    (contracts_generation.SequenceDeliveryEmitter.assemble_sequence, False,
     ("self", "request"), {
         "request": contracts_generation.SequenceAssemblyRequest,
         "return": contracts_generation.SequenceRows,
     }),
    (contracts_generation.SequenceDeliveryEmitter.prepare_product, False,
     ("self", "main_rows", "stream_rows", "report"), {
         "main_rows": Sequence[Mapping[str, object]],
         "stream_rows": Sequence[Mapping[str, object]], "report": Mapping[str, object],
         "return": contracts_generation.GenerationProduct,
     }),
    (contracts_generation.SequenceDeliveryEmitter.commit, False,
     ("self", "product"), {
         "product": contracts_generation.GenerationProduct,
         "return": Mapping[str, object],
     }),
    (contracts_generation.SequenceDeliveryEmitter.write_failed_report, False,
     ("self", "report"), {"report": Mapping[str, object], "return": type(None)}),
    (quality.QualityStage.run_attempt, True, ("self", "request"), {
        "request": contracts_generation.DownstreamAttemptRequest,
        "return": contracts_generation.DownstreamAttemptResult,
    }),
    (annotate.AnnotateStage.run_attempt, True, ("self", "request"), {
        "request": contracts_generation.DownstreamAttemptRequest,
        "return": contracts_generation.DownstreamAttemptResult,
    }),
    (verify.VerifyStage.run_attempt, True, ("self", "request"), {
        "request": contracts_generation.DownstreamAttemptRequest,
        "return": contracts_generation.DownstreamAttemptResult,
    }),
    (dedup.DedupIndex.group_reserve, True, ("self", "request", "context"), {
        "request": contracts_generation.DedupGroupRequest, "context": RunContext,
        "return": contracts_generation.DedupReservation,
    }),
    (dedup.DedupIndex.group_revalidate, False, ("self", "reservation"), {
        "reservation": contracts_generation.DedupReservation, "return": type(None),
    }),
    (dedup.DedupIndex.group_commit, False, ("self", "reservation"), {
        "reservation": contracts_generation.DedupReservation, "return": type(None),
    }),
    (dedup.DedupIndex.group_discard, False, ("self", "reservation"), {
        "reservation": contracts_generation.DedupReservation, "return": type(None),
    }),
    (project.CrossViewFrontier.check_primary, False, ("self", "candidate"), {
        "candidate": contracts_generation.PreparedCandidate,
        "return": contracts_generation.CrossViewDelta,
    }),
    (project.CrossViewFrontier.check_noise, False, ("self", "candidate"), {
        "candidate": contracts_generation.PreparedNoiseCandidate,
        "return": contracts_generation.CrossViewDelta,
    }),
    (project.CrossViewFrontier.commit, False, ("self", "delta"), {
        "delta": contracts_generation.CrossViewDelta, "return": type(None),
    }),
    (emitter.SequenceDeliveryEmitter.assemble_sequence, False,
     ("self", "request"), {
         "request": contracts_generation.SequenceAssemblyRequest,
         "return": contracts_generation.SequenceRows,
     }),
    (emitter.SequenceDeliveryEmitter.prepare_product, False,
     ("self", "main_rows", "stream_rows", "report"), {
         "main_rows": typing.Sequence[typing.Mapping[str, object]],
         "stream_rows": typing.Sequence[typing.Mapping[str, object]],
         "report": typing.Mapping[str, object],
         "return": contracts_generation.GenerationProduct,
     }),
    (emitter.SequenceDeliveryEmitter.commit, False, ("self", "product"), {
        "product": contracts_generation.GenerationProduct,
        "return": typing.Mapping[str, object],
    }),
    (emitter.SequenceDeliveryEmitter.write_failed_report, False,
     ("self", "report"), {
         "report": typing.Mapping[str, object], "return": type(None),
     }),
    (SchemaEngine.complete_post_validated, True, ("self", "request"), {
        "request": contracts_generation.PostValidatedCallRequest,
        "return": contracts_generation.ValidatedGenerationCall,
    }),
)


def _public_dataclasses(module) -> dict[str, type]:
    """返回当前模块自己声明的公开 dataclass。"""
    return {
        name: value for name, value in vars(module).items()
        if not name.startswith("_") and isinstance(value, type)
        and value.__module__ == module.__name__ and dataclasses.is_dataclass(value)
    }


@pytest.mark.parametrize(
    ("module", "manifest"),
    ((config_generation, CONFIG_FIELDS), (contracts_generation, CONTRACT_FIELDS)),
)
def test_generation_dataclass_manifest_is_exact(module, manifest):
    classes = _public_dataclasses(module)
    assert set(classes) == set(manifest)
    for name, expected in manifest.items():
        actual = classes[name]
        assert tuple(field.name for field in dataclasses.fields(actual)) == expected
        assert actual.__dataclass_params__.frozen is True


@pytest.mark.parametrize(
    ("module", "fields_manifest", "types_manifest"),
    (
        (config_generation, CONFIG_FIELDS, CONFIG_TYPES),
        (contracts_generation, CONTRACT_FIELDS, CONTRACT_TYPES),
    ),
)
def test_generation_dataclass_annotations_are_exact(
        module, fields_manifest, types_manifest):
    assert set(types_manifest) == set(fields_manifest)
    for name, expected in types_manifest.items():
        hints = get_type_hints(getattr(module, name))
        assert tuple(hints[field] for field in fields_manifest[name]) == expected


def test_internal_generation_carriers_have_no_implicit_defaults():
    for name in CONTRACT_FIELDS:
        for item in dataclasses.fields(getattr(contracts_generation, name)):
            assert item.default is dataclasses.MISSING
            assert item.default_factory is dataclasses.MISSING

    for name in CONFIG_FIELDS.keys() - {"GenerationLimits"}:
        for item in dataclasses.fields(getattr(config_generation, name)):
            assert item.default is dataclasses.MISSING
            assert item.default_factory is dataclasses.MISSING


def test_prepared_candidate_carriers_recursively_freeze_rows_and_counters():
    """候选进入乱序缓冲后不再共享调用方可变 JSON 或 dataset delta。"""
    slot = contracts_generation.DeliverySlot(
        "source/000000", "source", 0, "sequence", "pattern", ("positive",), 0,
    )
    witness = contracts_generation.ProjectionWitness(
        "a" * 32, "b" * 64, "c" * 64, ("d" * 64,),
    )
    sequence = contracts_generation.SequenceRows(
        {"_meta": {"id": "a" * 32}},
        ({"payload": {"text": "primary"}, "_meta": {}},),
        1,
    )
    reservation = contracts_generation.DedupReservation(
        "capability", 3, ("record",), ("cluster",),
    )
    counters = {"generated": 1}
    primary = contracts_generation.PreparedCandidate(
        slot, 1, (witness,), (sequence,), (), reservation, counters, 1, "digest",
    )
    noise_row = {"payload": {"text": "noise"}, "_meta": {"event": {"noise": True}}}
    noise_counters = {"generated": 1}
    noise = contracts_generation.PreparedNoiseCandidate(
        contracts_generation.NoiseSlot(
            "e" * 32, 0, "noise", "weather", 1000, 0, (), "noise_0",
        ),
        1,
        "f" * 64,
        noise_row,
        (1, 2),
        noise_counters,
        1,
        "noise-digest",
    )

    counters["generated"] = 9
    noise_counters["generated"] = 9
    noise_row["payload"]["text"] = "changed"

    assert primary.dataset_counters == {"generated": 1}
    assert noise.dataset_counters == {"generated": 1}
    assert noise.row["payload"]["text"] == "noise"


def test_generation_limit_defaults_are_exact():
    limits = config_generation.GenerationLimits()
    assert dataclasses.astuple(limits) == (
        32, 8, 8, 65536, 65536, 65536, 16384, 65536, 32768,
        32768, 32768, 500000, 500000, 536870912,
    )


def test_call_scope_manifest_is_exact():
    assert tuple(field.name for field in dataclasses.fields(CallScope)) == (
        "record_ids", "batch_no", "record", "user_treatment", "repair_context_bytes",
    )
    assert get_type_hints(CallScope) == {
        "record_ids": tuple[str, ...],
        "batch_no": int,
        "record": typing.Any,
        "user_treatment": bool | None,
        "repair_context_bytes": int | None,
    }
    assert CallScope() == CallScope((), 0, None, None, None)


@pytest.mark.parametrize(
    ("function", "is_async", "parameters", "expected_hints"),
    INTERFACE_MANIFEST + METHOD_MANIFEST,
)
def test_generation_interface_manifest_is_exact(
        function, is_async, parameters, expected_hints):
    signature = inspect.signature(function)
    assert inspect.iscoroutinefunction(function) is is_async
    assert tuple(signature.parameters) == parameters
    assert all(
        item.kind is inspect.Parameter.POSITIONAL_OR_KEYWORD
        and item.default is inspect.Parameter.empty
        for item in signature.parameters.values()
    )
    assert get_type_hints(function) == expected_hints
    doc = inspect.getdoc(function) or ""
    for name in parameters:
        if name != "self":
            assert f"@param {name} " in doc
    assert "@return " in doc


def test_changed_annotations_are_exact():
    event_request = get_type_hints(contracts_generation.EventPlanRequest)
    execution_context = get_type_hints(contracts_generation.EventExecutionContext)
    state_input = get_type_hints(contracts_generation.StateTransitionInput)
    parse_context = get_type_hints(contracts_generation.GenerationParseContext)
    assert event_request["history"] == tuple[contracts_generation.EventDraft, ...] | None
    assert event_request["state_schema"] == Mapping[str, object] | None
    assert event_request["outcome_schema"] == Mapping[str, object] | None
    assert execution_context["history"] == tuple[contracts_generation.EventDraft, ...]
    assert state_input["role"] == str | None
    assert parse_context["project_root"] is Path
    assert parse_context["hook_loader"] == Callable[[str, Path], contracts_generation.ResolvedHook]
    assert get_type_hints(contracts_generation.ReplayProjectionRequest)["source"] \
        is contracts_generation.SequenceRows
    assert get_type_hints(contracts_generation.PostValidatedCallRequest)["schema"] \
        == Mapping[str, object]


@pytest.mark.parametrize(("name", "expected"), _CARRIER_DOC_FIELDS.items())
def test_generation_carrier_fields_match_authoritative_markdown(
        name: str, expected: tuple[str, ...]):
    root = Path(__file__).parents[3]
    assert _markdown_table_fields(
        root / "docs/dev/SPEC-sequence-generation-redesign.md", name
    ) == expected
    assert _markdown_table_fields(root / "spec/40-ch4-data-structures.md", name) == expected
    assert _contracts_class_fields(root / "docs/CONTRACTS.md", name) == expected


def test_sequence_assembly_signature_matches_authoritative_markdown():
    root = Path(__file__).parents[3]
    expected = (("self", None), ("request", "SequenceAssemblyRequest"))
    sources = (
        root / "docs/dev/SPEC-sequence-generation-redesign.md",
        root / "docs/CONTRACTS.md",
        root / "spec/40-ch4-data-structures.md",
    )
    for path in sources:
        signatures = _markdown_method_signatures(path, "assemble_sequence")
        assert signatures and all(signature == expected for signature in signatures)
    emitter_spec = (root / "spec/311-m11-emitter.md").read_text(encoding="utf-8")
    assert "`SequenceDeliveryEmitter.assemble_sequence(request)`" in emitter_spec


def test_contracts_frame_only_and_segment_exception_semantics_are_frozen():
    path = Path(__file__).parents[3] / "docs/CONTRACTS.md"
    text = path.read_text(encoding="utf-8")
    normalized = " ".join(text.split())
    assert "sequence generation always supplies it" in normalized
    assert "A disabled classifier switch alone never clears an inherited v1.18 label" in normalized
    assert "all-member form when frame.classify is off" not in normalized
    assert "NEVER RAISES a record-level exception" not in normalized
    assert "no-raise contract" not in normalized

    annotate_member = _markdown_section(
        path, "async def annotate_member", "class AnnotateStage(Stage):",
    )
    required_member_failure = (
        "In ordinary process/flat member isolation",
        "content, Schema and ordinary",
        "provider failures including ProviderFatalError",
        "return None; the envelope may continue",
        "`SchemaViolation`, `ContextOverflowError`, `OutputTruncatedError`",
        "`ProviderRetryableError`",
        "are re-raised to `run_attempt` rather than converted to None",
        "retries the whole set",
        "`ProviderFatalError` is re-raised as a terminal sequence error",
    )
    assert all(fragment in annotate_member for fragment in required_member_failure)

    frame_pass = _markdown_section(
        path,
        "v1.12 frame pass (SPEC-frame-annotation §3.3",
        "### 7.5 M6 flat generation",
    )
    required_frame_pass = (
        "`annotate.enabled=false ⇒ direct frame pass`",
        "`frame.classify.enabled=false` does not imply `label=None`",
        "writes an inherited frame classification for every generated member",
        "`GenerationProgram.frame_classes[label]`",
        "Only a genuinely absent member classification in ordinary process/flat input",
        "submits pure pending-member TaskSpec leaves in declaration order",
        "recoverable member failures are typed frozen outcomes",
        "aligned results reduce in member declaration order",
        "whole-set retry",
    )
    assert all(fragment in frame_pass for fragment in required_frame_pass)
    assert "after a sequence envelope's OWN annotation succeeds — and only then" not in frame_pass
    assert "frame.classify off ⇒ label None ⇒ global instruction" not in frame_pass

    rule_43 = _markdown_section(
        path, "43. **帧粒度的 segment 边界**", "44. **帧类覆盖要求帧分类或序列生成**",
    )
    required_rule_43 = (
        "`frame.classify.enabled` ⇒ `segment.enabled = true`",
        '`frame.annotate.enabled ∧ generate.form!="sequence"` ⇒ `segment.enabled = true`',
        '`generate.form="sequence"` 是 frame annotation 的显式例外',
        "但绝不放宽 frame classification",
    )
    assert all(fragment in rule_43 for fragment in required_rule_43)
    assert "`frame.classify.enabled ∨ frame.annotate.enabled`" not in rule_43

    rule_49 = _markdown_section(
        path, "49. **frame 表停放警告**", "Sequence generation (v1.18",
    )
    assert "process/flat frame annotation takes rule 43's CONFIG_ERROR path" in rule_49
    assert "Sequence frame annotation is the explicit valid exception" in rule_49
    assert "produces neither rule 43 error nor parked warning" in rule_49


def test_sequence_report_rejection_fields_match_all_authoritative_sources():
    root = Path(__file__).parents[3]
    assert tuple(sequence_workflow._REJECTION_KEYS) == _REPORT_REJECTION_FIELDS
    sources = (
        (root / "docs/dev/SPEC-sequence-generation-redesign.md", "### 14.4 report"),
        (root / "spec/60-ch6-io-formats.md", "### 6.4.1 v1.20 sequence success report"),
        (root / "docs/CONTRACTS.md", "The frozen sequence report follows."),
    )
    for path, marker in sources:
        assert _report_rejection_fields(path, marker) == _REPORT_REJECTION_FIELDS


def test_common_generation_interface_signatures_are_exact():
    parse = inspect.signature(config_generation.parse_generation_config)
    assert tuple(parse.parameters) == ("raw_project", "context")
    assert all(item.default is inspect.Parameter.empty for item in parse.parameters.values())
    parse_hints = get_type_hints(config_generation.parse_generation_config)
    assert parse_hints["raw_project"] == typing.Mapping[str, object]
    assert parse_hints["context"] is contracts_generation.GenerationParseContext
    assert parse_hints["return"] is config_generation.SequenceGenerationConfig

    method = SchemaEngine.complete_post_validated
    signature = inspect.signature(method)
    assert inspect.iscoroutinefunction(method)
    assert tuple(signature.parameters) == ("self", "request")
    assert signature.parameters["request"].kind is inspect.Parameter.POSITIONAL_OR_KEYWORD
    hints = get_type_hints(method)
    assert hints["request"] is contracts_generation.PostValidatedCallRequest
    assert hints["return"] is contracts_generation.ValidatedGenerationCall


def test_mapping_carriers_recursively_copy_and_freeze_json():
    source = {"nested": {"values": [1, 2]}}
    value = config_generation.InstructionOnlySpec(
        "case", "support", 1, (2, 3), "instruction", source)
    source["nested"]["values"].append(3)
    assert value.state_schema["nested"]["values"] == (1, 2)
    with pytest.raises(TypeError):
        value.state_schema["nested"]["new"] = True

    execution = contracts_generation.EventExecution(
        {"nested": source}, {}, "before", "after", {}, ())
    source["new"] = True
    assert "new" not in execution.state_before["nested"]
    with pytest.raises(TypeError):
        execution.state_before["nested"]["blocked"] = True
