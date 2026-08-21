"""M1 的两份 TOML 逐节解析(spec 5.1 config.toml / 5.2 project.toml)。

本模块只做"读一节 → 物化一个配置对象"这件事: 类型、枚举、区间等**节内**约束在此
落地, 跨节与形态相关的组合约束一律留给 ``_constraints`` 模块——这样报错次序才能与
spec 3.1.4 的表行次序对齐。
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from decimal import Decimal, InvalidOperation
from pathlib import Path
import math
import re
from typing import Any, Literal

from labelkit.common.config._collect import (
    _MISSING,
    _GE0,
    _GT0,
    _KEY_RE,
    _UNIT_CLOSED,
    _UNIT_HALF_OPEN,
    _Collector,
    _check_schema_version,
    _fmt,
    _int_pair,
    _num_pair,
    _section,
    _Tbl,
)
from labelkit.common.config._rubrics import _RUBRIC_SELECTORS
from labelkit.common.config.model import (
    AnnotateConfig,
    ClassifyConfig,
    ClassSpec,
    ConsoleConfig,
    DedupConfig,
    EmbeddingProfile,
    ExtractConfig,
    FewShotExample,
    FrameAnnotateConfig,
    FrameClassifyConfig,
    GenerateConfig,
    GenerateStreamConfig,
    GenerateStyle,
    InputConfig,
    LLMProfile,
    OutputConfig,
    QualityConfig,
    SegmentConfig,
    StitchConfig,
    StreamConfig,
    TierSpec,
    ToolConfig,
    TraceConfig,
    VerifyConfig,
)
from labelkit.common.runtime import budget

# v1.9: 11 个取值——"stitch" 加入(T16; 通道 = 阶段名, S1: 事件名首段自动路由,
# 沿用 classify 先例)。
_TRACE_CHANNELS = ("ingest", "dedup", "segment", "stitch", "extract", "classify",
                   "quality", "annotate", "verify", "schema", "llm")

# v1.13 时间流形态的定向禁设键(v1.11 use_vision 原始节探针同款机制): 这四个键属于
# generate 的**另外两种形态**(种子池 / 独立计数 / 逐记录扩增 / 每调用种子数), 在时间流
# 形态下显式书写是 CONFIG_ERROR, 而非白名单外键的前向兼容 WARN。
_STREAM_FORBIDDEN_GEN_KEYS = ("seed_examples", "standalone_count",
                              "num_per_record", "seeds_per_call")
# 按类生成节的同族禁设键(num_per_record 在本形态从白名单语义中除名)。
_STREAM_FORBIDDEN_CLASS_GEN_KEYS = ("num_per_record", "seeds_per_call")

# v1.17 删除键(CONTRACTS §6.3 rule 62): 定向 CONFIG_ERROR 只指向新表达, 不读取旧值、
# 不转换。解析层无条件报错——这些键已从配置语言整体移除, 与形态开关无关。
_DELETED_STREAM_KEYS = {
    "sessions": "[generate.stream].crossed_sessions (total sessions are derived)",
    "ts_start": "[generate.stream].schedule.start / schedule.end",
    "noise_instruction": "[[generate.stream.noise]] (per-noise-class table)",
    "rules": "[[generate.stream.frame_rules]]",
    "windows": "[[generate.stream.frame_windows]]",
}

_FRAME_TEMPLATES = (
    "existence", "absence", "exactly", "init", "end", "responded_existence",
    "co_existence", "response", "precedence", "succession", "alternate_response",
    "chain_response", "chain_precedence", "not_co_existence", "not_succession",
    "contains",
)
_SEQUENCE_WEEKDAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")


@dataclass(frozen=True)
class _ToolSide:
    """config.toml 的解析产物捆包(工具级、部署静态的一侧)。"""

    tool: ToolConfig                                  # [tool] 日志级别与格式
    console: ConsoleConfig                            # [console] 三模式控制台节
    console_rich_explicit: bool                       # 原始表里是否显式写了 mode = "rich"
    llm_profiles: dict[str, LLMProfile]               # [llm.<name>] profile 表
    embedding_profiles: dict[str, EmbeddingProfile]   # [embedding.<name>] profile 表


@dataclass(frozen=True)
class _Project:
    """project.toml 的解析产物捆包(逐次运行的一侧)。"""

    run: dict[str, Any]                  # [run] 原始取值(输入/输出/模态/模式/批量……)
    input: InputConfig                   # [input] 摄取参数
    stream: StreamConfig                 # [stream] 时间序与会话切分声明
    dedup: DedupConfig                   # [dedup] 去重参数
    segment: SegmentConfig               # [segment] 流分段(vision_resolved 待冻结)
    stitch: StitchConfig                 # [stitch] 线索缝合
    extract: ExtractConfig               # [extract] 动作摘取
    classify: ClassifyConfig             # [classify] 序列级闭集分类
    classify_provided: dict[str, bool]   # [classify] 的显式书写探针(classes/max_labels)
    class_raw: Any                       # [class.<name>.*] 原始节(白名单校验留给后续)
    frame_classify: FrameClassifyConfig  # [frame.classify] 帧级分类
    frame_annotate: FrameAnnotateConfig  # [frame.annotate] 帧级标注
    frame_class_raw: Any                 # [frame.class.<name>.*] 原始节
    frame_provided: dict[str, bool]      # [frame] 命名空间的显式书写探针
    stream_provided: dict[str, bool]     # [stream] 的在场/显式书写探针
    segment_provided: dict[str, bool]    # [segment] 的在场/显式书写探针(含 use_vision)
    stitch_provided: dict[str, bool]     # [stitch] 的在场探针
    extract_provided: dict[str, bool]    # [extract] 的在场探针
    sequence_frames_provided: bool       # [annotate].sequence_frames 是否显式书写
    quality: QualityConfig               # [quality] 质量打分
    generate: GenerateConfig             # [generate] 生成扩增
    generate_stream: GenerateStreamConfig  # [generate.stream] 时间流生成形态(v1.13)
    gen_provided: dict[str, bool]        # [generate] 的另一形态禁设键探针
    top_ratio_provided: bool             # [quality].top_ratio 是否显式书写
    annotate: AnnotateConfig             # [annotate] 自动标注
    verify: VerifyConfig                 # [verify] LLM 复核
    output: OutputConfig                 # [output] 输出结构与 Schema 源
    trace: TraceConfig                   # [trace] 追踪通道
    rubric_raw: Any                      # [rubric] 原始节(按 3.1.4 次序延迟解析)


# ── config.toml 一侧 ────────────────────────────────────────────────────────


def _parse_tool(col: _Collector, file: str, data: Any) -> ToolConfig:
    """解析 ``[tool]`` 节(日志级别与格式)。

    @param col 错误聚合器
    @param file 报错定位用的 config.toml 路径字符串
    @param data 该节原始值
    @return ``ToolConfig``
    """
    t = _Tbl(col, file, "[tool]", data)
    tool = ToolConfig(
        log_level=t.get_str("log_level", "info", enum=("debug", "info", "warn", "error")),
        log_format=t.get_str("log_format", "text", enum=("text", "jsonl")),
    )
    t.finish()
    return tool


def _parse_console(col: _Collector, file: str, data: Any) -> ConsoleConfig:
    """v1.10(spec 5.1 [console] / 3.1.4 console 行): 工具级三模式控制台节, 整节可选。

    mode 枚举、refresh_hz ∈ [1, 10]、heartbeat_s ≥ 0——违规一律**聚合**为
    CONFIG_ERROR(绝不首错即抛); 节内未知键仍走前向兼容警告(标准 finish())。
    ``mode_resolved`` 在此保持 dataclass 默认值——真正的裁定由 load() 收尾冻结(U21)。

    @param col 错误聚合器
    @param file 报错定位用的 config.toml 路径字符串
    @param data 该节原始值
    @return ``ConsoleConfig``
    """
    t = _Tbl(col, file, "[console]", data)
    mode = t.get_str("mode", "auto", enum=("auto", "rich", "plain"))
    refresh_hz = t.get_int("refresh_hz", 5)
    if not 1 <= refresh_hz <= 10:
        col.error(f"{file}:[console].refresh_hz: expected an integer in [1, 10] "
                  f"(rich canvas repaint rate), got {refresh_hz}")
        refresh_hz = 5
    console = ConsoleConfig(
        mode=mode,
        refresh_hz=refresh_hz,
        heartbeat_s=t.get_int("heartbeat_s", 0, minimum=0),
        estimate=t.get_bool("estimate", False),
        interactive=t.get_bool("interactive", True),
    )
    t.finish()
    return console


def _auto_console_mode(*, isatty: bool, log_format: str, term: str | None,
                       rich_importable: bool) -> Literal["rich", "plain"]:
    """v1.10 auto 决策链(spec §7.7 / 3.1.4 console 行, U5/U25)。

    rich 当且仅当 ``stderr.isatty()`` ∧ ``tool.log_format == "text"`` ∧ TERM 非
    "dumb"/空 ∧ rich 可导入(find_spec 探针)。NO_COLOR 不参与(U25——rich 原生剥色但
    保留版式); TERM 是终端能力探针而非配置通道(§2.6 无环境变量规则不受影响)。
    纯函数化(探针值全部注入), 每条分支都可离线测试。

    @param isatty stderr 是否为 TTY
    @param log_format 工具级日志格式
    @param term TERM 环境变量值
    @param rich_importable rich 是否可导入
    @return "rich" 或 "plain"
    """
    if (isatty and log_format == "text" and term not in ("", "dumb", None)
            and rich_importable):
        return "rich"
    return "plain"


def _parse_key_envs(col: _Collector, t: _Tbl, data: dict) -> tuple[str, ...]:
    """v1.6 密钥池(spec 3.1.4 API-Key 行 / 5.1): ``api_key_env`` 与 ``api_key_envs`` 恰一。

    两种形态都归一为"互异且非空的环境变量名元组"(标量 → 单元素元组)。

    @param col 错误聚合器
    @param t 该 profile 表的读取器
    @param data 该 profile 的原始表
    @return 环境变量名元组; 声明非法时返回空元组(错误已聚合)
    """
    has_single = "api_key_env" in data
    has_multi = "api_key_envs" in data
    # 两个键都要消费掉, 否则 finish() 会把它们当未知键。
    single = t.get_str("api_key_env", None, nonempty=True)
    multi = t.get_str_tuple("api_key_envs", ())
    if has_single and has_multi:
        col.error(f"{t.loc('api_key_envs')}: mutually exclusive with api_key_env "
                  f"(exactly one must be provided, v1.6)")
        return ()
    if not has_single and not has_multi:
        col.error(f"{t.loc('api_key_env')}: missing required key - exactly one of "
                  f"api_key_env / api_key_envs must be provided (v1.6)")
        return ()
    if has_single:
        return (single,) if single else ()
    if not multi:
        raw = data.get("api_key_envs")
        if isinstance(raw, list) and not raw:
            col.error(f"{t.loc('api_key_envs')}: expected a non-empty array of env var "
                      f"names (>= 1 entry)")
        # 非数组 / 元素非法的情形: get_str_tuple 已经逐元素报过错——不再补第二条
        # 误导性错误(评审修正)。
        return ()
    return multi if _check_env_names(col, t, multi) else ()


def _check_env_names(col: _Collector, t: _Tbl, names: tuple[str, ...]) -> bool:
    """校验密钥池内的环境变量名: 逐个非空且池内互异。

    @param col 错误聚合器
    @param t 该 profile 表的读取器
    @param names 已读出的环境变量名元组
    @return 全部合法为 True
    """
    ok = True
    seen: set[str] = set()
    for i, env in enumerate(names, 1):
        if not env.strip():
            col.error(f"{t.loc('api_key_envs')}[{i}]: expected non-empty string, got {_fmt(env)}")
            ok = False
        elif env in seen:
            col.error(f"{t.loc('api_key_envs')}[{i}]: duplicate env var name {_fmt(env)} "
                      f"(names within a pool must be distinct)")
            ok = False
        seen.add(env)
    return ok


def _parse_llm_profile(col: _Collector, file: str, name: str, data: dict) -> LLMProfile:
    """解析一个 ``[llm.<name>]`` profile。

    @param col 错误聚合器
    @param file 报错定位用的 config.toml 路径字符串
    @param name profile 名
    @param data 该 profile 的原始表
    @return ``LLMProfile``
    """
    t = _Tbl(col, file, f"[llm.{name}]", data)
    key_envs = _parse_key_envs(col, t, data)
    prof = LLMProfile(
        name=name,
        provider=t.get_str("provider", "openai_compatible", required=True,
                           enum=("openai_compatible", "anthropic")),
        base_url=t.get_str("base_url", "", required=True, nonempty=True) or "",
        model=t.get_str("model", "", required=True, nonempty=True) or "",
        api_key_env=key_envs[0] if key_envs else "",
        api_key_envs=key_envs,
        max_concurrency=t.get_int("max_concurrency", 8, minimum=1),
        timeout_s=t.get_int("timeout_s", 120, minimum=1),
        max_retries=t.get_int("max_retries", 5, minimum=0),
        retry_base_delay_s=t.get_float("retry_base_delay_s", 1.0, bound=_GT0),
        supports_structured_output=t.get_bool("supports_structured_output", False),
        supports_vision=t.get_bool("supports_vision", False),
        max_output_tokens=t.get_int("max_output_tokens", 4096, minimum=1),
        context_window=t.get_int("context_window", 0, minimum=0),
        temperature=t.get_float("temperature", 0.0, bound=_GE0),
        thinking=t.get_str("thinking", None, enum=("enabled", "disabled")),
        max_image_px=t.get_int("max_image_px", 2048, minimum=1),
        default_image_px=t.get_int("default_image_px", 0, minimum=0),
        price_per_mtok_in=t.get_float("price_per_mtok_in", None, bound=_GE0),
        price_per_mtok_out=t.get_float("price_per_mtok_out", None, bound=_GE0),
    )
    t.finish()
    _check_llm_budget_keys(col, file, prof)
    return prof


def _check_llm_budget_keys(col: _Collector, file: str, prof: LLMProfile) -> None:
    """v1.11 的两条 profile 内约束: 声明窗口须留出正预算, 采样工作点不超上限。

    (V6, spec 3.1.4 上下文预算行; V18。)

    @param col 错误聚合器
    @param file 报错定位用的 config.toml 路径字符串
    @param prof 已物化的 profile
    """
    name = prof.name
    # V6: 声明了窗口就必须留出正的输入预算(0 = 未声明 = 预算关闭, 恒合法)。
    if prof.context_window > 0 and budget.input_budget(prof) <= 0:
        col.error(f"{file}:[llm.{name}].context_window: declared window leaves a "
                  f"non-positive budget - requires context_window > max_output_tokens "
                  f"+ margin (margin = max(256, ceil(0.10 * context_window)) = "
                  f"{budget.margin(prof.context_window)}), got context_window = "
                  f"{prof.context_window}, max_output_tokens = {prof.max_output_tokens}")
    # V18: 采样工作点永不超过上限。
    if prof.default_image_px > 0 and prof.default_image_px > prof.max_image_px:
        col.error(f"{file}:[llm.{name}].default_image_px: expected <= max_image_px "
                  f"({prof.max_image_px}), got {prof.default_image_px}")


def _parse_embedding_profile(col: _Collector, file: str, name: str,
                             data: dict) -> EmbeddingProfile:
    """解析一个 ``[embedding.<name>]`` profile。

    @param col 错误聚合器
    @param file 报错定位用的 config.toml 路径字符串
    @param name profile 名
    @param data 该 profile 的原始表
    @return ``EmbeddingProfile``
    """
    t = _Tbl(col, file, f"[embedding.{name}]", data)
    key_envs = _parse_key_envs(col, t, data)
    prof = EmbeddingProfile(
        name=name,
        provider=t.get_str("provider", "openai_compatible", enum=("openai_compatible",)),
        base_url=t.get_str("base_url", "", required=True, nonempty=True) or "",
        model=t.get_str("model", "", required=True, nonempty=True) or "",
        api_key_env=key_envs[0] if key_envs else "",
        api_key_envs=key_envs,
        max_concurrency=t.get_int("max_concurrency", 8, minimum=1),
        timeout_s=t.get_int("timeout_s", 60, minimum=1),
        max_retries=t.get_int("max_retries", 5, minimum=0),
        retry_base_delay_s=t.get_float("retry_base_delay_s", 1.0, bound=_GT0),
        context_window=t.get_int("context_window", 0, minimum=0),
        dims=t.get_int("dims", None, minimum=1),
    )
    t.finish()
    # v1.11 (V15): 嵌入预算 = cw − margin(不预留输出)——声明了窗口就须为正。
    if prof.context_window > 0 and budget.embed_budget(prof) <= 0:
        col.error(f"{file}:[embedding.{name}].context_window: declared window leaves a "
                  f"non-positive budget - requires context_window > margin "
                  f"(margin = max(256, ceil(0.10 * context_window)) = "
                  f"{budget.margin(prof.context_window)}), got {prof.context_window}")
    return prof


def _parse_config_file(col: _Collector, file: str, data: dict) -> _ToolSide:
    """解析整份 config.toml(工具级、部署静态的一侧)。

    @param col 错误聚合器
    @param file 报错定位用的 config.toml 路径字符串
    @param data 已 TOML 解析的顶层字典
    @return ``_ToolSide``
    """
    top = _Tbl(col, file, "", data)
    _check_schema_version(col, top)
    tool = _parse_tool(col, file, _section(col, top, "tool"))
    # v1.10: [console] 现在是显名拥有的顶层表——在此消费掉, 下面的 finish() 才不会
    # 把它当未知键。显式 rich 探针读的是**原始表**(dataclass 默认值 "auto" 不算
    # 用户意图, U21/§7.7)。
    console_section = _section(col, top, "console")
    console = _parse_console(col, file, console_section)
    console_rich_explicit = (isinstance(console_section, dict)
                             and console_section.get("mode") == "rich")
    llm_profiles = _parse_llm_table(col, file, top)
    embedding_profiles = _parse_embedding_table(col, file, top)
    top.finish()
    return _ToolSide(tool=tool, console=console,
                     console_rich_explicit=console_rich_explicit,
                     llm_profiles=llm_profiles, embedding_profiles=embedding_profiles)


def _parse_llm_table(col: _Collector, file: str, top: _Tbl) -> dict[str, LLMProfile]:
    """解析 ``[llm.*]`` 全表(至少 1 个 profile)。

    @param col 错误聚合器
    @param file 报错定位用的 config.toml 路径字符串
    @param top 顶层表读取器
    @return profile 名 → ``LLMProfile``
    """
    profiles: dict[str, LLMProfile] = {}
    llm_data = top.take("llm")
    if llm_data is _MISSING or not isinstance(llm_data, dict) or not llm_data:
        col.error(f"{file}:llm: at least one [llm.<name>] profile is required")
        return profiles
    for name, sub in llm_data.items():
        if not isinstance(sub, dict):
            col.error(f"{file}:[llm.{name}]: expected table, got {_fmt(sub)}")
            continue
        profiles[name] = _parse_llm_profile(col, file, name, sub)
    return profiles


def _parse_embedding_table(col: _Collector, file: str,
                           top: _Tbl) -> dict[str, EmbeddingProfile]:
    """解析 ``[embedding.*]`` 全表(整表可选)。

    @param col 错误聚合器
    @param file 报错定位用的 config.toml 路径字符串
    @param top 顶层表读取器
    @return profile 名 → ``EmbeddingProfile``
    """
    profiles: dict[str, EmbeddingProfile] = {}
    emb_data = top.take("embedding")
    if emb_data is _MISSING:
        return profiles
    if not isinstance(emb_data, dict):
        col.error(f"{file}:embedding: expected table, got {_fmt(emb_data)}")
        return profiles
    for name, sub in emb_data.items():
        if not isinstance(sub, dict):
            col.error(f"{file}:[embedding.{name}]: expected table, got {_fmt(sub)}")
            continue
        profiles[name] = _parse_embedding_profile(col, file, name, sub)
    return profiles


# ── project.toml 一侧 ───────────────────────────────────────────────────────


def _parse_styles(col: _Collector, file: str, raw: Any,
                  section: str = "generate") -> tuple[GenerateStyle, ...]:
    """解析 ``[[<section>.styles]]`` 表数组。

    ``section`` 平移错误定位以支持 v1.7 的按类 styles 覆盖
    ("class.<name>.generate"); 缺省保持全局 ``[generate]`` 文案。

    @param col 错误聚合器
    @param file 报错定位用的 project.toml 路径字符串
    @param raw 原始表数组
    @param section 所属节名
    @return ``GenerateStyle`` 元组
    """
    if raw is _MISSING:
        return ()
    if not isinstance(raw, list):
        col.error(f"{file}:[{section}].styles: expected array of tables, got {_fmt(raw)}")
        return ()
    styles: list[GenerateStyle] = []
    seen: set[str] = set()
    for i, sub in enumerate(raw, 1):
        label = f"[[{section}.styles]][{i}]"
        if not isinstance(sub, dict):
            col.error(f"{file}:{label}: expected table, got {_fmt(sub)}")
            continue
        t = _Tbl(col, file, label, sub)
        name = t.get_str("name", None, required=True, nonempty=True)
        prompt = t.get_str("prompt", None, required=True, nonempty=True)
        t.finish()
        if name is not None:
            if name in seen:
                col.error(f"{file}:{label}.name: name must be unique within the table, "
                          f"got duplicate {_fmt(name)}")
            seen.add(name)
        if name is not None and prompt is not None:
            styles.append(GenerateStyle(name=name, prompt=prompt))
    return tuple(styles)


def _parse_examples(col: _Collector, file: str, raw: Any,
                    section: str = "annotate") -> tuple[FewShotExample, ...]:
    """解析 ``[[<section>.examples]]`` 表数组(few-shot 示例集)。

    ``section`` 平移错误定位以支持 v1.7 的按类 examples 覆盖
    ("class.<name>.annotate"); 缺省保持全局 ``[annotate]`` 文案。

    @param col 错误聚合器
    @param file 报错定位用的 project.toml 路径字符串
    @param raw 原始表数组
    @param section 所属节名
    @return ``FewShotExample`` 元组
    """
    if raw is _MISSING:
        return ()
    if not isinstance(raw, list):
        col.error(f"{file}:[{section}].examples: expected array of tables, got {_fmt(raw)}")
        return ()
    examples: list[FewShotExample] = []
    for i, sub in enumerate(raw, 1):
        label = f"[[{section}.examples]][{i}]"
        if not isinstance(sub, dict):
            col.error(f"{file}:{label}: expected table, got {_fmt(sub)}")
            continue
        t = _Tbl(col, file, label, sub)
        inp = t.get_str("input", None, required=True, nonempty=True)
        out = t.take("output")
        expected = "table (object, must pass the user schema)"
        if out is _MISSING:
            t.err("output", expected)
            out = None
        elif not isinstance(out, dict):
            t.err("output", expected, out)
            out = None
        t.finish()
        if inp is not None and out is not None:
            examples.append(FewShotExample(input=inp, output=out))
    return tuple(examples)


def _parse_classes(col: _Collector, file: str, raw: Any,
                   section: str = "classify") -> tuple[ClassSpec, ...]:
    """解析 ``[[<section>.classes]]`` 类别表数组(spec 5.2 v1.7)。

    name 匹配 ``[a-z0-9_]+`` 且表内唯一, description 非空, examples 是可选的字符串
    数组(仅输入侧 few-shot 行)。v1.12: ``section`` 平移错误定位——帧类表
    ``[[frame.classify.classes]]`` 与之同构(沿用 _parse_styles/_parse_examples 惯例)。

    @param col 错误聚合器
    @param file 报错定位用的 project.toml 路径字符串
    @param raw 原始表数组
    @param section 所属节名
    @return ``ClassSpec`` 元组
    """
    if raw is _MISSING:
        return ()
    if not isinstance(raw, list):
        col.error(f"{file}:[{section}].classes: expected array of tables, got {_fmt(raw)}")
        return ()
    classes: list[ClassSpec] = []
    seen: set[str] = set()
    for i, sub in enumerate(raw, 1):
        label = f"[[{section}.classes]][{i}]"
        if not isinstance(sub, dict):
            col.error(f"{file}:{label}: expected table, got {_fmt(sub)}")
            continue
        t = _Tbl(col, file, label, sub)
        name = t.get_str("name", None, required=True, nonempty=True)
        if name is not None and not _KEY_RE.fullmatch(name):
            col.error(f"{file}:{label}.name: expected a match of [a-z0-9_]+, got {_fmt(name)}")
            name = None
        description = t.get_str("description", None, required=True, nonempty=True)
        examples = t.get_str_tuple("examples", ())
        t.finish()
        if name is not None:
            if name in seen:
                col.error(f"{file}:{label}.name: name must be unique within the table, "
                          f"got duplicate {_fmt(name)}")
            seen.add(name)
        if name is not None and description is not None:
            classes.append(ClassSpec(name=name, description=description, examples=examples))
    return tuple(classes)


def _parse_tiers(col: _Collector, file: str, raw: Any,
                 header: str) -> tuple[TierSpec, ...]:
    """v1.14: 解析档位表数组(_parse_classes 同款形)。

    只做键级类型校验(三键的类型与下界); 身份连续性、构成互异与名集归属、逐非零配额对
    的长度可覆盖等结构约束留给形态约束簇。产物按 ``tier_rank`` 升序存放——
    ``tiers[rank - 1]`` 直取是 M6 蓝图侧的取档方式。v1.15(裁决·表级原子覆盖): 定位串
    参数化, 同一实现同时服务全局表与 ``[[class.<name>.generate.tiers]]`` 按类表。

    @param col 错误聚合器
    @param file 报错定位用的 project.toml 路径字符串
    @param raw 该档位表键的原始值
    @param header 该表的表数组头, 如 ``"[[generate.stream.tiers]]"``; 键级定位串
                  (整表形状错误用)由它派生为 ``"[<父节>].<键>"``
    @return 按 ``tier_rank`` 升序的 ``TierSpec`` 元组
    """
    if raw is _MISSING:
        return ()
    if not isinstance(raw, list):
        parent, _, key = header.strip("[]").rpartition(".")
        col.error(f"{file}:[{parent}].{key}: expected array of tables, "
                  f"got {_fmt(raw)}")
        return ()
    tiers: list[TierSpec] = []
    for i, sub in enumerate(raw, 1):
        label = f"{header}[{i}]"
        if not isinstance(sub, dict):
            col.error(f"{file}:{label}: expected table, got {_fmt(sub)}")
            continue
        t = _Tbl(col, file, label, sub)
        rank = t.get_int("tier_rank", None, minimum=1)
        weight = t.get_int("weight", None, minimum=1)
        frame_classes = t.get_str_tuple("frame_classes", ())
        for key, value in (("tier_rank", rank), ("weight", weight)):
            if value is None and key not in sub:
                t.err(key, "positive integer")
        t.finish()
        if rank is not None and weight is not None:
            tiers.append(TierSpec(tier_rank=rank, weight=weight,
                                  frame_classes=frame_classes))
    return tuple(sorted(tiers, key=lambda spec: spec.tier_rank))


def _parse_time_us(t: _Tbl, key: str) -> tuple[int, int] | None:
    """解析规则 time_s 的半开秒区间并量化为无损整数微秒 [lo, hi)。"""
    raw = t.take(key)
    if raw is _MISSING:
        return None
    expected = "number range array of length 2 [lo, hi) with microsecond precision"
    if (not isinstance(raw, list) or len(raw) != 2
            or any(isinstance(value, bool) or not isinstance(value, (int, float))
                   for value in raw)):
        t.err(key, expected, raw)
        return None
    try:
        decimals = [Decimal(str(value)) * Decimal(1_000_000) for value in raw]
    except (InvalidOperation, ValueError):
        t.err(key, expected, raw)
        return None
    if any(not value.is_finite() or value != value.to_integral_value() for value in decimals):
        t.err(key, expected, raw)
        return None
    lo_us, hi_us = (int(value) for value in decimals)
    if lo_us < 0 or lo_us >= hi_us:
        t.err(key, "half-open range [lo, hi) with 0 <= lo < hi in microsecond precision",
              raw)
        return None
    return lo_us, hi_us


def _parse_clock_us(col: _Collector, loc: str, value: str) -> int | None:
    """把一个 HH:MM[:SS[.ffffff]] 墙钟字符串转换为日内微秒偏移。"""
    import re
    match = re.fullmatch(r"(\d{2}):(\d{2})(?::(\d{2})(?:\.(\d{1,6}))?)?", value)
    if match is None:
        col.error(f"{loc}: expected HH:MM, HH:MM:SS or microsecond-precision wall-clock time, got {_fmt(value)}")
        return None
    hour, minute = int(match.group(1)), int(match.group(2))
    second = int(match.group(3) or 0)
    micros = int((match.group(4) or "").ljust(6, "0") or 0)
    if hour > 23 or minute > 59 or second > 59:
        col.error(f"{loc}: expected a valid same-day wall-clock time, got {_fmt(value)}")
        return None
    return ((hour * 60 + minute) * 60 + second) * 1_000_000 + micros


    """解析 frame rule 的 typed correlation inline table（v1.17 无 operator 键）。"""
    from labelkit.common.runtime.scenario.model import CorrelationSpec

    raw = t.take(key)
    if raw is _MISSING:
        return None
    loc = f"{t.label}.{key}"
    if not isinstance(raw, dict):
        t.col.error(f"{t.file}:{loc}: expected table, got {_fmt(raw)}")
        return None
    nested = _Tbl(t.col, t.file, loc, raw)
    source_field = nested.get_str("source_field", None, required=True, nonempty=True)
    target_field = nested.get_str("target_field", None, required=True, nonempty=True)
    nested.finish()
    if "operator" in raw:
        t.col.error(f"{t.file}:{loc}.operator: unknown key - v1.17 correlation is "
                    f"equal-only, declare source_field / target_field only")
    if source_field is None or target_field is None:
        return None
    return CorrelationSpec(source_field=source_field, target_field=target_field)


def _parse_natural_name(table: _Tbl, loc: str) -> str | None:
    """解析规则/窗口/quota 行的必填自然名称（[a-z0-9_]+）。"""
    name = table.get_str("name", None, required=True, nonempty=True)
    if name is not None and not _KEY_RE.match(name):
        table.col.error(f"{table.file}:{loc}.name: expected a match of [a-z0-9_]+, "
                        f"got {_fmt(name)}")
        return None
    return name


def _parse_correlation(t: _Tbl, key: str) -> Any | None:
    """解析 typed correlation；v1.17 仅允许 source_field/target_field。"""
    from labelkit.common.runtime.scenario.model import CorrelationSpec
    raw = t.take(key)
    if raw is _MISSING:
        return None
    loc = f"{t.label}.{key}"
    if not isinstance(raw, dict):
        t.col.error(f"{t.file}:{loc}: expected table, got {_fmt(raw)}")
        return None
    nested = _Tbl(t.col, t.file, loc, raw)
    source = nested.get_str("source_field", None, required=True, nonempty=True)
    target = nested.get_str("target_field", None, required=True, nonempty=True)
    nested.finish()
    if "operator" in raw:
        t.col.error(f"{t.file}:{loc}.operator: unknown key - v1.17 correlation is equal-only")
    if source is None or target is None:
        return None
    return CorrelationSpec(source_field=source, target_field=target)
def _parse_frame_rules(col: _Collector, file: str, raw: Any,
                       header: str) -> tuple[Any, ...]:
    """解析 ``frame_rules`` 数组（每条必填 name；µs 域产物），跨字段语义留给约束簇。"""
    from labelkit.common.runtime.scenario.model import FrameRuleSpec

    if raw is _MISSING:
        return ()
    if not isinstance(raw, list):
        parent, _, key = header.strip("[]").rpartition(".")
        col.error(f"{file}:[{parent}].{key}: expected array of tables, "
                  f"got {_fmt(raw)}")
        return ()
    rules: list[FrameRuleSpec] = []
    for index, row in enumerate(raw, 1):
        label = f"{header}[{index}]"
        if not isinstance(row, dict):
            col.error(f"{file}:{label}: expected table, got {_fmt(row)}")
            continue
        table = _Tbl(col, file, label, row)
        name = _parse_natural_name(table, label)
        template = table.get_str("template", None, required=True,
                                 enum=_FRAME_TEMPLATES)
        frame_class = table.get_str("frame_class", None, nonempty=True)
        source = table.get_str("source", None, nonempty=True)
        target = table.get_str("target", None, nonempty=True)
        count = table.get_int("count", None, minimum=1)
        time_us = _parse_time_us(table, "time_s")
        correlation = _parse_correlation(table, "correlation")
        table.finish()
        if template is not None and name is not None:
            rules.append(FrameRuleSpec(name=name, template=template,
                                       frame_class=frame_class, source=source,
                                       target=target, count=count, time_us=time_us,
                                       correlation=correlation))
    return tuple(rules)


def _parse_half_open_time_us(t: _Tbl, key: str) -> tuple[int, int] | None:
    """解析半开微秒区间；sequence rule 与 frame rule 共用。"""
    return _parse_time_us(t, key)


def _parse_sequence_rules(col: _Collector, file: str, raw: Any,
                          header: str) -> tuple[Any, ...]:
    """解析跨 sequence 的四类 DECLARE 规则。"""
    from labelkit.common.runtime.scenario.model import SequenceRuleSpec
    if raw is _MISSING:
        return ()
    if not isinstance(raw, list):
        col.error(f"{file}:{header}: expected array of tables, got {_fmt(raw)}")
        return ()
    result = []
    for index, row in enumerate(raw, 1):
        label = f"{header}[{index}]"
        if not isinstance(row, dict):
            col.error(f"{file}:{label}: expected table, got {_fmt(row)}")
            continue
        t = _Tbl(col, file, label, row)
        name = _parse_natural_name(t, label)
        template = t.get_str("template", None, required=True,
                             enum=("precedence", "response", "succession", "not_co_existence"))
        source = t.get_str("source", None, required=True, nonempty=True)
        target = t.get_str("target", None, required=True, nonempty=True)
        period = t.get_str("period", "day", enum=("day", "week", "schedule"))
        gap = _parse_half_open_time_us(t, "gap_s")
        t.finish()
        if name and template and source and target and period:
            result.append(SequenceRuleSpec(name=name, template=template, source=source,
                                           target=target, period=period, gap_us=gap))
    return tuple(result)


def _parse_clock_us(col: _Collector, loc: str, value: str) -> int | None:
    """把一个 HH:MM[:SS[.ffffff]] 墙钟字符串转换为日内微秒偏移。"""
    import re

    match = re.fullmatch(r"(\d{2}):(\d{2})(?::(\d{2})(?:\.(\d{1,6}))?)?", value)
    if match is None:
        col.error(f"{loc}: expected HH:MM, HH:MM:SS or microsecond-precision "
                  f"wall-clock time, got {_fmt(value)}")
        return None
    hour, minute = int(match.group(1)), int(match.group(2))
    second = int(match.group(3) or 0)
    micros = int((match.group(4) or "").ljust(6, "0") or 0)
    if hour > 23 or minute > 59 or second > 59:
        col.error(f"{loc}: expected a valid same-day wall-clock time, got {_fmt(value)}")
        return None
    return ((hour * 60 + minute) * 60 + second) * 1_000_000 + micros


def _parse_day_ranges_us(col: _Collector, file: str, label: str,
                         raw: Any) -> tuple[tuple[int, int], ...]:
    """解析一个窗口表的同日半开时间分支并量化为微秒。"""
    expected = "a non-empty array of [start, end] time strings"
    if raw is _MISSING:
        col.error(f"{file}:{label}: missing required key, expected {expected}")
        return ()
    if not isinstance(raw, list) or not raw:
        col.error(f"{file}:{label}: expected {expected}, got {_fmt(raw)}")
        return ()
    ranges: list[tuple[int, int]] = []
    for index, item in enumerate(raw, 1):
        item_label = f"{label}[{index}]"
        if (not isinstance(item, list) or len(item) != 2
                or any(not isinstance(value, str) for value in item)):
            col.error(f"{file}:{item_label}: expected [start, end] string pair, got "
                      f"{_fmt(item)}")
            continue
        start = _parse_clock_us(col, f"{file}:{item_label}[1]", item[0])
        end = _parse_clock_us(col, f"{file}:{item_label}[2]", item[1])
        if start is not None and end is not None:
            ranges.append((start, end))
    return tuple(ranges)


def _parse_frame_windows(col: _Collector, file: str, raw: Any,
                         header: str) -> tuple[Any, ...]:
    """解析 ``frame_windows`` 数组（每条必填 name），重叠/跨午夜留给约束簇。"""
    from labelkit.common.runtime.scenario.calendar import of_week_from_words
    from labelkit.common.runtime.scenario.model import FrameWindowSpec

    if raw is _MISSING:
        return ()
    if not isinstance(raw, list):
        parent, _, key = header.strip("[]").rpartition(".")
        col.error(f"{file}:[{parent}].{key}: expected array of tables, "
                  f"got {_fmt(raw)}")
        return ()
    windows: list[FrameWindowSpec] = []
    for index, row in enumerate(raw, 1):
        label = f"{header}[{index}]"
        if not isinstance(row, dict):
            col.error(f"{file}:{label}: expected table, got {_fmt(row)}")
            continue
        table = _Tbl(col, file, label, row)
        name = _parse_natural_name(table, label)
        frame_class = table.get_str("frame_class", None, required=True, nonempty=True)
        day_raw = table.take("of_day")
        of_day = _parse_day_ranges_us(col, file, f"{label}.of_day", day_raw)
        words = table.get_str_tuple("of_week", _SEQUENCE_WEEKDAYS,
                                    elem_enum=_SEQUENCE_WEEKDAYS)
        table.finish()
        try:
            of_week = of_week_from_words(words)
        except ValueError:
            of_week = ()
        if frame_class is not None and name is not None and of_day:
            windows.append(FrameWindowSpec(name=name, frame_class=frame_class,
                                           of_day_us=of_day, of_week=of_week))
    return tuple(windows)


def _parse_schedule(col: _Collector, file: str, raw: Any) -> Any | None:
    """解析 ``[generate.stream.schedule]`` 三键并冻结为 µs 域 ``ScheduleSpec``。

    start/end 必带显式 ``Z`` 或 numeric offset 且同 offset、end > start；
    exclude_dates 不得重复且不得落在 schedule 本地日范围之外（fail-fast）。

    @param col 错误聚合器
    @param file project.toml 路径字符串
    @param raw ``[generate.stream].schedule`` 原始子表
    @return 冻结的 ``ScheduleSpec``；形状失败时 None（错误已聚合）
    """
    from labelkit.common.runtime.scenario.calendar import (
        out_of_range_exclusions,
        parse_schedule_spec,
    )
    from labelkit.common.runtime.scenario.model import ScheduleSpec

    if raw is _MISSING:
        return None
    if not isinstance(raw, dict):
        col.error(f"{file}:[generate.stream.schedule]: expected table, got {_fmt(raw)}")
        return None
    t = _Tbl(col, file, "[generate.stream.schedule]", raw)
    start = t.get_str("start", None, required=True, nonempty=True)
    end = t.get_str("end", None, required=True, nonempty=True)
    exclude = t.get_str_tuple("exclude_dates", ())
    t.finish()
    if start is None or end is None:
        return None
    try:
        spec: ScheduleSpec = parse_schedule_spec(start, end, exclude)
    except ValueError as exc:
        col.error(f"{file}:[generate.stream.schedule]: {exc}")
        return None
    outside = out_of_range_exclusions(spec)
    if outside:
        col.error(f"{file}:[generate.stream.schedule].exclude_dates: entries outside "
                  f"the schedule's local-date range are a directed config error "
                  f"(fail-fast, never silently ignored), got {_fmt(list(outside))}")
        return None
    return spec


def _parse_quotas(col: _Collector, file: str, raw: Any) -> tuple[Any, ...]:
    """解析 ``[[generate.stream.quotas]]`` 双形态表（exact counts | integer weights）。"""
    from labelkit.common.runtime.scenario.calendar import of_week_from_words
    from labelkit.common.runtime.scenario.model import QuotaSpec

    if raw is _MISSING:
        return ()
    if not isinstance(raw, list):
        col.error(f"{file}:[generate.stream].quotas: expected array of tables, "
                  f"got {_fmt(raw)}")
        return ()
    quotas: list[QuotaSpec] = []
    for index, row in enumerate(raw, 1):
        label = f"[[generate.stream.quotas]][{index}]"
        if not isinstance(row, dict):
            col.error(f"{file}:{label}: expected table, got {_fmt(row)}")
            continue
        t = _Tbl(col, file, label, row)
        name = _parse_natural_name(t, label)
        period = t.get_str("period", None, required=True,
                           enum=("day", "week", "schedule"))
        words = t.get_str_tuple("of_week", _SEQUENCE_WEEKDAYS,
                                elem_enum=_SEQUENCE_WEEKDAYS)
        counts_raw = t.take("counts")
        total = t.get_int("total", None, minimum=1)
        weights_raw = t.take("weights")
        allocation = t.get_str("allocation", None,
                               enum=("exact", "largest_remainder"))
        t.finish()
        if name is None or period is None:
            continue
        try:
            of_week = of_week_from_words(words)
        except ValueError:
            of_week = ()
        counts = _parse_quota_counts(col, file, label, counts_raw)
        weights = _parse_quota_weights(col, file, label, weights_raw)
        if counts is None and weights is None:
            continue
        quotas.append(QuotaSpec(name=name, period=period, of_week=of_week,
                                counts=counts, total=total, weights=weights,
                                allocation=allocation))
    return tuple(quotas)


def _parse_quota_counts(col: _Collector, file: str, label: str,
                        raw: Any) -> tuple[tuple[str, int], ...] | None:
    """解析 counts 形态（非空 {sequence_class = integer >= 0}）并裁定互斥。"""
    if raw is _MISSING:
        return None
    if not isinstance(raw, dict) or not raw:
        col.error(f"{file}:{label}.counts: expected a non-empty "
                  f"{{sequence_class = integer >= 0}} inline table, got {_fmt(raw)}")
        return None
    pairs: list[tuple[str, int]] = []
    for key, value in raw.items():
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            col.error(f"{file}:{label}.counts.{key}: expected an integer >= 0, "
                      f"got {_fmt(value)}")
            return None
        pairs.append((key, value))
    return tuple(pairs)


def _parse_quota_weights(col: _Collector, file: str, label: str,
                         raw: Any) -> tuple[tuple[str, int], ...] | None:
    """解析 weights 形态（至少两类正整数）并裁定与 counts 的互斥。"""
    if raw is _MISSING:
        return None
    if not isinstance(raw, dict) or len(raw) < 2:
        col.error(f"{file}:{label}.weights: expected an inline table of at least two "
                  f"sequence classes with positive integer weights, got {_fmt(raw)}")
        return None
    pairs: list[tuple[str, int]] = []
    for key, value in raw.items():
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            col.error(f"{file}:{label}.weights.{key}: expected a positive integer, "
                      f"got {_fmt(value)}")
            return None
        pairs.append((key, value))
    return tuple(pairs)


def _parse_noise_classes(col: _Collector, file: str, raw: Any) -> tuple[Any, ...]:
    """解析 ``[[generate.stream.noise]]`` 表（frame_class + 正整数 weight）。"""
    from labelkit.common.runtime.scenario.model import NoiseClassSpec

    if raw is _MISSING:
        return ()
    if not isinstance(raw, list):
        col.error(f"{file}:[generate.stream].noise: expected array of tables, "
                  f"got {_fmt(raw)}")
        return ()
    classes: list[NoiseClassSpec] = []
    for index, row in enumerate(raw, 1):
        label = f"[[generate.stream.noise]][{index}]"
        if not isinstance(row, dict):
            col.error(f"{file}:{label}: expected table, got {_fmt(row)}")
            continue
        t = _Tbl(col, file, label, row)
        frame_class = t.get_str("frame_class", None, required=True, nonempty=True)
        weight = t.get_int("weight", None, minimum=1)
        t.finish()
        if frame_class is None:
            continue
        if weight is None:
            col.error(f"{file}:{label}.weight: required, expected a positive integer")
            continue
        classes.append(NoiseClassSpec(frame_class=frame_class, weight=weight))
    return tuple(classes)


def _parse_duration_resources(col: _Collector, file: str, cname: str,
                              gen_sub: Any) -> tuple[tuple[int, int] | None, tuple[str, ...]]:
    """解析帧类 duration_s 闭区间及 resources。"""
    if not isinstance(gen_sub, dict):
        return None, ()
    label = f"[frame.class.{cname}.generate]"
    raw = gen_sub.get("duration_s")
    duration_us = None
    if raw is not None:
        try:
            if (not isinstance(raw, list) or len(raw) != 2
                    or any(isinstance(v, bool) or not isinstance(v, (int, float)) for v in raw)):
                raise ValueError
            values = [Decimal(str(v)) * Decimal(1_000_000) for v in raw]
            lo, hi = math.ceil(values[0]), math.floor(values[1])
            if not all(v.is_finite() for v in values) or values[0] < 1 or lo > hi:
                raise ValueError
            duration_us = (lo, hi)
        except (InvalidOperation, ValueError, OverflowError):
            col.error(f"{file}:{label}.duration_s: expected finite closed range with 1e-6 <= lo <= hi, got {_fmt(raw)}")
    raw_resources = gen_sub.get("resources", [])
    resources: list[str] = []
    if not isinstance(raw_resources, list):
        col.error(f"{file}:{label}.resources: expected a string array, got {_fmt(raw_resources)}")
    else:
        for index, value in enumerate(raw_resources, 1):
            if not isinstance(value, str) or not re.fullmatch(r"[a-z0-9_]+", value):
                col.error(f"{file}:{label}.resources[{index}]: expected a non-empty [a-z0-9_]+ resource name, got {_fmt(value)}")
            else:
                resources.append(value)
    if len(set(resources)) != len(resources):
        col.error(f"{file}:{label}.resources: resource names must be unique")
    if resources and duration_us is None:
        col.error(f"{file}:{label}.resources: non-empty resources require duration_s")
    return duration_us, tuple(resources)


def _parse_time_fields(col: _Collector, file: str, cname: str,
                       gen_sub: Any) -> dict[str, str] | None:
    """解析帧类时间字段绑定。"""
    raw = gen_sub.get("time_fields") if isinstance(gen_sub, dict) else None
    if raw is None:
        return None
    label = f"[frame.class.{cname}.generate.time_fields]"
    if not isinstance(raw, dict):
        col.error(f"{file}:{label}: expected table, got {_fmt(raw)}")
        return None
    result = {}
    for key, value in raw.items():
        if not isinstance(value, str):
            col.error(f"{file}:{label}.{key}: expected string (a time vocabulary term), got {_fmt(value)}")
        else:
            result[key] = value
    return result





def _check_deleted_stream_keys(t: _Tbl, raw: dict) -> None:
    """v1.17 删除键的定向报错（rule 62）：只指向新表达，不读取旧值。"""
    for key, target in _DELETED_STREAM_KEYS.items():
        if key in raw:
            t.seen.add(key)
            t.col.error(f"{t.file}:[generate.stream].{key}: this key was removed in "
                        f"v1.17 - use {target} instead")


def _parse_generate_stream(col: _Collector, file: str, raw: Any) -> GenerateStreamConfig:
    """v1.17: 解析 ``[generate.stream]`` 子表的全新键面（SPEC-SP §4.2）。

    结构性校验（类型、区间数组形状、内在序关系）在此完成; 跨节与形态相关的约束
    （quota 域、noise 域、schedule 前提……）留给形态约束簇——本形态关闭
    时那些键只是停放配置, 不构成错误。五删除键无条件定向报错。

    @param col 错误聚合器
    @param file 报错定位用的 project.toml 路径字符串
    @param raw ``[generate].stream`` 的原始值
    @return ``GenerateStreamConfig``
    """
    if raw is not _MISSING and not isinstance(raw, dict):
        col.error(f"{file}:[generate].stream: expected table, got {_fmt(raw)}")
        raw = _MISSING
    table = raw if isinstance(raw, dict) else None
    t = _Tbl(col, file, "[generate.stream]", table)
    if table is not None:
        _check_deleted_stream_keys(t, table)
    cfg = GenerateStreamConfig(
        enabled=t.get_bool("enabled", False),
        crossed_sessions=t.get_int("crossed_sessions", 0, minimum=0),
        noise_ratio=t.get_float("noise_ratio", 0.0),   # [0,1) 由形态约束簇裁定
        duplicates=t.get_int("duplicates", 0, minimum=0),
        frame_gap_s=_num_pair(t, "frame_gap_s", (5.0, 60.0)),
        max_attempts_per_slot=t.get_int("max_attempts_per_slot", 3, minimum=1),
        tiers=_parse_tiers(col, file, t.take("tiers"),           # v1.14 档位表
                           "[[generate.stream.tiers]]"),
        schedule=_parse_schedule(col, file, t.take("schedule")),
        quotas=_parse_quotas(col, file, t.take("quotas")),
        noise_classes=_parse_noise_classes(col, file, t.take("noise")),
        frame_rules=_parse_frame_rules(col, file, t.take("frame_rules"),
                                       "[[generate.stream.frame_rules]]"),
        frame_windows=_parse_frame_windows(col, file, t.take("frame_windows"),
                                           "[[generate.stream.frame_windows]]"),
        sequence_rules=_parse_sequence_rules(col, file, t.take("sequence_rules"),
                                             "[[generate.stream.sequence_rules]]"),
    )
    t.finish()
    return cfg


def _parse_judgment_reasons(col: _Collector, t: _Tbl) -> bool | str:
    """读取 ``[quality].judgment_reasons`` 三态键("auto" | true | false)。

    @param col 错误聚合器
    @param t ``[quality]`` 表读取器
    @return 合法取值; 违规时回落 "auto"
    """
    v = t.take("judgment_reasons")
    if v is _MISSING:
        return "auto"
    if isinstance(v, bool) or v == "auto":
        return v
    col.error(f'{t.loc("judgment_reasons")}: expected "auto" | true | false, got {_fmt(v)}')
    return "auto"


def _parse_run(col: _Collector, file: str, top: _Tbl) -> dict[str, Any]:
    """解析 ``[run]`` 节(取值以字典形态回传, 供后续 CLI 覆盖合并)。

    @param col 错误聚合器
    @param file 报错定位用的 project.toml 路径字符串
    @param top 顶层表读取器
    @return ``[run]`` 各键的取值字典
    """
    t = _Tbl(col, file, "[run]", _section(col, top, "run"))
    run = dict(
        input=t.get_str("input", None, nonempty=True),
        output=t.get_str("output", None, nonempty=True),
        modality=t.get_str("modality", None, required=True, enum=("text", "ui")),
        mode=t.get_str("mode", "process", enum=("process", "generate_only")),
        batch_size=t.get_int("batch_size", 256, minimum=1),
        seed=t.get_int("seed", 0),
        fatal_error_threshold=t.get_int("fatal_error_threshold", 20, minimum=1),
        max_park_s=t.get_int("max_park_s", 3600, minimum=0),
    )
    t.finish()
    return run


def _parse_input(col: _Collector, file: str, top: _Tbl) -> InputConfig:
    """解析 ``[input]`` 节(摄取参数)。

    @param col 错误聚合器
    @param file 报错定位用的 project.toml 路径字符串
    @param top 顶层表读取器
    @return ``InputConfig``
    """
    t = _Tbl(col, file, "[input]", _section(col, top, "input"))
    cfg = InputConfig(
        text_field=t.get_str("text_field", "text", nonempty=True),
        on_bad_line=t.get_str("on_bad_line", "skip", enum=("skip", "fail")),
        on_missing_pair=t.get_str("on_missing_pair", "skip", enum=("skip", "fail")),
        on_index_conflict=t.get_str("on_index_conflict", "fail", enum=("skip", "fail")),
        max_image_mb=t.get_int("max_image_mb", 20, minimum=1),
        ui_tree_max_chars=t.get_int("ui_tree_max_chars", 30000, minimum=1),
    )
    t.finish()
    return cfg


def _parse_stream(col: _Collector, file: str, section: Any) -> StreamConfig:
    """解析 ``[stream]`` 节(时间序与会话切分声明)。

    @param col 错误聚合器
    @param file 报错定位用的 project.toml 路径字符串
    @param section 该节原始值
    @return ``StreamConfig``
    """
    t = _Tbl(col, file, "[stream]", section)
    cfg = StreamConfig(
        order_by=t.get_str("order_by", "input_order", nonempty=True) or "input_order",
        on_disorder=t.get_str("on_disorder", "skip", enum=("skip", "fail")),
        key=t.get_str_tuple("key", ()),
        gap_s=t.get_int("gap_s", 300, minimum=0),
        gap_steps=t.get_int("gap_steps", 0, minimum=0),
        session_max_len=t.get_int("session_max_len", 200, minimum=1),
        session_max_span_s=t.get_int("session_max_span_s", 0, minimum=0),
    )
    t.finish()
    return cfg


def _parse_dedup(col: _Collector, file: str, top: _Tbl) -> DedupConfig:
    """解析 ``[dedup]`` 节(去重参数)。

    @param col 错误聚合器
    @param file 报错定位用的 project.toml 路径字符串
    @param top 顶层表读取器
    @return ``DedupConfig``
    """
    t = _Tbl(col, file, "[dedup]", _section(col, top, "dedup"))
    cfg = DedupConfig(
        enabled=t.get_bool("enabled", True),
        scope=t.get_str("scope", "global", enum=("global", "batch")),
        minhash_threshold=t.get_float("minhash_threshold", 0.85, bound=_UNIT_HALF_OPEN),
        minhash_num_perm=t.get_int("minhash_num_perm", 128, minimum=1),
        ngram=t.get_int("ngram", 5, minimum=1),
        image_phash_max_distance=t.get_int("image_phash_max_distance", 8, minimum=0),
        ui_dup_requires=t.get_str("ui_dup_requires", "both", enum=("both", "tree", "image")),
        bounds_quantize_px=t.get_int("bounds_quantize_px", 4, minimum=0),
        semantic=t.get_bool("semantic", False),
        semantic_embedding=t.get_str("semantic_embedding", None, nonempty=True),
        semantic_threshold=t.get_float("semantic_threshold", 0.95, bound=_UNIT_HALF_OPEN),
    )
    t.finish()
    return cfg


def _parse_segment(col: _Collector, file: str, section: Any) -> SegmentConfig:
    """解析 ``[segment]`` 节(流分段; ``vision_resolved`` 是 load() 收尾冻结的解析产物)。

    @param col 错误聚合器
    @param file 报错定位用的 project.toml 路径字符串
    @param section 该节原始值
    @return ``SegmentConfig``
    """
    t = _Tbl(col, file, "[segment]", section)
    cfg = SegmentConfig(
        enabled=t.get_bool("enabled", False),
        strategy=t.get_str("strategy", "hybrid", enum=("rules", "llm", "hybrid")),
        llm=t.get_str("llm", "default", nonempty=True),
        window=t.get_int("window", 20, minimum=1),   # >= 2 在约束簇里查(§3.6)
        digest_max_chars=t.get_int("digest_max_chars", 400, minimum=1),
        noise_filter=t.get_bool("noise_filter", True),
        min_len=t.get_int("min_len", 2, minimum=1),
        context=t.get_str("context", "") or "",
        on_error=t.get_str("on_error", "keep", enum=("keep", "fail")),
    )
    # v1.11 (V2/V27②): `use_vision` 已移除——它的显式在场是**定向** CONFIG_ERROR,
    # 由约束簇经原始节探针上报, 而非未知键前向兼容 WARN(此处标记 seen 抑制该 WARN,
    # 保证定向报错是唯一上报)。
    t.seen.add("use_vision")
    t.finish()
    return cfg


def _parse_stitch(col: _Collector, file: str, section: Any) -> StitchConfig:
    """解析 ``[stitch]`` 节(线索缝合)。

    @param col 错误聚合器
    @param file 报错定位用的 project.toml 路径字符串
    @param section 该节原始值
    @return ``StitchConfig``
    """
    t = _Tbl(col, file, "[stitch]", section)
    cfg = StitchConfig(
        enabled=t.get_bool("enabled", False),
        llm=t.get_str("llm", "default", nonempty=True),
        max_open=t.get_int("max_open", 4, minimum=1),
        bias=t.get_str("bias", "conservative", enum=("conservative", "llm")),
        rescue_short=t.get_bool("rescue_short", True),
        repass=t.get_bool("repass", True),
        stale_gap_steps=t.get_int("stale_gap_steps", 0, minimum=0),
        digest_max_chars=t.get_int("digest_max_chars", 400, minimum=1),
        context=t.get_str("context", "") or "",
        votes=t.get_int("votes", 1, minimum=1),   # 须为奇数, 在约束簇里查(T17)
        on_error=t.get_str("on_error", "keep", enum=("keep", "fail")),
    )
    t.finish()
    return cfg


def _parse_extract(col: _Collector, file: str, section: Any) -> ExtractConfig:
    """解析 ``[extract]`` 节(动作摘取)。

    @param col 错误聚合器
    @param file 报错定位用的 project.toml 路径字符串
    @param section 该节原始值
    @return ``ExtractConfig``
    """
    t = _Tbl(col, file, "[extract]", section)
    cfg = ExtractConfig(
        enabled=t.get_bool("enabled", False),
        llm=t.get_str("llm", "default", nonempty=True),
        instruction=t.get_str("instruction", "") or "",
        include_diff=t.get_bool("include_diff", True),
        on_error=t.get_str("on_error", "fallback", enum=("fallback", "fail")),
    )
    t.finish()
    return cfg


def _parse_classify(col: _Collector, file: str, section: Any) -> ClassifyConfig:
    """解析 ``[classify]`` 节(序列级闭集分类)。

    @param col 错误聚合器
    @param file 报错定位用的 project.toml 路径字符串
    @param section 该节原始值
    @return ``ClassifyConfig``
    """
    t = _Tbl(col, file, "[classify]", section)
    cfg = ClassifyConfig(
        enabled=t.get_bool("enabled", False),
        llm=t.get_str("llm", "default", nonempty=True),
        assignment=t.get_str("assignment", "single", enum=("single", "multi")),
        max_labels=t.get_int("max_labels", None),   # [2, len(classes)] 在约束簇里查
        instruction=t.get_str("instruction", "") or "",
        fallback_class=t.get_str("fallback_class", "") or "",
        self_consistency=t.get_int("self_consistency", 0, minimum=0),
        sc_temperature=t.get_float("sc_temperature", 0.7, bound=_GE0),
        on_error=t.get_str("on_error", "fallback", enum=("fallback", "fail")),
        classes=_parse_classes(col, file, t.take("classes")),
    )
    t.finish()
    return cfg


def _parse_quality(col: _Collector, file: str, section: Any) -> QualityConfig:
    """解析 ``[quality]`` 节(QuRating 质量打分)。

    @param col 错误聚合器
    @param file 报错定位用的 project.toml 路径字符串
    @param section 该节原始值
    @return ``QualityConfig``
    """
    t = _Tbl(col, file, "[quality]", section)
    cfg = QualityConfig(
        enabled=t.get_bool("enabled", True),
        mode=t.get_str("mode", "pairwise", enum=("pairwise", "pointwise")),
        llm=t.get_str("llm", "default", nonempty=True),
        rounds=t.get_int("rounds", 4, minimum=1),
        criteria_per_call=t.get_str("criteria_per_call", "all", enum=("all", "single")),
        threshold=t.get_float("threshold", None, bound=_UNIT_CLOSED),
        selection=t.get_str("selection", "threshold", enum=("threshold", "top_ratio")),
        top_ratio=t.get_float("top_ratio", None, bound=_UNIT_HALF_OPEN),
        judges=t.get_str_tuple("judges", ()),
        both_orders=t.get_bool("both_orders", False),
        on_unscored=t.get_str("on_unscored", "keep", enum=("keep", "drop")),
        rubric=t.get_str("rubric", "", enum=_RUBRIC_SELECTORS) or "",
        judgment_reasons=_parse_judgment_reasons(col, t),
    )
    t.finish()
    return cfg


def _parse_generate_block(col: _Collector, file: str,
                          section: Any) -> tuple[GenerateConfig, GenerateStreamConfig,
                                                 dict[str, bool]]:
    """解析 ``[generate]`` 节及其 ``stream`` 子表, 并采集另一形态禁设键探针。

    v1.17：``sequences`` 键已删除（rule 62）——显式书写是定向 CONFIG_ERROR，配额由
    ``[[generate.stream.quotas]]`` 承载。

    @param col 错误聚合器
    @param file 报错定位用的 project.toml 路径字符串
    @param section 该节原始值
    @return (``GenerateConfig``, ``GenerateStreamConfig``, 禁设键在场探针表)
    """
    t = _Tbl(col, file, "[generate]", section)
    if "sequences" in (section if isinstance(section, dict) else {}):
        t.seen.add("sequences")
        col.error(f"{file}:[generate].sequences: this key was removed in v1.17 - "
                  f"sequence quotas are carried by [[generate.stream.quotas]] "
                  f"(rule 62)")
    generate = GenerateConfig(
        enabled=t.get_bool("enabled", False),
        llms=t.get_str_tuple("llms", ("default",)) or ("default",),
        instruction=t.get_str("instruction", "") or "",
        mixture=t.get_str("mixture", "round_robin", enum=("round_robin", "weighted")),
        weights=t.get_float_tuple("weights", ()),
        styles=_parse_styles(col, file, t.take("styles")),
        num_per_record=t.get_int("num_per_record", 2, minimum=1),
        seeds_per_call=t.get_int("seeds_per_call", 3, minimum=1),
        num_per_call=t.get_int("num_per_call", 4, minimum=1),
        seed_min_score=t.get_float("seed_min_score", None, bound=_UNIT_CLOSED),
        temperature=t.get_float("temperature", 0.9, bound=_GE0),
        sample_validator=t.get_str("sample_validator", None, nonempty=True),
        sequence_validator=t.get_str("sequence_validator", None, nonempty=True),
        scenario_validator=t.get_str("scenario_validator", None, nonempty=True),
        seed_examples=t.get_str_tuple("seed_examples", ()),
        standalone_count=t.get_int("standalone_count", None, minimum=1),
        len_range=_int_pair(t, "len_range", (3, 6)),          # 全局默认抽取域
    )
    # 区分"显式设置"与"dataclass 默认"以支撑模式规则; v1.13 起同一张表兼作时间流
    # 形态的原始节禁设键探针(四键, _STREAM_FORBIDDEN_GEN_KEYS)。
    gen_provided = {
        key: isinstance(section, dict) and key in section
        for key in _STREAM_FORBIDDEN_GEN_KEYS
    }
    stream_section = t.take("stream")
    # v1.14 原始节探针: 档位表的**在场性**独立于其解析成败——档位表前提(仅时间流形态
    # 合法)必须在表内容非法时也照样上报。
    gen_provided["stream_tiers"] = (isinstance(stream_section, dict)
                                    and "tiers" in stream_section)
    gen_provided["stream_section"] = isinstance(stream_section, dict)
    generate_stream = _parse_generate_stream(col, file, stream_section)
    t.finish()
    return generate, generate_stream, gen_provided


def _parse_annotate(col: _Collector, file: str, section: Any) -> AnnotateConfig:
    """解析 ``[annotate]`` 节(自动标注)。

    @param col 错误聚合器
    @param file 报错定位用的 project.toml 路径字符串
    @param section 该节原始值
    @return ``AnnotateConfig``
    """
    t = _Tbl(col, file, "[annotate]", section)
    cfg = AnnotateConfig(
        enabled=t.get_bool("enabled", True),
        llm=t.get_str("llm", "default", nonempty=True),
        instruction=t.get_str("instruction", "") or "",
        examples=_parse_examples(col, file, t.take("examples")),
        self_consistency=t.get_int("self_consistency", 0, minimum=0),
        sc_temperature=t.get_float("sc_temperature", 0.7, bound=_GE0),
        sequence_frames=t.get_int("sequence_frames", 20, minimum=1),   # [2,100] 在约束簇
    )
    t.finish()
    return cfg


def _parse_verify(col: _Collector, file: str, top: _Tbl) -> VerifyConfig:
    """解析 ``[verify]`` 节(LLM-as-a-Judge 复核)。

    @param col 错误聚合器
    @param file 报错定位用的 project.toml 路径字符串
    @param top 顶层表读取器
    @return ``VerifyConfig``
    """
    t = _Tbl(col, file, "[verify]", _section(col, top, "verify"))
    cfg = VerifyConfig(
        enabled=t.get_bool("enabled", False),
        llm=t.get_str("llm", "judge", nonempty=True),
        judges=t.get_str_tuple("judges", ()),
        policy=t.get_str("policy", "drop", enum=("drop", "repair")),
        max_repair_rounds=t.get_int("max_repair_rounds", 1, minimum=0),
        extra_criteria=t.get_str("extra_criteria", "") or "",
    )
    t.finish()
    return cfg


def _parse_output(col: _Collector, file: str, top: _Tbl) -> OutputConfig:
    """解析 ``[output]`` 节(输出结构与 Schema 源)。

    @param col 错误聚合器
    @param file 报错定位用的 project.toml 路径字符串
    @param top 顶层表读取器
    @return ``OutputConfig``
    """
    t = _Tbl(col, file, "[output]", _section(col, top, "output"))
    cfg = OutputConfig(
        schema_path=t.get_str("schema_path", None, nonempty=True),
        schema_inline=t.get_str("schema_inline", None, nonempty=True),
        max_repair_attempts=t.get_int("max_repair_attempts", 2, minimum=0),
        repair_llm=t.get_str("repair_llm", None, nonempty=True),
        meta_mode=t.get_str("meta_mode", "inline", enum=("inline", "sidecar", "none")),
        passthrough_fields=t.get_str_tuple("passthrough_fields", ()),
        rejects=t.get_str("rejects", "refs", enum=("none", "refs", "full")),
        validator=t.get_str("validator", None, nonempty=True),
    )
    t.finish()
    return cfg


def _parse_trace(col: _Collector, file: str, top: _Tbl) -> TraceConfig:
    """解析 ``[trace]`` 节(追踪通道)。

    @param col 错误聚合器
    @param file 报错定位用的 project.toml 路径字符串
    @param top 顶层表读取器
    @return ``TraceConfig``
    """
    t = _Tbl(col, file, "[trace]", _section(col, top, "trace"))
    cfg = TraceConfig(
        enabled=t.get_bool("enabled", False),
        path=t.get_str("path", "") or "",
        channels=t.get_str_tuple("channels", ("quality", "verify", "schema"),
                                 elem_enum=_TRACE_CHANNELS),
        content=t.get_str("content", "refs", enum=("none", "refs", "excerpt", "full")),
    )
    t.finish()
    return cfg


def _frame_sub(col: _Collector, file: str, ft: _Tbl, key: str) -> dict | None:
    """从 ``[frame]`` 父表里取一个子表(缺省 → None, 非表 → 定位错误)。

    @param col 错误聚合器
    @param file 报错定位用的 project.toml 路径字符串
    @param ft ``[frame]`` 表读取器
    @param key 子表键名("classify" / "annotate" / "class")
    @return 子表字典或 None
    """
    raw = ft.take(key)
    if raw is _MISSING:
        return None
    if not isinstance(raw, dict):
        col.error(f"{file}:[frame].{key}: expected table, got {_fmt(raw)}")
        return None
    return raw


def _parse_frame_classify(col: _Collector, file: str, section: Any) -> FrameClassifyConfig:
    """解析 ``[frame.classify]`` 节(帧级闭集分类, v1.12)。

    @param col 错误聚合器
    @param file 报错定位用的 project.toml 路径字符串
    @param section 该节原始值
    @return ``FrameClassifyConfig``(``vision_resolved`` 由 load() 收尾冻结)
    """
    t = _Tbl(col, file, "[frame.classify]", section)
    cfg = FrameClassifyConfig(
        enabled=t.get_bool("enabled", False),
        llm=t.get_str("llm", "default", nonempty=True),
        fallback_class=t.get_str("fallback_class", "") or "",
        classes=_parse_classes(col, file, t.take("classes"), section="frame.classify"),
    )
    # v1.12 定向探针(v1.11 use_vision 的原始节探针同款机制): 帧级无多标签——
    # assignment 显式书写是定向 CONFIG_ERROR, 由约束簇经原始节探针上报; 此处标记
    # seen 抑制未知键前向兼容 WARN, 保证定向报错是唯一上报。
    t.seen.add("assignment")
    t.finish()
    return cfg


def _parse_frame_annotate(col: _Collector, file: str, section: Any) -> FrameAnnotateConfig:
    """解析 ``[frame.annotate]`` 节(帧级标注, v1.12)。

    @param col 错误聚合器
    @param file 报错定位用的 project.toml 路径字符串
    @param section 该节原始值
    @return ``FrameAnnotateConfig``
    """
    t = _Tbl(col, file, "[frame.annotate]", section)
    cfg = FrameAnnotateConfig(
        enabled=t.get_bool("enabled", False),
        llm=t.get_str("llm", "default", nonempty=True),
        instruction=t.get_str("instruction", "") or "",
        examples=_parse_examples(col, file, t.take("examples"), section="frame.annotate"),
        schema_path=t.get_str("schema_path", None, nonempty=True),
        schema_inline=t.get_str("schema_inline", None, nonempty=True),
    )
    # v1.12 定向探针(同上): 帧级无自洽采样——self_consistency 显式书写是定向
    # CONFIG_ERROR。
    t.seen.add("self_consistency")
    t.finish()
    return cfg


def _parse_frame_ns(col: _Collector, file: str, top: _Tbl) -> tuple[
        FrameClassifyConfig, FrameAnnotateConfig, Any, dict[str, bool]]:
    """解析 v1.12 的 ``[frame]`` 命名空间(SPEC-frame-annotation §3.1)。

    三个面: ``[frame.classify]`` / ``[frame.annotate]`` / ``[frame.class.<name>.*]``;
    ``[frame]`` 下未知子键走前向兼容 WARN(规则 1), 而 ``[frame.class.*]`` 白名单校验
    与七条组合约束在约束簇统一执行(需要已解析的 segment/output 等全局节)。

    @param col 错误聚合器
    @param file 报错定位用的 project.toml 路径字符串
    @param top 顶层表读取器
    @return (帧分类节, 帧标注节, 帧类原始节, 显式书写探针表)
    """
    frame_section = _section(col, top, "frame")
    ft = _Tbl(col, file, "[frame]", frame_section)
    frame_classify_section = _frame_sub(col, file, ft, "classify")
    frame_annotate_section = _frame_sub(col, file, ft, "annotate")
    frame_class_raw = _frame_sub(col, file, ft, "class")
    ft.finish()
    frame_classify = _parse_frame_classify(col, file, frame_classify_section)
    frame_annotate = _parse_frame_annotate(col, file, frame_annotate_section)
    provided = {
        "section": frame_section is not None,
        "classify_assignment": (isinstance(frame_classify_section, dict)
                                and "assignment" in frame_classify_section),
        "annotate_self_consistency": (isinstance(frame_annotate_section, dict)
                                      and "self_consistency" in frame_annotate_section),
    }
    return frame_classify, frame_annotate, frame_class_raw, provided


def _stream_family_probes(stream_section: Any, segment_section: Any,
                          stitch_section: Any,
                          extract_section: Any) -> dict[str, dict[str, bool]]:
    """v1.8 在场探针族(classify_provided 同款): 节在场驱动 no-op 警告, 显式键驱动意图检查。

    ``"non_switch_keys"`` = 该节除自身开关外还带了停放载荷(若只因"关着的开关"就把
    ``[segment]`` 报成被忽略, 文案会自指)。

    @param stream_section ``[stream]`` 原始节
    @param segment_section ``[segment]`` 原始节
    @param stitch_section ``[stitch]`` 原始节
    @param extract_section ``[extract]`` 原始节
    @return 四个探针表的字典
    """
    return {
        "stream": {
            "section": stream_section is not None,
            "gap_s": isinstance(stream_section, dict) and "gap_s" in stream_section,
        },
        "segment": {
            "non_switch_keys": (isinstance(segment_section, dict)
                                and any(k != "enabled" for k in segment_section)),
            # v1.11 (V2/V27②) 原始节探针: 已移除键的在场, 独立于未知键路径。
            "use_vision": (isinstance(segment_section, dict)
                           and "use_vision" in segment_section),
        },
        "stitch": {
            "non_switch_keys": (isinstance(stitch_section, dict)
                                and any(k != "enabled" for k in stitch_section)),
        },
        "extract": {
            "non_switch_keys": (isinstance(extract_section, dict)
                                and any(k != "enabled" for k in extract_section)),
        },
    }


def _parse_stream_family(col: _Collector, file: str, top: _Tbl) -> dict[str, Any]:
    """解析流家族的六节: run / input / stream / dedup / segment / stitch / extract。

    @param col 错误聚合器
    @param file 报错定位用的 project.toml 路径字符串
    @param top 顶层表读取器
    @return 以 ``_Project`` 字段名为键的部分产物字典
    """
    stream_section = _section(col, top, "stream")
    stream = _parse_stream(col, file, stream_section)
    dedup = _parse_dedup(col, file, top)
    segment_section = _section(col, top, "segment")
    segment = _parse_segment(col, file, segment_section)
    stitch_section = _section(col, top, "stitch")
    stitch = _parse_stitch(col, file, stitch_section)
    extract_section = _section(col, top, "extract")
    extract = _parse_extract(col, file, extract_section)
    probes = _stream_family_probes(stream_section, segment_section,
                                   stitch_section, extract_section)
    return dict(
        stream=stream, dedup=dedup, segment=segment, stitch=stitch, extract=extract,
        stream_provided=probes["stream"], segment_provided=probes["segment"],
        stitch_provided=probes["stitch"], extract_provided=probes["extract"],
    )


def _parse_labeling_family(col: _Collector, file: str, top: _Tbl) -> dict[str, Any]:
    """解析标注家族: classify / [frame.*] / quality / generate / annotate。

    @param col 错误聚合器
    @param file 报错定位用的 project.toml 路径字符串
    @param top 顶层表读取器
    @return 以 ``_Project`` 字段名为键的部分产物字典
    """
    classify_section = _section(col, top, "classify")
    classify = _parse_classify(col, file, classify_section)
    # "显式设置"与"dataclass 默认"要区分开(与 gen_provided 同款): max_labels 仅多标签
    # 可用, classes 驱动 R8。
    classify_provided = {
        "classes": isinstance(classify_section, dict) and "classes" in classify_section,
        "max_labels": isinstance(classify_section, dict) and "max_labels" in classify_section,
    }
    frame_classify, frame_annotate, frame_class_raw, frame_provided = _parse_frame_ns(
        col, file, top)
    quality_section = _section(col, top, "quality")
    quality = _parse_quality(col, file, quality_section)
    generate, generate_stream, gen_provided = _parse_generate_block(
        col, file, _section(col, top, "generate"))
    top_ratio_provided = isinstance(quality_section, dict) and "top_ratio" in quality_section
    annotate_section = _section(col, top, "annotate")
    annotate = _parse_annotate(col, file, annotate_section)
    return dict(
        classify=classify, classify_provided=classify_provided,
        frame_classify=frame_classify, frame_annotate=frame_annotate,
        frame_class_raw=frame_class_raw, frame_provided=frame_provided,
        quality=quality, generate=generate, generate_stream=generate_stream,
        gen_provided=gen_provided, top_ratio_provided=top_ratio_provided,
        annotate=annotate,
        sequence_frames_provided=(isinstance(annotate_section, dict)
                                  and "sequence_frames" in annotate_section),
    )


def _parse_delivery_family(col: _Collector, file: str, top: _Tbl) -> dict[str, Any]:
    """解析交付家族: verify / output / trace, 外加两块原样透传的原始节。

    ``[rubric]`` 不在此解析: 准则错误必须落在 3.1.4 表行次序的 rubric 槽位
    (spec 3.1.5 样例输出), 故由约束簇在准则解析阶段延迟解析。``[class.<name>.*]``
    (v1.7) 同样原样透传: 白名单校验与按类合并需要已解析的全局节**和**已解析的全局
    准则, 归约束簇所有。

    @param col 错误聚合器
    @param file 报错定位用的 project.toml 路径字符串
    @param top 顶层表读取器
    @return 以 ``_Project`` 字段名为键的部分产物字典
    """
    return dict(
        verify=_parse_verify(col, file, top),
        output=_parse_output(col, file, top),
        trace=_parse_trace(col, file, top),
        rubric_raw=_section(col, top, "rubric"),
        class_raw=_section(col, top, "class"),
    )


def _resolve_project_path(value: str | None, root: Path) -> str | None:
    """v1.17(SPEC-SP §5.1): 把 project TOML 里的相对路径按 project root 绝对化。

    绝对路径原样规范化返回; None / 空串原样返回(占位语义由各消费面解释)。

    @param value 声明路径取值
    @param root project root(``Path(project_path).resolve().parent``)
    @return 绝对规范化路径字符串, 或原样 None
    """
    if not value:
        return value
    path = Path(value)
    return str(path if path.is_absolute() else (root / path).resolve())


def _absolutize_raw_schema_paths(raw: Any, root: Path, gen: bool) -> None:
    """就地绝对化原始按类节里的 ``schema_path`` 声明值。

    ``[class.<name>.annotate].schema_path``(``gen = false``)与
    ``[frame.class.<name>.generate].schema_path``(``gen = true``)的值在后续
    白名单合并/Schema 装载时才被读取; 在此统一按 project root 绝对化, 使所有
    Schema 消费面拿到的都是绝对路径。键名不变, 白名单校验不受影响。

    @param raw ``[class.*]`` 或 ``[frame.class.*]`` 的原始节字典
    @param root project root
    @param gen 是否走 generate 子表的 schema_path 面
    """
    if not isinstance(raw, dict):
        return
    for sections in raw.values():
        if not isinstance(sections, dict):
            continue
        sub = sections.get("generate" if gen else "annotate")
        if isinstance(sub, dict) and isinstance(sub.get("schema_path"), str):
            sub["schema_path"] = _resolve_project_path(sub["schema_path"], root)


def _parse_project_file(col: _Collector, file: str, data: dict,
                        root: Path) -> _Project:
    """解析整份 project.toml, 逐节物化配置对象。

    节的解析次序即 spec 3.1.4 的表行次序——报错聚合次序由它决定, 不得调整。
    v1.17: 四类 schema_path 与显式 trace.path 在此按 ``root``(project root)绝对化;
    ``run.input``/``run.output`` 的解析留给约束簇(CLI 覆盖先按调用 cwd 解析、
    再参与 CLI > project 优先级合并, 无法在纯解析期定锚)。

    @param col 错误聚合器
    @param file 报错定位用的 project.toml 路径字符串
    @param data 已 TOML 解析的顶层字典
    @param root project root(相对路径解析基点)
    @return ``_Project``
    """
    top = _Tbl(col, file, "", data)
    _check_schema_version(col, top)
    parts: dict[str, Any] = dict(run=_parse_run(col, file, top),
                                 input=_parse_input(col, file, top))
    parts.update(_parse_stream_family(col, file, top))
    parts.update(_parse_labeling_family(col, file, top))
    parts.update(_parse_delivery_family(col, file, top))
    top.finish()
    parts["output"] = replace(parts["output"],
                              schema_path=_resolve_project_path(
                                  parts["output"].schema_path, root))
    parts["trace"] = replace(
        parts["trace"], path=_resolve_project_path(parts["trace"].path, root) or "")
    parts["frame_annotate"] = replace(
        parts["frame_annotate"],
        schema_path=_resolve_project_path(parts["frame_annotate"].schema_path, root))
    _absolutize_raw_schema_paths(parts["class_raw"], root, gen=False)
    _absolutize_raw_schema_paths(parts["frame_class_raw"], root, gen=True)
    return _Project(**parts)
