# E2E 测试发现与证据状态

> 本文件只记录可复核证据。已验证事实、权威验收目标、环境失败和待运行项分开书写。
> 当前序列生成以 v1.21 交织规格、v1.20 时间完整性规格、sequence redesign、v1.19 execution runtime 规格与
> `examples/sequence-generation` 为准；较早 revision 的结果仅作历史基线。

## 证据纪律

- “已验证”必须带命令、输入边界或可检查的产物身份。
- pytest 退出 0 不自动证明目标路径被执行；还要确认 marker 未跳过、真实 entrypoint 被调用、目标工件存在。
- 真实 LLM 证据必须使用真实 endpoint，不替换 HTTP transport、LLM client 或服务端，不使用录制响应。
- 429、5xx、额度耗尽属于环境失败；slot exhaustion 属于产品失败，不能重跑到绿后删除失败记录。
- API key value 只在内存中使用，不写日志、trace、main、stream、report、manifest、failed report 或 assertion repr。
- 尚未运行的证据必须保留 `[PENDING-EVIDENCE:<name>]`，不能用规格期望冒充结果。

## 2026-09-05 标注后处理钩子证据

权威规格为 `docs/dev/SPEC-annotation-postprocessing.md`。普通记录、序列记录及成员帧共用
模型 Schema → 工程后处理 → 框架时间 → 完整 Schema 的候选定稿边界。代码负责字段不进入模型生成输入，
最终交付保留这些字段；记录级 validator 与 verify 检查处理后的完整结果。

| 证据面 | 当前状态 | 已知事实 / 输入边界 |
|---|---|---|
| 改动前完整离线基线 | 已验证 | `3044 passed, 48 deselected in 623.44s`；基线 commit `9f88620` |
| 配置、投影与函数边界 | 已验证 | 761 passed；43/43 修改函数进入，覆盖未知类引用聚合、非法 Schema 聚合和全局完整 Schema 深冻结 |
| 标注与 stream verify | 已验证 | 230 passed；27/27 修改函数进入，覆盖真实 raw、副本隔离、自洽选择、时间顺序及修复错误传播 |
| SchemaEngine 隔离 | 已验证 | inference 套件 397 passed；新增 5 个定稿顺序、L3 调用次数、validator 深复制和程序错误身份用例 |
| 工程示例离线门 | 已验证 | 12 passed；含多实体、分隔符、缺失上下文、歧义、伪造位置/长度的拒绝，以及两个工程的冻结计划 |
| 真实本地 4B | 已验证 | `1 passed in 27.51s`；实际调用生产 `execute_run`，两次普通标注和一次带帧、replay 的 sequence 交付全部通过独立检查器 |
| 完整离线与覆盖门 | 已验证 | 隔离测试修复后的完整门为 `3239 passed, 49 deselected, 2 warnings in 702.15s`；79/79 修改函数进入；17 个修改生产文件最低行覆盖 90.05%、最低分支覆盖 77.50% |
| Uncle Bob mutation review | 已验证 | 干净提交 `0a3ccfe` 上的 191 个不同源码变异经 192 次预声明执行全部 killed；无 survived、invalid、inconclusive 或 blocked；首轮及环境准备失败记录完整保留 |
| 外部端点发布门 | 本轮未运行 | `[PENDING-EVIDENCE:postprocessing-deepseek]`、`[PENDING-EVIDENCE:postprocessing-zai]`；用户指定的本轮特性真实验收为本地 4B |

### 本地模型身份与可复现命令

本轮实测模型为 `/Users/atishoo/models/Qwen3.5-4B-GGUF/Qwen3.5-4B-Q6_K.gguf`，SHA-256 为
`fdedd781c9ce676ab66b018ca247ff78e8a33c98098a822c1e2d5075e7718f66`。实际服务版本为 llama-server
0.3.0，build 10621，commit `c1d0e7a00`。独立端口 18081 的服务启动命令为：

```bash
/opt/homebrew/bin/llama-server \
  -m /Users/atishoo/models/Qwen3.5-4B-GGUF/Qwen3.5-4B-Q6_K.gguf \
  -c 393216 -np 4 -b 2048 -ub 512 -t 6 -tb 6 \
  -ngl all -fa on --fit off -rea off \
  --host 127.0.0.1 --port 18081 --metrics --no-webui
```

两个工程使用 `examples/annotation-postprocessing/config-local-4b.toml`，实际 profile 为 context window 98304、
max concurrency 2、max output tokens 2048、thinking disabled。先运行两个工程的 `validate` 和 `dry-run`，
普通工程计划 2 次调用；sequence 工程为 1 primary sequence、3 primary events、3 replay events、0 noise，
预计 11 次调用。随后以非秘密的本地测试凭据设置 `LABELKIT_LOCAL_KEY`，执行：

```bash
uv run --python 3.12 pytest tests/integration/test_postprocessing_local_llm.py \
  -q -s -m 'integration and local_llm' \
  --basetemp /tmp/labelkit-postprocessor-20260905-RMOspE/live-test-final
```

