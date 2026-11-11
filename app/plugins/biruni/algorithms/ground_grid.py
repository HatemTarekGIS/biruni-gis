"""Ground and grid coordinates (combined scale factor) and site calibration
(localization): the two ways surveyors tie a site's local or ground coordinates
to the map grid."""
import json
import math
import os
import re

from qgis.core import (Qgis, QgsCoordinateReferenceSystem, QgsCoordinateTransform, QgsDistanceArea,
                       QgsEllipsoidUtils, QgsFeature, QgsFeatureSink, QgsField, QgsFields, QgsGeometry, QgsPoint,
                       QgsPointXY, QgsProcessingException, QgsProcessingParameterCrs, QgsProcessingParameterEnum,
                       QgsProcessingParameterFeatureSink, QgsProcessingParameterFeatureSource,
                       QgsProcessingParameterFile, QgsProcessingParameterFileDestination,
                       QgsProcessingParameterNumber, QgsProcessingParameterPoint, QgsProcessingParameterString)
from qgis.PyQt.QtCore import QMetaType

from ..i18n import tr
from ..survey.cogo import normalize_digits
from ..survey.transform import PlaneTransform, fit_affine, fit_similarity
from .base import BiruniAlgorithm

# ---- ground <-> grid --------------------------------------------------------------


def mean_radius(semi_major, semi_minor, lat_deg):
    """Gaussian mean radius of curvature sqrt(M·N) at a latitude (metres)."""
    a, b = semi_major, semi_minor
    e2 = 1 - (b * b) / (a * a)
    s2 = math.sin(math.radians(lat_deg)) ** 2
    w = 1 - e2 * s2
    m = a * (1 - e2) / w ** 1.5
    n = a / math.sqrt(w)
    return math.sqrt(m * n)


def elevation_factor(radius, ellipsoid_height):
    """Reduces a ground distance at this height to the ellipsoid."""
    return radius / (radius + ellipsoid_height)


def point_scale_factor(crs, x, y, transform_context, step=50.0):
    """Grid distance / ellipsoid distance at (x, y) of a projected system, measured
    over short lines in four directions (equal for a conformal projection)."""
    geo = QgsCoordinateReferenceSystem(crs.geographicCrsAuthId()) if crs.geographicCrsAuthId() else \
        QgsCoordinateReferenceSystem("EPSG:4326")
    xf = QgsCoordinateTransform(crs, geo, transform_context)
    da = QgsDistanceArea()
    da.setSourceCrs(geo, transform_context)
    da.setEllipsoid(crs.ellipsoidAcronym() or "EPSG:7030")
    ratios = []
    for dx, dy in ((step, 0), (0, step), (-step, 0), (0, -step)):
        p0 = xf.transform(QgsPointXY(x, y))
        p1 = xf.transform(QgsPointXY(x + dx, y + dy))
        d = da.measureLine(p0, p1)
        if d > 0:
            ratios.append(step / d)
    if not ratios:
        raise ValueError("no scale")
    return sum(ratios) / len(ratios), xf.transform(QgsPointXY(x, y))


def combined_factor(crs, x, y, height, geoid, transform_context):
    """(combined, point scale, elevation factor) at a point. height is the mean
    orthometric height of the site, geoid the geoid undulation N (h = H + N)."""
    k, lonlat = point_scale_factor(crs, x, y, transform_context)
    params = QgsEllipsoidUtils.ellipsoidParameters(crs.ellipsoidAcronym() or "EPSG:7030")
    a = params.semiMajor if params.valid else 6378137.0
    b = params.semiMinor if params.valid else 6356752.314245
    ef = elevation_factor(mean_radius(a, b, lonlat.y()), height + geoid)
    return k * ef, k, ef


