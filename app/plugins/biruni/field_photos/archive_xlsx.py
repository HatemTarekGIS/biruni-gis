"""Excel workbook writer for job archives (`<job no>.xlsx`) and the global index (`فهرس الأرشيف.xlsx`).

Uses bundled `openpyxl` (same styling pattern as `activity_report.py:99`), with RTL sheets when the UI
language is Arabic/RTL.
"""
import os

from ..i18n import is_rtl, tr

ACCENT = "0E7C74"
ACCENT_SOFT = "DDF0EE"


def _extract_job_fields(job, model):
    data = (job.get("data") if isinstance(job, dict) and isinstance(job.get("data"), dict) else {}) or {}
    q_by_name = {q["name"]: q["value"] for q in (model.get("job_questions") or ()) if isinstance(q, dict) and q.get("name")}

    def _pick(*names):
        for n in names:
            v = data.get(n)
            if v not in (None, "", []):
                return str(v)
            v2 = q_by_name.get(n)
            if v2 not in (None, "", "—"):
                return str(v2)
        return ""

    job_id = str(model.get("job_id") or job.get("id") or "").strip()
    job_no = _pick("job_no", "number", "code") or str(model.get("job_no") or job.get("title") or job_id)
    title = str(job.get("title") or model.get("title") or job_no or job_id).strip()
    client = _pick("client", "owner") or str(model.get("client") or "")
    deed_no = _pick("deed_no", "deed", "instrument_no")
    district = _pick("district", "neighborhood", "area")

    surveyors_list = list(model.get("surveyors") or ())
    if not surveyors_list and isinstance(job, dict) and job.get("to"):
        surveyors_list = [str(job["to"])]
    sep = "، " if is_rtl() else ", "
    surveyors = sep.join(s for s in surveyors_list if s)

    first_dt = str(model.get("first_photo") or "").strip()
    last_dt = str(model.get("last_photo") or "").strip()
    if first_dt and last_dt and first_dt != last_dt:
        dates = f"{first_dt} → {last_dt}"
    else:
        dates = first_dt or last_dt or ""

    percent = int(model.get("percent") or 0)
    raw_st = str(model.get("status") or (job.get("st") if isinstance(job, dict) else "") or "open")
    status_label = tr("Closed") if raw_st == "closed" else tr("In progress")
    return {
        "job_id": job_id,
        "job_no": job_no,
        "title": title,
        "client": client,
        "deed_no": deed_no,
        "district": district,
        "surveyors": surveyors,
        "first_date": first_dt[:10] if first_dt else "",
        "last_date": (last_dt[:10] if last_dt else (first_dt[:10] if first_dt else "")),
        "dates": dates,
        "percent": percent,
        "status": raw_st,
        "status_label": status_label,
    }


def _atomic_save_workbook(wb, path):
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    part = f"{path}.{os.getpid()}.tmp.xlsx"
    wb.save(part)
    os.replace(part, path)


