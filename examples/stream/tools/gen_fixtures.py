#!/usr/bin/env python3
"""examples/stream 一次性 fixture 生成脚本（确定性——无随机、无时间戳）。

生成 14 对 uitree_N.jsonl / image_N.png（index 1..14 连续、平铺）到
examples/stream/data/：

  任务 A「点外卖」帧 1–8   package=com.example.food（帧 5 除外）
    1 首页 → 2 搜索页 → 3 餐厅列表 → 4 菜品详情 → [5 无关屏
    com.example.social，预期 dropped_noise] → 6 购物车 → 7 结算页 → 8 订单完成
    实体延续：餐厅名「川味麻辣烫」跨 3/4/6，金额 ¥32 跨 4/6/7，订单号仅帧 8。
  任务 B「打车」帧 9–13    package=com.example.taxi
    9 首页 → 10 目的地输入 → 11 选车型 → 12 确认叫车 → 13 行程开始
    实体延续：虹桥机场跨 10/11/12，¥58 跨 11/12。
  帧 14 桌面屏             package=com.example.launcher（会话尾部自然帧）

树是唯一语义源；节点行形态照抄 examples/ui/data/uitree_*.jsonl：
  {id, parent, class, text, bounds, visible, package[, content_desc]}
截图 = PIL 纯色底（每 App 一底色）+ 顶部标题大字 + 按树 bounds 画控件矩形
与文本，尽力而为——中文经系统字体（PingFang/STHeiti）渲染，字体加载失败
回退画 ASCII 替代文本；图片语义以树为准。PNG 为 8-bit RGB、pnginfo 置空，
重跑产物字节稳定。

用法：cd examples/stream && uv run python tools/gen_fixtures.py
"""

from __future__ import annotations

import json
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont
from PIL.PngImagePlugin import PngInfo

W, H = 400, 800
DATA_DIR = Path(__file__).resolve().parent.parent / "data"

_FONT_CANDIDATES = (
    "/System/Library/Fonts/PingFang.ttc",
    "/System/Library/Fonts/STHeiti Light.ttc",
    "/System/Library/Fonts/STHeiti Medium.ttc",
)

# 每 App 一底色：food 橙 / social 灰 / taxi 蓝 / launcher 白。
APPS = {
    "food": {"package": "com.example.food", "bg": (255, 138, 61),
             "title": (255, 255, 255), "on_bg": (255, 255, 255),
             "fill": (255, 244, 235), "outline": (191, 87, 17),
             "widget_text": (74, 40, 12)},
    "social": {"package": "com.example.social", "bg": (158, 158, 158),
               "title": (255, 255, 255), "on_bg": (250, 250, 250),
               "fill": (240, 240, 240), "outline": (105, 105, 105),
               "widget_text": (55, 55, 55)},
    "taxi": {"package": "com.example.taxi", "bg": (46, 124, 246),
             "title": (255, 255, 255), "on_bg": (255, 255, 255),
             "fill": (238, 245, 255), "outline": (18, 70, 160),
             "widget_text": (16, 42, 92)},
    "launcher": {"package": "com.example.launcher", "bg": (255, 255, 255),
                 "title": (40, 40, 40), "on_bg": (40, 40, 40),
                 "fill": (238, 238, 242), "outline": (198, 198, 205),
                 "widget_text": (40, 40, 40)},
}

