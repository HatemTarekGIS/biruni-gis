"""Arabic text tools: repair Arabic text in a layer (wrong code page, letters stored in display
order, presentation forms, digits) and convert Hijri (Umm al-Qura) ↔ Gregorian dates."""
import datetime
import os
import struct

from qgis.core import (Qgis, QgsFeature, QgsFeatureSink, QgsField, QgsFields, QgsProcessing,
                       QgsProcessingException, QgsProcessingParameterBoolean, QgsProcessingParameterEnum,
                       QgsProcessingParameterFeatureSink, QgsProcessingParameterFeatureSource,
                       QgsProcessingParameterField, QgsProcessingParameterString, QgsVectorLayer)
from qgis.PyQt.QtCore import QDate, QDateTime, QMetaType

from .. import arabic_text as at
from ..i18n import tr
from .base import BiruniAlgorithm

TEXT_TYPES = (QMetaType.Type.QString,)
# .cpg contents GDAL understands for each code page
CPG_NAMES = {"utf-8": "UTF-8", "cp1256": "1256", "cp720": "720", "iso8859-6": "8859-6", "cp864": "864"}


# ---- raw DBF -----------------------------------------------------------------------------------
def read_dbf_text(dbf_path, max_records=None):
    """Raw bytes of the text (C) fields of a dBASE file, without decoding: (field indexes, {record
    number: [bytes per text field]}). Record numbers are the shapefile feature ids."""
    with open(dbf_path, "rb") as f:
        head = f.read(32)
        if len(head) < 32:
            raise ValueError("not a dBASE file")
        n_rec, head_len, rec_len = struct.unpack("<IHH", head[4:12])
        fields = []
        while True:
            d = f.read(32)
            if not d or d[0] == 0x0D:
                break
            fields.append((chr(d[11]), d[16]))
        f.seek(head_len)
        text_idx = [i for i, (t, _l) in enumerate(fields) if t == "C"]
        offsets = []
        pos = 1  # deletion flag
        for t, length in fields:
            offsets.append((pos, length))
            pos += length
        out = {}
        limit = n_rec if max_records is None else min(n_rec, max_records)
        for r in range(limit):
            rec = f.read(rec_len)
            if len(rec) < rec_len:
                break
            if rec[:1] == b"*":
                continue
            out[r] = [rec[offsets[i][0]:offsets[i][0] + offsets[i][1]].rstrip(b" \x00") for i in text_idx]
        return text_idx, out


def shapefile_paths(layer):
    """(shp, dbf, cpg) for a layer read from a shapefile, else None."""
    if not isinstance(layer, QgsVectorLayer) or layer.providerType() != "ogr":
        return None
    src = layer.source().split("|")[0]
    base, ext = os.path.splitext(src)
    if ext.lower() != ".shp":
        return None
    for dbf in (base + ".dbf", base + ".DBF"):
        if os.path.exists(dbf):
            return src, dbf, base + ".cpg"
    return None


def detect_dbf_encoding(dbf_path, sample=2000):
    """(encoding, score) of the Arabic text in a dBASE file; (None, 0) when it has no 8-bit text."""
    _idx, recs = read_dbf_text(dbf_path, sample)
    return at.guess_encoding([v for vals in recs.values() for v in vals])


def write_cpg(shp_path, encoding):
    """Write the .cpg file next to a shapefile so every program reads its text with `encoding`."""
    from .. import text_encoding

    if encoding.lower() in CPG_NAMES:
        base = os.path.splitext(shp_path)[0]
        with open(base + ".cpg", "w", encoding="ascii") as f:
            f.write(CPG_NAMES[encoding.lower()])
        return base + ".cpg"
    return text_encoding.write_cpg(shp_path, encoding)  # every other code page (Korean, Thai, Hebrew …)


def _is_text_field(field):
    return field.type() in TEXT_TYPES


