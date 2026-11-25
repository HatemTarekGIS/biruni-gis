r"""No-loss P6 stage B: PC cloud mailbox (tokens, BRMB1 descriptor, MailboxIntake pull & BRCP1 receipts,
state hygiene, and System Health rows) against local `tests\mb_node_server.mjs`.

Tests:
- tokens_match_worker
- descriptor_signed
- pull_saves_and_marks_r2
- receipt_written
- already_at_office
- refused_once
- state_no_secrets
- health_rows

Run:  engine\bin\python-qgis.bat tests\test_mailbox_pc.py
"""
import hashlib
import json
import os
import queue
import secrets
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QT_QPA_FONTDIR", r"C:\Windows\Fonts")

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, ".."))
sys.path.insert(0, os.path.join(ROOT, "app", "plugins"))
sys.path.insert(0, os.path.join(ROOT, "app", "python"))

NODE = shutil.which("node") or next(
    (p for p in (r"C:\Program Files\nodejs\node.exe", r"C:\Program Files (x86)\nodejs\node.exe") if os.path.isfile(p)),
    "node",
)

MB_SECRET = secrets.token_urlsafe(32)
SERVER_MJS = os.path.join(HERE, "mb_node_server.mjs")

node_env = dict(os.environ, MB_SECRET=MB_SECRET)
node_proc = subprocess.Popen(
    [NODE, SERVER_MJS],
    cwd=HERE,
    env=node_env,
    stdout=subprocess.PIPE,
    stderr=subprocess.PIPE,
    text=True,
    encoding="utf-8",
)

port = None
deadline = time.monotonic() + 15.0
while time.monotonic() < deadline:
    line = node_proc.stdout.readline()
    if not line:
        break
    if line.startswith("PORT "):
        port = int(line.split()[1])
        break

if not port:
    err_out = node_proc.stderr.read() if node_proc.stderr else ""
    node_proc.kill()
    raise RuntimeError(f"mb_node_server.mjs did not print PORT ({err_out})")

BASE_URL = f"http://127.0.0.1:{port}"
os.environ["BIRUNI_MB_BASE"] = BASE_URL

from biruni.vendor import ensure_vendor  # noqa: E402

ensure_vendor()
from cryptography.hazmat.primitives import hashes, serialization  # noqa: E402
from cryptography.hazmat.primitives.asymmetric import ec  # noqa: E402
from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature  # noqa: E402

from biruni.field_photos import bridge, drive, handover, mailbox, recipients  # noqa: E402
from biruni.field_photos.handover import HandoverKey  # noqa: E402
from biruni.field_photos.server import PhotoStore  # noqa: E402
from biruni.health import Ctx, checks, run as run_health  # noqa: E402

failures = []


def check(cond, msg):
    print(("PASS " if cond else "FAIL ") + msg, flush=True)
    if not cond:
        failures.append(msg)


