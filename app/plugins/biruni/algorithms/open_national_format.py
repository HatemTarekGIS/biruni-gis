import os

from qgis.core import (Qgis, QgsCoordinateReferenceSystem, QgsCoordinateTransform, QgsFeatureRequest,
                       QgsProcessing, QgsProcessingContext, QgsProcessingException, QgsProcessingParameterCrs,
                       QgsProcessingParameterEnum, QgsProcessingParameterFeatureSource,
                       QgsProcessingParameterField, QgsProcessingParameterFile,
                       QgsProcessingParameterFileDestination, QgsProcessingParameterNumber,
                       QgsProcessingParameterString, QgsVectorLayer, QgsWkbTypes)

from ..i18n import tr
from .base import BiruniAlgorithm

READ_FORMATS = ["auto", "ncz", "sxf", "cxf", "sima", "cadastral_xml", "dgn"]
WRITE_FORMATS = ["sima", "sigef_ods", "catastro_gml"]


def _read_labels():
    return [tr("Detect from the file"), tr("Netcad drawing (NCZ) - Turkey"), tr("Panorama SXF - Russia"),
            tr("Cadastral sheet CXF - Italy"), tr("SIMA survey coordinates - Japan"),
            tr("Cadastral XML - Rosreestr (Russia) / IN4 (Ukraine)"), tr("MicroStation DGN v7")]


def _write_labels():
    return [tr("SIMA survey coordinates - Japan"), tr("SIGEF certification spreadsheet (ODS) - Brazil"),
            tr("Catastro parcel GML (IVG) - Spain")]


class OpenNationalFormat(BiruniAlgorithm):
    ID = "opennationalformat"
    NAME = "Open a national file (NCZ, SXF, CXF, SIMA, cadastral XML…)"
    GROUP = "Import"
    GROUP_ID = "import"
    HELP = ("Opens drawing and cadastre files that GIS programs usually cannot: Netcad NCZ (Turkey), "
            "Panorama SXF (Russia), Italian cadastral sheets CXF, Japanese SIMA survey files, Rosreestr "
            "cadastral XML (Russia) and the Ukrainian cadastral exchange file, and MicroStation DGN v7. "
            "Every layer of the file is saved into one GeoPackage and added to the map; points, lines, "
            "polygons and texts are kept apart. When the file does not state its coordinate system, "
            "choose it here.")

    INPUT, FORMAT, CRS, RSC, OUTPUT = "INPUT", "FORMAT", "CRS", "RSC", "OUTPUT"

    def initAlgorithm(self, config=None):  # noqa: N802
        from .. import formats

        self.addParameter(QgsProcessingParameterFile(
            self.INPUT, tr("File"), extension="", fileFilter=formats.file_filter() + ";;All files (*)"))
        self.addParameter(QgsProcessingParameterEnum(
            self.FORMAT, tr("Format"), options=_read_labels(), defaultValue=0))
        self.addParameter(QgsProcessingParameterCrs(
            self.CRS, tr("Coordinate system (when the file does not say)"), optional=True))
        self.addParameter(QgsProcessingParameterFile(
            self.RSC, tr("SXF classifier (.rsc), if it is not next to the file"), optional=True,
            fileFilter="RSC (*.rsc *.RSC)"))
        self.addParameter(QgsProcessingParameterFileDestination(
            self.OUTPUT, tr("GeoPackage"), "GeoPackage (*.gpkg)"))

    def flags(self):
        return super().flags() | Qgis.ProcessingAlgorithmFlag.NoThreading

    def processAlgorithm(self, parameters, context, feedback):  # noqa: N802
        from .. import formats
        from ..formats.common import FormatError, write_gpkg

        path = self.parameterAsFile(parameters, self.INPUT, context)
        if not path or not os.path.exists(path):
            raise QgsProcessingException(tr("Choose the file to open."))
        fmt = READ_FORMATS[self.parameterAsEnum(parameters, self.FORMAT, context)]
        fmt = None if fmt == "auto" else fmt
        try:
            if (fmt or formats.detect(path)) == "sxf":
                from ..formats import sxf

                rsc = self.parameterAsFile(parameters, self.RSC, context) or None
                dataset = sxf.read(path, rsc=rsc)
            else:
                dataset = formats.read(path, fmt)
        except FormatError as e:
            raise QgsProcessingException(tr(str(e))) from e
        crs = self.parameterAsCrs(parameters, self.CRS, context)
        crs_text = crs.toWkt() if crs.isValid() else dataset.crs
        out = self.parameterAsFileOutput(parameters, self.OUTPUT, context)
        written = write_gpkg(dataset, out, crs_text)
        for note in dataset.notes:
            feedback.pushInfo(note)
        if not crs_text:
            feedback.reportError(tr("The file does not say its coordinate system and none was chosen: the "
                                    "layers have none. Set it with «What coordinate system is this?»."), False)
        name = os.path.splitext(os.path.basename(path))[0]
        for table, kind, n in written:
            feedback.pushInfo(tr("{t}: {n} features").format(t=table, n=n))
            uri = f"{out}|layername={table}"
            lyr = QgsVectorLayer(uri, f"{name} - {table}", "ogr")
            if lyr.isValid():
                context.temporaryLayerStore().addMapLayer(lyr)
                context.addLayerToLoadOnCompletion(lyr.id(), QgsProcessingContext.LayerDetails(
                    f"{name} - {table}", context.project(), self.OUTPUT))
        return {self.OUTPUT: out, "LAYER_COUNT": len(written), "FEATURE_COUNT": sum(n for *_x, n in written),
                "FORMAT": dataset.format, "NOTES": "\n".join(dataset.notes)}


