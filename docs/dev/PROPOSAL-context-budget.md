# 计划书：上下文预算与视觉能力自动推导（spec v1.11 候选）

> **状态：裁决全部闭合 + 三方预实施审计闭合（2026-07-22）**——A1–A5 按推荐
> 执行；A6 被 V17–V21 取代（测量-反应式范式，需求方自提）；A7（反应态溢出
> 终局 × 熔断）按推荐计入连击闭环；三方审计（代码可行性 F1–F15 × 文档清单
> ADD/CORRECT × deep-search [C-57]–[C-84]，§3.8）产出增补裁决 V22–V27，
> 全部折入 SPEC。详见 SPEC §1.1/§2。本计划书由七路并行侦察汇总而成：①
> use_vision 全景（生效点/触点/事实性风险）；② batch_size×window 与全链 13 个
> LLM 调用点普查；③ M9/M1/estimate_run 基础设施现状；④ 业界 deep-search
> （28 条带出处条目 [C-1]–[C-28]）；⑤⑥（2026-07-22 增补，需求方指令"以动态
> 装填为意向方案做深度排查"）estimate/console 链路语义深查 + 可变窗影响面
> 全量清查（结论见 §2.5，V9/V12 依此定为动态装填形态）；⑦（2026-07-22 晚，
> 需求方提出测量-反应式思路后）跨界自适应质量调研（[C-29]–[C-56]，§3.7）；
> ⑧（2026-07-22 晚，三方预实施审计之 deep-search 路）refute/elevate 增补
> （[C-57]–[C-84]，§3.8——含四项强制修订证据）。
> 姊妹文档 `SPEC-context-budget.md` 是进入 spec/*.md 正式修订的最终开发
> 规格（决策编号 V1–V27）。

## 1. 结论先行

三项相互独立但同根的修改，根 = **单次 LLM 调用的内容体积今天没有任何机制保证
装得进模型上下文窗口**：

1. **删除 `segment.use_vision`**，改为按 `segment.llm` 所指 profile 的
   `supports_vision` 能力旗标自动推导（加载期冻结为派生字段
   `SegmentConfig.vision_resolved`，复用 `mode_resolved` 先例）。用户不再二次
   配置；"有视觉能力但 segment 不带图"的省钱形态由**选 profile 即选能力**承载。
2. **引入上下文预算机制**：`[llm.<name>]` 新增 `context_window` 声明，预算公式
   `input_budget = context_window − max_output_tokens − margin`（业界共识形态
   [C-5][C-15][C-17]），代码侧零依赖启发式估算器（中文/ASCII 分系数 + 图片
   按 provider 闭式公式），保证**提供的信息 + 模型回复 ≤ 上下文窗口**。条数型
   参数全部降级为**上限值**，装填**动态**（按各帧/各记录的实际估算逐项装填，
   仍是输入+配置的确定函数——可复现性经审计不破，§2.5）；调用次数以静态
   最坏值 `w_min` 保证**上界**，estimate/console 分母按上界报——与既有分母
   语义（"无掉件上界、排除修复"，§2.5）同族。装不下最小单元的记录以新错误类
   `context_overflow` 走记录级 rejects，**不再喂熔断器**——修复"20 条超大树
   连续超窗就 exit 4 杀全 run"的记录级隔离违约（普查实锤，见 §2.3）。
3. **batch_size 与 window 不合并**（勘察裁定，见 §2.2）：`run.batch_size` 是
   批调度参数（内存生命周期 + QuRating 对比池基数 + stream 装箱容量），从不
   进入 prompt 拼装；`segment.window` 是 segment 唯一的单次 prompt 容量参数。
   两者无任何相乘/组合代码路径，职责在不同层；要做的是把语义写清楚并让
   window 接受预算钳制，而不是合并。

## 2. 现状事实（勘察结论，file:line）

### 2.1 use_vision 的真实形态

- 运行期唯一生效点是 `build_segment_prompt`（`labelkit/operators/segment.py:138-139`）：
  窗内每帧一张图、image Part 紧贴该帧摘要之前；verify 的回收复裁面经
  `judge_window` 同路径继承（`labelkit/operators/verify.py:1046-1052`）。
- text 模态下 `use_vision=true` 静默 no-op（`frame.image is None` 守卫）；
  `strategy="rules"` 时完全不显形。均无专属告警。
- `supports_vision` 是**纯 M1 启动校验旗标，运行期零消费**——M9 对 prompt 里的
  image Part 无条件编码（`labelkit/common/runtime/llm_client.py:258-278/294-315`），
  发不发图完全由算子拼装决定。loader 中唯一依赖 use_vision 的逻辑分支是
  vision 校验集之门（`labelkit/common/config/loader.py:1379-1380`）。
- M1 已有三个"加载期冻结派生值"先例，模式统一为**解析 → 校验 → load() 收尾
  计算 → `dataclasses.replace()` 覆写冻结**：`ConsoleConfig.mode_resolved`
  （loader.py:1903-1952）、`ClassifyConfig.max_labels` 回填（loader.py:1626-1631）、
  `quality.rubric=""` 按模态解析（loader.py:1584-1590）。
- 历史决策考据：S12/S30 只记录了**默认关的理由**（树摘要已含语义、定位为树
  贫瘠补偿开关 [63]、手册成本注记），"显式开关 vs 能力推导"的权衡**从未被
  讨论过**——自动推导不与任何已记录决策冲突。
- 事实性风险（需在 SPEC 中逐条处置）：`examples/config.toml` 两 profile 均
  `supports_vision=true`，自动推导后 `examples/stream` UI 工程翻转为多图调用
  （手册 25/26 章真跑数据须重采）；Anthropic ">20 图 + >2000px" 400 硬拒域
  成为无警告活约束（现有 S28 警告只盖 `annotate.sequence_frames`，
  loader.py:1795-1805）；老配置显式 `use_vision=false`（控成本）者删键后走
  "未知键忽略"警告会**静默翻成多图**——真金成本，须定向报错。

### 2.2 batch_size 与 window：无相乘路径（裁定依据）

- `run.batch_size`（默认 256）：非 stream 下 `islice(stream, batch_size)` 切批
  （`labelkit/orchestration/orchestrator.py:399-413`）；stream 下 next-fit 整会话
  装箱、容量 = batch_size 帧（orchestrator.py:415-469，单会话超容量硬切 + WARN
  + `session_split` duck 标，S21）；QuRating 对比池 = 批内（或类内）active 集合，
  **一次 pairwise 调用只装 2 条记录**（`labelkit/operators/quality.py:313-342`），
  池大小只决定调用次数与百分位归一化秩基数。批处理完 `del batch` 释放——批是
  **内存生命周期单位**。
- `segment.window`（默认 20）：滑窗 = [start, start+window)，步长 window−1、
  重叠 1 帧（`labelkit/operators/segment.py:215-227`）；一窗文本 ≈ window×400 字
  摘要 + (window−1) 条 diff 行 + 固定系统提示 ≈ 9–10k 字符；设计初衷即"有界
  上下文、恒定单请求规模"（spec/314-m14-segment.md:205，S32）——它是全链唯一
  以单次 prompt 容量为目的的条数旋钮。window=20 与 `ui_tree_max_chars=30000`
  两个默认值在 spec 中均无数值推导。
- **全库无 batch_size×window 组合算式；无任何调用点的单 prompt 体积随
  batch_size 增长**（13 调用点逐一核验）。唯一交点是
  `session_max_len > batch_size` 的 M1 WARN（loader.py:1806-1810），比较对象
  还不是 window。

### 2.3 超长风险普查（13 个 LLM 调用点，risk 排序）

| 嫌疑 | 调用点 | 机制 |
|---|---|---|
| ① | quality pairwise UI 对（quality.py:740-757） | 全链最大文本调用点：2×30000 字符树 + 2 图；30000 字符中文树 ≈ 15k–30k token，小窗模型一对即爆 |
| ② | 序列步骤行（quality/annotate/verify 三处） | [动作/步骤序列] 全量渲染**无条数上限**——200 帧 episode 恒 199 行 ≈ 20k 字符 |
| ③ | 单记录树渲染 | classify/annotate/verify/pointwise 各打满 `ui_tree_max_chars=30000` |
| ④ | dedup 语义嵌入（dedup.py:45-61,440） | **完全无截断**：UI 全树 serialize 不传 max_chars；序列 = 200 棵全树拼接 |
| ⑤ | annotate 序列多模态（annotate.py:338-409） | ≤`sequence_frames`=20 张关键帧 + 用户 Schema 全文 + few-shot 无上限 + 30k 摘要块 |

- **拼装后总长检查不存在**（全库确认）；文本模态记录全文、few-shot、类示例、
  用户 Schema 文本、标注结果 JSON、L3 修复原文、generate 种子、embed 输入
  全部无截断旋钮。
- 超窗后果：provider 通常回 400 → `_is_retryable_status` 判不可重试
  （llm_client.py:216-218）→ `ProviderFatalError` → **计入熔断连续致命计数**
  （`labelkit/common/observability/obslog.py:473-482`）：同批连续
  `fatal_error_threshold`（默认 20）条超大记录 = exit 4 杀全 run。若 provider
  以 429/5xx 报超长则先烧完 max_retries=5 次重试。**这违反"记录级隔离"
  非协商约束（spec §2.6）**——预算机制是修约束，不只是优化。

### 2.4 基础设施现状（预算机制的落点）

- usage 已有完整记账链：`Usage(prompt_tokens, completion_tokens)` 四下游
  （LLMResponse / report `llm_usage` / `snapshot()` / trace `llm.call`），
  `price_per_mtok_*` 消费证明下游成熟（llm_client.py:424-435,684-687）。
- `context_window` 字段插入点：`model.py:54`（`max_output_tokens` 旁），loader
  校验模式 `t.get_int(key, default, minimum=1)`（loader.py:418 同型）；
  CONTRACTS.md §6.1 有 LLMProfile 逐字镜像须同步。
- 全部对话调用统一经 `SchemaEngine.complete_validated` → `LLMClient.complete`
  ——**预算终检有天然咽喉点**；prompt 拼装分散在各算子的 `build_*_prompt`
  纯函数（模板冻结于 CONTRACTS §10）——**装填决策天然在算子侧**。
- 共享 helper 落点先例：截断族已住 `labelkit/common/contracts/types.py`
  （`frame_digest`/`UITree.serialize`，多算子复用）；`console_format.py` 是
  "共享单一事实源"归属先例。预算模块自然落位 `labelkit/common/runtime/budget.py`。
- `estimate_run` 估**调用次数不估 token**，`segment_calls = Σ ceil((L−1)/(w−1))`
  依赖配置 window（orchestrator.py:206-209）——凡"预算改变调用次数"的机制若按
  运行时数据裁剪，估算即失真；若钳制是配置可推导的静态值则可共用、零失真。
- 仓库零 tokenizer 依赖；依赖白名单（CLAUDE.md:49）无扩项余地论证前提下，
  启发式估算是唯一路线（论证见 §3 [C-7][C-13]：tiktoken 对 GLM 不给真值）。
- 序列成员摘要块已有"首末恒保留、丢中段整行"的三处同款实现
  （classify.py:92-108 / quality.py:268-285 / annotate.py:131-148）——与业界
  middle-out 裁剪 [C-17] 同向，动态裁剪规则直接沿用该家族语义。

### 2.5 动态装填深查（2026-07-22 增补：estimate/console 现状 + 可变窗影响面）

**estimate/console 链路的现状语义（决定解法形态）：**

- 分母**本来就是文档化的双侧近似**：estimate_run docstring 明文 "All
  estimates assume no drops (upper bound) and exclude retries/repairs"
  （orchestrator.py:151）；stream 下游按 "episodes ≈ sessions, LOWER bound"
  报下界并印固定 stderr 注（orchestrator.py:159-160, 1106-1111）；extract
  是文档化上界（:158）；批数精确（next-fit 模拟）。
- **分子今天已可能越过分母**：llm.call 按逻辑调用粒度发射（重试不加条，
  llm_client.py:993-1004），但 M8 L3 修复每轮独立发一条
  （schema_engine.py:483-491）；verify 修复轮全评审团重判 + 三修复面调用均落
  verify 括号（verify.py:540/574-596/805-852）；classify multi 扇出
  （orchestrator.py:1099-1105 R28 注）；generate 按类 ceil（generate.py:495-512）。
- **console 对双向失真天然容忍**：stage 行是裸文本 `name ▶ a/denom`，无百分
  比、无进度条、无钳制（console.py:890-906）；完成态吸附为 `✓` 数字消失
  （:902-903）；批级进度条钳 1.0（:880-881）；ETA 按 records 维度与调用数
  分母无关（:859-864）。
- **运行中分母修正通道存在**（v1 不用，列演进）：`metrics.run_estimate` 可
  重复调用、渲染器 on_estimate 整体覆写并重绘（obslog.py:391-394 +
  console.py:306-321）；counters 通道每 tick 拉取且新键不泄漏进白名单拼装的
  report（console.py:797-798、orchestrator.py:734-737）。
- 预扫**不碰树内容**（UI 侧纯文件名配对，"zero extra I/O"，
  ingest.py:600-622）——dry-run 无法预演装填，只能报静态界。

**可变窗影响面全量清查结论：**

- 全库以"窗"计数的落点仅两处：`segment_calls`（估算面）与
  `windows_failed`（语义不破坏，取值域变）；`segment.boundary` 事件 payload
  无结构假设（手册 25 章内嵌真跑 trace 样例会变值，属重采面）。report 字段、
  verify 复裁面（直调 judge_window 绕过切窗）、内部 Schema（本按每窗帧数
  参数化）、stitch/extract/dedup/classify（只读 episode）、M10 计量、成段
  流程（剔噪/切段/min_len 在会话级 rel[] 上执行、零窗口信息传入）逐项裁定
  不受影响。
- **确定性审计通过**：frame_digest/tree_diff 是记录内容纯函数（types.py:
  246-389）、segment 全程零 rng（segment.py:260）——按预算装填仍是
  (输入, 配置) 的确定函数，逐字节可复现不破坏；变化的是"窗口边界依赖记录
  内容"这一性质。
- digest 现状在切窗**之后**逐窗现算：接缝帧已双算、贫瘠护栏第三算（写死
  400）——前移到会话级一次计算是净改善；`build_segment_prompt` 冻结签名需
  增 digests 形参（CONTRACTS:2994），`judge_window` 公开签名不动（verify
  复裁零影响）。**重叠 1 帧必须保留**（接缝覆盖契约 segment.py:300-305）。
- 新护栏需求：预算极小时装填可退化到 2 帧/窗——200 帧会话至多 199 窗
  （约默认形态 18 倍调用）；现状无任何 prompt 体积静态拦截（M1 检查全集
  核验），须补 w_min 护栏（CONFIG_ERROR/WARN）。

## 3. 业界调研（2026-07-22 deep-search，28 条带出处）

> 以下 28 条即调研记录本体（每条含来源 URL 与原文数字的浓缩，承重数字全部
> 保留），按 a–h 检索角度分组并标注本方案的采纳映射；未决角度见 §3.6。

### 3.1 窗口大小的获知：注册表 + 用户声明（→ V6）

- [C-1] LiteLLM `model_prices_and_context_window.json`：`max_input_tokens` /
  `max_output_tokens` 字段是事实标准；legacy `max_tokens` 语义漂移（59 条脏
  条目）证明**中心化注册表也会脏**。
  https://docs.litellm.ai/docs/provider_registration/add_model_pricing
- [C-2] LiteLLM `get_max_tokens`/`get_model_info` 程序化查询。
  https://docs.litellm.ai/docs/completion/token_usage
- [C-3] aider：复用 LiteLLM 注册表 + `.aider.model.metadata.json` 用户声明覆盖；
  "Aider never enforces token limits, it only reports token limit errors from
  the API provider"——本方案的调用前保证是更强形态。
  https://aider.chat/docs/config/adv-model-settings.html
- [C-28] OpenRouter models API：`context_length` + `architecture.input_modalities`
  ——API 探测是网关特权而非单机工具常态。
  https://openrouter.ai/docs/guides/overview/models

**采纳**：profile 内显式 `context_window` 声明（用户配置即事实源），不内置
注册表、不做运行时探测。

### 3.2 预算公式与安全边际（→ V7）

- [C-5] LlamaIndex `PromptHelper`：`available_context = context_window −
  num_prompt_tokens − num_output`（负值抛错），`DEFAULT_PADDING = 5`，
  `repack()` 把内容重新装填至最大化利用。
  https://github.com/run-llama/llama_index/blob/main/llama-index-core/llama_index/core/indices/prompt_helper.py
- [C-15] Claude Code auto-compact（反编译）：触发点 = `contextWindow −
  min(maxOutputTokens, 20000) − 13000`——结构 = **预留输出 + 万级固定 buffer**；
  各来源百分比（~83%/~89.4%/~95%）不一，一致的是结构。
  https://gist.github.com/sam-saffron-jarvis/9d8e291c4e696ac7948702d6c4884448
- [C-16] Anthropic Compaction API 默认 200k 窗口在 150,000（75%）触发——一线
  厂商自己不贴 100% 用。
  https://platform.claude.com/docs/en/build-with-claude/compaction
- [C-14] OpenAI：`finish_reason=length` 应显式处理；推理模型建议**预留 ≥25,000**
  tokens。https://developers.openai.com/api/docs/guides/reasoning
- [C-17] OpenRouter middle-out：判定口径 = "输入+补全总 tokens"合并对窗口
  ——与本方案公式一致；≤8,192 窗口端点默认启用压缩。
  https://openrouter.ai/docs/guides/features/message-transforms

**采纳**：`input_budget = context_window − max_output_tokens − margin`，
`margin = max(256, ceil(0.10 × context_window))`（10% 比例与 [C-15] 的 13k/128k
量级吻合，floor 保护小窗模型）。margin 的业界统一常数**不存在**（调研未决项
①），故常数冻结于代码 + spec 记载依据，不开配置面；用户的万能逃生门 = 把
`context_window` 声明得更小。

### 3.3 Token 计数：启发式 + 厂商闭式图片公式，零依赖（→ V8）

- [C-7] tiktoken 只对 OpenAI 自家词表精确（o200k/cl100k…），消息封装开销
  ~3 t/消息 + 3 t/请求——**对 GLM 不给真值**，是"tokenizer 依赖需论证"的核心
  反方论据。https://developers.openai.com/cookbook/examples/how_to_count_tokens_with_tiktoken
- [C-8] OpenAI 官方警告：图片/文件/工具场景 "estimates like characters/4 are
  inaccurate"，官方计数 API 才准。
  https://developers.openai.com/api/docs/guides/token-counting
- [C-9] OpenAI 图片 tile 制：`gpt-4o/4.1` = **85 + 170/tile**（high：缩进
  2048² → 短边 768 → 数 512px tile）；patch 制（4.1-mini 等）= 32px patch ×
  模型系数。https://developers.openai.com/api/docs/guides/images-vision
- [C-10] Anthropic `/v1/messages/count_tokens`：免费、独立限流（2k–8k RPM）。
  https://platform.claude.com/docs/en/build-with-claude/token-counting
- [C-11] Anthropic 图片现行公式：**⌈w/28⌉ × ⌈h/28⌉**，标准档 ≤1568px 且
  ≤**1568** visual tokens 封顶（(w×h)/750 是旧版近似）。
  https://platform.claude.com/docs/en/build-with-claude/vision
- [C-12] Gemini `countTokens` 免费 3000 RPM；图 258 t/768² tile。
  https://ai.google.dev/gemini-api/docs/generate-content/tokens
- [C-13] 智谱 GLM：官方 tokenizer API（`POST /api/paas/v4/tokenizer`，响应含
  `image_tokens`）；**图片无公开闭式公式**；窗口表：GLM-5.2 = **1M/128K**、
  GLM-5 系 = 200K/128K、GLM-4.6V = 128K/32K、GLM-4V-Flash = **16K/1K**；官方
  换算口径 "1 token ≈ **1.5 个中文字符**"。
  https://docs.bigmodel.cn/cn/guide/start/model-overview
- [C-24] chars/4 出处（OpenAI 官方，**英文**文本）。
  https://help.openai.com/en/articles/4936856-what-are-tokens-and-how-to-count-them
- [C-25] 中文密度实测：GLM-5 中文比等义英文 +8.7%、o200k +23.7%、cl100k
  +66.8%；每汉字口径 GLM/Qwen/DeepSeek ≈ 0.67–0.9 t/字，o200k ≈ 0.8–1.0，
  cl100k ≈ 1.25–1.4。https://markhuang.ai/blog/chinese-token-myth
- [C-26] JSON 膨胀：同表格数据 JSON ≈ TSV 2×token；500 行订单 JSON 11,842 t
  vs TOON 4,617 t（−61%）。
  https://david-gilbertson.medium.com/llm-output-formats-why-json-costs-more-than-tsv-ebaf590bd541

**采纳**：估算器 = `ceil(ascii_chars / 3 + cjk_chars × 1.0 + other_chars / 2)`
+ 每消息 4 t 封装 + 结构化输出 schema 文本计入。ASCII 取 /3 而非 /4 是对 JSON
膨胀 [C-26] 的保守化；CJK×1.0 覆盖 GLM/o200k/Qwen（0.67–1.0）——对 cl100k 旧
词表（1.25–1.4）不足，spec 记载局限 + 逃生门（声明更小窗口）。图片（V17 后
修订）= 厂商公式仅作**首批先验种子**（anthropic patch 制 [C-47]；
openai_compatible tile 制 765 @2048px high [C-9]，×1.2 放大），第 2 批起由
V19 在线校准接管——公式准确度只影响首批效率，不影响正确性（§4.2 三层）。

### 3.4 条数上限 + 预算装填的一线先例（→ V9–V11）

- [C-19] Qwen-VL 官方评测口径：`max_frames=768` **且** 总视频 token ≤ 24,576
  双约束；维护者给出 **`max_pixels = 24576×28×28 // num_frames`**——帧数是
  上限、预算守恒。https://github.com/QwenLM/Qwen3-VL/issues/1248
- [C-20] UI-TARS："within the typically constrained token budget (e.g., 32k)…
  limit the input to the last N observations"——截图受预算约束、文本兜底。
  https://arxiv.org/html/2501.12326v1
- [C-21] NeMo sequence packing：以 token 而非条数为守恒量调 batch。
  https://docs.nvidia.com/nemo-framework/user-guide/25.07/sft_peft/packed_sequence.html
- [C-22] vLLM `max_num_batched_tokens`：token 预算是第一约束、条数并列上限
  （serving 侧佐证）。https://docs.vllm.ai/en/stable/configuration/optimization/
- [C-23] NeMo Curator Nemotron-CC 管线：`DocumentJoiner` 把相邻短段拼到
  `max_segment_tokens`（"maximize input utilization"）——数据管线同类算子。
  https://github.com/NVIDIA-NeMo/Curator/blob/main/tutorials/synthetic/nemotron_cc/nemotron_cc_pipelines.py
- [C-4] LangChain `trim_messages`：`'approximate'` 计数器 + 预算 + 首尾保留
  策略。https://reference.langchain.com/python/langchain-core/messages/utils/trim_messages
- [C-18] LangChain map-reduce `token_max=3000` 递归折叠。
- [C-6] Semantic Kernel reducer 的 `threshold_count` 滞回缓冲。

### 3.5 能力旗标自动推导（→ V1）

- [C-27] LiteLLM `litellm.supports_vision(model)`：读注册表布尔旗标决定发不发
  图——能力旗标驱动模态路由的直接先例。
  https://docs.litellm.ai/docs/completion/vision

### 3.6 调研未决项（诚实清单）

① 启发式估算的统一"保守系数"无业界权威数值（只有间接锚点：padding=5 /
13k buffer / ≥25k 预留 / 75% 触发）——系数自定并在 spec 记载依据。
② GLM 图片 token 无官方闭式公式（tokenizer API 是唯一官方途径）。
③ Haystack/DSPy 无显式 prompt 预算机制可考。④ Claude Code 阈值百分比
各来源不一（结构一致）。

### 3.7 跨界自适应质量调研（2026-07-22 晚补充，[C-29]–[C-56]，服务 V17–V21）

**a. 视频流 ABR：**
- [C-29] Apple HLS 阶梯规范：相邻档 1.5–2×、起播档取"多数客户端能承受"、现行参考阶梯 9 档比率 ~1.4–1.6×。https://developer.apple.com/documentation/http-live-streaming/hls-authoring-specification-for-apple-devices
- [C-30] Netflix Per-Title Encode：固定阶梯 → 逐内容实测定制（动画 1080p 只需 1540 kbps vs 一刀切 1750 只够 480p）；"按内容校准阶梯"直接先例。https://netflixtechblog.com/per-title-encode-optimization-7e99442b62a2
- [C-31] BBA（SIGCOMM 2014）：纯缓冲选档，reservoir 90s/cushion 216s；**稳态不需容量估计、启动期必须要**；>50 万用户 A/B rebuffer −10~20%。https://yuba.stanford.edu/~nickm/papers/sigcomm2014-video.pdf
- [C-32] FESTIVE（CoNEXT 2012）：20 样本调和平均 + **p=0.85 安全系数**；升档需档位 k 连续 k 次支持、降档即时（不对称防抖）；**样本不足不切换**。https://conferences.sigcomm.org/co-next/2012/eproceedings/conext/p97.pdf
- [C-33] PANDA（JSAC 2014）：探测式 AIMD（κ=0.14 加性上探、实测低于目标即按差回退）；**死区量化器 Δ_up > Δ_down 迟滞防抖**，不稳定性 −75%。https://arxiv.org/pdf/1305.0510
- [C-34] BOLA（INFOCOM 2016）：Lyapunov 效用 argmax_m (V·υ_m + V·γp − Q)/S_m，**零带宽预测**、距最优 O(1/V)；dash.js 默认家族。https://arxiv.org/abs/1601.06748
- [C-35] MPC/RobustMPC（SIGCOMM 2015）：QoE = 质量 − λ切换 − μ卡顿（μ=3000 量级）；5-chunk 滚动窗；Robust 版用**近 5 样本最大误差贴保守折扣** Ĉ/(1+err)。https://conferences.sigcomm.org/sigcomm/2015/pdf/papers/p325.pdf
- [C-36] Pensieve（SIGCOMM 2017）：RL reward = 质量 − μ·rebuffer − |切换|（μ=4.3/2.66）——防抖惩罚与质量同量纲、失败惩罚 ~4×。https://people.csail.mit.edu/alizadeh/papers/pensieve-sigcomm17.pdf
- [C-37] dash.js DYNAMIC（MMSys 2018）：**启动期吞吐式（90% 安全系数）+ 缓冲 ≥10s 后切稳态缓冲式**，2.6.0 起默认——两段式冷启动的产品化背书。https://dl.acm.org/doi/10.1145/3204949.3204953

**b. 游戏引擎 DRS：**
- [C-38] Unreal r.DynamicRes：帧时间预算 + **10% headroom**（Epic 官方白皮书）；防抖件：最小变更间隔 8 帧、最小幅度 2%、连续 2 帧超预算才紧急降、升档 0.9 摊销（升慢降快）。https://cdn2.unrealengine.com/reducing-fortnites-power-consumption-layout-v03-ffedbeb1adeb.pdf
- [C-39] DOOM 2016：升阈 14.5ms/降阈 15.15ms（**不对称迟滞带**）、分辨率保底 0.83（数值证据强度中）。https://www.digitalfoundry.net/articles/digitalfoundry-2016-doom-tech-interview
- [C-40] Halo 5：X/Y 轴独立连续调节（1152×810–1920×1080），"零超预算帧"第一优先——细粒度阶梯工业可行。https://www.digitalfoundry.net/articles/digitalfoundry-2015-what-works-and-what-doesnt-in-halo-5-tech-analysis
- [C-41] NVIDIA DLSS：命名档 1.5/1.72/2/3×每维 + 每档 Dynamic_Min/Max 连续区间（**双层阶梯**）。https://github.com/NVIDIA/DLSS/blob/main/doc/DLSS_Programming_Guide_Release.pdf
- [C-42] AMD FSR2：档位比例与 NVIDIA 惊人一致（1.5/1.7/2.0/3.0×每维）；**DRS 单次上采样比建议 ≤1.5×**（升档步长上限）。https://gpuopen.com/manuals/fidelityfx_sdk/techniques/super-resolution-temporal/

**c. VLM 动态分辨率：**
- [C-43] Qwen2/2.5-VL：28×28px = 1 token、每图 4–16384 token、官方推荐 min/max_pixels 控预算（256–1280 token 示例）——"分辨率 = token 预算旋钮"主流同构。https://huggingface.co/Qwen/Qwen2-VL-7B-Instruct
- [C-44] LLaVA-NeXT AnyRes：网格阶梯 {2×2,1×N,N×1}；设计动机原文 = **低分辨率加剧幻觉**——③升清重试的因果链证据。https://llava-vl.github.io/blog/2024-01-30-llava-next/
- [C-45] InternVL 动态 tiling：448² tile ×1–12（推理可 40）、**缩略图 + 高清 tile** 组合 = "低清全局 + 高清关键帧"模型侧同构。https://arxiv.org/abs/2404.16821
- [C-46] OpenAI detail 档：low/high/original/auto；per-model 系数各异且随代际漂移（85+170 → 70+140/tile）——**"文档不可依赖"的动机证据**。https://developers.openai.com/api/docs/guides/images-vision
- [C-47] Anthropic 现行公式 ⌈w/28⌉×⌈h/28⌉ 两档封顶（1568px/1568t、2576px/4784t）；官方例：1920×1080 标准档缩至 1456×819=1560t、高清档 2691t；/750 已降级为近似——**公式换代的实证**。https://platform.claude.com/docs/en/build-with-claude/vision
- [C-48] Zoom Eye（EMNLP 2025 Oral）：**置信度分数驱动**图像树递归变焦，训练无关；HR-Bench +15.7~17.7%，8B 超 GPT-4o。https://github.com/om-ai-lab/ZoomEye

**d. 粗到细/按需变焦：**
- [C-49] V*/SEAL（CVPR 2024）：全局不足 → 列缺失目标 → **置信度低于阈值即递归切 patch 搜索**（终止 = 命中或达最小粒度）；V*Bench 上 7B+搜索 75.4% vs GPT-4V 55.0%——③的三件套（触发/定向/终止）学术背书。https://vstar-seal.github.io/
- [C-50] OpenAI o3 "Thinking with images"：思维链内建 crop/zoom/rotate——按需变焦产品化为推理一等公民。https://openai.com/index/thinking-with-images/
- [C-51] OCR DPI 阶梯惯例（ABBYY/Tesseract 官方）：300 dpi 常规、小字 400–600、**600 封顶**（收益递减）；低分辨率输入触发"升 DPI 重扫"提示——数十年工程共识。https://support.abbyy.com/hc/en-us/articles/360002652920
- [C-52] Ferret-UI（Apple, ECCV 2024）：GUI 截图按纵横比切 **2 张子图**独立编码放大细节，超 GPT-4V——GUI 模态"定向升清"的最小充分形态。https://machinelearning.apple.com/research/ferretui-mobile

