# 第 19 章　教程一：最小可用的文本标注工程（从零搭起）

> **难度：★☆☆☆☆**
> 目标：不抄示例，从空目录亲手搭出一个能跑的标注工程，理解每一行配置为什么存在。
> 本教程刻意关掉一切可关的东西——只留「接入 → 标注 → 输出」的裸流程。

## 19.1 建目录、放数据

```bash
mkdir -p my-first-task/{data,out} && cd my-first-task
cat > data/input.jsonl <<'EOF'
{"q": "怎么把 PDF 转成 Word"}
{"q": "推荐三本关于时间管理的书"}
{"q": "帮我给房东写一条退租通知，下月底搬走"}
{"q": "太阳到地球的距离是多少"}
{"q": "把这句话改得客气点：这个方案不行"}
EOF
```

注意正文字段叫 `q`——不是默认的 `text`。这是故意的：让你第一次就学会对齐 `text_field`。

## 19.2 写 config.toml（照第 6 章，换成你的端点）

```toml
schema_version = 1

[llm.default]
provider = "anthropic"                      # 或 "openai_compatible"，看你的服务
base_url = "https://api.z.ai/api/anthropic"
model = "glm-5.2"
api_key_env = "LABELKIT_ZAI_KEY"
max_concurrency = 4
supports_structured_output = true
```

## 19.3 写 project.toml：能少则少

```toml
schema_version = 1

[run]
input = "./data/input.jsonl"
output = "./out/labels.jsonl"
modality = "text"

[input]
text_field = "q"                  # ← 对齐数据里的字段名

[dedup]
enabled = false                   # 教程刻意关闭：这批数据没有重复
[quality]
enabled = false                   # 教程刻意关闭：先不打分

[annotate]
instruction = """
你是用户请求的分类员。判断这条请求属于哪一类：
- ask: 询问事实或建议
- write: 要求代写或改写文本
- other: 其余
类别拿不准时选 other。另给出一个不超过 10 个字的主题短语。
"""

[output]
schema_inline = """
{
  "type": "object",
  "properties": {
    "kind": {"type": "string", "enum": ["ask", "write", "other"]},
    "topic": {"type": "string"}
  },
  "required": ["kind", "topic"],
  "additionalProperties": false
}
"""
```

三点观察：

- `quality` 关了、`annotate` 开着——合法（第 4 章的约束是「至少开一个」）；
- Schema 里 `kind` 的枚举与 instruction 里的类别一一对应——**指令定义判据，Schema 锁定取值**，两边名字必须一致；
- 没写的 `[trace]`、`[verify]`、`[generate]` 全按默认（关）。

## 19.4 体检 → 运行

```bash
set -a && source /path/to/.env && set +a
uv run labelkit validate --config config.toml --project project.toml --probe
uv run labelkit run --config config.toml --project project.toml
```

stderr 尾部应见到：

```
   scanned=5  ingested=5  bad_input=0  generated=0
   dropped_dup=0  dropped_lowq=0  dropped_verify=0  failed=0  emitted=5
INFO  run  batch=0 run.end exit_code=0
```

五进五出——没有任何治理工位开着，这是一条「纯标注」流水线。

## 19.5 看结果

```bash
jq -c 'del(._meta)' out/labels.jsonl
```

```json
{"kind": "ask", "topic": "PDF转Word"}
{"kind": "ask", "topic": "时间管理书单"}
{"kind": "write", "topic": "退租通知"}
{"kind": "ask", "topic": "日地距离"}
{"kind": "write", "topic": "措辞润色"}
```

（你的 topic 措辞可能略有不同——那是模型的自由发挥空间；`kind` 则被枚举锁死，只可能是三者之一。）

再看一条完整的 `_meta`：`scores` 是 `null`（quality 关了）、`dedup` 是 `null`（dedup 关了）、`verification` 同理，`annotation.attempts` 大概率是 1。**`_meta` 的键始终齐全，关掉的工位一律置 `null`——哪些值非空，忠实反映你开了哪些工位。**（给 jq 用户提个醒：`._meta.scores` 会取到 null 而不是报缺键，判断工位是否开启要比较 `!= null`。）

## 19.6 三个一分钟实验

**① 把 `text_field` 改回 `"text"` 再跑**——收获 5 条 `ingest.bad_line` 警告，随后运行以退出码 3 终止：

```
InputError: 无任何合法记录: data/input.jsonl（scanned=5 bad_input=5 missing_pair=0 index_conflict=0）
```

「一条合法记录都没有」是输入错误，不是一次成功的空跑。（设 `input.on_bad_line = "fail"` 会更早：第一条坏行处就停。）这是第 5 章说的头号坑，现在你亲眼见过它的症状了。

**② 往数据里加一行重复，把 dedup 打开**——`emitted` 变回 5，`dropped_dup=1`，rejects 里多出一条 `"stage": "dedup", "reason": "exact"` 的案底（rejects 里承载判重类别的字段是 `reason`；`kind` 是主输出 `_meta.dedup.kind` 的字段名，恰好也和本教程 Schema 里的业务字段同名，别混淆）。

**③ 在 instruction 里删掉「类别拿不准时选 other」再跑**——观察边界请求（比如第 5 条既像 write 又像 ask）的标注是否开始摇摆。边界规则的价值就在这里。

## 19.7 你学会了什么

- 一个工程的最小骨架：`[run]` 三件套 + `annotate.instruction` + `output.schema_inline`；
- `text_field` 必须与数据对齐，错了是全员坏行、以「无任何合法记录」退出码 3 终止，而不是静默错标；
- 开关哪个工位，`_meta` 和 counts 就长什么样。

下一篇教程把 quality 打开，学习画质量线——那是 LabelKit 从「能用」到「用好」的第一道分水岭。
