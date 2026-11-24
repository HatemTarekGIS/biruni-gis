"""Office archive builder for Biruni capture jobs (Phase P7).

Creates and updates the local office archive for a job on job close or via the "Build archive" button:
  - Archive root: dock setting `biruni/field-photos/archive-root`, defaulting to `<photo folder>\\أرشيف بيروني`.
  - Job folder: `<job no> - <title>` (Windows-safe, trimmed, paths under 240 chars).
  - Section subfolders in tree order: `01 <section>\\`, with repeat instance subfolders using their title answer,
    e.g. `05 الطابق\\الطابق الأرضي\\شقة 101\\` (or `03 الطابق الأرضي\\شقة 101\\`).
  - Photos are COPIED as `<job code>_<item path short>_NN.jpg` (originals stay where they are),
    re-hashed from disk, verified against the original SHA-256, and then marked `archive` in CopyLedger.
  - Extra photos go in `99 صور إضافية`.
  - Rebuilds are idempotent: unchanged files are not recopied, and newly arrived photos are added.
  - The sealed PDF report (`capture_report`) and `<job no>.xlsx` + `فهرس الأرشيف.xlsx` (`archive_xlsx`) are written.
"""
import os
import re
import time

from qgis.core import QgsSettings

from ..i18n import tr
from .capture import clean_cap, clean_schema, schema_key, tree
from .copies import CopyLedger, sha256_file

ARCHIVE_SETTING_KEY = "biruni/field-photos/archive-root"
DEFAULT_ARCHIVE_DIRNAME = "أرشيف بيروني"
EXTRA_PHOTOS_DIRNAME = "99 صور إضافية"
INDEX_FILENAME = "فهرس الأرشيف.xlsx"
MAX_ARCHIVE_PATH = 235

_WIN_UNSAFE_RE = re.compile(r'[<>:"/\\|?*\x00-\x1f]+')
_WS_RE = re.compile(r"\s+")
_WIN_RESERVED = {
    "CON", "PRN", "AUX", "NUL",
    "COM1", "COM2", "COM3", "COM4", "COM5", "COM6", "COM7", "COM8", "COM9",
    "LPT1", "LPT2", "LPT3", "LPT4", "LPT5", "LPT6", "LPT7", "LPT8", "LPT9",
}


def safe_win_name(text, fallback="بند", max_len=64):
    """Strip Windows-unsafe characters (`<>:\"/\\|?*` and controls), collapse spaces, trim dots/spaces."""
    s = _WIN_UNSAFE_RE.sub(" ", str(text or ""))
    s = _WS_RE.sub(" ", s).strip().strip(". ")
    if not s:
        s = fallback
    stem = s.split(".")[0].upper()
    if stem in _WIN_RESERVED:
        s = f"_{s}"
    if max_len and len(s) > max_len:
        s = s[:max_len].rstrip(". ")
    return s or fallback


def safe_code_token(text, fallback="item", max_len=36):
    """Compact Windows-safe token for `<job code>_<item path short>_NN.jpg` filenames."""
    s = safe_win_name(text, fallback=fallback, max_len=max_len * 2)
    s = re.sub(r"[\s\-–—›/\\]+", "-", s).strip("-._ ")
    if not s:
        s = fallback
    if max_len and len(s) > max_len:
        s = s[:max_len].rstrip("-._ ")
    return s or fallback


def default_archive_root(photo_folder):
    """Default archive root `<photo folder>\\أرشيف بيروني`."""
    base = str(photo_folder or "").strip()
    return os.path.join(base, DEFAULT_ARCHIVE_DIRNAME) if base else DEFAULT_ARCHIVE_DIRNAME


def get_archive_root(photo_folder=""):
    """Return the configured archive root from QgsSettings, or `<photo folder>\\أرشيف بيروني` when unset."""
    configured = str(QgsSettings().value(ARCHIVE_SETTING_KEY, "") or "").strip()
    if configured:
        return configured
    return default_archive_root(photo_folder)


def set_archive_root(path):
    """Save the archive root in QgsSettings (empty string resets to default)."""
    p = str(path or "").strip()
    QgsSettings().setValue(ARCHIVE_SETTING_KEY, p)
    return p


