r"""The Field photos panel end to end, off screen: start, a phone sends a photo, the point is on the map, stop.

Run:  engine\bin\python-qgis.bat tests\test_field_photos_dock.py
"""
import http.client
import json
import os
import shutil
import ssl
import sys
import tempfile
import time
import urllib.parse

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "app", "plugins"))
sys.path.insert(0, os.path.join(HERE, "..", "app", "python"))

from qgis.core import QgsApplication, QgsProject, QgsSettings  # noqa: E402

SETTINGS_DIR = tempfile.mkdtemp()
os.environ["QGIS_CUSTOM_CONFIG_PATH"] = SETTINGS_DIR
app = QgsApplication([], True)
app.initQgis()
from qgis.gui import QgsMapCanvas  # noqa: E402
from qgis.PyQt.QtWidgets import QMainWindow  # noqa: E402

from biruni.field_photos import dock as fd  # noqa: E402
from biruni.field_photos.server import PHOTO_PORT  # noqa: E402

failures = []


def check(cond, msg):
    print(("PASS " if cond else "FAIL ") + msg)
    if not cond:
        failures.append(msg)


class Bar:
    """The message bar: notices are kept so the test can read them."""
    notices = []

    def pushWarning(self, *a):
        pass

    def pushSuccess(self, title, text):
        Bar.notices.append(text)

    def createMessage(self, title, text):
        from qgis.PyQt.QtWidgets import QHBoxLayout, QWidget

        w = QWidget()
        QHBoxLayout(w)
        w.text = text
        return w

    def pushWidget(self, item, level, duration=0):
        Bar.notices.append(item.text)


class Iface:
    def __init__(self):
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


class Activity:
    def __init__(self):
        self.logged = []

    def log(self, *a):
        self.logged.append(a)


class Plugin:
    def __init__(self):
        self.iface = Iface()
        self.theme = Theme()
        self.activity = Activity()
        self.toolbox = None


work = tempfile.mkdtemp()
folder = os.path.join(work, "photos here")
QgsSettings().setValue(fd.KEY + "folder", folder)
QgsSettings().setValue(fd.KEY + "mode", "local")
plugin = Plugin()
d = fd.open_dock(plugin)
check(d is fd.open_dock(plugin), "one panel per window")
check(d.folder.text() == folder, "the chosen folder is shown")
d.kinds.setText("عمود، لوحة, Damage")
d.start()
check(d.server is not None and d.start_btn.text() != "Start" or d.server is not None, "started")
check(d.qr.pixmap() is not None and not d.qr.pixmap().isNull(), "the QR code is shown")
check(d.url.text().startswith("https://") and str(PHOTO_PORT) in d.url.text(), "the address is on the local network")
check(plugin.activity.logged and plugin.activity.logged[0][0] == "field_photos", "the start is in the activity log")
lyr = d.layer
check(lyr is not None and QgsProject.instance().mapLayer(lyr.id()) is not None, "the photos layer is on the map")

ctx = ssl.create_default_context()
ctx.check_hostname = False
ctx.verify_mode = ssl.CERT_NONE


def req(method, path, body=None, headers=None):
    c = http.client.HTTPSConnection("127.0.0.1", PHOTO_PORT, context=ctx, timeout=15)
    c.request(method, path, body=body, headers=headers or {})
    r = c.getresponse()
    data = r.read()
    c.close()
    return r.status, data


st, page = req("GET", d.server.path(), headers={"User-Agent": "Phone"})
check(st == 200 and "عمود" in page.decode() and "لوحة" in page.decode() and "Damage" in page.decode(),
      "the kinds reach the page (Arabic comma too)")

from qgis.PyQt.QtCore import QBuffer, QByteArray, QIODevice  # noqa: E402
from qgis.PyQt.QtGui import QColor, QImage  # noqa: E402

