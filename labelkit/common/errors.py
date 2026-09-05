"""异常体系（spec §4.3）与错误分类码（spec §7.6）。"""
from __future__ import annotations

import enum
from typing import Literal


class LabelKitError(Exception):
    """本工具全部异常的基类。"""


class GenerationProjectionMismatch(Exception):
    """表示当前 sequence attempt 的最终视图与冻结计划不一致。"""

    def __init__(self, reason: str):
        """构造不含用户内容的可恢复投影拒绝。

        @param reason 固定的英文机械检查原因。
        """
        self.reason = reason
        super().__init__(f"sequence projection mismatch: {reason}")


class ConfigError(LabelKitError):
    """M1。聚合**全部**校验错误（绝不只报第一条）。CLI 退出码 2。"""

    def __init__(self, errors: list[str]):
        """聚合校验错误列表并拼成单条消息。

        @param errors 全部校验错误文本（英文），按发现顺序
        """
        self.errors = errors
        super().__init__("\n".join(errors))


class InputError(LabelKitError):
    """M2，在某项 input.* 策略为 'fail' 时抛出（或无任何有效记录 / 运行开始时路径缺失）。
    仅 process 模式。CLI 退出码 3。"""

    def __init__(self, message: str):
        """构造输入级致命错误。

        @param message 英文错误消息（不含数据内容）
        """
        super().__init__(message)


class ProviderRetryableError(LabelKitError):
    """M9：可重试的 provider 错误且重试已耗尽（v1.6：含 park 预算超支，run.max_park_s）。
    记录级 → status='failed'。"""

    def __init__(self, message: str, profile: str, retries: int,
                 key_env: str | None = None):
        """构造重试耗尽错误。

        @param message 英文错误消息
        @param profile 发生错误的 [llm.*] / [embedding.*] profile 名
        @param retries 已消耗的重试次数
        @param key_env v1.6：最后一次尝试的密钥的环境变量**名**（密钥池），绝不含密钥值
        """
        self.profile = profile
        self.retries = retries
        # v1.6：密钥池下最后尝试的那把密钥的环境变量名（只有名，没有值）
        self.key_env = key_env
        super().__init__(message)


class ProviderFatalError(LabelKitError):
    """M9：不可重试的 provider 错误（401/403/400/404、维度不匹配）。喂给熔断器；
    连续计数 >= run.fatal_error_threshold 即以退出码 4 结束运行。
    v1.6 密钥池：被密钥轮换吸收掉的鉴权失败什么都不抛——鉴权类只有在**最后一把存活密钥**
    被停用时才抛本异常（spec 3.9.3）。"""

    def __init__(self, message: str, profile: str, status_code: int | None = None,
                 key_env: str | None = None):
        """构造 provider 致命错误。

        @param message 英文错误消息
        @param profile 发生错误的 profile 名
        @param status_code HTTP 状态码（无 HTTP 交互时为 None）
        @param key_env v1.6：出错密钥的环境变量**名**（密钥池），绝不含密钥值
        """
        self.profile = profile
        self.status_code = status_code
        # v1.6：出错的那把密钥的环境变量名（只有名，没有值）
        self.key_env = key_env
        super().__init__(message)


class ContextOverflowError(LabelKitError):
    """v1.11（V16/V24）：统一的上下文溢出信号。记录级 → status='failed'、
    kind='context_overflow'（§7.6）→ rejects；运行继续。

    phase='precheck'——M9 派发前的不变式检查命中（V16，零 provider 交互），
    或某装箱层发现连最小语义单元都塞不下（V10——由算子直接记账，无异常穿越）；
    phase='reactive'——真实的 provider 交互识别出溢出：预算门控的 400 响应体嗅探命中，
    或 200 形态的 `model_context_window_exceeded` 终止（V20/V24）。M9 自身对本异常
    **绝不**调用 `record_provider_result(fatal=True)`，也不消耗常规重试——reactive-400
    终态由**归属算子**在其有界降级重试耗尽后恰好喂一次（A7；§7.8 熔断矩阵）。

    ``origin``（SPEC-context-budget §3.5 熔断矩阵）携带算子否则看不见的 reactive 形态
    区分：M9 对 400 嗅探形态发的 `llm.call` 是 status="fatal"，而 200 形态是 status="ok"，
    只有 "http_400" 终态可以喂熔断器——"finish" 形态搭乘的是一次成功 HTTP 交互，其 ok
    已经清空了连续计数。仅当 phase="reactive" 时有意义；追加式尾部关键字参数（带默认值——
    早于本参数的构造点依旧有效）。"""

    def __init__(self, message: str, phase: Literal["precheck", "reactive"],
                 profile: str | None = None,
                 origin: Literal["http_400", "finish"] = "http_400"):
        """构造上下文溢出信号。

        @param message 英文错误消息
        @param phase 'precheck' = 派发前不变式命中；'reactive' = 真实交互识别出溢出
        @param profile 相关 profile 名（追加式载体，可缺省）
        @param origin reactive 形态区分：'http_400' 可喂熔断器，'finish' 不可
        """
        self.phase = phase
        # 追加式载体（尾部关键字参数）
        self.profile = profile
        self.origin = origin
        super().__init__(message)


