r"""Field photos from a phone (field_photos/): the photo server, the photo folder, the map layer, the tunnel.

Run:  engine\bin\python-qgis.bat tests\test_field_photos.py

Real HTTPS requests go to a real server on this PC, as the phone's page sends them.
"""
import http.client
import json
import os
import shutil
import ssl
import subprocess
import sys
import tempfile
import time
import urllib.parse

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "app", "plugins"))
sys.path.insert(0, os.path.join(HERE, "..", "app", "python"))

from qgis.core import QgsApplication, QgsProject  # noqa: E402

SETTINGS_DIR = tempfile.mkdtemp()
os.environ["QGIS_CUSTOM_CONFIG_PATH"] = SETTINGS_DIR
app = QgsApplication([], False)
app.initQgis()
from biruni.field_photos import server as fs  # noqa: E402
from biruni.field_photos import tunnel as ft  # noqa: E402
from biruni.field_photos import dock as fd  # noqa: E402

failures = []


def check(cond, msg):
    print(("PASS " if cond else "FAIL ") + msg)
    if not cond:
        failures.append(msg)


JPEG = b"\xff\xd8\xff\xe0" + b"\x00\x10JFIF\x00" + os.urandom(2000) + b"\xff\xd9"
TEXTS = {"title": "Biruni GIS - field photos", "intro": "x", "start": "Start", "waiting": "w", "shoot": "Take",
         "note": "n", "sent": "s", "queued": "q", "connected": "c", "lost": "l", "denied": "d", "insecure": "i"}
work = tempfile.mkdtemp()
folder = os.path.join(work, "Field photos")


def free_pair():
    """A port that is free together with the next one (a fixed port failed when another test held it)."""
    import socket
    for _ in range(50):
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            p = s.getsockname()[1]
        try:
            with socket.socket() as a, socket.socket() as b:
                a.bind(("0.0.0.0", p))
                b.bind(("0.0.0.0", p + 1))
            return p
        except OSError:
            continue
    raise RuntimeError("no two free ports")


port = free_pair()
srv =fs.PhotoServer(folder, os.path.join(work, "cert"), {"en": TEXTS}, "ar", ["عمود", "لوحة", "</script>"], port,
                     ["127.0.0.1"])
ctx = ssl.create_default_context()
ctx.check_hostname = False
ctx.verify_mode = ssl.CERT_NONE


def req(method, path, body=None, headers=None):
    c = http.client.HTTPSConnection("127.0.0.1", port, context=ctx, timeout=15)
    c.request(method, path, body=body, headers=headers or {})
    r = c.getresponse()
    data = r.read()
    c.close()
    return r.status, data


def send(meta, data=JPEG, ctype="image/jpeg"):
    q = urllib.parse.quote(json.dumps(meta, ensure_ascii=False))
    return req("POST", f"{srv.path()}photo?m={q}", data, {"Content-Type": ctype})


def drain():
    out = []
    while not srv.events.empty():
        out.append(srv.events.get_nowait())
    return out


# ---- the page ------------------------------------------------------------------
st, _ = req("GET", "/wrong-token/")
check(st == 404, "a wrong token gets nothing")
st, _ = req("GET", "/")
check(st == 404, "the bare address gets nothing")
st, page = req("GET", srv.path(), headers={"User-Agent": "Phone A"})
html = page.decode("utf-8")
check(st == 200 and 'dir="rtl"' in html and "Biruni GIS - field photos" in html, "the page, right to left in Arabic")
check("عمود" in html and "</script>\"" not in html and "<\\/script>" in html,
      "the kinds are in the page and cannot close its script")
st, _ = req("GET", srv.path().rstrip("/"))
check(st == 301, "the address without the last slash is redirected")
st, _ = req("GET", srv.path() + "ping")
check(st == 200, "ping answers")
ev = drain()
check(ev == [("phone", "Phone A")], "the PC is told a phone opened the page (once)")

# ---- photos ----------------------------------------------------------------------
t_ms = 1767225600000  # 2026-01-01 00:00 UTC
meta = {"id": "abcdef12-0001", "lat": 24.7136, "lon": 46.6753, "acc": 4.6, "alt": 612.3, "heading": None,
        "compass": 87, "t": t_ms, "fix_age": 1, "kind": "عمود", "note": "عمود مكسور أمام المسجد", "device": "Android"}
