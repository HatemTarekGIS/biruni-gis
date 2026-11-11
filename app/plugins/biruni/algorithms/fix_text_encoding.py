"""Fix the text of a layer in any language: the real code page of a shapefile, text decoded with the wrong
code page, and legacy glyph fonts (Vietnamese TCVN3/VNI, Bengali Bijoy, Hindi Kruti Dev)."""
from qgis.core import (Qgis, QgsFeature, QgsFeatureSink, QgsField, QgsFields, QgsProcessing,
                       QgsProcessingException, QgsProcessingParameterBoolean, QgsProcessingParameterEnum,
                       QgsProcessingParameterFeatureSink, QgsProcessingParameterFeatureSource,
                       QgsProcessingParameterField, QgsProcessingParameterString)
from qgis.PyQt.QtCore import QMetaType

from .. import legacy_fonts as lf
from .. import text_encoding as te
from ..i18n import tr
from .arabic_text_tools import shapefile_paths
from .base import BiruniAlgorithm

# (label, codec) - the first entry means "find it"
ENCODING_CHOICES = [
    ("Automatic", None), ("UTF-8", "utf-8"), ("Arabic, Persian, Urdu (Windows-1256)", "cp1256"),
    ("Hebrew (Windows-1255)", "cp1255"), ("Turkish (Windows-1254)", "cp1254"),
    ("Cyrillic (Windows-1251)", "cp1251"), ("Cyrillic (KOI8-R)", "koi8_r"),
    ("Central European (Windows-1250)", "cp1250"), ("Western European (Windows-1252)", "cp1252"),
    ("Thai (TIS-620 / 874)", "cp874"), ("Vietnamese (Windows-1258)", "cp1258"), ("Korean (CP949 / EUC-KR)", "cp949"),
    ("Chinese, simplified (GBK / GB18030)", "gbk"), ("Chinese, traditional (Big5)", "big5"),
    ("Japanese (Shift-JIS / 932)", "cp932"),
]
FONT_CHOICES = [("Automatic (Vietnamese fonts)", "auto"), ("No legacy font", None), ("Vietnamese TCVN3 (.VnTime, ABC)", "tcvn3"),
         ("Vietnamese VNI-Windows", "vni"), ("Bengali Bijoy / SutonnyMJ", "bijoy"), ("Hindi Kruti Dev 010", "krutidev")]


def _text_field(field):
    return field.type() == QMetaType.Type.QString


