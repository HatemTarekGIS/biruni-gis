"""Bill of quantities from map layers: lengths, areas and counts per item, group
and period, priced from a rates sheet, as an Excel workbook with live formulas.

The workbook carries its own "Rates" sheet; every price in it is referenced by
formulas, so filling or changing a rate there updates all amounts. The same
sheet is a rates template for the next run."""
import datetime as dt
import math
import os
import re
from collections import OrderedDict, defaultdict

from qgis.core import (Qgis, QgsDistanceArea, QgsFeature, QgsFeatureSink, QgsField, QgsFields,
                       QgsProcessingException, QgsProcessingParameterBoolean, QgsProcessingParameterDateTime,
                       QgsProcessingParameterFeatureSink, QgsProcessingParameterFile,
                       QgsProcessingParameterFileDestination, QgsProcessingParameterMultipleLayers,
                       QgsProcessingParameterNumber, QgsProcessingParameterString, QgsVectorLayer, QgsWkbTypes)
from qgis.PyQt.QtCore import QDate, QDateTime, QMetaType

from ..i18n import is_rtl, tr
from ..smart_table import normalise_text, parse_number, read_rows, split_header
from .base import BiruniAlgorithm

# Header spellings accepted in a rates sheet (compared after norm()).
RATE_HEADERS = {
    "layer": ("layer", "layer name", "الطبقه", "طبقه", "اسم الطبقه"),
    "item": ("item", "type", "code", "item code", "subtype", "البند", "بند", "النوع", "نوع", "الصنف", "كود البند"),
    "unit": ("unit", "uom", "الوحده", "وحده"),
    "rate": ("rate", "price", "unit price", "unit rate", "cost", "السعر", "سعر", "سعر الوحده", "الفئه", "فئه",
             "التكلفه"),
    "description": ("description", "desc", "details", "الوصف", "وصف", "البيان", "بيان", "الوصف/البيان"),
}
_ALEF = str.maketrans("أإآٱىة", "اااايه")


def norm(value):
    """Comparable text: Arabic digits and letter variants unified, case and spaces folded, 3.0 -> '3'."""
    if value is None:
        return ""
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    s = normalise_text(value).lower().translate(_ALEF)
    s = re.sub(r"[ً-ْـ]", "", s)        # harakat and tatweel
    s = re.sub(r"\s+", " ", s).strip()
    n = parse_number(s) if re.fullmatch(r"-?[\d.,]+", s) else None
    if n is not None and float(n).is_integer():
        return str(int(n))
    return s


def to_date(value):
    """A datetime.date from QDate/QDateTime/date/text, None when there is none."""
    if value is None:
        return None
    if isinstance(value, QDateTime):
        return value.date().toPyDate() if value.isValid() else None
    if isinstance(value, QDate):
        return value.toPyDate() if value.isValid() else None
    if isinstance(value, dt.datetime):
        return value.date()
    if isinstance(value, dt.date):
        return value
    s = normalise_text(value)
    if not s or s.upper() == "NULL":
        return None
    m = re.match(r"(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})", s)
    if m:
        y, mo, d = (int(x) for x in m.groups())
    else:
        m = re.match(r"(\d{1,2})[-/.](\d{1,2})[-/.](\d{4})", s)
        if not m:
            return None
        d, mo, y = (int(x) for x in m.groups())
    try:
        return dt.date(y, mo, d)
    except ValueError:
        return None


def read_rates(path):
    """[{layer, item, unit, rate, description}] from an Excel/CSV rates sheet."""
    headers, data = split_header(read_rows(path))
    col = {}
    for i, h in enumerate(headers):
        key = norm(h)
        for name, spellings in RATE_HEADERS.items():
            if name not in col and key in spellings:
                col[name] = i
    if "rate" not in col or ("item" not in col and "layer" not in col):
        raise QgsProcessingException(tr("The rates sheet needs a rate column and an item or layer column "
                                        "(for example: Layer, Item, Unit, Rate, Description)."))
    rates = []
    for row in data:
        def get(name):
            return row[col[name]] if name in col and col[name] < len(row) else None
        rate = parse_number(get("rate"))
        if rate is None:
            continue
        rates.append({"layer": norm(get("layer")), "item": norm(get("item")),
                      "unit": normalise_text(get("unit")), "rate": rate,
                      "description": normalise_text(get("description"))})
    return rates


