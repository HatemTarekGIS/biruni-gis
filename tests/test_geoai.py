"""Smart Digitise: a click on an image draws the outline (Segment Anything on the CPU, offline).

Without the model: the piece of the image read, pixels to map, a mask to a polygon, corners squared, the download
checked file by file, the import from a folder. With the model (set BIRUNI_SAM_MODELS to a folder holding its
files): buildings of a made-up orthophoto found from one click, and added to a layer in its edits.

Run:  engine\\bin\\python-qgis.bat tests\\test_geoai.py
"""
import hashlib
import os
import sys
import tempfile
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "app", "plugins"))

import numpy as np  # noqa: E402
from osgeo import gdal, osr  # noqa: E402
from qgis.core import (QgsApplication, QgsCoordinateReferenceSystem, QgsGeometry, QgsPointXY, QgsProject,  # noqa: E402
                       QgsRasterLayer, QgsRectangle, QgsVectorLayer)

os.environ["QGIS_CUSTOM_CONFIG_PATH"] = tempfile.mkdtemp()
app = QgsApplication([], True)
app.initQgis()

from biruni.geoai import core, model  # noqa: E402

failures = []


def check(cond, msg):
    print(("PASS " if cond else "FAIL ") + msg, flush=True)
    if not cond:
        failures.append(msg)


root = tempfile.mkdtemp()
X0, Y0, PX = 500000.0, 2700000.0, 0.1            # 0.1 m pixels
W, H = 2400, 1600
rng = np.random.default_rng(7)
img = np.clip(rng.normal(70, 12, (H, W, 3)), 0, 255).astype(np.uint8)    # dark ground
img[:, :, 1] = np.clip(img[:, :, 1].astype(int) + 15, 0, 255)          # a little green


def rect(c0, r0, c1, r1, color):
    img[r0:r1, c0:c1] = color


rect(300, 300, 500, 450, (225, 215, 200))             # a building of 20 x 15 m
rect(900, 200, 1100, 600, (200, 70, 60))               # a red roof, 20 x 40 m
rect(1500, 900, 1700, 1300, (230, 230, 235))           # an L: a 20 x 40 m wing …
rect(1700, 1100, 1900, 1300, (230, 230, 235))          # … and a 20 x 20 m one
yy, xx = np.mgrid[0:H, 0:W]
img[(xx - 600) ** 2 + (yy - 1200) ** 2 < 90 ** 2] = (40, 120, 200)    # a round pool, r = 9 m
tif = os.path.join(root, "ortho.tif")
ds = gdal.GetDriverByName("GTiff").Create(tif, W, H, 3, gdal.GDT_Byte)
ds.SetGeoTransform((X0, PX, 0, Y0 + H * PX, 0, -PX))
srs = osr.SpatialReference()
srs.ImportFromEPSG(32638)
ds.SetProjection(srs.ExportToWkt())
for b in range(3):
    ds.GetRasterBand(b + 1).WriteArray(img[:, :, b])
ds = None
ortho = QgsRasterLayer(tif, "Ortho")
QgsProject.instance().addMapLayer(ortho)
check(ortho.isValid() and core.native_size(ortho) == (PX, PX), "a 0.1 m orthophoto")


def at_px(c, r):
    return QgsPointXY(X0 + c * PX, Y0 + H * PX - r * PX)


# ---- the piece read -----------------------------------------------------------------------------------------
ext, w, h = core.plan_tile(ortho, at_px(400, 375))
check((w, h) == (1024, 1024) and abs(ext.width() - 102.4) < 1e-6, f"a piece of 1024 px at the image's own resolution ({w} x {h})")
check(ext.xMinimum() >= X0 - 1e-6 and ext.yMaximum() <= Y0 + H * PX + 1e-6, "kept inside the image (moved, not cut)")
small = QgsRectangle(at_px(350, 420), at_px(450, 330))
ext2, w2, h2 = core.plan_tile(ortho, at_px(400, 375), visible=small)
check(abs(ext2.width() - small.width()) < 1e-6 and w2 == 100, "zoomed in: what is seen, still at its own pixels")
t = core.tile_at(ortho, at_px(400, 375))
check(t.image.shape == (1024, 1024, 3), "read as the map shows it")
c, r = t.to_pixel(*core.Tile.to_map(t, 10.5, 20.5))
check(abs(c - 10.5) < 1e-6 and abs(r - 20.5) < 1e-6, "pixels to the map and back")
pc, pr = t.to_pixel(at_px(400, 375).x(), at_px(400, 375).y())
v = t.image[int(pr), int(pc)]
check(v[0] > 200 and v[2] > 180, f"the pixel under the building is the building's ({list(v)})")

