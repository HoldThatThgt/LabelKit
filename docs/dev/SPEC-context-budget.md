# 特性开发规格：上下文预算与视觉能力自动推导（spec v1.11 候选）

> **状态：定稿（2026-07-22，三方预实施审计闭合）——A1–A5 按推荐执行，A6 被
> V17–V21 取代（测量-反应式范式，需求方自提），A7 按推荐 (b) 闭环；三方
> 审计（代码可行性/亲和性 × 文档清单 × deep-search refute/elevate，v1.10
> U19–U27 同款工序）产出增补裁决 **V22–V27** 与引用 [C-57]–[C-84]，全部
> 折入本文；V1–V27 无待裁决项，本文即进入 spec/*.md 与 CONTRACTS.md 正式
> 修订的最终开发规格（§3.8 清单，实施工序与验证门见 §3.9 整体待办）**。
> 图片成本形态 = 默认采样装填 → 溢出裁帧保清重试 → 判审低置信裁帧升清
> 重试 → usage 在线校准（跨界调研 [C-29]–[C-56] 背书）。依据七路并行侦察
> 收敛：①–④
> use_vision 全景、13 调用点普查、M9/M1 基础设施、业界 deep-search
> [C-1]–[C-28]；⑤⑥（2026-07-22 增补，需求方指令"以动态装填为意向方案做深度
> 排查"）estimate/console 链路语义深查 + 可变窗影响面全量清查——**V9/V12 已依
> 排查证据改写为动态装填形态**（初稿的静态钳制形态废弃）；⑦–⑨（2026-07-22
> 晚，三方预实施审计）：代码侧 15 项机制发现（F1–F15，全部折入 V 行与
> §3.2/§3.3/§3.5）、文档清单 28 项增补（折入 §3.8）、外部证据 4 项强制修订
> （[C-57] 双协议 `model_context_window_exceeded`、[C-59] z.ai 端点 1M 须
> `[1m]` 后缀、[C-60] openai 图片先验按纵横比、[C-75] V20 pattern 实证种子）。
> 调研记录与动机全文见姊妹文档 `PROPOSAL-context-budget.md`。决策编号
> **V1–V27**（与 v1.8 的 S、v1.9 的 T、v1.10 的 U 区隔）。spec §1.6 增补
> 2026-07-22 决策行。

## 1. 结论与形态

**不变式（本特性的唯一目的）**：对每一次 LLM 调用，
`est(输入 prompt) + max_output_tokens + margin ≤ context_window`
——提供的信息与模型回复必须同时塞进模型上下文窗口。业界共识形态
（LlamaIndex `context_window − prompt − num_output` [C-5]、Claude Code
`contextWindow − min(maxOut,20k) − 13k` [C-15]、OpenRouter "输入+补全"合并判定
[C-17]）。

实现分五件事：

1. `[llm.<name>]` / `[embedding.<name>]` 新增 **`context_window`** 声明
   （0=未声明=该 profile 预算关闭，v1.10 行为原样）；
2. 零依赖启发式估算器 + **动态装填**（条数型参数降级为**上限值**，按实际
   内容逐项装填；调用次数以静态最坏值 `w_min` 保证**上界**，供估算与护栏）；
3. **图片成本测量-反应式三层**（V17–V21）：默认采样装填（`default_image_px`
   工作点）→ 溢出裁帧保清重试 → 判审低置信裁帧升清重试；`usage.prompt_tokens`
   运行内在线校准每图成本——provider 文档公式降级为首批先验；
4. M9 咽喉终检，超限抛记录级 `context_overflow`（**不喂熔断器**）；
5. 删除 `segment.use_vision`，改为派生 `vision_resolved`（能力推导）。

**默认字节等价声明**：`context_window` 全体未声明 ∧ 配置中无 `use_vision` 键
∧（非 UI 模态 ∨ segment.llm 无视觉能力）时，工具行为与 v1.10 逐字节一致。
无法开关的行为变化恰两项：① UI 模态 + segment.llm 有视觉能力的存量部署，
segment 窗口调用从纯文本翻转为多图（V1 的固有后果，迁移由 V2 定向报错兜住
显式配置者）；② 截断/超窗响应终局化（V11/V24）——`finish_reason=length`、
`stop_reason=max_tokens`、双协议 `model_context_window_exceeded`（[C-57]
[C-58]）不再流入 L1–L3 修复循环，改记录级 reject（旧行为是把截断 JSON 交给
修复循环"硬修"，可能修出语义残缺的合法输出——这是数据质量隐患，终局化是
正确性修复，不设开关）。V20 的 400 错误体嗅探则**按 profile 预算开关门控**
（`context_window == 0` 时嗅探不启用，400 走 v1.10 原路），不破坏等价声明。

### 1.1 裁决清单（A1–A6，逐项前因后果）

**裁决状态（2026-07-22 需求方）：A1–A5 按本文推荐执行；A6 被 V17–V21 取代
（范式为需求方自提，视同已裁）；A7 按推荐 (b) 闭环——全部裁决点闭合**。
索引：A1 移除键处置（V2）｜ A2 常数冻结（V7/V8）｜ A3 examples 翻转
（V1 后果）｜ A4 未声明窗口的残余违约（V6 衍生）｜ A5 预检 WARN 阈值
（V13③）｜ A6 Anthropic 图片档位（V8，已被取代）｜ A7 反应态溢出终态 ×
熔断（V20）。

**A1（V2）存量 `use_vision` 键：定向报错还是静默忽略。**
前因：v1.11 删除该键后，存量 project.toml 里可能残留它；loader 对未知键的
现行机制是前向兼容 WARN（loader.py:283-287），什么都不做就走这条路。关键
人群是今天**显式写 `use_vision = false` 控成本**的用户：升级后若只 WARN，
键被忽略 → 自动推导生效（profile `supports_vision=true`）→ segment 每窗附
最多 window 张图——**行为与账单静默改变**，批处理场景一条 WARN 极易被淹没，
事后也难归因到"一个被忽略的键"。
选项与后果：(a) 定向 CONFIG_ERROR（exit 2），文案说明键已移除、附图改由
profile 能力决定、要纯文本请把 `segment.llm` 指向纯文本 profile——用户删键
才能继续跑，**删除动作即知情确认**；符合 M1 fail-fast 一次报全哲学；代价 =
存量配置升级后首跑必失败一次。(b) 沿用未知键 WARN——零摩擦；代价 = 显式
false 用户被静默翻转，是真金成本事故。
推荐 (a)：一次性的启动失败远比静默的账单变化便宜。
裁决（2026-07-22 需求方）：**按推荐执行 (a)**。

**A2（V7/V8）margin 与估算系数：冻结常数还是开配置面。**
前因：估算是启发式的，margin 要吃三类误差——①字符→token 比率随词表差一倍
（中文：GLM 0.67 t/字、o200k 0.8–1.0、cl100k 1.25–1.4 [C-25]）；②消息封装
与结构化输出 schema 注入的请求侧开销；③厂商图片规则版本漂移（Anthropic
(w×h)/750 已被 28px patch 制替代 [C-11]、Gemini 计数 API 与计费出过 9 倍
出入 [C-12]）。裁决点 = 这些数字是 spec 常数还是用户旋钮。
选项与后果：(a) 冻结——依据是调研的明确负结果：**业界不存在权威保守系数**
（LlamaIndex padding=5、Claude Code 13k、OpenAI ≥25k、Anthropic 75% 触发，
量级从个位到万位各自场景化 [C-5][C-14][C-15][C-16]）；开旋钮等于把"须理解
估算器内部才能调对"的参数丢给用户，且调错后果不对称——**调小会超窗 400
（预算本要防的事故），调大只浪费窗口**；仓库同类机制常数先例（p50 deque
256、L3 修复预算）皆为 spec 常数；用户逃生门 = 把 `context_window` 声明得
更小，语义人人能懂、效果等价加大 margin。代价 = 特殊场景（如纯 ASCII 数据
配 GLM，估算偏保守 ~30% 浪费窗口）无微调手段，只能等 roadmap 的
per-profile 密度旋钮；后果仅是多切几刀，不产生错误。(b) 开配置面——灵活；
代价 = 配置/校验/文档/测试面各 +N，且用户误设小 margin 会复现超窗事故，
机制自噬。
推荐 (a)。
裁决（2026-07-22 需求方）：**按推荐执行 (a)**。

**A3（V1 固有后果）examples/stream 翻转多图 + 手册重采。**
前因链：`examples/config.toml` 两 profile 均 `supports_vision=true`（这不是
冗余——`examples/ui` 的 classify/annotate/verify 在 UI 模态下被 M1 强制要求）
→ `examples/stream/project.toml` 未设 use_vision（默认 false，现状 5 次窗口
调用纯文本）→ v1.11 推导 `vision_resolved = true` → **同一份配置**翻转为每窗
最多 16 帧截图的多图调用。三层后果：①成本时长——53 帧截图全过 LLM，示例
真跑与集成测试真金成本上升；②判决可能漂移——prompt 证据变了，episode
切分/噪声标记可能与纯文本版不同，下游 stitch/dedup/classify/守恒计数随动
（temperature-0 只保证同输入可复现，输入变了不在保证内）；③文档义务——
仓库规则"手册样例输出来自真跑" → 25/26 章账目（估算 98/实跑 135 次、
~345 秒、边界判决实录）全部重采重写。
为什么"固有"：需求正是"配了视觉模型就自动用视觉"，examples 恰是这样的
部署。规避备选 = 给 examples 增纯文本 profile 并把 segment.llm 指过去——
但示例从此不演示 segment 视觉形态，且与"自动开启"叙事相悖。
推荐照单接受：重采列入验收步骤（§3.7），示例顺势成为新机制展示件。
裁决（2026-07-22 需求方）：**按推荐执行（照单接受）**。

**A4（V6 衍生）未声明窗口的部署：熔断违约只修了一半。**
前因：`context_window` 是声明制（0 = 未声明 = 预算关），因为强制声明会
breaking 全部存量 config.toml，且未声明时工具不知道窗口大小、调用前预检
无从谈起。而 V16 的记录级隔离修复（overflow 不喂熔断）依赖预检 → **未声明
的部署里，超大记录仍走"400 → ProviderFatalError → 熔断计数 → 连续 20 条
exit 4"的老路**（llm_client.py:1043-1053 + obslog.py:473-482）——普查发现的
约束违约在这类部署只修了一半。
选项与后果：(a) 接受——预算语义聚焦"声明了就保证"；手册 troubleshooting
增补"遇 400 熔断先声明 context_window"指引；迁移零摩擦。(b) 错误体嗅探
兜底——解析 provider 400 响应体中 "context_length_exceeded" 等字样，命中
则归 `context_overflow` 不喂熔断。后果：openai_compatible 生态的错误体无
标准（Azure/vLLM/各网关格式互异），字符串匹配脆弱——**漏判**回到老路（只是
概率性缓解），**误判**更危险：把真 provider 故障当数据问题吞掉，熔断器
失灵、fail-fast 保护被架空。
推荐 (a)；嗅探列演进（若日后按 [C-13] tokenizer API 做校准回路，可顺带
收集真实错误体样本再评估）。
裁决（2026-07-22 需求方）：**按推荐执行 (a)**。

**A5（V13③）静态系统侧预检的 WARN 阈值。**
前因：系统侧静态部件（模板 + instruction + rubric/类表/schema/few-shot）是
用户语义资产，不参与动态裁剪（裁了等于改任务定义），但它们吃掉预算后留给
记录内容的空间可能过小，需要启动期预警。`est ≥ input_budget → CONFIG_ERROR`
是数学必然（任何记录都装不下、必错无疑）；**WARN 阈值 > 50% 是工程判断**：
系统侧过半意味着单记录可用空间减半，quality pairwise 双记录对分后每侧只剩
四分之一，最先出现质量退化。无外部权威依据。
选项与后果：50%（预警灵敏，可能对长 instruction 工程产生常态化告警噪声）/
75%（安静，预警晚）/ 不设 WARN 只保 ERROR（零噪声，用户在接近极限时无感）。
推荐 50%——告警是一次性启动行、成本低，预警早于质量退化值得少量噪声。
裁决（2026-07-22 需求方）：**按推荐执行（50%）**。

**A6（V8）Anthropic 图片估算的档位取向（展开详析，2026-07-22；待批）。**

*机制前因*：Anthropic 现行图片计费 = 28px patch 制 `⌈w/28⌉×⌈h/28⌉`，带
**档位封顶**（超限由 provider 侧降采样到封顶为止）：标准档长边 ≤1568px 且
≤**1568** visual tokens；高清档（Opus 4.7/4.8 等）2576px / ≤**4784**
[C-11]。档位是**模型属性**，请求侧无从声明，配置也无从得知。本方案的
`est_image` 在 `max_image_px`（默认 2048）最坏正方形下静态求值：2048px 图
在两档下的真实上限分别是 1568 与 4784——**同一常数必在其一侧出错，单图
gap = 3216 token**。

*为什么动态装填放大了这个 gap*：装填器的职责就是把 est 顶到预算线附近
（最大化利用），"est 贴线"是**常态而非尾部事件**——因此任何**系统性低估**
不再被"est 通常远离线"稀释，而是直接转化为超窗概率。量化：高清档 +
annotate 20 关键帧 → 低估上限 20×3216 = 64,320 token，远大于 200k 窗的
margin（20,000）；且**穿透后果走老路**——预检已通过，provider 返回 400 →
现行分类 `ProviderFatalError` → 熔断计数（V16 只救预检拦下的超窗，救不了
估算穿透的）。复合因素：Claude 系 tokenizer 的中文密度无实测锚点（cl100k
族 1.25–1.4 t/字暗示 CJK×1.0 可能同向低估 [C-25]）——文本与图片两个低估
同向叠加，anthropic 协议 + 中文 + 多图是最不利组合。

*一个修正视角*：本仓的 anthropic provider 首先是**协议**——主力端点
`api.z.ai/api/anthropic` 跑的是 GLM 而非 Claude（CLAUDE.md 集成基线），GLM
图片规则无公开公式（[C-13]）。所以 est_image 的 provider 常数本质是
**"协议旗舰实现的启发式"**，对第三方网关从来不是真值；档位表按模型名嗅探
（如 "opus-4.7" → 高清档）对网关自定义模型名（glm-5.2）必然错判，直接否决。

*选项与后果*：
(a) **默认常数 1568 + 文档化两条零代码逃生门**：①高清档用户把
`max_image_px` 调至 ≤1100（39²=1521 ≤ 1568，est 重新成为**真上界**，gap
被精确中和，且附带传输/成本收益，代价 = 分辨率）；②`context_window` 打折
声明（通用 margin 放大器）。标准档与小窗网关零损失；高清档多图工作负载
须按手册操作，**不操作则有穿透风险**（依赖用户知情）。
(a′) **客户端信封强制（"声明式压缩"的客户端实现；2026-07-22 需求方质询
"为什么客户端做不了"后修正补入）**：档位封顶只对**超限图**生效——把图在
发送前缩进标准档信封（`长边 ≤ 1568px ∧ ⌈w/28⌉×⌈h/28⌉ ≤ 1568`），则两档
收费**完全一致**（同一 patch 公式、无 provider 侧再降采样），档位不可知
问题整个消失，`est = 1568` 由机制保证为真上界而非依赖用户操作。实现落点：
`ImageRef.load_base64` 在载入时**本来就已打开图片、已知 (w,h)**
（types.py:48-49），在既有 max_px 长边缩放旁增加 patch 信封缩放即可——
逐图精确、零额外 I/O、零新配置键；M9 builder 按 provider 传入信封
（anthropic：patch 制；openai_compatible：既有 tile 归一化本身即信封，
est 765 已对齐，不变）。对标准档用户是**纯收益**：超出信封的像素 provider
本来就会降采样扔掉，先缩再传省带宽与延迟（Anthropic 官方推荐的客户端预
缩放实践 [C-11]）；对高清档用户 = 保真度被封在标准档水平——这才是 (c) 键
的真正位置：**放开信封**（声明 4784 → 信封放宽到高清档），而非修记账缺口。
(b) **一律取 4784**：200k+ 窗上近乎免费（segment w_min 与 annotate k 先被
各自上限绑定，实测差异极小），高清档穿透风险归零；代价 = anthropic 协议
小窗部署（如第三方小模型网关）的 w_min 从 ~6 退化到 ~2，触发窗数放大
WARN——为不用高清档的人付 3 倍预留。
(c) **per-profile `image_token_cap` 键（默认 1568）**：档位是模型能力事实，
与 `context_window`/`supports_vision` 同族归 profile 声明——在 (a′) 下语义
从"修估算缺口"转为"放宽信封上限"；代价 = 配置键 + 校验 + CONTRACTS/手册面
各 +1，且用户须知道自己模型的档位。**不取代 `max_image_px`**（需求方
2026-07-22 质询"能否只留一个键"的裁定）：两键分层——像素键 = 运载意图与
provider 硬限制域（带宽/载荷/内存；Anthropic 的 8000px 与 >20 图 ∧ >2000px
硬拒本身是像素制 [C-11]/S28；openai_compatible 与 GLM 无可靠 token→像素
映射，像素是该侧唯一诚实控制面），token 键 = 计费能力域（仅 anthropic
patch 制可反解）；生效信封 = 两者取小。删既有 max_image_px = V2 式迁移
成本换零能力收益，不做。
(d) 模型名嗅探——魔法字符串、跨网关必错，**否决**。

*分层原则（业界共识，2026-07-22 补记）*：max_image_px 与 image_token_cap
不是同一层的两个替代品——前者是**控制层**旋钮（改变现实：压缩载荷、改变
模型所见与实际计费，表达"愿意送多少像素"的质量/成本意图），后者是**记账层**
声明（描述现实：不改载荷，只让装填上界为真，表达"最多记多少账"的能力
事实）。业界形态：预算与上限在记账层声明与执行（LiteLLM 注册表
max_input_tokens [C-1]、Anthropic count_tokens [C-10]、vLLM
max_num_batched_tokens [C-22]、本方案的 context_window 本身），内容适配在
控制层做且**由预算反推**（Qwen 官方 `max_pixels = 预算//帧数` [C-19]；
OpenAI `detail: low` 是"声明式压缩"合并两层、换 85 token 固定计费 [C-9]；
Anthropic 官方建议客户端预缩放避免超限图的降采样延迟 [C-11]）。原则 =
**正确性归记账层，质量取舍归控制层**；(a) 用控制旋钮修记账缺口是 v1 权宜
（以质量损失补知识缺口），(c) 是方向正确的记账层修复——故列首位演进而非
弃案。另注：读真实像素（probe_size 演进项）解不了本问题——像素是图片属性、
档位是模型属性，档位缺口的终点只能是声明键或厂商计数 API 校准 [C-13]。

*推荐（2026-07-22 修正）*：v1 取 **(a′)**——客户端信封强制，est=1568 成为
机制保证的真上界，穿透风险归零、无须用户知情操作，且对标准档是纯收益；
(a) 的两条逃生门降级为补充文档（对不想被信封约束的场景仍有效）；(c) 列
首位演进、语义改为"放宽信封"（高清档保真需求真实出现时启用）；(b) 不取；
(d) 否决。附带修正一处论证：此前"声明式压缩需 provider 支持"的说法**不
成立**——provider 专属的只有内容无关的一口价（OpenAI detail:low 的 85
固定 [C-9]），而预算装填需要的"确定性 + 可计算 + 上界为真"客户端即可
自足（(a′)）；openai 协议侧另有真·`detail` 请求参数可传，但网关是否尊重
不可知（z.ai GLM），列演进不依赖。
**裁决（2026-07-22 晚）：本项整体被 V17–V21 取代**——需求方否决更深一层
的前提："方案过度依赖 provider 文档完善度，客户端不应如此依赖"。(a′)
信封与 (c) 放宽键退出正确性路径（文档公式降级为首批先验），图片成本改由
**测量-反应式**三层机制承担（默认采样装填 → 溢出裁帧保清重试 → 判审低
置信裁帧升清重试 → usage 在线校准）。本块保留作决策演化记录。

**A7（V20）反应态溢出降级耗尽后的终态：是否计入熔断连击（待批）。**

*前因*：熔断器语义 = 连续不可恢复的 provider 交互失败达 `fatal_error_threshold`
即停机保护（`obslog._fatal_streak`）。V16 已裁定**预检态** `ContextOverflowError`
不计连击——理由是预检发生在任何 provider 交互**之前**，属客户端决策，与
provider 健康无关。V20 引入反应路径后出现第三种形态：请求真实发出 →
provider 返回 400 → 错误体匹配溢出 pattern → 降级重试（≤2 次）仍失败。
记录按 `context_overflow` 入 rejects 已定（V10）；**未定的是该终态事件是否
计入熔断连击**——本文初稿在 V20 行写"最终失败照常计 fatal"、在 §3.5 表写
"不计熔断"，两处矛盾，须显式裁决。
*张力*：**不计（a）**= 记录级隔离最大化；但嗅探是启发式——若某种系统性
provider 故障的错误体恰好匹配溢出 pattern（或网关把各类 4xx 套同一 body
模板），全批记录会逐条"降级两次 → reject"而熔断永不触发，run 蠕行到底、
产出接近全空——这正是 A4 拒绝嗅探时担心的"熔断被架空"在窄门重现，且
浪费无界。**计入（b）**= 熔断保护完整；偶发的估算穿透（预检 + 校准的
漏网）是孤立事件，成功降级或任何成功调用都会清零连击，不会误触发；只有
"连续 20 条记录都无法靠降级挽救"才停机——这种系统性失配（估算体系 vs
provider 现实全面脱节）本就值得 run 级停机排查，不属于"单记录失败殃及
全局"的隔离违约。误判的最坏代价有界：每记录多花 ≤2 次重试后照常熔断。
*与主目标的相容性*：记录级隔离的**主修复**在预检 + 校准（V16/V19）——
常规超大记录被拦在 provider 交互之前、不产生连击；(b) 只作用于三层防御
全部漏网的残余，不削弱主修复。
*推荐 (b)：计入连击*。①熔断度量 provider 交互健康，反应态有真实交互；
②(b) 误判代价有界（多花重试），(a) 误判代价无界（熔断架空 + 全 run 空转）；
③与 V20"嗅探只是优化门、不构成熔断豁免面"的定位自洽。
裁决（2026-07-22 需求方）：**按推荐执行 (b)——反应态终局计入熔断连击**。

## 2. 设计裁决记录（V1–V16）

| # | 裁决 | 依据与要点 |
|---|---|---|
| V1 | **`segment.use_vision` 删除，改为能力推导**：`vision_resolved = (modality=="ui") ∧ segment.enabled ∧ strategy∈{llm,hybrid} ∧ llm_profiles[segment.llm].supports_vision`，M1 于 load() 收尾以 `dataclasses.replace` 冻结进 SegmentConfig（parse product，`mode_resolved` 先例 loader.py:1903-1952）。运行期 `build_segment_prompt` 改读 `seg.vision_resolved`（segment.py:138-139 唯一生效点）；verify 回收复裁面（verify.py:1046-1052）经同路径自动继承。省钱形态的表达面 = **选 profile 即选能力**（segment.llm 指向纯文本 profile），spec/manual 明示为预期用法。历史考据：S12/S30 只记录默认关的理由，"开关 vs 推导"从未被权衡——不与任何已记录决策冲突。业界先例 [C-27] LiteLLM `supports_vision(model)` 注册表旗标驱动模态路由。 |
| V2 | **移除键定向报错**：`[segment]` 内显式出现 `use_vision` → CONFIG_ERROR（文案含迁移指引），**不走**"未知键忽略"前向兼容警告。理由：显式 `use_vision=false` 控成本的存量配置若静默翻成多图是真金成本事故；fail-fast 哲学（M1 一次性报全）。 |
| V3 | **vision 校验集删 segment 分支**（loader.py:1379-1380 唯一逻辑依赖点）：segment 从"要求视觉"变为"适配视觉"，该校验命题失去可失败性。存在性/密钥/probe 三处引用集（只看 enabled ∧ strategy）不变。报错文案 stages 集合中 "segment" 不再可能出现。 |
| V4 | **贫瘠护栏指引改写**：`_DIGEST_POOR_WARNING`（segment.py:88-90）、`digest_is_poor` docstring（types.py:311）及手册指引从"开启 segment.use_vision"改为"为 segment.llm 配置 supports_vision=true 的 profile"。 |
| V5 | **S28 姊妹静态 WARN**：`vision_resolved ∧ segment.window > 20 ∧ profile.max_image_px > 2000` → WARN（Anthropic ">20 图 + >2000px" 400 硬拒域，S28 现只盖 annotate.sequence_frames，loader.py:1795-1805）。默认 window=20 恰在边界内侧，不触发。 |
| V6 | **窗口声明制**：`context_window: int = 0`，`0` = 未声明 → 该 profile 预算整体关闭 + 被启用阶段引用时 M1 一次性 WARN（含建议值指引）。不内置模型注册表、不做运行时 API 探测——注册表会脏（LiteLLM legacy 字段漂移 [C-1]）、探测是网关特权（[C-28]）；用户声明是单机工具的最稳事实源（aider `.aider.model.metadata.json` 先例 [C-3]）。声明后 `context_window ≤ max_output_tokens + margin` → CONFIG_ERROR（预算非正）。 |
| V7 | **预算公式与 margin 常数**：`margin = max(256, ceil(0.10 × context_window))`；`input_budget = context_window − max_output_tokens − margin`。10% 与 Claude Code 13k/128k 量级吻合 [C-15]，floor 256 保护小窗模型；业界无统一保守系数（调研未决①），故**常数冻结于代码、不开配置面**；用户逃生门 = 声明更小的 context_window。margin 承担：估算残差 + 消息封装 + provider 侧计数偏差（[C-12] 版本性出入警示）。 |
| V8 | **零依赖估算器**：`est_text(s) = ceil(ascii/3 + cjk×1.0 + other/2)`。ASCII 取 /3 非 /4 = 对 JSON 膨胀（≈2× TSV [C-26]）的保守化；CJK×1.0 覆盖 GLM 0.67 / o200k 0.8–1.0 / Qwen·DeepSeek 0.77–0.9（t/字，[C-25]；o200k 生僻字文本贴界 [C-73]）；已知局限：cl100k 旧词表中文 1.25–1.4 t/字**不被覆盖**，spec 记载 + 逃生门同 V7（per-profile 密度旋钮列 roadmap）。图片 = **先验种子 + 在线校准**（V17–V19）：先验取厂商公式于**生效工作点 px 的最坏纵横比**求值（anthropic patch 制 `min(⌈w/28⌉×⌈h/28⌉, 1568)` 最坏正方形 [C-47][C-69]；openai_compatible tile 制按 2048→短边 768 归一化后的**最坏纵横比**求值——@2048 长边竖屏 = 85+8×170 = **1445**，正方形 765 是特例、UI 截图纵横比下系统性低估 36%（[C-60] 审计强制修订））× 1.2 保守放大，仅作首批装填；第 2 批起读校准器（usage 反推，V19）——公式准确度只影响首批效率，不影响正确性。GLM 无官方闭式（官方 FAQ 有 GLM-4V 系"约 1047 t/图"孤值 [C-72]，仅作 PROPOSAL 参考记载，不进代码——先验按 provider 键控，不做模型名嗅探）→ 由校准接管。消息封装 +4 t/消息（[C-7][C-76] 的 3+1 保守化）；结构化输出 schema 文本计入 est（它随请求发送）。不引 tiktoken：对主力 GLM 不给真值（[C-7][C-13]），白名单扩项无从论证。**认识论定位（需求方 2026-07-22 质询后补记）**：换算真值在 provider 侧（公式会换代——(w×h)/750 → 28px patch [C-11]；计数 API 与计费出过版本性偏差 [C-12]；anthropic 协议跑 GLM 时 Claude 公式不适用），客户端算的是**公开规格的镜像**；因此 est 的语义自始是**预留上界而非精确记账**。**v2 修订（V17 后）**：图片估算进一步降级为**首批先验**——正确性改由 V19 在线校准（usage 反推实际成本）+ V20 溢出反应承担，文档公式只影响首批装填效率；文本估算维持启发式常数不变（margin 承担其残差）。 |
| V9 | **动态装填，静态最坏值只作护栏与估算上界**（需求方 2026-07-22 裁决，替换初稿静态钳制）：segment 改为"先算全会话逐帧 digest → 按预算贪心切窗"——窗从帧 s 起（首窗 s=0，后续 s=前窗末帧，**保留重叠 1 帧与接缝后窗整帧覆盖语义**，segment.py:300-305 契约不变），装填条件 `est_static_system + Σ c_i ≤ input_budget ∧ 窗内帧数 ≤ window`（`c_i = est_text(digest_i) + DIFF_MAX_TOKENS + est_image(若 vision_resolved)`；diff 在切窗后才算、输出结构有界故用最坏常数），溢出即封窗；`window` 降级为纯上限，预算未声明时逐字节退化为现行固定窗。**可复现性经审计不破坏**：frame_digest/tree_diff 是记录内容纯函数（types.py:246-389，"pure multiset arithmetic"）、segment 零 rng（segment.py:260、spec/314:128）——装填结果仍是 (输入, 配置) 的确定函数；变化的是"窗口边界依赖记录内容"这一性质。digest 计算前移到切窗前、每会话一次（现状在切窗后逐窗现算：接缝帧已双算、贫瘠护栏第三算且写死 400——前移是净改善；贫瘠护栏路径独立保持不动）；`build_segment_prompt` 冻结签名增 `digests` 形参（CONTRACTS:2994 修订）；**`judge_window` 公开签名不动**（verify 复裁面 verify.py:1046-1052 直调、自带 ≤3 帧表内部自算 digest，零影响）。内容级动态裁剪同初稿：树渲染动态上限、序列步骤行"首末恒保留丢中段"（classify.py:92-108 家族语义，middle-out 同向 [C-17]）、annotate 关键帧收缩、generate 种子尾丢、embed 截断。一线对标：Qwen-VL 帧数上限 × 总 token 预算双约束 [C-19]、UI-TARS 32k 预算保 N 帧 [C-20]、NeMo Curator token 装填 [C-23]、LlamaIndex repack [C-5]。**护栏**：M1 静态最坏检查 `w_min < floor` → CONFIG_ERROR，`floor = 3 if (verify.enabled ∧ verify.policy=="repair" ∧ segment.enabled) else 2`（F14；保证任意帧都放得进 floor 帧窗与 verify 三帧复裁窗，运行期装填与复裁永不失败——影响面清查证实现状无任何此类拦截）；`w_min = ⌊(input_budget − est_static_system)/per_frame_max⌋` 随启动 INFO 打印；`w_min == floor` → WARN（退化警示：每帧皆接缝、逐帧双裁决，200 帧会话至多 199 窗 ≈ 默认 20 窗形态的 18 倍调用量）。`est_static_system` 的模板头部件经 V22 冻结常数取得（跨层依赖免除）。 |
| V10 | **最小单元语义**：连语义最小单元（单记录 / pairwise 2 记录 / 2 帧窗 / 1 种子 / 2 关键帧）都装不下 → 该记录记 `StageError(kind="context_overflow")`、`status="failed"` 入 rejects，run 继续。**绝不发送注定失败的请求，绝不无限收缩破坏语义**。M4 pairwise 粒度适配（实现期钉板，2026-07-22）：2 记录对局装不下按既有「裁决失败」粒度折算 **tie**（错误与事件记 context_overflow、记录保持 active）——记录级隔离要求超大一侧不殃及配对另一侧；pointwise 单记录维持记录级 reject（spec 304 v1.11 行同步记载）。 |
| V11 | **输出截断显式化（审计扩订 [C-57][C-58]）**：响应终止原因按闭合映射处理，**不再把截断 JSON 送 L3 修复循环硬修**——① `finish_reason=length`（openai）/ `stop_reason="max_tokens"`（anthropic）→ `output_truncated`（输出触到 max_output_tokens 上限；记录级，不喂熔断——预算已为 max_output_tokens 预留完整空间，模型自然写满属输出侧事件）；② **`model_context_window_exceeded`（双协议现行值：anthropic 4.5+ 系 stop_reason [C-57]；z.ai openai 协议 finish_reason [C-58]）→ `context_overflow` 反应态**——input+max_tokens > cw 时新款后端不再 400 而是接受请求、生成触墙截断，此值即溢出 oracle 的 200 形态，进 V24 统一溢出信号（预算开启时可触发 V20 降级重试；HTTP 交互本身成功、streak 已被 ok 清零，不补喂熔断——与 400 嗅探态区别见 §3.5 矩阵）；③ z.ai 扩展值 `sensitive`/`network_error` 及其他未知值 → **v1 不做专项处置**，沿现行管线流转（内容进 M8 校验，垃圾输出自然走修复/拒收——记入 §4 非目标）。**显式拒绝厂商"加大 max_tokens 重试"建议（[C-61]）**：输入是按声明的 max_output_tokens 装填的，逐调用抬升输出上限即破坏 `est + max_output_tokens + margin ≤ cw` 不变式与确定性；正确的用户补救 = 提高配置的 `max_output_tokens`（手册 troubleshooting 记载）。依据 [C-14] OpenAI 官方处置建议与 Haystack 告警先例。 |
| V12 | **estimate_run 报上界，console 零改动**（深查裁定）：`segment_calls = Σ ceil((L−1)/(w_min−1))`，`w_min` 从 budget.py 导出、estimate 与 M1 护栏共用（单一事实源；预算关闭时 w_min=window，公式与现状同构、数值不变）。实际装填每窗帧数 ≥ w_min（实际 est ≤ 最坏值）⇒ 实际窗数 ≤ 估算——**上界**。**v2 注（V17 后）**：w_min 基于**先验**（配置可推导）；当 V19 校准值超先验或 V20 溢出分裂发生时，上界性不再严格——分母语义回落到既有"排除修复/重试"的近似家族（docstring 措辞同族），stream 注行相应措辞"segment 按先验装填报估"。该语义与既有分母契约同族，不是新例外：docstring 明文 "All estimates assume no drops (upper bound) and exclude retries/repairs"（orchestrator.py:151），stream 下游本就按 "episodes ≈ sessions, LOWER bound" 报下界并印固定 stderr 注（orchestrator.py:1106-1111），extract 亦是文档化上界（:158）；且**分子今天已可能越过分母**——M8 L3 修复每轮多发一条 llm.call（schema_engine.py:483-491，事件按逻辑调用粒度 llm_client.py:993-1004）、verify 修复轮全评审团重判 + 三修复面调用均落 verify 括号（verify.py:540/574-596/805-852）、classify multi 扇出（R28 注 orchestrator.py:1099-1105）、generate 按类 ceil 舍入（generate.py:495-512）。**console 渲染对双向失真天然容忍，零代码改动**：stage 行是裸文本 `name ▶ a/denom` 无百分比无进度条无钳制（console.py:890-906），完成态吸附为 `✓` 数字消失（:902-903），批级进度条钳 1.0（:880-881），ETA 按 records 维度与 segment_calls 无关（:859-864）。dry-run：stream 注行在 w_min < window 时增补一句「segment 按预算最坏装填报上界」；examples 无预算声明 ⇒ 五个黄金文件逐字节不动（强制面 tests/cli/test_console.py:597-617）。**运行中分母修正通道已证实存在但 v1 不用**（列演进）：`metrics.run_estimate` 可重复调用、渲染器 `on_estimate` 整体覆写 `_est` 并重绘（obslog.py:391-394 + console.py:306-321）；或 counters 通道 `ctx.metrics.count(...)` + 渲染器每 tick 拉取（console.py:797-798），新计数键不会泄漏进白名单拼装的 report（orchestrator.py:734-737）。 |
| V13 | **观测面最小增量**：① 启动期 INFO 打印预算参数（如 `segment: w_min=6 window=20 (budget)`，数据无关）；② `report.budget` 新节：`{profiles: {name: {context_window, input_budget}}, w_min: {"segment.window": [cap, w_min]}, truncations: {stage: n}, overflow_records: n}`——计数/统计 only，不含数据内容（§2.6）；③ M1 **静态系统侧预检**：每个启用阶段的静态 prompt 部件（模板+instruction+rubric/类表/schema/few-shot）est ≥ input_budget → CONFIG_ERROR（任何记录都装不下），> 50% → WARN；④ `report.stream.windows`（实际窗数，M14 属主，§6.4 增行）——供用户对账 V12 上界估算；⑤ **V17 三层的计数**（report.budget 增键）：`image_cost: {profile: 校准终值}`、`degrade_retries: n`（V20 降级重试次数）、`escalations: n`（V21 升级次数）——校准质量与反应频度可对账。无新 trace 通道（沿用 llm 通道的 usage 字段核对估算质量）。 |
| V14 | **batch_size 与 window 不合并**（勘察裁定）：全库无相乘/组合路径，无单 prompt 体积随 batch_size 增长的调用点（13 点逐一核验）。`run.batch_size` = 批调度参数（内存生命周期 orchestrator.py:399-413 + QuRating 池基数 quality.py:125-139 + stream 装箱容量 orchestrator.py:415-469）；`segment.window` = segment 唯一单次 prompt 容量参数（S32 "有界上下文"原旨）。处置 = spec §5 补语义句 + 手册误解防护，两参数各自保留。 |
| V15 | **embedding 预算**：`EmbeddingProfile.context_window`（0=关）同 V6；embed 输入按 `context_window − margin`（无输出预留）截断——修复 dedup 语义嵌入完全无截断（dedup.py:45-61 全树/200 棵树拼接）的普查发现④；截断 = 确定性头部保留（嵌入语义主体在前部）。既有 `embedding_failures` 跳过路径保留为兜底。 |
| V16 | **终检归属 M9**：`LLMClient.complete()` 于 provider 分派前执行不变式检查，超限抛 `ContextOverflowError(phase="precheck")`（新异常，`LabelKitError` 系；phase 字段见 V24）。归属理由：complete 是全部调用（含 M8 L3 修复调用与 probe——F13 审计修正：probe 经一次性子客户端同走 complete() 咽喉，max_output_tokens=1 + V6 正预算校验使其平凡通过，无须豁免工程）的唯一咽喉；**装填层正确时终检永不触发**——它是防御性不变式，不是第二套装填逻辑。`ContextOverflowError` / `output_truncated` **不进 `_record_provider_result(fatal=True)`**、不烧重试——修复"超窗 400 → ProviderFatalError → 熔断计数 → 连续 20 条 exit 4"（llm_client.py:1043-1053 + obslog.py:473-482）的记录级隔离违约（spec §2.6）。 |
| V17 | **图片成本走"测量-反应式"三层范式**（需求方 2026-07-22 方向裁决："方案不应依赖 provider 文档完善度"；取代 A6 全部形态）：①**先验装填**——V9 贪心装填不变，`est_image` 的来源从"文档公式镜像"降级为**首批先验**（公式仍可当先验种子，正确性不再依赖它）；②**溢出反应**——provider 超窗报错即 oracle，裁帧保清重试（V20）；③**置信升级**——verify 低置信触发裁帧升清重试（V21）；④**在线校准**——`usage.prompt_tokens` 反推每图实际成本（V19，基础设施四路下游已在）。范式对标：ABR 的 measure-don't-model（BBA"稳态不需要容量估计"[C-31]；BBR 测量式建模取代丢包反应 [C-54]）；OpenAI 图片计费 per-model 系数各异且随代际漂移（85+170/tile → 70+140/tile [C-46]）与 Anthropic 公式换代（/750 → 28px patch [C-47]）正是"文档不可依赖"的动机证据。dash.js DYNAMIC 双阶段（启动吞吐式 + 稳态缓冲式 [C-37]）背书"首批先验 + 稳态校准"的两段结构。 |
| V18 | **配置面与阶梯**：新增 `[llm.<name>].default_image_px: int = 0`——图片采样**默认工作点**（0 = 沿用 max_image_px，即 v1.10 行为逐字节不变）；`max_image_px` 语义不变，升格为**升级天花板 + provider 像素制硬限制域**（两键分层沿 A6(c) 裁定：default = 工作点，max = 上限）。阶梯为冻结常数（A2 哲学）：**几何间隔 ~1.5×/维**（Apple 相邻档 1.5–2× [C-29]；DLSS/FSR2 跨厂商一致 1.5/1.7/2.0/3.0×/维 [C-41][C-42]），格点对齐 28px（Qwen/Claude 的 token 原子 [C-43][C-47]）；降档下限 = 保底可读档（OCR 证据：x-height < 10px 不可识别 [C-51]），升档单步 ≤ 1.5×/维（FSR2 DRS 建议 [C-42]）。 |
| V19 | **在线校准器**（budget.py，运行内存、零持久化）：每 profile 维护每图实际成本估计——样本 = `(usage.prompt_tokens − est_text(该请求文本)) / 本请求图数`；滤波 = **窗口化最大值**（BBR windowed-max 形态 [C-54]，装填要保守取 max 而非均值）——**窗口单位 = 批（F8 审计修正）**：样本以 asyncio 完成序到达，按样本数开窗会让保留集依赖完成序、破坏 V19 自身的确定性护栏；故 `freeze_batch()` 聚合**本批样本的 max**（对无序集取 max，序无关）压入 `deque(maxlen=8)` 批最大值窗口，装填读数 = `max(deque) ÷ 0.85`（FESTIVE p=0.85 / dash.js 90% / PANDA ε=0.15 三方收敛 [C-32][C-37][C-33]）；先验（首批）= 文档公式常数 × 1.2 保守放大。**确定性护栏：校准快照按批冻结**——第 N 批装填只读 <N 批聚合值（批序串行 ⇒ 同输入同配置可复现；逐响应更新+逐调用读取会让内容依赖 asyncio 完成序，禁止）；样本 < 8 前不做主动升档（FESTIVE"样本不足不切换"[C-32]）。**usage 缺失兜底（[C-64] 实证：企业网关有 `usage: null`）**：响应无可用 usage → 不记样本，校准器停留先验 ×1.2，WARN 一次/profile（"image-cost calibration inactive"）。跨运行冷启动是 stateless 约束的固有代价（批处理 run 首批即收敛，摊薄可忽略）。 |
| V20 | **溢出反应路径（trim window, keep resolution）**：识别到 provider 上下文溢出（V24 统一信号：预算开启下 400 ∧ 错误体匹配 pattern 集，或双协议 `model_context_window_exceeded` 200 形态）→ **降级重试而非拒收**：segment 窗口**改切**（该窗对半分裂为两个子窗、维持重叠 1 帧与接缝归后窗，帧一个不丢——多花调用）；annotate 关键帧**减帧**（k 减半，min 2）；quality pairwise/单记录调用收紧文本份额重试一次。乘性减（AIMD [C-53]）、有界（每调用至多 2 次降级）；到最小单元仍溢出 → `context_overflow` reject（V10 不变）。**pattern 初始集（[C-75] 实证种子）**：OpenAI/Azure `code == "context_length_exceeded"` ∨ 消息含 `"maximum context length"`；vLLM 同消息族（type=BadRequestError、无该 code——只匹 code 会漏）；anthropic 协议 `invalid_request_error` ∧ 消息含 `"prompt is too long"`；z.ai 业务码 `"1261"` / 消息含 `"Prompt too long"`；OpenRouter `error_type == "context_length_exceeded"`。匹配在 M9 于**完整 resp.text** 上执行（先于 300 字符截断，F5）；**嗅探按 profile 预算门控**（`context_window == 0` 不启用——保 §1 字节等价）。**嗅探的地位与 A4 的本质区别**：这里嗅探只是"是否给降级机会"的**优化门**——未识别 → 走现行 fatal 老路（零回归），误识别 → 浪费 ≤2 次有界重试；**400 嗅探态**降级耗尽的终态计入熔断连击（**A7 已裁**，由属主算子经 `ctx.metrics.record_provider_result(fatal=True)` 恰好补喂一次——F5 职责分裂修正，M9 抛 `ContextOverflowError(phase="reactive")` 时不喂），故不构成 A4 所拒绝的熔断豁免面；**200 形态**（`model_context_window_exceeded`）HTTP 交互成功、streak 已被 ok 清零，终局**不补喂**（provider 健康无恙）。z.ai 端点错误体样本列入集成测试采集（[C-81] 未决项的闭合路径）。**P6 实测闭合（2026-07-22，E2E #15）**：z.ai anthropic 路由的 prompt-too-long **不走 400**——返回 200 体 `stop_reason:"model_context_window_exceeded"`（零计费、空 content），恰由 V24 的 200 形态承接（origin="finish"，不喂熔断）；pattern 集经实测零增补（该端点无 400 溢出形态可采），400 嗅探面对该端点为不可达防御层、对 openai_compatible 网关族保持有效。降档一触即发、升档要余量的不对称防抖是跨域共识（DOOM 15.15ms 降 / 14.5ms 升 [C-39]；UE 连续 2 帧超预算才紧急降 + 升档 0.9 摊销 [C-38]）。 |
| V21 | **判审升级路径（trim window, scale up resolution）**：`verify.policy="repair"` 的修复轮中，annotate 重标注按**质量阶梯换档**——关键帧数减半（k → max(2, ⌈k/2⌉)、首末恒保）、分辨率上探一档（default_image_px × 1.5ⁿ，≤ max_image_px），预算约束经校准估算复核后不变。触发信号 = **verify fail 判定，仅此一项（F4 审计修正：初稿并列的"annotate 自洽采样分歧 sc≥3"触发在 M5 主路径无宿主循环——annotate.py:400-402 只计数取样本 #1，无重标注环——且与"升级只发生在修复路径"的界定自相矛盾，删除；SC 分歧升级列演进）**。阶梯参数经 `annotate_record` 追加尾参传入（F3，CONTRACTS 冻结签名增订随 §3.8）；k 减半低于线索碎片数时 T14 逐碎片配额按既有文档化降级退化为均匀下采样（annotate.py:107-108），属可接受既有语义。**单向有界**：升级只发生在修复路径、每记录至多 `max_repair_rounds` 次（默认 1）——无全局振荡面，天然满足防抖（死区/最小间隔的连续系统问题在此退化为单步决策）。因果链背书：低分辨率直接致幻觉（LLaVA-NeXT 官方动机 [C-44]）；"低置信 → 定向升清重试"= V* 的置信度触发递归 zoom（GPT-4V 55% vs 7B+搜索 75% [C-49]）、OCR 低置信升 DPI 重扫惯例（300 → 400–600 dpi 封顶 [C-51]）、o3 思维链内建 zoom [C-50]、UI-Zoomer 置信门控变焦（GUI grounding +4.2–13.4% [C-66]）、AwaRes 判审触发裁剪升清 [C-67]；"缩略图保时序 + 高清关键帧保细节"同构于 InternVL thumbnail+tiles [C-45] 与 Ferret-UI 双子图 [C-52]。结构合法性：以 LLM 输出为条件的确定性形态（segment episodes / classify 扇出先例）。演进：定向**区域**升清（裁剪可疑区域而非整帧，Ferret-UI/VRS/MEGA-GUI foveation [C-52][C-56][C-67]）。 |
| V22 | **模板头冻结常数（F1，跨层依赖免除）**：V13③ 静态预检与 V9 护栏的 `est_static_system` 须计入各阶段冻结提示模板头，但模板头是算子层模块常数（如 segment.py:65-84 `_SYSTEM_HEAD`）而 budget.py/M1 属 common 层——common 禁 import operators。处置：budget.py 携带 per-stage `TEMPLATE_HEAD_TOKENS` 冻结整数常数（= est_text 在 CONTRACTS §10 冻结模板文本上的求值），离线测试跨层断言 `est_text(operator 模板常数) == budget 常数`（测试层允许双向 import；模板一旦修订测试即红，常数随 CONTRACTS 修订更新）。instruction/rubric/类表/schema/few-shot 均可从 ResolvedConfig 直取（审计核验），不受此限。 |
| V23 | **M9 载体面（F2/F7/F9，CONTRACTS §7.8 增订三处）**：① `PromptBundle` 增追加式字段 `image_px: int | None = None`——V21 升级档像素的唯一载体（builder 生效 px = `image_px or profile.default_image_px or profile.max_image_px`，再钳 `min(·, max_image_px)`）；px 必须随 bundle 而非算子可变状态——`build_body()` 每 attempt 重编码图片（llm_client.py:949-951），载体在 bundle 才保重试确定性。② `ImageCostCalibrator` 实例由 `LLMClient` 自持（构造器内建，零 factory 改动），公开面 `llm.calibrator`——M9 每响应喂样本、算子经 `ctx.llm.calibrator.cost(profile)` 读数、M10 批边界 `freeze_batch()`；RunContext 六字段冻结（stage.py:16-28 明文禁扩）不受触碰。③ `LLMResponse` 增追加式字段 `finish: str | None = None`（规范化终止原因：openai finish_reason / anthropic stop_reason 原值），供 V11/V24 判定；`_result_usage` 的 len==4 分派（llm_client.py:1097-1103）随元组形状调整同步改造。 |
| V24 | **溢出信号统一（F5 + [C-57][C-58][C-75]）**：`ContextOverflowError` 带 `phase: Literal["precheck","reactive"]`。precheck = V16 终检（零 provider 交互）与 V10 最小单元不装（由算子在装填处直接记录，无异常穿越）；reactive = ① 预算开启 ∧ 400 ∧ 完整错误体匹配 V20 pattern 集（M9 识别后抛 phase="reactive"，**不喂** `_record_provider_result(fatal=True)`），② 双协议 `model_context_window_exceeded`（200 形态，同抛 reactive，交互本身 ok 已清 streak）。熔断矩阵：precheck 不计连击；reactive-400 降级耗尽的终局由属主算子补喂恰一次（A7）；reactive-200 终局不补喂。降级机会仅在属主算子有降级机制且预算开启时消费（segment 窗改切 / annotate 减帧 / quality-pointwise 文本收紧）；无降级面的调用点（extract/复裁/probe/L3 修复）直接按 V10/V25 处置。 |
| V25 | **三处装填规则钉死（F10/F11/F12）**：① M8 L3 修复调用超预算 = **捕获 complete() 的 `ContextOverflowError`、该轮记失败**，且因修复 prompt 恒定、余轮必然同败——**短路至耗尽**，reject 归因维持既有 `schema_violation`/`callback_violation`（不新增 reject 值、不计 overflow_records；修复原文不截断——截断即破坏修复语义）。② verify 多评审共用单 prompt（verify.py:583-596/835-852 一次构建广播全团）——记录侧份额按**评审团最小 `input_budget`** 装填（quality pairwise 无此问题：逐 (对, 评审) 各自构建，按本评审预算装填）。③ 不可裁剪动态块显式闭合：verify 的 `[标注结果]` JSON（verify.py:343）与修复轮 `[上一版标注]/[审核意见]` 尾注（annotate.py:229-234）**计入 est、永不裁剪**（语义资产）；全部可裁份额耗尽仍超 → V10。 |
| V26 | **端点实效窗口声明教义（[C-59][C-81] 强制修订）**：`context_window` 声明的是**部署实效值而非厂商表值**——同名模型在不同部署差数倍（Together 256K；vLLM 由 `--max-model-len` 决定）。**P6 实测闭合（2026-07-22，E2E #16/#17）**：z.ai anthropic 路由裸 `glm-5.2` 实效窗 = `input_tokens + max_tokens ≤ 1,048,576`（2^20，12-token 夹逼实测）；`glm-5.2[1m]` 后缀在该端点反被 1211 Unknown Model 拒——[C-59] 的后缀条件不适用于本端点（教义本身经此反而更实：窗口只能实测、不能照抄文档）。examples 维持保守声明 131072（欠声明恒安全 + 黄金文件零扰动）。§3.1 表的指引句改写；examples/config.toml 两 profile 声明**保守下值 131072**（任何合理实效窗之下——欠声明恒安全，只多裁不溢出；实效窗按 P6 集成实测后可上调），P6 增补"实测未加后缀 glm-5.2 实效窗 + `[1m]` 行为"步骤。数值影响核算：131072 窗、4096 输出、margin 13108 → input_budget 113868；segment 最坏帧成本 ≈ 400(digest)+128(diff)+1882(图先验) ⇒ w_min ≈ 46 > window 默认 20 ⇒ 不钳、estimate 数值不变、五个 dry-run 黄金文件维持逐字节不动（V12 强制面成立）。 |
| V27 | **杂项审计钉板（F6/F15 + [C-62]）**：① 每个算子错误分类器（annotate.py:451-456、quality.py:211-220、verify.py:442、segment.py:317-318 及 classify/extract/generate/stitch 同款）增 `ContextOverflowError → StageError(kind="context_overflow")` 与 `OutputTruncatedError → kind="output_truncated"` 分支（共享 helper 落 budget.py 或各自内联——实现自便，词表必须精确，否则落 `internal_error` 破坏 §3.5 归因与 overflow_records 计数）。② V2 定向报错的实现机制 = loader 既有**原始节探针**先例（segment_provided，loader.py:743-746）：解析删除后于原始 `[segment]` dict 上探 `use_vision` 键存在性——不依赖未知键兜底路径。③ [C-62] 记载：gpt-5.6 级 openai 后端默认 `detail` 等效 `original`（服务端不再隐式钳制图片 token），`default_image_px`/`max_image_px` 因此成为该类后端唯一的客户端成本闸——写入 spec §5.1 键说明与手册。 |

## 3. 规格正文

### 3.1 配置面（spec §5.1 / §5.2 增量）

`[llm.<name>]`（§5.1 表增一行，`max_output_tokens` 行之后）：

| 键 | 类型 / 默认 | 说明与约束 |
|---|---|---|
| `context_window` | int / `0` | 模型上下文窗口（token）。`0` = 未声明：该 profile 上下文预算关闭（行为与 v1.10 一致），被启用阶段引用时 M1 WARN 一次。> 0 时须满足 `context_window > max_output_tokens + margin`，否则 CONFIG_ERROR（预算非正）；`margin = max(256, ceil(0.10 × context_window))`。**声明部署实效窗口，勿照抄厂商表（V26/[C-59]）**：同名模型随部署差数倍——z.ai anthropic 路由裸 `glm-5.2` 非 1M（1M 须模型名 `glm-5.2[1m]`）、Together 256K、vLLM 由 `--max-model-len` 决定；不确定时**欠声明恒安全**（只多裁不溢出）。 |

`[embedding.<name>]`（§5.1 表同型增行）：`context_window` int / `0`；预算 =
`context_window − margin`（无输出预留）。

`[llm.<name>]` 另增一行（V18）：

| 键 | 类型 / 默认 | 说明与约束 |
|---|---|---|
| `default_image_px` | int / `0` | 图片采样默认工作点（长边 px）。`0` = 沿用 `max_image_px`（v1.10 行为逐字节不变）。> 0 时须 ≤ `max_image_px`（CONFIG_ERROR）；V21 升级路径可上探至 `max_image_px`。 |

`[segment]`（§5.2）：**删 `use_vision` 行**；表尾注明 parse product：

| 键 | 类型 / 默认 | 说明与约束 |
|---|---|---|
| ~~`use_vision`~~ | —（v1.11 移除） | 显式出现 → CONFIG_ERROR：「`segment.use_vision` 已于 v1.11 移除：窗口是否附图由 `segment.llm` 所指 profile 的 `supports_vision` 自动决定；如需纯文本裁决，请将 segment.llm 指向纯文本 profile」。 |
| `vision_resolved` | bool（parse product，非用户键） | M1 于 load() 收尾冻结：`(modality=="ui") ∧ enabled ∧ strategy∈{llm,hybrid} ∧ profile.supports_vision`。 |

`run.batch_size` 行（§5.2）补语义句（V14）：「决定内存生命周期、QuRating
对比池基数与 stream 装箱容量；**从不影响单次 prompt 体积**——单次调用容量由
各算子条数上限与上下文预算（§3.9.x）共同决定」。

### 3.2 预算模块（新文件 `labelkit/common/runtime/budget.py`，CONTRACTS 新节）

```
# 全部纯函数、零第三方依赖；常数冻结（V7/V8/V22），修改即 spec 修订
MARGIN_FLOOR = 256            # token
MARGIN_RATIO = 0.10           # [C-15] 量级锚定
ASCII_PER_TOKEN = 3.0         # /4 的 JSON 保守化 [C-24][C-26]
CJK_TOKEN_PER_CHAR = 1.0      # 覆盖 GLM/o200k/Qwen [C-25][C-73]；cl100k 局限见 spec
OTHER_PER_TOKEN = 2.0
MSG_OVERHEAD_TOKENS = 4       # [C-7][C-76] 3+1 保守化
DIFF_MAX_TOKENS = 128         # segment 窗内单帧 diff 行最坏常数（输出结构有界，V9）
CALIBRATION_SAFETY = 0.85     # V19 装填折扣 [C-32][C-37][C-33]
CALIBRATION_MIN_SAMPLES = 8   # 样本不足不升档 [C-32]
CALIBRATION_WINDOW_BATCHES = 8  # 批最大值窗口深度（F8：窗口单位=批，序无关）
PRIOR_INFLATION = 1.2         # 首批先验保守放大（V17）
TEMPLATE_HEAD_TOKENS: dict[str, int]                  # V22：per-stage 冻结模板头 est 常数
                                                      #   （= est_text(CONTRACTS §10 冻结文本)，
                                                      #   离线测试跨层断言与算子常数一致）

def margin(context_window: int) -> int
def input_budget(profile: LLMProfile) -> int          # cw − max_output_tokens − margin；cw==0 → 0（预算关）
def embed_budget(profile: EmbeddingProfile) -> int    # cw − margin
def est_text(s: str) -> int                           # ceil(ascii/3 + cjk×1.0 + other/2)
def est_image_prior(profile: LLMProfile, px: int) -> int
                                                      # provider 公式先验 @ 生效 px（V8 v3）：
                                                      #   anthropic = min(⌈px/28⌉², 1568)
                                                      #   openai_compatible = tile 制最坏纵横比
                                                      #     （2048→短边768 归一化；@2048 竖屏 = 1445 [C-60]）
                                                      #   （校准器先验种子 = 本值 × PRIOR_INFLATION）
def est_prompt(bundle: PromptBundle, profile: LLMProfile,
               schema: dict | None,
               image_cost: int) -> int                # Σ est_text + n_images×image_cost
                                                      #   + MSG_OVERHEAD×消息数 + est_text(schema JSON)；
                                                      #   image_cost 由调用方读校准器传入（M9 终检同源）
def fit_text(s: str, budget_tokens: int,
             keep: Literal["head", "edges"]) -> str   # 行边界截断：head=头部保留（embed）；
                                                      # edges=首末恒保留丢中段（既有家族语义，V9）
def min_window(cfg: ResolvedConfig) -> int            # 最坏保证装填量 w_min（V9 护栏 + V12 estimate 上界
                                                      # 共用；未声明窗口 → cfg.segment.window 原值；基于先验）
def classify_stage_error(exc: BaseException) -> str | None
                                                      # V27①共享 helper：ContextOverflowError →
                                                      #   "context_overflow"；OutputTruncatedError →
                                                      #   "output_truncated"；其余 None（算子分类器前置调用）

class ImageCostCalibrator:                            # V19：每 profile 每图成本在线校准（运行内存，零持久化；
                                                      #   实例由 LLMClient 自持，公开面 llm.calibrator——V23②）
    def observe(self, profile: str, prompt_tokens: int,
                text_est: int, n_images: int) -> None # M9 每响应喂样本（含图调用才计；usage 缺失 → 不记样本，
                                                      #   WARN 一次/profile，先验长期生效——[C-64] 兜底）
    def freeze_batch(self) -> None                    # M10 批边界冻结：聚合本批样本 max（序无关）压入
                                                      #   deque(maxlen=CALIBRATION_WINDOW_BATCHES)，
                                                      #   刷新可读快照（第 N 批装填只读 <N 批聚合值）
    def cost(self, profile: str) -> int               # 装填读数 = max(批最大值窗口) ÷ 0.85 取整；
                                                      #   累计样本 < 8 → 先验 × 1.2
```

数据自适应的贪心装填器（`_pack_windows(costs, budget, cap)`）**属算子逻辑、
落在 segment.py**（依赖方向不变：operators → common）；budget.py 只提供估算
与预算原语 + 校准器。

`est_text` 对 prefix 单调 ⇒ `fit_text` 在行边界二分，确定性、O(n log n) 上界。
CJK 判定 = Unicode 块 CJK Unified Ideographs 及扩展 + 全角标点（实现列举区间，
测试钉死样例）。

### 3.3 装填规则（逐调用点；普查表 13 点全覆盖）

**调用次数面（动态装填 + 静态护栏/上界——V9/V12）**

1. **segment 窗口（M14）**：会话级预计算逐帧 digest → 贪心切窗（重叠 1 帧、
   接缝归后窗不变）：装填条件
   `est_static_system + Σ c_i ≤ input_budget ∧ 窗内帧数 ≤ window`，
   `c_i = est_text(digest_i) + DIFF_MAX_TOKENS + (est_image(profile) if vision_resolved else 0)`，
   溢出即封窗。`window` 为纯上限；预算未声明 → 逐字节退化为现行固定窗。
   静态护栏（M1）：`per_frame_max = est_text(digest_max_chars 最坏串) +
   DIFF_MAX_TOKENS + est_image`，`w_min = ⌊(input_budget − est_static_system)
   / per_frame_max⌋`；护栏下限 `floor = 3 if (verify.enabled ∧
   verify.policy == "repair" ∧ segment.enabled) else 2`（F14 审计修正：回收
   复裁窗仅在 policy="repair" 下构造——verify.py:744-748 路由门；policy="drop"
   的配置不做三帧静态要求）——verify 的回收复裁面用固定 [前成员, 候选, 后成员]
   三帧窗直调 judge_window（verify.py:1046-1052），预算须静态保证该窗也装得下，
   否则修复路径运行期必然 `context_overflow`；`w_min < floor` →
   **CONFIG_ERROR**（M1 报错优于运行期逐会话/逐修复爆错，且由此保证运行期
   装填与复裁永不失败）；`w_min == floor` → WARN（窗数放大警示）。estimate
   上界公式共用 w_min（V12）。示例：GLM-4V-Flash（16384/1024 声明）text-only w_min=28→上限
   window 生效（不钳）；vision（anthropic 公式）w_min=6 ⇒ 15 帧会话上界
   ceil(14/5)=3 窗，实际按摘要长短典型 1–2 窗。
2. **stitch 卡池（M16）**：卡结构有界（max_open+1 × digest 项）→ M1 静态
   预检：最坏 est > input_budget → WARN（不自动缩 max_open——改语义须用户
   动手）；运行时靠终检 + `on_error="keep"`（候选自开线索，保守安全）。

**内容面（单调用内容动态裁剪，调用次数不变——V9）**

3. **单记录树渲染（classify/annotate/verify/quality pointwise）**：
   `UITree.serialize(max_chars=…)` 的实参从固定 `ui_tree_max_chars` 改为
   `min(ui_tree_max_chars, fit_chars)`，fit_chars 由该调用点的记录侧预算份额
   经 `fit_text` 语义折算（渲染后按 est 复核，超则按行丢尾，保留既有
   `…(truncated N nodes)` marker）。`ui_tree_max_chars` 保留为绝对上限。
   **verify 多评审共用单 prompt** → 记录侧份额按评审团**最小 input_budget**
   装填（V25②）；quality 逐 (对, 评审) 各自构建，按本评审预算装填。
4. **quality pairwise（普查嫌疑①）**：记录侧预算 =
   `input_budget − est(系统提示+准则文本)`（per-judge，见 ③ 注），两记录各半；
   每侧渲染按 ③ 截断。附图（UI 对 2 张）计校准成本 ×2 后再分。
5. **序列步骤行（quality/annotate/verify 三处，普查嫌疑②）**：步骤行块获得
   预算份额，超出 → `fit_text(…, keep="edges")`（首末步恒保留、丢中段整行 +
   原位 marker——与成员摘要块既有语义同族）。
6. **annotate 序列关键帧与份额顺序**：预算分配定序（确定性）——①系统侧
   静态部件（instruction/schema/few-shot，不裁，V13③ 把关）；②文本块
   （步骤行 + 成员摘要块）按其既有绝对上限渲染并计 est；③图片吃剩余：
   `k_eff = min(sequence_frames, max(2, ⌊剩余 / est_image⌋))`，首末帧恒保留、
   中间均匀下采样（既有 `_keyframe_indexes` 语义不变，只缩 k）；④若 k=2 仍
   超预算，回头裁文本块（⑤的 edges 语义）直至装下；⑤仍不下 → V10。
   **图片先于文本让步**：文本摘要是裁决兜底证据、token 效率高于像素
   （UI-TARS 文本全保留/截图限最近 N 帧的同向取舍 [C-20]）。
7. **generate 种子**：`seeds_per_call` 降级为上限；按 rng 采样序**从尾部丢弃**
   种子直到装下（确定性），min 1；仍装不下 → V10。多 profile mixture
   （`generate.llms` round_robin/weighted）下按**本次调用的目标 profile** 的
   预算装填——轮转序确定性，装填结果仍可复现。
8. **dedup 语义嵌入（普查嫌疑④）**：`_dedup_text` 输出经
   `fit_text(…, embed_budget, keep="head")` 截断（V15）。
9. **M8 L3 修复调用（V25① 钉死）**：机制 = 捕获修复 complete() 抛出的
   `ContextOverflowError(phase="precheck")`，该轮记修复失败；修复 prompt
   恒定 ⇒ 余轮必然同败 → **短路至耗尽**；reject 归因维持既有
   `schema_violation`/`callback_violation`（不新增值、不计
   overflow_records；不截断修复原文——截断即破坏修复语义）。
10. **extract（恒 2 帧+2 图）/ verify 回收复裁（≤3 帧）/ probe（1 token 纯文
    本）**：无可收缩项，终检兜底（V16）；extract 超限走既有
    `on_error="fallback"` 机械回退。
11. **classify 类示例 / annotate few-shot / 用户 Schema / instruction /
    rubric**：静态系统侧部件，**不动态裁剪**（用户语义资产）——由 V13③ 的
    M1 静态预检把关（≥100% ERROR / >50% WARN；模板头经 V22 冻结常数计入）。
12. **不可裁剪动态块（V25③ 闭合）**：verify 的 `[标注结果]` JSON 与修复轮
    `[上一版标注]/[审核意见]` 尾注（per-record 动态、语义资产）——计入 est、
    永不裁剪；全部可裁份额（③⑤⑥）耗尽仍超 → V10。

**图片成本读数（V17/V19）**：以上各点凡涉图片，图片单价读校准器
`ctx.llm.calibrator.cost(profile)`（批冻结快照；首批 = `est_image_prior`
×1.2）；图片实际发送尺寸 = `default_image_px` 工作点（V18；0 = max_image_px
即现行为），V21 升级档经 `PromptBundle.image_px` 载体传递（V23①）。

**溢出反应（V20/V24，运行期）**：统一溢出信号（预算开启下 400 嗅探命中，或
双协议 `model_context_window_exceeded` 200 形态）→ 有界降级重试（segment
窗对半改切、annotate 关键帧减半、其余收紧文本份额一次）；仍不下 → V10
reject（400 嗅探态终局补喂熔断一次，200 形态不补喂——§3.5 矩阵）。未识别的
400 走现行 fatal 老路（零回归）。

**判审升级（V21，修复路径）**：verify fail ∧ policy="repair" → 重标注换档
（k 减半、px 上探 1.5×/维 ≤ max_image_px，经校准估算复核预算）；单向、
每记录 ≤ max_repair_rounds 次。

**终检（V16）**：`complete()` 分派前 `est_prompt + max_output_tokens + margin
> context_window` → `ContextOverflowError`。装填层正确时不可达。

### 3.4 use_vision 移除与 vision_resolved（M14/M1，V1–V5）

- `SegmentConfig`：删 `use_vision` 字段，增 `vision_resolved: bool = False`
  （注释标注 parse product，`mode_resolved` 同款；CONTRACTS §6 镜像同步）。
- loader：删 line 700 解析；`[segment]` 已知键集合改造 → 显式 `use_vision`
  命中 V2 定向 CONFIG_ERROR；vision_users 删 segment 分支（V3）；load() 收尾
  `segment = replace(segment, vision_resolved=…)`；新增 V5 WARN。
- `build_segment_prompt`（segment.py:138）判据改 `seg.vision_resolved`；
  §10.9 冻结模板注释行同步改写（CONTRACTS.md:4252-4256）。
- 贫瘠护栏文案（V4）两处改写；CONTRACTS rule 33/34 改写（segment 行删出
  vision 集、注明 adaptive）。

### 3.5 错误分类与观测（spec §7.6 / §6.4 增量）

§7.6 增两行（闭合词表扩两值）：

| kind | 语义 | 处置 |
|---|---|---|
| `context_overflow` | 三形态（V24）：precheck = 终检命中（V16）/ 最小单元不装（V10）；reactive-400 = 预算开启下嗅探命中且 V20 降级耗尽；reactive-200 = `model_context_window_exceeded` 且降级耗尽（或无降级面） | 记录级 `failed` → rejects；计 `report.budget.overflow_records`。熔断矩阵：**precheck 不计连击**（无 provider 交互）；**reactive-400 终局计入连击**（A7 已裁——由属主算子经 `ctx.metrics.record_provider_result(fatal=True)` 补喂恰一次，M9 抛出时不喂；成功降级/任何成功调用清零连击）；**reactive-200 终局不补喂**（HTTP 交互成功、streak 已被该次 ok 清零——`llm.call` 事件维持 status="ok"，实现者不得"修正"为 fatal，F9）。均不烧常规重试（V20 降级重试独立计数、有界） |
| `output_truncated` | 响应以输出上限截断收尾（`finish_reason=length` / `stop_reason="max_tokens"`，V11）——输入合窗、输出写满 max_output_tokens | 记录级 `failed` → rejects 归因独立成桶；不喂熔断（交互成功，`llm.call` 维持 ok）；不进 L1–L3 修复循环 |

L3 修复调用内的 precheck 溢出**不落词表**（V25①：轮失败短路耗尽，归因维持
`schema_violation`/`callback_violation`）。z.ai 扩展终止值 `sensitive`/
`network_error` 及未知值不做专项处置（V11③，沿现行管线）。

§6.4：`report.budget` 节（V13②，counts-only）；rejects reason 词表增两值。
§7.2 事件目录**零增**（无新 trace 通道）；启动期钳制 INFO 行走既有 stderr
运行日志（不含数据内容），归属 M10 启动段（audit ADD#22 裁定：orchestrator
运行起点打印，非 loader——加载期 logging 尚未按 CLI 覆盖定级）。

### 3.6 estimate_run / console / dry-run（V12）

- `estimate_run`：`segment_calls` 公式的 window 实参替换为
  `budget.min_window(cfg)`（**上界语义**：实际每窗 ≥ w_min 帧 ⇒ 实际窗数 ≤
  估算；预算关闭时 w_min=window，与现状同构）；其余公式零改动。stream
  stderr 注行（orchestrator.py:1106-1111）在 w_min < window 时增补一句
  「segment 按预算最坏装填报上界」。
- console：**零代码改动**（V12 容忍度证据：裸文本 stage 行、完成态 ✓ 吸附、
  批进度条钳 1.0、ETA 按 records 维度）；`LLMClient.snapshot()` 零改动。
- dry-run 行集与 goldens：examples 声明保守实效窗 131072（V26/[C-59]）→
  w_min ≈ 46 > window 默认（stream 工程 window ≤ 20）⇒ 不钳、estimate 数值
  不变 ⇒ 五个黄金文件（`dryrun-{text,text-synth,ui,stream,stream-text}.txt`）
  逐字节不动（强制面 tests/cli/test_console.py:597-617 不变）。声明小窗的新
  工程打出上界值 + 增补注行，属新行为面、按 V12 语义另行验收。
- **演进候选（v1 不做）**：运行中分母修正——`metrics.run_estimate` 重复调用
  通道或 counters 拉取通道（V12 已证实机械可行）。

### 3.7 测试与验收

**离线单测**
- `tests/common/runtime/test_budget.py`（新）：est_text 中/英/混排/JSON 样例
  钉死；est_image_prior 双 provider 双 px 档（含 openai 最坏纵横比 1445
  @2048 [C-60]）；margin/input_budget 边界（floor、非正预算）；fit_text 两
  模式行边界与幂等；min_window 矩阵（未声明/大窗/小窗/vision）；
  **TEMPLATE_HEAD_TOKENS 跨层等式**（V22：est_text(各算子模板头常数) ==
  budget 常数，测试层双向 import）；`classify_stage_error` 词表；
  **ImageCostCalibrator**：observe/freeze_batch/cost 语义、批最大值
  deque(8) 窗口（F8）、0.85 放大、样本 <8 用先验、usage 缺失不记样本
  （[C-64]）、**批冻结确定性**（同批样本乱序两次重放快照逐值一致；批内
  observe 不影响本批 cost 读数）。
- `tests/common/config/test_config.py`：context_window 解析与校验（0 合法、
  负值报错、非正预算报错）；default_image_px 校验（0 合法、> max_image_px
  报错）；V2 移除键定向报错文案（原始节探针机制，V27②）；vision_resolved
  推导矩阵（modality × strategy × supports_vision）；V6 引用 WARN、V5 WARN、
  V13③ 静态预检 ERROR/WARN、V9 护栏 floor 2/3 两态（F14：policy="drop" 不
  升 floor）（BASE_CONFIG 串替换模式）。
- `tests/common/runtime/test_llm_client.py`：终检先于网络触发
  （ContextOverflowError(phase="precheck")，零网络可测）；终止原因归一解析
  （`finish_reason=length` / `stop_reason=max_tokens` → OutputTruncatedError；
  双协议 `model_context_window_exceeded` → ContextOverflowError(
  phase="reactive")，纯响应解析）；V20 pattern 集匹配纯函数（[C-75] 五家
  样本钉死 + 预算关闭时不嗅探）；LLMResponse.finish 字段与 _result_usage
  形状适配（F9）。
- `tests/operators/test_segment.py`（影响面清查逐函数标注）：
  `test_window_spans_stride_is_window_minus_one`（:366）改造为装填器测试
  （成本向量 → 窗界断言：重叠 1 帧、上限封顶、溢出封窗、min-2、确定性重跑）；
  window=2 的五个 fixture 测试（:373 接缝覆盖、:574/:603 on_error 两态、
  :636 windows_failed、:689 贫瘠护栏）随装填器参数化重建（预算关闭态下断言
  值不变——回归锚）；两态 parts 形状（:279）按 profile 参数化（V1）；WARN
  文案改写断言（V4）；新增"预算开启下同输入重跑窗界逐字节一致"用例。
- `tests/orchestration/test_orchestrator.py`：四个 segment_calls 估算测试
  （:2021-2069、:2340-2354）增预算开/关两态分支（关 = 现值回归锚；开 =
  w_min 上界公式）。
- `tests/operators/test_{quality,annotate,classify,verify,dedup}.py`：微型
  声明窗口下的截断行为（marker 在位、首末保留、确定性重跑一致）；各算子
  错误分类器的 context_overflow/output_truncated 分支（V27①）；verify
  min-over-panel 装填（V25②）；annotate k_eff/V20 减帧/V21 换档参数传递
  （F3 追加尾参）。
- 回归锚：`uv run pytest -q -m 'not integration'` 全绿；plain console 黄金
  三层锚不动。
**集成（真端点 glm-5.2）**
- 小声明窗口 → `context_overflow` 记录级 rejects、run 存活、报表归因正确；
- **z.ai 超窗错误体采集**（V20 pattern 集的事实来源，[C-81] 闭合）：构造必
  超窗请求，记录状态码与 body 全文入测试断言与 E2E-FINDINGS；**实测未加
  后缀 `glm-5.2` 的实效窗与 `[1m]` 后缀行为**（V26），结论记 E2E-FINDINGS，
  examples 声明值若可上调随之修订；
- 校准收敛：多图真调用后 `report.budget.image_cost` 与 usage 反推值一致；
- `test_stream_llm.py` 适配多图窗口真调用（成本上升；:227-252 直调
  judge_window 断言帧数不断言窗数，机械上不受装填改动影响）；
- 429/400 熔断路径回归：确认 overflow 不再计入 `_fatal_streak`。
**验收（examples 真跑）**
- 三示例全跑通；`examples/stream` UI 工程（vision 翻转 + 实效窗声明）重采
  手册 25/26 章样例输出；守恒恒等式与 strict 语义不变；受新报表键/INFO 行
  波及的其余章样例按 audit MANUAL-IMPACT 清单核对重采（§3.8 手册表）。

### 3.8 文件修改清单（立项后逐文件执行；2026-07-22 三方审计增订版——ADD/CORRECT 全折入）

**代码**
| 文件 | 改动 |
|---|---|
| `labelkit/common/config/model.py` | LLMProfile/EmbeddingProfile +`context_window`；LLMProfile +`default_image_px`（V18）；SegmentConfig −`use_vision` +`vision_resolved` |
| `labelkit/common/config/loader.py` | 解析/校验/V2 定向报错（原始节探针机制，V27②）/V3 集合改造/V5·V6·V13③ 告警与预检/**V9 静态护栏（w_min < floor(2/3) → CONFIG_ERROR、== floor → WARN，F14 条件）**/`default_image_px ≤ max_image_px` 校验/收尾 replace 冻结 |
| `labelkit/common/runtime/budget.py`（新） | §3.2 全部纯函数与常数（含 TEMPLATE_HEAD_TOKENS——V22、DIFF_MAX_TOKENS、classify_stage_error helper——V27①）+ `ImageCostCalibrator`（V19，批最大值 deque(8)——F8） |
| `labelkit/common/runtime/llm_client.py` | V16 终检（precheck）；V11/V24 终止原因归一解析（length/max_tokens → OutputTruncatedError；`model_context_window_exceeded` 双协议 → ContextOverflowError(reactive)）；ContextOverflow/OutputTruncated 不喂 `_record_provider_result(fatal=True)`；V20 超窗错误体 pattern 集（[C-75] 种子；完整 resp.text 上匹配、预算门控；命中抛 reactive 不喂 streak——F5）；每响应喂校准样本（V19；usage 缺失不记样本 + WARN 一次）；图片编码生效 px = `bundle.image_px or default_image_px or max_image_px` 钳 max（V18/V21/V23①）；`PromptBundle.image_px` + `LLMResponse.finish` 追加字段与 `_result_usage` 形状适配（V23/F9）；`LLMClient.calibrator` 公开面（V23②，自持构造——factory 零改动） |
| `labelkit/common/errors.py` | `ContextOverflowError(phase: Literal["precheck","reactive"])`（V24）；`OutputTruncatedError`（V11）；ErrorKind 增 `CONTEXT_OVERFLOW`/`OUTPUT_TRUNCATED` |
| `labelkit/common/runtime/schema_engine.py` | L3 修复调用捕获 ContextOverflowError → 轮失败 + 短路耗尽，归因不变（§3.3⑨/V25①） |
| `labelkit/operators/segment.py` | vision_resolved 判据；会话级 digest 预计算前移 + 贪心装填器 `_pack_windows`（替换 `_window_spans` 的定长切分，保留重叠/接缝语义）；V20 溢出窗对半改切重试（终局补喂熔断——V24）；贫瘠文案（贫瘠护栏计算路径独立不动）；**实际窗数计数 → report.stream.windows（V13④，M14 属主）**；错误分类器 overflow/truncated 分支（V27①） |
| `labelkit/operators/quality.py` / `classify.py` / `verify.py` | 动态裁剪（§3.3③④⑤，quality per-judge、verify min-over-panel——V25②）；verify V21 触发（fail ∧ repair 时向 annotate 修复面传阶梯参数，经 annotate_record 追加尾参——F3）；不可裁块计入 est（V25③）；错误分类器分支（V27①）；**逐裁剪点计数 `budget.truncations.<stage>`** |
| `labelkit/operators/annotate.py` | 份额定序 + k_eff（§3.3⑥）+ V20 减帧重试 + V21 升级换档（`build_annotate_prompt`/`annotate_record` 追加尾参——F3）；错误分类器分支；truncations 计数 |
| `labelkit/operators/generate.py` | 种子尾丢（§3.3⑦）；错误分类器分支 |
| `labelkit/operators/dedup.py` | embed 输入截断（V15）；truncations 计数 |
| `labelkit/operators/extract.py` / `stitch.py` | 错误分类器 overflow/truncated 分支（V27①；extract 走既有 fallback、stitch 走 on_error="keep"——§3.3②⑩ 语义不变） |
| `labelkit/common/contracts/types.py` | `digest_is_poor` docstring（V4）；`ImageRef.load_base64` 签名不变（builder 传入生效 px）；serialize 实参链不变 |
| `labelkit/orchestration/orchestrator.py` | estimate_run 用 min_window（先验估）+ stream 注行增补句（:1106-1111）；批边界调 `self.llm.calibrator.freeze_batch()`（V19）；report.budget 汇总（profiles/w_min/truncations/overflow_records/image_cost/degrade_retries/escalations）+ report.stream.windows；**启动期预算 INFO 行（V13①，归属 M10 启动段）** |
| `labelkit/orchestration/factory.py` / `runtime.py` | **显式无改动**（校准器由 LLMClient 自持——V23② 裁定，此行防实现者臆造装配点） |
| `pyproject.toml` | **显式无改动**（零新依赖；budget.py 纯 stdlib——审计核验） |
**规格与契约**
| 文件 | 改动 |
|---|---|
| `docs/CONTRACTS.md` | LLMProfile/EmbeddingProfile/SegmentConfig 镜像；rule 33/34；§10.9 模板注（:4252-4256）；budget.py 新节；complete() 终检条款；错误类两枚；`build_segment_prompt` 增 digests 形参（:2994-2997）；**`build_annotate_prompt`/`annotate_record` 追加尾参（:1820/:1838——F3）**；**`PromptBundle.image_px`/`LLMResponse.finish`/`LLMClient.calibrator` §7.8 面（V23）**；SegmentConfig.window 字段注与 Strategy/Calls 规范弹的"step = window−1"句改装填语义（:1107-1110、:3013-3022）；**:443-459 frame_digest 契约 docstring 与 :3043-3045 M14 贫瘠护栏条款的 use_vision 文案（V4）**；**§9.2 (stage, reason) 闭合登记表增两值（:3576-3583）**；**§9.3 report 契约增 report.budget（计数键名 [FROZEN HERE]）与 report.stream.windows（:3598+）** |
| `spec/50-ch5-config-spec.md` | §5.1 两 profile 表增 context_window 行 + `default_image_px` 行；`max_image_px` 行改语义（:23，V18/V27③）；§5.2 segment 表（删 use_vision/注 parse product）；window 键行（:118）改"上限 + 预算装填"语义；`segment.llm` 行 vision 括注（:117，V3）；`ui_tree_max_chars` 行（:107）、`seeds_per_call` 行（:181）、`sequence_frames` 行（:192）补"上限 + 预算收缩"语义；batch_size 语义句；§5.1 示例块核对（:42-89） |
| `spec/301-m1-config.md` | 校验行（V2/V3/V5/V6/V9 护栏/V13③/default_image_px）；parse product 清单增 vision_resolved |
| `spec/309-m9-llm-client.md` | 预算公式/估算器/终检/错误分类/校准器新小节（§3.9.x）；**既有图像编码行改写（:80，工作点/升级档）**；**§3.9.3 错误分类与熔断行改写（V11/V16/V20/A7 交互）**；每响应校准采样行 |
| `spec/314-m14-segment.md` | §3.14.3-5/7：策略表步长句（:90）、refine() 伪代码改装填形态（:130-151）、模板注（:115）、window 键行（:173）、贫瘠指引（:162）、§3.14.7 背书"恒定单请求规模"→"有界单请求规模"且 S32 "调大 window 即整段单调用"补预算前提（:205）；windows 计数行（V13④） |
| `spec/304-m4-qualityqurating.md` / `305-m5-annotate.md` / `307-m7-verify.md` / `313-m13-classify.md` / `303-m3-dedup.md` / `306-m6-generate.md` / `308-m8-schema-engine.md` / `316-m16-stitch.md` / **`315-m15-extract.md`（ADD#1）** | 各自装填规则行（§3.3 对应点；315 = ⑩ 无收缩项 + fallback 语义不变） |
| `spec/310-m10-orchestrator.md` | estimate_run 公式行；**3.10.3 批边界 freeze_batch 步骤、report.budget/stream.windows 汇总、启动 INFO 行、dry-run 注行修订（:38 "segment_calls 行含义不变"句改上界——CORRECT#4）** |
| **`spec/20-ch2-overall-design.md`（ADD#2）** | :115 内嵌 `segment_calls = Σ ceil((L−1)/(window−1))` 公式改 w_min 上界语义 |
| **`spec/311-m11-emitter.md`（ADD#3）** | :13 rejects (stage, reason) 登记增两值；:14 report 结构登记增 budget/stream.windows |
| `spec/70-ch7-logging.md` | §7.6 两新 kind（三形态矩阵）；启动 INFO 行 |
| `spec/60-ch6-io-formats.md` | report.budget；report.stream.windows；rejects reason 词表 |
| `docs/dev/SPEC-stream-segmentation.md` | S22/S32 追加 v1.11 交叉注；**S12（:43）/S30（:61）同款交叉注（ADD#7——use_vision 指引/vision 校验句被 V1/V3/V4 修订）**；历史记录本体不改写 |
| `spec/40-ch4-data-structures.md` | frame_digest 注释；budget helper 契约引 |
| `spec/10-ch1-overview.md` | §1.5 背书增行（[C-5][C-15][C-19][C-54] 等）；**:121 树可靠性护栏行"手册指引开 use_vision"句改写（ADD#4/V4）**；§1.6 决策行（2026-07-22，V1–V27） |
| **`spec/85-ch9-references.md`（ADD#5）** | §1.5 新背书行的编号引用落位（[N] 条目追加） |
| `spec/80-ch8-nongoals-roadmap.md` | use_vision 行改写；roadmap：运行中分母修正/区域升清/密度旋钮/输出侧预算/计数 API 校准 |
| 渲染物 | `uv run python tools/build_design_doc.py --pdf` 重建 |
**手册与示例**
| 文件 | 改动 |
|---|---|
| `docs/manual/06-config-toml.md` | context_window 键文档（llm + embedding §6.4 :147）；`default_image_px` 新行；`max_image_px` 行改语义（:139）；`max_output_tokens` 行 V11/预算权衡改写（:137——"截断触发修复环"句已失真）；supports_vision 行 segment 例外句删除；收尾速查 :198-199 |
| `docs/manual/07-project-toml.md` | :56 `ui_tree_max_chars` 行——绝对上限 + 预算下动态收缩（§3.3③） |
| `docs/manual/25-stream.md` | 贫瘠段（:280）/vision 例外段（:302）改写；window 语义与调参叙事（:40/:45/:258——"window ≥ 会话长即单窗"补预算前提）；**:143 关键帧 bullet、:284-291 成本账表公式重写、:304 多图硬限段（V5 姊妹 WARN + default_image_px）**；真跑 boundary trace 样例（:260-276）与对账段（:292）重采 |
| `docs/manual/17-tuning.md` | 调用账表 segment 行公式（:12）改上界语义；**:26 sequence_frames 成本注（上限 + k_eff）；:35 "调小 max_output_tokens 不省钱"句改写（V11 终局化 + 预算权衡）** |
| **`docs/manual/18-troubleshooting.md`（ADD#8）** | §18.1 闭合 kind 表增两行（:11-27）；:23 schema_violation 行"输出被截断"成因剥离（V11）；:26/:59 熔断叙事补溢出豁免/A7；**A4 承诺的"遇 400 熔断先声明 context_window"指引** |
| **`docs/manual/08-outputs.md`（ADD#9）** | :104 rejects (stage, reason) 组合段增两值；§8.4 report 走读增 budget/stream.windows（:108 ff.，v1.8/v1.9 先例 :187） |
| **`docs/manual/14-schema-engine.md`（ADD#10）** | :28 L1 "修截断"句、:70 "截断 JSON 是 L1 最常见客户"句改写（V11 终局化）；§3.3⑨ 修复超预算规则入循环描述 |
| **`docs/manual/15-cli.md`（ADD#12）** | :34 dry-run 字段语义段补 segment 上界句 + 条件注行 |
| **`docs/manual/16-observability.md`（ADD#13）** | 启动 INFO 预算行入 §16.4 运行日志族；报告侧读单（:131 族）增 report.budget；trace 表 :40 llm.call usage 行交叉引校准 |
| **`docs/manual/04-concepts.md`（ADD#20）** | :74 熔断段"重试耗尽同计连击/成功清零"补 V16/V10 豁免与 V20/A7 反应态注 |
| **`docs/manual/09-dedup.md`（ADD#16）** | §④ 语义层（:57-59）补 V15 embed 头部保留截断注 |
| **`docs/manual/11-annotate.md`（ADD#17）** | :39 树文本双通道渲染动态上限；:65 sequence_frames 上限化 + V20 减帧 + V21 升级 + S28 交互；:143 调参 bullet |
| **`docs/manual/12-generate.md`（ADD#18）** | :23/:54/:148 seeds_per_call 上限化 + 确定性尾丢 |
| **`docs/manual/13-verify.md`（ADD#19）** | V21 修复换档叙事（k 减半 + px 上探 ≤ max_image_px） |
| `docs/manual/05-quickstart.md` | :111 图片缩放叙事补 default_image_px（audit 核验为编辑级，无重采） |
| `docs/manual/appendix-a-cheatsheet.md` | 校验规则 18、segment 速查两行（window :217）、**use_vision 行删除（:221）**、llm profile 行（:29-32：max_output_tokens V11 注 :30、max_image_px :32、+default_image_px）、embedding 行（:34-39 +context_window）、`ui_tree_max_chars` :59、`seeds_per_call` :106、`sequence_frames` :125、新增 context_window 行 |
| 受真跑影响章重采 | **必采**：25/26（vision 翻转 + 新报表键 + 成本账）；**核对后按需**：03（quickstart stderr——INFO 行）、08（text 报表 budget 节）、10/13/16/20/21/22/23/24（各内嵌真跑报表/日志摘录——audit MANUAL-IMPACT 清单）；既往先例章 5/15/18/19 同步核对 |
| `examples/config.toml` | 两 profile 声明 `context_window = 131072`（**端点实效保守值**，V26/[C-59]——P6 实测后可上调；勿写 1M） |
| **`docs/dev/E2E-FINDINGS.md`（ADD#25）** | 新录：z.ai 超窗错误体样本、实效窗实测、校准首批偏差实测 |
| **`CLAUDE.md` / `AGENTS.md`（ADD#26）** | :7 "current spec revision is v1.10" 改 v1.11；:9 spec 清单括注追加 v1.11；"Working with the spec" 列表 :134 后按 v1.9/v1.10 先例增 v1.11 段；两文件字节同步 |
| `README.md` | **显式无改动**（audit 核验无过期陈述） |
**测试**：§3.7 清单（budget/config/llm_client/schema_engine/segment/六算子/
orchestrator/console goldens/集成）；audit TESTS 枚举为逐断言执行细目
（test_segment.py :58-73 fixture 键替换、:279-293 两态重键、:366/:373/:574/
:603/:636/:689 装填器参数化重建、:703 WARN 文案、test_config.py :1515/
:1526-1564/:1920-1930 三处既有断言改造、test_orchestrator.py 四估算测试两态
分支、test_console.py :597-617 黄金锚不动、test_annotate.py :657-731/
:765-832、test_quality.py :433-462/:962-1076、test_classify.py :331-343、
test_dedup.py :637-658、test_generate.py :247-266、test_verify.py
:1163-1321、test_emitter.py :948/:1077 键子集断言核验）。

