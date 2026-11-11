"""Delivery specs: an Excel workbook that says which layers and fields a delivery must have (required values,
allowed values and ranges, unique values, text length, geometry type, coordinate system, feature count), a tool
that writes one from existing layers, and a tool that checks a delivery against it."""
import datetime as dt
import os
import re
from collections import Counter, defaultdict

from qgis.core import (Qgis, QgsCoordinateReferenceSystem, QgsCoordinateTransform, QgsFeature, QgsFeatureSink,
                       QgsField, QgsFields, QgsGeometry, QgsProcessingException,
                       QgsProcessingParameterFeatureSink, QgsProcessingParameterFile,
                       QgsProcessingParameterFileDestination, QgsProcessingParameterMultipleLayers,
                       QgsProviderRegistry, QgsVectorLayer, QgsWkbTypes)
from qgis.PyQt.QtCore import QMetaType

from ..i18n import is_rtl, tr
from ..version import APP_NAME, VERSION
from .base import BiruniAlgorithm

ACCENT = "0E7C74"

# ---- text helpers -------------------------------------------------------------------------------------------
_DIGITS = str.maketrans("٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹٫٬", "01234567890123456789.,")
_ARABIC_FOLD = str.maketrans({"أ": "ا", "إ": "ا", "آ": "ا", "ٱ": "ا", "ة": "ه", "ى": "ي", "ئ": "ي", "ؤ": "و"})
_MARKS = re.compile(r"[ً-ْٰـ]")


def norm(text):
    """Loose key for names and headers: case, spaces, punctuation, Arabic letter forms and the article
    'ال' do not matter; text in parentheses is a hint and is dropped."""
    s = re.sub(r"\(.*?\)|\[.*?\]", " ", str(text or ""))
    s = _MARKS.sub("", s.translate(_DIGITS).translate(_ARABIC_FOLD).casefold())
    words = [w[2:] if w.startswith("ال") and len(w) > 3 else w for w in re.split(r"[^\w]+", s)]
    return "".join(w.replace("_", "") for w in words)


def is_null(v):
    return v is None or (hasattr(v, "isNull") and v.isNull())


def is_empty(v, zero_is_empty=False):
    if is_null(v):
        return True
    if isinstance(v, str):
        return not v.strip() or (zero_is_empty and as_number(v) == 0)
    if zero_is_empty and isinstance(v, (int, float)) and not isinstance(v, bool) and v == 0:
        return True
    return False


def as_number(v):
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    try:
        return float(str(v).translate(_DIGITS).replace(",", "").strip())
    except (TypeError, ValueError):
        return None


def show(v):
    if is_null(v):
        return "NULL"
    if hasattr(v, "toString"):
        return v.toString("yyyy-MM-dd HH:mm:ss") if hasattr(v, "time") else v.toString("yyyy-MM-dd")
    return str(v)[:250]


_YES_WORDS = ("yes", "y", "true", "1", "x", "✓", "✔", "☑", "نعم", "ايوه", "ايوا", "اه", "صح", "مطلوب",
              "الزامي", "إلزامي", "required", "mandatory")
_YES = {norm(w) for w in _YES_WORDS} - {""}


def yes(v):
    if is_null(v) or isinstance(v, bool):
        return v is True
    s = str(v).strip().casefold()
    return bool(s) and (s in _YES_WORDS or norm(s) in _YES)


# ---- field and geometry kinds --------------------------------------------------------------------------------
INTEGER, DECIMAL, NUMBER, TEXT, DATE, DATETIME, BOOLEAN = ("integer", "decimal", "number", "text", "date",
                                                           "datetime", "boolean")
KIND_LABELS = {INTEGER: "Integer", DECIMAL: "Decimal", TEXT: "Text", DATE: "Date", DATETIME: "Date & time",
               BOOLEAN: "Yes/No"}
_KIND_WORDS = [  # checked in order: the first group with a word inside the cell wins
    (DATETIME, ("datetime", "date & time", "date time", "timestamp", "تاريخ ووقت", "تاريخ و وقت")),
    (DATE, ("date", "تاريخ")),
    (BOOLEAN, ("yes/no", "boolean", "bool", "logical", "نعم/لا", "منطقي")),
    (INTEGER, ("integer", "int", "long", "short", "whole", "عدد صحيح", "رقم صحيح", "صحيح")),
    (DECIMAL, ("decimal", "double", "real", "float", "عشري")),
    (NUMBER, ("number", "numeric", "رقم", "عدد", "رقمي")),
    (TEXT, ("text", "string", "char", "varchar", "نص", "نصي")),
]


