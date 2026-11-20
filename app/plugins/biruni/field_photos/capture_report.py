"""Sealed A4 PDF report with statistics for a capture job (Phase E).

Built on the existing Arabic PDF engine in `survey/report_pdf.py` (QPdfWriter + QPainter,
`font_family()`, `_Pen`, `_table`, RTL through `QTextOption`) and sealed with
`seal.core.seal_file(path, certificate=True)`.
"""
import datetime as dt
import math
import os

from qgis.core import (QgsCoordinateReferenceSystem, QgsCoordinateTransform, QgsMapRendererCustomPainterJob,
                       QgsMapSettings, QgsPointXY, QgsProject, QgsRectangle)
from qgis.PyQt.QtCore import QMarginsF, QPointF, QRectF, QSize, Qt
from qgis.PyQt.QtGui import (QColor, QImage, QImageReader, QPageSize, QPainter, QPdfWriter, QPen,
                             QPolygonF, QTextOption)

from .. import i18n
from ..i18n import tr
from ..seal.core import office as seal_office, seal_file
from ..survey.report_pdf import A4_H, A4_W, ACCENT, GRID, INK, LIGHT, MARGIN, _Pen, _table, font_family
from .capture import clean_cap, clean_schema, missing, progress_counts, schema_key, stats, tree

LRI = "\u2066"
PDI = "\u2069"
MAX_IMAGE_LONG_SIDE = 1000
PHOTOS_PER_ROW = 2
PHOTOS_PER_COL = 3
PHOTOS_PER_PAGE = PHOTOS_PER_ROW * PHOTOS_PER_COL

MUTED = QColor("#5b6670")
WARN_BG = QColor("#fff7e6")
WARN_INK = QColor("#9a5b00")
MISS_BG = QColor("#fdecec")
MISS_INK = QColor("#b42318")
OK_INK = QColor("#0f7b55")


def _lri(text):
    """Wrap LTR text (coordinates, ISO dates, IDs) in Unicode LRI/PDI isolation so digits are never reversed in RTL."""
    return f"{LRI}{text}{PDI}"


def _parse_iso_dt(val):
    if not val:
        return None
    if isinstance(val, (int, float)):
        ts = float(val) / 1000.0 if val > 1e11 else float(val)
        return dt.datetime.fromtimestamp(ts, dt.timezone.utc).astimezone()
    s = str(val).strip()
    if not s:
        return None
    try:
        d = dt.datetime.fromisoformat(s.replace("Z", "+00:00"))
        return d.astimezone() if d.tzinfo else d
    except ValueError:
        return None


def _format_local_time(val):
    d = _parse_iso_dt(val)
    if d is not None:
        return d.strftime("%Y-%m-%d %H:%M:%S")
    return str(val or "").replace("T", " ")[:19]


def _format_coords(lat, lon, acc=None):
    if lat is None or lon is None:
        return ""
    try:
        c_txt = f"{float(lat):.6f}, {float(lon):.6f}"
    except (TypeError, ValueError):
        return ""
    if acc is not None:
        try:
            c_txt += f" (±{float(acc):.1f} m)"
        except (TypeError, ValueError):
            pass
    return _lri(c_txt)


def _format_answer_value(val):
    if val is True:
        return tr("Yes")
    if val is False:
        return tr("No")
    if isinstance(val, (list, tuple)):
        return ", ".join(str(x) for x in val if x not in (None, ""))
    if isinstance(val, float) and val == int(val):
        return str(int(val))
    return str(val)


def _format_item_answers(q_defs, answers):
    if not answers:
        return []
    out = []
    seen = set()
    for q in q_defs or ():
        qname = q["name"]
        if qname in answers and answers[qname] not in (None, "", []):
            seen.add(qname)
            out.append((q["label"], _format_answer_value(answers[qname])))
    for qname, val in answers.items():
        if qname not in seen and val not in (None, "", []):
            out.append((qname, _format_answer_value(val)))
    return out


def _resolve_photo_path(rec, folder=""):
    p = rec.get("photo") or ""
    if p and os.path.isabs(p):
        return p
    if p and folder:
        cand = os.path.join(folder, p)
        if os.path.exists(cand):
            return cand
    if p:
        return p
    rid = rec.get("id") or ""
    if folder and rid:
        for ext in ("jpg", "jpeg", "png", "webp", "heic", "heif"):
            cand = os.path.join(folder, f"{rid}.{ext}")
            if os.path.exists(cand):
                return cand
    return ""


def decode_report_image(path, max_long_side=MAX_IMAGE_LONG_SIDE):
    """Decode one image from disk with QImageReader, EXIF auto-transform, and scaled to max_long_side.
    Returns (QImage, error_string); QImage is None when missing or undecodable (e.g. HEIC)."""
    if not path or not os.path.isfile(path):
        return None, "missing"
    reader = QImageReader(path)
    reader.setAutoTransform(True)
    sz = reader.size()
    if sz.isValid() and sz.width() > 0 and sz.height() > 0:
        long_side = max(sz.width(), sz.height())
        if long_side > max_long_side:
            scale = max_long_side / float(long_side)
            reader.setScaledSize(QSize(max(1, int(round(sz.width() * scale))),
                                       max(1, int(round(sz.height() * scale)))))
    img = reader.read()
    if img.isNull():
        return None, reader.errorString() or "undecodable"
    if max(img.width(), img.height()) > max_long_side:
        img = img.scaled(max_long_side, max_long_side,
                         Qt.AspectRatioMode.KeepAspectRatio,
                         Qt.TransformationMode.SmoothTransformation)
    return img, ""


