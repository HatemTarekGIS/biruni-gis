"""Reader for ASCII DXF drawings (no Qt): the entities of the model space with
their CAD layer, true arc data (bulges, ARC, CIRCLE), texts, and block
inserts with their attributes.

Lessons applied from years of real survey files:
- only the ENTITIES section is drawing content (BLOCKS definitions are not);
  paper-space entities (title blocks, viewports) are skipped;
- old R12 POLYLINE/VERTEX/SEQEND is read as well as LWPOLYLINE;
- a polyline whose last vertex returns to the first is closed even without the
  "closed" flag (70=128 plinegen files);
- zero-length edges and 180° "spikes" are removed from straight runs;
- MTEXT formatting codes are stripped but the text inside braces is kept;
- the text encoding follows the drawing ($DWGCODEPAGE before AutoCAD 2007,
  UTF-8 after) plus \\U+XXXX escapes;
- texts written with Arabic keyboard SHX fonts (xarb.shx) are decoded.
"""
import math
import re
from dataclasses import dataclass, field

OTHER_TYPES = {"SPLINE", "ELLIPSE", "HATCH", "SOLID", "3DFACE", "TRACE", "REGION", "MLINE", "LEADER"}


@dataclass
class Entity:
    kind: str                     # poly, line, arc, circle, point, text, insert, other
    dxf_type: str
    layer: str = "0"
    handle: str = ""
    color: int = None
    verts: list = field(default_factory=list)   # poly: [(x, y, bulge)]; line: [(x, y, 0), (x, y, 0)]
    closed: bool = False
    center: tuple = None
    radius: float = 0.0
    angles: tuple = None          # arc start/end, degrees
    mirrored: bool = False        # OCS extrusion (0, 0, -1)
    x: float = 0.0
    y: float = 0.0
    text: str = ""
    style: str = ""
    height: float = 0.0
    rotation: float = 0.0
    block: str = ""
    scale: tuple = (1.0, 1.0)
    attribs: dict = field(default_factory=dict)


@dataclass
class Drawing:
    version: str = ""
    codepage: str = ""
    encoding: str = "utf-8"
    insunits: int = 0
    styles: dict = field(default_factory=dict)       # style name (upper) -> font file
    entities: list = field(default_factory=list)
    other_handles: set = field(default_factory=set)
    skipped_paper: int = 0
    blocks_ignored: int = 0


# ---- text ---------------------------------------------------------------

_CODEPAGES = {"ANSI_936": "gbk", "ANSI_949": "cp949", "ANSI_950": "big5", "ANSI_932": "cp932",
              "DOS720": "cp720", "DOS864": "cp864", "UTF8": "utf-8", "UTF-8": "utf-8"}

ENCODINGS = [("", "Automatic (from the drawing)"), ("utf-8", "UTF-8"), ("cp1256", "Arabic (Windows-1256)"),
             ("cp1252", "Western (Windows-1252)"), ("cp1251", "Cyrillic (Windows-1251)"),
             ("cp1254", "Turkish (Windows-1254)"), ("cp1253", "Greek (Windows-1253)"),
             ("cp1255", "Hebrew (Windows-1255)"), ("gbk", "Chinese (GBK)")]


def codepage_to_python(cp):
    cp = (cp or "").strip().upper()
    if cp in _CODEPAGES:
        return _CODEPAGES[cp]
    m = re.fullmatch(r"ANSI_(\d+)", cp)
    return f"cp{m.group(1)}" if m else "cp1252"


_UNICODE = re.compile(r"\\U\+([0-9A-Fa-f]{4})")


def decode_unicode(s):
    return _UNICODE.sub(lambda m: chr(int(m.group(1), 16)), s)


_PERCENT = {"%%c": "Ø", "%%C": "Ø", "%%d": "°", "%%D": "°", "%%p": "±", "%%P": "±", "%%%": "%"}


def clean_text(s):
    """Plain text of a TEXT entity (%%-codes resolved)."""
    s = decode_unicode(s)
    for k, v in _PERCENT.items():
        s = s.replace(k, v)
    s = re.sub(r"%%[uUoOkK]", "", s)
    return s.strip()


def clean_mtext(s):
    """Plain text of an MTEXT: formatting codes removed, content of braces kept."""
    s = decode_unicode(s)
    s = s.replace("\\\\", "\x00").replace("\\{", "\x01").replace("\\}", "\x02")
    s = re.sub(r"\\[Pp]", " ", s)
    s = s.replace("\\~", " ")
    s = re.sub(r"\\S([^;]*);", lambda m: re.sub(r"[\^#/]", "/", m.group(1)).replace(" /", "/"), s)
    s = re.sub(r"\\[A-Za-z][^;\\{}]*;", "", s)
    s = re.sub(r"\\[A-Za-z]", "", s)
    s = s.replace("{", "").replace("}", "")
    s = s.replace("\x00", "\\").replace("\x01", "{").replace("\x02", "}")
    for k, v in _PERCENT.items():
        s = s.replace(k, v)
    return re.sub(r"\s+", " ", s).strip()