def scale_rotate(scale, rotation_deg, ox, oy):
    """PlaneTransform for p' = O + scale·R(θ)·(p - O), θ counter-clockwise."""
    t = math.radians(rotation_deg)
    m11, m12 = scale * math.cos(t), scale * math.sin(t)
    m21, m22 = -m12, m11
    dx = ox - (m11 * ox + m21 * oy)
    dy = oy - (m12 * ox + m22 * oy)
    return PlaneTransform(m11, m12, m21, m22, dx, dy, "scale")


def transform_geometry(geom, plane, dz=None):
    """Apply a plane transform (and optionally a height correction dz(x, y) of the
    original x, y) to every vertex, keeping curves, Z and M."""
    from qgis.PyQt.QtGui import QTransform

    g = QgsGeometry(geom)
    if g.isNull() or g.isEmpty():
        return g
    shifts = None
    if dz is not None and g.constGet().is3D():
        shifts = [dz(v.x(), v.y()) for v in g.vertices()]  # on the original coordinates
    g.transform(QTransform(*plane.qtransform_args()))
    if shifts:
        geom_ = g.get()
        for i, d in enumerate(shifts):
            vid = g.vertexIdFromVertexNr(i)
            if isinstance(vid, tuple):  # (found, id) in the Python bindings
                vid = vid[1]
            p = geom_.vertexAt(vid).clone()
            p.setZ(p.z() + d)
            geom_.moveVertex(vid, p)
    return g


