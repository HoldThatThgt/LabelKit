"""segment.mode 重键的运行侧测试：52 处判断登记、类 2 与类 3 逐处、11 个工程迁移。

权威登记见 docs/dev/SPEC-image-region-segmentation.md §7 与 §19.6。这里的配置全部走 M1 或显式构造，
不涉 LLM、不涉网络；流模式的既有行为由各算子自己的测试与 8 个 dry-run golden 守护。
"""
from __future__ import annotations

import ast
import dataclasses
import json
import logging
import os
import re
import runpy
import subprocess
import sys
import tomllib
import types
from datetime import datetime
from pathlib import Path, PurePosixPath
from types import SimpleNamespace

import pytest

from labelkit.cli.console import ConsoleRenderer
from labelkit.cli.main import main
from labelkit.common.config.model import SegmentConfig
from labelkit.common.contracts.types import ImageRef, Record, RecordRef, UINode, UITree
from labelkit.common.errors import ConfigError
from labelkit.common.inference.credentials import referenced_profiles, resolve_credentials
from labelkit.operators.annotate import AnnotatePromptOptions, _assemble_frame_prompt, _assemble_prompt
from labelkit.operators.classify import _assemble_frame_classify
from labelkit.operators.emitter import Emitter
from labelkit.operators.extract import build_extract_prompt
from labelkit.operators.ingest import Ingestor
from labelkit.operators.quality import QualityStage
from labelkit.operators.segment import SegmentStage
from labelkit.operators.verify import VerifyStage
from labelkit.orchestration.factory import build_stages
from labelkit.orchestration.process_workflow import ProcessWorkflow, estimate_run
from tests.common.config.test_segment_mode_config import BOXES, IMAGE, SEQUENCE, Env, config_text
from tests.operators.test_ingest import _generation_stream_rows
from tests.orchestration.test_process_workflow import Probe, build, make_cfg, rec

ROOT = Path(__file__).resolve().parents[2]
EXAMPLES = ROOT / "examples"

IMAGE_SEGMENT = SegmentConfig(enabled=True, mode="image", instruction="生成候选区域")
SEQUENCE_SEGMENT = SegmentConfig(enabled=True, mode="sequence", sequence_resolved=True)

CLI = "labelkit/cli/console.py"
CON = "labelkit/common/config/_constraints.py"
CRED = "labelkit/common/inference/credentials.py"
ANN = "labelkit/operators/annotate.py"
CLS = "labelkit/operators/classify.py"
EMI = "labelkit/operators/emitter.py"
EXT = "labelkit/operators/extract.py"
ING = "labelkit/operators/ingest.py"
QUA = "labelkit/operators/quality.py"
VER = "labelkit/operators/verify.py"
FAC = "labelkit/orchestration/factory.py"
WF = "labelkit/orchestration/process_workflow.py"

SEQ, ENABLED, LLM_REF = "sequence_resolved", "enabled", "llm_referenced"

