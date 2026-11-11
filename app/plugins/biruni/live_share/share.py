"""A live share: the share server, the hidden host page and the map service, started and
stopped together. The map service answers the viewers' requests in Biruni's main thread:
it draws the map canvas's visible layers, with the project's styling and labels, in
EPSG:3857 for the view a viewer asks for; it reads the features under a tap; and it counts
the map's changes (edits, styles, layers shown or hidden) so viewers redraw their view.
"""
import json
import math
import time

from qgis.core import (QgsCoordinateReferenceSystem, QgsCoordinateTransform, QgsCsException, QgsExpression,
                       QgsExpressionContext, QgsExpressionContextUtils, QgsFeatureRequest, QgsGeometry,
                       QgsMapRendererParallelJob, QgsMapSettings, QgsPointXY, QgsProject,
                       QgsRasterLayer, QgsRectangle, QgsVectorLayer)
from qgis.PyQt.QtCore import QBuffer, QByteArray, QIODevice, QObject, QSize, QTimer

from ..field_photos import bridge
from .server import SHARE_PORT, ShareServer

MERC = QgsCoordinateReferenceSystem("EPSG:3857")
MERC_MAX = 20037508.342789244
MAX_FEATURES = 8       # features listed for one tap
MAX_FIELDS = 40        # attributes shown per feature
MAX_TEXT = 300         # characters per value
RUNNING = 2            # map images drawn at once
PLAIN = ("", "TextEdit", "Range", "Hidden")  # widgets whose value is shown as it is
CHANGED = ("repaintRequested", "styleChanged", "rendererChanged", "dataChanged", "layerModified",
           "subsetStringChanged", "nameChanged")


