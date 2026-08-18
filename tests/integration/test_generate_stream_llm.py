"""v1.13/v1.14 time-stream generation integration tests — REAL endpoints, no mocks.

SPEC-stream-generation.md §3.9 and SPEC-generation-tiers.md §3.7 integration
rows. Two endpoints, by design:

1./2./4./5. **DeepSeek** (``deepseek-v4-flash`` via api.deepseek.com/anthropic)
   — the E2E face the stakeholder pinned for this feature (2026-08-13, carried
   over to v1.14 on 2026-08-18). The route hard-rejects forced tool calls (400,
   E2E-FINDINGS #24) ⇒ ``supports_structured_output = false`` ⇒ L0 is fully off
   and the structural compliance of the blueprint / realize calls rests on the
   two templates' embedded structure contracts, with the schema engine's L1
   deterministic parse + L2 validation + L3 repair behind them. Case 1 drives
   the whole ``generate_stream_all`` entry (one blueprint + one realize call for
   a single two-to-three-step sequence, noise off) and pins the
   artifact/envelope contracts; case 2 drives ``annotate_record`` through a
   per-sequence-class annotation schema (裁决·按类标注 Schema); case 4 (v1.14)
   drives the same entry over a two-tier table and pins 裁决·构成恰等 on the
   surviving envelopes plus the per-rank counters; case 5 (v1.14) pins the
   mechanical time-field backfill against the artifact's own timestamps.
3./6. **z.ai** (``glm-5.2``) — the standing assumptions behind the two internal
   schema constructors under a vendor structured-output route (L0 on,
   ``supports_structured_output = true``): case 3 pins ``realize_schema``'s
   ``prefixItems`` passthrough, case 6 (v1.14) pins ``plan_schema``'s
   ``cover_all`` products (``allOf`` + per-class ``contains``, 裁决·L0 待遇沿用).

Skips: the DeepSeek cases skip themselves when ``LABELKIT_DEEPSEEK_KEY`` is
absent; the z.ai cases ride the conftest-wide ``LABELKIT_ZAI_KEY`` gate that
skips every integration-marked test. Both variables are auto-loaded from the
git-ignored repo-root ``.env`` by tests/conftest.py. Every case stays at
temperature 0; the most expensive one (case 4) spends four real LLM calls.
"""
from __future__ import annotations

import hashlib
import json
import os
import random
from dataclasses import replace
from datetime import datetime

import jsonschema
import pytest

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
    InputConfig,
    LLMProfile,
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
)
from labelkit.common.contracts.stage import RunContext
from labelkit.common.contracts.types import Record, RecordRef
from labelkit.common.runtime.llm_client import Message, Part, PromptBundle
from labelkit.common.runtime.llm_client import LLMClient
from labelkit.common.runtime.schema_engine import (
    CallScope,
    SchemaEngine,
    plan_schema,
    realize_schema,
)
from labelkit.operators.annotate import AnnotatePromptOptions, annotate_record
from labelkit.operators.generate import (
    GenerateStage,
    canonical_json,
    render_plan_prompt_texts,
)

from tests.conftest import ZAI_BASE_URL, ZAI_KEY_ENV, ZAI_MODEL

pytestmark = pytest.mark.integration

# examples/synth-stream/config.toml 的 [llm.default] 逐键镜像（自含单 profile）。
DEEPSEEK_BASE_URL = "https://api.deepseek.com/anthropic"
DEEPSEEK_MODEL = "deepseek-v4-flash"
DEEPSEEK_KEY_ENV = "LABELKIT_DEEPSEEK_KEY"

needs_deepseek = pytest.mark.skipif(
    not os.environ.get(DEEPSEEK_KEY_ENV),
    reason=f"{DEEPSEEK_KEY_ENV} not set — the v1.13 E2E face runs on DeepSeek")

# 帧类生成 Schema（examples/synth-stream 的 task_request 同形）——realize_schema 会把
# 它逐位包进 prefixItems 作为子模式，故不带顶层 $schema 声明。
FRAME_GEN_SCHEMA = {
    "type": "object",
    "properties": {"utterance": {"type": "string"},
                   "entities": {"type": "array", "items": {"type": "string"}}},
    "required": ["utterance", "entities"],
    "additionalProperties": False,
}