def write_job_xlsx(path, job, model, archived_photos, rtl=None):
    """Write `<job no>.xlsx` with 4 sheets (job data, items, photos, stats) and RTL when language is Arabic."""
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
    from openpyxl.utils import get_column_letter

    if rtl is None:
        rtl = is_rtl()

    wb = Workbook()
    head_font = Font(bold=True, color="FFFFFF")
    head_fill = PatternFill("solid", fgColor=ACCENT)
    soft_fill = PatternFill("solid", fgColor=ACCENT_SOFT)
    link_font = Font(color="0563C1", underline="single")
    thin = Side(style="thin", color="DDE2E7")
    box = Border(top=thin, bottom=thin, left=thin, right=thin)

    def make_sheet(title, headers=None, widths=None, first=False):
        ws = wb.active if first else wb.create_sheet()
        ws.title = title[:31]
        ws.sheet_view.rightToLeft = bool(rtl)
        if headers:
            ws.append(list(headers))
            for c in ws[1]:
                c.font = head_font
                c.fill = head_fill
                c.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
            ws.freeze_panes = "A2"
        for i, w in enumerate(widths or (), start=1):
            ws.column_dimensions[get_column_letter(i)].width = w
        return ws

    info = _extract_job_fields(job, model)

    # ---- Sheet 1: job data ------------------------------------------------
    ws_job = make_sheet(
        tr("Job data"),
        headers=[tr("Field"), tr("Value")],
        widths=[32, 48],
        first=True,
    )
    known_q_names = {"job_no", "client", "deed_no", "district"}
    kv_rows = [
        (tr("Job ID"), info["job_id"]),
        (tr("Job number"), info["job_no"]),
        (tr("Job title:"), info["title"]),
        (tr("Client / owner"), info["client"] or "—"),
        (tr("Deed number"), info["deed_no"] or "—"),
        (tr("District"), info["district"] or "—"),
        (tr("Surveyor"), info["surveyors"] or "—"),
        (tr("First photo"), model.get("first_photo") or "—"),
        (tr("Last photo"), model.get("last_photo") or "—"),
        (tr("Dates"), info["dates"] or "—"),
        (tr("Completion"), f"{info['percent']}% ({model.get('done_count', 0)}/{model.get('total_count', 0)})"),
        (tr("Status"), info["status_label"]),
        (tr("Template:"), f"{model.get('template_title') or ''} ({model.get('schema_ref') or ''})"),
    ]
    for q in model.get("job_questions") or ():
        if q.get("name") not in known_q_names:
            kv_rows.append((q.get("label") or q.get("name"), str(q.get("value") or "—")))

    for idx, (k, v) in enumerate(kv_rows, start=2):
        c1 = ws_job.cell(row=idx, column=1, value=k)
        c2 = ws_job.cell(row=idx, column=2, value=v)
        c1.font = Font(bold=True)
        c1.border = box
        c2.border = box
        if idx % 2 == 0:
            c1.fill = soft_fill
            c2.fill = soft_fill

    # ---- Sheet 2: items ---------------------------------------------------
    # Collect all distinct question columns in schema order across items
    items = model.get("items") or []
    q_cols = []
    seen_q = set()
    schema = model.get("schema") or {}

    def _collect_schema_q(nodes):
        for n in nodes or ():
            for q in n.get("q") or ():
                qn = q["name"]
                if qn not in seen_q:
                    seen_q.add(qn)
                    q_cols.append((qn, q.get("label") or qn))
            if n.get("nodes"):
                _collect_schema_q(n["nodes"])

    _collect_schema_q(schema.get("nodes") or ())
    for it in items:
        for lbl, _val in it.get("answers") or ():
            if not any(lbl == qlbl or lbl == qn for qn, qlbl in q_cols):
                q_cols.append((lbl, lbl))

    item_headers = [
        tr("Path"),
        tr("Required"),
        tr("State"),
        tr("Photos"),
        tr("Note"),
    ] + [qlbl for _qn, qlbl in q_cols]
    item_widths = [42, 14, 30, 12, 28] + [20] * len(q_cols)
    ws_items = make_sheet(tr("Items"), headers=item_headers, widths=item_widths)

    for it in items:
        req_txt = tr("Yes") if it.get("req", True) else tr("No")
        if it.get("skipped"):
            reason = it.get("skip") or it.get("own_skip") or ""
            st_txt = f"{tr('Skipped')}: {reason}" if reason else tr("Skipped")
        elif it.get("own_done") or it.get("done"):
            st_txt = tr("Done")
        else:
            st_txt = tr("Missing")

        ans_by_label = dict(it.get("answers") or ())
        ans_dict = it.get("answers_dict") or {}
        q_values = []
        for qn, qlbl in q_cols:
            if qlbl in ans_by_label:
                q_values.append(ans_by_label[qlbl])
            elif qn in ans_dict:
                val = ans_dict[qn]
                if val is True:
                    q_values.append(tr("Yes"))
                elif val is False:
                    q_values.append(tr("No"))
                else:
                    q_values.append(str(val))
            else:
                q_values.append("")

        row = [
            it.get("path_label") or it.get("label") or "",
            req_txt,
            st_txt,
            int(it.get("photo_count") or 0),
            it.get("note") or "",
        ] + q_values
        ws_items.append(row)

    if items:
        ws_items.auto_filter.ref = f"A1:{get_column_letter(len(item_headers))}{len(items) + 1}"

    # ---- Sheet 3: photos --------------------------------------------------
    photo_headers = [
        tr("Archive file"),
        tr("Path"),
        tr("Latitude"),
        tr("Longitude"),
        tr("Accuracy (m)"),
        tr("Time"),
        tr("Note"),
        tr("Answers"),
        tr("Original SHA-256"),
    ]
    photo_widths = [34, 38, 15, 15, 14, 22, 28, 32, 66]
    ws_photos = make_sheet(tr("Photos"), headers=photo_headers, widths=photo_widths)

    for r_idx, ph in enumerate(archived_photos or (), start=2):
        rel_link = (ph.get("archive_rel_path") or ph.get("archive_name") or "").replace("\\", "/")
        row = [
            ph.get("archive_name") or "",
            ph.get("path_label") or "",
            ph.get("lat") if ph.get("lat") is not None else "",
            ph.get("lon") if ph.get("lon") is not None else "",
            ph.get("acc") if ph.get("acc") is not None else "",
            ph.get("time") or "",
            ph.get("note") or "",
            ph.get("answers") or "",
            ph.get("orig_sha") or ph.get("sha") or "",
        ]
        ws_photos.append(row)
        cell = ws_photos.cell(row=r_idx, column=1)
        if rel_link:
            cell.hyperlink = rel_link
            cell.font = link_font

    if archived_photos:
        ws_photos.auto_filter.ref = f"A1:{get_column_letter(len(photo_headers))}{len(archived_photos) + 1}"

    # ---- Sheet 4: stats ---------------------------------------------------
    ws_stats = make_sheet(
        tr("Statistics"),
        headers=[tr("Category"), tr("Item"), tr("Value"), tr("Details")],
        widths=[28, 38, 22, 42],
    )
    st = model.get("stats") or {}

    for r in st.get("repeat_rows") or ():
        ws_stats.append([tr("Instances per repeating item"), r["label"], r["count"], r["id"]])

    for r in st.get("choice_rows") or ():
        ws_stats.append([tr("Choice and yes/no tallies"), r["label"], r["summary"], r["key"]])

    for r in st.get("number_rows") or ():
        ws_stats.append([
            tr("Sums and averages"),
            r["label"],
            r["sum"],
            f"{tr('Count')}: {r['count']} · {tr('Average')}: {r['avg']}",
        ])

    for r in st.get("section_photo_rows") or ():
        ws_stats.append([tr("Photos per section"), r["label"], r["count"], ""])
    if st.get("extra_photos"):
        ws_stats.append([tr("Photos per section"), tr("Extra photo for this job"), st["extra_photos"], "*"])
    ws_stats.append([tr("Photos per section"), tr("Total"), st.get("total_photos", 0), ""])

    for r in st.get("missing_and_skipped") or ():
        ws_stats.append([
            tr("Missing and skipped items"),
            r.get("path_label") or "",
            r.get("status_label") or r.get("status") or "",
            r.get("reason") or "",
        ])

    _atomic_save_workbook(wb, path)
    return path


