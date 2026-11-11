# -*- coding: utf-8 -*-
# Copyright (C) 2026 Yusuf Eminoğlu
# SPDX-License-Identifier: GPL-2.0-or-later
"""Geometry record decoders for NCZ Engine v2.

Each decoder receives a :class:`GeometryRecord` and returns a decoded
entity dict (the pre-model representation) or ``None`` when the record
does not yield a drawable entity. Decoders are registered by geometry
type in :data:`DECODERS`.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

from .binary import Cursor, finite_pair_in_range

RAD_TO_DEG = 180.0 / math.pi


def _finite(value: float, default: float = 0.0) -> float:
    """Return *value* when finite, else *default* (keeps NaN/inf out of the output)."""
    return value if math.isfinite(value) else default


@dataclass(frozen=True)
class GeometryRecord:
    """Context for one geometry record inside the file buffer."""

    cursor: Cursor
    base: int  # absolute offset of the record's kind byte
    size: int  # container size convention (total bytes - 1)
    shift: int  # GIS layout shift: 0 (kind 21) or 28 (kind 22)

    @property
    def geometry_type(self) -> int:
        """Numeric geometry type byte of the record."""
        return self.cursor.u8(self.base + 6)

    @property
    def layer_code(self) -> int:
        """Layer code byte of the record."""
        return self.cursor.u8(self.base + 7)

    @property
    def color_code(self) -> int:
        """Colour code byte of the record."""
        return self.cursor.u8(self.base + 37)

    @property
    def end(self) -> int:
        """Absolute offset one past the record's last byte."""
        return self.base + self.size + 1


def map_point(stored_first: float, stored_second: float, z: float = 0.0) -> dict:
    """Stored order is northing-first; QGIS x is the second stored value."""
    return {"x": stored_second, "y": stored_first, "z": z}


def _entity(record: GeometryRecord, kind: str, coordinates: list[dict], **extra: Any) -> dict:
    """Build a decoded entity payload for *record* with default fields."""
    payload = {
        "geometry_kind": kind,
        "layer_code": record.layer_code,
        "layer_name": "",
        "color_argb": None,
        "name": "",
        "label_text": "",
        "text_height": 0.0,
        "rotation_degrees": 0.0,
        "box_width": 0.0,
        "box_height": 0.0,
        "scale": 0.0,
        "grid_x": 0.0,
        "grid_y": 0.0,
        "radius": 0.0,
        "start_angle": 0.0,
        "end_angle": 0.0,
        "is_closed": False,
        "coordinates": coordinates,
        "_color_code": record.color_code,
    }
    payload.update(extra)
    return payload


def _first_vertex(record: GeometryRecord) -> tuple[float, float, float]:
    """Return the record's first stored (first, second, z) vertex values."""
    cursor = record.cursor
    first = cursor.f64(record.base + 8)
    second = cursor.f64(record.base + 16)
    z = cursor.f32(record.base + 24)
    return first, second, z


def _z_with_fallback(record: GeometryRecord) -> float:
    """Return the record elevation, falling back to the next f32 slot."""
    z = record.cursor.f32(record.base + 24)
    if z == 0:
        z = record.cursor.f32(record.base + 28)
    return z


# ── vector helpers ──────────────────────────────────────────────────


def _dist(a: dict, b: dict) -> float:
    """Return the 3D distance between two mapped points."""
    dx = a["x"] - b["x"]
    dy = a["y"] - b["y"]
    dz = a["z"] - b["z"]
    return math.sqrt(dx * dx + dy * dy + dz * dz)


def _points_equal(a: dict, b: dict) -> bool:
    """Return True when two mapped points coincide within 1 mm."""
    return (
        abs(a["x"] - b["x"]) < 0.001
        and abs(a["y"] - b["y"]) < 0.001
        and abs(a["z"] - b["z"]) < 0.001
    )


