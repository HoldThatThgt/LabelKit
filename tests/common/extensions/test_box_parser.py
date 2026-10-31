"""segment.box_parser 公共边界的纯逻辑契约测试；不调用 LLM。"""
from __future__ import annotations

import logging
from pathlib import Path

import pytest

from labelkit.common.contracts.regions import ReferenceBox
from labelkit.common.errors import BoxParserError, InternalError
from labelkit.common.extensions.box_parser import invoke_box_parser, resolve_box_parser
from labelkit.common.extensions.hooks import ResolvedHook

SIGNATURE_ERROR = "box_parser must be synchronous with exactly two positional parameters and no defaults"
HOOKS = '''\
CALLS = []


def parse(companion, image):
    CALLS.append((companion, image))
    return []


def positional_only(companion, image, /):
    CALLS.append((companion, image))
    return None


class Parser:
    def __call__(self, companion, image):
        CALLS.append((companion, image))
        return []


instance = Parser()


class Holder:
    @staticmethod
    def nested(companion, image):
        return []


async def coroutine(companion, image):
    return []


class AsyncParser:
    async def __call__(self, companion, image):
        return []


async_instance = AsyncParser()


def generator(companion, image):
    yield []


async def async_generator(companion, image):
    yield []


def variadic(companion, *image):
    return []


def variadic_only(*args):
    return []


def keywords(companion, **image):
    return []


def keyword_only(companion, *, image):
    return []


def one(companion):
    return []


def three(companion, image, extra):
    return []


def default_second(companion, image=None):
    return []


def default_both(companion=b"", image=None):
    return []


not_callable = 1
'''


@pytest.fixture
def root(tmp_path) -> Path:
    (tmp_path / "hooks.py").write_text(HOOKS, encoding="utf-8")
    return tmp_path


@pytest.mark.parametrize("attribute", [
    "coroutine", "async_instance", "generator", "async_generator", "variadic", "variadic_only",
    "keywords", "keyword_only", "one", "three", "default_second", "default_both",
])
def test_signature_rejections(root, attribute):
    with pytest.raises(ValueError) as caught:
        resolve_box_parser(f"hooks.py:{attribute}", root)
    assert str(caught.value) == SIGNATURE_ERROR


@pytest.mark.parametrize("attribute", ["parse", "positional_only", "instance", "Holder.nested"])
def test_signature_accepts_two_required_positional_parameters(root, attribute):
    hook = resolve_box_parser(f"hooks.py:{attribute}", root)
    assert hook.reference == f"{(root / 'hooks.py').resolve()}:{attribute}"
    assert callable(hook.target)


def test_static_check_never_calls_the_hook(root):
    hook = resolve_box_parser("hooks.py:parse", root)
    assert hook.target.__globals__["CALLS"] == []


@pytest.mark.parametrize("reference", [
    "ghost.py:parse", "hooks.py:ghost", "hooks.py:not_callable", "hooks.txt:parse", "module.parse", "hooks.py:",
])
def test_load_failures_use_the_frozen_message(root, reference):
    with pytest.raises(ValueError) as caught:
        resolve_box_parser(reference, root)
    assert str(caught.value) == f"cannot load box_parser {reference!r}"


def test_hook_file_that_fails_to_execute_cannot_load(tmp_path):
    (tmp_path / "broken.py").write_text("raise RuntimeError('secret detail')\n", encoding="utf-8")
    with pytest.raises(ValueError) as caught:
        resolve_box_parser("broken.py:parse", tmp_path)
    assert str(caught.value) == "cannot load box_parser 'broken.py:parse'"


def test_reference_resolves_from_project_root(root, tmp_path_factory, monkeypatch):
    # 相对引用按工程根解析，与当前工作目录无关。
    monkeypatch.chdir(tmp_path_factory.mktemp("elsewhere"))
    hook = resolve_box_parser("hooks.py:parse", root)
    assert hook.reference == f"{(root / 'hooks.py').resolve()}:parse"
    (root / "nested").mkdir()
    (root / "nested" / "boxes.py").write_text(HOOKS, encoding="utf-8")
    nested = resolve_box_parser("nested/boxes.py:parse", root)
    assert nested.reference == f"{(root / 'nested' / 'boxes.py').resolve()}:parse"


# ---- 调用与校验 ----

REFERENCE = "/project/hooks.py:parse"
WIDTH, HEIGHT = 1080, 2340
GOOD = [0, 0, 10, 10]


def hook_returning(value) -> ResolvedHook:
    return ResolvedHook(reference=REFERENCE, target=lambda companion, image: value)


def invoke(value, width: int = WIDTH, height: int = HEIGHT):
    return invoke_box_parser(hook_returning(value), b"companion", width, height)