def report_model(job, merged_state, records=(), folder=""):
    """Build pure-data model for a capture job report: ordered sections/items, captions, stats, missing items."""
    job = job if isinstance(job, dict) else {}
    raw_schema = job.get("schema")
    if isinstance(raw_schema, dict):
        schema = clean_schema(raw_schema) if raw_schema.get("biruni_capture") != 1 else raw_schema
    else:
        from .capture_jobs import resolve_job_schema

        schema, _ = resolve_job_schema(None, job.get("id") or (merged_state or {}).get("job") or "JUNKNOWN",
                                       merged_state, records)

    job_id = str(job.get("id") or (merged_state or {}).get("job") or "").strip()
    st_in = dict(merged_state) if isinstance(merged_state, dict) else {}
    if job_id and not st_in.get("job"):
        st_in["job"] = job_id

    t = tree(schema, st_in, records)
    st_info = stats(t)
    pc = progress_counts(t)
    miss_list = missing(t)

    # Map node definitions and questions for human-readable statistics
    node_map = {}

    def _collect_nodes(nodes):
        for n in nodes or ():
            node_map[n["id"]] = n
            if n.get("nodes"):
                _collect_nodes(n["nodes"])

    _collect_nodes(schema.get("nodes") or ())

    # Job questions (job_no, client, deed_no, district, etc.)
    job_data = job.get("data") if isinstance(job.get("data"), dict) else {}
    job_questions = []
    job_no = ""
    client = ""
    for q in schema.get("job") or ():
        qname = q["name"]
        val = job_data.get(qname)
        sval = _format_answer_value(val) if val not in (None, "", []) else "—"
        if qname == "job_no" and val not in (None, ""):
            job_no = str(val)
        elif qname == "client" and val not in (None, ""):
            client = str(val)
        job_questions.append({
            "name": qname,
            "label": q["label"],
            "type": q["type"],
            "value": sval,
            "raw_value": val,
        })
    if not job_no:
        job_no = str(job_data.get("job_no") or job.get("title") or job_id)
    if not client and job_data.get("client"):
        client = str(job_data["client"])

    # Walk tree in exact tree order to collect sections, items, photos, and numeric question counts for averages
    sections = []
    ordered_items = []
    ordered_photos = []
    num_values = {}
    surveyors = []
    seen_surveyors = set()
    photo_dts = []
    points = []

    def _add_surveyor(name):
        nm = str(name or "").strip()
        if nm and nm != tr("Unknown") and nm not in seen_surveyors:
            seen_surveyors.add(nm)
            surveyors.append(nm)

    if job.get("to"):
        _add_surveyor(job.get("to"))
    if isinstance(merged_state, dict) and merged_state.get("by"):
        _add_surveyor(merged_state.get("by"))

    def _make_photo_entry(rec, item_node, section_label, is_extra=False):
        pid = rec.get("id") or ""
        path_lbl = (tr("Extra photo for this job") if is_extra
                    else (item_node.get("path_label") or (rec.get("cap") or {}).get("pl") or ""))
        taken_raw = rec.get("taken") or rec.get("t") or ""
        dt_obj = _parse_iso_dt(taken_raw)
        if dt_obj is not None:
            photo_dts.append(dt_obj)
        local_time = _format_local_time(taken_raw)
        lat = rec.get("lat")
        lon = rec.get("lon")
        acc = rec.get("acc")
        if isinstance(lat, (int, float)) and isinstance(lon, (int, float)):
            points.append({"id": pid, "lon": float(lon), "lat": float(lat), "acc": acc,
                           "path_label": path_lbl, "extra": is_extra})
        coords_txt = _format_coords(lat, lon, acc)
        who = rec.get("surveyor") or rec.get("by") or ""
        _add_surveyor(who)

        item_note = "" if is_extra else (item_node.get("note") or "")
        rec_note = rec.get("note") or ""
        if item_note and rec_note and item_note != rec_note:
            note_txt = f"{item_note} — {rec_note}"
        else:
            note_txt = item_note or rec_note

        ans_pairs = [] if is_extra else _format_item_answers(item_node.get("q") or (), item_node.get("answers") or {})
        ans_txt = " · ".join(f"{lbl}: {val}" for lbl, val in ans_pairs)
        is_preview = bool(rec.get("preview"))
        img_path = _resolve_photo_path(rec, folder)

        caption_lines = [path_lbl]
        if is_preview:
            caption_lines[0] = f"{path_lbl} [{tr('Preview')}]"
        meta_parts = [x for x in ( _lri(local_time) if local_time else "", coords_txt) if x]
        if meta_parts:
            caption_lines.append(" · ".join(meta_parts))
        if ans_txt:
            caption_lines.append(ans_txt)
        if note_txt:
            caption_lines.append(f"{tr('Note')}: {note_txt}")

        return {
            "id": pid,
            "k": "*" if is_extra else (item_node.get("k") or ""),
            "section": section_label,
            "path_label": path_lbl,
            "taken": taken_raw,
            "local_time": local_time,
            "lat": lat,
            "lon": lon,
            "acc": acc,
            "coords_text": coords_txt,
            "note": note_txt,
            "answers": ans_pairs,
            "answers_text": ans_txt,
            "preview": is_preview,
            "extra": is_extra,
            "photo_path": img_path,
            "file_name": os.path.basename(img_path) if img_path else (f"{pid}.jpg" if pid else ""),
            "surveyor": who,
            "caption_lines": caption_lines,
            "caption": "\n".join(caption_lines),
        }

    def _walk_slot(item, sec):
        ans_pairs = _format_item_answers(item.get("q") or (), item.get("answers") or {})
        for q in item.get("q") or ():
            qname, qtype = q["name"], q["type"]
            if qtype in ("number", "integer") and qname in (item.get("answers") or {}):
                try:
                    v = float(item["answers"][qname])
                    key = f"{item['id']}.{qname}"
                    num_values.setdefault(key, []).append(v)
                except (TypeError, ValueError):
                    pass

        item_photos = [_make_photo_entry(r, item, sec["label"], is_extra=False) for r in (item.get("records") or ())]
        entry = {
            "kind": item["kind"],
            "id": item["id"],
            "iid": item["iid"],
            "order": item.get("order"),
            "k": item["k"],
            "label": item["label"],
            "path": list(item.get("path") or ()),
            "path_label": item["path_label"],
            "section": sec["label"],
            "req": item["req"],
            "done": item["done"],
            "own_done": item["own_done"],
            "skipped": item["skipped"],
            "skip": item["skip"],
            "own_skip": item["own_skip"],
            "note": item["note"],
            "answers": ans_pairs,
            "answers_dict": dict(item.get("answers") or {}),
            "missing_q": list(item.get("missing_q") or ()),
            "photo_count": item["photo_count"],
            "received_count": item["received_count"],
            "pending_count": item["pending_count"],
            "photos_spec": list(item.get("photos") or (0, 0)),
            "photos": item_photos,
        }
        sec["items"].append(entry)
        ordered_items.append(entry)
        sec["photos"].extend(item_photos)
        ordered_photos.extend(item_photos)
        for ch in item.get("children") or ():
            _walk_node(ch, sec)

    def _walk_node(item, sec):
        if item["kind"] == "repeat":
            if item.get("own_skip"):
                ordered_items.append({
                    "kind": "repeat",
                    "id": item["id"],
                    "iid": item["iid"],
                    "order": None,
                    "k": item["k"],
                    "label": item["label"],
                    "path": list(item.get("path") or ()),
                    "path_label": item["path_label"],
                    "section": sec["label"],
                    "req": item["req"],
                    "done": item["done"],
                    "own_done": item["done"],
                    "skipped": item["skipped"],
                    "skip": item["skip"],
                    "own_skip": item["own_skip"],
                    "note": "",
                    "answers": [],
                    "answers_dict": {},
                    "missing_q": [],
                    "photo_count": 0,
                    "received_count": 0,
                    "pending_count": 0,
                    "photos_spec": [0, 0],
                    "photos": [],
                })
            for inst_it in item.get("instances") or ():
                _walk_slot(inst_it, sec)
        else:
            _walk_slot(item, sec)

    for root in t["roots"]:
        sec = {
            "id": root["id"],
            "label": root["label"],
            "items": [],
            "photos": [],
            "photo_count": st_info["section_photos"].get(root["label"], 0),
        }
        _walk_node(root, sec)
        sections.append(sec)

    extra_photos = [
        _make_photo_entry(r, {}, tr("Appendix: Extra photos"), is_extra=True)
        for r in (t.get("extra_records") or ())
    ]

    # Build rich stats tables
    repeat_rows = []
    for nid, count in st_info["instances"].items():
        n_def = node_map.get(nid) or {}
        repeat_rows.append({
            "id": nid,
            "label": n_def.get("label") or nid,
            "count": count,
        })

    choice_rows = []
    for key, tally in st_info["choices"].items():
        nid, qname = key.split(".", 1) if "." in key else ("", key)
        n_def = node_map.get(nid) or {}
        q_def = next((q for q in (n_def.get("q") or ()) if q["name"] == qname), None) or {}
        q_label = q_def.get("label") or qname
        n_label = n_def.get("label") or nid
        full_label = f"{n_label} — {q_label}" if n_label else q_label
        formatted_buckets = []
        for choice_val, cnt in tally.items():
            disp_val = tr("Yes") if choice_val == "yes" else tr("No") if choice_val == "no" else choice_val
            formatted_buckets.append((disp_val, cnt))
        summary = " / ".join(f"{v}: {c}" for v, c in formatted_buckets)
        choice_rows.append({
            "key": key,
            "node_id": nid,
            "question": qname,
            "label": full_label,
            "tallies": dict(tally),
            "display_tallies": formatted_buckets,
            "summary": summary,
        })

    number_rows = []
    for key, total_val in st_info["sums"].items():
        nid, qname = key.split(".", 1) if "." in key else ("", key)
        n_def = node_map.get(nid) or {}
        q_def = next((q for q in (n_def.get("q") or ()) if q["name"] == qname), None) or {}
        q_label = q_def.get("label") or qname
        n_label = n_def.get("label") or nid
        full_label = f"{n_label} — {q_label}" if n_label else q_label
        vals = num_values.get(key) or [total_val]
        cnt = len(vals)
        avg_val = (sum(vals) / cnt) if cnt else 0.0
        is_int = q_def.get("type") == "integer"
        number_rows.append({
            "key": key,
            "node_id": nid,
            "question": qname,
            "label": full_label,
            "type": q_def.get("type") or "number",
            "count": cnt,
            "sum": int(total_val) if is_int and total_val == int(total_val) else round(total_val, 2),
            "avg": round(avg_val, 2),
        })

    section_photo_rows = [
        {"label": sec_lbl, "count": cnt}
        for sec_lbl, cnt in st_info["section_photos"].items()
    ]
    total_section_photos = sum(st_info["section_photos"].values())
    total_all_photos = total_section_photos + st_info["extra_photos"]

    # Missing & skipped table rows: both skipped items with reasons AND items still missing without a reason
    missing_and_skipped = []
    for sk in st_info["skipped"]:
        missing_and_skipped.append({
            "k": sk["k"],
            "path_label": sk["path_label"],
            "status": "skipped",
            "status_label": tr("Skipped with reason"),
            "reason": sk["reason"],
        })
    for m in miss_list:
        nid = m.get("id") or ""
        n_def = node_map.get(nid) or {}
        q_labels = {q["name"]: q["label"] for q in (n_def.get("q") or ())}
        if m.get("reason") == "repeat_min":
            detail = tr("needs {have}/{min}").format(have=m.get("have", 0), min=m.get("min", 1))
        else:
            parts = []
            if m.get("need_photos", 0) > 0:
                parts.append(tr("needs {n} more photo(s)").format(n=m["need_photos"]))
            if m.get("missing_q"):
                q_names = ", ".join(q_labels.get(qn, qn) for qn in m["missing_q"])
                parts.append(f"{tr('missing required answers')} ({q_names})")
            detail = " · ".join(parts) if parts else tr("Incomplete — no reason given")
        missing_and_skipped.append({
            "k": m["k"],
            "path_label": m["path_label"],
            "status": "missing",
            "status_label": tr("Missing (no reason)"),
            "reason": detail,
        })

    first_dt = min(photo_dts).strftime("%Y-%m-%d %H:%M") if photo_dts else ""
    last_dt = max(photo_dts).strftime("%Y-%m-%d %H:%M") if photo_dts else ""
    if not first_dt and job.get("created"):
        d0 = _parse_iso_dt(job["created"])
        if d0:
            first_dt = d0.strftime("%Y-%m-%d %H:%M")

    if isinstance(job.get("loc"), (list, tuple)) and len(job["loc"]) == 2 and not points:
        try:
            points.append({"id": "job_loc", "lon": float(job["loc"][0]), "lat": float(job["loc"][1]),
                           "acc": None, "path_label": job.get("title") or job_id, "extra": False})
        except (TypeError, ValueError):
            pass

    return {
        "job_id": job_id,
        "title": job.get("title") or job_no or job_id,
        "template_title": schema.get("title") or "",
        "schema_ref": job.get("s") or schema_key(schema),
        "schema": schema,
        "status": job.get("st") or "open",
        "job_no": job_no,
        "client": client,
        "job_questions": job_questions,
        "first_photo": first_dt,
        "last_photo": last_dt,
        "surveyors": surveyors,
        "office": job.get("office") or seal_office() or "",
        "percent": pc["percent"],
        "done_count": pc["done"],
        "total_count": pc["total"],
        "complete": st_info["complete"],
        "points": points,
        "job_loc": job.get("loc"),
        "stats": {
            "instances": dict(st_info["instances"]),
            "choices": dict(st_info["choices"]),
            "sums": dict(st_info["sums"]),
            "averages": {r["key"]: r["avg"] for r in number_rows},
            "counts": {r["key"]: r["count"] for r in number_rows},
            "section_photos": dict(st_info["section_photos"]),
            "extra_photos": st_info["extra_photos"],
            "total_photos": total_all_photos,
            "repeat_rows": repeat_rows,
            "choice_rows": choice_rows,
            "number_rows": number_rows,
            "section_photo_rows": section_photo_rows,
            "skipped": list(st_info["skipped"]),
            "missing": list(miss_list),
            "missing_and_skipped": missing_and_skipped,
        },
        "sections": sections,
        "items": ordered_items,
        "photos": ordered_photos,
        "extra_photos": extra_photos,
    }