### 3.9 整体待办（实施工序，2026-07-22 裁决闭合后钉板）

> 工序服从仓库铁律：**spec 先行**（P0 完成前不写生产代码）；每阶段出口
> 有可失败的验证门（fable 原则：done = 一次可观察的验证）。勾选状态在本
> 节原地维护。

**P0 规格正式修订**（属主：spec/CONTRACTS；§3.8 表逐行执行）
- [ ] spec/50-ch5、301-m1、309-m9、314-m14 四主文件修订（配置面/校验行/预算小节/装填形态）
- [ ] spec/303/304/305/306/307/308/313/315/316 各装填规则行；310 估算+批边界+报表行；20 公式行；311 登记表；40/60/70/10/80/85 横切行
- [ ] docs/CONTRACTS.md 全部落点（§3.8 行：镜像 ×3、rule 33/34、§10.9 注、budget 新节、complete() 条款、错误类、build_segment_prompt/build_annotate_prompt/annotate_record 签名、§7.8 V23 三面、§9.2/§9.3 登记、frame_digest 两处 V4 文案）
- [ ] docs/dev/SPEC-stream-segmentation.md S12/S22/S30/S32 交叉注
- [ ] `uv run python tools/build_design_doc.py --pdf` 重建渲染物
- 出口门：spec 与本 SPEC §2/§3 逐决策比对无缺漏（V1–V27 每项在 spec 正文有落点）

