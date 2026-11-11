"""Imagery for CAD: a large orthomosaic or raster cut into JPG/PNG tiles that
AutoCAD and other CAD programs can insert, each with its world file and .prj."""
import csv
import math
import os

from qgis.core import (Qgis, QgsFeature, QgsFeatureSink, QgsField, QgsFields, QgsGeometry, QgsPointXY,
                       QgsProcessingException, QgsProcessingParameterBoolean, QgsProcessingParameterEnum,
                       QgsProcessingParameterFeatureSink, QgsProcessingParameterFolderDestination,
                       QgsProcessingParameterNumber, QgsProcessingParameterRasterLayer, QgsRectangle)
from qgis.PyQt.QtCore import QMetaType

from ..i18n import tr
from .base import BiruniAlgorithm

TILE_FORMATS = [("JPEG", "jpg", "jgw"), ("PNG", "png", "pgw")]
JPEG_MAX = 65000            # JPEG cannot be wider or taller than 65,535 pixels
MAX_BYTES = 2_000_000_000   # stay below 2 GB uncompressed per tile (CAD and 32-bit readers)


def tile_grid(width, height, max_side, bands):
    """(columns, rows, tile width, tile height) so that every tile fits the limits."""
    side = max(256, min(int(max_side), JPEG_MAX))
    while side * side * max(bands, 1) > MAX_BYTES:
        side = int(side * 0.9)
    cols = max(1, math.ceil(width / side))
    rows = max(1, math.ceil(height / side))
    return cols, rows, math.ceil(width / cols), math.ceil(height / rows)


def world_file(path, gt):
    """ESRI world file of the top-left pixel centre."""
    with open(path, "w", encoding="ascii") as f:
        f.write("\n".join(f"{v:.10f}" for v in (gt[1], gt[4], gt[2], gt[5],
                                                 gt[0] + gt[1] / 2 + gt[2] / 2, gt[3] + gt[4] / 2 + gt[5] / 2)))
        f.write("\n")


