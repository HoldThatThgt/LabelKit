# 跨批次序列缝合与上下文容量验收记录

本文件记录实际证据，不代替[开发规格](SPEC-sequence-context-capacity.md)。
[文件清单](SEQUENCE-CONTEXT-CAPACITY-FILES.md)列出实际改动范围。

## 当前阶段

功能已经实现；全部特性组合测试、完整离线回归与真实本地4B门禁通过。
用户授权本地提交与隔离变异后，Uncle Bob 已在干净提交 `9a3330a` 完成首轮。
首轮发现测试断言缺口，当前进行测试加固；须在新的干净提交完整复审，结果见
[Bob报告](BOB-sequence-context-capacity.md)。功能与回归通过不能替代该门禁。

| 核对项 | 实际证据 |
|---|---|
| checkout / HEAD | `/Users/atishoo/Project/LabelKit`；`codex/annotation-postprocessing`；实施基线 `56a0ea1`，功能提交 `9a3330a` |
| 修改前基线 | 3239 passed、49 deselected、699.72秒；开始工作区干净 |
| 规格和研究 | 官方Spark、Flink、ksqlDB、LlamaIndex与端点资料已核对；spec先审查再实现 |
| 首轮完整离线 | 3432 passed、5 failed、56 deselected、644.81秒；失败全为生成摘要固定向量 |
| 最终完整离线 | 3467 passed、56 deselected、653.00秒；shell墙钟653.52秒，测试进程RSS高水位944439296字节；包括更新金值后的大型规划与全部新增组合 |
| 固定向量核对 | 旧HEAD真实loader/compiler与当前版独立规范化diff仅删除旧sequence_frames；生成核心源文件未改；六金值更新，三个小摘要回归通过 |
| 生产覆盖率 | 最终完整门300/300改动可执行函数实际进入；32文件全部达标，最低行89.61%、分支78.05%；没有把Protocol声明当函数进入；独立核对源码与最终覆盖哈希 |
| 最后组合回归 | 159 passed，含25个跨模块组合、后处理大产物三参数和CLI实际布局；各owner另跑357 / 447项窄回归 |
| 配置静态门 | 六本地项目全部keyless validate及dry-run通过，共12条真实CLI命令 |
| 真实4B矩阵 | 七测试节点先6 passed、1 failed、115.97秒；static按原规格纠正并加强断言后真实复跑1 passed、14.21秒 |
| 独立产物检查 | 八个execute_run（文本batch_size 2/3/64与其余五项目）均通过独立业务、位置守恒及边界检查 |
| 输出与请求 | 文本classify/annotate/verify完整；UI25图/树完整，中间像素事实正确；stitch真实合并；active/reactive容量恢复与最小失败明确结束 |
| 文档版式 | HTML与186页PDF重建；两张新增流程图及受影响页面已渲染视检，无内容遮挡或裁切；预览及文件哈希归档 |
| 当前未完成门 | Uncle Bob首轮完成并发现测试断言缺口；测试加固后的完整复审尚未结算 |

## 真实模型与资源证据

服务为已有8082单槽，worldmock-qwen35-4b，llama-server b10621-c1d0e7a00，实际上下文32768、视觉开启。
模型文件SHA-256为 `fdedd781c9ce676ab66b018ca247ff78e8a33c98098a822c1e2d5075e7718f66`。
测试未修改服务配置或模拟响应；故障配置刻意高报窗口，端点实际拒绝后由生产容量控制器恢复。

| 项目 | 输入字节 | 输入出现数 / 输出数 | 成功calls；prompt / completion tokens | 容量与业务结果 |
|---|---:|---|---|---|
| text-2 / text-3 / text-64 | 各666 | 各6 / 1 | 各4；1932 / 475 | 重复ID保留，完整中间改色事实参与答案，三轮签名相同 |
| ui | 94130 | 25 / 1 | 2；7739 / 277 | 25图及完整树进入请求，green与25帧正确 |
| stitch | 84293 | 8 / 2 | 9；8727 / 1214 | 3片段、1次合并；配货[0,1,2,5,6,7]，聊天[3,4] |
| static | 47700 | 6 / 3 | 6；17619 / 791 | 2切点、2封闭前段，末段开放，0重算/0失败 |
| reactive | 72300 | 2 / 2 | 4；73224 / 451 | 1真实HTTP400，1切点、1次重算、0失败，无相同失败请求重播 |
| minimum | 68480 | 3 / 1 | 2；663 / 232 | 首会话保留；第二会话单帧终态，1失败、无无限重试 |

完整矩阵测试进程RSS高水位140689408字节；static复跑134692864字节。不是模型服务RSS，也不是会话
物理内存上限。输入字节按实际文件清单计数，不包含未使用夹具。模型请求成功、Schema合法、语义答案、
重复运行与内存观测分别取证；没有吞吐加速结论。

## 可审计产物

