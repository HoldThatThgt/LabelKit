"""v1.13 时间流形态的 M6 离线测试（SPEC-stream-generation §3.2/§3.9；零 LLM——
LLM 调用点经仓内既有 SchemaEngine 进程内桩先例路由，禁 mock 传输层）。

覆盖：计划期纯函数复演一致性与抽签顺序表钉板、交织器同 seed 双跑逐字节一致、
装箱/交叉形态/噪音容量/重复尾会话/ts 严格递增与会话间隔、工件行 truth 键集与
duplicate_of、直装组装约定（成员 id = M2 公式、序列 id = M14 公式、session_id
含噪音帧、inherited 两级、ref 行号）、canonical JSON 重放等价（工件行写临时
文件 → Ingestor 摄取 → 同 id 同会话切分）、sample_validator 整序列作废、
SimilarityFilter 序列单元、--limit 配额层截断与工件一致性、序列对半降级路径。

v1.14 档位面（SPEC-generation-tiers §3.2/§3.7）另覆盖：档位映射的连续分块与
``--limit`` 交换律、配分零抽签消费（同 seed 有无档位表抽签流逐字节一致）、蓝图的
档内子集表与覆盖句冻结文本、truth 键序三形态（任务/噪音/重发）、``ref.generator``
三键、逐档 planned/produced 落账。

v1.15 按类档位表（SPEC-per-class-tiers §3.2/§3.6）再覆盖：混合形态（一类声明 / 一类
回落）的计划期 rank 映射与 ``--limit`` 逐类分块、蓝图取本类生效表的档内子集、truth 与
``ref.generator`` 逐行与本类生效表一致、逐档计数器的类段键、按类配分仍零抽签消费、
同 seed 双跑逐字节一致，以及"按类表逐字段等于全局表 ⇒ 与全部缺省等价"的退化面。

v1.14 时间字段面（SPEC-generation-tiers §3.3/§3.7）再覆盖：缩减 Schema 派生
（properties 删键 / required 差集 / 其余关键字原样 / 不污染 M1 冻结产物 / 两个面
同源）、回填算术（首末边界 0.0、序内相邻口径含交叉夹帧、微秒精度、同 seed 双跑
确定性）、重发帧的同源载荷与承源 ts、回填后计 id 与工件重放往返、回填前钩子口径，
以及绑定表全缺省时的双关字节等价。
"""
from __future__ import annotations

import asyncio
import copy
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
    TierSpec,
    ToolConfig,
    TraceConfig,
    VerifyConfig,
    apportion_tiers,
)
from labelkit.common.errors import ContextOverflowError, SchemaViolation
from labelkit.common.contracts.types import Usage
from labelkit.operators.generate import (
    GenerateStage,
    NoiseCallPlan,
    RealizedSequence,
    SequencePlan,
    StreamGenerateProduct,
    _reduced_gen_schema,
    _StreamSlot,
    backfill_time_fields,
    expand_stream_quota,
    plan_stream,
    predraw_llm_style,
    render_plan_prompt_texts,
    stream_artifact_path,
    tier_rank_for_ordinal,
    weave_stream,
)


# ── 夹具：直构 ResolvedConfig（M1 形状）与进程内 SchemaEngine 桩 ─────────────

FRAME_SCHEMA = {"type": "object", "properties": {"utterance": {"type": "string"}},
                "required": ["utterance"], "additionalProperties": False}
FRAME_TABLE = ("task_request", "followup")      # 声明序（档内子集渲染按此序过滤）

# v1.14 绑定形态夹具：结构化帧类的生成 Schema 覆盖语义词表四值 + 一个自由字段；
# since_prev/elapsed 故意不写进 required——required 差集语义的容忍面。
TIMED_SCHEMA = {"type": "object",
                "properties": {"utterance": {"type": "string"},
                               "started_at": {"type": "string"},
                               "duration": {"type": "number"},
                               "since_prev": {"type": "number"},
                               "elapsed": {"type": "number"}},
                "required": ["utterance", "duration", "started_at"],
                "additionalProperties": False}
TIME_FIELDS = {"started_at": "ts", "duration": "gap_next_s",
               "since_prev": "gap_prev_s", "elapsed": "elapsed_s"}


def mk_view(name: str, *, sequences: int, len_range=(2, 3),
            instruction: str = "生成序列", styles=()) -> ClassView:
    return ClassView(
        name=name, quality=QualityConfig(), rubric=Rubric(name="r", criteria=()),
        annotate=AnnotateConfig(), verify=VerifyConfig(), extract=ExtractConfig(),
        generate=GenerateConfig(enabled=True, instruction=instruction,
                                sequences=sequences, len_range=len_range,
                                styles=tuple(styles)))


def frame_view(gen_instruction: str, gen_schema=None,
               time_fields=None) -> FrameClassView:
    return FrameClassView(instruction="", examples=(), enabled=True,
                          gen_instruction=gen_instruction, gen_schema=gen_schema,
                          time_fields=time_fields)


def mk_tiers(*specs) -> tuple[TierSpec, ...]:
    """按 (weight, frame_classes) 的声明序造档位表：tier_rank = 1..N、升序存放
    （M1 解析产物的形状）。"""
    return tuple(TierSpec(tier_rank=i, weight=weight, frame_classes=tuple(frame_classes))
                 for i, (weight, frame_classes) in enumerate(specs, 1))


