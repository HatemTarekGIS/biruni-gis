r"""Spatial statistics - regression: OLS with spatial diagnostics (biruni:ssols) and geographically
weighted regression (biruni:ssgwr).

Run:  engine\bin\python-qgis.bat tests\test_spatial_stats_regression.py

OLS is checked against numpy.linalg.lstsq and a textbook example (Anscombe's quartet I); GWR against
synthetic data whose true coefficients vary smoothly in space (and against data where they do not).
"""
import math
import os
import sys
import tempfile
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "app", "plugins"))

import numpy as np  # noqa: E402
from qgis.core import (QgsApplication, QgsFeature, QgsField, QgsGeometry, QgsProcessingException,  # noqa: E402
                       QgsProject, QgsVectorLayer)
from qgis.PyQt.QtCore import QMetaType  # noqa: E402

app = QgsApplication([], False)
app.initQgis()
sys.path.append(QgsApplication.prefixPath() + "/python/plugins")
import processing  # noqa: E402
from processing.core.Processing import Processing  # noqa: E402

Processing.initialize()
from biruni import i18n  # noqa: E402
from biruni.algorithms import BiruniProvider  # noqa: E402
from biruni.algorithms import ss_regression as alg  # noqa: E402
from biruni.spatial_stats import gwr as G  # noqa: E402
from biruni.spatial_stats import regression as R  # noqa: E402

provider = BiruniProvider()
QgsApplication.processingRegistry().addProvider(provider)
failures = []
OUT = tempfile.mkdtemp(prefix="ss_regression_")


def check(cond, msg):
    print(("PASS " if cond else "FAIL ") + msg)
    if not cond:
        failures.append(msg)


rng = np.random.default_rng(7)

# ---- OLS against lstsq -------------------------------------------------------------------------------
n = 200
X = rng.normal(size=(n, 3))
y = 2 + X @ np.array([1.5, -0.7, 0.0]) + rng.normal(scale=0.8, size=n)
res = R.ols(y, X, ["a", "b", "c"])
ref, rss_ref, *_ = np.linalg.lstsq(np.column_stack([np.ones(n), X]), y, rcond=None)
check(np.allclose(res["beta"], ref, atol=1e-10), "OLS coefficients equal numpy.linalg.lstsq")
check(abs(res["rss"] - float(rss_ref[0])) < 1e-8, "OLS residual sum of squares equals lstsq")
check(res["p"][1] < 0.001 and res["p"][3] > 0.01, "a strong variable is significant, a useless one is not")
check(all(v < 2 for v in res["vif"]), f"independent variables: VIF near 1 ({np.round(res['vif'], 2)})")
from scipy import stats  # noqa: E402

jb_ref = stats.jarque_bera(res["resid"])
check(abs(res["jb"] - jb_ref.statistic) < 1e-8, "Jarque-Bera equals scipy.stats.jarque_bera")
# HC1 = HC0 * n / (n - k)
Xc = np.column_stack([np.ones(n), X])
inv = np.linalg.inv(Xc.T @ Xc)
hc0 = inv @ (Xc.T * res["resid"] ** 2) @ Xc @ inv
check(np.allclose(res["se_robust"], np.sqrt(np.diag(hc0) * n / (n - 4))), "robust SE is White HC1")

# ---- textbook: Anscombe's quartet, data set I ------------------------------------------------------------
ax = [10, 8, 13, 9, 11, 14, 6, 4, 12, 7, 5]
ay = [8.04, 6.95, 7.58, 8.81, 8.33, 9.96, 7.24, 4.26, 10.84, 4.82, 5.68]
an = R.ols(ay, np.array(ax, float)[:, None], ["x"])
check(abs(an["beta"][0] - 3.0001) < 1e-3 and abs(an["beta"][1] - 0.5001) < 1e-3,
      f"Anscombe I: y = 3.00 + 0.500 x ({an['beta'][0]:.4f}, {an['beta'][1]:.4f})")