| 实际运行 | 产物与独立检查 | LLM usage | wall time |
|---|---|---|---|
| 普通标注首轮 | 2 rows；多实体及分隔符实体的规范值、原文切片、Unicode 位置和数量均正确 | default：2 calls、8 prompt tokens、46 completion tokens | 2.268 秒 |
| 普通标注再次运行 | 同一输入与独立实体 oracle；确定性字段关系再次正确 | default：2 calls、8 prompt tokens、46 completion tokens | 0.970 秒 |
| sequence 交付 | 1 main、6 stream；main/member/primary/replay 的完整标注一致，所有 manifest 文件 hash 与 delivery digest 检查通过 | default：10 calls、6740 prompt tokens、534 completion tokens；judge：1 call、2055 prompt tokens、45 completion tokens；全部零 retry | 22.154 秒 |

普通标注中模型负责抽取车牌值，工程函数规范化大小写与分隔符并计算 `start`、`end`、`entity_count`。
独立 oracle 检查 `粤B12345`、`京A12345` 的原文区间分别为 `[6,14)`、`[15,22)`，`沪C88888` 为 `[6,14)`。
sequence 的模型负责摘要及帧的请求身份、状态，工程函数计算 `summary_length`、`request_id_length` 和
`utterance_length`；检查器从最终业务字段与真实成员 payload 重新计算。函数不读取独立 oracle 文件。

最终 sequence delivery digest 为
`19cc31b578c2bb542910cbb2f6e3e554d7d3d4956fb869d7a62399e7034778eb`。
真实请求旁路观察只保存“模型 Schema/提示词是否含代码字段”的布尔值，观察到 ordinary 4、sequence 1、frame 3
个标注请求；未替换 transport、LLM client 或服务端。全部模型调用共 15 次。以上时间只证明本轮可运行，
不作为吞吐或加速结论。验证结束已关闭本轮启动的 18081 服务，用户原有 8080 服务未停止或替换。

### 失败证据与修复边界

首个真实 sequence 门因示例函数要求 utterance 内一定出现请求编号而失败：真实模型在两个成员的自然语言中
未重复该编号，但 payload 与帧标注中的请求身份、状态一致。失败正确传播为 `PostprocessorError`、退出码 4、
零 slot attempt，并写入 `generation_downstream_contract` failed report；未把程序错误回喂模型。
示例已改为从实际语义结果与 raw 计算有业务含义的长度，普通记录仍验证真实实体位置；最终完整真实门重新通过。
脱敏诊断原件为
`/private/tmp/labelkit-postprocessing-diag-2on1qjxa/examples/annotation-postprocessing/diagnostic.jsonl`，
同目录 failed report 保留零 attempt 与终态错误分类；诊断只记录字段存在性、相等性和位置匹配数量。

独立配置复核保留了 8 个失败用例的原始日志：未知类引用不能漏检、非法完整 Schema 不能逃逸错误聚合、
全局完整 Schema 必须深冻结。修复后三组回归全部通过。此前完整离线门的类型载体/摘要金值及示例迭代失败
也保留，不能使用该红色基线作为变异测试的分母。

本轮原始日志、最终真实产物及覆盖数据保存于 `/tmp/labelkit-postprocessor-20260905-RMOspE/`；
最终真实日志为 `local-4b-e2e-final.log`，最终工件位于
`live-test-final/test_real_local_postprocessing0/examples/annotation-postprocessing/out/`。
完整离线命令为 `uv run --python 3.12 pytest -q -m 'not integration' --cov=labelkit --cov-branch`，
此前补齐独立复核断言的日志为 `acceptance-offline.log`，覆盖 JSON 为 `coverage-acceptance.json`，
逐修改函数及文件证据为 `acceptance-production-coverage.json`。比较 `9f88620` 与本轮 AST 得到 79 个新增或
修改函数；全部进入。此前已通过的 3211 用例完整运行及其覆盖日志也保留，没有覆盖失败或旧门证据。

### 首轮语义变异与测试补强

用户已授权本地提交、隔离 worktree 中的临时生产语义变异及必要修复提交，不推送。首轮审查基线为
`2b62fae49f54290bc068fc1e9f289c71a21f9e64`，三个审查分区的 worktree 均已恢复零 diff 并移除，
caller 的 HEAD 和完整 status 保持不变。

| 审查范围 | Killed | Survived | Invalid | Inconclusive | 其他未计分证据 |
|---|---:|---:|---:|---:|---|
| 配置、投影与工程函数 | 109 | 12 | 1 | 1 | 无 |
| 结构引擎、标注与 verify | 38 | 3 | 0 | 0 | 无 |
| 序列冻结、交付与工程示例 | 25 | 3 | 0 | 0 | 回放 retained bytes 的原预声明宽基线失败 |

Survived 表示预声明局部 oracle 没有检测该变异，不能据此推断完整测试库也无法检测。首次缓存隔离不足的
试跑已撤销计分；所有上述正式结果均使用逐次独立的 `PYTHONPYCACHEPREFIX`，并验证生产导入来自隔离
worktree。模块导入次数没有规范依据，对应变异列为 invalid，不计入有效变异。

