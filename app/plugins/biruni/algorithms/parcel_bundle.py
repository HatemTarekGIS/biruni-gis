"""The files a survey office hands over for a parcel (a deed update at the land registry or the municipality, and similar):
the closed parcel polygon and its corner points as Shapefiles (UTF-8 with .cpg and .prj), a DXF
sketch (كروكي) with lengths, corner numbers, coordinates and a title block, a KML for Google Earth,
the boundaries text, and an A4 survey report in PDF - one folder per parcel and one ZIP.

Every parcel is checked first (one closed part, valid, no self-intersection, projected system,
area against the deed area, text lengths for the Shapefile); a parcel with a blocking problem is
skipped and the reason written to checks.txt and the log."""
import math
import os
import re
import zipfile
from xml.sax.saxutils import escape

from qgis.core import (Qgis, QgsCoordinateReferenceSystem, QgsCoordinateTransform, QgsFeature, QgsField,
                       QgsFields, QgsGeometry, QgsPointXY, QgsProcessingException, QgsProcessingParameterBoolean,
                       QgsProcessingParameterCrs, QgsProcessingParameterFeatureSource, QgsProcessingParameterField,
                       QgsProcessingParameterFolderDestination, QgsProcessingParameterNumber,
                       QgsProcessingParameterString, QgsVectorFileWriter, QgsVectorLayer)
from qgis.PyQt.QtCore import QDate, QMetaType

from ..i18n import tr
from ..survey.report_pdf import ReportPage, write_pdf
from .base import BiruniAlgorithm
from .parcel_description import (DEFAULTS, DIRS_ORDER, NUMBER_FIELDS, STYLES, _clean_number, add_neighbour_parameters,
                                 area_words, build_finder, centre_wgs, describe_geometry, fmt_length, guess_field,
                                 outer_ring, whole_layer, write_text)

OWNER_FIELDS = ("owner", "owner_name", "ownername", "المالك", "اسم_المالك", "اسم المالك", "مالك")
DEED_FIELDS = ("deed_no", "deedno", "deed", "deed_number", "رقم_الصك", "رقم الصك", "الصك", "صك")
CITY_FIELDS = ("city", "city_name", "المدينة", "مدينة", "المحافظة")
DISTRICT_FIELDS = ("district", "district_name", "neighborhood", "neighbourhood", "hay", "الحي", "حي")
PLAN_FIELDS = ("plan_no", "planno", "plan", "scheme", "رقم_المخطط", "رقم المخطط", "المخطط", "مخطط")
DEED_AREA_FIELDS = ("deed_area", "deedarea", "area_deed", "مساحة_الصك", "مساحة الصك")
KSA = (34.4, 15.9, 55.8, 32.3)                  # lon/lat box of Saudi Arabia
DBF_TEXT = 254


