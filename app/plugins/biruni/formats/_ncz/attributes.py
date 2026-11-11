# -*- coding: utf-8 -*-
# Copyright (C) 2026 Yusuf Eminoğlu
# SPDX-License-Identifier: GPL-2.0-or-later
"""@TAB attribute-table decoding for NCZ Engine v2.

Attribute records are located by scanning the file for the ASCII marker
``@TAB`` followed by decimal digits, then classified into one of three
row variants (label / segment / ascii) as described in
``docs/NCZ_FORMAT.md``.
"""

from __future__ import annotations

import math
import struct
from dataclasses import dataclass

from .binary import Cursor

_F32 = struct.Struct("<f")
_F64 = struct.Struct("<d")

COORDINATE_LOWER = 1000.0
COORDINATE_UPPER = 100000000.0

_ATTRIBUTE_MARKER = b"@TAB"


@dataclass(frozen=True)
class AttributeMarker:
    record_start: int
    table_ref: str


def find_attribute_markers(cursor: Cursor) -> list[AttributeMarker]:
    """Locate every ``@TAB<digits>`` marker in the buffer."""
    markers: list[AttributeMarker] = []
    position = 0
    while True:
        found = cursor.find(_ATTRIBUTE_MARKER, position)
        if found < 0:
            break
        end = found + 4
        while end < cursor.size and 48 <= cursor.u8(end) <= 57:
            end += 1
        if end == found + 4:
            # "@TAB" not followed by digits (e.g. text "@TABLE") is no marker.
            position = end
            continue
        table_ref = cursor.raw(found, end - found).decode("ascii", errors="ignore")
        record_start = found
        ref_length = end - found
        if found > 0 and cursor.u8(found - 1) == ref_length:
            record_start = found - 1
        markers.append(AttributeMarker(record_start, table_ref))
        position = end
    return markers


def _u16(chunk: bytes, offset: int) -> int:
    """Read a little-endian uint16 from *chunk*, or 0 when out of range."""
    if 0 <= offset and offset + 2 <= len(chunk):
        return int.from_bytes(chunk[offset : offset + 2], "little")
    return 0


def _u32(chunk: bytes, offset: int) -> int:
    """Read a little-endian uint32 from *chunk*, or 0 when out of range."""
    if 0 <= offset and offset + 4 <= len(chunk):
        return int.from_bytes(chunk[offset : offset + 4], "little")
    return 0


def _f32(chunk: bytes, offset: int) -> float:
    """Read a little-endian float32 from *chunk*, or 0.0 when out of range."""
    if 0 <= offset and offset + 4 <= len(chunk):
        return _F32.unpack_from(chunk, offset)[0]
    return 0.0


def _f64(chunk: bytes, offset: int) -> float:
    """Read a little-endian float64 from *chunk*, or 0.0 when out of range."""
    if 0 <= offset and offset + 8 <= len(chunk):
        return _F64.unpack_from(chunk, offset)[0]
    return 0.0


def _byte(chunk: bytes, offset: int) -> int:
    """Return the byte at *offset*, or 0 when out of range."""
    return chunk[offset] if 0 <= offset < len(chunk) else 0


def _safe_round(value: float) -> float | None:
    """Round *value* to six decimals, or None when it is not finite."""
    if not math.isfinite(value):
        return None
    if abs(value) < 1e-12:
        return 0.0
    return round(float(value), 6)


def _looks_like_coordinate(x: float | None, y: float | None) -> bool:
    """Return True when a pair resembles a plausible projected coordinate."""
    return (
        x is not None
        and y is not None
        and math.isfinite(x)
        and math.isfinite(y)
        and abs(x) <= COORDINATE_UPPER
        and abs(y) <= COORDINATE_UPPER
        and (abs(x) >= COORDINATE_LOWER or abs(y) >= COORDINATE_LOWER)
    )


def _collect_ascii_fields(chunk: bytes) -> list[str]:
    """Collect unique length-prefixed printable ASCII strings from *chunk*."""
    values: list[str] = []
    seen: set[str] = set()
    for index in range(max(0, len(chunk) - 1)):
        length = chunk[index]
        if length <= 0 or length > 64 or index + 1 + length > len(chunk):
            continue
        raw = chunk[index + 1 : index + 1 + length]
        if not raw or not all(32 <= value < 127 for value in raw):
            continue
        value = raw.decode("ascii", errors="ignore").strip("\0 ")
        if not value or value in seen:
            continue
        seen.add(value)
        values.append(value)
    return values