def mk_cfg(*, quotas: dict[str, int] | None = None, sessions: int = 2,
           noise_ratio: float = 0.0, duplicates: int = 0,
           limit: int | None = None, generate: GenerateConfig | None = None,
           len_range=(2, 3), session_max_len: int = 200,
           llm_profiles=None, tiers: tuple[TierSpec, ...] = (),
           class_tiers: dict | None = None,
           frame_classes: tuple[str, ...] = FRAME_TABLE,
           frame_schema=FRAME_SCHEMA, time_fields=None) -> ResolvedConfig:
    quotas = quotas if quotas is not None else {"booking": 2, "smalltalk": 1}
    base_generate = generate or GenerateConfig(enabled=True, num_per_call=2)
    # v1.15: class_tiers 逐类给 ClassView.tiers（缺席 = None = 回落全局 tiers）
    views = {name: replace(mk_view(name, sequences=n, len_range=len_range),
                           generate=replace(base_generate, instruction=f"生成{name}",
                                            sequences=n, len_range=len_range),
                           tiers=(class_tiers or {}).get(name))
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
            classes=tuple(ClassSpec(name=n, description="d") for n in frame_classes)),
        frame_class_views={
            n: frame_view(f"生成{n}",
                          frame_schema if n == "task_request" else None,
                          time_fields if n == "task_request" else None)
            for n in frame_classes},
        generate_stream=GenerateStreamConfig(
            enabled=True, sessions=sessions, noise_ratio=noise_ratio,
            noise_instruction="生成噪音" if noise_ratio > 0 else "",
            duplicates=duplicates, frame_gap_s=(5.0, 60.0),
            ts_start="2026-01-01T09:00:00+08:00", tiers=tiers),
    )


def mk_timed_cfg(**kwargs) -> ResolvedConfig:
    """v1.14 绑定形态的 mk_cfg：结构化帧类带 TIMED_SCHEMA + 语义词表四值绑定表
    （M1 合法形：绑定键 ∈ 顶层 properties、类型字面恰等、剔除后仍余 utterance）。"""
    kwargs.setdefault("frame_schema", TIMED_SCHEMA)
    kwargs.setdefault("time_fields", TIME_FIELDS)
    return mk_cfg(**kwargs)


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


# ── v1.14 档位面：映射、零抽签消费、蓝图双向硬约束、标识三点、逐档计数 ────────

TIERS = mk_tiers((2, FRAME_TABLE), (1, ("task_request",)))
# v1.15 按类表：构成与权重都与全局表反向（3 条配额按 (1, 2) 配分 = 1 + 2，
# 对照全局表的 (2, 1) 配分 = 2 + 1）——混合形态下两类的分块必然可区分。
OWN_TIERS = mk_tiers((1, ("task_request",)), (2, FRAME_TABLE))


def test_sequence_plan_tier_rank_defaults_to_none():
    """档位面不在场时计划期定稿的档位序数缺省为 None（尾追默认字段）。"""
    plan = SequencePlan(index=0, class_name="booking", ordinal=0, length=3,
                        llm="default", style_name=None, style_prompt=None)
    assert plan.tier_rank is None


def test_tier_mapping_is_ascending_contiguous_blocks():
    """映射 = 配分结果按 tier_rank 升序的连续分块前缀和（裁决·零抽签配分）。"""
    assert apportion_tiers(5, TIERS) == (3, 2)
    assert [tier_rank_for_ordinal(5, TIERS, o) for o in range(5)] == [1, 1, 1, 2, 2]
    # 档位表缺省 ⇒ 映射不参与（无档可归）
    assert tier_rank_for_ordinal(5, (), 0) is None


def test_tier_mapping_commutes_with_limit_and_cuts_from_the_top_rank():
    """--limit 是配额层前缀截断，映射吃全量配额 ⇒ 交换律成立，且类内从最高档位
    序数侧截起（低档序数在前）。"""
    cfg = mk_cfg(quotas={"booking": 5}, sessions=5, tiers=TIERS)
    full = [p.tier_rank
            for p in plan_stream(cfg, random.Random("0:0:generate")).sequences]
    assert full == [1, 1, 1, 2, 2]
    limited = [p.tier_rank for p in
               plan_stream(replace(cfg, limit=3),
                           random.Random("0:0:generate")).sequences]
    assert limited == full[:3]                  # 尾部（tier_rank 2）先被截掉


def test_tier_apportionment_consumes_no_rng():
    """配分零抽签（顺序表原文不动）：同 seed 下有无档位表，长度与 (llm, style)
    预抽流逐字节一致，且 rng 消费位置相同。v1.15：按类表在场时同款成立——生效表
    查找与配分都是纯查表，档位赋值仍是①与②之间的零消费步。"""
    plain = mk_cfg(quotas={"booking": 3, "smalltalk": 2}, sessions=3, noise_ratio=0.4)
    tiered = replace(plain, generate_stream=replace(plain.generate_stream, tiers=TIERS))
    per_class = mk_cfg(quotas={"booking": 3, "smalltalk": 2}, sessions=3, noise_ratio=0.4,
                       tiers=TIERS, class_tiers={"booking": OWN_TIERS})
    rng_plain = random.Random("0:0:generate")
    rng_tiered = random.Random("0:0:generate")
    rng_class = random.Random("0:0:generate")
    a, b = plan_stream(plain, rng_plain), plan_stream(tiered, rng_tiered)
    c = plan_stream(per_class, rng_class)
    draws = [[(p.length, p.llm, p.style_name, p.style_prompt) for p in plan.sequences]
             for plan in (a, b, c)]
    assert draws[0] == draws[1] == draws[2]
    assert (a.noise_target, a.noise_plans) == (b.noise_target, b.noise_plans) \
           == (c.noise_target, c.noise_plans)
    assert rng_plain.getstate() == rng_tiered.getstate() == rng_class.getstate()
    assert [p.tier_rank for p in a.sequences] == [None] * len(a.sequences)
    assert [p.tier_rank for p in b.sequences] == [1, 1, 2, 1, 2]
    # booking 改吃按类表 (1, 2) 分块 ⇒ 前三位变 [1, 2, 2]，smalltalk 两位不变
    assert [p.tier_rank for p in c.sequences] == [1, 2, 2, 1, 2]


def test_tier_face_only_adds_keys_to_the_v113_artifact_bytes():
    """构成 = 全表的单档 ⇒ 工件除 truth.tier_rank 一键外与 v1.13 逐字段一致
    （交织期抽签流不受档位面扰动，ts 与载荷全同）。"""
    plain = mk_cfg(quotas={"booking": 2, "smalltalk": 1}, sessions=2,
                   noise_ratio=0.3, duplicates=1)
    tiered = replace(plain, generate_stream=replace(plain.generate_stream,
                                                    tiers=mk_tiers((1, FRAME_TABLE))))
    a, _, _ = run_stream(plain)
    b, _, _ = run_stream(tiered)
    assert len(a.artifact_lines) == len(b.artifact_lines) > 0
    for plain_line, tiered_line in zip(a.artifact_lines, b.artifact_lines):
        plain_row, tiered_row = json.loads(plain_line), json.loads(tiered_line)
        assert tiered_row["truth"].pop("tier_rank", "absent") != "absent"
        assert tiered_row == plain_row


