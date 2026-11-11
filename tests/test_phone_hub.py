r"""The phone hub (phone_hub/): one code, one page on the phone that opens the phone tools started on this PC.

Run:  engine\bin\python-qgis.bat tests\test_phone_hub.py

Real HTTPS requests go to a real hub server on this PC (127.0.0.1 only, free ports), as the phone's page sends
them. The panel is made off screen with stand-in tools, then with the real tools' adapters reading stand-in panels.
"""
import http.client
import json
import os
import shutil
import ssl
import subprocess
import sys
import tempfile

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "app", "plugins"))
sys.path.insert(0, os.path.join(HERE, "..", "app", "python"))

from qgis.core import QgsApplication  # noqa: E402

SETTINGS_DIR = tempfile.mkdtemp()
os.environ["QGIS_CUSTOM_CONFIG_PATH"] = SETTINGS_DIR
app = QgsApplication([], True)
app.initQgis()
from qgis.gui import QgsMapCanvas  # noqa: E402
from qgis.PyQt.QtWidgets import QMainWindow  # noqa: E402

from biruni.phone_hub import dock as hd  # noqa: E402
from biruni.phone_hub import features as hf  # noqa: E402
from biruni.phone_hub import server as hs  # noqa: E402

failures = []


def check(cond, msg):
    print(("PASS " if cond else "FAIL ") + msg)
    if not cond:
        failures.append(msg)


def free_port():
    import socket
    for _ in range(50):
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            p = s.getsockname()[1]
        try:
            with socket.socket() as a:
                a.bind(("127.0.0.1", p))
            return p
        except OSError:
            continue
    raise RuntimeError("no free port")


ctx = ssl.create_default_context()
ctx.check_hostname = False
ctx.verify_mode = ssl.CERT_NONE


def req(port, method, path, body=None):
    c = http.client.HTTPSConnection("127.0.0.1", port, context=ctx, timeout=15)
    c.request(method, path, body=body, headers={"User-Agent": "Phone"})
    r = c.getresponse()
    data = r.read()
    c.close()
    return r.status, data


def state(port, token):
    st, body = req(port, "GET", f"/{token}/state")
    return st, (json.loads(body) if st == 200 else None)


work = tempfile.mkdtemp()
cert = os.path.join(work, "cert")

# ---- the links the page may see --------------------------------------------------------
check(hs.clean_link({"port": 10112, "path": "/abc-_9/"}) == {"port": 10112, "path": "/abc-_9/"}, "a local link is kept")
check(hs.clean_link({"url": "https://example.org/p/#k=abc&l=ar"}) is not None, "a full https link is kept")
for bad in ({"url": "javascript:alert(1)"}, {"url": "http://x/"}, {"url": "https://x/\"><script>"},
            {"port": 0, "path": "/"}, {"port": "80", "path": "/"}, {"port": True, "path": "/"},
            {"port": 80, "path": "/a b"}, {"port": 80, "path": "x"}, None, "https://x/"):
    check(hs.clean_link(bad) is None, f"an unsafe link is dropped: {bad!r}")
feats = hs.clean_features([{"id": "photos", "on": True, "link": {"port": 1, "path": "/t/"}},
                           {"id": "gps", "on": False, "link": {"port": 2, "path": "/t/"}},
                           {"id": "secret-layer", "on": True, "link": {"port": 3, "path": "/t/"}}])
check([f["id"] for f in feats] == list(hs.FEATURES), "every known tool is listed once, unknown ones never")
check(feats[0]["link"] is None and feats[1]["link"] == {"port": 1, "path": "/t/"} and feats[2]["link"] is None,
      "only a started tool has a link")