class OrthoTiles(BiruniAlgorithm):
    ID = "orthotiles"
    NAME = "Imagery for CAD (tiles with world files)"
    GROUP = "Survey & CAD"
    GROUP_ID = "survey"
    HELP = ("AutoCAD and many CAD programs refuse big GeoTIFF orthomosaics (BigTIFF over 2-4 GB) and don't read "
            "their georeferencing. This tool cuts the image into JPG or PNG tiles that CAD can insert, each with a "
            "world file (.jgw / .pgw) and a .prj, so every tile lands in its place. The tile size is chosen so "
            "that every tile stays under 2 GB and the JPEG size limit; an image that is not 8-bit (a DEM, a "
            "16-bit ortho) is stretched to 0-255 first. An index of the tiles is written as CSV and DXF (tile "
            "outlines with their names) and as a layer.")

    INPUT, FORMAT, MAX_SIDE, DOWNSAMPLE, QUALITY, OUTPUT, INDEX = (
        "INPUT", "FORMAT", "MAX_SIDE", "DOWNSAMPLE", "QUALITY", "OUTPUT", "INDEX")

    def initAlgorithm(self, config=None):  # noqa: N802
        self.addParameter(QgsProcessingParameterRasterLayer(self.INPUT, tr("Image or orthomosaic")))
        self.addParameter(QgsProcessingParameterEnum(
            self.FORMAT, tr("Tile format"), [f[0] for f in TILE_FORMATS], defaultValue=0))
        self.addParameter(QgsProcessingParameterNumber(
            self.MAX_SIDE, tr("Largest tile side (pixels)"), Qgis.ProcessingNumberParameterType.Integer, 8000,
            minValue=256, maxValue=JPEG_MAX))
        self.addParameter(QgsProcessingParameterNumber(
            self.DOWNSAMPLE, tr("Reduce resolution by (1 = full resolution)"),
            Qgis.ProcessingNumberParameterType.Integer, 1, minValue=1, maxValue=64))
        self.addParameter(QgsProcessingParameterNumber(
            self.QUALITY, tr("JPEG quality"), Qgis.ProcessingNumberParameterType.Integer, 85, minValue=10,
            maxValue=100))
        self.addParameter(QgsProcessingParameterFolderDestination(self.OUTPUT, tr("Folder for the tiles")))
        self.addParameter(QgsProcessingParameterFeatureSink(
            self.INDEX, tr("Tile index"), Qgis.ProcessingSourceType.VectorPolygon, optional=True,
            createByDefault=False))

    def processAlgorithm(self, parameters, context, feedback):  # noqa: N802
        from osgeo import gdal, osr

        gdal.UseExceptions()
        layer = self.parameterAsRasterLayer(parameters, self.INPUT, context)
        if layer is None:
            raise QgsProcessingException(self.invalidRasterError(parameters, self.INPUT))
        src = gdal.Open(layer.source())
        if src is None:
            raise QgsProcessingException(tr("The image could not be opened."))
        fmt_name, ext, wext = TILE_FORMATS[self.parameterAsEnum(parameters, self.FORMAT, context)]
        down = self.parameterAsInt(parameters, self.DOWNSAMPLE, context)
        quality = self.parameterAsInt(parameters, self.QUALITY, context)
        folder = self.parameterAsString(parameters, self.OUTPUT, context)
        os.makedirs(folder, exist_ok=True)

        width, height = src.RasterXSize // down, src.RasterYSize // down
        if width < 1 or height < 1:
            raise QgsProcessingException(tr("The image is too small for this reduction."))
        nb = src.RasterCount
        bands = [1, 2, 3] if nb >= 3 else [1]
        if nb == 4 and fmt_name == "PNG":
            bands = [1, 2, 3, 4]
        gt0 = src.GetGeoTransform()
        gt = (gt0[0], gt0[1] * down, gt0[2] * down, gt0[3], gt0[4] * down, gt0[5] * down)
        cols, rows, tw, th = tile_grid(width, height, self.parameterAsInt(parameters, self.MAX_SIDE, context),
                                       len(bands))

        scale = []
        for b in bands:
            band = src.GetRasterBand(b)
            if band.DataType == gdal.GDT_Byte:
                scale.append(None)
            else:
                mn, mx, _mean, _sd = band.ComputeStatistics(True)
                scale.append([mn, mx if mx > mn else mn + 1, 0, 255])
        wkt = src.GetProjection()
        esri_prj = ""
        if wkt:
            srs = osr.SpatialReference()
            srs.ImportFromWkt(wkt)
            srs.MorphToESRI()
            esri_prj = srs.ExportToWkt()

        base = os.path.splitext(os.path.basename(layer.source().split("|")[0]))[0] or "tile"
        fields = QgsFields()
        for n, t in (("tile", QMetaType.Type.QString), ("file", QMetaType.Type.QString),
                     ("column", QMetaType.Type.Int), ("row", QMetaType.Type.Int),
                     ("width_px", QMetaType.Type.Int), ("height_px", QMetaType.Type.Int),
                     ("pixel_size", QMetaType.Type.Double)):
            fields.append(QgsField(n, t))
        isink, iid = self.parameterAsSink(parameters, self.INDEX, context, fields, Qgis.WkbType.Polygon,
                                          layer.crs())
        index_rows = []
        total = cols * rows
        n = 0
        for r in range(rows):
            for c in range(cols):
                if feedback.isCanceled():
                    return {}
                xoff, yoff = c * tw, r * th
                w, h = min(tw, width - xoff), min(th, height - yoff)
                if w <= 0 or h <= 0:
                    continue
                name = f"{base}_r{r + 1:02d}_c{c + 1:02d}"
                out = os.path.join(folder, f"{name}.{ext}")
                opts = gdal.TranslateOptions(
                    format=fmt_name, bandList=bands, outputType=gdal.GDT_Byte,
                    srcWin=[xoff * down, yoff * down, w * down, h * down], width=w, height=h,
                    scaleParams=[s if s is not None else [0, 255, 0, 255] for s in scale] if any(scale) else None,
                    creationOptions=[f"QUALITY={quality}"] if fmt_name == "JPEG" else [], resampleAlg="average"
                    if down > 1 else "nearest")
                gdal.Translate(out, src, options=opts)
                for aux in (out + ".aux.xml",):
                    if os.path.exists(aux):
                        os.remove(aux)
                tgt = (gt[0] + xoff * gt[1] + yoff * gt[2], gt[1], gt[2], gt[3] + xoff * gt[4] + yoff * gt[5],
                       gt[4], gt[5])
                world_file(os.path.join(folder, f"{name}.{wext}"), tgt)
                if esri_prj:
                    with open(os.path.join(folder, f"{name}.prj"), "w", encoding="ascii") as f:
                        f.write(esri_prj)
                corners = [(tgt[0] + px * tgt[1] + py * tgt[2], tgt[3] + px * tgt[4] + py * tgt[5])
                           for px, py in ((0, 0), (w, 0), (w, h), (0, h))]
                index_rows.append((name, f"{name}.{ext}", c + 1, r + 1, w, h, abs(gt[1]), corners))
                if isink is not None:
                    feat = QgsFeature(fields)
                    feat.setGeometry(QgsGeometry.fromPolygonXY([[QgsPointXY(*p) for p in corners + corners[:1]]]))
                    feat.setAttributes([name, f"{name}.{ext}", c + 1, r + 1, w, h, abs(gt[1])])
                    isink.addFeature(feat, QgsFeatureSink.Flag.FastInsert)
                n += 1
                feedback.setProgress(100 * n / total)

        with open(os.path.join(folder, f"{base}_index.csv"), "w", newline="", encoding="utf-8") as f:
            wr = csv.writer(f)
            wr.writerow(["tile", "file", "column", "row", "width_px", "height_px", "pixel_size", "xmin", "ymin",
                         "xmax", "ymax"])
            for name, file, c, r, w, h, px, corners in index_rows:
                xs = [p[0] for p in corners]
                ys = [p[1] for p in corners]
                wr.writerow([name, file, c, r, w, h, f"{px:.6f}", f"{min(xs):.3f}", f"{min(ys):.3f}",
                             f"{max(xs):.3f}", f"{max(ys):.3f}"])
        self._index_dxf(os.path.join(folder, f"{base}_index.dxf"), index_rows, wkt)
        feedback.pushInfo(tr("{n} tiles of up to {w} × {h} pixels written to {f}.").format(n=n, w=tw, h=th,
                                                                                            f=folder))
        results = {self.OUTPUT: folder, "TILE_COUNT": n, "COLUMNS": cols, "ROWS": rows}
        if isink is not None:
            results[self.INDEX] = iid
        return results

    @staticmethod
    def _index_dxf(path, rows, wkt):
        from osgeo import ogr, osr

        if os.path.exists(path):
            os.remove(path)
        ds = ogr.GetDriverByName("DXF").CreateDataSource(path)
        srs = None
        if wkt:
            srs = osr.SpatialReference()
            srs.ImportFromWkt(wkt)
        lyr = ds.CreateLayer("entities", srs, ogr.wkbUnknown)
        for name, _file, _c, _r, _w, _h, px, corners in rows:
            ring = ogr.Geometry(ogr.wkbLineString)
            for x, y in corners + corners[:1]:
                ring.AddPoint_2D(x, y)
            f = ogr.Feature(lyr.GetLayerDefn())
            f.SetField("Layer", "TILES")
            f.SetGeometry(ring)
            lyr.CreateFeature(f)
            cx = sum(p[0] for p in corners) / 4
            cy = sum(p[1] for p in corners) / 4
            t = ogr.Feature(lyr.GetLayerDefn())
            t.SetField("Layer", "TILE_NAMES")
            size = max(abs(corners[1][0] - corners[0][0]) / 20, px)
            t.SetStyleString(f'LABEL(f:"Arial",t:"{name}",s:{size:.3f}g,p:5)')
            pt = ogr.Geometry(ogr.wkbPoint)
            pt.AddPoint_2D(cx, cy)
            t.SetGeometry(pt)
            lyr.CreateFeature(t)
        ds = None