**P1 公共层基础**（M1 + budget.py + errors）
- [ ] model.py/loader.py：context_window ×2、default_image_px、vision_resolved、V2 定向报错（原始节探针）、V3 集合改造、V5/V6/V13③ 告警预检、V9 护栏（floor 2/3，F14 条件）、收尾 replace 冻结
- [ ] labelkit/common/runtime/budget.py：常数（含 TEMPLATE_HEAD_TOKENS/DIFF_MAX_TOKENS）+ est_* + fit_text + min_window + classify_stage_error + ImageCostCalibrator（批最大值 deque(8)）
- [ ] errors.py：ContextOverflowError(phase) + OutputTruncatedError + ErrorKind 两值
- 出口门：`uv run pytest tests/common -q` 全绿（含新 test_budget.py 与 test_config.py 增例；V22 跨层等式测试在 P3 后启用断言）

**P2 M9 客户端**
- [ ] complete() 终检（V16 precheck）；终止原因归一解析（V11：length/max_tokens + 双协议 model_context_window_exceeded——[C-57][C-58]）；LLMResponse.finish + _result_usage 适配（F9）
- [ ] V20 超窗错误体 pattern 集（[C-75] 种子 + 可扩；完整 resp.text 匹配；预算门控）；overflow/截断不喂 fatal（F5：M9 抛 reactive 不喂、属主算子终局补喂——A7）
- [ ] 每响应喂校准样本（usage 缺失兜底）；PromptBundle.image_px 载体 + 图片编码生效 px（V18/V21/V23①）；LLMClient.calibrator 自持（V23②）
- 出口门：`uv run pytest tests/common/runtime/test_llm_client.py -q` 全绿（终检先于网络、解析纯函数级、pattern 集钉死）

