"""Clean what leaves the office: the leftovers a file carries besides its data.

scrub_file(path) rewrites a file Biruni sends out (GeoPackage, Shapefile set, DXF, KML/KMZ, CSV, XLSX, PDF) without
the things nobody meant to send: user names, folders of this computer, metadata rows, embedded QGIS projects, old
deleted pages, sidecar metadata files - and returns a receipt of what was removed. The data itself (features,
geometry, visible attribute values) is never changed: values that look like local paths are only reported.

Pure Python + sqlite3 + zipfile; no network, no new dependency."""
import getpass
import os
import re
import shutil
import sqlite3
import uuid
import zipfile

# a folder of a computer: C:\..., D:/..., \\server\share\..., file:///...
_PATH = re.compile(r"(?i)(?:file:/{2,3}|\b[a-z]:[\\/]|\\\\[^\\/\s\"'<>|]+[\\/])[^\"'<>|\r\n\t]*")


def _who():
    names = set()
    for v in (os.environ.get("USERNAME"), os.environ.get("USER")):
        if v and len(v) > 1:
            names.add(v)
    try:
        names.add(getpass.getuser())
    except Exception:  # noqa: BLE001
        pass
    prof = os.environ.get("USERPROFILE") or ""
    if prof:
        names.add(os.path.basename(prof.rstrip("\\/")))
    return {n for n in names if n and len(n) > 1}


class Receipt:
    """What was removed from one file: [(kind, count, preview)] and warnings (things seen, not changed)."""

    def __init__(self, path):
        self.path = path
        self.items = []
        self.warnings = []

    def add(self, kind, count=1, preview=""):
        if count:
            self.items.append({"kind": kind, "count": int(count), "preview": _short(preview)})

    def warn(self, text, preview=""):
        self.warnings.append({"text": text, "preview": _short(preview)})

    @property
    def removed(self):
        return sum(i["count"] for i in self.items)

    def kinds(self):
        return {i["kind"] for i in self.items}

    def as_text(self):
        lines = [os.path.basename(self.path)]
        if not self.items:
            lines.append("  nothing to remove")
        for i in self.items:
            lines.append(f"  - {i['kind']}: {i['count']}" + (f"  ({i['preview']})" if i["preview"] else ""))
        for w in self.warnings:
            lines.append(f"  ! {w['text']}" + (f"  ({w['preview']})" if w["preview"] else ""))
        return "\n".join(lines)


def _short(s, n=40):
    s = str(s or "").replace("\r", " ").replace("\n", " ")
    return s if len(s) <= n else s[:n - 1] + "…"


def _strip_path(s):
    """A local path -> its file name only (the folders of this computer go)."""
    s = s.rstrip("\\/")
    base = re.split(r"[\\/]", s)[-1]
    return base


def clean_text(text, rc=None, kind="paths of this computer", names=None):
    """Local paths in a text become file names; user names become '-'. -> new text"""
    n = [0]
    first = [""]

    def sub(m):
        n[0] += 1
        first[0] = first[0] or m.group(0)
        return _strip_path(m.group(0))

    out = _PATH.sub(sub, text)
    if rc is not None:
        rc.add(kind, n[0], first[0])
    for who in names or ():
        k = len(re.findall(re.escape(who), out, flags=re.I))
        if k:
            out = re.sub(re.escape(who), "-", out, flags=re.I)
            if rc is not None:
                rc.add("user names", k, "")
    return out


# ---- GeoPackage ------------------------------------------------------------------------------------------------
_GPKG_DROP = ("qgis_projects", "qgis_projects_attachments")