# SPEC §7.2 全部 52 行：(行号, 文件, 符号, 类, 该行必须读取的 SegmentConfig 属性)。
REGISTRY = (
    (1, CLI, "_freeze_header_facts", 2, ENABLED),
    (2, CLI, "_freeze_header_facts", 1, SEQ),
    (3, CLI, "_account_line", 1, SEQ),
    (4, CLI, "_print_estimate_notes", 1, SEQ),
    (5, CON, "_check_profile_refs", 3, LLM_REF),
    (6, CON, "_vision_users", 3, LLM_REF),
    (7, CON, "_vision_users", 3, ENABLED),
    (8, CON, "_collect_referenced", 3, LLM_REF),
    (9, CON, "_collect_referenced", 3, ENABLED),
    (10, CON, "_resolve_global_rubric", 1, SEQ),
    (11, CON, "_check_stage_switches", 2, ENABLED),
    (12, CON, "_check_stage_switches", 1, ENABLED),
    (13, CON, "_check_stage_switches", 1, ENABLED),
    (14, CON, "_check_stream_family", 1, SEQ),
    (15, CON, "_check_frame_switches", 1, ENABLED),
    (16, CON, "_check_frame_switches", 1, ENABLED),
    (17, CON, "_freeze_vision", 3, LLM_REF),
    (18, CON, "_warn_undeclared_windows", 3, ENABLED),
    (19, CON, "_warn_undeclared_windows", 3, SEQ),
    (20, CON, "_static_checks_prefix", 3, SEQ),
    (21, CRED, "referenced_profiles", 3, LLM_REF),
    (22, CRED, "referenced_profiles", 3, ENABLED),
    (23, ANN, "_assemble_prompt", 1, SEQ),
    (24, ANN, "_sequence_parts", 1, SEQ),
    (25, ANN, "_budgeted_call", 1, SEQ),
    (26, ANN, "_assemble_frame_prompt", 1, SEQ),
    (27, ANN, "_frame_call_prompt", 1, SEQ),
    (28, CLS, "_classify_prompt", 1, SEQ),
    (29, CLS, "_assemble_frame_classify", 1, SEQ),
    (30, CLS, "_assemble_frame_classify", 1, SEQ),
    (31, CLS, "_frame_prompt_fit", 1, SEQ),
    (32, EMI, "_rubric_selector", 1, SEQ),
    (33, EMI, "_stream_block", 1, SEQ),
    (34, EMI, "_verification_block", 1, SEQ),
    (35, EXT, "build_extract_prompt", 1, SEQ),
    (36, EXT, "_extract_transition_outcome", 1, SEQ),
    (37, ING, "scan", 1, SEQ),
    (38, QUA, "_pairwise_prompt", 1, SEQ),
    (39, VER, "run", 1, SEQ),
    (40, FAC, "build_stages", 2, ENABLED),
    (41, WF, "_estimate_scale", 1, SEQ),
    (42, WF, "_estimate_classify_calls", 1, SEQ),
    (43, WF, "_run_process", 1, SEQ),
    (44, WF, "_batch_end_payload", 1, SEQ),
    (45, WF, "_log_budget_startup", 1, SEQ),
    (46, WF, "_compose_chain", 2, ENABLED),
    (47, WF, "_build_report", 1, SEQ),
    (48, WF, "_report_counts", 1, SEQ),
    (49, WF, "_report_counts", 1, SEQ),
    (50, WF, "_report_budget", 1, SEQ),
    (51, WF, "_print_dry_notes", 1, SEQ),
    (52, WF, "_print_dry_notes", 1, SEQ),
)

# §19.6：`segment.enabled` 文本只允许留在这些符号内。_segment_image.py 持有开关本身的
# 图片模式判定与 rule 72 的冻结文本（§6.3）；其余是 §7.2 保留开关的行与 §7.4 的字节不变文本。
ALLOWED_RESIDUAL = {
    (CLI, "_freeze_header_facts"),
    ("labelkit/common/config/model.py", "StreamConfig"),
    ("labelkit/common/config/_segment_image.py", "image_mode"),
    ("labelkit/common/config/_segment_image.py", "check_segment_image"),
    (CON, "_vision_users"),
    (CON, "_collect_referenced"),
    (CON, "_check_stage_switches"),
    (CON, "_warn_segment_on"),
    (CON, "_warn_segment_off"),
    (CON, "_check_stream_family"),
    (CON, "_check_frame_switches"),
    (CON, "_freeze_vision"),
    (CON, "_warn_undeclared_windows"),
    (CRED, "referenced_profiles"),
    (FAC, "build_stages"),
    (WF, "_compose_chain"),
}

SEQUENCE_PROJECTS = (
    ("stream", "project.toml", "../config.toml"),
    ("stream", "project-text.toml", "../config.toml"),
    ("mix", "project.toml", "config.toml"),
    ("mix", "project-text.toml", "config.toml"),
    ("sequence-context-capacity", "project-minimum.toml", "config-minimum.toml"),
    ("sequence-context-capacity", "project-reactive.toml", "config-reactive.toml"),
    ("sequence-context-capacity", "project-static.toml", "config-static.toml"),
    ("sequence-context-capacity", "project-stitch.toml", "config-local-4b.toml"),
    ("sequence-context-capacity", "project-text.toml", "config-local-4b.toml"),
    ("sequence-context-capacity", "project-ui.toml", "config-local-4b.toml"),
    ("sequence-generation", "project-replay.toml", "config-local-4b.toml"),
)


@pytest.fixture
def env(tmp_path) -> Env:
    return Env(tmp_path)


def _modules() -> dict[str, tuple[str, ast.Module]]:
    """解析 labelkit/ 全部生产文件。"""
    parsed = {}
    for path in sorted((ROOT / "labelkit").rglob("*.py")):
        text = path.read_text(encoding="utf-8")
        parsed[path.relative_to(ROOT).as_posix()] = (text, ast.parse(text))
    return parsed


