"""Map stops: the data. A stop = a QGIS bookmark (name, group, extent, rotation) + extras kept beside it, keyed by
the bookmark id: the exact view frame (centre, size, rotation, scale), the layers shown (a map theme), the time
range, a note, a colour tag and a small JPEG picture.

Project stops live in the project's bookmark manager; their extras in one project entry ("Biruni"/"stops", inside
the .qgz - never a separate file). Stops "for all my projects" live in QGIS's user bookmark manager; their extras in
a JSON file of the user profile.

The bookmark is the source of truth for name, group and extent: a stop renamed, regrouped or deleted in QGIS's own
bookmark manager is renamed, regrouped or deleted here too. Extras whose bookmark is gone are dropped the next time
the extras are written (never while a project is being read or closed).
"""
import base64
import datetime as _dt
import json
import math
import os
import uuid

from qgis.core import (QgsApplication, QgsBookmark, QgsCoordinateReferenceSystem, QgsCoordinateTransform,
                       QgsDateTimeRange, QgsMapSettings, QgsPointXY, QgsProject,
                       QgsRectangle, QgsReferencedRectangle, QgsSettings)
from qgis.PyQt.QtCore import QBuffer, QByteArray, QDateTime, QIODevice, QObject, QSize, Qt, pyqtSignal
from qgis.PyQt.QtGui import QImage

from ..i18n import tr

PROJECT, USER = "project", "user"
SCOPES = (PROJECT, USER)
ENTRY_SCOPE, ENTRY_KEY = "Biruni", "stops"
USER_FILE = "biruni-stops.json"
KEY = "biruni/stops/"
THUMB_W, THUMB_H = 256, 160
FORMAT = 1

DEFAULTS = {"fly": True, "fly_ms": 700, "tour_pause": 4, "jump_keys": "alt", "keep_scale": False,
            "remember_layers": True, "scope": PROJECT, "place_names": True}


def setting(name):
    d = DEFAULTS[name]
    return QgsSettings().value(KEY + name, d, type=type(d))


def set_setting(name, value):
    QgsSettings().setValue(KEY + name, value)


def _now():
    return _dt.datetime.now().replace(microsecond=0).isoformat()


# ---- the view frame ----------------------------------------------------------------------------------------------
class Frame:
    """What a map view shows: centre, the size of the (unrotated) frame in map units, rotation (degrees clockwise,
    as QGIS) and the CRS. Rebuilds the same view on a map of another size (fit) or keeps the scale."""

    __slots__ = ("cx", "cy", "w", "h", "rot", "crs")

    def __init__(self, cx, cy, w, h, rot=0.0, crs=None):
        self.cx, self.cy, self.w, self.h = float(cx), float(cy), float(w), float(h)
        self.rot = float(rot or 0.0)
        self.crs = crs if isinstance(crs, QgsCoordinateReferenceSystem) else QgsCoordinateReferenceSystem(crs or "")

    @classmethod
    def from_canvas(cls, canvas):
        ms = canvas.mapSettings()
        c = canvas.center()
        mupp = canvas.mapUnitsPerPixel()
        size = ms.outputSize()
        w, h = max(1, size.width()), max(1, size.height())
        if canvas.rotation() == 0:
            e = canvas.extent()
            return cls(c.x(), c.y(), e.width(), e.height(), 0.0, ms.destinationCrs())
        return cls(c.x(), c.y(), mupp * w, mupp * h, canvas.rotation(), ms.destinationCrs())

    @classmethod
    def from_rect(cls, rect, rot=0.0, crs=None):
        c = rect.center()
        return cls(c.x(), c.y(), rect.width(), rect.height(), rot, crs)

    def rect(self):
        return QgsRectangle(self.cx - self.w / 2, self.cy - self.h / 2, self.cx + self.w / 2, self.cy + self.h / 2)

    def corners(self):
        """The 4 corners of what is shown (a rotated map shows a turned rectangle), in order around it."""
        a = math.radians(self.rot)
        ca, sa = math.cos(a), math.sin(a)
        out = []
        for dx, dy in ((-1, 1), (1, 1), (1, -1), (-1, -1)):
            x, y = dx * self.w / 2, dy * self.h / 2
            out.append(QgsPointXY(self.cx + x * ca - y * sa, self.cy + x * sa + y * ca))
        return out

    def bbox(self):
        pts = self.corners()
        xs, ys = [p.x() for p in pts], [p.y() for p in pts]
        return QgsRectangle(min(xs), min(ys), max(xs), max(ys))

    def is_valid(self):
        vals = (self.cx, self.cy, self.w, self.h, self.rot)
        return all(math.isfinite(v) for v in vals) and self.w >= 0 and self.h >= 0

    def to_dict(self):
        return {"cx": self.cx, "cy": self.cy, "w": self.w, "h": self.h, "rot": self.rot,
                "crs": crs_text(self.crs)}

    @classmethod
    def from_dict(cls, d):
        return cls(d["cx"], d["cy"], d["w"], d["h"], d.get("rot", 0.0), crs_from_text(d.get("crs", "")))

    def transformed(self, dest, project=None):
        """The frame in another CRS (ValueError when it cannot be shown there)."""
        if not dest.isValid() or not self.crs.isValid() or dest == self.crs:
            return Frame(self.cx, self.cy, self.w, self.h, self.rot, dest if dest.isValid() else self.crs)
        xf = QgsCoordinateTransform(self.crs, dest, project or QgsProject.instance())
        pts = c = None
        try:
            c = xf.transform(QgsPointXY(self.cx, self.cy))
            pts = [xf.transform(p) for p in self.corners()]
        except Exception:  # noqa: BLE001 - QgsCsException: a corner off this projection's world
            pts = None
        ok = pts is not None and all(math.isfinite(v) for p in pts + [c] for v in (p.x(), p.y()))
        if not ok:  # a whole-world view in a local CRS: the bounding box of what can be shown
            try:
                xf.setBallparkTransformsAreAppropriate(True)
                bb = xf.transformBoundingBox(self.bbox(), handle180Crossover=True)
            except Exception as e:  # noqa: BLE001
                raise ValueError(str(e)) from e
            vals = (bb.xMinimum(), bb.yMinimum(), bb.xMaximum(), bb.yMaximum())
            if not all(math.isfinite(v) for v in vals) or (bb.width() <= 0 and bb.height() <= 0):
                raise ValueError("this view cannot be shown in " + dest.authid())
            return Frame.from_rect(bb, 0.0, dest)
        w = math.hypot(pts[1].x() - pts[0].x(), pts[1].y() - pts[0].y())
        h = math.hypot(pts[2].x() - pts[1].x(), pts[2].y() - pts[1].y())
        # the direction of north changes between projections a little; keep the turn the user chose
        return Frame(c.x(), c.y(), w, h, self.rot, dest)


