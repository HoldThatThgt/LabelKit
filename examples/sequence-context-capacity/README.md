# 完整会话与上下文容量本地门禁

这个例子把完整输入文件作为一次有限运行。`batch_size` 控制计算分组，语义成员、缝合及最终提交由完整会话决定。
正常配置连接已有 `127.0.0.1:8082` 单槽 Qwen3.5-4B-Q6_K 服务，不启动、停止或调整服务。

| 项目 | 核验内容 |
|---|---|
| `project-text.toml` | 六次输入出现；两行完全相同；首尾修改与中间改色共同确定最终配货结果 |
| `project-ui.toml` | 二十五张完整截图；中间唯一绿色卡片只存在于像素；每棵完整树必须进入请求 |
| `project-stitch.toml` | 仓库任务三帧、聊天两帧、仓库恢复三帧；要求真正合并且保留两段完整任务成员 |
| `project-static.toml` | 正常服务上诚实限制应用预算；完整成员达到容量后封闭，后续不重并 |
| `project-reactive.toml` | 专用故障配置高报窗口，真实服务拒绝超窗请求，随后确定性成员拆分并重算 |
| `project-minimum.toml` | 先交付正常会话，再遇到不可再拆的单个完整超大成员；必须有限终止 |

`config-reactive.toml` 和 `config-minimum.toml` 中的 65536 是故障注入，真实服务仍为 32768；正常运行始终用
`config-local-4b.toml`。失败响应不要求伪造 usage，成功请求必须有真实非零 prompt/completion tokens。

从仓库根目录先检查配置与输入，不发模型请求：

```bash
uv run labelkit validate --config examples/sequence-context-capacity/config-local-4b.toml \
  --project examples/sequence-context-capacity/project-text.toml --console plain
uv run labelkit run --config examples/sequence-context-capacity/config-local-4b.toml \
  --project examples/sequence-context-capacity/project-text.toml --dry-run --console plain
```

本地测试内部复制项目并重新生成确定性图像与压力数据；`prepare.py` 只操作文件，截图字体固定使用本机
`/System/Library/Fonts/STHeiti Medium.ttc`。保持单槽空闲，串行执行：

```bash
LABELKIT_GATE_EVIDENCE=$(mktemp -d /tmp/labelkit-sequence-context-local.XXXXXX)
LABELKIT_LOCAL_KEY=local-test-key uv run --python 3.12 pytest \
  tests/integration/test_sequence_context_capacity_local_llm.py -q -s \
  -m 'integration and local_llm' --basetemp "$LABELKIT_GATE_EVIDENCE/pytest" \
  --junitxml "$LABELKIT_GATE_EVIDENCE/junit.xml"
```

`local-test-key` 是现有无认证本地服务的非秘密占位值。测试不替换模型、HTTP transport 或任何预算函数；
观察器原样传递请求和返回，只保留输入哈希、确切阶段、图片顺序、完整证据布尔值、错误结构和用量。
端点错误形状测试先用真实 `/tokenize` 确認正文 token 下界已超过槽容量，再只发一次实际补全请求。

`check_output.py` 独立于 LabelKit 实现。可对实际输出运行，例如：

```bash
uv run python examples/sequence-context-capacity/check_output.py /absolute/path/text-2.jsonl
uv run python examples/sequence-context-capacity/check_output.py /absolute/path/stitch.jsonl --case stitch
uv run python examples/sequence-context-capacity/check_output.py /absolute/path/reactive.jsonl --case reactive
```

检查器按整数出现位置验证成员守恒和唯一归属，不用内容 ID 集合吞掉重复输入；还验证业务答案、真实合并计数、
完整容量边界和失败前会话的交付。请求完整、Schema 合格、业务答案正确和重复运行稳定分别取证。

## 本轮验证记录

2026-09-09：所有项目已通过 keyless 配置加载，文本 validate 与 dry-run 成功。本特性本地门禁已完成，
结果来自两次真实执行，不能记为单轮七项全通过：

| 实际 pytest 范围 | 结果 | 归档日志 |
|---|---|---|
| 整份 `test_sequence_context_capacity_local_llm.py`，七个节点 | `6 passed, 1 failed in 115.97s`；唯一失败是 static 测试误要求尾段也 sealed，业务输出已有三段、六成员、零失败、零重算 | [矩阵日志](out/verification-20260909/labelkit-capacity-local-final.log) |
| 修正并加强断言后，单独执行 `test_real_local_capacity_recovery[static]` | `1 passed in 14.21s`，重新发送真实模型请求；逐段边界、允许范围和精确封闭计数通过 | [static 复跑日志](out/verification-20260909/labelkit-capacity-local-static-final.log) |

两次命令的 pytest 选择范围分别如下；环境与临时证据目录使用前述运行方式，每次使用新目录：