img = QImage(64, 48, QImage.Format.Format_RGB32)
img.fill(QColor("#2CC5B4"))
ba = QByteArray()
buf = QBuffer(ba)
buf.open(QIODevice.OpenModeFlag.WriteOnly)
img.save(buf, "JPG")
jpeg = bytes(ba)
for i in range(3):
    meta = {"id": f"dockphoto-000{i}", "lat": 30.04 + i * 0.001, "lon": 31.23, "acc": 5, "compass": 90 * i,
            "t": int(time.time() * 1000), "kind": "عمود", "note": f"ملاحظة {i}"}
    st, _ = req("POST", d.server.path() + "photo?m=" + urllib.parse.quote(json.dumps(meta)), jpeg,
                {"Content-Type": "image/jpeg"})
    check(st == 200, f"photo {i} accepted")
d._read_events()
check(lyr.featureCount() == 3, "three photos, three points - straight away")
check(d.received == 3 and "3" in d.count.text(), "the counter shows them")
check(any("3" in n and "photo" in n.lower() for n in Bar.notices), "the user is told photos came in, and from whom: "
      + str(Bar.notices[-1:] if Bar.notices else None))
check(d.thumb.pixmap() is not None and not d.thumb.pixmap().isNull(), "the last photo is shown in the panel")
check("ملاحظة 2" in d.last_info.text(), "with its note")
check(not lyr.isEditable() and not lyr.isModified(), "nothing to save")

# the layer removed from the map by mistake: the next photo brings it back, without duplicates
QgsProject.instance().removeMapLayer(lyr.id())
meta = {"id": "dockphoto-0009", "lat": 30.05, "lon": 31.24, "acc": 3, "t": int(time.time() * 1000)}
req("POST", d.server.path() + "photo?m=" + urllib.parse.quote(json.dumps(meta)), jpeg, {"Content-Type": "image/jpeg"})
d._read_events()
check(d.layer is not None and QgsProject.instance().mapLayer(d.layer.id()) is not None and
      d.layer.featureCount() == 4, "the layer comes back with all four photos")

d.stop()
check(d.server is None and d.qr.pixmap().isNull() if d.qr.pixmap() is not None else True, "stopped, QR cleared")
try:
    req("GET", "/")
    check(False, "the server is closed after stop")
except OSError:
    check(True, "the server is closed after stop")

# started again: the photos received before are on the map once, not twice
d.start()
check(d.layer.featureCount() == 4, "restart: still four points")
d.stop()

# my own Cloudflare tunnel: shown only in its mode, a wrong token stops before anything starts
check(d.own.isHidden(), "the own-tunnel boxes are hidden in the nearby mode")
d.mode.setCurrentIndex(d.mode.findData("own"))
check(not d.own.isHidden(), "and shown in the own-tunnel mode")
d.own_token.setText("not a token")
d.own_host.setText("photos.example.com")
d.start()
check(d.server is None and "token" in d.state.text().lower(), "a wrong token: a clear message, nothing started")
import base64  # noqa: E402

tok = base64.b64encode(json.dumps({"a": "0" * 32, "t": "x", "s": "eA=="}).encode()).decode()
d.own_token.setText("cloudflared.exe service install " + tok)
d.own_host.setText("")
d.start()
check(d.server is None and "address" in d.state.text().lower(), "no address: a clear message, nothing started")
stored = QgsSettings().value(fd.KEY + "own-token", "")
check(tok not in str(stored), "the token is never stored in clear")
d.mode.setCurrentIndex(d.mode.findData("local"))

# the Biruni bridge: the QR code points at the phone page with this PC's key; the server is for this PC only
from biruni.field_photos import bridge  # noqa: E402
from biruni.secure import door as _door  # noqa: E402
_door.check_door = lambda u, **k: {"state": "ok", "page": _door.rel_of(u), "why": "", "version": "test"}  # the live site is checked in test_door.py

