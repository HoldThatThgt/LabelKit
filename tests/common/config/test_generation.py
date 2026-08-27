"""v1.21 sequence 配置的真实示例与聚合负例。"""
from __future__ import annotations

import json
import shutil
from pathlib import Path
from types import MappingProxyType

import pytest
from jsonschema import Draft202012Validator

from labelkit.common.config import load
from labelkit.common.config.model import CliOverrides
from labelkit.common.errors import ConfigError
from labelkit.orchestration.factory import build_stages


EXAMPLE_ROOT = Path(__file__).resolve().parents[3] / "examples" / "sequence-generation"


def _copied_example(tmp_path: Path) -> Path:
    """复制完整教学工程并返回临时根。"""
    root = tmp_path / "sequence-generation"
    shutil.copytree(EXAMPLE_ROOT, root)
    return root


def _load_example(project_name: str = "project.toml"):
    """通过公开 loader 装载仓库真实示例。"""
    return load(EXAMPLE_ROOT / "config.toml", EXAMPLE_ROOT / project_name, CliOverrides())


def _mutated_project(tmp_path: Path, old: str, new: str):
    """复制完整示例后精确替换一段 project 文本。"""
    root = _copied_example(tmp_path)
    project = root / "project.toml"
    text = project.read_text(encoding="utf-8")
    assert old in text
    project.write_text(text.replace(old, new, 1), encoding="utf-8")
    return root


def _mutated_named_project(tmp_path: Path, project_name: str, old: str, new: str):
    """复制完整示例后精确修改指定 project 文件。"""
    root = tmp_path / "sequence-generation"
    shutil.copytree(EXAMPLE_ROOT, root)
    project = root / project_name
    text = project.read_text(encoding="utf-8")
    assert old in text
    project.write_text(text.replace(old, new, 1), encoding="utf-8")
    return root


def _named_errors(root: Path, project_name: str) -> list[str]:
    """返回指定 project 文件的一次公开 loader 聚合错误。"""
    with pytest.raises(ConfigError) as captured:
        load(root / "config.toml", root / project_name, CliOverrides())
    return captured.value.errors


def _mutated_config(tmp_path: Path, old: str, new: str):
    """复制完整示例后精确替换一段 tool 配置。"""
    root = tmp_path / "sequence-generation"
    shutil.copytree(EXAMPLE_ROOT, root)
    config = root / "config.toml"
    text = config.read_text(encoding="utf-8")
    assert old in text
    config.write_text(text.replace(old, new, 1), encoding="utf-8")
    return root


def _errors(root: Path) -> list[str]:
    """返回一次公开 loader 聚合的全部错误。"""
    with pytest.raises(ConfigError) as captured:
        load(root / "config.toml", root / "project.toml", CliOverrides())
    return captured.value.errors


def _has(errors: list[str], text: str) -> None:
    """断言至少一条聚合错误含目标文本。"""
    assert any(text in item for item in errors), "expected config error was absent"


def test_declared_example_loads_through_public_loader():
    cfg = _load_example()
    generation = cfg.sequence_generation
    assert generation is not None and generation.mode == "declared"
    assert (generation.semantic_profile, generation.evaluation_profile) == ("default", "judge")
    assert generation.max_slot_attempts == 8
    assert tuple(cfg.class_views) == ("ticket_booking",)
    assert tuple(cfg.frame_class_views) == (
        "task_request", "acknowledgement", "confirmation", "noise")
    class_generation = cfg.class_views["ticket_booking"].sequence_generation
    assert class_generation is not None
    assert class_generation.initial_state_source == "catalog"
    assert class_generation.initial_state_catalog_path.endswith("catalogs/ticket-booking.jsonl")
    assert len(class_generation.initial_state_catalog) == 13
    assert class_generation.state_schema["type"] == "object"
    assert class_generation.instruction.startswith("围绕 catalog")
    assert isinstance(class_generation.state_schema, MappingProxyType)

    assert len(generation.patterns) == 1
    pattern = generation.patterns[0]
    assert (pattern.name, pattern.sequence_class) == ("booking_success", "ticket_booking")
    assert pattern.description.startswith("请求者提出订票需求")
    assert pattern.order == ("request", "acknowledge", "confirm")
    assert pattern.max_span_us == 2_400_000_000
    assert tuple((item.name, item.before, item.after, item.min_gap_us, item.max_gap_us)
                 for item in pattern.gaps) == (
        ("request_to_acknowledge", "request", "acknowledge", 5_000_000, 120_000_000),
        ("acknowledge_to_confirm", "acknowledge", "confirm", 30_000_000, 1_200_000_000),
    )
    request = pattern.roles[0]
    assert (request.name, request.frame_class, request.actor) == (
        "request", "task_request", "requester")
    assert request.read_roots == ("/public", "/goal", "/request", "/actors/requester")
    assert request.write_roots == ("/request/status",)
    assert request.observers == ("requester", "system")
    assert request.pre_state_schema is not None and request.calendar_window == "service_hours"
    assert tuple((item.payload_path, item.state_phase, item.state_path)
                 for item in request.payload_bindings) == (
        ("/request_id", "after", "/request/id"),
        ("/status", "after", "/request/status"),
    )
    assert len(generation.counterfactual_sets) == 1
    assert generation.counterfactual_sets[0].interleaving_candidate_set is None
    assert generation.interleaving is None
    variants = generation.counterfactual_sets[0].variants
    assert tuple(item.name for item in variants) == (
        "positive", "missing_acknowledgement",
        "confirmation_before_acknowledgement", "confirmation_timeout",
    )
    assert variants[1].expected_violation == {
        "kind": "missing_role", "target": "acknowledge"}
    assert variants[3].expected_violation == {
        "kind": "gap_above_max", "target": "acknowledge_to_confirm"}
    assert tuple(item.divergence_role for item in variants) == (
        None, "acknowledge", "acknowledge", "confirm")
    assert variants[0].target == {}
    assert variants[1].target == {"role": "acknowledge"}
    assert variants[2].target == {"before": "acknowledge", "after": "confirm"}
    assert variants[3].target == {
        "gap": "acknowledge_to_confirm",
        "min_excess_us": 1_000_000,
        "max_excess_us": 600_000_000,
    }
    timeline = generation.timeline
    assert timeline.timestamp_start_us == 1_767_574_800_000_000
    assert timeline.utc_offset_minutes == 480
    assert timeline.event_gap_us == (5_000_000, 60_000_000)
    assert (timeline.session_max_events, timeline.session_max_span_us) == (8, 3_600_000_000)
    assert (timeline.session_gap_us, timeline.noise_events, timeline.duplicate_sequences) == (
        3_600_000_000, 2, 1)
    window = generation.calendar_windows["service_hours"]
    assert (window.utc_offset_minutes, window.days) == (
        480, ("mon", "tue", "wed", "thu", "fri"))
    assert window.intervals_us == (
        (28_800_000_000, 43_200_000_000),
        (46_800_000_000, 64_800_000_000),
    )
    assert generation.noise is not None
    assert (generation.noise.frame_class, generation.noise.instruction) == (
        "noise", "生成与任何任务无关、没有可执行诉求的一句自然闲聊。")
    assert generation.noise.topics == (
        "夜空中的月相观察", "手工面包出炉时的香气",
    )
    assert tuple(vars(generation.limits).values()) == (
        32, 8, 8, 65536, 65536, 65536, 16384, 65536,
        32768, 32768, 32768, 500000, 500000, 536870912,
    )
    assert cfg.frame_class_views["confirmation"].gen_schema["$defs"]
    assert cfg.frame_class_views["noise"].gen_instruction.startswith("生成一句")
    assert cfg.paths.stream.endswith("sequence-labels.stream.jsonl")
    assert cfg.paths.manifest.endswith("sequence-labels.manifest.json")
    assert cfg.paths.failed_report.endswith("sequence-labels.failed.report.json")
    assert cfg.paths.rejects is None and cfg.paths.sidecar is None


@pytest.mark.parametrize("suffix", (
    ".jsonl", ".stream.jsonl", ".report.json", ".manifest.json",
    ".failed.report.json", ".jsonl.part", ".stream.jsonl.part",
    ".report.json.part", ".manifest.json.part", ".failed.report.json.part",
))
def test_sequence_existing_non_file_fixed_or_part_target_fails_startup(tmp_path, suffix):
    """五个 fixed 与五个同目录 part 目标若为目录，必须在 M1 聚合失败。"""
    root = _copied_example(tmp_path)
    target = root / "out" / f"sequence-labels{suffix}"
    if target.is_file() or target.is_symlink():
        target.unlink()
    target.mkdir(parents=True, exist_ok=True)
    _has(_errors(root), "existing sequence target must be a writable regular file")


def test_sequence_existing_symlink_part_target_fails_startup(tmp_path):
    """既存 part 符号链接不得把交付重定向到声明外文件。"""
    root = _copied_example(tmp_path)
    target = root / "out" / "sequence-labels.jsonl.part"
    target.symlink_to(root / "project.toml")
    _has(_errors(root), "existing sequence target must be a writable regular file")


