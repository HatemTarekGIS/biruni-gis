r"""Spatial statistics of points and rates (spatial_stats/points.py, distribution.py, rates.py and
algorithms/ss_points.py): the maths on synthetic patterns with known answers, then every tool run through
processing.run on layers in a projected CRS and in EPSG:4326, in English and Arabic, with reports.

Run:  engine\bin\python-qgis.bat tests\test_spatial_stats_points.py
"""
import math
import os
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "app", "plugins"))
sys.path.insert(0, os.path.join(HERE, "..", "app", "python"))

SETTINGS_DIR = tempfile.mkdtemp()
os.environ["QGIS_CUSTOM_CONFIG_PATH"] = SETTINGS_DIR

import numpy as np  # noqa: E402
from qgis.core import (QgsApplication, QgsCoordinateReferenceSystem, QgsCoordinateTransform, QgsFeature,  # noqa: E402
                       QgsGeometry, QgsPointXY, QgsProject, QgsRasterLayer, QgsVectorLayer)

app = QgsApplication([], False)
app.initQgis()
sys.path.append(QgsApplication.prefixPath() + "/python/plugins")
import processing  # noqa: E402
from processing.core.Processing import Processing  # noqa: E402

Processing.initialize()
from biruni import i18n  # noqa: E402
from biruni.algorithms import BiruniProvider, algorithm_classes  # noqa: E402
from biruni.spatial_stats import distribution as D  # noqa: E402
from biruni.spatial_stats import points as P  # noqa: E402
from biruni.spatial_stats import rates as RT  # noqa: E402

i18n.init("en")
provider = BiruniProvider()
QgsApplication.processingRegistry().addProvider(provider)
failures = []
WORK = tempfile.mkdtemp()
KEEP_PNG = os.environ.get("SS_KEEP_PNG")  # folder to save one report's charts for a visual check


def check(cond, msg):
    print(("PASS " if cond else "FAIL ") + msg)
    if not cond:
        failures.append(msg)


g = np.random.default_rng(7)
ids = {c.ID for c in algorithm_classes()}
for a in ("ssnearest", "ssripley", "sskde", "sscentre", "ssdbscan", "ssrates"):
    check(a in ids, f"registered: biruni:{a}")

# ---- maths: Clark-Evans ---------------------------------------------------------------------------------
s = 10.0
gx, gy = np.meshgrid(np.arange(30) * s, np.arange(30) * s)
grid = np.column_stack([gx.ravel(), gy.ravel()])
reg = P.Region.rectangle(-s / 2, -s / 2, 30 * s - s / 2, 30 * s - s / 2)
ce = P.clark_evans(grid, reg.area)
check(abs(ce["R"] - 2.0) < 0.01 and ce["z"] > 20, f"Clark-Evans regular grid: R = {ce['R']:.3f} (2 for a square lattice; "
                                                   "2.15 is the hexagonal maximum)")
hexa = []
for r in range(30):
    for c in range(30):
        hexa.append((c * s + (s / 2 if r % 2 else 0), r * s * math.sqrt(3) / 2))
hexa = np.array(hexa)
hreg = P.Region.rectangle(-s / 4, -s * math.sqrt(3) / 4, 30 * s + s / 4, 30 * s * math.sqrt(3) / 2 - s * math.sqrt(3) / 4)
ceh = P.clark_evans(hexa, hreg.area)
check(2.05 < ceh["R"] < 2.2, f"Clark-Evans hexagonal lattice: R = {ceh['R']:.3f} (≈2.15)")
big = P.Region.rectangle(0, 0, 1000, 1000)
tight = g.normal(500, 3, (300, 2))
ce = P.clark_evans(tight, big.area)
check(ce["R"] < 0.1 and ce["p"] < 1e-6, f"Clark-Evans tight cluster: R = {ce['R']:.3f} << 1")
csr = big.random(1000, g)
ce = P.clark_evans(csr, big.area, big.perimeter, "donnelly")
check(0.93 < ce["R"] < 1.07, f"Clark-Evans CSR (Donnelly): R = {ce['R']:.3f} ≈ 1")
mc = P.nn_monte_carlo(ce["observed"], len(csr), big, 99, np.random.default_rng(1))
check(mc["p"] > 0.05 and 0.93 < mc["R"] < 1.07, f"Monte Carlo on CSR: p = {mc['p']:.2f}, R = {mc['R']:.3f}")