def _symbol_nodes(tree: ast.Module, symbol: str) -> list[ast.AST]:
    """按名字找函数或类；同名方法全部返回。"""
    return [node for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and node.name == symbol]


def _reads(node: ast.AST, attribute: str) -> bool:
    return any(isinstance(child, ast.Attribute) and child.attr == attribute for child in ast.walk(node))


def _innermost_symbol(tree: ast.Module, line: int) -> str | None:
    """返回覆盖该行的最内层函数或类名；模块级文本返回 None。"""
    best, span = None, None
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            if node.lineno <= line <= node.end_lineno and (span is None or node.end_lineno - node.lineno < span):
                best, span = node.name, node.end_lineno - node.lineno
    return best


def _ui_record(record_id: str = "a", *, kind: str = "single", members: tuple = ()) -> Record:
    """带完整树与（不读取的）图片引用的 UI 记录。"""
    tree = UITree(nodes=(UINode(node_id="0", parent_id=None, depth=0, role="View", text="登录", content_desc="",
                                bounds=(0, 0, 10, 10), visible=True, extra={}),))
    if kind == "sequence":
        return Record(id=record_id, modality="ui", text=None, raw=None, ui_tree=None, image=None,
                      ref=RecordRef("", None, None, ()), kind="sequence", members=members)
    return Record(id=record_id, modality="ui", text=None, raw=None, ui_tree=tree,
                  image=ImageRef(Path("image_0.png"), "png", 1), ref=RecordRef("image_0.png", None, 0, ()))


def _write_png(path: Path) -> None:
    from PIL import Image

    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (4, 4), "white").save(path)


# ── §7 登记 ────────────────────────────────────────────────────────────────


def test_registry_rows_match_spec_class_totals():
    assert [row[0] for row in REGISTRY] == list(range(1, 53))
    totals = {klass: sum(1 for row in REGISTRY if row[3] == klass) for klass in (1, 2, 3)}
    assert totals == {1: 37, 2: 4, 3: 11}


@pytest.mark.parametrize("row", REGISTRY, ids=lambda row: f"row{row[0]}")
def test_registry_row_reads_its_registered_predicate(row):
    _number, relative, symbol, _klass, attribute = row
    tree = ast.parse((ROOT / relative).read_text(encoding="utf-8"))
    nodes = _symbol_nodes(tree, symbol)
    assert nodes, f"{relative}: registered symbol {symbol} no longer exists"
    assert any(_reads(node, attribute) for node in nodes), f"{relative}:{symbol} does not read {attribute}"


def test_registry_matches_source_search():
    """§19.6：残留的 segment.enabled 只出现在登记允许的符号内。"""
    pattern = re.compile(r"segment\.enabled")
    found = []
    for relative, (text, tree) in _modules().items():
        for number, line in enumerate(text.splitlines(), start=1):
            if pattern.search(line):
                found.append((relative, _innermost_symbol(tree, number), number))
    stray = [entry for entry in found if entry[:2] not in ALLOWED_RESIDUAL]
    assert stray == []
    # 登记的类 2 判断确实还在：搜索不是对空集通过。
    assert (WF, "_compose_chain") in {entry[:2] for entry in found}
    assert (FAC, "build_stages") in {entry[:2] for entry in found}


def test_class1_predicates_false_in_image_mode(env):
    """类 1 的所有符号都不再读开关，M1 又让图片模式的 sequence_resolved 恒假。"""
    image = env.load(env.project(IMAGE)).segment
    assert image.enabled is True and image.mode == "image" and image.sequence_resolved is False
    boxes = env.load(env.project(BOXES)).segment
    assert boxes.enabled is True and boxes.sequence_resolved is False
    assert env.load(env.project(SEQUENCE)).segment.sequence_resolved is True
    # 这些符号的判断已全部改读 sequence_resolved；仍读开关的符号都在 ALLOWED_RESIDUAL 里单独登记。
    for _number, relative, symbol, klass, attribute in REGISTRY:
        if klass != 1 or attribute != SEQ or (relative, symbol) in ALLOWED_RESIDUAL:
            continue
        for node in _symbol_nodes(ast.parse((ROOT / relative).read_text(encoding="utf-8")), symbol):
            assert "segment.enabled" not in ast.unparse(node), f"{relative}:{symbol}"


# ── 类 2：阶段在链 ─────────────────────────────────────────────────────────