def test_sequence_frame_annotation_loads_without_segment_or_sequence_annotation(tmp_path):
    """sequence 形态允许 quality 与 frame annotation 独立组成合法下游链。"""
    quality = '[quality]\nenabled = true\nmode = "pointwise"\nthreshold = 0.5'
    root = _mutated_project(tmp_path, "[quality]\nenabled = false", quality)
    project = root / "project.toml"
    text = project.read_text(encoding="utf-8")
    text = text.replace("[annotate]\nenabled = true", "[annotate]\nenabled = false", 1)
    frame = (
        '[frame.annotate]\nenabled = true\nllm = "default"\n'
        'instruction = "标注每一帧。"\nschema_path = "schemas/frame-request.json"\n\n'
    )
    text = text.replace("[quality]\n", frame + "[quality]\n", 1)
    project.write_text(text, encoding="utf-8")
    cfg = load(root / "config.toml", project, CliOverrides())
    assert cfg.segment.enabled is False
    assert cfg.annotate.enabled is False and cfg.quality.enabled is True
    assert cfg.frame_annotate.enabled is True and cfg.frame_schema is not None
    assert [stage.name for stage in build_stages(cfg)] == ["dedup", "quality", "annotate"]


def test_blocked_acknowledgement_schema_forbids_repeating_terminal_result():
    """迟到回执只引用先前通知，不再复述能否出票。"""
    schema = _load_example().frame_class_views["acknowledgement"].gen_schema
    validator = Draft202012Validator(schema)
    good = {
        "utterance": "已收到您的 R-EXAMPLE 订票请求；此次补充确认不影响先前通知。",
        "request_id": "R-EXAMPLE",
        "status": "blocked",
    }
    bad = {
        "utterance": "R-EXAMPLE 请求无法完成出票，此前未能出票的结果保持不变。",
        "request_id": "R-EXAMPLE",
        "status": "blocked",
    }
    wrong_object = {
        "utterance": "已收到 R-EXAMPLE 订票请求的补充确认；先前通知不变。",
        "request_id": "R-EXAMPLE",
        "status": "blocked",
    }
    assert not list(validator.iter_errors(good))
    assert list(validator.iter_errors(bad))
    assert list(validator.iter_errors(wrong_object))


def test_instruction_only_example_loads_through_public_loader():
    cfg = _load_example("project-instruction-only.toml")
    generation = cfg.sequence_generation
    assert generation is not None and generation.mode == "instruction_only"
    assert generation.patterns == () and generation.counterfactual_sets == ()
    assert generation.interleaving is None
    assert len(generation.instruction_only) == 1
    item = generation.instruction_only[0]
    assert (item.name, item.sequence_class, item.count, item.len_range) == (
        "open_booking", "ticket_booking", 1, (3, 4))
    assert item.state_schema["type"] == "object"
    assert item.state_schema["$defs"]["actor_state"]["type"] == "object"
    assert cfg.class_views["ticket_booking"].sequence_generation is None
    assert tuple(cfg.frame_class_views) == (
        "task_request", "acknowledgement", "confirmation")
    assert generation.calendar_windows == {} and generation.noise is None
    assert generation.timeline.duplicate_sequences == 0


def test_instruction_only_allows_non_generatable_registry_frame_but_excludes_it_later(
    tmp_path,
):
    """M1 允许普通参考帧与可生成帧共存，不要求所有 registry 成员可生成。"""
    addition = '''[frame.class.reference]
description = "仅用于处理既有数据的参考帧"

[[generate.instruction_only]]'''
    root = _mutated_named_project(
        tmp_path,
        "project-instruction-only.toml",
        "[[generate.instruction_only]]",
        addition,
    )
    cfg = load(
        root / "config.toml",
        root / "project-instruction-only.toml",
        CliOverrides(),
    )
    reference = cfg.frame_class_views["reference"]
    assert reference.description and reference.gen_instruction is None
    assert reference.gen_schema is None


def test_sequence_rejects_deleted_stream_surface(tmp_path):
    root = _mutated_project(
        tmp_path, 'max_slot_attempts = 8\n',
        'max_slot_attempts = 8\nstream = { enabled = true }\n')
    _has(_errors(root), "deleted sequence key")


def test_sequence_profiles_must_be_distinct_and_budgeted(tmp_path):
    root = _mutated_project(
        tmp_path, 'evaluation_llm = "judge"', 'evaluation_llm = "default"')
    _has(_errors(root), "must differ from semantic_llm")

    root = _mutated_config(tmp_path / "small", "context_window = 131072",
                           "context_window = 8500")
    _has(_errors(root), "complete sequence prompt and Schema do not fit")


def test_generation_schema_requires_a_valid_root_example(tmp_path):
    root = tmp_path / "sequence-generation"
    shutil.copytree(EXAMPLE_ROOT, root)
    schema = root / "schemas" / "frame-request.json"
    schema.write_text(
        schema.read_text(encoding="utf-8").replace('"examples": [', '"sample_values": [', 1),
        encoding="utf-8",
    )
    _has(_errors(root), "requires a non-empty root examples array")

    root = tmp_path / "invalid" / "sequence-generation"
    shutil.copytree(EXAMPLE_ROOT, root)
    schema = root / "schemas" / "frame-request.json"
    schema.write_text(
        schema.read_text(encoding="utf-8").replace(
            '"utterance": "请帮我预订车票。"', '"utterance": ""', 1
        ),
        encoding="utf-8",
    )
    _has(_errors(root), "root examples contain no valid object")


def test_unused_declared_frame_schema_does_not_require_a_root_example(tmp_path):
    root = tmp_path / "sequence-generation"
    shutil.copytree(EXAMPLE_ROOT, root)
    project = root / "project.toml"
    project.write_text(
        project.read_text(encoding="utf-8")
        + '\n[frame.class.unused]\ndescription = "未引用帧"\n'
        + '[frame.class.unused.generate]\ninstruction = "不会被调用"\n'
        + "schema_inline = '{\"type\":\"object\"}'\n",
        encoding="utf-8",
    )
    cfg = load(root / "config.toml", project, CliOverrides())
    assert cfg.frame_class_views["unused"].gen_schema == {"type": "object"}


@pytest.mark.parametrize(("project_name", "schema_name"), (
    ("project-instruction-only.toml", "state.json"),
    ("project.toml", "outcome-positive.json"),
))
def test_state_and_outcome_schemas_require_root_examples(
    tmp_path, project_name, schema_name,
):
    root = tmp_path / "sequence-generation"
    shutil.copytree(EXAMPLE_ROOT, root)
    schema = root / "schemas" / schema_name
    schema.write_text(
        schema.read_text(encoding="utf-8").replace('"examples": [', '"samples": [', 1),
        encoding="utf-8",
    )
    _has(_named_errors(root, project_name), "requires a non-empty root examples array")


@pytest.mark.parametrize(("project_name", "schema_name"), (
    ("project.toml", "frame-request.json"),
    ("project.toml", "outcome-positive.json"),
    ("project-instruction-only.toml", "state.json"),
))
def test_explicit_default_shaped_runtime_schema_still_requires_examples(
    tmp_path, project_name, schema_name,
):
    root = tmp_path / "sequence-generation"
    shutil.copytree(EXAMPLE_ROOT, root)
    schema = root / "schemas" / schema_name
    schema.write_text('{"type":"object"}', encoding="utf-8")
    _has(_named_errors(root, project_name), "requires a non-empty root examples array")


def test_declared_llm_default_shaped_state_schema_requires_examples(tmp_path):
    root = tmp_path / "sequence-generation"
    shutil.copytree(EXAMPLE_ROOT, root)
    project = root / "project.toml"
    text = project.read_text(encoding="utf-8").replace(
        'initial_state_source = "catalog"\n'
        'initial_state_catalog_path = "catalogs/ticket-booking.jsonl"',
        'initial_state_source = "llm"',
        1,
    )
    project.write_text(text, encoding="utf-8")
    (root / "schemas" / "state.json").write_text('{"type":"object"}', encoding="utf-8")
    _has(_errors(root), "requires a non-empty root examples array")


def test_instruction_only_default_state_uses_fixed_empty_witness(tmp_path):
    root = tmp_path / "sequence-generation"
    shutil.copytree(EXAMPLE_ROOT, root)
    project = root / "project-instruction-only.toml"
    text = project.read_text(encoding="utf-8").replace(
        'state_schema_path = "schemas/state.json"\n\n[generate.timeline]',
        "[generate.timeline]",
        1,
    )
    project.write_text(text, encoding="utf-8")
    cfg = load(root / "config.toml", project, CliOverrides())
    assert cfg.sequence_generation.instruction_only[0].state_schema == {"type": "object"}


