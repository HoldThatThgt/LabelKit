"""config.toml + project.toml + CLI 覆盖项的定型冻结镜像（spec ch.5）。"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal, Mapping, Sequence


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
    max_concurrency: int = 8                      # 每档案一个 Semaphore 的并发上限
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
    api_key: str = field(default="", repr=False)  # 由 M1 从环境变量解析；**绝不**入日志
                                                  # （冻结面 [FROZEN HERE]）
    api_key_envs: tuple[str, ...] = ()            # v1.6 密钥池（spec 3.9.3）：TOML 侧
                                                  # api_key_env/api_key_envs 恰填其一；
                                                  # M1 把**两种**写法都归一进本元组
                                                  # （标量 → 单元素元组）；api_key_env
                                                  # 镜像第 0 个元素
    api_keys: tuple[str, ...] = field(default=(), repr=False)
                                                  # v1.6：与 api_key_envs 对齐的解析值；
                                                  # **绝不**入日志；api_key 镜像第 0 个元素


@dataclass(frozen=True)
class EmbeddingProfile:
    """一个 [embedding.<name>] 端点档案：语义去重用的向量化端点（spec 5.1）。"""

    name: str                                     # [embedding.<name>] 的键名        [FROZEN HERE]
    base_url: str                                 # 端点根地址
    model: str                                    # 向量模型串
    api_key_env: str                              # 密钥所在的环境变量**名**
    provider: Literal["openai_compatible"] = "openai_compatible"
                                                  # 协议族（向量侧只支持 openai_compatible）
    max_concurrency: int = 8                      # 并发上限（与 llm.* 同机制）
    timeout_s: int = 60                           # 单次请求超时（秒）
    max_retries: int = 5                          # 最大重试次数
    retry_base_delay_s: float = 1.0               # 退避基准延迟，与 llm.* 同机制 [FROZEN HERE]
    context_window: int = 0                       # v1.11（V15）：0 = 未声明 = 向量预算关；
                                                  # > 0 → 向量输入截到
                                                  # budget = context_window − margin
                                                  # （无输出预留；§7.17 embed_budget）
    dims: int | None = None                       # 若设置，embed() 校验返回维度
    api_key: str = field(default="", repr=False)  # 由 M1 从环境变量解析
    api_key_envs: tuple[str, ...] = ()            # v1.6 密钥池——归一规则同
                                                  # LLMProfile.api_key_envs
    api_keys: tuple[str, ...] = field(default=(), repr=False)   # v1.6；**绝不**入日志


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

    segment.enabled=false 时声明本节 → 一条 no-op 警告。v1.13 起本节同时充当
    时间流**生成侧的铺设契约**（order_by 命名工件时间戳键、gap_s 兜底会话间隔、
    session_max_len 封顶交织）。
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

    name: str                                     # 表内唯一
    prompt: str                                   # 非空


@dataclass(frozen=True)
class CorrelationSpec:
    """v1.16 序列规则的类型敏感相等关联条件。"""

    operator: Literal["equal"] = "equal"         # 关联操作符，首版只允许 equal
    source_field: str = ""                        # source 帧类 Schema 的顶层属性名
    target_field: str = ""                        # target 帧类 Schema 的顶层属性名


@dataclass(frozen=True)
class SequenceRuleSpec:
    """v1.16 一条序列规则的冻结配置镜像。"""

    template: str                                  # 15 个 DECLARE 模板之一
    frame_class: str | None = None                # 一元模板的帧类
    source: str | None = None                      # 二元模板的 source 帧类
    target: str | None = None                      # 二元模板的 target 帧类
    count: int | None = None                       # existence/absence/exactly 的正整数
    time_s: tuple[float, float] | None = None      # 半开秒区间 [lo, hi)
    correlation: CorrelationSpec | None = None     # 可选的 typed equality 条件


@dataclass(frozen=True)
class SequenceWindowSpec:
    """v1.16 一条帧类日历窗口的冻结配置镜像。"""

    frame_class: str                              # 被约束的帧类
    of_day: tuple[tuple[str, str], ...]            # 同日半开墙钟窗口表
    of_week: tuple[str, ...] = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
                                                   # 允许的星期，缺省为全周


@dataclass(frozen=True)
class TierSpec:
    """v1.14（裁决·档位即帧类构成）：``[[generate.stream.tiers]]`` 档位表的一项。

    档位的定义**就是**该档序列的帧类构成集合——不携带质量指令、不控制帧内部语义
    质量（那归各帧类的生成指令与温度）。
    """

    tier_rank: int                                # 档位序数（第几档的要求）：正整数、
                                                  # 表内唯一、**每张生效表各自**连续
                                                  # 覆盖 1..N（v1.15 裁决·rank 类内
                                                  # 身份——按类表在场时 N 逐类可不同，
                                                  # 跨类同 rank 无工具语义）；也是配分
                                                  # 平票与类内序数分块的确定性排序
                                                  # 依据。工具**不赋予**序数高低任何
                                                  # 质量方向语义（方向归用户）
    weight: int                                   # 配额权重：整数 >= 1；类配额按整数域
                                                  # 最大余额法在各档间零抽签配分
                                                  # （apportion_tiers）
    frame_classes: tuple[str, ...]                # 档位构成：该档序列**恰用**这些帧类
                                                  # （enum 给「⊆」、contains 给「⊇」）；
                                                  # 非空、档内互异、名 ∈ 帧类表、各档
                                                  # 构成集合两两互异（M1 校验）


def apportion_tiers(sequences: int, tiers: Sequence[TierSpec]) -> tuple[int, ...]:
    """v1.14（裁决·零抽签配分）：把一个序列类的配额按权重配到各档。

    整数域最大余额法（算术冻结，**禁止任何浮点中间量**——平票判定要一路喂给类内序数
    分块 → truth → 工件字节 → 成员 id，是冻结面，不能悬在浮点比较语义上）：
    基额 = ``(sequences × weight) // Σweight``、余额键 =
    ``(sequences × weight) mod Σweight``，按余额键降序、平票按 ``tier_rank`` 升序逐档
    +1，直至 Σ逐档 = sequences。是 ``(sequences, tiers)`` 的纯函数、零 rng——冻结的抽签
    消费顺序表原文不动。落点在 common 而非 operators：M1 的逐非零配额对约束与 M6 计划
    期共用同一实现，而分层纪律不许 common 依赖 operators（M6 反向导入）。

    @param sequences 该类的序列尝试配额（>= 0；0 = 该类不参与，逐档得 0）
    @param tiers 该类的**生效**档位表（v1.15：按类表 ?? 全局表，取自
                 ``effective_tiers``），调用方按 ``tier_rank`` 升序传入
                 （``GenerateStreamConfig.tiers`` / ``ClassView.tiers`` 的存放序即
                 此序），每档 ``weight >= 1``（M1 解析期强制）
    @return 与入参同序（即 ``tier_rank`` 升序）的逐档配额元组；空档位表返回空元组
    """
    if not tiers:
        return ()
    total_weight = sum(spec.weight for spec in tiers)
    scaled = [sequences * spec.weight for spec in tiers]
    quotas = [value // total_weight for value in scaled]
    remainders = [value % total_weight for value in scaled]
    order = sorted(range(len(tiers)),
                   key=lambda i: (-remainders[i], tiers[i].tier_rank))
    for i in order[:sequences - sum(quotas)]:
        quotas[i] += 1
    return tuple(quotas)


def effective_tiers(class_tiers: tuple[TierSpec, ...] | None,
                    global_tiers: tuple[TierSpec, ...]) -> tuple[TierSpec, ...]:
    """v1.15（裁决·表级原子覆盖 + 裁决·全局表为锚）：取一个序列类的生效档位表。

    表级原子覆盖——类声明了就用类的**整张表**，未声明（None）回落全局表；不做行级
    合并（行级合并会让 rank 身份跨表漂移）。全局表为锚 ⇒ 档位面开关恒 = 全局表非空，
    每个参与类恒有生效表。落点在 common 而非 operators：M1 约束簇、M6 计划期与 M10
    报表装配三方共用同一实现，而分层纪律不许 common 依赖 operators（M6/M10 反向导入，
    ``apportion_tiers`` 同款）。

    @param class_tiers 该类的 ``[[class.<name>.generate.tiers]]`` 解析产物；
                       None = 未声明（回落）；空元组 = 显式空表（M1 拒收，此处原样返回）
    @param global_tiers 全局 ``[[generate.stream.tiers]]`` 档位表
    @return 该类的生效档位表（按 ``tier_rank`` 升序）
    """
    return global_tiers if class_tiers is None else class_tiers


def effective_rules(class_rules: tuple[SequenceRuleSpec, ...] | None,
                    global_rules: tuple[SequenceRuleSpec, ...]) -> tuple[SequenceRuleSpec, ...]:
    """v1.16 按类 rules 的三态整表查找。

    @param class_rules 按类声明的整张表；``None`` 表示继承，空元组表示显式清空
    @param global_rules 全局整张规则表
    @return 该序列类的生效规则表
    """
    return global_rules if class_rules is None else class_rules


def effective_windows(class_windows: tuple[SequenceWindowSpec, ...] | None,
                      global_windows: tuple[SequenceWindowSpec, ...]) -> tuple[SequenceWindowSpec, ...]:
    """v1.16 按类 windows 的三态整表查找。

    @param class_windows 按类声明的整张表；``None`` 表示继承，空元组表示显式清空
    @param global_windows 全局整张窗口表
    @return 该序列类的生效窗口表
    """
    return global_windows if class_windows is None else class_windows


@dataclass(frozen=True)
class GenerateStreamConfig:
    """v1.13（spec 5.2 [generate.stream]）：generate_only 的时间流形态。

    LLM 只做蓝图与帧实现两类内容调用，装箱/交叉/噪音/重复/时间戳全部由机械交织器
    完成。默认关；全关 ⇒ 与 v1.12 字节等价。
    """

    enabled: bool = False                         # 形态总开关；true ⇒ generate_only ∧ text
                                                  # ∧ generate.enabled ∧ classify.enabled
                                                  # ∧ stream.order_by = "meta:<字段>"
                                                  # ∧ output.meta_mode != "none"（M1 硬合取）
    sessions: int = 0                             # 会话数（≥ 1）；交叉会话数 =
                                                  # Σsequences − sessions，故 M1 要求
                                                  # sessions ≤ Σsequences ≤ 2 × sessions
                                                  # （交叉并发度恒 k ∈ {1, 2}）
    noise_ratio: float = 0.0                      # 噪音帧 / 任务帧 比例，[0,1)；
                                                  # 噪音帧数 = round(noise_ratio × 任务帧数)
    noise_instruction: str = ""                   # 噪音帧生成指令；noise_ratio > 0 时必填非空
    duplicates: int = 0                           # 原样重发的序列条数（0 = 无；≤ Σsequences）
                                                  # ——重发帧逐字节同源，恒落流尾新会话
    frame_gap_s: tuple[float, float] = (5.0, 60.0)
                                                  # 会话内帧间隔的均匀采样区间（秒）；字段本身
                                                  # 只承载数值闭区间，不承载条件边界校验。
                                                  # M1 按路径裁决：v1.15 默认路径，以及仅有
                                                  # sequence_validator、无实际非零 rules/windows
                                                  # 前缀的路径，要求 1e-6 ≤ lo ≤ hi < stream.gap_s；
                                                  # 仅 --limit 后实际非零配额前缀有生效 rules/windows
                                                  # 的 v1.16 联合路径允许 hi == stream.gap_s。
    ts_start: str = "2026-01-01T00:00:00Z"        # 时间流起点（ISO-8601；恒不取墙钟——
                                                  # 同 seed 双跑工件逐字节一致）
    tiers: tuple[TierSpec, ...] = ()              # v1.14 档位表（[[generate.stream.tiers]]），
                                                  # 按 tier_rank 升序存放；空元组 = 档位面
                                                  # 整体不在场（字节等价 v1.13）
    rules: tuple[SequenceRuleSpec, ...] = ()      # v1.16 全局序列规则表
    windows: tuple[SequenceWindowSpec, ...] = ()  # v1.16 全局帧类日历窗口表


@dataclass(frozen=True)
class GenerateConfig:
    """[generate] 节：M6 数据生成（默认关；三种形态共用本节）。"""

    enabled: bool = False                         # 生成总开关
    llms: tuple[str, ...] = ("default",)          # 生成档案表（可多档混合）
    instruction: str = ""                         # enabled 时必填（v1.13：时间流形态改由
                                                  # 各帧类的 instruction 承载任务描述）
    mixture: Literal["round_robin", "weighted"] = "round_robin"     # 多档案混合方式
    weights: tuple[float, ...] = ()               # mixture="weighted" 时必填；len == len(llms)
    styles: tuple[GenerateStyle, ...] = ()        # 可选风格表（逐样本轮转）
    num_per_record: int = 2                       # process 模式：每条种子记录生成几条
    seeds_per_call: int = 3                       # process 模式：每次调用喂几条种子
    num_per_call: int = 4                         # 每次调用产出几个样本
    seed_min_score: float | None = None           # None = 自动（取 quality.threshold，
                                                  # 否则取批内中位数）
    temperature: float = 0.9                      # 生成温度（默认高于判决路径）
    sample_validator: str | None = None           # v1.5 plan-A 钩子 "module:function"：
                                                  # fn(text) -> list[str]，样本级过滤
                                                  # （相似度过滤之前，spec 3.6.2）
    sequence_validator: str | None = None          # v1.16 序列级钩子 "module:function"
    seed_examples: tuple[str, ...] = ()           # 仅 generate_only 的种子池形态
    standalone_count: int | None = None           # 仅 generate_only 的无种子形态；与
                                                  # seed_examples 互斥
    sequences: int = 0                            # v1.13 时间流形态：该类的序列**尝试配额**
                                                  # （全局设默认、[class.<name>.generate]
                                                  # 覆盖）；0 = 该类不参与生成
    len_range: tuple[int, int] = (3, 6)           # v1.13 时间流形态：单序列步数的均匀采样
                                                  # 区间（1 ≤ lo ≤ hi；类覆盖照常）


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

    schema_path: str | None = None                # schema_path / schema_inline 恰填其一
    schema_inline: str | None = None              # 内联 JSON Schema 文本
    max_repair_attempts: int = 2                  # Schema 引擎的 L3 修复预算
    repair_llm: str | None = None                 # None = 与调用方同档案
    meta_mode: Literal["inline", "sidecar", "none"] = "inline"      # _meta 落盘形态
    passthrough_fields: tuple[str, ...] = ()      # 从原始行原样透传到输出的字段
    rejects: Literal["none", "refs", "full"] = "refs"       # rejects 通道的详略档位
    validator: str | None = None                  # v1.5 plan-A 钩子 "module:function"：
                                                  # fn(obj, record|None) -> list[str]，
                                                  # engine L2.5（仅用户 Schema，spec 3.8.2）


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
    classify 关闭时 ResolvedConfig.class_views == {}。"""

    name: str                                     # 类名（= class_views 的键）
    quality: QualityConfig                        # 选择组已合并（R6）；其中 rubric 字段存的是
                                                  # 该类的生效选择符
    rubric: Rubric                                # 重解析产物（R7）
    annotate: AnnotateConfig                      # 该类的生效标注配置
    generate: GenerateConfig                      # 该类的生效生成配置（含 v1.13 序列配额）
    verify: VerifyConfig                          # 该类的生效评审配置
    extract: ExtractConfig                        # v1.8（S3）：只有 `instruction` 在白名单内；
                                                  # segment 没有按类视图（它跑在 classify 之前
                                                  # ——那时标签还不存在）
    schema: Mapping | None = None                 # v1.13（裁决·按类标注 Schema）：该类的
                                                  # 标注输出 Schema——[class.<name>.annotate]
                                                  # 的 schema_path/schema_inline 解析产物
                                                  # （至多其一）；None = 回落全局
                                                  # output.schema（覆盖语义，rubric 按类
                                                  # 重资产先例）
    tiers: tuple[TierSpec, ...] | None = None     # v1.15（裁决·表级原子覆盖 + 裁决·载体
                                                  # ClassView 顶层字段）：该类的档位表
                                                  # （[[class.<name>.generate.tiers]]），按
                                                  # tier_rank 升序存放。None = 未声明 ⇒ 回落
                                                  # 全局 [[generate.stream.tiers]]（生效表查
                                                  # 找恒经 effective_tiers）；空元组 = 显式
                                                  # 空表 ⇒ M1 拒收。**不落** GenerateConfig：
                                                  # 档位不改变任何调用数，dry-run 的按类覆盖
                                                  # 注记不应因它触发（裁决·note 行不因档位
                                                  # 触发）
    rules: tuple[SequenceRuleSpec, ...] | None = None
                                                  # v1.16：按类 rules；None = 继承全局，
                                                  # 空元组 = 显式清空
    windows: tuple[SequenceWindowSpec, ...] | None = None
                                                  # v1.16：按类 windows；三态同 rules


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
    """v1.12：M5 帧级逐帧标注（默认关；仅流模式）。

    没有 self_consistency（成本 ×n 且投票键须取自帧 Schema——显式书写是定向
    CONFIG_ERROR）。
    """

    enabled: bool = False                         # true ⇒ segment.enabled = true（M1 约束）
    llm: str = "default"                          # ui ∧ enabled 时无条件入 vision 必需集
                                                  # （截图是标注主证据，镜像序列级 annotate）
    instruction: str = ""                         # 全局帧标注指令；enabled 时必填
    examples: tuple[FewShotExample, ...] = ()     # 可选 few-shot；M1 对帧级 Schema 干跑校验
    schema_path: str | None = None                # 帧级输出 JSON Schema：enabled 时
    schema_inline: str | None = None              # schema_path/schema_inline 恰一
                                                  # （镜像 output.schema 全套分支）


