import os

import numpy as np
from qgis.core import (Qgis, QgsProcessingException, QgsProcessingParameterCrs, QgsProcessingParameterEnum,
                       QgsProcessingParameterFeatureSource, QgsProcessingParameterFile, QgsProcessingParameterNumber,
                       QgsProcessingParameterRasterDestination, QgsProcessingParameterString, QgsUnitTypes)

from ..i18n import tr
from .base import BiruniAlgorithm
from .scan_to_parcels import PlanTransform, ScanToParcels, kind_label


def page_path(path, page):
    """GDAL name of a PDF page ('PDF:2:file.pdf'); page 1 and images are the file itself."""
    if path.lower().endswith(".pdf") and page > 1:
        return f"PDF:{int(page)}:{path}"
    return path


def pdf_page_count(path, dpi=72):
    from osgeo import gdal

    ds = gdal.OpenEx(path, gdal.OF_RASTER, open_options=[f"DPI={int(dpi)}"])
    if ds is None:
        return 0
    subs = ds.GetSubDatasets()
    return max(1, len(subs))


def open_page(path, page, dpi):
    from osgeo import gdal

    name = page_path(path, page)
    opts = [f"DPI={int(dpi)}"] if path.lower().endswith(".pdf") else []
    ds = gdal.OpenEx(name, gdal.OF_RASTER, open_options=opts)
    if ds is None:
        raise QgsProcessingException(tr("The image could not be opened: {p}").format(p=name))
    return ds, name, opts