class ArabicTextDoctor(BiruniAlgorithm):
    ID = "arabictextdoctor"
    NAME = "Fix Arabic text in a layer"
    GROUP = "Quality"
    GROUP_ID = "quality"
    HELP = ("Repairs Arabic text that shows as ÃÇÑÚ, Ø´Ø§Ø±Ø¹ or ???? (read with the wrong code page), text "
            "stored back to front by old CAD and DOS programs (عراش instead of شارع), and letters stored as "
            "presentation forms; optionally converts digits and removes tatweel. For a shapefile the real "
            "code page of the DBF is detected and the right .cpg file can be written next to it. The result "
            "is a new layer (saved as UTF-8) and a table of every change.")

    INPUT, FIELDS, ENCODING, MOJIBAKE, FORMS, REVERSED, DIGITS, TATWEEL, WRITE_CPG, OUTPUT, CHANGES = (
        "INPUT", "FIELDS", "ENCODING", "MOJIBAKE", "FORMS", "REVERSED", "DIGITS", "TATWEEL", "WRITE_CPG", "OUTPUT",
        "CHANGES")
    MODES = ["Keep digits as they are", "Latin digits (0-9)", "Arabic-Indic digits (٠-٩)"]  # digits

    def initAlgorithm(self, config=None):  # noqa: N802
        self.addParameter(QgsProcessingParameterFeatureSource(
            self.INPUT, tr("Layer or table"), [QgsProcessing.SourceType.TypeVector]))
        self.addParameter(QgsProcessingParameterField(
            self.FIELDS, tr("Text fields (empty = all)"), parentLayerParameterName=self.INPUT,
            type=Qgis.ProcessingFieldParameterDataType.String, allowMultiple=True, optional=True))
        self.addParameter(QgsProcessingParameterBoolean(
            self.ENCODING, tr("Detect the real code page of a shapefile (DBF)"), True))
        self.addParameter(QgsProcessingParameterBoolean(
            self.MOJIBAKE, tr("Repair text read with the wrong code page"), True))
        self.addParameter(QgsProcessingParameterBoolean(
            self.FORMS, tr("Turn presentation forms back into ordinary letters"), True))
        self.addParameter(QgsProcessingParameterBoolean(
            self.REVERSED, tr("Put text stored back to front in reading order"), True))
        self.addParameter(QgsProcessingParameterEnum(
            self.DIGITS, tr("Digits"), [tr(m) for m in self.MODES], defaultValue=0))
        self.addParameter(QgsProcessingParameterBoolean(self.TATWEEL, tr("Remove tatweel (ـ)"), False))
        self.addParameter(QgsProcessingParameterBoolean(
            self.WRITE_CPG, tr("Also write the detected code page (.cpg) next to the shapefile"), False))
        self.addParameter(QgsProcessingParameterFeatureSink(self.OUTPUT, tr("Fixed layer")))
        self.addParameter(QgsProcessingParameterFeatureSink(
            self.CHANGES, tr("Changes"), QgsProcessing.SourceType.TypeVector, optional=True,
            createByDefault=False))

    def flags(self):
        return super().flags() | Qgis.ProcessingAlgorithmFlag.NoThreading

    def processAlgorithm(self, parameters, context, feedback):  # noqa: N802
        source = self.parameterAsSource(parameters, self.INPUT, context)
        if source is None:
            raise QgsProcessingException(self.invalidSourceError(parameters, self.INPUT))
        layer = self.parameterAsVectorLayer(parameters, self.INPUT, context)
        fields = source.fields()
        chosen = self.parameterAsFields(parameters, self.FIELDS, context)
        idx = [fields.indexOf(n) for n in chosen] if chosen else \
            [i for i in range(fields.count()) if _is_text_field(fields.at(i))]
        idx = [i for i in idx if i >= 0 and _is_text_field(fields.at(i))]
        do_enc = self.parameterAsBool(parameters, self.ENCODING, context)
        do_moj = self.parameterAsBool(parameters, self.MOJIBAKE, context)
        do_forms = self.parameterAsBool(parameters, self.FORMS, context)
        do_rev = self.parameterAsBool(parameters, self.REVERSED, context)
        digits = self.parameterAsEnum(parameters, self.DIGITS, context)
        do_tat = self.parameterAsBool(parameters, self.TATWEEL, context)
        do_cpg = self.parameterAsBool(parameters, self.WRITE_CPG, context)

        sink, sink_id = self.parameterAsSink(parameters, self.OUTPUT, context, fields, source.wkbType(),
                                             source.sourceCrs())
        cf = QgsFields()
        cf.append(QgsField("fid", QMetaType.Type.LongLong))
        cf.append(QgsField("field", QMetaType.Type.QString))
        cf.append(QgsField("old", QMetaType.Type.QString))
        cf.append(QgsField("new", QMetaType.Type.QString))
        cf.append(QgsField("fix", QMetaType.Type.QString))
        csink, csink_id = self.parameterAsSink(parameters, self.CHANGES, context, cf, Qgis.WkbType.NoGeometry)

        # the real code page of a shapefile, read from the raw DBF bytes
        raw, encoding, provider_enc = {}, None, ""
        paths = shapefile_paths(layer) if do_enc else None
        if paths:
            shp, dbf, cpg = paths
            provider_enc = (layer.dataProvider().encoding() or "").lower().replace("_", "-")
            try:
                text_idx, raw = read_dbf_text(dbf)
                encoding, score = at.guess_encoding([v for vals in raw.values() for v in vals])
            except (OSError, ValueError, struct.error) as e:
                feedback.reportError(tr("The DBF could not be read: {e}").format(e=e))
                raw, encoding = {}, None
            if encoding:
                feedback.pushInfo(tr("Code page of the text in the DBF: {enc} (Arabic score {s:.0%}); the layer "
                                     "was read as {cur}.").format(enc=encoding, s=score, cur=provider_enc or "?"))
                if do_cpg:
                    path = write_cpg(shp, encoding)
                    feedback.pushInfo(tr("Written: {p}").format(p=path))
            # DBF text field k ↔ layer field index (same order in OGR)
            dbf_text_pos = {}
            k = 0
            for i in range(fields.count()):
                if _is_text_field(fields.at(i)):
                    dbf_text_pos[i] = k
                    k += 1
        counts = {}

        def note(rule):
            counts[rule] = counts.get(rule, 0) + 1

        total = source.featureCount() or 1
        for n, feat in enumerate(source.getFeatures()):
            if feedback.isCanceled():
                return {}
            attrs = feat.attributes()
            for i in idx:
                v = attrs[i]
                if v is None or (hasattr(v, "isNull") and v.isNull()):
                    continue
                old = str(v)
                new, rules = old, []
                if raw and encoding and encoding != provider_enc and feat.id() in raw:
                    pos = dbf_text_pos.get(i)
                    vals = raw[feat.id()]
                    if pos is not None and pos < len(vals):
                        try:
                            dec = vals[pos].decode(encoding)
                        except UnicodeDecodeError:
                            dec = None
                        if dec is not None and dec != new and at.arabic_quality(dec) > at.arabic_quality(new):
                            new = dec
                            rules.append(tr("code page {enc}").format(enc=encoding))
                if do_moj and not rules:
                    fixed, how = at.fix_mojibake(new)
                    if how:
                        new = fixed
                        rules.append(tr("wrong code page ({how})").format(how=how))
                if do_rev and at.is_reversed(new):
                    new = at.fix_reversed(new)
                    rules.append(tr("back to front"))
                if do_forms:
                    u = at.unshape(new)
                    if u != new:
                        new = u
                        rules.append(tr("presentation forms"))
                if digits == 1:
                    u = at.to_latin_digits(new)
                elif digits == 2:
                    u = at.to_arabic_digits(new)
                else:
                    u = new
                if u != new:
                    new = u
                    rules.append(tr("digits"))
                if do_tat:
                    u = at.remove_tatweel(new)
                    if u != new:
                        new = u
                        rules.append(tr("tatweel"))
                if new != old:
                    attrs[i] = new
                    for r in rules:
                        note(r)
                    if csink is not None:
                        c = QgsFeature(cf)
                        c.setAttributes([feat.id(), fields.at(i).name(), old, new, "; ".join(rules)])
                        csink.addFeature(c, QgsFeatureSink.Flag.FastInsert)
            out = QgsFeature(fields)
            out.setGeometry(feat.geometry())
            out.setAttributes(attrs)
            sink.addFeature(out, QgsFeatureSink.Flag.FastInsert)
            feedback.setProgress(100 * n / total)
        changed = sum(counts.values())
        if counts:
            feedback.pushInfo(tr("Fixed: {what}.").format(what=", ".join(f"{k} ×{v}" for k, v in counts.items())))
        else:
            feedback.pushInfo(tr("No Arabic text needed fixing."))
        result = {self.OUTPUT: sink_id, "FIX_COUNT": changed, "DETECTED_ENCODING": encoding or ""}
        if csink is not None:
            result[self.CHANGES] = csink_id
        return result


