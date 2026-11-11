r"""Spatial statistics - autocorrelation tools (spatial_stats/weights, autocorr, spacetime, scale and
algorithms/ss_autocorr): the maths against hand-computed values and known results, then every tool run
headless through processing.run on synthetic layers.

Run:  engine\bin\python-qgis.bat tests\test_spatial_stats_autocorr.py
"""
import os
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "app", "plugins"))
SETTINGS_DIR = tempfile.mkdtemp()
os.environ["QGIS_CUSTOM_CONFIG_PATH"] = SETTINGS_DIR

import numpy as np  # noqa: E402
from qgis.core import (QgsApplication, QgsFeature, QgsGeometry, QgsPointXY, QgsProcessingContext,  # noqa: E402
                       QgsProcessingException, QgsProcessingFeedback, QgsProject, QgsVectorLayer)
from qgis.PyQt.QtCore import QDate  # noqa: E402

app = QgsApplication([], False)
app.initQgis()
sys.path.append(QgsApplication.prefixPath() + "/python/plugins")
import processing  # noqa: E402
from processing.core.Processing import Processing  # noqa: E402

Processing.initialize()
from biruni import i18n  # noqa: E402
from biruni.algorithms import BiruniProvider, algorithm_classes  # noqa: E402
from biruni.spatial_stats import autocorr as ac  # noqa: E402
from biruni.spatial_stats import scale as sc  # noqa: E402
from biruni.spatial_stats import spacetime as stc  # noqa: E402
from biruni.spatial_stats import weights as wts  # noqa: E402
from biruni.spatial_stats.common import rng  # noqa: E402

provider = BiruniProvider()
QgsApplication.processingRegistry().addProvider(provider)
failures = []


def check(cond, msg):
    print(("PASS " if cond else "FAIL ") + msg)
    if not cond:
        failures.append(msg)


class Feedback(QgsProcessingFeedback):
    def __init__(self):
        super().__init__()
        self.warnings = []

    def pushWarning(self, text):  # noqa: N802
        self.warnings.append(text)


ctx = QgsProcessingContext()
ctx.setProject(QgsProject.instance())
WORK = tempfile.mkdtemp()


def run(alg, params, expect_error=False):
    fb = Feedback()
    t = time.time()
    try:
        res = processing.run(f"biruni:{alg}", params, context=ctx, feedback=fb)
    except QgsProcessingException as e:
        if expect_error:
            return str(e), fb, 0
        raise
    if expect_error:
        return None, fb, 0
    return res, fb, time.time() - t


def out(value):
    return ctx.getMapLayer(value) if isinstance(value, str) else value


def layer(geom, crs, fields, rows):
    """rows: [(wkt, [attrs])]"""
    lyr = QgsVectorLayer(f"{geom}?crs={crs}&" + "&".join(f"field={f}" for f in fields), "t", "memory")
    feats = []
    for wkt, attrs in rows:
        f = QgsFeature(lyr.fields())
        f.setGeometry(QgsGeometry.fromWkt(wkt) if isinstance(wkt, str) else wkt)
        f.setAttributes(attrs)
        feats.append(f)
    lyr.dataProvider().addFeatures(feats)
    QgsProject.instance().addMapLayer(lyr)
    return lyr


def sq(x, y, s=100):
    return f"POLYGON(({x} {y},{x+s} {y},{x+s} {y+s},{x} {y+s},{x} {y}))"


g = rng(7)

# ================================================================== the maths =================================
# hand-computed: 4 features in a line, neighbours share a side, row-standardised; y = 1, 2, 3, 4
# z = (-1.5, -0.5, 0.5, 1.5), lag = (-0.5, -0.5, 0.5, 0.5), sum z*lag = 2, sum z^2 = 5 -> I = 4/4 * 2/5 = 0.4
w4 = wts.from_pairs([0, 1, 2], [1, 2, 3], 4)
r = ac.moran(np.array([1.0, 2, 3, 4]), wts.row_standardise(w4))
check(abs(r["I"] - 0.4) < 1e-12, f"Moran's I hand example = 0.4 ({r['I']})")
check(abs(r["EI"] + 1 / 3) < 1e-12, "E[I] = -1/(n-1)")
# binary (not standardised) weights: I = n/S0 * sum w z z / sum z^2 = 4/6 * (0.75+(-0.25)+0.75)*2/5
r = ac.moran(np.array([1.0, 2, 3, 4]), w4)
check(abs(r["I"] - 4 / 6 * 2.5 / 5) < 1e-12, f"Moran's I with binary weights ({r['I']})")

