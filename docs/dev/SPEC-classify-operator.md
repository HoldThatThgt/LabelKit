# 特性开发规格：分类算子 classify 与按类条件化（spec v1.7）

> 2026-07-07。本文件是 **v1.7 特性的开发规格（implementation-ready）**：需求与业界论证见 `PROPOSAL-classify-operator.md`（本文不重复）；本文在提案基础上并入六域 fan-out 可行性审查的全部裁决，给出规范文本、完整文件修改清单与开发计划。
> **状态：待需求方终审。**终审通过后按 §4 清单把规范文本散布合入 spec/、CONTRACTS.md 与手册（仓库惯例：文档先行，再实现）；§7 开放决策点带默认裁决，需求方可改。

---

## 1. 可行性结论（fan-out 审查汇总）

六个并行审查域（各自对抗式通读相关源码与契约后判决），**全部 feasible_with_frictions，无 blocker**：

| 审查域 | 判决 | 摩擦数 | 一句话结论 |
|---|---|---|---|
| M1 配置层 | feasible_with_frictions | 7 | 每项都有 loader 现成先例可套；真设计工作在互斥键「选择组」合并与 per-class rubric 重解析 |
| M4 分池打分 | feasible_with_frictions | 7 | pairing→BT→归一化→门控已全部按 items 子集参数化，分池只需 run() 套一层池循环；摩擦集中在计数器/报表/trace 的池维度 |
| M6 生成 / M10 编排 | feasible_with_frictions | 8 | 链插入与批计数天然自洽；规划器需按类分段（CallPlan 带类名）；counts.fanout 被契约钉死归 M10 |
| M13 / M8 / 契约机制 | feasible_with_frictions | 7 | 内部 Schema/gather 派发/原地 append 全有先例；uniqueItems 必须移出 Schema；类标签穿不过 M5 冻结签名 |
| M5/M7/M11/M12 | feasible_with_frictions | 6 | 以只增为主；M5/M7 需先修 CONTRACTS §7.4 签名（提案量级「小」上调为「中」）；multi 下 rejects/trace 需 label 消歧 |
| 文档盘点 | — | — | 盘点 67 文件；与其余五域清单合并去重后共 **70**（spec 18、CONTRACTS 1、手册 18、代码 15、测试 13、examples 2、根 3——盘点域漏了 `cli.py` 与 `test_cli.py`/`test_obslog.py`，由 M1/M13/M5 域补齐）；提案 §5 有 8 类遗漏，已并入 §4 |

编排者 inline 复核过的承重事实：`counts.*` 仅 M10 可增（CONTRACTS §11 冻结，docs/CONTRACTS.md 约 1702 行）；rejects 归因取 `item.errors[0]`（`labelkit/emitter.py:392-394`）；openai_compatible L0 无条件 `strict: true` 透传 Schema（`labelkit/llm_client.py:254-258`）；trace 通道合法值唯一硬编码于 `labelkit/config/loader.py:67`；report 桶字段白名单漏 `rejected_by_validator`（`labelkit/orchestrator.py:484` vs `labelkit/generate.py:295/314`，现存 bug，见 §6）。

## 2. 设计裁决记录（对提案的修正与细化；终审后并入 spec §1.6）

审查发现的 35 条摩擦收敛为以下裁决。凡与 `PROPOSAL-classify-operator.md` 原文不一致处，以本表为准：

