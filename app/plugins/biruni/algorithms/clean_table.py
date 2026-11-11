"""Clean my table: the routine clean-up of attribute values in one step - spaces,
invisible characters, Arabic-Indic digits, letter forms, case, placeholders
such as "-" or «لا يوجد», numbers and dates kept as text, split / merged
columns and duplicate rows - either into a new layer or in the layer itself
inside an edit session, so Ctrl+Z / discarding the edits brings everything back.
Every changed cell is listed in a "changes" table."""
import datetime as dt
import re
from collections import Counter, defaultdict

from qgis.core import (Qgis, QgsFeature, QgsFeatureRequest, QgsFeatureSink, QgsField, QgsFields,
                       QgsProcessingException, QgsProcessingParameterBoolean, QgsProcessingParameterEnum,
                       QgsProcessingParameterFeatureSink, QgsProcessingParameterField,
                       QgsProcessingParameterString, QgsProcessingParameterVectorLayer)
from qgis.PyQt.QtCore import QDate, QMetaType

from ..arabic_text import number_from_match
from ..i18n import tr
from .base import BiruniAlgorithm

MODES = ["Keep as it is", "UPPER CASE", "lower case", "Title Case"]   # Latin letters only
KIND_LABELS = {"integer": "whole numbers", "decimal": "decimal numbers", "date": "dates",
               "excel date": "Excel day numbers (dates)", "hijri date": "Hijri dates (not converted)",
               "code": "numbers with leading zeros - codes, kept as text"}

_INVISIBLE = re.compile("[​-‏‪-‮⁠-⁤⁦-⁩﻿­؜᠎]")
_WIDE_SPACE = re.compile("[   　\t\r\n]")
_TATWEEL = "ـ"
_WS = re.compile(r"\s+")
_DIGITS = str.maketrans("٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹", "01234567890123456789")
_SEPARATORS = re.compile(r"(?<=\d)[٫٬](?=\d)")
_LETTERS = str.maketrans("أإآٱىة", "اااايه")
PLACEHOLDERS = {"-", "--", "---", "—", "–", "_", "__", ".", "..", "...", "?", "??", "n/a", "na", "#n/a", "n.a.",
                "null", "<null>", "none", "nil", "nan", "لا يوجد", "لايوجد", "لا شيء", "لاشيء", "لا شئ", "لاشئ"}
_NUM_FULL = re.compile(r"^[+-]?(?:\d{1,3}(?:,\d{3})+|\d+)(?:[.,]\d+)?$")
_YMD = re.compile(r"^(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})(?:[ T]\d{1,2}:\d{2}(?::\d{2})?.*)?$")
_DMY = re.compile(r"^(\d{1,2})[-/.](\d{1,2})[-/.](\d{4}|\d{2})(?:[ T]\d{1,2}:\d{2}(?::\d{2})?.*)?$")
_DATE_HINT = re.compile(r"date|day|تاريخ|يوم", re.I)
_EXCEL_EPOCH = dt.date(1899, 12, 30)
_ID_FIELDS = {"fid", "ogc_fid", "objectid", "object_id", "id"}
SHARE = 0.9          # a text column is "really" numbers / dates when this share of its values are


def is_null(value):
    return value is None or (hasattr(value, "isNull") and value.isNull())


def clean_text(value, invisible=True, spaces=True, digits=True, letters=False, case=0, placeholders=True,
               zero=False):
    """(cleaned value, [rules that changed it]). The value stays a str, or becomes None."""
    rules = []
    s = value

    def step(new, rule):
        nonlocal s
        if new != s:
            rules.append(rule)
            s = new

    if invisible:
        step(_INVISIBLE.sub("", s).replace(_TATWEEL, ""), "invisible")
    if spaces:
        step(_WS.sub(" ", _WIDE_SPACE.sub(" ", s)).strip(), "spaces")
    if digits:
        step(_SEPARATORS.sub(lambda m: "." if m.group(0) == "٫" else ",", s.translate(_DIGITS)), "digits")
    if letters:
        step(s.translate(_LETTERS), "letters")
    if case == 1:
        step(s.upper(), "case")
    elif case == 2:
        step(s.lower(), "case")
    elif case == 3:
        step(" ".join(w[:1].upper() + w[1:].lower() if w[:1].isascii() else w for w in s.split(" ")), "case")
    if placeholders and (s.strip() == "" or s.strip().casefold() in PLACEHOLDERS):
        rules.append("empty")
        return None, rules
    if zero and s.strip() in ("0", "0.0", "0.00"):
        rules.append("zero")
        return None, rules
    return s, rules