def crs_text(crs):
    if crs is None or not crs.isValid():
        return ""
    a = crs.authid()
    if a and QgsCoordinateReferenceSystem(a) == crs:
        return a
    return crs.toWkt(QgsCoordinateReferenceSystem.WktVariant.WKT_PREFERRED) if hasattr(
        QgsCoordinateReferenceSystem, "WktVariant") else crs.toWkt()


def crs_from_text(text):
    if not text:
        return QgsCoordinateReferenceSystem()
    crs = QgsCoordinateReferenceSystem(text)
    if not crs.isValid():
        crs = QgsCoordinateReferenceSystem.fromWkt(text)
    return crs


# ---- putting a frame on the map ---------------------------------------------------------------------------------
def fit_frame(frame, canvas, keep_scale=False, scale=0.0):
    """(centre QgsPointXY, map units per pixel) that shows the whole frame on this canvas (or keeps the scale)."""
    size = canvas.mapSettings().outputSize()
    w, h = max(1, size.width()), max(1, size.height())
    fw, fh = frame.w, frame.h
    if fw <= 0 and fh <= 0:  # a point: a sensible street-level view
        fw = fh = _units_for_metres(frame, 400.0)
    elif fw <= 0:
        fw = fh * w / h
    elif fh <= 0:
        fh = fw * h / w
    mupp = max(fw / w, fh / h)
    if keep_scale and scale > 0 and canvas.scale() > 0 and canvas.mapUnitsPerPixel() > 0:
        mupp = canvas.mapUnitsPerPixel() * scale / canvas.scale()
    return QgsPointXY(frame.cx, frame.cy), mupp


def _units_for_metres(frame, metres):
    if frame.crs.isValid() and frame.crs.isGeographic():
        return metres / 111320.0
    return metres


def apply_frame(canvas, centre, mupp, rot):
    """Show this centre at this many map units per pixel, turned by rot (one refresh)."""
    size = canvas.mapSettings().outputSize()
    w, h = max(1, size.width()), max(1, size.height())
    if abs((canvas.rotation() - rot + 180) % 360 - 180) > 1e-9:
        canvas.setRotation(rot)
    if rot == 0:
        canvas.setExtent(QgsRectangle(centre.x() - mupp * w / 2, centre.y() - mupp * h / 2,
                                      centre.x() + mupp * w / 2, centre.y() + mupp * h / 2))
    else:
        canvas.setCenter(centre)
        cur = canvas.mapUnitsPerPixel()
        if cur > 0 and canvas.scale() > 0 and abs(cur - mupp) / mupp > 1e-9:
            canvas.zoomScale(canvas.scale() * mupp / cur, True)
    canvas.refresh()


# ---- pictures ----------------------------------------------------------------------------------------------------
def image_to_bytes(img, fmt="JPG", quality=82):
    ba = QByteArray()
    buf = QBuffer(ba)
    buf.open(QIODevice.OpenModeFlag.WriteOnly)
    if img.hasAlphaChannel():
        flat = QImage(img.size(), QImage.Format.Format_RGB32)
        flat.fill(Qt.GlobalColor.white)
        from qgis.PyQt.QtGui import QPainter

        p = QPainter(flat)
        p.drawImage(0, 0, img)
        p.end()
        img = flat
    if not img.save(buf, fmt, quality):
        img.save(buf, "PNG")
    buf.close()
    return bytes(ba)


def is_blank(img):
    """One colour all over (an empty map, or nothing drawn yet)."""
    small = img.scaled(24, 24)
    first = small.pixel(0, 0)
    return all(small.pixel(x, y) == first for x in range(24) for y in range(24))


