"""M9 —— LLM 客户端（spec 3.9，CONTRACTS.md §7.8）。

统一的异步多 provider 客户端：消息装配（纯文本 / 多模态）、provider 适配
（openai_compatible / anthropic 原生）、结构化输出参数透传、超时与重试与限流、
token 及成本计量，以及 ``validate --probe`` 连通性探测。

边界（spec 3.9.1）：不做业务解析（只回原始文本或原生结构化载荷——解析属于 M8）、
不做响应缓存、不做模型路由。

请求体装配与响应解析一律是模块级纯函数，便于零网络的离线单测覆盖。
"""
from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import random
import statistics
import time
from collections import deque
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import TYPE_CHECKING, Any, Callable, Literal, Mapping

import httpx

from labelkit.common.config.model import EmbeddingProfile, LLMProfile
from labelkit.common.contracts.types import ImageRef, Usage
from labelkit.common.errors import (
    CircuitBreakerTripped,
    ContextOverflowError,
    OutputTruncatedError,
    ProviderFatalError,
    ProviderRetryableError,
)
from labelkit.common.runtime import budget
from labelkit.common.runtime.budget import ImageCostCalibrator

if TYPE_CHECKING:
    from labelkit.common.observability.obslog import MetricsSink

from labelkit.common.observability.obslog import (
    EV_LLM_CALL,
    EV_LLM_KEY_COOLDOWN,
    EV_LLM_KEY_DISABLED,
    EV_LLM_POOL_PARKED,
)

_logger = logging.getLogger("labelkit.llm")

ANTHROPIC_VERSION = "2023-06-01"          # [FROZEN in CONTRACTS.md §7.8]
STRUCTURED_TOOL_NAME = "emit"             # [FROZEN in CONTRACTS.md §7.8]
_MAX_BACKOFF_S = 60.0                     # 退避封顶（spec 3.9.3）——v1.6 起只覆盖非 429 可重试错误
_MAX_KEY_COOLDOWN_S = 300.0               # 无 Retry-After 时的每键 429 冷却封顶（spec 3.9.3）
_PARK_SLICE_S = 60.0                      # 驻留分片时长；每片复检熔断（v1.6）

# v1.11（V11/V24，[C-57][C-58]）：每个 200 都按终止原因终局化——
# 输出上限命中，以及 200 形态的上下文溢出神谕（双协议同名）。
_TRUNCATED_FINISH_VALUES = ("length", "max_tokens")
_OVERFLOW_FINISH_VALUE = "model_context_window_exceeded"

# v1.11（V20，CONTRACTS §7.8 嗅探条款——[C-75] 实证种子，冻结面）：
# 溢出错误体 pattern 集，在**完整** 400 resp.text 上（截断之前，F5）做大小写不敏感的
# 子串匹配——OpenAI/Azure 的 code/message 族、vLLM 的纯 message 族（无 code，只匹 code
# 会漏）、anthropic 协议的 "prompt is too long"、z.ai 业务码 "1261" / "Prompt too long"、
# OpenRouter 的 error_type。按剖面预算门控（context_window == 0 ⇒ 嗅探关闭，该 400 走
# v1.10 的 fatal 老路）。
_OVERFLOW_BODY_PATTERNS = (
    "maximum context length",
    "context_length_exceeded",
    "prompt is too long",
    "prompt too long",
    '"code":"1261"',
    '"code": "1261"',
    "context_window_exceeded",
)


def overflow_body_matches(text: str) -> bool:
    """V20 纯匹配器：在完整响应体上对冻结 pattern 集做大小写不敏感的子串测试。

    @param text 完整响应体文本
    @return 命中任一 pattern 为 True
    """
    lowered = text.lower()
    return any(p in lowered for p in _OVERFLOW_BODY_PATTERNS)


def _sniff_overflow_400(context_window: int, status_code: int | None,
                        body_text: str) -> bool:
    """V20 纯门控：仅在剖面预算开启（context_window > 0——预算关闭的 400 逐字节保持
    v1.10 的 fatal 老路）且状态码为 400 时才嗅探。

    @param context_window 剖面声明的上下文窗口（0 = 预算关闭）
    @param status_code 本次响应的 HTTP 状态码
    @param body_text 完整响应体文本
    @return 命中溢出形态为 True
    """
    return (status_code == 400 and context_window > 0
            and overflow_body_matches(body_text))


def _raise_for_finish(finish: str | None, profile: str,
                      max_output_tokens: int) -> None:
    """V11/V24 纯处置：按归一化后的终止原因分派——length（openai）/ max_tokens
    （anthropic）⇒ OutputTruncatedError；model_context_window_exceeded（**双协议**）
    ⇒ reactive 形态的 ContextOverflowError（200 形态神谕）。其余取值原样放行
    （V11③——含 z.ai 的 sensitive / network_error）。

    @param finish provider 返回的原始终止原因（None = 未返回）
    @param profile 剖面名，写入异常字段
    @param max_output_tokens 剖面的输出上限，写入异常消息
    @raises OutputTruncatedError 响应终止于输出上限
    @raises ContextOverflowError 200 形态的上下文溢出神谕
    """
    if finish in _TRUNCATED_FINISH_VALUES:
        raise OutputTruncatedError(
            f"response terminated at the output cap (finish={finish!r}, "
            f"max_output_tokens={max_output_tokens})",
            profile=profile, finish=finish)
    if finish == _OVERFLOW_FINISH_VALUE:
        # origin="finish"（SPEC §3.5）：200 形态神谕来自一次成功的 HTTP 交互（streak 已被
        # 该次 ok 清零）——此形态的降级耗尽终局**不得**由属主算子补喂熔断，而 origin 字段
        # 正是让算子分辨形态的依据。
        raise ContextOverflowError(
            f"provider signaled context overflow via termination reason "
            f"{finish!r} (200-shaped oracle)",
            phase="reactive", profile=profile, origin="finish")


# ── 公开数据结构（CONTRACTS.md §7.8，形状逐字段冻结） ──────────────────────

@dataclass(frozen=True)
class Part:
    """一条消息内的单个内容片段（文本或图像）。"""
    kind: Literal["text", "image"]                 # 片段类型
    text: str | None = None                        # kind="text" 时的文本内容
    image: ImageRef | None = None                  # kind="image" 时的图像引用（字节惰性加载）


@dataclass(frozen=True)
class Message:
    """一条 provider 消息：角色 + 有序片段序列。"""
    role: Literal["system", "user", "assistant"]   # 消息角色
    parts: tuple[Part, ...]                        # 有序片段；单文本片段会退化成纯字符串内容


@dataclass(frozen=True)
class PromptBundle:
    """一次调用的完整消息束（连同采样与图像工作点的按调用覆写）。"""
    messages: tuple[Message, ...]                  # 有序消息序列
    temperature: float | None = None               # None = 用剖面默认温度
    image_px: int | None = None                    # v1.11 追加字段（V23①）：按调用的**生效**
                                                   # 图像 px 载体，是 V21 升清阶梯的唯一载具。
                                                   # 装配方计算生效 px = image_px or
                                                   # profile.default_image_px or
                                                   # profile.max_image_px，再钳
                                                   # min(·, max_image_px)。px 必须随 bundle 走、
                                                   # 绝不放算子状态：build_body() 每次尝试都会
                                                   # 重新编码图像，只有随 bundle 的取值才能让
                                                   # 重试保持确定性


@dataclass(frozen=True)
class LLMResponse:
    """一次成功调用的解析结果（M9 不做任何业务解析）。"""
    text: str                                      # 原始文本载荷（openai_compatible）
    structured: dict | None                        # anthropic tool_choice 的原生载荷，否则 None
    usage: Usage                                   # 本次调用的 token 用量
    model: str                                     # provider 回报的模型名（缺失时回落剖面模型）
    latency_ms: int                                # 末次尝试的耗时
    finish: str | None = None                      # v1.11 追加字段（V23③）：**归一化**终止原因
                                                   # ——即 openai finish_reason 或 anthropic 的
                                                   # stop_reason 原始取值（provider 未给时为
                                                   # None）；喂给 V11/V24 处置。_result_usage 的
                                                   # len==4 分派随元组形状一并调整（F9）


@dataclass                                          # v1.6 每键累加器（CONTRACTS §7.8）
class KeyUsage:
    """单把密钥在本次运行内的用量与状态累加。"""
    calls: int = 0                                 # 该键上成功的逻辑调用数
    rate_limited: int = 0                          # 该键上观察到的 429 次数
    disabled: bool = False                         # 本次运行内是否已被鉴权禁用


@dataclass                                          # 可变的每剖面累加器
class ProfileUsage:
    """单个剖面在本次运行内的用量累加（report.llm_usage 的数据源）。"""
    calls: int = 0                                 # 成功的逻辑调用数
    prompt_tokens: int = 0                         # 累计输入 token
    completion_tokens: int = 0                     # 累计输出 token
    retries: int = 0                               # 累计重试次数
    est_cost_usd: float | None = None              # 仅在两侧单价都配置时才有值
    keys: dict[str, KeyUsage] = field(default_factory=dict)
                                                   # v1.6：按环境变量名索引；报告只在池 > 1 时
                                                   # 输出该子对象（§9.3）
    parked_calls: int = 0                          # v1.6：至少驻留过一次的逻辑调用数
    parked_ms: int = 0                             # v1.6：累计驻留墙钟


@dataclass(frozen=True)
class ProbeResult:
    """单把密钥的探测结果（``validate --probe`` 的展示单元）。"""
    profile: str                                   # 被探测的剖面名
    ok: bool                                       # 是否连通
    model: str                                     # 回报或回落的模型名
    latency_ms: int                                # 探测耗时
    error: str | None = None                       # 失败原因（ok=True 时为 None）
    key_env: str | None = None                     # v1.6：池化（>1 键）剖面由 probe_all() 回填；
                                                   # 单键剖面为 None


@dataclass(frozen=True)
class KeySnapshot:                                 # v1.10（spec 3.9.2）：控制台面板的密钥行
    """密钥池中单把密钥的只读面板投影。"""
    env: str                                       # 环境变量**名**——唯一可展示的身份
                                                   # （密钥**值**永不出现在任何地方，spec 7.4）
    state: Literal["ok", "cooldown", "disabled"]   # 三态；disabled 优先于 cooldown
    cooldown_remaining_s: int = 0                  # 剩余冷却秒数（向上取整）；非 cooldown 时为 0
    calls: int = 0                                 # 每键用量镜像（KeyUsage）——面板 'l' 展开视图
    rate_limited: int = 0                          # （§7.7）；池未材料化时为 0