| # | 问题 | 裁决 |
|---|---|---|
| R1 | multi Schema 的 `uniqueItems` 会被 OpenAI strict 模式与部分约束解码网关硬拒（L0 无条件透传） | **Schema 不写 uniqueItems**；重复标签由 classify 代码在 M8 验证后确定性归一化（去重，属已验证集合的收窄），内部 Schema 关键字集保持零增量 |
| R2 | 类标签穿不过 M5 冻结签名（`build_annotate_prompt` / `annotate_record` 只见 frozen Record；multi 克隆共享 record.id，映射法不可行） | 两函数各增末位可选形参 `label: str \| None = None`（默认 None = 现行为，旧调用点零改动）；CONTRACTS §7.4 与 §12.11 修订。M5/M7 stage 层从 `item.classification.label` 传入 |
| R3 | verify 提示词的 `[任务指令]` 段嵌入 `cfg.annotate.instruction`（`verify.py:99/104`），按类标注 + 全局评审指令是语义 bug | `build_verify_prompt` 增 `label` 形参（该函数不在契约内，零契约成本）：`[任务指令]` 与 `extra_criteria` 均取类有效值；`_judge_round`/`_reannotate` 透传 label |
| R4 | `on_error="fallback"` 若写 `item.errors` 会污染 rejects 归因（`_reject_stage_reason` 取 `errors[0]`，冻结规则） | **fallback 留痕不写 `item.errors`**：改放 `Classification.detail`（含 kind 与消息）+ error trace 事件 + 计数器 `classify.fallback`；`item.errors` 仅 `on_error="fail"` 时写。emitter 归因规则不动 |
| R5 | multi 扇出后同 record.id 的兄弟信封在 rejects 与逐记录 trace 事件中不可区分 | classify 启用时：rejects `_meta` 增 `label` 键（修订 §9.2 封闭五键枚举→六键）；逐记录事件（`annotate.done`/`verify.verdict`/`quality.gate`/`error`）payload 增 `label` 字段（§8.1 契约本就只增） |
| R6 | 互斥键继承碰撞：全局 threshold + 类设 top_ratio（或反向）逐键 replace 后两键并存 → 按提案原文校验即误报 | **按「选择组」合并**：类显式提供 selection/threshold/top_ratio 任一 ⇒ 合并视图剔除全局侧互斥对键；合并期维护逐键 provenance；互斥校验跑在合并后视图上 |
| R7 | per-class rubric 不是逐键 replace：selector 字符串与解析产物分居两字段，inline 解析未函数化 | 抽 `_resolve_rubric(col, selector, raw, label)` helper；per-class 合并 =「合并 selector → 重解析」；pointwise 6 级校验跑在「类有效 mode × 类有效 rubric」组合上；`[class.X.rubric]` 在场但该类 selector 非 inline ⇒ 忽略并 warning（同全局惯例） |
| R8 | `classify.enabled=false` + `[class.*]`/`[[classify.classes]]` 在场：提案定 CONFIG_ERROR，与仓库 no-op 键分级惯例（warning）冲突 | **改为 warning**（一次、点名被忽略的表），对齐 top_ratio 未生效等家族先例；「留配置、关开关」合法 |
| R9 | `counts.fanout` 所有权：扇出发生在 classify 内，但 `counts.*` 仅 M10 可增（冻结） | M10 在 `_process_batch` 链循环对 classify 阶段记 `len(batch)` 前后增量并计数（与从 generate 返回值计 generated 同构）；契约 §11 所有权表加一行 |
| R10 | 熔断部分交付的 `unprocessed` 残差公式（`orchestrator.py:420-423`）未加 fanout 项，multi 下会少报 | counts 块按 `assignment=="multi"` 条件挂 `fanout` 键；残差公式右侧同步 `+ fanout` |
| R11 | 提案的 `classify_calls = total_records × …` 与继承语义矛盾（回流子批跳过 classify） | 修正：process 模式 = `ingested × max(1, sc)`；generate_only = `生成记录数 × max(1, sc)` |
| R12 | 同名 criterion 跨类 rubric 会使 tie 计数器与 report 统计混池 | classify 启用时计数器键升维为 `quality.tie_outcomes.<pool>.<crit>`（tie_comparisons 同）；M10 直方图/均值累加器升维为 (pool, crit)；report 增 `quality.by_class`；关闭时键格式与报表形状不变 |
| R13 | 「类池按字典序处理」的并发-确定性歧义：逐池串行 await 损吞吐，池协程并发则 rng 消费序不定 | **两阶段**：同步循环按类名字典序为每池预抽配对计划（消费 ctx.rng），再把各池 LLM 调用合并为一个 gather（跨池满并发）。`_run_pairwise` 拆 plan/dispatch 两相 |
| R14 | report 顶层 `quality.mode/rounds` 在 per-class 覆盖下失真；tie_rate 输出以全局 mode 为门 | 顶层字段保留（= 全局继承基值）；`by_class` 每池携带有效 mode/rounds；tie_rate 门改「存在 pairwise 池」 |
| R15 | quality 批级 internal-error 兜底（`quality.py:278-289`）会连坐其他池 | try/except 移入池循环：池级隔离，A 池失效不波及已完成的 B 池 |
| R16 | `quality.bt_fit` 事件 record_ids 恒空，分池后不可归因 | classify 启用时 `quality.bt_fit`/`quality.gate`/`quality.judgment` payload 增 `pool` 字段（只增） |
| R17 | M6 规划器单池单指令结构；类归属穿不过 `postprocess_samples` 的扁平返回值 | `CallPlan` 增 `class_name` 字段；`one_call` 按 plan.class_name 取类有效 instruction/temperature；`postprocess_samples` 返回 `list[tuple[Record, str \| None]]`；`run()` 构造 PipelineItem 时带 `source="inherited"` 的 Classification |
| R18 | 类间调用序未定义（预抽可复现性） | **字典序段拼接**：参与类（有种子的类）按类名字典序占据连续全局调用序号区间；预算 `C_c = ⌈len(seeds_c) × num_per_record_c / num_per_call⌉`；单遍 i=0..C−1 预抽——llm 照旧按全局序号（round_robin 零 rng 消耗 / weighted 逐 i 一次 choices），style 从**该 i 所属类**的有效 styles 均匀抽；种子抽样按全局序号升序逐调用。classify 关闭 ⇒ 单一匿名段 = 现行为 |
| R19 | 种子门槛的按类默认链未定义 | 每类：全局 `seed_min_score` → 缺省取**该类有效** `quality.threshold` → 再缺省取**该类种子池**聚合分中位数；`select_seeds` 按 label 分组返回 |
| R20 | `batch.start.size` 为扇出前基数，与 batch.end 各态计数和不再相等 | 语义定为「批入口信封数」；`batch.end` payload 增 `fanout` 字段（只增） |
| R21 | 提案漏 `classify.sc_temperature` 键（写了「同 annotate.sc_temperature 机制」但键表没有） | 补键 `classify.sc_temperature`（float，默认 0.7，仅 sc ≥ 3 生效） |
| R22 | Stage 契约例外措辞歧义（「不修改既有元素」与写 status/classification 矛盾） | 契约 ②a 采用精确措辞，见 §3.2 |
| R23 | `ResolvedConfig` 全必填风格 vs 新字段默认值 | 保持**全必填无默认**风格（新增 `classify`、`class_views` 两必填字段）；直接构造 ResolvedConfig 的 ~14 个测试文件做机械补参 |
| R24 | 三处 profile 引用集独立枚举（密钥解析 / vision 校验 / probe），漏一处即运行期失败 | `loader.py` referenced 集、vision_users、`cli.referenced_profiles()` 三处各加 classify 分支，测试三处各覆盖 |
| R25 | 顶层未知键现仅 warning；`[class.*]` 白名单要求 error | spec 3.1.4「未知键报 warning」行加例外句：`[classify]`/`[class.*]` 显式接管，`[class.*]` 内白名单外键报 CONFIG_ERROR |
| R26 | sc 投票不复用 `annotate._majority_vote`（其无过半回退首样本，语义不合） | classify 自写投票（single 多数票、无过半归兜底；multi 逐标签 > n/2、全落选归兜底）；gather 骨架抄 annotate 结构而非 import |
| R27 | UI 模态 Part 组装三处各自为政（模板逐字冻结、标签互异） | classify 抄 `annotate.py:78-88` 单记录形态，在 CONTRACTS §10 钉死自己的逐字模板 |
| R28 | dry-run 在 per-class quality 覆盖下静态不可精确算 | quality/annotate/verify 估算按全局继承配置；存在 `[class.*]` 覆盖或 multi 时 stderr 注明「按全局配置估算 / multi 按标签乘数 1 报下界」 |
| R29 | classify 事件的 stderr 级别与默认通道 | `classify.decision` 为 trace-only（无 stderr 镜像，同 quality.judgment）；默认 `trace.channels` 保持 `["quality","verify","schema"]` 不变，用户显式加 `"classify"`；reason 请求条件 = trace.enabled 且 channels 含 classify |
| R30 | 手册新章编号：链序插入需全书重排 | **追加制**：新章 `docs/manual/24-classify.md`，README 目录与第 9–13 章交叉引用它（开放决策点⑨，可改） |

