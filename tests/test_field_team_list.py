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

# ---- 3 surveyors with sealed identity, filters, export, name unification, device blocking & daily report ------------
import http.client  # noqa: E402
import json  # noqa: E402
import shutil  # noqa: E402
import socket  # noqa: E402
import ssl  # noqa: E402
import urllib.parse  # noqa: E402
from biruni.field_photos import server as fs  # noqa: E402
from biruni.field_photos.handover import seal as seal_for_pc  # noqa: E402
from biruni.vendor import ensure_vendor  # noqa: E402

ensure_vendor()

work = tempfile.mkdtemp()
photos_folder = os.path.join(work, "Field photos")
cert_folder = os.path.join(work, "cert")
with socket.socket() as _s:
    _s.bind(("127.0.0.1", 0))
    srv_port = _s.getsockname()[1]

JPEG = b"\xff\xd8\xff\xe0" + b"\x00\x10JFIF\x00" + os.urandom(1200) + b"\xff\xd9"
srv = fs.PhotoServer(photos_folder, cert_folder, fs.all_phone_texts(), "ar", ["عمود", "غرفة"], srv_port, ["127.0.0.1"])
d.folder.setText(photos_folder)
d.layer = fd.open_layer(photos_folder)
d.server = srv

# Check old field order is preserved and surveyor + device_id are added at the end
field_names = [n for n in (d.layer.fields().at(i).name() for i in range(d.layer.fields().count())) if n != "fid"]
check(field_names[:15] == [name for name, _t in fd.FIELDS[:15]]
      and field_names[15:17] == ["surveyor", "device_id"],
      f"old columns keep their exact order; surveyor and device_id added at the end ({field_names})")

# Phone page requires the surveyor's name before the first photo and keeps a random device_id
page_html = srv.page.decode("utf-8")
check("function requireName()" in page_html and "if(!requireName())return" in page_html
      and "biruni-dev" in page_html and "device_id:DEVID" in page_html,
      "phone page requires the surveyor name before the first photo and keeps a persistent device_id")

ctx = ssl.create_default_context()
ctx.check_hostname = False
ctx.verify_mode = ssl.CERT_NONE


def post_sealed_photo(inner_meta, outer_override=None):
    sealed_blob = seal_for_pc(srv.handover.public, inner_meta, JPEG)
    outer = {"id": inner_meta["id"], "sealed": 1, "t": inner_meta.get("t")}
    if outer_override:
        outer.update(outer_override)
    q = urllib.parse.quote(json.dumps(outer, ensure_ascii=False))
    c = http.client.HTTPSConnection("127.0.0.1", srv_port, context=ctx, timeout=15)
    c.request("POST", f"{srv.path()}photo?m={q}", body=sealed_blob, headers={"Content-Type": "application/octet-stream"})
    r = c.getresponse()
    body = r.read()
    c.close()
    return r.status, body


now_ms = int(time.time() * 1000)
yesterday_ms = now_ms - 2 * 86400 * 1000

# Old photo without a surveyor name -> kept as "Unknown"
st_old, _ = post_sealed_photo({"id": "oldphoto-0001", "lat": 30.01, "lon": 31.21, "acc": 10.0, "t": yesterday_ms,
                               "device": "Android"})
check(st_old == 200, "an older photo without surveyor name is accepted")

# 3 phones with different surveyor names and persistent device_ids (sealed inside the encrypted payload)
phones = [
    ("dev_ahmed_000001", "أحمد طارق", 30.0444, 31.2357, 4.0, "direct", False, now_ms - 60000),
    ("dev_ahmed_000001", "احمد طارق", 30.0448, 31.2361, 6.0, "direct", False, now_ms - 30000),
    ("dev_sara_0000002", "سارة علي", 30.0550, 31.2450, 8.0, "relay", True, now_ms - 20000),
    ("dev_omar_0000003", "عمر حسن", 30.0650, 31.2550, 12.0, "peer", False, now_ms - 10000),
]
for idx, (dev_id, who, lat, lon, acc, route, prev, t_val) in enumerate(phones, 1):
    st, _ = post_sealed_photo(
        {"id": f"survphoto-000{idx}", "lat": lat, "lon": lon, "acc": acc, "t": t_val,
         "device": "Android", "device_id": dev_id, "surveyor": who, "by": who,
         "route": route, "preview": prev},
        outer_override={"surveyor": "SpoofOutside", "device_id": "spoof_dev_999999"},
    )
    check(st == 200, f"sealed photo {idx} from {who} accepted")

d._read_events()
check(d.layer.featureCount() == 5, f"all 5 photos (1 old + 4 from 3 phones) are on the layer ({d.layer.featureCount()})")

by_id = {f["photo_id"]: f for f in d.layer.getFeatures()}
check(by_id["oldphoto-0001"]["surveyor"] == "Unknown" and not by_id["oldphoto-0001"]["device_id"],
      f"old nameless photo gets 'Unknown' ({by_id['oldphoto-0001']['surveyor']})")
check(by_id["survphoto-0001"]["surveyor"] == "أحمد طارق"
      and by_id["survphoto-0001"]["device_id"] == "dev_ahmed_000001",
      "surveyor and device_id come from inside the sealed payload, ignoring outer spoof")
check(by_id["survphoto-0003"]["surveyor"] == "سارة علي"
      and by_id["survphoto-0004"]["surveyor"] == "عمر حسن",
      "each of the 3 phones is recorded with its own surveyor name")

# Check the Surveyors table shows all surveyors, their routes, today/total/preview counts, and average accuracy
s_rows = {d.team_list.topLevelItem(i).data(0, fd.Qt.ItemDataRole.UserRole): d.team_list.topLevelItem(i)
          for i in range(d.team_list.topLevelItemCount())}