凭据测试曾在 pytest 格式化失败时继续拦截全局环境读取，造成 inconclusive；修复为只在实际配置加载的
作用域内拦截。原 sequence 宽基线依赖 checkout 绝对路径的摘要金值，保留 `1 failed, 377 passed`，
不通过 deselect 缩小分母；现复用既有稳定路径 fixture，原宽基线已达到 `379 passed`。

| 补强范围 | 新增的可观察断言 |
|---|---|
| 返回、冻结与调用隔离 | 返回完整字典替换候选；引用与 callable、类完整 Schema 不可变；缺失 raw 保持 None；sys.path 不变；每个候选恰调用一次 |
| 模型投影与静态预算 | 普通 required 保留；递归数组和 object default 投影；完整 few-shot 值约束；类与帧使用各自模型 Schema 计价 |
| 标注与 verify | 帧提示词、response Schema 和预算一致；无 hook 帧仍不运行记录 validator 或累计 resolved_at；verify 评审不重复调用 hook |
| 交付与资源计数 | manifest report hash 独立复算；非空 main/stream 摘要及 main 等长变化敏感性；冻结完整帧 Schema 绑定；真实候选装配计入非空 replay 并在超限时回滚 |

首轮完整报告与逐条 patch、命令、因果日志保留于以下本地目录：

- `/tmp/labelkit-bob-config-20260905.1T2vSl/`
- `/tmp/labelkit-annotation-postprocessing-bob.cp6mRB-results/`；报告为
  `/tmp/labelkit-annotation-postprocessing-bob.cp6mRB-report.md`。
- `/tmp/labelkit-bob-postprocess-delivery.wgi3bm/`

测试补强工作区和完整回归证据为 `/tmp/labelkit-postprocessor-hardening.jZBxxM/`。本次补强没有修改生产代码
或实际工程示例函数，真实本地 4B 证据仍对应相同的实现语义。最终复审结论记录如下。

补强后的首次隔离完整门保留 `4 failed, 3234 passed, 49 deselected in 670.77s`，日志为上述目录的
`full-offline.log`。其中两个大规模规划金值仍依赖 checkout 绝对路径，两个示例测试依赖未跟踪的 out 目录。
示例测试现明确通过 CLI 覆盖把输出指向 tmp_path，窄门为 `12 passed in 1.83s`。规模测试复用并扩展既有
固定路径 fixture；两个不同绝对根的 declared 和 instruction-only program 在处理后具有相同的独立语义
摘要。固定向量与双根检查为 `2 passed in 0.91s`，实际两个规模用例为 `2 passed in 474.17s`，保留固定摘要、
记录规模、solver 调用数和资源约束；600 分支的最终 plan 另以独立材料及 domain SHA-256 复算。
相关证据为 `path-normalization-input-evidence.log`、`path-stable-scale-final.log` 和
`generation-import-proof.log`。新完整门在这些修复后重新执行，旧失败日志保留。

隔离测试修复后的最终完整门为 `3239 passed, 49 deselected, 2 warnings in 702.15s`。
日志为 `full-offline-green.log`，分支覆盖为 `coverage-hardening-green.json`，逐文件与修改函数证据为
`final-production-coverage.json`；均位于 `/tmp/labelkit-postprocessor-hardening.jZBxxM/`。
使用 caller 的 Python 3.12 环境、显式 hardening `PYTHONPATH` 和本次专属的 `PYTHONPYCACHEPREFIX`，
执行 `python -m pytest -q -m 'not integration' --cov=labelkit --cov-branch`，没有缩小完整离线门。
79/79 修改生产函数进入，17 个修改生产文件最低行覆盖为 90.05%、最低分支覆盖为 77.50%。
同一批补强测试通过 Ruff 0.16.6 的 E4/E7/E9/F 检查，目标版本为 Python 3.12。

### 最终语义变异复审

复审使用干净提交 `0a3ccfe86359f8eb210983c11162fa0feeb6716d`。配置、运行期和交付各自在独立 detached
worktree 中执行，生产导入路径指向对应 worktree；每次运行使用独立 `PYTHONPYCACHEPREFIX`。
每次变异只临时修改生产源码，运行原预声明 oracle 后立即恢复，并检查 HEAD、staged/unstaged diff
和完整 status；没有修改测试、删减命令、录制模型响应或保留源码变异。

| 审查范围 | 不同源码变异 | 执行次数 | 因果复核结果 | 绿色基线 |
|---|---:|---:|---|---|
| 配置、投影与工程函数 | 121 | 122 | 全部 killed | 176 passed in 2.01s，零 skip |
| 结构引擎、标注与 verify | 41 | 41 | 全部 killed | 预检 361 passed in 1.36s；恢复后 361 passed in 0.77s |
| 序列冻结、交付与工程示例 | 29 | 29 | 全部 killed | PROGRAM 4、WORKFLOW 15、EMITTER 88、EXAMPLE 12；原完整 workflow + project 为 380 passed in 31.33s |