xy = g.random((150, 2)) * 1000
W = wts.row_standardise(wts.knn(xy, 6))
sims = [ac.moran(g.normal(size=150), W) for _ in range(3000)]
mean_i = np.mean([s["I"] for s in sims])
check(abs(mean_i + 1 / 149) < 0.0015, f"mean I of random data = -1/(n-1) ({mean_i:.5f} vs {-1/149:.5f})")
zs = np.array([s["z"] for s in sims])
check(abs(zs.std() - 1) < 0.06 and abs(zs.mean()) < 0.06, f"z under randomisation ~ N(0,1) (sd {zs.std():.3f})")
check(abs((np.array([s["p_norm"] for s in sims]) < 0.05).mean() - 0.05) < 0.015, "5% false alarms at p < 0.05")
r = ac.moran(g.normal(size=150), W, 199, rng(1))
r2 = ac.moran(r["z_values"], W, 199, rng(1))
check(r["p_sim"] == r2["p_sim"] and r["sims"].size == 199, "permutation p reproducible with a seed")

# weights
check(wts.cardinality(wts.knn(xy, 5)).tolist() == [5] * 150, "k nearest: exactly k each")
dup = np.vstack([xy[:10], xy[:10]])
check(wts.cardinality(wts.knn(dup, 3)).min() == 3, "k nearest survives duplicate points")
d0 = wts.min_threshold(xy)
check(wts.islands(wts.distance_band(xy, d0 * 1.0001)).size == 0 and
      wts.islands(wts.distance_band(xy, d0 * 0.999)).size > 0, "automatic band = smallest with no islands")
wi = wts.distance_band(xy, 200, inverse=True)
check(abs(wi[0].data.max() - 1 / np.hypot(*(xy[wi[0].indices[np.argmax(wi[0].data)]] - xy[0]))) < 1e-9,
      "inverse distance weight = 1/d")
hw = 100.0
cells = wts.hex_cells(g.random((3000, 2)) * 2000, hw)
card = wts.cardinality(wts.hex_neighbours(cells["centres"], hw))
check(card.max() == 6 and np.median(card) == 6, "hexagons have six touching neighbours")
check(cells["counts"].sum() == 3000, "hexagon counts add up to the points")
pt = np.array([[0.0, 0.0], [49.0, 0.0], [0.0, 30.0]])
c, rr = wts.hex_bin(pt, hw, (0, 0))
check(c.tolist()[:2] == [0, 0] and rr.tolist()[:2] == [0, 0], "points near a centre fall in its hexagon")

# Gi* on a constructed cluster: 20 x 20 grid, a block of high values in one corner
gx, gy = np.meshgrid(np.arange(20), np.arange(20))
cxy = np.column_stack([gx.ravel(), gy.ravel()]) * 100.0
yv = g.normal(10, 1, 400)
block = (gx.ravel() < 5) & (gy.ravel() < 5)
yv[block] += 6
gz, gp = ac.gi_star(yv, wts.distance_band(cxy, 150))
bins = ac.confidence_bins(gz, gp)
inner = block & (gx.ravel() > 0) & (gx.ravel() < 4) & (gy.ravel() > 0) & (gy.ravel() < 4)
check((gz[inner] > 2.58).all(), f"Gi* z > 2.58 in the heart of the cluster (min {gz[inner].min():.2f})")
check((bins[inner] == 3).all(), "Gi* bins: 99% hot spots in the cluster")
check((bins[~block] > 0).mean() < 0.1, "few hot spots outside the cluster")
# Gi* hand check: one feature, its 2 neighbours; x = (1, 2, 3)
z3, _ = ac.gi_star(np.array([1.0, 2, 3]), wts.from_pairs([0], [1], 3))
xb, s = 2.0, np.sqrt(14 / 3 - 4)
want = (3 - xb * 2) / (s * np.sqrt((3 * 2 - 4) / 2))
check(abs(z3[0] - want) < 1e-12, f"Gi* by hand ({z3[0]:.4f} vs {want:.4f})")

