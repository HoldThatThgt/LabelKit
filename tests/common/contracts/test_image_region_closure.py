"""图片分割特性的闭合门：解析真实文件，让延期与越界直接让离线套件失败。

七类判据对应 docs/dev/SPEC-image-region-segmentation.md 的 §7、§17、§19、§20、§21 与提案
docs/dev/PROPOSAL-image-region-segmentation.md 的 §4.4、§4.13、§4.15、§5：

(a) 验收行全部有真实节点，提案的矩阵行与失败表行全部被验收行承接；
(b) 特性新建的文件里没有延期标记与空函数体；
(c) 五道真实门有测试、有真实调用、有断言，并登记在 E2E-FINDINGS；
(d) segment.enabled 只留在登记允许的位置；
(e) §4.13 每一行的文档与交付物都有具体内容；
(f) 范围围栏：字段集、子命令、Shapely 导入点、禁用词、正则、叠图只进内存、无公开几何函数；
(g) 行数与参数上限。

这里只读文件，不发请求。真实端点的运行证据不由本文件证明。
"""
from __future__ import annotations

import ast
import dataclasses
import importlib.util
import json
import os
import re
import subprocess
import sys
from functools import lru_cache
from pathlib import Path

import pytest

from labelkit.cli.parser import build_parser
from labelkit.common.config.model import ClassView, InputConfig, ResolvedPaths, SegmentConfig
from tests.cli.test_cli import EXPECTED_PRODUCTION_PY, EXPECTED_TEST_PY

ROOT = Path(__file__).resolve().parents[3]
PROPOSAL_PATH = ROOT / "docs/dev/PROPOSAL-image-region-segmentation.md"
SPEC_PATH = ROOT / "docs/dev/SPEC-image-region-segmentation.md"
CONTRACTS_PATH = ROOT / "docs/CONTRACTS.md"
FINDINGS_PATH = ROOT / "docs/dev/E2E-FINDINGS.md"
GATE_FILE = "tests/integration/test_segment_image_llm.py"
THIS_FILE = "tests/common/contracts/test_image_region_closure.py"

FunctionNode = ast.FunctionDef | ast.AsyncFunctionDef

# 这些词拼开写，本文件才能把自己也纳入扫描。
DEFERRAL_RE = re.compile("|".join((
    r"\bTO" r"DO\b", r"\bFIX" r"ME\b", r"\bXX" r"X\b", "NotImplemented" "Error", r"pytest\.sk" "ip",
    "importor" "skip", "skip" "if", r"mark\.sk" "ip", "xf" "ail", "PENDING" "-IMPL", "(?i:defer" "red)",
)))
MARK_BLOCKERS = {"sk" "ip", "skip" "if", "xf" "ail"}
CALL_BLOCKERS = {"sk" "ip", "xf" "ail", "importor" "skip"}
UNRESOLVED_SPEC_RE = re.compile("|".join(("待" "定", "TB" "D", "待" "实测", "待" "补", "待" "填", "未" "定")))
PENDING_EVIDENCE = "[PENDING" "-EVIDENCE:"


# ── 解析工具 ──────────────────────────────────────────────────────────────


def _text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _section(text: str, heading: str) -> str:
    """取一个 Markdown 章节，到下一个同级或更高级标题为止。

    @param text 文档全文
    @param heading 标题行前缀（含井号），例如 ``"### 4.15 "``
    @return 章节正文（含标题行）
    """
    level = len(heading) - len(heading.lstrip("#"))
    lines = text.split("\n")
    starts = [index for index, line in enumerate(lines) if line.startswith(heading)]
    assert len(starts) == 1, f"heading {heading!r} matched {len(starts)} lines"
    collected = [lines[starts[0]]]
    fenced = False
    for line in lines[starts[0] + 1:]:
        if line.startswith("```"):
            fenced = not fenced
        marks = len(line) - len(line.lstrip("#"))
        if not fenced and 0 < marks <= level and line[marks:marks + 1] == " ":
            break
        collected.append(line)
    return "\n".join(collected)


def _tables(section: str) -> list[list[list[str]]]:
    """取章节里的全部表格的数据行（去掉表头与分隔行）。

    @param section 章节正文
    @return 每张表一个列表，每行是去掉首尾空白的单元格列表
    """
    tables, current = [], []
    for line in [*section.split("\n"), ""]:
        if line.startswith("|"):
            current.append([cell.strip() for cell in line.strip().strip("|").split("|")])
        elif current:
            tables.append(current[2:])
            current = []
    return tables


def _ac_rows() -> dict[str, str]:
    """@return SPEC §19 的 ``AC-NN`` → 整行原文。"""
    rows: dict[str, str] = {}
    for line in _section(_text(SPEC_PATH), "## 19. ").split("\n"):
        match = re.match(r"\| (AC-\d+) \|", line)
        if match:
            assert match.group(1) not in rows, f"{match.group(1)} appears twice"
            rows[match.group(1)] = line
    return rows


def _node_ids(row: str) -> list[str]:
    """@return 一行里全部 ``tests/...py::test_x`` 节点。"""
    return re.findall(r"`(tests/[\w/.]+\.py::[\w:]+)`", row)


@lru_cache(maxsize=None)
def _parse(relative: str) -> ast.Module:
    return ast.parse(_text(ROOT / relative))


def _all_functions(tree: ast.AST) -> dict[str, list[FunctionNode]]:
    """按简单名归集一个语法树里的全部函数（含方法与嵌套函数）。"""
    functions: dict[str, list[FunctionNode]] = {}
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            functions.setdefault(node.name, []).append(node)
    return functions


def _lookup_node(tree: ast.Module, node_id: str) -> FunctionNode | None:
    """按 ``test_x`` 或 ``TestClass::test_x`` 找测试函数；只认模块级函数与模块级类的方法。"""
    parts = node_id.split("::")[1:]
    scope = tree.body
    for part in parts[:-1]:
        owner = [node for node in scope if isinstance(node, ast.ClassDef) and node.name == part]
        if not owner:
            return None
        scope = owner[0].body
    found = [node for node in scope if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
             and node.name == parts[-1]]
    return found[0] if found else None


def _mark_names(expression: ast.expr) -> set[str]:
    """取 ``pytest.mark.x``、``pytest.mark.x(...)`` 以及列表形式里的全部 mark 名。"""
    names = set()
    for node in ast.walk(expression):
        if (isinstance(node, ast.Attribute) and isinstance(node.value, ast.Attribute)
                and node.value.attr == "mark"):
            names.add(node.attr)
    return names


def _module_marks(tree: ast.Module) -> set[str]:
    marks: set[str] = set()
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(isinstance(target, ast.Name) and target.id == "pytestmark"
                                                for target in node.targets):
            marks |= _mark_names(node.value)
    return marks


def _node_marks(tree: ast.Module, node: FunctionNode) -> set[str]:
    """节点自身、外层类与模块级的全部 mark。"""
    marks = _module_marks(tree)
    for decorator in node.decorator_list:
        marks |= _mark_names(decorator)
    for owner in ast.walk(tree):
        if isinstance(owner, ast.ClassDef) and node in owner.body:
            for decorator in owner.decorator_list:
                marks |= _mark_names(decorator)
    return marks


def _called_names(node: ast.AST) -> set[str]:
    """节点内全部调用的简单名：``f()`` 与 ``x.f()`` 都记 ``f``。"""
    names = set()
    for child in ast.walk(node):
        if isinstance(child, ast.Call):
            if isinstance(child.func, ast.Name):
                names.add(child.func.id)
            elif isinstance(child.func, ast.Attribute):
                names.add(child.func.attr)
    return names


def _reachable(node: FunctionNode, functions: dict[str, list[FunctionNode]]) -> list[FunctionNode]:
    """从一个函数出发，沿同模块内按名解析的调用走到的全部函数（含自身）。"""
    seen: list[FunctionNode] = []
    pending = [node]
    while pending:
        current = pending.pop()
        if current in seen:
            continue
        seen.append(current)
        for name in _called_names(current):
            pending.extend(functions.get(name, ()))
    return seen


def _is_assertion(node: ast.AST) -> bool:
    if isinstance(node, ast.Assert):
        return True
    return (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name) and node.func.value.id == "pytest"
            and node.func.attr in ("raises", "fail"))


def _asserts(node: FunctionNode, functions: dict[str, list[FunctionNode]]) -> bool:
    """节点自身或它调用的同模块函数里有断言。"""
    return any(_is_assertion(child) for current in _reachable(node, functions) for child in ast.walk(current))