def test_plan_user_line_variants_are_frozen():
    """蓝图 user 行的两个冻结变体（§10.14）：system 段不随 cover_all 变。"""
    classes = (ClassSpec(name="a", description="d"),)
    system_plain, plain = render_plan_prompt_texts("指令", classes, "类", 4)
    system_covered, covered = render_plan_prompt_texts("指令", classes, "类", 4,
                                                       cover_all=True)
    assert plain == "请为一条「类」序列产出 4 步蓝图。"
    assert covered == "请为一条「类」序列产出 4 步蓝图，且 [帧类表] 中每个帧类都至少出现一次。"
    assert system_plain == system_covered


def test_blueprint_uses_in_tier_subset_table_and_cover_all_schema():
    """档位在场的蓝图调用：[帧类表] 只渲染档内类（按帧类表**声明序**，非档内书写
    序）、enum 限档内子集、schema 带逐类 contains 覆盖；产物构成恰等档声明。"""
    cfg = mk_cfg(quotas={"booking": 1}, sessions=1, len_range=(3, 3),
                 frame_classes=("task_request", "followup", "confirmation"),
                 tiers=mk_tiers((1, ("confirmation", "task_request"))))
    product, engine, _ = run_stream(cfg)
    (_, prompt, schema), = [c for c in engine.calls if "steps" in c[2]["properties"]]
    system = prompt.messages[0].parts[0].text
    user = prompt.messages[1].parts[0].text
    assert "[帧类表]\ntask_request: d\nconfirmation: d\n" in system
    assert "followup" not in system
    assert user.endswith("，且 [帧类表] 中每个帧类都至少出现一次。")
    steps = schema["properties"]["steps"]
    assert steps["items"]["properties"]["frame_class"]["enum"] == ["task_request",
                                                                   "confirmation"]
    assert steps["allOf"] == [
        {"contains": {"type": "object",
                      "properties": {"frame_class": {"const": name}},
                      "required": ["frame_class"]}}
        for name in ("task_request", "confirmation")]
    assert {row["truth"]["frame_class"] for row in parse_lines(product)} == {
        "task_request", "confirmation"}


def test_blueprint_without_tiers_is_byte_identical_to_v113():
    """档位表缺省 ⇒ 全帧类表 + 无覆盖约束，提示词与 schema 逐字节回归 v1.13。"""
    from labelkit.common.runtime.schema_engine import plan_schema

    cfg = mk_cfg(quotas={"booking": 1}, sessions=1, len_range=(3, 3))
    _, engine, _ = run_stream(cfg)
    (_, prompt, schema), = [c for c in engine.calls if "steps" in c[2]["properties"]]
    assert (prompt.messages[0].parts[0].text,
            prompt.messages[1].parts[0].text) == render_plan_prompt_texts(
        "生成booking", cfg.frame_classify.classes, "booking", 3)
    assert schema == plan_schema(list(FRAME_TABLE), 3)


def test_truth_key_order_frozen_with_tier_rank_for_the_three_frame_shapes():
    """裁决·真值键序重冻结：tier_rank 在 sequence 之后、frame_class 之前；任务帧
    带本档序数、噪音帧恒 null、重发帧承源档。"""
    cfg = mk_cfg(quotas={"booking": 3}, sessions=2, noise_ratio=0.4, duplicates=1,
                 tiers=TIERS)
    product, _, _ = run_stream(cfg)
    rows = parse_lines(product)
    base = ["session", "sequence_class", "sequence", "tier_rank", "frame_class", "noise"]
    shapes = set()
    for row in rows:
        truth = row["truth"]
        assert list(truth) in (base, base + ["duplicate_of"])
        if truth["noise"]:
            assert truth["tier_rank"] is None
            shapes.add("noise")
        elif "duplicate_of" in truth:
            source = [r["truth"] for r in rows
                      if r["truth"]["sequence_class"] == truth["sequence_class"]
                      and r["truth"]["sequence"] == truth["duplicate_of"]]
            assert {t["tier_rank"] for t in source} == {truth["tier_rank"]}
            shapes.add("duplicate")
        else:
            assert truth["tier_rank"] == tier_rank_for_ordinal(3, TIERS,
                                                               truth["sequence"])
            shapes.add("task")
    assert shapes == {"noise", "duplicate", "task"}


def test_member_generator_gains_tier_rank_only_when_the_table_is_declared():
    """裁决·档位标识三点落位其一：成员 ref.generator 三键（值 = owner 序列档序数）；
    档位表缺省时两键不变。"""
    tiers = mk_tiers((1, FRAME_TABLE), (1, ("task_request",)))
    product, _, _ = run_stream(mk_cfg(quotas={"booking": 2}, sessions=2, tiers=tiers))
    for env in product.envelopes:
        ordinal = env.record.members[0].raw["truth"]["sequence"]
        expected = tier_rank_for_ordinal(2, tiers, ordinal)
        for member in env.record.members:
            assert list(member.ref.generator) == ["llm", "style", "tier_rank"]
            assert member.ref.generator["tier_rank"] == expected
    assert {env.record.members[0].ref.generator["tier_rank"]
            for env in product.envelopes} == {1, 2}
    plain, _, _ = run_stream(mk_cfg(quotas={"booking": 2}, sessions=2))
    assert all(set(m.ref.generator) == {"llm", "style"}
               for env in plain.envelopes for m in env.record.members)


