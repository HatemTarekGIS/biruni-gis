"""Surveyor workflows: ground/grid, site calibration, field to finish, LandXML,
wrong-place check, imagery tiles for CAD and the volume report.

Run:  engine\\bin\\python-qgis.bat tests\\test_survey_office.py
"""
import json
import math
import os
import sys
import tempfile

from qgis.core import (QgsApplication, QgsCircularString, QgsCompoundCurve, QgsCoordinateReferenceSystem,
                       QgsCoordinateTransform, QgsFeature, QgsGeometry, QgsPointXY, QgsProcessingContext,
                       QgsProcessingFeedback, QgsProject, QgsRasterLayer, QgsVectorLayer)

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "app", "plugins"))

app = QgsApplication([], False)
app.initQgis()
sys.path.append(QgsApplication.prefixPath() + "/python/plugins")
import processing  # noqa: E402
from processing.core.Processing import Processing  # noqa: E402

Processing.initialize()
from biruni.algorithms import BiruniProvider  # noqa: E402
from biruni.algorithms import field_to_finish as ftf  # noqa: E402
from biruni.algorithms import ground_grid as gg  # noqa: E402
from biruni.algorithms import landxml as lx  # noqa: E402
from biruni.algorithms import ortho_tiles as ot  # noqa: E402

provider = BiruniProvider()
QgsApplication.processingRegistry().addProvider(provider)
failures = []
TMP = tempfile.mkdtemp(prefix="biruni_survey_office_")


def check(cond, msg):
    print(("PASS " if cond else "FAIL ") + msg)
    if not cond:
        failures.append(msg)


ctx = QgsProcessingContext()
ctx.setProject(QgsProject.instance())
fb = QgsProcessingFeedback()


def out(value):
    return ctx.getMapLayer(value) if isinstance(value, str) else value


def run(alg, params):
    return processing.run(alg, params, context=ctx, feedback=fb)


def layer(kind, crs, wkts, fields="field=name:string"):
    lyr = QgsVectorLayer(f"{kind}?crs={crs}&{fields}", "t", "memory")
    feats = []
    for i, w in enumerate(wkts):
        f = QgsFeature(lyr.fields())
        f.setGeometry(QgsGeometry.fromWkt(w))
        f.setAttributes([f"f{i}"])
        feats.append(f)
    lyr.dataProvider().addFeatures(feats)
    return lyr


def write(name, text, mode="w"):
    p = os.path.join(TMP, name)
    with open(p, mode, encoding=None if "b" in mode else "utf-8") as f:
        f.write(text)
    return p


ids = {a.id() for a in provider.algorithms()}
check({"biruni:groundgrid", "biruni:localization", "biruni:fieldtofinish", "biruni:landxmlimport",
       "biruni:landxmlexport", "biruni:orthotiles", "biruni:wrongplace"} <= ids, "tools registered")

# ---- 1. ground <-> grid ------------------------------------------------------------
E0, N0 = 670000.0, 2730000.0  # Riyadh, UTM 38N
site = layer("Polygon", "EPSG:32638", [f"POLYGON(({E0} {N0},{E0 + 200} {N0},{E0 + 200} {N0 + 100},{E0} {N0 + 100},"
                                       f"{E0} {N0}))", f"POLYGON(({E0 + 300} {N0},{E0 + 350} {N0},{E0 + 350} "
                                       f"{N0 + 50},{E0 + 300} {N0}))"])
res = run("biruni:groundgrid", {"INPUT": site, "DIRECTION": 0, "FACTOR_MODE": 0, "HEIGHT": 610, "GEOID": -5,
                                "ORIGIN_MODE": 1, "ROTATION": 0, "OUTPUT": "memory:"})
# Transverse Mercator: k = k0 (1 + x²/2R²) near the equator-ish; and the elevation factor R/(R+h)
x = E0 + 175 - 500000
k_expected = 0.9996 * (1 + x * x / (2 * 6371000.0 ** 2))
check(abs(res["SCALE_FACTOR"] - k_expected) < 3e-6, f"projection scale factor {res['SCALE_FACTOR']:.8f} "
      f"(≈ {k_expected:.8f})")
check(abs(res["ELEVATION_FACTOR"] - 6371000 / (6371000 + 605)) < 2e-6,
      f"elevation factor {res['ELEVATION_FACTOR']:.8f} for 605 m above the ellipsoid")
