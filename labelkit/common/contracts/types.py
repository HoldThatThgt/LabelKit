"""共享数据类型（spec ch.4）。冻结契约——未同步更新 CONTRACTS.md 前不得改动。"""
from __future__ import annotations

import base64
import io
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, Mapping

Status = Literal[
    "active",          # 存活，继续向下流动
    "dropped_dup",     # M3 判定为重复
    "dropped_lowq",    # M4 未过质量门
    "dropped_verify",  # M7 判决 fail 且 policy=drop（或修复预算耗尽）
    "failed",          # 处理错误（不可修复的 schema / provider 重试耗尽……）
    "absorbed",        # v1.8：被 episode 吸收的成员帧（M14；两个输出通道都不进）
    "dropped_noise",   # v1.8：噪音帧（M14 打断 / below_min_len；M7 成员收缩）
    "stitched",        # v1.9：被并入线索的 episode 壳（M16；终态，两个通道都不进）
]


@dataclass(frozen=True)
class RecordRef:
    """记录的来源坐标：输入文件位置 + 生成谱系（spec §4.1）。"""

    source_file: str                       # 相对 run.input 的路径（生成记录为 ""）
    line_no: int | None                    # 文本模态：从 1 开始的行号
    pair_index: int | None                 # UI 模态：文件对序号
    generated_from: tuple[str, ...]        # process 模式生成样本：种子记录 id；
                                           # 其余一切（含 generate_only 样本）：()
                                           # ——合成与否看 `generator`，不看本字段（v1.4）
    generator: Mapping | None = None       # 生成记录：{"llm": <profile>, "style": <name>|None}
                                           # 非生成记录：None


@dataclass(frozen=True)
class ImageRef:
    """截图文件的惰性引用——字节永不常驻内存（spec §2.6）。"""

    path: Path                             # 截图文件绝对/相对路径，按需从磁盘读取
    format: Literal["png", "jpeg"]         # ".jpg"/".jpeg" 都归一为 "jpeg"
    size_bytes: int                        # 磁盘文件字节数（M2 用于 max_image_mb 判定）

    def load_base64(self, max_px: int) -> tuple[str, str]:
        """调用时才从磁盘装载；长边超过 max_px 则按比例缩放（Pillow）后再编码。

        @param max_px 长边像素上限
        @return (media_type, b64)，media_type 为 "image/png" | "image/jpeg"
        """
        from PIL import Image  # 局部导入：保持模块导入轻量；Pillow 是硬依赖

        media_type = "image/png" if self.format == "png" else "image/jpeg"
        with Image.open(self.path) as im:
            width, height = im.size
            long_edge = max(width, height)
            if long_edge > max_px:
                scale = max_px / long_edge
                new_size = (max(1, round(width * scale)), max(1, round(height * scale)))
                resized = im.resize(new_size, Image.Resampling.LANCZOS)
                if self.format == "jpeg" and resized.mode not in ("RGB", "L"):
                    resized = resized.convert("RGB")
                buf = io.BytesIO()
                resized.save(buf, format="PNG" if self.format == "png" else "JPEG")
                data = buf.getvalue()
            else:
                data = self.path.read_bytes()
        return media_type, base64.b64encode(data).decode("ascii")


@dataclass(frozen=True)
class UINode:
    """UI 控件树的一个节点（M2 从原始 dump 归一而来）。"""

    node_id: str                           # 单帧内的节点标识；**不是**跨帧身份
    parent_id: str | None                  # 父节点 node_id；根节点为 None
    depth: int                             # 树深度，根为 0；序列化时每层缩进两空格
    role: str                              # 从 class/type 归一出的控件角色
    text: str                              # 控件可见文本
    content_desc: str                      # 无障碍描述（content-desc）
    bounds: tuple[int, int, int, int]      # (l, t, r, b) 像素
    visible: bool                          # 不可见节点被序列化与摘要一律跳过
    extra: Mapping[str, str]               # 非白名单来源字段，值一律字符串化