class MapService(QObject):
    def __init__(self, canvas, server, attributes=True, parent=None):
        super().__init__(parent)
        self.canvas = canvas
        self.server = server
        self.attributes = attributes
        self.renders = 0
        self._running = []
        self._pending = []
        self._links = []
        self._last_bump = 0.0
        self._timer = QTimer(self)
        self._timer.setInterval(30)
        self._timer.timeout.connect(self._drain)
        self._debounce = QTimer(self)
        self._debounce.setSingleShot(True)
        self._debounce.setInterval(700)
        self._debounce.timeout.connect(self._bump)

    def start(self):
        project = QgsProject.instance()
        for sig, slot in ((project.layersAdded, self._layers_changed), (project.layersRemoved, self._layers_changed),
                          (self.canvas.layersChanged, self._layers_changed)):
            sig.connect(slot)
            self._links.append((sig, slot))
        self._watch()
        self._timer.start()

    def stop(self):
        self._timer.stop()
        self._debounce.stop()
        for sig, slot in self._links:
            try:
                sig.disconnect(slot)
            except (TypeError, RuntimeError):
                pass
        self._links = []
        self._unwatch()
        for job, rj in self._running:
            try:
                rj.cancelWithoutBlocking()
            except RuntimeError:
                pass
            job.finish(503)
        self._running = []
        for job in self._pending:
            job.finish(503)
        self._pending = []

    # ---- what changes the map --------------------------------------------------------
    def _watch(self):
        self._unwatch()
        self._layer_links = []
        for lyr in self.canvas.layers():
            for name in CHANGED:
                sig = getattr(lyr, name, None)
                if sig is not None and hasattr(sig, "connect"):
                    sig.connect(self.changed)
                    self._layer_links.append(sig)

    def _unwatch(self):
        for sig in getattr(self, "_layer_links", ()):
            try:
                sig.disconnect(self.changed)
            except (TypeError, RuntimeError):
                pass
        self._layer_links = []

    def _layers_changed(self, *_a):
        self._watch()
        self.changed()

    def changed(self, *_a):
        """Something on the map changed: viewers redraw, at most every 1.5 s."""
        if not self._debounce.isActive():
            self._debounce.setInterval(max(700, int(1500 - (time.monotonic() - self._last_bump) * 1000)))
            self._debounce.start()

    def _bump(self):
        self._last_bump = time.monotonic()
        self.server.bump()

    # ---- the requests ------------------------------------------------------------------
    def _drain(self):
        while not self.server.jobs.empty():
            job = self.server.jobs.get_nowait()
            if job.kind == "render":
                self._pending.append(job)
                continue
            try:
                body = self.info() if job.kind == "info" else self.identify(**job.args)
                job.finish(200, json.dumps(body, ensure_ascii=False).encode("utf-8"))
            except Exception as e:  # noqa: BLE001 - a viewer's request never stops Biruni
                job.finish(500, str(e).encode("utf-8", "replace")[:200], "text/plain")
        self._pending = [j for j in self._pending if not j.cancelled]
        while self._pending and len(self._running) < RUNNING:
            self._render(self._pending.pop(0))

    def map_settings(self, a):
        ms = QgsMapSettings(self.canvas.mapSettings())
        ms.setDestinationCrs(MERC)
        ms.setRotation(0)
        ms.setMagnificationFactor(1.0)
        ms.setDevicePixelRatio(1.0)
        ms.setOutputDpi(96 * a["d"])  # symbols and labels as big as on a normal screen
        ms.setOutputSize(QSize(a["pw"], a["ph"]))
        ms.setExtent(QgsRectangle(a["x0"], a["y0"], a["x1"], a["y1"]))
        return ms

    def _render(self, job):
        ms = self.map_settings(job.args)
        rj = QgsMapRendererParallelJob(ms)
        self._running.append((job, rj))
        rj.finished.connect(lambda: self._rendered(job, rj, ms))
        rj.start()

    def _rendered(self, job, rj, ms):
        self._running = [(j, r) for j, r in self._running if j is not job]
        if job.cancelled or job.done.is_set():
            return
        img = rj.renderedImage()
        photo = any(isinstance(lyr, QgsRasterLayer) for lyr in ms.layers())
        data = QByteArray()
        buf = QBuffer(data)
        buf.open(QIODevice.OpenModeFlag.WriteOnly)
        img.save(buf, "JPG" if photo else "PNG", 82 if photo else -1)
        buf.close()
        e = ms.visibleExtent()
        self.renders += 1
        job.finish(200, bytes(data), "image/jpeg" if photo else "image/png",
                   {"X-Extent": ",".join(f"{v:.3f}" for v in (e.xMinimum(), e.yMinimum(), e.xMaximum(),
                                                               e.yMaximum()))})

    def info(self):
        project = QgsProject.instance()
        title = project.title() or project.baseName() or ""
        layers = [lyr.name() for lyr in self.canvas.layers()]
        return {"title": title[:120], "extent": self.extent(), "layers": layers[:200], "v": self.server.version,
                "attrs": bool(self.attributes), "bg": self.canvas.mapSettings().backgroundColor().name()}

    def extent(self):
        """The canvas's view in EPSG:3857 (where the viewers start)."""
        ms = self.canvas.mapSettings()
        r = QgsRectangle(self.canvas.extent())
        crs = ms.destinationCrs()
        if crs.isGeographic():
            r = r.intersect(QgsRectangle(-180, -85, 180, 85))
        try:
            out = QgsCoordinateTransform(crs, MERC, QgsProject.instance()).transformBoundingBox(r)
        except QgsCsException:
            out = QgsRectangle()
        vals = [out.xMinimum(), out.yMinimum(), out.xMaximum(), out.yMaximum()]
        if out.isEmpty() or not all(math.isfinite(v) for v in vals):
            return [-MERC_MAX, -MERC_MAX / 2, MERC_MAX, MERC_MAX / 2]
        return [max(-MERC_MAX, vals[0]), max(-MERC_MAX, vals[1]), min(MERC_MAX, vals[2]), min(MERC_MAX, vals[3])]

    def identify(self, x, y, r):
        """The features near a point (EPSG:3857) in the shown vector layers, top layer first."""
        if not self.attributes:
            return {"off": True, "features": []}
        project = QgsProject.instance()
        probe = QgsGeometry.fromPointXY(QgsPointXY(x, y))
        area = QgsRectangle(x - r, y - r, x + r, y + r)
        out, more = [], False
        for lyr in self.canvas.layers():
            if not isinstance(lyr, QgsVectorLayer) or not lyr.isSpatial() or not lyr.isValid():
                continue
            try:
                there = QgsCoordinateTransform(MERC, lyr.crs(), project).transformBoundingBox(area)
            except QgsCsException:
                continue
            back = QgsCoordinateTransform(lyr.crs(), MERC, project)
            hits = []
            req = QgsFeatureRequest().setFilterRect(there).setLimit(200)
            for f in lyr.getFeatures(req):
                g = QgsGeometry(f.geometry())
                try:
                    g.transform(back)
                except QgsCsException:
                    continue
                d = g.distance(probe)
                if 0 <= d <= r:
                    hits.append((d, f))
            hits.sort(key=lambda t: t[0])
            for _d, f in hits:
                if len(out) == MAX_FEATURES:
                    more = True
                    break
                out.append(self._describe(lyr, f))
            if more:
                break
        return {"features": out, "more": more}

    def _describe(self, lyr, f):
        from qgis.core import QgsApplication

        ctx = QgsExpressionContext(QgsExpressionContextUtils.globalProjectLayerScopes(lyr))
        ctx.setFeature(f)
        title = ""
        if lyr.displayExpression():
            v = QgsExpression(lyr.displayExpression()).evaluate(ctx)
            title = "" if v is None else _text(v)
        attrs = []
        formatters = QgsApplication.fieldFormatterRegistry()
        for idx, field in enumerate(lyr.fields()):
            if len(attrs) == MAX_FIELDS:
                break
            setup = lyr.editorWidgetSetup(idx)
            if setup.type() == "Hidden":
                continue
            value = f.attribute(idx)
            if isinstance(value, (bytes, bytearray, QByteArray)):
                continue
            shown = value
            if setup.type() not in PLAIN:  # value maps, relations, dates, check boxes: as the form shows them
                try:
                    fmt = formatters.fieldFormatter(setup.type())
                    shown = fmt.representValue(lyr, idx, setup.config(), None, value)
                except Exception:  # noqa: BLE001 - the raw value then
                    pass
            attrs.append([lyr.attributeDisplayName(idx)[:80], _text(shown)])
        return {"layer": lyr.name()[:120], "title": title, "attrs": attrs}