# 按序列类的标注 Schema（examples/synth-stream 的 ticket_booking 同形）。全局
# Schema 的字段集刻意与之不相交——产物过类 Schema 即证明按类路由生效。
CLASS_SCHEMA = {
    "type": "object",
    "properties": {"intent": {"type": "string"},
                   "origin": {"type": "string"},
                   "destination": {"type": "string"},
                   "depart_date": {"type": "string"},
                   "constraints": {"type": "array", "items": {"type": "string"}}},
    "required": ["intent", "origin", "destination", "depart_date", "constraints"],
    "additionalProperties": False,
}

GLOBAL_SCHEMA = {
    "type": "object",
    "properties": {"placeholder_only": {"type": "string"}},
    "required": ["placeholder_only"],
    "additionalProperties": False,
}

TRUTH_KEYS = {"session", "sequence_class", "sequence", "frame_class", "noise"}

CLASS_INSTRUCTION = (
    "你在为高铁购票场景合成真实用户与语音助手的一次对话序列。序列围绕同一次出行"
    "展开：从提出购票诉求开始，逐步补全或修改出发地、目的地、日期时段、车次坐席"
    "等要素。要求口语化、中文、每帧一句话，前后帧信息连贯且有推进。")
ANNOTATE_INSTRUCTION = (
    "你是高铁购票会话的标注员。阅读整段请求序列，抽出这次出行的意图（intent，一个"
    "动宾短语）、出发地（origin）、目的地（destination）、出发日期或时段"
    "（depart_date，原文表述即可）以及用户提出的其他约束（constraints，如坐席、"
    "车次偏好；没有则给空数组）。序列中未提及的字段填空字符串。")

FRAME_CLASSES = (
    ClassSpec(name="task_request", description="发起任务的首帧请求：说明诉求并给出已知要素"),
    ClassSpec(name="followup", description="对进行中任务的追问、修改、补充约束"),
)
FRAME_VOCAB = {spec.name for spec in FRAME_CLASSES}

TASK_FRAME_INSTRUCTION = ("产出发起任务的首帧：utterance 是用户说出的那一句话，"
                          "entities 逐项列出其中的关键要素（地点、日期时段、车次"
                          "坐席等），没有则给空数组。")
FOLLOWUP_FRAME_INSTRUCTION = ("产出一句追问或修改：在已有诉求之上补充约束、更换要素"
                              "或询问细节，口语、中文、一句话。")

# ── v1.14 档位面（SPEC-generation-tiers §3.1/§3.2）──────────────────────────
# 第三个帧类只服务档位面：两档的构成必须集合互异，故第 2 档比第 1 档多一类。
CONFIRMATION = ClassSpec(name="confirmation",
                         description="对助手结果的确认、收尾或致谢")
CONFIRMATION_FRAME_INSTRUCTION = ("产出一句确认或收尾：认可助手给出的结果、拍板下单，"
                                  "或补一句致谢，口语、中文、一句话。")
# 权重 1:1 × sequences = 2 ⇒ 整数域最大余额法配分 (1, 1)：两档各真跑一条序列。
TIERS = (TierSpec(tier_rank=1, weight=1,
                  frame_classes=("task_request", "followup")),
         TierSpec(tier_rank=2, weight=1,
                  frame_classes=("task_request", "followup", "confirmation")))
TIER_COMPOSITION = {spec.tier_rank: set(spec.frame_classes) for spec in TIERS}

# ── v1.14 时间字段面（SPEC-generation-tiers §3.3）───────────────────────────
# 两个帧类都结构化并各绑一个语义词：gap_next_s 的末帧 0.0 与 gap_prev_s 的首帧 0.0
# 两个边界哨兵因此在同一条序列里同时受检。绑定字段从 LLM 面向的逐位 Schema 与契约
# 行里剔除（裁决·绑定即剔除），故两条帧指令都不提它们。
TIMED_TASK_SCHEMA = {
    "type": "object",
    "properties": {"utterance": {"type": "string"},
                   "entities": {"type": "array", "items": {"type": "string"}},
                   "duration": {"type": "number"}},
    "required": ["utterance", "entities", "duration"],
    "additionalProperties": False,
}
TIMED_FOLLOWUP_SCHEMA = {
    "type": "object",
    "properties": {"utterance": {"type": "string"},
                   "wait_s": {"type": "number"}},
    "required": ["utterance", "wait_s"],
    "additionalProperties": False,
}
TIMED_SCHEMAS = {"task_request": TIMED_TASK_SCHEMA,
                 "followup": TIMED_FOLLOWUP_SCHEMA}
