"""图片模式候选载体、显示投影与叠图渲染（SPEC-image-region-segmentation §4.1、§5.4、§11、§16.1）。

载体只含 int、float、str、tuple，深不可变；几何真值恒为原图像素。提示词里的 0–1000 网格只是
显示投影，用精确有理运算得到，不回写真值。叠图在 M9 编码请求时惰性渲染，字节用完即弃。
"""
from __future__ import annotations

import base64
import io
import json
import math
from dataclasses import dataclass
from fractions import Fraction
from typing import TYPE_CHECKING

from labelkit.common.contracts.types import ImageRef, scaled_size

if TYPE_CHECKING:
    from PIL import Image as PILImage
    from PIL import ImageDraw as PILImageDraw

OVERLAY_FILL_ALPHA = 64                       # 64/255，约 25.1%
OVERLAY_OUTLINE_PX = 1                        # 同色、不透明，填充合成之后绘制
OVERLAY_BASE_COLOR = (255, 255, 255)
OVERLAY_COLORS = (
    (230, 25, 75), (60, 180, 75), (0, 130, 200), (245, 130, 48),
    (145, 30, 180), (0, 170, 170), (240, 50, 230), (128, 128, 0),
)

_LABEL_FILL = (255, 255, 255)
_LABEL_STROKE_FILL = (0, 0, 0)

_RequestPoint = tuple[int, int]
_RequestPart = list[list[_RequestPoint]]      # 一个部件在请求画布上的环：外环在前，内环在后


@dataclass(frozen=True)
class ReferenceBox:
    """box_parser 钩子返回并经框架校验的一个参考框（原图像素，仅轴对齐矩形，D14）。"""

    box_id: str                                        # 钩子给出的框名，全部留空时由框架按数组顺序补为 "1"…"N"
    bbox: tuple[int | float, int | float, int | float, int | float]  # (l, t, r, b)，保留钩子给出的数值类型
    text: str                                          # 钩子给模型看的描述，可为空


@dataclass(frozen=True)
class RegionPolygon:
    """单个多边形部件：原图像素闭环外环与零到多个闭环内环。"""

    exterior: tuple[tuple[int | float, int | float], ...]  # 闭环：≥4 个位置，首末相同
    holes: tuple[tuple[tuple[int | float, int | float], ...], ...]  # 每个内环同样是闭环


@dataclass(frozen=True)
class CandidateRegion:
    """一个候选区域：区域标识（框名或框架编号 r<n>）、几何部件、来源参考框与叠图标注点。"""

    region_id: str                                     # 来自参考框：框名；模型新画：框架按出现顺序编号 r1, r2, …
    polygons: tuple[RegionPolygon, ...]                # 1 个 = Polygon；多个 = MultiPolygon
    box_id: str | None                                 # 几何取自哪个参考框（复用或 D15 直通）；模型画的多边形为 None
    source_box_ids: tuple[str, ...]                    # 没有参考框时恒为空；box_id 非空时恒为 (box_id,)
    label_anchor: tuple[float, float]                  # 原图像素点，仅用于渲染，不是几何真值


@dataclass(frozen=True)
class RegionSet:
    """一个样本的完整分割结果；regions 为空元组表示合法的空候选。"""

    width: int                                         # 原图存储像素宽（Pillow 只读文件头）
    height: int
    boxes: tuple[ReferenceBox, ...]                    # 未注册钩子或无伴随文件时为空
    regions: tuple[CandidateRegion, ...]


