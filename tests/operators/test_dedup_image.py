"""M3 图片模式判重（SPEC-image-region-segmentation §15.1）：精确键、参考框文本与退化规则。

C6 的复现在 ``test_c6_*``：没有内容摘要时，无树样本的判重文本全是空串，N 张图片会被并成 1 张。
"""
from __future__ import annotations

import hashlib
import json
import math
import random
from dataclasses import replace
from pathlib import Path

import imagehash
import numpy as np
import pytest
from PIL import Image

from labelkit.common.config.model import DedupConfig
from labelkit.common.contracts.regions import ReferenceBox, RegionSet
from labelkit.common.contracts.stage import RunContext
from labelkit.common.contracts.types import ImageRef, PipelineItem, Record, RecordRef
from labelkit.operators.dedup import DedupIndex, DedupStage, _box_dedup_text, _build_minhash, _phash_int
from labelkit.operators.ingest import Ingestor

from tests.operators.test_ingest_image import PIXELS_ONLY, make_cfg


class Metrics:
    """记录 trace 事件与计数。"""

    def __init__(self):
        self.events: list[tuple[str, dict]] = []
        self.counters: dict[str, int] = {}

    def event(self, ev, *, stage, batch_no, record_ids=(), payload=None):
        self.events.append((ev, dict(payload or {})))

    def count(self, key, n=1):
        self.counters[key] = self.counters.get(key, 0) + n


class NoEmbedding:
    """任何 embedding 调用都是缺陷。"""

    async def embed(self, *_args, **_kwargs):
        raise AssertionError("image mode must never call embedding")


class NoTasks:
    """语义层不参与时不提交任务组。"""

    async def run_group(self, _request):
        raise AssertionError("image mode must not submit embedding tasks")


def make_ctx() -> RunContext:
    return RunContext(cfg=None, llm=NoEmbedding(), schema_engine=None, metrics=Metrics(),
                      rng=random.Random(0), batch_no=1, tasks=NoTasks(), task_namespace="test:dedup")


