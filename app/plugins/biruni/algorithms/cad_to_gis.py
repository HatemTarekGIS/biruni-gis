import math
import os
import re

from qgis.core import (Qgis, QgsAbstractGeometry, QgsCircularString, QgsCompoundCurve, QgsCurvePolygon, QgsFeature,
                       QgsField, QgsFields, QgsGeometry, QgsLineString, QgsPoint, QgsPointXY,
                       QgsProcessingContext, QgsProcessingException, QgsProcessingParameterBoolean,
                       QgsProcessingParameterCrs, QgsProcessingParameterEnum, QgsProcessingParameterFile,
                       QgsProcessingParameterFileDestination, QgsProcessingParameterNumber, QgsSpatialIndex,
                       QgsVectorFileWriter, QgsVectorLayer, QgsWkbTypes)
from qgis.PyQt.QtCore import QMetaType

from ..i18n import tr
from ..survey import dxf as dxfmod
from .base import BiruniAlgorithm

SPLIT = ["One layer per geometry type", "One layer per CAD layer"]
KINDS = {  # kind: (title, geometry)
    "polygons": ("Polygons", "polygon"),
    "lines": ("Lines", "line"),
    "points": ("Points", "point"),
    "blocks": ("Blocks", "point"),
    "texts": ("Texts", "point"),
}
ORDER = ["polygons", "lines", "points", "blocks", "texts"]
BASE_FIELDS = {
    "polygons": [("cad_layer", "s"), ("handle", "s"), ("color", "i"), ("label", "s"), ("texts", "i"),
                 ("area", "d"), ("perimeter", "d"), ("source", "s")],
    "lines": [("cad_layer", "s"), ("handle", "s"), ("color", "i"), ("entity", "s"), ("length", "d")],
    "points": [("cad_layer", "s"), ("handle", "s"), ("color", "i")],
    "blocks": [("cad_layer", "s"), ("handle", "s"), ("color", "i"), ("block", "s"), ("rotation", "d"),
               ("scale_x", "d"), ("scale_y", "d")],
    "texts": [("cad_layer", "s"), ("handle", "s"), ("color", "i"), ("text", "s"), ("style", "s"),
              ("height", "d"), ("rotation", "d"), ("entity", "s"), ("in_polygon", "b")],
}
_TYPES = {"s": QMetaType.Type.QString, "d": QMetaType.Type.Double, "i": QMetaType.Type.Int,
          "b": QMetaType.Type.Bool}


def _arc_points(cx, cy, r, a1, a2):
    """Start, middle and end of a counter-clockwise arc from angle a1 to a2 (degrees)."""
    if a2 <= a1:
        a2 += 360
    am = (a1 + a2) / 2
    return [(cx + r * math.cos(math.radians(a)), cy + r * math.sin(math.radians(a))) for a in (a1, am, a2)]


def poly_curve(verts, closed):
    """QgsCompoundCurve through DXF vertices (x, y, bulge); arcs are true arcs."""
    curve = QgsCompoundCurve()
    run = []
    segs = list(zip(verts, verts[1:]))
    if closed:
        segs.append((verts[-1], verts[0]))

    def flush():
        if len(run) >= 2:
            curve.addCurve(QgsLineString([QgsPoint(x, y) for x, y in run]))

    run.append((verts[0][0], verts[0][1]))
    for a, b in segs:
        if abs(a[2]) > 1e-12:
            flush()
            m = dxfmod.bulge_midpoint(a, b, a[2])
            curve.addCurve(QgsCircularString(QgsPoint(a[0], a[1]), QgsPoint(*m), QgsPoint(b[0], b[1])))
            run[:] = [(b[0], b[1])]
        else:
            run.append((b[0], b[1]))
    flush()
    return curve


def _polygon(curve):
    cp = QgsCurvePolygon()
    cp.setExteriorRing(curve)
    return QgsGeometry(cp)


