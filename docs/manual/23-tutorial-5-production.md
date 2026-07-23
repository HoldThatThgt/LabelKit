# 第 23 章　教程五：生产级配置——鲁棒性、审计与运维纪律

> **难度：★★★★☆**
> 前四篇教程解决「跑通」和「调好」；这一篇解决「敢把结果交出去」。
> 场景设定：每周一批 5 万条输入法请求，产物直接入训练数据资产库，错标有真实代价。
> 本章给出一份带完整理由的生产配置模板 + 一套运维纪律。

## 23.1 生产与探索的分界

| 维度 | 探索期（教程 1–4） | 生产期（本章） |
|---|---|---|
| 目标 | 快速迭代配置 | 结果可信、过程可审计、失败可感知 |
| 裁决 | 单模型单次 | 评审团 + 双序（关键任务） |
| 校验 | 可以不开 | 必开，独立模型 |
| 失败处理 | 看看账就行 | `--strict` + 退出码接入调度系统 |
| 产物管理 | 随手覆盖 | 按批次命名、trace 归档、报告留存 |

## 23.2 生产配置模板（逐段带理由）

**config.toml**——三个异构 profile 是评审团的物质基础：

```toml
schema_version = 1

[tool]
log_level = "info"
log_format = "jsonl"          # 理由：接日志采集系统；console 强制 plain 档（第 16 章），CI 里本来也不需要面板

[llm.default]                 # 主力：打分与标注
provider = "openai_compatible"
base_url = "https://llm-gw.example.com/v1"
model = "qwen2.5-vl-72b-instruct"
api_key_env = "LABELKIT_KEY_DEFAULT"
max_concurrency = 16          # 理由：生产网关额度高；从限流值的 60% 起步，盯 retries
supports_structured_output = true
supports_vision = true
context_window = 131072       # 理由：v1.11 预算护栏的开关——声明后超长输入变成有界裁剪或
                              # 记录级 context_overflow 拒绝，而不是撞端点报错烧熔断连击；
                              # 值按部署实测保守声明（欠声明恒安全），每个会被调用的 profile
                              # 都声明（第 6、18 章；报表对账读 report.budget，第 8 章）
price_per_mtok_in = 0.6       # 理由：没有单价就没有成本账，预算无从谈起
price_per_mtok_out = 1.8

[llm.judge_a]                 # 评审团成员：三个不同家族的中档模型
provider = "anthropic"
base_url = "https://api.anthropic.com"
model = "claude-sonnet-5"
api_key_env = "LABELKIT_KEY_JUDGE_A"
max_concurrency = 8
supports_structured_output = true
context_window = 200000
price_per_mtok_in = 3.0       # 理由：成本账按 profile 计——漏配单价的 profile 不进 est_cost_usd，
price_per_mtok_out = 15.0     # 而本配置的调用大头恰在评审团，漏配它们成本账就废了

[llm.judge_b]
provider = "openai_compatible"
base_url = "https://llm-gw.example.com/v1"
model = "deepseek-v3"
api_key_env = "LABELKIT_KEY_DEFAULT"
max_concurrency = 8
supports_structured_output = true
context_window = 65536
price_per_mtok_in = 0.3
price_per_mtok_out = 1.1

[llm.judge_c]
provider = "openai_compatible"
base_url = "https://llm-gw.example.com/v1"
model = "glm-5.2"
api_key_env = "LABELKIT_KEY_DEFAULT"
max_concurrency = 8
supports_structured_output = true
context_window = 131072
price_per_mtok_in = 0.4
price_per_mtok_out = 1.6

[llm.fixer]                   # 专职修 JSON 的便宜小模型
provider = "openai_compatible"
base_url = "https://llm-gw.example.com/v1"
model = "qwen2.5-7b-instruct"
api_key_env = "LABELKIT_KEY_DEFAULT"
max_concurrency = 16
supports_structured_output = true
context_window = 131072
price_per_mtok_in = 0.05
price_per_mtok_out = 0.1
```

**project.toml**——按批次实例化（`0707` 是批次号）：

```toml
schema_version = 1

[run]
input = "./data/2026-W28"
output = "./out/ime-intent-0707.jsonl"   # 理由：产物带批次号，永不覆盖历史
modality = "text"
batch_size = 256
seed = 42                                # 理由：固定 seed，任何一批都可精确复跑对账
fatal_error_threshold = 10               # 理由：生产端点稳定，坏了就该快点死（默认 20 偏宽容）

[input]
text_field = "instruction"
on_bad_line = "fail"                     # 理由：上游是受控管道，坏行=上游事故，快速失败

[dedup]
scope = "global"
minhash_threshold = 0.80                 # 理由：短文本适当收紧（教程二的数据画像支持这个值）

[quality]
mode = "pairwise"                        # 理由：批内优选语义天然配 top_ratio；成本 2N 与准则数无关
                                         #（本 rubric 两条准则时与 pointwise 持平，准则更多时开始占优）
rounds = 4
selection = "top_ratio"
top_ratio = 0.7                          # 理由：每批稳定保留 70%，产量可预期（对下游承诺产能时的关键性质）
judges = ["judge_a", "judge_b", "judge_c"]   # 理由：三家族评审团，稀释单模型口味（成本 ×3，已论证）
both_orders = true                       # 理由：位置偏差系统性消除（成本再 ×2；本任务错杀代价高，值）
rubric = "inline"                        # 理由：生产 rubric 必须版本化在工程文件里，不依赖包默认
judgment_reasons = false                 # 理由：rubric 已在探索期定稿，全量跑省下理由 token

[rubric]
name = "ime-intent-v1"                   # inline rubric 必填的标识，写入每条输出记录的 _meta.run.rubric
# [[rubric.criteria]] …（教程二定稿的那两条准则，此处省略）

[annotate]
llm = "default"
instruction = """……（探索期定稿版，含边界规则）"""
examples = [ … ]                         # 3 条边界示例
self_consistency = 3                     # 理由：输出以枚举字段为主，投票收益已在小样本验证

[verify]
enabled = true
judges = ["judge_a", "judge_b", "judge_c"]   # 理由：入库前的最后闸门用评审团
                                         # （judges 非空时 verify.llm 被替代，无须设置，v1.5 起也不再校验其存在）
policy = "drop"                          # 理由：数据管够，宁缺毋滥；rounds 语义简单利于审计

[trace]
enabled = true
channels = ["quality", "verify", "schema"]
content = "refs"                         # 理由：审计底账常开；refs 档不含数据内容，合规无虞

[output]
meta_mode = "sidecar"                    # 理由：主输出交付纯净结构；_meta 旁车归档供审计
passthrough_fields = ["source", "ts"]
rejects = "refs"
schema_path = "./schema/ime-intent.v3.json"  # 理由：Schema 独立文件、独立版本号
max_repair_attempts = 2
repair_llm = "fixer"                     # 理由：修 JSON 不需要智力，小模型省钱
```

