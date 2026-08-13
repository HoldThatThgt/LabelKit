"""M6 generate —— 从种子合成新文本记录（spec 3.6，CONTRACTS §7.5）。

process 模式：种子即当前批质量闸的幸存者；``run()`` 返回由新 PipelineItem 组成的子批
（输入批永不改动）。generate_only 模式（v1.4）：``generate_all()`` 一次性产出全部
Record——有种子池时取 ``generate.seed_examples``，无种子形态则由 ``generate.instruction``
× 风格按 ``standalone_count`` 目标产出。

v1.7 按类种子池（classify 开启、process 模式；spec 3.6.2 按类种子池，R17–R19）：种子按
``item.classification.label`` 分组；参与类按类名字典序占据连续的全局调用序区间；每次调用
取类有效的 instruction/styles/num_per_record/temperature，而 llms/mixture/weights/
seeds_per_call/num_per_call 恒读全局段。新记录继承种子类
（``Classification(label, (label,), "inherited", {})``）。classify 关闭 ⇒ 单个匿名段，
等价 v1.7 之前的行为，抽签流亦逐字节一致。generate_only 的 ``generate_all`` 路径保持平面
（全局指令，无类段）。

全部随机性来自 ``ctx.rng``：(llm, style) 的整体指派与逐调用种子抽取都在任何派发之前按调用
序完成，故结果与并发调度无关（spec 3.6.2）。新样本须通过针对种子与彼此的 MinHash 相似度
过滤（Self-Instruct filter，阈值 = dedup.minhash_threshold）。

v1.13 时间流形态（SPEC-stream-generation §3.2，``generate_stream.enabled``）：generate_only
的第三形态——LLM 只做两类内容调用（一序列一次蓝图、一次帧实现，噪音帧批量实现复用平面
模板），装箱/交叉/噪音/重复/时间戳全部由机械交织器完成；单流 ``Random(f"{seed}:0:generate")``
按裁决·抽签消费顺序表三段消费（计划期①②③、派发期零消费、交织期④–⑨）。产物一式两份：
可重放的时间流工件行（工件行即 raw——重放同 id；真值不携最终 id——循环依赖封死）与直装
序列信封（两级 inherited 标签 + session_id）。``generate_all`` 平面路径零改动。
"""
from __future__ import annotations

import asyncio
import dataclasses
import hashlib
import json
import logging
import math
import re
import statistics
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Sequence

from datasketch import MinHash, MinHashLSH

from labelkit.common.errors import (
    CircuitBreakerTripped,
    ContextOverflowError,
    ErrorKind,
    LabelKitError,
    ProviderFatalError,
    ProviderRetryableError,
    SchemaViolation,
)
from labelkit.common.contracts.types import Classification, PipelineItem, Record, RecordRef
from labelkit.common.runtime import budget
from labelkit.common.runtime.schema_engine import CallScope

if TYPE_CHECKING:
    import random
    from typing import Mapping

    from labelkit.common.config.model import GenerateConfig, GenerateStyle, ResolvedConfig
    from labelkit.common.contracts.stage import RunContext
    from labelkit.common.runtime.llm_client import PromptBundle

# M6 的观测面只有 report.generate.buckets 计数器（spec 3.6.2 溯源与可观测，
# CONTRACTS §7.5）。没有 M6 专属 trace 事件：§8.1 目录未为 generate 定义任何事件，
# "generate" 也不是合法的 trace.channels 取值。作废调用经已编目的 llm.call /
# schema.repair 事件（M9/M8）加下方值-free stderr 日志保持可观测。
_log = logging.getLogger("labelkit.generate")


# ── 规范化辅助 ─────────────────────────────────────────────────────────────

def canonical_json(obj) -> str:
    """M2 的规范 JSON 序列化，即生成记录 id 的计算输入（CONTRACTS §3）。

    :param obj: 待序列化对象。
    :returns: 键序稳定、非 ASCII 保真、无冗余空白的紧凑 JSON 文本。
    """
    return json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def make_generated_record(sample: str, text_field: str, seed_ids: Sequence[str],
                          llm: str, style: str | None) -> Record:
    """按 spec 3.6.2 新记录构造装配一条生成记录。

    :param sample: LLM 产出的样本文本。
    :param text_field: raw 对象承载文本的字段名（``input.text_field``）。
    :param seed_ids: 本次调用实际送出的种子记录 id；无种子形态为空序列。
    :param llm: 产出该样本的 [llm.*] profile 名。
    :param style: 产出该样本的风格名；None = 未启用风格条件化。
    :returns: 冻结的新 Record（id = raw 规范 JSON 的 sha256 前 16 位）。
    """
    raw = {text_field: sample}
    rec_id = hashlib.sha256(canonical_json(raw).encode("utf-8")).hexdigest()[:16]
    return Record(
        id=rec_id,
        modality="text",
        text=sample,
        raw=raw,
        ui_tree=None,
        image=None,
        ref=RecordRef(
            source_file="",
            line_no=None,
            pair_index=None,
            generated_from=tuple(seed_ids),
            generator={"llm": llm, "style": style},
        ),
    )


def bucket_key(llm: str, style: str | None, class_name: str | None = None) -> str:
    """报告桶键 ``<llm>×<style|null>``（CONTRACTS §7.5 [FROZEN]）。

    v1.7：归属类段的调用（classify 开启、process 模式）多带一节类前缀——
    ``<class>×<llm>×<style|null>``，分隔符同为字面量 ``×``。

    :param llm: [llm.*] profile 名。
    :param style: 风格名；None 渲染为字面量 ``null``。
    :param class_name: owning 类段名；None（classify 关闭与平面 generate_only
        路径）保持两节形态逐字节不变。
    :returns: 桶键字符串。
    """
    tail = f"{llm}×{style if style is not None else 'null'}"
    return tail if class_name is None else f"{class_name}×{tail}"


# ── 提示词装配（§10.4，确定性模板）────────────────────────────────────────

def render_prompt_texts(instruction: str, style_prompt: str | None,
                        num_per_call: int, seed_texts: Sequence[str]) -> tuple[str, str]:
    """平面生成提示词的纯文本装配。

    :param instruction: 类有效生成指令。
    :param style_prompt: 风格提示词；None = 不带风格段。
    :param num_per_call: 单次调用要求的样本条数。
    :param seed_texts: 本次调用送出的种子文本；空 = 无种子形态。
    :returns: (system_text, user_text) 二元组。
    """
    system_lines = [instruction]
    if style_prompt is not None:
        system_lines.append(f"[风格要求] {style_prompt}")
    system_lines.append("输出必须是符合以下结构的单个 JSON 对象，不输出任何其他内容：")
    system_lines.append('{"samples": [<新样本文本>, ...]}' + f"（恰 {num_per_call} 条）")
    user_lines = [f"[种子示例 {i}] {text}" for i, text in enumerate(seed_texts, start=1)]
    user_lines.append(f"请生成 {num_per_call} 条全新样本。")
    return "\n".join(system_lines), "\n".join(user_lines)


def build_generate_prompt(instruction: str, style_prompt: str | None, num_per_call: int,
                          seed_texts: Sequence[str], temperature: float) -> "PromptBundle":
    """把平面生成模板装配成可派发的 PromptBundle。

    :param instruction: 类有效生成指令。
    :param style_prompt: 风格提示词；None = 不带风格段。
    :param num_per_call: 单次调用要求的样本条数。
    :param seed_texts: 本次调用送出的种子文本。
    :param temperature: 类有效温度。
    :returns: 单 system + 单 user 的 PromptBundle。
    """
    # 懒导入：本模块的纯逻辑要在 M9 就位之前也能被导入。
    from labelkit.common.runtime.llm_client import Message, Part, PromptBundle

    system_text, user_text = render_prompt_texts(instruction, style_prompt,
                                                 num_per_call, seed_texts)
    return PromptBundle(
        messages=(
            Message(role="system", parts=(Part(kind="text", text=system_text),)),
            Message(role="user", parts=(Part(kind="text", text=user_text),)),
        ),
        temperature=temperature,
    )


def _samples_schema(num_per_call: int) -> dict:
    """取平面生成调用的内部 samples Schema。

    :param num_per_call: 单次调用要求的样本条数（= 定长数组长度）。
    :returns: draft 2020-12 Schema 对象。
    """
    # 懒导入：内部 Schema 构造器归 M8（CONTRACTS §7.7/§10.7）。
    from labelkit.common.runtime.schema_engine import samples_schema

    return samples_schema(num_per_call)


# ── 预抽调用计划（spec 3.6.2 多模型混合 / 风格条件化 / v1.7 类段）──────────

@dataclass(frozen=True)
class CallPlan:
    """一次平面生成调用的预抽计划（派发前定稿，抽签流与并发调度无关）。"""
    index: int                          # 全局调用序号 0..C-1（跨类段连续编号）
    llm: str                            # [llm.*] profile 名
    style_name: str | None              # 预抽风格名；None = 未启用风格条件化
    style_prompt: str | None            # 预抽风格提示词；None = 不带风格段
    seed_ids: tuple[str, ...]           # process 模式抽中的种子记录 id；否则 ()
    seed_texts: tuple[str, ...]         # 抽中的种子文本（() = 无种子形态）
    class_name: str | None = None       # v1.7（R17）owning 类段；None = 匿名段
                                        # （classify 关闭 / generate_only 平面路径）


@dataclass(frozen=True)
class ClassSegment:
    """一个类段的计划期输入（v1.7，R18）——或复现 v1.7 之前行为的那个唯一匿名段
    （class_name=None）。"""
    class_name: str | None                      # 类名；None = 匿名段
    seeds: tuple[tuple[str | None, str], ...]   # (记录 id 或 None, 文本)；() = 无种子
    num_calls: int                              # 段预算 C_c
    styles: tuple["GenerateStyle", ...]         # 类有效风格池（() = 无风格）


