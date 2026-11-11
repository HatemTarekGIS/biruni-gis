"""The basemap under a dashboard map (SPEC.md, Changes: "basemap"): the project's own background layers - the
orthophoto, an XYZ/OSM layer, an offline PMTiles basemap - drawn by QGIS on this PC for the view a viewer is
looking at, and sent as one JPEG.

Only raster and vector-tile layers that are visible in the project are drawn: never a vector layer, so a
basemap image can carry no feature or attribute a card may not see. With a card limited to a zone, the image
never reaches far beyond that zone. Drawing happens in Biruni's main thread (QgsMapRendererParallelJob, as the
"See my map" share does), at most RUNNING at once; a viewer's newer view replaces its older one still waiting.
"""
import base64
import math

from qgis.core import (QgsCoordinateReferenceSystem, QgsCoordinateTransform, QgsCsException, QgsGeometry,
                       QgsMapLayerType, QgsMapRendererParallelJob, QgsMapSettings, QgsRectangle)
from qgis.PyQt.QtCore import QBuffer, QByteArray, QIODevice, QObject, QSize
from qgis.PyQt.QtGui import QColor

MERC = QgsCoordinateReferenceSystem("EPSG:3857")
WGS84 = QgsCoordinateReferenceSystem("EPSG:4326")
RUNNING = 2
MAX_SIDE = 1600
QUALITY = 80
ZONE_MARGIN = 0.25


def basemap_layers(project):
    """The project's visible raster and vector-tile layers, top first (what a basemap is made of)."""
    root = project.layerTreeRoot()
    out = []
    for lyr in root.layerOrder():
        node = root.findLayer(lyr.id())
        if node is None or not node.isVisible():
            continue
        if lyr.type() in (QgsMapLayerType.RasterLayer, QgsMapLayerType.VectorTileLayer):
            out.append(lyr)
    return out


def _num(v, lo, hi):
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return x if math.isfinite(x) and lo <= x <= hi else None


def view_args(msg):
    """The view asked for, checked: {"bbox": [w, s, e, n] in 4326, "pw", "ph"} or None."""
    b = msg.get("bbox")
    if not isinstance(b, list) or len(b) != 4:
        return None
    w, s, e, n = (_num(b[0], -180, 180), _num(b[1], -85, 85), _num(b[2], -180, 180), _num(b[3], -85, 85))
    if None in (w, s, e, n) or e <= w or n <= s:
        return None
    cw, ch, d = _num(msg.get("w"), 16, 4000), _num(msg.get("h"), 16, 4000), _num(msg.get("d") or 1, 0.5, 3)
    if None in (cw, ch, d):
        return None
    pw, ph = cw * d, ch * d
    k = min(1.0, MAX_SIDE / max(pw, ph))
    return {"bbox": [w, s, e, n], "pw": max(16, int(pw * k)), "ph": max(16, int(ph * k))}


def clamp_to_zone(bbox, zone_wkt):
    """The bbox cut to the card's zone (with a margin); None when they do not meet."""
    if not zone_wkt:
        return bbox
    g = QgsGeometry.fromWkt(zone_wkt)
    if g.isEmpty():
        return None
    z = g.boundingBox()
    mx, my = z.width() * ZONE_MARGIN + 1e-4, z.height() * ZONE_MARGIN + 1e-4
    z = QgsRectangle(z.xMinimum() - mx, z.yMinimum() - my, z.xMaximum() + mx, z.yMaximum() + my)
    r = QgsRectangle(*bbox).intersect(z)
    if r.isEmpty() or r.width() <= 0 or r.height() <= 0:
        return None
    return [r.xMinimum(), r.yMinimum(), r.xMaximum(), r.yMaximum()]


class Basemaps(QObject):
    def __init__(self, project, parent=None):
        super().__init__(parent)
        self.project = project
        self.running = {}    # key -> job
        self.waiting = {}    # key -> (session, widget id, args)
        self.renders = 0

    def request(self, session, wid, args, zone=None):
        bbox = clamp_to_zone(args["bbox"], zone)
        if bbox is None:
            session._out({"t": "basemap", "widget": wid, "bbox": args["bbox"], "img": None})
            return
        a = dict(args)
        if bbox != args["bbox"]:   # keep the pixel size in proportion to the part that is drawn
            fx = (bbox[2] - bbox[0]) / (args["bbox"][2] - args["bbox"][0])
            fy = (bbox[3] - bbox[1]) / (args["bbox"][3] - args["bbox"][1])
            a["pw"], a["ph"] = max(16, int(a["pw"] * fx)), max(16, int(a["ph"] * fy))
        a["bbox"] = bbox
        self.waiting[(session.id, wid)] = (session, wid, a)
        self._next()

    def _next(self):
        for key in list(self.waiting):
            if len(self.running) >= RUNNING:
                return
            if key in self.running:
                continue
            session, wid, a = self.waiting.pop(key)
            if session.closed:
                continue
            self._start(key, session, wid, a)

    def _start(self, key, session, wid, a):
        layers = basemap_layers(self.project)
        if not layers:
            session._out({"t": "basemap", "widget": wid, "bbox": a["bbox"], "img": None})
            return
        tr = QgsCoordinateTransform(WGS84, MERC, self.project)
        try:
            ext = tr.transformBoundingBox(QgsRectangle(*a["bbox"]))
        except QgsCsException:
            session._out({"t": "basemap", "widget": wid, "bbox": a["bbox"], "img": None})
            return
        ms = QgsMapSettings()
        ms.setLayers(layers)
        ms.setDestinationCrs(MERC)
        ms.setOutputSize(QSize(a["pw"], a["ph"]))
        ms.setOutputDpi(96)
        ms.setBackgroundColor(QColor(self.project.backgroundColor()) if self.project.backgroundColor().isValid()
                              else QColor("#f2efe9"))
        ms.setExtent(ext)
        ms.setTransformContext(self.project.transformContext())
        job = QgsMapRendererParallelJob(ms)
        self.running[key] = job
        job.finished.connect(lambda: self._done(key, session, wid, a, job, ms))
        job.start()

    def _done(self, key, session, wid, a, job, ms):
        self.running.pop(key, None)
        try:
            if not session.closed:
                img = job.renderedImage()
                buf_data = QByteArray()
                buf = QBuffer(buf_data)
                buf.open(QIODevice.OpenModeFlag.WriteOnly)
                img.save(buf, "JPG", QUALITY)
                buf.close()
                # the extent drawn (the map settings widen one side to keep the pixels square)
                tr = QgsCoordinateTransform(MERC, WGS84, self.project)
                try:
                    e = tr.transformBoundingBox(ms.visibleExtent())
                    box = [round(e.xMinimum(), 7), round(e.yMinimum(), 7), round(e.xMaximum(), 7),
                           round(e.yMaximum(), 7)]
                except QgsCsException:
                    box = a["bbox"]
                self.renders += 1
                session._out({"t": "basemap", "widget": wid, "bbox": box,
                              "img": "data:image/jpeg;base64," + base64.b64encode(bytes(buf_data)).decode("ascii")})
        finally:
            self._next()

    def stop(self):
        for job in list(self.running.values()):
            try:
                job.cancelWithoutBlocking()
            except Exception:  # noqa: BLE001
                pass
        self.running.clear()
        self.waiting.clear()
