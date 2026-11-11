"""Where the astrolabe's commands get their features (``LayerSource``) and where their results go (``commit``).

Everything is worked out in one CRS in metres: the project's when it is projected in metres, else the UTM zone
under the middle of the map - so "15" on the outer ring is 15 m whatever the layers are stored in.
"""
from collections import Counter

from qgis.core import (Qgis, QgsCoordinateReferenceSystem, QgsCoordinateTransform, QgsDistanceArea, QgsFeature,
                       QgsFeatureRequest, QgsField, QgsGeometry, QgsPalLayerSettings, QgsProject, QgsTextFormat,
                       QgsVectorLayer, QgsVectorLayerSimpleLabeling, QgsVectorLayerUtils, QgsWkbTypes)
from qgis.PyQt.QtCore import QMetaType

from .engine import LINE, POINT, POLYGON, Item, stepping_stone

PREVIEW_CAP = 1500     # features of one target worked out for the preview (the command itself runs on all)
MAX_LAYERS = 5         # layers offered on the rings (the current layer first)


def work_crs(project, canvas):
    crs = project.crs()
    if crs.isValid() and not crs.isGeographic() and crs.mapUnits() == Qgis.DistanceUnit.Meters:
        return crs
    wgs = QgsCoordinateReferenceSystem("EPSG:4326")
    try:
        src = canvas.mapSettings().destinationCrs()
        c = canvas.extent().center()
        if src.isValid() and src != wgs:
            c = QgsCoordinateTransform(src, wgs, project).transform(c)
        lon, lat = c.x(), c.y()
    except Exception:  # noqa: BLE001 - an empty map
        lon, lat = 0.0, 0.0
    if not (-180 <= lon <= 180 and -84 <= lat <= 84):
        lon, lat = 0.0, 0.0
    zone = int((lon + 180) // 6) % 60 + 1
    return QgsCoordinateReferenceSystem(f"EPSG:{(32600 if lat >= 0 else 32700) + zone}")


class LayerSource:
    """The features behind a ring's key: "sel" (what is selected, in any layer), "L:<layer id>" (a layer),
    "view" (what the map shows of the current layer). With ``cap``, a large layer gives only what is in view,
    up to the cap (``capped`` then says so)."""

    def __init__(self, canvas, active=None, project=None, cap=None):
        self.canvas = canvas
        self.project = project or QgsProject.instance()
        self.cap = cap
        self.crs = work_crs(self.project, canvas)
        self.active = active if self._usable(active) else next(iter(self.layers()), None)
        self._cache = {}
        self._ct = {}
        self.capped = {}
        self._da = QgsDistanceArea()
        self._da.setSourceCrs(self.crs, self.project.transformContext())
        self._da.setEllipsoid(self.project.ellipsoid() or "WGS84")

    @staticmethod
    def _usable(lyr):
        return isinstance(lyr, QgsVectorLayer) and lyr.isValid() and lyr.isSpatial()

    def layers(self):
        """The layers on the rings: the current one first, then the others the map shows."""
        root = self.project.layerTreeRoot()
        out = []
        for node in root.findLayers():
            lyr = node.layer()
            if self._usable(lyr) and node.isVisible() and lyr not in out:
                out.append(lyr)
        active = getattr(self, "active", None)
        if active is not None:
            out = [active] + [x for x in out if x is not active]
        return out[:MAX_LAYERS]

    def layer(self, key):
        if key == "view":
            return self.active
        if key and key.startswith("L:"):
            lyr = self.project.mapLayer(key[2:])
            return lyr if self._usable(lyr) else None
        return None

    def layer_id(self, key):
        lyr = self.layer(key)
        return lyr.id() if lyr is not None else None

    def invalidate(self):
        self._cache.clear()
        self.capped.clear()

    # ---- features ------------------------------------------------------------------------------------------------
    def _transform(self, lyr):
        ct = self._ct.get(lyr.id())
        if ct is None:
            ct = self._ct[lyr.id()] = QgsCoordinateTransform(lyr.crs(), self.crs, self.project)
        return ct

    def _view_rect(self, lyr):
        ct = QgsCoordinateTransform(self.canvas.mapSettings().destinationCrs(), lyr.crs(), self.project)
        try:
            return ct.transformBoundingBox(self.canvas.extent())
        except Exception:  # noqa: BLE001 - the view does not fit the layer's CRS
            return None

    def _fetch(self, lyr, request, limit=None):
        ct, out = self._transform(lyr), []
        for f in lyr.getFeatures(request):
            g = f.geometry()
            if g is None or g.isNull() or g.isEmpty():
                continue
            g = QgsGeometry(g)
            if not ct.isShortCircuited():
                try:
                    g.transform(ct)
                except Exception:  # noqa: BLE001 - a shape outside the CRS's world
                    continue
            out.append(Item(lyr.id(), f.id(), g, f.attributes()))
            if limit and len(out) >= limit:
                break
        return out

    def items(self, key):
        if key in self._cache:
            return self._cache[key]
        out = []
        if key == "sel":
            for lyr in self.project.mapLayers().values():
                if self._usable(lyr) and lyr.selectedFeatureCount():
                    left = self.cap - len(out) if self.cap else None
                    if self.cap and left <= 0:
                        self.capped[key] = True
                        break
                    out += self._fetch(lyr, QgsFeatureRequest().setFilterFids(lyr.selectedFeatureIds()), left)
        else:
            lyr = self.layer(key)
            if lyr is not None:
                request = QgsFeatureRequest()
                big = self.cap and lyr.featureCount() > self.cap
                if key == "view" or big:
                    rect = self._view_rect(lyr)
                    if rect is not None:
                        request.setFilterRect(rect)
                out = self._fetch(lyr, request, self.cap)
                if big:
                    self.capped[key] = True
        self._cache[key] = out
        return out

    def count(self, key):
        if key == "sel":
            return sum(lyr.selectedFeatureCount() for lyr in self.project.mapLayers().values() if self._usable(lyr))
        lyr = self.layer(key)
        if lyr is None:
            return 0
        if key == "view":
            return len(self.items(key))
        n = lyr.featureCount()
        return n if n >= 0 else len(self.items(key))

    # ---- measures (square metres, metres) -----------------------------------------------------------------------
    def area(self, geom):
        try:
            return self._da.convertAreaMeasurement(self._da.measureArea(geom), Qgis.AreaUnit.SquareMeters)
        except Exception:  # noqa: BLE001
            return geom.area()

    def length(self, geom):
        try:
            return self._da.convertLengthMeasurement(self._da.measureLength(geom), Qgis.DistanceUnit.Meters)
        except Exception:  # noqa: BLE001
            return geom.length()


class CommitError(Exception):
    def __init__(self, code, name=""):
        super().__init__(code)
        self.code, self.name = code, name


def _memory_layer(items, name, source):
    kind = Counter(i.geom.type() for i in items).most_common(1)[0][0]
    items = [i for i in items if i.geom.type() == kind]
    multi = kind != POINT or any(i.geom.isMultipart() for i in items)
    wkb = {POINT: "Point", LINE: "LineString", POLYGON: "Polygon"}[kind]
    lyr = QgsVectorLayer(("Multi" if multi else "") + wkb, name, "memory")
    lyr.setCrs(source.crs)
    pr = lyr.dataProvider()
    origins = {i.layer for i in items}
    src = source.project.mapLayer(next(iter(origins))) if len(origins) == 1 and None not in origins else None
    keep = src is not None and all(i.attrs is not None for i in items)
    if keep:   # the features come from one layer: they keep its fields
        pr.addAttributes(src.fields().toList())
        lyr.updateFields()
    feats = []
    for i in items:
        f = QgsFeature(lyr.fields())
        g = QgsGeometry(i.geom)
        if multi and not g.isMultipart():
            g.convertToMultiType()
        f.setGeometry(g)
        if keep:
            f.setAttributes(list(i.attrs)[:lyr.fields().count()])
        feats.append(f)
    pr.addFeatures(feats)
    lyr.updateExtents()
    return lyr


def _labels_layer(labels, name, source):
    lyr = QgsVectorLayer("Point", name, "memory")
    lyr.setCrs(source.crs)
    pr = lyr.dataProvider()
    pr.addAttributes([QgsField("value", QMetaType.Type.Double), QgsField("text", QMetaType.Type.QString)])
    lyr.updateFields()
    feats = []
    for at, value, text in labels:
        f = QgsFeature(lyr.fields())
        f.setGeometry(QgsGeometry.fromPointXY(at))
        f.setAttributes([value, text])
        feats.append(f)
    pr.addFeatures(feats)
    lyr.updateExtents()
    pal = QgsPalLayerSettings()
    pal.fieldName = "text"
    fmt = QgsTextFormat()
    fmt.setSize(9)
    fmt.buffer().setEnabled(True)
    pal.setFormat(fmt)
    lyr.setLabeling(QgsVectorLayerSimpleLabeling(pal))
    lyr.setLabelsEnabled(True)
    sym = lyr.renderer().symbol()
    sym.setSize(1.2)
    return lyr


def commit(steps, outs, source, name_of, text_of, command=""):
    """Carry a worked-out command into the project: new layers for what the steps made, edits in the layers
    whose features were changed in place (left in the layer's edit session, one undo step), the selection, and
    a labelled layer for measures. Nothing is written when a layer that must change cannot be edited.

    ``name_of(step)`` names a new layer, ``text_of(at, value, kind, step)`` writes a measure.
    -> {"layers": [...], "edited": [layers], "changed": n, "selected": n or None, "total": (sum, kind) or None}
    """
    project = source.project
    edits = {}
    for o in outs:
        for i in o.changed:
            edits.setdefault(i.layer, {"changed": {}, "added": []})["changed"][i.fid] = i
        for i in o.added:
            edits.setdefault(i.layer, {"changed": {}, "added": []})["added"].append(i)
    todo = []
    for lid, e in edits.items():
        lyr = project.mapLayer(lid)
        if lyr is None:
            raise CommitError("readonly", "?")
        if not lyr.isEditable() and (lyr.readOnly() or not lyr.startEditing()):
            raise CommitError("readonly", lyr.name())
        todo.append((lyr, e))
    result = {"layers": [], "edited": [], "changed": 0, "selected": None, "total": None}
    for lyr, e in todo:
        back = QgsCoordinateTransform(source.crs, lyr.crs(), project)
        multi = QgsWkbTypes.isMultiType(lyr.wkbType())
        keys = set(lyr.primaryKeyAttributes())

        def fit(geom, back=back, multi=multi):
            g = QgsGeometry(geom)
            if not back.isShortCircuited():
                g.transform(back)
            if multi and not g.isMultipart():
                g.convertToMultiType()
            return g
        lyr.beginEditCommand(command or "Astrolabe")
        for fid, item in e["changed"].items():
            lyr.changeGeometry(fid, fit(item.geom))
            result["changed"] += 1
        for item in e["added"]:
            attrs = {n: v for n, v in enumerate(item.attrs or []) if n not in keys}
            lyr.addFeature(QgsVectorLayerUtils.createFeature(lyr, fit(item.geom), attrs))
            result["changed"] += 1
        lyr.endEditCommand()
        lyr.triggerRepaint()
        result["edited"].append(lyr)
    for i, (st, o) in enumerate(zip(steps, outs)):
        if o.made and o.last and not stepping_stone(steps, outs, i):
            lyr = _memory_layer(o.last, name_of(st), source)
            project.addMapLayer(lyr)
            result["layers"].append(lyr)
        if o.sel is not None:
            found = {}
            for item in o.sel:
                found.setdefault(item.layer, []).append(item.fid)
            for lid in {x.layer for x in o.sel} | set(o.target_layers):
                lyr = project.mapLayer(lid)
                if lyr is not None:
                    lyr.selectByIds(found.get(lid, []))
            result["selected"] = len(o.sel)
        if st["v"] == "measure" and o.labels:
            lyr = _labels_layer([(at, v, text_of(at, v, kind, st)) for at, v, kind in o.labels], name_of(st), source)
            project.addMapLayer(lyr)
            result["layers"].append(lyr)
            values = [(v, kind) for _at, v, kind in o.labels if v is not None]
            if values and len({k for _v, k in values}) == 1:
                result["total"] = (sum(v for v, _k in values), values[0][1])
    return result
