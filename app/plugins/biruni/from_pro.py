"""«Coming from ArcGIS Pro?»: one window for someone weighing the move.

- What you save: a yearly cost sheet (licences, extensions, field-worker user types, credits) with editable prices -
  the defaults are rough list prices, the user types in their own invoice.
- Speed, measured here on this machine: how long Biruni took to start (process creation -> ready, recorded at every
  start) and a small timed benchmark (20,000 points: buffer, dissolve, spatial join). Nothing about Pro is claimed:
  the user can type the time they see in Pro next to ours.
- What you do not lose: the migration tools (readiness scan, arcpy scripts, writing back to Pro formats,
  company services).
- What Pro does not have: features of Biruni, each card opens the feature.
"""
import ctypes
import random
import time

from qgis.core import QgsFeature, QgsGeometry, QgsPointXY, QgsSettings, QgsVectorLayer
from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtWidgets import (QDialog, QDoubleSpinBox, QFrame, QGridLayout, QHBoxLayout, QLabel, QPushButton,
                                 QScrollArea, QSpinBox, QTabWidget, QVBoxLayout, QWidget)

from .i18n import is_rtl, tr
from .version import APP_NAME

_START_KEY = "biruni/from-pro/start-seconds"
_PRO_KEY = "biruni/from-pro/pro-start-seconds"
_COST_KEY = "biruni/from-pro/costs"

# (key, label, default count, default yearly price in USD) - rough list prices, meant to be overwritten
COST_ROWS = [
    ("pro", "ArcGIS Pro licences (per user, per year)", 1, 2750.0),
    ("ext", "Extensions (Spatial, 3D, Network, ... each, per year)", 1, 1500.0),
    ("field", "Field / mobile user types (per user, per year)", 0, 350.0),
    ("credits", "Credits (thousands per year: geocoding, routing, storage)", 0, 120.0),
    ("other", "Other (training, maintenance, servers ...)", 0, 0.0),
]


# ---- start-up time -------------------------------------------------------------------------------
def process_age_seconds():
    """Seconds since this process was created (Windows), or None."""
    try:
        class FT(ctypes.Structure):
            _fields_ = [("lo", ctypes.c_ulong), ("hi", ctypes.c_ulong)]

        k = ctypes.windll.kernel32
        k.GetCurrentProcess.restype = ctypes.c_void_p
        k.GetProcessTimes.argtypes = [ctypes.c_void_p] + [ctypes.POINTER(FT)] * 4
        created, ex, kern, user = FT(), FT(), FT(), FT()
        if not k.GetProcessTimes(k.GetCurrentProcess(), ctypes.byref(created), ctypes.byref(ex),
                                 ctypes.byref(kern), ctypes.byref(user)):
            return None
        now = FT()
        k.GetSystemTimeAsFileTime(ctypes.byref(now))
        a = (created.hi << 32) | created.lo
        b = (now.hi << 32) | now.lo
        return max(0.0, (b - a) / 1e7)
    except Exception:  # noqa: BLE001 - not Windows
        return None


def record_start():
    """Called once the app is ready: keep how long this start took."""
    age = process_age_seconds()
    if age is not None:
        QgsSettings().setValue(_START_KEY, round(age, 2))
    return age


def last_start():
    v = QgsSettings().value(_START_KEY, None)
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


# ---- benchmark -----------------------------------------------------------------------------------
def benchmark(n=20000, seed=7):
    """Time a few everyday operations on generated data. Returns [(name, seconds, detail)]."""
    from processing.core.Processing import Processing

    Processing.initialize()
    from qgis import processing as qp

    rnd = random.Random(seed)
    out = []
    t = time.perf_counter()
    pts = QgsVectorLayer("Point?crs=EPSG:3857&field=id:integer&field=cls:integer", "pts", "memory")
    feats = []
    for i in range(n):
        f = QgsFeature(pts.fields())
        f.setAttributes([i, i % 12])
        f.setGeometry(QgsGeometry.fromPointXY(QgsPointXY(rnd.uniform(0, 50000), rnd.uniform(0, 50000))))
        feats.append(f)
    pts.dataProvider().addFeatures(feats)
    out.append(("create", time.perf_counter() - t, tr("{n:,} points made").format(n=n)))

    t = time.perf_counter()
    buf = qp.run("native:buffer", {"INPUT": pts, "DISTANCE": 150, "SEGMENTS": 8, "OUTPUT": "memory:"})["OUTPUT"]
    out.append(("buffer", time.perf_counter() - t, tr("{n:,} buffers").format(n=buf.featureCount())))

    t = time.perf_counter()
    dis = qp.run("native:dissolve", {"INPUT": buf, "FIELD": ["cls"], "OUTPUT": "memory:"})["OUTPUT"]
    out.append(("dissolve", time.perf_counter() - t, tr("{n:,} areas").format(n=dis.featureCount())))

    t = time.perf_counter()
    grid = qp.run("native:creategrid", {"TYPE": 2, "EXTENT": "0,50000,0,50000 [EPSG:3857]", "HSPACING": 2500,
                                        "VSPACING": 2500, "CRS": "EPSG:3857", "OUTPUT": "memory:"})["OUTPUT"]
    cnt = qp.run("native:countpointsinpolygon", {"POLYGONS": grid, "POINTS": pts, "FIELD": "n",
                                                 "OUTPUT": "memory:"})["OUTPUT"]
    out.append(("join", time.perf_counter() - t, tr("{n:,} cells counted").format(n=cnt.featureCount())))
    return out


