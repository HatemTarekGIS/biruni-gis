"""Netcad NCZ drawings (Turkey) - the format most Turkish surveying and planning offices exchange.

Decoding uses the bundled NCZ Engine v2 (formats/_ncz, from NCZ2Geo, GPL-2.0-or-later). Each Netcad layer
gives up to four GIS layers: points (points, symbols, block references, circle centres), lines, polygons
(closed polylines, boxes, triangles, map sheets) and texts (points carrying the text, height and angle).
Arcs and circles are turned into vertices. Turkish text is decoded as Windows-1254."""
import math

from .common import Dataset, FormatError, line_wkt, point_wkt, polygon_wkt

ID = "ncz"
LABEL = "Netcad drawing (NCZ)"
EXTENSIONS = (".ncz", ".nca")
COUNTRIES = ("TR",)

_POINT_KINDS = {"Point", "Symbol", "BlockReference"}
_POLY_KINDS = {"Polygon", "Box", "Triangle", "MapSheet", "SmartObject"}


def _xyz(c):
    return (float(c.get("x", 0.0)), float(c.get("y", 0.0)), float(c.get("z", 0.0) or 0.0))


def _arc_points(cx, cy, r, a0, a1, step=math.radians(5)):
    if a1 <= a0:
        a1 += 2 * math.pi
    n = max(2, int((a1 - a0) / step) + 1)
    return [(cx + r * math.cos(a0 + (a1 - a0) * i / n), cy + r * math.sin(a0 + (a1 - a0) * i / n), 0.0)
            for i in range(n + 1)]


def _epsg(meta):
    text = str(meta or "")
    digits = "".join(ch for ch in text if ch.isdigit())
    return f"EPSG:{digits}" if digits else None


def read(path):
    from ._ncz import parse_file

    try:
        result = parse_file(path)
    except Exception as e:  # noqa: BLE001 - damaged or unknown variant
        raise FormatError(f"The NCZ file could not be decoded ({e}).") from e
    entities = result.get("entities") or []
    if not entities:
        raise FormatError("No drawing objects were found in this NCZ file (it may be empty or a newer "
                          "Netcad variant). Export it to DXF from Netcad as a fallback.")
    ds = Dataset(ID, _epsg(result.get("epsg")))
    if result.get("projection_text") and not ds.crs:
        ds.notes.append(f"Netcad projection: {result['projection_text']} - choose the matching coordinate "
                        "system.")
    skipped = 0
    for e in entities:
        kind = e.get("geometry_kind", "")
        layer = e.get("layer_name") or f"layer_{e.get('layer_code', 0)}"
        pts = [_xyz(c) for c in e.get("coordinates") or []]
        attrs = {"netcad_layer": layer, "kind": kind}
        if e.get("name"):
            attrs["name"] = e["name"]
        if e.get("color_argb") is not None:
            attrs["color"] = f"#{int(e['color_argb']) & 0xFFFFFF:06x}"
        if kind == "Text":
            if not pts:
                skipped += 1
                continue
            ds.layer(layer, "point").add(point_wkt(*pts[0]), text=e.get("label_text", ""),
                                          height=float(e.get("text_height") or 0.0),
                                          angle=float(e.get("rotation_degrees") or 0.0), **attrs)
        elif kind in ("Circle", "Arc") and pts and e.get("radius"):
            cx, cy, _z = pts[0]
            r = float(e["radius"])
            if kind == "Circle":
                ring = _arc_points(cx, cy, r, 0.0, 2 * math.pi)
                ds.layer(layer, "line").add(line_wkt(ring), radius=r, **attrs)
            else:
                arc = _arc_points(cx, cy, r, float(e.get("start_angle") or 0), float(e.get("end_angle") or 0))
                ds.layer(layer, "line").add(line_wkt(arc), radius=r, **attrs)
        elif kind in _POINT_KINDS or (len(pts) == 1 and kind not in _POLY_KINDS):
            if not pts:
                skipped += 1
                continue
            ds.layer(layer, "point").add(point_wkt(*pts[0]), label=e.get("label_text", ""), **attrs)
        elif kind in _POLY_KINDS or (e.get("is_closed") and len(pts) >= 3):
            wkt = polygon_wkt([pts])
            if wkt is None:
                skipped += 1
                continue
            ds.layer(layer, "polygon").add(wkt, label=e.get("label_text", ""), **attrs)
        elif len(pts) >= 2:
            ds.layer(layer, "line").add(line_wkt(pts), label=e.get("label_text", ""), **attrs)
        else:
            skipped += 1
    tables = result.get("attribute_tables") or []
    if tables:
        att = ds.layer("attribute_tables", "none")
        for t in tables:
            for row in t.get("rows", []) if isinstance(t, dict) else []:
                cols = row.get("columns", {}) if isinstance(row, dict) else {}
                att.add(None, table_ref=str(t.get("table_ref", "")),
                        **{str(k): str(v) for k, v in cols.items()})
    unsupported = result.get("unsupported_geometry_types") or {}
    if unsupported:
        ds.notes.append("Some Netcad object types are not supported yet and were skipped: "
                        + ", ".join(f"type {k} ×{v}" for k, v in sorted(unsupported.items())))
    if skipped:
        ds.notes.append(f"{skipped} objects without usable coordinates were skipped.")
    return ds