def job_number_and_title(job, merged_state=None):
    """Extract `(job_no, title)` for a job record."""
    job = job if isinstance(job, dict) else {}
    job_id = str(job.get("id") or (merged_state or {}).get("job") or "JOB").strip()
    data = job.get("data") if isinstance(job.get("data"), dict) else {}
    raw_no = data.get("job_no")
    job_no = str(raw_no).strip() if raw_no not in (None, "") else ""
    title = str(job.get("title") or "").strip()
    if not job_no:
        job_no = title or job_id
    if not title:
        title = str(data.get("client") or job_no or job_id).strip()
    return job_no, title


def job_folder_name(job, merged_state=None, max_len=80):
    """Build Windows-safe `<job no> - <title>` folder name."""
    job_no, title = job_number_and_title(job, merged_state)
    safe_no = safe_win_name(job_no, fallback=str(job.get("id") or "JOB"), max_len=36)
    safe_title = safe_win_name(title, fallback=safe_no, max_len=max_len)
    if safe_title and safe_title != safe_no:
        combined = f"{safe_no} - {safe_title}"
    else:
        combined = safe_no
    return safe_win_name(combined, fallback=safe_no, max_len=max_len)


def _clamp_rel_dir(archive_root, job_dir_name, rel_parts, filename_budget=52):
    """Ensure `os.path.join(archive_root, job_dir_name, *rel_parts, <file>)` stays under 240 characters."""
    root_abs = os.path.abspath(archive_root)
    jname = safe_win_name(job_dir_name, fallback="JOB", max_len=70)
    parts = [safe_win_name(p, fallback="بند", max_len=48) for p in rel_parts if p]

    # Budget available for jname + parts
    avail = MAX_ARCHIVE_PATH - len(root_abs) - filename_budget - (len(parts) + 2)
    if avail < 24:
        avail = 24
    total_cur = len(jname) + sum(len(p) for p in parts)
    if total_cur > avail and parts:
        per_part = max(8, (avail - min(len(jname), 28)) // max(1, len(parts)))
        jname = safe_win_name(jname, fallback="JOB", max_len=max(12, min(len(jname), 32)))
        parts = [safe_win_name(p, fallback="بند", max_len=per_part) for p in parts]
    return jname, parts


def _resolve_original_photo(rec, folder):
    p = rec.get("photo") or ""
    if p and os.path.isabs(p) and os.path.isfile(p):
        return p
    if p and folder:
        cand = os.path.join(folder, p)
        if os.path.isfile(cand):
            return cand
    if p and os.path.isfile(p):
        return os.path.abspath(p)
    rid = rec.get("id") or ""
    if folder and rid:
        for ext in ("jpg", "jpeg", "png", "webp", "heic", "heif"):
            cand = os.path.join(folder, f"{rid}.{ext}")
            if os.path.isfile(cand):
                return cand
    return ""


def _copy_verified(src_path, dst_path, expected_sha=""):
    """Copy `src_path` to `dst_path` atomically (if needed), re-hash from disk, and verify against `expected_sha`.

    Idempotent on rebuild: if `dst_path` already exists with matching size and SHA-256, it is not recopied.
    Returns `(copied_now: bool, sha_hex: str)`.
    """
    src_sha = expected_sha or sha256_file(src_path)
    src_size = os.path.getsize(src_path)
    os.makedirs(os.path.dirname(os.path.abspath(dst_path)), exist_ok=True)

    if os.path.isfile(dst_path):
        try:
            if os.path.getsize(dst_path) == src_size:
                existing_sha = sha256_file(dst_path)
                if existing_sha == src_sha:
                    return False, existing_sha
        except OSError:
            pass

    part = dst_path + ".part"
    with open(src_path, "rb") as rf, open(part, "wb") as wf:
        while True:
            buf = rf.read(1 << 20)
            if not buf:
                break
            wf.write(buf)
        wf.flush()
        os.fsync(wf.fileno())
    os.replace(part, dst_path)

    dst_sha = sha256_file(dst_path)
    if dst_sha != src_sha:
        try:
            os.remove(dst_path)
        except OSError:
            pass
        raise IOError(f"archive copy hash mismatch for {dst_path}: expected {src_sha}, got {dst_sha}")
    return True, dst_sha


def _find_or_build_sealed_pdf(job_dir, store_folder, job, model, iface=None, pdf_source_path=None):
    """Copy an existing sealed PDF report for the job into `job_dir`, or generate and seal one there."""
    from ..seal import core as seal_core
    from .capture_report import render_location_map, write_report

    job_no, _title = job_number_and_title(job)
    safe_no = safe_win_name(job_no, fallback=job.get("id") or "report", max_len=40)
    dst_pdf = os.path.join(job_dir, f"{safe_no}.pdf")

    # 1. Check if caller supplied a pre-built sealed PDF or if one exists in jobs/<job_id>/
    cand_pdfs = []
    if pdf_source_path and os.path.isfile(pdf_source_path):
        cand_pdfs.append(pdf_source_path)
    jdir = os.path.join(store_folder, "jobs", str(job.get("id") or ""))
    if os.path.isdir(jdir):
        pdfs = [
            os.path.join(jdir, fn)
            for fn in os.listdir(jdir)
            if fn.lower().endswith(".pdf") and not fn.lower().endswith(seal_core.CERT_EXT)
        ]
        pdfs.sort(key=lambda p: os.path.getmtime(p), reverse=True)
        cand_pdfs.extend(pdfs)

    for src_pdf in cand_pdfs:
        if os.path.isfile(src_pdf + seal_core.SEAL_EXT):
            _copy_verified(src_pdf, dst_pdf)
            for ext in (seal_core.SEAL_EXT, seal_core.QR_EXT, seal_core.CERT_EXT):
                if os.path.isfile(src_pdf + ext):
                    _copy_verified(src_pdf + ext, dst_pdf + ext)
            return dst_pdf

    # 2. If dst_pdf already exists and is sealed and unchanged, keep it if no new photos were copied
    if os.path.isfile(dst_pdf) and os.path.isfile(dst_pdf + seal_core.SEAL_EXT):
        try:
            st, _det = seal_core.check_file(dst_pdf)
            if st == seal_core.UNCHANGED:
                return dst_pdf
        except Exception:  # noqa: BLE001
            pass

    # 3. Generate and seal the PDF directly inside the archive job folder
    map_img = render_location_map(model, iface=iface)
    write_report(dst_pdf, model, map_image=map_img)
    return dst_pdf


def build_job_archive(
    store,
    job_id,
    records=None,
    archive_root=None,
    ledger=None,
    iface=None,
    pdf_source_path=None,
):
    """Build or update the office archive for `job_id`.

    Returns a dictionary summarizing the archive location, copied/skipped photo counts, Excel paths,
    PDF path, and photo rows.
    """
    from .archive_xlsx import update_global_index, write_job_xlsx
    from .capture_jobs import resolve_job_schema
    from .capture_report import report_model
    from .server import read_log

    store_folder = store.folder
    if records is None:
        records = read_log(store_folder)
    else:
        records = list(records)

    if ledger is None:
        ledger = CopyLedger(store_folder)

    if not archive_root:
        archive_root = get_archive_root(store_folder)
    os.makedirs(archive_root, exist_ok=True)

    store.reload()
    job = store.get(job_id)
    merged = store.merged_state(job_id)
    schema, _is_unk = resolve_job_schema(store, job_id, merged, records)
    if job is None:
        job = {
            "id": job_id,
            "title": f"{tr('Unknown job')} ({job_id})",
            "schema": schema,
            "s": schema_key(schema),
            "data": {},
            "st": "open",
        }
    else:
        job = dict(job, schema=clean_schema(schema))

    job_no, title = job_number_and_title(job, merged)
    job_code = safe_code_token(job_no, fallback=job["id"], max_len=24)
    raw_job_dir_name = job_folder_name(job, merged)
    clamped_job_dir_name, _ = _clamp_rel_dir(archive_root, raw_job_dir_name, [])
    job_dir = os.path.join(archive_root, clamped_job_dir_name)
    os.makedirs(job_dir, exist_ok=True)

    st_in = dict(merged) if isinstance(merged, dict) else {}
    if job_id and not st_in.get("job"):
        st_in["job"] = job_id
    t = tree(job["schema"], st_in, records)
    model = report_model(job, st_in, records, folder=store_folder)

    # Index photo entries from report_model by photo_id for rich metadata (answers, coords, local_time)
    model_photo_by_id = {}
    for p in list(model.get("photos") or ()) + list(model.get("extra_photos") or ()):
        if p.get("id"):
            model_photo_by_id[p["id"]] = p

    archived_photos = []
    copied_count = 0
    reused_count = 0

    def _archive_slot_photos(slot_item, sec_folder_name, instance_folders):
        nonlocal copied_count, reused_count
        recs = slot_item.get("records") or []
        if not recs:
            return
        rel_parts = [sec_folder_name] + list(instance_folders)
        _jname, safe_parts = _clamp_rel_dir(archive_root, clamped_job_dir_name, rel_parts, filename_budget=54)
        target_dir = os.path.join(job_dir, *safe_parts)
        os.makedirs(target_dir, exist_ok=True)

        path_tokens = [safe_code_token(p, fallback="item", max_len=16) for p in (slot_item.get("path") or ())]
        if len(path_tokens) > 1:
            short_parts = path_tokens[1:]
        else:
            short_parts = path_tokens or [safe_code_token(slot_item.get("id") or "item", max_len=16)]
        item_path_short = safe_code_token("_".join(short_parts), fallback=slot_item.get("id") or "item", max_len=32)

        for idx, rec in enumerate(recs, 1):
            pid = rec.get("id") or ""
            src_path = _resolve_original_photo(rec, store_folder)
            if not src_path or not os.path.isfile(src_path):
                continue
            orig_sha = sha256_file(src_path)
            ssha = str(rec.get("ssha") or "")

            # Ensure filename + path stays well under 240 chars
            base_prefix = f"{job_code}_{item_path_short}"
            max_prefix_len = max(12, MAX_ARCHIVE_PATH - len(os.path.abspath(target_dir)) - 10)
            if len(base_prefix) > max_prefix_len:
                base_prefix = base_prefix[:max_prefix_len].rstrip("_-.")
            arch_filename = f"{base_prefix}_{idx:02d}.jpg"
            dst_path = os.path.join(target_dir, arch_filename)

            copied_now, verified_sha = _copy_verified(src_path, dst_path, expected_sha=orig_sha)
            if copied_now:
                copied_count += 1
            else:
                reused_count += 1

            # Mark CopyLedger 'archive' ONLY after the copy is re-hashed and verified
            if pid:
                ledger.mark(pid, "archive", sha=verified_sha, ssha=ssha)

            mp = model_photo_by_id.get(pid) or {}
            rel_path = os.path.relpath(dst_path, job_dir)
            archived_photos.append({
                "id": pid,
                "k": slot_item.get("k") or "",
                "section": sec_folder_name,
                "archive_name": arch_filename,
                "archive_rel_path": rel_path.replace("\\", "/"),
                "archive_path": dst_path,
                "original_path": src_path,
                "sha": verified_sha,
                "orig_sha": orig_sha,
                "path_label": slot_item.get("path_label") or mp.get("path_label") or "",
                "lat": rec.get("lat") if rec.get("lat") is not None else mp.get("lat"),
                "lon": rec.get("lon") if rec.get("lon") is not None else mp.get("lon"),
                "acc": rec.get("acc") if rec.get("acc") is not None else mp.get("acc"),
                "time": mp.get("local_time") or str(rec.get("taken") or ""),
                "note": mp.get("note") or rec.get("note") or slot_item.get("note") or "",
                "answers": mp.get("answers_text") or "",
                "surveyor": mp.get("surveyor") or rec.get("surveyor") or rec.get("by") or "",
                "extra": False,
            })

    def _walk_node(node, sec_folder_name, instance_folders):
        if node["kind"] == "repeat":
            for inst_it in node.get("instances") or ():
                inst_folder = safe_win_name(inst_it.get("label") or f"{node['label']} {inst_it.get('order') or 1}", max_len=40)
                new_inst_folders = list(instance_folders) + [inst_folder]
                _archive_slot_photos(inst_it, sec_folder_name, new_inst_folders)
                for ch in inst_it.get("children") or ():
                    _walk_node(ch, sec_folder_name, new_inst_folders)
        else:
            _archive_slot_photos(node, sec_folder_name, instance_folders)
            for ch in node.get("children") or ():
                _walk_node(ch, sec_folder_name, instance_folders)

    # Walk sections in exact tree order: `01 <section>\`, `02 <section>\`, ...
    for sec_idx, root_node in enumerate(t["roots"], 1):
        sec_title = safe_win_name(root_node["label"], fallback=root_node["id"], max_len=42)
        sec_folder_name = f"{sec_idx:02d} {sec_title}"
        sec_dir = os.path.join(job_dir, sec_folder_name)
        os.makedirs(sec_dir, exist_ok=True)
        _walk_node(root_node, sec_folder_name, [])

    # Extra photos (`cap.k == "*"`) go in `99 صور إضافية`
    extra_recs = t.get("extra_records") or []
    if extra_recs:
        extra_dir = os.path.join(job_dir, EXTRA_PHOTOS_DIRNAME)
        os.makedirs(extra_dir, exist_ok=True)
        extra_short = safe_code_token("extra", fallback="extra", max_len=16)
        for idx, rec in enumerate(extra_recs, 1):
            pid = rec.get("id") or ""
            src_path = _resolve_original_photo(rec, store_folder)
            if not src_path or not os.path.isfile(src_path):
                continue
            orig_sha = sha256_file(src_path)
            ssha = str(rec.get("ssha") or "")
            arch_filename = f"{job_code}_{extra_short}_{idx:02d}.jpg"
            dst_path = os.path.join(extra_dir, arch_filename)

            copied_now, verified_sha = _copy_verified(src_path, dst_path, expected_sha=orig_sha)
            if copied_now:
                copied_count += 1
            else:
                reused_count += 1

            if pid:
                ledger.mark(pid, "archive", sha=verified_sha, ssha=ssha)

            mp = model_photo_by_id.get(pid) or {}
            rel_path = os.path.relpath(dst_path, job_dir)
            archived_photos.append({
                "id": pid,
                "k": "*",
                "section": EXTRA_PHOTOS_DIRNAME,
                "archive_name": arch_filename,
                "archive_rel_path": rel_path.replace("\\", "/"),
                "archive_path": dst_path,
                "original_path": src_path,
                "sha": verified_sha,
                "orig_sha": orig_sha,
                "path_label": tr("Extra photo for this job"),
                "lat": rec.get("lat") if rec.get("lat") is not None else mp.get("lat"),
                "lon": rec.get("lon") if rec.get("lon") is not None else mp.get("lon"),
                "acc": rec.get("acc") if rec.get("acc") is not None else mp.get("acc"),
                "time": mp.get("local_time") or str(rec.get("taken") or ""),
                "note": mp.get("note") or rec.get("note") or "",
                "answers": "",
                "surveyor": mp.get("surveyor") or rec.get("surveyor") or rec.get("by") or "",
                "extra": True,
            })

    # Copy or build the sealed PDF report
    pdf_path = _find_or_build_sealed_pdf(
        job_dir, store_folder, job, model, iface=iface, pdf_source_path=pdf_source_path
    )

    # Write `<job no>.xlsx` and atomically update `فهرس الأرشيف.xlsx`
    safe_xlsx_stem = safe_win_name(job_no, fallback=job["id"], max_len=48)
    xlsx_path = os.path.join(job_dir, f"{safe_xlsx_stem}.xlsx")
    write_job_xlsx(xlsx_path, job, model, archived_photos)

    index_path = os.path.join(archive_root, INDEX_FILENAME)
    index_row = update_global_index(index_path, job, model, job_dir)

    return {
        "job_id": job["id"],
        "job_no": job_no,
        "title": title,
        "archive_root": archive_root,
        "job_dir": job_dir,
        "xlsx_path": xlsx_path,
        "pdf_path": pdf_path,
        "index_path": index_path,
        "index_row": index_row,
        "photos": archived_photos,
        "copied_count": copied_count,
        "reused_count": reused_count,
        "built_at": int(time.time() * 1000),
    }
