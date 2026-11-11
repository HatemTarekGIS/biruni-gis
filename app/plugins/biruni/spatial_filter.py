"""Show only what is inside an area: filters vector layers to the features that intersect the selected
polygon(s), the current map extent or a given geometry - without touching the data, and reversibly.

Every filtered layer keeps its own filter (subset string) underneath: the area is ANDed to it and
clear() puts back exactly what was there. The state is kept in the project, so a reopened project
still knows which layers are filtered and offers to show everything again."""
import json

from qgis.core import (Qgis, QgsCoordinateReferenceSystem, QgsCoordinateTransform, QgsFeatureRequest, QgsGeometry,
                       QgsProject, QgsVectorLayer)

from .i18n import tr

SCOPE, KEY = "biruni", "spatial_filter"
SQL_PROVIDERS = {"postgres", "spatialite", "mssql", "oracle", "hana", "db2"}
EXPRESSION_PROVIDERS = {"memory", "delimitedtext"}
BIG = 100_000  # above this many features (in all layers) a progress dialog is shown


# ---- state ----------------------------------------------------------------------------------------------------

def _state(project=None):
    project = project or QgsProject.instance()
    text, ok = project.readEntry(SCOPE, KEY, "")
    if not ok or not text:
        return {}
    try:
        return json.loads(text)
    except ValueError:
        return {}


def _save_state(state, project=None):
    project = project or QgsProject.instance()
    if state:
        project.writeEntry(SCOPE, KEY, json.dumps(state, ensure_ascii=False))
    else:
        project.removeEntry(SCOPE, KEY)


def is_active(project=None):
    return bool(_state(project).get("layers"))


def filtered_layer_ids(project=None):
    return list(_state(project).get("layers", {}))


# ---- the area -------------------------------------------------------------------------------------------------

def selected_polygons(layer):
    """Union of the selected polygons of a layer, or None."""
    if not isinstance(layer, QgsVectorLayer) or layer.geometryType() != Qgis.GeometryType.Polygon:
        return None
    geoms = [f.geometry() for f in layer.getSelectedFeatures() if f.hasGeometry()]
    if not geoms:
        return None
    g = QgsGeometry.unaryUnion(geoms)
    return g if g and not g.isEmpty() else None


def area_from_map(iface):
    """(geometry, crs, layer id that gave it) from the selected polygons of the active layer, else the map
    extent."""
    layer = iface.activeLayer()
    g = selected_polygons(layer)
    if g is not None:
        return g, layer.crs(), layer.id()
    canvas = iface.mapCanvas()
    return QgsGeometry.fromRect(canvas.extent()), canvas.mapSettings().destinationCrs(), None


# ---- filters --------------------------------------------------------------------------------------------------

def _quote(value):
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return str(int(value)) if float(value).is_integer() else repr(value)
    return "'" + str(value).replace("'", "''") + "'"


def filter_kind(layer):
    """How the layer can be filtered: 'expression', 'ogr', 'sql' or None."""
    p = layer.providerType()
    if p in EXPRESSION_PROVIDERS:
        return "expression"
    if p == "ogr":
        if layer.subsetString().strip().lower().startswith("select"):
            return None  # a full OGR SQL statement: leave it alone
        return "ogr"
    if p in SQL_PROVIDERS and len(layer.dataProvider().pkAttributeIndexes()) == 1:
        return "sql"
    return None


def id_clause(layer, kind, ids, keys=None):
    """The subset clause that keeps the features `ids` (or primary-key values `keys`)."""
    if kind == "expression":
        return "$id IN ({})".format(",".join(str(i) for i in ids) or "-1")
    if kind == "ogr":
        pk = layer.dataProvider().pkAttributeIndexes()
        col = '"{}"'.format(layer.fields()[pk[0]].name().replace('"', '""')) if len(pk) == 1 else "FID"
        return "{} IN ({})".format(col, ",".join(str(i) for i in ids) or "-1")
    name = layer.fields()[layer.dataProvider().pkAttributeIndexes()[0]].name().replace('"', '""')
    vals = ",".join(_quote(k) for k in keys) if keys else "NULL"
    return f'"{name}" IN ({vals})'


def combine(old, clause):
    old = (old or "").strip()
    return f"({old}) AND ({clause})" if old else clause


def matching(layer, geometry, crs, transform_context, progress=None):
    """(feature ids, primary-key values) of the features of `layer` that intersect `geometry`."""
    g = QgsGeometry(geometry)
    if crs.isValid() and layer.crs().isValid() and crs != layer.crs():
        g.transform(QgsCoordinateTransform(crs, layer.crs(), transform_context))
    engine = QgsGeometry.createGeometryEngine(g.constGet())
    engine.prepareGeometry()
    pk = layer.dataProvider().pkAttributeIndexes() if layer.providerType() in SQL_PROVIDERS else []
    req = QgsFeatureRequest().setFilterRect(g.boundingBox())
    req.setSubsetOfAttributes(pk[:1])
    ids, keys = [], []
    for n, f in enumerate(layer.getFeatures(req)):
        if progress is not None and n % 2000 == 0 and not progress():
            return None, None
        if f.hasGeometry() and engine.intersects(f.geometry().constGet()):
            ids.append(f.id())
            if pk:
                keys.append(f.attributes()[pk[0]])
    return ids, keys


