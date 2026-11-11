"""The live dashboard's PC side: the LAN server end to end (a card's hello → the dashboard and its numbers, a long
poll that wakes on an edit, a wrong token, an expired card, an oversize body), the host page for viewers from
anywhere, the builder (widgets, styles, undo, save and reload, templates), the panel (cards issued, revoked) and
the live preview (the page in a browser, fed the unsaved definition).

Run:  engine\\bin\\python-qgis.bat tests\\test_dashboard_desk.py
"""
import json
import os
import ssl
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "app", "plugins"))
sys.path.insert(0, os.path.join(HERE, "..", "app", "python"))

from qgis.PyQt.QtCore import Qt  # noqa: E402

from qgis.core import (QgsApplication, QgsFeature, QgsGeometry, QgsProject, QgsVectorLayer)  # noqa: E402

os.environ["QGIS_CUSTOM_CONFIG_PATH"] = tempfile.mkdtemp()
app = QgsApplication([], True)
app.initQgis()

from biruni.dashboard import builder, cards, dock, host, model, seals, server, sharing  # noqa: E402
from biruni.dashboard.service import DashboardService  # noqa: E402

failures = []
TMP = tempfile.mkdtemp()
cards._TEST_SEED.append(bytes(range(32)))
seals._PATH.append(os.path.join(TMP, "seals.sqlite"))


def check(cond, msg):
    print(("PASS " if cond else "FAIL ") + msg, flush=True)
    if not cond:
        failures.append(msg)


def pump(seconds=0.1):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        app.processEvents()
        time.sleep(0.01)


def wait_for(pred, seconds=10):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        app.processEvents()
        if pred():
            return True
        time.sleep(0.02)
    return pred()


CTX = ssl.create_default_context()
CTX.check_hostname = False
CTX.verify_mode = ssl.CERT_NONE


def http(method, url, body=None, timeout=40, raw=None):
    """(status, json or text) - run in a thread while the main thread pumps Qt."""
    out = {}

    def run():
        data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
        req = urllib.request.Request(url, data=data, method=method)
        if data is not None:
            req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, context=CTX, timeout=timeout) as r:
                txt = r.read().decode("utf-8")
                out["r"] = (r.status, txt)
        except urllib.error.HTTPError as e:
            out["r"] = (e.code, e.read().decode("utf-8", "replace"))
        except Exception as e:  # noqa: BLE001
            out["r"] = (0, str(e))

    t = threading.Thread(target=run, daemon=True)
    t.start()
    while t.is_alive():
        app.processEvents()
        time.sleep(0.01)
    st, txt = out["r"]
    try:
        return st, json.loads(txt)
    except ValueError:
        return st, txt


def start_async(method, url, body=None, timeout=40):
    out = {}

    def run():
        out["r"] = http_raw(method, url, body, timeout)

    t = threading.Thread(target=run, daemon=True)
    t.start()
    return t, out


def poll_until(base, sid, n, pred, seconds=25, action=None):
    """Long-poll the session until pred(msg) holds for a message (action() runs once the first poll waits).
    -> (found message or None, last count)"""
    end = time.monotonic() + seconds
    first = True
    while time.monotonic() < end:
        t, holder = start_async("GET", base + f"ev?s={sid}&n={n}")
        if first and action is not None:
            pump(0.3)
            action()
        first = False
        while t.is_alive() and time.monotonic() < end:
            app.processEvents()
            time.sleep(0.01)
        st, r = holder.get("r", (0, {}))
        if not isinstance(r, dict):
            return None, n
        n = r.get("n") or n
        for m in r.get("out") or []:
            if pred(m):
                return m, n
        if any(m.get("t") == "bye" for m in r.get("out") or []):
            return None, n
    return None, n


def http_raw(method, url, body=None, timeout=40):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    try:
        with urllib.request.urlopen(req, context=CTX, timeout=timeout) as r:
            return r.status, json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")
    except Exception as e:  # noqa: BLE001
        return 0, str(e)