check(abs(an["r2"] - 0.6665) < 1e-3, f"Anscombe I: R² = 0.667 ({an['r2']:.4f})")
check(abs(an["se"][1] - 0.1179) < 1e-3 and abs(an["t"][1] - 4.24) < 0.01 and abs(an["p"][1] - 0.00217) < 1e-4,
      f"Anscombe I: slope SE 0.118, t 4.24, p 0.0022 ({an['se'][1]:.4f}, {an['t'][1]:.3f}, {an['p'][1]:.5f})")

# ---- VIF, Koenker-Bassett ------------------------------------------------------------------------------
x1 = rng.normal(size=n)
x2 = x1 + rng.normal(scale=0.1, size=n)
r2 = R.ols(x1 + x2 + rng.normal(size=n), np.column_stack([x1, x2]), ["x1", "x2"])
check(min(r2["vif"]) > R.VIF_LIMIT, f"near-copies of a variable: VIF > 7.5 ({np.round(r2['vif'], 1)})")
check(any("VIF" in w for w in R.warnings_list(r2, "en")), "multicollinearity is warned about")
xh = rng.uniform(0, 10, n)
het = R.ols(1 + xh + rng.normal(size=n) * xh, xh[:, None])
hom = R.ols(1 + xh + rng.normal(size=n), xh[:, None])
check(het["kb_p"] < 0.01 and hom["kb_p"] > 0.05,
      f"Koenker-Bassett finds changing spread ({het['kb_p']:.4f}) and not constant spread ({hom['kb_p']:.3f})")

# ---- Moran's I of residuals -----------------------------------------------------------------------------
side = 20
gu, gv = np.meshgrid(np.arange(side) * 100.0, np.arange(side) * 100.0)
coords = np.column_stack([gu.ravel(), gv.ravel()])
m = len(coords)
xa = rng.normal(size=m)
hidden = np.sin(coords[:, 0] / 600) + np.cos(coords[:, 1] / 500)  # a smooth variable that is left out
ym = 1 + 2 * xa + 1.5 * hidden + rng.normal(scale=0.3, size=m)
wk = R.knn_weights(coords, 8)
mi = R.morans_i(R.ols(ym, xa[:, None])["resid"], wk, 499)
yr = 1 + 2 * xa + rng.normal(scale=0.3, size=m)
mr = R.morans_i(R.ols(yr, xa[:, None])["resid"], wk, 499)
check(mi["I"] > 0.3 and mi["p"] < 0.01, f"left-out smooth variable: residuals clustered (I={mi['I']:.2f})")
check(mr["p"] > 0.05, f"complete model: residuals not clustered (I={mr['I']:.3f}, p={mr['p']:.2f})")
wb, isl = R.band_weights(coords, R.default_band(coords))
check(isl == 0 and abs(wb.sum() - m) < 1e-6, "distance band at the default distance: everyone has neighbours")
text = R.answer_text(R.ols(ym, xa[:, None], ["xa"]), xa[:, None], "y", mi, "en")
check("GWR" in text and "explains" in text, "clustered residuals: the answer suggests GWR")
text_ar = R.answer_text(R.ols(ym, xa[:, None], ["xa"]), xa[:, None], "y", mi, "ar")
check("GWR" in text_ar and "يفسّر" in text_ar, "the answer is written in Arabic too")

# ---- GWR: smoothly varying coefficients -----------------------------------------------------------------
side = 25
gu, gv = np.meshgrid(np.arange(side) * 100.0, np.arange(side) * 100.0)
u, v = gu.ravel(), gv.ravel()
ng = len(u)
z1, z2 = rng.normal(size=ng), rng.normal(size=ng)
b1 = 1 + 4 * (u + v) / (u.max() + v.max())
b2 = 2 * np.sin(np.pi * u / u.max())
yg = 3 + b1 * z1 + b2 * z2 + rng.normal(scale=0.5, size=ng)
cg = np.column_stack([u, v])
for kernel in G.KERNEL_IDS:
    model = G.GWR(cg, yg, np.column_stack([z1, z2]), ["z1", "z2"], kernel)
    fit = model.fit()
    c1 = np.corrcoef(fit["beta"][:, 1], b1)[0, 1]
    c2 = np.corrcoef(fit["beta"][:, 2], b2)[0, 1]
    check(c1 > 0.9 and c2 > 0.9, f"GWR {kernel}: recovered surfaces correlate with the truth ({c1:.3f}, {c2:.3f})")