BENCH_NAMES = {
    "create": "Make 20,000 points",
    "buffer": "Buffer every point",
    "dissolve": "Dissolve the buffers by class",
    "join": "Count points in a grid (spatial join)",
}


# ---- the window ----------------------------------------------------------------------------------
def _qss(t):
    return f"""
QDialog#FromPro {{ background: {t['start_bg']}; }}
QDialog#FromPro QLabel {{ color: {t['text']}; background: transparent; }}
#FPTitle {{ font-size: 17pt; font-weight: 700; }}
#FPDim {{ color: {t['text_dim']}; }}
#FPBig {{ font-size: 22pt; font-weight: 700; color: {t['accent']}; }}
QFrame#FPCard {{ background: {t['card']}; border: 1px solid {t['border']}; border-radius: 10px; }}
QFrame#FPCard:hover {{ border: 1px solid {t['accent']}; background: {t['card_hover']}; }}
#FPCardTitle {{ font-weight: 600; }}
"""


class _Card(QFrame):
    def __init__(self, title, text, on_click, tokens):
        super().__init__()
        self.setObjectName("FPCard")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self._on_click = on_click
        v = QVBoxLayout(self)
        v.setContentsMargins(14, 12, 14, 12)
        a = QLabel(title)
        a.setObjectName("FPCardTitle")
        a.setWordWrap(True)
        b = QLabel(text)
        b.setObjectName("FPDim")
        b.setWordWrap(True)
        v.addWidget(a)
        v.addWidget(b)
        v.addStretch(1)

    def mouseReleaseEvent(self, ev):
        if ev.button() == Qt.MouseButton.LeftButton and self.rect().contains(ev.position().toPoint()):
            self._on_click()
        super().mouseReleaseEvent(ev)


# (command id, title, text) - cards whose command is missing are left out
KEEP_CARDS = [
    ("migrate_scan", "Readiness scan",
     "Point it at your folders: every .mxd, .aprx, arcpy script and model is checked and you get a report of what "
     "opens 100%, what changes and what is missing."),
    ("open_arcgis", "Open your Pro and ArcMap projects",
     "Layers, groups, symbols, labels and filters of .aprx, .mapx, .lyrx and .mxd files."),
    ("run_arcpy", "Run your arcpy scripts",
     "Your existing scripts run here without a licence; anything not supported yet is named clearly."),
    ("import_models", "Bring your ModelBuilder models",
     "Models in .atbx toolboxes become models in the toolbox."),
    ("export_aprx", "Go back to Pro any time",
     "Write .aprx, .mapx and .lyrx with symbols and labels, so half the team can stay on Pro."),
    ("arcgis_services", "Your company's ArcGIS services",
     "Connect to ArcGIS Server, Portal or Online, edit feature services, or take them offline and sync back."),
]
EXTRA_CARDS = [
    ("history_versions", "Time machine",
     "Every save keeps a version of the project; go back to any of them."),
    ("lock_database", "Locked vault",
     "Lock a database with a password and give each user their own rights per layer, offline."),
    ("activity_report", "Who did what, and for how long",
     "Sessions, real working time and edits per layer, as an Excel report."),
    ("field_photos", "Field photos from any phone",
     "Scan a code with the phone: photos and answers arrive on your map, no cloud account."),
    ("live_share", "Share your map live",
     "Someone opens a link on their phone and sees your map as you work."),
    ("seal_files", "Seal your deliverables",
     "A signature that shows if anyone changed a delivered file."),
    ("data_charter", "Data charter",
     "Fields, value lists and rules for your data in one place."),
    ("office_tools", "Engineering office tools",
     "Deeds to parcels, setbacks, quantities, delivery checks and more."),
    ("arabic_labels_fix", "Right-to-left labels done right",
     "Arabic, Persian, Urdu and more, also on curved lines."),
    ("light_mode", "Light mode",
     "Runs well on an old laptop: less memory, faster start."),
]