def test_image_mode_segment_stays_in_chain_and_console(env):
    cfg = env.load(env.project(IMAGE))
    stages = [SimpleNamespace(name=name) for name in ("annotate", "segment", "dedup")]
    chain = ProcessWorkflow._compose_chain(SimpleNamespace(cfg=cfg, stages=stages), include_generate=False)
    assert [stage.name for stage in chain] == ["segment", "dedup", "annotate"]

    renderer = ConsoleRenderer()
    renderer._freeze_header_facts(cfg)
    assert renderer._chain[0] == "segment"

    off = env.load(env.project(None))
    assert off.segment.enabled is False
    chain = ProcessWorkflow._compose_chain(SimpleNamespace(cfg=off, stages=stages), include_generate=False)
    assert [stage.name for stage in chain] == ["dedup", "annotate"]
    renderer._freeze_header_facts(off)
    assert "segment" not in renderer._chain


def test_image_mode_factory_selects_image_stage(env, monkeypatch):
    """enabled 决定在链，mode 只选阶段类；阶段名与链首位置不变。"""
    stub = types.ModuleType("labelkit.operators.segment_image")

    class SegmentImageStage:
        name = "segment"

        def __init__(self, cfg):
            self.cfg = cfg

    stub.SegmentImageStage = SegmentImageStage
    monkeypatch.setitem(sys.modules, "labelkit.operators.segment_image", stub)

    image = env.load(env.project(IMAGE))
    stages = build_stages(image)
    assert type(stages[0]) is SegmentImageStage and stages[0].cfg is image
    assert [stage.name for stage in stages][0] == "segment"
    assert sum(stage.name == "segment" for stage in stages) == 1

    sequence = build_stages(env.load(env.project(SEQUENCE)))
    assert type(sequence[0]) is SegmentStage

    off = build_stages(env.load(env.project(None)))
    assert all(stage.name != "segment" for stage in off)


# ── 类 3：引用、视觉、修复与预算集合 ──────────────────────────────────────


def test_image_mode_segment_llm_in_credential_set(env, monkeypatch):
    project = env.project(IMAGE + 'llm = "vision"\n')
    cfg = env.load(project)
    llm_names, embedding_names = referenced_profiles(cfg)
    assert "vision" in llm_names and embedding_names == []

    # 凭据物化用同一引用集：segment.llm 的密钥缺失就是启动错误，提供后进入密钥表。
    config = config_text().replace('[llm.vision]\nprovider = "openai_compatible"\nbase_url = "https://example.com/v1"\n'
                                   'model = "vision-model"\napi_key_env = "LK_TEST_KEY"',
                                   '[llm.vision]\nprovider = "openai_compatible"\nbase_url = "https://example.com/v1"\n'
                                   'model = "vision-model"\napi_key_env = "LK_VISION_KEY"')
    cfg = env.load(project, config)
    monkeypatch.setenv("LK_TEST_KEY", "dummy-test")
    monkeypatch.delenv("LK_VISION_KEY", raising=False)
    with pytest.raises(ConfigError) as caught:
        resolve_credentials(cfg)
    assert any("LK_VISION_KEY" in error for error in caught.value.errors)
    monkeypatch.setenv("LK_VISION_KEY", "dummy-vision")
    assert "vision" in resolve_credentials(cfg).llm


def test_boxes_only_segment_llm_stays_out_of_credential_set(env, monkeypatch):
    config = config_text().replace('model = "default-model"\napi_key_env = "LK_TEST_KEY"',
                                   'model = "default-model"\napi_key_env = "LK_DEFAULT_KEY"')
    cfg = env.load(env.project(BOXES, annotate='instruction = "标注"\nllm = "vision"\n',
                               body='[generate]\nllms = ["vision"]\n'), config)
    assert "default" not in referenced_profiles(cfg)[0]
    monkeypatch.setenv("LK_TEST_KEY", "dummy-test")
    monkeypatch.delenv("LK_DEFAULT_KEY", raising=False)
    assert "default" not in resolve_credentials(cfg).llm


def test_image_mode_undefined_segment_llm_is_a_profile_error(env):
    """第 5 行：图片模式写了 instruction 时 segment.llm 走引用校验。"""
    errors = env.errors(env.project(IMAGE + 'llm = "missing"\n'))
    assert any("[segment].llm" in error and "missing" in error for error in errors)


@pytest.mark.parametrize("attempts, referenced", [(0, False), (1, True)])
def test_image_mode_repair_llm_follows_repair_rounds(env, attempts, referenced):
    """第 9、22 行：图片模式与流模式同一公式，max_repair_attempts = 0 时不进集合。"""
    project = env.project(IMAGE, output=f'repair_llm = "vision"\nmax_repair_attempts = {attempts}\n',
                          annotate='instruction = "标注"\n')
    assert ("vision" in referenced_profiles(env.load(project))[0]) is referenced


