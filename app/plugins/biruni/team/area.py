"""A member's area of work: the place on the map where they may change features, kept by the host.

The host draws it (or takes it from features already on the map) and it is kept with the member's rights, under
the key "_area": {"wkt": the area, "crs": its coordinate system}. It travels with the rights to the member and
to whoever takes over as host. The host refuses a change whose feature is not inside the area before or after the
change, so a member can neither move a parcel in from outside, nor take one out, nor delete one outside, nor draw
one beyond it; the member's copy goes back as for any refused change. A feature without a shape is not inside.
"""
import json

from qgis.core import QgsCoordinateReferenceSystem, QgsCoordinateTransform, QgsGeometry, QgsProject, QgsWkbTypes

KEY = "_area"
TOLERANCE_M = 0.05      # a shared edge drawn on the area's line is inside


def of(rights):
    a = (rights or {}).get(KEY)
    return a if isinstance(a, dict) and a.get("wkt") else None


def make(geometries, crs):
    """The area that covers these shapes (any kind: lines and points are taken with a metre around them)."""
    parts = []
    for g in geometries:
        if g is None or g.isNull() or g.isEmpty():
            continue
        g = QgsGeometry(g)
        if g.type() != QgsWkbTypes.PolygonGeometry:
            g = g.buffer(1.0 if not crs.isGeographic() else 1e-5, 8)
        parts.append(g.makeValid())
    if not parts:
        return None
    u = QgsGeometry.unaryUnion(parts)
    if u.isNull() or u.isEmpty():
        return None
    return {"wkt": u.asWkt(9), "crs": crs.authid() or crs.toWkt()}


class Checker:
    """Answers whether features are inside members' areas, the area made ready once per layer."""

    def __init__(self):
        self._cache = {}

    def _engine(self, area, layer):
        k = (json.dumps(area, sort_keys=True), layer.id())
        e = self._cache.get(k)
        if e is None:
            g = QgsGeometry.fromWkt(area["wkt"])
            src = QgsCoordinateReferenceSystem(area["crs"])
            if src.isValid() and layer.crs().isValid() and src != layer.crs():
                g.transform(QgsCoordinateTransform(src, layer.crs(), QgsProject.instance()))
            tol = TOLERANCE_M / 111320.0 if layer.crs().isGeographic() else TOLERANCE_M
            g = g.buffer(tol, 8)
            e = QgsGeometry.createGeometryEngine(g.constGet())
            e.prepareGeometry()
            if len(self._cache) > 64:
                self._cache.clear()
            self._cache[k] = e = (e, g)
        return e

    def inside(self, area, layer, state):
        """Is the feature of this state ({"g": WKB hex}) inside the area?"""
        if state is None:
            return True
        hexwkb = state.get("g")
        if not hexwkb:
            return False
        g = QgsGeometry()
        g.fromWkb(bytes.fromhex(hexwkb))
        if g.isNull() or g.isEmpty():
            return False
        engine, _shape = self._engine(area, layer)
        return engine.contains(g.constGet())

    def shape(self, area, layer):
        return QgsGeometry(self._engine(area, layer)[1])