# ---- the server ------------------------------------------------------------------------------
texts = {"en": hs.phone_texts(lambda s: s), "ar": hs.phone_texts(lambda s: "</script><b>" + s)}
port = free_port()
srv = hs.HubServer(cert, texts, "ar", port, ["127.0.0.1"], bind="127.0.0.1")
check(srv.port == port and srv.url("127.0.0.1").startswith(f"https://127.0.0.1:{port}/"), "the hub serves HTTPS here")
st, page = req(port, "GET", srv.path())
html = page.decode("utf-8")
check(st == 200 and 'dir="rtl"' in html and 'lang="ar"' in html, "the page opens with the token, in Arabic")
check("</script><b>" not in html and "<\\/script><b>" in html, "texts cannot close the page's script")
check(srv.events.get_nowait()[0] == "phone", "the panel hears that a phone opened the page")
check(req(port, "GET", "/")[0] == 404, "no page without the token")
check(req(port, "GET", "/wrong-token/")[0] == 404 and req(port, "GET", "/wrong-token/state")[0] == 404,
      "no page and no states with a wrong token")
check(req(port, "GET", srv.path()[:-1])[0] == 301, "the address without the last slash is sent to the page")
check(req(port, "GET", srv.path() + "anything")[0] == 404, "nothing else is served")
check(req(port, "POST", srv.path() + "state", b'{"id":"gps","on":true}')[0] == 405,
      "the phone cannot change anything")
st, s0 = state(port, srv.token)
check(st == 200 and all(not f["on"] and f["link"] is None for f in s0["features"]), "at first every tool is off")
check(srv.set_features([{"id": "photos", "on": True, "link": {"port": 4242, "path": "/tok/"}}]),
      "a started tool changes the state")
check(not srv.set_features([{"id": "photos", "on": True, "link": {"port": 4242, "path": "/tok/"}}]),
      "the same states are not a change")
st, s1 = state(port, srv.token)
on = {f["id"]: f for f in s1["features"]}
check(s1["v"] > s0["v"] and on["photos"]["on"] and on["photos"]["link"] == {"port": 4242, "path": "/tok/"}
      and not on["gps"]["on"] and not on["map"]["on"], "the phone sees the started tool, and only it")
srv.set_features([])
st, s2 = state(port, srv.token)
check(s2["v"] > s1["v"] and all(f["link"] is None for f in s2["features"]), "a stopped tool leaves the page")
srv.stop()

# ---- the page script compiles ----------------------------------------------------------------
node = shutil.which("node") or next((p for p in (r"C:\Program Files\nodejs\node.exe",) if os.path.isfile(p)), None)
if node:
    js = html.split("<script>")[1].split("</script>")[0]
    path = os.path.join(work, "hub.js")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(js)
    out = subprocess.run([node, "--check", path], capture_output=True, text=True)
    check(out.returncode == 0, "the phone page script has no syntax error " + out.stderr[:200])
else:
    print("SKIP node not found: page script not compiled")
check(set(hs.phone_texts(lambda s: s)) <= set(hs.all_phone_texts()["en"]), "the page texts are in every language")
check(all(set(v) == set(hs.phone_texts(lambda s: s)) for v in hs.all_phone_texts().values()),
      "every language has every page text (English when not translated)")


# ---- the panel -------------------------------------------------------------------------------
class Bar:
    def pushWarning(self, *a):
        pass


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


class Fake(hf.Feature):
    """A phone tool that only remembers whether it is on."""

    def __init__(self, fid, link):
        self.id, self.icon, self.on, self.link, self.shown = fid, "camera", False, link, 0

    def title(self):
        return self.id

    def state(self, plugin):
        return self.on, (self.link if self.on else None)

    def start(self, plugin):
        self.on = True

    def stop(self, plugin):
        self.on = False

    def show(self, plugin):
        self.shown += 1


from qgis.core import QgsSettings  # noqa: E402

QgsSettings().setValue("locale/overrideFlag", False)
plugin = Plugin()
fakes = [Fake("gps", {"port": 5001, "path": "/g/"}), Fake("photos", {"port": 5002, "path": "/p/"}),
         Fake("map", {"url": "https://example.org/v/#k=x"})]