# ---- region helpers ----------------------------------------------------------------------------------
lshape = P.Region([[np.array([[0, 0], [100, 0], [100, 50], [50, 50], [50, 100], [0, 100]])]])
check(abs(lshape.area - 7500) < 1e-6 and abs(lshape.perimeter - 400) < 1e-6, "Region area and perimeter (L shape)")
rp = lshape.random(2000, g)
check(lshape.contains(rp).all() and not ((rp[:, 0] > 50) & (rp[:, 1] > 50)).any(), "random points stay in the L shape")
fr = big.circle_inside(np.array([[0.0, 500.0], [500.0, 500.0], [0.0, 0.0]]), [100.0])
check(abs(fr[0, 0] - 0.5) < 0.03 and fr[1, 0] == 1.0 and abs(fr[2, 0] - 0.25) < 0.03,
      "circle share inside: edge 1/2, centre 1, corner 1/4")

# ---- maths: Ripley ---------------------------------------------------------------------------------------
d = P.default_distances(big, 25)
t0 = time.time()
rk = P.ripley(csr[:600], big, d, "isotropic", 99, np.random.default_rng(3))
inside_share = float(np.mean(rk["state"] == 0))
check(inside_share >= 0.9 and rk["p"] > 0.05,
      f"Ripley L on CSR stays in the envelope ({inside_share:.0%} of distances, global p = {rk['p']:.2f}, "
      f"{time.time() - t0:.1f} s)")
kk = P.k_function(csr, big, d, "isotropic")
check(np.max(np.abs(np.sqrt(kk / np.pi) - d)) < 0.06 * d[-1], "isotropic K of CSR ≈ π d²")
kb = P.k_function(csr, big, d, "border")
check(np.max(np.abs(np.sqrt(kb / np.pi) - d)) < 0.08 * d[-1], "border K of CSR ≈ π d²")
parents = g.uniform(100, 900, (15, 2))
clus = np.vstack([p + g.normal(0, 15, (30, 2)) for p in parents])
clus = clus[big.contains(clus)]
rk2 = P.ripley(clus, big, d, "isotropic", 99, np.random.default_rng(3))
check(rk2["runs"][0][0] == 1 and rk2["p"] < 0.05, f"Ripley on a Thomas-like cluster process: clustered at short "
                                                    f"distances (runs {[(r[0], round(r[1]), round(r[2])) for r in rk2['runs']]})")
lab = np.zeros(len(csr), bool)
lab[:500] = True
ck = P.cross_k(csr, lab, big, d, "isotropic", 99, np.random.default_rng(4))
check(ck["p"] > 0.05, f"cross-K of random labels: independent (p = {ck['p']:.2f})")

# ---- maths: KDE ------------------------------------------------------------------------------------------
h, info = D.silverman_bandwidth(clus)
check(h > 0 and abs(h - info["sigma"] * len(clus) ** (-1 / 6)) < 1e-9, f"Silverman 2-D bandwidth h = {h:.1f} m")
ext = (-4 * h, -4 * h, 1000 + 4 * h, 1000 + 4 * h)
cell = D.grid_for(ext, h)
dens, x0, y1, cols, rows = D.kde_grid(clus, None, h, ext, cell)
integral = dens.sum() * cell * cell
check(abs(integral - len(clus)) / len(clus) < 0.01, f"KDE integrates to n: {integral:.1f} vs {len(clus)}")
wts = g.uniform(1, 3, len(clus))
dw, *_ = D.kde_grid(clus, wts, h, ext, cell)
check(abs(dw.sum() * cell * cell - wts.sum()) / wts.sum() < 0.01, "weighted KDE integrates to the total weight")
hl, gr, sc = D.lcv_bandwidth(clus, None, h, np.random.default_rng(1))
check(gr[0] < hl < gr[-1] and hl < h, f"likelihood CV picks an inner bandwidth ({hl:.1f} m) below Silverman's "
                                      "for a clustered pattern")

