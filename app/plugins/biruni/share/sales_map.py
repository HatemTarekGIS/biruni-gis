"""Plot sales map: one HTML page for a developer's sales team.

The plots of a plan are coloured by their status (available, reserved, sold or
any status the developer uses), with counts, a search by plot number, filters on
status, area and price, and a card per plot with a WhatsApp / call button that
already says which plot the buyer asks about. No server: the page opens from a
file, on a phone too.

An "editor copy" of the page lets one sales coordinator change statuses, prices
and notes in the browser and download the result as a small statuses file;
Biruni reads that file back into the plots layer. Files that come back are
untrusted: they are checked against a strict format, and every text is escaped
when it is shown.
"""
import base64
import html
import json
import math
import os
import re
import uuid
from datetime import datetime, timezone

from qgis.core import (Qgis, QgsCoordinateReferenceSystem, QgsCoordinateTransform, QgsDistanceArea, QgsField,
                       QgsGeometry, QgsProject, QgsRectangle, QgsVectorLayer, QgsWkbTypes)
from qgis.PyQt.QtCore import QMetaType

from .. import i18n
from ..arabic_text import normalise
from ..i18n import tr
from ..version import APP_NAME
from .webmap import BASEMAPS, WEB_DIR

FORMAT = "biruni-sales-status"
PAGE_FORMAT = "biruni-sales-map"
VERSION = 1
MAX_FILE_BYTES = 2 * 1024 * 1024
MAX_PLOTS = 100000
MAX_NO = 40
MAX_LABEL = 40
MAX_NOTES = 1000
MAX_PRICE = 1e13
ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f‪-‮⁦-⁩]")
_DIGITS = str.maketrans("٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹", "01234567890123456789")
# words before a plot number ("قطعة 15", "Plot no 15"), only when a space or the digits follow them
PREFIX_RE = re.compile(r"^(?:رقم القطعه|قطعه رقم|القطعه|قطعه|رقم|plot no|plot|lot|parcel|no)(?:\s+|(?=\d))")
_NEGATIONS = ("غير", "لا", "ليس", "not", "no")

STATUS_KEYS = ("available", "reserved", "sold")
UNKNOWN = "unknown"
COLORS = {"available": "#2e9d5b", "reserved": "#e8901c", "sold": "#d64545", UNKNOWN: "#9aa5b1"}
CUSTOM_COLORS = ("#2f7ed8", "#8e5bd0", "#1aa3a3", "#c2569b", "#7a8b2a", "#b5651d")
STATUS_WORDS = {
    "available": ("متاح", "متاحه", "متوفر", "متوفره", "للبيع", "شاغر", "شاغره", "فاضي", "فاضيه", "available", "free",
                  "for sale", "open", "vacant", "avail", "unsold"),
    "reserved": ("محجوز", "محجوزه", "حجز", "تحت الحجز", "مربوط", "reserved", "hold", "on hold", "booked", "pending",
                 "under offer", "under contract"),
    "sold": ("مباع", "مباعه", "مبيوع", "مبيوعه", "بيع", "تم البيع", "منتهي", "sold", "sold out", "closed"),
}
HEADERS = {  # what a column of the statuses sheet may be called (compared after Arabic normalisation)
    "no": ("رقم القطعه", "رقم القطع", "القطعه", "قطعه", "رقم", "plot no", "plot number", "plot", "parcel", "number",
           "no", "id", "lot", "unit"),
    "status": ("الحاله", "حاله", "حالة البيع", "status", "state", "sale status"),
    "price": ("السعر", "سعر", "سعر البيع", "القيمه", "المبلغ", "price", "cost", "value", "amount"),
    "area": ("المساحه", "مساحه", "المسطح", "area", "size", "m2"),
    "notes": ("ملاحظات", "ملاحظه", "الملاحظات", "notes", "note", "remarks", "comments", "comment"),
}
WGS84 = QgsCoordinateReferenceSystem("EPSG:4326")

SATELLITE = {"url": "https://tiles.maps.eox.at/wmts/1.0.0/s2cloudless_3857/default/g/{z}/{y}/{x}.jpg",
             "maxZoom": 19, "maxNativeZoom": 14,
             "attribution": '<a href="https://s2maps.eu">Sentinel-2 cloudless - https://s2maps.eu</a> by EOX IT '
                            'Services GmbH (Contains modified Copernicus Sentinel data 2016)'}


class SalesFileError(ValueError):
    """A statuses file that cannot be read at all."""


def status_labels():
    return {"available": tr("Available"), "reserved": tr("Reserved"), "sold": tr("Sold"), UNKNOWN: tr("Not set")}


def clean_text(value, limit):
    """Outside text: no control or direction-override characters, at most `limit` long. (text, was_cut)."""
    if value is None:
        return "", False
    value = CONTROL_RE.sub("", str(value)).strip()
    return (value[:limit], True) if len(value) > limit else (value, False)


def plot_key(value):
    """The key a plot number is matched by: '١٥', '015', 'قطعة 15' and 'Plot 15' are all '15'."""
    if value is None:
        return ""
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    s = normalise(str(value))
    s = PREFIX_RE.sub("", s)
    s = s.replace(" ", "")
    if s.isdigit():
        s = str(int(s))
    return s


def status_key(value):
    """(key, label) of a status written in any way: a standard key, a custom one ('x:<text>') or unknown."""
    if value is None:
        return UNKNOWN, status_labels()[UNKNOWN]
    raw, _ = clean_text(value, MAX_LABEL)
    if not raw:
        return UNKNOWN, status_labels()[UNKNOWN]
    n = normalise(raw)
    for key, words in STATUS_WORDS.items():
        if n in words or n == key:
            return key, status_labels()[key]
    # "غير متاح", "not available", "unsold": a word inside does not decide it
    negated = n.split(" ")[0] in _NEGATIONS or n.startswith(("un", "non"))
    for key, words in STATUS_WORDS.items():
        if not negated and any(w in n for w in words if len(w) > 3):
            return key, status_labels()[key]
    labels = status_labels()
    for key, label in labels.items():
        if normalise(label) == n:
            return key, label
    return "x:" + n, raw


