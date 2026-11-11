"""Accounts for the live paper (dashboard/accounts.py + ext_accounts.py + the paper's address QR in paper.py):
passwords kept only as scrypt hashes, wrong passwords, the lockout, the visitor template (no hidden field and no
label reaches a visitor), a user's zone, cards that expire, signing out; the page opened from a paper's QR
(#p=…) asks visitor / sign in and goes straight to the paper's camera view (the rear camera is asked for).

Run:  engine\\bin\\python-qgis.bat tests\\test_dashboard_accounts.py
"""
import json
import os
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "app", "plugins"))
sys.path.insert(0, os.path.join(HERE, "..", "app", "python"))
os.environ["QT_QPA_PLATFORM"] = "offscreen"
os.environ["QTWEBENGINE_DISABLE_SANDBOX"] = "1"
os.environ["QTWEBENGINE_CHROMIUM_FLAGS"] = "--disable-gpu --no-sandbox"
os.environ.setdefault("QGIS_CUSTOM_CONFIG_PATH", tempfile.mkdtemp())

from PyQt6.QtCore import QCoreApplication, Qt  # noqa: E402

QCoreApplication.setAttribute(Qt.ApplicationAttribute.AA_ShareOpenGLContexts)
BROWSER = "--browser" in sys.argv
failures = []


def check(cond, msg):
    print(("PASS " if cond else "FAIL ") + msg, flush=True)
    if not cond:
        failures.append(msg)