check(abs(res["FACTOR"] - res["SCALE_FACTOR"] * res["ELEVATION_FACTOR"]) < 1e-12, "combined = scale × elevation")
ground = out(res["OUTPUT"])
g_area = next(ground.getFeatures()).geometry().area()
check(abs(g_area - 20000 / res["FACTOR"] ** 2) < 1e-6, f"ground area = grid area / factor² ({g_area:.4f})")
f0 = next(ground.getFeatures())
check(abs(f0["gg_factor"] - res["FACTOR"]) < 1e-15 and f0["gg_direction"] == "grid>ground" and f0["name"] == "f0",
      "factor, direction and the original fields are kept")
back = run("biruni:groundgrid", {"INPUT": ground, "DIRECTION": 1, "FACTOR_MODE": 1, "FACTOR": res["FACTOR"],
                                 "ORIGIN_MODE": 2, "ORIGIN": f"{res['ORIGIN_X']},{res['ORIGIN_Y']} [EPSG:32638]",
                                 "ROTATION": 0, "OUTPUT": "memory:"})
diff = max(abs(a.x() - b.x()) + abs(a.y() - b.y()) for fa, fb_ in zip(site.getFeatures(), out(back["OUTPUT"])
                                                                         .getFeatures())
           for a, b in zip(fa.geometry().vertices(), fb_.geometry().vertices()))
check(diff < 1e-6, f"ground → grid with the same settings gives the grid back ({diff:.2e} m)")
rot = run("biruni:groundgrid", {"INPUT": site, "DIRECTION": 0, "FACTOR_MODE": 1, "FACTOR": 0.9998,
                                "ORIGIN_MODE": 0, "ROTATION": 1.5, "OUTPUT": "memory:"})
rback = run("biruni:groundgrid", {"INPUT": out(rot["OUTPUT"]), "DIRECTION": 1, "FACTOR_MODE": 1, "FACTOR": 0.9998,
                                  "ORIGIN_MODE": 0, "ROTATION": 1.5, "OUTPUT": "memory:"})
diff = max(abs(a.x() - b.x()) + abs(a.y() - b.y()) for fa, fb_ in zip(site.getFeatures(), out(rback["OUTPUT"])
                                                                         .getFeatures())
           for a, b in zip(fa.geometry().vertices(), fb_.geometry().vertices()))
check(diff < 1e-6, f"with a rotation about 0,0 too ({diff:.2e} m)")
geo = layer("Point", "EPSG:4326", ["POINT(46.7 24.6)"])
try:
    run("biruni:groundgrid", {"INPUT": geo, "DIRECTION": 0, "FACTOR_MODE": 0, "OUTPUT": "memory:"})
    check(False, "degrees are refused for the computed factor")
except Exception as e:  # noqa: BLE001
    check("projected" in str(e), "degrees are refused for the computed factor")

# ---- 2. site calibration -------------------------------------------------------------
true = gg.scale_rotate(1.00025, 32.0, 0, 0)
true.dx, true.dy = E0 - 5000, N0 - 5000
local = [(5000, 5000, 100.0), (5400, 5050, 101.2), (5380, 5600, 99.5), (4980, 5550, 100.4), (5200, 5300, 100.9)]
lines = ["Point,Local E,Local N,Local H,Grid E,Grid N,Grid H"]
for k, (e, n, hh) in enumerate(local, 1):
    E, N = true.apply(e, n)
    H = hh + 2.0 + 0.001 * (e - 5000) - 0.0005 * (n - 5000)  # an inclined vertical offset
    lines.append(f"CP{k},{e},{n},{hh},{E:.4f},{N:.4f},{H:.4f}")
ctl = write("control.csv", "\n".join(lines))
pairs = gg.parse_control(open(ctl, encoding="utf-8").read())
check(len(pairs) == 5 and pairs[0][0] == "CP1" and pairs[0][2][2] is not None, "control file read (names, heights)")
check(len(gg.parse_control("1 5000 5000 670000 2730000\n2,5100,5000,670100,2730000")) == 2,
      "numeric point numbers and mixed separators")
check(len(gg.parse_control("١ ٥٠٠٠ ٥٠٠٠ ٦٧٠٠٠٠ ٢٧٣٠٠٠٠")) == 1, "Arabic digits")
lin = layer("LineStringZ", "EPSG:32638", ["LINESTRING Z(5000 5000 100, 5300 5200 101, 5350 5500 99)"])
cal = os.path.join(TMP, "site.json")
res = run("biruni:localization", {"INPUT": lin, "CONTROL": ctl, "METHOD": 0, "VERTICAL": 2, "DIRECTION": 0,
                                  "CRS": "EPSG:32638", "CALIBRATION_OUT": cal, "OUTPUT": "memory:",
                                  "RESIDUALS": "memory:"})