def apply_to_layers(layers, geometry, crs, project=None, exclude=(), progress=None):
    """Filter `layers` to `geometry` (in `crs`). Returns {'filtered': {layer id: count}, 'skipped': [(name,
    reason)]}. A filter already in place is cleared first."""
    project = project or QgsProject.instance()
    if is_active(project):
        clear_layers(project)
    state = {"layers": {}, "wkt": geometry.asWkt(), "crs": crs.toWkt() if crs.isValid() else ""}
    result = {"filtered": {}, "skipped": []}
    for layer in layers:
        if not isinstance(layer, QgsVectorLayer) or layer.id() in exclude or not layer.isValid() \
                or not layer.isSpatial():
            continue
        if layer.isEditable():
            result["skipped"].append((layer.name(), tr("being edited")))
            continue
        kind = filter_kind(layer)
        if kind is None:
            result["skipped"].append((layer.name(), tr("this kind of layer cannot be filtered")))
            continue
        ids, keys = matching(layer, geometry, crs, project.transformContext(), progress)
        if ids is None:  # cancelled: put everything back
            _save_state(state, project)
            clear_layers(project)
            return None
        old = layer.subsetString()
        if not layer.setSubsetString(combine(old, id_clause(layer, kind, ids, keys))):
            layer.setSubsetString(old)
            result["skipped"].append((layer.name(), tr("the filter was refused")))
            continue
        state["layers"][layer.id()] = old
        result["filtered"][layer.id()] = len(ids)
    _save_state(state if state["layers"] else {}, project)
    return result


def clear_layers(project=None):
    """Put back the filters the layers had before. Returns how many layers were restored."""
    project = project or QgsProject.instance()
    state = _state(project)
    n = 0
    for lid, old in state.get("layers", {}).items():
        layer = project.mapLayer(lid)
        if layer is None:
            continue
        if layer.isEditable():  # a subset cannot change while editing: keep this one for later
            continue
        layer.setSubsetString(old)
        n += 1
    left = {lid: old for lid, old in state.get("layers", {}).items()
            if project.mapLayer(lid) is not None and project.mapLayer(lid).isEditable()}
    _save_state(dict(state, layers=left) if left else {}, project)
    return n


def filter_area(project=None):
    """(geometry, crs) of the filter in place, or (None, None)."""
    state = _state(project)
    if not state.get("wkt"):
        return None, None
    return QgsGeometry.fromWkt(state["wkt"]), QgsCoordinateReferenceSystem.fromWkt(state.get("crs", ""))


# ---- with the interface ---------------------------------------------------------------------------------------

def apply(iface, layers=None, geometry=None, crs=None):
    """Filter the project's vector layers (or `layers`) to the selected polygons / the map extent / `geometry`."""
    project = QgsProject.instance()
    source_id = None
    if geometry is None:
        geometry, crs, source_id = area_from_map(iface)
    if crs is None:
        crs = iface.mapCanvas().mapSettings().destinationCrs()
    if layers is None:
        layers = [lyr for lyr in project.mapLayers().values() if isinstance(lyr, QgsVectorLayer)]
    total = sum(max(lyr.featureCount(), 0) for lyr in layers if isinstance(lyr, QgsVectorLayer))
    dialog = None
    progress = None
    if total > BIG:
        from qgis.PyQt.QtCore import Qt
        from qgis.PyQt.QtWidgets import QApplication, QProgressDialog

        dialog = QProgressDialog(tr("Finding the features inside the area…"), tr("Cancel"), 0, 0,
                                 iface.mainWindow())
        dialog.setWindowModality(Qt.WindowModality.WindowModal)
        dialog.setMinimumDuration(300)

        def progress():
            QApplication.processEvents()
            return not dialog.wasCanceled()

    try:
        result = apply_to_layers(layers, geometry, crs, project, exclude=(source_id,) if source_id else (),
                                 progress=progress)
    finally:
        if dialog is not None:
            dialog.close()
    bar = iface.messageBar()
    if result is None:
        bar.pushMessage(tr("Show only inside the area"), tr("Cancelled: nothing was filtered."),
                        Qgis.MessageLevel.Info, 4)
        return None
    n = len(result["filtered"])
    shown = sum(result["filtered"].values())
    msg = tr("{n} layers show only the {k} features inside the area.").format(n=n, k=shown)
    if result["skipped"]:
        msg += " " + tr("Not filtered: {l}.").format(l=", ".join(f"{a} ({b})" for a, b in result["skipped"]))
    _push_banner(iface, msg, Qgis.MessageLevel.Success if n else Qgis.MessageLevel.Warning)
    iface.mapCanvas().refreshAllLayers()
    return result


def clear(iface):
    n = clear_layers()
    if iface is not None:
        iface.messageBar().pushMessage(tr("Show only inside the area"),
                                       tr("Everything is shown again ({n} layers).").format(n=n),
                                       Qgis.MessageLevel.Info, 4)
        iface.mapCanvas().refreshAllLayers()
    return n


def _push_banner(iface, text, level):
    from qgis.PyQt.QtWidgets import QPushButton

    bar = iface.messageBar()
    item = bar.createMessage(tr("Show only inside the area"), text)
    button = QPushButton(tr("Show everything"))

    def done():
        clear(iface)
        bar.popWidget(item)

    button.clicked.connect(done)
    item.layout().addWidget(button)
    bar.pushWidget(item, level, 0)


def check_on_open(iface):
    """Call after a project is read: if layers were left filtered to an area, say so and offer to clear."""
    if is_active():
        n = len(filtered_layer_ids())
        _push_banner(iface, tr("{n} layers of this project show only the features inside an area.").format(n=n),
                     Qgis.MessageLevel.Info)
