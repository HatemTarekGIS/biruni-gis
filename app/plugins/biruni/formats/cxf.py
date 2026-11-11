"""Italian cadastral map sheets - CXF (Agenzia delle Entrate, "formato di interscambio CXF").

A CXF is plain text, one value per line, in blocks:

* MAPPA / MAPPA FONDIARIO - header: sheet name (e.g. A001_000100) and original scale;
* BORDO - a closed outline: code, text size, text angle, text position X/Y, inner point X/Y, number of
  islands, total number of vertices, the vertex count of each island, then the vertices (X and Y on
  separate lines; outer ring first, then the islands). The code says what it is: a number = parcel,
  a number ending in "+" = building, STRADA = road, ACQUA = water, the sheet name = sheet border;
* TESTO - text, size, angle, X, Y;
* SIMBOLO - symbol code, angle, X, Y;
* FIDUCIALE - fiducial point: number, symbol, X, Y, drawn-at X, drawn-at Y;
* LINEA - line type, number of vertices, vertices;
* EOF.

Coordinates are in the sheet's own system (Cassini-Soldner on a local origin for most of Italy, or
Gauss-Boaga): the file does not say which, so the coordinate system is chosen when opening. The .SUP
file that often comes with it (parcel areas) is joined when it is next to the CXF."""
import os
import re

from .common import Dataset, FormatError, line_wkt, point_wkt, polygon_wkt, read_text

ID = "cxf"
LABEL = "Italian cadastral sheet (CXF)"
EXTENSIONS = (".cxf",)
COUNTRIES = ("IT",)

_BLOCKS = {"BORDO", "TESTO", "SIMBOLO", "FIDUCIALE", "LINEA", "EOF", "MAPPA", "MAPPA FONDIARIO"}


class _Lines:
    def __init__(self, text):
        self.lines = [ln.strip() for ln in text.splitlines()]
        self.i = 0

    def next(self):
        while self.i < len(self.lines) and self.lines[self.i] == "":
            self.i += 1
        if self.i >= len(self.lines):
            raise EOFError
        v = self.lines[self.i]
        self.i += 1
        return v

    def num(self):
        v = self.next()
        try:
            return float(v.replace(",", "."))
        except ValueError:
            raise FormatError(f"Line {self.i}: a number was expected, found «{v}».") from None

    def int(self):
        return int(round(self.num()))


def _kind_of(code, sheet):
    c = code.strip().upper()
    if c == sheet.upper() or c.startswith(sheet.upper()[:4]) and "_" in c:
        return "sheet"
    if c.endswith("+"):
        return "building"
    if c in ("STRADA", "ACQUA"):
        return c.lower()
    return "parcel"


def read_sup(path):
    """{parcel code: area m²} from a .SUP file - best effort: any line holding a parcel code and an area."""
    areas = {}
    if not os.path.exists(path):
        return areas
    for line in read_text(path).splitlines():
        parts = re.split(r"[\s;,]+", line.strip())
        nums = [p for p in parts[1:] if re.fullmatch(r"\d+(\.\d+)?", p)]
        if parts and parts[0] and nums:
            areas[parts[0]] = float(nums[-1])
    return areas


def read(path):
    text = read_text(path)
    r = _Lines(text)
    ds = Dataset(ID)
    sheet, scale = os.path.splitext(os.path.basename(path))[0], None
    outlines = ds.layer("cadastre", "polygon")
    texts = ds.layer("texts", "point")
    symbols = ds.layer("symbols", "point")
    fiducials = ds.layer("fiducial_points", "point")
    lines = ds.layer("lines", "line")
    try:
        head = r.next().upper()
        if head not in ("MAPPA", "MAPPA FONDIARIO", "QUADRO D'UNIONE"):
            raise FormatError("This is not a CXF cadastral file (it should start with MAPPA).")
        sheet = r.next() or sheet
        if r.lines[r.i:r.i + 1] and r.lines[r.i].upper().startswith("SCALA"):
            r.next()
            scale = r.next()
        while True:
            tag = r.next().upper()
            if tag == "EOF":
                break
            if tag == "BORDO":
                code = r.next()
                size, angle, tx, ty, ix, iy = (r.num() for _ in range(6))
                n_isl, n_vert = r.int(), r.int()
                isl = [r.int() for _ in range(n_isl)]
                pts = [(r.num(), r.num()) for _ in range(n_vert)]
                outer_n = n_vert - sum(isl)
                rings, k = [pts[:outer_n]], outer_n
                for n in isl:
                    rings.append(pts[k:k + n])
                    k += n
                wkt = polygon_wkt(rings)
                if wkt:
                    outlines.add(wkt, code=code, kind=_kind_of(code, sheet), sheet=sheet,
                                 label_x=tx, label_y=ty, label_size=size, label_angle=angle)
            elif tag == "TESTO":
                txt = r.next()
                size, angle, x, y = (r.num() for _ in range(4))
                texts.add(point_wkt(x, y), text=txt, size=size, angle=angle, sheet=sheet)
            elif tag == "SIMBOLO":
                code = r.next()
                angle, x, y = r.num(), r.num(), r.num()
                symbols.add(point_wkt(x, y), symbol=code, angle=angle, sheet=sheet)
            elif tag == "FIDUCIALE":
                num = r.next()
                sym = r.next()
                x, y, dx, dy = (r.num() for _ in range(4))
                fiducials.add(point_wkt(x, y), number=num, symbol=sym, drawn_x=dx, drawn_y=dy, sheet=sheet)
            elif tag == "LINEA":
                ltype = r.next()
                n = r.int()
                pts = [(r.num(), r.num(), 0.0) for _ in range(n)]
                if len(pts) >= 2:
                    lines.add(line_wkt(pts), line_type=ltype, sheet=sheet)
            else:
                ds.notes.append(f"Unknown block «{tag}» at line {r.i}: reading stopped there.")
                break
    except EOFError:
        ds.notes.append("The file ends without EOF: it may be cut short.")
    if not ds.count():
        raise FormatError("No parcels, texts or points were found in this CXF file.")
    areas = read_sup(os.path.splitext(path)[0] + ".sup") or read_sup(os.path.splitext(path)[0] + ".SUP")
    if areas:
        for i, (wkt, attrs) in enumerate(outlines.features):
            if attrs.get("code") in areas:
                attrs["sup_area"] = areas[attrs["code"]]
        outlines.fields.setdefault("sup_area", "float")
    ds.notes.append(f"Sheet {sheet}" + (f", original scale 1:{scale}" if scale else "")
                    + ": choose its coordinate system (Cassini-Soldner local origin or Gauss-Boaga).")
    return ds