check(abs(res["SCALE"] - 1.00025) < 1e-7 and abs(res["ROTATION"] - 32.0) < 1e-6 and res["RMSE"] < 1e-4,
      f"similarity found: scale {res['SCALE']:.9f}, rotation {res['ROTATION']:.6f}°, RMSE {res['RMSE']:.2e}")
v = list(next(out(res["OUTPUT"]).getFeatures()).geometry().vertices())
E, N = true.apply(5300, 5200)
check(abs(v[1].x() - E) < 1e-3 and abs(v[1].y() - N) < 1e-3, "line moved to the grid")
check(abs(v[1].z() - (101 + 2.0 + 0.001 * 300 - 0.0005 * 200)) < 1e-3, f"inclined height plane applied "
      f"(Z {v[1].z():.4f})")
rl = out(res["RESIDUALS"])
check(rl.featureCount() == 5 and max(f["error"] for f in rl.getFeatures()) < 1e-3, "residuals per control point")
saved = json.load(open(cal, encoding="utf-8"))
check(saved["format"] == "biruni-calibration" and len(saved["points"]) == 5 and saved["crs"] == "EPSG:32638",
      "calibration saved as a file")
inv = run("biruni:localization", {"INPUT": out(res["OUTPUT"]), "CALIBRATION_IN": cal, "DIRECTION": 1,
                                  "OUTPUT": "memory:"})
w = list(next(out(inv["OUTPUT"]).getFeatures()).geometry().vertices())
check(all(abs(a.x() - b.x()) < 1e-6 and abs(a.y() - b.y()) < 1e-6 and abs(a.z() - b.z()) < 1e-6
          for a, b in zip(w, lin.getFeature(1).geometry().vertices())),
      "the saved calibration, grid → local, gives the local line back (with heights)")
aff = run("biruni:localization", {"CONTROL_TEXT": "\n".join(lines[1:4]), "METHOD": 1, "OUTPUT": "memory:"})
check(aff["RMSE"] < 1e-6, "affine from three points")
try:
    run("biruni:localization", {"CONTROL_TEXT": lines[1], "METHOD": 0})
    check(False, "one control point is refused")
except Exception as e:  # noqa: BLE001
    check("at least" in str(e), "one control point is refused")
binary = write("trimble.cal", b"\x00\x01binary\x00", "wb")
try:
    run("biruni:localization", {"CONTROL": binary, "METHOD": 0})
    check(False, "a binary .cal file is explained")
except Exception as e:  # noqa: BLE001
    check("binary" in str(e), "a binary .cal file is explained")

# ---- 3. field to finish --------------------------------------------------------------
pts = [  # point, N, E, H, code
    (1, N0, E0, 610.0, "KERB1 B"), (2, N0, E0 + 20, 610.1, "KERB1"), (3, N0, E0 + 40, 610.2, "KERB1 PC"),
    (4, N0 + 5.857864, E0 + 54.142136, 610.2, "KERB1"), (5, N0 + 20, E0 + 60, 610.3, "KERB1 PT"),
    (6, N0 + 40, E0 + 60, 610.3, "KERB1 E"),
    (7, N0 - 10, E0, 609.9, "KERB2 B"), (8, N0 - 10, E0 + 50, 609.9, "KERB2"),
    (9, N0 + 50, E0 + 10, 611, "BLD B"), (10, N0 + 50, E0 + 30, 611, "BLD"), (11, N0 + 70, E0 + 30, 611, "BLD"),
    (12, N0 + 70, E0 + 10, 611, "BLD C"), (13, N0 + 80, E0 + 80, 611, "TREE Palm"), (14, N0 + 81, E0 + 81, 611,
                                                                                         "XYZ"),
    (15, N0 + 5, E0 + 5, 610, "KERB2"),  # KERB2 continues after other codes
]
csv_path = write("job.csv", "\n".join(f"{p},{n:.4f},{e:.4f},{h},{c}" for p, n, e, h, c in pts))
lib_path = write("codes.csv", "Code,Layer,Description,Type\nKERB,KERBS,Kerb line,line\nBLD,BUILDINGS,Building,"
                              "polygon\nTREE,TREES,Tree,point\n")
