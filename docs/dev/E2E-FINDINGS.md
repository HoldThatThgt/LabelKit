# E2E 测试发现与证据状态

> 本文件只记录可复核证据。已验证事实、权威验收目标、环境失败和待运行项分开书写。
> 当前序列生成以 v1.18 行为规格、v1.19 execution runtime 规格与 `examples/sequence-generation` 为准。

## 证据纪律

- “已验证”必须带命令、输入边界或可检查的产物身份。
- pytest 退出 0 不自动证明目标路径被执行；还要确认 marker 未跳过、真实 entrypoint 被调用、目标工件存在。
- 真实 LLM 证据必须使用真实 endpoint，不替换 HTTP transport、LLM client 或服务端，不使用录制响应。
- 429、5xx、额度耗尽属于环境失败；slot exhaustion 属于产品失败，不能重跑到绿后删除失败记录。
- API key value 只在内存中使用，不写日志、trace、main、stream、report、manifest、failed report 或 assertion repr。
- 尚未运行的证据必须保留 `[PENDING-EVIDENCE:<name>]`，不能用规格期望冒充结果。

## v1.18 闭包看板

| 证据面 | 当前状态 | 已知事实 / 占位 |
|---|---|---|
| 变更前 offline baseline | 已验证 | 2157 tests |
| 当前 offline suite | 已验证 | 2610 passed，47 deselected |
| merged coverage | 已验证 | line 95.71%、branch 91.30%；1548/1548 可执行生产函数已进入 |
| keyless compile / dry-run | 已验证 | 2 sets、8 primary sequences、22 primary events、2 noise events、3 replay events、27 stream rows |
| 500000 record-unit planner probe | 已验证 | 16.889 秒，peak RSS 839221248 bytes |
| retained-content 口径 | 已验证 | 536870912 bytes 是最终 main+stream canonical UTF-8 紧凑核算，不是物理分配 |
| DeepSeek sequence integration | 已验证 | 5 passed in 119.26s，含真实双-noise 话题交付 |
| DeepSeek teaching example | 已验证 | 2 sets、8 sequences、27 stream rows，checker PASS |
| process replay | 已验证 | 27 scanned、9 episodes、2 noise、1 duplicate、8 emitted |
| instruction-only live | 已验证 | 1 sequence、3 events，checker PASS |
| frame-only live | 已验证 | 1 sequence、3 events，sequence annotation 为 null，checker PASS |
| real failure injection | 已验证 | whole-set rollback 与 M8 L3 两条真实用例通过 |
| z.ai structured output | 已验证 | 1 passed in 60.81s |
| 完整真实端点 integration suite | 已验证 | 47 passed in 438.37s，无 skip |
| 52-sequence blind review | 已验证 | 两名评审各 52/52；五类缺陷与系统性缺陷均为 0 |

## v1.19 execution runtime 闭包看板

| 证据面 | 当前状态 | 已知事实 |
|---|---|---|
| v1.18 pre-revision offline | 已验证 | 2610 passed，47 deselected |
| v1.19 offline | 已验证 | 2774 passed，48 deselected，33.78 秒 |
| runtime/resource/ordinary/sequence 对抗窄门 | 已验证 | 917 passed；CircuitBreaker cleanup 后 admission/profile/origin 许可恢复 2/2/2 |
| 六百槽合成门 | 已验证 | 10 passed；running 与 commit-waiting high-water 均为 600；声明序与 capacity 1/600 digest 等价 |
| 六百槽固定候选压力 | 已验证 | 每候选 65536 bytes，candidate bytes high-water 39321600；peak RSS 183468032 bytes |
| 六百槽短提交吞吐 | 已验证 | 受控候选到达率 5396.529/s；commit service rate 33761.210/s；本工作负载未形成持续背压 |
| 本地 Qwen3.5-4B 四槽 E2E | 已验证 | 三次均通过；34 calls、4 attempts、0 rejections；server request high-water 精确为 4 |
| 同 fixture v1.18/v1.19 性能 | 已验证但不宣称加速 | v1.18 median 41.75 秒；v1.19 median 52.38 秒；并发改变 prompt-cache/设备争用形状 |
| DeepSeek sequence | 已验证 | 5 passed in 107.49s，无 skip |
| z.ai structured output | 已验证 | 1 passed in 52.31s，无 skip |
| 完整真实端点 suite | 已验证 | 47 passed、1 skipped in 370.53s；skip 仅因该 shell 未设置本地模型专用 key |
| Uncle Bob mutation review | 已验证 | 33 个独立语义变异全部 killed；survived、invalid、inconclusive 均为 0 |