def render_location_map(model, iface=None, project=None, width_px=1200, height_px=800):
    """Render the project layers at the photo extent + 20% with QgsMapRendererCustomPainterJob and draw the
    photo points on top. Returns None when no project layers are available (so write_report draws the plain frame)."""
    proj = project or QgsProject.instance()
    layers = []
    if iface is not None and hasattr(iface, "mapCanvas") and iface.mapCanvas() is not None:
        layers = [lyr for lyr in iface.mapCanvas().layers() if lyr is not None and lyr.isValid()]
    if not layers and proj is not None:
        layers = [lyr for lyr in proj.mapLayers().values() if lyr is not None and lyr.isValid() and lyr.isSpatial()]
    if not layers:
        return None

    pts = model.get("points") or []
    if not pts:
        return None

    wgs84 = QgsCoordinateReferenceSystem("EPSG:4326")
    dest_crs = proj.crs() if (proj is not None and proj.crs().isValid()) else (layers[0].crs() if layers[0].crs().isValid() else wgs84)
    xf = QgsCoordinateTransform(wgs84, dest_crs, proj) if dest_crs != wgs84 else None

    proj_pts = []
    for p in pts:
        pt = QgsPointXY(p["lon"], p["lat"])
        if xf is not None:
            try:
                pt = xf.transform(pt)
            except Exception:  # noqa: BLE001
                continue
        proj_pts.append((pt, p))
    if not proj_pts:
        return None

    xs = [pt.x() for pt, _ in proj_pts]
    ys = [pt.y() for pt, _ in proj_pts]
    minx, maxx = min(xs), max(xs)
    miny, maxy = min(ys), max(ys)
    dx = maxx - minx
    dy = maxy - miny
    if dx < 1e-5 and dy < 1e-5:
        pad = 0.001 if dest_crs.isGeographic() else 100.0
        rect = QgsRectangle(minx - pad, miny - pad, maxx + pad, maxy + pad)
    else:
        pad_x = max(dx * 0.20, 1e-5 if dest_crs.isGeographic() else 10.0)
        pad_y = max(dy * 0.20, 1e-5 if dest_crs.isGeographic() else 10.0)
        rect = QgsRectangle(minx - pad_x, miny - pad_y, maxx + pad_x, maxy + pad_y)

    img = QImage(width_px, height_px, QImage.Format.Format_ARGB32_Premultiplied)
    img.fill(QColor("#f7f9fc"))

    ms = QgsMapSettings()
    ms.setLayers(layers)
    ms.setDestinationCrs(dest_crs)
    ms.setExtent(rect)
    ms.setOutputSize(QSize(width_px, height_px))
    ms.setBackgroundColor(QColor("#f7f9fc"))

    painter = QPainter(img)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    try:
        job = QgsMapRendererCustomPainterJob(ms, painter)
        job.start()
        job.waitForFinished()

        m2p = ms.mapToPixel()
        for idx, (pt, pinfo) in enumerate(proj_pts, 1):
            px_pt = m2p.transform(pt)
            qpt = QPointF(px_pt.x(), px_pt.y())
            painter.setPen(QPen(QColor("#ffffff"), 3.0))
            painter.setBrush(QColor("#d97706") if pinfo.get("extra") else ACCENT)
            painter.drawEllipse(qpt, 8.0, 8.0)
    finally:
        painter.end()
    return img


