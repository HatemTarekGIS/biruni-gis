"""Regional coordinate systems catalogue, the official shift-grid manager and the
regional side of "What coordinate system is this?".

Run:  engine\\bin\\python-qgis.bat tests\\test_regional_crs.py
"""
import math
import os
import sqlite3
import sys
import tempfile
import time

from qgis.core import (QgsApplication, QgsCoordinateReferenceSystem, QgsCoordinateTransform,
                       QgsCoordinateTransformContext, QgsFeature, QgsGeometry, QgsPointXY, QgsProcessingContext,
                       QgsProcessingFeedback, QgsProject, QgsVectorLayer)

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "app", "plugins"))

app = QgsApplication([], False)
app.initQgis()
sys.path.append(QgsApplication.prefixPath() + "/python/plugins")
import processing  # noqa: E402
from processing.core.Processing import Processing  # noqa: E402

Processing.initialize()
from biruni import proj_grids, regional_crs as rc  # noqa: E402
from biruni.algorithms import BiruniProvider  # noqa: E402
from biruni.algorithms import guess_crs as gc  # noqa: E402

provider = BiruniProvider()
QgsApplication.processingRegistry().addProvider(provider)
failures = []
WGS84 = QgsCoordinateReferenceSystem("EPSG:4326")
CTX = QgsCoordinateTransformContext()


def check(cond, msg):
    print(("PASS " if cond else "FAIL ") + msg)
    if not cond:
        failures.append(msg)


def code_of(name):
    """EPSG authid of a system by its exact PROJ-database name."""
    found = rc.pattern_codes(name)
    return next(iter(found), None)


# ---- catalogue integrity --------------------------------------------------------------------
db = rc.proj_db()
con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
bad_codes, bad_patterns = [], []
for e in rc.CATALOG:
    for c in e["codes"]:
        n = con.execute("SELECT count(*) FROM projected_crs WHERE auth_name='EPSG' AND code=? AND deprecated=0",
                        (c.split(":")[1],)).fetchone()[0]
        if not n or not QgsCoordinateReferenceSystem(c).isValid():
            bad_codes.append((e["id"], c))
    for p in e["patterns"]:
        if not rc.pattern_codes(p):
            bad_patterns.append((e["id"], p))
check(not bad_codes, f"every catalogue code exists and is current in this PROJ database {bad_codes}")
check(not bad_patterns, f"every catalogue pattern finds systems {bad_patterns}")
grid_names = {r[0] for r in con.execute("SELECT proj_grid_name FROM grid_alternatives")}
bad_grids = [(e["id"], g) for e in rc.CATALOG for g in e["grids"] if g not in grid_names]
check(not bad_grids, f"every grid named in the catalogue is known to PROJ {bad_grids}")
con.close()
rc._ensure_cape()
bad_reg = [k for k, s in rc._REG.items() if not rc.is_valid_key(k) or
           (rc.resolve(k).func is None and (rc.resolve(k).crs is None or not rc.resolve(k).crs.isValid()))]
check(not bad_reg and len(rc._REG) > 40, f"every regional key resolves ({len(rc._REG)} keys) {bad_reg}")
check(len(rc.countries()) >= 40, f"catalogue covers {len(rc.countries())} countries")
need = {"RU", "KZ", "UA", "VN", "KR", "PH", "TH", "ID", "IN", "PK", "BD", "IL", "IR", "IQ", "SA", "EG", "MA", "DZ",
        "TN", "KE", "NG", "GH", "ZA", "ET", "SN", "CI", "CM", "BR", "PE", "CO", "AR", "ES", "FR", "DE", "AT", "CH",
        "IT", "NL", "PL", "FI", "SE", "NO", "JP", "CN", "TR"}