**e. 拥塞控制（祖师爷模式）：**
- [C-53] TCP AIMD/慢启动（RFC 5681）：报错砍半（ssthresh = FlightSize/2）、无错线性回升、慢启动指数探测——反应式基线规范。https://www.rfc-editor.org/rfc/rfc5681.html
- [C-54] BBR（ACM Queue 2016）：**测量式取代反应式**——windowed-max 带宽 / windowed-min RTT 双参数建模；Startup 增益 2.89 + **连续 3 轮增速 <25% 平台期检测**退出；ProbeBW 8 相增益循环（1.25 上探/0.75 排空）；探测成本 ~2%（200ms/10s）——V19 校准器的完整蓝本。https://dl.acm.org/doi/fullHtml/10.1145/3012426.3022184

**f. 可伸缩编码与注意力分配：**
- [C-55] H.264 SVC（IEEE TCSVT 2007）：时间/空间/质量三维分层，可伸缩性开销 **10–50% 可容忍**——"低清+高清增量"双档表示的心理价位。https://ieeexplore.ieee.org/document/4317636
- [C-56] DirectX VRS/foveated（微软官方规范）：逐 16×16 块指定 1×1–4×4 着色率——"预算花在关键处"的硬件形态，定向区域升清的同构。https://microsoft.github.io/DirectX-Specs/d3d/VariableRateShading.html