# ---- the data ------------------------------------------------------------------------------------------------
parcels = QgsVectorLayer("Polygon?crs=EPSG:4326&field=district:string&field=status:string&field=area:double"
                         "&field=owner:string&field=surveyed:date", "Parcels", "memory")
zones = QgsVectorLayer("Polygon?crs=EPSG:4326&field=name:string", "Districts", "memory")
feats = []
for i in range(30):
    f = QgsFeature(parcels.fields())
    x, y = 46.0 + (i % 6) * 0.01, 24.0 + (i // 6) * 0.01
    f.setGeometry(QgsGeometry.fromWkt(f"POLYGON(({x} {y},{x + .008} {y},{x + .008} {y + .008},{x} {y + .008},{x} {y}))"))
    f.setAttributes(["North" if i < 18 else "South", "done" if i % 3 == 0 else "open", 400.0 + i, f"Owner {i}",
                     None])
    feats.append(f)
parcels.dataProvider().addFeatures(feats)
for name, wkt in (("North", "POLYGON((45.99 23.99,46.07 23.99,46.07 24.035,45.99 24.035,45.99 23.99))"),
                  ("South", "POLYGON((45.99 24.035,46.07 24.035,46.07 24.06,45.99 24.06,45.99 24.035))")):
    z = QgsFeature(zones.fields())
    z.setGeometry(QgsGeometry.fromWkt(wkt))
    z.setAttributes([name])
    zones.dataProvider().addFeature(z)
QgsProject.instance().addMapLayers([parcels, zones])

# ---- templates -------------------------------------------------------------------------------------------------
for name in builder.TEMPLATES:
    d = builder.template(name, {"main": parcels, "zones": zones})
    probs = model.validate(d)
    check(not probs and len(d["widgets"]) >= 3, f"template «{name}»: {len(d['widgets'])} widgets, valid "
          f"{probs[:2] if probs else ''}")
d = builder.template("Survey progress", {"main": parcels, "zones": zones})
types = {w["type"] for w in d["widgets"]}
check({"kpi", "gauge", "bar", "progress", "map"} <= types, f"survey progress has numbers, a gauge, bars, zones and a map "
      f"({sorted(types)})")
done = next(w for w in d["widgets"] if w["type"] == "kpi" and (w.get("measure") or {}).get("where"))
check(done["measure"]["where"] == "\"status\" = 'done'", f"the template found the «done» value ({done['measure']['where']})")

# ---- the builder (no preview) ------------------------------------------------------------------------------------
dlg = builder.BuilderDialog(None, preview=False)
for k in model.TYPES:
    dlg.add(k)
check(len(dlg.d["widgets"]) == 15, "the builder adds every one of the 15 widget types")
xs = [(w["x"], w["y"], w["w"], w["h"]) for w in dlg.d["widgets"]]
overlap = any(a[0] < b[0] + b[2] and b[0] < a[0] + a[2] and a[1] < b[1] + b[3] and b[1] < a[1] + a[3]
              for i, a in enumerate(xs) for b in xs[i + 1:])
check(not overlap and all(x + w <= 12 for x, _y, w, _h in xs), "new widgets are placed without overlapping, inside 12 columns")
probs = dlg.problems_now()
check(not probs, f"a dashboard of all types is valid ({probs[:3]})")
kpi = dlg.d["widgets"][0]
dlg.grid.current = kpi["id"]
dlg.panel.show_widget(dlg.d, kpi["id"])
dlg.panel.layer.setCurrentIndex(dlg.panel.layer.findData(parcels.id()))
dlg.panel.title.setText("Parcels done")
dlg.panel.op.setCurrentIndex(dlg.panel.op.findData("sum"))
dlg.panel.field.setCurrentIndex(dlg.panel.field.findData("area"))
dlg.panel.where.setText("\"status\" = 'done'")
dlg.panel.unit.setText("m²")
dlg.panel.decimals.setValue(1)
dlg.panel.has_target.setChecked(True)
dlg.panel.target.setValue(5000)
dlg.panel.thresholds.setText("1000:#f9a825; 3000:#c62828; nonsense")
dlg.panel.color.set_color("#123456")
dlg.panel._save()
k = next(w for w in dlg.d["widgets"] if w["id"] == kpi["id"])
check(k["title"] == "Parcels done" and k["measure"] == {"op": "sum", "field": "area", "where": "\"status\" = 'done'"},
      f"the panel writes the measure ({k['measure']})")
check(k["style"]["unit"] == "m²" and k["style"]["decimals"] == 1 and k["style"]["target"] == 5000
      and k["style"]["color"] == "#123456" and [t["at"] for t in k["style"]["thresholds"]] == [1000, 3000],
      f"the panel writes the look ({k['style']})")
before = json.dumps(dlg.d, sort_keys=True)
dlg.grid.current = kpi["id"]
k["x"], k["y"] = 6, 20                 # as a drag does
dlg.grid.changed.emit()
check(json.dumps(dlg.d, sort_keys=True) != before, "a move changes the definition")
dlg.undo()
k2 = next(w for w in dlg.d["widgets"] if w["id"] == kpi["id"])
check((k2["x"], k2["y"]) != (6, 20), "undo takes the move back")
dlg.redo()
k3 = next(w for w in dlg.d["widgets"] if w["id"] == kpi["id"])
check((k3["x"], k3["y"]) == (6, 20), "redo makes it again")
dlg.title.setText("Desk test")
dlg._settings()
dlg.theme.setCurrentIndex(dlg.theme.findData("ocean"))
check(dlg.d["theme"] == "ocean" and dlg.theme.count() >= 6, f"themes from the page ({dlg.theme.count()} of them)")
dlg.duplicate()
check(len(dlg.d["widgets"]) == 16, "duplicate adds a copy")
dlg.remove()
check(len(dlg.d["widgets"]) == 15, "remove takes it away")
check(dlg.save(warn=False), f"the builder saves the dashboard {dlg.problems_now()[:3]}")
saved = model.get(QgsProject.instance(), dlg.d["id"])
check(saved is not None and saved["title"] == "Desk test"
      and next(w for w in saved["widgets"] if w["id"] == kpi["id"])["style"]["target"] == 5000,
      "the dashboard is read back from the project with its styles")
dlg2 = builder.BuilderDialog(saved, preview=False)
check(len(dlg2.d["widgets"]) == 15 and dlg2.title.text() == "Desk test", "the builder opens a saved dashboard again")
dlg2.deleteLater()
dlg.deleteLater()

# a dashboard for the server tests
survey = builder.template("Survey progress", {"main": parcels, "zones": zones})
survey["title"] = "Survey"
survey = model.save(QgsProject.instance(), survey)

# ---- the LAN server ------------------------------------------------------------------------------------------------
svc = DashboardService(QgsProject.instance())
share = sharing.Sharing(svc, lang="en", addresses=["127.0.0.1"], port=10176, folder=TMP)
share.start()
base = share.lan.local_url()
check(base.startswith("https://127.0.0.1:") and "/d/" in base, f"the LAN server is up ({base[:40]}…)")
st, page = http("GET", base)
check(st == 200 and isinstance(page, str) and "<html" in page.lower(), "the page is served at its token address")
st, _ = http("GET", base.replace(share.lan.token, "wrongtoken"))
check(st == 404, f"a wrong token gets 404 ({st})")
st, _ = http("POST", base.replace(share.lan.token, "wrongtoken") + "m", {"s": None, "m": {"t": "hello"}})
check(st == 404, "a message to a wrong token gets 404")

card = dock.issue_card(survey, "Supervisor", days=2)
st, r = http("POST", base + "m", {"s": None, "m": {"t": "hello", "card": card, "lang": "en"}})
kinds = [m.get("t") for m in (r.get("out") or [])] if isinstance(r, dict) else []
check(st == 200 and r.get("s") and "dash" in kinds, f"hello with a card → the dashboard ({kinds})")
sid, n = r["s"], r.get("n") or 0
got_data = [m for m in r.get("out") or [] if m.get("t") == "data"]
if not got_data:
    st, r2 = http("GET", base + f"ev?s={sid}&n={n}")
    got_data = [m for m in r2.get("out") or [] if m.get("t") == "data"]
    n = r2.get("n") or n
check(bool(got_data) and got_data[0].get("widgets"), "…and its numbers")
first = got_data[0] if got_data else {"widgets": {}}
dash_msg = next((m for m in r.get("out") or [] if m.get("t") == "dash"), {})
check("sources" not in (dash_msg.get("def") or {}), "the page never gets the sources (layers, expressions)")
kpi_id = next(w["id"] for w in survey["widgets"] if w["type"] == "kpi")
v0 = (first["widgets"].get(kpi_id) or {}).get("value")
check(v0 == 30, f"the first number counts all 30 parcels ({v0})")
seal_code = (first["widgets"].get(kpi_id) or {}).get("seal")
check(bool(seal_code), f"the number carries a seal ({seal_code})")

# long poll wakes on an edit
t, holder = start_async("GET", base + f"ev?s={sid}&n={n}")
pump(0.5)
parcels.startEditing()
f = QgsFeature(parcels.fields())
f.setGeometry(QgsGeometry.fromWkt("POLYGON((46.2 24.2,46.21 24.2,46.21 24.21,46.2 24.2))"))
f.setAttributes(["North", "done", 999.0, "New", None])
parcels.addFeature(f)
parcels.commitChanges()
ok = wait_for(lambda: not t.is_alive(), 20)
st, r3 = holder.get("r", (0, {}))
datas = [m for m in (r3.get("out") or []) if m.get("t") == "data"] if isinstance(r3, dict) else []
newv = next(((m["widgets"].get(kpi_id) or {}).get("value") for m in datas if kpi_id in m.get("widgets", {})), None)
check(ok and st == 200 and newv == 31, f"the long poll wakes on an edit with the new number ({st}, {newv})")
check(all(m.get("rev", 0) > first.get("rev", 0) for m in datas), "…with a later revision")
n = r3.get("n") or n if isinstance(r3, dict) else n

# a filter message gets an answer by the long poll only
st, rf = http("POST", base + "m", {"s": sid, "m": {"t": "ping"}})
check(st == 200 and not rf.get("out"), "later messages are answered by the long poll only (nothing twice)")

# the seal of the number checks out
r = dock.check_seal_text(seal_code)
check(r["ok"] and r["statement"].get("widget") == kpi_id, "the seal code of the number checks out on this PC")
txt = seals.as_text(seal_code)
r = dock.check_seal_text(txt)
check(r["ok"], "the seal text checks out (as another Biruni would)")
st_, sig_ = seals.from_text(txt)
st_["value"] = 12345
check(not seals.verify(st_, sig_), "a seal whose number was changed does not check out")
r = dock.check_seal_text("ZZZZ-ZZZZ")
check(not r["ok"], "an unknown seal code is refused")

# an expired card
old = cards.issue(survey["id"], "Old", exp=time.time() - 60)
st, r = http("POST", base + "m", {"s": None, "m": {"t": "hello", "card": old}})
whys = [m.get("why") for m in (r.get("out") or []) if m.get("t") == "error"] if isinstance(r, dict) else []
check("expired" in whys, f"an expired card is refused ({whys})")
st, r = http("POST", base + "m", {"s": None, "m": {"t": "hello", "card": card[:-6] + "AAAAAA"}})
whys = [m.get("why") for m in (r.get("out") or []) if m.get("t") == "error"] if isinstance(r, dict) else []
check(bool(whys) and "expired" not in whys, f"an altered card is refused ({whys})")

# oversize and malformed bodies
st, _ = http("POST", base + "m", raw=b"{" + b"x" * (server.MAX_BODY + 10) + b"}")
check(st == 413, f"an oversize body gets 413 ({st})")
st, _ = http("POST", base + "m", raw=b"not json")
check(st == 400, f"a body that is not JSON gets 400 ({st})")
st, r = http("GET", base + "ev?s=nosuchsession&n=0")
check(st == 410, f"an unknown session is told to stop ({st})")

# the hub's limits
hub = server.SessionHub()
for _ in range(server.RATE_N):
    hub.rate_ok("10.0.0.9")
check(not hub.rate_ok("10.0.0.9") and hub.rate_ok("10.0.0.8"), "one address cannot flood the PC (rate limit)")
sids = [hub.open("lan", "x") for _ in range(server.MAX_SESSIONS + 3)]
check(sids.count(None) == 3, "no more viewers than the limit")
s1 = sids[0]
for i in range(server.MAX_OUTBOX + 50):
    hub.push(s1, {"t": "data", "rev": i})
check(len(hub.sessions[s1].out) <= server.MAX_OUTBOX, "a viewer that does not read cannot fill the memory")
hub.stop()

# revoking a card ends its session
check(len(share.viewers()) >= 1, f"the service lists the open viewer ({len(share.viewers())})")
panel = dock.DashboardDock(None, service=svc, sharing_factory=lambda s: share)
panel.sharing = share
panel.refresh(select=survey["id"])
issued = [panel.cards.item(i).data(Qt.ItemDataRole.UserRole + 1) for i in range(panel.cards.count())]
check(any(p.get("name") == "Supervisor" for p in issued), f"the panel lists the issued cards ({len(issued)})")
for i in range(panel.cards.count()):
    if panel.cards.item(i).data(Qt.ItemDataRole.UserRole + 1).get("name") == "Supervisor":
        panel.cards.setCurrentRow(i)
p = panel.selected_card()
check(dock.card_text(p) == card, "the card text is made again from its payload (links and QR any time)")
links = share.links(card)
check(links["lan"] and links["lan"][0].startswith("https://127.0.0.1:") and "#c=" + card in links["lan"][0],
      "the LAN link carries the card after #")
pix = dock.qr_pixmap(links["lan"][0])
check(not pix.isNull(), f"the link fits a QR code ({len(links['lan'][0])} characters)")
t, holder = start_async("GET", base + f"ev?s={sid}&n={n}")
panel.revoke(ask=False)
ok = wait_for(lambda: not t.is_alive(), 30)
st, r4 = holder.get("r", (0, {}))
whys = [m.get("why") for m in (r4.get("out") or []) if m.get("t") == "error"] if isinstance(r4, dict) else []
check(ok and "revoked" in whys, f"the open page of a revoked card is told at once ({whys})")
panel._fill_cards()
states = [panel.cards.item(i).text() for i in range(panel.cards.count())]
check(any("Supervisor" in s and "revoked" in s for s in states), "the panel shows the card as revoked")
st, r = http("POST", base + "m", {"s": None, "m": {"t": "hello", "card": card}})
whys = [m.get("why") for m in (r.get("out") or []) if m.get("t") == "error"] if isinstance(r, dict) else []
check("revoked" in whys, "the revoked card cannot open the dashboard again")

# a zone card sees only its zone
north = next(z for z in zones.getFeatures() if z["name"] == "North").geometry()
zc = dock.issue_card(survey, "North only", zone=(north, zones.crs()), hide=["owner"])
st, r = http("POST", base + "m", {"s": None, "m": {"t": "hello", "card": zc}})
datas = [m for m in (r.get("out") or []) if m.get("t") == "data"]
if not datas and r.get("s"):
    st, r5 = http("GET", base + f"ev?s={r['s']}&n={r.get('n') or 0}")
    datas = [m for m in (r5.get("out") or []) if m.get("t") == "data"]
zv = (datas[0]["widgets"].get(kpi_id) or {}).get("value") if datas else None
check(zv is not None and zv < 31, f"a zone card counts only the features in its zone ({zv})")
check("owner" not in json.dumps(datas, ensure_ascii=False), "a field kept back never reaches the page")

# TV mode
tv = panel.tv(open_browser=False)
check(tv and tv.startswith("https://127.0.0.1:") and "tv=1" in tv, "big screen: a local link in TV mode")

# ---- the host page (viewers from anywhere) ----------------------------------------------------------------------------
hp = host.host_page().decode("utf-8")
check("SIG.start" in hp and "bridgeKey" in hp and "'part'" in hp, "the hidden host page meets viewers over the bridge "
      "and splits long messages")
st, txt = http("GET", base + "host")
check(st == 200 and "SIG.start" in str(txt), "the host page is served to this PC itself")
st, r = http("POST", base + "m", {"s": None, "tr": "bridge", "m": {"t": "hello", "card": zc}})
info = share.hub.info(r.get("s")) if isinstance(r, dict) and r.get("s") else None
check(info is not None and info["transport"] == "bridge", "a message relayed by the host page counts as a bridge viewer")
u = host.viewer_url(zc, "KEY", "ar")
check(u.startswith(host.PAGE_URL + "#c=") and "&k=KEY&i=" in u and u.endswith("&l=ar"), "the link from anywhere keeps card, key and the PC pin after #")
pub = host.write_public_page(os.path.join(TMP, "bridge-page"))
check(os.path.isfile(pub) and os.path.getsize(pub) > 10000, "the public page for GitHub Pages is written")

share.stop()
check(share.lan is None, "sharing stops")

# ---- the live preview (the page in a browser, fed the definition being edited) ----------------------------------------
svc2 = DashboardService(QgsProject.instance())
share2 = sharing.Sharing(svc2, lang="en", addresses=["127.0.0.1"], port=10186, folder=TMP)
pdlg = builder.BuilderDialog(survey, lang="en", preview=True, sharing_provider=lambda: share2)
links = pdlg.preview.links()
check(links.get("local", "").startswith("https://127.0.0.1:") and "#c=PREVIEW." in links["local"],
      "the builder gives a preview link for this PC's browser")
check(bool(links.get("lan")) and links["lan"][0].startswith("https://127.0.0.1:"), "…and one for a phone on the network")
pdlg.preview.show_phone()
check(pdlg.preview.qr.pixmap() is not None and not pdlg.preview.qr.pixmap().isNull(), "the phone preview has a QR code")
pbase = share2.lan.local_url()
pcard = links["local"].split("#c=", 1)[1].split("&", 1)[0]
st, r = http("POST", pbase + "m", {"s": None, "m": {"t": "hello", "card": pcard}})
outs = r.get("out") or [] if isinstance(r, dict) else []
pdash = next((m for m in outs if m.get("t") == "dash"), None)
check(st == 200 and pdash is not None and len(pdash["def"]["widgets"]) == len(survey["widgets"]),
      "a browser opened on the preview link gets the dashboard being edited")
psid, pn = r.get("s"), r.get("n") or 0
pump(0.5)
check(pdlg.preview.previewers() == 1, f"the builder counts the open preview ({pdlg.preview.previewers()})")


def _edit():
    pdlg.add("text")
    pdlg._soon.timeout.emit()


m, pn = poll_until(pbase, psid, pn, lambda m: m.get("t") == "dash"
                   and len(m["def"]["widgets"]) == len(survey["widgets"]) + 1, action=_edit)
check(m is not None, "an edit in the builder reaches the open preview at once (unsaved)")
saved_now = model.get(QgsProject.instance(), survey["id"])
check(len(saved_now["widgets"]) == len(survey["widgets"]), "the preview does not save anything")
st, r = http("POST", pbase + "m", {"s": None, "m": {"t": "hello", "card": "PREVIEW." + "x" * 24}})
whys = [m.get("why") for m in (r.get("out") or []) if m.get("t") == "error"] if isinstance(r, dict) else []
check("ended" in whys, "a made-up preview key gets nothing")
m, pn = poll_until(pbase, psid, pn, lambda m: m.get("t") in ("bye", "error"), action=lambda: pdlg.done(0))
check(m is not None, f"closing the builder ends its previews ({m})")
share2.stop()
svc2.stop()
pdlg.deleteLater()
panel.deleteLater()
svc.stop()
pump(0.3)

print()
print("ALL PASSED" if not failures else f"FAILED: {len(failures)}")
QgsProject.instance().removeAllMapLayers()
app.exitQgis()
sys.exit(1 if failures else 0)