```bash
uv run --python 3.12 pytest tests/integration/test_sequence_context_capacity_local_llm.py \
  -q -s -m 'integration and local_llm'
uv run --python 3.12 pytest \
  'tests/integration/test_sequence_context_capacity_local_llm.py::test_real_local_capacity_recovery[static]' \
  -q -s -m 'integration and local_llm'
```

文本节点内部真正运行 `batch_size=2→3→64` 三轮，所以归档包含八个最终 `execute_run` 工件，另有真实超窗
错误形状节点。八份输出均通过独立 checker；输入文件字节数、哈希、结构计数、报告用量、运行耗时及服务身份见
[summary.json](out/verification-20260909/summary.json)。模型为 `worldmock-qwen35-4b`，build
`b10621-c1d0e7a00`，单槽真实容量 32768；模型 SHA-256 为
`fdedd781c9ce676ab66b018ca247ff78e8a33c98098a822c1e2d5075e7718f66`。

| 工件 | 输入字节 | 正式结果 | 报告 calls / 输入 token / 输出 token | execute_run 耗时（秒） |
|---|---:|---|---|---|
| text-2 / text-3 / text-64 | 各 666 | 各一段、六成员；业务与成员签名相同，完整请求观察器逐项通过 | 各 4 / 1932 / 475 | 8.21 / 8.03 / 8.05 |
| ui | 94130 | 一段、二十五成员；所有实际完整请求携带二十五图及完整树，像素中的唯一绿色被识别 | 2 / 7739 / 277 | 4.94 |
| stitch | 84293 | 三个语义段、一个真实合并壳、两条任务输出；仓库恢复后答案正确 | 9 / 8727 / 1214 | 26.06 |
| static | 47700 | 三段覆盖六成员，sealed 为 `[true,true,false]`；两个切点、两个封闭、零重算 | 6 / 17619 / 791 | 13.84 |
| reactive | 72300 | 一次真实超窗、一个切点、一次重算，最终两段均 sealed，零失败 | 4 / 73224 / 451 | 36.04 |
| minimum | 68480 | 先前两成员会话交付；随后单成员明确终态，一次重算、一个最小失败，无拆分 | 2 / 663 / 232 | 4.52 |

报告 calls 和 token 口径不包含没有 usage 的超窗响应。reactive 实际观察到五次 HTTP 请求，其中一次
HTTP 400 为 `exceed_context_size_error`，`n_prompt_tokens=36140`、`n_ctx=32768`；minimum 实际为
三次 HTTP 请求，其中一次同类型错误为 `34113 > 32768`。失败请求没有原样重播；修复与失败请求事实保留。

## 早期失败与修复边界

最初文本夹具仅正文重复、时间字段不同，内容身份并不相同，导致重复 ID 断言失败。修正为整行 JSON 完全重复后，
没有修改生产 ID 规则；最终三轮仍完整保留六个出现位置，并确认重复身份不会吞掉成员。

首次 stitch 门禁真实产生三段、一次合并和两条任务，但 verify 拒绝了两条输出，因此该轮不算通过。
审核对仓库任务假设了未观察到的未来变化，对聊天任务忽略了原指令规定的空值格式；当时图像只画应用名与步骤号，
业务正文仅在完整树中。修订将同一业务正文实际绘入截图，并在本项目审核约定中明确“最终”只指给定成员、
聊天空值格式是任务定义，时间中断本身不足以判 wrong_stitch。没有给审核员答案、要求 pass 或豁免真实身份冲突；
原位置、业务答案、合并次数和 verify/repair 门禁全部保留。
[首次 stitch/UI 日志](out/verification-20260909/labelkit-capacity-local-ui-stitch-first.log)保留失败事实，
最终 stitch 的两条任务通过真实评审后才交付。

首次 static/reactive 容量运行暴露评审边界余量把人工切点外的正文和图片重新加入请求；reactive 产生
36533 token 请求，容量拆分后仍无法容纳。另发现同波两个实际请求均超限却只传播首错，使另一失败请求被重发。
修复明确排除允许区间之外的证据，并在业务归并前按声明序收齐所有容量失败，再统一推进切点或最小终态；
没有裁剪允许区间内的成员、树或图片，也没有关闭 verify 或降低业务答案要求。
[首次容量日志](out/verification-20260909/labelkit-capacity-local-capacity-first.log)保留 `2 failed, 1 passed`
及真实错误记录；最终 reactive 的一次重算和唯一失败请求哈希验证了修复后的闭环。

最终矩阵的 static 单项失败属于测试断言错误：初始贪心分区只封闭被后段接替的前段，尾段保持开放符合规格。
修正后要求精确的 sealed 序列、逐段 before/after、半开 allowed_positions 和报告计数，并保留 reactive 全部 sealed
要求；随后进行了上述 14.21 秒真实复跑。早期失败与最终成功证据分开保留，未改写失败记录。
本地门禁补充本特性证据，不替代 DeepSeek/z.ai 的正式发布门禁。
