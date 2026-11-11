# -*- coding: utf-8 -*-
# Copyright (C) 2026 Yusuf Eminoğlu
# SPDX-License-Identifier: GPL-2.0-or-later
"""Bounds-checked binary reading primitives for NCZ Engine v2.

Every read is clamped to the underlying buffer and returns a neutral
default instead of raising, so decoders never need per-field guards.
"""

from __future__ import annotations

import math
import struct

# Netcad stores text as single-byte Windows-1254 (Turkish). Biruni GIS change
# (2026-10-29): decode the whole code page, so İ Ş Ğ ğ ı ş Ç ç Ö ö Ü ü keep their
# Turkish form instead of being folded to ASCII as upstream does.

_F32 = struct.Struct("<f")
_F64 = struct.Struct("<d")

COORDINATE_LIMIT = 100000000.0


def decode_oem_text(raw: bytes) -> str:
    """Decode NUL-padded Windows-1254 bytes to text."""
    return raw.decode("cp1254", errors="replace").rstrip("\0")


def finite_pair_in_range(x: float, y: float) -> bool:
    """Return True when both values are finite and within the coordinate limit."""
    return (
        math.isfinite(x)
        and math.isfinite(y)
        and abs(x) <= COORDINATE_LIMIT
        and abs(y) <= COORDINATE_LIMIT
    )


class Cursor:
    """Random-access reader over an immutable byte buffer.

    All accessors take absolute offsets and never read past the buffer;
    out-of-range reads yield 0 / 0.0 / empty string.
    """

    __slots__ = ("data", "size")

    def __init__(self, data: bytes) -> None:
        """Wrap *data* for random-access reading."""
        self.data = data
        self.size = len(data)

    def u8(self, offset: int) -> int:
        """Return the unsigned byte at *offset*, or 0 when out of range."""
        if 0 <= offset < self.size:
            return self.data[offset]
        return 0

    def u16(self, offset: int) -> int:
        """Return the little-endian uint16 at *offset*, or 0 when out of range."""
        if 0 <= offset and offset + 2 <= self.size:
            return int.from_bytes(self.data[offset : offset + 2], "little")
        return 0

    def u32(self, offset: int) -> int:
        """Return the little-endian uint32 at *offset*, or 0 when out of range."""
        if 0 <= offset and offset + 4 <= self.size:
            return int.from_bytes(self.data[offset : offset + 4], "little")
        return 0

    def f32(self, offset: int) -> float:
        """Return the little-endian float32 at *offset*, or 0.0 when out of range."""
        if 0 <= offset and offset + 4 <= self.size:
            return _F32.unpack_from(self.data, offset)[0]
        return 0.0

    def f64(self, offset: int) -> float:
        """Return the little-endian float64 at *offset*, or 0.0 when out of range."""
        if 0 <= offset and offset + 8 <= self.size:
            return _F64.unpack_from(self.data, offset)[0]
        return 0.0

    def raw(self, offset: int, length: int) -> bytes:
        """Return *length* bytes at *offset*, clamped to the buffer."""
        if length <= 0 or offset >= self.size or offset < 0:
            return b""
        return self.data[offset : min(offset + length, self.size)]

    def text(self, offset: int, length: int) -> str:
        """Return decoded OEM text of *length* bytes at *offset*."""
        return decode_oem_text(self.raw(offset, length))

    def length_prefixed_text(
        self, length_offset: int, text_offset: int, max_length: int = 240
    ) -> str:
        """Text whose 1-byte length lives at *length_offset*."""
        if not (0 <= length_offset < self.size and 0 <= text_offset < self.size):
            return ""
        text_length = self.data[length_offset]
        if text_length <= 0 or text_length > max_length or text_offset + text_length > self.size:
            return ""
        return self.text(text_offset, text_length).strip("\0 ")

    def positive_f32(self, offset: int, upper: float = 100000.0) -> float | None:
        """A finite strictly-positive f32 within (0, upper], else None."""
        if offset < 0 or offset + 4 > self.size:
            return None
        value = self.f32(offset)
        if not math.isfinite(value) or value <= 0.0 or value > upper:
            return None
        return float(value)

    def find(self, needle: bytes, start: int = 0) -> int:
        """Return the index of *needle* at or after *start*, or -1."""
        return self.data.find(needle, start)