def _col_layout(x, w, widths, rtl):
    """Column (cx, cw) pairs in logical order: rightmost-first when rtl=True, leftmost-first when rtl=False."""
    xs = []
    if rtl:
        cur = x + w
        for frac in widths:
            cw = w * frac
            cur -= cw
            xs.append((cur, cw))
    else:
        cur = x
        for frac in widths:
            cw = w * frac
            xs.append((cur, cw))
            cur += cw
    return xs


def _draw_table(pen, x, y, w, header, rows, widths, rtl=True, row_h=6.2, size=8.2, rtl_cols=None):
    """Draw a table using survey/report_pdf._table in RTL, or mirrored column order in LTR."""
    if rtl:
        return _table(pen, x, y, w, header, rows, widths, row_h=row_h, size=size, rtl_cols=rtl_cols)
    rtl_cols = rtl_cols or [False] * len(header)
    xs = _col_layout(x, w, widths, rtl=False)
    pen.box(x, y, w, row_h, fill=ACCENT, color=ACCENT)
    for (cx, cw), h in zip(xs, header):
        pen.text(cx + 1, y, cw - 2, row_h, h, size, True, "center", QColor("white"), rtl=False)
    y += row_h
    for i, row in enumerate(rows):
        lines = max(1, max(math.ceil(len(str(v)) * size * 0.19 / max(cw - 2, 1)) for v, (_, cw) in zip(row, xs)))
        rh = row_h * min(lines, 3)
        pen.box(x, y, w, rh, fill=LIGHT if i % 2 else None)
        for (cx, cw), v, col_rtl in zip(xs, row, rtl_cols):
            pen.text(cx + 1, y, cw - 2, rh, v, size, False, "right" if col_rtl else "left", rtl=col_rtl)
        for cx, _ in xs[1:]:
            pen.line(cx, y, cx, y + rh)
        y += rh
    return y


def _draw_header_banner(pen, model, title_txt, subtitle_txt="", rtl=True):
    x0, w = MARGIN, A4_W - 2 * MARGIN
    y = MARGIN
    pen.box(x0, y, w, 20, fill=LIGHT, color=ACCENT, width=0.4)
    align_start = "right" if rtl else "left"
    align_end = "left" if rtl else "right"
    office_txt = model.get("office") or tr("Engineering survey office")
    job_badge = f"{tr('Job ID')}: {_lri(model.get('job_id') or '—')}"

    if rtl:
        pen.text(x0 + w - 68, y + 2, 66, 8, office_txt, 10, True, "right", ACCENT, rtl=True)
        pen.text(x0 + w - 68, y + 10.5, 66, 7, job_badge, 8, False, "right", INK, rtl=True)
        pen.text(x0 + 2, y + 2, 50, 8, _lri(f"{model.get('percent', 0)}%"), 11, True, "left", ACCENT, rtl=False)
        pen.text(x0 + 2, y + 10.5, 50, 7, model.get("template_title") or "", 8, False, "left", MUTED, rtl=True)
        pen.text(x0 + 52, y + 2.5, w - 122, 9, title_txt, 13.5, True, "center", INK, rtl=True)
        if subtitle_txt:
            pen.text(x0 + 52, y + 11.5, w - 122, 7, subtitle_txt, 8.5, False, "center", MUTED, rtl=True)
    else:
        pen.text(x0 + 2, y + 2, 66, 8, office_txt, 10, True, align_start, ACCENT, rtl=False)
        pen.text(x0 + 2, y + 10.5, 66, 7, job_badge, 8, False, align_start, INK, rtl=False)
        pen.text(x0 + w - 52, y + 2, 50, 8, f"{model.get('percent', 0)}%", 11, True, align_end, ACCENT, rtl=False)
        pen.text(x0 + w - 52, y + 10.5, 50, 7, model.get("template_title") or "", 8, False, align_end, MUTED, rtl=False)
        pen.text(x0 + 70, y + 2.5, w - 122, 9, title_txt, 13.5, True, "center", INK, rtl=False)
        if subtitle_txt:
            pen.text(x0 + 70, y + 11.5, w - 122, 7, subtitle_txt, 8.5, False, "center", MUTED, rtl=False)
    return y + 23


def _draw_footer(pen, page_no, total_pages, rtl=True):
    x0, w = MARGIN, A4_W - 2 * MARGIN
    fy = A4_H - MARGIN - 4.5
    pen.line(x0, fy - 0.5, x0 + w, fy - 0.5)
    page_str = _lri(f"{page_no} / {total_pages}")
    made_str = tr("Prepared with Biruni GIS")
    if rtl:
        pen.text(x0, fy, w / 2, 4.5, page_str, 7.5, False, "left", GRID, rtl=False)
        pen.text(x0 + w / 2, fy, w / 2, 4.5, made_str, 7.5, False, "right", GRID, rtl=True)
    else:
        pen.text(x0, fy, w / 2, 4.5, made_str, 7.5, False, "left", GRID, rtl=False)
        pen.text(x0 + w / 2, fy, w / 2, 4.5, page_str, 7.5, False, "right", GRID, rtl=False)