def entity_geometry(e, close_tol):
    """(kind, geometry, source) for one parsed entity, or None."""
    if e.kind == "poly":
        verts, closed = dxfmod.normalize_polyline(e.verts, e.closed, close_tol)
        if len(verts) < 2:
            return None
        if closed and len(verts) >= 2:
            g = _polygon(poly_curve(verts, True))
            if g.area() > 1e-12:
                return "polygons", g, "closed polyline"
            return "lines", QgsGeometry(poly_curve(verts, True)), None   # collapsed ring: never a polygon
        return "lines", QgsGeometry(poly_curve(verts, False)), None
    if e.kind == "line":
        (x1, y1, _), (x2, y2, _) = e.verts
        if x1 == x2 and y1 == y2:
            return None
        return "lines", QgsGeometry(QgsLineString([QgsPoint(x1, y1), QgsPoint(x2, y2)])), None
    if e.kind == "arc":
        cx, cy = e.center
        pts = _arc_points(cx, cy, e.radius, *e.angles)
        if e.mirrored:
            pts = [(-x, y) for x, y in pts]
        return "lines", QgsGeometry(QgsCircularString(*(QgsPoint(x, y) for x, y in pts))), None
    if e.kind == "circle":
        cx, cy = e.center
        if e.mirrored:
            cx = -cx
        r = e.radius
        ring = QgsCircularString()
        ring.setPoints([QgsPoint(cx + r, cy), QgsPoint(cx, cy + r), QgsPoint(cx - r, cy),
                        QgsPoint(cx, cy - r), QgsPoint(cx + r, cy)])
        cc = QgsCompoundCurve()
        cc.addCurve(ring)
        return "polygons", _polygon(cc), "circle"
    if e.kind == "point":
        return "points", QgsGeometry(QgsPoint(e.x, e.y)), None
    if e.kind == "text":
        return "texts", QgsGeometry(QgsPoint(e.x, e.y)), None
    if e.kind == "insert":
        return "blocks", QgsGeometry(QgsPoint(e.x, e.y)), None
    return None


def safe_table(name):
    s = re.sub(r"[^\w]+", "_", name, flags=re.UNICODE).strip("_")
    return s or "layer"