class HijriDates(BiruniAlgorithm):
    ID = "hijridates"
    NAME = "Hijri ↔ Gregorian dates"
    GROUP = "Quality"
    GROUP_ID = "quality"
    HELP = ("Converts a field of dates between the Hijri (Umm al-Qura) and the Gregorian calendar into a new "
            "field. Hijri dates are read in any common form - ١٤٤٥/٠٣/١٢ هـ, 12-3-1445, 1445/3/12H. The Umm "
            "al-Qura calendar is exact for 1318-1500 AH (1900-2077); outside it the arithmetical calendar is "
            "used and the row is marked as approximate (±1 day).")

    INPUT, FIELD, DIRECTION, NEW_FIELD, PATTERN, ARABIC, SUFFIX, OUTPUT = (
        "INPUT", "FIELD", "DIRECTION", "NEW_FIELD", "PATTERN", "ARABIC", "SUFFIX", "OUTPUT")
    DIRECTIONS = ["Automatic (by the year)", "Hijri → Gregorian", "Gregorian → Hijri"]

    def initAlgorithm(self, config=None):  # noqa: N802
        self.addParameter(QgsProcessingParameterFeatureSource(
            self.INPUT, tr("Layer or table"), [QgsProcessing.SourceType.TypeVector]))
        self.addParameter(QgsProcessingParameterField(
            self.FIELD, tr("Date field"), parentLayerParameterName=self.INPUT))
        self.addParameter(QgsProcessingParameterEnum(
            self.DIRECTION, tr("Conversion"), [tr(d) for d in self.DIRECTIONS], defaultValue=0))
        self.addParameter(QgsProcessingParameterString(
            self.NEW_FIELD, tr("New field name (empty = automatic)"), optional=True))
        self.addParameter(QgsProcessingParameterString(
            self.PATTERN, tr("Hijri date pattern (yyyy, mm, dd)"), "yyyy/mm/dd"))
        self.addParameter(QgsProcessingParameterBoolean(self.ARABIC, tr("Arabic-Indic digits"), False))
        self.addParameter(QgsProcessingParameterBoolean(self.SUFFIX, tr("Add هـ after Hijri dates"), True))
        self.addParameter(QgsProcessingParameterFeatureSink(self.OUTPUT, tr("Converted")))

    def processAlgorithm(self, parameters, context, feedback):  # noqa: N802
        source = self.parameterAsSource(parameters, self.INPUT, context)
        if source is None:
            raise QgsProcessingException(self.invalidSourceError(parameters, self.INPUT))
        name = self.parameterAsString(parameters, self.FIELD, context)
        fi = source.fields().indexOf(name)
        if fi < 0:
            raise QgsProcessingException(tr("Choose the date field."))
        direction = self.parameterAsEnum(parameters, self.DIRECTION, context)
        pattern = self.parameterAsString(parameters, self.PATTERN, context) or "yyyy/mm/dd"
        arabic = self.parameterAsBool(parameters, self.ARABIC, context)
        suffix = self.parameterAsBool(parameters, self.SUFFIX, context)
        new_name = (self.parameterAsString(parameters, self.NEW_FIELD, context) or "").strip()

        values = [f[fi] for f in source.getFeatures()]
        if direction == 0:
            hijri = sum(1 for v in values if _looks_hijri(v))
            greg = sum(1 for v in values if _as_date(v) is not None and not _looks_hijri(v))
            direction = 1 if hijri >= greg else 2
            feedback.pushInfo(tr("The field holds {kind} dates.").format(
                kind=tr("Hijri") if direction == 1 else tr("Gregorian")))
        to_greg = direction == 1
        if not new_name:
            new_name = f"{name[:7]}_g" if to_greg else f"{name[:7]}_h"
        fields = QgsFields(source.fields())
        while fields.indexOf(new_name) >= 0:
            new_name += "_"
        fields.append(QgsField(new_name, QMetaType.Type.QDate if to_greg else QMetaType.Type.QString))
        fields.append(QgsField(f"{new_name[:8]}_ok", QMetaType.Type.QString))
        sink, sink_id = self.parameterAsSink(parameters, self.OUTPUT, context, fields, source.wkbType(),
                                             source.sourceCrs())
        done = failed = approx = 0
        total = source.featureCount() or 1
        for n, feat in enumerate(source.getFeatures()):
            if feedback.isCanceled():
                return {}
            v = feat[fi]
            out_v, status = None, ""
            if v is not None and not (hasattr(v, "isNull") and v.isNull()) and str(v).strip():
                try:
                    if to_greg:
                        ymd = at.parse_hijri(v)
                        if ymd:
                            d, exact = at.hijri_to_gregorian(*ymd)
                            out_v = QDate(d.year, d.month, d.day)
                    else:
                        d = _as_date(v)
                        if d:
                            ymd, exact = at.gregorian_to_hijri(d)
                            out_v = at.format_hijri(ymd, pattern, arabic, suffix)
                except ValueError:
                    out_v = None
                if out_v is None:
                    failed += 1
                    status = tr("not a date")
                else:
                    done += 1
                    if exact:
                        status = tr("exact")
                    else:
                        approx += 1
                        status = tr("approximate (±1 day)")
            f = QgsFeature(fields)
            f.setGeometry(feat.geometry())
            f.setAttributes(feat.attributes() + [out_v, status])
            sink.addFeature(f, QgsFeatureSink.Flag.FastInsert)
            feedback.setProgress(100 * n / total)
        feedback.pushInfo(tr("Converted {d}, not understood {f}, approximate {a}.").format(d=done, f=failed, a=approx))
        return {self.OUTPUT: sink_id, "CONVERTED": done, "FAILED": failed, "APPROXIMATE": approx,
                "FIELD_NAME": new_name}


def _looks_hijri(v):
    if v is None or isinstance(v, (QDate, QDateTime, datetime.date)):
        return False
    ymd = at.parse_hijri(v)
    return bool(ymd) and 1200 <= ymd[0] <= 1600


def _as_date(v):
    """datetime.date from a date/datetime field value or Gregorian text (2024-03-11, 11/03/2024)."""
    if v is None or (hasattr(v, "isNull") and v.isNull()):
        return None
    if isinstance(v, QDateTime):
        v = v.date()
    if isinstance(v, QDate):
        return datetime.date(v.year(), v.month(), v.day()) if v.isValid() else None
    if isinstance(v, datetime.datetime):
        return v.date()
    if isinstance(v, datetime.date):
        return v
    return at.parse_gregorian(v)
