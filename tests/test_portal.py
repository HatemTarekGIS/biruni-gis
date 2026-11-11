"""The signed portal (portal/): crypto round trips, signatures and tampering, groups as keys (adding, removing a
member, rotation), following other portals with pinned keys, expiry and auto-rotation, and the viewer page offscreen
(served on 127.0.0.1 and from file://) - including its pure-JS fallbacks for Ed25519 and X25519."""
import hashlib
import http.server
import json
import os
import shutil
import socketserver
import sys
import tempfile
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "app", "plugins"))
sys.path.insert(0, os.path.join(HERE, "..", "app", "python"))
os.environ["QT_QPA_PLATFORM"] = "offscreen"
os.environ["QTWEBENGINE_DISABLE_SANDBOX"] = "1"
os.environ["QTWEBENGINE_CHROMIUM_FLAGS"] = "--disable-gpu --no-sandbox"

from PyQt6.QtCore import QCoreApplication, Qt, QUrl  # noqa: E402

QCoreApplication.setAttribute(Qt.ApplicationAttribute.AA_ShareOpenGLContexts)
from PyQt6.QtWebEngineCore import QWebEnginePage, QWebEngineProfile  # noqa: E402
from PyQt6.QtWebEngineWidgets import QWebEngineView  # noqa: E402
from PyQt6.QtWidgets import QApplication  # noqa: E402

app = QApplication(sys.argv[:1])
from biruni.portal import core  # noqa: E402
from biruni.portal import crypto as C  # noqa: E402
from biruni.portal import page  # noqa: E402

OWNER = bytes(range(32))
OTHER = bytes(range(100, 132))
core._TEST_SEED.append(OWNER)
failures, errors = [], []


def check(cond, msg):
    print(("PASS " if cond else "FAIL ") + msg, flush=True)
    if not cond:
        failures.append(msg)


def raises(fn, exc=Exception):
    try:
        fn()
    except exc:
        return True
    except Exception:  # noqa: BLE001
        return False
    return False


def wait(cond, seconds):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        app.processEvents()
        try:
            if cond():
                return True
        except Exception:  # noqa: BLE001
            pass
        time.sleep(0.01)
    return False