def _drop_collinear(points: list[dict]) -> list[dict]:
    """Iteratively remove ring vertices that continue the previous edge."""
    if len(points) <= 4:
        return points
    ring = list(points)
    changed = True
    while changed and len(ring) > 4:
        changed = False
        for index in range(len(ring)):
            before = ring[index - 1]
            here = ring[index]
            after = ring[(index + 1) % len(ring)]
            ax, ay = here["x"] - before["x"], here["y"] - before["y"]
            bx, by = after["x"] - here["x"], after["y"] - here["y"]
            len_a = math.sqrt(ax * ax + ay * ay)
            len_b = math.sqrt(bx * bx + by * by)
            if len_a < 0.001 or len_b < 0.001:
                ring.pop(index)
                changed = True
                break
            if abs(ax * by - ay * bx) / (len_a * len_b) <= 0.02:
                ring.pop(index)
                changed = True
                break
    return ring


def _ring_is_closed(points: list[dict]) -> bool:
    """Return True when a polyline's endpoints close into a ring."""
    if len(points) < 4:
        return False
    if _points_equal(points[0], points[-1]):
        return True
    if len(points) < 5:
        return False
    first_edge = _dist(points[0], points[1])
    last_edge = _dist(points[-2], points[-1])
    gap = _dist(points[0], points[-1])
    reference = min(first_edge, last_edge)
    if reference <= 0.001:
        return False
    return gap <= max(reference * 0.2, 0.05)


def _lengths_close(a: float, b: float) -> bool:
    """Return True when two edge lengths agree within 2% (or 2 cm)."""
    return abs(a - b) <= max(max(abs(a), abs(b)) * 0.02, 0.02)


def _rectangle_metrics(points: list[dict]) -> tuple[bool, float, float, float]:
    """(is_rectangle, width, height, rotation°) for a closed ring."""
    if len(points) < 5:
        return False, 0.0, 0.0, 0.0
    ring = points[:-1] if _points_equal(points[0], points[-1]) else points
    ring = _drop_collinear(ring)
    if len(ring) != 4:
        return False, 0.0, 0.0, 0.0

    edges = []
    lengths = []
    for index in range(4):
        a, b = ring[index], ring[(index + 1) % 4]
        edge = (b["x"] - a["x"], b["y"] - a["y"])
        edges.append(edge)
        lengths.append(math.sqrt(edge[0] * edge[0] + edge[1] * edge[1]))
    if any(value < 0.001 for value in lengths):
        return False, 0.0, 0.0, 0.0

    if not (_lengths_close(lengths[0], lengths[2]) and _lengths_close(lengths[1], lengths[3])):
        return False, 0.0, 0.0, 0.0
    for index in range(4):
        edge_a, edge_b = edges[index], edges[(index + 1) % 4]
        dot = abs(
            (edge_a[0] * edge_b[0] + edge_a[1] * edge_b[1])
            / (lengths[index] * lengths[(index + 1) % 4])
        )
        if dot > 0.03:
            return False, 0.0, 0.0, 0.0

    rotation = math.degrees(math.atan2(edges[0][1], edges[0][0])) % 360.0
    return True, lengths[0], lengths[1], rotation


def _corner_ring(
    origin_first: float,
    origin_second: float,
    width: float,
    height: float,
    bottom: tuple[float, float],
    side: tuple[float, float],
) -> list[dict]:
    """Rectangle ring from an origin and two (bottom, side) axis vectors."""
    p0 = (origin_first, origin_second)
    p1 = (p0[0] + bottom[0] * width, p0[1] + bottom[1] * width)
    p2 = (p1[0] + side[0] * height, p1[1] + side[1] * height)
    p3 = (p0[0] + side[0] * height, p0[1] + side[1] * height)
    return [map_point(*p0), map_point(*p1), map_point(*p2), map_point(*p3), map_point(*p0)]


def _rotated_rectangle(
    origin_first: float, origin_second: float, width: float, height: float, rotation_degrees: float
) -> list[dict]:
    """Box corner ring (bottom = cos/-sin, side = sin/cos axes).

    Uses ``deg * (pi/180)`` rather than ``math.radians`` to reproduce the
    the historical NCZ box computation used by the QGIS plugin.
    """
    angle = rotation_degrees * (math.pi / 180.0)
    return _corner_ring(
        origin_first,
        origin_second,
        width,
        height,
        (math.cos(angle), -math.sin(angle)),
        (math.sin(angle), math.cos(angle)),
    )


