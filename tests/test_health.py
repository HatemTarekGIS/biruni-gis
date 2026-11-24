r"""No-loss P4: system health checks, account owners, departures, `hs` status receiver, and persistent Presence.

Tests:
- red_without_key_backup
- red_without_second_copy
- amber_items_older_than_3_days
- red_at_5_days
- owners_persist
- departure_flags_owner_and_holder
- personal_email_warns
- raising_check_is_na
- hs_route_updates_presence
- hs_refused_for_blocked_device_or_bad_sk
- presence_survives_restart

Run:  engine\bin\python-qgis.bat tests\test_health.py
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

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QT_QPA_FONTDIR", r"C:\Windows\Fonts")

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, ".."))
sys.path.insert(0, os.path.join(ROOT, "app", "plugins"))
sys.path.insert(0, os.path.join(ROOT, "app", "python"))

from biruni.field_photos import bridge, ladder, recipients, server  # noqa: E402
from biruni.field_photos.handover import HandoverKey  # noqa: E402
from biruni.field_photos.presence import Presence  # noqa: E402
from biruni.health import Ctx, checks, departures, is_personal, mark_left, owners, register, run, set_key_backup, set_owner, worst  # noqa: E402

failures = []


def check(cond, msg):
    print(("PASS " if cond else "FAIL ") + msg, flush=True)
    if not cond:
        failures.append(msg)


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def row_by_id(rows, rid):
    return next((r for r in rows if r["id"] == rid), None)


work = tempfile.mkdtemp(prefix="biruni_p4_health_")
try:
    cert_dir = os.path.join(work, "cert")
    photos_dir = os.path.join(work, "photos")
    mirror_dir = os.path.join(work, "mirror")
    os.makedirs(cert_dir, exist_ok=True)
    os.makedirs(photos_dir, exist_ok=True)
    hk = HandoverKey(cert_dir)
    now_s = 1764000000.0
    today_str = time.strftime("%Y-%m-%d", time.gmtime(now_s))

    settings = {}
    ctx = Ctx(get_setting=settings.get, cert_folder=cert_dir, photos_folder=photos_dir, now=now_s)

    # ---- 1. red_without_key_backup & red_without_second_copy -------------------------------------------------------
    rows = run(ctx)
    kb_row = row_by_id(rows, "key_backup")
    sc_row = row_by_id(rows, "second_copy")
    check(kb_row is not None and kb_row["level"] == "bad" and kb_row["fix"] == "key_backup",
          f"red_without_key_backup ({kb_row})")
    check(sc_row is not None and sc_row["level"] == "bad" and sc_row["fix"] == "second_copy",
          f"red_without_second_copy ({sc_row})")
    check(worst(rows) == "bad", "worst(rows) is bad when key backup or second copy is missing")

    # v1 key backup or mismatched kid is also bad; >180 days is warn; v2 current is ok
    settings[checks.KEY_BACKUP_SETTING] = json.dumps({"kid": hk.kid, "at": today_str, "v": 1})
    check(row_by_id(run(ctx), "key_backup")["level"] == "bad", "v1 key backup is bad")
    settings[checks.KEY_BACKUP_SETTING] = json.dumps({"kid": "0000000000000000", "at": today_str, "v": 2})
    check(row_by_id(run(ctx), "key_backup")["level"] == "bad", "key backup with another kid is bad")
    old_date = time.strftime("%Y-%m-%d", time.gmtime(now_s - 185 * 86400))
    settings[checks.KEY_BACKUP_SETTING] = json.dumps({"kid": hk.kid, "at": old_date, "v": 2})
    check(row_by_id(run(ctx), "key_backup")["level"] == "warn", "key backup older than 180 days is warn (amber)")
    settings[checks.KEY_BACKUP_SETTING] = json.dumps({"kid": hk.kid, "at": today_str, "v": 2, "ts": now_s})
    check(row_by_id(run(ctx), "key_backup")["level"] == "ok", "v2 key backup of current kid is ok")

    # Unreachable mirror folder is bad; existing mirror folder is ok/warn
    settings[checks.MIRROR_FOLDER_SETTING] = os.path.join(work, "nonexistent_mirror")
    check(row_by_id(run(ctx), "second_copy")["level"] == "bad", "unreachable second copy folder is bad")
    os.makedirs(mirror_dir, exist_ok=True)
    settings[checks.MIRROR_FOLDER_SETTING] = mirror_dir
    check(row_by_id(run(ctx), "second_copy")["level"] in ("ok", "warn"),
          "existing second copy folder is no longer bad")

    # ---- 2. amber_items_older_than_3_days & red_at_5_days ----------------------------------------------------------
    pres = Presence(photos_dir)
    # A surveyor known only from position (no hs yet) -> na ("not reported yet")
    pres.note_pos({"id": "dev_old_page", "name": "OldPage", "lat": 24.7, "lon": 46.7, "t": int(now_s * 1000)}, now=now_s)
    # 3.5 days old pending item -> warn (amber)
    old_3_5d_ms = int((now_s - 3.5 * 86400) * 1000)
    pres.note_hs({"d": "dev_amber_3d", "n": "Tariq", "q": [2, 0, 0, 0], "old": old_3_5d_ms, "lp": 0, "t": int(now_s * 1000)}, now=now_s)
    # 5.2 days old pending item -> bad (red)
    old_5_2d_ms = int((now_s - 5.2 * 86400) * 1000)
    pres.note_hs({"d": "dev_red_5d", "n": "Ziad", "q": [1, 1, 0, 0], "old": old_5_2d_ms, "lp": 4, "t": int(now_s * 1000)}, now=now_s)
    # Refused item (n9 > 0) -> warn (amber)
    pres.note_hs({"d": "dev_refused_n9", "n": "Mazen", "q": [0, 0, 0, 1], "old": None, "lp": 0, "t": int(now_s * 1000)}, now=now_s)
    # Clean surveyor -> ok (green)
    pres.note_hs({"d": "dev_clean_ok", "n": "Salim", "q": [0, 0, 0, 0], "old": None, "lp": 0, "t": int(now_s * 1000)}, now=now_s)
    pres.save(force=True)

    rows = run(ctx)
    r_na = row_by_id(rows, "surveyor:dev_old_page")
    r_3d = row_by_id(rows, "surveyor:dev_amber_3d")
    r_5d = row_by_id(rows, "surveyor:dev_red_5d")
    r_n9 = row_by_id(rows, "surveyor:dev_refused_n9")
    r_ok = row_by_id(rows, "surveyor:dev_clean_ok")
    check(r_na is not None and r_na["level"] == "na", f"surveyor without hs is na ({r_na})")
    check(r_3d is not None and r_3d["level"] == "warn", f"amber_items_older_than_3_days ({r_3d})")
    check(r_5d is not None and r_5d["level"] == "bad", f"red_at_5_days ({r_5d})")
    check(r_n9 is not None and r_n9["level"] == "warn", f"surveyor with n9>0 is warn ({r_n9})")
    check(r_ok is not None and r_ok["level"] == "ok", f"surveyor with 0 pending is ok ({r_ok})")

    # ---- 3. owners_persist & personal_email_warns ------------------------------------------------------------------
    owners.set_org(cert_dir, "مكتب البيروني للمساحة")
    set_owner(cert_dir, "drive", "أحمد محمود", "it@biruni-office.sa", at="2026-11-24")
    set_owner(cert_dir, "cloudflare", "خالد سعيد", "khalid.personal@gmail.com", at="2026-11-24")
    set_owner(cert_dir, "domain", "منى عادل", "domains@biruni-office.sa", at="2026-11-24")
    set_key_backup(cert_dir, holder="أحمد محمود", where="خزنة المكتب الرئيسية")

    loaded_doc = owners.load(cert_dir)
    check(loaded_doc["org"] == "مكتب البيروني للمساحة"
          and loaded_doc["services"]["drive"]["owner"] == "أحمد محمود"
          and loaded_doc["services"]["drive"]["email"] == "it@biruni-office.sa"
          and loaded_doc["key_backup"]["where"] == "خزنة المكتب الرئيسية",
          f"owners_persist ({loaded_doc})")

    check(is_personal("khalid.personal@gmail.com") is True
          and is_personal("user@hotmail.com") is True
          and is_personal("it@biruni-office.sa") is False,
          "is_personal detects personal vs organisation emails")

    rows = run(ctx)
    r_drv = row_by_id(rows, "owner:drive")
    r_cf = row_by_id(rows, "owner:cloudflare")
    r_dom = row_by_id(rows, "owner:domain")
    check(r_drv is not None and r_drv["level"] == "ok", f"org email owner is ok ({r_drv})")
    check(r_cf is not None and r_cf["level"] == "warn" and r_cf["fix"] == "owner:cloudflare",
          f"personal_email_warns ({r_cf})")
    check(r_dom is not None and r_dom["level"] == "ok", f"domain owner with org email is ok ({r_dom})")

    # ---- 4. departure_flags_owner_and_holder -----------------------------------------------------------------------
    em_holders = ["أحمد محمود", "سارة علي", "وليد حسن", "نور الدين", "ياسر كمال"]
    other_pc = HandoverKey(os.path.join(work, "em_key"))
    recipients.add(cert_dir, other_pc.public, "emergency", "Emergency key", office_pub=hk.public)
    rec_doc = recipients.load(cert_dir)
    rec_doc["emergency"] = {"made": today_str, "holders": em_holders}
    recipients.save(cert_dir, rec_doc)

    mark_left(cert_dir, "وليد حسن", date="2026-11-24")
    mark_left(cert_dir, "منى عادل", date="2026-11-24")
    dep = departures(owners.load(cert_dir), em_holders)
    check(dep["services"] == ["domain"] and dep["holders"] == [(3, "وليد حسن")],
          f"departures() reports service owner and holder who left ({dep})")

    rows = run(ctx)
    r_dom_left = row_by_id(rows, "owner:domain")
    r_h3_left = row_by_id(rows, "holder:3")
    check(r_dom_left is not None and r_dom_left["level"] == "bad"
          and r_h3_left is not None and r_h3_left["level"] == "bad",
          f"departure_flags_owner_and_holder (owner={r_dom_left}, holder={r_h3_left})")

    # ---- 5. raising_check_is_na ------------------------------------------------------------------------------------
    def _boom_check(_ctx):
        raise RuntimeError("disk probe failed")

    _boom_check.check_id = "test_boom"
    _boom_check.check_group = "Test"
    _boom_check.check_title = "Boom check"
    register(_boom_check, order=99)
    try:
        rows = run(ctx)
        r_boom = row_by_id(rows, "test_boom")
        check(r_boom is not None and r_boom["level"] == "na" and "disk probe failed" in r_boom["text"],
              f"raising_check_is_na ({r_boom})")
    finally:
        checks._CHECKS[:] = [(o, f) for o, f in checks._CHECKS if f is not _boom_check]

    # ---- 6. hs_route_updates_presence, hs_refused_for_blocked_device_or_bad_sk, presence_survives_restart ----------
    srv_photos_dir = os.path.join(work, "srv_photos")
    srv_cert_dir = os.path.join(work, "srv_cert")
    port = free_port()
    texts = {"en": server.phone_texts(lambda s: s)}
    srv = server.PhotoServer(srv_photos_dir, srv_cert_dir, texts, "en", (), port, ["127.0.0.1"], bind="127.0.0.1")
    bkey = bridge.new_key()
    srv.set_ladder(bkey)

    ssl_ctx = ssl.create_default_context()
    ssl_ctx.check_hostname = False
    ssl_ctx.verify_mode = ssl.CERT_NONE

    def post_hs(body_bytes):
        c = http.client.HTTPSConnection("127.0.0.1", port, context=ssl_ctx, timeout=10)
        c.request("POST", srv.path() + "hs", body=body_bytes, headers={"Content-Type": "application/json"})
        resp = c.getresponse()
        data = resp.read()
        c.close()
        return resp.status, data

    def drain_events():
        out = []
        while not srv.events.empty():
            out.append(srv.events.get_nowait())
        return out

    # Valid hs update
    hs_payload = {
        "d": "dev_surveyor_1",
        "n": "Hatem",
        "q": [3, 1, 2, 0],
        "old": int((now_s - 1200) * 1000),
        "lp": 5,
        "tm": "5",
        "cs": 42,
        "ps": True,
        "t": int(now_s * 1000),
    }
    st, _ = post_hs(json.dumps(hs_payload).encode("utf-8"))
    evs = [v for k, v in drain_events() if k == "hs"]
    srv_pres = Presence(srv_photos_dir)
    for e in evs:
        srv_pres.note_hs(e, now=now_s)
    srv_pres.save(force=True)
    p_rows = srv_pres.rows(now_s)
    check(st == 200 and len(evs) == 1 and len(p_rows) == 1
          and p_rows[0]["dev"] == "dev_surveyor_1"
          and p_rows[0]["name"] == "Hatem"
          and p_rows[0]["q"] == [3, 1, 2, 0]
          and p_rows[0]["lp"] == 5
          and p_rows[0]["tm"] == "5"
          and p_rows[0]["cs"] == 42,
          f"hs_route_updates_presence ({p_rows})")

    # Also test env.hs inside lpos via Ladder.take / server.take_env
    lad_client = ladder.Ladder(bkey)
    now_ms = int(time.time() * 1000)
    lpos_sealed = lad_client.seal({
        "t": "lpos", "v": 1, "d": "dev_ladder_hs", "e": "ep1", "lo": 1, "bl": 0,
        "b": [],
        "hs": {"d": "dev_ladder_hs", "n": "ViaLadder", "q": [1, 0, 0, 0], "old": now_ms - 5000, "lp": 2, "tm": "off", "cs": 7, "t": now_ms},
        "n": "nonce_hs_1", "ts": now_ms,
    })
    srv.take_env(lpos_sealed, "drive")
    lad_evs = [v for k, v in drain_events() if k == "hs"]
    check(len(lad_evs) == 1 and lad_evs[0]["d"] == "dev_ladder_hs" and lad_evs[0]["lp"] == 2 and lad_evs[0]["tm"] == "off",
          f"Ladder.take copies env.hs into last_info and emits hs ({lad_evs})")
    srv_pres.note_hs(lad_evs[0], now=now_s)
    srv_pres.save(force=True)

    # Blocked device, bad sk, and oversized hs (>512 bytes) refused
    srv.store.block_device("dev_surveyor_1", reason="left")
    st_blocked, _ = post_hs(json.dumps(hs_payload).encode("utf-8"))
    st_bad_sk, _ = post_hs(json.dumps(dict(hs_payload, d="dev_other_99", sk="wrong_secret_key")).encode("utf-8"))
    st_big, _ = post_hs((json.dumps(dict(hs_payload, d="dev_other_99", pad="x" * 600))).encode("utf-8"))
    check(st_blocked == 400 and st_bad_sk == 400 and st_big == 413 and not [v for k, v in drain_events() if k == "hs"],
          f"hs_refused_for_blocked_device_or_bad_sk (blocked={st_blocked}, bad_sk={st_bad_sk}, big={st_big})")

    # Verify hs never touched CopyLedger
    check(srv.store.ledger.get("dev_surveyor_1") is None and srv.store.ledger.get("dev_ladder_hs") is None,
          "hs is display-only and never touches CopyLedger")

    srv.stop()

    # Presence survives restart (fresh Presence instance reads team_seen.json from disk)
    restarted_pres = Presence(srv_photos_dir)
    r_rows = {r["dev"]: r for r in restarted_pres.rows(now_s)}
    check("dev_surveyor_1" in r_rows and r_rows["dev_surveyor_1"]["q"] == [3, 1, 2, 0]
          and "dev_ladder_hs" in r_rows and r_rows["dev_ladder_hs"]["name"] == "ViaLadder",
          f"presence_survives_restart ({r_rows})")

    # ---- 7. Non-blocking HealthDialog & startup alert at most once a day -------------------------------------------
    from qgis.core import QgsApplication, QgsSettings
    from biruni.health.dialog import ALERT_SETTING, HealthDialog, check_on_startup

    qgs_settings_dir = os.path.join(work, "qgs_cfg")
    os.environ["QGIS_CUSTOM_CONFIG_PATH"] = qgs_settings_dir
    _qapp = QgsApplication.instance() or QgsApplication([], True)
    if not QgsApplication.prefixPath():
        _qapp.initQgis()

    class _FakeBar:
        def __init__(self):
            self.pushed = []

        def createMessage(self, title, text):
            from qgis.PyQt.QtWidgets import QHBoxLayout, QWidget

            w = QWidget()
            QHBoxLayout(w)
            w.text = text
            return w

        def pushWidget(self, item, level, duration=0):
            self.pushed.append(item.text)

    class _FakeIface:
        def __init__(self):
            self.bar = _FakeBar()

        def mainWindow(self):
            return None

        def messageBar(self):
            return self.bar

    class _FakePlugin:
        def __init__(self):
            self.iface = _FakeIface()

    fp = _FakePlugin()
    bad_ctx = Ctx(get_setting={}.get, cert_folder=os.path.join(work, "empty_cert"), photos_folder=photos_dir, now=now_s)
    QgsSettings().setValue(ALERT_SETTING, "")
    alerted1 = check_on_startup(fp, ctx=bad_ctx, today="2026-11-24")
    alerted2 = check_on_startup(fp, ctx=bad_ctx, today="2026-11-24")
    alerted3 = check_on_startup(fp, ctx=bad_ctx, today="2026-11-25")
    check(alerted1 is True and alerted2 is False and alerted3 is True and len(fp.iface.bar.pushed) == 2,
          f"startup health alert fires at most once per day (pushed={len(fp.iface.bar.pushed)})")

    dlg = HealthDialog(fp, ctx_factory=lambda: ctx)
    check(dlg.tree.topLevelItemCount() == len(run(ctx)),
          f"HealthDialog populates all {dlg.tree.topLevelItemCount()} check rows without blocking")
    dlg.close()

finally:
    shutil.rmtree(work, ignore_errors=True)

print("ALL PASS" if not failures else f"{len(failures)} FAILED", flush=True)
sys.exit(1 if failures else 0)
