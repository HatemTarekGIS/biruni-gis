r"""Capture schema PC intake and progress view tests (Phase D).

Covers:
  - photo_with_cap_saved_and_columns
  - photo_without_cap_unchanged
  - state_via_http_sealed
  - state_via_drive_bfp
  - state_replay_and_out_of_order_rev
  - state_from_blocked_device_refused
  - state_from_revoked_sk_refused
  - bad_job_id_path_refused
  - tree_rebuilt_from_photos_only
  - progress_missing_with_reasons
  - two_devices_union
  - progress_dialog_opens_and_filters

Run:  engine\bin\python-qgis.bat tests\test_capture_intake.py
"""
import http.client
import json
import os
import queue
import shutil
import socket
import ssl
import sys
import tempfile
import urllib.parse

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "..", "app", "plugins"))
sys.path.insert(0, os.path.join(HERE, "..", "app", "python"))

from qgis.core import (Qgis, QgsApplication, QgsField, QgsFields, QgsProject,  # noqa: E402
                       QgsVectorFileWriter, QgsVectorLayer)
from qgis.PyQt.QtCore import QMetaType  # noqa: E402

SETTINGS_DIR = tempfile.mkdtemp()
os.environ["QGIS_CUSTOM_CONFIG_PATH"] = SETTINGS_DIR
app = QgsApplication([], True)
app.initQgis()

from biruni import i18n  # noqa: E402
from biruni.field_photos import bridge, drive  # noqa: E402
from biruni.field_photos import capture as cap  # noqa: E402
from biruni.field_photos import capture_jobs as cj  # noqa: E402
from biruni.field_photos import capture_progress as cp  # noqa: E402
from biruni.field_photos import capture_templates as ct  # noqa: E402
from biruni.field_photos import dock as fd  # noqa: E402
from biruni.field_photos import server as fs  # noqa: E402
from biruni.field_photos.handover import HandoverKey, seal  # noqa: E402
from biruni.vendor import ensure_vendor  # noqa: E402
import drivesim  # noqa: E402

ensure_vendor()
from cryptography.hazmat.primitives import serialization  # noqa: E402
from cryptography.hazmat.primitives.asymmetric import ec  # noqa: E402

i18n.init("en_US")
failures = []


def check(cond, msg):
    print(("PASS " if cond else "FAIL ") + msg, flush=True)
    if not cond:
        failures.append(msg)


work = tempfile.mkdtemp()
JPEG = b"\xff\xd8\xff\xe0" + b"\x00\x10JFIF\x00" + os.urandom(1200) + b"\xff\xd9"
TEXTS = {"title": "Biruni", "intro": "x", "start": "Start", "waiting": "w", "shoot": "Take",
         "note": "n", "sent": "s", "queued": "q", "connected": "c", "lost": "l", "denied": "d", "insecure": "i"}


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def phone_key():
    key = ec.generate_private_key(ec.SECP256R1())
    raw = key.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
    return key, drive._b64u(raw)


def drain(q):
    out = []
    while not q.empty():
        out.append(q.get_nowait())
    return out