def test_declared_llm_budget_schema_uses_observer_actor_union(tmp_path, monkeypatch):
    from labelkit.common.inference import schema_engine as schema_module

    root = tmp_path / "sequence-generation"
    shutil.copytree(EXAMPLE_ROOT, root)
    project = root / "project.toml"
    text = project.read_text(encoding="utf-8").replace(
        'initial_state_source = "catalog"\n'
        'initial_state_catalog_path = "catalogs/ticket-booking.jsonl"',
        'initial_state_source = "llm"',
        1,
    ).replace(
        'observers = ["requester", "system"]',
        'observers = ["requester", "system", "auditor"]',
        1,
    )
    project.write_text(text, encoding="utf-8")
    observed = []
    original = schema_module.scenario_seed_schema

    def capture(actor_names, state_schema):
        observed.append(actor_names)
        return original(actor_names, state_schema)

    monkeypatch.setattr(schema_module, "scenario_seed_schema", capture)
    load(root / "config.toml", project, CliOverrides())
    assert ("requester", "system", "auditor") in observed


def test_generation_schema_selects_smallest_valid_root_example(tmp_path):
    root = tmp_path / "sequence-generation"
    shutil.copytree(EXAMPLE_ROOT, root)
    schema = root / "schemas" / "frame-request.json"
    value = json.loads(schema.read_text(encoding="utf-8"))
    value["properties"]["utterance"]["maxLength"] = 400
    value["examples"].insert(0, {
        "utterance": "x" * 300,
        "request_id": "R-LARGE",
        "status": "pending",
    })
    schema.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
    assert load(root / "config.toml", root / "project.toml", CliOverrides())


def test_generation_schema_rejects_oversized_smallest_root_example(tmp_path):
    root = tmp_path / "sequence-generation"
    shutil.copytree(EXAMPLE_ROOT, root)
    schema = root / "schemas" / "frame-request.json"
    value = json.loads(schema.read_text(encoding="utf-8"))
    value["properties"]["utterance"]["maxLength"] = 40_000
    value["examples"] = [{
        "utterance": "x" * 32_768,
        "request_id": "R-LARGE",
        "status": "pending",
    }]
    schema.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
    _has(_errors(root), "smallest valid root example exceeds the prompt value byte limit")


def test_resolved_outcome_schema_reaches_frozen_variant_carrier(tmp_path):
    root = _mutated_config(
        tmp_path,
        "context_window = 131072",
        "context_window = 131072",
    )
    load(root / "config.toml", root / "project.toml", CliOverrides())
    path = root / "schemas" / "outcome-timeout.json"
    schema = json.loads(path.read_text(encoding="utf-8"))
    schema["description"] = "x" * 60_000
    path.write_text(json.dumps(schema), encoding="utf-8")
    cfg = load(root / "config.toml", root / "project.toml", CliOverrides())
    variants = cfg.sequence_generation.counterfactual_sets[0].variants
    timeout = next(item for item in variants if item.name == "confirmation_timeout")
    assert len(timeout.outcome_schema["description"]) == 60_000


def test_instruction_budget_prices_resolved_state_schema_not_only_its_path(tmp_path):
    root = _mutated_config(
        tmp_path,
        "context_window = 131072",
        "context_window = 90000",
    )
    load(root / "config.toml", root / "project-instruction-only.toml", CliOverrides())
    path = root / "schemas" / "state.json"
    schema = json.loads(path.read_text(encoding="utf-8"))
    schema["description"] = "x" * 50_000
    path.write_text(json.dumps(schema), encoding="utf-8")
    errors = _named_errors(root, "project-instruction-only.toml")
    _has(errors, "complete sequence prompt and Schema do not fit")


def test_sequence_budget_takes_family_max_instead_of_summing_exclusive_slots(tmp_path):
    root = _mutated_named_project(
        tmp_path,
        "project-instruction-only.toml",
        "hidden_sentinel 不变。\n\"\"\"\nstate_schema_path",
        f"hidden_sentinel 不变。{'x' * 16_000}\n\"\"\"\nstate_schema_path",
    )
    project = root / "project-instruction-only.toml"
    text = project.read_text(encoding="utf-8")
    second = f'''[[generate.instruction_only]]
name = "open_booking_second"
sequence_class = "ticket_booking"
count = 1
len_range = [3, 4]
instruction = """{'y' * 16_000}"""
state_schema_path = "schemas/state.json"

'''
    text = text.replace("[generate.timeline]\n", second + "[generate.timeline]\n")
    project.write_text(text, encoding="utf-8")
    config = root / "config.toml"
    config.write_text(
        config.read_text(encoding="utf-8").replace(
            "context_window = 131072", "context_window = 100000", 1),
        encoding="utf-8",
    )
    cfg = load(config, project, CliOverrides())
    assert cfg.sequence_generation is not None
    assert tuple(item.name for item in cfg.sequence_generation.instruction_only) == (
        "open_booking", "open_booking_second",
    )


def test_sequence_budget_matches_structured_schema_transport(tmp_path):
    """同一 PromptBundle 仅在 structured profile 上计入实际 active Schema。"""
    root = _mutated_config(
        tmp_path, "context_window = 131072", "context_window = 90000",
    )
    load(root / "config.toml", root / "project-instruction-only.toml", CliOverrides())
    config = root / "config.toml"
    text = config.read_text(encoding="utf-8")
    text = text.replace("supports_structured_output = false",
                        "supports_structured_output = true")
    config.write_text(text, encoding="utf-8")
    _has(_named_errors(root, "project-instruction-only.toml"),
         "complete sequence prompt and Schema do not fit")


def _append_repair_profile(root: Path) -> None:
    """为预算测试追加不支持 structured output 的独立小 repair profile。"""
    config = root / "config.toml"
    text = config.read_text(encoding="utf-8")
    text += '''
[llm.repair]
provider = "anthropic"
base_url = "https://api.deepseek.com/anthropic"
model = "deepseek-v4-flash"
api_key_env = "LABELKIT_DEEPSEEK_KEY"
max_concurrency = 1
max_retries = 0
supports_structured_output = false
supports_vision = false
max_output_tokens = 8192
thinking = "disabled"
context_window = 10000
temperature = 0.0
'''
    config.write_text(text, encoding="utf-8")


def test_event_plan_repair_replay_is_budgeted_only_when_enabled(tmp_path):
    """EventPlan L3 计完整四消息重放；关闭 repair 后不引用该调用包络。"""
    root = _mutated_project(
        tmp_path / "enabled", '[output]\n', '[output]\nrepair_llm = "repair"\n',
    )
    _append_repair_profile(root)
    _has(_errors(root), "complete sequence prompt and Schema do not fit")

    root = _mutated_project(
        tmp_path / "disabled", '[output]\n',
        '[output]\nmax_repair_attempts = 0\nrepair_llm = "repair"\n',
    )
    _append_repair_profile(root)
    cfg = load(root / "config.toml", root / "project.toml", CliOverrides())
    assert cfg.output.max_repair_attempts == 0


def test_sequence_attempt_default_and_state_hook_default(tmp_path):
    root = _mutated_project(
        tmp_path,
        'max_slot_attempts = 8\nstate_validator = "hooks.py:validate_state"\n',
        "",
    )
    generation = load(root / "config.toml", root / "project.toml", CliOverrides()).sequence_generation
    assert generation is not None
    assert generation.max_slot_attempts == 3
    assert generation.state_validator is None


@pytest.mark.parametrize("attempts", (0, 21, True))
def test_sequence_attempt_limit_is_closed(tmp_path, attempts):
    value = "true" if attempts is True else str(attempts)
    root = _mutated_project(
        tmp_path, "max_slot_attempts = 8", f"max_slot_attempts = {value}")
    _has(_errors(root), "expected integer in 1..20")


@pytest.mark.parametrize("key_value", (
    'llms = ["default"]',
    "styles = []",
    'seed_examples = ["seed"]',
    "standalone_count = 1",
    "num_per_record = 1",
    "seeds_per_call = 1",
    "num_per_call = 1",
))
def test_sequence_rejects_every_flat_generation_key(tmp_path, key_value):
    root = _mutated_project(
        tmp_path, "max_slot_attempts = 8\n",
        f"max_slot_attempts = 8\n{key_value}\n")
    _has(_errors(root), "flat key is forbidden in sequence form")


def test_flat_form_rejects_sequence_keys(tmp_path):
    root = _mutated_named_project(
        tmp_path, "project-instruction-only.toml",
        'form = "sequence"', 'form = "flat"')
    errors = _named_errors(root, "project-instruction-only.toml")
    _has(errors, "sequence key is forbidden in flat form")


@pytest.mark.parametrize(("old", "new", "expected"), (
    ('semantic_llm = "default"', 'semantic_llm = "ghost"', "does not exist"),
    ('evaluation_llm = "judge"', 'evaluation_llm = "ghost"', "does not exist"),
))
def test_sequence_profiles_must_resolve(tmp_path, old, new, expected):
    root = _mutated_project(tmp_path, old, new)
    _has(_errors(root), expected)


def test_each_sequence_profile_requires_declared_context_window(tmp_path):
    root = _mutated_config(
        tmp_path, "context_window = 131072", "context_window = 0")
    _has(_errors(root), "profile must declare context_window > 0")