cmp = G.compare_with_ols(fit, yg, np.column_stack([z1, z2]), ["z1", "z2"])
check(cmp["aicc_drop"] > 3 and cmp["f_p"] < 0.01, f"varying coefficients: GWR beats OLS (AICc drop "
      f"{cmp['aicc_drop']:.0f}, F p={cmp['f_p']:.2g})")
check(1 < fit["enp"] < ng and fit["t_crit"] > 1.96, f"Silva-Fotheringham critical t above 1.96 "
      f"({fit['t_crit']:.2f}, ENP {fit['enp']:.1f})")
check(np.all(fit["cond"] >= 1) and np.all(np.isfinite(fit["local_r2"])), "condition numbers and local R² computed")
# constant coefficients -> large bandwidth
yc = 3 + 2 * z1 - z2 + rng.normal(scale=0.5, size=ng)
mc = G.GWR(cg, yc, np.column_stack([z1, z2]), ["z1", "z2"])
fc = mc.fit()
cc = G.compare_with_ols(fc, yc, np.column_stack([z1, z2]))
check(fc["bandwidth"] >= 0.5 * ng, f"constant coefficients: a large bandwidth ({fc['bandwidth']:.0f} of {ng})")
check(cc["aicc_drop"] < 3, f"constant coefficients: GWR does not beat OLS by 3 AICc ({cc['aicc_drop']:.2f})")
# a global-size fixed Gaussian kernel reproduces OLS
mg = G.GWR(cg, yg, np.column_stack([z1, z2]), kernel=G.FIXED_GAUSSIAN)
fg = mg.fit(1e9)
check(np.allclose(fg["beta"], np.tile(cmp["ols"]["beta"], (ng, 1)), atol=1e-6), "an infinite bandwidth gives OLS")
# speed: ~3,000 features
side = 55
gu, gv = np.meshgrid(np.arange(side) * 50.0, np.arange(side) * 50.0)
cb = np.column_stack([gu.ravel(), gv.ravel()])
nb_ = len(cb)
w1, w2 = rng.normal(size=nb_), rng.normal(size=nb_)
yb = 1 + (1 + cb[:, 0] / cb[:, 0].max()) * w1 + w2 + rng.normal(scale=0.5, size=nb_)
t0 = time.time()
fb = G.GWR(cb, yb, np.column_stack([w1, w2])).fit()
dt = time.time() - t0
check(dt < 60, f"GWR with {nb_} features, bandwidth search included: {dt:.1f} s")

# ---- through Processing ----------------------------------------------------------------------------------


def grid_layer(values, crs="EPSG:32637", size=100.0, origin=(500000.0, 2700000.0), ncol=25, geographic=False):
    """Square polygons on a grid with numeric fields."""
    lyr = QgsVectorLayer(f"Polygon?crs={'EPSG:4326' if geographic else crs}", "grid", "memory")
    names = list(values)
    lyr.dataProvider().addAttributes([QgsField(nm, QMetaType.Type.Double) for nm in names])
    lyr.updateFields()
    feats = []
    count = len(values[names[0]])
    for i in range(count):
        c, r = i % ncol, i // ncol
        if geographic:
            x0, y0, s = 46.0 + c * 0.001, 24.0 + r * 0.001, 0.001
        else:
            x0, y0, s = origin[0] + c * size, origin[1] + r * size, size
        f = QgsFeature(lyr.fields())
        f.setGeometry(QgsGeometry.fromWkt(f"POLYGON(({x0} {y0},{x0+s} {y0},{x0+s} {y0+s},{x0} {y0+s},{x0} {y0}))"))
        f.setAttributes([None if values[nm][i] is None else float(values[nm][i]) for nm in names])
        feats.append(f)
    lyr.dataProvider().addFeatures(feats)
    QgsProject.instance().addMapLayer(lyr)
    return lyr


