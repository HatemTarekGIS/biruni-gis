r"""No-loss P3: a backup PC opens what the office PC never saw.

The real phone page (QWebEngine, a pinned link with &d=1&pk=office) takes the PC's signed recipients list from the
hello, then seals a new photo v2 for office + backup. The backup PC, with the office pack the office saved for it:
- takes the photo from a .bfp (the office's Drive folder; header kid = the office, v:1),
- takes it from a .bfx bundle (P2),
- opens the positions (.bgp, under the office's bridge key from the pack).
A photo queued before the list arrived stays v1 (never re-sealed) and stays the office's only.
Mixed pairs: a page without a list (an older PC) seals v1; the office opens both.

Run:  engine\bin\python-qgis.bat tests\test_backup_pc.py
"""
import base64
import json
import os
import shutil
import sys
import tempfile
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QT_QPA_FONTDIR", r"C:\Windows\Fonts")
os.environ["QTWEBENGINE_CHROMIUM_FLAGS"] = "--disable-gpu --no-sandbox"
ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(ROOT, "app", "plugins"))
sys.path.insert(0, os.path.join(ROOT, "app", "python"))

from cryptography.hazmat.primitives import serialization  # noqa: E402
from cryptography.hazmat.primitives.asymmetric import ec  # noqa: E402
from qgis.PyQt.QtCore import QCoreApplication, Qt, QUrl  # noqa: E402
from qgis.PyQt.QtWebEngineCore import QWebEnginePage, QWebEngineSettings  # noqa: E402
from qgis.PyQt.QtWidgets import QApplication  # noqa: E402

from biruni.field_photos import bridge, bundle, drive, envelope, recipients, server  # noqa: E402
from biruni.field_photos.handover import HandoverKey  # noqa: E402
from biruni.field_photos.ladder import Ladder  # noqa: E402

QCoreApplication.setAttribute(Qt.ApplicationAttribute.AA_ShareOpenGLContexts)
app = QApplication.instance() or QApplication(sys.argv)
failures = []


def check(cond, msg):
    print(f"  [{'PASS' if cond else 'FAIL'}] {msg}", flush=True)
    if not cond:
        failures.append(msg)


def wait(pred, timeout=15):
    t0 = time.time()
    while time.time() - t0 < timeout:
        app.processEvents()
        v = pred()
        if v:
            return v
        time.sleep(0.03)
    return pred()


def unb64u(s):
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def b64u(b):
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode("ascii")


