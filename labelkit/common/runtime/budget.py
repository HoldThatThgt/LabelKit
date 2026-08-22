"""v1.11 上下文预算原语（spec 3.9.5，CONTRACTS.md §7.17）。

本模块提供：余量/预算算术、零依赖的文本与图像 token 估算器、确定性文本裁剪、
静态最小窗口保证（w_min）、V27① 阶段错误归类助手，以及 ``ImageCostCalibrator``
（V19 在线单图成本校准）。全部是纯函数 + 一个纯内存类；零第三方依赖；零持久化。

分层约束：llm_client 在运行期导入本模块，因此本模块运行期绝不可反向导入
llm_client（或任何算子）——profile / bundle / config 类型一律以鸭子类型值传入
（下方 TYPE_CHECKING 内的导入仅供类型检查）。v1.12（装箱器下沉裁决）：
数据自适应贪心装箱器 ``pack_windows`` 自 segment.py 原样下沉为本模块公开面——
M14 窗口切分与 M13 帧级批量判决共用（帧级为零重叠调用形）；除此之外本模块仍只
提供估算/预算原语 + 校准器。
"""
from __future__ import annotations

import json
import math
from collections import deque
from typing import TYPE_CHECKING, Literal, Mapping

from labelkit.common.errors import ContextOverflowError, OutputTruncatedError

if TYPE_CHECKING:
    from labelkit.common.config.model import (
        EmbeddingProfile,
        LLMProfile,
        ResolvedConfig,
    )
    from labelkit.common.runtime.llm_client import PromptBundle

# ── 冻结常量（V7/V8/V22——改动其中任一取值都属于 spec 修订） ─────────────────

MARGIN_FLOOR = 256            # token
MARGIN_RATIO = 0.10           # [C-15] 量级锚定
ASCII_PER_TOKEN = 3.0         # /4 的 JSON 保守化 [C-24][C-26]
CJK_TOKEN_PER_CHAR = 1.0      # 覆盖 GLM/o200k/Qwen [C-25][C-73]；cl100k 局限见 spec
OTHER_PER_TOKEN = 2.0
MSG_OVERHEAD_TOKENS = 4       # [C-7][C-76] 3+1 保守化
DIFF_MAX_TOKENS = 128         # segment 窗内单帧 diff 行最坏常数（输出结构有界，V9）
CALIBRATION_SAFETY = 0.85     # V19 装填折扣 [C-32][C-37][C-33]
CALIBRATION_MIN_SAMPLES = 8   # 样本不足不升档 [C-32]
CALIBRATION_WINDOW_BATCHES = 8  # 批最大值窗口深度（F8：窗口单位=批，序无关）
PRIOR_INFLATION = 1.2         # 首批先验保守放大（V17）

# V22（跨层依赖豁免）：common 不得导入算子，因此各阶段冻结提示词模板头以
# 「冻结整型常量」的形式落在这里，供 M1 静态预检（V13③）与 V9 保证使用。
# 每个取值 = est_text(该阶段算子模板中最大的那条冻结 system/模板头常量)
# （CONTRACTS §10 冻结文本）；tests/common/runtime/test_budget.py 以跨层等式
# 断言 est_text(算子常量) == 本字典取值——修订 §10 模板会让该测试变红，常量
# 随 CONTRACTS 修订同步。
# 例外——"segment" 覆盖的是提示词的「完整最坏情况静态脚手架」而非仅模板头：
# est_text("\n".join(_SYSTEM_HEAD, _STRUCTURE_SENTENCE, _STRUCTURE_REASON))
# ——with_reason 结构变体即最坏情况。min_window 的静态项锚定 V9 运行期装填
# 保证，故它对任意配置都必须 ≥ segment._static_prompt_est；只取模板头会漏算
# 结构句，让装箱器看到的单窗预算小于保证承诺的量。
TEMPLATE_HEAD_TOKENS: dict[str, int] = {
    "segment": 484,   # §10.9 完整静态脚手架（模板头 + 结构句，
                      # with_reason 变体——见上方例外说明）
    "classify": 48,   # classify._SYSTEM_HEAD_MULTI（§10.8）
    "quality": 39,    # §10.2 成对判决/结构句（内联字面量）
    "annotate": 32,   # annotate._SCHEMA_SENTENCE（§10.1）
    "verify": 192,    # verify._SEQ_SYSTEM_DEFECT_TYPES（§10.5 stream 变体）
    "generate": 29,   # §10.4 结构句（内联字面量）
    "stitch": 325,    # stitch._SYSTEM_HEAD（§10.11）
    "extract": 286,   # extract._SYSTEM_HEAD（§10.10）
    # v1.12 帧级两键：值 = est_text(算子帧模板头常数)，由跨层等式测试钉住
    # （test_budget 与 classify/annotate 的冻结常量逐字对齐）；同时供 M1
    # 静态预算预检（V13③ 两新段）使用。
    "frame_classify": 81,   # classify._FRAME_SYSTEM_HEAD (§10.12)
    "frame_annotate": 35,   # annotate._FRAME_SYSTEM_STATIC (§10.13)
}