def test_disabled_segment_repair_llm_stays_referenced(env):
    """第 9、22 行关闭侧：开关关闭时 repair_llm 恒进集合，字节不变的旧语义。"""
    project = env.project(None, output='repair_llm = "vision"\nmax_repair_attempts = 0\n')
    assert "vision" in referenced_profiles(env.load(project))[0]


def test_image_mode_repair_llm_joins_vision_set(env):
    """第 7 行：两种模式的修复都重放图片。"""
    project = env.project(IMAGE, output='repair_llm = "plain"\nmax_repair_attempts = 1\n')
    errors = env.errors(project)
    assert any("output.repair" in error and "plain" in error for error in errors)


# ── 类 1：行为抽样，每个模块的图片模式都走非流分支 ───────────────────────


def test_console_has_no_stream_badge_in_image_mode(env):
    renderer = ConsoleRenderer()
    image = env.load(env.project(IMAGE))
    renderer._freeze_header_facts(image)
    assert renderer._mode_badge == "process/ui"
    renderer._cfg = image
    assert "noise" not in renderer._account_line({})

    sequence = env.load(env.project(SEQUENCE))
    renderer._freeze_header_facts(sequence)
    assert renderer._mode_badge == "process/ui/stream"
    renderer._cfg = sequence
    assert "noise" in renderer._account_line({})


def test_console_estimate_notes_omit_stream_line_in_image_mode(env):
    def notes(cfg):
        lines: list[str] = []
        renderer = ConsoleRenderer()
        renderer._console = SimpleNamespace(print=lines.append)
        renderer._Text = lambda text, style=None: text
        renderer._print_estimate_notes(cfg)
        return "\n".join(lines)

    assert "stream estimate" not in notes(env.load(env.project(IMAGE)))
    assert "stream estimate" in notes(env.load(env.project(SEQUENCE)))


def test_dry_notes_omit_stream_lines_in_image_mode(env, capsys):
    workflow = SimpleNamespace(cfg=env.load(env.project(IMAGE)))
    ProcessWorkflow._print_dry_notes(workflow)
    err = capsys.readouterr().err
    assert "stream estimate" not in err and "capacity repartition" not in err

    workflow = SimpleNamespace(cfg=env.load(env.project(SEQUENCE)))
    ProcessWorkflow._print_dry_notes(workflow)
    err = capsys.readouterr().err
    assert "stream estimate" in err and "capacity repartition" in err


def test_estimate_ignores_sessions_in_image_mode(tmp_path):
    """第 41、42 行：图片样本按记录计批，不按会话，也不算流调用基数。"""
    plan = SimpleNamespace(estimated_records=10, session_lens=(6, 4))
    cfg = make_cfg(tmp_path, batch_size=4, annotate=True, segment=IMAGE_SEGMENT)
    estimate = estimate_run(cfg, plan)
    assert estimate["batches"] == 3
    assert estimate["annotate_calls"] == 10
    sequence = estimate_run(make_cfg(tmp_path, batch_size=4, annotate=True, segment=SEQUENCE_SEGMENT), plan)
    assert sequence["batches"] == 2
    assert sequence["annotate_calls"] == 2


def test_ingest_plan_has_no_sessions_in_image_mode(env):
    """第 37 行：图片模式不做会话预演。"""
    _write_png(env.tmp / "capture" / "image_1.png")
    _write_png(env.tmp / "capture" / "image_2.png")
    (env.tmp / "capture" / "uitree_1.jsonl").write_text(
        '{"id": "0", "parent": null, "class": "View", "text": "", "visible": true, '
        '"bounds": [0, 0, 4, 4], "package": "p"}\n', encoding="utf-8")
    (env.tmp / "capture" / "uitree_2.jsonl").write_text(
        '{"id": "0", "parent": null, "class": "View", "text": "", "visible": true, '
        '"bounds": [0, 0, 4, 4], "package": "p"}\n', encoding="utf-8")
    image_plan = Ingestor(env.load(env.project(IMAGE))).scan(estimate=True)
    assert image_plan.session_lens == ()
    sequence_plan = Ingestor(env.load(env.project(SEQUENCE))).scan(estimate=True)
    assert sum(sequence_plan.session_lens) == 2