# 帧定义：(app, [(class, text, bounds[, content_desc]), ...])。
# 每帧 6–10 节点：FrameLayout 根 + TextView 标题（首个非空 text，即
# frame_digest 的 title）+ 若干 Button/EditText/TextView，全 visible，
# bounds 平铺于 400×800 内。
FRAMES: list[tuple[str, list[tuple]]] = [
    # ── 任务 A「点外卖」 ────────────────────────────────────────────────
    # 1 首页：搜索框 + 推荐列表
    ("food", [
        ("FrameLayout", "", (0, 0, 400, 800)),
        ("TextView", "美食外卖", (24, 20, 200, 56)),
        ("EditText", "", (24, 80, 296, 128), "搜索美食"),
        ("Button", "搜索", (308, 80, 376, 128)),
        ("TextView", "推荐餐厅", (24, 160, 160, 188)),
        ("TextView", "老王烧烤 4.8 分", (24, 200, 376, 240)),
        ("TextView", "巷口火锅 4.6 分", (24, 248, 376, 288)),
        ("TextView", "翠华茶餐厅 4.5 分", (24, 296, 376, 336)),
    ]),
    # 2 搜索页：EditText 含 text「麻辣烫」
    ("food", [
        ("FrameLayout", "", (0, 0, 400, 800)),
        ("TextView", "搜索", (24, 20, 120, 56)),
        ("EditText", "麻辣烫", (24, 80, 296, 128)),
        ("Button", "搜索", (308, 80, 376, 128)),
        ("TextView", "热门搜索", (24, 160, 160, 188)),
        ("Button", "火锅", (24, 200, 100, 240)),
        ("Button", "烧烤", (112, 200, 188, 240)),
        ("Button", "奶茶", (200, 200, 276, 240)),
    ]),
    # 3 餐厅列表：三家餐厅 Button（餐厅名实体自此开始）
    ("food", [
        ("FrameLayout", "", (0, 0, 400, 800)),
        ("TextView", "麻辣烫 搜索结果", (24, 20, 280, 56)),
        ("Button", "川味麻辣烫", (24, 80, 376, 150)),
        ("Button", "张记麻辣烫", (24, 166, 376, 236)),
        ("Button", "蜀香麻辣烫", (24, 252, 376, 322)),
        ("TextView", "共 3 家餐厅", (24, 340, 200, 368)),
    ]),
    # 4 菜品详情：招牌麻辣烫 ¥32 + 加入购物车
    ("food", [
        ("FrameLayout", "", (0, 0, 400, 800)),
        ("TextView", "川味麻辣烫", (24, 20, 220, 56)),
        ("Button", "收藏", (300, 20, 376, 56)),
        ("TextView", "招牌麻辣烫 ¥32", (24, 90, 300, 126)),
        ("TextView", "微辣 · 大份 · 含粉丝青菜", (24, 134, 340, 162)),
        ("TextView", "月售 500+ 好评率 98%", (24, 170, 300, 198)),
        ("Button", "加入购物车", (24, 700, 376, 760)),
    ]),
    # 5 无关屏（package 异域）：通知面板两条文本——预期 dropped_noise
    ("social", [
        ("FrameLayout", "", (0, 0, 400, 800)),
        ("TextView", "新消息", (24, 20, 160, 56)),
        ("TextView", "小李：周末一起打球吗？", (24, 90, 376, 130)),
        ("TextView", "妈妈：记得周末回家吃饭", (24, 140, 376, 180)),
        ("Button", "全部已读", (24, 700, 180, 748)),
        ("Button", "设置", (220, 700, 376, 748)),
    ]),
    # 6 购物车：招牌麻辣烫 ×1 ¥32 + 去结算
    ("food", [
        ("FrameLayout", "", (0, 0, 400, 800)),
        ("TextView", "购物车", (24, 20, 150, 56)),
        ("TextView", "川味麻辣烫", (24, 90, 220, 120)),
        ("TextView", "招牌麻辣烫 ×1 ¥32", (24, 130, 320, 166)),
        ("TextView", "合计 ¥32", (24, 180, 180, 210)),
        ("Button", "继续点餐", (24, 620, 180, 668)),
        ("Button", "去结算", (24, 700, 376, 760)),
    ]),
    # 7 结算页：地址 + 提交订单 ¥32
    ("food", [
        ("FrameLayout", "", (0, 0, 400, 800)),
        ("TextView", "确认订单", (24, 20, 180, 56)),
        ("TextView", "收货地址：上海市浦东新区世纪大道 100 号", (24, 90, 376, 150)),
        ("TextView", "招牌麻辣烫 ×1", (24, 160, 250, 196)),
        ("TextView", "配送费 ¥0", (24, 204, 180, 232)),
        ("TextView", "预计送达 13:30", (24, 240, 250, 268)),
        ("Button", "提交订单 ¥32", (24, 700, 376, 760)),
    ]),
    # 8 订单完成：下单成功 + 订单号（订单号实体仅本帧）
    ("food", [
        ("FrameLayout", "", (0, 0, 400, 800)),
        ("TextView", "下单成功", (24, 20, 180, 56)),
        ("TextView", "订单号：FD20260713001", (24, 100, 340, 136)),
        ("TextView", "预计 30 分钟内送达", (24, 144, 280, 172)),
        ("Button", "查看订单", (24, 700, 190, 760)),
        ("Button", "返回首页", (210, 700, 376, 760)),
    ]),
    # ── 任务 B「打车」 ──────────────────────────────────────────────────
    # 9 打车首页：输入目的地 EditText
    ("taxi", [
        ("FrameLayout", "", (0, 0, 400, 800)),
        ("TextView", "快捷出行", (24, 20, 200, 56)),
        ("EditText", "输入目的地", (24, 90, 376, 140)),
        ("TextView", "当前位置：人民广场", (24, 150, 280, 180)),
        ("Button", "预约用车", (24, 620, 180, 668)),
        ("Button", "代人叫车", (220, 620, 376, 668)),
    ]),
    # 10 目的地输入：text「虹桥机场」（实体自此开始）
    ("taxi", [
        ("FrameLayout", "", (0, 0, 400, 800)),
        ("TextView", "选择目的地", (24, 20, 220, 56)),
        ("EditText", "虹桥机场", (24, 90, 376, 140)),
        ("TextView", "虹桥机场 T1 航站楼", (24, 160, 376, 200)),
        ("TextView", "虹桥机场 T2 航站楼", (24, 208, 376, 248)),
        ("Button", "确认", (24, 700, 376, 760)),
    ]),
    # 11 选车型：经济型 ¥58 / 舒适型 ¥78 两 Button
    ("taxi", [
        ("FrameLayout", "", (0, 0, 400, 800)),
        ("TextView", "选择车型", (24, 20, 180, 56)),
        ("TextView", "目的地：虹桥机场", (24, 80, 300, 110)),
        ("Button", "经济型 ¥58", (24, 140, 376, 210)),
        ("Button", "舒适型 ¥78", (24, 226, 376, 296)),
        ("TextView", "预计 35 分钟到达", (24, 320, 260, 350)),
    ]),
    # 12 确认叫车：呼叫经济型 ¥58
    ("taxi", [
        ("FrameLayout", "", (0, 0, 400, 800)),
        ("TextView", "确认叫车", (24, 20, 180, 56)),
        ("TextView", "目的地：虹桥机场", (24, 90, 300, 120)),
        ("TextView", "车型：经济型", (24, 130, 220, 160)),
        ("Button", "更换车型", (24, 620, 180, 668)),
        ("Button", "呼叫经济型 ¥58", (24, 700, 376, 760)),
    ]),
    # 13 行程开始：司机已接单 沪A·12345
    ("taxi", [
        ("FrameLayout", "", (0, 0, 400, 800)),
        ("TextView", "司机已接单", (24, 20, 220, 56)),
        ("TextView", "车牌 沪A·12345", (24, 90, 260, 126)),
        ("TextView", "白色 大众朗逸", (24, 134, 240, 162)),
        ("TextView", "司机 王师傅 · 5.0 分", (24, 170, 280, 198)),
        ("Button", "联系司机", (24, 700, 190, 760)),
        ("Button", "取消行程", (210, 700, 376, 760)),
    ]),
    # ── 帧 14 桌面屏（会话尾部自然帧，不设强断言） ───────────────────────
    ("launcher", [
        ("FrameLayout", "", (0, 0, 400, 800)),
        ("TextView", "主屏幕", (24, 20, 150, 56)),
        ("TextView", "美食外卖", (32, 120, 128, 200)),
        ("TextView", "快捷出行", (152, 120, 248, 200)),
        ("TextView", "消息", (272, 120, 368, 200)),
        ("TextView", "相机", (32, 240, 128, 320)),
        ("TextView", "设置", (152, 240, 248, 320)),
        ("TextView", "相册", (272, 240, 368, 320)),
    ]),
]

