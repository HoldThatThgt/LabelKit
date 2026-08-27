"""ResolvedConfig 到 GenerationProgram 的编译与规模边界测试。"""

from __future__ import annotations

import resource
import sys
from dataclasses import replace
from types import MappingProxyType

import pytest

from labelkit.common.config.generation import InterleavingPatternSpec, InterleavingSpec
from labelkit.common.config.model import FewShotExample, TimeBindingSpec
from labelkit.common.errors import ConfigError
from labelkit.operators.generation.planner import compile_scenario_plan
from labelkit.operators.generation.program import (
    compile_generation_program,
    generation_program_digest,
)


_RSS_LIMIT_BYTES = 4 * 1024**3


def _thaw(value):
    """把配置层冻结 JSON 复制为测试可变树。"""
    if isinstance(value, MappingProxyType):
        return {key: _thaw(item) for key, item in value.items()}
    if hasattr(value, "items"):
        return {key: _thaw(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw(item) for item in value]
    return value


def _peak_rss_bytes() -> int:
    """把当前进程 peak RSS 统一为 byte。"""
    value = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return int(value if sys.platform == "darwin" else value * 1024)


def _instruction_scale_config(config, *, count: int, length: int):
    """构造只改变静态基数的 instruction-only 配置。"""
    sequence = config.sequence_generation
    source = replace(sequence.instruction_only[0], count=count, len_range=(length, length))
    timeline = replace(
        sequence.timeline,
        session_max_events=max(sequence.timeline.session_max_events, length),
    )
    return replace(
        config,
        sequence_generation=replace(
            sequence,
            instruction_only=(source,),
            timeline=timeline,
        ),
    )


def _max_declared_config(config):
    """构造 32 roles 与 8 variants 的最小合法声明程序。"""
    sequence = config.sequence_generation
    pattern = sequence.patterns[0]
    role_names = tuple(f"role_{index:02d}" for index in range(32))
    roles = tuple(
        replace(pattern.roles[0], name=name, calendar_window=None)
        for name in role_names
    )
    gaps = tuple(
        replace(
            pattern.gaps[0],
            name=f"gap_{index:02d}",
            before=role_names[index],
            after=role_names[index + 1],
            min_gap_us=1_000_000,
            max_gap_us=1_000_000,
        )
        for index in range(31)
    )
    maximum = replace(
        pattern,
        roles=roles,
        order=role_names,
        gaps=gaps,
        max_span_us=31_000_000,
    )
    source = sequence.counterfactual_sets[0]
    positive = next(item for item in source.variants if item.kind == "positive")
    missing = next(item for item in source.variants if item.kind == "missing")
    variants = (positive, *(
        replace(
            missing,
            name=f"missing_{name}",
            target={"role": name},
            expected_violation={"kind": "missing_role", "target": name},
            divergence_role=name,
        )
        for name in role_names[1:8]
    ))
    timeline = replace(
        sequence.timeline,
        session_max_events=32,
        noise_events=0,
        duplicate_sequences=0,
    )
    updated = replace(
        sequence,
        patterns=(maximum,),
        counterfactual_sets=(replace(source, count=1, variants=variants),),
        timeline=timeline,
        noise=None,
    )
    return replace(config, sequence_generation=updated)


def test_program_digest_is_deterministic_and_covers_frozen_semantics(declared_config):
    first = compile_generation_program(declared_config)
    second = compile_generation_program(declared_config)
    assert first == second
    assert len(first.digest) == 64
    assert first.calendar_windows
    assert first.state_validator.reference.endswith("hooks.py:validate_state")


def test_program_compiler_rejects_non_sequence_form(declared_config):
    """编译入口必须拒绝被协调篡改为 flat 的 ResolvedConfig。"""
    config = replace(
        declared_config,
        generate=replace(declared_config.generate, form="flat"),
    )
    with pytest.raises(ConfigError, match="requires generate.form = sequence"):
        compile_generation_program(config)


def _interleaving_config(config):
    """从教学配置构造两个候选集与一个合法交织 pattern。"""
    sequence = config.sequence_generation
    class_views = {
        name: replace(
            view,
            sequence_generation=replace(
                view.sequence_generation,
                initial_state_catalog_path=(
                    "/labelkit-test-fixture/catalogs/ticket-booking.jsonl"
                ),
            ),
        )
        for name, view in config.class_views.items()
    }
    state_validator = replace(
        sequence.state_validator,
        reference="/labelkit-test-fixture/hooks.py:validate_state",
    )
    source = sequence.counterfactual_sets[0]
    trigger = replace(
        source,
        name="trigger_source",
        count=1,
        interleaving_candidate_set="trigger_candidates",
    )
    partner = replace(
        source,
        name="partner_source",
        count=1,
        interleaving_candidate_set="partner_candidates",
    )
    pattern = InterleavingPatternSpec(
        "trigger_with_partner",
        "trigger_candidates",
        "partner_candidates",
        3,
    )
    updated = replace(
        sequence,
        state_validator=state_validator,
        counterfactual_sets=(trigger, partner),
        interleaving=InterleavingSpec(2, (pattern,)),
    )
    return replace(
        config,
        class_views=MappingProxyType(class_views),
        sequence_generation=updated,
    )


def test_program_digest_recursively_covers_interleaving_configuration(declared_config):
    """候选归属、pattern 关系和两个权重均进入 program digest。"""
    program = compile_generation_program(_interleaving_config(declared_config))
    assert program.digest == (
        "ee1c7ba6655081ea5771727682aa2297072235474f2a64bc21a79b3c846f3aec"
    )
    source = program.counterfactual_sets[0]
    pattern = program.interleaving.patterns[0]
    mutations = (
        replace(
            program,
            counterfactual_sets=(
                replace(source, interleaving_candidate_set="changed_candidates"),
                *program.counterfactual_sets[1:],
            ),
        ),
        replace(
            program,
            interleaving=replace(program.interleaving, no_interleaving_weight=5),
        ),
        replace(
            program,
            interleaving=replace(
                program.interleaving,
                patterns=(replace(pattern, trigger_candidate_set="changed_candidates"),),
            ),
        ),
        replace(
            program,
            interleaving=replace(
                program.interleaving,
                patterns=(replace(pattern, partner_candidate_set="changed_candidates"),),
            ),
        ),
        replace(
            program,
            interleaving=replace(
                program.interleaving,
                patterns=(replace(pattern, trigger_weight=4),),
            ),
        ),
    )
    expected = _interleaving_config(declared_config).sequence_generation.interleaving
    assert program.interleaving == expected
    assert all(generation_program_digest(item) != program.digest for item in mutations)


def test_program_copies_and_deep_freezes_class_and_frame_views(declared_config):
    class_schema = _thaw(declared_config.class_views["ticket_booking"].schema)
    frame_schema = _thaw(declared_config.frame_class_views["task_request"].gen_schema)
    example_output = {"nested": {"values": ["original"]}}
    class_views = dict(declared_config.class_views)
    class_views["ticket_booking"] = replace(
        class_views["ticket_booking"], schema=class_schema,
    )
    frame_views = dict(declared_config.frame_class_views)
    frame_views["task_request"] = replace(
        frame_views["task_request"],
        gen_schema=frame_schema,
        examples=(FewShotExample("example", example_output),),
    )
    program = compile_generation_program(
        replace(declared_config, class_views=class_views, frame_class_views=frame_views)
    )
    digest = program.digest

    class_schema["source_mutation"] = True
    frame_schema["source_mutation"] = True
    example_output["nested"]["values"].append("source_mutation")

    frozen_class = program.class_views["ticket_booking"].schema
    frozen_frame = program.frame_classes["task_request"]
    assert "source_mutation" not in frozen_class
    assert "source_mutation" not in frozen_frame.gen_schema
    assert frozen_frame.examples[0].output["nested"]["values"] == ("original",)
    assert generation_program_digest(program) == digest
    with pytest.raises(TypeError):
        frozen_class["blocked"] = True
    with pytest.raises(TypeError):
        frozen_frame.gen_schema["blocked"] = True
    with pytest.raises(TypeError):
        frozen_frame.examples[0].output["nested"]["blocked"] = True


def test_program_materializes_global_user_schema_and_freezes_frame_schema(declared_config):
    """program 显式闭包全局用户 Schema 与最终帧标注 Schema。"""
    user_schema = {
        "type": "object",
        "properties": {"result": {"type": "array", "items": {"type": "string"}}},
    }
    frame_schema = {
        "type": "object",
        "properties": {"frame": {"type": "object", "properties": {"ok": {}}}},
    }
    views = dict(declared_config.class_views)
    views["ticket_booking"] = replace(views["ticket_booking"], schema=None)
    config = replace(
        declared_config,
        class_views=views,
        user_schema=user_schema,
        frame_schema=frame_schema,
    )
    program = compile_generation_program(config)
    original_digest = program.digest

    user_schema["properties"]["result"]["items"]["type"] = "integer"
    frame_schema["properties"]["frame"]["properties"]["changed"] = {}
    assert program.class_views["ticket_booking"].schema["properties"]["result"][
        "items"
    ]["type"] == "string"
    assert "changed" not in program.frame_schema["properties"]["frame"]["properties"]
    assert generation_program_digest(program) == original_digest
    with pytest.raises(TypeError):
        program.frame_schema["blocked"] = True

    changed = replace(
        config,
        user_schema={"type": "object", "properties": {"other": {}}},
    )
    assert compile_generation_program(changed).digest != original_digest


def test_program_freezes_temporal_frame_and_model_schema_carriers(declared_config):
    full_schema = {
        "type": "object",
        "properties": {
            "timestamp": {"type": "integer", "x-labelkit-business-time": True},
            "text": {"type": "string"},
        },
        "required": ["timestamp", "text"],
        "additionalProperties": False,
    }
    model_schema = {
        "type": "object",
        "properties": {"text": {"type": "string"}},
        "required": ["text"],
        "additionalProperties": False,
    }
    frames = dict(declared_config.frame_class_views)
    frames["task_request"] = replace(
        frames["task_request"],
        gen_schema=full_schema,
        model_gen_schema=model_schema,
        business_time_paths=("/timestamp",),
        time_bindings=(TimeBindingSpec(
            "/timestamp", "event_start_milliseconds", None,
        ),),
        duration_us=10_000_000,
        resources=("foreground_app",),
    )

    program = compile_generation_program(
        replace(declared_config, frame_class_views=frames)
    )
    frozen = program.frame_classes["task_request"]
    digest = program.digest

    full_schema["properties"]["text"]["maxLength"] = 1
    model_schema["properties"]["text"]["maxLength"] = 1
    assert "maxLength" not in frozen.gen_schema["properties"]["text"]
    assert "maxLength" not in frozen.model_gen_schema["properties"]["text"]
    assert frozen.business_time_paths == ("/timestamp",)
    assert frozen.duration_us == 10_000_000
    assert frozen.resources == ("foreground_app",)
    assert generation_program_digest(program) == digest

    changed_frames = dict(program.frame_classes)
    changed_frames["task_request"] = replace(frozen, duration_us=11_000_000)
    changed = replace(program, frame_classes=changed_frames, digest="")
    assert generation_program_digest(changed) != digest


def test_program_rejects_record_unit_limit_at_compile_boundary(declared_config):
    sequence = replace(
        declared_config.sequence_generation,
        limits=replace(declared_config.sequence_generation.limits, record_units=1),
    )
    with pytest.raises(ConfigError, match="record_units"):
        compile_generation_program(replace(declared_config, sequence_generation=sequence))


def test_program_rejects_catalog_shorter_than_declared_slots(declared_config):
    view = declared_config.class_views["ticket_booking"]
    generation = replace(
        view.sequence_generation,
        initial_state_catalog=view.sequence_generation.initial_state_catalog[:1],
    )
    views = dict(declared_config.class_views)
    views["ticket_booking"] = replace(view, sequence_generation=generation)
    with pytest.raises(ConfigError, match="catalog has fewer rows"):
        compile_generation_program(replace(declared_config, class_views=views))


def test_program_accepts_500000_record_units_with_lightweight_carrier_oracle(instruction_config):
    """日常门验证精确编译边界；release gate 另用真实 planner 隔离压测。"""
    config = _instruction_scale_config(instruction_config, count=100_000, length=4)
    program = compile_generation_program(config)
    primary_sequences = sum(item.count for item in program.instruction_only)
    primary_events = sum(item.count * item.len_range[0] for item in program.instruction_only)
    assert primary_sequences + primary_events == 500_000

    minimal_row = MappingProxyType({"payload": MappingProxyType({}), "_meta": MappingProxyType({})})
    virtual_units = (minimal_row,) * 500_000
    assert len(virtual_units) == 500_000
    assert virtual_units[0] is virtual_units[-1] is minimal_row
    assert _peak_rss_bytes() < _RSS_LIMIT_BYTES


def test_planner_accepts_500000_record_units_without_interleaving(instruction_config):
    """真实 planner 冻结十万条四事件 sequence，并保持交织关闭。"""
    config = _instruction_scale_config(instruction_config, count=100_000, length=4)
    program = compile_generation_program(config)

    plan = compile_scenario_plan(program)

    assert plan.digest == "7b93a75e407382e24c4cd8dcfabf97cd9dfd30ff9c19ecbce166e2bfbd5d56ad"
    assert len(plan.delivery_slots) == 100_000
    assert sum(len(events) for block in plan.blocks for events in block.values()) == 400_000
    assert plan.interleaving_opportunities == 0
    assert plan.interleaving_layouts == ()
    assert plan.primary_sessions == 100_000
    assert _peak_rss_bytes() < _RSS_LIMIT_BYTES


def test_program_uses_canonical_minimum_instruction_length_for_exact_scale(instruction_config):
    """len_range 上界不得把 canonical 500000 units 误报为 600000。"""
    config = _instruction_scale_config(instruction_config, count=100_000, length=4)
    sequence = config.sequence_generation
    source = replace(sequence.instruction_only[0], len_range=(4, 5))
    config = replace(
        config,
        sequence_generation=replace(sequence, instruction_only=(source,)),
    )
    program = compile_generation_program(config)
    assert sum(item.count * (1 + item.len_range[0])
               for item in program.instruction_only) == 500_000


def test_program_rejects_500001_record_units(instruction_config):
    """compile 边界直接拒绝 500001 record units。"""
    config = _instruction_scale_config(instruction_config, count=99_999, length=4)
    sequence = config.sequence_generation
    extra = replace(
        sequence.instruction_only[0],
        name="one_more",
        count=1,
        len_range=(5, 5),
    )
    config = replace(
        config,
        sequence_generation=replace(
            sequence,
            instruction_only=(*sequence.instruction_only, extra),
        ),
    )
    assert sum(item.count * (1 + item.len_range[1])
               for item in config.sequence_generation.instruction_only) == 500_001
    with pytest.raises(ConfigError, match="record_units"):
        compile_generation_program(config)


def test_maximum_declared_roles_and_variants_produce_complete_plan(declared_config):
    """32 roles 与 8 variants 的合法边界完整进入冻结计划。"""
    program = compile_generation_program(_max_declared_config(declared_config))
    plan = compile_scenario_plan(program)
    visible = [
        events
        for block in plan.blocks
        for (_slot_key, variant), events in block.items()
        if variant is not None
    ]
    assert len(program.patterns["booking_success"].roles) == 32
    assert len(program.counterfactual_sets[0].variants) == 8
    assert len(visible) == 8
    assert sorted(map(len, visible)) == [31] * 7 + [32]
