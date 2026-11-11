"""Field photos panel: start, show the QR code, and watch the photos arrive as points."""
import json
import os
import threading
import time

from qgis.core import (Qgis, QgsApplication, QgsCoordinateReferenceSystem, QgsCoordinateTransform,
                       QgsEditorWidgetSetup, QgsFeature, QgsField, QgsFields, QgsGeometry, QgsPointXY,
                       QgsProject, QgsProperty, QgsSettings, QgsSimpleMarkerSymbolLayer, QgsSymbolLayer,
                       QgsVectorFileWriter, QgsVectorLayer)
from qgis.PyQt.QtCore import QMetaType, Qt, QTimer, QUrl
from qgis.PyQt.QtGui import QColor, QDesktopServices, QPixmap
from qgis.PyQt.QtWidgets import (QComboBox, QDockWidget, QFileDialog, QFrame, QHBoxLayout, QLabel, QLineEdit,
                                 QMessageBox, QPushButton, QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget)

from ..i18n import lang, tr

KEY = "biruni/field-photos/"
LAYER = "field_photos"
GPKG = "field_photos.gpkg"
WGS84 = QgsCoordinateReferenceSystem("EPSG:4326")
FIELDS = (("photo_id", QMetaType.Type.QString), ("photo", QMetaType.Type.QString),
          ("taken", QMetaType.Type.QString), ("kind", QMetaType.Type.QString), ("note", QMetaType.Type.QString),
          ("accuracy_m", QMetaType.Type.Double), ("altitude", QMetaType.Type.Double),
          ("compass", QMetaType.Type.Double), ("heading", QMetaType.Type.Double),
          ("fix_age_s", QMetaType.Type.Double), ("device", QMetaType.Type.QString),
          ("avg_n", QMetaType.Type.Int), ("avg_sd_m", QMetaType.Type.Double),
          ("level_id", QMetaType.Type.Int), ("unit_id", QMetaType.Type.QString))
ROOM_URL = "https://hatemtarekgis.github.io/biruni-gis/r/"
TEAM_LAYER = "field_team_track"
TEAM_FIELDS = (("device", QMetaType.Type.QString), ("name", QMetaType.Type.QString),
               ("seen", QMetaType.Type.QString), ("accuracy_m", QMetaType.Type.Double),
               ("heading", QMetaType.Type.Double))
MAX_GUIDE_FEATURES = 200


def default_folder():
    home = QgsProject.instance().homePath()
    if home and os.path.isdir(home):
        return os.path.join(home, "Field photos")
    docs = os.path.join(os.path.expanduser("~"), "Documents")
    return os.path.join(docs if os.path.isdir(docs) else os.path.expanduser("~"), "Biruni GIS", "Field photos")


# ---- the photo layer (GeoPackage next to the photos: written to disk at once) -------
def _fields(spec=FIELDS):
    f = QgsFields()
    for name, t in spec:
        f.append(QgsField(name, t))
    return f


def _gpkg_layer(path, layer, spec, title):
    """A point layer of the field photos GeoPackage (made when missing; columns added later are added here)."""
    uri = f"{path}|layername={layer}"
    for lyr in QgsProject.instance().mapLayers().values():
        if isinstance(lyr, QgsVectorLayer) and os.path.normcase(lyr.source()) == os.path.normcase(uri):
            return lyr, False
    lyr = QgsVectorLayer(uri, title, "ogr") if os.path.exists(path) else None
    if lyr is None or not lyr.isValid():
        opts = QgsVectorFileWriter.SaveVectorOptions()
        opts.driverName = "GPKG"
        opts.layerName = layer
        if os.path.exists(path):
            opts.actionOnExistingFile = QgsVectorFileWriter.ActionOnExistingFile.CreateOrOverwriteLayer
        w = QgsVectorFileWriter.create(path, _fields(spec), Qgis.WkbType.Point, WGS84,
                                       QgsProject.instance().transformContext(), opts)
        err = w.hasError()
        del w
        if err != QgsVectorFileWriter.WriterError.NoError:
            raise OSError(f"cannot create {path}")
        lyr = QgsVectorLayer(uri, title, "ogr")
        if not lyr.isValid():
            raise OSError(f"cannot open {path}")
    missing = [QgsField(n, t) for n, t in spec if lyr.fields().indexOf(n) < 0]
    if missing and lyr.dataProvider().addAttributes(missing):
        lyr.updateFields()
    return lyr, True


COLUMN_TYPES = {"number": QMetaType.Type.Double, "integer": QMetaType.Type.Int, "yesno": QMetaType.Type.Bool}


def form_columns(lyr, form, records=()):
    """Give the layer a column for each question (and for answers of questions removed since),
    with the question as its alias and a matching edit widget on the PC."""
    have = set(lyr.fields().names())
    new = []
    for f in form:
        if f["name"] not in have:
            new.append(QgsField(f["name"], COLUMN_TYPES.get(f["type"], QMetaType.Type.QString)))
            have.add(f["name"])
    for r in records:
        for name in r.get("fields") or ():
            if name not in have:
                new.append(QgsField(name, QMetaType.Type.QString))
                have.add(name)
    if new:
        if not lyr.dataProvider().addAttributes(new):
            raise OSError(lyr.dataProvider().lastError() or "cannot add the form's columns")
        lyr.updateFields()
    for f in form:
        idx = lyr.fields().indexOf(f["name"])
        if idx < 0:
            continue
        lyr.setFieldAlias(idx, f["label"])
        if f["type"] == "choice":
            lyr.setEditorWidgetSetup(idx, QgsEditorWidgetSetup("ValueMap", {"map": [{c: c} for c in f["choices"]]}))
        elif f["type"] == "yesno":
            lyr.setEditorWidgetSetup(idx, QgsEditorWidgetSetup("CheckBox", {}))
        elif f["type"] == "date":
            lyr.setEditorWidgetSetup(idx, QgsEditorWidgetSetup("DateTime", {
                "field_format": "yyyy-MM-dd", "display_format": "yyyy-MM-dd", "calendar_popup": True,
                "allow_null": True}))


def open_layer(folder, add=True):
    """The photos layer of this folder (created when missing), added to the project once."""
    os.makedirs(folder, exist_ok=True)
    lyr, new = _gpkg_layer(os.path.join(folder, GPKG), LAYER, FIELDS, tr("Field photos"))
    if not new:
        return lyr
    style_layer(lyr)
    if add:
        QgsProject.instance().addMapLayer(lyr)
    return lyr


def style_layer(lyr):
    sym = lyr.renderer().symbol() if hasattr(lyr.renderer(), "symbol") else None
    if sym is not None:
        dot = QgsSimpleMarkerSymbolLayer(Qgis.MarkerShape.Circle, 3.2, 0, Qgis.ScaleMethod.ScaleArea,
                                         QColor("#2CC5B4"), QColor("#0B1F1D"))
        dot.setStrokeWidth(0.5)
        arrow = QgsSimpleMarkerSymbolLayer(Qgis.MarkerShape.Triangle, 2.0, 0, Qgis.ScaleMethod.ScaleArea,
                                           QColor("#0B1F1D"), QColor("#2CC5B4"))
        arrow.setOffset(QgsPointXY(0, -2.6).toQPointF())
        arrow.setDataDefinedProperty(QgsSymbolLayer.Property.Angle,
                                     QgsProperty.fromExpression('coalesce("compass", "heading")'))
        arrow.setDataDefinedProperty(QgsSymbolLayer.Property.LayerEnabled,
                                     QgsProperty.fromExpression('coalesce("compass", "heading") IS NOT NULL'))
        sym.deleteSymbolLayer(0)
        sym.appendSymbolLayer(arrow)
        sym.appendSymbolLayer(dot)
    idx = lyr.fields().indexOf("photo")
    if idx >= 0:  # the form shows the photo itself
        lyr.setEditorWidgetSetup(idx, QgsEditorWidgetSetup("ExternalResource", {
            "DocumentViewer": 1, "DocumentViewerHeight": 320, "FileWidget": True, "FileWidgetButton": True,
            "RelativeStorage": 0, "StorageMode": 0}))
    lyr.setDisplayExpression('coalesce("kind", \'\') || \' \' || "taken"')
    lyr.setMapTipTemplate('<b>[% "kind" %]</b> [% "taken" %]<br>[% "note" %]<br>'
                          '<img src="file:///[% replace("photo", \'\\\\\', \'/\') %]" width="320">')