def thumb_from_image(img):
    """A 256×160 picture cut from the middle of a bigger one (fill, no bands); b"" for a blank map (the list then
    shows a placeholder card instead of a white rectangle)."""
    if img is None or img.isNull() or is_blank(img):
        return b""
    target = THUMB_W / THUMB_H
    w, h = img.width(), img.height()
    if w / max(1, h) > target:
        cw, ch = int(round(h * target)), h
    else:
        cw, ch = w, int(round(w / target))
    crop = img.copy((w - cw) // 2, (h - ch) // 2, cw, ch)
    small = crop.scaled(THUMB_W, THUMB_H, Qt.AspectRatioMode.IgnoreAspectRatio,
                        Qt.TransformationMode.SmoothTransformation)
    return image_to_bytes(small)


def thumb_settings(base_settings, frame):
    """Map settings that draw this frame filling a 256×160 picture."""
    ms = QgsMapSettings(base_settings)
    target = THUMB_W / THUMB_H
    fw, fh = frame.w or 1.0, frame.h or 1.0
    if fw / fh > target:
        fw = fh * target
    else:
        fh = fw / target
    ms.setOutputSize(QSize(THUMB_W * 2, THUMB_H * 2))
    ms.setOutputDpi(96)
    ms.setDestinationCrs(frame.crs)
    ms.setRotation(frame.rot)
    ms.setExtent(QgsRectangle(frame.cx - fw / 2, frame.cy - fh / 2, frame.cx + fw / 2, frame.cy + fh / 2))
    return ms


def render_thumb(settings, timeout_ms=6000):
    """Draw the map settings now (waits; for tests and for "update picture"); JPEG bytes."""
    from qgis.core import QgsMapRendererParallelJob
    from qgis.PyQt.QtCore import QEventLoop, QTimer

    job = QgsMapRendererParallelJob(settings)
    loop = QEventLoop()
    job.finished.connect(loop.quit)
    timer = QTimer()
    timer.setSingleShot(True)
    timer.timeout.connect(loop.quit)
    job.start()
    timer.start(timeout_ms)
    if job.isActive():
        loop.exec()
    if job.isActive():
        job.cancelWithoutBlocking()
    img = job.renderedImage()
    return thumb_from_image(img)


class ThumbJob(QObject):
    """Draws a stop's picture in the background (the stop is saved at once; its picture follows)."""

    done = pyqtSignal(str, bytes)

    def __init__(self, stop_id, settings, timeout_ms=8000, parent=None):
        super().__init__(parent)
        from qgis.core import QgsMapRendererParallelJob
        from qgis.PyQt.QtCore import QTimer

        self.stop_id = stop_id
        self.job = QgsMapRendererParallelJob(settings)
        self.job.finished.connect(self._finished)
        self.timer = QTimer(self)
        self.timer.setSingleShot(True)
        self.timer.timeout.connect(self._timeout)
        self._sent = False

    def start(self):
        self.job.start()
        self.timer.start(8000)

    def _timeout(self):
        if self.job.isActive():
            self.job.cancelWithoutBlocking()
        self._finished()

    def _finished(self):
        if self._sent:
            return
        self._sent = True
        self.timer.stop()
        self.done.emit(self.stop_id, thumb_from_image(self.job.renderedImage()))


def thumb_b64(data):
    return base64.b64encode(data).decode("ascii") if data else ""


def thumb_bytes(text):
    if not text:
        return b""
    try:
        return base64.b64decode(text)
    except (ValueError, TypeError):
        return b""


# ---- the stop ----------------------------------------------------------------------------------------------------
class Stop:
    """One stop: the bookmark's name/group/extent + the extras. Plain data; the store does the changes."""

    def __init__(self, sid, scope, name, group, bookmark_rect, rotation, extras):
        self.id = sid
        self.scope = scope
        self.name = name
        self.group = group or ""
        self.bookmark_rect = bookmark_rect          # QgsReferencedRectangle (the QGIS bookmark's extent)
        self.rotation = rotation or 0.0
        x = extras or {}
        self.extras = x
        self.note = x.get("note", "")
        self.color = x.get("color", "")
        self.scale = float(x.get("scale", 0.0) or 0.0)
        self.theme = x.get("theme", "")
        self.layers = list(x.get("layers") or [])
        self.time = tuple(x["time"]) if x.get("time") else None
        self.thumb = thumb_bytes(x.get("thumb", ""))
        self.created = x.get("created", "")
        self.updated = x.get("updated", "")
        self.frame = self._frame()

    def _frame(self):
        crs = self.bookmark_rect.crs()
        v = self.extras.get("view")
        if v:
            try:
                f = Frame.from_dict(v)
                bb = self.extras.get("bbox")
                # the bookmark was moved in QGIS's own manager: its extent wins
                if bb and not _same_rect(QgsRectangle(*bb), self.bookmark_rect, 1e-6):
                    raise ValueError("changed")
                if f.is_valid():
                    return f
            except (KeyError, ValueError, TypeError):
                pass
        return Frame.from_rect(self.bookmark_rect, self.rotation, crs)

    @property
    def crs(self):
        return self.frame.crs

    def remembers_layers(self):
        return bool(self.theme or self.layers)

    def search_text(self):
        return " ".join((self.name, self.group, self.note))


def _same_rect(a, b, tol):
    span = max(a.width(), a.height(), b.width(), b.height(), 1e-12)
    return all(abs(p - q) <= tol * span for p, q in ((a.xMinimum(), b.xMinimum()), (a.yMinimum(), b.yMinimum()),
                                                      (a.xMaximum(), b.xMaximum()), (a.yMaximum(), b.yMaximum())))


# ---- what was shown ----------------------------------------------------------------------------------------------
def layer_tree_model(iface=None, project=None):
    """The layer tree model of the Layers panel (styles and legend checks follow it), else a private one."""
    project = project or QgsProject.instance()
    if iface is not None:
        try:
            view = iface.layerTreeView()
            if view is not None and view.layerTreeModel() is not None:
                return view.layerTreeModel()
        except (AttributeError, RuntimeError):
            pass
    from qgis.core import QgsLayerTreeModel

    model = getattr(project, "_biruni_stops_tree_model", None)
    if model is None or model.rootGroup() is not project.layerTreeRoot():
        model = QgsLayerTreeModel(project.layerTreeRoot())
        try:
            project._biruni_stops_tree_model = model  # noqa: SLF001 - keep it alive with the project
        except AttributeError:
            pass
    return model


def visible_layers(project=None):
    project = project or QgsProject.instance()
    out = []
    for node in project.layerTreeRoot().findLayers():
        if node.isVisible() and node.layer() is not None:
            lyr = node.layer()
            out.append({"id": lyr.id(), "name": lyr.name(), "source": lyr.source()})
    return out


def apply_layers(snapshot, project=None):
    """Show the layers of a snapshot (by id, else by source, else by name) and hide the others. Does nothing when
    none of them is in this project (they were deleted, or it is another project). Returns how many were found."""
    project = project or QgsProject.instance()
    nodes = project.layerTreeRoot().findLayers()
    ids = {d.get("id") for d in snapshot}
    sources = {os.path.normcase(d.get("source") or "") for d in snapshot} - {""}
    names = {d.get("name") for d in snapshot} - {None, ""}
    match = []
    for node in nodes:
        lyr = node.layer()
        if lyr is None:
            continue
        hit = lyr.id() in ids or os.path.normcase(lyr.source()) in sources or lyr.name() in names
        match.append((node, hit))
    found = sum(1 for _n, hit in match if hit)
    if not found:
        return 0
    for node, hit in match:
        node.setItemVisibilityChecked(hit)
        if hit:  # its groups too, or it stays hidden
            parent = node.parent()
            while parent is not None and parent is not project.layerTreeRoot():
                parent.setItemVisibilityChecked(True)
                parent = parent.parent()
    return found


# ---- the store ---------------------------------------------------------------------------------------------------
class StopsStore(QObject):
    """All stops of the open project and of the user. One per project; created when first used."""

    changed = pyqtSignal()

    def __init__(self, project=None, iface=None, user_file=None):
        super().__init__()
        self.project = project or QgsProject.instance()
        self.iface = iface
        self.user_file = user_file or os.path.join(QgsApplication.qgisSettingsDirPath(), USER_FILE)
        self._cache = {PROJECT: None, USER: None}
        self._busy = 0
        self._jobs = []
        for scope in SCOPES:
            m = self.manager(scope)
            m.bookmarkAdded.connect(self._external)
            m.bookmarkChanged.connect(lambda sid, s=scope: self._bookmark_changed(s, sid))
            m.bookmarkRemoved.connect(self._external)
        self.project.readProject.connect(self._reloaded)
        self.project.cleared.connect(self._reloaded)

    # ---- plumbing -----------------------------------------------------------------------------------------------
    def manager(self, scope):
        return self.project.bookmarkManager() if scope == PROJECT else QgsApplication.bookmarkManager()

    def _reloaded(self, *_):
        self._cache[PROJECT] = None
        self.changed.emit()

    def _external(self, *_):
        if not self._busy:
            self.changed.emit()

    def _bookmark_changed(self, scope, sid):
        """Renamed in QGIS's manager: its map theme follows the name."""
        if self._busy:
            return
        x = self.extras(scope)["stops"].get(sid)
        b = self.manager(scope).bookmarkById(sid)
        if x and x.get("theme") and scope == PROJECT:
            want = self._theme_name(b.name(), sid)
            if want != x["theme"]:
                themes = self.project.mapThemeCollection()
                if themes.hasMapTheme(x["theme"]) and not themes.hasMapTheme(want):
                    themes.renameMapTheme(x["theme"], want)
                    x["theme"] = want
                    self._write(scope)
        self.changed.emit()

    def extras(self, scope):
        data = self._cache[scope]
        if data is None:
            text = ""
            if scope == PROJECT:
                text, _ok = self.project.readEntry(ENTRY_SCOPE, ENTRY_KEY, "")
            elif os.path.exists(self.user_file):
                try:
                    with open(self.user_file, encoding="utf-8") as f:
                        text = f.read()
                except OSError:
                    text = ""
            try:
                data = json.loads(text) if text else {}
            except ValueError:
                data = {}
            if not isinstance(data, dict):
                data = {}
            data.setdefault("format", FORMAT)
            data.setdefault("order", [])
            data.setdefault("stops", {})
            self._cache[scope] = data
        return data

    def _write(self, scope):
        """Store the extras of a scope; extras whose bookmark is gone are dropped (with their map theme)."""
        data = self.extras(scope)
        m = self.manager(scope)
        live = {b.id() for b in m.bookmarks()}
        themes = self.project.mapThemeCollection()
        for sid in [s for s in data["stops"] if s not in live]:
            x = data["stops"].pop(sid)
            if scope == PROJECT and x.get("theme") and themes.hasMapTheme(x["theme"]):
                themes.removeMapTheme(x["theme"])
        data["order"] = [s for s in dict.fromkeys(data["order"]) if s in live]
        text = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
        if scope == PROJECT:
            self.project.writeEntry(ENTRY_SCOPE, ENTRY_KEY, text)
        else:
            try:
                os.makedirs(os.path.dirname(self.user_file), exist_ok=True)
                tmp = self.user_file + ".tmp"
                with open(tmp, "w", encoding="utf-8") as f:
                    f.write(text)
                os.replace(tmp, self.user_file)
            except OSError:
                pass

    # ---- reading ------------------------------------------------------------------------------------------------
    def _stop(self, scope, b):
        x = self.extras(scope)["stops"].get(b.id(), {})
        return Stop(b.id(), scope, b.name(), b.group(), b.extent(), b.rotation(), x)

    def stops(self, scope=None, group=None, query=None):
        """Stops in the order shown: those without a group first, then each group (in the order its first stop
        comes); inside each, the project's stops in their order, then the user's. Alt+1…9 and the tour follow it."""
        out = []
        for sc in ([scope] if scope in SCOPES else SCOPES):
            bms = {b.id(): b for b in self.manager(sc).bookmarks()}
            order = [s for s in self.extras(sc)["order"] if s in bms]
            order += [s for s in bms if s not in set(order)]
            out += [self._stop(sc, bms[s]) for s in order]
        out = display_order(out)
        if group is not None:
            out = [s for s in out if s.group == group]
        if query:
            out = search(out, query)
        return out

    def get(self, sid):
        for scope in SCOPES:
            b = self.manager(scope).bookmarkById(sid)
            if b.id():
                return self._stop(scope, b)
        return None

    def scope_of(self, sid):
        for scope in SCOPES:
            if self.manager(scope).bookmarkById(sid).id():
                return scope
        return None

    def groups(self, scope=None):
        """Group names in the order their first stop appears."""
        return list(dict.fromkeys(s.group for s in self.stops(scope) if s.group))

    # ---- changes ------------------------------------------------------------------------------------------------
    def add(self, name, frame, scope=None, group="", note="", color="", scale=0.0, thumb=b"", layers=None,
            theme_record=None, time=None, sid=None, created=None, position=None):
        """A new stop from a view frame. Returns the Stop."""
        scope = scope if scope in SCOPES else setting("scope")
        m = self.manager(scope)
        b = QgsBookmark()
        if sid and not m.bookmarkById(sid).id() and not self.get(sid):
            b.setId(sid)
        b.setName(name)
        b.setGroup(group or "")
        b.setExtent(QgsReferencedRectangle(frame.bbox(), frame.crs))
        b.setRotation(frame.rot)
        self._busy += 1
        try:
            new_id, ok = m.addBookmark(b)
            if not ok:
                return None
            x = {"view": frame.to_dict(), "bbox": _rect_list(frame.bbox()), "scale": float(scale or 0.0),
                 "note": note or "", "color": color or "", "thumb": thumb_b64(thumb),
                 "created": created or _now(), "updated": _now()}
            if layers:
                x["layers"] = layers
            if time:
                x["time"] = list(time)
            if theme_record is not None and scope == PROJECT:
                x["theme"] = self._put_theme(name, new_id, theme_record)
            data = self.extras(scope)
            data["stops"][new_id] = x
            order = [s for s in data["order"] if s != new_id] or [bm.id() for bm in m.bookmarks() if bm.id() != new_id]
            if position is None:
                order.append(new_id)
            else:
                order.insert(max(0, min(position, len(order))), new_id)
            data["order"] = order
            self._write(scope)
        finally:
            self._busy -= 1
        self.changed.emit()
        return self.get(new_id)

    def update(self, sid, **fields):
        """Change fields of a stop: name, group, note, color, thumb (bytes), frame, scale, time, layers, theme_record
        (None removes the remembered layers)."""
        scope = self.scope_of(sid)
        if scope is None:
            return None
        m = self.manager(scope)
        b = m.bookmarkById(sid)
        x = self.extras(scope)["stops"].setdefault(sid, {"created": _now()})
        self._busy += 1
        try:
            touch_bm = False
            if "name" in fields and fields["name"] and fields["name"] != b.name():
                b.setName(fields["name"])
                touch_bm = True
                if x.get("theme"):
                    x["theme"] = self._rename_theme(x["theme"], fields["name"], sid)
            if "group" in fields and (fields["group"] or "") != b.group():
                b.setGroup(fields["group"] or "")
                touch_bm = True
            if "frame" in fields and fields["frame"] is not None:
                f = fields["frame"]
                b.setExtent(QgsReferencedRectangle(f.bbox(), f.crs))
                b.setRotation(f.rot)
                x["view"] = f.to_dict()
                x["bbox"] = _rect_list(f.bbox())
                touch_bm = True
            if touch_bm:
                m.updateBookmark(b)
            for k in ("note", "color"):
                if k in fields:
                    x[k] = fields[k] or ""
            if "scale" in fields:
                x["scale"] = float(fields["scale"] or 0.0)
            if "thumb" in fields:
                x["thumb"] = thumb_b64(fields["thumb"])
            if "time" in fields:
                if fields["time"]:
                    x["time"] = list(fields["time"])
                else:
                    x.pop("time", None)
            if "layers" in fields:
                if fields["layers"]:
                    x["layers"] = fields["layers"]
                else:
                    x.pop("layers", None)
            if "theme_record" in fields:
                rec = fields["theme_record"]
                themes = self.project.mapThemeCollection()
                if rec is None:
                    if x.get("theme") and themes.hasMapTheme(x["theme"]):
                        themes.removeMapTheme(x["theme"])
                    x.pop("theme", None)
                    x.pop("layers", None)
                elif scope == PROJECT:
                    x["theme"] = self._put_theme(b.name(), sid, rec, x.get("theme"))
            x["updated"] = _now()
            self._write(scope)
        finally:
            self._busy -= 1
        self.changed.emit()
        return self.get(sid)

    def rename(self, sid, name):
        return self.update(sid, name=name)

    def snapshot(self, sid):
        """Everything needed to bring a deleted stop back (undo)."""
        scope = self.scope_of(sid)
        if scope is None:
            return None
        b = self.manager(scope).bookmarkById(sid)
        x = json.loads(json.dumps(self.extras(scope)["stops"].get(sid, {})))
        rec = None
        if x.get("theme") and self.project.mapThemeCollection().hasMapTheme(x["theme"]):
            rec = self.project.mapThemeCollection().mapThemeState(x["theme"])
        order = self.extras(scope)["order"]
        pos = order.index(sid) if sid in order else None
        return {"scope": scope, "bookmark": QgsBookmark(b), "extras": x, "theme_record": rec, "position": pos}

    def delete(self, ids):
        """Delete stops; returns the snapshots for restore() (undo)."""
        snaps = []
        self._busy += 1
        try:
            for sid in ids:
                snap = self.snapshot(sid)
                if snap is None:
                    continue
                snaps.append(snap)
                self.manager(snap["scope"]).removeBookmark(sid)
            for scope in {s["scope"] for s in snaps}:
                self._write(scope)  # drops their extras and map themes
        finally:
            self._busy -= 1
        if snaps:
            self.changed.emit()
        return snaps

    def restore(self, snaps):
        """Undo a delete: the same ids, names, pictures, map themes and places in the order."""
        self._busy += 1
        try:
            for snap in sorted(snaps, key=lambda s: (s["position"] is None, s["position"] or 0)):
                scope = snap["scope"]
                m = self.manager(scope)
                b = QgsBookmark(snap["bookmark"])
                if m.bookmarkById(b.id()).id():
                    continue
                new_id, ok = m.addBookmark(b)
                if not ok:
                    continue
                x = snap["extras"]
                if snap["theme_record"] is not None and x.get("theme"):
                    x["theme"] = self._put_theme(b.name(), new_id, snap["theme_record"], x["theme"])
                data = self.extras(scope)
                data["stops"][new_id] = x
                order = [s for s in data["order"] if s != new_id]
                if snap["position"] is None:
                    order.append(new_id)
                else:
                    order.insert(min(snap["position"], len(order)), new_id)
                data["order"] = order
                self._write(scope)
        finally:
            self._busy -= 1
        self.changed.emit()

    def duplicate(self, sid):
        s = self.get(sid)
        if s is None:
            return None
        rec = None
        if s.theme and self.project.mapThemeCollection().hasMapTheme(s.theme):
            rec = self.project.mapThemeCollection().mapThemeState(s.theme)
        pos = [t.id for t in self.stops(s.scope)].index(sid) + 1
        return self.add(tr("{name} (copy)").format(name=s.name), s.frame, s.scope, s.group, s.note, s.color,
                        s.scale, s.thumb, s.layers or None, rec, s.time, position=pos)

    def move_to_scope(self, sid, scope):
        """Between this project and all my projects (same id; the map theme becomes a list of layers)."""
        src = self.scope_of(sid)
        if src is None or src == scope:
            return self.get(sid)
        self._busy += 1
        try:
            x = self.extras(src)["stops"].get(sid, {})
            if not self.manager(src).moveBookmark(sid, self.manager(scope)):
                return None
            x = json.loads(json.dumps(x))
            if src == PROJECT and x.get("theme"):
                themes = self.project.mapThemeCollection()
                if themes.hasMapTheme(x["theme"]):
                    themes.removeMapTheme(x["theme"])
                x.pop("theme", None)
            dst = self.extras(scope)
            dst["stops"][sid] = x
            dst["order"] = [s for s in dst["order"] if s != sid] + [sid]
            self._write(src)
            self._write(scope)
        finally:
            self._busy -= 1
        self.changed.emit()
        return self.get(sid)

    def reorder(self, ids):
        """A new order (ids of one or both scopes, in the order wanted); stops not listed keep their place after."""
        for scope in SCOPES:
            mine = [s for s in ids if self.manager(scope).bookmarkById(s).id()]
            if not mine:
                continue
            rest = [s.id for s in self.stops(scope) if s.id not in set(mine)]
            self.extras(scope)["order"] = mine + rest
            self._busy += 1
            try:
                self._write(scope)
            finally:
                self._busy -= 1
        self.changed.emit()

    def set_group(self, ids, group):
        for sid in ids:
            self.update(sid, group=group)

    def rename_group(self, old, new):
        self._busy += 1
        try:
            for scope in SCOPES:
                self.manager(scope).renameGroup(old, new)
        finally:
            self._busy -= 1
        self.changed.emit()

    # ---- map themes ---------------------------------------------------------------------------------------------
    def _theme_name(self, name, sid):
        base = tr("Stop · {name}").format(name=name)
        themes = self.project.mapThemeCollection()
        stops = self.extras(PROJECT)["stops"]
        others = {x.get("theme") for k, x in stops.items() if k != sid}
        own = stops.get(sid, {}).get("theme")
        cand, n = base, 2
        while cand in others or (themes.hasMapTheme(cand) and cand != own):
            cand = f"{base} ({n})"
            n += 1
        return cand

    def _put_theme(self, name, sid, record, old=None):
        themes = self.project.mapThemeCollection()
        if old and themes.hasMapTheme(old):
            themes.update(old, record)
            return old
        tname = self._theme_name(name, sid)
        if themes.hasMapTheme(tname):
            themes.update(tname, record)
        else:
            themes.insert(tname, record)
        return tname

    def _rename_theme(self, old, name, sid):
        themes = self.project.mapThemeCollection()
        want = self._theme_name(name, sid)
        if themes.hasMapTheme(old) and want != old and not themes.hasMapTheme(want):
            themes.renameMapTheme(old, want)
            return want
        return old

    # ---- capturing the current view -----------------------------------------------------------------------------
    def capture(self, canvas, name=None, scope=None, group="", note="", with_layers=None, thumb=True,
                position=None):
        """A new stop from what the map shows now. The picture is taken from the screen when the map is drawn,
        else drawn in the background (the stop is there at once)."""
        frame = Frame.from_canvas(canvas)
        scope = scope if scope in SCOPES else setting("scope")
        if with_layers is None:
            with_layers = setting("remember_layers")
        layers, rec = None, None
        if with_layers:
            layers = visible_layers(self.project)
            if scope == PROJECT:
                from qgis.core import QgsMapThemeCollection

                rec = QgsMapThemeCollection.createThemeFromCurrentState(
                    self.project.layerTreeRoot(), layer_tree_model(self.iface, self.project))
        time = current_time(canvas)
        name = name or auto_name(self, canvas)
        pic = grab_canvas(canvas) if thumb else b""
        stop = self.add(name, frame, scope, group, note, "", canvas.scale(), pic, layers, rec, time,
                        position=position)
        if stop is not None and thumb and not pic:
            self.request_thumb(stop.id, canvas.mapSettings(), frame)
        return stop

    def update_view(self, sid, canvas):
        """"Update to current view": the frame, scale, time and (when it remembers them) the layers shown."""
        s = self.get(sid)
        if s is None:
            return None
        fields = {"frame": Frame.from_canvas(canvas), "scale": canvas.scale(), "time": current_time(canvas)}
        if s.remembers_layers():
            fields["layers"] = visible_layers(self.project)
            if s.scope == PROJECT:
                from qgis.core import QgsMapThemeCollection

                fields["theme_record"] = QgsMapThemeCollection.createThemeFromCurrentState(
                    self.project.layerTreeRoot(), layer_tree_model(self.iface, self.project))
        pic = grab_canvas(canvas)
        if pic:
            fields["thumb"] = pic
        out = self.update(sid, **fields)
        if not pic:
            self.request_thumb(sid, canvas.mapSettings(), fields["frame"])
        return out

    def remember_layers(self, sid, on):
        if not on:
            return self.update(sid, theme_record=None, layers=None)
        s = self.get(sid)
        if s is None:
            return None
        fields = {"layers": visible_layers(self.project)}
        if s.scope == PROJECT:
            from qgis.core import QgsMapThemeCollection

            fields["theme_record"] = QgsMapThemeCollection.createThemeFromCurrentState(
                self.project.layerTreeRoot(), layer_tree_model(self.iface, self.project))
        return self.update(sid, **fields)

    def request_thumb(self, sid, base_settings, frame=None, wait=False):
        """Draw a stop's picture from the map (in the background unless wait)."""
        s = self.get(sid)
        if s is None:
            return None
        ms = thumb_settings(base_settings, frame or s.frame)
        if wait:
            data = render_thumb(ms)
            self.update(sid, thumb=data)
            return data
        job = ThumbJob(sid, ms, parent=self)
        self._jobs.append(job)

        def done(stop_id, data, job=job):
            if job in self._jobs:
                self._jobs.remove(job)
            if data and self.get(stop_id) is not None:
                self.update(stop_id, thumb=data)
            job.deleteLater()

        job.done.connect(done)
        job.start()
        return job

    # ---- showing a stop -----------------------------------------------------------------------------------------
    def target(self, stop, canvas):
        """(centre, map units per pixel, rotation) of a stop on this canvas (its CRS may differ). ValueError when it
        cannot be shown there (or has no place: a QGIS bookmark whose empty extent QGIS did not keep)."""
        if not stop.frame.is_valid():
            raise ValueError("no place")
        dest = canvas.mapSettings().destinationCrs()
        frame = stop.frame.transformed(dest, self.project) if dest.isValid() else stop.frame
        keep = setting("keep_scale") and stop.scale > 0 and stop.crs == dest
        centre, mupp = fit_frame(frame, canvas, keep, stop.scale)
        return centre, mupp, frame.rot

    def show_extras(self, stop, canvas):
        """The layers shown and the time range of a stop (after the view)."""
        if stop.theme and self.project.mapThemeCollection().hasMapTheme(stop.theme):
            self.project.mapThemeCollection().applyTheme(stop.theme, self.project.layerTreeRoot(),
                                                         layer_tree_model(self.iface, self.project))
        elif stop.layers:
            apply_layers(stop.layers, self.project)
        if stop.time:
            set_time(canvas, stop.time)


def display_order(stops):
    """Stops without a group first, then each group in the order its first stop appears (stable inside)."""
    sections = {}
    for s in stops:
        sections.setdefault(s.group, []).append(s)
    keys = ([""] if "" in sections else []) + [g for g in sections if g != ""]
    return [s for g in keys for s in sections[g]]


def _rect_list(r):
    return [r.xMinimum(), r.yMinimum(), r.xMaximum(), r.yMaximum()]


# ---- time ------------------------------------------------------------------------------------------------------
def current_time(canvas):
    """(begin, end) ISO texts when the map shows a time range, else None."""
    try:
        if not canvas.mapSettings().isTemporal():
            return None
        r = canvas.temporalRange()
    except AttributeError:
        return None
    if not r.begin().isValid() and not r.end().isValid():
        return None
    fmt = Qt.DateFormat.ISODateWithMs
    return (r.begin().toString(fmt) if r.begin().isValid() else "", r.end().toString(fmt) if r.end().isValid() else "")


def set_time(canvas, time):
    fmt = Qt.DateFormat.ISODateWithMs
    begin = QDateTime.fromString(time[0], fmt) if time[0] else QDateTime()
    end = QDateTime.fromString(time[1], fmt) if time[1] else QDateTime()
    rng = QgsDateTimeRange(begin, end)
    ctrl = canvas.temporalController()
    if ctrl is not None and hasattr(ctrl, "setTemporalExtents"):
        try:
            from qgis.core import Qgis

            ctrl.setNavigationMode(Qgis.TemporalNavigationMode.FixedRange)
        except (AttributeError, TypeError):
            pass
        ctrl.setTemporalExtents(rng)
    canvas.setTemporalRange(rng)


# ---- the screen picture -----------------------------------------------------------------------------------------
def grab_canvas(canvas):
    """The map as it is on the screen (instant), when it is shown and finished drawing; b"" otherwise."""
    try:
        if not canvas.isVisible() or canvas.isDrawing() or canvas.width() < 32 or canvas.height() < 32:
            return b""
        vp = canvas.viewport() if hasattr(canvas, "viewport") else canvas
        img = vp.grab().toImage()
    except (AttributeError, RuntimeError):
        return b""
    if img.isNull():
        return b""
    return thumb_from_image(img)


# ---- names -----------------------------------------------------------------------------------------------------
def format_scale(scale):
    if not scale or not math.isfinite(scale) or scale <= 0:
        return ""
    s = float(scale)
    if s >= 10000:
        mag = 10 ** (int(math.log10(s)) - 2)
        s = round(s / mag) * mag
    else:
        s = round(s)
    return "1:" + f"{int(s):,}"


def place_name(lon, lat, scale):
    """The name of the place under the map centre from Biruni's own offline world data: a city when the map is
    close enough to be about a city, else the country. None at sea or far from everything."""
    try:
        from .. import places
        from qgis.core import QgsFeatureRequest
    except ImportError:
        return None
    if not (math.isfinite(lon) and math.isfinite(lat)):
        return None
    if scale and scale <= 3_000_000:
        lyr = _cities()
        if lyr is not None:
            best, best_d = None, None
            for r in (0.35, 1.0):
                req = QgsFeatureRequest().setFilterRect(QgsRectangle(lon - r, lat - r, lon + r, lat + r))
                for f in lyr.getFeatures(req):
                    p = f.geometry().asPoint()
                    d = math.hypot((p.x() - lon) * math.cos(math.radians(lat)), p.y() - lat)
                    if best_d is None or d < best_d:
                        best, best_d = f, d
                if best is not None:
                    break
            # within ~40 km of a city: say the city
            if best is not None and best_d * 111.32 <= 40:
                return best["name"] or best["nameascii"]
    try:
        return places.country_at(lon, lat)
    except Exception:  # noqa: BLE001 - the world data is optional
        return None


_CITY = {}


def _cities():
    if "layer" not in _CITY:
        from .. import places

        lyr = places.world_layer("cities") if os.path.exists(places.WORLD) else None
        _CITY["layer"] = lyr if lyr is not None and lyr.isValid() else None
    return _CITY["layer"]


def auto_name(store, canvas):
    """"Riyadh — 1:2,500" (the place under the map centre), else "Stop 3 — 1:2,500"."""
    scale = canvas.scale()
    sc = format_scale(scale)
    place = None
    if setting("place_names"):
        crs = canvas.mapSettings().destinationCrs()
        c = canvas.center()
        try:
            if crs.isValid():
                xf = QgsCoordinateTransform(crs, QgsCoordinateReferenceSystem("EPSG:4326"), store.project)
                p = xf.transform(c)
                place = place_name(p.x(), p.y(), scale)
        except Exception:  # noqa: BLE001 - a view that cannot be put on the globe
            place = None
    if not place:
        place = tr("Stop {n}").format(n=len(store.stops()) + 1)
    name = f"{place} — {sc}" if sc else place
    taken = {s.name for s in store.stops()}
    if name in taken:
        n = 2
        while f"{name} ({n})" in taken:
            n += 1
        name = f"{name} ({n})"
    return name


# ---- search ----------------------------------------------------------------------------------------------------
def search(stops, query):
    """Stops whose name, group or note contain every word of the query - Arabic-aware (hamza forms, ta marbuta,
    alef maqsura, tashkeel, Arabic-Indic digits all match their plain forms)."""
    from ..arabic_text import normalise

    words = [w for w in normalise(query).split(" ") if w]
    if not words:
        return list(stops)
    out = []
    for s in stops:
        hay = normalise(s.search_text())
        hay2 = normalise(s.search_text(), strip_prefixes=True)
        if all(w in hay or w in hay2 for w in words):
            out.append(s)
    return out


# ---- one store per project ---------------------------------------------------------------------------------------
_STORES = {}


def store_for(plugin=None, project=None):
    project = project or QgsProject.instance()
    st = _STORES.get(id(project))
    if st is None:
        st = StopsStore(project, getattr(plugin, "iface", None))
        _STORES[id(project)] = st
    elif plugin is not None and st.iface is None:
        st.iface = plugin.iface
    return st


def new_id():
    return "{" + str(uuid.uuid4()) + "}"