# CJK 判定（V8）：Unicode 汉字基本区 + 各扩展区 + 全角标点——区间逐个枚举
# （闭区间），测试钉住确切样本。假名等其它文字刻意归入 OTHER 桶。
_CJK_RANGES: tuple[tuple[int, int], ...] = (
    (0x3000, 0x303F),    # 中日韩符号与标点（、。「」等全角标点）
    (0x3400, 0x4DBF),    # 汉字扩展 A 区
    (0x4E00, 0x9FFF),    # 汉字基本区
    (0xFF00, 0xFF60),    # 全角 ASCII 变体（！（）：？等全角标点）
    (0xFFE0, 0xFFE6),    # 全角符号（￠￡￥￦等）
    (0x20000, 0x2A6DF),  # 汉字扩展 B 区
    (0x2A700, 0x2B73F),  # 汉字扩展 C 区
    (0x2B740, 0x2B81F),  # 汉字扩展 D 区
    (0x2B820, 0x2CEAF),  # 汉字扩展 E 区
    (0x2CEB0, 0x2EBEF),  # 汉字扩展 F 区
    (0x2EBF0, 0x2EE5F),  # 汉字扩展 I 区
    (0x30000, 0x3134F),  # 汉字扩展 G 区
    (0x31350, 0x323AF),  # 汉字扩展 H 区
)

# anthropic 的 28px patch 计费上限（standard 档，[C-11][C-47]）；openai 分块常量
# （[C-9][C-60]）：先装入 2048 见方 → 短边归一到 768 → 按 512px 分块 →
# 基础 85 + 每块 170。
_ANTHROPIC_PATCH_PX = 28
_ANTHROPIC_TOKEN_CAP = 1568
_OPENAI_FIT_SQUARE_PX = 2048
_OPENAI_SHORT_SIDE_PX = 768
_OPENAI_TILE_PX = 512
_OPENAI_BASE_TOKENS = 85
_OPENAI_TILE_TOKENS = 170

# 截断标记族（classify.py 成员行语义，V9）：整行丢弃中段，标记原地补位。
_FIT_MARKER = "…(truncated {n} lines)"


# ── 预算算术（V7/V15） ───────────────────────────────────────────────────────

def margin(context_window: int) -> int:
    """计算预算余量 max(256, ceil(0.10 × context_window))。

    余量吸收估算器残差、消息信封开销与提供方计数漂移（V7）。

    @param context_window 该 profile 声明的端点有效上下文窗口（token）。
    @return 余量 token 数。
    """
    return max(MARGIN_FLOOR, math.ceil(MARGIN_RATIO * context_window))


def input_budget(profile: "LLMProfile") -> int:
    """计算一次对话补全调用的输入预算：context_window − max_output_tokens − margin。

    @param profile LLM profile（鸭子类型，读取 context_window / max_output_tokens）。
    @return 输入预算 token 数；context_window == 0（未声明）⇒ 0，即预算功能关闭。
    """
    cw = profile.context_window
    if cw <= 0:
        return 0
    return cw - profile.max_output_tokens - margin(cw)


def embed_budget(profile: "EmbeddingProfile") -> int:
    """计算一次向量化调用的输入预算：context_window − margin（不预留输出，V15）。

    @param profile embedding profile（鸭子类型，读取 context_window）。
    @return 输入预算 token 数；context_window == 0（未声明）⇒ 0，即预算功能关闭。
    """
    cw = profile.context_window
    if cw <= 0:
        return 0
    return cw - margin(cw)


# ── 零依赖文本估算器（V8） ───────────────────────────────────────────────────

def _is_cjk(cp: int) -> bool:
    """判定码位是否落在 CJK 计费桶（_CJK_RANGES 逐区间闭区间比对）。

    @param cp Unicode 码位。
    @return 落在任一 CJK 区间为 True，否则 False。
    """
    for lo, hi in _CJK_RANGES:
        if lo <= cp <= hi:
            return True
    return False


