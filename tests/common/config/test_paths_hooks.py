"""路径与通用输出、flat sample 钩子面的离线测试。

覆盖三 cwd 一致性、TOML 相对 vs CLI 相对 precedence、四类 schema_path + trace +
input/output + 四 hook 的 project-root 解析、hook module name hash 防污染、
validate/dry-run 零 ``os.environ.get``、hook 异常/非法返回聚合进 ConfigError。
纯逻辑——不涉 LLM、不涉网络。
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from labelkit.common.config.loader import load
from labelkit.common.config.model import CliOverrides
from labelkit.common.errors import ConfigError

_HOOKS_PY = '''
def validate_output(obj, record):
    return []

def validate_sample(text):
    return []

def explodes(text):
    raise RuntimeError("hook exploded")

def bad_return(text):
    return "not-a-list"

def output_explodes(obj, record):
    raise RuntimeError("hook exploded")

def output_bad_return(obj, record):
    return "not-a-list"
'''

# 指向一个刻意不设置的环境变量——keyless 静态装载的锚（SPEC-SP §13.1 credential 锚）。
_KEY_ENV = "LK_W2A_KEY_NEVER_SET"

_CONFIG = f"""schema_version = 1

[llm.default]
provider = "openai_compatible"
base_url = "https://llm-gw.example.com/v1"
model = "test-model"
api_key_env = "{_KEY_ENV}"
"""

_SCHEMA_JSON = '{"type": "object", "properties": {"topic": {"type": "string"}}, "required": ["topic"]}'


def _make_project(tmp_path: Path) -> Path:
    """生成一个自含工程目录（输入/输出/schema/钩子文件全相对声明）。"""
    (tmp_path / "data.jsonl").write_text('{"text": "样例"}\n', encoding="utf-8")
    (tmp_path / "hooks.py").write_text(_HOOKS_PY, encoding="utf-8")
    (tmp_path / "schema.json").write_text(_SCHEMA_JSON, encoding="utf-8")
    (tmp_path / "out").mkdir(exist_ok=True)
    (tmp_path / "config.toml").write_text(_CONFIG, encoding="utf-8")
    project = tmp_path / "project.toml"
    project.write_text(
        "schema_version = 1\n"
        "\n"
        "[run]\n"
        'mode = "process"\n'
        'modality = "text"\n'
        'input = "data.jsonl"\n'
        'output = "out/labels.jsonl"\n'
        "\n"
        "[annotate]\n"
        'instruction = "标注意图"\n'
        "\n"
        "[output]\n"
        'schema_path = "schema.json"\n'
        'validator = "hooks.py:validate_output"\n'
        "\n"
        "[trace]\n"
        "enabled = true\n"
        'path = "trace/custom.trace.jsonl"\n'
        "\n"
        "[generate]\n"
        'enabled = false\n'
        'sample_validator = "hooks.py:validate_sample"\n',
        encoding="utf-8")
    return project



def _load(tmp_path: Path, cli: CliOverrides | None = None):
    return load(tmp_path / "config.toml", tmp_path / "project.toml",
                cli or CliOverrides())


@pytest.fixture(autouse=True)
def _keyless(monkeypatch):
    """锚 2 环境：目标密钥环境变量刻意缺席。"""
    monkeypatch.delenv(_KEY_ENV, raising=False)


@pytest.fixture()
def project_dir(tmp_path) -> Path:
    project = _make_project(tmp_path)
    (tmp_path / "trace").mkdir(exist_ok=True)
    return project.parent


def test_keyless_static_load_succeeds_and_reads_no_env_values(project_dir, monkeypatch):
    """SPEC-SP §13.1 credential 锚 + §5.2：无 key 的 validate/dry-run 全程零
    ``os.environ.get``。"""
    calls: list[str] = []

    def _forbidden(*args, **kw):
        calls.append(args[0] if args else "?")
        raise AssertionError(f"os.environ.get forbidden in static load: {args}")

    monkeypatch.setattr(os.environ, "get", _forbidden)
    cfg = _load(project_dir)
    assert calls == []
    assert cfg.llm_profiles["default"].api_key_envs == (_KEY_ENV,)
    assert not hasattr(cfg.llm_profiles["default"], "api_key")   # 值字段已删除


def test_three_cwds_yield_identical_paths_and_digest(project_dir, monkeypatch, tmp_path):
    """SPEC-SP §13.4：三个不同 cwd 对同一 project 运行，ResolvedPaths 与 digest 相同。"""
    digests, path_triples = set(), set()
    for i, cwd in enumerate(("cwd-a", "cwd-b", "cwd-c")):
        room = tmp_path / cwd
        room.mkdir(exist_ok=True)
        monkeypatch.chdir(room)
        cfg = _load(project_dir)
        digests.add(cfg.project_digest)
        path_triples.add((
            cfg.paths.project, cfg.paths.project_root, cfg.paths.input,
            cfg.paths.output, cfg.paths.report, cfg.paths.trace))
    assert len(digests) == 1
    assert len(path_triples) == 1
    project_root = str(project_dir.resolve())
    output = str((project_dir / "out" / "labels.jsonl").resolve())
    paths = path_triples.pop()
    assert paths[1] == project_root
    assert paths[2] == str((project_dir / "data.jsonl").resolve())
    assert paths[3] == output
    assert paths[4] == str((project_dir / "out" / "labels.report.json").resolve())
    assert paths[5] == str((project_dir / "trace" / "custom.trace.jsonl").resolve())


def test_run_paths_match_report_and_side_channels(project_dir):
    """live/dry-run report 名与未启用通道的 None 语义（SPEC-SP §5.1）。"""
    cfg = _load(project_dir)
    assert cfg.paths.report.endswith("labels.report.json")
    assert cfg.paths.rejects.endswith("labels.rejects.jsonl")   # rejects = "refs"
    assert cfg.paths.sidecar is None                            # meta_mode = "inline"
    assert cfg.paths.trace.endswith("custom.trace.jsonl")
    assert cfg.paths.stream is None                             # 形态关闭
    dry = _load(project_dir, CliOverrides(dry_run=True))
    assert dry.paths.report.endswith("labels.dryrun.report.json")


def test_toml_relative_output_anchors_project_root_cli_relative_anchors_cwd(
        project_dir, monkeypatch, tmp_path):
    """TOML 相对 output 按 project root；CLI 相对 output 先按调用 cwd 解析，再参与
    CLI > project 优先级（SPEC-SP §13.4 precedence 测试）。"""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "cli-out").mkdir(exist_ok=True)
    cli = _load(project_dir, CliOverrides(output="cli-out/o.jsonl"))
    assert cli.paths.output == str((tmp_path / "cli-out" / "o.jsonl").resolve())
    toml = _load(project_dir)
    assert toml.paths.output == str(
        (project_dir / "out" / "labels.jsonl").resolve())
    assert cli.paths.output != toml.paths.output


def test_cli_absolute_output_wins(project_dir):
    absolute = str(project_dir.parent / "abs.jsonl")
    cli = _load(project_dir, CliOverrides(output=absolute))
    assert cli.paths.output == str(Path(absolute).resolve())


def test_all_path_faces_resolve_against_project_root(project_dir, tmp_path):
    """四类 schema_path + trace + input/output 的 project-root 解析（§13.4）。"""
    cfg = _load(project_dir)
    root = project_dir.resolve()
    assert cfg.output.schema_path == str(root / "schema.json")
    assert cfg.user_schema["type"] == "object"
    assert cfg.run.input == str(root / "data.jsonl")
    assert cfg.run.output == str(root / "out" / "labels.jsonl")
    assert cfg.trace.path == str(root / "trace" / "custom.trace.jsonl")
    # 其余三类 schema_path 走原始按类节——用帧类生成面与按类标注面探针验证。
    (project_dir / "frame_schema.json").write_text(_SCHEMA_JSON, encoding="utf-8")
    (project_dir / "class_schema.json").write_text(_SCHEMA_JSON, encoding="utf-8")
    (project_dir / "project.toml").write_text(
        (project_dir / "project.toml").read_text(encoding="utf-8")
        + '\n[frame.annotate]\nenabled = false\nschema_path = "frame_schema.json"\n'
          '\n[classify]\nenabled = true\nfallback_class = "booking"\n'
          '\n[[classify.classes]]\nname = "booking"\ndescription = "d"\n'
          '\n[[classify.classes]]\nname = "other"\ndescription = "d"\n'
          '\n[class.booking.annotate]\nschema_path = "class_schema.json"\n',
        encoding="utf-8")
    cfg2 = _load(project_dir)
    assert cfg2.frame_annotate.schema_path == str(root / "frame_schema.json")
    assert cfg2.frame_schema is None            # enabled=false ⇒ 不装载, 但路径已绝对化
    assert cfg2.class_views                     # classify.enabled ⇒ 按类视图物化
    booking = cfg2.class_views["booking"]
    assert booking.schema == {"type": "object", "properties": {"topic": {"type": "string"}},
                              "required": ("topic",)}




def test_sample_validator_gated_on_generate(project_dir):
    """generate.enabled=false 时 sample_validator 不解析（v1.16 门保留），
    其余三键照常。"""
    cfg = _load(project_dir)
    assert cfg.validation_hooks.sample is None
    assert cfg.validation_hooks.output is not None


def test_same_named_hook_files_do_not_pollute_each_other(tmp_path, monkeypatch):
    """SPEC-SP §13.4：两个工程同名 hooks.py 不得污染（module name hash）。"""
    markers = ("AAA", "BBB")
    loaded = []
    for name, marker in zip(("proj-a", "proj-b"), markers):
        project_root = tmp_path / name
        project_root.mkdir()
        (project_root / "hooks.py").write_text(
            f"MARKER = {marker!r}\n\n\ndef validate_output(obj, record):\n"
            f"    return [MARKER]\n", encoding="utf-8")
        (project_root / "data.jsonl").write_text('{"text": "x"}\n', encoding="utf-8")
        (project_root / "out").mkdir()
        (project_root / "config.toml").write_text(_CONFIG, encoding="utf-8")
        (project_root / "project.toml").write_text(
            'schema_version = 1\n\n[run]\nmode = "process"\nmodality = "text"\n'
            'input = "data.jsonl"\noutput = "out/labels.jsonl"\n\n[annotate]\n'
            'instruction = "i"\n\n[output]\n'
            f'schema_inline = """{_SCHEMA_JSON}"""\n'
            'validator = "hooks.py:validate_output"\n', encoding="utf-8")
        monkeypatch.chdir(project_root)
        cfg = load(project_root / "config.toml", project_root / "project.toml",
                   CliOverrides())
        loaded.append(cfg.validation_hooks.output.target({}, None))
    assert loaded == [["AAA"], ["BBB"]]


def test_hook_exception_and_illegal_return_aggregate_into_config_error(tmp_path):
    """SPEC-SP §4.9：异常与非法返回值在 validate 阶段聚合，绝不等到首条真实 content。"""
    (tmp_path / "hooks.py").write_text(_HOOKS_PY, encoding="utf-8")
    (tmp_path / "data.jsonl").write_text('{"text": "x"}\n', encoding="utf-8")
    (tmp_path / "out").mkdir()
    (tmp_path / "config.toml").write_text(_CONFIG, encoding="utf-8")
    base = ('schema_version = 1\n\n[run]\nmode = "process"\nmodality = "text"\n'
            'input = "data.jsonl"\noutput = "out/labels.jsonl"\n\n[annotate]\n'
            'instruction = "i"\n\n[output]\n'
            f'schema_inline = """{_SCHEMA_JSON}"""\n')

    def errors_with(validator: str) -> list[str]:
        (tmp_path / "project.toml").write_text(
            base + f'validator = "{validator}"\n', encoding="utf-8")
        with pytest.raises(ConfigError) as ei:
            load(tmp_path / "config.toml", tmp_path / "project.toml", CliOverrides())
        return ei.value.errors

    errs = errors_with("hooks.py:output_explodes")
    assert any("synthetic dry-run raised RuntimeError" in e
               and "[output].validator" in e for e in errs), errs
    errs = errors_with("hooks.py:output_bad_return")
    assert any("synthetic dry-run returned an invalid value" in e for e in errs), errs
    errs = errors_with("hooks.py:validate_sample")
    assert any("must accept exactly 2 positional" in e for e in errs), errs
    errs = errors_with("hooks.py:missing_fn")
    assert any("not found in hook file" in e for e in errs), errs
    errs = errors_with("some.module:fn")
    assert any("form is deleted" in e for e in errs), errs


def test_output_must_not_equal_the_input_file(project_dir):
    """既有自吞防护在新路径语义下仍生效（绝对化后的同文件检查）。"""
    (project_dir / "project.toml").write_text(
        (project_dir / "project.toml").read_text(encoding="utf-8").replace(
            'output = "out/labels.jsonl"', 'output = "data.jsonl"'),
        encoding="utf-8")
    with pytest.raises(ConfigError) as ei:
        _load(project_dir)
    assert any("same as the input file" in e for e in ei.value.errors)