def test_declared_mode_rejects_instruction_only_table(tmp_path):
    root = _mutated_project(
        tmp_path, '[generate.timeline]\n',
        '[[generate.instruction_only]]\n'
        'name = "illegal"\nsequence_class = "ticket_booking"\ncount = 1\n'
        'len_range = [2, 2]\ninstruction = "illegal second owner"\n'
        'state_schema_path = "schemas/state.json"\n\n[generate.timeline]\n')
    errors = _errors(root)
    _has(errors, "instruction_only: forbidden in declared mode")


@pytest.mark.parametrize(("old", "new", "expected"), (
    ('sequence_class = "ticket_booking"', 'sequence_class = "ghost"',
     "unknown sequence class"),
    ('frame_class = "task_request"', 'frame_class = "ghost"',
     "unknown frame class"),
    ('calendar_window = "service_hours"', 'calendar_window = "ghost"',
     "unknown calendar window"),
    ('pattern = "booking_success"', 'pattern = "ghost"',
     "unknown pattern"),
))
def test_declared_references_must_resolve(tmp_path, old, new, expected):
    root = _mutated_project(tmp_path, old, new)
    _has(_errors(root), expected)


def test_catalog_actors_must_match_pattern_actor_set(tmp_path):
    root = _mutated_project(
        tmp_path, 'observers = ["requester", "system"]',
        'observers = ["requester", "system", "auditor"]')
    _has(_errors(root), "catalog actors must exactly match the class actor set")


def test_catalog_must_cover_declared_slots_without_replacement(tmp_path):
    root = _mutated_project(tmp_path, "count = 2", "count = 14")
    _has(_errors(root), "13 rows but 14 slots require rows")


@pytest.mark.parametrize(("source", "path_line", "expected"), (
    ("llm", 'initial_state_catalog_path = "catalogs/ticket-booking.jsonl"',
     "forbidden for llm source"),
    ("catalog", "", "required for catalog source"),
))
def test_class_state_source_and_catalog_path_are_exact(tmp_path, source, path_line, expected):
    root = _mutated_project(
        tmp_path,
        'initial_state_source = "catalog"\n'
        'initial_state_catalog_path = "catalogs/ticket-booking.jsonl"',
        f'initial_state_source = "{source}"\n{path_line}',
    )
    _has(_errors(root), expected)


@pytest.mark.parametrize(("schema_name", "expected"), (
    ("state.json", 'top-level type must be "object"'),
    ("frame-request.json", 'top-level type must be "object"'),
    ("outcome-positive.json", 'top-level type must be "object"'),
))
def test_generation_schema_surfaces_require_object_roots(tmp_path, schema_name, expected):
    root = tmp_path / "sequence-generation"
    shutil.copytree(EXAMPLE_ROOT, root)
    (root / "schemas" / schema_name).write_text('{"type":"array"}', encoding="utf-8")
    _has(_errors(root), expected)


def test_generation_schema_rejects_unresolved_local_reference(tmp_path):
    root = tmp_path / "sequence-generation"
    shutil.copytree(EXAMPLE_ROOT, root)
    (root / "schemas" / "frame-request.json").write_text(
        '{"type":"object","properties":{"x":{"$ref":"#/$defs/missing"}}}',
        encoding="utf-8")
    _has(_errors(root), "unresolvable reference")


@pytest.mark.parametrize(("old", "new", "expected"), (
    ('state_schema_path = "schemas/state.json"',
     'state_schema_path = "schemas/missing.json"', "cannot read schema file"),
    ('schema_path = "schemas/frame-request.json"',
     '# schema path intentionally absent', "exactly one of schema_path or schema_inline"),
    ('outcome_schema_path = "schemas/outcome-positive.json"',
     'outcome_schema_inline = "{}"', "only outcome_schema_path is supported"),
))
def test_generation_schema_source_contracts(tmp_path, old, new, expected):
    root = _mutated_project(tmp_path, old, new)
    _has(_errors(root), expected)


def test_catalog_rejects_invalid_json_and_invalid_scenario_shell(tmp_path):
    root = tmp_path / "sequence-generation"
    shutil.copytree(EXAMPLE_ROOT, root)
    catalog = root / "catalogs" / "ticket-booking.jsonl"
    rows = catalog.read_text(encoding="utf-8").splitlines()
    catalog.write_text("not-json\n" + rows[1] + "\n", encoding="utf-8")
    _has(_errors(root), "invalid JSON")

    root = tmp_path / "bad-shell" / "sequence-generation"
    shutil.copytree(EXAMPLE_ROOT, root)
    catalog = root / "catalogs" / "ticket-booking.jsonl"
    rows = [json.loads(line) for line in catalog.read_text(encoding="utf-8").splitlines()]
    rows[0].pop("time_context")
    catalog.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")
    _has(_errors(root), "invalid ScenarioSeed catalog row")


def test_payload_binding_rejects_duplicate_and_ancestor_paths(tmp_path):
    marker = (
        '  { payload_path = "/status", state_phase = "after", '
        'state_path = "/request/status" }\n]')
    replacement = (
        '  { payload_path = "/status", state_phase = "after", '
        'state_path = "/request/status" },\n'
        '  { payload_path = "/status/detail", state_phase = "after", '
        'state_path = "/request/status" }\n]')
    root = _mutated_project(tmp_path, marker, replacement)
    _has(_errors(root), "conflicting payload paths")


@pytest.mark.parametrize(("old", "new", "expected"), (
    ('read_roots = ["/public", "/goal", "/request", "/actors/requester"]',
     'read_roots = ["not-a-pointer"]', "expected RFC 6901 pointer"),
    ('write_roots = ["/request/status"]',
     'write_roots = ["not-a-pointer"]', "expected RFC 6901 pointer"),
    ('publish_roots = ["/request/id", "/request/status", "/goal/origin", '
     '"/goal/destination", "/goal/travel_date"]',
     'publish_roots = ["not-a-pointer"]', "expected RFC 6901 pointer"),
    ('payload_path = "/request_id"', 'payload_path = "not-a-pointer"',
     "expected RFC 6901 pointer"),
    ('state_path = "/request/id"', 'state_path = "not-a-pointer"',
     "expected RFC 6901 pointer"),
    ('payload_path = "/request_id"', 'payload_path = ""',
     "expected non-root RFC 6901 pointer"),
))
def test_every_pointer_surface_rejects_invalid_syntax(tmp_path, old, new, expected):
    root = _mutated_project(tmp_path, old, new)
    _has(_errors(root), expected)


@pytest.mark.parametrize(("old", "new"), (
    ('read_roots = ["/public", "/goal", "/request", "/actors/requester"]',
     'read_roots = ["/request", "/request/id"]'),
    ('write_roots = ["/request/status"]',
     'write_roots = ["/request", "/request/status"]'),
    ('publish_roots = ["/request/id", "/request/status", "/goal/origin", '
     '"/goal/destination", "/goal/travel_date"]',
     'publish_roots = ["/request", "/request/id"]'),
))
def test_each_permission_root_list_rejects_ancestor_overlap(tmp_path, old, new):
    root = _mutated_project(tmp_path, old, new)
    _has(_errors(root), "redundant pointer roots")


@pytest.mark.parametrize(("old", "new"), (
    ('state_path = "/request/id"', 'state_path = "/hidden_sentinel"'),
    ('state_phase = "after"', 'state_phase = "future"'),
))
def test_payload_binding_enforces_permission_and_phase(tmp_path, old, new):
    root = _mutated_project(tmp_path, old, new)
    errors = _errors(root)
    assert any(text in error for text in (
        "must be covered by read_roots and publish_roots",
        "expected before | after",
    ) for error in errors)


@pytest.mark.parametrize("key_value", (
    "primary_sessions = 1",
    "crossed_primary_sessions = 0",
))
def test_timeline_rejects_deleted_session_count_keys(tmp_path, key_value):
    root = _mutated_project(
        tmp_path,
        "[generate.timeline]\n",
        f"[generate.timeline]\n{key_value}\n",
    )
    _has(_errors(root), "generation_config_invalid: deleted timeline key")


@pytest.mark.parametrize(("old", "new", "expected"), (
    ('sequence_class = "ticket_booking"', 'sequence_class = "ghost"',
     "unknown sequence class"),
    ("len_range = [3, 4]", "len_range = [0, 4]",
     "expected 1 <= low <= high <= 8"),
    ("len_range = [3, 4]", "len_range = [3, 9]",
     "expected 1 <= low <= high <= 8"),
    ("count = 1", "count = 0", "expected integer >= 1"),
))
def test_instruction_only_fields_are_strict(tmp_path, old, new, expected):
    root = _mutated_named_project(
        tmp_path, "project-instruction-only.toml", old, new)
    _has(_named_errors(root, "project-instruction-only.toml"), expected)