class OutputTruncatedError(LabelKitError):
    """v1.11（V11）：响应因撞上输出上限而终止——finish_reason='length'（openai）/
    stop_reason='max_tokens'（anthropic）：输入本身塞得下窗口，是模型把 max_output_tokens
    写满了。记录级 → status='failed'、kind='output_truncated' → rejects（独立桶）；
    被截断的文本**绝不**进入 L1–L3 修复环，也绝不喂熔断器（HTTP 交互是成功的——
    `llm.call` 仍为 status='ok'）。"""

    def __init__(self, message: str, profile: str | None = None,
                 finish: str | None = None):
        """构造输出截断信号。

        @param message 英文错误消息
        @param profile 相关 profile 名（追加式载体）
        @param finish provider 返回的原始终止原因串（追加式载体）
        """
        # 追加式载体（尾部关键字参数）
        self.profile = profile
        self.finish = finish
        super().__init__(message)


class SchemaViolation(LabelKitError):
    """M8：L3 预算耗尽，对象仍不合法。记录级 → status='failed'、kind='schema_violation'
    ——当剩余违规**全部**来自 output.validator 钩子时改为 'callback_violation'
    （callback_only=True，spec 3.8.2 L2.5）。"""

    def __init__(self, errors: list[str], raw_last_output: str, *,
                 callback_only: bool = False):
        """构造 Schema 违规错误。

        @param errors 渲染后的违规条目 "<json-pointer>: <message>"
        @param raw_last_output 最后一次 LLM 原始输出（用于 rejects 取证）
        @param callback_only 剩余违规是否全部来自 L2.5 用户回调
        """
        # 渲染后的违规条目："<json-pointer>: <message>"
        self.errors = errors
        self.raw_last_output = raw_last_output
        self.callback_only = callback_only
        super().__init__("; ".join(errors))


class InternalError(LabelKitError):
    """不变式被破坏（例如 M11 收尾的 validate_only 失败）。记录级 → 'failed'、
    kind='internal_error'；调用栈以 debug 级别写入 stderr 日志。"""


class PostprocessorError(InternalError):
    """工程后处理函数违反同步 JSON 返回契约的脱敏内部错误。"""

    def __init__(self) -> None:
        """构造不包含业务数据或原始函数异常的固定错误。

        @return 无。
        """
        super().__init__("postprocessor_error")


class DeliveryError(LabelKitError):
    """v1.18 sequence 精确交付尝试耗尽；CLI 退出码 1。"""

    def __init__(self, kind: str, slot_key: str, attempts_used: int):
        """构造不含状态、payload、prompt 或 provider 内容的交付错误。

        @param kind 冻结交付错误分类
        @param slot_key 耗尽的槽键
        @param attempts_used 已使用尝试数
        """
        self.kind = kind
        self.slot_key = slot_key
        self.attempts_used = attempts_used
        super().__init__(f"{kind}: slot={slot_key} attempts={attempts_used}")


class CircuitBreakerTripped(LabelKitError):
    """MetricsSink.circuit_broken 置位后由 LLMClient 抛出；ProcessWorkflow 将其转为
    致命的运行结束（退出码 4）。[FROZEN HERE]"""


# ── CLI 退出码（spec §2.4）─────────────────────────────────────────────────
EXIT_OK = 0              # 运行完成（允许存在 rejects）
EXIT_STRICT = 1          # --strict、report 写失败或 sequence delivery exhaustion
EXIT_CONFIG = 2          # ConfigError
EXIT_INPUT = 3           # InputError（仅 process 模式；generate_only 永不返回 3）
EXIT_FATAL = 4           # provider 鉴权失败 / 熔断 / 输出路径不可写