def _gpkg(path, rc, opt):
    names = opt["names"]
    con = sqlite3.connect(path)
    try:
        tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        for t in ("gpkg_metadata_reference", "gpkg_metadata"):
            if t in tables:
                n = con.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0]
                prev = ""
                if t == "gpkg_metadata" and n:
                    prev = (con.execute("SELECT metadata FROM gpkg_metadata LIMIT 1").fetchone()[0] or "")
                con.execute(f'DELETE FROM "{t}"')
                rc.add("metadata rows", n, prev)
        if "gpkg_extensions" in tables:
            con.execute("DELETE FROM gpkg_extensions WHERE table_name IN ('gpkg_metadata','gpkg_metadata_reference') "
                        "OR extension_name='gpkg_metadata'")
        for t in _GPKG_DROP:
            if t in tables:
                n = con.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0]
                con.execute(f'DROP TABLE "{t}"')
                rc.add("embedded QGIS projects", max(n, 1), t)
        if "layer_styles" in tables:
            cols = [r[1] for r in con.execute("PRAGMA table_info(layer_styles)")]
            rows = con.execute("SELECT rowid, * FROM layer_styles").fetchall()
            sub = Receipt(path)
            for row in rows:
                rec = dict(zip(["rowid"] + cols, row))
                upd = {}
                if rec.get("owner"):
                    upd["owner"] = ""
                    sub.add("user names", 1, "")
                for c in ("styleQML", "styleSLD", "description", "ui"):
                    v = rec.get(c)
                    if isinstance(v, str) and v:
                        nv = clean_text(v, sub, "paths in styles", names)
                        if nv != v:
                            upd[c] = nv
                if upd:
                    con.execute("UPDATE layer_styles SET " + ", ".join(f'"{k}"=?' for k in upd) + " WHERE rowid=?",
                                list(upd.values()) + [rec["rowid"]])
            rc.items.extend(sub.items)
        # descriptions of the contents: the folders of this computer go
        if "gpkg_contents" in tables:
            for tn, desc in con.execute("SELECT table_name, description FROM gpkg_contents").fetchall():
                if desc:
                    nd = clean_text(desc, rc, "paths in descriptions", names)
                    if nd != desc:
                        con.execute("UPDATE gpkg_contents SET description=? WHERE table_name=?", (nd, tn))
        for t in (opt.get("drop_tables") or ()):
            if t in tables:
                con.execute(f'DROP TABLE "{t}"')
                rc.add("tables not asked for", 1, t)
        _drop_fields_sqlite(con, opt.get("drop_fields") or {}, rc)
        _warn_values_sqlite(con, rc)
        con.commit()
        free = con.execute("PRAGMA freelist_count").fetchone()[0]
        con.execute("VACUUM")
        if free:
            rc.add("deleted pages (old data still inside the file)", free, "")
    finally:
        con.close()


def _drop_fields_sqlite(con, drop, rc):
    for table, fields in drop.items():
        cols = [r[1] for r in con.execute(f'PRAGMA table_info("{table}")')]
        for f in fields:
            if f in cols:
                con.execute(f'ALTER TABLE "{table}" DROP COLUMN "{f}"')
                rc.add("hidden fields", 1, f)


def _warn_values_sqlite(con, rc):
    feats = [r[0] for r in con.execute("SELECT table_name FROM gpkg_contents WHERE data_type IN ('features','attributes')")]
    n, first = 0, ""
    for t in feats:
        cols = [r[1] for r in con.execute(f'PRAGMA table_info("{t}")') if r[2].upper().startswith("TEXT")]
        for c in cols:
            for (v,) in con.execute(f'SELECT "{c}" FROM "{t}" WHERE "{c}" LIKE \'%:\\%\' OR "{c}" LIKE \'%:/%\' '
                                    f'OR "{c}" LIKE \'\\\\%\' LIMIT 50'):
                if isinstance(v, str) and _PATH.search(v):
                    n += 1
                    first = first or v
    if n:
        rc.warn(f"{n} attribute values look like paths of this computer (left as they are: they are data)", first)


# ---- Shapefile set ---------------------------------------------------------------------------------------------
_SHP_KEEP = {".shp", ".shx", ".dbf", ".prj", ".cpg", ".qix", ".sbn", ".sbx"}
_SHP_DROP = {".shp.xml", ".xml", ".qmd", ".aux.xml", ".shp.aux.xml"}


def _shp(path, rc, opt):
    folder, name = os.path.split(path)
    stem = os.path.splitext(name)[0]
    for f in sorted(os.listdir(folder or ".")):
        if not f.lower().startswith(stem.lower() + "."):
            continue
        ext = f[len(stem):].lower()
        full = os.path.join(folder, f)
        if ext in _SHP_KEEP:
            continue
        if ext in _SHP_DROP:
            os.remove(full)
            rc.add("metadata files beside the shapefile", 1, f)
        elif ext in (".qml", ".sld"):
            with open(full, encoding="utf-8", errors="replace") as fh:
                t = fh.read()
            nt = clean_text(t, rc, "paths in styles", opt["names"])
            if nt != t:
                with open(full, "w", encoding="utf-8") as fh:
                    fh.write(nt)
    drop = (opt.get("drop_fields") or {})
    fields = drop.get(stem) or drop.get("*") or []
    if fields:
        from osgeo import ogr
        ds = ogr.Open(path, 1)
        if ds is not None:
            lyr = ds.GetLayer(0)
            for f in fields:
                i = lyr.GetLayerDefn().GetFieldIndex(f)
                if i >= 0:
                    lyr.DeleteField(i)
                    rc.add("hidden fields", 1, f)
            ds = None