def test_instruction_only_forbids_catalog_class_source(tmp_path):
    root = _mutated_named_project(
        tmp_path, "project-instruction-only.toml",
        '[class.ticket_booking.annotate]',
        '[class.ticket_booking.generate]\n'
        'state_schema_path = "schemas/state.json"\n'
        'initial_state_source = "catalog"\n'
        'initial_state_catalog_path = "catalogs/ticket-booking.jsonl"\n'
        'instruction = "catalog world"\n\n'
        '[class.ticket_booking.annotate]',
    )
    _has(_named_errors(root, "project-instruction-only.toml"),
         "catalog source is forbidden in instruction_only mode")


def test_instruction_only_requires_nonempty_instruction(tmp_path):
    old = (
        'instruction = """\n'
        '生成一次完整、自然、状态连续的中文订票交互。参与者先提出请求，系统再确认接收并给出结果；\n'
        '所有事件必须围绕同一个 request_id。patch 先 test 当前可见的精确叶子值，再只 replace 完成本事件所需的\n'
        '最少叶子 path；不得替换 actors、audit、goal、request、ticket、sla 等容器，不得改变任何字段类型。\n'
        '请求事件只把 request.status 从 new 改为 pending；受理事件只把 acknowledged 改为 true 并把 status 改为\n'
        'acknowledged；结果事件只把 request.status、ticket.id 与 ticket.status 改为一致终态。始终保持\n'
        'hidden_sentinel 不变。\n'
        '"""\n'
    )
    root = _mutated_named_project(
        tmp_path, "project-instruction-only.toml", old, 'instruction = " "\n')
    _has(_named_errors(root, "project-instruction-only.toml"), "expected non-empty string")


@pytest.mark.parametrize(("definition", "name", "expected"), (
    ("NOT_CALLABLE = 1", "NOT_CALLABLE", "is not callable"),
    ("def bad_arity(left, right):\n    return []", "bad_arity", "must accept exactly 1 positional"),
    ("def bad_return(value):\n    return 'bad'", "bad_return", "returned an invalid value"),
    ("def explodes(value):\n    raise RuntimeError('boom')", "explodes",
     "synthetic dry-run raised RuntimeError"),
    ("_calls = 0\ndef changes(value):\n    global _calls\n    _calls += 1\n"
     "    return [] if _calls == 1 else ['changed']", "changes",
     "synthetic dry-run is nondeterministic"),
))
def test_state_hook_startup_probe_fails_closed(tmp_path, definition, name, expected):
    root = tmp_path / "sequence-generation"
    shutil.copytree(EXAMPLE_ROOT, root)
    hooks = root / "hooks.py"
    hooks.write_text(hooks.read_text(encoding="utf-8") + f"\n\n{definition}\n", encoding="utf-8")
    project = root / "project.toml"
    project.write_text(
        project.read_text(encoding="utf-8").replace(
            'state_validator = "hooks.py:validate_state"',
            f'state_validator = "hooks.py:{name}"'),
        encoding="utf-8",
    )
    _has(_errors(root), expected)


def test_state_hook_rejects_deleted_module_form_and_missing_attribute(tmp_path):
    root = _mutated_project(
        tmp_path, 'state_validator = "hooks.py:validate_state"',
        'state_validator = "hooks:validate_state"')
    _has(_errors(root), "form is deleted")

    root = _mutated_project(
        tmp_path / "missing", 'state_validator = "hooks.py:validate_state"',
        'state_validator = "hooks.py:missing"')
    _has(_errors(root), "not found in hook file")


@pytest.mark.parametrize(("old", "new", "expected"), (
    ("max_span_s = 2400", "# max_span_s intentionally absent",
     "expected numeric seconds"),
    ("max_gap_s = 120", "# max_gap_s intentionally absent",
     "expected numeric seconds"),
    ("min_gap_s = 30", "min_gap_s = 1201", "min_gap_s must be <= max_gap_s"),
    ("max_span_s = 2400", "max_span_s = 10",
     "adjacent minimum gaps exceed max_span_s"),
    ('order = ["request", "acknowledge", "confirm"]',
     'order = ["request", "confirm"]', "exact permutation"),
    ('after = "acknowledge"', 'after = "request"', "gap must point forward"),
))
def test_pattern_order_gap_and_span_constraints(tmp_path, old, new, expected):
    root = _mutated_project(tmp_path, old, new)
    _has(_errors(root), expected)


def test_every_adjacent_role_pair_requires_a_gap(tmp_path):
    old = (
        '[[generate.pattern.booking_success.gaps]]\n'
        'name = "request_to_acknowledge"\n'
        'before = "request"\n'
        'after = "acknowledge"\n'
        'min_gap_s = 5\n'
        'max_gap_s = 120\n\n'
    )
    root = _mutated_project(tmp_path, old, "")
    _has(_errors(root), "every adjacent role pair requires exactly one gap")


@pytest.mark.parametrize(("old", "new", "expected"), (
    ('name = "positive"\nkind = "positive"',
     'name = "positive"\nkind = "positive"\ntarget_role = "request"',
     "forbidden variant target keys"),
    ('name = "missing_acknowledgement"\nkind = "missing"\ntarget_role = "acknowledge"',
     'name = "missing_acknowledgement"\nkind = "missing"',
     "missing required variant target keys"),
    ('target_role = "acknowledge"', 'target_role = "ghost"',
     "target role 'ghost' does not exist"),
    ('target_before = "acknowledge"\ntarget_after = "confirm"',
     'target_before = "request"\ntarget_after = "confirm"',
     "reordered targets must be adjacent"),
    ('target_gap = "acknowledge_to_confirm"', 'target_gap = "ghost"',
     "target gap 'ghost' does not exist"),
    ("min_excess_s = 1\nmax_excess_s = 600",
     "min_excess_s = 601\nmax_excess_s = 600",
     "min_excess_s must be <= max_excess_s"),
))
def test_variant_kind_target_contracts(tmp_path, old, new, expected):
    root = _mutated_project(tmp_path, old, new)
    _has(_errors(root), expected)


def test_missing_target_frame_must_be_unique(tmp_path):
    root = _mutated_project(
        tmp_path, 'frame_class = "task_request"',
        'frame_class = "acknowledgement"')
    _has(_errors(root), "missing target frame class must be unique")


def test_missing_variant_cannot_remove_the_only_pattern_role(tmp_path):
    """公开 loader 聚合拒绝会产生空序列的单 role missing 声明。"""
    root = tmp_path / "sequence-generation"
    shutil.copytree(EXAMPLE_ROOT, root)
    project = root / "project.toml"
    text = project.read_text(encoding="utf-8")
    pattern_start = text.index("[generate.pattern.booking_success]")
    role_start = text.index("[[generate.pattern.booking_success.roles]]", pattern_start)
    second_role = text.index("[[generate.pattern.booking_success.roles]]", role_start + 1)
    role = text[role_start:second_role]
    replacement = (
        '[generate.pattern.booking_success]\nsequence_class = "ticket_booking"\n'
        'description = "单事件请求。"\norder = ["request"]\nmax_span_s = 2400\n\n'
        + role
        + '[[generate.counterfactual_sets]]\nname = "single_missing"\n'
        'pattern = "booking_success"\ncount = 1\n\n'
        '[[generate.counterfactual_sets.variants]]\nname = "missing_request"\n'
        'kind = "missing"\ntarget_role = "request"\n'
        'outcome_schema_path = "schemas/outcome-missing.json"\n\n'
    )
    timeline_start = text.index("[generate.timeline]", pattern_start)
    text = text[:pattern_start] + replacement + text[timeline_start:]
    text = text.replace("duplicate_sequences = 1", "duplicate_sequences = 0", 1)
    project.write_text(text, encoding="utf-8")
    _has(_errors(root), "missing variant must retain at least one role")


def test_reordered_targets_must_use_distinct_frame_classes(tmp_path):
    root = _mutated_project(
        tmp_path, 'name = "confirm"\nframe_class = "confirmation"',
        'name = "confirm"\nframe_class = "acknowledgement"')
    _has(_errors(root), "reordered target roles must have different frame classes")


def test_variant_expected_violation_signatures_must_be_unique(tmp_path):
    marker = (
        '[[generate.counterfactual_sets.variants]]\n'
        'name = "confirmation_before_acknowledgement"'
    )
    duplicate = (
        '[[generate.counterfactual_sets.variants]]\n'
        'name = "missing_again"\nkind = "missing"\n'
        'target_role = "acknowledge"\n'
        'outcome_schema_path = "schemas/outcome-missing.json"\n\n'
        '[[generate.counterfactual_sets.variants]]\n'
        'name = "confirmation_before_acknowledgement"'
    )
    root = _mutated_project(tmp_path, marker, duplicate)
    _has(_errors(root), "expected violation signatures must be unique")


def test_sequence_rejects_cli_limit_and_non_inline_output(tmp_path):
    root = _mutated_project(tmp_path, 'meta_mode = "inline"', 'meta_mode = "sidecar"')
    with pytest.raises(ConfigError) as captured:
        load(root / "config.toml", root / "project.toml", CliOverrides(limit=1))
    _has(captured.value.errors, 'sequence form must be "inline"')
    _has(captured.value.errors, "sequence form must be absent")


