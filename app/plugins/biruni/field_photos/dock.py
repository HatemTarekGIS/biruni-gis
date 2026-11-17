"""Field photos panel: start, show the QR code, and watch the photos arrive as points."""
import csv
import datetime as dt
import html as html_mod
import json
import os
import shutil
import threading
import time
from urllib.parse import quote

from qgis.core import (Qgis, QgsApplication, QgsCoordinateReferenceSystem, QgsCoordinateTransform,
                       QgsEditorWidgetSetup, QgsFeature, QgsField, QgsFields, QgsGeometry, QgsPointXY,
                       QgsProject, QgsProperty, QgsRectangle, QgsSettings, QgsSimpleMarkerSymbolLayer, QgsSymbolLayer,
                       QgsVectorFileWriter, QgsVectorLayer)
from qgis.PyQt.QtCore import QMetaType, Qt, QTimer, QUrl
from qgis.PyQt.QtGui import QColor, QDesktopServices, QPixmap
from qgis.PyQt.QtWidgets import (QAbstractItemView, QCheckBox, QComboBox, QDialog, QDockWidget, QFileDialog, QFrame,
                                 QHBoxLayout, QInputDialog, QLabel, QLineEdit, QListWidget, QMenu, QMessageBox,
                                 QPushButton, QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget)

from ..i18n import lang, tr
from .server import read_surveyor_keys

KEY = "biruni/field-photos/"
P_SCOPE = "Biruni"
P_DRIVE = "/field-photos/drive"
P_NOTICE = "/field-photos/drive-notice"
DRIVE_ROOTS = KEY + "drive-roots"
KEY_BACKUP = KEY + "key-backup"
LAYER = "field_photos"
GPKG = "field_photos.gpkg"
WGS84 = QgsCoordinateReferenceSystem("EPSG:4326")
FIELDS = (("photo_id", QMetaType.Type.QString), ("photo", QMetaType.Type.QString),
          ("taken", QMetaType.Type.QString), ("kind", QMetaType.Type.QString), ("note", QMetaType.Type.QString),
          ("accuracy_m", QMetaType.Type.Double), ("altitude", QMetaType.Type.Double),
          ("compass", QMetaType.Type.Double), ("heading", QMetaType.Type.Double),
          ("fix_age_s", QMetaType.Type.Double), ("device", QMetaType.Type.QString),
          ("avg_n", QMetaType.Type.Int), ("avg_sd_m", QMetaType.Type.Double),
          ("level_id", QMetaType.Type.Int), ("unit_id", QMetaType.Type.QString),
          ("surveyor", QMetaType.Type.QString), ("device_id", QMetaType.Type.QString))
ROOM_URL = "https://biruni-gis.com/r/"
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
            missing = [QgsField(n, t) for n, t in spec if lyr.fields().indexOf(n) < 0]
            if missing and lyr.dataProvider().addAttributes(missing):
                lyr.updateFields()
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


def _backfill_unknown_surveyor(lyr):
    """Old photos that had no surveyor name keep all their data and get 'Unknown' in surveyor."""
    s_idx = lyr.fields().indexOf("surveyor")
    if s_idx < 0:
        return
    sub = lyr.subsetString()
    if sub:
        lyr.setSubsetString("")
    unk = tr("Unknown")
    updates = {f.id(): {s_idx: unk} for f in lyr.getFeatures() if not f["surveyor"]}
    if updates:
        lyr.dataProvider().changeAttributeValues(updates)
    if sub:
        lyr.setSubsetString(sub)