dxf = os.path.join(TMP, "job.dxf")
res = run("biruni:fieldtofinish", {"INPUT": csv_path, "FORMAT": 0, "CODES": lib_path, "CRS": "EPSG:32638",
                                   "POINTS": "memory:", "LINES": "memory:", "POLYGONS": "memory:", "DXF": dxf})
check(res["POINT_COUNT"] == 15 and res["ORDER"] == "PNEZD", f"15 points, order detected from the values "
      f"({res['ORDER']})")
pl = out(res["POINTS"])
p1 = next(pl.getFeatures())
check(abs(p1.geometry().constGet().x() - E0) < 1e-6 and abs(p1.geometry().constGet().y() - N0) < 1e-6
      and p1["layer"] == "KERBS", "points placed as easting X / northing Y with their layer")
ll = out(res["LINES"])
lines_by = {(f["code"], f["string"]): f for f in ll.getFeatures()}
check(res["LINE_COUNT"] == 2 and set(lines_by) == {("KERB", "1"), ("KERB", "2")}, "two kerb strings (KERB1, KERB2)")
k1 = lines_by[("KERB", "1")]
curve = k1.geometry().constGet()
has_arc = isinstance(curve, QgsCompoundCurve) and any(isinstance(curve.curveAt(i), QgsCircularString)
                                                      for i in range(curve.nCurves()))
check(has_arc and k1["points"] == 6, "PC … PT makes a true arc through three points")
arc_len = 20 + 20 + (math.pi / 2) * 20 + 20
check(abs(k1["length"] - arc_len) < 0.05, f"kerb length with the arc {k1['length']:.3f} (≈ {arc_len:.3f})")
check(lines_by[("KERB", "2")]["points"] == 3, "a string continues after other codes until it ends")
al = out(res["POLYGONS"])
bld = next(al.getFeatures())
check(res["AREA_COUNT"] == 1 and abs(bld["area"] - 400) < 1e-6 and bld["layer"] == "BUILDINGS",
      f"BLD … C closes a 400 m² building ({bld['area']})")
check(res["UNKNOWN_CODES"] == "XYZ", f"unknown codes listed ({res['UNKNOWN_CODES']})")
from osgeo import ogr  # noqa: E402

ds = ogr.Open(dxf)
dl = ds.GetLayer(0)
layers = {f.GetField("Layer") for f in dl}
check({"KERBS", "BUILDINGS", "TREES", "PNT_NUMBERS"} <= layers, f"DXF written with layers by code ({sorted(layers)})")
ds = None

penzd = write("penzd.txt", "PT E N Z CODE\n" + "\n".join(f"{p} {e:.3f} {n:.3f} {h} {c.split()[0]}"
                                                           for p, n, e, h, c in pts[:3]))
res = run("biruni:fieldtofinish", {"INPUT": penzd, "FORMAT": 0, "POINTS": "memory:", "LINES": "memory:",
                                   "POLYGONS": "memory:"})
p1 = next(out(res["POINTS"]).getFeatures())
check(res["ORDER"] == "PENZD" and abs(p1.geometry().constGet().x() - E0) < 1e-6, "PENZD with a header row")
jobxml = write("job.jxl", f"""<?xml version="1.0"?>
<JOBFile version="5.6" xmlns:x="urn:x"><FieldBook>
<PointRecord><Name>1</Name><Code>TREE</Code><Grid><North>{N0}</North><East>{E0}</East><Elevation>610</Elevation>
</Grid></PointRecord>
<PointRecord><Name>2</Name><Code>KERB B</Code><Grid><North>{N0 + 10}</North><East>{E0}</East>
<Elevation>610</Elevation></Grid></PointRecord>
<PointRecord><Name>3</Name><Code>KERB E</Code><Grid><North>{N0 + 20}</North><East>{E0}</East>
<Elevation>610</Elevation></Grid></PointRecord>
<PointRecord><Name>4</Name><Code>DEL</Code><Deleted>true</Deleted><Grid><North>1</North><East>1</East></Grid>
</PointRecord></FieldBook>
<Reductions><Point><Name>1</Name><Code>TREE</Code><Grid><North>{N0 + 0.005}</North><East>{E0}</East>
<Elevation>610.01</Elevation></Grid></Point></Reductions></JOBFile>""")
res = run("biruni:fieldtofinish", {"INPUT": jobxml, "FORMAT": 0, "POINTS": "memory:", "LINES": "memory:",
                                   "POLYGONS": "memory:"})