@dataclass(frozen=True)
class RegionOverlayRef:
    """原图加候选的惰性叠图引用；与 ImageRef 共享 load_base64 接口。"""

    base: ImageRef
    regions: RegionSet

    def load_base64(self, max_px: int) -> tuple[str, str]:
        """按与 base 相同的逐轴缩放渲染叠图，字节不保留。

        @param max_px 本次请求的长边像素上限。
        @return (media_type, b64)，media_type 恒为 image/png。
        @raises OSError 文件不可读或 Pillow 解码失败（DecompressionBombError 与解码时的其他异常都转成它），由 image_decode_error 归类
        """
        from PIL import Image  # 局部导入：保持模块导入轻量；Pillow 是硬依赖

        try:
            with Image.open(self.base.path) as source:
                width, height = source.size
                scaled = source
                if max(width, height) > max_px:
                    scaled = source.resize(scaled_size(width, height, max_px), Image.Resampling.LANCZOS)
                canvas = _opaque_canvas(scaled)
        except Image.DecompressionBombError as exc:
            raise OSError("image exceeds the pixel limit") from exc
        except OSError:
            raise
        except Exception as exc:  # 损坏的 PNG 在解码时还会抛 SyntaxError、ValueError；绘制在 try 之外，其缺陷不转换
            raise OSError(f"image unreadable: {type(exc).__name__}") from exc
        buf = io.BytesIO()
        _paint_regions(canvas, self.regions).convert("RGB").save(buf, format="PNG")
        return "image/png", base64.b64encode(buf.getvalue()).decode("ascii")


def _opaque_canvas(image: PILImage.Image) -> PILImage.Image:
    """把请求尺寸的原图变成不透明 RGBA 底图；含透明度的图先合成到冻结白底。

    @param image 已缩放到请求尺寸的原图
    @return 不透明的 RGBA 底图
    """
    from PIL import Image

    if image.mode in ("RGBA", "LA", "PA") or "transparency" in image.info:
        rgba = image.convert("RGBA")
        image = Image.alpha_composite(Image.new("RGBA", rgba.size, (*OVERLAY_BASE_COLOR, 255)), rgba)
    return image.convert("RGB").convert("RGBA")


def _request_parts(region: CandidateRegion, regions: RegionSet, size: tuple[int, int]) -> list[_RequestPart]:
    """把一个候选的所有环按冻结比例换算到请求画布（§11.1）。

    @param region 待换算的候选
    @param regions 候选所属的分割结果（提供原图宽高）
    @param size 请求画布 (req_w, req_h)
    @return 逐部件的整数环，外环在前
    """
    return [[[_request_point(point, regions, size) for point in ring] for ring in (polygon.exterior, *polygon.holes)]
            for polygon in region.polygons]


def _request_point(point: tuple[int | float, int | float], regions: RegionSet,
                   size: tuple[int, int]) -> _RequestPoint:
    """把一个原图像素点换算到请求画布（§11.1）。

    @param point 原图像素点
    @param regions 提供原图宽高的分割结果
    @param size 请求画布 (req_w, req_h)
    @return 落在请求画布内的整数点
    """
    x_px, y_px = point
    req_w, req_h = size
    return (min(round(x_px * req_w / regions.width), req_w - 1),
            min(round(y_px * req_h / regions.height), req_h - 1))


def _region_mask(parts: list[_RequestPart],
                 scratch: PILImage.Image) -> tuple[tuple[int, int, int, int], PILImage.Image]:
    """生成一个区域的 L 掩膜，只覆盖外接矩形加 1 像素余量（截到画布内）。

    Pillow 的多边形扫描用 float32 算交点，把顶点平移到小掩膜上会改变 .5 附近的取整结果，
    所以各部件都按请求画布绝对坐标画在同一张暂存画布上，再裁出外接矩形并清回 0。

    @param parts 区域的逐部件整数环
    @param scratch 与请求画布同尺寸、全 0 的暂存 L 画布；返回时仍全 0
    @return (paste 用的 box, 与 box 同尺寸的掩膜)
    """
    from PIL import ImageChops, ImageDraw

    req_w, req_h = scratch.size
    xs = [x for part in parts for x, _ in part[0]]
    ys = [y for part in parts for _, y in part[0]]
    box = (max(0, min(xs) - 1), max(0, min(ys) - 1), min(req_w, max(xs) + 2), min(req_h, max(ys) + 2))
    draw = ImageDraw.Draw(scratch)
    merged = None
    for exterior, *holes in parts:
        draw.polygon(exterior, fill=255)
        for hole in holes:
            draw.polygon(hole, fill=0)
        part_mask = scratch.crop(box)
        scratch.paste(0, box)
        merged = part_mask if merged is None else ImageChops.lighter(merged, part_mask)
    return box, merged