hport = free_port()
d = hd.open_dock(plugin, features=fakes, port=hport, bind="127.0.0.1", addresses=["127.0.0.1"])
check(d is hd.open_dock(plugin), "one panel per window")
check(d.server is not None and d.server.port == hport, "opening the panel starts the hub")
check(d.qr.pixmap() is not None and not d.qr.pixmap().isNull(), "the one code is shown")
check(d.url.text() == f"https://127.0.0.1:{hport}/{d.server.token}/", "the code's address has the token")
check(plugin.activity.logged and plugin.activity.logged[0][0] == "phone_hub", "the start is in the activity log")
token = d.server.token
st, s = state(hport, token)
check(st == 200 and not any(f["on"] for f in s["features"]), "nothing is on at first")
d.toggle_feature(fakes[1])
check(fakes[1].on and d.rows["photos"][2].text() != d.rows["gps"][2].text(), "the panel starts a tool")
st, s = state(hport, token)
on = {f["id"]: f for f in s["features"]}
check(on["photos"]["on"] and on["photos"]["link"] == {"port": 5002, "path": "/p/"} and not on["gps"]["on"],
      "the phone page sees the tool started on the PC")
fakes[2].on = True  # started from its own panel: the hub sees it at its next look
d.refresh()
st, s = state(hport, token)
check({f["id"]: f for f in s["features"]}["map"]["link"] == {"url": "https://example.org/v/#k=x"},
      "a tool started in its own panel reaches the phone page")
d.toggle_feature(fakes[1])
st, s = state(hport, token)
check(not fakes[1].on and {f["id"]: f for f in s["features"]}["photos"]["link"] is None,
      "a stopped tool leaves the phone page")
d.show_feature(fakes[0])
check(fakes[0].shown == 1, "the tool's own panel opens from the hub")
d.toggle()
check(d.server is None and not d.url.text() and (d.qr.pixmap() is None or d.qr.pixmap().isNull()),
      "the hub stops and its code is gone")
d.toggle()
check(d.server is not None and d.server.token != token, "started again, the code is new")
old = d.server.port

# the real tools' adapters, reading stand-in panels
real = Plugin()
gps, photos, smap = hf.all_features()
check(not gps.available(real) and photos.state(real) == (False, None) and smap.state(real) == (False, None),
      "without their panels the tools are off")
from biruni.field_photos import dock as fd  # noqa: E402
from biruni.live_share import dock as ld  # noqa: E402
from biruni.secure import door as _door  # noqa: E402
_door.check_door = lambda u, **k: {"state": "ok", "page": _door.rel_of(u), "why": "", "version": "test"}  # the live site is checked in test_door.py


class Combo:
    def __init__(self, v):
        self.v = v

    def currentData(self):
        return self.v


class Label:
    def __init__(self, tip):
        self.tip = tip

    def toolTip(self):
        return self.tip


class FakeServer:
    port, token = 6000, "tok"

    def path(self):
        return "/tok/"


class PhotosPanel:
    server, mode, url = FakeServer(), Combo("local"), Label("")


class SharePanel:
    share, link = object(), "https://example.org/live/#k=abc&l=ar"


fd._DOCK[id(real)] = PhotosPanel()
ld._DOCK[id(real)] = SharePanel()
check(photos.state(real) == (True, {"port": 6000, "path": "/tok/"}), "field photos nearby: a link on this PC")
PhotosPanel.mode = Combo("bridge")
PhotosPanel.url = Label("https://example.org/f/#k=1")
check(photos.state(real) == (True, {"url": "https://example.org/f/#k=1"}), "field photos from anywhere: its address")
PhotosPanel.url = Label("")
check(photos.state(real) == (True, None), "a tunnel not open yet: on, no link")
check(smap.state(real) == (True, {"url": SharePanel.link}), "see my map: its link")
del fd._DOCK[id(real)], ld._DOCK[id(real)]

hd.close_dock(plugin)
check(id(plugin) not in hd._DOCK, "the panel is closed")
try:
    req(old, "GET", f"/{token}/")
    closed = False
except OSError:
    closed = True
check(closed, "closing the panel stops the hub")

shutil.rmtree(work, ignore_errors=True)
print("ALL PASS" if not failures else f"{len(failures)} FAILED")
app.exitQgis()
sys.exit(1 if failures else 0)
