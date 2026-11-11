"""DWG to DXF and back, with LibreDWG's command-line tools.

Reading: dwg2dxf turns the drawing (AutoCAD R13 to 2018 files: AC1012 to AC1032) into a DXF, which then goes
through Biruni's own DXF reading (CAD to GIS): arcs, blocks and their attributes, Arabic texts in Unicode and in
the Arabic keyboard SHX fonts (xarb) come out as from a DXF saved by AutoCAD.

Writing: dxf2dwg writes AutoCAD 2000 (AC1015) drawings only - LibreDWG cannot write newer ones yet; every
AutoCAD since 2000 opens them. An AutoCAD 2000 drawing keeps its texts in a code page, not in Unicode, so the DXF
is first put in the code page that holds its texts (Arabic: Windows-1256); a character no code page holds is
written as \\U+XXXX, which AutoCAD shows.
"""
import os
import re
import tempfile

from .. import external_tools as xt
from .. import text_encoding as te

TOOL = "libredwg"
READ_TIMEOUT = 600
WRITE_TIMEOUT = 900


class DwgError(Exception):
    pass


def available():
    return xt.exe(TOOL, "dwg2dxf.exe") is not None


def version_of(path):
    """The AutoCAD version written at the start of a DWG ("AC1032"), or "" when it is not one."""
    try:
        with open(path, "rb") as f:
            head = f.read(6)
    except OSError:
        return ""
    return head.decode("ascii", "replace") if re.match(rb"AC\d{4}", head) else ""


NAMES = {"AC1012": "R13", "AC1014": "R14", "AC1015": "2000", "AC1018": "2004", "AC1021": "2007",
         "AC1024": "2010", "AC1027": "2013", "AC1032": "2018"}


def _lines(out):
    return "\n".join(l for l in out.splitlines() if l.strip() and not l.startswith("Warning"))[-800:]


def to_dxf(path, out=None, feedback=None):
    """The DXF of a DWG (written to ``out``, or beside it in a new temporary folder). Returns its path."""
    exe = xt.exe(TOOL, "dwg2dxf.exe")
    if exe is None:
        raise DwgError("missing")
    ver = version_of(path)
    if not ver:
        raise DwgError("not dwg")
    if ver < "AC1012":
        raise DwgError("too old")
    if out is None:
        out = os.path.join(tempfile.mkdtemp(prefix="biruni_dwg_"),
                           os.path.splitext(os.path.basename(path))[0] + ".dxf")
    code, text = xt.run([exe, "-y", "-o", out, path], cwd=os.path.dirname(exe), feedback=feedback,
                        timeout=READ_TIMEOUT)
    if code is None:
        raise DwgError("cancelled")
    if code != 0 or not os.path.isfile(out) or os.path.getsize(out) < 100:
        raise DwgError("failed: " + _lines(text))
    return out


_SUB = {"VERTEX", "SEQEND", "ATTRIB"}     # owned by the entity before them, not by a block


def own_entities(text):
    """Every entity with its owner (group 330): the block record of *Model_Space (or *Paper_Space, or the block it
    is drawn in). Some programs - the DXF export of the engine among them - leave it out, and LibreDWG then drops
    the entities from the DWG it writes. Returns (text, entities given an owner)."""
    nl = "\r\n" if "\r\n" in text else "\n"
    lines = text.split(nl)
    pairs = [(lines[i].strip(), lines[i + 1]) for i in range(0, len(lines) - 1, 2)]
    records, section, i = {}, None, 0
    while i < len(pairs):                       # the block records: name -> handle
        c, v = pairs[i]
        if c == "0" and v.strip() == "BLOCK_RECORD":
            h = n = None
            j = i + 1
            while j < len(pairs) and pairs[j][0] != "0":
                if pairs[j][0] == "5":
                    h = pairs[j][1].strip()
                elif pairs[j][0] == "2":
                    n = pairs[j][1].strip()
                j += 1
            if h and n:
                records[n.upper()] = h
            i = j
            continue
        i += 1
    model, paper = records.get("*MODEL_SPACE"), records.get("*PAPER_SPACE")
    if not model:
        return text, 0
    out, block, given, i = [], None, 0, 0
    while i < len(pairs):
        c, v = pairs[i]
        if c == "0" and v.strip() == "SECTION" and i + 1 < len(pairs):
            section = pairs[i + 1][1].strip()
        if c == "0" and section in ("ENTITIES", "BLOCKS") and v.strip() not in ("SECTION", "ENDSEC", "ENDBLK"):
            kind = v.strip()
            j = i + 1
            while j < len(pairs) and pairs[j][0] != "0":
                j += 1
            body = pairs[i + 1:j]
            if kind == "BLOCK":
                block = next((b.strip() for a, b in body if a == "2"), None)
            if kind not in _SUB and kind != "BLOCK" and not any(a == "330" for a, _b in body):
                if section == "BLOCKS":
                    owner = records.get((block or "").upper())
                else:
                    owner = paper if any(a == "67" and b.strip() == "1" for a, b in body) and paper else model
                if owner:
                    at = next((k + 1 for k, (a, _b) in enumerate(body) if a == "5"), 0)
                    body = body[:at] + [("330", owner)] + body[at:]
                    given += 1
            out.append((c, v))
            out.extend(body)
            i = j
            continue
        out.append((c, v))
        i += 1
    tail = [lines[-1]] if len(lines) % 2 else []
    return nl.join([x for a, b in out for x in (a.rjust(3) if a.isdigit() else a, b)] + tail), given