st, body = send(meta)
check(st == 200 and json.loads(body)["id"] == meta["id"], "a photo is accepted")
ev = drain()
check(len(ev) == 1 and ev[0][0] == "photo", "the PC gets the new photo")
rec = ev[0][1] if ev else {}
check(os.path.isfile(rec.get("photo", "")) and open(rec["photo"], "rb").read() == JPEG,
      "the photo is on disk, byte for byte")
check(rec.get("photo", "").endswith(".jpg") and "abcdef12" in os.path.basename(rec.get("photo", "")),
      "file name carries the time and the id")
check(os.path.basename(os.path.dirname(rec.get("photo", ""))).count("-") == 2, "one folder per day")
check(rec.get("note") == meta["note"] and rec.get("kind") == "عمود", "Arabic note and kind kept")
check(rec.get("compass") == 87 and rec.get("acc") == 4.6 and rec.get("heading") is None, "direction and accuracy")
check(rec.get("taken", "").startswith("2026-01-01T00:00:00"), "time taken from the phone")
check(not [f for f in os.listdir(os.path.dirname(rec["photo"])) if f.endswith(".part")], "no half-written file")

st, _ = send(meta)
check(st == 200 and drain() == [], "the same photo sent again (retry) is kept once")
day = os.path.dirname(rec["photo"])
check(len(os.listdir(day)) == 1, "still one file")

st, _ = send(dict(meta, id="abcdef12-0002"), b"<html>not a photo</html>", "image/jpeg")
check(st == 400, "something that is not a photo is refused")
st, _ = send(dict(meta, id="abcdef12-0003", lat=None))
check(st == 400, "a photo without a position is refused")
st, _ = send(dict(meta, id="../../evil"))
check(st == 400, "an id that could be a path is refused")
st, _ = send(dict(meta, id="abcdef12-0004", lat=95))
check(st == 400, "a position out of range is refused")
png = b"\x89PNG\r\n\x1a\n" + os.urandom(100)
st, _ = send(dict(meta, id="abcdef12-0005", note="x" * 2000), png, "image/png")
ev = drain()
check(st == 200 and ev and ev[0][1]["photo"].endswith(".png") and len(ev[0][1]["note"]) == 500,
      "PNG kept as .png, a long note is cut")
st, _ = req("POST", srv.path() + "photo?m=%7B%7D", b"", {"Content-Type": "image/jpeg"})
check(st == 400, "an empty body is refused")

c = http.client.HTTPSConnection("127.0.0.1", port, context=ctx, timeout=15)
c.putrequest("POST", srv.path() + "photo?m=%7B%7D")
c.putheader("Content-Length", str(fs.MAX_PHOTO + 1))
c.endheaders()
r = c.getresponse()
check(r.status == 413, "a photo over the size limit is refused before it is read")
c.close()
st, _ = req("POST", "/wrong-token/photo?m=%7B%7D", JPEG)
check(st == 404, "a photo with a wrong token is refused")

# ---- the log: rebuilds the map after a crash ---------------------------------------
log = fs.read_log(folder)
check([r["id"] for r in log] == ["abcdef12-0001", "abcdef12-0005"], "the log has every photo once")
with open(os.path.join(folder, fs.LOG_NAME), "a", encoding="utf-8") as f:
    f.write('{"id": "half-writ')  # power cut in the middle of a line
check(len(fs.read_log(folder)) == 2, "a damaged last line is skipped")
store2 = fs.PhotoStore(folder)
check(set(store2.records) == {"abcdef12-0001", "abcdef12-0005"}, "after a restart the photos already received are known")
_, new = store2.save(meta, JPEG)
check(new is False, "and a retry after the restart is still kept once")
srv.stop()