def match_rate(rates, layer, item):
    """The best rate row for (layer, item): layer+item, then item alone, then the layer alone."""
    lk, ik = norm(layer), norm(item)
    best, score = None, 0
    for r in rates:
        if r["layer"] and r["layer"] != lk:
            continue
        if r["item"] and r["item"] != ik:
            continue
        s = (2 if r["item"] else 0) + (1 if r["layer"] else 0)
        if not r["item"] and ik and r["layer"]:
            s = 0.5                         # a layer-wide rate applies to every item of the layer
        if s > score:
            best, score = r, s
    return best


def _field(layer, name):
    if not name:
        return -1
    want = name.strip().lower()
    for i, f in enumerate(layer.fields()):
        if f.name().lower() == want:
            return i
    return -1


def _split_names(text):
    return [s.strip() for s in re.split(r"[,;،]", text or "") if s.strip()]


def _sheet_title(text, used):
    base = re.sub(r"[\[\]:*?/\\]", "_", text)[:31] or "Sheet"
    name, k = base, 2
    while name.lower() in used:
        suffix = f" {k}"
        name, k = base[:31 - len(suffix)] + suffix, k + 1
    used.add(name.lower())
    return name


def write_boq(path, rows, details, group_names, vat=0.0, currency="", title="", period=""):
    """The workbook. rows: [{layer, item, unit, quantity, rate, description}] (rate may be None);
    details: [{layer, fid, item, groups: [..], date, quantity, unit}]."""
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
    from openpyxl.utils import get_column_letter

    accent = "0E7C74"
    thin = Side(style="thin", color="C9D0D6")
    box = Border(top=thin, bottom=thin, left=thin, right=thin)
    head_font = Font(bold=True, color="FFFFFF")
    head_fill = PatternFill("solid", fgColor=accent)
    total_fill = PatternFill("solid", fgColor="DDF0EE")
    money = f'#,##0.00 "{currency}"' if currency else "#,##0.00"
    rtl = is_rtl()
    used = set()

    def header(ws, r, labels, widths=None):
        for c, h in enumerate(labels, 1):
            cell = ws.cell(row=r, column=c, value=h)
            cell.font, cell.fill, cell.border = head_font, head_fill, box
            cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
            if widths:
                ws.column_dimensions[get_column_letter(c)].width = widths[c - 1]

    def label(row):
        return f"{row['layer']} - {row['item']}" if row["item"] not in ("", None) else row["layer"]

    wb = Workbook()
    summary = wb.active
    summary.title = _sheet_title(tr("Bill of quantities"), used)
    rates_ws = wb.create_sheet(_sheet_title(tr("Rates"), used))
    rates_ref = "'" + rates_ws.title.replace("'", "''") + "'"
    for ws in (summary, rates_ws):
        ws.sheet_view.rightToLeft = rtl

    # Rates: the prices every amount refers to (and a template for the next run)
    header(rates_ws, 1, [tr("Layer"), tr("Item"), tr("Unit"), tr("Rate"), tr("Description")], [24, 22, 10, 14, 40])
    rate_row = {}
    for k, row in enumerate(rows):
        r = k + 2
        rate_row[k] = r
        for c, v in enumerate((row["layer"], row["item"], row["unit"], row["rate"], row["description"]), 1):
            rates_ws.cell(row=r, column=c, value=v).border = box
        rates_ws.cell(row=r, column=4).number_format = money
        if row["rate"] is None:
            rates_ws.cell(row=r, column=4).fill = PatternFill("solid", fgColor="FFF4D6")
    rates_ws.freeze_panes = "A2"

    # Summary
    summary["A1"] = title or tr("Bill of quantities")
    summary["A1"].font = Font(bold=True, size=16, color=accent)
    summary["A2"] = period
    summary["A3"] = tr("Rates and amounts are live formulas: change a rate on the Rates sheet and every amount "
                       "follows.")
    summary["A3"].font = Font(italic=True, color="7B8791")
    h = 5
    header(summary, h, ["#", tr("Layer"), tr("Item"), tr("Description"), tr("Unit"), tr("Quantity"), tr("Rate"),
                        tr("Amount")], [6, 22, 20, 36, 9, 14, 14, 16])
    for k, row in enumerate(rows):
        r = h + 1 + k
        rr = rate_row[k]
        vals = [k + 1, row["layer"], row["item"], f"={rates_ref}!E{rr}", row["unit"], row["quantity"],
                f"={rates_ref}!D{rr}", f"=F{r}*G{r}"]
        for c, v in enumerate(vals, 1):
            cell = summary.cell(row=r, column=c, value=v)
            cell.border = box
        summary.cell(row=r, column=4).alignment = Alignment(wrap_text=True)
        summary.cell(row=r, column=6).number_format = "#,##0.00"
        summary.cell(row=r, column=7).number_format = money
        summary.cell(row=r, column=8).number_format = money
    last = h + len(rows)
    t = last + 1
    lines = [(tr("Total"), f"=SUM(H{h + 1}:H{last})" if rows else 0, None)]
    if vat:
        lines.append((tr("VAT"), f"=H{t}*G{t + 1}", vat / 100.0))
        lines.append((tr("Grand total"), f"=H{t}+H{t + 1}", None))
    for n, (text, formula, pct) in enumerate(lines):
        r = t + n
        summary.cell(row=r, column=2, value=text)
        if pct is not None:
            summary.cell(row=r, column=7, value=pct).number_format = "0%"
        summary.cell(row=r, column=8, value=formula).number_format = money
        for c in range(1, 9):
            cell = summary.cell(row=r, column=c)
            cell.font, cell.fill, cell.border = Font(bold=True), total_fill, box
    summary.freeze_panes = f"A{h + 1}"
    summary.page_setup.fitToWidth = 1
    summary.sheet_properties.pageSetUpPr.fitToPage = True

    # One pivot per grouping field: group values down, items across, amount by SUMPRODUCT with the rates row
    for gi, gname in enumerate(group_names):
        ws = wb.create_sheet(_sheet_title(tr("By {f}").format(f=gname), used))
        ws.sheet_view.rightToLeft = rtl
        keys = [(row["layer"], row["item"]) for row in rows]
        index = {key: k for k, key in enumerate(keys)}
        table = defaultdict(lambda: [0.0] * len(rows))
        for d in details:
            k = index.get((d["layer"], d["item"]))
            if k is not None:
                table[d["groups"][gi]][k] += d["quantity"]
        n = len(rows)
        lc = get_column_letter(n + 1)
        header(ws, 1, [gname] + [label(row) for row in rows] + [tr("Amount")], [22] + [14] * n + [16])
        ws.cell(row=2, column=1, value=tr("Unit")).font = Font(bold=True)
        ws.cell(row=3, column=1, value=tr("Rate")).font = Font(bold=True)
        for k, row in enumerate(rows):
            ws.cell(row=2, column=k + 2, value=row["unit"]).alignment = Alignment(horizontal="center")
            ws.cell(row=3, column=k + 2, value=f"={rates_ref}!D{rate_row[k]}").number_format = money
        r = 3
        for value in sorted(table, key=lambda v: norm(v)):
            r += 1
            ws.cell(row=r, column=1, value=value).border = box
            for k, q in enumerate(table[value]):
                cell = ws.cell(row=r, column=k + 2, value=round(q, 3) if q else None)
                cell.number_format, cell.border = "#,##0.00", box
            cell = ws.cell(row=r, column=n + 2, value=f"=SUMPRODUCT(B{r}:{lc}{r},B$3:{lc}$3)" if n else 0)
            cell.number_format, cell.border = money, box
        tot = r + 1
        ws.cell(row=tot, column=1, value=tr("Total"))
        for c in range(2, n + 3):
            col = get_column_letter(c)
            cell = ws.cell(row=tot, column=c, value=f"=SUM({col}4:{col}{r})" if r >= 4 else 0)
            cell.number_format = money if c == n + 2 else "#,##0.00"
        for c in range(1, n + 3):
            cell = ws.cell(row=tot, column=c)
            cell.font, cell.fill, cell.border = Font(bold=True), total_fill, box
        ws.freeze_panes = "B4"

    # Details: one row per feature
    ws = wb.create_sheet(_sheet_title(tr("Details"), used))
    ws.sheet_view.rightToLeft = rtl
    header(ws, 1, [tr("Layer"), tr("Feature ID"), tr("Item")] + list(group_names) + [tr("Date"), tr("Quantity"),
                                                                                     tr("Unit")],
           [22, 11, 20] + [16] * len(group_names) + [12, 14, 9])
    for k, d in enumerate(details):
        r = k + 2
        vals = [d["layer"], d["fid"], d["item"]] + list(d["groups"]) + [d["date"], round(d["quantity"], 3),
                                                                       d["unit"]]
        for c, v in enumerate(vals, 1):
            ws.cell(row=r, column=c, value=v)
        if d["date"]:
            ws.cell(row=r, column=4 + len(group_names)).number_format = "yyyy-mm-dd"
        ws.cell(row=r, column=5 + len(group_names)).number_format = "#,##0.000"
    ws.freeze_panes = "A2"
    if details:
        ws.auto_filter.ref = f"A1:{get_column_letter(6 + len(group_names))}{len(details) + 1}"

    wb.active = 0
    wb.save(path)
    return path


