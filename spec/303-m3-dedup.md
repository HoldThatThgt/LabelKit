## 3.3 M3 去重 dedup

### 3.3.1 职责与边界

**做：**对批内（或全局作用域）记录执行精确去重与近似去重，UI 模态叠加图像 pHash；为重复记录标记 `dropped_dup` 状态并记录簇归属；维护运行内存中的去重索引。 
**不做：**默认配置下不调用任何 LLM/Embedding API、不做语义级判重（v1.2 例外：`dedup.semantic = true` 时经 M9 `embed()` 调用 embedding API 执行可选第④级语义判重，3.3.3——仍不调用对话补全 LLM）；不物理删除记录（状态标记，由 M10/M11 决定去向）；不跨运行持久化索引（无状态约束）。

### 3.3.2 输入 / 输出

| 方向 | 内容 |
|---|---|
| 输入 | `list[PipelineItem]`（一个批）；运行级 `DedupIndex`（scope=global 时跨批共享，进程内存）。 |
| 输出 | 同一列表，重复项 `status="dropped_dup"`、`item.dedup = DedupInfo(kind, cluster_key, kept_id)`；非重复项 `DedupInfo(kind="unique")` 并入索引。 |

### 3.3.3 算法

| 层 | 算法 | 判重条件 |
|---|---|---|
| ① 精确 | 去重键 = `sha256(dedup_text)`。`dedup_text`：文本模态为抽取文本经 NFC 规范化、连续空白折叠为单空格、strip 后的字节；UI 模态为 UI 树规范序列化（4.3 节 `UITree.serialize()`，不含坐标抖动位——坐标按 `dedup.bounds_quantize_px`（默认 4px）量化后参与序列化）。哈希入 `set`，O(1) 查询。 | 哈希相等 |
| ② 近似 | MinHash：`dedup_text` 的字符 n-gram（默认 n=5，对中英混合文本鲁棒；空白折叠后取滑窗）shingle 集合 → 128-permutation 签名 → `datasketch.MinHashLSH(threshold=0.85)` 查询候选 → 对候选精算签名估计 Jaccard，≥ 阈值判重。首见者入索引（first-writer-wins，保留簇内最早记录，与 Lee et al. 的 cluster-keep-one 策略一致 [3]）。 | 估计 Jaccard ≥ `dedup.minhash_threshold` |
| ③ 图像（仅 UI 模态） | 截图缩放解码 → 64-bit pHash（imagehash 默认 DCT 实现）→ 与索引中全部已保留 pHash 求汉明距离（64-bit 异或 popcount，50 万条线性扫描 < 100ms/查询，可接受；实现里按 16-bit 前缀分桶加速）。 | 汉明距离 ≤ `dedup.image_phash_max_distance`（默认 8） |
| ④ 语义（可选） | `dedup.semantic = true` 时启用（默认 false，5.2）：`dedup_text` → 经 config.toml `[embedding.<name>]` profile（由 `dedup.semantic_embedding` 引用，5.1）取句向量（M9 `embed()`，3.9.2；向量 L2 归一化后余弦 = 点积）→ 与索引中全部已保留向量求余弦相似度（批内与全局索引通查，实现为向量矩阵化点积的线性扫描（pHash 的 16-bit 前缀分桶依赖汉明位结构，不适用于稠密向量），规模上限见 2.6 注）→ 达阈判重，kind=`"near_semantic"`（4.2）；未判重者向量入索引（first-writer-wins，同②）。执行序在①②③之后，仅当前级已构成判重（含 UI 模态合成判定成立）时才短路——UI 模态 `ui_dup_requires="both"` 下③单独命中不构成判重、不短路④；仅对尚未判重的记录发起 embedding。UI 模态：作用于树规范序列化文本（与①②同一 `dedup_text`），在合成判定中视同 tree 层命中（见下段）。成本：每条参检记录 1 次 embedding 调用（走 M9 计量与重试，3.9.3）。 | 余弦相似度 ≥ `dedup.semantic_threshold`（默认 0.95，SemDeDup 论文的高相似区间 [26]） |

UI 模态合成判定：`dedup.ui_dup_requires = "both"`（默认，树近似重 **且** 图近似重才判重——最保守，避免同一界面模板不同内容被误杀）| `"tree"` | `"image"`。精确层（①）命中则无条件判重。生成样本回流批同样经过本模块，天然实现 Self-Instruct 的「新样本与已有样本相似度过滤」[18]（以 MinHash-Jaccard 替代其 ROUGE-L，二者同为 n-gram 重叠度量，MinHash 可索引化）。

第④级（语义，v1.2）与合成判定的关系：UI 模态下④作用于树规范序列化文本，在 `ui_dup_requires` 合成判定中**视同 tree 层命中**——"both" 下需（②或④）与③同时命中才判重；"tree" 下④单独命中即判重；"image" 下④不参与判定。判重由④贡献时记 `kind="near_semantic"`（④与③同时命中仍记 `near_both`）。④ 与 ② 的分工：② 抓 n-gram 重叠的表层改写，④ 抓措辞不同语义相同的深层重复（[26] 的动机），两级独立可关。