def http_call(method, path, token=None, body=None, content_type=None):
    headers = {"User-Agent": "Biruni-Test"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if content_type:
        headers["Content-Type"] = content_type
    if body is not None:
        headers["Content-Length"] = str(len(body))
    req = urllib.request.Request(BASE_URL + path, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


class FakeSettings:
    def __init__(self):
        self.d = {}

    def value(self, k, default=None):
        return self.d.get(k, default)

    def setValue(self, k, v):
        self.d[k] = v


JPEG = (
    b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00"
    + b"\x11" * 128
    + b"\xff\xd9"
)

phone_sk = ec.generate_private_key(ec.SECP256R1())
phone_pub = drive._b64u(
    phone_sk.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
)


def make_bfp(k, pc_pub, pid, n, dev="dev_01", sk_str="", jpg=JPEG):
    meta = {
        "id": pid,
        "t": int(time.time() * 1000),
        "lat": 30.0444,
        "lon": 31.2357,
        "surveyor": "Hatem",
        "device_id": dev,
    }
    body = handover.seal(pc_pub, meta, jpg)
    h = {
        "room": drive.room_tag(k),
        "dev": dev,
        "kid": drive.key_id(pc_pub),
        "id": pid,
        "n": n,
        "t": meta["t"],
        "pub": phone_pub,
        "cert": drive.cert(drive.auth_key(k, sk_str), phone_pub, dev),
    }
    blob, fname = drive.pack(h, body, phone_sk)
    return blob, fname, hashlib.sha256(body).hexdigest()


def upload_to_worker(ut, room, dev, fname, blob):
    fsha = hashlib.sha256(blob).hexdigest()
    sign_req = json.dumps({
        "room": room,
        "dev": dev,
        "name": fname,
        "size": len(blob),
        "sha": fsha,
    }).encode("utf-8")
    st, raw = http_call("POST", "/mb/sign", token=ut, body=sign_req, content_type="application/json")
    if st != 200:
        raise RuntimeError(f"sign failed ({st}): {raw}")
    signed_url = json.loads(raw.decode("utf-8"))["url"]
    st2, raw2 = http_call("PUT", signed_url, body=blob, content_type="application/octet-stream")
    return st2, raw2


work = tempfile.mkdtemp(prefix="biruni_p6_mb_pc_")
try:
    st_store = FakeSettings()
    k = bridge.load_key(st_store)
    _id_sk, pin = bridge.identity(k)
    room = drive.room_tag(k)
    host = "biruni-relay.myorg.workers.dev"
    ut, ot = mailbox.tokens(MB_SECRET, room)

    # ---- 1. tokens_match_worker ------------------------------------------------------------------------------------
    pv = mailbox.probe(host)
    check(pv == {"rel": 2, "mb": True}, f"probe returns rel:2, mb:True ({pv})")

    cert_dir = os.path.join(work, "cert")
    photos_dir = os.path.join(work, "photos")
    mirror_dir = os.path.join(work, "mirror")
    os.makedirs(cert_dir, exist_ok=True)
    os.makedirs(photos_dir, exist_ok=True)
    os.makedirs(mirror_dir, exist_ok=True)
    hk = HandoverKey(cert_dir)

    blob1, fname1, ssha1 = make_bfp(k, hk.public, "mb-photo-0001", 1, dev="dev_01")
    put_st, _ = upload_to_worker(ut, room, "dev_01", fname1, blob1)
    list_st_ut, _ = http_call("GET", f"/mb/list?room={room}", token=ut)
    list_st_ot, list_raw = http_call("GET", f"/mb/list?room={room}", token=ot)
    listed_objs = json.loads(list_raw.decode("utf-8")).get("objects") or []
    check(put_st == 201 and list_st_ut == 403 and list_st_ot == 200 and len(listed_objs) == 1,
          f"tokens_match_worker (put={put_st}, ut_list={list_st_ut}, ot_list={list_st_ot})")

    # ---- 2. descriptor_signed --------------------------------------------------------------------------------------
    desc1 = mailbox.descriptor(k, host, MB_SECRET, 14)
    parsed1 = mailbox.verify_descriptor(desc1, pin, k[:12], tag=room)
    desc1_same = mailbox.descriptor(k, host, MB_SECRET, 14)
    parsed1_same = mailbox.verify_descriptor(desc1_same, pin, k[:12], tag=room, last_seq=parsed1["seq"])
    desc2 = mailbox.descriptor(k, host, MB_SECRET, 30)
    parsed2 = mailbox.verify_descriptor(desc2, pin, k[:12], tag=room, last_seq=parsed1["seq"])

    tampered = dict(desc2, l=desc2["l"].replace('"days":30', '"days":7'))
    tampered_refused = False
    try:
        mailbox.verify_descriptor(tampered, pin, k[:12], tag=room)
    except ValueError:
        tampered_refused = True

    wrong_pin_refused = False
    try:
        mailbox.verify_descriptor(desc2, "0" * 22, k[:12], tag=room)
    except ValueError:
        wrong_pin_refused = True

    rollback_refused = False
    try:
        mailbox.verify_descriptor(desc1, pin, k[:12], tag=room, last_seq=parsed2["seq"] + 1)
    except ValueError:
        rollback_refused = True

    check(parsed1["ut"] == ut
          and parsed1["tag"] == room
          and parsed1["days"] == 14
          and parsed1_same["seq"] == parsed1["seq"]
          and parsed2["seq"] > parsed1["seq"]
          and tampered_refused and wrong_pin_refused and rollback_refused,
          f"descriptor_signed (seq1={parsed1['seq']}, seq2={parsed2['seq']})")

    # ---- 3. pull_saves_and_marks_r2 --------------------------------------------------------------------------------
    store = PhotoStore(photos_dir, mirror_folder=mirror_dir)
    events = queue.Queue()
    intake = mailbox.MailboxIntake(host, MB_SECRET, store, hk, k, events, cert_dir, days=14)
    res_scan1 = intake.scan()
    rec1 = store.records.get("mb-photo-0001")
    ledger1 = store.ledger.get("mb-photo-0001")
    places1 = (ledger1 or {}).get("places") or {}
    c1, _h1, h8_1 = store.ledger.receipt_info("mb-photo-0001")
    check(res_scan1["new"] == 1
          and rec1 is not None and rec1.get("route") == "r2" and os.path.isfile(rec1["photo"])
          and bool(places1.get("office")) and bool(places1.get("r2")) and bool(places1.get("mirror"))
          and c1 >= 2 and h8_1 == ssha1[:8],
          f"pull_saves_and_marks_r2 (places={places1}, c={c1}, h8={h8_1})")

    # ---- 4. receipt_written ----------------------------------------------------------------------------------------
    rc_st, rc_raw = http_call("GET", f"/mb/rc/{room}/dev_01.json", token=ut)
    rc_doc = json.loads(rc_raw.decode("utf-8")) if rc_st == 200 else {}
    rc_items = rc_doc.get("items") or []
    rc_str = ",".join(f"{x[0]}:{x[1]}:{x[2]}" for x in rc_items)
    rc_signed_text = f"BRCP1|{rc_doc.get('nonce')}|{rc_doc.get('rt')}|{rc_str}".encode("utf-8")
    pub_raw = recipients._unb64u(rc_doc.get("ip") or "")
    sig_raw = recipients._unb64u(rc_doc.get("is") or "")
    pk = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), pub_raw)
    der = encode_dss_signature(int.from_bytes(sig_raw[:32], "big"), int.from_bytes(sig_raw[32:], "big"))
    pk.verify(der, rc_signed_text, ec.ECDSA(hashes.SHA256()))
    check(rc_st == 200
          and rc_doc.get("nonce") == "mb.dev_01"
          and rc_doc.get("room") == room
          and recipients.pin_of(rc_doc["ip"]) == pin
          and rc_items == [["mb-photo-0001", c1, ssha1[:8]]],
          f"receipt_written ({rc_doc})")

    # ---- 5. already_at_office --------------------------------------------------------------------------------------
    # Photo arrives first over direct bridge; later its .bfp is in R2 -> pull marks r2, no duplicate photo event
    meta2 = {
        "id": "mb-photo-0002",
        "t": int(time.time() * 1000),
        "lat": 30.05,
        "lon": 31.24,
        "surveyor": "Hatem",
        "device_id": "dev_01",
        "route": "direct",
    }
    blob2, fname2, ssha2 = make_bfp(k, hk.public, "mb-photo-0002", 2, dev="dev_01")
    store.save(meta2, JPEG, ssha=ssha2)
    while not events.empty():
        events.get_nowait()

    upload_to_worker(ut, room, "dev_01", fname2, blob2)
    res_scan2 = intake.scan()
    photo_evs = []
    while not events.empty():
        ev_k, ev_v = events.get_nowait()
        if ev_k == "photo":
            photo_evs.append(ev_v)
    places2 = (store.ledger.get("mb-photo-0002") or {}).get("places") or {}
    rc_st2, rc_raw2 = http_call("GET", f"/mb/rc/{room}/dev_01.json", token=ut)
    rc_items2 = {x[0]: x for x in (json.loads(rc_raw2.decode("utf-8")).get("items") or [])}
    check(res_scan2["new"] == 0
          and not photo_evs
          and bool(places2.get("office")) and bool(places2.get("r2"))
          and "mb-photo-0002" in rc_items2 and rc_items2["mb-photo-0002"][1] >= 2,
          f"already_at_office (places2={places2}, rc_items2={rc_items2})")

    # ---- 6. refused_once -------------------------------------------------------------------------------------------
    # Upload a damaged .bfp (signed with wrong office key); first scan refuses it, second scan skips it without re-fetching
    other_k = bridge.new_key()
    bad_blob, bad_name, _ = make_bfp(other_k, hk.public, "mb-photo-bad", 3, dev="dev_01")
    upload_to_worker(ut, room, "dev_01", bad_name, bad_blob)
    res_bad1 = intake.scan()
    res_bad2 = intake.scan()
    check(len(res_bad1["refused"]) == 1 and len(res_bad2["refused"]) == 0
          and "mb-photo-bad" not in store.records,
          f"refused_once (scan1={res_bad1['refused']}, scan2={res_bad2['refused']})")

    # ---- 7. state_no_secrets ---------------------------------------------------------------------------------------
    seen_file = os.path.join(photos_dir, mailbox.SEEN_NAME)
    state_file = os.path.join(cert_dir, mailbox.STATE_NAME)
    seen_txt = open(seen_file, encoding="utf-8").read()
    state_txt = open(state_file, encoding="utf-8").read()
    check(os.path.isfile(seen_file) and os.path.isfile(state_file)
          and all(tok not in seen_txt and tok not in state_txt for tok in (MB_SECRET, ut, ot)),
          "state_no_secrets: neither mailbox_seen.json nor mailbox_state.json holds MB_SECRET, ut, or ot")

    # ---- 8. health_rows --------------------------------------------------------------------------------------------
    now_s = time.time()
    h_settings = {
        checks.ORG_RELAY_SETTING: host,
        "biruni/mailbox/on": True,
        "biruni/mailbox/secret": "encrypted-blob",
    }
    ctx = Ctx(get_setting=h_settings.get, cert_folder=cert_dir, photos_folder=photos_dir, now=now_s)

    def mb_rows(c=ctx):
        return {r["id"]: r for r in run_health(c) if r["id"].startswith("mailbox_")}

    # Healthy state just written by intake.scan()
    r_ok = mb_rows()
    check(r_ok.get("mailbox_relay", {}).get("level") == "ok"
          and r_ok.get("mailbox_pull", {}).get("level") == "ok"
          and r_ok.get("mailbox_bytes", {}).get("level") == "ok"
          and r_ok.get("mailbox_quota", {}).get("level") == "ok",
          f"health_rows healthy ok ({r_ok})")

    # Storage at 75% (warn) and 95% (bad), quota at 80% (warn), pull 3 days old (warn), pull failed 8 days (bad)
    st_doc = mailbox.load_state(cert_dir)
    st_doc.update({
        "at": now_s - 3 * 86400,
        "ok": True,
        "err": "",
        "bytes": int(7.5 * 1000 * 1000 * 1000),
        "month_puts": 800_000,
    })
    mailbox.save_state(cert_dir, st_doc)
    r_warn = mb_rows()
    check(r_warn["mailbox_pull"]["level"] == "warn"
          and r_warn["mailbox_bytes"]["level"] == "warn"
          and r_warn["mailbox_quota"]["level"] == "warn",
          f"health_rows warn thresholds ({r_warn})")

    st_doc.update({
        "at": now_s - 8 * 86400,
        "ok": False,
        "err": "HTTP 500",
        "bytes": int(9.5 * 1000 * 1000 * 1000),
        "month_puts": 950_000,
    })
    mailbox.save_state(cert_dir, st_doc)
    r_bad = mb_rows()
    check(r_bad["mailbox_pull"]["level"] == "bad"
          and r_bad["mailbox_bytes"]["level"] == "bad"
          and r_bad["mailbox_quota"]["level"] == "bad",
          f"health_rows bad thresholds ({r_bad})")

    # Older relay (rel: None) -> warn with fix org_relay
    st_doc.update({"rel": None, "mb": False})
    mailbox.save_state(cert_dir, st_doc)
    r_old = mb_rows()
    check(r_old["mailbox_relay"]["level"] == "warn" and r_old["mailbox_relay"]["fix"] == "org_relay",
          f"health_rows old relay warn ({r_old})")

    # Relay without mailbox (mb: False) -> na
    st_doc.update({"rel": 2, "mb": False})
    mailbox.save_state(cert_dir, st_doc)
    r_nomb = mb_rows()
    check(r_nomb["mailbox_relay"]["level"] == "na", f"health_rows no-mailbox na ({r_nomb})")

    # No org relay and mailbox never on -> no mailbox rows
    empty_ctx = Ctx(get_setting={}.get, cert_folder=os.path.join(work, "empty_cert"), photos_folder=photos_dir, now=now_s)
    check(not mb_rows(empty_ctx), "health_rows omitted when no org relay and mailbox never configured")

finally:
    node_proc.kill()
    shutil.rmtree(work, ignore_errors=True)

print("ALL PASS" if not failures else f"{len(failures)} FAILED", flush=True)
sys.exit(1 if failures else 0)
