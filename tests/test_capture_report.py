r"""Capture schema sealed PDF report and statistics tests (Phase E).

Covers:
  - model_order_follows_tree
  - stats_counts_and_tallies
  - pdf_written_ar_rtl (plus report_page1.png, report_stats.png, report_photos.png, sample_report_ar.pdf)
  - pdf_all_languages
  - missing_table_lists_reasons
  - seal_unchanged
  - heic_placeholder
  - perf_300_photos

Run:  engine\bin\python-qgis.bat tests\test_capture_report.py
"""
import os
import shutil
import sys
import tempfile
import time
import tracemalloc

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QT_QPA_FONTDIR", r"C:\Windows\Fonts")
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "app", "plugins"))
sys.path.insert(0, os.path.join(HERE, "..", "app", "python"))

from qgis.core import QgsApplication, QgsFeature, QgsGeometry, QgsPointXY, QgsProject, QgsVectorLayer  # noqa: E402
from qgis.PyQt.QtCore import QPointF, QRectF, Qt  # noqa: E402
from qgis.PyQt.QtGui import QColor, QFont, QImage, QLinearGradient, QPainter, QPen  # noqa: E402

SETTINGS_DIR = tempfile.mkdtemp()
os.environ["QGIS_CUSTOM_CONFIG_PATH"] = SETTINGS_DIR
app = QgsApplication([], True)
app.initQgis()

from biruni import i18n  # noqa: E402
from biruni.field_photos import capture as cap  # noqa: E402
from biruni.field_photos import capture_jobs as cj  # noqa: E402
from biruni.field_photos import capture_progress as cp  # noqa: E402
from biruni.field_photos import capture_report as cr  # noqa: E402
from biruni.field_photos import capture_templates as ct  # noqa: E402
from biruni.seal import core as seal_core  # noqa: E402
from biruni.survey.report_pdf import font_family  # noqa: E402

SCRATCH_DIR = r"D:\Claude Code - Online\biruni\_scratch\capture"
os.makedirs(SCRATCH_DIR, exist_ok=True)

failures = []


def check(cond, msg):
    print(("PASS " if cond else "FAIL ") + msg, flush=True)
    if not cond:
        failures.append(msg)


work = tempfile.mkdtemp(prefix="biruni_cap_report_")


