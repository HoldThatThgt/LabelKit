"""生成图片候选区域示例工程的 data/ 与 fixtures/：图片、伴随文件和固定候选夹具。

所有文件都由下面的表格生成，所以图片上的内容、伴随文件里的矩形与固定候选的期望几何彼此对齐。
期望几何用精确有理数计算，不经过 LabelKit，因此可以当独立 oracle。图片字节依赖 Pillow 与 FreeType 构建，
重新生成后可能不同；伴随文件与夹具的文本字节固定。

运行方式：在本目录执行 ``python prepare.py``。
"""
from __future__ import annotations

import json
from fractions import Fraction
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
BACKGROUND = (246, 246, 242)
INK = (32, 36, 44)
SCREEN = (540, 960)
BUTTONS = (
    ("Sign in", (60, 300, 480, 380), (47, 111, 196)),
    ("Register", (60, 420, 480, 500), (78, 150, 104)),
    ("Forgot password", (60, 540, 480, 600), (190, 118, 60)),
)
PAGE = (800, 1000)
OCR_LINES = (
    ("Invoice 2041", [60.5, 80.25, 420.75, 118.5], 0.98),
    ("Total due 318.40", [60.5, 180.0, 512.25, 214.75], 0.93),
    ("smudged footer", [60.5, 900.0, 300.0, 930.0], 0.31),
)
FIXED_SIZE = (2601, 1873)      # 长边超过默认 max_image_px，请求必然缩放；两轴的缩放比例不相等
FIXED_COLORS = ((196, 72, 64), (60, 124, 190), (84, 160, 96))
FIXED_REGIONS = (
    (((100, 120), (480, 120), (480, 500), (100, 500), (100, 120)),
     ((200, 220), (200, 400), (380, 400), (380, 220), (200, 220))),
    (((560, 100), (760, 100), (760, 300), (560, 300), (560, 100)),),
    (((620, 420), (930, 420), (930, 880), (620, 880), (620, 420)),),
    (((120, 620), (470, 620), (295, 930), (120, 620)),),
)
FIXED_REGION_PARTS = ((FIXED_REGIONS[0],), (FIXED_REGIONS[1], FIXED_REGIONS[2]), (FIXED_REGIONS[3],))


def write_text(path: Path, text: str) -> None:
    """按 UTF-8 与 LF 写文本，换行不随平台变化。

    @param path 目标路径
    @param text 文本内容
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(text.encode("utf-8"))


def write_json(path: Path, value: object) -> None:
    """写一份两格缩进的 JSON。

    @param path 目标路径
    @param value JSON 值
    """
    write_text(path, json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def font(size: int) -> ImageFont.ImageFont:
    """@return 示例图片使用的默认字体。"""
    return ImageFont.load_default(size=size)


def draw_screen(path: Path, title: str, buttons: tuple) -> None:
    """画一张带按钮的竖屏截图。

    @param path 目标 PNG
    @param title 顶部标题
    @param buttons (文字, 矩形, 颜色) 序列
    """
    image = Image.new("RGB", SCREEN, BACKGROUND)
    draw = ImageDraw.Draw(image)
    draw.text((60, 120), title, fill=INK, font=font(48))
    for label, rectangle, color in buttons:
        draw.rectangle(rectangle, fill=color)
        draw.text((rectangle[0] + 24, rectangle[1] + 18), label, fill=(255, 255, 255), font=font(30))
    path.parent.mkdir(parents=True, exist_ok=True)
    image.save(path)


def uiautomator_xml(buttons: tuple) -> str:
    """把按钮表写成 uiautomator dump。

    @param buttons (文字, 矩形, 颜色) 序列
    @return XML 文本，根节点是整屏容器
    """
    lines = ["<?xml version='1.0' encoding='UTF-8' standalone='yes' ?>", '<hierarchy rotation="0">',
             f'  <node class="android.widget.FrameLayout" text="" clickable="false" bounds="[0,0][{SCREEN[0]},{SCREEN[1]}]">']
    for label, (left, top, right, bottom), _color in buttons:
        lines.append(f'    <node class="android.widget.Button" text="{label}" clickable="true" '
                     f'bounds="[{left},{top}][{right},{bottom}]" />')
    lines += ["  </node>", "</hierarchy>", ""]
    return "\n".join(lines)


def accessibility_tree(buttons: tuple) -> dict:
    """把按钮表写成无障碍树。

    @param buttons (文字, 矩形, 颜色) 序列
    @return 根节点是无名窗口的树
    """
    children = [{"role": "button", "name": label,
                 "rect": {"x": left, "y": top, "width": right - left, "height": bottom - top}, "children": []}
                for label, (left, top, right, bottom), _color in buttons]
    return {"root": {"role": "window", "name": "",
                     "rect": {"x": 0, "y": 0, "width": SCREEN[0], "height": SCREEN[1]}, "children": children}}


def draw_photos() -> None:
    """画两张没有任何伴随文件的图片。"""
    scene = Image.new("RGB", (900, 600), (168, 208, 236))
    draw = ImageDraw.Draw(scene)
    draw.rectangle((0, 400, 900, 600), fill=(96, 148, 84))
    draw.ellipse((640, 60, 800, 220), fill=(240, 204, 72))
    draw.rectangle((180, 240, 460, 460), fill=(186, 104, 76))
    draw.polygon(((150, 240), (320, 110), (490, 240)), fill=(112, 60, 52))
    scene.save(_ensure(DATA / "photos" / "image_1.png"))
    table = Image.new("RGB", (900, 600), (92, 70, 58))
    draw = ImageDraw.Draw(table)
    draw.ellipse((120, 140, 400, 420), fill=(238, 238, 232))
    draw.ellipse((190, 210, 330, 350), fill=(204, 92, 70))
    draw.rectangle((520, 120, 640, 480), fill=(218, 200, 150))
    draw.rectangle((700, 180, 780, 420), fill=(70, 120, 150))
    table.save(_ensure(DATA / "photos" / "image_2.png"))


def _ensure(path: Path) -> Path:
    """@return 父目录已存在的同一路径。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def draw_document() -> None:
    """画一张文档图片并写 OCR 结果；最后一行置信度低，示例钩子会丢弃它。"""
    page = Image.new("RGB", PAGE, (252, 252, 248))
    draw = ImageDraw.Draw(page)
    for text, bbox, _confidence in OCR_LINES:
        draw.text((bbox[0], bbox[1]), text, fill=INK, font=font(34))
    page.save(_ensure(DATA / "documents" / "image_1.png"))
    lines = [{"text": text, "bbox": bbox, "confidence": confidence} for text, bbox, confidence in OCR_LINES]
    write_json(DATA / "documents" / "image_1.ocr.json", {"lines": lines})