# ---- DXF -------------------------------------------------------------------------------------------------------
_DXF_HEADER_BLANK = {"$LASTSAVEDBY", "$PROJECTNAME", "$HYPERLINKBASE", "$STYLESHEET", "$AUTHOR"}
_DXF_HEADER_GUID = {"$FINGERPRINTGUID", "$VERSIONGUID"}
_DXF_TEXT_CODES = set(range(1, 10)) | {300, 301, 302, 303, 304, 305, 306, 307, 308, 309, 1000, 1001}


def _dxf(path, rc, opt):
    with open(path, "rb") as fh:
        raw = fh.read()
    nl = b"\r\n" if b"\r\n" in raw[:4096] else b"\n"
    enc = "utf-8"
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        enc = "cp1252"
        text = raw.decode(enc, errors="replace")
    lines = text.split(nl.decode())
    names = opt["names"]
    in_header, var = False, None
    paths, first = 0, ""
    for i in range(0, len(lines) - 1, 2):
        code_s, val = lines[i].strip(), lines[i + 1]
        try:
            code = int(code_s)
        except ValueError:
            continue
        if code == 2 and val.strip() == "HEADER":
            in_header = True
        elif code == 0 and val.strip() == "ENDSEC":
            in_header = False
        if in_header and code == 9:
            var = val.strip()
            continue
        if in_header and var in _DXF_HEADER_BLANK and code in (1, 3) and val.strip():
            rc.add("drawing properties (" + var + ")", 1, val)
            lines[i + 1] = ""
            continue
        if in_header and var in _DXF_HEADER_GUID and code == 2:
            lines[i + 1] = "{" + str(uuid.uuid4()).upper() + "}"
            rc.add("drawing tracking ids (" + var + ")", 1, "")
            continue
        if code in _DXF_TEXT_CODES and _PATH.search(val):
            nv = _PATH.sub(lambda m: _strip_path(m.group(0)), val)
            if nv != val:
                paths += 1
                first = first or val
                lines[i + 1] = nv
        if code in _DXF_TEXT_CODES and names:
            for who in names:
                if re.search(re.escape(who), lines[i + 1], flags=re.I) and code in (1000,):
                    lines[i + 1] = re.sub(re.escape(who), "-", lines[i + 1], flags=re.I)
                    rc.add("user names", 1, "")
    rc.add("paths of this computer", paths, first)
    with open(path, "wb") as fh:
        fh.write(nl.decode().join(lines).encode(enc, errors="replace"))


# ---- KML / KMZ -------------------------------------------------------------------------------------------------
_KML_AUTHOR = re.compile(r"<(atom:author|author)\b[^>]*>.*?</\1>", re.S | re.I)


def _kml_text(t, rc, opt):
    n = len(_KML_AUTHOR.findall(t))
    if n:
        prev = _KML_AUTHOR.search(t).group(0)
        t = _KML_AUTHOR.sub("", t)
        rc.add("author", n, re.sub(r"<[^>]+>", " ", prev).strip())
    t = clean_text(t, rc, "paths of this computer", opt["names"])
    for f in (opt.get("drop_fields") or {}).get("*", []):
        pat = re.compile(r"<(SimpleData|Data)\s+name=\"" + re.escape(f) + r"\"[^>]*>.*?</\1>", re.S)
        k = len(pat.findall(t))
        if k:
            t = pat.sub("", t)
            t = re.sub(r"<SimpleField\b[^>]*name=\"" + re.escape(f) + r"\"[^>]*>.*?</SimpleField>|"
                       r"<SimpleField\b[^>]*name=\"" + re.escape(f) + r"\"[^>]*/>", "", t, flags=re.S)
            rc.add("hidden fields", 1, f)
    return t


def _kml(path, rc, opt):
    with open(path, encoding="utf-8", errors="replace") as fh:
        t = fh.read()
    nt = _kml_text(t, rc, opt)
    if nt != t:
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(nt)


def _rezip(path, fn):
    """Rewrite a zip member by member: fn(name, bytes) -> bytes or None (drop)."""
    tmp = path + ".scrub.tmp"
    with zipfile.ZipFile(path) as zin, zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as zout:
        for info in zin.infolist():
            data = fn(info.filename, zin.read(info.filename))
            if data is None:
                continue
            zi = zipfile.ZipInfo(info.filename, date_time=(1980, 1, 1, 0, 0, 0))
            zi.compress_type = zipfile.ZIP_DEFLATED
            zi.external_attr = info.external_attr
            zout.writestr(zi, data)
    os.replace(tmp, path)