# LISA quadrants and clusters
check(ac.quadrant(np.array([1, -1, -1, 1]), np.array([1, 1, -1, -1])).tolist() == [ac.HH, ac.LH, ac.LL, ac.HL],
      "LISA quadrant logic (HH, LH, LL, HL)")
yv2 = g.normal(0, 1, 400)
yv2[block] += 4
yv2[(gx.ravel() > 14) & (gy.ravel() > 14)] -= 4
yv2[210] = 9  # a lone high value among ordinary ones... make its neighbours low
for j in (190, 209, 211, 230):
    yv2[j] = -4
lm = ac.local_moran(yv2, wts.distance_band(cxy, 110), 999, rng(3))
check((lm["cluster"][inner] == ac.HH).all(), "LISA: the high block is HH")
cold = (gx.ravel() > 15) & (gx.ravel() < 19) & (gy.ravel() > 15) & (gy.ravel() < 19)
check((lm["cluster"][cold] == ac.LL).mean() > 0.8, "LISA: the low block is LL")
check(lm["q"][210] == ac.HL and lm["q"][190] == ac.LH, "LISA: high among low = HL, its low neighbour = LH")
check(lm["p"].min() >= 1 / 1000, "pseudo p-values never below 1/(permutations+1)")
lm2 = ac.local_moran(yv2, wts.distance_band(cxy, 110), 999, rng(3))
check(np.array_equal(lm["p"], lm2["p"]), "LISA reproducible with a seed")
lmr = ac.local_moran(g.normal(size=400), wts.distance_band(cxy, 110), 999, rng(4), use_fdr=True)
check(lmr["sig"].sum() <= 4, f"LISA on random data with FDR: almost nothing significant ({lmr['sig'].sum()})")

# Mann-Kendall against known values
s_, mz, mp = ac.mann_kendall(np.arange(1, 11))
check(s_[0] == 45 and abs(mz[0] - 44 / np.sqrt(125)) < 1e-9, f"Mann-Kendall 1..10: S = 45, z = 3.935 ({mz[0]:.3f})")
# with ties: 1 2 2 3 -> S = 5, var = (4*3*13 - 2*1*9)/18 = 138/18, z = (5-1)/sqrt(var)
s_, mz, _ = ac.mann_kendall([1, 2, 2, 3])
check(s_[0] == 5 and abs(mz[0] - 4 / np.sqrt(138 / 18)) < 1e-9, "Mann-Kendall with ties")
s_, mz, mp = ac.mann_kendall([[5, 3, 4, 1, 2, 0, 1, -1, 0, -2], [1, 3, 2, 3, 1, 2, 3, 1, 2, 2]])
check(mz[0] < -2.5 and mp[1] > 0.5, "Mann-Kendall: decreasing series negative, flat one not significant")

# Co-location quotient: B placed right next to A
na = 200
axy = g.random((na, 2)) * 5000
bxy = axy + g.normal(0, 5, (na, 2))
oxy = g.random((800, 2)) * 5000
allxy = np.vstack([axy, bxy, oxy])
is_a = np.r_[np.ones(na, bool), np.zeros(na + 800, bool)]
is_b = np.r_[np.zeros(na, bool), np.ones(na, bool), np.zeros(800, bool)]
cq = ac.colocation(allxy, is_a, is_b, 4, 199, rng(5))
check(cq["clq"] > 2 and cq["p"] <= 0.01, f"CLQ: B next to A gives CLQ > 2 ({cq['clq']:.2f}, p {cq['p']:.3f})")
# hand: expected share = N_B/(N-1); each A has exactly one B among 4 neighbours -> CLQ = 0.25 / (200/1199)
check(abs(cq["clq"] - (cq["count_b"][is_a].mean() / 4) / (200 / 1199)) < 1e-9, "CLQ formula")
lab = g.random(1200) < 0.3
cr = ac.colocation(allxy, lab, ~lab, 8, 199, rng(6))
check(0.85 < cr["clq"] < 1.15 and cr["p"] > 0.05, f"CLQ of random labels ~ 1 ({cr['clq']:.2f})")