def predraw_llm_style(
    g: "GenerateConfig", num_calls: int, rng: "random.Random",
    styles_by_index: Sequence[tuple["GenerateStyle", ...]] | None = None,
) -> list[tuple[str, "GenerateStyle | None"]]:
    """用 ctx.rng 为每个调用序号 0..num_calls-1 预抽 (llm, style) 组合。

    round_robin：llms[i % len(llms)]（llm 不消费 rng）；weighted：逐位
    rng.choices；风格池非空时逐位 rng.choice 均匀抽取。

    :param g: 全局 [generate] 配置段（llms/mixture/weights/styles 从这里读）。
    :param num_calls: 待预抽的调用总数。
    :param rng: 单流 PRNG（消费顺序即抽签流，测试钉死）。
    :param styles_by_index: v1.7（R18）逐全局序号给出 owning 类的有效风格池；
        None = 各处统一用 g.styles（抽签流完全一致）。
    :returns: 与调用序号对位的 (llm, style) 列表。
    """
    pairs: list[tuple[str, "GenerateStyle | None"]] = []
    for i in range(num_calls):
        if g.mixture == "weighted":
            llm = rng.choices(list(g.llms), weights=list(g.weights), k=1)[0]
        else:
            llm = g.llms[i % len(g.llms)]
        styles = g.styles if styles_by_index is None else styles_by_index[i]
        style = rng.choice(styles) if styles else None
        pairs.append((llm, style))
    return pairs


def build_segment_plans(g: "GenerateConfig", segments: Sequence[ClassSegment],
                        rng: "random.Random",
                        exec_calls: int | None = None) -> list[CallPlan]:
    """跨类段拼接的完整派发前计划（v1.7，R18）。

    各段按给定顺序占据连续的全局调用序区间（调用方已把参与类按字典序排好）。一趟
    预抽覆盖全部序号——llm 完全照旧按全局序号取，风格取 owning 段的风格池——故
    ``--limit`` 截断不扰动抽签流；随后按全局序号升序，逐个待执行调用从 owning 段的
    种子池抽种子。单个匿名段能逐字节复现 v1.7 之前的计划。

    :param g: 全局 [generate] 配置段。
    :param segments: 已排序的类段序列。
    :param rng: 单流 PRNG。
    :param exec_calls: 实际执行的调用数；None = 全部执行（超出总数按总数截断）。
    :returns: 按全局调用序排列的 CallPlan 列表。
    """
    total_calls = sum(seg.num_calls for seg in segments)
    if exec_calls is None:
        exec_calls = total_calls
    exec_calls = min(exec_calls, total_calls)
    owner: list[ClassSegment] = []
    for seg in segments:
        owner.extend([seg] * seg.num_calls)
    pairs = predraw_llm_style(g, total_calls, rng,
                              styles_by_index=[seg.styles for seg in owner])
    plans: list[CallPlan] = []
    for i in range(exec_calls):
        seg = owner[i]
        llm, style = pairs[i]
        if seg.seeds:
            k = min(g.seeds_per_call, len(seg.seeds))
            drawn = rng.sample(list(seg.seeds), k)
        else:
            drawn = []
        plans.append(CallPlan(
            index=i,
            llm=llm,
            style_name=style.name if style else None,
            style_prompt=style.prompt if style else None,
            seed_ids=tuple(sid for sid, _ in drawn if sid is not None),
            seed_texts=tuple(text for _, text in drawn),
            class_name=seg.class_name,
        ))
    return plans


def build_call_plans(g: "GenerateConfig", seeds: Sequence[tuple[str | None, str]],
                     num_calls: int, rng: "random.Random",
                     exec_calls: int | None = None) -> list[CallPlan]:
    """v1.7 之前的平面计划：单个匿名段 + 全局风格池。作为零改动回归锚保留——
    分段规划器在单匿名段下的抽签流与 v1.7 之前的实现完全一致。

    :param g: 全局 [generate] 配置段。
    :param seeds: 匿名段种子池 [(记录 id 或 None, 文本), ...]。
    :param num_calls: 段预算。
    :param rng: 单流 PRNG。
    :param exec_calls: 实际执行的调用数；None = 全部执行。
    :returns: 按全局调用序排列的 CallPlan 列表。
    """
    segment = ClassSegment(class_name=None, seeds=tuple(seeds),
                           num_calls=num_calls, styles=g.styles)
    return build_segment_plans(g, [segment], rng, exec_calls=exec_calls)


# ── MinHash 相似度过滤（Self-Instruct，spec 3.6.2 回流 / 3.3.3）─────────────

def _normalize(text: str) -> str:
    """与 M3 dedup 同款的文本归一化：NFC + 空白串折叠 + 去首尾空白。

    :param text: 原始文本。
    :returns: 归一化后的文本。
    """
    return re.sub(r"\s+", " ", unicodedata.normalize("NFC", text)).strip()


class SimilarityFilter:
    """生成样本对种子、以及样本彼此之间的 MinHash-LSH 近重过滤器。

    在归一化文本上取字符 n-gram shingle；探针与任一已存文本的估计 Jaccard ≥ 阈值
    即判为近重。阈值缺省取 spec 的 0.85（dedup.minhash_threshold）。"""

    def __init__(self, threshold: float = 0.85, num_perm: int = 128, ngram: int = 5):
        """构造过滤器。

        :param threshold: 近重判定的 Jaccard 阈值。
        :param num_perm: MinHash 置换数。
        :param ngram: 字符 shingle 长度。
        """
        self._threshold = threshold
        self._num_perm = num_perm
        self._ngram = ngram
        self._lsh = MinHashLSH(threshold=threshold, num_perm=num_perm)
        self._sigs: dict[str, MinHash] = {}

    def _minhash(self, text: str) -> MinHash:
        """计算一段文本的 MinHash 签名。

        :param text: 待签名文本。
        :returns: MinHash 签名对象。
        """
        norm = _normalize(text)
        if len(norm) >= self._ngram:
            shingles = {norm[i:i + self._ngram] for i in range(len(norm) - self._ngram + 1)}
        else:
            shingles = {norm}
        m = MinHash(num_perm=self._num_perm)
        for s in shingles:
            m.update(s.encode("utf-8"))
        return m

    def _is_duplicate(self, m: MinHash) -> bool:
        """在 LSH 索引里精确复核候选签名是否构成近重。

        :param m: 待判定的 MinHash 签名。
        :returns: True = 与某条已存文本的 Jaccard ≥ 阈值。
        """
        for key in self._lsh.query(m):
            if m.jaccard(self._sigs[key]) >= self._threshold:
                return True
        return False

    def add(self, text: str) -> None:
        """无条件把一段文本纳入索引（种子入库用，不做判重）。

        :param text: 待入库文本。
        """
        m = self._minhash(text)
        key = f"s{len(self._sigs)}"
        self._sigs[key] = m
        self._lsh.insert(key, m)

    def probe_and_add(self, text: str) -> bool:
        """判重并在新颖时入库。

        :param text: 待判定文本。
        :returns: True = 新颖（已入库）；False = 近重（未入库）。
        """
        m = self._minhash(text)
        if self._is_duplicate(m):
            return False
        key = f"s{len(self._sigs)}"
        self._sigs[key] = m
        self._lsh.insert(key, m)
        return True


# ── 种子选取（process 模式，spec 3.6.2 种子选取 / v1.7 按类种子池）─────────

def select_seeds(batch: Sequence[PipelineItem],
                 cfg: "ResolvedConfig") -> dict[str | None, list[tuple[str, str]]]:
    """按类分组挑选种子池（v1.7，R19）。

    classify 开启 ⇒ 键 = ``item.classification.label``；关闭 ⇒ 单个匿名组（键
    None），选取逻辑与 v1.7 之前完全一致。逐组阈值链：全局
    ``generate.seed_min_score`` → 缺省时取类有效的 ``quality.threshold``（匿名组取
    全局值）→ 仍缺省时取该组自身已评分池的聚合分中位数。未评分条目永不做种子；
    无人过阈的组直接缺席。键按类名字典序排列，故迭代序即类段序。

    :param batch: 当前批（只看 ``status == "active"`` 且已评分的条目）。
    :param cfg: 已解析配置。
    :returns: {类名或 None: [(记录 id, 文本), ...]}。
    """
    pools: dict[str | None, list[tuple[PipelineItem, float]]] = {}
    for item in batch:
        if item.status != "active":
            continue
        agg = item.scores.get("__aggregate__")
        if agg is None or agg.score is None:
            continue
        if cfg.classify.enabled and item.classification is not None:
            label: str | None = item.classification.label
        else:
            label = None
        pools.setdefault(label, []).append((item, agg.score))
    selected: dict[str | None, list[tuple[str, str]]] = {}
    for label in sorted(pools, key=lambda l: l or ""):
        scored = pools[label]
        threshold = cfg.generate.seed_min_score
        if threshold is None:
            effective_quality = (cfg.class_views[label].quality if label is not None
                                 else cfg.quality)
            threshold = effective_quality.threshold
        if threshold is None:
            threshold = statistics.median(s for _, s in scored)
        seeds = [(item.record.id, item.record.text or "")
                 for item, score in scored if score >= threshold]
        if seeds:
            selected[label] = seeds
    return selected


# ── 类有效配置 + 类段装配（v1.7）───────────────────────────────────────────

def effective_generate(cfg: "ResolvedConfig", class_name: str | None) -> "GenerateConfig":
    """取类有效的 [generate] 段（R17）。

    类段取 ``class_views[class].generate``，匿名段取全局段。按 5.2 白名单，只有
    instruction / styles / num_per_record / temperature 可以按类不同；llms /
    mixture / weights / seeds_per_call / num_per_call 由调用方从全局段读取。

    :param cfg: 已解析配置。
    :param class_name: 类名；None = 匿名段。
    :returns: 类有效的 GenerateConfig。
    """
    if class_name is None:
        return cfg.generate
    return cfg.class_views[class_name].generate