def _draw_key_value_grid(pen, x0, y, w, pairs, rtl=True, row_h=7.2, label_w=36.0):
    """Draw (label, value) pairs two per row, mirrored for RTL/LTR."""
    rows = [pairs[i:i + 2] for i in range(0, len(pairs), 2)]
    half = w / 2.0
    align = "right" if rtl else "left"
    for row in rows:
        for j, (label, value) in enumerate(row):
            bx = (x0 + w - (j + 1) * half) if rtl else (x0 + j * half)
            pen.box(bx, y, half, row_h)
            if rtl:
                pen.box(bx + half - label_w, y, label_w, row_h, fill=LIGHT)
                pen.text(bx + half - label_w + 1, y, label_w - 2, row_h, label, 8.2, True, "right", INK, rtl=True)
                pen.text(bx + 1.5, y, half - label_w - 3, row_h, value, 8.5, False, "right", INK, rtl=True)
            else:
                pen.box(bx, y, label_w, row_h, fill=LIGHT)
                pen.text(bx + 1.5, y, label_w - 3, row_h, label, 8.2, True, "left", INK, rtl=False)
                pen.text(bx + label_w + 1.5, y, half - label_w - 3, row_h, value, 8.5, False, "left", INK, rtl=False)
        y += row_h
    return y


def _draw_cover_page(painter, pen, model, page_no, total_pages, rtl=True):
    x0, w = MARGIN, A4_W - 2 * MARGIN
    y = _draw_header_banner(
        pen, model,
        tr("Capture Job Report"),
        f"{model.get('title') or model.get('job_id')} — {model.get('template_title')}",
        rtl=rtl,
    )
    align = "right" if rtl else "left"

    # Completion summary banner
    pct = model.get("percent", 0)
    complete = model.get("complete", False)
    miss_n = len((model.get("stats") or {}).get("missing") or ())
    status_word = tr("Complete") if complete else tr("{n} missing").format(n=miss_n)
    pen.box(x0, y, w, 16, fill=LIGHT if complete else WARN_BG, color=ACCENT if complete else WARN_INK, width=0.35)
    pen.text(x0 + 3, y + 1, w - 6, 7,
             f"{tr('Job title:')} {model.get('title') or '—'}",
             11, True, align, INK, rtl=rtl)
    pen.text(x0 + 3, y + 8.5, w - 6, 6,
             f"{tr('Completion')}: {_lri(f'{pct}%')} ({status_word})  ·  "
             f"{tr('Total photos: {n}').format(n=_lri(model['stats']['total_photos']))}",
             9, True, align, OK_INK if complete else WARN_INK, rtl=rtl)
    y += 20

    # Primary metadata table
    pen.text(x0, y, w, 6, tr("Job details"), 10, True, align, ACCENT, rtl=rtl)
    y += 6.5
    surveyors_txt = "، ".join(model["surveyors"]) if (rtl and model.get("surveyors")) else (
        ", ".join(model.get("surveyors") or ()) or "—"
    )
    meta_pairs = [
        (tr("Job title:"), model.get("title") or "—"),
        (tr("Template:"), f"{model.get('template_title') or '—'} ({_lri(model.get('schema_ref') or '')})"),
        (tr("Job number"), _lri(model.get("job_no") or model.get("job_id") or "—")),
        (tr("Client / owner"), model.get("client") or "—"),
        (tr("First photo"), _lri(model.get("first_photo") or "—")),
        (tr("Last photo"), _lri(model.get("last_photo") or "—")),
        (tr("Surveyor"), surveyors_txt),
        (tr("Office"), model.get("office") or "—"),
        (tr("Status"), tr("Closed") if model.get("status") == "closed" else tr("In progress")),
        (tr("Completion"), f"{_lri(str(pct) + '%')} ({_lri(str(model.get('done_count', 0)) + '/' + str(model.get('total_count', 0)))})"),
    ]
    y = _draw_key_value_grid(pen, x0, y, w, meta_pairs, rtl=rtl, row_h=7.5, label_w=38.0) + 5

    # Job questions answered when creating the job
    job_qs = model.get("job_questions") or []
    if job_qs:
        pen.text(x0, y, w, 6, tr("Job questions"), 10, True, align, ACCENT, rtl=rtl)
        y += 6.5
        q_pairs = [(q["label"], str(q["value"])) for q in job_qs]
        y = _draw_key_value_grid(pen, x0, y, w, q_pairs, rtl=rtl, row_h=7.2, label_w=42.0) + 5

    # Section overview table on the cover
    sec_rows = [(s["label"], _lri(str(s["count"]))) for s in model["stats"]["section_photo_rows"]]
    if model["stats"]["extra_photos"]:
        sec_rows.append((tr("Extra photo for this job"), _lri(str(model["stats"]["extra_photos"]))))
    sec_rows.append((tr("Total"), _lri(str(model["stats"]["total_photos"]))))
    if sec_rows:
        pen.text(x0, y, w, 6, tr("Photos per section"), 10, True, align, ACCENT, rtl=rtl)
        y += 6.5
        y = _draw_table(
            pen, x0, y, w,
            [tr("Section"), tr("Photos")],
            sec_rows,
            [0.72, 0.28],
            rtl=rtl, row_h=6.5, size=8.5, rtl_cols=[rtl, False],
        )

    _draw_footer(pen, page_no, total_pages, rtl=rtl)


def _draw_plain_points_frame(painter, pen, model, x, y, w, h, rtl=True):
    """Fallback location map when no QGIS canvas / project layers are available: draws a clean coordinate grid and points."""
    pen.box(x, y, w, h, fill=QColor("#f7f9fc"), color=INK, width=0.35)
    for frac in (0.25, 0.5, 0.75):
        pen.line(x + w * frac, y, x + w * frac, y + h, color=QColor("#dce4ee"), width=0.2)
        pen.line(x, y + h * frac, x + w, y + h * frac, color=QColor("#dce4ee"), width=0.2)

    pts = model.get("points") or []
    if not pts:
        pen.text(x + 10, y + h / 2 - 5, w - 20, 10, tr("No coordinates recorded for this job"),
                 10, False, "center", MUTED, rtl=rtl)
        return

    lons = [p["lon"] for p in pts]
    lats = [p["lat"] for p in pts]
    min_lon, max_lon = min(lons), max(lons)
    min_lat, max_lat = min(lats), max(lats)
    dx = max(max_lon - min_lon, 1e-5)
    dy = max(max_lat - min_lat, 1e-5)
    pad = 20.0
    sx = (w - 2 * pad) / dx
    sy = (h - 2 * pad) / dy
    s = min(sx, sy)
    cx = x + w / 2.0
    cy = y + h / 2.0
    mid_lon = (min_lon + max_lon) / 2.0
    mid_lat = (min_lat + max_lat) / 2.0

    for idx, p in enumerate(pts, 1):
        px = cx + (p["lon"] - mid_lon) * s
        py = cy - (p["lat"] - mid_lat) * s
        pen.p.setPen(QPen(QColor("white"), 0.5 * pen.k))
        pen.p.setBrush(QColor("#d97706") if p.get("extra") else ACCENT)
        pen.p.drawEllipse(pen.pt(px, py), 1.8 * pen.k, 1.8 * pen.k)
        pen.p.setBrush(Qt.BrushStyle.NoBrush)
        if len(pts) <= 40:
            pen.text(px - 4, py - 5.5, 8, 4, str(idx), 6.5, True, "center", INK, rtl=False)

    # North arrow
    ax, ay = x + 10, y + 10
    arrow = QPolygonF([pen.pt(ax, ay), pen.pt(ax + 2.5, ay + 8), pen.pt(ax, ay + 6), pen.pt(ax - 2.5, ay + 8)])
    pen.p.setPen(QPen(INK, 0.3 * pen.k))
    pen.p.setBrush(INK)
    pen.p.drawPolygon(arrow)
    pen.p.setBrush(Qt.BrushStyle.NoBrush)
    pen.text(ax - 4, ay + 8.5, 8, 4, "N", 7, True, "center", INK, rtl=False)

    # Extent coordinates caption at bottom of frame
    ext_txt = _lri(f"[{min_lat:.5f}, {min_lon:.5f}] – [{max_lat:.5f}, {max_lon:.5f}]")
    pen.text(x + 3, y + h - 6.5, w - 6, 5, ext_txt, 7.5, False, "left" if rtl else "right", MUTED, rtl=False)