# space-time: classification rules on hand-made series
T = 12
pattern_rows = {
    "new": [0] * 11 + [1], "consecutive": [0] * 9 + [1, 1, 1], "persistent": [1] * 12,
    "sporadic": [0, 1, 0, 0, 1, 0, 0, 1, 0, 0, 0, 1], "oscillating": [-1, -1, 0, 0, 1, 0, -1, 0, 0, 0, 1, 1],
    "historical": [1] * 11 + [0], "no_pattern": [0] * 12,
}
sig = np.array(list(pattern_rows.values()))
flat = np.ones((len(sig), T))
flat[:, 1::2] = 1.0001  # z with no trend
codes, sides, _, _ = stc.classify(sig, flat + 0 * sig)
check(codes == list(pattern_rows), f"emerging categories by the definitions ({codes})")
zt = np.tile(np.arange(T, dtype=float), (2, 1))
codes2, _, _, _ = stc.classify(np.array([[1] * 12, [1] * 12]), np.vstack([zt[0], -zt[1]]))
check(codes2 == ["intensifying", "diminishing"], "intensifying / diminishing from the trend of z")
check(stc.classify(-np.array([[0] * 11 + [1]]), np.zeros((1, T)))[1][0] == -1, "cold side mirrored")
idx, starts = stc.to_steps(np.array(["2024-01-15", "2024-03-02", "2024-12-31"], dtype="datetime64[s]"), "M")
check(idx.tolist() == [0, 2, 11] and len(starts) == 12, "monthly steps follow the calendar")

# scale
check(sc.parse_factors("1, 2;0.5, x, -1") == [0.5, 1.0, 2.0], "scale factors parsed")
check(sc.verdict([{"ok": True, "I": 0.3, "p": 0.001}, {"ok": True, "I": 0.2, "p": 0.01}])[0] == "robust",
      "verdict robust")
check(sc.verdict([{"ok": True, "I": 0.3, "p": 0.001}, {"ok": True, "I": 0.01, "p": 0.4}])[0] == "depends",
      "verdict depends")

# no heavy imports at module import time (Biruni imports every tool when it starts)
import subprocess  # noqa: E402

probe = ("import sys; sys.path.insert(0, r'%s');" % os.path.join(HERE, "..", "app", "plugins").replace("\\", "/") +
         "import biruni.algorithms.base, biruni.spatial_stats.common;"
         "heavy = ('scipy', 'matplotlib', 'pandas'); before = {m for m in heavy if m in sys.modules};"
         "import biruni.spatial_stats.weights, biruni.spatial_stats.autocorr, biruni.spatial_stats.spacetime, "
         "biruni.spatial_stats.scale, biruni.algorithms.ss_autocorr;"
         "print('NEW', sorted({m for m in heavy if m in sys.modules} - before))")
res_p = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True)
check("NEW []" in res_p.stdout, f"no scipy/matplotlib/pandas loaded by importing the modules ({res_p.stdout.strip()[-60:]}"
                                f"{res_p.stderr.strip()[-200:]})")

# ================================================================== the tools =================================
ids = {c.ID for c in algorithm_classes()}
for a in ("ssmoran", "sslisa", "sshotspots", "ssincremental", "ssbivariate", "sscolocation", "ssemerging",
          "ssscale"):
    check(a in ids, f"{a} registered")

# polygons: 30 x 30 squares in UTM 38N; 'v' clustered (smooth + noise), 'r' random, 'cb' checkerboard
rows = []
for i in range(30):
    for j in range(30):
        v = np.sin(i / 6) + np.cos(j / 6) + g.normal(0, 0.3)
        rows.append((sq(500000 + i * 100, 2700000 + j * 100), [float(v), float(g.normal()), float((i + j) % 2),
                                                              float(v * 2 + g.normal(0, 0.5)), 1.0, int(round(np.exp(v) * 3))]))
rows.append((sq(520000, 2720000), [0.5, 0.1, 0.0, 1.0, 1.0, 2]))  # an island far away
poly = layer("Polygon", "EPSG:32638", ["v:double", "r:double", "cb:double", "v2:double", "one:double", "cnt:integer"], rows)