# ---- 1. photo_without_cap_unchanged & 2. photo_with_cap_saved_and_columns --------------------------------
def test_photo_with_and_without_cap():
    folder = os.path.join(work, "photos_cap")
    store = fs.PhotoStore(folder)
    t_ms = 1767225600000

    # Photo WITHOUT cap: must be stored byte-identically to baseline (no "cap" key in clean_meta or photos.jsonl)
    meta_plain = {
        "id": "plainphoto-0001",
        "lat": 24.7136,
        "lon": 46.6753,
        "acc": 3.5,
        "alt": 610.0,
        "heading": None,
        "compass": 90,
        "t": t_ms,
        "fix_age": 1,
        "kind": "pole",
        "note": "plain photo",
        "device": "Android",
        "by": "Ali",
        "surveyor": "Ali",
        "device_id": "dev_ali_0001",
    }
    cleaned_plain = fs.clean_meta(meta_plain)
    check("cap" not in cleaned_plain, "photo_without_cap_unchanged: clean_meta has no 'cap' key when meta has no cap")

    rec_plain, new_plain = store.save(meta_plain, JPEG)
    check(new_plain is True and "cap" not in rec_plain, "photo_without_cap_unchanged: saved record has no 'cap' key")
    raw_lines = open(os.path.join(folder, fs.LOG_NAME), encoding="utf-8").read().splitlines()
    expected_plain = {
        "id": "plainphoto-0001",
        "lat": 24.7136,
        "lon": 46.6753,
        "acc": 3.5,
        "alt": 610.0,
        "heading": None,
        "compass": 90.0,
        "fix_age": 1.0,
        "taken": rec_plain["taken"],
        "kind": "pole",
        "note": "plain photo",
        "device": "Android",
        "by": "Ali",
        "surveyor": "Ali",
        "device_id": "dev_ali_0001",
        "route": "",
        "fields": {},
        "avg_n": None,
        "avg_sd": None,
        "level": None,
        "unit": "",
        "preview": False,
        "photo": rec_plain["photo"],
    }
    check(raw_lines[0] == json.dumps(expected_plain, ensure_ascii=False),
          "photo_without_cap_unchanged: photos.jsonl line is byte-identical to pre-Phase-D format")

    # Photo WITH invalid cap: invalid cap is dropped so record still has no 'cap' key
    cleaned_bad_cap = fs.clean_meta(dict(meta_plain, id="plainphoto-0002", cap={"j": "../bad", "k": ":front"}))
    check("cap" not in cleaned_bad_cap, "photo_without_cap_unchanged: invalid cap is dropped by clean_meta")

    # Photo WITH valid cap
    meta_cap = dict(
        meta_plain,
        id="capphoto-0001",
        cap={
            "j": "JBLD0001",
            "k": "rm0001:rooms",
            "pl": "Floor 1 › Apt 1 › Kitchen",
            "ch": [["floors", "fl0001", 1], ["apts", "ap0001", 1], ["rooms", "rm0001", 1]],
            "extra_ignored": "dropped",
        },
    )
    rec_cap, new_cap = store.save(meta_cap, JPEG)
    check(new_cap is True and rec_cap.get("cap") == {
        "j": "JBLD0001",
        "k": "rm0001:rooms",
        "pl": "Floor 1 › Apt 1 › Kitchen",
        "ch": [["floors", "fl0001", 1], ["apts", "ap0001", 1], ["rooms", "rm0001", 1]],
    }, "photo_with_cap_saved_and_columns: photo record stores cleaned cap in photos.jsonl")

    # Simulate an older project GPKG created BEFORE job_id and job_item existed in FIELDS
    legacy_fields = QgsFields()
    for name, t in fd.FIELDS:
        if name not in ("job_id", "job_item"):
            legacy_fields.append(QgsField(name, t))
    gpkg_path = os.path.join(folder, fd.GPKG)
    opts = QgsVectorFileWriter.SaveVectorOptions()
    opts.driverName = "GPKG"
    opts.layerName = fd.LAYER
    w = QgsVectorFileWriter.create(gpkg_path, legacy_fields, Qgis.WkbType.Point, fd.WGS84,
                                   QgsProject.instance().transformContext(), opts)
    del w
    legacy_check = QgsVectorLayer(f"{gpkg_path}|layername={fd.LAYER}", "legacy", "ogr")
    check(legacy_check.fields().indexOf("job_id") < 0 and legacy_check.fields().indexOf("job_item") < 0,
          "legacy GPKG starts without job_id and job_item columns")
    del legacy_check

    # open_layer and add_records safely add job_id and job_item to the existing layer and populate them
    lyr = fd.open_layer(folder, add=False)
    check(lyr.fields().indexOf("job_id") >= 0 and lyr.fields().indexOf("job_item") >= 0,
          "photo_with_cap_saved_and_columns: open_layer adds missing job_id and job_item columns to existing GPKG")
    added = fd.add_records(lyr, fs.read_log(folder))
    check(len(added) == 2, "photo_with_cap_saved_and_columns: both records added to GPKG layer")
    by_pid = {f["photo_id"]: f for f in lyr.getFeatures()}
    check(not by_pid["plainphoto-0001"]["job_id"] and not by_pid["plainphoto-0001"]["job_item"],
          "photo_without_cap_unchanged: plain photo has empty job_id and job_item")
    check(by_pid["capphoto-0001"]["job_id"] == "JBLD0001"
          and by_pid["capphoto-0001"]["job_item"] == "rm0001:rooms",
          "photo_with_cap_saved_and_columns: cap photo has job_id='JBLD0001' and job_item='rm0001:rooms'")