def parse_price(value):
    """A price from a number or text ('1,250,000', '١٬٢٥٠٬٠٠٠ ريال'); None when there is none."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        v = float(value)
    else:
        s = str(value).translate(_DIGITS).replace("٬", ",").replace("٫", ".")
        s = re.sub(r"[^\d.,-]", "", s)
        if s.count(",") and s.count(".") <= 1:
            s = s.replace(",", "")
        try:
            v = float(s)
        except ValueError:
            return None
    return v if math.isfinite(v) and 0 <= v <= MAX_PRICE else None


def sales_phone(text):
    """International digits for wa.me / tel: ('+966 50 123 4567' -> '966501234567'); '' when not a phone."""
    s = str(text or "").translate(_DIGITS)
    digits = re.sub(r"\D", "", s)
    if digits.startswith("00"):
        digits = digits[2:]
    return digits if 8 <= len(digits) <= 15 else ""


# ---- the statuses sheet -------------------------------------------------------------------------------------------

def _match_header(name):
    n = normalise(name)
    if not n:
        return None
    for key, words in HEADERS.items():  # exact first: "رقم" is the number, "سعر البيع" is the price
        if n in words:
            return key
    for key, words in HEADERS.items():
        if any(len(w) > 2 and w in n for w in words):
            return key
    return None


def read_status_table(path, sheet=None):
    """Rows of an Excel / CSV statuses sheet keyed by plot. Returns (rows {key: row}, columns {role: header},
    problems). A row is {"no", "status", "label", "price", "area", "notes"}."""
    from ..smart_table import read_rows

    rows = read_rows(path, sheet)
    header_row, columns = None, {}
    for i, r in enumerate(rows[:15]):  # the header may sit a few rows down under a title
        found = {}
        for j, cell in enumerate(r):
            role = _match_header(cell) if isinstance(cell, str) else None
            if role and role not in found:
                found[role] = j
        if "no" in found and len(found) >= 2:
            header_row, columns = i, found
            break
    if header_row is None:
        raise SalesFileError(tr("The sheet needs a plot number column and a status (or price) column."))
    out, problems = {}, []
    for k, r in enumerate(rows[header_row + 1:], header_row + 2):
        def cell(role):
            j = columns.get(role)
            return r[j] if j is not None and j < len(r) else None

        no = cell("no")
        key = plot_key(no)
        if not key:
            continue
        status, label = status_key(cell("status")) if "status" in columns else (None, None)
        notes, _ = clean_text(cell("notes"), MAX_NOTES)
        area = parse_price(cell("area"))
        item = {"no": clean_text(no, MAX_NO)[0], "status": status, "label": label, "price": parse_price(cell("price")),
                "area": area, "notes": notes}
        if key in out:
            problems.append(tr("Row {r}: plot {n} appears twice; the last row is used.").format(r=k, n=item["no"]))
        out[key] = item
    headers = {role: str(rows[header_row][j]) for role, j in columns.items()}
    return out, headers, problems


# ---- collecting the plots -------------------------------------------------------------------------------------------

class SalesData:
    def __init__(self):
        self.plots = []            # dicts for the page
        self.counts = {}           # status key -> count
        self.labels = {}           # status key -> label
        self.unmatched = []        # sheet rows with no plot on the map
        self.without_status = []   # plots without a status
        self.problems = []         # notes about the sheet
        self.bounds = QgsRectangle()
        self.bounds.setNull()


def _value(feature, field):
    if not field:
        return None
    v = feature[field]
    return None if v is None or (hasattr(v, "isNull") and v.isNull()) else v


def collect(layer, number_field, status_field=None, price_field=None, area_field=None, notes_field=None, table=None,
            sheet=None, project=None):
    """The plots of `layer` with their number, status, price, area and notes, from the layer's fields and / or a
    statuses sheet (the sheet wins where it has a value)."""
    if not isinstance(layer, QgsVectorLayer) or layer.geometryType() != Qgis.GeometryType.Polygon:
        raise ValueError(tr("Choose a polygon layer with the plots."))
    if number_field not in layer.fields().names():
        raise ValueError(tr("Choose the field with the plot numbers."))
    project = project or QgsProject.instance()
    sheet_rows, problems = {}, []
    if table:
        sheet_rows, _, problems = read_status_table(table, sheet)
    xform = QgsCoordinateTransform(layer.crs(), WGS84, project.transformContext())
    da = QgsDistanceArea()
    da.setSourceCrs(layer.crs(), project.transformContext())
    da.setEllipsoid(layer.crs().ellipsoidAcronym() or "EPSG:7030")
    data = SalesData()
    data.labels = dict(status_labels())
    seen = set()
    for f in layer.getFeatures():
        g = f.geometry()
        if g is None or g.isNull() or g.isEmpty():
            continue
        number = _value(f, number_field)
        if number is None or str(number).strip() == "":
            continue
        key = plot_key(number)
        row = sheet_rows.get(key, {})
        seen.add(key)
        status, label = status_key(_value(f, status_field)) if status_field else (UNKNOWN, data.labels[UNKNOWN])
        if row.get("status") and row["status"] != UNKNOWN:
            status, label = row["status"], row["label"]
        price = row.get("price") if row.get("price") is not None else parse_price(_value(f, price_field))
        area = row.get("area") if row.get("area") is not None else parse_price(_value(f, area_field))
        if area is None:
            area = da.measureArea(g)
        notes = row.get("notes") or clean_text(_value(f, notes_field), MAX_NOTES)[0]
        geom = QgsGeometry(g)
        if QgsWkbTypes.isCurvedType(geom.wkbType()):
            geom = QgsGeometry(geom.constGet().segmentize())
        try:
            geom.transform(xform)
        except Exception:  # noqa: BLE001
            continue
        data.bounds.combineExtentWith(geom.boundingBox())
        no_text = clean_text(str(int(number)) if isinstance(number, float) and number.is_integer() else number,
                             MAX_NO)[0]
        data.plots.append({"n": no_text, "k": key, "s": status, "p": price,
                           "a": round(area, 1) if area is not None else None, "t": notes,
                           "g": json.loads(geom.asJson(7))["coordinates"],
                           "m": geom.isMultipart()})
        data.counts[status] = data.counts.get(status, 0) + 1
        data.labels.setdefault(status, label)
        if status == UNKNOWN:
            data.without_status.append(no_text)
    data.unmatched = [r["no"] for k, r in sheet_rows.items() if k not in seen]
    data.problems = problems
    return data


def _status_list(data):
    order = [k for k in STATUS_KEYS] + sorted(k for k in data.labels if k.startswith("x:"))
    if data.counts.get(UNKNOWN) or UNKNOWN not in order:
        order.append(UNKNOWN)
    out, n = [], 0
    for key in order:
        if key.startswith("x:"):
            color = CUSTOM_COLORS[n % len(CUSTOM_COLORS)]
            n += 1
        else:
            color = COLORS[key]
        out.append({"key": key, "label": data.labels.get(key, key), "color": color})
    return out


def _page_text():
    return {
        "made": tr("Made with {app}").format(app=APP_NAME), "search": tr("Plot number…"), "go": tr("Find"),
        "filters": tr("Filters"), "area": tr("Area (m²)"), "price": tr("Price"), "from": tr("from"), "to": tr("to"),
        "reset": tr("Show all"), "plot": tr("Plot"), "status": tr("Status"), "notes": tr("Notes"),
        "whatsapp": tr("Ask on WhatsApp"), "call": tr("Call"), "ask": tr("Inquiry about plot number {n}"),
        "not_found": tr("No plot with this number."), "shown": tr("{n} of {t} plots shown"),
        "map": tr("Map"), "satellite": tr("Satellite"), "nobase": tr("No basemap"),
        "offline": tr("The background map needs the internet - the plots still work."),
        "edit": tr("Change"), "save": tr("Save"), "cancel": tr("Cancel"), "close": tr("Close"),
        "download_json": tr("Download statuses (for Biruni)"), "download_csv": tr("Download as Excel (CSV)"),
        "open_file": tr("Open statuses file"), "changed": tr("{n} plots changed"),
        "unsaved": tr("There are changes that were not downloaded yet."), "loaded": tr("{n} statuses loaded."),
        "bad_file": tr("This is not a statuses file."),
        "other_map": tr("These statuses were made for another map. Load them anyway?"),
        "editor_note": tr("Editor copy: changes stay in this browser until you download them. Send the downloaded "
                          "file to whoever updates the plan in Biruni."),
        "no_storage": tr("This browser does not keep changes if you close the page: download them before closing."),
        "m2": tr("m²"), "legend": tr("Legend"), "all": tr("All"), "number": tr("Plot number"),
    }


def _write_page(path, payload, title):
    blob = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).replace("<", "\\u003c")
    with open(os.path.join(WEB_DIR, "leaflet.js"), encoding="utf-8") as f:
        leaflet_js = f.read()
    with open(os.path.join(WEB_DIR, "leaflet.css"), encoding="utf-8") as f:
        leaflet_css = f.read()
    for img in ("layers-2x.png", "layers.png"):
        with open(os.path.join(WEB_DIR, img), "rb") as f:
            uri = "data:image/png;base64," + base64.b64encode(f.read()).decode("ascii")
        leaflet_css = leaflet_css.replace("images/" + img, uri)
    page = (TEMPLATE
            .replace("%%LANG%%", "ar" if i18n.is_rtl() else i18n.lang() or "en")
            .replace("%%DIR%%", "rtl" if i18n.is_rtl() else "ltr")
            .replace("%%TITLE%%", html.escape(title or tr("Plot sales map")))
            .replace("%%LEAFLET_CSS%%", leaflet_css)
            .replace("%%LEAFLET_JS%%", leaflet_js.replace("</script", "<\\/script"))
            .replace("%%DATA%%", blob))
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(page)
    return path


def editor_path(path):
    base, ext = os.path.splitext(path)
    return f"{base} - {tr('editor')}{ext or '.html'}"


class SalesMapResult:
    def __init__(self, path, editor, data, map_id, phone_ok):
        self.path, self.editor_path, self.data, self.map_id, self.phone_ok = path, editor, data, map_id, phone_ok

    def summary(self):
        d = self.data
        lines = [tr("{n} plots on the map.").format(n=len(d.plots))]
        lines += ["  " + d.labels.get(k, k) + ": " + str(n) for k, n in sorted(d.counts.items(), key=lambda kv: -kv[1])]
        if d.without_status:
            lines.append(tr("{n} plots have no status: {v}").format(n=len(d.without_status),
                                                                    v=", ".join(d.without_status[:15])))
        if d.unmatched:
            lines.append(tr("{n} rows of the sheet match no plot on the map: {v}").format(
                n=len(d.unmatched), v=", ".join(str(x) for x in d.unmatched[:15])))
        if not self.phone_ok:
            lines.append(tr("No valid sales phone number: the WhatsApp and call buttons are hidden."))
        return "\n".join(lines)


def export_sales_map(layer, path, number_field, status_field=None, price_field=None, area_field=None,
                     notes_field=None, table=None, sheet=None, title="", phone="", currency="", basemap="osm",
                     show_prices=True, editor=False, map_id=None, project=None):
    """Write the sales page (and, with editor=True, the editor copy next to it). Returns a SalesMapResult."""
    data = collect(layer, number_field, status_field, price_field, area_field, notes_field, table, sheet, project)
    if not data.plots:
        raise ValueError(tr("The layer has no plots with a number."))
    map_id = map_id if map_id and ID_RE.match(map_id) else uuid.uuid4().hex[:16]
    digits = sales_phone(phone)
    plots = data.plots if show_prices else [dict(p, p=None) for p in data.plots]
    b = data.bounds
    bases = {"osm": BASEMAPS["osm"], "satellite": SATELLITE}
    payload = {
        "format": PAGE_FORMAT, "version": VERSION, "map_id": map_id, "title": title or "",
        "phone": digits, "currency": clean_text(currency, 12)[0], "editor": False, "prices": bool(show_prices),
        "statuses": _status_list(data), "plots": plots, "basemaps": bases,
        "basemap": basemap if basemap in bases else "none", "rtl": i18n.is_rtl(),
        "bounds": None if b.isNull() else [[b.yMinimum(), b.xMinimum()], [b.yMaximum(), b.xMaximum()]],
        "limits": {"no": MAX_NO, "label": MAX_LABEL, "notes": MAX_NOTES, "file": MAX_FILE_BYTES},
        "status_format": FORMAT, "text": _page_text(),
    }
    _write_page(path, payload, title)
    epath = None
    if editor:
        epath = editor_path(path)
        _write_page(epath, dict(payload, editor=True, plots=data.plots, prices=True), title)
    return SalesMapResult(path, epath, data, map_id, bool(digits))


# ---- statuses coming back --------------------------------------------------------------------------------------------

def parse_status_file(path):
    """(meta, items, warnings) of a statuses file downloaded from the editor page; or the rows of an Excel/CSV
    sheet. Raises SalesFileError when it is neither."""
    name = os.path.basename(path)
    ext = os.path.splitext(path)[1].lower()
    if ext in (".csv", ".xlsx", ".xlsm", ".xls", ".ods"):
        rows, headers, problems = read_status_table(path)
        items = [{"key": k, "no": r["no"], "status": r["status"], "label": r["label"], "price": r["price"],
                  "notes": r["notes"], "has_notes": "notes" in headers, "has_price": "price" in headers}
                 for k, r in rows.items()]
        return {"map_id": "", "file": name}, items, problems
    try:
        size = os.path.getsize(path)
    except OSError as e:
        raise SalesFileError(tr("{f}: the file could not be opened ({e}).").format(f=name, e=e)) from e
    if size > MAX_FILE_BYTES:
        raise SalesFileError(tr("{f}: the file is too large for a statuses file.").format(f=name))
    try:
        with open(path, "rb") as f:
            data = json.loads(f.read().decode("utf-8-sig"))
    except (UnicodeDecodeError, ValueError) as e:
        raise SalesFileError(tr("{f}: this is not a statuses file.").format(f=name)) from e
    if not isinstance(data, dict) or data.get("format") != FORMAT:
        raise SalesFileError(tr("{f}: this is not a statuses file.").format(f=name))
    version = data.get("version")
    if not isinstance(version, int) or isinstance(version, bool) or version < 1:
        raise SalesFileError(tr("{f}: this is not a statuses file.").format(f=name))
    if version > VERSION:
        raise SalesFileError(tr("{f}: made by a newer version of {app} - update {app} first.")
                             .format(f=name, app=APP_NAME))
    plots = data.get("plots")
    if not isinstance(plots, list):
        raise SalesFileError(tr("{f}: this is not a statuses file.").format(f=name))
    warnings = []
    if len(plots) > MAX_PLOTS:
        warnings.append(tr("{f}: only the first {n} plots were read.").format(f=name, n=MAX_PLOTS))
        plots = plots[:MAX_PLOTS]
    map_id = data.get("map_id") if isinstance(data.get("map_id"), str) and ID_RE.match(data.get("map_id")) else ""
    items, seen = [], set()
    labels = status_labels()
    for k, p in enumerate(plots, 1):
        if not isinstance(p, dict) or not isinstance(p.get("n"), (str, int)) or isinstance(p.get("n"), bool):
            warnings.append(tr("{f}: plot {n} was skipped (not readable).").format(f=name, n=k))
            continue
        no, _ = clean_text(p["n"], MAX_NO)
        key = plot_key(no)
        s = p.get("s")
        if not key or not isinstance(s, str):
            warnings.append(tr("{f}: plot {n} was skipped (not readable).").format(f=name, n=k))
            continue
        if s in STATUS_KEYS or s == UNKNOWN:
            status, label = s, labels[s]
        elif s.startswith("x:"):
            label, _ = clean_text(p.get("l") if isinstance(p.get("l"), str) else s[2:], MAX_LABEL)
            if not label:
                warnings.append(tr("{f}: plot {n} was skipped (not readable).").format(f=name, n=k))
                continue
            status = "x:" + normalise(label)
        else:
            warnings.append(tr("{f}: plot {n} was skipped (not readable).").format(f=name, n=k))
            continue
        price = p.get("p")
        if price is not None and (isinstance(price, bool) or not isinstance(price, (int, float))
                                  or not math.isfinite(price) or not 0 <= price <= MAX_PRICE):
            warnings.append(tr("{f}: the price of plot {n} was ignored.").format(f=name, n=no))
            price = None
        notes = p.get("t")
        notes, cut = clean_text(notes, MAX_NOTES) if isinstance(notes, str) else ("", False)
        if cut:
            warnings.append(tr("{f}: the notes of plot {n} were too long and were shortened.").format(f=name, n=no))
        if key in seen:
            continue
        seen.add(key)
        items.append({"key": key, "no": no, "status": status, "label": label,
                      "price": float(price) if price is not None else None, "notes": notes,
                      "has_notes": "t" in p, "has_price": "p" in p})
    return {"map_id": map_id, "file": name}, items, warnings


class StatusImportResult:
    def __init__(self):
        self.updated = 0
        self.unchanged = 0
        self.not_found = []
        self.warnings = []
        self.map_id = ""

    def summary(self):
        lines = [tr("{u} plots updated, {s} unchanged.").format(u=self.updated, s=self.unchanged)]
        if self.not_found:
            lines.append(tr("{n} plots of the file are not in the layer: {v}").format(
                n=len(self.not_found), v=", ".join(self.not_found[:15])))
        return "\n".join(lines)


def _ensure_field(layer, name, typ):
    if not name:
        return -1
    idx = layer.fields().indexOf(name)
    if idx >= 0:
        return idx
    if not layer.dataProvider().addAttributes([QgsField(name, typ)]):
        raise ValueError(tr("The field {f} could not be added to the layer.").format(f=name))
    layer.updateFields()
    return layer.fields().indexOf(name)


def import_sales_status(layer, path, number_field, status_field, price_field=None, notes_field=None, commit=True,
                        expected_map_id=None):
    """Write the statuses (and prices / notes) of a statuses file or sheet into the plots layer. Missing status,
    price or notes fields are added. Changes go through the layer's edit buffer (undoable); they are saved unless
    the layer was already being edited or commit is False."""
    meta, items, warnings = parse_status_file(path)
    result = StatusImportResult()
    result.warnings = list(warnings)
    result.map_id = meta.get("map_id", "")
    if expected_map_id and result.map_id and result.map_id != expected_map_id:
        result.warnings.append(tr("This file was made for another sales map."))
    if number_field not in layer.fields().names():
        raise ValueError(tr("Choose the field with the plot numbers."))
    was_editing = layer.isEditable()
    if not was_editing:
        s_idx = _ensure_field(layer, status_field, QMetaType.Type.QString)
        p_idx = _ensure_field(layer, price_field, QMetaType.Type.Double)
        n_idx = _ensure_field(layer, notes_field, QMetaType.Type.QString)
        if not layer.startEditing():
            raise ValueError(tr("The layer cannot be edited."))
    else:
        s_idx = layer.fields().indexOf(status_field) if status_field else -1
        p_idx = layer.fields().indexOf(price_field) if price_field else -1
        n_idx = layer.fields().indexOf(notes_field) if notes_field else -1
        if status_field and s_idx < 0:
            raise ValueError(tr("Stop editing the layer first, so the status field can be added."))
    by_key = {}
    for f in layer.getFeatures():
        by_key.setdefault(plot_key(f[number_field]), []).append(f)
    price_numeric = p_idx >= 0 and layer.fields().at(p_idx).isNumeric()

    def fit(idx, value):
        """A text cut to the field's length (a shapefile field holds 254 characters)."""
        if value is None or idx < 0:
            return value
        length = layer.fields().at(idx).length()
        if isinstance(value, str) and length > 0 and len(value) > length:
            result.warnings.append(tr("A text longer than the field {f} ({n} characters) was shortened.")
                                   .format(f=layer.fields().at(idx).name(), n=length))
            return value[:length]
        return value

    layer.beginEditCommand(tr("Plot statuses from {f}").format(f=meta.get("file", "")))
    try:
        for it in items:
            feats = by_key.get(it["key"])
            if not feats:
                result.not_found.append(it["no"])
                continue
            for f in feats:
                changes = {}
                if s_idx >= 0 and it["status"] and it["status"] != UNKNOWN:
                    changes[s_idx] = fit(s_idx, it["label"])
                if p_idx >= 0 and it["price"] is not None:
                    changes[p_idx] = it["price"] if price_numeric else fit(p_idx, f"{it['price']:g}")
                if n_idx >= 0 and it.get("has_notes"):
                    changes[n_idx] = fit(n_idx, it["notes"] or None)
                changes = {i: v for i, v in changes.items() if f.attribute(i) != v}
                if not changes:
                    result.unchanged += 1
                    continue
                for i, v in changes.items():
                    layer.changeAttributeValue(f.id(), i, v)
                result.updated += 1
        layer.endEditCommand()
    except Exception:
        layer.destroyEditCommand()
        raise
    if not was_editing and commit:
        if not layer.commitChanges():
            errors = "; ".join(layer.commitErrors())
            layer.rollBack()
            raise ValueError(tr("The changes could not be saved: {e}").format(e=errors))
    return result