### 周工程一小时现象的归因

`white-collar-week.report.json` 记录 wall 4720.439 秒，其中 generate 4049.871 秒、annotate 636.202 秒；
365 个 exact sets、1360 个 primary events，无 provider retry 与 attempt rejection。default 与 judge 合计
3540 calls、4815622 prompt tokens、277052 completion tokens。v1.18 的 `_deliver_primary_slots()` 又要求一个槽完成
generation、evaluation、dedup、quality、annotate、verify、CrossView 与 commit 后才启动下一槽。因此退化来自调用量与
token 规模扩大叠加跨槽串行，不是“从一天到七天所以只应乘七”，也不能只归因于 scheduler。

### 同一四槽 fixture 的前后对照

模型固定为 `Qwen3.5-4B-Q6_K.gguf`，SHA-256
`fdedd781c9ce676ab66b018ca247ff78e8a33c98098a822c1e2d5075e7718f66`；llama-server v9200，
`-c 393216 -np 4`。工程固定为 4 sets、12 primary events、1 noise、3 replay events、34 calls、4 attempts、
0 rejections。每组从空 prompt cache 开始连续运行三次。

| checkout | shell wall 三次 | median / range | peak RSS 三次 | server high-water |
|---|---|---|---|---|
| v1.18 `c66816a` | 61.960 / 41.750 / 41.315 秒 | 41.750 / 41.315–61.960 秒 | 153796608 / 153911296 / 154845184 bytes | 1 |
| v1.19 runtime | 50.560 / 58.520 / 52.380 秒 | 52.380 / 50.560–58.520 秒 | 188514304 / 188416000 / 179191808 bytes | 4 |

v1.19 三次报告的总 prompt tokens 为 41635 / 41223 / 41340，completion tokens 为 2220 / 2219 / 2226；
v1.18 为 41247 / 2818 / 1842 与恒定 2213。后两次旧运行因串行 prompt cache 复用显著减少计费 prompt 形状，
所以本对照只证明真实四槽重叠、结构正确和资源观测闭合，不能声称纯 scheduler 加速。单 GPU 上同时执行四个请求还会
争用相同计算资源；部署容量必须按目标 endpoint 的吞吐、cache、429、延迟和内存实测选择。

### 六百槽合成门的使用边界

合成门不替换网络 transport，也没有发起六百个模型请求。它证明 scheduler 可同时接纳六百 leaf、反序完成仍按输入序
返回；sequence 六百候选反序准备仍按零至五百九十九提交，六百 reservation 可共存，primary frontier 与 noise
similarity 的调用数分别随一百、三百、六百线性增长。固定 64 KiB candidate 的缓冲与 RSS 只代表该候选大小；合法
Schema 产生更大 provider response 或 Python 对象时必须重新测量。

### Uncle Bob mutation review

权威范围为 `SPEC-execution-runtime.md` 的冻结执行契约、资源通道与 transport、普通标记工作流、sequence 候选缓冲、
声明序提交、dedup reservation、CrossView 线性化、取消拓扑和验收矩阵。干净已提交树在 detached worktree 的 offline
baseline 通过；每个变异只修改一个生产行为，运行预先声明的窄测试后立即反向恢复，并以 `git diff --exit-code` 与完整
status 证明零残留。结果为 33 个 mapped requirements、0 个 implementation missing、0 个 implementation diverged、
33 killed、0 survived、0 invalid、0 inconclusive。