**P3 算子装填改造**
- [ ] segment.py：digest 前移 + `_pack_windows` 贪心装填（重叠/接缝不变）+ V20 窗对半改切重试（终局补喂）+ vision_resolved 判据 + 贫瘠文案 + windows 计数
- [ ] quality/classify/verify/pointwise：树渲染动态上限（§3.3③④，per-judge / min-over-panel——V25②）；序列步骤行 edges 裁剪（⑤）；不可裁块计入 est（V25③）
- [ ] annotate.py：份额定序 + k_eff（⑥）+ V20 减帧重试 + V21 升级换档（F3 追加尾参）
- [ ] generate.py 种子尾丢（⑦）；dedup.py embed 截断（V15）；schema_engine L3 溢出短路（⑨/V25①）
- [ ] verify.py：V21 触发（fail ∧ repair → 阶梯参数传 annotate 修复面）
- [ ] 全算子错误分类器 overflow/truncated 分支（V27①）+ truncations 计数
- 出口门：`uv run pytest tests/operators tests/common/runtime/test_budget.py -q` 全绿（含 §3.7 逐函数改造清单与 V22 跨层等式）

**P4 编排与观测**
- [ ] estimate_run 用 min_window + stream 注行增补；批边界 `self.llm.calibrator.freeze_batch()`
- [ ] report.budget（profiles/w_min/truncations/overflow_records/image_cost/degrade_retries/escalations）+ report.stream.windows；启动 INFO 行（M10 启动段）
- 出口门：`uv run pytest tests/orchestration tests/cli -q` 全绿；五个 dry-run goldens 逐字节不动（131072 声明下 w_min>window 回归锚——V26）