class ExportNationalFormat(BiruniAlgorithm):
    ID = "exportnationalformat"
    NAME = "Save parcels in a national format (SIMA, SIGEF, Catastro GML)"
    GROUP = "Import"
    GROUP_ID = "import"
    HELP = ("Writes parcels (and points) in the file a national authority or local software expects: "
            "SIMA for Japanese survey software, INCRA's SIGEF certification spreadsheet (ODS) for Brazilian "
            "rural properties - vertex codes, SIRGAS 2000 coordinates and precisions, checked against INCRA's "
            "limits - and the INSPIRE parcel GML that Spain's Catastro accepts for the graphic validation "
            "report (IVG), in ETRS89 UTM. Coordinates are converted to the system each format requires.")

    INPUT, FORMAT, ID_FIELD, REF_FIELD, PREFIX, KIND, SIGMA, LIMIT, START, OUTPUT = (
        "INPUT", "FORMAT", "ID_FIELD", "REF_FIELD", "PREFIX", "KIND", "SIGMA", "LIMIT", "START", "OUTPUT")
    VERTEX_KINDS = ["M", "P", "V"]

    def initAlgorithm(self, config=None):  # noqa: N802
        self.addParameter(QgsProcessingParameterFeatureSource(
            self.INPUT, tr("Parcels (polygons) or points"),
            [QgsProcessing.SourceType.TypeVectorPolygon, QgsProcessing.SourceType.TypeVectorPoint]))
        self.addParameter(QgsProcessingParameterEnum(
            self.FORMAT, tr("Format"), options=_write_labels(), defaultValue=0))
        self.addParameter(QgsProcessingParameterField(
            self.ID_FIELD, tr("Parcel number / point name field"), parentLayerParameterName=self.INPUT,
            optional=True))
        self.addParameter(QgsProcessingParameterField(
            self.REF_FIELD, tr("Cadastral reference field (Catastro)"), parentLayerParameterName=self.INPUT,
            optional=True))
        self.addParameter(QgsProcessingParameterString(
            self.PREFIX, tr("SIGEF accreditation code of the surveyor (e.g. ABCD)"), defaultValue="ABCD",
            optional=True))
        self.addParameter(QgsProcessingParameterEnum(
            self.KIND, tr("SIGEF vertex type"), options=[tr("M - monumented"), tr("P - not monumented"),
                                                         tr("V - virtual")], defaultValue=0))
        self.addParameter(QgsProcessingParameterNumber(
            self.SIGMA, tr("SIGEF horizontal precision of the vertices (m)"),
            Qgis.ProcessingNumberParameterType.Double, 0.05, minValue=0.0))
        self.addParameter(QgsProcessingParameterString(
            self.LIMIT, tr("SIGEF boundary type (LA1… artificial, LN1… natural)"), defaultValue="LA1"))
        self.addParameter(QgsProcessingParameterNumber(
            self.START, tr("SIGEF first vertex number"), Qgis.ProcessingNumberParameterType.Integer, 1,
            minValue=1))
        self.addParameter(QgsProcessingParameterFileDestination(
            self.OUTPUT, tr("Output file"), "SIMA (*.sim);;ODS (*.ods);;GML (*.gml)"))
        for p in (self.REF_FIELD, self.PREFIX, self.KIND, self.SIGMA, self.LIMIT, self.START):
            self.parameterDefinition(p).setFlags(
                self.parameterDefinition(p).flags() | Qgis.ProcessingParameterFlag.Advanced)

    def processAlgorithm(self, parameters, context, feedback):  # noqa: N802
        from ..formats import catastro_gml, sigef_ods, sima
        from ..formats.common import FormatError

        source = self.parameterAsSource(parameters, self.INPUT, context)
        if source is None:
            raise QgsProcessingException(self.invalidSourceError(parameters, self.INPUT))
        fmt = WRITE_FORMATS[self.parameterAsEnum(parameters, self.FORMAT, context)]
        id_field = self.parameterAsString(parameters, self.ID_FIELD, context)
        out = self.parameterAsFileOutput(parameters, self.OUTPUT, context)
        ext = {"sima": ".sim", "sigef_ods": ".ods", "catastro_gml": ".gml"}[fmt]
        if not out.lower().endswith(ext):
            out = os.path.splitext(out)[0] + ext
        polygons = QgsWkbTypes.geometryType(source.wkbType()) == Qgis.GeometryType.Polygon

        def target(crs):
            xf = QgsCoordinateTransform(source.sourceCrs(), crs, context.transformContext())
            rows = []
            for f in source.getFeatures(QgsFeatureRequest()):
                g = f.geometry()
                if g.isNull() or g.isEmpty():
                    continue
                g.transform(xf)
                rows.append((f, g))
            return rows

        try:
            if fmt == "sima":
                rows = target(source.sourceCrs())
                pts, plots = [], []
                for i, (f, g) in enumerate(rows, 1):
                    label = str(f[id_field]) if id_field else str(i)
                    if polygons:
                        for part in g.asMultiPolygon() if g.isMultipart() else [g.asPolygon()]:
                            plots.append((label, "", [(p.x(), p.y()) for p in part[0]]))
                    else:
                        p = g.asPoint()
                        pts.append((i, label, p.x(), p.y(), None))
                sima.write(out, pts, plots)
                count = len(plots) or len(pts)
            elif fmt == "sigef_ods":
                if not polygons:
                    raise QgsProcessingException(tr("SIGEF needs parcel polygons."))
                rows = target(QgsCoordinateReferenceSystem("EPSG:4674"))
                perimeters, start = [], self.parameterAsInt(parameters, self.START, context)
                kind = self.VERTEX_KINDS[self.parameterAsEnum(parameters, self.KIND, context)]
                prefix = (self.parameterAsString(parameters, self.PREFIX, context) or "ABCD").strip().upper()
                sigma = self.parameterAsDouble(parameters, self.SIGMA, context)
                limit = self.parameterAsString(parameters, self.LIMIT, context) or "LA1"
                for f, g in rows:
                    for part in g.asMultiPolygon() if g.isMultipart() else [g.asPolygon()]:
                        verts = sigef_ods.vertices_from_ring([(p.x(), p.y()) for p in part[0]], prefix, kind,
                                                             start, sigma, limit=limit)
                        start += len(verts)
                        perimeters.append(verts)
                sigef_ods.write(out, perimeters, {"denominacao": os.path.splitext(os.path.basename(out))[0]})
                count = len(perimeters)
            else:
                if not polygons:
                    raise QgsProcessingException(tr("Catastro GML needs parcel polygons."))
                ext_ll = QgsCoordinateTransform(source.sourceCrs(), QgsCoordinateReferenceSystem("EPSG:4326"),
                                                context.transformContext()).transformBoundingBox(source.sourceExtent())
                lon, lat = ext_ll.center().x(), ext_ll.center().y()
                zone = int((lon + 180) / 6) + 1
                epsg = 4083 if (lat < 30 and lon < -12) else 25800 + max(28, min(31, zone))
                rows = target(QgsCoordinateReferenceSystem(f"EPSG:{epsg}"))
                ref_field = self.parameterAsString(parameters, self.REF_FIELD, context)
                parcels = []
                for i, (f, g) in enumerate(rows, 1):
                    polys = g.asMultiPolygon() if g.isMultipart() else [g.asPolygon()]
                    parcels.append({"id": str(f[id_field]) if id_field else str(i),
                                    "reference": str(f[ref_field]) if ref_field and f[ref_field] else "",
                                    "polygons": [[[(p.x(), p.y()) for p in ring] for ring in poly] for poly in polys],
                                    "area": g.area()})
                catastro_gml.write(out, parcels, epsg)
                problems = catastro_gml.validate(out)
                for p in problems:
                    feedback.reportError(p, False)
                feedback.pushInfo(tr("Written in EPSG:{e} (ETRS89 / REGCAN95 UTM).").format(e=epsg))
                count = len(parcels)
        except FormatError as e:
            raise QgsProcessingException(tr("The file was not written:") + "\n" + str(e)) from e
        feedback.pushInfo(tr("{n} parcels or points written to {p}.").format(n=count, p=out))
        return {self.OUTPUT: out, "COUNT": count}