def _imported_test_helpers(tree: ast.Module) -> dict[str, list[FunctionNode]]:
    """把 ``from tests.x import helper`` 引入的测试辅助函数并入可达范围。"""
    helpers: dict[str, list[FunctionNode]] = {}
    for node in tree.body:
        if isinstance(node, ast.ImportFrom) and node.module and node.module.startswith("tests."):
            source = ROOT / (node.module.replace(".", "/") + ".py")
            if source.exists():
                imported = _all_functions(_parse(source.relative_to(ROOT).as_posix()))
                for alias in node.names:
                    if alias.name in imported:
                        helpers.setdefault(alias.asname or alias.name, []).extend(imported[alias.name])
    return helpers


def _production_files() -> list[Path]:
    return sorted((ROOT / "labelkit").rglob("*.py"))


def _inventory(heading: str) -> list[str]:
    """@return SPEC §17 某张清单表第一列的相对路径。"""
    rows = _tables(_section(_section(_text(SPEC_PATH), "## 17. "), heading))[0]
    return [re.fullmatch(r"`([^`]+)`", row[0]).group(1) for row in rows]


def _feature_production() -> list[str]:
    return _inventory("### 17.1 ")


def _feature_tests() -> list[str]:
    return _inventory("### 17.3 ")


def _feature_examples() -> list[str]:
    return sorted(path.relative_to(ROOT).as_posix() for path in (ROOT / "examples/image-segmentation").rglob("*.py"))


def _is_stub(node: FunctionNode) -> bool:
    """函数体只有文档串、``pass`` 或 ``...``。"""
    body = list(node.body)
    if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) \
            and isinstance(body[0].value.value, str):
        body = body[1:]
    return all(isinstance(stmt, ast.Pass)
               or (isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Constant)
                   and stmt.value.value is Ellipsis)
               for stmt in body)


def _stubs_outside_protocols(tree: ast.Module) -> list[str]:
    """列出 Protocol 类、抽象方法与重载声明之外的空函数体。"""
    exempt: set[ast.AST] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and any("Protocol" in ast.unparse(base) for base in node.bases):
            exempt.update(child for child in node.body if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)))
    found = []
    for name, nodes in _all_functions(tree).items():
        for node in nodes:
            decorators = {ast.unparse(decorator) for decorator in node.decorator_list}
            if node not in exempt and _is_stub(node) and not decorators & {"abstractmethod", "overload", "abc.abstractmethod", "typing.overload"}:
                found.append(f"{name}:{node.lineno}")
    return found


def _param_count(node: FunctionNode) -> int:
    """形参个数，不计 ``self`` 与 ``cls``；``*args`` 与 ``**kwargs`` 各计一个。"""
    arguments = node.args
    names = [arg.arg for arg in (*arguments.posonlyargs, *arguments.args, *arguments.kwonlyargs)]
    if names and names[0] in ("self", "cls"):
        names = names[1:]
    return len(names) + (arguments.vararg is not None) + (arguments.kwarg is not None)


def _innermost_symbol(tree: ast.Module, line: int) -> str | None:
    """覆盖该行的最内层函数或类名；模块级文本返回 None。"""
    best, span = None, None
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            if node.lineno <= line <= node.end_lineno and (span is None or node.end_lineno - node.lineno < span):
                best, span = node.name, node.end_lineno - node.lineno
    return best


# ── (a) 验收 ──────────────────────────────────────────────────────────────

PUBLISHED_AC = {f"AC-{number:02d}" for number in (*range(1, 81), *range(101, 116), *range(120, 147))}

# 提案行第一列 → (承接它的验收行, 同时出现在提案行与验收行里的判别词)。
SECTION5_ROWS = {
    "纯图片，无控件树文件": ("AC-01", "纯图片"),
    "控件树为空或只有窗口外壳": ("AC-02", "embedding"),
    "图片与控件树对齐": ("AC-03", "source_box_ids"),
    "伴随数据不是控件树（本提案新增）": ("AC-04", "companion_invalid"),
    "只按参考框标注（本提案新增，D15）": ("AC-05", "missing_pair"),
    "两张独立图片": ("AC-06", "session_attempt"),
    "多边形含孔洞或多部件": ("AC-07", "_meta.regions"),
    "模型请求缩放图片": ("AC-08", "投影公式"),
    "修改 annotate 的用户 prompt": ("AC-09", "两套指令"),
    "候选为空": ("AC-10", "RegionSet(regions=())"),
    "几何非法或引用不存在": ("AC-11", "未闭合"),
    "annotate 发生修复或复核返工": ("AC-12", "verify 修复"),
    "用户选择矩形输出": ("AC-13", 'meta_mode = "none"'),
    "开启图片分割": ("AC-14", "classification"),
    "关闭图片分割": ("AC-15", "逐字节不变"),
    "图片方向（本提案新增，D4）": ("AC-16", "Orientation = 6"),
    "叠图样式（本提案新增，D12）": ("AC-17", "segment-image-overlay-ablation"),
    "框名（本提案新增，D13）": ("AC-18", "box_index"),
    "图片模式发现与计数（本提案新增）": ("AC-19", "index_conflict"),
}
FAILURE_ROWS = {
    "`box_parser` 钩子返回 `None`（伴随数据无法解析）": ("AC-20", "companion_invalid"),
    "`box_parser` 钩子抛异常、返回值不合 §4.15 契约": ("AC-21", "internal_error"),
    "首轮预检超限或服务端报超限": ("AC-22", "context_overflow"),
    "Schema 或几何违规修复穷尽；修复轮溢出（现行归因，见 D11）": ("AC-23", "segmentation_invalid"),
    "输出截断": ("AC-24", "output_truncated"),
    "provider 错误": ("AC-25", "provider_fatal"),
    "校验函数异常或非法返回（`post_validator_exception` / `post_validator_invalid`）": ("AC-26", "internal_error"),
    "摄取之后图片不可读（文件被改动或删除、像素数据截断）": ("AC-27", "image_decode_error"),
    "模型返回空列表，或（D15 路径）钩子对一份存在的伴随文件返回 `[]`": ("AC-28", "RegionSet(regions=())"),
}
HOOK_ROWS = {
    "`id`": ("AC-32", "r<正整数>"),
    "`bbox`": ("AC-33", "NaN"),
    "`text`": ("AC-34", "换行"),
    "钩子返回 `list`，每个框都合法": ("AC-38", "RegionSet.boxes"),
    "钩子返回 `None`": ("AC-39", "box_parser rejected companion"),
    "钩子抛异常、返回既非 `list` 也非 `None`、非严格 JSON、任一框违反上表": ("AC-40", "BoxParserError"),
    "没有伴随文件，写了 `instruction`": ("AC-41", "不调用钩子"),
    "没有伴随文件，没写 `instruction`（D15）": ("AC-42", "不产生 Record"),
    "零字节伴随文件": ("AC-43", 'b""'),
    "有 `instruction`，无 `box_parser`": ("AC-45", "只依据图片"),
    "有 `instruction`，有 `box_parser`": ("AC-46", "[参考框]"),
    "无 `instruction`，有 `box_parser`": ("AC-47", "矩形候选"),
    "两者都没有": ("AC-48", "CONFIG_ERROR"),
}


def _assert_rows_carried(rows: list[list[str]], expected: dict[str, tuple[str, str]]) -> None:
    """提案行与承接表一一对应；承接行还在、有节点，判别词在两边都出现。"""
    acceptance = _ac_rows()
    assert sorted(row[0] for row in rows) == sorted(expected)
    for row in rows:
        ac_id, token = expected[row[0]]
        assert ac_id in acceptance, f"proposal row {row[0]!r}: {ac_id} is gone from SPEC §19"
        assert _node_ids(acceptance[ac_id]), f"{ac_id} names no test node"
        assert token in " | ".join(row), f"proposal row {row[0]!r} no longer says {token!r}"
        assert token in acceptance[ac_id], f"{ac_id} no longer says {token!r}"


def test_ac_ids_are_exactly_the_published_set():
    assert set(_ac_rows()) == PUBLISHED_AC