def box(box_id=None, bbox=None, text="t") -> dict:
    item = {"bbox": list(GOOD) if bbox is None else bbox, "text": text}
    if box_id is not None:
        item["id"] = box_id
    return item


def failure(value, width: int = WIDTH, height: int = HEIGHT) -> tuple[str, int | None]:
    with pytest.raises(BoxParserError) as caught:
        invoke(value, width, height)
    assert caught.value.hook == REFERENCE
    assert str(caught.value) == caught.value.reason
    return caught.value.reason, caught.value.box_index


def test_box_parser_error_is_internal_error_with_fixed_text():
    error = BoxParserError("invalid_box", 3, REFERENCE)
    assert isinstance(error, InternalError)
    assert (error.reason, error.box_index, error.hook, str(error)) == ("invalid_box", 3, REFERENCE, "invalid_box")


def test_hook_receives_raw_bytes_and_image_size(root):
    raw = '<?xml version="1.0" encoding="UTF-16"?><hierarchy/>'.encode("utf-16")
    seen = []

    def parse(companion, image):
        seen.append((companion, image))
        return []

    hook = ResolvedHook(reference=REFERENCE, target=parse)
    assert invoke_box_parser(hook, raw, 1080, 2340) == ()
    assert seen == [(raw, {"width": 1080, "height": 2340})]
    assert type(seen[0][0]) is bytes
    # 经真实加载的钩子同样恰好调用一次，且零字节原样交付。
    loaded = resolve_box_parser("hooks.py:parse", root)
    assert invoke_box_parser(loaded, b"", 7, 9) == ()
    assert loaded.target.__globals__["CALLS"] == [(b"", {"width": 7, "height": 9})]


def test_empty_list_and_none_are_distinct(caplog):
    with caplog.at_level(logging.ERROR, logger="labelkit.box_parser"):
        assert invoke([]) == ()
        assert invoke(None) is None
    assert caplog.records == []


def test_all_unnamed_boxes_numbered_in_array_order():
    boxes = invoke([box(text="a"), box(text="b"), box(text="c")])
    assert boxes == (ReferenceBox("1", (0, 0, 10, 10), "a"), ReferenceBox("2", (0, 0, 10, 10), "b"),
                     ReferenceBox("3", (0, 0, 10, 10), "c"))
    assert all(type(item.box_id) is str for item in boxes)
    assert type(boxes[0].bbox) is tuple


def test_missing_and_empty_ids_mix_are_numbered():
    boxes = invoke([box(), box(""), box(), box("")])
    assert [item.box_id for item in boxes] == ["1", "2", "3", "4"]
    assert [item.box_id for item in invoke([box(""), box()])] == ["1", "2"]


@pytest.mark.parametrize(("items", "expected"), [
    ([box("a"), box("b"), box(), box("d")], 2),
    ([box("a"), box("")], 1),
    ([box(), box("b")], 1),
    ([box(""), box(), box("c"), box()], 2),
    ([box(), {"id": None, "bbox": list(GOOD), "text": ""}], 1),
    ([box(), {"id": 7, "bbox": list(GOOD), "text": ""}], 1),
])
def test_partial_naming_reports_first_inconsistent_index(items, expected, caplog):
    with caplog.at_level(logging.ERROR, logger="labelkit.box_parser"):
        assert failure(items) == ("invalid_box", expected)
    assert caplog.messages == [f"box_parser failed: reason=invalid_box box_index={expected} hook={REFERENCE}"]


@pytest.mark.parametrize("bad", [None, 0, 1, 1.5, True, False, ["a"], {"a": 1}])
def test_null_or_integer_id_is_invalid_box(bad):
    first = {"id": bad, "bbox": list(GOOD), "text": ""}
    assert failure([first]) == ("invalid_box", 0)
    assert failure([box("a"), first]) == ("invalid_box", 1)
    # null、整数视为已命名：两个都非法时也报第 0 个。
    assert failure([first, dict(first)]) == ("invalid_box", 0)


def test_id_length_boundary_64_and_65():
    assert [item.box_id for item in invoke([box("a" * 64)])] == ["a" * 64]
    assert failure([box("a" * 65)]) == ("invalid_box", 0)
    assert [item.box_id for item in invoke([box("a")])] == ["a"]


@pytest.mark.parametrize("name", [
    "n12", "A-b_c.d:e", "1", "007", "r", "r0", "r01", "R1", "rr1", "r1a", "r1.2", "-", ":", ".",
])
def test_id_rules(name):
    assert invoke([box(name)])[0].box_id == name