@pytest.mark.parametrize(("old", "new", "expected"), (
    ('mode = "generate_only"', 'mode = "process"', 'must be "generate_only"'),
    ('modality = "text"', 'modality = "ui"', 'must be "text"'),
    ("enabled = true\nform = \"sequence\"", "enabled = false\nform = \"sequence\"",
     "sequence form must be true"),
    ('[classify]\nenabled = false', '[classify]\nenabled = true',
     "sequence form must be false"),
    ('[frame.classify]\nenabled = false', '[frame.classify]\nenabled = true',
     "sequence form must be false"),
    ('[dedup]\nenabled = true', '[dedup]\nenabled = false',
     "sequence form must be true"),
    ('scope = "global"', 'scope = "batch"', 'must be "global"'),
    ('rejects = "none"', 'rejects = "refs"', 'must be "none"'),
))
def test_sequence_runtime_combination_gates(tmp_path, old, new, expected):
    root = _mutated_project(tmp_path, old, new)
    _has(_errors(root), expected)


def test_sequence_quality_accepts_only_explicit_pointwise_threshold(tmp_path):
    root = _mutated_project(
        tmp_path, '[quality]\nenabled = false',
        '[quality]\nenabled = true\nmode = "pointwise"\nthreshold = 0.5')
    cfg = load(root / "config.toml", root / "project.toml", CliOverrides())
    assert cfg.quality.mode == "pointwise" and cfg.quality.threshold == 0.5

    root = _mutated_project(
        tmp_path / "pairwise", '[quality]\nenabled = false',
        '[quality]\nenabled = true')
    _has(_errors(root), "sequence quality requires pointwise with an explicit fixed threshold")

    root = _mutated_project(
        tmp_path / "ratio", '[quality]\nenabled = false',
        '[quality]\nenabled = true\nmode = "pointwise"\n'
        'selection = "top_ratio"\ntop_ratio = 0.5')
    _has(_errors(root), "sequence quality requires pointwise with an explicit fixed threshold")


@pytest.mark.parametrize(("old", "new", "expected"), (
    ('timestamp_start = "2026-01-05T09:00:00+08:00"',
     'timestamp_start = "2026-01-05T09:00:00"', "must include a fixed UTC offset"),
    ('timestamp_start = "2026-01-05T09:00:00+08:00"',
     'timestamp_start = 123', "expected ISO-8601 string with offset"),
    ('timestamp_start = "2026-01-05T09:00:00+08:00"',
     'timestamp_start = "not-a-date"', "expected ISO-8601 string with offset"),
    ("event_gap_s = [5, 60]", "event_gap_s = [61, 60]",
     "range lower bound must be <= upper bound"),
    ("event_gap_s = [5, 60]", "event_gap_s = [5]", "expected two-number array"),
    ("session_max_events = 8", "session_max_events = 0", "expected integer >= 1"),
    ("session_max_span_s = 3600", "session_max_span_s = 0", "expected seconds > 0"),
    ("session_gap_s = 3600", "session_gap_s = 0", "expected seconds > 0"),
    ("duplicate_sequences = 1", "duplicate_sequences = 3",
     "not enough positive primary sources for replay"),
))
def test_timeline_constraints_are_fail_fast(tmp_path, old, new, expected):
    root = _mutated_project(tmp_path, old, new)
    _has(_errors(root), expected)


def test_runtime_interleaving_loads_through_public_loader():
    cfg = _load_example("project-runtime-four-slot.toml")
    generation = cfg.sequence_generation
    assert generation is not None
    assert tuple(
        (item.name, item.interleaving_candidate_set)
        for item in generation.counterfactual_sets
    ) == (
        ("runtime_trigger", "runtime_trigger"),
        ("runtime_partner", "runtime_partner"),
    )
    interleaving = generation.interleaving
    assert interleaving is not None and interleaving.no_interleaving_weight == 0
    assert tuple(
        (
            item.name,
            item.trigger_candidate_set,
            item.partner_candidate_set,
            item.trigger_weight,
        )
        for item in interleaving.patterns
    ) == (("runtime_pair", "runtime_trigger", "runtime_partner", 1),)
    assert not hasattr(generation.timeline, "primary_sessions")
    assert not hasattr(generation.timeline, "crossed_primary_sessions")


def test_interleaving_requires_candidate_labels_and_section_together(tmp_path):
    section = '''[generate.interleaving]
no_interleaving_weight = 0

[generate.interleaving.pattern.runtime_pair]
trigger_candidate_set = "runtime_trigger"
partner_candidate_set = "runtime_partner"
trigger_weight = 1

'''
    root = _mutated_named_project(
        tmp_path / "missing-section",
        "project-runtime-four-slot.toml",
        section,
        "",
    )
    _has(
        _named_errors(root, "project-runtime-four-slot.toml"),
        "required when interleaving_candidate_set is declared",
    )

    root = _copied_example(tmp_path / "missing-labels")
    project = root / "project-runtime-four-slot.toml"
    text = project.read_text(encoding="utf-8").replace(
        'interleaving_candidate_set = "runtime_trigger"\n', "", 1,
    ).replace(
        'interleaving_candidate_set = "runtime_partner"\n', "", 1,
    )
    project.write_text(text, encoding="utf-8")
    _has(
        _named_errors(root, "project-runtime-four-slot.toml"),
        "required when generate.interleaving is declared",
    )


@pytest.mark.parametrize(("old", "new"), (
    ('interleaving_candidate_set = "runtime_trigger"',
     'interleaving_candidate_set = "runtime-trigger"'),
    ('interleaving_candidate_set = "runtime_trigger"',
     'interleaving_candidate_set = ["runtime_trigger"]'),
    ('[generate.interleaving.pattern.runtime_pair]',
     '[generate.interleaving.pattern.runtime-pair]'),
))
def test_interleaving_names_are_short_exact_identifiers(tmp_path, old, new):
    root = _mutated_named_project(
        tmp_path,
        "project-runtime-four-slot.toml",
        old,
        new,
    )
    _has(_named_errors(root, "project-runtime-four-slot.toml"), "expected name matching")


@pytest.mark.parametrize(("old", "new"), (
    ('no_interleaving_weight = 0', 'no_interleaving_weight = -1'),
    ('no_interleaving_weight = 0', 'no_interleaving_weight = false'),
    ('no_interleaving_weight = 0', 'no_interleaving_weight = 0.5'),
    ('no_interleaving_weight = 0', 'no_interleaving_weight = 9223372036854775808'),
    ('trigger_weight = 1', 'trigger_weight = 0'),
    ('trigger_weight = 1', 'trigger_weight = true'),
    ('trigger_weight = 1', 'trigger_weight = 1.5'),
    ('trigger_weight = 1', 'trigger_weight = 9223372036854775808'),
))
def test_interleaving_weights_require_exact_toml_int64(tmp_path, old, new):
    root = _mutated_named_project(
        tmp_path,
        "project-runtime-four-slot.toml",
        old,
        new,
    )
    _has(_named_errors(root, "project-runtime-four-slot.toml"), "expected TOML int64")


def test_interleaving_weight_int64_boundary_and_total(tmp_path):
    maximum = 9_223_372_036_854_775_807
    root = _mutated_named_project(
        tmp_path / "exact",
        "project-runtime-four-slot.toml",
        "trigger_weight = 1",
        f"trigger_weight = {maximum}",
    )
    cfg = load(root / "config.toml", root / "project-runtime-four-slot.toml", CliOverrides())
    assert cfg.sequence_generation.interleaving.patterns[0].trigger_weight == maximum

    root = _mutated_named_project(
        tmp_path / "overflow",
        "project-runtime-four-slot.toml",
        "no_interleaving_weight = 0",
        f"no_interleaving_weight = {maximum}",
    )
    _has(
        _named_errors(root, "project-runtime-four-slot.toml"),
        "opportunity weight total exceeds TOML int64",
    )


@pytest.mark.parametrize(("old", "replacement"), (
    ('kind = "positive"', 'kind = "missing"\ntarget_role = "request"'),
    (
        'kind = "positive"\noutcome_schema_path = "schemas/outcome-positive.json"',
        'kind = "positive"\noutcome_schema_path = "schemas/outcome-positive.json"\n\n'
        '[[generate.counterfactual_sets.variants]]\nname = "positive_second"\n'
        'kind = "positive"\noutcome_schema_path = "schemas/outcome-positive.json"',
    ),
))
def test_interleaving_candidate_requires_unique_positive_branch(tmp_path, old, replacement):
    root = _mutated_named_project(
        tmp_path,
        "project-runtime-four-slot.toml",
        old,
        replacement,
    )
    _has(
        _named_errors(root, "project-runtime-four-slot.toml"),
        "interleaving candidate set requires exactly one positive variant",
    )