async def test_workflow_takes_plain_batch_path_in_image_mode(tmp_path):
    """第 43、44、47、48、49 行：普通批路径，报表没有流块与流计数。"""
    order: list[str] = []
    cfg = make_cfg(tmp_path, batch_size=8, dedup=False, annotate=True, segment=IMAGE_SEGMENT)
    orch, _metrics, emitter, ingestor = build(cfg, [Probe("segment", order), Probe("annotate", order)],
                                              [rec(1), rec(2), rec(3)])
    await orch.run()
    assert ingestor.records_called is True           # 流模式走 sessions()，这里不可达
    assert order == ["segment", "annotate"]
    assert "stream" not in emitter.report
    counts = emitter.report["counts"]
    assert not {"episodes", "absorbed", "dropped_noise", "unprocessed"} & set(counts)
    payload = ProcessWorkflow._batch_end_payload(SimpleNamespace(cfg=cfg), {}, (0, 0), 0.0)
    assert not {"episodes", "absorbed", "dropped_noise"} & set(payload)


def _budget_workflow(tmp_path, segment):
    cfg = make_cfg(tmp_path, annotate=True, segment=segment)
    profile = SimpleNamespace(name="default", context_window=8192)
    return SimpleNamespace(cfg=dataclasses.replace(cfg, llm_profiles={"default": profile}),
                           _budget_profiles=lambda: [("default", 8192, 4096)])


def test_budget_startup_log_and_report_omit_minimum_frames_in_image_mode(tmp_path, caplog, monkeypatch):
    """第 45、50 行。"""
    logger = "labelkit.process_workflow"
    with caplog.at_level(logging.INFO, logger=logger):
        ProcessWorkflow._log_budget_startup(_budget_workflow(tmp_path, IMAGE_SEGMENT))
    assert not any("minimum_frames" in record.getMessage() for record in caplog.records)
    caplog.clear()
    monkeypatch.setattr("labelkit.common.inference.budget.min_window", lambda cfg: 2)
    with caplog.at_level(logging.INFO, logger=logger):
        ProcessWorkflow._log_budget_startup(_budget_workflow(tmp_path, SEQUENCE_SEGMENT))
    assert any("minimum_frames" in record.getMessage() for record in caplog.records)

    def budget_block(segment):
        workflow = _budget_workflow(tmp_path, segment)
        workflow.llm = None

        class _Counters:
            values: dict = {}

            def __call__(self, key):
                return 0

        return ProcessWorkflow._report_budget(workflow, _Counters())

    assert "minimum_frames" not in budget_block(IMAGE_SEGMENT)
    assert budget_block(SEQUENCE_SEGMENT)["minimum_frames"] == 2


def test_emitter_has_no_stream_semantics_in_image_mode(env):
    """第 32、33、34 行。"""
    def emitter(segment):
        cfg = env.load(env.project(segment))
        return Emitter(cfg, engine=None, run_id="a" * 12, run_started_at=datetime.now().astimezone())

    member = _ui_record("m")
    item = SimpleNamespace(record=_ui_record("s", kind="sequence", members=(member, member)))
    verification = SimpleNamespace(verdict="pass", rounds=1, defects=("x",))
    image, sequence = emitter(IMAGE), emitter(SEQUENCE)

    assert image._stream_block(item) is None
    assert "defects" not in image._verification_block(SimpleNamespace(verification=verification))
    assert sequence._verification_block(SimpleNamespace(verification=verification))["defects"] == ["x"]
    # 第 32 行镜像解析规则：空选择子在图片模式不解析为轨迹准则。
    empty = dataclasses.replace(image._cfg.quality, rubric="")
    image._cfg = dataclasses.replace(image._cfg, quality=empty)
    sequence._cfg = dataclasses.replace(sequence._cfg, quality=empty)
    assert image._rubric_selector() == "default:ui"
    assert sequence._rubric_selector() == "default:trajectory"


def test_annotate_prompt_keeps_image_px_in_image_mode(env):
    """第 23 行：流模式固定图片工作点，图片模式保留升档后的 image_px。"""
    record = dataclasses.replace(_ui_record(), modality="text", text="文本", ui_tree=None, image=None)
    opts = AnnotatePromptOptions(image_px=1024)
    assert _assemble_prompt(record, env.load(env.project(IMAGE)), "{}", opts).image_px == 1024
    assert _assemble_prompt(record, env.load(env.project(SEQUENCE)), "{}", opts).image_px is None