**综合观察**（可回溯条目）：①阶梯几何间隔 1.5–2×/档、3–4 语义档 + 档内连续微调的双层结构（[C-29][C-41][C-42]）；②冷启动 = 保守起步 + 首样本立即修正 + **样本不足不切换**（[C-31][C-32][C-37][C-54]）；③降档硬信号一触即发、升档软信号要余量——阈值不对称（[C-32][C-38][C-39][C-53]）；④防抖三件套 = 死区/最小间隔/切换惩罚入目标函数（[C-33][C-38][C-35][C-36]）；⑤校准器 = 窗口化极值滤波 + 0.85–0.9 安全系数 + ~2% 探测预算（[C-32][C-33][C-37][C-54]）；⑥低分辨率 → 幻觉/标注错误的因果链多源独立成立，且**定向**升清优于整帧升清（[C-44][C-48][C-49][C-51][C-52][C-56]）。

**未决**：Unreal 官方文档页不可达（防抖数值为引擎提取件+官方白皮书旁证，强度中）；"按不确定性选分辨率"无生产级服务栈先例（研究侧充分，实现属应用层首创）；GUI agent"重采更高分辨率原图"无先例记录（LabelKit 离线批处理场景原图在手，不受影响）。

### 3.8 三方预实施审计增补（2026-07-22 晚，[C-57]–[C-84]，refute/elevate 路）