**P5 全量离线回归**
- [ ] `uv run pytest -q -m 'not integration'` 全绿；确定性重跑用例（装填/校准快照）双跑一致
- 出口门：即上述命令；任何 golden 漂移须回溯到已裁决行为变化并在 PR 说明

**P6 集成与验收（真端点 glm-5.2，需 .env）**
- [ ] z.ai 超窗错误体采集 → V20 pattern 断言 + E2E-FINDINGS 记录；**实测未加后缀 glm-5.2 实效窗与 `[1m]` 行为（V26）**
- [ ] 小声明窗口 → context_overflow rejects、run 存活、A7 连击语义断言；校准收敛断言（report.budget.image_cost ≈ usage 反推）
- [ ] test_stream_llm 多图窗口适配；429/400 熔断路径回归
- [ ] examples 三工程全跑（text/ui/stream ×2 项目）；stream UI 工程 vision 翻转 + 守恒恒等式 + strict 语义核对
- 出口门：`uv run pytest tests/integration -q -m integration` 全绿 + 五条 example 命令 exit 0

**P7 文档同步**
- [ ] manual 04/05/06/07/08/09/11/12/13/14/15/16/17/18/25/26 + cheatsheet 按 §3.8 手册表修订；25/26 章真跑样例重采；03/10/20/21/22/23/24 章按 MANUAL-IMPACT 核对重采
- [ ] 既往同步先例核对（ch. 5/15/18/19 是否被新报表键/日志行波及）
- [ ] E2E-FINDINGS 增录（错误体样本、实效窗实测、校准首批偏差实测）；CLAUDE.md/AGENTS.md v1.11 段 + :7/:9 过期句修订，字节同步
- 出口门：手册引用的每个数字可回溯到 P6 真跑产物