def test_every_ac_row_names_existing_unmarked_nodes():
    problems = []
    trees: dict[str, ast.Module] = {}
    for ac_id, row in _ac_rows().items():
        nodes = _node_ids(row)
        if not nodes:
            problems.append(f"{ac_id}: names no test node")
        for node_id in nodes:
            relative = node_id.split("::")[0]
            if not (ROOT / relative).is_file():
                problems.append(f"{ac_id}: {relative} does not exist")
                continue
            tree = trees.setdefault(relative, _parse(relative))
            node = _lookup_node(tree, node_id)
            if node is None:
                problems.append(f"{ac_id}: {node_id} does not exist")
                continue
            blocked = _node_marks(tree, node) & MARK_BLOCKERS
            if blocked:
                problems.append(f"{ac_id}: {node_id} carries {sorted(blocked)}")
            direct = {child.func.attr for child in ast.walk(node)
                      if isinstance(child, ast.Call) and isinstance(child.func, ast.Attribute)
                      and isinstance(child.func.value, ast.Name) and child.func.value.id == "pytest"}
            if direct & CALL_BLOCKERS:
                problems.append(f"{ac_id}: {node_id} calls pytest.{sorted(direct & CALL_BLOCKERS)}")
            if "integration" in _node_marks(tree, node):
                problems.append(f"{ac_id}: {node_id} never runs offline")
    assert problems == []


def test_every_named_node_asserts_behaviour():
    """节点存在还不够：它自己或它调用的辅助函数里必须有断言。"""
    problems = []
    trees: dict[str, tuple[ast.Module, dict[str, list[FunctionNode]]]] = {}
    for ac_id, row in _ac_rows().items():
        for node_id in _node_ids(row):
            relative = node_id.split("::")[0]
            if relative not in trees:
                tree = _parse(relative)
                trees[relative] = (tree, {**_imported_test_helpers(tree), **_all_functions(tree)})
            tree, functions = trees[relative]
            node = _lookup_node(tree, node_id)
            if node is not None and not _asserts(node, functions):
                problems.append(f"{ac_id}: {node_id} asserts nothing")
    assert problems == []


def test_proposal_section5_matrix_is_carried():
    matrix = _tables(_section(_text(PROPOSAL_PATH), "## 5. "))[0]
    _assert_rows_carried(matrix, SECTION5_ROWS)


def test_proposal_section5_evidence_column_is_carried():
    """提案第四列要求的真实门，验收行的证据列都要写。"""
    acceptance = _ac_rows()
    matrix = _tables(_section(_text(PROPOSAL_PATH), "## 5. "))[0]
    required = None
    for row in matrix:
        evidence = row[3]
        marker = re.search(r"PENDING-EVIDENCE:([\w-]+)", evidence)
        if marker:
            required = marker.group(1)
        elif "第 2 道门" in evidence:
            required = "segment-image-zai"
        elif evidence != "同上":
            required = None
        if required is not None:
            ac_id, _token = SECTION5_ROWS[row[0]]
            assert required in acceptance[ac_id].rsplit("|", 2)[1], f"{ac_id} dropped gate {required}"


def test_proposal_failure_table_is_carried():
    table = _tables(_section(_text(PROPOSAL_PATH), "### 4.4 "))[-1]
    _assert_rows_carried(table, FAILURE_ROWS)


def test_proposal_hook_tables_are_carried():
    tables = _tables(_section(_text(PROPOSAL_PATH), "### 4.15 "))
    assert len(tables) == 3
    _assert_rows_carried([row for table in tables for row in table], HOOK_ROWS)


# ── (b) 延期标记 ──────────────────────────────────────────────────────────


def test_no_deferral_markers_in_feature_files():
    problems = []
    for relative in (*_feature_production(), *_feature_tests(), *_feature_examples()):
        for number, line in enumerate(_text(ROOT / relative).split("\n"), start=1):
            if DEFERRAL_RE.search(line):
                problems.append(f"{relative}:{number}: {line.strip()[:100]}")
    assert problems == []


def test_no_stub_bodies_in_feature_files():
    problems = {}
    for relative in (*_feature_production(), *_feature_tests(), *_feature_examples()):
        stubs = _stubs_outside_protocols(_parse(relative))
        if stubs:
            problems[relative] = stubs
    assert problems == {}


def test_production_has_no_deferral_or_evidence_markers():
    """labelkit/ 全部文本都不留延期词与证据标记；未实现异常按语法树只禁抛出。"""
    allowed_catch_only = "NotImplemented" "Error"
    problems = []
    for path in sorted((ROOT / "labelkit").rglob("*")):
        if not path.is_file() or path.suffix not in (".py", ".toml", ".md", ".json", ".txt"):
            continue
        relative = path.relative_to(ROOT).as_posix()
        for number, line in enumerate(_text(path).split("\n"), start=1):
            for hit in DEFERRAL_RE.finditer(line):
                if hit.group(0) != allowed_catch_only:
                    problems.append(f"{relative}:{number}: {hit.group(0)}")
            if PENDING_EVIDENCE in line:
                problems.append(f"{relative}:{number}: evidence marker")
        if path.suffix == ".py":
            for node in ast.walk(ast.parse(_text(path))):
                if isinstance(node, ast.Raise) and node.exc is not None and allowed_catch_only in ast.unparse(node.exc):
                    problems.append(f"{relative}:{node.lineno}: raises {allowed_catch_only}")
    assert problems == []


def test_spec_has_no_unresolved_markers():
    hits = [f"{number}: {line[:80]}" for number, line in enumerate(_text(SPEC_PATH).split("\n"), start=1)
            if UNRESOLVED_SPEC_RE.search(line) or "PENDING" "-IMPL" in line]
    assert hits == []


# ── (c) 真实门证据 ────────────────────────────────────────────────────────


def _gates() -> dict[str, list[str]]:
    """@return SPEC §20.1 的门名 → 测试节点。"""
    rows = _tables(_section(_text(SPEC_PATH), "### 20.1 "))[0]
    gates = {}
    for row in rows:
        marker = re.fullmatch(r"`\[PENDING-EVIDENCE:([\w-]+)\]`", row[1]).group(1)
        gates[marker] = _node_ids(row[3])
    return gates


GATE_NAMES = {"zai-pixel-perception", "segment-image-zai", "segment-image-overlay-ablation",
              "segment-image-quality", "segment-image-annotate-quality"}
REKEY_GATE_NAMES = {f"segment-mode-rekey-{name}"
                    for name in ("stream-llm", "stitch-llm", "frame-llm", "project-replay", "local-capacity")}
# 走到这些入口才算发出真实请求：完整装配、真实引擎，或真实运行时上的真实阶段。
REAL_CALL_NAMES = {"execute_run", "complete_validated", "complete_post_validated"}


def test_gates_are_exactly_the_five_named_gates():
    gates = _gates()
    assert set(gates) == GATE_NAMES
    assert all(gates.values())


def test_every_gate_is_recorded_in_findings():
    findings = _text(FINDINGS_PATH)
    for gate, nodes in _gates().items():
        assert gate in findings, f"gate {gate} missing from E2E-FINDINGS.md"
        for node_id in nodes:
            assert node_id.split("::")[1] in findings, f"{node_id} missing from E2E-FINDINGS.md"
    for gate in REKEY_GATE_NAMES:
        assert gate in findings, f"gate {gate} missing from E2E-FINDINGS.md"


def test_gate_nodes_are_collected_with_integration_and_zai_marks():
    done = subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "-q", "-m", "integration and zai",
         "-p", "no:cacheprovider", GATE_FILE],
        cwd=ROOT, env=dict(os.environ, PYTHONUTF8="1"), capture_output=True, text=True, encoding="utf-8", timeout=300)
    assert done.returncode == 0, done.stdout + done.stderr
    collected = {line.strip() for line in done.stdout.splitlines() if "::" in line}
    for gate, nodes in _gates().items():
        for node_id in nodes:
            assert node_id in collected, f"gate {gate}: {node_id} is not collected"


def test_gate_bodies_make_real_calls_and_assert():
    tree = _parse(GATE_FILE)
    functions = _all_functions(tree)
    assert {"integration", "zai"} <= _module_marks(tree)
    for gate, nodes in _gates().items():
        for node_id in nodes:
            node = _lookup_node(tree, node_id)
            assert node is not None, f"gate {gate}: {node_id} does not exist"
            assert not _node_marks(tree, node) & MARK_BLOCKERS, f"gate {gate}: {node_id} is marked away"
            reached = _reachable(node, functions)
            called = set().union(*(_called_names(current) for current in reached))
            assert called & REAL_CALL_NAMES or {"run_stage", "aclose"} <= called, f"{node_id} makes no real call"
            assert any(_is_assertion(child) for child in ast.walk(node)), f"{node_id} has no assert of its own"
            literal_markers = {child.value for current in reached for child in ast.walk(current)
                               if isinstance(child, ast.Constant) and isinstance(child.value, str)}
            assert gate in literal_markers, f"{node_id} does not record evidence under {gate}"