class GroundGrid(BiruniAlgorithm):
    ID = "groundgrid"
    NAME = "Ground ↔ grid coordinates"
    GROUP = "Survey & CAD"
    GROUP_ID = "survey"
    HELP = ("Distances measured on the ground are longer or shorter than the same distances on the map grid "
            "(UTM, a TM zone): the projection stretches them and the height of the site shrinks them. This tool "
            "computes the combined scale factor at the site - the projection's scale factor there times the "
            "elevation factor from the mean height (orthometric height plus the geoid undulation) - or takes "
            "the factor you give, and scales the layer about an origin: grid to ground for drawings that must "
            "match tape and total-station distances, or ground to grid to bring them back. An optional rotation "
            "is applied too. Running the other direction with the same settings gives back exactly the same "
            "coordinates. The factor, origin and direction are written in the fields gg_factor, gg_origin and "
            "gg_direction.")

    INPUT, DIRECTION, FACTOR_MODE, FACTOR, HEIGHT, GEOID = "INPUT", "DIRECTION", "FACTOR_MODE", "FACTOR", "HEIGHT", \
        "GEOID"
    ORIGIN_MODE, ORIGIN, ROTATION, OUTPUT = "ORIGIN_MODE", "ORIGIN", "ROTATION", "OUTPUT"
    DIRECTIONS = ["Grid to ground", "Ground to grid"]
    MODES = ["Compute from the site (projection and height)", "Use the factor I give"]

    def initAlgorithm(self, config=None):  # noqa: N802
        self.addParameter(QgsProcessingParameterFeatureSource(
            self.INPUT, tr("Layer"), [Qgis.ProcessingSourceType.VectorAnyGeometry]))
        self.addParameter(QgsProcessingParameterEnum(
            self.DIRECTION, tr("Direction"), [tr(d) for d in self.DIRECTIONS], defaultValue=0))
        self.addParameter(QgsProcessingParameterEnum(
            self.FACTOR_MODE, tr("Scale factor"), [tr(m) for m in self.MODES], defaultValue=0))
        self.addParameter(QgsProcessingParameterNumber(
            self.FACTOR, tr("Combined scale factor (grid distance ÷ ground distance)"),
            Qgis.ProcessingNumberParameterType.Double, 1.0, minValue=0.5, maxValue=1.5))
        self.addParameter(QgsProcessingParameterNumber(
            self.HEIGHT, tr("Mean height of the site above sea level (m)"),
            Qgis.ProcessingNumberParameterType.Double, 0.0, minValue=-500, maxValue=9000))
        self.addParameter(QgsProcessingParameterNumber(
            self.GEOID, tr("Geoid undulation N (m; ellipsoid height = height + N)"),
            Qgis.ProcessingNumberParameterType.Double, 0.0, minValue=-120, maxValue=120))
        self.addParameter(QgsProcessingParameterEnum(
            self.ORIGIN_MODE, tr("Scale about"), [tr("0, 0"), tr("Centre of the layer"), tr("A point I choose")], defaultValue=0))
        self.addParameter(QgsProcessingParameterPoint(self.ORIGIN, tr("Origin point"), optional=True))
        self.addParameter(QgsProcessingParameterNumber(
            self.ROTATION, tr("Rotation (degrees, counter-clockwise)"), Qgis.ProcessingNumberParameterType.Double,
            0.0, minValue=-360, maxValue=360))
        self.addParameter(QgsProcessingParameterFeatureSink(self.OUTPUT, tr("Scaled")))

    def processAlgorithm(self, parameters, context, feedback):  # noqa: N802
        source = self.parameterAsSource(parameters, self.INPUT, context)
        if source is None:
            raise QgsProcessingException(self.invalidSourceError(parameters, self.INPUT))
        crs = source.sourceCrs()
        to_ground = self.parameterAsEnum(parameters, self.DIRECTION, context) == 0
        extent = source.sourceExtent()
        centre = extent.center() if not extent.isEmpty() else QgsPointXY(0, 0)

        if self.parameterAsEnum(parameters, self.FACTOR_MODE, context) == 0:
            if not crs.isValid() or crs.isGeographic():
                raise QgsProcessingException(tr("The layer must be in a projected system (metres) to compute the "
                                                "scale factor, or give the factor yourself."))
            height = self.parameterAsDouble(parameters, self.HEIGHT, context)
            geoid = self.parameterAsDouble(parameters, self.GEOID, context)
            try:
                factor, k, ef = combined_factor(crs, centre.x(), centre.y(), height, geoid,
                                                context.transformContext())
            except Exception as e:  # noqa: BLE001
                raise QgsProcessingException(tr("The scale factor could not be computed: {e}").format(e=e))
            feedback.pushInfo(tr("Projection scale factor {k} × elevation factor {e} = combined factor {c}").format(
                k=f"{k:.9f}", e=f"{ef:.9f}", c=f"{factor:.9f}"))
        else:
            factor = self.parameterAsDouble(parameters, self.FACTOR, context)
            k = ef = None
        if not factor or factor <= 0:
            raise QgsProcessingException(tr("The scale factor must be greater than 0."))

        mode = self.parameterAsEnum(parameters, self.ORIGIN_MODE, context)
        if mode == 1:
            ox, oy = centre.x(), centre.y()
        elif mode == 2:
            if parameters.get(self.ORIGIN) in (None, ""):
                raise QgsProcessingException(tr("Choose the origin point."))
            p = self.parameterAsPoint(parameters, self.ORIGIN, context, crs)
            ox, oy = p.x(), p.y()
        else:
            ox = oy = 0.0
        rotation = self.parameterAsDouble(parameters, self.ROTATION, context)
        if to_ground:
            plane = scale_rotate(1.0 / factor, rotation, ox, oy)
        else:
            plane = scale_rotate(factor, -rotation, ox, oy)

        fields = QgsFields(source.fields())
        for name, kind in (("gg_factor", QMetaType.Type.Double), ("gg_origin", QMetaType.Type.QString),
                           ("gg_direction", QMetaType.Type.QString)):
            if fields.indexOf(name) < 0:
                fields.append(QgsField(name, kind))
        sink, dest = self.parameterAsSink(parameters, self.OUTPUT, context, fields, source.wkbType(), crs)
        direction = "grid>ground" if to_ground else "ground>grid"
        origin_text = f"{ox:.4f},{oy:.4f}"
        i_factor, i_origin, i_dir = (fields.indexOf(n) for n in ("gg_factor", "gg_origin", "gg_direction"))
        total = source.featureCount() or 1
        for n, f in enumerate(source.getFeatures()):
            if feedback.isCanceled():
                return {}
            out = QgsFeature(fields)
            attrs = list(f.attributes()) + [None] * (fields.count() - len(f.attributes()))
            attrs[i_factor], attrs[i_origin], attrs[i_dir] = factor, origin_text, direction
            out.setAttributes(attrs)
            out.setGeometry(transform_geometry(f.geometry(), plane))
            sink.addFeature(out, QgsFeatureSink.Flag.FastInsert)
            feedback.setProgress(100 * n / total)

        note = tr("{d}: combined scale factor {f} about {o}, rotation {r}°").format(
            d=tr(self.DIRECTIONS[0 if to_ground else 1]), f=f"{factor:.9f}", o=origin_text, r=rotation)
        feedback.pushInfo(note)
        self._note = note
        if context.willLoadLayerOnCompletion(dest):
            details = context.layerToLoadOnCompletionDetails(dest)
            self._post = _AbstractNote(note)
            details.setPostProcessor(self._post)
        return {self.OUTPUT: dest, "FACTOR": factor, "SCALE_FACTOR": k, "ELEVATION_FACTOR": ef,
                "ORIGIN_X": ox, "ORIGIN_Y": oy}


