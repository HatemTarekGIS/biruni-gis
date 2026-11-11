r"""The field team on the computer (2026-11-03): the list of the phones (connected or not, Wi-Fi or mobile data, how
good the link is, the battery), one label for phones sending the very same place, the track without the wander
of a phone standing still, and the time in this computer's time.

Run:  engine\bin\python-qgis.bat tests\test_field_team_list.py
"""
import os
import sys
import tempfile
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "app", "plugins"))
sys.path.insert(0, HERE)

from qgis.core import (QgsApplication, QgsMapRendererSequentialJob, QgsMapSettings, QgsProject,  # noqa: E402
                       QgsRectangle, QgsSettings)
from qgis.PyQt.QtCore import QSize  # noqa: E402

os.environ["QGIS_CUSTOM_CONFIG_PATH"] = tempfile.mkdtemp()
app = QgsApplication([], True)
app.initQgis()
from biruni.field_photos import dock as fd  # noqa: E402
from biruni.field_photos.server import clean_pos  # noqa: E402

failures = []


def check(cond, msg):
    print(("PASS " if cond else "FAIL ") + msg, flush=True)
    if not cond:
        failures.append(msg)


# ---- what a phone sends is cleaned ---------------------------------------------------------------------------------
p = clean_pos({"id": "a" * 16, "lat": 31.1, "lon": 29.8, "acc": 7, "net": "wifi", "eff": "4g", "down": 9.5,
               "ping": 120, "bat": 0.73, "chg": True, "route": "relay", "t": 0})
check(p["net"] == "wifi" and p["ping"] == 120 and p["bat"] == 0.73 and p["chg"] is True and p["route"] == "relay",
      "the phone's network, link, battery and route are kept")
bad = clean_pos({"id": "a" * 16, "lat": 31.1, "lon": 29.8, "net": "<b>x</b>", "eff": "9g", "ping": -5, "bat": 7,
                 "chg": "yes", "route": "evil"})
check(bad["net"] == "" and bad["eff"] == "" and bad["ping"] is None and bad["bat"] is None and bad["chg"] is False
      and bad["route"] == "", "anything else is dropped")
check(clean_pos({"id": "a" * 16, "lat": 31.1, "lon": 29.8})["ping"] is None, "an older phone page still works")

# ---- the texts ------------------------------------------------------------------------------------------------------
check([fd.link_quality(x) for x in (60, 250, 700, 3000, None)] == [4, 3, 2, 1, None], "link from the time there and back")
check(fd._net_text({"net": "cellular", "eff": "4g"}).endswith("(4G)") and fd._net_text({}) == "—",
      "mobile data with its kind; an iPhone (that does not tell) shows a dash")
check(fd._battery_text({"bat": 0.42, "chg": True}) == "42% ⚡", "the battery")


# ---- the panel -------------------------------------------------------------------------------------------------------
class Bar:
    def pushWarning(self, *a):
        pass

    def pushSuccess(self, *a):
        pass

    def createMessage(self, title, text):
        from qgis.PyQt.QtWidgets import QHBoxLayout, QWidget

        w = QWidget()
        QHBoxLayout(w)
        return w

    def pushWidget(self, *a, **k):
        pass


class Iface:
    def __init__(self):
        from qgis.gui import QgsMapCanvas
        from qgis.PyQt.QtWidgets import QMainWindow

        self.mw = QMainWindow()
        self.canvas = QgsMapCanvas(self.mw)
        self.mw.setCentralWidget(self.canvas)

    def mainWindow(self):
        return self.mw

    def mapCanvas(self):
        return self.canvas

    def messageBar(self):
        return Bar()


class Theme:
    def tokens(self):
        return {k: "#888888" for k in ("text", "text_dim", "accent", "panel", "border", "accent_text",
                                       "accent_hover", "mid", "icon")}


class Plugin:
    def __init__(self):
        self.iface = Iface()
        self.theme = Theme()
        self.activity = type("A", (), {"log": lambda *a: None})()
        self.toolbox = None


class Store:
    folder = tempfile.mkdtemp()


QgsSettings().setValue(fd.KEY + "mode", "bridge")
d = fd.open_dock(Plugin())
d.server = type("S", (), {"store": Store(), "stop": lambda self: None})()
check(not d.team_list.isVisible() and not d.team_list.topLevelItemCount(), "no list before a phone comes")


def pos(dev, name, lat, **more):
    m = {"id": dev * 16, "name": name, "lat": lat, "lon": 29.79315, "acc": 7, "t": time.time() * 1000}
    m.update(more)
    return clean_pos(m)


d._on_pos(pos("a", "Hatem Tarek", 31.11447, net="wifi", ping=90, bat=0.8, route="lan"))
d._on_pos(pos("b", "Ziad Omar", 31.11447, net="cellular", eff="4g", ping=650, bat=0.31, chg=True, route="relay"))
rows = {d.team_list.topLevelItem(i).text(0): [d.team_list.topLevelItem(i).text(c) for c in range(6)]
        for i in range(d.team_list.topLevelItemCount())}
check(set(rows) == {"Hatem Tarek", "Ziad Omar"}, f"both phones are listed ({list(rows)})")
check(rows["Hatem Tarek"][1] == "Connected" and rows["Hatem Tarek"][2] == "Wi-Fi" and "Strong" in rows["Hatem Tarek"][3],
      f"Hatem: connected, Wi-Fi, strong ({rows['Hatem Tarek']})")
check(rows["Ziad Omar"][2] == "Mobile data (4G)" and "Weak" in rows["Ziad Omar"][3] and rows["Ziad Omar"][5] == "31% ⚡",
      f"Ziad: mobile data, weak link, battery charging ({rows['Ziad Omar']})")
check("2 phones, 2 connected" in d.team_info.text(), d.team_info.text())

# a phone that stopped sending
d.team["b" * 16]["at"] -= 200
d._show_team()
z = next(d.team_list.topLevelItem(i) for i in range(2) if d.team_list.topLevelItem(i).text(0) == "Ziad Omar")
check(z.text(1) == "Not heard for 3 min" and "1 connected" in d.team_info.text(), f"a silent phone is shown so ({z.text(1)})")

# one label for the two phones on the same spot
live = d.team_live
s = QgsMapSettings()
s.setLayers([live])
s.setDestinationCrs(live.crs())
s.setOutputSize(QSize(1000, 800))
s.setExtent(QgsRectangle(29.7912, 31.1125, 29.7952, 31.1165))
j = QgsMapRendererSequentialJob(s)
j.start()
j.waitForFinished()
labels = sorted(lab.labelText for lab in j.takeLabelingResults().allLabels())
check(labels == ["Hatem Tarek · Ziad Omar"], f"phones on the very same spot share one label ({labels})")

# the track keeps moves, not the wander of a phone standing still
track = d.team_track
n0 = track.featureCount()
for k in range(6):   # wandering a few metres to and fro around the same place, ± 7 m
    d._on_pos(pos("a", "Hatem Tarek", 31.11447 + (0.00004 if k % 2 else -0.00003), net="wifi"))
check(track.featureCount() == n0, f"standing still adds no track points ({track.featureCount() - n0})")
d._on_pos(pos("a", "Hatem Tarek", 31.11447 + 0.0005, net="wifi"))             # 55 m away
check(track.featureCount() == n0 + 1, "a real move does")

d.stop()
check(not d.team_list.isVisible() and "no phone yet" in d.team_info.text(), "stopping clears the list")
print(f"\n{'FAILED: ' + str(len(failures)) if failures else 'ALL PASSED'}", flush=True)
os._exit(1 if failures else 0)