def read_global_index(index_path):
    """Read existing rows from `فهرس الأرشيف.xlsx` if present."""
    if not index_path or not os.path.isfile(index_path):
        return []
    from openpyxl import load_workbook

    try:
        wb = load_workbook(index_path, data_only=False)
    except Exception:  # noqa: BLE001
        return []
    ws = wb.active
    rows = []
    for row_idx in range(2, ws.max_row + 1):
        code = ws.cell(row=row_idx, column=1).value
        if code in (None, ""):
            continue
        link_cell = ws.cell(row=row_idx, column=10)
        folder_link = ""
        if link_cell.hyperlink and link_cell.hyperlink.target:
            folder_link = str(link_cell.hyperlink.target)
        elif link_cell.value:
            folder_link = str(link_cell.value)
        job_id_val = ws.cell(row=row_idx, column=11).value
        rows.append({
            "code": str(code).strip(),
            "title": str(ws.cell(row=row_idx, column=2).value or "").strip(),
            "client": str(ws.cell(row=row_idx, column=3).value or "").strip(),
            "deed_no": str(ws.cell(row=row_idx, column=4).value or "").strip(),
            "district": str(ws.cell(row=row_idx, column=5).value or "").strip(),
            "dates": str(ws.cell(row=row_idx, column=6).value or "").strip(),
            "surveyors": str(ws.cell(row=row_idx, column=7).value or "").strip(),
            "percent": ws.cell(row=row_idx, column=8).value,
            "status": str(ws.cell(row=row_idx, column=9).value or "").strip(),
            "folder": folder_link,
            "job_id": str(job_id_val or code).strip(),
        })
    wb.close()
    return rows


