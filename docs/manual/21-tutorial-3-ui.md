# 第 21 章　教程三：UI 截图标注全流程

> **难度：★★★☆☆**
> 舞台：`examples/ui`——6 组手机屏幕数据，藏了三个「机关」：一对跨子目录的文件、一条精确重复、一棵没有配图的孤儿树。
> 目标：走通「配对 → 双通道去重 → 视觉分类 → 锦标赛打分 → 视觉标注 → 独立评审」的完整 UI 管线（截图 + 控件树格式能开的算子全开；generate 仅文本模态），学会读多模态工程的账。

## 21.1 数据长什么样

```
examples/ui/data/
├── uitree_1.jsonl  image_1.png     # 登录页
├── uitree_2.jsonl  image_2.png     # 设置页
├── uitree_3.jsonl  image_3.png     # ← 机关②：与 1 号完全相同的副本
├── uitree_4.jsonl  image_4.png     # 删除确认弹窗
├── uitree_9.jsonl                  # ← 机关③：孤儿树，没有 image_9
└── sub/
    ├── uitree_5.jsonl  image_5.png # ← 机关①：整对都在子目录里（首页）
```

工程配置要点（`project.toml`）：`modality="ui"`、`batch_size=8`、**classify 视觉分类**（auth / dialog / browse / other 四类封闭集，dialog 类挂了一条按类标注指令覆盖，第 24 章）、pairwise 打分（`rounds=2`，无阈值——只打分不过滤）、标注 + **verify 评审（policy="repair"）**、trace 开。用户 Schema 要求四个字段：`screen_category`（枚举）、`page_title`、`interactive_elements`（对象数组，含 role/label/bounds）、`description`（≤200 字）。

## 21.2 跑起来，先读警告

```bash
cd examples/ui && mkdir -p out
set -a && source ../../.env && set +a
uv run labelkit run --config ../config.toml --project project.toml
```

启动阶段就有两条值得停下来读的输出：

```
warning: project.toml:[verify].llm: verify.llm 与 annotate.llm 使用同一模型 "glm-5.2"，
         存在自增强偏差风险（3.7.2）
WARN  ingest  batch=0 ingest.missing_pair index=9 present=tree file=uitree_9.jsonl
```

第一条：示例环境只有一个模型可用，评审与标注同源——工具提醒你这有偏差风险但不拦着（第 13 章）。第二条：机关③被抓——9 号只有树没有图，按默认 `on_missing_pair="skip"` 跳过并计数。

终账：

```
scanned=6  ingested=5  bad_input=1  dropped_dup=1  emitted=4
```

对账：6 个 index 被扫到 → 9 号缺对（bad_input=1）→ 5 条记录成立 → 3 号被去重（dropped_dup=1）→ 4 条走完全程。**机关①的子目录对呢？**`sub/` 里的 5 号安然在列——UI 配对的 index 命名空间覆盖整个目录树（第 5 章），跨子目录不是问题。

## 21.3 去重账本：双通道判定

report.json 的去重节：

```json
"dedup": {"exact": 1, "near_text": 0, "near_image": 0, "near_both": 0,
          "clusters": 1, "image_decode_failures": 0}
```

3 号是**精确重复**（树、图字节级相同 ⇒ 规范序列化哈希相同），精确层直接拦下，不需要走 pHash。rejects 里它的案底：

```json
{"_meta": {"id": "40f47f09487dc7cc", "source": {"file": "uitree_3.jsonl", "pair_index": 3, ...},
           "stage": "dedup", "reason": "exact", "errors": [], "label": null}}
```

（行尾的 `label` 是 v1.7 分类标签落点——它死在去重工位、还没走到分类，所以是 null。）

有意思的细节：它的 id 与 1 号（被保留者）**完全相同**——UI 记录的 id 是内容哈希（树字节+图字节），完全相同的文件对必然同 id。这正是「确定性 id」设计的体现。