def build_class_segments(pools: "Mapping[str | None, list[tuple[str, str]]]",
                         cfg: "ResolvedConfig") -> list[ClassSegment]:
    """把分组种子池按类名字典序切成类段（R18）。

    段预算 C_c = ceil(len(seeds_c) × num_per_record_c / num_per_call)，其中
    num_per_record 取类有效值、num_per_call 取全局值。

    :param pools: ``select_seeds`` 的分组结果。
    :param cfg: 已解析配置。
    :returns: 按类名字典序排列的 ClassSegment 列表。
    """
    segments: list[ClassSegment] = []
    for label in sorted(pools, key=lambda l: l or ""):
        seeds_c = pools[label]
        gen_c = effective_generate(cfg, label)
        segments.append(ClassSegment(
            class_name=label,
            seeds=tuple(seeds_c),
            num_calls=math.ceil(len(seeds_c) * gen_c.num_per_record
                                / cfg.generate.num_per_call),
            styles=gen_c.styles,
        ))
    return segments


# ── 后处理：过滤 + 记录构造 + 桶统计 ───────────────────────────────────────

class _SampleGate:
    """样本级用户回调闸门（v1.5 plan A，spec 3.6.2）。

    在相似度过滤之前逐样本执行 ``generate.sample_validator``。过滤语义：违规样本
    直接剔除（不重试、不产 failed 记录），按桶计数由调用方负责；回调自身抛异常视同
    违规，并只提示一次。未配置钩子时闸门常开。"""

    def __init__(self, hook_ref: str | None):
        """解析并持有回调。

        :param hook_ref: ``generate.sample_validator`` 的 ``module:function``
            引用；None 或空串 ⇒ 闸门常开。
        """
        # 懒导入 + 每次解析：钩子解析面允许被测试替换，构造期解析保持单次开销。
        self._hook_ref = hook_ref
        self._hook = None
        if hook_ref:
            from labelkit.common.extensions.hooks import resolve_hook
            self._hook = resolve_hook(hook_ref)
        self._warned = False

    @property
    def enabled(self) -> bool:
        """@return 闸门是否生效（即是否配置了 sample_validator）。"""
        return self._hook is not None

    def violates(self, sample: str) -> bool:
        """判定单个样本是否违规。

        :param sample: 待判定的生成样本文本。
        :returns: True = 违规须剔除；False = 放行。
        """
        from labelkit.common.extensions.hooks import normalize_violations
        try:
            violations = normalize_violations(self._hook(sample), self._hook_ref)
        except Exception as exc:            # 钩子缺陷：剔除命中样本，绝不中断整轮
            if not self._warned:
                self._warned = True
                _log.warning(
                    "generate.sample_validator raised; the offending sample is "
                    "dropped as a violation (warned once): %s: %s",
                    type(exc).__name__, exc,
                    extra={"stage": "generate", "batch": 0})
            violations = ["callback raised"]
        return bool(violations)


@dataclass(frozen=True)
class _PostprocessContext:
    """``postprocess_samples`` 逐调用后处理共享的只读上下文。"""
    gate: _SampleGate               # 样本级回调闸门
    filt: SimilarityFilter          # 相似度过滤器（已注入种子）
    cfg: "ResolvedConfig"           # 已解析配置（读 input.text_field）
    metrics: object                 # MetricsSink 鸭子面（只做 count）


def postprocess_samples(plans: Sequence[CallPlan],
                        results: Sequence[list[str] | None],
                        seed_texts: Sequence[str],
                        cfg: "ResolvedConfig",
                        metrics) -> list[tuple[Record, str | None]]:
    """派发后的确定性装配，严格按调用序处理。

    ``results[i]`` 是第 i 次调用的样本列表，作废调用（M8 修复后仍非法 / 重试穷尽）
    为 None：其桶只计 ``calls`` 而 ``produced`` 为 0，且不产 failed 记录
    （spec 3.6.3）。桶计数器（CONTRACTS §9.3）：calls = 已派发调用数；produced =
    LLM 返回的样本数；survived_dedup = 通过 MinHash 相似度过滤的样本数（只有它们
    成为 Record）。v1.7（R17）：返回 (记录, 类名) 对——类名取产出方计划的
    class_name（匿名段为 None）——且类段调用使用三节桶键
    ``<class>×<llm>×<style|null>``。

    :param plans: 与 results 对位的调用计划序列。
    :param results: 逐调用样本列表；None = 该调用作废。
    :param seed_texts: 全部种子文本（先行注入相似度过滤器）。
    :param cfg: 已解析配置。
    :param metrics: MetricsSink 鸭子面。
    :returns: (新记录, 类名) 对的列表，按调用序排列。
    """
    d = cfg.dedup
    filt = SimilarityFilter(threshold=d.minhash_threshold,
                            num_perm=d.minhash_num_perm, ngram=d.ngram)
    for text in seed_texts:
        filt.add(text)
    pc = _PostprocessContext(gate=_SampleGate(cfg.generate.sample_validator),
                             filt=filt, cfg=cfg, metrics=metrics)
    records: list[tuple[Record, str | None]] = []
    for plan, samples in zip(plans, results):
        key = bucket_key(plan.llm, plan.style_name, plan.class_name)
        metrics.count(f"generate.buckets.{key}.calls")
        if pc.gate.enabled:
            metrics.count(f"generate.buckets.{key}.rejected_by_validator", 0)
        if samples is None:
            continue
        metrics.count(f"generate.buckets.{key}.produced", len(samples))
        records.extend(_accept_samples(plan, samples, pc))
    return records


def _accept_samples(plan: CallPlan, samples: Sequence[str],
                    pc: _PostprocessContext) -> list[tuple[Record, str | None]]:
    """单次调用返回样本的接收流水：回调闸门 → 相似度过滤 → 新记录构造。

    :param plan: 产出这批样本的调用计划。
    :param samples: LLM 返回的样本文本列表。
    :param pc: 逐调用共享的只读后处理上下文。
    :returns: 该调用最终成记录的 (记录, 类名) 对列表。
    """
    key = bucket_key(plan.llm, plan.style_name, plan.class_name)
    accepted: list[tuple[Record, str | None]] = []
    for sample in samples:
        if pc.gate.enabled and pc.gate.violates(sample):
            pc.metrics.count(f"generate.buckets.{key}.rejected_by_validator")
            continue
        if not pc.filt.probe_and_add(sample):
            continue
        rec = make_generated_record(sample, pc.cfg.input.text_field,
                                    plan.seed_ids, plan.llm, plan.style_name)
        pc.metrics.count(f"generate.buckets.{key}.survived_dedup")
        # 注意：counts.generated 归 M10（orchestrator）所有，由它统计从
        # generate_all/GenerateStage 收到的记录数。这里再自增会在 report.counts
        # 里双计（§9.3 不变式）。
        accepted.append((rec, plan.class_name))
    return accepted


def _error_kind(exc: LabelKitError) -> str:
    """把一个 LabelKitError 归到 §7.6 错误种类（stderr 一行里的 kind 字段）。

    :param exc: 作废调用捕获到的异常。
    :returns: §7.6 错误种类字符串。
    """
    # v1.11（V27①）：预算词汇优先路由——context_overflow / output_truncated 的
    # 作废绝不能在 stderr 一行里显示成 internal_error。
    kind = budget.classify_stage_error(exc)
    if kind is not None:
        return kind
    if isinstance(exc, SchemaViolation):
        return ErrorKind.SCHEMA_VIOLATION.value
    if isinstance(exc, ProviderRetryableError):
        return ErrorKind.PROVIDER_RETRYABLE_EXHAUSTED.value
    if isinstance(exc, ProviderFatalError):
        return ErrorKind.PROVIDER_FATAL.value
    return ErrorKind.INTERNAL_ERROR.value


# ── v1.11 种子装填（spec 3.6.2 上下文预算装填 row / §3.3⑦）──────────────────

def _fit_plan_seeds(plan: CallPlan, cfg: "ResolvedConfig") -> tuple[CallPlan, bool, bool]:
    """预算声明后把 seeds_per_call 降格为上界，按需裁剪单次调用的种子。

    种子从 rng 抽取序的尾部开始丢弃（绝不重抽——确定性），直到该调用的提示词估算
    装得进目标 profile 的输入预算；最少保留 1 条种子。(llm, style) 预抽与轮转序丝毫
    不动，故裁剪后的计划仍逐调用可复现（含 llms 混合）。系统侧（指令 / 风格 /
    输出结构句）是静态量——归 V13③ 的 M1 预检管辖，这里永不裁剪。

    :param plan: 待装填的调用计划。
    :param cfg: 已解析配置。
    :returns: (裁剪后的计划, 是否发生裁剪, 是否不可装填)；不可装填 = 连 1 条种子
        （或无种子形态的空提示词）都装不下，该调用按 V10 由派发方处置（作废，
        kind = context_overflow）。预算未声明（profile 缺失 / cw == 0）时恒返回
        (plan, False, False)，逐字节等价预算关闭前的行为。
    """
    prof = cfg.llm_profiles.get(plan.llm)
    if prof is None or prof.context_window <= 0:
        return plan, False, False
    g = cfg.generate
    gen_c = effective_generate(cfg, plan.class_name)
    available = _generate_input_budget(prof, g.num_per_call)

    def fits(seed_texts: Sequence[str]) -> bool:
        """估算给定种子集下的提示词是否装得进输入预算。

        :param seed_texts: 候选种子文本（抽取序的前缀）。
        :returns: True = 装得下。
        """
        system_text, user_text = render_prompt_texts(
            gen_c.instruction, plan.style_prompt, g.num_per_call, seed_texts)
        est = (budget.est_text(system_text) + budget.est_text(user_text)
               + 2 * budget.MSG_OVERHEAD_TOKENS)
        return est <= available

    return _tail_drop_seeds(plan, fits)


def _generate_input_budget(prof, num_per_call: int) -> int:
    """平面生成调用可用的输入预算。

    supports_structured_output 的 profile 上 response_schema 随请求上行，故要从
    输入预算里另行扣除其文本量。

    :param prof: 目标 [llm.*] profile（已确认声明了 context_window）。
    :param num_per_call: 单次调用要求的样本条数（决定 samples Schema 文本量）。
    :returns: 提示词侧可用的 token 预算。
    """
    available = budget.input_budget(prof)
    if prof.supports_structured_output:
        available -= budget.est_text(json.dumps(_samples_schema(num_per_call),
                                                ensure_ascii=False))
    return available