def test_frame_prompts_do_not_use_complete_evidence_in_image_mode(tmp_path):
    """第 26、29、30、35 行：完整证据门在图片模式为假（这些路径由 rule 76 挡在 M1 之外）。"""
    member = _ui_record()
    image_cfg = make_cfg(tmp_path, annotate=True, modality="ui", segment=IMAGE_SEGMENT)
    sequence_cfg = make_cfg(tmp_path, annotate=True, modality="ui", segment=SEQUENCE_SEGMENT)

    def texts(bundle):
        return [part.text for message in bundle.messages for part in message.parts if part.kind == "text"]

    assert any("[前一帧完整控件树]" in text for text in texts(build_extract_prompt(member, member, sequence_cfg, None)))
    assert not any("[前一帧完整控件树]" in text for text in texts(build_extract_prompt(member, member, image_cfg, None)))

    # frame annotate：流模式发完整证据；图片模式走可裁剪的普通序列化，两者调用的序列化入口不同。
    calls: list[int | None] = []
    tree = member.ui_tree

    class _Spy:
        def serialize(self, max_chars=None):
            calls.append(max_chars)
            return tree.serialize(max_chars=max_chars)

    _assemble_frame_prompt(dataclasses.replace(member, ui_tree=_Spy()), image_cfg, "{}", None)
    assert calls == [image_cfg.input.ui_tree_max_chars]

    # frame classify：图片模式且未开视觉时不附成员截图。
    frame_cfg = dataclasses.replace(image_cfg, frame_classify=dataclasses.replace(
        image_cfg.frame_classify, vision_resolved=False))
    bundle = _assemble_frame_classify((member,), frame_cfg, ("摘要",))
    assert not any(part.kind == "image" for message in bundle.messages for part in message.parts)
    stream_cfg = dataclasses.replace(sequence_cfg, frame_classify=frame_cfg.frame_classify)
    bundle = _assemble_frame_classify((member,), stream_cfg, ("摘要",))
    assert any(part.kind == "image" for message in bundle.messages for part in message.parts)


def test_quality_pairwise_prompt_keeps_tree_limit_in_image_mode(tmp_path, monkeypatch):
    """第 38 行：只有流模式取消树上限。"""
    captured = []
    monkeypatch.setattr("labelkit.operators.quality._build_pairwise_prompt",
                        lambda *args: captured.append(args[3]) or "prompt")
    call = SimpleNamespace(record_a=None, record_b=None, transitions_a=None, transitions_b=None, criteria=(),
                           with_reason=False)
    for segment in (IMAGE_SEGMENT, SEQUENCE_SEGMENT):
        cfg = make_cfg(tmp_path, quality=True, segment=segment)
        QualityStage._pairwise_prompt(SimpleNamespace(cfg=cfg), call, None)
    assert captured == [make_cfg(tmp_path).input.ui_tree_max_chars, None]


async def test_verify_routes_sequence_items_to_classic_in_image_mode(tmp_path):
    """第 39 行：图片模式下即使出现序列记录也不进流复核驱动器。"""
    cfg = make_cfg(tmp_path, annotate=True, verify=True, modality="ui", segment=IMAGE_SEGMENT)
    item = SimpleNamespace(status="active", annotation={"a": 1}, record=_ui_record("s", kind="sequence"))
    seen = []

    async def run_classic(items, ctx):
        seen.append(items)

    stage = SimpleNamespace(cfg=cfg, _run_classic=run_classic)
    assert await VerifyStage.run(stage, [item], None) == [item]
    assert seen == [[item]]


# ── 迁移面 ─────────────────────────────────────────────────────────────────


def test_every_enabled_example_project_declares_mode():
    """没有遗漏的工程：除图片模式示例外，启用 segment 的 TOML 恰是登记的 11 个且 mode 为 sequence。"""
    enabled = {}
    for path in EXAMPLES.rglob("*.toml"):
        relative = path.relative_to(EXAMPLES)
        if "out" in relative.parts:
            continue
        segment = tomllib.loads(path.read_text(encoding="utf-8")).get("segment", {})
        if segment.get("enabled") is True and segment.get("mode") != "image":
            enabled[relative.as_posix()] = segment.get("mode")
    assert enabled == {f"{subdir}/{project}": "sequence" for subdir, project, _config in SEQUENCE_PROJECTS}


