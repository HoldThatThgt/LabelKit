# 4. 核心数据结构与内部 API

本章是全部模块共享的类型契约。除 `PipelineItem` 的状态字段外全部为不可变（frozen dataclass）；模块间只通过这些类型与第 3 章列出的类签名交互。

## 4.1 记录与信封

```
Status = Literal["active",        # 存活，继续流转
                 "dropped_dup",   # M3 判重
                 "dropped_lowq",  # M4 低于质量门
                 "dropped_verify",# M7 评审失败且策略为 drop
                 "failed"]        # 处理异常（结构不可修复 / provider 错误耗尽重试等）

@dataclass(frozen=True)
class RecordRef:
    source_file: str                  # 相对 run.input 的路径
    line_no: int | None               # 文本模态：1-based 行号
    pair_index: int | None            # UI 模态：文件对 index
    generated_from: tuple[str, ...]   # process 模式生成样本：种子记录 id 列表；其余（含 generate_only 生成样本）为空元组——合成判据用 generator（v1.4）
    generator: Mapping | None = None  # v1.2：生成记录的 {"llm": profile 名, "style": name|None} 溯源（3.6.2）；非生成记录为 None

@dataclass(frozen=True)
class ImageRef:
    path: Path; format: Literal["png", "jpeg"]; size_bytes: int
    def load_base64(self, max_px: int) -> tuple[str, str]:   # (media_type, b64) 用后即弃

@dataclass(frozen=True)
class UINode:
    node_id: str; parent_id: str | None; depth: int
    role: str                         # class/type 归一后的控件角色
    text: str; content_desc: str
    bounds: tuple[int, int, int, int] # (l, t, r, b) 像素
    visible: bool; extra: Mapping[str, str]   # 白名单外字段原样保留

@dataclass(frozen=True)
class UITree:
    nodes: tuple[UINode, ...]         # 深度优先序
    def serialize(self, max_chars: int | None = None, quantize_px: int = 0) -> str

@dataclass(frozen=True)
class Record:
    id: str                           # sha256 前 16 hex（M2 定义的确定性规则）
    modality: Literal["text", "ui"]
    text: str | None                  # 文本模态：抽取文本；UI 模态：None
    raw: Mapping | None               # 文本模态：原始行对象
    ui_tree: UITree | None; image: ImageRef | None
    ref: RecordRef

@dataclass
class PipelineItem:                   # 唯一可变信封；生命周期 = 一个批
    record: Record
    status: Status = "active"
    dedup: DedupInfo | None = None
    scores: dict[str, QualityScore] = field(default_factory=dict)
    annotation: Annotation | None = None
    verification: VerificationResult | None = None
    errors: list[StageError] = field(default_factory=list)
```

## 4.2 阶段结果类型

```
@dataclass(frozen=True)
class DedupInfo:  kind: Literal["unique","exact","near_text","near_image","near_both","near_semantic"]
                  cluster_key: str; kept_id: str | None    # 重复时指向被保留记录

@dataclass(frozen=True)
class QualityScore: criterion: str; score: float           # [0,1] 归一化
                    mode: Literal["pairwise_bt","pointwise"]
                    detail: Mapping    # pairwise: {comparisons, wins, ties, log_theta}
                                       # pointwise: {raw_score(0-5), reason}

@dataclass(frozen=True)
class Annotation: output: Mapping     # 已通过用户 Schema (L2) 的对象
                  model: str; attempts: int                # 1 + L3 修复次数
                  usage: Usage

@dataclass(frozen=True)
class VerificationResult: verdict: Literal["pass","fail"]
                          rounds: int; critiques: tuple[Mapping, ...]

@dataclass(frozen=True)
class StageError: stage: str; kind: str                    # 错误分类码（7.6）
                  message: str; retryable: bool
```

## 4.3 Stage 协议与异常层级

```
class Stage(Protocol):
    name: str
    async def run(self, batch: list[PipelineItem], ctx: RunContext) -> list[PipelineItem]:
        """契约：① 只处理 status=='active' 的项；② 不删除列表元素（只改 status）；
           ③ generate 例外——返回新增子批（原批元素不修改）；④ 单条失败不得抛出到批层面，
           必须落入 item.errors 并置 status='failed'。"""

LabelKitError
 ├─ ConfigError(errors: list[str])            # M1，退出码 2
 ├─ InputError                                 # M2 fail 策略触发，退出码 3
 ├─ ProviderRetryableError / ProviderFatalError# M9
 ├─ SchemaViolation(errors, raw_last_output)   # M8，记录级
 └─ InternalError                              # 不变量破坏（如 M11 终检失败）
```

`UITree.serialize()` 的规范定义（M3 去重与 M5 提示词共用，M3 传 `quantize_px=dedup.bounds_quantize_px`）：深度优先遍历可见节点；每行 = `" "*depth + role + (' "'+text+'"' if text) + (' desc="'+content_desc+'"' if content_desc) + ' ['+l,t,r,b+']' + 非空 extra 的 k=v 列表`；坐标除以 quantize_px 取整（0 = 不量化）；超长截断规则见 3.5.2。该线性化即 ScreenAI 的 screen-schema 表示思想 [13]。