# ---- the map layer -----------------------------------------------------------------
lyr = fd.open_layer(folder)
check(lyr.isValid() and lyr.featureCount() == 0, "the photos layer is created next to the photos")
added = fd.add_records(lyr, fs.read_log(folder))
check(len(added) == 2 and lyr.featureCount() == 2, "the photos become points")
check(fd.add_records(lyr, fs.read_log(folder)) == [] and lyr.featureCount() == 2, "adding again adds nothing")
f = next(lyr.getFeatures())
check(abs(f.geometry().asPoint().x() - 46.6753) < 1e-9 and f["kind"] == "عمود", "position and fields")
check(lyr.editorWidgetSetup(lyr.fields().indexOf("photo")).type() == "ExternalResource", "the form shows the photo")
check(not lyr.isEditable(), "written straight to the file - nothing left to save")
again = fd.open_layer(folder)
check(again.id() == lyr.id() and len(QgsProject.instance().mapLayers()) == 1, "the layer is added to the map once")
from qgis.core import QgsVectorLayer  # noqa: E402

disk = QgsVectorLayer(os.path.join(folder, fd.GPKG) + "|layername=" + fd.LAYER, "x", "ogr")
check(disk.featureCount() == 2, "the points are on disk")
check(fd.default_folder().endswith("Field photos"), "default folder")

# ---- the tunnel ----------------------------------------------------------------------
line = "2026-10-30T10:00:00Z INF |  https://quiet-river-1234.trycloudflare.com                                     |"
check(ft.URL_RE.search(line).group(0) == "https://quiet-river-1234.trycloudflare.com", "the tunnel address is read")
check(ft.READY_RE.search("INF Registered tunnel connection connIndex=0 event=0 ip=198.41.192.7"),
      "the tunnel is ready when a connection is registered")
check(ft.find_exe(os.path.join(work, "none")) in (None,) or os.path.isfile(ft.find_exe(os.path.join(work, "none"))),
      "no tunnel program yet, or the bundled one")
# the user's own tunnel: the token from what they paste, the address from what they type
import base64  # noqa: E402

tok = base64.b64encode(json.dumps({"a": "0" * 32, "t": "1234abcd-0000-4000-8000-000000000000",
                                   "s": "c2VjcmV0c2VjcmV0c2VjcmV0"}).encode()).decode()
check(ft.token_from(tok) == tok, "a pasted token is read")
check(ft.token_from(f"cloudflared.exe service install {tok}\n") == tok, "the token inside the whole install command")
check(ft.token_from("sudo cloudflared service install eyJhIjoiYiJ9") is None, "a token that is not a tunnel's is refused")
check(ft.token_from("") is None and ft.token_from("hello") is None, "no token")
check(ft.host_from("https://Photos.MyDomain.com/abc") == "photos.mydomain.com", "the address without https and path")
check(ft.host_from("photos.mydomain.com") == "photos.mydomain.com", "a plain address")
check(ft.host_from("localhost") is None and ft.host_from("not an address") is None, "a wrong address is refused")
enc = ft.protect(tok)
check(enc and tok not in enc and ft.unprotect(enc) == tok, "the token is kept encrypted, and read back")
check(ft.unprotect("not-base64!") == "" and ft.protect("") == "", "a damaged stored token reads as empty")
check(ft.FAIL_RE.search('ERR Register tunnel error error="Unauthorized: Invalid tunnel secret"'),
      "a refused token is noticed")

fake = os.path.join(work, "fake.exe")
with open(fake, "wb") as fh:
    fh.write(b"MZ" + os.urandom(64))
if sys.platform == "win32":
    check(ft.signed_by_cloudflare(fake) is False, "an unsigned file is not trusted")
    job = ft._Job()
    check(bool(job.handle), "the job object that ends the tunnel with Biruni is made")
    p = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    job.add(p)
    import ctypes

    ctypes.windll.kernel32.CloseHandle(job.handle)
    job.handle = None
    time.sleep(1.0)
    check(p.poll() is not None, "closing the job ends the process (as when Biruni stops)")
    if p.poll() is None:
        p.kill()

# ---- the form: its rules, the answers checked again here, and the layer's columns -----------------------
form = fs.clean_form([
    {"label": "Pole number", "type": "text", "req": True},
    {"label": "Pole number", "type": "text"},  # same text: another column
    {"label": "حالة العمود", "type": "choice", "choices": ["سليم", "تالف", "سليم", ""]},
    {"label": "Problems", "type": "multi", "choices": []},  # no choices: left out
    {"label": "Height (m)", "type": "number", "min": 40, "max": 0},
    {"label": "Lamps", "type": "integer", "min": 1, "max": 8},
    {"label": "Safe?", "type": "yesno"}, {"label": "Visited", "type": "date"},
    {"label": "note", "type": "wrong"}, {"label": "1st check", "type": "text"}, {"label": "", "type": "text"},
    {"label": "المسّاح", "type": "text"}, {"label": "Profundidad", "type": "number"}, "nonsense"])