def noise(path: Path, seed: int, *, nudge: int = 0) -> None:
    """低频噪声图：pHash 有意义；``nudge`` 只改一个像素，字节不同而 pHash 相近。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    rng = random.Random(seed)
    small = Image.new("L", (16, 16))
    small.putdata([rng.randrange(256) for _ in range(256)])
    image = small.resize((128, 128), Image.Resampling.BICUBIC)
    if nudge:
        image.putpixel((0, 0), (image.getpixel((0, 0)) + nudge) % 256)
    image.save(path, format="PNG")


def sample(tmp_path: Path, name: str, seed: int, *, nudge: int = 0, companion: bytes | None = None) -> Record:
    """构造 M2 产出形态的图片模式记录（id 取文件名，便于断言 kept_id）。"""
    path = tmp_path / f"{name}.png"
    noise(path, seed, nudge=nudge)
    return image_record(path, name, companion)


def image_record(path: Path, name: str, companion: bytes | None = None) -> Record:
    """把已落盘的图片包成 M2 产出形态的记录。"""
    data = path.read_bytes()
    return Record(id=name, modality="ui", text=None, raw=None, ui_tree=None,
                  image=ImageRef(path=path, format="png", size_bytes=len(data)),
                  ref=RecordRef(f"{name}.png", None, 1, ()),
                  companion=companion,
                  image_sha256=hashlib.sha256(data).hexdigest(),
                  companion_sha256=None if companion is None else hashlib.sha256(companion).hexdigest())


def boxes(*texts: str) -> RegionSet:
    """每段文本一个框，坐标固定，模拟同一界面模板。"""
    return RegionSet(width=128, height=128, regions=(),
                     boxes=tuple(ReferenceBox(f"n{i}", (0, 10 * i, 100, 10 * i + 8), text)
                                 for i, text in enumerate(texts)))


async def run(items: list[PipelineItem], cfg: DedupConfig | None = None) -> RunContext:
    cfg = cfg or DedupConfig()
    ctx = make_ctx()
    await DedupStage(cfg, DedupIndex(cfg, "ui")).run(items, ctx)
    return ctx


LOGIN = ("欢迎回来，请使用手机号和验证码登录账号，首次登录会自动创建新账号，"
         "继续即表示同意用户协议与隐私政策，如遇问题请联系在线客服获取帮助")
SEARCH = ("今日热门搜索榜单：夏季防晒霜选购指南、周末短途旅行攻略、家常红烧肉做法，"
          "点击条目查看详情并收藏到个人清单，下拉刷新可以换一批推荐内容")


# ── C6 复现 ─────────────────────────────────────────────────────────────────


async def test_c6_tree_less_images_collapse_without_digests_and_survive_with_them(tmp_path):
    """默认 dedup 下 N 张无树图片：缺摘要时只剩 1 张（缺陷形态），M2 写摘要后全部保留。"""
    root = tmp_path / "in"
    for index in range(1, 5):
        noise(root / f"image_{index}.png", seed=index * 11)
    records = list(Ingestor(make_cfg(tmp_path, PIXELS_ONLY)).records())
    assert len(records) == 4

    stripped = [PipelineItem(record=replace(record, image_sha256=None)) for record in records]
    await run(stripped)
    assert [item.status for item in stripped] == ["active"] + ["dropped_dup"] * 3
    assert {item.dedup.kind for item in stripped[1:]} == {"exact"}

    items = [PipelineItem(record=record, regions=RegionSet(128, 128, (), ())) for record in records]
    await run(items)
    assert [item.status for item in items] == ["active"] * 4
    assert {item.dedup.kind for item in items} == {"unique"}
    assert len({item.dedup.cluster_key for item in items}) == 4


# ── AC-02 ───────────────────────────────────────────────────────────────────


async def test_same_companion_different_image_not_exact_duplicate(tmp_path):
    items = [PipelineItem(record=sample(tmp_path, "a", 1, companion=b"<shell/>"), regions=boxes("外壳")),
             PipelineItem(record=sample(tmp_path, "b", 2, companion=b"<shell/>"), regions=boxes("外壳"))]
    await run(items)
    assert [item.dedup.kind for item in items] == ["unique", "unique"]
    assert [item.status for item in items] == ["active", "active"]


async def test_same_template_different_box_text_not_near_duplicate_under_both(tmp_path):
    login = PipelineItem(record=sample(tmp_path, "login", 7, companion=b"login"), regions=boxes(LOGIN))
    search = PipelineItem(record=sample(tmp_path, "search", 7, nudge=1, companion=b"search"),
                          regions=boxes(SEARCH))
    again = PipelineItem(record=sample(tmp_path, "again", 7, nudge=2, companion=b"again"), regions=boxes(LOGIN))
    await run([login, search, again])
    assert login.status == "active"
    # 图片几乎相同、框文字不同：默认 "both" 不误杀。
    assert search.status == "active" and search.dedup.kind == "unique"
    # 正对照：框文字也相同，才判近重复，并保留两层都命中的证据。
    assert again.status == "dropped_dup"
    assert again.dedup.kind == "near_both" and again.dedup.kept_id == "login"


async def test_no_boxes_similar_images_near_duplicate_by_phash(tmp_path):
    first = PipelineItem(record=sample(tmp_path, "a", 3), regions=RegionSet(128, 128, (), ()))
    second = PipelineItem(record=sample(tmp_path, "b", 3, nudge=1), regions=RegionSet(128, 128, (), ()))
    other = PipelineItem(record=sample(tmp_path, "c", 4), regions=RegionSet(128, 128, (), ()))
    ctx = await run([first, second, other])
    assert first.status == "active" and other.status == "active"
    assert second.status == "dropped_dup"
    assert second.dedup.kind == "near_image" and second.dedup.kept_id == "a"
    event = [payload for ev, payload in ctx.metrics.events if ev == "dedup.duplicate"][0]
    assert isinstance(event["hamming"], int)


@pytest.mark.parametrize("semantic", [False, True])
async def test_image_mode_never_calls_embedding(tmp_path, semantic):
    """M1 的 rule 78a 已拒绝 semantic；即便绕过 M1 直接构造，语义层也不参与。"""
    items = [PipelineItem(record=sample(tmp_path, "a", 1), regions=boxes(LOGIN)),
             PipelineItem(record=sample(tmp_path, "b", 2), regions=None)]
    cfg = DedupConfig(semantic=semantic, semantic_embedding="embed")
    ctx = await run(items, cfg)
    assert [item.dedup.kind for item in items] == ["unique", "unique"]
    assert "dedup.embedding_failures" not in ctx.metrics.counters


# ── AC-137 ──────────────────────────────────────────────────────────────────


async def test_exact_key_is_domain_separated_and_keeps_phash_layer(tmp_path):
    record = sample(tmp_path, "a", 5, companion=b"<c/>")
    index = DedupIndex(DedupConfig(), "ui")
    detail = index.prepare(record)
    material = json.dumps(["labelkit:image", "exact", record.image_sha256, record.companion_sha256],
                          ensure_ascii=False, separators=(",", ":"))
    assert detail.digest == hashlib.sha256(material.encode("utf-8")).digest()
    assert detail.own_key == detail.digest.hex()[:16]
    assert record.exact_dedup_text is None
    assert detail.exact_only is False
    assert detail.phash is not None

    without_companion = index.prepare(replace(record, companion=None, companion_sha256=None))
    expected = json.dumps(["labelkit:image", "exact", record.image_sha256, None],
                          ensure_ascii=False, separators=(",", ":"))
    assert without_companion.digest == hashlib.sha256(expected.encode("utf-8")).digest()
    assert without_companion.digest != detail.digest

    # 判重文本恰为同一串的普通 UI 记录也不会撞进图片模式的精确键。
    forged = replace(record, image_sha256=None, companion_sha256=None)
    assert index.prepare(forged).digest == hashlib.sha256(b"").digest()

    first = PipelineItem(record=record, regions=boxes(LOGIN))
    second = PipelineItem(record=replace(record, id="copy"), regions=boxes(SEARCH))
    await run([first, second])
    assert second.status == "dropped_dup"
    assert second.dedup.kind == "exact" and second.dedup.kept_id == "a"


def test_box_text_quantization_and_escaping():
    regions = RegionSet(width=2000, height=2000, regions=(), boxes=(
        ReferenceBox("n1", (43, 140, 821, 1264), 'Button "登录"\n[0,0,1,1] "伪造"'),
        ReferenceBox("zzz", (43.9, 7.5, 99.0, 8), ""),
    ))
    assert _box_dedup_text(regions, 4) == (
        '[10,35,205,316] "Button \\"登录\\"\\n[0,0,1,1] \\"伪造\\""\n'
        '[10,1,24,2] ""')
    assert _box_dedup_text(regions, 0) == (
        '[43,140,821,1264] "Button \\"登录\\"\\n[0,0,1,1] \\"伪造\\""\n'
        '[43,7,99,8] ""')
    assert "n1" not in _box_dedup_text(regions, 0) and "zzz" not in _box_dedup_text(regions, 0)
    # 文字转义后不能伪造出另一行。
    assert len(_box_dedup_text(regions, 4).split("\n")) == 2
    # 按钩子返回顺序拼接。
    swapped = replace(regions, boxes=tuple(reversed(regions.boxes)))
    assert _box_dedup_text(swapped, 4).split("\n")[0] == '[10,1,24,2] ""'
    assert _box_dedup_text(None, 4) == ""
    assert _box_dedup_text(RegionSet(1, 1, (), ()), 4) == ""


def test_box_quantization_uses_exact_rational_division():
    # 大坐标上浮点除法已经丢失整数精度；精确地板除不会，int 坐标也不经过浮点。
    value = 10 ** 17
    assert math.floor(float(value) / 3) != value // 3   # 这组取值确实能区分两种实现
    regions = RegionSet(width=2 ** 60, height=2 ** 60, regions=(),
                        boxes=(ReferenceBox("a", (float(value), value + 1, value + 2, value + 3), ""),))
    assert _box_dedup_text(regions, 3) == (
        f'[{value // 3},{(value + 1) // 3},{(value + 2) // 3},{(value + 3) // 3}] ""')


async def test_both_degrades_to_image_without_boxes(tmp_path):
    index = DedupIndex(DedupConfig(), "ui")
    kept = sample(tmp_path, "a", 9)
    assert index.probe_and_add(kept).kind == "unique"
    near = sample(tmp_path, "b", 9, nudge=1)
    # regions=None 与空 boxes 都是树侧缺席，退化只作用于本记录。
    assert index.probe_prepared(index.prepare(near)).kind == "near_image"
    empty = RegionSet(128, 128, (), ())
    assert index.probe_prepared(index.prepare_image_sample(near, empty)).kind == "near_image"
    # 有框的记录仍要两层都命中：kept 没有 MinHash，所以 "both" 不成立。
    assert index.probe_prepared(index.prepare_image_sample(near, boxes(LOGIN))).kind == "unique"


async def test_image_requires_setting_ignores_box_side(tmp_path):
    cfg = DedupConfig(ui_dup_requires="image")
    first = PipelineItem(record=sample(tmp_path, "a", 9), regions=boxes(LOGIN))
    second = PipelineItem(record=sample(tmp_path, "b", 9, nudge=1), regions=boxes(SEARCH))
    await run([first, second], cfg)
    assert second.dedup.kind == "near_image"


async def test_tree_only_exact_without_boxes(tmp_path):
    cfg = DedupConfig(ui_dup_requires="tree")
    first = PipelineItem(record=sample(tmp_path, "a", 9), regions=RegionSet(128, 128, (), ()))
    near = PipelineItem(record=sample(tmp_path, "b", 9, nudge=1), regions=RegionSet(128, 128, (), ()))
    copy = PipelineItem(record=replace(first.record, id="c"), regions=RegionSet(128, 128, (), ()))
    await run([first, near, copy], cfg)
    assert near.status == "active" and near.dedup.kind == "unique"
    assert copy.status == "dropped_dup" and copy.dedup.kind == "exact"


async def test_tree_setting_uses_box_text_when_present(tmp_path):
    cfg = DedupConfig(ui_dup_requires="tree")
    first = PipelineItem(record=sample(tmp_path, "a", 1), regions=boxes(LOGIN))
    second = PipelineItem(record=sample(tmp_path, "b", 2), regions=boxes(LOGIN))
    await run([first, second], cfg)
    assert second.dedup.kind == "near_text" and second.dedup.kept_id == "a"


async def test_decode_failure_without_boxes_can_only_be_exact(tmp_path):
    first = sample(tmp_path, "a", 1)
    broken = sample(tmp_path, "b", 2)
    broken.image.path.write_bytes(b"\x89PNG\r\n\x1a\ntruncated")
    items = [PipelineItem(record=first), PipelineItem(record=broken)]
    ctx = await run(items)
    assert items[1].status == "active" and items[1].dedup.kind == "unique"
    assert ctx.metrics.counters["dedup.image_decode_failures"] == 1


# ── 16 位灰度：imagehash 的 convert("L") 会把超过 255 的值全部削成白 ────────────


def low_frequency(seed: int | None) -> np.ndarray:
    """取值 0..1 的低频图样（``seed = None`` 为渐变），信号落在 pHash 的 8x8 低频块内。"""
    if seed is None:
        return np.linspace(0.0, 1.0, 128 * 128).reshape(128, 128)
    rng = np.random.default_rng(seed)
    small = Image.fromarray(rng.integers(0, 256, (16, 16)).astype("uint8"))
    return np.asarray(small.resize((128, 128), Image.Resampling.BICUBIC), dtype=np.float64) / 255


def gray16(path: Path, pattern: np.ndarray, low: int, high: int) -> Record:
    """把图样线性映射到 ``low..high`` 后存成 16 位灰度 PNG，并确认 Pillow 按 I;16 打开。"""
    Image.fromarray((low + pattern * (high - low)).astype("uint16")).save(path, format="PNG")
    with Image.open(path) as opened:
        assert opened.mode == "I;16"
        assert low <= opened.getextrema()[0] and opened.getextrema()[1] <= high
    return image_record(path, path.stem)


@pytest.mark.parametrize("regions", [None, RegionSet(128, 128, (), ())], ids=["no-regions", "empty-boxes"])
@pytest.mark.parametrize("low,high", [(1000, 60000), (256, 1000), (1, 255)], ids=["full", "above-8bit", "low-range"])
async def test_distinct_16bit_images_without_boxes_stay_unique(tmp_path, regions, low, high):
    """C6 的另一种形态：默认配置下 N 张不同的 16 位灰度图不能并成 1 张。"""
    patterns = {"gradient": None, "noise_a": 21, "noise_b": 22}
    items = [PipelineItem(record=gray16(tmp_path / f"{name}.png", low_frequency(seed), low, high), regions=regions)
             for name, seed in patterns.items()]
    ctx = await run(items)
    assert [item.status for item in items] == ["active"] * 3
    assert [item.dedup.kind for item in items] == ["unique"] * 3
    assert "dedup.image_decode_failures" not in ctx.metrics.counters


@pytest.mark.parametrize("delta", [1, 5000], ids=["nudge", "new-maximum"])
async def test_nudged_16bit_copy_still_folds_as_near_image(tmp_path, delta):
    """按本图最大值缩放后仍是近重复；单个像素抬高最大值只改变整体比例。"""
    first = gray16(tmp_path / "a.png", low_frequency(31), 1000, 60000)
    with Image.open(first.image.path) as opened:
        pixels = np.array(opened)
    pixels[0, 0] = pixels.max() + delta
    Image.fromarray(pixels).save(tmp_path / "b.png", format="PNG")
    second = image_record(tmp_path / "b.png", "b")
    items = [PipelineItem(record=first), PipelineItem(record=second)]
    ctx = await run(items)
    assert first.image_sha256 != second.image_sha256
    assert items[1].status == "dropped_dup"
    assert items[1].dedup.kind == "near_image" and items[1].dedup.kept_id == "a"
    assert "dedup.image_decode_failures" not in ctx.metrics.counters


@pytest.mark.parametrize("mode", ["I;16B", "I"])
def test_every_integer_mode_hashes_like_i16(tmp_path, mode):
    """旧版 Pillow 把 16 位 PNG 开成 I，大端 TIFF 开成 I;16B；两者与 I;16 走同一缩放，也不抛异常。"""
    reference = gray16(tmp_path / "ref.png", low_frequency(41), 1000, 60000)
    with Image.open(reference.image.path) as opened:
        pixels = np.array(opened)
    target = tmp_path / "other.tiff"
    if mode == "I":
        Image.fromarray(pixels.astype("int32")).save(target, format="TIFF")
    else:
        Image.frombytes(mode, (128, 128), pixels.astype(">u2").tobytes()).save(target, format="TIFF")
    with Image.open(target) as opened:
        assert opened.mode == mode
    assert _phash_int(target) == _phash_int(reference.image.path)


@pytest.mark.parametrize("mode", ["L", "RGB", "RGBA", "P"])
def test_non_integer_modes_hash_exactly_as_imagehash(tmp_path, mode):
    target = tmp_path / "plain.png"
    noise(target, 51)
    with Image.open(target) as opened:
        opened.convert(mode).save(target, format="PNG")
    with Image.open(target) as opened:
        assert opened.mode == mode
        expected = 0
        for bit in imagehash.phash(opened).hash.flatten():
            expected = (expected << 1) | int(bit)
    assert _phash_int(target) == expected


def test_low_range_integer_image_is_not_scaled(tmp_path):
    """最大值不超过 255 的 16 位图不缩放：哈希等于同一像素的 8 位图。"""
    record = gray16(tmp_path / "low.png", low_frequency(61), 1, 200)
    with Image.open(record.image.path) as opened:
        Image.fromarray(np.array(opened).astype("uint8")).save(tmp_path / "eight.png", format="PNG")
    assert _phash_int(record.image.path) == _phash_int(tmp_path / "eight.png")


def test_non_image_ui_record_keeps_empty_text_path():
    """普通 UI 分支：摘要字段为 None 时走旧配方，非图片字节不变。"""
    record = Record(id="u", modality="ui", text=None, raw=None, ui_tree=None, image=None,
                    ref=RecordRef("uitree_1.jsonl", None, 1, ()))
    detail = DedupIndex(DedupConfig(), "ui").prepare(record)
    assert detail.dedup_text == ""
    assert detail.digest == hashlib.sha256(b"").digest()
    assert detail.image_sample is False
    assert detail.minhash is None
    assert _build_minhash("", 3, 16) is None
