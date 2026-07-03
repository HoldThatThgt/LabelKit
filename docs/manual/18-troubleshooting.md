# 第 18 章　故障排查：错误码表与高频问题

> 出问题时按顺序查三处：**stderr 最后几行**（直接死因）→ **report.json 的 counts**（哪类记录出了问题）→
> **rejects 里的 `_meta.reason`** 或 **trace 的 error 事件**（每条记录的具体错误码）。
> 本章给出错误码全表和一份按症状组织的 FAQ。

## 18.1 记录级错误码（StageError.kind）

这些错误码出现在 trace `error` 事件 payload 的 `kind` 字段；在 rejects 行里，failed 记录的 `_meta.reason` 即首个错误的 kind（`_meta.errors` 数组只存人类可读的错误消息文本，不含码）。注意 ingest 的四类（`bad_input_line` / `missing_pair` / `index_conflict` / `image_too_large`）是分类口径：坏数据在成为记录之前就被跳过或触发退出码 3，**不进 rejects、也不产生 error 事件**，只体现为 trace 的 `ingest.*` 事件与 report 的 `bad_input` 计数：

| kind | 谁发出 | 含义与处置 |
|---|---|---|
| `bad_input_line` | ingest | 坏行（非 JSON object / text_field 未命中）。按 `input.on_bad_line` 跳过或退出码 3。集中出现 ⇒ 先查 `text_field` 拼写 |
| `missing_pair` | ingest | UI 单侧文件。按 `on_missing_pair` 处理 |
| `index_conflict` | ingest | UI 同编号多文件。默认退出码 3——回去整理目录（第 5 章） |
| `image_too_large` | ingest | 超过 `max_image_mb`，该记录跳过 |
| `image_decode_error` | dedup / annotate / verify | 图解码失败：dedup 跳过图像层按树判；标注/评审阶段遇到则该记录 failed |
| `judgment_invalid` | quality | 单次裁决修复后仍非法 ⇒ 按平局计入 BT（不失败记录），计 `report.quality.judgment_failures`。率 >5% 见第 16 章诊断 |
| `schema_violation` | schema 引擎 | L3 修复预算耗尽 ⇒ 记录 failed。批量出现 ⇒ 第 14 章（Schema 太难/输出被截断） |
| `callback_violation` | schema 引擎 | L3 耗尽且剩余违规全部来自 `output.validator` 回调（14.5）⇒ 记录 failed。批量出现 ⇒ 回调规则模型学不会——把违规消息改写成更明确的改进指示，或放宽规则 |
| `provider_retryable_exhausted` | llm-client | 重试 max_retries 次仍失败（网络/超时/429/5xx）⇒ 记录 failed。批量出现 ⇒ 端点在持续故障或限流 |
| `provider_fatal` | llm-client | 不可重试错误（401/403/400/404）⇒ 记录立即 failed 并计入熔断窗口。批量出现 ⇒ 密钥/权限/模型名问题 |
| `internal_error` | 任意 | 未预期异常（含输出前终检兜底）⇒ 记录 failed，堆栈在 debug 级日志。理论上不该出现，出现请留存日志报告 |

## 18.2 按症状排查

### 「启动就退出，码 2」

读 stderr——所有配置错误都带**文件:节.键**定位与期望值提示，且一次列全：

```
ConfigError: 2 个配置错误（全量聚合反馈）
project.toml:[run].output: 缺失必填键，期望字符串（可用 CLI --output 提供）
config.toml:[llm.default].api_key_env: 环境变量 "LABELKIT_ZAI_KEY" 未设置或为空
```

高频前六名：环境变量没加载（`set -a && source .env && set +a`）；引用的 profile 名拼错（错误里会列出可用名单）；Schema 不是合法 draft 2020-12 / 顶层不是 object / 声明了 `_meta`；`selection = "top_ratio"` 时仍设了 `threshold`（两种淘汰机制互斥，第 10 章）；UI 模态引用的 profile 没开 `supports_vision`；输出父目录不存在（忘了 `mkdir -p out`）。反向情形（`selection` 保持默认 `"threshold"` 时写了 `top_ratio`）不报错但会打一条 warning 提示「该键不会生效」——看到它就补上 `selection = "top_ratio"`。

另注意**警告不是错误但更阴险**：「未知键，已忽略（前向兼容）」意味着你拼错了某个参数名、它压根没生效——看到这条警告立刻回头对拼写（对照附录 A）。

### 「退出码 3」

输入路径不存在 / 目录下没有候选文件（文本模态：没有 `.jsonl`；UI 模态：找不到 `uitree_*` 与 `image_*`）/ **无任何合法记录**（读完输入 `ingested=0`）/ UI index 冲突（默认 fail）/ 坏行、缺对显式配了 fail 策略。stderr 都会给出定位信息，按提示修数据或字段名即可。

其中「无任何合法记录」的经典病根是 **`text_field` 与数据字段名不匹配**——每行都成了坏行，默认 skip 策略逐行告警后在流末尾统一报错：

```
InputError: 无任何合法记录: input.jsonl（scanned=14 bad_input=14 missing_pair=0 index_conflict=0）
```

只要有部分行合法，skip 策略照常跑完（坏行计入 `bad_input`，退出码 0）——见下文「`bad_input` 占大头」一行。

