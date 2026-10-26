from qgis.core import (Qgis, QgsFeature, QgsFeatureSink, QgsField, QgsFields, QgsGeometry, QgsPointXY,
                       QgsProcessingException, QgsProcessingParameterCrs, QgsProcessingParameterEnum,
                       QgsProcessingParameterFeatureSink, QgsProcessingParameterFeatureSource,
                       QgsProcessingParameterString)
from qgis.PyQt.QtCore import QMetaType
from qgis.PyQt.QtGui import QTransform

from ..i18n import tr
from ..survey.transform import fit_affine, fit_rigid, fit_similarity, parse_pairs
from .base import BiruniAlgorithm

METHODS = [  # (label, fit function, minimum points)
    ("Shift, rotate and scale (similarity / Helmert)", fit_similarity, 2),
    ("Shift and rotate only (keep the size)", fit_rigid, 2),
    ("Affine (also fixes skew and different X/Y scales)", fit_affine, 3),
]


class SimilarityTransform(BiruniAlgorithm):
    ID = "similaritytransform"
    NAME = "Move CAD/Scan to Real Coordinates"
    GROUP = "Survey & CAD"
    GROUP_ID = "survey"
    HELP = ("Places a drawing that was made in local coordinates (a CAD file, a digitised scan, a sketch) on its "
            "real position. Give control points as a line layer - each line drawn from a point of the drawing "
            "to the place where it really is - or type them as 'x,y > X,Y' pairs separated by ';'. Two points "
            "give an exact fit; with three or more the best fit is computed and the error at every control "
            "point is reported (RMSE). Arcs and curves are kept.")

    INPUT, CONTROL, CONTROL_TEXT, METHOD, CRS, OUTPUT, RESIDUALS = (
        "INPUT", "CONTROL", "CONTROL_TEXT", "METHOD", "CRS", "OUTPUT", "RESIDUALS")

    def initAlgorithm(self, config=None):  # noqa: N802
        self.addParameter(QgsProcessingParameterFeatureSource(
            self.INPUT, tr("Layer to move"), [Qgis.ProcessingSourceType.VectorAnyGeometry]))
        self.addParameter(QgsProcessingParameterFeatureSource(
            self.CONTROL, tr("Control lines (from the drawing point to its real position)"),
            [Qgis.ProcessingSourceType.VectorLine], optional=True))
        self.addParameter(QgsProcessingParameterString(
            self.CONTROL_TEXT, tr("Or type control points (x,y > X,Y ; …)"), multiLine=True, optional=True))
        self.addParameter(QgsProcessingParameterEnum(
            self.METHOD, tr("Method"), [tr(m[0]) for m in METHODS], defaultValue=0))
        self.addParameter(QgsProcessingParameterCrs(
            self.CRS, tr("Coordinate system of the real coordinates"), optional=True))
        self.addParameter(QgsProcessingParameterFeatureSink(self.OUTPUT, tr("Moved")))
        self.addParameter(QgsProcessingParameterFeatureSink(
            self.RESIDUALS, tr("Control point errors"), Qgis.ProcessingSourceType.VectorPoint, optional=True,
            createByDefault=False))

    def _pairs(self, parameters, context):
        pairs = []
        control = self.parameterAsSource(parameters, self.CONTROL, context)
        if control is not None:
            for f in control.getFeatures():
                g = f.geometry()
                if g.isNull() or g.isEmpty():
                    continue
                verts = list(g.vertices())
                if len(verts) >= 2:
                    pairs.append(((verts[0].x(), verts[0].y()), (verts[-1].x(), verts[-1].y())))
        text = self.parameterAsString(parameters, self.CONTROL_TEXT, context)
        if text and text.strip():
            try:
                pairs += parse_pairs(text)
            except ValueError as e:
                raise QgsProcessingException(tr("Cannot read the control point '{t}'.").format(t=str(e)))
        return pairs

    def processAlgorithm(self, parameters, context, feedback):  # noqa: N802
        source = self.parameterAsSource(parameters, self.INPUT, context)
        if source is None:
            raise QgsProcessingException(self.invalidSourceError(parameters, self.INPUT))
        label, fit, minimum = METHODS[self.parameterAsEnum(parameters, self.METHOD, context)]
        pairs = self._pairs(parameters, context)
        if len(pairs) < minimum:
            raise QgsProcessingException(tr("This method needs at least {n} control points; {m} given.").format(
                n=minimum, m=len(pairs)))
        try:
            t = fit(pairs)
        except ValueError:
            raise QgsProcessingException(tr("The control points do not define a transformation "
                                            "(they coincide or lie on one line)."))
        crs = self.parameterAsCrs(parameters, self.CRS, context)
        if not crs.isValid():
            crs = source.sourceCrs()
        qt = QTransform(*t.qtransform_args())

        sink, dest = self.parameterAsSink(parameters, self.OUTPUT, context, source.fields(), source.wkbType(), crs)
        total = source.featureCount() or 1
        for i, f in enumerate(source.getFeatures()):
            if feedback.isCanceled():
                break
            g = QgsGeometry(f.geometry())
            if not g.isNull():
                g.transform(qt)
            out = QgsFeature(f)
            out.setGeometry(g)
            sink.addFeature(out, QgsFeatureSink.Flag.FastInsert)
            feedback.setProgress(100 * i / total)

        feedback.pushInfo(tr("Scale {s}, rotation {r}°, {n} control points, RMSE {e}").format(
            s=f"{t.scale:.8f}", r=f"{t.rotation:.6f}", n=len(pairs), e=f"{t.rmse:.4f}"))
        for k, ((x, y), (X, Y)), (ex, ey, e) in zip(range(1, len(pairs) + 1), pairs, t.residuals):
            feedback.pushInfo(f"  {k}: ({x:.3f}, {y:.3f}) → ({X:.3f}, {Y:.3f})  dX={ex:+.4f} dY={ey:+.4f}  "
                              f"e={e:.4f}")
        if len(pairs) > minimum and t.rmse > 0:
            worst = max(range(len(pairs)), key=lambda i: t.residuals[i][2])
            feedback.pushInfo(tr("Largest error at control point {k}: {e}").format(
                k=worst + 1, e=f"{t.residuals[worst][2]:.4f}"))

        results = {self.OUTPUT: dest, "RMSE": t.rmse, "SCALE": t.scale, "ROTATION": t.rotation,
                   "POINTS": len(pairs)}
        rf = QgsFields()
        for n in ("point", "x", "y", "real_x", "real_y", "fitted_x", "fitted_y", "error_x", "error_y", "error"):
            rf.append(QgsField(n, QMetaType.Type.Int if n == "point" else QMetaType.Type.Double))
        rsink, rdest = self.parameterAsSink(parameters, self.RESIDUALS, context, rf, Qgis.WkbType.Point, crs)
        if rsink is not None:
            for k, ((x, y), (X, Y)), (ex, ey, e) in zip(range(1, len(pairs) + 1), pairs, t.residuals):
                fx, fy = t.apply(x, y)
                o = QgsFeature(rf)
                o.setGeometry(QgsGeometry.fromPointXY(QgsPointXY(X, Y)))
                o.setAttributes([k, x, y, X, Y, fx, fy, ex, ey, e])
                rsink.addFeature(o, QgsFeatureSink.Flag.FastInsert)
            results[self.RESIDUALS] = rdest
        return results