def field_kind(field):
    t = field.type()
    if t in (QMetaType.Type.Int, QMetaType.Type.UInt, QMetaType.Type.LongLong, QMetaType.Type.ULongLong,
             QMetaType.Type.Short, QMetaType.Type.UShort):
        return INTEGER
    if t in (QMetaType.Type.Double, QMetaType.Type.Float):
        return DECIMAL
    if t == QMetaType.Type.QString:
        return TEXT
    if t == QMetaType.Type.QDate:
        return DATE
    if t == QMetaType.Type.QDateTime:
        return DATETIME
    if t == QMetaType.Type.Bool:
        return BOOLEAN
    return field.typeName() or "other"


def spec_kind(text):
    s = str(text or "").casefold().strip()
    if not s:
        return None
    for kind, words in _KIND_WORDS:
        for w in words:
            if w.isascii():
                if re.search(r"(^|[^\w])" + re.escape(w) + r"($|[^\w])", s):
                    return kind
            elif w in s:  # Arabic words take the article and suffixes: التاريخ, رقمي
                return kind
    return None


def kind_ok(wanted, actual):
    if wanted is None:
        return True
    if wanted == NUMBER:
        return actual in (INTEGER, DECIMAL)
    if wanted == DECIMAL:
        return actual in (INTEGER, DECIMAL)
    return wanted == actual


GEOM_LABELS = {Qgis.GeometryType.Point: "Point", Qgis.GeometryType.Line: "Line",
               Qgis.GeometryType.Polygon: "Polygon", Qgis.GeometryType.Null: "No geometry (table)"}
_GEOM_WORDS = [
    (Qgis.GeometryType.Null, ("no geometry", "table", "none", "بدون", "بلا", "جدول")),
    (Qgis.GeometryType.Point, ("point", "نقط", "نقاط")),
    (Qgis.GeometryType.Line, ("line", "polyline", "خط", "خطوط")),
    (Qgis.GeometryType.Polygon, ("polygon", "area", "مضلع", "مساح", "مضلعات")),
]


def spec_geometry(text):
    s = str(text or "").casefold().strip()
    if not s:
        return None
    for gtype, words in _GEOM_WORDS:
        if any(w in s for w in words):
            return gtype
    return None


def spec_crs(text):
    s = str(text or "").strip().translate(_DIGITS)
    if not s:
        return None
    if re.fullmatch(r"\d{4,6}", s):
        s = f"EPSG:{s}"
    crs = QgsCoordinateReferenceSystem(s)
    return crs if crs.isValid() else s


def crs_ok(wanted, crs):
    if wanted is None:
        return True
    if isinstance(wanted, QgsCoordinateReferenceSystem):
        return crs.isValid() and (crs == wanted or crs.authid() == wanted.authid())
    return norm(wanted) in (norm(crs.authid()), norm(crs.description()))


_NUM = r"-?\d+(?:\.\d+)?"


def parse_allowed(text):
    """('range', lo, hi) | ('list', [values]) | None. A range is 'lo-hi', 'lo..hi', 'lo to hi', '>= lo' …"""
    if is_null(text):
        return None
    s = str(text).translate(_DIGITS).strip()
    if not s:
        return None
    m = re.fullmatch(rf"({_NUM})\s*(?:\.\.|–|—|to|الى|إلى|:|-)\s*({_NUM})", s, re.I)
    if m:
        lo, hi = float(m.group(1)), float(m.group(2))
        return ("range", min(lo, hi), max(lo, hi))
    m = re.fullmatch(rf"(>=|=>|≥|>|<=|=<|≤|<)\s*({_NUM})", s)
    if m:
        op, x = m.group(1), float(m.group(2))
        if op in (">=", "=>", "≥"):
            return ("range", x, None)
        if op == ">":
            return ("range", x, None, "open")
        if op in ("<=", "=<", "≤"):
            return ("range", None, x)
        return ("range", None, x, "open")
    items = [p.strip() for p in re.split(r"[,;،؛|\n]", str(text)) if p.strip()]
    return ("list", items) if items else None


def value_allowed(rule, v):
    """None when allowed, else the rule name of the failure."""
    if rule[0] == "range":
        x = as_number(v)
        if x is None:
            return "Value out of range"
        lo, hi = rule[1], rule[2]
        strict = len(rule) > 3
        if lo is not None and (x < lo or (strict and x == lo)):
            return "Value out of range"
        if hi is not None and (x > hi or (strict and x == hi)):
            return "Value out of range"
        return None
    sv = show(v).strip()
    x = as_number(v)
    for item in rule[1]:
        if item.casefold() == sv.casefold():
            return None
        y = as_number(item)
        if x is not None and y is not None and abs(x - y) < 1e-9:
            return None
    return "Value not allowed"


