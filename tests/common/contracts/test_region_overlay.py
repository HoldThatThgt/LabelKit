"""叠图渲染的像素判据（SPEC-image-region-segmentation §11，AC-101 至 AC-115）；不调用 LLM。"""
from __future__ import annotations

import base64
import dataclasses
import io
import random
import re
import tempfile
import tomllib
from pathlib import Path

import pytest
from PIL import Image, ImageChops, ImageDraw, ImageFont

from labelkit.common.contracts import regions as regions_module
from labelkit.common.contracts.regions import (
    OVERLAY_BASE_COLOR,
    OVERLAY_COLORS,
    OVERLAY_FILL_ALPHA,
    OVERLAY_OUTLINE_PX,
    CandidateRegion,
    ReferenceBox,
    RegionOverlayRef,
    RegionPolygon,
    RegionSet,
)
from labelkit.common.contracts.types import ImageRef, scaled_size

ROOT = Path(__file__).resolve().parents[3]
SPEC = ROOT / "docs" / "dev" / "SPEC-image-region-segmentation.md"
SOLID = (50, 100, 150)
PURE_WHITE = (255, 255, 255)
PURE_BLACK = (0, 0, 0)


def rect(left, top, right, bottom):
    return ((left, top), (right, top), (right, bottom), (left, bottom), (left, top))


def polygon(exterior, *holes) -> RegionPolygon:
    return RegionPolygon(exterior=exterior, holes=holes)


def candidate(region_id: str, *polygons: RegionPolygon, anchor) -> CandidateRegion:
    return CandidateRegion(region_id=region_id, polygons=polygons, box_id=None, source_box_ids=(),
                           label_anchor=anchor)


def region_set(size: tuple[int, int], *regions: CandidateRegion, boxes: tuple[ReferenceBox, ...] = ()) -> RegionSet:
    return RegionSet(width=size[0], height=size[1], boxes=boxes, regions=regions)


def image_ref(tmp_path: Path, image: Image.Image, name: str = "image_1.png", **save) -> ImageRef:
    path = tmp_path / name
    image.save(path, **save)
    return ImageRef(path=path, format="jpeg" if name.endswith(".jpg") else "png", size_bytes=path.stat().st_size)


def solid_ref(tmp_path: Path, size: tuple[int, int], color=SOLID) -> ImageRef:
    return image_ref(tmp_path, Image.new("RGB", size, color))


def noise_image(size: tuple[int, int], seed: int = 5) -> Image.Image:
    return Image.frombytes("RGB", size, random.Random(seed).randbytes(3 * size[0] * size[1]))


def decode(payload: tuple[str, str]) -> Image.Image:
    image = Image.open(io.BytesIO(base64.b64decode(payload[1])))
    image.load()
    return image


def render(ref: ImageRef, regions: RegionSet, max_px: int = 4000) -> Image.Image:
    return decode(RegionOverlayRef(base=ref, regions=regions).load_base64(max_px))


def scaled_base(ref: ImageRef, max_px: int) -> Image.Image:
    with Image.open(ref.path) as source:
        size = scaled_size(*source.size, max_px)
        return (source.resize(size, Image.Resampling.LANCZOS) if size != source.size else source).convert("RGB")


def blend(base: tuple[int, int, int], color: tuple[int, int, int]) -> tuple[float, ...]:
    return tuple((b * (255 - OVERLAY_FILL_ALPHA) + c * OVERLAY_FILL_ALPHA) / 255 for b, c in zip(base, color))


def assert_close(actual, expected, tolerance: float = 1) -> None:
    assert len(actual) == len(expected)
    assert all(abs(a - e) <= tolerance for a, e in zip(actual, expected)), (actual, expected)


def request_point(point, source: tuple[int, int], size: tuple[int, int]) -> tuple[int, int]:
    """§11.1 的冻结公式，测试侧独立实现。"""
    return (min(round(point[0] * size[0] / source[0]), size[0] - 1),
            min(round(point[1] * size[1] / source[1]), size[1] - 1))