def _tail_drop_seeds(plan: CallPlan, fits) -> tuple[CallPlan, bool, bool]:
    """从 rng 抽取序的尾部逐条丢弃种子，直到提示词装得下（最少保留 1 条）。

    :param plan: 待裁剪的调用计划。
    :param fits: 判定给定种子前缀是否装得下的谓词。
    :returns: 与 ``_fit_plan_seeds`` 同形的 (计划, 是否裁剪, 是否不可装填)。
    """
    n = len(plan.seed_texts)
    for keep in range(n, 0, -1):                # 尾部丢弃：只取抽取序的前缀
        if fits(plan.seed_texts[:keep]):
            if keep == n:
                return plan, False, False
            # process 模式下 seed_ids 与 seed_texts 逐位对齐；generate_only 的
            # 种子池不带 id（空元组保持为空）。
            ids = (plan.seed_ids[:keep] if len(plan.seed_ids) == n
                   else plan.seed_ids)
            return (dataclasses.replace(plan, seed_ids=ids,
                                        seed_texts=plan.seed_texts[:keep]),
                    True, False)
    if n == 0 and fits(()):                     # 无种子形态：没有可丢弃的东西
        return plan, False, False
    return plan, False, True                    # V10：整调用作废，不做任何裁剪


def void_log_message(plan: CallPlan, exc: LabelKitError) -> str:
    """一次作废生成调用的值-free stderr 摘要（spec 3.6.3）。

    只含结构化字段——调用序号、配置标识（llm profile / 风格名）、错误种类、违规
    条数。绝不使用 str(exc)：SchemaViolation 渲染出的违规文本内嵌 LLM 生成的样本
    内容，而 stderr 不得携带数据内容或提示词（CONTRACTS §8.4、§11.7；spec ch.7）。

    :param plan: 作废调用的计划。
    :param exc: 触发作废的异常。
    :returns: 单行值-free 摘要文本。
    """
    msg = (f"generate call voided: call={plan.index} llm={plan.llm} "
           f"style={plan.style_name if plan.style_name is not None else 'null'} "
           f"kind={_error_kind(exc)}")
    if isinstance(exc, SchemaViolation):
        msg += f" violations={len(exc.errors)}"
    return msg


# ── v1.13 时间流形态：模板（§10.14/§10.15，实现即冻结面）────────────────────

# 蓝图模板静态脚手架：budget.TEMPLATE_HEAD_TOKENS["generate_plan"] 钉住
# est_text(_PLAN_SYSTEM_STATIC)（V22 家族跨层等式，tests/common/runtime/
# test_budget.py 守护两侧同步）；类生成指令与帧类表是配置量，在 M1 静态预算
# 预检（V13③）各自计量。
_PLAN_SYSTEM_HEAD = ("你是时间流数据规划器。给定任务描述与帧类表，为一条序列规划逐步蓝图："
                     "每一步选定一个帧类，并用一句话写明该步内容要点。")
_PLAN_LABEL_TASK = "[任务]"
_PLAN_LABEL_FRAME_TABLE = "[帧类表]"
_PLAN_STRUCTURE = (
    "输出必须是符合以下结构的单个 JSON 对象，不输出任何其他内容：\n"
    '{"steps": [{"frame_class": <帧类名>, "brief": <一句话要点>}, ...]}\n'
    "字段说明：steps 恰为要求的步数，一步一项，按时间顺序排列；frame_class 必须取自 "
    "[帧类表] 中的帧类名；brief 用一句话写明该步内容要点，供逐帧实现展开。")
_PLAN_SYSTEM_STATIC = "\n".join((_PLAN_SYSTEM_HEAD, _PLAN_LABEL_TASK,
                                 _PLAN_LABEL_FRAME_TABLE, _PLAN_STRUCTURE))

# 帧实现模板静态脚手架：TEMPLATE_HEAD_TOKENS["generate_realize"] 钉住
# est_text(_REALIZE_SYSTEM_STATIC)。逐位契约行把帧类生成 Schema 文本按步重复——
# L0 关端点（DeepSeek anthropic 路由硬拒强制 tool call）上结构服从性靠该契约。
_REALIZE_LABEL_TASK = "[任务]"
_REALIZE_LABEL_STYLE = "[风格要求]"
_REALIZE_STRUCTURE = (
    "输出必须是符合以下结构的单个 JSON 对象，不输出任何其他内容：\n"
    '{"frames": [<第 1 帧内容>, <第 2 帧内容>, ...]}\n'
    "字段说明：frames 恰为蓝图步数，一帧一项，与蓝图步序逐位对应；逐帧内容契约如下：")
_REALIZE_FREE_TEXT = "自由文本一段"
_REALIZE_SYSTEM_STATIC = "\n".join((_REALIZE_LABEL_TASK, _REALIZE_LABEL_STYLE,
                                    _REALIZE_STRUCTURE))

_MAX_STREAM_DEGRADE_LEVELS = 2   # 实现调用对半降级级数上限（裁决·预算头两键，AIMD ≤2）


def render_plan_prompt_texts(instruction: str, frame_classes: Sequence,
                             class_name: str, length: int) -> tuple[str, str]:
    """蓝图调用的纯文本装配（§10.14）：返回 (system_text, user_text)。

    :param instruction: 类有效生成指令（[class.<name>.generate].instruction）。
    :param frame_classes: 全帧类表（[[frame.classify.classes]] 的 ClassSpec 序列）。
    :param class_name: 序列类名（user 段引用）。
    :param length: 步数 L（与 plan_schema 的 minItems=maxItems 同源）。
    """
    table = "\n".join(f"{c.name}: {c.description}" for c in frame_classes)
    system = "\n".join((_PLAN_SYSTEM_HEAD,
                        f"{_PLAN_LABEL_TASK} {instruction}",
                        f"{_PLAN_LABEL_FRAME_TABLE}\n{table}",
                        _PLAN_STRUCTURE))
    return system, f"请为一条「{class_name}」序列产出 {length} 步蓝图。"


def render_realize_prompt_texts(instruction: str, style_prompt: str | None,
                                steps: Sequence[tuple[str, str]],
                                contracts: Sequence[str]) -> tuple[str, str]:
    """帧实现调用的纯文本装配（§10.15）：返回 (system_text, user_text)。

    :param instruction: 类有效生成指令。
    :param style_prompt: 预抽风格提示；None = 无风格段（蓝图不带风格，实现才带）。
    :param steps: 蓝图步序列 [(frame_class, brief), ...]（对半降级时为切片，局部重编号）。
    :param contracts: 与 steps 对位的逐帧内容契约文本（Schema 单行 dump 或自由文本句）。
    """
    lines = [f"{_REALIZE_LABEL_TASK} {instruction}"]
    if style_prompt is not None:
        lines.append(f"{_REALIZE_LABEL_STYLE} {style_prompt}")
    lines.append(_REALIZE_STRUCTURE)
    for i, ((frame_class, _brief), contract) in enumerate(zip(steps, contracts), 1):
        lines.append(f"第 {i} 帧（{frame_class}）须符合：{contract}")
    user_lines = [f"{i}. [{frame_class}] {brief}"
                  for i, (frame_class, brief) in enumerate(steps, 1)]
    user_lines.append(f"请实现全部 {len(steps)} 帧内容。")
    return "\n".join(lines), "\n".join(user_lines)


def _plan_schema(names: Sequence[str], length: int) -> dict:
    """取蓝图调用的内部 Schema。

    :param names: 全帧类名闭集（逐步 frame_class 的 enum 域）。
    :param length: 步数 L（minItems = maxItems）。
    :returns: draft 2020-12 Schema 对象。
    """
    # 懒导入：内部 Schema 构造器归 M8（CONTRACTS §7.7/§10.7）。
    from labelkit.common.runtime.schema_engine import plan_schema

    return plan_schema(names, length)


def _realize_schema(step_schemas: Sequence[dict]) -> dict:
    """取帧实现调用的内部 Schema（原生 prefixItems 逐位约束）。

    :param step_schemas: 与蓝图步序对位的逐帧内容 Schema。
    :returns: draft 2020-12 Schema 对象。
    """
    # 懒导入：同上。
    from labelkit.common.runtime.schema_engine import realize_schema

    return realize_schema(step_schemas)


def _text_bundle(system_text: str, user_text: str,
                 temperature: float) -> "PromptBundle":
    """单 system + 单 user 的纯文本 PromptBundle（蓝图/实现两模板共用装配尾）。"""
    from labelkit.common.runtime.llm_client import Message, Part, PromptBundle

    return PromptBundle(
        messages=(Message(role="system", parts=(Part(kind="text", text=system_text),)),
                  Message(role="user", parts=(Part(kind="text", text=user_text),))),
        temperature=temperature)


# ── v1.13 时间流形态：计划期纯函数（estimate_run 精确复演共用）──────────────

@dataclass(frozen=True)
class SequencePlan:
    """一条待生成序列的计划期定稿（蓝图与帧实现共用）。"""
    index: int                  # 计划序全局序号 0 基（配额展开序）
    class_name: str             # 所属序列类
    ordinal: int                # 类内序数 0 基（= 工件 truth.sequence）
    length: int                 # 步数 L（rng.randint(类有效 len_range)）
    llm: str                    # 预抽 profile——蓝图+实现绑定同一 profile
    style_name: str | None      # 预抽风格名（实现才生效，蓝图不带风格）
    style_prompt: str | None    # 预抽风格提示词


@dataclass(frozen=True)
class NoiseCallPlan:
    """一次噪音帧批量实现调用的计划期定稿（复用平面生成模板）。"""
    index: int                  # 噪音批调用序号 0 基
    llm: str                    # 独立预抽 profile（裁决·生成键效力矩阵）
    style_name: str | None      # 预抽风格名（全局 styles 池）
    style_prompt: str | None    # 预抽风格提示词


