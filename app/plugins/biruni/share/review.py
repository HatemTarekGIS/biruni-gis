"""Map review without accounts: send a map for review as one HTML file, bring the comments back.

The reviewer opens the page in any browser (a phone too), taps a feature or any
place, or sketches a short line or area, and writes a comment (note, error,
question, approved). Comments stay in the browser and are sent back as one small
file - by WhatsApp, e-mail, anything. Biruni reads those files into the
`review_comments` layer of the project database, where each comment gets a status
and a reply; the replies go back to the reviewer as a file their page can open.

Everything that comes back is treated as untrusted: the files are checked
against a strict format and every text is escaped when it is shown again.
"""
import base64
import html
import json
import math
import os
import re
import uuid
from datetime import datetime, timezone

from qgis.core import (Qgis, QgsCoordinateReferenceSystem, QgsCoordinateTransform, QgsEditorWidgetSetup, QgsFeature,
                       QgsFeatureRequest, QgsField, QgsFields, QgsGeometry, QgsLineSymbol, QgsMarkerSymbol,
                       QgsPalLayerSettings, QgsProject, QgsRectangle, QgsRuleBasedRenderer, QgsTextBufferSettings,
                       QgsTextFormat, QgsVectorFileWriter, QgsVectorLayer, QgsVectorLayerSimpleLabeling, QgsWkbTypes)
from qgis.PyQt.QtCore import QDate, QDateTime, QMetaType
from qgis.PyQt.QtGui import QColor

from .. import i18n
from ..i18n import tr
from ..version import APP_NAME
from .styles import ExpressionEvaluator, LayerStyler, geometry_kind, label_expression
from .webmap import BASEMAPS, MAX_LABELS, WEB_DIR, json_value, popup_fields

FORMAT = "biruni-review"
REPLIES_FORMAT = "biruni-review-replies"
VERSION = 1
MAX_FILE_BYTES = 5 * 1024 * 1024
MAX_COMMENTS = 5000
MAX_TEXT = 4000
MAX_NAME = 80
MAX_REF = 200
MAX_VERTICES = 500
ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f‪-‮⁦-⁩]")

CATEGORY_KEYS = ("note", "error", "question", "ok")
STATUS_KEYS = ("new", "in_progress", "resolved", "rejected")
CLOSED = ("resolved", "rejected")
COLORS = {"note": "#2f7ed8", "error": "#d64545", "question": "#e8901c", "ok": "#2e9d5b"}
ROLE_KEY = "biruni/role"
COMMENTS_TABLE, SKETCHES_TABLE = "review_comments", "review_sketches"
WGS84 = QgsCoordinateReferenceSystem("EPSG:4326")

SATELLITE = {"url": "https://tiles.maps.eox.at/wmts/1.0.0/s2cloudless_3857/default/g/{z}/{y}/{x}.jpg",
             "maxZoom": 19, "maxNativeZoom": 14,
             "attribution": '<a href="https://s2maps.eu">Sentinel-2 cloudless - https://s2maps.eu</a> by EOX IT '
                            'Services GmbH (Contains modified Copernicus Sentinel data 2016)'}


class ReviewFileError(ValueError):
    """A review file that cannot be read at all."""


def category_labels():
    return {"note": tr("Note"), "error": tr("Error"), "question": tr("Question"), "ok": tr("Approved")}


def status_labels():
    return {"new": tr("New"), "in_progress": tr("In progress"), "resolved": tr("Resolved"),
            "rejected": tr("Rejected")}


# ---- the review page --------------------------------------------------------------------------------------------

def _layer_data(layer, project, labels=True, extent=None):
    """The layer as GeoJSON (WGS84) with its style, popup values and a reference per feature."""
    kind = geometry_kind(layer)
    xform = QgsCoordinateTransform(layer.crs(), WGS84, project.transformContext())
    fields = popup_fields(layer)
    label_of = ExpressionEvaluator(layer, label_expression(layer) if labels else None)
    request = QgsFeatureRequest()
    if extent is not None:
        request.setFilterRect(extent)
    feats, n_labels = [], 0
    bounds = QgsRectangle()
    bounds.setNull()
    with LayerStyler(layer) as styler:
        for f in layer.getFeatures(request):
            g = f.geometry()
            if g is None or g.isNull() or g.isEmpty():
                continue
            s = styler.style_index(f)
            if s is None:
                continue
            g = QgsGeometry(g)
            if QgsWkbTypes.isCurvedType(g.wkbType()):
                g = QgsGeometry(g.constGet().segmentize())
            try:
                g.transform(xform)
            except Exception:
                continue
            bounds.combineExtentWith(g.boundingBox())
            props = {"s": s, "k": f.id(), "v": [json_value(f.attribute(i)) for i, _ in fields]}
            if label_of.expr is not None and n_labels < MAX_LABELS:
                lbl = label_of(f)
                if lbl:
                    props["l"] = lbl
                    n_labels += 1
            feats.append({"type": "Feature", "properties": props, "geometry": json.loads(g.asJson(6))})
        styles = styler.styles
    return {"name": layer.name(), "id": layer.id(), "kind": kind, "fields": [n for _, n in fields],
            "styles": styles or [{}], "geojson": {"type": "FeatureCollection", "features": feats}}, bounds


def _page_text():
    """Every text of the review page, translated."""
    return {
        "made": tr("Made with {app}").format(app=APP_NAME),
        "layers": tr("Layers"),
        "map": tr("Map"), "satellite": tr("Satellite"), "topo": tr("Topographic"), "nobase": tr("No basemap"),
        "offline": tr("The background map needs the internet - your comments still work."),
        "hint": tr("Tap a feature or any place on the map to comment."),
        "line": tr("Sketch a line"), "area": tr("Sketch an area"), "comments": tr("Comments"), "send": tr("Send"),
        "here": tr("Comment here"), "on_feature": tr("Comment on this feature"),
        "finish": tr("Finish"), "undo_point": tr("Undo point"), "cancel": tr("Cancel"),
        "draw_line": tr("Tap the map to draw the line, then Finish."),
        "draw_area": tr("Tap the map to draw the area, then Finish."),
        "need_points": tr("Add more points first."),
        "category": tr("Type"), "note": tr("Note"), "error": tr("Error"), "question": tr("Question"),
        "ok": tr("Approved"), "comment": tr("Your comment"), "save": tr("Save"), "delete": tr("Delete"),
        "confirm_delete": tr("Delete this comment?"), "empty": tr("Write the comment first."),
        "your_name": tr("Your name"), "name_hint": tr("Your name is shown with your comments."),
        "start": tr("Start"), "continue": tr("Continue"), "edit_name": tr("Change name"),
        "instructions": tr("Instructions"), "deadline": tr("Please reply by {date}"),
        "how": tr("Tap a feature or any place on the map and write what you see. When you finish, press Send and "
                  "send the file to whoever asked for the review - by WhatsApp or e-mail."),
        "none_yet": tr("No comments yet."),
        "export": tr("Download file"), "copy": tr("Copy text"), "share": tr("Share (WhatsApp…)"),
        "replies": tr("Open replies file"), "copied": tr("Copied. Paste it in a message."),
        "copy_manual": tr("Select all the text below and copy it."),
        "share_unavailable": tr("Sharing is not available in this browser: the file is downloaded instead."),
        "send_title": tr("Send your comments"),
        "send_text": tr("Send this file to whoever asked for the review. Your comments stay on this device too."),
        "no_storage": tr("This browser does not keep the comments if you close the page: send them before "
                         "closing."),
        "unsent": tr("You have comments that were not sent yet."),
        "status": tr("Status"), "reply": tr("Reply"),
        "new": tr("New"), "in_progress": tr("In progress"), "resolved": tr("Resolved"), "rejected": tr("Rejected"),
        "replies_loaded": tr("{n} replies loaded."), "replies_other": tr("These replies belong to another review."),
        "bad_file": tr("This is not a replies file of this review."),
        "close": tr("Close"), "feature": tr("Feature"),
    }