class Quantities(BiruniAlgorithm):
    ID = "quantities"
    NAME = "Bill of quantities (BOQ) from layers"
    GROUP = "Engineering office"
    GROUP_ID = "office"
    HELP = ("Measures the work drawn on the map and prices it: lengths of lines (metres), areas of polygons "
            "(square metres) and counts of points, measured on the ellipsoid, per item (a type field such as the "
            "kerb or pipe type), optionally split by zone, contractor or any other field, and limited to a period "
            "by a date field. A rates sheet (Excel or CSV with Layer, Item, Unit, Rate, Description columns, "
            "English or Arabic headers) turns quantities into amounts, with VAT. The Excel workbook keeps live "
            "formulas, a sheet per grouping field and a row per feature; without a rates file its Rates sheet "
            "lists the items found, ready to fill in.")

    LAYERS, ITEM, GROUPS, QTY, DATE, DATE_FROM, DATE_TO, SELECTED, RATES, VAT, CURRENCY, XLSX, OUTPUT = (
        "LAYERS", "ITEM_FIELD", "GROUP_FIELDS", "QUANTITY_FIELD", "DATE_FIELD", "DATE_FROM", "DATE_TO",
        "SELECTED", "RATES", "VAT", "CURRENCY", "XLSX", "OUTPUT")

    def flags(self):
        # reads the selection of project layers directly - run on the main thread
        return super().flags() | Qgis.ProcessingAlgorithmFlag.NoThreading

    def initAlgorithm(self, config=None):  # noqa: N802
        self.addParameter(QgsProcessingParameterMultipleLayers(
            self.LAYERS, tr("Layers to measure"), Qgis.ProcessingSourceType.Vector))
        self.addParameter(QgsProcessingParameterString(
            self.ITEM, tr("Item field (type of work, e.g. kerb type) - optional"), "", optional=True))
        self.addParameter(QgsProcessingParameterString(
            self.GROUPS, tr("Split by fields (zone, contractor…; separate with commas) - optional"), "",
            optional=True))
        self.addParameter(QgsProcessingParameterString(
            self.QTY, tr("Quantity field to sum instead of measuring - optional"), "", optional=True))
        self.addParameter(QgsProcessingParameterString(
            self.DATE, tr("Date field (work date) - optional"), "", optional=True))
        self.addParameter(QgsProcessingParameterDateTime(
            self.DATE_FROM, tr("From date"), Qgis.ProcessingDateTimeParameterDataType.Date, optional=True))
        self.addParameter(QgsProcessingParameterDateTime(
            self.DATE_TO, tr("To date"), Qgis.ProcessingDateTimeParameterDataType.Date, optional=True))
        self.addParameter(QgsProcessingParameterBoolean(self.SELECTED, tr("Selected features only"), False))
        self.addParameter(QgsProcessingParameterFile(
            self.RATES, tr("Rates sheet (Excel or CSV) - optional"), optional=True,
            fileFilter="Excel / CSV (*.xlsx *.xlsm *.xls *.ods *.csv *.txt);;All files (*.*)"))
        self.addParameter(QgsProcessingParameterNumber(
            self.VAT, tr("VAT % (0 = none)"), Qgis.ProcessingNumberParameterType.Double, 15.0, minValue=0.0,
            maxValue=100.0))
        self.addParameter(QgsProcessingParameterString(self.CURRENCY, tr("Currency"), "", optional=True))
        self.addParameter(QgsProcessingParameterFileDestination(
            self.XLSX, tr("Bill of quantities (Excel)"), "Excel (*.xlsx)"))
        self.addParameter(QgsProcessingParameterFeatureSink(
            self.OUTPUT, tr("Quantities table"), Qgis.ProcessingSourceType.Vector, optional=True))

    def processAlgorithm(self, parameters, context, feedback):  # noqa: N802
        layers = [lyr for lyr in self.parameterAsLayerList(parameters, self.LAYERS, context)
                  if isinstance(lyr, QgsVectorLayer)]
        if not layers:
            raise QgsProcessingException(tr("Choose at least one vector layer."))
        item_name = self.parameterAsString(parameters, self.ITEM, context).strip()
        group_names = _split_names(self.parameterAsString(parameters, self.GROUPS, context))
        qty_name = self.parameterAsString(parameters, self.QTY, context).strip()
        date_name = self.parameterAsString(parameters, self.DATE, context).strip()
        d_from = to_date(self.parameterAsDateTime(parameters, self.DATE_FROM, context)) \
            if parameters.get(self.DATE_FROM) not in (None, "") else None
        d_to = to_date(self.parameterAsDateTime(parameters, self.DATE_TO, context)) \
            if parameters.get(self.DATE_TO) not in (None, "") else None
        selected = self.parameterAsBoolean(parameters, self.SELECTED, context)
        rates_path = self.parameterAsFile(parameters, self.RATES, context)
        vat = self.parameterAsDouble(parameters, self.VAT, context)
        currency = self.parameterAsString(parameters, self.CURRENCY, context).strip()
        xlsx = self.parameterAsFileOutput(parameters, self.XLSX, context)
        if (d_from or d_to) and not date_name:
            raise QgsProcessingException(tr("A period needs the date field that holds the work date."))

        rates = read_rates(rates_path) if rates_path else []
        if rates_path:
            feedback.pushInfo(tr("{n} rates read.").format(n=len(rates)))

        units = {Qgis.GeometryType.Line: tr("m"), Qgis.GeometryType.Polygon: tr("m²")}
        totals = OrderedDict()           # (layer, item) -> [quantity, unit]
        details = []
        skipped_period = skipped_empty = 0
        n_layers = len(layers)
        for li, layer in enumerate(layers):
            if feedback.isCanceled():
                return {}
            gtype = QgsWkbTypes.geometryType(layer.wkbType())
            i_item, i_qty, i_date = _field(layer, item_name), _field(layer, qty_name), _field(layer, date_name)
            i_groups = [_field(layer, g) for g in group_names]
            for name, idx in ((item_name, i_item), (qty_name, i_qty), (date_name, i_date)):
                if name and idx < 0:
                    feedback.reportError(tr("{layer}: no field named {f}").format(layer=layer.name(), f=name))
            unit = tr("units") if i_qty >= 0 else units.get(gtype, tr("No."))
            da = QgsDistanceArea()
            da.setSourceCrs(layer.crs(), context.transformContext())
            ell = context.ellipsoid()
            if not ell or ell.upper() == "NONE":
                ell = layer.crs().ellipsoidAcronym() or "EPSG:7030"
            da.setEllipsoid(ell)
            feats = layer.getSelectedFeatures() if selected else layer.getFeatures()
            total = (layer.selectedFeatureCount() if selected else layer.featureCount()) or 1
            for i, f in enumerate(feats):
                if feedback.isCanceled():
                    return {}
                attrs = f.attributes()
                day = to_date(attrs[i_date]) if i_date >= 0 else None
                if d_from or d_to:
                    if day is None or (d_from and day < d_from) or (d_to and day > d_to):
                        skipped_period += 1
                        continue
                if i_qty >= 0:
                    q = parse_number(attrs[i_qty])
                    if q is None:
                        skipped_empty += 1
                        continue
                else:
                    g = f.geometry()
                    if gtype in units and (g.isNull() or g.isEmpty()):
                        skipped_empty += 1
                        continue
                    if gtype == Qgis.GeometryType.Line:
                        q = da.convertLengthMeasurement(da.measureLength(g), Qgis.DistanceUnit.Meters)
                    elif gtype == Qgis.GeometryType.Polygon:
                        q = da.convertAreaMeasurement(da.measureArea(g), Qgis.AreaUnit.SquareMeters)
                    else:
                        q = 1.0
                if not math.isfinite(q):
                    skipped_empty += 1
                    continue
                raw_item = attrs[i_item] if i_item >= 0 else None
                item = "" if raw_item is None or str(raw_item) == "NULL" else normalise_text(raw_item)
                if isinstance(raw_item, float) and raw_item.is_integer():
                    item = str(int(raw_item))
                groups = []
                for gi in i_groups:
                    v = attrs[gi] if gi >= 0 else None
                    groups.append("-" if v is None or str(v) in ("", "NULL") else
                                  (str(int(v)) if isinstance(v, float) and v.is_integer() else normalise_text(v)))
                key = (layer.name(), item)
                slot = totals.setdefault(key, [0.0, unit])
                slot[0] += q
                details.append({"layer": layer.name(), "fid": f.id(), "item": item, "groups": groups, "date": day,
                                "quantity": q, "unit": unit})
                feedback.setProgress(80 * (li + i / total) / n_layers)

        if not totals:
            raise QgsProcessingException(tr("No features to measure (check the period and the selection)."))

        rows = []
        unpriced = 0
        layer_order = {lyr.name(): k for k, lyr in enumerate(layers)}
        for (lname, item), (q, unit) in sorted(totals.items(), key=lambda kv: (layer_order[kv[0][0]],
                                                                                norm(kv[0][1]))):
            r = match_rate(rates, lname, item) if rates else None
            if rates and r is None:
                unpriced += 1
            rows.append({"layer": lname, "item": item, "unit": (r["unit"] if r and r["unit"] else unit),
                         "quantity": round(q, 3), "rate": r["rate"] if r else None,
                         "description": r["description"] if r else ""})
        if unpriced:
            feedback.reportError(tr("{n} items have no rate in the rates sheet: fill them in on the Rates sheet "
                                    "of the workbook.").format(n=unpriced))
        if skipped_period:
            feedback.pushInfo(tr("{n} features outside the period (or without a date) were left out.").format(
                n=skipped_period))
        if skipped_empty:
            feedback.pushInfo(tr("{n} features without a geometry or quantity were left out.").format(
                n=skipped_empty))

        amount = sum(r["quantity"] * (r["rate"] or 0) for r in rows)
        vat_amount = amount * vat / 100.0
        period = ""
        if d_from or d_to:
            period = tr("Period: {a} → {b}").format(a=d_from.isoformat() if d_from else "…",
                                                    b=d_to.isoformat() if d_to else "…")
        os.makedirs(os.path.dirname(os.path.abspath(xlsx)), exist_ok=True)
        try:
            write_boq(xlsx, rows, details, group_names, vat, currency, tr("Bill of quantities"), period)
        except PermissionError:
            raise QgsProcessingException(tr("The file is open in another program. Close it and try again."))
        feedback.setProgress(95)

        fields = QgsFields()
        for name, typ in (("layer", QMetaType.Type.QString), ("item", QMetaType.Type.QString),
                          ("description", QMetaType.Type.QString), ("unit", QMetaType.Type.QString),
                          ("quantity", QMetaType.Type.Double), ("rate", QMetaType.Type.Double),
                          ("amount", QMetaType.Type.Double)):
            fields.append(QgsField(name, typ))
        result = {self.XLSX: xlsx, "ITEM_COUNT": len(rows), "FEATURE_COUNT": len(details),
                  "TOTAL_AMOUNT": round(amount, 2), "VAT_AMOUNT": round(vat_amount, 2),
                  "GRAND_TOTAL": round(amount + vat_amount, 2), "UNPRICED_COUNT": unpriced}
        sink, dest = self.parameterAsSink(parameters, self.OUTPUT, context, fields, Qgis.WkbType.NoGeometry)
        if sink is not None:
            for r in rows:
                feat = QgsFeature(fields)
                feat.setAttributes([r["layer"], r["item"], r["description"], r["unit"], r["quantity"], r["rate"],
                                    round(r["quantity"] * r["rate"], 2) if r["rate"] is not None else None])
                sink.addFeature(feat, QgsFeatureSink.Flag.FastInsert)
            result[self.OUTPUT] = dest
        feedback.pushInfo(tr("{n} items, {f} features measured. Total {t} + VAT {v} = {g}").format(
            n=len(rows), f=len(details), t=f"{amount:,.2f}", v=f"{vat_amount:,.2f}", g=f"{amount + vat_amount:,.2f}"))
        feedback.setProgress(100)
        return result
