"""GNSS quality check: which surveyed points meet the accuracy the job needs.

Receivers and field apps export their quality figures under many names
(Emlid "Easting RMS", Trimble "H Prec", QField "horizontal_accuracy", NMEA fix
quality 4/5, Arabic headers…). The columns are found by name, normalised to
metres and fix classes, and every point gets a verdict with the reasons. A
geoid model (raster of N in metres) turns ellipsoidal heights into orthometric
heights H = h - N.
"""
import math
import re

from qgis.core import (Qgis, QgsCoordinateTransform, QgsFeature, QgsFeatureSink, QgsField, QgsFields,
                       QgsProcessing, QgsProcessingException, QgsProcessingParameterEnum,
                       QgsProcessingParameterFeatureSink, QgsProcessingParameterFeatureSource,
                       QgsProcessingParameterField, QgsProcessingParameterFileDestination,
                       QgsProcessingParameterNumber, QgsProcessingParameterRasterLayer, QgsPointXY, QgsWkbTypes)
from qgis.PyQt.QtCore import QMetaType

from ..arabic_text import light_normalise, normalise
from ..i18n import is_rtl, tr
from .base import BiruniAlgorithm

# role -> names (normalised: lower case, no spaces/underscores/dots), checked in order; Arabic too.
# Vertical and per-axis roles come first so the generic "accuracy" cannot take them.
ROLE_NAMES = {
    "v_acc": ["vrms", "verticalaccuracy", "vacc", "vaccuracy", "vertacc", "vprec", "verticalprecision",
              "vertprecision", "elevationrms", "heightrms", "sdu", "sigmau", "verticalerror",
              "الدقهالراسيه", "دقهراسيه"],
    "e_acc": ["eastingrms", "erms", "sde", "sigmae", "eastrms", "stdeast"],
    "n_acc": ["northingrms", "nrms", "sdn", "sigman", "northrms", "stdnorth"],
    "h_acc": ["hrms", "horizontalaccuracy", "hacc", "haccuracy", "horizacc", "hprec", "horizontalprecision",
              "horizprecision", "hprecision", "posaccuracy", "accuracy", "horizontalerror", "herror", "sigmah",
              "الدقهالافقيه", "دقهافقيه", "الدقه"],
    "fix": ["solutionstatus", "solution", "fixtype", "fixquality", "fix", "gpsquality", "quality", "rtkstatus",
            "status", "solutiontype", "نوعالحل", "الحل"],
    "pdop": ["pdop", "dop", "gdop"],
    "sats": ["satellites", "sats", "numsats", "numsat", "satellitesused", "nsat", "svs", "satellitecount",
             "عددالاقمار", "الاقمار"],
    "antenna": ["antennaheight", "antheight", "antenna", "anth", "ارتفاعالهوائي"],
    "height": ["ellipsoidalheight", "ellipsoidheight", "ellheight", "hae", "heightellipsoid", "elevation", "height",
               "altitude", "alt", "z", "الارتفاع", "ارتفاع"],
}
FIX_ORDER = ["unknown", "single", "dgps", "float", "fixed"]
FIX_LEVELS = ["Any fix", "DGPS or better", "RTK float or better", "RTK fixed only"]
FIX_NAMES = {"unknown": "Unknown", "single": "Single (autonomous)", "dgps": "DGPS / SBAS",
             "float": "RTK float", "fixed": "RTK fixed"}
LEVEL_MIN = ["unknown", "dgps", "float", "fixed"]


def key(name):
    return re.sub(r"[\s_\-.()/\[\]]+", "", normalise(name or "").lower())


def detect_fields(names):
    """{role: field name} found among `names` (exact normalised match first, then contains)."""
    keys = {n: key(n) for n in names}
    found, used = {}, set()
    for role, cands in ROLE_NAMES.items():
        hit = None
        for c in cands:
            hit = next((n for n, k in keys.items() if k == c and n not in used), None)
            if hit:
                break
        if hit is None:
            for c in cands:
                if len(c) < 4:
                    continue
                hit = next((n for n, k in keys.items() if c in k and n not in used), None)
                if hit:
                    break
        if hit:
            found[role] = hit
            used.add(hit)
    return found


def fix_class(value):
    """Receiver solution text/number -> one of FIX_ORDER."""
    if value is None:
        return "unknown"
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return {4: "fixed", 5: "float", 2: "dgps", 9: "dgps", 1: "single", 6: "single"}.get(int(value), "unknown")
    s = normalise(str(value)).lower().strip()
    if s.isdigit():
        return fix_class(int(s))
    if re.search(r"\bfix(ed)?\b|rtk ?fix|ثابت|مثبت", s) and "float" not in s:
        return "fixed"
    if re.search(r"float|عائم|عايم", s):  # normalise() writes ئ as ي
        return "float"
    if re.search(r"dgps|dgnss|sbas|differential|تفاضلي", s):
        return "dgps"
    if re.search(r"single|autonomous|standalone|gps only|3d|مفرد", s):
        return "single"
    return "unknown"