def _draw_map_page(painter, pen, model, map_image, page_no, total_pages, rtl=True):
    x0, w = MARGIN, A4_W - 2 * MARGIN
    y = _draw_header_banner(pen, model, tr("Location map"), model.get("title") or "", rtl=rtl)
    align = "right" if rtl else "left"
    pts = model.get("points") or []
    pen.text(x0, y, w, 6,
             f"{tr('Photo locations')} ({_lri(str(len(pts)))})",
             10, True, align, ACCENT, rtl=rtl)
    y += 7

    map_h = A4_H - MARGIN - 16 - y
    if isinstance(map_image, QImage) and not map_image.isNull():
        pen.box(x0, y, w, map_h, fill=QColor("#f7f9fc"), color=INK, width=0.35)
        target = pen.r(x0 + 0.5, y + 0.5, w - 1.0, map_h - 1.0)
        scaled = map_image.scaled(int(target.width()), int(target.height()),
                                  Qt.AspectRatioMode.KeepAspectRatio,
                                  Qt.TransformationMode.SmoothTransformation)
        ox = target.x() + (target.width() - scaled.width()) / 2.0
        oy = target.y() + (target.height() - scaled.height()) / 2.0
        painter.drawImage(QPointF(ox, oy), scaled)
    else:
        _draw_plain_points_frame(painter, pen, model, x0, y, w, map_h, rtl=rtl)

    _draw_footer(pen, page_no, total_pages, rtl=rtl)


def _build_stats_pages(model):
    """Split statistics tables across one or more pages so large missing/choice tables never overflow A4."""
    st = model["stats"]
    blocks = []

    if st["repeat_rows"]:
        blocks.append(("repeats", st["repeat_rows"]))
    if st["choice_rows"]:
        blocks.append(("choices", st["choice_rows"]))
    if st["number_rows"]:
        blocks.append(("numbers", st["number_rows"]))
    sec_rows = list(st["section_photo_rows"])
    if st["extra_photos"]:
        sec_rows.append({"label": tr("Extra photo for this job"), "count": st["extra_photos"]})
    sec_rows.append({"label": tr("Total"), "count": st["total_photos"]})
    blocks.append(("section_photos", sec_rows))

    ms_rows = st["missing_and_skipped"]
    if ms_rows:
        chunk_size = 16
        for i in range(0, len(ms_rows), chunk_size):
            blocks.append(("missing", ms_rows[i:i + chunk_size]))
    else:
        blocks.append(("missing", []))

    pages = []
    cur_page = []
    cur_h = 0.0
    max_h = A4_H - 2 * MARGIN - 36.0
    for kind, rows in blocks:
        est_h = 14.0 + max(1, len(rows)) * 6.8
        if cur_page and (cur_h + est_h > max_h):
            pages.append(cur_page)
            cur_page = [(kind, rows)]
            cur_h = est_h
        else:
            cur_page.append((kind, rows))
            cur_h += est_h
    if cur_page or not pages:
        pages.append(cur_page)
    return pages


def _draw_stats_page(painter, pen, model, blocks, page_no, total_pages, rtl=True):
    x0, w = MARGIN, A4_W - 2 * MARGIN
    y = _draw_header_banner(pen, model, tr("Statistics"), model.get("title") or "", rtl=rtl)
    align = "right" if rtl else "left"

    for kind, rows in blocks:
        if y > A4_H - MARGIN - 28:
            break
        if kind == "repeats":
            pen.text(x0, y, w, 6, tr("Instances per repeating item"), 9.5, True, align, ACCENT, rtl=rtl)
            y += 6.2
            t_rows = [(r["label"], _lri(str(r["count"]))) for r in rows]
            y = _draw_table(pen, x0, y, w, [tr("Item"), tr("Count")], t_rows, [0.72, 0.28],
                            rtl=rtl, row_h=6.0, size=8.2, rtl_cols=[rtl, False]) + 4
        elif kind == "choices":
            pen.text(x0, y, w, 6, tr("Choice and yes/no tallies"), 9.5, True, align, ACCENT, rtl=rtl)
            y += 6.2
            t_rows = [(r["label"], r["summary"]) for r in rows]
            y = _draw_table(pen, x0, y, w, [tr("Question"), tr("Tallies")], t_rows, [0.42, 0.58],
                            rtl=rtl, row_h=6.0, size=8.0, rtl_cols=[rtl, rtl]) + 4
        elif kind == "numbers":
            pen.text(x0, y, w, 6, tr("Sums and averages"), 9.5, True, align, ACCENT, rtl=rtl)
            y += 6.2
            t_rows = [(r["label"], _lri(str(r["count"])), _lri(str(r["sum"])), _lri(str(r["avg"]))) for r in rows]
            y = _draw_table(pen, x0, y, w, [tr("Question"), tr("Count"), tr("Sum"), tr("Average")],
                            t_rows, [0.46, 0.18, 0.18, 0.18],
                            rtl=rtl, row_h=6.0, size=8.0, rtl_cols=[rtl, False, False, False]) + 4
        elif kind == "section_photos":
            pen.text(x0, y, w, 6, tr("Photos per section"), 9.5, True, align, ACCENT, rtl=rtl)
            y += 6.2
            t_rows = [(r["label"], _lri(str(r["count"]))) for r in rows]
            y = _draw_table(pen, x0, y, w, [tr("Section"), tr("Photos")], t_rows, [0.72, 0.28],
                            rtl=rtl, row_h=6.0, size=8.2, rtl_cols=[rtl, False]) + 4
        elif kind == "missing":
            pen.text(x0, y, w, 6, tr("Missing and skipped items"), 9.5, True, align, ACCENT, rtl=rtl)
            y += 6.2
            if not rows:
                pen.box(x0, y, w, 8, fill=LIGHT, color=ACCENT, width=0.25)
                pen.text(x0 + 2, y + 1, w - 4, 6, tr("All required items are complete with no skipped items."),
                         8.5, False, align, OK_INK, rtl=rtl)
                y += 11
            else:
                t_rows = [(r["path_label"], r["status_label"], r["reason"]) for r in rows]
                y = _draw_table(pen, x0, y, w, [tr("Path"), tr("State"), tr("Reason / details")],
                                t_rows, [0.40, 0.22, 0.38],
                                rtl=rtl, row_h=6.0, size=7.8, rtl_cols=[rtl, rtl, rtl]) + 4

    _draw_footer(pen, page_no, total_pages, rtl=rtl)


