"""Minimal image dimension extraction (JPEG/PNG) without Pillow."""
from __future__ import annotations

import struct

_SOF_MARKERS = {
    0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7,
    0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF,
}


def image_size(data: bytes) -> tuple[int, int]:
    """Return (width, height) in pixels for JPEG or PNG bytes."""
    if data[:3] == b"\xff\xd8\xff":
        return _jpeg_size(data)
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        w = struct.unpack(">I", data[16:20])[0]
        h = struct.unpack(">I", data[20:24])[0]
        return w, h
    raise ValueError("unsupported image format (expected JPEG or PNG)")


def _jpeg_size(data: bytes) -> tuple[int, int]:
    i = 2
    n = len(data)
    while i + 9 < n:
        if data[i] != 0xFF:
            i += 1
            continue
        marker = data[i + 1]
        if marker in _SOF_MARKERS:
            h = struct.unpack(">H", data[i + 5 : i + 7])[0]
            w = struct.unpack(">H", data[i + 7 : i + 9])[0]
            return w, h
        seg_len = struct.unpack(">H", data[i + 2 : i + 4])[0]
        i += 2 + seg_len
    raise ValueError("JPEG has no SOF marker")
