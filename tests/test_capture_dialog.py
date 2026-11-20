r"""Capture schema PC dialog, jobs store, and server guide carrier tests (Phase B).

Run:  engine\bin\python-qgis.bat tests\test_capture_dialog.py
"""
import copy
import http.client
import json
import os
import shutil
import socket
import ssl
import sys
import tempfile

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "app", "plugins"))
sys.path.insert(0, os.path.join(HERE, "..", "app", "python"))

from qgis.core import QgsApplication  # noqa: E402

SETTINGS_DIR = tempfile.mkdtemp()
os.environ["QGIS_CUSTOM_CONFIG_PATH"] = SETTINGS_DIR
app = QgsApplication([], True)
app.initQgis()

from qgis.PyQt.QtCore import Qt  # noqa: E402
from biruni.field_photos import capture as cap  # noqa: E402
from biruni.field_photos import capture_dialog as cd  # noqa: E402
from biruni.field_photos import capture_jobs as cj  # noqa: E402
from biruni.field_photos import capture_templates as ct  # noqa: E402
from biruni.field_photos import form_dialog as fmd  # noqa: E402
from biruni.field_photos import server as fs  # noqa: E402

failures = []


def check(cond, msg):
    print(("PASS " if cond else "FAIL ") + msg)
    if not cond:
        failures.append(msg)


work = tempfile.mkdtemp()


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


# ---- test_schema_dialog_roundtrip -----------------------------------------------------------------------
def test_schema_dialog_roundtrip():
    for tpl in ct.builtin_templates():
        dlg = cd.SchemaDialog(tpl)
        err, built = dlg._check()
        check(err is None and built == tpl, f"SchemaDialog roundtrip preserves {tpl['id']} template")

    # Add a node, edit its properties in the side panel, save to office library
    dlg = cd.SchemaDialog(ct.shop_template())
    dlg.id_edit.setText("custom_shop")
    dlg.title_edit.setText("Custom shop survey")
    dlg._add_node(child=False)
    dlg.node_id_edit.setText("parking_lot")
    dlg.node_label_edit.setText("Parking lot")
    dlg.node_req_box.setChecked(False)
    dlg.repeat_box.setChecked(True)
    dlg.rep_min.setValue(0)
    dlg.rep_max.setValue(5)
    dlg.photos_box.setChecked(True)
    dlg.ph_min.setValue(1)
    dlg.ph_max.setValue(4)
    dlg._ok()
    check(dlg.schema["id"] == "custom_shop" and any(n["id"] == "parking_lot" for n in dlg.schema["nodes"]),
          "SchemaDialog edits and saves new node to office library")
    loaded = {s["id"]: s for s in cd.load_office_schemas()}
    check("custom_shop" in loaded and loaded["custom_shop"]["title"] == "Custom shop survey",
          "saved office schema is loaded from library folder")


# ---- test_node_questions_use_form_dialog ----------------------------------------------------------------
def test_node_questions_use_form_dialog():
    # FormDialog with keep=False hides the Keep column (column 6); title=True adds Title (column 7)
    fd_default = fmd.FormDialog([])
    check(not fd_default.table.isColumnHidden(6), "FormDialog shows Keep column by default")

    qs = [{"label": "Room type", "type": "choice", "choices": ["Kitchen", "Bedroom"], "req": True, "title": True}]
    fd_node = fmd.FormDialog(qs, keep=False, title=True)
    check(fd_node.table.isColumnHidden(6), "FormDialog(keep=False) hides the Keep column")
    check(fd_node.table.columnCount() == 8 and fd_node.table.item(0, 7).checkState() == Qt.CheckState.Checked,
          "FormDialog(title=True) preserves the title flag on node questions")
    err, cleaned = fd_node._check()
    check(err is None and cleaned[0].get("title") is True and cleaned[0].get("keep") is False,
          "FormDialog(keep=False, title=True) outputs title=True and keep=False")

    # Reserved columns include job_id and job_item so a question named job_id is renamed
    q_res = fs.clean_form([{"label": "job_id", "type": "text"}, {"label": "job_item", "type": "text"}])
    check(q_res[0]["name"] == "job_id_2" and q_res[1]["name"] == "job_item_2",
          "server.RESERVED blocks job_id and job_item from being used as question column names")