ROOT = tempfile.mkdtemp(prefix="biruni_portal_test_")
try:
    # ---- 1. crypto ---------------------------------------------------------------------------------------------------
    k = C.new_key()
    check(C.unseal(k, C.seal(k, b"abc", "x"), "x") == b"abc", "AES-GCM round trip")
    check(raises(lambda: C.unseal(k, C.seal(k, b"abc", "x"), "y"), C.PortalCryptoError), "another aad is refused")
    mpriv, mcard = C.new_member_key("سارة")
    card = C.parse_card(mcard)
    check(card["name"] == "سارة" and card["box"] == C.box_public(mpriv).hex(), "a member card round trips")
    env = C.box_to(card["box"], b"group-key", "group:a")
    check(C.box_open(mpriv, env, "group:a") == b"group-key", "an envelope opens with the member key only")
    check(raises(lambda: C.box_open(C.new_key(), env, "group:a")), "... and not with another key")
    kf = C.key_file("سارة", mpriv, "passphrase-123", rounds=1000)
    check(C.read_key_file(kf, "passphrase-123")[1] == mpriv, "a protected key file opens with its passphrase")
    check(raises(lambda: C.read_key_file(kf, "wrong-pass")), "... and not with a wrong one")
    check(raises(lambda: C.parse_card("BPM1.bm90IGEgY2FyZA")), "a broken card is refused")

    # ---- 2. a portal --------------------------------------------------------------------------------------------------
    P1 = os.path.join(ROOT, "p1")
    p = core.Portal.create(P1, "بلدية الوادي", "إدارة المساحة", "بوابة تجريبية")
    gA = p.add_group("فريق المساحة")
    gB = p.add_group("المكتب الاستشاري")
    pub_file = os.path.join(ROOT, "plan.txt")
    with open(pub_file, "w", encoding="utf-8") as f:
        f.write("مخطط عام\n" * 50)
    m_pub = p.add_file(pub_file, "المخطط العام", tags=["مخططات", "عام"], scope="للاطلاع")
    secret = os.urandom(5000) + "سرّي".encode("utf-8")
    m_a = p.add_file(data=secret, name="parcels.gpkg", title="قطع المرحلة الثانية", tags=["قطع"], groups=[gA],
                     scope="للمراجعة فقط")
    m_ab = p.add_file(data=b"shared by both groups", name="both.pdf", title="محضر مشترك", groups=[gA, gB])
    m_b = p.add_file(data=b"only for B", name="b.pdf", title="خاص بالمكتب", groups=[gB])
    m_link = p.add_link("https://example.org/map", "رابط عام", kind="link")
    m_ms = p.add_link("../mapservice/", "خدمة خرائط المخطط", kind="mapservice", tags=["خدمات"])
    m_sl = p.add_link("https://example.org/secret-dash#card", "لوحة الفريق", kind="dashboard", groups=[gA])
    m_exp_r = p.add_file(data=b"expires and rotates", name="e1.txt", title="ينتهي ويُدوَّر", groups=[gA],
                         expires_days=1 / 86400, rotate=True)
    m_exp = p.add_file(data=b"expires", name="e2.txt", title="ينتهي فقط", expires_days=1 / 86400)
    check(os.path.isfile(os.path.join(P1, "index.html")) and os.path.isfile(os.path.join(P1, "portal.js")),
          "a portal folder carries its viewer and portal.js")
    rep = core.verify_doc(core.read_doc(P1))
    check(rep["ok"] and len(rep["items"]) == 9 and all(i["ok"] for i in rep["items"]), "the portal and its items verify")
    check(core.verify_doc(core.read_doc(P1), pinned=p.pub)["ok"], "... with the owner key pinned")
    check(core.verify_doc(core.read_doc(P1), pinned=C.ed_public(OTHER).hex())["why"] == "not the pinned key",
          "... and not with another key pinned")
    check(p.read(m_pub["id"]) == open(pub_file, "rb").read(), "a public item is stored as it is")
    check(p.read(m_a["id"]) == secret, "the owner reopens a sealed item")
    raw = open(os.path.join(P1, m_a["file"]["path"]), "rb").read()
    check(secret not in raw and b"\xd8\xb3" not in raw[:0] and C.sha256_hex(raw) == m_a["file"]["sha256"],
          "a sealed item is stored encrypted, with its stored hash signed")
    check(b"secret-dash" not in open(os.path.join(P1, core.INDEX), "rb").read(), "a sealed link's address is hidden")
    check(core.Portal.open(P1).index["seq"] == p.index["seq"], "the owner reopens the portal")
    check(raises(lambda: core.Portal.open(P1, seed=OTHER), core.PortalError), "another key cannot open it as owner")

    # tampering
    doc = core.read_doc(P1)
    bad = dict(doc, index=doc["index"].replace("المخطط العام", "المخطط المزوّر"))
    check(core.verify_doc(bad)["why"] == "bad signature", "an altered index is refused")
    idx = json.loads(doc["index"])
    fm = json.loads(idx["items"][0]["m"])
    fm["title"] = "عنصر مدسوس"
    forged = C.text_of(fm)
    idx["items"].append({"m": forged, "s": C.sign_text(OTHER, forged), "keys": {}})
    t2 = C.text_of(idx)
    rep2 = core.verify_doc({"v": 1, "index": t2, "sig": C.sign_text(OWNER, t2)})
    check(rep2["ok"] and rep2["items"][-1]["ok"] is False and rep2["items"][-1]["why"] == "bad signature",
          "an item not signed by the owner is flagged, the rest stays trusted")

    # ---- 3. members ---------------------------------------------------------------------------------------------------
    sara_priv, sara_card = C.new_member_key("سارة")
    omar_priv, omar_card = C.new_member_key("عمر")
    check(p.add_member(gA, card=sara_card) and p.add_member(gA, card=omar_card), "members are added by their cards")
    check(not p.add_member(gA, card=sara_card), "a member is not added twice")
    p.add_member(gB, card=omar_card)
    idx = core.verify_doc(core.read_doc(P1))["index"]
    check(core.open_item(P1, idx, m_a["id"], sara_priv) == secret, "a member opens a group item")
    check(core.open_item(P1, idx, m_ab["id"], sara_priv) == b"shared by both groups", "... and an item of two groups")
    check(raises(lambda: core.open_item(P1, idx, m_b["id"], sara_priv), core.PortalError),
          "a member does not open another group's item")
    check(core.open_item(P1, idx, m_sl["id"], sara_priv) == b"https://example.org/secret-dash#card",
          "a member opens a sealed link")
    old_sha = m_a["file"]["sha256"]
    old_idx = idx
    old_blob = open(os.path.join(P1, m_a["file"]["path"]), "rb").read()
    check(p.remove_member(gA, C.box_public(sara_priv).hex()), "a member is removed")
    idx = core.verify_doc(core.read_doc(P1))["index"]
    ga = next(g for g in idx["groups"] if g["id"] == gA)
    check(ga["ver"] == 2 and all(x["box"] != C.box_public(sara_priv).hex() for x in ga["members"]),
          "removing a member replaces the group key")
    new_a = json.loads(next(e for e in idx["items"] if json.loads(e["m"])["id"] == m_a["id"])["m"])
    check(new_a["file"]["sha256"] != old_sha, "the group's files are sealed again")
    check(raises(lambda: core.open_item(P1, idx, m_a["id"], sara_priv), core.PortalError),
          "the removed member no longer opens the new files")
    # with the old group key she kept: it does not unwrap the new content key
    old_gk = C.box_open(sara_priv, next(x for x in next(g for g in old_idx["groups"] if g["id"] == gA)["members"]
                                        if x["box"] == C.box_public(sara_priv).hex())["env"],
                        f"group:{idx['id']}:{gA}:1")
    w = next(e for e in idx["items"] if json.loads(e["m"])["id"] == m_a["id"])["keys"][gA]
    check(raises(lambda: C.unseal(old_gk, C.unb64u(w["w"]), f"cek:{m_a['id']}:{gA}:{w['v']}")),
          "her old group key does not open the new wrapped key")
    check(old_blob != open(os.path.join(P1, m_a["file"]["path"]), "rb").read(), "... and the old file is gone")
    check(core.open_item(P1, idx, m_a["id"], omar_priv) == secret, "the other members still open it")
    check(core.open_item(P1, idx, m_ab["id"], omar_priv) == b"shared by both groups"
          and p.read(m_ab["id"]) == b"shared by both groups", "an item of two groups is rewrapped for both")
    check(not p.remove_member(gA, p.index["owner"]["box"]), "the owner cannot be removed")
    p.add_member(gA, card=sara_card)   # back, for the viewer test

    # ---- 4. following -----------------------------------------------------------------------------------------------
    P2, P3 = os.path.join(ROOT, "p2"), os.path.join(ROOT, "p3")
    q2 = core.Portal.create(P2, "مكتب النخبة", "م. خالد", seed=OTHER)
    q2.add_file(data=b"office deliverable", name="d.dxf", title="تسليم المكتب", tags=["تسليمات"])
    q3 = core.Portal.create(P3, "مكتب مجهول", "؟", seed=bytes(range(50, 82)))
    q3.add_file(data=b"x", name="x.txt", title="عنصر من مكتب لا نثق به")
    p.follow("../p2/", q2.pub, "مكتب النخبة")
    p.follow("../p3/", q2.pub, "مكتب يدّعي أنه النخبة")      # pinned to the wrong key
    p.follow("../p4/", q2.pub, "غير موجود")

    def fetch(url):
        return open(os.path.normpath(os.path.join(P1, url)), "rb").read()

    rows = {r["follow"]["url"]: r for r in core.aggregate(p.index, fetch)}
    check(rows["../p2/"]["status"] == "ok" and len(rows["../p2/"]["report"]["items"]) == 1, "a followed portal is merged")
    check(rows["../p3/"]["status"] == "not the pinned key", "a portal with another key is flagged")
    check(rows["../p4/"]["status"] == "unreachable", "an unreachable portal is flagged")
    shutil.copytree(P2, os.path.join(ROOT, "p2_orig"))
    d2 = core.read_doc(P2)
    with open(os.path.join(P2, core.INDEX), "w", encoding="utf-8") as f:
        json.dump(dict(d2, index=d2["index"].replace("تسليم المكتب", "تسليم مزوّر")), f, ensure_ascii=False)
    rows = {r["follow"]["url"]: r for r in core.aggregate(p.index, fetch)}
    check(rows["../p2/"]["status"] == "bad signature", "a tampered followed portal is flagged")
    shutil.rmtree(P2)
    shutil.copytree(os.path.join(ROOT, "p2_orig"), P2)

    # ---- 5. expiry ----------------------------------------------------------------------------------------------------
    time.sleep(1.2)
    rep = core.verify_doc(core.read_doc(P1))
    ex = {i["manifest"]["id"]: i.get("expired") for i in rep["items"]}
    check(ex[m_exp_r["id"]] and ex[m_exp["id"]] and not ex[m_pub["id"]], "expired items are recognised")
    ver_before = p.group(gA)["ver"]
    gone = p.sweep()
    check(gone == [m_exp_r["id"]] and not os.path.exists(os.path.join(P1, "items", m_exp_r["id"])),
          "sweep takes out the expired item marked auto-rotate (file and keys)")
    check(p.group(gA)["ver"] == ver_before + 1, "... and replaces its group's key")
    check(any(m["id"] == m_exp["id"] for m in p.items()), "an expired item without the mark stays listed")
    idx = core.verify_doc(core.read_doc(P1))["index"]
    check(core.open_item(P1, idx, m_a["id"], sara_priv) == secret, "members still open the remaining items")

    # ---- 6. publishing ------------------------------------------------------------------------------------------------
    pubdir = os.path.join(ROOT, "site", "p1")
    p.publish(pubdir)
    check(core.verify_doc(core.read_doc(pubdir))["ok"], "a published copy verifies")
    hv = page.build_bridge_viewer(os.path.join(ROOT, "bridge"))
    check(os.path.isfile(hv) and "BiruniPortal" in open(hv, encoding="utf-8").read(), "the hosted viewer is built")
    html = page.viewer_html()
    check("<script src=\"http" not in html and "cdn" not in html.lower(), "the viewer loads nothing from outside")

    # ---- 7. the viewer page -------------------------------------------------------------------------------------------
    class Quiet(http.server.SimpleHTTPRequestHandler):
        def __init__(self, *a, **kw):
            super().__init__(*a, directory=ROOT, **kw)

        def log_message(self, *a):
            pass

        def end_headers(self):
            self.send_header("Cache-Control", "no-store")
            super().end_headers()

    srv = socketserver.ThreadingTCPServer(("127.0.0.1", 0), Quiet)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    port = srv.server_address[1]

    class Page(QWebEnginePage):
        def __init__(self):
            super().__init__(QWebEngineProfile.defaultProfile())

        def javaScriptConsoleMessage(self, level, message, line, source):  # noqa: N802
            if level == QWebEnginePage.JavaScriptConsoleMessageLevel.ErrorMessageLevel:
                errors.append(f"{message} (line {line})")
                print("JS ERROR:", message, line, flush=True)

    view = QWebEngineView()
    pg = Page()
    view.setPage(pg)
    view.resize(1200, 800)
    view.show()

    def js(expr, secs=60):
        out = {}
        pg.runJavaScript(expr, lambda r: out.update(v=r))
        wait(lambda: "v" in out, secs)
        return out.get("v")

    nav = [0]

    def load(url, pre=""):
        # a new query each time: a change of the #hash alone is not a new page load
        nav[0] += 1
        head, _, frag = url.partition("#")
        url = head + ("&" if "?" in head else "?") + f"n={nav[0]}" + ("#" + frag if frag else "")
        done = {}
        pg.loadFinished.connect(lambda ok: done.update(ok=ok))
        pg.load(QUrl(url))
        wait(lambda: "ok" in done, 20)
        try:
            pg.loadFinished.disconnect()
        except Exception:  # noqa: BLE001
            pass
        if pre:
            js(pre + ";BiruniPortal.reload();1")
        js("window.__r=0;BiruniPortal.ready().then(()=>window.__r=1);1")
        return wait(lambda: js("window.__r") == 1, 60)

    base = f"http://127.0.0.1:{port}/p1/"
    check(load(base + f"#k={p.pub}&l=ar"), "the viewer loads the portal over http://127.0.0.1")
    st = js("(()=>{const S=BiruniPortal.state,P=S.portals;return JSON.stringify({ok:P[0].ok,pinned:P[0].pinned,n:P[0].items.length,"
            "follows:P.slice(1).map(x=>[x.follow.url,x.ok,x.why,x.items.length]),badge:document.getElementById('pBadge').textContent,"
            "cards:document.querySelectorAll('.card').length,bad:document.querySelectorAll('.card.untrusted').length,dir:document.documentElement.dir})})()")
    st = json.loads(st)
    print("   ", st["badge"])
    check(st["ok"] and st["pinned"] and "موقّعة من" in st["badge"] and st["dir"] == "rtl",
          "the viewer verifies the index and the pinned key (Arabic, RTL)")
    fol = {f[0]: f for f in st["follows"]}
    check(fol["../p2/"][1] and fol["../p2/"][3] == 1, "the viewer merges the followed portal")
    check(not fol["../p3/"][1] and fol["../p3/"][2] == "not the pinned key" and fol["../p4/"][2] == "unreachable",
          "the viewer flags the wrong-key and the missing portals")
    check(st["cards"] == len(p.items()) + 2 and st["bad"] == 1,
          "the grid shows own + followed items; the untrusted one is marked (%d cards, %d untrusted)" % (st["cards"], st["bad"]))
    check(json.loads(js("JSON.stringify(Array.from(BiruniPortal.xpub(new Uint8Array(%s))))" % list(sara_priv))) ==
          list(C.box_public(sara_priv)), "the page's X25519 matches Python's")
    js("document.getElementById('q').value='قطع';document.getElementById('q').dispatchEvent(new Event('input'));1")
    check(js("document.querySelectorAll('.card').length") == 1, "search finds an item by its title")
    js("document.getElementById('q').value='';document.getElementById('fType').value='mapservice';document.getElementById('fType').dispatchEvent(new Event('change'));1")
    check(js("document.querySelectorAll('.card').length") == 1, "the type filter works")
    js("document.getElementById('fType').value='';document.getElementById('fType').dispatchEvent(new Event('change'));1")

    kf = C.key_file("سارة", sara_priv, "passphrase-123", rounds=50000)
    js("window.__k=0;BiruniPortal.useKey(%s,'wrong-pass').then(()=>window.__k=1,()=>window.__k=2);1" % json.dumps(kf))
    check(wait(lambda: js("window.__k") == 2, 30), "a wrong passphrase is refused in the page")
    js("window.__k=0;BiruniPortal.useKey(%s,'passphrase-123').then(()=>window.__k=1,e=>window.__k=String(e));1" % json.dumps(kf))
    check(wait(lambda: js("window.__k") == 1, 30), "the member key file opens with its passphrase in the page")

    def open_hash(item_id):
        js("window.__h=null;BiruniPortal.openHash(%s).then(h=>window.__h=h,e=>window.__h='ERR:'+(e&&e.message||e));1" % json.dumps(item_id))
        wait(lambda: js("window.__h") is not None, 30)
        return js("window.__h")

    want = hashlib.sha256(secret).hexdigest()
    check(open_hash(m_a["id"]) == want, "the page decrypts a group item with the member key (WebCrypto paths)")
    check(open_hash(m_pub["id"]) == hashlib.sha256(open(pub_file, "rb").read()).hexdigest(),
          "the page downloads a public item and checks its hash")
    check(str(open_hash(m_b["id"])).startswith("ERR:"), "the page refuses an item of a group she is not in")
    check(str(open_hash(m_exp["id"])).startswith("ERR:"), "the page refuses an expired item")
    js("document.querySelector('.card[data-id=\"%s\"]').click();1" % m_a["id"])
    check(js("!document.getElementById('itemDlg').classList.contains('hide')&&document.getElementById('itemBox').textContent.includes('للمراجعة فقط')"),
          "the item page shows its scope of use")
    wait(lambda: False, 0.6)
    view.grab().save(os.path.join(os.environ.get("BIRUNI_PORTAL_SHOTS", ROOT), "portal_item.png"))
    js("document.getElementById('itemDlg').classList.add('hide');1")

    # the pure-JS fallbacks (browsers without Ed25519/X25519 in WebCrypto)
    check(load(base + f"#k={p.pub}&l=ar", "window.__noWebEd=1;window.__noWebX=1"), "the viewer reloads with the JS fallbacks")
    check(js("BiruniPortal.state.portals[0].ok&&BiruniPortal.state.portals[0].items.every(i=>i.ok)") is True,
          "Ed25519 in BigInt verifies the portal")
    check(open_hash(m_a["id"]) == want, "X25519 in BigInt opens the group item")
    view.grab().save(os.path.join(os.environ.get("BIRUNI_PORTAL_SHOTS", ROOT), "portal_grid.png"))

    # a pinned key that is not the owner's
    check(load(base + "#k=" + C.ed_public(OTHER).hex() + "&l=ar"), "the viewer loads with another key pinned")
    check(js("BiruniPortal.state.portals[0].ok") is False and "ليست صاحبة" in js("document.getElementById('pBadge').textContent"),
          "... and refuses the portal")
    # tampered index
    good = open(os.path.join(P1, core.INDEX), encoding="utf-8").read()
    with open(os.path.join(P1, core.INDEX), "w", encoding="utf-8") as f:
        f.write(good.replace("المخطط العام", "المخطط المزوّر"))
    check(load(f"http://127.0.0.1:{port}/p1/?t=2#l=en"), "the viewer loads a tampered portal")
    check(js("BiruniPortal.state.portals[0].ok") is False and "Invalid signature" in js("document.getElementById('pBadge').textContent")
          and js("document.querySelectorAll('.card.untrusted').length") >= 1, "a tampered portal is shown untrusted (English)")
    with open(os.path.join(P1, core.INDEX), "w", encoding="utf-8") as f:
        f.write(good)
    # rollback: an older index after a newer one was seen
    older = good
    p.add_file(data=b"new", name="n.txt", title="جديد")
    check(load(base + "?t=3#l=ar"), "the viewer loads the newer portal")
    with open(os.path.join(P1, core.INDEX), "w", encoding="utf-8") as f:
        f.write(older)
    check(load(base + "?t=4#l=ar") and js("BiruniPortal.state.portals[0].rollback") is not None
          and not js("document.getElementById('banner').classList.contains('hide')"), "an older copy is flagged (rollback)")
    p.save()
    # file:// (a USB stick): portal.js
    check(load(QUrl.fromLocalFile(os.path.join(P1, "index.html")).toString()), "the viewer opens from file://")
    check(js("BiruniPortal.state.portals[0].ok&&BiruniPortal.state.portals[0].items.length>5") is True,
          "from file:// the index comes from portal.js and verifies")
    # the hosted viewer opens a portal by its address
    check(load(f"http://127.0.0.1:{port}/bridge/portal/#u=" + base + f"&k={p.pub}"), "the hosted viewer opens a portal by #u=")
    check(js("BiruniPortal.state.portals[0].ok&&BiruniPortal.state.portals[0].items.length>5") is True,
          "... verified against the pinned key")
    srv.shutdown()
    view.close()
finally:
    shutil.rmtree(ROOT, ignore_errors=True)

check(not errors, "no JavaScript errors" + ("" if not errors else ": " + "; ".join(errors[:4])))
print("ALL PASSED" if not failures else "FAILED: %d" % len(failures), flush=True)
sys.exit(1 if failures else 0)