class ErrorKind(str, enum.Enum):
    """StageError.kind 的取值（spec §7.6）。比较与序列化一律用 .value。"""

    BAD_INPUT_LINE = "bad_input_line"                        # M2，记录级
    MISSING_PAIR = "missing_pair"                            # M2，记录级
    INDEX_CONFLICT = "index_conflict"                        # M2，记录级
    IMAGE_TOO_LARGE = "image_too_large"                      # M2，记录级
    IMAGE_DECODE_ERROR = "image_decode_error"                # M3 跳过 pHash；M5/M7 → failed
    CLASSIFICATION_INVALID = "classification_invalid"        # v1.7：M13，M8 修复穷尽——
                                                             # fallback 保留记录；"fail" → rejects
    SEGMENTATION_INVALID = "segmentation_invalid"            # v1.8：M14，M8 修复穷尽——
                                                             # "keep" = 整个会话作为一个 episode
                                                             # 存活（痕迹在 _meta.stream.degraded）；
                                                             # "fail" = 会话成员 failed → rejects
    EXTRACTION_INVALID = "extraction_invalid"                # v1.8：M15，M8 修复穷尽——
                                                             # "fallback" = 步骤记录取
                                                             # action_type="other"（痕迹在
                                                             # Transition.detail，不入 item.errors）；
                                                             # "fail" = 该 episode failed → rejects
    STITCH_INVALID = "stitch_invalid"                        # v1.9：M16，M8 修复穷尽——
                                                             # "keep" = 该 episode 候选自开线索
                                                             # （取证走 error 事件 + stitch.failures，
                                                             # 绝不入 item.errors）；
                                                             # "fail" = episode 候选信封
                                                             # failed → rejects（成员帧仍 absorbed；
                                                             # 救援候选永不走 fail 路径，B-2）
    JUDGMENT_INVALID = "judgment_invalid"                    # M4，比较级 → 记为平局
    SCHEMA_VIOLATION = "schema_violation"                    # M8 L3 穷尽 → failed → rejects
    CALLBACK_VIOLATION = "callback_violation"                # v1.5：L3 穷尽，剩余违规
                                                             # 全部来自 output.validator
    PROVIDER_RETRYABLE_EXHAUSTED = "provider_retryable_exhausted"  # M9 → failed，喂熔断窗口
    PROVIDER_FATAL = "provider_fatal"                        # M9 运行级，直接喂熔断器
    CONTEXT_OVERFLOW = "context_overflow"                    # v1.11：ContextOverflowError——
                                                             # precheck（V16 咽喉 / V10 最小单元）
                                                             # 或 reactive（V20/V24）→ failed →
                                                             # rejects；计入 report.budget.
                                                             # overflow_records；熔断矩阵 §7.8
    OUTPUT_TRUNCATED = "output_truncated"                    # v1.11：OutputTruncatedError（V11）——
                                                             # 输出撞上 max_output_tokens →
                                                             # failed → rejects 独立桶；永不修复、
                                                             # 永不喂熔断器
    GENERATION_CONFIG_INVALID = "generation_config_invalid"  # v1.18 M1 或编译器 → exit 2
    GENERATION_PLAN_INFEASIBLE = "generation_plan_infeasible"# CP-SAT 不可行 → exit 2
    GENERATION_PLAN_BUDGET = "generation_plan_budget"        # 未证明最优 → exit 4
    GENERATION_PLAN_INTERNAL = "generation_plan_internal"    # 模型或不变量失效 → exit 4
    GENERATION_DEDUP_TRANSACTION = "generation_dedup_transaction"  # 过期 token → exit 4
    GENERATION_DOWNSTREAM_CONTRACT = "generation_downstream_contract"  # 协议违约
    POST_VALIDATOR_INVALID = "post_validator_invalid"        # 当前槽 attempt 拒绝
    POST_VALIDATOR_EXCEPTION = "post_validator_exception"    # 当前槽 attempt 拒绝
    SEQUENCE_DELIVERY_EXHAUSTED = "sequence_delivery_exhausted"  # 交付尝试耗尽 → exit 1
    SEQUENCE_PROJECTION_MISMATCH = "sequence_projection_mismatch"  # 当前槽 attempt 拒绝
    GENERATION_COMMIT_IO = "generation_commit_io"            # 成功工件提交失败 → exit 4
    GENERATION_FAILED_REPORT_IO = "generation_failed_report_io"  # 保留 primary error
    INTERNAL_ERROR = "internal_error"                        # 任何未预期异常


class PostValidatorInvalidError(Exception):
    """内部后置验证器在运行期返回了非法契约形状。"""