names = {f["point"]: f for f in out(res["POINTS"]).getFeatures()}
check(res["POINT_COUNT"] == 3 and res["LINE_COUNT"] == 1 and abs(names["1"]["northing"] - (N0 + 0.005)) < 1e-6,
      "JobXML: deleted points skipped, reduced coordinates win, B/E line without a library")
gsi = write("job.gsi", "*110001+0000000000000101 81..00+0000000670000123 82..00+0000002730000456 "
                       "83..00+0000000000610000 71....+000000000000TREE\n"
                       "*110002+0000000000000102 81..00+0000000670010000 82..00-0000002730000000 "
                       "83..00+0000000000611000 71....+00000000KERB1 B\n")
res = run("biruni:fieldtofinish", {"INPUT": gsi, "FORMAT": 0, "POINTS": "memory:", "LINES": "memory:",
                                   "POLYGONS": "memory:"})
names = {f["point"]: f for f in out(res["POINTS"]).getFeatures()}
check(res["ORDER"] == "GSI" and abs(names["101"]["easting"] - 670000.123) < 1e-6
      and abs(names["101"]["height"] - 610.0) < 1e-6 and names["101"]["code"] == "TREE",
      "Leica GSI-16: point number, mm coordinates, code")
check(abs(names["102"]["northing"] + 2730000.0) < 1e-6, "GSI sign")
gsi8 = write("job8.gsi", "110001+00000001 81..00+05000123 82..00+05100456 83..00+00100000\n")
res = run("biruni:fieldtofinish", {"INPUT": gsi8, "FORMAT": 4, "POINTS": "memory:", "LINES": "memory:",
                                   "POLYGONS": "memory:"})
p = next(out(res["POINTS"]).getFeatures())
check(abs(p["easting"] - 5000.123) < 1e-6 and abs(p["northing"] - 5100.456) < 1e-6, "Leica GSI-8")
rw5 = write("job.rw5", f"JB,NMjob,DT01-01-2026\nSP,PN100,N {N0:.4f},E {E0:.4f},EL610.0000,--KERB B\n"
                       f"GS,PN101,N {N0 + 30:.4f},E {E0:.4f},EL610.5000,--KERB E\nBK,OP1,BP2,BS0.0000\n")
res = run("biruni:fieldtofinish", {"INPUT": rw5, "FORMAT": 0, "POINTS": "memory:", "LINES": "memory:",
                                   "POLYGONS": "memory:"})
check(res["ORDER"] == "RW5" and res["POINT_COUNT"] == 2 and res["LINE_COUNT"] == 1, "Carlson RW5 (SP and GS)")
ar_lib = write("codes_ar.csv", "الكود,الطبقة,الوصف,النوع\nسور,الأسوار,سور,خط\n")
lib = ftf.read_code_library(ar_lib)
check(lib.get("سور", {}).get("kind") == "line" and lib["سور"]["layer"] == "الأسوار", "Arabic code library")

# ---- 4. LandXML ------------------------------------------------------------------------
xml_in = write("in.xml", f"""<?xml version="1.0" encoding="UTF-8"?>
<LandXML xmlns="http://www.landxml.org/schema/LandXML-1.2" version="1.2">
<Units><Metric linearUnit="meter" areaUnit="squareMeter" volumeUnit="cubicMeter"/></Units>
<CoordinateSystem epsgCode="32638"/>
<CgPoints><CgPoint name="1" code="CP">{N0} {E0} 610</CgPoint><CgPoint name="2">{N0} {E0 + 100} 612</CgPoint>
<CgPoint name="3">{N0 + 100} {E0 + 100} 614</CgPoint></CgPoints>
<Surfaces><Surface name="EG"><Definition surfType="TIN"><Pnts>
<P id="1">{N0} {E0} 600</P><P id="2">{N0} {E0 + 100} 610</P><P id="3">{N0 + 100} {E0 + 100} 620</P>
<P id="4">{N0 + 100} {E0} 610</P><P id="5">{N0 + 300} {E0 + 300} 0</P></Pnts>
<Faces><F>1 2 3</F><F>1 3 4</F><F i="1">3 4 5</F></Faces></Definition></Surface></Surfaces>
<Parcels><Parcel name="Lot 7" area="10314.159"><CoordGeom>
<Line><Start pntRef="1"/><End pntRef="2"/></Line>
<Curve rot="ccw"><Start>{N0} {E0 + 100}</Start><Center>{N0 + 50} {E0 + 100}</Center><End>{N0 + 100} {E0 + 100}</End>
</Curve>
<Line><Start>{N0 + 100} {E0 + 100}</Start><End>{N0 + 100} {E0}</End></Line>
<Line><Start>{N0 + 100} {E0}</Start><End>{N0} {E0}</End></Line></CoordGeom></Parcel></Parcels>
</LandXML>""")
dem = os.path.join(TMP, "eg.tif")
res = run("biruni:landxmlimport", {"INPUT": xml_in, "CELL": 1.0, "POINTS": "memory:", "TRIANGLES": "memory:",
                                   "PARCELS": "memory:", "DEM": dem})
