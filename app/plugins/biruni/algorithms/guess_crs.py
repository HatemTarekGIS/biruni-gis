"""What coordinate system is this? Ranks the coordinate systems that could hold
coordinates of unknown origin, by where they land."""
import math
import os
import re
import sqlite3

from qgis.core import (Qgis, QgsCoordinateReferenceSystem, QgsCoordinateTransform, QgsCsException, QgsFeature,
                       QgsFeatureRequest, QgsFeatureSink, QgsField, QgsFields, QgsGeometry, QgsPointXY,
                       QgsProcessing, QgsProcessingContext, QgsProcessingException, QgsProcessingParameterBoolean,
                       QgsProcessingParameterExtent, QgsProcessingParameterFeatureSink,
                       QgsProcessingParameterFeatureSource, QgsProcessingParameterPoint,
                       QgsProcessingParameterString, QgsProjUtils, QgsRectangle, QgsSpatialIndex, QgsVectorLayer)
from qgis.PyQt.QtCore import QMetaType

from .. import regional_crs
from ..i18n import lang, tr
from ..smart_table import normalise_text
from .base import BiruniAlgorithm

WGS84 = QgsCoordinateReferenceSystem("EPSG:4326")
WORLD = os.path.join(os.path.dirname(os.path.dirname(__file__)), "resources", "data", "world.gpkg")
MAX_POINTS = 50
MAX_CANDIDATES = 450
SAME_M = 1.0          # candidates closer than this to the best one give the same position

# Systems in use in the Middle East and North Africa (matched on the EPSG name)
REGIONAL = ("Ain el Abd", "KSA-GRF17", "MTRF-2000", "Egypt", "Nahrwan", "Karbala", "Deir ez Zor", "Palestine",
            "Israel", "Qatar", "QND95", "Dubai", "Jordan", "Iraq", "KUDAMS", "Bahrain", "PSD93", "Fahud",
            "LGD2006", "ELD79", "Yemen", "Nord Sahara", "Voirol", "Merchich", "Carthage", "Adindan", "Syria",
            "Levant", "ED50 / UTM", "ED50 / TM")
# Geographic (degrees) systems tried for coordinates that look like degrees
GEOGRAPHIC = ("EPSG:4326", "EPSG:4204", "EPSG:4229", "EPSG:4230", "EPSG:4270", "EPSG:4743", "EPSG:4227",
              "EPSG:4281")
# Preferred first when two systems give the same position (the common ones)
COMMON = ("EPSG:4326", "EPSG:3857")


def haversine_km(lon1, lat1, lon2, lat2):
    p1, p2 = math.radians(lat1), math.radians(lat2)
    a = (math.sin((p2 - p1) / 2) ** 2
         + math.cos(p1) * math.cos(p2) * math.sin(math.radians(lon2 - lon1) / 2) ** 2)
    return 2 * 6371.0088 * math.asin(min(1.0, math.sqrt(a)))


def parse_pairs(text):
    """(x, y) pairs from pasted text, one point per line. With three or more
    numbers on a line, a leading point number is skipped; otherwise the first
    two numbers are X and Y (a trailing height is ignored)."""
    pairs = []
    for line in (text or "").splitlines():
        s = normalise_text(line)
        nums = [float(n) for n in re.findall(r"-?\d+(?:\.\d+)?", s)]
        if len(nums) < 2:
            continue
        if len(nums) >= 3 and nums[0] == int(nums[0]) and abs(nums[0]) < min(abs(nums[1]), abs(nums[2])) / 10:
            nums = nums[1:]
        pairs.append((nums[0], nums[1]))
    return pairs


def spread(items, n=MAX_POINTS):
    """At most n items, evenly spread over the list."""
    if len(items) <= n:
        return list(items)
    step = len(items) / n
    return [items[int(i * step)] for i in range(n)]