def _paint_regions(canvas: PILImage.Image, regions: RegionSet) -> PILImage.Image:
    """在底图上按冻结样式依次合成填充层、描轮廓、写标识（§11.3–§11.6）。

    @param canvas 请求尺寸的不透明 RGBA 底图
    @param regions 样本的分割结果
    @return 绘制完成的 RGBA 图
    """
    from PIL import Image, ImageDraw

    size = canvas.size
    colored = [(OVERLAY_COLORS[index % len(OVERLAY_COLORS)], _request_parts(region, regions, size))
               for index, region in enumerate(regions.regions)]
    layer = Image.new("RGBA", size, (0, 0, 0, 0))
    scratch = Image.new("L", size, 0)
    for color, parts in colored:
        box, mask = _region_mask(parts, scratch)
        layer.paste((*color, OVERLAY_FILL_ALPHA), box, mask)
    composed = Image.alpha_composite(canvas, layer)
    draw = ImageDraw.Draw(composed)
    for color, parts in colored:
        for part in parts:
            for ring in part:
                draw.line(ring, fill=color, width=OVERLAY_OUTLINE_PX)
    _draw_labels(draw, regions, size)
    return composed


def _draw_labels(draw: PILImageDraw.ImageDraw, regions: RegionSet, size: tuple[int, int]) -> None:
    """按元组顺序绘制完整 region_id；越出画布的标识整体平移回画布内（§11.6）。

    @param draw 已合成底图的绘图句柄
    @param regions 样本的分割结果
    @param size 请求画布 (req_w, req_h)
    """
    from PIL import ImageFont

    if not regions.regions:
        return
    font_size = max(12, round(max(size) * 0.012))
    font = ImageFont.load_default(size=font_size)
    stroke = max(1, font_size // 8)
    for region in regions.regions:
        x, y = _request_point(region.label_anchor, regions, size)
        left, top, right, bottom = draw.textbbox((x, y), region.region_id, font=font, anchor="mm",
                                                 stroke_width=stroke)
        position = (x + _inside_shift(left, right, size[0]), y + _inside_shift(top, bottom, size[1]))
        draw.text(position, region.region_id, font=font, anchor="mm", fill=_LABEL_FILL,
                  stroke_fill=_LABEL_STROKE_FILL, stroke_width=stroke)


def _inside_shift(low: int | float, high: int | float, limit: int) -> int | float:
    """计算把一维区间移回 ``0..limit`` 所需的平移；比画布还长时对齐起点。

    @param low 区间起点
    @param high 区间终点（不含）
    @param limit 画布该轴长度
    @return 平移量
    """
    if low < 0 or high - low > limit:
        return -low
    if high > limit:
        return limit - high
    return 0


def _grid_box(box: ReferenceBox, width: int, height: int) -> tuple[int, int, int, int]:
    """把参考框像素矩形外扩取整到 0–1000 网格（精确有理运算，只用于显示）。

    @param box 已校验在画布内的参考框
    @param width 原图存储像素宽
    @param height 原图存储像素高
    @return 网格 (gl, gt, gr, gb)，恒包含像素框且面积为正
    """
    left, top, right, bottom = box.bbox
    return (math.floor(Fraction(left) * 1000 / width), math.floor(Fraction(top) * 1000 / height),
            math.ceil(Fraction(right) * 1000 / width), math.ceil(Fraction(bottom) * 1000 / height))


def _grid_ring(ring: tuple[tuple[int | float, int | float], ...], width: int, height: int) -> list[list[int]]:
    """把模型画的环按就近取偶还原成 0–1000 网格整数（只用于显示）。

    @param ring 原图像素闭环
    @param width 原图存储像素宽
    @param height 原图存储像素高
    @return 与原环同序的网格顶点
    """
    return [[round(Fraction(x) * 1000 / width), round(Fraction(y) * 1000 / height)] for x, y in ring]


def render_reference_boxes(regions: RegionSet) -> str:
    """渲染 ``[参考框]`` 正文：每框一行 JSON，行间 ``\n``，无末尾换行。

    @param regions 样本的分割结果
    @return 正文；没有参考框时为空串
    """
    return "\n".join(
        json.dumps({"box_id": box.box_id, "bbox": list(_grid_box(box, regions.width, regions.height)),
                    "text": box.text}, ensure_ascii=False)
        for box in regions.boxes)


def _candidate_display(region: CandidateRegion, regions: RegionSet, boxes: dict[str, ReferenceBox]) -> dict:
    """构造一个候选的提示词显示项；几何来自参考框时与 ``[参考框]`` 逐值相等。

    @param region 待显示的候选
    @param regions 候选所属的分割结果（提供画布尺寸）
    @param boxes 按框名索引的本样本参考框
    @return 键序冻结的显示项
    @raises KeyError box_id 不是本样本的参考框（载体不变式被破坏）
    """
    if region.box_id is None:
        polygons = [{"exterior": _grid_ring(polygon.exterior, regions.width, regions.height),
                     "holes": [_grid_ring(hole, regions.width, regions.height) for hole in polygon.holes]}
                    for polygon in region.polygons]
        return {"region_id": region.region_id, "polygons": polygons, "box_id": None,
                "source_box_ids": list(region.source_box_ids)}
    box = boxes[region.box_id]
    left, top, right, bottom = _grid_box(box, regions.width, regions.height)
    exterior = [[left, top], [right, top], [right, bottom], [left, bottom], [left, top]]
    return {"region_id": region.region_id, "polygons": [{"exterior": exterior, "holes": []}],
            "box_id": region.box_id, "source_box_ids": list(region.source_box_ids), "text": box.text}


def render_candidates(regions: RegionSet) -> str:
    """渲染 ``[候选区域]`` 正文：一行 JSON，网格显示投影，不是 GeoJSON。

    @param regions 样本的分割结果
    @return 正文；空候选为 ``[]``
    @raises KeyError 候选的 box_id 不是本样本的参考框（载体不变式被破坏）
    """
    boxes = {box.box_id: box for box in regions.boxes}
    return json.dumps([_candidate_display(region, regions, boxes) for region in regions.regions],
                      ensure_ascii=False)


def render_material_note(regions: RegionSet) -> str:
    """渲染 annotate 与 verify 共用的冻结材料说明。

    @param regions 样本的分割结果（提供画布尺寸）
    @return 固定说明文本
    """
    return ("原图与候选区域叠图来自同一样本；叠图中每个区域的标识就是该候选的 region_id；"
            "候选只是辅助信息，不要求全部选中。引用候选只能用 region_id；"
            "没有成为候选的参考框不能被引用。候选与参考框坐标为 0–1000 网格，"
            f"画布为 {regions.width}×{regions.height} 像素。")


def _geometry(region: CandidateRegion) -> dict:
    """把候选几何投影成 GeoJSON 嵌套形态；环序、顶点顺序与数值类型都不变。

    @param region 待投影的候选
    @return 单部件为 Polygon，多部件为 MultiPolygon
    """
    parts = [[[list(vertex) for vertex in ring] for ring in (polygon.exterior, *polygon.holes)]
             for polygon in region.polygons]
    if len(parts) == 1:
        return {"type": "Polygon", "coordinates": parts[0]}
    return {"type": "MultiPolygon", "coordinates": parts}


def project_regions(regions: RegionSet) -> dict:
    """生成 ``{"image", "boxes", "items"}`` 投影；后处理上下文与 ``_meta.regions`` 共用。

    @param regions 样本的分割结果
    @return 不共享任何容器的 JSON 树，坐标为原图像素且不取整
    """
    return {
        "image": {"width": regions.width, "height": regions.height},
        "boxes": [{"id": box.box_id, "bbox": list(box.bbox), "text": box.text} for box in regions.boxes],
        "items": [{"region_id": region.region_id, "geometry": _geometry(region), "box_id": region.box_id,
                   "source_box_ids": list(region.source_box_ids)} for region in regions.regions],
    }
