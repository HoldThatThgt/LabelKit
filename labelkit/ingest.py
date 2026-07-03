"""M2 — data ingest (spec 3.2, 6.1/6.2; CONTRACTS.md §7.1).

Materializes ``run.input`` into a lazy ``Record`` iterator:

- text modality: line-by-line JSONL parsing, ``input.text_field`` dotted-path
  extraction, deterministic id = sha256(canonical_json(raw))[:16];
- UI modality: recursive scan, ``uitree_<index>.jsonl`` / ``image_<index>.*``
  pairing across subdirectories (one shared index namespace), UI-tree node
  parsing per the §6.2 field mapping, lazy ``ImageRef`` (magic number + size
  check only — no pixel decode), id = sha256(tree_bytes + image_bytes)[:16].

Bad data follows input.on_bad_line / on_missing_pair / on_index_conflict
("skip" → count + trace event; "fail" → InputError, CLI exit 3).
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator, Mapping

from labelkit.config.model import ResolvedConfig
from labelkit.errors import InputError
from labelkit.types import ImageRef, Record, RecordRef, UINode, UITree

__all__ = ["IngestPlan", "IngestReport", "Ingestor"]


# ── filename patterns (spec 3.2.4; extension match case-insensitive) ───────
_TREE_RE = re.compile(r"^uitree_(\d+)\.(?i:jsonl)$")
_IMAGE_RE = re.compile(r"^image_(\d+)\.(?i:png|jpg|jpeg)$")

# image magic numbers (spec 3.2.4: 仅校验魔数与尺寸，不解码全图)
_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
_JPEG_MAGIC = b"\xff\xd8\xff"

# ── §6.2 field mapping: accepted source names, in precedence order ─────────
_NODE_ID_KEYS = ("id", "node_id")
_PARENT_KEYS = ("parent", "parent_id")
_ROLE_KEYS = ("class", "className", "type", "role")
_TEXT_KEYS = ("text", "label")
_DESC_KEYS = ("content_desc", "contentDescription", "desc")
_BOUNDS_KEYS = ("bounds",)
_VISIBLE_KEYS = ("visible", "visible_to_user")

_BOUNDS_STR_RE = re.compile(
    r"^\s*\[\s*(-?\d+)\s*,\s*(-?\d+)\s*\]\s*\[\s*(-?\d+)\s*,\s*(-?\d+)\s*\]\s*$"
)


def _canonical_json(obj: Any) -> str:
    """Canonical JSON per spec 3.2.5 (sort_keys, ensure_ascii=False, compact)."""
    return json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def _text_record_id(raw: Mapping) -> str:
    return hashlib.sha256(_canonical_json(raw).encode("utf-8")).hexdigest()[:16]


def _extract_text_field(obj: Mapping, dotted_path: str) -> str | None:
    """Dotted-path extraction (spec 3.2.5). Returns None on a miss.

    String hit → used as-is; array/object (or any other non-null JSON value)
    hit → canonical JSON serialization; missing key / null / non-mapping
    intermediate → miss.
    """
    cur: Any = obj
    for part in dotted_path.split("."):
        if not isinstance(cur, Mapping) or part not in cur:
            return None
        cur = cur[part]
    if cur is None:
        return None
    if isinstance(cur, str):
        return cur
    return _canonical_json(cur)


@dataclass(frozen=True)
class IngestPlan:
    files: tuple[str, ...]                     # text: .jsonl files (lexicographic by name);
                                               # UI: all matched files, tree then image per
                                               # pair, pairs ascending. Paths relative to
                                               # run.input (as RecordRef.source_file)
    pairs: tuple[tuple[int, str, str], ...]    # UI pairing table (spec 3.2.3 配对表):
                                               # (index, tree_path, image_path), ascending
                                               # by index; text modality: ()
    estimated_records: int                     # text: total lines (cheap count); UI: len(pairs)


@dataclass
class IngestReport:
    scanned: int = 0                           # lines seen / pair indexes seen
    ingested: int = 0
    bad_input: int = 0                         # bad lines + skipped conflicts + missing pairs
    missing_pair: int = 0                      # UI only
    index_conflict: int = 0                    # UI only
    bad_locations: list[dict] = field(default_factory=list)
                                               # {"file": str, "line_no": int|None,
                                               #  "index": int|None, "reason": str}


@dataclass(frozen=True)
class _UIScan:
    """Full UI scan result (internal): matched pairs + anomalies, all by index."""
    pairs: tuple[tuple[int, str, str], ...]            # ascending by index
    conflicts: tuple[tuple[int, tuple[str, ...]], ...]  # (index, offending files) ascending
    missing: tuple[tuple[int, str, str], ...]           # (index, present "tree"|"image", file)


class Ingestor:
    """M2 ingest. Not a Stage — has no ctx; the CLI/orchestrator sets
    ``ingestor.metrics`` (public attribute, default None) before calling
    ``records()`` so ingest trace events are emitted with batch_no=0."""

    def __init__(self, cfg: ResolvedConfig):
        self._cfg = cfg
        self._root = Path(cfg.run.input) if cfg.run.input else None
        self._report = IngestReport()
        self.metrics = None  # MetricsSink | None, wired externally (CONTRACTS §7.1)

    @property
    def report(self) -> IngestReport:
        return self._report

    # ── scan ────────────────────────────────────────────────────────────────

    def scan(self, *, estimate: bool = True) -> IngestPlan:
        """Scan only, no parsing: file list, pairing table, estimated record count.
        Used by --dry-run, `validate` and the orchestrator's P2-4 pre-scan.
        Raises InputError if run.input is missing/unreadable or a UI pairing
        problem hits a 'fail' policy. ``estimate=False`` skips the text-modality
        line count (which reads every input byte) — the pre-scan needs only the
        fail-fast checks, not the estimate, and must not double the input I/O."""
        root = self._require_root()
        if self._cfg.run.modality == "text":
            files = self._text_files(root)
            estimated = 0
            if estimate:
                for rel in files:
                    path = root / rel if root.is_dir() else root
                    try:
                        with path.open("rb") as fh:
                            estimated += sum(1 for line in fh if line.strip())
                    except OSError as exc:
                        raise InputError(f"无法读取输入文件 {path}: {exc}") from exc
            return IngestPlan(files=tuple(files), pairs=(), estimated_records=estimated)

        ui = self._scan_ui(root)
        if ui.conflicts and self._cfg.input.on_index_conflict == "fail":
            index, files_ = ui.conflicts[0]
            self._emit("ingest.index_conflict", {"index": index, "files": list(files_)})
            self._stderr_fallback(
                "ingest.index_conflict index=%s files=%s", index, list(files_))
            raise InputError(
                f"UI index 冲突: index={index} 匹配多个文件 {list(files_)}"
                f"（input.on_index_conflict = \"fail\"）"
            )
        if ui.missing and self._cfg.input.on_missing_pair == "fail":
            # Same fail-fast contract as the conflict branch (P2-4 review):
            # a missing-pair 'fail' run must die HERE, before run.start ever
            # opens (and truncates) the previous run's trace file.
            index, present, file_ = ui.missing[0]
            self._emit("ingest.missing_pair",
                       {"index": index, "present": present, "file": file_})
            self._stderr_fallback(
                "ingest.missing_pair index=%s present=%s file=%s", index, present, file_)
            raise InputError(
                f"UI 文件缺对: index={index} 仅有 {present} 侧（{file_}）"
                f"（input.on_missing_pair = \"fail\"）"
            )
        plan_files: list[str] = []
        for _, tree, image in ui.pairs:
            plan_files.append(tree)
            plan_files.append(image)
        return IngestPlan(files=tuple(plan_files), pairs=ui.pairs,
                          estimated_records=len(ui.pairs))

    # ── record stream ───────────────────────────────────────────────────────

    def records(self) -> Iterator[Record]:
        """Lazy Record stream. Parse errors follow input.on_bad_line /
        on_missing_pair / on_index_conflict ('skip' → count + trace event;
        'fail' → raise InputError). A stream that exhausts with ZERO valid
        records raises InputError（「无任何合法记录」, spec §2.4 → exit 3）—
        a run that would produce nothing is an input error, not a success."""
        root = self._require_root()
        if self._cfg.run.modality == "text":
            yield from self._text_records(root)
        else:
            yield from self._ui_records(root)
        if self._report.ingested == 0:
            r = self._report
            raise InputError(
                f"无任何合法记录: {root}（scanned={r.scanned} bad_input={r.bad_input}"
                f" missing_pair={r.missing_pair} index_conflict={r.index_conflict}）"
            )

    # ── shared helpers ──────────────────────────────────────────────────────

    def _require_root(self) -> Path:
        if self._root is None:
            raise InputError("run.input 未设置（process 模式必需）")
        if not self._root.exists():
            raise InputError(f"run.input 路径不存在: {self._root}")
        return self._root

    def _stderr_fallback(self, msg: str, *args) -> None:
        """ERROR-level stderr line for scan-time 'fail' policies when metrics is
        detached (the orchestrator pre-scan runs with metrics=None so trace
        stays untouched) — log-pipeline consumers matching ingest.* event names
        must still see the structured line (spec §7.2 fail 策略 error 级)."""
        if self.metrics is None:
            logging.getLogger("labelkit.ingest").error(
                msg, *args, extra={"stage": "ingest", "batch": 0})

    def _emit(self, ev: str, payload: dict) -> None:
        if self.metrics is not None:
            self.metrics.event(ev, stage="ingest", batch_no=0, payload=payload)

    def _bad(self, *, file: str, line_no: int | None, index: int | None,
             reason: str) -> None:
        self._report.bad_input += 1
        self._report.bad_locations.append(
            {"file": file, "line_no": line_no, "index": index, "reason": reason})

    # ── text modality ───────────────────────────────────────────────────────

    def _text_files(self, root: Path) -> list[str]:
        """Relative .jsonl file list, lexicographic by name (spec 3.2.2)."""
        if root.is_file():
            return [root.name]
        if not root.is_dir():
            raise InputError(f"run.input 不是文件也不是目录: {root}")
        files = sorted(p.name for p in root.iterdir()
                       if p.is_file() and p.suffix == ".jsonl")
        if not files:
            raise InputError(f"run.input 目录下没有 .jsonl 文件: {root}")
        return files

    def _text_records(self, root: Path) -> Iterator[Record]:
        on_bad = self._cfg.input.on_bad_line
        text_field = self._cfg.input.text_field
        for rel in self._text_files(root):
            path = root / rel if root.is_dir() else root
            # Binary read + strict per-line decode: spec 6.1 mandates UTF-8
            # JSONL and 3.2.1 mandates 原样保留 — invalid bytes must become a
            # bad line, never be silently replaced (errors="replace") and
            # ingested as altered data.
            with path.open("rb") as fh:
                for line_no, line_bytes in enumerate(fh, 1):
                    if not line_bytes.strip():
                        continue  # empty lines skipped silently (spec 6.1)
                    self._report.scanned += 1
                    reason: str | None = None
                    raw: Any = None
                    try:
                        line = line_bytes.decode("utf-8")
                    except UnicodeDecodeError:
                        reason = "行不是合法 UTF-8"
                    if reason is None:
                        try:
                            raw = json.loads(line)
                        except json.JSONDecodeError as exc:
                            reason = f"JSON 解析失败: {exc.msg}"
                    if reason is None and not isinstance(raw, dict):
                        reason = "JSON 行不是 object"
                    text: str | None = None
                    if reason is None:
                        text = _extract_text_field(raw, text_field)
                        if text is None:
                            reason = f'input.text_field "{text_field}" 未命中'
                    if reason is not None:
                        self._bad(file=rel, line_no=line_no, index=None, reason=reason)
                        self._emit("ingest.bad_line",
                                   {"file": rel, "line_no": line_no, "reason": reason})
                        if on_bad == "fail":
                            raise InputError(f"{rel}:{line_no}: {reason}"
                                             f"（input.on_bad_line = \"fail\"）")
                        continue
                    self._report.ingested += 1
                    yield Record(
                        id=_text_record_id(raw),
                        modality="text",
                        text=text,
                        raw=raw,
                        ui_tree=None,
                        image=None,
                        ref=RecordRef(source_file=rel, line_no=line_no,
                                      pair_index=None, generated_from=()),
                    )

    # ── UI modality: scan & pairing (spec 3.2.4) ────────────────────────────

    def _scan_ui(self, root: Path) -> _UIScan:
        if not root.is_dir():
            raise InputError(f"UI 模态 run.input 必须是目录: {root}")
        trees: dict[int, list[str]] = {}
        images: dict[int, list[str]] = {}
        for path in sorted(root.rglob("*")):
            if not path.is_file():
                continue
            rel = path.relative_to(root).as_posix()
            m = _TREE_RE.match(path.name)
            if m:
                trees.setdefault(int(m.group(1), 10), []).append(rel)
                continue
            m = _IMAGE_RE.match(path.name)
            if m:
                images.setdefault(int(m.group(1), 10), []).append(rel)
        if not trees and not images:
            raise InputError(
                f"UI 模态目录下未找到 uitree_<index>.jsonl / image_<index>.(png|jpg|jpeg) 文件: {root}")

        pairs: list[tuple[int, str, str]] = []
        conflicts: list[tuple[int, tuple[str, ...]]] = []
        missing: list[tuple[int, str, str]] = []
        for index in sorted(set(trees) | set(images)):
            t = trees.get(index, [])
            i = images.get(index, [])
            if len(t) >= 2 or len(i) >= 2:
                conflicts.append((index, tuple(t + i)))
            elif not i:
                missing.append((index, "tree", t[0]))
            elif not t:
                missing.append((index, "image", i[0]))
            else:
                pairs.append((index, t[0], i[0]))
        return _UIScan(pairs=tuple(pairs), conflicts=tuple(conflicts),
                       missing=tuple(missing))

    def _ui_records(self, root: Path) -> Iterator[Record]:
        icfg = self._cfg.input
        ui = self._scan_ui(root)
        # `scanned` is counted per index as each index is actually handled
        # (not eagerly for the whole scan), so a partially consumed stream
        # (--limit, circuit breaker, SIGINT) keeps the §6.4 report invariant
        # emitted + dropped_* + failed + bad_input = scanned + generated.

        # Anomalies are reported in ascending index order, before pair parsing.
        for index, files in ui.conflicts:
            self._report.scanned += 1
            self._report.index_conflict += 1
            self._emit("ingest.index_conflict", {"index": index, "files": list(files)})
            if icfg.on_index_conflict == "fail":
                raise InputError(f"UI index 冲突: index={index} 匹配多个文件 "
                                 f"{list(files)}（input.on_index_conflict = \"fail\"）")
            self._bad(file=files[0], line_no=None, index=index,
                      reason=f"index 冲突: {list(files)}")
        for index, present, rel in ui.missing:
            self._report.scanned += 1
            self._report.missing_pair += 1
            self._emit("ingest.missing_pair",
                       {"index": index, "present": present, "file": rel})
            if icfg.on_missing_pair == "fail":
                raise InputError(f"UI 文件缺对: index={index} 仅有 {present} 侧文件 "
                                 f"{rel}（input.on_missing_pair = \"fail\"）")
            self._bad(file=rel, line_no=None, index=index,
                      reason=f"缺对: 仅有 {present} 侧文件")

        max_bytes = icfg.max_image_mb * 1024 * 1024
        for index, tree_rel, image_rel in ui.pairs:
            self._report.scanned += 1
            tree_path = root / tree_rel
            image_path = root / image_rel
            reason = self._check_image(image_path, max_bytes)
            bad_file = image_rel
            ui_tree: UITree | None = None
            tree_bytes = b""
            if reason is None:
                bad_file = tree_rel
                try:
                    tree_bytes = tree_path.read_bytes()
                except OSError as exc:
                    reason = f"无法读取 UI 树文件: {exc}"
                else:
                    ui_tree, reason = _parse_ui_tree(tree_bytes)
            if reason is not None:
                self._bad(file=bad_file, line_no=None, index=index, reason=reason)
                self._emit("ingest.bad_line",
                           {"file": bad_file, "line_no": None, "reason": reason})
                if icfg.on_bad_line == "fail":
                    raise InputError(
                        f"{bad_file}: {reason}（input.on_bad_line = \"fail\"）")
                continue

            image_bytes = image_path.read_bytes()
            rec_id = hashlib.sha256(tree_bytes + image_bytes).hexdigest()[:16]
            ext = image_path.suffix.lower().lstrip(".")
            image_ref = ImageRef(
                path=image_path,
                format="png" if ext == "png" else "jpeg",
                size_bytes=len(image_bytes),
            )
            del image_bytes  # only hashed — pixels stay lazy (spec §2.6)
            self._report.ingested += 1
            yield Record(
                id=rec_id,
                modality="ui",
                text=None,
                raw=None,
                ui_tree=ui_tree,
                image=image_ref,
                ref=RecordRef(source_file=tree_rel, line_no=None,
                              pair_index=index, generated_from=()),
            )

    @staticmethod
    def _check_image(path: Path, max_bytes: int) -> str | None:
        """Magic-number + size check only, no full decode (spec 3.2.4).
        Returns a reason string when the image is bad, else None."""
        try:
            size = path.stat().st_size
            with path.open("rb") as fh:
                head = fh.read(8)
        except OSError as exc:
            return f"无法读取图像文件: {exc}"
        if size > max_bytes:
            return (f"图像大小 {size} 字节超出 input.max_image_mb = "
                    f"{max_bytes // (1024 * 1024)} 上限")
        ext = path.suffix.lower().lstrip(".")
        if ext == "png":
            if not head.startswith(_PNG_MAGIC):
                return "图像魔数与 .png 扩展名不符"
        else:
            if not head.startswith(_JPEG_MAGIC):
                return f"图像魔数与 .{ext} 扩展名不符"
        return None


# ── UI tree parsing (spec 3.2.4 + §6.2 field mapping) ───────────────────────

def _parse_ui_tree(data: bytes) -> tuple[UITree | None, str | None]:
    """Parse a uitree_<index>.jsonl file. Returns (tree, None) on success or
    (None, reason) when the file is empty or every line is bad (spec 3.2.4:
    空文件或全坏行 ⇒ 该记录按坏记录跳过). Individual bad node lines are skipped."""
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return None, "UI 树文件不是合法 UTF-8"
    lines = [(no, ln) for no, ln in enumerate(text.splitlines(), 1) if ln.strip()]
    if not lines:
        return None, "UI 树文件为空"

    # First-line probe: object containing a `children` array → nested style.
    nested = False
    try:
        first = json.loads(lines[0][1])
        nested = isinstance(first, dict) and isinstance(first.get("children"), list)
    except json.JSONDecodeError:
        pass

    nodes: list[UINode] = []
    if nested:
        counter = [0]
        for _, ln in lines:
            try:
                obj = json.loads(ln)
            except json.JSONDecodeError:
                continue
            if not isinstance(obj, dict):
                continue
            _walk_nested(obj, parent_id=None, depth=0, counter=counter, out=nodes)
    else:
        flat: list[UINode] = []
        for line_no, ln in lines:
            try:
                obj = json.loads(ln)
            except json.JSONDecodeError:
                continue
            if not isinstance(obj, dict):
                continue
            flat.append(_normalize_node(obj, default_node_id=str(line_no),
                                        structural_parent=None))
        nodes = _flat_to_dfs(flat)
    if not nodes:
        return None, "UI 树文件全为坏行"
    return UITree(nodes=tuple(nodes)), None


def _flat_to_dfs(flat: list[UINode]) -> list[UINode]:
    """Rebuild flat-style nodes into depth-first order with depths derived
    from the parent_id graph (spec 4.1: ``UITree.nodes # 深度优先序`` — a type
    contract that must hold regardless of file order, e.g. BFS-ordered
    accessibility dumps). Roots = nodes whose parent_id is None or unknown;
    children keep file order; any node unreachable from a root (parent-id
    cycle) falls back to a depth-0 root, preserving file order."""
    known_ids = {n.node_id for n in flat}
    roots: list[int] = []
    children: dict[str, list[int]] = {}
    for i, node in enumerate(flat):
        if node.parent_id is None or node.parent_id not in known_ids:
            roots.append(i)
        else:
            children.setdefault(node.parent_id, []).append(i)

    out: list[UINode] = []
    visited: set[int] = set()

    def _visit(i: int, depth: int) -> None:
        if i in visited:
            return
        visited.add(i)
        node = flat[i]
        out.append(_with_depth(node, depth))
        for child in children.get(node.node_id, ()):
            _visit(child, depth + 1)

    for i in roots:
        _visit(i, 0)
    for i in range(len(flat)):  # cycle members unreachable from any root
        _visit(i, 0)
    return out


def _walk_nested(obj: dict, *, parent_id: str | None, depth: int,
                 counter: list[int], out: list[UINode]) -> None:
    """Depth-first traversal of a nested-style tree (spec 3.2.4)."""
    counter[0] += 1
    node = _normalize_node(obj, default_node_id=str(counter[0]),
                           structural_parent=parent_id, consume_children=True)
    out.append(_with_depth(node, depth))
    children = obj.get("children")
    if isinstance(children, list):
        for child in children:
            if isinstance(child, dict):
                _walk_nested(child, parent_id=node.node_id, depth=depth + 1,
                             counter=counter, out=out)


def _with_depth(node: UINode, depth: int) -> UINode:
    if node.depth == depth:
        return node
    return UINode(node_id=node.node_id, parent_id=node.parent_id, depth=depth,
                  role=node.role, text=node.text, content_desc=node.content_desc,
                  bounds=node.bounds, visible=node.visible, extra=node.extra)


def _first_present(obj: dict, keys: tuple[str, ...]) -> tuple[str | None, Any]:
    for key in keys:
        if key in obj:
            return key, obj[key]
    return None, None


def _parse_bounds(value: Any) -> tuple[int, int, int, int] | None:
    """Accepts [l,t,r,b] arrays and "[l,t][r,b]" strings (spec §6.2)."""
    if isinstance(value, list) and len(value) == 4:
        try:
            return tuple(int(v) for v in value)  # type: ignore[return-value]
        except (TypeError, ValueError):
            return None
    if isinstance(value, str):
        m = _BOUNDS_STR_RE.match(value)
        if m:
            return tuple(int(g) for g in m.groups())  # type: ignore[return-value]
    return None


def _coerce_visible(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() not in ("false", "0", "no", "")
    return bool(value)


def _stringify_extra(value: Any) -> str:
    if isinstance(value, str):
        return value
    return _canonical_json(value)


def _normalize_node(obj: dict, *, default_node_id: str,
                    structural_parent: str | None,
                    consume_children: bool = False) -> UINode:
    """§6.2 field mapping: first present source field per target, per-field
    defaults, remaining fields stringified into `extra` (insertion order).
    `children` is structural only in the nested style (consume_children=True);
    a flat-style row carrying a `children` field keeps it in `extra` per the
    §6.2 extra row (其余全部字段，值转字符串)."""
    consumed: set[str] = {"children"} if consume_children else set()

    key, value = _first_present(obj, _NODE_ID_KEYS)
    if key is not None:
        consumed.add(key)
    node_id = str(value) if key is not None and value is not None else default_node_id

    key, value = _first_present(obj, _PARENT_KEYS)
    if key is not None:
        consumed.add(key)
        parent_id = str(value) if value is not None else None
    else:
        parent_id = structural_parent

    key, value = _first_present(obj, _ROLE_KEYS)
    if key is not None:
        consumed.add(key)
    role = str(value) if key is not None and value is not None else "unknown"

    key, value = _first_present(obj, _TEXT_KEYS)
    if key is not None:
        consumed.add(key)
    text = str(value) if key is not None and value is not None else ""

    key, value = _first_present(obj, _DESC_KEYS)
    if key is not None:
        consumed.add(key)
    content_desc = str(value) if key is not None and value is not None else ""

    key, value = _first_present(obj, _BOUNDS_KEYS)
    bounds: tuple[int, int, int, int] = (0, 0, 0, 0)
    if key is not None:
        consumed.add(key)
        parsed = _parse_bounds(value)
        if parsed is not None:
            bounds = parsed

    key, value = _first_present(obj, _VISIBLE_KEYS)
    if key is not None:
        consumed.add(key)
    visible = _coerce_visible(value) if key is not None and value is not None else True

    extra = {k: _stringify_extra(v) for k, v in obj.items() if k not in consumed}
    return UINode(node_id=node_id, parent_id=parent_id, depth=0, role=role,
                  text=text, content_desc=content_desc, bounds=bounds,
                  visible=visible, extra=extra)