def _kmz(path, rc, opt):
    def fn(name, data):
        if name.lower().endswith(".kml"):
            t = data.decode("utf-8", errors="replace")
            return _kml_text(t, rc, opt).encode("utf-8")
        return data
    _rezip(path, fn)
    rc.add("file dates inside the archive", 1, "")


# ---- XLSX ------------------------------------------------------------------------------------------------------
_XL_TAGS = ("dc:creator", "cp:lastModifiedBy", "Company", "Manager", "cp:lastPrinted")


def _xlsx(path, rc, opt):
    def fn(name, data):
        low = name.lower()
        if low == "docprops/custom.xml":
            rc.add("custom document properties", 1, "")
            return None
        if low in ("docprops/core.xml", "docprops/app.xml"):
            t = data.decode("utf-8", errors="replace")
            for tag in _XL_TAGS:
                pat = re.compile(r"<" + tag + r"(\s[^>]*)?>([^<]*)</" + tag + ">")
                m = pat.search(t)
                if m and m.group(2).strip():
                    t = pat.sub("<" + tag + r"\1></" + tag + ">", t)
                    rc.add("document properties (" + tag.split(":")[-1] + ")", 1, m.group(2))
            return t.encode("utf-8")
        if low == "xl/workbook.xml":
            t = data.decode("utf-8", errors="replace")
            pat = re.compile(r"<x15ac:absPath\b[^>]*/>|<mc:AlternateContent\b[^>]*>\s*<mc:Choice[^>]*>\s*"
                             r"<x15ac:absPath\b[^>]*/>\s*</mc:Choice>\s*</mc:AlternateContent>")
            k = len(pat.findall(t))
            if k:
                t = pat.sub("", t)
                rc.add("the folder the workbook was saved in", k, "")
            return t.encode("utf-8")
        return data
    _rezip(path, fn)


# ---- CSV -------------------------------------------------------------------------------------------------------
def _csv(path, rc, opt):
    import csv
    fields = (opt.get("drop_fields") or {}).get("*", [])
    with open(path, encoding="utf-8-sig", newline="") as fh:
        rows = list(csv.reader(fh))
    if not rows:
        return
    head = rows[0]
    kill = [i for i, h in enumerate(head) if h in fields]
    if kill:
        rows = [[v for i, v in enumerate(r) if i not in kill] for r in rows]
        for i in kill:
            rc.add("hidden fields", 1, head[i])
        with open(path, "w", encoding="utf-8", newline="") as fh:
            csv.writer(fh).writerows(rows)
    n = sum(1 for r in rows[1:] for v in r if _PATH.search(v))
    if n:
        rc.warn(f"{n} values look like paths of this computer (left as they are: they are data)", "")


# ---- PDF -------------------------------------------------------------------------------------------------------
_PDF_INFO = (b"/Author", b"/Creator", b"/Producer", b"/Subject", b"/Keywords")
_XMP_TAGS = (b"dc:creator", b"xmp:CreatorTool", b"pdf:Producer", b"xmpMM:DocumentID", b"xmpMM:InstanceID",
             b"pdf:Keywords")