# ---- the spec workbook ---------------------------------------------------------------------------------------
HEADERS = {
    "layer": ("Layer", "layer name", "table", "table name", "feature class", "الطبقة", "اسم الطبقة", "جدول",
              "الجدول"),
    "field": ("Field", "field name", "column", "attribute", "الحقل", "اسم الحقل", "العمود"),
    "required": ("Required", "mandatory", "not null", "مطلوب", "إلزامي", "الزامي"),
    "type": ("Type", "data type", "field type", "النوع", "نوع البيانات", "نوع الحقل"),
    "allowed": ("Allowed values", "values", "domain", "allowed", "range", "القيم المسموحة", "القيم المسموح بها",
                "القيم", "النطاق", "المدى"),
    "unique": ("Unique", "unique values", "no duplicates", "فريد", "غير مكرر", "بدون تكرار"),
    "maxlen": ("Max length", "length", "max len", "maximum length", "أقصى طول", "الطول", "الحد الأقصى للطول"),
    "zero": ("Zero counts as empty", "zero is empty", "الصفر يعتبر فارغ", "الصفر فارغ"),
    "notes": ("Notes", "remarks", "comments", "ملاحظات"),
    "geometry": ("Geometry", "geometry type", "shape", "الشكل الهندسي", "نوع الشكل", "الشكل", "الأشكال",
                 "نوع الهندسة"),
    "mincount": ("Min features", "min count", "minimum count", "min feature count", "minimum features",
                 "أقل عدد عناصر", "أقل عدد", "الحد الأدنى للعناصر"),
    "crs": ("CRS", "coordinate system", "epsg", "srs", "spatial reference", "نظام الإحداثيات", "الإسقاط"),
}
_HEADER_KEYS = {norm(name): key for key, names in HEADERS.items() for name in names}


class FieldRule:
    def __init__(self, layer, field):
        self.layer, self.field = layer, field
        self.required = self.unique = self.zero = False
        self.kind = self.allowed = self.maxlen = None


class LayerRule:
    def __init__(self, layer):
        self.layer = layer
        self.required = True
        self.geometry = self.mincount = self.crs = None
        self.fields = []


def _header_map(row):
    found = {}
    for i, cell in enumerate(row):
        key = _HEADER_KEYS.get(norm(cell)) if not is_null(cell) else None
        if key and key not in found:
            found[key] = i
    return found


def read_spec(path):
    """{normalised layer name: LayerRule} from a spec workbook (sheets are recognised by their headers)."""
    from openpyxl import load_workbook

    try:
        wb = load_workbook(path, read_only=True, data_only=True)
    except Exception as e:
        raise QgsProcessingException(tr("The spec workbook could not be read: {e}").format(e=e))
    layers = {}

    def layer_rule(name):
        key = norm(name)
        if key not in layers:
            layers[key] = LayerRule(str(name).strip())
        return layers[key]

    for ws in wb.worksheets:
        rows = ws.iter_rows(values_only=True)
        cols = None
        for _ in range(10):
            row = next(rows, None)
            if row is None:
                break
            found = _header_map(row)
            if "layer" in found and len(found) >= 2:
                cols = found
                break
        if not cols:
            continue

        def cell(row, key):
            i = cols.get(key)
            return row[i] if i is not None and i < len(row) else None

        for row in rows:
            name = cell(row, "layer")
            if is_null(name) or not str(name).strip():
                continue
            lr = layer_rule(name)
            if "field" in cols:
                fname = cell(row, "field")
                if is_null(fname) or not str(fname).strip():
                    continue
                fr = FieldRule(lr.layer, str(fname).strip())
                fr.required = yes(cell(row, "required"))
                fr.unique = yes(cell(row, "unique"))
                fr.zero = yes(cell(row, "zero"))
                fr.kind = spec_kind(cell(row, "type"))
                fr.allowed = parse_allowed(cell(row, "allowed"))
                n = as_number(cell(row, "maxlen"))
                fr.maxlen = int(n) if n and n > 0 else None
                lr.fields.append(fr)
            else:
                req = cell(row, "required")
                lr.required = True if is_null(req) or not str(req).strip() else yes(req)
                lr.geometry = spec_geometry(cell(row, "geometry"))
                n = as_number(cell(row, "mincount"))
                lr.mincount = int(n) if n and n > 0 else None
                lr.crs = spec_crs(cell(row, "crs"))
    wb.close()
    if not layers:
        raise QgsProcessingException(tr("No 'Layer' column was found in the spec workbook. Make a spec with "
                                        "'Delivery spec template from layers'."))
    return layers


def layer_names(layer):
    """Every name a delivered layer can be known by: its title, its table and its file name."""
    names = {norm(layer.name())}
    try:
        parts = QgsProviderRegistry.instance().decodeUri(layer.providerType(), layer.source())
    except Exception:
        parts = {}
    for k in ("layerName", "table"):
        if parts.get(k):
            names.add(norm(parts[k]))
    path = parts.get("path") or ""
    if path:
        names.add(norm(os.path.splitext(os.path.basename(path))[0]))
    names.discard("")
    return names


