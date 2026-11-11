"""LandXML 1.2: points (CgPoints), surfaces (TIN) and parcels (CoordGeom) in and
out - the exchange format of Civil 3D, Trimble Business Center, Carlson and
machine-control systems."""
import datetime
import math
import os
import xml.etree.ElementTree as ET

from qgis.core import (Qgis, QgsCoordinateReferenceSystem, QgsCoordinateTransform, QgsFeature, QgsFeatureSink,
                       QgsField, QgsFields, QgsGeometry, QgsLineString, QgsPoint, QgsPolygon,
                       QgsProcessingException, QgsProcessingParameterBoolean, QgsProcessingParameterCrs,
                       QgsProcessingParameterFeatureSink, QgsProcessingParameterFeatureSource,
                       QgsProcessingParameterField, QgsProcessingParameterFile,
                       QgsProcessingParameterFileDestination, QgsProcessingParameterNumber,
                       QgsProcessingParameterRasterDestination, QgsProcessingParameterRasterLayer,
                       QgsProcessingParameterString)
from qgis.PyQt.QtCore import QMetaType

from ..i18n import tr
from .base import BiruniAlgorithm

NS = "http://www.landxml.org/schema/LandXML-1.2"
NODATA = -9999.0
MAX_BYTES = 1_500_000_000


def _strip_ns(root):
    for el in root.iter():
        if isinstance(el.tag, str) and "}" in el.tag:
            el.tag = el.tag.split("}", 1)[1]
    return root


def _nez(text):
    """LandXML coordinates are 'northing easting [elevation]'."""
    vals = [float(v) for v in (text or "").replace(",", " ").split()]
    if len(vals) < 2:
        raise ValueError("coordinate")
    return vals[0], vals[1], (vals[2] if len(vals) > 2 else None)


# ---- reading ------------------------------------------------------------------------

def read_landxml(path):
    """{'units', 'epsg', 'points': [(name, code, desc, n, e, z)], 'surfaces': [{name,
    points:{id:(n,e,z)}, faces:[(a,b,c)], hidden}], 'parcels': [{name, area, ring:
    [(n,e)]}]}"""
    root = _strip_ns(ET.parse(path).getroot())
    out = {"units": "", "epsg": "", "points": [], "surfaces": [], "parcels": []}
    units = root.find("Units")
    if units is not None and len(units):
        u = units[0]
        out["units"] = f"{u.tag} {u.get('linearUnit', '')}".strip()
    cs = root.find("CoordinateSystem")
    if cs is not None:
        out["epsg"] = cs.get("epsgCode", "") or ""
    names = {}
    for cg in root.iter("CgPoint"):
        try:
            n, e, z = _nez(cg.text)
        except ValueError:
            ref = cg.get("pntRef")
            if ref and ref in names:
                n, e, z = names[ref]
            else:
                continue
        name = cg.get("name") or cg.get("oID") or str(len(out["points"]) + 1)
        names[name] = (n, e, z)
        out["points"].append((name, cg.get("code", ""), cg.get("desc", ""), n, e, z))
    for surf in root.iter("Surface"):
        definition = surf.find("Definition")
        if definition is None:
            continue
        pts = {}
        for p in definition.iter("P"):
            try:
                pts[p.get("id")] = _nez(p.text)
            except ValueError:
                continue
        faces, hidden = [], 0
        for f in definition.iter("F"):
            if f.get("i") == "1":  # invisible (deleted) triangle
                hidden += 1
                continue
            ids = (f.text or "").split()
            if len(ids) >= 3 and all(i in pts for i in ids[:3]):
                faces.append(tuple(ids[:3]))
        out["surfaces"].append({"name": surf.get("name", f"Surface {len(out['surfaces']) + 1}"), "points": pts,
                                "faces": faces, "hidden": hidden})
    for parcel in root.iter("Parcel"):
        geom = parcel.find("CoordGeom")
        if geom is None:
            continue
        ring = []
        for el in geom:
            try:
                start = _point_of(el.find("Start"), names)
                end = _point_of(el.find("End"), names)
            except ValueError:
                continue
            if not ring:
                ring.append(start)
            if el.tag == "Curve":
                centre = _point_of(el.find("Center"), names)
                ring.extend(_arc(start, end, centre, el.get("rot", "ccw"))[1:])
            ring.append(end)
        if len(ring) >= 3:
            area = parcel.get("area")
            out["parcels"].append({"name": parcel.get("name", ""), "area": float(area) if area else None,
                                   "desc": parcel.get("desc", ""), "ring": ring})
    return out


