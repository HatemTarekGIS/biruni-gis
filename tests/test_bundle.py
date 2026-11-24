"""P2 sealed export (.bfx) and PC import test:
- phone page exports a .bfx bundle (`BFX1 | JSON index | sealed blobs`) and does NOT delete anything on the phone;
- JSON index holds only ids, sizes, hashes and states (no names, no coordinates, no notes);
- round trip: PC `bundle.import_bundle` and `FieldPhotosDock.import_bundle_files` feed `PhotoStore.save` and record in `CopyLedger`;
- importing the same bundle again is a no-op (deduplicated by id);
- truncated, trailing-garbage, extra-index-field, hash-mismatched, or tampered bundles are refused atomically;
- chunking splits bundles over max_chunk_bytes.
"""
import base64
import hashlib
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

from qgis.PyQt.QtCore import QCoreApplication, Qt, QUrl  # noqa: E402
from qgis.PyQt.QtWebEngineCore import QWebEnginePage, QWebEngineSettings  # noqa: E402
from qgis.PyQt.QtWidgets import QApplication  # noqa: E402

from biruni.field_photos import bridge, bundle, handover, server  # noqa: E402

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


print("=== P2 Sealed Export (.bfx) & PC Import Test ===", flush=True)
work = tempfile.mkdtemp(prefix="biruni_p2_bundle_")