class FromProDialog(QDialog):
    def __init__(self, plugin):
        super().__init__(plugin.mw)
        self.plugin = plugin
        self.setObjectName("FromPro")
        self.setWindowTitle(tr("Coming from ArcGIS Pro?"))
        self.setLayoutDirection(Qt.LayoutDirection.RightToLeft if is_rtl() else Qt.LayoutDirection.LeftToRight)
        self.resize(900, 640)
        t = plugin.theme.tokens()
        self.setStyleSheet(_qss(t))
        self.tokens = t
        v = QVBoxLayout(self)
        title = QLabel(tr("Coming from ArcGIS Pro?"))
        title.setObjectName("FPTitle")
        sub = QLabel(tr("{app} is free for ever: no sign-in, no licence that runs out, no credits, and it works "
                        "without the internet. Here is what you save, how fast it is on this computer, and how "
                        "you keep everything you made in Pro.").format(app=APP_NAME))
        sub.setObjectName("FPDim")
        sub.setWordWrap(True)
        v.addWidget(title)
        v.addWidget(sub)
        tabs = QTabWidget()
        tabs.addTab(self._keep_tab(), tr("Keep your work"))
        tabs.addTab(self._cost_tab(), tr("What you save"))
        tabs.addTab(self._speed_tab(), tr("Speed"))
        tabs.addTab(self._cards_tab(EXTRA_CARDS, tr("Things you will not find in Pro:")), tr("Only here"))
        self.tabs = tabs
        v.addWidget(tabs, 1)
        close = QPushButton(tr("Close"))
        close.clicked.connect(self.accept)
        row = QHBoxLayout()
        row.addStretch(1)
        row.addWidget(close)
        v.addLayout(row)

    # -- cards
    def _run(self, cid):
        from .commands import CATALOG

        cmd = CATALOG.get(cid)
        if cmd is not None:
            self.plugin.runner.execute(cmd)

    def _cards_tab(self, specs, intro):
        from .commands import CATALOG

        w = QWidget()
        outer = QVBoxLayout(w)
        lab = QLabel(intro)
        lab.setObjectName("FPDim")
        lab.setWordWrap(True)
        outer.addWidget(lab)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        host = QWidget()
        grid = QGridLayout(host)
        grid.setSpacing(10)
        n = 0
        self.card_ids = getattr(self, "card_ids", [])
        for cid, title, text in specs:
            if cid not in CATALOG:
                continue
            grid.addWidget(_Card(tr(title), tr(text), lambda c=cid: self._run(c), self.tokens), n // 2, n % 2)
            self.card_ids.append(cid)
            n += 1
        grid.setRowStretch(n // 2 + 1, 1)
        scroll.setWidget(host)
        outer.addWidget(scroll, 1)
        return w

    def _keep_tab(self):
        return self._cards_tab(KEEP_CARDS, tr("Moving does not mean losing anything - and you can always go back:"))

    # -- costs
    def _cost_tab(self):
        import json

        w = QWidget()
        g = QGridLayout(w)
        note = QLabel(tr("Type the numbers from your own invoice - the prices filled in are only rough list "
                         "prices in US dollars."))
        note.setObjectName("FPDim")
        note.setWordWrap(True)
        g.addWidget(note, 0, 0, 1, 4)
        g.addWidget(QLabel(tr("How many")), 1, 1)
        g.addWidget(QLabel(tr("Price each, per year")), 1, 2)
        try:
            saved = json.loads(QgsSettings().value(_COST_KEY, "{}") or "{}")
        except ValueError:
            saved = {}
        self.cost_inputs = {}
        for r, (key, label, count, price) in enumerate(COST_ROWS, start=2):
            c0, p0 = (saved.get(key) or [count, price])[:2]
            g.addWidget(QLabel(tr(label)), r, 0)
            cn = QSpinBox()
            cn.setRange(0, 100000)
            cn.setValue(int(c0))
            pr = QDoubleSpinBox()
            pr.setRange(0, 1e7)
            pr.setDecimals(0)
            pr.setSingleStep(50)
            pr.setValue(float(p0))
            cn.valueChanged.connect(self._update_cost)
            pr.valueChanged.connect(self._update_cost)
            g.addWidget(cn, r, 1)
            g.addWidget(pr, r, 2)
            self.cost_inputs[key] = (cn, pr)
        r = len(COST_ROWS) + 2
        self.cost_year = QLabel()
        self.cost_year.setObjectName("FPBig")
        self.cost_more = QLabel()
        self.cost_more.setObjectName("FPDim")
        self.cost_more.setWordWrap(True)
        g.addWidget(self.cost_year, r, 0, 1, 4)
        g.addWidget(self.cost_more, r + 1, 0, 1, 4)
        g.setRowStretch(r + 2, 1)
        g.setColumnStretch(0, 1)
        self._update_cost()
        return w

    def yearly_cost(self):
        return sum(cn.value() * pr.value() for cn, pr in self.cost_inputs.values())

    def _update_cost(self, *_):
        import json

        total = self.yearly_cost()
        self.cost_year.setText(tr("You save about ${n:,.0f} a year").format(n=total))
        self.cost_more.setText(tr("${a:,.0f} over 3 years, ${b:,.0f} over 5 years - and {app} costs $0.").format(
            a=total * 3, b=total * 5, app=APP_NAME))
        QgsSettings().setValue(_COST_KEY, json.dumps({k: [cn.value(), pr.value()]
                                                      for k, (cn, pr) in self.cost_inputs.items()}))

    # -- speed
    def _speed_tab(self):
        from . import performance

        w = QWidget()
        g = QGridLayout(w)
        st = last_start()
        g.addWidget(QLabel(tr("Start-up on this computer (from launch to ready):")), 0, 0)
        self.start_label = QLabel(tr("{s:.1f} seconds").format(s=st) if st is not None else tr("not measured yet"))
        self.start_label.setObjectName("FPBig")
        g.addWidget(self.start_label, 0, 1)
        ws, _priv = performance.process_memory_mb()
        g.addWidget(QLabel(tr("Memory in use right now:")), 1, 0)
        mem = QLabel(tr("{m:,.0f} MB").format(m=ws) if ws else "-")
        mem.setObjectName("FPBig")
        g.addWidget(mem, 1, 1)
        g.addWidget(QLabel(tr("Your ArcGIS Pro start-up time (seconds, measure it with a stopwatch):")), 2, 0)
        self.pro_start = QDoubleSpinBox()
        self.pro_start.setRange(0, 1000)
        self.pro_start.setDecimals(0)
        try:
            self.pro_start.setValue(float(QgsSettings().value(_PRO_KEY, 0) or 0))
        except (TypeError, ValueError):
            pass
        self.pro_start.valueChanged.connect(self._update_compare)
        g.addWidget(self.pro_start, 2, 1)
        self.compare = QLabel()
        self.compare.setObjectName("FPDim")
        self.compare.setWordWrap(True)
        g.addWidget(self.compare, 3, 0, 1, 2)
        sep = QLabel(tr("A short test with generated data, timed here:"))
        sep.setObjectName("FPDim")
        g.addWidget(sep, 4, 0, 1, 2)
        self.bench_btn = QPushButton(tr("Run the speed test"))
        self.bench_btn.clicked.connect(self.run_benchmark)
        g.addWidget(self.bench_btn, 5, 0)
        self.bench_label = QLabel()
        self.bench_label.setWordWrap(True)
        self.bench_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        g.addWidget(self.bench_label, 6, 0, 1, 2)
        g.setRowStretch(7, 1)
        g.setColumnStretch(0, 1)
        self._update_compare()
        return w

    def _update_compare(self, *_):
        pro = self.pro_start.value()
        QgsSettings().setValue(_PRO_KEY, pro)
        st = last_start()
        if pro and st:
            self.compare.setText(tr("{app} starts {x:.0f}× faster here.").format(app=APP_NAME, x=pro / max(st, 0.1))
                                 if pro > st else tr("ArcGIS Pro started faster on this computer."))
        else:
            self.compare.setText("")

    def run_benchmark(self):
        from qgis.PyQt.QtWidgets import QApplication

        self.bench_btn.setEnabled(False)
        self.bench_label.setText(tr("Working…"))
        QApplication.processEvents()
        try:
            rows = benchmark()
            total = sum(r[1] for r in rows)
            lines = [f"{tr(BENCH_NAMES[k])}: {tr('{s:.2f} s').format(s=s)}  ({d})" for k, s, d in rows]
            lines.append(tr("Total: {s:.2f} seconds").format(s=total))
            self.bench_label.setText("\n".join(lines))
            self.bench_rows = rows
        except Exception as e:  # noqa: BLE001
            self.bench_label.setText(tr("The test could not run: {e}").format(e=e))
            self.bench_rows = None
        finally:
            self.bench_btn.setEnabled(True)


def open_dialog(plugin, show=True):
    d = FromProDialog(plugin)
    if show:
        d.show()
    plugin._from_pro_dialog = d  # keep it alive (modeless)
    return d


__all__ = ["open_dialog", "record_start", "last_start", "benchmark"]
