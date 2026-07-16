# 4. 核心数据结构与内部 API

本章是全部模块共享的类型契约。除 `PipelineItem` 的状态字段外全部为不可变（frozen dataclass）；模块间只通过这些类型与第 3 章列出的类签名交互。

## 4.1 记录与信封

```
Status = Literal["active",        # 存活，继续流转
                 "dropped_dup",   # M3 判重
                 "dropped_lowq",  # M4 低于质量门
                 "dropped_verify",# M7 评审失败且策略为 drop
                 "failed",        # 处理异常（结构不可修复 / provider 错误耗尽重试等）
                 "absorbed",      # v1.8 只增：成员帧已被序列信封吸收（M14 ②b，3.14）；
                                  #   第三路由——不写主输出也不写 rejects，仅计数（3.11.2）
                 "dropped_noise"] # v1.8 只增：噪声帧 / 短段帧（M14：reason=noise / below_min_len，3.14）
                                  #   或 verify 修复收缩弃帧（M7：off_task_member）→ rejects（3.11.2）

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
    kind: Literal["single", "sequence"] = "single"   # v1.8 只增（尾部追加、带默认——既有构造点零改动）：
                                      #   "sequence" = M14 拼装的 episode 序列记录（3.14）
    members: tuple["Record", ...] = ()# v1.8 只增：sequence 时为成员帧按序键升序；single 恒 ()
                                      # 序列 Record 字段约定（S24）：text/raw/ui_tree/image = None；
                                      #   modality = 成员模态；id = sha256("\n".join(member_ids))[:16]
                                      #   （拼装时定格，成员手术不重算）；ref = RecordRef(source_file=首成员源,
                                      #   line_no=首成员 line_no, pair_index=首成员 pair_index,
                                      #   generated_from=(), generator=None)——完整成员溯源由
                                      #   _meta.stream.member_sources 承担（6.3）

@dataclass(frozen=True)
class Classification:                 # v1.7：M13 分类结果（3.13）
    label: str                            # 本信封路由标签
    labels: tuple[str, ...]               # 该记录命中全集（声明序；single 恒单元素）
    source: Literal["llm", "fallback", "inherited"]
    detail: Mapping                       # reason / sc 统计 / fallback 留痕（kind, message）

@dataclass
class PipelineItem:                   # 唯一可变信封；生命周期 = 一个批
    record: Record
    status: Status = "active"
    classification: Classification | None = None   # v1.7：未启用 classify 恒为 None
    dedup: DedupInfo | None = None
    scores: dict[str, QualityScore] = field(default_factory=dict)
    annotation: Annotation | None = None
    verification: VerificationResult | None = None
    errors: list[StageError] = field(default_factory=list)
    transitions: tuple[Transition, ...] | None = None   # v1.8 只增：M15 写入（3.15）；
                                      #   None = 未启用 extract / 未到站（幂等门：is not None 跳过）
    session_id: str | None = None     # v1.8 只增：会话边界的批内载体（S4）——M10 装箱时对帧信封
                                      #   盖章、M14 对追加的 episode 信封盖章（簿记非业务逻辑）；
                                      #   M7 修复邻域查询 = session_id 过滤 + 批列表位置序
```

## 4.2 阶段结果类型

```
@dataclass(frozen=True)
class DedupInfo:  kind: Literal["unique","exact","near_text","near_image","near_both","near_semantic"]
                  cluster_key: str; kept_id: str | None    # 重复时指向被保留记录

@dataclass(frozen=True)
class Transition:                     # v1.8 只增：M15 对一对相邻成员帧的摘取产物（3.15），
                                      #   经 PipelineItem.transitions 承载（4.1）
    index: int                        # 重建后位次（恒 = 在 transitions 元组中的下标）；成员手术后
                                      #   重编号——不变量 len(transitions) = len(members)−1 恒真（S31）
    action: Mapping                   # 过 action_schema 的对象：{action_type, target, value,
                                      #   description}（字段语义见 3.15）
    model: str                        # 摘取 profile 的模型名
    attempts: int                     # 1 + L3 修复次数
    detail: Mapping                   # fallback 留痕：{kind:"extraction_invalid", message}（S16）；
                                      #   手术接缝重摘取：{reseamed: true}（S31）；干净摘取为 {}

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
                          defects: tuple[Mapping, ...] = ()
                          # ↑ v1.8 additive（S7）：stream 缺陷表（3.7 stream 分支），每项
                          #   {"kind","members","position","detail"}（kind 五枚举见 3.7）；
                          #   非 stream 路径恒 ()；随信封入 _meta.verification.defects（6.3）

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
           ②a（v1.7）classify 例外（仅 assignment="multi"）——可向传入列表尾部追加派生信封；
           追加物视同批内普通元素、同受 ①③④ 约束；不得删除、重排或替换任何既有元素对象
           （既有元素的 status / classification / errors 字段写入属 ①④ 的正常行为）；
           返回值仍须是传入的同一列表对象（调用方依赖列表身份）；
           ②b（v1.8）segment 例外（仅 stream 模式）——segment 可将批内既有 active 成员信封的
           status 置为 `absorbed` 或 `dropped_noise`（属①④的正常状态写入），并向传入列表
           **尾部**追加以这些成员拼装的序列信封；追加物视同批内普通元素、同受①③④约束；
           每个成员信封至多被一个序列信封吸收；不得删除、重排或替换任何既有元素对象；
           返回值仍须是传入的同一列表对象。**M7 修复路径豁免**：verify 的缺陷修复可在本批内
           将成员信封状态在 `absorbed` 与 `dropped_noise` 间双向改写（成员回收/收缩），
           此为契约①的唯一反向豁免；禁止将成员信封翻回 `active`；
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

**共享帧 helper（v1.8 只增，S12/S13）**：`frame_digest` 与 `tree_diff` 为 `labelkit/common/contracts/types.py` 模块级函数（与 `UITree.serialize` 同处的共享渲染层，签名入 CONTRACTS §3），供 M14 分段（3.14）、M15 摘取（3.15）、M13 序列分支（3.13）与 M4 序列打分（3.4）共用——算子模块互不依赖，共享渲染逻辑一律落本章类型层：

```
def frame_digest(record: Record, max_chars: int) -> str
    # best-effort 确定性帧摘要（S12——UINode 封闭九字段，包名/activity 仅经 extra 兜底可达）：
    # UI 模态：app      = extra 键 package|package_name|pkg 首个非空（可见节点）
    #          activity = extra 键 activity|activity_name|window_title 首个非空（可缺省）
    #          title    = DFS 首个可见非空 text
    #          salient  = 可见 text/content_desc 按序去重；Button/EditText/CheckBox 类
    #                     交互角色加 "*" 前缀
    #          整体截断至 max_chars（serialize 截断惯例）。
    # 文本模态：record.text 截断至 max_chars。
    # 摘要贫瘠判定：可见文本节点数为 0 或摘要长度 < 8 ⇒ 贫瘠——调用方计入
    #   digest_poor_frames（6.4 report.stream）+ 每运行一次 WARN，指引开 segment.use_vision。

def tree_diff(a: UITree | None, b: UITree | None, quantize_px: int) -> Mapping
    # 结构键 (role, bounds//quantize_px, depth) 多重集匹配（S13——node_id 非跨帧身份，
    #   不得作匹配键）；仅可见节点；O(n1+n2)；纯统计不做语义归因（归因属 M15）。返回：
    # {added:int, removed:int, text_changed:int, change_ratio:float,
    #  app_changed:bool, title_changed:bool}
```