def _smart_object_ring(
    origin_first: float, origin_second: float, width: float, height: float, rotation_degrees: float
) -> list[dict]:
    """Smart-object corner ring (bottom = sin/cos, side = cos/-sin axes)."""
    angle = math.radians(rotation_degrees)
    return _corner_ring(
        origin_first,
        origin_second,
        width,
        height,
        (math.sin(angle), math.cos(angle)),
        (math.cos(angle), -math.sin(angle)),
    )


# ── decoders ────────────────────────────────────────────────────────


def decode_point(record: GeometryRecord) -> dict | None:
    """Decode a point record, or None when its coordinates are invalid."""
    first, second, _ = _first_vertex(record)
    if not finite_pair_in_range(first, second):
        return None
    cursor = record.cursor
    name_offset = record.base + record.shift + 86
    return _entity(
        record,
        "Point",
        [map_point(first, second, _z_with_fallback(record))],
        name=cursor.text(name_offset + 1, cursor.u8(name_offset)),
    )


def decode_line(record: GeometryRecord) -> dict | None:
    """Decode a two-endpoint line record, or None when invalid."""
    cursor = record.cursor
    first_a, second_a, z_a = _first_vertex(record)
    tail = record.base + record.size
    first_b = cursor.f64(tail - 19)
    second_b = cursor.f64(tail - 11)
    z_b = cursor.f32(tail - 3)
    if not (finite_pair_in_range(first_a, second_a) and finite_pair_in_range(first_b, second_b)):
        return None
    return _entity(
        record, "Line", [map_point(first_a, second_a, z_a), map_point(first_b, second_b, z_b)]
    )


def decode_circle(record: GeometryRecord) -> dict | None:
    """Decode a circle record into a centre point with a radius."""
    cursor = record.cursor
    first, second, z = _first_vertex(record)
    if not finite_pair_in_range(first, second):
        return None
    diameter = abs(cursor.f64(record.base + 50) - cursor.f64(record.base + 66))
    return _entity(record, "Circle", [map_point(first, second, z)], radius=_finite(diameter / 2.0))


def decode_arc(record: GeometryRecord) -> dict | None:
    """Decode an arc record into a centre point with radius and angles."""
    cursor = record.cursor
    first, second, z = _first_vertex(record)
    if not finite_pair_in_range(first, second):
        return None
    shifted = record.base + record.shift
    return _entity(
        record,
        "Arc",
        [map_point(first, second, z)],
        radius=_finite(cursor.f64(shifted + 86)),
        start_angle=_finite(cursor.f64(shifted + 104)),
        end_angle=_finite(cursor.f64(shifted + 112)),
    )


def _text_payload(record: GeometryRecord) -> str:
    """Return the first length-prefixed text payload found in the record."""
    cursor = record.cursor
    shifted = record.base + record.shift
    for length_offset, text_offset in (
        (shifted + 97, shifted + 98),
        (shifted + 86, shifted + 87),
        (record.base + 97, record.base + 98),
        (record.base + 86, record.base + 87),
    ):
        value = cursor.length_prefixed_text(length_offset, text_offset)
        if value:
            return value
    return ""


def decode_text(record: GeometryRecord) -> dict | None:
    """Decode a text record, or None when text or height is missing."""
    cursor = record.cursor
    first, second, _ = _first_vertex(record)
    if not finite_pair_in_range(first, second):
        return None
    text = _text_payload(record)
    if not text:
        return None
    shifted = record.base + record.shift
    height = cursor.positive_f32(shifted + 86)
    if height is None:
        height = cursor.positive_f32(record.base + 86)
    if height is None:
        return None
    return _entity(
        record,
        "Text",
        [map_point(first, second, _z_with_fallback(record))],
        label_text=text,
        text_height=height,
        rotation_degrees=_finite(cursor.f32(shifted + 90) * RAD_TO_DEG) % 360.0,
    )