@dataclass(frozen=True)
class ProfileSnapshot:                             # v1.10（spec 3.9.2）：控制台 LLM 区的一行
    """单个剖面的只读面板投影（每次渲染 tick 拉取一次）。"""
    name: str                                      # 剖面名
    kind: Literal["llm", "embedding"]              # _usage 按**名字**分桶（既有怪癖）——
                                                   # kind 用来消歧快照身份
    in_flight: int                                 # Σ _KeyState.in_flight——在飞的 HTTP 请求，
                                                   # 不含驻留中与退避中的调用
    max_concurrency: int                           # 剖面声明的并发上限
    calls: int                                     # 成功的逻辑调用数
    retries: int                                   # 累计重试次数
    prompt_tokens: int                             # 累计输入 token
    completion_tokens: int                         # 累计输出 token
    est_cost_usd: float | None                     # 未配置单价时为 None（面板显示 "—"）
    p50_latency_ms: int | None                     # 有界窗口（deque 256）中位数，只统计成功调用
                                                   # （spec 3.9.3 快照行）；窗口空时为 None
    keys: tuple[KeySnapshot, ...]                  # 池大小为 1 时是单元素；由 _pool_members 推导，
                                                   # **不**材料化 _pools


# ── v1.6 密钥池（spec 3.9.3 密钥池行） ─────────────────────────────────────

@dataclass
class _KeyState:
    """单把密钥的进程内运行时状态（无持久化，spec §2.6）。"""
    index: int                     # 声明顺序（选键的平手裁决依据）
    env: str                       # 环境变量**名**——唯一会被记录的身份
    key: str = field(repr=False, default="")   # 已解析的密钥值；repr 排除，绝不入日志
    in_flight: int = 0             # 当前在飞的 HTTP 请求数
    cooldown_until: float = 0.0    # time.monotonic() 口径的冷却截止（429 冷却）
    consec_429: int = 0            # 跨调用的连续 429 计数；该键上一次成功即清零
    disabled: bool = False         # 401/403：本次运行余下时间内鉴权已死


class _KeyPool:
    """每 (kind, profile) 的进程内密钥池状态。纯逻辑——``now`` 由调用方注入，
    使选键与驻留算术可离线单测。"""

    def __init__(self, members: list[tuple[str, str]]):
        """按声明顺序建池。

        @param members (环境变量名, 已解析密钥值) 序对列表
        """
        self.states = [_KeyState(index=i, env=env, key=key)
                       for i, (env, key) in enumerate(members)]

    @property
    def size(self) -> int:
        """@return 池大小（含已禁用的键）。"""
        return len(self.states)

    def live(self) -> list[_KeyState]:
        """@return 未被鉴权禁用的键（可能仍在冷却中）。"""
        return [s for s in self.states if not s.disabled]

    def select(self, now: float) -> _KeyState | None:
        """选出在飞最少的可用密钥，平手按声明顺序裁决——确定性、无随机数
        （纯时序、豁免 seed 纪律；spec 3.9.3）。

        @param now 当前时刻（time.monotonic() 口径）
        @return 选中的密钥状态；无可用键时为 None
        """
        eligible = [s for s in self.states
                    if not s.disabled and s.cooldown_until <= now]
        if not eligible:
            return None
        return min(eligible, key=lambda s: (s.in_flight, s.index))

    def earliest_wake(self, now: float) -> float:
        """@param now 当前时刻（time.monotonic() 口径）
        @return 最早一把活键退出冷却还需的秒数（≥ 0）；调用方保证至少有一把活键。
        """
        return max(0.0, min(s.cooldown_until for s in self.live()) - now)


def _key_cooldown_upper(base_delay_s: float, consec_429: int) -> float:
    """无 Retry-After 时的每键 429 冷却上界：全抖动 random(0, base × 2^c)，
    其上界封顶 300 s（spec 3.9.3）。

    @param base_delay_s 剖面的 retry_base_delay_s
    @param consec_429 该键跨调用的连续 429 计数 c
    @return 冷却时长的上界（秒）
    """
    return min(_MAX_KEY_COOLDOWN_S, base_delay_s * (2.0 ** consec_429))


def _pool_members(prof: "LLMProfile | EmbeddingProfile") -> list[tuple[str, str]]:
    """解析剖面的密钥池成员（v1.6）。经 M1 归一的剖面带对齐的
    api_key_envs/api_keys；直接构造的剖面（测试、探针子客户端）回落到 api_key
    或环境变量，与 v1.6 之前的单键行为一致。

    @param prof [llm.*] 或 [embedding.*] 剖面
    @return (环境变量名, 已解析密钥值) 序对列表
    """
    envs = tuple(prof.api_key_envs) or ((prof.api_key_env,) if prof.api_key_env else ())
    if not envs:
        return [("", prof.api_key or "")]
    keys = tuple(prof.api_keys)
    if len(keys) != len(envs):
        if len(envs) == 1:
            keys = (prof.api_key or os.environ.get(envs[0], ""),)
        else:
            keys = tuple(os.environ.get(e, "") for e in envs)
    return list(zip(envs, keys))


# ── 内部参数对象（重试引擎与探针的入参收拢面） ─────────────────────────────

@dataclass(frozen=True)
class _CallSpec:
    """一次逻辑调用的请求装配契约（``_post_with_retries`` 的唯一入参）。"""
    kind: Literal["llm", "embedding"]              # 剖面种类；与信号量、密钥池、p50 窗口同键，
                                                   # 使同名的 llm 与 embedding 剖面互不串用
    prof: LLMProfile | EmbeddingProfile            # 目标剖面（并发/重试/超时/密钥池来源）
    url: str                                       # 完整 POST 端点
    build_body: Callable[[], dict]                 # 每次尝试重建请求体——图像字节在此惰性加载
    parse: Callable[[Mapping], tuple]              # 2xx 响应解析器；返回元组形状见 _result_usage
    operation: str | None = None                   # llm.call 事件的 operation 字段（embedding 用）
    trace_extra: Mapping | None = None             # trace.content="full" 时的输入消息载荷
    # 仅成功时调用：从 parse 结果渲染追加的 trace 字段（如 gen_ai.output.messages）
    finalize_extra: Callable[[tuple], Mapping] | None = None


@dataclass(frozen=True)
class _CallOutcome:
    """一次逻辑调用的结局，用于装配 llm.call 事件载荷（键与时序为冻结面）。"""
    latency_ms: int                                # 末次尝试耗时
    usage: Usage                                   # 成功时的实际用量；失败终态一律空 Usage
    retries: int                                   # 已消耗的重试次数
    status: str                                    # ok / fatal / retryable_exhausted /
                                                   # breaker_aborted（冻结词表）
    extra: Mapping | None = None                   # 追加载荷（key_env 与 gen_ai.* 消息键）


@dataclass(frozen=True)
class _AttemptFailure:
    """单次尝试的失败描述（尚未终局化——是否重试由重试引擎裁决）。"""
    message: str                                   # 供异常消息使用的失败描述（已截断到 300 字符）
    status_code: int | None = None                 # HTTP 状态码；传输层异常为 None
    body_text: str = ""                            # **完整**响应体（V20 嗅探用，截断之前，F5）
    retry_after: float | None = None               # 429 的 Retry-After 解析结果（秒）
    retryable: bool = True                         # 是否属于可重试类别（传输层异常恒为 True）


@dataclass(frozen=True)
class _ProbeTarget:
    """一次探测的目标：剖面 + 具体某把密钥。"""
    profile: str                                   # 剖面名
    prof: LLMProfile | EmbeddingProfile            # 剖面对象
    is_llm: bool                                   # True = [llm.*]，False = [embedding.*]
    env: str                                       # 本次探测使用的密钥环境变量名
    key: str                                       # 已解析的密钥值（绝不入日志与结果）
    key_env: str | None                            # 回填进 ProbeResult.key_env；单键探测为 None


@dataclass
class _RetryContext:
    """重试引擎在单次逻辑调用内的可变状态（生命周期 = 一次 ``_post_with_retries``）。"""
    spec: _CallSpec                # 本次调用的请求装配契约
    pool: _KeyPool                 # 该剖面的密钥池（v1.6）
    acc: ProfileUsage              # 该剖面的用量累加器（retries / parked_* 就地累加）
    park_budget: float             # run.max_park_s：本次逻辑调用的驻留上限（秒）
    retries_used: int = 0          # 已消耗的重试次数（llm.call.retries）
    latency_ms: int = 0            # 最近一次尝试的耗时；尚无尝试时为 0
    park_spent: float = 0.0        # 本次调用已累计的驻留秒数
    parked: bool = False           # 是否已计入 parked_calls（每逻辑调用至多一次）
    last_env: str | None = None    # 最近一次尝试所用密钥的环境变量**名**
    attempt: int = 0               # 已完成的尝试序号；与 prof.max_retries 比较判定耗尽
    result: tuple | None = None    # 成功时的 parse() 结果元组

    @property
    def prof(self) -> LLMProfile | EmbeddingProfile:
        """@return 本次调用的目标剖面。"""
        return self.spec.prof

    def key_extra(self) -> Mapping | None:
        """llm.call 载荷的 key_env 附加面（仅池 > 1）：最近一次尝试所用密钥的环境变量名；
        零尝试的调用不带该键。

        @return 合并后的 trace 载荷；非池化或零尝试时原样返回 spec.trace_extra
        """
        if self.pool.size > 1 and self.last_env is not None:
            merged = dict(self.spec.trace_extra or {})
            merged["key_env"] = self.last_env
            return merged
        return self.spec.trace_extra


# ── 纯函数：重试算术与错误分类 ─────────────────────────────────────────────

def _is_retryable_status(status: int) -> bool:
    """@param status HTTP 状态码
    @return 可重试（HTTP 408/409/429/5xx，spec 3.9.3）为 True，其余一律致命。
    """
    return status in (408, 409, 429) or 500 <= status <= 599