# ---- a mask to a polygon --------------------------------------------------------------------------------------
m = np.zeros((t.height, t.width), dtype=bool)
r0, c0 = int(pr) - 75, int(pc) - 100
m[r0:r0 + 150, c0:c0 + 200] = True
m[r0 + 70:r0 + 74, c0 + 90:c0 + 94] = False       # a car on the roof
m[r0 + 10, c0 + 199] = False                      # a ragged edge
far = np.zeros_like(m)
far[10:30, 10:30] = True
g = core.mask_to_polygon(m | far, t, at_px(400, 375))
check(g is not None and abs(g.area() - 300) < 3, f"the mask under the click to a polygon: {g.area():.1f} m² (20 x 15 m)")
check(not g.asPolygon()[1:] if not g.isMultipart() else True, "the car on the roof is not a courtyard")
check(len(g.asPolygon()[0]) <= 8, f"the stairs of pixels made straight ({len(g.asPolygon()[0])} corners)")
check(abs(core.mask_to_polygon(m | far, t).area() - 300) < 3, "no click: the largest one")

wobbly = QgsGeometry.fromWkt("POLYGON((0 0,10.02 0.1,10.1 6.03,5 6.08,-0.05 5.95,0 0))")
sq = core.square(wobbly)
ring = sq.asPolygon()[0]
check(len(ring) == 5 and abs(sq.area() - wobbly.area()) / wobbly.area() < 0.03, "a wobbly building squared to its rectangle")
L = QgsGeometry.fromWkt("POLYGON((0 0,20 0.2,20.1 10,10 9.9,10.2 20,0.1 20.1,0 0))")
sl = core.square(L)
check(len(sl.asPolygon()[0]) == 7 and core.iou(sl, L) > 0.95, f"an L keeps its six corners, now right angles ({len(sl.asPolygon()[0]) - 1})")
circle = QgsGeometry.fromPointXY(QgsPointXY(0, 0)).buffer(9, 16)
check(core.square(circle).asWkt() == circle.asWkt(), "a round pool is left round")
check(len(core.shaped(L, "rectangle").asPolygon()[0]) == 5, "«rectangle» always gives its rectangle")
target = QgsVectorLayer("MultiPolygon?crs=EPSG:4326", "Buildings", "memory")
tg = core.to_layer(sq.buffer(0, 1), QgsCoordinateReferenceSystem("EPSG:32638"), target)
check(tg.isMultipart() and abs(tg.boundingBox().xMinimum()) < 180, "into the layer's coordinates and kind")

# ---- the model: download and import ----------------------------------------------------------------------------
fake_root = os.path.join(root, "models")
blobs = {"a.onnx": b"encoder" * 1000, "b.onnx": b"decoder" * 500}
fake = {"id": "fake", "name": "Fake", "license": "MIT", "source": "x", "license_url": "x",
        "files": [{"name": n, "size": len(b), "sha256": hashlib.sha256(b).hexdigest()} for n, b in blobs.items()]}
asked = []


def fetch(url, dest, progress):
    asked.append(url)
    name = url.rsplit("/", 1)[1]
    data = blobs[name] if name in blobs else b""
    with open(dest, "wb") as f:
        f.write(data)
    progress(len(data), len(data))


check(not model.installed(fake, fake_root), "not there before")
model.download(fake, fake_root, fetch)
check(model.installed(fake, fake_root) and all(model.REVISION in u for u in asked), "downloaded from the fixed revision, checked")
n0 = len(asked)
model.download(fake, fake_root, fetch)
check(len(asked) == n0, "already there: not downloaded again")
model.remove(fake, fake_root)
bad = dict(blobs)
bad["b.onnx"] = b"tampered" * 500


def fetch_bad(url, dest, progress):
    with open(dest, "wb") as f:
        f.write(bad[url.rsplit("/", 1)[1]])


try:
    model.download(fake, fake_root, fetch_bad)
    check(False, "a tampered download kept")
except model.ModelError as e:
    check(str(e) == "checksum" and not model.installed(fake, fake_root)
          and not os.path.exists(os.path.join(fake_root, "fake", "b.onnx.part")), "a tampered file: refused, nothing left")


def fetch_offline(url, dest, progress):
    raise OSError("no network")


try:
    model.download(fake, fake_root, fetch_offline)
except model.ModelError as e:
    check(str(e) == "offline", "no network: said so")
drive = os.path.join(root, "flash", "models")
os.makedirs(drive)
for n, b in blobs.items():
    with open(os.path.join(drive, n), "wb") as f:
        f.write(b)
model.import_from(os.path.join(root, "flash"), fake, fake_root)
check(model.installed(fake, fake_root), "imported from a flash drive, checked")
with open(os.path.join(fake_root, "fake", "a.onnx"), "ab") as f:
    f.write(b"x")