def test_generator_tier_rank_flows_out_through_the_real_emitter(tmp_path):
    """标识流出面：emitter 的 source 装配零改动（generator 整字典原样拷出）⇒ 档位
    序数自然出现在主输出行的 _meta.source.generator，rejects 侧共用同一装配。"""
    from datetime import timezone

    from labelkit.operators.emitter import Emitter

    cfg = mk_cfg(quotas={"booking": 2}, sessions=2, tiers=TIERS)
    cfg = replace(cfg, run=replace(cfg.run, output=str(tmp_path / "labels.jsonl")),
                  annotate=AnnotateConfig(enabled=False))
    product, _, _ = run_stream(cfg)

    class EngineStub:
        def validate_only(self, obj, schema=None):
            return []

    emitter = Emitter(cfg, EngineStub(), "run0", datetime.now(timezone.utc))
    emitter.open()
    emitter.emit_batch(product.envelopes, 1)
    emitter.finalize({"counts": {}}, deliver=True)

    rows = [json.loads(line) for line
            in (tmp_path / "labels.jsonl").read_text("utf-8").splitlines()]
    assert len(rows) == len(product.envelopes) == 2
    for row, env in zip(rows, product.envelopes):
        generator = row["_meta"]["source"]["generator"]
        assert generator == env.record.members[0].ref.generator
        assert list(generator) == ["llm", "style", "tier_rank"]
    assert [row["_meta"]["source"]["generator"]["tier_rank"] for row in rows] == [1, 2]


def test_tier_planned_and_produced_counters_land_per_rank():
    """逐档计数：planned 在计划期落账（含被作废的序列），produced 口径 = 最终进链
    的幸存序列；档位表缺省 ⇒ 整族计数器不在场。v1.15（裁决·计数器键按类重冻结）：
    键恒带类段 generate.stream.tiers.<类>.<档>.*，M6 只喂这一族（禁双写）。"""
    cfg = mk_cfg(quotas={"booking": 3}, sessions=2, tiers=TIERS)
    _, _, metrics = run_stream(cfg, engine=StreamEngine(fail_plans={3}))
    assert metrics.counters["generate.stream.tiers.booking.1.planned"] == 2
    assert metrics.counters["generate.stream.tiers.booking.2.planned"] == 1
    assert metrics.counters["generate.stream.sequences.booking.planned"] == 3
    assert metrics.counters["generate.stream.tiers.booking.1.produced"] == 2
    # 第三条（tier_rank 2）蓝图作废 ⇒ 该档 produced 不落账（报表侧按声明表零基铺开）
    assert "generate.stream.tiers.booking.2.produced" not in metrics.counters
    # 单一喂数纪律：绝不同时喂 v1.14 的无类段键
    assert "generate.stream.tiers.1.planned" not in metrics.counters
    _, _, plain = run_stream(mk_cfg(quotas={"booking": 3}, sessions=2))
    assert not [key for key in plain.counters
                if key.startswith("generate.stream.tiers.")]


# ── v1.15 按类档位表：混合形态的取表点（SPEC-per-class-tiers §3.2）───────────

def mixed_cfg(**kwargs) -> ResolvedConfig:
    """混合形态：booking 用按类表、smalltalk 回落全局表（各 3 条配额）。"""
    kwargs.setdefault("quotas", {"booking": 3, "smalltalk": 3})
    kwargs.setdefault("sessions", 4)
    return mk_cfg(tiers=TIERS, class_tiers={"booking": OWN_TIERS}, **kwargs)


def test_mixed_form_maps_each_class_off_its_own_effective_table():
    """计划期逐类查本类生效表：声明类吃按类表 (1, 2) 分块，未声明类吃全局表
    (2, 1) 分块——同一 rank 值在两类间无可比性（裁决·rank 类内身份）。"""
    plan = plan_stream(mixed_cfg(), random.Random("0:0:generate"))
    ranks = {(p.class_name, p.ordinal): p.tier_rank for p in plan.sequences}
    assert [ranks[("booking", o)] for o in range(3)] == [1, 2, 2]
    assert [ranks[("smalltalk", o)] for o in range(3)] == [1, 1, 2]


def test_limit_truncation_cuts_from_each_class_top_rank_side():
    """--limit 仍是配额层前缀截断、映射仍吃全量配额 ⇒ 交换律逐类原文成立；
    类内序数按**本类**生效表分块，故从本类最高 rank 一侧截起。"""
    cfg = mixed_cfg()
    full = [(p.class_name, p.ordinal, p.tier_rank)
            for p in plan_stream(cfg, random.Random("0:0:generate")).sequences]
    for limit in (2, 4):
        limited = [(p.class_name, p.ordinal, p.tier_rank) for p in
                   plan_stream(replace(cfg, limit=limit),
                               random.Random("0:0:generate")).sequences]
        assert limited == full[:limit]
    # booking 截到 2 条 ⇒ 只剩 rank 1 的一条与 rank 2 的头一条（尾部先掉）
    assert [r for _, _, r in full[:2]] == [1, 2]


def plan_call_of(engine: StreamEngine, cname: str):
    """按 user 行里的序列类名取该类的蓝图调用（gather 序无关）。"""
    for _, prompt, schema in engine.calls:
        if ("steps" in schema["properties"]
                and f"「{cname}」" in prompt.messages[1].parts[0].text):
            return prompt, schema
    raise AssertionError(f"no blueprint call for {cname}")


def test_blueprint_reads_the_effective_table_of_the_sequence_class():
    """蓝图取档 = 本类生效表[tier_rank - 1]：[帧类表]、enum 与 contains 覆盖分支
    全部落在**本类**档内构成上，两类互不串档。"""
    cfg = mk_cfg(quotas={"booking": 1, "smalltalk": 1}, sessions=2, len_range=(2, 2),
                 frame_classes=("task_request", "followup", "confirmation"),
                 tiers=mk_tiers((1, ("task_request", "confirmation"))),
                 class_tiers={"booking": mk_tiers((1, ("task_request", "followup")))})
    _, engine, _ = run_stream(cfg)
    prompt_b, schema_b = plan_call_of(engine, "booking")
    system_b = prompt_b.messages[0].parts[0].text
    assert "[帧类表]\ntask_request: d\nfollowup: d\n" in system_b
    assert "confirmation" not in system_b
    assert (schema_b["properties"]["steps"]["items"]["properties"]["frame_class"]["enum"]
            == ["task_request", "followup"])
    _, schema_s = plan_call_of(engine, "smalltalk")     # 回落全局表的那一档
    assert (schema_s["properties"]["steps"]["items"]["properties"]["frame_class"]["enum"]
            == ["task_request", "confirmation"])
    assert [branch["contains"]["properties"]["frame_class"]["const"]
            for branch in schema_s["properties"]["steps"]["allOf"]] == [
        "task_request", "confirmation"]


