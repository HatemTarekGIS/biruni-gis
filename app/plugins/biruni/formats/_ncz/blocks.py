# -*- coding: utf-8 -*-
# Copyright (C) 2026 Yusuf Eminoğlu
# SPDX-License-Identifier: GPL-2.0-or-later
"""Block scanning and drawing-metadata decoding for NCZ Engine v2."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterator

from .binary import Cursor

BLOCK_LAYER_TABLE = 6
BLOCK_GEOMETRY_PLAIN = 21
BLOCK_GEOMETRY_GIS = 22
BLOCK_VERSION = 25
BLOCK_NAMED = 28
GEOMETRY_BLOCK_KINDS = (BLOCK_GEOMETRY_PLAIN, BLOCK_GEOMETRY_GIS)
CONTAINER_BLOCK_KINDS = frozenset({0, 5, 14, 48, 108, 111, 132, 150, 180})

GIS_LAYOUT_SHIFT = 28

PROJECTION_CODES = {1: "Geographic", 2: "6", 3: "3"}
DATUM_CODES = {0: "WGS-84", 1: "ITRF", 4: "ED50", 254: "ED50-HGK"}


@dataclass(frozen=True)
class RawBlock:
    """One container block: kind byte + declared extent."""

    kind: int
    offset: int
    size: int  # size in the container convention: total bytes - 1

    @property
    def end(self) -> int:
        """Absolute offset one past the block's last byte."""
        return self.offset + self.size + 1


def scan_blocks(cursor: Cursor) -> Iterator[RawBlock]:
    """Walk the top-level block sequence, resynchronizing on bad lengths."""
    position = 0
    limit = cursor.size
    while position + 5 < limit:
        size = cursor.u32(position + 1) + 4
        if size < 4 or position + size + 1 > limit:
            position += 1
            continue
        yield RawBlock(cursor.u8(position), position, size)
        position += size + 1


def scan_embedded_geometry(cursor: Cursor, container: RawBlock) -> Iterator[RawBlock]:
    """Find nested 21/22 geometry records inside a container block.

    A nested record is recognized by its kind byte followed by a valid
    length whose bytes at +5 and +6 are equal (length-prefix echo).
    """
    position = container.offset + 5
    end = container.end
    while position + 6 < end:
        if cursor.u8(position) not in GEOMETRY_BLOCK_KINDS or cursor.u8(position + 5) != cursor.u8(
            position + 6
        ):
            position += 1
            continue
        inner_size = cursor.u32(position + 1) + 4
        if inner_size < 7 or position + inner_size + 1 > end:
            position += 1
            continue
        yield RawBlock(cursor.u8(position), position, inner_size)
        position += inner_size + 1


@dataclass
class DrawingMetadata:
    """Layer table, colours and CRS hints accumulated during the scan."""

    layer_names: list[str] = field(default_factory=list)
    layer_colors: list[int] = field(default_factory=list)
    version_name: str = ""
    projection_text: str = ""
    epsg: str = ""

    def layer_name(self, layer_code: int) -> str:
        """Return the layer name for *layer_code*, or an empty string."""
        for candidate in (layer_code, layer_code - 1):
            if 0 <= candidate < len(self.layer_names):
                return self.layer_names[candidate]
        return ""

    def layer_color(self, layer_code: int) -> int | None:
        """Return the normalized ARGB colour for *layer_code*, or None."""
        for candidate in (layer_code, layer_code - 1):
            if 0 <= candidate < len(self.layer_colors):
                return _normalize_color(self.layer_colors[candidate])
        return None

    def resolve_color(self, layer_code: int, color_code: int) -> int | None:
        """Resolve an entity's ARGB colour from its colour code and layer."""
        if color_code == 1:
            return _argb(0, 0, 255)
        if color_code == 255:
            return _argb(255, 0, 0)
        if color_code != 0:
            return None
        return self.layer_color(layer_code)

    def to_dict(self) -> dict:
        """Return the accumulated metadata as a JSON-serializable dictionary."""
        return {
            "layer_names": list(self.layer_names),
            "layer_colors": list(self.layer_colors),
            "version_name": self.version_name,
            "projection_text": self.projection_text,
            "epsg": self.epsg,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "DrawingMetadata":
        """Rebuild metadata from its dictionary form."""
        return cls(
            layer_names=list(data.get("layer_names", [])),
            layer_colors=list(data.get("layer_colors", [])),
            version_name=data.get("version_name", ""),
            projection_text=data.get("projection_text", ""),
            epsg=data.get("epsg", ""),
        )


def _argb(red: int, green: int, blue: int) -> int:
    """Pack opaque red/green/blue bytes into an ARGB integer."""
    return (255 << 24) | (red << 16) | (green << 8) | blue


def _normalize_color(argb: int) -> int:
    """Producer quirk: near-black-blue layer colours mean pure black."""
    red = (argb >> 16) & 0xFF
    green = (argb >> 8) & 0xFF
    blue = argb & 0xFF
    if red == 0 and green == 0 and blue <= 1:
        return _argb(0, 0, 0)
    return argb


def apply_metadata_block(cursor: Cursor, block: RawBlock, metadata: DrawingMetadata) -> None:
    """Decode a non-geometry block into *metadata* (no-op otherwise)."""
    base = block.offset
    if block.kind == BLOCK_VERSION:
        if not metadata.version_name:
            metadata.version_name = cursor.text(base + 6, cursor.u8(base + 5))
        return

    if block.kind == BLOCK_LAYER_TABLE:
        if base + 17 >= cursor.size:
            return
        count = cursor.u16(base + 16)
        for index in range(count):
            item = base + 18 + index * 29
            if item + 29 > cursor.size:
                break
            name = cursor.text(item + 5, cursor.u8(item + 4))
            if name.strip():
                metadata.layer_names.append(name)
        return

    if block.kind != BLOCK_NAMED:
        return

    name = cursor.text(base + 6, cursor.u8(base + 5))
    if name == "MPROJ":
        if base + 21 < cursor.size:
            projection = PROJECTION_CODES.get(cursor.u8(base + 16), "Undefined")
            datum = DATUM_CODES.get(cursor.u8(base + 17), "Undefined")
            metadata.projection_text = f"{datum} / {projection} / Zone {cursor.u8(base + 21)}"
    elif name == "TILED_XML":
        metadata.epsg = _read_epsg(cursor, base, min(block.size + 1, cursor.size - base))
    elif name == "LEX.ST2":
        if base + 20 < cursor.size:
            count = cursor.u8(base + 20)
            for index in range(count):
                item = base + 23 + index * 256 + 56
                if item + 2 >= cursor.size:
                    break
                metadata.layer_colors.append(
                    _argb(cursor.u8(item), cursor.u8(item + 1), cursor.u8(item + 2))
                )


def _read_epsg(cursor: Cursor, base: int, max_length: int) -> str:
    """Extract the ``SRS:"EPSG:xxxx"`` value from a TILED_XML block."""
    for index in range(max(0, max_length - 3)):
        start = base + index
        if start + 2 >= cursor.size:
            break
        if cursor.raw(start, 3) != b"SRS":
            continue
        chars = []
        position = start
        # Stay inside the block: an unterminated value must not swallow the
        # following blocks' bytes. NUL padding also ends the value.
        limit = min(cursor.size, base + max_length)
        while position < limit and cursor.u8(position) not in (0, 62):  # NUL, ">"
            chars.append(cursor.text(position, 1))
            position += 1
        return "".join(chars).replace("SRS:", "").replace('"', "")
    return ""