def make_synthetic_jpeg(path, w=640, h=480, c1="#1f4e79", c2="#2cc5b4", label=""):
    """Create a realistic synthetic JPEG with gradient, architectural frame lines, and text label."""
    img = QImage(w, h, QImage.Format.Format_RGB32)
    painter = QPainter(img)
    grad = QLinearGradient(QPointF(0, 0), QPointF(w, h))
    grad.setColorAt(0.0, QColor(c1))
    grad.setColorAt(1.0, QColor(c2))
    painter.fillRect(0, 0, w, h, grad)
    painter.setPen(QPen(QColor("white"), max(2, w // 180)))
    painter.drawRect(w // 12, h // 12, w - w // 6, h - h // 6)
    painter.drawLine(w // 12, h * 2 // 3, w - w // 12, h * 2 // 3)
    painter.drawLine(w // 3, h // 12, w // 3, h * 2 // 3)
    painter.drawLine(w * 2 // 3, h // 12, w * 2 // 3, h * 2 // 3)
    if label:
        f = QFont(font_family())
        f.setPixelSize(max(18, h // 14))
        f.setBold(True)
        painter.setFont(f)
        painter.setPen(QPen(QColor("white")))
        painter.drawText(QRectF(w // 10, h * 2 // 3 + 10, w * 4 // 5, h // 5),
                         int(Qt.AlignmentFlag.AlignCenter), label)
    painter.end()
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    img.save(path, "JPG", 82)
    return path


def image_non_white_ratio(img_or_path):
    img = QImage(img_or_path) if isinstance(img_or_path, str) else img_or_path
    if img.isNull() or img.width() == 0 or img.height() == 0:
        return 0.0
    non_white = 0
    total = 0
    step_x = max(1, img.width() // 150)
    step_y = max(1, img.height() // 200)
    for y in range(0, img.height(), step_y):
        for x in range(0, img.width(), step_x):
            total += 1
            if (img.pixel(x, y) & 0x00FFFFFF) != 0x00FFFFFF:
                non_white += 1
    return non_white / max(total, 1)


def build_realistic_building_fixture(folder, lang_code="ar", include_unfinished_missing=False):
    """Build a realistic 4-floor building survey job with multiple apartments, rooms, photos, and skipped items."""
    i18n.init(lang_code)
    is_ar = lang_code == "ar"
    tpl = ct.building_template()
    job_data = {
        "job_no": "BLD-2026-408",
        "client": "شركة العمران للتطوير العقاري" if is_ar else "Al-Omran Real Estate Development",
        "deed_no": "410209887312",
        "district": "حي العليا — الرياض" if is_ar else "Al-Olaya District — Riyadh",
    }
    job = cap.new_job(
        tpl,
        data=job_data,
        title="عمارة العليا التجارية السكنية" if is_ar else "Al-Olaya Mixed-Use Building",
        loc=[46.685200, 24.695100],
        to="م. أحمد العتيبي" if is_ar else "Eng. Ahmad Al-Otaibi",
        job_id="JBLD2026",
    )
    job["office"] = "مكتب البيروني للاستشارات الهندسية والمساحية" if is_ar else "Biruni Engineering & Survey Office"

    # 4 floors, multiple apartments and rooms
    inst = {}
    ans = {}
    note = {}
    ph = {}
    skip = {}
    records = []

    floor_names = (
        ["الطابق الأرضي", "الطابق الأول", "الطابق الثاني", "الطابق الثالث"]
        if is_ar else ["Ground floor", "First floor", "Second floor", "Third floor"]
    )
    uses = [i18n.tr("Commercial"), i18n.tr("Residential"), i18n.tr("Residential"), i18n.tr("Office")]
    conds = [i18n.tr("Good"), i18n.tr("Good"), i18n.tr("Fair"), i18n.tr("Poor")]
    room_types = [i18n.tr("Reception"), i18n.tr("Kitchen"), i18n.tr("Bedroom"), i18n.tr("Living room")]

    palette = [("#1f4e79", "#2cc5b4"), ("#2b580c", "#639a67"), ("#5b2c6f", "#a569bd"), ("#7e5109", "#d68910")]
    photo_seq = [0]

    def add_photo(slot_k, pl_txt, chain, note_txt="", preview=False, extra=False, idx_color=0):
        photo_seq[0] += 1
        seq = photo_seq[0]
        pid = f"ph-{seq:04d}"
        c1, c2 = palette[idx_color % len(palette)]
        img_file = os.path.join(folder, f"{pid}.jpg")
        if not os.path.isfile(img_file):
            make_synthetic_jpeg(img_file, 800, 600, c1, c2, f"#{seq} {slot_k}")
        rec = {
            "id": pid,
            "photo": img_file,
            "lat": round(24.695100 + (seq % 7) * 0.00012, 6),
            "lon": round(46.685200 + (seq // 7) * 0.00014, 6),
            "acc": round(2.2 + (seq % 5) * 0.6, 1),
            "taken": f"2026-11-20T0{(8 + seq // 15) % 10}:{(seq * 3) % 60:02d}:15+03:00",
            "surveyor": "م. أحمد العتيبي" if is_ar else "Eng. Ahmad Al-Otaibi",
            "by": "م. أحمد العتيبي" if is_ar else "Eng. Ahmad Al-Otaibi",
            "device_id": "dev_ahmad_01",
            "note": note_txt,
            "preview": preview,
            "cap": {"j": job["id"], "k": "*" if extra else slot_k, "pl": "*" if extra else pl_txt, "ch": chain},
        }
        records.append(rec)
        ph.setdefault("*" if extra else slot_k, []).append(pid)
        return rec

    # Facades: front, right, left, back (back is skipped with a valid reason)
    add_photo(":front", f"{i18n.tr('Facades')} › {i18n.tr('Front facade')}", [],
              note_txt="واجهة حجرية على شارع تجاري عرض 30 م" if is_ar else "Stone facade on 30m commercial street", idx_color=0)
    add_photo(":right", f"{i18n.tr('Facades')} › {i18n.tr('Right facade')}", [],
              note_txt="ممر جانبي عرض 4 م" if is_ar else "4m side passage", idx_color=0)
    add_photo(":left", f"{i18n.tr('Facades')} › {i18n.tr('Left facade')}", [],
              note_txt="ملاصق للمبنى المجاور" if is_ar else "Adjacent to neighbouring building", idx_color=0)
    skip[":back"] = ["ممر خلفي مغلق بأعمال صيانة طارئة" if is_ar else "Back alley closed for emergency maintenance", 1000]

    # Entrance, stairs, roof
    add_photo(":entrance", i18n.tr("Entrance"), [],
              note_txt="المدخل الرئيسي مع لوحة العنوان الوطني" if is_ar else "Main entrance with national address plate", idx_color=1)
    add_photo(":stairs", i18n.tr("Stairs"), [],
              note_txt="بيت الدرج والمصعد" if is_ar else "Stairwell and elevator", idx_color=1)
    add_photo(":roof", i18n.tr("Roof"), [],
              note_txt="سطح المبنى وخزانات المياه" if is_ar else "Building roof and water tanks", idx_color=1)

    # 4 Floors
    for f_idx in range(1, 5):
        fl_iid = f"fl000{f_idx}"
        inst[fl_iid] = {"n": "floors", "p": "", "o": f_idx, "t": 100 + f_idx, "del": 0}
        fl_title = floor_names[f_idx - 1]
        ans[f"{fl_iid}:floors"] = {"floor_name": [fl_title, 100 + f_idx]}

        # 2 apartments per floor (8 apartments total)
        for a_idx in range(1, 3):
            ap_iid = f"ap0{f_idx}0{a_idx}"
            inst[ap_iid] = {"n": "apts", "p": fl_iid, "o": a_idx, "t": 200 + f_idx * 10 + a_idx, "del": 0}
            apt_num = f"{f_idx}0{a_idx}"
            use_val = uses[(f_idx + a_idx) % len(uses)]
            cond_val = conds[(f_idx * 2 + a_idx) % len(conds)]
            rcount = 3 + (a_idx % 2)
            ans[f"{ap_iid}:apts"] = {
                "apt_no": [apt_num, 200],
                "use": [use_val, 200],
                "condition": [cond_val, 200],
                "rooms_count": [rcount, 200],
            }

            # Skip apartment 302 with a valid reason
            if f_idx == 3 and a_idx == 2:
                skip[f"{ap_iid}:apts"] = [
                    "الشقة مغلقة لوجود المستأجر خارج المدينة" if is_ar else "Apartment locked — tenant out of town",
                    300,
                ]
                continue

            # If testing unfinished missing items, leave apartment 402 with an incomplete room and no skip reason
            for r_idx in range(1, 3):
                rm_iid = f"rm{f_idx}{a_idx}0{r_idx}"
                inst[rm_iid] = {"n": "rooms", "p": ap_iid, "o": r_idx, "t": 300 + r_idx, "del": 0}
                rtype = room_types[(f_idx + a_idx + r_idx) % len(room_types)]
                rm_k = f"{rm_iid}:rooms"
                if include_unfinished_missing and f_idx == 4 and a_idx == 2 and r_idx == 2:
                    # Leave 1 photo instead of min 2 and no room_type answer, without skip reason
                    ch = [["floors", fl_iid, f_idx], ["apts", ap_iid, a_idx], ["rooms", rm_iid, r_idx]]
                    pl = f"{fl_title} › {apt_num} › {i18n.tr('Room')} {r_idx}"
                    add_photo(rm_k, pl, ch, note_txt="تحتاج صورة ثانية" if is_ar else "Needs second photo", idx_color=f_idx)
                    continue
                ans[rm_k] = {"room_type": [rtype, 300]}
                note[rm_k] = [
                    f"تهوية وإضاءة طبيعية — {rtype}" if is_ar else f"Natural light and ventilation — {rtype}",
                    300,
                ]
                ch = [["floors", fl_iid, f_idx], ["apts", ap_iid, a_idx], ["rooms", rm_iid, r_idx]]
                pl = f"{fl_title} › {apt_num} › {rtype}"
                is_prev = (f_idx == 4 and a_idx == 1 and r_idx == 2)
                add_photo(rm_k, pl, ch, preview=is_prev, idx_color=f_idx)
                add_photo(rm_k, pl, ch, preview=False, idx_color=f_idx)

    # Services: electricity & water meters
    add_photo(":elec_meter", f"{i18n.tr('Services')} › {i18n.tr('Electricity meter')}", [],
              note_txt="لوحة عدادات الكهرباء الرقمية" if is_ar else "Digital electricity meters panel", idx_color=2)
    add_photo(":water_meter", f"{i18n.tr('Services')} › {i18n.tr('Water meter')}", [],
              note_txt="عداد المياه الرئيسي" if is_ar else "Main water meter", idx_color=3)

    # Appendix extra photo (k == "*")
    add_photo("*", "*", [], note_txt="صورة عامة للمواقف الخارجية" if is_ar else "General view of outdoor parking",
              extra=True, idx_color=0)

    state = {
        "v": 1,
        "job": job["id"],
        "dev": "dev_ahmad_01",
        "by": "م. أحمد العتيبي" if is_ar else "Eng. Ahmad Al-Otaibi",
        "rev": 25,
        "t": 1795000000000,
        "s": job["s"],
        "inst": inst,
        "ans": ans,
        "note": note,
        "ph": ph,
        "skip": skip,
        "closed": None if include_unfinished_missing else 1795000050000,
    }
    merged = cap.merge_states([state], job["schema"])
    return job, merged, records


# ---- 1. model_order_follows_tree -------------------------------------------------------------------------
def test_model_order_follows_tree():
    folder = os.path.join(work, "order_test")
    os.makedirs(folder, exist_ok=True)
    job, merged, records = build_realistic_building_fixture(folder, "ar", include_unfinished_missing=False)
    # Shuffle records in reverse order to prove report_model orders by tree traversal, not arrival order
    shuffled_recs = list(reversed(records))
    model = cr.report_model(job, merged, shuffled_recs, folder=folder)

    t = cap.tree(job["schema"], merged, shuffled_recs)
    expected_slot_keys = []

    def _walk(node):
        if node["kind"] == "repeat":
            if node.get("own_skip"):
                expected_slot_keys.append(node["k"])
            for sub in node.get("instances") or ():
                _walk(sub)
        else:
            expected_slot_keys.append(node["k"])
            for ch in node.get("children") or ():
                _walk(ch)

    for r in t["roots"]:
        _walk(r)

    got_slot_keys = [it["k"] for it in model["items"]]
    check(got_slot_keys == expected_slot_keys,
          f"model_order_follows_tree: items follow exact schema tree order ({len(got_slot_keys)} items)")

    # Check section order matches schema root nodes
    expected_sec_ids = [n["id"] for n in job["schema"]["nodes"]]
    got_sec_ids = [s["id"] for s in model["sections"]]
    check(got_sec_ids == expected_sec_ids,
          f"model_order_follows_tree: sections match root nodes order {got_sec_ids}")

    # Check photo order follows tree items order, and extra photos (k == '*') go to extra_photos appendix
    item_photo_keys = [p["k"] for p in model["photos"]]
    check(item_photo_keys[0] == ":front" and item_photo_keys[1] == ":right" and item_photo_keys[-1] == ":water_meter",
          f"model_order_follows_tree: photos ordered by tree traversal (first={item_photo_keys[0]}, last={item_photo_keys[-1]})")
    check(len(model["extra_photos"]) == 1 and model["extra_photos"][0]["k"] == "*",
          "model_order_follows_tree: extra photos (k == '*') placed in appendix")

    # Check preview photo is flagged and captioned as preview, and coordinates use LRI/PDI isolation
    previews = [p for p in model["photos"] if p["preview"]]
    check(len(previews) == 1 and i18n.tr("Preview") in previews[0]["caption"],
          f"model_order_follows_tree: preview photo captioned as preview ({previews[0]['caption_lines'][0]})")
    check(all(cr.LRI in p["coords_text"] and cr.PDI in p["coords_text"] for p in model["photos"]),
          "model_order_follows_tree: photo coordinates wrapped in LRI/PDI isolation")


# ---- 2. stats_counts_and_tallies -------------------------------------------------------------------------
def test_stats_counts_and_tallies():
    folder = os.path.join(work, "order_test")
    job, merged, records = build_realistic_building_fixture(folder, "en_US", include_unfinished_missing=False)
    model = cr.report_model(job, merged, records, folder=folder)
    st = model["stats"]

    # 4 floors, 8 apartments, 14 rooms (7 non-skipped apartments * 2 rooms)
    check(st["instances"] == {"floors": 4, "apts": 8, "rooms": 14},
          f"stats_counts_and_tallies: repeat instance counts = {st['instances']}")

    # Choice tallies
    check(sum(st["choices"].get("apts.condition", {}).values()) == 8
          and sum(st["choices"].get("apts.use", {}).values()) == 8
          and sum(st["choices"].get("rooms.room_type", {}).values()) == 14,
          f"stats_counts_and_tallies: choice tallies match answered instances ({st['choices']})")

    # Integer sums and averages: 8 apartments, 4 with rooms_count=4 and 4 with rooms_count=3 -> sum=28, avg=3.5
    check(st["sums"].get("apts.rooms_count") == 28
          and st["averages"].get("apts.rooms_count") == 3.5
          and st["counts"].get("apts.rooms_count") == 8,
          f"stats_counts_and_tallies: integer sum=28 and avg=3.5 ({st['number_rows']})")

    # Photos per section and total: Facades 3, Entrance 1, Stairs 1, Roof 1, Floor 28 (14 rooms * 2), Services 2, Extra 1 -> Total 37
    check(st["section_photos"].get("Facades") == 3
          and st["section_photos"].get("Floor") == 28
          and st["section_photos"].get("Services") == 2
          and st["extra_photos"] == 1
          and st["total_photos"] == 37,
          f"stats_counts_and_tallies: photos per section={st['section_photos']}, extra={st['extra_photos']}, total={st['total_photos']}")


# ---- 3. pdf_written_ar_rtl & sample_report_ar.pdf --------------------------------------------------------
def test_pdf_written_ar_rtl():
    folder = os.path.join(work, "ar_pdf")
    os.makedirs(folder, exist_ok=True)
    seal_core.set_office("مكتب البيروني للاستشارات الهندسية والمساحية")
    job, merged, records = build_realistic_building_fixture(folder, "ar", include_unfinished_missing=False)
    model = cr.report_model(job, merged, records, folder=folder)

    # Create a project layer so render_location_map renders with QgsMapRendererCustomPainterJob
    lyr = QgsVectorLayer("Point?crs=EPSG:4326&field=name:string", "survey_points", "memory")
    feats = []
    for p in model["points"]:
        f = QgsFeature(lyr.fields())
        f.setGeometry(QgsGeometry.fromPointXY(QgsPointXY(p["lon"], p["lat"])))
        f.setAttributes([p["id"]])
        feats.append(f)
    lyr.dataProvider().addFeatures(feats)
    QgsProject.instance().addMapLayer(lyr)
    try:
        map_img = cr.render_location_map(model, project=QgsProject.instance())
        check(map_img is not None and not map_img.isNull(),
              "pdf_written_ar_rtl: render_location_map rendered project layer with QgsMapRendererCustomPainterJob")
    finally:
        QgsProject.instance().removeMapLayer(lyr.id())

    planned = cr.plan_report_pages(model, map_image=map_img)
    # Expected pages:
    # 1 Cover + 1 Map + 1 Stats + Photo pages (Facades:1, Entrance:1, Stairs:1, Roof:1, Floor 28 photos -> 5 pages, Services:1) + 1 Appendix + 1 Signatures = 15 pages
    expected_pages = len(planned)
    sample_pdf_path = os.path.join(SCRATCH_DIR, "sample_report_ar.pdf")
    res = cr.write_report(sample_pdf_path, model, map_image=map_img, lang="ar")

    check(os.path.isfile(sample_pdf_path) and os.path.getsize(sample_pdf_path) > 20_000,
          f"pdf_written_ar_rtl: sample_report_ar.pdf written ({os.path.getsize(sample_pdf_path):,} bytes)")
    check(res["pages"] == expected_pages and expected_pages == 15,
          f"pdf_written_ar_rtl: page count matches expected ({res['pages']} pages)")

    # Render page 1 (cover), stats page, and first photo page through the same draw function to PNGs in SCRATCH_DIR
    p1_png = os.path.join(SCRATCH_DIR, "report_page1.png")
    stats_idx = next(i for i, p in enumerate(planned) if p["kind"] == "stats")
    photos_idx = next(i for i, p in enumerate(planned) if p["kind"] == "photos" and len(p["photos"]) == 6)
    stats_png = os.path.join(SCRATCH_DIR, "report_stats.png")
    photos_png = os.path.join(SCRATCH_DIR, "report_photos.png")

    cr.render_report_page_png(p1_png, model, page_index=0, map_image=map_img, lang="ar")
    cr.render_report_page_png(stats_png, model, page_index=stats_idx, map_image=map_img, lang="ar")
    cr.render_report_page_png(photos_png, model, page_index=photos_idx, map_image=map_img, lang="ar")

    for label, png_path in (("report_page1.png", p1_png), ("report_stats.png", stats_png), ("report_photos.png", photos_png)):
        ratio = image_non_white_ratio(png_path)
        check(os.path.isfile(png_path) and ratio > 0.03,
              f"pdf_written_ar_rtl: {label} saved and non-blank (ink ratio={ratio:.1%})")


# ---- 4. pdf_all_languages --------------------------------------------------------------------------------
def test_pdf_all_languages():
    folder = os.path.join(work, "langs_pdf")
    os.makedirs(folder, exist_ok=True)
    for lng in i18n.LANGUAGE_INFO:
        code = lng.code
        i18n.init(code)
        tpl = ct.shop_template()
        job = cap.new_job(tpl, data={"job_no": f"SH-{code}", "client": "Client A"}, job_id="JSHOP100")
        p1 = make_synthetic_jpeg(os.path.join(folder, f"sh1_{code}.jpg"), 400, 300, label=code)
        recs = [{
            "id": f"sh1-{code}", "photo": p1, "lat": 24.71, "lon": 46.68, "acc": 3.0,
            "taken": "2026-11-20T09:00:00+03:00", "surveyor": "Sami",
            "cap": {"j": job["id"], "k": ":signboard", "pl": "Signboard", "ch": []},
        }]
        st = {
            "v": 1, "job": job["id"], "dev": "dev01", "by": "Sami", "rev": 1, "t": 1000, "s": job["s"],
            "inst": {},
            "ans": {
                ":signboard": {"shop_name": ["Al-Waha", 1000], "activity": ["Cafe", 1000], "frontage_m": [6.5, 1000]},
                ":hall": {"area_m2": [120.0, 1000], "condition": [tpl["nodes"][1]["q"][1]["choices"][0], 1000]},
                ":safety": {"fire_ext": [True, 1000], "license_shown": [False, 1000]},
            },
            "note": {}, "ph": {":signboard": [f"sh1-{code}"]}, "skip": {":hall": ["Under renovation", 1000], ":safety": ["Locked", 1000]},
            "closed": 1000,
        }
        merged = cap.merge_states([st], job["schema"])
        model = cr.report_model(job, merged, recs, folder=folder)
        pdf_path = os.path.join(folder, f"report_{code}.pdf")
        res = cr.write_report(pdf_path, model, map_image=None, lang=code)
        check(os.path.isfile(pdf_path) and os.path.getsize(pdf_path) > 5000 and res["pages"] >= 4,
              f"pdf_all_languages: {code} ({'RTL' if lng.rtl else 'LTR'}) PDF written ({res['pages']} pages)")
    i18n.init("en_US")


# ---- 5. missing_table_lists_reasons ----------------------------------------------------------------------
def test_missing_table_lists_reasons():
    folder = os.path.join(work, "missing_test")
    os.makedirs(folder, exist_ok=True)
    job, merged, records = build_realistic_building_fixture(folder, "ar", include_unfinished_missing=True)
    model = cr.report_model(job, merged, records, folder=folder)
    ms = model["stats"]["missing_and_skipped"]

    skipped_rows = [r for r in ms if r["status"] == "skipped"]
    missing_rows = [r for r in ms if r["status"] == "missing"]
    check(len(skipped_rows) == 2
          and any(r["k"] == ":back" and "صيانة" in r["reason"] for r in skipped_rows)
          and any(r["k"] == "ap0302:apts" and "المستأجر" in r["reason"] for r in skipped_rows),
          f"missing_table_lists_reasons: skipped items list their reasons ({skipped_rows})")
    check(len(missing_rows) == 1
          and missing_rows[0]["k"] == "rm4202:rooms"
          and "1" in missing_rows[0]["reason"],
          f"missing_table_lists_reasons: unfinished item without reason is listed in missing table ({missing_rows})")


# ---- 6. seal_unchanged -----------------------------------------------------------------------------------
def test_seal_unchanged():
    sample_pdf_path = os.path.join(SCRATCH_DIR, "sample_report_ar.pdf")
    for ext in (seal_core.SEAL_EXT, seal_core.QR_EXT, seal_core.CERT_EXT):
        check(os.path.isfile(sample_pdf_path + ext), f"seal_unchanged: {ext} exists next to sample_report_ar.pdf")
    status, details = seal_core.check_file(sample_pdf_path)
    check(status == seal_core.UNCHANGED,
          f"seal_unchanged: seal.core.check_file reports UNCHANGED ({status}, sha256={details.get('file_sha256', '')[:16]}…)")

    # Also verify JobsDialog and ProgressDialog 'Report (PDF)…' button integration
    jstore = cj.JobStore(os.path.join(work, "dlg_report"))
    i18n.init("en_US")
    j = jstore.create(ct.shop_template(), data={"job_no": "SH-DLG", "client": "Noor"}, job_id="JDLG0001")
    jdlg = cj.JobsDialog(jstore)
    out_pdf1 = os.path.join(work, "dlg_report", "jobs", j["id"], "from_jobs_dlg.pdf")
    res1 = jdlg._export_report(path=out_pdf1, open_after=False)
    check(res1 is not None and os.path.isfile(out_pdf1) and seal_core.check_file(out_pdf1)[0] == seal_core.UNCHANGED,
          "seal_unchanged: JobsDialog 'Report (PDF)…' writes and seals the PDF")

    pdlg = cp.ProgressDialog(jstore, j["id"])
    out_pdf2 = os.path.join(work, "dlg_report", "jobs", j["id"], "from_prog_dlg.pdf")
    res2 = pdlg.export_report(path=out_pdf2, open_after=False)
    check(res2 is not None and os.path.isfile(out_pdf2) and seal_core.check_file(out_pdf2)[0] == seal_core.UNCHANGED,
          "seal_unchanged: ProgressDialog 'Report (PDF)…' writes and seals the PDF")


# ---- 7. heic_placeholder ---------------------------------------------------------------------------------
def test_heic_placeholder():
    folder = os.path.join(work, "heic_test")
    os.makedirs(folder, exist_ok=True)
    i18n.init("en_US")
    tpl = ct.shop_template()
    job = cap.new_job(tpl, data={"job_no": "SH-HEIC", "client": "H"}, job_id="JHEIC001")

    heic_path = os.path.join(folder, "IMG_4021.heic")
    with open(heic_path, "wb") as f:
        f.write(b"\x00\x00\x00\x18ftypheic\x00\x00\x00\x00mif1heic" + os.urandom(256))

    img, err = cr.decode_report_image(heic_path)
    check(img is None and bool(err), f"heic_placeholder: undecodable HEIC returns None ({err})")

    recs = [{
        "id": "heic-0001", "photo": heic_path, "lat": 24.71, "lon": 46.68, "acc": 4.0,
        "taken": "2026-11-20T10:00:00+03:00", "surveyor": "Ali",
        "cap": {"j": job["id"], "k": ":signboard", "pl": "Facade & signboard", "ch": []},
    }]
    merged = cap.merge_states([], job["schema"])
    model = cr.report_model(job, merged, recs, folder=folder)
    pdf_path = os.path.join(folder, "heic_report.pdf")
    png_path = os.path.join(folder, "heic_photo_page.png")

    res = cr.write_report(pdf_path, model, lang="en_US")
    planned = cr.plan_report_pages(model)
    p_idx = next(i for i, p in enumerate(planned) if p["kind"] == "photos")
    cr.render_report_page_png(png_path, model, page_index=p_idx, lang="en_US")
    check(os.path.isfile(pdf_path) and res["pages"] == len(planned) and image_non_white_ratio(png_path) > 0.03,
          "heic_placeholder: report writes framed placeholder for undecodable HEIC without crashing")


# ---- 8. perf_300_photos ----------------------------------------------------------------------------------
def test_perf_300_photos():
    folder = os.path.join(work, "perf_300")
    os.makedirs(folder, exist_ok=True)
    i18n.init("ar")
    tpl = ct.building_template()
    job = cap.new_job(tpl, data={"job_no": "BLD-300-PERF", "client": "شركة المقاولات"}, job_id="JPERF300")

    # Generate 10 distinct 4000x3000 gradient JPEGs and write 300 distinct photo files (copying the 10 templates)
    # so all 300 records read real 4000x3000 JPEG files from disk one by one via QImageReader.
    t_gen0 = time.perf_counter()
    seeds = []
    for s_idx in range(6):
        sp = os.path.join(folder, f"_seed_{s_idx}.jpg")
        img = QImage(4000, 3000, QImage.Format.Format_RGB32)
        p = QPainter(img)
        g = QLinearGradient(QPointF(0, 0), QPointF(4000, 3000))
        g.setColorAt(0.0, QColor.fromHsv((s_idx * 55) % 360, 180, 160))
        g.setColorAt(1.0, QColor.fromHsv(((s_idx + 2) * 55) % 360, 140, 220))
        p.fillRect(0, 0, 4000, 3000, g)
        p.setPen(QPen(QColor("white"), 16))
        p.drawRect(200, 200, 3600, 2600)
        p.end()
        img.save(sp, "JPG", 75)
        del img
        seeds.append(sp)

    inst = {}
    ans = {}
    ph = {}
    records = []

    # 10 floors * 3 apartments * 5 rooms = 150 rooms * 2 photos = 300 photos
    seq = 0
    for f_idx in range(1, 11):
        fl_iid = f"pfl{f_idx:03d}"
        inst[fl_iid] = {"n": "floors", "p": "", "o": f_idx, "t": 100, "del": 0}
        ans[f"{fl_iid}:floors"] = {"floor_name": [f"الطابق {f_idx}", 100]}
        for a_idx in range(1, 4):
            ap_iid = f"pap{f_idx:02d}{a_idx:02d}"
            inst[ap_iid] = {"n": "apts", "p": fl_iid, "o": a_idx, "t": 100, "del": 0}
            ans[f"{ap_iid}:apts"] = {
                "apt_no": [f"{f_idx}0{a_idx}", 100],
                "use": [i18n.tr("Residential"), 100],
                "condition": [i18n.tr("Good"), 100],
                "rooms_count": [5, 100],
            }
            for r_idx in range(1, 6):
                rm_iid = f"prm{f_idx:02d}{a_idx}{r_idx}"
                inst[rm_iid] = {"n": "rooms", "p": ap_iid, "o": r_idx, "t": 100, "del": 0}
                rm_k = f"{rm_iid}:rooms"
                ans[rm_k] = {"room_type": [i18n.tr("Bedroom"), 100]}
                ch = [["floors", fl_iid, f_idx], ["apts", ap_iid, a_idx], ["rooms", rm_iid, r_idx]]
                pl = f"الطابق {f_idx} › {f_idx}0{a_idx} › {i18n.tr('Bedroom')}"
                for p_no in (1, 2):
                    seq += 1
                    pid = f"perf-{seq:04d}"
                    jpg_path = os.path.join(folder, f"{pid}.jpg")
                    shutil.copyfile(seeds[seq % len(seeds)], jpg_path)
                    ph.setdefault(rm_k, []).append(pid)
                    records.append({
                        "id": pid,
                        "photo": jpg_path,
                        "lat": 24.7000 + seq * 0.00002,
                        "lon": 46.6800 + seq * 0.00002,
                        "acc": 2.5,
                        "taken": "2026-11-20T11:00:00+03:00",
                        "surveyor": "م. أحمد",
                        "cap": {"j": job["id"], "k": rm_k, "pl": pl, "ch": ch},
                    })
    t_gen = time.perf_counter() - t_gen0
    print(f"     generated 300 synthetic 4000x3000 JPEGs in {t_gen:.2f} s", flush=True)

    state = {
        "v": 1, "job": job["id"], "dev": "dev_perf", "by": "م. أحمد", "rev": 1, "t": 1000, "s": job["s"],
        "inst": inst, "ans": ans, "note": {}, "ph": ph, "skip": {}, "closed": 1000,
    }
    merged = cap.merge_states([state], job["schema"])

    tracemalloc.start()
    t0 = time.perf_counter()
    model = cr.report_model(job, merged, records, folder=folder)
    pdf_path = os.path.join(folder, "perf_300_report.pdf")
    res = cr.write_report(pdf_path, model, lang="ar")
    elapsed = time.perf_counter() - t0
    _cur_mem, peak_mem = tracemalloc.get_traced_memory()
    tracemalloc.stop()

    pdf_size_mb = os.path.getsize(pdf_path) / (1024 * 1024)
    peak_mb = peak_mem / (1024 * 1024)
    print(f"     perf_300_photos: time={elapsed:.2f} s, pages={res['pages']}, pdf_size={pdf_size_mb:.2f} MB, python_peak_mem={peak_mb:.2f} MB", flush=True)

    check(elapsed < 90.0, f"perf_300_photos: writing 300 photos finished in {elapsed:.2f} s (< 90 s)")
    check(peak_mb < 60.0, f"perf_300_photos: Python peak memory stayed reasonable at {peak_mb:.2f} MB (< 60 MB)")
    check(seal_core.check_file(pdf_path)[0] == seal_core.UNCHANGED, "perf_300_photos: 300-photo PDF is sealed and valid")
    i18n.init("en_US")


test_model_order_follows_tree()
test_stats_counts_and_tallies()
test_pdf_written_ar_rtl()
test_pdf_all_languages()
test_missing_table_lists_reasons()
test_seal_unchanged()
test_heic_placeholder()
test_perf_300_photos()

QgsProject.instance().removeAllMapLayers()
shutil.rmtree(work, ignore_errors=True)
print("ALL PASS" if not failures else f"{len(failures)} FAILED", flush=True)
app.exitQgis()
sys.exit(1 if failures else 0)