TIME_BINDINGS = {"task_request": {"duration": "gap_next_s"},
                 "followup": {"wait_s": "gap_prev_s"}}
TIMED_FOLLOWUP_INSTRUCTION = ("产出一句追问或修改：utterance 是用户说出的那一句话"
                              "（在已有诉求之上补充约束、更换要素或询问细节，口语、"
                              "中文、一句话）。")
NOISE_INSTRUCTION = ("生成与任何任务都无关的干扰输入：用户随口说的闲聊、感叹或跑题的"
                     "一句话，长度 5–20 字，不得包含任何可执行的诉求。")


# ── fixtures: real profiles + a directly built ResolvedConfig (M1 shape) ────

def _deepseek_profile() -> LLMProfile:
    """examples/synth-stream/config.toml:[llm.default] 逐键镜像（retries 按测试
    预算收紧）：L0 全关 ⇒ 结构服从性走模板内嵌契约 + 引擎 L1/L2/L3。"""
    return LLMProfile(
        name="default", provider="anthropic", base_url=DEEPSEEK_BASE_URL,
        model=DEEPSEEK_MODEL, api_key_env=DEEPSEEK_KEY_ENV, max_concurrency=4,
        timeout_s=120, max_retries=2, supports_structured_output=False,
        supports_vision=False, max_output_tokens=4096, context_window=131072,
        temperature=0.0, api_key=os.environ.get(DEEPSEEK_KEY_ENV, ""))


def _zai_profile() -> LLMProfile:
    """z.ai glm-5.2：L0 开启面（supports_structured_output = true）——prefixItems
    的供应商透传服从性钉板。"""
    return LLMProfile(
        name="default", provider="anthropic", base_url=ZAI_BASE_URL,
        model=ZAI_MODEL, api_key_env=ZAI_KEY_ENV, max_concurrency=4,
        timeout_s=120, max_retries=2, supports_structured_output=True,
        supports_vision=True, max_output_tokens=4096, temperature=0.0,
        api_key=os.environ.get(ZAI_KEY_ENV, ""))


def _class_view(name: str, *, sequences: int, schema=None,
                len_range=(2, 3)) -> ClassView:
    return ClassView(
        name=name, quality=QualityConfig(), rubric=Rubric(name="r", criteria=()),
        annotate=AnnotateConfig(enabled=True, llm="default",
                                instruction=ANNOTATE_INSTRUCTION),
        generate=GenerateConfig(enabled=True, llms=("default",),
                                instruction=CLASS_INSTRUCTION, temperature=0.0,
                                sequences=sequences, len_range=len_range),
        verify=VerifyConfig(), extract=ExtractConfig(), schema=schema)


def _frame_view(instruction: str, gen_schema=None,
                time_fields=None) -> FrameClassView:
    return FrameClassView(instruction="", examples=(), enabled=True,
                          gen_instruction=instruction, gen_schema=gen_schema,
                          time_fields=time_fields)