names = [f["name"] for f in form]
check(names == ["pole_number", "pole_number_2", "حالة_العمود", "height_m", "lamps", "safe", "visited", "note_2",
                "f_1st_check", "المساح", "profundidad"], "column names: from the question, unique, never a built-in column " + str(names))
check(form[2]["choices"] == ["سليم", "تالف"] and form[3]["min"] == 0 and form[3]["max"] == 40 and form[7]["type"] == "text",
      "choices cleaned, limits put in order, an unknown type is text")
check(fs.clean_form(form) == form, "a kept form reads back the same")
renamed = fs.clean_form([dict(form[0], label="Pole no.")])
check(renamed[0]["name"] == "pole_number", "renaming a question keeps its column")
check(len(fs.clean_form([{"label": f"q{i}"} for i in range(50)])) == fs.MAX_FIELDS, "at most 30 questions")
vals = fs.clean_values(form, {"pole_number": " P-17 ", "حالة_العمود": "تالف", "height_m": 41, "lamps": 2.5,
                              "safe": "yes", "visited": "2026-02-30", "note_2": "x" * 300, "unknown": 1})
check(vals == {"pole_number": "P-17", "حالة_العمود": "تالف", "note_2": "x" * 250},
      "values that do not fit are left out (limit, fraction, not yes/no, no such day, unknown question)")
vals = fs.clean_values(form, {"height_m": "12.5", "lamps": 3.0, "safe": False, "visited": "2026-10-30"})
check(vals == {"height_m": 12.5, "lamps": 3, "safe": False, "visited": "2026-10-30"}, "values that fit are kept")
check(fs.clean_values(form, "nonsense") == {} and fs.clean_values([], {"a": 1}) == {}, "no form, no answers")
fsrv = fs.PhotoServer(os.path.join(work, "form photos"), os.path.join(work, "cert"), {"en": TEXTS}, "en", [],
                      port + 1, ["127.0.0.1"], form=form + [{"label": "</script><b>", "type": "text"}])
fpage = fsrv.page.decode("utf-8")
check('"name": "lamps"' in fpage and "</script><b>" not in fpage, "the form is in the page and cannot close its script")
fmeta = {"id": "formphoto-0001", "lat": 24.7, "lon": 46.6, "t": int(time.time() * 1000),
         "f": {"pole_number": "P-17", "lamps": 3, "safe": True, "height_m": 99}}
c = http.client.HTTPSConnection("127.0.0.1", port + 1, context=ctx, timeout=15)
c.request("POST", f"/{fsrv.token}/photo?m=" + urllib.parse.quote(json.dumps(fmeta)), body=JPEG,
          headers={"Content-Type": "image/jpeg"})
check(c.getresponse().status == 200, "a photo with answers is accepted")
c.close()
frec = fsrv.events.get(timeout=5)[1]
check(frec["fields"] == {"pole_number": "P-17", "lamps": 3, "safe": True}, "its answers are kept, checked again")
flyr = fd.open_layer(fsrv.store.folder, add=False)
fd.form_columns(flyr, fsrv.store.form, [dict(frec, fields=dict(frec["fields"], old_question="x"))])
fl = flyr.fields()
check(all(fl.indexOf(n) >= 0 for n in names) and fl.indexOf("old_question") >= 0,
      "the layer has a column for each question (and for answers of a removed one)")
check(fl.field("lamps").type() == fd.QMetaType.Type.Int and fl.field("safe").type() == fd.QMetaType.Type.Bool
      and fl.field("height_m").type() == fd.QMetaType.Type.Double, "numbers, whole numbers and yes/no keep their type")
check(flyr.attributeAlias(fl.indexOf("حالة_العمود")) == "حالة العمود"
      and flyr.editorWidgetSetup(fl.indexOf("حالة_العمود")).type() == "ValueMap",
      "on the PC the question is the column's title, and a choice is a list")
