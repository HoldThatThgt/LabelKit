"""AC-27 的真实损坏图片夹具：每种损坏都是写到磁盘的文件，文件头可读，不伪造异常。"""
from __future__ import annotations

import io
import random
import struct
import zlib
from pathlib import Path

from PIL import Image

from labelkit.common.contracts.types import ImageRef

NOISE_SIZE = (200, 200)                       # 噪声压不动，Pillow 写 PNG 时至少分成两个 IDAT
UNDER_REQUEST_PX = 4000                       # 长边不超限：load_base64 走不缩放分支
RESIZED_PX = 64                               # 长边超限：load_base64 走缩放分支

# 损坏名 → Pillow 解码时原本抛的异常类型（load() 之后的未转换结果）。
PIXEL_TRUNCATIONS = {"png_cut_half": OSError, "jpeg_cut_half": OSError}
DECODER_FAILURES = {
    "chunk_header_cut_4": SyntaxError,
    "chunk_header_cut_5": SyntaxError,
    "chunk_header_cut_6": SyntaxError,
    "chunk_header_cut_7": SyntaxError,
    "garbled_chunk_id": SyntaxError,
    "zero_tail": SyntaxError,
    "oversized_ztxt": ValueError,
}
ALL_DAMAGES = {**PIXEL_TRUNCATIONS, **DECODER_FAILURES}


def _noise_bytes(fmt: str) -> bytes:
    width, height = NOISE_SIZE
    image = Image.frombytes("RGB", NOISE_SIZE, random.Random(0).randbytes(width * height * 3))
    buf = io.BytesIO()
    image.save(buf, format=fmt)
    return buf.getvalue()


def _chunk_offset(raw: bytes, cid: bytes, nth: int = 0) -> int:
    """定位 PNG 第 nth 个 cid 数据块的起始偏移（长度字段处）。"""
    position, seen = 8, 0
    while position < len(raw):
        length = struct.unpack(">I", raw[position:position + 4])[0]
        if raw[position + 4:position + 8] == cid:
            if seen == nth:
                return position
            seen += 1
        position += 12 + length
    raise AssertionError(f"PNG has no chunk {cid!r} #{nth}")


def _png_chunk(cid: bytes, body: bytes) -> bytes:
    return struct.pack(">I", len(body)) + cid + body + struct.pack(">I", zlib.crc32(cid + body))


def _damaged_bytes(damage: str) -> bytes:
    if damage == "jpeg_cut_half":
        raw = _noise_bytes("JPEG")
        return raw[:len(raw) // 2]
    raw = _noise_bytes("PNG")
    second_idat = _chunk_offset(raw, b"IDAT", 1)
    if damage == "png_cut_half":
        return raw[:len(raw) // 2]
    if damage.startswith("chunk_header_cut_"):
        return raw[:second_idat + int(damage.rsplit("_", 1)[1])]
    if damage == "garbled_chunk_id":
        return raw[:second_idat + 4] + b"\0\0\0\0" + raw[second_idat + 8:]
    if damage == "zero_tail":
        return raw[:second_idat] + b"\0" * (len(raw) - second_idat)
    if damage == "oversized_ztxt":                # 解压后超过 Pillow 的 MAX_TEXT_CHUNK，位于 IDAT 之后
        iend = _chunk_offset(raw, b"IEND")
        return raw[:iend] + _png_chunk(b"zTXt", b"k\0\0" + zlib.compress(b"a" * (2 * 1024 * 1024))) + raw[iend:]
    raise AssertionError(f"unknown damage {damage!r}")


def write_damaged_image(directory: Path, damage: str) -> ImageRef:
    """把一种损坏写成磁盘文件并返回它的引用。

    @param directory 目标目录
    @param damage ALL_DAMAGES 的键
    @return 指向损坏文件的 ImageRef
    """
    fmt = "jpeg" if damage.startswith("jpeg") else "png"
    path = directory / f"damaged_{damage}.{'jpg' if fmt == 'jpeg' else 'png'}"
    path.write_bytes(_damaged_bytes(damage))
    return ImageRef(path=path, format=fmt, size_bytes=path.stat().st_size)