**REFUTES（四项强制修订证据）：**
- [C-57] Anthropic stop_reason 词表现含 `model_context_window_exceeded`（"Treat the response as truncated"）；且 Claude 4.5+ 系 **input+max_tokens > cw 时 API 接受请求**、生成触墙即以该值收尾（仅 input 单独超窗才 400 "prompt is too long"）——V11 初稿 {length, max_tokens} 闭集漏接、V20 的 400-oracle 不完备。https://platform.claude.com/docs/en/api/handling-stop-reasons ；https://platform.claude.com/docs/en/build-with-claude/context-windows
- [C-58] z.ai openai 协议 finish_reason 官方枚举 = `stop / tool_calls / length / sensitive / model_context_window_exceeded / network_error`——主力端点自证同款漏接 + 两个专有值。https://docs.z.ai/api-reference/llm/chat-completion.md ；https://docs.bigmodel.cn/cn/api/api-code.md
- [C-59] z.ai anthropic 路由（本仓基线端点）1M 上下文是**模型名后缀选入**（"GLM-5.2[1m] in Claude Code to enable 1M context"；devpack："add the `[1m]` suffix"）——裸 `glm-5.2` 非 1M；Together 版同名模型 256K；窗口是**部署属性非模型属性**。照抄厂商表 1M = 制造本特性要防的系统性溢出。https://z.ai/blog/glm-5.2 ；https://docs.z.ai/devpack/latest-model.md ；https://www.together.ai/models/glm-52
  **【P6 实测修正（2026-07-22，E2E-FINDINGS #16）】**：本条的后缀条件在真端点**不成立**——裸 `glm-5.2` 实测实效窗即 `input+max_tokens ≤ 2^20 = 1,048,576`（12-token 夹逼），`glm-5.2[1m]` 反被 1211 Unknown Model 拒。核心论点（窗口是部署属性、只能实测/欠声明，不能照抄文档）经此**加强**而非削弱；本条按调研记录惯例保留原文。
