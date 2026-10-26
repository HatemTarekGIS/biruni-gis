"""'Check my data': one pass over every layer of the project that lists the
problems people actually run into - broken or temporary layers, wrong or
missing coordinate systems, invalid / empty / duplicate geometries, slivers,
overlaps, empty fields, data outside the project folder, shapefiles without
an encoding.

The scan only reads. Fixes are offered where they are safe: they run inside
an edit session as one undoable command and leave the layer in edit mode so
the user reviews and saves (or undoes) them.
"""
import math
import os

from qgis.core import (Qgis, QgsCoordinateReferenceSystem, QgsCoordinateTransform, QgsFeatureRequest, QgsGeometry,
                       QgsProject, QgsProviderRegistry, QgsRasterLayer, QgsRectangle,
                       QgsSpatialIndex, QgsVectorLayer, QgsWkbTypes)

from qgis.PyQt.QtCore import QMetaType

from ..algorithms.base import polygonal
from ..i18n import tr
from . import topology as topo

# (English message, kind). Messages are formatted with {n} (count) and the
# extra values of the issue.
TITLES = [
    ("The layer's data can't be found or opened.", "broken"),
    ("The layer has no coordinate system - it can't be placed on the map correctly.", "no_crs"),
    ("The layer's coordinate system ({crs}) differs from the project's ({project}). It is shown reprojected; "
     "use the same one before analysis.", "crs_mismatch"),
    ("Temporary layer - it will be lost when the project is closed.", "temporary"),
    ("Stored outside the project folder: {path}", "outside_folder"),
    ("{n} features have no geometry.", "empty_geometry"),
    ("{n} features have an invalid geometry (self-intersections, bad rings).", "invalid"),
    ("{n} features are exact copies of another feature's geometry.", "duplicate_geometry"),
    ("{n} features have repeated vertices.", "duplicate_vertices"),
    ("{n} features lie outside the valid range of latitude / longitude.", "out_of_range"),
    ("The coordinates look swapped (latitude and longitude).", "swapped"),
    ("The data lies outside the area its coordinate system is meant for - the coordinate system is "
     "probably wrong.", "outside_crs_area"),
    ("{n} thin sliver polygons (tiny and long).", "sliver"),
    ("{n} polygons overlap another polygon.", "overlap"),
    ("Empty fields (no value in any feature): {fields}", "empty_fields"),
    ("Shapefile without an encoding (.cpg) file - Arabic and other non-Latin text may show wrong characters.",
     "no_cpg"),
    ("Only the first {n} features were checked.", "sampled"),
]
MESSAGES = {kind: msg for msg, kind in TITLES}
SEVERITY_ORDER = {"error": 0, "warning": 1, "info": 2}

MAX_FEATURES = 300000      # geometry checks stop after this many features per layer
MAX_OVERLAP_POLYGONS = 50000
MAX_OVERLAP_CHECKS = 400000
SLIVER_THINNESS = 0.05     # 4·pi·area / perimeter² (1 = circle)
SLIVER_AREA_SHARE = 0.01   # ... and smaller than 1 % of the layer's median polygon


class Issue:
    def __init__(self, severity, layer, kind, count=1, fids=None, fix=None, fix_label="", **values):
        self.severity = severity
        self.layer_id = layer.id() if layer is not None else ""
        self.layer_name = layer.name() if layer is not None else ""
        self.kind = kind
        self.count = count
        self.fids = list(fids or [])
        self.fix = fix              # callable() -> number of features fixed
        self.fix_label = fix_label  # already translated
        self.values = values

    @property
    def message(self):
        return MESSAGES[self.kind]

    def text(self):
        return tr(self.message).format(n=self.count, **self.values)

    def layer(self):
        return QgsProject.instance().mapLayer(self.layer_id)

    def __repr__(self):
        return f"Issue({self.severity}, {self.layer_name}, {self.kind}, {self.count})"


# ---- fixes ---------------------------------------------------------------
def _edit_geometries(layer, fids, change, label):
    """Apply ``change(geometry) -> geometry or None`` to ``fids`` as one undoable edit."""
    if not (layer.dataProvider().capabilities() & Qgis.VectorProviderCapability.ChangeGeometries):
        return 0
    started = not layer.isEditable()
    if started and not layer.startEditing():
        return 0
    layer.beginEditCommand(label)
    done = 0
    for f in layer.getFeatures(QgsFeatureRequest().setFilterFids(list(fids)).setNoAttributes()):
        g = change(QgsGeometry(f.geometry()))
        if g is not None and layer.changeGeometry(f.id(), g):
            done += 1
    if done:
        layer.endEditCommand()
    else:
        layer.destroyEditCommand()
        if started:
            layer.rollBack()
    layer.triggerRepaint()
    return done