def update_global_index(index_path, job, model, job_dir, rtl=None):
    """Atomically rewrite `فهرس الأرشيف.xlsx` at the archive root with one row per job."""
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter

    if rtl is None:
        rtl = is_rtl()

    info = _extract_job_fields(job, model)
    archive_root = os.path.dirname(os.path.abspath(index_path))
    try:
        rel_folder = os.path.relpath(os.path.abspath(job_dir), archive_root)
    except ValueError:
        rel_folder = os.path.abspath(job_dir)

    new_entry = {
        "code": info["job_no"] or info["job_id"],
        "title": info["title"],
        "client": info["client"],
        "deed_no": info["deed_no"],
        "district": info["district"],
        "dates": info["dates"],
        "surveyors": info["surveyors"],
        "percent": f"{info['percent']}%",
        "status": info["status_label"],
        "folder": rel_folder,
        "job_id": info["job_id"],
    }

    existing = read_global_index(index_path)
    merged_rows = []
    replaced = False
    for r in existing:
        same_job = (
            (r.get("job_id") and info["job_id"] and r["job_id"] == info["job_id"])
            or (r.get("code") == new_entry["code"])
        )
        if same_job:
            if not replaced:
                merged_rows.append(new_entry)
                replaced = True
        else:
            merged_rows.append(r)
    if not replaced:
        merged_rows.append(new_entry)

    wb = Workbook()
    ws = wb.active
    ws.title = tr("Archive index")[:31]
    ws.sheet_view.rightToLeft = bool(rtl)

    headers = [
        tr("Job number"),
        tr("Job title:"),
        tr("Client / owner"),
        tr("Deed number"),
        tr("District"),
        tr("Dates"),
        tr("Surveyor"),
        tr("Completion"),
        tr("Status"),
        tr("Folder"),
        tr("Job ID"),
    ]
    widths = [18, 30, 26, 18, 22, 28, 24, 14, 16, 36, 16]
    head_font = Font(bold=True, color="FFFFFF")
    head_fill = PatternFill("solid", fgColor=ACCENT)
    link_font = Font(color="0563C1", underline="single")

    ws.append(headers)
    for c in ws[1]:
        c.font = head_font
        c.fill = head_fill
        c.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
    ws.freeze_panes = "A2"

    for row_idx, r in enumerate(merged_rows, start=2):
        folder_val = r.get("folder") or ""
        ws.append([
            r.get("code") or "",
            r.get("title") or "",
            r.get("client") or "",
            r.get("deed_no") or "",
            r.get("district") or "",
            r.get("dates") or "",
            r.get("surveyors") or "",
            r.get("percent") if r.get("percent") is not None else "",
            r.get("status") or "",
            folder_val,
            r.get("job_id") or r.get("code") or "",
        ])
        fcell = ws.cell(row=row_idx, column=10)
        if folder_val:
            fcell.hyperlink = folder_val.replace("\\", "/")
            fcell.font = link_font

    if merged_rows:
        ws.auto_filter.ref = f"A1:{get_column_letter(len(headers))}{len(merged_rows) + 1}"
    for i, w in enumerate(widths, start=1):
        ws.column_dimensions[get_column_letter(i)].width = w

    _atomic_save_workbook(wb, index_path)
    return new_entry
