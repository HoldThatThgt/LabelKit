"""v1.13 时间流形态的 M6 离线测试（SPEC-stream-generation §3.2/§3.9；零 LLM——
LLM 调用点经仓内既有 SchemaEngine 进程内桩先例路由，禁 mock 传输层）。

覆盖：计划期纯函数复演一致性与抽签顺序表钉板、交织器同 seed 双跑逐字节一致、
装箱/交叉形态/噪音容量/重复尾会话/ts 严格递增与会话间隔、工件行 truth 键集与
duplicate_of、直装组装约定（成员 id = M2 公式、序列 id = M14 公式、session_id
含噪音帧、inherited 两级、ref 行号）、canonical JSON 重放等价（工件行写临时
文件 → Ingestor 摄取 → 同 id 同会话切分）、sample_validator 整序列作废、
SimilarityFilter 序列单元、--limit 配额层截断与工件一致性、序列对半降级路径。
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import random
from dataclasses import replace
from datetime import datetime, timedelta
from types import SimpleNamespace

from labelkit.common.config.model import (
    AnnotateConfig,
    ClassSpec,
    ClassView,
    ClassifyConfig,
    ConsoleConfig,
    DedupConfig,
    ExtractConfig,
    FrameClassView,
    FrameClassifyConfig,
    GenerateConfig,
    GenerateStreamConfig,
    GenerateStyle,
    InputConfig,
    OutputConfig,
    QualityConfig,
    ResolvedConfig,
    Rubric,
    RunConfig,
    SegmentConfig,
    StitchConfig,
    StreamConfig,
    ToolConfig,
    TraceConfig,
    VerifyConfig,
)
from labelkit.common.errors import ContextOverflowError, SchemaViolation
from labelkit.common.contracts.types import Usage
from labelkit.operators.generate import (
    GenerateStage,
    NoiseCallPlan,
    SequencePlan,
    StreamGenerateProduct,
    expand_stream_quota,
    plan_stream,
    predraw_llm_style,
    stream_artifact_path,
)


# ── 夹具：直构 ResolvedConfig（M1 形状）与进程内 SchemaEngine 桩 ─────────────

FRAME_SCHEMA = {"type": "object", "properties": {"utterance": {"type": "string"}},
                "required": ["utterance"], "additionalProperties": False}


def mk_view(name: str, *, sequences: int, len_range=(2, 3),
            instruction: str = "生成序列", styles=()) -> ClassView:
    return ClassView(
        name=name, quality=QualityConfig(), rubric=Rubric(name="r", criteria=()),
        annotate=AnnotateConfig(), verify=VerifyConfig(), extract=ExtractConfig(),
        generate=GenerateConfig(enabled=True, instruction=instruction,
                                sequences=sequences, len_range=len_range,
                                styles=tuple(styles)))


def frame_view(gen_instruction: str, gen_schema=None) -> FrameClassView:
    return FrameClassView(instruction="", examples=(), enabled=True,
                          gen_instruction=gen_instruction, gen_schema=gen_schema)


def mk_cfg(*, quotas: dict[str, int] | None = None, sessions: int = 2,
           noise_ratio: float = 0.0, duplicates: int = 0,
           limit: int | None = None, generate: GenerateConfig | None = None,
           len_range=(2, 3), session_max_len: int = 200,
           llm_profiles=None) -> ResolvedConfig:
    quotas = quotas if quotas is not None else {"booking": 2, "smalltalk": 1}
    base_generate = generate or GenerateConfig(enabled=True, num_per_call=2)
    views = {name: replace(mk_view(name, sequences=n, len_range=len_range),
                           generate=replace(base_generate, instruction=f"生成{name}",
                                            sequences=n, len_range=len_range))
             for name, n in quotas.items()}
    return ResolvedConfig(
        tool=ToolConfig(), console=ConsoleConfig(),
        llm_profiles=llm_profiles or {}, embedding_profiles={},
        run=RunConfig(output="out/synth-labels.jsonl", modality="text",
                      mode="generate_only", seed=0),
        input=InputConfig(),
        stream=StreamConfig(order_by="meta:ts", gap_s=900,
                            session_max_len=session_max_len),
        dedup=DedupConfig(), segment=SegmentConfig(), stitch=StitchConfig(),
        extract=ExtractConfig(),
        classify=ClassifyConfig(
            enabled=True,
            classes=tuple(ClassSpec(name=n, description="d") for n in sorted(quotas))),
        quality=QualityConfig(),
        generate=base_generate,
        annotate=AnnotateConfig(), verify=VerifyConfig(),
        output=OutputConfig(schema_inline="{}"), trace=TraceConfig(),
        rubric=Rubric(name="r", criteria=()),
        class_views=views,
        user_schema={"type": "object"}, limit=limit, strict=False, dry_run=False,
        config_path="config.toml", project_path="project.toml",
        config_digest="sha256:0", project_digest="sha256:0",
        frame_classify=FrameClassifyConfig(
            classes=(ClassSpec(name="task_request", description="d"),
                     ClassSpec(name="followup", description="d"))),
        frame_class_views={"task_request": frame_view("生成请求", FRAME_SCHEMA),
                           "followup": frame_view("生成跟进")},
        generate_stream=GenerateStreamConfig(
            enabled=True, sessions=sessions, noise_ratio=noise_ratio,
            noise_instruction="生成噪音" if noise_ratio > 0 else "",
            duplicates=duplicates, frame_gap_s=(5.0, 60.0),
            ts_start="2026-01-01T09:00:00+08:00"),
    )


class StreamEngine:
    """进程内 SchemaEngine 桩（v1.12 _FrameEngine 先例）：按 schema 形状路由——
    "steps" = 蓝图、"frames" = 帧实现（逐位按 prefixItems 产内容）、"samples" =
    噪音批；fail_plans/fail_realize 按调用序号注入既定异常（修复穷尽/溢出形）。"""

    def __init__(self, fail_plans=(), fail_realize=(), realize_overflow=0,
                 frame_text=None):
        self.calls = []                        # (profile, prompt, schema)
        self.fail_plans = set(fail_plans)      # 第 n 次蓝图调用抛 SchemaViolation
        self.fail_realize = set(fail_realize)  # 第 n 次实现调用抛 SchemaViolation
        self.realize_overflow = realize_overflow   # 前 n 次实现调用抛 reactive 溢出
        self.frame_text = frame_text           # (call_no, i) -> str 的定制器
        self.plan_no = 0
        self.realize_no = 0
        self.noise_no = 0

    async def complete_validated(self, profile, prompt, schema=None, *, scope):
        self.calls.append((profile, prompt, schema))
        props = schema["properties"]
        if "steps" in props:
            self.plan_no += 1
            if self.plan_no in self.fail_plans:
                raise SchemaViolation(["/steps: 枚举违规"], "{}")
            length = props["steps"]["minItems"]
            names = props["steps"]["items"]["properties"]["frame_class"]["enum"]
            steps = [{"frame_class": names[i % len(names)],
                      "brief": f"step {self.plan_no}-{i}"} for i in range(length)]
            return {"steps": steps}, Usage(1, 1), 1, "m"
        if "frames" in props:
            self.realize_no += 1
            if self.realize_no in self.fail_realize:
                raise SchemaViolation(["/frames: 违规"], "{}")
            if self.realize_no <= self.realize_overflow:
                raise ContextOverflowError("prompt too long", phase="reactive")
            frames = []
            for i, sub in enumerate(props["frames"]["prefixItems"]):
                if sub.get("type") == "string":
                    frames.append(self._text(self.realize_no, i))
                else:
                    frames.append({"utterance": self._text(self.realize_no, i)})
            return {"frames": frames}, Usage(1, 1), 1, "m"
        self.noise_no += 1
        n = props["samples"]["minItems"]
        return {"samples": [f"噪音闲聊第 {self.noise_no}-{i} 句" for i in range(n)]}, \
            Usage(1, 1), 1, "m"

    def _text(self, call_no: int, i: int) -> str:
        if self.frame_text is not None:
            return self.frame_text(call_no, i)
        return f"帧内容 {call_no}-{i}"


class Metrics:
    def __init__(self):
        self.counters: dict[str, int] = {}
        self.events = []
        self.fed: list[bool] = []          # record_provider_result 捕获（A7 喂给面）

    def count(self, key, n=1):
        self.counters[key] = self.counters.get(key, 0) + n

    def event(self, ev, **kw):
        self.events.append((ev, kw))

    def record_provider_result(self, fatal, **kw):
        self.fed.append(fatal)


def run_stream(cfg, engine=None, metrics=None) -> tuple[StreamGenerateProduct, StreamEngine, Metrics]:
    engine = engine or StreamEngine()
    metrics = metrics or Metrics()
    ctx = SimpleNamespace(cfg=cfg, schema_engine=engine, metrics=metrics,
                          batch_no=0, rng=random.Random(f"{cfg.run.seed}:0:generate"),
                          llm=None)
    product = asyncio.run(GenerateStage(cfg).generate_stream_all(ctx))
    return product, engine, metrics


def parse_lines(product: StreamGenerateProduct) -> list[dict]:
    return [json.loads(line) for line in product.artifact_lines]


# ── 计划期纯函数：复演一致性与抽签顺序表钉板 ────────────────────────────────

def test_plan_stream_same_seed_replays_identically():
    cfg = mk_cfg(noise_ratio=0.3)
    a = plan_stream(cfg, random.Random("0:0:generate"))
    b = plan_stream(cfg, random.Random("0:0:generate"))
    assert a == b


def test_quota_expansion_lexicographic_with_ordinals_and_limit():
    cfg = mk_cfg(quotas={"zeta": 1, "alpha": 2})
    assert expand_stream_quota(cfg) == [("alpha", 0), ("alpha", 1), ("zeta", 0)]
    # --limit 前缀截断在配额层：作废序列不再生成、不进交织
    assert expand_stream_quota(replace(cfg, limit=2)) == [("alpha", 0), ("alpha", 1)]


def test_draw_order_pinned_lengths_then_llm_style_then_noise():
    """顺序表钉板（裁决·抽签消费顺序表）：②全部序列 L → ③序列 (llm, style) 预抽
    紧接噪音批预抽，同一 rng 流——用独立 Random 按文档顺序手工复演逐项对齐。"""
    styles = (GenerateStyle(name="s1", prompt="p1"), GenerateStyle(name="s2", prompt="p2"))
    generate = GenerateConfig(enabled=True, llms=("a", "b"), mixture="weighted",
                              weights=(1.0, 3.0), styles=styles, num_per_call=2)
    cfg = mk_cfg(quotas={"booking": 2, "smalltalk": 1}, noise_ratio=0.5,
                 generate=generate)
    plan = plan_stream(cfg, random.Random("0:0:generate"))

    rng = random.Random("0:0:generate")
    entries = expand_stream_quota(cfg)
    lengths = []
    for name, _ in entries:
        lo, hi = cfg.class_views[name].generate.len_range
        lengths.append(rng.randint(lo, hi))                      # ② 逐序列 L
    noise_target = round(0.5 * sum(lengths))
    n_noise = -(-noise_target // 2)
    styles_by_index = [cfg.class_views[name].generate.styles for name, _ in entries]
    pairs = predraw_llm_style(generate, len(entries) + n_noise, rng,
                              styles_by_index=styles_by_index + [styles] * n_noise)
    assert [p.length for p in plan.sequences] == lengths
    assert [(p.llm, p.style_name) for p in plan.sequences] == [
        (llm, style.name if style else None) for llm, style in pairs[: len(entries)]]
    assert plan.noise_target == noise_target
    assert [(p.llm, p.style_name) for p in plan.noise_plans] == [
        (llm, style.name if style else None) for llm, style in pairs[len(entries):]]


def test_plan_blueprint_and_realize_bind_same_profile_and_style_only_realize():
    """蓝图+实现绑定同一 profile；style 只进实现段、蓝图不带（生成键效力矩阵）。"""
    styles = (GenerateStyle(name="formal", prompt="正式语气"),)
    cfg = mk_cfg(quotas={"booking": 1},
                 generate=GenerateConfig(enabled=True, llms=("a", "b"),
                                         styles=styles, num_per_call=2))
    product, engine, _ = run_stream(cfg)
    plan_calls = [c for c in engine.calls if "steps" in c[2]["properties"]]
    realize_calls = [c for c in engine.calls if "frames" in c[2]["properties"]]
    assert plan_calls[0][0] == realize_calls[0][0] == "a"    # round_robin index 0
    plan_system = plan_calls[0][1].messages[0].parts[0].text
    realize_system = realize_calls[0][1].messages[0].parts[0].text
    assert "[风格要求] 正式语气" not in plan_system
    assert "[风格要求] 正式语气" in realize_system
    (env,) = product.envelopes
    assert env.record.members[0].ref.generator == {"llm": "a", "style": "formal"}


# ── 交织器：确定性、装箱、交叉、噪音、重复、ts ──────────────────────────────

def test_same_seed_double_run_byte_identical():
    cfg = mk_cfg(noise_ratio=0.3, duplicates=1)
    first, _, _ = run_stream(cfg)
    second, _, _ = run_stream(cfg)
    assert first.artifact_lines == second.artifact_lines
    assert [e.record.id for e in first.envelopes] == [e.record.id
                                                      for e in second.envelopes]
    assert [e.session_id for e in first.envelopes] == [e.session_id
                                                       for e in second.envelopes]


def test_draw_order_pinned_weave_phase_sample_shuffle_cross_noise_ts():
    """顺序表钉板（裁决·抽签消费顺序表交织期④–⑨）：独立 Random 先经真实
    plan_stream 消费计划期①②③，再按 ④sample 重复选取 → ⑤shuffle 装箱洗牌 →
    ⑥逐交叉会话两次 randint 切换点 → ⑦逐噪音帧 choice+randint → ⑧零消费 →
    ⑨逐帧 uniform 铺 ts 的文档顺序手工复演，与真实产物的行序/会话构成/切换点/
    噪音落位/ts 序列逐项对齐——任何两步对调都会使工件字节漂移并在此翻红。"""
    cfg = mk_cfg(quotas={"booking": 2, "smalltalk": 1}, sessions=2,
                 noise_ratio=0.5, duplicates=1)
    product, _, _ = run_stream(cfg)
    rows = parse_lines(product)

    rng = random.Random("0:0:generate")
    plan = plan_stream(cfg, rng)                            # ①②③（另有独立钉板）
    entries = expand_stream_quota(cfg)
    lengths = [p.length for p in plan.sequences]
    n = len(entries)
    dup_pick = rng.sample(list(range(n)), 1)                # ④（桩引擎全部存活）
    order = list(range(n))
    rng.shuffle(order)                                      # ⑤
    n_cross = n - min(cfg.generate_stream.sessions, n)
    sessions: list[list[tuple]] = []
    for pair in range(n_cross):                             # ⑥ 成对交叉
        a, b = order[2 * pair], order[2 * pair + 1]
        if lengths[a] < 2 <= lengths[b]:
            a, b = b, a
        slots_a = [(a, i) for i in range(lengths[a])]
        slots_b = [(b, i) for i in range(lengths[b])]
        if lengths[a] < 2 and lengths[b] < 2:
            sessions.append(slots_a + slots_b)              # 退化：零消费顺次拼接
            continue
        cut_a = rng.randint(1, len(slots_a) - 1)
        cut_b = rng.randint(1, len(slots_b))
        sessions.append(slots_a[:cut_a] + slots_b[:cut_b]
                        + slots_a[cut_a:] + slots_b[cut_b:])
    for idx in order[2 * n_cross:]:
        sessions.append([(idx, i) for i in range(lengths[idx])])
    for _ in range(plan.noise_target):                      # ⑦（max_len 恒未触顶）
        target = rng.choice(sessions)
        target.insert(rng.randint(0, len(target)), ("noise", None))
    for src in dup_pick:                                    # ⑧ 零消费，流尾新会话
        sessions.append([("dup", src)] * lengths[src])
    lo, hi = cfg.generate_stream.frame_gap_s
    gap = float(cfg.stream.gap_s)
    current = datetime.fromisoformat(cfg.generate_stream.ts_start)
    expected_ts: list[str] = []
    first = True
    for session in sessions:
        for position in range(len(session)):
            if first:
                first = False
            elif position == 0:
                current += timedelta(seconds=rng.uniform(gap + lo, gap + hi))  # ⑨
            else:
                current += timedelta(seconds=rng.uniform(lo, hi))
            expected_ts.append(current.isoformat(timespec="microseconds"))

    # 对齐一：ts 序列逐字节相等（④–⑦ 的结构决定行序，⑨ 的消费序决定数值）
    assert [row["ts"] for row in rows] == expected_ts
    # 对齐二：逐行归属（会话号 / 序列身份 / 噪音 / 重发）
    flat = [(s_no, slot) for s_no, sess in enumerate(sessions) for slot in sess]
    assert len(rows) == len(flat)
    for row, (s_no, slot) in zip(rows, flat):
        truth = row["truth"]
        assert truth["session"] == s_no
        if slot[0] == "noise":
            assert truth["noise"] is True and truth["sequence_class"] is None
        elif slot[0] == "dup":
            cls, ordinal = entries[slot[1]]
            assert truth["duplicate_of"] == ordinal
            assert truth["sequence_class"] == cls and truth["sequence"] is None
        else:
            cls, ordinal = entries[slot[0]]
            assert truth["sequence_class"] == cls and truth["sequence"] == ordinal
            assert truth["noise"] is False


def test_session_packing_and_true_cross_shape():
    """sessions_eff = min(sessions, Σ幸存)；交叉会话数 = Σ幸存 − sessions_eff；
    交叉形态 A 段+B 段+A 余段[+B 余段]——A 必在 B 头部之后回续（真交叉）。"""
    cfg = mk_cfg(quotas={"booking": 2, "smalltalk": 1}, sessions=2)
    product, _, _ = run_stream(cfg)
    rows = parse_lines(product)
    by_session: dict[int, list[dict]] = {}
    for row in rows:
        by_session.setdefault(row["truth"]["session"], []).append(row)
    assert len(by_session) == 2                      # 3 序列装 2 会话 ⇒ 1 交叉
    crossed = [rows_ for rows_ in by_session.values()
               if len({(r["truth"]["sequence_class"], r["truth"]["sequence"])
                       for r in rows_}) == 2]
    assert len(crossed) == 1
    keys = [(r["truth"]["sequence_class"], r["truth"]["sequence"])
            for r in crossed[0]]
    a_key = keys[0]                                  # A = 会话首帧属主
    a_at = [i for i, k in enumerate(keys) if k == a_key]
    b_at = [i for i, k in enumerate(keys) if k != a_key]
    # 真交叉：B 的首帧落在 A 首末帧之间（A 段 → B 段 → A 余段）
    assert a_at[0] < b_at[0] < a_at[-1]
    # 序列内帧序保持：每属主的行内 frame_class 序 = 蓝图步序（连续下标单调）
    assert a_at == sorted(a_at) and b_at == sorted(b_at)


def test_noise_respects_session_max_len_and_leftover_absent():
    cfg = mk_cfg(quotas={"booking": 2}, sessions=1, noise_ratio=0.9,
                 len_range=(3, 3), session_max_len=7)
    product, _, metrics = run_stream(cfg)
    rows = parse_lines(product)
    by_session: dict[int, list[dict]] = {}
    for row in rows:
        by_session.setdefault(row["truth"]["session"], []).append(row)
    assert all(len(v) <= 7 for v in by_session.values())
    noise_rows = [r for r in rows if r["truth"]["noise"]]
    # 任务帧 6、目标 round(0.9×6)=5，容量只装得下 1 帧 ⇒ 余 4 帧缺席不补
    assert len(noise_rows) == 1
    assert metrics.counters["generate.stream.noise_frames"] == 1
    for row in noise_rows:
        assert (row["truth"]["sequence_class"], row["truth"]["sequence"],
                row["truth"]["frame_class"]) == (None, None, None)


def test_duplicates_land_as_tail_sessions_byte_identical():
    cfg = mk_cfg(quotas={"booking": 2, "smalltalk": 1}, sessions=2, duplicates=1)
    product, _, metrics = run_stream(cfg)
    rows = parse_lines(product)
    dup_rows = [r for r in rows if "duplicate_of" in r["truth"]]
    assert dup_rows, "duplicates=1 必产重复帧"
    dup_session = {r["truth"]["session"] for r in dup_rows}
    assert dup_session == {max(r["truth"]["session"] for r in rows)}   # 流尾新会话
    source_class = dup_rows[0]["truth"]["sequence_class"]
    source_ordinal = dup_rows[0]["truth"]["duplicate_of"]
    source_rows = [r for r in rows
                   if r["truth"]["sequence_class"] == source_class
                   and r["truth"]["sequence"] == source_ordinal]
    # 帧 text_field 值逐字节同源；truth.sequence = null（重发无自身计划期身份）
    assert [r["text"] for r in dup_rows] == [r["text"] for r in source_rows]
    assert all(r["truth"]["sequence"] is None for r in dup_rows)
    # 重复序列不进 envelopes：信封数 = 幸存序列数（3），会话数不含重复尾会话
    assert len(product.envelopes) == 3
    assert metrics.counters["generate.stream.sessions"] == 2
    assert metrics.counters["generate.stream.duplicates"] == 1


def test_timestamps_strictly_increasing_with_session_gap_contract():
    cfg = mk_cfg(quotas={"booking": 2, "smalltalk": 1}, sessions=2,
                 noise_ratio=0.2, duplicates=1)
    product, _, _ = run_stream(cfg)
    rows = parse_lines(product)
    stamps = [datetime.fromisoformat(r["ts"]) for r in rows]
    assert stamps[0] == datetime.fromisoformat("2026-01-01T09:00:00+08:00")
    assert rows[0]["ts"] == "2026-01-01T09:00:00.000000+08:00"   # 微秒精度写出
    assert all(a < b for a, b in zip(stamps, stamps[1:]))        # 严格递增
    sessions = [r["truth"]["session"] for r in rows]
    for i in range(1, len(rows)):
        gap = (stamps[i] - stamps[i - 1]).total_seconds()
        if sessions[i] != sessions[i - 1]:
            assert 900 + 5.0 <= gap <= 900 + 60.0    # 会话间隔 uniform(gap_s+lo, gap_s+hi)
        else:
            assert 5.0 <= gap <= 60.0                # 帧间隔 uniform(frame_gap_s)


# ── 工件行：truth 键集冻结与真值不携最终 id ─────────────────────────────────

def test_artifact_row_shape_and_truth_key_set_frozen():
    cfg = mk_cfg(noise_ratio=0.3, duplicates=1)
    product, _, _ = run_stream(cfg)
    member_ids = {m.id for env in product.envelopes for m in env.record.members}
    episode_ids = {env.record.id for env in product.envelopes}
    for row in parse_lines(product):
        assert list(row)[:2] == ["ts", "text"] and list(row)[-1] == "truth"
        truth = row["truth"]
        base = ["session", "sequence_class", "sequence", "frame_class", "noise"]
        assert list(truth) in (base, base + ["duplicate_of"])
        if truth["noise"]:
            assert (truth["sequence_class"], truth["sequence"],
                    truth["frame_class"]) == (None, None, None)
        # 真值不携最终 id（裁决·真值不携最终 id）：任何装配后 id 不得出现在 truth
        assert not (set(map(str, truth.values())) & (member_ids | episode_ids))


# ── 直装组装：id 公式、ref、两级 inherited、session_id ──────────────────────

def test_direct_assembly_id_formulas_and_refs():
    cfg = mk_cfg(noise_ratio=0.2, duplicates=1)
    product, _, _ = run_stream(cfg)
    rows = parse_lines(product)

    def m2_id(obj) -> str:
        canonical = json.dumps(obj, sort_keys=True, ensure_ascii=False,
                               separators=(",", ":"))
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]

    line_ids = [m2_id(row) for row in rows]
    by_session: dict[int, list[str]] = {}
    for row, rec_id in zip(rows, line_ids):
        by_session.setdefault(row["truth"]["session"], []).append(rec_id)
    session_id_of = {no: hashlib.sha256("\n".join(ids).encode("utf-8"))
                     .hexdigest()[:16] for no, ids in by_session.items()}

    path = stream_artifact_path(cfg)
    for env in product.envelopes:
        members = env.record.members
        # 成员 id = M2 公式（工件行全对象为 raw ⇒ 重放同 id）；行号 = 列表序 + 1
        for member in members:
            assert member.id == line_ids[member.ref.line_no - 1]
            assert member.ref.source_file == path
            assert member.ref.pair_index is None
            assert member.ref.generated_from == ()
            row = rows[member.ref.line_no - 1]
            assert member.raw == row
            expected_text = (row["text"] if isinstance(row["text"], str)
                             else json.dumps(row["text"], sort_keys=True,
                                             ensure_ascii=False,
                                             separators=(",", ":")))
            assert member.text == expected_text
        # 序列 id = M14 公式；S24 字段惯例 + ref = 首成员 ref
        joined = "\n".join(m.id for m in members)
        assert env.record.id == hashlib.sha256(joined.encode("utf-8")).hexdigest()[:16]
        assert env.record.kind == "sequence"
        assert (env.record.text, env.record.raw, env.record.ui_tree,
                env.record.image) == (None, None, None, None)
        assert env.record.ref is members[0].ref
        # session_id = M2 公式，含噪音帧与重复帧（该会话全部行 id）
        first_row = rows[members[0].ref.line_no - 1]
        assert env.session_id == session_id_of[first_row["truth"]["session"]]
        # 两级 inherited：序列级 = truth.sequence_class；帧级 = truth.frame_class
        assert env.classification.source == "inherited"
        assert env.classification.label == first_row["truth"]["sequence_class"]
        assert env.classification.labels == (env.classification.label,)
        for member in members:
            cls = env.member_classifications[member.id]
            row = rows[member.ref.line_no - 1]
            assert (cls.label, cls.source) == (row["truth"]["frame_class"],
                                               "inherited")


def test_envelopes_returned_in_plan_order():
    cfg = mk_cfg(quotas={"booking": 2, "smalltalk": 1})
    product, _, _ = run_stream(cfg)
    keys = [(e.classification.label,
             json.loads(json.dumps(e.record.members[0].raw))["truth"]["sequence"])
            for e in product.envelopes]
    assert keys == [("booking", 0), ("booking", 1), ("smalltalk", 0)]


def test_artifact_path_matches_emitter_channel_rule():
    from datetime import timezone

    from labelkit.operators.emitter import Emitter

    cfg = mk_cfg()
    engine = SimpleNamespace(validate_only=lambda *a, **k: [])
    emitter = Emitter(cfg, engine, "run0", datetime.now(timezone.utc))
    assert stream_artifact_path(cfg) == str(emitter._artifact_path)


# ── canonical JSON 重放等价：工件行 → M2 摄取 → 同 id 同会话 ────────────────

def test_artifact_replay_reingests_same_ids_and_sessions(tmp_path):
    cfg = mk_cfg(quotas={"booking": 2, "smalltalk": 1}, sessions=2,
                 noise_ratio=0.2, duplicates=1)
    product, _, _ = run_stream(cfg)
    artifact = tmp_path / "events.jsonl"
    artifact.write_text("\n".join(product.artifact_lines) + "\n", encoding="utf-8")

    from labelkit.operators.ingest import Ingestor

    replay_cfg = replace(
        cfg,
        run=replace(cfg.run, mode="process", input=str(artifact)),
        segment=SegmentConfig(enabled=True),
        generate=GenerateConfig(),
        generate_stream=GenerateStreamConfig(),
        limit=None)
    sessions = list(Ingestor(replay_cfg).sessions())
    # 会话切分复演：会话数 = sessions_eff + duplicates（会话间隔恒 > gap_s）
    assert len(sessions) == 2 + 1
    replay_ids = [r.id for s in sessions for r in s.records]
    expected_ids = [
        hashlib.sha256(json.dumps(json.loads(line), sort_keys=True,
                                  ensure_ascii=False, separators=(",", ":"))
                       .encode("utf-8")).hexdigest()[:16]
        for line in product.artifact_lines]
    assert replay_ids == expected_ids                    # 成员 id 逐字节一致
    # 直装 session_id 与重放会话 id 同式同值（M2 公式，含噪音/重复帧）
    replay_session_ids = {s.session_id for s in sessions}
    assert {env.session_id for env in product.envelopes} <= replay_session_ids
    # text 投影等价：成员 text == 重放记录 text
    replay_text = {r.id: r.text for s in sessions for r in s.records}
    for env in product.envelopes:
        for member in env.record.members:
            assert replay_text[member.id] == member.text


# ── 作废语义：蓝图/实现失败、逐帧钩子、相似度过滤、降级 ─────────────────────

def test_plan_failure_voids_sequence_without_failed_record():
    cfg = mk_cfg(quotas={"booking": 2}, sessions=2)
    product, _, metrics = run_stream(cfg, engine=StreamEngine(fail_plans={1}))
    assert len(product.envelopes) == 1
    assert metrics.counters["generate.stream.plan_failures"] == 1
    assert metrics.counters["generate.stream.plan_calls"] == 2
    # 作废序列的帧从交织缺席：工件只覆盖幸存序列
    assert {r["truth"]["sequence"] for r in parse_lines(product)} == {1}


def test_realize_failure_voids_sequence():
    cfg = mk_cfg(quotas={"booking": 2}, sessions=2)
    product, _, metrics = run_stream(cfg, engine=StreamEngine(fail_realize={1}))
    assert len(product.envelopes) == 1
    assert metrics.counters["generate.stream.realize_failures"] == 1


def test_realize_reactive_overflow_halves_and_succeeds():
    cfg = mk_cfg(quotas={"booking": 1}, sessions=1, len_range=(4, 4))
    engine = StreamEngine(realize_overflow=1)     # 全长调用溢出，两半各自成功
    product, engine, metrics = run_stream(cfg, engine=engine)
    (env,) = product.envelopes
    assert len(env.record.members) == 4           # 两半拼接回全长
    assert metrics.counters["budget.degrade_retries"] == 1
    assert metrics.counters["generate.stream.realize_calls"] == 3   # 全长 + 两半
    realize_schemas = [c[2] for c in engine.calls if "frames" in c[2]["properties"]]
    assert [s["properties"]["frames"]["minItems"] for s in realize_schemas] == [4, 2, 2]


def test_realize_overflow_exhaustion_voids_sequence():
    cfg = mk_cfg(quotas={"booking": 1}, sessions=1, len_range=(4, 4))
    product, _, metrics = run_stream(cfg, engine=StreamEngine(realize_overflow=99))
    assert product.envelopes == [] and product.artifact_lines == []
    assert metrics.counters["generate.stream.realize_failures"] == 1
    assert metrics.counters["budget.degrade_retries"] == 2   # (0,4)→(0,2)→(0,1) 触底
    assert metrics.counters["generate.stream.realize_calls"] == 3
    assert metrics.fed == [True]      # 降级穷尽的 reactive-400 终局恰一次喂熔断（A7）


def test_sample_validator_scraps_whole_sequence(monkeypatch):
    import labelkit.operators.generate as generate_module

    def hook(text):
        return ["禁词命中"] if "1-0" in text else []

    monkeypatch.setattr(
        "labelkit.common.extensions.hooks.resolve_hook", lambda ref: hook)
    generate = GenerateConfig(enabled=True, num_per_call=2,
                              sample_validator="mod:fn")
    cfg = mk_cfg(quotas={"booking": 2}, sessions=2, generate=generate)
    product, _, metrics = run_stream(cfg)
    assert len(product.envelopes) == 1            # 首序列整条作废（拒绝采样语义）
    assert metrics.counters["generate.stream.validator_scrapped"] == 1
    assert metrics.counters[
        "generate.buckets.booking×default×null.rejected_by_validator"] == 1
    assert generate_module is not None


def test_similarity_filter_runs_at_sequence_unit():
    text = "这是一条完全相同的帧内容，足够长以便产生五元组切片，用于近重判定。"
    engine = StreamEngine(frame_text=lambda call_no, i: f"{text}第{i}帧")
    cfg = mk_cfg(quotas={"booking": 2}, sessions=2, len_range=(3, 3))
    product, _, metrics = run_stream(cfg, engine=engine)
    assert len(product.envelopes) == 1            # 兄弟序列近重 ⇒ 第二条淘汰
    bucket = "generate.buckets.booking×default×null"
    assert metrics.counters[f"{bucket}.produced"] == 2
    assert metrics.counters[f"{bucket}.survived_dedup"] == 1
    assert metrics.counters["generate.stream.sequences.booking.produced"] == 1
    # 淘汰序列不进工件（交织前过滤）
    assert {r["truth"]["sequence"] for r in parse_lines(product)} == {0}


def test_limit_truncates_quota_and_artifact_consistently():
    cfg = mk_cfg(quotas={"booking": 2, "smalltalk": 2}, sessions=3, limit=2)
    product, _, metrics = run_stream(cfg)
    assert len(product.envelopes) == 2
    rows = parse_lines(product)
    assert {(r["truth"]["sequence_class"], r["truth"]["sequence"])
            for r in rows} == {("booking", 0), ("booking", 1)}
    assert metrics.counters["generate.stream.sequences.booking.planned"] == 2
    assert "generate.stream.sequences.smalltalk.planned" not in metrics.counters


def test_bucket_keys_three_segment_for_sequences_two_for_noise():
    cfg = mk_cfg(quotas={"booking": 1}, sessions=1, noise_ratio=0.5,
                 len_range=(2, 2))
    _, _, metrics = run_stream(cfg)
    assert metrics.counters["generate.buckets.booking×default×null.calls"] == 2
    assert metrics.counters["generate.buckets.default×null.calls"] == 1


def test_generate_all_flat_path_untouched():
    """平面路径零改动锚：generate_all 的签名与既有测试族已覆盖——此处只锁
    generate_stream_all 不触碰平面计数（无 seed 池/独立计数键）。"""
    cfg = mk_cfg(quotas={"booking": 1}, sessions=1)
    _, _, metrics = run_stream(cfg)
    assert "counts.generated" not in metrics.counters   # M10 属主，M6 不碰


def test_real_product_flows_through_real_emitter(tmp_path):
    """M6 × M11 接缝：真产物过真 Emitter——工件通道落盘 + 主输出行的
    _meta.stream（或门在场、members label = 帧类真值、order_span/member_sources
    指向工件行号）与工件逐行对得上。"""
    from datetime import datetime, timezone

    from jsonschema import Draft202012Validator

    from labelkit.operators.emitter import Emitter

    cfg = mk_cfg(quotas={"booking": 2, "smalltalk": 1}, sessions=2,
                 noise_ratio=0.2, duplicates=1)
    cfg = replace(cfg, run=replace(cfg.run, output=str(tmp_path / "labels.jsonl")),
                  annotate=AnnotateConfig(enabled=False))
    product, _, _ = run_stream(cfg)

    class EngineStub:
        def validate_only(self, obj, schema=None):
            active = schema if schema is not None else {"type": "object"}
            return [e.message for e in Draft202012Validator(active).iter_errors(obj)]

    emitter = Emitter(cfg, EngineStub(), "run0", datetime.now(timezone.utc))
    emitter.open()
    emitter.write_stream_artifact(product.artifact_lines)
    result = emitter.emit_batch(product.envelopes, 1)
    emitter.finalize({"counts": {}}, deliver=True)

    assert result.emitted == len(product.envelopes) and result.rejected == 0
    artifact = tmp_path / "labels.stream.jsonl"
    assert artifact.read_text(encoding="utf-8").splitlines() == product.artifact_lines
    rows = [json.loads(line)
            for line in (tmp_path / "labels.jsonl").read_text("utf-8").splitlines()]
    artifact_rows = parse_lines(product)
    for row, env in zip(rows, product.envelopes):
        stream = row["_meta"]["stream"]
        assert stream is not None                    # 或门：generate_stream 开
        assert stream["episode_id"] == env.record.id
        assert stream["session_id"] == env.session_id
        first, last = env.record.members[0], env.record.members[-1]
        assert stream["order_span"] == [
            f"{first.ref.source_file}:{first.ref.line_no}",
            f"{last.ref.source_file}:{last.ref.line_no}"]
        for entry, member in zip(stream["members"], env.record.members):
            truth = artifact_rows[member.ref.line_no - 1]["truth"]
            assert list(entry) == ["index", "id", "label"]
            assert entry["id"] == member.id
            assert entry["label"] == truth["frame_class"]     # 真值门
        assert (stream["session_split"], stream["repaired"],
                stream["degraded"], stream["steps"]) == (False, False, None, None)
