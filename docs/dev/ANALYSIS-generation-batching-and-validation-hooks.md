# 需求分析：生成的批量产出与校验看护 / 生成与标注的校验回调注册

> 2026-07-03。两个需求的现状分析、缺口清点与设计方案对比。
> **状态更新（同日）：方案 A 已获需求方认可并实现落地**（spec §3.8.2 L2.5 / §3.6.2、`[output].validator` 与 `[generate].sample_validator`、手册 14.5/12.5）；「缺口 A（部分接受）」维持待立项。以下为原始分析。

---

## 需求一：生成任务能否一次模型调用生成多条数据？批量生成有没有校验看护？

### 结论先行

**两问的答案都是「已经支持」**：单次调用产多条是生成算子的默认工作方式（`generate.num_per_call`，默认 4）；批量产出的看护不是单一开关，而是一条五道闸的链路，全部默认在位。真正的缺口在两处边界（见「缺口」小节）。

### 现状：一次调用产多条

`generate.num_per_call = N` 控制每次调用要求模型产出恰好 N 条样本。这不是提示词里的软性要求——生成输出走结构引擎的内部 Schema（`{"samples": [str, ...]}`，`minItems = maxItems = N`），四层保证照常生效：条数不对、类型不对都会触发确定性修复 → 校验 → 有界 LLM 修复环，最终不合法则**该次调用作废**（计入桶统计 `calls`，`produced = 0`），不影响其他调用与原批（记录级隔离在调用粒度上的对应物）。

调用次数由预算公式反推：`⌈种子数 × num_per_record / num_per_call⌉`（无种子形态为 `⌈standalone_count / num_per_call⌉`）——调大 `num_per_call` 直接线性减少调用数。

### 现状：批量产出的五道看护闸

| 闸 | 机制 | 可观测落点 |
|---|---|---|
| ① 结构 | 内部 Schema 强约束条数与类型，经 M8 四层保证；截断/破损 JSON 有 L1 修复 + `l1_lossy` 有损告警 | `schema.repair` 事件 |
| ② 新颖性 | 生成算子**内置**相似度过滤（无条件执行）：新样本 vs 种子、vs 同批样本互查，复用 `[dedup]` 的 MinHash 参数 | `report.generate.buckets.*.survived_dedup` |
| ③ 全局查重 | 生成子批**回流**流水线从 dedup 走起：与全部原始记录及先前生成样本查重（global 索引） | `dropped_dup` / rejects |
| ④ 质量闸 | 回流经过 quality：合成品与真实数据同一把尺子打分，可被 threshold/top_ratio 淘汰 | `_meta.scores` / `dropped_lowq` |
| ⑤ 语义评审 | 回流经过 annotate（用户 Schema 四层保证）与 verify（独立评审） | `_meta.verification` / `dropped_verify` |

外加审计面：每条合成记录带 `generator`（llm×style）与 `generated_from` 溯源；`buckets` 统计让「哪个模型×风格组合在产重复货」量化可见。

### 缺口与建议

**缺口 A：数量约束是全有或全无。**`minItems = maxItems = N` 意味着模型产出 3/4 条时整个调用进修复环、修不好则 4 条全部陪葬——没有「部分接受」路径。
*建议*：把内部 Schema 放宽为 `minItems=1, maxItems=N` 并新增桶字段 `short_calls` 计数。代价是 `num_per_record` 预算从「精确」变「上界」；收益是长输出截断场景（`num_per_call` 调大时最常见的故障）可以抢救大部分样本。属行为变更，需对齐后进 spec 3.6.2。

**缺口 B：单条样本无结构约束。**生成输出的每条样本是 `string`——要产出结构化样本（如带槽位的指令对）目前靠「生成文本 → 回流 annotate 结构化」两段式。这个两段式其实是设计优点（结构化统一由用户 Schema + M8 保证，生成专心管多样性），建议**维持现状并在手册言明**，除非出现「样本本身必须是 JSON」的真实场景再立项 per-sample schema。

**调参提醒**（已在手册第 12 章）：`num_per_call` 越大，调用内自我重复越多（盯 `survived_dedup/produced` 新颖率）、输出越容易顶到 `max_output_tokens` 截断（盯 `l1_lossy` 与 `resolved_at.l3_*`）。经验带：4–8。

---

## 需求二：生成/标注输出能否注册校验回调，用代码做进一步硬校验？

### 现状：不支持，且需求正当