合计 191 个不同生产源码变异、192 次预声明执行；无 survived、invalid、inconclusive 或 blocked。
配置分区的原凭据双测试与单测试使用相同源码补丁，保留两组 oracle 分别重跑，不能把它们算成两个不同变异。
首轮结果按执行分类统计，18 处局部测试缺口均已闭合；首轮 invalid 仍独立保留历史。
该结果证明预声明语义变异能被相应测试检测，不声明所有可能程序错误都已枚举。

交付分区的两次环境准备失败均为零变异：首次导入证明发现 caller cwd 抢先解析包；另一次旧 sequence
示例缺少文档要求的空 out 目录，使 PROGRAM fixture 失败。两次均保留日志并清理工作区，未计入变异结果；
修正 scratch runner 后从同一干净提交重新执行全部五组基线。原完整命令为：

```bash
python -m pytest -p no:cacheprovider -q \
  tests/orchestration/test_sequence_workflow.py tests/operators/generation/test_project.py
```

该命令没有 `-k`、deselect 或 skip；其 380 用例绿色基线建立后，才用同一完整命令复核 replay retained
变异。report 最终文件 hash、非空 main/stream 独立摘要、冻结完整帧 Schema 和真实 replay 容量门均给出
明确因果失败。运行期原帧预算、无 hook 帧待遇及 verify 重复调用三处缺口同样全部闭合。

| 完整报告 | 逐项与恢复证据 |
|---|---|
| [配置与钩子报告](/tmp/labelkit-bob-config-20260905.1T2vSl/round-two/bob-report.md) | 同目录 `execution.vM1kut/classified-ledger.json`、`execution-integrity.json`、`restoration.json` |
| [运行期报告](/tmp/labelkit-postprocessor-bob-round2-run.52jVoY/report.md) | 同目录 `results/` 的 patch、日志、JUnit、result 与 restore；`cleanup-proof.log` |
| [交付报告](/tmp/labelkit-bob-postprocess-delivery-rerun.VKhimw/bob-report.md) | 同目录 `classified-results.tsv`、`results.tsv`、`restoration.log` 与原始逐项日志 |

所有正式及环境准备审查 worktree 均已不使用 force 地移除，路径和 Git 注册不存在。最后的 caller 证明
保持上述提交及 `codex/annotation-postprocessing` 分支，staged/unstaged diff 与完整 status 为空。
复审后只整理本规格、文件清单与验收文档；生产代码和测试保持通过完整回归及审查的同一内容。全部提交仅在本地，未推送。

## 2026-09-04 并发缺口修复证据

本轮只增加三处已有依赖边允许的并发，不改变普通批屏障或同一状态链：declared baseline 后的 sibling
counterfactual suffix、stitch 不同会话的当前候选 wave，以及 `validate --probe` 的 profile/密钥探测。

| 证据面 | 当前状态 | 已知事实 / 输入边界 |
|---|---|---|
| 定向回归 | 已验证 | 338 passed；覆盖 scenario、stitch、LLM probe 与 Application probe；新增 18 个对抗用例全部通过 |
| 同时在途屏障 | 已验证 | suffix 三任务、stitch 两会话 pass-1/repass、三个 profile 与三把 pool key 均须同时到达同步屏障；串行降级不能靠 on_error 掩盖 |
| 顺序与失败语义 | 已验证 | baseline 未验收时 suffix 零调用；variant/session/profile/key 按声明序归并；主动/外部取消等待 sibling cleanup 并恢复原始 CancelledError；stitch SchemaViolation 单票弃权且严格多数仍以原始 n 为分母 |
| changed production coverage | 已验证 | 19/19 个修改函数进入；4 个修改文件最低 line 90.58%、最低 branch 81.00% |
| 完整 offline suite | 已验证 | `3026 passed, 48 deselected in 728.03s`；同一次运行生成 branch coverage，未缩小门面 |
| keyless 真实入口 | 已验证 | sequence 主例 `validate` 通过；`dry-run` 为 2 sets、8 sequences、22 primary events、27 stream rows，零 LLM 调用且零输出写入 |
| Uncle Bob mutation review | 已验证 | probe/stitch 在 `a79edfe`、sequence 在测试加固提交 `42ad20d` 的 detached worktree 审查；31 个有效语义变异全部 killed，survived、invalid、inconclusive 均为 0 |
| 真实 endpoint 吞吐 | 待运行 | `[PENDING-EVIDENCE:concurrency-real-throughput]`；未复用历史四槽数据声称本轮加速 |

### 并发修复 Uncle Bob 台账

三个分片分别先跑干净 baseline：probe 两文件 `219 passed`、scenario `62 passed`、stitch `57 passed`。每个
mutation 只修改生产源码，运行预先声明的窄测试后立即用反向 patch 恢复，并以 `git diff --exit-code` 与
porcelain status 空证明零残留。首轮 scenario 的 `fatal-cleanup-before-propagation` survived：旧测试在
`asyncio.run()` 退出后才断言，事件循环 teardown 会替错误实现清理遗留任务。`42ad20d` 把断言移入同一运行中
event loop，并补同 branch state/history 串行 oracle；重审后该变异与新增 branch 并发变异均 killed。

