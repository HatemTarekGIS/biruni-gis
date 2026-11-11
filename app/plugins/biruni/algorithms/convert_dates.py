"""Convert dates between the calendars used in land records and maps: Gregorian, Hijri (Umm al-Qura),
Solar Hijri (Jalali, Iran/Afghanistan), Thai Buddhist era and Ethiopian."""
from qgis.core import (Qgis, QgsFeature, QgsFeatureSink, QgsField, QgsFields, QgsProcessingException,
                       QgsProcessingParameterBoolean, QgsProcessingParameterEnum, QgsProcessingParameterFeatureSink,
                       QgsProcessingParameterFeatureSource, QgsProcessingParameterField,
                       QgsProcessingParameterString)
from qgis.PyQt.QtCore import QDate, QMetaType

from .. import calendars
from ..i18n import tr
from .base import BiruniAlgorithm


class ConvertDates(BiruniAlgorithm):
    ID = "convertdates"
    NAME = "Convert dates between calendars"
    GROUP = "Quality"
    GROUP_ID = "quality"
    HELP = ("Writes the dates of a field in another calendar: Gregorian, Hijri (Umm al-Qura), Solar Hijri "
            "(Jalali, used in Iran and Afghanistan), Thai Buddhist era or Ethiopian. The field may be a date "
            "field or text in any common form (2024-03-11, 11/3/2024, ١٤٤٥/٠٩/٠١ هـ, ۱۴۰۳/۰۵/۲۰, 2567/01/15 "
            "พ.ศ.); the calendar of each value is found from its digits, its suffix (هـ, ه.ش, พ.ศ., ዓ.ም) or "
            "its year when you leave it on Automatic. A plain year between 1300 and 1500 is read as Hijri: "
            "choose Solar Hijri for Iranian data written without a suffix. Dates in Gregorian come out as a "
            "date field, other calendars as text with the pattern you choose (yyyy, mm, dd, MMMM for the "
            "month name). The field <i>&lt;name&gt;_ok</i> says whether each value was converted exactly.")

    INPUT, FIELD, SOURCE, TARGET, PATTERN, DIGITS, DAY_FIRST, NEW_FIELD, OUTPUT = (
        "INPUT", "FIELD", "SOURCE", "TARGET", "PATTERN", "DIGITS", "DAY_FIRST", "NEW_FIELD", "OUTPUT")

    def initAlgorithm(self, config=None):  # noqa: N802
        self.addParameter(QgsProcessingParameterFeatureSource(
            self.INPUT, tr("Layer or table"), [Qgis.ProcessingSourceType.Vector]))
        self.addParameter(QgsProcessingParameterField(self.FIELD, tr("Date field"), parentLayerParameterName=self.INPUT))
        self.addParameter(QgsProcessingParameterEnum(
            self.SOURCE, tr("Calendar of the dates"),
            [tr("Automatic"), tr("Gregorian"), tr("Hijri (Umm al-Qura)"), tr("Solar Hijri (Jalali, Iran)"),
             tr("Thai Buddhist era"), tr("Ethiopian")], defaultValue=0))
        self.addParameter(QgsProcessingParameterEnum(
            self.TARGET, tr("Write them in"),
            [tr("Gregorian"), tr("Hijri (Umm al-Qura)"), tr("Solar Hijri (Jalali, Iran)"), tr("Thai Buddhist era"),
             tr("Ethiopian")], defaultValue=0))
        self.addParameter(QgsProcessingParameterString(self.PATTERN, tr("Pattern of the text (yyyy, mm, dd, MMMM)"),
                                                       defaultValue="yyyy/mm/dd"))
        self.addParameter(QgsProcessingParameterBoolean(self.DIGITS, tr("Write the digits of the calendar's "
                                                                        "language (٠١٢, ۰۱۲, ๐๑๒)"),
                                                        defaultValue=False))
        self.addParameter(QgsProcessingParameterBoolean(self.DAY_FIRST, tr("Dates with the year last are "
                                                                           "day/month/year"), defaultValue=True))
        self.addParameter(QgsProcessingParameterString(self.NEW_FIELD, tr("New field name (empty = automatic)"),
                                                       defaultValue="", optional=True))
        self.addParameter(QgsProcessingParameterFeatureSink(self.OUTPUT, tr("Converted dates")))

    def processAlgorithm(self, parameters, context, feedback):  # noqa: N802
        source = self.parameterAsSource(parameters, self.INPUT, context)
        if source is None:
            raise QgsProcessingException(self.invalidSourceError(parameters, self.INPUT))
        field = self.parameterAsString(parameters, self.FIELD, context)
        idx = source.fields().indexOf(field)
        if idx < 0:
            raise QgsProcessingException(tr("Choose the date field."))
        src_choice = self.parameterAsEnum(parameters, self.SOURCE, context)
        src = None if src_choice == 0 else calendars.CALENDARS[src_choice - 1]
        target = calendars.CALENDARS[self.parameterAsEnum(parameters, self.TARGET, context)]
        pattern = self.parameterAsString(parameters, self.PATTERN, context) or "yyyy/mm/dd"
        digits = "native" if self.parameterAsBool(parameters, self.DIGITS, context) else "latin"
        day_first = self.parameterAsBool(parameters, self.DAY_FIRST, context)
        name = (self.parameterAsString(parameters, self.NEW_FIELD, context) or "").strip() or f"{field}_{target}"[:60]

        fields = QgsFields(source.fields())
        out_type = QMetaType.Type.QDate if target == "gregorian" else QMetaType.Type.QString
        fields.append(QgsField(name, out_type, len=40 if out_type == QMetaType.Type.QString else 0))
        fields.append(QgsField(f"{name}_ok"[:63], QMetaType.Type.QString, len=12))
        sink, dest = self.parameterAsSink(parameters, self.OUTPUT, context, fields, source.wkbType(),
                                          source.sourceCrs())

        done = failed = approx = 0
        found = {}
        total = source.featureCount() or 1
        for k, f in enumerate(source.getFeatures()):
            if feedback.isCanceled():
                return {}
            value = f[idx]
            result = None
            if value is not None and not (hasattr(value, "isNull") and value.isNull()) and str(value).strip():
                result = calendars.convert(value, target, src, pattern, digits, day_first=day_first)
            out = QgsFeature(fields)
            out.setGeometry(f.geometry())
            attrs = list(f.attributes())
            if result is None:
                attrs += [None, "not a date" if value not in (None, "") else None]
                failed += 1 if value not in (None, "") else 0
            else:
                res, exact, used = result
                if target == "gregorian":
                    res = QDate(res.year, res.month, res.day)
                attrs += [res, "exact" if exact else "approximate"]
                done += 1
                approx += 0 if exact else 1
                found[used] = found.get(used, 0) + 1
            out.setAttributes(attrs)
            sink.addFeature(out, QgsFeatureSink.Flag.FastInsert)
            feedback.setProgress(100 * k / total)
        main = max(found, key=found.get) if found else ""
        feedback.pushInfo(tr("{n} dates converted to {cal}, {f} not read, {a} approximate.").format(
            n=done, cal=tr(calendars.NAMES[target]), f=failed, a=approx))
        if approx:
            feedback.pushWarning(tr("Approximate dates are Hijri dates outside the Umm al-Qura tables "
                                    "(before 1318 or after 1500 AH)."))
        return {self.OUTPUT: dest, "CONVERTED": done, "FAILED": failed, "APPROXIMATE": approx, "SOURCE_FOUND": main,
                "FIELD_NAME": name}