# ---- 3. state_via_http_sealed ----------------------------------------------------------------------------
def test_state_via_http_sealed():
    folder = os.path.join(work, "photos_http_state")
    port = free_port()
    srv = fs.PhotoServer(folder, os.path.join(work, "cert"), {"en": TEXTS}, "en", [], port, ["127.0.0.1"])
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE

    try:
        jstore = cj.JobStore(folder)
        job = jstore.create(ct.shop_template(), data={"job_no": "SH-200", "client": "Mona"}, job_id="JSHOP200")
        srv.store.jobs.reload()

        state_payload = {
            "v": 1,
            "job": job["id"],
            "dev": "dev_phone_01",
            "by": "Mona",
            "rev": 4,
            "t": 1767225605000,
            "s": job["s"],
            "inst": {},
            "ans": {
                ":signboard": {"shop_name": ["Al-Badr Shop", 1767225605000]},
                ":hall": {"area_m2": [85.0, 1767225605000], "condition": ["Good", 1767225605000]},
            },
            "note": {":signboard": ["Clean signboard", 1767225605000]},
            "ph": {":signboard": ["ph_fac_1", "ph_fac_2"]},
            "skip": {":storage": ["No storage room", 1767225605000]},
            "closed": None,
        }
        item_id = "js4-abcdef123"
        inner_meta = {
            "id": item_id,
            "t": 1767225605000,
            "cs": {"j": job["id"], "rev": 4},
            "device_id": "dev_phone_01",
            "surveyor": "Mona",
            "by": "Mona",
        }
        sealed_body = seal(srv.handover.public, inner_meta, b"BCS1" + json.dumps(state_payload).encode("utf-8"))
        outer_meta = {"id": item_id, "sealed": 1, "t": 1767225605000}

        q = urllib.parse.quote(json.dumps(outer_meta))
        c = http.client.HTTPSConnection("127.0.0.1", port, context=ctx, timeout=15)
        c.request("POST", f"{srv.path()}photo?m={q}", body=sealed_body,
                  headers={"Content-Type": "application/octet-stream"})
        r = c.getresponse()
        resp_body = json.loads(r.read().decode("utf-8"))
        c.close()

        check(r.status == 200 and resp_body.get("id") == item_id,
              f"state_via_http_sealed: server returns 200 with item id ({resp_body})")
        evs = drain(srv.events)
        check(("job", job["id"]) in evs and not any(k == "photo" for k, _ in evs),
              f"state_via_http_sealed: server puts ('job', '{job['id']}') and no 'photo' event ({evs})")

        state_file = os.path.join(folder, "jobs", job["id"], "state-dev_phone_01.json")
        check(os.path.isfile(state_file), "state_via_http_sealed: jobs/<jobId>/state-<dev>.json written on disk")
        saved_st = json.load(open(state_file, encoding="utf-8"))
        check(saved_st["rev"] == 4 and saved_st["ans"][":signboard"]["shop_name"][0] == "Al-Badr Shop"
              and saved_st["ans"][":hall"]["area_m2"][0] == 85.0
              and saved_st["skip"][":storage"][0] == "No storage room",
              "state_via_http_sealed: cleaned state contents match")
        check(not os.path.exists(os.path.join(folder, fs.LOG_NAME)),
              "state_via_http_sealed: state item is not written to photos.jsonl")
        check(srv.store.originals([item_id, "unknown-id"]) == [item_id],
              "state_via_http_sealed: originals() includes merged state item id")

        # Unknown job state item: kept in jobs/<jobId>/state-<dev>.json without job.json
        unk_id = "JUNK9999"
        unk_item_id = "js2-unk123456"
        unk_state = {
            "v": 1, "job": unk_id, "dev": "dev_phone_01", "by": "Mona", "rev": 2, "t": 1767225609000,
            "s": "shop@1", "inst": {}, "ans": {}, "note": {}, "ph": {":facade": ["p_unk_1"]}, "skip": {}, "closed": None,
        }
        unk_meta = {"id": unk_item_id, "t": 1767225609000, "cs": {"j": unk_id, "rev": 2},
                    "device_id": "dev_phone_01", "surveyor": "Mona", "by": "Mona"}
        unk_sealed = seal(srv.handover.public, unk_meta, b"BCS1" + json.dumps(unk_state).encode("utf-8"))
        c = http.client.HTTPSConnection("127.0.0.1", port, context=ctx, timeout=15)
        c.request("POST", f"{srv.path()}photo?m=" + urllib.parse.quote(json.dumps({"id": unk_item_id, "sealed": 1, "t": 1})),
                  body=unk_sealed, headers={"Content-Type": "application/octet-stream"})
        r_unk = c.getresponse()
        r_unk.read()
        c.close()
        check(r_unk.status == 200
              and os.path.isfile(os.path.join(folder, "jobs", unk_id, "state-dev_phone_01.json"))
              and not os.path.exists(os.path.join(folder, "jobs", unk_id, "job.json")),
              "state_via_http_sealed: unknown job state is kept without job.json")
    finally:
        srv.stop()


# ---- 4. state_via_drive_bfp ------------------------------------------------------------------------------
def test_state_via_drive_bfp():
    folder = os.path.join(work, "photos_drive_state")
    pc_key = HandoverKey(os.path.join(work, "pc_drive"))
    k = bridge.new_key()
    store = fs.PhotoStore(folder)
    job = store.jobs.create(ct.vacant_land_template(), data={"job_no": "VL-50", "client": "Tariq", "plot_no": "50"}, job_id="JLAND050")

    phone_root = os.path.join(work, "sim_phone")
    pc_root = os.path.join(work, "sim_pc")
    phone_sync = os.path.join(phone_root, "biruni-room-drive")
    pc_sync = os.path.join(pc_root, "biruni-room-drive")
    os.makedirs(phone_sync, exist_ok=True)
    os.makedirs(pc_sync, exist_ok=True)

    sim = drivesim.DriveSim({"phone": phone_root, "pc": pc_root})

    dev = "DEVDRV01"
    item_id = "js7-drvstate1"
    state_payload = {
        "v": 1,
        "job": job["id"],
        "dev": dev,
        "by": "Tariq",
        "rev": 7,
        "t": 1767225700000,
        "s": job["s"],
        "inst": {"cn0001": {"n": "corners", "p": "", "o": 1, "t": 100, "del": 0}},
        "ans": {"cn0001:corners": {"corner_id": ["C1", 100], "marker_type": ["Iron peg", 100]}},
        "note": {},
        "ph": {":north_bound": ["pn1"], "cn0001:corners": ["pc1"]},
        "skip": {},
        "closed": None,
    }
    meta = {
        "id": item_id,
        "t": 1767225700000,
        "cs": {"j": job["id"], "rev": 7},
        "device_id": dev,
        "surveyor": "Tariq",
        "by": "Tariq",
    }
    skey, spub = phone_key()
    sealed_body = seal(pc_key.public, meta, b"BCS1" + json.dumps(state_payload).encode("utf-8"))
    hdr = {
        "room": drive.room_tag(k),
        "dev": dev,
        "kid": drive.key_id(pc_key.public),
        "id": item_id,
        "n": 1,
        "t": meta["t"],
        "pub": spub,
        "cert": drive.cert(drive.auth_key(k, ""), spub, dev),
    }
    bfp_bytes, bfp_name = drive.pack(hdr, sealed_body, skey)
    phone_p_dir = os.path.join(phone_sync, drive.folder_name(k), "p-" + dev)
    os.makedirs(phone_p_dir, exist_ok=True)
    with open(os.path.join(phone_p_dir, bfp_name), "wb") as f:
        f.write(bfp_bytes)

    # Replicate from phone view to PC view through DriveSim
    sim.step()

    ev = queue.Queue()
    intake = drive.DriveIntake([pc_sync], store, pc_key, k, ev)
    scan_res = intake.scan()
    evs = drain(ev)

    check(scan_res["seen"] == 1 and scan_res["refused"] == [] and scan_res["new"] == 0,
          f"state_via_drive_bfp: DriveIntake accepts state .bfp without counting it as a photo ({scan_res})")
    check(("job", job["id"]) in evs and not any(kind == "photo" for kind, _ in evs),
          f"state_via_drive_bfp: ('job', '{job['id']}') event emitted ({evs})")
    st_path = os.path.join(folder, "jobs", job["id"], f"state-{dev}.json")
    check(os.path.isfile(st_path) and json.load(open(st_path, encoding="utf-8"))["rev"] == 7,
          "state_via_drive_bfp: state-<dev>.json written with rev=7")
    check(store.originals([item_id]) == [item_id],
          "state_via_drive_bfp: originals() returns the state item id so Drive cleanup frees it")