smooth = np.sin(u / 600) + np.cos(v / 500)  # a smooth variable the model will not be given
vals = {"crashes": list(yg), "pop": list(z1), "income": list(z2), "flat": [5.0] * ng,
        "hidden_y": list(3 + 2 * z1 + 1.5 * smooth + rng.normal(scale=0.3, size=ng))}
vals["crashes"][3] = None
lyr = grid_layer(vals, geographic=True)
rep = os.path.join(OUT, "ols.html")
out = processing.run("biruni:ssols", {"INPUT": lyr, "DEPENDENT": "crashes", "EXPLANATORY": ["pop", "income"],
                                       "OUTPUT": "memory:", "REPORT": rep})
ol = out["OUTPUT"]
check(ol.featureCount() == ng, "OLS output keeps every feature")
check(all(ol.fields().indexOf(f) >= 0 for f in ("ols_fit", "ols_resid", "ols_stdres")), "OLS adds residual fields")
miss = [f for f in ol.getFeatures() if f["crashes"] is None or (hasattr(f["crashes"], "isNull") and
                                                                  f["crashes"].isNull())]
check(len(miss) == 1 and (miss[0]["ols_resid"] is None or str(miss[0]["ols_resid"]) == "NULL"),
      "a feature with a missing value gets no residual")
check("explains" in out["ANSWER"] and 0 < out["R2"] < 1, f"OLS answer: {out['ANSWER'][:120]}…")
oh = processing.run("biruni:ssols", {"INPUT": lyr, "DEPENDENT": "hidden_y", "EXPLANATORY": ["pop"],
                                      "OUTPUT": "memory:"})
check(oh["MORAN_P"] < 0.01 and "GWR" in oh["ANSWER"],
      f"a left-out smooth variable leaves clustered OLS residuals (I={oh['MORAN_I']:.2f}, p={oh['MORAN_P']:.3f})")
with open(rep, encoding="utf-8") as fh:
    html = fh.read()
check("data:image/png;base64" in html and html.count("<img") == 3 and "Jarque" in html, "OLS report with 3 charts")
alg.style_ols(ol)
check(ol.renderer().type() == "graduatedSymbol" and len(ol.renderer().ranges()) == 7, "residual map styled")
for mode in (1, 2):
    o2 = processing.run("biruni:ssols", {"INPUT": lyr, "DEPENDENT": "crashes", "EXPLANATORY": ["pop"],
                                         "NEIGHBOURHOOD": mode, "OUTPUT": "memory:"})
    check(math.isfinite(o2["MORAN_I"]), f"neighbourhood option {mode} gives a Moran's I ({o2['MORAN_I']:.3f})")

try:
    processing.run("biruni:ssols", {"INPUT": lyr, "DEPENDENT": "crashes", "EXPLANATORY": ["flat"],
                                    "OUTPUT": "memory:"})
    check(False, "a constant explanatory variable is refused")
except QgsProcessingException as e:
    check("same value" in str(e), "a constant explanatory variable is refused: " + str(e))
small = grid_layer({"a": [1, 2, 3, 4, 5], "b": [2, 1, 4, 3, 6]})
try:
    processing.run("biruni:ssols", {"INPUT": small, "DEPENDENT": "a", "EXPLANATORY": ["b"], "OUTPUT": "memory:"})
    check(False, "five features are refused")
except QgsProcessingException as e:
    check("features" in str(e), "five features are refused: " + str(e))