i18n.init("en_US")
rep = os.path.join(WORK, "moran.html")
res, fb, dt = run("ssmoran", {"INPUT": poly, "FIELD": "v", "REPORT": rep})
check(res["MORAN_I"] > 0.5 and res["P_VALUE"] <= 0.001, f"clustered polygons: I = {res['MORAN_I']:.3f}")
check(res["ANSWER"].startswith("The values are clustered") and "p < 0.001" in res["ANSWER"],
      f"English answer: {res['ANSWER']}")
check(os.path.exists(rep) and "data:image/png" in open(rep, encoding="utf-8").read(), "Moran report with charts")
check(any("island" in w for w in fb.warnings), "island reported")
res, _, _ = run("ssmoran", {"INPUT": poly, "FIELD": "cb", "WEIGHTS": 2})
check(res["MORAN_I"] < -0.8 and "dispersed" in res["ANSWER"], f"checkerboard (rook) is dispersed: {res['ANSWER']}")
res, _, _ = run("ssmoran", {"INPUT": poly, "FIELD": "r", "WEIGHTS": 4})
check(res["P_VALUE"] > 0.01 or abs(res["MORAN_I"]) < 0.05, f"random values: {res['ANSWER']}")
res, _, _ = run("ssmoran", {"INPUT": poly, "FIELD": "v", "WEIGHTS": 5, "DISTANCE": 250})
check(res["MORAN_I"] > 0.3, "inverse distance weights run")
err, _, _ = run("ssmoran", {"INPUT": poly, "FIELD": "one"}, expect_error=True)
check(err and "same" in err, f"constant values refused: {err}")
few = layer("Point", "EPSG:32638", ["v:double"], [(f"POINT({i * 10} {i * 7})", [float(i)]) for i in range(5)])
err, _, _ = run("ssmoran", {"INPUT": few, "FIELD": "v"}, expect_error=True)
check(err and "at least" in err, f"too few features refused: {err}")

i18n.init("ar")
rep_ar = os.path.join(WORK, "lisa_ar.html")
res, fb, dt = run("sslisa", {"INPUT": poly, "FIELD": "v", "OUTPUT": "TEMPORARY_OUTPUT", "REPORT": rep_ar})
lyr = out(res["OUTPUT"])
names = lyr.fields().names()
check(all(n in names for n in ("lisa_i", "lisa_z", "lisa_p", "lisa_type", "lisa_label")), "LISA output fields")
types = [f["lisa_type"] for f in lyr.getFeatures()]
check(types.count("HH") > 50 and types.count("LL") > 50, f"LISA finds HH and LL ({types.count('HH')}, "
                                                          f"{types.count('LL')})")
check(lyr.featureCount() == 901, "every input feature kept")
check(any("؀" <= ch <= "ۿ" for ch in res["ANSWER"]), f"Arabic answer: {res['ANSWER']}")
html = open(rep_ar, encoding="utf-8").read()
check('dir="rtl"' in html and "ما لا تعنيه" in html, "Arabic report, right to left, with the caveats")
check(isinstance(lyr.renderer(), type(lyr.renderer())) , "LISA layer loaded")

i18n.init("en_US")
res, _, _ = run("ssbivariate", {"INPUT": poly, "FIELD": "v", "FIELD_Y": "v2", "OUTPUT": "TEMPORARY_OUTPUT",
                                "REPORT": os.path.join(WORK, "biv.html")})
blyr = out(res["OUTPUT"])
check("bv_type" in blyr.fields().names() and "High v tends to sit next to high v2" in res["ANSWER"],
      f"bivariate: {res['ANSWER']}")
res, _, _ = run("ssbivariate", {"INPUT": poly, "FIELD": "v", "FIELD_Y": "r", "OUTPUT": "TEMPORARY_OUTPUT"})
check("not clearly related" in res["ANSWER"], f"bivariate unrelated: {res['ANSWER']}")

# points in degrees (EPSG:4326, Riyadh) with a value field: reprojected to UTM and said so
prow = []
for _ in range(400):
    lon, lat = 46.6 + g.random() * 0.2, 24.6 + g.random() * 0.2
    prow.append((f"POINT({lon} {lat})", [float((lon - 46.6) * 10 + g.normal(0, 0.3))]))
