r"""No-loss P4: printed emergency runbook (health/runbook.py).

Tests:
- the model holds the org name and exactly 3-of-5 with five names;
- the PDF has one page;
- model_has_no_secrets (no private keys, no bridge keys, no pins, no room codes, no passwords, no share bytes).

Run:  engine\bin\python-qgis.bat tests\test_runbook.py
"""
import json
import os
import re
import shutil
import sys
import tempfile

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QT_QPA_FONTDIR", r"C:\Windows\Fonts")

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, ".."))
sys.path.insert(0, os.path.join(ROOT, "app", "plugins"))
sys.path.insert(0, os.path.join(ROOT, "app", "python"))

from qgis.PyQt.QtWidgets import QApplication  # noqa: E402

app = QApplication.instance() or QApplication(sys.argv[:1])

from biruni.field_photos import bridge, emergency, handover, recipients, server  # noqa: E402
from biruni.field_photos.handover import HandoverKey  # noqa: E402
from biruni.health import Ctx, owners, runbook_model, write_runbook  # noqa: E402

failures = []


def check(cond, msg):
    print(("PASS " if cond else "FAIL ") + msg, flush=True)
    if not cond:
        failures.append(msg)


def count_pdf_pages(path):
    with open(path, "rb") as f:
        data = f.read()
    # Count "/Type /Page" not followed by "s" ("/Type /Pages")
    return len(re.findall(rb"/Type\s*/Page\b", data))


work = tempfile.mkdtemp(prefix="biruni_p4_runbook_")
try:
    cert_dir = os.path.join(work, "cert")
    photos_dir = os.path.join(work, "photos")
    shares_dir = os.path.join(work, "shares")
    os.makedirs(cert_dir, exist_ok=True)
    os.makedirs(photos_dir, exist_ok=True)

    # Create real secrets in cert_dir / photos_dir / shares_dir to ensure none of them leak into the runbook
    hk = HandoverKey(cert_dir)
    bkey = bridge.new_key()
    id_sk, id_pin = bridge.identity(bkey)
    room_code = bkey[:12]

    holders = ["أحمد طارق", "سارة محمود", "خالد العتيبي", "منى الشمري", "ياسر الدوسري"]
    em_pub, share_paths = emergency.create(shares_dir, holders)
    recipients.add(cert_dir, em_pub, "emergency", "Emergency key", office_pub=hk.public)
    rec_doc = recipients.load(cert_dir)
    rec_doc["emergency"] = {"made": "2026-11-24", "holders": holders}
    recipients.save(cert_dir, rec_doc)

    backup_pw = "SuperSecret-Admin-Pass-2026!"
    bak_path = os.path.join(work, "office.biruni-key")

    class FakeSettings:
        def __init__(self):
            from biruni.field_photos.tunnel import protect

            self.d = {
                "biruni/field-photos/bridge-key": protect(bkey),
                "biruni/seal/office": "مكتب البيروني للاستشارات الهندسية",
            }

        def value(self, k, default=""):
            return self.d.get(k, default)

        def setValue(self, k, v):
            self.d[k] = v

    st = FakeSettings()
    handover.backup_v2(cert_dir, bak_path, backup_pw, settings=st)

    store = server.PhotoStore(photos_dir)
    surv_entry = store.issue_surveyor_key("Surveyor Secret User")

    owners.set_org(cert_dir, "مكتب البيروني للاستشارات الهندسية")
    owners.set_owner(cert_dir, "drive", "أحمد طارق", "drive-admin@biruni-gis.sa")
    owners.set_owner(cert_dir, "cloudflare", "خالد العتيبي", "net-admin@biruni-gis.sa")
    owners.set_owner(cert_dir, "domain", "منى الشمري", "domains@biruni-gis.sa")
    owners.set_key_backup(cert_dir, holder="أحمد طارق", where="الخزنة الحديدية — الدور الثاني")

    ctx = Ctx(get_setting=st.value, cert_folder=cert_dir, photos_folder=photos_dir, now=1764000000.0)
    model = runbook_model(ctx)

    # 1. Model holds the org name and 3-of-5 with 5 names
    check(model["org"] == "مكتب البيروني للاستشارات الهندسية",
          f"model holds the org name ({model['org']})")
    check(model["k"] == 3 and len(model["holders"]) == 5 and model["holders"] == holders,
          f"model holds k=3 and the 5 emergency key holders ({model['holders']})")
    check("أحمد طارق" in model["backup_holder"] and "الخزنة الحديدية" in model["backup_where"]
          and "drive-admin@biruni-gis.sa" in model["drive_owner"]
          and "net-admin@biruni-gis.sa" in model["cf_owner"],
          "model holds key backup location, holder, and service owners")

    # 2. Write the PDF and verify it has exactly 1 page
    pdf_path = os.path.join(work, "runbook.pdf")
    write_runbook(pdf_path, model)
    check(os.path.isfile(pdf_path) and os.path.getsize(pdf_path) > 1000,
          f"runbook PDF written ({os.path.getsize(pdf_path)} bytes)")
    pages = count_pdf_pages(pdf_path)
    check(pages == 1, f"the PDF has one page (pages={pages})")

    # 3. model_has_no_secrets
    model_dump = json.dumps(model, ensure_ascii=False)
    share_docs = [json.load(open(p, encoding="utf-8")) for p in share_paths]
    forbidden = [
        ("bridge key", bkey),
        ("room code", room_code),
        ("identity pin", id_pin),
        ("identity private key", id_sk),
        ("handover public key", hk.public),
        ("handover kid", hk.kid),
        ("emergency public key", em_pub),
        ("backup password", backup_pw),
        ("surveyor secret", surv_entry["secret"]),
    ]
    for sdoc in share_docs:
        forbidden.append((f"share {sdoc['x']} bytes", sdoc["share"]))
        forbidden.append((f"share {sdoc['x']} check", sdoc["check"]))

    leaked = [label for label, secret_val in forbidden if secret_val and secret_val in model_dump]
    check(not leaked, f"model_has_no_secrets (leaked={leaked})")

finally:
    shutil.rmtree(work, ignore_errors=True)

print("ALL PASS" if not failures else f"{len(failures)} FAILED", flush=True)
sys.exit(1 if failures else 0)