**序列记录（v1.8，S10）。**stream 模式下抵达本模块的判重单元是 episode（`record.kind = "sequence"`，3.14）——episode 级重复 =「同样的操作流程」；成员帧不会单独抵达（链序 segment 在 dedup 之前，成员帧已置 absorbed / dropped_noise，3.10.3），帧级判重语义在 stream 模式下有意留空（连续 UI 帧上帧级判重本就失效）。四处适配，其余零改动：

- **①② dedup_text**：配方增 `kind == "sequence"` 分支（优先于模态分支）——成员逐条按其单记录配方（文本规则 / 树规范序列化，随成员模态）产出后按成员序拼接，分隔符 `"\x1e"`（ASCII Record Separator，0x1E：`isspace() == True`，而成员配方输出的规范化文本已将空白折叠为单空格、不可能含该字符——拼接串与任何单记录配方输出结构性零碰撞）；①精确与②近似两级在拼接文本上照常执行。
- **③ pHash**：对序列记录自动跳过（序列 Record 的 `image is None`——既有跳过门，零新增代码路径）；`ui_dup_requires = "both"` 下序列记录的合成判定**降级按 `"tree"` 处理**（与图像解码失败的降级路径同款，3.3.4）。
- **④ 语义**：参检判定与判种归类两处逻辑（实现为 `_semantic_participates` / `_semantic_verdict_kind`）各增序列 case——"both" 对序列走 tree-only 分支（与③的降级一致）；序列拼接文本超长导致 embedding 重试耗尽时，走既有 `embedding_failures` 跳过路径（3.3.4——该记录按①—③判定，不增新失败通道）。

**线索记录（v1.9）。**stitch 启用时抵达本模块的判重单元升维为**线索**（thread——M16 缝合后的幸存序列信封，链序 stitch 在 dedup 之前，3.10.3/3.16）：`dedup_text` 配方**机制原样**（上列 S10 序列分支零改动）——成员逐条按其单记录配方产出后按成员序以 `"\x1e"` 拼接，作用对象自然是**重绑后**的成员元组，线索级重复 =「同样的完整操作流程（含恢复段）」；被并 episode 壳（`status = "stitched"`）被既有 `status == "active"` 处理面过滤**天然排除**、不参检不入索引（absorbed 成员帧同理）——**本模块代码零改动**（T13，审计核查点 6）。

**嵌入输入预算截断（v1.11，V15）。**`dedup.semantic = true` 且所引 `[embedding.<name>]` profile 声明 `context_window` 时（0 = 未声明 = 预算关闭，行为与 v1.10 一致），第④级的 embed 输入（`dedup_text` 产物，含序列/线索拼接文本）在发起 embedding 调用前按 `embed_budget = context_window − margin`（无输出预留，3.9）截断——**确定性头部保留**（`keep = "head"` 行边界截断：嵌入语义主体在文本前部），修复该调用点完全无截断的既有缺口；截断计入 `report.budget.truncations`（6.4）。既有 `embedding_failures` 跳过路径（3.3.4）**保留为兜底**——截断之外的 embedding 失败仍按①—③判定、不增新失败通道。

### 3.3.4 API 与配置

```
class DedupStage(Stage):
    name = "dedup"
    def __init__(self, cfg: DedupConfig, index: DedupIndex): ...
    async def run(self, batch: list[PipelineItem], ctx: RunContext) -> list[PipelineItem]: ...

class DedupIndex:
    """运行内存索引：exact set[bytes] + MinHashLSH + list[(id, phash)]
       （+ dedup.semantic=true 时 list[(id, unit_vec)]，v1.2）。scope=batch 时每批重建。"""
    def probe_and_add(self, rec: Record) -> DedupInfo: ...
```

配置见 5.2 `[dedup]`。错误处理：图像解码失败 ⇒ 该记录跳过 pHash 层（按树判定）并计入 `report.dedup.image_decode_failures`；embedding 调用失败（重试耗尽，3.9.3）⇒ 该记录跳过第④级（按①—③判定）并计入 `report.dedup.embedding_failures`（仅 `dedup.semantic = true` 时可能非零，v1.2）。

**背书：**MinHash 近似去重由 Lee et al.（ACL 2022）确立为 LLM 训练数据方法学标准并证明可提升模型质量 [3]；Dolma [6]、Data-Juicer [4]、NeMo Curator [9] 的内置去重算子均为「精确哈希 + MinHash-LSH」两级结构，阈值 0.8–0.9 为通行区间。pHash 图像判重为 imagehash 库承载的工业标准做法 [9]。

### 3.3.5 输入 / 输出示例

设定：文本模态第一批共 4 条（`DedupIndex` 为空），`[dedup]` 全部取 5.2 默认值：`dedup.scope`="global"、`dedup.minhash_threshold`=0.85、`dedup.minhash_num_perm`=128、`dedup.ngram`=5；`input.text_field`="instruction"。输入 4 行 JSONL，依行序记为 r1–r4：

