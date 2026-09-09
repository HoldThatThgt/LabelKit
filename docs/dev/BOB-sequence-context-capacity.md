# Bob report

## 当前结果

**COMPLETE：完整复审293个有效语义变体全部 killed，零 survived、invalid、inconclusive；本特性审查门通过。**

仓库为 `/Users/atishoo/Project/LabelKit`；审查基线为 `a050661f93f660446dc46143f0e3df47e4ed71d1`。
唯一行为规范为 [SPEC-sequence-context-capacity.md](SPEC-sequence-context-capacity.md)，范围是该特性的完整
process+segment会话处理、上下文容量、各实际请求、状态归属、提交和输出合同。没有发现原版实现缺失
或明确偏离规格；首轮测试断言缺口全部闭合。全部规格功能已实现，没有延期实现项。

用户“go”明确授权了本地提交、隔离临时生产源码语义变异及必要修复提交。只创建本地提交，没有push。

## 最终独立基线与结果

| 审查范围 | 预声明原版基线 | 有效变体 | killed | survived / invalid / inconclusive |
|---|---|---:|---:|---|
| 公共契约、配置、预算、完整证据、Schema、错误和指标 | contracts 18、config 356、evidence 109、schema 124、budget 71、wire 10、metrics 55、calibration 25；八组全部通过 | 69 | 69 | 0 / 0 / 0 |
| classify、extract、quality、annotate及容量预览 | 428 passed，1.74秒；恢复后428 passed，1.12秒 | 66 | 66 | 0 / 0 / 0 |
| segment、stitch、verify、边界手术与输出 | 559 passed，4.25秒；64组精确oracle基线全部通过 | 82 | 82 | 0 / 0 / 0 |
| 会话编排、容量恢复、去重增量与提交 | 412 passed，5.87秒 | 76 | 76 | 0 / 0 / 0 |
| 合计 | 各组在独立detached worktree执行 | 293 | 293 | 0 / 0 / 0 |

全部原292个有效源码变体完整重跑，另预声明一个规范UTF-8身份变体；未只复跑存活项或替换成更容易
检出的变体。各组生产文件范围互不重叠。每条oracle在变异前冻结并先通过原版基线；没有看结果再改命令
或测试。退出码1只是待裁决结果，只有失败与预期规格违例存在直接因果关系时才计killed。

完整离线回归在另一棵从未施加变异的工作树执行：**3543 passed、56 deselected、2个依赖弃用警告，
688.72秒**；shell墙钟691.32秒，命令RSS高水位1018331136字节。覆盖审计为300/300改动可执行函数进入，
32文件全部达标，最低行89.88%、分支78.46%；一个无可执行体的Protocol声明明确排除。
不将这一完整回归与任一变异命令混在同一工作树运行。不声明变异分数或完整规范变异覆盖百分比。

## 因果核对与测试加固

新增断言直接验证独立身份固定向量、真实固定包络与Schema预算、完整成员/动作/双侧请求、最小请求归属、
非容量异常路由、预览纯度、内部接缝任务名、回收提交边界、修复后的容量边界及实际输出元数据。
控制器断言验证尝试任务身份、过时失败失效、异常引用释放、提交前成员守恒、上游计数保留、去重最高分
和并列顺序、最终探测状态与增量释放。最高分测试保留，新增并列测试不替换已有异分场景。

公共层疑难结果另经子审查只读交叉核对：类视图profile变体检出的是漏报必需的聚合配置错误，
不是任意错误文案变化；错误根身份变体在一次二分后丢掉子序列失败，无法产生所需最小终态；
L3路由变体把容量错误改为SchemaViolation，未宣称本次已执行到后续消息完整性断言。
两个原先只因错误regex失败的成员归属变体，现在由完整提交反例直接检出：去掉检查后不再抛InternalError，
且原版必须在去重、统计与输出提交之前拒绝这些违规。

## 可复核证据与恢复

归档根为 `examples/sequence-context-capacity/out/verification-20260909/`。

