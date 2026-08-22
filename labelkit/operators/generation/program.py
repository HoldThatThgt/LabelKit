"""ResolvedConfig 到 v1.18 GenerationProgram 的纯编译器。"""
from __future__ import annotations

import dataclasses
import hashlib
import logging
from collections.abc import Mapping
from pathlib import Path

from labelkit.common.config.model import ResolvedConfig
from labelkit.common.contracts.generation import GenerationProgram
from labelkit.common.errors import ConfigError
from labelkit.operators.generation.project import canonical_json


_log = logging.getLogger("labelkit.generation.program")


def compile_generation_program(config: ResolvedConfig) -> GenerationProgram:
    """校验交付基数与 catalog 外壳并冻结程序摘要。

    @param config 完整解析配置。
    @return 不含随机规划结果的冻结生成程序。
    """
    sequence = config.sequence_generation
    errors = _compiler_errors(config)
    if errors:
        for error in errors:
            _log.error("generation program compile failed: %s", error)
        raise ConfigError(errors)
    patterns = {pattern.name: pattern for pattern in sequence.patterns}
    class_views = {
        name: dataclasses.replace(
            view,
            schema=(view.schema if view.schema is not None else config.user_schema),
        )
        for name, view in config.class_views.items()
    }
    base = GenerationProgram(
        mode=sequence.mode,
        semantic_profile=sequence.semantic_profile,
        evaluation_profile=sequence.evaluation_profile,
        max_slot_attempts=sequence.max_slot_attempts,
        planner_seed=config.run.seed,
        class_views=class_views,
        frame_classes=dict(config.frame_class_views),
        frame_schema=config.frame_schema,
        patterns=patterns,
        counterfactual_sets=sequence.counterfactual_sets,
        instruction_only=sequence.instruction_only,
        timeline=sequence.timeline,
        calendar_windows=dict(sequence.calendar_windows),
        noise=sequence.noise,
        limits=sequence.limits,
        state_validator=sequence.state_validator,
        digest="",
    )
    return dataclasses.replace(base, digest=generation_program_digest(base))


def _compiler_errors(config: "ResolvedConfig") -> list[str]:
    """收集 compiler 仍需证明的闭包错误。

    @param config 完整解析配置。
    @return 按发现序排列的错误文本。
    """
    if config.generate.form != "sequence" or config.sequence_generation is None:
        return ["generation compiler requires generate.form = sequence"]
    sequence = config.sequence_generation
    errors: list[str] = []
    primary_sequences = _primary_sequence_count(sequence)
    if sequence.timeline.primary_sessions != (
        primary_sequences - sequence.timeline.crossed_primary_sessions
    ):
        errors.append("generation timeline primary session identity is inconsistent")
    errors.extend(_catalog_errors(config, sequence))
    errors.extend(_scale_errors(sequence, primary_sequences))
    errors.extend(_replay_errors(sequence))
    return errors


def _primary_sequence_count(sequence) -> int:
    """计算精确 primary sequence 数。

    @param sequence 冻结 sequence 配置。
    @return primary sequence 总数。
    """
    if sequence.mode == "instruction_only":
        return sum(item.count for item in sequence.instruction_only)
    return sum(item.count * len(item.variants) for item in sequence.counterfactual_sets)


def _catalog_errors(config, sequence) -> list[str]:
    """验证 catalog 能按 class slot 数无放回分配。

    @param config 完整解析配置。
    @param sequence 冻结 sequence 配置。
    @return catalog 基数错误。
    """
    if sequence.mode != "declared":
        return []
    needed: dict[str, int] = {}
    for item in sequence.counterfactual_sets:
        pattern = next(pattern for pattern in sequence.patterns if pattern.name == item.pattern)
        needed[pattern.sequence_class] = needed.get(pattern.sequence_class, 0) + item.count
    errors: list[str] = []
    for class_name, count in needed.items():
        class_config = config.class_views[class_name].sequence_generation
        if class_config is None:
            errors.append(f"sequence class {class_name!r} has no generation config")
        elif class_config.initial_state_source == "catalog" and (
            len(class_config.initial_state_catalog) < count
        ):
            errors.append(
                f"sequence class {class_name!r} catalog has fewer rows than delivery slots"
            )
    return errors


def _scale_errors(sequence, primary_sequences: int) -> list[str]:
    """按 canonical planner 精确事件数验证 record/stream 上限。

    @param sequence 冻结 sequence 配置。
    @param primary_sequences primary sequence 总数。
    @return 上限错误。
    """
    primary_events = _primary_event_count(sequence)
    replay_events = _replay_event_count(sequence)
    stream_rows = primary_events + sequence.timeline.noise_events + replay_events
    record_units = primary_sequences + stream_rows
    errors = []
    if record_units < 1 or record_units > sequence.limits.record_units:
        errors.append("generation record_units exceeds the frozen limit")
    if stream_rows > sequence.limits.stream_rows:
        errors.append("generation stream_rows exceeds the frozen limit")
    return errors


def _primary_event_count(sequence) -> int:
    """计算 canonical planner 的精确 primary event 数。

    @param sequence 冻结 sequence 配置。
    @return primary event 精确数量。
    """
    if sequence.mode == "instruction_only":
        return sum(item.count * item.len_range[0] for item in sequence.instruction_only)
    patterns = {item.name: item for item in sequence.patterns}
    total = 0
    for item in sequence.counterfactual_sets:
        length = len(patterns[item.pattern].roles)
        per_set = sum(length - 1 if variant.kind == "missing" else length
                      for variant in item.variants)
        total += item.count * per_set
    return total


def _replay_event_count(sequence) -> int:
    """计算冻结 replay source 的事件总数。

    @param sequence 冻结 sequence 配置。
    @return replay event 数。
    """
    if sequence.mode != "declared" or sequence.timeline.duplicate_sequences == 0:
        return 0
    patterns = {item.name: item for item in sequence.patterns}
    sources: list[int] = []
    for item in sequence.counterfactual_sets:
        if any(variant.kind == "positive" for variant in item.variants):
            sources.extend([len(patterns[item.pattern].roles)] * item.count)
    return sum(sources[:sequence.timeline.duplicate_sequences])


def _replay_errors(sequence) -> list[str]:
    """验证 positive replay source 基数。

    @param sequence 冻结 sequence 配置。
    @return replay source 错误。
    """
    if sequence.mode != "declared":
        return []
    positive = sum(
        item.count
        for item in sequence.counterfactual_sets
        if any(variant.kind == "positive" for variant in item.variants)
    )
    if positive < sequence.timeline.duplicate_sequences:
        return ["generation has fewer positive sequences than duplicate_sequences"]
    return []


def generation_program_digest(program: GenerationProgram) -> str:
    """计算覆盖全部语义字段的 64-hex program digest。

    @param program 待校验或尚未写入 digest 的冻结程序。
    @return 64 位小写十六进制摘要。
    """
    material = canonical_json(_semantic_value(program))
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def _semantic_value(value):
    """把 dataclass 图转换为排除 callable 的 JSON-compatible 值。

    @param value 任意冻结配置值。
    @return 规范摘要输入。
    """
    if dataclasses.is_dataclass(value):
        if hasattr(value, "reference") and hasattr(value, "target"):
            return {"reference": value.reference}
        return {
            field.name: _semantic_value(getattr(value, field.name))
            for field in dataclasses.fields(value)
            if field.name != "digest"
        }
    if isinstance(value, Mapping):
        return {str(key): _semantic_value(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_semantic_value(item) for item in value]
    if isinstance(value, Path):
        return str(value)
    if callable(value):
        return None
    return value