try:
    key = bridge.new_key()
    sk_b64, pin = bridge.identity(key)
    hk = handover.HandoverKey(os.path.join(work, "cert"))

    bridge.RELAYS = ()
    bridge.LEGACY_RELAYS = ()
    bridge.MQTT = ()
    bridge.LEGACY_MQTT = ()
    bridge.NTFY = ""
    bridge.OWN = ""

    raw_page = bridge.phone_page(server.all_phone_texts())
    page = QWebEnginePage()
    page.settings().setAttribute(QWebEngineSettings.WebAttribute.LocalContentCanAccessRemoteUrls, True)
    page.javaScriptConsoleMessage = lambda lv, msg, ln, src: print("  js:", msg, ln, flush=True)

    url = QUrl(f"https://photos.biruni-gis.com/p/#k={key}&i={pin}&l=en&d=1&pk={hk.public}")
    ready = []
    page.loadFinished.connect(ready.append)
    page.setHtml(raw_page, url)
    check(wait(lambda: bool(ready), 15), "phone page loaded in QWebEngine")

    def js(code):
        box = []
        page.runJavaScript(code, box.append)
        wait(lambda: bool(box), 10)
        return box[0] if box else None

    def ajs(expr):
        page.runJavaScript(
            "window.__r=undefined;(async()=>{try{window.__r=JSON.stringify(await (%s))}"
            "catch(e){window.__r=JSON.stringify('error '+e)}})()" % expr
        )
        wait(lambda: js("window.__r!==undefined") is True, 10)
        v = js("window.__r")
        return json.loads(v) if isinstance(v, str) else None

    check(wait(lambda: js("!!BK && !!db") is True, 10), "bridgeKey and IndexedDB initialized")

    # 1. Queue two photos on the phone (one st=0, one st=9 refused) with sensitive surveyor name, note & coordinates,
    #    export a .bfx bundle from the phone page, and verify exporting does NOT delete anything on the phone.
    exp_res = ajs(r"""(async()=>{
     window.BIRUNI_NO_AUTODL=true;
     const jpg1=new Uint8Array([0xff,0xd8,0xff,0xe0,0,16,74,70,73,70,0,1,1,2,3,4,0xff,0xd9]);
     const jpg2=new Uint8Array([0xff,0xd8,0xff,0xe0,0,16,74,70,73,70,0,1,5,6,7,8,0xff,0xd9]);
     await put({
      id:'bfx-photo-0001',
      blob:new Blob([jpg1],{type:'image/jpeg'}),
      meta:{id:'bfx-photo-0001',lat:30.04442,lon:31.23571,acc:4.2,t:1763370000000,surveyor:'HatemSecretName',by:'HatemSecretName',note:'SecretMonumentNote',kind:'Pole'},
      st:0,room:ROOM
     });
     await put({
      id:'bfx-photo-0002',
      blob:new Blob([jpg2],{type:'image/jpeg'}),
      meta:{id:'bfx-photo-0002',lat:29.97923,lon:31.13420,acc:3.8,t:1763370060000,surveyor:'HatemSecretName',by:'HatemSecretName',note:'SecondSecretNote',kind:'Sign'},
      st:9,why:'refused earlier',room:ROOM
     });
     const bundles=await exportBundles();
     const b0=bundles[0];
     const bytes=new Uint8Array(await b0.blob.arrayBuffer());
     const remain=(await all()).map(x=>x.id).sort();
     const dlCount=$('exportLinks').querySelectorAll('a').length;
     return {
      bundleCount:bundles.length,
      name:b0.name,
      b64:b64u.enc(bytes),
      remain:remain,
      dlCount:dlCount
     };
    })()""")
    check(isinstance(exp_res, dict)
          and exp_res.get("bundleCount") == 1
          and exp_res.get("name", "").endswith(".bfx")
          and exp_res.get("remain") == ["bfx-photo-0001", "bfx-photo-0002"]
          and exp_res.get("dlCount") == 1,
          f"phone exports .bfx bundle, creates download fallback link, and keeps all items on phone ({exp_res and exp_res.get('remain')})")

    b64_str = exp_res["b64"]
    raw_bfx = base64.urlsafe_b64decode(b64_str + "=" * (-len(b64_str) % 4))
    bfx_path = os.path.join(work, exp_res["name"])
    with open(bfx_path, "wb") as f:
        f.write(raw_bfx)

    # 2. Check index privacy: only id, size, sha, st — no names, coordinates, or notes anywhere in the index
    idx_len = int.from_bytes(raw_bfx[4:8], "big")
    raw_index_bytes = raw_bfx[8:8 + idx_len]
    raw_index_text = raw_index_bytes.decode("utf-8")
    idx, parsed = bundle.parse(raw_bfx)
    check(raw_bfx[:4] == b"BFX1" and len(parsed) == 2,
          f"BFX1 header and 2 items parsed ({idx})")
    check(all(set(item.keys()) == {"id", "size", "sha", "st"} for item in idx["items"])
          and "HatemSecretName" not in raw_index_text
          and "SecretMonumentNote" not in raw_index_text
          and "30.04442" not in raw_index_text
          and "31.23571" not in raw_index_text
          and "lat" not in raw_index_text
          and "lon" not in raw_index_text,
          "BFX1 JSON index contains ONLY id, size, sha, and st — no names, notes, or coordinates")
    check( "HatemSecretName".encode("utf-8") not in raw_bfx
          and "SecretMonumentNote".encode("utf-8") not in raw_bfx,
          "plaintext names and notes do not appear anywhere in the .bfx file (blobs are sealed)")

    # 3. Round-trip import into PhotoStore + CopyLedger with second-copy mirror folder
    photos_dir = os.path.join(work, "office_photos")
    mirror_dir = os.path.join(work, "mirror_photos")
    store = server.PhotoStore(photos_dir, mirror_folder=mirror_dir)
    res1 = bundle.import_bundle(bfx_path, store, hk)
    check(res1["total"] == 2 and res1["imported"] == 2 and res1["skipped"] == 0 and res1["refused"] == [],
          f"first import saves both sealed items ({res1})")
    rec1 = store.records.get("bfx-photo-0001") or {}
    rec2 = store.records.get("bfx-photo-0002") or {}
    check(os.path.isfile(rec1.get("photo", "")) and os.path.isfile(rec2.get("photo", ""))
          and rec1.get("surveyor") == "HatemSecretName"
          and rec1.get("note") == "SecretMonumentNote"
          and abs(rec1.get("lat", 0) - 30.04442) < 1e-6,
          "unsealed photos and metadata are saved accurately by PhotoStore.save")
    c1, h1, _ = store.ledger.receipt_info("bfx-photo-0001")
    c2, h2, _ = store.ledger.receipt_info("bfx-photo-0002")
    check(c1 == 2 and c2 == 2 and h1 == idx["items"][0]["sha"] and h2 == idx["items"][1]["sha"],
          f"imported items are recorded in CopyLedger with office + mirror = 2 copies and matching ssha ({c1}, {c2})")

    # 4. Importing the same bundle again is a no-op (deduplicated by id)
    files_before = sorted(
        os.path.join(dp, fn) for dp, _, fns in os.walk(photos_dir) for fn in fns if not fn.endswith(".jsonl")
    )
    res2 = bundle.import_bundle(bfx_path, store, hk)
    files_after = sorted(
        os.path.join(dp, fn) for dp, _, fns in os.walk(photos_dir) for fn in fns if not fn.endswith(".jsonl")
    )
    check(res2["total"] == 2 and res2["imported"] == 0 and res2["skipped"] == 2 and files_before == files_after,
          f"re-importing the same bundle is a no-op ({res2})")

    # 5. Truncated or tampered bundles are refused atomically (nothing imported)
    fresh_store = server.PhotoStore(os.path.join(work, "fresh_store"))

    def expect_refused(bad_bytes, label):
        try:
            bundle.import_bundle(bad_bytes, fresh_store, hk)
            check(False, f"{label}: expected BfxError but succeeded")
        except bundle.BfxError as e:
            check(len(fresh_store.records) == 0, f"{label}: refused with BfxError ({e}) and 0 records written")

    expect_refused(b"BFX2" + raw_bfx[4:], "wrong magic")
    expect_refused(raw_bfx[:6], "truncated in header")
    expect_refused(raw_bfx[:8 + idx_len // 2], "truncated in JSON index")
    expect_refused(raw_bfx[:-1], "truncated 1 byte off last sealed blob")
    expect_refused(raw_bfx[:8 + idx_len + 10], "truncated in first sealed blob")
    expect_refused(raw_bfx + b"\x00", "trailing byte after end of bundle")

    tampered_blob = bytearray(raw_bfx)
    tampered_blob[-10] ^= 0xFF
    expect_refused(bytes(tampered_blob), "flipped byte in sealed blob (SHA mismatch)")

    # Tampered index with valid SHA of tampered ciphertext -> fails HandoverKey.open atomically
    bad_sealed = bytearray(parsed[1][1])
    bad_sealed[-5] ^= 0xFF
    bad_pack = bundle.pack_one([
        (parsed[0][0]["id"], parsed[0][1], 0),
        (parsed[1][0]["id"], bytes(bad_sealed), 0),
    ])
    expect_refused(bad_pack, "second item has valid index SHA but tampered AES-GCM ciphertext (atomic rollback)")

    # Index containing disallowed metadata field (e.g., "lat" or "name") -> refused by parse()
    sneaky_idx = {"v": 1, "items": [dict(idx["items"][0], name="Leak")]}
    sneaky_jb = json.dumps(sneaky_idx).encode("utf-8")
    sneaky_bfx = b"BFX1" + len(sneaky_jb).to_bytes(4, "big") + sneaky_jb + parsed[0][1]
    expect_refused(sneaky_bfx, "index with forbidden extra key 'name'")

    # 6. Chunking: buildBundles(maxChunkBytes) splits when total exceeds limit
    chunk_res = ajs(r"""(async()=>{
     const bundles=await buildBundles(180);
     return {count:bundles.length,names:bundles.map(b=>b.name),counts:bundles.map(b=>b.count)};
    })()""")
    check(isinstance(chunk_res, dict) and chunk_res.get("count") == 2 and chunk_res.get("counts") == [1, 1],
          f"buildBundles splits items across multiple .bfx chunks when size exceeds chunk limit ({chunk_res})")

    py_chunks = bundle.pack([
        ("bfx-photo-0001", parsed[0][1], 0),
        ("bfx-photo-0002", parsed[1][1], 9),
    ], max_chunk_bytes=len(parsed[0][1]) + 200)
    check(len(py_chunks) == 2, f"Python bundle.pack also splits chunks at max_chunk_bytes ({len(py_chunks)} chunks)")

finally:
    shutil.rmtree(work, ignore_errors=True)

print("ALL PASS" if not failures else f"{len(failures)} FAILED", flush=True)
sys.exit(1 if failures else 0)