- [C-60] OpenAI tile 制现行算法（2048² 缩入 → 短边 768 → 数 512px tile → 85+170/tile）按纵横比求值：2048×1152 → 1105 t、945×2048 → **1445 t**——765 是正方形特例，UI 截图纵横比下先验系统性低估 ~36%（V17 下仅伤首批效率，仍须按最坏纵横比取先验）。https://developers.openai.com/api/docs/guides/images-vision

**ELEVATES：**
- [C-62] gpt-5.6 级模型 `detail` 取 original/auto 时**服务端不再隐式降采样**（"uses the original patch count without resizing … resize the image before sending it"）；gpt-5.5/5.6 默认省略即 original——客户端 px 控制（default_image_px/max_image_px）成为该类后端唯一成本闸（强化 V17/V18）。同页：系数持续代漂（gpt-5 = 70+140/tile；patch 制 1536 预算 × 1.62/2.46/1.72 系数）。https://developers.openai.com/api/docs/guides/images-vision
- [C-63] LangChain `count_tokens_approximately(use_usage_metadata_scaling=True)`：用最近含 usage_metadata 的 AI 消息把启发式计数按 `AI_total_tokens / approx_tokens` 比例缩放——主流框架运行时以 usage 校准启发式的直接先例（滤波=单样本、无安全系数；V19 的窗口化 max + 0.85 + 批冻结严格更保守）；亦证文本密度 usage 校准可行（roadmap）。https://reference.langchain.com/python/langchain-core/messages/utils/count_tokens_approximately
- [C-64] Cline 生产级上下文管理**刻意反应式**（"deliberate design choice since accurate token counting varies by model/tokenizer … the first request that exceeds limits will fail"；图片 10K–30K+ t/张且 usage 滞后一拍）——V17 三层范式的产品同构；且实证企业网关存在 `usage: null`（V19 必须定义缺样本兜底）。https://github.com/cline/cline/issues/6055 ；/issues/7383 ；/issues/9433
- [C-65] LiteLLM `ContextWindowExceededError`：400 子类、逐 provider 字符串映射（openai/azure/anthropic/bedrock/together/…），文档明言"enables context window fallbacks"——V20 嗅探-分类-反应的一线同款；其映射表 = pattern 集现成种子库。https://docs.litellm.ai/docs/exception_mapping
- [C-66] UI-Zoomer（2026-04，训练无关）：变焦触发与档位 = 预测不确定性量化问题，置信门控激活——GUI grounding +4.2–13.4%（V21 的 GUI 域直接背书）。arXiv 2604.14113（索引：https://github.com/OSU-NLP-Group/GUI-Agents-Paper-List ）
- [C-67] AwaRes：LLM-as-a-Judge 比对低清 vs 全清输出构造"何时需要高清裁片"信号，推理时工具调用选裁片、低清全局图保留（https://www.arxiv.org/pdf/2603.16932 ）；MEGA-GUI "Conservative Scale Agent" 裁片放大回高清再 grounding（73.18% ScreenSpot-Pro，https://arxiv.org/pdf/2511.13087 ）——判审触发 + 区域裁片升清是领域主流形态（区域升清列演进正确）。
- [C-68] OpenRouter：Responses 皮 `context_length_exceeded` → **转译为成功补全 + finish_reason=length**；chat 皮保留类型化 400；anthropic 皮折叠为 invalid_request_error + 内层 error_type——溢出未必以 400 到达，output_truncated 桶会吸收部分真溢出（归因近似性入 spec 注）。https://openrouter.ai/docs/api-reference/errors