def test_truth_and_generator_follow_the_class_effective_table_row_by_row():
    """标识三点的值来源改了、装配面没改：逐行 truth.tier_rank 与成员
    ref.generator.tier_rank 都等于**本行序列类**生效表内的档序数。"""
    product, _, _ = run_stream(mixed_cfg())
    tables = {"booking": OWN_TIERS, "smalltalk": TIERS}
    rows = parse_lines(product)
    for truth in (row["truth"] for row in rows):
        if truth["noise"]:
            assert truth["tier_rank"] is None       # 噪音帧不属任何序列
            continue
        assert truth["tier_rank"] == tier_rank_for_ordinal(
            3, tables[truth["sequence_class"]], truth["sequence"])
    # 两类的分块确实不同（本用例对"误用全局表"是可判的）
    assert sorted({(t["sequence"], t["tier_rank"]) for t in
                   (row["truth"] for row in rows)
                   if t["sequence_class"] == "booking"}) == [(0, 1), (1, 2), (2, 2)]
    for envelope in product.envelopes:
        truth = envelope.record.members[0].raw["truth"]
        expected = tier_rank_for_ordinal(3, tables[truth["sequence_class"]],
                                         truth["sequence"])
        assert all(m.ref.generator["tier_rank"] == expected
                   for m in envelope.record.members)


def test_per_class_counters_land_under_the_class_segment():
    """裁决·计数器键按类重冻结：逐档 planned/produced 按 <类>.<档> 落账，
    两类的同 rank 计数各自独立（平面报表的跨类求和由编排器负责）。"""
    _, _, metrics = run_stream(mixed_cfg())
    tiered = {k: v for k, v in metrics.counters.items()
              if k.startswith("generate.stream.tiers.")}
    assert tiered == {"generate.stream.tiers.booking.1.planned": 1,
                      "generate.stream.tiers.booking.2.planned": 2,
                      "generate.stream.tiers.booking.1.produced": 1,
                      "generate.stream.tiers.booking.2.produced": 2,
                      "generate.stream.tiers.smalltalk.1.planned": 2,
                      "generate.stream.tiers.smalltalk.2.planned": 1,
                      "generate.stream.tiers.smalltalk.1.produced": 2,
                      "generate.stream.tiers.smalltalk.2.produced": 1}


def test_per_class_tiers_double_run_is_byte_identical():
    """同 seed 双跑逐字节一致（按类表在场）：生效表查找与配分都零 rng，交织期
    抽签流不受按类化扰动。"""
    cfg = mixed_cfg(noise_ratio=0.3, duplicates=1)
    first, _, _ = run_stream(cfg)
    second, _, _ = run_stream(cfg)
    assert first.artifact_lines == second.artifact_lines
    assert [e.record.id for e in first.envelopes] == [e.record.id
                                                      for e in second.envelopes]


def test_declaring_the_global_table_per_class_equals_falling_back():
    """裁决·表级原子覆盖的退化面：按类表逐字段等于全局表 ⇒ 与全部缺省（v1.14
    路径）的工件、信封 id 与计数器逐字节等价。"""
    plain = mk_cfg(quotas={"booking": 2, "smalltalk": 1}, sessions=2,
                   noise_ratio=0.3, duplicates=1, tiers=TIERS)
    explicit = mk_cfg(quotas={"booking": 2, "smalltalk": 1}, sessions=2,
                      noise_ratio=0.3, duplicates=1, tiers=TIERS,
                      class_tiers={"booking": TIERS, "smalltalk": TIERS})
    a, _, metrics_a = run_stream(plain)
    b, _, metrics_b = run_stream(explicit)
    assert a.artifact_lines == b.artifact_lines
    assert [e.record.id for e in a.envelopes] == [e.record.id for e in b.envelopes]
    assert metrics_a.counters == metrics_b.counters


# ── v1.14 时间字段面：缩减 Schema 派生 ──────────────────────────────────────

def test_reduced_gen_schema_strips_bound_keys_from_properties_and_required():
    """派生 =「生成 Schema − 绑定键」：properties 删键、required 取差集（绑定键不在
    required 的形照样容忍）、其余关键字与各属性子 Schema 引用原样。"""
    reduced = _reduced_gen_schema(frame_view("生成", TIMED_SCHEMA, TIME_FIELDS))
    assert list(reduced["properties"]) == ["utterance"]
    assert reduced["required"] == ["utterance"]
    assert reduced["type"] == "object" and reduced["additionalProperties"] is False
    # 属性子 Schema 引用原样（层级拷贝只到 properties 一层）
    assert reduced["properties"]["utterance"] is TIMED_SCHEMA["properties"]["utterance"]
    # 纯文本帧（未声明生成 Schema）没有派生面
    assert _reduced_gen_schema(frame_view("生成")) is None


def test_reduced_gen_schema_tolerates_a_schema_without_required():
    """required 差集的容忍面 + 其余关键字原样：没有 required 关键字时派生不凭空造键，
    非 properties/required 的关键字连同其子对象引用原样带过。"""
    schema = {"type": "object",
              "properties": {"utterance": {"type": "string"},
                             "elapsed": {"type": "number"}},
              "$defs": {"unused": {"type": "string"}}}
    reduced = _reduced_gen_schema(frame_view("生成", schema, {"elapsed": "elapsed_s"}))
    assert reduced == {"type": "object",
                       "properties": {"utterance": {"type": "string"}},
                       "$defs": {"unused": {"type": "string"}}}
    assert "required" not in reduced
    assert reduced["$defs"] is schema["$defs"]