变异覆盖 profile 独立通道、全域接纳上界、输入序、Context 复制、nested submit、取消许可、CircuitBreaker、profile 与
origin 许可、HTTPX 显式 pool、repair 首轮通道、同名 LLM/embedding、单 runtime 身份、关闭异常优先级、frame-only
annotate、ordinary ProviderFatal、普通 semantic dedup 并发与 first-writer、unused speculative outcome、quality 声明序、
verify 纯叶、generated child、candidate window、昂贵下游跨槽重叠、六百槽 head、reservation 共存、commit-time
revalidate、拒绝优先级、retained 边界、primary/noise 线性检查和最终 full CrossView。所有 detached worktree 均通过
正常 `git worktree remove` 清理；caller checkout 的 HEAD 与完整 status 在清理后复核。

## 已验证的 keyless 计划

命令：

```bash
cd examples/sequence-generation
mkdir -p out
uv run labelkit validate --config config.toml --project project.toml --console plain
uv run labelkit run --config config.toml --project project.toml --dry-run --console plain
```

这条路径在凭据物化、EventLog/输出打开和 attempt 消耗之前编译同一份 GenerationProgram/ScenarioPlan。
精确算术：

| 对象 | 数量 |
|---|---:|
| counterfactual sets | 2 |
| variants per set | 4 |
| primary sequences | 8 |
| primary events | 22 |
| noise events | 2 |
| replay sequences | 1 |
| replay events | 3 |
| stream rows | 27 |

每个 set 是 `3 + 2 + 3 + 3 = 11` 个 primary events，两个 set 共 22；再加 2 noise 与 3 replay，
得到 27。sequence dry-run 不创建或替换 main、stream、success report、manifest 或 failed report。

## 已验证的规模证据

500000 record-unit planner probe 的记录值是 16.889 秒、peak RSS 839221248 bytes。`record_units` 与
`stream_rows` 都有 500000 固定上限，超过时 compile 失败。

`retained_content_bytes = 536870912` 是另一条独立上限。它按最终 main 与 stream 每行 canonical JSONL 的
UTF-8 字节计算，包括重复出现在两个视图中的 payload、annotation、generation truth、replay、元数据与换行。
它不是提前分配 512 MiB，也不能用截断 payload/truth 规避。

## 已验证的真实端点矩阵

### DeepSeek 核心

目标命令：

```bash
uv run --python 3.12 pytest tests/integration/test_sequence_generation_llm.py -q \
  -m 'integration and deepseek'
```

本轮通过以下断言：

- 一个 catalog slot 真实交付四个声明 variants；
- scenario ID 共享，world branch ID 各不相同；
- protected prefix 耦合、patch 重放、state/outcome Schema 与 expected/actual violation 全成立；
- hidden sentinel 不进入 planner/renderer request 或 payload；
- report 的 set/sequence/variant planned = delivered；
- 两个 profile 的 usage calls 与 prompt/completion tokens 都大于零；
- 真实请求模型为 `deepseek-v4-flash`；
- request body 精确携带 `{"thinking": {"type": "disabled"}}`，且不含 tools/tool-choice；
- 两个显式 noise 话题分别到达 renderer 与独立 evaluator，最终四项语义判定均为真；
- endpoint、parser、semantic correctness 和 stability 分别记录。

结果为 5 passed in 119.26s，无 skip。五个用例共用真实 DeepSeek anthropic route 与生产 LLMClient；
覆盖核心交付、显式双-noise 交付、whole-set 失败注入、EventPlan post-validator 的真实 M8 L3 修复以及
请求体/secret 泄漏检查。

### instruction-only

真实 instruction-only 交付 1 条 sequence、3 个 primary events；frame/actor 落闭集、patch 可重放、semantic evaluation
通过，truth 不含 declared pattern、variant 或 expected violation。default profile 为 9 calls、9884 input tokens、
1548 output tokens；judge profile 为 1 call、2839 input tokens、51 output tokens；provider retries 均为 0，
wall time 15.299 秒，checker PASS。