fd.add_records(flyr, [frec])
ff = next(flyr.getFeatures())
check(ff["pole_number"] == "P-17" and ff["lamps"] == 3 and ff["safe"] is True, "the answers are on the point")
again = fd.form_columns(flyr, fsrv.store.form)
check(flyr.fields().count() == fl.count(), "opening again adds no column twice")
check(flyr.fields().indexOf("avg_n") >= 0 and flyr.fields().indexOf("avg_sd_m") >= 0, "the averaged-position columns")

# ---- the team: positions from the phones -------------------------------------------------------------------
check(fs.clean_pos({"id": "dev1234567", "lat": 24.7, "lon": 46.6, "name": "حاتم", "acc": 5})["name"] == "حاتم",
      "a phone's position is read")
check(fs.clean_pos({"id": "x", "lat": 24.7, "lon": 46.6}) is None and fs.clean_pos({"id": "dev1234567", "lat": 99,
                                                                                      "lon": 46.6}) is None,
      "a bad id or position is refused")
c = http.client.HTTPSConnection("127.0.0.1", port + 1, context=ctx, timeout=15)
c.request("POST", f"/{fsrv.token}/pos", body=json.dumps({"id": "dev1234567", "lat": 24.71, "lon": 46.67,
                                                         "name": "Ali", "acc": 4, "t": int(time.time() * 1000)}))
check(c.getresponse().status == 200, "the phone's position is accepted")
c.close()
pos = fsrv.events.get(timeout=5)
check(pos[0] == "pos" and pos[1]["name"] == "Ali", "and handed to the panel")
live, track = fd.team_layers(fsrv.store.folder)
fd.update_team(live, track, pos[1])
fd.update_team(live, track, dict(pos[1], lat=24.72, seen="2026-10-30T10:00:05+00:00"))
fd.update_team(live, track, dict(pos[1], id="dev7654321", name="Sara"))
check(live.featureCount() == 2 and track.featureCount() == 3, "one live point per phone, every position on the track")
moved = next(f for f in live.getFeatures() if f["device"] == "dev1234567")
check(abs(moved.geometry().asPoint().y() - 24.72) < 1e-9, "the phone's point moves with it")
check(fd.team_layers(fsrv.store.folder)[0].id() == live.id(), "the team layers are made once")

# ---- the guide: features sent to the phones ------------------------------------------------------------------
from qgis.core import QgsFeature, QgsGeometry, QgsVectorLayer  # noqa: E402

polys = QgsVectorLayer("Polygon?crs=EPSG:32638&field=name:string", "plots", "memory")
pf = QgsFeature(polys.fields())
pf.setGeometry(QgsGeometry.fromWkt("POLYGON((668000 2734000, 668100 2734000, 668100 2734100, 668000 2734100, "
                                   "668000 2734000),(668040 2734040, 668060 2734040, 668060 2734060, "
                                   "668040 2734060, 668040 2734040))"))
pf["name"] = "قطعة 12"
polys.dataProvider().addFeatures([pf])
polys.setDisplayExpression('"name"')
items = fd.guide_items(polys, list(polys.getFeatures()))
check(len(items) == 1 and items[0]["k"] == "poly" and items[0]["n"] == "قطعة 12" and len(items[0]["c"]) == 2,
      "a polygon with a hole goes as its rings, named")
check(45 < items[0]["c"][0][0][0] < 48 and 24 < items[0]["c"][0][0][1] < 25, "in longitude / latitude")
pts = QgsVectorLayer("MultiPoint?crs=EPSG:4326", "pts", "memory")
mp = QgsFeature(pts.fields())
mp.setGeometry(QgsGeometry.fromWkt("MULTIPOINT((46.6 24.7),(46.61 24.71))"))
pts.dataProvider().addFeatures([mp])
check([i["k"] for i in fd.guide_items(pts, list(pts.getFeatures()))] == ["pt", "pt"], "multi points go one by one")
n = fsrv.set_guide(items)
c = http.client.HTTPSConnection("127.0.0.1", port + 1, context=ctx, timeout=15)
c.request("GET", f"/{fsrv.token}/guide")
g = json.loads(c.getresponse().read())
c.close()
check(n == 1 and g["v"] == 1 and g["items"][0]["n"] == "قطعة 12", "the phones read the guide")
big = [{"k": "line", "n": str(i), "c": [[[46 + j / 1e4, 24.0] for j in range(400)]]} for i in range(100)]
check(len(fs.guide_bytes(2, big)[0]) <= fs.MAX_GUIDE + 200, "a guide too big is cut to what one message carries")
# ---- inside a building: rooms to the phones, room codes, photos placed in a room -----------------------------
units = QgsVectorLayer("Polygon?crs=EPSG:32638&field=unit_id:string&field=level_id:integer&field=name:string"
                       "&field=number:string&field=category:string", "units", "memory")