check(res["POINT_COUNT"] == 3 and res["TRIANGLE_COUNT"] == 2 and res["HIDDEN_TRIANGLES"] == 1,
      "LandXML: 3 points, 2 triangles, the invisible one left out")
pl = out(res["POINTS"])
p1 = next(pl.getFeatures())
check(pl.crs().authid() == "EPSG:32638" and abs(p1.geometry().constGet().x() - E0) < 1e-6 and p1["code"] == "CP",
      "northing/easting order and the file's EPSG code")
parcel = next(out(res["PARCELS"]).getFeatures())
expect = 100 * 100 + math.pi * 50 * 50 / 2
check(abs(parcel["area"] - expect) / expect < 0.002 and parcel["name"] == "Lot 7",
      f"parcel with a curved side: {parcel['area']:.1f} m² (≈ {expect:.1f})")
rl = QgsRasterLayer(dem, "dem")
val, ok = rl.dataProvider().sample(QgsPointXY(E0 + 70.5, N0 + 20.5), 1)
check(ok and abs(val - (600 + 0.1 * 70.5 + 0.1 * 20.5)) < 0.02, f"surface raster interpolates the triangles "
      f"({val:.3f})")
outside, ok2 = rl.dataProvider().sample(QgsPointXY(E0 + 250, N0 + 250), 1)
check(not ok2 or outside != outside or outside < -9000, "no surface outside the triangles")

xml_out = os.path.join(TMP, "out.xml")
pts_l = layer("PointZ", "EPSG:32638", [f"POINT Z({E0 + i * 10} {N0 + (i % 3) * 7} {600 + i})" for i in range(12)])
res = run("biruni:landxmlexport", {"POINTS": pts_l, "NAME_FIELD": "name", "PARCELS": site, "PARCEL_NAME": "name",
                                   "SURFACE_POINTS": pts_l, "SURFACE_NAME": "Survey", "OUTPUT": xml_out})
data = lx.read_landxml(xml_out)
check(res["POINT_COUNT"] == 12 and len(data["points"]) == 12 and data["points"][0][0] == "f0"
      and abs(data["points"][0][4] - E0) < 1e-3, "points written (name, northing easting order)")
check(res["TRIANGLE_COUNT"] >= 10 and len(data["surfaces"][0]["faces"]) == res["TRIANGLE_COUNT"]
      and data["surfaces"][0]["hidden"] == 0, f"TIN from points: {res['TRIANGLE_COUNT']} triangles, none hidden")
check(len(data["parcels"]) == 2 and abs(sum(1 for _ in data["parcels"][0]["ring"]) - 5) <= 1
      and data["epsg"] == "32638", "parcels and the EPSG code written")
import xml.etree.ElementTree as ET  # noqa: E402

root = ET.parse(xml_out).getroot()
check(root.tag == "{http://www.landxml.org/schema/LandXML-1.2}LandXML" and root.get("version") == "1.2",
      "LandXML 1.2 namespace and version")
res = run("biruni:landxmlexport", {"SURFACE_RASTER": dem, "OUTPUT": os.path.join(TMP, "dem.xml")})
d2 = lx.read_landxml(os.path.join(TMP, "dem.xml"))
check(res["TRIANGLE_COUNT"] > 1000 and all(len(set(f)) == 3 for f in d2["surfaces"][0]["faces"]),
      f"TIN from a raster: {res['TRIANGLE_COUNT']} triangles, nodata cells left out")
check(lx.check_landxml(xml_out)[0], "the written file passes its check")

# ---- 5. why is it in the wrong place? ---------------------------------------------------
riyadh = "46.712,24.690 [EPSG:4326]"
xf = QgsCoordinateTransform(QgsCoordinateReferenceSystem("EPSG:4326"), QgsCoordinateReferenceSystem("EPSG:32638"),
                            QgsProject.instance())