rep2 = os.path.join(OUT, "gwr.html")
lyr2 = grid_layer({"crashes": list(yg), "pop people": list(z1), "income": list(z2)})
out = processing.run("biruni:ssgwr", {"INPUT": lyr2, "DEPENDENT": "crashes", "EXPLANATORY": ["pop people", "income"],
                                       "OUTPUT": "memory:", "REPORT": rep2})
gl = out["OUTPUT"]
need = ["gwr_fit", "gwr_r2", "gwr_cond", "b_intercept", "b_pop_people", "t_income", "se_income", "sig_income"]
check(all(gl.fields().indexOf(f) >= 0 for f in need), "GWR fields per feature (b_, se_, t_, sig_, local R², cond)")
bvals = np.array([f["b_pop_people"] for f in gl.getFeatures()], dtype=float)
check(np.corrcoef(bvals, b1)[0, 1] > 0.9, "GWR via Processing recovers the varying coefficient")
check(out["AICC"] < out["AICC_OLS"] - 3, f"GWR AICc {out['AICC']:.0f} < OLS {out['AICC_OLS']:.0f}")
names = alg.style_gwr(gl, ["intercept", "pop_people", "income"])
sm = gl.styleManager()
check(len(names) == 3 and set(names) <= set(sm.styles()) and len(sm.styles()) == 6,
      f"one map style per coefficient + R², condition, residual ({sm.styles()})")
r_ = gl.renderer()
check(r_.type() == "graduatedSymbol" and r_.classAttribute() == "b_pop_people", "first explanatory map is shown")
ranges = r_.ranges()  # keep the copies alive while their symbols are read
sym = ranges[0].symbol()
check(sym.symbolLayerCount() == 2 and sym.symbolLayer(1).layerType() == "LinePatternFill",
      "non-significant coefficients are hatched")
with open(rep2, encoding="utf-8") as fh:
    html2 = fh.read()
check("ANOVA" in html2 and html2.count("<img") == 2, "GWR report: ANOVA table and 2 charts")
o3 = processing.run("biruni:ssgwr", {"INPUT": lyr2, "DEPENDENT": "crashes", "EXPLANATORY": ["income"],
                                      "KERNEL": 1, "BW_MODE": 1, "BANDWIDTH": 400, "OUTPUT": "memory:"})
check(abs(o3["BANDWIDTH_USED"] - 400) < 1e-6, "a bandwidth given by the user is used")

# the charts, for a look
from biruni.spatial_stats.common import png  # noqa: E402,F401

with open(os.path.join(OUT, "ols_obs_pred.png"), "wb") as fh:
    fh.write(R.chart_observed_predicted(yg, cmp["ols"]["fitted"], "crashes", "ar"))
with open(os.path.join(OUT, "gwr_coefs.png"), "wb") as fh:
    fh.write(G.chart_coefficients(cg, fit, "en"))
print("charts in", OUT)

i18n._lang = "ar"  # the whole run and both reports in Arabic
rep_ar = os.path.join(OUT, "ols_ar.html")
oa = processing.run("biruni:ssols", {"INPUT": lyr, "DEPENDENT": "hidden_y", "EXPLANATORY": ["pop"],
                                      "OUTPUT": "memory:", "REPORT": rep_ar})
with open(rep_ar, encoding="utf-8") as fh:
    check('dir="rtl"' in fh.read() and "يفسّر" in oa["ANSWER"], "Arabic OLS answer and right-to-left report")
og = processing.run("biruni:ssgwr", {"INPUT": lyr2, "DEPENDENT": "crashes", "EXPLANATORY": ["income"],
                                      "OUTPUT": "memory:", "REPORT": os.path.join(OUT, "gwr_ar.html")})
check("AICc" in og["ANSWER"] and "المعامل" in og["ANSWER"], "Arabic GWR answer")
i18n._lang = "en"
print("ALL PASS" if not failures else f"{len(failures)} FAILED")
sys.exit(1 if failures else 0)