def test_reduced_derivation_never_pollutes_the_frozen_frame_class_view_schema():
    """层级拷贝纪律：``FrameClassView.gen_schema`` 是 M1 冻结产物（静态预算预检与
    契约行渲染同源读它）——派生与整轮真跑之后本体逐对象不变。"""
    snapshot = copy.deepcopy(TIMED_SCHEMA)
    properties, required = TIMED_SCHEMA["properties"], TIMED_SCHEMA["required"]
    view = frame_view("生成", TIMED_SCHEMA, TIME_FIELDS)
    reduced = _reduced_gen_schema(view)
    assert view.gen_schema["properties"] is properties      # 未被换掉，也未被删键
    assert view.gen_schema["required"] is required
    assert view.gen_schema == snapshot
    assert reduced is not view.gen_schema
    assert reduced["properties"] is not properties
    run_stream(mk_timed_cfg(quotas={"booking": 1}, sessions=1, len_range=(3, 3)))
    assert TIMED_SCHEMA == snapshot


def test_realize_faces_take_the_same_reduced_product_for_schema_and_contract():
    """逐位 Schema 面与契约行文本面取同一份派生产物；纯文本帧位逐字节不变。"""
    cfg = mk_timed_cfg(quotas={"booking": 1}, sessions=1)
    schemas, contracts = GenerateStage(cfg)._realize_step_faces(
        [("task_request", "要点"), ("followup", "要点")])
    assert schemas[0] == _reduced_gen_schema(cfg.frame_class_views["task_request"])
    assert contracts[0] == json.dumps(schemas[0], ensure_ascii=False,
                                      separators=(", ", ": "))
    assert not [field for field in TIME_FIELDS if field in contracts[0]]
    assert schemas[1] == {"type": "string"} and contracts[1] == "自由文本一段"


def test_dispatched_realize_schema_carries_no_bound_field():
    """真派发面：LLM 收到的逐位 Schema 已剔除绑定字段（不为注定被覆写的字段付
    token 与修复环成本）。"""
    cfg = mk_timed_cfg(quotas={"booking": 1}, sessions=1, len_range=(3, 3))
    _, engine, _ = run_stream(cfg)
    (schema,) = [c[2] for c in engine.calls if "frames" in c[2]["properties"]]
    structured = [sub for sub in schema["properties"]["frames"]["prefixItems"]
                  if sub.get("type") == "object"]
    assert structured
    for sub in structured:
        assert list(sub["properties"]) == ["utterance"]
        assert sub["required"] == ["utterance"]


def test_time_field_face_absent_reproduces_the_v113_faces_byte_for_byte():
    """双关字节等价其一：绑定表全缺省 ⇒ 逐位 Schema 与契约行回归 v1.13 原式，
    回填尾声对载荷对象零触碰。"""
    cfg = mk_cfg(quotas={"booking": 1}, sessions=1)
    views = cfg.frame_class_views
    schemas, contracts = GenerateStage(cfg)._realize_step_faces(
        [("task_request", "要点"), ("followup", "要点")])
    assert schemas == [dict(views["task_request"].gen_schema), {"type": "string"}]
    assert contracts == [json.dumps(views["task_request"].gen_schema,
                                    ensure_ascii=False, separators=(", ", ": ")),
                         "自由文本一段"]
    product, _, _ = run_stream(mk_cfg(quotas={"booking": 2, "smalltalk": 1},
                                      sessions=2, noise_ratio=0.3, duplicates=1))
    for row in parse_lines(product):
        if isinstance(row["text"], dict):
            assert list(row["text"]) == ["utterance"]       # LLM 产物原样，无回填键


def test_binding_face_only_adds_payload_keys_to_the_v113_artifact_bytes():
    """双关字节等价其二：绑定面零 rng ⇒ 同 seed 下工件除结构化载荷多出的四个绑定
    键外逐字段一致（ts、truth、纯文本帧与噪音/重发行全同）。"""
    plain = mk_cfg(quotas={"booking": 2, "smalltalk": 1}, sessions=2,
                   noise_ratio=0.3, duplicates=1, len_range=(3, 3))
    bound = mk_timed_cfg(quotas={"booking": 2, "smalltalk": 1}, sessions=2,
                         noise_ratio=0.3, duplicates=1, len_range=(3, 3))
    a, _, _ = run_stream(plain)
    b, _, _ = run_stream(bound)
    assert len(a.artifact_lines) == len(b.artifact_lines) > 0
    for plain_line, bound_line in zip(a.artifact_lines, b.artifact_lines):
        plain_row, bound_row = json.loads(plain_line), json.loads(bound_line)
        if isinstance(bound_row["text"], dict):
            for field in TIME_FIELDS:
                assert field in bound_row["text"]
                bound_row["text"].pop(field)
        assert bound_row == plain_row


# ── v1.14 时间字段面：回填算术、重发共享、回填后计 id、钩子口径 ──────────────

def test_time_field_values_boundaries_and_in_sequence_arithmetic():
    """直调回填尾声钉住四值算术：``ts`` 取该槽位已铺串、首帧 gap_prev_s/elapsed_s
    = 0.0、末帧 gap_next_s = 0.0，其余 = 本序列相邻/首帧的 ts 差秒。"""
    cfg = mk_timed_cfg(quotas={"booking": 1}, sessions=1)
    stamps = ("2026-01-01T09:00:00.000000+08:00", "2026-01-01T09:00:05.500000+08:00",
              "2026-01-01T09:00:17.750000+08:00")
    payloads = [{"utterance": f"第{i}帧"} for i in range(3)]
    slots = [_StreamSlot(payload=payload, truth={"frame_class": "task_request"},
                         owner=0, ts=ts) for payload, ts in zip(payloads, stamps)]
    backfill_time_fields([slots], cfg)
    assert payloads[0] == {"utterance": "第0帧", "started_at": stamps[0],
                           "since_prev": 0.0, "duration": 5.5, "elapsed": 0.0}
    assert payloads[1] == {"utterance": "第1帧", "started_at": stamps[1],
                           "since_prev": 5.5, "duration": 12.25, "elapsed": 5.5}
    assert payloads[2] == {"utterance": "第2帧", "started_at": stamps[2],
                           "since_prev": 12.25, "duration": 0.0, "elapsed": 17.75}


