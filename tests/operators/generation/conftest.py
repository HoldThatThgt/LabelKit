"""v1.20 generation 纯逻辑测试共享夹具。"""

from __future__ import annotations

from pathlib import Path

import pytest

from labelkit.cli.parser import CliOverrides
from labelkit.common.config import load
from labelkit.operators.generation.program import compile_generation_program


_ROOT = Path(__file__).resolve().parents[3]
_EXAMPLE = _ROOT / "examples" / "sequence-generation"


@pytest.fixture
def declared_config(monkeypatch):
    """装载真实 declared 教学配置但不访问网络。"""
    monkeypatch.setenv("LABELKIT_DEEPSEEK_KEY", "offline-test-key")
    return load(_EXAMPLE / "config.toml", _EXAMPLE / "project.toml", CliOverrides())


@pytest.fixture
def instruction_config(monkeypatch):
    """装载真实 instruction-only 教学配置但不访问网络。"""
    monkeypatch.setenv("LABELKIT_DEEPSEEK_KEY", "offline-test-key")
    project = _EXAMPLE / "project-instruction-only.toml"
    return load(_EXAMPLE / "config.toml", project, CliOverrides())


@pytest.fixture
def declared_program(declared_config):
    """返回 declared 的冻结 GenerationProgram。"""
    return compile_generation_program(declared_config)


@pytest.fixture
def instruction_program(instruction_config):
    """返回 instruction-only 的冻结 GenerationProgram。"""
    return compile_generation_program(instruction_config)