@dataclass(frozen=True)
class StreamPlan:
    """时间流生成的整轮计划期产物（M10 estimate_run 精确复演的同一对象）。"""
    sequences: tuple[SequencePlan, ...]     # 计划序（类字典序 × 类内序数）
    noise_target: int                       # round(noise_ratio × Σ length)
    noise_plans: tuple[NoiseCallPlan, ...]  # ⌈noise_target / num_per_call⌉ 个


@dataclass(frozen=True)
class RealizedSequence:
    """蓝图 + 帧实现都成功后的一条序列（交织器与直装组装的输入单元）。"""
    plan: SequencePlan                      # 该序列的计划期定稿
    frame_classes: tuple[str, ...]          # 蓝图逐步帧类（帧级真值）
    payloads: tuple = ()                    # 逐帧 text_field 值（str 或结构化帧对象）


@dataclass(frozen=True)
class StreamGenerateProduct:
    """``generate_stream_all`` 的富返回（裁决·时间流入口与配额截断）——
    ``PipelineItem(record=r)`` 裸构造无法携带 session_id/classification/
    member_classifications，故必须整信封交付。"""
    envelopes: list[PipelineItem]           # 直装序列信封（计划序）
    artifact_lines: list[str]               # 工件行（交织序定稿；行号 = 列表序 + 1）


def expand_stream_quota(cfg: "ResolvedConfig") -> list[tuple[str, int]]:
    """计划期第①步（零 rng）：类按类名字典序展开配额为 (类名, 类内序数) 列表；
    ``--limit`` 在此做前缀截断（配额层截断 ⇒ 作废序列不再生成、不进交织，工件与
    主输出覆盖面恒一致）。"""
    entries: list[tuple[str, int]] = []
    for name in sorted(cfg.class_views):
        for ordinal in range(cfg.class_views[name].generate.sequences):
            entries.append((name, ordinal))
    if cfg.limit is not None:
        entries = entries[: cfg.limit]
    return entries


def plan_stream(cfg: "ResolvedConfig", rng: "random.Random") -> StreamPlan:
    """计划期纯函数（M10 estimate_run 精确复演共用，裁决·估算精确复演）。

    抽签消费顺序冻结（裁决·抽签消费顺序表，测试钉住）：①配额展开（截断，零 rng）
    ②逐序列 L = rng.randint(类有效 len_range) ③逐序列 (llm, style) 预抽——噪音批
    调用独立预抽，紧随序列预抽在同一 predraw 流内消费（round_robin 不耗 rng、
    weighted 逐位 rng.choices、styles 非空逐位 rng.choice；噪音批取全局 styles）。
    """
    entries = expand_stream_quota(cfg)
    lengths: list[int] = []
    for name, _ in entries:
        lo, hi = cfg.class_views[name].generate.len_range
        lengths.append(rng.randint(lo, hi))
    g = cfg.generate
    noise_target = round(cfg.generate_stream.noise_ratio * sum(lengths))
    n_noise = math.ceil(noise_target / g.num_per_call) if noise_target > 0 else 0
    styles_by_index = ([cfg.class_views[name].generate.styles for name, _ in entries]
                       + [g.styles] * n_noise)
    pairs = predraw_llm_style(g, len(entries) + n_noise, rng,
                              styles_by_index=styles_by_index)
    sequences = tuple(
        SequencePlan(index=i, class_name=name, ordinal=ordinal, length=lengths[i],
                     llm=pairs[i][0],
                     style_name=pairs[i][1].name if pairs[i][1] else None,
                     style_prompt=pairs[i][1].prompt if pairs[i][1] else None)
        for i, (name, ordinal) in enumerate(entries))
    offset = len(entries)
    noise_plans = tuple(
        NoiseCallPlan(index=j, llm=pairs[offset + j][0],
                      style_name=(pairs[offset + j][1].name
                                  if pairs[offset + j][1] else None),
                      style_prompt=(pairs[offset + j][1].prompt
                                    if pairs[offset + j][1] else None))
        for j in range(n_noise))
    return StreamPlan(sequences=sequences, noise_target=noise_target,
                      noise_plans=noise_plans)


# ── v1.13 时间流形态：机械交织器（纯函数族，零 LLM 零 IO）────────────────────

@dataclass
class _StreamSlot:
    """交织后的一帧槽位（工件行装配前形态；仅本模块内部可变）。"""
    payload: "str | Mapping"    # text_field 值（结构化帧 = 行内对象）
    truth: dict                 # 冻结键集 truth（session 值交织尾声回填）
    owner: int | None           # 幸存序列下标（任务帧）；噪音/重复帧 = None
    ts: str = ""                # ⑨ 铺设的 ISO-8601 时间戳


def _sequence_slots(index: int, seq: RealizedSequence) -> list[_StreamSlot]:
    """一条幸存序列的任务帧槽位（truth.session 占位 −1，交织尾声回填）。"""
    return [_StreamSlot(payload=seq.payloads[i],
                        truth={"session": -1, "sequence_class": seq.plan.class_name,
                               "sequence": seq.plan.ordinal,
                               "frame_class": seq.frame_classes[i], "noise": False},
                        owner=index)
            for i in range(len(seq.payloads))]


def _duplicate_slots(seq: RealizedSequence) -> list[_StreamSlot]:
    """⑧ 一条重复序列的流尾新会话槽位：帧 text_field 值逐字节同源（同对象再序列
    化），truth 带 duplicate_of = 原序列类内序数、sequence = null（重发副本无自身
    计划期身份，归属经 duplicate_of 对账——裁决·工件行真值字段集）。"""
    return [_StreamSlot(payload=seq.payloads[i],
                        truth={"session": -1, "sequence_class": seq.plan.class_name,
                               "sequence": None, "frame_class": seq.frame_classes[i],
                               "noise": False, "duplicate_of": seq.plan.ordinal},
                        owner=None)
            for i in range(len(seq.payloads))]


def _noise_slot(payload: str) -> _StreamSlot:
    """一帧插入噪音的槽位（真值三 null + noise=true）。"""
    return _StreamSlot(payload=payload,
                       truth={"session": -1, "sequence_class": None, "sequence": None,
                              "frame_class": None, "noise": True},
                       owner=None)


def _cross_session(slots_a: list[_StreamSlot], slots_b: list[_StreamSlot],
                   rng: "random.Random") -> list[_StreamSlot]:
    """⑥ 单个交叉会话的切换点掷签：形态 A 段+B 段+A 余段[+B 余段]（裁决·会话装箱
    定容）——cut_a ∈ [1, len(A)−1] 保证真交叉（A 必在 B 头部之后回续），cut_b ∈
    [1, len(B)]（= len(B) 时无 B 余段）。A 不足 2 帧时与 B 互换；两者都不足 ⇒
    真交叉不可构造，退化为顺次拼接（纯长度条件，确定性，零 rng 消费）。"""
    if len(slots_a) < 2 <= len(slots_b):
        slots_a, slots_b = slots_b, slots_a
    if len(slots_a) < 2:
        return slots_a + slots_b
    cut_a = rng.randint(1, len(slots_a) - 1)
    cut_b = rng.randint(1, len(slots_b))
    return slots_a[:cut_a] + slots_b[:cut_b] + slots_a[cut_a:] + slots_b[cut_b:]


def _pack_sessions(survivors: Sequence[RealizedSequence], declared: int,
                   rng: "random.Random") -> tuple[list[list[_StreamSlot]], int]:
    """⑤ 装箱定容：洗牌后前 Σ幸存 − sessions_eff 对成对交叉（sessions_eff =
    min(sessions, Σ幸存)），其余单序列会话；会话序 = 洗牌序（交叉会话在前）。"""
    order = list(range(len(survivors)))
    rng.shuffle(order)
    sessions_eff = min(declared, len(order))
    n_cross = len(order) - sessions_eff
    sessions: list[list[_StreamSlot]] = []
    for pair in range(n_cross):
        a, b = order[2 * pair], order[2 * pair + 1]
        sessions.append(_cross_session(_sequence_slots(a, survivors[a]),
                                       _sequence_slots(b, survivors[b]), rng))
    for index in order[2 * n_cross:]:
        sessions.append(_sequence_slots(index, survivors[index]))
    return sessions, n_cross


def _insert_noise(sessions: list[list[_StreamSlot]], payloads: Sequence[str],
                  session_max_len: int, rng: "random.Random") -> int:
    """⑦ 逐噪音帧 (会话, 槽位) 掷签：满员会话（len ≥ session_max_len）退出签池；
    签池耗尽 ⇒ 余帧从交织缺席（不补生成）。返回实际织入帧数。"""
    woven = 0
    for payload in payloads:
        pool = [session for session in sessions if len(session) < session_max_len]
        if not pool:
            _log.warning("noise weaving stopped: every session is at "
                         "stream.session_max_len; %d noise frame(s) dropped",
                         len(payloads) - woven,
                         extra={"stage": "generate", "batch": 0})
            break
        target = rng.choice(pool)
        target.insert(rng.randint(0, len(target)), _noise_slot(payload))
        woven += 1
    return woven


def _lay_timestamps(sessions: list[list[_StreamSlot]], cfg: "ResolvedConfig",
                    rng: "random.Random") -> None:
    """⑨ ts 铺设：起点 ts_start（流首帧零消费）；帧间隔 uniform(frame_gap_s)、会话
    间隔 uniform(gap_s + lo, gap_s + hi)（恒 > stream.gap_s ⇒ 摄取侧按同一 gap_s
    复演出相同会话切分）；datetime + timedelta 正间隔累加 ⇒ 严格递增；isoformat
    微秒精度写出。"""
    lo, hi = cfg.generate_stream.frame_gap_s
    gap = float(cfg.stream.gap_s)
    current = datetime.fromisoformat(cfg.generate_stream.ts_start)
    first = True
    for session in sessions:
        for position, slot in enumerate(session):
            if first:
                first = False
            elif position == 0:
                current += timedelta(seconds=rng.uniform(gap + lo, gap + hi))
            else:
                current += timedelta(seconds=rng.uniform(lo, hi))
            slot.ts = current.isoformat(timespec="microseconds")