def _backoff_delay(retry_no: int, base_delay_s: float, rng: random.Random) -> float:
    """全抖动指数退避：wait_i = random(0, base × 2^i)，其上界封顶 60 s（spec 3.9.3）。

    @param retry_no 1 基的重试序号（spec 3.9.4 ③ 的时间线里，第二次尝试后的等待取 i=2）
    @param base_delay_s 剖面的 retry_base_delay_s
    @param rng 抖动随机源（纯时序，不由 seed 派生）
    @return 本次等待秒数
    """
    upper = min(_MAX_BACKOFF_S, base_delay_s * (2.0 ** retry_no))
    return rng.uniform(0.0, upper)


def _retry_after_seconds(value: str) -> float | None:
    """解析 Retry-After 的 delta-seconds 形式。

    @param value 已去空白的头部值
    @return 秒数（负值钳到 0）；非数值形式为 None
    """
    try:
        return max(0.0, float(value))
    except ValueError:
        _logger.debug("Retry-After is not delta-seconds; trying the HTTP-date form")
        return None


def _retry_after_http_date(value: str, now: datetime | None) -> float | None:
    """解析 Retry-After 的 HTTP-date 形式。

    @param value 已去空白的头部值
    @param now 参考时刻；None 表示取当前 UTC 时间
    @return 距该时刻的秒数（过去的时刻钳到 0）；无法解析为 None
    """
    try:
        dt = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        _logger.warning("unparseable Retry-After header ignored")
        return None
    # RFC 5322 的 "-0000"（本地时区未知）解析为 naive datetime——按 UTC 处理。
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    ref = now if now is not None else datetime.now(timezone.utc)
    return max(0.0, (dt - ref).total_seconds())


def _parse_retry_after(value: str | None, *, now: datetime | None = None) -> float | None:
    """解析 Retry-After 头部：delta-seconds 或 HTTP-date。

    @param value 头部原始值；None 或空串表示未提供
    @param now 参考时刻（HTTP-date 形式用），便于离线单测注入
    @return 需等待的秒数；缺失或无法解析为 None
    """
    if value is None:
        return None
    value = value.strip()
    if not value:
        return None
    seconds = _retry_after_seconds(value)
    return seconds if seconds is not None else _retry_after_http_date(value, now)


# ── 纯函数：请求体装配 ─────────────────────────────────────────────────────

def _resolve_temperature(profile: LLMProfile, prompt: PromptBundle) -> float:
    """@param profile 目标剖面
    @param prompt 消息束（temperature 为 None 时用剖面默认值）
    @return 本次请求生效的温度。
    """
    return profile.temperature if prompt.temperature is None else prompt.temperature


def _effective_image_px(profile: LLMProfile, prompt: PromptBundle) -> int:
    """v1.11 生效 px 链（V18/V21/V23①，spec 3.9.3 图像编码行）：
    bundle.image_px（升清载体）or profile.default_image_px（工作点）or
    profile.max_image_px，再钳到 max_image_px（天花板）。全零/None 的腿逐字节退化为
    v1.10 的 max_image_px。

    @param profile 目标剖面
    @param prompt 消息束
    @return 本次请求生效的图像采样边长（像素）
    """
    px = prompt.image_px or profile.default_image_px or profile.max_image_px
    return min(px, profile.max_image_px)


def _build_openai_body(profile: LLMProfile, prompt: PromptBundle,
                       response_schema: dict | None) -> dict:
    """装配 POST {base_url}/chat/completions 的请求体。图像编码为 image_url 的 data URI；
    结构化输出 = response_format json_schema strict（spec 3.9.3 / 3.9.4 ①）。图像字节在
    **此处**（请求装配时）惰性加载，且只存在于返回的请求体内。

    @param profile 目标剖面
    @param prompt 消息束
    @param response_schema 结构化输出 schema；仅在剖面支持时才写入请求体
    @return 可直接 json 序列化的请求体
    """
    image_px = _effective_image_px(profile, prompt)
    messages: list[dict] = []
    for msg in prompt.messages:
        content: Any
        if len(msg.parts) == 1 and msg.parts[0].kind == "text":
            content = msg.parts[0].text or ""
        else:
            content = []
            for part in msg.parts:
                if part.kind == "text":
                    content.append({"type": "text", "text": part.text or ""})
                else:
                    assert part.image is not None
                    media_type, b64 = part.image.load_base64(image_px)
                    content.append({"type": "image_url",
                                    "image_url": {"url": f"data:{media_type};base64,{b64}"}})
        messages.append({"role": msg.role, "content": content})
    body: dict = {
        "model": profile.model,
        "temperature": _resolve_temperature(profile, prompt),
        "max_tokens": profile.max_output_tokens,
        "messages": messages,
    }
    if response_schema is not None and profile.supports_structured_output:
        body["response_format"] = {
            "type": "json_schema",
            "json_schema": {"name": "user_schema", "strict": True, "schema": response_schema},
        }
    return body


def _build_anthropic_body(profile: LLMProfile, prompt: PromptBundle,
                          response_schema: dict | None) -> dict:
    """装配 POST {base_url}/v1/messages 的请求体。system 消息折叠进顶层 `system` 参数；
    图像用 source.type="base64"；结构化输出 = 单个强制工具 "emit"，schema 作其
    input_schema（CONTRACTS.md §7.8）。

    @param profile 目标剖面
    @param prompt 消息束
    @param response_schema 结构化输出 schema；仅在剖面支持时才写入请求体
    @return 可直接 json 序列化的请求体
    """
    image_px = _effective_image_px(profile, prompt)
    system_chunks: list[str] = []
    messages: list[dict] = []
    for msg in prompt.messages:
        if msg.role == "system":
            system_chunks.extend(part.text or "" for part in msg.parts if part.kind == "text")
            continue
        blocks: list[dict] = []
        for part in msg.parts:
            if part.kind == "text":
                blocks.append({"type": "text", "text": part.text or ""})
            else:
                assert part.image is not None
                media_type, b64 = part.image.load_base64(image_px)
                blocks.append({"type": "image",
                               "source": {"type": "base64",
                                          "media_type": media_type,
                                          "data": b64}})
        messages.append({"role": msg.role, "content": blocks})
    body: dict = {
        "model": profile.model,
        "max_tokens": profile.max_output_tokens,
        "temperature": _resolve_temperature(profile, prompt),
        "messages": messages,
    }
    if system_chunks:
        body["system"] = "\n".join(system_chunks)
    if response_schema is not None and profile.supports_structured_output:
        body["tools"] = [{"name": STRUCTURED_TOOL_NAME, "input_schema": response_schema}]
        body["tool_choice"] = {"type": "tool", "name": STRUCTURED_TOOL_NAME}
    return body


def _build_embeddings_body(profile: EmbeddingProfile, texts: list[str]) -> dict:
    """装配 POST {base_url}/embeddings 的请求体（spec 3.9.3，v1.2）。

    @param profile 目标 embedding 剖面
    @param texts 待嵌入文本，顺序即响应对齐顺序
    @return 可直接 json 序列化的请求体
    """
    return {"model": profile.model, "input": list(texts)}


def _build_headers(provider: str, api_key: str) -> dict[str, str]:
    """装配 provider 对应的请求头。

    @param provider "anthropic" 或 "openai_compatible"
    @param api_key 本次尝试选中的密钥值
    @return 请求头字典
    """
    if provider == "anthropic":
        return {"x-api-key": api_key,
                "anthropic-version": ANTHROPIC_VERSION,
                "content-type": "application/json"}
    return {"Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json"}


# ── 纯函数：响应解析 ───────────────────────────────────────────────────────

def _parse_anthropic_response(data: Mapping, fallback_model: str
                              ) -> tuple[str, dict | None, Usage, str, str | None]:
    """从 /v1/messages 响应提取 (text, structured, usage, model, finish)。tool_use 块
    （强制工具）⇒ 结构化载荷；text 块拼接为文本；finish = 原始 stop_reason
    （v1.11 V23③，provider 未给时为 None）。

    @param data 已解析的响应 JSON
    @param fallback_model 响应未带模型名时的回落值
    @return 五元组，形状由 _result_usage 依赖（F9）
    """
    texts: list[str] = []
    structured: dict | None = None
    for block in data.get("content") or ():
        if not isinstance(block, Mapping):
            continue
        btype = block.get("type")
        if btype == "text":
            texts.append(str(block.get("text") or ""))
        elif btype == "tool_use" and structured is None:
            payload = block.get("input")
            if isinstance(payload, Mapping):
                structured = dict(payload)
    raw_usage = data.get("usage") or {}
    usage = Usage(prompt_tokens=int(raw_usage.get("input_tokens") or 0),
                  completion_tokens=int(raw_usage.get("output_tokens") or 0))
    model = str(data.get("model") or fallback_model)
    stop_reason = data.get("stop_reason")
    finish = str(stop_reason) if isinstance(stop_reason, str) and stop_reason else None
    return "\n".join(texts), structured, usage, model, finish


def _parse_openai_response(data: Mapping, fallback_model: str
                           ) -> tuple[str, dict | None, Usage, str, str | None]:
    """从 /chat/completions 响应提取 (text, structured=None, usage, model, finish)。
    json_schema 模式的产物就是**文本**——M9 从不解析它（spec 3.9.1）；finish = 原始
    choices[0].finish_reason（v1.11 V23③）。缺失或形状意外的部分一律退化为默认值而不
    抛出，使畸形 2xx 永不未分类地逃出 M9。

    @param data 已解析的响应 JSON
    @param fallback_model 响应未带模型名时的回落值
    @return 五元组，形状由 _result_usage 依赖（F9）
    """
    text = ""
    finish: str | None = None
    choices = data.get("choices")
    first = choices[0] if isinstance(choices, (list, tuple)) and choices else None
    message = first.get("message") if isinstance(first, Mapping) else None
    if isinstance(message, Mapping):
        content = message.get("content")
        if isinstance(content, str):
            text = content
        elif isinstance(content, list):  # 部分网关回带类型的片段列表
            text = "".join(str(p.get("text") or "") for p in content if isinstance(p, Mapping))
    if isinstance(first, Mapping):
        finish_reason = first.get("finish_reason")
        if isinstance(finish_reason, str) and finish_reason:
            finish = finish_reason
    raw_usage = data.get("usage")
    if not isinstance(raw_usage, Mapping):
        raw_usage = {}
    usage = Usage(prompt_tokens=int(raw_usage.get("prompt_tokens") or 0),
                  completion_tokens=int(raw_usage.get("completion_tokens") or 0))
    model = str(data.get("model") or fallback_model)
    return text, None, usage, model, finish