EventPlanRequest 必须显式携带完整 state Schema，让真实 post-validator/L3 能看到合法枚举；declared request 的
该字段固定为 null，由冻结 program 解析权威 Schema。pre-state/base-state 的 L3 violations 只允许
`<kind>:<json-pointer>:<validator-keyword>`，不得泄漏 actual/expected value。

该路径的 delivery digest 为 `407b70e68dd0eb6d55c06eb83f1c2ab004e97da9c3a94a6fc573c366862f7d15`。

### real failure injection

测试只能装饰 production collaborator，不能替换网络组件：

- 首个完整通过 generation、全部 evaluator 和下游的 attempt 在 group commit 前固定拒绝；
- 后续完整 attempt 重跑真实 DeepSeek 并成功；先行自然 rejection 仍保留在报告；
- 两次都观察到完整四 variant；
- 被注入拒绝的完整 attempt 不进入正式 output、dedup index 或 dataset counters；
- failed-attempt schema/usage/retry/trace 累积；
- dataset/item/annotation/token/rows 回滚；
- 最终 report 只合并成功 attempt 的 dataset counters。

另一条注入在 EventPlan production state post-validator 边界触发真实 M8 L3，再由最后成功的冻结
EventExecution 直接进入提交。declared 最后事件先由 M8/StateExecutor 以 outcome Schema 修复：
hidden baseline 机械选择 positive outcome，交付 branch 选择当前 variant outcome；送入 L3 的错误仍只含
value-free outcome-schema pointer/keyword。StateEvaluator 随后独立重放复验，不能共享同一份结论冒充独立证据。

上述两条 failure injection 都在同一真实 DeepSeek 集成命令内通过。测试观察到被拒绝与最终成功 attempt 的完整
variant 集、失败 attempt 隔离、usage/trace 累积、成功计数单次提交，以及 L3 返回的冻结 EventExecution 直接进入提交；
没有替换 transport、LLM client 或服务端。

### z.ai structured output

目标命令：

```bash
uv run --python 3.12 pytest tests/integration/test_sequence_generation_structured_output_llm.py -q \
  -m 'integration and zai'
```

ScenarioSeed、EventPlan、frame 与 SemanticEvaluation 必须都有非空 `LLMResponse.structured` 并通过完整 Schema。
真实 anthropic body 必须恰有一个 frozen tool、强制 tool choice、完整 input Schema 与声明的 thinking 形状；
真实 usage token 必须大于零。

结果为 1 passed in 60.81s，无 skip；ScenarioSeed、EventPlan、frame 与 SemanticEvaluation 均取得非空 structured
载荷并通过完整 Schema，真实 usage token 大于零。

## 已验证的教学 example 与 replay

完整命令：

```bash
cd examples/sequence-generation
set -a
source ../../.env
set +a
uv run labelkit validate --config config.toml --project project.toml --probe --console plain
uv run labelkit run --config config.toml --project project.toml --console plain
uv run python check_output.py
uv run labelkit run --config config.toml --project project-replay.toml --console plain
uv run python check_output.py --replay
uv run labelkit run --config config.toml --project project-instruction-only.toml --console plain
uv run python check_output.py --instruction-only
uv run labelkit run --config config.toml --project project-frame-only.toml --console plain
uv run python check_output.py --frame-only
```

主例 checker 必须从用户可见工件验证 exact counts、variant violations、main/stream 双向对账、hidden sentinel
不泄漏、replay provenance、report/manifest digest。state、patch 与 ActorView 不写训练工件，所以 patch replay
证据必须从集成测试的内存 EventTrace 取得，不能由 checker 假装读取不可见字段。

replay 必须从最终 successful SequenceRows 派生，不从预投影 Record 或独立世界对象复制。M2 在单一 stream 文件内
重算 primary event/owner sequence/replay sequence/replay event ID、ordinal 与 duplicate provenance；payload、
timestamp、role、owner、world branch、source 或事件数任一篡改都 fail closed。