## 3. 规格正文（拟合入各文档的规范内容）

### 3.1 M13 classify 模块规格（新文件 `spec/313-m13-classify.md`）

**职责/边界/依赖**（入 §2.2.1 表）：职责 = 按用户类别表对批内 `status="active"` 且 `classification is None` 的记录做 LLM 封闭集分类（单/多标签可配，可选 self-consistency 投票）；结果写 `item.classification`；multi 模式按标签向批尾扇出兄弟信封。边界 = 不淘汰记录；不定义类别语义；不做标注；不改链结构（扇出只改批内信封基数）。依赖 M1, M8, M9。

**提示词模板**（确定性拼接，逐字入 CONTRACTS §10.8）：

```
system:
  single: 你是数据分类员。阅读待分类数据，判断它属于以下类别中的哪一类。类别表：
  multi:  你是数据分类员。阅读待分类数据，判断它适用于以下哪些类别（至少 1 类，至多 {max_labels} 类）。类别表：
  - {name}: {description}                       ← 按 [[classify.classes]] 声明序逐类一行
  {classify.instruction}                        ← 可选补充说明；缺省省略此行
  输出必须是符合以下结构的单个 JSON 对象，不输出任何其他内容：
  single: {"class": <类名>[, "reason": <一句话理由>]}
  multi:  {"classes": [<类名>, ...][, "reason": <一句话理由>]}   ← reason 仅请求时出现于两式
user (对每条配置了 examples 的类，按声明序；类内按数组序):
  [类别示例·{name}] {example}
user (当前记录):
  文本模态: [待分类数据] {record.text}
  UI 模态:  [屏幕截图] <image: base64>
           [UI 控件树] {record.ui_tree.serialize(max_chars=input.ui_tree_max_chars)}
```

**内部 Schema**（`schema_engine.classification_schema`，逐字入 CONTRACTS §10.7；关键字集 ⊆ 既有冻结集，**无 uniqueItems**，R1）：

```python
def classification_schema(class_names: list[str], assignment: str,
                          max_labels: int, with_reason: bool) -> dict:
    if assignment == "single":
        props: dict = {"class": {"type": "string", "enum": list(class_names)}}
        required = ["class"]
    else:
        props = {"classes": {"type": "array",
                             "items": {"type": "string", "enum": list(class_names)},
                             "minItems": 1, "maxItems": max_labels}}
        required = ["classes"]
    if with_reason:
        props["reason"] = {"type": "string"}
        required += ["reason"]
    return {"type": "object", "properties": props,
            "required": required, "additionalProperties": False}
```

**算法规格**：

| 设计点 | 定义 |
|---|---|
| 调用与校验 | 每记录 1 次调用（sc 时 ×n），经 `complete_validated(schema=classification_schema(...))`——内部 Schema：不计 resolved_at、不过 L2.5。reason 请求条件见 R29。temperature 0（sc 采样取 `classify.sc_temperature`）。批内记录级并发（asyncio.gather + profile 信号量，骨架同 M5） |
| 归一化（M8 之后，确定性，顺序固定） | ① 标签映射到类别表声明序并**去重**；② 兜底类与具体类同现 ⇒ 剔除兜底类（纯兜底保留）。归一化只收窄已验证集合 |
| sc 投票 | `self_consistency = n`（0 关；≥3 奇数）：n 次独立采样（SchemaViolation 样本弃权，分母仍 n）。single：多数票，无过半 ⇒ 归兜底类；multi：逐标签保留出现于 > n/2 个采样集合者，全落选 ⇒ 归兜底类。`detail.sc = {"n", "agreement_ratio"}`（single = 胜出类票占比；multi = 保留标签中最低票占比） |
| 失败与兜底 | M8 修复耗尽：`on_error="fallback"`（默认）⇒ 归兜底类，`source="fallback"`，留痕写 `Classification.detail`（不写 item.errors，R4）+ error 事件（kind=`classification_invalid`）+ 计数器；`on_error="fail"` ⇒ `status="failed"`、StageError 入 item.errors ⇒ rejects |
| multi 扇出 | 归一化后 k ≥ 2：原信封取首标签（声明序），其余 k−1 标签各克隆一个兄弟 `PipelineItem` **原地追加到传入批列表尾部**——克隆共享 `record` 与 `dedup`（引用；保证兄弟行 `_meta.dedup` 一致），`classification` 换 label（labels 同全集），`status="active"`，scores/annotation/verification/errors 为全新默认容器。追加序 =（原元素批内位置 → 标签声明序），逐字节可复现。返回值 = 传入的同一列表对象 |
| 幂等 | `classification is not None` 的项跳过（覆盖生成样本的 inherited 继承与任何重入） |
| 事件与计数 | 每记录一条 `classify.decision`（payload：`label`、`labels`（multi 携带全集）、`source`、`reason`†、`sc`†）；计数器（M13 属主）：`classify.classes.<name>`（逐标签计）、`classify.fallback`、`classify.failures`、`classify.multi_label_records`；`counts.fanout` 由 M10 计（R9） |