本地归档根为 `examples/sequence-context-capacity/out/verification-20260909/`。

| 产物 | 内容 |
|---|---|
| `summary.json` | 八个真实运行的输入文件字节/SHA-256、运行耗时、计数、usage、capacity与模型身份 |
| `text/`、`ui/`、`stitch/`、`static/`、`reactive/`、`minimum/` | 实际输入、配置、Schema、主输出、rejects、report、trace与请求结构证据 |
| `labelkit-capacity-local-final.log` | 七节点原始完整日志，保留static断言失败 |
| `labelkit-capacity-local-static-final.log` | 修正并加强static断言后的真实复跑日志 |
| `labelkit-capacity-offline-final.log`、`labelkit-capacity-coverage.json` | 首完整离线失败与覆盖原件，不作为Bob绿色基线 |
| `labelkit-capacity-offline-verified.log`、`labelkit-capacity-coverage-final.json` | 功能阶段完整离线绿色结果及对应覆盖原件；独立Bob基线与复审另行结算 |
| `changed-production-coverage.json` | 基线/源哈希、每个改动函数真实函数体行、每文件门槛及缺失行/分支 |
| `changed-production-coverage-first-run.json` | 首轮覆盖审计原件，最终审计未覆盖历史记录 |
| `source-constraints.json` | 改动生产文件、行、函数与参数约束，AGENTS/CLAUDE一致性 |
| `bob-preflight-status.txt` | Bob调用方工作区非空的原始证据，不是变异执行记录 |
| `generation-digest-canonical-diff.json` | 旧版与当前规范化材料、独立hash及仅旧字段删除的精确差异 |
| `labelkit-capacity-example-static-validation.log` | 六项目validate/dry-run真实CLI结果 |
| `design-preview/` | 设计PDF受影响页及联系图，用于版式检查 |

## 规格到独立证据

每个验收要求均映射到具体测试断言，不把单模块实现存在当作组合证明。

| 特性范围 | 可执行证据及独立审查 |
|---|---|
| 会话生命周期、计算组、全局去重、提交和取消 | `test_session_workflow.py`、`test_dedup_session.py`、`test_process_workflow.py`及[控制器审查](research/sequence-context-capacity-controller-audit.md) |
| 语义分段、噪声、min_len、sealed、所有缝合入口 | `test_segment.py`、`test_stitch.py`和[上游审查](research/sequence-context-capacity-upstream-audit.md) |
| 人工边界、回收、出现位置、交错片段、多标签接缝依赖 | `test_verify.py`、`test_emitter_capacity.py`；真实fan_out→SHRINK/RECLAIM→依赖复评和共享帧产物回归 |
| 完整请求、固定包络、全部已失败叶、L3完整scope | `test_sequence_complete_evidence.py`及`test_sequence_capacity_combinations.py`；后者八类真实调用入口使用真实SchemaEngine验证修复链 |
| 后处理大产物 | `test_project_postprocessor_expansion_reaches_real_schema_verify_and_session_capacity[fit/split/minimum]`；真实工程函数和完整SchemaEngine→实际verify预算→真实会话控制器 |
| 会话快照校准 | 组合测试使用真实ImageCostCalibrator和ProcessWorkflow，跨重算固定、下一会话才更新；分组大小与完成序四组合 |
| 空会话及已提交前缀取消 | 组合测试覆盖空会话迭代；真实DedupStage与Emitter首会话成功后第二会话实际取消，输出、正式索引和最终计数无泄漏 |
| 配置、身份、错误分类、可观察计数 | common/config、contracts、inference、observability测试及[公共层审查](research/sequence-context-capacity-common-audit.md) |
| 真实端点与业务结果 | `test_sequence_context_capacity_local_llm.py`七节点及独立`check_output.py`；没有模拟服务或录制响应 |
| 完整回归、函数/文件覆盖率、Bob | 由各自真实门禁结果独立结算；未执行不得写通过 |

## 历史失败与修复

首文本失败来自夹具时间戳不等，修成完全相同输入后保留重复ID且语义正确。首stitch确实完成合并但verify
拒收；修订截图使业务事实同时可见，并澄清审核任务范围，未提供正确答案、要求pass或豁免身份冲突。
首static/reactive门暴露verify读取切点外邻帧，以及已失败叶只传播首错；修复后恢复通过。随后独立审查
补齐完整用户包络、完整L3修复、repair profile能力、dedup同步计划多错、克隆归属/共享帧产物和交错片段
重排。最后static仅测试误要求末段sealed；加强边界及精确计数断言后再经真实模型复验。
原始失败日志保留，不能以最终绿色结果覆盖失败经过。

本地4B补充本次特性验收，不替代DeepSeek/z.ai正式发布门；本轮未执行该发布门，明确记录
`[PENDING-EVIDENCE:sequence-context-capacity-release-endpoints]`。所有本特性开发验收项必须闭合，不能据此延期。