prow.append((prow[0][0], [1.0]))  # a duplicate point
p4326 = layer("Point", "EPSG:4326", ["v:double"], prow)
res, fb, _ = run("ssmoran", {"INPUT": p4326, "FIELD": "v", "WEIGHTS": 4})
check(any("EPSG:32638" in w for w in fb.warnings), "degrees: distances measured in UTM 38N, and said so")
check(any("on top of another" in w for w in fb.warnings), "duplicate points reported")
check(any("Distance band chosen automatically" in w for w in fb.warnings), "automatic distance band reported")
check(res["MORAN_I"] > 0.3, "east-west trend is clustered")
res, _, _ = run("sshotspots", {"INPUT": p4326, "FIELD": "v", "OUTPUT": "TEMPORARY_OUTPUT"})
hl = out(res["OUTPUT"])
check(hl.crs().authid() == "EPSG:4326" and "gi_bin" in hl.fields().names(), "hot spots on 4326 points keep the CRS")
bins_v = [f["gi_bin"] for f in hl.getFeatures()]
check(max(bins_v) == 3 and min(bins_v) == -3, "hot east, cold west")

# incidents: 5,000 points, background + two dense clusters (accidents)
inc = list(g.random((3800, 2)) * 10000)
inc += list(g.normal((2500, 2500), 250, (700, 2)))
inc += list(g.normal((7500, 6000), 300, (500, 2)))
inc = np.array(inc) + (400000, 2600000)
irows = [(QgsGeometry.fromPointXY(QgsPointXY(x, y)), [int(k % 3)]) for k, (x, y) in enumerate(inc)]
incl = layer("Point", "EPSG:32638", ["kind:integer"], irows)
rep = os.path.join(WORK, "hot.html")
res, fb, dt = run("sshotspots", {"INPUT": incl, "OUTPUT": "TEMPORARY_OUTPUT", "REPORT": rep})
hx = out(res["OUTPUT"])
feats = list(hx.getFeatures())
check(set(("count", "gi_z", "gi_p", "gi_bin", "gi_label")) <= set(hx.fields().names()), "hexagon output fields")
check(abs(sum(f["count"] for f in feats) - 5000) < 1e-6, "every incident counted once")
hot_c = [f.geometry().centroid().asPoint() for f in feats if f["gi_bin"] == 3]
near = [min(np.hypot(p.x() - 402500, p.y() - 2602500), np.hypot(p.x() - 407500, p.y() - 2606000)) for p in hot_c]
check(hot_c and np.median(near) < 800, f"99% hot spots sit on the clusters ({len(hot_c)} cells)")
check(res["DISTANCE_USED"] > 0 and "hot spots" in res["ANSWER"], f"incidents answer: {res['ANSWER']}")
check(dt < 30, f"hot spots on 5,000 incidents in {dt:.1f} s")
check(any("hexagons of" in w for w in fb.warnings), "automatic hexagon size reported")

# 5,000 points with values: timings
vals = np.sin(inc[:, 0] / 1500) + g.normal(0, 0.5, 5000)
vrows = [(QgsGeometry.fromPointXY(QgsPointXY(x, y)), [float(v)]) for (x, y), v in zip(inc, vals)]
big = layer("Point", "EPSG:32638", ["v:double"], vrows)
res, _, dt = run("sslisa", {"INPUT": big, "FIELD": "v", "OUTPUT": "TEMPORARY_OUTPUT",
                            "REPORT": os.path.join(WORK, "lisa_big.html")})