# ---- Excel -------------------------------------------------------------------------------------------------
class Book:
    """A small openpyxl workbook writer with Biruni's header style; right-to-left when the UI is."""

    def __init__(self):
        from openpyxl import Workbook

        self.wb = Workbook()
        self.first = True
        self.rtl = is_rtl()

    def sheet(self, title, headers=None, rows=(), widths=None):
        from openpyxl.styles import Alignment, Font, PatternFill
        from openpyxl.utils import get_column_letter

        ws = self.wb.active if self.first else self.wb.create_sheet()
        self.first = False
        ws.title = title[:31]
        ws.sheet_view.rightToLeft = self.rtl
        rows = list(rows)
        if headers:
            ws.append(list(headers))
            for c in ws[1]:
                c.font = Font(bold=True, color="FFFFFF")
                c.fill = PatternFill("solid", fgColor=ACCENT)
                c.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
            ws.freeze_panes = "A2"
        for r in rows:
            ws.append(list(r))
        if headers and rows:
            ws.auto_filter.ref = f"A1:{get_column_letter(len(headers))}{len(rows) + 1}"
        for i, w in enumerate(widths or [], start=1):
            ws.column_dimensions[get_column_letter(i)].width = w
        return ws

    @staticmethod
    def title(ws, row, text, size=12):
        from openpyxl.styles import Font

        ws.cell(row=row, column=1, value=text).font = Font(bold=True, size=size, color=ACCENT)

    @staticmethod
    def table(ws, row, headers, rows):
        """Writes a small headed table at row; returns the next free row."""
        from openpyxl.styles import Font, PatternFill

        for j, h in enumerate(headers, start=1):
            c = ws.cell(row=row, column=j, value=h)
            c.font = Font(bold=True, color="FFFFFF")
            c.fill = PatternFill("solid", fgColor=ACCENT)
        for i, r in enumerate(rows, start=1):
            for j, v in enumerate(r, start=1):
                ws.cell(row=row + i, column=j, value=v)
        return row + len(rows) + 2

    @staticmethod
    def footer(ws, row):
        from openpyxl.styles import Font

        ws.cell(row=row, column=1, value=tr("Made with {app} {v} on {d}").format(
            app=APP_NAME, v=VERSION, d=dt.datetime.now().strftime("%Y-%m-%d %H:%M"))).font = Font(
            italic=True, color="7B8791")

    def save(self, path):
        folder = os.path.dirname(path)
        if folder:
            os.makedirs(folder, exist_ok=True)
        try:
            self.wb.save(path)
        except OSError as e:
            raise QgsProcessingException(tr("The Excel file could not be saved (is it open in Excel?): {e}")
                                         .format(e=e))


# ---- tool 1: the template ------------------------------------------------------------------------------------
README_EN = [
    "How to fill this delivery spec",
    "Sheet 'Layers': one row per layer the delivery must contain - its geometry (Point, Line, Polygon or "
    "No geometry), whether it is required, the minimum number of features and the coordinate system "
    "(e.g. EPSG:32637).",
    "Sheet 'Fields': one row per field. Required = Yes means every feature needs a value. Type: Integer, Decimal, "
    "Number, Text, Date, Date & time or Yes/No. Allowed values: a list separated by commas or semicolons "
    "(A, B, C) or a range (0-100, >= 0). Unique = Yes means no two features may share a value. Max length "
    "limits text. Zero counts as empty = Yes makes 0 a missing value.",
    "Leave a cell empty when there is no rule. Headers may be in English or Arabic. Yes can be written as "
    "Yes, Y, 1, ✓ or نعم.",
    "Check a delivery with the Biruni GIS tool 'Check a delivery against the spec'.",
]
README_AR = [
    "طريقة تعبئة كراسة شروط التسليم",
    "ورقة «الطبقات»: صف لكل طبقة لازم تكون في التسليم - نوع الشكل (نقطة أو خط أو مضلع أو بدون شكل)، وهل هي "
    "مطلوبة، وأقل عدد عناصر، ونظام الإحداثيات (مثل EPSG:32637).",
    "ورقة «الحقول»: صف لكل حقل. مطلوب = نعم يعني كل عنصر لازم يكون له قيمة. النوع: عدد صحيح أو رقم عشري أو رقم "
    "أو نص أو تاريخ أو تاريخ ووقت أو نعم/لا. القيم المسموحة: قائمة مفصولة بفواصل (أ، ب، ج) أو مدى (0-100 أو "
    ">= 0). فريد = نعم يعني ممنوع عنصرين بنفس القيمة. أقصى طول يحدد طول النص. الصفر يعتبر فارغ = نعم يخلي "
    "الصفر قيمة ناقصة.",
    "سيب الخانة فاضية لو مفيش شرط. العناوين ممكن تكون عربي أو إنجليزي. «نعم» ممكن تتكتب نعم أو Yes أو 1 أو ✓.",
    "افحص التسليم بأداة Biruni GIS «فحص التسليم مقابل كراسة الشروط».",
]