def label_style(long_edge: int) -> dict:
    font_size = max(12, round(long_edge * 0.012))
    return {"font": ImageFont.load_default(size=font_size), "anchor": "mm",
            "stroke_width": max(1, font_size // 8)}


def draw_label(image: Image.Image, position, text: str, long_edge: int) -> None:
    ImageDraw.Draw(image).text(position, text, fill=PURE_WHITE, stroke_fill=PURE_BLACK, **label_style(long_edge))


def count_pixels(image: Image.Image, color: tuple[int, int, int]) -> int:
    return {found: count for count, found in image.getcolors(image.width * image.height)}.get(color, 0)


# ---- 请求尺寸（AC-08、AC-101） ----


@pytest.mark.parametrize("max_px", [4000, 2048, 1568, 1024, 512, 100])
@pytest.mark.parametrize("size", [(1080, 2340), (2340, 1080), (640, 480), (1000, 1000), (3, 2000)])
def test_overlay_and_base_request_size_match_on_working_points(tmp_path, size, max_px):
    ref = solid_ref(tmp_path, size)
    regions = region_set(size, candidate("r1", polygon(rect(0, 0, size[0], size[1])),
                                         anchor=(size[0] / 2, size[1] / 2)))
    overlay = render(ref, regions, max_px)
    assert overlay.size == decode(ref.load_base64(max_px)).size == scaled_size(*size, max_px)


@pytest.mark.parametrize(("size", "max_px", "box", "expected"), [
    ((1080, 2340), 1024, (108, 234, 756, 1638), (47, 102, 331, 717)),
    ((2340, 1080), 512, (234, 108, 1638, 756), (51, 24, 358, 165)),
    ((999, 333), 400, (100, 50, 900, 300), (40, 20, 360, 120)),
])
def test_geometry_scaled_with_same_axis_ratios(tmp_path, size, max_px, box, expected):
    ref = solid_ref(tmp_path, size, PURE_WHITE)
    left, top, right, bottom = box
    regions = region_set(size, candidate("r1", polygon(rect(*box)), anchor=((left + right) / 2, (top + bottom) / 2)))
    overlay = render(ref, regions, max_px)
    req_left, req_top, req_right, req_bottom = expected
    assert request_point((left, top), size, overlay.size) == (req_left, req_top)
    assert request_point((right, bottom), size, overlay.size) == (req_right, req_bottom)
    changed = ImageChops.difference(overlay, Image.new("RGB", overlay.size, PURE_WHITE)).getbbox()
    assert changed == (req_left, req_top, req_right + 1, req_bottom + 1)
    assert overlay.getpixel((req_left, req_top)) == OVERLAY_COLORS[0]
    assert overlay.getpixel((req_right, req_bottom)) == OVERLAY_COLORS[0]


def test_right_and_bottom_canvas_edges_map_inside_the_canvas(tmp_path):
    size = (1000, 500)
    ref = solid_ref(tmp_path, size)
    regions = region_set(size, candidate("r1", polygon(rect(0, 0, 1000, 500)), anchor=(500, 250)))
    overlay = render(ref, regions, 400)
    assert overlay.size == (400, 200)
    for corner in ((0, 0), (399, 0), (399, 199), (0, 199)):
        assert overlay.getpixel(corner) == OVERLAY_COLORS[0]


def test_reference_boxes_are_not_drawn(tmp_path):
    size = (300, 300)
    ref = solid_ref(tmp_path, size)
    drawn = ReferenceBox(box_id="n1", bbox=(10, 10, 100, 100), text="kept")
    ignored = ReferenceBox(box_id="n2", bbox=(150, 150, 280, 280), text="unused")
    region = candidate("n1", polygon(rect(10, 10, 100, 100)), anchor=(55, 55))
    overlay = render(ref, region_set(size, region, boxes=(drawn, ignored)))
    assert overlay.crop((101, 101, 300, 300)).getcolors() == [(199 * 199, SOLID)]
    assert render(ref, region_set(size, region)).tobytes() == overlay.tobytes()


# ---- 合成链路（AC-103） ----


def half_transparent_rgba() -> Image.Image:
    image = Image.new("RGBA", (20, 20), (0, 0, 0, 0))
    image.paste((0, 0, 255, 128), (10, 0, 20, 20))
    return image


def test_transparent_base_composited_on_white(tmp_path):
    ref = image_ref(tmp_path, half_transparent_rgba())
    overlay = render(ref, region_set((20, 20)))
    assert overlay.mode == "RGB"
    assert overlay.getpixel((5, 5)) == OVERLAY_BASE_COLOR
    assert_close(overlay.getpixel((15, 5)), (255 * 127 / 255, 255 * 127 / 255, 255))


@pytest.mark.parametrize(("image", "save", "transparent", "opaque"), [
    (Image.new("LA", (4, 4), (10, 0)), {}, (0, 0), None),
    (Image.new("P", (4, 4), 0), {"transparency": 0}, (0, 0), None),
    (Image.new("RGB", (4, 4), (9, 8, 7)), {"transparency": (9, 8, 7)}, (0, 0), None),
    (Image.new("RGB", (4, 4), (9, 8, 7)), {"transparency": (1, 1, 1)}, None, (0, 0)),
])
def test_every_transparency_form_flattens_on_white(tmp_path, image, save, transparent, opaque):
    ref = image_ref(tmp_path, image, **save)
    overlay = render(ref, region_set((4, 4)))
    if transparent is not None:
        assert overlay.getpixel(transparent) == OVERLAY_BASE_COLOR
    if opaque is not None:
        assert overlay.getpixel(opaque) == (9, 8, 7)


def test_transparent_base_scaled_then_composited(tmp_path):
    source = Image.new("RGBA", (200, 200), (0, 0, 255, 128))
    overlay = render(image_ref(tmp_path, source), region_set((200, 200)), 100)
    assert overlay.size == (100, 100)
    assert_close(overlay.getpixel((50, 50)), (127, 127, 255), 2)


# ---- 填充（AC-104 至 AC-108） ----


@pytest.mark.parametrize(("source", "max_px"), [((300, 150), 4000), ((600, 300), 300)])
def test_region_pixels_blend_color_at_frozen_alpha(tmp_path, source, max_px):
    ref = image_ref(tmp_path, noise_image(source))
    scale = source[0] / 300
    box = tuple(int(value * scale) for value in (50, 25, 250, 125))
    regions = region_set(source, candidate("r1", polygon(rect(*box)), anchor=(70 * scale, 40 * scale)))
    overlay = render(ref, regions, max_px)
    base = scaled_base(ref, max_px)
    assert overlay.size == (300, 150)
    for x in range(110, 245):
        for y in range(60, 120):
            assert_close(overlay.getpixel((x, y)), blend(base.getpixel((x, y)), OVERLAY_COLORS[0]))
    # 区域外逐值等于缩放原图：填充与轮廓都没有越出候选。
    outside = overlay.copy()
    outside.paste(0, (50, 25, 251, 126))
    expected = base.copy()
    expected.paste(0, (50, 25, 251, 126))
    assert outside.tobytes() == expected.tobytes()


@pytest.mark.parametrize("levels", [1, 2, 5, 8])
def test_nested_regions_keep_constant_opacity(tmp_path, levels):
    size = (400, 400)
    ref = solid_ref(tmp_path, size)
    nested = [candidate(f"r{index + 1}", polygon(rect(10 * index, 10 * index, 399 - 10 * index, 399 - 10 * index)),
                        anchor=(10 * index + 12, 10 * index + 12))
              for index in range(levels)]
    pixel = render(ref, region_set(size, *nested)).getpixel((200, 200))
    color = OVERLAY_COLORS[levels - 1]
    assert_close(pixel, blend(SOLID, color))
    channel = max(range(3), key=lambda index: abs(color[index] - SOLID[index]))
    opacity = (pixel[channel] - SOLID[channel]) / (color[channel] - SOLID[channel])
    assert opacity == pytest.approx(OVERLAY_FILL_ALPHA / 255, abs=0.01)


@pytest.mark.parametrize(("source", "max_px"), [((300, 300), 4000), ((600, 600), 300)])
def test_hole_pixels_equal_scaled_base(tmp_path, source, max_px):
    ref = image_ref(tmp_path, noise_image(source))
    scale = source[0] // 300
    outer = rect(30 * scale, 30 * scale, 270 * scale, 270 * scale)
    hole = rect(100 * scale, 100 * scale, 200 * scale, 200 * scale)
    regions = region_set(source, candidate("r1", polygon(outer, hole), anchor=(50 * scale, 50 * scale)))
    overlay = render(ref, regions, max_px)
    base = scaled_base(ref, max_px)
    inside = (101, 101, 200, 200)
    assert overlay.crop(inside).tobytes() == base.crop(inside).tobytes()
    assert_close(overlay.getpixel((80, 150)), blend(base.getpixel((80, 150)), OVERLAY_COLORS[0]))


def test_hole_keeps_other_region_fill(tmp_path):
    size = (300, 300)
    ref = solid_ref(tmp_path, size)
    under = candidate("r1", polygon(rect(10, 10, 290, 290)), anchor=(25, 25))
    over = candidate("r2", polygon(rect(60, 60, 240, 240), rect(110, 110, 190, 190)), anchor=(80, 80))
    overlay = render(ref, region_set(size, under, over))
    assert_close(overlay.getpixel((150, 150)), blend(SOLID, OVERLAY_COLORS[0]))
    assert_close(overlay.getpixel((85, 150)), blend(SOLID, OVERLAY_COLORS[1]))
    assert_close(overlay.getpixel((30, 150)), blend(SOLID, OVERLAY_COLORS[0]))


def test_later_region_overwrites_earlier_color_in_overlap(tmp_path):
    size = (300, 300)
    ref = solid_ref(tmp_path, size)
    first = candidate("r1", polygon(rect(10, 10, 200, 200)), anchor=(25, 25))
    second = candidate("r2", polygon(rect(100, 100, 290, 290)), anchor=(270, 270))
    overlay = render(ref, region_set(size, first, second))
    assert_close(overlay.getpixel((50, 150)), blend(SOLID, OVERLAY_COLORS[0]))
    assert_close(overlay.getpixel((150, 150)), blend(SOLID, OVERLAY_COLORS[1]))


def test_hole_island_fixture_is_part_order_independent(tmp_path):
    size = (300, 300)
    ref = solid_ref(tmp_path, size)
    ring = polygon(rect(20, 20, 280, 280), rect(80, 80, 220, 220))
    island = polygon(rect(120, 120, 180, 180))
    forward = RegionOverlayRef(ref, region_set(size, candidate("r1", ring, island, anchor=(40, 40))))
    backward = RegionOverlayRef(ref, region_set(size, candidate("r1", island, ring, anchor=(40, 40))))
    assert forward.load_base64(4000) == backward.load_base64(4000)
    overlay = decode(forward.load_base64(4000))
    assert_close(overlay.getpixel((150, 150)), blend(SOLID, OVERLAY_COLORS[0]))  # 岛着色
    assert overlay.getpixel((100, 100)) == SOLID                                 # 孔洞不着色
    assert_close(overlay.getpixel((50, 150)), blend(SOLID, OVERLAY_COLORS[0]))   # 带孔外环着色


def _full_canvas_render(ref: ImageRef, regions: RegionSet, max_px: int) -> Image.Image:
    """整幅掩膜的参考实现：每个部件、每个区域都用请求画布大小的掩膜。"""
    source_size = (regions.width, regions.height)
    canvas = scaled_base(ref, max_px).convert("RGBA")
    size = canvas.size
    layer = Image.new("RGBA", size, (0, 0, 0, 0))
    mapped = [[[[request_point(point, source_size, size) for point in ring] for ring in (part.exterior, *part.holes)]
               for part in region.polygons] for region in regions.regions]
    for index, parts in enumerate(mapped):
        merged = Image.new("L", size, 0)
        for exterior, *holes in parts:
            mask = Image.new("L", size, 0)
            ImageDraw.Draw(mask).polygon(exterior, fill=255)
            for hole in holes:
                ImageDraw.Draw(mask).polygon(hole, fill=0)
            merged = ImageChops.lighter(merged, mask)
        layer.paste((*OVERLAY_COLORS[index % 8], OVERLAY_FILL_ALPHA), (0, 0, *size), merged)
    composed = Image.alpha_composite(canvas, layer)
    draw = ImageDraw.Draw(composed)
    for index, parts in enumerate(mapped):
        for part in parts:
            for ring in part:
                draw.line(ring, fill=OVERLAY_COLORS[index % 8], width=1)
    long_edge = max(size)
    for region in regions.regions:
        x, y = request_point(region.label_anchor, source_size, size)
        left, top, right, bottom = draw.textbbox((x, y), region.region_id, **label_style(long_edge))
        shift_x = -left if left < 0 or right - left > size[0] else (size[0] - right if right > size[0] else 0)
        shift_y = -top if top < 0 or bottom - top > size[1] else (size[1] - bottom if bottom > size[1] else 0)
        draw_label(composed, (x + shift_x, y + shift_y), region.region_id, long_edge)
    return composed.convert("RGB")


# 这三个三角形平移到小掩膜后，Pillow 12.3.0 的 float32 扫描会有 1 个像素取整不同。
TIE_TRIANGLES = (
    ((502, 1721), (547, 1779), (488, 1733)),
    ((520, 1579), (545, 1588), (492, 1638)),
    ((700, 682), (735, 699), (673, 755)),
)


@pytest.mark.parametrize("max_px", [4000, 1024, 200])
def test_bbox_mask_matches_full_canvas_mask(tmp_path, max_px):
    size = (945, 2048)
    ref = image_ref(tmp_path, noise_image(size))
    triangles = [candidate(f"r{index + 1}", polygon((*points, points[0])), anchor=points[0])
                 for index, points in enumerate(TIE_TRIANGLES)]
    regions = region_set(
        size,
        candidate("r4", polygon(rect(0, 0, 945, 2048), rect(300, 300, 600, 600)), anchor=(50, 50)),
        candidate("r5", polygon(rect(0, 0, 40, 40)), polygon(rect(905, 2008, 945, 2048)), anchor=(20, 20)),
        candidate("r6", polygon(rect(900, 0, 945, 2048)), anchor=(944, 2047)),
        candidate("r7", polygon(rect(350, 350, 550, 550), rect(400, 400, 500, 500)),
                  polygon(rect(430, 430, 470, 470)), anchor=(375, 375)),
        *triangles,
    )
    assert render(ref, regions, max_px).tobytes() == _full_canvas_render(ref, regions, max_px).tobytes()


def test_region_mask_covers_only_padded_bbox_and_matches_full_mask():
    size = (945, 2048)
    rng = random.Random(23)
    scratch = Image.new("L", size, 0)
    shapes = [[list(points) + [points[0]]] for points in TIE_TRIANGLES]
    for _ in range(400):
        left, top = rng.randint(0, size[0] - 100), rng.randint(0, size[1] - 100)
        shapes.append([[(left + rng.randint(0, 99), top + rng.randint(0, 99)) for _ in range(rng.randint(3, 8))]])
    shapes.append([[(0, 0), (944, 0), (944, 2047), (0, 2047), (0, 0)]])
    for part in shapes:
        xs, ys = [x for x, _ in part[0]], [y for _, y in part[0]]
        box, mask = regions_module._region_mask([part], scratch)
        assert box == (max(0, min(xs) - 1), max(0, min(ys) - 1), min(size[0], max(xs) + 2), min(size[1], max(ys) + 2))
        assert mask.mode == "L" and mask.size == (box[2] - box[0], box[3] - box[1])
        full = Image.new("L", size, 0)
        ImageDraw.Draw(full).polygon(part[0], fill=255)
        assert mask.tobytes() == full.crop(box).tobytes()
        assert full.getbbox() is None or ImageChops.difference(full, _pasted(mask, box, size)).getbbox() is None
        assert scratch.getbbox() is None


def _pasted(mask: Image.Image, box: tuple[int, int, int, int], size: tuple[int, int]) -> Image.Image:
    full = Image.new("L", size, 0)
    full.paste(mask, box)
    return full


# ---- 轮廓（AC-109） ----


@pytest.mark.parametrize(("source", "max_px"), [((200, 200), 4000), ((400, 400), 200)])
def test_outline_is_one_request_pixel(tmp_path, source, max_px):
    scale = source[0] // 200
    ref = solid_ref(tmp_path, source)
    regions = region_set(source, candidate("r1", polygon(rect(*(value * scale for value in (40, 40, 160, 160)))),
                                           anchor=(100 * scale, 100 * scale)))
    overlay = render(ref, regions, max_px)
    color = OVERLAY_COLORS[0]
    filled = blend(SOLID, color)
    assert OVERLAY_OUTLINE_PX == 1
    assert overlay.getpixel((39, 70)) == SOLID and overlay.getpixel((161, 70)) == SOLID
    assert overlay.getpixel((70, 39)) == SOLID and overlay.getpixel((70, 161)) == SOLID
    for pixel in ((40, 70), (160, 70), (70, 40), (70, 160)):
        assert overlay.getpixel(pixel) == color
    for pixel in ((41, 70), (159, 70), (70, 41), (70, 159)):
        assert_close(overlay.getpixel(pixel), filled)


def test_diagonal_outline_has_one_pixel_per_crossing(tmp_path):
    size = (200, 200)
    ref = solid_ref(tmp_path, size)
    diamond = ((100, 20), (180, 100), (100, 180), (20, 100), (100, 20))
    overlay = render(ref, region_set(size, candidate("r1", polygon(diamond), anchor=(100, 100))))
    row = [x for x in range(200) if overlay.getpixel((x, 60)) == OVERLAY_COLORS[0]]
    assert row == [60, 140]


def test_outline_drawn_on_inner_rings(tmp_path):
    size = (300, 300)
    ref = solid_ref(tmp_path, size)
    regions = region_set(size, candidate("r1", polygon(rect(20, 20, 280, 280), rect(100, 100, 200, 200)),
                                         anchor=(40, 40)))
    overlay = render(ref, regions)
    color = OVERLAY_COLORS[0]
    assert_close(overlay.getpixel((99, 150)), blend(SOLID, color))
    assert overlay.getpixel((100, 150)) == color
    assert overlay.getpixel((101, 150)) == SOLID
    assert overlay.getpixel((199, 150)) == SOLID
    assert overlay.getpixel((200, 150)) == color
    assert_close(overlay.getpixel((201, 150)), blend(SOLID, color))


# ---- 颜色（AC-110） ----


def test_colors_cycle_in_region_order(tmp_path):
    assert OVERLAY_COLORS == ((230, 25, 75), (60, 180, 75), (0, 130, 200), (245, 130, 48),
                              (145, 30, 180), (0, 170, 170), (240, 50, 230), (128, 128, 0))
    assert OVERLAY_FILL_ALPHA == 64 and OVERLAY_BASE_COLOR == (255, 255, 255)
    size = (500, 100)
    ref = solid_ref(tmp_path, size)
    squares = [candidate(f"r{index + 1}", polygon(rect(index * 50 + 5, 5, index * 50 + 45, 95)),
                         anchor=(index * 50 + 25, 70)) for index in range(10)]
    overlay = render(ref, region_set(size, *squares))
    seen = [overlay.getpixel((index * 50 + 5, 20)) for index in range(10)]
    assert seen == [OVERLAY_COLORS[index % 8] for index in range(10)]
    assert seen[8] == seen[0] and seen[9] == seen[1]
    for index in range(10):
        assert_close(overlay.getpixel((index * 50 + 15, 20)), blend(SOLID, OVERLAY_COLORS[index % 8]))


# ---- 标识（AC-111、AC-112） ----


@pytest.mark.parametrize(("source", "max_px"), [
    ((300, 300), 4000),    # 原尺寸，字号 12，描边 1
    ((600, 600), 300),     # 缩放后锚点换算
    ((1080, 2340), 1024),  # 两轴比例不同，字号 12
    ((2048, 200), 4000),   # 字号 25，描边 3
])
def test_label_text_equals_region_id_and_is_opaque(tmp_path, source, max_px):
    ref = solid_ref(tmp_path, source)
    width, height = source
    region_id = "button.submit-12"
    anchor = (width * 0.5, height * 0.5)
    box = (width // 100, height // 100, width - width // 100, height - height // 100)
    overlay = render(ref, region_set(source, candidate(region_id, polygon(rect(*box)), anchor=anchor)), max_px)
    position = request_point(anchor, source, overlay.size)
    long_edge = max(overlay.size)
    background = overlay.getpixel((position[0], overlay.size[1] // 100 + 2))
    assert_close(background, blend(SOLID, OVERLAY_COLORS[0]))

    def expected_with(text: str) -> Image.Image:
        image = Image.new("RGBA", overlay.size, (*background, 255))
        draw_label(image, position, text, long_edge)
        return image.convert("RGB")

    area = ImageDraw.Draw(overlay).textbbox(position, region_id, **label_style(long_edge))
    crop = overlay.crop(area)
    assert crop.tobytes() == expected_with(region_id).crop(area).tobytes()
    # 截断的标识不会通过上面的逐字节比较。
    assert crop.tobytes() != expected_with(region_id[:-1]).crop(area).tobytes()
    assert count_pixels(crop, PURE_WHITE) > 0 and count_pixels(crop, PURE_BLACK) > 0


@pytest.mark.parametrize(("size", "max_px", "font_size"), [
    ((512, 40), 4000, 12),
    ((1024, 40), 4000, 12),
    ((1568, 40), 4000, 19),
    ((2048, 40), 4000, 25),
    ((4000, 40), 4000, 48),
    ((4000, 40), 2048, 25),   # 按请求画布长边，不按原图长边
])
def test_label_font_size_formula(tmp_path, monkeypatch, size, max_px, font_size):
    real = ImageFont.load_default
    sizes: list[float] = []

    def spy(size=None):
        sizes.append(size)
        return real(size=size)

    monkeypatch.setattr(ImageFont, "load_default", spy)
    ref = solid_ref(tmp_path, size)
    regions = region_set(size, candidate("r1", polygon(rect(0, 0, *size)), anchor=(10, 10)))
    render(ref, regions, max_px)
    assert sizes == [font_size]


@pytest.mark.parametrize(("anchor", "box"), [
    ((299, 299), (200, 200, 299, 299)),
    ((0, 0), (0, 0, 100, 100)),
    ((299, 0), (200, 0, 299, 100)),
    ((0, 299), (0, 200, 100, 299)),
])
def test_long_label_near_corner_stays_inside_canvas(tmp_path, anchor, box):
    size = (300, 300)
    region_id = "very-long-region-name-42"
    ref = solid_ref(tmp_path, size)
    region = candidate(region_id, polygon(rect(*box)), anchor=anchor)
    overlay = render(ref, region_set(size, region))
    # 大画布上完整绘制同一标识：叠图里纯白、纯黑像素数相同，说明没有任何部分被画布截掉。
    reference = Image.new("RGBA", (600, 600), (*SOLID, 255))
    draw_label(reference, (300, 300), region_id, 300)
    reference = reference.convert("RGB")
    assert count_pixels(overlay, PURE_WHITE) == count_pixels(reference, PURE_WHITE) > 0
    assert count_pixels(overlay, PURE_BLACK) == count_pixels(reference, PURE_BLACK) > 0
    # 平移不改几何：轮廓仍在原位；载体的 label_anchor 不变。
    assert region.label_anchor == anchor
    corner = (box[0], box[3]) if anchor[1] == 0 else (box[2], box[1])
    assert overlay.getpixel(corner) == OVERLAY_COLORS[0]


@pytest.mark.parametrize(("size", "anchor"), [((60, 300), (30, 150)), ((300, 8), (150, 4))])
def test_label_larger_than_canvas_aligns_to_start(tmp_path, size, anchor):
    region_id = "very-long-region-name-42"
    ref = solid_ref(tmp_path, size)
    overlay = render(ref, region_set(size, candidate(region_id, polygon(rect(0, 0, 1, 1)), anchor=anchor)))
    expected = Image.new("RGBA", size, (*SOLID, 255))
    draw = ImageDraw.Draw(expected)
    draw.polygon([(0, 0), (1, 0), (1, 1), (0, 1)], fill=(*OVERLAY_COLORS[0], 255))
    left, top, right, bottom = draw.textbbox(anchor, region_id, **label_style(max(size)))
    shift_x = -left if right - left > size[0] else 0
    shift_y = -top if bottom - top > size[1] else 0
    assert (shift_x != 0) or (shift_y != 0)
    draw_label(expected, (anchor[0] + shift_x, anchor[1] + shift_y), region_id, max(size))
    crop = (2, 2, *size)
    assert overlay.crop(crop).tobytes() == expected.convert("RGB").crop(crop).tobytes()


def test_labels_draw_after_every_outline_in_tuple_order(tmp_path):
    size = (300, 300)
    ref = solid_ref(tmp_path, size)
    # r2 的轮廓穿过 r1 的标识中心；标识画在所有轮廓之后，所以中心纯白不被轮廓覆盖。
    first = candidate("r1", polygon(rect(10, 10, 290, 290)), anchor=(150, 150))
    second = candidate("r2", polygon(rect(150, 40, 260, 260)), anchor=(205, 60))
    overlay = render(ref, region_set(size, first, second))
    reference = Image.new("RGBA", size, (*SOLID, 255))
    draw_label(reference, (150, 150), "r1", 300)
    assert count_pixels(overlay.crop((130, 135, 170, 165)), PURE_WHITE) == count_pixels(
        reference.convert("RGB").crop((130, 135, 170, 165)), PURE_WHITE)


# ---- 编码、确定性与原图不动（AC-113、AC-114） ----


@pytest.mark.parametrize(("image", "name"), [
    (Image.new("RGB", (40, 30), SOLID), "image_1.jpg"),
    (Image.new("RGB", (40, 30), SOLID), "image_1.png"),
    (Image.new("RGBA", (40, 30), (*SOLID, 128)), "image_1.png"),
    (Image.new("L", (40, 30), 77), "image_1.png"),
    (Image.new("P", (40, 30), 3), "image_1.png"),
    (Image.new("1", (40, 30), 1), "image_1.png"),
    (Image.new("I;16", (40, 30), 4000), "image_1.png"),
    (Image.new("CMYK", (40, 30), (1, 2, 3, 4)), "image_1.jpg"),
])
def test_overlay_is_rgb_png(tmp_path, image, name):
    ref = image_ref(tmp_path, image, name)
    regions = region_set((40, 30), candidate("r1", polygon(rect(5, 5, 30, 25)), anchor=(15, 15)))
    media_type, payload = RegionOverlayRef(ref, regions).load_base64(20)
    raw = base64.b64decode(payload)
    assert media_type == "image/png"
    assert raw.startswith(b"\x89PNG\r\n\x1a\n")
    with Image.open(io.BytesIO(raw)) as decoded:
        assert decoded.format == "PNG" and decoded.mode == "RGB"
        assert decoded.size == (20, 15)


def test_pyproject_pillow_lower_bound():
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert "Pillow>=10.1" in project["project"]["dependencies"]


@pytest.mark.parametrize("max_px", [4000, 300])
def test_same_build_renders_identical_bytes(tmp_path, max_px):
    size = (600, 400)
    ref = image_ref(tmp_path, noise_image(size))
    regions = region_set(size, candidate("n12", polygon(rect(10, 10, 300, 300)), anchor=(155.5, 155.25)),
                         candidate("r1", polygon(((400, 50), (590, 200), (420, 380), (400, 50))), anchor=(450.0, 200.0)))
    first = RegionOverlayRef(ref, regions).load_base64(max_px)
    assert RegionOverlayRef(ref, regions).load_base64(max_px) == first
    overlay = RegionOverlayRef(ref, regions)
    assert overlay.load_base64(max_px) == overlay.load_base64(max_px) == first


def test_render_writes_no_files_and_keeps_base_unchanged(tmp_path, monkeypatch):
    workdir = tmp_path / "cwd"
    scratch = tmp_path / "tmp"
    store = tmp_path / "input"
    for path in (workdir, scratch, store):
        path.mkdir()
    monkeypatch.chdir(workdir)
    monkeypatch.setattr(tempfile, "tempdir", str(scratch))
    ref = image_ref(store, noise_image((300, 200)))
    original = ref.path.read_bytes()
    stamp = ref.path.stat().st_mtime_ns
    targets = []
    real_save = Image.Image.save

    def spy(self, fp, *args, **kwargs):
        targets.append(fp)
        return real_save(self, fp, *args, **kwargs)

    monkeypatch.setattr(Image.Image, "save", spy)
    overlay = RegionOverlayRef(ref, region_set((300, 200), candidate("r1", polygon(rect(10, 10, 250, 150)),
                                                                     anchor=(40, 40))))
    before = sorted(path for path in tmp_path.rglob("*"))
    overlay.load_base64(4000)
    overlay.load_base64(100)
    assert sorted(path for path in tmp_path.rglob("*")) == before
    assert ref.path.read_bytes() == original and ref.path.stat().st_mtime_ns == stamp
    assert len(targets) == 2 and all(isinstance(target, io.BytesIO) for target in targets)
    # 载体不保留字节：实例上只有两个声明字段。
    assert [field.name for field in dataclasses.fields(RegionOverlayRef)] == ["base", "regions"]
    assert set(vars(overlay)) == {"base", "regions"}


# ---- 边界行为 ----


def test_empty_regions_render_the_flattened_base_only(tmp_path):
    ref = image_ref(tmp_path, noise_image((200, 100)))
    overlay = render(ref, region_set((200, 100)), 100)
    assert overlay.tobytes() == scaled_base(ref, 100).tobytes()


def test_region_collapsed_below_one_request_pixel_still_renders(tmp_path):
    size = (4000, 2000)
    ref = solid_ref(tmp_path, size)
    regions = region_set(size, candidate("r1", polygon(rect(1000.0, 1000.0, 1001.0, 1001.0)), anchor=(1000.5, 1000.5)))
    overlay = render(ref, regions, 100)
    assert overlay.size == (100, 50)
    assert count_pixels(overlay, SOLID) < 100 * 50


def test_exif_orientation_is_not_applied(tmp_path):
    exif = Image.Exif()
    exif[0x0112] = 6
    size = (40, 20)
    ref = image_ref(tmp_path, Image.new("RGB", size, SOLID), "image_1.jpg", exif=exif.tobytes())
    regions = region_set(size, candidate("r1", polygon(rect(0, 0, 10, 10)), anchor=(5, 5)))
    assert render(ref, regions, 4000).size == size
    assert render(ref, regions, 20).size == (20, 10)


@pytest.mark.parametrize("name", ["image_1.png", "image_1.jpg"])
def test_truncated_file_raises_oserror(tmp_path, name):
    ref = image_ref(tmp_path, noise_image((64, 48)), name)
    data = ref.path.read_bytes()
    ref.path.write_bytes(data[:len(data) // 3])
    with pytest.raises(OSError):
        RegionOverlayRef(ref, region_set((64, 48))).load_base64(4000)


def test_missing_file_raises_oserror(tmp_path):
    ref = ImageRef(path=tmp_path / "gone.png", format="png", size_bytes=1)
    with pytest.raises(OSError):
        RegionOverlayRef(ref, region_set((4, 4))).load_base64(4000)


# ---- 成本登记（AC-115） ----


def test_spec_cpu_table_is_filled():
    text = SPEC.read_text(encoding="utf-8")
    section = text.split("### 11.8 ", 1)[1].split("\n### ", 1)[0]
    assert "待 S4a" not in section
    rows = [line for line in section.splitlines() if re.match(r"\| \d+ \|", line)]
    points = {int(row.split("|")[1]) for row in rows}
    assert points >= {512, 1024, 2048, 4000}
    for row in rows:
        _, point, request, count, median, png_bytes, _ = (cell.strip() for cell in row.split("|"))
        width, height = (int(value) for value in request.split("×"))
        assert max(width, height) == int(point)
        assert int(count) > 0 and float(median) > 0 and int(png_bytes.replace(",", "")) > 0
    for fact in ("Pillow 12.3.0", "FreeType 2.14.3", "重复"):
        assert fact in section