| probe 变异身份 | 预声明杀手测试 | 结果 |
|---|---|---|
| `pool-key-serial` | pool key overlap barrier | killed：首任务内部 timeout |
| `pool-key-reverse-settlement` | pool key declaration-order result | killed：结果变为 C/B/A |
| `probe-constructor-escape` | constructor failures become ordered results | killed：泄漏 constructor RuntimeError |
| `pool-key-active-cancel-bypass` | direct/self child cancellation cleanup | killed：两个分支均由测试内 timeout 失败 |
| `pool-key-external-cancel-misclassify` | external cancellation identity | killed：取消参数丢失 |
| `profile-serial` | referenced-profile overlap barrier | killed：首 profile 内部 timeout |
| `profile-reverse-settlement` | referenced-profile declaration order | killed：embedding 结果错误居首 |
| `profile-active-cancel-bypass` | direct/self profile cancellation cleanup | killed：两个分支均由测试内 timeout 失败 |
| `profile-external-cancel-misclassify` | external profile cancellation identity | killed：取消参数丢失 |
| `profile-failure-swallow` | primary probe failure priority | killed：主异常被吞、close failure 错误升级 |
| `probe-cancel-skip-close` | external cancellation closes root once | killed：close 次数为 0 |

| sequence 变异身份 | 预声明杀手测试 | 结果 |
|---|---|---|
| `suffix-serial` | suffix overlap barrier | killed：首 suffix 内部 timeout |
| `baseline-validation-late` | baseline rejection starts zero suffix calls | killed：观察到 3 次 suffix 调用 |
| `variant-results-reversed` | exact high-level variant order | killed：首结果不再是 positive |
| `recoverable-select-last` | recoverable declaration priority | killed：选择末位 state_transition |
| `fatal-exception-group-leak` | fatal original-exception identity | killed：泄漏 ExceptionGroup |
| `suffix-active-cancel-bypass` | direct/self suffix cancellation cleanup | killed：两个分支均由测试内 timeout 失败 |
| `suffix-external-cancel-misclassify` | external suffix cancellation identity | killed：取消参数丢失 |
| `fatal-cleanup-before-propagation` | same-loop fatal cleanup assertion | killed：异常传播时 cleaned 仅为 1/3 |
| `branch-events-parallel` | prior state/history visibility | killed：第二事件早于首事件 render 进入 |

| stitch 变异身份 | 预声明杀手测试 | 结果 |
|---|---|---|
| `pass-one-sessions-serial` | pass-one multi-session overlap | killed：high-water 降为 1 |
| `sessions-reverse-settlement` | wave judge event order | killed：事件顺序变为 b/a |
| `wave-split-requests` | one ordered TaskGroupRequest per wave | killed：出现多个 request |
| `repass-sessions-serial` | repass multi-session overlap | killed：high-water 降为 1 |
| `provider-failure-escalation` | runtime fatal boundary and cross-session isolation | killed：ProviderFatalError 逃逸为 runtime fatal |
| `schema-violation-as-failure` | one abstention plus valid majority | killed：首张违规票直接抛出 |
| `abstention-shrinks-denominator` | two abstentions plus one valid vote | killed：单张合法票错误胜出 |
| `all-violations-select-first` | all violations raise last declaration | killed：错误抛出第一张违规票 |
| `vote-completion-order` | reverse-completion first-sample identity | killed：返回声明序末位样本 |
| `leaf-nested-submit-after-success` | wave owns one request | killed：出现额外嵌套 request |
| `leaf-nested-submit-before-normalization` | runtime rejects nested leaf submission | killed：触发 nested-task-group InternalError |

最终合计为 31 killed、0 survived、0 invalid、0 inconclusive，implementation missing 与 implementation diverged
均为 0。审查未修改测试或文档；probe、scenario、stitch worktree 最终均回到各自 baseline commit 且 status 为空。

## 2026-09-04 vLLM extra_body 证据

`[llm.<name>.extra_body]` 仅为 OpenAI-compatible chat-completions profile 提供 JSON 顶层扩展字段。配置装载必须拒绝
Anthropic、非 JSON 值与 LabelKit 保留键冲突；缺省空表必须维持既有请求体，显式扩展必须在普通调用与 probe 共用的
请求构造器中平铺，不能发送 `extra_body` 包裹字段。

| 证据面 | 当前状态 | 已知事实 / 输入边界 |
|---|---|---|
| 配置与请求体离线回归 | 已验证 | 配置与 LLM client 定向套件 551 passed；完整 offline suite 3044 passed、48 deselected，用时 653.83 秒 |
| changed production coverage | 已验证 | 5/5 个修改函数进入；3 个修改文件最低 line 92.61%、最低 branch 91.74% |
| Uncle Bob mutation review | 已验证 | 修复提交 `c414c9a` 的 detached worktree 复审：23 killed、0 survived、0 invalid、0 inconclusive；implementation missing 与 implementation diverged 均为 0 |
| 真实 vLLM endpoint | 待运行 | `[PENDING-EVIDENCE:vllm-extra-body-real-endpoint]`；未用纯函数请求体断言冒充服务端接收证据 |

