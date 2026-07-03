# 第 13 章　校验算子 verify：独立审核与有界返工

> verify 是主输出前的最后一道人祸闸门：用**另一个** LLM 独立评审每条 (记录, 标注)，
> 不合格的要么淘汰、要么带着批评意见返工重标。
> 本章讲评审如何工作、drop 与 repair 怎么选、多评审团何时值得上。

## 13.1 直觉：为什么标注完了还要审

annotate + 结构引擎已经保证了输出**结构**合法，但结构合法 ≠ 内容正确：intent 可能标错类、UI 元素列表可能编造了截图里不存在的按钮、difficulty 可能整体偏乐观。这些**语义**错误只有「再看一遍」才能发现——而且看的人最好不是干活的人。

verify 就是抽检科：给定 **任务指令 + 原始数据 + 标注结果** 三样东西，独立回答「这个标注合格吗」。三条设计原则：

1. **独立性**：评审用单独的 profile（`verify.llm`，惯例叫 `judge`），**强烈建议与标注模型不同家族**——模型审自己的作业有自增强偏差（LLM-as-a-Judge 论文），LabelKit 在两者 model 相同时会打警告（不阻断）；
2. **先意见后结论**：评审输出强制「逐维度意见在前、verdict 在后」，让结论以意见为条件（链式思考评审），而不是拍个结论再找补；
3. **审改分离**：verify 自己**永远不改标注**。修复 = 把批评意见回喂给 annotate 重标 + 结构引擎重校 + verify 重审。审的人不动笔，动笔的人不定案。

## 13.2 评审调用长什么样

```
system: 你是标注质量审核员。给定任务指令、原始数据与标注结果，独立判断标注是否合格。
        评审维度: ① 是否遵循任务指令 ② 与原始数据的事实一致性 ③ 字段语义是否正确填写
        {verify.extra_criteria}                  ← 你追加的维度（可选）
        先逐维度给出简短意见，再给结论。
user:   [任务指令] {annotate.instruction}
        [原始数据] {记录内容；UI 模态含截图+序列化树}
        [标注结果] {标注对象的 JSON}

输出（经结构引擎校验）:
{"critiques": [{"aspect": "...", "opinion": "..."}], "verdict": "pass"|"fail"}
```

三个内置维度覆盖了标注错误的大盘：没按指令来、跟数据对不上、字段填的不是那个意思。`extra_criteria` 是自由文本，用来追加你的场景特有的红线，例如：

```toml
extra_criteria = "④ topic 是否为名词短语而非整句复述 ⑤ 涉及金额/日期的字段是否与原文逐字一致"
```

## 13.3 drop 还是 repair

| 策略 | 行为 | 适用 |
|---|---|---|
| `policy = "drop"`（默认） | fail ⇒ 记录置 `dropped_verify`，进拒绝通道（`stage="verify"`、`reason="verify_fail"`，`errors` 为空——判负不是错误）；批评意见全文只落在 trace 的 `verify.verdict` 事件（见 13.6） | 数据管够、要纯度：错了就扔，不给返工机会 |
| `policy = "repair"` | fail ⇒ 批评意见追加进标注提示词（`[上一版标注] … [审核意见] … 请修正后重新输出`）→ 重标 → 重校 → **重审**；最多 `max_repair_rounds`（默认 1）轮，仍 fail 按 drop 收尾 | 数据金贵（UI 采集、稀缺场景）：先给一次改错机会 |

repair 的完整轨迹示例（真实流程走查）：

1. 首审：`{"critiques": [{"aspect": "字段语义", "opinion": "difficulty 标为 easy，但该指令涉及正式文书格式与措辞得体性，应为 medium"}], "verdict": "fail"}`
2. 返工：annotate 收到上一版标注 + 上述意见，重新输出 `{"intent": "writing_assist", "topic": "请假条写作", "difficulty": "medium"}`
3. 复审：`verdict = "pass"` ⇒ 记录存活，`_meta.verification = {"verdict": "pass", "rounds": 2}`（rounds 含首审：一次过 = 1，返工一轮后过 = 2）。

**repair 是流水线上唯一会改写已产出标注的路径**——如果你的下游要求「标注一旦产生不可变更」，用 drop。

成本账：verify 本身每记录 1 次调用；每轮 repair 追加「1 次重标 + 1 次复审」。`max_repair_rounds` 默认 1 是刻意的——第一轮修不好的，第二轮大概率也在原地打转（Self-Refine 的停机设定），不如省下钱换个思路（改 instruction、换模型）。

## 13.4 多评审团

`verify.judges = ["judge_a", "judge_b", "judge_c"]`（奇数个）时启用评审团：

- 各评审用**同一模板独立**评审（互相看不见对方意见）；
- `verdict` 取多数票；
- 全部意见合并保留，每条批注来源评审（critiques 条目带 `judge` 字段）；
- repair 回喂时，[审核意见] 段 = **投 fail 的那些评审**的意见合并（各条前缀来源名）。

成本 ×|judges|。**配置心法**：3 个异构小模型好过 3 倍预算砸一个大模型——PoLL 论文证明跨家族评审团在多种评审设置上优于单一大评审，因为多样性稀释了单模型的口味偏差。这与「judge 独立于标注模型」是同一条去偏原则的推广。

## 13.5 配置参考

```toml
[verify]
enabled = false               # 默认关
llm = "judge"                 # 单评审 profile；enabled 且 judges 为空时须存在于 [llm.*]
judges = []                   # 评审团；非空须奇数个，替代 llm（此时 llm 键不参与也不校验）
policy = "drop"               # "drop" | "repair"
max_repair_rounds = 1         # repair 轮数上限
extra_criteria = ""           # 追加评审维度（自由文本）
```

约束回顾：`verify` 开 ⇒ `annotate` 必须开（第 4 章）；UI 模态下评审 profile 也要 `supports_vision = true`（它要看图核对）。

## 13.6 结果落在哪 & 何时开 verify

- `_meta.verification = {"verdict", "rounds"}`（未启用为 null）；
- 拒绝通道：`stage="verify"`；
- trace：`verify.verdict` 事件**每轮一条**（评审团下每评审一条，带 `judge` 字段），critiques 全文在 `content ≥ refs` 档可见——想知道评审都在挑什么刺，trace 里全有；
- 报告 `counts.dropped_verify`。

**何时开**：verify 让每条记录的成本增加约一半（1 次标注 + 1 次评审起步），换来的是对**语义正确性**的独立背书。建议的决策线：

- 标注结果直接进训练集/评测集、错标代价高 → 开，先 drop；
- 数据稀缺、扔一条心疼 → 开 repair；
- 还在调 instruction 的探索期 → 先不开，把 verify 的预算省下来多迭代几轮指令（此时 trace + 人工抽查是更高效的反馈回路）；
- 上了生产、instruction 已稳定 → 开，且让 `dropped_verify` 率成为你监控的核心指标：它突然升高 = 数据分布变了或模型退化了。