def test_time_field_values_keep_microsecond_resolution():
    """round(·, 6) 的分辨率下界与 isoformat 对齐：1 微秒差保真为 1e-06，不塌到 0.0
    （0.0 是首/末帧边界哨兵，须无歧义）。"""
    cfg = mk_timed_cfg(quotas={"booking": 1}, sessions=1)
    payloads = [{"utterance": "甲"}, {"utterance": "乙"}]
    slots = [_StreamSlot(payload=payload, truth={"frame_class": "task_request"},
                         owner=0, ts=ts)
             for payload, ts in zip(payloads, ("2026-01-01T09:00:00.000000+08:00",
                                               "2026-01-01T09:00:00.000001+08:00"))]
    backfill_time_fields([slots], cfg)
    assert payloads[0]["duration"] == 1e-06
    assert payloads[1]["since_prev"] == 1e-06 == payloads[1]["elapsed"]


def test_backfilled_values_match_the_in_sequence_neighbour_gaps():
    """整轮真跑对账：逐条序列按**交织后真实 ts** 复算四值，与工件行载荷逐值相等；
    无绑定帧类（纯文本帧）与噪音帧不被触碰。"""
    cfg = mk_timed_cfg(quotas={"booking": 2, "smalltalk": 1}, sessions=2,
                       noise_ratio=0.3, duplicates=1, len_range=(3, 3))
    product, _, _ = run_stream(cfg)
    groups: dict[tuple, list[dict]] = {}
    for row in parse_lines(product):
        truth = row["truth"]
        if truth["noise"]:
            assert isinstance(row["text"], str)          # 噪音帧不触碰
            continue
        if "duplicate_of" in truth:
            continue
        groups.setdefault((truth["sequence_class"], truth["sequence"]), []).append(row)
    assert len(groups) == 3
    for members in groups.values():
        stamps = [datetime.fromisoformat(row["ts"]) for row in members]
        for i, row in enumerate(members):
            if row["truth"]["frame_class"] != "task_request":
                assert isinstance(row["text"], str)      # 无绑定帧类不触碰
                continue
            payload = row["text"]
            expected_prev = (round((stamps[i] - stamps[i - 1]).total_seconds(), 6)
                             if i else 0.0)
            expected_next = (round((stamps[i + 1] - stamps[i]).total_seconds(), 6)
                             if i + 1 < len(stamps) else 0.0)
            assert payload["started_at"] == row["ts"]
            assert payload["since_prev"] == expected_prev
            assert payload["duration"] == expected_next
            assert payload["elapsed"] == round(
                (stamps[i] - stamps[0]).total_seconds(), 6)
            if i == 0:
                assert (payload["since_prev"], payload["elapsed"]) == (0.0, 0.0)
            if i == len(members) - 1:
                assert payload["duration"] == 0.0


def test_in_sequence_caliber_ignores_foreign_frames_woven_between_members():
    """裁决·序内间隔口径：交叉会话夹入的外序列帧与插入的噪音帧本就占用其间墙钟，
    绑定值仍按本序列相邻成员的 ts 差计——与「会话内相邻行」口径以差值不等钉开。"""
    cfg = mk_timed_cfg(quotas={"booking": 2, "smalltalk": 1}, sessions=2,
                       noise_ratio=0.4, len_range=(3, 3))
    product, _, _ = run_stream(cfg)
    rows = parse_lines(product)
    by_session: dict[int, list[dict]] = {}
    for row in rows:
        by_session.setdefault(row["truth"]["session"], []).append(row)
    crossed = [session for session in by_session.values()
               if len({(r["truth"]["sequence_class"], r["truth"]["sequence"])
                       for r in session if not r["truth"]["noise"]}) == 2]
    assert len(crossed) == 1                       # 3 序列装 2 会话 ⇒ 1 交叉会话
    checked = 0
    for session in by_session.values():
        for position, row in enumerate(session[:-1]):
            if row["truth"]["noise"] or row["truth"]["frame_class"] != "task_request":
                continue
            key = (row["truth"]["sequence_class"], row["truth"]["sequence"])
            own = [r for r in session
                   if (r["truth"]["sequence_class"], r["truth"]["sequence"]) == key]
            index = own.index(row)
            if index + 1 >= len(own):
                continue
            neighbour = session[position + 1]
            if neighbour is own[index + 1]:
                continue                           # 会话内下一行恰是序内下一帧
            assert row["text"]["duration"] == round(
                (datetime.fromisoformat(own[index + 1]["ts"])
                 - datetime.fromisoformat(row["ts"])).total_seconds(), 6)
            assert row["text"]["duration"] != round(
                (datetime.fromisoformat(neighbour["ts"])
                 - datetime.fromisoformat(row["ts"])).total_seconds(), 6)
            checked += 1
    assert checked, "交织后须至少有一个绑定帧的会话内邻居属外序列或噪音"


def test_backfill_is_deterministic_under_the_same_seed():
    """同 seed 双跑逐字节确定性——档位 + 绑定同开（SPEC §3.5 点名回归）：两机制均
    零 rng（配分是纯函数、回填只读已铺时间轴），工件与信封逐字节一致。"""
    cfg = mk_timed_cfg(quotas={"booking": 2, "smalltalk": 1}, sessions=2,
                       noise_ratio=0.3, duplicates=1, tiers=TIERS)
    first, _, _ = run_stream(cfg)
    second, _, _ = run_stream(cfg)
    assert first.artifact_lines == second.artifact_lines
    assert [e.record.id for e in first.envelopes] == [e.record.id
                                                      for e in second.envelopes]


class CountingPayload(dict):
    """写入计数的载荷 dict：钉住「每个载荷对象恰被写入一次」——重发槽位与源槽位共享
    同一对象，若回填也遍历重发槽位，计数就会翻倍。"""

    writes = 0

    def __setitem__(self, key, value):
        self.writes += 1
        super().__setitem__(key, value)