def _parse_embeddings_response(data: Mapping, n_texts: int, profile_name: str,
                               dims: int | None) -> tuple[list[list[float]], Usage]:
    """提取与输入顺序对齐的向量，并执行 dims 校验（不符 ⇒ ProviderFatalError，
    spec 3.9.2）。``data`` 内非 Mapping 的条目被丢弃而不是崩溃——它们随后表现为条数不符
    （ProviderFatalError），绝不会成为未分类的 AttributeError。

    @param data 已解析的响应 JSON
    @param n_texts 请求的文本条数
    @param profile_name 剖面名，写入异常字段
    @param dims 声明的向量维数；None 表示不校验
    @return (向量列表, 用量)
    @raises ProviderFatalError 条数不符或维数不符
    """
    raw_items = data.get("data")
    items = [it for it in (raw_items if isinstance(raw_items, (list, tuple)) else ())
             if isinstance(it, Mapping)]
    items.sort(key=lambda item: int(item.get("index") or 0))
    vectors = [[float(x) for x in (item.get("embedding") or ())] for item in items]
    if len(vectors) != n_texts:
        raise ProviderFatalError(
            f"embeddings response returned {len(vectors)} vectors for {n_texts} inputs",
            profile=profile_name, status_code=None)
    if dims is not None:
        for i, vec in enumerate(vectors):
            if len(vec) != dims:
                raise ProviderFatalError(
                    f"embedding dims mismatch at index {i}: expected {dims}, got {len(vec)}",
                    profile=profile_name, status_code=None)
    raw_usage = data.get("usage") or {}
    usage = Usage(prompt_tokens=int(raw_usage.get("prompt_tokens") or 0),
                  completion_tokens=int(raw_usage.get("completion_tokens") or 0))
    return vectors, usage


def _classify_http_failure(resp: httpx.Response) -> _AttemptFailure:
    """把非 2xx 响应归类成尝试级失败描述。**完整**响应体保留给 V20 嗅探（在任何截断
    之前，F5）；失败消息本身仍截断到 300 字符。

    @param resp 非 2xx 响应
    @return 尝试级失败描述
    """
    status = resp.status_code
    body_text = resp.text
    return _AttemptFailure(
        message=f"HTTP {status}: {body_text[:300]}",
        status_code=status,
        body_text=body_text,
        retry_after=(_parse_retry_after(resp.headers.get("retry-after"))
                     if status == 429 else None),
        retryable=_is_retryable_status(status))


# ── 纯函数：计量与 trace 渲染 ──────────────────────────────────────────────

def _accumulate_usage(acc: ProfileUsage, usage: Usage, retries: int,
                      price_per_mtok_in: float | None,
                      price_per_mtok_out: float | None) -> None:
    """记入一次成功的逻辑调用：calls+1、token 求和、重试数累加；两侧单价都配置时，
    成本按运行总量重算（spec 3.9.3 计量）。

    @param acc 该剖面的用量累加器
    @param usage 本次调用的用量
    @param retries 本次调用消耗的重试次数
    @param price_per_mtok_in 每百万输入 token 单价；None 表示未配置
    @param price_per_mtok_out 每百万输出 token 单价；None 表示未配置
    """
    acc.calls += 1
    acc.prompt_tokens += usage.prompt_tokens
    acc.completion_tokens += usage.completion_tokens
    acc.retries += retries
    if price_per_mtok_in is not None and price_per_mtok_out is not None:
        acc.est_cost_usd = (acc.prompt_tokens / 1e6 * price_per_mtok_in
                            + acc.completion_tokens / 1e6 * price_per_mtok_out)


def _render_trace_messages(prompt: PromptBundle) -> list[dict]:
    """渲染 trace.content='full' 的 gen_ai.input.messages 载荷。图像只按路径引用、
    绝不内联（base64 载荷不属于 trace）。

    @param prompt 本次请求的消息束
    @return 可序列化的消息列表
    """
    rendered: list[dict] = []
    for msg in prompt.messages:
        content: list[dict] = []
        for part in msg.parts:
            if part.kind == "text":
                content.append({"type": "text", "text": part.text or ""})
            else:
                content.append({"type": "image",
                                "path": str(part.image.path) if part.image else ""})
        rendered.append({"role": msg.role, "content": content})
    return rendered


def _render_output_messages(text: str, structured: dict | None) -> list[dict]:
    """渲染 trace.content='full' 的 gen_ai.output.messages 载荷（spec 7.4、
    CONTRACTS.md §8.2/§8.3）：助手回复——有原生结构化载荷（anthropic 强制工具）时用它，
    否则用原始文本。

    @param text 原始文本载荷
    @param structured 原生结构化载荷；None 表示没有
    @return 可序列化的消息列表
    """
    return [{"role": "assistant",
             "content": structured if structured is not None else text}]


def _key_snapshot(state: _KeyState, ts: float,
                  key_usages: Mapping[str, KeyUsage]) -> KeySnapshot:
    """把一把密钥的运行时状态投影成面板行（disabled > cooldown > ok 三态）。

    @param state 密钥运行时状态
    @param ts 快照时刻（time.monotonic() 口径）
    @param key_usages 该剖面的每键用量镜像
    @return 只读的面板密钥行
    """
    cooling = not state.disabled and state.cooldown_until > ts
    usage = key_usages.get(state.env)
    return KeySnapshot(
        env=state.env,
        state="disabled" if state.disabled else "cooldown" if cooling else "ok",
        cooldown_remaining_s=math.ceil(state.cooldown_until - ts) if cooling else 0,
        calls=usage.calls if usage is not None else 0,
        rate_limited=usage.rate_limited if usage is not None else 0)


# ── 客户端 ─────────────────────────────────────────────────────────────────