def add_records(lyr, records):
    """Add the records whose photo_id is not in the layer yet; returns the features added."""
    have = {f["photo_id"] for f in lyr.getFeatures()} if records else set()
    feats = []
    for r in records:
        if r["id"] in have:
            continue
        f = QgsFeature(lyr.fields())
        f.setGeometry(QgsGeometry.fromPointXY(QgsPointXY(r["lon"], r["lat"])))
        for name, value in (("photo_id", r["id"]), ("photo", r["photo"]), ("taken", r.get("taken")),
                            ("kind", r.get("kind") or None), ("note", r.get("note") or None),
                            ("accuracy_m", r.get("acc")), ("altitude", r.get("alt")),
                            ("compass", r.get("compass")), ("heading", r.get("heading")),
                            ("fix_age_s", r.get("fix_age")), ("device", r.get("device") or None),
                            ("avg_n", r.get("avg_n")), ("avg_sd_m", r.get("avg_sd")),
                            ("level_id", r.get("level")), ("unit_id", r.get("unit") or None)):
            f[name] = value
        for name, value in (r.get("fields") or {}).items():
            if f.fields().indexOf(name) >= 0:
                f[name] = value
        feats.append(f)
        have.add(r["id"])
    if feats:
        # straight to the file: not part of an edit session, nothing to save, safe from power cuts
        ok, added = lyr.dataProvider().addFeatures(feats)
        if not ok:
            raise OSError(lyr.dataProvider().lastError() or "write failed")
        lyr.updateExtents()
        lyr.triggerRepaint()
        from .. import team

        team.notify_added(lyr, added)  # a layer shared in a team room: the others get the phone's points too
        return added
    return []


# ---- the team: where each phone is (live), and the way it went (kept) ------------------------------
# phones in one place often send the very same position (the location comes from the same Wi-Fi), and the map
# then drew only one of the names: phones on the same spot share one label (the "label" field, written by
# update_team; an expression gathering the names while drawing hung the map)
TEAM_LABEL = 'if("label" is null, coalesce(nullif("name", \'\'), \'•\'), "label")'


def team_layers(folder):
    """(live, track): the live layer is in memory (one point per phone), the track is in the GeoPackage."""
    from qgis.core import QgsPalLayerSettings, QgsTextFormat, QgsVectorLayerSimpleLabeling

    track, new = _gpkg_layer(os.path.join(folder, GPKG), TEAM_LAYER, TEAM_FIELDS, tr("Field team - tracks"))
    if new:
        sym = track.renderer().symbol()
        sym.setColor(QColor("#7FA7E8"))
        sym.setSize(1.4)
        QgsProject.instance().addMapLayer(track)
    live = None
    for lyr in QgsProject.instance().mapLayers().values():
        if lyr.customProperty("biruni/field-team") == folder:
            live = lyr
            lab = lyr.labeling()
            if lab is not None and lab.settings().fieldName == 'coalesce(nullif("name", \'\'), \'•\')':  # older maps
                pal = QgsPalLayerSettings(lab.settings())
                pal.fieldName = TEAM_LABEL
                lyr.setLabeling(QgsVectorLayerSimpleLabeling(pal))
            if lyr.fields().indexOf("label") < 0:
                lyr.dataProvider().addAttributes([QgsField("label", QMetaType.Type.QString)])
                lyr.updateFields()
    if live is None:
        live = QgsVectorLayer("Point?crs=EPSG:4326&field=device:string&field=name:string&field=seen:string"
                              "&field=accuracy_m:double&field=heading:double&field=label:string",
                              tr("Field team (live)"), "memory")
        live.setCustomProperty("biruni/field-team", folder)
        dot = QgsSimpleMarkerSymbolLayer(Qgis.MarkerShape.Circle, 4.2, 0, Qgis.ScaleMethod.ScaleArea,
                                         QColor("#2B7FFF"), QColor("#FFFFFF"))
        dot.setStrokeWidth(0.8)
        sym = live.renderer().symbol()
        sym.deleteSymbolLayer(0)
        sym.appendSymbolLayer(dot)
        pal = QgsPalLayerSettings()
        pal.fieldName = TEAM_LABEL
        pal.isExpression = True
        fmt = QgsTextFormat()
        fmt.setSize(9)
        fmt.buffer().setEnabled(True)
        pal.setFormat(fmt)
        live.setLabeling(QgsVectorLayerSimpleLabeling(pal))
        live.setLabelsEnabled(True)
        QgsProject.instance().addMapLayer(live)
    return live, track


def _share_labels(live):
    """Phones on the very same spot: the first writes all their names, the others write none (the map drew
    only one of the names there)."""
    col = live.fields().indexOf("label")
    if col < 0:
        return
    spots = {}
    for f in live.getFeatures():
        g = f.geometry()
        if g.isEmpty():
            continue
        p = g.asPoint()
        spots.setdefault((p.x(), p.y()), []).append(f)
    out = {}
    for group in spots.values():
        group.sort(key=lambda f: str(f["device"]))
        names = " · ".join(f["name"] or "•" for f in group) if len(group) > 1 else None
        for i, f in enumerate(group):
            want = (names if i == 0 else "") if names else None
            if f["label"] != want:
                out[f.id()] = {col: want}
    if out:
        live.dataProvider().changeAttributeValues(out)


_TRACK_LAST = {}   # phone -> (lon, lat) of its last point on the track


def _moved(pos, least=10.0):
    """The phone really moved since its last track point: further than its own accuracy (and 10 m). A phone
    standing indoors wanders ±10-50 m and sends again every 15-30 s: its track became a cloud of dots."""
    import math

    last = _TRACK_LAST.get(pos["id"])
    if last is not None:
        k = math.cos(math.radians(pos["lat"]))
        d = math.hypot((pos["lon"] - last[0]) * k, pos["lat"] - last[1]) * 111_320
        if d < max(least, pos.get("acc") or 0):
            return False
    _TRACK_LAST[pos["id"]] = (pos["lon"], pos["lat"])
    return True


def update_team(live, track, pos):
    """Move this phone's point (or add it) and keep the position on its track."""
    geom = QgsGeometry.fromPointXY(QgsPointXY(pos["lon"], pos["lat"]))
    values = {"device": pos["id"], "name": pos.get("name") or None, "seen": pos["seen"],
              "accuracy_m": pos.get("acc"), "heading": pos.get("heading")}
    pr = live.dataProvider()
    fid = next((f.id() for f in live.getFeatures() if f["device"] == pos["id"]), None)
    if fid is None:
        f = QgsFeature(live.fields())
        f.setGeometry(geom)
        for k, v in values.items():
            f[k] = v
        pr.addFeatures([f])
    else:
        pr.changeGeometryValues({fid: geom})
        pr.changeAttributeValues({fid: {live.fields().indexOf(k): v for k, v in values.items()}})
    _share_labels(live)
    live.triggerRepaint()
    if not _moved(pos):
        return
    t = QgsFeature(track.fields())
    t.setGeometry(geom)
    for k, v in values.items():
        t[k] = v
    track.dataProvider().addFeatures([t])
    track.triggerRepaint()