def _pixel_ring(ring: tuple, size: tuple[int, int]) -> list[list[float]]:
    """用精确有理数把网格环投影成原图像素，不取整。

    @param ring 网格顶点
    @param size 原图宽高
    @return 像素顶点
    """
    return [[float(Fraction(x * size[0], 1000)), float(Fraction(y * size[1], 1000))] for x, y in ring]


def _grid_part(rings: tuple) -> dict:
    """@return 模型输出里的一个多边形部件。"""
    return {"exterior": [list(point) for point in rings[0]], "holes": [[list(point) for point in hole] for hole in rings[1:]]}


def draw_fixed() -> None:
    """画固定候选对应的图片，并写模型输出夹具与独立算出的期望几何。"""
    image = Image.new("RGB", FIXED_SIZE, BACKGROUND)
    draw = ImageDraw.Draw(image)
    colors = (FIXED_COLORS[0], FIXED_COLORS[1], FIXED_COLORS[1], FIXED_COLORS[2])
    for rings, color in zip(FIXED_REGIONS, colors):
        draw.polygon([tuple(point) for point in _pixel_ring(rings[0], FIXED_SIZE)], fill=color)
        for hole in rings[1:]:
            draw.polygon([tuple(point) for point in _pixel_ring(hole, FIXED_SIZE)], fill=BACKGROUND)
    image.save(_ensure(DATA / "fixed" / "image_1.png"))
    expected = []
    for index, parts in enumerate(FIXED_REGION_PARTS, start=1):
        coordinates = [[_pixel_ring(ring, FIXED_SIZE) for ring in rings] for rings in parts]
        geometry = ({"type": "Polygon", "coordinates": coordinates[0]} if len(coordinates) == 1
                    else {"type": "MultiPolygon", "coordinates": coordinates})
        expected.append({"region_id": f"r{index}", "geometry": geometry, "box_id": None, "source_box_ids": []})
    regions = [{"box_id": None, "polygons": [_grid_part(rings) for rings in parts], "source_box_ids": None}
               for parts in FIXED_REGION_PARTS]
    write_json(ROOT / "fixtures" / "fixed-candidates.json", {
        "image": {"width": FIXED_SIZE[0], "height": FIXED_SIZE[1]},
        "model_output": {"regions": regions},
        "expected_items": expected,
    })


def main() -> None:
    """重新生成全部示例数据。"""
    draw_photos()
    draw_screen(DATA / "screens-xml" / "image_1.png", "Welcome", BUTTONS)
    write_text(DATA / "screens-xml" / "image_1.xml", uiautomator_xml(BUTTONS))
    draw_screen(DATA / "screens-xml" / "image_2.png", "Loading", ())
    write_text(DATA / "screens-xml" / "image_2.xml", uiautomator_xml(()))
    draw_screen(DATA / "screens-json" / "image_1.png", "Account", BUTTONS[:2])
    write_json(DATA / "screens-json" / "image_1.json", accessibility_tree(BUTTONS[:2]))
    draw_document()
    draw_fixed()


if __name__ == "__main__":
    main()