check({"dev_ahmed_000001", "dev_sara_0000002", "dev_omar_0000003"}.issubset(set(s_rows)),
      f"all 3 surveyors appear in the table ({list(s_rows)})")
ahmed_it = s_rows["dev_ahmed_000001"]
sara_it = s_rows["dev_sara_0000002"]
check(ahmed_it.text(4) == "± 5 m" and ahmed_it.text(6) == "Direct"
      and ahmed_it.text(7) == "2" and ahmed_it.text(8) == "2" and ahmed_it.text(9) == "0",
      f"Ahmed stats: avg acc ± 5 m, Direct, 2 today, 2 total, 0 previews ({[ahmed_it.text(c) for c in range(11)]})")
check(sara_it.text(4) == "± 8 m" and sara_it.text(6) == "Relay"
      and sara_it.text(7) == "1" and sara_it.text(8) == "1" and sara_it.text(9) == "1",
      f"Sara stats: avg acc ± 8 m, Relay, 1 today, 1 waiting original ({[sara_it.text(c) for c in range(11)]})")

# Name unification: Ahmed wrote his name in two ways ("أحمد طارق" and "احمد طارق")
n_unified = d.unify_surveyor_name("أحمد طارق محمد", device_id="dev_ahmed_000001", old_names=["أحمد طارق", "احمد طارق"])
check(n_unified == 2, f"unifying Ahmed's name updated both of his photos ({n_unified})")
check({f["surveyor"] for f in d.layer.getFeatures() if f["device_id"] == "dev_ahmed_000001"} == {"أحمد طارق محمد"},
      "both photos in the GeoPackage layer now carry the unified name")

# Filter: "Show only their photos" and "Show all" (display only, nothing deleted)
d.filter_by_surveyor("dev_ahmed_000001")
check(d.layer.featureCount() == 2, f"filtering to Ahmed shows only his 2 photos ({d.layer.featureCount()})")
d.filter_by_surveyor("سارة علي")
check(d.layer.featureCount() == 1, f"filtering to Sara shows only her 1 photo ({d.layer.featureCount()})")
d.show_all_photos()
check(d.layer.featureCount() == 5, f"Show all brings back all 5 photos ({d.layer.featureCount()})")

# Filter: "Today's work" (excludes yesterday's photo, keeps today's 4 photos)
d.filter_today(True)
check(d.layer.featureCount() == 4, f"Today's work filter shows today's 4 photos ({d.layer.featureCount()})")
d.show_all_photos()
check(d.layer.featureCount() == 5, "clearing Today's work filter restores all 5 photos")

# Zoom to surveyor's work
rect = d.zoom_to_surveyor("dev_sara_0000002")
check(rect is not None and rect.contains(fd.QgsPointXY(31.2450, 30.0550)), "zoom to Sara's work centers on her photos")

# Export surveyor's work (GeoPackage and CSV/Excel with photos subfolder)
exp_gpkg = d.export_surveyor_work("dev_ahmed_000001", os.path.join(work, "export", "ahmed.gpkg"))
exp_csv = d.export_surveyor_work("dev_ahmed_000001", os.path.join(work, "export", "ahmed.csv"))
check(exp_gpkg["count"] == 2 and os.path.isfile(exp_gpkg["path"])
      and len(os.listdir(exp_gpkg["photos_dir"])) == 2,
      f"export to GeoPackage writes 2 features and copies 2 photos ({exp_gpkg})")
check(exp_csv["count"] == 2 and os.path.isfile(exp_csv["path"])
      and "أحمد طارق محمد" in open(exp_csv["path"], encoding="utf-8-sig").read(),
      "export to CSV/Excel keeps the surveyor's unified Arabic name")

# Block device: refuses new photos from that device_id, logs the reason, and keeps old photos untouched
d.block_device("dev_omar_0000003", reason="Lost phone in the field", name="عمر حسن")
st_blk, body_blk = post_sealed_photo(
    {"id": "survphoto-0099", "lat": 30.066, "lon": 31.256, "acc": 5.0, "t": now_ms,
     "device": "Android", "device_id": "dev_omar_0000003", "surveyor": "عمر حسن", "by": "عمر حسن"}
)
d._read_events()
rej = fs.read_rejected(photos_folder)
check(st_blk == 403 and json.loads(body_blk).get("error") == "blocked",
      f"a photo from a blocked device is refused with 403 ({st_blk}, {body_blk})")
check(len(rej) == 1 and rej[0]["device_id"] == "dev_omar_0000003"
      and "Lost phone" in rej[0]["reason"],
      f"the rejection reason is written to rejected.jsonl ({rej})")
check(d.layer.featureCount() == 5
      and any(f["device_id"] == "dev_omar_0000003" for f in d.layer.getFeatures()),
      "blocking a device does not delete its earlier photos")

# Daily report: HTML and PDF with today's count per surveyor, first/last photo, and waiting originals
html_rep = os.path.join(work, "daily_report.html")
pdf_rep = os.path.join(work, "daily_report.pdf")
rep_data = d.save_daily_report(html_rep)
d.save_daily_report(pdf_rep)
check(rep_data["total_today"] == 4 and rep_data["waiting_originals"] == 1
      and os.path.isfile(html_rep) and "أحمد طارق محمد" in open(html_rep, encoding="utf-8").read()
      and os.path.isfile(pdf_rep) and os.path.getsize(pdf_rep) > 1000,
      f"daily report saved as both HTML and PDF ({rep_data['total_today']} today, {rep_data['waiting_originals']} waiting)")

d.stop()
shutil.rmtree(work, ignore_errors=True)
print(f"\n{'FAILED: ' + str(len(failures)) if failures else 'ALL PASSED'}", flush=True)
os._exit(1 if failures else 0)
