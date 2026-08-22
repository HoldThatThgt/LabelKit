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
                 "dropped_noise", # v1.8 只增：噪声帧 / 短段帧（M14：reason=noise / below_min_len，3.14）
                                  #   或 verify 修复收缩弃帧（M7：off_task_member）→ rejects（3.11.2）
                 "stitched"]      # v1.9 只增：被并 episode 信封壳（M16 ②c，3.16）——壳终态，仅计
                                  #   被并 episode 信封（救援短段无信封形态、不产生壳）；第四路由
                                  #   ——不写主输出也不写 rejects，仅计数（absorbed 同款，3.11.2）

@dataclass(frozen=True)
class RecordRef:
    source_file: str                  # 相对 run.input 的路径
    line_no: int | None               # 文本模态：1-based 行号
    pair_index: int | None            # UI 模态：文件对 index
    generated_from: tuple[str, ...]   # process 模式生成样本：种子记录 id 列表；其余（含 generate_only 生成样本）为空元组——合成判据用 generator（v1.4）
    generator: Mapping | None = None  # flat 生成记录的 {"llm": profile 名, "style": name|None} 溯源；
                                      # sequence 的生成真值位于 _meta.generation / _meta.event，
                                      # 不在 RecordRef 增加另一套结构字段

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
    id: str                           # process/flat 使用既有 M2 公式；v1.18 sequence 使用冻结的 32-hex ID 公式
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
                                      #   （拼装时定格，成员手术不重算；v1.9：M16 缝合重绑同样不重算
                                      #   ——episode_id = 幸存信封 record.id = thread_id，碎片原
                                      #   episode_id 落 _meta.stream.fragments[].source_episode，
                                      #   3.16.4/6.3）；ref = RecordRef(source_file=首成员源,
                                      #   line_no=首成员 line_no, pair_index=首成员 pair_index,
                                      #   generated_from=(), generator=None)——完整成员溯源由
                                      #   _meta.stream.member_sources 承担（6.3）
                                      # sequence 生成侧：sequence Record.id = sequence_id；成员
                                      #   Record.id = event_id、raw = 完整 stream row、text =
                                      #   canonical_json(payload)。两类 ID 均为第 11 章冻结的 32-hex 公式，
                                      #   不复用 M2 的 16-hex 摄取公式

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
    thread_id: str | None = None      # v1.9 只增：线索身份（3.16）——M16 对幸存线索信封盖章
                                      #   （= record.id，单碎片线索亦盖）；未启用 stitch 恒 None；
                                      #   classify multi 扇出克隆复制（3.13.4）。另有 duck 标
                                      #   seam_indexes: tuple[int, ...]（M16 对幸存线索信封盖章，
                                      #   无接缝 = 空元组；非 dataclass 字段）：元素 = 接缝对左成员在重绑成员元组
                                      #   中的下标，与 Transition.index / steps[].index 同坐标、
                                      #   值域 [0, len(members)−2]，与 _meta.stream.order_span 的
                                      #   会话序键空间无换算关系（3.16.4；_fan_out 同复制）
    member_classifications: dict[str, Classification] | None = None
                                      # v1.12 只增：M13 帧级批量判决写入（首标签序列信封，3.13.7）；
                                      #   键 = 成员 record.id、值恒单标签（labels = (label,)，
                                      #   source ∈ {"llm","fallback","inherited"}；sequence projector
                                      #   按实际事件 frame class 直装 inherited 值，classify 调用为零；
                                      #   None = 帧 pass 未运行
                                      #   （帧分类关闭 / 降格会话 / 非首标签克隆）——幂等门 is None；
                                      #   扇出克隆按引用共享同一 dict（record/dedup 同族，3.13.7）
    member_annotations: dict[str, Annotation] | None = None
                                      # v1.12 只增：M5 帧级逐帧标注写入（同一执行门，3.5.5）；
                                      #   键 = 成员 record.id；值语义 = 单一真相（emitter 三值判定
                                      #   直读 dict 形态，3.11.2）：占键 Annotation = annotated、
                                      #   占键 None = failed（成员标注不可修复）、缺键 = skipped
                                      #   （跳过类）、dict 本身 None = 帧 pass 未运行；克隆按引用
                                      #   共享（同上）；M7 手术同步随成员集删键/补跑（3.7.3）
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
                                      #   手术接缝重摘取：{reseamed: true}（S31）；干净摘取为 {}；
                                      #   v1.9 只增保留键：线索接缝占位 {kind:"thread_seam",
                                      #   interrupted_by:[...]}（与 extraction_invalid 并列，零 LLM
                                      #   机械占位，3.15.4；emitter 据此推导 steps 行内 resumed=true）

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
                          #   {"kind","members","position","detail"}（kind 枚举见 3.7——v1.8
                          #   五值，v1.9 起六值：+wrong_stitch，只标记不拆线）；
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
           ②c（v1.9）stitch 例外（仅 stitch 启用）——授权恰三件事：其一，将批内既有 active
           episode 信封（被并方）的 status 置为 `stitched`（壳终态，属①④的正常状态写入）；
           其二，对幸存线索信封执行 Record 重绑（`members` 替换为两方成员按序键升序拼接的
           新元组；`record.id` 不重算——M7 手术先例，thread_id == 幸存信封 episode_id）；
           其三，将 below_min_len 来源帧信封 `dropped_noise → absorbed` 翻转（②b 双向豁免的
           M16 延伸，**仅限救援命中**）。**幸存者规范句**：一遍中幸存信封恒为**线索创始信封**
           （开线索者），被并候选信封作壳；二遍复评方向相反——单碎片线索作候选方并入目标
           线索，**目标线索信封幸存**、候选信封作壳（fragments 按会话序重排，episode_id /
           thread_id 随幸存信封，3.16.4）。不得删除、重排或替换任何既有元素对象（重绑改写的
           是幸存信封自身的 record 字段，非元素替换）；返回值仍须是传入的同一列表对象；
           禁止将 `stitched` 壳翻回 `active`；授权面不含 absorbed / dropped_noise → failed
           的帧迁移（`on_error="fail"` 仅施于 episode 候选信封，3.16.6）；
           ③ generate 例外——返回新增子批（原批元素不修改）；④ 单条失败不得抛出到批层面，
           必须落入 item.errors 并置 status='failed'。"""

LabelKitError
 ├─ ConfigError(errors: list[str])            # M1，退出码 2
 ├─ InputError                                 # M2 fail 策略触发，退出码 3
 ├─ ProviderRetryableError / ProviderFatalError# M9
 ├─ SchemaViolation(errors, raw_last_output)   # M8，记录级
 ├─ DeliveryError                              # sequence slot 耗尽，退出码 1
 └─ InternalError                              # 不变量破坏（如 M11 终检失败）
```

**帧粒度与 Stage 契约（v1.12 零改动声明）**：帧级分类/标注（3.13.7、3.5.5）对本契约**零改动**——契约例外维持 ②a/②b/②c 三条原文，不新增例外条款：帧产物只写入信封自身的 `member_classifications` / `member_annotations` 两字段（属 ①④ 的正常字段写入），不改成员帧状态机（成员保持 `absorbed`）、不增删列表元素、不改链序与守恒恒等式（6.4）。**克隆共享语义补注**（②a 的 v1.12 侧注）：classify multi 扇出克隆对两字段**按引用共享**（与 `record` / `dedup` 同族，3.13.7 扇出共享行）——帧产物描述成员帧本身而非信封路由，原/克隆行渲染同一 dict；帧级两 pass 只在首标签信封上执行（克隆判据 = `classification.label != classification.labels[0]`，verify S8 同款），M7 帧产物同步亦无克隆分支（克隆信封永不手术，3.7.3）；手术后原/克隆行 members 分叉由既有 `repaired` 位消歧（6.3 补注）。

**sequence 与 Stage 契约：**flat 继续使用 generate 的新增子批例外。sequence 不调用 `GenerateStage.run`，
由 M10 以 `AttemptTransaction` 串行 admission；候选在事务内调用 M3/M4/M5/M7 的 attempt 接口，只有整个
counterfactual set 接受后才提交 dedup 与 dataset 状态。因此 sequence 不新增 `PipelineItem.status`，也不借用
segment/stitch 的成员状态转换；slot rejection 不写半成品 `item.errors`。

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
    # v1.11（V9）：M14 的 digest 计算前移为会话级预计算——切窗前每会话一次求出逐帧
    #   digest（预算贪心装填以逐帧成本为输入，3.14）；接缝帧不再随相邻两窗双算
    #   （前移是净改善；贫瘠护栏计算路径独立、保持不动）。本函数为记录内容纯函数，
    #   签名与语义零改动。
    # 摘要贫瘠判定：可见文本节点数为 0 或摘要长度 < 8 ⇒ 贫瘠——调用方计入
    #   digest_poor_frames（6.4 report.stream）+ 每运行一次 WARN，指引为 segment.llm
    #   配置 supports_vision=true 的 profile（v1.11/V4 改写——use_vision 键已移除，
    #   窗口附图由能力推导 vision_resolved 决定，3.14）。

def tree_diff(a: UITree | None, b: UITree | None, quantize_px: int) -> Mapping
    # 结构键 (role, bounds//quantize_px, depth) 多重集匹配（S13——node_id 非跨帧身份，
    #   不得作匹配键）；仅可见节点；O(n1+n2)；纯统计不做语义归因（归因属 M15）。返回：
    # {added:int, removed:int, text_changed:int, change_ratio:float,
    #  app_changed:bool, title_changed:bool}
```

**预算原语契约引（v1.11）**：上下文预算的估算与装填原语（`margin` / `input_budget` / `embed_budget` / `est_text` / `est_image_prior` / `est_prompt` / `fit_text` / `min_window` / `classify_stage_error` 与 `ImageCostCalibrator`）为新共享模块 `labelkit/common/runtime/budget.py` 的模块级纯函数与类（common 层运行时，**非本章类型层**——签名与冻结常数以 CONTRACTS 的 budget 新节为准，机制见 3.9）；本章共享渲染层（`serialize` / `frame_digest` / `tree_diff`）签名零改动，装填器（贪心切窗等）属算子逻辑、落各算子模块。

**v1.18 sequence 冻结类型：**下表类型全部为 frozen dataclass；Mapping 在构造时深拷贝为
JSON-compatible 值，再以 `MappingProxyType` 对外暴露。字段顺序是接口契约，生产 typedef/dataclass
每个字段都要有中文语义注释。

| 类型 | 按声明顺序冻结的字段 |
|---|---|
| `SequenceClassGenerationConfig` | `instruction`, `state_schema`, `initial_state_source`, `initial_state_catalog_path`, `initial_state_catalog` |
| `PayloadBindingSpec` | `payload_path`, `state_phase`, `state_path` |
| `RoleSpec` | `name`, `frame_class`, `actor`, `read_roots`, `write_roots`, `publish_roots`, `observers`, `state_instruction`, `pre_state_schema`, `payload_bindings`, `calendar_window` |
| `GapSpec` | `name`, `before`, `after`, `min_gap_us`, `max_gap_us` |
| `SequencePattern` | `name`, `sequence_class`, `description`, `roles`, `order`, `gaps`, `max_span_us` |
| `VariantSpec` | `name`, `kind`, `target`, `outcome_schema`, `expected_violation`, `divergence_role` |
| `CounterfactualSetSpec` | `name`, `pattern`, `count`, `variants` |
| `InstructionOnlySpec` | `name`, `sequence_class`, `count`, `len_range`, `instruction`, `state_schema` |
| `TimelineSpec` | `timestamp_start_us`, `utc_offset_minutes`, `event_gap_us`, `primary_sessions`, `crossed_primary_sessions`, `session_max_events`, `session_max_span_us`, `session_gap_us`, `noise_events`, `duplicate_sequences` |
| `CalendarWindowSpec` | `name`, `utc_offset_minutes`, `days`, `intervals_us` |
| `NoiseSpec` | `frame_class`, `instruction`, `topics` |
| `GenerationLimits` | `pattern_roles`, `variants_per_counterfactual_set`, `instruction_only_events`, `scenario_seed_bytes`, `state_or_outcome_schema_bytes`, `frame_schema_bytes`, `event_patch_bytes`, `rendered_payload_bytes`, `prompt_value_bytes`, `repair_context_bytes`, `prompt_text_bytes`, `record_units`, `stream_rows`, `retained_content_bytes` |
| `SequenceGenerationConfig` | `mode`, `semantic_profile`, `evaluation_profile`, `max_slot_attempts`, `state_validator`, `patterns`, `counterfactual_sets`, `instruction_only`, `timeline`, `calendar_windows`, `noise`, `limits` |
| `GenerationProgram` | `mode`, `semantic_profile`, `evaluation_profile`, `max_slot_attempts`, `planner_seed`, `class_views`, `frame_classes`, `frame_schema`, `patterns`, `counterfactual_sets`, `instruction_only`, `timeline`, `calendar_windows`, `noise`, `limits`, `state_validator`, `digest` |
| `DeliverySlot` | `slot_key`, `source_name`, `scenario_index`, `sequence_class`, `pattern_name`, `variant_names`, `catalog_row_index` |
| `PlannedEvent` | `event_key`, `role`, `position`, `logical_time_us`, `timestamp_us`, `session_id` |
| `NoiseSlot` | `event_key`, `ordinal`, `frame_class`, `topic`, `timestamp_us`, `session_id` |
| `ReplayLayout` | `source_slot_key`, `source_variant_name`, `replay_ordinal`, `session_id`, `timestamps_us` |
| `ScenarioPlan` | `blocks`, `delivery_slots`, `noise_slots`, `replay_layouts`, `primary_sessions`, `digest` |
| `ScenarioSeed` | `initial_state`, `actors`, `shared_facts`, `style`, `time_context` |
| `ActorView` | `actor`, `goal`, `read_state`, `observations`, `logical_time_us`, `wait_since_previous_us` |
| `EventPlan` | `frame_class`, `actor`, `intent`, `patch` |
| `EventExecution` | `state_before`, `state_after`, `state_before_hash`, `state_after_hash`, `publish_snapshot`, `normalized_patch` |
| `EventDraft` | `event_key`, `event_id`, `frame_class`, `actor`, `logical_time_us`, `timestamp_us`, `actor_view`, `intent`, `patch`, `state_before_hash`, `state_after_hash`, `publish_snapshot`, `payload` |
| `EventTruth` | `event_key`, `event_id`, `role`, `frame_class`, `actor`, `logical_time_us`, `timestamp_us`, `actor_view`, `intent`, `patch`, `state_before_hash`, `state_after_hash`, `publish_snapshot`, `payload` |
| `ObservedEvent` | `event_id`, `frame_class`, `timestamp_us` |
| `SemanticReviewEvent` | `frame_class`, `actor`, `logical_time_us`, `wait_since_previous_us`, `actor_view`, `intent`, `patch`, `state_before_hash`, `state_after_hash`, `publish_snapshot`, `payload` |
| `PatternEvaluation` | `actual_bindings`, `actual_violations` |
| `StateEvaluation` | `replay_hash`, `final_state_hash`, `bindings_valid`, `outcome_valid`, `protected_prefix_valid` |
| `SemanticEvaluation` | `causal_consistency`, `actor_knowledge`, `goal_consistency`, `temporal_plausibility`, `cross_frame_consistency`, `realism`, `reason_codes` |
| `NoiseSemanticEvaluation` | `unrelated_to_declared_tasks`, `no_executable_task`, `realism`, `matches_planned_topic`, `reason_codes` |
| `EventTrace` | `scenario_id`, `world_branch_id`, `sequence_class`, `pattern_name`, `variant_name`, `scenario_seed`, `events`, `final_state`, `pattern_evaluation`, `state_evaluation`, `semantic_evaluation` |
| `GenerationParseContext` | `project_root`, `class_views`, `frame_classes`, `llm_profiles`, `max_repair_attempts`, `repair_profile`, `hook_loader`, `collector` |
| `ScenarioSeedRequest` | `program`, `slot`, `attempt_index`, `random_seed` |
| `EventPlanRequest` | `mode`, `semantic_profile`, `slot_key`, `planned_event`, `role`, `generation_instruction`, `sequence_length`, `eligible_frame_classes`, `eligible_actors`, `actor_view`, `visible_state`, `state_schema`, `outcome_schema`, `history`, `actor_profiles`, `public_facts`, `attempt_index`, `variation_nonce` |
| `EventExecutionContext` | `program`, `plan`, `slot`, `variant_name`, `event_index`, `scenario_seed`, `current_state`, `history` |
| `StateTransitionInput` | `slot_key`, `variant`, `role`, `state_before`, `state_after`, `patch` |
| `PostValidationResult` | `violations`, `event_execution` |
| `PostValidatedCallRequest` | `profile`, `prompt`, `schema`, `scope`, `post_validator` |
| `ValidatedGenerationCall` | `object`, `event_execution`, `resolved_at`, `usage`, `attempts`, `model` |
| `RenderEventRequest` | `semantic_profile`, `slot_key`, `planned_event`, `event_plan`, `actor_view`, `publish_snapshot`, `state_before_hash`, `state_after_hash`, `binding_values`, `frame_spec`, `role`, `public_facts`, `attempt_index`, `limits` |
| `StateEvaluationRequest` | `program`, `slot`, `pattern`, `variant`, `scenario_seed`, `events`, `baseline_events`, `final_state` |
| `CouplingEvaluationRequest` | `variant`, `baseline_events`, `events` |
| `SemanticEvaluationRequest` | `evaluation_profile`, `mode`, `sequence_class`, `class_description`, `pattern_description`, `scenario_seed`, `review_events`, `final_state`, `attempt_index`, `limits` |
| `NoiseRenderRequest` | `semantic_profile`, `noise_slot`, `noise_spec`, `frame_spec`, `class_descriptions`, `frame_descriptions`, `attempt_index`, `limits` |
| `NoiseEvaluationRequest` | `evaluation_profile`, `payload`, `planned_topic`, `class_descriptions`, `frame_descriptions`, `attempt_index`, `limits` |
| `ProjectionRequest` | `program`, `plan`, `slot`, `trace` |
| `NoiseProjectionRequest` | `program`, `run_id`, `noise_slot`, `payload` |
| `ReplayProjectionRequest` | `program`, `plan`, `layout`, `source` |
| `ProjectedSequence` | `main_record`, `primary_stream_rows` |
| `SequenceRows` | `main_row`, `primary_stream_rows`, `retained_content_bytes` |
| `SequenceAssemblyRequest` | `program`, `schema_engine`, `item`, `projection`, `batch_no` |
| `ReplayRows` | `rows`, `retained_content_bytes` |
| `ProjectionWitness` | `main_record_id`, `generation_digest`, `member_sources_digest`, `primary_base_digests` |
| `ReconcileRequest` | `program`, `plan`, `run_id`, `projection_witnesses`, `sequences`, `noise_payload_digests`, `noise_rows`, `replays`, `retained_content_bytes` |
| `GenerationServices` | `config`, `schema_engine`, `llm`, `metrics` |
| `RuntimeCredentials` | `llm`, `embedding` |
| `ResolvedHook` | `reference`, `target` |
| `ValidationHooks` | `output`, `sample`, `state` |
| `ResolvedPaths` | `project`, `project_root`, `input`, `output`, `report`, `rejects`, `sidecar`, `trace`, `stream`, `manifest`, `failed_report` |
| `DeliveryRequest` | `program`, `plan`, `paths`, `run_attempt_id`, `run_id` |
| `DeliveryServices` | `generation`, `dedup`, `quality`, `annotate`, `verify`, `emitter` |
| `AttemptTransaction` | `items`, `class_views`, `projected_sequences` |
| `DownstreamAttemptRequest` | `transaction`, `run_context` |
| `DownstreamAttemptResult` | `accepted`, `rejected_stage`, `dataset_counters` |
| `DedupGroupRequest` | `records`, `exempt_pairs`, `embedding_profile` |
| `DedupProbeToken` | `capability_id`, `index_generation`, `record_digests`, `exact_features`, `minhash_features`, `embedding_features` |
| `GenerationProduct` | `main_rows`, `stream_rows`, `report` |

`ScenarioBlock` 是只读 Mapping，键为 `tuple[str, str | None]`，值为 `PlannedEvent` tuple。
`None` 只表示 instruction-only block。`ScenarioPlan` 是 `DeliverySlot` 的唯一属主；
`GenerationProgram` 不复制 slot，`DeliverySlot.catalog_row_index` 是 catalog 行的唯一分配真值。
`PlannedEvent` 只冻结位置与时间结构；declared 的 frame class/actor 从 role 解析，instruction-only
的 frame class/actor 由当次 `EventPlan` 选择，两者都不在计划事件上伪造预置值。

`EventExecutionContext.history` 恰为 `tuple[EventDraft, ...]`；`EventPlanRequest.history` 恰为
`tuple[EventDraft, ...] | None`，且只在 instruction-only 非 null。`EventDraft` 刻意没有 role，
是逐事件生成期唯一 history carrier。declared branch 在独立 PatternEvaluator 完成一对一 binding 前
不得构造 `EventTruth`；只有完整 `actual_bindings` 覆盖全部 event_id 后，才为每个 draft 增加唯一 role。

`NoiseSlot` 只描述独立 noise 事件；`ReplayLayout` 只描述一次完整 replay 的 source、variant、
ordinal、session 与逐事件 timestamp，两者均不进入 `ScenarioBlock`。`ReplayLayout.timestamps_us`
的长度必须等于 source positive sequence 的事件数；source 只按 `slot_key` 与
`source_variant_name` 解析，不得按 payload、位置或临时 list index 猜测。

`ResolvedPaths` 对 sequence 冻结 main、stream、report、manifest、failed_report，rejects 与 sidecar 为 null。
`RuntimeCredentials` 只服务于 factory 构造 LLMClient，随后不进入 delivery request；其与
`ResolvedHook.target` 的 repr/compare 均不得暴露 callable 或 secret value。

`SequenceValidationInput`、`ScenarioValidationInput`、`GenerateStreamConfig`、`ScenarioConfig`、
`SequencePlan`、`StreamPlan`、`ExecuteEventRequest` 均不存在；不得保留同名 alias、wrapper 或转换层。

**v1.18 sequence 冻结接口：**

~~~python
def parse_generation_config(
    raw_project: Mapping[str, object],
    context: GenerationParseContext,
) -> SequenceGenerationConfig:
    """解析 sequence 生成配置。"""


def compile_generation_program(config: ResolvedConfig) -> GenerationProgram:
    """编译唯一生成程序。"""


def compile_scenario_plan(program: GenerationProgram) -> ScenarioPlan:
    """冻结确定性场景计划。"""


async def generate_scenario_seed(
    request: ScenarioSeedRequest,
    services: GenerationServices,
) -> ScenarioSeed:
    """生成或读取一个完整初始世界。"""


def build_event_plan_request(
    context: EventExecutionContext,
    attempt_index: int,
    variation_nonce: str,
) -> EventPlanRequest:
    """校验事件引用并机械投影 prompt-safe 请求。"""


async def plan_event(
    context: EventExecutionContext,
    attempt_index: int,
    variation_nonce: str,
    services: GenerationServices,
) -> tuple[EventPlan, EventExecution]:
    """从唯一执行根规划事件并返回同候选的执行证明。"""


def execute_event(
    context: EventExecutionContext,
    event_plan: EventPlan,
) -> EventExecution:
    """在执行根的状态副本上原子执行并验证状态转移。"""


def post_validate_event_plan(
    candidate: Mapping[str, object],
    context: EventExecutionContext,
) -> PostValidationResult:
    """从当次候选构造唯一 EventPlan 并返回执行证明。"""


async def render_event(
    request: RenderEventRequest,
    services: GenerationServices,
) -> Mapping[str, object]:
    """以发布快照与机械 binding 渲染 frame payload。"""


def evaluate_pattern(
    pattern: SequencePattern,
    events: Sequence[ObservedEvent],
) -> PatternEvaluation:
    """从观察事件独立绑定实际 role 与违规。"""


def evaluate_state(request: StateEvaluationRequest) -> StateEvaluation:
    """从 program 真值独立重放并验证事件轨迹。"""


def evaluate_coupling(request: CouplingEvaluationRequest) -> bool:
    """机械验证反事实受保护前缀。"""


async def evaluate_semantics(
    request: SemanticEvaluationRequest,
    services: GenerationServices,
) -> SemanticEvaluation:
    """以独立 profile 对盲化评审事件判定轨迹语义。"""


async def render_noise(
    request: NoiseRenderRequest,
    services: GenerationServices,
) -> Mapping[str, object]:
    """以独立 noise slot 渲染 noise payload。"""


async def evaluate_noise(
    request: NoiseEvaluationRequest,
    services: GenerationServices,
) -> NoiseSemanticEvaluation:
    """以独立 profile 判定 noise payload 的固定语义。"""


def project_trace(request: ProjectionRequest) -> ProjectedSequence:
    """把通过判定的轨迹投影为下游前 sequence Record 与 primary stream 基础行。"""


def project_noise(request: NoiseProjectionRequest) -> Mapping[str, object]:
    """把已验证 noise payload 投影为独立 stream 行。"""


def project_replay(request: ReplayProjectionRequest) -> ReplayRows:
    """从最终 source SequenceRows 投影完整 replay 行。"""


def projection_witness(projection: ProjectedSequence) -> ProjectionWitness:
    """从尚存的投影视图计算完整 SHA-256 CrossView 源证明。"""


def noise_payload_digest(payload: Mapping[str, object]) -> str:
    """计算 noise semantic gate 后 payload 的完整源摘要。"""


def scenario_plan_digest(plan: ScenarioPlan) -> str:
    """计算排除 digest 自身的完整 ScenarioPlan 摘要。"""


def validate_planned_events(
    program: GenerationProgram,
    slot: DeliverySlot,
    variant_name: str | None,
    events: Sequence[PlannedEvent],
) -> None:
    """把 branch 的位置、role 与 event key 重新绑定到程序。"""


def validate_plan_identity(program: GenerationProgram, plan: ScenarioPlan) -> None:
    """验证 program、plan 自摘要与 canonical planner 完整身份。"""


def reconcile_views(request: ReconcileRequest) -> None:
    """提交前机械核对最终 main、primary、noise 与 replay 视图。"""


def reconcile_prospective_views(request: ReconcileRequest) -> None:
    """机械核对当前尚未提交的连续交付前缀。"""


async def deliver_generation(
    request: DeliveryRequest,
    services: DeliveryServices,
) -> GenerationProduct:
    """执行 sequence 精确交付并返回仅在成功时可提交的产品。"""


class DownstreamAttemptCollaborator(Protocol):
    """定义 sequence 下游 attempt-local 协作者。"""

    async def run_attempt(
        self,
        request: DownstreamAttemptRequest,
    ) -> DownstreamAttemptResult:
        """在事务内运行已开启的下游阶段且不提交全局状态。"""


class DedupIndex:
    """管理 sequence 交付期的探测与原子索引提交。"""

    async def group_probe(
        self,
        request: DedupGroupRequest,
        context: RunContext,
    ) -> DedupProbeToken:
        """无写入地试算整组去重并返回一次性能力。"""

    def group_commit(self, token: DedupProbeToken) -> None:
        """在无 await 临界区一次写入已探测索引特征。"""


class SequenceDeliveryEmitter:
    """组装并提交 sequence 的最终产品。"""

    def assemble_sequence(
        self,
        request: SequenceAssemblyRequest,
    ) -> SequenceRows:
        """从闭包请求组装零 I/O 的完整 sequence 行并执行最终 Schema 终检。"""

    def prepare_product(
        self,
        main_rows: Sequence[Mapping[str, object]],
        stream_rows: Sequence[Mapping[str, object]],
        report: Mapping[str, object],
    ) -> GenerationProduct:
        """唯一地计算 delivery digest 并构造深度冻结产品。"""

    def commit(self, product: GenerationProduct) -> Mapping[str, object]:
        """按固定顺序提交产品并最后替换 manifest。"""

    def write_failed_report(self, report: Mapping[str, object]) -> None:
        """原子写入不属于成功 manifest 的失败诊断。"""


def derive_generation_id(
    domain: str,
    components: Sequence[object],
) -> str:
    """以域分离 canonical JSON 组件派生 32-hex generation ID。"""


def canonical_delivery_row(row: Mapping[str, object]) -> bytes:
    """移除发射期墙钟观测字段并返回 canonical UTF-8 行。"""


def validate_state(value: StateTransitionInput) -> list[str]:
    """实现可选的确定性状态转换校验 hook。"""
~~~

`EventExecutionContext` 是 event plan 的唯一根。`build_event_plan_request` 先验证 slot 属于 plan、
block key 存在且 event index 合法，再机械投影 prompt-safe request；`plan_event` 不接收另一份
request。任一内部引用失配均是 `generation_downstream_contract`、exit 4、零 LLM call 且不消耗
slot attempt。`post_validate_event_plan` 是唯一从 candidate 构造 `EventPlan` 的入口；通过后返回的
`EventExecution` 是正式提交直接消费的同一执行证明，不得重放 patch/hook。

`SemanticEvaluationRequest` 不得携带 `EventTrace`、variant/target/expected/actual violation、
`PatternEvaluation`、`StateEvaluation` 或任何 evaluator truth。declared 的 `EventTruth.role` 只能在
pattern evaluation 之后从 `actual_bindings` 机械回填；instruction-only 的 role 机械为 `position_NNN`。
`EventTrace.events` 只接受 `EventTruth`，不接受 `EventDraft`。

`ProjectionRequest` 不复制 `PatternEvaluation`。`ProjectedSequence` 只是 dedup 与下游前载体；
只有 M11 用最终 `PipelineItem` 组装的 `SequenceRows` 才是 main/primary 最终行与计费真值。
replay 必须在此后只从 source `SequenceRows.primary_stream_rows` 派生，不得从 pre-downstream
Record 或 `ProjectedSequence` 构造。

`SequenceAssemblyRequest(program, schema_engine, item, projection, batch_no)` 是 M11 的唯一装配输入。
`GenerationProgram.class_views` 已把每个类的覆盖 Schema 或全局用户 Schema 物化为生效 Schema，
`GenerationProgram.frame_schema` 是唯一帧标注 Schema。M11 和下游协作者不得回读 source
`ResolvedConfig` 的同名 class/frame views 或 Schema，也不得把 `FrameClassView.gen_schema` 当成标注 Schema。

`ProjectionRequest` 与 `ReplayProjectionRequest` 都显式携带完整 `ScenarioPlan`。公开 projector 在构造任意行前
先验证 program/plan canonical identity，再要求 slot/layout 与 plan 中唯一成员完整 dataclass 相等；控制器在交付边界
验证一次后只用包内 validated helper，内容重试不重新运行 CP-SAT。四类 render/evaluate request 的 `limits` 都必须
来自 `GenerationProgram.limits`，是编译后提示、repair 与 payload 上限的唯一运行真值。

`GenerationServices` 是唯一 source config、LLM、SchemaEngine 与 metrics 根；`DeliveryServices` 不复制
`RunContext`。dedup context 的 cfg 直接复用该 source config；Quality、Annotate 与 Verify 的 context.cfg 则从它
派生，并用 `GenerationProgram.class_views/frame_classes/frame_schema` 替换同名 sequence/frame 视图与帧标注
Schema，所有正常与 repair 路径只读该 attempt-local cfg。其余 `llm/schema_engine/metrics` 仍与
`GenerationServices` 对应对象身份相同，只新建 rng 与 batch number。`AttemptTransaction.items` 是当次 attempt
内唯一可变 item 真值；
`DownstreamAttemptResult` 不复制 items 或 Schema stats。dataset counter delta 属 attempt-local；LLM usage、
retry、latency、SchemaEngine resolved-at 与 trace 是全局运行事实，不随事务回滚。

`ReconcileRequest.replays` 保留 ReplayLayout 顺序的 `ReplayRows` 分组，不能预先展平；
`retained_content_bytes` 携带 prospective 或最终全量费用。CrossView 必须直接从实际 sequence main/primary、noise
与 replay canonical rows 独立复算每个分组和总费用，不能信任或只相加 carrier 内已有计数。M11 终检或
CrossView 失败都归 `sequence_projection_mismatch`，拒绝并回滚整个当前 set attempt。

`SequenceDeliveryEmitter.prepare_product` 是 `delivery_digest` 的唯一属主：它只计算一次并写入
report 的深拷贝。`GenerationProduct` 不复制 digest 或 manifest input；`commit` 只从深度冻结的
`product.report.delivery_digest` 构造 manifest，不得重算第二份摘要。缺失或格式非法必须在打开 `.part`
前以 `generation_downstream_contract` 终止。

复杂接口使用冻结 request dataclass，函数不超过五个参数；全部接口声明须有 doxygen style 中文 docstring。
generation 包不导出旧函数名或参数转换 wrapper。