当前输出的「硬校验」只有一种表达方式：JSON Schema（声明式）。它覆盖不了：

- **跨字段约束**：`bounds` 四元组须满足 l<r、t<b；`difficulty=hard` 时 `topic` 不得为空泛词；
- **外部一致性**：`label` 必须属于一份运行时词表；UI 元素坐标必须落在该截图分辨率内；
- **业务规则**：日期真实存在、金额格式合法、敏感词黑名单；
- **生成样本的代码级筛选**：长度窗口之外还有语种检测、正则黑名单等。

这些今天只能推给 verify（LLM 评审——语义强但仍是概率产物，恰与「硬校验」诉求相反）或下游后处理（此时坏记录已计入 emitted，丧失了修复环与 rejects 记账）。**用户代码回调是「LLM 输出不可信」原则的自然收尾**：让最硬的裁判（确定性代码）站在离输出最近的位置。

### 设计空间：三个方案

**方案 A：Python 可调用对象注册（推荐）**

```toml
[output]
validator = "my_validators:check_annotation"     # module:function，importlib 加载

[generate]
sample_validator = "my_validators:check_sample"  # 可选，独立注册点
```

约定签名（与 M8 的违规清单格式对齐）：

```python
def check_annotation(obj: dict, record: dict | None) -> list[str]:
    """返回违规描述列表，空列表 = 通过。record 为原始记录（generate 侧为 None）。"""
```

- **挂接点 1（标注）**：M8 `complete_validated` 的 L2 通过之后追加 **L2.5**——回调违规与 Schema 违规同格式并入 `[违规清单]` 回喂 L3 修复环，共享 `max_repair_attempts` 预算；耗尽则按现有 `schema_violation` 路径进 rejects（错误码可细分 `callback_violation`）。这让 LLM **拿着用户代码的意见自我修正**，是本方案最大的复利：回调不只是门卫，还是修复环的教练。
- **挂接点 2（生成）**：`postprocess_samples` 里逐条过 `sample_validator`，违规样本**剔除**（与相似度过滤同语义，不触发重试），桶统计增 `rejected_by_validator`。
- **失败隔离**：回调抛异常 = 该记录 `failed`（`internal_error` 家族，消息带回调名），绝不升级为批/运行失败；
- **启动校验**：M1 import 回调并对 few-shot 示例的 output 干跑一遍（示例过不了用户自己的回调显然是配置错误，快速失败）；
- **信任边界**：执行任意用户代码——与运行 LabelKit 本身同权限、同信任级（配置文件也是用户写的），文档明示即可；不引入新依赖（importlib 标准库），无状态/隐私约束不受影响。

**方案 B：子进程钩子**（`validator_cmd = ["python", "check.py"]`，stdin JSON → 违规 JSON/退出码）。语言无关、故障隔离更强；但每记录一进程的开销可观（50 万条不可行，需再造批处理协议），进程管理引入新错误面，与「单机单进程、无框架」的工具气质相悖。适合不信任回调代码的多租户平台——不是本工具的场景。

**方案 C：Schema 内嵌表达式 DSL**（JSONLogic/CEL 等）。表达力受限（外部词表、IO 类约束仍做不到），且违背 §1.6 已对齐的「标准 JSON Schema、零转换层」决策。不建议作为主路径。

### 推荐与工作量估算

推荐 **方案 A**，两个注册点分别落在 `[output].validator` 与 `[generate].sample_validator`。触点清单：

| 触点 | 内容 | 量级 |
|---|---|---|
| M1 loader | 两个键的解析、import 校验、few-shot 干跑 | 小 |
| M8 engine | L2.5 环节 + 违规并入修复提示词 + `resolved_at` 增桶 | 中 |
| M6 generate | 样本过滤 + 桶字段 | 小 |
| 错误码/trace | `callback_violation` + `schema.repair` payload 只增字段 | 小 |
| spec/CONTRACTS | 3.8.2 增 L2.5、5.2 两键、7.6 错误码、CONTRACTS §10 | 中 |
| 手册 | 第 14 章增「代码回调」节 + 第 12 章生成侧 + 附录 A | 中 |
| 测试 | loader/engine/generate 单测 + 集成 1 例 | 中 |

前置条件：这是接口级新特性，按项目惯例（§1.6）**先对齐、写入 spec，再实现**——本文档即对齐材料。若认可方案 A，建议连同「缺口 A（部分接受）」一起立项，两者都动 3.6.2/3.8.2，一次改完。