def test_interleaving_references_must_resolve_and_cover_candidates(tmp_path):
    root = _mutated_named_project(
        tmp_path / "unknown",
        "project-runtime-four-slot.toml",
        'trigger_candidate_set = "runtime_trigger"',
        'trigger_candidate_set = "ghost"',
    )
    _has(
        _named_errors(root, "project-runtime-four-slot.toml"),
        "unknown or empty interleaving candidate set",
    )

    root = _mutated_named_project(
        tmp_path / "unreferenced",
        "project-runtime-four-slot.toml",
        'interleaving_candidate_set = "runtime_trigger"',
        'interleaving_candidate_set = "orphan"',
    )
    _has(
        _named_errors(root, "project-runtime-four-slot.toml"),
        "declared candidate set is not referenced",
    )


def test_interleaving_candidate_roles_are_disjoint(tmp_path):
    root = _mutated_named_project(
        tmp_path / "same-pattern",
        "project-runtime-four-slot.toml",
        'partner_candidate_set = "runtime_partner"',
        'partner_candidate_set = "runtime_trigger"',
    )
    _has(
        _named_errors(root, "project-runtime-four-slot.toml"),
        "trigger_candidate_set must differ from partner_candidate_set",
    )

    second = '''trigger_weight = 1

[generate.interleaving.pattern.reverse_pair]
trigger_candidate_set = "runtime_partner"
partner_candidate_set = "runtime_trigger"
trigger_weight = 1'''
    root = _mutated_named_project(
        tmp_path / "role-conflict",
        "project-runtime-four-slot.toml",
        "trigger_weight = 1",
        second,
    )
    _has(
        _named_errors(root, "project-runtime-four-slot.toml"),
        "candidate sets cannot serve both trigger and partner roles",
    )


def test_interleaving_allows_patterns_to_share_candidate_pools(tmp_path):
    second = '''trigger_weight = 1

[generate.interleaving.pattern.runtime_pair_second]
trigger_candidate_set = "runtime_trigger"
partner_candidate_set = "runtime_partner"
trigger_weight = 2'''
    root = _mutated_named_project(
        tmp_path,
        "project-runtime-four-slot.toml",
        "trigger_weight = 1",
        second,
    )
    cfg = load(root / "config.toml", root / "project-runtime-four-slot.toml", CliOverrides())
    assert tuple(item.name for item in cfg.sequence_generation.interleaving.patterns) == (
        "runtime_pair",
        "runtime_pair_second",
    )


@pytest.mark.parametrize(("old", "new", "expected"), (
    ('[generate.interleaving]\n', '[generate.interleaving]\nkind = "owner_word"\n',
     "unknown or deleted sequence key"),
    ('trigger_weight = 1', 'trigger_weight = 1\nowner_word = "ABBA"',
     "unknown or deleted sequence key"),
))
def test_interleaving_rejects_extra_configuration_layers(tmp_path, old, new, expected):
    root = _mutated_named_project(
        tmp_path,
        "project-runtime-four-slot.toml",
        old,
        new,
    )
    _has(_named_errors(root, "project-runtime-four-slot.toml"), expected)


def test_interleaving_requires_a_named_pattern(tmp_path):
    block = '''[generate.interleaving.pattern.runtime_pair]
trigger_candidate_set = "runtime_trigger"
partner_candidate_set = "runtime_partner"
trigger_weight = 1

'''
    root = _mutated_named_project(
        tmp_path,
        "project-runtime-four-slot.toml",
        block,
        "",
    )
    _has(
        _named_errors(root, "project-runtime-four-slot.toml"),
        "at least one named pattern is required",
    )

    root = _mutated_named_project(
        tmp_path / "invalid-row",
        "project-runtime-four-slot.toml",
        block,
        "pattern = { broken = 1 }\n\n",
    )
    _has(_named_errors(root, "project-runtime-four-slot.toml"), "expected table")


def test_instruction_only_and_flat_forbid_interleaving(tmp_path):
    section = '''[generate.interleaving]
no_interleaving_weight = 0

[generate.interleaving.pattern.illegal]
trigger_candidate_set = "left"
partner_candidate_set = "right"
trigger_weight = 1

'''
    root = _mutated_named_project(
        tmp_path / "instruction",
        "project-instruction-only.toml",
        "[generate.timeline]\n",
        section + "[generate.timeline]\n",
    )
    _has(
        _named_errors(root, "project-instruction-only.toml"),
        "forbidden in instruction_only mode",
    )

    root = _mutated_named_project(
        tmp_path / "flat",
        "project-instruction-only.toml",
        'form = "sequence"',
        'form = "flat"',
    )
    project = root / "project-instruction-only.toml"
    text = project.read_text(encoding="utf-8").replace(
        "[generate.timeline]\n",
        section + "[generate.timeline]\n",
        1,
    )
    project.write_text(text, encoding="utf-8")
    _has(
        _named_errors(root, "project-instruction-only.toml"),
        "[generate].interleaving: generation_config_invalid: sequence key is forbidden",
    )


def test_instruction_only_rejects_replay_count(tmp_path):
    root = _mutated_named_project(
        tmp_path,
        "project-instruction-only.toml",
        "duplicate_sequences = 0",
        "duplicate_sequences = 1",
    )
    _has(
        _named_errors(root, "project-instruction-only.toml"),
        "instruction_only requires zero duplicate sequences",
    )


@pytest.mark.parametrize(("old", "new", "expected"), (
    ('utc_offset = "+08:00"', 'utc_offset = "UTC+8"', "expected fixed UTC offset"),
    ('days = ["mon", "tue", "wed", "thu", "fri"]', 'days = ["mon", "mon"]',
     "expected non-empty unique weekday names"),
    ('days = ["mon", "tue", "wed", "thu", "fri"]', 'days = ["monday"]',
     "expected non-empty unique weekday names"),
    ('intervals = [["08:00:00", "12:00:00"], ["13:00:00", "18:00:00"]]',
     'intervals = []', "expected non-empty interval array"),
    ('intervals = [["08:00:00", "12:00:00"], ["13:00:00", "18:00:00"]]',
     'intervals = [["08:00:00", "14:00:00"], ["13:00:00", "18:00:00"]]',
     "calendar intervals must not overlap"),
    ('intervals = [["08:00:00", "12:00:00"], ["13:00:00", "18:00:00"]]',
     'intervals = [["12:00:00", "08:00:00"]]',
     "interval must be non-empty and same-day"),
))
def test_calendar_window_constraints(tmp_path, old, new, expected):
    root = _mutated_project(tmp_path, old, new)
    _has(_errors(root), expected)


def test_noise_table_and_count_are_biconditional(tmp_path):
    root = _mutated_project(tmp_path, "noise_events = 2", "noise_events = 0")
    _has(_errors(root), "noise table must be present iff noise_events > 0")

    noise_table = (
        '[generate.noise]\n'
        'frame_class = "noise"\n'
        'instruction = "生成与任何任务无关、没有可执行诉求的一句自然闲聊。"\n'
        'topics = ["夜空中的月相观察", "手工面包出炉时的香气"]\n'
    )
    root = _mutated_project(tmp_path / "missing", noise_table, "")
    _has(_errors(root), "noise table must be present iff noise_events > 0")


def test_noise_frame_is_dedicated_and_resolved(tmp_path):
    root = _mutated_project(
        tmp_path, 'frame_class = "noise"\ninstruction = "生成与任何任务无关',
        'frame_class = "task_request"\ninstruction = "生成与任何任务无关')
    _has(_errors(root), "noise frame class cannot be used by a role")

    root = _mutated_project(
        tmp_path / "unknown", 'frame_class = "noise"\ninstruction = "生成与任何任务无关',
        'frame_class = "ghost"\ninstruction = "生成与任何任务无关')
    _has(_errors(root), "unknown noise frame class")


@pytest.mark.parametrize(("topics", "expected"), (
    ('["夜空中的月相观察"]', "topic count must equal"),
    ('["夜空中的月相观察", "手工面包出炉时的香气", "街角树叶的颜色"]',
     "topic count must equal"),
    ('["夜空中的月相观察", "夜空中的月相观察"]', "non-empty unique"),
    ('["夜空中的月相观察", ""]', "non-empty unique"),
))
def test_noise_topics_are_explicit_unique_and_exact(topics, expected, tmp_path):
    """noise ordinal 与显式唯一话题一一绑定。"""
    original = '["夜空中的月相观察", "手工面包出炉时的香气"]'
    root = _mutated_project(tmp_path, original, topics)
    _has(_errors(root), expected)