# ---- numbers and dates written as text --------------------------------------------------------------
def parse_number_text(s):
    """A number from a whole text value ("1,250.5", "٢٠٫٥", "-3"); None when the text is more than a number."""
    t = _SEPARATORS.sub(lambda m: "." if m.group(0) == "٫" else ",", str(s).translate(_DIGITS)).strip()
    if not _NUM_FULL.match(t):
        return None
    sign = -1.0 if t.startswith("-") else 1.0
    v = number_from_match(t.lstrip("+-"))
    return None if v is None else sign * v


def _date_parts(s):
    t = str(s).translate(_DIGITS).strip()
    m = _YMD.match(t)
    if m:
        return "ymd", int(m.group(1)), int(m.group(2)), int(m.group(3))
    m = _DMY.match(t)
    if m:
        y = int(m.group(3))
        if len(m.group(3)) == 2:
            y += 2000 if y < 50 else 1900
        return "dmy", int(m.group(1)), int(m.group(2)), y
    return None


def _make_date(y, m, d):
    try:
        return dt.date(y, m, d)
    except ValueError:
        return None


def detect_kind(values, field_name=""):
    """What a column of text values really holds: ("integer" | "decimal" | "date" | "excel date" |
    "hijri date" | "code" | None, share of the values that fit, {"day_first": bool})."""
    vals = [v for v in values if v is not None and str(v).strip() != ""]
    if not vals:
        return None, 0.0, {}
    n = len(vals)
    nums = [parse_number_text(v) for v in vals]
    num_ok = [x for x in nums if x is not None]
    if len(num_ok) / n >= SHARE:
        texts = [str(v).translate(_DIGITS).strip().lstrip("+-") for v, x in zip(vals, nums) if x is not None]
        if any(re.match(r"^0\d", t) for t in texts) or any(len(re.sub(r"\D", "", t)) > 15 for t in texts):
            return "code", len(num_ok) / n, {}
        whole = all(float(x).is_integer() and abs(x) < 2 ** 53 for x in num_ok)
        if whole and _DATE_HINT.search(field_name or "") and all(20000 <= x <= 80000 for x in num_ok):
            return "excel date", len(num_ok) / n, {}
        return ("integer" if whole else "decimal"), len(num_ok) / n, {}
    parts = [_date_parts(v) for v in vals]
    ok = [p for p in parts if p is not None]
    if not ok or len(ok) / n < SHARE:
        return None, 0.0, {}
    years = [p[1] if p[0] == "ymd" else p[3] for p in ok]
    if sum(1 for y in years if 1300 <= y <= 1500) > len(years) / 2:
        return "hijri date", len(ok) / n, {}
    dmy = [p for p in ok if p[0] == "dmy"]
    day_first = True
    if any(p[2] > 12 for p in dmy) and not any(p[1] > 12 for p in dmy):
        day_first = False
    opts = {"day_first": day_first}
    good = sum(1 for p in ok if _to_date(p, day_first) is not None)
    if good / n < SHARE:
        return None, 0.0, {}
    return "date", good / n, opts


def _to_date(p, day_first=True):
    if p is None:
        return None
    if p[0] == "ymd":
        return _make_date(p[1], p[2], p[3])
    a, b, y = p[1], p[2], p[3]
    return _make_date(y, b, a) if day_first else _make_date(y, a, b)


def convert_value(value, kind, opts):
    """The typed value of one cell for a detected kind, or None."""
    if value is None or str(value).strip() == "":
        return None
    if kind in ("integer", "decimal"):
        x = parse_number_text(value) if isinstance(value, str) else float(value)
        if x is None:
            return None
        return int(x) if kind == "integer" else float(x)
    if kind == "excel date":
        x = parse_number_text(value) if isinstance(value, str) else float(value)
        if x is None:
            return None
        d = _EXCEL_EPOCH + dt.timedelta(days=int(x))
        return QDate(d.year, d.month, d.day)
    if kind == "date":
        d = _to_date(_date_parts(value), opts.get("day_first", True))
        return QDate(d.year, d.month, d.day) if d else None
    return None


def split_value(value, sep):
    if value is None:
        return []
    s = str(value)
    if sep in ("", " "):
        parts = s.split()
    else:
        alt = {",": "،", ";": "؛"}.get(sep)
        if alt:
            s = s.replace(alt, sep)
        parts = s.split(sep)
    return [p.strip() for p in parts if p.strip() != ""]