def _decode_label_row(chunk: bytes, label_text: str, label_length: int, row_index: int) -> dict:
    """Decode a label-variant attribute record into columns."""
    separator = 29 + label_length
    columns = {
        "row_variant": "label",
        "record_length": len(chunk),
        "label": label_text,
        "label_length": label_length,
        "prefix_float": _safe_round(_f32(chunk, 17)),
        "code_u16": _u16(chunk, 25),
        "separator_1": _byte(chunk, separator),
        "style_code": _u32(chunk, separator + 1),
        "flag_1": _byte(chunk, separator + 5),
        "flag_2": _byte(chunk, separator + 6),
        "flag_3": _byte(chunk, separator + 7),
        "coord_1_x": _safe_round(_f64(chunk, separator + 8)),
        "coord_1_y": _safe_round(_f64(chunk, separator + 16)),
        "separator_2": _byte(chunk, separator + 35),
        "scale_float": _safe_round(_f32(chunk, separator + 46)),
        "coord_2_x": _safe_round(_f64(chunk, separator + 50)),
        "coord_2_y": _safe_round(_f64(chunk, separator + 58)),
        "coord_3_x": _safe_round(_f64(chunk, separator + 66)),
        "coord_3_y": _safe_round(_f64(chunk, separator + 74)),
    }
    if len(chunk) >= 11:
        columns["table_ref_inline"] = chunk[1:11].decode("ascii", errors="ignore").strip("\0 ")
    return {"row_index": row_index, "columns": columns}


def _decode_segment_row(chunk: bytes, row_index: int) -> dict:
    """Decode a coordinate-bearing segment attribute record into columns."""
    columns = {
        "row_variant": "segment",
        "record_length": len(chunk),
        "coord_0_x": _safe_round(_f64(chunk, 17)),
        "coord_0_y": _safe_round(_f64(chunk, 25)),
        "style_code": _u32(chunk, 37),
        "flag_1": _byte(chunk, 41),
        "flag_2": _byte(chunk, 42),
        "flag_3": _byte(chunk, 43),
        "flag_4": _byte(chunk, 44),
        "coord_1_x": _safe_round(_f64(chunk, 45)),
        "coord_1_y": _safe_round(_f64(chunk, 53)),
        "separator_2": _byte(chunk, 72),
        "coord_2_x": _safe_round(_f64(chunk, 87)),
        "coord_2_y": _safe_round(_f64(chunk, 95)),
        "coord_3_x": _safe_round(_f64(chunk, 103)),
        "coord_3_y": _safe_round(_f64(chunk, 111)),
    }
    if len(chunk) >= 11:
        columns["table_ref_inline"] = chunk[1:11].decode("ascii", errors="ignore").strip("\0 ")
    return {"row_index": row_index, "columns": columns}


def _decode_ascii_row(chunk: bytes, table_ref: str, row_index: int) -> dict:
    """Decode an unrecognized attribute record using its printable fields."""
    values = _collect_ascii_fields(chunk)
    columns = {
        "row_variant": "unknown",
        "record_length": len(chunk),
        "ascii_values": " | ".join(value for value in values if value != table_ref),
    }
    if len(chunk) >= 11:
        columns["table_ref_inline"] = chunk[1:11].decode("ascii", errors="ignore").strip("\0 ")
    return {"row_index": row_index, "columns": columns}


def decode_attribute_row(chunk: bytes, table_ref: str, row_index: int) -> dict:
    """Classify and decode one attribute record."""
    label_length = _byte(chunk, 28)
    has_label = 1 <= label_length <= 64 and 29 + label_length <= len(chunk)
    if has_label:
        label_bytes = chunk[29 : 29 + label_length]
        if all(32 <= value < 127 for value in label_bytes):
            label_text = label_bytes.decode("ascii", errors="ignore").strip("\0 ")
            if label_text:
                return _decode_label_row(chunk, label_text, label_length, row_index)

    if len(chunk) >= 119:
        coord_0 = (_safe_round(_f64(chunk, 17)), _safe_round(_f64(chunk, 25)))
        coord_1 = (_safe_round(_f64(chunk, 45)), _safe_round(_f64(chunk, 53)))
        coord_2 = (_safe_round(_f64(chunk, 87)), _safe_round(_f64(chunk, 95)))
        if (
            _looks_like_coordinate(*coord_0)
            and _looks_like_coordinate(*coord_1)
            and _looks_like_coordinate(*coord_2)
        ):
            return _decode_segment_row(chunk, row_index)

    return _decode_ascii_row(chunk, table_ref, row_index)


def decode_attribute_tables(cursor: Cursor) -> list[dict]:
    """Decode every ``@TAB`` table into ``{table_ref, rows}`` dicts."""
    markers = find_attribute_markers(cursor)
    if not markers:
        return []

    tables: dict[str, list[dict]] = {}
    for index, marker in enumerate(markers):
        next_start = cursor.size
        if index + 1 < len(markers):
            candidate = markers[index + 1].record_start
            if candidate > marker.record_start:
                next_start = candidate
        record_end = min(cursor.size, next_start)
        if record_end <= marker.record_start:
            continue
        chunk = cursor.raw(marker.record_start, record_end - marker.record_start)
        rows = tables.setdefault(marker.table_ref, [])
        rows.append(decode_attribute_row(chunk, marker.table_ref, len(rows) + 1))

    return [
        {"table_ref": table_ref, "rows": rows} for table_ref, rows in sorted(tables.items()) if rows
    ]