def test_context_budget_enumerates_each_declared_noise_topic(monkeypatch):
    """启动期预算必须为每个 ordinal 分别构造 renderer 与 evaluator case。"""
    captured = []

    def capture(_state, _config, cases):
        captured.extend(cases)

    from labelkit.common.config import _generation_budget

    monkeypatch.setattr(
        _generation_budget, "check_generation_context_budget", capture,
    )
    cfg = _load_example()
    topics = cfg.sequence_generation.noise.topics
    frame_schema = cfg.frame_class_views[cfg.sequence_generation.noise.frame_class].gen_schema
    from labelkit.common.inference.schema_engine import noise_semantic_evaluation_schema

    expected = (
        (
            cfg.sequence_generation.semantic_profile,
            "独立噪声事件渲染器",
            frame_schema,
            (),
            False,
        ),
        (
            cfg.sequence_generation.evaluation_profile,
            "独立噪声语义判定器",
            noise_semantic_evaluation_schema(),
            (cfg.sequence_generation.limits.rendered_payload_bytes,),
            False,
        ),
    )
    for topic in topics:
        matched = []
        for case in captured:
            text = "".join(
                part.text or "" for message in case.prompt.messages for part in message.parts
            )
            if topic in text:
                system = case.prompt.messages[0].parts[0].text
                family = next(name for name in (expected[0][1], expected[1][1]) if name in system)
                matched.append((
                    case.profile, family, case.schema,
                    case.dynamic_byte_limits, case.post_validated,
                ))
        assert tuple(matched) == expected


@pytest.mark.parametrize(("old", "new", "expected"), (
    ('description = "与订票任务无关且不包含可执行诉求的自然闲聊"',
     'description = ""', "expected non-empty string"),
    ('instruction = "生成一句与任何订票或设备任务无关、没有可执行诉求的自然闲聊。"',
     'instruction = ""', "expected non-empty string"),
    ('schema_path = "schemas/frame-noise.json"',
     '# noise schema intentionally absent', "exactly one of schema_path or schema_inline"),
))
def test_referenced_frame_requires_full_generation_contract(tmp_path, old, new, expected):
    root = _mutated_project(tmp_path, old, new)
    _has(_errors(root), expected)


def test_sequence_unknown_nested_key_is_rejected(tmp_path):
    root = _mutated_project(
        tmp_path, '[generate.timeline]\n',
        '[generate.timeline]\nunknown_owner = true\n')
    _has(_errors(root), "unknown or deleted sequence key")


@pytest.mark.parametrize("deleted_line", (
    "stream = {}",
    "tiers = []",
    "tier_rank = 1",
    "subsequence = true",
    "filler = {}",
    "time_fields = {}",
    'brief_schema = "old.json"',
    'realize_schema = "old.json"',
    'sequence_validator = "hooks.py:validate_state"',
    'scenario_validator = "hooks.py:validate_state"',
))
def test_every_deleted_top_level_sequence_key_is_targeted_error(tmp_path, deleted_line):
    root = _mutated_project(
        tmp_path, "max_slot_attempts = 8\n",
        f"max_slot_attempts = 8\n{deleted_line}\n")
    _has(_errors(root), "generation_config_invalid: deleted sequence key")


@pytest.mark.parametrize(("old", "new"), (
    ('[class.ticket_booking.generate]\ninstruction = """',
     '[class.ticket_booking.generate]\ntiers = []\ninstruction = """'),
    ('[frame.class.noise.generate]\ninstruction = ',
     '[frame.class.noise.generate]\ntime_fields = {}\ninstruction = '),
))
def test_deleted_nested_sequence_keys_are_targeted_errors(tmp_path, old, new):
    root = _mutated_project(tmp_path, old, new)
    _has(_errors(root), "deleted")


def test_instruction_byte_limit_accepts_exact_boundary_and_rejects_one_more(tmp_path):
    original = "生成与任何任务无关、没有可执行诉求的一句自然闲聊。"
    root = _mutated_project(tmp_path, original, "a" * 32768)
    cfg = load(root / "config.toml", root / "project.toml", CliOverrides())
    assert cfg.sequence_generation.noise.instruction == "a" * 32768

    root = _mutated_project(tmp_path / "over", original, "a" * 32769)
    _has(_errors(root), "generation prompt text exceeds 32768 UTF-8 bytes")


def test_generation_schema_file_byte_limit_is_closed(tmp_path):
    base = '{"type":"object","examples":[{}]}'
    root = tmp_path / "exact" / "sequence-generation"
    shutil.copytree(EXAMPLE_ROOT, root)
    schema = root / "schemas" / "frame-noise.json"
    schema.write_text(base + " " * (65536 - len(base)), encoding="utf-8")
    cfg = load(root / "config.toml", root / "project.toml", CliOverrides())
    assert cfg.frame_class_views["noise"].gen_schema == {
        "type": "object", "examples": ({},),
    }

    root = tmp_path / "over" / "sequence-generation"
    shutil.copytree(EXAMPLE_ROOT, root)
    schema = root / "schemas" / "frame-noise.json"
    schema.write_text(base + " " * (65537 - len(base)), encoding="utf-8")
    _has(_errors(root), "schema file exceeds 65536 bytes")


def _catalog_with_canonical_size(root: Path, size: int) -> None:
    """把第一行 catalog 扩到指定 canonical UTF-8 byte 数。"""
    path = root / "catalogs" / "ticket-booking.jsonl"
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    rows[0]["style"] = {"padding": ""}
    def compact(value):
        return json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        )
    current = len(compact(rows[0]).encode("utf-8"))
    assert current <= size
    rows[0]["style"]["padding"] = "a" * (size - current)
    assert len(compact(rows[0]).encode("utf-8")) == size
    path.write_text("\n".join(compact(row) for row in rows) + "\n", encoding="utf-8")


def test_scenario_seed_catalog_byte_limit_is_closed(tmp_path):
    root = tmp_path / "exact" / "sequence-generation"
    shutil.copytree(EXAMPLE_ROOT, root)
    _catalog_with_canonical_size(root, 65536)
    cfg = load(root / "config.toml", root / "project.toml", CliOverrides())
    catalog = cfg.class_views["ticket_booking"].sequence_generation.initial_state_catalog
    assert len(catalog) == 13

    root = tmp_path / "over" / "sequence-generation"
    shutil.copytree(EXAMPLE_ROOT, root)
    _catalog_with_canonical_size(root, 65537)
    _has(_errors(root), "invalid ScenarioSeed catalog row")


def test_record_unit_limit_accepts_exact_boundary_and_rejects_one_more(tmp_path):
    root = _mutated_named_project(
        tmp_path, "project-instruction-only.toml",
        "count = 1", "count = 250000")
    project = root / "project-instruction-only.toml"
    project.write_text(
        project.read_text(encoding="utf-8")
        .replace("len_range = [3, 4]", "len_range = [1, 1]"),
        encoding="utf-8",
    )
    cfg = load(root / "config.toml", project, CliOverrides())
    assert cfg.sequence_generation.instruction_only[0].count == 250000

    project.write_text(
        project.read_text(encoding="utf-8")
        .replace("count = 250000", "count = 250001"),
        encoding="utf-8",
    )
    _has(_named_errors(root, "project-instruction-only.toml"),
         "derived record_units must be in 1..500000")


def test_record_unit_limit_uses_canonical_instruction_length(tmp_path):
    """M1 与 planner 都按 len_range 下界冻结精确 instruction 事件数。"""
    root = _mutated_named_project(
        tmp_path, "project-instruction-only.toml",
        "count = 1", "count = 100000")
    project = root / "project-instruction-only.toml"
    project.write_text(
        project.read_text(encoding="utf-8")
        .replace("len_range = [3, 4]", "len_range = [4, 5]"),
        encoding="utf-8",
    )
    cfg = load(root / "config.toml", project, CliOverrides())
    source = cfg.sequence_generation.instruction_only[0]
    assert source.count * (1 + source.len_range[0]) == 500_000


def test_stream_row_limit_rejects_derived_overflow(tmp_path):
    root = _mutated_named_project(
        tmp_path, "project-instruction-only.toml",
        "count = 1", "count = 62501")
    project = root / "project-instruction-only.toml"
    project.write_text(
        project.read_text(encoding="utf-8")
        .replace("len_range = [3, 4]", "len_range = [8, 8]"),
        encoding="utf-8",
    )
    _has(_named_errors(root, "project-instruction-only.toml"),
         "derived stream_rows must be in 1..500000")


def test_pattern_role_and_variant_count_limits(tmp_path):
    role_rows = "".join(
        '[[generate.pattern.booking_success.roles]]\n'
        f'name = "extra_{index}"\n'
        'frame_class = "task_request"\nactor = "requester"\n'
        'read_roots = ["/request"]\nwrite_roots = ["/request"]\n'
        'publish_roots = ["/request/id"]\nobservers = ["requester", "system"]\n'
        'state_instruction = "keep state"\npayload_bindings = []\n\n'
        for index in range(30)
    )
    root = _mutated_project(
        tmp_path, '[[generate.pattern.booking_success.gaps]]\n',
        role_rows + '[[generate.pattern.booking_success.gaps]]\n')
    _has(_errors(root), "expected 1..32 roles")

    variants = "".join(
        '[[generate.counterfactual_sets.variants]]\n'
        f'name = "extra_{index}"\nkind = "positive"\n'
        'outcome_schema_path = "schemas/outcome-positive.json"\n\n'
        for index in range(5)
    )
    marker = '[[generate.counterfactual_sets.variants]]\nname = "positive"'
    root = _mutated_project(
        tmp_path / "variants", marker, variants + marker)
    _has(_errors(root), "expected 1..8 variants")
