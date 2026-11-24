r"""No-loss P5: BCN1 consent records (`consents.jsonl`, CopyLedger integration, idempotency, malformed rejection).

Run:  engine\bin\python-qgis.bat tests\test_consent_record.py
"""
import http.client
import json
import os
import shutil
import socket
import ssl
import sys
import tempfile
import time
import urllib.parse

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QT_QPA_FONTDIR", r"C:\Windows\Fonts")

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "app", "plugins"))
sys.path.insert(0, os.path.join(HERE, "..", "app", "python"))

from biruni.field_photos import bundle, envelope, handover, recipients, server  # noqa: E402
from biruni.field_photos.handover import HandoverKey  # noqa: E402

failures = []


def check(cond, msg):
    print(("PASS " if cond else "FAIL ") + msg, flush=True)
    if not cond:
        failures.append(msg)


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


work = tempfile.mkdtemp(prefix="biruni_p5_consent_")
try:
    photos_dir = os.path.join(work, "photos")
    mirror_dir = os.path.join(work, "mirror")
    cert_dir = os.path.join(work, "cert")
    backup_dir = os.path.join(work, "backup_cert")
    os.makedirs(mirror_dir, exist_ok=True)

    office_hk = HandoverKey(cert_dir)
    backup_hk = HandoverKey(backup_dir)
    recipients.add(cert_dir, backup_hk.public, "backup", "Branch", office_pub=office_hk.public)

    port = free_port()
    texts = {"en": server.phone_texts(lambda s: s)}
    srv = server.PhotoServer(
        photos_dir, cert_dir, texts, "en", (), port, ["127.0.0.1"], bind="127.0.0.1", mirror_folder=mirror_dir
    )

    ssl_ctx = ssl.create_default_context()
    ssl_ctx.check_hostname = False
    ssl_ctx.verify_mode = ssl.CERT_NONE

    def post_sealed_item(meta_inner, raw_bytes, pubs=None):
        pubs = pubs or [office_hk.public, backup_hk.public]
        sealed = envelope.seal(pubs, meta_inner, raw_bytes)
        outer = {"id": meta_inner["id"], "sealed": 1, "t": meta_inner.get("t", int(time.time() * 1000)), "cn": 1}
        q = urllib.parse.quote(json.dumps(outer, ensure_ascii=False))
        c = http.client.HTTPSConnection("127.0.0.1", port, context=ssl_ctx, timeout=10)
        c.request("POST", f"{srv.path()}photo?m={q}", body=sealed, headers={"Content-Type": "application/octet-stream"})
        r = c.getresponse()
        body = r.read()
        c.close()
        return r.status, body

    now_ms = int(time.time() * 1000)
    dev_id = "dev_consent_0001"
    rec_id = f"cn-{dev_id}-29000001"
    bcn_body = b"BCN1" + json.dumps({
        "v": 1,
        "dev": dev_id,
        "by": "Hatem",
        "seq": 29000001,
        "mode": "5",
        "hours": [[0, 480, 1020]],
        "a": True,
        "t": now_ms,
    }).encode("utf-8")
    meta = {
        "id": rec_id,
        "t": now_ms,
        "cn": 1,
        "device_id": dev_id,
        "surveyor": "Hatem",
        "by": "Hatem",
    }

    # 1. A valid sealed BCN1 record lands in consents.jsonl and is marked in the CopyLedger
    st, resp_raw = post_sealed_item(meta, bcn_body)
    resp = json.loads(resp_raw)
    cons_path = os.path.join(photos_dir, server.CONSENTS_NAME)
    lines = open(cons_path, encoding="utf-8").read().strip().splitlines() if os.path.isfile(cons_path) else []
    check(st == 200 and resp.get("ok") is True and resp.get("c") == 2 and len(lines) == 1,
          f"BCN1 record lands in consents.jsonl and gets 2 verified copies ({resp}, lines={len(lines)})")
    saved = json.loads(lines[0])
    check(saved["id"] == rec_id and saved["dev"] == dev_id and saved["by"] == "Hatem"
          and saved["seq"] == 29000001 and saved["mode"] == "5" and saved["a"] is True,
          f"consents.jsonl entry holds all verified fields ({saved})")
    check(srv.store.ledger.count(rec_id) == 2 and rec_id not in srv.store.records,
          "consent record is in CopyLedger (2 copies) and not in photo records")

    # 2. A duplicate with the same id is idempotent (changes nothing in consents.jsonl)
    st_dup, resp_dup_raw = post_sealed_item(meta, bcn_body)
    lines_after_dup = open(cons_path, encoding="utf-8").read().strip().splitlines()
    check(st_dup == 200 and len(lines_after_dup) == 1 and lines_after_dup == lines,
          "duplicate consent record is idempotent (consents.jsonl unchanged)")

    # 3. Withdrawal record (a: False) appends a second distinct line
    withdraw_id = f"cn-{dev_id}-29000001-0"
    bcn_withdraw = b"BCN1" + json.dumps({
        "v": 1,
        "dev": dev_id,
        "by": "Hatem",
        "seq": 29000001,
        "mode": "5",
        "hours": [[0, 480, 1020]],
        "a": False,
        "t": now_ms + 5000,
    }).encode("utf-8")
    st_w, _ = post_sealed_item(dict(meta, id=withdraw_id), bcn_withdraw)
    lines_w = open(cons_path, encoding="utf-8").read().strip().splitlines()
    check(st_w == 200 and len(lines_w) == 2 and json.loads(lines_w[1])["a"] is False,
          "withdrawal consent record appended to consents.jsonl")

    # 4. Malformed records are refused with 400 and do not change consents.jsonl
    bad_cases = [
        ("wrong magic", dict(meta, id=f"cn-{dev_id}-bad1"), b"XXXX" + bcn_body[4:]),
        ("invalid json", dict(meta, id=f"cn-{dev_id}-bad2"), b"BCN1{not-json"),
        ("invalid mode", dict(meta, id=f"cn-{dev_id}-bad3"),
         b"BCN1" + json.dumps({"v": 1, "dev": dev_id, "by": "Hatem", "seq": 10, "mode": "evil", "hours": [], "a": True, "t": now_ms}).encode()),
        ("dev mismatch", dict(meta, id=f"cn-{dev_id}-bad4", device_id="dev_other_99999"),
         b"BCN1" + json.dumps({"v": 1, "dev": dev_id, "by": "Hatem", "seq": 10, "mode": "5", "hours": [], "a": True, "t": now_ms}).encode()),
        ("invalid hours", dict(meta, id=f"cn-{dev_id}-bad5"),
         b"BCN1" + json.dumps({"v": 1, "dev": dev_id, "by": "Hatem", "seq": 10, "mode": "5", "hours": [[9, 0, 60]], "a": True, "t": now_ms}).encode()),
    ]
    for label, m_bad, b_bad in bad_cases:
        st_b, _ = post_sealed_item(m_bad, b_bad)
        check(st_b == 400, f"malformed consent record ({label}) refused with 400 (got {st_b})")

    check(len(open(cons_path, encoding="utf-8").read().strip().splitlines()) == 2,
          "malformed records left consents.jsonl untouched")

    # 5. A BCN1 record sealed for rlFor('state') also imports cleanly on the backup PC via .bfx bundle
    sealed_v2 = envelope.seal([office_hk.public, backup_hk.public], dict(meta, id=f"cn-{dev_id}-29000002"), bcn_body)
    bfx_blob = bundle.pack_one([(f"cn-{dev_id}-29000002", sealed_v2, 0)])
    backup_store = server.PhotoStore(os.path.join(work, "backup_photos"))
    res = bundle.import_bundle(bfx_blob, backup_store, backup_hk)
    check(res["imported"] == 1 and f"cn-{dev_id}-29000002" in backup_store.consents,
          f"backup PC opens and imports BCN1 consent record from .bfx bundle ({res})")

    srv.stop()
finally:
    shutil.rmtree(work, ignore_errors=True)

print("ALL PASS" if not failures else f"{len(failures)} FAILED", flush=True)
sys.exit(1 if failures else 0)