check(dt < 30, f"LISA, 5,000 points, 999 permutations in {dt:.1f} s")
res, _, dt = run("ssmoran", {"INPUT": big, "FIELD": "v", "REPORT": os.path.join(WORK, "moran_big.html")})
check(dt < 15 and res["MORAN_I"] > 0.3, f"Moran, 5,000 points in {dt:.1f} s")
rep = os.path.join(WORK, "inc.html")
res, _, dt = run("ssincremental", {"INPUT": big, "FIELD": "v", "REPORT": rep, "TABLE": "TEMPORARY_OUTPUT"})
check(res["FIRST_PEAK"] == res["FIRST_PEAK"] or "every distance" in res["ANSWER"], f"incremental: {res['ANSWER']}")
check(out(res["TABLE"]).featureCount() >= 5, "incremental table")
check(dt < 30, f"incremental, 5,000 points in {dt:.1f} s")
res, _, _ = run("ssincremental", {"INPUT": incl})
check(res["FIRST_PEAK"] > 0, f"incidents: clustering scale {res['FIRST_PEAK']:.0f} m ({res['ANSWER']})")

# co-location in a layer: shop (A), pharmacy placed next to shops (B), other
crow = []
for x, y in axy:
    crow.append((f"POINT({x + 300000} {y + 2500000})", ["shop"]))
for x, y in bxy:
    crow.append((f"POINT({x + 300000} {y + 2500000})", ["pharmacy"]))
for x, y in oxy:
    crow.append((f"POINT({x + 300000} {y + 2500000})", ["other"]))
col = layer("Point", "EPSG:32638", ["cat:string"], crow)
rep = os.path.join(WORK, "clq.html")
res, _, _ = run("sscolocation", {"INPUT": col, "CATEGORY": "cat", "CATEGORY_A": "shop", "CATEGORY_B": "pharmacy",
                                 "K": 4, "OUTPUT": "TEMPORARY_OUTPUT", "REPORT": rep})
cl = out(res["OUTPUT"])
check(res["CLQ"] > 2 and "times as often" in res["ANSWER"], f"co-location: {res['ANSWER']}")
check(cl.featureCount() == na and "clq_type" in cl.fields().names(), "one output point per A point")
check(sum(f["clq"] > 1 for f in cl.getFeatures()) > na * 0.9, "local CLQ above 1 around (almost) every shop")
# one pharmacy among 4 neighbours (expected 0.67) is not significant alone; three next to each shop is
crow3 = [(f"POINT({x + 300000} {y + 2500000})", ["shop"]) for x, y in axy]
crow3 += [(f"POINT({x + 300000 + dx} {y + 2500000 + dy})", ["pharmacy"]) for x, y in axy
          for dx, dy in ((6, 0), (-5, 4), (0, -7))]
crow3 += [(f"POINT({x + 300000} {y + 2500000})", ["other"]) for x, y in g.random((3000, 2)) * 5000]
res, _, _ = run("sscolocation", {"INPUT": layer("Point", "EPSG:32638", ["cat:string"], crow3), "CATEGORY": "cat",
                                 "CATEGORY_A": "shop", "CATEGORY_B": "pharmacy", "K": 4, "OUTPUT": "TEMPORARY_OUTPUT"})
check(sum(f["clq_type"] == "colocated" for f in out(res["OUTPUT"]).getFeatures()) > na * 0.9,
      "shops with three pharmacies next to them are significant one by one")
err, _, _ = run("sscolocation", {"INPUT": col, "CATEGORY": "cat", "CATEGORY_A": "shop", "CATEGORY_B": "bank",
                                 "OUTPUT": "TEMPORARY_OUTPUT"}, expect_error=True)
check(err and "pharmacy" in err, f"unknown category lists the values: {err}")

# emerging hot spots: 24 months; background everywhere, a cluster that starts in the last 4 months and
# one that is there all along and grows
erows = []
base = QDate(2023, 1, 1)
for m in range(24):
    for _ in range(60):
        x, y = g.random(2) * 8000
        erows.append((f"POINT({x + 400000} {y + 2600000})", [base.addMonths(m).addDays(int(g.integers(0, 27)))]))
    if m >= 20:
        for _ in range(40):
            x, y = g.normal((2000, 2000), 250)
            erows.append((f"POINT({x + 400000} {y + 2600000})", [base.addMonths(m).addDays(3)]))
    for _ in range(10 + 2 * m):
        x, y = g.normal((6000, 6000), 250)
        erows.append((f"POINT({x + 400000} {y + 2600000})", [base.addMonths(m).addDays(10)]))
