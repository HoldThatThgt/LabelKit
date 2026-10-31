"""图片候选区域示例工程的钩子：把伴随数据转成参考框，把模型选择的 region_id 补成几何。

box_parser 钩子收到伴随文件的原始字节与 {"width", "height"}。数据无法解析时返回 None 拒收样本，
数据合法但没有框时返回 []，两者不要混用。超出画布或零面积的框由钩子自己决定裁剪还是丢弃，框架不修正。

框架也不去重。控件树的两个钩子在遍历时剔除 bbox 相同的嵌套框，否则它们的标识画在同一点上，模型分不清：
同一条父子链上有文字或名称的节点优先，都有或都没有时留最内层；兄弟节点不算嵌套，不合并。

后处理钩子的第二入参在图片模式下是框架绑定的样本上下文（sample_id、source、image、boxes、regions）。
引用检查已经在调用之前挡下未知 region_id；启动探针路径没有样本，第二入参为 None。
返回值必须是 JSON 字典与列表，不能用元组。
"""
from __future__ import annotations

import json
from xml.etree import ElementTree


def uiautomator_to_boxes(companion: bytes, image: dict) -> list[dict] | None:
    """把 uiautomator dump 转成参考框：只保留有文字或可点击的节点，裁到画布内，剔除同框嵌套。

    @param companion XML 原始字节，编码由 XML 声明决定
    @param image {"width": W, "height": H}，原图存储像素尺寸
    @return 参考框数组；不是 uiautomator 层级或 bounds 无法解析时返回 None
    """
    try:
        root = ElementTree.fromstring(companion)
    except (ElementTree.ParseError, LookupError, ValueError):      # 后两者是 XML 声明了未知或不支持的编码
        return None
    if root.tag != "hierarchy":
        return None
    entries = []
    index = -1
    pending = [(root, {})]
    while pending:
        node, enclosing = pending.pop()
        if node.tag == "node":
            index += 1
            rectangle = _parse_bounds(node.get("bounds", ""))
            if rectangle is None:
                return None
            text = node.get("text") or node.get("content-desc") or ""
            bbox = _clip(rectangle, image)
            if bbox is not None and (text or node.get("clickable") == "true"):
                role = node.get("class", "").rsplit(".", 1)[-1]
                box = {"id": f"n{index}", "bbox": bbox, "text": f"{role} {text}".strip()}
                enclosing = _add_box(entries, box, bool(text), enclosing)
        pending.extend((child, enclosing) for child in reversed(node))
    return [entry["box"] for entry in entries if not entry["dropped"]]


def accessibility_to_boxes(companion: bytes, image: dict) -> list[dict] | None:
    """把无障碍树 JSON 转成参考框：深度优先遍历，只保留有名称的节点，剔除同框嵌套。

    @param companion UTF-8 JSON 原始字节，形如 {"root": {"role", "name", "rect", "children"}}
    @param image {"width": W, "height": H}，原图存储像素尺寸
    @return 参考框数组，框名按留下的框连续编号；不是这种结构时返回 None
    """
    try:
        pending = [(json.loads(companion)["root"], {})]
        entries = []
        while pending:
            node, enclosing = pending.pop()
            rect = node["rect"]
            rectangle = [rect["x"], rect["y"], rect["x"] + rect["width"], rect["y"] + rect["height"]]
            bbox = _clip(rectangle, image)
            if node["name"] and bbox is not None:
                enclosing = _add_box(entries, {"bbox": bbox, "text": f"{node['role']} {node['name']}"}, True, enclosing)
            pending.extend((child, enclosing) for child in reversed(node["children"]))
    except (KeyError, TypeError, ValueError):
        return None
    kept = [entry["box"] for entry in entries if not entry["dropped"]]
    return [{"id": f"a{number}", **box} for number, box in enumerate(kept, start=1)]


def ocr_to_boxes(companion: bytes, image: dict) -> list[dict] | None:
    """把 OCR 行结果转成未命名的参考框，由框架按数组顺序补名 "1"…"N"。

    @param companion UTF-8 JSON 原始字节，形如 {"lines": [{"text", "bbox", "confidence"}]}
    @param image {"width": W, "height": H}，原图存储像素尺寸
    @return 参考框数组，坐标保留 OCR 给出的浮点值；不是这种结构时返回 None
    """
    try:
        lines = json.loads(companion)["lines"]
        boxes = []
        for line in lines:
            bbox = _clip(list(line["bbox"]), image)
            if bbox is not None and line["confidence"] >= 0.5:
                boxes.append({"bbox": bbox, "text": line["text"]})
    except (KeyError, TypeError, ValueError):
        return None
    return boxes


