"""AutoCAD DWG through LibreDWG (engine\\tools\\libredwg): a DXF written as an AutoCAD 2000 DWG and read back, with
its arcs and its Arabic texts (in Unicode and in the xarb SHX font); CAD to GIS opening a DWG directly; the
layers of the map saved as a DWG.

Run:  engine\\bin\\python-qgis.bat tests\\test_dwg.py
"""
import math
import os
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "app", "plugins"))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from qgis.core import (QgsApplication, QgsFeature, QgsGeometry, QgsProcessingContext,  # noqa: E402
                       QgsProcessingFeedback, QgsProject, QgsVectorLayer)

os.environ["QGIS_CUSTOM_CONFIG_PATH"] = tempfile.mkdtemp()
app = QgsApplication([], True)
app.initQgis()
sys.path.append(QgsApplication.prefixPath() + "/python/plugins")
import processing  # noqa: E402
from processing.core.Processing import Processing  # noqa: E402

Processing.initialize()
from biruni.algorithms import BiruniProvider  # noqa: E402
from biruni.dwg import core as dwg  # noqa: E402
from biruni.survey import dxf  # noqa: E402

failures = []


def check(cond, msg):
    print(("PASS " if cond else "FAIL ") + msg, flush=True)
    if not cond:
        failures.append(msg)


if not dwg.available():
    print("SKIP LibreDWG is not in engine\\tools\\libredwg (scripts\\add_tools.py)")
    sys.exit(0)
provider = BiruniProvider()
QgsApplication.processingRegistry().addProvider(provider)
TMP = tempfile.mkdtemp(prefix="biruni_dwg_test_")


def pairs_text(*pairs_):
    return "".join(f"{c}\n{v}\n" for c, v in pairs_)


# the drawing as the program's own DXF export writes it (complete tables, no entity owners), with a true arc,
# then two texts put in it: one in Unicode Arabic, one in the Arabic keyboard SHX font (xarb) and its style
from biruni.dwg import ui as dwg_ui  # noqa: E402

parcels = QgsVectorLayer("CurvePolygon?crs=EPSG:32637&field=name:string", "parcels", "memory")
feats = []
for wkt in ("CURVEPOLYGON(COMPOUNDCURVE(CIRCULARSTRING(500040 2700000,500045 2699995,500050 2700000),"
            "(500050 2700000,500050 2700010,500040 2700010,500040 2700000)))",
            "POLYGON((500000 2700000,500010 2700000,500010 2700010,500000 2700010,500000 2700000))"):
    f = QgsFeature(parcels.fields())
    f.setGeometry(QgsGeometry.fromWkt(wkt))
    feats.append(f)
parcels.dataProvider().addFeatures(feats)
base = dwg_ui.export_layers_dxf([parcels], os.path.join(TMP, "export.dxf"))
body = open(base, "rb").read().decode("cp1256")
nl = "\r\n" if "\r\n" in body else "\n"


def text(handle, x, y, value, style="STANDARD"):
    return pairs_text((0, "TEXT"), (5, handle), (100, "AcDbEntity"), (8, "parcels"), (100, "AcDbText"), (10, x),
                      (20, y), (30, 0), (40, 1.0), (1, value), (7, style), (100, "AcDbText")).replace("\n", nl)


ents = body.index("ENTITIES")
end = body.index("ENDSEC", ents)
cut = body.rindex(nl + "  0" + nl, 0, end) + len(nl)
body = body[:cut] + text("FFF01", 500005, 2700005, "قطعة 7") + text("FFF02", 500045, 2700005, "pl] uvH[", "ARAB") \
    + body[cut:]
st = body.index("TABLE" + nl + "  2" + nl + "STYLE" + nl)     # the STYLE table: its handle owns the new style
st_handle = body[st:].split(nl)[4].strip()
endtab = body.index("ENDTAB", st)
cut = body.rindex(nl + "  0" + nl, 0, endtab) + len(nl)
style = pairs_text((0, "STYLE"), (5, "FFF10"), (330, st_handle), (100, "AcDbSymbolTableRecord"),
                   (100, "AcDbTextStyleTableRecord"), (2, "ARAB"), (70, 0), (40, 0.0), (41, 1.0), (50, 0.0),
                   (71, 0), (42, 2.5), (3, "xarb.shx"), (4, "")).replace("\n", nl)