# ---- maths: centres --------------------------------------------------------------------------------------
line = np.array([[0, 0], [1, 0], [2, 0], [10, 0], [100, 0]], float)
mcen = D.median_centre(line)
check(np.allclose(mcen, [2, 0], atol=1e-3), f"median centre of collinear points = the middle point ({mcen})")
tri = np.array([[0, 0], [4, 0], [0, 3], [1, 1]], float)
from scipy.optimize import minimize  # noqa: E402

ref = minimize(lambda p: np.hypot(*(tri - p).T).sum(), tri.mean(axis=0), method="Nelder-Mead",
               options={"xatol": 1e-10, "fatol": 1e-12}).x
check(np.allclose(D.median_centre(tri), ref, atol=1e-4), "median centre = numerical minimum of the distance sum")
sq = np.array([[0, 0], [10, 0], [10, 10], [0, 10]], float)
check(np.allclose(D.median_centre(sq), [5, 5], atol=1e-6), "median centre of a square = its centre")
base = np.column_stack([g.normal(0, 300, 5000), g.normal(0, 60, 5000)])  # long along local x
for want in (30.0, 120.0):
    th = math.radians(want)
    # local x axis points to azimuth `want` (clockwise from north): east = sin, north = cos
    rot = np.column_stack([base[:, 0] * math.sin(th) + base[:, 1] * math.cos(th),
                           base[:, 0] * math.cos(th) - base[:, 1] * math.sin(th)]) + [1000, 2000]
    c, a, b, az = D.deviational_ellipse(rot)
    check(abs(az - want) < 2.0 and abs(a / math.sqrt(2) - 300) < 15 and abs(b / math.sqrt(2) - 60) < 5,
          f"SDE orientation {az:.1f}° (want {want}°), axes {a / math.sqrt(2):.0f} / {b / math.sqrt(2):.0f} m")
    inside = (((rot - c) @ np.array([math.sin(th), math.cos(th)])) / a) ** 2 + \
             (((rot - c) @ np.array([math.cos(th), -math.sin(th)])) / b) ** 2 <= 1
check(0.58 < inside.mean() < 0.68, f"1 SD ellipse holds ≈63% of a normal cloud ({inside.mean():.0%})")
check(D.compass(1, 1) == "north-east" and D.compass(-1, 0, "ar") == "الغرب", "compass words")

# ---- maths: DBSCAN ---------------------------------------------------------------------------------------
centres = np.array([[200, 200], [700, 300], [400, 800]])
blobs = np.vstack([cc + g.normal(0, 25, (300, 2)) for cc in centres])
noise = g.uniform(0, 1000, (150, 2))
xyb = np.vstack([blobs, noise])
eps, kd = P.suggest_eps(xyb, 5)
lbl, core = P.dbscan(xyb, eps, 5)
truth = np.repeat([1, 2, 3], 300)
purity = np.mean([np.bincount(lbl[:900][truth == t]).max() / 300 for t in (1, 2, 3)])
check(lbl.max() == 3 and purity > 0.95, f"DBSCAN recovers 3 blobs (eps {eps:.1f} m suggested, purity {purity:.0%}, "
                                        f"noise {np.mean(lbl == 0):.0%})")
check(np.mean(lbl[900:] == 0) > 0.6, "most uniform background points are noise")

# ---- maths: rates ----------------------------------------------------------------------------------------
pop = np.round(10 ** g.uniform(1.5, 5, 200))
ev = g.poisson(0.002 * pop * g.gamma(20, 1 / 20, 200))
eb, w, b, a = RT.global_eb(ev, pop)
small, large = pop < 300, pop > 20000
check(np.nanmean(w[small]) < np.nanmean(w[large]), f"EB keeps less of small areas' own rate "
                                                    f"({np.nanmean(w[small]):.2f} vs {np.nanmean(w[large]):.2f})")