def prepare_dxf(path_in, path_out):
    """A DXF of the same drawing that dxf2dwg writes faithfully to AutoCAD 2000: $ACADVER AC1015, its texts in
    the code page that holds them, every entity with its owner. Returns (code page written, characters escaped)."""
    with open(path_in, "rb") as f:
        raw = f.read()
    ver, cp = te._dxf_header(raw)
    if te._dxf_utf8_version(ver):
        text = raw.decode("utf-8", "replace")
    else:
        _texts, _cp, real = te.dxf_texts(path_in)
        text = raw.decode(real or te.codec(cp) or "cp1252", "replace")
    text = te._unescape(text) if "\\U+" in text else text
    out_enc = "cp1252"
    for enc in ["cp1256", te.codec(cp) or "cp1252"] + [c for c in te.DXF_CODEPAGES if c != "utf-8"]:
        try:
            text.encode(enc)
            out_enc = enc
            break
        except (UnicodeEncodeError, LookupError):
            continue
    else:
        out_enc = "cp1256" if re.search("[؀-ۿ]", text) else (te.codec(cp) or "cp1252")
    name = te.DXF_CODEPAGES.get(out_enc, "ANSI_1252")
    text = re.sub(r"(\$ACADVER\s*\r?\n\s*1\s*\r?\n)AC\d{4}", r"\g<1>AC1015", text, count=1)
    if re.search(r"\$DWGCODEPAGE\s*\r?\n\s*3\s*\r?\n", text):
        text = re.sub(r"(\$DWGCODEPAGE\s*\r?\n\s*3\s*\r?\n)[^\r\n]*", lambda m: m.group(1) + name, text, count=1)
    else:
        nl = "\r\n" if "\r\n" in text else "\n"
        text = re.sub(r"(\$ACADVER\s*\r?\n\s*1\s*\r?\nAC1015)",
                      lambda m: m.group(1) + f"{nl}  9{nl}$DWGCODEPAGE{nl}  3{nl}{name}", text, count=1)
    text, _given = own_entities(text)
    escaped = 0
    buf = []
    for ch in text:
        try:
            buf.append(ch.encode(out_enc))
        except UnicodeEncodeError:
            buf.append(f"\\U+{ord(ch):04X}".encode("ascii"))
            escaped += 1
    with open(path_out, "wb") as f:
        f.write(b"".join(buf))
    return name, escaped


def to_dwg(dxf_path, out, feedback=None):
    """Write the DXF as an AutoCAD 2000 DWG at ``out``. Returns {"codepage", "escaped"}."""
    exe = xt.exe(TOOL, "dxf2dwg.exe")
    if exe is None:
        raise DwgError("missing")
    tmp = os.path.join(tempfile.mkdtemp(prefix="biruni_dwg_"), "drawing.dxf")
    cp, escaped = prepare_dxf(dxf_path, tmp)
    if os.path.exists(out):
        os.remove(out)
    code, text = xt.run([exe, "-y", "--as", "r2000", "-o", out, tmp], cwd=os.path.dirname(exe), feedback=feedback,
                        timeout=WRITE_TIMEOUT)
    if code is None:
        if os.path.exists(out):
            os.remove(out)
        raise DwgError("cancelled")
    if code != 0 or version_of(out) != "AC1015":
        raise DwgError("failed: " + _lines(text))
    # read back: a DWG that lost entities is not handed over as if it were the drawing
    from ..survey import dxf as dxfmod

    want = len(dxfmod.read(tmp).entities)
    back = to_dxf(out, os.path.join(os.path.dirname(tmp), "back.dxf"), feedback)
    got = len(dxfmod.read(back).entities)
    if got < want:
        os.remove(out)
        raise DwgError(f"lost {want - got} of {want}")
    return {"codepage": cp, "escaped": escaped, "entities": got}