# ---- 5. state_replay_and_out_of_order_rev ----------------------------------------------------------------
def test_state_replay_and_out_of_order_rev():
    folder = os.path.join(work, "photos_replay")
    store = fs.PhotoStore(folder)
    job = store.jobs.create(ct.shop_template(), data={"job_no": "SH-REV"}, job_id="JREV0001")

    def send_rev(rev, note_txt, item_id):
        st = {
            "v": 1, "job": job["id"], "dev": "dev_rev_1", "by": "Ali", "rev": rev, "t": 1000 * rev,
            "s": job["s"], "inst": {}, "ans": {}, "note": {":facade": [note_txt, 1000 * rev]},
            "ph": {}, "skip": {}, "closed": None,
        }
        meta = {"id": item_id, "t": 1000 * rev, "cs": {"j": job["id"], "rev": rev},
                "device_id": "dev_rev_1", "surveyor": "Ali"}
        return store.save(meta, b"BCS1" + json.dumps(st).encode("utf-8"))

    send_rev(5, "Note at rev 5", "js5-item00001")
    merged5 = store.jobs.merged_state(job["id"])
    check(merged5["rev"] == 5 and merged5["note"][":facade"][0] == "Note at rev 5",
          "state_replay_and_out_of_order_rev: rev 5 saved")

    # Out-of-order older rev 3 must be ignored
    send_rev(3, "Stale note at rev 3", "js3-item00002")
    merged_after_3 = store.jobs.merged_state(job["id"])
    check(merged_after_3["rev"] == 5 and merged_after_3["note"][":facade"][0] == "Note at rev 5",
          "state_replay_and_out_of_order_rev: older rev 3 is ignored")

    # Replayed rev 5 must be ignored
    send_rev(5, "Replayed rev 5 different text", "js5-item00003")
    merged_after_replay = store.jobs.merged_state(job["id"])
    check(merged_after_replay["rev"] == 5 and merged_after_replay["note"][":facade"][0] == "Note at rev 5",
          "state_replay_and_out_of_order_rev: replayed rev 5 is ignored")

    # Newer rev 6 must win
    send_rev(6, "Updated note at rev 6", "js6-item00004")
    merged6 = store.jobs.merged_state(job["id"])
    check(merged6["rev"] == 6 and merged6["note"][":facade"][0] == "Updated note at rev 6",
          "state_replay_and_out_of_order_rev: newer rev 6 updates state")
    check(set(store.originals(["js5-item00001", "js3-item00002", "js5-item00003", "js6-item00004"])) == {
        "js5-item00001", "js3-item00002", "js5-item00003", "js6-item00004"
    }, "state_replay_and_out_of_order_rev: originals() acknowledges all received state item ids for Drive cleanup")


