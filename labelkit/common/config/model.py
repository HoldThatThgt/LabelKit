"""config.toml + project.toml + CLI 覆盖项的定型冻结镜像（spec ch.5）。"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal, Mapping

from labelkit.common.config.generation import (
    SequenceClassGenerationConfig,
    SequenceGenerationConfig,
)

if TYPE_CHECKING:
    from labelkit.common.extensions.hooks import ValidationHooks


# ── config.toml 侧 ─────────────────────────────────────────────────────────

@dataclass(frozen=True)
class ToolConfig:
    """工具级日志设置（spec 5.1 [tool]）。"""

    log_level: str = "info"                       # debug|info|warn|error；被 --log-level 覆盖
    log_format: Literal["text", "jsonl"] = "text" # jsonl 强制 console 走 plain（spec §7.7）


@dataclass(frozen=True)
class ConsoleConfig:
    """v1.10（spec 5.1 [console]）：三模式 console/进度面（§7.7）。

    工具级——属部署属性，整节可省略。
    """

    mode: Literal["auto", "rich", "plain"] = "auto"   # 面板模式；被 CLI --console 覆盖，
                                                  # auto 判定链见 §7.7（U5/U25）
    refresh_hz: int = 5                           # rich 画布重绘频率，闭区间 1–10
                                                  # （越界 = CONFIG_ERROR，spec 3.1.4）
    heartbeat_s: int = 0                          # 仅 plain ∧ 非 TTY：每 N 秒一行无数据摘要；
                                                  # 0 = 关（默认，U14——保住回归锚点）；
                                                  # < 0 = CONFIG_ERROR
    estimate: bool = False                        # 仅文本模态：启动期估算扫描，换来批次总数
                                                  # 分母与 ETA（多一趟输入扫描，U17）
    interactive: bool = True                      # rich ∧ stdin 为 TTY ∧ termios：键盘开关
                                                  # （闭集 ? l e + - p q；h=?，§3.4）；
                                                  # false = 只渲染不接键（U15）
    mode_resolved: Literal["rich", "plain"] = "plain"
                                                  # 解析**产物**——由 loader 在 load() 收尾
                                                  # 计算（spec 3.1.4 console 行，U21）：
                                                  # emitter 静态门控所依据的、冻结的 auto
                                                  # 判定结论


@dataclass(frozen=True)
class LLMProfile:
    """一个 [llm.<name>] 端点档案：连接、能力与预算声明（spec 5.1）。"""

    name: str                                     # [llm.<name>] 的键名               [FROZEN HERE]
    # provider 协议族：openai_compatible | anthropic（决定请求/响应形态与结构化输出面）
    provider: Literal["openai_compatible", "anthropic"]
    base_url: str                                 # 端点根地址
    model: str                                    # 模型串（原样发给 provider）
    api_key_env: str                              # 密钥所在的环境变量**名**（值绝不入配置）
    max_concurrency: int = 8                      # ResourceManager 中该档案的逻辑调用容量
    timeout_s: int = 120                          # 单次 HTTP 请求超时（秒）
    max_retries: int = 5                          # 可重试错误的最大重试次数
    retry_base_delay_s: float = 1.0               # 全抖动指数退避的基准延迟（秒）
    supports_structured_output: bool = False      # 端点是否支持厂商结构化输出（M8 的 L0 层）
    supports_vision: bool = False                 # 端点是否收图（vision 必需集据此校验）
    max_output_tokens: int = 4096                 # 单次调用的输出上限，参与预算不变式
    context_window: int = 0                       # v1.11（V6/V26）：模型上下文窗口（token）。
                                                  # 0 = 未声明 = 该档案上下文预算**关**
                                                  # （v1.10 行为不变）；被启用阶段引用而仍为
                                                  # 0 → M1 一条 WARN。
                                                  # > 0 时要求 context_window >
                                                  # max_output_tokens + margin，否则
                                                  # CONFIG_ERROR（预算非正）。声明**部署实测**
                                                  # 窗口，绝不照抄厂商表值（V26——少报永远安全：
                                                  # 只是多裁剪，绝不溢出）
    temperature: float = 0.0                      # 默认温度（可复现性要求默认 0）
    thinking: Literal["enabled", "disabled"] | None = None
                                                  # v1.16：两种 provider 的顶层 thinking 开关；
                                                  # None = 不写请求字段，保持既有请求体形态
    max_image_px: int = 2048                      # 图片长边像素**上限**（V21 升档天花板）
    default_image_px: int = 0                     # v1.11（V18）：图片采样默认**工作点**
                                                  # （长边像素）。0 = 取 max_image_px
                                                  # （与 v1.10 行为逐字节一致）。> 0 须
                                                  # <= max_image_px（CONFIG_ERROR）；V21
                                                  # 升档梯可一路探到 max_image_px
    price_per_mtok_in: float | None = None        # 每百万输入 token 单价（仅用于报告估算）
    price_per_mtok_out: float | None = None       # 每百万输出 token 单价（同上）
    api_key_envs: tuple[str, ...] = ()            # v1.6 密钥池（spec 3.9.3）：TOML 侧
                                                  # api_key_env/api_key_envs 恰填其一；
                                                  # M1 把**两种**写法都归一进本元组
                                                  # （标量 → 单元素元组）；api_key_env
                                                  # 镜像第 0 个元素。v1.17 secret-free：
                                                  # profile 只保存环境变量**名**——
                                                  # 密钥值由 run/probe 期的
                                                  # RuntimeCredentials 物化，绝不入配置


@dataclass(frozen=True)
class EmbeddingProfile:
    """一个 [embedding.<name>] 端点档案：语义去重用的向量化端点（spec 5.1）。"""

    name: str                                     # [embedding.<name>] 的键名        [FROZEN HERE]
    base_url: str                                 # 端点根地址
    model: str                                    # 向量模型串
    api_key_env: str                              # 密钥所在的环境变量**名**
    provider: Literal["openai_compatible"] = "openai_compatible"
                                                  # 协议族（向量侧只支持 openai_compatible）
    max_concurrency: int = 8                      # ResourceManager 中该档案的逻辑调用容量
    timeout_s: int = 60                           # 单次请求超时（秒）
    max_retries: int = 5                          # 最大重试次数
    retry_base_delay_s: float = 1.0               # 退避基准延迟，与 llm.* 同机制 [FROZEN HERE]
    context_window: int = 0                       # v1.11（V15）：0 = 未声明 = 向量预算关；
                                                  # > 0 → 向量输入截到
                                                  # budget = context_window − margin
                                                  # （无输出预留；§7.17 embed_budget）
    dims: int | None = None                       # 若设置，embed() 校验返回维度
    api_key_envs: tuple[str, ...] = ()            # v1.6 密钥池——归一规则同
                                                  # LLMProfile.api_key_envs；v1.17
                                                  # secret-free：只保存环境变量名


# ── project.toml 侧 ────────────────────────────────────────────────────────

@dataclass(frozen=True)
class RunConfig:
    """[run] 节：本次运行的输入/输出、模态、批次与容错阈值（spec 5.2）。"""

    output: str                                   # 主输出路径（其它输出通道由它派生词干）
    modality: Literal["text", "ui"]               # 数据模态：纯文本 / 截图+控件树文件对
    input: str | None = None                      # process 模式必填（CLI --input 可补）；
                                                  # generate_only 下**必须**缺席
    mode: Literal["process", "generate_only"] = "process"   # 运行形态
    batch_size: int = 256                         # = QuRating 的比较池大小
    seed: int = 0                                 # 全部抽样的 PRNG 种子（可复现性）
    fatal_error_threshold: int = 20               # 连续致命 provider 错误达此值即熔断
    max_park_s: int = 3600                        # v1.6（spec 3.9.3/5.2）：整池冷却期间，
                                                  # 单次逻辑 LLM 调用的 park 预算；
                                                  # 0 = 不 park；超支 → 走重试耗尽路径


@dataclass(frozen=True)
class InputConfig:
    """[input] 节：输入解析策略与单条资源上限（spec 5.2）。"""

    text_field: str = "text"                      # 点分路径（例如 "conversation.turns"）
    on_bad_line: Literal["skip", "fail"] = "skip" # 坏行策略
    on_missing_pair: Literal["skip", "fail"] = "skip"       # UI 侧配对缺失策略
    on_index_conflict: Literal["skip", "fail"] = "fail"     # UI 侧序号冲突策略
    max_image_mb: int = 20                        # 单张截图大小上限（MB）
    ui_tree_max_chars: int = 30000                # 控件树线性化进 prompt 的字符上限


@dataclass(frozen=True)
class DedupConfig:
    """[dedup] 节：M3 精确/近似/语义去重参数（spec 5.2）。"""

    enabled: bool = True                          # 去重总开关
    scope: Literal["global", "batch"] = "global"  # 去重作用域：跨批全局 / 仅批内
    minhash_threshold: float = 0.85               # MinHash-LSH 的 Jaccard 判重阈值
    minhash_num_perm: int = 128                   # MinHash 置换数（精度/开销权衡）
    ngram: int = 5                                # 文本切分的 n-gram 长度
    image_phash_max_distance: int = 8             # pHash 汉明距离上限（截图近似判重）
    ui_dup_requires: Literal["both", "tree", "image"] = "both"
                                                  # UI 判重所需证据：双证据 / 只看树 / 只看图
    bounds_quantize_px: int = 4                   # 控件坐标量化粒度（抗渲染抖动）
    semantic: bool = False                        # 是否启用向量语义去重
    semantic_embedding: str | None = None         # semantic=True 时必填；[embedding.*] 档案名
    semantic_threshold: float = 0.95              # 语义相似度判重阈值


@dataclass(frozen=True)
class StreamConfig:
    """v1.8（spec 5.2 [stream]）：输入侧排序 + 会话切分声明，由 M2 消费。

    segment.enabled=false 时声明本节会产生一条 no-op 警告。sequence generation
    不消费本节，其工件时间线只由 SequenceGenerationConfig.timeline 持有。
    """

    order_by: str = "input_order"                 # "input_order" | "meta:<字段>"（仅文本模态）
    on_disorder: Literal["skip", "fail"] = "skip" # 乱序 / 时间戳解析失败的策略
    key: tuple[str, ...] = ()                     # 分区键："meta:<字段>"（文本）| "source_dir"；
                                                  # groupby 语义——键一变即关闭当前会话
    gap_s: int = 300                              # 时间间隔切分；仅 order_by="meta:*" 生效
                                                  # （无 meta:* 排序却显式设置 → M1 警告）；
                                                  # 默认刻意偏大：欠切分还能靠 LLM 精修补救，
                                                  # 过切分补救不回来
    gap_steps: int = 0                            # 序号间隔切分（任何排序键都可）；0 = 关；
                                                  # gap_s 与 gap_steps 可并用——任一触发即切
    session_max_len: int = 200                    # 会话长度硬上限（帧）；> run.batch_size → M1 WARN
    session_max_span_s: int = 0                   # 会话跨度硬上限（仅 meta:*）；0 = 关


@dataclass(frozen=True)
class SegmentConfig:
    """v1.8（spec 5.2 [segment]）：M14 时序切分，也是流模式的总开关。"""

    enabled: bool = False                         # 关 = v1.7 行为（除 _meta.stream 恒 null）
    strategy: Literal["rules", "llm", "hybrid"] = "hybrid"   # 边界判定策略
    llm: str = "default"                          # 仅当 strategy ∈ {llm, hybrid} 时入引用集
                                                  # （S30）；v1.11（V3）：永不入 vision 集——
                                                  # vision 由下方 vision_resolved **自适应**派生
    window: int = 20                              # v1.11（V9）：单次窗口调用的帧数**上限**；
                                                  # M1：>= 2。声明了预算 → 按每帧成本贪心装箱
                                                  # 到此上限（M1 保证 w_min >= 下限，§7.17
                                                  # min_window）；预算关 → 固定窗口，与 v1.10
                                                  # 逐字节一致
    digest_max_chars: int = 400                   # 单帧摘要字符上限
    noise_filter: bool = True                     # 仅 llm/hybrid；rules + true → no-op 警告
    min_len: int = 2                              # 仅作用于经 LLM 精修的片段（S11）
    context: str = ""                             # 可选领域上下文——**不是**边界定义
    on_error: Literal["keep", "fail"] = "keep"    # 修复穷尽时：整会话保留为一个 episode / 失败
    vision_resolved: bool = False                 # v1.11（V1）解析**产物**——永非用户键
                                                  # （ConsoleConfig.mode_resolved 先例）：
                                                  # M1 在 load() 收尾以 dataclasses.replace
                                                  # 冻结为 (modality=="ui") ∧ enabled ∧
                                                  # strategy∈{llm,hybrid} ∧
                                                  # llm_profiles[segment.llm].supports_vision。
                                                  # 注意：旧用户键 `use_vision` 已在 v1.11
                                                  # **移除**——显式书写 [segment] use_vision
                                                  # 是带迁移指引的**定向** CONFIG_ERROR（V2），
                                                  # 而非未知键的前向兼容警告


@dataclass(frozen=True)
class StitchConfig:
    """v1.9（spec 5.2 [stitch]）：M16 线索缝合（默认关，仅流模式）。"""

    enabled: bool = False                         # true ⇒ segment.enabled（M1 约束，T17）
    llm: str = "default"                          # 判定档案；enabled 时入引用集——纯文本证据，
                                                  # **永不**进任何 vision 必需集（T16）
    max_open: int = 4                             # 开放线索池容量（挂起窗口均值 3 + 1 活跃，
                                                  # T8 锚点）
    bias: Literal["conservative", "llm"] = "conservative"
                                                  # conservative = LLM 判"续" **且** 机械先验
                                                  # （T9 合取）；llm = 纯 LLM 判决
    rescue_short: bool = True                     # below_min_len 短串加入候选流（T11）；
                                                  # 救援永不开线索（B-2）
    repass: bool = True                           # 对单碎片线索做有界二遍复评（T19）；
                                                  # false = 纯一遍贪心
    stale_gap_steps: int = 0                      # 序号间隔衰减阈值；0 = 该腿关闭；
                                                  # 一键两用：T9 先验降级 + T8 池满淘汰优先级
                                                  # （与 stream.gap_steps 是两回事）
    digest_max_chars: int = 400                   # 摘要卡内的单帧摘要字符上限
                                                  # （沿用 segment 的键名语义，m-9）
    context: str = ""                             # 可选领域提示（「同一任务」在本域指什么）
    votes: int = 1                                # T18：1 = 单次调用；>1 = n 次采样取
                                                  # (verdict, thread_ref) 的严格多数（M-4；
                                                  # 偶数 = CONFIG_ERROR）
    on_error: Literal["keep", "fail"] = "keep"    # fail 只作用于 episode 候选信封（B-2）


@dataclass(frozen=True)
class ExtractConfig:
    """v1.8（spec 5.2 [extract]）：M15 动作抽取，仅 UI 序列。"""

    enabled: bool = False                         # 要求 segment.enabled + modality="ui"
    llm: str = "default"                          # 恒在 vision 引用集内（S30）
    instruction: str = ""                         # 唯一可经 [class.<name>.extract] 覆盖的键
    include_diff: bool = True                     # 注入[树变更摘要]（可消融，S14）
    on_error: Literal["fallback", "fail"] = "fallback"      # 修复穷尽时的兜底策略


@dataclass(frozen=True)
class ClassSpec:
    """闭集类表的一项：类名 + 判定依据 + 可选 few-shot（spec 5.2）。"""

    name: str                                     # [a-z0-9_]+，表内唯一
    description: str                              # 非空
    examples: tuple[str, ...] = ()                # 可选的输入侧 few-shot 行


@dataclass(frozen=True)
class ClassifyConfig:
    """v1.7（spec 5.2 [classify]）：M13 序列/记录级闭集分类。"""

    enabled: bool = False                         # 关 = v1.6 行为（spec 5.2 v1.7）
    llm: str = "default"                          # UI 模态要求 supports_vision
    assignment: Literal["single", "multi"] = "single"       # 单标签 / 多标签（多标签会扇出）
    max_labels: int | None = None                 # 仅 multi；M1 回填为 len(classes)
    instruction: str = ""                         # 追加在类表之后写进 system
    fallback_class: str = ""                      # enabled 时必填；须 ∈ classes
    self_consistency: int = 0                     # 0 = 关；否则为奇数且 >= 3
    sc_temperature: float = 0.7                   # 仅 sc >= 3 时生效（R21）
    on_error: Literal["fallback", "fail"] = "fallback"      # 修复穷尽时的兜底策略
    classes: tuple[ClassSpec, ...] = ()           # enabled 时要求 >= 2 项


@dataclass(frozen=True)
class QualityConfig:
    """[quality] 节：M4 QuRating 打分与质量门（spec 5.2）。"""

    enabled: bool = True                          # 打分总开关
    mode: Literal["pairwise", "pointwise"] = "pairwise"     # 成对比较 / 逐条打分
    llm: str = "default"                          # 评委档案（judges 为空时的单评委）
    rounds: int = 4                               # 成对模式的 k
    criteria_per_call: Literal["all", "single"] = "all"     # 一次判全部维度还是逐维度
    threshold: float | None = None                # 缺省 = 只打分不过滤
    selection: Literal["threshold", "top_ratio"] = "threshold"      # 选择方式（互斥组）
    top_ratio: float | None = None                # (0,1]；selection="top_ratio" 时必填
    judges: tuple[str, ...] = ()                  # 空 = 单评委（quality.llm）；否则须为奇数个
    both_orders: bool = False                     # 成对比较是否正反两序各判一次（消偏）
    on_unscored: Literal["keep", "drop"] = "keep" # 未评分记录（判决全失败）的去留
    rubric: str = ""                              # "default:text"|"default:ui"|"inline"；
                                                  # "" = 按模态自动选（由 M1 解析）
    judgment_reasons: bool | str = "auto"         # "auto" | True | False


@dataclass(frozen=True)
class GenerateStyle:
    """生成风格表的一项：风格名 + 风格提示（spec 5.2）。"""

    name: str                                      # 表内唯一
    prompt: str                                    # 非空


@dataclass(frozen=True)
class GenerateConfig:
    """M6 通用与 flat 生成面；sequence 专用状态使用独立载体。"""

    enabled: bool = False                         # 生成总开关
    form: Literal["flat", "sequence"] = "flat"  # 两种互斥生成形态
    llms: tuple[str, ...] = ("default",)          # 生成档案表（可多档混合）
    instruction: str = ""                         # flat 启用时必填
    mixture: Literal["round_robin", "weighted"] = "round_robin"     # 多档案混合方式
    weights: tuple[float, ...] = ()               # mixture="weighted" 时必填；len == len(llms)
    styles: tuple[GenerateStyle, ...] = ()        # 可选风格表（逐样本轮转）
    num_per_record: int = 2                       # process 模式：每条种子记录生成几条
    seeds_per_call: int = 3                       # process 模式：每次调用喂几条种子
    num_per_call: int = 4                         # 每次调用产出几个样本
    seed_min_score: float | None = None           # None = 自动（取 quality.threshold，
                                                  # 否则取批内中位数）
    temperature: float = 0.9                      # 生成温度（默认高于判决路径）
    sample_validator: str | None = None           # flat 样本过滤 hook 引用
    seed_examples: tuple[str, ...] = ()           # 仅 generate_only 的种子池形态
    standalone_count: int | None = None           # generate_only 无种子 flat 数量


@dataclass(frozen=True)
class FewShotExample:
    """标注 few-shot 的一条：输入文本 + 期望输出对象（spec 5.2）。"""

    input: str                                    # 示例输入文本
    output: Mapping                               # 须通过用户 Schema（由 M1 校验）


@dataclass(frozen=True)
class AnnotateConfig:
    """[annotate] 节：M5 自动标注（spec 5.2）。"""

    enabled: bool = True                          # 标注总开关
    llm: str = "default"                          # 标注档案（UI 模态入 vision 必需集）
    instruction: str = ""                         # enabled 时必填
    examples: tuple[FewShotExample, ...] = ()     # 可选 few-shot（M1 干跑校验）
    self_consistency: int = 0                     # 0 = 关；否则为奇数且 >= 3
    sc_temperature: float = 0.7                   # 仅自洽采样时生效
    sequence_frames: int = 20                     # v1.8：序列标注的关键帧上限
                                                  # （首帧/末帧恒保留，中间均匀降采样）；
                                                  # M1：2 <= v <= 100（CONFIG_ERROR），
                                                  # > 20 且 max_image_px > 2000 → WARN（S28）


@dataclass(frozen=True)
class VerifyConfig:
    """[verify] 节：M7 LLM-as-a-Judge 评审与修复（spec 5.2）。"""

    enabled: bool = False                         # 评审总开关（要求 annotate 开启）
    llm: str = "judge"                            # enabled 时须存在于 [llm.*]
    judges: tuple[str, ...] = ()                  # 空 = 单评委（verify.llm）；否则须为奇数个
    policy: Literal["drop", "repair"] = "drop"    # 判 fail 后：丢弃 / 进修复环
    max_repair_rounds: int = 1                    # 修复轮次预算（policy="repair"）
    extra_criteria: str = ""                      # 追加评审要点（拼进评审 prompt）


@dataclass(frozen=True)
class OutputConfig:
    """[output] 节：输出 Schema、_meta 形态与 rejects 详略（spec 5.2）。"""

    schema_path: str | None = None                # schema_path / schema_inline 恰填其一；
                                                  # 相对路径相对 project root 解析（v1.17）
    schema_inline: str | None = None              # 内联 JSON Schema 文本
    max_repair_attempts: int = 2                  # Schema 引擎的 L3 修复预算
    repair_llm: str | None = None                 # None = 与调用方同档案
    meta_mode: Literal["inline", "sidecar", "none"] = "inline"      # _meta 落盘形态
    passthrough_fields: tuple[str, ...] = ()      # 从原始行原样透传到输出的字段
    rejects: Literal["none", "refs", "full"] = "refs"       # rejects 通道的详略档位
    validator: str | None = None                  # v1.17 钩子引用 "<python-file>:
                                                  # <attribute-path>"：fn(obj, record|None)
                                                  # -> list[str]，engine L2.5（仅用户
                                                  # Schema，spec 3.8.2）


@dataclass(frozen=True)
class TraceConfig:
    """[trace] 节：可选的 JSONL 事件通道（spec 5.2 / §8.3）。"""

    enabled: bool = False                         # trace 通道总开关（默认关）
    path: str = ""                                # M1 把 "" 解析为 "{output_stem}.trace.jsonl"
    channels: tuple[str, ...] = ("quality", "verify", "schema")
                                                  # 允许值（v1.9 起 11 个）：ingest|dedup|segment|
                                                  # stitch|extract|classify|quality|annotate|verify|
                                                  # schema|llm
    content: Literal["none", "refs", "excerpt", "full"] = "refs"    # 脱敏档位（§8.3）


# ── rubric（附录 A 结构，spec §5.3）────────────────────────────────────────

@dataclass(frozen=True)
class Criterion:
    """评分量表的一个维度（spec §5.3）。"""

    key: str                                      # [a-z0-9_]+，全局唯一
    description: str                              # 维度语义描述
    pairwise_prompt: str                          # 成对比较用的提问文本
    weight: float = 1.0                           # > 0，汇总加权用
    pointwise_levels: tuple[str, ...] = ()        # 逐条模式下恰 6 项（0-5 级）


@dataclass(frozen=True)
class Rubric:
    """一份完整的评分量表：名字 + 维度表（spec §5.3）。"""

    name: str                                     # 量表名（内置或用户自定义）
    criteria: tuple[Criterion, ...]               # 维度表（顺序即渲染顺序）


@dataclass(frozen=True)
class ClassView:
    """v1.7：一个类的生效配置——全局各节与其 [class.<name>.*] 覆盖的合并产物
    （逐键溯源；R6 选择组语义；R7 rubric 重解析）。由 M1 在装载期冻结；
    classify 关闭时通常为空；v1.18 sequence registry 仍会物化。"""

    name: str                                     # 类名（= class_views 的键）
    quality: QualityConfig                        # 选择组已合并（R6）；其中 rubric 字段存的是
                                                  # 该类的生效选择符
    rubric: Rubric                                # 重解析产物（R7）
    annotate: AnnotateConfig                      # 该类的生效标注配置
    generate: GenerateConfig                      # 该类的生效通用/flat 生成配置
    verify: VerifyConfig                          # 该类的生效评审配置
    extract: ExtractConfig                        # v1.8（S3）：只有 `instruction` 在白名单内；
                                                  # segment 没有按类视图（它跑在 classify 之前
                                                  # ——那时标签还不存在）
    schema: Mapping | None = None                 # 按类标注 Schema；None 回落全局
    description: str = ""                         # v1.18 sequence class 描述
    sequence_generation: SequenceClassGenerationConfig | None = None
                                                  # declared 类的世界生成配置


# ── 帧粒度（v1.12，spec §3.1 [frame.classify]/[frame.annotate]/[frame.class.*]）──

@dataclass(frozen=True)
class FrameClassifyConfig:
    """v1.12：M13 帧级闭集分类（默认关；仅流模式——「帧粒度要求流模式」约束）。

    镜像 ClassifyConfig，但没有 assignment/max_labels（帧单一归属是地基，显式书写
    这两个键是定向 CONFIG_ERROR）。
    """

    enabled: bool = False                         # true ⇒ segment.enabled = true（M1 约束）
    llm: str = "default"                          # 判决 profile；enabled 时入引用集；
                                                  # 永不入 vision 必需集（vision 语义分列裁决——
                                                  # 成本控制面 = 指向纯文本 profile）
    fallback_class: str = ""                      # enabled 时必填；须 ∈ [[frame.classify.classes]]
                                                  # （修复穷尽/窗口失败兜底，v1.7 fallback 哲学下推）
    classes: tuple[ClassSpec, ...] = ()           # 帧类表，与 [[classify.classes]] 同构；
                                                  # 与序列类表相互独立、允许重名、互不约束
    vision_resolved: bool = False                 # v1.12 解析产物（segment.vision_resolved 同款，
                                                  # 永非用户键）：M1 于 load() 收尾冻结为
                                                  # (modality=="ui") ∧ enabled ∧
                                                  # llm_profiles[frame.classify.llm].supports_vision


@dataclass(frozen=True)
class FrameAnnotateConfig:
    """v1.12：M5 帧级逐帧标注（默认关；流模式或 sequence 生成）。

    没有 self_consistency（成本 ×n 且投票键须取自帧 Schema——显式书写是定向
    CONFIG_ERROR）。
    """

    enabled: bool = False                         # true ⇒ segment 或 generate.form=sequence
    llm: str = "default"                          # ui ∧ enabled 时无条件入 vision 必需集
                                                  # （截图是标注主证据，镜像序列级 annotate）
    instruction: str = ""                         # 全局帧标注指令；enabled 时必填
    examples: tuple[FewShotExample, ...] = ()     # 可选 few-shot；M1 对帧级 Schema 干跑校验
    schema_path: str | None = None                # 帧级输出 JSON Schema：enabled 时
    schema_inline: str | None = None              # schema_path/schema_inline 恰一
                                                  # （镜像 output.schema 全套分支）


@dataclass(frozen=True)
class FrameClassView:
    """一个帧类的生效标注配置与 v1.18 对象生成契约。

    全局 [frame.annotate] 与 [frame.class.<name>.annotate] 白名单三键的合并产物
    （键 = 帧类名）；M1 装载期冻结。process/flat 由 frame.classify 类表物化，
    sequence 由冻结 frame 注册表物化。
    """

    instruction: str                              # 生效帧标注指令（类覆盖 > 全局）
    examples: tuple[FewShotExample, ...]          # 生效 few-shot（类覆盖 > 全局）
    enabled: bool                                 # false ⇒ 该类成员跳过帧标注（省成本面；
                                                  # 成员在 members[] 呈现 status="skipped"）
    description: str = ""                         # v1.18 帧注册表描述
    gen_instruction: str | None = None            # 完整帧渲染指令
    gen_schema: Mapping | None = None             # 对象根 payload Schema


# ── CLI 覆盖项与总聚合 ──────────────────────────────────────────────────────

@dataclass(frozen=True)
class ResolvedPaths:
    """运行涉及的全部绝对路径（v1.17 SPEC-SP §5.1 / CONTRACTS §7.19.2 冻结块）。

    由 M1 在装载期一次派生；未启用的通道为 None，绝不写相对路径。project_root =
    ``Path(project_path).resolve().parent``，在 project TOML 读取成功后立即冻结。
    """

    project: str                                  # project.toml 绝对路径
    project_root: str                             # 工程根目录绝对路径
    input: str | None                             # 生效输入（process 模式；绝对）
    output: str                                   # 生效主输出（绝对）
    report: str                                   # live "<stem>.report.json" / dry-run
                                                  # "<stem>.dryrun.report.json"（绝对）
    rejects: str | None                           # "<stem>.rejects.jsonl"；通道关闭为 None
    sidecar: str | None                           # "<stem>.meta.jsonl"；非 sidecar 形态为 None
    trace: str | None                             # 追踪文件（绝对）；通道关闭为 None
    stream: str | None                            # sequence stream JSONL；形态关闭为 None
    manifest: str | None                          # manifest-last 成功标记
    failed_report: str | None                     # 独立且无内容的失败报告


@dataclass(frozen=True)
class CliOverrides:
    """CLI 侧覆盖项；优先级最高（CLI > project.toml > config.toml）。"""

    input: str | None = None                      # --input：覆盖 run.input
    output: str | None = None                     # --output：覆盖 run.output
    limit: int | None = None                      # --limit：只处理前 N 条
    dry_run: bool = False                         # --dry-run：只估算不发调用
    strict: bool = False                          # --strict：存在 rejects 即退出码 1
    log_level: str | None = None                  # --log-level：覆盖 tool.log_level
    console: str | None = None                    # v1.10 面板模式：--console auto|rich|plain
                                                  # （spec §7.7；argparse 的 choices 已预校验取值）


@dataclass(frozen=True)
class ResolvedConfig:
    """M1 的最终产物：合并并校验后的整套不可变配置（spec 3.1）。"""

    tool: ToolConfig                              # 工具级日志设置
    console: ConsoleConfig                        # v1.10（spec 5.1 [console]）；mode_resolved
                                                  # 由 M1 于 load() 收尾冻结（3.1.4，U21）
    llm_profiles: Mapping[str, LLMProfile]        # 键 = 档案名
    embedding_profiles: Mapping[str, EmbeddingProfile]      # 键 = 向量档案名
    run: RunConfig                                # [run] 节
    input: InputConfig                            # [input] 节
    stream: StreamConfig                          # v1.8
    dedup: DedupConfig                            # [dedup] 节
    segment: SegmentConfig                        # v1.8
    stitch: StitchConfig                          # v1.9
    extract: ExtractConfig                        # v1.8
    classify: ClassifyConfig                      # v1.7；max_labels 由 M1 回填
    quality: QualityConfig                        # [quality] 节
    generate: GenerateConfig                      # [generate] 节
    annotate: AnnotateConfig                      # [annotate] 节
    verify: VerifyConfig                          # [verify] 节
    output: OutputConfig                          # [output] 节
    trace: TraceConfig                            # [trace] 节
    rubric: Rubric                                # 已解析（内置包数据或 inline）
    class_views: Mapping[str, ClassView]          # 键 = 类名；process/flat 随 classify 物化，
                                                  # sequence 随冻结 sequence class 注册表物化
    user_schema: Mapping                          # 已解析的 dict，元 Schema 预校验通过
    limit: int | None                             # CLI --limit
    strict: bool                                  # CLI --strict
    dry_run: bool                                 # CLI --dry-run
    config_path: str                              # CLI 上给定的原样路径
    project_path: str                             # 同上（project.toml）
    config_digest: str                            # 原始文件字节的 "sha256:<hex>"  [FROZEN HERE]
    project_digest: str                           # 同上（project.toml）
    # v1.12 帧粒度四字段：带默认值（有意偏离 stream/stitch 的 R23「必填无默认」惯例——
    # 全关默认 = 字节等价 v1.11，既有 ResolvedConfig 构造点零波及；loader 恒显式传入）。
    # 默认字段须列于全部必填字段之后，故置于尾部。
    frame_classify: FrameClassifyConfig = FrameClassifyConfig()
                                                  # v1.12；vision_resolved 由 M1 于 load() 收尾冻结
    frame_annotate: FrameAnnotateConfig = FrameAnnotateConfig()
                                                  # v1.12：[frame.annotate] 节
    frame_class_views: Mapping[str, FrameClassView] = field(default_factory=dict)
                                                  # 键 = 帧类名；process/flat 随 frame.classify
                                                  # 物化，sequence 随冻结 frame 注册表物化
    frame_schema: Mapping | None = None           # v1.12：帧级输出 Schema 解析产物（user_schema
                                                  # 同胞：元校验 + few-shot 干跑）；frame.annotate
                                                  # 关闭时恒 None
    sequence_generation: SequenceGenerationConfig | None = None
                                                  # form=sequence 时唯一专用配置载体
    paths: ResolvedPaths | None = None
                                                  # v1.17（SPEC-SP §5.1）：全部绝对路径的
                                                  # 冻结 parse product；生产 loader 恒显式填充
    validation_hooks: "ValidationHooks | None" = None
                                                  # output/sample/state 冻结 hook 集