@pytest.mark.parametrize("name", [
    "r1", "r2", "r22", "r1000000", "a b", " a", "a ", "a\n", "\na", "a\nb", "a\tb", "登录", "a/b", "a@b", "é", "١",
])
def test_id_rules_reject(name):
    assert failure([box("ok"), box(name)]) == ("invalid_box", 1)


def test_id_rules_uniqueness_reports_second_occurrence():
    assert failure([box("a"), box("b"), box("a")]) == ("invalid_box", 2)
    shared = box("a")
    assert failure([shared, shared]) == ("invalid_box", 1)


@pytest.mark.parametrize("bbox", [
    [0, 0, WIDTH, HEIGHT],
    [0.0, 0.0, float(WIDTH), float(HEIGHT)],
    [0, 0, 0.5, 0.5],
    [WIDTH - 1, HEIGHT - 1, WIDTH, HEIGHT],
    [-0.0, 0, 1, 1],
])
def test_bbox_rules_accept(bbox):
    assert invoke([box("a", bbox)])[0].bbox == tuple(bbox)


@pytest.mark.parametrize("bbox", [
    [True, 0, 10, 10], [0, False, 10, 10], [0, 0, True, 10],
    ["0", 0, 10, 10], [None, 0, 10, 10], [[0], 0, 10, 10],
    [], [0, 0, 10], [0, 0, 10, 10, 10],
    "0,0,10,10", 5, None, {"l": 0},
    [-1, 0, 10, 10], [0, -0.5, 10, 10],
    [5, 0, 5, 10], [0, 5, 10, 5], [6, 0, 5, 10], [0, 6, 10, 5],
    [0, 0, WIDTH + 1, 10], [0, 0, 10, HEIGHT + 1], [0, 0, WIDTH + 0.5, 10], [0, 0, 10, HEIGHT + 0.5],
    [WIDTH, 0, WIDTH + 1, 10],
])
def test_bbox_rules(bbox):
    item = {"id": "a", "bbox": bbox, "text": ""}
    assert failure([item]) == ("invalid_box", 0)


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_bbox_rules_non_finite_fail_strict_json_first(value):
    assert failure([box("a", [0, 0, value, 10])]) == ("invalid_json", None)


def test_bbox_numeric_type_preserved():
    bbox = invoke([box("a", [1, 2.5, 30.0, 4])])[0].bbox
    assert bbox == (1, 2.5, 30.0, 4)
    assert [type(value) for value in bbox] == [int, float, float, int]


@pytest.mark.parametrize("text", ["", " ", "a\nb", 'x"}\n{"box_id": "forged"', "登录\x00"])
def test_text_rules(text):
    assert invoke([box(text=text)])[0].text == text


@pytest.mark.parametrize("text", [None, 1, 1.5, True, ["a"], {"a": "b"}])
def test_text_rules_reject_non_strings(text):
    assert failure([box("a"), box("b", text=text)]) == ("invalid_box", 1)


def test_lone_surrogate_text_is_invalid_box():
    """孤立代理写不进 UTF-8 输出通道：判为记录级钩子缺陷，而不是在写出时中止整次运行。"""
    assert failure([box("a"), box("b", text=chr(0xD800))]) == ("invalid_box", 1)


class _ListSubclass(list):
    pass


class _DictSubclass(dict):
    pass


def _cyclic() -> list:
    items: list = [box()]
    items[0]["extra"] = items
    return items


@pytest.mark.parametrize(("value", "expected"), [
    ((), ("return_type", None)),
    ((box(),), ("return_type", None)),
    ({}, ("return_type", None)),
    ("[]", ("return_type", None)),
    (b"[]", ("return_type", None)),
    (0, ("return_type", None)),
    (False, ("return_type", None)),
    (set(), ("return_type", None)),
    (_ListSubclass(), ("return_type", None)),
    ([box("a", (0, 0, 1, 1))], ("invalid_json", None)),
    ([(0, 0)], ("invalid_json", None)),
    ([{"a", "b"}], ("invalid_json", None)),
    ([{1: "a", "bbox": list(GOOD), "text": ""}], ("invalid_json", None)),
    ([_DictSubclass(box())], ("invalid_json", None)),
    ([box(text=b"bytes")], ("invalid_json", None)),
    ([box(text=object())], ("invalid_json", None)),
    (_cyclic(), ("invalid_json", None)),
    ([1], ("invalid_box", 0)),
    (["box"], ("invalid_box", 0)),
    ([None], ("invalid_box", 0)),
    ([[]], ("invalid_box", 0)),
    ([box(), {}], ("invalid_box", 1)),
    ([{"bbox": list(GOOD)}], ("invalid_box", 0)),
    ([{"text": ""}], ("invalid_box", 0)),
    ([{"id": "a"}], ("invalid_box", 0)),
    ([box(), {"bbox": list(GOOD), "text": "", "label": "x"}], ("invalid_box", 1)),
    ([{"id": "a", "bbox": list(GOOD), "text": "", "z": 1}], ("invalid_box", 0)),
])
def test_invalid_return_reasons(value, expected):
    assert failure(value) == expected