try:
    from qgis.core import QgsProcessingLayerPostProcessorInterface

    class _AbstractNote(QgsProcessingLayerPostProcessorInterface):
        """Writes how the layer was made into its metadata abstract."""

        def __init__(self, note):
            super().__init__()
            self.note = note

        def postProcessLayer(self, layer, context, feedback):  # noqa: N802
            md = layer.metadata()
            md.setAbstract(((md.abstract() + "\n") if md.abstract() else "") + self.note)
            layer.setMetadata(md)
except ImportError:  # pragma: no cover
    _AbstractNote = None


# ---- localization (site calibration) ---------------------------------------------

def parse_control(text):
    """Control pairs from a calibration or control-point file, any separator.
    Each line: [name] local E N [H]  grid E N [H] (a leading name or point number
    is kept as the name). Returns [(name, (e, n, h|None), (E, N, H|None))]."""
    pairs = []
    for line in (text or "").splitlines():
        s = normalize_digits(line).strip()
        if not s or s.startswith(("#", "//", ";")):
            continue
        tokens = [t for t in re.split(r"[,\t;| ]+", s) if t]
        name = ""
        nums = []
        for t in tokens:
            try:
                nums.append(float(t))
            except ValueError:
                if not nums and not name:
                    name = t
        if len(nums) in (5, 7) and not name:  # a numeric point number
            name = str(int(nums[0])) if nums[0] == int(nums[0]) else str(nums[0])
            nums = nums[1:]
        if len(nums) == 4:
            pairs.append((name, (nums[0], nums[1], None), (nums[2], nums[3], None)))
        elif len(nums) == 6:
            pairs.append((name, (nums[0], nums[1], nums[2]), (nums[3], nums[4], nums[5])))
    return pairs


def fit_vertical(pairs, mode):
    """Height correction dz(x, y) on local coordinates: 0 none, 1 constant shift,
    2 inclined plane. Returns (coefficients, residuals)."""
    pts = [(lo[0], lo[1], gr[2] - lo[2]) for _, lo, gr in pairs if lo[2] is not None and gr[2] is not None]
    if mode == 0 or not pts:
        return None, []
    if mode == 1 or len(pts) < 3:
        c = sum(p[2] for p in pts) / len(pts)
        coef = {"a": c, "b": 0.0, "c": 0.0, "x0": 0.0, "y0": 0.0}
    else:
        x0 = sum(p[0] for p in pts) / len(pts)
        y0 = sum(p[1] for p in pts) / len(pts)
        from ..survey.transform import _solve

        ata = [[0.0] * 3 for _ in range(3)]
        atb = [0.0] * 3
        for x, y, d in pts:
            row = (1.0, x - x0, y - y0)
            for i in range(3):
                atb[i] += row[i] * d
                for j in range(3):
                    ata[i][j] += row[i] * row[j]
        try:
            a, b, c = _solve(ata, atb)
        except ValueError:
            a, b, c = sum(p[2] for p in pts) / len(pts), 0.0, 0.0
        coef = {"a": a, "b": b, "c": c, "x0": x0, "y0": y0}
    res = [d - vertical_value(coef, x, y) for x, y, d in pts]
    return coef, res