## 4. 非目标（v1.11 明确不做）

- 不引入 tokenizer 依赖；不做调用前计数 API 预检（[C-10] 列演进）。
- 不做 dry-run 装填预演：预扫不碰树内容（ingest.py:600-622 "zero extra
  I/O"），预演需二次全量树解析——dry-run 报 w_min 先验估即可。
- 不做运行中分母修正（两条通道已证实机械可行，V12 记录，列演进）。
- 不改无重叠装填：重叠 1 帧与接缝覆盖语义保留（V9；否则契约影响面扩大一圈）。
- 不做跨运行校准持久化（stateless 非协商约束；V19 校准只活在进程内存）。
- 不做修复路径之外的全局升降档（V21 单向有界——无 ABR 式振荡面，防抖
  三件套 [C-33][C-38] 留给未来若做全局自适应时引用）。
- 不做定向区域升清（裁剪可疑区域而非整帧，Ferret-UI/VRS 同构
  [C-52][C-56]，列演进）。
- 不做输出侧预算（num_per_call × 样本长 vs max_output_tokens，roadmap）。
- 不合并 batch_size/window（V14）；不做跨调用会话级预算。
- margin/密度常数与阶梯常数不开配置面（V7/V8/V18；per-profile 密度旋钮列
  roadmap）。