def _cfg(profile: LLMProfile, *, class_schema=None) -> ResolvedConfig:
    """examples/synth-stream 的最小同构配置：一个序列类 × sequences = 1、
    len_range = [2,3]、帧类表两类（其一带生成 Schema）、噪音关（调用量最小）。"""
    return ResolvedConfig(
        tool=ToolConfig(), console=ConsoleConfig(),
        llm_profiles={"default": profile}, embedding_profiles={},
        run=RunConfig(output="out/synth-labels.jsonl", modality="text",
                      mode="generate_only", seed=20260813),
        input=InputConfig(text_field="text"),
        stream=StreamConfig(order_by="meta:ts", gap_s=900),
        dedup=DedupConfig(), segment=SegmentConfig(), stitch=StitchConfig(),
        extract=ExtractConfig(),
        classify=ClassifyConfig(
            enabled=True,
            classes=(ClassSpec(name="ticket_booking", description="高铁购票会话"),)),
        quality=QualityConfig(),
        generate=GenerateConfig(enabled=True, llms=("default",), num_per_call=4,
                                temperature=0.0),
        annotate=AnnotateConfig(enabled=True, llm="default",
                                instruction=ANNOTATE_INSTRUCTION),
        verify=VerifyConfig(),
        output=OutputConfig(schema_inline=json.dumps(GLOBAL_SCHEMA)),
        trace=TraceConfig(), rubric=Rubric(name="default:trajectory", criteria=()),
        class_views={"ticket_booking": _class_view("ticket_booking", sequences=1,
                                                   schema=class_schema)},
        user_schema=GLOBAL_SCHEMA, limit=None, strict=False, dry_run=False,
        config_path="config.toml", project_path="project.toml",
        config_digest="sha256:0", project_digest="sha256:0",
        frame_classify=FrameClassifyConfig(classes=FRAME_CLASSES),
        frame_class_views={
            "task_request": _frame_view(TASK_FRAME_INSTRUCTION, FRAME_GEN_SCHEMA),
            "followup": _frame_view(FOLLOWUP_FRAME_INSTRUCTION)},
        generate_stream=GenerateStreamConfig(
            enabled=True, sessions=1, noise_ratio=0.0, duplicates=0,
            frame_gap_s=(5.0, 60.0), ts_start="2026-01-05T09:00:00+08:00"),
    )


def _tiered_cfg() -> ResolvedConfig:
    """v1.14 档位面的最小真跑配置（examples/synth-stream 的档位表同构缩微）：帧类
    表三类、两档（构成两类 / 全三类、权重 1:1），单序列类 sequences = 2 ⇒ 逐档各
    一条；len_range = [3, 3] 恰等最大构成大小（M1 长度可覆盖的下界），噪音与重发
    全关 ⇒ 恰四次真实调用。"""
    base = _cfg(_deepseek_profile())
    views = dict(base.frame_class_views)
    views["confirmation"] = _frame_view(CONFIRMATION_FRAME_INSTRUCTION)
    return replace(
        base,
        class_views={"ticket_booking": _class_view("ticket_booking", sequences=2,
                                                   len_range=(3, 3))},
        frame_classify=replace(base.frame_classify,
                               classes=FRAME_CLASSES + (CONFIRMATION,)),
        frame_class_views=views,
        generate_stream=replace(base.generate_stream, sessions=2, tiers=TIERS))


def _timed_cfg() -> ResolvedConfig:
    """v1.14 时间字段面的最小真跑配置：单序列类 sequences = 2、len_range = [3, 3]，
    两个帧类都结构化且各绑一个语义词；噪音开、单会话（⇒ 两条序列交叉，序内口径要有
    外来帧夹入才检得出）、重发一条（承源载荷面）⇒ 五次真实调用（两轮蓝图 + 帧实现
    + 一批噪音）。配额取 2 而非 1 是作废容忍所需：一条序列偶发作废时另一条仍能承载
    全部断言。"""
    base = _cfg(_deepseek_profile())
    return replace(
        base,
        class_views={"ticket_booking": _class_view("ticket_booking", sequences=2,
                                                   len_range=(3, 3))},
        frame_class_views={
            "task_request": _frame_view(TASK_FRAME_INSTRUCTION, TIMED_TASK_SCHEMA,
                                        TIME_BINDINGS["task_request"]),
            "followup": _frame_view(TIMED_FOLLOWUP_INSTRUCTION,
                                    TIMED_FOLLOWUP_SCHEMA,
                                    TIME_BINDINGS["followup"])},
        generate_stream=replace(base.generate_stream, noise_ratio=0.5,
                                duplicates=1,
                                noise_instruction=NOISE_INSTRUCTION))


class _RecordingMetrics:
    def __init__(self):
        self.counters: dict[str, int] = {}
        self.events: list[tuple] = []

    def count(self, key: str, n: int = 1) -> None:
        self.counters[key] = self.counters.get(key, 0) + n

    def event(self, ev, *, stage, batch_no, record_ids=(), payload=None) -> None:
        self.events.append((ev, stage, batch_no, record_ids, payload or {}))

    def record_provider_result(self, fatal: bool, *, hard: bool = False) -> None:
        self.count("provider.fatal" if fatal else "provider.ok")