这套配置的单条成本 ≈ 探索期默认档的 **6 倍**——quality 12N（评审团 ×3 × 双序 ×2）+ annotate 3N（SC ×3）+ verify 3N（评审团 ×3）= 18N，对比探索期默认档的 quality 2N + annotate 1N = 3N。所以 23.1 那张表才强调：**这些选项每一个都要先在小样本上论证收益，再进生产**。反过来，预算敏感的生产线砍掉 `both_orders` 和 verify 评审团（改单 judge），成本回到约 3 倍档（6N+3N+1N = 10N）。

## 23.3 运行纪律：脚本化的四步

```bash
#!/usr/bin/env bash
set -euo pipefail
set -a && source /etc/labelkit/.env && set +a
BATCH=0707

# ① 体检（配置 + 连通）；validate 失败 = 配置坏了，直接停
uv run labelkit validate --config config.toml --project project.toml --probe \
  | tee out/probe-$BATCH.log
grep -q "FAIL" out/probe-$BATCH.log && { echo "probe failed"; exit 1; }   # probe 失败不改退出码，要自己 grep

# ② 预算核对：估算调用数落在预期区间才放行
uv run labelkit run --config config.toml --project project.toml \
  --dry-run --output out/dryrun-$BATCH.jsonl

# ③ 全量，--strict 让"有淘汰"可被调度系统感知
#    注意：脚本开头有 set -e，必须用 `|| rc=$?` 吸收非零退出码——
#    否则退出码 1（有淘汰，分诊信号）会让脚本在此行直接终止，步骤④永远不会执行
rc=0
uv run labelkit run --config config.toml --project project.toml --strict || rc=$?

# ④ 归档：产物 + 账本 + trace 一起进对象存储（trace 下次运行会被截断！）
tar czf archive/ime-intent-$BATCH.tgz out/ime-intent-$BATCH.*
exit $rc
```

退出码约定回顾（第 15 章）：`0` 全过；`1` 有淘汰（strict）——**不是事故，是分诊信号**：让调度系统把它标黄，人看一眼 rejects 决定是否放行；`2/3/4` 才是红色告警。

## 23.4 周报要看的五个指标

生产化的本质是把「看账」变成例行公事。每批跑完从 report.json 提取：

| 指标 | 取处 | 异动含义 |
|---|---|---|
| 淘汰率结构（dup/lowq/verify/failed 各占比） | counts | lowq 突升 = 数据分布漂移或上游质量事故；failed 突升 = 端点/Schema 问题 |
| 聚合分直方图形状 | quality.aggregate_histogram | 整体左移 = 来源质量下滑（top_ratio 模式下淘汰率不变但货变差，**只有直方图能暴露**） |
| judgment_failures 率 | quality | >5% = 裁决链路退化（模型更新了？） |
| resolved_at.l3_* 占比 | schema_engine | 上升 = 模型输出纪律退化，或 Schema 被人改复杂了 |
| est_cost_usd / emitted | llm_usage 各 profile 的 est_cost_usd **求和** ÷ counts.emitted | 单条成本，预算的生命线。est_cost_usd 只对配了单价的 profile 出现——所以 23.2 里每个会被调用的 profile 都配了价 |

`config_digest` / `project_digest` 写进周报——每一批产物都能精确回答「这是哪套配置跑出来的」。

## 23.5 变更管理：改配置的规矩

1. **rubric、instruction、Schema、阈值的任何变更**都走教程二的流程：`--limit` 小样本 + 同 seed 对比，收敛了才进生产；
2. Schema 变更升版本号（`ime-intent.v3.json` → `v4`），主输出文件名同步换代——下游靠文件名就能对齐结构版本；
3. 换模型 = 重新小样本验证 rubric 与 instruction（不同模型对同一提示词的行为差异是真实存在的）；
4. 每次变更后的第一批，人工抽查 rejects 与 20 条主输出——指标正常不等于口径没漂。

## 23.6 全书方法论的一页总结

```
选口径（rubric/instruction 说清楚"好"是什么）
  → 小样本迭代（--limit + trace + 同 seed 对比）
    → 画线（直方图 + rejects 双材料）
      → 按需加固（评审团/双序/SC/verify，逐项论证）
        → 全量（--strict + 归档 + 五指标周报）
          → 漂移时回到第一步
```

这条环就是 LabelKit 的正确打开方式：工具负责把每一步的证据摆到你面前，判断力永远是你的。