# ---- the team list ------------------------------------------------------------------------------------
def _state_text(ago, lost_after):
    if ago < lost_after:
        return tr("Connected")
    mins = int(ago // 60)
    return tr("Not heard for {m} min").format(m=mins) if mins else tr("Not heard for a minute")


def _net_text(p):
    """Wi-Fi or mobile data: Android's browser tells it, an iPhone's does not."""
    net = p.get("net")
    if net == "wifi":
        return tr("Wi-Fi")
    if net == "cellular":
        return tr("Mobile data") + (f" ({p['eff'].upper()})" if p.get("eff") in ("2g", "3g", "4g") else "")
    if net == "ethernet":
        return tr("Cable")
    return "—"


def link_quality(ping):
    """The link from the time a message takes to the computer and back (ms): the browser gives no signal bars,
    and this is what the photos feel - the whole way, not only the phone's own signal."""
    if ping is None:
        return None
    return 4 if ping < 150 else 3 if ping < 400 else 2 if ping < 1000 else 1


def _link_text(p):
    q = link_quality(p.get("ping"))
    if q is None:
        return "—"
    word = {4: tr("Strong"), 3: tr("Good"), 2: tr("Weak"), 1: tr("Very weak")}[q]
    return "▮" * q + "▯" * (4 - q) + " " + word + " · {ms} ms".format(ms=round(p["ping"]))


def _link_tip(p):
    route = {"lan": tr("On the same network as this computer."),
             "direct": tr("Directly over the internet."),
             "relay": tr("Through a relay on the internet (the phone's network does not allow a direct link)."),
             "tunnel": tr("Through the tunnel on the internet."),
             "peer": tr("Carried by a teammate's phone (this phone's network allows no direct link).")}.get(
        p.get("route"), "")
    parts = [route] if route else []
    if p.get("ping") is not None:
        parts.append(tr("A message takes {ms} ms to reach this computer and come back.").format(ms=round(p["ping"])))
    if p.get("down"):
        parts.append(tr("The phone's own estimate of its speed: {d} Mbit/s.").format(d=p["down"]))
    return "\n".join(parts)


def _battery_text(p):
    if p.get("bat") is None:
        return "—"
    return "{n}%".format(n=round(p["bat"] * 100)) + (" ⚡" if p.get("chg") else "")


# ---- the guide: features the phones are led to --------------------------------------------------------
def guide_items(layer, features, limit=MAX_GUIDE_FEATURES):
    """Features of a layer as the guide's items (lon/lat), named by the layer's display name."""
    from qgis.core import QgsExpression, QgsExpressionContext, QgsExpressionContextUtils

    xf = QgsCoordinateTransform(layer.crs(), WGS84, QgsProject.instance())
    ctx = QgsExpressionContext(QgsExpressionContextUtils.globalProjectLayerScopes(layer))
    expr = QgsExpression(layer.displayExpression() or "")
    items = []
    for f in features:
        if len(items) >= limit:
            break
        g = QgsGeometry(f.geometry())
        if g.isEmpty():
            continue
        try:
            g.transform(xf)
        except Exception:  # noqa: BLE001 - a feature outside the projection's area is left out
            continue
        if g.constGet().nCoordinates() > 3000:
            g = g.simplify(0.00001)
        ctx.setFeature(f)
        name = expr.evaluate(ctx) if expr.expression() else None
        name = "" if name is None else str(name)
        kind = g.type()
        if kind == Qgis.GeometryType.Point:
            for pt in (g.asMultiPoint() if g.isMultipart() else [g.asPoint()]):
                items.append({"k": "pt", "n": name, "c": [pt.x(), pt.y()]})
        elif kind == Qgis.GeometryType.Line:
            parts = g.asMultiPolyline() if g.isMultipart() else [g.asPolyline()]
            items.append({"k": "line", "n": name, "c": [[[q.x(), q.y()] for q in part] for part in parts]})
        elif kind == Qgis.GeometryType.Polygon:
            polys = g.asMultiPolygon() if g.isMultipart() else [g.asPolygon()]
            items.append({"k": "poly", "n": name, "c": [[[q.x(), q.y()] for q in ring] for poly in polys for ring in poly]})
    return items


# ---- inside a building: the rooms of an indoor map (indoor/), their codes on the doors -------------------
def units_layer(project=None):
    """The rooms layer of an indoor map in the project: the current layer if it is one, else the first."""
    from qgis.utils import iface

    def is_units(lyr):
        return (isinstance(lyr, QgsVectorLayer) and lyr.geometryType() == Qgis.GeometryType.Polygon
                and lyr.fields().indexOf("unit_id") >= 0 and lyr.fields().indexOf("level_id") >= 0)

    cur = iface.activeLayer() if iface is not None else None
    if is_units(cur):
        return cur
    return next((lyr for lyr in (project or QgsProject.instance()).mapLayers().values() if is_units(lyr)), None)


def indoor_items(units):
    """(levels, units) of an indoor map for server.indoor_payload: rooms in longitude / latitude."""
    import math

    xf = QgsCoordinateTransform(units.crs(), WGS84, QgsProject.instance())
    names = {}
    src = units.source().split("|")[0]
    levels_lyr = QgsVectorLayer(f"{src}|layername=levels", "levels", "ogr") if src.lower().endswith(".gpkg") else None
    if levels_lyr is not None and levels_lyr.isValid():
        names = {f["level_id"]: f["name"] or "" for f in levels_lyr.getFeatures()}
    idx = {n: units.fields().indexOf(n) for n in ("name", "number", "category")}
    out, seen = [], set()
    for f in units.getFeatures():
        g = QgsGeometry(f.geometry())
        if g.isEmpty() or f["unit_id"] in (None, ""):
            continue
        inside = g.pointOnSurface().asPoint()
        far = max((math.hypot(v.x() - inside.x(), v.y() - inside.y()) for v in g.vertices()), default=5.0)
        try:
            g.transform(xf)
        except Exception:  # noqa: BLE001 - a room outside the projection's area is left out
            continue
        polys = g.asMultiPolygon() if g.isMultipart() else [g.asPolygon()]
        p = g.pointOnSurface().asPoint()
        lv = int(f["level_id"] or 0)
        seen.add(lv)
        out.append({"id": str(f["unit_id"]), "lv": lv, "p": [p.x(), p.y()],
                    "r": 5.0 if units.crs().isGeographic() else far,  # metres only in a projected plan
                    "n": (f["name"] if idx["name"] >= 0 else "") or "",
                    "num": (f["number"] if idx["number"] >= 0 else "") or "",
                    "cat": (f["category"] if idx["category"] >= 0 else "") or "",
                    "c": [[[q.x(), q.y()] for q in ring] for poly in polys for ring in poly]})
    return [{"id": lv, "name": names.get(lv) or ""} for lv in sorted(seen)], out


def room_url(unit_id, label="", level=None):
    from urllib.parse import quote

    return ROOM_URL + f"#u={quote(str(unit_id))}" + (f"&n={quote(label)}" if label else "") + (
        f"&l={level}" if level is not None else "")


def room_labels_pdf(units, path, per_row=3, per_col=5):
    """Sheets of stickers, one per room: its code and its number. The code works with the field photos
    page (it places the photo in the room) and alone (the phone's camera opens the room's page)."""
    from qgis.PyQt.QtCore import QMarginsF, QRectF
    from qgis.PyQt.QtGui import QFont, QImage, QPageLayout, QPageSize, QPainter, QPdfWriter

    from ..gps.browser import qr_png
    from ..vendor import ensure_vendor

    ensure_vendor()
    rooms = [f for f in units.getFeatures() if f["unit_id"] not in (None, "")
             and (units.fields().indexOf("category") < 0 or f["category"] not in ("corridor", "shaft"))]
    rooms.sort(key=lambda f: (int(f["level_id"] or 0), str(f["unit_id"])))
    pdf = QPdfWriter(path)
    pdf.setPageLayout(QPageLayout(QPageSize(QPageSize.PageSizeId.A4), QPageLayout.Orientation.Portrait,
                                  QMarginsF(8, 8, 8, 8), QPageLayout.Unit.Millimeter))
    pdf.setResolution(300)
    painter = QPainter(pdf)
    mm = 300 / 25.4
    cell_w, cell_h = (210 - 16) / per_row * mm, (297 - 16) / per_col * mm
    num_i = units.fields().indexOf("number")
    name_i = units.fields().indexOf("name")
    for i, f in enumerate(rooms):
        if i and i % (per_row * per_col) == 0:
            pdf.newPage()
        k = i % (per_row * per_col)
        x, y = (k % per_row) * cell_w, (k // per_row) * cell_h
        number = str(f["number"] or "") if num_i >= 0 else ""
        name = str(f["name"] or "") if name_i >= 0 else ""
        painter.setPen(QColor("#BBBBBB"))
        painter.drawRect(QRectF(x + 2 * mm, y + 2 * mm, cell_w - 4 * mm, cell_h - 4 * mm))
        img = QImage.fromData(qr_png(room_url(f["unit_id"], " ".join(x for x in (number, name) if x),
                                              int(f["level_id"] or 0)), 8))
        side = min(cell_w * 0.62, cell_h - 22 * mm)
        painter.drawImage(QRectF(x + (cell_w - side) / 2, y + 5 * mm, side, side), img)
        painter.setPen(QColor("#111111"))
        font = QFont("Segoe UI")
        font.setPixelSize(int(6 * mm))
        font.setBold(True)
        painter.setFont(font)
        painter.drawText(QRectF(x, y + 6 * mm + side, cell_w, 8 * mm), int(Qt.AlignmentFlag.AlignCenter),
                         number or str(f["unit_id"]))
        font.setPixelSize(int(3.4 * mm))
        font.setBold(False)
        painter.setFont(font)
        painter.drawText(QRectF(x + 3 * mm, y + 14 * mm + side, cell_w - 6 * mm, 5 * mm),
                         int(Qt.AlignmentFlag.AlignCenter), " · ".join(
                             v for v in (name, tr("Level {n}").format(n=int(f["level_id"] or 0))) if v))
    painter.end()
    return len(rooms)


# ---- the panel ---------------------------------------------------------------
class FieldPhotosDock(QDockWidget):
    def __init__(self, plugin):
        super().__init__(tr("Field Photos"), plugin.iface.mainWindow())
        self.plugin = plugin
        self.iface = plugin.iface
        self.setObjectName("BiruniFieldPhotos")
        self.server = self.tunnel = self.bridge = None
        self._bridge_since = 0
        self._relays_up = 0
        self.layer = None
        self.received = 0
        self._downloading = None
        self._own = None  # (token, host) of the user's own tunnel while it is used
        self.team = {}  # device -> its last position message and when it came (the team list)
        self.team_live = self.team_track = None
        self._last_photo = None
        self._poll = QTimer(self)
        self._poll.setInterval(400)
        self._poll.timeout.connect(self._read_events)
        self._build()

    def _build(self):
        s = QgsSettings()
        body = QWidget()
        body.setObjectName("BiruniGps")
        v = QVBoxLayout(body)
        v.setContentsMargins(10, 8, 10, 8)
        v.setSpacing(8)

        card = QFrame()
        card.setObjectName("GpsCard")
        cv = QVBoxLayout(card)
        self.mode = QComboBox()
        self.mode.addItem(tr("Phone nearby (same Wi-Fi or the phone's hotspot)"), "local")
        self.mode.addItem(tr("Phone anywhere - Biruni bridge (direct, nothing to set up)"), "bridge")
        self.mode.addItem(tr("Phone anywhere (over the internet)"), "internet")
        self.mode.addItem(tr("Phone anywhere - my own Cloudflare tunnel (fixed address)"), "own")
        self.mode.setCurrentIndex(max(0, self.mode.findData(s.value(KEY + "mode", "local"))))
        cv.addWidget(self.mode)
        from .tunnel import unprotect

        self.own = QFrame()
        ov = QVBoxLayout(self.own)
        ov.setContentsMargins(0, 0, 0, 0)
        self.own_token = QLineEdit(unprotect(s.value(KEY + "own-token", "")))
        self.own_token.setEchoMode(QLineEdit.EchoMode.Password)
        self.own_token.setPlaceholderText(tr("Tunnel token - paste the whole command Cloudflare shows, that is fine"))
        self.own_host = QLineEdit(s.value(KEY + "own-host", ""))
        self.own_host.setPlaceholderText(tr("Its address, e.g. photos.mydomain.com"))
        how = QPushButton(tr("How do I get these?"))
        how.clicked.connect(self._own_help)
        ov.addWidget(self.own_token)
        ov.addWidget(self.own_host)
        ov.addWidget(how)
        cv.addWidget(self.own)
        self.new_code = QPushButton(tr("New code"))
        self.new_code.setToolTip(tr("Makes a new code for the bridge. Phones that scanned the old one can no longer "
                                    "send to this computer."))
        self.new_code.clicked.connect(self._new_code)
        cv.addWidget(self.new_code)
        self.kinds = QLineEdit(s.value(KEY + "kinds", ""))
        self.kinds.setPlaceholderText(tr("Kinds to choose on the phone, separated by commas (optional)"))
        cv.addWidget(self.kinds)
        frow = QHBoxLayout()
        self.form_info = QLabel()
        self.form_info.setObjectName("GpsHint")
        self.form_btn = QPushButton(tr("Form…"))
        self.form_btn.setToolTip(tr("Questions the phone asks with each photo: choices, numbers, yes / no, dates."))
        self.form_btn.clicked.connect(self._edit_form)
        frow.addWidget(self.form_info, 1)
        frow.addWidget(self.form_btn)
        cv.addLayout(frow)
        row = QHBoxLayout()
        self.folder = QLineEdit()
        self.folder.setReadOnly(True)
        pick = QPushButton(tr("Change…"))
        pick.clicked.connect(self._pick_folder)
        show = QPushButton(tr("Open"))
        show.clicked.connect(self._open_folder)
        row.addWidget(self.folder, 1)
        row.addWidget(pick)
        row.addWidget(show)
        cv.addWidget(QLabel(tr("Photos are saved in")))
        cv.addLayout(row)
        self.start_btn = QPushButton(tr("Start"))
        self.start_btn.setObjectName("GpsPrimary")
        self.start_btn.clicked.connect(self.toggle)
        cv.addWidget(self.start_btn)
        self.state = QLabel(tr("Not started"))
        self.state.setObjectName("GpsHint")
        self.state.setWordWrap(True)
        cv.addWidget(self.state)
        v.addWidget(card)

        qr_card = QFrame()
        qr_card.setObjectName("GpsCard")
        qv = QVBoxLayout(qr_card)
        self.qr = QLabel()
        self.qr.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.url = QLabel()
        self.url.setObjectName("GpsHint")
        self.url.setWordWrap(True)
        self.url.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.url.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.hint = QLabel()
        self.hint.setObjectName("GpsHint")
        self.hint.setWordWrap(True)
        qv.addWidget(self.qr)
        qv.addWidget(self.url)
        qv.addWidget(self.hint)
        v.addWidget(qr_card)

        last = QFrame()
        last.setObjectName("GpsCard")
        lv = QVBoxLayout(last)
        self.count = QLabel(tr("Photos received: {n}").format(n=0))
        self.count.setObjectName("GpsValue")
        self.thumb = QLabel()
        self.thumb.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.thumb.setMinimumHeight(40)
        self.last_info = QLabel()
        self.last_info.setObjectName("GpsHint")
        self.last_info.setWordWrap(True)
        lv.addWidget(self.count)
        lv.addWidget(self.thumb)
        lv.addWidget(self.last_info)
        self.place_btn = QPushButton(tr("Place this photo on the map (a sketch or a plan)"))
        self.place_btn.setToolTip(tr("Lays the selected photo (or the last one) over the map to fit it by hand or "
                                     "by pairs of points - for a sketch drawn on paper or a photographed plan."))
        self.place_btn.clicked.connect(self._place_photo)
        lv.addWidget(self.place_btn)
        v.addWidget(last)

        team = QFrame()
        team.setObjectName("GpsCard")
        tv = QVBoxLayout(team)
        self.team_info = QLabel(tr("Field team: no phone yet"))
        self.team_info.setObjectName("GpsHint")
        self.team_info.setWordWrap(True)
        tv.addWidget(self.team_info)
        self.team_list = QTreeWidget()
        self.team_list.setRootIsDecorated(False)
        self.team_list.setHeaderLabels([tr("Name"), tr("State"), tr("Network"), tr("Link"), tr("Location"),
                                        tr("Battery")])
        self.team_list.setToolTip(tr("Double-click a phone to see it on the map."))
        self.team_list.itemDoubleClicked.connect(self._go_to_phone)
        self.team_list.setMaximumHeight(170)
        self.team_list.hide()
        tv.addWidget(self.team_list)
        self._team_tick = QTimer(self)
        self._team_tick.setInterval(5000)
        self._team_tick.timeout.connect(self._show_team)
        grow = QHBoxLayout()
        self.guide_btn = QPushButton(tr("Guide the phones to the selection"))
        self.guide_btn.setToolTip(tr("The selected features of the current layer go to every phone: points to reach, "
                                     "lines and boundaries to walk. The phone shows them with its own position, the "
                                     "distance and the direction."))
        self.guide_btn.clicked.connect(self._send_guide)
        self.guide_clear = QPushButton(tr("Clear"))
        self.guide_clear.clicked.connect(lambda: self._set_guide([], tr("The phones' guide is cleared.")))
        grow.addWidget(self.guide_btn, 1)
        grow.addWidget(self.guide_clear)
        tv.addLayout(grow)
        irow = QHBoxLayout()
        self.indoor_btn = QPushButton(tr("Send the indoor map"))
        self.indoor_btn.setToolTip(tr("The rooms of the indoor map go to the phones: inside the building a phone scans "
                                      "the code on a room's door, then taps where it is - the photo is placed in that "
                                      "room, on its level, without GPS."))
        self.indoor_btn.clicked.connect(self._send_indoor)
        self.labels_btn = QPushButton(tr("Room codes (PDF)…"))
        self.labels_btn.setToolTip(tr("Sheets of stickers with each room's code, to stick on the doors."))
        self.labels_btn.clicked.connect(self._room_labels)
        irow.addWidget(self.indoor_btn, 1)
        irow.addWidget(self.labels_btn)
        tv.addLayout(irow)
        v.addWidget(team)
        v.addStretch(1)
        self.setWidget(body)
        self.setMinimumWidth(300)
        self.mode.currentIndexChanged.connect(self._show_hint)
        self._show_hint()
        self._show_form()
        self.folder.setText(self._folder())

    def refresh_theme(self):
        from ..tracking import panel_qss

        self.widget().setStyleSheet(panel_qss(self.plugin.theme.tokens()))

    def _folder(self):
        return QgsSettings().value(KEY + "folder", "") or default_folder()

    def _pick_folder(self):
        d = QFileDialog.getExistingDirectory(self, tr("Folder for the field photos"), self._folder())
        if d:
            QgsSettings().setValue(KEY + "folder", d)
            self.folder.setText(d)

    def form(self):
        from .server import clean_form

        try:
            return clean_form(json.loads(QgsSettings().value(KEY + "form", "") or "[]"))
        except ValueError:
            return []

    def _show_form(self):
        n = len(self.form())
        self.form_info.setText(tr("Form: {n} questions").format(n=n) if n else tr("No form - photo, kind and note only"))

    def _edit_form(self):
        from .form_dialog import FormDialog

        d = FormDialog(self.form(), self)
        if d.exec():
            QgsSettings().setValue(KEY + "form", json.dumps(d.form, ensure_ascii=False))
            self._show_form()
            if self.server is not None:
                self.state.setText(tr("The form is saved. Press Stop, then Start, to send it to the phone."))

    def _show_hint(self):
        self.own.setVisible(self.mode.currentData() == "own")
        self.new_code.setVisible(self.mode.currentData() == "bridge")
        if self.mode.currentData() == "bridge":
            self.hint.setText(tr("Works wherever the phone has mobile data, with nothing to download or set up. "
                                 "The phone and this computer find each other through public relays, encrypted, "
                                 "then the photos go directly from the phone to this computer - they are not kept "
                                 "anywhere on the way. The code stays the same, so the phone can keep the page as a "
                                 "bookmark. A few mobile networks block direct links: then use another way."))
        elif self.mode.currentData() == "own":
            self.hint.setText(tr("Your own tunnel in your Cloudflare account: the same address every time, so the "
                                 "phone can keep it as a bookmark. Made once, in about ten minutes, and it needs a "
                                 "domain of your own. The photos only pass through it and are saved on this "
                                 "computer only."))
        elif self.mode.currentData() == "internet":
            self.hint.setText(tr("Works wherever the phone has mobile data. Biruni opens a temporary secure address "
                                 "through Cloudflare's free tunnel - no account, nothing to set up. The photos only "
                                 "pass through it and are saved on this computer only. The first time, Biruni "
                                 "downloads the tunnel program from Cloudflare (once)."))
        else:
            self.hint.setText(tr("The phone and this computer must be on the same Wi-Fi, or the computer connected to "
                                 "the phone's hotspot. Nothing leaves your network. The first time the phone warns "
                                 "that the connection is not private: choose Advanced, then Continue."))

    def _new_code(self):
        from .bridge import load_key

        if self.server is not None:
            return
        if QMessageBox.question(self, tr("New code"), self.new_code.toolTip()) == QMessageBox.StandardButton.Yes:
            load_key(QgsSettings(), fresh=True)
            self.state.setText(tr("A new code is made. Press Start to show it."))

    def _own_help(self):
        from .server import PHOTO_PORT

        steps = [tr("Make a free account at dash.cloudflare.com and add your domain to it (Cloudflare guides you)."),
                 tr("Open Zero Trust, then Networks, then Tunnels, and create a tunnel of the Cloudflared kind. "
                    "Name it Biruni, for example."),
                 tr("Cloudflare shows a command with a long token to install the connector. Do not run it: copy it "
                    "and paste it here in the token box."),
                 tr("Go on to the public hostname (application route): choose a name such as photos and your "
                    "domain. Service type HTTPS, URL localhost:{p}.").format(p=PHOTO_PORT),
                 tr("In its TLS settings turn on \"No TLS Verify\", then save."),
                 tr("Write the address (for example photos.mydomain.com) here and press Start. That is all - "
                    "next time just press Start.")]
        box = QMessageBox(self)
        box.setWindowTitle(tr("My own Cloudflare tunnel"))
        box.setTextFormat(Qt.TextFormat.RichText)
        box.setText("<ol>" + "".join(f"<li style='margin-bottom:6px'>{x}</li>" for x in steps) + "</ol>"
                    "<p><a href='https://dash.cloudflare.com'>dash.cloudflare.com</a></p>")
        box.setTextInteractionFlags(Qt.TextInteractionFlag.TextBrowserInteraction)
        box.exec()

    # ---- start / stop --------------------------------------------------------
    def toggle(self):
        if self.server is not None or self._downloading is not None:
            self.stop()
        else:
            self.start()

    def start(self):
        from ..gps.source import local_addresses
        from ..vendor import ensure_vendor
        from .server import PHOTO_PORT, PhotoServer, all_phone_texts, read_log

        ensure_vendor()  # certificate and QR code libraries
        s = QgsSettings()
        mode = self.mode.currentData()
        self._own = None
        if mode == "own":
            from . import tunnel

            token, host = tunnel.token_from(self.own_token.text()), tunnel.host_from(self.own_host.text())
            if token is None:
                self.state.setText(tr("The tunnel token is not right. Copy the command Cloudflare shows again and "
                                      "paste it."))
                return
            if host is None:
                self.state.setText(tr("Write the tunnel's address, for example photos.mydomain.com."))
                return
            self.own_token.setText(token)
            self.own_host.setText(host)
            s.setValue(KEY + "own-token", tunnel.protect(token))
            s.setValue(KEY + "own-host", host)
            self._own = (token, host)
        s.setValue(KEY + "mode", mode)
        s.setValue(KEY + "kinds", self.kinds.text())
        folder = self._folder()
        self.folder.setText(folder)
        form = self.form()
        try:
            self.layer = open_layer(folder)
            records = read_log(folder)
            form_columns(self.layer, form, records)
            add_records(self.layer, records)  # photos saved while the map was not updated
        except OSError as e:
            self.state.setText(tr("It could not be done: {e}").format(e=e))
            return
        texts = all_phone_texts()
        kinds = [k for k in self.kinds.text().replace("،", ",").split(",") if k.strip()]
        cert_folder = os.path.join(QgsApplication.qgisSettingsDirPath(), "biruni")
        bridged = mode == "bridge"  # then only the hidden receiver on this PC talks to the server
        try:
            self.server = PhotoServer(folder, cert_folder, texts, lang(), kinds, PHOTO_PORT,
                                      ["127.0.0.1"] if bridged else local_addresses(),
                                      bind="127.0.0.1" if bridged else "0.0.0.0", form=form)
        except OSError as e:
            self.state.setText(tr("Port {p} is busy ({e}).").format(p=PHOTO_PORT, e=e))
            return
        self.received = 0
        self.count.setText(tr("Photos received: {n}").format(n=0))
        self.start_btn.setText(tr("Stop"))
        self.mode.setEnabled(False)
        self.own.setEnabled(False)
        self.new_code.setEnabled(False)
        self._poll.start()
        if bridged:
            self._start_bridge()
        elif mode in ("internet", "own"):
            self._start_tunnel()
        else:
            ips = local_addresses()
            if not ips:
                self.state.setText(tr("No network found. Connect this computer to the Wi-Fi or the phone's hotspot."))
            else:
                self._show_qr(self.server.url(ips[0]), "\n".join(self.server.url(ip) for ip in ips))
                self.state.setText(tr("Scan the code with the phone's camera."))
        self.plugin.activity.log("field_photos", "started", mode, mode, 1)

    def _start_bridge(self):
        from . import bridge

        key = bridge.load_key(QgsSettings())
        try:
            self.bridge = bridge.BridgeHost(self.server.url("127.0.0.1") + "bridge#" + bridge.host_frag(key))
        except OSError as e:
            self.state.setText(tr("It could not be done: {e}").format(e=e))
            return
        self._bridge_since = time.monotonic()
        self._relays_up = 0
        url = bridge.phone_url(key, lang())
        from ..secure.door import door_ok

        if not door_ok(self, url):   # the phone page on the site is not the released one: no link, no QR
            self.bridge.stop()
            self.bridge = None
            self.state.setText(tr("The phone page could not be trusted: no link was shown."))
            return
        self._show_qr(url, url)
        self.url.setText(bridge.PAGE_URL.split("://", 1)[1])
        self.state.setText(tr("Connecting to the relays…"))

    def _start_tunnel(self):
        from . import tunnel

        exe = tunnel.find_exe()
        if exe is None:
            self.state.setText(tr("Downloading the tunnel program (once)…"))
            box = {"done": None, "error": None, "pct": 0, "cancel": False}
            self._downloading = box

            def work():
                try:
                    box["done"] = tunnel.download(progress=lambda d, t: box.update(pct=int(100 * d / t)),
                                                  cancelled=lambda: box["cancel"])
                except RuntimeError as e:
                    box["error"] = str(e)

            threading.Thread(target=work, daemon=True).start()
            return
        try:
            token, host = self._own or (None, None)
            self.tunnel = tunnel.Tunnel(exe, self.server.port, token, host)
        except OSError as e:
            self.state.setText(tr("It could not be done: {e}").format(e=e))
            return
        self.state.setText(tr("Opening the secure address…"))

    def stop(self):
        self._poll.stop()
        if self._downloading is not None:
            self._downloading["cancel"] = True
            self._downloading = None
        if self.tunnel is not None:
            self.tunnel.stop()
            self.tunnel = None
        if self.bridge is not None:
            self.bridge.stop()
            self.bridge = None
        if self.server is not None:
            try:
                self.server.stop()
            except OSError:
                pass
            self.server = None
        self.qr.clear()
        self.url.clear()
        self.start_btn.setText(tr("Start"))
        self.team = {}
        self._team_tick.stop()
        self._show_team()
        self.mode.setEnabled(True)
        self.own.setEnabled(True)
        self.new_code.setEnabled(True)
        self.state.setText(tr("Stopped. {n} photos received - they are in the folder and on the map.")
                           .format(n=self.received))

    def shutdown(self):
        self.stop()

    def _show_qr(self, url, tooltip=""):
        from ..gps.browser import qr_png

        pm = QPixmap()
        pm.loadFromData(qr_png(url, 5))
        self.qr.setPixmap(pm)
        self.url.setText(url)
        self.url.setToolTip(tooltip or url)

    # ---- events ----------------------------------------------------------------
    def _read_events(self):
        box = self._downloading
        if box is not None:
            if box["done"]:
                self._downloading = None
                self._start_tunnel()
            elif box["error"]:
                self._downloading = None
                self.state.setText(tr("The tunnel program could not be downloaded ({e}). Use \"Phone nearby\" "
                                      "or try again.").format(e=box["error"]))
            else:
                self.state.setText(tr("Downloading the tunnel program (once)… {p}%").format(p=box["pct"]))
        if self.tunnel is not None:
            while not self.tunnel.events.empty():
                kind, value = self.tunnel.events.get_nowait()
                if kind == "url" and self.server is not None:
                    self._show_qr(value + self.server.path())
                    self.state.setText(tr("Ready. Scan the code with the phone's camera - it works on mobile data "
                                          "too."))
                    if self.tunnel.fixed:
                        self._check_own(value + self.server.path() + "ping")
                elif kind == "check":
                    self.state.setText(tr("The tunnel is connected but its address does not reach this computer. "
                                          "In Cloudflare check the address, and that its service is HTTPS "
                                          "localhost:{p} with \"No TLS Verify\" on.").format(p=self.server.port))
                elif kind == "error" and self.tunnel.fixed:
                    self.state.setText(tr("Cloudflare did not accept the tunnel token. Copy it again from your "
                                          "tunnel's page."))
                elif kind == "error":
                    self.state.setText(tr("The secure address could not be opened. Check the internet, or use "
                                          "\"Phone nearby\"."))
                elif kind == "ended" and self.server is not None:
                    self.qr.clear()
                    self.url.clear()
                    self.state.setText(tr("The internet address closed. Press Stop, then Start, for a new one."))
        if self.bridge is not None:
            while not self.bridge.events.empty():
                kind, value = self.bridge.events.get_nowait()
                if kind == "ended" and self.server is not None:
                    self.qr.clear()
                    self.url.clear()
                    self.state.setText(tr("The bridge stopped ({e}). Press Stop, then Start.")
                                       .format(e=(self.bridge.log or [str(value)])[-1][-160:]))
            if (self._relays_up == 0 and self.bridge is not None and self.bridge.running()
                    and time.monotonic() - self._bridge_since > 30):
                self.state.setText(tr("No relay can be reached. Check this computer's internet connection."))
        if self.server is None:
            return
        photos = []
        while not self.server.events.empty():
            kind, value = self.server.events.get_nowait()
            if kind == "phone":
                self.state.setText(tr("A phone opened the page."))
            elif kind == "photo":
                photos.append(value)
            elif kind == "bridge":
                self._on_bridge(value)
            elif kind == "pos":
                self._on_pos(value)
        if photos:
            self._on_photos(photos)

    def _on_bridge(self, ev):
        t = ev.get("type")
        if t == "relays":
            was, self._relays_up = self._relays_up, int(ev.get("up") or 0)
            if self._relays_up and not was:
                self.state.setText(tr("Ready. Scan the code with the phone's camera - it works on mobile data too."))
            elif not self._relays_up:
                self.state.setText(tr("Connecting to the relays…"))
        elif t == "phone":
            self.state.setText(tr("A phone is connected through a teammate's phone.") if ev.get("via")
                               else tr("A phone is connected directly."))
        elif t == "p2pfail":
            self.state.setText(tr("A phone's network did not allow a direct link. It keeps its photos; on that "
                                  "network use \"Phone anywhere (over the internet)\"."))

    def _check_own(self, url):
        """Your own tunnel works only when its address leads here: try it once from this computer."""
        import urllib.request

        events = self.tunnel.events

        def work():
            for _ in range(3):  # a new route can take a few seconds to spread
                try:
                    req = urllib.request.Request(url, headers={"User-Agent": "Biruni-GIS"})
                    with urllib.request.urlopen(req, timeout=15) as r:
                        if r.status == 200:
                            return
                except OSError:
                    pass
                threading.Event().wait(5)
            events.put(("check", None))

        threading.Thread(target=work, daemon=True).start()

    def _live_layer(self):
        """The photos layer if it is still on the map (the user may have removed it - then it is deleted)."""
        from qgis.PyQt import sip

        lyr = self.layer
        if lyr is None or sip.isdeleted(lyr) or QgsProject.instance().mapLayer(lyr.id()) is None:
            self.layer = None
            return None
        return lyr

    def _on_pos(self, pos):
        from qgis.PyQt import sip

        try:
            if (self.team_live is None or sip.isdeleted(self.team_live)
                    or QgsProject.instance().mapLayer(self.team_live.id()) is None
                    or sip.isdeleted(self.team_track) or QgsProject.instance().mapLayer(self.team_track.id()) is None):
                self.team_live, self.team_track = team_layers(self.server.store.folder)
            update_team(self.team_live, self.team_track, pos)
        except (OSError, RuntimeError) as e:
            self.team_info.setText(tr("It could not be done: {e}").format(e=e))
            return
        if pos["id"] not in self.team:
            self._notify(tr("{who} joined the field team.").format(who=pos.get("name") or tr("a phone")),
                         self.team_live)
        if not pos.get("route"):   # over the bridge this computer measured it; otherwise the way is the mode's
            pos["route"] = {"local": "lan", "internet": "tunnel", "own": "tunnel"}.get(self.mode.currentData(), "")
        self.team[pos["id"]] = {"pos": pos, "at": time.time()}
        self._show_team()
        if not self._team_tick.isActive():
            self._team_tick.start()

    # a phone sends its position at least every 30 s while its page is open (and sharing its place is on)
    LOST_AFTER = 75

    def _show_team(self):
        """The team list: who is connected, over Wi-Fi or mobile data, how good the link is, the battery."""
        now = time.time()
        rows = sorted(self.team.items(), key=lambda kv: (kv[1]["pos"].get("name") or "~").lower())
        on = sum(1 for _d, x in rows if now - x["at"] < self.LOST_AFTER)
        if not rows:
            self.team_info.setText(tr("Field team: no phone yet"))
        else:
            self.team_info.setText(tr("Field team: {n} phones, {k} connected now").format(n=len(rows), k=on))
        self.team_list.setVisible(bool(rows))
        self.team_list.clear()
        for dev, x in rows:
            p, ago = x["pos"], now - x["at"]
            it = QTreeWidgetItem([p.get("name") or tr("(no name)"), _state_text(ago, self.LOST_AFTER),
                                  _net_text(p), _link_text(p), "± {m} m".format(m=round(p["acc"])) if p.get("acc")
                                  is not None else "—", _battery_text(p)])
            it.setData(0, Qt.ItemDataRole.UserRole, dev)
            it.setToolTip(3, _link_tip(p))
            if ago >= self.LOST_AFTER:
                for c in range(it.columnCount()):
                    it.setForeground(c, QColor("#8A8F98"))
            self.team_list.addTopLevelItem(it)
        for c in range(self.team_list.columnCount()):
            self.team_list.resizeColumnToContents(c)

    def _go_to_phone(self, item, _col):
        x = self.team.get(item.data(0, Qt.ItemDataRole.UserRole))
        if x is None:
            return
        canvas = self.iface.mapCanvas()
        pt = QgsCoordinateTransform(QgsCoordinateReferenceSystem("EPSG:4326"), canvas.mapSettings().destinationCrs(),
                                    QgsProject.instance()).transform(QgsPointXY(x["pos"]["lon"], x["pos"]["lat"]))
        canvas.setCenter(pt)
        canvas.refresh()
        if self.team_live is not None:
            canvas.flashGeometries([QgsGeometry.fromPointXY(QgsPointXY(x["pos"]["lon"], x["pos"]["lat"]))],
                                   QgsCoordinateReferenceSystem("EPSG:4326"))

    def photo_to_place(self):
        """The selected photo of the photos layer, else the last one received."""
        lyr = self._live_layer()
        if lyr is not None:
            for f in lyr.getSelectedFeatures():
                if f["photo"] and os.path.isfile(f["photo"]):
                    return f["photo"]
        return self._last_photo if self._last_photo and os.path.isfile(self._last_photo) else None

    def _place_photo(self):
        from .. import quick_align

        path = self.photo_to_place()
        if path is None:
            self.state.setText(tr("Select a photo on the map, or take one first."))
            return
        quick_align.start(self.plugin, path)

    def _send_guide(self):
        lyr = self.iface.activeLayer()
        if self.server is None:
            self.state.setText(tr("Press Start first."))
            return
        if not isinstance(lyr, QgsVectorLayer) or not lyr.isSpatial():
            self.state.setText(tr("Choose a layer of points, lines or polygons in the Layers panel first."))
            return
        feats = list(lyr.getSelectedFeatures())
        if not feats:
            if lyr.featureCount() > MAX_GUIDE_FEATURES:
                self.state.setText(tr("Select the features to send (at most {n}).").format(n=MAX_GUIDE_FEATURES))
                return
            feats = list(lyr.getFeatures())
        self._set_guide(guide_items(lyr, feats), None)

    def _send_indoor(self):
        from .server import indoor_payload

        if self.server is None:
            self.state.setText(tr("Press Start first."))
            return
        units = units_layer()
        if units is None:
            self.state.setText(tr("Open an indoor map first (Floor plan to indoor map) - its rooms layer is needed."))
            return
        levels, rooms = indoor_items(units)
        n = self.server.set_indoor(indoor_payload(levels, rooms))
        self.state.setText(tr("{n} rooms on {l} levels sent to the phones.").format(n=n, l=len(levels)))

    def _room_labels(self):
        units = units_layer()
        if units is None:
            self.state.setText(tr("Open an indoor map first (Floor plan to indoor map) - its rooms layer is needed."))
            return
        path, _ = QFileDialog.getSaveFileName(self, tr("Room codes (PDF)…"), os.path.join(self._folder(),
                                                                                      "room_codes.pdf"), "PDF (*.pdf)")
        if path:
            n = room_labels_pdf(units, path)
            self.state.setText(tr("{n} room codes written. Print them and stick each on its room's door.").format(n=n))
            QDesktopServices.openUrl(QUrl.fromLocalFile(path))

    def _set_guide(self, items, message):
        if self.server is None:
            return
        n = self.server.set_guide(items)
        self.state.setText(message or tr("{n} features sent to the phones' guide.").format(n=n))

    def _answers(self, r):
        labels = {f["name"]: f["label"] for f in (self.server.store.form if self.server is not None else [])}
        yes_no = {True: tr("Yes"), False: tr("No")}
        return "".join(f"\n{labels.get(k, k)}: {yes_no.get(v, v) if isinstance(v, bool) else v}"
                       for k, v in (r.get("fields") or {}).items())

    def _open_folder(self):
        """The photos folder is made when Start is pressed; Open makes it too, so it always opens."""
        folder = self._folder()
        try:
            os.makedirs(folder, exist_ok=True)
        except OSError as e:
            self.state.setText(tr("It could not be done: {e}").format(e=e))
            return
        QDesktopServices.openUrl(QUrl.fromLocalFile(folder))

    def _notify(self, text, layer=None):
        """Tell the user something arrived from a phone: a message above the map (with a button to see it)
        and, when Biruni is not the window in front, a Windows notification."""
        from ..version import APP_NAME

        bar = self.iface.messageBar()
        item = bar.createMessage(APP_NAME, text)
        if layer is not None:
            btn = QPushButton(tr("Show"))
            btn.clicked.connect(lambda: (self.iface.setActiveLayer(layer), self.iface.zoomToActiveLayer()))
            item.layout().addWidget(btn)
        bar.pushWidget(item, Qgis.MessageLevel.Success, 6)
        mw = self.iface.mainWindow()
        if not mw.isActiveWindow() or mw.isMinimized():
            from qgis.PyQt.QtWidgets import QSystemTrayIcon

            if QSystemTrayIcon.isSystemTrayAvailable():
                if getattr(self, "_tray", None) is None:
                    from .. import icons

                    self._tray = QSystemTrayIcon(icons.app_icon(), self)
                self._tray.show()
                self._tray.showMessage(APP_NAME, text, QSystemTrayIcon.MessageIcon.Information, 6000)

    def _on_photos(self, records):
        lyr = self._live_layer()
        if lyr is None:  # removed from the map: bring it back
            try:
                self.layer = lyr = open_layer(self._folder() if self.server is None else self.server.store.folder)
                form_columns(lyr, self.server.store.form if self.server is not None else self.form())
            except OSError as e:
                self.state.setText(tr("It could not be done: {e}").format(e=e))
                return
        try:
            form_columns(lyr, self.server.store.form if self.server is not None else [], records)
            added = add_records(lyr, records)
        except OSError as e:
            self.state.setText(tr("Photo saved, but not added to the map ({e}). It is added next time.").format(e=e))
            return
        self.received += len(records)
        self.count.setText(tr("Photos received: {n}").format(n=self.received))
        senders = sorted({r.get("by") or r.get("device") or tr("a phone") for r in records})
        self._notify(tr("{n} photos received from {who}.").format(n=len(records), who=", ".join(senders))
                     if len(records) > 1 else tr("A photo was received from {who}.").format(who=senders[0]), lyr)
        r = records[-1]
        self._last_photo = r["photo"]
        pm = QPixmap(r["photo"])
        if not pm.isNull():
            self.thumb.setPixmap(pm.scaled(280, 200, Qt.AspectRatioMode.KeepAspectRatio,
                                           Qt.TransformationMode.SmoothTransformation))
        acc = f" · ± {r['acc']:.0f} m" if r.get("acc") is not None else ""
        self.last_info.setText(" · ".join(x for x in (r.get("kind"), r.get("taken", "").replace("T", " ")[:19]) if x)
                               + acc + (f"\n{r['note']}" if r.get("note") else "") + self._answers(r))
        canvas = self.iface.mapCanvas()
        xf = QgsCoordinateTransform(WGS84, canvas.mapSettings().destinationCrs(), QgsProject.instance())
        geoms = []
        for f in added:
            g = QgsGeometry(f.geometry())
            try:
                g.transform(xf)
            except Exception:  # noqa: BLE001 - a flash is only a courtesy
                continue
            geoms.append(g)
            if not canvas.extent().contains(g.asPoint()):
                canvas.setCenter(g.asPoint())
                canvas.refresh()
        if geoms:
            canvas.flashGeometries(geoms, canvas.mapSettings().destinationCrs(), QColor("#2CC5B4"),
                                   QColor("#FFFFFF"), 3, 300)


_DOCK = {}


def open_dock(plugin):
    d = _DOCK.get(id(plugin))
    if d is None:
        d = FieldPhotosDock(plugin)
        from ..commands import tab_beside

        tab_beside(plugin.iface.mainWindow(), d, getattr(plugin, "toolbox", None))
        d.refresh_theme()
        _DOCK[id(plugin)] = d
        QgsApplication.instance().aboutToQuit.connect(d.shutdown)
    if d.isFloating():
        from ..commands import tab_beside

        tab_beside(plugin.iface.mainWindow(), d, getattr(plugin, "toolbox", None))
    d.show()
    d.raise_()
    return d


def close_dock(plugin):
    d = _DOCK.pop(id(plugin), None)
    if d is not None:
        d.shutdown()
        plugin.iface.mainWindow().removeDockWidget(d)
        d.deleteLater()