# Arabic keyboard SHX fonts (xarb.shx): each Arabic letter is stored as the
# Latin key it sits on; shifted keys are contextual forms of the same letter.
_XARB = {"q": "ض", "w": "ص", "e": "ث", "r": "ق", "t": "ف", "y": "غ", "u": "ع", "i": "ه", "o": "خ", "p": "ح",
         "[": "ج", "]": "د", "a": "ش", "s": "س", "d": "ي", "f": "ب", "g": "ل", "h": "ا", "j": "ت", "k": "ن",
         "l": "م", ";": "ك", "'": "ط", "z": "ئ", "x": "ء", "c": "ؤ", "v": "ر", "b": "لا", "n": "ى", "m": "ة",
         ",": "و", ".": "ز", "/": "ظ", "`": "ذ"}
_XARB_SHIFT = {"{": "[", "}": "]", ":": ";", '"': "'", "<": ",", ">": ".", "?": "/", "~": "`"}


def decode_xarb(s):
    out = []
    for i, ch in enumerate(s):
        prev_digit = i > 0 and s[i - 1].isdigit()
        next_digit = i + 1 < len(s) and s[i + 1].isdigit()
        if ch in ".,/:" and prev_digit and next_digit:      # 8.00 stays a number
            out.append(ch)
            continue
        key = _XARB_SHIFT.get(ch, ch.lower() if ch.isascii() and ch.isalpha() else ch)
        out.append(_XARB.get(key, ch))
    return "".join(out)


def is_arabic_keyboard_font(font):
    return bool(re.search(r"xarb", font or "", re.I))


# ---- parsing --------------------------------------------------------------

def _pairs(text):
    lines = text.splitlines()
    n = len(lines) - 1
    i = 0
    while i < n:
        code = lines[i].strip()
        try:
            c = int(code)
        except ValueError:
            i += 1
            continue
        yield c, lines[i + 1]
        i += 2


def _float(v, default=0.0):
    try:
        return float(v.strip())
    except (ValueError, AttributeError):
        return default


def sniff(raw):
    """(version, codepage) from the header, read as Latin-1."""
    head = raw[:200000].decode("latin-1", "replace")
    ver = re.search(r"\$ACADVER\s*\r?\n\s*1\s*\r?\n([^\r\n]+)", head)
    cp = re.search(r"\$DWGCODEPAGE\s*\r?\n\s*3\s*\r?\n([^\r\n]+)", head)
    return (ver.group(1).strip() if ver else ""), (cp.group(1).strip() if cp else "")


def is_binary(raw):
    return raw[:22].startswith(b"AutoCAD Binary DXF")


def read(path, encoding=""):
    with open(path, "rb") as f:
        raw = f.read()
    if is_binary(raw):
        raise ValueError("binary DXF")
    return parse_bytes(raw, encoding)


def parse_bytes(raw, encoding=""):
    version, codepage = sniff(raw)
    d = Drawing(version=version, codepage=codepage)
    if encoding:
        enc = encoding
    elif version and version >= "AC1021":
        enc = "utf-8"
    else:
        enc = codepage_to_python(codepage)
    if raw.startswith(b"\xef\xbb\xbf"):
        raw, enc = raw[3:], "utf-8"
    d.encoding = enc
    try:
        text = raw.decode(enc)
    except LookupError:
        d.encoding = enc = "cp1252"
        text = raw.decode(enc, "replace")
    except UnicodeDecodeError:
        text = raw.decode(enc, "replace")
    parse_text(text, d)
    return d


def parse_text(text, d=None):
    d = d or Drawing()
    section = None
    records = []          # (type, [(code, value)]) of the current section
    expect_name = False
    for code, value in _pairs(text):
        if code == 0:
            v = value.strip()
            if v == "SECTION":
                expect_name = True
                records = []
                continue
            if v == "ENDSEC":
                _finish_section(section, records, d)
                section, records = None, []
                continue
            if v == "EOF":
                break
            records.append((v, []))
            continue
        if expect_name and code == 2:
            section = value.strip()
            expect_name = False
            continue
        if section == "HEADER":
            if code == 9:
                records.append((value.strip(), []))
            elif records:
                records[-1][1].append((code, value))
            continue
        if records:
            records[-1][1].append((code, value))
    return d