body = body[:cut] + style + body[cut:]
src = os.path.join(TMP, "drawing.dxf")
with open(src, "wb") as fh:
    fh.write(body.encode("cp1256"))


def summary(path):
    d = dxf.read(path)
    kinds = sorted(e.dxf_type for e in d.entities)
    texts = sorted(e.text for e in d.entities if e.text)
    bulges = [round(v[2], 6) for e in d.entities for v in e.verts if len(v) > 2 and v[2]]
    return d, kinds, texts, bulges


d0, k0, t0, b0 = summary(src)
check(t0 == ["حمد عراج", "قطعة 7"] and b0, f"the drawing as written: {t0}, an arc {b0}")

# ---- DXF -> DWG (AutoCAD 2000) -> DXF ------------------------------------------------------------------------
out = os.path.join(TMP, "drawing.dwg")
t = time.time()
res = dwg.to_dwg(src, out)
check(dwg.version_of(out) == "AC1015" and res["codepage"] == "ANSI_1256" and res["escaped"] == 0,
      f"written as an AutoCAD 2000 DWG in {time.time() - t:.2f} s, Arabic code page ({res})")
back = dwg.to_dxf(out, os.path.join(TMP, "back.dxf"))
d1, k1, t1, b1 = summary(back)
check(k1 == k0, f"the same entities back ({k1})")
check(t1 == t0, f"the same texts back, Arabic and xarb included ({t1})")
check(b1 == b0 and b1, f"the arc kept as a bulge ({b1})")
p0 = sorted((round(x, 6), round(y, 6)) for e in d0.entities for x, y, *_ in e.verts)
p1 = sorted((round(x, 6), round(y, 6)) for e in d1.entities for x, y, *_ in e.verts)
check(p0 == p1, "every vertex in its place")

# ---- a UTF-8 DXF (AutoCAD 2018) written as a DWG: its texts put in the Arabic code page ---------------------------
u8 = os.path.join(TMP, "utf8.dxf")
with open(u8, "wb") as fh:
    fh.write(body.replace("AC1018", "AC1032", 1).encode("utf-8"))
ver, _cp = dwg.te._dxf_header(open(u8, "rb").read())
check(ver == "AC1032" and dxf.read(u8).encoding == "utf-8", f"the same drawing as an AutoCAD 2018 DXF, UTF-8 ({ver})")
out2 = os.path.join(TMP, "from2018.dwg")
res2 = dwg.to_dwg(u8, out2)
_d2, k2, t2, _b2 = summary(dwg.to_dxf(out2, os.path.join(TMP, "back2.dxf")))
check(t2 == t0 and k2 == k0, f"…and back from the DWG with its Arabic texts ({res2})")

# a character no code page holds (a Chinese one beside Arabic): written as \U+, the rest kept
mixed = body.replace("قطعة 7", "قطعة 7 中").encode("cp1256", "replace").replace(" ?".encode(), " \\U+4E2D".encode())
mx = os.path.join(TMP, "mixed.dxf")
open(mx, "wb").write(mixed)
res3 = dwg.to_dwg(mx, os.path.join(TMP, "mixed.dwg"))
_d3, _k3, t3, _b3 = summary(dwg.to_dxf(os.path.join(TMP, "mixed.dwg"), os.path.join(TMP, "back3.dxf")))
check(res3["escaped"] == 1 and any(x.startswith("قطعة 7") and ("中" in x or "4E2D" in x) for x in t3),
      f"a character with no code page: written as \\U+ ({res3['escaped']}), the Arabic kept")

# ---- CAD to GIS opens the DWG directly --------------------------------------------------------------------------
ctx = QgsProcessingContext()
ctx.setProject(QgsProject.instance())
fb = QgsProcessingFeedback()
gpkg = os.path.join(TMP, "from_dwg.gpkg")
r = processing.run("biruni:cadtogis", {"INPUT": out, "CRS": "EPSG:32636", "SPLIT": 0, "LABELS": True,
                                       "KEEP_CURVES": True, "OUTPUT": gpkg}, context=ctx, feedback=fb)