def _ctx(cfg: ResolvedConfig, *, stage: str = "generate") -> RunContext:
    metrics = _RecordingMetrics()
    llm = LLMClient(cfg.llm_profiles, cfg.embedding_profiles, metrics=None)
    engine = SchemaEngine(dict(cfg.user_schema), llm, cfg.output, metrics=None)
    return RunContext(cfg=cfg, llm=llm, schema_engine=engine, metrics=metrics,
                      rng=random.Random(f"{cfg.run.seed}:0:{stage}"), batch_no=0)


# ── 1. DeepSeek: generate_stream_all end to end (blueprint + realize) ───────

@needs_deepseek
async def test_generate_stream_all_real_deepseek_blueprint_realize_artifact():
    cfg = _cfg(_deepseek_profile())
    ctx = _ctx(cfg)

    product = await GenerateStage(cfg).generate_stream_all(ctx)   # 2 real calls

    # 蓝图 + 实现各一次真跑通，零作废（L0 关 ⇒ 结构靠模板内嵌契约 + L1/L2/L3）
    assert ctx.metrics.counters.get("generate.stream.plan_calls") == 1
    assert ctx.metrics.counters.get("generate.stream.realize_calls") == 1
    assert "generate.stream.plan_failures" not in ctx.metrics.counters
    assert "generate.stream.realize_failures" not in ctx.metrics.counters
    assert "generate.stream.noise_calls" not in ctx.metrics.counters  # noise off

    # ── 信封面：inherited 两级标签在场 ──
    assert len(product.envelopes) == 1
    item = product.envelopes[0]
    assert item.record.kind == "sequence"
    assert 2 <= len(item.record.members) <= 3          # len_range = [2,3]
    assert item.classification is not None
    assert item.classification.label == "ticket_booking"
    assert item.classification.labels == ("ticket_booking",)
    assert item.classification.source == "inherited"   # 序列级：零判决调用
    assert item.session_id and len(item.session_id) == 16
    member_ids = [member.id for member in item.record.members]
    assert set(item.member_classifications) == set(member_ids)
    for member_id in member_ids:
        frame_cl = item.member_classifications[member_id]
        assert frame_cl.source == "inherited"          # 帧级：蓝图即真值
        assert frame_cl.label in FRAME_VOCAB
        assert frame_cl.labels == (frame_cl.label,)
    # 序列 id = M14 公式；成员 line_no = 工件行号
    assert item.record.id == hashlib.sha256(
        "\n".join(member_ids).encode("utf-8")).hexdigest()[:16]
    assert [member.ref.line_no for member in item.record.members] == list(
        range(1, len(member_ids) + 1))

    # ── 工件面：行 = {ts, text, truth}、truth 键集冻结、id = M2 公式 ──
    assert len(product.artifact_lines) == len(member_ids)   # 噪音/重复都关
    previous_ts = ""
    for line_no, (line, member) in enumerate(
            zip(product.artifact_lines, item.record.members), start=1):
        row = json.loads(line)
        assert set(row) == {"ts", "text", "truth"}
        assert set(row["truth"]) == TRUTH_KEYS           # duplicate_of 不在场
        assert row["truth"] == {"session": 0, "sequence_class": "ticket_booking",
                                "sequence": 0, "noise": False,
                                "frame_class": row["truth"]["frame_class"]}
        assert row["truth"]["frame_class"] in FRAME_VOCAB
        assert row["ts"] > previous_ts                   # ts 严格递增
        previous_ts = row["ts"]
        # 工件行即 raw：成员 id = sha256(canonical_json(行))[:16]（重放逐字节一致）
        assert member.id == hashlib.sha256(
            canonical_json(row).encode("utf-8")).hexdigest()[:16]
        assert member.raw == row
        assert member.ref.line_no == line_no
        assert member.ref.generator == {"llm": "default", "style": None}

    # ── 逐帧内容契约：结构化帧过其生成 Schema、纯文本帧是非空字符串 ──
    frame_classes = [json.loads(line)["truth"]["frame_class"]
                     for line in product.artifact_lines]
    for line, frame_class in zip(product.artifact_lines, frame_classes):
        payload = json.loads(line)["text"]
        if frame_class == "task_request":
            assert isinstance(payload, dict)
            jsonschema.Draft202012Validator(FRAME_GEN_SCHEMA).validate(payload)
            assert str(payload["utterance"]).strip()
        else:
            assert isinstance(payload, str) and payload.strip()
    # 类指令写明「从提出购票诉求开始」⇒ 温度 0 下蓝图恒以 task_request 开场；
    # 模型若漂移，上面的逐帧契约断言仍是钉住的行为面（frame_llm 同款放宽口径）。
    assert "task_request" in frame_classes