print("=== P3 backup PC ===", flush=True)
work = tempfile.mkdtemp(prefix="biruni_p3_bpc_")
try:
    key = bridge.new_key()
    sk_b64, pin = bridge.identity(key)
    office_dir = os.path.join(work, "office-cert")
    office = HandoverKey(office_dir)
    backup = HandoverKey(os.path.join(work, "backup-cert"))
    recipients.add(office_dir, backup.public, "backup", "branch", office_pub=office.public)
    rl = recipients.signed(office_dir, office.public, key)

    for n in ("RELAYS", "LEGACY_RELAYS", "MQTT", "LEGACY_MQTT"):
        setattr(bridge, n, ())
    bridge.NTFY = ""
    bridge.OWN = ""
    raw_page = bridge.phone_page(server.all_phone_texts())
    page = QWebEnginePage()
    page.settings().setAttribute(QWebEngineSettings.WebAttribute.LocalContentCanAccessRemoteUrls, True)
    page.javaScriptConsoleMessage = lambda lv, msg, ln, src: print("  js:", msg, ln, flush=True)
    ready = []
    page.loadFinished.connect(ready.append)
    page.setHtml(raw_page, QUrl(f"https://photos.biruni-gis.com/p/#k={key}&i={pin}&l=en&d=1&pk={office.public}"))
    check(wait(lambda: bool(ready), 15), "phone page loaded")

    def js(code):
        box = []
        page.runJavaScript(code, box.append)
        wait(lambda: bool(box), 10)
        return box[0] if box else None

    def ajs(expr):
        page.runJavaScript("window.__r=undefined;(async()=>{try{window.__r=JSON.stringify(await (%s))}"
                           "catch(e){window.__r=JSON.stringify('error '+e)}})()" % expr)
        wait(lambda: js("window.__r!==undefined") is True, 15)
        v = js("window.__r")
        return json.loads(v) if isinstance(v, str) else None

    check(wait(lambda: js("!!BK && !!db && !!ID.pin") is True, 10), "bridge key, IndexedDB and the pin are ready")

    def photo(pid, extra=""):
        return ajs(r"""(async()=>{const jpg=new Uint8Array([0xff,0xd8,0xff,0xe0,1,2,3,%s0xff,0xd9]);
         await put({id:'%s',blob:new Blob([jpg],{type:'image/jpeg'}),meta:{id:'%s',lat:30.05,lon:31.24,acc:3,
          t:1763370000000,kind:'Pole'},st:0,room:ROOM});
         const x=await getItem('%s');return {b:b64u.enc(new Uint8Array(await x.blob.arrayBuffer())),pk:x.pk||''}})()"""
                   % (extra, pid, pid, pid))

    # a photo queued before any list: v1, the office's only
    early = photo("bpc-early-0001")
    eb = unb64u(early["b"])
    check(eb[0] == 1 and early["pk"] == office.public, "before the list: sealed v1 for the office (an older PC)")

    # the hello brings the list (the receiver page sends CFG.rl with the hello)
    js("hello(%s)" % json.dumps({"kinds": [], "pk": office.public, "rl": rl}))
    check(wait(lambda: js("!!(rlGet()&&rlGet().lst.seq===%d)" % json.loads(rl["l"])["seq"]) is True, 10),
          "the hello's signed list is taken (pin checked)")
    early2 = ajs("(async()=>{const x=await getItem('bpc-early-0001');"
                 "return b64u.enc(new Uint8Array(await x.blob.arrayBuffer()))})()")
    check(unb64u(early2) == eb, "the photo queued earlier is not sealed again (stays v1)")
    late = photo("bpc-late-0001", "4,5,6,")
    lb = unb64u(late["b"])
    check(lb[0] == 2 and envelope.kids(lb) == [office.kid, backup.kid] and late["pk"] == office.public,
          "after the list: sealed v2 for office + backup; the record still names the office key (.bfp kid)")
    check(office.open(lb)[0]["id"] == "bpc-late-0001", "the office opens the v2 photo too")

    # the guide carries it as well; a list signed by someone else is refused
    other_sk, _ = bridge.identity(bridge.new_key())
    lst = json.loads(rl["l"])
    lst["seq"] += 5
    lst["r"] = lst["r"][:1]
    l_text = recipients.canonical(lst)
    ip, sig = recipients.sign_with(other_sk, l_text)
    js("setGuide(%s)" % json.dumps({"v": 7, "items": [], "rl": {"l": l_text, "ip": ip, "is": sig}}))
    time.sleep(0.3)
    check(wait(lambda: js("rlGet().lst.seq") == json.loads(rl["l"])["seq"], 3),
          "a guide carrying a list signed by another identity changes nothing")

    # ---- the office pack and the backup PC ---------------------------------------------------------------------
    pack_blob = recipients.office_pack(backup.public, {"biruni/field-photos/bridge-key": key})
    check(pack_blob[:4] == b"BOP1" and key.encode() not in pack_blob, "the office pack is sealed")
    try:
        recipients.open_office_pack(office, pack_blob)
        check(False, "the pack opens only on the backup PC")
    except recipients.RecipientsError:
        check(True, "the pack opens only on the backup PC")
    pack = recipients.open_office_pack(backup, pack_blob)
    check(pack["keys"]["biruni/field-photos/bridge-key"] == key, "the backup PC reads the office's bridge key")

    def bfp_of(body, pid, n):
        ph = ec.generate_private_key(ec.SECP256R1())
        ppub = b64u(ph.public_key().public_bytes(serialization.Encoding.X962,
                                                 serialization.PublicFormat.UncompressedPoint))
        h = {"room": drive.room_tag(key), "dev": "devBPC01", "kid": office.kid, "id": pid, "n": n,
             "t": 1763370000000, "pub": ppub, "cert": drive.cert(drive.auth_key(key), ppub, "devBPC01")}
        blob, name = drive.pack(h, body, ph)
        p = os.path.join(work, "drive", name)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        open(p, "wb").write(blob)
        return p

    p_late = bfp_of(lb, "bpc-late-0001", 2)
    p_early = bfp_of(eb, "bpc-early-0001", 1)
    store_b = server.PhotoStore(os.path.join(work, "backup-photos"))
    store_o = server.PhotoStore(os.path.join(work, "office-photos"))
    res = recipients.import_files([p_late, p_early], store_b, backup, pack)
    check(res["imported"] == 1 and len(res["refused"]) == 1 and "another computer" in res["refused"][0][1],
          f"the backup PC takes the v2 .bfp and refuses the v1 one ({res})")
    rec = store_b.records.get("bpc-late-0001")
    check(bool(rec) and os.path.isfile(rec.get("photo") or "") and not store_o.records,
          "the photo is saved on the backup PC; the office never saw it")
    check(recipients.import_files([p_late], store_b, backup, pack)["skipped"] == 1, "taking it again: already here")
    check("office pack" in recipients.import_files([p_late], server.PhotoStore(os.path.join(work, "b2")), backup,
                                                   None)["refused"][0][1], "without the pack a .bfp is refused")

    # a bundle (P2) with the v2 photo
    bfx = os.path.join(work, "phone.bfx")
    open(bfx, "wb").write(bundle.pack_one([("bpc-late-0001", lb, 0)]))
    store_b2 = server.PhotoStore(os.path.join(work, "backup-photos-2"))
    r2 = recipients.import_files([bfx], store_b2, backup, None)
    check(r2["imported"] == 1 and "bpc-late-0001" in store_b2.records, "a .bfx bundle opens on the backup PC")
    bfx_v1 = os.path.join(work, "phone-v1.bfx")
    open(bfx_v1, "wb").write(bundle.pack_one([("bpc-early-0001", eb, 0)]))
    check(recipients.import_files([bfx_v1], server.PhotoStore(os.path.join(work, "b3")), backup, None)["refused"],
          "a bundle of v1 photos is refused on the backup PC (the office's only)")

    # positions (.bgp): the office's bridge key from the pack
    env = Ladder(key).seal({"t": "lpos", "v": 1, "d": "devBPC01", "e": "ep1", "lo": 1, "bl": 1,
                            "b": [[1, {"id": "devBPC01", "lat": 30.2, "lon": 31.3, "t": 1763370000000}]],
                            "n": "nonce-bpc", "ts": 1763370000000})
    opened = Ladder(pack["keys"]["biruni/field-photos/bridge-key"]).open(env)
    check(isinstance(opened, dict) and opened["b"][0][1]["lat"] == 30.2, "the backup PC opens the positions")

    # the office's Drive intake still takes the v2 file (header kid = office)
    h, body, _sk = drive.verify(open(p_late, "rb").read(), drive.auth_keys(key, {}), office.kid, drive.room_tag(key))
    check(office.open(body)[0]["id"] == "bpc-late-0001", "the office's own Drive checks take the v2 .bfp")
    stranger = HandoverKey(os.path.join(work, "stranger"))
    try:
        drive.verify(open(p_late, "rb").read(), drive.auth_keys(key, {}), stranger.kid, drive.room_tag(key))
        check(False, "a PC that is not a recipient is refused by the Drive checks")
    except drive.BfpError:
        check(True, "a PC that is not a recipient is refused by the Drive checks")
finally:
    shutil.rmtree(work, ignore_errors=True)

print("ALL PASS" if not failures else f"{len(failures)} FAILED", flush=True)
sys.exit(1 if failures else 0)