def _point_of(el, names):
    if el is None:
        raise ValueError("missing")
    if el.text and el.text.strip():
        n, e, _ = _nez(el.text)
        return n, e
    ref = el.get("pntRef")
    if ref in names:
        return names[ref][:2]
    raise ValueError("ref")


def _arc(start, end, centre, rot, step_deg=2.0):
    """Points along a circular arc (northing, easting tuples), start included."""
    (n1, e1), (n2, e2), (nc, ec) = start, end, centre
    r = math.hypot(e1 - ec, n1 - nc)
    a1 = math.atan2(n1 - nc, e1 - ec)
    a2 = math.atan2(n2 - nc, e2 - ec)
    sweep = a2 - a1
    if rot == "cw":
        while sweep > 0:
            sweep -= 2 * math.pi
    else:
        while sweep < 0:
            sweep += 2 * math.pi
    steps = max(2, int(abs(math.degrees(sweep)) / step_deg))
    return [(nc + r * math.sin(a1 + sweep * k / steps), ec + r * math.cos(a1 + sweep * k / steps))
            for k in range(steps)]


def tin_to_raster(points, faces, path, cell, wkt=""):
    """Rasterise the TIN exactly (linear inside every triangle) into a GeoTIFF."""
    import numpy as np
    from osgeo import gdal

    es = [p[1] for p in points.values()]
    ns = [p[0] for p in points.values()]
    xmin, xmax, ymin, ymax = min(es), max(es), min(ns), max(ns)
    w = max(1, int(math.ceil((xmax - xmin) / cell)))
    h = max(1, int(math.ceil((ymax - ymin) / cell)))
    if w * h * 4 > MAX_BYTES:
        raise ValueError("too large")
    z = np.full((h, w), NODATA, dtype="float32")
    for a, b, c in faces:
        (n0, e0, z0), (n1, e1, z1), (n2, e2, z2) = points[a], points[b], points[c]
        if None in (z0, z1, z2):
            continue
        det = (n1 - n2) * (e0 - e2) + (e2 - e1) * (n0 - n2)
        if abs(det) < 1e-12:
            continue
        c0 = max(0, int(math.floor((min(e0, e1, e2) - xmin) / cell)))
        c1 = min(w - 1, int(math.ceil((max(e0, e1, e2) - xmin) / cell)))
        r0 = max(0, int(math.floor((ymax - max(n0, n1, n2)) / cell)))
        r1 = min(h - 1, int(math.ceil((ymax - min(n0, n1, n2)) / cell)))
        if c1 < c0 or r1 < r0:
            continue
        cols = np.arange(c0, c1 + 1)
        rows = np.arange(r0, r1 + 1)
        xx, yy = np.meshgrid(xmin + (cols + 0.5) * cell, ymax - (rows + 0.5) * cell)
        l0 = ((n1 - n2) * (xx - e2) + (e2 - e1) * (yy - n2)) / det
        l1 = ((n2 - n0) * (xx - e2) + (e0 - e2) * (yy - n2)) / det
        l2 = 1 - l0 - l1
        inside = (l0 >= -1e-9) & (l1 >= -1e-9) & (l2 >= -1e-9)
        vals = l0 * z0 + l1 * z1 + l2 * z2
        block = z[r0:r1 + 1, c0:c1 + 1]
        block[inside] = vals[inside]
    drv = gdal.GetDriverByName("GTiff")
    ds = drv.Create(path, w, h, 1, gdal.GDT_Float32, options=["COMPRESS=DEFLATE", "TILED=YES"])
    ds.SetGeoTransform((xmin, cell, 0, ymax, 0, -cell))
    if wkt:
        ds.SetProjection(wkt)
    band = ds.GetRasterBand(1)
    band.SetNoDataValue(NODATA)
    band.WriteArray(z)
    ds = None
    return w, h