**CONFIRMS（要点）：**
- [C-69] Anthropic vision 现行文档全数核实：⌈w/28⌉×⌈h/28⌉；档表（高清档 Fable 5/Mythos 5/Opus 4.8/4.7/Sonnet 5 = 2576px/4784t，其余 1568px/1568t）；8000×8000 上限；>20 图 ∧ >2000px 拒（invalid_request_error）；官方建议客户端预缩放；100/600 图每请求上限。https://platform.claude.com/docs/en/build-with-claude/vision
- [C-70] bigmodel 窗口表核实：GLM-5.2 = 1M/128K、GLM-4.6V = 128K/32K、GLM-4V-Flash = 16K/1K；glm-5.2 max_tokens 默认 65536/上限 131072——V9 示例数字精确（受 [C-59] 端点告诫约束）。https://docs.bigmodel.cn/cn/guide/start/model-overview ；/concept-param.md
- [C-71] 官方 tokenizer API 双品牌在（`POST /paas/v4/tokenizer`，响应含 image_tokens）；**模型枚举不含 glm-5.2**（旗舰文本计数无官方途径——校准回路 roadmap 的边界条件）。https://docs.bigmodel.cn/api-reference/模型-api/文本分词器.md
- [C-72] GLM 官方换算："1 token ≈ 0.75 英文词或 1.5 中文字符"（0.67 t/字，CJK×1.0 覆盖充分）；官方 FAQ 孤值"GLM-4V 系单图约 **1047** token"（唯一官方 GLM 图片成本数字——PROPOSAL 记载、不进代码，先验按 provider 键控不做模型名嗅探）；官方自指 usage 为真值源（V17 立场同源）。https://docs.bigmodel.cn/cn/guide/start/concept-param.md ；/cn/faq/fee-issues
- [C-73] 中文密度独立实测：o200k ZH 1.06–1.55×（均 1.34），cl100k 均 2.08×；"CJK 典型 1 token/字"——CJK×1.0 对 GLM（0.63–0.67）/o200k（0.8–1.0，生僻字文本贴界）成立、cl100k 缺口如实记载；LangChain anthropic 专项 chars_per_token=3.3（误用 4.0 曾致 16% 低估 → API 拒收）佐证 ASCII/3 保守。https://masonailab.com/en/insights/token-efficiency/ ；https://github.com/langchain-ai/langchain/issues/36318
- [C-74] 双协议字段名/值核实：openai `finish_reason ∈ {stop, length, tool_calls, content_filter, function_call}`；anthropic 非流式 `stop_reason = "max_tokens"`——V11 基础值精确（增值见 [C-57][C-58]）。https://platform.openai.com/docs/api-reference/chat/object ；https://platform.claude.com/docs/en/api/handling-stop-reasons
- [C-75] 溢出错误体实证集（V20 pattern 种子）：OpenAI/Azure 400 `code:"context_length_exceeded"` + "This model's maximum context length is X tokens…"；vLLM 400 `type:BadRequestError`（**数字 code、无该字符串 code**）+ 同消息族；anthropic 400 invalid_request_error "prompt is too long"；OpenRouter `error_type:"context_length_exceeded"`；z.ai 业务码 **1261** = HTTP 400 "Prompt too long"（`{"error":{"code":"1261",…}}`）——只匹 code 会漏 vLLM 与 z.ai。https://community.openai.com/t/error-code-400-max-token-length/716391 ；https://github.com/vllm-project/vllm/pull/4016 ；https://docs.z.ai/api-reference/api-code.md
- [C-76] tiktoken cookbook 现行：3 t/消息 + 1（name）+ 3（回复引导），notebook 与 API usage 实测相符——MSG_OVERHEAD=4 保守成立（anthropic 协议无文档值，margin 吸收）。https://developers.openai.com/cookbook/examples/how_to_count_tokens_with_tiktoken
- [C-77] OpenAI 推理模型"预留 ≥25,000 tokens"现行原文；Responses API 截断以 `status:"incomplete"` + `incomplete_details.reason:"max_output_tokens"` 表达（第三种信号形态，openai_compatible 网关若采 Responses 语义时相关）。https://developers.openai.com/api/docs/guides/reasoning
- [C-78] OpenAI Codex CLI：`model_context_window` 用户声明 + 目录钳制（`context_window.min(max_context_window)`）+ 400K = 272K 输入 + 128K 输出预留 + `effective_context_window_percent = 95` + 90% 触发压缩——声明制 + 输出预留 + 比例边距的完整同构（V6/V7 结构背书）。https://developers.openai.com/codex/config-advanced ；https://github.com/openai/codex/issues/19185
- [C-79] Spark AQE：物化点（stage 边界）= "natural opportunity for reoptimization … statistics on all partitions are available and successive operations have not started"——已完成 stage 的统计只喂未启动 stage 的规划 = V19 批冻结快照的分布式先例。https://www.databricks.com/blog/2020/05/29/adaptive-query-execution-speeding-up-spark-sql-at-runtime.html
- [C-80] Anthropic 错误规范：`{"type":"error","error":{…}}` 封套；413 request_too_large @32MB（多图窗的第二溢出通道——像素制硬限域佐证）；官方明言"catch typed classes rather than string-matching"——A4 脆弱性诊断的厂商自证（V20 维持嗅探=优化门定位）。https://platform.claude.com/docs/en/api/errors