| 归档 | 内容 |
|---|---|
| [汇总与源码去重核对](../../examples/sequence-context-capacity/out/verification-20260909/bob-final-review/summary.json) | 独立核对293个不同的生产路径与变异源码哈希；四组生产围栏互不重叠；归档文件哈希见同目录archive-manifest.json |
| [公共层报告](../../examples/sequence-context-capacity/out/verification-20260909/bob-final-review/common/bob-report.md) | 69项台账、预声明命令、补丁/日志、独立交叉裁决与restoration.json |
| [下游报告](../../examples/sequence-context-capacity/out/verification-20260909/bob-final-review/downstream/bob-report.md) | 66项相同源码变体重跑、实际导入及哈希证明、cleanup.json |
| [上游报告](../../examples/sequence-context-capacity/out/verification-20260909/bob-final-review/upstream/report.md) | 82项台账、36组规范映射、64组oracle基线和cleanup.json |
| [控制器报告](../../examples/sequence-context-capacity/out/verification-20260909/bob-final-review/controller/BOB-report.md) | 76项完整重跑、5个历史无效候选排除理由和restoration.json |
| [完整回归与覆盖](../../examples/sequence-context-capacity/out/verification-20260909/bob-final-review/full/summary.json) | 原始offline.log、coverage.json、独立逐函数覆盖审计、命令及未变异工作树恢复证明 |
| [首轮归档](../../examples/sequence-context-capacity/out/verification-20260909/bob-first-review/archive-manifest.json) | 首轮及补充轮全部原件、存活诊断、测试加固、原始环境失败及文件SHA-256 |

每次变异后精确清理目标模块pyc；每次反向恢复立即核对源码、git diff及完整porcelain，随后才执行下一项。
四棵最终审查工作树和独立完整回归工作树均已无force恢复并移除，路径与注册均不存在。
各审查结束时调用方HEAD仍为 `a050661`，diff和完整porcelain为空；之后才进入最终文档提交阶段。
未修改用户其他工作树。归档保留原始执行路径；每条变异的差异和日志均同时存于相应归档目录。

## 历史结果保留

最初HEAD为 `56a0ea1`，本次功能尚未提交，Uncle Bob按干净调用方前置条件返回BLOCKED，没有绕过检查。
原始状态为 `bob-preflight-status.txt`。用户授权后在功能提交 `9a3330a` 建立隔离审查。
首轮独立完整基线3467 passed、56 deselected、606.31秒；首轮及补充轮的不同源码变体按最后独立轮次结算：

| 首轮范围 | 不同变体 | killed | survived | invalid | inconclusive |
|---|---:|---:|---:|---:|---:|
| 公共层 | 69 | 64 | 4 | 1 | 0 |
| 下游 | 66 | 52 | 14 | 0 | 0 |
| 上游 | 83 | 78 | 4 | 1 | 0 |
| 控制器 | 81 | 57 | 17 | 5 | 2 |
| 合计 | 299 | 251 | 39 | 7 | 2 |

历史7个invalid包括错误输入形状、无规范依据、不可达竞争或等价行为，不算有效检出；有规范依据的
有效替代变体已包含在原292个有效集合内，并全部纳入最终复审。首轮补充oracle各自先跑原版基线，旧结果未被追溯改写。
控制器首轮因既有示例输出目录缺失产生的基线失败，以及两次反向补丁恢复闸门停止，均保留原始记录，
未算killed。结束首轮并恢复所有审查树后才修改测试；17文件加固集成1183 passed，5.31秒，再提交 `a050661`。

## 与真实模型验收的边界

本轮仅加强测试和证据文档，生产与真实本地4B测试源码均与已经实跑的功能版本完全一致。
生产源码清单SHA-256为 `8ac26b912f06f29c35d5be7a19ec0eb95ea46e87c085f2e69760da45d2f1794c`。
真实Qwen3.5-4B的七个节点已分别通过，见[功能验收记录](SEQUENCE-CONTEXT-CAPACITY-VERIFICATION.md)。
离线变异不代替真实模型语义验证；本地4B也不替代尚未运行的DeepSeek/z.ai正式发布端点门。