def sample_source(source, feedback=None, limit=5000):
    """Raw coordinates (whatever the layer claims its CRS is) of a few vertices
    of a few thousand features."""
    pts = []
    req = QgsFeatureRequest().setNoAttributes().setLimit(limit)
    for f in source.getFeatures(req):
        if feedback is not None and feedback.isCanceled():
            break
        g = f.geometry()
        if g is None or g.isNull() or g.isEmpty():
            continue
        for k, v in enumerate(g.vertices()):
            if k >= 3:
                break
            if math.isfinite(v.x()) and math.isfinite(v.y()):
                pts.append((v.x(), v.y()))
    return spread(pts)


def proj_db():
    for p in QgsProjUtils.searchPaths():
        db = os.path.join(p, "proj.db")
        if os.path.exists(db):
            return db
    return None


def candidate_codes(location=None, points=None):
    """{key: name} of the systems to try: WGS 84 and its UTM zones, Web
    Mercator, the regional systems, and - with a location - every EPSG system
    made for that place plus the regional catalogue (legacy systems, sign and
    unit variants, Chinese web-map offsets). With the points, systems hinted by
    the numbers themselves (a Gauss-Krüger zone written in the easting)."""
    codes = _base_codes(location)
    extra = {}
    if points:
        extra.update(regional_crs.candidates_for_values(points))
    if location is not None:
        extra.update(regional_crs.candidates_for_point(*location))
    for k, v in extra.items():
        codes.setdefault(k, v)
    return codes


def _base_codes(location=None):
    codes = {a: "" for a in GEOGRAPHIC}
    codes["EPSG:3857"] = ""
    for z in range(1, 61):
        codes[f"EPSG:{32600 + z}"] = ""
        codes[f"EPSG:{32700 + z}"] = ""
    db = proj_db()
    if db is None:
        return codes
    try:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    except sqlite3.Error:
        return codes
    try:
        for pat in REGIONAL:
            for code, name in con.execute("SELECT code, name FROM projected_crs WHERE auth_name='EPSG' AND "
                                          "deprecated=0 AND name LIKE ?", (f"%{pat}%",)):
                codes.setdefault(f"EPSG:{code}", name)
        if location is not None:
            lon, lat = location
            for table, extra in (("projected_crs", ""), ("geodetic_crs", " AND c.type='geographic 2D'")):
                sql = (f"SELECT c.code, c.name FROM {table} c JOIN usage u ON u.object_table_name='{table}' "
                       "AND u.object_auth_name=c.auth_name AND u.object_code=c.code JOIN extent e ON "
                       "e.auth_name=u.extent_auth_name AND e.code=u.extent_code WHERE c.auth_name='EPSG' AND "
                       "c.deprecated=0 AND ? BETWEEN e.south_lat AND e.north_lat AND ((e.west_lon <= e.east_lon "
                       "AND ? BETWEEN e.west_lon AND e.east_lon) OR (e.west_lon > e.east_lon AND (? >= e.west_lon "
                       f"OR ? <= e.east_lon))){extra}")
                for code, name in con.execute(sql, (lat, lon, lon, lon)):
                    if "grid system" in name.lower():
                        continue
                    codes.setdefault(f"EPSG:{code}", name)
    finally:
        con.close()
    return dict(list(codes.items())[:MAX_CANDIDATES])


class Land:
    """Which country a WGS 84 position falls in (bundled world map)."""

    def __init__(self):
        self.geoms, self.names = {}, {}
        self.index = QgsSpatialIndex()
        if not os.path.exists(WORLD):
            return
        lyr = QgsVectorLayer(f"{WORLD}|layername=countries", "countries", "ogr")
        if not lyr.isValid():
            return
        field = "name_ar" if lang() == "ar" and lyr.fields().indexOf("name_ar") >= 0 else "name"
        for f in lyr.getFeatures():
            g = f.geometry()
            if g.isNull():
                continue
            self.geoms[f.id()] = g
            self.names[f.id()] = f[field] or f["name"]
            self.index.addFeature(f.id(), g.boundingBox())

    def country(self, lon, lat):
        p = QgsGeometry.fromPointXY(QgsPointXY(lon, lat))
        for fid in self.index.intersects(QgsRectangle(lon, lat, lon, lat)):
            if self.geoms[fid].contains(p):
                return self.names[fid]
        return ""


