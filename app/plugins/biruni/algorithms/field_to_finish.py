"""Field to finish: survey point files (CSV/TXT, Trimble JobXML, Leica GSI,
Carlson/TDS RW5) to points, and lines and polygons drawn from the point codes
(begin / end / close / arc control codes and numbered strings)."""
import csv
import io
import math
import os
import re
import xml.etree.ElementTree as ET

from qgis.core import (Qgis, QgsCircularString, QgsCompoundCurve, QgsCoordinateReferenceSystem, QgsFeature,
                       QgsFeatureSink, QgsField, QgsFields, QgsGeometry, QgsLineString, QgsPoint, QgsPolygon,
                       QgsProcessingException, QgsProcessingParameterCrs, QgsProcessingParameterEnum,
                       QgsProcessingParameterFeatureSink, QgsProcessingParameterFile,
                       QgsProcessingParameterFileDestination)
from qgis.PyQt.QtCore import QMetaType

from ..i18n import tr
from ..survey.cogo import normalize_digits
from .base import BiruniAlgorithm

FORMATS = ["Automatic", "CSV / text: point, northing, easting, height, code (PNEZD)",
           "CSV / text: point, easting, northing, height, code (PENZD)", "Trimble JobXML", "Leica GSI",
           "Carlson / TDS RW5"]
BEGIN = {"B", "BEG", "BEGIN", "ST", "START", "SL"}
END = {"E", "END", "STOP", "EL", "ES"}
CLOSE = {"C", "CL", "CLO", "CLOSE"}
ARC_START = {"PC", "BC", "ARC", "AC", "SC"}
ARC_END = {"PT", "EC", "EA", "EOC"}
CONTROLS = BEGIN | END | CLOSE | ARC_START | ARC_END


class SurveyPoint:
    __slots__ = ("name", "e", "n", "z", "desc", "order")

    def __init__(self, name, e, n, z, desc, order):
        self.name, self.e, self.n, self.z, self.desc, self.order = name, e, n, z, desc or "", order


# ---- readers ------------------------------------------------------------------------

def _num(s):
    try:
        return float(normalize_digits(str(s)).replace(",", "."))
    except (TypeError, ValueError):
        return None


def read_delimited(text, order=None):
    """Point, then three numbers, then the description. order is 'NE', 'EN' or None
    (decide from a header or from the values)."""
    sample = text[:4096]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",;\t| ")
        delim = dialect.delimiter
    except csv.Error:
        delim = ","
    rows = []
    header = None
    for raw in csv.reader(io.StringIO(text), delimiter=delim):
        cells = [c.strip() for c in raw if c is not None]
        if delim == " ":
            cells = [c for c in cells if c]
        if not cells:
            continue
        nums = [_num(c) for c in cells[1:4]]
        if len(cells) < 4 or any(v is None for v in nums):
            if header is None and not rows:
                header = [c.lower() for c in cells]
            continue
        rows.append((cells[0], nums, " ".join(c for c in cells[4:] if c)))
    if order is None:
        order = "NE"
        if header and len(header) >= 3:
            h1, h2 = header[1], header[2]
            if h1.startswith(("e", "x")) or h2.startswith(("n", "y")):
                order = "EN"
        elif rows:
            a = sum(abs(r[1][0]) for r in rows) / len(rows)
            b = sum(abs(r[1][1]) for r in rows) / len(rows)
            # UTM-like grids: northings are millions, eastings hundreds of thousands
            if b > 1_000_000 > a:
                order = "EN"
    points = []
    for k, (name, (v1, v2, z), desc) in enumerate(rows):
        n, e = (v1, v2) if order == "NE" else (v2, v1)
        points.append(SurveyPoint(name, e, n, z, desc, k))
    return points, order


def _strip_ns(root):
    for el in root.iter():
        if isinstance(el.tag, str) and "}" in el.tag:
            el.tag = el.tag.split("}", 1)[1]
    return root