def _finish_section(section, records, d):
    if section == "HEADER":
        for name, vals in records:
            if name == "$INSUNITS" and vals:
                d.insunits = int(_float(vals[0][1]))
    elif section == "TABLES":
        for typ, vals in records:
            if typ == "STYLE":
                name = next((v.strip() for c, v in vals if c == 2), "")
                font = next((v.strip() for c, v in vals if c == 3), "")
                if name:
                    d.styles[name.upper()] = font
    elif section == "BLOCKS":
        d.blocks_ignored = sum(1 for t, _ in records if t == "BLOCK")
    elif section == "ENTITIES":
        _entities(records, d)


def _common(e, vals):
    for c, v in vals:
        if c == 8:
            e.layer = v.strip()
        elif c == 5:
            e.handle = v.strip()
        elif c == 62:
            e.color = int(_float(v))
        elif c == 230:
            e.mirrored = _float(v, 1.0) < 0
    return e


def _paper(vals):
    return any(c == 67 and v.strip() == "1" for c, v in vals)


def _entities(records, d):
    i = 0
    n = len(records)
    while i < n:
        typ, vals = records[i]
        i += 1
        if typ == "POLYLINE":
            verts = []
            while i < n and records[i][0] in ("VERTEX", "SEQEND"):
                vt, vv = records[i]
                i += 1
                if vt == "SEQEND":
                    break
                flags = int(next((_float(v) for c, v in vv if c == 70), 0))
                if flags & 16 or flags & 128 and not flags & 64:   # spline frame / polyface face
                    continue
                verts.append((next((_float(v) for c, v in vv if c == 10), 0.0),
                              next((_float(v) for c, v in vv if c == 20), 0.0),
                              next((_float(v) for c, v in vv if c == 42), 0.0)))
            if _paper(vals):
                d.skipped_paper += 1
                continue
            flags = int(next((_float(v) for c, v in vals if c == 70), 0))
            if flags & (16 | 64):        # 3D mesh / polyface mesh
                continue
            e = _common(Entity("poly", "POLYLINE"), vals)
            e.verts, e.closed = verts, bool(flags & 1)
            if flags & 8:                # 3D polyline: world coordinates, no OCS
                e.mirrored = False
            if e.mirrored:
                e.verts = [(-x, y, -b) for x, y, b in e.verts]
            d.entities.append(e)
            continue
        if typ == "INSERT":
            attribs = {}
            has_attribs = any(c == 66 and v.strip() == "1" for c, v in vals)
            while has_attribs and i < n and records[i][0] in ("ATTRIB", "SEQEND"):
                at, av = records[i]
                i += 1
                if at == "SEQEND":
                    break
                tag = next((v.strip() for c, v in av if c == 2), "")
                val = next((v for c, v in av if c == 1), "")
                if tag:
                    attribs[tag] = clean_text(val)
            if _paper(vals):
                d.skipped_paper += 1
                continue
            e = _common(Entity("insert", "INSERT"), vals)
            g = dict((c, v) for c, v in vals)
            e.block = g.get(2, "").strip()
            e.x, e.y = _float(g.get(10, "0")), _float(g.get(20, "0"))
            e.rotation = _float(g.get(50, "0"))
            e.scale = (_float(g.get(41, "1"), 1.0), _float(g.get(42, "1"), 1.0))
            e.attribs = attribs
            if e.mirrored:
                e.x = -e.x
            d.entities.append(e)
            continue
        if typ in ("VERTEX", "SEQEND", "ATTRIB"):
            continue
        if _paper(vals):
            d.skipped_paper += 1
            continue
        e = _entity(typ, vals, d)
        if e is not None:
            d.entities.append(e)