def status_file_payload(items, map_id=""):
    """A statuses file as the editor page writes it (used by tests and by 'save statuses from the layer')."""
    return {"format": FORMAT, "version": VERSION, "map_id": map_id,
            "exported": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), "plots": items}


TEMPLATE = r"""<!doctype html>
<html lang="%%LANG%%" dir="%%DIR%%">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="generator" content="Biruni GIS">
<title>%%TITLE%%</title>
<style>%%LEAFLET_CSS%%</style>
<style>
html,body{height:100%;margin:0;font-family:"Segoe UI",Tahoma,Arial,sans-serif;background:#f4f6f8;color:#1d2733}
#top{position:absolute;top:0;left:0;right:0;z-index:1000;background:#14365a;color:#fff;padding:8px 12px;
 box-shadow:0 1px 4px rgba(0,0,0,.25)}
#title{font-size:17px;font-weight:600;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
#row{display:flex;flex-wrap:wrap;gap:6px;align-items:center;margin-top:6px}
#q{flex:0 1 150px;min-width:90px;padding:5px 8px;border-radius:6px;border:0;font-size:14px}
.btn{border:0;border-radius:6px;padding:6px 10px;font-size:13px;cursor:pointer;background:#e9eef4;color:#14365a}
.btn.main{background:#2f7ed8;color:#fff}.btn:disabled{opacity:.5}
.chip{border:0;border-radius:14px;padding:4px 10px;font-size:13px;cursor:pointer;color:#fff;opacity:1}
.chip.off{opacity:.35}
#map{position:absolute;top:84px;bottom:0;left:0;right:0;direction:ltr}
#filters,#card,#editbar{position:absolute;z-index:1001;background:#fff;border-radius:10px;
 box-shadow:0 2px 12px rgba(0,0,0,.3);padding:12px;font-size:14px}
#filters{top:92px;inset-inline-end:10px;width:250px;display:none}
#filters label{display:block;margin:6px 0 2px;color:#5b6b7b;font-size:12px}
#filters input{width:44%;padding:4px;margin:0 2%}
#card{bottom:12px;inset-inline-start:12px;width:300px;max-width:calc(100% - 48px);display:none}
#card h3{margin:0 0 6px;font-size:18px}
#card .st{display:inline-block;color:#fff;border-radius:12px;padding:2px 10px;font-size:13px}
#card table{border-collapse:collapse;margin:8px 0;width:100%}
#card th{text-align:start;color:#5b6b7b;font-weight:600;padding:3px 0;width:40%;vertical-align:top}
#card td{padding:3px 0;word-break:break-word}
#card .acts{display:flex;gap:6px;flex-wrap:wrap;margin-top:8px}
#card a.btn{text-decoration:none}
#card select,#card input,#card textarea{width:100%;box-sizing:border-box;padding:5px;font-size:14px}
#card textarea{height:60px}
#editbar{top:92px;inset-inline-start:10px;max-width:330px;display:none}
#editbar p{margin:0 0 8px;color:#5b6b7b;font-size:12px}
#msg{position:absolute;z-index:1002;top:96px;left:50%;transform:translateX(-50%);background:#1d2733;color:#fff;
 padding:6px 12px;border-radius:6px;font-size:13px;display:none;max-width:80%}
.legend{background:#fff;padding:8px 10px;border-radius:6px;box-shadow:0 1px 5px rgba(0,0,0,.3);font-size:13px}
.legend .it{display:flex;align-items:center;gap:6px;margin:2px 0}
.legend i{display:inline-block;width:14px;height:14px;border-radius:3px}
.plotlbl{background:transparent;border:0;box-shadow:none;font-weight:700;color:#1d2733;font-size:12px;
 text-shadow:0 0 3px #fff,0 0 3px #fff}
.plotlbl:before{display:none}
.nolabels .plotlbl{display:none}
[dir=rtl] .legend,[dir=rtl] #card,[dir=rtl] #filters{direction:rtl;text-align:right}
</style>
</head>
<body>
<div id="top"><div id="title"></div><div id="row">
 <input id="q" type="search" autocomplete="off"><button class="btn main" id="go"></button>
 <span id="chips"></span><button class="btn" id="fbtn"></button><span id="shown"></span>
</div></div>
<div id="map"></div>
<div id="filters"><label id="lArea"></label><input id="aMin" type="number" min="0"><input id="aMax" type="number" min="0">
 <div id="pBox"><label id="lPrice"></label><input id="pMin" type="number" min="0"><input id="pMax" type="number" min="0"></div>
 <div style="margin-top:10px"><button class="btn" id="fReset"></button></div></div>
<div id="card"></div>
<div id="editbar"><p id="eNote"></p><div id="eCount" style="margin-bottom:8px;font-weight:600"></div>
 <button class="btn main" id="dJson"></button> <button class="btn" id="dCsv"></button>
 <button class="btn" id="oFile"></button><input id="fileIn" type="file" accept=".json,.txt" hidden></div>
<div id="msg"></div>
<script>%%LEAFLET_JS%%</script>
<script type="application/json" id="biruni-data">%%DATA%%</script>
<script>
(function(){
var D = JSON.parse(document.getElementById('biruni-data').textContent), T = D.text;
function $(id){ return document.getElementById(id); }
function esc(s){ return String(s === null || s === undefined ? '' : s).replace(/[&<>"']/g, function(c){
  return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]; }); }
function fmt(n){ if (n === null || n === undefined || n === '') return ''; var v = Number(n);
  return isFinite(v) ? v.toLocaleString('en-US', {maximumFractionDigits: 2}) : ''; }
function tpl(s, o){ return String(s).replace(/\{(\w+)\}/g, function(m, k){ return o[k] === undefined ? m : o[k]; }); }
function digits(s){ return String(s || '').replace(/[٠-٩]/g, function(c){ return c.charCodeAt(0) - 0x660; })
  .replace(/[۰-۹]/g, function(c){ return c.charCodeAt(0) - 0x6f0; }); }
function key(s){  // the same key Biruni gives a plot number (arabic_text.normalise + plot_key)
  s = digits(s).toLowerCase().replace(/[ؐ-ًؚ-ٰٟۖ-ۭـء]/g, '')
    .replace(/[أإآٱ]/g, 'ا').replace(/ى/g, 'ي').replace(/ة/g, 'ه').replace(/ؤ/g, 'و').replace(/ئ/g, 'ي')
    .replace(/[^0-9a-z؀-ۿ\s]|_/g, ' ').replace(/\s+/g, ' ').trim();
  s = s.replace(/^(?:رقم القطعه|قطعه رقم|القطعه|قطعه|رقم|plot no|plot|lot|parcel|no)(?:\s+|(?=\d))/, '').replace(/ /g, '');
  return /^\d+$/.test(s) ? String(parseInt(s, 10)) : s; }
var msgTimer = null;
function say(text){ var m = $('msg'); m.textContent = text; m.style.display = 'block'; clearTimeout(msgTimer);
  msgTimer = setTimeout(function(){ m.style.display = 'none'; }, 3500); }
var store = null;
try { window.localStorage.setItem('biruni-test', '1'); window.localStorage.removeItem('biruni-test'); store = window.localStorage; }
catch (e) { store = null; }
var SKEY = 'biruni-sales:' + D.map_id;
var statuses = {}; D.statuses.forEach(function(s){ statuses[s.key] = s; });
function stOf(p){ return statuses[p.s] || statuses.unknown || {label: p.s, color: '#9aa5b1'}; }
// edits of the editor copy, kept in this browser
var edits = {};
if (D.editor && store) { try { edits = JSON.parse(store.getItem(SKEY) || '{}') || {}; } catch (e) { edits = {}; } }
var dirty = false;
function apply(p){ var e = edits[p.k]; if (!e) return p;
  var q = {}; for (var a in p) q[a] = p[a];
  if (typeof e.s === 'string') q.s = e.s; if ('p' in e) q.p = e.p; if ('t' in e) q.t = e.t; return q; }
function ensureStatus(k, label){ if (statuses[k]) return; var c = ['#2f7ed8','#8e5bd0','#1aa3a3','#c2569b','#7a8b2a','#b5651d'];
  statuses[k] = {key: k, label: label, color: c[Object.keys(statuses).length % c.length]}; D.statuses.push(statuses[k]); }
Object.keys(edits).forEach(function(k){ var e = edits[k]; if (e && e.s && e.s.indexOf('x:') === 0 && e.l) ensureStatus(e.s, e.l); });

document.title = D.title || document.title;
$('title').textContent = D.title || '';
$('q').placeholder = T.search; $('go').textContent = T.go; $('fbtn').textContent = T.filters;
$('lArea').textContent = T.area; $('lPrice').textContent = T.price + (D.currency ? ' (' + D.currency + ')' : '');
$('aMin').placeholder = $('pMin').placeholder = T.from; $('aMax').placeholder = $('pMax').placeholder = T.to;
$('fReset').textContent = T.reset;
if (!D.prices) $('pBox').style.display = 'none';

var map = L.map('map', {preferCanvas: false, zoomControl: false});
L.control.zoom({position: D.rtl ? 'topright' : 'topleft'}).addTo(map);
map.attributionControl.setPrefix('<a href="https://leafletjs.com">Leaflet</a> | ' + esc(T.made));
var bases = {}, current = null, warned = false;
function base(k){ var b = D.basemaps[k]; if (!b) return null;
  var l = L.tileLayer(b.url, {maxZoom: b.maxZoom, maxNativeZoom: b.maxNativeZoom || b.maxZoom, attribution: b.attribution});
  l.on('tileerror', function(){ if (!warned) { warned = true; say(T.offline); } }); return l; }
var names = {osm: T.map, satellite: T.satellite};
Object.keys(D.basemaps).forEach(function(k){ var l = base(k); if (l) bases[esc(names[k] || k)] = l; if (k === D.basemap) current = l; });
if (current) current.addTo(map);
L.control.layers(bases, null, {position: D.rtl ? 'topleft' : 'topright', collapsed: true}).addTo(map);
L.control.scale({imperial: false, position: D.rtl ? 'bottomright' : 'bottomleft'}).addTo(map);

var hidden = {}, filter = {aMin: null, aMax: null, pMin: null, pMax: null};
var layers = {};  // plot key -> leaflet layer
function geo(p){ return {type: 'Feature', properties: {}, geometry: {type: p.m ? 'MultiPolygon' : 'Polygon', coordinates: p.g}}; }
function style(p){ var c = stOf(p).color; return {color: '#26323f', weight: 1, fillColor: c, fillOpacity: .6}; }
function visible(p){
  if (hidden[p.s]) return false;
  var a = p.a, pr = p.p;
  if (filter.aMin !== null && !(a >= filter.aMin)) return false;
  if (filter.aMax !== null && !(a <= filter.aMax)) return false;
  if (D.prices && filter.pMin !== null && !(pr >= filter.pMin)) return false;
  if (D.prices && filter.pMax !== null && !(pr <= filter.pMax)) return false;
  return true; }
var group = L.featureGroup().addTo(map);
D.plots.forEach(function(p0){
  var l = L.geoJSON(geo(p0), {style: function(){ return style(apply(p0)); }});
  l.on('click', function(){ openCard(p0.k); });
  l.bindTooltip(esc(p0.n), {permanent: true, direction: 'center', className: 'plotlbl'});
  layers[p0.k] = {layer: l, plot: p0};
});
var byKey = {}; D.plots.forEach(function(p){ byKey[p.k] = p; });
function refresh(){
  var n = 0, counts = {};
  D.plots.forEach(function(p0){ var p = apply(p0), it = layers[p0.k];
    counts[p.s] = (counts[p.s] || 0) + 1;
    it.layer.setStyle(style(p));
    var show = visible(p);
    if (show) { n++; if (!group.hasLayer(it.layer)) group.addLayer(it.layer); }
    else if (group.hasLayer(it.layer)) group.removeLayer(it.layer); });
  var h = '';
  D.statuses.forEach(function(s){ if (!counts[s.key] && s.key !== 'available' && s.key !== 'reserved' && s.key !== 'sold') return;
    h += '<button class="chip' + (hidden[s.key] ? ' off' : '') + '" data-k="' + esc(s.key) + '" style="background:' + esc(s.color) + '">'
      + esc(s.label) + ' ' + (counts[s.key] || 0) + '</button> '; });
  $('chips').innerHTML = h;
  Array.prototype.forEach.call($('chips').querySelectorAll('.chip'), function(b){
    b.onclick = function(){ var k = b.getAttribute('data-k'); hidden[k] = !hidden[k]; refresh(); }; });
  $('shown').textContent = tpl(T.shown, {n: n, t: D.plots.length});
  legend();
  if (D.editor) { var c = Object.keys(edits).length; $('eCount').textContent = tpl(T.changed, {n: c}); }
}
var lg = null;
function legend(){
  if (lg) map.removeControl(lg);
  lg = L.control({position: D.rtl ? 'bottomleft' : 'bottomright'});
  lg.onAdd = function(){ var d = L.DomUtil.create('div', 'legend'), h = '<b>' + esc(T.legend) + '</b>';
    D.statuses.forEach(function(s){ h += '<div class="it"><i style="background:' + esc(s.color) + '"></i>' + esc(s.label) + '</div>'; });
    d.innerHTML = h; return d; };
  lg.addTo(map); }
function labels(){ var c = map.getContainer(); if (map.getZoom() >= 17) c.classList.remove('nolabels'); else c.classList.add('nolabels'); }
map.on('zoomend', labels);

function row(th, td){ return '<tr><th>' + esc(th) + '</th><td>' + esc(td) + '</td></tr>'; }
var openKey = null;
function openCard(k){
  var it = layers[k]; if (!it) return; openKey = k;
  var p = apply(it.plot), st = stOf(p), h = '<h3>' + esc(T.plot) + ' ' + esc(p.n) + '</h3>';
  h += '<span class="st" style="background:' + esc(st.color) + '">' + esc(st.label) + '</span><table>';
  if (p.a !== null && p.a !== undefined) h += row(T.area, fmt(p.a));
  if (D.prices && p.p !== null && p.p !== undefined) h += row(T.price, fmt(p.p) + (D.currency ? ' ' + D.currency : ''));
  if (p.t) h += row(T.notes, p.t);
  h += '</table><div class="acts">';
  if (D.phone) {
    var text = tpl(T.ask, {n: p.n}) + (D.title ? ' - ' + D.title : '');
    h += '<a class="btn main" target="_blank" rel="noopener" href="https://wa.me/' + esc(D.phone) + '?text=' + esc(encodeURIComponent(text)) + '">' + esc(T.whatsapp) + '</a>';
    h += '<a class="btn" href="tel:+' + esc(D.phone) + '">' + esc(T.call) + '</a>'; }
  if (D.editor) h += '<button class="btn" id="cEdit">' + esc(T.edit) + '</button>';
  h += '<button class="btn" id="cClose">' + esc(T.close) + '</button></div>';
  var card = $('card'); card.innerHTML = h; card.style.display = 'block';
  $('cClose').onclick = function(){ card.style.display = 'none'; openKey = null; };
  if (D.editor) $('cEdit').onclick = function(){ editCard(k); };
}
function editCard(k){
  var p = apply(layers[k].plot), card = $('card');
  var h = '<h3>' + esc(T.plot) + ' ' + esc(p.n) + '</h3><label>' + esc(T.status) + '</label><select id="eS">';
  D.statuses.forEach(function(s){ h += '<option value="' + esc(s.key) + '"' + (s.key === p.s ? ' selected' : '') + '>' + esc(s.label) + '</option>'; });
  h += '</select><label>' + esc(T.price) + '</label><input id="eP" type="number" min="0" step="any">';
  h += '<label>' + esc(T.notes) + '</label><textarea id="eT"></textarea><div class="acts">';
  h += '<button class="btn main" id="eSave">' + esc(T.save) + '</button><button class="btn" id="eCancel">' + esc(T.cancel) + '</button></div>';
  card.innerHTML = h;
  $('eP').value = p.p === null || p.p === undefined ? '' : p.p;
  $('eT').value = p.t || ''; $('eT').maxLength = D.limits.notes;
  $('eCancel').onclick = function(){ openCard(k); };
  $('eSave').onclick = function(){
    var pr = $('eP').value === '' ? null : Number($('eP').value);
    if (pr !== null && !(isFinite(pr) && pr >= 0)) pr = null;
    var e = {s: $('eS').value, p: pr, t: $('eT').value.slice(0, D.limits.notes)};
    if (e.s.indexOf('x:') === 0) e.l = statuses[e.s].label;
    edits[k] = e; dirty = true; persist(); refresh(); openCard(k); };
}
function persist(){ if (!store) return; try { store.setItem(SKEY, JSON.stringify(edits)); } catch (e) { say(T.no_storage); } }

function find(){ var k = key($('q').value); if (!k) return; var it = layers[k];
  if (!it) { say(T.not_found); return; }
  map.fitBounds(it.layer.getBounds(), {maxZoom: 19, padding: [40, 40]}); openCard(k); }
$('go').onclick = find; $('q').onkeydown = function(e){ if (e.key === 'Enter') find(); };
$('fbtn').onclick = function(){ var f = $('filters'); f.style.display = f.style.display === 'block' ? 'none' : 'block'; };
function num(id){ var v = $(id).value; return v === '' ? null : Number(v); }
['aMin', 'aMax', 'pMin', 'pMax'].forEach(function(id){ $(id).oninput = function(){ filter[id] = num(id); refresh(); }; });
$('fReset').onclick = function(){ ['aMin', 'aMax', 'pMin', 'pMax'].forEach(function(id){ $(id).value = ''; filter[id] = null; });
  hidden = {}; refresh(); };

// ---- editor copy: download / open statuses ----
function download(name, type, text){
  var b = new Blob([text], {type: type}), a = document.createElement('a');
  a.href = URL.createObjectURL(b); a.download = name; document.body.appendChild(a); a.click();
  setTimeout(function(){ URL.revokeObjectURL(a.href); a.remove(); }, 500); }
function safeName(s){ return String(s || 'plots').replace(/[\\\/:*?"<>|]+/g, ' ').trim().slice(0, 60) || 'plots'; }
function currentPlots(){ return D.plots.map(function(p0){ var p = apply(p0), o = {n: p.n, s: p.s, p: p.p === undefined ? null : p.p, t: p.t || ''};
  if (p.s.indexOf('x:') === 0) o.l = stOf(p).label; return o; }); }
if (D.editor) {
  $('editbar').style.display = 'block'; $('eNote').textContent = T.editor_note + (store ? '' : ' ' + T.no_storage);
  $('dJson').textContent = T.download_json; $('dCsv').textContent = T.download_csv; $('oFile').textContent = T.open_file;
  $('dJson').onclick = function(){
    var out = {format: D.status_format, version: 1, map_id: D.map_id, title: D.title, exported: new Date().toISOString(), plots: currentPlots()};
    download(safeName(D.title) + '.biruni-sales.json', 'application/json', JSON.stringify(out)); dirty = false; };
  $('dCsv').onclick = function(){
    function q(v){ v = String(v === null || v === undefined ? '' : v); if (/^[=+\-@]/.test(v)) v = "'" + v;
      return '"' + v.replace(/"/g, '""') + '"'; }
    var lines = [[T.number, T.status, T.price, T.area, T.notes].map(q).join(',')];
    D.plots.forEach(function(p0){ var p = apply(p0); lines.push([p.n, stOf(p).label, p.p, p.a, p.t].map(q).join(',')); });
    download(safeName(D.title) + '.csv', 'text/csv', '﻿' + lines.join('\r\n')); dirty = false; };
  $('oFile').onclick = function(){ $('fileIn').click(); };
  $('fileIn').onchange = function(){
    var f = this.files[0]; this.value = ''; if (!f) return;
    if (f.size > D.limits.file) { say(T.bad_file); return; }
    var r = new FileReader();
    r.onload = function(){
      var d; try { d = JSON.parse(String(r.result).replace(/^﻿/, '')); } catch (e) { say(T.bad_file); return; }
      if (!d || d.format !== D.status_format || !Array.isArray(d.plots)) { say(T.bad_file); return; }
      if (d.map_id && d.map_id !== D.map_id && !window.confirm(T.other_map)) return;
      var n = 0;
      d.plots.slice(0, 100000).forEach(function(p){
        if (!p || (typeof p.n !== 'string' && typeof p.n !== 'number') || typeof p.s !== 'string') return;
        var k = key(p.n); if (!byKey[k]) return;
        var s = p.s;
        if (s.indexOf('x:') === 0) { if (typeof p.l !== 'string' || !p.l) return; ensureStatus(s, p.l.slice(0, D.limits.label)); }
        else if (!statuses[s]) return;
        var e = {s: s, p: (typeof p.p === 'number' && isFinite(p.p) && p.p >= 0) ? p.p : null,
                 t: typeof p.t === 'string' ? p.t.slice(0, D.limits.notes) : ''};
        if (s.indexOf('x:') === 0) e.l = statuses[s].label;
        edits[k] = e; n++; });
      persist(); refresh(); say(tpl(T.loaded, {n: n})); };
    r.readAsText(f); };
  window.addEventListener('beforeunload', function(e){ if (dirty) { e.preventDefault(); e.returnValue = T.unsaved; return T.unsaved; } });
}
refresh(); labels();
if (D.bounds) { var b = L.latLngBounds(D.bounds); if (b.getNorthEast().equals(b.getSouthWest())) map.setView(b.getCenter(), 17);
  else map.fitBounds(b, {padding: [20, 20]}); } else map.setView([24, 45], 5);
labels();
})();
</script>
</body>
</html>
"""