def read_jobxml(data):
    root = _strip_ns(ET.fromstring(data))

    def coords(rec):
        grid = rec.find("Grid") if rec.find("Grid") is not None else rec.find("ComputedGrid")
        if grid is None:
            return None
        n, e = _num(grid.findtext("North")), _num(grid.findtext("East"))
        if n is None or e is None:
            return None
        return e, n, _num(grid.findtext("Elevation"))

    reduced = {}  # the final (reduced) coordinates of every point
    for rec in root.iter("Point"):
        c = coords(rec)
        if c is not None and rec.findtext("Deleted", "false").strip().lower() != "true":
            reduced[(rec.findtext("Name") or "").strip()] = (c, (rec.findtext("Code") or "").strip())
    points, seen = [], set()
    for rec in root.iter("PointRecord"):  # field order, which the line codes follow
        if rec.findtext("Deleted", "false").strip().lower() == "true":
            continue
        name = (rec.findtext("Name") or "").strip()
        if name in seen:
            continue
        c = reduced[name][0] if name in reduced else coords(rec)
        if c is None:
            continue
        code = (rec.findtext("Code") or "").strip() or (reduced[name][1] if name in reduced else "")
        seen.add(name)
        points.append(SurveyPoint(name, c[0], c[1], c[2], code, len(points)))
    for name, (c, code) in reduced.items():  # reduced points without a field record
        if name not in seen:
            points.append(SurveyPoint(name, c[0], c[1], c[2], code, len(points)))
    return points


GSI_UNITS = {"0": 0.001, "1": 0.001 * 0.3048, "6": 0.0001, "7": 0.0001 * 0.3048, "8": 0.00001}


def read_gsi(text):
    """Leica GSI-8 and GSI-16 (WI 11 point, 71 code, 81/82/83 E/N/H)."""
    points = []
    for line in text.splitlines():
        line = line.strip().lstrip("*")
        if not line:
            continue
        rec = {}
        for word in line.split():
            if len(word) < 9:
                continue
            wi, info, sign, data = word[:2], word[2:6], word[6], word[7:]
            if not wi.isdigit():
                continue
            if wi == "11":
                rec["name"] = data.lstrip("0") or "0"
            elif wi in ("41", "71"):  # code block / remark: right-aligned, padded with zeros
                rec["code"] = data.lstrip("0").strip() or rec.get("code", "")
            elif wi in ("81", "82", "83") and data.lstrip("-").isdigit():
                factor = GSI_UNITS.get(info[3], 0.001)
                rec[wi] = (-1 if sign == "-" else 1) * int(data) * factor
        if "81" in rec and "82" in rec:
            points.append(SurveyPoint(rec.get("name", str(len(points) + 1)), rec["81"], rec["82"], rec.get("83"),
                                      rec.get("code", ""), len(points)))
    return points


def read_rw5(text):
    """Carlson / TDS raw file: stored and GPS points (SP, GS records)."""
    points = []
    for line in text.splitlines():
        parts = [p.strip() for p in line.split(",")]
        if not parts or parts[0] not in ("SP", "GS", "GPS"):
            continue
        rec = {}
        for p in parts[1:]:
            key, val = p[:2].upper(), p[2:].strip()
            if key == "PN":
                rec["name"] = val
            elif key == "N ":
                rec["n"] = _num(val)
            elif key == "E ":
                rec["e"] = _num(val)
            elif key == "EL":
                rec["z"] = _num(val)
            elif key == "--":
                rec["desc"] = val
        if rec.get("n") is not None and rec.get("e") is not None:
            points.append(SurveyPoint(rec.get("name", str(len(points) + 1)), rec["e"], rec["n"], rec.get("z"),
                                      rec.get("desc", ""), len(points)))
    return points


