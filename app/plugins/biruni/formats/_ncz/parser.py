# -*- coding: utf-8 -*-
# Copyright (C) 2026 Yusuf Eminoğlu
# SPDX-License-Identifier: GPL-2.0-or-later
"""NCZ Engine v2 orchestrator: block scan, lazy catalog, selective decode.

The engine works in two phases:

1. **Index** — a single block scan records drawing metadata (layers,
   colours, CRS) and the *position* of every geometry record, without
   decoding any coordinates.
2. **Decode** — geometry records are decoded on demand, either all of
   them (:meth:`NczCatalog.decode_all`) or only those belonging to a
   chosen set of layer codes (:meth:`NczCatalog.decode_layers`).

This lets the dock build a layer catalog cheaply and materialize only
the layers a user actually imports.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable

from .attributes import decode_attribute_tables
from .binary import Cursor
from .blocks import (
    CONTAINER_BLOCK_KINDS,
    GEOMETRY_BLOCK_KINDS,
    GIS_LAYOUT_SHIFT,
    DrawingMetadata,
    apply_metadata_block,
    scan_blocks,
    scan_embedded_geometry,
)
from .geometry import DECODERS, GeometryRecord

PARSER_BACKEND_V2 = "pure-python-v2"

# Coarse geometry family per numeric type, for the cheap layer catalog.
# Type 7 (polyline/polygon) is closure-dependent, so it is reported as a
# line family until decoded; the value never affects layer-code selection.
_TYPE_FAMILY = {
    1: "POINT",
    2: "LINE",
    3: "POLYGON",
    4: "LINE",
    5: "POINT",
    6: "POINT",
    7: "LINE",
    9: "LINE",
    10: "POLYGON",
    11: "POLYGON",
    12: "POLYGON",
    13: "POINT",
    15: "POLYGON",
}


@dataclass(frozen=True)
class RecordIndex:
    """Location of one geometry record, without decoded geometry."""

    base: int
    size: int
    shift: int
    geometry_type: int
    layer_code: int


@dataclass
class LayerSummary:
    """Cheap per-layer catalog entry produced before decoding."""

    layer_code: int
    layer_name: str
    record_count: int = 0
    families: set = field(default_factory=set)


class NczCatalog:
    """Indexed view of an NCZ buffer supporting selective decoding."""

    def __init__(self, data: bytes) -> None:
        """Wrap an in-memory NCZ buffer; call :meth:`index` before use."""
        self._cursor = Cursor(data)
        self.metadata = DrawingMetadata()
        self.records: list[RecordIndex] = []
        self.unsupported: dict[int, int] = {}
        self._indexed = False

    # ── phase 1: index ────────────────────────────────────────────

    def index(self) -> "NczCatalog":
        """Scan blocks once, collecting metadata and record positions."""
        if self._indexed:
            return self
        cursor = self._cursor
        for block in scan_blocks(cursor):
            if block.kind in GEOMETRY_BLOCK_KINDS and block.size >= 7:
                self._index_record(
                    block.offset, block.size, GIS_LAYOUT_SHIFT if block.kind == 22 else 0
                )
            elif block.kind in CONTAINER_BLOCK_KINDS:
                for inner in scan_embedded_geometry(cursor, block):
                    self._index_record(
                        inner.offset, inner.size, GIS_LAYOUT_SHIFT if inner.kind == 22 else 0
                    )
            else:
                apply_metadata_block(cursor, block, self.metadata)
        self._indexed = True
        return self

    def _index_record(self, base: int, size: int, shift: int) -> None:
        """Record the position and layer of one geometry record."""
        cursor = self._cursor
        if base + 7 >= cursor.size:
            return
        geometry_type = cursor.u8(base + 6)
        self.records.append(
            RecordIndex(
                base=base,
                size=size,
                shift=shift,
                geometry_type=geometry_type,
                layer_code=cursor.u8(base + 7),
            )
        )

    # ── layer catalog ─────────────────────────────────────────────

    def layer_catalog(self) -> list[LayerSummary]:
        """Summarize records per layer without decoding geometry."""
        self.index()
        summaries: dict[int, LayerSummary] = {}
        for record in self.records:
            if record.geometry_type not in DECODERS:
                continue
            summary = summaries.get(record.layer_code)
            if summary is None:
                summary = LayerSummary(
                    layer_code=record.layer_code,
                    layer_name=self.metadata.layer_name(record.layer_code),
                )
                summaries[record.layer_code] = summary
            summary.record_count += 1
            summary.families.add(_TYPE_FAMILY.get(record.geometry_type, "LINE"))
        return [summaries[key] for key in sorted(summaries)]

    # ── phase 2: decode ───────────────────────────────────────────

    def decode_all(self) -> list[dict]:
        """Decode every supported geometry record."""
        return self._decode(self.records)

    def decode_layers(self, layer_codes: Iterable[int]) -> list[dict]:
        """Decode only records whose layer code is in *layer_codes*."""
        wanted = set(layer_codes)
        selected = [r for r in self.records if r.layer_code in wanted]
        return self._decode(selected)

    def _decode(self, records: list[RecordIndex]) -> list[dict]:
        """Decode the given records, finalizing metadata on each payload."""
        self.index()
        cursor = self._cursor
        metadata = self.metadata
        entities: list[dict] = []
        for record in records:
            decoder = DECODERS.get(record.geometry_type)
            if decoder is None:
                self.unsupported[record.geometry_type] = (
                    self.unsupported.get(record.geometry_type, 0) + 1
                )
                continue
            payload = decoder(GeometryRecord(cursor, record.base, record.size, record.shift))
            if payload is None:
                continue
            self._finalize(payload, metadata)
            entities.append(payload)
        return _drop_smart_object_artifacts(entities)

    @staticmethod
    def _finalize(payload: dict, metadata: DrawingMetadata) -> None:
        """Attach resolved layer name and colour to a decoded payload."""
        color_code = payload.pop("_color_code", 0)
        layer_code = payload["layer_code"]
        payload["layer_name"] = metadata.layer_name(layer_code)
        color = metadata.resolve_color(layer_code, color_code)
        if color is None:
            # Post-pass: an unresolved colour (e.g. a non-standard colour
            # code) falls back to the layer's own colour.
            color = metadata.resolve_color(layer_code, 0)
        payload["color_argb"] = color

    # ── attribute tables ──────────────────────────────────────────

    def decode_attribute_tables(self) -> list[dict]:
        """Decode every ``@TAB`` attribute table in the buffer."""
        return decode_attribute_tables(self._cursor)


def _drop_smart_object_artifacts(entities: list[dict]) -> list[dict]:
    """Remove ``S0`` layer-0 symbols emitted alongside smart objects."""
    if not any(e["geometry_kind"] == "SmartObject" for e in entities):
        return entities
    return [
        entity
        for entity in entities
        if not (
            entity["geometry_kind"] == "Symbol"
            and entity["layer_code"] == 0
            and entity.get("label_text") == "S0"
        )
    ]


def parse_file(file_path: str) -> dict:
    """Full decode of *file_path* into the public payload dict."""
    with open(file_path, "rb") as handle:
        data = handle.read()
    return parse_bytes(data)


def parse_bytes(data: bytes) -> dict:
    """Full decode of an in-memory NCZ buffer."""
    catalog = NczCatalog(data).index()
    entities = catalog.decode_all()
    metadata = catalog.metadata
    return {
        "entities": entities,
        "attribute_tables": catalog.decode_attribute_tables(),
        "layer_names": list(metadata.layer_names),
        "layer_colors": list(metadata.layer_colors),
        "version_name": metadata.version_name,
        "epsg": metadata.epsg,
        "projection_text": metadata.projection_text,
        "unsupported_geometry_types": dict(catalog.unsupported),
        "parser_backend": PARSER_BACKEND_V2,
    }