def _draw_photo_grid_page(painter, pen, model, heading, photos, page_no, total_pages, rtl=True):
    """Draw up to 6 photos (2 columns × 3 rows) on an A4 page, decoding one image at a time."""
    x0, w = MARGIN, A4_W - 2 * MARGIN
    y = _draw_header_banner(pen, model, heading, model.get("title") or "", rtl=rtl)
    avail_h = A4_H - MARGIN - 8 - y
    gap_x = 4.0
    gap_y = 4.0
    cell_w = (w - gap_x) / PHOTOS_PER_ROW
    cell_h = (avail_h - gap_y * (PHOTOS_PER_COL - 1)) / PHOTOS_PER_COL
    img_h = cell_h * 0.64
    cap_h = cell_h - img_h
    align = "right" if rtl else "left"

    for idx, ph in enumerate(photos[:PHOTOS_PER_PAGE]):
        r_idx = idx // PHOTOS_PER_ROW
        c_idx = idx % PHOTOS_PER_ROW
        if rtl:
            cx = x0 + w - (c_idx + 1) * cell_w - c_idx * gap_x
        else:
            cx = x0 + c_idx * (cell_w + gap_x)
        cy = y + r_idx * (cell_h + gap_y)

        pen.box(cx, cy, cell_w, cell_h, fill=QColor("white"), color=GRID, width=0.3)
        img_box_x = cx + 1.5
        img_box_y = cy + 1.5
        img_box_w = cell_w - 3.0
        img_box_h = img_h - 2.0

        img, _err = decode_report_image(ph.get("photo_path") or "", MAX_IMAGE_LONG_SIDE)
        if img is not None and not img.isNull():
            pen.box(img_box_x, img_box_y, img_box_w, img_box_h, fill=QColor("#f4f6f8"), color=GRID, width=0.2)
            target = pen.r(img_box_x + 0.4, img_box_y + 0.4, img_box_w - 0.8, img_box_h - 0.8)
            scaled = img.scaled(int(target.width()), int(target.height()),
                                Qt.AspectRatioMode.KeepAspectRatio,
                                Qt.TransformationMode.SmoothTransformation)
            ox = target.x() + (target.width() - scaled.width()) / 2.0
            oy = target.y() + (target.height() - scaled.height()) / 2.0
            painter.drawImage(QPointF(ox, oy), scaled)
            del scaled
            del img
        else:
            # Undecodable image (HEIC, etc.): framed placeholder with file name
            pen.box(img_box_x, img_box_y, img_box_w, img_box_h, fill=LIGHT, color=ACCENT, width=0.35)
            pen.line(img_box_x, img_box_y, img_box_x + img_box_w, img_box_y + img_box_h, color=GRID, width=0.2)
            pen.line(img_box_x + img_box_w, img_box_y, img_box_x, img_box_y + img_box_h, color=GRID, width=0.2)
            fname = ph.get("file_name") or os.path.basename(ph.get("photo_path") or "") or ph.get("id") or "image"
            pen.box(img_box_x + 4, img_box_y + img_box_h / 2 - 7, img_box_w - 8, 14,
                    fill=QColor("white"), color=ACCENT, width=0.25)
            pen.text(img_box_x + 5, img_box_y + img_box_h / 2 - 6, img_box_w - 10, 6,
                     tr("Image preview unavailable"), 7.5, True, "center", ACCENT, rtl=rtl)
            pen.text(img_box_x + 5, img_box_y + img_box_h / 2, img_box_w - 10, 6,
                     _lri(fname), 7.2, False, "center", INK, rtl=False)

        if ph.get("preview"):
            badge_w = 24.0
            bx = (img_box_x + 1.5) if rtl else (img_box_x + img_box_w - badge_w - 1.5)
            pen.box(bx, img_box_y + 1.5, badge_w, 5.2, fill=WARN_BG, color=WARN_INK, width=0.25)
            pen.text(bx + 0.5, img_box_y + 1.5, badge_w - 1.0, 5.2, tr("Preview"), 6.8, True, "center", WARN_INK, rtl=rtl)

        # Caption area below image
        cap_y = cy + img_h + 0.5
        pen.line(cx, cap_y - 0.5, cx + cell_w, cap_y - 0.5, color=LIGHT, width=0.2)
        path_title = ph.get("path_label") or ""
        if ph.get("preview"):
            path_title = f"{path_title} ({tr('Preview')})"
        pen.text(cx + 2, cap_y, cell_w - 4, 5.2, path_title, 7.8, True, align, ACCENT, rtl=rtl)

        line_y = cap_y + 5.2
        time_coords = " · ".join(x for x in (_lri(ph["local_time"]) if ph.get("local_time") else "",
                                             ph.get("coords_text") or "") if x)
        if time_coords:
            pen.text(cx + 2, line_y, cell_w - 4, 4.6, time_coords, 6.8, False, align, MUTED, rtl=rtl)
            line_y += 4.6

        details = []
        if ph.get("answers_text"):
            details.append(ph["answers_text"])
        if ph.get("note"):
            details.append(f"{tr('Note')}: {ph['note']}")
        if details:
            rem_h = max(4.5, (cy + cell_h - 1.0) - line_y)
            pen.text(cx + 2, line_y, cell_w - 4, rem_h, "\n".join(details), 6.9, False, align, INK, rtl=rtl)

    _draw_footer(pen, page_no, total_pages, rtl=rtl)