def to_metres(value, field_name=""):
    """An accuracy cell as metres; centimetre/millimetre columns are recognised by name."""
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        v = float(value)
        text = ""
    else:
        text = light_normalise(str(value))[0].replace("٫", ".").replace(",", ".").strip()
        v = None
    if v is None:
        try:
            v = float(text)
        except ValueError:
            v = None
    if v is None:
        m = re.search(r"-?\d+(?:\.\d+)?", text)
        if not m:
            return None
        v = float(m.group())
    k = key(field_name)
    if k.endswith("mm") or "(mm)" in (field_name or "").lower():
        return v / 1000
    if k.endswith("cm") or "(cm)" in (field_name or "").lower():
        return v / 100
    return v


def check_point(q, max_h, max_v, min_fix, max_pdop, min_sats):
    """q: dict with h_acc, v_acc, fix, pdop, sats -> (ok, [reasons])."""
    reasons = []
    if max_h > 0 and q.get("h_acc") is not None and q["h_acc"] > max_h:
        reasons.append(tr("horizontal {v:.3f} m > {m:.3f} m").format(v=q["h_acc"], m=max_h))
    if max_v > 0 and q.get("v_acc") is not None and q["v_acc"] > max_v:
        reasons.append(tr("vertical {v:.3f} m > {m:.3f} m").format(v=q["v_acc"], m=max_v))
    if min_fix != "unknown" and FIX_ORDER.index(q.get("fix", "unknown")) < FIX_ORDER.index(min_fix):
        reasons.append(tr("solution {f}").format(f=tr(FIX_NAMES[q.get("fix", "unknown")])))
    if max_pdop > 0 and q.get("pdop") is not None and q["pdop"] > max_pdop:
        reasons.append(tr("PDOP {v:.1f} > {m:.1f}").format(v=q["pdop"], m=max_pdop))
    if min_sats > 0 and q.get("sats") is not None and q["sats"] < min_sats:
        reasons.append(tr("{v} satellites < {m}").format(v=int(q["sats"]), m=min_sats))
    return not reasons, reasons