def test_gate_file_has_no_stand_ins():
    """请求只经真实 LLMClient：记录器必须转交父类，文件里没有替身传输或替换后的 complete。"""
    source = _text(ROOT / GATE_FILE)
    tree = ast.parse(source)
    assert "await super().complete(profile, sent, response_schema)" in source
    for banned in ("MockTransport", "respx", "http.server", "HTTPServer", "AsyncMock", "MagicMock", "unittest.mock"):
        assert banned not in source
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "setattr":
            targets = [arg.value for arg in node.args if isinstance(arg, ast.Constant)]
            assert targets and set(targets) <= {"LLMClient", "setup_logging"}, ast.unparse(node)


# ── (d) 重键 ──────────────────────────────────────────────────────────────


def _rekey_rows() -> list[dict]:
    """@return SPEC §7.2 全部登记行。"""
    rows: list[dict] = []
    for row in _tables(_section(_text(SPEC_PATH), "### 7.2 "))[0]:
        path = re.fullmatch(r"`([^`:]+):\d+`", row[1]).group(1)
        symbol = row[2].strip("`").split(".")[-1]
        predicate = rows[-1]["predicate"] if row[4] == "同上" else row[4]
        rows.append({"number": int(row[0]), "path": path, "symbol": symbol, "class": int(row[3]), "predicate": predicate})
    return rows


def _switch_row_numbers() -> set[int]:
    """@return SPEC §19.6 允许残留开关读取的 §7.2 行号。"""
    sentence = re.search(r"§7\.2 第 ([0-9、]+) 行的符号内", _section(_text(SPEC_PATH), "### 19.6 "))
    return {int(number) for number in sentence.group(1).split("、")}


def _unchanged_text_symbols() -> set[tuple[str, str]]:
    """@return SPEC §7.4 里保持开关文本的 (文件, 符号)。

    处置以「改」开头的行已改写为 sequence_resolved，不能再出现开关；「同上」沿用上一行。
    只有 StreamConfig 例外：改写后的文档串仍保留一句只说开关关闭的告警条件。
    """
    keep = False
    symbols = {("labelkit/common/config/model.py", "StreamConfig")}
    for row in _tables(_section(_text(SPEC_PATH), "### 7.4 "))[0]:
        if row[2] != "同上":
            keep = not row[2].startswith("改")
        if keep:
            path = re.fullmatch(r"`([^`:]+):\d+`", row[0]).group(1)
            symbols.add((path, re.search(r"`([^`]+)`", row[1]).group(1).split(".")[0]))
    return symbols


def _allowed_switch_symbols() -> set[tuple[str, str]]:
    numbers = _switch_row_numbers()
    allowed = {(row["path"], row["symbol"]) for row in _rekey_rows() if row["number"] in numbers}
    allowed |= _unchanged_text_symbols()
    allowed |= {("labelkit/common/config/_segment_image.py", "image_mode"),
                ("labelkit/common/config/_segment_image.py", "check_segment_image")}
    return allowed


@lru_cache(maxsize=None)
def _switch_sites() -> list[tuple[str, str | None, int]]:
    """搜索 labelkit/ 的开关读取：文本 ``segment.enabled``，以及 ``seg = x.segment`` 别名上的 ``.enabled``。"""
    sites = []
    for path in _production_files():
        relative = path.relative_to(ROOT).as_posix()
        text = _text(path)
        tree = ast.parse(text)
        for number, line in enumerate(text.split("\n"), start=1):
            if re.search(r"segment\.enabled|\bsegment\b\s*,\s*[\"']enabled[\"']", line):
                sites.append((relative, _innermost_symbol(tree, number), number))
        aliases = {target.id for node in ast.walk(tree) if isinstance(node, ast.Assign)
                   and isinstance(node.value, ast.Attribute) and node.value.attr == "segment"
                   for target in node.targets if isinstance(target, ast.Name)}
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and node.attr == "enabled" and isinstance(node.value, ast.Name) \
                    and node.value.id in aliases and node.value.id != "segment":
                sites.append((relative, _innermost_symbol(tree, node.lineno), node.lineno))
    return sites


def _reads_attribute(path: str, symbol: str, attribute: str) -> bool:
    tree = _parse(path)
    nodes = [node for node in ast.walk(tree)
             if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and node.name == symbol]
    assert nodes, f"{path}: registered symbol {symbol} no longer exists"
    return any(isinstance(child, ast.Attribute) and child.attr == attribute
               for node in nodes for child in ast.walk(node))


def test_registry_keeps_the_52_rows_and_class_totals():
    rows = _rekey_rows()
    assert [row["number"] for row in rows] == list(range(1, 53))
    totals = {klass: sum(row["class"] == klass for row in rows) for klass in (1, 2, 3)}
    assert totals == {1: 37, 2: 4, 3: 11}


def test_segment_enabled_only_at_registered_sites():
    allowed = _allowed_switch_symbols()
    sites = _switch_sites()
    assert [site for site in sites if site[:2] not in allowed] == []
    # 类 2 的开关判断确实还在：搜索不是对空集通过。
    found = {site[:2] for site in sites}
    for row in _rekey_rows():
        if row["class"] == 2:
            assert (row["path"], row["symbol"]) in found, row


def test_every_class1_site_uses_sequence_resolved():
    """类 1 改读 sequence_resolved；第 12、13、15、16 行是 §7.3 的前置条件，只报开关关闭的旧文本。"""
    switch_rows = _switch_row_numbers()
    allowed = _allowed_switch_symbols()
    switch_only = set()
    for row in _rekey_rows():
        if row["class"] != 1:
            continue
        assert "sequence_resolved" in row["predicate"], row
        if row["number"] in switch_rows and row["number"] != 14:
            switch_only.add(row["number"])
            continue
        assert _reads_attribute(row["path"], row["symbol"], "sequence_resolved"), row
        if (row["path"], row["symbol"]) not in allowed:
            sites = [site for site in _switch_sites() if site[:2] == (row["path"], row["symbol"])]
            assert sites == [], row
    assert switch_only == {12, 13, 15, 16}
    # 这四行的「语义 not sequence_resolved」由这些行为测试守住：图片模式只报 rule 76，开关关闭才报旧文本。
    rule76 = _ac_rows()["AC-63"]
    assert "test_rule76_sequence_stages_rejected" in rule76 and "test_rule76_reports_each_error_once" in rule76
    config_tests = _all_functions(_parse("tests/common/config/test_segment_mode_config.py"))
    assert "test_preconditions_keep_old_text_when_switch_is_off" in config_tests


def test_class2_and_class3_sites_read_their_registered_predicate():
    for row in _rekey_rows():
        if row["class"] == 1:
            continue
        named = re.findall(r"llm_referenced|sequence_resolved|enabled", row["predicate"])
        # 第 17 行写「§6.1 按模式求值的公式」，公式的引用部分就是 llm_referenced。
        attribute = named[0] if named else "llm_referenced"
        assert _reads_attribute(row["path"], row["symbol"], attribute), row


# ── (e) 文档闭合 ──────────────────────────────────────────────────────────