class LandXmlImport(BiruniAlgorithm):
    ID = "landxmlimport"
    NAME = "Open LandXML (points, surfaces, parcels)"
    GROUP = "Survey & CAD"
    GROUP_ID = "survey"
    HELP = ("Reads a LandXML file from Civil 3D, Trimble Business Center, Carlson or a machine-control system: "
            "the points (CgPoints) with name, code and description, the surfaces as triangles - deleted "
            "(invisible) triangles are left out - and optionally as an elevation raster, and the parcels with "
            "their straight and curved sides. LandXML gives coordinates as northing, easting; they are placed "
            "as easting X, northing Y.")

    INPUT, CRS, CELL = "INPUT", "CRS", "CELL"
    POINTS, TRIANGLES, PARCELS, DEM = "POINTS", "TRIANGLES", "PARCELS", "DEM"

    def initAlgorithm(self, config=None):  # noqa: N802
        self.addParameter(QgsProcessingParameterFile(
            self.INPUT, tr("LandXML file"), fileFilter="LandXML (*.xml *.landxml);;" + tr("All files") + " (*.*)"))
        self.addParameter(QgsProcessingParameterCrs(self.CRS, tr("Coordinate system"), optional=True))
        self.addParameter(QgsProcessingParameterNumber(
            self.CELL, tr("Raster cell size for the surface (m, 0 = automatic)"),
            Qgis.ProcessingNumberParameterType.Double, 0.0, minValue=0.0))
        self.addParameter(QgsProcessingParameterFeatureSink(
            self.POINTS, tr("LandXML points"), Qgis.ProcessingSourceType.VectorPoint))
        self.addParameter(QgsProcessingParameterFeatureSink(
            self.TRIANGLES, tr("Surface triangles"), Qgis.ProcessingSourceType.VectorPolygon))
        self.addParameter(QgsProcessingParameterFeatureSink(
            self.PARCELS, tr("LandXML parcels"), Qgis.ProcessingSourceType.VectorPolygon))
        self.addParameter(QgsProcessingParameterRasterDestination(
            self.DEM, tr("Surface as elevation raster"), optional=True, createByDefault=False))

    def processAlgorithm(self, parameters, context, feedback):  # noqa: N802
        path = self.parameterAsFile(parameters, self.INPUT, context)
        if not path or not os.path.exists(path):
            raise QgsProcessingException(tr("Choose the LandXML file."))
        try:
            data = read_landxml(path)
        except ET.ParseError as e:
            raise QgsProcessingException(tr("The XML file could not be read: {e}").format(e=e))
        crs = self.parameterAsCrs(parameters, self.CRS, context)
        if not crs.isValid() and data["epsg"]:
            crs = QgsCoordinateReferenceSystem(f"EPSG:{data['epsg']}")
        if data["units"]:
            feedback.pushInfo(tr("Units: {u}").format(u=data["units"]))

        pf = QgsFields()
        for n, t in (("name", QMetaType.Type.QString), ("code", QMetaType.Type.QString),
                     ("description", QMetaType.Type.QString), ("easting", QMetaType.Type.Double),
                     ("northing", QMetaType.Type.Double), ("elevation", QMetaType.Type.Double)):
            pf.append(QgsField(n, t))
        psink, pid = self.parameterAsSink(parameters, self.POINTS, context, pf, Qgis.WkbType.PointZ, crs)
        for name, code, desc, n, e, z in data["points"]:
            f = QgsFeature(pf)
            f.setGeometry(QgsGeometry(QgsPoint(e, n, z if z is not None else 0.0)))
            f.setAttributes([name, code, desc, e, n, z])
            psink.addFeature(f, QgsFeatureSink.Flag.FastInsert)

        tf = QgsFields()
        tf.append(QgsField("surface", QMetaType.Type.QString))
        tf.append(QgsField("slope_pct", QMetaType.Type.Double))
        tsink, tid = self.parameterAsSink(parameters, self.TRIANGLES, context, tf, Qgis.WkbType.PolygonZ, crs)
        n_faces = hidden = 0
        for s in data["surfaces"]:
            hidden += s["hidden"]
            for a, b, c in s["faces"]:
                pa, pb, pc = (s["points"][k] for k in (a, b, c))
                ring = QgsLineString([QgsPoint(p[1], p[0], p[2] or 0.0) for p in (pa, pb, pc, pa)])
                poly = QgsPolygon()
                poly.setExteriorRing(ring)
                f = QgsFeature(tf)
                f.setGeometry(QgsGeometry(poly))
                f.setAttributes([s["name"], _slope(pa, pb, pc)])
                tsink.addFeature(f, QgsFeatureSink.Flag.FastInsert)
                n_faces += 1
        if hidden:
            feedback.pushInfo(tr("{n} deleted (invisible) triangles were left out.").format(n=hidden))

        af = QgsFields()
        for n, t in (("name", QMetaType.Type.QString), ("description", QMetaType.Type.QString),
                     ("area_file", QMetaType.Type.Double), ("area", QMetaType.Type.Double)):
            af.append(QgsField(n, t))
        asink, aid = self.parameterAsSink(parameters, self.PARCELS, context, af, Qgis.WkbType.Polygon, crs)
        for p in data["parcels"]:
            ring = [QgsPoint(e, n) for n, e in p["ring"]]
            if (ring[0].x(), ring[0].y()) != (ring[-1].x(), ring[-1].y()):
                ring.append(ring[0].clone())
            poly = QgsPolygon()
            poly.setExteriorRing(QgsLineString(ring))
            g = QgsGeometry(poly)
            f = QgsFeature(af)
            f.setGeometry(g)
            f.setAttributes([p["name"], p["desc"], p["area"], g.area()])
            asink.addFeature(f, QgsFeatureSink.Flag.FastInsert)

        results = {self.POINTS: pid, self.TRIANGLES: tid, self.PARCELS: aid, "POINT_COUNT": len(data["points"]),
                   "TRIANGLE_COUNT": n_faces, "PARCEL_COUNT": len(data["parcels"]), "HIDDEN_TRIANGLES": hidden}
        dem = self.parameterAsOutputLayer(parameters, self.DEM, context)
        if dem and data["surfaces"]:
            s = max(data["surfaces"], key=lambda x: len(x["faces"]))
            cell = self.parameterAsDouble(parameters, self.CELL, context)
            if cell <= 0:
                es = [p[1] for p in s["points"].values()]
                ns = [p[0] for p in s["points"].values()]
                area = max((max(es) - min(es)) * (max(ns) - min(ns)), 1.0)
                cell = max(0.1, round(math.sqrt(area / max(len(s["points"]), 1)) / 2, 2))
            try:
                tin_to_raster(s["points"], s["faces"], dem, cell, crs.toWkt() if crs.isValid() else "")
            except ValueError:
                raise QgsProcessingException(tr("The raster would be too large: use a bigger cell size."))
            results[self.DEM] = dem
        feedback.pushInfo(tr("{p} points, {t} triangles, {a} parcels.").format(
            p=len(data["points"]), t=n_faces, a=len(data["parcels"])))
        return results


