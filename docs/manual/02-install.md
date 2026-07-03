# 第 2 章　安装与环境准备

> 本章带你从一台干净的机器走到「`labelkit --help` 能跑」，并把 LLM 服务的密钥安置妥当。
> 全程大约 10 分钟，其中大部分时间在等依赖下载。

## 2.1 你需要准备什么

| 项目 | 要求 | 说明 |
|---|---|---|
| 操作系统 | macOS / Linux（Windows 建议 WSL） | LabelKit 是纯 Python 命令行工具，无系统级依赖 |
| Python | **≥ 3.11** | 用到了 3.11 才进标准库的 `tomllib`；用 uv 管理时无需预装 |
| 包管理器 | 推荐 [uv](https://docs.astral.sh/uv/)；pip 亦可 | 本手册全部示例用 uv |
| 一个 LLM API | OpenAI 兼容接口 **或** Anthropic 接口 | 这是 LabelKit 唯一的外部依赖 |
| API 密钥 | 放在环境变量里 | 密钥的**名字**写进配置，**值**只从环境读取 |

关于 LLM 服务的选择：LabelKit 支持两类接口协议——`openai_compatible`（绝大多数国产模型网关、vLLM、各类中转都兼容）与 `anthropic`。如果你要处理 UI 截图（`ui` 模态），所用模型必须支持视觉输入；如果想让结构化输出更稳，选支持 JSON Schema 约束输出的模型更好（第 14 章会解释为什么「不支持也没关系」）。

## 2.2 安装

LabelKit 以源码工程形式分发。拿到代码目录后：

```bash
cd LabelKit
uv sync            # 创建虚拟环境（Python 3.12）并安装全部依赖
```

`uv sync` 会读取 `pyproject.toml` 与锁文件 `uv.lock`，装出一个完全可复现的环境。全部第三方依赖只有 7 个，都很轻：

| 依赖 | 用途 |
|---|---|
| `httpx` | 异步 HTTP，调 LLM API |
| `jsonschema` | 校验你的输出结构 |
| `datasketch` | MinHash-LSH 近似去重 |
| `Pillow` + `imagehash` | 截图缩放与感知哈希（图像去重） |
| `json-repair` | 确定性修复 LLM 输出的破损 JSON |
| `numpy` | Bradley-Terry 质量模型拟合 |

没有任何「框架级」依赖——没有 LangChain，没有数据库驱动，没有 Web 框架。

验证安装：

```bash
uv run labelkit --help
```

看到三个子命令 `run` / `validate` / `rubric` 的帮助就说明装好了。

> **小贴士：`uv run` 前缀**
> 本手册示例统一写 `uv run labelkit ...`，它保证命令在项目虚拟环境里执行。
> 如果你习惯先 `source .venv/bin/activate`，之后直接写 `labelkit ...` 也完全等价。

## 2.3 安置 API 密钥

LabelKit 有一条隐私红线：**除了 API 密钥，不使用任何环境变量；密钥只以「环境变量名」出现在配置文件里，真实值只从环境读取**。这样配置文件可以放心提交进代码仓库，密钥永远不会出现在文件、日志或报告里。

举例：假设你的 LLM 网关密钥打算叫 `LABELKIT_KEY_DEFAULT`。

第一步，在 `config.toml` 里**声明名字**（下一章细讲这个文件）：

```toml
[llm.default]
provider = "openai_compatible"
base_url = "https://llm-gw.example.com/v1"
model = "qwen2.5-vl-72b-instruct"
api_key_env = "LABELKIT_KEY_DEFAULT"     # ← 写的是变量名，不是密钥本身
```

第二步，让这个环境变量在运行时存在。最省事的做法是一个**不入库**的 `.env` 文件：

```bash
# .env（记得加进 .gitignore）
LABELKIT_KEY_DEFAULT=sk-xxxxxxxxxxxxxxxx
```

运行前加载它：

```bash
set -a && source .env && set +a
uv run labelkit run --config config.toml --project project.toml
```

`set -a` 让 `source` 进来的变量自动导出给子进程；`set +a` 恢复默认。你也可以用 direnv、系统 keychain 或 CI 的 secret 机制——LabelKit 不关心值从哪来，只在启动时检查「这个名字的变量存在且非空」。

> **常见坑**：忘了加载 `.env` 就运行，会在启动时收到清晰的配置错误并以退出码 2 结束：
>
> ```
> ConfigError: 1 个配置错误（全量聚合反馈）
> config.toml:[llm.default].api_key_env: 环境变量 "LABELKIT_KEY_DEFAULT" 未设置或为空
> ```
>
> 注意：只有**被启用的算子实际引用到的** profile 才需要密钥。配置里多写几个备用 profile 没关系，不用它就不查它的密钥。

## 2.4 验证连通性：先探测，再开跑

装好之后，强烈建议先用 `validate --probe` 做一次连通性探测——它会对每个被引用的 profile 发一次 1-token 的试调用：

```bash
uv run labelkit validate --config config.toml --project project.toml --probe
```

成功时输出形如：

```
配置校验通过
probe default: ok model=glm-5.2 latency_ms=7291
```

**为什么这一步重要**：密钥/权限错误（HTTP 401/403）会在第一次调用时**立即熔断**、以退出码 4 终止——这类故障不会自愈，工具不再浪费一分钱。但另一类配置事故：模型名拼错、路径不对（HTTP 400/404）仍按「连续 N 次致命错误」熔断（默认 20），小批量试跑可能攒不满阈值，表现为记录纷纷失败而运行「成功」结束。一次 30 秒的 probe 把两类问题都提前暴露。养成习惯：**新环境、换密钥、换网关之后，先 probe**。

## 2.5 目录怎么摆

一个典型的工作目录长这样（第 5 章详细讲数据排布）：

```
my-labeling-task/
├── config.toml          # 工具配置：LLM 从哪来（跨任务复用，很少改）
├── project.toml         # 工程配置：这一次任务怎么跑（每个任务一份）
├── .env                 # 密钥（不入库）
├── data/
│   └── input.jsonl      # 输入数据
└── out/                 # 输出目录（运行前建好：mkdir -p out）
```

两份配置放哪里都行——CLI 用 `--config` / `--project` 显式指路径；`project.toml` 里的相对路径（输入、输出、外部 Schema 文件）相对**当前工作目录**解析，所以建议在工程目录里运行命令。

> **注意**：输出目录不会自动创建。运行前 `mkdir -p out`，否则启动时的配置校验会直接拦下，报配置错误并以退出码 2 结束：`project.toml:[run].output: 输出父目录不存在或不可写`。（退出码 4 的「输出路径不可写」只针对运行**中途**写输出失败的情形，比如磁盘写满或权限被中途收回。）

## 2.6 给开发者：运行测试套件

如果你拿到的是完整源码仓库，可以跑一遍测试确认环境健康：

```bash
uv run pytest -q -m 'not integration'    # 离线套件，秒级完成，不联网
```

`tests/integration/` 下还有一组打真实 LLM 端点的集成测试，需要 `.env` 里有密钥，平时可以不管它。

下一章我们直接跑通第一个完整例子。