def vertical_value(coef, x, y):
    return coef["a"] + coef["b"] * (x - coef["x0"]) + coef["c"] * (y - coef["y0"])


def invert(plane):
    det = plane.m11 * plane.m22 - plane.m21 * plane.m12
    if abs(det) < 1e-18:
        raise ValueError("singular")
    i11, i21 = plane.m22 / det, -plane.m21 / det
    i12, i22 = -plane.m12 / det, plane.m11 / det
    idx = -(i11 * plane.dx + i21 * plane.dy)
    idy = -(i12 * plane.dx + i22 * plane.dy)
    return PlaneTransform(i11, i12, i21, i22, idx, idy, plane.kind)


def calibration_to_json(plane, vertical, pairs, crs_authid=""):
    return {"format": "biruni-calibration", "version": 1, "kind": plane.kind,
            "m11": plane.m11, "m12": plane.m12, "m21": plane.m21, "m22": plane.m22, "dx": plane.dx, "dy": plane.dy,
            "scale": plane.scale, "rotation_deg": plane.rotation, "rmse": plane.rmse, "vertical": vertical,
            "crs": crs_authid,
            "points": [{"name": n, "local": list(lo), "grid": list(gr)} for n, lo, gr in pairs]}


def calibration_from_json(data):
    if not isinstance(data, dict) or data.get("format") != "biruni-calibration":
        raise ValueError("not a Biruni calibration file")
    plane = PlaneTransform(*(float(data[k]) for k in ("m11", "m12", "m21", "m22", "dx", "dy")),
                           data.get("kind", "similarity"))
    vertical = data.get("vertical")
    if vertical is not None:
        vertical = {k: float(vertical[k]) for k in ("a", "b", "c", "x0", "y0")}
    return plane, vertical