- 不做"加大 max_tokens 重试"（厂商建议 [C-61]——与预算不变式和确定性冲突，
  显式拒绝并记载；用户补救 = 配置层提高 max_output_tokens）。
- 不做 z.ai 扩展终止值 `sensitive`/`network_error` 的专项处置（V11③——
  沿现行管线流转，垃圾输出由 M8 校验兜住）。
- 不做 SC 自洽分歧触发的升级（F4 删除——M5 主路径无宿主循环，列演进）。
- 不做文本密度在线校准（[C-63] LangChain usage-scaling 证实可行，列 roadmap
  ——cl100k 缺口的将来闭合路径）。

## 5. 风险与演进

| 风险 | 缓解 |
|---|---|
| 估算器对 cl100k 旧词表中文低估 25–40% | spec 记载局限；margin 10%；逃生门=声明更小窗口；roadmap 密度旋钮 |
| GLM 图片 token 无官方公式，openai_compatible 常数可能偏离 | margin 兜底；演进=按 [C-13] tokenizer API 抽样校准（网络成本，opt-in） |
| vision 翻转改变 examples 边界判决 → 手册大面积重采 | 验收步骤固定（§3.7）；判决漂移属预期（新输入=新判据证据） |
| provider 以 429/5xx 报超长（V20 pattern 不匹配状态码） | 先烧常规重试后落 fatal 老路（现状等价，零回归）；集成测试采集 z.ai 实际行为后可扩 pattern 集 |
| 上界分母的松弛（实际调用数 < 分母，stage 行早收尾） | 与既有"无掉件上界、排除修复"分母语义同族（V12 证据）；stage 行无进度条、完成吸附 ✓，无 UX 断裂；report.stream.windows 供对账 |
| 动态装填后窗口边界依赖记录内容 → 判决上下文随数据变 | 确定性审计通过（V9）；同输入同配置逐字节可复现不破；跨数据集不可比属固有性质，手册明示 |
| w_min=2 退化配置的窗数放大（200 帧满长会话至多 199 窗 ≈ 18×） | M1 WARN（V9 护栏）+ 启动 INFO 打印 w_min 与上界；用户调大 context_window/换 profile/缩 digest_max_chars |
| digest 前移触碰冻结签名 build_segment_prompt | CONTRACTS 修订随 §3.8 清单执行；judge_window 公开面不动（verify 复裁零影响，深查已证） |
| 校准冷启动：首批按先验 ×1.2 装填，若先验仍低估（如 GLM 实际计费远超公式） | 首批溢出由 V20 反应路径兜住（降级重试）；第 2 批起校准值接管；样本 <8 不升档（[C-32] 同则） |
| V20 错误体识别漏判（未知网关格式） | 优化门语义：漏判 = 走现行 fatal 老路，零回归；集成采集样本渐进扩 pattern（E2E-FINDINGS 记录）；且新款后端的 200 形态（model_context_window_exceeded）不依赖嗅探即被 V24 捕获 |
| V19 校准依赖 provider usage 返回的确定性 | 同请求同 tokenizer 计数确定；provider 版本漂移与 LLM 输出漂移同类（条件确定性，仓库既有口径）；usage 缺失网关（[C-64]）→ 不记样本、先验长期生效 + WARN 一次 |
| examples 声明 131072 若远低于实效窗 → 多裁不溢出（保守浪费） | 欠声明恒安全（V26）；P6 实测实效窗后上调声明值并重跑核对 goldens |
| 溢出经 OpenRouter 类网关转译为 200+length（[C-68]） | `output_truncated` 桶吸收部分真溢出——归因近似性记入 spec §7.6 注与手册；处置（记录级 reject）两桶一致，无正确性影响 |