**API**（入 CONTRACTS §7 新节）：

```python
class ClassifyStage(Stage):
    name = "classify"
    def __init__(self, cfg: ResolvedConfig): ...
    async def run(self, batch, ctx) -> list[PipelineItem]: ...   # 返回传入的同一列表（multi 可尾部追加）

def build_classify_prompt(record: Record, cfg: ResolvedConfig,
                          with_reason: bool) -> PromptBundle       # §10.8 模板的确定性组装
async def classify_record(record: Record, ctx: RunContext) -> Classification
```

### 3.2 数据结构与 Stage 契约（spec §4 修订）

```python
@dataclass(frozen=True)
class Classification:
    label: str                            # 本信封路由标签
    labels: tuple[str, ...]               # 该记录命中全集（声明序；single 恒单元素）
    source: Literal["llm", "fallback", "inherited"]
    detail: Mapping                       # reason / sc 统计 / fallback 留痕（kind, message）

@dataclass
class PipelineItem:
    ...                                   # 既有字段不变
    classification: Classification | None = None
```

Stage 契约（spec §4.3 / `stage.py` docstring / CONTRACTS §5，在既有 ①–④ 上增补）：

> **②a classify 例外（仅 `assignment="multi"`）**——可向传入列表**尾部**追加派生信封；追加物视同批内普通元素、同受 ①③④ 约束；不得删除、重排或替换任何既有元素对象（既有元素的 status / classification / errors 字段写入属 ①④ 的正常行为）；返回值仍须是传入的同一列表对象（调用方依赖列表身份）。

### 3.3 配置规格（spec §5.2 增量）

`[classify]` 键表：

| 键 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `classify.enabled` | bool | false | 默认关——工具行为与 v1.6 完全一致（`_meta.classification: null` 除外） |
| `classify.llm` | str | "default" | profile 引用；UI 模态须 supports_vision（M1 校验）；计入密钥解析/vision/probe 三处引用集（R24） |
| `classify.assignment` | str | "single" | "single"（锁定一条一类）\| "multi"（允许多类命中并扇出） |
| `classify.max_labels` | int | 类别数 | 仅 multi 可设；∈ [2, 类别数]；M1 解析后回填 |
| `classify.instruction` | str | "" | 可选补充说明，追加进 system 类别表之后 |
| `classify.fallback_class` | str | 必填† | † enabled 时必填且 ∈ classes |
| `classify.self_consistency` | int | 0 | 0 或 ≥3 奇数（M1 校验） |
| `classify.sc_temperature` | float | 0.7 | 仅 sc ≥ 3 生效（R21） |
| `classify.on_error` | str | "fallback" | "fallback" \| "fail" |
| `[[classify.classes]]` | array | 必填† | † enabled 时 ≥ 2 项。每项：`name`（`[a-z0-9_]+`，表内唯一）、`description`（非空）、`examples`（字符串数组，可选，仅输入侧） |

`[class.<name>.<section>]` 覆盖白名单（同提案 §4.4 表；白名单外键 CONFIG_ERROR）。**合并语义**（M1 启动时静态合并冻结为 `class_views`）：

- 逐键 provenance 合并：类显式提供的键覆盖全局，未提供的键继承全局；
- **选择组**（R6）：类提供 selection/threshold/top_ratio 任一 ⇒ 合并视图剔除全局侧的互斥对键；互斥校验跑在合并后视图上；
- **rubric**（R7）：合并 selector → `_resolve_rubric` 重解析；pointwise 6 级校验跑在（类有效 mode × 类有效 rubric）组合上；`[class.X.rubric]` 在场但 selector 非 inline ⇒ 忽略 + warning；
- 类 examples 干跑全局用户 Schema 与 validator（错误定位 `[[class.<name>.annotate.examples]][N]`）；
- `enabled=false` 而 `[[classify.classes]]` / `[class.*]` 在场 ⇒ **warning**（R8，偏离提案的 CONFIG_ERROR）。

配置 dataclass（CONTRACTS §6.1 verbatim；`ResolvedConfig` 增 `classify: ClassifyConfig` 与 `class_views: Mapping[str, ClassView]` 两**必填**字段，R23）：

```python
@dataclass(frozen=True)
class ClassSpec:
    name: str; description: str
    examples: tuple[str, ...] = ()

@dataclass(frozen=True)
class ClassifyConfig:
    enabled: bool = False
    llm: str = "default"
    assignment: Literal["single", "multi"] = "single"
    max_labels: int | None = None          # M1 回填为类别数
    instruction: str = ""
    fallback_class: str = ""
    self_consistency: int = 0
    sc_temperature: float = 0.7
    on_error: Literal["fallback", "fail"] = "fallback"
    classes: tuple[ClassSpec, ...] = ()

@dataclass(frozen=True)
class ClassView:                           # 一类的有效配置；enabled=false 时 class_views = {}
    name: str
    quality: QualityConfig                 # 选择组语义合并（R6）；rubric selector 已回填
    rubric: Rubric                         # 重解析产物（R7）
    annotate: AnnotateConfig
    generate: GenerateConfig
    verify: VerifyConfig
```