首轮 survived 分别允许 `_complete_spec` 在调用共享 builder 前清空 `extra_body`，以及允许第二次和后续 retry
删除 `top_k`。前者现在由带非空扩展字段的 `_complete_spec(...).build_body()` 断言承保；后者连续进入 attempt 0 与
attempt 1 的 `_dispatch_attempt` body handoff，并在 origin admission 前以资源边界哨兵停止，零网络且不伪造 HTTP
server/transport。上述修复已通过 2 条窄回归、551 条定向回归与完整 offline suite。复审重新执行全部 23 个
语义变异；原 M22 与 M23 分别由 `_complete_spec` 精确断言和第二次 attempt handoff 断言 killed，其余 21 个
变异保持 killed，最终无 survived、invalid 或 inconclusive。审查前后 detached worktree 与调用方工作树均为 clean。

## v1.21 序列交织证据看板

本节记录 2026-08-28 当前 checkout 的权重选择、partner pool、真实交织布局、报告、规模与本地真实模型证据。
DeepSeek 与 z.ai 的历史结果不冒充本轮回归；本轮未运行时分别保留
`[PENDING-EVIDENCE:v1.21-deepseek]` 与 `[PENDING-EVIDENCE:v1.21-zai]`。

| 证据面 | 当前状态 | 已知事实 / 输入边界 |
|---|---|---|
| interleaving 特性回归 | 已验证 | 最终定向门 855 passed，覆盖配置、carrier、program、plan、project、planner、workflow 与 CLI |
| changed production coverage | 已验证 | 1514 passed、1 deselected；55/55 个修改函数进入；11 个修改文件最低 line 87.34%、最低 branch 81.38% |
| 完整 offline suite | 已验证 | `3002 passed, 48 deselected in 575.62s`；shell wall 576.11 秒，peak RSS 991674368 bytes，0 swap |
| 50 万 record-unit planner 门 | 已验证 | 100000 条四事件 sequence、400000 个事件；1 passed、12 deselected，21.81 秒；shell wall 22.38 秒，peak RSS 919453696 bytes，0 swap |
| 六百分支规模门 | 已验证 | 600 positive branches、300 forced pairs；1 passed，65 deselected，446.83 秒；shell wall 447.46 秒，peak RSS 219054080 bytes，0 swap |
| 主例 keyless 门 | 已验证 | 2 sets、8 sequences、22 primary events、0 opportunity、8 primary sessions、27 stream rows |
| 四槽 keyless 门 | 已验证 | 4 sets、4 sequences、12 primary events、2 opportunities、2 interleaved sessions、16 stream rows |
| 本地 Qwen3.5-4B 集成 checker | 已验证 | 修正最终 row 断言后 1 passed in 82.23 秒；server request high-water 精确为 4，两个 profile usage 均非零 |
| 本地四槽持久化工件 | 已验证 | 4 main、16 stream、2 个双 owner 真交织 session；manifest-last 与 report 原子提交 |
| Uncle Bob mutation review | 已验证 | 40 个有效语义变异全部 killed；2 个删除重复约束的等价变异为 invalid；survived 与 inconclusive 均为 0 |

Uncle Bob review 在干净提交 `f5d828a` 的 detached worktree 上分组执行。配置闭包与 program/plan identity 为
12 killed；权重、共享 partner pool、fail-closed 与声明序放置为 14 killed；逻辑 offset、资源、起点唯一性、
真交织、容量、报告与冻结重试为 14 killed、2 equivalent invalid。两个 invalid 分别只删除 CP-SAT combined span
约束和 combined event-count guard，但相同上限仍由 `_check_session` 的 post-check 拒绝，因此没有可观察语义变化。
审查没有修改测试，每个生产变异都立即反向恢复并验证 detached 与 caller status 为空。

50 万门真实调用 `compile_scenario_plan`，并冻结 program-bound plan digest
`7b93a75e407382e24c4cd8dcfabf97cd9dfd30ff9c19ecbce166e2bfbd5d56ad`；它不是共享最小对象的 lightweight
carrier oracle。交织规模门命令为：

```bash
/usr/bin/time -l uv run --python 3.12 pytest -q \
  tests/operators/generation/test_planner.py \
  -k 'six_hundred_positive_branches' -x
```

它实际构造 600 个 delivery slots、300 个布局与 300 个机会，并验证 1200 次 solver 调用、派生
`primary_sessions=300` 和不构造 pair matrix；实际 plan digest 为
`59e11af8d22ecdf195409d6f2e242c11ba7a5d8f23e0315b4ae2013b41de8a89`。完整 suite 再次包含同一规模用例，
未通过 deselect 缩小组合门。

本地真实模型为
`/Users/atishoo/models/Qwen3.5-4B-GGUF/Qwen3.5-4B-Q6_K.gguf`，SHA-256 为
`fdedd781c9ce676ab66b018ca247ff78e8a33c98098a822c1e2d5075e7718f66`。服务为 llama-server 0.3.0，
build 10621，commit `c1d0e7a00`。实际启动命令为：