def _slope(a, b, c):
    """Slope of a triangle in percent."""
    (n0, e0, z0), (n1, e1, z1), (n2, e2, z2) = a, b, c
    if None in (z0, z1, z2):
        return None
    ux, uy, uz = e1 - e0, n1 - n0, z1 - z0
    vx, vy, vz = e2 - e0, n2 - n0, z2 - z0
    nx, ny, nz = uy * vz - uz * vy, uz * vx - ux * vz, ux * vy - uy * vx
    if abs(nz) < 1e-12:
        return None
    return round(100 * math.hypot(nx, ny) / abs(nz), 3)


# ---- writing ------------------------------------------------------------------------

def _fmt(v):
    return f"{v:.4f}"


def triangulate(points):
    """Delaunay triangles [(i, j, k)] of [(x, y)], without slivers of zero area."""
    try:
        import numpy as np
        from scipy.spatial import Delaunay

        tri = Delaunay(np.array(points))
        faces = [tuple(int(v) for v in s) for s in tri.simplices]
    except ImportError:  # pragma: no cover - GEOS fallback
        from qgis.core import QgsMultiPoint

        mp = QgsMultiPoint()
        for x, y in points:
            mp.addGeometry(QgsPoint(x, y))
        index = {(round(x, 6), round(y, 6)): i for i, (x, y) in enumerate(points)}
        faces = []
        for part in QgsGeometry(mp).delaunayTriangulation().asGeometryCollection():
            ring = part.asPolygon()[0][:3]
            ids = [index.get((round(p.x(), 6), round(p.y(), 6))) for p in ring]
            if None not in ids:
                faces.append(tuple(ids))
    good = []
    for i, j, k in faces:
        (x0, y0), (x1, y1), (x2, y2) = points[i], points[j], points[k]
        if abs((x1 - x0) * (y2 - y0) - (x2 - x0) * (y1 - y0)) > 1e-9:
            good.append((i, j, k))
    return good