check(not model.installed(fake, fake_root), "a file changed afterwards: not taken as installed")
check(model.MODEL["license"].startswith("Apache-2.0") and model.total_size() < 50e6,
      f"the real model: free for any use, {model.total_size() / 1e6:.1f} MB")

# ---- with the real model ---------------------------------------------------------------------------------------
src = os.environ.get("BIRUNI_SAM_MODELS", "")
if not src or not os.path.isdir(src):
    print("SKIP the real model (set BIRUNI_SAM_MODELS to a folder with its files)")
elif not model.runtime_ok():
    check(False, "the runtime is installed")
else:
    model.import_from(src)
    check(model.installed(), "the real model imported and checked")
    seg = model.Segmenter()
    tiles = {}
    truth = {
        "building": (at_px(400, 375), QgsGeometry.fromRect(QgsRectangle(at_px(300, 450), at_px(500, 300)))),
        "red roof": (at_px(1000, 400), QgsGeometry.fromRect(QgsRectangle(at_px(900, 600), at_px(1100, 200)))),
        "L": (at_px(1600, 1100), QgsGeometry.fromRect(QgsRectangle(at_px(1500, 1300), at_px(1700, 900))).combine(
            QgsGeometry.fromRect(QgsRectangle(at_px(1700, 1300), at_px(1900, 1100))))),
        "pool": (at_px(600, 1200), QgsGeometry.fromPointXY(at_px(600, 1200)).buffer(9, 32)),
    }
    for name, (p, true) in truth.items():
        tile = core.tile_at(ortho, p)
        seg.encode(tile)
        enc = seg.last_encode
        mask, score = seg.segment(tile, [(p.x(), p.y(), True)])
        dec = seg.last_decode
        g = core.mask_to_polygon(mask, tile, p)
        q = core.iou(g, true) if g is not None else 0
        check(q > 0.85, f"{name}: found from one click, overlap {q:.2f} (seen {enc:.2f} s, drawn {dec:.3f} s)")
        tiles[name] = (tile, enc, dec)
    tile = tiles["L"][0]
    p = truth["L"][0]
    mask, _s = seg.segment(tile, [(p.x(), p.y(), True), (at_px(1800, 1200).x(), at_px(1800, 1200).y(), True)])
    g = core.square(core.mask_to_polygon(mask, tile, p))
    check(core.iou(g, truth["L"][1]) > 0.9, f"the L with a second click, squared: {core.iou(g, truth['L'][1]):.2f}")
    check(max(v[1] for v in tiles.values()) < 30 and max(v[2] for v in tiles.values()) < 1.0,
          "speeds within reach on this CPU")

    # the panel on a real canvas
    from qgis.gui import QgsMapCanvas
    from qgis.PyQt.QtWidgets import QWidget

    canvas = QgsMapCanvas()
    canvas.resize(800, 600)
    canvas.setDestinationCrs(ortho.crs())
    canvas.setLayers([ortho])
    canvas.setExtent(ortho.extent())
    bld = QgsVectorLayer("Polygon?crs=EPSG:32638&field=kind:string", "Buildings", "memory")
    QgsProject.instance().addMapLayer(bld)

    class Bar:
        def pushMessage(self, *a, **k):
            pass

    class Iface:
        def __init__(self):
            self.w = QWidget()

        def mainWindow(self):
            return self.w

        def mapCanvas(self):
            return canvas

        def activeLayer(self):
            return bld

        def messageBar(self):
            return Bar()

    class Plugin:
        iface = Iface()

    from biruni.geoai import ui

    panel = ui.open_ui(Plugin(), segmenter=seg)
    check(panel is not None and canvas.mapTool() is panel.tool and panel.image_layer() is ortho
          and panel.target_layer() is bld, "the panel takes the image and the layer, the click tool is on")
    panel.add_point(truth["building"][0], True)
    check(panel.shape is not None and "m²" in panel.out.text() and "101" not in panel.out.text(),
          "a click: " + panel.out.text()[:60])
    added = panel.accept_shape()
    check(added is not None and bld.featureCount() == 1 and bld.isEditable(), "Enter: in the layer's edits")
    bld.undoStack().undo()
    check(bld.featureCount() == 0, "and undone like any edit")
    panel.add_point(truth["red roof"][0], True)
    panel.undo_point()
    check(panel.shape is None and not panel.points, "Backspace: the click taken back")
    panel.close()
    check(canvas.mapTool() is not panel.tool, "closed: the click tool is off")

print()
print("FAILED: %d" % len(failures) if failures else "ALL PASSED")
app.exitQgis()
sys.exit(1 if failures else 0)
