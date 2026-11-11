"""MicroStation DGN (Bentley) - cadastral maps in Vietnam, older utility and road drawings everywhere.

GDAL reads DGN version 7 (ISFF) natively; DGN version 8 (V8/V8i/CONNECT) needs the Open Design
Alliance libraries (GDAL's DGNv8 driver), which may not be redistributed in a free GPL installer.
So: v7 opens here; for v8 the file is recognised and the user is told the two free routes - save as
DXF or V7 DGN from MicroStation, or convert with the free ODA File Converter - instead of a vague
"cannot open"."""
from osgeo import ogr

from .common import Dataset, FormatError

ID = "dgn"
LABEL = "MicroStation DGN (v7)"
EXTENSIONS = (".dgn",)
COUNTRIES = ("VN", "US", "GB", "DE")

V8_HELP = ("This is a MicroStation V8 DGN. Its format needs the Open Design Alliance libraries, which "
           "cannot be included in a free program. Two free ways: in MicroStation use File › Save As › "
           "DWG/DXF (or V7 DGN); or convert it with the free ODA File Converter "
           "(opendesign.com/guestfiles/oda_file_converter) to DXF, then open the DXF in Biruni.")


def version(path):
    """7, 8 or None - from the first bytes (V8 files are OLE compound documents)."""
    with open(path, "rb") as f:
        head = f.read(8)
    if head.startswith(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"):
        return 8
    if len(head) >= 4 and (head[0] & 0x3F) in (8, 9) and head[1] in (0x09, 0x0B):
        return 7
    return None


_KIND = {ogr.wkbPoint: "point", ogr.wkbLineString: "line", ogr.wkbPolygon: "polygon",
         ogr.wkbMultiLineString: "line", ogr.wkbMultiPolygon: "polygon"}


def read(path):
    v = version(path)
    if v == 8:
        raise FormatError(V8_HELP)
    try:
        src = ogr.Open(path)
    except RuntimeError:  # GDAL exceptions enabled
        src = None
    if src is None or src.GetDriver().GetName() != "DGN":
        raise FormatError("GDAL could not open this DGN file.")
    ds = Dataset(ID)
    lyr = src.GetLayer(0)
    for f in lyr:
        g = f.GetGeometryRef()
        if g is None:
            continue
        kind = _KIND.get(ogr.GT_Flatten(g.GetGeometryType()), "line")
        level = f.GetField("Level")
        attrs = {"level": level, "color": f.GetField("ColorIndex"), "dgn_type": f.GetField("Type")}
        text = f.GetField("Text")
        if text:
            attrs["text"] = text
        ds.layer(f"level_{level}", kind).add(g.ExportToIsoWkt(), **attrs)
    if not ds.count():
        raise FormatError("The DGN file contains no elements.")
    return ds