def raster_grid_tin(path, step=1, max_points=250_000):
    """Points and triangles of a raster: two triangles per (resampled) cell."""
    import numpy as np
    from osgeo import gdal

    ds = gdal.Open(path)
    band = ds.GetRasterBand(1)
    gt = ds.GetGeoTransform()
    w, h = ds.RasterXSize, ds.RasterYSize
    while (w // step) * (h // step) > max_points:
        step += 1
    a = band.ReadAsArray().astype("float64")
    nd = band.GetNoDataValue()
    rows = list(range(0, h, step))
    cols = list(range(0, w, step))
    ids = {}
    pts = []
    for r in rows:
        for c in cols:
            v = a[r, c]
            if (nd is not None and v == nd) or not np.isfinite(v):
                continue
            x = gt[0] + (c + 0.5) * gt[1] + (r + 0.5) * gt[2]
            y = gt[3] + (c + 0.5) * gt[4] + (r + 0.5) * gt[5]
            ids[(r, c)] = len(pts)
            pts.append((x, y, float(v)))
    faces = []
    for ri in range(len(rows) - 1):
        for ci in range(len(cols) - 1):
            p00, p01 = ids.get((rows[ri], cols[ci])), ids.get((rows[ri], cols[ci + 1]))
            p10, p11 = ids.get((rows[ri + 1], cols[ci])), ids.get((rows[ri + 1], cols[ci + 1]))
            if None not in (p00, p01, p11):
                faces.append((p00, p11, p01))
            if None not in (p00, p11, p10):
                faces.append((p00, p10, p11))
    return pts, faces, step


def write_landxml(path, points=(), parcels=(), surface=None, epsg="", units="meter", name="Biruni GIS"):
    """points: [(name, code, desc, e, n, z)]; parcels: [(name, desc, area, [(e, n)])];
    surface: (name, [(e, n, z)], [(i, j, k)]) with 0-based indices."""
    ET.register_namespace("", NS)
    now = datetime.datetime.now()
    root = ET.Element(f"{{{NS}}}LandXML", {"version": "1.2", "date": now.strftime("%Y-%m-%d"),
                                           "time": now.strftime("%H:%M:%S")})
    units_el = ET.SubElement(root, f"{{{NS}}}Units")
    ET.SubElement(units_el, f"{{{NS}}}Metric", {"linearUnit": units, "areaUnit": "squareMeter",
                                                "volumeUnit": "cubicMeter", "angularUnit": "decimal degrees",
                                                "directionUnit": "decimal degrees"})
    if epsg:
        ET.SubElement(root, f"{{{NS}}}CoordinateSystem", {"epsgCode": str(epsg)})
    ET.SubElement(root, f"{{{NS}}}Application", {"name": name, "manufacturer": "Biruni GIS", "version": "1"})
    if points:
        cg = ET.SubElement(root, f"{{{NS}}}CgPoints")
        used = set()
        for k, (pname, code, desc, e, n, z) in enumerate(points, 1):
            pname = str(pname or k)
            while pname in used:
                pname += "_"
            used.add(pname)
            attrs = {"name": pname}
            if code:
                attrs["code"] = str(code)
            if desc:
                attrs["desc"] = str(desc)
            el = ET.SubElement(cg, f"{{{NS}}}CgPoint", attrs)
            el.text = f"{_fmt(n)} {_fmt(e)}" + (f" {_fmt(z)}" if z is not None else "")
    if surface is not None:
        sname, spts, faces = surface
        surfs = ET.SubElement(root, f"{{{NS}}}Surfaces")
        s = ET.SubElement(surfs, f"{{{NS}}}Surface", {"name": sname})
        d = ET.SubElement(s, f"{{{NS}}}Definition", {"surfType": "TIN"})
        pnts = ET.SubElement(d, f"{{{NS}}}Pnts")
        for i, (e, n, z) in enumerate(spts, 1):
            p = ET.SubElement(pnts, f"{{{NS}}}P", {"id": str(i)})
            p.text = f"{_fmt(n)} {_fmt(e)} {_fmt(z)}"
        fs = ET.SubElement(d, f"{{{NS}}}Faces")
        for i, j, k in faces:
            f = ET.SubElement(fs, f"{{{NS}}}F")
            f.text = f"{i + 1} {j + 1} {k + 1}"
    if parcels:
        pe = ET.SubElement(root, f"{{{NS}}}Parcels")
        for pname, desc, area, ring in parcels:
            attrs = {"name": str(pname)}
            if area is not None:
                attrs["area"] = f"{area:.3f}"
            if desc:
                attrs["desc"] = str(desc)
            p = ET.SubElement(pe, f"{{{NS}}}Parcel", attrs)
            cgeom = ET.SubElement(p, f"{{{NS}}}CoordGeom")
            pts = list(ring)
            if pts and pts[0] != pts[-1]:
                pts.append(pts[0])
            for (e1, n1), (e2, n2) in zip(pts, pts[1:]):
                line = ET.SubElement(cgeom, f"{{{NS}}}Line")
                ET.SubElement(line, f"{{{NS}}}Start").text = f"{_fmt(n1)} {_fmt(e1)}"
                ET.SubElement(line, f"{{{NS}}}End").text = f"{_fmt(n2)} {_fmt(e2)}"
    tree = ET.ElementTree(root)
    ET.indent(tree, space=" ")
    tree.write(path, encoding="utf-8", xml_declaration=True)


def check_landxml(path):
    """Structural checks of a written file: (ok, [problems])."""
    problems = []
    data = read_landxml(path)
    for s in data["surfaces"]:
        for face in s["faces"]:
            if len(set(face)) < 3:
                problems.append(f"degenerate face {face}")
        if s["hidden"]:
            problems.append(f"{s['hidden']} hidden faces")
    return not problems, problems


class LandXmlExport(BiruniAlgorithm):
    ID = "landxmlexport"
    NAME = "Save as LandXML (points, surface, parcels)"
    GROUP = "Survey & CAD"
    GROUP_ID = "survey"
    HELP = ("Writes a LandXML 1.2 file for Civil 3D, Trimble Business Center, Carlson or machine control: points "
            "(with name and code fields), parcels from polygons (with their area) and a surface (TIN) made from "
            "an elevation raster or triangulated from survey points with heights. Only valid triangles are "
            "written - no deleted or flat-sliver faces - and the file is read back and checked before it is "
            "kept.")

    POINTS, NAME_FIELD, CODE_FIELD, PARCELS, PARCEL_NAME = "POINTS", "NAME_FIELD", "CODE_FIELD", "PARCELS", \
        "PARCEL_NAME"
    SURFACE_RASTER, SURFACE_POINTS, Z_FIELD, SURFACE_NAME, OUTPUT = "SURFACE_RASTER", "SURFACE_POINTS", "Z_FIELD", \
        "SURFACE_NAME", "OUTPUT"
    CRS = "CRS"

    def initAlgorithm(self, config=None):  # noqa: N802
        self.addParameter(QgsProcessingParameterFeatureSource(
            self.POINTS, tr("Points"), [Qgis.ProcessingSourceType.VectorPoint], optional=True))
        self.addParameter(QgsProcessingParameterField(
            self.NAME_FIELD, tr("Point name field"), parentLayerParameterName=self.POINTS, optional=True))
        self.addParameter(QgsProcessingParameterField(
            self.CODE_FIELD, tr("Point code field"), parentLayerParameterName=self.POINTS, optional=True))
        self.addParameter(QgsProcessingParameterFeatureSource(
            self.PARCELS, tr("Parcels (polygons)"), [Qgis.ProcessingSourceType.VectorPolygon], optional=True))
        self.addParameter(QgsProcessingParameterField(
            self.PARCEL_NAME, tr("Parcel name field"), parentLayerParameterName=self.PARCELS, optional=True))
        self.addParameter(QgsProcessingParameterRasterLayer(
            self.SURFACE_RASTER, tr("Surface from an elevation raster"), optional=True))
        self.addParameter(QgsProcessingParameterFeatureSource(
            self.SURFACE_POINTS, tr("Or surface from points with heights"), [Qgis.ProcessingSourceType.VectorPoint],
            optional=True))
        self.addParameter(QgsProcessingParameterField(
            self.Z_FIELD, tr("Height field (empty = the points' Z)"), parentLayerParameterName=self.SURFACE_POINTS,
            type=Qgis.ProcessingFieldParameterDataType.Numeric, optional=True))
        self.addParameter(QgsProcessingParameterString(self.SURFACE_NAME, tr("Surface name"), "Surface"))
        self.addParameter(QgsProcessingParameterCrs(self.CRS, tr("Coordinate system of the file"), optional=True))
        self.addParameter(QgsProcessingParameterFileDestination(self.OUTPUT, tr("LandXML file"),
                                                                "LandXML (*.xml)"))

    def processAlgorithm(self, parameters, context, feedback):  # noqa: N802
        crs = self.parameterAsCrs(parameters, self.CRS, context)
        sources = [self.parameterAsSource(parameters, k, context) if parameters.get(k) else None
                   for k in (self.POINTS, self.PARCELS, self.SURFACE_POINTS)]
        raster = self.parameterAsRasterLayer(parameters, self.SURFACE_RASTER, context) \
            if parameters.get(self.SURFACE_RASTER) else None
        if not crs.isValid():
            for s in sources:
                if s is not None and s.sourceCrs().isValid():
                    crs = s.sourceCrs()
                    break
            else:
                crs = raster.crs() if raster is not None else crs
        if crs.isValid() and crs.isGeographic():
            raise QgsProcessingException(tr("LandXML needs grid coordinates: choose a projected coordinate system "
                                            "(metres)."))
        tctx = context.transformContext()

        def xform(src_crs):
            return QgsCoordinateTransform(src_crs, crs, tctx) if crs.isValid() and src_crs.isValid() \
                and src_crs != crs else None

        points = []
        src = sources[0]
        if src is not None:
            name_f = self.parameterAsString(parameters, self.NAME_FIELD, context)
            code_f = self.parameterAsString(parameters, self.CODE_FIELD, context)
            xf = xform(src.sourceCrs())
            for f in src.getFeatures():
                g = f.geometry()
                if g.isNull() or g.isEmpty():
                    continue
                if xf:
                    g = QgsGeometry(g)
                    g.transform(xf)
                v = next(g.vertices())
                z = v.z() if v.is3D() and math.isfinite(v.z()) else None
                points.append((f[name_f] if name_f else f.id(), f[code_f] if code_f else "", "", v.x(), v.y(), z))

        parcels = []
        src = sources[1]
        if src is not None:
            pname_f = self.parameterAsString(parameters, self.PARCEL_NAME, context)
            xf = xform(src.sourceCrs())
            for f in src.getFeatures():
                g = QgsGeometry(f.geometry())
                if g.isNull() or g.isEmpty():
                    continue
                if xf:
                    g.transform(xf)
                for part in (g.asMultiPolygon() if g.isMultipart() else [g.asPolygon()]):
                    if part:
                        ring = [(p.x(), p.y()) for p in part[0]]
                        parcels.append((f[pname_f] if pname_f else f.id(), "",
                                        abs(QgsGeometry.fromPolygonXY([part[0]]).area()), ring))

        surface = None
        sname = self.parameterAsString(parameters, self.SURFACE_NAME, context) or "Surface"
        if raster is not None:
            pts, faces, step = raster_grid_tin(raster.source())
            if step > 1:
                feedback.pushInfo(tr("The raster was thinned to every {n}th cell for the surface.").format(n=step))
            surface = (sname, pts, faces)
        elif sources[2] is not None:
            src = sources[2]
            zf = self.parameterAsString(parameters, self.Z_FIELD, context)
            xf = xform(src.sourceCrs())
            seen = {}
            for f in src.getFeatures():
                g = f.geometry()
                if g.isNull() or g.isEmpty():
                    continue
                if xf:
                    g = QgsGeometry(g)
                    g.transform(xf)
                v = next(g.vertices())
                z = f[zf] if zf else (v.z() if v.is3D() else None)
                if z is None or not math.isfinite(float(z)):
                    continue
                seen[(round(v.x(), 4), round(v.y(), 4))] = (v.x(), v.y(), float(z))  # duplicates: last wins
            pts = list(seen.values())
            if len(pts) < 3:
                raise QgsProcessingException(tr("At least three points with heights are needed for a surface."))
            surface = (sname, pts, triangulate([(x, y) for x, y, _ in pts]))
        if not points and not parcels and surface is None:
            raise QgsProcessingException(tr("Give points, parcels or a surface to write."))

        out = self.parameterAsFileOutput(parameters, self.OUTPUT, context)
        epsg = crs.authid().split(":")[1] if crs.isValid() and crs.authid().startswith("EPSG:") else ""
        write_landxml(out, points, parcels, surface, epsg)
        ok, problems = check_landxml(out)
        if not ok:
            raise QgsProcessingException(tr("The written file failed its check: {p}").format(p="; ".join(problems)))
        n_faces = len(surface[2]) if surface else 0
        feedback.pushInfo(tr("LandXML written: {p} points, {a} parcels, {t} triangles.").format(
            p=len(points), a=len(parcels), t=n_faces))
        return {self.OUTPUT: out, "POINT_COUNT": len(points), "PARCEL_COUNT": len(parcels), "TRIANGLE_COUNT": n_faces}