def _pdf(path, rc, opt):
    """The same bytes, same length: the values are blanked in place, so the cross-reference table stays right.
    Compressed (or encrypted) metadata streams cannot be read this way and are reported."""
    with open(path, "rb") as fh:
        b = bytearray(fh.read())
    for key in _PDF_INFO:
        for m in re.finditer(re.escape(key) + rb"\s*\(", bytes(b)):
            start, depth, i = m.end(), 1, m.end()
            while i < len(b) and depth:
                c = b[i]
                if c == 0x5C:
                    i += 2
                    continue
                if c == 0x28:
                    depth += 1
                elif c == 0x29:
                    depth -= 1
                i += 1
            end = i - 1
            if end > start:
                prev = bytes(b[start:end]).decode("latin-1", errors="replace")
                b[start:end] = b" " * (end - start)
                rc.add("document properties (" + key.decode()[1:] + ")", 1, prev)
        for m in re.finditer(re.escape(key) + rb"\s*<([0-9A-Fa-f\s]*)>", bytes(b)):
            hx = m.group(1)
            if len(hx.strip()) > 4:
                s, e = m.start(1), m.end(1)
                fill = (b"FEFF" + b"0020" * ((e - s - 4) // 4) + b"0" * ((e - s - 4) % 4)) if hx[:4].upper() == b"FEFF" \
                    else b"20" * ((e - s) // 2) + b"0" * ((e - s) % 2)
                b[s:e] = fill[: e - s]
                rc.add("document properties (" + key.decode()[1:] + ")", 1, "")
    xmp = 0
    for tag in _XMP_TAGS:
        pat = re.compile(rb"<" + re.escape(tag) + rb"(\s[^>]*)?>(.*?)</" + re.escape(tag) + rb">", re.S)
        for m in pat.finditer(bytes(b)):
            s, e = m.start(2), m.end(2)
            inner = bytes(b[s:e])
            blank = re.sub(rb">[^<]+<", lambda q: b">" + b" " * (len(q.group(0)) - 2) + b"<", inner)
            blank = re.sub(rb"^[^<]+", lambda q: b" " * len(q.group(0)), blank)
            blank = re.sub(rb"[^>]+$", lambda q: b" " * len(q.group(0)), blank)
            if blank != inner:
                b[s:e] = blank
                xmp += 1
    rc.add("XMP metadata values", xmp, "")
    if re.search(rb"/Type\s*/Metadata\s*/Subtype\s*/XML[^>]*/Filter", bytes(b)) or re.search(
            rb"/Filter\s*/FlateDecode[^>]*/Type\s*/Metadata", bytes(b)):
        rc.warn("a compressed XMP metadata stream could not be cleaned", "")
    if b"/Encrypt" in b:
        rc.warn("the PDF is encrypted: its properties could not be cleaned", "")
    for who in opt["names"]:
        if who.encode("utf-8", errors="ignore") and re.search(re.escape(who.encode("utf-8")), bytes(b), flags=re.I):
            rc.warn("the user name still appears in the PDF (inside page content or a compressed part)", who[:2] + "…")
    with open(path, "wb") as fh:
        fh.write(bytes(b))


# ---- GeoJSON ---------------------------------------------------------------------------------------------------
def _geojson(path, rc, opt):
    import json
    with open(path, encoding="utf-8") as fh:
        d = json.load(fh)
    fields = (opt.get("drop_fields") or {}).get("*", [])
    k = 0
    for f in d.get("features") or []:
        p = f.get("properties") or {}
        for x in fields:
            if x in p:
                del p[x]
                k += 1
    if k:
        for x in fields:
            rc.add("hidden fields", 1, x)
    if "description" in d and isinstance(d["description"], str):
        d["description"] = clean_text(d["description"], rc, "paths in descriptions", opt["names"])
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(d, fh, ensure_ascii=False)


HANDLERS = {".gpkg": _gpkg, ".shp": _shp, ".dxf": _dxf, ".kml": _kml, ".kmz": _kmz, ".xlsx": _xlsx, ".csv": _csv,
            ".pdf": _pdf, ".geojson": _geojson, ".json": _geojson}


def supported(path):
    return os.path.splitext(path)[1].lower() in HANDLERS


def scrub_file(path, out_path=None, options=None):
    """Clean one file (in place, or into out_path). options: names (extra user names to remove), drop_fields
    ({layer or file stem or "*": [fields]}), drop_tables ([gpkg tables]). -> Receipt"""
    opt = dict(options or {})
    opt["names"] = sorted(set(opt.get("names") or ()) | _who(), key=len, reverse=True)
    ext = os.path.splitext(path)[1].lower()
    fn = HANDLERS.get(ext)
    target = out_path or path
    rc = Receipt(target)
    if fn is None:
        rc.warn("this kind of file is not cleaned", ext)
        if out_path and out_path != path:
            shutil.copy2(path, out_path)
        return rc
    if out_path and os.path.abspath(out_path) != os.path.abspath(path):
        if ext == ".shp":
            folder, name = os.path.split(path)
            stem = os.path.splitext(name)[0]
            ostem = os.path.splitext(os.path.basename(out_path))[0]
            for f in os.listdir(folder or "."):
                if f.lower().startswith(stem.lower() + "."):
                    shutil.copy2(os.path.join(folder, f), os.path.join(os.path.dirname(out_path) or ".", ostem + f[len(stem):]))
            dk = opt.get("drop_fields") or {}
            if stem in dk:
                dk = dict(dk)
                dk[ostem] = dk[stem]
                opt["drop_fields"] = dk
        else:
            shutil.copy2(path, out_path)
    fn(target, rc, opt)
    return rc