@dataclass(frozen=True)
class FrameClassView:
    """v1.12：一个帧类的生效标注配置（v1.13 起兼载该帧类的生成面）。

    全局 [frame.annotate] 与 [frame.class.<name>.annotate] 白名单三键的合并产物
    （键 = 帧类名）；M1 装载期冻结；frame.classify 关闭时 frame_class_views == {}。
    """

    instruction: str                              # 生效帧标注指令（类覆盖 > 全局）
    examples: tuple[FewShotExample, ...]          # 生效 few-shot（类覆盖 > 全局）
    enabled: bool                                 # false ⇒ 该类成员跳过帧标注（省成本面；
                                                  # 成员在 members[] 呈现 status="skipped"）
    gen_instruction: str | None = None            # v1.13（裁决·帧类生成面）：该帧类的内容
                                                  # 生成指令（[frame.class.<name>.generate]
                                                  # .instruction）；None = 未声明——时间流
                                                  # 生成形态下每个帧类都必填（M1 校验）
    gen_schema: Mapping | None = None             # v1.13：该帧类的生成 Schema 解析产物
                                                  # （至多其一的 schema_path/schema_inline）；
                                                  # None = 纯文本帧（帧内容直取文本）
    time_fields: Mapping[str, str] | None = None  # v1.14（裁决·绑定即剔除）：时间语义字段
                                                  # 绑定表（[frame.class.<name>.generate
                                                  # .time_fields]）——键 = 生成 Schema 顶层
                                                  # 字段名, 值 ∈ 语义词表 {ts, gap_prev_s,
                                                  # gap_next_s, elapsed_s}; 绑定字段从
                                                  # LLM 面向的逐位 Schema 与契约行中剔除,
                                                  # 值由机械回填尾声按已铺时间轴写回。
                                                  # None = 无绑定


# ── CLI 覆盖项与总聚合 ──────────────────────────────────────────────────────

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
    class_views: Mapping[str, ClassView]          # v1.7：键 = 类名；非 classify.enabled 时为 {}
                                                  # （R23：仍然不给默认值）
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
                                                  # v1.12：键 = 帧类名；仅 frame.classify.enabled
                                                  # 时物化（零覆盖类也各得一份视图，class_views 同款）
    frame_schema: Mapping | None = None           # v1.12：帧级输出 Schema 解析产物（user_schema
                                                  # 同胞：元校验 + few-shot 干跑）；frame.annotate
                                                  # 关闭时恒 None
    generate_stream: GenerateStreamConfig = GenerateStreamConfig()
                                                  # v1.13：时间流生成形态（默认关 = 字节等价
                                                  # v1.12；沿用 v1.12 帧粒度四字段的
                                                  # 「尾部追加带默认」惯例）