# ── 2. DeepSeek: the per-sequence-class annotation schema (裁决·按类标注 Schema) ──

@needs_deepseek
async def test_annotate_record_real_deepseek_routes_per_class_schema():
    cfg = _cfg(_deepseek_profile(), class_schema=CLASS_SCHEMA)
    ctx = _ctx(cfg, stage="annotate")
    members = tuple(
        Record(id=f"cccc00000000000{i}", modality="text", text=text,
               raw={"text": text}, ui_tree=None, image=None,
               ref=RecordRef("out/synth-labels.stream.jsonl", i, None, ()))
        for i, text in enumerate(
            ("帮我订下周五上午上海到北京的高铁票",
             "要二等座靠窗的，最好是复兴号"), start=1))
    sequence = Record(id="dddd000000000001", modality="text", text=None, raw=None,
                      ui_tree=None, image=None, ref=members[0].ref,
                      kind="sequence", members=members)

    annotation = await annotate_record(                        # ONE real call
        sequence, ctx, AnnotatePromptOptions(label="ticket_booking"))

    # 产物过**类** Schema——全局 Schema 的字段集与之不相交，过了即证明按类路由
    jsonschema.Draft202012Validator(CLASS_SCHEMA).validate(dict(annotation.output))
    assert set(annotation.output) == set(CLASS_SCHEMA["properties"])
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.Draft202012Validator(GLOBAL_SCHEMA).validate(
            dict(annotation.output))
    assert str(annotation.output["intent"]).strip()
    assert annotation.attempts >= 1
    assert annotation.model
    assert annotation.usage.prompt_tokens > 0
    assert annotation.usage.completion_tokens > 0


# ── 3. z.ai: realize_schema prefixItems passthrough under L0 ────────────────

async def test_realize_schema_prefixitems_passthrough_zai_structured_output():
    """站立假设钉板：``prefixItems`` 随 L0（supports_structured_output = true）
    原样透传给供应商结构化输出，返回的 frames 逐位服从各自契约。"""
    cfg = _cfg(_zai_profile())
    ctx = _ctx(cfg)
    schema = realize_schema([FRAME_GEN_SCHEMA, {"type": "string"}])
    system_text = (
        "你在为高铁购票场景合成一次对话序列的帧内容。\n"
        "只输出一个 JSON 对象，形如 {\"frames\": [...]}，frames 恰含 2 帧，逐位对应"
        "下面的契约。\n"
        f"第 1 帧（task_request）须符合："
        f"{json.dumps(FRAME_GEN_SCHEMA, ensure_ascii=False)}\n"
        "第 2 帧（followup）须符合：一段自由文本（中文一句话）。")
    user_text = ("1. [task_request] 用户提出购票诉求，给出出发地、目的地与日期时段\n"
                 "2. [followup] 用户补充坐席偏好\n请实现全部 2 帧内容。")
    prompt = PromptBundle(
        messages=(Message(role="system", parts=(Part(kind="text", text=system_text),)),
                  Message(role="user", parts=(Part(kind="text", text=user_text),))),
        temperature=0.0)

    obj, usage, attempts, model = await ctx.schema_engine.complete_validated(
        "default", prompt, schema=schema,                      # ONE real call
        scope=CallScope(batch_no=0))

    frames = obj["frames"]
    assert len(frames) == 2                       # minItems = maxItems 钉死长度
    jsonschema.Draft202012Validator(FRAME_GEN_SCHEMA).validate(frames[0])
    assert str(frames[0]["utterance"]).strip()
    assert isinstance(frames[1], str) and frames[1].strip()
    # 整体再过一次包装器 Schema（prefixItems + items: false 封尾）
    jsonschema.Draft202012Validator(schema).validate(obj)
    assert attempts >= 1 and model
    assert usage.prompt_tokens > 0 and usage.completion_tokens > 0