def open_layer(folder, add=True):
    """The photos layer of this folder (created when missing), added to the project once."""
    os.makedirs(folder, exist_ok=True)
    lyr, new = _gpkg_layer(os.path.join(folder, GPKG), LAYER, FIELDS, tr("Field photos"))
    _backfill_unknown_surveyor(lyr)
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
    """Add the records whose photo_id is not in the layer yet (and update the file path when an original replaces a
    preview); returns the features added."""
    if not records:
        return []
    sub = lyr.subsetString()
    if sub:
        lyr.setSubsetString("")
    try:
        existing = {f["photo_id"]: f for f in lyr.getFeatures()}
        have = set(existing)
        feats = []
        p_idx = lyr.fields().indexOf("photo")
        s_idx = lyr.fields().indexOf("surveyor")
        d_idx = lyr.fields().indexOf("device_id")
        updates = {}
        for r in records:
            surveyor = r.get("surveyor") or r.get("by") or tr("Unknown")
            dev_id = r.get("device_id") or None
            if r["id"] in have:
                old_f = existing.get(r["id"])
                if old_f is not None:
                    chg = {}
                    if p_idx >= 0 and old_f["photo"] != r["photo"]:
                        chg[p_idx] = r["photo"]
                    if s_idx >= 0 and surveyor and old_f["surveyor"] != surveyor:
                        chg[s_idx] = surveyor
                    if d_idx >= 0 and dev_id and old_f["device_id"] != dev_id:
                        chg[d_idx] = dev_id
                    if chg:
                        updates[old_f.id()] = chg
                continue
            f = QgsFeature(lyr.fields())
            f.setGeometry(QgsGeometry.fromPointXY(QgsPointXY(r["lon"], r["lat"])))
            for name, value in (("photo_id", r["id"]), ("photo", r["photo"]), ("taken", r.get("taken")),
                                ("kind", r.get("kind") or None), ("note", r.get("note") or None),
                                ("accuracy_m", r.get("acc")), ("altitude", r.get("alt")),
                                ("compass", r.get("compass")), ("heading", r.get("heading")),
                                ("fix_age_s", r.get("fix_age")), ("device", r.get("device") or None),
                                ("avg_n", r.get("avg_n")), ("avg_sd_m", r.get("avg_sd")),
                                ("level_id", r.get("level")), ("unit_id", r.get("unit") or None),
                                ("surveyor", surveyor), ("device_id", dev_id)):
                if f.fields().indexOf(name) >= 0:
                    f[name] = value
            for name, value in (r.get("fields") or {}).items():
                if f.fields().indexOf(name) >= 0:
                    f[name] = value
            feats.append(f)
            have.add(r["id"])
        if updates:
            lyr.dataProvider().changeAttributeValues(updates)
            lyr.triggerRepaint()
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
    finally:
        if sub:
            lyr.setSubsetString(sub)


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
             "peer": tr("Carried by a teammate's phone (this phone's network allows no direct link)."),
             "drive": tr("Through the surveyor's own Google Drive, synced to this computer.")}.get(
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


def _route_text(route):
    return {
        "direct": tr("Direct"),
        "relay": tr("Relay"),
        "peer": tr("Teammate"),
        "lan": tr("Local network"),
        "tunnel": tr("Tunnel"),
        "drive": tr("Google Drive"),
    }.get(route or "", "—")


def _local_day(iso_str):
    if not iso_str:
        return ""
    try:
        return dt.datetime.fromisoformat(str(iso_str)).astimezone().strftime("%Y-%m-%d")
    except ValueError:
        return str(iso_str)[:10]


def _local_time_str(iso_str):
    if not iso_str:
        return "—"
    try:
        d = dt.datetime.fromisoformat(str(iso_str)).astimezone()
        today = dt.datetime.now().astimezone().strftime("%Y-%m-%d")
        return d.strftime("%H:%M:%S") if d.strftime("%Y-%m-%d") == today else d.strftime("%Y-%m-%d %H:%M")
    except ValueError:
        return str(iso_str).replace("T", " ")[:19]


def daily_report_data(records, day=None):
    """Summary of the day's field photos per surveyor: today's count, first and last photo, waiting originals."""
    day = day or dt.datetime.now().astimezone().strftime("%Y-%m-%d")
    groups = {}
    for r in records or ():
        dev = r.get("device_id") or ""
        name = r.get("surveyor") or r.get("by") or tr("Unknown")
        key = dev if dev else f"name:{name}"
        g = groups.setdefault(key, {
            "key": key, "surveyor": name, "device_id": dev,
            "today_count": 0, "total_count": 0, "waiting_originals": 0,
            "first_photo": None, "last_photo": None, "accs": [], "waiting_ids": [],
        })
        if name and (g["surveyor"] == tr("Unknown") or dev):
            g["surveyor"] = name
        g["total_count"] += 1
        if r.get("preview"):
            g["waiting_originals"] += 1
            g["waiting_ids"].append(r.get("id") or "")
        if r.get("acc") is not None:
            g["accs"].append(float(r["acc"]))
        taken = r.get("taken") or ""
        if _local_day(taken) == day or str(taken).startswith(day):
            g["today_count"] += 1
            if g["first_photo"] is None or taken < g["first_photo"]:
                g["first_photo"] = taken
            if g["last_photo"] is None or taken > g["last_photo"]:
                g["last_photo"] = taken
    rows = []
    for g in sorted(groups.values(), key=lambda x: (-x["today_count"], -x["total_count"], x["surveyor"].lower())):
        avg_acc = round(sum(g["accs"]) / len(g["accs"]), 1) if g["accs"] else None
        rows.append({
            "surveyor": g["surveyor"],
            "device_id": g["device_id"],
            "today_count": g["today_count"],
            "total_count": g["total_count"],
            "first_photo": _local_time_str(g["first_photo"]) if g["first_photo"] else "—",
            "last_photo": _local_time_str(g["last_photo"]) if g["last_photo"] else "—",
            "first_iso": g["first_photo"] or "",
            "last_iso": g["last_photo"] or "",
            "waiting_originals": g["waiting_originals"],
            "waiting_ids": g["waiting_ids"],
            "avg_accuracy_m": avg_acc,
        })
    return {
        "day": day,
        "surveyors": rows,
        "total_today": sum(r["today_count"] for r in rows),
        "total_all": sum(r["total_count"] for r in rows),
        "waiting_originals": sum(r["waiting_originals"] for r in rows),
    }


def write_daily_report(path, records, day=None):
    """Write the daily report as HTML or PDF; returns the report summary dict."""
    rep = daily_report_data(records, day=day)
    is_rtl = lang() in ("ar", "fa")
    direction = "rtl" if is_rtl else "ltr"
    title = tr("Field Photos Daily Report") + f" — {rep['day']}"
    if path.lower().endswith(".pdf"):
        from qgis.PyQt.QtCore import QMarginsF, QRectF
        from qgis.PyQt.QtGui import QFont, QPageLayout, QPageSize, QPainter, QPdfWriter

        pdf = QPdfWriter(path)
        pdf.setPageLayout(QPageLayout(QPageSize(QPageSize.PageSizeId.A4), QPageLayout.Orientation.Portrait,
                                      QMarginsF(12, 12, 12, 12), QPageLayout.Unit.Millimeter))
        pdf.setResolution(300)
        painter = QPainter(pdf)
        mm = 300 / 25.4
        page_w = (210 - 24) * mm
        y = 4 * mm
        font = QFont("Segoe UI")
        font.setPixelSize(int(6 * mm))
        font.setBold(True)
        painter.setFont(font)
        painter.setPen(QColor("#111111"))
        painter.drawText(QRectF(0, y, page_w, 10 * mm),
                         int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter), title)
        y += 12 * mm
        font.setPixelSize(int(3.6 * mm))
        font.setBold(False)
        painter.setFont(font)
        summary = (tr("Photos today: {n}").format(n=rep["total_today"]) + " · "
                   + tr("Total photos: {n}").format(n=rep["total_all"]) + " · "
                   + tr("Waiting originals: {n}").format(n=rep["waiting_originals"]))
        painter.drawText(QRectF(0, y, page_w, 7 * mm),
                         int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter), summary)
        y += 10 * mm
        cols = [
            (tr("Surveyor"), 0.24),
            (tr("Device ID"), 0.18),
            (tr("Today"), 0.10),
            (tr("Total"), 0.10),
            (tr("First photo"), 0.14),
            (tr("Last photo"), 0.14),
            (tr("Waiting originals"), 0.10),
        ]
        font.setBold(True)
        painter.setFont(font)
        x = 0.0
        for label, frac in cols:
            w = page_w * frac
            painter.drawRect(QRectF(x, y, w, 8 * mm))
            painter.drawText(QRectF(x + 1.5 * mm, y, w - 3 * mm, 8 * mm),
                             int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter), label)
            x += w
        y += 8 * mm
        font.setBold(False)
        painter.setFont(font)
        for r in rep["surveyors"]:
            if y > (297 - 36) * mm:
                pdf.newPage()
                y = 4 * mm
            vals = [
                r["surveyor"],
                r["device_id"] or "—",
                str(r["today_count"]),
                str(r["total_count"]),
                r["first_photo"],
                r["last_photo"],
                str(r["waiting_originals"]),
            ]
            x = 0.0
            for val, (_label, frac) in zip(vals, cols):
                w = page_w * frac
                painter.drawRect(QRectF(x, y, w, 7.5 * mm))
                painter.drawText(QRectF(x + 1.5 * mm, y, w - 3 * mm, 7.5 * mm),
                                 int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter), str(val))
                x += w
            y += 7.5 * mm
        painter.end()
        return rep
    esc = html_mod.escape
    trs = []
    for r in rep["surveyors"]:
        acc_txt = f"± {r['avg_accuracy_m']:.1f} m" if r["avg_accuracy_m"] is not None else "—"
        trs.append(
            f"<tr><td><b>{esc(r['surveyor'])}</b></td><td><code>{esc(r['device_id'] or '—')}</code></td>"
            f"<td>{r['today_count']}</td><td>{r['total_count']}</td><td>{esc(r['first_photo'])}</td>"
            f"<td>{esc(r['last_photo'])}</td><td>{r['waiting_originals']}</td><td>{esc(acc_txt)}</td></tr>"
        )
    doc = f"""<!doctype html><html lang="{esc(lang())}" dir="{direction}"><head><meta charset="utf-8">
<title>{esc(title)}</title>
<style>
body{{font:15px/1.5 system-ui,-apple-system,"Segoe UI",Roboto,"Noto Sans Arabic",Tahoma,sans-serif;margin:28px;color:#14181D;background:#F4F6F8}}
.card{{background:#fff;border:1px solid #E1E6EB;border-radius:14px;padding:20px;max-width:960px;margin:0 auto}}
h1{{margin:0 0 8px;font-size:22px}}
.meta{{color:#5F6B76;margin-bottom:16px;font-weight:600}}
table{{width:100%;border-collapse:collapse;margin-top:10px}}
th,td{{border:1px solid #E1E6EB;padding:9px 11px;text-align:start}}
th{{background:#F4F6F8;font-weight:700}}
code{{font-size:13px;color:#5F6B76}}
</style></head><body><div class="card">
<h1>{esc(title)}</h1>
<div class="meta">{esc(tr("Photos today: {n}").format(n=rep["total_today"]))} · {esc(tr("Total photos: {n}").format(n=rep["total_all"]))} · {esc(tr("Waiting originals: {n}").format(n=rep["waiting_originals"]))}</div>
<table><thead><tr>
<th>{esc(tr("Surveyor"))}</th><th>{esc(tr("Device ID"))}</th><th>{esc(tr("Today"))}</th><th>{esc(tr("Total"))}</th>
<th>{esc(tr("First photo"))}</th><th>{esc(tr("Last photo"))}</th><th>{esc(tr("Waiting originals"))}</th><th>{esc(tr("Accuracy"))}</th>
</tr></thead><tbody>{"".join(trs)}</tbody></table>
</div></body></html>"""
    with open(path, "w", encoding="utf-8") as f:
        f.write(doc)
    return rep


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
        self.server = self.tunnel = self.bridge = self.drive = None
        self._drive_last = None
        self._bridge_since = 0
        self._relays_up = 0
        self.layer = None
        self.received = 0
        self._downloading = None
        self._own = None  # (token, host) of the user's own tunnel while it is used
        self.team = {}  # device -> its last position message and when it came (the team list)
        self._photo_records = {}  # photo_id -> record (for surveyor stats in the table)
        self._base_qr_url = ""
        self._base_qr_tip = ""
        self._active_surveyor_filter = None
        self._active_today_filter = False
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
        self.qr_mode = QComboBox()
        self.qr_mode.addItem(tr("One QR code for all surveyors (each writes their name)"), "shared")
        self.qr_mode.addItem(tr("Separate QR code per surveyor (verified identity & instant revoke)"), "per_surveyor")
        init_qrm = read_surveyor_keys(self._folder()).get("qr_mode", s.value(KEY + "qr-mode", "shared"))
        self.qr_mode.setCurrentIndex(max(0, self.qr_mode.findData(init_qrm)))
        self.qr_mode.currentIndexChanged.connect(self._on_qr_mode_changed)
        cv.addWidget(self.qr_mode)
        qmrow = QHBoxLayout()
        self.surveyor_qr_btn = QPushButton(tr("Surveyor QR code…"))
        self.surveyor_qr_btn.setToolTip(tr("Create or show a personal QR code with a secret key for one surveyor."))
        self.surveyor_qr_btn.clicked.connect(self._surveyor_qr_dialog)
        self.shared_qr_btn = QPushButton(tr("Team QR code"))
        self.shared_qr_btn.setToolTip(tr("Show the shared team QR code."))
        self.shared_qr_btn.clicked.connect(self.show_shared_qr)
        qmrow.addWidget(self.surveyor_qr_btn, 1)
        qmrow.addWidget(self.shared_qr_btn)
        cv.addLayout(qmrow)
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
        self.drive_box = QCheckBox(tr("Receive photos through Google Drive (this project)"))
        self.drive_box.toggled.connect(self._on_drive_toggled)
        cv.addWidget(self.drive_box)
        self.drive_frame = QFrame()
        dv = QVBoxLayout(self.drive_frame)
        dv.setContentsMargins(18, 2, 0, 4)
        dv.setSpacing(4)
        dv.addWidget(QLabel(tr("Google Drive folders on this computer")))
        self.drive_list = QListWidget()
        self.drive_list.setMaximumHeight(54)
        dv.addWidget(self.drive_list)
        drow = QHBoxLayout()
        self.drive_add_btn = QPushButton(tr("Add…"))
        self.drive_add_btn.clicked.connect(self._add_drive_root)
        self.drive_remove_btn = QPushButton(tr("Remove"))
        self.drive_remove_btn.clicked.connect(self._remove_drive_root)
        drow.addWidget(self.drive_add_btn)
        drow.addWidget(self.drive_remove_btn)
        drow.addStretch(1)
        dv.addLayout(drow)
        self.drive_state = QLabel()
        self.drive_state.setObjectName("GpsHint")
        self.drive_state.setWordWrap(True)
        dv.addWidget(self.drive_state)
        krow = QHBoxLayout()
        self.key_backup_btn = QPushButton(tr("Back up key…"))
        self.key_backup_btn.clicked.connect(self._backup_key)
        self.key_restore_btn = QPushButton(tr("Restore key…"))
        self.key_restore_btn.clicked.connect(self._restore_key)
        krow.addWidget(self.key_backup_btn)
        krow.addWidget(self.key_restore_btn)
        krow.addStretch(1)
        dv.addLayout(krow)
        self.key_hint = QLabel()
        self.key_hint.setObjectName("GpsHint")
        self.key_hint.setWordWrap(True)
        dv.addWidget(self.key_hint)
        self.drive_frame.hide()
        cv.addWidget(self.drive_frame)
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
        self.team_list.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.team_list.setHeaderLabels([
            tr("Name"), tr("State"), tr("Network"), tr("Link"), tr("Location"),
            tr("Battery"), tr("Route"), tr("Today"), tr("Total"),
            tr("Waiting originals"), tr("Last seen"),
        ])
        self.team_list.setToolTip(tr("Double-click a phone to see it on the map."))
        self.team_list.itemDoubleClicked.connect(self._go_to_phone)
        self.team_list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.team_list.customContextMenuRequested.connect(self._team_menu)
        self.team_list.setMaximumHeight(190)
        self.team_list.hide()
        tv.addWidget(self.team_list)
        srow1 = QHBoxLayout()
        self.filter_surveyor_btn = QPushButton(tr("Show only their photos"))
        self.filter_surveyor_btn.clicked.connect(self._filter_selected_surveyor)
        self.show_all_btn = QPushButton(tr("Show all"))
        self.show_all_btn.clicked.connect(self.show_all_photos)
        self.zoom_surveyor_btn = QPushButton(tr("Zoom to their work"))
        self.zoom_surveyor_btn.clicked.connect(self._zoom_selected_surveyor)
        self.today_btn = QPushButton(tr("Today's work"))
        self.today_btn.setCheckable(True)
        self.today_btn.clicked.connect(self._toggle_today_filter)
        for b in (self.filter_surveyor_btn, self.show_all_btn, self.zoom_surveyor_btn, self.today_btn):
            srow1.addWidget(b)
        tv.addLayout(srow1)
        srow2 = QHBoxLayout()
        self.export_surveyor_btn = QPushButton(tr("Export their work…"))
        self.export_surveyor_btn.clicked.connect(self._export_selected_surveyor)
        self.unify_name_btn = QPushButton(tr("Unify name…"))
        self.unify_name_btn.clicked.connect(self._unify_selected_surveyor)
        self.block_dev_btn = QPushButton(tr("Block device…"))
        self.block_dev_btn.clicked.connect(self._block_selected_device)
        self.report_btn = QPushButton(tr("Daily report…"))
        self.report_btn.clicked.connect(self._save_daily_report_dialog)
        for b in (self.export_surveyor_btn, self.unify_name_btn, self.block_dev_btn, self.report_btn):
            srow2.addWidget(b)
        tv.addLayout(srow2)
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
        self._sync_drive()

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
        msg = self.new_code.toolTip()
        if self.drive_enabled():
            msg += "\n\n" + tr("Photos still waiting in Google Drive under the old code will not be received.")
        if QMessageBox.question(self, tr("New code"), msg) == QMessageBox.StandardButton.Yes:
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
            self._photo_records = {r["id"]: r for r in records}
            form_columns(self.layer, form, records)
            add_records(self.layer, records)  # photos saved while the map was not updated
        except OSError as e:
            self.state.setText(tr("It could not be done: {e}").format(e=e))
            return
        texts = all_phone_texts()
        kinds = [k for k in self.kinds.text().replace("،", ",").split(",") if k.strip()]
        cert_folder = self._cert_folder()
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
        self._start_drive()
        if bridged:
            self._start_bridge()
        elif mode in ("internet", "own"):
            self._start_tunnel()
        else:
            ips = local_addresses()
            if not ips:
                self.state.setText(tr("No network found. Connect this computer to the Wi-Fi or the phone's hotspot."))
            else:
                self._base_qr_url = self.server.url(ips[0])
                self._base_qr_tip = "\n".join(self.server.url(ip) for ip in ips)
                self._show_qr(self._base_qr_url, self._base_qr_tip)
                self.state.setText(tr("Scan the code with the phone's camera."))
        self.set_qr_mode(self.qr_mode.currentData() or "shared")
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
        self._base_qr_url = url
        self._base_qr_tip = url
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
        self._stop_drive()
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
        self._photo_records = {}
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
                    self._base_qr_url = value + self.server.path()
                    self._base_qr_tip = self._base_qr_url
                    self._show_qr(self._base_qr_url)
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
            elif kind == "drive":
                self._on_drive(value)
            elif kind == "rejected":
                self.state.setText(tr("Refused a photo from blocked device {d} ({r}).").format(
                    d=value.get("device_id", ""), r=value.get("reason", "")))
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
            self.state.setText(tr("A phone is connected through the relays (previews now, originals on Wi-Fi).")
                               if ev.get("relay") else
                               tr("A phone is connected through a teammate's phone.") if ev.get("via")
                               else tr("A phone is connected directly."))
        elif t == "p2pfail":
            self.state.setText(tr("A phone's network blocked a direct link: receiving encrypted previews over the "
                                  "relays; full originals wait for Wi-Fi."))

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

    def _active_folder(self):
        if self.server is not None and hasattr(self.server, "store"):
            return self.server.store.folder
        return self._folder()

    def _all_photo_records(self, from_disk=False):
        from .server import read_log

        if self.server is not None and hasattr(self.server.store, "records"):
            return list(self.server.store.records.values())
        if self._photo_records and not from_disk:
            return list(self._photo_records.values())
        if from_disk or self._live_layer() is not None:
            recs = read_log(self._active_folder())
            if recs:
                return recs
        return []

    def _surveyor_rows(self, include_disk=False):
        from .server import read_blocked

        now = time.time()
        today = dt.datetime.now().astimezone().strftime("%Y-%m-%d")
        active = bool(self.server is not None or self.team or self._photo_records or include_disk)
        blocked = read_blocked(self._active_folder()) if active else {}
        skeys = read_surveyor_keys(self._active_folder()).get("surveyors", {}) if active else {}
        groups = {}
        for sid, s in skeys.items():
            nm = s.get("name") or sid
            groups[sid] = {
                "key": sid, "device_id": sid,
                "name": nm,
                "names": {nm} if nm else set(),
                "pos": {}, "at": None,
                "route": "",
                "today_count": 0, "total_count": 0, "waiting_originals": 0,
                "accs": [], "last_photo": "",
                "blocked": bool(s.get("revoked") or sid in blocked),
            }
        for dev, x in self.team.items():
            p = x["pos"]
            s_entry = skeys.get(dev)
            groups[dev] = {
                "key": dev, "device_id": dev,
                "name": (s_entry.get("name") if s_entry else None) or p.get("name") or tr("(no name)"),
                "names": {p["name"]} if p.get("name") else set(),
                "pos": p, "at": x["at"],
                "route": p.get("route") or "",
                "today_count": 0, "total_count": 0, "waiting_originals": 0,
                "accs": [], "last_photo": "",
                "blocked": bool((s_entry and s_entry.get("revoked")) or dev in blocked),
            }
        recs = self._all_photo_records(from_disk=include_disk) if (self._photo_records or include_disk) else []
        for r in recs:
            dev = r.get("device_id") or ""
            s_name = r.get("surveyor") or r.get("by") or tr("Unknown")
            key = dev if dev else f"name:{s_name}"
            g = groups.get(key)
            if g is None:
                g = groups[key] = {
                    "key": key, "device_id": dev,
                    "name": s_name, "names": set(),
                    "pos": {}, "at": None, "route": "",
                    "today_count": 0, "total_count": 0, "waiting_originals": 0,
                    "accs": [], "last_photo": "",
                    "blocked": bool(dev and dev in blocked),
                }
            if s_name:
                g["names"].add(s_name)
                if g["name"] in (tr("(no name)"), tr("Unknown")) or dev:
                    g["name"] = s_name
            g["total_count"] += 1
            if r.get("preview"):
                g["waiting_originals"] += 1
            if r.get("acc") is not None:
                g["accs"].append(float(r["acc"]))
            taken = r.get("taken") or ""
            if _local_day(taken) == today or str(taken).startswith(today):
                g["today_count"] += 1
            if taken > g["last_photo"]:
                g["last_photo"] = taken
            if not g["route"] and r.get("route"):
                g["route"] = r["route"]
            elif not g["route"] and r.get("preview"):
                g["route"] = "relay"
        out = []
        for g in sorted(groups.values(), key=lambda x: (x["name"] or "~").lower()):
            g["avg_acc"] = (sum(g["accs"]) / len(g["accs"])) if g["accs"] else g["pos"].get("acc")
            g["connected"] = bool(g["at"] is not None and (now - g["at"] < self.LOST_AFTER) and not g["blocked"])
            out.append(g)
        return out

    def _show_team(self, include_disk=False):
        """The surveyors table: name, state, network, link, average accuracy, battery, route, today/total/preview counts, last seen."""
        now = time.time()
        rows = self._surveyor_rows(include_disk=include_disk)
        on = sum(1 for r in rows if r["connected"])
        if not rows:
            self.team_info.setText(tr("Field team: no phone yet"))
        else:
            self.team_info.setText(tr("Field team: {n} phones, {k} connected now").format(n=len(rows), k=on))
        self.team_list.setVisible(bool(rows))
        self.team_list.clear()
        for r in rows:
            p = r["pos"]
            ago = (now - r["at"]) if r["at"] is not None else None
            if r["blocked"]:
                st_txt = tr("Blocked")
            elif ago is not None:
                st_txt = _state_text(ago, self.LOST_AFTER)
            else:
                st_txt = tr("Offline")
            if r["at"] is not None:
                seen_iso = dt.datetime.fromtimestamp(r["at"], dt.timezone.utc).isoformat(timespec="seconds")
                if r["last_photo"] and r["last_photo"] > seen_iso:
                    seen_iso = r["last_photo"]
            else:
                seen_iso = r["last_photo"]
            acc_txt = "± {m} m".format(m=round(r["avg_acc"])) if r["avg_acc"] is not None else "—"
            it = QTreeWidgetItem([
                r["name"],
                st_txt,
                _net_text(p),
                _link_text(p),
                acc_txt,
                _battery_text(p),
                _route_text(r["route"]),
                str(r["today_count"]),
                str(r["total_count"]),
                str(r["waiting_originals"]),
                _local_time_str(seen_iso),
            ])
            it.setData(0, Qt.ItemDataRole.UserRole, r["key"])
            it.setData(0, Qt.ItemDataRole.UserRole + 1, r)
            it.setToolTip(3, _link_tip(p))
            if not r["connected"]:
                for c in range(it.columnCount()):
                    it.setForeground(c, QColor("#8A8F98"))
            self.team_list.addTopLevelItem(it)
        for c in range(self.team_list.columnCount()):
            self.team_list.resizeColumnToContents(c)

    def _go_to_phone(self, item, _col):
        key = item.data(0, Qt.ItemDataRole.UserRole)
        x = self.team.get(key)
        if x is None:
            self.zoom_to_surveyor(key)
            return
        canvas = self.iface.mapCanvas()
        pt = QgsCoordinateTransform(QgsCoordinateReferenceSystem("EPSG:4326"), canvas.mapSettings().destinationCrs(),
                                    QgsProject.instance()).transform(QgsPointXY(x["pos"]["lon"], x["pos"]["lat"]))
        canvas.setCenter(pt)
        canvas.refresh()
        if self.team_live is not None:
            canvas.flashGeometries([QgsGeometry.fromPointXY(QgsPointXY(x["pos"]["lon"], x["pos"]["lat"]))],
                                   QgsCoordinateReferenceSystem("EPSG:4326"))

    def _selected_surveyors(self):
        out = []
        for it in self.team_list.selectedItems():
            r = it.data(0, Qt.ItemDataRole.UserRole + 1)
            if isinstance(r, dict):
                out.append(r)
        if not out and self.team_list.currentItem() is not None:
            r = self.team_list.currentItem().data(0, Qt.ItemDataRole.UserRole + 1)
            if isinstance(r, dict):
                out.append(r)
        return out

    def _resolve_target(self, target):
        """Return (device_id, surveyor_name, names_set) for a key, name, device_id or row dict."""
        if isinstance(target, dict):
            return (target.get("device_id") or "", target.get("name") or target.get("surveyor") or "",
                    set(target.get("names") or ()))
        s = str(target or "").strip()
        if not s:
            return "", "", set()
        if s.startswith("name:"):
            return "", s[5:], {s[5:]}
        for r in self._surveyor_rows(include_disk=True):
            if r["key"] == s or r["device_id"] == s or r["name"] == s or s in r["names"]:
                return r["device_id"], r["name"], set(r["names"]) | ({r["name"]} if r["name"] else set())
        return s, s, {s}

    def _apply_layer_filter(self):
        lyr = self._live_layer()
        if lyr is None:
            return ""
        q = lambda v: "'" + str(v).replace("'", "''") + "'"
        clauses = []
        if self._active_surveyor_filter:
            dev_id, s_name, names = self._resolve_target(self._active_surveyor_filter)
            parts = []
            if dev_id:
                parts.append(f'"device_id" = {q(dev_id)}')
            for n in sorted(names | ({s_name} if s_name else set())):
                if n:
                    parts.append(f'"surveyor" = {q(n)}')
            if parts:
                clauses.append("(" + " OR ".join(parts) + ")")
        if self._active_today_filter:
            now_local = dt.datetime.now().astimezone()
            start_utc = now_local.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(
                dt.timezone.utc).isoformat(timespec="seconds")
            end_utc = now_local.replace(hour=23, minute=59, second=59, microsecond=0).astimezone(
                dt.timezone.utc).isoformat(timespec="seconds")
            day_str = now_local.strftime("%Y-%m-%d")
            clauses.append(
                f'(("taken" >= {q(start_utc)} AND "taken" <= {q(end_utc)}) OR "taken" LIKE {q(day_str + "%")})'
            )
        expr = " AND ".join(clauses)
        lyr.setSubsetString(expr)
        lyr.triggerRepaint()
        self.iface.mapCanvas().refresh()
        return expr

    def filter_by_surveyor(self, surveyor_or_dev, only_today=None):
        """Show only one surveyor's photos on the map and layer (display filter only; nothing is deleted)."""
        self._active_surveyor_filter = surveyor_or_dev
        if only_today is not None:
            self._active_today_filter = bool(only_today)
            self.today_btn.setChecked(self._active_today_filter)
        return self._apply_layer_filter()

    def filter_today(self, only_today=True, surveyor_or_dev=False):
        """Filter the layer to today's photos (for one surveyor or for all)."""
        self._active_today_filter = bool(only_today)
        self.today_btn.setChecked(self._active_today_filter)
        if surveyor_or_dev is not False:
            self._active_surveyor_filter = surveyor_or_dev
        return self._apply_layer_filter()

    def show_all_photos(self):
        """Clear surveyor and date filters so all photos are shown again."""
        self._active_surveyor_filter = None
        self._active_today_filter = False
        self.today_btn.setChecked(False)
        return self._apply_layer_filter()

    def zoom_to_surveyor(self, surveyor_or_dev=None):
        """Zoom the map canvas to the selected surveyor's photos."""
        lyr = self._live_layer()
        if lyr is None:
            return None
        dev_id, s_name, names = self._resolve_target(surveyor_or_dev)
        sub = lyr.subsetString()
        if sub:
            lyr.setSubsetString("")
        try:
            pts = []
            for f in lyr.getFeatures():
                if ((dev_id and f["device_id"] == dev_id)
                        or (s_name and f["surveyor"] == s_name)
                        or (f["surveyor"] in names)):
                    if not f.geometry().isEmpty():
                        pts.append(f.geometry().asPoint())
        finally:
            if sub:
                lyr.setSubsetString(sub)
        if not pts:
            return None
        xs = [p.x() for p in pts]
        ys = [p.y() for p in pts]
        rect = QgsRectangle(min(xs), min(ys), max(xs), max(ys))
        if rect.width() < 0.0005 or rect.height() < 0.0005:
            rect.grow(0.001)
        else:
            rect.scale(1.25)
        canvas = self.iface.mapCanvas()
        xf = QgsCoordinateTransform(WGS84, canvas.mapSettings().destinationCrs(), QgsProject.instance())
        try:
            rect = xf.transformBoundingBox(rect)
        except Exception:  # noqa: BLE001
            pass
        canvas.setExtent(rect)
        canvas.refresh()
        return rect

    def export_surveyor_work(self, surveyor_or_dev, out_path):
        """Export a surveyor's photos and attribute table (GeoPackage or CSV/Excel) with a folder of their photos."""
        lyr = self._live_layer()
        if lyr is None:
            lyr = open_layer(self._active_folder(), add=False)
        dev_id, s_name, names = self._resolve_target(surveyor_or_dev)
        sub = lyr.subsetString()
        if sub:
            lyr.setSubsetString("")
        try:
            feats = [
                f for f in lyr.getFeatures()
                if (dev_id and f["device_id"] == dev_id)
                or (s_name and f["surveyor"] == s_name)
                or (f["surveyor"] in names)
            ]
            fields = lyr.fields()
        finally:
            if sub:
                lyr.setSubsetString(sub)
        os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
        stem = os.path.splitext(os.path.basename(out_path))[0]
        photos_dir = os.path.join(os.path.dirname(os.path.abspath(out_path)), f"{stem}_photos")
        os.makedirs(photos_dir, exist_ok=True)
        copied_feats = []
        for f in feats:
            nf = QgsFeature(fields)
            nf.setGeometry(f.geometry())
            nf.setAttributes(f.attributes())
            src = f["photo"] or ""
            if src and os.path.isfile(src):
                dst = os.path.join(photos_dir, os.path.basename(src))
                if os.path.abspath(src) != os.path.abspath(dst):
                    shutil.copy2(src, dst)
                nf["photo"] = dst
            copied_feats.append(nf)
        ext = os.path.splitext(out_path)[1].lower()
        if ext == ".gpkg":
            opts = QgsVectorFileWriter.SaveVectorOptions()
            opts.driverName = "GPKG"
            opts.layerName = LAYER
            w = QgsVectorFileWriter.create(out_path, fields, Qgis.WkbType.Point, WGS84,
                                           QgsProject.instance().transformContext(), opts)
            w.addFeatures(copied_feats)
            del w
        elif ext == ".xlsx":
            opts = QgsVectorFileWriter.SaveVectorOptions()
            opts.driverName = "XLSX"
            opts.layerName = LAYER
            w = QgsVectorFileWriter.create(out_path, fields, Qgis.WkbType.Point, WGS84,
                                           QgsProject.instance().transformContext(), opts)
            if w.hasError() == QgsVectorFileWriter.WriterError.NoError:
                w.addFeatures(copied_feats)
                del w
            else:
                del w
                self._write_csv_export(out_path, fields, copied_feats)
        else:
            self._write_csv_export(out_path, fields, copied_feats)
        return {"path": out_path, "photos_dir": photos_dir, "count": len(copied_feats)}

    def _write_csv_export(self, out_path, fields, feats):
        names = [fields.at(i).name() for i in range(fields.count())]
        with open(out_path, "w", encoding="utf-8-sig", newline="") as f:
            w = csv.writer(f)
            w.writerow(["lon", "lat"] + names)
            for feat in feats:
                pt = feat.geometry().asPoint() if not feat.geometry().isEmpty() else QgsPointXY(0, 0)
                w.writerow([pt.x(), pt.y()] + [feat[n] if feat[n] is not None else "" for n in names])

    def unify_surveyor_name(self, new_name, device_id=None, old_names=()):
        """Unify surveyor name across the GeoPackage layer, photos.jsonl, and the live team table."""
        from .server import PhotoStore

        new_name = str(new_name or "").strip()[:40]
        if not new_name:
            return 0
        old_set = {str(x).strip()[:40] for x in (old_names or ()) if str(x).strip()}
        if self.server is not None and hasattr(self.server.store, "unify_name"):
            self.server.store.unify_name(new_name, device_id=device_id, old_names=old_set)
        else:
            PhotoStore(self._active_folder(), self.form()).unify_name(new_name, device_id=device_id, old_names=old_set)
        for r in self._photo_records.values():
            if (device_id and r.get("device_id") == device_id) or (
                old_set and (r.get("surveyor") in old_set or r.get("by") in old_set)
            ):
                r["surveyor"] = new_name
                r["by"] = new_name
        if device_id and device_id in self.team:
            self.team[device_id]["pos"]["name"] = new_name
        for x in self.team.values():
            if x["pos"].get("name") in old_set:
                x["pos"]["name"] = new_name
        lyr = self._live_layer()
        changed = 0
        if lyr is not None:
            s_idx = lyr.fields().indexOf("surveyor")
            if s_idx >= 0:
                sub = lyr.subsetString()
                if sub:
                    lyr.setSubsetString("")
                try:
                    updates = {}
                    for f in lyr.getFeatures():
                        if (device_id and f["device_id"] == device_id) or (old_set and f["surveyor"] in old_set):
                            updates[f.id()] = {s_idx: new_name}
                    if updates:
                        lyr.dataProvider().changeAttributeValues(updates)
                        lyr.triggerRepaint()
                        changed = len(updates)
                finally:
                    if sub:
                        lyr.setSubsetString(sub)
        if self._active_surveyor_filter in old_set:
            self._active_surveyor_filter = new_name
            self._apply_layer_filter()
        self._show_team(include_disk=True)
        return changed

    def block_device(self, device_id, reason="", name=""):
        """Block a device_id so future photos from it are refused and logged; existing photos stay untouched."""
        from .server import PhotoStore

        device_id = str(device_id or "").strip()[:64]
        if not device_id:
            return
        if self.server is not None and hasattr(self.server.store, "block_device"):
            self.server.store.block_device(device_id, reason=reason, name=name)
        else:
            PhotoStore(self._active_folder(), self.form()).block_device(device_id, reason=reason, name=name)
        self._show_team(include_disk=bool(self._photo_records))

    def unblock_device(self, device_id):
        from .server import PhotoStore

        device_id = str(device_id or "").strip()[:64]
        if not device_id:
            return
        if self.server is not None and hasattr(self.server.store, "unblock_device"):
            self.server.store.unblock_device(device_id)
        else:
            PhotoStore(self._active_folder(), self.form()).unblock_device(device_id)
        self._show_team(include_disk=bool(self._photo_records))
        self._drive_retry()

    def save_daily_report(self, out_path, day=None):
        """Write the daily report (HTML or PDF) for all photos in the folder."""
        recs = self._all_photo_records(from_disk=True)
        return write_daily_report(out_path, recs, day=day)

    def _filter_selected_surveyor(self):
        sel = self._selected_surveyors()
        if not sel:
            self.state.setText(tr("Select a surveyor in the list first."))
            return
        self.filter_by_surveyor(sel[0])
        self.state.setText(tr("Showing only photos from {who}.").format(who=sel[0]["name"]))

    def _zoom_selected_surveyor(self):
        sel = self._selected_surveyors()
        if not sel:
            self.state.setText(tr("Select a surveyor in the list first."))
            return
        if self.zoom_to_surveyor(sel[0]) is None:
            self.state.setText(tr("No photos on the map for {who}.").format(who=sel[0]["name"]))

    def _toggle_today_filter(self, checked=False):
        self.filter_today(bool(checked))

    def _export_selected_surveyor(self):
        sel = self._selected_surveyors()
        if not sel:
            self.state.setText(tr("Select a surveyor in the list first."))
            return
        who = sel[0]["name"]
        safe = "".join(c if c.isalnum() or c in ("-", "_") else "_" for c in who).strip("_") or "surveyor"
        path, _ = QFileDialog.getSaveFileName(
            self, tr("Export their work…"),
            os.path.join(self._active_folder(), f"{safe}.gpkg"),
            "GeoPackage (*.gpkg);;CSV / Excel (*.csv *.xlsx)",
        )
        if path:
            res = self.export_surveyor_work(sel[0], path)
            self.state.setText(tr("Exported {n} photos for {who}.").format(n=res["count"], who=who))

    def _unify_selected_surveyor(self):
        sel = self._selected_surveyors()
        if not sel:
            self.state.setText(tr("Select a surveyor in the list first."))
            return
        names = set()
        dev_id = sel[0].get("device_id") or None
        for r in sel:
            names.update(r.get("names") or ())
            if r.get("name"):
                names.add(r["name"])
        new_name, ok = QInputDialog.getText(self, tr("Unify name…"), tr("Unified surveyor name:"),
                                            text=sel[0]["name"])
        if ok and new_name.strip():
            n = self.unify_surveyor_name(new_name.strip(), device_id=dev_id, old_names=names)
            self.state.setText(tr("Unified name to {who} ({n} photos).").format(who=new_name.strip(), n=n))

    def _block_selected_device(self):
        sel = self._selected_surveyors()
        if not sel or not sel[0].get("device_id"):
            self.state.setText(tr("Select a surveyor with a device ID in the list first."))
            return
        r = sel[0]
        if r.get("blocked"):
            self.unblock_device(r["device_id"])
            self.state.setText(tr("Device {d} is unblocked.").format(d=r["device_id"]))
            return
        reason, ok = QInputDialog.getText(self, tr("Block device…"),
                                          tr("Reason for blocking {who}:").format(who=r["name"]),
                                          text=tr("Lost phone or left the team"))
        if ok:
            self.block_device(r["device_id"], reason=reason.strip(), name=r["name"])
            self.state.setText(tr("Blocked device {d} ({who}). Existing photos are kept.").format(
                d=r["device_id"], who=r["name"]))

    def _save_daily_report_dialog(self):
        day = dt.datetime.now().astimezone().strftime("%Y-%m-%d")
        path, _ = QFileDialog.getSaveFileName(
            self, tr("Daily report…"),
            os.path.join(self._active_folder(), f"daily_report_{day}.pdf"),
            "PDF (*.pdf);;HTML (*.html)",
        )
        if path:
            self.save_daily_report(path, day=day)
            self.state.setText(tr("Daily report saved: {p}").format(p=path))
            QDesktopServices.openUrl(QUrl.fromLocalFile(path))

    def _team_menu(self, pos):
        it = self.team_list.itemAt(pos)
        if it is not None and not it.isSelected():
            it.setSelected(True)
        m = QMenu(self)
        m.addAction(tr("Show only their photos"), self._filter_selected_surveyor)
        m.addAction(tr("Show all"), self.show_all_photos)
        m.addAction(tr("Zoom to their work"), self._zoom_selected_surveyor)
        act_today = m.addAction(tr("Today's work"))
        act_today.setCheckable(True)
        act_today.setChecked(self._active_today_filter)
        act_today.triggered.connect(self._toggle_today_filter)
        m.addSeparator()
        m.addAction(tr("Surveyor QR code…"), self._surveyor_qr_dialog)
        m.addAction(tr("New QR code for this surveyor"), self._replace_selected_surveyor_qr)
        m.addSeparator()
        m.addAction(tr("Export their work…"), self._export_selected_surveyor)
        m.addAction(tr("Unify name…"), self._unify_selected_surveyor)
        m.addAction(tr("Block device…"), self._block_selected_device)
        m.addSeparator()
        m.addAction(tr("Daily report…"), self._save_daily_report_dialog)
        m.exec(self.team_list.viewport().mapToGlobal(pos))

    def qr_mode_value(self):
        return self.qr_mode.currentData() or "shared"

    def set_qr_mode(self, mode):
        from .server import PhotoStore

        mode = "per_surveyor" if mode == "per_surveyor" else "shared"
        idx = self.qr_mode.findData(mode)
        if idx >= 0 and self.qr_mode.currentIndex() != idx:
            self.qr_mode.blockSignals(True)
            self.qr_mode.setCurrentIndex(idx)
            self.qr_mode.blockSignals(False)
        QgsSettings().setValue(KEY + "qr-mode", mode)
        if self.server is not None and hasattr(self.server.store, "set_qr_mode"):
            self.server.store.set_qr_mode(mode)
        else:
            PhotoStore(self._active_folder(), self.form()).set_qr_mode(mode)
        self.shared_qr_btn.setVisible(mode == "shared")
        return mode

    def _on_qr_mode_changed(self):
        mode = self.set_qr_mode(self.qr_mode.currentData() or "shared")
        if mode == "per_surveyor":
            self.state.setText(tr("Per-surveyor QR mode: each surveyor needs their own QR code (Surveyor QR code…)."))
        else:
            self.show_shared_qr()
        self._drive_retry()

    def _current_base_url(self):
        if self._base_qr_url:
            return self._base_qr_url
        if self.server is not None:
            from ..gps.source import local_addresses

            ips = local_addresses()
            return self.server.url(ips[0] if ips else "127.0.0.1")
        from . import bridge

        return bridge.phone_url(bridge.load_key(QgsSettings()), lang())

    def show_shared_qr(self):
        base = self._current_base_url()
        if base:
            self._show_qr(base, self._base_qr_tip or base)
            self.state.setText(tr("Showing the shared team QR code."))
        return base

    def issue_surveyor_qr(self, name, surveyor_id=None, rotate=False):
        from .server import PhotoStore

        name = str(name or "").strip()[:40]
        if not name:
            raise ValueError("surveyor name required")
        if self.server is not None and hasattr(self.server.store, "issue_surveyor_key"):
            entry = self.server.store.issue_surveyor_key(name, surveyor_id=surveyor_id, rotate=rotate)
        else:
            entry = PhotoStore(self._active_folder(), self.form()).issue_surveyor_key(
                name, surveyor_id=surveyor_id, rotate=rotate
            )
        base = self._current_base_url()
        frag = f"sk={entry['secret']}&sn={quote(entry['name'])}"
        url = f"{base}&{frag}" if "#" in base else f"{base}#{frag}"
        self._show_qr(url, f"{entry['name']} — {url}")
        self.url.setText(f"{entry['name']} ({entry['id']})")
        self._show_team(include_disk=True)
        self._drive_retry()
        return {"id": entry["id"], "name": entry["name"], "secret": entry["secret"], "url": url}

    def revoke_surveyor_qr(self, target, reason=""):
        from .server import PhotoStore

        dev_id, s_name, _names = self._resolve_target(target)
        key = dev_id or s_name or str(target or "").strip()
        if self.server is not None and hasattr(self.server.store, "revoke_surveyor_key"):
            res = self.server.store.revoke_surveyor_key(key, reason=reason)
        else:
            res = PhotoStore(self._active_folder(), self.form()).revoke_surveyor_key(key, reason=reason)
        self._show_team(include_disk=True)
        return res

    def _surveyor_qr_dialog(self):
        sel = self._selected_surveyors()
        default_name = sel[0]["name"] if sel and sel[0].get("name") not in (None, "", tr("Unknown"), tr("(no name)")) else ""
        sid = sel[0].get("device_id") if sel and str(sel[0].get("device_id") or "").startswith("srv_") else None
        name, ok = QInputDialog.getText(self, tr("Surveyor QR code…"), tr("Surveyor name:"), text=default_name)
        if ok and name.strip():
            info = self.issue_surveyor_qr(name.strip(), surveyor_id=sid, rotate=False)
            self.state.setText(tr("QR code ready for {who}.").format(who=info["name"]))

    def _replace_selected_surveyor_qr(self):
        sel = self._selected_surveyors()
        if not sel or not sel[0].get("name"):
            self.state.setText(tr("Select a surveyor in the list first."))
            return
        r = sel[0]
        sid = r.get("device_id") if str(r.get("device_id") or "").startswith("srv_") else None
        info = self.issue_surveyor_qr(r["name"], surveyor_id=sid, rotate=True)
        self.state.setText(tr("Issued a new QR code for {who}; their old QR code is now invalid.").format(who=info["name"]))


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
        seen_ids = getattr(self, "_seen_photo_ids", None)
        if seen_ids is None:
            self._seen_photo_ids = seen_ids = set()
        new_recs = [r for r in records if r["id"] not in seen_ids]
        for r in new_recs:
            seen_ids.add(r["id"])
        for r in records:
            if not r.get("route"):
                r["route"] = ("relay" if r.get("preview")
                              else {"local": "lan", "bridge": "direct", "internet": "tunnel", "own": "tunnel"}
                              .get(self.mode.currentData(), ""))
            self._photo_records[r["id"]] = r
            dev = r.get("device_id") or ""
            if dev and dev in self.team:
                self.team[dev]["at"] = time.time()
                if r.get("by"):
                    self.team[dev]["pos"]["name"] = r["by"]
                if not self.team[dev]["pos"].get("route") and r.get("route"):
                    self.team[dev]["pos"]["route"] = r["route"]
        self._show_team()
        self.received += len(new_recs)
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
        prev_tag = (" · " + tr("preview")) if r.get("preview") else ""
        self.last_info.setText(" · ".join(x for x in (r.get("kind"), r.get("taken", "").replace("T", " ")[:19]) if x)
                               + acc + prev_tag + (f"\n{r['note']}" if r.get("note") else "") + self._answers(r))
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

    # ---- Google Drive intake & handover key backup -------------------------------------------------------------------
    def _cert_folder(self):
        return os.path.join(QgsApplication.qgisSettingsDirPath(), "biruni")

    def drive_enabled(self):
        return bool(QgsProject.instance().readBoolEntry(P_SCOPE, P_DRIVE, False)[0])

    def _drive_roots(self):
        try:
            raw = json.loads(QgsSettings().value(DRIVE_ROOTS, "") or "[]")
        except (TypeError, ValueError):
            return []
        if not isinstance(raw, list):
            return []
        out, seen = [], set()
        for p in raw:
            p = str(p or "").strip()
            if not p:
                continue
            key = os.path.normcase(os.path.normpath(p))
            if key not in seen:
                seen.add(key)
                out.append(p)
        return out

    def _set_drive_roots(self, roots):
        out, seen = [], set()
        for p in roots or ():
            p = str(p or "").strip()
            if not p:
                continue
            key = os.path.normcase(os.path.normpath(p))
            if key not in seen:
                seen.add(key)
                out.append(p)
        QgsSettings().setValue(DRIVE_ROOTS, json.dumps(out))
        self.drive_list.clear()
        self.drive_list.addItems(out)
        if self.drive is not None:
            self.drive.roots = list(out)
        if not out:
            self.drive_state.setText(tr("Add the folder Google Drive for desktop syncs to, for example G:\\My Drive."))
            self.drive_state.setToolTip("")
        elif self.server is None:
            self.drive_state.setText(tr("Google Drive is checked while Field Photos is started."))
            self.drive_state.setToolTip("")

    def _add_drive_root(self):
        d = QFileDialog.getExistingDirectory(self, tr("Google Drive folder on this computer"), os.path.expanduser("~"))
        if not d:
            return
        self._set_drive_roots(self._drive_roots() + [d])
        if self.server is not None and self.drive is None and self.drive_enabled():
            self._start_drive()

    def _remove_drive_root(self):
        rows = sorted({idx.row() for idx in self.drive_list.selectedIndexes()}, reverse=True)
        if not rows:
            r = self.drive_list.currentRow()
            if r >= 0:
                rows = [r]
        if not rows:
            return
        roots = self._drive_roots()
        for r in rows:
            if 0 <= r < len(roots):
                roots.pop(r)
        self._set_drive_roots(roots)

    def _show_drive_idle_state(self, roots=None):
        if roots is None:
            roots = self._drive_roots()
        if not roots:
            self.drive_state.setText(tr("Add the folder Google Drive for desktop syncs to, for example G:\\My Drive."))
        else:
            self.drive_state.setText(tr("Google Drive is checked while Field Photos is started."))
        self.drive_state.setToolTip("")

    def _on_drive_toggled(self, on):
        proj = QgsProject.instance()
        if on:
            if not proj.readBoolEntry(P_SCOPE, P_NOTICE, False)[0]:
                if not self._confirm_drive():
                    self.drive_box.blockSignals(True)
                    self.drive_box.setChecked(False)
                    self.drive_box.blockSignals(False)
                    return
            proj.writeEntryBool(P_SCOPE, P_DRIVE, True)
            proj.writeEntryBool(P_SCOPE, P_NOTICE, True)
            proj.setDirty(True)
            if not self._drive_roots():
                from . import drive

                suggested = drive.suggest_roots()
                if suggested:
                    self._set_drive_roots(suggested)
            roots = self._drive_roots()
            self.drive_list.clear()
            self.drive_list.addItems(roots)
            self.drive_frame.setVisible(True)
            self._show_key_state()
            if not self._is_key_backed_up():
                self._nudge_backup()
            if self.server is not None:
                self._start_drive()
            if self.drive is None:
                self._show_drive_idle_state(roots)
        else:
            proj.removeEntry(P_SCOPE, P_DRIVE)
            proj.setDirty(True)
            self.drive_frame.setVisible(False)
            self._stop_drive()

    def _confirm_drive(self):
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Question)
        box.setWindowTitle(tr("Google Drive for this project"))
        box.setText(tr(
            "With this on, phones may send this project's photos through Google Drive.\n\n"
            "Each photo, with its location and notes, is encrypted on the phone for this computer. Google and "
            "anyone who can see the Drive folder cannot open it. The file's time, size and the phone's ID are not "
            "hidden.\n\n"
            "The encrypted files are kept in the Google account the phone signs in to, on Google's servers, which "
            "may be outside Saudi Arabia. If this project's data must stay in the Kingdom (GEOSA rules), get approval "
            "from whoever is responsible first.\n\n"
            "This is for this project only; it stays off for every other project."
        ))
        on_btn = box.addButton(tr("Turn on"), QMessageBox.ButtonRole.AcceptRole)
        cancel_btn = box.addButton(tr("Cancel"), QMessageBox.ButtonRole.RejectRole)
        box.setDefaultButton(cancel_btn)
        box.exec()
        return box.clickedButton() is on_btn

    def _nudge_backup(self):
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Information)
        box.setWindowTitle(tr("Back up this computer's key"))
        box.setText(tr(
            "Photos sent through Google Drive are locked for this computer's key. If the key is lost (Windows "
            "reinstalled, disk replaced, another user account), photos still waiting in Google Drive can never be "
            "opened by anyone. The backup is one small file protected by a password you choose. Keep it away from "
            "this computer."
        ))
        now_btn = box.addButton(tr("Back up now…"), QMessageBox.ButtonRole.AcceptRole)
        box.addButton(tr("Later"), QMessageBox.ButtonRole.RejectRole)
        box.setDefaultButton(now_btn)
        box.exec()
        if box.clickedButton() is now_btn:
            self._backup_key()

    def _sync_drive(self):
        on = self.drive_enabled()
        self.drive_box.blockSignals(True)
        self.drive_box.setChecked(on)
        self.drive_box.blockSignals(False)
        self.drive_frame.setVisible(on)
        roots = self._drive_roots()
        self.drive_list.clear()
        self.drive_list.addItems(roots)
        if on:
            self._show_key_state()
        if self.server is not None:
            if on and roots:
                self._start_drive()
            else:
                self._stop_drive()
                if on:
                    self._show_drive_idle_state(roots)
        elif on:
            self._show_drive_idle_state(roots)

    def _start_drive(self):
        from . import bridge, drive

        roots = self._drive_roots()
        if not self.drive_enabled() or not roots or self.server is None or self.drive is not None:
            return
        k = bridge.load_key(QgsSettings())
        self._drive_last = {"new": 0, "refused": 0, "reasons": []}
        self.drive = drive.DriveIntake(
            roots, self.server.store, self.server.handover, k, self.server.events, interval=drive.INTERVAL
        )
        self.drive.start()
        self.drive_state.setText(tr("Checking Google Drive…"))
        self.drive_state.setToolTip("")

    def _stop_drive(self):
        if self.drive is not None:
            self.drive.stop()
            self.drive = None
        if self.drive_enabled():
            self._show_drive_idle_state(self._drive_roots())

    def _drive_retry(self):
        if self.drive is not None:
            self.drive.retry_refused()
        else:
            from . import drive

            drive.forget_refused(self._active_folder())

    def _on_drive(self, ev):
        if "error" in ev:
            self.drive_state.setText(tr("Google Drive could not be checked: {e}").format(e=ev.get("error") or ""))
            self.drive_state.setToolTip("")
            return
        if int(ev.get("folders") or 0) == 0:
            from . import bridge, drive

            k = self.drive.k if self.drive is not None else bridge.load_key(QgsSettings())
            self.drive_state.setText(
                tr("No Biruni folder in Google Drive yet ({name}). It appears after the first phone sends.")
                .format(name=drive.folder_name(k))
            )
            self.drive_state.setToolTip("")
            return
        if self._drive_last is None:
            self._drive_last = {"new": 0, "refused": 0, "reasons": []}
        st = self._drive_last
        st["new"] += int(ev.get("new") or 0)
        refused = ev.get("refused") or []
        st["refused"] += len(refused)
        for item in refused:
            if isinstance(item, (list, tuple)) and len(item) >= 2:
                st["reasons"].append(f"{item[0]}: {item[1]}")
            else:
                st["reasons"].append(str(item))
        st["reasons"] = st["reasons"][-10:]
        at = ev.get("at")
        t = time.strftime("%H:%M", time.localtime(at if at is not None else time.time()))
        self.drive_state.setText(
            tr("Google Drive checked at {t}. Received: {n}. Refused: {r}.").format(t=t, n=st["new"], r=st["refused"])
        )
        self.drive_state.setToolTip("\n".join(st["reasons"]))

    def _pc_kid(self):
        from . import drive
        from .handover import HandoverKey

        pub = self.server.handover.public if self.server is not None else HandoverKey(self._cert_folder()).public
        return drive.key_id(pub)

    def _key_backup_info(self):
        try:
            d = json.loads(QgsSettings().value(KEY_BACKUP, "") or "{}")
        except (TypeError, ValueError):
            return {}
        return d if isinstance(d, dict) else {}

    def _record_key_backup(self, kid, at=None):
        QgsSettings().setValue(KEY_BACKUP, json.dumps({"kid": kid, "at": at or time.strftime("%Y-%m-%d")}))

    def _is_key_backed_up(self):
        info = self._key_backup_info()
        return bool(info.get("kid") and info.get("kid") == self._pc_kid())

    def _show_key_state(self):
        if not self.drive_enabled():
            return
        info = self._key_backup_info()
        if info.get("kid") == self._pc_kid():
            self.key_hint.setText(tr("Key backed up on {date}.").format(date=info.get("at") or ""))
        else:
            self.key_hint.setText(tr(
                "This computer's key is not backed up. If it is lost, photos waiting in Google Drive can never be "
                "opened."
            ))

    def _secure_plugin(self):
        try:
            t = self.plugin.theme.tokens()
            if isinstance(t, dict) and "window" in t and "icon_disabled" in t:
                return self.plugin
        except Exception:  # noqa: BLE001
            pass
        return None

    def _ask_backup_password(self):
        from ..secure import passwords, widgets

        p = self._secure_plugin()
        dlg = QDialog(self)
        dlg.setWindowTitle(tr("Back up this computer's key"))
        widgets.style_dialog(dlg, p)
        v = QVBoxLayout(dlg)
        v.setContentsMargins(18, 16, 18, 16)
        v.setSpacing(8)
        v.addWidget(widgets.header(tr("Back up this computer's key"),
                                   tr("Nobody can recover this password."), "key", p))
        v.addWidget(widgets.label(tr("Password"), "SecSub"))
        pw1 = widgets.PasswordField(tr("Password"), p)
        v.addWidget(pw1)
        meter = widgets.StrengthMeter(passwords.MIN_USER)
        v.addWidget(meter)
        v.addWidget(widgets.label(tr("Repeat the password"), "SecSub"))
        pw2 = widgets.PasswordField(tr("Repeat the password"), p)
        v.addWidget(pw2)
        err = widgets.label("", "SecError")
        err.hide()
        v.addWidget(err)
        row = QHBoxLayout()
        row.addStretch(1)
        cancel = QPushButton(tr("Cancel"))
        ok = widgets.primary(tr("Back up…"))
        ok.setEnabled(False)
        row.addWidget(cancel)
        row.addWidget(ok)
        v.addLayout(row)

        def refresh(*_):
            good = meter.update_for(pw1.text())
            same = bool(pw1.text()) and pw1.text() == pw2.text()
            if pw2.text() and not same:
                err.setText(tr("The two passwords do not match."))
                err.show()
            else:
                err.hide()
            ok.setEnabled(bool(good and same))

        pw1.changed.connect(refresh)
        pw2.changed.connect(refresh)
        cancel.clicked.connect(dlg.reject)
        ok.clicked.connect(dlg.accept)
        dlg.resize(440, 0)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return None
        return pw1.text()

    def _backup_key(self):
        from . import drive, handover

        pw = self._ask_backup_password()
        if not pw:
            return
        docs = os.path.join(os.path.expanduser("~"), "Documents")
        base_dir = docs if os.path.isdir(docs) else os.path.expanduser("~")
        kid = self._pc_kid()
        default_path = os.path.join(base_dir, f"biruni-photos-key-{kid}.biruni-key")
        path, _ = QFileDialog.getSaveFileName(
            self, tr("Back up this computer's key"), default_path, tr("Biruni key backup (*.biruni-key)")
        )
        if not path:
            return
        try:
            pub = handover.backup(self._cert_folder(), path, pw)
        except (OSError, ValueError) as e:
            QMessageBox.warning(self, tr("Back up this computer's key"), tr("It could not be done: {e}").format(e=e))
            return
        self._record_key_backup(drive.key_id(pub))
        self._show_key_state()
        self.drive_state.setText(tr("Key backed up."))

    def _restore_key(self):
        from . import drive, handover
        from ..secure import widgets

        if self.server is not None:
            QMessageBox.warning(self, tr("Restore key…"), tr("Stop Field Photos first."))
            return
        docs = os.path.join(os.path.expanduser("~"), "Documents")
        base_dir = docs if os.path.isdir(docs) else os.path.expanduser("~")
        path, _ = QFileDialog.getOpenFileName(
            self, tr("Restore key…"), base_dir, tr("Biruni key backup (*.biruni-key);;All files (*)")
        )
        if not path:
            return
        try:
            info = handover.backup_info(path)
        except ValueError:
            QMessageBox.warning(self, tr("Restore key…"), tr("Wrong password, or not a Biruni key backup."))
            return
        pw = widgets.ask_password(self, tr("Restore key…"), os.path.basename(path), self._secure_plugin())
        if pw is None:
            return
        cur_pub = handover.HandoverKey(self._cert_folder()).public
        if info["pub"] != cur_pub:
            ans = QMessageBox.question(
                self, tr("Restore key…"),
                tr("This replaces this computer's key. Phones must scan the code again; the present key is kept "
                   "beside it as a file.")
            )
            if ans != QMessageBox.StandardButton.Yes:
                return
        try:
            pub = handover.restore(self._cert_folder(), path, pw)
        except ValueError:
            QMessageBox.warning(self, tr("Restore key…"), tr("Wrong password, or not a Biruni key backup."))
            return
        drive.forget_refused(self._active_folder())
        self._record_key_backup(drive.key_id(pub))
        self._show_key_state()
        self.drive_state.setText(tr("Key restored."))


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


def on_project_changed(plugin):
    d = _DOCK.get(id(plugin))
    if d is not None:
        d._sync_drive()