```bash
/opt/homebrew/bin/llama-server \
  -m /Users/atishoo/models/Qwen3.5-4B-GGUF/Qwen3.5-4B-Q6_K.gguf \
  -c 393216 -np 4 -b 2048 -ub 512 -t 6 -tb 6 \
  -ngl all -fa on --fit off -rea off \
  --host 127.0.0.1 --port 18081 --metrics --no-webui
```

真实集成命令及结果为：

```bash
LABELKIT_LOCAL_KEY=local-test-key uv run --python 3.12 pytest \
  tests/integration/test_execution_runtime_local_llm.py -q \
  -m 'integration and local_llm'
```

修正 checker 使其直接比较最终 row 的 duration/resources 后，结果为 1 passed in 82.23 秒，无 skip；shell wall
82.54 秒，peak RSS 207421440 bytes，0 swap。checker 在独立临时工程中机械验证 main 4 行、stream 16 行、两个共享
session 各含一个 `runtime_trigger` 与一个 `runtime_partner`、每个 session 六个事件且 owner runs 至少为三；同时验证
最终 stream 的 owner 行序映射到连续 plan position、logical/artifact delta、最终 row duration/resource、
plan/delivery digest、manifest 文件哈希与 secret scan。
轮询真实 server metrics 得到 `llamacpp:requests_processing` high-water 精确为 4。

随后使用同一服务运行公开四槽工程并持久化工件，shell wall 为 86.23 秒，peak RSS 182059008 bytes，0 swap。
report 记录 `program_digest=ddfc456f66e48f8e25c1899ffa786041f3836dab5dfe6197a8e010f1db199af5`、
`plan_digest=cc0d4342bd454b43bddee6689f41d7f4ce2d04f1bf75243950e4698f79d6e3ac` 与
`delivery_digest=0a3b798a163859d3bc52a05edc4725bdfe9f6c1b61090d7bebfb501c37fada96`。default profile 为
29 calls、24322 prompt tokens、1973 completion tokens；judge 为 5 calls、8539 prompt tokens、214 completion tokens；
两者 provider retries 均为 0。manifest 中 main、stream、report SHA-256 分别为
`d657f9c66ef5175f1421d65622d34038a00c579b1f482006dc1a6134c5f70829`、
`2b9604561b2a834ce3e08f33aaf6334084ffca0a3ec4bd7d24ab3cef78647a4a` 与
`f0624f1870bba8aea2502fed4366247fb4c7c4203fb1efbe16a323a371dc7a51`。

## v1.20 时间完整性证据看板

本节只记录 2026-08-27 当前实现 turn 已完成的离线门、Dataset-Person keyless 生产入口与本地 4B/GLM-5.2
真实端点门。完整 offline suite 与本轮 Uncle Bob mutation review 均已计入。下面的历史 v1.18/v1.19
真实端点结果不能替代 v1.20 raw 时间证明。

| 证据面 | 当前状态 | 已知事实 / 输入边界 |
|---|---|---|
| 完整 offline suite | 已验证 | 修复真实端点发现的冻结嵌套 annotation 误判后，`2928 passed, 48 deselected in 43.83s` |
| config 与 frozen shapes | 已验证 | 当前 `tests/common/config` 相关全组 552 passed，含 few-shot JSON array 回归 |
| Schema finalizer、M2、M3 | 已验证 | model/full Schema、generic finalizer、自描述 ingest 与 exact-only dedup 相关组 346 passed |
| planner、program、contracts | 已验证 | planner 35 passed；1 ms quantum、interval/resource/containment 与 replay rebound 均有直接测试 |
| Dataset-Person 离线门 | 已验证 | 当前两组门分别 46 passed 与 663 subtests；不把外部付费生成算入 |
| production keyless validate | 已验证 | production constructor 验证通过，未物化 LLM key value |
| production dry-run | 已验证 | 4380 sequences、16320 primary events；LLM calls 为 0，正式输出为 0 |
| 重复 compile | 已验证 | 两次 program digest 均为 `0e0a49...8f94b7`，plan digest 均为 `0c957e...bcca08` |
| plan temporal audit | 已验证 | duplicate starts 0；`foreground_app` overlaps 0；containment violations 0；annotation resource missing 0 |
| 本地 4B / GLM-5.2 probe | 已验证 | Qwen3.5-4B-Q6_K 162 ms；真实 `glm-5.2` 2678 ms；两个 profile 均由 production probe 到达 |
| 教学工程真实四槽 | 已验证 | 4/4 sets、12 primary、1 noise、3 replay；34 calls、0 rejection；running high-water 4，原子提交并独立审核通过 |
| production 时间叶门 | 已验证 | 单事件 navigation 检查 3 个业务时间 binding；双事件 navigation 检查 5 个 binding 与严格 containment，均由 GLM-5.2 接受 |
| production navigation 提交 | 已验证 | 1/1 set、1 primary；default 3 calls、judge 1 call；3 个 payload binding 与 annotation binding、manifest 哈希及 delivery digest 独立通过 |
| production 四类烟测 | 已验证失败边界 | navigation 等候选准备完成；`check_in` 被 GLM-5.2 连续拒绝 8 次后 slot exhaustion；无 success artifact，不能记为通过 |
| Uncle Bob mutation review | 已验证 | detached baseline `e90e7b1`；10 个独立语义变异全部 killed，survived、invalid、inconclusive 均为 0 |
| 十二周真实 raw 生成 | 待运行 | `[PENDING-EVIDENCE:v1.20-12w-real-generation]` |