def read_points(path, fmt=0):
    with open(path, "rb") as f:
        data = f.read()
    text = None
    for enc in ("utf-8-sig", "cp1256", "latin-1"):
        try:
            text = data.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    ext = os.path.splitext(path)[1].lower()
    if fmt == 0:
        head = text.lstrip()[:200]
        if head.startswith("<") and ("JOBFile" in text[:2000] or ext in (".jxl", ".xml")):
            fmt = 3
        elif ext in (".gsi",) or re.match(r"^\*?\d{2}[0-9.]{4}[+-]", head):
            fmt = 4
        elif ext == ".rw5" or re.search(r"^(SP|GS),PN", text, re.M):
            fmt = 5
    if fmt == 3:
        return read_jobxml(data), "JobXML"
    if fmt == 4:
        return read_gsi(text), "GSI"
    if fmt == 5:
        return read_rw5(text), "RW5"
    pts, order = read_delimited(text, {1: "NE", 2: "EN"}.get(fmt))
    return pts, "PNEZD" if order == "NE" else "PENZD"


# ---- code library -------------------------------------------------------------------

def _truthy(v):
    return str(v or "").strip().lower() in ("1", "y", "yes", "true", "x", "✓", "نعم", "line", "خط")


def read_code_library(path):
    """{CODE: {layer, description, kind}} from CSV/TXT/XLSX with columns code,
    layer, description and type (point/line/polygon) - English or Arabic headers."""
    rows = []
    if path.lower().endswith((".xlsx", ".xlsm")):
        import openpyxl

        wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
        ws = wb.worksheets[0]
        rows = [["" if c is None else str(c) for c in r] for r in ws.iter_rows(values_only=True)]
        wb.close()
    else:
        with open(path, encoding="utf-8-sig", errors="replace") as f:
            text = f.read()
        try:
            delim = csv.Sniffer().sniff(text[:2048], delimiters=",;\t|").delimiter
        except csv.Error:
            delim = ","
        rows = list(csv.reader(io.StringIO(text), delimiter=delim))
    if not rows:
        return {}
    head = [str(h).strip().lower() for h in rows[0]]

    def col(*names):
        for i, h in enumerate(head):
            if any(n in h for n in names):
                return i
        return None

    ci = col("code", "كود", "رمز")
    li = col("layer", "طبقة", "طبقه")
    di = col("desc", "وصف")
    ti = col("type", "kind", "نوع", "line", "خط")
    start = 1
    if ci is None:  # no header: code, layer, description, type
        ci, li, di, ti, start = 0, 1, 2, 3, 0
    lib = {}
    for r in rows[start:]:
        if ci >= len(r) or not str(r[ci]).strip():
            continue
        code = str(r[ci]).strip().upper()
        kind = str(r[ti]).strip().lower() if ti is not None and ti < len(r) else ""
        if kind in ("polygon", "area", "مضلع", "مساحة", "closed"):
            kind = "polygon"
        elif kind in ("line", "خط", "polyline") or _truthy(kind):
            kind = "line"
        else:
            kind = "point"
        lib[code] = {"layer": (str(r[li]).strip() if li is not None and li < len(r) else "") or code,
                     "description": str(r[di]).strip() if di is not None and di < len(r) else "", "kind": kind}
    return lib


def split_code(token, library):
    """('KERB', '1') for KERB1 when KERB is a known (or line) code; else (token, '')."""
    t = token.upper()
    if t in library:
        return t, ""
    m = re.match(r"^(.*?[A-Z_؀-ۿ])(\d+)$", t)
    if m and (m.group(1) in library or not library):
        return m.group(1), m.group(2)
    return t, ""


# ---- line building --------------------------------------------------------------------

def arc_points(p1, p2, p3):
    """Circle through three points -> True when they are not collinear."""
    ax, ay = p1
    bx, by = p2
    cx, cy = p3
    d = 2 * (ax * (by - cy) + bx * (cy - ay) + cx * (ay - by))
    return abs(d) > 1e-9