def _make_valid(layer):
    gtype = layer.geometryType()
    multi = QgsWkbTypes.isMultiType(layer.wkbType())

    def change(g):
        v = g.makeValid()
        if gtype == Qgis.GeometryType.Polygon:
            v = polygonal(v)
        elif v is not None and v.type() != gtype:
            parts = [p for p in v.asGeometryCollection() if p.type() == gtype]
            v = QgsGeometry.collectGeometry(parts) if parts else None
        if v is None or v.isEmpty():
            return None
        if not multi and v.isMultipart():
            parts = v.asGeometryCollection()
            if len(parts) != 1:
                return None  # would need a multi-part layer; leave it for the user
            v = parts[0]
        elif multi:
            v.convertToMultiType()
        return v if v.isGeosValid() else None
    return change


def _dedupe_vertices(g):
    return g if g.removeDuplicateNodes(1e-8) else None


def fix_invalid(layer, fids):
    return lambda: _edit_geometries(layer, fids, _make_valid(layer), tr("Fix invalid geometries"))


def fix_duplicate_vertices(layer, fids):
    return lambda: _edit_geometries(layer, fids, _dedupe_vertices, tr("Remove repeated vertices"))


# ---- helpers ---------------------------------------------------------------
def layer_path(layer):
    """Local file behind a layer, or ''."""
    try:
        parts = QgsProviderRegistry.instance().decodeUri(layer.providerType(), layer.source())
    except Exception:
        return ""
    path = parts.get("path") or ""
    return path if path and os.path.exists(path) else ""


def _inside(path, folder):
    try:
        return os.path.commonpath([os.path.normcase(os.path.abspath(path)),
                                   os.path.normcase(os.path.abspath(folder))]) == \
            os.path.normcase(os.path.abspath(folder))
    except ValueError:
        return False


def _crs_area(crs, project):
    """The CRS's area of use in the CRS's own coordinates, or None."""
    try:
        bounds = crs.bounds()
        if bounds.isEmpty():
            return None
        if crs.isGeographic():
            return bounds
        tr4326 = QgsCoordinateTransform(QgsCoordinateReferenceSystem("EPSG:4326"), crs, project)
        return tr4326.transformBoundingBox(bounds)
    except Exception:
        return None


def _expand(rect, share):
    r = QgsRectangle(rect)
    dx, dy = rect.width() * share, rect.height() * share
    r.setXMinimum(r.xMinimum() - dx)
    r.setXMaximum(r.xMaximum() + dx)
    r.setYMinimum(r.yMinimum() - dy)
    r.setYMaximum(r.yMaximum() + dy)
    return r


# ---- the scan ------------------------------------------------------------
def scan_layer(layer, project, feedback=None, start=0.0, span=100.0):
    issues = []
    if not layer.isValid():
        return [Issue("error", layer, "broken")]

    if not layer.crs().isValid():
        issues.append(Issue("error", layer, "no_crs"))
    elif project.crs().isValid() and layer.crs() != project.crs():
        issues.append(Issue("info", layer, "crs_mismatch", crs=layer.crs().userFriendlyIdentifier(),
                            project=project.crs().userFriendlyIdentifier()))

    if layer.providerType() == "memory":
        issues.append(Issue("warning", layer, "temporary"))
    path = layer_path(layer)
    home = project.absolutePath()
    if path and project.fileName() and home and not _inside(path, home):
        issues.append(Issue("warning", layer, "outside_folder", path=path))
    if path.lower().endswith(".shp") and not os.path.exists(os.path.splitext(path)[0] + ".cpg") \
            and isinstance(layer, QgsVectorLayer) \
            and any(f.type() == QMetaType.Type.QString for f in layer.fields()):
        issues.append(Issue("warning", layer, "no_cpg"))

    if isinstance(layer, QgsVectorLayer):
        issues += _scan_features(layer, project, feedback, start, span)
    return issues