CONTRACTS_REVISION = "## Image region segmentation contract"
CONTRACTS_SECTIONS = {
    "## Process sequence context capacity contract": ("sequence_resolved",),
    "## 1. Package layout": ("regions.py", "post_validation.py", "box_parser.py", "ingest_image.py",
                             "segment_image.py", "segment_geometry.py", "_segment_image.py"),
    "## 2. Architecture recap": ("sequence_resolved",),
    "## 3. `labelkit/common/contracts/types.py`": ("scaled_size", "regions: RegionSet | None", "companion: bytes | None",
                                                   "RegionOverlayRef", "sha256(companion_bytes + image_bytes).hexdigest()[:16]"),
    "### 3.1 `labelkit/common/contracts/regions.py`": ("class ReferenceBox", "class RegionPolygon", "class CandidateRegion",
                                                       "class RegionSet", "class RegionOverlayRef"),
    "### 6.1 `labelkit/common/config/model.py`": ("sequence_resolved", "resolved_box_parser", "companion_suffix",
                                                  "llm_referenced"),
    "### 6.3 Validation rules": ("required when segment.enabled = true", "x-labelkit-region-ref", "default rubric",
                                 "meta_mode"),
    "### 6.4 User hook surfaces": ("segment.box_parser", "BoxParserError", "invalid_box", "return_type", "invalid_json"),
    "### 7.1 M2": ("ignored_files", "companion_suffix", "index_conflict"),
    "### 7.2 M3": ("labelkit:image", '"both"', "semantic"),
    "### 7.4 M5": ("regions", "AnnotatePromptOptions", "annotation_record_context"),
    "### 7.6 M7": ("VerifyPromptOptions", "regions", "M14", "annotate_record_leaf"),
    "### 7.7 M8": ("region_ids", "region_ref_paths", "complete_post_validated", "proof", "ValidatedPostCall"),
    "### 7.8 M9": ("RegionOverlayRef", '"overlay"'),
    "### 7.9 M10": ("segment_calls", "sequence_resolved"),
    "### 7.10 M11": ("regions", "companion_path", "sequence_resolved"),
    "### 7.11 M12": ("EV_SEGMENT_REGIONS",),
    "### 7.12 CLI": ("referenced_profiles", "SegmentImageStage", "llm_referenced"),
    "### 7.13 M13": ("regions",),
    "### 7.17 Budget": ('"segment_image"',),
    "### 7.20 M14 image mode": ("SegmentImageStage", "validate_regions", "companion_invalid", "BoxParserError",
                                "CircuitBreakerTripped"),
    "### 8.1 Event catalog": ("segment.regions",),
    "### 9.1 Main output": ("regions", "image_path", "companion_path"),
    "### 9.2 Rejects channel": ("companion_invalid", "segmentation_invalid", "companion_path"),
    "### 9.3 `report.json`": ("segment_image", "ignored_files", "completion_tokens"),
    "### 10.1 M5 annotation": ("[候选区域叠图]", "[候选区域]", "[参考框]"),
    "### 10.2 M4 pairwise": ("record.ui_tree is None",),
    "### 10.3 M4 pointwise": ("[记录内容 图片]",),
    "### 10.5 M7 verify": ("[候选区域叠图]", "[标注结果]"),
    "### 10.6.1 M8 generic": ("min(max_repair_attempts, 2)", "SchemaViolation"),
    "### 10.7 Internal schemas": ("source_box_ids", "exterior", "holes"),
    "### 10.8 M13 classification": ("record.ui_tree is None",),
    "### 10.20 M14 segment image": ("[分割指令]", "0–1000", "本样本没有参考框。"),
    "## 12. Historical decision registry": ("FROZEN HERE", "v1.22"),
}
# §4.13「spec/」一行点名的章节 → 该章必须出现的内容。
SPEC_CHAPTERS = {
    "spec/00-frontmatter.md": ("v1.22",),
    "spec/10-ch1-overview.md": ("空间定位与标注分立", "sequence_resolved"),
    "spec/20-ch2-overall-design.md": ("shapely==2.2.0", "每请求 2 张图", "sequence_resolved"),
    "spec/301-m1-config.md": ("box_parser", "companion_suffix", "x-labelkit-region-ref"),
    "spec/302-m2-ingest.md": ("companion_suffix", "ignored_files"),
    "spec/303-m3-dedup.md": ("labelkit:image", '"both"'),
    "spec/304-m4-qualityqurating.md": ("无树",),
    "spec/305-m5-annotate.md": ("[候选区域叠图]", "ContextOverflowError", "schema_violation"),
    "spec/307-m7-verify.md": ("[候选区域叠图]", "judge_error"),
    "spec/308-m8-schema-engine.md": ("proof", "x-labelkit-region-ref"),
    "spec/309-m9-llm-client.md": ("scaled_size", "RegionOverlayRef"),
    "spec/310-m10-orchestration.md": ("SegmentImageStage", "segment_calls"),
    "spec/311-m11-emitter.md": ("_meta.regions", "sequence_resolved"),
    "spec/313-m13-classify.md": ("regions", "无树"),
    "spec/314-m14-segment.md": ("sequence_resolved", "3.18"),
    "spec/315-m15-extract.md": ("sequence_resolved",),
    "spec/316-m16-stitch.md": ("sequence_resolved",),
    "spec/318-m14-segment-image.md": ("SegmentImageStage", "RegionOverlayRef", "segment_geometry"),
    "spec/40-ch4-data-structures.md": ("RegionSet", "PipelineItem", "regions"),
    "spec/50-ch5-config-spec.md": ("box_parser", "companion_suffix", "instruction"),
    "spec/60-ch6-io-formats.md": ("regions", "segment_image"),
    "spec/70-ch7-logging.md": ("segment.regions",),
    "spec/80-ch8-nongoals-roadmap.md": ("make_valid", "A1", "A3"),
    "spec/85-ch9-references.md": ("2310.11441", "2408.00203"),
    "spec/90-appendix-a-rubrics.md": ("sequence_resolved",),
}
DEV_SPECS = {
    "docs/dev/SPEC-annotation-postprocessing.md": ("x-labelkit-region-ref", "sample_id", "complete_finalized"),
    "docs/dev/SPEC-context-budget.md": ("vision_resolved", "图片模式", "sequence_resolved"),
    "docs/dev/SPEC-sequence-context-capacity.md": ("sequence_resolved",),
    "docs/dev/SPEC-sequence-generation-redesign.md": ("ValidatedPostCall", "proof"),
    "docs/dev/E2E-FINDINGS.md": tuple(sorted(GATE_NAMES | REKEY_GATE_NAMES)),
}
MANUAL_CHAPTERS = {
    "docs/manual/05-data-preparation.md": ("companion_suffix", "EXIF"),
    "docs/manual/07-project-toml.md": ('mode = "image"', "rubric"),
    "docs/manual/08-outputs.md": ("_meta.regions", "segment_image", "ignored_files"),
    "docs/manual/11-annotate.md": ("[候选区域叠图]", "x-labelkit-region-ref"),
    "docs/manual/13-verify.md": ("[候选区域叠图]", "judge_error"),
    "docs/manual/14-schema-engine.md": ("box_parser", "x-labelkit-region-ref"),
    "docs/manual/25-stream.md": ('mode = "sequence"',),
    "docs/manual/26-thread.md": ('mode = "sequence"',),
    "docs/manual/28-image-segmentation.md": ("box_parser", "companion_invalid", "没写 `instruction`"),
    "docs/manual/README.md": ("28-image-segmentation.md",),
}
# §4.13 其余各行的第一列 → {文件: 该文件里必须出现的内容}。
ROW_TOKENS = {
    "`labelkit/common/config/`（model、_sections、_constraints、_collect；图片模式规则建议放新模块）": {
        "labelkit/common/config/model.py": ("sequence_resolved", "resolved_box_parser", "companion_suffix"),
        "labelkit/common/config/_sections.py": ("box_parser", "companion_suffix"),
        "labelkit/common/config/_constraints.py": ("sequence_resolved", "llm_referenced"),
        "labelkit/common/config/_segment_image.py": ("required when segment.enabled = true", "meta_mode"),
    },
    "`labelkit/common/contracts/regions.py`（新）、`types.py`、`generation.py`": {
        "labelkit/common/contracts/regions.py": ("class ReferenceBox", "class RegionOverlayRef", "def render_reference_boxes"),
        "labelkit/common/contracts/types.py": ("def scaled_size", "regions:", "companion:"),
        "labelkit/common/contracts/post_validation.py": ("class ValidatedPostCall", "proof"),
    },
    "`labelkit/common/extensions/box_parser.py`（新）、`hooks.py`": {
        "labelkit/common/extensions/box_parser.py": ("def resolve_box_parser", "def invoke_box_parser", "BoxParserError"),
        "labelkit/common/extensions/hooks.py": ("box_parser",),
    },
    "`labelkit/common/inference/schema_engine.py`、`llm_client.py`、`credentials.py`、`budget.py`": {
        "labelkit/common/inference/schema_engine.py": ("region_ids", "proof", "def segment_image_schema"),
        "labelkit/common/inference/llm_client.py": ("RegionOverlayRef", '"overlay"'),
        "labelkit/common/inference/credentials.py": ("llm_referenced",),
        "labelkit/common/inference/budget.py": ('"segment_image"',),
    },
    "`labelkit/common/observability/obslog.py`": {
        "labelkit/common/observability/obslog.py": ("EV_SEGMENT_REGIONS", "segment.regions"),
    },
    "`labelkit/operators/segment_image.py`、`segment_geometry.py`、`ingest_image.py`（新）": {
        "labelkit/operators/segment_image.py": ("class SegmentImageStage", "invoke_box_parser", "run_group"),
        "labelkit/operators/segment_geometry.py": ("def validate_regions", "import shapely"),
        "labelkit/operators/ingest_image.py": ("def scan_image_plan", "def image_records", "ignored_files"),
    },
    "`labelkit/operators/annotate.py`、`verify.py`、`classify.py`、`quality.py`、`dedup.py`、`emitter.py`、`extract.py`、`annotation_finalization.py`": {
        "labelkit/operators/annotate.py": ("RegionOverlayRef", "sequence_resolved", "regions"),
        "labelkit/operators/verify.py": ("RegionOverlayRef", "sequence_resolved", "regions"),
        "labelkit/operators/classify.py": ("regions", "sequence_resolved"),
        "labelkit/operators/quality.py": ("sequence_resolved", "ui_tree is None"),
        "labelkit/operators/dedup.py": ("labelkit:image",),
        "labelkit/operators/emitter.py": ("project_regions", "companion_path", "sequence_resolved"),
        "labelkit/operators/extract.py": ("sequence_resolved",),
        "labelkit/operators/annotation_finalization.py": ("regions", "sample_id"),
    },
    "`labelkit/orchestration/process_workflow.py`、`factory.py`；`labelkit/cli/console.py`": {
        "labelkit/orchestration/process_workflow.py": ("sequence_resolved", "segment_image", "ignored_files"),
        "labelkit/orchestration/factory.py": ("SegmentImageStage", 'mode == "image"'),
        "labelkit/cli/console.py": ("sequence_resolved",),
    },
    "`pyproject.toml`、`uv.lock`": {
        "pyproject.toml": ('"shapely==2.2.0"', '"Pillow>=10.1"'),
        "uv.lock": ('name = "shapely"', 'version = "2.2.0"'),
    },
    "`tests/`": {
        "tests/cli/test_cli.py": ('"tests/common/contracts/test_image_region_closure.py"', '"labelkit/operators/segment_geometry.py"',
                                  '"ingest_image"', '"segment_image"'),
        "tests/common/contracts/test_generation_contracts.py": ("ValidatedPostCall",),
        "tests/cli/goldens/dryrun-image-model.txt": ("segment_calls=2",),
        "tests/cli/goldens/dryrun-image-boxes.txt": ("segment_calls=0",),
    },
    "`examples/`": {
        "examples/image-segmentation/hooks.py": ("def uiautomator_to_boxes", "def accessibility_to_boxes", "def ocr_to_boxes",
                                                 "def keep_polygons", "def to_bounding_boxes"),
        "examples/image-segmentation/project.toml": ('mode = "image"',),
        "examples/image-segmentation/project-boxes.toml": ("box_parser",),
        "examples/image-segmentation/project-uiautomator.toml": ("uiautomator_to_boxes",),
        "examples/image-segmentation/project-accessibility.toml": ("accessibility_to_boxes",),
        "examples/image-segmentation/project-ocr.toml": ("ocr_to_boxes",),
        "examples/image-segmentation/project-fixed.toml": ('mode = "image"',),
        "examples/stream/project.toml": ('mode = "sequence"',),
        "examples/sequence-context-capacity/prepare.py": ('mode = "sequence"',),
    },
    "`docs/design/`、`tools/design_figures/`": {
        "docs/design/labelkit-design-v1.html": ("segment 图片模式（M14）", "ValidatedPostCall", "sequence_resolved"),
        "tools/build_design_doc.py": ("318-m14-segment-image",),
        "tools/design_figures/fig-3-10.svg": ("<svg",),
    },
}
# 这些行由各自的专门测试判定，这里只核对行还在。
ROWS_WITH_OWN_TEST = {
    "`docs/dev/SPEC-image-region-segmentation.md`（新）",
    "`docs/CONTRACTS.md`",
    "`spec/`",
    "`docs/dev/` 其他规格",
    "`docs/manual/`",
    "`CLAUDE.md`、`AGENTS.md`（逐字节一致）",
}


