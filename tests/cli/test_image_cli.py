"""图片模式的 validate 与 dry-run（SPEC-image-region-segmentation §16.5，AC-01、AC-05、AC-30、AC-143）。

dry-run 走真实的 M1 与 M2 扫描，不发模型请求，也不需要凭据。
"""
from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path

import pytest
from PIL import Image

from labelkit.cli import main

_EXAMPLE = Path(__file__).resolve().parents[2] / "examples" / "image-segmentation"
_GOLDENS = Path(__file__).parent / "goldens"
_CONFIG = """
schema_version = 1

[llm.default]
provider = "anthropic"
base_url = "https://api.z.ai/api/anthropic"
model = "glm-5.2"
api_key_env = "LABELKIT_ZAI_KEY"
supports_vision = true
context_window = 131072
"""
_SCHEMA = '{"type": "object", "additionalProperties": false, "required": ["items"], ' \
          '"properties": {"items": {"type": "array", "items": {"type": "string"}}}}'


@pytest.fixture(autouse=True)
def keyless_environment(monkeypatch):
    """dry-run 与 validate 是纯静态面，任何 LABELKIT_ 变量都不应该被用到。"""
    for name in [name for name in os.environ if name.startswith("LABELKIT_")]:
        monkeypatch.delenv(name)


@pytest.fixture(autouse=True)
def restore_logging():
    """main 的 setup_logging 会把 handler 绑到被捕获的 stderr，用完摘掉。"""
    logger = logging.getLogger("labelkit")
    saved = logger.handlers[:]
    yield
    logger.handlers = saved


def _dry_run(project: str, output: Path, *extra: str) -> None:
    """跑一次示例工程的 dry-run 并确认退出码；stderr 由调用方经 capsys 读取。

    @param project 示例工程文件名
    @param output 主输出路径，dry-run 只在它旁边写报告
    @param extra 追加的命令行参数
    """
    code = main(["run", "--config", "../config.toml", "--project", project, "--output", str(output),
                 "--dry-run", "--console", "plain", *extra])
    assert code == 0


def _dry_lines(capsys) -> list[str]:
    return [line for line in capsys.readouterr().err.splitlines() if line.startswith("dry-run")]


def _calls(lines: list[str]) -> dict[str, int]:
    line = next(line for line in lines if "estimated LLM calls" in line)
    return {key: int(value) for key, value in re.findall(r"(\w+)=(\d+)", line)}


def _report(output: Path) -> dict:
    return json.loads(output.with_name(output.stem + ".dryrun.report.json").read_text(encoding="utf-8"))


@pytest.mark.parametrize("project,golden", [
    ("project.toml", "dryrun-image-model.txt"),
    ("project-boxes.toml", "dryrun-image-boxes.txt"),
])
def test_dry_run_image_golden_files(project, golden, monkeypatch, tmp_path, capsys):
    """两种候选来源的 dry-run 输出字节锚定；golden 不含流注记。"""
    monkeypatch.chdir(_EXAMPLE)
    _dry_run(project, tmp_path / "o.jsonl")
    lines = _dry_lines(capsys)
    assert lines == (_GOLDENS / golden).read_text(encoding="utf-8").splitlines()
    assert not any("stream estimate" in line or "capacity repartition" in line for line in lines)


def test_dry_run_model_segment_calls_equal_records(monkeypatch, tmp_path, capsys):
    """写了 instruction：每个样本一个分割调用，随 --limit 收缩，并计入 total。"""
    monkeypatch.chdir(_EXAMPLE)
    output = tmp_path / "o.jsonl"
    _dry_run("project.toml", output)
    calls = _calls(_dry_lines(capsys))
    assert calls["segment_calls"] == 2
    assert calls["annotate_calls"] == 2
    assert calls["total"] == 4

    report = _report(output)
    estimate = report["estimate"]
    assert estimate["records"] == estimate["segment_calls"] == 2
    assert estimate["batches"] == 1             # 没有会话预演：按记录分批
    assert estimate["total_calls"] == 4
    assert "stream" not in report
    assert report["segment_image"] == {"calls": 0, "samples": 0, "boxes": 0, "regions": 0, "empty": 0,
                                       "failed": {}, "usage": {"prompt_tokens": 0, "completion_tokens": 0}}
    keys = list(report)
    assert keys.index("estimate") < keys.index("segment_image") < keys.index("dedup")

    limited = tmp_path / "limited.jsonl"
    _dry_run("project.toml", limited, "--limit", "1")
    assert _calls(_dry_lines(capsys))["segment_calls"] == 1
    assert _report(limited)["estimate"]["records"] == 1