### 3.4 下游算子修订

**M4 quality（spec §3.4.3 增补）**：active 项按 `classification.label` 分池（classify 关闭 ⇒ 单一匿名池 = 现行为，零变化回归锚）。两阶段执行（R13）：同步按类名字典序逐池预抽配对计划（消费 ctx.rng），再跨池合并一个 gather 派发。每池取 `class_views[label]` 的 (QualityConfig, Rubric)：mode/rounds/rubric/threshold/selection/top_ratio 池内生效；judges/both_orders/criteria_per_call/llm/on_unscored 恒为全局。池级 try/except 隔离（R15）。N=1 池沿用现行单条规则。top_ratio 名额基数 = 池内 scored 存活数。计数器与统计升维（R12）：classify 启用时 `quality.tie_outcomes.<pool>.<crit>`；`quality.bt_fit`/`gate`/`judgment` payload 增 `pool`（R16）。切口：`_run_pairwise`/`_run_pointwise`/`_set_aggregates`/`_apply_gate` 加 (q, criteria, pool) 参数；`fit_bradley_terry`、`_pairing_plan`、`_percentile_scores`、`_top_ratio_selection`、prompt builder 全部不动。

**M5 annotate（spec §3.5.2 增补 + CONTRACTS §7.4 修订，R2）**：`build_annotate_prompt(..., label: str | None = None)`、`annotate_record(..., label: str | None = None)`——label 非 None 时 instruction/examples 取 `class_views[label].annotate`；stage 层传 `item.classification.label if item.classification else None`。

**M6 generate（spec §3.6.2 增补）**：种子按 label 分组（R19 门槛链）；类段按字典序拼接全局调用序（R18）；`CallPlan` 增 `class_name`，`one_call` 按类取 instruction/temperature（R17）；styles 按类抽、llm 仍全局序号轮转/加权；`postprocess_samples` 返回 (Record, class)，构造 PipelineItem 带 `Classification(label, (label,), "inherited", {})`；桶 key classify 启用时为 `<class>×<llm>×<style>`。generate_only：`generate_all` 扁平路径不动（全局指令），产物由链上 classify 正常分类。

**M7 verify（spec §3.7.2 增补，R3）**：`build_verify_prompt(..., label)`——`[任务指令]` 段与 `extra_criteria` 均取类有效值；`_judge_round`/`_reannotate` 增 label 形参并透传（repair 重标注调 `annotate_record(..., label=...)`）。

### 3.5 编排与输出

**M10（spec §3.10.3 增补）**：`_CHAIN_ORDER = ("dedup", "classify", "quality", "generate", "annotate", "verify")`；`_compose_chain` enabled 表加 classify（主链/回流链/generate_only 链均含，继承项靠幂等跳过）；`counts.fanout` = classify 阶段前后 `len(batch)` 差值，M10 计（R9）；`batch.end` payload 增 `fanout`（R20）；熔断残差公式 `+ fanout`（R10）；`_estimate` 增 `classify_calls`（R11 公式）+ stderr 注记（R28）。

**M11（spec §3.11 / §6.3 / §6.4 增补）**：`_meta` 增恒在键 `"classification": {"label", "labels", "source"}`（未启用 = null）；`_meta.scores` 增 `pool`（仅 classify 启用）；rejects `_meta` 增 `label` 键（仅 classify 启用，R5——§9.2 五键枚举修订为六键）。report：

```
"classify": {"assignment": "...", "classes": {<name>: n}, "fallback_count": n, "failures": n
             [, "multi_label_records": n]}                    // 仅 enabled 时出现
"counts":   {... [, "fanout": n]}                             // 仅 multi 时出现
"quality":  {..., "by_class": {<pool>: {"mode", "rounds", "aggregate_histogram",
             "per_criterion_mean", "per_criterion_tie_rate"}}} // 仅 classify 启用时出现（R12/R14）
"generate": {"buckets": {"<class>×<llm>×<style>": {...}}}      // 仅 classify 启用时含类前缀
```

不变量（spec §6.4）：`emitted + dropped_* + failed + bad_input = scanned + generated [+ fanout]`；熔断中止再 `+ unprocessed`。

### 3.6 可观测性（spec §7.2 / §7.6 增补）

- `trace.channels` 枚举 7→8（加 `"classify"`；默认值不变，R29）；唯一改点 `loader.py:67 _TRACE_CHANNELS`。
- 新事件行：`classify.decision`｜classify 通道 / trace-only｜M13 每记录分类定案后｜payload `label`、`labels`（multi）、`source`、`reason`†、`sc`†。
- 既有事件只增字段：`quality.bt_fit`/`quality.gate`/`quality.judgment` 增 `pool`；`annotate.done`/`verify.verdict`/`error` 增 `label`（均仅 classify 启用时携带）；`batch.end` 增 `fanout`。
- 错误码表（§7.6）增行：`classification_invalid`｜记录级｜M13：M8 修复耗尽——on_error="fallback" 时归兜底类并留痕于 `Classification.detail`（不入 rejects），"fail" 时记录 failed → rejects。

### 3.7 CONTRACTS.md 修订面（冻结点变更一览）