c = xf.transform(QgsPointXY(46.712, 24.690))
mm = layer("LineString", "EPSG:32638", [f"LINESTRING({c.x() * 1000} {c.y() * 1000}, {(c.x() + 50) * 1000} "
                                        f"{(c.y() + 30) * 1000})"])
res = run("biruni:wrongplace", {"INPUT": mm, "LOCATION": riyadh, "OUTPUT": "memory:", "CORRECTED": "memory:"})
check(res["FOUND"] and res["UNIT"] == "millimetres" and abs(res["SCALE"] - 0.001) < 1e-12
      and res["CRS"] == "EPSG:32638" and not res["SWAPPED"], f"a drawing in millimetres: {res['SUMMARY']}")
fixed = next(out(res["CORRECTED"]).getFeatures()).geometry().vertexAt(0)
check(abs(fixed.x() - c.x()) < 1e-6 and abs(fixed.y() - c.y()) < 1e-6, "corrected copy in metres")
ft = layer("Point", "EPSG:32638", [f"POINT({c.y() / 0.3048} {c.x() / 0.3048})"])
res = run("biruni:wrongplace", {"INPUT": ft, "LOCATION": riyadh, "OUTPUT": "memory:", "CORRECTED": "memory:"})
check(res["FOUND"] and res["UNIT"] == "international feet" and res["SWAPPED"],
      f"feet and swapped axes at once: {res['SUMMARY']}")
ok_l = layer("Point", "EPSG:32638", [f"POINT({c.x()} {c.y()})"])
res = run("biruni:wrongplace", {"INPUT": ok_l, "LOCATION": riyadh, "OUTPUT": "memory:"})
check(res["FOUND"] and res["UNIT"] == "metres" and res["DISTANCE_KM"] < 0.01, f"already right: {res['SUMMARY']}")
loc = layer("Point", "EPSG:32638", ["POINT(5000 5000)", "POINT(5100 5050)"])
res = run("biruni:wrongplace", {"INPUT": loc, "LOCATION": riyadh, "OUTPUT": "memory:", "CORRECTED": "memory:"})
check(not res["FOUND"] and "local coordinates" in res["SUMMARY"], "local coordinates are recognised as such")
try:
    run("biruni:wrongplace", {"INPUT": loc, "OUTPUT": "memory:"})
    check(False, "a location is required")
except Exception as e:  # noqa: BLE001
    check("Click" in str(e), "a location is required")

# ---- 6. imagery tiles for CAD ------------------------------------------------------------
import numpy as np  # noqa: E402
from osgeo import gdal  # noqa: E402

img = os.path.join(TMP, "ortho.tif")
drv = gdal.GetDriverByName("GTiff")
ds = drv.Create(img, 3000, 2000, 3, gdal.GDT_Byte)
ds.SetGeoTransform((E0, 0.05, 0, N0 + 100, 0, -0.05))
ds.SetProjection(QgsCoordinateReferenceSystem("EPSG:32638").toWkt())
for b in range(3):
    ds.GetRasterBand(b + 1).WriteArray((np.indices((2000, 3000))[b % 2] % 256).astype("uint8"))
ds = None
folder = os.path.join(TMP, "tiles")
res = run("biruni:orthotiles", {"INPUT": img, "FORMAT": 0, "MAX_SIDE": 1024, "DOWNSAMPLE": 1, "QUALITY": 80,
                                "OUTPUT": folder, "INDEX": "memory:"})
check(res["TILE_COUNT"] == 6 and res["COLUMNS"] == 3 and res["ROWS"] == 2, "3 × 2 tiles for 3000 × 2000 at 1024")
jgw = open(os.path.join(folder, "ortho_r01_c02.jgw"), encoding="ascii").read().split()
check(abs(float(jgw[0]) - 0.05) < 1e-12 and abs(float(jgw[3]) + 0.05) < 1e-12
      and abs(float(jgw[4]) - (E0 + 1000 * 0.05 + 0.025)) < 1e-9 and abs(float(jgw[5]) - (N0 + 100 - 0.025)) < 1e-9,
      "world file of the second tile: pixel size and top-left pixel centre")
check(os.path.exists(os.path.join(folder, "ortho_r02_c03.jpg")) and os.path.exists(
    os.path.join(folder, "ortho_r02_c03.prj")), "JPEG tiles with .prj")