def _unique_name(name, used):
    base, k, n = name, 2, name
    while n.lower() in used:
        n = f"{base}_{k}"
        k += 1
    used.add(n.lower())
    return n


def _as_text(value):
    if is_null(value):
        return None
    if isinstance(value, QDate):
        return value.toString("yyyy-MM-dd")
    return str(value)


class CleanTable(BiruniAlgorithm):
    ID = "cleantable"
    NAME = "Clean my table"
    GROUP = "Quality"
    GROUP_ID = "quality"
    HELP = ("Cleans the values of a layer or table in one step: extra and double spaces, invisible characters "
            "(direction marks, zero-width spaces, tatweel), Arabic-Indic digits to 0-9, optionally the letter "
            "forms (أ إ آ → ا, ة → ه, ى → ي) and the case of Latin letters, and empty texts or placeholders such "
            "as «-», «N/A», «null», «لا يوجد» to empty (NULL). It finds text columns that really hold numbers or "
            "dates (dd/mm/yyyy, yyyy-mm-dd, Excel day numbers, Arabic digits) and can add them as real number / "
            "date fields; it can split a column by a separator, merge columns, and mark duplicate rows with a "
            "dup_group number. The result is a new layer, or - with «Change the layer itself» - the edits are "
            "made in an edit session of the layer: check them, then save, or undo (Ctrl+Z) / discard them. Every "
            "changed cell is listed in the changes table with the rule that changed it.")

    (INPUT, FIELDS, INVISIBLE, SPACES, DIGITS, LETTERS, CASE, PLACEHOLDERS, ZERO, CONVERT, SPLIT_FIELD, SPLIT_SEP,
     MERGE_FIELDS, MERGE_SEP, MERGE_NAME, DUPLICATES, DUP_FIELDS, IN_PLACE, OUTPUT, CHANGES) = (
        "INPUT", "FIELDS", "INVISIBLE", "SPACES", "DIGITS", "LETTERS", "CASE", "PLACEHOLDERS", "ZERO", "CONVERT",
        "SPLIT_FIELD", "SPLIT_SEP", "MERGE_FIELDS", "MERGE_SEP", "MERGE_NAME", "DUPLICATES", "DUP_FIELDS",
        "IN_PLACE", "OUTPUT", "CHANGES")

    def flags(self):
        # may edit the project layer itself (edit session, undo stack) - run on the main thread
        return super().flags() | Qgis.ProcessingAlgorithmFlag.NoThreading

    def initAlgorithm(self, config=None):  # noqa: N802
        self.addParameter(QgsProcessingParameterVectorLayer(
            self.INPUT, tr("Layer or table"), [Qgis.ProcessingSourceType.Vector]))
        self.addParameter(QgsProcessingParameterField(
            self.FIELDS, tr("Fields to clean (none = all text fields)"), parentLayerParameterName=self.INPUT,
            allowMultiple=True, optional=True))
        self.addParameter(QgsProcessingParameterBoolean(
            self.INVISIBLE, tr("Remove invisible characters (direction marks, zero-width spaces, tatweel)"), True))
        self.addParameter(QgsProcessingParameterBoolean(
            self.SPACES, tr("Remove extra spaces at the start and end and double spaces"), True))
        self.addParameter(QgsProcessingParameterBoolean(
            self.DIGITS, tr("Arabic-Indic digits to 0-9"), True))
        self.addParameter(QgsProcessingParameterBoolean(
            self.LETTERS, tr("Unify Arabic letter forms (أ إ آ → ا, ة → ه, ى → ي) - changes the spelling"), False))
        self.addParameter(QgsProcessingParameterEnum(
            self.CASE, tr("Latin letters"), [tr(m) for m in MODES], defaultValue=0))
        self.addParameter(QgsProcessingParameterBoolean(
            self.PLACEHOLDERS, tr("Empty texts and placeholders (-, N/A, null, لا يوجد) to empty (NULL)"), True))
        self.addParameter(QgsProcessingParameterBoolean(self.ZERO, tr("Zero (0) to empty (NULL)"), False))
        self.addParameter(QgsProcessingParameterBoolean(
            self.CONVERT, tr("Add number / date fields for text columns that hold numbers or dates"), False))
        self.addParameter(QgsProcessingParameterField(
            self.SPLIT_FIELD, tr("Split this field"), parentLayerParameterName=self.INPUT, optional=True))
        self.addParameter(QgsProcessingParameterString(self.SPLIT_SEP, tr("… by this separator"), ",",
                                                       optional=True))
        self.addParameter(QgsProcessingParameterField(
            self.MERGE_FIELDS, tr("Merge these fields"), parentLayerParameterName=self.INPUT, allowMultiple=True,
            optional=True))
        self.addParameter(QgsProcessingParameterString(self.MERGE_SEP, tr("… with this separator"), " ",
                                                       optional=True))
        self.addParameter(QgsProcessingParameterString(self.MERGE_NAME, tr("… into a field named"), "merged",
                                                       optional=True))
        self.addParameter(QgsProcessingParameterBoolean(
            self.DUPLICATES, tr("Mark duplicate rows (field dup_group)"), True))
        self.addParameter(QgsProcessingParameterField(
            self.DUP_FIELDS, tr("Rows are duplicates when these fields are equal (none = all fields)"),
            parentLayerParameterName=self.INPUT, allowMultiple=True, optional=True))
        self.addParameter(QgsProcessingParameterBoolean(
            self.IN_PLACE, tr("Change the layer itself (in an edit session you can undo)"), False))
        self.addParameter(QgsProcessingParameterFeatureSink(
            self.OUTPUT, tr("Cleaned layer"), Qgis.ProcessingSourceType.Vector, optional=True))
        self.addParameter(QgsProcessingParameterFeatureSink(
            self.CHANGES, tr("Changes"), Qgis.ProcessingSourceType.Vector, optional=True))

    def processAlgorithm(self, parameters, context, feedback):  # noqa: N802
        layer = self.parameterAsVectorLayer(parameters, self.INPUT, context)
        if layer is None:
            raise QgsProcessingException(self.invalidSourceError(parameters, self.INPUT))
        p = {k: self.parameterAsBool(parameters, k, context) for k in (
            self.INVISIBLE, self.SPACES, self.DIGITS, self.LETTERS, self.PLACEHOLDERS, self.ZERO, self.CONVERT,
            self.DUPLICATES, self.IN_PLACE)}
        case = self.parameterAsEnum(parameters, self.CASE, context)
        fields = layer.fields()
        chosen = self.parameterAsStrings(parameters, self.FIELDS, context)
        if chosen:
            targets = [fields.lookupField(n) for n in chosen]
            targets = [i for i in targets if i >= 0]
        else:
            targets = [i for i in range(fields.count()) if fields.at(i).type() == QMetaType.Type.QString]
        split_name = self.parameterAsString(parameters, self.SPLIT_FIELD, context)
        split_idx = fields.lookupField(split_name) if split_name else -1
        split_sep = self.parameterAsString(parameters, self.SPLIT_SEP, context)
        if split_sep is None:
            split_sep = ","
        merge_idx = [fields.lookupField(n) for n in self.parameterAsStrings(parameters, self.MERGE_FIELDS, context)]
        merge_idx = [i for i in merge_idx if i >= 0]
        merge_sep = self.parameterAsString(parameters, self.MERGE_SEP, context)
        merge_sep = " " if merge_sep is None else merge_sep
        merge_name = (self.parameterAsString(parameters, self.MERGE_NAME, context) or "merged").strip() or "merged"
        dup_idx = [fields.lookupField(n) for n in self.parameterAsStrings(parameters, self.DUP_FIELDS, context)]
        dup_idx = [i for i in dup_idx if i >= 0] or [
            i for i in range(fields.count()) if fields.at(i).name().lower() not in _ID_FIELDS]
        in_place = p[self.IN_PLACE]

        # ---- 1. read and clean every value --------------------------------------------------------------
        feedback.pushInfo(tr("Cleaning the values…"))
        request = QgsFeatureRequest()
        if not in_place and parameters.get(self.OUTPUT) is None:
            request.setFlags(QgsFeatureRequest.Flag.NoGeometry)
        feats = list(layer.getFeatures(request))
        total = len(feats) or 1
        counts = Counter()
        rows = []            # (feature, cleaned attribute list)
        cell_changes = []    # (fid, field index, old, new, rules)
        text_targets = [i for i in targets if fields.at(i).type() == QMetaType.Type.QString]
        for n, f in enumerate(feats):
            if feedback.isCanceled():
                return {}
            attrs = list(f.attributes())
            for i in targets:
                old = attrs[i]
                if is_null(old):
                    continue
                if i in text_targets:
                    new, rules = clean_text(str(old), p[self.INVISIBLE], p[self.SPACES], p[self.DIGITS],
                                            p[self.LETTERS], case, p[self.PLACEHOLDERS], p[self.ZERO])
                elif p[self.ZERO] and isinstance(old, (int, float)) and not isinstance(old, bool) and old == 0:
                    new, rules = None, ["zero"]
                else:
                    continue
                if rules:
                    attrs[i] = new
                    counts.update(rules)
                    cell_changes.append((f.id(), i, old, new, rules))
            rows.append((f, attrs))
            feedback.setProgress(40 * n / total)

        # ---- 2. columns that are numbers / dates in disguise ---------------------------------------------
        kinds = {}
        for i in targets:
            name = fields.at(i).name()
            if i in text_targets:
                kind, share, opts = detect_kind([a[i] for _, a in rows], name)
            elif fields.at(i).isNumeric() and _DATE_HINT.search(name):
                vals = [a[i] for _, a in rows if not is_null(a[i])]
                ok = vals and all(float(v).is_integer() and 20000 <= v <= 80000 for v in vals)
                kind, share, opts = ("excel date", 1.0, {}) if ok else (None, 0.0, {})
            else:
                continue
            if kind:
                kinds[i] = (kind, share, opts)
                feedback.pushInfo(tr("{f}: {k} ({p:.0%} of the values)").format(f=name, k=tr(KIND_LABELS[kind]),
                                                                                    p=share))

        # ---- 3. new fields: converted, split, merged, dup_group -----------------------------------------
        used = {fields.at(i).name().lower() for i in range(fields.count())}
        extra = []           # (QgsField, callable(feature, attrs) -> value)
        if p[self.CONVERT]:
            for i, (kind, _, opts) in kinds.items():
                if kind not in ("integer", "decimal", "date", "excel date"):
                    continue
                t = {"integer": QMetaType.Type.LongLong, "decimal": QMetaType.Type.Double}.get(kind, QMetaType.Type.QDate)
                suffix = "num" if kind in ("integer", "decimal") else "date"
                fld = QgsField(_unique_name(f"{fields.at(i).name()}_{suffix}", used), t)
                extra.append((fld, ("convert", i, kind, opts)))
        if split_idx >= 0:
            width = 0
            for _, a in rows:
                width = max(width, len(split_value(a[split_idx], split_sep)))
            width = min(width, 10)
            for k in range(width):
                fld = QgsField(_unique_name(f"{fields.at(split_idx).name()}_{k + 1}", used), QMetaType.Type.QString)
                extra.append((fld, ("split", split_idx, k)))
        if len(merge_idx) >= 1:
            extra.append((QgsField(_unique_name(merge_name, used), QMetaType.Type.QString), ("merge",)))
        dup_groups = {}
        if p[self.DUPLICATES]:
            seen = defaultdict(list)
            for n, (_, a) in enumerate(rows):
                key = tuple(str(a[i]).casefold() if isinstance(a[i], str) else (None if is_null(a[i]) else a[i])
                            for i in dup_idx)
                seen[key].append(n)
            g = 0
            for members in seen.values():
                if len(members) > 1:
                    g += 1
                    for n in members:
                        dup_groups[n] = g
            extra.append((QgsField(_unique_name("dup_group", used), QMetaType.Type.Int), ("dup",)))
            counts["duplicate"] = len(dup_groups)

        def extra_values(n, attrs):
            out = []
            for _, spec in extra:
                if spec[0] == "convert":
                    _, i, kind, opts = spec
                    v = convert_value(attrs[i], kind, opts)
                    if v is not None:
                        counts["number" if kind in ("integer", "decimal") else "date"] += 1
                    elif not is_null(attrs[i]) and str(attrs[i]).strip():
                        failures.append((rows[n][0].id(), i, attrs[i], kind))
                    out.append(v)
                elif spec[0] == "split":
                    parts = split_value(attrs[spec[1]], split_sep)
                    if spec[2] == 0 and len(parts) > 1:
                        counts["split"] += 1
                    if spec[2] < len(parts):
                        # the last field takes whatever is left
                        last = spec[2] == sum(1 for e in extra if e[1][0] == "split") - 1
                        out.append((split_sep if split_sep.strip() else " ").join(parts[spec[2]:]) if last
                                   else parts[spec[2]])
                    else:
                        out.append(None)
                elif spec[0] == "merge":
                    parts = [_as_text(attrs[i]) for i in merge_idx]
                    parts = [x for x in parts if x is not None and x.strip() != ""]
                    if parts:
                        counts["merge"] += 1
                    out.append(merge_sep.join(parts) if parts else None)
                else:
                    out.append(dup_groups.get(n))
            return out

        failures = []
        changes_fields = QgsFields()
        for name, t in (("fid", QMetaType.Type.LongLong), ("field", QMetaType.Type.QString),
                        ("old", QMetaType.Type.QString), ("new", QMetaType.Type.QString),
                        ("rule", QMetaType.Type.QString)):
            changes_fields.append(QgsField(name, t))

        # ---- 4. write --------------------------------------------------------------------------------
        results = {}
        if in_place:
            if not layer.isEditable() and not layer.startEditing():
                raise QgsProcessingException(tr("The layer cannot be edited."))
            layer.beginEditCommand(tr("Clean my table"))
            try:
                for fld, _ in extra:
                    if not layer.addAttribute(fld):
                        raise QgsProcessingException(tr("Could not add the field {f}.").format(f=fld.name()))
                new_idx = [layer.fields().lookupField(fld.name()) for fld, _ in extra]
                for fid, i, old, new, _ in cell_changes:
                    layer.changeAttributeValue(fid, i, new, old)
                for n, (f, attrs) in enumerate(rows):
                    if feedback.isCanceled():
                        break
                    for j, v in zip(new_idx, extra_values(n, attrs)):
                        if v is not None:
                            layer.changeAttributeValue(f.id(), j, v)
                    feedback.setProgress(40 + 50 * n / total)
            except Exception:
                layer.endEditCommand()
                raise
            layer.endEditCommand()
            feedback.pushInfo(tr("The changes are made in an edit session of «{l}»: check them, then save the edits, "
                                 "or undo (Ctrl+Z) / discard them.").format(l=layer.name()))
            results["IN_PLACE"] = True
        else:
            out_fields = QgsFields(fields)
            for fld, _ in extra:
                out_fields.append(fld)
            if parameters.get(self.OUTPUT) is not None:
                sink, dest = self.parameterAsSink(parameters, self.OUTPUT, context, out_fields, layer.wkbType(),
                                                  layer.crs())
                for n, (f, attrs) in enumerate(rows):
                    if feedback.isCanceled():
                        return {}
                    out = QgsFeature(out_fields)
                    out.setGeometry(f.geometry())
                    out.setAttributes(attrs + extra_values(n, attrs))
                    sink.addFeature(out, QgsFeatureSink.Flag.FastInsert)
                    feedback.setProgress(40 + 50 * n / total)
                results[self.OUTPUT] = dest
            else:
                for n, (_, attrs) in enumerate(rows):
                    extra_values(n, attrs)
            results["IN_PLACE"] = False

        if parameters.get(self.CHANGES) is not None:
            csink, cdest = self.parameterAsSink(parameters, self.CHANGES, context, changes_fields,
                                                Qgis.WkbType.NoGeometry)
            for fid, i, old, new, rules in cell_changes:
                feat = QgsFeature(changes_fields)
                feat.setAttributes([fid, fields.at(i).name(), _as_text(old), _as_text(new), ", ".join(rules)])
                csink.addFeature(feat, QgsFeatureSink.Flag.FastInsert)
            for fid, i, old, kind in failures:
                feat = QgsFeature(changes_fields)
                rule = "not a number" if kind in ("integer", "decimal") else "not a date"
                feat.setAttributes([fid, fields.at(i).name(), _as_text(old), None, rule])
                csink.addFeature(feat, QgsFeatureSink.Flag.FastInsert)
            results[self.CHANGES] = cdest

        feedback.setProgress(100)
        feedback.pushInfo(tr("{c} values cleaned in {r} rows; {d} duplicate rows.").format(
            c=len(cell_changes), r=len({c[0] for c in cell_changes}), d=len(dup_groups)))
        results.update({
            "CHANGED_CELLS": len(cell_changes),
            "RULE_COUNTS": dict(counts),
            "TYPES": {fields.at(i).name(): k for i, (k, _, _) in kinds.items()},
            "DUPLICATE_ROWS": len(dup_groups),
            "DUPLICATE_GROUPS": len(set(dup_groups.values())),
            "NOT_CONVERTED": len(failures),
        })
        return results