§1 包布局表加 `classify.py`；§2 架构 recap 链序；§3 `types.py` verbatim（Classification/PipelineItem）；§4 `errors.py` verbatim（新 kind；顺带补齐存量漂移：`SchemaViolation` 缺 v1.5 `callback_only` 参数的镜像）；§5 Stage 契约 ②a 与「返回同一列表」句；§6.1/§6.3 配置 dataclass 与校验清单；§7 新增 classify API 节 + **§7.4 两签名修订（R2）** + §7.5/§7.9 规范段；§8.1 事件目录；§9.1 `_meta`、§9.2 rejects 六键、§9.3 计数器词表（`counts.fanout`、`classify.*`、tie 计数器池维键式）与所有权表、不变量；§10 新增 §10.8 分类提示词模板 + §10.7 增 `classification_schema`（注明去重在代码侧归一化）；§12 决策登记。

## 4. 文件修改清单（全量 70 文件 + 3 项操作性）

### spec/（17 改 + 1 新）

| 文件 | 动作 | 内容 |
|---|---|---|
| `spec/313-m13-classify.md` | 新建 | M13 模块节全文（§3.1 规格正文展开，按 §3 统一模板） |
| `spec/00-frontmatter.md` | 修改 | 版本历史表加 v1.7 行 |
| `spec/10-ch1-overview.md` | 修改 | §1.4 需求映射加行；§1.5 背书表加 4 条；§1.6 记录对齐决策（含 §7 裁决与 §2 裁决表要点） |
| `spec/20-ch2-overall-design.md` | 修改 | §2.1.1 功能表加分类行；§2.1.2 ⑥ 补「不做跨类输出配比」划界；§2.2.1 模块表 + 图 2-1/2-2 加 M13；§2.3.1 开关矩阵与约束；§2.3.2 加 classify/扇出行；§2.4 dry-run 行；§2.5 配置总览 |
| `spec/301-m1-config.md` | 修改 | §3.1.4 classify 全量校验清单 + 合并语义（R6/R7/R8/R24/R25） |
| `spec/304-m4-qualityqurating.md` | 修改 | §3.4.3 分池规格（§3.4 修订条目） |
| `spec/305-m5-annotate.md` | 修改 | §3.5.2 label 形参与类有效取值 |
| `spec/306-m6-generate.md` | 修改 | §3.6.2 类段/继承/桶 key/门槛链（R17–R19） |
| `spec/307-m7-verify.md` | 修改 | §3.7.2 [任务指令] 与 extra_criteria 按类（R3） |
| `spec/308-m8-schema-engine.md` | 修改 | 内部 Schema 清单加 classification_schema |
| `spec/310-m10-orchestrator.md` | 修改 | §3.10.3 链序/跳过/扇出基数/fanout 计量/估算公式 |
| `spec/311-m11-emitter.md` | 修改 | §3.11.2 report classify 节引用与 rejects label 键；§3.11.3 示例 |
| `spec/40-ch4-data-structures.md` | 修改 | §4.1 Classification + PipelineItem；§4.3 契约 ②a |
| `spec/50-ch5-config-spec.md` | 修改 | §5.2 `[classify]` 键表 + `[class.*]` 白名单表 + 合并优先级；trace.channels 枚举 |
| `spec/60-ch6-io-formats.md` | 修改 | §6.3 `_meta`/pool；§6.4 report 结构、fanout、不变量、multi 行唯一键 |
| `spec/70-ch7-logging.md` | 修改 | §7.2 事件目录（新行 + 只增字段）；§7.6 错误码行 |
| `spec/80-ch8-nongoals-roadmap.md` | 修改 | §8.4 演进候选（embedding 两级分类、开放集 tagging、按类 Schema、适用度打分档、仅打标不扇出档）；O6 注记 |
| `spec/85-ch9-references.md` | 修改 | 加 [37] Nemotron-CC、[38] InsTag、[39] Tülu 3、[40] NeMo Curator 分类器文档 |
| `spec/302/303/309/312/90` | 不改 | M2/M3/M9/M12 无新行为；默认 rubric 不变 |

### docs/CONTRACTS.md（1 改）

见 §3.7 修订面一览（§1–§12 十二处触点，其中 §7.4 为冻结签名修订、§9.2 为封闭枚举修订，其余只增）。

### docs/manual/（17 改 + 1 新；b = 样例输出需重跑重同步）

| 文件 | 动作 | 内容 |
|---|---|---|
| `24-classify.md` | 新建 | 专章：直觉/配置/两种 assignment/扇出与行唯一键/纯打标模式/调优与 fallback 诊断；样例取自 `examples/classify` 真实运行（编号策略见 §7⑨） |
| `README.md` | 修改 | 目录表加新章行 + 阅读路线 |
| `01` | 修改 | §1.4 算子总览表加 classify 工位行 |
| `03-quickstart.md` | **重同步(b)** | §3.3 dry-run 估算行（+`classify_calls=0`）与 §3.4 完整 `_meta` 样例块（+`"classification": null`）——默认关闭也变，重跑 examples/text 后同步 |
| `04-concepts.md` | 修改 | 铁律②加 ②a 例外；守恒等式加 fanout 项；§4.5 组合约束/菜谱表加 classify |
| `07-project-toml.md` | 修改 | §7.4 算子节速览加 `[classify]`；`[class.*]` 概览指向 24 章 |
| `08-outputs.md` | 修改+**重同步(b)** | §8.2 `_meta` 真实块重跑 + classification/pool 字段解读；§8.4 report 加 classify 节/fanout/buckets key/multi 行唯一键警示 |
| `10-quality.md` | 修改 | 分池语义/`scores.pool`/小类池退化指引（per-class pointwise、增大 batch_size） |
| `11-annotate.md` | 修改(轻) | 按类 instruction/examples 一段 + 交叉引用 |
| `12-generate.md` | 修改 | 按类种子池/继承/桶 key/generate_only 不支持按类配比 |
| `13-verify.md` | 修改(轻) | 按类 [任务指令] 与 extra_criteria 一段 |
| `14-schema-engine.md` | 修改(轻) | §14.7 内部结构清单加分类 Schema |
| `15-cli.md` | 修改+**重同步(b)** | §15.1 dry-run 真实输出重跑；multi 估算下界口径 |
| `16-observability.md` | 修改 | §16.2 事件表加 classify.decision；channels 7→8；只增字段说明 |
| `17-tuning.md` | 修改(轻) | §17.1 调用账表加 classify 行；multi ×m̄ 说明 |
| `18-troubleshooting.md` | 修改 | §18.1 错误码表加 classification_invalid（两形态） |
| `19-tutorial-1-minimal.md` | 修改(轻) | §19.5「关闭工位置 null」枚举句补 classification |
| `appendix-a-cheatsheet.md` | 修改 | `[classify]`/`[class.*]` 全键速查 + A.8 组合约束 |
| `02/05/06/09/20/21/22/23/appendix-b` | 不改 | 无受影响规格面或样例（20/21/22 章样例均为 `_meta` 子对象，逐字段不变） |