tile = gdal.Open(os.path.join(folder, "ortho_r02_c03.jpg"))
check(tile.RasterXSize == 1000 and tile.RasterYSize == 1000 and tile.RasterCount == 3, "tile size and bands")
tile = None
idx = out(res["INDEX"])
check(idx.featureCount() == 6 and abs(next(idx.getFeatures()).geometry().area() - 50 * 50) < 1e-6,
      "tile index layer")
check(os.path.exists(os.path.join(folder, "ortho_index.csv")) and ogr.Open(
    os.path.join(folder, "ortho_index.dxf")) is not None, "index as CSV and DXF")
dem16 = os.path.join(TMP, "dem16.tif")
ds = drv.Create(dem16, 600, 400, 1, gdal.GDT_UInt16)
ds.SetGeoTransform((E0, 1, 0, N0, 0, -1))
ds.GetRasterBand(1).WriteArray((np.indices((400, 600))[1] * 50).astype("uint16"))
ds = None
res = run("biruni:orthotiles", {"INPUT": dem16, "FORMAT": 1, "MAX_SIDE": 4000, "DOWNSAMPLE": 2,
                                "OUTPUT": os.path.join(TMP, "tiles16")})
t16 = gdal.Open(os.path.join(TMP, "tiles16", "dem16_r01_c01.png"))
arr = t16.GetRasterBand(1).ReadAsArray()
check(res["TILE_COUNT"] == 1 and t16.RasterXSize == 300 and arr.max() > 200 and arr.min() < 10,
      "16-bit image stretched to 8-bit PNG, half resolution")
t16 = None
check(ot.tile_grid(70000, 1000, 100000, 3)[0] >= 2, "JPEG width limit respected")

# ---- 7. volume report --------------------------------------------------------------------
surf = os.path.join(TMP, "pile.tif")
ds = drv.Create(surf, 120, 120, 1, gdal.GDT_Float32)
ds.SetGeoTransform((E0, 0.5, 0, N0 + 60, 0, -0.5))
ds.SetProjection(QgsCoordinateReferenceSystem("EPSG:32638").toWkt())
yy, xx = np.indices((120, 120))
cx, cy = (xx + 0.5) * 0.5, (yy + 0.5) * 0.5
r = np.hypot(cx - 30, cy - 30)
ds.GetRasterBand(1).WriteArray((50 + np.clip(6 * (1 - r / 20), 0, None)).astype("float32"))
ds = None
area_l = layer("Polygon", "EPSG:32638", [QgsGeometry.fromPointXY(QgsPointXY(E0 + 30, N0 + 30)).buffer(22, 64)
                                         .asWkt()])
xlsx = os.path.join(TMP, "volumes.xlsx")
res = run("biruni:lasvolume", {"INPUT": surf, "POLYGONS": area_l, "BASE": 2, "LEVEL": 50.0, "SIGMA_Z": 0.05,
                               "OUTPUT": "memory:", "REPORT": xlsx})
cone_v = math.pi * 20 ** 2 * 6 / 3
check(abs(res["ABOVE_M3"] - cone_v) / cone_v < 0.03, f"cone volume {res['ABOVE_M3']:.1f} m³")
fv = next(out(res["OUTPUT"]).getFeatures())
check(fv["uncert_m3"] >= 0.05 * fv["area_m2"] and abs(fv["uncert95_m3"] - 1.96 * fv["uncert_m3"]) < 0.01,
      f"uncertainty ±{fv['uncert_m3']} m³ (≥ σz × area), 95% {fv['uncert95_m3']} m³")
import openpyxl  # noqa: E402

wb = openpyxl.load_workbook(xlsx)
ws = wb.active
cells = [c.value for row in ws.iter_rows() for c in row if c.value is not None]
check(any(isinstance(v, str) and v.startswith("=SUM(D") for v in cells) and "f0" in cells,
      "Excel report with the area names and totals")
pdf = os.path.join(TMP, "volumes.pdf")
res = run("biruni:lasvolume", {"INPUT": surf, "POLYGONS": area_l, "BASE": 0, "OUTPUT": "memory:", "REPORT": pdf})
check(os.path.exists(pdf) and open(pdf, "rb").read(5) == b"%PDF-" and os.path.getsize(pdf) > 1000, "PDF report")

print("ALL PASS" if not failures else f"{len(failures)} failure(s)")
sys.exit(1 if failures else 0)