```
{"instruction": "帮我写一条请假条，明天上午要去医院复诊，大概十点到医院，下午两点前能回公司，请按半天事假写，语气客气一点，落款写研发部小李，谢谢", "source": "ime-log", "ts": "2026-06-30T10:12:00Z"}
{"instruction": " 帮我写一条请假条，明天上午要去医院复诊，大概十点到医院，下午两点前能回公司，请按半天事假写，语气客气一点，落款写研发部小李，谢谢　", "source": "ime-log", "ts": "2026-06-30T10:12:07Z"}
{"instruction": "帮我写一条请假条，明天上午要去医院复诊，大概十点到医院，下午两点前能回公司，请按半天事假写，语气客气一点，落款写研发部小李，谢谢 10:12", "source": "ime-log", "ts": "2026-06-30T10:14:33Z"}
{"instruction": "把这段会议纪要翻译成英文，术语保留原文", "source": "ime-log", "ts": "2026-06-30T10:15:21Z"}
```

记录 id 按 3.2.5 规则 = `sha256(canonical_json(raw))[:16]`：r1=`ff21d1c3963d17db`、r2=`350d516a359a6174`、r3=`6cd63faf65b8cfb7`、r4=`08363dbb4cd11f29`。r2 的 raw 与 r1 不同（空白、ts），id 不同——判重依据是 `dedup_text`，与 id 无关。

| 记录 | dedup_text（NFC + 空白折叠 + strip） | sha256(dedup_text) 前 16 hex | 命中层 | DedupInfo | status |
|---|---|---|---|---|---|
| r1 | 原文已是规范形，无改动（64 字） | `57e3f858bc013a54` | 无（首见：精确键 + MinHash 签名入索引） | `kind="unique" cluster_key="57e3f858bc013a54" kept_id=null` | `active` |
| r2 | 去掉行首半角空格与句尾 ` `（全角空格）后，与 r1 逐字节相同 | `57e3f858bc013a54`（与 r1 相同） | ① 精确（哈希已在 `set` 中，无条件判重） | `kind="exact" cluster_key="57e3f858bc013a54" kept_id="ff21d1c3963d17db"` | `dropped_dup` |
| r3 | 仅句尾多出 " 10:12"（70 字），空白已是单空格，无改动 | `07aaef398c499831` ≠ r1，① 未中 | ② 近似：LSH 候选 = {r1}，签名估计 Jaccard = 0.91 ≥ 0.85 | `kind="near_text" cluster_key="57e3f858bc013a54" kept_id="ff21d1c3963d17db"` | `dropped_dup` |
| r4 | 原文已是规范形，无改动（19 字） | `e49758a83ce5efec` | 无（① ② 均未中，入索引） | `kind="unique" cluster_key="e49758a83ce5efec" kept_id=null` | `active` |

r3 的真实字符 5-gram Jaccard 可精确验算：r1 折叠后 64 字 → 60 个 shingle，r3 70 字 → 66 个；交集 60、并集 66，J = 60/66 ≈ 0.909。128-permutation 签名估计值的标准差约 √(0.909×0.091/128) ≈ 0.025，本例估计值取 0.91，与阈值关系稳定。约定：`cluster_key` 取簇首（首见保留记录）的精确去重键前 16 hex，unique 记录填自身键。本批计入 `report.dedup`：`{"exact": 1, "near_text": 1, "clusters": 1}`（其余计数为 0）；r2、r3 由 M10 按 `output.rejects` 策略落 rejects 通道，不进主输出。

#### UI 模态合成判定示例（`dedup.ui_dup_requires` = "both"，默认）

待判记录：`capture/2026-07-01/b/uitree_2.jsonl` + `c/image_2.png`（pair_index=2，登录页，即 6.3 主输出示例中 `_meta.id="9f2c31ab52e08d17"` 那条）；索引中已保留同一 App 的登录页 pair_index=1（`a/uitree_1.jsonl` + `a/image_1.png`）。

| 层 | 计算 | 结果 |
|---|---|---|
| ① 精确 | `sha256(UITree.serialize(quantize_px=4))` = `1c7a0d93e2b6f458`，索引中 pair_index=1 的键为 `b2e49c05d7a1f36e` | 不相等，未中 |
| ② 树近似 | 序列化树的 MinHash 签名估计 Jaccard = 0.95 ≥ 0.85（同一登录模板，控件树几乎一致） | 命中 |
| ③ 图像 | pHash(`c/image_2.png`) = `5181a9e2bc40fcca`，簇首 pHash = `c3a5e17098d2b46f`，汉明距离 = 21 > 8（输入框已填入手机号、软键盘弹出，画面差异大） | 未命中 |
| 合成 | "both" 要求 ② 且 ③ 同时命中；仅 ② 命中 ⇒ 不判重 | `DedupInfo(kind="unique", cluster_key="1c7a0d93e2b6f458", kept_id=null)`，`status="active"` |

**设计意图：**若配置 `ui_dup_requires="tree"`，本条将因 ② 命中被判 `near_text` 丢弃——同一界面模板承载不同内容（不同账号、不同输入状态）的屏幕会被大量误杀。默认 "both" 即为规避该风险（3.3.3）；反之，树与图同时命中时记 `kind="near_both"`。