if not BROWSER:
    from qgis.core import QgsApplication, QgsFeature, QgsGeometry, QgsProject, QgsVectorLayer  # noqa: E402

    app = QgsApplication([], True)
    app.initQgis()
    from biruni.dashboard import accounts, cards, ext_accounts, model, paper, seals, service  # noqa: E402,F401

    cards._TEST_SEED[:] = [bytes(range(32))]
    seals._PATH[:] = [os.path.join(tempfile.mkdtemp(), "seals.sqlite")]
    prj = QgsProject.instance()
    lyr = QgsVectorLayer("Polygon?crs=EPSG:4326&field=no:string&field=district:string&field=status:string"
                         "&field=area:double&field=owner:string", "parcels", "memory")
    fs = []
    for i in range(30):
        x, y = (i % 6) * 0.01, (i // 6) * 0.01
        f = QgsFeature(lyr.fields())
        f.setGeometry(QgsGeometry.fromWkt(f"POLYGON(({x} {y},{x + .008} {y},{x + .008} {y + .008},{x} {y + .008},{x} {y}))"))
        f.setAttributes([f"P-{i:02d}", "rayhan" if x < 0.03 else "nada", ["done", "todo"][i % 2], 500.0 + i,
                         "SECRET-OWNER-%d" % i])
        fs.append(f)
    lyr.dataProvider().addFeatures(fs)
    prj.addMapLayer(lyr)
    D = model.blank("Paper test")
    D["sources"] = {"s1": {"layer": lyr.id(), "filter": ""}}
    D["widgets"] = [
        {"id": "m1", "type": "map", "title": "Map", "x": 0, "y": 0, "w": 8, "h": 8, "source": "s1",
         "options": {"layers": [{"source": "s1", "style": {"kind": "categories", "field": "status"}, "label": "no",
                                 "popup": ["no", "district", "area", "owner"]}]}},
        {"id": "k1", "type": "kpi", "title": "Parcels", "x": 8, "y": 0, "w": 4, "h": 2, "source": "s1",
         "measure": {"op": "count"}},
        {"id": "t1", "type": "table", "title": "Rows", "x": 8, "y": 2, "w": 4, "h": 6, "source": "s1",
         "options": {"fields": ["no", "district", "area", "owner"]}}]
    model.save(prj, D)
    DID = D["id"]

    # ---- hashing ---------------------------------------------------------------------------------------------------
    salt, h = accounts.hash_password("1234")
    check(accounts.check_password("1234", salt, h) and not accounts.check_password("1235", salt, h),
          "scrypt hash checks the right password only")
    rec = accounts.set_user(DID, "Mona", "1234", "منى", project=prj)
    accounts.set_user(DID, "sami", "abcd", "سامي", widgets=["m1", "t1"], hide=["area", "owner"],
                      zone="POLYGON((-0.001 -0.001,0.0285 -0.001,0.0285 0.06,-0.001 0.06,-0.001 -0.001))", project=prj)
    stored = json.dumps(model._entries(prj, accounts.ENTRY), ensure_ascii=False)
    check("1234" not in stored and "abcd" not in stored and rec["hash"] in stored, "no plain password is kept")
    try:
        accounts.set_user(DID, "x", None, project=prj)
        check(False, "a new user needs a password")
    except ValueError:
        check(True, "a new user needs a password")

    # ---- visitor template --------------------------------------------------------------------------------------------
    v = accounts.set_visitor(DID, True, project=prj, definition=D)
    check(v["widgets"] == ["m1"] and "status" not in v["hide"] and {"no", "owner", "area"} <= set(v["hide"]),
          "visitor default: the map only, every field but the colour field kept back")

    svc = service.DashboardService(prj)
    pp = paper.make_passport(DID, "m1", "Paper test", [0, 0, 0.06, 0.05], "A4", "landscape", code=12,
                             url_fn=lambda pid: "https://example.invalid/d/#p=" + pid + "&l=ar")
    paper.save_passport(pp, prj)
    check(pp.get("url", "").endswith("#p=" + pp["id"] + "&l=ar") and paper.verify_passport(pp),
          "the passport carries the paper's address and is signed")
    check(pp["frame"][3] <= pp["page"][1] - 8 - paper.qr_side(*pp["page"]) - 4 + 1e-6,
          "the map frame leaves room for the bigger QR code")

    def session():
        box = []
        return svc.connect(box.append, "lan"), box

    s, box = session()
    s.receive({"t": "acct_info", "paper": pp["id"]})
    check(box and box[-1]["t"] == "acct_info" and box[-1]["visitor"] and box[-1]["users"], "acct_info before a hello")
    s.receive({"t": "acct_visitor", "paper": pp["id"]})
    c = next((m for m in box if m["t"] == "acct_card"), None)
    dash = next((m for m in box if m["t"] == "dash"), None)
    data = [m for m in box if m["t"] == "data"]
    check(c and c["who"] == "visitor" and s.ready, "a visitor gets a card and the dashboard")
    check(dash and [w["id"] for w in dash["def"]["widgets"]] == ["m1"], "the visitor sees the map only")
    blob = json.dumps(data, ensure_ascii=False)
    check(data and "SECRET-OWNER" not in blob and "P-0" not in blob and "500.0" not in blob,
          "no hidden field, no label, no area reaches a visitor")
    check("done" in blob or "#" in blob, "the visitor still gets the colours")
    check(c["exp"] - time.time() < accounts.VISITOR_HOURS * 3600 + 5, "a visitor card is short-lived")
    check(not any(x.get("id") == cards.parse(c["card"])["id"] for x in model.issued_cards(prj, DID)),
          "visitor cards do not fill the project's list of cards")

    # ---- users ---------------------------------------------------------------------------------------------------------
    s2, box2 = session()
    s2.receive({"t": "acct_login", "paper": pp["id"], "user": "mona", "password": "nope"})
    check(box2[-1]["t"] == "acct_err" and box2[-1]["why"] == "bad login" and not s2.ready, "wrong password refused")
    s3, box3 = session()
    s3.receive({"t": "acct_login", "paper": pp["id"], "user": " MONA ", "password": "1234"})
    c3 = next((m for m in box3 if m["t"] == "acct_card"), None)
    check(c3 and c3["who"] == "user:mona" and s3.ready, "a user signs in (name case and spaces do not matter)")
    d3 = next((m for m in box3 if m["t"] == "dash"), None)
    check(d3 and len(d3["def"]["widgets"]) == 3, "mona sees the whole dashboard")

    s4, box4 = session()
    s4.receive({"t": "acct_login", "paper": pp["id"], "user": "sami", "password": "abcd"})
    b4 = json.dumps([m for m in box4 if m["t"] == "data"], ensure_ascii=False)
    check(s4.ready and "SECRET-OWNER" not in b4 and "500.0" not in b4, "sami: area and owner never reach him")
    tbl = next((m["widgets"].get("t1") for m in box4 if m["t"] == "data" and "t1" in m["widgets"]), None)
    rows = (tbl or {}).get("rows") or []
    check(rows and all("nada" not in json.dumps(r) for r in rows), "sami's zone: only his district's rows")

    # ---- lockout ------------------------------------------------------------------------------------------------------
    now = time.time()
    for i in range(accounts.LOCK_TRIES):
        try:
            accounts.login(DID, "mona", "bad%d" % i, prj, now=now + i * 2)
        except accounts.AccountError as e:
            last = e.why
    check(last == "locked", "5 wrong passwords lock the name")
    try:
        accounts.login(DID, "mona", "1234", prj, now=now + 20)
        check(False, "locked even with the right password")
    except accounts.AccountError as e:
        check(e.why == "locked", "locked even with the right password")
    card, payload = accounts.login(DID, "mona", "1234", prj, now=now + accounts.LOCK_FOR + 30)
    check(payload["acct"] == "user:mona", "the lock ends after 5 minutes")

    class Sess:
        pass

    ss = Sess()
    try:
        accounts.login(DID, "sami", "x", prj, now=1000.0, session=ss)
    except accounts.AccountError:
        pass
    try:
        accounts.login(DID, "sami", "abcd", prj, now=1000.3, session=ss)
        check(False, "one try a second per session")
    except accounts.AccountError as e:
        check(e.why == "slow down", "one try a second per session")
    accounts.set_user(DID, "sami", None, "سامي", disabled=True, project=prj)
    try:
        accounts.login(DID, "sami", "abcd", prj, now=time.time() + 4000)
        check(False, "a turned-off account cannot sign in")
    except accounts.AccountError as e:
        check(e.why == "disabled", "a turned-off account cannot sign in")

    # ---- expiry and sign-out -----------------------------------------------------------------------------------------
    p3 = cards.parse(c3["card"])
    try:
        cards.parse(c3["card"], now=p3["exp"] + 1)
        check(False, "a session card expires")
    except cards.CardError as e:
        check(e.why == "expired", "a session card expires")
    box3.clear()
    s3.receive({"t": "acct_logout"})
    check(box3 and box3[-1]["t"] == "acct_out", "sign out answered")
    s5, box5 = session()
    s5.receive({"t": "hello", "card": c3["card"]})
    check(box5 and box5[-1].get("why") == "revoked", "a signed-out card cannot come back")

    # ---- visitors off -----------------------------------------------------------------------------------------------
    accounts.set_visitor(DID, False, project=prj, definition=D)
    s6, box6 = session()
    s6.receive({"t": "acct_visitor", "paper": pp["id"]})
    check(box6[-1]["t"] == "acct_err" and box6[-1]["why"] == "no visitors" and not s6.ready, "visitors can be turned off")

    # ---- the page (another process: QtWebEngine) ---------------------------------------------------------------------
    env = dict(os.environ, BIRUNI_TEST_PP=json.dumps(dict(pp, dash="d_t")))
    r = subprocess.run([sys.executable, os.path.abspath(__file__), "--browser"], capture_output=True, text=True,
                       encoding="utf-8", errors="replace", timeout=300, env=env)
    for line in r.stdout.splitlines():
        if line.startswith(("PASS ", "FAIL ")):
            print(line, flush=True)
            if line.startswith("FAIL "):
                failures.append(line[5:])
    if r.returncode not in (0, 1):
        check(False, "the page test ran (" + r.stderr[-400:] + ")")
    svc.stop()
else:
    import time as _t

    from PyQt6.QtCore import QUrl  # noqa: E402
    from PyQt6.QtWebEngineCore import QWebEnginePage, QWebEngineProfile  # noqa: E402
    from PyQt6.QtWebEngineWidgets import QWebEngineView  # noqa: E402
    from PyQt6.QtWidgets import QApplication  # noqa: E402

    app = QApplication(sys.argv[:1])
    from biruni.dashboard import page as P  # noqa: E402

    errs = []

    class Pg(QWebEnginePage):
        def javaScriptConsoleMessage(self, level, message, line, source):  # noqa: N802
            if level == QWebEnginePage.JavaScriptConsoleMessageLevel.ErrorMessageLevel:
                errs.append(f"{message} ({line})")

    def pump(sec):
        end = _t.monotonic() + sec
        while _t.monotonic() < end:
            app.processEvents()
            _t.sleep(0.01)

    def js(pg, expr):
        out = {}
        pg.runJavaScript(expr, lambda r: out.update(v=r))
        end = _t.monotonic() + 10
        while "v" not in out and _t.monotonic() < end:
            app.processEvents()
            _t.sleep(0.01)
        return out.get("v")

    DEF = {"v": 1, "id": "d_t", "title": "Paper test", "theme": "biruni-dark", "columns": 12, "dates": "greg",
           "widgets": [{"id": "m1", "type": "map", "title": "Map", "x": 0, "y": 0, "w": 8, "h": 6},
                       {"id": "k1", "type": "kpi", "title": "Parcels", "x": 8, "y": 0, "w": 4, "h": 2}]}
    sq = lambda x, y: {"type": "Polygon", "coordinates": [[[x, y], [x + .008, y], [x + .008, y + .008], [x, y + .008], [x, y]]]}  # noqa: E731
    MAP = {"layers": [{"id": "L1", "name": "Parcels", "kind": "fill", "style": {"opacity": .6}, "legend": [], "label": True,
                       "popup": [], "geojson": {"type": "FeatureCollection", "features": [
                           {"type": "Feature", "id": i, "geometry": sq((i % 5) * .01, (i // 5) * .01),
                            "properties": {"id": i, "_c": "#43a047", "_l": "P-%d" % i}} for i in range(10)]}}],
           "bbox": [0, 0, .05, .02], "total": 10}
    PP = json.loads(os.environ["BIRUNI_TEST_PP"])
    SHIM = r"""<script>window.BIRUNI_ACCT_SIM=true;window.__gum=[];
navigator.mediaDevices=navigator.mediaDevices||{};navigator.mediaDevices.getUserMedia=function(c){window.__gum.push(c);return Promise.reject(new Error('no camera here'))};
</script>"""
    DRV = r"""<script>(function(){const b=window.BiruniDash,DASH=__DASH__,MAP=__MAP__,PP=__PP__;let role=null;
b.onSend=function(m){if(!m||!m.t)return;setTimeout(()=>{
 if(m.t==='acct_info')b.feed({t:'acct_info',title:'Paper test',visitor:true,users:true});
 else if(m.t==='acct_visitor'){role='visitor';b.feed({t:'acct_card',card:'SIM.visitor',who:'visitor',exp:Date.now()/1000+3600,name:'Visitor'});go()}
 else if(m.t==='acct_login'){if(m.user==='sami'&&m.password==='1234'){role='sami';b.feed({t:'acct_card',card:'SIM.sami',who:'user:sami',exp:Date.now()/1000+3600,name:'Sami'});go()}
  else b.feed({t:'acct_err',why:'bad login'})}
 else if(m.t==='acct_logout')b.feed({t:'acct_out'});
 else if(m.t==='paper_list')b.feed({t:'paper_list',papers:[PP]});},30)};
function go(){const d=JSON.parse(JSON.stringify(DASH));if(role==='visitor')d.def.widgets=d.def.widgets.filter(w=>w.id==='m1');d.card={name:role};
 b.feed(d);b.feed({t:'data',rev:1,ts:Date.now()/1000,widgets:{m1:MAP,k1:{value:10}}})}
})();</script>"""
    html = P.page_html("ar", "preview")
    k = html.index('<div class="toast"')
    html = html[:k] + SHIM + html[k:]
    drv = (DRV.replace("__DASH__", json.dumps({"t": "dash", "def": DEF, "card": {}, "pc": "PC", "pub": "00"}))
           .replace("__MAP__", json.dumps(MAP)).replace("__PP__", json.dumps(PP)))
    i = html.rfind("</body>")
    html = html[:i] + drv + html[i:]
    v = QWebEngineView()
    pg = Pg(QWebEngineProfile.defaultProfile())
    v.setPage(pg)
    v.resize(420, 860)
    v.show()
    pg.setHtml(html, QUrl("https://paper.invalid/paper/#p=" + PP["id"] + "&l=ar"))
    pump(2)
    js(pg, "try{localStorage.clear()}catch(e){};1")     # a phone that never scanned this paper
    pg.setHtml(html, QUrl("https://paper.invalid/paper/#p=" + PP["id"] + "&l=ar"))
    pump(3)
    check(js(pg, "!document.getElementById('acctGate').classList.contains('hide')") is True,
          "a scanned paper (#p=) asks: visitor or sign in")
    check(js(pg, "document.getElementById('acctVisit').textContent") == "المتابعة كزائر", "the gate speaks Arabic")
    js(pg, "document.getElementById('acctVisit').click()")
    pump(2.5)
    check(js(pg, "document.getElementById('acctGate').classList.contains('hide')") is True, "visitor: the gate closes")
    check(js(pg, "!document.getElementById('paperView').classList.contains('hide')") is True,
          "the page goes straight to the live paper view")
    g = js(pg, "JSON.stringify(window.__gum)") or "[]"
    check("environment" in g, "the rear camera is asked for (then the demo when there is none)")
    check(js(pg, "JSON.parse(localStorage.getItem('biruni-acct:paper.invalid/paper/')||'{}').who") == "visitor",
          "the card is kept on the phone")
    check(js(pg, "document.querySelectorAll('#grid .w').length") == 1, "the visitor's dashboard has the map only")
    js(pg, "document.querySelector('#paperView .acctPB').click()")
    pump(0.5)
    check(js(pg, "!document.getElementById('acctGate').classList.contains('hide')&&"
                 "document.getElementById('acctVisit').style.display==='none'") is True,
          "sign in from the paper view opens the form")
    js(pg, "document.getElementById('acctUser').value='sami';document.getElementById('acctPass').value='bad';"
           "document.getElementById('acctForm').requestSubmit()")
    pump(1)
    check(bool(js(pg, "document.getElementById('acctMsg').textContent")), "a wrong password is said")
    js(pg, "document.getElementById('acctPass').value='1234';document.getElementById('acctForm').requestSubmit()")
    pump(2)
    check(js(pg, "document.getElementById('acctGate').classList.contains('hide')") is True and
          js(pg, "JSON.parse(localStorage.getItem('biruni-acct:paper.invalid/paper/')||'{}').who") == "user:sami",
          "sami signs in; the view changes live")
    check(js(pg, "document.querySelectorAll('#grid .w').length") == 2, "sami's dashboard (the upgrade) is shown")
    check("Sami" in (js(pg, "document.querySelector('#paperView .acctPB').textContent") or ""),
          "the paper view says who is signed in")
    check(not errs, "no JavaScript errors" + ("" if not errs else ": " + "; ".join(errs[:3])))

# ---- a new paper address (settings of this test only: QGIS_CUSTOM_CONFIG_PATH is a temporary folder) ----------
if not BROWSER:
    from biruni.dashboard import accounts as _acc, ext_accounts as _ext  # noqa: E402
    from biruni.field_photos import bridge as _br  # noqa: E402

    k1 = _acc.stable("bridge", _br.new_key)
    check(_acc.stable("bridge", _br.new_key) == k1 and len(_br.pin(k1)) == 22, "the paper key and its PC pin stay the same")
    pin1 = _br.pin(k1)
    _br._IDS.clear()
    check(_br.pin(k1) == pin1, "... also after a restart (the identity is kept with the key)")
    check("bridge" in _acc.reset_stable(), "a new paper address forgets the kept key")
    k2 = _acc.stable("bridge", _br.new_key)
    check(k2 != k1 and _br.pin(k2) != pin1, "the next paper gets a new key and a new PC identity")
    check(any(a[0] == "acct_new_address" for a in _ext.ACTIONS), "the panel offers New paper address…")

print("ALL PASSED" if not failures else f"FAILED: {len(failures)}", flush=True)
sys.exit(1 if failures else 0)