raw = RT.raw_rates(ev, pop)
pull = np.abs(raw - eb) / np.maximum(np.abs(raw - b), 1e-12)
check(np.nanmean(pull[small]) > np.nanmean(pull[large]), "EB pulls small populations towards the mean more")
check(np.nanstd(eb) < np.nanstd(raw), "smoothed rates vary less than raw rates")
lo, hi = RT.poisson_ci(np.array([0, 10]), np.array([100, 100]))
check(lo[0] == 0 and abs(hi[0] - 0.03689) < 1e-4 and abs(lo[1] - 0.04795) < 1e-4, "exact Poisson intervals (Garwood)")
nb = RT.knn_neighbours(np.column_stack([np.arange(10.0), np.zeros(10)]), 2)
leb, lw = RT.local_eb(ev[:10], pop[:10], nb)
check(np.all(np.isfinite(leb)), "local EB with kNN neighbours")

# ---- layers --------------------------------------------------------------------------------------------
UTM = QgsCoordinateReferenceSystem("EPSG:32638")
WGS = QgsCoordinateReferenceSystem("EPSG:4326")
to_wgs = QgsCoordinateTransform(UTM, WGS, QgsProject.instance())
OX, OY = 670000.0, 2730000.0  # Riyadh


def point_layer(xy, crs="EPSG:32638", cats=None, years=None, weights=None, name="pts"):
    lyr = QgsVectorLayer(f"Point?crs={crs}&field=cat:string&field=year:integer&field=w:double", name, "memory")
    feats = []
    for k, (x, y) in enumerate(xy):
        f = QgsFeature(lyr.fields())
        p = QgsPointXY(OX + x, OY + y)
        if crs != "EPSG:32638":
            p = to_wgs.transform(p)
        f.setGeometry(QgsGeometry.fromPointXY(p))
        f.setAttributes([cats[k] if cats is not None else "a", int(years[k]) if years is not None else 2024,
                         float(weights[k]) if weights is not None else 1.0])
        feats.append(f)
    lyr.dataProvider().addFeatures(feats)
    return lyr


def run(alg, params):
    t = time.time()
    r = processing.run(f"biruni:{alg}", params)
    return r, time.time() - t


