import statistics

from qgis.core import (Qgis, QgsExpression, QgsExpressionContext, QgsFeature,
                       QgsFeatureSink, QgsField, QgsFields, QgsProcessingException, QgsProcessingParameterEnum,
                       QgsProcessingParameterExpression, QgsProcessingParameterFeatureSink,
                       QgsProcessingParameterFeatureSource, QgsProcessingParameterField,
                       QgsProcessingParameterNumber, QgsProcessingParameterString)
from qgis.PyQt.QtCore import QMetaType

from ..i18n import tr
from .base import BiruniAlgorithm

ORDERS = [
    "Rows: top to bottom, left to right",
    "Rows: top to bottom, right to left",
    "Columns: left to right, top to bottom",
    "Columns: right to left, top to bottom",
    "By expression (ascending)",
]


class NumberFeatures(BiruniAlgorithm):
    ID = "numberfeatures"
    NAME = "Number features in reading order"
    GROUP = "Data management"
    GROUP_ID = "data"
    HELP = ("Gives every feature a sequential number following its position on the map - row by row or "
            "column by column, in either reading direction - or following an expression. Useful for "
            "parcels, plots, poles, sample points and map labels. Optionally restarts the numbering for "
            "each group (e.g. block or district) and adds a text prefix.")

    INPUT, FIELD, ORDER, EXPRESSION, TOLERANCE, START, STEP, PREFIX, GROUP_BY, OUTPUT = (
        "INPUT", "FIELD", "ORDER", "EXPRESSION", "TOLERANCE", "START", "STEP", "PREFIX", "GROUP_BY", "OUTPUT")

    def initAlgorithm(self, config=None):  # noqa: N802
        self.addParameter(QgsProcessingParameterFeatureSource(
            self.INPUT, tr("Input layer"), [Qgis.ProcessingSourceType.VectorAnyGeometry]))
        self.addParameter(QgsProcessingParameterString(self.FIELD, tr("Number field name"), "num"))
        self.addParameter(QgsProcessingParameterEnum(self.ORDER, tr("Order"), [tr(o) for o in ORDERS],
                                                     defaultValue=0))
        self.addParameter(QgsProcessingParameterExpression(
            self.EXPRESSION, tr("Sort expression (for 'By expression')"), parentLayerParameterName=self.INPUT,
            optional=True))
        self.addParameter(QgsProcessingParameterNumber(
            self.TOLERANCE, tr("Row/column tolerance (layer units, 0 = automatic)"),
            Qgis.ProcessingNumberParameterType.Double, 0.0, minValue=0.0))
        self.addParameter(QgsProcessingParameterNumber(
            self.START, tr("Start at"), Qgis.ProcessingNumberParameterType.Integer, 1))
        self.addParameter(QgsProcessingParameterNumber(
            self.STEP, tr("Step"), Qgis.ProcessingNumberParameterType.Integer, 1, minValue=1))
        self.addParameter(QgsProcessingParameterString(self.PREFIX, tr("Text prefix (optional)"), "",
                                                       optional=True))
        self.addParameter(QgsProcessingParameterField(
            self.GROUP_BY, tr("Restart numbering for each value of (optional)"),
            parentLayerParameterName=self.INPUT, optional=True))
        self.addParameter(QgsProcessingParameterFeatureSink(self.OUTPUT, tr("Numbered")))

    def processAlgorithm(self, parameters, context, feedback):  # noqa: N802
        source = self.parameterAsSource(parameters, self.INPUT, context)
        if source is None:
            raise QgsProcessingException(self.invalidSourceError(parameters, self.INPUT))
        name = (self.parameterAsString(parameters, self.FIELD, context) or "num").strip()
        order = self.parameterAsEnum(parameters, self.ORDER, context)
        expr_text = self.parameterAsExpression(parameters, self.EXPRESSION, context)
        tol = self.parameterAsDouble(parameters, self.TOLERANCE, context)
        start = self.parameterAsInt(parameters, self.START, context)
        step = self.parameterAsInt(parameters, self.STEP, context)
        prefix = self.parameterAsString(parameters, self.PREFIX, context) or ""
        group_by = (self.parameterAsStrings(parameters, self.GROUP_BY, context) or [None])[0]
        if order == 4 and not expr_text:
            raise QgsProcessingException(tr("Choose a sort expression for 'By expression'."))

        feats = []
        expr = None
        ectx = QgsExpressionContext()
        if order == 4:
            expr = QgsExpression(expr_text)
            ectx = self.createExpressionContext(parameters, context, source)
            expr.prepare(ectx)
        for f in source.getFeatures():
            if feedback.isCanceled():
                return {}
            g = f.geometry()
            p = g.pointOnSurface().asPoint() if not g.isNull() and not g.isEmpty() else None
            bb = g.boundingBox() if p is not None else None
            key = None
            if expr is not None:
                ectx.setFeature(f)
                key = expr.evaluate(ectx)
            feats.append({"f": f, "p": p, "h": bb.height() if bb else 0, "w": bb.width() if bb else 0,
                          "key": key, "group": f[group_by] if group_by else None})

        located = [d for d in feats if d["p"] is not None]
        if order in (0, 1, 2, 3) and tol <= 0 and located:
            sizes = [d["h"] if order in (0, 1) else d["w"] for d in located]
            sizes = [s for s in sizes if s > 0]
            tol = statistics.median(sizes) * 0.5 if sizes else 0.0
            feedback.pushInfo(tr("Automatic tolerance: {t}").format(t=round(tol, 6)))

        groups = {}
        for d in feats:
            groups.setdefault(d["group"], []).append(d)

        numbers = {}
        for gkey in sorted(groups, key=lambda k: (k is None, str(k))):
            ordered = self._order(groups[gkey], order, tol)
            n = start
            for d in ordered:
                numbers[d["f"].id()] = f"{prefix}{n}" if prefix else n
                n += step

        fields = QgsFields()
        kept = [f.name() for f in source.fields() if f.name() != name]
        for f in source.fields():
            if f.name() != name:
                fields.append(f)
        fields.append(QgsField(name, QMetaType.Type.QString if prefix else QMetaType.Type.Int))
        sink, dest = self.parameterAsSink(parameters, self.OUTPUT, context, fields, source.wkbType(),
                                          source.sourceCrs())
        for d in feats:
            f = d["f"]
            out = QgsFeature(fields)
            out.setGeometry(f.geometry())
            out.setAttributes([f[k] for k in kept] + [numbers.get(f.id())])
            sink.addFeature(out, QgsFeatureSink.Flag.FastInsert)
        return {self.OUTPUT: dest}

    @staticmethod
    def _order(items, order, tol):
        if order == 4:
            return sorted(items, key=lambda d: (d["key"] is None, d["key"]))
        located = [d for d in items if d["p"] is not None]
        unlocated = [d for d in items if d["p"] is None]
        rows = order in (0, 1)
        if rows:
            located.sort(key=lambda d: -d["p"].y())
            along = (lambda d: d["p"].x()) if order == 0 else (lambda d: -d["p"].x())
            across = lambda d: -d["p"].y()  # noqa: E731
        else:
            located.sort(key=lambda d: d["p"].x() if order == 2 else -d["p"].x())
            along = lambda d: -d["p"].y()  # noqa: E731
            across = (lambda d: d["p"].x()) if order == 2 else (lambda d: -d["p"].x())
        out, band, ref = [], [], None
        for d in located:
            v = across(d)
            if ref is None or v - ref <= tol:
                if ref is None:
                    ref = v
                band.append(d)
            else:
                out.extend(sorted(band, key=along))
                band, ref = [d], v
        out.extend(sorted(band, key=along))
        return out + unlocated