**UNRESOLVED：**
- [C-81] z.ai anthropic 路由的溢出错误体形态（anthropic 封套 vs 平台 1261 封套）与裸 glm-5.2 实效窗**官方无载**（第三方称 ~200K，非权威）——P6 集成实测闭合（SPEC §3.7/§3.9 已列）。
- [C-82] GLM-4.6V/5V 图片 token 闭式公式官方无载（tokenizer API image_tokens 与响应 usage 是唯二官方途径；1047 孤值是否适用 4.6V/5V 未文档化）。
- [C-83] "usage 差分反推每图单价 + 极值滤波"无框架先例（最近邻 = LangChain 整体比例缩放 [C-63]、Cline 整窗反应 [C-64]）——V19 机制属应用层首创（PROPOSAL §3.7 未决预告成立）。
- [C-84] margin 权威通用常数仍不存在（13k 定值 / ≥25k / padding 5 / 75% 触发 / 95%×窗 + 128k 预留 + 90% 触发各自场景化）——A2 冻结决策站在确认的负结果上；10%×1M=100k 级绝对量无先例检验（大窗场景 margin 偏保守，属可接受浪费向）。

## 4. 方案设计

### 4.1 use_vision → vision_resolved（能力推导，M14）

```
vision_resolved = (run.modality == "ui")
                ∧ segment.enabled
                ∧ segment.strategy ∈ {llm, hybrid}
                ∧ llm_profiles[segment.llm].supports_vision
```

- M1 在 load() 收尾计算并以 `dataclasses.replace` 冻结进 `SegmentConfig`
  （`mode_resolved` 同款 parse product）；`build_segment_prompt` 改读
  `seg.vision_resolved`；verify 回收复裁面自动继承。
- loader vision 校验集删除 segment 分支（segment 从"要求视觉"变为"适配
  视觉"，该命题失去可失败性）；存在性/密钥/probe 三处引用集不变。
- `[segment]` 中显式出现 `use_vision` 键 → **定向 CONFIG_ERROR**（迁移文案），
  不走"未知键忽略"前向兼容警告——防止显式 `use_vision=false` 控成本的存量
  配置静默翻成多图（真金成本）。
- 摘要贫瘠护栏 WARN 与手册指引改写为"为 `segment.llm` 配置视觉 profile"。
- 新增 S28 姊妹静态 WARN：`vision_resolved ∧ window > 20 ∧ max_image_px >
  2000` → Anthropic 20 图硬拒域预警（现有 S28 只盖 annotate.sequence_frames）。
- 省钱形态的表达面：**选 profile 即选能力**——把 `segment.llm` 指向纯文本
  profile。spec/manual 明示这是预期用法。

### 4.2 上下文预算（M1 + M9 + 新共享模块 budget.py）

**配置面**（最小增量）：`[llm.<name>].context_window: int = 0`、
`[embedding.<name>].context_window: int = 0`。0 = 未声明 → 该 profile 预算
机制关闭（v1.10 行为原样），被启用阶段引用时 M1 一次性 WARN。声明后：

```
margin(p)       = max(256, ceil(0.10 × p.context_window))
input_budget(p) = p.context_window − p.max_output_tokens − margin(p)   # ≤ 0 → CONFIG_ERROR
```

**估算器**（`labelkit/common/runtime/budget.py`，纯函数、零新依赖）：

```
est_text(s)   = ceil(ascii/3 + cjk×1.0 + other/2)      # [C-24][C-25][C-26] 保守化
est_image(p)  = 校准器读数（V19）；首批先验 = provider 公式常数 × 1.2    # [C-9][C-47]
est_prompt(b) = Σ est_text(part) + Σ est_image + 4×消息数 + est_text(schema)
```

**图片成本：测量-反应式三层（V17–V21，2026-07-22 需求方改向，取代 A6 的
信封/放宽键形态）**——范式 = ABR 的 measure-don't-model（[C-31][C-54]）：

- **先验装填**：图片以 `default_image_px` 工作点采样（新键，0 = 沿用
  max_image_px 即现行为；max_image_px 升格为升级天花板 + 硬限制域）；
  est_image 首批用公式先验 ×1.2，此后读**在线校准器**——每响应
  `(usage.prompt_tokens − est_text)/图数` 喂样本，窗口化 max 滤波 + 0.85
  安全放大（[C-32][C-54]），**快照按批冻结**保确定性，样本 <8 不升档
  （[C-32]），零跨运行持久化（stateless）。文档公式退出正确性路径——
  OpenAI 系数随代际漂移、Anthropic 公式换代（[C-46][C-47]）正是动机证据。
- **溢出反应（trim window, keep resolution）**：识别的 provider 超窗 →
  降级重试：segment 窗对半改切（帧不丢、多花调用）、annotate 关键帧减半、
  其余收紧文本份额；乘性减 + 有界（[C-53]）；仍不下才 reject。错误体嗅探
  在此是**优化门**（漏判 = 走现行 fatal 老路零回归，误判 = 浪费一次有界
  重试）——与 A4 否决的熔断豁免面本质不同。