class DeliverySpecTemplate(BiruniAlgorithm):
    ID = "deliveryspectemplate"
    NAME = "Delivery spec template from layers"
    GROUP = "Engineering office"
    GROUP_ID = "office"
    HELP = ("Writes an Excel delivery spec from the chosen layers, ready for the client or the contractor to "
            "fill: a 'Layers' sheet (geometry, required, minimum feature count, coordinate system) and a "
            "'Fields' sheet (required, type, allowed values or range, unique, maximum length), prefilled with "
            "what the layers have now - current types, and the current values as a hint when there are few. "
            "Check a delivery against it with 'Check a delivery against the spec'.")

    LAYERS, OUTPUT = "LAYERS", "OUTPUT"

    def initAlgorithm(self, config=None):  # noqa: N802
        self.addParameter(QgsProcessingParameterMultipleLayers(
            self.LAYERS, tr("Layers"), Qgis.ProcessingSourceType.Vector))
        self.addParameter(QgsProcessingParameterFileDestination(
            self.OUTPUT, tr("Spec workbook"), tr("Excel workbook") + " (*.xlsx)"))

    def processAlgorithm(self, parameters, context, feedback):  # noqa: N802
        from openpyxl.styles import Alignment, Font
        from openpyxl.worksheet.datavalidation import DataValidation

        layers = [lyr for lyr in self.parameterAsLayerList(parameters, self.LAYERS, context)
                  if isinstance(lyr, QgsVectorLayer)]
        if not layers:
            raise QgsProcessingException(tr("Choose at least one vector layer."))
        path = self.parameterAsFileOutput(parameters, self.OUTPUT, context)
        y, n = tr("Yes"), tr("No")

        layer_rows, field_rows = [], []
        for i, lyr in enumerate(layers):
            if feedback.isCanceled():
                return {}
            gtype = lyr.geometryType() if lyr.isSpatial() else Qgis.GeometryType.Null
            layer_rows.append([lyr.name(), tr(GEOM_LABELS.get(gtype, "No geometry (table)")), y, None,
                               lyr.crs().authid() if lyr.isSpatial() and lyr.crs().isValid() else None, None])
            fields = lyr.fields()
            # the GeoPackage / ArcGIS feature id is not data
            keep = [idx for idx in range(fields.count())
                    if not (idx == 0 and fields.at(idx).name().lower() in ("fid", "ogc_fid", "objectid")
                            and field_kind(fields.at(idx)) == INTEGER)]
            empty = {idx: False for idx in keep}
            distinct = {idx: set() for idx in keep}
            count = 0
            for feat in lyr.getFeatures():
                count += 1
                attrs = feat.attributes()
                for idx in keep:
                    v = attrs[idx]
                    if is_empty(v):
                        empty[idx] = True
                    elif len(distinct[idx]) <= 15:
                        distinct[idx].add(show(v))
            for idx in keep:
                f = fields.at(idx)
                kind = field_kind(f)
                has_empty = empty[idx] or count == 0
                notes = None
                if 0 < len(distinct[idx]) <= 15:
                    notes = tr("Current values: {v}").format(v=", ".join(sorted(distinct[idx])))
                maxlen = f.length() if kind == TEXT and 0 < f.length() < 10000 else None
                field_rows.append([lyr.name(), f.name(), n if has_empty else y,
                                   tr(KIND_LABELS.get(kind, "Text")) if kind in KIND_LABELS else kind,
                                   None, n, maxlen, n, notes])
            feedback.setProgress(100 * (i + 1) / len(layers))

        book = Book()
        lh = [tr("Layer"), tr("Geometry"), tr("Required"), tr("Min features"), tr("CRS"), tr("Notes")]
        ws_l = book.sheet(tr("Layers"), lh, layer_rows, [28, 22, 12, 14, 16, 40])
        fh = [tr("Layer"), tr("Field"), tr("Required"), tr("Type"), tr("Allowed values"), tr("Unique"),
              tr("Max length"), tr("Zero counts as empty"), tr("Notes")]
        ws_f = book.sheet(tr("Fields"), fh, field_rows, [24, 22, 12, 14, 30, 10, 12, 14, 50])

        yn = DataValidation(type="list", formula1=f'"{y},{n}"', allow_blank=True)
        ws_l.add_data_validation(yn)
        yn.add(f"C2:C{max(len(layer_rows), 1) + 200}")
        geo = DataValidation(type="list", formula1='"{}"'.format(",".join(tr(v) for v in GEOM_LABELS.values())),
                             allow_blank=True)
        ws_l.add_data_validation(geo)
        geo.add(f"B2:B{max(len(layer_rows), 1) + 200}")
        yn2 = DataValidation(type="list", formula1=f'"{y},{n}"', allow_blank=True)
        ws_f.add_data_validation(yn2)
        last = max(len(field_rows), 1) + 500
        for col in ("C", "F", "H"):
            yn2.add(f"{col}2:{col}{last}")
        kinds = DataValidation(type="list", formula1='"{}"'.format(",".join(tr(v) for v in KIND_LABELS.values())),
                               allow_blank=True)
        ws_f.add_data_validation(kinds)
        kinds.add(f"D2:D{last}")

        ws_r = book.sheet(tr("Readme"))
        ws_r.sheet_view.rightToLeft = False
        ws_r.column_dimensions["A"].width = 110
        r = 1
        for block in (README_EN, README_AR):
            for k, line in enumerate(block):
                c = ws_r.cell(row=r, column=1, value=line)
                c.alignment = Alignment(wrap_text=True, vertical="top",
                                        horizontal="right" if block is README_AR else "left")
                if k == 0:
                    c.font = Font(bold=True, size=13, color=ACCENT)
                r += 1
            r += 1
        book.footer(ws_r, r)
        book.save(path)
        feedback.pushInfo(tr("Spec written for {l} layers and {f} fields.").format(l=len(layer_rows),
                                                                                   f=len(field_rows)))
        return {self.OUTPUT: path, "LAYER_COUNT": len(layer_rows), "FIELD_COUNT": len(field_rows)}