def _draw_signatures_page(painter, pen, model, page_no, total_pages, rtl=True):
    x0, w = MARGIN, A4_W - 2 * MARGIN
    y = _draw_header_banner(pen, model, tr("Signatures and approval"), model.get("title") or "", rtl=rtl)
    align = "right" if rtl else "left"

    # Brief summary box before signature boxes
    pen.box(x0, y, w, 24, fill=LIGHT, color=ACCENT, width=0.35)
    surveyors_txt = "، ".join(model["surveyors"]) if (rtl and model.get("surveyors")) else (
        ", ".join(model.get("surveyors") or ()) or "—"
    )
    pen.text(x0 + 3, y + 2, w - 6, 6,
             f"{tr('Job title:')} {model.get('title') or '—'}  ·  {tr('Job ID')}: {_lri(model.get('job_id') or '—')}",
             9.5, True, align, INK, rtl=rtl)
    pen.text(x0 + 3, y + 9, w - 6, 6,
             f"{tr('Template:')} {model.get('template_title') or '—'}  ·  "
             f"{tr('Completion')}: {_lri(str(model.get('percent', 0)) + '%')}  ·  "
             f"{tr('Total photos: {n}').format(n=_lri(model['stats']['total_photos']))}",
             8.5, False, align, INK, rtl=rtl)
    pen.text(x0 + 3, y + 16, w - 6, 6,
             f"{tr('Surveyor')}: {surveyors_txt}  ·  {tr('Office')}: {model.get('office') or '—'}",
             8.5, False, align, MUTED, rtl=rtl)
    y += 32

    # Three signature / stamp boxes: Surveyor, Reviewer, Office stamp
    gap = 4.0
    box_w = (w - 2 * gap) / 3.0
    box_h = 65.0
    today_str = _lri(dt.date.today().isoformat())
    boxes = [
        (tr("Surveyor"), surveyors_txt),
        (tr("Reviewer"), ""),
        (tr("Office stamp"), model.get("office") or ""),
    ]
    for idx, (role_title, person_name) in enumerate(boxes):
        bx = (x0 + w - (idx + 1) * box_w - idx * gap) if rtl else (x0 + idx * (box_w + gap))
        pen.box(bx, y, box_w, box_h, color=ACCENT, width=0.35)
        pen.box(bx, y, box_w, 8.0, fill=LIGHT, color=ACCENT, width=0.3)
        pen.text(bx + 2, y + 1, box_w - 4, 6, role_title, 9.5, True, "center", ACCENT, rtl=rtl)
        if idx < 2:
            pen.text(bx + 3, y + 12, box_w - 6, 6, f"{tr('Name')}: {person_name}", 8.5, False, align, INK, rtl=rtl)
            pen.text(bx + 3, y + 26, box_w - 6, 6, f"{tr('Signature')}: ........................", 8.5, False, align, INK, rtl=rtl)
            pen.text(bx + 3, y + 40, box_w - 6, 6, f"{tr('Date')}: {today_str}", 8.5, False, align, INK, rtl=rtl)
        else:
            if person_name:
                pen.text(bx + 3, y + 12, box_w - 6, 6, person_name, 8.5, True, "center", INK, rtl=rtl)
            pen.text(bx + 3, y + 32, box_w - 6, 8, tr("Office stamp"), 9.0, False, "center", GRID, rtl=rtl)

    _draw_footer(pen, page_no, total_pages, rtl=rtl)


def plan_report_pages(model, map_image=None):
    """Return an ordered list of page descriptors `[{"kind": ..., ...}]` for `model`."""
    pages = [{"kind": "cover"}]
    pages.append({"kind": "map", "map_image": map_image})
    for blk in _build_stats_pages(model):
        pages.append({"kind": "stats", "blocks": blk})

    for sec in model.get("sections") or ():
        sec_photos = sec.get("photos") or []
        for i in range(0, len(sec_photos), PHOTOS_PER_PAGE):
            chunk = sec_photos[i:i + PHOTOS_PER_PAGE]
            pages.append({
                "kind": "photos",
                "section": sec["label"],
                "heading": f"{tr('Section')}: {sec['label']}",
                "photos": chunk,
            })

    extra_photos = model.get("extra_photos") or []
    for i in range(0, len(extra_photos), PHOTOS_PER_PAGE):
        chunk = extra_photos[i:i + PHOTOS_PER_PAGE]
        pages.append({
            "kind": "appendix",
            "section": tr("Appendix: Extra photos"),
            "heading": tr("Appendix: Extra photos"),
            "photos": chunk,
        })

    pages.append({"kind": "signatures"})
    return pages


def draw_report_page(painter, dpmm, model, page_spec, page_no=1, total_pages=1, rtl=None):
    """Draw a single planned page using QPainter (shared between QPdfWriter and QImage PNG rendering)."""
    if rtl is None:
        rtl = i18n.is_rtl()
    pen = _Pen(painter, dpmm)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    painter.setRenderHint(QPainter.RenderHint.TextAntialiasing, True)
    painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)

    kind = page_spec["kind"]
    if kind == "cover":
        _draw_cover_page(painter, pen, model, page_no, total_pages, rtl=rtl)
    elif kind == "map":
        _draw_map_page(painter, pen, model, page_spec.get("map_image"), page_no, total_pages, rtl=rtl)
    elif kind == "stats":
        _draw_stats_page(painter, pen, model, page_spec.get("blocks") or (), page_no, total_pages, rtl=rtl)
    elif kind in ("photos", "appendix"):
        _draw_photo_grid_page(painter, pen, model, page_spec.get("heading") or "",
                              page_spec.get("photos") or (), page_no, total_pages, rtl=rtl)
    elif kind == "signatures":
        _draw_signatures_page(painter, pen, model, page_no, total_pages, rtl=rtl)


def render_report_page_png(path, model, page_index=0, map_image=None, dpi=150, lang=None):
    """Render one page of the report through `draw_report_page` to a PNG image for inspection and tests."""
    prev_code = i18n._code
    if lang:
        i18n.init(lang)
    try:
        rtl = i18n.is_rtl()
        pages = plan_report_pages(model, map_image=map_image)
        idx = max(0, min(int(page_index), len(pages) - 1))
        dpmm = dpi / 25.4
        img = QImage(int(round(A4_W * dpmm)), int(round(A4_H * dpmm)), QImage.Format.Format_RGB32)
        img.setDotsPerMeterX(int(round(dpmm * 1000)))
        img.setDotsPerMeterY(int(round(dpmm * 1000)))
        img.fill(QColor("white"))
        painter = QPainter(img)
        try:
            draw_report_page(painter, dpmm, model, pages[idx], idx + 1, len(pages), rtl=rtl)
        finally:
            painter.end()
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        img.save(path)
        return path
    finally:
        if lang and prev_code != lang:
            i18n.init(prev_code)


def write_report(path, model, map_image=None, progress=None, lang=None):
    """Write the ordered capture job report to `path` as an A4 PDF and seal it with `seal_file(path, certificate=True)`.
    Returns `{"pdf": path, "pages": len(pages), "seal": sealed, "cancelled": False}`."""
    prev_code = i18n._code
    if lang:
        i18n.init(lang)
    try:
        rtl = i18n.is_rtl()
        pages = plan_report_pages(model, map_image=map_image)
        total = len(pages)
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)

        writer = QPdfWriter(path)
        writer.setPageSize(QPageSize(QPageSize.PageSizeId.A4))
        writer.setPageMargins(QMarginsF(0, 0, 0, 0))
        writer.setResolution(300)
        writer.setTitle(f"{tr('Capture Job Report')} — {model.get('title') or model.get('job_id') or ''}")
        writer.setCreator("Biruni GIS")

        painter = QPainter(writer)
        cancelled = False
        try:
            dpmm = writer.width() / A4_W
            for i, page_spec in enumerate(pages):
                if progress is not None:
                    res = progress(i, total)
                    if res is False:
                        cancelled = True
                        break
                if i > 0:
                    writer.newPage()
                draw_report_page(painter, dpmm, model, page_spec, i + 1, total, rtl=rtl)
            if not cancelled and progress is not None:
                if progress(total, total) is False:
                    cancelled = True
        finally:
            painter.end()

        if cancelled:
            try:
                if os.path.isfile(path):
                    os.remove(path)
            except OSError:
                pass
            return {"pdf": path, "pages": total, "seal": None, "cancelled": True}

        sealed = seal_file(path, certificate=True)
        return {"pdf": path, "pages": total, "seal": sealed, "cancelled": False}
    finally:
        if lang and prev_code != lang:
            i18n.init(prev_code)