def _distance_to_rect(lon, lat, rect):
    cl = min(max(lon, rect.xMinimum()), rect.xMaximum())
    ca = min(max(lat, rect.yMinimum()), rect.yMaximum())
    return haversine_km(lon, lat, cl, ca)


def evaluate(points, codes, transform_context, location=None, extent=None, land=None, feedback=None):
    """One result per candidate and axis order that turns the points into valid
    positions: {crs, name, swapped, lonlat, centre, inside, distance_km, country}."""
    results = []
    total = max(len(codes), 1)
    degrees_like = all(-180 <= x <= 180 and -90 <= y <= 90 for x, y in points)
    for n, (authid, name) in enumerate(codes.items()):
        if feedback is not None:
            if feedback.isCanceled():
                break
            feedback.setProgress(90 * n / total)
        spec = regional_crs.resolve(authid)
        crs = spec.crs
        xf = None
        if spec.func is None:
            if crs is None:
                continue
            try:
                xf = QgsCoordinateTransform(crs, WGS84, transform_context)
            except Exception:  # noqa: BLE001 - no operation between the datums
                continue
        geographic = spec.is_geographic()
        bounds = spec.bounds
        for swapped in (False, True):
            out = []
            for x, y in points:
                if swapped:
                    x, y = y, x
                x, y = spec.pre(x, y)
                if geographic and not (-180 <= x <= 180 and -90 <= y <= 90):
                    out = []
                    break
                if spec.func is not None:
                    out.append(spec.func(x, y))
                    continue
                try:
                    p = xf.transform(QgsPointXY(x, y))
                except QgsCsException:
                    continue
                if math.isfinite(p.x()) and math.isfinite(p.y()) and -180 <= p.x() <= 180 and -90 <= p.y() <= 90:
                    out.append((p.x(), p.y()))
            if len(out) < max(1, len(points) // 2):
                continue
            inside = sum(1 for lo, la in out if bounds.isEmpty() or (
                bounds.xMinimum() - 0.1 <= lo <= bounds.xMaximum() + 0.1
                and bounds.yMinimum() - 0.1 <= la <= bounds.yMaximum() + 0.1)) / len(out)
            centre = (sum(p[0] for p in out) / len(out), sum(p[1] for p in out) / len(out))
            dist = None
            if location is not None:
                dist = min(haversine_km(lo, la, location[0], location[1]) for lo, la in out)
            elif extent is not None:
                dist = min(_distance_to_rect(lo, la, extent) for lo, la in out)
            results.append({"crs": authid, "name": name or spec.label or (crs.description() if crs else authid),
                            "swapped": swapped, "points": out,
                            "centre": centre, "inside": inside, "distance_km": dist,
                            "country": land.country(*centre) if land is not None else "",
                            # numbers that look like degrees are not metres a few steps from a false origin
                            "implausible": degrees_like and not geographic,
                            "area": max(bounds.width() * bounds.height(), 1e-9) if not bounds.isEmpty() else 1e9})
    return results


def rank(results):
    """Best first. With a location: nearest (to 10 m), then inside the system's
    area of use; without one: degrees read as degrees, inside the area of use,
    on land, the most local system. Ties go to the axis order as stored and to
    the common systems."""
    def key(r):
        common = 0 if r["crs"] in COMMON or r["crs"].startswith(("EPSG:326", "EPSG:327")) else 1
        regional = r["crs"].startswith("REG:")  # an EPSG system wins a tie with a home-made one
        if r["distance_km"] is not None:
            return (round(r["distance_km"], 2), r["inside"] < 0.9, r["swapped"], regional, common)
        return (r["implausible"], r["inside"] < 0.9, not r["country"], r["swapped"], regional, r["area"], common)
    return sorted(results, key=key)


def apply_to_geometry(geom, key, swapped):
    """A copy of a geometry with the fix of a candidate applied: axes swapped, signs or units changed, Chinese
    web-map offsets removed. Returns (geometry, crs to give it)."""
    from qgis.PyQt.QtGui import QTransform

    spec = regional_crs.resolve(key)
    g = QgsGeometry(geom)
    if g.isNull():
        return g, spec.crs if spec.crs is not None else WGS84
    if swapped:
        g.get().swapXy()
    if spec.scaled:
        g.transform(QTransform(spec.sx * spec.factor, 0, 0, spec.sy * spec.factor, 0, 0))
    if spec.func is not None:
        i = 0
        while True:
            v = g.vertexAt(i)
            if v.isEmpty():
                break
            lo, la = spec.func(v.x(), v.y())
            g.moveVertex(lo, la, i)
            i += 1
        return g, WGS84
    return g, spec.crs


class GuessCrs(BiruniAlgorithm):
    """Finds the coordinate system of coordinates that came without one."""

    ID = "guesscrs"
    NAME = "What coordinate system is this?"
    GROUP = "Engineering office"
    GROUP_ID = "office"
    HELP = ("For coordinates that came without their coordinate system - a layer whose system is missing or "
            "wrong, or X Y values pasted from Excel, a report or a drawing. Every likely system is tried: "
            "degrees, every UTM zone, Web Mercator and the systems used in the region (Ain el Abd, KSA-GRF17, "
            "the Egyptian belts, Nahrwan, Karbala, Palestine, Jordan TM, Qatar, Dubai, ED50 and others), with X "
            "and Y also swapped; each one is ranked by where the points then land. Click on the map where the data "
            "really is (or give an area): click exactly on a feature you know to tell apart systems whose "
            "positions differ by only 100-300 m, such as Ain el Abd and WGS 84. Without a location the list "
            "still shows which country the points would fall in for each system, but many systems fit equally - "
            "a location makes the answer certain. The best system can also be given to a copy of the layer "
            "(the coordinates are not changed, except that X and Y are exchanged when they were swapped).")

    INPUT, TEXT, LOCATION, EXTENT = "INPUT", "TEXT", "LOCATION", "EXTENT"
    OUTPUT, ASSIGN, OUTPUT_LAYER = "OUTPUT", "ASSIGN", "OUTPUT_LAYER"

    def initAlgorithm(self, config=None):  # noqa: N802
        self.addParameter(QgsProcessingParameterFeatureSource(
            self.INPUT, tr("Layer with unknown coordinate system"), [Qgis.ProcessingSourceType.Vector],
            optional=True))
        self.addParameter(QgsProcessingParameterString(
            self.TEXT, tr("Or paste coordinates (one point per line: X Y)"), multiLine=True, optional=True))
        self.addParameter(QgsProcessingParameterPoint(
            self.LOCATION, tr("Where the data really is (click on the map)"), optional=True))
        self.addParameter(QgsProcessingParameterExtent(
            self.EXTENT, tr("Or the area where the data is"), optional=True))
        self.addParameter(QgsProcessingParameterBoolean(
            self.ASSIGN, tr("Give the best system to a copy of the layer"), False))
        self.addParameter(QgsProcessingParameterFeatureSink(
            self.OUTPUT, tr("Likely coordinate systems"), Qgis.ProcessingSourceType.Vector))
        self.addParameter(QgsProcessingParameterFeatureSink(
            self.OUTPUT_LAYER, tr("Layer with the best system"), Qgis.ProcessingSourceType.VectorAnyGeometry,
            optional=True, createByDefault=False))

    @staticmethod
    def _transformations(best, points, context, feedback):
        """The transformations from the best system to WGS 84, how far apart they put the data, and the
        official grids that are not installed yet."""
        from .. import proj_grids

        spec = regional_crs.resolve(best["crs"])
        if spec.func is not None or spec.crs is None or spec.crs.authid() == "EPSG:4326":
            return {"TRANSFORMS": [], "TRANSFORM_SPREAD_M": 0.0, "MISSING_GRIDS": []}
        x, y = points[0]
        if best["swapped"]:
            x, y = y, x
        x, y = spec.pre(x, y)
        try:
            info = proj_grids.transformation_spread(spec.crs, x, y, context.transformContext())
        except Exception:  # noqa: BLE001 - informative only
            return {"TRANSFORMS": [], "TRANSFORM_SPREAD_M": 0.0, "MISSING_GRIDS": []}
        ops = info["ops"]
        if len(ops) > 1:
            feedback.pushInfo(tr("{n} ways to convert {crs} to WGS 84 put the data up to {m} m apart:").format(
                n=len(ops), crs=best["crs"], m=f"{info['spread_m']:.1f}"))
            for o in ops[:6]:
                acc = tr("accuracy {a} m").format(a=o["accuracy"]) if o["accuracy"] is not None and o["accuracy"] >= 0 \
                    else tr("accuracy unknown")
                feedback.pushInfo(f"  • {o['name']} ({acc}; {o['shift_m']:.1f} m)")
        grids = [g for g in info["missing"] if g["downloadable"]]
        if grids:
            feedback.pushInfo(tr("More accurate with the official grid {g}: download it with 'Download shift "
                                 "grids'.").format(g=", ".join(g["name"] for g in grids[:3])))
        return {"TRANSFORMS": [o["name"] for o in ops], "TRANSFORM_SPREAD_M": info["spread_m"],
                "MISSING_GRIDS": [g["name"] for g in info["missing"]]}

    @staticmethod
    def _given(parameters, name):
        v = parameters.get(name)
        return v is not None and str(v).strip() != ""

    def processAlgorithm(self, parameters, context, feedback):  # noqa: N802
        source = self.parameterAsSource(parameters, self.INPUT, context) if self._given(parameters,
                                                                                         self.INPUT) else None
        text = self.parameterAsString(parameters, self.TEXT, context)
        if source is not None:
            points = sample_source(source, feedback)
        else:
            points = spread(parse_pairs(text))
        if not points:
            raise QgsProcessingException(tr("Give a layer or paste coordinates (X Y on each line)."))

        location = extent = None
        if self._given(parameters, self.LOCATION):
            p = self.parameterAsPoint(parameters, self.LOCATION, context, WGS84)
            location = (p.x(), p.y())
        elif self._given(parameters, self.EXTENT):
            extent = self.parameterAsExtent(parameters, self.EXTENT, context, WGS84)
            if extent.isEmpty():
                extent = None
        if location is None and extent is None:
            feedback.pushWarning(tr("No location given: many systems may fit. Click where the data really is "
                                    "for a certain answer."))

        anchor = location if location is not None else (extent.center().x(), extent.center().y()) \
            if extent is not None else None
        codes = candidate_codes(anchor, points)
        feedback.pushInfo(tr("Trying {n} coordinate systems on {p} points…").format(n=len(codes), p=len(points)))
        land = Land()
        results = rank(evaluate(points, codes, context.transformContext(), location, extent, land, feedback))
        if not results:
            raise QgsProcessingException(tr("No coordinate system turns these numbers into positions on Earth."))

        best = results[0]
        bx, by = best["centre"]
        fields = QgsFields()
        for name, kind in (("rank", QMetaType.Type.Int), ("crs", QMetaType.Type.QString),
                           ("name", QMetaType.Type.QString), ("distance_km", QMetaType.Type.Double),
                           ("swapped", QMetaType.Type.Bool), ("inside_area", QMetaType.Type.Bool),
                           ("country", QMetaType.Type.QString), ("differs_m", QMetaType.Type.Double),
                           ("lon", QMetaType.Type.Double), ("lat", QMetaType.Type.Double),
                           ("note", QMetaType.Type.QString)):
            fields.append(QgsField(name, kind))
        sink, dest = self.parameterAsSink(parameters, self.OUTPUT, context, fields, Qgis.WkbType.NoGeometry,
                                          QgsCoordinateReferenceSystem())
        for i, r in enumerate(results, 1):
            cx, cy = r["centre"]
            differs = haversine_km(bx, by, cx, cy) * 1000
            notes = []
            if i > 1 and differs < SAME_M:
                notes.append(tr("same position as the best one"))
            if r["inside"] < 0.9:
                notes.append(tr("outside the area this system is made for"))
            if not r["country"]:
                notes.append(tr("in the sea or outside any country"))
            if r["swapped"]:
                notes.append(tr("X and Y swapped"))
            f = QgsFeature(fields)
            f.setAttributes([i, r["crs"], r["name"], r["distance_km"], r["swapped"], r["inside"] >= 0.9,
                             r["country"], differs, cx, cy, "; ".join(notes)])
            sink.addFeature(f, QgsFeatureSink.Flag.FastInsert)

        for i, r in enumerate(results[:5], 1):
            where = r["country"] or tr("sea")
            dist = "" if r["distance_km"] is None else tr(" - {d} km away").format(d=f"{r['distance_km']:.3f}")
            swap = tr(" (X and Y swapped)") if r["swapped"] else ""
            feedback.pushInfo(f"{i}. {r['crs']} {r['name']}{swap}: {where}{dist}")
        if source is not None and source.sourceCrs().isValid():
            claimed = source.sourceCrs().authid()
            pos = next((i for i, r in enumerate(results, 1) if r["crs"] == claimed and not r["swapped"]), None)
            feedback.pushInfo(tr("The layer says {crs}: rank {n}.").format(crs=claimed, n=pos if pos else "-"))

        out = {self.OUTPUT: dest, "TOP_CRS": best["crs"], "TOP_SWAPPED": best["swapped"],
               "CANDIDATE_COUNT": len(results)}
        note = regional_crs.axis_note(best["crs"])
        if note and best["swapped"]:
            feedback.pushInfo(note)
        out.update(self._transformations(best, points, context, feedback))
        assign = self.parameterAsBoolean(parameters, self.ASSIGN, context)
        if assign or self._given(parameters, self.OUTPUT_LAYER):
            if source is None:
                feedback.pushWarning(tr("Pasted coordinates have no layer to copy."))
            else:
                params = dict(parameters)
                load = not self._given(parameters, self.OUTPUT_LAYER)
                if load:
                    params[self.OUTPUT_LAYER] = QgsProcessing.TEMPORARY_OUTPUT
                _g, crs = apply_to_geometry(QgsGeometry(), best["crs"], False)
                lsink, ldest = self.parameterAsSink(params, self.OUTPUT_LAYER, context, source.fields(),
                                                    source.wkbType(), crs)
                if load:
                    context.addLayerToLoadOnCompletion(ldest, QgsProcessingContext.LayerDetails(
                        tr("Layer with the best system"), context.project(), self.OUTPUT_LAYER))
                for f in source.getFeatures():
                    if feedback.isCanceled():
                        break
                    g = f.geometry()
                    if g is not None and not g.isNull():
                        g, _crs = apply_to_geometry(g, best["crs"], best["swapped"])
                        f.setGeometry(g)
                    lsink.addFeature(f, QgsFeatureSink.Flag.FastInsert)
                out[self.OUTPUT_LAYER] = ldest
        feedback.setProgress(100)
        return out


# ---- why is it in the wrong place? ------------------------------------------------

# (label, factor that turns the file's numbers into metres)
LENGTH_UNITS = [("metres", 1.0), ("millimetres", 0.001), ("centimetres", 0.01), ("international feet", 0.3048),
         ("US survey feet", 1200.0 / 3937.0), ("inches", 0.0254), ("kilometres", 1000.0)]
CLOSE_KM = 5.0  # a fix that lands the data this close to the place is a real answer


def local_codes(codes, anchor, claimed=""):
    """The candidates made for the place (and the layer's own system)."""
    lon, lat = anchor
    keep = {}
    if claimed:
        keep[claimed] = codes.get(claimed, "")
    zone = int((lon + 180) // 6) + 1
    keep[f"EPSG:{(32600 if lat >= 0 else 32700) + zone}"] = ""
    for authid, name in codes.items():
        crs = QgsCoordinateReferenceSystem(authid)
        b = crs.bounds()
        if crs.isValid() and not crs.isGeographic() and not b.isEmpty() and \
                b.xMinimum() <= lon <= b.xMaximum() and b.yMinimum() <= lat <= b.yMaximum():
            keep.setdefault(authid, name)
    return keep


class WrongPlace(BiruniAlgorithm):
    """Data that shows up in the wrong place: wrong units, swapped axes or the wrong system."""

    ID = "wrongplace"
    NAME = "Why is it in the wrong place?"
    GROUP = "Engineering office"
    GROUP_ID = "office"
    HELP = ("A DXF, DWG or shapefile that lands in the sea, in another country or a thousand times too big is "
            "almost always one of three mistakes: the drawing is in millimetres, centimetres or feet instead of "
            "metres, X and Y are swapped, or the coordinate system is wrong. Click where the data really is (or "
            "give the area): every unit - metres, millimetres, centimetres, international and US survey feet, "
            "inches, kilometres - is tried with both axis orders and every coordinate system made for that place, "
            "and the fixes are ranked by how close they bring the data. The answer reads like 'the drawing is in "
            "millimetres: scale 0.001, EPSG:32638'. A corrected copy of the layer is made in one step. When no "
            "fix comes close, the drawing is in local coordinates: use Site calibration or Move CAD/Scan to Real "
            "Coordinates with a few known points.")

    INPUT, LOCATION, EXTENT, OUTPUT, CORRECTED = "INPUT", "LOCATION", "EXTENT", "OUTPUT", "CORRECTED"

    def initAlgorithm(self, config=None):  # noqa: N802
        self.addParameter(QgsProcessingParameterFeatureSource(
            self.INPUT, tr("Layer in the wrong place"), [Qgis.ProcessingSourceType.VectorAnyGeometry]))
        self.addParameter(QgsProcessingParameterPoint(
            self.LOCATION, tr("Where the data really is (click on the map)"), optional=True))
        self.addParameter(QgsProcessingParameterExtent(
            self.EXTENT, tr("Or the area where the data is"), optional=True))
        self.addParameter(QgsProcessingParameterFeatureSink(
            self.OUTPUT, tr("Possible fixes"), Qgis.ProcessingSourceType.Vector))
        self.addParameter(QgsProcessingParameterFeatureSink(
            self.CORRECTED, tr("Corrected layer"), Qgis.ProcessingSourceType.VectorAnyGeometry, optional=True,
            createByDefault=True))

    def processAlgorithm(self, parameters, context, feedback):  # noqa: N802
        source = self.parameterAsSource(parameters, self.INPUT, context)
        if source is None:
            raise QgsProcessingException(self.invalidSourceError(parameters, self.INPUT))
        location = extent = None
        if GuessCrs._given(parameters, self.LOCATION):
            p = self.parameterAsPoint(parameters, self.LOCATION, context, WGS84)
            location = (p.x(), p.y())
        elif GuessCrs._given(parameters, self.EXTENT):
            extent = self.parameterAsExtent(parameters, self.EXTENT, context, WGS84)
            if extent.isEmpty():
                extent = None
        if location is None and extent is None:
            raise QgsProcessingException(tr("Click on the map where the data really is, or give the area."))
        anchor = location if location is not None else (extent.center().x(), extent.center().y())
        points = sample_source(source, feedback)
        if not points:
            raise QgsProcessingException(tr("The layer has no coordinates to check."))

        claimed_crs = source.sourceCrs()
        claimed = claimed_crs.authid() if claimed_crs.isValid() else ""
        if claimed_crs.isValid():
            try:
                xf = QgsCoordinateTransform(claimed_crs, WGS84, context.transformContext())
                now = [xf.transform(QgsPointXY(x, y)) for x, y in points]
                c = (sum(q.x() for q in now) / len(now), sum(q.y() for q in now) / len(now))
                away = haversine_km(c[0], c[1], anchor[0], anchor[1])
                feedback.pushInfo(tr("As it is ({crs}), the layer is {d} km from where it should be.").format(
                    crs=claimed, d=f"{away:,.1f}"))
            except Exception:  # noqa: BLE001 - coordinates that the system cannot place
                feedback.pushInfo(tr("As it is ({crs}), the layer's coordinates are not valid positions.").format(
                    crs=claimed))

        all_codes = candidate_codes(anchor)
        if claimed:
            all_codes.setdefault(claimed, claimed_crs.description())
        near = local_codes(all_codes, anchor, claimed)
        results = []
        for k, (unit, factor) in enumerate(LENGTH_UNITS):
            if feedback.isCanceled():
                break
            scaled = [(x * factor, y * factor) for x, y in points]
            codes = all_codes if factor == 1.0 else near
            for r in evaluate(scaled, codes, context.transformContext(), location, extent):
                r["unit"], r["factor"] = unit, factor
                results.append(r)
            feedback.setProgress(90 * (k + 1) / len(LENGTH_UNITS))

        def key(r):
            return (round(r["distance_km"], 2), r["inside"] < 0.9, r["factor"] != 1.0, r["crs"] != claimed,
                    r["swapped"], r["crs"] not in COMMON and not r["crs"].startswith(("EPSG:326", "EPSG:327")))

        results.sort(key=key)
        if not results:
            raise QgsProcessingException(tr("No unit or coordinate system turns these numbers into positions on "
                                            "Earth."))
        best = results[0]
        fields = QgsFields()
        for name, kind in (("rank", QMetaType.Type.Int), ("unit", QMetaType.Type.QString),
                           ("scale", QMetaType.Type.Double), ("crs", QMetaType.Type.QString),
                           ("name", QMetaType.Type.QString), ("swapped", QMetaType.Type.Bool),
                           ("distance_km", QMetaType.Type.Double), ("note", QMetaType.Type.QString)):
            fields.append(QgsField(name, kind))
        sink, dest = self.parameterAsSink(parameters, self.OUTPUT, context, fields, Qgis.WkbType.NoGeometry,
                                          QgsCoordinateReferenceSystem())
        for i, r in enumerate(results[:30], 1):
            notes = []
            if r["factor"] != 1.0:
                notes.append(tr("the numbers are in {u}").format(u=tr(r["unit"])))
            if r["swapped"]:
                notes.append(tr("X and Y swapped"))
            if claimed and r["crs"] != claimed:
                notes.append(tr("different coordinate system"))
            f = QgsFeature(fields)
            f.setAttributes([i, tr(r["unit"]), r["factor"], r["crs"], r["name"], r["swapped"], r["distance_km"],
                             "; ".join(notes)])
            sink.addFeature(f, QgsFeatureSink.Flag.FastInsert)

        close = best["distance_km"] is not None and best["distance_km"] <= CLOSE_KM
        parts = []
        if best["factor"] != 1.0:
            parts.append(tr("the drawing is in {u}: scale {f}").format(u=tr(best["unit"]), f=f"{best['factor']:g}"))
        if best["swapped"]:
            parts.append(tr("X and Y are swapped"))
        if best["crs"] != claimed:
            parts.append(tr("its coordinate system is {crs}").format(crs=best["crs"]))
        if close:
            summary = "; ".join(parts) if parts else tr("the layer is already in the right place")
            feedback.pushInfo(tr("Answer: {s} ({d} km from the place).").format(s=summary,
                                                                               d=f"{best['distance_km']:.2f}"))
        else:
            summary = tr("no unit or axis fix brings the data near the place - it is probably in local "
                         "coordinates: use Site calibration or Move CAD/Scan to Real Coordinates with a few known "
                         "points")
            feedback.pushWarning(summary)

        out = {self.OUTPUT: dest, "UNIT": best["unit"], "SCALE": best["factor"], "CRS": best["crs"],
               "SWAPPED": best["swapped"], "DISTANCE_KM": best["distance_km"], "SUMMARY": summary, "FOUND": close}
        if close:
            _g, crs = apply_to_geometry(QgsGeometry(), best["crs"], False)
            csink, cdest = self.parameterAsSink(parameters, self.CORRECTED, context, source.fields(),
                                                source.wkbType(), crs)
            if csink is not None:
                from qgis.PyQt.QtGui import QTransform

                scale = QTransform(best["factor"], 0, 0, best["factor"], 0, 0)
                for f in source.getFeatures():
                    if feedback.isCanceled():
                        break
                    g = QgsGeometry(f.geometry())
                    if not g.isNull():
                        if best["factor"] != 1.0:
                            g.transform(scale)
                        g, _crs = apply_to_geometry(g, best["crs"], best["swapped"])
                        f.setGeometry(g)
                    csink.addFeature(f, QgsFeatureSink.Flag.FastInsert)
                out[self.CORRECTED] = cdest
        feedback.setProgress(100)
        return out