# ── 4. DeepSeek: v1.14 帧类构成档位（裁决·构成恰等 + 逐档计数）───────────────

@needs_deepseek
async def test_generate_stream_tiers_real_deepseek_composition_and_counters():
    """档位真跑：**幸存**序列的 members[] 帧类真值集合恰等于其档声明的构成
    （enum 给「⊆」、contains 给「⊇」），逐档 planned/produced 如实落账。

    作废容忍（E2E-FINDINGS 第 26 条先例）：断言只压幸存面并要求至少一条幸存——
    L0 关端点上蓝图或帧实现偶发违约，按既有 plan_failures / realize_failures 作废
    并让该序列缺席，不是本用例要钉的行为。
    """
    cfg = _tiered_cfg()
    ctx = _ctx(cfg)

    product = await GenerateStage(cfg).generate_stream_all(ctx)   # 4 real calls

    # 计划期逐档落账：零抽签配分（sequences = 2、权重 1:1）⇒ 每档恰一条
    assert ctx.metrics.counters.get("generate.stream.tiers.1.planned") == 1
    assert ctx.metrics.counters.get("generate.stream.tiers.2.planned") == 1
    assert product.envelopes, "两条计划序列全部作废，档位面无从断言"

    produced: dict[int, int] = {}
    for item in product.envelopes:
        ranks = {member.ref.generator["tier_rank"]
                 for member in item.record.members}
        assert len(ranks) == 1                      # 一条序列恒属一档
        rank = ranks.pop()
        assert len(item.record.members) == 3        # len_range = [3, 3]
        labels = {item.member_classifications[member.id].label
                  for member in item.record.members}
        assert labels == TIER_COMPOSITION[rank]     # 裁决·构成恰等
        produced[rank] = produced.get(rank, 0) + 1
    for rank, count in produced.items():
        assert ctx.metrics.counters[f"generate.stream.tiers.{rank}.produced"] == count
    assert sum(produced.values()) == len(product.envelopes) <= 2

    # 工件面：truth 键序冻结（tier_rank 在 sequence 之后、frame_class 之前），
    # 逐行档位与帧类自洽——档位身份可从数据反推对账，不必信任标签
    for row in map(json.loads, product.artifact_lines):
        assert list(row["truth"]) == ["session", "sequence_class", "sequence",
                                      "tier_rank", "frame_class", "noise"]
        assert (row["truth"]["frame_class"]
                in TIER_COMPOSITION[row["truth"]["tier_rank"]])


# ── 5. DeepSeek: v1.14 时间字段回填（裁决·绑定即剔除 / 序内间隔口径）─────────

def _expected_time_value(stamps: list[datetime], position: int, term: str) -> float:
    """从工件自身的 ts 独立复算一个语义词的期望值（不复用生产实现）。"""
    if term == "gap_next_s":
        return (round((stamps[position + 1] - stamps[position]).total_seconds(), 6)
                if position + 1 < len(stamps) else 0.0)
    assert term == "gap_prev_s"
    return (round((stamps[position] - stamps[position - 1]).total_seconds(), 6)
            if position else 0.0)


def _owned_sequences(rows: list[dict]) -> dict[tuple[str, int], list[dict]]:
    """按序列身份归组工件行（噪音行与重发行除外），组内保持工件序 = 序内成员序。"""
    groups: dict[tuple[str, int], list[dict]] = {}
    for row in rows:
        truth = row["truth"]
        if truth["noise"] or "duplicate_of" in truth:
            continue
        groups.setdefault((truth["sequence_class"], truth["sequence"]), []).append(row)
    return groups