本轮 Uncle Bob 在 detached worktree 中只临时修改生产源码，逐项运行预先声明的窄测试并立即反向恢复。十个变异覆盖
Schema projection、generic finalizer 完整终验、strict containment、resource overlap、replay rebinding、annotation
repair、M2 descriptor、exact dedup、precommit terminal 与 frozen nested annotation。还原后的同命令基线为 866 passed、
1 deselected；唯一 deselected 用例是绑定绝对 checkout 路径的固定 program/plan digest 向量，它已在 caller checkout 的
2928-test 完整离线套件中通过。审查结束时 detached HEAD 仍为 `e90e7b1`，`git diff --exit-code` 与包含 untracked 的完整
status 均为空，worktree 已通过 `git worktree remove` 清理。

production constructor 现在由 Schema annotation、frame `duration_s/resources/time_bindings`、pattern containment 与
sequence annotation binding 描述唯一时间真值。Dataset-Person exporter 的产品边界是格式转换与只读校验；不得 align、
normalize、shift、synchronize 或重写 raw 业务时间。keyless validate/dry-run 与计划审核只证明编译、排程和零调用边界，
不证明付费模型返回、最终 raw 工件或十二周 manifest-last 提交。

本轮本地 renderer 使用
`/Users/atishoo/models/Qwen3.5-4B-GGUF/Qwen3.5-4B-Q6_K.gguf`，SHA-256 为
`fdedd781c9ce676ab66b018ca247ff78e8a33c98098a822c1e2d5075e7718f66`；服务为 llama-server v9200，
关键参数为 `-c 393216 -np 4 -b 2048 -ub 512 -t 6 -tb 6 -ngl all -fa on --fit off -rea off`。semantic
evaluation profile 固定为真实 z.ai `glm-5.2`，没有替换 transport、LLM client、服务端或响应。

四槽教学工程报告记录 main 4 行、stream 16 行、delivery digest
`fad30744ddb5e6507e99e14fb80a06aeb068e8b279b9b187e4083a3fde3b3ae0`。default profile 为 29 calls、
32000 prompt tokens、1997 completion tokens；judge 为 5 calls、11262 prompt tokens、300 completion tokens；两者
provider retry 均为 0。独立审核重算 manifest 文件哈希与 delivery digest，并验证 16 个时间点全局有序且唯一、三种
primary/noise/replay descriptor 闭包、failed report 缺失与密钥不落盘。

首次 production 烟测在最终对账暴露真实缺陷：M11 已递归解冻 annotation 并通过完整 Schema，CrossView 最终对账却
直接把深冻结 `mappingproxy` 交给 `jsonschema`，导致合法嵌套 object 被误判为 type 违规。受控诊断证明原对象有一条
`/actionInfo: type` 违规，递归解冻后为零；修复为对最终用户对象同样递归解冻，并新增专门回归。修复后 navigation
单槽以真实模型原子提交，delivery digest 为
`053a3faadea448a0b17993d038a7c1b9dbf0db2fc727ee68dd7c888ea579b0f1`；独立审核验证 main/stream/report 哈希、
delivery framing、outer/payload/annotation 时间一致、时间唯一、failed report 缺失与密钥不落盘。

同一修复后的四类 production 烟测不能报绿：`check_in` 槽的八个候选全部被 GLM-5.2 的 semantic evaluation 拒绝，
failed report 精确记录 `sequence_delivery_exhausted`、8 个 `semantic_evaluation` rejection、default 93 calls、judge
11 calls，且 `artifacts_committed=false`；main、stream、success report、manifest 均不存在。该证据证明时间提交缺陷已
越过，但不证明 Qwen3.5-4B 能稳定满足全部白领工程语义，也不替代十二周真实 raw 生成。

## 历史 v1.18 闭包看板

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

## 历史 v1.19 execution runtime 闭包看板

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

这组 v1.18 证据只证明当时 replay 从最终 successful SequenceRows 派生，不从预投影 Record 或独立世界对象复制。
v1.20 已删除旧 payload-copy 判定：M2 改为从单一 stream 的 event descriptor 重算业务时间，验证 duration/resources、
constant shift、非时间 payload 与下游 metadata，并用 rebound payload 重算 replay event ID。当前行为只由上方 v1.20
门和新的 raw 生成证据证明。

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
- payload/annotation 业务时间、duration/resources、descriptor、containment 与全局 resource intervals 在提交前复验；
  source 与全部 rebound replay 进入同一个 `CrossViewDelta` 和同一次原子提交。
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