def test_dry_run_boxes_only_segment_calls_zero(monkeypatch, tmp_path, capsys):
    """只用参考框（D15）：segment 不调用模型，估算不计；其余阶段照算。"""
    monkeypatch.chdir(_EXAMPLE)
    output = tmp_path / "o.jsonl"
    _dry_run("project-boxes.toml", output)
    lines = _dry_lines(capsys)
    assert "estimated_records=2" in lines[0]
    calls = _calls(lines)
    assert calls["segment_calls"] == 0
    assert calls["annotate_calls"] == calls["total"] == 2
    assert _report(output)["estimate"]["segment_calls"] == 0


def _image_project(tmp_path: Path, segment: str, sections: str) -> Path:
    """在临时目录写一个两张图片的工程，返回工程文件路径。"""
    data = tmp_path / "data"
    data.mkdir()
    for index in (1, 2):
        Image.new("RGB", (32, 32), (index, 0, 0)).save(data / f"image_{index}.png")
        (data / f"image_{index}.cmp").write_bytes(b"companion")
    (data / "notes.txt").write_text("stray", encoding="utf-8")
    (tmp_path / "config.toml").write_text(_CONFIG, encoding="utf-8")
    (tmp_path / "schema.json").write_text(_SCHEMA, encoding="utf-8")
    project = tmp_path / "project.toml"
    project.write_text(f'''
schema_version = 1

[run]
input = "./data"
output = "./out.jsonl"
modality = "ui"

[segment]
enabled = true
mode = "image"
{segment}

[quality]
enabled = false

[verify]
enabled = false

[output]
schema_path = "schema.json"
meta_mode = "inline"

{sections}
''', encoding="utf-8")
    return project


def test_segment_only_dry_run_drops_quality_and_annotate_items(monkeypatch, tmp_path, capsys):
    """只跑分割（D9）：估算只有分割调用，报告没有 quality 块，被忽略的文件计入 counts。"""
    project = _image_project(tmp_path, 'instruction = "分割"', "[annotate]\nenabled = false\n")
    monkeypatch.chdir(tmp_path)
    assert main(["run", "--config", "config.toml", "--project", project.name, "--dry-run", "--console", "plain"]) == 0
    calls = _calls(_dry_lines(capsys))
    assert calls["segment_calls"] == calls["total"] == 2
    assert calls["quality_calls"] == calls["annotate_calls"] == 0
    report = _report(tmp_path / "out.jsonl")
    assert "quality" not in report
    assert report["counts"]["ignored_files"] == 3   # 未注册钩子：两个伴随文件与杂项文件都不算输入


def test_validate_and_dry_run_never_call_box_parser(monkeypatch, tmp_path, capsys):
    """validate 与 --dry-run 只做静态签名检查：钩子被调用就会留下哨兵文件并让运行失败。"""
    sentinel = tmp_path / "called.txt"
    (tmp_path / "hooks.py").write_text(
        "from pathlib import Path\n\n\n"
        "def parse(companion, image):\n"
        f"    Path({sentinel.as_posix()!r}).write_text('called', encoding='utf-8')\n"
        "    raise AssertionError('box_parser must not run at startup')\n", encoding="utf-8")
    project = _image_project(
        tmp_path, 'box_parser = "hooks.py:parse"\ncompanion_suffix = ".cmp"',
        '[annotate]\nenabled = true\nllm = "default"\ninstruction = "标注"\n')
    monkeypatch.chdir(tmp_path)
    assert main(["validate", "--config", "config.toml", "--project", project.name]) == 0
    assert "configuration valid" in capsys.readouterr().err
    assert main(["run", "--config", "config.toml", "--project", project.name, "--dry-run", "--console", "plain"]) == 0
    assert _calls(_dry_lines(capsys))["segment_calls"] == 0
    assert not sentinel.exists()

    # 反向对照：签名不对的钩子在同一路径上被静态拒绝，说明钩子确实被加载并检查过。
    (tmp_path / "hooks.py").write_text("def parse(companion):\n    return []\n", encoding="utf-8")
    assert main(["validate", "--config", "config.toml", "--project", project.name]) == 2
    assert "box_parser" in capsys.readouterr().err