@needs_deepseek
async def test_generate_stream_time_fields_real_deepseek_backfilled_from_ts():
    """时间字段真跑：绑定字段的值恰等于**本序列相邻成员**的 ts 差（序内口径——
    交叉进来的外序列帧与夹在中间的噪音帧占墙钟但不计入），首/末帧边界取 0.0，
    重发行承源值不与自身会话时间轴对账，且回填先于行对象与 id 计算。

    作废容忍同案例四：断言只压幸存序列，并要求至少一条幸存。
    """
    cfg = _timed_cfg()
    ctx = _ctx(cfg)

    product = await GenerateStage(cfg).generate_stream_all(ctx)   # <= 5 real calls

    rows = [json.loads(line) for line in product.artifact_lines]
    groups = _owned_sequences(rows)
    assert groups, "两条计划序列全部作废，时间字段面无从断言"

    checked = 0
    for members in groups.values():
        assert len(members) == 3                    # len_range = [3, 3]
        stamps = [datetime.fromisoformat(row["ts"]) for row in members]
        for position, row in enumerate(members):
            payload = row["text"]
            frame_class = row["truth"]["frame_class"]
            # 回填后的载荷满足用户声明的**完整**生成 Schema（含被剔除的绑定字段）
            jsonschema.Draft202012Validator(
                TIMED_SCHEMAS[frame_class]).validate(payload)
            for field, term in TIME_BINDINGS[frame_class].items():
                assert payload[field] == _expected_time_value(stamps, position, term)
                checked += 1
    assert checked == 3 * len(groups)               # 每帧恰一个绑定字段

    # 噪音帧不属任何序列 ⇒ 不被回填遍历（纯文本载荷，压根没有绑定字段可写）
    assert all(isinstance(row["text"], str) and row["text"].strip()
               for row in rows if row["truth"]["noise"])

    # 重发会话：与源帧引用同一载荷对象 ⇒ 绑定值逐字节承源（裁决·重发帧承源档与
    # 同源载荷），自身另铺一条时间轴，不与之对账
    duplicated: dict[tuple[str, int], list[dict]] = {}
    for row in rows:
        truth = row["truth"]
        if "duplicate_of" in truth:
            key = (truth["sequence_class"], truth["duplicate_of"])
            duplicated.setdefault(key, []).append(row)
    assert len(duplicated) == 1                     # duplicates = 1
    key, copies = next(iter(duplicated.items()))
    for source, duplicate in zip(groups[key], copies):
        assert duplicate["text"] == source["text"]
        assert duplicate["ts"] > source["ts"]
        assert duplicate["truth"]["sequence"] is None

    # 裁决·回填后计 id：行对象与成员 id 都含回填值 ⇒ 工件重放逐字节同 id
    for item in product.envelopes:
        members = groups[(item.classification.label,
                          item.record.members[0].raw["truth"]["sequence"])]
        for member, row in zip(item.record.members, members):
            assert member.raw == row
            assert member.id == hashlib.sha256(
                canonical_json(row).encode("utf-8")).hexdigest()[:16]


# ── 6. z.ai: plan_schema cover_all (allOf + contains) passthrough under L0 ──

async def test_plan_schema_cover_all_passthrough_zai_structured_output():
    """站立假设钉板（裁决·L0 待遇沿用，prefixItems 同族）：``cover_all`` 产物的
    ``allOf`` + 逐类 ``contains`` 随 L0（supports_structured_output = true）原样
    上行为供应商强制工具的 input_schema，返回的蓝图逐类覆盖（构成恰等）。"""
    cfg = _cfg(_zai_profile())
    ctx = _ctx(cfg)
    classes = FRAME_CLASSES + (CONFIRMATION,)
    names = [spec.name for spec in classes]
    schema = plan_schema(names, 3, cover_all=True)
    assert [branch["contains"]["properties"]["frame_class"]["const"]
            for branch in schema["properties"]["steps"]["allOf"]] == names
    system_text, user_text = render_plan_prompt_texts(
        CLASS_INSTRUCTION, classes, "ticket_booking", 3, cover_all=True)
    assert user_text.endswith("中每个帧类都至少出现一次。")   # 冻结的覆盖句变体
    prompt = PromptBundle(
        messages=(Message(role="system", parts=(Part(kind="text", text=system_text),)),
                  Message(role="user", parts=(Part(kind="text", text=user_text),))),
        temperature=0.0)

    obj, usage, attempts, model = await ctx.schema_engine.complete_validated(
        "default", prompt, schema=schema,                      # ONE real call
        scope=CallScope(batch_no=0))

    steps = obj["steps"]
    assert len(steps) == 3                        # minItems = maxItems 钉死步数
    assert {step["frame_class"] for step in steps} == set(names)   # 逐类覆盖
    assert all(str(step["brief"]).strip() for step in steps)
    jsonschema.Draft202012Validator(schema).validate(obj)
    assert attempts >= 1 and model
    assert usage.prompt_tokens > 0 and usage.completion_tokens > 0