# ---- test_job_create_freezes_schema ---------------------------------------------------------------------
def test_job_create_freezes_schema():
    folder = os.path.join(work, "photos_jobs")
    store = cj.JobStore(folder)
    s_v1 = ct.shop_template()
    job = store.create(s_v1, data={"job_no": "SH-10", "client": "Al-Noor"}, loc=[46.7, 24.7])
    check(job["s"] == "shop@1" and job["title"] == "SH-10" and os.path.isfile(os.path.join(folder, "jobs", job["id"], "job.json")),
          "JobStore.create writes jobs/<jobId>/job.json with title from title question")

    # Mutate template to rev 2 and save; the existing job must keep its frozen shop@1 snapshot
    s_v2 = copy.deepcopy(s_v1)
    s_v2["rev"] = 2
    s_v2["title"] = "Shop survey v2"
    s_v2["nodes"].append({"id": "back_door", "label": "Back door", "req": True, "photos": [1, 2]})
    cd.save_office_schema(s_v2)

    store2 = cj.JobStore(folder)
    reloaded = store2.get(job["id"])
    check(reloaded["s"] == "shop@1" and reloaded["schema"]["rev"] == 1
          and not any(n["id"] == "back_door" for n in reloaded["schema"]["nodes"]),
          "job keeps its frozen schema snapshot after the template is edited")


# ---- test_guide_without_jobs_unchanged & test_guide_carries_cap -----------------------------------------
def test_guide_without_jobs_unchanged_and_carries_cap():
    folder = os.path.join(work, "photos_srv")
    port = free_port()
    TEXTS = {"title": "Biruni", "intro": "x", "start": "Start", "waiting": "w", "shoot": "Take",
             "note": "n", "sent": "s", "queued": "q", "connected": "c", "lost": "l", "denied": "d", "insecure": "i"}
    srv = fs.PhotoServer(folder, os.path.join(work, "cert"), {"en": TEXTS}, "en", [], port, ["127.0.0.1"])
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE

    def get_guide():
        c = http.client.HTTPSConnection("127.0.0.1", port, context=ctx, timeout=15)
        c.request("GET", f"/{srv.token}/guide")
        raw = c.getresponse().read()
        c.close()
        return raw

    try:
        # 1. With no jobs, guide bytes are byte-identical to guide_bytes(v, items) and have no 'cap' key
        raw0 = get_guide()
        check(raw0 == fs.guide_bytes(0, [])[0] and b'"cap"' not in raw0,
              "initial guide with no jobs has no 'cap' key and is byte-identical")
        items = [{"k": "pt", "n": "P1", "c": [46.675, 24.713]}]
        srv.set_guide(items)
        raw1 = get_guide()
        check(raw1 == fs.guide_bytes(1, items)[0] and b'"cap"' not in raw1,
              "set_guide with no jobs stays byte-identical to guide_bytes (no 'cap' key)")

        # 2. When a job is created and published, guide carries 'cap' alongside guide items
        store = cj.JobStore(folder)
        dlg = cj.JobsDialog(store, server=srv)
        job = dlg.create_job(ct.building_template(), data={"job_no": "BLD-1", "client": "Owner A"}, loc=[46.675, 24.713])
        check(job is not None, "JobsDialog.create_job created and published job")
        raw2 = get_guide()
        g2 = json.loads(raw2.decode("utf-8"))
        check("cap" in g2 and g2["cap"]["jobs"][0]["id"] == job["id"]
              and "building@1" in g2["cap"]["schemas"] and g2["items"][0]["n"] == "P1",
              "guide carries 'cap' with open job and schema while preserving guide items")

        # 3. Closing all jobs removes 'cap' so guide is byte-identical to guide_bytes again
        dlg._close_selected()
        raw3 = get_guide()
        check(raw3 == fs.guide_bytes(srv.guide_v, items)[0] and b'"cap"' not in raw3,
              "after closing all jobs, guide has no 'cap' key and is byte-identical again")
    finally:
        srv.stop()


# ---- test_cap_over_limit_refused_with_message -----------------------------------------------------------
def test_cap_over_limit_refused_with_message():
    folder = os.path.join(work, "photos_limit")
    store = cj.JobStore(folder)
    dlg = cj.JobsDialog(store)
    res = dlg.create_job(ct.building_template(), data={"job_no": "BLD-BIG", "client": "C"}, limit=200)
    check(res is None and bool(dlg.last_error) and "KB" in dlg.last_error,
          f"cap over limit is refused with a clear message: {dlg.last_error}")
    check(store.list_jobs(status="open") == [], "refused job is not left open")


test_schema_dialog_roundtrip()
test_node_questions_use_form_dialog()
test_job_create_freezes_schema()
test_guide_without_jobs_unchanged_and_carries_cap()
test_cap_over_limit_refused_with_message()

shutil.rmtree(work, ignore_errors=True)
print("ALL PASS" if not failures else f"{len(failures)} FAILED")
app.exitQgis()
sys.exit(1 if failures else 0)