def decode_symbol(record: GeometryRecord) -> dict | None:
    """Decode a symbol record into a point with its symbol code and size."""
    cursor = record.cursor
    first, second, z = _first_vertex(record)
    if not finite_pair_in_range(first, second):
        return None
    shifted = record.base + record.shift
    code_offset = shifted + 94
    if code_offset < record.base or code_offset >= cursor.size:
        code_offset = record.base + 94
    size = cursor.positive_f32(shifted + 86)
    if size is None:
        size = cursor.positive_f32(record.base + 86)
    if size is None:
        size = 5.0
    return _entity(
        record,
        "Symbol",
        [map_point(first, second, z)],
        label_text=f"S{cursor.u8(code_offset)}",
        text_height=size,
        rotation_degrees=_finite(cursor.f32(shifted + 90) * RAD_TO_DEG) % 360.0,
    )


def decode_polyline(record: GeometryRecord) -> dict | None:
    """Decode a vertex-list record into a polyline or closed polygon."""
    cursor = record.cursor
    shifted = record.base + record.shift
    label = cursor.text(shifted + 87, cursor.u8(shifted + 86))
    vertex_count = (record.size + 1 - 113 - record.shift) // 24
    if vertex_count < 2:
        return None

    points = []
    for index in range(vertex_count):
        vertex = shifted + 113 + index * 24
        if vertex + 24 > cursor.size:
            break
        first, second = cursor.f64(vertex), cursor.f64(vertex + 8)
        if not (math.isfinite(first) and math.isfinite(second)):
            continue
        points.append(map_point(first, second, _finite(cursor.f64(vertex + 16))))
    if len(points) < 2:
        return None

    closed = _ring_is_closed(points)
    if closed and not _points_equal(points[0], points[-1]):
        points.append(dict(points[0]))

    # Rectangle metrics only describe a closed ring; an open polyline whose
    # cyclic vertex list happens to reduce to four corners is no rectangle.
    is_rect, width, height, rotation = (
        _rectangle_metrics(points) if closed else (False, 0.0, 0.0, 0.0)
    )
    return _entity(
        record,
        "Polygon" if closed else "Polyline",
        points,
        label_text=label,
        is_closed=closed,
        box_width=width,
        box_height=height,
        rotation_degrees=rotation if is_rect else 0.0,
    )


def decode_compressed_curve(record: GeometryRecord) -> dict | None:
    """Decode a delta-packed curve record into a polyline."""
    cursor = record.cursor
    origin_first = cursor.f64(record.base + 8)
    origin_second = cursor.f64(record.base + 16)
    if not finite_pair_in_range(origin_first, origin_second):
        return None

    start = record.base + record.shift + 122
    if start + 8 > record.end:
        return None

    points: list[dict] = []
    bad_streak = 0
    for position in range(start, record.end - 7, 18):
        delta_first = cursor.f32(position)
        delta_second = cursor.f32(position + 4)
        if not (math.isfinite(delta_first) and math.isfinite(delta_second)):
            bad_streak += 1
            if points and bad_streak >= 4:
                break
            continue
        first = origin_first + delta_first
        second = origin_second + delta_second
        if not finite_pair_in_range(first, second):
            bad_streak += 1
            if points and bad_streak >= 4:
                break
            continue
        bad_streak = 0
        point = map_point(first, second)
        if points:
            previous = points[-1]
            if (
                abs(previous["x"] - point["x"]) < 0.0001
                and abs(previous["y"] - point["y"]) < 0.0001
            ):
                continue
        points.append(point)

    if len(points) < 2:
        return None
    return _entity(record, "Polyline", points)