@dataclass(frozen=True)
class UITree:
    """一帧的 UI 控件树；`serialize` 是它面向 dedup 与 prompt 的唯一线性化面。"""

    nodes: tuple[UINode, ...]              # 深度优先顺序

    def serialize(self, max_chars: int | None = None, quantize_px: int = 0) -> str:
        """规范线性化（spec §4.3），由 M3 dedup（quantize_px =
        dedup.bounds_quantize_px）与 M5 prompt（quantize_px = 0，max_chars =
        input.ui_tree_max_chars）共用。

        规则（精确）：
        - 按 `nodes` 存储顺序（深度优先）遍历；跳过 visible == False 的节点。
        - 每节点一行，以 "\\n" 连接，无末尾换行：
            line = ("  " * depth) + role
                   + (f' "{text}"' if text else "")
                   + (f' desc="{content_desc}"' if content_desc else "")
                   + f" [{l},{t},{r},{b}]"
                   + "".join(f" {k}={v}" for k, v in extra.items() if v)
          （extra 取插入序；缩进为每层**两个空格**——与 spec 3.2.7/3.9.4 的样例一致
           [FROZEN HERE, see §12]。）
        - quantize_px > 0 时每个坐标先做地板除：c = c // quantize_px。
        - max_chars 非 None 且全量输出超限时：保留最长的**整行**前缀，使其连接长度
          （含 "\\n" 分隔符与下述标记行）≤ max_chars，再追加一行
          "…(truncated N nodes)"，N = 被省略的可见节点数。[FROZEN HERE]

        @param max_chars 输出字符上限；None = 不截断
        @param quantize_px 坐标量化粒度像素；0 = 不量化
        @return 线性化后的树文本（无末尾换行）
        """
        lines = [_serialize_node_line(node, quantize_px)
                 for node in self.nodes if node.visible]
        full = "\n".join(lines)
        if max_chars is None or len(full) <= max_chars:
            return full
        return _truncate_serialized(lines, max_chars)