def weave_stream(survivors: Sequence[RealizedSequence], noise_payloads: Sequence[str],
                 cfg: "ResolvedConfig", rng: "random.Random",
                 ) -> tuple[list[list[_StreamSlot]], dict]:
    """机械交织器入口（纯函数族，零 LLM 零 IO；裁决·抽签消费顺序表④–⑨单流顺序
    消费）：④重复选取 rng.sample ⑤装箱洗牌+成对交叉 ⑥逐交叉会话切换点 ⑦逐噪音帧
    掷签 ⑧重复序列成流尾新会话（零 rng）⑨ts 铺设；尾声回填 truth.session 全流会话
    序数。返回 (会话列表, counts-only 统计——sessions 不含重复尾会话)。"""
    gs = cfg.generate_stream
    dup_k = min(gs.duplicates, len(survivors))
    if dup_k < gs.duplicates:
        _log.warning("duplicates clamped to the surviving sequence count: %d -> %d",
                     gs.duplicates, dup_k, extra={"stage": "generate", "batch": 0})
    chosen = rng.sample(list(survivors), dup_k) if dup_k else []           # ④
    sessions, crossed = _pack_sessions(survivors, gs.sessions, rng)        # ⑤⑥
    woven_noise = _insert_noise(sessions, noise_payloads,
                                cfg.stream.session_max_len, rng)           # ⑦
    for source in chosen:                                                  # ⑧
        sessions.append(_duplicate_slots(source))
    for session_no, session in enumerate(sessions):
        for slot in session:
            slot.truth["session"] = session_no
    _lay_timestamps(sessions, cfg, rng)                                    # ⑨
    stats = {"sessions": len(sessions) - dup_k, "crossed_sessions": crossed,
             "frames": sum(len(seq.payloads) for seq in survivors),
             "noise_frames": woven_noise, "duplicates": dup_k}
    return sessions, stats


# ── v1.13 时间流形态：直装组装 ───────────────────────────────────────────────

def stream_artifact_path(cfg: "ResolvedConfig") -> str:
    """工件路径推导：输出路径去末级后缀 + ".stream.jsonl"。M11 Emitter 的工件通道
    用同一规则各自推导（算子间不互导，两侧等式由测试钉住）。"""
    return str(Path(cfg.run.output).with_suffix("")) + ".stream.jsonl"


def _payload_text(payload: "str | Mapping") -> str:
    """text_field 值的 M2 语义投影：字符串直取、对象 canonical JSON（重放时 M2
    的 dotted-path 提取产出同一投影——裁决·工件行即 raw）。"""
    return payload if isinstance(payload, str) else canonical_json(payload)


def _stream_envelope(seq: RealizedSequence, records: tuple[Record, ...],
                     session_id: str) -> PipelineItem:
    """一条幸存序列的直装信封：sequence Record（S24 字段惯例、ref = 首成员 ref、
    id = M14 公式 sha256("\\n".join(member ids))[:16]）+ session_id + 序列级/帧级
    两级 inherited 标签（帧级真值随 member_classifications 落 members[]）。"""
    joined = "\n".join(record.id for record in records)
    label = seq.plan.class_name
    sequence_record = Record(
        id=hashlib.sha256(joined.encode("utf-8")).hexdigest()[:16],
        modality="text", text=None, raw=None, ui_tree=None, image=None,
        ref=records[0].ref, kind="sequence", members=records)
    member_classifications = {
        record.id: Classification(label=frame_class, labels=(frame_class,),
                                  source="inherited", detail={})
        for record, frame_class in zip(records, seq.frame_classes)}
    return PipelineItem(
        record=sequence_record, session_id=session_id,
        classification=Classification(label=label, labels=(label,),
                                      source="inherited", detail={}),
        member_classifications=member_classifications)


def assemble_stream(sessions: list[list[_StreamSlot]],
                    survivors: Sequence[RealizedSequence],
                    cfg: "ResolvedConfig") -> tuple[list[str], list[PipelineItem]]:
    """直装组装（裁决·工件行即 raw / 真值不携最终 id）：逐行构造工件行对象
    ``{<ts字段>: …, <text_field>: …, "truth": {…}}``（行序列化 json.dumps
    ensure_ascii=False 族；canonical_json 只用于 id 计算）与成员 Record（id =
    M2 公式、行号 = 列表序 + 1）；session_id = M2 公式（含噪音帧与重复帧）；
    噪音/重复帧只活在工件。信封按计划序返回。"""
    ts_field = cfg.stream.order_by[len("meta:"):]
    text_field = cfg.input.text_field
    path = stream_artifact_path(cfg)
    lines: list[str] = []
    session_ids: list[str] = []
    members: dict[int, list[Record]] = {}
    owner_session: dict[int, int] = {}
    for session_no, session in enumerate(sessions):
        frame_ids: list[str] = []
        for slot in session:
            row = {ts_field: slot.ts, text_field: slot.payload, "truth": slot.truth}
            rec_id = hashlib.sha256(
                canonical_json(row).encode("utf-8")).hexdigest()[:16]
            frame_ids.append(rec_id)
            lines.append(json.dumps(row, ensure_ascii=False))
            if slot.owner is None:
                continue                   # 噪音/重复帧不构造信封
            plan = survivors[slot.owner].plan
            members.setdefault(slot.owner, []).append(Record(
                id=rec_id, modality="text", text=_payload_text(slot.payload),
                raw=row, ui_tree=None, image=None,
                ref=RecordRef(source_file=path, line_no=len(lines), pair_index=None,
                              generated_from=(),
                              generator={"llm": plan.llm, "style": plan.style_name})))
            owner_session.setdefault(slot.owner, session_no)
        session_ids.append(hashlib.sha256(
            "\n".join(frame_ids).encode("utf-8")).hexdigest()[:16])
    envelopes = [_stream_envelope(survivors[owner], tuple(members[owner]),
                                  session_ids[owner_session[owner]])
                 for owner in sorted(members)]
    return lines, envelopes


async def _realize_degrading(realize, span: tuple[int, int], ctx: "RunContext",
                             level: int = 0) -> list[list]:
    """帧实现的反应式对半降级（classify._judge_frames_degrading 零重叠版同型）：
    reactive ContextOverflowError ⇒ [s, m) / [m, e) 顺序重试（每次对半计
    budget.degrade_retries，≤ _MAX_STREAM_DEGRADE_LEVELS 级；schema 与蓝图概要
    随切片同步减半）；precheck 相位、单步跨度或级数耗尽 ⇒ 原样上抛由调用方作废
    序列。返回跨度序的叶结果列表（帧载荷列表）。"""
    try:
        return [await realize(span)]
    except ContextOverflowError as exc:
        start, end = span
        if (exc.phase != "reactive" or end - start < 2
                or level >= _MAX_STREAM_DEGRADE_LEVELS):
            raise
        ctx.metrics.count("budget.degrade_retries")
        middle = (start + end) // 2
        leaves = await _realize_degrading(realize, (start, middle), ctx, level + 1)
        leaves.extend(await _realize_degrading(realize, (middle, end), ctx, level + 1))
        return leaves


# ── 算子本体 ───────────────────────────────────────────────────────────────