def build_strings(points, library):
    """Group coded points into strings. Returns (strings, unknown codes):
    strings = [{code, string, layer, kind, closed, vertices:[(point, arc_flag)]}]."""
    strings, active, unknown = [], {}, {}
    for p in points:
        tokens = p.desc.split()
        if not tokens:
            continue
        code_token = tokens[0]
        controls = {t.upper() for t in tokens[1:] if t.upper() in CONTROLS}
        if code_token.upper() in CONTROLS and len(tokens) > 1:  # "B KERB"
            controls.add(code_token.upper())
            code_token = tokens[1]
        base, sid = split_code(code_token, library)
        info = library.get(base)
        if library and info is None and base not in unknown:
            unknown[base] = 0
        if library and info is None:
            unknown[base] += 1
        kind = info["kind"] if info else ("line" if controls else "point")
        if kind == "point" and not controls:
            continue
        key = (base, sid)
        s = active.get(key)
        if s is None or controls & BEGIN and s["vertices"]:
            if s is not None and len(s["vertices"]) >= 2:
                strings.append(s)
            s = {"code": base, "string": sid, "layer": info["layer"] if info else base,
                 "kind": "polygon" if kind == "polygon" else "line", "closed": False, "vertices": []}
            active[key] = s
        flag = "pc" if controls & ARC_START else ("pt" if controls & ARC_END else "")
        s["vertices"].append((p, flag))
        if controls & CLOSE:
            s["closed"] = True
        if controls & (END | CLOSE):
            if len(s["vertices"]) >= 2:
                strings.append(s)
            del active[key]
    for s in active.values():
        if len(s["vertices"]) >= 2:
            if s["kind"] == "polygon":
                s["closed"] = True
            strings.append(s)
    return strings, unknown


def string_curve(vertices, closed):
    """A compound curve: straight segments, and circular arcs from a PC point
    through the next point to the PT point."""
    pts = [QgsPoint(p.e, p.n, p.z if p.z is not None else 0.0) for p, _ in vertices]
    flags = [f for _, f in vertices]
    if closed and (pts[0].x(), pts[0].y()) != (pts[-1].x(), pts[-1].y()):
        pts.append(pts[0].clone())
        flags.append("")
    curve = QgsCompoundCurve()
    run = [pts[0]]
    i = 0
    while i < len(pts) - 1:
        if flags[i] == "pc" and i + 2 < len(pts) and flags[i + 2] == "pt" and arc_points(
                (pts[i].x(), pts[i].y()), (pts[i + 1].x(), pts[i + 1].y()), (pts[i + 2].x(), pts[i + 2].y())):
            if len(run) >= 2:
                curve.addCurve(QgsLineString(run))
            curve.addCurve(QgsCircularString(pts[i], pts[i + 1], pts[i + 2]))
            run = [pts[i + 2]]
            i += 2
            continue
        run.append(pts[i + 1])
        i += 1
    if len(run) >= 2:
        curve.addCurve(QgsLineString(run))
    return curve