def est_text(s: str) -> int:
    """按字符类估算文本 token 数：ceil(ascii/3 + cjk×1.0 + other/2)。

    该估算在前缀上单调（⇒ fit_text 可在行边界上二分）。已知局限：cl100k 族的
    中文（1.25–1.4 token/字）不在覆盖范围内——已记入 spec，由余量吸收（V8）。

    @param s 待估算文本。
    @return 估算 token 数（向上取整）。
    """
    ascii_n = cjk_n = other_n = 0
    for ch in s:
        cp = ord(ch)
        if cp < 128:
            ascii_n += 1
        elif _is_cjk(cp):
            cjk_n += 1
        else:
            other_n += 1
    return math.ceil(ascii_n / ASCII_PER_TOKEN
                     + cjk_n * CJK_TOKEN_PER_CHAR
                     + other_n / OTHER_PER_TOKEN)


# ── 图像成本先验（V8 v3 / V17：仅作首批种子，正确性由校准器 +
#    V20 溢出反应负责） ────────────────────────────────────────────────────

def _image_prior(provider: str, px: int) -> int:
    """按提供方文档公式估算单图成本，取长边为 px 时的「最坏长宽比」。

    @param provider 提供方标识（"anthropic" | "openai_compatible"）。
    @param px 图像长边像素（工作点）。
    @return 单图先验 token 数。
    """
    if provider == "anthropic":
        # 28px patch 计费，最坏取正方形，封顶 standard 档上限（[C-47][C-69]）。
        return min(math.ceil(px / _ANTHROPIC_PATCH_PX) ** 2, _ANTHROPIC_TOKEN_CAP)
    # openai_compatible 分块计费：先装入 2048 见方，再把短边归一到 768，然后按
    # 512px 分块计数。最坏长宽比即最大化 ceil(short/512) × ceil(long/512)——短边
    # 封顶 768（[C-60]：@2048 时最坏竖图为 85 + 8×170 = 1445；正方形 765 是特例）。
    long_edge = min(px, _OPENAI_FIT_SQUARE_PX)
    tiles = (math.ceil(min(_OPENAI_SHORT_SIDE_PX, long_edge) / _OPENAI_TILE_PX)
             * math.ceil(long_edge / _OPENAI_TILE_PX))
    return _OPENAI_BASE_TOKENS + _OPENAI_TILE_TOKENS * tiles


def est_image_prior(profile: "LLMProfile", px: int) -> int:
    """按 profile 的提供方公式给出生效像素下的单图先验成本。

    校准器的种子值 = 本函数返回值 × PRIOR_INFLATION（V17/V19）。

    @param profile LLM profile（鸭子类型，读取 provider）。
    @param px 图像长边像素（工作点）。
    @return 单图先验 token 数。
    """
    return _image_prior(profile.provider, px)


# ── 整条提示词估算（V8/V16） ────────────────────────────────────────────────

def est_prompt(bundle: "PromptBundle", profile: "LLMProfile",
               schema: dict | None, image_cost: int) -> int:
    """估算整条提示词的输入 token：Σ est_text(文本片段) + 图片数 × image_cost
    + MSG_OVERHEAD × 消息条数 + est_text(Schema JSON)。

    Schema 仅在结构化输出生效时随请求上行，否则调用方传 None。``image_cost``
    由「调用方」从校准器读出（M9 终检与各装填层共用同一份按批冻结的读数）。
    ``profile`` 属于冻结签名（§7.17）——为提供方相关的信封项预留，当前不参与计算。

    @param bundle 待估算的提示词包（鸭子类型，读取 messages/parts）。
    @param profile LLM profile（冻结签名占位，当前实现不读取）。
    @param schema 随请求上行的 Schema；不上行时传 None。
    @param image_cost 单图 token 成本（调用方从校准器取得的按批冻结值）。
    @return 估算的输入 token 总数。
    """
    del profile
    est = 0
    n_images = 0
    for message in bundle.messages:
        for part in message.parts:
            if part.kind == "text":
                est += est_text(part.text or "")
            else:
                n_images += 1
    est += n_images * image_cost
    est += MSG_OVERHEAD_TOKENS * len(bundle.messages)
    if schema is not None:
        est += est_text(json.dumps(schema, ensure_ascii=False))
    return est