TITLE_SIZE = 30
BODY_SIZE = 18
PLACEHOLDER_COLOR = (150, 150, 150)


def _load_fonts() -> tuple[dict[int, ImageFont.FreeTypeFont] | None, bool]:
    """Try system CJK fonts; on total failure fall back to the PIL bitmap
    font (ASCII-substitute text is drawn instead — tree stays authoritative)."""
    for path in _FONT_CANDIDATES:
        try:
            fonts = {size: ImageFont.truetype(path, size=size)
                     for size in (TITLE_SIZE, 18, 16, 14, 12)}
            return fonts, True
        except OSError:
            continue
    return None, False


_FONTS, CJK_OK = _load_fonts()
_BITMAP_FONT = None if CJK_OK else ImageFont.load_default()


def _ascii_fallback(text: str, role: str) -> str:
    kept = text.encode("ascii", "ignore").decode().strip()
    return kept or role


def _font_for(size: int):
    return _FONTS[size] if CJK_OK else _BITMAP_FONT


def _fit_size(draw: ImageDraw.ImageDraw, text: str, box_w: int, start: int) -> int:
    """Largest candidate size whose rendered width fits box_w (best effort —
    the smallest size is returned even if it still overflows)."""
    candidates = [s for s in (start, 18, 16, 14, 12) if s <= start]
    for size in candidates:
        bbox = draw.textbbox((0, 0), text, font=_font_for(size))
        if bbox[2] - bbox[0] <= box_w:
            return size
    return candidates[-1]


