"""Shared data types (spec ch.4). Frozen contract — do not edit without updating CONTRACTS.md."""
from __future__ import annotations

import base64
import io
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, Mapping

Status = Literal[
    "active",          # alive, keeps flowing
    "dropped_dup",     # M3 judged duplicate
    "dropped_lowq",    # M4 below quality gate
    "dropped_verify",  # M7 verdict fail with policy=drop (or repair budget exhausted)
    "failed",          # processing error (irreparable schema / provider retries exhausted ...)
    "absorbed",        # v1.8: member frame absorbed into an episode (M14; neither output channel)
    "dropped_noise",   # v1.8: noise frame (M14 interruption / below_min_len; M7 member shrink)
    "stitched",        # v1.9: merged-fragment episode shell (M16; terminal, neither channel)
]


@dataclass(frozen=True)
class RecordRef:
    source_file: str                       # path relative to run.input ("" for generated records)
    line_no: int | None                    # text modality: 1-based line number
    pair_index: int | None                 # UI modality: file-pair index
    generated_from: tuple[str, ...]        # process-mode generated sample: seed record ids;
                                           # everything else (incl. generate_only samples): ()
                                           # — synthetic-ness is judged by `generator`, not this (v1.4)
    generator: Mapping | None = None       # generated records: {"llm": <profile>, "style": <name>|None}
                                           # non-generated records: None


@dataclass(frozen=True)
class ImageRef:
    path: Path
    format: Literal["png", "jpeg"]         # ".jpg"/".jpeg" both map to "jpeg"
    size_bytes: int

    def load_base64(self, max_px: int) -> tuple[str, str]:
        """Load from disk at call time. If the longer edge exceeds max_px, downscale
        proportionally (Pillow) before encoding. Returns (media_type, b64) where media_type is
        "image/png" | "image/jpeg". Bytes are not cached — used and discarded (spec §2.6)."""
        from PIL import Image  # local import: keep module import light; Pillow is a hard dep

        media_type = "image/png" if self.format == "png" else "image/jpeg"
        with Image.open(self.path) as im:
            width, height = im.size
            long_edge = max(width, height)
            if long_edge > max_px:
                scale = max_px / long_edge
                new_size = (max(1, round(width * scale)), max(1, round(height * scale)))
                resized = im.resize(new_size, Image.Resampling.LANCZOS)
                if self.format == "jpeg" and resized.mode not in ("RGB", "L"):
                    resized = resized.convert("RGB")
                buf = io.BytesIO()
                resized.save(buf, format="PNG" if self.format == "png" else "JPEG")
                data = buf.getvalue()
            else:
                data = self.path.read_bytes()
        return media_type, base64.b64encode(data).decode("ascii")


@dataclass(frozen=True)
class UINode:
    node_id: str
    parent_id: str | None
    depth: int
    role: str                              # widget role normalized from class/type
    text: str
    content_desc: str
    bounds: tuple[int, int, int, int]      # (l, t, r, b) pixels
    visible: bool
    extra: Mapping[str, str]               # non-whitelisted source fields, values stringified