# ── 确定性文本裁剪（V9/V15） ────────────────────────────────────────────────

def fit_text(s: str, budget_tokens: int,
             keep: Literal["head", "edges"]) -> str:
    """按行边界裁剪文本到预算内，确定且幂等（裁剪结果再裁剪得自身）。

    ``head`` 保留能装下的最长行前缀（向量化输入截断，V15——嵌入输入的语义重心
    在开头）。``edges`` 保留首行与末行、丢弃中段整行，并用原地标记
    "…(truncated N lines)" 补位——即 classify.py 成员行族语义（V9）；退化下界是
    只剩标记一行代表全部内容。

    @param s 待裁剪文本。
    @param budget_tokens 允许占用的 token 预算。
    @param keep 裁剪形态："head" 保留前缀，"edges" 保留首尾。
    @return 裁剪后的文本；原文本已在预算内时原样返回。
    """
    if est_text(s) <= budget_tokens:
        return s
    lines = s.split("\n")
    n = len(lines)
    if keep == "head":
        # est_text 对前缀单调 → 二分求能装下的最大 k。
        lo, hi = 0, n - 1                    # 已知 k == n 装不下
        while lo < hi:
            mid = (lo + hi + 1) // 2
            if est_text("\n".join(lines[:mid])) <= budget_tokens:
                lo = mid
            else:
                hi = mid - 1
        return "\n".join(lines[:lo])
    # edges：首行 + 最长中段前缀 + 标记 + 末行（classify.py:92-108 方案）；
    # 至少要丢掉一行中段。
    for keep_middle in range(n - 3, -1, -1):
        marker = _FIT_MARKER.format(n=n - 2 - keep_middle)
        candidate = "\n".join(lines[:1 + keep_middle] + [marker, lines[-1]])
        if est_text(candidate) <= budget_tokens:
            return candidate
    return _FIT_MARKER.format(n=n)


# ── greedy budget packer（v1.12 下沉自 segment._pack_windows；V9） ───────────

def pack_windows(costs: list[int], budget: int, cap: int) -> list[tuple[int, int]]:
    """数据自适应贪心装箱器（v1.11 V9，spec 3.14.4 装填伪代码）。

    来历（v1.12 装箱器下沉裁决）：本函数原为 M14 私有 ``segment._pack_windows``，
    v1.12 原样搬入本模块改为公开面——算法与行为字节等价（既有装箱测试原样守住）；
    M14 窗口切分与 M13 帧级批量判决共用。``budget`` 已由调用方扣除静态项，因此
    这里的装填条件 Σ c_j ≤ budget 就是 spec 的 est_static_system + Σ c_i ≤
    input_budget。

    窗口取半开区间 [start, end)：首窗自 0 起，其后每窗自前窗 end − 1 起——1 帧重叠
    与「接缝归后窗所有」的约定保持不变（M14 的 rel[] 覆写序依赖之）；预算与帧数上
    限同时满足时帧并入当前窗，超出即封窗。每窗至少 2 帧——V10 语义下界：M1 的
    w_min ≥ floor 保证在「先验」图像定价下任意两个最坏帧都装得下（spec 3.1.4），
    但装箱器按校准器定价，而校准器在样本数越过 CALIBRATION_MIN_SAMPLES 之后合法地
    可能超出 先验 × PRIOR_INFLATION（刻意不设夹紧）。因此预算本会让窗口不足 2 帧时，
    无视成本强制装到 2 帧：若其真实估算确实超预算，交由 M9 发送前终检按记录级处理
    ——绝不升级为运行级失败，且强制推进保证循环收敛。本函数是 (costs, budget, cap)
    的纯函数 ⇒ 重跑结果确定。

    零重叠调用形（v1.12，M13 帧级批量判决专用）：帧分类窗口是不重叠切分——重叠
    语义不适用时，调用方对返回跨度自后窗起丢弃与前窗重叠的首帧（[start+1, end)）
    即得不重叠划分；本函数自身的跨度链约定（start = end − 1）保持冻结不变。

    @param costs 逐帧成本 c_i 列表（下标即帧序）。
    @param budget 单窗可用预算 = input_budget − est_static_system。
    @param cap 单窗帧数上限。
    @return 窗口跨度列表 [(start, end), ...]，半开区间且相邻窗重叠 1 帧。
    """
    spans: list[tuple[int, int]] = []
    n = len(costs)
    start = 0
    while start < n:
        end = start
        total = 0
        while end < n and end - start < cap and total + costs[end] <= budget:
            total += costs[end]
            end += 1
        if end - start < 2:
            end = min(start + 2, n)                # 强制补足语义下界 2 帧
        spans.append((start, end))
        if end == n:
            break
        start = end - 1
    return spans