def _scan_features(layer, project, feedback, start, span):
    issues = []
    total = layer.featureCount()
    fields = layer.fields()
    has_geom = layer.isSpatial()
    gtype = layer.geometryType() if has_geom else None
    filled = [False] * fields.count()
    empty, invalid, dup_vertex = [], [], []
    seen, duplicates = {}, []
    geographic = layer.crs().isValid() and layer.crs().isGeographic()
    out_of_range = []
    swapped_votes = 0
    areas = {}
    polys = {}
    thin = []
    extent = QgsRectangle()
    extent.setNull()

    req = QgsFeatureRequest()
    if total > MAX_FEATURES:
        req.setLimit(MAX_FEATURES)
    if not has_geom:
        req.setFlags(Qgis.FeatureRequestFlag.NoGeometry)
    for i, f in enumerate(layer.getFeatures(req)):
        if feedback is not None and i % 1000 == 0:
            if feedback.isCanceled():
                return issues
            feedback.setProgress(start + span * 0.8 * i / max(min(total, MAX_FEATURES), 1))
        attrs = f.attributes()
        for k, v in enumerate(attrs):
            if not filled[k] and v is not None and not (isinstance(v, str) and not v.strip()) \
                    and not (hasattr(v, "isNull") and v.isNull()):
                filled[k] = True
        if not has_geom:
            continue
        g = f.geometry()
        if g is None or g.isNull() or g.isEmpty():
            empty.append(f.id())
            continue
        bb = g.boundingBox()
        extent.combineExtentWith(bb)
        bad = gtype != Qgis.GeometryType.Point and not g.isGeosValid()
        if bad:
            invalid.append(f.id())
        elif gtype != Qgis.GeometryType.Point:
            if QgsGeometry(g).removeDuplicateNodes(1e-8):
                dup_vertex.append(f.id())
        n = QgsGeometry(g)
        n.normalize()
        key = bytes(n.asWkb())
        if key in seen:
            duplicates.append(f.id())
        else:
            seen[key] = f.id()
        if geographic:
            if bb.xMinimum() < -180 or bb.xMaximum() > 180 or bb.yMinimum() < -90 or bb.yMaximum() > 90:
                out_of_range.append(f.id())
                if -90 <= bb.xMinimum() and bb.xMaximum() <= 90 and -180 <= bb.yMinimum() and bb.yMaximum() <= 180:
                    swapped_votes += 1
        if gtype == Qgis.GeometryType.Polygon and not bad:
            area = g.area()
            areas[f.id()] = area
            per = g.length()
            if per > 0 and 4 * math.pi * area / (per * per) < SLIVER_THINNESS:
                thin.append(f.id())
            if len(polys) < MAX_OVERLAP_POLYGONS:
                polys[f.id()] = g
    seen.clear()

    def add(sev, kind, fids, fix=None, fix_label=""):
        if fids:
            issues.append(Issue(sev, layer, kind, len(fids), fids, fix, fix_label))

    editable = bool(layer.dataProvider().capabilities() & Qgis.VectorProviderCapability.ChangeGeometries)
    add("error", "empty_geometry", empty)
    add("error", "invalid", invalid, fix_invalid(layer, invalid) if editable else None, tr("Fix geometries"))
    add("warning", "duplicate_geometry", duplicates)
    add("info", "duplicate_vertices", dup_vertex,
        fix_duplicate_vertices(layer, dup_vertex) if editable else None, tr("Remove repeated vertices"))
    if out_of_range:
        add("error", "out_of_range", out_of_range)
        if swapped_votes >= max(1, len(out_of_range) // 2):
            issues.append(Issue("error", layer, "swapped", len(out_of_range), out_of_range))
    elif not extent.isNull() and layer.crs().isValid():
        area = _crs_area(layer.crs(), project)
        # degrees typed into a projected layer are the classic mistake; otherwise
        # allow data well beyond the official area (zones are often stretched)
        degree_like = not geographic and QgsRectangle(-180, -90, 180, 90).contains(extent)
        if area is not None and ((degree_like and not area.intersects(extent))
                                 or not _expand(area, 0.5).intersects(extent)):
            issues.append(Issue("warning", layer, "outside_crs_area"))

    if thin and areas:
        med = sorted(areas.values())[len(areas) // 2]
        slivers = [fid for fid in thin if areas[fid] < SLIVER_AREA_SHARE * med]
        add("warning", "sliver", slivers)

    if len(polys) > 1:
        index = QgsSpatialIndex()
        for fid, g in polys.items():
            index.addFeature(fid, g.boundingBox())
        if feedback is not None:
            feedback.setProgress(start + span * 0.85)
        pairs = topo.find_overlaps(polys, index, 0.0, feedback, MAX_OVERLAP_CHECKS, relative=1e-6)
        fids = sorted({a for a, _b, _g in pairs} | {b for _a, b, _g in pairs})
        add("warning", "overlap", fids)

    empty_fields = [fields.at(k).name() for k in range(fields.count()) if not filled[k]]
    if empty_fields and total:
        issues.append(Issue("info", layer, "empty_fields", len(empty_fields), fields=", ".join(empty_fields)))
    if total > MAX_FEATURES:
        issues.append(Issue("info", layer, "sampled", MAX_FEATURES))
    return issues


def scan_project(project=None, feedback=None, layers=None):
    """Every problem found in the project's layers (or ``layers``), worst first."""
    project = project or QgsProject.instance()
    if layers is None:
        layers = [tl.layer() for tl in project.layerTreeRoot().findLayers() if tl.layer() is not None]
        known = {lyr.id() for lyr in layers}
        layers += [lyr for lyr in project.mapLayers().values() if lyr.id() not in known]
    layers = [lyr for lyr in layers if isinstance(lyr, (QgsVectorLayer, QgsRasterLayer)) or not lyr.isValid()]
    issues = []
    n = max(len(layers), 1)
    for i, layer in enumerate(layers):
        if feedback is not None and feedback.isCanceled():
            break
        issues += scan_layer(layer, project, feedback, 100.0 * i / n, 100.0 / n)
    if feedback is not None:
        feedback.setProgress(100)
    order = {lyr.id(): k for k, lyr in enumerate(layers)}
    issues.sort(key=lambda it: (order.get(it.layer_id, 0), SEVERITY_ORDER[it.severity]))
    return issues


def summary(issues):
    out = {"error": 0, "warning": 0, "info": 0}
    for it in issues:
        out[it.severity] += 1
    return out
