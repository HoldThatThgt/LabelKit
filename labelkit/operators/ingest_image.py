"""M2 图片模式的发现与读取（SPEC-image-region-segmentation §8）；``Ingestor`` 只做分派。

一张图片就是一个样本：图片必需，伴随文件只在注册了 ``segment.box_parser`` 时按 index 配对，并按
原始字节读入 ``Record.companion``，这里从不解码、也不判断它是什么格式。图片模式不构造 ``UITree``。
"""
from __future__ import annotations

import hashlib
import logging
import warnings
from pathlib import Path
from typing import Iterator

from labelkit.common.contracts.types import ImageRef, Record, RecordRef
from labelkit.common.errors import InputError
from labelkit.operators.ingest import _IMAGE_RE, _LOG_EXTRA, IngestPlan, Ingestor, IngestReport, _UIScan

_LOGGER = logging.getLogger("labelkit.ingest")


def scan_image_plan(ingestor: Ingestor, root: Path) -> IngestPlan:
    """图片模式扫描分支：配对表与两类配对异常的 fail 策略快速失败。

    @param ingestor 持有配置与账本的摄取器
    @param root run.input 根目录
    @return files 按「伴随文件（若有）后图片」排列、``session_lens`` 恒空的扫描计划
    @raises InputError 目录不合法，或配对异常命中 "fail" 策略
    """
    scan = _scan_images(ingestor, root)
    ingestor._scan_fail_fast(scan)
    files: list[str] = []
    for _, companion, image in scan.pairs:
        if companion is not None:
            files.append(companion)
        files.append(image)
    return IngestPlan(files=tuple(files), pairs=scan.pairs, estimated_records=len(scan.pairs))


def image_records(ingestor: Ingestor, root: Path) -> Iterator[Record]:
    """图片模式记录流：先按 index 升序报告两类异常，再逐样本读取产出记录。

    ``scanned`` 与 UI 模态一样在处理到每个 index 时才累加，部分消费的流仍满足报告恒等式。

    @param ingestor 持有配置与账本的摄取器
    @param root run.input 根目录
    @return Record 迭代器
    @raises InputError 配对异常或坏样本命中对应 "fail" 策略
    """
    icfg = ingestor._cfg.input
    scan = _scan_images(ingestor, root)
    ingestor._report_ui_anomalies(scan)
    max_bytes = icfg.max_image_mb * 1024 * 1024
    for pair in scan.pairs:
        ingestor.report.scanned += 1
        record, reason, bad_file = _load_sample(root, pair, max_bytes)
        if reason is not None:
            _LOGGER.error("image mode: bad sample: index=%s file=%s reason=%s",
                          pair[0], bad_file, reason, extra=_LOG_EXTRA)
            ingestor._bad(file=bad_file, line_no=None, index=pair[0], reason=reason)
            ingestor._emit("ingest.bad_line", {"file": bad_file, "line_no": None, "reason": reason})
            if icfg.on_bad_line == "fail":
                raise InputError(f"{bad_file}: {reason} (input.on_bad_line = \"fail\")")
            continue
        ingestor.report.ingested += 1
        yield record


def _scan_images(ingestor: Ingestor, root: Path) -> _UIScan:
    """递归扫描输入目录，按全局 index 归集图片与已声明的伴随文件。

    @param ingestor 持有配置与账本的摄取器
    @param root run.input 根目录
    @return 配对条目形如 ``(index, 伴随文件 | None, 图片)`` 的全量扫描结果
    @raises InputError run.input 不是目录，或目录下没有图片也没有伴随文件
    """
    if not root.is_dir():
        raise InputError(f"UI modality requires run.input to be a directory: {root}")
    segment = ingestor._cfg.segment
    suffix = segment.companion_suffix if segment.box_parser is not None else None
    images: dict[int, list[str]] = {}
    companions: dict[int, list[str]] = {}
    ignored = 0
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(root).as_posix()
        match = _IMAGE_RE.match(path.name)
        if match:
            images.setdefault(int(match.group(1), 10), []).append(rel)
            continue
        companion_index = _companion_index(path.name, suffix) if suffix else None
        if companion_index is not None:
            companions.setdefault(companion_index, []).append(rel)
            continue
        ignored += 1
    _note_ignored(ingestor.report, ignored)
    if not images and not companions:
        raise InputError(f"no image_<index>.(png|jpg|jpeg) files under the image mode directory: {root}")
    # D15：没写 instruction 时伴随文件是候选的唯一来源，缺它就是缺对。
    companion_required = bool(suffix) and not segment.instruction.strip()
    return _pair_by_index(images, companions, companion_required)