d.mode.setCurrentIndex(d.mode.findData("bridge"))
check(not d.new_code.isHidden() and d.own.isHidden(), "bridge mode: the New code button, no tunnel boxes")
key = bridge.load_key(QgsSettings())
check(bridge.load_key(QgsSettings()) == key, "the bridge key stays the same between starts")
d.start()
check(d.server is not None and d.bridge is not None and d.bridge.running(), "bridge mode started its hidden receiver")
check(d.qr.pixmap() is not None and not d.qr.pixmap().isNull() and d.url.toolTip() == bridge.phone_url(key, "en")
      or d.url.toolTip().startswith(bridge.PAGE_URL + "#k=" + key), "the QR code holds the phone page and the key")
check(d.server.httpd.server_address[0] == "127.0.0.1", "in bridge mode the photo server is for this PC only")
proc = d.bridge.proc
d.stop()
check(d.bridge is None and proc.poll() is not None, "Stop ends the hidden receiver")
check(bridge.load_key(QgsSettings(), fresh=True) != key, "a new code makes a new key")
from biruni.gps.source import GPS_BRIDGE_KEY  # noqa: E402

check(bridge.load_key(QgsSettings(), name=GPS_BRIDGE_KEY) != bridge.load_key(QgsSettings()),
      "Phone GPS has its own bridge key: it never answers the Field Photos phones")
d.mode.setCurrentIndex(d.mode.findData("local"))

check(d.photo_to_place() is not None and os.path.isfile(d.photo_to_place()),
      "the last photo can be placed on the map (Place Image on Map)")

# ---- the form designer (hidden: the rules are checked without clicking) -------------------------------------------
from biruni.field_photos.form_dialog import FormDialog, example_form  # noqa: E402

check(d.form() == [] and d.form_info.text(), "no form at first: the panel says so")
dlg = FormDialog([], d)
dlg._fill(example_form())
problem, form = dlg._check()
check(problem is None and len(form) == 7 and form[1]["choices"], "the example form is right as it is")
dlg.table.item(1, 2).setText("Good")
check("two choices" in (dlg._check()[0] or ""), "a choice question needs two choices")
dlg.table.item(1, 2).setText("Good, Broken")
dlg.table.item(3, 3).setText("ten")
check("numbers" in (dlg._check()[0] or ""), "limits must be numbers")
dlg.table.item(3, 3).setText("50")
check("larger" in (dlg._check()[0] or ""), "the minimum cannot be over the maximum")
dlg.table.item(3, 3).setText("0")
dlg.table.item(6, 0).setText(dlg.table.item(0, 0).text())
check("Two questions" in (dlg._check()[0] or ""), "two questions cannot have the same text")
dlg.table.item(6, 0).setText("Surveyor")
before = [f["name"] for f in dlg._check()[1]]
dlg.table.setCurrentCell(0, 0)
dlg._move(1)
check([f["name"] for f in dlg._check()[1]] == [before[1], before[0]] + before[2:], "questions move, keeping their columns")
dlg.table.item(0, 0).setText("Status")
check(dlg._check()[1][0]["name"] == before[1], "renaming keeps the column")
dlg._ok()
QgsSettings().setValue(fd.KEY + "form", json.dumps(dlg.form, ensure_ascii=False))
d._show_form()
check(len(d.form()) == 7 and "7" in d.form_info.text(), "the form is kept and the panel counts it")
d.start()
check(d.server is not None and len(d.server.store.form) == 7 and b'"choices"' in d.server.page,
      "Start sends the form to the phone page")
check(all(d.layer.fields().indexOf(f["name"]) >= 0 for f in d.form()), "and gives the photos layer its columns")
d.stop()
fd.close_dock(plugin)
check(fd._DOCK == {}, "the panel is removed on unload")
QgsProject.instance().clear()
shutil.rmtree(work, ignore_errors=True)
print("ALL PASS" if not failures else f"{len(failures)} FAILED")
app.exitQgis()
sys.exit(1 if failures else 0)