@dataclass(frozen=True)
class UITree:
    nodes: tuple[UINode, ...]              # depth-first order

    def serialize(self, max_chars: int | None = None, quantize_px: int = 0) -> str:
        """Canonical linearization (spec §4.3), shared by M3 dedup (quantize_px =
        dedup.bounds_quantize_px) and M5 prompts (quantize_px = 0, max_chars =
        input.ui_tree_max_chars).

        Rules (exact):
        - Traverse `nodes` in stored (depth-first) order; skip nodes with visible == False.
        - One line per node, joined with "\\n", no trailing newline:
            line = ("  " * depth) + role
                   + (f' "{text}"' if text else "")
                   + (f' desc="{content_desc}"' if content_desc else "")
                   + f" [{l},{t},{r},{b}]"
                   + "".join(f" {k}={v}" for k, v in extra.items() if v)
          (extra in insertion order; indentation is TWO spaces per depth level — matches the
           worked examples in spec 3.2.7/3.9.4 [FROZEN HERE, see §12].)
        - If quantize_px > 0, each coordinate is floor-divided first: c = c // quantize_px.
        - If max_chars is not None and the full output exceeds it: keep the longest prefix of
          whole lines whose joined length (incl. "\\n" separators and the marker line below)
          ≤ max_chars, then append a final line "…(truncated N nodes)" where N = number of
          visible nodes omitted. [FROZEN HERE]
        """
        lines: list[str] = []
        for node in self.nodes:
            if not node.visible:
                continue
            l, t, r, b = node.bounds
            if quantize_px > 0:
                l, t, r, b = (l // quantize_px, t // quantize_px,
                              r // quantize_px, b // quantize_px)
            line = ("  " * node.depth) + node.role
            if node.text:
                line += f' "{node.text}"'
            if node.content_desc:
                line += f' desc="{node.content_desc}"'
            line += f" [{l},{t},{r},{b}]"
            line += "".join(f" {k}={v}" for k, v in node.extra.items() if v)
            lines.append(line)

        full = "\n".join(lines)
        if max_chars is None or len(full) <= max_chars:
            return full

        # Truncate: longest prefix of whole lines such that the joined output including the
        # final marker line fits within max_chars.
        total = len(lines)
        # prefix_len[k] = len("\n".join(lines[:k]))
        prefix_len = [0] * (total + 1)
        for i, line in enumerate(lines):
            prefix_len[i + 1] = prefix_len[i] + (1 if i else 0) + len(line)
        for keep in range(total - 1, -1, -1):
            marker = f"…(truncated {total - keep} nodes)"
            joined = prefix_len[keep] + (1 if keep else 0) + len(marker)
            if joined <= max_chars:
                return "\n".join(lines[:keep] + [marker])
        # Even the marker alone exceeds max_chars: emit the marker for all visible nodes.
        return f"…(truncated {total} nodes)"


@dataclass(frozen=True)
class Record:
    id: str                                # sha256 hex prefix [:16]; rule per modality (M2/M6)
                                           # sequence (v1.8): sha256("\n".join(member ids))[:16],
                                           # fixed at formation — member surgery never recomputes it
    modality: Literal["text", "ui"]
    text: str | None                       # text modality: extracted text; UI modality: None
    raw: Mapping | None                    # text modality: original line object; UI: None
    ui_tree: UITree | None
    image: ImageRef | None
    ref: RecordRef
    kind: Literal["single", "sequence"] = "single"   # v1.8: appended with default (frozen-compat)
    members: tuple["Record", ...] = ()     # v1.8 sequence: member frames in order-key ascending
                                           # order; single: (). Sequence-record field convention
                                           # (S24): text/raw/ui_tree/image = None; modality = the
                                           # members' modality; ref = RecordRef(source_file=first
                                           # member's source, line_no=first member's line_no,
                                           # pair_index=first member's pair_index,
                                           # generated_from=(), generator=None) — full member
                                           # provenance travels in _meta.stream.member_sources


@dataclass(frozen=True)
class Classification:                      # v1.7: M13 classify verdict (spec 3.13, §4.1)
    label: str                             # routing label of THIS envelope
    labels: tuple[str, ...]                # the record's full hit set (declaration order;
                                           # single assignment: always one element)
    source: Literal["llm", "fallback", "inherited"]
    detail: Mapping                        # reason / sc stats / fallback trace (kind, message)


@dataclass(frozen=True)
class DedupInfo:
    kind: Literal["unique", "exact", "near_text", "near_image", "near_both", "near_semantic"]
    cluster_key: str                       # exact-dedup key ([:16] hex) of the cluster head;
                                           # unique records carry their own key
    kept_id: str | None                    # duplicates: id of the retained record; unique: None


@dataclass(frozen=True)
class QualityScore:
    criterion: str                         # rubric criterion key, or "__aggregate__"
    score: float | None                    # [0,1] normalized; None = unscored (all judgments failed)
    mode: Literal["pairwise_bt", "pointwise"]
    detail: Mapping                        # pairwise: {comparisons, wins, ties, log_theta}
                                           # pointwise: {raw_score (0-5), reason}
                                           # __aggregate__: {}


@dataclass(frozen=True)
class Usage:
    prompt_tokens: int = 0
    completion_tokens: int = 0

    def __add__(self, other: "Usage") -> "Usage":          # [FROZEN HERE]
        return Usage(self.prompt_tokens + other.prompt_tokens,
                     self.completion_tokens + other.completion_tokens)

    def __radd__(self, other: object) -> "Usage":          # [FROZEN HERE]
        # Supports `sum(usage_list)`: sum's implicit start is int 0.
        if other == 0:
            return self
        return NotImplemented


@dataclass(frozen=True)
class Annotation:
    output: Mapping                        # object that PASSED the user schema (L2)
    model: str                             # provider model string of the annotating profile
    attempts: int                          # 1 + number of L3 repair calls
                                           # (self-consistency: sum of attempts over the n samples)
    usage: Usage                           # tokens of first call + repair calls (all n samples if SC)
    sc: Mapping | None = None              # self-consistency only: {"n": int, "agreement_ratio": float}
                                           # [FROZEN HERE: carried here so M11 can write _meta]


@dataclass(frozen=True)
class VerificationResult:
    verdict: Literal["pass", "fail"]
    rounds: int                            # judged rounds incl. the first (pass on first review = 1)
    critiques: tuple[Mapping, ...]         # accumulated over rounds, in order:
                                           # {"aspect": str, "opinion": str[, "judge": str]}
    defects: tuple[Mapping, ...] = ()      # v1.8 (stream verify only): typed defect table entries
                                           # {"kind","members","position","detail"[, "suspected"]}
                                           # — carried here so M11 can write _meta (Annotation.sc
                                           # precedent) [FROZEN HERE]


@dataclass(frozen=True)
class StageError:
    stage: str                             # stage name that produced the error
    kind: str                              # error classification code (§7.6 / errors.ErrorKind)
    message: str
    retryable: bool


# ── v1.8 shared stream helpers (spec §4 / CONTRACTS §3) ─────────────────────
# Deterministic frame digest + tree diff shared by M14 segment, M15 extract,
# M13 classify and M4 quality sequence branches. Operators never depend on each
# other — shared rendering lives here next to UITree.serialize (M3/M5 precedent).

_DIGEST_APP_KEYS = ("package", "package_name", "pkg")
_DIGEST_ACTIVITY_KEYS = ("activity", "activity_name", "window_title")
_DIGEST_INTERACTIVE_ROLES = ("Button", "EditText", "CheckBox", "Switch", "ImageButton")


def frame_digest(record: "Record", max_chars: int) -> str:
    """Deterministic best-effort frame digest (spec §4 shared helper, S12).

    text modality: record.text truncated to max_chars (plain slice).
    UI modality — "[{app} activity={act}] {title}｜{salient}", absent parts
    omitted (fields depend on what the capture-side dump put into `extra`):
      app      = first non-empty `extra` value among package/package_name/pkg
                 (visible nodes, DFS order); absent → the whole "[{app}] " head
                 segment is omitted (an activity value is anchored to it and
                 drops with it)
      activity = first non-empty among activity/activity_name/window_title
                 (often absent), rendered as " activity={v}" right after app
      title    = text of the first visible node with non-empty text (DFS order)
      salient  = ordered, de-duplicated non-empty text/content_desc of visible
                 nodes, "、"-joined; entries whose role contains one of
                 Button/EditText/CheckBox/Switch/ImageButton get a "*" prefix
    A digest longer than max_chars is cut to max_chars-1 chars + "…" (total ==
    max_chars, serialize truncation convention). Poverty judgment is the
    caller-side digest_is_poor() below (digest_poor_frames counter)."""
    if record.modality == "text":
        return (record.text or "")[:max_chars]
    if record.ui_tree is None:
        return ""
    app = activity = title = None
    salient: list[str] = []
    seen: set[str] = set()
    for node in record.ui_tree.nodes:
        if not node.visible:
            continue
        if app is None:
            for key in _DIGEST_APP_KEYS:
                value = node.extra.get(key)
                if value:
                    app = value
                    break
        if activity is None:
            for key in _DIGEST_ACTIVITY_KEYS:
                value = node.extra.get(key)
                if value:
                    activity = value
                    break
        if title is None and node.text:
            title = node.text
        interactive = any(role in node.role for role in _DIGEST_INTERACTIVE_ROLES)
        for piece in (node.text, node.content_desc):
            if piece and piece not in seen:
                seen.add(piece)
                salient.append(f"*{piece}" if interactive else piece)
    head = ""
    if app:
        head = f"[{app} activity={activity}] " if activity else f"[{app}] "
    salient_text = "、".join(salient)
    body = f"{title}｜{salient_text}" if title else salient_text
    digest = head + body if body else head.rstrip()
    if len(digest) > max_chars:
        digest = digest[: max_chars - 1] + "…"
    return digest


def digest_is_poor(record: "Record") -> bool:
    """True iff the record is UI modality and either its tree has ZERO visible
    text nodes (nodes with non-empty text or content_desc) or its rendered
    digest is shorter than 8 characters — barren ghost-node / canvas screens
    whose digest carries no information (S12 guard, spec §4 poverty judgment:
    可见文本节点数为 0 或摘要长度 < 8; callers count digest_poor_frames and
    WARN once per run, directing users to configure a supports_vision=true
    profile for segment.llm — v1.11 V4 wording, the former use_vision key is
    removed and vision follows profile capability). Text modality is never
    poor by this judgment. The length disjunct renders at a cap far above the
    threshold, so the cap value cannot mask a genuinely short digest."""
    if record.modality != "ui":
        return False
    if record.ui_tree is None:
        return True
    if not any(node.visible and (node.text or node.content_desc)
               for node in record.ui_tree.nodes):
        return True
    return len(frame_digest(record, 400)) < 8


def tree_diff(a: "UITree | None", b: "UITree | None", quantize_px: int) -> Mapping:
    """Deterministic structural tree diff (spec §4 shared helper, S13).

    Visible nodes only, matched as a MULTISET (collections.Counter) over the
    structural key k(node) = (role, bounds // quantize_px when quantize_px > 0,
    depth) — node_id is NOT a cross-frame identity and must not be a match key.
    Returns {"added": int, "removed": int, "text_changed": int,
    "change_ratio": float, "app_changed": bool, "title_changed": bool}:
      added/removed  = unpaired node counts per structural key (a or b None ⇒
                       every visible node of the other side counts here)
      text_changed   = LOWER BOUND on content-changed pairs: within each key's
                       min(count_a, count_b) pairing, the number of (text,
                       content_desc) multiset mismatches
      change_ratio   = (added + removed + text_changed)
                       / max(1, max(visible_a, visible_b))
      app_changed / title_changed = compared via the same extraction rules as
                       frame_digest (extra app keys / DFS-first visible text)
    Deterministic (result independent of hash/iteration order — pure multiset
    arithmetic) and O(n1 + n2). Magnitude/type evidence only, no semantic
    attribution (that is M15's job)."""
    def _index(tree: "UITree | None"):
        keyed: dict[tuple, Counter] = {}
        app = title = None
        count = 0
        for node in (tree.nodes if tree is not None else ()):
            if not node.visible:
                continue
            count += 1
            l, t, r, bt = node.bounds
            if quantize_px > 0:
                l, t, r, bt = (l // quantize_px, t // quantize_px,
                               r // quantize_px, bt // quantize_px)
            key = (node.role, (l, t, r, bt), node.depth)
            keyed.setdefault(key, Counter())[(node.text, node.content_desc)] += 1
            if app is None:
                for k in _DIGEST_APP_KEYS:
                    value = node.extra.get(k)
                    if value:
                        app = value
                        break
            if title is None and node.text:
                title = node.text
        return keyed, app, title, count

    keyed_a, app_a, title_a, n_a = _index(a)
    keyed_b, app_b, title_b, n_b = _index(b)
    added = removed = text_changed = 0
    for key in keyed_a.keys() | keyed_b.keys():
        contents_a = keyed_a.get(key) or Counter()
        contents_b = keyed_b.get(key) or Counter()
        count_a = sum(contents_a.values())
        count_b = sum(contents_b.values())
        paired = min(count_a, count_b)
        removed += count_a - paired
        added += count_b - paired
        # Multiset intersection = maximum content-preserving matching within the
        # pairing; the remainder is the mismatch lower bound.
        text_changed += paired - sum((contents_a & contents_b).values())
    return {
        "added": added,
        "removed": removed,
        "text_changed": text_changed,
        "change_ratio": (added + removed + text_changed) / max(1, n_a, n_b),
        "app_changed": app_a != app_b,
        "title_changed": title_a != title_b,
    }


@dataclass(frozen=True)
class Transition:                          # v1.8: one M15 extract verdict for an adjacent frame pair
    index: int                             # 0-based member-pair ordinal; ALWAYS equals the position
                                           # in the rebuilt tuple (renumbered after member surgery)
    action: Mapping                        # object that passed action_schema
                                           # {"action_type","target","value","description"}
    model: str                             # provider model string of the extracting profile
    attempts: int                          # 1 + L3 repair calls
    detail: Mapping                        # fallback trace {"kind","message"} / {"reseamed": True} /
                                           # v1.9 thread-seam placeholder {"kind": "thread_seam",
                                           # "interrupted_by": [...]} (T10, zero-LLM);
                                           # {} for a clean extraction


@dataclass
class PipelineItem:                        # the ONLY mutable envelope; lifetime = one batch
    record: Record
    status: Status = "active"
    classification: Classification | None = None   # v1.7: written by M13 classify (or inherited)
    dedup: DedupInfo | None = None
    scores: dict[str, QualityScore] = field(default_factory=dict)
    annotation: Annotation | None = None
    verification: VerificationResult | None = None
    errors: list[StageError] = field(default_factory=list)
    session_id: str | None = None          # v1.8: stamped by M10 at envelope construction (stream
                                           # mode); M14 groups by it, M7 repair queries neighbors
    thread_id: str | None = None           # v1.9: stamped by M16 stitch on surviving thread
                                           # envelopes (== record.id == episode_id, T22); duck marks
                                           # seam_indexes / seam_interrupted_by / stitch_fragments
                                           # travel alongside (T20, copied by classify._fan_out)
    transitions: tuple[Transition, ...] | None = None   # v1.8: written by M15 extract
