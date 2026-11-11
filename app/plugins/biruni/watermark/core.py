"""Who was given this copy: a mark in the coordinates themselves.

Every coordinate of a copy is put a quarter of a fingerprint step (a fortieth of a millimetre on a projected map)
to one side or the other of where the fingerprint reads it, the side chosen by a secret kept for that recipient.
The fingerprint takes coordinates to the step, so the marked copy is the certified data, the same certificate
checks it; but its coordinates, read past the step, tell whose copy it is. It is found again from the copy alone
(no original needed), from any part of it (a few hundred coordinates are enough), whatever file it was saved in.
It does not survive coordinates rounded to the millimetre, or a change of coordinate system.

The secrets stay on this computer (the register of the copies given), never in a copy.
"""
import datetime as dt
import hashlib
import hmac
import json
import math
import os
import secrets

from qgis.core import (QgsAbstractGeometryTransformer, QgsApplication, QgsCoordinateTransformContext, QgsFeature,
                       QgsFeatureRequest, QgsGeometry, QgsVectorFileWriter, QgsWkbTypes)

from ..fingerprint import core as fp

SIDE = 0.25          # how far from the step a mark puts a coordinate, in steps
BAND = (0.15, 0.35)  # a coordinate this far from the step (either side) carries a mark; else it says nothing
LIMIT = 60000        # coordinates read to find a mark


class WatermarkError(Exception):
    pass


def register_path():
    return os.path.join(QgsApplication.qgisSettingsDirPath(), "biruni", "watermarks.json")


def register(path=None):
    path = path or register_path()
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f).get("copies", [])
    except (OSError, ValueError):
        return []


def _remember(entry, path=None):
    path = path or register_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    copies = register(path) + [entry]
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"format": 1, "copies": copies}, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


def _bits(key, qx, qy):
    d = hmac.new(key, f"{qx},{qy}".encode("ascii"), hashlib.sha256).digest()
    return d[0] & 1, d[1] & 1


class _Mark(QgsAbstractGeometryTransformer):
    def __init__(self, key, step):
        super().__init__()
        self.key, self.step = key, step

    def transformPoint(self, x, y, z, m):
        s = self.step
        qx, qy = math.floor(x / s + 0.5), math.floor(y / s + 0.5)
        bx, by = _bits(self.key, qx, qy)
        return True, (qx + (SIDE if bx else -SIDE)) * s, (qy + (SIDE if by else -SIDE)) * s, z, m


def mark_geometry(g, key, step):
    """The shape with its coordinates marked (a curve is left as it is: its box would move with its points)."""
    if g is None or g.isNull() or g.isEmpty() or QgsWkbTypes.isCurvedType(g.wkbType()):
        return g
    a = g.constGet().clone()
    a.transform(_Mark(key, step))
    return QgsGeometry(a)


def mark(layer, recipient, path, note="", fids=None, reg=None):
    """A marked copy of ``layer`` for ``recipient``, saved as a GeoPackage at ``path``; the secret is kept in the
    register. Returns the register entry."""
    recipient = (recipient or "").strip()
    if not recipient:
        raise WatermarkError("recipient")
    _cell, step = fp.grid_for(layer)
    key = secrets.token_bytes(32)
    if os.path.exists(path):
        os.remove(path)
    opts = QgsVectorFileWriter.SaveVectorOptions()
    opts.driverName = "GPKG"
    opts.layerName = layer.name()
    opts.fileEncoding = "UTF-8"
    keys = set(layer.primaryKeyAttributes())
    from qgis.core import QgsFields
    fields = QgsFields()
    idx = []
    for i, f in enumerate(layer.fields()):
        if i not in keys:
            fields.append(f)
            idx.append(i)
    w = QgsVectorFileWriter.create(path, fields, layer.wkbType(), layer.crs(), QgsCoordinateTransformContext(), opts)
    if w.hasError() != QgsVectorFileWriter.WriterError.NoError:
        raise WatermarkError(w.errorMessage())
    req = QgsFeatureRequest()
    if fids is not None:
        req.setFilterFids(list(fids))
    n = 0
    for f in layer.getFeatures(req):
        nf = QgsFeature(fields)
        if f.hasGeometry():
            nf.setGeometry(mark_geometry(QgsGeometry(f.geometry()), key, step))
        nf.setAttributes([f.attribute(i) for i in idx])
        w.addFeature(nf)
        n += 1
    del w
    entry = {"recipient": recipient, "note": note, "layer": layer.name(), "features": n, "file": path,
             "made": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
             "crs": layer.crs().authid(), "step": step, "key": key.hex()}
    _remember(entry, reg)
    return entry


def read_marks(layer, limit=LIMIT):
    """The coordinates of ``layer`` that carry a mark: {(qx, qy): (side x or None, side y or None)}."""
    _cell, step = fp.grid_for(layer)
    lo, hi = BAND
    out = {}
    seen = 0
    for f in layer.getFeatures(QgsFeatureRequest().setNoAttributes()):
        g = f.geometry()
        if g is None or g.isNull():
            continue
        for p in g.constGet().vertices():
            seen += 1
            ux, uy = p.x() / step, p.y() / step
            qx, qy = math.floor(ux + 0.5), math.floor(uy + 0.5)
            ox, oy = ux - qx, uy - qy
            sx = (1 if ox > 0 else 0) if lo <= abs(ox) <= hi else None
            sy = (1 if oy > 0 else 0) if lo <= abs(oy) <= hi else None
            if sx is not None or sy is not None:
                out[(qx, qy)] = (sx, sy)
            if len(out) >= limit:
                return out, seen
    return out, seen


class Finding:
    def __init__(self, entry, agree, total):
        self.entry, self.agree, self.total = entry, agree, total

    @property
    def rate(self):
        return self.agree / self.total if self.total else 0.0

    @property
    def z(self):
        """How far from chance (half) the agreement is, in standard deviations."""
        return (self.agree - self.total / 2.0) / math.sqrt(self.total / 4.0) if self.total else 0.0


def find(layer, reg=None, copies=None):
    """[Finding] of every copy in the register, the likeliest first; the first is the one when its rate is high
    and its z far from chance (see who())."""
    marks, _seen = read_marks(layer)
    out = []
    for e in (copies if copies is not None else register(reg)):
        key = bytes.fromhex(e["key"])
        agree = total = 0
        for (qx, qy), (sx, sy) in marks.items():
            bx, by = _bits(key, qx, qy)
            if sx is not None:
                total += 1
                agree += sx == bx
            if sy is not None:
                total += 1
                agree += sy == by
        out.append(Finding(e, agree, total))
    out.sort(key=lambda f: -f.z)
    return out, len(marks)


def who(layer, reg=None, copies=None):
    """The Finding of the copy this layer is, or None when no copy of the register is in it."""
    found, _n = find(layer, reg, copies)
    if found and found[0].total >= 24 and found[0].rate >= 0.9 and found[0].z >= 4.5:   # chance: < 1 in 250,000
        return found[0]
    return None