def export_review_page(layers, path, title="", instructions="", deadline=None, basemap="osm", options=None):
    """Write the review page for `layers` (vector layers, top of the map first). Returns (path, review_id).

    deadline: QDate, datetime.date or text (optional). options: labels (default True), extent
    (QgsReferencedRectangle), project (QgsProject), review_id (to reissue a page of an existing review)."""
    options = options or {}
    project = options.get("project") or QgsProject.instance()
    extent = options.get("extent")
    out_layers = []
    total = QgsRectangle()
    total.setNull()
    for lyr in layers:
        if not isinstance(lyr, QgsVectorLayer) or not lyr.isSpatial() or geometry_kind(lyr) is None:
            continue
        rect = None
        if extent is not None:
            rect = QgsCoordinateTransform(extent.crs(), lyr.crs(), project.transformContext()) \
                .transformBoundingBox(extent)
        data, b = _layer_data(lyr, project, options.get("labels", True), rect)
        out_layers.append(data)
        if not b.isNull():
            total.combineExtentWith(b)
    if not out_layers:
        raise ValueError(tr("Choose at least one vector layer."))
    if isinstance(deadline, QDate):
        deadline = deadline.toString("yyyy-MM-dd") if deadline.isValid() else None
    elif deadline is not None and not isinstance(deadline, str):
        deadline = deadline.isoformat()
    review_id = options.get("review_id") or uuid.uuid4().hex[:16]
    bases = {"osm": BASEMAPS["osm"], "topo": BASEMAPS["topo"], "satellite": SATELLITE}
    payload = {
        "format": FORMAT, "version": VERSION, "review_id": review_id,
        "title": title or "", "instructions": instructions or "", "deadline": deadline or "",
        "layers": out_layers, "basemaps": bases, "basemap": basemap if basemap in bases else "none",
        "bounds": None if total.isNull() else [[total.yMinimum(), total.xMinimum()],
                                                [total.yMaximum(), total.xMaximum()]],
        "rtl": i18n.is_rtl(), "colors": COLORS, "categories": list(CATEGORY_KEYS),
        "limits": {"text": MAX_TEXT, "name": MAX_NAME, "vertices": MAX_VERTICES},
        "text": _page_text(),
    }
    # "<" never appears raw inside the data block, so nothing in it can close the script element
    blob = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).replace("<", "\\u003c")
    with open(os.path.join(WEB_DIR, "leaflet.js"), encoding="utf-8") as f:
        leaflet_js = f.read()
    with open(os.path.join(WEB_DIR, "leaflet.css"), encoding="utf-8") as f:
        leaflet_css = f.read()
    for img in ("layers-2x.png", "layers.png"):
        with open(os.path.join(WEB_DIR, img), "rb") as f:
            uri = "data:image/png;base64," + base64.b64encode(f.read()).decode("ascii")
        leaflet_css = leaflet_css.replace("images/" + img, uri)
    page = (TEMPLATE
            .replace("%%LANG%%", "ar" if i18n.is_rtl() else i18n.lang() or "en")
            .replace("%%DIR%%", "rtl" if i18n.is_rtl() else "ltr")
            .replace("%%TITLE%%", html.escape(title or tr("Map review")))
            .replace("%%LEAFLET_CSS%%", leaflet_css)
            .replace("%%LEAFLET_JS%%", leaflet_js.replace("</script", "<\\/script"))
            .replace("%%DATA%%", blob))
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(page)
    return path, review_id


# ---- reading review files ---------------------------------------------------------------------------------------

def _clean(value, limit):
    """A text from outside: str only, no control or direction-override characters, at most `limit` long.
    Returns (text, was_cut)."""
    if value is None:
        return "", False
    if not isinstance(value, str):
        raise ValueError("not a text")
    value = CONTROL_RE.sub("", value).strip()
    return (value[:limit], True) if len(value) > limit else (value, False)


def _number(v):
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v):
        raise ValueError("not a number")
    return float(v)


def _position(p):
    if not isinstance(p, (list, tuple)) or len(p) < 2:
        raise ValueError("bad position")
    x, y = _number(p[0]), _number(p[1])
    if not (-180 <= x <= 180 and -90 <= y <= 90):
        raise ValueError("position outside the world")
    return [x, y]


def _geometry(g):
    """A GeoJSON Point, LineString or Polygon (one ring) in WGS84, checked; returns a QgsGeometry."""
    if not isinstance(g, dict):
        raise ValueError("no geometry")
    kind, coords = g.get("type"), g.get("coordinates")
    if kind == "Point":
        x, y = _position(coords)
        return QgsGeometry.fromWkt(f"POINT({x!r} {y!r})")
    if kind == "LineString":
        if not isinstance(coords, list) or not 2 <= len(coords) <= MAX_VERTICES:
            raise ValueError("bad line")
        pts = [_position(p) for p in coords]
        return QgsGeometry.fromWkt("LINESTRING(" + ",".join(f"{x!r} {y!r}" for x, y in pts) + ")")
    if kind == "Polygon":
        if not isinstance(coords, list) or len(coords) != 1 or not isinstance(coords[0], list):
            raise ValueError("bad area")
        ring = [_position(p) for p in coords[0]]
        if ring and ring[0] != ring[-1]:
            ring.append(ring[0])
        if not 4 <= len(ring) <= MAX_VERTICES + 1:
            raise ValueError("bad area")
        return QgsGeometry.fromWkt("POLYGON((" + ",".join(f"{x!r} {y!r}" for x, y in ring) + "))")
    raise ValueError("unknown geometry")