def target_crs(chosen, source_crs, lon, lat):
    """The chosen system; otherwise KSA-GRF17 / UTM in Saudi Arabia, the layer's own projected system, or the
    WGS 84 UTM zone."""
    if chosen is not None and chosen.isValid():
        return chosen
    zone = int((lon + 180) // 6) + 1
    if KSA[0] <= lon <= KSA[2] and KSA[1] <= lat <= KSA[3] and 36 <= zone <= 40:
        return QgsCoordinateReferenceSystem(f"EPSG:{9356 + zone - 36}")
    if source_crs.isValid() and not source_crs.isGeographic():
        return source_crs
    return QgsCoordinateReferenceSystem(f"EPSG:{(32600 if lat >= 0 else 32700) + zone}")


def safe_name(s, fallback):
    s = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", str(s or "")).strip(" .")
    return s[:80] or fallback


def utf8_cut(s, limit=DBF_TEXT):
    b = str(s).encode("utf-8")
    if len(b) <= limit:
        return str(s), False
    return b[:limit].decode("utf-8", "ignore"), True


def preflight(geom, work_crs, deed_area=None, area_tol=1.0, texts=None):
    """(errors, warnings) for one parcel already in the working system."""
    errors, warnings = [], []
    if geom is None or geom.isNull() or geom.isEmpty():
        return [tr("The parcel has no geometry.")], warnings
    if work_crs.isGeographic():
        errors.append(tr("The coordinate system is in degrees: choose a projected system in metres."))
    parts = geom.asMultiPolygon() if geom.isMultipart() else [geom.asPolygon()]
    parts = [p for p in parts if p and p[0]]
    if len(parts) != 1:
        errors.append(tr("The parcel is made of {n} separate parts: a deed parcel must be one closed shape.")
                      .format(n=len(parts)))
    if parts:
        ring = parts[0][0]
        if len(parts[0]) > 1:
            warnings.append(tr("The parcel has {n} holes inside it.").format(n=len(parts[0]) - 1))
        if ring[0] != ring[-1]:
            errors.append(tr("The boundary is not closed."))
        if len({(round(p.x(), 3), round(p.y(), 3)) for p in ring[:-1]}) < 3:
            errors.append(tr("The boundary has fewer than three corners."))
        dups = sum(1 for a, b in zip(ring, ring[1:]) if math.hypot(a.x() - b.x(), a.y() - b.y()) < 0.001)
        if dups:
            warnings.append(tr("{n} repeated corners were removed.").format(n=dups))
    if not geom.isGeosValid():
        reasons = "; ".join(e.what() for e in geom.validateGeometry()[:3]) or tr("invalid geometry")
        errors.append(tr("The boundary crosses itself or is invalid ({why}).").format(why=reasons))
    area = abs(geom.area())
    if area < 1.0:
        errors.append(tr("The area is less than 1 m²."))
    if deed_area:
        diff = area - deed_area
        if abs(diff) > area_tol / 100 * deed_area:
            warnings.append(tr("The surveyed area {a:.2f} m² differs from the deed area {d:.2f} m² by {x:+.2f} m² "
                               "({p:+.2f}%), more than {t}%.").format(a=area, d=deed_area, x=diff,
                                                                        p=100 * diff / deed_area, t=area_tol))
    for name, value in (texts or {}).items():
        if value and len(str(value).encode("utf-8")) > DBF_TEXT:
            warnings.append(tr("{field} is longer than a Shapefile allows and was shortened.").format(field=name))
    return errors, warnings


# ---- DXF R12 sketch ----------------------------------------------------------------------------
# R12 (AC1009) is the version every CAD program opens without complaint; Arabic is written in
# Windows-1256 with $DWGCODEPAGE ANSI_1256 and a TrueType style (TEXT in R12 has no \U+ escapes).

class _Dxf:
    def __init__(self):
        self.ents = []

    def _add(self, *pairs):
        self.ents.extend(pairs)

    def text(self, x, y, h, s, layer="TEXT", rot=0.0, align=0):
        """align: 0 left, 1 centre, 2 right - middle of the text at (x, y) when not left."""
        pairs = [(0, "TEXT"), (8, layer), (7, "ARABIC"), (10, x), (20, y), (30, 0.0), (40, h), (1, s), (50, rot)]
        if align:
            pairs += [(72, align), (11, x), (21, y), (31, 0.0), (73, 2)]
        self._add(*pairs)

    def line(self, x1, y1, x2, y2, layer="TABLE"):
        self._add((0, "LINE"), (8, layer), (10, x1), (20, y1), (30, 0.0), (11, x2), (21, y2), (31, 0.0))

    def circle(self, x, y, r, layer="POINTS"):
        self._add((0, "CIRCLE"), (8, layer), (10, x), (20, y), (30, 0.0), (40, r))

    def polyline(self, pts, layer="PARCEL", closed=True):
        self._add((0, "POLYLINE"), (8, layer), (66, 1), (10, 0.0), (20, 0.0), (30, 0.0), (70, 1 if closed else 0))
        for x, y in pts:
            self._add((0, "VERTEX"), (8, layer), (10, x), (20, y), (30, 0.0))
        self._add((0, "SEQEND"), (8, layer))

    def save(self, path, extent):
        out = [(0, "SECTION"), (2, "HEADER"), (9, "$ACADVER"), (1, "AC1009"), (9, "$DWGCODEPAGE"),
               (3, "ANSI_1256"), (9, "$INSUNITS"), (70, 6), (9, "$EXTMIN"), (10, extent[0]), (20, extent[1]),
               (9, "$EXTMAX"), (10, extent[2]), (20, extent[3]), (0, "ENDSEC"),
               (0, "SECTION"), (2, "TABLES"),
               (0, "TABLE"), (2, "LTYPE"), (70, 1), (0, "LTYPE"), (2, "CONTINUOUS"), (70, 0), (3, "Solid line"),
               (72, 65), (73, 0), (40, 0.0), (0, "ENDTAB"),
               (0, "TABLE"), (2, "LAYER"), (70, 7)]
        for name, colour in (("PARCEL", 1), ("LENGTHS", 5), ("POINTS", 3), ("DESC", 6), ("NORTH", 7),
                             ("TITLE", 4), ("TABLE", 8)):
            out += [(0, "LAYER"), (2, name), (70, 0), (62, colour), (6, "CONTINUOUS")]
        out += [(0, "ENDTAB"), (0, "TABLE"), (2, "STYLE"), (70, 1), (0, "STYLE"), (2, "ARABIC"), (70, 0),
                (40, 0.0), (41, 1.0), (50, 0.0), (71, 0), (42, 1.0), (3, "arial.ttf"), (4, ""), (0, "ENDTAB"),
                (0, "ENDSEC"), (0, "SECTION"), (2, "ENTITIES")]
        out += self.ents + [(0, "ENDSEC"), (0, "EOF")]
        lines = []
        for code, value in out:
            if isinstance(value, float):
                value = f"{value:.4f}"
            lines.append(f"{code:>3}\r\n{value}\r\n")
        with open(path, "wb") as f:
            f.write("".join(lines).encode("cp1256", "replace"))


def write_kroki_dxf(path, desc, info, crs_name):
    """The parcel sketch in its real coordinates: boundary, side lengths, corner numbers, neighbours, north
    arrow, and below it the title block, the boundaries table and the coordinates table."""
    d = _Dxf()
    ring = [(x, y) for _, x, y in desc.corners]
    xs, ys = [p[0] for p in ring], [p[1] for p in ring]
    minx, maxx, miny, maxy = min(xs), max(xs), min(ys), max(ys)
    span = max(maxx - minx, maxy - miny, 1.0)
    th = max(span * 0.028, 0.2)
    d.polyline(ring)
    cx, cy = (minx + maxx) / 2, (miny + maxy) / 2
    for side in desc.sides.values():
        for e in side:
            (x1, y1), (x2, y2) = e.p, e.q
            ln = math.hypot(x2 - x1, y2 - y1) or 1
            nx, ny = (y2 - y1) / ln, -(x2 - x1) / ln                   # outside of a counter-clockwise ring
            rot = math.degrees(math.atan2(y2 - y1, x2 - x1))
            if rot > 90 or rot <= -90:
                rot += 180
            d.text((x1 + x2) / 2 + nx * th * 1.2, (y1 + y2) / 2 + ny * th * 1.2, th * 0.8,
                   f"{fmt_length(e.length)} م", "LENGTHS", rot % 360, 1)
    for n, x, y in desc.corners:
        d.circle(x, y, th * 0.25)
        ox, oy = x - cx, y - cy
        k = math.hypot(ox, oy) or 1
        d.text(x + ox / k * th * 1.1, y + oy / k * th * 1.1, th * 0.75, str(n), "POINTS", 0, 1)
    offsets = {"N": (cx, maxy + th * 4), "S": (cx, miny - th * 4), "E": (maxx + th * 3, cy), "W": (minx - th * 3, cy)}
    for key in DIRS_ORDER:
        text = desc.side_ar(key)
        if text:
            x, y = offsets[key]
            d.text(x, y, th * 0.7, text.split(" بطول ")[0], "DESC", 0, {"N": 1, "S": 1, "E": 0, "W": 2}[key])
    # north arrow at the top left
    ax, ay = minx - th * 2, maxy + th * 6
    d.line(ax, ay, ax, ay + th * 4, "NORTH")
    d.line(ax, ay + th * 4, ax - th * 0.8, ay + th * 2.6, "NORTH")
    d.line(ax, ay + th * 4, ax + th * 0.8, ay + th * 2.6, "NORTH")
    d.text(ax, ay + th * 5, th, "N", "NORTH", 0, 1)
    # title block and tables under the sketch, right aligned at the parcel's right edge
    right, w = maxx + th * 3, max(span + th * 6, th * 40)
    left = right - w
    y = miny - th * 9
    rows = [("رقم القطعة", info.get("parcel", "")), ("رقم الصك", info.get("deed", "")),
            ("المالك", info.get("owner", "")), ("المدينة / الحي", " / ".join(v for v in (info.get("city"),
                                                                                          info.get("district")) if v)),
            ("المساحة", f"{desc.area:,.2f} م²"), ("نظام الإحداثيات", crs_name)]
    d.text(right, y, th * 1.2, info.get("title", "كروكي القطعة"), "TITLE", 0, 2)
    y -= th * 2
    for label, value in rows:
        d.text(right, y, th * 0.8, f"{label}: {value}", "TITLE", 0, 2)
        y -= th * 1.4
    d.text(right, y - th * 0.5, th * 0.9, area_words(desc.area), "TITLE", 0, 2)
    y -= th * 3
    # boundaries
    d.text(right, y, th * 0.9, "بيان الحدود والأطوال", "TITLE", 0, 2)
    y -= th * 1.6
    for key in DIRS_ORDER:
        if desc.side_ar(key):
            d.text(right, y, th * 0.75, desc.side_ar(key), "TABLE", 0, 2)
            y -= th * 1.3
    y -= th
    # coordinates table with a grid
    cols = [right - w * 0.2, right - w * 0.6, left]             # right edges of the columns: no, E, N
    row_h = th * 1.4
    d.text(right, y, th * 0.9, "جدول الإحداثيات", "TITLE", 0, 2)
    y -= th * 1.2
    top = y
    header = ("النقطة", "الشرقي (E)", "الشمالي (N)")
    body = [(str(n), f"{x:.3f}", f"{yy:.3f}") for n, x, yy in desc.corners]
    for r, row in enumerate([header] + body):
        y0 = top - r * row_h
        centres = [(right + cols[0]) / 2, (cols[0] + cols[1]) / 2, (cols[1] + left) / 2]
        for c, value in enumerate(row):
            d.text(centres[c], y0 - row_h / 2, th * 0.7, value, "TABLE", 0, 1)
    bottom = top - (len(body) + 1) * row_h
    for r in range(len(body) + 2):
        d.line(left, top - r * row_h, right, top - r * row_h)
    for xline in (right, cols[0], cols[1], left):
        d.line(xline, top, xline, bottom)
    d.save(path, (min(left, minx) - th * 4, bottom - th, max(right, maxx) + th * 4, maxy + th * 12))


def write_kml(path, name, geom_wgs, desc_text, corners_wgs):
    def coords(ring):
        return " ".join(f"{x:.8f},{y:.8f},0" for x, y in ring)

    ring = outer_ring(geom_wgs)
    marks = "".join(f"<Placemark><name>{n}</name><styleUrl>#pt</styleUrl><Point><coordinates>{x:.8f},{y:.8f},0"
                    f"</coordinates></Point></Placemark>" for n, x, y in corners_wgs)
    kml = ('<?xml version="1.0" encoding="UTF-8"?>\n<kml xmlns="http://www.opengis.net/kml/2.2"><Document>'
           f"<name>{escape(name)}</name>"
           '<Style id="parcel"><LineStyle><color>ff0000ff</color><width>2</width></LineStyle>'
           "<PolyStyle><color>330000ff</color></PolyStyle></Style>"
           '<Style id="pt"><IconStyle><scale>0.6</scale></IconStyle></Style>'
           f"<Placemark><name>{escape(name)}</name><description>{escape(desc_text)}</description>"
           "<styleUrl>#parcel</styleUrl><Polygon><outerBoundaryIs><LinearRing><coordinates>"
           f"{coords(ring)}</coordinates></LinearRing></outerBoundaryIs></Polygon></Placemark>"
           f"<Folder><name>{escape('نقاط القطعة')}</name>{marks}</Folder></Document></kml>")
    with open(path, "w", encoding="utf-8") as f:
        f.write(kml)


def write_shapefiles(folder, base, geom, crs, attrs, corners, transform_context):
    """<base>.shp (the parcel) and <base>_points.shp (its corners), UTF-8 with .cpg and .prj."""
    pf = QgsFields()
    specs = [("PARCEL_NO", QMetaType.Type.QString, 50), ("DEED_NO", QMetaType.Type.QString, 50),
             ("OWNER", QMetaType.Type.QString, DBF_TEXT), ("CITY", QMetaType.Type.QString, 100),
             ("DISTRICT", QMetaType.Type.QString, 100), ("PLAN_NO", QMetaType.Type.QString, 50),
             ("AREA_M2", QMetaType.Type.Double, 0), ("SURV_DATE", QMetaType.Type.QDate, 0),
             ("NORTH", QMetaType.Type.QString, DBF_TEXT), ("SOUTH", QMetaType.Type.QString, DBF_TEXT),
             ("EAST", QMetaType.Type.QString, DBF_TEXT), ("WEST", QMetaType.Type.QString, DBF_TEXT)]
    for name, t, ln in specs:
        f = QgsField(name, t)
        if ln:
            f.setLength(ln)
        if t == QMetaType.Type.Double:
            f.setLength(16)
            f.setPrecision(3)
        pf.append(f)
    mem = QgsVectorLayer(f"Polygon?crs={crs.authid()}", base, "memory")
    mem.dataProvider().addAttributes(pf.toList())
    mem.updateFields()
    feat = QgsFeature(mem.fields())
    feat.setGeometry(geom)
    feat.setAttributes([attrs.get(n) for n, _, _ in specs])
    mem.dataProvider().addFeature(feat)
    _write(mem, os.path.join(folder, base + ".shp"), transform_context)

    pts = QgsVectorLayer(f"Point?crs={crs.authid()}", base + "_points", "memory")
    pts.dataProvider().addAttributes([QgsField("PT_NO", QMetaType.Type.Int), QgsField("E", QMetaType.Type.Double),
                                      QgsField("N", QMetaType.Type.Double),
                                      QgsField("PARCEL_NO", QMetaType.Type.QString, len=50)])
    pts.updateFields()
    feats = []
    for n, x, y in corners:
        pfeat = QgsFeature(pts.fields())
        pfeat.setGeometry(QgsGeometry.fromPointXY(QgsPointXY(x, y)))
        pfeat.setAttributes([n, round(x, 3), round(y, 3), attrs.get("PARCEL_NO")])
        feats.append(pfeat)
    pts.dataProvider().addFeatures(feats)
    _write(pts, os.path.join(folder, base + "_points.shp"), transform_context)


def _write(layer, path, transform_context):
    opts = QgsVectorFileWriter.SaveVectorOptions()
    opts.driverName = "ESRI Shapefile"
    opts.fileEncoding = "UTF-8"
    err = QgsVectorFileWriter.writeAsVectorFormatV3(layer, path, transform_context, opts)
    if err[0] != QgsVectorFileWriter.WriterError.NoError:
        raise QgsProcessingException(tr("Could not write {path}: {e}").format(path=path, e=err[1]))
    cpg = os.path.splitext(path)[0] + ".cpg"
    if not os.path.exists(cpg):
        with open(cpg, "w", encoding="ascii") as f:
            f.write("UTF-8")


class ParcelBundle(BiruniAlgorithm):
    ID = "parcelbundle"
    NAME = "Parcel deliverable bundle"
    GROUP = "Engineering office"
    GROUP_ID = "office"
    HELP = ("Prepares the files a survey office hands over for each parcel - for example to update a deed at "
            "the land registry or the municipality: the closed parcel polygon and its corner points as Shapefiles (UTF-8, with .cpg and "
            ".prj) carrying the owner, deed number, parcel number, city, district, area and date; a DXF sketch "
            "(كروكي) in real coordinates with side lengths, corner numbers, neighbours, north arrow, title block "
            "and coordinates table; a KML for Google Earth; the boundaries text; and an A4 survey report in PDF "
            "(Arabic) with the sketch, boundaries, coordinates, area in words, and places for the office stamp "
            "and signature. One folder per parcel and one ZIP of everything. Each parcel is checked first: one "
            "closed shape, no self-intersection, a projected system in metres, the area against the deed area. "
            "Parcels with a blocking problem are skipped and listed in checks.txt. By default the files are in "
            "KSA-GRF17 / UTM in Saudi Arabia (the zone of the parcel), otherwise in the layer's own projected "
            "system. Select parcels to prepare only those.")

    (INPUT, NUMBER, OWNER, DEED, CITY, DISTRICT, PLAN, DEED_AREA, STREETS, STREET_NAME, STREET_WIDTH, DEFAULT, STYLE,
     TOLERANCE, SEARCH, CRS, OFFICE, ENGINEER, NOTES, DATE, AREA_TOL, SHP, DXF, KML, PDF, ZIP, OUTPUT) = (
        "INPUT", "NUMBER", "OWNER", "DEED", "CITY", "DISTRICT", "PLAN", "DEED_AREA", "STREETS", "STREET_NAME",
        "STREET_WIDTH", "DEFAULT", "STYLE", "TOLERANCE", "SEARCH", "CRS", "OFFICE", "ENGINEER", "NOTES", "DATE",
        "AREA_TOL", "SHP", "DXF", "KML", "PDF", "ZIP", "OUTPUT")

    def flags(self):
        return super().flags() | Qgis.ProcessingAlgorithmFlag.NoThreading

    def initAlgorithm(self, config=None):  # noqa: N802
        self.addParameter(QgsProcessingParameterFeatureSource(self.INPUT, tr("Parcels"),
                                                              [Qgis.ProcessingSourceType.VectorPolygon]))
        for key, label in ((self.NUMBER, tr("Parcel number field")), (self.OWNER, tr("Owner field")),
                           (self.DEED, tr("Deed number field")), (self.CITY, tr("City field")),
                           (self.DISTRICT, tr("District field")), (self.PLAN, tr("Plan number field"))):
            self.addParameter(QgsProcessingParameterField(key, label, parentLayerParameterName=self.INPUT,
                                                          optional=True))
        self.addParameter(QgsProcessingParameterField(
            self.DEED_AREA, tr("Deed area field (m², to check the surveyed area)"),
            parentLayerParameterName=self.INPUT, type=Qgis.ProcessingFieldParameterDataType.Numeric, optional=True))
        add_neighbour_parameters(self)
        self.addParameter(QgsProcessingParameterCrs(
            self.CRS, tr("Coordinate system of the files (empty: KSA-GRF17 / UTM in Saudi Arabia)"), optional=True))
        self.addParameter(QgsProcessingParameterString(self.OFFICE, tr("Office name"), optional=True))
        self.addParameter(QgsProcessingParameterString(self.ENGINEER, tr("Surveyor"), optional=True))
        self.addParameter(QgsProcessingParameterString(self.NOTES, tr("Notes for the report"), multiLine=True,
                                                       optional=True))
        self.addParameter(QgsProcessingParameterString(self.DATE, tr("Survey date"),
                                                       QDate.currentDate().toString("yyyy-MM-dd")))
        self.addParameter(QgsProcessingParameterNumber(
            self.AREA_TOL, tr("Warn when the area differs from the deed by more than (%)"),
            Qgis.ProcessingNumberParameterType.Double, 1.0, minValue=0.0))
        for key, label in ((self.SHP, tr("Shapefiles")), (self.DXF, tr("DXF sketch")),
                           (self.KML, tr("KML (Google Earth)")), (self.PDF, tr("PDF survey report")),
                           (self.ZIP, tr("One ZIP of everything"))):
            self.addParameter(QgsProcessingParameterBoolean(key, label, True))
        self.addParameter(QgsProcessingParameterFolderDestination(self.OUTPUT, tr("Output folder")))

    def _field(self, parameters, context, key, fields, names):
        return self.parameterAsString(parameters, key, context) or guess_field(fields, names)

    def processAlgorithm(self, parameters, context, feedback):  # noqa: N802
        source = self.parameterAsSource(parameters, self.INPUT, context)
        if source is None:
            raise QgsProcessingException(self.invalidSourceError(parameters, self.INPUT))
        layer = whole_layer(self, parameters, self.INPUT, context)
        fields = source.fields()
        fnum = self._field(parameters, context, self.NUMBER, fields, NUMBER_FIELDS)
        fown = self._field(parameters, context, self.OWNER, fields, OWNER_FIELDS)
        fdeed = self._field(parameters, context, self.DEED, fields, DEED_FIELDS)
        fcity = self._field(parameters, context, self.CITY, fields, CITY_FIELDS)
        fdist = self._field(parameters, context, self.DISTRICT, fields, DISTRICT_FIELDS)
        fplan = self._field(parameters, context, self.PLAN, fields, PLAN_FIELDS)
        farea = self._field(parameters, context, self.DEED_AREA, fields, DEED_AREA_FIELDS)
        crs = source.sourceCrs()
        lon, lat = centre_wgs(crs, source.sourceExtent(), context)
        chosen = self.parameterAsCrs(parameters, self.CRS, context) if parameters.get(self.CRS) else None
        work = target_crs(chosen, crs, lon, lat)
        feedback.pushInfo(tr("Files in {crs}.").format(crs=f"{work.authid()} - {work.description()}"))
        finder, _ = build_finder(self, parameters, context, source, fnum, work, layer)
        style = ("saudi", "egypt")[self.parameterAsEnum(parameters, self.STYLE, context)]
        default = DEFAULTS[self.parameterAsEnum(parameters, self.DEFAULT, context)]
        office = self.parameterAsString(parameters, self.OFFICE, context)
        engineer = self.parameterAsString(parameters, self.ENGINEER, context)
        notes = self.parameterAsString(parameters, self.NOTES, context)
        date_text = self.parameterAsString(parameters, self.DATE, context) or QDate.currentDate().toString(
            "yyyy-MM-dd")
        date = QDate.fromString(date_text, "yyyy-MM-dd")
        area_tol = self.parameterAsDouble(parameters, self.AREA_TOL, context)
        want = {k: self.parameterAsBoolean(parameters, k, context) for k in (self.SHP, self.DXF, self.KML, self.PDF,
                                                                            self.ZIP)}
        out_dir = self.parameterAsString(parameters, self.OUTPUT, context)
        os.makedirs(out_dir, exist_ok=True)
        xf = QgsCoordinateTransform(crs, work, context.transformContext())
        to_wgs = QgsCoordinateTransform(work, QgsCoordinateReferenceSystem("EPSG:4326"), context.transformContext())

        def value(f, name):
            if not name or f.fields().lookupField(name) < 0:
                return ""
            v = f[name]
            return "" if v is None or str(v) == "NULL" else _clean_number(v)

        log, done, skipped, used = [], [], [], set()
        total = source.featureCount() or 1
        for n, f in enumerate(source.getFeatures()):
            if feedback.isCanceled():
                return {}
            g = QgsGeometry(f.geometry())
            num = value(f, fnum) or str(f.id())
            title = f"قطعة رقم {num}"
            if g.isNull() or g.isEmpty():
                skipped.append(num)
                log.append(f"{title}: {tr('The parcel has no geometry.')}")
                continue
            g.transform(xf)
            g.removeDuplicateNodes(3, False)
            deed_area = None
            if farea:
                try:
                    deed_area = float(f[farea]) if f[farea] not in (None, "") and str(f[farea]) != "NULL" else None
                except (TypeError, ValueError):
                    deed_area = None
            info = {"parcel": num, "deed": value(f, fdeed), "owner": value(f, fown), "city": value(f, fcity),
                    "district": value(f, fdist), "plan": value(f, fplan)}
            errors, warnings = preflight(g, work, deed_area, area_tol, {tr("Owner"): info["owner"]})
            for w in warnings:
                feedback.pushWarning(f"{title}: {w}")
                log.append(f"{title}: ⚠ {w}")
            if errors:
                for e in errors:
                    feedback.reportError(f"{title}: {e}", fatalError=False)
                    log.append(f"{title}: ✖ {e}")
                skipped.append(num)
                continue
            desc = describe_geometry(g, finder, f.id(), style, default)
            base = safe_name(num, f"parcel_{f.id()}")
            while base in used:
                base += "_"
            used.add(base)
            folder = os.path.join(out_dir, base)
            os.makedirs(folder, exist_ok=True)
            attrs = {"PARCEL_NO": num, "DEED_NO": info["deed"], "OWNER": utf8_cut(info["owner"])[0],
                     "CITY": info["city"], "DISTRICT": info["district"], "PLAN_NO": info["plan"],
                     "AREA_M2": round(desc.area, 3), "SURV_DATE": date if date.isValid() else None}
            for key, name in (("N", "NORTH"), ("S", "SOUTH"), ("E", "EAST"), ("W", "WEST")):
                attrs[name] = utf8_cut(desc.side_ar(key))[0]
            if want[self.SHP]:
                ring = [QgsPointXY(x, y) for _, x, y in desc.corners]
                write_shapefiles(folder, base, QgsGeometry.fromPolygonXY([ring + [ring[0]]]), work, attrs,
                                 desc.corners, context.transformContext())
            write_text(os.path.join(folder, base + ".txt"), [(title, desc)])
            if want[self.DXF]:
                write_kroki_dxf(os.path.join(folder, base + ".dxf"), desc, dict(info, title="كروكي القطعة"),
                                f"{work.authid()} {work.description()}")
            wgs = QgsGeometry(g)
            wgs.transform(to_wgs)
            corners_wgs = []
            for cn, x, y in desc.corners:
                p = to_wgs.transform(QgsPointXY(x, y))
                corners_wgs.append((cn, p.x(), p.y()))
            if want[self.KML]:
                write_kml(os.path.join(folder, base + ".kml"), title, wgs, desc.text_ar(), corners_wgs)
            if want[self.PDF]:
                c = wgs.centroid().asPoint()
                page = ReportPage(
                    office=office, engineer=engineer, notes=notes, date=date_text,
                    crs=f"{work.authid()} - {work.description()}",
                    info=[(lab, v) for lab, v in (("رقم القطعة", num), ("رقم الصك", info["deed"]),
                                                  ("المالك", info["owner"]), ("رقم المخطط", info["plan"]),
                                                  ("المدينة", info["city"]), ("الحي", info["district"]))],
                    pts=[(x, y) for _, x, y in reversed(desc.corners)],
                    corners=desc.corners,
                    side_labels={k: desc.side_ar(k).split(" بطول ")[0] for k in DIRS_ORDER if desc.side_ar(k)},
                    boundaries=[(STYLES[style][k], " + ".join(fmt_length(e.length) for e in desc.sides[k]),
                                 " + ".join(a for a, _ in desc.side_neighbours(k)))
                                for k in DIRS_ORDER if desc.sides.get(k)],
                    area=desc.area, area_words=area_words(desc.area),
                    qr_text=f"{title} | {desc.area:.2f} m2 | https://maps.google.com/?q={c.y():.6f},{c.x():.6f}")
                write_pdf(os.path.join(folder, base + ".pdf"), [page], title)
            done.append(num)
            feedback.setProgress(100 * n / total)

        with open(os.path.join(out_dir, "checks.txt"), "w", encoding="utf-8-sig") as fh:
            fh.write("\n".join(log) or tr("All parcels passed the checks."))
        if not done:
            raise QgsProcessingException(tr("No parcel could be prepared:") + "\n" + "\n".join(log))
        result = {self.OUTPUT: out_dir, "PARCEL_COUNT": len(done), "SKIPPED_COUNT": len(skipped),
                  "CRS": work.authid()}
        if want[self.ZIP]:
            zpath = os.path.join(out_dir, safe_name(os.path.basename(out_dir.rstrip("\\/")), "parcels") + ".zip")
            with zipfile.ZipFile(zpath, "w", zipfile.ZIP_DEFLATED) as z:
                for root, _, files in os.walk(out_dir):
                    for fn in files:
                        full = os.path.join(root, fn)
                        if os.path.abspath(full) != os.path.abspath(zpath):
                            z.write(full, os.path.relpath(full, out_dir))
            result["ZIP"] = zpath
        feedback.pushInfo(tr("{n} parcels prepared, {s} skipped.").format(n=len(done), s=len(skipped)))
        return result