def _missing_tokens(expected: dict[str, tuple[str, ...]]) -> list[str]:
    problems = []
    for relative, tokens in expected.items():
        path = ROOT / relative
        if not path.is_file():
            problems.append(f"{relative}: missing")
            continue
        text = _text(path)
        problems.extend(f"{relative}: lacks {token!r}" for token in tokens if token not in text)
    return problems


def test_every_413_row_has_a_closure_check():
    rows = _tables(_section(_text(PROPOSAL_PATH), "### 4.13 "))[0]
    assert {row[0] for row in rows} == set(ROW_TOKENS) | ROWS_WITH_OWN_TEST


@pytest.mark.parametrize("row", sorted(ROW_TOKENS))
def test_413_row_has_concrete_tokens(row):
    assert _missing_tokens(ROW_TOKENS[row]) == []


def test_contracts_revision_preamble():
    preamble = _section(_text(CONTRACTS_PATH), CONTRACTS_REVISION)
    for token in ("SPEC-image-region-segmentation.md", "segment.sequence_resolved", "v1.22", "D8"):
        assert token in preamble, token


def test_contracts_dependency_whitelist_names_shapely():
    head = _text(CONTRACTS_PATH).split("## 1. Package layout")[0]
    assert "shapely==2.2.0" in head and "segment_geometry.py" in head


@pytest.mark.parametrize("heading", sorted(CONTRACTS_SECTIONS))
def test_contracts_section_carries_its_tokens(heading):
    section = _section(_text(CONTRACTS_PATH), heading)
    assert [token for token in CONTRACTS_SECTIONS[heading] if token not in section] == []


def test_contracts_atomic_delivery_and_conventions_stay_free_of_overlay_files():
    """§9.4 与 §11 按 D1 不变：不得出现任何叠图或候选输出通道。"""
    text = _text(CONTRACTS_PATH)
    for heading in ("### 9.4 Atomic delivery", "## 11. Cross-cutting conventions"):
        section = _section(text, heading)
        for token in ("overlay", "RegionOverlayRef", "segment_image"):
            assert token not in section, f"{heading} mentions {token}"


def test_spec_chapters_carry_their_tokens():
    assert _missing_tokens(SPEC_CHAPTERS) == []


def test_dev_specs_carry_their_tokens():
    assert _missing_tokens(DEV_SPECS) == []


def test_manual_chapters_carry_their_tokens():
    assert _missing_tokens(MANUAL_CHAPTERS) == []


def test_agent_guides_are_recorded_as_out_of_tree():
    """CLAUDE.md 与 AGENTS.md 不在检出中，SPEC 必须如实记录这一行没有在本仓完成。"""
    spec = _text(SPEC_PATH)
    assert "`CLAUDE.md`、`AGENTS.md` 被 gitignore 且不在本检出中" in spec
    log = [row for row in _tables(_section(spec, "## 22. "))[0] if row[0] == "13"]
    assert log and "CLAUDE.md" in log[0][1]


def test_design_pdf_is_a_rebuilt_document():
    """按原始字节认：标题来自现行 frontmatter，且带 §3.18 的命名目的地；只有头尾的旧 PDF 不算重建。"""
    pdf = (ROOT / "docs/design/labelkit-design-v1.pdf").read_bytes()
    assert pdf.startswith(b"%PDF-") and pdf.rstrip().endswith(b"%%EOF")
    title = _text(ROOT / "spec/00-frontmatter.md").split("\n")[0].strip()
    assert ("FEFF" + title.encode("utf-16-be").hex().upper()).encode() in pdf, title
    assert b"/s3-18" in pdf


def test_design_html_matches_a_fresh_spec_build(tmp_path, monkeypatch):
    """现场从 spec/ 构建一份 HTML，与检入版按解码文本比（不比字节：Windows 的 write_text 写 CRLF）。"""
    spec = importlib.util.spec_from_file_location("design_doc_builder", ROOT / "tools/build_design_doc.py")
    builder = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(builder)
    monkeypatch.setattr(builder, "OUT_HTML", tmp_path / "design.html")
    monkeypatch.setattr(sys, "argv", ["build_design_doc.py"])
    builder.main()
    fresh = _text(tmp_path / "design.html").split("\n")
    checked_in = _text(ROOT / "docs/design/labelkit-design-v1.html").split("\n")
    # 全文近 80 万字符，只报前几处差异行，不让 pytest 展开整篇 diff。
    drift = [index for index, (new, old) in enumerate(zip(fresh, checked_in), start=1) if new != old]
    stale = "docs/design HTML 落后于 spec/，请用 tools/build_design_doc.py --pdf 重建"
    assert (len(fresh), drift[:5]) == (len(checked_in), []), stale