# ---- 6. state_from_blocked_device_refused & 7. state_from_revoked_sk_refused ----------------------------
def test_state_security_blocked_and_revoked():
    folder = os.path.join(work, "photos_sec_state")
    port = free_port()
    srv = fs.PhotoServer(folder, os.path.join(work, "cert"), {"en": TEXTS}, "en", [], port, ["127.0.0.1"])
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE

    try:
        job = srv.store.jobs.create(ct.shop_template(), data={"job_no": "SH-SEC"}, job_id="JSEC0001")

        # 6. Blocked device refused
        srv.store.block_device("dev_blocked_99", reason="Lost phone", name="BadPhone")
        st_blk = {
            "v": 1, "job": job["id"], "dev": "dev_blocked_99", "by": "BadPhone", "rev": 1, "t": 1000,
            "s": job["s"], "inst": {}, "ans": {}, "note": {}, "ph": {}, "skip": {}, "closed": None,
        }
        m_blk = {"id": "js1-blocked99", "t": 1000, "cs": {"j": job["id"], "rev": 1},
                 "device_id": "dev_blocked_99", "surveyor": "BadPhone"}
        sealed_blk = seal(srv.handover.public, m_blk, b"BCS1" + json.dumps(st_blk).encode("utf-8"))
        c = http.client.HTTPSConnection("127.0.0.1", port, context=ctx, timeout=15)
        c.request("POST", f"{srv.path()}photo?m=" + urllib.parse.quote(json.dumps({"id": "js1-blocked99", "sealed": 1, "t": 1000})),
                  body=sealed_blk, headers={"Content-Type": "application/octet-stream"})
        r_blk = c.getresponse()
        r_blk.read()
        c.close()

        check(r_blk.status == 403, f"state_from_blocked_device_refused: HTTP 403 returned ({r_blk.status})")
        check(not os.path.exists(os.path.join(folder, "jobs", job["id"], "state-dev_blocked_99.json")),
              "state_from_blocked_device_refused: no state file written for blocked device")
        evs = drain(srv.events)
        check(any(k == "rejected" and v.get("device_id") == "dev_blocked_99" for k, v in evs)
              and not any(k == "job" for k, _ in evs),
              f"state_from_blocked_device_refused: rejected event emitted and no job event ({evs})")

        # 7. Revoked surveyor key refused
        omar = srv.store.issue_surveyor_key("Omar")
        srv.store.revoke_surveyor_key(omar["id"], reason="Left the project")
        st_rev = {
            "v": 1, "job": job["id"], "dev": "dev_omar_phone", "by": "Omar", "rev": 2, "t": 2000,
            "s": job["s"], "inst": {}, "ans": {}, "note": {}, "ph": {}, "skip": {}, "closed": None,
        }
        m_rev = {"id": "js2-revoked01", "t": 2000, "cs": {"j": job["id"], "rev": 2},
                 "device_id": "dev_omar_phone", "surveyor": "Omar", "sk": omar["secret"]}
        sealed_rev = seal(srv.handover.public, m_rev, b"BCS1" + json.dumps(st_rev).encode("utf-8"))
        c = http.client.HTTPSConnection("127.0.0.1", port, context=ctx, timeout=15)
        c.request("POST", f"{srv.path()}photo?m=" + urllib.parse.quote(json.dumps({"id": "js2-revoked01", "sealed": 1, "t": 2000})),
                  body=sealed_rev, headers={"Content-Type": "application/octet-stream"})
        r_rev = c.getresponse()
        r_rev.read()
        c.close()

        check(r_rev.status == 403, f"state_from_revoked_sk_refused: HTTP 403 returned ({r_rev.status})")
        check(not os.path.exists(os.path.join(folder, "jobs", job["id"], f"state-{omar['id']}.json"))
              and not os.path.exists(os.path.join(folder, "jobs", job["id"], "state-dev_omar_phone.json")),
              "state_from_revoked_sk_refused: no state file written for revoked sk")
        rejected_ids = [r.get("id") for r in fs.read_rejected(folder)]
        check("js1-blocked99" in rejected_ids and "js2-revoked01" in rejected_ids,
              f"blocked device and revoked sk state attempts logged in rejected.jsonl ({rejected_ids})")
    finally:
        srv.stop()


# ---- 8. bad_job_id_path_refused --------------------------------------------------------------------------
def test_bad_job_id_path_refused():
    folder = os.path.join(work, "photos_bad_paths")
    store = fs.PhotoStore(folder)
    bad_job_ids = [
        "../evil",
        "..\\evil",
        "../../outside",
        "J123/../../evil",
        "ab",                       # too short (< 4)
        "J" * 33,                   # too long (> 32)
        "J" * 80,                   # very long
        "معاملة123",                # unicode Arabic letters
        "J12345\u0661",             # unicode Arabic-Indic digit
        "J123_é",                   # non-ASCII Latin
        "J123 456",                 # space
        "J123\n456",                # newline
    ]
    for bjid in bad_job_ids:
        st = {"v": 1, "job": bjid, "dev": "dev_valid_1", "rev": 1, "t": 1000, "inst": {}, "ans": {}, "note": {}, "ph": {}, "skip": {}}
        meta = {"id": "js1-badjob123", "t": 1000, "cs": {"j": bjid, "rev": 1}, "device_id": "dev_valid_1"}
        refused_save = False
        try:
            store.save(meta, b"BCS1" + json.dumps(st).encode("utf-8"))
        except ValueError:
            refused_save = True
        refused_take = False
        try:
            store.jobs.take_state(bjid, "dev_valid_1", st)
        except ValueError:
            refused_take = True
        check(refused_save and refused_take, f"bad_job_id_path_refused: refused bad job_id {bjid!r}")

    bad_devs = ["../evil_dev", "..\\evil_dev", "a", "D" * 65, "جهاز_1", "dev 1", "dev/1"]
    for bdev in bad_devs:
        st = {"v": 1, "job": "JGOOD123", "dev": bdev, "rev": 1, "t": 1000, "inst": {}, "ans": {}, "note": {}, "ph": {}, "skip": {}}
        meta = {"id": "js1-baddev123", "t": 1000, "cs": {"j": "JGOOD123", "rev": 1}, "device_id": bdev}
        refused_save = False
        try:
            store.save(meta, b"BCS1" + json.dumps(st).encode("utf-8"))
        except ValueError:
            refused_save = True
        refused_take = False
        try:
            store.jobs.take_state("JGOOD123", bdev, st)
        except ValueError:
            refused_take = True
        check(refused_save and refused_take, f"bad_job_id_path_refused: refused bad dev {bdev!r}")

    check(not os.path.exists(os.path.join(work, "evil"))
          and not os.path.exists(os.path.join(folder, "evil")),
          "bad_job_id_path_refused: nothing written outside jobs/<safe jobId>/")