class LLMClient:
    """按剖面索引的异步 provider 客户端（CONTRACTS.md §7.8）。"""

    def __init__(self, llm_profiles: Mapping[str, LLMProfile],
                 embedding_profiles: Mapping[str, EmbeddingProfile],
                 metrics: "MetricsSink | None" = None):
        """建立进程内的剖面表、限流器、密钥池与校准器（全部只在内存，spec §2.6）。

        @param llm_profiles [llm.*] 剖面表，按声明顺序
        @param embedding_profiles [embedding.*] 剖面表，按声明顺序
        @param metrics 观测汇（M12）；None 表示不发事件、不喂熔断
        """
        self._llm_profiles: dict[str, LLMProfile] = dict(llm_profiles)
        self._embedding_profiles: dict[str, EmbeddingProfile] = dict(embedding_profiles)
        self._metrics = metrics
        self._usage: dict[str, ProfileUsage] = {}
        # 每剖面一个信号量，被**所有**调用共享（含修复、verify、probe）。键为
        # (kind, name)，使同名的 llm 与 embedding 剖面永不共用限流器。
        self._semaphores: dict[tuple[str, str], asyncio.Semaphore] = {}
        # v1.6 密钥池状态，键与信号量一致（仅内存，spec §2.6——无持久化）。
        self._pools: dict[tuple[str, str], _KeyPool] = {}
        # v1.10 p50 延迟窗口（spec 3.9.3 快照行）：每 (kind, name) 一条有界样本队列，
        # 只收成功的逻辑调用——**唯一**新增采集点；既不入 report.json 也不入任何事件。
        self._latencies: dict[tuple[str, str], deque] = {}
        # v1.11（V19/V23②）：每剖面的每图成本校准器——**自持构造**（factory 与 runtime
        # 装配零改动；RunContext 的六个冻结字段不动）。先验需要 (provider, 工作点 px)，
        # 工作点 px = default_image_px or max_image_px（V18）。
        self.calibrator = ImageCostCalibrator({
            name: (p.provider, p.default_image_px or p.max_image_px)
            for name, p in self._llm_profiles.items()})
        # [C-64] 兜底记账：缺 usage 的响应每剖面只 WARN 一次
        # （"image-cost calibration inactive"），此后保持静默。
        self._calibration_warned: set[str] = set()
        # 抖动随机源刻意**不**由 seed 派生——纯时序 [FROZEN §7.8]。
        self._jitter_rng = random.Random()
        self._http_client: httpx.AsyncClient | None = None

    # -- 公开 API ------------------------------------------------------------

    async def complete(self, profile: str, prompt: PromptBundle,
                       response_schema: dict | None = None) -> LLMResponse:
        """发起一次 LLM 内容调用——M9 的唯一咽喉。

        response_schema 只在剖面声明 supports_structured_output 时成为 L0 参数，否则忽略。
        v1.11（spec 3.9.5）：声明了预算的剖面在任何 provider 分派前先跑 V16 终检；每个 200
        都按终止原因终局化（V11/V24——OutputTruncatedError / reactive ContextOverflowError，
        二者都在成功记账**之后**抛出，永不喂熔断、永不进重试环）；带图的成功响应喂 V19 校准器。

        @param profile [llm.*] 剖面名
        @param prompt 待发送的消息束
        @param response_schema 结构化输出 schema；None 表示不约束
        @return 解析后的 LLMResponse
        @raises ValueError 未知剖面
        @raises CircuitBreakerTripped 熔断器已开（快速失败）
        @raises ProviderRetryableError 重试耗尽或驻留超 run.max_park_s
        @raises ProviderFatalError provider 致命错误
        @raises ContextOverflowError 终检超窗（precheck）或 provider 溢出信号（reactive）
        @raises OutputTruncatedError 响应终止于输出上限
        """
        prof = self._llm_profiles.get(profile)
        if prof is None:
            raise ValueError(f"unknown [llm.*] profile: {profile!r}")
        self._check_breaker()
        # schema 只在 supports_structured_output 下上线（L0 条款）——未发送的 schema 不得
        # 抬高估算（prompt 内嵌的 schema 副本已由它所在的文本片段计入）。
        effective_schema = response_schema if prof.supports_structured_output else None
        self._precheck_budget(prof, prompt, effective_schema)

        result, latency_ms, retries = await self._post_with_retries(
            self._complete_spec(prof, prompt, response_schema))
        text, structured, usage, model, finish = result
        _accumulate_usage(self._usage.setdefault(prof.name, ProfileUsage()),
                          usage, retries,
                          prof.price_per_mtok_in, prof.price_per_mtok_out)
        self._feed_calibrator(prof, prompt, effective_schema, usage)
        # v1.11 终止原因处置（V11/V24，[C-57][C-58]）——在成功记账**之后**（streak 已清零、
        # llm.call status="ok" 已在 _post_with_retries 内发出；按 spec §3.5 二者都不是
        # provider-fatal，实现者不得"修正"为 fatal，F9）。两者都不进重试环、不喂熔断。
        _raise_for_finish(finish, prof.name, prof.max_output_tokens)
        return LLMResponse(text=text, structured=structured, usage=usage,
                           model=model, latency_ms=latency_ms, finish=finish)

    async def embed(self, profile: str, texts: list[str]) -> list[list[float]]:
        """v1.2 向量化调用。profile 必须是 [embedding.*] 名字——传 [llm.*] 名字直接
        ValueError。仅支持 openai_compatible：POST {base_url}/embeddings；向量与输入顺序
        对齐；dims 不符 ⇒ ProviderFatalError。用量记在 embedding 剖面名下；每次调用发一条
        operation="embedding" 的 llm.call 事件。重试与限流规则与 complete() 完全一致。

        @param profile [embedding.*] 剖面名
        @param texts 待嵌入文本
        @return 与输入顺序对齐的向量列表
        @raises ValueError 未知剖面或误传 [llm.*] 名字
        @raises ProviderFatalError 条数/维数不符或 provider 致命错误
        @raises ProviderRetryableError 重试耗尽或驻留超 run.max_park_s
        @raises CircuitBreakerTripped 熔断器已开（快速失败）
        """
        prof = self._embedding_profiles.get(profile)
        if prof is None:
            if profile in self._llm_profiles:
                raise ValueError(
                    f"embed() requires an [embedding.*] profile; {profile!r} is an [llm.*] name")
            raise ValueError(f"unknown [embedding.*] profile: {profile!r}")
        self._check_breaker()

        n = len(texts)
        (result,), _latency_ms, retries = await self._post_with_retries(_CallSpec(
            kind="embedding", prof=prof,
            url=prof.base_url.rstrip("/") + "/embeddings",
            build_body=lambda: _build_embeddings_body(prof, texts),
            parse=lambda data: _split_embed(data, n, prof),
            operation="embedding"))
        vectors, usage = result
        _accumulate_usage(self._usage.setdefault(prof.name, ProfileUsage()),
                          usage, retries, None, None)
        return vectors

    async def probe(self, profile: str) -> ProbeResult:
        """validate --probe：对 llm 剖面做一次最小的 1 token 活体调用，对 embedding
        剖面做一次单文本嵌入。永不抛出，失败落在 .error 里。池化剖面只探**第一把**密钥
        （v1.6）——其余由 probe_all 覆盖。

        @param profile 剖面名
        @return 单条探测结果
        """
        return (await self._probe_keys(profile, first_only=True))[0]

    async def probe_all(self, profile: str) -> list[ProbeResult]:
        """v1.6：按声明顺序对池内每把密钥各探一次，llm 与 embedding 剖面同规——池化
        （>1 键）剖面的结果带 key_env。单键剖面退化为 [await probe(profile)] 且
        key_env=None。供 ``validate --probe`` 使用。永不抛出。

        @param profile 剖面名
        @return 每把密钥一条探测结果
        """
        return await self._probe_keys(profile, first_only=False)

    @property
    def usage_by_profile(self) -> dict[str, ProfileUsage]:
        """@return 按剖面名索引的用量累加器（report.llm_usage 的数据源，实时可变）。"""
        return self._usage

    def snapshot(self, now: float | None = None) -> tuple[ProfileSnapshot, ...]:
        """v1.10（spec 3.9.2 / 3.9.3 快照行）：控制台面板的只读拉取面（§7.7，每渲染 tick
        一次，U19/U26）。纯读——不 await、不加锁，且**绝不**改状态（尤其不会材料化
        ``self._pools``）；只在渲染 tick（事件循环线程）里调用，因此 U26 下不存在跨线程竞争。

        枚举顺序：先全部 [llm.*] 剖面、再全部 [embedding.*] 剖面，各按声明顺序。

        @param now 快照时刻（time.monotonic() 口径），便于离线单测注入；None 表示取当前时刻
        @return 每剖面一行的只读快照
        """
        ts = time.monotonic() if now is None else now
        profile_maps: tuple[tuple[Literal["llm", "embedding"], Mapping], ...] = (
            ("llm", self._llm_profiles), ("embedding", self._embedding_profiles))
        return tuple(self._profile_snapshot(kind, name, prof, ts)
                     for kind, profiles in profile_maps
                     for name, prof in profiles.items())

    async def aclose(self) -> None:
        """释放共享的 httpx.AsyncClient（工具方法；运行结束时调用）。"""
        if self._http_client is not None:
            await self._http_client.aclose()
            self._http_client = None

    # -- complete() 的分解 ----------------------------------------------------

    def _precheck_budget(self, prof: LLMProfile, prompt: PromptBundle,
                         schema: dict | None) -> None:
        """v1.11 终检（V16 precheck，F13）：complete() 是唯一咽喉——M8 L3 修复环调用与探针
        同走此处（探针平凡通过：max_output_tokens=1 + V6 的正预算校验）。预算关闭的剖面
        （cw == 0）整段跳过；装填层正确时它永不触发（防御性不变式，不是第二套装填逻辑）。
        零 provider 交互：不喂熔断、不烧重试。

        @param prof 目标剖面
        @param prompt 待估算的消息束
        @param schema 真正会上线的 schema（None 表示不上线）
        @raises ContextOverflowError phase="precheck"，估算超窗
        """
        if prof.context_window <= 0:
            return
        est = budget.est_prompt(prompt, prof, schema,
                                image_cost=self.calibrator.cost(prof.name))
        margin = budget.margin(prof.context_window)
        if est + prof.max_output_tokens + margin <= prof.context_window:
            return
        raise ContextOverflowError(
            f"estimated prompt {est} tokens + max_output_tokens "
            f"{prof.max_output_tokens} + margin "
            f"{margin} exceeds "
            f"context_window {prof.context_window}",
            phase="precheck", profile=prof.name)

    def _complete_spec(self, prof: LLMProfile, prompt: PromptBundle,
                       response_schema: dict | None) -> _CallSpec:
        """装配 complete() 的请求契约：provider 分支的 URL / 请求体 / 解析器 + trace 载荷。

        @param prof 目标 [llm.*] 剖面
        @param prompt 消息束
        @param response_schema 原始 schema（是否上线由请求体装配按 L0 条款决定）
        @return 供重试引擎消费的 _CallSpec
        """
        if prof.provider == "anthropic":
            url = prof.base_url.rstrip("/") + "/v1/messages"
            build_body: Callable[[], dict] = lambda: _build_anthropic_body(
                prof, prompt, response_schema)
            parse: Callable[[Mapping], tuple] = lambda data: _parse_anthropic_response(
                data, prof.model)
        else:
            url = prof.base_url.rstrip("/") + "/chat/completions"
            build_body = lambda: _build_openai_body(prof, prompt, response_schema)
            parse = lambda data: _parse_openai_response(data, prof.model)
        full_trace = self._full_content_trace_enabled()
        # 成功时 llm.call 事件还必须携带响应内容（spec 7.4 / CONTRACTS §8.2）。事件在
        # _post_with_retries 内发出，因此输出消息在那里、由 parse 结果渲染后再序列化。
        finalize = ((lambda result: {"gen_ai.output.messages":
                                     _render_output_messages(result[0], result[1])})
                    if full_trace else None)
        return _CallSpec(
            kind="llm", prof=prof, url=url, build_body=build_body, parse=parse,
            trace_extra=({"gen_ai.input.messages": _render_trace_messages(prompt)}
                         if full_trace else {}),
            finalize_extra=finalize)

    def _feed_calibrator(self, prof: LLMProfile, prompt: PromptBundle,
                         schema: dict | None, usage: Usage) -> None:
        """v1.11 校准喂点（V19/V23②，spec 3.9.3 校准采样行）：每个带图响应把
        (prompt_tokens − 纯文本估算) / 图片数 采样进**当前批次**的桶；无 usage 的响应
        （[C-64] 网关）不记样本、每剖面只 WARN 一次——先验 × PRIOR_INFLATION 无限期沿用。

        @param prof 目标剖面
        @param prompt 本次请求的消息束（用于数图片与算纯文本估算）
        @param schema 真正上线的 schema（估算须与请求一致）
        @param usage provider 回报的用量
        """
        n_images = sum(1 for m in prompt.messages
                       for p in m.parts if p.kind == "image")
        if not n_images:
            return
        if usage.prompt_tokens > 0:
            text_est = budget.est_prompt(prompt, prof, schema, image_cost=0)
            self.calibrator.observe(prof.name, usage.prompt_tokens, text_est, n_images)
        elif prof.name not in self._calibration_warned:
            self._calibration_warned.add(prof.name)
            _logger.warning(
                "image-cost calibration inactive: profile %s returns no usage", prof.name)

    # -- 探针 -----------------------------------------------------------------

    async def _probe_keys(self, profile: str, *, first_only: bool) -> list[ProbeResult]:
        """按声明顺序逐把密钥探测。

        @param profile 剖面名（[llm.*] 或 [embedding.*]）
        @param first_only True = 只探第一把密钥（probe），False = 全池（probe_all）
        @return 每把密钥一条结果；未知剖面回单条 ok=False 结果
        """
        is_llm = profile in self._llm_profiles
        if not is_llm and profile not in self._embedding_profiles:
            return [ProbeResult(profile=profile, ok=False, model="", latency_ms=0,
                                error=f"unknown profile: {profile!r}")]
        prof = (self._llm_profiles[profile] if is_llm
                else self._embedding_profiles[profile])
        members = _pool_members(prof)
        pooled = len(members) > 1 and not first_only
        if first_only:
            members = members[:1]
        return [await self._probe_one(_ProbeTarget(
                    profile=profile, prof=prof, is_llm=is_llm, env=env, key=key,
                    key_env=env if pooled else None))
                for env, key in members]

    async def _probe_one(self, target: _ProbeTarget) -> ProbeResult:
        """探测单把密钥。

        @param target 探测目标（剖面、密钥、待回填的 key_env）
        @return 成功或失败的 ProbeResult；本方法永不抛出
        """
        start = time.monotonic()
        client = self._probe_client(target)
        try:
            model, latency_ms = await self._probe_call(client, target, start)
        except Exception as exc:  # noqa: BLE001 —— 探针永不抛出
            # 失败原因随 ProbeResult.error 结构化返回给 validate --probe，这里只留
            # 排障用的低噪声痕迹（不改 stderr 的默认可见输出）。
            _logger.debug("probe failed for profile %s: %s", target.profile, exc)
            self._merge_usage(client._usage)
            return ProbeResult(profile=target.profile, ok=False, model=target.prof.model,
                               latency_ms=int((time.monotonic() - start) * 1000),
                               error=str(exc), key_env=target.key_env)
        self._merge_usage(client._usage)
        return ProbeResult(profile=target.profile, ok=True, model=model,
                           latency_ms=latency_ms, key_env=target.key_env)

    def _probe_client(self, target: _ProbeTarget) -> "LLMClient":
        """构造把剖面收窄到单把密钥的一次性子客户端（共享连接池与信号量，因此剖面级的
        聚合并发上限仍然生效）。

        @param target 探测目标
        @return 一次性子客户端
        """
        mod = replace(target.prof, api_key_env=target.env, api_key=target.key,
                      api_key_envs=(target.env,), api_keys=(target.key,))
        if target.is_llm:
            client = LLMClient({target.profile: replace(mod, max_output_tokens=1)},
                               {}, self._metrics)
        else:
            client = LLMClient({}, {target.profile: mod}, self._metrics)
        client._http_client = self._http()   # 共享连接池
        client._semaphores = self._semaphores
        return client

    async def _probe_call(self, client: "LLMClient", target: _ProbeTarget,
                          start: float) -> tuple[str, int]:
        """执行一次探测调用。

        @param client 一次性子客户端
        @param target 探测目标
        @param start time.monotonic() 起点（embedding 与截断兜底路径据此算耗时）
        @return (模型名, 耗时 ms)
        """
        if not target.is_llm:
            await client.embed(target.profile, ["ping"])
            return target.prof.model, int((time.monotonic() - start) * 1000)
        prompt = PromptBundle(messages=(
            Message(role="user", parts=(Part(kind="text", text="ping"),)),))
        try:
            resp = await client.complete(target.profile, prompt)
        except OutputTruncatedError:
            # v1.11 P6 修复：max_output_tokens=1 的探针在合规端点上按构造必然终止于输出
            # 上限（anthropic stop_reason="max_tokens"——z.ai 2026-07-23 实测），V11 处置
            # 本会判其为失败；但对探针而言这恰恰**就是**活体证明（鉴权通过、模型有响应、
            # usage 已返回）——spec 3.9.4 的探针语义不变。
            _logger.debug("probe hit the 1-token output cap on profile %s (expected)",
                          target.profile)
            return target.prof.model, int((time.monotonic() - start) * 1000)
        return resp.model, resp.latency_ms

    # -- 快照 -----------------------------------------------------------------

    def _profile_snapshot(self, kind: Literal["llm", "embedding"], name: str,
                          prof: LLMProfile | EmbeddingProfile,
                          ts: float) -> ProfileSnapshot:
        """组装单个剖面的面板行：用量镜像（``self._usage`` 缺失时全零——按**名字**分桶的
        怪癖由 ``kind`` 消歧）+ 密钥三态 + p50 中位数。

        @param kind 剖面种类
        @param name 剖面名
        @param prof 剖面对象
        @param ts 快照时刻（time.monotonic() 口径）
        @return 该剖面的只读快照行
        """
        usage = self._usage.get(name)
        keys, in_flight = self._key_snapshots(
            self._pools.get((kind, name)), prof, ts,
            usage.keys if usage is not None else {})
        window = self._latencies.get((kind, name))
        return ProfileSnapshot(
            name=name, kind=kind, in_flight=in_flight,
            max_concurrency=prof.max_concurrency,
            calls=usage.calls if usage is not None else 0,
            retries=usage.retries if usage is not None else 0,
            prompt_tokens=usage.prompt_tokens if usage is not None else 0,
            completion_tokens=usage.completion_tokens if usage is not None else 0,
            est_cost_usd=usage.est_cost_usd if usage is not None else None,
            p50_latency_ms=int(statistics.median(window)) if window else None,
            keys=keys)

    def _key_snapshots(self, pool: _KeyPool | None,
                       prof: LLMProfile | EmbeddingProfile, ts: float,
                       key_usages: Mapping[str, KeyUsage]
                       ) -> tuple[tuple[KeySnapshot, ...], int]:
        """投影密钥行并汇总在飞请求数。

        @param pool 已材料化的密钥池；None 表示尚未材料化
        @param prof 剖面对象（未材料化时据此推导声明的环境变量名）
        @param ts 快照时刻（time.monotonic() 口径）
        @param key_usages 该剖面的每键用量镜像
        @return (密钥行序列, Σ in_flight)
        """
        if pool is None:
            # 纯读：按池构造器**会**使用的成员列表推导，且不材料化 self._pools（spec 3.9.2）。
            return tuple(KeySnapshot(env=env, state="ok")
                         for env, _key in _pool_members(prof)), 0
        return (tuple(_key_snapshot(s, ts, key_usages) for s in pool.states),
                sum(s.in_flight for s in pool.states))

    # -- 运行时基础设施 --------------------------------------------------------

    def _pool(self, kind: str, prof: LLMProfile | EmbeddingProfile) -> _KeyPool:
        """按需材料化该 (kind, 剖面) 的密钥池。

        @param kind 剖面种类（"llm" / "embedding"）
        @param prof 剖面对象
        @return 该剖面的密钥池
        """
        key = (kind, prof.name)
        pool = self._pools.get(key)
        if pool is None:
            pool = _KeyPool(_pool_members(prof))
            self._pools[key] = pool
            if pool.size > 1:
                # 每个成员预置一条 KeyUsage：report.llm_usage 必须列出池化剖面的**每一把**
                # 密钥（未用到的为零），且 keys 子对象的门槛取决于池**大小**而非流量形态
                # ——在飞最少的选键策略会让串行流量始终命中 0 号键，那不该让一个池看起来
                # 像单键剖面（§9.3，评审修复）。
                acc = self._usage.setdefault(prof.name, ProfileUsage())
                for s in pool.states:
                    acc.keys.setdefault(s.env, KeyUsage())
        return pool

    def _max_park_s(self) -> float:
        """读取 run.max_park_s（v1.6，spec 5.2）。

        @return 配置值；直接构造的客户端（测试、探针子客户端）回落到 3600
        """
        cfg = getattr(self._metrics, "cfg", None)
        run = getattr(cfg, "run", None)
        return float(getattr(run, "max_park_s", 3600))

    def _semaphore(self, kind: str, name: str, max_concurrency: int) -> asyncio.Semaphore:
        """按 (kind, name) 取或建剖面限流器。

        @param kind 剖面种类
        @param name 剖面名
        @param max_concurrency 剖面声明的并发上限
        @return 该剖面共享的信号量
        """
        key = (kind, name)
        sem = self._semaphores.get(key)
        if sem is None:
            sem = asyncio.Semaphore(max_concurrency)
            self._semaphores[key] = sem
        return sem

    def _http(self) -> httpx.AsyncClient:
        """@return 共享的 httpx 客户端（首次使用时建立；超时按调用传入）。"""
        if self._http_client is None:
            self._http_client = httpx.AsyncClient(timeout=None)
        return self._http_client

    def _check_breaker(self) -> None:
        """熔断快速失败检查（spec 3.9.2）。

        @raises CircuitBreakerTripped 熔断器已开
        """
        if self._metrics is not None and getattr(self._metrics, "circuit_broken", False):
            raise CircuitBreakerTripped("provider circuit breaker is open")

    def _full_content_trace_enabled(self) -> bool:
        """@return trace 已开启、content="full" 且 llm 通道在册时为 True。"""
        cfg = getattr(self._metrics, "cfg", None)
        trace = getattr(cfg, "trace", None)
        if trace is None:
            return False
        return (getattr(trace, "enabled", False)
                and getattr(trace, "content", "") == "full"
                and "llm" in (getattr(trace, "channels", ()) or ()))

    def _record_provider_result(self, fatal: bool, *, hard: bool = False) -> None:
        """向观测汇喂一次 provider 结果（熔断连击计数）。

        @param fatal True = 致命结局（计入连击），False = 成功（清零连击）
        @param hard True = 立即硬熔断（仅 401/403 打光最后一把活键）
        """
        if self._metrics is None:
            return
        record = getattr(self._metrics, "record_provider_result", None)
        if record is None:
            return
        record(fatal=fatal, hard=hard)

    def _emit_llm_call(self, prof: LLMProfile | EmbeddingProfile,
                       outcome: _CallOutcome, *, operation: str | None = None) -> None:
        """发出 llm.call trace 事件（载荷键与时序为冻结面，CONTRACTS §8.2）。

        @param prof 本次调用的剖面
        @param outcome 本次调用的结局（耗时 / 用量 / 重试数 / 状态 / 附加载荷）
        @param operation 事件的 operation 字段；None 时不写该键（embedding 调用会写）
        """
        if self._metrics is None:
            return
        payload: dict = {
            "profile": prof.name,
            "gen_ai.request.model": prof.model,
            "latency_ms": outcome.latency_ms,
            "gen_ai.usage.input_tokens": outcome.usage.prompt_tokens,
            "gen_ai.usage.output_tokens": outcome.usage.completion_tokens,
            "retries": outcome.retries,
            "status": outcome.status,
        }
        if operation is not None:
            payload["operation"] = operation
        if outcome.extra:
            payload.update(outcome.extra)
        emit = getattr(self._metrics, "event", None)
        if emit is not None:
            emit(EV_LLM_CALL, stage="llm", batch_no=0, record_ids=(), payload=payload)

    def _merge_usage(self, other: dict[str, ProfileUsage]) -> None:
        """把子客户端（探针）的用量并回本客户端。

        @param other 子客户端的按剖面用量表
        """
        for name, src in other.items():
            acc = self._usage.setdefault(name, ProfileUsage())
            acc.calls += src.calls
            acc.prompt_tokens += src.prompt_tokens
            acc.completion_tokens += src.completion_tokens
            acc.retries += src.retries
            if src.est_cost_usd is not None:
                acc.est_cost_usd = (acc.est_cost_usd or 0.0) + src.est_cost_usd
            acc.parked_calls += src.parked_calls
            acc.parked_ms += src.parked_ms
            for env, ku in src.keys.items():
                dst = acc.keys.setdefault(env, KeyUsage())
                dst.calls += ku.calls
                dst.rate_limited += ku.rate_limited
                dst.disabled = dst.disabled or ku.disabled

    def _emit_event(self, ev: str, payload: dict) -> None:
        """发出一条 v1.6 密钥池事件（llm.key_cooldown / llm.key_disabled /
        llm.pool_parked）。载荷只带环境变量**名**——密钥值绝不进入任何日志路径
        （spec 7.2/7.4）。

        @param ev 事件名
        @param payload 事件载荷
        """
        if self._metrics is None:
            return
        emit = getattr(self._metrics, "event", None)
        if emit is not None:
            emit(ev, stage="llm", batch_no=0, record_ids=(), payload=payload)

    # -- 重试引擎（v1.6 密钥池，spec 3.9.3 密钥池行） --------------------------
    #
    # 每次尝试都用"在飞最少的可用密钥"重建请求头。429 冷却**那把**密钥（带 Retry-After
    # 就全额遵从，否则按该键跨调用的连续 429 计数做全抖动、封顶 300 s），下一次尝试立即
    # 轮换——只要还有活键就零等待。401/403 禁用该键：还有兄弟键时静默吸收（不消耗重试、
    # 不喂熔断）；打光最后一把活键则硬熔断，池大小为 1 时逐字节保持 v1.5 语义。全部活键
    # 都在冷却 ⇒ 在已持有的信号量槽位内驻留，按 ≤60 s 分片、每片复检熔断，单次逻辑调用
    # 的总驻留受 run.max_park_s 约束——超限走重试耗尽路径（P1-1）。400/404 与解析致命
    # 都与密钥无关：不轮换、立即致命，与 v1.5 完全一致。

    async def _post_with_retries(self, spec: _CallSpec) -> tuple[tuple, int, int]:
        """共享重试引擎：信号量 → 逐尝试选键 → 尝试环 → 计量钩子 + 每种结局的 llm.call。

        @param spec 本次逻辑调用的请求装配契约
        @return (parse 结果元组, 末次尝试耗时 ms, 已消耗重试次数)
        @raises CircuitBreakerTripped 熔断器在尝试间被打开
        @raises ProviderFatalError 打光最后一把活键、400/404、或 parse 判定的致命响应
        @raises ProviderRetryableError 重试耗尽或驻留超 run.max_park_s
        @raises ContextOverflowError 预算开启下 400 错误体嗅探命中（reactive）
        """
        prof = spec.prof
        ctx = _RetryContext(spec=spec, pool=self._pool(spec.kind, prof),
                            acc=self._usage.setdefault(prof.name, ProfileUsage()),
                            park_budget=self._max_park_s())
        async with self._semaphore(spec.kind, prof.name, prof.max_concurrency):
            while True:
                self._guard_breaker(ctx)
                ks = await self._select_key(ctx)
                if ks is None:
                    continue                     # 驻留结束，重新选键
                ctx.last_env = ks.env
                ku = ctx.acc.keys.setdefault(ks.env, KeyUsage())
                failure = await self._dispatch_attempt(ctx, ks, ku)
                if failure is None:
                    return ctx.result, ctx.latency_ms, ctx.retries_used
                if self._absorb_auth_failure(ctx, ks, ku, failure):
                    continue                     # 401/403：同一次尝试改在下一把键上重发
                self._settle_non_retryable(ctx, ks, failure)
                self._apply_429_cooldown(ctx, ks, ku, failure)
                self._guard_retry_budget(ctx, ks, failure)
                ctx.attempt += 1
                ctx.retries_used += 1
                if failure.status_code == 429:
                    continue                     # 429 的等待落在密钥冷却上，不内联 sleep
                await asyncio.sleep(_backoff_delay(ctx.attempt, prof.retry_base_delay_s,
                                                   self._jitter_rng))

    def _guard_breaker(self, ctx: _RetryContext) -> None:
        """每次尝试前、**取得信号量之后**复检熔断：gather() 下每个排队协程都在任何 HTTP
        完成之前通过了 complete() 的入口检查，没有这道复检，排在"打开熔断那次调用"后面的
        请求仍会发出注定失败的请求（快速失败契约，spec 3.9.2）。

        @param ctx 本次逻辑调用的可变状态
        @raises CircuitBreakerTripped 熔断器已开
        """
        try:
            self._check_breaker()
        except CircuitBreakerTripped:
            if ctx.retries_used:
                # 该逻辑调用在熔断于退避期间打开**之前**确实上过线——它的尝试不能从
                # report.llm_usage 与 llm.call trace 里凭空消失（评审发现）。
                ctx.acc.retries += ctx.retries_used
                outcome = _CallOutcome(latency_ms=ctx.latency_ms, usage=Usage(),
                                       retries=ctx.retries_used,
                                       status="breaker_aborted", extra=ctx.key_extra())
                self._emit_llm_call(ctx.prof, outcome, operation=ctx.spec.operation)
            raise

    async def _select_key(self, ctx: _RetryContext) -> _KeyState | None:
        """选出本次尝试使用的密钥；全部活键都在冷却时先驻留。

        @param ctx 本次逻辑调用的可变状态
        @return 选中的密钥状态；已驻留、需重新选键时为 None
        @raises ProviderFatalError 整池鉴权已死
        @raises ProviderRetryableError 驻留超 run.max_park_s
        """
        now = time.monotonic()
        ks = ctx.pool.select(now)
        if ks is not None:
            return ks
        live = ctx.pool.live()
        if not live:
            # 整池鉴权已死。打光最后一把键的那次调用早已硬熔断，因此入口检查与逐尝试复检
            # 通常先一步拦下——此处是防御性的终态致命（计入连击）。
            self._settle_terminal(ctx, "fatal")
            raise ProviderFatalError("all keys of the pool are auth-disabled",
                                     profile=ctx.prof.name, key_env=ctx.last_env)
        wait = ctx.pool.earliest_wake(now)
        if ctx.park_budget <= 0 or ctx.park_spent + wait > ctx.park_budget:
            # 驻留超限——含"注定无望"的情形（最早的冷却结束时刻已超出剩余预算：立即失败，
            # 不空耗墙钟）与 max_park_s = 0（不驻留）。走重试耗尽路径：记录 failed，计入
            # 熔断窗口（spec 3.9.3，1.6 决策③）。
            self._settle_terminal(ctx, "retryable_exhausted")
            raise ProviderRetryableError(
                f"park budget exhausted ({ctx.park_spent:.0f}s parked, next "
                f"key eligible in {wait:.0f}s, run.max_park_s="
                f"{ctx.park_budget:.0f}): all live keys cooling",
                profile=ctx.prof.name, retries=ctx.retries_used, key_env=ctx.last_env)
        await self._park(ctx, wait, len(live), now)
        return None

    async def _park(self, ctx: _RetryContext, wait: float, live_keys: int,
                    started: float) -> None:
        """在已持有的信号量槽位内驻留到最早一把活键退出冷却。

        @param ctx 本次逻辑调用的可变状态（parked_calls / parked_ms 在此累加）
        @param wait 需等待的秒数
        @param live_keys 当前活键数（写入 llm.pool_parked 事件）
        @param started 驻留起点（time.monotonic() 口径）
        """
        if not ctx.parked:
            ctx.parked = True
            ctx.acc.parked_calls += 1
        self._emit_event(EV_LLM_POOL_PARKED,
                         {"profile": ctx.prof.name, "wait_s": round(wait, 3),
                          "live_keys": live_keys})
        end = started + wait
        while True:
            # 每 ≤60 s 分片复检熔断（保留 v1.5 的加固）：熔断打开即跳出，随后由环顶抛出
            # CircuitBreakerTripped 并做 breaker_aborted 记账。
            if self._metrics is not None and getattr(
                    self._metrics, "circuit_broken", False):
                break
            remaining = end - time.monotonic()
            if remaining <= 0:
                break
            await asyncio.sleep(min(_PARK_SLICE_S, remaining))
        elapsed = time.monotonic() - started
        ctx.park_spent += elapsed
        ctx.acc.parked_ms += int(elapsed * 1000)

    async def _dispatch_attempt(self, ctx: _RetryContext, ks: _KeyState,
                                ku: KeyUsage) -> _AttemptFailure | None:
        """发起一次 HTTP 尝试，并在成功时就地完成成功记账。

        @param ctx 本次逻辑调用的可变状态（latency_ms / result 在此写回）
        @param ks 本次尝试选中的密钥状态
        @param ku 该密钥的用量累加器
        @return None 表示成功（结果已写入 ctx.result）；否则为尝试级失败描述
        @raises ProviderFatalError parse() 判定的致命响应（终态记账后原样上抛）
        """
        headers = _build_headers(ctx.prof.provider, ks.key)
        # 图像字节在此、逐尝试加载，并在请求结束时随 `body` 一同释放（惰性加载契约，spec §2.6）。
        body = ctx.spec.build_body()
        start = time.monotonic()
        ks.in_flight += 1
        try:
            resp = await self._http().post(
                ctx.spec.url, json=body, headers=headers,
                timeout=httpx.Timeout(ctx.prof.timeout_s))
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            ctx.latency_ms = int((time.monotonic() - start) * 1000)
            _logger.warning("provider transport failure on profile %s: %s",
                            ctx.prof.name, type(exc).__name__)
            return _AttemptFailure(message=f"{type(exc).__name__}: {exc}")
        else:
            ctx.latency_ms = int((time.monotonic() - start) * 1000)
            if 200 <= resp.status_code < 300:
                return self._settle_2xx(ctx, ks, ku, resp)
            return _classify_http_failure(resp)
        finally:
            ks.in_flight -= 1
            del body   # 释放请求字节（含 base64 图像）

    def _settle_2xx(self, ctx: _RetryContext, ks: _KeyState, ku: KeyUsage,
                    resp: httpx.Response) -> _AttemptFailure | None:
        """处置 2xx 响应：JSON 解析 → parse() → 成功记账。

        @param ctx 本次逻辑调用的可变状态
        @param ks 本次尝试选中的密钥状态
        @param ku 该密钥的用量累加器
        @param resp 已收到的 2xx 响应
        @return None 表示成功；否则为（可重试的）失败描述
        @raises ProviderFatalError parse() 判定的致命响应（终态记账后原样上抛）
        """
        try:
            data = resp.json()
        except (ValueError, json.JSONDecodeError):
            _logger.warning("provider %s returned unparseable JSON", ctx.prof.name)
            return _AttemptFailure(message="provider returned unparseable JSON")
        try:
            result = ctx.spec.parse(data)
        except ProviderFatalError:
            self._settle_terminal(ctx, "fatal")
            raise
        except Exception as exc:  # noqa: BLE001 —— 形状意外的 2xx
            # JSON 能解析、但形状意外的 2xx，与响应体不可解析同属一类 provider 故障：
            # 可重试，且绝不允许未分类地逃出 M9（spec 3.9.2 / §7.8）。
            _logger.warning("malformed provider response from profile %s: %s",
                            ctx.prof.name, type(exc).__name__)
            return _AttemptFailure(
                message=f"malformed provider response: {type(exc).__name__}: {exc}")
        self._record_success(ctx, ks, ku, result)
        return None

    def _record_success(self, ctx: _RetryContext, ks: _KeyState, ku: KeyUsage,
                        result: tuple) -> None:
        """成功记账：该键连续 429 计数清零、喂熔断成功、发 status="ok" 的 llm.call、喂
        v1.10 的 p50 窗口（spec 3.9.3 快照行的唯一采集点），最后把结果写回 ctx。

        @param ctx 本次逻辑调用的可变状态
        @param ks 本次尝试选中的密钥状态
        @param ku 该密钥的用量累加器
        @param result parse() 的结果元组
        """
        usage = _result_usage(result)
        extra: Mapping | None = ctx.key_extra()
        if ctx.spec.finalize_extra is not None:
            extra = dict(extra or {})
            extra.update(ctx.spec.finalize_extra(result))
        ks.consec_429 = 0     # 该键上的成功清零 c
        ku.calls += 1
        self._record_provider_result(fatal=False)
        outcome = _CallOutcome(latency_ms=ctx.latency_ms, usage=usage,
                               retries=ctx.retries_used, status="ok", extra=extra)
        self._emit_llm_call(ctx.prof, outcome, operation=ctx.spec.operation)
        self._latencies.setdefault((ctx.spec.kind, ctx.prof.name),
                                   deque(maxlen=256)).append(ctx.latency_ms)
        ctx.result = result

    def _absorb_auth_failure(self, ctx: _RetryContext, ks: _KeyState, ku: KeyUsage,
                             failure: _AttemptFailure) -> bool:
        """处置 401/403：鉴权是**密钥级**的确定性失败（v1.6），本次运行内禁用该键。并发在飞
        的调用可能同时观察到同一把键的 401——事件与 WARN 每键每运行至多发一次
        （spec 7.2 基数约束），但每个观察者仍会各自轮换或硬熔断。

        @param ctx 本次逻辑调用的可变状态
        @param ks 本次尝试选中的密钥状态
        @param ku 该密钥的用量累加器
        @param failure 本次尝试的失败描述
        @return True = 已吸收，应轮换到下一把键；False = 非鉴权失败，交由后续分支处置
        @raises ProviderFatalError 被禁用的是最后一把活键（立即硬熔断）
        """
        if failure.status_code not in (401, 403):
            return False
        if not ks.disabled:
            ks.disabled = True
            ku.disabled = True
            self._emit_event(EV_LLM_KEY_DISABLED,
                             {"profile": ctx.prof.name, "key_env": ks.env,
                              "status_code": failure.status_code})
        if ctx.pool.live():
            # 吸收：**同一次**尝试改在下一把键上重发——不消耗重试预算、不喂熔断（每把键
            # 至多鉴权失败一次，因此轮换次数被池大小所界）。
            return True
        # 打光最后一把活键 → v1.5 的鉴权语义：立即硬熔断（凭据不会自愈，spec 3.9.3）。
        self._settle_terminal(ctx, "fatal", hard=True)
        raise ProviderFatalError(failure.message, profile=ctx.prof.name,
                                 status_code=failure.status_code, key_env=ks.env)

    def _settle_non_retryable(self, ctx: _RetryContext, ks: _KeyState,
                              failure: _AttemptFailure) -> None:
        """处置不可重试的状态码（400/404）；可重试的失败原样返回由调用方继续。

        @param ctx 本次逻辑调用的可变状态
        @param ks 本次尝试选中的密钥状态
        @param failure 本次尝试的失败描述
        @raises ContextOverflowError 预算开启下 400 错误体嗅探命中（reactive，不喂熔断）
        @raises ProviderFatalError 其余不可重试状态码（计入连击）
        """
        if failure.retryable:
            return
        if _sniff_overflow_400(ctx.prof.context_window, failure.status_code,
                               failure.body_text):
            # v1.11（V20/V24，预算门控）：**完整**错误体命中溢出 pattern 集的 400 改抛
            # reactive 的 ContextOverflowError 而非 ProviderFatalError。按 SPEC §3.5
            # 「M9 抛出时不喂」：此处**跳过**熔断喂点——由**属主算子**在其有界降级重试耗尽
            # 后补喂恰一次（A7）。llm.call 事件仍发 status="fatal"：它确实是一次
            # provider-fatal 形态的 HTTP 交互，该事件纯观测、不驱动任何熔断逻辑，冻结的
            # status 词表也不必为此新增取值。
            self._settle_terminal(ctx, "fatal", feed_breaker=False)
            # origin="http_400"（SPEC §3.5）：**这个**形态的降级耗尽终局才由属主算子补喂
            # 熔断恰一次（A7）。
            raise ContextOverflowError(
                f"provider context overflow (400 body sniff): {failure.message}",
                phase="reactive", profile=ctx.prof.name, origin="http_400")
        # 400/404：请求形状类错误与密钥无关——不轮换、立即致命并计入连击（spec 3.9.3）。
        self._settle_terminal(ctx, "fatal")
        raise ProviderFatalError(failure.message, profile=ctx.prof.name,
                                 status_code=failure.status_code, key_env=ks.env)

    def _apply_429_cooldown(self, ctx: _RetryContext, ks: _KeyState, ku: KeyUsage,
                            failure: _AttemptFailure) -> None:
        """v1.6：**一切** 429 等待都表达为每键冷却——带 Retry-After 就全额遵从，否则按该键
        跨调用的连续 429 计数做全抖动、封顶 300 s。

        @param ctx 本次逻辑调用的可变状态
        @param ks 本次尝试选中的密钥状态
        @param ku 该密钥的用量累加器
        @param failure 本次尝试的失败描述（非 429 时直接返回）
        """
        if failure.status_code != 429:
            return
        ks.consec_429 += 1
        ku.rate_limited += 1
        cooldown = (failure.retry_after if failure.retry_after is not None
                    else self._jitter_rng.uniform(
                        0.0, _key_cooldown_upper(ctx.prof.retry_base_delay_s,
                                                 ks.consec_429)))
        ks.cooldown_until = time.monotonic() + cooldown
        self._emit_event(EV_LLM_KEY_COOLDOWN,
                         {"profile": ctx.prof.name, "key_env": ks.env,
                          "cooldown_s": round(cooldown, 3),
                          "retry_after": failure.retry_after is not None})

    def _guard_retry_budget(self, ctx: _RetryContext, ks: _KeyState,
                            failure: _AttemptFailure) -> None:
        """重试预算检查；耗尽同样计入熔断窗口
        （spec 7.6 provider_retryable_exhausted：「计入熔断窗口」）。

        @param ctx 本次逻辑调用的可变状态
        @param ks 本次尝试选中的密钥状态
        @param failure 本次尝试的失败描述
        @raises ProviderRetryableError 重试已耗尽
        """
        if ctx.attempt < ctx.prof.max_retries:
            return
        self._settle_terminal(ctx, "retryable_exhausted")
        raise ProviderRetryableError(
            f"retries exhausted ({ctx.retries_used}): {failure.message}",
            profile=ctx.prof.name, retries=ctx.retries_used, key_env=ks.env)

    def _settle_terminal(self, ctx: _RetryContext, status: str, *,
                         feed_breaker: bool = True, hard: bool = False) -> None:
        """终态记账：熔断计数 → retries 汇总 → llm.call 事件（三步顺序为冻结面）。

        @param ctx 本次逻辑调用的可变状态
        @param status llm.call 的 status 取值（fatal / retryable_exhausted）
        @param feed_breaker 是否喂熔断连击；reactive-400 由属主算子补喂，此处传 False（A7）
        @param hard 是否立即硬熔断（仅 401/403 打光最后一把活键）
        """
        if feed_breaker:
            self._record_provider_result(fatal=True, hard=hard)
        if ctx.retries_used:
            ctx.acc.retries += ctx.retries_used
        outcome = _CallOutcome(latency_ms=ctx.latency_ms, usage=Usage(),
                               retries=ctx.retries_used, status=status,
                               extra=ctx.key_extra())
        self._emit_llm_call(ctx.prof, outcome, operation=ctx.spec.operation)


def _split_embed(data: Mapping, n: int, prof: EmbeddingProfile) -> tuple:
    """适配器：让重试引擎能用统一形状为 embedding 计量。

    @param data 已解析的响应 JSON
    @param n 请求的文本条数
    @param prof 目标 embedding 剖面
    @return 单元素元组，内含 (向量列表, 用量)
    @raises ProviderFatalError 条数不符或维数不符
    """
    vectors, usage = _parse_embeddings_response(data, n, prof.name, prof.dims)
    return ((vectors, usage),)


def _result_usage(result: tuple) -> Usage:
    """从 parse() 结果元组里取出 Usage。

    @param result complete 是五元组（usage 在索引 2——v1.11 F9：追加 finish 把形状从 4
                  拓到 5）；embed 是内含 (vectors, usage) 的单元素元组
    @return 本次调用的用量
    """
    if len(result) == 5:
        return result[2]
    inner = result[0]
    return inner[1]