for uid, lv, name, num, cat, wkt in (
        ("L1-101", 1, "مكتب", "101", "office", "POLYGON((668000 2734000,668006 2734000,668006 2734005,668000 2734005,668000 2734000))"),
        ("L1-102", 1, "Lab", "102", "lab", "POLYGON((668006 2734000,668014 2734000,668014 2734005,668006 2734005,668006 2734000))"),
        ("L1-C01", 1, "ممر", "", "corridor", "POLYGON((668000 2734005,668014 2734005,668014 2734007,668000 2734007,668000 2734005))"),
        ("L2-201", 2, "", "201", "classroom", "POLYGON((668000 2734000,668010 2734000,668010 2734007,668000 2734007,668000 2734000))")):
    uf = QgsFeature(units.fields())
    uf.setGeometry(QgsGeometry.fromWkt(wkt))
    for k, v in (("unit_id", uid), ("level_id", lv), ("name", name), ("number", num), ("category", cat)):
        uf[k] = v
    units.dataProvider().addFeatures([uf])
lv_list, rooms = fd.indoor_items(units)
check([x["id"] for x in lv_list] == [1, 2] and len(rooms) == 4, "levels and rooms are read from the indoor map")
r101 = next(r for r in rooms if r["id"] == "L1-101")
check(46 < r101["p"][0] < 48 and 3.5 < r101["r"] < 4.5 and r101["num"] == "101", "each room: a point inside, its size, its number")
payload = fs.indoor_payload(lv_list, rooms)
fsrv.set_indoor(payload)
c = http.client.HTTPSConnection("127.0.0.1", port + 1, context=ctx, timeout=15)
c.request("GET", f"/{fsrv.token}/guide")
g2 = json.loads(c.getresponse().read())
c.close()
check(g2["v"] > g["v"] and len(g2["indoor"]["units"]) == 4 and g2["items"][0]["n"] == "قطعة 12",
      "the indoor map goes to the phones with the guide (the guide is kept)")
check(fs.clean_meta({"id": "abcdef12-0100", "lat": 24.7, "lon": 46.6, "level": 2, "unit": "L2-201"})["unit"] == "L2-201"
      and fs.clean_meta({"id": "abcdef12-0100", "lat": 24.7, "lon": 46.6, "level": 2.5})["level"] is None,
      "a photo keeps its level and room")
check(fd.room_url("L1-101", "101 مكتب", 1).startswith(fd.ROOM_URL + "#u=L1-101&n=") and "&l=1" in fd.room_url("L1-101", "x", 1),
      "a room code's address")
pdf_path = os.path.join(work, "room_codes.pdf")
check(fd.room_labels_pdf(units, pdf_path) == 3 and os.path.getsize(pdf_path) > 5000,
      "room codes for every room but the corridor, as a PDF to print")
from biruni.field_photos.room_page import TEXTS as ROOM_TEXTS, room_page  # noqa: E402

check(set(ROOM_TEXTS) == {"en", "ar", "es", "pt_BR", "fr", "tr", "fa"} and "biruni-room" in room_page(),
      "the room page: every language, and it notes the room for the photo page")
fsrv.stop()
del flyr

# ---- the page script compiles ----------------------------------------------------------
node = shutil.which("node") or next((p for p in (r"C:\Program Files\nodejs\node.exe",) if os.path.isfile(p)), None)
if node:
    js = html.split("<script>")[1].split("</script>")[0]
    path = os.path.join(work, "page.js")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(js)
    out = subprocess.run([node, "--check", path], capture_output=True, text=True)
    check(out.returncode == 0, "the phone page script has no syntax error " + out.stderr[:200])
else:
    print("SKIP node not found: page script not compiled")

QgsProject.instance().clear()
shutil.rmtree(work, ignore_errors=True)
print("ALL PASS" if not failures else f"{len(failures)} FAILED")
app.exitQgis()
sys.exit(1 if failures else 0)
