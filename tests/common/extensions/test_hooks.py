"""Offline unit tests for labelkit/common/extensions/hooks.py (v1.5 plan A). Pure logic — no LLM."""
from __future__ import annotations

import pytest

from labelkit.common.extensions.hooks import normalize_violations, resolve_hook


def test_resolve_hook_happy_path():
    fn = resolve_hook("tests.hook_samples:topic_max6")
    assert fn({"topic": "这是一个很长很长的主题"}, None)
    assert fn({"topic": "请假条"}, None) == []


def test_resolve_hook_bad_format():
    for ref in ("no-colon", ":fn", "mod:", "  :  "):
        with pytest.raises(ValueError, match="module:function"):
            resolve_hook(ref)


def test_resolve_hook_import_and_attr_errors():
    with pytest.raises(ValueError, match="无法导入模块"):
        resolve_hook("no_such_module_xyz:fn")
    with pytest.raises(ValueError, match="找不到"):
        resolve_hook("tests.hook_samples:missing_fn")


def test_resolve_hook_not_callable():
    with pytest.raises(ValueError, match="不是可调用对象"):
        resolve_hook("tests.hook_samples:NOT_CALLABLE")


def test_normalize_violations():
    assert normalize_violations(None, "r") == []
    assert normalize_violations([], "r") == []
    assert normalize_violations(("a", 1), "r") == ["a", "1"]
    with pytest.raises(TypeError, match="应返回 list"):
        normalize_violations("nope", "r")