主例在 2026-08-22 最终代码上交付 2 sets、8 sequences、22 primary events、2 noise events、1 条三事件 replay，
共 27 行 stream；四个 variant 各 2 条，所有 rejected-attempt 桶为 0。default profile 为 38 calls、34470 input
tokens、2511 output tokens；judge profile 为 10 calls、9541 input tokens、484 output tokens；provider retries 均为 0，
wall time 44.989 秒。delivery digest 为
`269089200ba4cbe62e41229d3921625341f902179f57cf2e0b95722aa23c8a76`，checker PASS。

同一最终 stream 的 replay 扫描 27 行，组装 9 episodes、吸收 25 个 primary frames、剔除 2 条 noise，exact dedup
命中 1 条 replay tail，最终 emitted 8；default profile 18 calls、1620 input tokens、954 output tokens、retries 0，
wall time 5.217 秒，checker PASS。两条 noise 分别绑定“夜空中的月相观察”和“手工面包出炉时的香气”；
回放 checker 仍精确剔除两条，没有放宽验收条件。

frame-only 真实交付 1 条三帧 sequence；main 的 sequence annotation 为 null，三个 member 的 frame annotation 与
primary stream 逐帧一致并通过完整 Schema。default profile 为 10 calls，judge 为 5 calls，provider retries 均为 0，
wall time 14.374 秒，checker PASS。

## 已验证的 52-sequence blind review

最终发布工件是第八轮真实 DeepSeek declared 运行：13 个 counterfactual sets、52 条序列、143 个 primary events。
main SHA-256 为 `d3247306770068be716aabf3c94c133a74a561b0ac87f4e0c5b8be185fdc250f`，stream SHA-256 为
`2b50be3fe1da94045fb0a372534040a8971a376f618623bfcc6be72655ae11e1`，manifest SHA-256 为
`87e2dd38df308bc19f25d45ea14c7364be2ab2b489c909f07a733ba64ab48851`，report SHA-256 为
`e559b564758bb885d82e4b89dd5842049a1af74fa10218134d3ed41aa53dfb57`，独立重算的 delivery digest 为
`d4582fafe9e975d1da5b6661b529178ad509d7869fd6ecab2d09edf43587b996`。四种 variant 各 13 条，52 个 owner
与 143 行 stream 双向闭合，正式工件不含 hidden sentinel。16 次 sequence slot attempt 中有 3 次 semantic
rejection；它们全部留在报告中，最终 13 个 set 精确交付。

selection seed 为 20260822；盲样本只保留 review key、匿名 group key、timestamp、actor、frame class 与 payload，
不含 variant、expected violation、sequence/scenario id、state、patch 或模型自评。Birch 与 Cedar 各自独立评审
52 条，均为 52 pass / 0 fail；五个缺陷维度全部为零，也没有跨 scenario 的系统性缺陷。明显不真实比例为 0%，
没有需要第三人裁决的分歧，因此本门通过。盲样本 SHA-256 为
`8e2a4915615e372b954f151438d68d04559756f13e071e43ae80c77a49421b5e`，完整 value-free 账本见
`docs/dev/evidence/v1.18-sequence-realism-review.jsonl`。

该运行 default profile 为 270 calls、259595 input tokens、18736 output tokens；judge profile 为 61 calls、
91328 input tokens、3141 output tokens；provider retries 均为 0，wall time 338.422 秒。

为避免只记录最终绿色结果，前序现实性迭代保留如下：

| 运行 | 观察 | 处置 |
|---|---|---|
| 首轮 | 两名评审均为 27/52；13 个 reordered 场景出现终态后重新处理 | blocked 的迟到回执只确认收件，不重开状态 |
| 第二轮 | 两名评审均为 49/52；3 个场景在迟到回执中重复终态 | renderer、Schema 与 evaluator 禁止近义复述终态 |
| 第三轮 | 两名评审为 49/52、48/52；3 个场景把系统发出的补充确认写成系统收到的对象 | 明确 actor 收发关系并机械约束教学帧 |
| 第四轮 | 50/52、52/52；第三人裁定两个 timeout 场景的等待主语生硬 | 约束时间叙述的自然主语关系 |
| 第五轮 | 首个 slot 四次 semantic rejection 后 exhaustion | 诊断发现 evaluator 把自然的等待过程也误判为业务实体，收窄到精确语法关系 |
| 第六轮 | 两名评审均为 51/52；唯一缺陷只在一个 scenario | 继续修正，不把阈值内瑕疵当作最终质量 |
| 第七轮 | 两个独立 scenario 重复终态结论，构成系统性模板缺陷 | 收紧 confirmation Schema、renderer 与 evaluator |
| 第八轮 | 两名评审均为 52/52，五类缺陷全零 | 通过发布门并固化 104 条逐评审记录 |