### labelkit/（14 改 + 1 新）

| 文件 | 改动 |
|---|---|
| `classify.py`（新） | M13 本体：模板组装/sc 投票/归一化/fallback/multi 扇出/事件计数（事件名用模块内字面量，仿 quality.py） |
| `config/model.py` | ClassSpec/ClassifyConfig/ClassView；ResolvedConfig 两必填字段（R23） |
| `config/loader.py` | `_parse_classify`；`[class]` raw 透传 + 白名单合并（选择组/rubric 重解析/provenance）；抽 `_resolve_rubric` 与 few-shot 干跑 helper；referenced/vision_users 加 classify；`_TRACE_CHANNELS` 加 "classify"；R8 warning |
| `types.py` | Classification + PipelineItem.classification |
| `stage.py` | 契约 docstring 增 ②a |
| `errors.py` | ErrorKind 增 CLASSIFICATION_INVALID |
| `schema_engine.py` | classification_schema() |
| `quality.py` | 池循环 + 两阶段 plan/dispatch + (q, criteria, pool) 参数下穿 + 池级 try + 计数器/事件池维 |
| `annotate.py` | 两函数 label 形参；stage 传 label；annotate.done 增 label |
| `verify.py` | build_verify_prompt/label 线程化；[任务指令] 与 extra_criteria 按类；verify.verdict 增 label |
| `generate.py` | select_seeds 按类分组；CallPlan.class_name；类段预抽；postprocess 返回 (Record, class)；inherited 构造；桶 key 类前缀 |
| `orchestrator.py` | 链序；fanout 计量与 batch.end 字段；counts/残差；classify report 节 + quality by_class 累加器升维；`_estimate` classify_calls + stderr 注记；**顺带修 §6 桶白名单 bug** |
| `emitter.py` | `_assemble_meta` classification 键；`_scores_block` pool；rejects label 键 |
| `cli.py` | `_build_stages` 加 ClassifyStage；`referenced_profiles()` 加 classify.llm |
| `__init__.py` | docstring 链序（微） |
| `dedup.py`/`ingest.py`/`llm_client.py`/`obslog.py`/`hooks.py`/`data/` | 不改（无默认类别表——类别表语义完全属用户域） |

### tests/（11 改 + 2 新）

| 文件 | 要点 |
|---|---|
| `test_classify.py`（新） | Schema 形状（无 uniqueItems）；模板组装确定性（类别表/few-shot/UI 三段）；归一化（去重/兜底剔除/声明序/纯兜底）；sc 两模式与弃权；扇出（首标签+克隆追加序/同列表对象/Record 与 dedup 引用共享/容器独立）；幂等跳过；fallback 两形态；非 active 不处理 |
| `integration/test_classify_llm.py`（新） | 真实 glm-5.2：enum 不出词表外标签；fallback 可触发；multi 双意图扇出两行；single 锁定同 fixture 一行 |
| `test_config.py` | 解析/白名单/合并视图（含选择组回归 `test_class_selection_group_merge_not_spuriously_exclusive`）/校验清单/防呆 warning/channels |
| `test_quality.py` | 池字典序/池内 top_ratio 名额/混模式共存/N=1 池/rng 消费序/tie 计数器池维/事件 pool 字段/关闭单池零变化 |
| `test_generate.py` | 类段预算与字典序/round_robin 全局序/按类 style 抽取/inherited 构造/桶 key 前缀/单类退化回归；存量 postprocess 用例适配返回值 |
| `test_orchestrator.py` | 链序/回流跳过/fanout 计数与不变量/残差含 fanout/桶 key 解析/**桶白名单含 rejected_by_validator（顺带修回归）**/dry-run 两模式公式/limit 与扇出 |
| `test_emitter.py` | **存量 `_meta` 键全集断言须加 classification**；null 语义/pool/rejects label 键 |
| `test_annotate.py` | 类有效 instruction/examples；label=None 回退全局；stage 传 label |
| `test_verify.py` | 类有效 [任务指令]+extra_criteria；repair 线程化 label |
| `test_types.py` | Classification 冻结性；PipelineItem 默认 None 与容器独立性 |
| `test_schema_engine.py` | classification_schema 二态 |
| `test_obslog.py` | classify.decision 通道路由；classify 阶段 error 事件归属 |
| `test_cli.py` | referenced_profiles 含 classify.llm |
| （横切） | 直接构造 ResolvedConfig 的 ~14 个测试文件机械补 classify/class_views 两参（R23） |

### examples/ 与根目录