- **判审升级（trim window, scale up resolution）**：verify fail ∧ repair →
  重标注换档（k 减半、px 上探 1.5×/维 ≤ max_image_px）；单向、每记录 ≤
  max_repair_rounds 次，无振荡面。因果链：低分辨率致幻觉（[C-44]）、置信
  度触发递归变焦（[C-48][C-49]）、OCR 低置信升 DPI 惯例（[C-51]）、
  o3 内建 zoom（[C-50]）；"缩略图保时序 + 高清关键帧保细节"同构
  （[C-45][C-52]）。

**两层执行**：

- **装填层（算子侧，决定内容；动态，2026-07-22 依 §2.5 排查定形）**：
  segment 先算全会话逐帧 digest，再按预算**贪心切窗**（重叠 1 帧与接缝覆盖
  语义保留；`c_i = est_text(digest_i) + diff 最坏常数 + 图片常数`，溢出即
  封窗；window 降级为纯上限，预算未声明时逐字节退化为现行固定窗）。静态
  最坏值只作两用：M1 护栏 `w_min < 2 → CONFIG_ERROR`（保证任意帧放得进
  2 帧窗、运行期装填永不失败；`w_min == 2 → WARN` 窗数放大警示）与
  estimate 上界（下条）。单调用内容裁剪同样**动态**——树渲染上限从固定
  30000 字符改为 min(30000, 预算折算字符)，序列步骤行获得预算上限（沿用
  "首末恒保留、丢中段"家族语义 [C-17]），annotate 关键帧数在
  `sequence_frames` 上限内按图片预算收缩（[C-19][C-20] 同款），generate
  种子数、embed 输入同理。**estimate/console 的解法**：`segment_calls`
  改用 `w_min` 报**上界**（实际每窗 ≥ w_min 帧 ⇒ 实际窗数 ≤ 估算），与
  docstring 既有"upper bound / exclude repairs"语义同族（§2.5）；console
  经排查**零代码改动**（裸文本 stage 行容忍双向失真、完成态 ✓ 吸附、ETA 按
  records）；dry-run stream 注行增补一句上界说明；黄金文件因 examples 无
  预算声明逐字节不动。
- **终检层（M9 咽喉，保证不变式）**：`complete()` 调用前
  `est_prompt + max_output_tokens + margin ≤ context_window` 不满足 → 抛
  `ContextOverflowError`（新 §7.6 错误类 `context_overflow`，记录级、
  **不喂熔断器**、不烧重试）。装填层正确时终检永不触发——它是防御性不变式。

**最小单元语义**：连最小单元（单记录/pairwise 2 记录/2 帧窗）都装不下 →
该记录 `context_overflow` 入 rejects，run 继续。**输出侧**：
`finish_reason=length`（Anthropic `max_tokens` stop）→ 新错误类
`output_truncated`（[C-14]），不再把截断 JSON 送修复循环硬修。

### 4.3 batch_size 与 window：语义澄清（不合并）

两参数保持独立；spec §5 补语义句："`run.batch_size` 决定内存生命周期、
QuRating 池基数与 stream 装箱容量，**从不影响单次 prompt 体积**；单次调用
容量由各算子条数上限 × 上下文预算决定"。`session_max_len > batch_size` WARN
保留。误解防护写入手册第 5/25 章。

## 5. 触点清单（若立项的文件修改面，逐文件详表见 SPEC §3.8）

代码：`model.py`/`loader.py`（M1）、新 `common/runtime/budget.py`、
`llm_client.py`（终检 + ContextOverflowError）、`segment.py`（vision_resolved
+ digest 前移 + 贪心装填器）、`quality.py`/`annotate.py`/`classify.py`/
`verify.py`（动态裁剪）、`generate.py`、`dedup.py`（embed 截断）、
`errors.py`、`orchestrator.py`（estimate_run 用 min_window 报上界 + stream
注行增补）。文档：CONTRACTS.md（LLMProfile 镜像、SegmentConfig、rule 33/34、
§10.9 模板注、build_segment_prompt 签名、窗口机制句、新 budget 契约）、
spec 10+ 个文件、manual 6 章、examples 重跑重采。测试：config 解析/派生/
迁移错误、budget 纯函数、装填器窗界、估算两态、两态 prompt 形状改参数化、
集成侧多图真调用。

## 6. 决策清单（V1–V27，详见 SPEC §2）

V1 能力推导替代显式开关 ｜ V2 移除键定向 CONFIG_ERROR ｜ V3 vision 校验集
删 segment 分支 ｜ V4 贫瘠指引改写 ｜ V5 S28 姊妹 WARN ｜ V6 context_window
声明制（0=关） ｜ V7 预算公式与 margin 常数 ｜ V8 零依赖估算器系数 ｜
V9 动态装填 + 静态最坏护栏（w_min） ｜ V10 最小单元 context_overflow ｜
V11 output_truncated ｜ V12 estimate 报上界、console 零改动 ｜ V13 观测面
（report.budget + report.stream.windows + 启动期预算日志） ｜
V14 batch_size/window 不合并 ｜ V15 embedding 预算 ｜ V16 终检不变式归属 M9 ｜
V17 图片成本测量-反应式三层范式 ｜ V18 default_image_px 工作点 + 阶梯常数 ｜
V19 在线校准器（批冻结快照） ｜ V20 溢出裁帧保清重试 ｜ V21 判审裁帧升清重试 ｜
V22 模板头冻结常数（跨层免除） ｜ V23 M9 载体面（image_px/calibrator/finish） ｜
V24 溢出信号统一（phase + 200 形态 + 熔断矩阵） ｜ V25 三处装填规则钉死
（L3 短路/min-over-panel/不可裁块） ｜ V26 端点实效窗口声明教义 ｜
V27 杂项钉板（分类器分支/原始节探针/detail=original 记载）

## 7. 非目标

- 不引入 tokenizer 依赖（tiktoken 对主力 GLM 不给真值，[C-7]）；不做调用前
  计数 API 预检（网络成本，[C-10] 留作演进）。
- 不做 dry-run 装填预演（预扫不碰树内容，预演需二次全量解析，§2.5——
  dry-run 报 w_min 先验估即可）；不做运行中分母修正（通道已证实存在，列演进）。
- 不改无重叠装填：重叠 1 帧与接缝覆盖语义保留（§2.5）。
- 不做跨运行校准持久化（stateless）；不做修复路径之外的全局升降档（V21
  单向有界，无振荡面）；不做定向区域升清（[C-52][C-56]，列演进）。
- 不改输出侧 num_per_call × 样本长 ≤ max_output_tokens 的输出预算（roadmap）。
- 不合并 batch_size/window；不做跨调用的会话级预算（本工具无会话记忆）。

## 8. 引用

[C-1]–[C-28]、[C-29]–[C-56]、[C-57]–[C-84] 全文（URL + 原文数字/引文）以
2026-07-22 各路调研代理交付原文归档；本文引用处均已内联 URL。调研未决项见
§3.6 与 §3.8 UNRESOLVED（[C-81]–[C-84]）。