# ---- tool 2: the check ---------------------------------------------------------------------------------------
RULES = ["Missing layer", "Missing field", "Wrong field type", "Wrong geometry type", "Wrong coordinate system",
         "Too few features", "Required value missing", "Value not allowed", "Value out of range",
         "Duplicate value", "Text too long", "No geometry", "Empty geometry", "Invalid geometry"]


class DeliveryCheck(BiruniAlgorithm):
    ID = "deliverycheck"
    NAME = "Check a delivery against the spec"
    GROUP = "Engineering office"
    GROUP_ID = "office"
    HELP = ("Checks delivered layers against an Excel delivery spec (see 'Delivery spec template from layers'; "
            "English or Arabic headers): missing layers and fields, wrong field types, empty required values, "
            "values outside the allowed list or range, duplicates in unique fields, text that is too long, "
            "missing, empty or invalid geometries, the wrong geometry type or coordinate system and too few "
            "features. Every problem becomes a point on the feature (layer problems have no point) and a row "
            "in an Excel report that shows how ready each layer is: the share of its features without "
            "problems.")

    SPEC, LAYERS, OUTPUT, REPORT = "SPEC", "LAYERS", "OUTPUT", "REPORT"

    def initAlgorithm(self, config=None):  # noqa: N802
        self.addParameter(QgsProcessingParameterFile(
            self.SPEC, tr("Spec workbook"), fileFilter=tr("Excel workbook") + " (*.xlsx *.xlsm)"))
        self.addParameter(QgsProcessingParameterMultipleLayers(
            self.LAYERS, tr("Delivered layers"), Qgis.ProcessingSourceType.Vector))
        self.addParameter(QgsProcessingParameterFeatureSink(
            self.OUTPUT, tr("Delivery problems"), Qgis.ProcessingSourceType.VectorPoint))
        self.addParameter(QgsProcessingParameterFileDestination(
            self.REPORT, tr("Report"), tr("Excel workbook") + " (*.xlsx)", optional=True, createByDefault=True))

    def processAlgorithm(self, parameters, context, feedback):  # noqa: N802
        spec = read_spec(self.parameterAsFile(parameters, self.SPEC, context))
        layers = [lyr for lyr in self.parameterAsLayerList(parameters, self.LAYERS, context)
                  if isinstance(lyr, QgsVectorLayer)]
        report = self.parameterAsFileOutput(parameters, self.REPORT, context)

        out_crs = next((lyr.crs() for lyr in layers if lyr.isSpatial() and lyr.crs().isValid()),
                       QgsCoordinateReferenceSystem("EPSG:4326"))
        of = QgsFields()
        for name, t in (("layer", QMetaType.Type.QString), ("fid", QMetaType.Type.LongLong),
                        ("field", QMetaType.Type.QString), ("rule", QMetaType.Type.QString),
                        ("value", QMetaType.Type.QString), ("message", QMetaType.Type.QString)):
            of.append(QgsField(name, t))
        sink, sink_id = self.parameterAsSink(parameters, self.OUTPUT, context, of, Qgis.WkbType.Point, out_crs)

        problems = []  # (layer, fid, field, rule, value, message, point)

        def problem(layer, fid, field, rule, value, message, point=None):
            problems.append((layer, fid, field, rule, value, message, point))

        matched = {}
        for lyr in layers:
            names = layer_names(lyr)
            for key in spec:
                if key in names and key not in matched:
                    matched[key] = lyr
                    break
            else:
                feedback.pushInfo(tr("{layer} is not in the spec - not checked.").format(layer=lyr.name()))

        per_layer = []  # [name, found, features, features with problems, layer problems]
        total = max(len(spec), 1)
        for n, (key, lr) in enumerate(spec.items()):
            if feedback.isCanceled():
                return {}
            lyr = matched.get(key)
            if lyr is None:
                if lr.required:
                    problem(lr.layer, None, None, "Missing layer", None,
                            tr("The layer {layer} is not in the delivery.").format(layer=lr.layer))
                per_layer.append([lr.layer, False, 0, 0, 1 if lr.required else 0])
                continue
            feedback.pushInfo(tr("Checking {layer}…").format(layer=lyr.name()))
            bad, layer_issues = self._check_layer(lr, lyr, out_crs, context, feedback, problem)
            per_layer.append([lr.layer, True, lyr.featureCount(), len(bad), layer_issues])
            feedback.setProgress(100 * (n + 1) / total)

        for layer, fid, field, rule, value, message, point in problems:
            f = QgsFeature(of)
            if point is not None:
                f.setGeometry(point)
            f.setAttributes([layer, fid, field, tr(rule), value, message])
            sink.addFeature(f, QgsFeatureSink.Flag.FastInsert)

        features = sum(r[2] for r in per_layer)
        clean = sum(r[2] - r[3] for r in per_layer)
        readiness = round(100.0 * clean / features, 1) if features else 0.0
        layer_problems = sum(r[4] for r in per_layer)
        if report:
            self._report(report, spec, per_layer, problems, readiness)
        feedback.pushInfo(tr("{n} problems found; {r}% of the features are ready.").format(
            n=len(problems), r=readiness))
        return {self.OUTPUT: sink_id, self.REPORT: report, "PROBLEM_COUNT": len(problems),
                "LAYER_PROBLEMS": layer_problems, "READINESS": readiness}

    def _check_layer(self, lr, lyr, out_crs, context, feedback, problem):
        layer_issues = 0

        def layer_problem(rule, message, field=None, value=None):
            nonlocal layer_issues
            layer_issues += 1
            problem(lr.layer, None, field, rule, value, message)

        gtype = lyr.geometryType() if lyr.isSpatial() else Qgis.GeometryType.Null
        if lr.geometry is not None and lr.geometry != gtype:
            layer_problem("Wrong geometry type", tr("The geometry is {got}, the spec asks for {want}.").format(
                got=tr(GEOM_LABELS.get(gtype, "No geometry (table)")), want=tr(GEOM_LABELS[lr.geometry])),
                value=tr(GEOM_LABELS.get(gtype, "No geometry (table)")))
        if lr.crs is not None and lyr.isSpatial() and not crs_ok(lr.crs, lyr.crs()):
            want = lr.crs.authid() if isinstance(lr.crs, QgsCoordinateReferenceSystem) else lr.crs
            layer_problem("Wrong coordinate system", tr("The coordinate system is {got}, the spec asks for "
                                                        "{want}.").format(got=lyr.crs().authid() or "?",
                                                                          want=want),
                          value=lyr.crs().authid())
        count = lyr.featureCount()
        if lr.mincount and count < lr.mincount:
            layer_problem("Too few features", tr("{n} features, the spec asks for at least {m}.").format(
                n=count, m=lr.mincount), value=str(count))

        fields = lyr.fields()
        by_name = {norm(fields.at(i).name()): i for i in range(fields.count())}
        for i in range(fields.count()):
            alias = fields.at(i).alias()
            if alias:
                by_name.setdefault(norm(alias), i)
        checks = []  # (FieldRule, index)
        for fr in lr.fields:
            idx = by_name.get(norm(fr.field))
            if idx is None:
                layer_problem("Missing field", tr("The field {field} is missing.").format(field=fr.field),
                              field=fr.field)
                continue
            actual = field_kind(fields.at(idx))
            if not kind_ok(fr.kind, actual):
                layer_problem("Wrong field type", tr("The field {field} is {got}, the spec asks for {want}.")
                              .format(field=fr.field, got=tr(KIND_LABELS.get(actual, str(actual))),
                                      want=tr(KIND_LABELS.get(fr.kind, "Number"))),
                              field=fr.field, value=tr(KIND_LABELS.get(actual, str(actual))))
            checks.append((fr, idx))

        xform = None
        if lyr.isSpatial() and lyr.crs().isValid() and lyr.crs() != out_crs:
            xform = QgsCoordinateTransform(lyr.crs(), out_crs, context.transformContext())

        bad = set()
        seen = defaultdict(list)  # (field index, normalised value) -> [(fid, value, point)]
        total = max(count, 1)
        for k, feat in enumerate(lyr.getFeatures()):
            if feedback.isCanceled():
                break
            fid = feat.id()
            point = None
            if lyr.isSpatial():
                g = feat.geometry()
                if g is None or g.isNull():
                    problem(lr.layer, fid, None, "No geometry", None, tr("The feature has no geometry."))
                    bad.add(fid)
                elif g.isEmpty():
                    problem(lr.layer, fid, None, "Empty geometry", None, tr("The geometry is empty."))
                    bad.add(fid)
                else:
                    point = self._marker(g, xform)
                    if not g.isGeosValid():
                        errors = g.validateGeometry(Qgis.GeometryValidationEngine.Geos)
                        what = errors[0].what() if errors else ""
                        problem(lr.layer, fid, None, "Invalid geometry", None,
                                tr("The geometry is invalid: {e}").format(e=what), point)
                        bad.add(fid)
            attrs = feat.attributes()
            for fr, idx in checks:
                v = attrs[idx]
                if is_empty(v, fr.zero):
                    if fr.required:
                        problem(lr.layer, fid, fr.field, "Required value missing", show(v),
                                tr("{field} is required but empty.").format(field=fr.field), point)
                        bad.add(fid)
                    continue
                if fr.allowed is not None:
                    fail = value_allowed(fr.allowed, v)
                    if fail:
                        msg = (tr("{value} is not one of the allowed values of {field}.") if fail ==
                               "Value not allowed" else tr("{value} is outside the allowed range of {field}."))
                        problem(lr.layer, fid, fr.field, fail, show(v), msg.format(value=show(v), field=fr.field),
                                point)
                        bad.add(fid)
                if fr.maxlen and len(show(v)) > fr.maxlen:
                    problem(lr.layer, fid, fr.field, "Text too long", show(v),
                            tr("{field} has {n} characters, at most {m} are allowed.").format(
                                field=fr.field, n=len(show(v)), m=fr.maxlen), point)
                    bad.add(fid)
                if fr.unique:
                    x = as_number(v) if not isinstance(v, str) else None
                    seen[(idx, x if x is not None else show(v).strip().casefold())].append((fid, show(v), point))
            if k % 500 == 0:
                feedback.setProgress(100 * k / total)

        fields_by_idx = {idx: fr for fr, idx in checks}
        for (idx, _key), rows in seen.items():
            if len(rows) < 2:
                continue
            fr = fields_by_idx[idx]
            for fid, value, point in rows:
                problem(lr.layer, fid, fr.field, "Duplicate value", value,
                        tr("{value} appears {n} times in {field}, which must be unique.").format(
                            value=value, n=len(rows), field=fr.field), point)
                bad.add(fid)
        return bad, layer_issues

    @staticmethod
    def _marker(g, xform):
        p = g.pointOnSurface()
        if p is None or p.isNull() or p.isEmpty():
            p = g.centroid()
        if p is None or p.isNull() or p.isEmpty():
            p = QgsGeometry.fromPointXY(g.boundingBox().center())
        if QgsWkbTypes.isMultiType(p.wkbType()):
            p = QgsGeometry.fromPointXY(p.asMultiPoint()[0])
        if xform is not None:
            try:
                p.transform(xform)
            except Exception:
                return None
        return p

    @staticmethod
    def _report(path, spec, per_layer, problems, readiness):
        book = Book()
        ws = book.sheet(tr("Summary"))
        ws.column_dimensions["A"].width = 32
        for col in "BCDEF":
            ws.column_dimensions[col].width = 18
        book.title(ws, 1, tr("Delivery check"), 16)
        ws.cell(row=2, column=1, value=tr("Features ready"))
        ws.cell(row=2, column=2, value=f"{readiness}%")
        ws.cell(row=3, column=1, value=tr("Problems"))
        ws.cell(row=3, column=2, value=len(problems))
        rows = []
        for name, found, n, nbad, issues in per_layer:
            ready = round(100.0 * (n - nbad) / n, 1) if n else 0.0
            status = (tr("Missing") if not found else tr("Not ready") if issues or nbad else tr("Ready"))
            rows.append([name, status, n, nbad, f"{ready}%", issues])
        r = book.table(ws, 5, [tr("Layer"), tr("Status"), tr("Features"), tr("Features with problems"),
                               tr("Ready"), tr("Layer problems")], rows)
        counts = Counter(p[3] for p in problems)
        r = book.table(ws, r, [tr("Rule"), tr("Problems")],
                       [[tr(rule), counts[rule]] for rule in RULES if counts[rule]])
        book.footer(ws, r)
        book.sheet(tr("Problems"), [tr("Layer"), tr("Feature id"), tr("Field"), tr("Rule"), tr("Value"),
                                    tr("Message")],
                   [[p[0], p[1], p[2], tr(p[3]), p[4], p[5]] for p in problems], [24, 12, 20, 24, 24, 60])
        book.save(path)