def _serialize_node_line(node: UINode, quantize_px: int) -> str:
    """渲染 `UITree.serialize` 的单节点行（缩进 + 角色 + 文本 + 描述 + 坐标 + extra）。

    @param node 待渲染的可见节点
    @param quantize_px 坐标量化粒度像素；0 = 不量化
    @return 该节点对应的一行文本（不含换行）
    """
    l, t, r, b = node.bounds
    if quantize_px > 0:
        l, t, r, b = (l // quantize_px, t // quantize_px,
                      r // quantize_px, b // quantize_px)
    line = ("  " * node.depth) + node.role
    if node.text:
        line += f' "{node.text}"'
    if node.content_desc:
        line += f' desc="{node.content_desc}"'
    line += f" [{l},{t},{r},{b}]"
    line += "".join(f" {k}={v}" for k, v in node.extra.items() if v)
    return line


def _truncate_serialized(lines: list[str], max_chars: int) -> str:
    """按 `UITree.serialize` 的截断约定裁剪已渲染行表。

    取最长的整行前缀，使「前缀 + 标记行」的连接长度不超过 max_chars；
    连标记行本身都放不下时，退化为覆盖全部可见节点的单行标记。

    @param lines 已渲染的可见节点行（顺序即输出顺序）
    @param max_chars 输出字符上限
    @return 截断后的文本（末行恒为 "…(truncated N nodes)"）
    """
    total = len(lines)
    # prefix_len[k] = len("\n".join(lines[:k]))
    prefix_len = [0] * (total + 1)
    for i, line in enumerate(lines):
        prefix_len[i + 1] = prefix_len[i] + (1 if i else 0) + len(line)
    for keep in range(total - 1, -1, -1):
        marker = f"…(truncated {total - keep} nodes)"
        joined = prefix_len[keep] + (1 if keep else 0) + len(marker)
        if joined <= max_chars:
            return "\n".join(lines[:keep] + [marker])
    # 连标记行自身都超过 max_chars：对全部可见节点只发标记行
    return f"…(truncated {total} nodes)"


@dataclass(frozen=True)
class Record:
    """一条不可变的输入/生成记录；序列记录（v1.8）以 `members` 承载成员帧。"""

    id: str                                # sha256 十六进制前缀 [:16]；规则按模态定（M2/M6）
                                           # 序列（v1.8）：sha256("\n".join(成员 id))[:16]，
                                           # 成形时固定——成员手术永不重算
    modality: Literal["text", "ui"]        # 记录模态；序列记录取成员的模态
    text: str | None                       # 文本模态：抽取出的正文；UI 模态：None
    raw: Mapping | None                    # 文本模态：原始行对象；UI 模态：None
    ui_tree: UITree | None                 # UI 模态：归一后的控件树；文本模态：None
    image: ImageRef | None                 # UI 模态：截图惰性引用；文本模态：None
    ref: RecordRef                         # 来源坐标与生成谱系
    kind: Literal["single", "sequence"] = "single"   # v1.8：带默认值追加（冻结兼容）
    members: tuple["Record", ...] = ()     # v1.8 序列：成员帧按序键升序排列；
                                           # single：()。序列记录字段约定
                                           # （S24）：text/raw/ui_tree/image = None；modality = 成员
                                           # 的模态；ref = RecordRef(source_file=首成员的 source,
                                           # line_no=首成员的 line_no,
                                           # pair_index=首成员的 pair_index,
                                           # generated_from=(), generator=None)——完整成员
                                           # 谱系走 _meta.stream.member_sources


@dataclass(frozen=True)
class Classification:                      # v1.7：M13 classify 判决（spec 3.13、§4.1）
    """闭集分类判决；multi 扇出后每个兄弟信封各持一份（label 不同、labels 相同）。"""

    label: str                             # **本信封**的路由标签
    labels: tuple[str, ...]                # 该记录的完整命中集（声明序；
                                           # single 归属恒为一个元素）
    source: Literal["llm", "fallback", "inherited"]   # 判决来源：LLM / 兜底类 / 继承
    detail: Mapping                        # 理由 / 自洽统计 / 兜底痕迹（kind、message）


@dataclass(frozen=True)
class SequenceValidationFrame:
    """序列级生成钩子看到的单帧只读输入。"""

    position: int                           # 序列内零基位置
    frame_class: str                        # planner 冻结的帧类
    payload: object                         # JSON-compatible 深拷贝载荷


@dataclass(frozen=True)
class SequenceValidationInput:
    """序列级生成钩子的冻结输入契约。"""

    sequence_class: str                     # 生成序列类名
    tier_rank: int | None                   # 生效档位序数；无档位时为 None
    frames: tuple[SequenceValidationFrame, ...]  # 按序列位置排列的成员帧


@dataclass(frozen=True)
class DedupInfo:
    """M3 去重结论：本记录是唯一还是某簇的重复，以及簇头是谁。"""

    # 判重种类：唯一 / 精确重复 / 近似重复（文本、图像、双证据、语义）
    kind: Literal["unique", "exact", "near_text", "near_image", "near_both", "near_semantic"]
    cluster_key: str                       # 簇头的精确去重键（[:16] 十六进制）；
                                           # 唯一记录携带自己的键
    kept_id: str | None                    # 重复项：被保留记录的 id；唯一项：None


@dataclass(frozen=True)
class QualityScore:
    """M4 单个评分维度的结果；键 "__aggregate__" 为加权汇总。"""

    criterion: str                         # rubric 维度键，或 "__aggregate__"
    score: float | None                    # 归一到 [0,1]；None = 未评分（判决全失败）
    mode: Literal["pairwise_bt", "pointwise"]         # 评分模式（成对 BT 拟合 / 逐条打分）
    detail: Mapping                        # pairwise：{comparisons, wins, ties, log_theta}
                                           # pointwise：{raw_score (0-5), reason}
                                           # __aggregate__：{}


@dataclass(frozen=True)
class Usage:
    """一次或多次 LLM 调用的 token 用量；可直接相加与 sum() 聚合。"""

    prompt_tokens: int = 0                 # 输入 token 数（provider usage 回报，缺失记 0）
    completion_tokens: int = 0             # 输出 token 数（同上）

    def __add__(self, other: "Usage") -> "Usage":          # 冻结面 [FROZEN HERE]
        """按字段相加两份用量。

        @param other 另一份用量
        @return 逐字段求和后的新 Usage
        """
        return Usage(self.prompt_tokens + other.prompt_tokens,
                     self.completion_tokens + other.completion_tokens)

    def __radd__(self, other: object) -> "Usage":          # 冻结面 [FROZEN HERE]
        """支持 `sum(usage_list)`：sum 的隐式起始值是 int 0。

        @param other 左操作数；仅接受 int 0
        @return 起始值为 0 时返回自身，否则 NotImplemented
        """
        if other == 0:
            return self
        return NotImplemented


@dataclass(frozen=True)
class Annotation:
    """M5 标注产物：通过用户 Schema（L2）的对象及其调用账目。"""

    output: Mapping                        # 已**通过**用户 Schema（L2）的对象
    model: str                             # 标注 profile 的 provider 模型串
    attempts: int                          # 1 + L3 修复调用次数
                                           # （自洽：n 个样本的 attempts 之和）
    usage: Usage                           # 首调 + 修复调用的 token（自洽时含全部 n 个样本）
    sc: Mapping | None = None              # 仅自洽：{"n": int, "agreement_ratio": float}
                                           # [FROZEN HERE：挂在此处以便 M11 写 _meta]


@dataclass(frozen=True)
class VerificationResult:
    """M7 评审结论：判决、轮次、累计评语，以及流模式的缺陷表。"""

    verdict: Literal["pass", "fail"]       # 终局判决
    rounds: int                            # 已判轮次含首轮（首评即过 = 1）
    critiques: tuple[Mapping, ...]         # 按轮次累积，保持顺序：
                                           # {"aspect": str, "opinion": str[, "judge": str]}
    defects: tuple[Mapping, ...] = ()      # v1.8（仅流模式 verify）：定型缺陷表条目
                                           # {"kind","members","position","detail"[, "suspected"]}
                                           # ——挂在此处以便 M11 写 _meta（Annotation.sc
                                           # 先例）[FROZEN HERE]


@dataclass(frozen=True)
class StageError:
    """一条记录级错误；落在 item.errors 里，随记录进 rejects。"""

    stage: str                             # 产生该错误的阶段名
    kind: str                              # 错误分类码（§7.6 / errors.ErrorKind）
    message: str                           # 英文错误消息（不含数据内容）
    retryable: bool                        # 是否属可重试类错误（取证用，不驱动重试）


# ── v1.8 流模式共享辅助（spec §4 / CONTRACTS §3）────────────────────────────
# 确定性的帧摘要与树差分，由 M14 segment、M15 extract、M13 classify 与 M4 quality
# 的序列分支共用。算子之间永不相互依赖——共享渲染放在这里，紧挨 UITree.serialize
# （M3/M5 先例）。

_DIGEST_APP_KEYS = ("package", "package_name", "pkg")
_DIGEST_ACTIVITY_KEYS = ("activity", "activity_name", "window_title")
_DIGEST_INTERACTIVE_ROLES = ("Button", "EditText", "CheckBox", "Switch", "ImageButton")


def _scan_ui_digest(tree: "UITree") -> tuple[str | None, str | None, str | None, list[str]]:
    """按 DFS 顺序单趟扫描可见节点，取出 `frame_digest` 所需的四件材料。

    @param tree 待扫描的 UI 控件树
    @return (app, activity, title, salient)——前三者取首个非空命中，salient 为
            有序去重后的显著文本列表（交互控件条目带 "*" 前缀）
    """
    app = activity = title = None
    salient: list[str] = []
    seen: set[str] = set()
    for node in tree.nodes:
        if not node.visible:
            continue
        if app is None:
            for key in _DIGEST_APP_KEYS:
                value = node.extra.get(key)
                if value:
                    app = value
                    break
        if activity is None:
            for key in _DIGEST_ACTIVITY_KEYS:
                value = node.extra.get(key)
                if value:
                    activity = value
                    break
        if title is None and node.text:
            title = node.text
        interactive = any(role in node.role for role in _DIGEST_INTERACTIVE_ROLES)
        for piece in (node.text, node.content_desc):
            if piece and piece not in seen:
                seen.add(piece)
                salient.append(f"*{piece}" if interactive else piece)
    return app, activity, title, salient


def frame_digest(record: "Record", max_chars: int) -> str:
    """确定性的尽力而为帧摘要（spec §4 共享辅助，S12）。

    文本模态：record.text 截到 max_chars（纯切片）。
    UI 模态——"[{app} activity={act}] {title}｜{salient}"，缺失部分省略
    （具体有哪些字段取决于采集侧 dump 往 `extra` 里放了什么）：
      app      = 可见节点（DFS 序）中 package/package_name/pkg 的首个非空 `extra` 值；
                 缺失 → 整个 "[{app}] " 头段省略（activity 值锚定其上，一并消失）
      activity = activity/activity_name/window_title 的首个非空值（常常缺失），
                 紧跟 app 渲染为 " activity={v}"
      title    = 首个 text 非空的可见节点的文本（DFS 序）
      salient  = 可见节点 text/content_desc 的有序去重非空集合，以 "、" 连接；
                 role 中含 Button/EditText/CheckBox/Switch/ImageButton 之一的条目
                 加 "*" 前缀
    长于 max_chars 的摘要裁到 max_chars-1 字符 + "…"（总长 == max_chars，
    与 serialize 的截断约定一致）。贫瘠与否由调用侧的 digest_is_poor() 判定
    （digest_poor_frames 计数器）。

    @param record 待摘要的记录（单帧）
    @param max_chars 摘要字符上限
    @return 摘要文本；UI 模态但无控件树时为 ""
    """
    if record.modality == "text":
        return (record.text or "")[:max_chars]
    if record.ui_tree is None:
        return ""
    app, activity, title, salient = _scan_ui_digest(record.ui_tree)
    head = ""
    if app:
        head = f"[{app} activity={activity}] " if activity else f"[{app}] "
    salient_text = "、".join(salient)
    body = f"{title}｜{salient_text}" if title else salient_text
    digest = head + body if body else head.rstrip()
    if len(digest) > max_chars:
        digest = digest[: max_chars - 1] + "…"
    return digest


def digest_is_poor(record: "Record") -> bool:
    """判定摘要是否贫瘠：UI 模态且「可见文本节点数为 0 或摘要长度 < 8」（S12 护栏，
    spec §4 贫瘠判据）。

    即控件树里可见的 text/content_desc 节点一个都没有，或渲染出的摘要短于 8 个字符
    ——这类是空壳 ghost-node / canvas 画布屏，摘要不携带任何信息。调用侧据此累加
    digest_poor_frames 并每次运行 WARN 一次，引导用户给 segment.llm 配一个
    supports_vision=true 的 profile（v1.11 V4 措辞：旧的 use_vision 键已移除，
    vision 随 profile 能力自动派生）。文本模态按本判据永不贫瘠。长度那一支在远高于
    阈值的上限下渲染，故上限取值不可能掩盖一条真正过短的摘要。

    @param record 待判定的记录
    @return True 表示摘要贫瘠
    """
    if record.modality != "ui":
        return False
    if record.ui_tree is None:
        return True
    if not any(node.visible and (node.text or node.content_desc)
               for node in record.ui_tree.nodes):
        return True
    return len(frame_digest(record, 400)) < 8


def _diff_index(tree: "UITree | None", quantize_px: int):
    """把一棵树的可见节点归到结构键上，供 `tree_diff` 做多重集配对。

    结构键 k(node) = (role, quantize_px > 0 时量化后的 bounds, depth)——node_id
    **不是**跨帧身份，绝不能当匹配键。

    @param tree 待索引的控件树；None 视作空树
    @param quantize_px 坐标量化粒度像素；0 = 不量化
    @return (keyed, app, title, count)——keyed 为「结构键 → (text, content_desc)
            计数器」，app/title 与 frame_digest 同款抽取规则，count 为可见节点数
    """
    keyed: dict[tuple, Counter] = {}
    app = title = None
    count = 0
    for node in (tree.nodes if tree is not None else ()):
        if not node.visible:
            continue
        count += 1
        l, t, r, bt = node.bounds
        if quantize_px > 0:
            l, t, r, bt = (l // quantize_px, t // quantize_px,
                           r // quantize_px, bt // quantize_px)
        key = (node.role, (l, t, r, bt), node.depth)
        keyed.setdefault(key, Counter())[(node.text, node.content_desc)] += 1
        if app is None:
            for k in _DIGEST_APP_KEYS:
                value = node.extra.get(k)
                if value:
                    app = value
                    break
        if title is None and node.text:
            title = node.text
    return keyed, app, title, count


def tree_diff(a: "UITree | None", b: "UITree | None", quantize_px: int) -> Mapping:
    """确定性的结构化树差分（spec §4 共享辅助，S13）。

    只看可见节点，以**多重集**（collections.Counter）在结构键
    k(node) = (role, quantize_px > 0 时量化后的 bounds, depth) 上配对——node_id
    不是跨帧身份，不得作为匹配键。返回 {"added": int, "removed": int,
    "text_changed": int, "change_ratio": float, "app_changed": bool,
    "title_changed": bool}：
      added/removed  = 按结构键未配上的节点数（a 或 b 为 None ⇒ 另一侧的全部可见
                       节点都记在这里）
      text_changed   = 内容变化对数的**下界**：在每个键 min(count_a, count_b) 的
                       配对内，(text, content_desc) 多重集不匹配的数量
      change_ratio   = (added + removed + text_changed)
                       / max(1, max(visible_a, visible_b))
      app_changed / title_changed = 用与 frame_digest 相同的抽取规则比较
                       （extra 的 app 键 / DFS 首个可见文本）
    确定性（结果与哈希/迭代顺序无关——纯多重集算术），复杂度 O(n1 + n2)。只给
    量级与类型证据，不做语义归因（那是 M15 的职责）。

    @param a 前一帧的控件树；None 视作空树
    @param b 后一帧的控件树；None 视作空树
    @param quantize_px 坐标量化粒度像素；0 = 不量化
    @return 上述六键的差分证据字典
    """
    keyed_a, app_a, title_a, n_a = _diff_index(a, quantize_px)
    keyed_b, app_b, title_b, n_b = _diff_index(b, quantize_px)
    added = removed = text_changed = 0
    for key in keyed_a.keys() | keyed_b.keys():
        contents_a = keyed_a.get(key) or Counter()
        contents_b = keyed_b.get(key) or Counter()
        count_a = sum(contents_a.values())
        count_b = sum(contents_b.values())
        paired = min(count_a, count_b)
        removed += count_a - paired
        added += count_b - paired
        # 多重集交集 = 配对内内容保持不变的最大匹配；余量即不匹配数的下界
        text_changed += paired - sum((contents_a & contents_b).values())
    return {
        "added": added,
        "removed": removed,
        "text_changed": text_changed,
        "change_ratio": (added + removed + text_changed) / max(1, n_a, n_b),
        "app_changed": app_a != app_b,
        "title_changed": title_a != title_b,
    }


@dataclass(frozen=True)
class Transition:                          # v1.8：一次 M15 extract 对相邻帧对的判决
    """相邻两帧之间的一步结构化动作（含 v1.9 零 LLM 的线索接缝占位）。"""

    index: int                             # 从 0 开始的成员对序号；**恒等于**其在重建
                                           # 元组中的位置（成员手术后重新编号）
    action: Mapping                        # 通过 action_schema 的对象
                                           # {"action_type","target","value","description"}
    model: str                             # 抽取 profile 的 provider 模型串
    attempts: int                          # 1 + L3 修复调用次数
    detail: Mapping                        # 兜底痕迹 {"kind","message"} / {"reseamed": True} /
                                           # v1.9 线索接缝占位 {"kind": "thread_seam",
                                           # "interrupted_by": [...]}（T10，零 LLM）；
                                           # 干净抽取时为 {}


@dataclass
class PipelineItem:                        # **唯一**可变信封；生命周期 = 一个批次
    """流水线信封：包裹一条冻结的 Record，累积各阶段产物与状态。"""

    record: Record                         # 被包裹的记录（单帧或序列）
    status: Status = "active"              # 状态机取值；只有算子按契约推进
    classification: Classification | None = None   # v1.7：M13 classify 写入（或继承）
    dedup: DedupInfo | None = None         # M3 去重结论
    scores: dict[str, QualityScore] = field(default_factory=dict)
                                           # M4 评分，键 = rubric 维度键 / "__aggregate__"
    annotation: Annotation | None = None   # M5 标注产物
    verification: VerificationResult | None = None  # M7 评审结论
    errors: list[StageError] = field(default_factory=list)
                                           # 记录级错误累积（契约④：单条失败落这里）
    session_id: str | None = None          # v1.8：M10 在信封构造时打戳（流模式）；
                                           # M14 据此分组，M7 修复据此查邻居
    thread_id: str | None = None           # v1.9：M16 stitch 打在幸存线索信封上
                                           # （== record.id == episode_id，T22）；鸭子标记
                                           # seam_indexes / seam_interrupted_by / stitch_fragments
                                           # 随行（T20，由 classify._fan_out 复制）
    transitions: tuple[Transition, ...] | None = None   # v1.8：M15 extract 写入
    member_classifications: dict[str, Classification] | None = None
                                           # v1.12：M13 帧级批量判决写入（首标签序列信封）；
                                           # 键 = 成员 record.id；扇出克隆按引用共享同一 dict
                                           # （record/dedup 同族，classify._fan_out 显式复制）
    member_annotations: dict[str, Annotation] | None = None
                                           # v1.12：M5 帧级逐帧标注写入（同一执行门）；
                                           # 键 = 成员 record.id；值 None = 该成员标注不可修复
                                           # （failed 占键为 None，skipped 不占键——单一真相 =
                                           # dict 形态本身）；克隆按引用共享（同上）