class GnssQuality(BiruniAlgorithm):
    ID = "gnssquality"
    NAME = "GNSS quality check"
    GROUP = "GPS & field"
    GROUP_ID = "gps"
    HELP = ("Checks surveyed GNSS points against the accuracy the job needs: horizontal and vertical precision, "
            "solution type (RTK fixed, float, DGPS, single), PDOP and number of satellites. The columns are found "
            "by themselves in exports from Emlid, Trimble, Leica, Topcon, QField, SW Maps and others, with English "
            "or Arabic headers; you can also pick them. Every point gets a verdict and the reasons, and an Excel "
            "report summarises the job. With a geoid model (a raster of geoid heights N in metres) the "
            "ellipsoidal heights are also turned into orthometric heights (H = h - N), minus the antenna height "
            "when the export did not remove it.")

    def initAlgorithm(self, config=None):  # noqa: N802
        self.addParameter(QgsProcessingParameterFeatureSource(
            "INPUT", tr("GNSS points"), [QgsProcessing.SourceType.TypeVectorPoint]))
        self.addParameter(QgsProcessingParameterNumber("MAX_H", tr("Required horizontal accuracy (m, 0 = no check)"),
                                                       Qgis.ProcessingNumberParameterType.Double, 0.05, minValue=0))
        self.addParameter(QgsProcessingParameterNumber("MAX_V", tr("Required vertical accuracy (m, 0 = no check)"),
                                                       Qgis.ProcessingNumberParameterType.Double, 0.10, minValue=0))
        self.addParameter(QgsProcessingParameterEnum("MIN_FIX", tr("Required solution"),
                                                     [tr(x) for x in FIX_LEVELS], defaultValue=3))
        self.addParameter(QgsProcessingParameterNumber("MAX_PDOP", tr("Maximum PDOP (0 = no check)"),
                                                       Qgis.ProcessingNumberParameterType.Double, 0, minValue=0))
        self.addParameter(QgsProcessingParameterNumber("MIN_SATS", tr("Minimum satellites (0 = no check)"),
                                                       defaultValue=0, minValue=0))
        for role, label in (("H_FIELD", tr("Horizontal accuracy field")), ("V_FIELD", tr("Vertical accuracy field")),
                            ("FIX_FIELD", tr("Solution type field")),
                            ("HEIGHT_FIELD", tr("Ellipsoidal height field"))):
            self.addParameter(QgsProcessingParameterField(role, label + " " + tr("(empty = find it)"),
                                                          parentLayerParameterName="INPUT", optional=True))
        self.addParameter(QgsProcessingParameterRasterLayer("GEOID", tr("Geoid model (N in metres, optional)"),
                                                            optional=True))
        self.addParameter(QgsProcessingParameterNumber("ANTENNA", tr("Subtract antenna height (m, 0 = none)"),
                                                       Qgis.ProcessingNumberParameterType.Double, 0, minValue=0))
        self.addParameter(QgsProcessingParameterFeatureSink("OUTPUT", tr("Checked points")))
        self.addParameter(QgsProcessingParameterFileDestination("REPORT", tr("Excel report"), "Excel (*.xlsx)",
                                                                optional=True, createByDefault=False))

    def processAlgorithm(self, parameters, context, feedback):  # noqa: N802
        src = self.parameterAsSource(parameters, "INPUT", context)
        if src is None:
            raise QgsProcessingException(self.invalidSourceError(parameters, "INPUT"))
        names = src.fields().names()
        roles = detect_fields(names)
        for role, param in (("h_acc", "H_FIELD"), ("v_acc", "V_FIELD"), ("fix", "FIX_FIELD"),
                            ("height", "HEIGHT_FIELD")):
            chosen = self.parameterAsString(parameters, param, context)
            if chosen:
                roles[role] = chosen
                if role == "h_acc":
                    roles.pop("e_acc", None)
                    roles.pop("n_acc", None)
        found = ", ".join(f"{r}={n}" for r, n in roles.items())
        feedback.pushInfo(tr("Columns used: {c}").format(c=found or tr("none found")))
        if not any(r in roles for r in ("h_acc", "e_acc", "v_acc", "fix", "pdop", "sats")):
            raise QgsProcessingException(tr("No accuracy, solution, PDOP or satellite column was found. Choose "
                                            "the columns yourself."))
        max_h = self.parameterAsDouble(parameters, "MAX_H", context)
        max_v = self.parameterAsDouble(parameters, "MAX_V", context)
        min_fix = LEVEL_MIN[self.parameterAsEnum(parameters, "MIN_FIX", context)]
        max_pdop = self.parameterAsDouble(parameters, "MAX_PDOP", context)
        min_sats = self.parameterAsInt(parameters, "MIN_SATS", context)
        geoid = self.parameterAsRasterLayer(parameters, "GEOID", context)
        antenna = self.parameterAsDouble(parameters, "ANTENNA", context)
        xf = None
        if geoid is not None:
            xf = QgsCoordinateTransform(src.sourceCrs(), geoid.crs(), context.transformContext())

        fields = QgsFields(src.fields())
        new = [("gnss_ok", QMetaType.Type.Bool), ("gnss_reasons", QMetaType.Type.QString),
               ("h_acc_m", QMetaType.Type.Double), ("v_acc_m", QMetaType.Type.Double),
               ("fix_class", QMetaType.Type.QString), ("pdop_value", QMetaType.Type.Double),
               ("satellites_n", QMetaType.Type.Int)]
        if geoid is not None:
            new += [("geoid_n", QMetaType.Type.Double), ("ortho_h", QMetaType.Type.Double)]
        for n, t in new:
            fields.append(QgsField(n, t))
        sink, dest = self.parameterAsSink(parameters, "OUTPUT", context, fields, src.wkbType(), src.sourceCrs())

        rows, stats = [], {"total": 0, "ok": 0, "by_fix": {}, "reasons": {}}
        total = src.featureCount() or 1
        for k, f in enumerate(src.getFeatures()):
            if feedback.isCanceled():
                break
            q = {}
            if "h_acc" in roles:
                q["h_acc"] = to_metres(f[roles["h_acc"]], roles["h_acc"])
            elif "e_acc" in roles and "n_acc" in roles:
                e, n = to_metres(f[roles["e_acc"]], roles["e_acc"]), to_metres(f[roles["n_acc"]], roles["n_acc"])
                q["h_acc"] = math.hypot(e, n) if e is not None and n is not None else None
            if "v_acc" in roles:
                q["v_acc"] = to_metres(f[roles["v_acc"]], roles["v_acc"])
            q["fix"] = fix_class(f[roles["fix"]]) if "fix" in roles else "unknown"
            if "pdop" in roles:
                q["pdop"] = to_metres(f[roles["pdop"]])
            if "sats" in roles:
                s = to_metres(f[roles["sats"]])
                q["sats"] = None if s is None else int(s)
            ok, reasons = check_point(q, max_h, max_v, min_fix if "fix" in roles else "unknown", max_pdop,
                                      min_sats)
            vals = list(f.attributes()) + [ok, "; ".join(reasons) or None, q.get("h_acc"), q.get("v_acc"),
                                           tr(FIX_NAMES[q["fix"]]), q.get("pdop"), q.get("sats")]
            if geoid is not None:
                n_val = h_val = None
                g = f.geometry()
                if not g.isNull():
                    try:
                        p = xf.transform(g.asPoint())
                        v, okv = geoid.dataProvider().sample(QgsPointXY(p), 1)
                        if okv and math.isfinite(v):
                            n_val = v
                    except Exception:  # noqa: BLE001 - outside the model
                        n_val = None
                    h = None
                    if "height" in roles:
                        h = to_metres(f[roles["height"]])
                    elif QgsWkbTypes.hasZ(g.wkbType()):
                        h = g.constGet().z()
                    if h is not None and n_val is not None:
                        h_val = h - n_val - antenna
                vals += [n_val, h_val]
            out = QgsFeature(fields)
            out.setGeometry(f.geometry())
            out.setAttributes(vals)
            sink.addFeature(out, QgsFeatureSink.Flag.FastInsert)
            stats["total"] += 1
            stats["ok"] += ok
            stats["by_fix"][q["fix"]] = stats["by_fix"].get(q["fix"], 0) + 1
            for r in reasons:
                kind = r.split(" ")[0]
                stats["reasons"][kind] = stats["reasons"].get(kind, 0) + 1
            rows.append((f.id(), ok, reasons, q))
            feedback.setProgress(100 * k / total)
        bad = stats["total"] - stats["ok"]
        feedback.pushInfo(tr("{ok} of {n} points meet the requirement; {bad} do not.").format(
            ok=stats["ok"], n=stats["total"], bad=bad))
        report = self.parameterAsFileOutput(parameters, "REPORT", context)
        if report:
            write_report(report, rows, stats, roles, (max_h, max_v, min_fix, max_pdop, min_sats))
        return {"OUTPUT": dest, "REPORT": report or None, "TOTAL": stats["total"], "PASSED": stats["ok"],
                "FAILED": bad}