class FieldToFinish(BiruniAlgorithm):
    ID = "fieldtofinish"
    NAME = "Field to finish (survey points to drawing)"
    GROUP = "Survey & CAD"
    GROUP_ID = "survey"
    HELP = ("Reads a survey point file from the field - CSV or text (point, northing, easting, height, code or "
            "point, easting, northing…), Trimble JobXML, Leica GSI-8/16 or a Carlson / TDS RW5 raw file - and "
            "draws it: every point with its number and code, and lines and areas joined from the codes. A code "
            "library (Excel or CSV: code, layer, description, type point / line / polygon) says which codes are "
            "lines; control codes after the code steer them: B / BEGIN starts a line, E / END ends it, C / CLOSE "
            "closes it, PC … PT makes an arc through three points; numbered codes (KERB1, KERB2) are separate "
            "lines of the same kind. Codes missing from the library are listed. Optionally writes a DXF for CAD "
            "with points, numbers, lines and areas on layers by code.")

    INPUT, FORMAT, CODES, CRS = "INPUT", "FORMAT", "CODES", "CRS"
    POINTS, LINES, POLYGONS, DXF = "POINTS", "LINES", "POLYGONS", "DXF"

    def initAlgorithm(self, config=None):  # noqa: N802
        self.addParameter(QgsProcessingParameterFile(
            self.INPUT, tr("Survey point file"),
            fileFilter=tr("Survey files") + " (*.csv *.txt *.xyz *.pnt *.asc *.jxl *.xml *.gsi *.rw5);;" + tr(
                "All files") + " (*.*)"))
        self.addParameter(QgsProcessingParameterEnum(
            self.FORMAT, tr("Format"), [tr(f) for f in FORMATS], defaultValue=0))
        self.addParameter(QgsProcessingParameterFile(
            self.CODES, tr("Code library (Excel / CSV: code, layer, description, type)"), optional=True,
            fileFilter=tr("Code library") + " (*.xlsx *.csv *.txt)"))
        self.addParameter(QgsProcessingParameterCrs(self.CRS, tr("Coordinate system of the points"),
                                                    optional=True))
        self.addParameter(QgsProcessingParameterFeatureSink(
            self.POINTS, tr("Survey points"), Qgis.ProcessingSourceType.VectorPoint))
        self.addParameter(QgsProcessingParameterFeatureSink(
            self.LINES, tr("Survey lines"), Qgis.ProcessingSourceType.VectorLine))
        self.addParameter(QgsProcessingParameterFeatureSink(
            self.POLYGONS, tr("Survey areas"), Qgis.ProcessingSourceType.VectorPolygon))
        self.addParameter(QgsProcessingParameterFileDestination(
            self.DXF, tr("DXF for CAD"), "DXF (*.dxf)", optional=True, createByDefault=False))

    def processAlgorithm(self, parameters, context, feedback):  # noqa: N802
        path = self.parameterAsFile(parameters, self.INPUT, context)
        if not path or not os.path.exists(path):
            raise QgsProcessingException(tr("Choose the survey point file."))
        try:
            points, kind = read_points(path, self.parameterAsEnum(parameters, self.FORMAT, context))
        except ET.ParseError as e:
            raise QgsProcessingException(tr("The XML file could not be read: {e}").format(e=e))
        if not points:
            raise QgsProcessingException(tr("No survey points were found in this file."))
        feedback.pushInfo(tr("{n} points read ({f}).").format(n=len(points), f=kind))
        lib_path = self.parameterAsFile(parameters, self.CODES, context)
        library = read_code_library(lib_path) if lib_path else {}
        crs = self.parameterAsCrs(parameters, self.CRS, context)
        if not crs.isValid():
            crs = QgsCoordinateReferenceSystem()

        pf = QgsFields()
        for n, t in (("point", QMetaType.Type.QString), ("code", QMetaType.Type.QString),
                     ("description", QMetaType.Type.QString), ("layer", QMetaType.Type.QString),
                     ("easting", QMetaType.Type.Double), ("northing", QMetaType.Type.Double),
                     ("height", QMetaType.Type.Double)):
            pf.append(QgsField(n, t))
        psink, pid = self.parameterAsSink(parameters, self.POINTS, context, pf, Qgis.WkbType.PointZ, crs)
        for p in points:
            tokens = p.desc.split()
            code = ""
            if tokens:
                code = tokens[1] if tokens[0].upper() in CONTROLS and len(tokens) > 1 else tokens[0]
            base, _sid = split_code(code, library) if code else ("", "")
            info = library.get(base, {})
            f = QgsFeature(pf)
            f.setGeometry(QgsGeometry(QgsPoint(p.e, p.n, p.z if p.z is not None else 0.0)))
            f.setAttributes([p.name, base, p.desc, info.get("layer", base), p.e, p.n, p.z])
            psink.addFeature(f, QgsFeatureSink.Flag.FastInsert)

        strings, unknown = build_strings(points, library)
        lf = QgsFields()
        for n, t in (("code", QMetaType.Type.QString), ("string", QMetaType.Type.QString),
                     ("layer", QMetaType.Type.QString), ("description", QMetaType.Type.QString),
                     ("closed", QMetaType.Type.Bool), ("points", QMetaType.Type.Int),
                     ("first_point", QMetaType.Type.QString), ("last_point", QMetaType.Type.QString),
                     ("length", QMetaType.Type.Double)):
            lf.append(QgsField(n, t))
        af = QgsFields(lf)
        af.append(QgsField("area", QMetaType.Type.Double))
        lsink, lid = self.parameterAsSink(parameters, self.LINES, context, lf, Qgis.WkbType.CompoundCurveZ, crs)
        asink, aid = self.parameterAsSink(parameters, self.POLYGONS, context, af, Qgis.WkbType.CurvePolygonZ, crs)
        n_lines = n_areas = 0
        dxf_items = []
        for s in strings:
            curve = string_curve(s["vertices"], s["closed"])
            desc = library.get(s["code"], {}).get("description", "")
            attrs = [s["code"], s["string"], s["layer"], desc, s["closed"], len(s["vertices"]),
                     s["vertices"][0][0].name, s["vertices"][-1][0].name, curve.length()]
            if s["closed"] and s["kind"] == "polygon" and len(s["vertices"]) >= 3:
                from qgis.core import QgsCurvePolygon

                poly = QgsCurvePolygon()
                poly.setExteriorRing(curve)
                g = QgsGeometry(poly)
                f = QgsFeature(af)
                f.setGeometry(g)
                f.setAttributes(attrs + [g.area()])
                asink.addFeature(f, QgsFeatureSink.Flag.FastInsert)
                n_areas += 1
            else:
                g = QgsGeometry(curve)
                f = QgsFeature(lf)
                f.setGeometry(g)
                f.setAttributes(attrs)
                lsink.addFeature(f, QgsFeatureSink.Flag.FastInsert)
                n_lines += 1
            dxf_items.append((s["layer"], g))

        if unknown:
            listed = ", ".join(f"{c} ({n})" for c, n in sorted(unknown.items(), key=lambda kv: -kv[1]))
            feedback.pushWarning(tr("Codes missing from the library: {c}").format(c=listed))
        feedback.pushInfo(tr("{l} lines and {a} areas drawn from the codes.").format(l=n_lines, a=n_areas))
        results = {self.POINTS: pid, self.LINES: lid, self.POLYGONS: aid, "POINT_COUNT": len(points),
                   "LINE_COUNT": n_lines, "AREA_COUNT": n_areas, "UNKNOWN_CODES": ", ".join(sorted(unknown)),
                   "ORDER": kind}
        dxf = self.parameterAsFileOutput(parameters, self.DXF, context)
        if dxf:
            write_dxf(dxf, points, library, dxf_items, crs)
            results[self.DXF] = dxf
        return results