# ---- 9. tree_rebuilt_from_photos_only & 10. progress_missing_with_reasons --------------------------------
def test_tree_rebuilt_and_missing_with_reasons():
    folder = os.path.join(work, "photos_rebuild")
    store = fs.PhotoStore(folder)
    bldg = ct.building_template()
    job = store.jobs.create(bldg, data={"job_no": "BLD-300", "client": "Al-amal"}, job_id="JBLD0300")

    # Save photos covering facades, entrance, stairs, and floor 1 -> apt 1 -> room 1 & room 2
    slot_photos = [
        ("ph-front-001", ":front", "Facades › Front facade", []),
        ("ph-right-001", ":right", "Facades › Right facade", []),
        ("ph-left-001", ":left", "Facades › Left facade", []),
        ("ph-back-001", ":back", "Facades › Back facade", []),
        ("ph-entr-001", ":entrance", "Entrance & stairs › Entrance", []),
        ("ph-stair-001", ":stairs", "Entrance & stairs › Stairs", []),
        ("ph-room1-001", "rm0001:rooms", "Ground › 101 › Kitchen",
         [["floors", "fl0001", 1], ["apts", "ap0001", 1], ["rooms", "rm0001", 1]]),
        ("ph-room1-002", "rm0001:rooms", "Ground › 101 › Kitchen",
         [["floors", "fl0001", 1], ["apts", "ap0001", 1], ["rooms", "rm0001", 1]]),
    ]
    for idx, (pid, k, pl, ch) in enumerate(slot_photos):
        m = {
            "id": pid,
            "lat": 24.7136 + idx * 0.0001,
            "lon": 46.6753 + idx * 0.0001,
            "t": 1767225800000 + idx * 1000,
            "by": "Ali",
            "surveyor": "Ali",
            "device_id": "dev_ali_01",
            "cap": {"j": job["id"], "k": k, "pl": pl, "ch": ch},
        }
        store.save(m, JPEG)

    # Save a state snapshot that has answers for apt 1 & room 1, plus a 2nd apartment (ap0002) that is missing
    state1 = {
        "v": 1, "job": job["id"], "dev": "dev_ali_01", "by": "Ali", "rev": 1, "t": 1767225810000, "s": job["s"],
        "inst": {
            "fl0001": {"n": "floors", "p": "", "o": 1, "t": 100, "del": 0},
            "ap0001": {"n": "apts", "p": "fl0001", "o": 1, "t": 110, "del": 0},
            "ap0002": {"n": "apts", "p": "fl0001", "o": 2, "t": 120, "del": 0},
            "rm0001": {"n": "rooms", "p": "ap0001", "o": 1, "t": 130, "del": 0},
        },
        "ans": {
            "fl0001:floors": {"floor_name": ["Ground", 100]},
            "ap0001:apts": {"apt_no": ["101", 110], "use": ["Residential", 110], "condition": ["Good", 110]},
            "rm0001:rooms": {"room_type": ["Kitchen", 130]},
        },
        "note": {},
        "ph": {},
        "skip": {"ap0002:apts": ["no", 140]},  # < 3 chars: invalid skip reason!
        "closed": None,
    }
    store.jobs.take_state(job["id"], "dev_ali_01", state1, item_id="js1-bld300a")

    records = fs.read_log(folder)
    merged1 = store.jobs.merged_state(job["id"])
    miss1 = cap.missing(job["schema"], merged1, records)
    miss1_keys = [m["k"] for m in miss1]
    check("ap0002:apts" in miss1_keys,
          f"progress_missing_with_reasons: 2-char skip reason is not accepted, ap0002:apts is missing ({miss1_keys})")

    # Now give ap0002:apts a valid skip reason (>= 3 chars); subtree under ap0002 is covered -> 0 missing!
    state2 = dict(state1, rev=2, skip={"ap0002:apts": ["Apartment locked by owner", 200]})
    store.jobs.take_state(job["id"], "dev_ali_01", state2, item_id="js2-bld300b")
    merged2 = store.jobs.merged_state(job["id"])
    miss2 = cap.missing(job["schema"], merged2, records)
    check(miss2 == [], f"progress_missing_with_reasons: simulated building job with valid skip reason shows 0 missing ({miss2})")

    # 9. Delete all state-*.json files and rebuild tree from photos.jsonl alone
    jdir = os.path.join(folder, "jobs", job["id"])
    for fname in os.listdir(jdir):
        if fname.startswith("state-") and fname.endswith(".json"):
            os.remove(os.path.join(jdir, fname))
    check(store.jobs.states(job["id"]) == [], "tree_rebuilt_from_photos_only: all state-*.json files deleted")

    merged_no_state = store.jobs.merged_state(job["id"])
    rebuilt = cap.tree(job["schema"], merged_no_state, records)
    floors_node = next(r for r in rebuilt["roots"] if r["id"] == "floors")
    check(floors_node["instance_count"] == 1 and floors_node["instances"][0]["iid"] == "fl0001",
          "tree_rebuilt_from_photos_only: floor instance fl0001 rebuilt from photo cap.ch")
    apts_node = next(c for c in floors_node["instances"][0]["children"] if c["id"] == "apts")
    check(apts_node["instance_count"] == 1 and apts_node["instances"][0]["iid"] == "ap0001",
          "tree_rebuilt_from_photos_only: apartment instance ap0001 rebuilt from photo cap.ch")
    rooms_node = next(c for c in apts_node["instances"][0]["children"] if c["id"] == "rooms")
    check(rooms_node["instance_count"] == 1 and rooms_node["instances"][0]["iid"] == "rm0001"
          and rooms_node["instances"][0]["received_count"] == 2,
          "tree_rebuilt_from_photos_only: room instance rm0001 and its 2 photos rebuilt from photos.jsonl")