def write_report(path, rows, stats, roles, limits):
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill

    wb = Workbook()
    ws = wb.active
    ws.title = tr("Summary")[:31]
    ws.sheet_view.rightToLeft = is_rtl()
    bold = Font(bold=True)
    max_h, max_v, min_fix, max_pdop, min_sats = limits
    ws.append([tr("GNSS quality check")])
    ws["A1"].font = Font(bold=True, size=14)
    ws.append([])
    for label, value in ((tr("Points"), stats["total"]), (tr("Meet the requirement"), stats["ok"]),
                         (tr("Do not meet it"), stats["total"] - stats["ok"]),
                         (tr("Share meeting it (%)"), round(100 * stats["ok"] / max(stats["total"], 1), 1)),
                         (tr("Required horizontal accuracy (m)"), max_h or "-"),
                         (tr("Required vertical accuracy (m)"), max_v or "-"),
                         (tr("Required solution"), tr(FIX_NAMES[min_fix]) if min_fix != "unknown" else "-"),
                         (tr("Maximum PDOP"), max_pdop or "-"), (tr("Minimum satellites"), min_sats or "-")):
        ws.append([label, value])
        ws.cell(ws.max_row, 1).font = bold
    ws.append([])
    ws.append([tr("Solution"), tr("Points")])
    for fx in reversed(FIX_ORDER):
        if stats["by_fix"].get(fx):
            ws.append([tr(FIX_NAMES[fx]), stats["by_fix"][fx]])
    ws.append([])
    ws.append([tr("Columns used"), ", ".join(f"{r}={n}" for r, n in roles.items())])
    ws.column_dimensions["A"].width = 38
    ws.column_dimensions["B"].width = 22
    d = wb.create_sheet(tr("Points")[:31])
    d.sheet_view.rightToLeft = is_rtl()
    d.append(["fid", tr("Meets"), tr("Horizontal (m)"), tr("Vertical (m)"), tr("Solution"), "PDOP",
              tr("Satellites"), tr("Reasons")])
    for c in d[1]:
        c.font = bold
    red = PatternFill("solid", fgColor="F8D7DA")
    for fid, ok, reasons, q in rows:
        d.append([fid, tr("Yes") if ok else tr("No"), q.get("h_acc"), q.get("v_acc"), tr(FIX_NAMES[q["fix"]]),
                  q.get("pdop"), q.get("sats"), "; ".join(reasons)])
        if not ok:
            for c in d[d.max_row]:
                c.fill = red
    for col, w in zip("ABCDEFGH", (8, 8, 14, 14, 20, 8, 10, 60)):
        d.column_dimensions[col].width = w
    wb.save(path)