class GenerateStage:
    """M6 生成算子：process 模式的链外子批产出方，兼 generate_only 三形态入口。"""

    name = "generate"

    def __init__(self, cfg: "ResolvedConfig"):
        """构造算子。

        :param cfg: 已解析配置（算子无状态，只持有只读配置）。
        """
        self._cfg = cfg

    async def run(self, batch: list[PipelineItem], ctx: "RunContext") -> list[PipelineItem]:
        """process 模式入口：返回由新 PipelineItem 组成的子批（输入批丝毫不动）。

        M8 修复后仍非法、或重试穷尽的生成调用一律作废（桶计 ``calls``、
        ``produced`` 为 0）；不产 failed 记录；种子记录不受影响。v1.7：种子按类
        分组（classify 开启时），新记录继承种子类（``source="inherited"``，R17）。

        :param batch: 当前批（读质量闸幸存者作种子）。
        :param ctx: 运行上下文（rng / metrics / schema_engine）。
        :returns: 新 PipelineItem 子批；无可用种子时为空列表。
        """
        pools = select_seeds(batch, self._cfg)
        if not pools:
            return []
        segments = build_class_segments(pools, self._cfg)
        records = await self._generate(segments, ctx, limit=None)
        return [
            PipelineItem(record=rec) if cls is None else PipelineItem(
                record=rec,
                classification=Classification(label=cls, labels=(cls,),
                                              source="inherited", detail={}))
            for rec, cls in records
        ]

    async def generate_all(self, ctx: "RunContext") -> list[Record]:
        """generate_only 平面形态入口（M10 分批前调用一次；ctx.batch_no == 0，
        ctx.rng == Random(f"{seed}:0:generate")）。

        按 3.6.2 的调用数公式执行全部调用；``--limit`` 先按预抽序截断到前
        ceil(limit / num_per_call) 次调用，再把记录截断到 limit 条。v1.7：平面
        路径零改动——单匿名段、全局指令、不带类标签（spec 3.6.2）。

        :param ctx: 运行上下文。
        :returns: 全部新 Record（已按 --limit 截断）。
        """
        g = self._cfg.generate
        if g.seed_examples:
            seeds: list[tuple[str | None, str]] = [(None, s) for s in g.seed_examples]
            num_calls = math.ceil(len(seeds) * g.num_per_record / g.num_per_call)
        else:
            seeds = []
            num_calls = math.ceil((g.standalone_count or 0) / g.num_per_call)
        segment = ClassSegment(class_name=None, seeds=tuple(seeds),
                               num_calls=num_calls, styles=g.styles)
        records = await self._generate([segment], ctx, limit=self._cfg.limit)
        return [rec for rec, _ in records]

    async def _generate(self, segments: Sequence[ClassSegment], ctx: "RunContext",
                        limit: int | None) -> list[tuple[Record, str | None]]:
        """平面生成的公共主干：计划 → 装填 → 并发派发 → 后处理。

        :param segments: 已排序的类段序列（单匿名段即平面路径）。
        :param ctx: 运行上下文。
        :param limit: 记录条数上限；None = 不截断。
        :returns: (新记录, 类名) 对的列表。
        """
        g = self._cfg.generate
        num_calls = sum(seg.num_calls for seg in segments)
        exec_calls = num_calls
        if limit is not None:
            exec_calls = min(num_calls, math.ceil(limit / g.num_per_call))
        # 所有抽签都在派发之前按全局调用序完成（spec 3.6.2）。
        plans = build_segment_plans(g, segments, ctx.rng, exec_calls=exec_calls)
        schema = _samples_schema(g.num_per_call)
        fitted = self._fit_plans(plans, ctx)
        plans = [plan for plan, _ in fitted]
        results = await asyncio.gather(
            *(self._one_generate_call(p, u, schema, ctx) for p, u in fitted))
        seed_texts = [text for seg in segments for _, text in seg.seeds]
        records = postprocess_samples(plans, list(results), seed_texts,
                                      self._cfg, ctx.metrics)
        if limit is not None:
            records = records[:limit]
        return records

    def _fit_plans(self, plans: Sequence[CallPlan],
                   ctx: "RunContext") -> list[tuple[CallPlan, bool]]:
        """v1.11（§3.3⑦）派发前的逐调用种子装填。

        装填是确定性的（只依赖内容与预抽计划），故装填后的计划同时驱动派发与后
        处理——新记录继承的是实际送出的种子溯源。

        :param plans: 预抽的调用计划序列。
        :param ctx: 运行上下文（计 budget.truncations.generate）。
        :returns: 与输入对位的 (装填后计划, 是否不可装填) 列表。
        """
        fitted: list[tuple[CallPlan, bool]] = []
        for plan in plans:
            plan, truncated, unfittable = _fit_plan_seeds(plan, self._cfg)
            if truncated:
                ctx.metrics.count("budget.truncations.generate")
            fitted.append((plan, unfittable))
        return fitted

    async def _one_generate_call(self, plan: CallPlan, unfittable: bool,
                                 schema: dict, ctx: "RunContext") -> list[str] | None:
        """派发单次平面生成调用。

        :param plan: 装填后的调用计划。
        :param unfittable: True = 连 1 条种子都装不下，按 V10 就地作废不发请求。
        :param schema: 该轮共用的 samples Schema。
        :param ctx: 运行上下文。
        :returns: 样本文本列表；None = 该调用作废。
        """
        if unfittable:
            # V10：连 1 条种子都装不下——绝不发出注定失败的请求；该调用按既有失败
            # 语义作废（桶计 calls、produced 为 0、不产 failed 记录），stderr 一行
            # 里带精确 kind。phase=precheck 永不喂熔断。
            _log.warning(void_log_message(plan, ContextOverflowError(
                "generation call unfittable at 1 seed", phase="precheck",
                profile=plan.llm)),
                extra={"stage": self.name, "batch": ctx.batch_no})
            return None
        # R17：指令与温度取类有效值；num_per_call 恒取全局值。
        gen_c = effective_generate(self._cfg, plan.class_name)
        prompt = build_generate_prompt(gen_c.instruction, plan.style_prompt,
                                       self._cfg.generate.num_per_call,
                                       plan.seed_texts, gen_c.temperature)
        try:
            obj, _usage, _attempts, _model = await ctx.schema_engine.complete_validated(
                plan.llm, prompt, schema=schema,
                scope=CallScope(record_ids=plan.seed_ids, batch_no=ctx.batch_no))
            return list(obj["samples"])
        except CircuitBreakerTripped:
            raise
        except LabelKitError as exc:
            # 作废调用：只丢失本次调用的样本（记录级隔离）。spec 3.6.3：不产 failed
            # 记录、不写 StageError，因而也没有 error trace 事件（§8.1 把它绑定在
            # StageError 构造上）——作废经 report.generate.buckets（calls 计数、
            # produced 为 0）与 M8/M9 自己的 schema.repair / llm.call 事件可见，
            # stderr 只得到一行值-free 摘要。
            # v1.11：reactive-400 溢出终局（此处没有降级面）恰喂一次熔断（A7）；
            # precheck 与 finish 形终局永不喂。
            if (isinstance(exc, ContextOverflowError) and exc.phase == "reactive"
                    and getattr(exc, "origin", "http_400") == "http_400"
                    and not getattr(exc, "_breaker_fed", False)):
                exc._breaker_fed = True  # type: ignore[attr-defined]
                ctx.metrics.record_provider_result(fatal=True)
            _log.warning(void_log_message(plan, exc),
                         extra={"stage": self.name, "batch": ctx.batch_no})
            return None

    # ── v1.13 时间流形态（SPEC-stream-generation §3.2）──────────────────────

    async def generate_stream_all(self, ctx: "RunContext") -> StreamGenerateProduct:
        """GENERATE_ONLY 时间流形态入口（M10 分支调用一次；ctx.batch_no == 0，
        ctx.rng == Random(f"{seed}:0:generate")）。计划期抽签（①②③）→ 派发
        （零 rng：逐序列蓝图→实现作业与噪音批并发）→ 逐帧钩子与序列相似度过滤 →
        机械交织（④–⑨）→ 直装组装。作废序列只缺席，不产 failed 记录。"""
        plan = plan_stream(self._cfg, ctx.rng)
        for seq_plan in plan.sequences:
            ctx.metrics.count(
                f"generate.stream.sequences.{seq_plan.class_name}.planned")
        results = await asyncio.gather(
            *(self._stream_sequence_job(p, ctx) for p in plan.sequences),
            *(self._stream_noise_call(p, ctx) for p in plan.noise_plans))
        realized = [r for r in results[: len(plan.sequences)] if r is not None]
        noise: list[str] = []
        for samples in results[len(plan.sequences):]:
            noise.extend(samples or ())
        survivors = self._filter_stream_sequences(realized, ctx)
        sessions, stats = weave_stream(survivors, noise[: plan.noise_target],
                                       self._cfg, ctx.rng)
        lines, envelopes = assemble_stream(sessions, survivors, self._cfg)
        self._count_stream_product(survivors, stats, ctx)
        return StreamGenerateProduct(envelopes=envelopes, artifact_lines=lines)

    async def _stream_sequence_job(self, plan: SequencePlan,
                                   ctx: "RunContext") -> RealizedSequence | None:
        """一条序列的蓝图 → 帧实现 → 逐帧钩子作业；任一环节作废 ⇒ None（作废语义
        同平面路径 3.6.3：计数 + 值-free 日志，不产 failed 记录）。"""
        steps = await self._stream_plan_call(plan, ctx)
        if steps is None:
            return None
        payloads = await self._stream_realize_call(plan, steps, ctx)
        if payloads is None:
            return None
        bucket = bucket_key(plan.llm, plan.style_name, plan.class_name)
        ctx.metrics.count(f"generate.buckets.{bucket}.produced")
        if not self._stream_frames_valid(plan, payloads, ctx):
            return None
        return RealizedSequence(plan=plan,
                                frame_classes=tuple(fc for fc, _ in steps),
                                payloads=tuple(payloads))

    async def _stream_plan_call(self, plan: SequencePlan,
                                ctx: "RunContext") -> list[tuple[str, str]] | None:
        """蓝图调用（一序列一次；§10.14 模板 + plan_schema 内部待遇）。

        修复穷尽或不可装填 ⇒ 序列作废并计 plan_failures（不产 failed 记录）。

        :param plan: 该序列的计划期定稿。
        :param ctx: 运行上下文。
        :returns: 蓝图步序 [(frame_class, brief), ...]；None = 序列作废。
        """
        cfg = self._cfg
        gen_c = cfg.class_views[plan.class_name].generate
        classes = cfg.frame_classify.classes
        system_text, user_text = render_plan_prompt_texts(
            gen_c.instruction, classes, plan.class_name, plan.length)
        schema = _plan_schema([c.name for c in classes], plan.length)
        bucket = bucket_key(plan.llm, plan.style_name, plan.class_name)
        ctx.metrics.count(f"generate.buckets.{bucket}.calls")
        ctx.metrics.count("generate.stream.plan_calls")
        if cfg.generate.sample_validator:
            ctx.metrics.count(f"generate.buckets.{bucket}.rejected_by_validator", 0)
        if not self._stream_fits((system_text, user_text), plan.llm, schema):
            # V10 先例：最小单元不可装填——从不发出注定失败的请求；precheck 不喂熔断
            _log.warning(self._void_stream_sequence(plan, ContextOverflowError(
                "plan call unfittable under the input budget", phase="precheck",
                profile=plan.llm), "plan", ctx),
                extra={"stage": self.name, "batch": ctx.batch_no})
            return None
        prompt = _text_bundle(system_text, user_text, gen_c.temperature)
        try:
            obj, _usage, _attempts, _model = await ctx.schema_engine.complete_validated(
                plan.llm, prompt, schema=schema,
                scope=CallScope(batch_no=ctx.batch_no))
            return [(step["frame_class"], str(step["brief"]))
                    for step in obj["steps"]]
        except CircuitBreakerTripped:
            raise
        except LabelKitError as exc:
            _log.warning(self._void_stream_sequence(plan, exc, "plan", ctx),
                         extra={"stage": self.name, "batch": ctx.batch_no})
            return None

    def _realize_step_faces(self, steps: Sequence[tuple[str, str]]
                            ) -> tuple[list[dict], list[str]]:
        """把蓝图步序展开成逐位的 Schema 面与文本契约面（§10.15）。

        帧类声明了生成 Schema ⇒ 结构化帧（Schema 单行 dump 作契约行）；未声明 ⇒
        纯文本帧（``{"type": "string"}`` + 自由文本契约句）。

        :param steps: 蓝图步序 [(frame_class, brief), ...]。
        :returns: (逐位 Schema 列表, 逐位契约文本列表)，与 steps 对位。
        """
        views = self._cfg.frame_class_views
        schemas = [(dict(views[fc].gen_schema) if views[fc].gen_schema is not None
                    else {"type": "string"}) for fc, _ in steps]
        contracts = [(json.dumps(views[fc].gen_schema, ensure_ascii=False,
                                 separators=(", ", ": "))
                      if views[fc].gen_schema is not None else _REALIZE_FREE_TEXT)
                     for fc, _ in steps]
        return schemas, contracts

    async def _stream_realize_call(self, plan: SequencePlan,
                                   steps: Sequence[tuple[str, str]],
                                   ctx: "RunContext") -> list | None:
        """帧实现调用（一蓝图一次；§10.15 逐位契约 + realize_schema）。

        反应式溢出 ⇒ 序列对半分（schema 与蓝图概要同步减半，≤2 级，计
        budget.degrade_retries 既有通道）；穷尽或其余不可修复 ⇒ 序列作废并计
        realize_failures。

        :param plan: 该序列的计划期定稿。
        :param steps: 蓝图步序。
        :param ctx: 运行上下文。
        :returns: 逐帧内容列表；None = 序列作废。
        """
        gen_c = self._cfg.class_views[plan.class_name].generate
        schemas, contracts = self._realize_step_faces(steps)
        bucket = bucket_key(plan.llm, plan.style_name, plan.class_name)

        async def realize(span: tuple[int, int]) -> list:
            """派发一个跨度的帧实现调用。

            :param span: 蓝图步序上的半开区间 [start, end)。
            :returns: 该跨度的逐帧内容列表。
            :raises ContextOverflowError: 该跨度装不进输入预算（precheck 相位）。
            """
            start, end = span
            system_text, user_text = render_realize_prompt_texts(
                gen_c.instruction, plan.style_prompt, steps[start:end],
                contracts[start:end])
            schema = _realize_schema(schemas[start:end])
            ctx.metrics.count(f"generate.buckets.{bucket}.calls")
            ctx.metrics.count("generate.stream.realize_calls")
            if not self._stream_fits((system_text, user_text), plan.llm, schema):
                raise ContextOverflowError(
                    "realize call unfittable under the input budget",
                    phase="precheck", profile=plan.llm)
            obj, _usage, _attempts, _model = await ctx.schema_engine.complete_validated(
                plan.llm, _text_bundle(system_text, user_text, gen_c.temperature),
                schema=schema, scope=CallScope(batch_no=ctx.batch_no))
            return list(obj["frames"])

        try:
            leaves = await _realize_degrading(realize, (0, len(steps)), ctx)
            return [frame for leaf in leaves for frame in leaf]
        except CircuitBreakerTripped:
            raise
        except LabelKitError as exc:
            _log.warning(self._void_stream_sequence(plan, exc, "realize", ctx),
                         extra={"stage": self.name, "batch": ctx.batch_no})
            return None

    async def _stream_noise_call(self, plan: NoiseCallPlan,
                                 ctx: "RunContext") -> list[str] | None:
        """噪音批量实现：复用平面生成模板与 samples_schema（裁决·噪音只做插入与
        重复）；作废 ⇒ None，缺额帧从交织缺席（不补生成）。"""
        g = self._cfg.generate
        instruction = self._cfg.generate_stream.noise_instruction
        system_text, user_text = render_prompt_texts(instruction, plan.style_prompt,
                                                     g.num_per_call, ())
        schema = _samples_schema(g.num_per_call)
        bucket = bucket_key(plan.llm, plan.style_name)
        ctx.metrics.count(f"generate.buckets.{bucket}.calls")
        ctx.metrics.count("generate.stream.noise_calls")
        if not self._stream_fits((system_text, user_text), plan.llm, schema):
            _log.warning("noise call voided: unfittable under the input budget "
                         "call=%d llm=%s", plan.index, plan.llm,
                         extra={"stage": self.name, "batch": ctx.batch_no})
            return None
        prompt = build_generate_prompt(instruction, plan.style_prompt,
                                       g.num_per_call, (), g.temperature)
        try:
            obj, _usage, _attempts, _model = await ctx.schema_engine.complete_validated(
                plan.llm, prompt, schema=schema,
                scope=CallScope(batch_no=ctx.batch_no))
            samples = [str(sample) for sample in obj["samples"]]
            ctx.metrics.count(f"generate.buckets.{bucket}.produced", len(samples))
            return samples
        except CircuitBreakerTripped:
            raise
        except LabelKitError as exc:
            budget.feed_reactive_terminal(exc, ctx.metrics)
            _log.warning("noise call voided: call=%d llm=%s kind=%s", plan.index,
                         plan.llm, _error_kind(exc),
                         extra={"stage": self.name, "batch": ctx.batch_no})
            return None

    def _stream_fits(self, texts: tuple[str, str], llm: str, schema: dict) -> bool:
        """``_fit_plan_seeds`` 先例的时间流预检：est(system) + est(user) + 2×消息
        包封 ≤ 输入预算；supports_structured_output 时 response_schema 文本另计
        （L0 上 schema 随请求上行；提示词内嵌 schema 文本已在 est(system) 内恒计）。
        预算未声明（profile 缺失 / cw == 0）恒可装填。"""
        prof = self._cfg.llm_profiles.get(llm)
        if prof is None or prof.context_window <= 0:
            return True
        available = budget.input_budget(prof)
        if prof.supports_structured_output:
            available -= budget.est_text(json.dumps(schema, ensure_ascii=False))
        system_text, user_text = texts
        est = (budget.est_text(system_text) + budget.est_text(user_text)
               + 2 * budget.MSG_OVERHEAD_TOKENS)
        return est <= available

    def _void_stream_sequence(self, plan: SequencePlan, exc: LabelKitError,
                              call_kind: str, ctx: "RunContext") -> str:
        """作废一条序列（蓝图/实现失败语义，与平面路径作废同款）。

        计 generate.stream.<call_kind>_failures、按 A7 恰一次喂熔断（仅
        reactive-400 终局；precheck 与 200 形终局永不喂）；不产 failed 记录、不写
        StageError。摘要文本由调用点就地记录，使每个异常分支自带错误日志。

        :param plan: 被作废序列的计划期定稿。
        :param exc: 触发作废的异常。
        :param call_kind: 作废发生的调用类别（``plan`` / ``realize``）。
        :param ctx: 运行上下文。
        :returns: 值-free 的单行 stderr 摘要。
        """
        ctx.metrics.count(f"generate.stream.{call_kind}_failures")
        budget.feed_reactive_terminal(exc, ctx.metrics)
        message = (f"stream sequence voided: seq={plan.index} "
                   f"class={plan.class_name} llm={plan.llm} call={call_kind} "
                   f"kind={_error_kind(exc)}")
        if isinstance(exc, SchemaViolation):
            message += f" violations={len(exc.errors)}"
        return message

    def _stream_frames_valid(self, plan: SequencePlan, payloads: Sequence,
                             ctx: "RunContext") -> bool:
        """``sample_validator`` 逐帧执行（裁决·生成键效力矩阵）：任一帧违规 ⇒ 整
        序列作废（蓝图定长不可剔单帧，拒绝采样语义）计 validator_scrapped 与桶
        rejected_by_validator；回调抛异常视同违规（平面路径同款兜底）。未配置钩子
        恒 True。"""
        hook_ref = self._cfg.generate.sample_validator
        if not hook_ref:
            return True
        from labelkit.common.extensions.hooks import normalize_violations, resolve_hook

        hook = resolve_hook(hook_ref)
        for position, payload in enumerate(payloads):
            try:
                violations = normalize_violations(hook(_payload_text(payload)),
                                                  hook_ref)
            except Exception as exc:  # 钩子缺陷：按违规作废本序列，绝不逸出批级
                _log.warning(
                    "generate.sample_validator raised on a stream frame; the "
                    "sequence is scrapped: %s: %s", type(exc).__name__, exc,
                    extra={"stage": self.name, "batch": ctx.batch_no})
                violations = ["callback raised"]
            if violations:
                bucket = bucket_key(plan.llm, plan.style_name, plan.class_name)
                ctx.metrics.count(f"generate.buckets.{bucket}.rejected_by_validator")
                ctx.metrics.count("generate.stream.validator_scrapped")
                _log.warning(
                    "stream sequence scrapped by sample_validator: seq=%d "
                    "class=%s frame=%d violations=%d", plan.index,
                    plan.class_name, position, len(violations),
                    extra={"stage": self.name, "batch": ctx.batch_no})
                return False
        return True

    def _filter_stream_sequences(self, realized: list[RealizedSequence],
                                 ctx: "RunContext") -> list[RealizedSequence]:
        """序列级相似度过滤（裁决·序列相似度过滤）：判重文本 = 成员 text 按序
        "\\x1e" 拼接（M3 序列配方同式）、比对面 = 兄弟序列（无种子）、参数取
        [dedup] 三键；淘汰以 survived_dedup 桶差呈现。幸存序列保持计划序。"""
        d = self._cfg.dedup
        filt = SimilarityFilter(threshold=d.minhash_threshold,
                                num_perm=d.minhash_num_perm, ngram=d.ngram)
        survivors: list[RealizedSequence] = []
        for seq in realized:
            probe = "\x1e".join(_payload_text(p) for p in seq.payloads)
            bucket = bucket_key(seq.plan.llm, seq.plan.style_name,
                                seq.plan.class_name)
            if not filt.probe_and_add(probe):
                _log.info("stream sequence eliminated by the similarity filter: "
                          "seq=%d class=%s", seq.plan.index, seq.plan.class_name,
                          extra={"stage": self.name, "batch": ctx.batch_no})
                continue
            ctx.metrics.count(f"generate.buckets.{bucket}.survived_dedup")
            survivors.append(seq)
        return survivors

    def _count_stream_product(self, survivors: Sequence[RealizedSequence],
                              stats: "Mapping", ctx: "RunContext") -> None:
        """report.generate.stream 供数（counts-only；键集 = 裁决·观测面；planned
        已在计划期计数，本处补交织统计与按类 produced）。"""
        for key in ("sessions", "crossed_sessions", "frames", "noise_frames",
                    "duplicates"):
            if stats[key]:
                ctx.metrics.count(f"generate.stream.{key}", stats[key])
        for seq in survivors:
            ctx.metrics.count(
                f"generate.stream.sequences.{seq.plan.class_name}.produced")