而 1、2、4、5 号是同一 App 的四种**不同页面**（登录/设置/弹窗/首页），近似层对它们全程无感——实测各树对 Jaccard 仅 0.23~0.28、各图对 pHash 距离 26~40，树、图两条通道都远未到阈值（0.85 / 8），所以本数据集改成 `"tree"` 重跑也不会误杀。`ui_dup_requires="both"` 的价值要在真实的**模板化数据**上才显现：同一界面模板承载不同内容时（同一张表单空着 vs 填了一半），树高度相似而画面不同，`"both"` 要求两条通道同时近似才判重，能把这类「结构像、状态不同」的有价值样本保下来；配成 `"tree"` 它们就危险了（第 9 章的核心告诫）。

## 21.4 分类与打分账本：类池切开后的锦标赛

先看分类工位（视觉调用：截图 + 控件树同入提示词）。四条幸存记录被分进三个池——`classify.classes` 计数：auth 1（登录页）、dialog 1（删除确认弹窗）、browse 2（设置页、首页），`fallback_count=0`。trace 里每条 `classify.decision` 都带理由：「页面包含手机号输入框、验证码输入框……是典型的账号登录认证界面」。

分类会连带改变打分的地形：**classify 启用时 pairwise 按类分池比较**（同类才同台，spec 3.4.3），报告的 `quality` 节随之多出 `by_class`：

```json
"quality": {"mode": "pairwise_bt", "rounds": 2, "judgment_failures": 0,
            "aggregate_histogram": {"0.4-0.5": 1, "0.5-0.6": 3, "…": 0},
            "per_criterion_mean": {"interaction_richness": 0.5, "screenshot_readability": 0.5,
                                    "state_completeness": 0.5, "tree_screen_consistency": 0.5},
            "per_criterion_tie_rate": {"interaction_richness": 0.0, "screenshot_readability": 0.5,
                                        "state_completeness": 0.0, "tree_screen_consistency": 0.5}}
```

三个读数：

1. 四条准则的均值**全是 0.5**——这不是巧合，是 pairwise 的数学性质：分数是池内百分位，`(rank−1)/(N−1)` 的均值恒为 0.5。**pairwise 报告里的 per_criterion_mean 没有跨工程比较意义**（pointwise 才有）；
2. auth、dialog 两个池各只有 1 条记录——**孤记录无对手可比**，四条准则全记中间值 0.5、聚合 0.5（看它们的 `_meta.scores` 就是清一色 0.5）。这是「按类分池 × 小批量」的固有形状：类切得越细，池越小，pairwise 越没得比。真在意类内排序就攒大批量，或换 pointwise；
3. 真正的比较发生在 browse 池：设置页 vs 首页，`rounds=2` 正反各比一次。首页赢下交互丰富度（两轮全胜——轮播图 + 推荐卡片 + 换一批按钮）与截图可读性（一胜一平，胜局的理由是「轮播图和推荐卡片等图文内容，视觉层次更丰富」——两张程序化生成的图其实同样清晰，这条准则在本数据集上的裁决更像口味，`tie_rate=0.5`）；设置页赢下树一致性（一胜一平）；状态完整性最有意思：两轮的赢家恰好都是当轮坐在 **B 位**的那条（`tie_rate=0.0`，但两边各拿 0.5）——正反双序把这份位置偏好对消掉了，这正是 `rounds=2` 正反各一次的价值（第 10 章）。

看池内胜者首页的 `_meta.scores`（分数是本次运行的快照，逐次会有浮动）：

```json
{"screenshot_readability": 1.0, "tree_screen_consistency": 0.0,
 "state_completeness": 0.5, "interaction_richness": 1.0,
 "__aggregate__": 0.5555555555555556, "mode": "pairwise_bt", "batch_no": 1, "pool": "browse"}
```

验算聚合分（default:ui 的 tree_screen_consistency 权重是 1.5，其余 1.0）：
(1.0 + 0.0×1.5 + 0.5 + 1.0) / 4.5 = 2.5 / 4.5 = **0.556** ✓。设置页则是镜像的 (0.0 + 1.0×1.5 + 0.5 + 0.0) / 4.5 = 0.444——`scores.pool` 字段自述了它们在哪个池里比的（第 24 章）。