## 成功提交与失败注入的验收口径

- final `PipelineItem -> SequenceRows` 必须含 inherited classification、quality、sequence/frame annotation 与
  verification；CrossView、retained bytes、delivery digest 与正式文件使用同一最终 bytes。
- primary/noise/replay rows 按最终 artifact timestamp 全局稳定排序；main members 保持 owner 内顺序。
- prospective retained bytes 在 dedup commit 前同时计 source 与它的全部 replay。
- 成功按 main、stream、report 原子替换，manifest last。
- exhaustion 或 pre-commit terminal 保留已有成功四件套，另写 failed report。
- commit-I/O 可留下固定路径混代，但旧 manifest 保持；hash mismatch 必须让消费者拒绝。
- failed report 的 usage 键与成功报告一样是 `llm_usage`；精确键集测试不得接受别名。
- provider fatal/circuit breaker 零 attempt；retryable exhausted 恰消耗一次 attempt。

## 已验证的 MinHash 配置闭包

`uv run --python 3.12 pytest -q tests/common/config/test_config.py tests/cli/test_cli.py` 结果为
401 passed。CLI 的生产 `labelkit validate` 入口测试现在会把 `minhash_threshold = 1.0` 与默认
`minhash_num_perm = 128` 的组合拒绝为 CONFIG_ERROR，stderr 不含 datasketch 异常或 traceback；同一配置层还验证了
`0.99 + 128` 被拒绝、`0.95 + 64` 正常装载。完整离线命令
`uv run --python 3.12 pytest -q -m 'not integration'` 结果为 2610 passed、47 deselected。

## 历史非序列 findings 摘要

下表保留仍有诊断价值的旧 findings；它们不是当前 sequence closure 的替代证据。

| finding | 处置 |
|---|---|
| retryable exhausted 曾未进入 breaker window | 已修复 |
| 全部输入非法曾错误 exit zero | 已修复为 input error |
| 认证失败曾被记录级隔离吞成全灭 | 已修复为首错熔断 |
| trace 曾在启动即截断 | 已修复为惰性打开 |
| JSON repair 可能产生有损但合法内容 | 已加损失启发式与 trace 证据，仍需审慎 |
| temperature zero 的服务端结果仍可漂移 | 服务端非确定性；保持为已知锐边 |
| threshold 与 top-ratio 组合曾被静默忽略 | 已修复配置反馈 |
| explicit judges 曾仍强制 default verify profile | 已修复 |
| pairwise criterion mean 易被误读 | 已增加 tie-rate 观测 |
| circuit-broken report 曾缺少显式标记 | 已修复 |
| trajectory rubric metadata 曾回落错误 | 已修复 |
| rich markup 曾吞掉 `profile[key]` | 已修复 |
| context overflow 的成功响应形状与普通 HTTP 错误不同 | 已由真实 integration 固定解析边界 |
| 并发同 stem 运行可能碰撞固定输出路径 | 仍是使用锐边；生产调度需保证路径唯一 |

## 历史端点观察的使用边界

2026-08-12 曾观察到 DeepSeek anthropic route 的三个行为：未显式禁用时返回 thinking block、不接受 image block、
强制 tool choice 返回 HTTP 400。这些仍只是历史 route 观察。当前 v1.18 DeepSeek 集成已经重新验证 text-only、
structured-output off、thinking disabled 的生产请求体和响应；z.ai 用例独立验证 structured-output on，不能用其中
任一结果替代另一路径。
