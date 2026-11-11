"""Panorama SXF (Russia, CIS) - the exchange format of GIS Panorama and the Russian topographic and
cadastral services. Read through GDAL's SXF driver; the RSC classifier next to the file (same name, or
any single .rsc in the folder, or one the user names) turns object codes into named layers and
attribute names. Text (Cyrillic) comes back as Unicode."""
import glob
import os

from osgeo import gdal, ogr

from .common import Dataset, FormatError

ID = "sxf"
LABEL = "Panorama SXF (Russia)"
EXTENSIONS = (".sxf",)
COUNTRIES = ("RU", "BY", "KZ", "UA")

_KIND = {ogr.wkbPoint: "point", ogr.wkbMultiPoint: "point", ogr.wkbLineString: "line",
         ogr.wkbMultiLineString: "line", ogr.wkbPolygon: "polygon", ogr.wkbMultiPolygon: "polygon"}


def find_rsc(path):
    stem = os.path.splitext(path)[0]
    for cand in (stem + ".rsc", stem + ".RSC"):
        if os.path.exists(cand):
            return cand
    found = glob.glob(os.path.join(os.path.dirname(path) or ".", "*.rsc"))
    return found[0] if len(found) == 1 else None


def read(path, rsc=None):
    rsc = rsc or find_rsc(path)
    opts = ["SXF_LAYER_FULLNAME=YES", "SXF_SET_VERTCS=YES"]
    if rsc:
        opts.append(f"SXF_RSC_FILENAME={rsc}")
    try:
        src = gdal.OpenEx(path, gdal.OF_VECTOR, allowed_drivers=["SXF"], open_options=opts)
    except RuntimeError:  # GDAL raises when its exceptions are enabled (as inside QGIS)
        src = None
    if src is None:
        raise FormatError("GDAL could not open this SXF file (damaged, or not SXF version 3/4).")
    srs = None
    ds = Dataset(ID)
    if not rsc:
        ds.notes.append("No .rsc classifier was found next to the file: layers are grouped by object code "
                        "only. Put the matching .rsc (same name) beside the .sxf for named layers.")
    for i in range(src.GetLayerCount()):
        lyr = src.GetLayer(i)
        if srs is None and lyr.GetSpatialRef() is not None:
            srs = lyr.GetSpatialRef().ExportToWkt()
        defn = lyr.GetLayerDefn()
        names = [defn.GetFieldDefn(k).GetName() for k in range(defn.GetFieldCount())]
        for f in lyr:
            g = f.GetGeometryRef()
            kind = _KIND.get(ogr.GT_Flatten(g.GetGeometryType()), "point") if g else "none"
            if kind == "point" and g is not None and ogr.GT_Flatten(g.GetGeometryType()) == ogr.wkbMultiPoint:
                g = g.GetGeometryRef(0)
            attrs = {n: f.GetField(n) for n in names if f.GetField(n) is not None}
            ds.layer(lyr.GetName(), kind).add(g.ExportToIsoWkt() if g is not None else None, **attrs)
    ds.crs = srs
    if not ds.count():
        raise FormatError("The SXF file contains no objects.")
    return ds