def test_module_inventory_exists_and_is_registered():
    production, tests = _feature_production(), _feature_tests()
    assert [path for path in production if not (ROOT / path).is_file()] == []
    assert [path for path in tests if not (ROOT / path).is_file()] == []
    assert set(production) <= EXPECTED_PRODUCTION_PY
    assert THIS_FILE in tests
    assert set(tests) <= EXPECTED_TEST_PY
    # SPEC-named 交付物本身也不能退化成空文件。
    for relative in (*production, *tests):
        assert any(isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) for node in _parse(relative).body), relative


def test_example_project_inventory_exists():
    root = ROOT / "examples/image-segmentation"
    for name in ("project.toml", "project-boxes.toml", "project-uiautomator.toml", "project-accessibility.toml",
                 "project-ocr.toml", "project-fixed.toml", "hooks.py", "fixtures/fixed-candidates.json", "prepare.py",
                 "README.md"):
        assert (root / name).is_file(), name
    assert any((root / "data").rglob("image_*.png"))
    assert any((root / "schemas").iterdir())


# ── (f) 范围围栏 ──────────────────────────────────────────────────────────

SEGMENT_FIELDS = ("enabled", "strategy", "llm", "window", "noise_filter", "min_len", "context", "on_error",
                  "vision_resolved", "mode", "instruction", "box_parser", "companion_suffix", "resolved_box_parser",
                  "sequence_resolved")
PATH_FIELDS = ("project", "project_root", "input", "output", "report", "rejects", "sidecar", "trace", "stream",
               "manifest", "failed_report")
CLI_COMMANDS = {"run", "validate", "rubric"}
DEPENDENCIES = {"httpx", "jsonschema", "jsonpatch", "jsonpointer", "datasketch", "Pillow", "imagehash", "json-repair",
                "numpy", "ortools", "rich", "shapely"}
FORBIDDEN_IN_PRODUCTION = ("make_valid", "buffer(0)", ".buffer(", "orient_polygons", "set_precision", "exif_transpose",
                           "ImageOps", "max_regions", "region_limit", "default:image", "default_image.toml", "vision_probe",
                           "probe_vision", "segment_anything")
GEOMETRY_PUBLIC = {"boxes_as_regions", "validate_regions"}
REGIONS_PUBLIC = {"ReferenceBox", "RegionPolygon", "CandidateRegion", "RegionSet", "RegionOverlayRef",
                  "render_reference_boxes", "render_candidates", "render_material_note", "project_regions",
                  "OVERLAY_FILL_ALPHA", "OVERLAY_OUTLINE_PX", "OVERLAY_BASE_COLOR", "OVERLAY_COLORS"}


def _public_names(tree: ast.Module) -> set[str]:
    names = set()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, ast.Assign):
            names.update(target.id for target in node.targets if isinstance(target, ast.Name))
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names.add(node.target.id)
    return {name for name in names if not name.startswith("_")}


def _imported_roots(tree: ast.AST) -> set[str]:
    roots = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            roots.add(node.module.split(".")[0])
    return roots


def test_segment_config_field_set_is_exact():
    assert tuple(field.name for field in dataclasses.fields(SegmentConfig)) == SEGMENT_FIELDS


def test_resolved_paths_field_set_is_exact():
    assert tuple(field.name for field in dataclasses.fields(ResolvedPaths)) == PATH_FIELDS


def test_no_user_key_widens_the_config_surface():
    """没有 input.ui_tree 键，也没有按类分割指令（[class.<name>.segment]）。"""
    assert "ui_tree" not in {field.name for field in dataclasses.fields(InputConfig)}
    assert "segment" not in {field.name for field in dataclasses.fields(ClassView)}
    views = _text(ROOT / "labelkit/common/config/_classviews.py")
    assert not re.search(r"""["']segment["']""", views)
    sections = _text(ROOT / "labelkit/common/config/_sections.py")
    assert not re.search(r"""["']ui_tree["']""", sections)


def test_cli_subcommands_are_exact():
    parser = build_parser()
    commands = {name for action in parser._actions if hasattr(action, "choices") and isinstance(action.choices, dict)
                for name in action.choices}
    assert commands == CLI_COMMANDS


def test_dependency_whitelist_is_exact():
    import tomllib

    declared = tomllib.loads(_text(ROOT / "pyproject.toml"))["project"]["dependencies"]
    assert {re.split(r"[=<>!~ ]", item)[0] for item in declared} == DEPENDENCIES
    assert "shapely==2.2.0" in declared and "Pillow>=10.1" in declared


def test_rubric_set_has_no_image_rubric():
    assert {path.name for path in (ROOT / "labelkit/data/rubrics").iterdir() if path.is_file()} == {
        "default_text.toml", "default_trajectory.toml", "default_ui.toml"}


def test_shapely_imported_only_by_segment_geometry():
    importers = {path.relative_to(ROOT).as_posix() for path in _production_files()
                 if "shapely" in _imported_roots(ast.parse(_text(path)))}
    assert importers == {"labelkit/operators/segment_geometry.py"}
    dynamic = [path.relative_to(ROOT).as_posix() for path in _production_files()
               if re.search(r"""import_module\(\s*["']shapely|__import__\(\s*["']shapely""", _text(path))]
    assert dynamic == []


def test_forbidden_tokens_absent_from_production():
    problems = []
    for path in sorted((ROOT / "labelkit").rglob("*")):
        if path.is_file() and path.suffix in (".py", ".toml"):
            text = _text(path)
            problems.extend(f"{path.relative_to(ROOT).as_posix()}: {token}" for token in FORBIDDEN_IN_PRODUCTION if token in text)
    assert problems == []


def test_no_regex_in_feature_modules_beyond_spec():
    """segment_image 与 ingest_image 完全不导入正则；box_parser 只留 SPEC §9.2 冻结的两条框名规则。"""
    for relative in ("labelkit/operators/segment_image.py", "labelkit/operators/ingest_image.py",
                     "labelkit/operators/segment_geometry.py"):
        assert not _imported_roots(_parse(relative)) & {"re", "regex", "fnmatch"}, relative
    tree = _parse("labelkit/common/extensions/box_parser.py")
    uses = [node for node in ast.walk(tree)
            if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id == "re"]
    assert [node.attr for node in uses] == ["compile", "compile"]
    patterns = [node.args[0].value for node in ast.walk(tree) if isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute) and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "re"]
    assert patterns == ["[A-Za-z0-9_.:-]{1,64}", "r[1-9][0-9]*"]
    rule = _section(_text(SPEC_PATH), "### 9.2 ")
    assert "`[A-Za-z0-9_.:-]`" in rule and "1–64 个字符" in rule and "`^r[1-9][0-9]*$`" in rule


def test_overlay_writes_only_to_memory():
    """regions.py 只把 PNG 存进 BytesIO；没有 open 写、临时文件或路径写入。"""
    tree = _parse("labelkit/common/contracts/regions.py")
    assert _imported_roots(tree) <= {"__future__", "base64", "io", "json", "math", "dataclasses", "fractions", "typing",
                                     "labelkit", "PIL"}
    buffers = {target.id for node in ast.walk(tree) if isinstance(node, ast.Assign)
               and isinstance(node.value, ast.Call) and ast.unparse(node.value.func) == "io.BytesIO"
               for target in node.targets if isinstance(target, ast.Name)}
    assert buffers
    saves = [node for node in ast.walk(tree) if isinstance(node, ast.Call)
             and isinstance(node.func, ast.Attribute) and node.func.attr == "save"]
    assert saves, "overlay no longer encodes through save()"
    for call in saves:
        assert isinstance(call.args[0], ast.Name) and call.args[0].id in buffers, ast.unparse(call)
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            callee = ast.unparse(node.func)
            assert not callee.endswith(("write_bytes", "write_text", "mkstemp", "NamedTemporaryFile", "mkdtemp")), callee
            assert callee != "open", "regions.py must not call builtin open"