def write_dxf(path, points, library, items, crs):
    """Points (with their number as text) and the lines/areas on layers named
    after the codes, through the GDAL DXF writer."""
    from osgeo import ogr, osr

    if os.path.exists(path):
        os.remove(path)
    drv = ogr.GetDriverByName("DXF")
    ds = drv.CreateDataSource(path)
    srs = None
    if crs.isValid():
        srs = osr.SpatialReference()
        srs.ImportFromWkt(crs.toWkt())
    lyr = ds.CreateLayer("entities", srs, ogr.wkbUnknown)
    defn = lyr.GetLayerDefn()

    def add(geom_wkb, layer, style=None):
        f = ogr.Feature(defn)
        f.SetField("Layer", layer)
        if style:
            f.SetStyleString(style)
        f.SetGeometry(ogr.CreateGeometryFromWkb(geom_wkb))
        lyr.CreateFeature(f)

    for p in points:
        tokens = p.desc.split()
        code = tokens[0] if tokens else ""
        base, _ = split_code(code, library) if code else ("", "")
        layer = library.get(base, {}).get("layer") or (base or "POINTS")
        wkb = bytes(QgsGeometry(QgsPoint(p.e, p.n, p.z or 0.0)).asWkb())
        add(wkb, layer)
        label = str(p.name).replace('"', "'")
        add(wkb, "PNT_NUMBERS", f'LABEL(f:"Arial",t:"{label}",s:0.5g,dx:0.3g,dy:0.3g)')
    for layer, g in items:
        add(bytes(g.constGet().segmentize().asWkb()) if g.constGet() else bytes(g.asWkb()), layer)
    ds = None
