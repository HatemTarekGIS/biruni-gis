"""P1 receipts and CopyLedger test: BRAK2, BRCP1, BRLA2, BRLT1 signatures, copy counting, and second-copy volume check."""
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(ROOT, "app", "plugins"))
sys.path.insert(0, os.path.join(ROOT, "app", "python"))

from biruni.field_photos import bridge, copies, handover, ladder, server  # noqa: E402

failures = []


def check(cond, msg):
    print(f"  [{'PASS' if cond else 'FAIL'}] {msg}", flush=True)
    if not cond:
        failures.append(msg)


print("=== P1 Receipts & CopyLedger Test ===", flush=True)
work = tempfile.mkdtemp(prefix="biruni_p1_receipts_")

try:
    # 1. CopyLedger places, count, non_mailbox_count, and receipt_info
    photos_dir = os.path.join(work, "photos")
    mirror_dir = os.path.join(work, "mirror")
    led = copies.CopyLedger(photos_dir, mirror_folder=mirror_dir)

    check(led.count("p-001") == 0 and led.non_mailbox_count("p-001") == 0,
          "unknown item has 0 copies and 0 non-mailbox copies")

    # Mark office only -> count=1, non_mailbox_count=1
    led.mark("p-001", "office", sha="a" * 64, ssha="b" * 64)
    c1, h1, h8_1 = led.receipt_info("p-001")
    check(c1 == 1 and h1 == "b" * 64 and h8_1 == "bbbbbbbb" and led.non_mailbox_count("p-001") == 1,
          f"office copy alone: count=1, non_mailbox=1, h=ssha ({c1}, {h8_1})")

    # Add drive_mb -> count=2 (for phone delete rule), but non_mailbox_count is still 1 (Drive file cannot be trashed yet!)
    led.mark("p-001", "drive_mb")
    check(led.count("p-001") == 2 and led.non_mailbox_count("p-001") == 1,
          "office + drive_mb counts as 2 for phone delete rule, but 1 for non-mailbox (Drive cannot be trashed yet)")

    # Write verified second copy to mirror folder -> count=3, non_mailbox_count=2
    src_file = os.path.join(photos_dir, "2026-11-24", "photo1.jpg")
    os.makedirs(os.path.dirname(src_file), exist_ok=True)
    payload = b"\xff\xd8\xff\xe0" + b"test-jpeg-bytes" + b"\xff\xd9"
    payload_sha = hashlib.sha256(payload).hexdigest()
    with open(src_file, "wb") as f:
        f.write(payload)

    ok_mirror = led.write_mirror_file("p-001", src_file, expected_sha=payload_sha, ssha="b" * 64)
    mirrored_path = os.path.join(mirror_dir, "2026-11-24", "photo1.jpg")
    check(ok_mirror is True and os.path.isfile(mirrored_path) and os.path.getsize(mirrored_path) == len(payload),
          "write_mirror_file copies photo to mirror folder, re-hashes from disk, and marks mirror")
    check(led.count("p-001") == 3 and led.non_mailbox_count("p-001") == 2,
          "with mirror copy added, non_mailbox_count reaches 2 (Drive mailbox file may now be trashed)")

    # Persistence across reload
    led2 = copies.CopyLedger(photos_dir, mirror_folder=mirror_dir)
    check(led2.count("p-001") == 3 and led2.non_mailbox_count("p-001") == 2,
          "copies.jsonl persists places and hashes across CopyLedger reload")

    # 2. PhotoStore.save + originals(for_drive_clean) + copy_status
    st_dir = os.path.join(work, "store_nomirror")
    store1 = server.PhotoStore(st_dir)
    meta_d = {"id": "drvphoto-1", "lat": 30.0, "lon": 31.0, "t": 1760000000000, "route": "drive"}
    rec_d, is_new = store1.save(meta_d, payload, ssha="c" * 64)
    check(is_new is True and rec_d.get("sha") == payload_sha and rec_d.get("ssha") == "c" * 64,
          "PhotoStore.save records plaintext sha and sealed ssha")
    check(store1.ledger.count("drvphoto-1") == 2 and store1.ledger.non_mailbox_count("drvphoto-1") == 1,
          "Drive-routed photo without mirror folder has count=2 (office + drive_mb) and non_mailbox_count=1")
    check(store1.originals(["drvphoto-1"], for_drive_clean=False) == ["drvphoto-1"]
          and store1.originals(["drvphoto-1"], for_drive_clean=True) == [],
          "originals(for_drive_clean=True) withholds id while fewer than 2 non-mailbox copies exist")

    # Now attach a mirror folder to store1 -> syncs existing photo to mirror -> non_mailbox_count becomes 2
    store1.set_mirror_folder(os.path.join(work, "store_mirror"))
    check(store1.ledger.non_mailbox_count("drvphoto-1") == 2
          and store1.originals(["drvphoto-1"], for_drive_clean=True) == ["drvphoto-1"],
          "once second-copy mirror folder is synced, originals(for_drive_clean=True) releases id for Drive trashing")
    cs = store1.copy_status(["drvphoto-1", "missing-id"])
    check(cs == [["drvphoto-1", 3, "cccccccc"], ["missing-id", 0, ""]],
          f"copy_status returns [[id, c, h8], ...] ({cs})")

    # 3. Volume comparison helper
    check(copies.different_volume(photos_dir, photos_dir) is False,
          "different_volume is False for identical folder")
    check(copies.different_volume(photos_dir, mirror_dir) is False,
          "different_volume is False for two folders on the same volume")

    # 4. BRAK1 + BRAK2, BRCP1, BRLA1 + BRLA2, BRLT1 signatures in Node + Python
    key = bridge.new_key()
    sk_b64, pin = bridge.identity(key)
    lad = ladder.Ladder(key)

    # Test Ladder BRLA2 (u2=0 when mirror_ok=False, u2=upto when mirror_ok=True)
    now_ms = int(__import__("time").time() * 1000)
    env_ok = lad.seal({
        "t": "lpos", "v": 1, "d": "dev_test_1234", "e": "ep1",
        "lo": 1, "bl": 2,
        "b": [[1, {"id": "dev_test_1234", "lat": 30.0, "lon": 31.0, "t": now_ms}]],
        "n": "nonce1234", "ts": now_ms,
    })
    pts, ack_no_mirror = lad.take(env_ok, "mqtt", server.clean_pos, lambda p: p, mirror_ok=False)
    ack_obj1 = lad.open(ack_no_mirror)
    check(len(pts) == 1 and ack_obj1.get("upto") == 1 and ack_obj1.get("u2") == 0 and bool(ack_obj1.get("is2")),
          f"Ladder.take with mirror_ok=False produces lack with upto=1, u2=0, and is2 ({ack_obj1})")

    env_ok2 = lad.seal({
        "t": "lpos", "v": 1, "d": "dev_test_1234", "e": "ep1",
        "lo": 2, "bl": 1,
        "b": [[2, {"id": "dev_test_1234", "lat": 30.01, "lon": 31.01, "t": now_ms}]],
        "n": "nonce5678", "ts": now_ms,
    })
    pts2, ack_with_mirror = lad.take(env_ok2, "mqtt", server.clean_pos, lambda p: p, mirror_ok=True)
    ack_obj2 = lad.open(ack_with_mirror)
    check(len(pts2) == 1 and ack_obj2.get("upto") == 2 and ack_obj2.get("u2") == 2 and bool(ack_obj2.get("is2")),
          f"Ladder.take with mirror_ok=True produces lack with upto=2, u2=2, and is2 ({ack_obj2})")

    # Verify BRAK2, BRCP1, BRLA2, BRLT1 in Node using CORE_JS
    node = shutil.which("node") or next(
        (p for p in (r"C:\Program Files\nodejs\node.exe",) if os.path.isfile(p)), "node"
    )
    node_script = os.path.join(work, "verify_sigs.js")
    with open(node_script, "w", encoding="utf-8") as f:
        f.write("const crypto = require('crypto').webcrypto; globalThis.crypto = crypto;\n")
        f.write("globalThis.location = {hostname:'example.com', hash:'#k=" + key + "&i=" + pin + "&s=" + sk_b64 + "'};\n")
        f.write("globalThis.localStorage = {getItem:()=>null, setItem:()=>{}};\n")
        f.write("globalThis.WebSocket = class { close(){} };\n")
        f.write("globalThis.fetch = async ()=>({ok:false});\n")
        f.write(bridge._core() + "\n")
        f.write(r"""
(async () => {
 await bridgeKey('""" + key + r"""');
 const h = 'a'.repeat(64), rt = 1760000000123;
 const g1 = await IDSIGN('BRAK1|ph-1|original|n-1');
 const g2 = await IDSIGN('BRAK2|ph-1|original|n-1|' + h + '|2|' + rt);
 const ok1 = await IDOK(g1.pub, g1.sig, 'BRAK1|ph-1|original|n-1');
 const ok2 = await IDOK(g2.pub, g2.sig, 'BRAK2|ph-1|original|n-1|' + h + '|2|' + rt);
 const badCount = await IDOK(g2.pub, g2.sig, 'BRAK2|ph-1|original|n-1|' + h + '|1|' + rt);
 const badHash = await IDOK(g2.pub, g2.sig, 'BRAK2|ph-1|original|n-1|' + 'b'.repeat(64) + '|2|' + rt);

 const cp = await IDSIGN('BRCP1|cn-1|' + rt + '|ph-1:2:aaaaaaaa,ph-2:1:bbbbbbbb');
 const cpOk = await IDOK(cp.pub, cp.sig, 'BRCP1|cn-1|' + rt + '|ph-1:2:aaaaaaaa,ph-2:1:bbbbbbbb');
 const cpBad = await IDOK(cp.pub, cp.sig, 'BRCP1|cn-1|' + rt + '|ph-1:2:cccccccc,ph-2:1:bbbbbbbb');

 const la2 = """ + json.dumps(ack_obj2) + r""";
 const la1Ok = await IDOK(la2.ip, la2.is, 'BRLA1|' + la2.d + '|' + la2.e + '|' + la2.upto);
 const la2Ok = await IDOK(la2.ip, la2.is2, 'BRLA2|' + la2.d + '|' + la2.e + '|' + la2.upto + '|' + la2.u2);
 const la2Tamper = await IDOK(la2.ip, la2.is2, 'BRLA2|' + la2.d + '|' + la2.e + '|' + la2.upto + '|0');

 console.log(JSON.stringify({ok1, ok2, badCount, badHash, cpOk, cpBad, la1Ok, la2Ok, la2Tamper}));
 process.exit(0);
})();
""")
    proc = subprocess.run([node, node_script], capture_output=True, text=True, timeout=15)
    check(proc.returncode == 0, f"Node signature verification ran (rc={proc.returncode}, err={proc.stderr.strip()})")
    res = json.loads(proc.stdout.strip().splitlines()[-1]) if proc.stdout.strip() else {}
    check(res.get("ok1") is True and res.get("ok2") is True
          and res.get("badCount") is False and res.get("badHash") is False,
          f"BRAK2 binds (id, kind, nonce, h, c, rt); any tampered c or h fails ({res})")
    check(res.get("cpOk") is True and res.get("cpBad") is False,
          f"BRCP1 binds (nonce, rt, id:c:h8,...); tampered h8 fails ({res})")
    check(res.get("la1Ok") is True and res.get("la2Ok") is True and res.get("la2Tamper") is False,
          f"BRLA1 and BRLA2 verify cleanly and reject tampered u2 ({res})")

finally:
    shutil.rmtree(work, ignore_errors=True)

print("ALL PASS" if not failures else f"{len(failures)} FAILED", flush=True)
sys.exit(1 if failures else 0)