class Localization(BiruniAlgorithm):
    ID = "localization"
    NAME = "Site calibration (localization)"
    GROUP = "Survey & CAD"
    GROUP_ID = "survey"
    HELP = ("Ties a site's local coordinates (an arbitrary 5000/5000 system, an old plan, a total-station job) to "
            "the map grid from control points known in both: a file with one point per line - name, local E N "
            "[H], grid E N [H] - such as a CSV from a controller, a Carlson .loc or a text export of a Trimble "
            ".cal, or pairs typed in the box. A best-fit similarity (shift, rotation, one scale) or affine "
            "transformation is computed with the error at every point, plus an optional height shift or "
            "inclined plane. The calibration can be saved as a file and used again on any layer - points, "
            "lines, polygons and CAD linework - in either direction.")

    INPUT, CONTROL, CONTROL_TEXT, CALIBRATION_IN = "INPUT", "CONTROL", "CONTROL_TEXT", "CALIBRATION_IN"
    METHOD, VERTICAL, DIRECTION, CRS = "METHOD", "VERTICAL", "DIRECTION", "CRS"
    CALIBRATION_OUT, OUTPUT, RESIDUALS = "CALIBRATION_OUT", "OUTPUT", "RESIDUALS"
    METHODS = ["Similarity (shift, rotation, one scale)", "Affine (separate scales and skew)"]
    DIRECTIONS = ["Local to grid", "Grid to local"]

    def initAlgorithm(self, config=None):  # noqa: N802
        self.addParameter(QgsProcessingParameterFeatureSource(
            self.INPUT, tr("Layer to transform (optional)"), [Qgis.ProcessingSourceType.VectorAnyGeometry],
            optional=True))
        self.addParameter(QgsProcessingParameterFile(
            self.CONTROL, tr("Control points file (CSV, TXT, .loc, .cal text)"), optional=True,
            fileFilter=tr("Control points") + " (*.csv *.txt *.loc *.cal *.ctl);;" + tr("All files") + " (*.*)"))
        self.addParameter(QgsProcessingParameterString(
            self.CONTROL_TEXT, tr("Or type control points (name, local E N [H], grid E N [H] per line)"),
            multiLine=True, optional=True))
        self.addParameter(QgsProcessingParameterFile(
            self.CALIBRATION_IN, tr("Or use a saved calibration (.json)"), optional=True,
            fileFilter=tr("Calibration") + " (*.json)"))
        self.addParameter(QgsProcessingParameterEnum(
            self.METHOD, tr("Method"), [tr(m) for m in self.METHODS], defaultValue=0))
        self.addParameter(QgsProcessingParameterEnum(
            self.VERTICAL, tr("Heights"), [tr("No height correction"), tr("Height shift"), tr("Inclined plane (3+ points)")], defaultValue=0))
        self.addParameter(QgsProcessingParameterEnum(
            self.DIRECTION, tr("Direction"), [tr(d) for d in self.DIRECTIONS], defaultValue=0))
        self.addParameter(QgsProcessingParameterCrs(self.CRS, tr("Coordinate system of the output"),
                                                    optional=True))
        self.addParameter(QgsProcessingParameterFileDestination(
            self.CALIBRATION_OUT, tr("Save the calibration"), tr("Calibration") + " (*.json)", optional=True,
            createByDefault=False))
        self.addParameter(QgsProcessingParameterFeatureSink(
            self.OUTPUT, tr("Transformed"), optional=True, createByDefault=True))
        self.addParameter(QgsProcessingParameterFeatureSink(
            self.RESIDUALS, tr("Control point errors"), Qgis.ProcessingSourceType.VectorPoint, optional=True,
            createByDefault=False))

    def processAlgorithm(self, parameters, context, feedback):  # noqa: N802
        cal_path = self.parameterAsFile(parameters, self.CALIBRATION_IN, context)
        pairs = []
        vres = []
        if cal_path:
            try:
                with open(cal_path, encoding="utf-8") as f:
                    plane, vertical = calibration_from_json(json.load(f))
            except (OSError, ValueError, KeyError, TypeError) as e:
                raise QgsProcessingException(tr("The calibration file could not be read: {e}").format(e=e))
            feedback.pushInfo(tr("Using the saved calibration {p}.").format(p=os.path.basename(cal_path)))
        else:
            text = ""
            path = self.parameterAsFile(parameters, self.CONTROL, context)
            if path:
                if path.lower().endswith(".cal"):
                    with open(path, "rb") as f:
                        head = f.read(4096)
                    if b"\x00" in head:
                        raise QgsProcessingException(tr("This .cal file is in a binary format: export the "
                                                        "calibration points from the controller or Trimble "
                                                        "Business Center as CSV."))
                with open(path, encoding="utf-8-sig", errors="replace") as f:
                    text = f.read()
            text += "\n" + (self.parameterAsString(parameters, self.CONTROL_TEXT, context) or "")
            pairs = parse_control(text)
            method = self.parameterAsEnum(parameters, self.METHOD, context)
            minimum = 2 if method == 0 else 3
            if len(pairs) < minimum:
                raise QgsProcessingException(tr("This method needs at least {n} control points; {m} found.").format(
                    n=minimum, m=len(pairs)))
            xy = [((lo[0], lo[1]), (gr[0], gr[1])) for _, lo, gr in pairs]
            try:
                plane = fit_similarity(xy) if method == 0 else fit_affine(xy)
            except ValueError:
                raise QgsProcessingException(tr("The control points do not define a transformation "
                                                "(they coincide or lie on one line)."))
            vertical, vres = fit_vertical(pairs, self.parameterAsEnum(parameters, self.VERTICAL, context))
            feedback.pushInfo(tr("Scale {s}, rotation {r}°, {n} control points, horizontal RMSE {e} m").format(
                s=f"{plane.scale:.9f}", r=f"{plane.rotation:.6f}", n=len(pairs), e=f"{plane.rmse:.4f}"))
            for (name, lo, gr), (ex, ey, e) in zip(pairs, plane.residuals):
                feedback.pushInfo(f"  {name or '-'}: dE={ex:+.4f} dN={ey:+.4f} e={e:.4f}")
            if vres:
                feedback.pushInfo(tr("Height RMSE {e} m").format(
                    e=f"{math.sqrt(sum(r * r for r in vres) / len(vres)):.4f}"))

        crs = self.parameterAsCrs(parameters, self.CRS, context)
        results = {"SCALE": plane.scale, "ROTATION": plane.rotation, "RMSE": plane.rmse, "POINTS": len(pairs)}
        out_cal = self.parameterAsFileOutput(parameters, self.CALIBRATION_OUT, context)
        if out_cal:
            with open(out_cal, "w", encoding="utf-8") as f:
                json.dump(calibration_to_json(plane, vertical, pairs, crs.authid() if crs.isValid() else ""), f,
                          ensure_ascii=False, indent=1)
            results[self.CALIBRATION_OUT] = out_cal

        to_grid = self.parameterAsEnum(parameters, self.DIRECTION, context) == 0
        source = self.parameterAsSource(parameters, self.INPUT, context) if parameters.get(self.INPUT) else None
        if source is not None:
            if to_grid:
                use, dz = plane, (lambda x, y: vertical_value(vertical, x, y)) if vertical else None
            else:
                use = invert(plane)
                dz = None
                if vertical:
                    def dz(x, y, inv=use):  # the correction is defined on local coordinates
                        lx, ly = inv.apply(x, y)
                        return -vertical_value(vertical, lx, ly)
            out_crs = crs if crs.isValid() else source.sourceCrs()
            sink, dest = self.parameterAsSink(parameters, self.OUTPUT, context, source.fields(), source.wkbType(),
                                              out_crs)
            if sink is not None:
                total = source.featureCount() or 1
                for n, f in enumerate(source.getFeatures()):
                    if feedback.isCanceled():
                        return {}
                    out = QgsFeature(f)
                    out.setGeometry(transform_geometry(f.geometry(), use, dz))
                    sink.addFeature(out, QgsFeatureSink.Flag.FastInsert)
                    feedback.setProgress(100 * n / total)
                results[self.OUTPUT] = dest

        if pairs:
            rf = QgsFields()
            rf.append(QgsField("name", QMetaType.Type.QString))
            for n in ("local_e", "local_n", "grid_e", "grid_n", "error_e", "error_n", "error", "error_h"):
                rf.append(QgsField(n, QMetaType.Type.Double))
            rsink, rdest = self.parameterAsSink(parameters, self.RESIDUALS, context, rf, Qgis.WkbType.Point,
                                                crs if crs.isValid() else QgsCoordinateReferenceSystem())
            if rsink is not None:
                hres = iter(vres)
                for (name, lo, gr), (ex, ey, e) in zip(pairs, plane.residuals):
                    o = QgsFeature(rf)
                    o.setGeometry(QgsGeometry.fromPointXY(QgsPointXY(gr[0], gr[1])))
                    eh = next(hres, None) if (lo[2] is not None and gr[2] is not None and vertical) else None
                    o.setAttributes([name, lo[0], lo[1], gr[0], gr[1], ex, ey, e, eh])
                    rsink.addFeature(o, QgsFeatureSink.Flag.FastInsert)
                results[self.RESIDUALS] = rdest
        return results