class CadToGis(BiruniAlgorithm):
    ID = "cadtogis"
    NAME = "CAD to GIS"
    GROUP = "Survey & CAD"
    GROUP_ID = "survey"
    HELP = ("Converts an AutoCAD drawing (DXF) into GIS layers in one GeoPackage: closed polylines and circles "
            "become polygons (also polylines that return to their start without the 'closed' flag), texts inside "
            "a polygon become its 'label', block inserts become points with the block name and its attributes "
            "as fields, and arcs stay true arcs. Lines drawn separately can also be joined into polygons. "
            "Choose one layer per geometry type, or one per CAD layer. Arabic texts, including Arabic keyboard "
            "SHX fonts (xarb), are decoded. DWG: save it as DXF first, or use DWG / DXF Import.")

    INPUT, CRS, SPLIT, LABELS, BUILD, CURVES, CLOSE_TOL, ENCODING, OUTPUT = (
        "INPUT", "CRS", "SPLIT", "LABELS", "BUILD_POLYGONS", "KEEP_CURVES", "CLOSE_TOLERANCE", "ENCODING", "OUTPUT")

    def initAlgorithm(self, config=None):  # noqa: N802
        self.addParameter(QgsProcessingParameterFile(
            self.INPUT, tr("CAD drawing"), fileFilter="DXF (*.dxf *.DXF);;All files (*.*)"))
        self.addParameter(QgsProcessingParameterCrs(self.CRS, tr("Coordinate system of the drawing"),
                                                    "ProjectCrs"))
        self.addParameter(QgsProcessingParameterEnum(self.SPLIT, tr("Layers"), [tr(s) for s in SPLIT],
                                                     defaultValue=0))
        self.addParameter(QgsProcessingParameterBoolean(
            self.LABELS, tr("Put the texts inside each polygon into its 'label' field"), defaultValue=True))
        self.addParameter(QgsProcessingParameterBoolean(
            self.BUILD, tr("Also build polygons from separate lines that enclose areas"), defaultValue=False))
        self.addParameter(QgsProcessingParameterBoolean(
            self.CURVES, tr("Keep arcs as true curves"), defaultValue=True))
        self.addParameter(QgsProcessingParameterNumber(
            self.CLOSE_TOL, tr("Treat a polyline as closed when its ends are this close"),
            Qgis.ProcessingNumberParameterType.Double, 0.001, minValue=0.0))
        self.addParameter(QgsProcessingParameterEnum(
            self.ENCODING, tr("Text encoding"), [tr(e[1]) for e in dxfmod.ENCODINGS], defaultValue=0))
        self.addParameter(QgsProcessingParameterFileDestination(
            self.OUTPUT, tr("GeoPackage"), "GeoPackage (*.gpkg)"))

    # ---- reading ---------------------------------------------------------
    def _read(self, path, encoding, feedback):
        try:
            drawing = dxfmod.read(path, encoding)
        except ValueError:
            raise QgsProcessingException(tr("Binary DXF and DWG files are not supported: save the drawing as an "
                                            "ASCII DXF, or use DWG / DXF Import."))
        except OSError as e:
            raise QgsProcessingException(tr("Could not read {p}").format(p=f"{path}: {e}"))
        return drawing

    def _gdal_others(self, path, handles, feedback):
        """Geometries of the entity types read by GDAL (splines, ellipses, hatches…)."""
        from osgeo import gdal, ogr

        out = []
        if not handles:
            return out
        gdal.UseExceptions()
        old = gdal.GetConfigOption("DXF_INLINE_BLOCKS")
        gdal.SetConfigOption("DXF_INLINE_BLOCKS", "FALSE")
        try:
            ds = gdal.OpenEx(path, gdal.OF_VECTOR)
            lyr = ds.GetLayerByName("entities") or ds.GetLayer(0)
            for f in lyr:
                h = (f.GetField("EntityHandle") or "").upper()
                if h not in handles:
                    continue
                g = f.GetGeometryRef()
                if g is None:
                    continue
                qg = QgsGeometry()
                qg.fromWkb(bytes(g.ExportToIsoWkb()))
                if qg.isNull():
                    continue
                qg.get().dropZValue()
                sub = f.GetField("SubClasses") or ""
                out.append((f.GetField("Layer") or "0", h, qg, sub))
        except RuntimeError as e:
            feedback.pushWarning(str(e))
        finally:
            gdal.SetConfigOption("DXF_INLINE_BLOCKS", old)
        return out

    # ---- main ------------------------------------------------------------
    def processAlgorithm(self, parameters, context, feedback):  # noqa: N802
        path = self.parameterAsFile(parameters, self.INPUT, context)
        crs = self.parameterAsCrs(parameters, self.CRS, context)
        per_layer = self.parameterAsEnum(parameters, self.SPLIT, context) == 1
        labels = self.parameterAsBool(parameters, self.LABELS, context)
        build = self.parameterAsBool(parameters, self.BUILD, context)
        curves = self.parameterAsBool(parameters, self.CURVES, context)
        close_tol = self.parameterAsDouble(parameters, self.CLOSE_TOL, context)
        encoding = dxfmod.ENCODINGS[self.parameterAsEnum(parameters, self.ENCODING, context)][0]
        out_path = self.parameterAsFileOutput(parameters, self.OUTPUT, context)
        if not path or not os.path.exists(path):
            raise QgsProcessingException(tr("Could not read {p}").format(p=path))
        if path.lower().endswith(".dwg"):
            raise QgsProcessingException(tr("Binary DXF and DWG files are not supported: save the drawing as an "
                                            "ASCII DXF, or use DWG / DXF Import."))

        drawing = self._read(path, encoding, feedback)
        feedback.pushInfo(tr("Drawing {v}, text encoding {e}, {n} entities.").format(
            v=drawing.version or "R12", e=drawing.encoding, n=len(drawing.entities)))
        if drawing.skipped_paper:
            feedback.pushInfo(tr("{n} paper-space entities (layout sheets) were skipped.").format(
                n=drawing.skipped_paper))

        # records: kind -> list of dicts(geometry, cad_layer, values)
        recs = {k: [] for k in KINDS}
        attr_tags = {}      # cad layer (or "") -> ordered tags
        for e in drawing.entities:
            r = entity_geometry(e, close_tol)
            if r is None:
                continue
            kind, g, source = r
            vals = {"cad_layer": e.layer, "handle": e.handle, "color": e.color}
            if kind == "polygons":
                vals["source"] = source
            elif kind == "lines":
                vals["entity"] = e.dxf_type
            elif kind == "texts":
                vals.update(text=e.text, style=e.style, height=e.height, rotation=e.rotation, entity=e.dxf_type,
                            in_polygon=False)
            elif kind == "blocks":
                vals.update(block=e.block, rotation=e.rotation, scale_x=e.scale[0], scale_y=e.scale[1])
                key = e.layer if per_layer else ""
                tags = attr_tags.setdefault(key, [])
                for t in e.attribs:
                    if t not in tags:
                        tags.append(t)
                vals["_attribs"] = e.attribs
            recs[kind].append({"g": g, "v": vals})

        for layer, handle, g, sub in self._gdal_others(path, drawing.other_handles, feedback):
            vals = {"cad_layer": layer, "handle": handle, "color": None}
            gt = QgsWkbTypes.geometryType(g.wkbType())
            name = sub.split(":")[-1].replace("AcDb", "").upper() or "OTHER"
            if gt == Qgis.GeometryType.Polygon:
                vals["source"] = name.lower()
                recs["polygons"].append({"g": g, "v": vals})
            elif gt == Qgis.GeometryType.Line:
                vals["entity"] = name
                recs["lines"].append({"g": g, "v": vals})

        if build:
            self._build_polygons(recs, per_layer, feedback)

        if labels and recs["texts"] and recs["polygons"]:
            self._assign_labels(recs)

        for r in recs["polygons"]:
            r["v"]["area"] = r["g"].area()
            r["v"]["perimeter"] = r["g"].length()
        for r in recs["lines"]:
            r["v"]["length"] = r["g"].length()
        if not curves:
            for kind in ("polygons", "lines"):
                for r in recs[kind]:
                    if r["g"].constGet().hasCurvedSegments():
                        r["g"] = QgsGeometry(r["g"].constGet().segmentize(
                            0.0001, QgsAbstractGeometry.SegmentationToleranceType.MaximumDifference))

        tables = self._write(recs, attr_tags, per_layer, crs, out_path, context, feedback)
        if not tables:
            raise QgsProcessingException(tr("Nothing to convert was found in the drawing."))
        group = os.path.splitext(os.path.basename(path))[0]
        for table, display, count in tables:
            lyr = QgsVectorLayer(f"{out_path}|layername={table}", display, "ogr")
            if not lyr.isValid():
                continue
            context.temporaryLayerStore().addMapLayer(lyr)
            details = QgsProcessingContext.LayerDetails(display, context.project(), self.OUTPUT)
            try:
                details.groupName = group
                details.layerSortKey = len(tables) - [t[0] for t in tables].index(table)
            except AttributeError:
                pass
            context.addLayerToLoadOnCompletion(lyr.id(), details)
        summary = ", ".join(f"{display}: {count}" for _t, display, count in tables)
        feedback.pushInfo(summary)
        counts = {k: len(v) for k, v in recs.items()}
        return {self.OUTPUT: out_path, "LAYERS": [t[0] for t in tables], **{k.upper(): n for k, n in counts.items()}}

    def _build_polygons(self, recs, per_layer, feedback):
        groups = {}
        for r in recs["lines"]:
            groups.setdefault(r["v"]["cad_layer"], []).append(r["g"])
        made = 0
        for layer, geoms in groups.items():
            lines = [QgsGeometry(g.constGet().segmentize()) if g.constGet().hasCurvedSegments() else g
                     for g in geoms]
            noded = QgsGeometry.unaryUnion(lines)
            if noded.isNull():
                continue
            poly = QgsGeometry.polygonize([noded])
            existing = [r["g"] for r in recs["polygons"] if r["v"]["cad_layer"] == layer]
            for part in poly.asGeometryCollection():
                if part.area() <= 1e-9:
                    continue
                if any(abs(part.area() - e.area()) < 1e-6 and part.equals(e) for e in existing):
                    continue
                recs["polygons"].append({"g": part, "v": {"cad_layer": layer, "handle": "", "color": None,
                                                          "source": "built from lines"}})
                made += 1
        feedback.pushInfo(tr("{n} polygons built from lines.").format(n=made))

    def _assign_labels(self, recs):
        polys = recs["polygons"]
        index = QgsSpatialIndex()
        engines = {}
        for i, r in enumerate(polys):
            f = QgsFeature(i)
            f.setGeometry(r["g"])
            index.addFeature(f)
            r["_texts"] = []
        for t in recs["texts"]:
            p = t["g"].asPoint() if not t["g"].isNull() else None
            if p is None:
                continue
            best = None
            for i in index.intersects(t["g"].boundingBox()):
                g = polys[i]["g"]
                if i not in engines:
                    eg = QgsGeometry.createGeometryEngine(g.constGet())
                    eg.prepareGeometry()
                    engines[i] = eg
                if engines[i].contains(t["g"].constGet()):
                    a = g.area()
                    if a > 0 and (best is None or a < polys[best]["g"].area()):
                        best = i
            if best is not None:
                polys[best]["_texts"].append(t["v"]["text"])
                t["v"]["in_polygon"] = True
        for r in polys:
            texts = r.pop("_texts", [])
            r["v"]["texts"] = len(texts)
            r["v"]["label"] = " | ".join(texts) if texts else None

    def _write(self, recs, attr_tags, per_layer, crs, out_path, context, feedback):
        if os.path.exists(out_path):
            try:
                os.remove(out_path)
            except OSError:
                raise QgsProcessingException(tr("Cannot overwrite {p} - is it open?").format(p=out_path))
        buckets = []    # (table, display, kind, records, attribute tags)
        for kind in ORDER:
            if not recs[kind]:
                continue
            if per_layer:
                by_layer = {}
                for r in recs[kind]:
                    by_layer.setdefault(r["v"]["cad_layer"], []).append(r)
                for layer in sorted(by_layer):
                    buckets.append((f"{safe_table(layer)}_{kind}", f"{layer} - {tr(KINDS[kind][0])}", kind,
                                    by_layer[layer], attr_tags.get(layer, [])))
            else:
                buckets.append((kind, tr(KINDS[kind][0]), kind, recs[kind], attr_tags.get("", [])))
        used = set()
        tables = []
        first = True
        for table, display, kind, rows, tags in buckets:
            base, k = table, 2
            while table.lower() in used:
                table = f"{base}_{k}"
                k += 1
            used.add(table.lower())
            fields = QgsFields()
            for name, t in BASE_FIELDS[kind]:
                fields.append(QgsField(name, _TYPES[t]))
            tag_fields = {}
            if kind == "blocks":
                taken = {n.lower() for n, _ in BASE_FIELDS[kind]} | {"fid"}
                for tag in tags:
                    fname = re.sub(r"[^\w]+", "_", tag, flags=re.UNICODE).strip("_") or "attr"
                    if fname.lower() in taken:
                        fname = "attr_" + fname
                    while fname.lower() in taken:
                        fname += "_"
                    taken.add(fname.lower())
                    tag_fields[tag] = fname
                    fields.append(QgsField(fname, QMetaType.Type.QString))
            geom_kind = KINDS[kind][1]
            curved = any(r["g"].constGet().hasCurvedSegments() for r in rows)
            if geom_kind == "polygon":
                multi = any(r["g"].isMultipart() for r in rows)
                wkb = (Qgis.WkbType.MultiSurface if multi else Qgis.WkbType.CurvePolygon) if curved else \
                    (Qgis.WkbType.MultiPolygon if multi else Qgis.WkbType.Polygon)
            elif geom_kind == "line":
                multi = any(r["g"].isMultipart() for r in rows)
                wkb = (Qgis.WkbType.MultiCurve if multi else Qgis.WkbType.CompoundCurve) if curved else \
                    (Qgis.WkbType.MultiLineString if multi else Qgis.WkbType.LineString)
            else:
                wkb = Qgis.WkbType.Point
            opts = QgsVectorFileWriter.SaveVectorOptions()
            opts.driverName = "GPKG"
            opts.layerName = table
            opts.fileEncoding = "UTF-8"
            opts.actionOnExistingFile = (QgsVectorFileWriter.ActionOnExistingFile.CreateOrOverwriteFile if first
                                         else QgsVectorFileWriter.ActionOnExistingFile.CreateOrOverwriteLayer)
            writer = QgsVectorFileWriter.create(out_path, fields, wkb, crs, context.transformContext(), opts)
            if writer is None or writer.hasError():
                raise QgsProcessingException(writer.errorMessage() if writer else out_path)
            first = False
            for r in rows:
                f = QgsFeature(fields)
                g = r["g"]
                if g.wkbType() != wkb:
                    coerced = g.coerceToType(wkb)
                    g = coerced[0] if coerced else g
                f.setGeometry(g)
                for name, _t in BASE_FIELDS[kind]:
                    f.setAttribute(name, r["v"].get(name))
                for tag, fname in tag_fields.items():
                    f.setAttribute(fname, r["v"].get("_attribs", {}).get(tag))
                writer.addFeature(f)
            del writer
            tables.append((table, display, len(rows)))
        return tables