### 「退出码 4」

- **熔断**：report 照常写出，显式标志是 `run.circuit_broken: true`（`interrupted` 保持 `false`——那个字段仅在 SIGINT/SIGTERM 中断时为 true），主输出 `.part` 不改名交付。认证类错误（401/403）**首次出现即熔断**；400/404、重试耗尽等按连续计数达阈值熔断。查密钥、模型名、网关状态；
- **输出不可写（运行期才失败）**：启动时输出目录还正常、运行中途写入失败——目录被删/改名、磁盘写满、权限被中途收回等。注意：忘了 `mkdir -p out` 或目录一开始就没有写权限，会在启动校验被拦下 → **退出码 2**（消息「输出父目录不存在或不可写」）；
- **Ctrl-C 打在流水线之外**：运行中的 Ctrl-C 走优雅中断（正常交付、退出码 0/1，见「`.part` 文件是什么」）；但打在启动/收尾阶段（配置装载、probe 等）或信号处理不可用的平台上时，进程以 `interrupted` + 退出码 4 收场。

顺带说明 stderr 的死因行格式：真正逃逸到进程级的异常，首行为「异常类名: 消息」——现实中会出现的有 `InputError`（退出码 3）、运行期写盘失败的 `LabelKitError` 与各种未预期异常类名（退出码 4）；配置错误则是 `ConfigError: N 个配置错误…` 的聚合格式。注意**熔断不产生异常死因行**——它走正常收尾，stderr 特征是连续 provider 错误日志之后的 `run.end exit_code=4` 与终版摘要；`ProviderFatalError` 也总是被转成记录级错误（落在 rejects 的 `_meta.reason`），不会以死因行出现。

### 「退出码 0，但主输出是空的 / 比预期少很多」

这是最需要冷静读账的一类。按 counts 分诊：

| counts 特征 | 病因 | 去哪治 |
|---|---|---|
| `failed` 占大头 | 看 rejects 的 `_meta.reason`：`provider_fatal` = 模型名/路径类错误（400/404）没攒够熔断阈值——密钥错误（401/403）如今会立即熔断、不会走到这里；`schema_violation` = Schema 问题 | 第 2 章 probe / 第 14 章 |
| `dropped_lowq` 占大头 | 质量线切多了，或默认 rubric 的口径不适合你的数据 | 第 10 章：看直方图重新画线 / 换 rubric |
| `dropped_dup` 占大头 | 模板化数据被近似去重大面积命中 | 第 9 章场景二：阈值提到 0.92+ |
| `bad_input` 占大头（但仍有部分合法行） | text_field 对部分行不适用 / 文件格式混杂（全员坏行不会走到这里——那是退出码 3「无任何合法记录」） | 第 5 章自查清单 |
| `dropped_verify` 占大头 | 评审口径过严，或标注质量真的差 | trace 读 critiques（第 13/16 章） |

### 「跑得比 dry-run 估的贵」

估算不含重试与修复。查 `llm_usage.retries`（限流？）与 `schema_engine.resolved_at.l3_*`（修复环烧钱？）。

### 「同配置两次运行结果不一样」

流程路径（配对、抽样、顺序）在同 seed 下是完全可复现的；**LLM 服务端本身的非确定性**（即使 temperature=0，部分服务的输出也非严格确定）无法由工具消除。判定翻转率高时参考第 16 章「同 seed 重跑翻转率」的诊断与处置。

### 「trace 文件怎么没了 / 变小了」

trace 默认路径随输出走，在**首个事件写出时**截断（覆盖前 stderr 有一条 `trace file ... already exists — truncating` 的 WARN）。死于配置/输入校验的「秒败」运行与 dry-run（写 `{名}.dryrun{后缀}` 独立文件）都不会碰它；正常启动的重跑仍会覆盖——要历史就归档或换 `trace.path`。

### 「`.part` 文件是什么」

主输出的临时名。运行中存在是正常态；运行结束后还在 = 那次运行没走到交付：熔断（退出码 4）、运行期写盘失败、进程被强杀/崩溃或异常中途退出。注意优雅中断（Ctrl-C / SIGINT/SIGTERM）**会**正常交付——已完成批次的 `.part` 被 fsync 后改名为最终输出，report 标记 `interrupted: true`，不留残骸。残骸可删，以 report 为准。

## 18.3 求助前收集什么

- stderr 全文（放心，里面没有你的数据内容）；
- `report.json`（同样只有统计）；
- 复现命令与两份配置（**删掉 api_key_env 指向的真实密钥值**——配置文件本身不含密钥，可直接给）；
- 若为记录级问题：rejects 里对应行的 `_meta.errors`。

## 18.4 一分钟自检脚本

```bash
# 逐级体检：语法 → 连通 → 输入 → 小样本
uv run labelkit validate --config config.toml --project project.toml --probe && \
uv run labelkit run --config config.toml --project project.toml \
    --dry-run --output out/check.jsonl && \
uv run labelkit run --config config.toml --project project.toml \
    --limit 5 --output out/check.jsonl --strict
echo "exit=$?"   # 0 = 五条全过；1 = 有淘汰（去 out/check.rejects.jsonl 看原因）
```