@pytest.mark.parametrize("case", SEQUENCE_PROJECTS, ids=[f"{row[0]}-{row[1]}" for row in SEQUENCE_PROJECTS])
def test_all_stream_projects_validate_and_dry_run(case, monkeypatch, tmp_path, capsys):
    subdir, project, config = case
    for name in [name for name in os.environ if name.startswith("LABELKIT_")]:
        monkeypatch.delenv(name)
    monkeypatch.chdir(EXAMPLES / subdir)
    assert main(["validate", "--config", config, "--project", project]) == 0
    assert "configuration valid" in capsys.readouterr().err

    extra = []
    if project == "project-replay.toml":
        # run.input 是上一次生成运行的产物，干净检出里不存在；这里只替换为合法的生成流。
        replay = tmp_path / "generated.stream.jsonl"
        lines = [json.dumps(row, ensure_ascii=False) for row in _generation_stream_rows()]
        replay.write_text("\n".join(lines) + "\n", encoding="utf-8")
        extra = ["--input", str(replay)]
    code = main(["run", "--config", config, "--project", project, "--output", str(tmp_path / "o.jsonl"),
                 "--dry-run", "--console", "plain", *extra])
    assert code == 0
    err = capsys.readouterr().err
    assert "dry-run: estimated LLM calls" in err
    # 流工程的两条注记门都读 sequence_resolved：第二条恒在，证明它们仍走流分支。
    assert "dry-run: note: capacity repartition" in err


@pytest.mark.parametrize("case", ("ui", "stitch", "static", "reactive", "minimum"))
def test_prepare_regenerates_committed_projects(case, tmp_path, monkeypatch):
    """prepare.py 在临时目录重新生成后，文本工件与已提交文件逐字节相同。

    图片不比较：prepare.py 的中文字体只存在于 macOS，这里替换字体只为让生成走完。
    写盘换行固定为 LF，否则 Windows 的文本模式换行转换会掩盖模板本身的字节。
    """
    from PIL import ImageFont

    source = EXAMPLES / "sequence-context-capacity"
    real_truetype = ImageFont.truetype
    monkeypatch.setattr(ImageFont, "truetype", lambda font=None, size=10, *args, **kwargs: (
        ImageFont.load_default(size=size) if str(font).startswith("/System/")
        else real_truetype(font, size, *args, **kwargs)))
    real_write = Path.write_text
    monkeypatch.setattr(Path, "write_text", lambda self, data, encoding=None, errors=None, newline=None: (
        real_write(self, data, encoding=encoding, errors=errors, newline="\n")))

    root = tmp_path / "copy"
    (root / "data").mkdir(parents=True)
    (root / "schemas").mkdir()
    (root / "config-local-4b.toml").write_bytes((source / "config-local-4b.toml").read_bytes())
    runpy.run_path(str(source / "prepare.py"))["prepare"](root, case)

    generated = sorted(path.relative_to(root) for path in root.rglob("*")
                       if path.is_file() and path.suffix != ".png" and path.name != "config-local-4b.toml")
    assert PurePosixPath(f"project-{case}.toml") in {PurePosixPath(path.as_posix()) for path in generated}
    for relative in generated:
        assert (root / relative).read_bytes() == (source / relative).read_bytes(), relative.as_posix()


def test_integration_tests_collect():
    """tests/integration 能完整收集，且内联配置里启用 segment 的片段都写了 mode。"""
    env = dict(os.environ, PYTHONUTF8="1")
    done = subprocess.run([sys.executable, "-m", "pytest", "--collect-only", "-q", "-m", "integration",
                           "-p", "no:cacheprovider", "tests/integration"],
                          cwd=ROOT, env=env, capture_output=True, text=True, encoding="utf-8", timeout=300)
    assert done.returncode == 0, done.stdout + done.stderr
    assert not re.search(r"\berrors?\b", done.stdout.splitlines()[-1])
    assert re.search(r"\d+ tests? collected|\d+/\d+ tests? collected", done.stdout)

    for path in (ROOT / "tests" / "integration").glob("*.py"):
        text = path.read_text(encoding="utf-8")
        for match in re.finditer(r"\[segment\](?:\\n|\n)", text):
            block = text[match.start():match.start() + 200]
            if re.search(r"enabled\s*=\s*true", block):
                assert "mode =" in block, f"{path.name}: [segment] without mode"
        assert not re.search(r"SegmentConfig\(enabled=True(?![^)]*mode=)", text), path.name


def test_production_files_within_line_limit():
    over = {path.relative_to(ROOT).as_posix(): lines
            for path in (ROOT / "labelkit").rglob("*.py")
            if (lines := len(path.read_text(encoding="utf-8").splitlines())) > 2000}
    assert over == {}