演进候选：运行中分母修正（run_estimate 重调 / counters 通道，V12）、
middle-out 通用化 [C-17]、计数 API 校准回路 [C-10][C-13]、定向区域升清
（[C-52][C-56]）、稳态周期性校准探测（BBR ProbeBW 的 ~2% 预算形态
[C-54]）、openai 协议 `detail` 参数透传 [C-46]、输出预算、密度旋钮。

## 6. 引用

[C-1]–[C-28]、[C-29]–[C-56]（跨界自适应质量调研：ABR/游戏 DRS/VLM 动态
分辨率/粗到细变焦/拥塞控制/可伸缩编码，2026-07-22 deep-search）与
[C-57]–[C-84]（2026-07-22 三方预实施审计 deep-search refute/elevate 增补：
双协议 model_context_window_exceeded、z.ai `[1m]` 后缀窗、openai 图片纵横比
先验、五家溢出错误体实证、LangChain usage-scaling、Cline 反应式先例、
LiteLLM ContextWindowExceededError 映射表、Codex 声明+钳制+比例边距、Spark
AQE 批边界统计先例、UI-Zoomer/AwaRes）均见 `PROPOSAL-context-budget.md`
§3（每条含 URL 与原文数字/引文）；仓库事实引用（file:line）以七路侦察 +
三方审计交付原文为准（F1–F15 机制发现与文档清单 ADD/CORRECT 已折入 §2
V22–V27、§3.3、§3.5、§3.8），关键锚点已内联于 §2 裁决行。