check(need <= set(rc.countries()), f"all requested countries present (missing {sorted(need - set(rc.countries()))})")
vn = rc.for_country("vn")
check(vn and all(("authid" in x or "proj" in x) and x["label"] for x in vn), f"for_country contract ({len(vn)} entries)")
check(any(x["id"].startswith("REG:VN2000") for x in vn), "VN province systems listed")
desc = rc.describe(rc.for_country("IT")[0])
check("Istituto Geografico Militare" in desc and "EPSG:3003" in desc, "describe: systems and licence note")
check("swapped" in rc.axis_note("EPSG:6677"), "axis note for Japanese plane systems")
check(rc.msk_crs(37.5, 1250000, -5000000).isValid(), "MSK from user parameters")

# VN-2000 home-made definition agrees with EPSG (rotation signs right)
p = (105.6, 21.0)
own = rc.resolve("REG:VN2000-TM3-105-30").crs
epsg = QgsCoordinateReferenceSystem(code_of("VN-2000 / TM-3 105-30"))
a = QgsCoordinateTransform(WGS84, own, CTX).transform(QgsPointXY(*p))
b = QgsCoordinateTransform(WGS84, epsg, CTX).transform(QgsPointXY(*p))
check(math.hypot(a.x() - b.x(), a.y() - b.y()) < 1.0, f"VN-2000 PROJ string matches EPSG ({math.hypot(a.x() - b.x(), a.y() - b.y()):.2f} m)")

# ---- GCJ-02 / BD-09 -------------------------------------------------------------------------
worst = 0.0
for lon, lat in ((116.397, 39.909), (121.47, 31.23), (113.26, 23.13), (104.07, 30.67), (87.6, 43.8)):
    g = rc.wgs84_to_gcj02(lon, lat)
    back = rc.gcj02_to_wgs84(*g)
    worst = max(worst, gc.haversine_km(lon, lat, *back) * 1000)
    bd = rc.wgs84_to_bd09(lon, lat)
    worst = max(worst, gc.haversine_km(lon, lat, *rc.bd09_to_wgs84(*bd)) * 1000)
shift = gc.haversine_km(116.397, 39.909, *rc.wgs84_to_gcj02(116.397, 39.909)) * 1000
check(worst < 1.0 and 100 < shift < 900, f"GCJ-02/BD-09 round trip {worst:.3f} m, offset in Beijing {shift:.0f} m")
check(rc.wgs84_to_gcj02(46.7, 24.7) == (46.7, 24.7), "outside China nothing is shifted")