# ---- 11. two_devices_union -------------------------------------------------------------------------------
def test_two_devices_union():
    folder = os.path.join(work, "photos_two_devs")
    store = fs.PhotoStore(folder)
    bldg = ct.building_template()
    job = store.jobs.create(bldg, data={"job_no": "BLD-2DEV"}, job_id="J2DEV001")

    # Device A and Device B both work on J2DEV001:
    # - Both offline create a Floor 1 with order o=1 (duplicate instances fl_a01 and fl_b01)
    # - Device A also creates Floor 2 (fl_a02)
    # - Both answer note on :entrance (Device B has higher timestamp -> LWW wins)
    # - Device A has photo pf_a on :front (received on PC); Device B has photo pr_b on :right (only in ph on phone, not received yet)
    st_a = {
        "v": 1, "job": job["id"], "dev": "dev_surv_A", "by": "Ahmad", "rev": 3, "t": 5000, "s": job["s"],
        "inst": {
            "fla001": {"n": "floors", "p": "", "o": 1, "t": 1000, "del": 0},
            "fla002": {"n": "floors", "p": "", "o": 2, "t": 1100, "del": 0},
        },
        "ans": {"fla002:floors": {"floor_name": ["First floor", 5000]}},
        "note": {":entrance": ["Ahmad note", 5000]},
        "ph": {":front": ["pfa-000001"]},
        "skip": {},
        "closed": None,
    }
    st_b = {
        "v": 1, "job": job["id"], "dev": "dev_surv_B", "by": "Basem", "rev": 2, "t": 7000, "s": job["s"],
        "inst": {
            "flb001": {"n": "floors", "p": "", "o": 1, "t": 2000, "del": 0},
        },
        "ans": {},
        "note": {":entrance": ["Basem newer note", 7000]},
        "ph": {":right": ["prb-pending-1"]},
        "skip": {":stairs": ["Stairs under maintenance", 7000]},
        "closed": None,
    }
    store.jobs.take_state(job["id"], "dev_surv_A", st_a, item_id="js3-deva0001")
    store.jobs.take_state(job["id"], "dev_surv_B", st_b, item_id="js2-devb0001")

    # Only pfa-000001 has arrived as a photo record on the PC; prb-pending-1 is still on phone B
    store.save({
        "id": "pfa-000001", "lat": 24.71, "lon": 46.67, "t": 5000, "by": "Ahmad", "device_id": "dev_surv_A",
        "cap": {"j": job["id"], "k": ":front", "pl": "Facades › Front facade", "ch": []},
    }, JPEG)
    recs = fs.read_log(folder)

    merged = store.jobs.merged_state(job["id"])
    check(set(merged["devices"]) == {"dev_surv_A", "dev_surv_B"}, "two_devices_union: both devices merged")
    check(set(merged["inst"]) == {"fla001", "fla002", "flb001"}, "two_devices_union: instances are unioned across devices")
    check(merged["note"][":entrance"][0] == "Basem newer note", "two_devices_union: LWW note from dev B wins")
    check(merged["skip"][":stairs"][0] == "Stairs under maintenance", "two_devices_union: skip from dev B preserved")

    t = cap.tree(job["schema"], merged, recs)
    facades = next(r for r in t["roots"] if r["id"] == "facades")
    right_slot = next(c for c in facades["children"] if c["id"] == "right")
    check(right_slot["received_count"] == 0 and right_slot["pending_count"] == 1 and right_slot["photo_count"] == 1,
          "two_devices_union: photo in state.ph missing from records is counted as pending ('on the phone, not here yet')")

    dups = cap.duplicate_instances(t)
    check(len(dups) == 1 and set(dups[0]["iids"]) == {"fla001", "flb001"},
          f"two_devices_union: duplicate Floor 1 instances created offline by two surveyors are detected ({dups})")