def _draw_text(draw: ImageDraw.ImageDraw, text: str,
               bounds: tuple[int, int, int, int], size: int, color,
               center: bool) -> None:
    if not text:
        return
    l, t, r, b = bounds
    pad = 8
    size = _fit_size(draw, text, r - l - 2 * pad, size)
    font = _font_for(size)
    bbox = draw.textbbox((0, 0), text, font=font)
    tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
    if center:
        xy = (l + max((r - l - tw) // 2, pad), t + max((b - t - th) // 2, 2))
    else:
        xy = (l + pad, t + max((b - t - th) // 2, 2))
    draw.text(xy, text, font=font, fill=color)


def render_image(app: str, nodes: list[tuple], path: Path) -> None:
    style = APPS[app]
    img = Image.new("RGB", (W, H), style["bg"])
    draw = ImageDraw.Draw(img)
    title_seen = False
    for i, spec in enumerate(nodes):
        if i == 0:                                    # root = 纯色底
            continue
        role, text, bounds = spec[0], spec[1], spec[2]
        content_desc = spec[3] if len(spec) > 3 else ""
        shown = text if CJK_OK else _ascii_fallback(text, role)
        if not title_seen and text:                   # 首个非空 text = 标题大字
            title_seen = True
            _draw_text(draw, shown, bounds, TITLE_SIZE, style["title"],
                       center=False)
            continue
        if role == "Button":
            draw.rounded_rectangle(list(bounds), radius=10,
                                   fill=style["fill"],
                                   outline=style["outline"], width=2)
            _draw_text(draw, shown, bounds, BODY_SIZE,
                       style["widget_text"], center=True)
        elif role == "EditText":
            draw.rounded_rectangle(list(bounds), radius=6,
                                   fill=(255, 255, 255),
                                   outline=style["outline"], width=2)
            if text:
                _draw_text(draw, shown, bounds, BODY_SIZE,
                           style["widget_text"], center=False)
            elif content_desc:                        # 空输入框画灰色占位文本
                placeholder = (content_desc if CJK_OK
                               else _ascii_fallback(content_desc, role))
                _draw_text(draw, placeholder, bounds, BODY_SIZE,
                           PLACEHOLDER_COLOR, center=False)
        else:                                         # TextView 等：纯文本
            _draw_text(draw, shown, bounds, BODY_SIZE, style["on_bg"],
                       center=False)
    # pnginfo 置空：不写任何辅助块，保证重跑字节稳定。
    img.save(path, format="PNG", pnginfo=PngInfo())


def build_tree(app: str, nodes: list[tuple]) -> str:
    pkg = APPS[app]["package"]
    lines = []
    for i, spec in enumerate(nodes):
        role, text, bounds = spec[0], spec[1], spec[2]
        obj: dict = {
            "id": str(i),
            "parent": None if i == 0 else "0",
            "class": role,
            "text": text,
            "bounds": list(bounds),
            "visible": True,
            "package": pkg,
        }
        if len(spec) > 3:
            obj["content_desc"] = spec[3]
        lines.append(json.dumps(obj, ensure_ascii=False))
    return "\n".join(lines) + "\n"


def main() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if not CJK_OK:
        print("warning: 未找到系统中文字体，截图文字回退为 ASCII 替代"
              "（树语义不受影响）")
    for index, (app, nodes) in enumerate(FRAMES, start=1):
        tree_path = DATA_DIR / f"uitree_{index}.jsonl"
        image_path = DATA_DIR / f"image_{index}.png"
        tree_path.write_text(build_tree(app, nodes), encoding="utf-8")
        render_image(app, nodes, image_path)
        print(f"wrote uitree_{index}.jsonl + image_{index}.png "
              f"({APPS[app]['package']})")
    print(f"done: {len(FRAMES)} pairs in {DATA_DIR}")


if __name__ == "__main__":
    main()