# ---- detection by place -----------------------------------------------------------------------
def sample(crs_key, lon, lat, n=6):
    """Raw coordinates of points near a place, as stored in a system (or a regional key)."""
    spec = rc.resolve(crs_key)
    pts = []
    for i in range(n):
        lo, la = lon + 0.004 * (i % 3), lat + 0.003 * (i // 3)
        if spec.func is not None:
            x, y = (rc.wgs84_to_gcj02 if "GCJ" in crs_key else rc.wgs84_to_bd09)(lo, la)
        else:
            q = QgsCoordinateTransform(WGS84, spec.crs, CTX).transform(QgsPointXY(lo, la))
            x, y = q.x() / (spec.sx * spec.factor), q.y() / (spec.sy * spec.factor)
        pts.append((x, y))
    return pts


def found(key, lon, lat, swap=False, points=None, top=3):
    pts = points or sample(key, lon, lat)
    if swap:
        pts = [(y, x) for x, y in pts]
    res = gc.rank(gc.evaluate(pts, gc.candidate_codes((lon, lat), pts), CTX, location=(lon, lat)))
    if not res:
        return False, "no result"
    best = res[0]
    mine = [r for r in res if r["crs"] == key and r["swapped"] == swap]
    ok = bool(mine) and (res.index(mine[0]) < top or
                         (mine[0]["distance_km"] is not None and mine[0]["distance_km"] <= best["distance_km"] + 0.002))
    return ok, f"{best['crs']}{' swapped' if best['swapped'] else ''} first; target at " \
               f"{res.index(mine[0]) + 1 if mine else '-'} of {len(res)}"


CASES = [
    ("SA Ain el Abd 38N", "EPSG:20438", 46.7, 24.7),
    ("EG Red Belt", "EPSG:22992", 31.24, 30.05),
    ("IQ Karbala 1979", code_of("Karbala 1979 / UTM zone 38N"), 44.4, 33.3),
    ("IL ITM", "EPSG:2039", 35.2, 31.8),
    ("MA Nord Maroc", code_of("Merchich / Nord Maroc"), -6.84, 34.0),
    ("TN Carthage UTM 32N", code_of("Carthage / UTM zone 32N"), 10.18, 36.8),
    ("KE Arc 1960 37S", "EPSG:21037", 36.82, -1.29),
    ("NG Minna Mid Belt", "EPSG:26392", 7.49, 9.06),
    ("GH Ghana National Grid (feet)", "EPSG:2136", -0.19, 5.6),
    ("ET Adindan 37N", code_of("Adindan / UTM zone 37N"), 38.75, 9.0),
    ("PK Kalianpur 1962 43N", "EPSG:24313", 74.3, 31.55),
    ("BD BTM", "EPSG:9678", 90.4, 23.8),
    ("TH Indian 1975 47N", "EPSG:24047", 100.5, 13.75),
    ("VN VN-2000 Hanoi 105°00'", "REG:VN2000-TM3-105-00", 105.85, 21.03),
    ("KR 5174 KLIS", "EPSG:5174", 127.0, 37.55),
    ("PH PRS92 zone 3", "EPSG:3123", 121.0, 14.6),
    ("ID DGN95 TM-3 48.2", code_of("DGN95 / Indonesia TM-3 zone 48.2"), 106.8, -6.2),
    ("CN CGCS2000 3° zone 39", "EPSG:4527", 116.4, 39.9),
    ("CN GCJ-02 web map", "REG:GCJ02", 116.4, 39.9),
    ("RU SK-42 zone 7", "EPSG:28407", 37.6, 55.75),
    ("UA UCS-2000 zone 6", "EPSG:5564", 30.5, 50.45),
    ("TR TUREF TM30", "EPSG:5254", 29.0, 41.0),
    ("ES ED50 30N", "EPSG:23030", -3.7, 40.4),
    ("FR Lambert-93", "EPSG:2154", 2.35, 48.85),
    ("DE Gauss-Krüger 3", "EPSG:31467", 8.68, 50.11),
    ("CH LV95", "EPSG:2056", 7.45, 46.95),
    ("IT Gauss-Boaga 1", "EPSG:3003", 12.5, 41.9),
    ("NL RD New", "EPSG:28992", 4.9, 52.37),
    ("FI TM35FIN", "EPSG:3067", 24.94, 60.17),
    ("SE SWEREF99 TM", "EPSG:3006", 18.07, 59.33),
    ("NO NTM 10", code_of("ETRS89-NOR [EUREF89] / NTM zone 10"), 10.75, 59.9),
    ("BR SAD69 23S", "EPSG:29193", -46.63, -23.55),
    ("PE PSAD56 18S", "EPSG:24878", -77.03, -12.05),
    ("CO CTM12", "EPSG:9377", -74.07, 4.71),
    ("AR POSGAR 2007 5", "EPSG:5347", -58.4, -34.6),
    ("IN Kalianpur 1975 43N", code_of("Kalianpur 1975 / UTM zone 43N"), 77.2, 28.6),
    ("IR UTM 39N", "EPSG:32639", 51.4, 35.7),
]
t0 = time.time()
for label, key, lon, lat in CASES:
    if key is None:
        check(False, f"{label}: system not in this PROJ database")
        continue
    ok, why = found(key, lon, lat)
    check(ok, f"by place - {label} ({key}): {why}")
check(time.time() - t0 < 240, f"{len(CASES)} places checked in {time.time() - t0:.0f} s")

# ---- axis order, signs, units ------------------------------------------------------------------
ok, why = found("EPSG:6677", 139.7, 35.68, swap=True)
check(ok, f"Japan: survey order X = north (swapped) found: {why}")
ok, why = found("EPSG:2180", 21.0, 52.23, swap=True)
check(ok, f"Poland PL-1992 written x = north: {why}")
ok, why = found("REG:ZA-Lo29-xy", 28.0, -26.2)
check(ok, f"South Africa Lo29 stored as -Y / -X: {why}")
cape = next(k for k in rc._REG if k.startswith("REG:CapeLo29") and k.endswith("capeft"))
ok, why = found(cape, 28.0, -26.2)
check(ok, f"South Africa Cape datum in Cape feet: {why}")

# ---- hints from the numbers (no location) -------------------------------------------------------
pts = sample("EPSG:28407", 37.6, 55.75)
hints = rc.candidates_for_values(pts)
check("EPSG:28407" in hints and code_of("GSK-2011 / Gauss-Kruger zone 7") in hints,
      f"zone 7 read from the easting prefix ({len(hints)} systems)")
res = gc.rank(gc.evaluate(pts, gc.candidate_codes(None, pts), CTX))
top = [r["crs"] for r in res[:5]]
check(any(c in top for c in ("EPSG:28407", code_of("Pulkovo 1995 / Gauss-Kruger zone 7"),
                              code_of("GSK-2011 / Gauss-Kruger zone 7"))), f"no location: a zone-7 system first ({top})")
cn = rc.candidates_for_values(sample("EPSG:4527", 116.4, 39.9))
check("EPSG:4527" in cn and code_of("Xian 1980 / 3-degree Gauss-Kruger zone 39") in cn, "zone 39 from a Chinese easting")
pl = rc.candidates_for_values(sample("EPSG:2178", 21.0, 52.23))
check("EPSG:2178" in pl, "Polish CS2000 zone 7 from the easting")
check(rc.candidates_for_values([(512345.0, 2734567.0)]) == {}, "ordinary UTM numbers give no zone hints")

# ---- the tool end to end -----------------------------------------------------------------------
ctx = QgsProcessingContext()
ctx.setProject(QgsProject.instance())
fb = QgsProcessingFeedback()
lyr = QgsVectorLayer("Point?crs=EPSG:4326", "gcj", "memory")
feats = []
truth = []
for i in range(5):
    lo, la = 116.39 + 0.002 * i, 39.90 + 0.001 * i
    truth.append((lo, la))
    f = QgsFeature()
    f.setGeometry(QgsGeometry.fromPointXY(QgsPointXY(*rc.wgs84_to_gcj02(lo, la))))
    feats.append(f)
lyr.dataProvider().addFeatures(feats)
res = processing.run("biruni:guesscrs", {"INPUT": lyr, "LOCATION": "116.395,39.902 [EPSG:4326]", "ASSIGN": True,
                                         "OUTPUT": "memory:", "OUTPUT_LAYER": "memory:"}, context=ctx, feedback=fb)
check(res["TOP_CRS"] == "REG:GCJ02", f"web-map (GCJ-02) points recognised ({res['TOP_CRS']})")
copy = res["OUTPUT_LAYER"] if isinstance(res["OUTPUT_LAYER"], QgsVectorLayer) else ctx.getMapLayer(res["OUTPUT_LAYER"])
err = max(gc.haversine_km(*t, f.geometry().asPoint().x(), f.geometry().asPoint().y()) * 1000
          for t, f in zip(truth, copy.getFeatures()))
check(copy.crs().authid() == "EPSG:4326" and err < 1.0, f"copy moved back to WGS 84 ({err:.2f} m)")

ain = QgsVectorLayer("Point?crs=EPSG:20438", "ain", "memory")
q = QgsCoordinateTransform(WGS84, QgsCoordinateReferenceSystem("EPSG:20438"), CTX).transform(QgsPointXY(46.7, 24.7))
f = QgsFeature()
f.setGeometry(QgsGeometry.fromPointXY(q))
ain.dataProvider().addFeatures([f])
res = processing.run("biruni:guesscrs", {"INPUT": ain, "LOCATION": "46.7,24.7 [EPSG:4326]", "OUTPUT": "memory:"},
                     context=ctx, feedback=fb)
check(len(res["TRANSFORMS"]) >= 2 and res["TRANSFORM_SPREAD_M"] > 1.0,
      f"competing Ain el Abd transformations reported ({len(res['TRANSFORMS'])}, {res['TRANSFORM_SPREAD_M']:.1f} m)")

# ---- shift grids -------------------------------------------------------------------------------
check(os.path.isdir(proj_grids.install_dir()), f"proj folder {proj_grids.install_dir()}")
miss = proj_grids.missing_grids(QgsCoordinateReferenceSystem("EPSG:31467"), QgsCoordinateReferenceSystem("EPSG:25832"))
names = {g["name"] for g in miss}
installed = proj_grids.installed_grids()
check("de_adv_BETA2007.tif" in names or "de_adv_BETA2007.tif" in installed, f"BeTA2007 listed as missing ({sorted(names)[:4]})")
beta = next((g for g in miss if g["name"] == "de_adv_BETA2007.tif"), None)
check(beta is None or (beta["downloadable"] and beta["url"].startswith(proj_grids.CDN)), "BeTA2007 is downloadable")
tmp = tempfile.mkdtemp()
calls = []


def fake_fetch(url):
    calls.append(url)
    if "broken" in url:
        raise OSError("HTTP 404")
    return b"GRID" * 100


out = proj_grids.download(["xx_test_grid.tif", "broken.tif", "../evil.tif", {"name": "yy.tif"}], fetch=fake_fetch,
                          target=tmp)
check(os.path.exists(os.path.join(tmp, "xx_test_grid.tif")) and out["xx_test_grid.tif"].endswith("xx_test_grid.tif"),
      "download writes the grid into the proj folder")
check("404" in out["broken.tif"] and not os.path.exists(os.path.join(tmp, "broken.tif")), "failed download reported")
check(out["../evil.tif"] and "../evil.tif" not in [u.split("/")[-1] for u in calls] and
      not os.path.exists(os.path.join(tmp, "..", "evil.tif")), "path tricks refused")
check(all(u.startswith("https://cdn.proj.org/") for u in calls), "only the PROJ CDN is used")
again = proj_grids.download(["xx_test_grid.tif"], fetch=fake_fetch, target=tmp)
check(len(calls) == 3 and again["xx_test_grid.tif"].endswith("xx_test_grid.tif"), "an installed grid is not fetched again")
check("Istituto Geografico Militare" in proj_grids.licence_note("it") and proj_grids.licence_note("SA") == "",
      "licensed grids explained")
spread = proj_grids.transformation_spread(QgsCoordinateReferenceSystem("EPSG:20438"), q.x(), q.y())
check(len(spread["ops"]) >= 2 and spread["ops"][0]["shift_m"] == 0.0, f"spread helper ({spread['spread_m']:.1f} m)")

# live download (only when the network works)
try:
    live = proj_grids.download(["us_noaa_alaska.tif"], target=tempfile.mkdtemp())
    path = live["us_noaa_alaska.tif"]
    if os.path.exists(path):
        check(os.path.getsize(path) > 1000, f"live: a real grid downloaded ({os.path.getsize(path)} bytes)")
    else:
        print(f"     live download skipped: {path}")
except Exception as e:  # noqa: BLE001
    print(f"     live download skipped: {e}")

print("ALL PASS" if not failures else f"{len(failures)} failure(s)")
sys.exit(1 if failures else 0)