def _entity(typ, vals, d):
    g = {}
    for c, v in vals:
        g.setdefault(c, v)
    if typ == "LWPOLYLINE":
        e = _common(Entity("poly", typ), vals)
        verts = []
        for c, v in vals:
            if c == 10:
                verts.append([_float(v), 0.0, 0.0])
            elif c == 20 and verts:
                verts[-1][1] = _float(v)
            elif c == 42 and verts:
                verts[-1][2] = _float(v)
        e.verts = [tuple(v) for v in verts]
        e.closed = bool(int(_float(g.get(70, "0"))) & 1)
        if e.mirrored:
            e.verts = [(-x, y, -b) for x, y, b in e.verts]
        return e
    if typ == "LINE":
        e = _common(Entity("line", typ), vals)
        e.verts = [(_float(g.get(10, "0")), _float(g.get(20, "0")), 0.0),
                   (_float(g.get(11, "0")), _float(g.get(21, "0")), 0.0)]
        e.mirrored = False          # LINE is in world coordinates
        return e
    if typ in ("ARC", "CIRCLE"):
        e = _common(Entity("arc" if typ == "ARC" else "circle", typ), vals)
        cx, cy = _float(g.get(10, "0")), _float(g.get(20, "0"))
        e.radius = _float(g.get(40, "0"))
        if typ == "ARC":
            e.angles = (_float(g.get(50, "0")), _float(g.get(51, "0")))
        e.center = (cx, cy)
        return e if e.radius > 0 else None
    if typ == "POINT":
        e = _common(Entity("point", typ), vals)
        e.x, e.y = _float(g.get(10, "0")), _float(g.get(20, "0"))
        e.mirrored = False
        return e
    if typ in ("TEXT", "MTEXT", "ATTDEF"):
        if typ == "ATTDEF":
            return None
        e = _common(Entity("text", typ), vals)
        e.style = g.get(7, "STANDARD").strip()
        e.height = _float(g.get(40, "0"))
        if typ == "TEXT":
            h, vj = int(_float(g.get(72, "0"))), int(_float(g.get(73, "0")))
            x, y = _float(g.get(10, "0")), _float(g.get(20, "0"))
            if (h or vj) and 11 in g:
                x2, y2 = _float(g.get(11, "0")), _float(g.get(21, "0"))
                if h in (3, 5) and not vj:          # aligned / fit: between the two points
                    x, y = (x + x2) / 2, (y + y2) / 2
                else:
                    x, y = x2, y2
            e.x, e.y = x, y
            e.rotation = _float(g.get(50, "0"))
            e.text = clean_text(g.get(1, ""))
        else:
            e.x, e.y = _float(g.get(10, "0")), _float(g.get(20, "0"))
            if 50 in g:
                e.rotation = _float(g[50])
            elif 11 in g:
                e.rotation = math.degrees(math.atan2(_float(g.get(21, "0")), _float(g.get(11, "1"))))
            chunks = [v for c, v in vals if c == 3] + [v for c, v in vals if c == 1]
            e.text = clean_mtext("".join(chunks))
            e.mirrored = False      # MTEXT insertion point is in world coordinates
        font = d.styles.get(e.style.upper(), "")
        if is_arabic_keyboard_font(font):
            e.text = decode_xarb(e.text)
        if e.mirrored:
            e.x = -e.x
        return e if e.text else None
    if typ in OTHER_TYPES:
        e = _common(Entity("other", typ), vals)
        if e.handle:
            d.other_handles.add(e.handle.upper())
        return None
    return None


# ---- geometry helpers (plain coordinates) --------------------------------

def bulge_midpoint(p1, p2, bulge):
    """Middle point of the arc from p1 to p2 with the DXF bulge (tan(θ/4), + = counter-clockwise)."""
    dx, dy = p2[0] - p1[0], p2[1] - p1[1]
    return ((p1[0] + p2[0]) / 2 + bulge / 2 * dy, (p1[1] + p2[1]) / 2 - bulge / 2 * dx)


def clean_vertices(verts, closed, tol=1e-9):
    """Remove repeated vertices and straight 180° spikes; (verts, closed)."""
    v = list(verts)
    changed = True
    while changed and len(v) >= 2:
        changed = False
        out = []
        for p in v:
            if out and math.hypot(p[0] - out[-1][0], p[1] - out[-1][1]) <= tol:
                out[-1] = (out[-1][0], out[-1][1], p[2])   # keep the bulge of the later vertex
                changed = True
                continue
            out.append(p)
        if closed and len(out) >= 2 and math.hypot(out[0][0] - out[-1][0], out[0][1] - out[-1][1]) <= tol:
            out.pop()
            changed = True
        v = out
        m = len(v)
        if m < 3:
            break
        for i in range(m):
            if not closed and (i == 0 or i == m - 1):
                continue
            a, b, c = v[i - 1], v[i], v[(i + 1) % m]
            if a[2] or b[2]:            # an arc on either side: not a straight spike
                continue
            ux, uy = b[0] - a[0], b[1] - a[1]
            wx, wy = c[0] - b[0], c[1] - b[1]
            lu, lw = math.hypot(ux, uy), math.hypot(wx, wy)
            if lu and lw and (ux * wx + uy * wy) / (lu * lw) < -0.9999:
                del v[i]
                changed = True
                break
    return v, closed


def normalize_polyline(verts, closed, close_tol):
    """Closed when flagged, or when the end returns to the start (within close_tol)."""
    if len(verts) >= 2 and not closed:
        gap = math.hypot(verts[0][0] - verts[-1][0], verts[0][1] - verts[-1][1])
        if gap <= 1e-9:
            verts = verts[:-1]
            closed = True
        elif gap <= close_tol and len(verts) >= 3:
            closed = True        # keep every vertex; the ring is closed by its own short edge
    return clean_vertices(verts, closed)
