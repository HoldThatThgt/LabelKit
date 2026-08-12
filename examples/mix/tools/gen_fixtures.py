#!/usr/bin/env python3
"""examples/mix 一次性 fixture 生成脚本（确定性——无随机、无时间戳）。

生成三个会话子目录共 17 对 uitree_N.jsonl / image_N.png 到
examples/mix/data/（`[stream] key = ["source_dir"]` 按子目录分会话；
index 命名空间全树唯一、各会话错开编号）：

  s1-food-order/ 1–6        外卖下单流程（com.example.food）：浏览列表 →
    商品详情 → 规格表单（帧类 form_screen，吃
    [frame.class.form_screen.annotate] 覆盖指令抽表单字段与取值）→
    订单确认 → 支付处理过渡屏（帧类 transition，
    [frame.class.transition.annotate].enabled=false 跳过标注示范位；
    同 App 任务内过渡屏，segment.context 约定视为当前段延续）→
    支付成功。实体延续：「金牌黄焖鸡」跨 2/3/4、「黄焖鸡米饭」跨
    2/3/4/6、¥38 跨 2/3/4/6，订单号仅帧 6。
  s2-hotel-booking/ 101–105 订酒店流程（com.example.hotel）：搜索表单 →
    酒店列表 → 系统通知插入屏（com.example.sysnotify，与前后操作无关，
    segment 噪声候选，预期 dropped_noise）→ 房型详情 → 订单确认。
    实体延续：「平江府观景酒店」跨 102/104/105、¥429 跨 102/104/105。
  s3-food-order-replay/ 201–206 = s1 的逐字节复刻（同内容不同文件名——
    episode 判重配方与 s1 逐字一致，预期 dropped_dup·exact，episode 级
    判重演示；帧粒度两 pass 在重复信封上永不运行——重复不付费）。

树是唯一语义源；节点行形态照抄 examples/ui/data/uitree_*.jsonl：
  {id, parent, class, text, bounds, visible, package[, content_desc]}
截图 = PIL 纯色底（每 App 一底色）+ 顶部标题大字 + 按树 bounds 画控件矩形
与文本，尽力而为——中文经系统字体（PingFang/STHeiti）渲染，字体加载失败
回退画 ASCII 替代文本；图片语义以树为准。PNG 为 8-bit RGB、pnginfo 置空，
重跑产物字节稳定。

用法：cd examples/mix && uv run python tools/gen_fixtures.py
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

# 每 App 一底色（外卖橙 / 酒店青 / 系统通知灰蓝）。
APPS = {
    "food": {"package": "com.example.food", "bg": (255, 138, 61),
             "title": (255, 255, 255), "on_bg": (255, 255, 255),
             "fill": (255, 244, 235), "outline": (191, 87, 17),
             "widget_text": (74, 40, 12)},
    "hotel": {"package": "com.example.hotel", "bg": (0, 137, 123),
              "title": (255, 255, 255), "on_bg": (255, 255, 255),
              "fill": (232, 247, 245), "outline": (0, 77, 64),
              "widget_text": (0, 51, 43)},
    "sysnotify": {"package": "com.example.sysnotify", "bg": (84, 110, 122),
                  "title": (255, 255, 255), "on_bg": (245, 247, 248),
                  "fill": (236, 239, 241), "outline": (44, 62, 70),
                  "widget_text": (33, 46, 52)},
}

# 帧定义：(app, [(class, text, bounds[, content_desc]), ...])。
# 每帧 4–8 节点：FrameLayout 根 + TextView 标题（首个非空 text，即
# frame_digest 的 title）+ 若干 Button/EditText/TextView，全 visible，
# bounds 平铺于 400×800 内。两个任务域之间刻意避免任何完全相同的文本串；
# 同一任务的帧之间刻意保留逐字延续实体。

# ── s1-food-order：外卖下单 6 帧（帧 5 为支付处理过渡屏） ────────────────────
S1_FRAMES: list[tuple[str, list[tuple]]] = [
    # 1 浏览列表：推荐餐厅列表（list_screen）
    ("food", [
        ("FrameLayout", "", (0, 0, 400, 800)),
        ("TextView", "美食外卖", (24, 20, 200, 56)),
        ("EditText", "", (24, 80, 296, 128), "搜索美食"),
        ("Button", "搜索", (308, 80, 376, 128)),
        ("TextView", "推荐餐厅", (24, 160, 160, 188)),
        ("Button", "金牌黄焖鸡 4.9 分", (24, 200, 376, 260)),
        ("Button", "老面坊牛肉面 4.7 分", (24, 276, 376, 336)),
        ("Button", "青禾轻食沙拉 4.5 分", (24, 352, 376, 412)),
    ]),
    # 2 商品详情：黄焖鸡米饭 ¥38（detail_screen；实体自此开始）
    ("food", [
        ("FrameLayout", "", (0, 0, 400, 800)),
        ("TextView", "金牌黄焖鸡", (24, 20, 220, 56)),
        ("TextView", "黄焖鸡米饭 ¥38", (24, 90, 300, 126)),
        ("TextView", "月售 1200+ 好评率 99%", (24, 134, 300, 162)),
        ("TextView", "招牌黄焖鸡块 配米饭一份", (24, 170, 340, 198)),
        ("Button", "选规格", (24, 700, 376, 760)),
    ]),
    # 3 规格表单：份量/辣度/米饭 + 备注（form_screen 覆盖指令示范位）
    ("food", [
        ("FrameLayout", "", (0, 0, 400, 800)),
        ("TextView", "选择规格", (24, 20, 180, 56)),
        ("TextView", "黄焖鸡米饭 ¥38", (24, 80, 300, 112)),
        ("Button", "份量：大份", (24, 140, 376, 196)),
        ("Button", "辣度：微辣", (24, 212, 376, 268)),
        ("TextView", "米饭 ×1", (24, 284, 180, 316)),
        ("EditText", "", (24, 332, 376, 388), "口味备注（选填）"),
        ("Button", "加入购物车", (24, 700, 376, 760)),
    ]),
    # 4 订单确认：地址 + 提交订单 ¥38（confirm_screen）
    ("food", [
        ("FrameLayout", "", (0, 0, 400, 800)),
        ("TextView", "确认订单", (24, 20, 180, 56)),
        ("TextView", "金牌黄焖鸡", (24, 80, 220, 112)),
        ("TextView", "黄焖鸡米饭 大份 ×1", (24, 120, 300, 152)),
        ("TextView", "收货地址：南京市玄武区中山路 18 号", (24, 166, 376, 218)),
        ("TextView", "预计送达 12:40", (24, 226, 250, 254)),
        ("Button", "提交订单 ¥38", (24, 700, 376, 760)),
    ]),
    # 5 支付处理过渡屏（同 App 任务内过渡：transition，跳过标注示范位）
    ("food", [
        ("FrameLayout", "", (0, 0, 400, 800)),
        ("TextView", "正在处理", (24, 20, 180, 56)),
        ("TextView", "支付处理中，请稍候…", (24, 320, 340, 356)),
        ("TextView", "请勿关闭页面", (24, 366, 240, 394)),
    ]),
    # 6 支付成功：订单号（confirm_screen；订单号实体仅本帧）
    ("food", [
        ("FrameLayout", "", (0, 0, 400, 800)),
        ("TextView", "支付成功", (24, 20, 180, 56)),
        ("TextView", "订单号 FD20260812001", (24, 100, 340, 136)),
        ("TextView", "黄焖鸡米饭 大份 ×1 实付 ¥38", (24, 144, 360, 176)),
        ("TextView", "预计 40 分钟内送达", (24, 184, 280, 212)),
        ("Button", "查看订单", (24, 700, 190, 760)),
        ("Button", "返回首页", (210, 700, 376, 760)),
    ]),
]

# ── s2-hotel-booking：订酒店 5 帧（帧 103 为系统通知插入屏——噪声候选） ──────
S2_FRAMES: list[tuple[str, list[tuple]]] = [
    # 101 搜索表单：目的地 + 日期（form_screen）
    ("hotel", [
        ("FrameLayout", "", (0, 0, 400, 800)),
        ("TextView", "酒店预订", (24, 20, 180, 56)),
        ("EditText", "苏州", (24, 80, 376, 128)),
        ("TextView", "9月1日 入住 · 9月2日 离店", (24, 140, 340, 172)),
        ("TextView", "1 间 · 1 位住客", (24, 180, 220, 208)),
        ("Button", "搜索酒店", (24, 700, 376, 760)),
    ]),
    # 102 酒店列表（list_screen；实体「平江府观景酒店」自此开始）
    ("hotel", [
        ("FrameLayout", "", (0, 0, 400, 800)),
        ("TextView", "苏州酒店", (24, 20, 180, 56)),
        ("Button", "平江府观景酒店 ¥429 起 4.8 分", (24, 80, 376, 150)),
        ("Button", "城南商旅酒店 ¥219 起 4.3 分", (24, 166, 376, 236)),
        ("Button", "金鸡湖畔民宿 ¥560 起 4.9 分", (24, 252, 376, 322)),
        ("TextView", "共 3 家酒店", (24, 340, 200, 368)),
    ]),
    # 103 系统通知插入屏（package 异域，与前后操作无关）——预期 dropped_noise
    ("sysnotify", [
        ("FrameLayout", "", (0, 0, 400, 800)),
        ("TextView", "系统通知", (24, 20, 200, 56)),
        ("TextView", "应用商店：3 款应用已在后台完成更新", (24, 90, 376, 142)),
        ("TextView", "存储空间：本周已清理 1.2GB 缓存", (24, 152, 376, 204)),
        ("Button", "查看详情", (24, 700, 190, 760)),
        ("Button", "全部忽略", (210, 700, 376, 760)),
    ]),
    # 104 房型详情：高级大床房 ¥429（detail_screen）
    ("hotel", [
        ("FrameLayout", "", (0, 0, 400, 800)),
        ("TextView", "平江府观景酒店", (24, 20, 260, 56)),
        ("TextView", "高级大床房 ¥429/晚 含双早", (24, 90, 350, 126)),
        ("TextView", "免费取消 · 近平江路历史街区", (24, 134, 340, 166)),
        ("TextView", "观景飘窗 · 45㎡", (24, 174, 240, 202)),
        ("Button", "预订高级大床房", (24, 700, 376, 760)),
    ]),
    # 105 订单确认（confirm_screen；会话在支付前自然截止）
    ("hotel", [
        ("FrameLayout", "", (0, 0, 400, 800)),
        ("TextView", "确认订单", (24, 20, 180, 56)),
        ("TextView", "平江府观景酒店", (24, 80, 260, 112)),
        ("TextView", "高级大床房 ¥429 共 1 晚", (24, 120, 330, 152)),
        ("TextView", "入住人 沈悦 139****2210", (24, 160, 320, 192)),
        ("Button", "去支付", (24, 700, 376, 760)),
    ]),
]

# 会话表：(子目录, 起始 index, 帧表)。index 全树唯一、各会话错开编号；
# s3 复用 S1_FRAMES 帧表——同节点同渲染，产物与 s1 逐字节一致（仅文件名不同）。
SCENARIOS: list[tuple[str, int, list[tuple[str, list[tuple]]]]] = [
    ("s1-food-order", 1, S1_FRAMES),
    ("s2-hotel-booking", 101, S2_FRAMES),
    ("s3-food-order-replay", 201, S1_FRAMES),
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
    if not CJK_OK:
        print("warning: 未找到系统中文字体，截图文字回退为 ASCII 替代"
              "（树语义不受影响）")
    total = 0
    for dirname, start, frames in SCENARIOS:
        scenario_dir = DATA_DIR / dirname
        scenario_dir.mkdir(parents=True, exist_ok=True)
        for offset, (app, nodes) in enumerate(frames):
            index = start + offset
            tree_path = scenario_dir / f"uitree_{index}.jsonl"
            image_path = scenario_dir / f"image_{index}.png"
            tree_path.write_text(build_tree(app, nodes), encoding="utf-8")
            render_image(app, nodes, image_path)
            print(f"wrote {dirname}/uitree_{index}.jsonl + image_{index}.png "
                  f"({APPS[app]['package']})")
            total += 1
    print(f"done: {total} pairs in {DATA_DIR}")


if __name__ == "__main__":
    main()