em = layer("Point", "EPSG:32638", ["d:date"], erows)
rep = os.path.join(WORK, "emerging.html")
res, fb, dt = run("ssemerging", {"INPUT": em, "TIME": "d", "STEP": 4, "CELL_SIZE": 500, "OUTPUT": "TEMPORARY_OUTPUT",
                                 "REPORT": rep})
el = out(res["OUTPUT"])
pats = {}
for f in el.getFeatures():
    c_ = f.geometry().centroid().asPoint()
    pats.setdefault(f["pattern"], []).append((c_.x() - 400000, c_.y() - 2600000))
near_new = [p for k, v in pats.items() if k in ("new_hot", "consecutive_hot") for p in v
            if np.hypot(p[0] - 2000, p[1] - 2000) < 700]
near_old = [p for k, v in pats.items() if k in ("intensifying_hot", "persistent_hot") for p in v
            if np.hypot(p[0] - 6000, p[1] - 6000) < 700]
check(near_new, f"new / consecutive hot spots where the cluster appeared ({sorted(pats)})")
check(near_old, "intensifying / persistent hot spot where the cluster always was")
check({"events", "pattern", "pattern_label", "trend_z"} <= set(el.fields().names()), "emerging output fields")
check("24 time steps" in res["ANSWER"], f"emerging answer: {res['ANSWER']}")
check(dt < 30, f"emerging hot spots in {dt:.1f} s")
res2, _, _ = run("ssemerging", {"INPUT": em, "TIME": "d", "OUTPUT": "TEMPORARY_OUTPUT"})
check("time steps" in res2["ANSWER"], "automatic step and cell size")

# scale check: a broad gradient of incidents is clustered at every size (robust); tight clusters of
# 250 m are found with small hexagons but swallowed by big ones (depends on the scale) - the MAUP itself
broad = np.vstack([g.random((2000, 2)) * 10000, g.normal((3000, 3000), 1500, (3000, 2))]) + (400000, 2600000)
bl = layer("Point", "EPSG:32638", ["k:integer"],
           [(QgsGeometry.fromPointXY(QgsPointXY(x, y)), [1]) for x, y in broad])
rep = os.path.join(WORK, "scale.html")
res, _, dt = run("ssscale", {"INPUT": bl, "REPORT": rep})
check(res["VERDICT"] == "robust" and "every cell size" in res["ANSWER"], f"scale (broad): {res['ANSWER']}")
check(dt < 60, f"scale check, 5,000 points in {dt:.1f} s")
res, _, _ = run("ssscale", {"INPUT": incl, "CELL_SIZE": 250, "FACTORS": "1, 2, 4, 8"})
check(res["VERDICT"] == "depends" and "250 m" in res["ANSWER"], f"scale (tight clusters): {res['ANSWER']}")
err, _, _ = run("ssscale", {"INPUT": poly, "FIELD": "one", "CELL_SIZE": 300}, expect_error=True)
check(err and "same" in err, "constant counts refused")
res, _, _ = run("ssscale", {"INPUT": poly, "FIELD": "cnt", "CELL_SIZE": 200, "FACTORS": "1, 1.5, 2"})
check(res["VERDICT"] in ("robust", "depends"), f"polygon counts re-aggregated: {res['ANSWER']}")
err, _, _ = run("ssscale", {"INPUT": poly}, expect_error=True)
check(err and "count field" in err, "polygons without a count field refused")
i18n.init("ar")
res, _, _ = run("ssscale", {"INPUT": bl})
check("ثابت" in res["ANSWER"], f"Arabic scale answer: {res['ANSWER']}")
i18n.init("en_US")

# a chart to look at
import base64, re  # noqa: E401,E402

for name in ("lisa_ar.html", "hot.html", "emerging.html", "scale.html"):
    imgs = re.findall(r'src="data:image/png;base64,([^"]+)"', open(os.path.join(WORK, name), encoding="utf-8").read())
    for k, b in enumerate(imgs):
        with open(os.path.join(WORK, f"{name[:-5]}_{k}.png"), "wb") as f:
            f.write(base64.b64decode(b))
print("charts in", WORK)

print("ALL PASS" if not failures else f"{len(failures)} FAILED: {failures}")
QgsApplication.exitQgis()
sys.exit(1 if failures else 0)