class FixTextEncoding(BiruniAlgorithm):
    ID = "fixtextencoding"
    NAME = "Fix text encoding (any language)"
    GROUP = "Quality"
    GROUP_ID = "quality"
    HELP = ("Repairs text that shows as gibberish - ÃÇÑÚ, Ð¼Ð¾Ñ, ¼­¿ï, ???? - in any language: Arabic, Persian, "
            "Hebrew, Turkish, Russian and other Cyrillic, Central European, Thai, Vietnamese, Korean, Chinese and "
            "Japanese. For a shapefile the real code page of the DBF is found from its bytes and the right .cpg can "
            "be written next to it. Text in old glyph fonts is turned into real Unicode: Vietnamese TCVN3 "
            "(.VnTime) and VNI, Bengali Bijoy/SutonnyMJ and Hindi Kruti Dev. A country code (KR, TH, VN, IR …) "
            "helps when the text is short. The result is a new layer and a table of every change.")
    INPUT, FIELDS, SOURCE, FONT, COUNTRY, PERSIAN, WRITE_CPG, OUTPUT, CHANGES = (
        "INPUT", "FIELDS", "SOURCE", "FONT", "COUNTRY", "PERSIAN", "WRITE_CPG", "OUTPUT", "CHANGES")

    def initAlgorithm(self, config=None):  # noqa: N802
        self.addParameter(QgsProcessingParameterFeatureSource(
            self.INPUT, tr("Layer or table"), [QgsProcessing.SourceType.TypeVector]))
        self.addParameter(QgsProcessingParameterField(
            self.FIELDS, tr("Text fields (empty = all)"), parentLayerParameterName=self.INPUT,
            type=Qgis.ProcessingFieldParameterDataType.String, allowMultiple=True, optional=True))
        self.addParameter(QgsProcessingParameterEnum(
            self.SOURCE, tr("The text was written in"), [tr(label) for label, _c in ENCODING_CHOICES], defaultValue=0))
        self.addParameter(QgsProcessingParameterEnum(
            self.FONT, tr("Legacy font"), [tr(label) for label, _f in FONT_CHOICES], defaultValue=0))
        self.addParameter(QgsProcessingParameterString(
            self.COUNTRY, tr("Country code, to help with short texts (e.g. KR, TH, VN, IR)"), optional=True))
        self.addParameter(QgsProcessingParameterBoolean(
            self.PERSIAN, tr("Write Arabic ي and ك as Persian/Kurdish ی and ک"), False))
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
            [i for i in range(fields.count()) if _text_field(fields.at(i))]
        idx = [i for i in idx if i >= 0 and _text_field(fields.at(i))]
        forced = ENCODING_CHOICES[self.parameterAsEnum(parameters, self.SOURCE, context)][1]
        font_choice = FONT_CHOICES[self.parameterAsEnum(parameters, self.FONT, context)][1]
        cc = (self.parameterAsString(parameters, self.COUNTRY, context) or "").strip().upper()
        persian = self.parameterAsBool(parameters, self.PERSIAN, context)
        do_cpg = self.parameterAsBool(parameters, self.WRITE_CPG, context)
        prefer = [c for c in te.for_country(cc) if te.codec(c)]
        font_hint = next((c for c in te.for_country(cc) if c in lf.CONVERTERS), None)

        sink, sink_id = self.parameterAsSink(parameters, self.OUTPUT, context, fields, source.wkbType(),
                                             source.sourceCrs())
        cf = QgsFields()
        for name, typ in (("fid", QMetaType.Type.LongLong), ("field", QMetaType.Type.QString),
                          ("old", QMetaType.Type.QString), ("new", QMetaType.Type.QString),
                          ("fix", QMetaType.Type.QString)):
            cf.append(QgsField(name, typ))
        csink, csink_id = self.parameterAsSink(parameters, self.CHANGES, context, cf, Qgis.WkbType.NoGeometry)

        provider_name = (layer.dataProvider().encoding() or "") if layer is not None else ""
        # true ISO-8859-1 turns bytes 0x80-0x9F into control characters where cp1252 has ‡ € “ …: keep them apart
        provider_enc = "latin-1" if provider_name.lower().replace("_", "-") in (
            "iso-8859-1", "iso8859-1", "latin1", "latin-1") else te.codec(provider_name)
        # ---- the raw bytes of a shapefile: the surest way to the real code page
        raw_by_field, enc, report = {}, forced, []
        paths = shapefile_paths(layer)
        if paths:
            shp, dbf, _cpg = paths
            try:
                strings = te.read_dbf_strings(dbf)
            except (OSError, ValueError) as e:
                feedback.reportError(tr("The DBF could not be read: {e}").format(e=e))
                strings = {}
            text_positions = [i for i in range(fields.count()) if _text_field(fields.at(i))]
            for pos, values in zip(text_positions, strings.values()):
                raw_by_field[pos] = values

        feats = list(source.getFeatures())

        def present(v):
            return v is not None and not (hasattr(v, "isNull") and v.isNull())

        # ---- legacy glyph fonts first: their bytes would otherwise pass for some code page (TCVN3 for Thai)
        legacy = {}
        if font_choice:
            for i in idx:
                if i in raw_by_field:
                    latin = [b.decode("cp1252", "replace") for b in raw_by_field[i].values()]
                else:
                    latin = [str(f.attributes()[i]) for f in feats if present(f.attributes()[i])]
                family = lf.detect(latin, hint=font_hint) if font_choice == "auto" else font_choice
                if family:
                    legacy[i] = family
                    report.append(tr("{f}: {font}").format(f=fields.at(i).name(), font=tr(lf.LABELS[family])))

        # ---- the code page, from the raw bytes of the other text fields
        if paths and forced is None:
            samples = [b for i, vals in raw_by_field.items() if i not in legacy for b in vals.values()]
            ldid = te.dbf_ldid(dbf)
            enc, score = te.detect(samples, prefer=prefer + ([ldid] if ldid and ldid != "cp1252" else []))
            if enc and score < 0.5:  # a few stray bytes are no evidence
                enc = None
            if enc:
                feedback.pushInfo(tr("Code page of the text in the DBF: {enc} (score {s:.2f}); the layer was "
                                     "read as {cur}.").format(enc=enc, s=score, cur=provider_enc or "?"))
        if paths and enc and do_cpg:
            feedback.pushInfo(tr("Written: {p}").format(p=te.write_cpg(paths[0], enc)))

        # ---- decode every value
        fixed = {}  # (fid, field index) -> (new, [rules])
        for feat in feats:
            attrs = feat.attributes()
            for i in idx:
                v = attrs[i]
                if not present(v):
                    continue
                old = new = str(v)
                rules = []
                raw = raw_by_field.get(i, {}).get(feat.id())
                if i in legacy:
                    base = raw.decode("cp1252", "replace") if raw is not None else old
                    new = lf.convert(base, legacy[i])
                    if new != old:
                        fixed[(feat.id(), i)] = (new, [tr(lf.LABELS[legacy[i]])])
                    continue
                if raw is not None and enc and enc != provider_enc and any(b >= 0x80 for b in raw):
                    dec = te.decode(raw, enc)
                    if dec is not None and dec != new and (forced or te.score_text(dec) > te.score_text(new)):
                        new = dec
                        rules.append(tr("code page {enc}").format(enc=enc))
                elif raw is None and forced and provider_enc and forced != provider_enc:
                    try:
                        dec = te.decode(new.encode(provider_enc), forced)
                    except UnicodeEncodeError:
                        dec = None
                    if dec and dec != new:
                        new = dec
                        rules.append(tr("code page {enc}").format(enc=forced))
                if not rules and forced is None:
                    rep, how = te.repair_mojibake(new, prefer=prefer)
                    if how:
                        new = rep
                        rules.append(tr("wrong code page ({how})").format(how=how))
                if new != old or rules:
                    fixed[(feat.id(), i)] = (new, rules)

        # ---- write
        counts = {}
        total = len(feats) or 1
        for n, feat in enumerate(feats):
            if feedback.isCanceled():
                return {}
            attrs = feat.attributes()
            for i in idx:
                v = attrs[i]
                if v is None or (hasattr(v, "isNull") and v.isNull()):
                    continue
                old = str(v)
                new, rules = fixed.get((feat.id(), i), (old, []))
                if persian:
                    p = te.persian_letters(new)
                    if p != new:
                        new = p
                        rules = rules + [tr("Persian letters")]
                if new != old:
                    attrs[i] = new
                    for r in rules:
                        counts[r] = counts.get(r, 0) + 1
                    if csink is not None:
                        c = QgsFeature(cf)
                        c.setAttributes([feat.id(), fields.at(i).name(), old, new, "; ".join(rules)])
                        csink.addFeature(c, QgsFeatureSink.Flag.FastInsert)
            out = QgsFeature(fields)
            out.setGeometry(feat.geometry())
            out.setAttributes(attrs)
            sink.addFeature(out, QgsFeatureSink.Flag.FastInsert)
            feedback.setProgress(100 * n / total)
        for line in report:
            feedback.pushInfo(line)
        if counts:
            feedback.pushInfo(tr("Fixed: {what}.").format(what=", ".join(f"{k} ×{v}" for k, v in counts.items())))
        else:
            feedback.pushInfo(tr("No text needed fixing."))
        result = {self.OUTPUT: sink_id, "FIX_COUNT": sum(counts.values()), "DETECTED_ENCODING": enc or "",
                  "FONTS": "; ".join(report)}
        if csink is not None:
            result[self.CHANGES] = csink_id
        return result