def _text(v):
    if v is None or (hasattr(v, "isNull") and v.isNull()):
        return ""
    if isinstance(v, float) and v.is_integer() and abs(v) < 1e15:
        v = int(v)
    s = v.toString() if hasattr(v, "toString") and not isinstance(v, str) else str(v)
    return s if len(s) <= MAX_TEXT else s[:MAX_TEXT - 1] + "…"


class LiveShare:
    """One share: its own new key (the link dies with it), the server, the hidden host and
    the map service. poll() reads what the host page reports."""

    def __init__(self, canvas, cert_folder, attributes=True, port=SHARE_PORT):
        self.canvas = canvas
        self.cert_folder = cert_folder
        self.port = port
        self.attributes = attributes
        self.server = self.host = self.maps = None
        self.key = None
        self.viewers = 0
        self.relays_up = 0
        self.started = 0.0

    def start(self):
        """Raises OSError when the port is busy or the hidden page cannot run."""
        self.server = ShareServer(self.cert_folder, self.port)
        try:
            self.maps = MapService(self.canvas, self.server, self.attributes)
            self.maps.start()
            self.key = bridge.new_key()
            self.host = bridge.BridgeHost(self.server.url() + "host#" + bridge.host_frag(self.key))
        except Exception:
            self.stop()
            raise
        self.started = time.monotonic()
        self.viewers = self.relays_up = 0

    def url(self, lang="en"):
        from .viewer import share_url

        return share_url(self.key, lang)

    def set_attributes(self, on):
        self.attributes = bool(on)
        if self.maps is not None:
            self.maps.attributes = self.attributes

    def running(self):
        return self.server is not None

    def poll(self):
        """[(kind, value)]: ("host", event dict) from the page, ("ended", detail) when it stopped."""
        out = []
        if self.server is not None:
            while not self.server.events.empty():
                kind, ev = self.server.events.get_nowait()
                t = ev.get("type")
                if t == "viewers":
                    self.viewers = max(0, int(ev.get("n") or 0))
                elif t == "relays":
                    self.relays_up = int(ev.get("up") or 0)
                out.append((kind, ev))
        if self.host is not None:
            while not self.host.events.empty():
                kind, value = self.host.events.get_nowait()
                if kind == "ended" and self.server is not None:
                    out.append(("ended", (self.host.log or [str(value)])[-1][-160:]))
        return out

    def stop(self, wait=2.0):
        """Tells the viewers that sharing ended, then ends the host, the server and the map service."""
        if self.server is not None and self.host is not None and self.host.running():
            self.server.announce_stop()
            end = time.monotonic() + wait
            said = False
            while time.monotonic() < end and not said and self.host.running():
                try:
                    kind, ev = self.server.events.get(timeout=0.05)
                    said = ev.get("type") == "bye"
                except Exception:  # noqa: BLE001 - queue.Empty
                    pass
        if self.host is not None:
            self.host.stop()
            self.host = None
        if self.maps is not None:
            self.maps.stop()
            self.maps.deleteLater()
            self.maps = None
        if self.server is not None:
            try:
                self.server.stop()
            except OSError:
                pass
            self.server = None
        self.viewers = 0
        self.key = None