polys = list(QgsVectorLayer(f"{gpkg}|layername=polygons", "p", "ogr").getFeatures())
texts = [f["text"] for f in QgsVectorLayer(f"{gpkg}|layername=texts", "t", "ogr").getFeatures()] \
    if "texts" in r.get("LAYERS", []) else []
areas = sorted(round(f.geometry().area(), 4) for f in polys)
check(len(polys) >= 2 and any(abs(a - (100 + math.pi * 12.5)) < 1e-3 for a in areas),
      f"CAD to GIS on the DWG: the parcels, the arc's exact area ({areas})")
check(any(f.geometry().constGet().hasCurvedSegments() for f in polys), "…the arc a true curve")
labels = [f["label"] for f in polys if f.fields().indexOf("label") >= 0]
check("قطعة 7" in texts or "قطعة 7" in labels or any("قطعة" in (x or "") for x in texts + labels),
      f"…and the Arabic text ({[x for x in texts + labels if x][:4]})")
fake = os.path.join(TMP, "fake.dwg")
open(fake, "wb").write(b"not a drawing")
try:
    processing.run("biruni:cadtogis", {"INPUT": fake, "CRS": "EPSG:32636", "OUTPUT": os.path.join(TMP, "y.gpkg")},
                   context=ctx, feedback=fb)
    check(False, "a file that is not a DWG was read")
except Exception as e:  # noqa: BLE001
    check("not a DWG" in str(e), "a .dwg that is not one: said so")

# ---- the buttons ----------------------------------------------------------------------------------------------
from qgis.PyQt.QtWidgets import QWidget  # noqa: E402

from biruni.dwg import ui  # noqa: E402


class Bar:
    def __init__(self):
        self.said = []

    def pushMessage(self, title, text, *a, **k):
        self.said.append(text)


class Tree:
    def __init__(self, layers):
        self.layers = layers

    def selectedLayers(self):  # noqa: N802
        return self.layers


class Iface:
    def __init__(self):
        self.w, self.bar, self.tree = QWidget(), Bar(), Tree([])

    def mainWindow(self):  # noqa: N802
        return self.w

    def messageBar(self):  # noqa: N802
        return self.bar

    def layerTreeView(self):  # noqa: N802
        return self.tree


class Plugin:
    iface = Iface()


plug = Plugin()
seen = []
ui.open_ui(plug, out, run=lambda alg, params: seen.append((alg, params)))
check(seen == [("biruni:cadtogis", {"INPUT": out})], "Open DWG: CAD to GIS opened on the drawing")
lyr = QgsVectorLayer("Polygon?crs=EPSG:32637&field=name:string", "قطع", "memory")
f = QgsFeature(lyr.fields())
f.setAttributes(["قطعة 9"])
f.setGeometry(QgsGeometry.fromWkt("POLYGON((500000 2700000,500020 2700000,500020 2700020,500000 2700020,500000 2700000))"))
lyr.dataProvider().addFeatures([f])
QgsProject.instance().addMapLayer(lyr)
plug.iface.tree.layers = [lyr]
saved = ui.save_ui(plug, out=os.path.join(TMP, "layers.dwg"))
check(saved and dwg.version_of(saved) == "AC1015" and "AutoCAD 2000" in plug.iface.bar.said[-1],
      "Save as DWG: the selected layer written as an AutoCAD 2000 drawing")
dd = dxf.read(dwg.to_dxf(saved, os.path.join(TMP, "layers_back.dxf")))
pts = [(x, y) for e in dd.entities for x, y, *_ in e.verts]
check(any(abs(x - 500020) < 1e-6 and abs(y - 2700020) < 1e-6 for x, y in pts),
      f"…its parcel in the DWG, at its coordinates ({len(dd.entities)} entities)")

print()
print("FAILED: %d" % len(failures) if failures else "ALL PASSED")
app.exitQgis()
sys.exit(1 if failures else 0)