# ---- 12. progress_dialog_opens_and_filters ---------------------------------------------------------------
def test_progress_dialog_opens_and_filters():
    folder = os.path.join(work, "photos_two_devs")
    store = fs.PhotoStore(folder)
    job_id = "J2DEV001"

    # Also add an unbound photo (old page without cap) taken during this job, and a bound extra photo ('*')
    store.save({
        "id": "unbound-oldpage-1", "lat": 24.711, "lon": 46.671, "t": int(store.jobs.get(job_id)["created"]) + 2000,
        "by": "Ahmad", "device_id": "dev_surv_A", "note": "From old cached page",
    }, JPEG)
    store.save({
        "id": "extraphoto-0001", "lat": 24.712, "lon": 46.672, "t": int(store.jobs.get(job_id)["created"]) + 3000,
        "by": "Ahmad", "device_id": "dev_surv_A",
        "cap": {"j": job_id, "k": "*", "pl": "*", "ch": []},
    }, JPEG)

    recs = fs.read_log(folder)
    lyr = fd.open_layer(folder, add=True)
    fd.add_records(lyr, recs)

    # Check JobsDialog percent column and Progress… button
    jdlg = cj.JobsDialog(store.jobs, records=recs)
    check(jdlg.table.topLevelItemCount() == 1 and "%" in jdlg.table.topLevelItem(0).text(4),
          f"JobsDialog percent column computed from merged states and records: {jdlg.table.topLevelItem(0).text(4)}")

    dlg = cp.ProgressDialog(store.jobs, job_id, recs, iface=None, layer=lyr)
    check(dlg.tree.columnCount() == 6, "progress_dialog_opens_and_filters: tree has 6 columns")
    all_keys = dlg.visible_keys()
    check(":front" in all_keys and ":stairs" in all_keys and ":left" in all_keys
          and "unbound" in all_keys and "duplicates" in all_keys and "*" in all_keys,
          f"progress_dialog_opens_and_filters: shows tree items, extra photos, unbound bucket, and duplicate instances bucket ({all_keys})")

    # Toggle 'Missing only': completed (:front), skipped (:stairs), and extra ('*') must be hidden, while missing (:left, :back) stay visible
    dlg.missing_box.setChecked(True)
    miss_vis = dlg.visible_keys()
    check(":left" in miss_vis and ":back" in miss_vis
          and ":front" not in miss_vis and ":stairs" not in miss_vis and "*" not in miss_vis,
          f"progress_dialog_opens_and_filters: 'Missing only' hides done/skipped items and keeps missing items ({miss_vis})")

    dlg.missing_box.setChecked(False)
    check(":front" in dlg.visible_keys() and ":stairs" in dlg.visible_keys(),
          "progress_dialog_opens_and_filters: unchecking 'Missing only' restores all items")

    # Double-clicking / selecting an item selects its photos on the photos layer by job_id / job_item
    fids_front = dlg.select_item_photos(":front")
    sel_pids = [f["photo_id"] for f in lyr.getSelectedFeatures()]
    check(len(fids_front) == 1 and sel_pids == ["pfa-000001"],
          f"progress_dialog_opens_and_filters: selecting ':front' selects its feature on the layer ({sel_pids})")

    # Selecting the unbound bucket selects the unbound photo
    for i in range(dlg.tree.topLevelItemCount()):
        top_it = dlg.tree.topLevelItem(i)
        meta = top_it.data(0, dlg.tree.Qt.ItemDataRole.UserRole if hasattr(dlg.tree, "Qt") else cp.Qt.ItemDataRole.UserRole) or {}
        if meta.get("kind") == "unbound":
            dlg._on_item_double_clicked(top_it, 0)
            break
    sel_unbound = [f["photo_id"] for f in lyr.getSelectedFeatures()]
    check(sel_unbound == ["unbound-oldpage-1"],
          f"progress_dialog_opens_and_filters: double-clicking unbound bucket selects unbound photo ({sel_unbound})")

    # Unknown job in ProgressDialog
    store.jobs.take_state("JUNKNOWN77", "dev_surv_A", {
        "v": 1, "job": "JUNKNOWN77", "dev": "dev_surv_A", "by": "Ahmad", "rev": 1, "t": 9000,
        "s": "shop@1", "inst": {}, "ans": {}, "note": {}, "ph": {":facade": ["p1"]}, "skip": {}, "closed": None,
    })
    udlg = cp.ProgressDialog(store.jobs, "JUNKNOWN77", recs, iface=None, layer=lyr)
    check(udlg.unknown_job is True and "Unknown job" in udlg.windowTitle(),
          f"progress_dialog_opens_and_filters: unknown job is listed as Unknown job ({udlg.windowTitle()})")


test_photo_with_and_without_cap()
test_state_via_http_sealed()
test_state_via_drive_bfp()
test_state_replay_and_out_of_order_rev()
test_state_security_blocked_and_revoked()
test_bad_job_id_path_refused()
test_tree_rebuilt_and_missing_with_reasons()
test_two_devices_union()
test_progress_dialog_opens_and_filters()

QgsProject.instance().removeAllMapLayers()
shutil.rmtree(work, ignore_errors=True)
print("ALL PASS" if not failures else f"{len(failures)} FAILED", flush=True)
app.exitQgis()
sys.exit(1 if failures else 0)