def keep_polygons(candidate: dict, context: dict | None) -> dict:
    """保留原多边形：把候选几何写进代码负责字段 geometry。

    @param candidate 通过模型 Schema 的 {"items": [{"region_id", "label"}]}
    @param context 样本上下文；启动探针路径为 None
    @return 完整标注；没有样本时原样返回候选
    """
    if context is None:
        return candidate
    geometry = {region["region_id"]: region["geometry"] for region in context["regions"]}
    return {"items": [{**item, "geometry": geometry[item["region_id"]]} for item in candidate["items"]]}


def to_bounding_boxes(candidate: dict, context: dict | None) -> dict:
    """转外接矩形：对各部件外环顶点取 min/max，写进代码负责字段 bbox。

    @param candidate 通过模型 Schema 的 {"items": [{"region_id", "label"}]}
    @param context 样本上下文；启动探针路径为 None
    @return 完整标注，bbox 为 [l, t, r, b] 列表；没有样本时原样返回候选
    """
    if context is None:
        return candidate
    geometry = {region["region_id"]: region["geometry"] for region in context["regions"]}
    return {"items": [{**item, "bbox": _bounding_box(geometry[item["region_id"]])} for item in candidate["items"]]}


def _bounding_box(geometry: dict) -> list:
    """对 Polygon 或 MultiPolygon 的外环顶点取外接矩形，孔洞不影响结果。

    @param geometry GeoJSON 形状的几何
    @return [min_x, min_y, max_x, max_y]
    """
    parts = [geometry["coordinates"]] if geometry["type"] == "Polygon" else geometry["coordinates"]
    exteriors = [part[0] for part in parts]
    xs = [x for ring in exteriors for x, _y in ring]
    ys = [y for ring in exteriors for _x, y in ring]
    return [min(xs), min(ys), max(xs), max(ys)]


def _add_box(entries: list[dict], box: dict, named: bool, enclosing: dict) -> dict:
    """收进一个框并按模块文档的规则剔除同框嵌套，同一条父子链上只留一个。

    外层被淘汰后仍留在 enclosing 里，它的其他子节点要继续和它比较，结果才与兄弟顺序无关。

    @param entries 已收的框记录，按遍历顺序追加；被后来的内层淘汰时标记 dropped
    @param box 本节点的参考框
    @param named 本节点自身是否有文字或名称（钩子加的角色前缀不算）
    @param enclosing 祖先中同框比较的外层记录，键为 bbox；沿路径传递，这里不原地修改
    @return 本节点的子节点使用的外层表
    """
    key = tuple(box["bbox"])
    outer = enclosing.get(key)
    if outer is not None and outer["named"] and not named:
        return enclosing
    if outer is not None:
        outer["dropped"] = True
    entry = {"box": box, "named": named, "dropped": False}
    entries.append(entry)
    return {**enclosing, key: entry}


def _parse_bounds(bounds: str) -> list[int] | None:
    """解析 uiautomator 的 "[l,t][r,b]"。

    @param bounds 原始属性值
    @return [l, t, r, b]；形态不对时为 None
    """
    values = bounds.replace("][", ",").strip("[]").split(",")
    if len(values) != 4 or not all(value.strip().removeprefix("-").isdecimal() for value in values):
        return None
    try:
        return [int(value) for value in values]
    except ValueError:      # 位数超过解释器的整数转换上限
        return None


def _clip(rectangle: list, image: dict) -> list | None:
    """把矩形裁到画布内；这是本工程的策略，框架对越界的框按钩子错误处理。

    @param rectangle [l, t, r, b]
    @param image {"width": W, "height": H}
    @return 裁剪后的矩形；不剩面积时为 None
    """
    left, top, right, bottom = rectangle
    clipped = [max(left, 0), max(top, 0), min(right, image["width"]), min(bottom, image["height"])]
    return clipped if clipped[0] < clipped[2] and clipped[1] < clipped[3] else None
