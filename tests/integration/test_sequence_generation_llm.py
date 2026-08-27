"""v1.19 序列生成在真实 DeepSeek Anthropic 端点上的验收测试。"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
from dataclasses import FrozenInstanceError
from pathlib import Path
from typing import Mapping

import jsonpatch
import pytest
from jsonschema import Draft202012Validator

from labelkit.cli.parser import CliOverrides
from labelkit.common.config import load
from labelkit.common.contracts.generation import EventExecution, PostValidationResult
from labelkit.common.observability.obslog import MetricsSink
from labelkit.common.inference.schema_engine import SchemaEngine
from labelkit.operators.dedup import DedupIndex
from labelkit.common.errors import GenerationProjectionMismatch
from labelkit.operators.generation.planner import compile_scenario_plan
from labelkit.operators.generation.program import compile_generation_program
from labelkit.operators.generation.project import canonical_delivery_row, canonical_json
from labelkit.operators.generation.state import resolve_planned_events
from labelkit.orchestration.sequence_workflow import estimate_sequence_products
from labelkit.orchestration.application import execute_run

from tests.conftest import (
    DEEPSEEK_BASE_URL,
    DEEPSEEK_KEY_ENV,
    DEEPSEEK_MODEL,
)


pytestmark = [pytest.mark.integration, pytest.mark.deepseek]

_ROOT = Path(__file__).resolve().parents[2]
_EXAMPLE = _ROOT / "examples" / "sequence-generation"
_VARIANTS = {
    "positive",
    "missing_acknowledgement",
    "confirmation_before_acknowledgement",
    "confirmation_timeout",
}


def _copy_project(
    tmp_path: Path,
    project_name: str,
    *,
    one_set: bool,
    keep_noise: bool = False,
) -> tuple[Path, Path, Path]:
    """复制真实教学工程，并把交付规模收敛到当前验收需要。"""
    project_root = tmp_path / "sequence-generation"
    shutil.copytree(_EXAMPLE, project_root)
    project_path = project_root / project_name
    text = project_path.read_text(encoding="utf-8")
    if one_set:
        text = text.replace("count = 2", "count = 1", 1)
        text = text.replace("primary_sessions = 8", "primary_sessions = 4", 1)
        if not keep_noise:
            text = text.replace("noise_events = 2", "noise_events = 0", 1)
        text = text.replace("duplicate_sequences = 1", "duplicate_sequences = 0", 1)
        if not keep_noise:
            noise_block = (
                '\n[generate.noise]\nframe_class = "noise"\n'
                'instruction = "生成与任何任务无关、没有可执行诉求的一句自然闲聊。"\n'
                'topics = ["夜空中的月相观察", "手工面包出炉时的香气"]\n'
            )
            text = text.replace(noise_block, "\n", 1)
    text += '\n[trace]\nenabled = true\nchannels = ["schema", "llm"]\ncontent = "refs"\n'
    project_path.write_text(text, encoding="utf-8")
    return project_root, project_root / "config.toml", project_path


def _load_json(path: str | Path) -> dict:
    """读取一个 UTF-8 JSON 对象。"""
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _load_jsonl(path: str | Path) -> list[dict]:
    """读取一个 UTF-8 JSONL 文件。"""
    return [json.loads(line) for line in Path(path).read_text(encoding="utf-8").splitlines()]


def _project_paths(cfg) -> tuple[Path, ...]:
    """返回本次成功与失败通道的全部可检查路径。"""
    paths = cfg.paths
    values = (
        paths.output,
        paths.stream,
        paths.report,
        paths.trace,
        paths.manifest,
        paths.failed_report,
    )
    return tuple(Path(value) for value in values if value is not None)


def _assert_api_key_absent(secret: str, stderr: str, paths: tuple[Path, ...]) -> None:
    """用固定失败文本检查凭据未进入任何捕获工件。"""
    if secret and secret in stderr:
        pytest.fail("API key leaked into captured stderr", pytrace=False)
    for path in paths:
        if path.exists() and secret and secret in path.read_text(encoding="utf-8"):
            pytest.fail("API key leaked into an integration artifact", pytrace=False)


def _assert_hidden_absent(value: object, message: str, sentinel: str) -> None:
    """用固定失败文本检查 hidden sentinel 未越过指定边界。"""
    if sentinel and sentinel in canonical_json(value):
        pytest.fail(message, pytrace=False)


def _install_body_observer(monkeypatch):
    """装饰生产 Anthropic 序列化器并只记录无内容布尔证据。"""
    from labelkit.common.inference import llm_client

    original = llm_client._build_anthropic_body
    observations: list[dict[str, bool]] = []

    def wrapped(profile, prompt, response_schema):
        body = original(profile, prompt, response_schema)
        if profile.base_url.rstrip("/") == DEEPSEEK_BASE_URL:
            observations.append({
                "model": profile.model == DEEPSEEK_MODEL,
                "structured_off": profile.supports_structured_output is False,
                "thinking": body.get("thinking") == {"type": "disabled"},
                "no_tools": "tools" not in body and "tool_choice" not in body,
            })
        return body

    monkeypatch.setattr(llm_client, "_build_anthropic_body", wrapped)
    return observations


def _assert_deepseek_bodies(observations: list[dict[str, bool]]) -> None:
    """断言每次真实 DeepSeek 派发都保持冻结 body 形状。"""
    assert observations, "DeepSeek request serializer was not exercised"
    assert all(all(item.values()) for item in observations), "DeepSeek request body drifted"


def _install_trace_observer(monkeypatch):
    """捕获内存 EventTrace，并检查 planner/renderer 的 hidden 边界。"""
    from labelkit.operators.generation import scenario

    original_generate = scenario._generate_validated_slot_traces
    original_request = scenario._build_validated_event_plan_request
    original_render = scenario.render_event
    attempts: list[tuple[int, tuple]] = []
    programs: list[object] = []
    planner_leaks: list[bool] = []
    renderer_leaks: list[bool] = []
    sentinels: set[str] = set()

    async def generate(program, plan, slot, attempt_index, services):
        traces = await original_generate(program, plan, slot, attempt_index, services)
        attempts.append((attempt_index, tuple(traces)))
        programs.append(program)
        return traces

    def request(context, attempt_index, variation_nonce):
        value = original_request(context, attempt_index, variation_nonce)
        sentinel = str(context.scenario_seed.initial_state.get("hidden_sentinel", ""))
        if sentinel:
            sentinels.add(sentinel)
        planner_leaks.append(bool(sentinel and sentinel in canonical_json(value)))
        return value

    async def render(value, services):
        rendered = canonical_json(value)
        renderer_leaks.append(any(sentinel in rendered for sentinel in sentinels))
        return await original_render(value, services)

    monkeypatch.setattr(scenario, "_generate_validated_slot_traces", generate)
    monkeypatch.setattr(scenario, "_build_validated_event_plan_request", request)
    monkeypatch.setattr(scenario, "render_event", render)
    return attempts, programs, planner_leaks, renderer_leaks


def _run(config_path: Path, project_path: Path):
    """经真实 CLI 运行装配面执行一次 sequence run。"""
    overrides = CliOverrides(console="plain")
    cfg = load(config_path, project_path, overrides)
    exit_code = execute_run(config_path, project_path, overrides)
    return cfg, exit_code


def _state_schema(program, trace):
    """从冻结程序选择当前 trace 的完整状态 Schema。"""
    if program.mode == "instruction_only":
        source = next(item for item in program.instruction_only if item.name == "open_booking")
        return source.state_schema
    return program.class_views[trace.sequence_class].sequence_generation.state_schema


def _outcome_schema(program, trace):
    """从冻结声明选择当前分支的 outcome Schema。"""
    if program.mode == "instruction_only":
        return _state_schema(program, trace)
    source = next(item for item in program.counterfactual_sets if item.pattern == trace.pattern_name)
    return next(item.outcome_schema for item in source.variants if item.name == trace.variant_name)


def _hash_state(state: object) -> str:
    """计算生产状态哈希的同口径摘要。"""
    return hashlib.sha256(canonical_json(state).encode("utf-8")).hexdigest()


def _assert_trace_replays(program, trace) -> None:
    """从 initial_state 重放全部 patch，并逐步检查状态与 outcome。"""
    schema = _state_schema(program, trace)
    validator = Draft202012Validator(schema)
    state = json.loads(canonical_json(trace.scenario_seed.initial_state))
    assert next(validator.iter_errors(state), None) is None
    for event in trace.events:
        assert _hash_state(state) == event.state_before_hash
        state = jsonpatch.apply_patch(state, json.loads(canonical_json(event.patch)), in_place=False)
        assert _hash_state(state) == event.state_after_hash
        assert next(validator.iter_errors(state), None) is None
    assert canonical_json(state) == canonical_json(trace.final_state)
    assert next(Draft202012Validator(_outcome_schema(program, trace)).iter_errors(state), None) is None


def _protected_value(event) -> str:
    """投影 causal closure 要求逐字节相同的字段。"""
    return canonical_json({
        "event_key": event.event_key,
        "role": event.role,
        "frame_class": event.frame_class,
        "actor": event.actor,
        "logical_time_us": event.logical_time_us,
        "actor_view": event.actor_view,
        "intent": event.intent,
        "patch": event.patch,
        "state_before_hash": event.state_before_hash,
        "state_after_hash": event.state_after_hash,
        "publish_snapshot": event.publish_snapshot,
        "payload": event.payload,
    })


def _assert_declared_truth(program, traces) -> None:
    """检查四分支身份、违规与 protected prefix 精确耦合。"""
    by_name = {trace.variant_name: trace for trace in traces}
    assert set(by_name) == _VARIANTS
    assert len({trace.scenario_id for trace in traces}) == 1
    assert len({trace.world_branch_id for trace in traces}) == 4
    assert len({trace.scenario_seed.initial_state["request"]["id"] for trace in traces}) == 1
    source = program.counterfactual_sets[0]
    baseline = by_name["positive"]
    for variant in source.variants:
        trace = by_name[variant.name]
        expected = () if not variant.expected_violation else (dict(variant.expected_violation),)
        actual = tuple(dict(item) for item in trace.pattern_evaluation.actual_violations)
        assert actual == expected
        count = next(
            (index for index, event in enumerate(baseline.events)
             if event.role == variant.divergence_role),
            len(baseline.events),
        )
        assert all(
            _protected_value(left) == _protected_value(right)
            for left, right in zip(baseline.events[:count], trace.events[:count], strict=True)
        )


def _assert_report_usage(report: Mapping[str, object]) -> None:
    """检查全部活跃 profile 都有真实调用与 token。"""
    usage = report["llm_usage"]
    assert set(usage) == {"default", "judge"}
    for entry in usage.values():
        assert entry["calls"] > 0
        assert entry["prompt_tokens"] > 0
        assert entry["completion_tokens"] > 0


def _assert_runtime_report(report: Mapping[str, object]) -> None:
    """检查真实序列交付写出冻结顺序与九字段 runtime 节点。"""
    assert list(report) == [
        "run", "counts", "schema_engine", "generate", "runtime", "trace",
        "llm_usage", "timing",
    ]
    runtime = report["runtime"]
    assert isinstance(runtime, Mapping)
    assert list(runtime) == [
        "queue_high_water", "running_high_water", "resource_wait_high_water",
        "commit_waiting_high_water", "candidate_bytes_high_water", "cancelled_tasks",
        "resource_wait_ms", "http_pool_wait_ms", "commit_ms",
    ]
    assert all(type(value) is int and value >= 0 for value in runtime.values())
    assert runtime["commit_waiting_high_water"] >= 1
    assert runtime["candidate_bytes_high_water"] >= 1


def _assert_main_stream_round_trip(main: list[dict], stream: list[dict]) -> None:
    """检查 main 成员与 primary stream 的双向 owner/id 对账。"""
    primary = [row for row in stream if row["_meta"]["event"].get("owner_sequence_id")]
    owners = {row["_meta"]["id"]: row for row in main}
    assert set(owners) == {row["_meta"]["event"]["owner_sequence_id"] for row in primary}
    for owner_id, row in owners.items():
        expected = tuple(row["_meta"]["stream"]["member_ids"])
        actual = tuple(
            item["_meta"]["event"]["event_id"]
            for item in primary
            if item["_meta"]["event"]["owner_sequence_id"] == owner_id
        )
        assert actual == expected


def _delivery_digest(main: list[dict], stream: list[dict]) -> str:
    """按冻结 framing 独立重算成功交付摘要。"""
    digest = hashlib.sha256(b"labelkit:v1.20:delivery\n")
    for row in (*main, *stream):
        payload = canonical_delivery_row(row)
        digest.update(str(len(payload)).encode("ascii"))
        digest.update(b":")
        digest.update(payload)
    return digest.hexdigest()


def _assert_manifest(cfg, main: list[dict], stream: list[dict], report: dict) -> None:
    """独立对账 manifest、正式文件与 report 中的交付身份。"""
    _assert_runtime_report(report)
    manifest = _load_json(cfg.paths.manifest)
    sequence = report["generate"]["sequence"]
    assert manifest["schema_version"] == 1
    assert manifest["artifacts_committed"] is True
    assert manifest["run_id"] == sequence["run_id"]
    assert manifest["delivery_digest"] == sequence["delivery_digest"]
    assert manifest["delivery_digest"] == _delivery_digest(main, stream)
    expected = {
        "main": (Path(cfg.paths.output), len(main)),
        "stream": (Path(cfg.paths.stream), len(stream)),
        "report": (Path(cfg.paths.report), None),
    }
    for name, (path, rows) in expected.items():
        artifact = manifest[name]
        assert Path(artifact["path"]).resolve() == path.resolve()
        assert artifact["sha256"] == hashlib.sha256(path.read_bytes()).hexdigest()
        if rows is not None:
            assert artifact["rows"] == rows


def _assert_semantic_pass(trace) -> None:
    """检查六项独立语义 verdict 全部通过。"""
    value = trace.semantic_evaluation
    assert value.causal_consistency is True
    assert value.actor_knowledge is True
    assert value.goal_consistency is True
    assert value.temporal_plausibility is True
    assert value.cross_frame_consistency is True
    assert value.realism is True
    assert value.reason_codes == ()


def _install_noise_observer(monkeypatch):
    """装饰真实 noise 协作者并记录计划话题与最终四门判定。"""
    from labelkit.operators.generation import evaluate, render

    original_render = render.render_noise
    original_evaluate = evaluate.evaluate_noise
    render_topics: list[str] = []
    evaluations: list[tuple[str, object]] = []

    async def wrapped_render(request, services):
        render_topics.append(request.noise_slot.topic)
        return await original_render(request, services)

    async def wrapped_evaluate(request, services):
        result = await original_evaluate(request, services)
        evaluations.append((request.planned_topic, result))
        return result

    monkeypatch.setattr(render, "render_noise", wrapped_render)
    monkeypatch.setattr(evaluate, "evaluate_noise", wrapped_evaluate)
    return render_topics, evaluations


def test_declared_four_variants_real_deepseek(monkeypatch, tmp_path, capsys):
    """一个 catalog slot 真实交付四个 declared variant。"""
    _root, config_path, project_path = _copy_project(tmp_path, "project.toml", one_set=True)
    bodies = _install_body_observer(monkeypatch)
    attempts, programs, planner_leaks, renderer_leaks = _install_trace_observer(monkeypatch)

    cfg, exit_code = _run(config_path, project_path)
    captured = capsys.readouterr()
    main = _load_jsonl(cfg.paths.output)
    stream = _load_jsonl(cfg.paths.stream)
    report = _load_json(cfg.paths.report)
    traces = attempts[-1][1]
    program = programs[-1]

    assert exit_code == 0
    assert attempts
    assert len(main) == 4
    _assert_declared_truth(program, traces)
    for trace in traces:
        _assert_trace_replays(program, trace)
        _assert_semantic_pass(trace)
        sentinel = str(trace.scenario_seed.initial_state["hidden_sentinel"])
        for event in trace.events:
            _assert_hidden_absent(event.payload, "Hidden sentinel leaked into frame payload", sentinel)
    assert planner_leaks, "EventPlanRequest observer was not exercised"
    assert renderer_leaks, "RenderEventRequest observer was not exercised"
    assert not any(planner_leaks), "Hidden sentinel reached an EventPlanRequest"
    assert not any(renderer_leaks), "Hidden sentinel reached a RenderEventRequest"
    sequence = report["generate"]["sequence"]
    assert sequence["planned_sets"] == sequence["delivered_sets"] == 1
    assert sequence["planned_sequences"] == sequence["delivered_sequences"] == 4
    assert all(
        counts == {"planned": 1, "delivered": 1}
        for counts in sequence["by_pattern"]["booking_success"].values()
    )
    _assert_report_usage(report)
    _assert_main_stream_round_trip(main, stream)
    _assert_manifest(cfg, main, stream, report)
    _assert_deepseek_bodies(bodies)
    _assert_api_key_absent(os.environ[DEEPSEEK_KEY_ENV], captured.err, _project_paths(cfg))


def test_declared_two_planned_noise_topics_real_deepseek(monkeypatch, tmp_path, capsys):
    """两个显式 noise 话题经真实渲染和独立四门评估后精确交付。"""
    _root, config_path, project_path = _copy_project(
        tmp_path, "project.toml", one_set=True, keep_noise=True
    )
    bodies = _install_body_observer(monkeypatch)
    render_topics, evaluations = _install_noise_observer(monkeypatch)

    cfg, exit_code = _run(config_path, project_path)
    captured = capsys.readouterr()
    main = _load_jsonl(cfg.paths.output)
    stream = _load_jsonl(cfg.paths.stream)
    report = _load_json(cfg.paths.report)
    expected = set(cfg.sequence_generation.noise.topics)
    noise = [row for row in stream if row["_meta"]["event"].get("owner_sequence_id") is None]

    assert exit_code == 0 and len(main) == 4 and len(noise) == 2
    assert set(render_topics) == expected
    assert {topic for topic, _value in evaluations} == expected
    for topic in expected:
        accepted = [value for observed, value in evaluations if observed == topic]
        assert any(
            value.unrelated_to_declared_tasks
            and value.no_executable_task
            and value.realism
            and value.matches_planned_topic
            and value.reason_codes == ()
            for value in accepted
        )
    sequence = report["generate"]["sequence"]
    assert sequence["noise_events"] == 2
    assert sequence["noise_slot_attempts"] >= 2
    _assert_report_usage(report)
    _assert_main_stream_round_trip(main, stream)
    _assert_manifest(cfg, main, stream, report)
    _assert_deepseek_bodies(bodies)
    _assert_api_key_absent(os.environ[DEEPSEEK_KEY_ENV], captured.err, _project_paths(cfg))


def test_instruction_only_real_deepseek(monkeypatch, tmp_path, capsys):
    """instruction-only 真实生成非空序列且不声明 pattern truth。"""
    _root, config_path, project_path = _copy_project(
        tmp_path, "project-instruction-only.toml", one_set=False
    )
    bodies = _install_body_observer(monkeypatch)
    attempts, programs, _planner_leaks, _renderer_leaks = _install_trace_observer(monkeypatch)

    cfg, exit_code = _run(config_path, project_path)
    captured = capsys.readouterr()
    main = _load_jsonl(cfg.paths.output)
    stream = _load_jsonl(cfg.paths.stream)
    report = _load_json(cfg.paths.report)
    trace = attempts[-1][1][0]
    program = programs[-1]

    assert exit_code == 0 and len(main) == 1
    assert 3 <= len(trace.events) <= 4
    assert {event.frame_class for event in trace.events} <= set(program.frame_classes)
    assert trace.pattern_name is None and trace.variant_name is None
    assert trace.pattern_evaluation is None
    _assert_trace_replays(program, trace)
    _assert_semantic_pass(trace)
    truth = main[0]["_meta"]["generation"]
    assert truth["validation_mode"] == "instruction_only"
    assert truth["actor_knowledge_validation"] == "semantic"
    assert not ({"scenario_set", "pattern", "variant", "expected_violation"} & set(truth))
    sequence_calls = report["generate"]["sequence"]["sequence_calls"]
    assert sequence_calls["scenario_seed_calls"] > 0
    assert sequence_calls["baseline_event_plan_calls"] > 0
    assert sequence_calls["frame_render_calls"] > 0
    assert sequence_calls["semantic_evaluation_calls"] > 0
    _assert_report_usage(report)
    _assert_main_stream_round_trip(main, stream)
    _assert_manifest(cfg, main, stream, report)
    _assert_deepseek_bodies(bodies)
    _assert_api_key_absent(os.environ[DEEPSEEK_KEY_ENV], captured.err, _project_paths(cfg))


def test_cross_view_one_shot_retries_whole_real_attempt(monkeypatch, tmp_path, capsys):
    """CrossView 一次拒绝发生在完整 attempt 后，并只提交后续成功尝试。"""
    _root, config_path, project_path = _copy_project(tmp_path, "project.toml", one_set=True)
    bodies = _install_body_observer(monkeypatch)
    attempts, _programs, _planner, _renderer = _install_trace_observer(monkeypatch)
    from labelkit.operators.generation import project

    original_reconcile = project.reconcile_primary_candidate
    original_commit = DedupIndex.group_commit
    original_merge = MetricsSink.merge_counts
    from labelkit.operators.emitter import SequenceDeliveryEmitter

    original_assemble = SequenceDeliveryEmitter.assemble_sequence
    original_prepare = SequenceDeliveryEmitter.prepare_product
    injection_count = 0
    dedup_commits: list[int] = []
    dataset_merges: list[dict[str, int]] = []
    assembled_ids: dict[int, set[int]] = {}
    prepared_ids: set[int] = set()

    def reconcile(request):
        nonlocal injection_count
        original_reconcile(request)
        if len(request.sequences) == 4 and injection_count == 0:
            injection_count += 1
            raise GenerationProjectionMismatch("fixed integration rejection")

    def commit(index, token):
        original_commit(index, token)
        dedup_commits.append(len(index._group_exact))

    def merge(metrics, counters):
        original_merge(metrics, counters)
        dataset_merges.append(dict(counters))

    def assemble(emitter, request):
        rows = original_assemble(emitter, request)
        assembled_ids.setdefault(attempts[-1][0], set()).add(id(rows.main_row))
        return rows

    def prepare(emitter, main_rows, stream_rows, report):
        prepared_ids.update(id(row) for row in main_rows)
        return original_prepare(emitter, main_rows, stream_rows, report)

    monkeypatch.setattr(project, "reconcile_primary_candidate", reconcile)
    monkeypatch.setattr(DedupIndex, "group_commit", commit)
    monkeypatch.setattr(MetricsSink, "merge_counts", merge)
    monkeypatch.setattr(SequenceDeliveryEmitter, "assemble_sequence", assemble)
    monkeypatch.setattr(SequenceDeliveryEmitter, "prepare_product", prepare)

    cfg, exit_code = _run(config_path, project_path)
    captured = capsys.readouterr()
    main = _load_jsonl(cfg.paths.output)
    stream = _load_jsonl(cfg.paths.stream)
    report = _load_json(cfg.paths.report)
    sequence = report["generate"]["sequence"]
    assert exit_code == 0
    complete_attempts = tuple(attempt for attempt, _traces in attempts)
    assert len(complete_attempts) == 2
    assert complete_attempts[0] < complete_attempts[1]
    assert all({trace.variant_name for trace in traces} == _VARIANTS for _index, traces in attempts)
    assert injection_count == 1
    assert sequence["sequence_slot_attempts"] == complete_attempts[-1] + 1
    assert sequence["rejected_attempts"]["reconcile"] == 1
    assert sum(sequence["rejected_attempts"].values()) == sequence["sequence_slot_attempts"] - 1
    assert sequence["delivered_sets"] == 1 and sequence["delivered_sequences"] == 4
    assert dedup_commits == [4]
    assert len(dataset_merges) == 1
    assert report["counts"]["generated"] == report["counts"]["emitted"] == 4
    assert prepared_ids == assembled_ids[complete_attempts[-1]]
    rejected_ids = set().union(*(assembled_ids[index] for index in complete_attempts[:-1]))
    assert not (prepared_ids & rejected_ids)
    _assert_manifest(cfg, main, stream, report)
    program = compile_generation_program(cfg)
    plan = compile_scenario_plan(program)
    lower = estimate_sequence_products(cfg, program, plan)["sequence"]
    calls = sum(entry["calls"] for entry in report["llm_usage"].values())
    assert calls >= 2 * lower["successful_attempt_lower_bound"]
    _assert_deepseek_bodies(bodies)
    _assert_api_key_absent(os.environ[DEEPSEEK_KEY_ENV], captured.err, _project_paths(cfg))


def test_state_post_validator_real_l3_reuses_execution(monkeypatch, tmp_path, capsys):
    """一次 state violation 进入真实 L3，成功证明原实例被 renderer 消费。"""
    _root, config_path, project_path = _copy_project(
        tmp_path, "project-instruction-only.toml", one_set=False
    )
    bodies = _install_body_observer(monkeypatch)
    from labelkit.operators.generation import scenario

    original_post = scenario.post_validate_event_plan
    original_complete = SchemaEngine.complete_post_validated
    original_render = scenario._render_draft
    injected = 0
    injected_event_keys: list[str] = []
    resolved: dict[str, object] = {}
    consumed: dict[str, EventExecution] = {}

    def post(candidate, context):
        nonlocal injected
        result = original_post(candidate, context)
        if injected == 0 and not result.violations:
            injected += 1
            injected_event_keys.append(resolve_planned_events(context)[context.event_index].event_key)
            return PostValidationResult(("injected state transition violation",), None)
        return result

    async def complete(engine, request):
        result = await original_complete(engine, request)
        resolved[request.scope.record_ids[0]] = result
        return result

    async def render(context, planned, event_plan, execution, draft_render):
        consumed[planned.event_key] = execution
        return await original_render(context, planned, event_plan, execution, draft_render)

    monkeypatch.setattr(scenario, "post_validate_event_plan", post)
    monkeypatch.setattr(SchemaEngine, "complete_post_validated", complete)
    monkeypatch.setattr(scenario, "_render_draft", render)

    cfg, exit_code = _run(config_path, project_path)
    captured = capsys.readouterr()
    main = _load_jsonl(cfg.paths.output)
    stream = _load_jsonl(cfg.paths.stream)
    report = _load_json(cfg.paths.report)

    assert exit_code == 0 and injected == 1
    assert len(injected_event_keys) == 1
    event_key = injected_event_keys[0]
    result = resolved[event_key]
    assert result.resolved_at in {"l3_1", "l3_2"}
    assert result.event_execution is consumed[event_key]
    assert isinstance(result.event_execution, EventExecution)
    with pytest.raises(FrozenInstanceError):
        result.event_execution.state_before_hash = "changed"
    assert report["generate"]["sequence"]["delivered_sequences"] == 1
    _assert_manifest(cfg, main, stream, report)
    _assert_deepseek_bodies(bodies)
    _assert_api_key_absent(os.environ[DEEPSEEK_KEY_ENV], captured.err, _project_paths(cfg))
