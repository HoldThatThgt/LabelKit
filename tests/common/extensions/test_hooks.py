"""Offline unit tests for labelkit/common/extensions/hooks.py.

file-form 钩子面（``<python-file>:<attribute-path>``）纯逻辑测试——不涉 LLM。
fixture 用 tmp 目录下的真实 .py 文件（文件形态装载的真实执行路径）。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

from labelkit.common.contracts.generation import StateTransitionInput
from labelkit.common.extensions.hooks import (
    ResolvedHook,
    ValidationHooks,
    check_hook_arity,
    clone_state_input,
    load_hook,
    normalize_state_violations,
    normalize_violations,
    probe_hook,
    state_probe_input,
)

# 仓库根下的样本钩子文件（文件形态装载面复用它）。
_REPO_HOOKS = Path(__file__).resolve().parents[3] / "tests" / "hook_samples.py"

_HOOK_PY = '''
def add_marker(obj, record):
    return []

def one_arg(text):
    return []

class Gate:
    @staticmethod
    def static_fn(text):
        return []

def explodes(text):
    raise RuntimeError("hook exploded")

def bad_return(text):
    return "not-a-list"

NOT_CALLABLE = 42
'''


@pytest.fixture()
def hook_dir(tmp_path, monkeypatch):
    """生成一个独立工程目录（含 hooks.py），并把 cwd 移走以验证不依赖 cwd。"""
    project = tmp_path / "proj"
    project.mkdir()
    (project / "hooks.py").write_text(_HOOK_PY, encoding="utf-8")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    return project


def test_load_hook_resolves_relative_file_against_project_root(hook_dir):
    hook = load_hook("hooks.py:add_marker", hook_dir)
    assert isinstance(hook, ResolvedHook)
    assert hook.reference == f"{(hook_dir / 'hooks.py').resolve()}:add_marker"
    assert callable(hook.target)
    assert hook.target({}, None) == []


def test_load_hook_accepts_absolute_path(hook_dir):
    ref = f"{hook_dir / 'hooks.py'}:one_arg"
    hook = load_hook(ref, hook_dir.parent)
    assert hook.target("x") == []
    assert hook.reference.endswith(":one_arg")


def test_load_hook_supports_dotted_attribute_path(hook_dir):
    hook = load_hook("hooks.py:Gate.static_fn", hook_dir)
    assert hook.target("x") == []


def test_load_hook_rejects_old_module_colon_function_form(hook_dir):
    with pytest.raises(ValueError, match="module:function.*form is deleted"):
        load_hook("tests.hook_samples:ok", hook_dir)


def test_load_hook_rejects_non_py_file(hook_dir):
    (hook_dir / "hooks.txt").write_text("x = 1\n", encoding="utf-8")
    with pytest.raises(ValueError, match="must be a .py regular file"):
        load_hook("hooks.txt:one_arg", hook_dir)


def test_load_hook_rejects_missing_file(hook_dir):
    with pytest.raises(ValueError, match="does not exist"):
        load_hook("ghost.py:one_arg", hook_dir)


def test_load_hook_rejects_missing_attribute(hook_dir):
    with pytest.raises(ValueError, match="not found in hook file"):
        load_hook("hooks.py:missing_fn", hook_dir)


def test_load_hook_rejects_non_callable(hook_dir):
    with pytest.raises(ValueError, match="is not callable"):
        load_hook("hooks.py:NOT_CALLABLE", hook_dir)


def test_load_hook_rejects_bad_format(hook_dir):
    for ref in ("no-colon", ":fn", "hooks.py:", "  :  "):
        with pytest.raises(ValueError, match="attribute-path"):
            load_hook(ref, hook_dir)


def test_load_hook_reports_module_execution_failure(tmp_path):
    (tmp_path / "broken.py").write_text("raise ImportError('boom')\n", encoding="utf-8")
    with pytest.raises(ValueError, match="cannot execute hook file"):
        load_hook("broken.py:fn", tmp_path)


def test_hook_module_names_are_hashed_per_absolute_path(tmp_path):
    """两个工程同名 hooks.py 不得互相污染 sys.modules（SPEC-SP §4.9）。"""
    for name, marker in (("a", "AAA"), ("b", "BBB")):
        project = tmp_path / name
        project.mkdir()
        (project / "hooks.py").write_text(
            f"MARKER = {marker!r}\n\n\ndef one_arg(text):\n    return [MARKER]\n",
            encoding="utf-8")
    ha = load_hook("hooks.py:one_arg", tmp_path / "a")
    hb = load_hook("hooks.py:one_arg", tmp_path / "b")
    assert ha.target("x") == ["AAA"]
    assert hb.target("x") == ["BBB"]
    assert sys.modules[_module_of(ha)] is not sys.modules[_module_of(hb)]


def _module_of(hook: ResolvedHook) -> str:
    """从冻结载体反查其模块在 sys.modules 里的注册名。"""
    for name, module in list(sys.modules.items()):
        if not name.startswith("labelkit_user_hook_"):
            continue
        if getattr(module, "one_arg", None) is hook.target:
            return name
    raise AssertionError("hook module not found in sys.modules")


def test_resolved_hook_repr_and_equality_exclude_target(hook_dir):
    left = load_hook("hooks.py:one_arg", hook_dir)
    right = load_hook("hooks.py:one_arg", hook_dir)
    assert "target" not in repr(left)
    assert repr(left.target) not in repr(left)   # callable 的 repr 绝不出现
    assert left == right            # compare=False: callable 不参与相等
    assert hash(left) == hash(right)


def test_validation_hooks_defaults_are_none():
    hooks = ValidationHooks()
    assert (hooks.output, hooks.sample, hooks.state) == 3 * (None,)


def test_check_hook_arity(hook_dir):
    assert check_hook_arity(load_hook("hooks.py:add_marker", hook_dir), 2) is None
    assert check_hook_arity(load_hook("hooks.py:one_arg", hook_dir), 1) is None
    two = load_hook("hooks.py:add_marker", hook_dir)
    assert "exactly 1 positional" in check_hook_arity(two, 1)


def test_probe_hook_passes_and_aggregates_defects(hook_dir):
    ok = load_hook("hooks.py:one_arg", hook_dir)
    assert probe_hook(ok, ("text",)) is None
    boom = load_hook("hooks.py:explodes", hook_dir)
    assert "raised RuntimeError" in probe_hook(boom, ("text",))
    bad = load_hook("hooks.py:bad_return", hook_dir)
    assert "invalid value" in probe_hook(bad, ("text",))


def test_normalize_violations():
    assert normalize_violations(None, "r") == []
    assert normalize_violations([], "r") == []
    assert normalize_violations(("a", 1), "r") == ["a", "1"]
    with pytest.raises(TypeError, match="must return list"):
        normalize_violations("nope", "r")


def _state_input() -> StateTransitionInput:
    """构造状态转换钩子测试输入。"""
    return StateTransitionInput(
        slot_key="slot-1",
        variant="minority",
        role=None,
        state_before={"nested": {"value": "original"}, "items": [1]},
        state_after={"nested": {"value": "changed"}, "items": [1, 2]},
        patch=({"op": "replace", "path": "/nested", "value": {"items": [2]}},),
    )


def test_clone_state_input_recursively_thaws_and_preserves_metadata():
    original = _state_input()
    cloned = clone_state_input(original)
    assert cloned is not original
    assert (cloned.slot_key, cloned.variant, cloned.role) == ("slot-1", "minority", None)
    assert cloned.state_before == original.state_before
    assert cloned.patch == original.patch
    assert cloned.state_before is not original.state_before
    assert cloned.patch[0] is not original.patch[0]
    assert cloned.patch[0]["value"] is not original.patch[0]["value"]


def test_state_probe_input_is_nested_and_user_data_free():
    probe = state_probe_input()
    assert isinstance(probe, StateTransitionInput)
    assert probe.slot_key == "__m1_probe__"
    assert probe.role is None
    assert probe.state_before["nested"]["values"] == (1, 2)
    assert probe.patch[0]["value"]["result"] == (3,)


def test_normalize_state_violations_is_strict():
    assert normalize_state_violations([], "r") == ()
    assert normalize_state_violations(["bad"], "r") == ("bad",)
    for invalid in (None, (), [""], [1], "bad"):
        with pytest.raises(TypeError, match="must return list"):
            normalize_state_violations(invalid, "r")


def test_repo_hook_samples_file_form_still_loads():
    """仓库样本钩子文件按文件形态可装载（供 config 侧测试共用同一文件）。"""
    hook = load_hook(f"{_REPO_HOOKS}:topic_max6", Path("/"))
    assert hook.target({"topic": "请假条"}, None) == []