def _companion_index(name: str, suffix: str) -> int | None:
    """按文件名取伴随文件的 index：``image_<n>`` 加 ``companion_suffix``，大小写严格。

    @param name 文件名（不含目录）
    @param suffix 工程声明的伴随文件后缀
    @return 十进制 index；文件名不符合时为 None
    """
    if not (name.startswith("image_") and name.endswith(suffix)):
        return None
    digits = name[len("image_"):len(name) - len(suffix)]
    return int(digits, 10) if digits.isdecimal() else None


def _pair_by_index(images: dict[int, list[str]], companions: dict[int, list[str]],
                   companion_required: bool) -> _UIScan:
    """按 index 组装配对表与两类异常。

    @param images index → 图片相对路径
    @param companions index → 伴随文件相对路径
    @param companion_required 只有图片的 index 是否算缺对（D15）
    @return 全量扫描结果，三张表都按 index 升序
    """
    pairs: list[tuple[int, str | None, str]] = []
    conflicts: list[tuple[int, tuple[str, ...]]] = []
    missing: list[tuple[int, str, str]] = []
    for index in sorted(set(images) | set(companions)):
        image_files = images.get(index, [])
        companion_files = companions.get(index, [])
        if len(image_files) >= 2 or len(companion_files) >= 2:
            conflicts.append((index, tuple(companion_files + image_files)))
        elif not image_files:
            missing.append((index, "companion", companion_files[0]))
        elif companion_files:
            pairs.append((index, companion_files[0], image_files[0]))
        elif companion_required:
            missing.append((index, "image", image_files[0]))
        else:
            pairs.append((index, None, image_files[0]))
    return _UIScan(pairs=tuple(pairs), conflicts=tuple(conflicts), missing=tuple(missing))


def _note_ignored(report: IngestReport, count: int) -> None:
    """记录被忽略的文件数；预扫描与记录流看到同一个数时只告警一次。

    @param report 摄取账本（该计数不参与 scanned、bad_input 与守恒恒等式）
    @param count 既不是图片也不是已声明伴随文件的文件数
    """
    if count == report.ignored_files or count == 0:
        return
    report.ignored_files = count
    _LOGGER.warning(
        "image mode: ignored %d file(s) that are neither images nor declared companion files",
        report.ignored_files, extra=_LOG_EXTRA)


def _check_header(path: Path) -> str | None:
    """文件头检查：Pillow 惰性打开，只读文件头，不解码像素，也不看方向（D4）。

    @param path 图片路径
    @return 坏图原因；合格时为 None
    """
    from PIL import Image  # 局部导入：与 ImageRef.load_base64 同一惯例

    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(path):
                return None
    except (Image.DecompressionBombError, Image.DecompressionBombWarning):
        return "image pixel count exceeds the Pillow MAX_IMAGE_PIXELS limit"
    except Exception as exc:  # Pillow 对残缺文件头抛的类型不止 OSError，都是这一个样本的问题
        _LOGGER.debug("image header unreadable: %s", type(exc).__name__, extra=_LOG_EXTRA)
        return "image header is not a recognized image format"


def _load_sample(root: Path, pair: tuple[int, str | None, str],
                 max_bytes: int) -> tuple[Record | None, str | None, str]:
    """加载一个样本：图片检查、伴随文件原始字节、内容摘要与记录 id。

    @param root run.input 根目录
    @param pair 配对表条目 ``(index, 伴随文件 | None, 图片)``
    @param max_bytes 图片字节数上限（由 input.max_image_mb 换算）
    @return (Record, 坏样本原因, 归因文件)；坏样本时 Record 为 None
    """
    index, companion_rel, image_rel = pair
    image_path = root / image_rel
    reason = Ingestor._check_image(image_path, max_bytes) or _check_header(image_path)
    if reason is not None:
        return None, reason, image_rel
    companion: bytes | None = None
    if companion_rel is not None:
        try:
            companion = (root / companion_rel).read_bytes()
        except OSError as exc:
            _LOGGER.debug("cannot read companion file: %s", exc, extra=_LOG_EXTRA)
            return None, f"cannot read companion file: {exc}", companion_rel
    try:
        image_bytes = image_path.read_bytes()
    except OSError as exc:
        _LOGGER.debug("cannot read image file: %s", exc, extra=_LOG_EXTRA)
        return None, f"cannot read image file: {exc}", image_rel
    suffix = image_path.suffix.lower().lstrip(".")
    record = Record(
        id=hashlib.sha256((companion or b"") + image_bytes).hexdigest()[:16],
        modality="ui",
        text=None,
        raw=None,
        ui_tree=None,
        image=ImageRef(path=image_path, format="png" if suffix == "png" else "jpeg",
                       size_bytes=len(image_bytes)),
        ref=RecordRef(source_file=image_rel, line_no=None, pair_index=index, generated_from=()),
        companion=companion,
        companion_path=None if companion_rel is None else str(root / companion_rel),
        image_sha256=hashlib.sha256(image_bytes).hexdigest(),
        companion_sha256=None if companion is None else hashlib.sha256(companion).hexdigest(),
    )
    return record, None, image_rel