def decode_box(record: GeometryRecord) -> dict | None:
    """Decode an oriented box record into a closed rectangular ring."""
    cursor = record.cursor
    first_a = cursor.f64(record.base + 8)
    second_a = cursor.f64(record.base + 16)
    shifted = record.base + record.shift
    first_b = cursor.f64(shifted + 104)
    second_b = cursor.f64(shifted + 112)
    if not (finite_pair_in_range(first_a, second_a) and finite_pair_in_range(first_b, second_b)):
        return None
    width = abs(first_b - first_a)
    height = abs(second_b - second_a)
    rotation = _finite(cursor.f32(shifted + 120) * RAD_TO_DEG) % 360.0
    return _entity(
        record,
        "Polygon",
        _rotated_rectangle(first_a, second_a, width, height, rotation),
        is_closed=True,
        box_width=width,
        box_height=height,
        rotation_degrees=rotation,
        label_text=_plan_box_name(record),
    )


def _plan_box_name(record: GeometryRecord) -> str:
    """A ``plan<digits>`` token inside the record, if present.

    Operates on a single lowercased copy of the record bytes and uses
    ``bytes.find`` instead of a per-byte scan, matching the expected decoded result.
    """
    cursor = record.cursor
    end = min(cursor.size, record.end)
    raw = cursor.raw(record.base, end - record.base)
    lowered = raw.lower()
    limit = len(raw)
    search = 0
    while True:
        found = lowered.find(b"plan", search)
        if found < 0:
            return ""
        position = found + 4
        while position < limit and position - found < 32 and _is_name_byte(raw[position]):
            position += 1
        if position > found + 4:
            name = raw[found:position].decode("ascii", errors="ignore").strip("\0 ")
            if len(name) > 4 and name[4:].isdigit():
                return name
        search = found + 1


def _is_name_byte(value: int) -> bool:
    """Return True when *value* is a plausible name token byte."""
    return 48 <= value <= 57 or 65 <= value <= 90 or 97 <= value <= 122 or value in (45, 95)


def _printable_prefixed_name(cursor: Cursor, start: int, end: int) -> str:
    """First printable length-prefixed string inside [start, end)."""
    bounded = min(end, cursor.size)
    for index in range(max(0, start), max(0, bounded - 2)):
        length = cursor.u8(index)
        if length <= 0 or length > 64 or index + 1 + length > bounded:
            continue
        value = cursor.text(index + 1, length).strip("\0 ")
        if value and all(ord(char) >= 32 or char == "\t" for char in value):
            return value
    return ""


def decode_map_sheet(record: GeometryRecord) -> dict | None:
    """Decode a map-sheet rectangle record into a closed ring."""
    cursor = record.cursor
    first_a = cursor.f64(record.base + 50)
    second_a = cursor.f64(record.base + 58)
    first_b = cursor.f64(record.base + 66)
    second_b = cursor.f64(record.base + 74)
    if not (finite_pair_in_range(first_a, second_a) and finite_pair_in_range(first_b, second_b)):
        return None

    lo_first, hi_first = min(first_a, first_b), max(first_a, first_b)
    lo_second, hi_second = min(second_a, second_b), max(second_a, second_b)
    if abs(hi_first - lo_first) < 0.001 or abs(hi_second - lo_second) < 0.001:
        return None

    return _entity(
        record,
        "MapSheet",
        [
            map_point(lo_first, lo_second),
            map_point(hi_first, lo_second),
            map_point(hi_first, hi_second),
            map_point(lo_first, hi_second),
            map_point(lo_first, lo_second),
        ],
        is_closed=True,
        box_width=hi_first - lo_first,
        box_height=hi_second - lo_second,
        label_text=_printable_prefixed_name(cursor, record.base + 86, record.end),
    )