def _time(s):
    if not isinstance(s, str) or len(s) > 40:
        return None
    try:
        dt = datetime.fromisoformat(s.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return QDateTime.fromSecsSinceEpoch(int(dt.timestamp()))


def _comment(c, file_author):
    """One comment checked and normalised, or ValueError."""
    if not isinstance(c, dict):
        raise ValueError("not an object")
    cid = c.get("id")
    if not isinstance(cid, str) or not ID_RE.match(cid):
        raise ValueError("bad id")
    text, cut = _clean(c.get("text"), MAX_TEXT)
    if not text:
        raise ValueError("empty comment")
    author, _ = _clean(c.get("author") or file_author, MAX_NAME)
    category = c.get("category")
    category = category if category in CATEGORY_KEYS else "note"
    geom = _geometry(c.get("geometry"))
    feature = c.get("feature") if isinstance(c.get("feature"), dict) else {}
    layer_ref, _ = _clean(feature.get("layer"), MAX_REF)
    layer_id, _ = _clean(feature.get("layer_id"), MAX_REF)
    fid = feature.get("fid")
    fid = int(fid) if isinstance(fid, (int, float)) and not isinstance(fid, bool) and math.isfinite(fid) else None
    label, _ = _clean(feature.get("label") if isinstance(feature.get("label"), str) else None, MAX_REF)
    zoom = c.get("zoom")
    zoom = float(zoom) if isinstance(zoom, (int, float)) and not isinstance(zoom, bool) and 0 <= zoom <= 30 else None
    return {"id": cid, "author": author, "created": _time(c.get("created")), "updated": _time(c.get("updated")),
            "category": category, "text": text, "cut": cut, "geometry": geom, "layer_ref": layer_ref,
            "layer_id": layer_id, "fid": fid, "label": label, "zoom": zoom}


def parse_review_file(path):
    """(meta, comments, warnings) of a review file sent back by a reviewer. Raises ReviewFileError when the
    file is not a review file at all; single bad comments are skipped with a warning."""
    name = os.path.basename(path)
    try:
        size = os.path.getsize(path)
    except OSError as e:
        raise ReviewFileError(tr("{f}: the file could not be opened ({e}).").format(f=name, e=e)) from e
    if size > MAX_FILE_BYTES:
        raise ReviewFileError(tr("{f}: the file is too large for a review file.").format(f=name))
    try:
        with open(path, "rb") as f:
            data = json.loads(f.read().decode("utf-8-sig"))
    except (UnicodeDecodeError, ValueError) as e:
        raise ReviewFileError(tr("{f}: this is not a review file.").format(f=name)) from e
    if not isinstance(data, dict) or data.get("format") != FORMAT:
        raise ReviewFileError(tr("{f}: this is not a review file.").format(f=name))
    version = data.get("version")
    if not isinstance(version, int) or isinstance(version, bool) or version < 1:
        raise ReviewFileError(tr("{f}: this is not a review file.").format(f=name))
    if version > VERSION:
        raise ReviewFileError(tr("{f}: made by a newer version of {app} - update {app} first.")
                              .format(f=name, app=APP_NAME))
    comments = data.get("comments")
    if not isinstance(comments, list):
        raise ReviewFileError(tr("{f}: this is not a review file.").format(f=name))
    warnings = []
    if len(comments) > MAX_COMMENTS:
        warnings.append(tr("{f}: only the first {n} comments were read.").format(f=name, n=MAX_COMMENTS))
        comments = comments[:MAX_COMMENTS]
    review_id = data.get("review_id") if isinstance(data.get("review_id"), str) and \
        ID_RE.match(data.get("review_id") or "") else ""
    try:
        author, _ = _clean(data.get("author"), MAX_NAME)
    except ValueError:
        author = ""
    try:
        title, _ = _clean(data.get("title"), MAX_REF)
    except ValueError:
        title = ""
    out, seen = [], set()
    for k, c in enumerate(comments, 1):
        try:
            item = _comment(c, author)
        except ValueError:
            warnings.append(tr("{f}: comment {n} was skipped (not readable).").format(f=name, n=k))
            continue
        if item["id"] in seen:
            continue
        seen.add(item["id"])
        if item["cut"]:
            warnings.append(tr("{f}: comment {n} was too long and was shortened.").format(f=name, n=k))
        out.append(item)
    return {"review_id": review_id, "author": author, "title": title, "file": name}, out, warnings


# ---- the review layers -------------------------------------------------------------------------------------------

def _comment_fields():
    fields = QgsFields()
    for name, typ, length in (("comment_id", QMetaType.Type.QString, 64), ("review_id", QMetaType.Type.QString, 32),
                              ("author", QMetaType.Type.QString, MAX_NAME),
                              ("created", QMetaType.Type.QDateTime, 0), ("updated", QMetaType.Type.QDateTime, 0),
                              ("category", QMetaType.Type.QString, 16), ("text", QMetaType.Type.QString, 0),
                              ("layer_ref", QMetaType.Type.QString, MAX_REF),
                              ("feature_ref", QMetaType.Type.QString, 32),
                              ("feature_label", QMetaType.Type.QString, MAX_REF),
                              ("status", QMetaType.Type.QString, 16), ("reply", QMetaType.Type.QString, 0),
                              ("source_file", QMetaType.Type.QString, 255)):
        fields.append(QgsField(name, typ, "", length))
    return fields


def _sketch_fields():
    fields = QgsFields()
    for name, length in (("comment_id", 64), ("author", MAX_NAME), ("category", 16), ("text", 0)):
        fields.append(QgsField(name, QMetaType.Type.QString, "", length))
    return fields


def _find(project, role):
    for lyr in project.mapLayers().values():
        if isinstance(lyr, QgsVectorLayer) and lyr.customProperty(ROLE_KEY) == role and lyr.isValid():
            return lyr
    return None


def _open_or_create(project, role, table, geom, fields, title):
    from ..project import project_gpkg

    layer = _find(project, role)
    if layer is not None:
        return layer, False
    gpkg = project_gpkg(project)
    if gpkg:
        layer = QgsVectorLayer(f"{gpkg}|layername={table}", title, "ogr")
        if not layer.isValid():
            mem = QgsVectorLayer(f"{geom}?crs=EPSG:4326", table, "memory")
            mem.dataProvider().addAttributes(fields.toList())
            mem.updateFields()
            opts = QgsVectorFileWriter.SaveVectorOptions()
            opts.driverName, opts.layerName, opts.fileEncoding = "GPKG", table, "UTF-8"
            opts.actionOnExistingFile = QgsVectorFileWriter.ActionOnExistingFile.CreateOrOverwriteLayer
            err, msg, *_ = QgsVectorFileWriter.writeAsVectorFormatV3(mem, gpkg, project.transformContext(), opts)
            if err != QgsVectorFileWriter.WriterError.NoError:
                raise RuntimeError(msg)
            layer = QgsVectorLayer(f"{gpkg}|layername={table}", title, "ogr")
        if not layer.isValid():
            layer = None
    if layer is None:
        layer = QgsVectorLayer(f"{geom}?crs=EPSG:4326", title, "memory")
        layer.dataProvider().addAttributes(fields.toList())
        layer.updateFields()
    layer.setCustomProperty(ROLE_KEY, role)
    return layer, True


def _value_map(labels):
    return QgsEditorWidgetSetup("ValueMap", {"map": [{label: key} for key, label in labels.items()]})


def style_review_layer(layer):
    """Coloured pins by type (closed comments grey), short labels, readable choices for type and status."""
    root = QgsRuleBasedRenderer.Rule(None)
    closed = ", ".join(f"'{s}'" for s in CLOSED)

    def pin(color):
        return QgsMarkerSymbol.createSimple({"name": "circle", "color": color, "size": "4.2",
                                             "outline_color": "#ffffff", "outline_width": "0.6"})

    rule = QgsRuleBasedRenderer.Rule(pin("#9aa4ae"), 0, 0, f'"status" IN ({closed})', tr("Closed"))
    root.appendChild(rule)
    for key, label in category_labels().items():
        root.appendChild(QgsRuleBasedRenderer.Rule(
            pin(COLORS[key]), 0, 0, f'"category" = \'{key}\' AND coalesce("status", \'new\') NOT IN ({closed})',
            label))
    root.appendChild(QgsRuleBasedRenderer.Rule(pin(COLORS["note"]), 0, 0, "ELSE", tr("Other")))
    layer.setRenderer(QgsRuleBasedRenderer(root))
    pal = QgsPalLayerSettings()
    pal.isExpression = True
    pal.fieldName = "coalesce(\"author\", '') || ': ' || left(coalesce(\"text\", ''), 40)"
    fmt = QgsTextFormat()
    fmt.setSize(8.5)
    buf = QgsTextBufferSettings()
    buf.setEnabled(True)
    buf.setSize(0.8)
    buf.setColor(QColor("#ffffff"))
    fmt.setBuffer(buf)
    pal.setFormat(fmt)
    layer.setLabeling(QgsVectorLayerSimpleLabeling(pal))
    layer.setLabelsEnabled(True)
    idx = layer.fields().indexOf
    if idx("category") >= 0:
        layer.setEditorWidgetSetup(idx("category"), _value_map(category_labels()))
    if idx("status") >= 0:
        layer.setEditorWidgetSetup(idx("status"), _value_map(status_labels()))
    for name in ("text", "reply"):
        if idx(name) >= 0:
            layer.setEditorWidgetSetup(idx(name), QgsEditorWidgetSetup("TextEdit", {"IsMultiline": True}))
    for name, alias in (("author", tr("Reviewer")), ("created", tr("Written")), ("category", tr("Type")),
                        ("text", tr("Comment")), ("layer_ref", tr("Layer")), ("feature_label", tr("Feature")),
                        ("status", tr("Status")), ("reply", tr("Reply"))):
        if idx(name) >= 0:
            layer.setFieldAlias(idx(name), alias)


def _style_sketches(layer):
    root = QgsRuleBasedRenderer.Rule(None)
    for key, label in category_labels().items():
        sym = QgsLineSymbol.createSimple({"color": COLORS[key], "width": "0.8", "line_style": "dash"})
        root.appendChild(QgsRuleBasedRenderer.Rule(sym, 0, 0, f'"category" = \'{key}\'', label))
    root.appendChild(QgsRuleBasedRenderer.Rule(QgsLineSymbol.createSimple({"color": COLORS["note"], "width": "0.8"}),
                                               0, 0, "ELSE", tr("Other")))
    layer.setRenderer(QgsRuleBasedRenderer(root))


def ensure_review_layers(project=None, sketches=False):
    """(comments layer, sketches layer or None) of the project, created and added to the map when missing.
    They live in the project database when there is one, otherwise in memory."""
    project = project or QgsProject.instance()
    comments, new_c = _open_or_create(project, COMMENTS_TABLE, COMMENTS_TABLE, "Point", _comment_fields(),
                                      tr("Review comments"))
    sketch = None
    if sketches or _find(project, SKETCHES_TABLE) is not None:
        sketch, new_s = _open_or_create(project, SKETCHES_TABLE, SKETCHES_TABLE, "LineString", _sketch_fields(),
                                        tr("Review sketches"))
        if new_s:
            _style_sketches(sketch)
            project.addMapLayer(sketch)
    if new_c:
        style_review_layer(comments)
        project.addMapLayer(comments)
    return comments, sketch


class ImportResult:
    def __init__(self):
        self.new = 0
        self.updated = 0
        self.unchanged = 0
        self.by_author = {}
        self.warnings = []
        self.errors = []
        self.layer = None

    def summary(self):
        text = tr("{new} new comments, {upd} updated, {same} already there.").format(
            new=self.new, upd=self.updated, same=self.unchanged)
        if self.by_author:
            text += "\n" + "\n".join(f"{a or tr('Unknown')}: {n}" for a, n in
                                     sorted(self.by_author.items(), key=lambda x: -x[1]))
        return text


def _is_point(geom):
    return geom.type() == Qgis.GeometryType.Point


def _anchor(geom):
    """The pin of a comment: the point itself, the start of a line or a point inside an area."""
    if _is_point(geom):
        return geom
    if geom.type() == Qgis.GeometryType.Line:
        line = geom.asPolyline()
        return QgsGeometry.fromPointXY(line[0]) if line else geom.centroid()
    return geom.pointOnSurface()


def _outline(geom):
    if geom.type() == Qgis.GeometryType.Polygon:
        return QgsGeometry.fromPolylineXY(geom.asPolygon()[0])
    return geom


def import_reviews(paths, project=None):
    """Read review files into the review layers. Comments already there (same id) are updated when the
    reviewer changed them; their status and reply are kept."""
    project = project or QgsProject.instance()
    result = ImportResult()
    parsed = []
    for p in paths:
        try:
            parsed.append(parse_review_file(p))
        except ReviewFileError as e:
            result.errors.append(str(e))
    if not parsed:
        return result
    # the same comment may come in several files (sent twice): keep its latest version
    latest = {}
    for meta, comments, warnings in parsed:
        result.warnings.extend(warnings)
        for c in comments:
            prev = latest.get(c["id"])
            stamp = c["updated"] or c["created"]
            if prev is not None:
                old_stamp = prev[1]["updated"] or prev[1]["created"]
                if stamp is None or (old_stamp is not None and stamp < old_stamp):
                    continue
            latest[c["id"]] = (meta, c)
    need_sketch = any(not _is_point(c["geometry"]) for _, c in latest.values())
    layer, sketch = ensure_review_layers(project, sketches=need_sketch)
    result.layer = layer
    for lyr in (layer, sketch):
        if lyr is not None and lyr.isEditable():
            raise RuntimeError(tr("Save or stop editing the layer {name} first.").format(name=lyr.name()))
    fields = layer.fields()
    existing = {}
    for f in layer.getFeatures():
        if f["comment_id"]:
            existing[f["comment_id"]] = f
    sketch_ids = {}
    if sketch is not None:
        for f in sketch.getFeatures():
            sketch_ids[f["comment_id"]] = f.id()
    adds, sketch_adds = [], []
    xf_needed = layer.crs() != WGS84
    xform = QgsCoordinateTransform(WGS84, layer.crs(), project.transformContext()) if xf_needed else None
    sxform = (QgsCoordinateTransform(WGS84, sketch.crs(), project.transformContext())
              if sketch is not None and sketch.crs() != WGS84 else None)
    for meta, c in latest.values():
        pin = QgsGeometry(_anchor(c["geometry"]))
        if xform is not None:
            pin.transform(xform)
        values = {"comment_id": c["id"], "review_id": meta["review_id"], "author": c["author"],
                  "created": c["created"], "updated": c["updated"] or c["created"], "category": c["category"],
                  "text": c["text"], "layer_ref": c["layer_ref"],
                  "feature_ref": "" if c["fid"] is None else str(c["fid"]), "feature_label": c["label"],
                  "source_file": meta["file"]}
        old = existing.get(c["id"])
        if old is None:
            f = QgsFeature(fields)
            f.setGeometry(pin)
            for k, v in values.items():
                f[k] = v
            f["status"] = "new"
            adds.append(f)
            result.new += 1
            result.by_author[c["author"]] = result.by_author.get(c["author"], 0) + 1
        else:
            changes = {}
            for k in ("category", "text", "author", "layer_ref", "feature_ref", "feature_label"):
                if (old[k] or "") != (values[k] or ""):
                    changes[fields.indexOf(k)] = values[k]
            moved = old.geometry().isNull() or old.geometry().distance(pin) > 1e-9
            if changes or moved:
                changes[fields.indexOf("updated")] = values["updated"]
                changes[fields.indexOf("source_file")] = values["source_file"]
                layer.dataProvider().changeAttributeValues({old.id(): changes})
                if moved:
                    layer.dataProvider().changeGeometryValues({old.id(): pin})
                from .. import team

                team.notify_changed(layer, [old.id()])   # a layer shared in a team room: the others see it too
                result.updated += 1
                result.by_author[c["author"]] = result.by_author.get(c["author"], 0) + 1
            else:
                result.unchanged += 1
        if sketch is not None and not _is_point(c["geometry"]):
            line = QgsGeometry(_outline(c["geometry"]))
            if sxform is not None:
                line.transform(sxform)
            sid = sketch_ids.get(c["id"])
            sv = {"comment_id": c["id"], "author": c["author"], "category": c["category"], "text": c["text"]}
            if sid is None:  # GeoPackage layers start with their fid field: always write by name
                sf = QgsFeature(sketch.fields())
                sf.setGeometry(line)
                for k, v in sv.items():
                    sf[k] = v
                sketch_adds.append(sf)
            else:
                sfields = sketch.fields()
                sketch.dataProvider().changeGeometryValues({sid: line})
                sketch.dataProvider().changeAttributeValues({sid: {sfields.indexOf(k): v for k, v in sv.items()}})
    if adds:
        ok, out = layer.dataProvider().addFeatures(adds)
        if not ok:
            raise RuntimeError(tr("The comments could not be written: {e}").format(
                e="; ".join(layer.dataProvider().errors()[-3:])))
        from .. import team

        team.notify_added(layer, out)
    if sketch is not None and sketch_adds:
        sketch.dataProvider().addFeatures(sketch_adds)
    for lyr in (layer, sketch):
        if lyr is not None:
            lyr.updateExtents()
            lyr.triggerRepaint()
    return result


# ---- replies back to the reviewer -------------------------------------------------------------------------------

def export_replies(layer, path, review_id=None):
    """Write the status and reply of every comment to a file the reviewer opens in their review page.
    Returns the number of comments written."""
    replies, seen_ids = [], set()
    for f in layer.getFeatures():
        cid = f["comment_id"]
        if not cid or not ID_RE.match(str(cid)):
            continue
        if review_id and f["review_id"] and f["review_id"] != review_id:
            continue
        if f["review_id"]:
            seen_ids.add(f["review_id"])
        status = f["status"] if f["status"] in STATUS_KEYS else "new"
        replies.append({"id": cid, "status": status, "reply": (f["reply"] or "")[:MAX_TEXT] if isinstance(
            f["reply"], str) else ""})
    # one review: the page checks the id; several reviews mixed: no id, every page takes its own comments
    rid = review_id or (next(iter(seen_ids)) if len(seen_ids) == 1 else "")
    payload = {"format": REPLIES_FORMAT, "version": VERSION, "review_id": rid,
               "exported": datetime.now(timezone.utc).isoformat(timespec="seconds"), "replies": replies}
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        json.dump(payload, f, ensure_ascii=False, indent=1)
    return len(replies)


def review_ids(layer):
    """The reviews (ids) the comments of the layer belong to, most comments first."""
    counts = {}
    for f in layer.getFeatures():
        if f["review_id"]:
            counts[f["review_id"]] = counts.get(f["review_id"], 0) + 1
    return [k for k, _ in sorted(counts.items(), key=lambda x: -x[1])]


TEMPLATE = r"""<!doctype html>
<html lang="%%LANG%%" dir="%%DIR%%">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="generator" content="Biruni GIS">
<title>%%TITLE%%</title>
<style>%%LEAFLET_CSS%%</style>
<style>
html,body{height:100%;margin:0;font-family:"Segoe UI",Tahoma,Arial,sans-serif;background:#f4f6f8;color:#1d2733}
button{font:inherit;cursor:pointer}
#bar{position:absolute;top:0;left:0;right:0;height:52px;display:flex;align-items:center;gap:8px;padding:0 12px;
 background:#14365a;color:#fff;z-index:1000;box-shadow:0 1px 4px rgba(0,0,0,.25)}
#bar .t{flex:1;font-size:16px;font-weight:600;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
#bar button{background:rgba(255,255,255,.14);color:#fff;border:0;border-radius:6px;padding:7px 11px;font-size:14px;
 white-space:nowrap}
#bar button.main{background:#2e9d5b}
#bar .n{display:inline-block;min-width:18px;padding:0 5px;margin-inline-start:4px;border-radius:9px;background:#fff;
 color:#14365a;font-size:12px;font-weight:700}
#map{position:absolute;top:52px;bottom:0;left:0;right:0;direction:ltr}
#hint{position:absolute;top:62px;left:50%;transform:translateX(-50%);z-index:900;background:rgba(20,54,90,.9);
 color:#fff;padding:6px 14px;border-radius:16px;font-size:13px;max-width:90vw;text-align:center}
#note{position:absolute;bottom:26px;left:50%;transform:translateX(-50%);z-index:900;background:#fff3cd;
 color:#5c4400;padding:6px 12px;border-radius:6px;font-size:12px;display:none;max-width:90vw}
#drawbar{position:absolute;bottom:30px;left:50%;transform:translateX(-50%);z-index:950;display:none;gap:8px;
 background:#fff;padding:8px;border-radius:10px;box-shadow:0 2px 10px rgba(0,0,0,.3)}
#drawbar button{border:1px solid #c6d0da;background:#fff;border-radius:6px;padding:7px 12px}
#drawbar button.main{background:#14365a;color:#fff;border-color:#14365a}
.panel{position:absolute;z-index:1100;background:#fff;box-shadow:0 4px 18px rgba(0,0,0,.3);border-radius:10px;
 display:none;flex-direction:column;max-height:calc(100% - 80px)}
#form,#who,#welcome,#sendp{top:64px;left:50%;transform:translateX(-50%);width:min(440px,94vw);padding:14px 16px}
#list{top:52px;bottom:0;width:min(380px,100vw);border-radius:0;max-height:none}
[dir=ltr] #list{right:0} [dir=rtl] #list{left:0}
.panel h3{margin:0 0 8px;font-size:16px}
.panel textarea,.panel input[type=text]{width:100%;box-sizing:border-box;font:inherit;padding:8px;border:1px solid #c6d0da;
 border-radius:6px}
.panel textarea{min-height:110px;resize:vertical}
.row{display:flex;gap:8px;flex-wrap:wrap;margin-top:10px;align-items:center}
.row .sp{flex:1}
.btn{border:1px solid #c6d0da;background:#fff;border-radius:6px;padding:7px 12px}
.btn.main{background:#14365a;color:#fff;border-color:#14365a}
.btn.danger{color:#b42318;border-color:#f0b4ae}
.chips{display:flex;gap:6px;flex-wrap:wrap;margin:6px 0 10px}
.chip{border:2px solid transparent;border-radius:16px;padding:5px 12px;color:#fff;opacity:.55}
.chip.on{opacity:1;border-color:#1d2733}
.ref{font-size:12px;color:#5b6b7b;margin-bottom:6px}
.scroll{overflow:auto;flex:1;padding:0 14px 14px}
.lh{padding:12px 14px;border-bottom:1px solid #e3e8ee;display:flex;align-items:center;gap:8px}
.lh b{flex:1}
.item{border-bottom:1px solid #eef1f4;padding:10px 0;cursor:pointer}
.item .m{font-size:12px;color:#5b6b7b;display:flex;gap:6px;align-items:center;flex-wrap:wrap}
.item .x{margin-top:3px;white-space:pre-wrap;word-break:break-word}
.dot{display:inline-block;width:10px;height:10px;border-radius:50%}
.st{font-size:11px;border-radius:8px;padding:1px 7px;background:#e3e8ee;color:#1d2733}
.st.resolved{background:#d1f0dc;color:#14532d}.st.rejected{background:#f6d5d2;color:#7a1d14}
.st.in_progress{background:#fff1c2;color:#6b4e00}
.rep{margin-top:4px;padding:6px 8px;background:#f1f5f9;border-radius:6px;font-size:13px;white-space:pre-wrap;
 word-break:break-word}
.pin{width:24px;height:24px;border-radius:50% 50% 50% 0;transform:rotate(-45deg);border:2px solid #fff;
 box-shadow:0 1px 4px rgba(0,0,0,.4);display:flex;align-items:center;justify-content:center}
.pin span{transform:rotate(45deg);color:#fff;font-size:11px;font-weight:700}
.pin.closed{filter:grayscale(1);opacity:.7}
.pp table{border-collapse:collapse;font-size:12px;margin:4px 0}
.pp th{text-align:start;padding:2px 8px 2px 0;color:#5b6b7b;font-weight:600;vertical-align:top}
.pp td{padding:2px 0;word-break:break-word}
[dir=rtl] .pp,[dir=rtl] .leaflet-popup-content,[dir=rtl] .leaflet-control-layers{direction:rtl;text-align:right}
.lbl{background:transparent;border:0;box-shadow:none;font-weight:600;color:#1d2733;
 text-shadow:0 0 3px #fff,0 0 3px #fff,0 0 3px #fff}
.lbl:before{display:none}
#manual{width:100%;height:120px;margin-top:8px;display:none}
@media (max-width:600px){#bar .lbl2{display:none}#bar button{padding:7px 8px}}
</style>
</head>
<body>
<div id="bar"><div class="t" id="title"></div>
 <button id="bLine"></button><button id="bArea"></button>
 <button id="bList"><span class="lbl2" id="bListT"></span><span class="n" id="count">0</span></button>
 <button id="bSend" class="main"></button></div>
<div id="map"></div>
<div id="hint"></div>
<div id="note"></div>
<div id="drawbar"><button id="dFinish" class="main"></button><button id="dUndo"></button><button id="dCancel"></button></div>
<div class="panel" id="welcome"><h3 id="wTitle"></h3><div id="wBody" class="scroll" style="padding:0"></div>
 <div class="row"><span class="sp"></span><button class="btn main" id="wStart"></button></div></div>
<div class="panel" id="who"><h3 id="whoT"></h3><div class="ref" id="whoH"></div><input type="text" id="whoIn">
 <div class="row"><span class="sp"></span><button class="btn" id="whoCancel"></button><button class="btn main" id="whoOk"></button></div></div>
<div class="panel" id="form"><h3 id="fT"></h3><div class="ref" id="fRef"></div><div class="chips" id="chips"></div>
 <textarea id="fText"></textarea><div id="fReply"></div>
 <div class="row"><button class="btn danger" id="fDel"></button><span class="sp"></span>
 <button class="btn" id="fCancel"></button><button class="btn main" id="fSave"></button></div></div>
<div class="panel" id="list"><div class="lh"><b id="lT"></b><button class="btn" id="lName"></button>
 <button class="btn" id="lClose">&times;</button></div><div class="scroll" id="lBody"></div></div>
<div class="panel" id="sendp"><h3 id="sT"></h3><div class="ref" id="sH"></div>
 <div class="row"><button class="btn main" id="sShare"></button><button class="btn" id="sDown"></button>
 <button class="btn" id="sCopy"></button></div><textarea id="manual" readonly></textarea>
 <div class="row"><button class="btn" id="sReplies"></button><span class="sp"></span><button class="btn" id="sClose"></button></div>
 <input type="file" id="fileIn" accept=".json,.txt,application/json,text/plain" hidden></div>
<script>%%LEAFLET_JS%%</script>
<script type="application/json" id="biruni-data">%%DATA%%</script>
<script>
(function(){
'use strict';
var D = JSON.parse(document.getElementById('biruni-data').textContent), T = D.text;
function $(id){ return document.getElementById(id); }
function esc(s){ return String(s == null ? '' : s).replace(/[&<>"']/g, function(c){
  return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]; }); }
function fmt(s, o){ return String(s).replace(/\{(\w+)\}/g, function(m, k){ return o[k] != null ? o[k] : m; }); }
var KEY = 'biruni-review:' + D.review_id;
var store = (function(){ try { var t = '__biruni'; localStorage.setItem(t, t); localStorage.removeItem(t);
  return localStorage; } catch (e) { return null; } })();
function load(k, d){ if (!store) return d; try { var v = store.getItem(KEY + k); return v ? JSON.parse(v) : d; }
  catch (e) { return d; } }
function save(k, v){ if (!store) return; try { store.setItem(KEY + k, JSON.stringify(v)); } catch (e) {} }
var S = {name: load(':name', ''), comments: load(':comments', []), replies: load(':replies', {}),
         dirty: load(':dirty', false)};
if (typeof S.name !== 'string') S.name = '';
if (!Array.isArray(S.comments)) S.comments = [];
if (!S.replies || typeof S.replies !== 'object') S.replies = {};
S.comments = S.comments.filter(function(c){ return c && typeof c.id === 'string' && c.geometry; });
function persist(){ save(':comments', S.comments); save(':dirty', S.dirty); }

document.title = D.title || document.title;
$('title').textContent = D.title || '';
$('bLine').textContent = T.line; $('bArea').textContent = T.area; $('bListT').textContent = T.comments + ' ';
$('bSend').textContent = T.send; $('hint').textContent = T.hint;
$('dFinish').textContent = T.finish; $('dUndo').textContent = T.undo_point; $('dCancel').textContent = T.cancel;
$('whoT').textContent = T.your_name; $('whoH').textContent = T.name_hint; $('whoOk').textContent = T.continue;
$('whoCancel').textContent = T.cancel; $('whoIn').maxLength = D.limits.name;
$('fDel').textContent = T.delete; $('fCancel').textContent = T.cancel; $('fSave').textContent = T.save;
$('fText').maxLength = D.limits.text; $('fText').placeholder = T.comment;
$('lT').textContent = T.comments; $('lName').textContent = T.edit_name;
$('sT').textContent = T.send_title; $('sH').textContent = T.send_text; $('sShare').textContent = T.share;
$('sDown').textContent = T.export; $('sCopy').textContent = T.copy; $('sReplies').textContent = T.replies;
$('sClose').textContent = T.close; $('wStart').textContent = T.start;

var map = L.map('map', {preferCanvas: true, zoomControl: false});
L.control.zoom({position: D.rtl ? 'topright' : 'topleft'}).addTo(map);
map.attributionControl.setPrefix('<a href="https://leafletjs.com">Leaflet</a> | ' + esc(T.made));
var bases = {}, names = {osm: T.map, satellite: T.satellite, topo: T.topo}, tileErrors = 0;
Object.keys(D.basemaps).forEach(function(k){
  var b = D.basemaps[k];
  var t = L.tileLayer(b.url, {maxZoom: b.maxZoom, maxNativeZoom: b.maxNativeZoom || b.maxZoom, attribution: b.attribution});
  t.on('tileerror', function(){ if (++tileErrors === 4) { $('note').textContent = T.offline; $('note').style.display = 'block';
    setTimeout(function(){ $('note').style.display = 'none'; }, 6000); } });
  bases[esc(names[k] || k)] = t;
});
if (D.basemap !== 'none' && D.basemaps[D.basemap]) bases[esc(names[D.basemap])].addTo(map);

var side = D.rtl ? 'topleft' : 'topright', mode = null, draft = [], draftLayer = null, pending = null, editing = null;
function row(ly, f){
  var rows = '';
  ly.fields.forEach(function(name, k){ var v = f.properties.v[k];
    if (v === null || v === undefined || v === '') return;
    rows += '<tr><th>' + esc(name) + '</th><td>' + esc(v) + '</td></tr>'; });
  return rows;
}
var overlays = {}, groups = [];
for (var i = D.layers.length - 1; i >= 0; i--) {
  (function(ly){
    var styleOf = function(f){ return ly.styles[f.properties.s] || ly.styles[0]; };
    var g = L.geoJSON(ly.geojson, {
      style: styleOf,
      pointToLayer: function(f, ll){ return L.circleMarker(ll, styleOf(f)); },
      onEachFeature: function(f, l){
        if (f.properties.l) l.bindTooltip(esc(f.properties.l), {permanent: true, direction: 'center', className: 'lbl'});
        l.on('click', function(e){
          L.DomEvent.stopPropagation(e);
          if (mode) { addPoint(e.latlng); return; }
          var div = L.DomUtil.create('div', 'pp'), rows = row(ly, f);
          div.innerHTML = '<b>' + esc(ly.name) + '</b>' + (rows ? '<table>' + rows + '</table>' : '');
          var b = L.DomUtil.create('button', 'btn main', div); b.textContent = T.on_feature;
          b.onclick = function(){ map.closePopup(); begin({type: 'Point', coordinates: [e.latlng.lng, e.latlng.lat]},
            {layer: ly.name, layer_id: ly.id, fid: f.properties.k, label: f.properties.l || ''}); };
          L.popup({maxWidth: 320}).setLatLng(e.latlng).setContent(div).openOn(map);
        });
      }
    }).addTo(map);
    overlays['<span>' + esc(ly.name) + '</span>'] = g; groups.push(g);
  })(D.layers[i]);
}
L.control.layers(bases, overlays, {collapsed: true, position: side}).addTo(map);
L.control.scale({imperial: false, position: D.rtl ? 'bottomright' : 'bottomleft'}).addTo(map);
var pins = L.layerGroup().addTo(map);

map.on('click', function(e){
  if (mode) { addPoint(e.latlng); return; }
  var div = L.DomUtil.create('div', 'pp'), b = L.DomUtil.create('button', 'btn main', div);
  b.textContent = T.here;
  b.onclick = function(){ map.closePopup(); begin({type: 'Point', coordinates: [e.latlng.lng, e.latlng.lat]}, null); };
  L.popup().setLatLng(e.latlng).setContent(div).openOn(map);
});

// ---- sketching a line or an area ----
function startDraw(m){
  cancelDraw(); mode = m; draft = []; map.closePopup(); map.doubleClickZoom.disable();
  $('hint').textContent = m === 'line' ? T.draw_line : T.draw_area; $('hint').style.display = 'block';
  $('drawbar').style.display = 'flex';
}
function addPoint(ll){
  if (draft.length >= D.limits.vertices) return;
  draft.push([ll.lng, ll.lat]);
  if (draftLayer) map.removeLayer(draftLayer);
  var lls = draft.map(function(p){ return [p[1], p[0]]; });
  draftLayer = (mode === 'area' && draft.length > 2 ? L.polygon(lls, {color: '#14365a', dashArray: '6 4'})
               : L.polyline(lls, {color: '#14365a', dashArray: '6 4'})).addTo(map);
}
function cancelDraw(){
  if (draftLayer) map.removeLayer(draftLayer);
  draftLayer = null; draft = []; mode = null; map.doubleClickZoom.enable();
  $('drawbar').style.display = 'none'; $('hint').textContent = T.hint;
}
function finishDraw(){
  var need = mode === 'area' ? 3 : 2;
  if (draft.length < need) { alert(T.need_points); return; }
  var g = mode === 'area' ? {type: 'Polygon', coordinates: [draft.concat([draft[0]])]}
                          : {type: 'LineString', coordinates: draft.slice()};
  cancelDraw(); begin(g, null);
}
$('bLine').onclick = function(){ startDraw('line'); };
$('bArea').onclick = function(){ startDraw('area'); };
$('dFinish').onclick = finishDraw;
$('dUndo').onclick = function(){ if (draft.length) { draft.pop(); var p = draft.slice(); draft = []; p.forEach(function(q){ addPoint(L.latLng(q[1], q[0])); }); } };
$('dCancel').onclick = cancelDraw;
map.on('dblclick', function(){ if (mode) finishDraw(); });

// ---- panels ----
function show(id){ ['form', 'who', 'welcome', 'sendp'].forEach(function(p){ $(p).style.display = p === id ? 'flex' : 'none'; }); }
function hide(id){ $(id).style.display = 'none'; }
function askName(then){
  $('whoIn').value = S.name; show('who'); $('whoIn').focus();
  $('whoOk').onclick = function(){ var n = $('whoIn').value.trim().slice(0, D.limits.name);
    if (!n) { $('whoIn').focus(); return; } S.name = n; save(':name', n); hide('who'); renderList(); if (then) then(); };
  $('whoCancel').onclick = function(){ hide('who'); };
}
var cat = 'note';
function chips(){
  var h = $('chips'); h.innerHTML = '';
  D.categories.forEach(function(k){ var b = document.createElement('button'); b.className = 'chip' + (k === cat ? ' on' : '');
    b.style.background = D.colors[k]; b.textContent = T[k]; b.onclick = function(){ cat = k; chips(); }; h.appendChild(b); });
}
function refText(c){
  if (c.feature && c.feature.layer) return T.feature + ': ' + c.feature.layer + (c.feature.label ? ' - ' + c.feature.label : '');
  return '';
}
function begin(geometry, feature){
  if (!S.name) { askName(function(){ begin(geometry, feature); }); return; }
  pending = {geometry: geometry, feature: feature}; editing = null; cat = 'note';
  $('fT').textContent = T.comment; $('fRef').textContent = refText({feature: feature}); $('fText').value = '';
  $('fReply').innerHTML = ''; $('fDel').style.display = 'none'; chips(); show('form'); $('fText').focus();
}
function openComment(c){
  editing = c; pending = null; cat = c.category;
  $('fT').textContent = c.author + ' - ' + new Date(c.created).toLocaleString();
  $('fRef').textContent = refText(c); $('fText').value = c.text; $('fDel').style.display = '';
  var r = S.replies[c.id];
  $('fReply').innerHTML = r ? '<div class="row"><span class="st ' + esc(r.status) + '">' + esc(T[r.status] || '') + '</span></div>' +
    (r.reply ? '<div class="rep">' + esc(r.reply) + '</div>' : '') : '';
  chips(); show('form');
}
function newId(){
  var a = new Uint8Array(9);
  try { crypto.getRandomValues(a); } catch (e) { for (var i = 0; i < a.length; i++) a[i] = Math.floor(Math.random() * 256); }
  return Array.prototype.map.call(a, function(b){ return ('0' + b.toString(16)).slice(-2); }).join('') + Date.now().toString(36);
}
$('fSave').onclick = function(){
  var text = $('fText').value.trim().slice(0, D.limits.text);
  if (!text) { alert(T.empty); return; }
  var now = new Date().toISOString();
  if (editing) { editing.text = text; editing.category = cat; editing.updated = now; }
  else S.comments.push({id: newId(), author: S.name, created: now, updated: now, category: cat, text: text,
                        geometry: pending.geometry, feature: pending.feature, zoom: map.getZoom()});
  S.dirty = true; persist(); hide('form'); render();
};
$('fCancel').onclick = function(){ hide('form'); };
$('fDel').onclick = function(){
  if (!editing || !confirm(T.confirm_delete)) return;
  S.comments = S.comments.filter(function(c){ return c !== editing; });
  S.dirty = true; persist(); hide('form'); render();
};

function anchor(c){
  var g = c.geometry, p = g.type === 'Point' ? g.coordinates : g.type === 'LineString' ? g.coordinates[0] : g.coordinates[0][0];
  return L.latLng(p[1], p[0]);
}
function render(){
  pins.clearLayers();
  S.comments.forEach(function(c, i){
    var r = S.replies[c.id], closed = r && (r.status === 'resolved' || r.status === 'rejected'), color = D.colors[c.category] || '#2f7ed8';
    if (c.geometry.type !== 'Point') {
      var lls = (c.geometry.type === 'Polygon' ? c.geometry.coordinates[0] : c.geometry.coordinates).map(function(p){ return [p[1], p[0]]; });
      (c.geometry.type === 'Polygon' ? L.polygon(lls, {color: color, weight: 3, fillOpacity: .15})
                                     : L.polyline(lls, {color: color, weight: 4})).on('click', function(e){ L.DomEvent.stopPropagation(e); openComment(c); }).addTo(pins);
    }
    var icon = L.divIcon({className: '', iconSize: [24, 24], iconAnchor: [12, 24],
      html: '<div class="pin' + (closed ? ' closed' : '') + '" style="background:' + esc(color) + '"><span>' + (i + 1) + '</span></div>'});
    L.marker(anchor(c), {icon: icon}).on('click', function(e){ L.DomEvent.stopPropagation(e); openComment(c); }).addTo(pins);
  });
  $('count').textContent = S.comments.length;
  renderList();
}
function renderList(){
  var h = '';
  if (!S.comments.length) h = '<p>' + esc(T.none_yet) + '</p>';
  S.comments.forEach(function(c, i){
    var r = S.replies[c.id];
    h += '<div class="item" data-i="' + i + '"><div class="m"><span class="dot" style="background:' + esc(D.colors[c.category]) + '"></span>' +
      '<b>' + (i + 1) + '</b><span>' + esc(T[c.category]) + '</span><span>' + esc(new Date(c.created).toLocaleString()) + '</span>' +
      (r ? '<span class="st ' + esc(r.status) + '">' + esc(T[r.status] || '') + '</span>' : '') + '</div>' +
      '<div class="x">' + esc(c.text) + '</div>' + (r && r.reply ? '<div class="rep">' + esc(r.reply) + '</div>' : '') + '</div>';
  });
  $('lBody').innerHTML = h;
  Array.prototype.forEach.call($('lBody').querySelectorAll('.item'), function(el){
    el.onclick = function(){ var c = S.comments[+el.getAttribute('data-i')]; if (!c) return;
      map.setView(anchor(c), Math.max(map.getZoom(), c.zoom || 17)); if (window.innerWidth < 700) hide('list'); openComment(c); };
  });
  $('lName').textContent = S.name ? S.name + ' ✎' : T.edit_name;
}
$('bList').onclick = function(){ $('list').style.display = $('list').style.display === 'flex' ? 'none' : 'flex'; renderList(); };
$('lClose').onclick = function(){ hide('list'); };
$('lName').onclick = function(){ askName(null); };

// ---- sending the comments ----
function safe(s){ return String(s || '').replace(/[\\\/:*?"<>|\s]+/g, '_').slice(0, 60) || 'review'; }
function payload(){
  return JSON.stringify({format: D.format, version: D.version, review_id: D.review_id, title: D.title, author: S.name,
                         exported: new Date().toISOString(), comments: S.comments}, null, 1);
}
function fname(ext){ return safe(D.title) + '-review-' + safe(S.name) + '.biruni-review.' + ext; }
function sent(){ S.dirty = false; persist(); }
function download(){
  var blob = new Blob([payload()], {type: 'application/json'}), a = document.createElement('a');
  a.href = URL.createObjectURL(blob); a.download = fname('json'); document.body.appendChild(a); a.click();
  setTimeout(function(){ URL.revokeObjectURL(a.href); a.remove(); }, 2000); sent();
}
$('bSend').onclick = function(){ $('manual').style.display = 'none'; show('sendp'); };
$('sDown').onclick = download;
$('sShare').onclick = function(){
  var text = payload(), files = [new File([text], fname('json'), {type: 'application/json'}),
                                 new File([text], fname('txt'), {type: 'text/plain'})];
  for (var i = 0; i < files.length; i++) {
    if (navigator.canShare && navigator.canShare({files: [files[i]]})) {
      navigator.share({files: [files[i]], title: D.title}).then(sent, function(){});
      return;
    }
  }
  alert(T.share_unavailable); download();
};
$('sCopy').onclick = function(){
  var text = payload();
  function manual(){ var m = $('manual'); m.value = text; m.style.display = 'block'; m.focus(); m.select(); alert(T.copy_manual); }
  if (navigator.clipboard && navigator.clipboard.writeText) navigator.clipboard.writeText(text).then(function(){ sent(); alert(T.copied); }, manual);
  else manual();
};
$('sClose').onclick = function(){ hide('sendp'); };
$('sReplies').onclick = function(){ $('fileIn').value = ''; $('fileIn').click(); };
$('fileIn').onchange = function(){
  var f = this.files && this.files[0]; if (!f) return;
  if (f.size > 5 * 1024 * 1024) { alert(T.bad_file); return; }
  var rd = new FileReader();
  rd.onload = function(){
    var d; try { d = JSON.parse(rd.result); } catch (e) { alert(T.bad_file); return; }
    if (!d || d.format !== 'biruni-review-replies' || !Array.isArray(d.replies)) { alert(T.bad_file); return; }
    if (d.review_id && d.review_id !== D.review_id && !confirm(T.replies_other)) return;
    var n = 0, ok = {'new': 1, in_progress: 1, resolved: 1, rejected: 1};
    d.replies.slice(0, 5000).forEach(function(r){
      if (!r || typeof r.id !== 'string' || !/^[A-Za-z0-9_-]{1,64}$/.test(r.id) || !ok[r.status]) return;
      S.replies[r.id] = {status: r.status, reply: typeof r.reply === 'string' ? r.reply.slice(0, D.limits.text) : ''}; n++;
    });
    save(':replies', S.replies); render(); alert(fmt(T.replies_loaded, {n: n}));
  };
  rd.readAsText(f);
};
window.addEventListener('beforeunload', function(e){ if (S.dirty && S.comments.length) { e.preventDefault(); e.returnValue = T.unsent; return T.unsent; } });

// ---- start ----
if (!store) { $('note').textContent = T.no_storage; $('note').style.display = 'block'; }
if (D.bounds) { var b = L.latLngBounds(D.bounds);
  if (b.getNorthEast().equals(b.getSouthWest())) map.setView(b.getCenter(), 17); else map.fitBounds(b, {padding: [20, 20]});
} else map.setView([20, 0], 2);
render();
if (!load(':seen', false)) {
  $('wTitle').textContent = D.title || T.comments;
  var w = '';
  if (D.instructions) w += '<p style="white-space:pre-wrap">' + esc(D.instructions) + '</p>';
  if (D.deadline) w += '<p><b>' + esc(fmt(T.deadline, {date: D.deadline})) + '</b></p>';
  w += '<p>' + esc(T.how) + '</p>';
  $('wBody').innerHTML = w; show('welcome');
  $('wStart').onclick = function(){ save(':seen', true); hide('welcome'); if (!S.name) askName(null); };
}
})();
</script>
</body>
</html>
"""