| 文件 | 动作 | 内容 |
|---|---|---|
| `examples/classify/project.toml`（新） | 新建 | 混合意图工程：[classify] + 三类（含 fallback）+ [class.*] 覆盖示范 + multi 变体注释 |
| `examples/classify/data/input.jsonl`（新） | 新建 | 各类样本 + 语义模糊样本（触发 fallback）+ 双意图样本（multi 验收） |
| `CLAUDE.md` / `AGENTS.md` | 修改（两份逐字同步） | 模块映射加 M13→classify.py；算子层清单；链序；v1.7 修订注；examples/classify |
| `README.md` | 修改 | 算子计数与流水线图；示例工程列表 |
| 操作性（不计文件数） | — | ① `examples/{text,ui,generate}/out/` 重跑刷新（`_meta` 多 `classification: null`，为手册 3/8/15 章重同步的前置）；② `docs/dev/E2E-FINDINGS.md` 追加桶白名单 bug 条目（见 §6）；③ `PROPOSAL-classify-operator.md` 状态行更新 |

## 5. 开发计划

依赖序分八步，每步以离线测试全绿为门禁（`uv run pytest -q -m 'not integration'`），红不进下一步：

| 步 | 内容 | 门禁（观测定义） |
|---|---|---|
| 0 | 按 §4 清单合入 spec / CONTRACTS 修订（文档先行） | 文档交叉引用自洽（§5.2 表 ↔ §6.3 校验清单 ↔ §7.2 事件目录逐条对得上） |
| 1 | M1：model + loader（解析/白名单/选择组合并/rubric 重解析/三处引用集/防呆） | `test_config.py` 全绿；`labelkit validate` 对 examples/classify 通过 |
| 2 | types/stage/errors/schema_engine + `classify.py` 本体 | `test_classify.py` / `test_types.py` / `test_schema_engine.py` 全绿 |
| 3 | M10 链插入 + fanout 计量 + 估算；M11 `_meta`/rejects；channels | `test_orchestrator.py` / `test_emitter.py` / `test_obslog.py` 全绿 |
| 4 | M4 分池（两阶段 + 参数下穿 + 池维计数） | `test_quality.py` 全绿，单池路径存量用例原样通过（零变化锚） |
| 5 | M5/M7 label 线程化 | `test_annotate.py` / `test_verify.py` 全绿 |
| 6 | M6 类段（预算/预抽/继承/桶 key）+ 顺带修桶白名单 bug | `test_generate.py` 全绿 + 白名单回归用例先红后绿 |
| 7 | `examples/classify` 工程 + 集成测试（真实 glm-5.2）+ 存量回归 | `uv run pytest tests/integration -q -m integration` 全绿；三个存量 example 重跑与 v1.6 产物逐字段 diff 仅差 `classification: null` |
| 8 | 手册（新章 + 17 章增改 + 3/8/15 章样例重同步）+ CLAUDE/AGENTS/README | 手册中每个样例块与真实运行产物逐字一致 |

**验收标准**（在提案 §6 基础上并入审查结论）：① `examples/classify` 真实运行——report.classify 分布合理、trace 抽查各类走各自 instruction、counts 不变量成立；② multi 双意图 fixture 两行 / single 锁定一行、行键 (`_meta.id`, label) 唯一、`counts.fanout` 与扩展不变量（含熔断残差）成立；③ enum 硬约束下不出词表外标签、fallback 可触发且 rejects 归因不被污染（R4 回归）；④ `classify.enabled=false` 全量回归逐字段一致（除 `classification: null`）；⑤ `--strict`/dry-run/熔断交付语义不变；⑥ 选择组合并回归（全局 threshold + 类 top_ratio 不误报互斥）。

## 6. 顺带修复与已知锐边

- **现存 bug（本次顺带修）**：report 桶字段白名单（`orchestrator.py:484`）漏 `rejected_by_validator`——M6 计的该计数器（`generate.py:295/314`）永远到不了 report.json，与 spec §6.4 / CONTRACTS §9.3 承诺相矛盾。修复 + 回归测试 + E2E-FINDINGS 台账补条目。
- **存量契约漂移（触碰时一并补齐）**：CONTRACTS §4 的 `SchemaViolation` verbatim 块缺 v1.5 `callback_only` 参数镜像（CONTRACTS:328 vs `errors.py:53-54`）。
- **已知锐边（记录不修）**：quality 的 `judgment_invalid` 留痕在记录后续失败时会以 `errors[0]` 误归因 rejects（罕见路径，先于本特性存在；classify 侧已用 R4 规避同型问题）。
- **确定性条件化声明（写入 spec §2.6 幂等行）**：分池构成与扇出以分类输出为条件——temperature 0 下端点无逐字节保证，配对计划的可复现性从「仅依赖 seed」条件化为「以分类结果为条件」（generate 回流已有同类先例）。

## 7. 开放决策点（默认裁决随本 spec 生效，需求方可改）

1. 模块编号：**追加 M13**（默认）。
2. fallback 语义：**普通类成员、必填**（默认）。
3. generate_only 按类配比：**不做**，与 §8.3 O6 一并立项（默认）。
4. 白名单放开 per-class `quality.llm`/`annotate.llm`：**v1 不放**（默认）。
5. 纯打标模式显式开关：**不加**（零覆盖自然退化）（默认）。
6. 多标签中间档（仅打标不扇出）：**暂不加**，`assignment` 枚举留扩展位（默认）。
7. dry-run multi 口径：**乘数 1 报下界 + stderr 注明**（默认，R28）。
8. `enabled=false` + 类配置在场：**warning**（本 spec 裁决 R8，偏离提案的 CONFIG_ERROR——对齐仓库 no-op 诊断惯例）。
9. 手册新章编号：**追加制 `24-classify.md`**（默认，R30）；备选为链序插入（第 10 章前），代价是全书章号重排与交叉引用重写。