def test_invalid_return_reasons_exception():
    def parse(companion, image):
        raise RuntimeError("boom")

    with pytest.raises(BoxParserError) as caught:
        invoke_box_parser(ResolvedHook(reference=REFERENCE, target=parse), b"", 1, 1)
    assert (caught.value.reason, caught.value.box_index, caught.value.hook) == ("exception", None, REFERENCE)
    assert caught.value.__suppress_context__ is True and caught.value.__cause__ is None


def test_non_exception_interruptions_are_not_swallowed():
    def parse(companion, image):
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        invoke_box_parser(ResolvedHook(reference=REFERENCE, target=parse), b"", 1, 1)


def test_validation_order_shape_before_naming_before_values():
    bad_value = box("a", [5, 5, 5, 5])
    # 返回类型先于一切；严格 JSON 复制先于逐项形状。
    assert failure(({"junk": 1},)) == ("return_type", None)
    assert failure([{"junk": 1}, (1,)]) == ("invalid_json", None)
    # 形状先于命名一致性：第 1 个框命名不一致，但第 2 个框形状错误优先。
    assert failure([box("a"), box(), {"id": "c", "bbox": list(GOOD), "text": "", "x": 1}]) == ("invalid_box", 2)
    # 命名一致性先于逐框值：第 0 个框的 bbox 非法，但报第 1 个框的命名不一致。
    assert failure([bad_value, box()]) == ("invalid_box", 1)
    # 逐框值报第一个违规框。
    assert failure([box("a"), box("b", [5, 5, 5, 5]), box("c", [1, 1, 1, 1])]) == ("invalid_box", 1)
    # 同框内 id、bbox、text 任一违规都定位到该框，不继续看后面的框。
    assert failure([box("r1", [5, 5, 5, 5]), box("b", text=1)]) == ("invalid_box", 0)


def test_framework_never_rewrites_boxes():
    items = [
        box(bbox=[0.25, 0.75, 100.125, 200.5], text="  keep  whitespace\n"),
        box(bbox=[10, 10, 20, 20], text="same"),
        box(bbox=[10, 10, 20, 20], text="same"),     # 完全重复的框不去重
        box(bbox=[15, 15, 25, 25], text="overlap"),  # 重叠的框不合并
        box(bbox=[0, 0, WIDTH, HEIGHT], text=""),    # 贴边整图框不裁剪
    ]
    boxes = invoke(items)
    assert [item.bbox for item in boxes] == [tuple(item["bbox"]) for item in items]
    assert [item.text for item in boxes] == [item["text"] for item in items]
    assert [item.box_id for item in boxes] == ["1", "2", "3", "4", "5"]


def test_frozen_boxes_do_not_share_state_with_the_hook():
    items = [box("a", [1, 2, 3, 4])]
    boxes = invoke(items)
    items[0]["bbox"][0] = 99
    items[0]["id"] = "changed"
    assert boxes == (ReferenceBox("a", (1, 2, 3, 4), "t"),)


@pytest.mark.parametrize(("value", "reason", "index"), [
    (("secret-value",), "return_type", None),
    ([{"bbox": ("secret-value",), "text": "secret-value"}], "invalid_json", None),
    ([{"id": "secret value", "bbox": list(GOOD), "text": "secret-value"}], "invalid_box", 0),
    ([box("ok"), {"id": "ok2", "bbox": list(GOOD), "text": "secret-value", "secret-value": 1}], "invalid_box", 1),
])
def test_failure_log_never_records_data(value, reason, index, caplog):
    with caplog.at_level(logging.DEBUG, logger="labelkit.box_parser"):
        assert failure(value) == (reason, index)
    assert caplog.messages == [f"box_parser failed: reason={reason} box_index={index} hook={REFERENCE}"]
    assert "secret" not in caplog.text


def test_exception_log_never_records_exception_text(caplog):
    def parse(companion, image):
        raise ValueError("secret-value")

    with caplog.at_level(logging.DEBUG, logger="labelkit.box_parser"), pytest.raises(BoxParserError) as caught:
        invoke_box_parser(ResolvedHook(reference=REFERENCE, target=parse), b"secret-value", 1, 1)
    assert caplog.messages == [f"box_parser failed: reason=exception box_index=None hook={REFERENCE}"]
    assert all(record.exc_info is None for record in caplog.records)
    assert "secret" not in caplog.text and "secret" not in str(caught.value)