## 21.5 标注与评审：看图说话，独立复核

标注调用把**截图（base64）+ 序列化控件树**一起喂给视觉模型（第 11 章）。1 号的产出（本次运行原文）：

```json
{"screen_category": "login", "page_title": "登录",
 "interactive_elements": [
   {"role": "input", "label": "手机号输入框", "bounds": [32, 160, 368, 216]},
   {"role": "input", "label": "验证码输入框", "bounds": [32, 240, 368, 296]},
   {"role": "button", "label": "获取验证码", "bounds": [240, 248, 360, 288]},
   {"role": "button", "label": "登录", "bounds": [32, 340, 368, 396]},
   {"role": "checkbox", "label": "同意用户协议", "bounds": [32, 420, 50, 438]},
   {"role": "link", "label": "我已阅读并同意《用户协议》", "bounds": [60, 420, 340, 440]}],
 "description": "手机号验证码登录页面，用户输入手机号并获取验证码，勾选同意用户协议后点击登录按钮完成登录。"}
```

4 号弹窗走的则是 `[class.dialog.annotate]` 的**按类指令**（「务必包含全部按钮及其 bounds……说明该弹窗要求用户做什么决定」）——同一工位、按类换词（第 24 章）。verify 用 judge profile 按三个内置维度复核（遵循指令 / 与截图树一致 / 字段语义），trace 里每轮一条 `verify.verdict` 事件、critiques 全文可读——4 号那条的评审意见原文就写着「可交互元素列表(interactive_elements，含全部按钮及bounds)……无遗漏」：评审读的任务指令同样是按类覆盖后的版本。本次运行 4 条全部一次通过：`_meta.verification = {"verdict": "pass", "rounds": 1}`、`dropped_verify=0`。若有 fail，`policy="repair"` 会带着批评意见让标注模型返工一轮（第 13 章走查过完整轨迹）。

## 21.6 多模态工程的成本结构

```json
"llm_usage": {"default": {"calls": 10, "prompt_tokens": 8545, ...},
              "judge":   {"calls": 4, "prompt_tokens": 3097, ...}},
"timing": {"wall_s": 27.185, "per_stage_s": {"dedup": 0.011, "classify": 6.5, "quality": 6.5,
                                               "annotate": 6.4, "verify": 7.8}}
```

default 档的 10 次调用 = 分类 4 + 裁决 2 + 标注 4（browse 池 2 轮正反共 2 次裁决调用；孤池零调用——分池顺带省了钱）。注意 prompt_tokens 的量级：**每次调用平均约 830 token**——截图就是这么贵（按分辨率折 token）。v1.11 起报告里还有一个更直接的图片成本读数：`report.budget.image_cost` 给出按真实 usage 校准的**每图 token 成本终值**（本次真跑 default 档 240；judge 档样本不足 8 个、维持先验读数 1882——校准机制见第 8、16 章）。这也是为什么 `max_image_px` 是 UI 工程的重要成本旋钮（第 6 章，v1.11 另有 `default_image_px` 把「日常工作点」和「上限」分开声明）：2048 → 1536 能省一大截，但先确认小字仍可读。4 条记录 14 次调用不到半分钟——UI 管线天生比纯文本慢，`--limit` 试跑的价值更大。

## 21.7 迁移到你自己的截图数据

1. 按第 5 章排布文件（`uitree_<N>.jsonl` + `image_<N>.png`，编号全目录唯一）；
2. 树导出字段对照 5.2 的兼容映射表（第 5 章），对不上的在采集侧重命名；
3. profile 必须 `supports_vision = true`（启动就会查）；
4. 从 `rubric = "default:ui"` 起步，跑小样本读 trace 里的裁决理由，再决定要不要自定义准则；
5. `ui_dup_requires` 保持 `"both"`，除非你明确想按模板压缩;
6. 数据金贵，verify 用 `policy = "repair"` 给一次返工机会。