# ── 静态最小窗口保证（V9/V12） ──────────────────────────────────────────────

def min_window(cfg: "ResolvedConfig") -> int:
    """计算最坏情况下仍能保证装填的窗口大小 w_min，供 M1 的 V9 保证与 V12 估算上界共用。

    预算未声明（segment profile 缺失或 context_window == 0）⇒ 原样返回
    cfg.segment.window。已声明 ⇒ ⌊(input_budget − est_static_system) /
    per_frame_max⌋（不小于 0），且全部按「先验」定价：

    - per_frame_max = est_text(digest_max_chars 长的最坏全中文摘要) +
      DIFF_MAX_TOKENS +（仅 vision_resolved 时）工作像素下的 图像先验 ×
      PRIOR_INFLATION；
    - est_static_system = V22 冻结的 segment 脚手架常量（系统头 + 最坏结构句，见
      TEMPLATE_HEAD_TOKENS 的例外说明）+ segment.context（额外 +1 计其拼接换行，
      使分别取整之和 ≥ 运行期拼接后的估算）+ 两条消息信封——三者之和对任意配置都
      ≥ segment._static_prompt_est，这正是 V9 保证赖以成立的对齐关系。

    保证本身是「先验」口径：校准后的图像成本高于 先验 × PRIOR_INFLATION 是合法的
    （刻意不夹紧），此时经装箱器的强制 2 帧窗 + M9 终检按记录级降级——绝不升级为
    运行级失败（spec 3.1.4 的诚实表述）。

    注意：返回值不按 window 封顶——w_min 可能超过该上限（保证方需要预算推导出的原
    值；估算侧消费者按 V12/V26 自行夹紧）。鸭子类型：只读 cfg.segment 与
    cfg.llm_profiles（M1 在 ResolvedConfig 组装之前即调用本函数）。

    @param cfg 已解析（或组装中）的配置对象，需提供 segment 与 llm_profiles。
    @return 最坏情况保证窗口大小 w_min（帧数）。
    """
    seg = cfg.segment
    prof = cfg.llm_profiles.get(seg.llm)
    if prof is None or prof.context_window <= 0:
        return seg.window
    est_static = (TEMPLATE_HEAD_TOKENS["segment"]
                  + (est_text(seg.context) + 1 if seg.context else 0)
                  + 2 * MSG_OVERHEAD_TOKENS)
    per_frame = est_text("\u597d" * seg.digest_max_chars) + DIFF_MAX_TOKENS
    if seg.vision_resolved:
        px = prof.default_image_px or prof.max_image_px
        per_frame += math.ceil(est_image_prior(prof, px) * PRIOR_INFLATION)
    return max(0, (input_budget(prof) - est_static) // per_frame)


# ── V27① 共享的阶段错误归类器 ───────────────────────────────────────────────

def classify_stage_error(exc: BaseException) -> str | None:
    """把预算相关异常归类为 §7.6 的错误 kind 词汇。

    ContextOverflowError → "context_overflow"；OutputTruncatedError →
    "output_truncated"；其余 → None。各算子在自己的记录级错误归类器里「首先」调用
    本函数——词汇不准会落进 internal_error，破坏 §3.5 归因与 overflow_records 计数。

    @param exc 捕获到的异常对象。
    @return 对应的错误 kind 字符串；不属于本词汇表时返回 None。
    """
    if isinstance(exc, ContextOverflowError):
        return "context_overflow"
    if isinstance(exc, OutputTruncatedError):
        return "output_truncated"
    return None


def feed_reactive_terminal(exc: BaseException, metrics) -> None:
    """A7/§7.8 熔断矩阵的共享「恰好一次」喂入点。

    只有 reactive-400（响应体嗅探）判出的溢出终局才喂致命连击计数，且对同一个异常
    对象至多喂一次（``_breaker_fed`` 鸭子标志防止异常跨层时重复喂入，例如 M7→M5 的
    修复链或 M8 的 L3 短路）；预检与 200 形态的 finish 判据一律不喂。本函数落在
    common 层，是因为 M8 schema_engine 也要喂而 common 不得导入算子；``metrics``
    为鸭子类型（提供 MetricsSink.record_provider_result），并容忍 None 以支持无
    metrics 的引擎/validate 路径。

    @param exc 待判定的异常对象。
    @param metrics 指标汇（鸭子类型）；None 时静默跳过。
    @return 无。
    """
    if (metrics is not None
            and isinstance(exc, ContextOverflowError) and exc.phase == "reactive"
            and getattr(exc, "origin", "http_400") == "http_400"
            and not getattr(exc, "_breaker_fed", False)):
        exc._breaker_fed = True  # type: ignore[attr-defined]
        metrics.record_provider_result(fatal=True)


# ── V19 在线单图成本校准 ────────────────────────────────────────────────────

class ImageCostCalibrator:
    """按 profile 维护的单图成本在线校准器（仅运行期内存，零持久化——跨运行冷启动
    是无状态约束的代价）。由 LLMClient 自持（V23②），公开面为 ``llm.calibrator``。

    确定性护栏（F8）：可读快照「按批冻结」——第 N 批的装填只读得到 < N 批的聚合值。
    样本按 asyncio 完成序抵达，故 ``freeze_batch()`` 在「无序样本集合」上取批最大值
    （与顺序无关），折入 deque(maxlen=8) 的批最大值窗口；第 N 批进行中的逐响应
    ``observe()`` 绝不影响该批自身的 ``cost()`` 读数（累计样本数一并冻结——否则批中
    途的第 8 个样本会让 cost() 中途改口）。
    """

    def __init__(self, profiles: Mapping[str, tuple[str, int]]):
        """构造校准器。

        @param profiles profile 名 → (提供方, 工作像素) 映射，即先验公式所需的入参，
               由 LLMClient 从其 profile 表推导（工作像素 = default_image_px 或
               max_image_px，V18）。
        """
        self._profiles: dict[str, tuple[str, int]] = dict(profiles)
        self._current: dict[str, list[int]] = {}      # 当前批开放中的样本桶
        self._windows: dict[str, deque[int]] = {}     # 已冻结的批最大值窗口
        self._frozen_total: dict[str, int] = {}       # 已冻结的累计样本数
        self._snapshot: dict[str, int] = {}           # 已冻结的 cost() 读数

    def observe(self, profile: str, prompt_tokens: int,
                text_est: int, n_images: int) -> None:
        """对一条带图响应采样一次：(prompt_tokens − text_est) / n_images 落入当前批桶。

        不带图的调用从不采样；退化的非正残差夹到 ≥ 1（取最大值的过滤器绝不能被
        零/负数假象污染）。

        @param profile 本次调用所用 profile 名。
        @param prompt_tokens 提供方回报的输入 token 数。
        @param text_est 本次提示词的纯文本估算 token 数。
        @param n_images 本次提示词携带的图片数。
        @return 无。
        """
        if n_images < 1:
            return
        sample = max(1, math.ceil((prompt_tokens - text_est) / n_images))
        self._current.setdefault(profile, []).append(sample)

    def freeze_batch(self) -> None:
        """在 M10 批边界冻结本批聚合：把各 profile 当前批桶的最大值折入其
        deque(maxlen=CALIBRATION_WINDOW_BATCHES)，并刷新可读快照（第 N+1 批只读得到
        ≤ N 批的聚合值）。

        @return 无。
        """
        for profile, samples in self._current.items():
            # 桶只由 observe() 建立且建立即写入, 故此处恒非空。
            window = self._windows.setdefault(
                profile, deque(maxlen=CALIBRATION_WINDOW_BATCHES))
            window.append(max(samples))               # 与顺序无关的聚合
            self._frozen_total[profile] = (self._frozen_total.get(profile, 0)
                                           + len(samples))
            self._snapshot[profile] = math.ceil(max(window)
                                                / CALIBRATION_SAFETY)
        self._current.clear()

    def cost(self, profile: str) -> int:
        """给出装填侧读数——只读冻结快照。

        累计（冻结）样本数不足 CALIBRATION_MIN_SAMPLES ⇒ 先验 × PRIOR_INFLATION；
        否则 max(批最大值窗口) ÷ CALIBRATION_SAFETY 并向上取整（该值在冻结时已预算好）。

        @param profile 待读取的 profile 名。
        @return 单图成本 token 数。
        """
        if self._frozen_total.get(profile, 0) < CALIBRATION_MIN_SAMPLES:
            provider, px = self._profiles[profile]
            return math.ceil(_image_prior(provider, px) * PRIOR_INFLATION)
        return self._snapshot[profile]