class GeorefPdf(BiruniAlgorithm):
    """A PDF page or an image -> GeoTIFF from control points (or the GeoPDF's own georeference)."""

    ID = "georefpdf"
    NAME = "Georeference a PDF or image"
    GROUP = "Raster & terrain"
    GROUP_ID = "raster"
    HELP = ("Puts a PDF page or an image (scanned map, plan, screenshot) on the map as a GeoTIFF. Give control "
            "points, one per line as 'pixel_x pixel_y X Y' (pixels counted from the top-left corner of the page at "
            "the chosen resolution), a points layer with the fields pixel_x and pixel_y, or a georeferencer "
            ".points file. Two points fit scale and rotation, three or four an affine transform, five or more a "
            "projective one; the error of every point is reported. If the PDF is already geospatial (a GeoPDF "
            "exported from GIS or CAD software), choose 'Use the georeference inside the PDF' and no control "
            "points are needed.")

    INPUT, PAGE, DPI, MODE = "INPUT", "PAGE", "DPI", "MODE"
    GCP_LAYER, GCP_TEXT, TRANSFORM, CRS, OUTPUT = "GCP_LAYER", "GCP_TEXT", "TRANSFORM", "CRS", "OUTPUT"
    TRANSFORM_KEYS = ["auto", "similarity", "affine", "projective"]

    def initAlgorithm(self, config=None):  # noqa: N802
        self.addParameter(QgsProcessingParameterFile(
            self.INPUT, tr("PDF or image"),
            fileFilter=tr("Images and PDF") + " (*.pdf *.tif *.tiff *.png *.jpg *.jpeg *.bmp *.gif)"))
        self.addParameter(QgsProcessingParameterNumber(
            self.PAGE, tr("Page of the PDF"), Qgis.ProcessingNumberParameterType.Integer, 1, minValue=1))
        self.addParameter(QgsProcessingParameterNumber(
            self.DPI, tr("Resolution for PDF files (dpi)"), Qgis.ProcessingNumberParameterType.Integer, 300,
            minValue=72, maxValue=1200))
        self.addParameter(QgsProcessingParameterEnum(
            self.MODE, tr("Georeference"),
            [tr("From control points"), tr("Use the georeference inside the PDF (GeoPDF)")], defaultValue=0))
        self.addParameter(QgsProcessingParameterFeatureSource(
            self.GCP_LAYER, tr("Control points layer (fields pixel_x, pixel_y)"),
            [Qgis.ProcessingSourceType.VectorPoint, Qgis.ProcessingSourceType.Vector], optional=True))
        self.addParameter(QgsProcessingParameterString(
            self.GCP_TEXT, tr("Control points: pixel_x pixel_y X Y, one per line"), multiLine=True,
            optional=True))
        self.addParameter(QgsProcessingParameterEnum(
            self.TRANSFORM, tr("Transformation"),
            [tr("Automatic (from the number of points)"), tr("Scale and rotation (2+ points)"),
             tr("Affine (3+ points)"), tr("Projective, for photos (4+ points)")], defaultValue=0))
        self.addParameter(QgsProcessingParameterCrs(self.CRS, tr("Coordinate system of the control points"),
                                                    "ProjectCrs"))
        self.addParameter(QgsProcessingParameterRasterDestination(self.OUTPUT, tr("Georeferenced image (GeoTIFF)")))

    # the control points are read exactly like the scanned-plan tool reads them
    _control_points = ScanToParcels._control_points

    def processAlgorithm(self, parameters, context, feedback):  # noqa: N802
        from osgeo import gdal

        path = self.parameterAsFile(parameters, self.INPUT, context)
        if not path or not os.path.exists(path):
            raise QgsProcessingException(tr("Choose the PDF or the image."))
        page = self.parameterAsInt(parameters, self.PAGE, context)
        dpi = self.parameterAsInt(parameters, self.DPI, context)
        dest = self.parameterAsOutputLayer(parameters, self.OUTPUT, context)
        if path.lower().endswith(".pdf"):
            pages = pdf_page_count(path)
            if page > pages:
                raise QgsProcessingException(tr("The PDF has {n} pages.").format(n=pages))
        ds, name, opts = open_page(path, page, dpi)

        if self.parameterAsEnum(parameters, self.MODE, context) == 1:
            if not ds.GetProjection() or ds.GetGeoTransform(can_return_null=True) is None:
                raise QgsProcessingException(tr("This file has no georeference inside: give control points."))
            ds = None
            out = gdal.Translate(dest, gdal.OpenEx(name, gdal.OF_RASTER, open_options=opts), format="GTiff",
                                 creationOptions=["COMPRESS=DEFLATE", "TILED=YES", "BIGTIFF=IF_SAFER"])
            if out is None:
                raise QgsProcessingException(tr("The georeferenced image could not be written."))
            out = None
            feedback.pushInfo(tr("The georeference inside the file was used."))
            return {self.OUTPUT: dest, "GCP_RMS": 0.0, "TRANSFORMATION": "file"}

        crs = self.parameterAsCrs(parameters, self.CRS, context)
        if not crs.isValid():
            raise QgsProcessingException(tr("Choose the coordinate system of the control points."))
        pts = self._control_points(parameters, context, crs)
        kind = self.TRANSFORM_KEYS[self.parameterAsEnum(parameters, self.TRANSFORM, context)]
        try:
            model = PlanTransform.fit(pts, kind)
        except (ValueError, np.linalg.LinAlgError):
            need = PlanTransform.MIN_POINTS.get(kind, 2)
            raise QgsProcessingException(
                tr("At least {n} control points are needed ({m} given).").format(n=need, m=len(pts)))
        units = QgsUnitTypes.toAbbreviatedString(crs.mapUnits())
        feedback.pushInfo(tr("Transformation: {k}, {n} control points.").format(k=kind_label(model.kind), n=len(pts)))
        for i, r in enumerate(model.residuals, 1):
            feedback.pushInfo(tr("Control point {i}: error {r:.3f} {u}").format(i=i, r=r, u=units))
        feedback.pushInfo(tr("Mean error (RMS): {r:.3f} {u}").format(r=model.rms, u=units))
        pix = model.pixel_size(ds.RasterXSize / 2, ds.RasterYSize / 2)
        if pix <= 0:
            raise QgsProcessingException(tr("The control points are on one line: add points spread over the plan."))
        if len(pts) > PlanTransform.MIN_POINTS[model.kind] and model.rms > 5 * pix:
            feedback.pushWarning(tr("The control points disagree by more than five pixels: check the point with "
                                    "the largest error."))
        feedback.pushInfo(tr("One pixel of the scan is {p:.3f} {u} on the ground.").format(p=pix, u=units))
        ScanToParcels._write_raster(dest, name, opts, ds, model, crs)
        ds = None
        return {self.OUTPUT: dest, "GCP_RMS": model.rms, "TRANSFORMATION": model.kind,
                "RESIDUALS": [float(r) for r in model.residuals]}