def read(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def report_ok(path, *words):
    if not path or not os.path.exists(path):
        return False
    txt = read(path)
    return all(w in txt for w in words) and "data:image/png;base64," in txt


def rep(name):
    return os.path.join(WORK, name + ".html")


clus_lyr = point_layer(clus)
csr_lyr = point_layer(csr)
wgs_lyr = point_layer(clus, "EPSG:4326")
area_lyr = QgsVectorLayer("Polygon?crs=EPSG:32638", "area", "memory")
fa = QgsFeature()
fa.setGeometry(QgsGeometry.fromWkt(f"POLYGON(({OX} {OY},{OX + 1000} {OY},{OX + 1000} {OY + 1000},{OX} {OY + 1000},{OX} {OY}))"))
area_lyr.dataProvider().addFeatures([fa])

# nearest neighbour
r, dt = run("ssnearest", {"INPUT": clus_lyr, "AREA": 1, "AREA_LAYER": area_lyr, "REPORT": rep("nn")})
check(r["R"] < 0.6 and "clustered" in r["ANSWER"] and report_ok(r["REPORT"], "What this does not mean"),
      f"ssnearest clustered: R = {r['R']:.2f} ({dt:.1f} s) - {r['ANSWER'][:90]}...")
r, _ = run("ssnearest", {"INPUT": csr_lyr, "AREA": 1, "AREA_LAYER": area_lyr, "REPORT": rep("nn2")})
check("random" in r["ANSWER"] and 0.9 < r["R"] < 1.1, f"ssnearest CSR: random, R = {r['R']:.2f}")
r, _ = run("ssnearest", {"INPUT": wgs_lyr, "AREA": 1, "AREA_LAYER": area_lyr, "REPORT": rep("nn3")})
check(r["R"] < 0.6 and "EPSG:32638" in read(r["REPORT"]),
      "ssnearest on EPSG:4326: measured in UTM 38N and says so")
r1, _ = run("ssnearest", {"INPUT": clus_lyr, "AREA": 0, "SEED": 5, "REPORT": rep("nn4")})
r2, _ = run("ssnearest", {"INPUT": clus_lyr, "AREA": 0, "SEED": 5, "REPORT": rep("nn5")})
check(r1["P_SIMULATED"] == r2["P_SIMULATED"] and r1["ANSWER"] == r2["ANSWER"], "same seed, same answer")
try:
    run("ssnearest", {"INPUT": point_layer(csr[:5]), "REPORT": rep("x")})
    check(False, "ssnearest refuses 5 points")
except Exception as e:
    check("Too few points" in str(e), "ssnearest refuses 5 points")
dup_lyr = point_layer(np.vstack([csr[:200], csr[:50]]))
r, _ = run("ssnearest", {"INPUT": dup_lyr, "AREA": 1, "AREA_LAYER": area_lyr, "REPORT": rep("dup")})
check("duplicates" in read(r["REPORT"]), "duplicate points are reported")
outside = point_layer(np.vstack([csr[:300], [[1500, 1500], [-300, 200]]]))
r, _ = run("ssnearest", {"INPUT": outside, "AREA": 1, "AREA_LAYER": area_lyr, "REPORT": rep("out")})
check("2 points lie outside the study area" in read(r["REPORT"]), "points outside the area reported")

# Ripley
r, dt = run("ssripley", {"INPUT": clus_lyr, "AREA": 1, "AREA_LAYER": area_lyr, "REPORT": rep("ripley"),
                         "TABLE": "TEMPORARY_OUTPUT"})
check("clustered between" in r["ANSWER"] or "clustered at" in r["ANSWER"], f"ssripley: {r['ANSWER']} ({dt:.1f} s)")
check(r["TABLE"].featureCount() == 30 and report_ok(r["REPORT"], "L(d) - d"), "ssripley table and report")
r, _ = run("ssripley", {"INPUT": csr_lyr, "AREA": 1, "AREA_LAYER": area_lyr, "CORRECTION": 1, "REPORT": rep("ripley2")})
check(r["P"] > 0.05, f"ssripley CSR, border correction: {r['ANSWER']}")
cats = np.where(np.arange(len(clus)) % 2 == 0, "A", "B")
r, _ = run("ssripley", {"INPUT": point_layer(clus, cats=cats), "CATEGORY_FIELD": "cat", "CATEGORY_A": "A",
                        "CATEGORY_B": "B", "REPORT": rep("cross")})
check("'B' points relative to 'A'" in r["ANSWER"], f"ssripley cross-K: {r['ANSWER'][:120]}")

# KDE
out_tif = os.path.join(WORK, "kde.tif")
r, dt = run("sskde", {"INPUT": clus_lyr, "OUTPUT": out_tif, "REPORT": rep("kde")})
rl = QgsRasterLayer(out_tif, "kde")
check(rl.isValid() and rl.crs().authid() == "EPSG:32638", f"sskde raster in the metric CRS ({dt:.1f} s)")
check(r["TOP10_SHARE"] > 0.3 and "densest 10%" in r["ANSWER"], f"sskde: {r['ANSWER'][:110]}")
from osgeo import gdal  # noqa: E402

ds = gdal.Open(out_tif)
arr = ds.ReadAsArray().astype(float)
cs = ds.GetGeoTransform()[1]
ds = None
check(abs(arr.sum() * cs * cs / 1e6 - len(clus)) / len(clus) < 0.01, "raster (per km²) integrates to n")
r, _ = run("sskde", {"INPUT": wgs_lyr, "METHOD": 1, "WEIGHT": "w", "OUTPUT": os.path.join(WORK, "kde2.tif"),
                     "REPORT": rep("kde2")})
check(QgsRasterLayer(os.path.join(WORK, "kde2.tif"), "k").crs().authid() == "EPSG:32638" and
      "cross-validation" in r["ANSWER"], "sskde from EPSG:4326 with cross-validation writes UTM")

# centre
years = np.repeat([2023, 2025], len(clus))
moved = np.vstack([clus, clus + [850.0, 850.0]])  # the same pattern 1.2 km to the north-east
r, dt = run("sscentre", {"INPUT": point_layer(moved, years=years), "CASE_FIELD": "year", "CENTRES": "TEMPORARY_OUTPUT",
                         "SHAPES": "TEMPORARY_OUTPUT", "REPORT": rep("centre")})
check("1.2 km towards the north-east between 2023 and 2025" in r["ANSWER"], f"sscentre movement: {r['ANSWER'][:120]}")
check(r["CENTRES"].featureCount() == 4 and r["SHAPES"].featureCount() == 4, "sscentre: 2 centres and 2 shapes per group")
r, _ = run("sscentre", {"INPUT": point_layer(rot - [1000, 2000], "EPSG:4326"), "CENTRES": "TEMPORARY_OUTPUT",
                        "SHAPES": "TEMPORARY_OUTPUT", "LEVEL": 1, "REPORT": rep("centre2")})
ell = [f for f in r["SHAPES"].getFeatures() if f["kind"] == "ellipse"][0]
check(abs(ell["azimuth"] - 120) < 3 and ell["sd_level"] == 2 and r["SHAPES"].crs().authid() == "EPSG:4326",
      f"sscentre ellipse from EPSG:4326: {ell['azimuth']:.1f}° (UTM grid vs north), output in the layer's CRS")

# DBSCAN
r, dt = run("ssdbscan", {"INPUT": point_layer(xyb), "OUTPUT": "TEMPORARY_OUTPUT", "CLUSTERS": "TEMPORARY_OUTPUT",
                         "REPORT": rep("dbscan")})
check(r["N_CLUSTERS"] == 3 and r["CLUSTERS"].featureCount() == 3 and r["OUTPUT"].featureCount() == len(xyb),
      f"ssdbscan: {r['ANSWER'][:110]}")
check(r["OUTPUT"].fields().indexOf("cluster") >= 0, "ssdbscan: cluster field on the points")
r, _ = run("ssdbscan", {"INPUT": point_layer(xyb, "EPSG:4326"), "HULL": 1, "OUTPUT": "TEMPORARY_OUTPUT",
                        "CLUSTERS": "TEMPORARY_OUTPUT", "REPORT": rep("dbscan2")})
check(r["N_CLUSTERS"] == 3 and r["CLUSTERS"].crs().authid() == "EPSG:4326", "ssdbscan on EPSG:4326 with concave hulls")

# rates
poly = QgsVectorLayer("Polygon?crs=EPSG:32638&field=name:string&field=cases:integer&field=pop:double", "areas", "memory")
feats = []
for k in range(200):
    i, j = k % 20, k // 20
    f = QgsFeature(poly.fields())
    x, y = OX + i * 1000, OY + j * 1000
    f.setGeometry(QgsGeometry.fromWkt(f"POLYGON(({x} {y},{x + 1000} {y},{x + 1000} {y + 1000},{x} {y + 1000},{x} {y}))"))
    p = 0.0 if k == 7 else float(pop[k])
    f.setAttributes([f"Area {k}", int(ev[k]) if k != 7 else 0, p])
    feats.append(f)
poly.dataProvider().addFeatures(feats)
r, dt = run("ssrates", {"INPUT": poly, "EVENTS": "cases", "POPULATION": "pop", "NAME": "name",
                        "OUTPUT": "TEMPORARY_OUTPUT", "REPORT": rep("rates")})
out = {f["name"]: f for f in r["OUTPUT"].getFeatures()}
check(out["Area 7"]["reliability"] == "no rate" and "zero population" in read(r["REPORT"]),
      "ssrates flags zero population")
nbs = [f["neighbours"] for f in r["OUTPUT"].getFeatures()]
check(min(nbs) == 3 and max(nbs) == 8, "queen contiguity: corners 3, inside 8 neighbours")
check("unreliable" in r["ANSWER"] and report_ok(r["REPORT"], "funnel plot"), f"ssrates: {r['ANSWER'][:140]} ({dt:.1f} s)")
r, _ = run("ssrates", {"INPUT": poly, "EVENTS": "cases", "POPULATION": "pop", "NEIGHBOURS": 1, "K": 4,
                       "OUTPUT": "TEMPORARY_OUTPUT", "REPORT": rep("rates2")})
check(all(f["neighbours"] == 4 for f in r["OUTPUT"].getFeatures()), "ssrates with 4 nearest areas")

# ---- Arabic ----------------------------------------------------------------------------------------------
i18n.init("ar")
ar_answers = {}
for alg, params in (("ssnearest", {"INPUT": clus_lyr}), ("ssripley", {"INPUT": clus_lyr, "SIMULATIONS": 39}),
                    ("sskde", {"INPUT": clus_lyr, "OUTPUT": os.path.join(WORK, "kde_ar.tif")}),
                    ("sscentre", {"INPUT": point_layer(moved, years=years), "CASE_FIELD": "year",
                                  "CENTRES": "TEMPORARY_OUTPUT", "SHAPES": "TEMPORARY_OUTPUT"}),
                    ("ssdbscan", {"INPUT": point_layer(xyb), "OUTPUT": "TEMPORARY_OUTPUT", "CLUSTERS": "TEMPORARY_OUTPUT"}),
                    ("ssrates", {"INPUT": poly, "EVENTS": "cases", "POPULATION": "pop", "OUTPUT": "TEMPORARY_OUTPUT"})):
    params = dict(params, REPORT=rep(alg + "_ar"))
    r, _ = run(alg, params)
    ar_answers[alg] = r["ANSWER"]
    txt = read(r["REPORT"])
    check(any("؀" <= ch <= "ۿ" for ch in r["ANSWER"]) and 'dir="rtl"' in txt and "ما لا تعنيه" in txt,
          f"{alg} in Arabic: {r['ANSWER'][:80]}")
i18n.init("en")
if KEEP_PNG:
    import base64
    import re

    for name in ("ssripley_ar", "ssrates_ar", "sscentre_ar", "kde", "dbscan"):
        for k, m in enumerate(re.findall(r"base64,([A-Za-z0-9+/=]+)", read(rep(name)))):
            with open(os.path.join(KEEP_PNG, f"{name}_{k}.png"), "wb") as fh:
                fh.write(base64.b64decode(m))

# ---- timing on 10,000 points ------------------------------------------------------------------------------
many = np.vstack([p + g.normal(0, 60, (400, 2)) for p in g.uniform(0, 5000, (20, 2))] + [g.uniform(0, 5000, (2000, 2))])
many_lyr = point_layer(many)
check(many_lyr.featureCount() == 10000, "10,000 points")
for alg, params, limit in (("ssnearest", {"INPUT": many_lyr}, 30), ("ssripley", {"INPUT": many_lyr}, 60),
                           ("sskde", {"INPUT": many_lyr, "OUTPUT": os.path.join(WORK, "kde10k.tif")}, 30),
                           ("sscentre", {"INPUT": many_lyr, "CENTRES": "TEMPORARY_OUTPUT", "SHAPES": "TEMPORARY_OUTPUT"}, 20),
                           ("ssdbscan", {"INPUT": many_lyr, "OUTPUT": "TEMPORARY_OUTPUT", "CLUSTERS": "TEMPORARY_OUTPUT"}, 30)):
    r, dt = run(alg, dict(params, REPORT=rep(alg + "_10k")))
    check(dt < limit, f"{alg} on 10,000 points: {dt:.1f} s (limit {limit} s)")
r, dt = run("sskde", {"INPUT": many_lyr, "METHOD": 1, "OUTPUT": os.path.join(WORK, "kde10k_lcv.tif"), "REPORT": rep("lcv10k")})
check(dt < 60, f"sskde cross-validation on 10,000 points: {dt:.1f} s")

print()
print(f"{len(failures)} failures" if failures else "ALL PASS")
QgsApplication.exitQgis()
sys.exit(1 if failures else 0)