def decode_triangle(record: GeometryRecord) -> dict | None:
    """Decode a triangle record into a closed three-vertex ring."""
    cursor = record.cursor

    def vertex(first_at: int, second_at: int, z_at: int | None = None) -> dict | None:
        """Return a mapped vertex at the given offsets, or None if invalid."""
        if record.base + first_at + 8 > cursor.size or record.base + second_at + 8 > cursor.size:
            return None
        first = cursor.f64(record.base + first_at)
        second = cursor.f64(record.base + second_at)
        z = 0.0
        if z_at is not None and record.base + z_at + 4 <= cursor.size:
            z = cursor.f32(record.base + z_at)
        if not finite_pair_in_range(first, second):
            return None
        return map_point(first, second, z)

    a = vertex(8, 16, 24)
    b = vertex(86, 94)
    c = vertex(106, 114)
    if a is None or b is None or c is None:
        return None
    doubled_area = abs(
        (b["x"] - a["x"]) * (c["y"] - a["y"]) - (b["y"] - a["y"]) * (c["x"] - a["x"])
    )
    if doubled_area <= 0.0001:
        return None
    return _entity(record, "Triangle", [a, b, c], is_closed=True)


def decode_block_reference(record: GeometryRecord) -> dict | None:
    """Decode a block-reference record into a point with its label."""
    cursor = record.cursor
    first, second, z = _first_vertex(record)
    if not finite_pair_in_range(first, second):
        return None
    shifted = record.base + record.shift
    return _entity(
        record,
        "Block",
        [map_point(first, second, z)],
        label_text=_printable_prefixed_name(cursor, shifted + 86, record.end),
        rotation_degrees=_finite(cursor.f32(shifted + 118) * RAD_TO_DEG) % 360.0,
    )


def decode_smart_object(record: GeometryRecord) -> dict | None:
    """Decode a smart-object record into a closed, scaled rectangle ring."""
    cursor = record.cursor
    first_a = cursor.f64(record.base + 8)
    second_a = cursor.f64(record.base + 16)
    if not finite_pair_in_range(first_a, second_a):
        return None

    def bounded_f64(offset: int) -> float:
        """Read a float64 at *offset*, or 0.0 when it would exceed the buffer."""
        if record.base + offset + 8 <= cursor.size:
            return _finite(cursor.f64(record.base + offset))
        return 0.0

    width = bounded_f64(169)
    height = bounded_f64(177)
    grid_x = bounded_f64(185)
    grid_y = bounded_f64(193)
    if width <= 0.0 or height <= 0.0:
        first_b = cursor.f64(record.base + 66)
        second_b = cursor.f64(record.base + 74)
        if not finite_pair_in_range(first_b, second_b):
            return None
        width = abs(first_b - first_a)
        height = abs(second_b - second_a)
    if width < 0.001 or height < 0.001:
        return None

    angle_grads = cursor.f32(record.base + 82)
    rotation = (angle_grads * 0.9) % 360.0 if math.isfinite(angle_grads) else 0.0
    scale = cursor.f32(record.base + 86)
    if not math.isfinite(scale):
        scale = 0.0

    payload = cursor.raw(record.base, record.end - record.base)
    label = "BASIC" if b"BASIC" in payload else _ascii_token(cursor, record.base + 145, record.end)
    return _entity(
        record,
        "SmartObject",
        _smart_object_ring(first_a, second_a, width, height, rotation),
        is_closed=True,
        box_width=width,
        box_height=height,
        rotation_degrees=rotation,
        scale=scale,
        grid_x=grid_x,
        grid_y=grid_y,
        label_text=label,
    )


def _ascii_token(cursor: Cursor, start: int, end: int) -> str:
    """Return the first ASCII name token of length >= 3 inside [start, end)."""
    bounded = min(end, cursor.size)
    position = max(0, start)
    while position < bounded:
        if not _is_name_byte(cursor.u8(position)):
            position += 1
            continue
        token_start = position
        while position < bounded and _is_name_byte(cursor.u8(position)):
            position += 1
        if position - token_start >= 3:
            return cursor.raw(token_start, position - token_start).decode("ascii", errors="ignore")
    return ""


DECODERS = {
    1: decode_point,
    2: decode_line,
    3: decode_circle,
    4: decode_arc,
    5: decode_text,
    6: decode_symbol,
    7: decode_polyline,
    9: decode_compressed_curve,
    10: decode_box,
    11: decode_map_sheet,
    12: decode_triangle,
    13: decode_block_reference,
    15: decode_smart_object,
}