def test_duplicate_slots_share_the_backfilled_payload_object():
    """裁决·重发帧承源档与同源载荷：重发槽位与源槽位引用同一载荷对象 ⇒ 回填只写
    一次即自动生效，其 ``ts`` 绑定值 = 源帧时间戳 ≠ 自身行 ts。"""
    cfg = mk_timed_cfg(quotas={"booking": 1}, sessions=1, duplicates=1)
    plan = SequencePlan(index=0, class_name="booking", ordinal=0, length=2,
                        llm="default", style_name=None, style_prompt=None)
    payloads = (CountingPayload(utterance="一"), CountingPayload(utterance="二"))
    seq = RealizedSequence(plan=plan,
                           frame_classes=("task_request", "task_request"),
                           payloads=payloads)
    sessions, _ = weave_stream([seq], (), cfg, random.Random("0:0:generate"))
    backfill_time_fields(sessions, cfg)
    task = [slot for session in sessions for slot in session if slot.owner is not None]
    resent = [slot for session in sessions for slot in session if slot.owner is None]
    assert len(task) == len(resent) == 2
    for source, copy_slot in zip(task, resent):
        assert copy_slot.payload is source.payload
        assert copy_slot.payload["started_at"] == source.ts != copy_slot.ts
    for payload in payloads:
        assert payload.writes == len(TIME_FIELDS)       # 恰一次回填，无重复写入


def test_duplicate_rows_carry_the_source_payload_bytes_including_backfilled_fields():
    """工件面同源：重发行的 text_field 值与源行逐字节相同（含回填字段），且其
    ``ts`` 绑定值指向源帧而非自身行 ts。"""
    cfg = mk_timed_cfg(quotas={"booking": 2, "smalltalk": 1}, sessions=2,
                       duplicates=1, len_range=(3, 3))
    product, _, _ = run_stream(cfg)
    rows = parse_lines(product)
    resent = [row for row in rows if "duplicate_of" in row["truth"]]
    assert resent
    source = [row for row in rows
              if "duplicate_of" not in row["truth"] and not row["truth"]["noise"]
              and row["truth"]["sequence_class"] == resent[0]["truth"]["sequence_class"]
              and row["truth"]["sequence"] == resent[0]["truth"]["duplicate_of"]]
    assert [row["text"] for row in resent] == [row["text"] for row in source]
    bound = [(copy_row, source_row) for copy_row, source_row in zip(resent, source)
             if isinstance(copy_row["text"], dict)]
    assert bound
    for copy_row, source_row in bound:
        assert copy_row["text"]["started_at"] == source_row["ts"]
        assert copy_row["text"]["started_at"] != copy_row["ts"]


def test_backfill_precedes_row_and_id_construction():
    """裁决·回填后计 id（调用序钉板：回填在 weave 之后、assemble 之前）——工件行、
    成员 ``raw``/``text``/id 与序列 id 全部按回填后载荷计算。"""
    cfg = mk_timed_cfg(quotas={"booking": 1}, sessions=1, len_range=(3, 3))
    product, _, _ = run_stream(cfg)
    rows = parse_lines(product)
    (env,) = product.envelopes
    bound_rows = 0
    for member in env.record.members:
        row = rows[member.ref.line_no - 1]
        assert member.raw == row
        canonical = json.dumps(row, sort_keys=True, ensure_ascii=False,
                               separators=(",", ":"))
        assert member.id == hashlib.sha256(
            canonical.encode("utf-8")).hexdigest()[:16]
        if isinstance(row["text"], dict):
            assert set(TIME_FIELDS) <= set(row["text"])
            assert json.loads(member.text) == row["text"]
            bound_rows += 1
    assert bound_rows
    joined = "\n".join(member.id for member in env.record.members)
    assert env.record.id == hashlib.sha256(joined.encode("utf-8")).hexdigest()[:16]


def test_artifact_replay_with_bound_time_fields_reingests_same_ids(tmp_path):
    """重放往返（v1.13 用例的绑定形扩展）：含回填数值的工件行经 M2 摄取，成员 id、
    会话切分与 canonical JSON 文本投影逐字节一致。"""
    cfg = mk_timed_cfg(quotas={"booking": 2, "smalltalk": 1}, sessions=2,
                       noise_ratio=0.2, duplicates=1, len_range=(3, 3))
    product, _, _ = run_stream(cfg)
    assert any(isinstance(row["text"], dict)
               and isinstance(row["text"]["duration"], float)
               for row in parse_lines(product))
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
    assert len(sessions) == 2 + 1
    replay_ids = [record.id for session in sessions for record in session.records]
    assert replay_ids == [
        hashlib.sha256(json.dumps(json.loads(line), sort_keys=True,
                                  ensure_ascii=False, separators=(",", ":"))
                       .encode("utf-8")).hexdigest()[:16]
        for line in product.artifact_lines]
    assert {env.session_id for env in product.envelopes} <= {
        session.session_id for session in sessions}
    replay_text = {record.id: record.text
                   for session in sessions for record in session.records}
    for env in product.envelopes:
        for member in env.record.members:
            assert replay_text[member.id] == member.text


def test_hooks_and_the_similarity_filter_see_pre_backfill_payloads(monkeypatch):
    """裁决·回填前钩子口径：``sample_validator`` 逐帧探针与序列相似度过滤探针都在
    交织之前取值 ⇒ 绑定字段缺席（时间量是机械量，不参与内容校验与内容判重）。"""
    import labelkit.operators.generate as generate_module

    seen: list[str] = []

    def hook(text: str) -> list[str]:
        seen.append(text)
        return []

    monkeypatch.setattr("labelkit.common.extensions.hooks.resolve_hook",
                        lambda ref: hook)
    probes: list[str] = []
    probe_and_add = generate_module.SimilarityFilter.probe_and_add

    def spy(self, text: str) -> bool:
        probes.append(text)
        return probe_and_add(self, text)

    monkeypatch.setattr(generate_module.SimilarityFilter, "probe_and_add", spy)
    generate = GenerateConfig(enabled=True, num_per_call=2,
                              sample_validator="mod:fn")
    cfg = mk_timed_cfg(quotas={"booking": 2}, sessions=2, len_range=(3, 3),
                       generate=generate)
    product, _, _ = run_stream(cfg)
    assert seen and probes
    assert not [text for text in seen + probes
                if [field for field in TIME_FIELDS if field in text]]
    # 对照面：同一批载荷回填之后才带绑定字段
    assert [line for line in product.artifact_lines if '"duration"' in line]