def test_no_public_geometry_helper_is_exported():
    tree = _parse("labelkit/operators/segment_geometry.py")
    assert _public_names(tree) == GEOMETRY_PUBLIC
    regions = _parse("labelkit/common/contracts/regions.py")
    assert _public_names(regions) == REGIONS_PUBLIC
    # 只有 segment_image 能用 segment_geometry，其他算子、配置层、CLI 都不得把几何函数转手暴露。
    importers = set()
    for path in _production_files():
        for node in ast.walk(ast.parse(_text(path))):
            if isinstance(node, ast.ImportFrom) and node.module == "labelkit.operators.segment_geometry":
                importers.add(path.relative_to(ROOT).as_posix())
    assert importers == {"labelkit/operators/segment_image.py"}
    package_init = ast.parse(_text(ROOT / "labelkit/__init__.py"))
    assert _public_names(package_init) == {"TOOL_VERSION"}
    assert not re.search(r"(?m)^\s*def (?!_)\w*(?:iou|bounding|bounds|polygon_area)\w*\(",
                         "\n".join(_text(path) for path in _production_files()))


def test_feature_tests_use_no_mock_servers_or_transports():
    banned = ("MockTransport", "respx", "HTTPServer", "http.server", "pytest_httpserver", "aiohttp.test_utils")
    problems = [f"{relative}: {token}" for relative in _feature_tests() if relative != THIS_FILE
                for token in banned if token in _text(ROOT / relative)]
    assert problems == []


# ── (g) 上限 ──────────────────────────────────────────────────────────────


def test_every_production_file_is_within_2000_lines():
    over = {path.relative_to(ROOT).as_posix(): count for path in _production_files()
            if (count := len(_text(path).splitlines())) > 2000}
    assert over == {}


def test_functions_have_at_most_five_parameters():
    """全部生产函数，以及特性新建的测试文件与示例代码里的函数。"""
    relatives = [path.relative_to(ROOT).as_posix() for path in _production_files()]
    problems = []
    for relative in (*relatives, *_feature_tests(), *_feature_examples()):
        for name, nodes in _all_functions(_parse(relative)).items():
            problems.extend(f"{relative}:{node.lineno} {name}" for node in nodes if _param_count(node) > 5)
    assert problems == []


def test_feature_files_are_utf8_with_lf():
    relatives = [*_feature_production(), *_feature_tests(), *_feature_examples(),
                 "docs/dev/SPEC-image-region-segmentation.md", "docs/manual/28-image-segmentation.md",
                 "spec/318-m14-segment-image.md"]
    for relative in relatives:
        raw = (ROOT / relative).read_bytes()
        raw.decode("utf-8")
        assert b"\r" not in raw, f"{relative} has a carriage return"


# ── 手册的通用章节与合同示例（用户排查时查的是这些表，不只是第 28 章）────────
MANUAL = ROOT / "docs/manual"
ZERO_SEGMENT_IMAGE = {"calls": 0, "samples": 0, "boxes": 0, "regions": 0, "empty": 0,
                      "failed": {}, "usage": {"prompt_tokens": 0, "completion_tokens": 0}}


def _row(section: str, first_cell: str) -> list[str]:
    """取章节表格里第一格含指定文本的唯一一行。

    @param section 章节正文
    @param first_cell 第一格应含的文本
    @return 这一行的单元格
    """
    rows = [row for table in _tables(section) for row in table if first_cell in row[0]]
    assert len(rows) == 1, first_cell
    return rows[0]


def test_manual_constraint_table_states_the_image_mode_exceptions():
    """§4.5：只有写了 instruction 的图片模式放宽 annotate；时序流阶段在图片模式被拒。"""
    section = _section(_text(MANUAL / "04-concepts.md"), "## 4.5 ")
    for row in (_row(section, "**至少开一个**"), _row(section, "`segment` 开 ⇒")):
        for token in ('segment.mode = "image"', "`instruction`", "`box_parser`", "28.3"):
            assert token in " ".join(row), (row[0], token)
    rejected = " ".join(_row(section, '`segment.mode = "image"` ⇒'))
    for token in ("`stitch`", "`extract`", "`frame.classify`", "`frame.annotate`", "`[stream]`"):
        assert token in rejected, token


def test_manual_trace_table_has_the_segment_regions_row():
    """§16.2：紧跟 segment.boundary，写明通道、六个键、两条不变量和不带的内容。"""
    rows = _tables(_section(_text(MANUAL / "16-observability.md"), "## 16.2 "))[0]
    names = [row[0] for row in rows]
    assert names.index("`segment.regions`") == names.index("`segment.boundary`") + 1
    when, payload = rows[names.index("`segment.regions`")][1:]
    for token in ("仅 trace", '`"segment"`', "只用参考框", "v1.22", "第 28 章"):
        assert token in when, token
    for token in ("regions", "reused", "drawn", "width", "height", "attempts", "regions = reused + drawn",
                  "attempts = 0", "几何", "框名"):
        assert token in payload, token


def test_manual_call_ledger_counts_image_mode_segment_calls():
    """§17.1：每样本一次初始请求，不含修复；只有 box_parser 时为零。"""
    calls = " ".join(next(row for row in _tables(_section(_text(MANUAL / "17-tuning.md"), "## 17.1 "))[0]
                          if row[0] == "segment")[1:])
    for token in ("图片模式", "`instruction`", "每个样本", "不含修复", "`box_parser`", "第 28 章"):
        assert token in calls, token


def test_manual_error_table_covers_image_mode_codes():
    """§18.1 自称全表：新码有行，两个旧码写出图片模式的发出方与结局。"""
    section = _section(_text(MANUAL / "18-troubleshooting.md"), "## 18.1 ")
    emitter, meaning = _row(section, "`companion_invalid`")[1:]
    assert "segment" in emitter
    for token in ("box_parser rejected companion", "rejects", "不调用模型", "`bad_input`", "except: return []",
                  "14.8", "28.8"):
        assert token in meaning, token
    emitter, meaning = _row(section, "`image_decode_error`")[1:]
    assert "segment" in emitter and "图片模式" in meaning
    stream, image = _row(section, "`segmentation_invalid`")[2].split("**图片模式")
    assert 'on_error="keep"' in stream and 'on_error="keep"' not in image
    for token in ("记录级", "`on_error`", "`window`", "配置错误", "rejects", "修复轮超预算", "28.8"):
        assert token in image, token


@pytest.mark.parametrize("relative", ["docs/dev/SPEC-image-region-segmentation.md", "docs/CONTRACTS.md",
                                      "spec/60-ch6-io-formats.md"])
def test_report_example_is_the_report_of_a_run_without_failures(relative):
    """report.segment_image 的形状示例就是零样本运行真实写出的块，failed 不虚构计数为 0 的键。"""
    match = re.search(r'"segment_image":\s*\{\s*"calls".*?"usage":\s*\{[^}]*\}\s*\}', _text(ROOT / relative), re.DOTALL)
    assert match, relative
    block = re.sub(r"^\s*//", "", match.group(0), flags=re.MULTILINE)
    assert json.loads("{" + block + "}")["segment_image"] == ZERO_SEGMENT_IMAGE


def test_manual_grid_figures_are_resolution_not_accuracy():
    """width / 1000 是网格能表示的最细单位，不是模型画得有多准；两处都要这样写。"""
    chapter = _section(_text(MANUAL / "28-image-segmentation.md"), "## 28.5 ")
    outputs = _section(_text(MANUAL / "08-outputs.md"), "## 8.2 ")
    for text in (chapter, outputs):
        assert "x 精度是" not in text and "y 精度是" not in text
        assert "`width / 1000`" in text and "不是模型" in text
    assert "28.5" in outputs and "唯一的几何证据" not in outputs


def test_manual_carries_the_first_run_grid_warning():
    """横屏 y 轴的警告带上数字和全部限定：一次首轮、一个模型、少量合成图、原因未证实。"""
    chapter = _text(MANUAL / "28-image-segmentation.md")
    grid = _section(chapter, "## 28.5 ")
    for token in ("glm-5.2", "一次首轮", "合成图片", "横屏", "900 × 600", "3/3 → 0/3", "2/3 → 0/3", "0/3 → 2/3",
                  "0/2 → 2/2", "2601 × 1873", "0/3", "0.99", "0.61", "0.74", "没有保存", "原因没有证实", "竖屏",
                  "0/3 → 3/3", "0.984", "`_meta.regions`"):
        assert token in grid, token
    assert "只能靠真实端点确认" not in grid
    assert "尚未运行" not in chapter.split("\n")[5]
    assert "网格" in _section(chapter, "## 28.11 ") and "28.5" in _section(chapter, "## 28.11 ")
    checks = [line for line in _section(chapter, "## 28.12 ").split("\n") if "`_meta.regions`" in line and "原图" in line]
    assert len(checks) == 1 and "28.5" in checks[0]
