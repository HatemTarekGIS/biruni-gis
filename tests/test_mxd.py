"""Tests for opening ArcMap documents (biruni.arcgis.mxd).

The fixture is a small .mxd written here: an OLE compound file whose "Maps"
stream holds the objects the way ArcMap 10.x persists them (data frame, group,
feature, raster, XY event, basemap and annotation layers, and references).

Run:  engine\\bin\\python-qgis.bat tests\\test_mxd.py
"""
import glob
import os
import struct
import sys
import tempfile
import uuid

from qgis.core import QgsApplication, QgsProject

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "app", "plugins"))

app = QgsApplication([], False)
app.initQgis()
from osgeo import gdal, ogr, osr  # noqa: E402

from biruni.arcgis import list_maps, open_arcgis_file  # noqa: E402
from biruni.arcgis.mxd import MxdError, package_candidates, read_mxd  # noqa: E402
from biruni.arcgis.reader import ArcGISError  # noqa: E402

failures = []


def check(cond, msg):
    print(("PASS " if cond else "FAIL ") + msg)
    if not cond:
        failures.append(msg)


tmp = tempfile.mkdtemp(prefix="biruni_mxd_")

# ---- data the document points to ---------------------------------------------
srs = osr.SpatialReference()
srs.ImportFromEPSG(32637)
os.makedirs(os.path.join(tmp, "data"))
ds = ogr.GetDriverByName("OpenFileGDB").CreateDataSource(os.path.join(tmp, "data", "city.gdb"))
for name, gtype, wkt in (("parcels", ogr.wkbPolygon, "POLYGON((500000 2400000,500100 2400000,500100 2400100,"
                                                      "500000 2400100,500000 2400000))"),
                         ("roads", ogr.wkbLineString, "LINESTRING(500000 2399900,501000 2399900)")):
    lyr = ds.CreateLayer(name, srs, gtype)
    lyr.CreateField(ogr.FieldDefn("kind", ogr.OFTString))
    for k in ("A", "B", "B"):
        f = ogr.Feature(lyr.GetLayerDefn())
        f.SetField("kind", k)
        f.SetGeometry(ogr.CreateGeometryFromWkt(wkt))
        lyr.CreateFeature(f)
ds = None
shp = ogr.GetDriverByName("ESRI Shapefile").CreateDataSource(os.path.join(tmp, "data", "wells.shp"))
lyr = shp.CreateLayer("wells", srs, ogr.wkbPoint)
f = ogr.Feature(lyr.GetLayerDefn())
f.SetGeometry(ogr.CreateGeometryFromWkt("POINT(500010 2400010)"))
lyr.CreateFeature(f)
shp = None
r = gdal.GetDriverByName("GTiff").Create(os.path.join(tmp, "data", "dem.tif"), 4, 4, 1, gdal.GDT_Byte)
r.SetGeoTransform((500000, 25, 0, 2400100, 0, -25))
r.SetProjection(srs.ExportToWkt())
r = None


# ---- writing an .mxd ----------------------------------------------------------
def g(s):
    return uuid.UUID(s).bytes_le


MAP, GROUP = g("E6BDAA76-4D35-11D0-98BE-00805F7CED21"), g("EDAD6647-1810-11D1-86AE-0000F8751720")
FEATURE, RASTER = g("E663A651-8AAD-11D0-BEC7-00805F7C4268"), g("D02371C9-35F7-11D2-B1F2-00C04F8EDEFF")
ANNO, BASEMAP = g("CE8F3972-E9BE-11D1-A232-080009B6F22B"), g("DA4122BF-7B07-4158-88B0-19D342BED8BA")
WS_NAME, PROPSET = g("5A350011-E371-11D1-AA82-00C04FA33A15"), g("588E5A11-D09B-11D1-AA7C-00C04FA33A15")
FGDB, SHAPEFILE = g("71FE75F0-EA0C-4406-873E-B7D53748AE7E"), g("A06ADB96-D95C-11D1-AA81-00C04FA33A15")
FC_NAME = g("75E10086-4226-42AC-AFEC-CBB9B748F847")
XY_SOURCE, TABLE_NAME = g("309AA920-EAEC-11D3-9F8A-00C04F6BDF06"), g("06783DB1-E5EE-11D1-B0A2-0000F8780820")
WKT = ('PROJCS["WGS_1984_UTM_Zone_37N",GEOGCS["GCS_WGS_1984",DATUM["D_WGS_1984",SPHEROID["WGS_1984",6378137.0,'
       '298.257223563]],PRIMEM["Greenwich",0.0],UNIT["Degree",0.0174532925199433]],PROJECTION["Transverse_Mercator"],'
       'PARAMETER["False_Easting",500000.0],PARAMETER["False_Northing",0.0],PARAMETER["Central_Meridian",39.0],'
       'PARAMETER["Scale_Factor",0.9996],PARAMETER["Latitude_Of_Origin",0.0],UNIT["Meter",1.0]]')
_ids = iter(range(30, 10000))


def bstr(s):
    raw = s.encode("utf-16-le") + b"\0\0"
    return struct.pack("<I", len(raw)) + raw


def obj(cls, gap=2, oid=None):
    """class id + object id + version (gap bytes after the id)."""
    return cls + struct.pack("<I", next(_ids) if oid is None else oid) + b"\x22\x00" + b"\0" * (gap - 2)


def flag(v):
    return b"\xff\xff" if v else b"\0\0"


def data_frame(name):
    return (obj(MAP) + bstr(name) + bstr("") + b"\0" + g("D22A6267-B21D-51BF-0800-2022F5730000") +
            struct.pack("<IH", next(_ids), 6) + struct.pack("<I", len(WKT)) + WKT.encode() + b"\0\0\0" + b"\0" * 40)


def group(name, visible, children):
    return (obj(GROUP) + bstr(name) + flag(visible) + b"\xff\xff\0\0" + struct.pack("<dd", 0, 0) +
            struct.pack("<I", children) + b"\0" * 24)


def feature(name, visible, dataset, ws, factory, dtype, query=""):
    return (obj(FEATURE) + bstr(name) + bstr(dtype) + flag(visible) + b"\0" * 4 +
            obj(FC_NAME) + b"\x01\x00\x00\x00\x02\x00" + bstr(dataset) + b"\x02\0\0\0\0\0" + bstr(dtype) +
            bstr("Shape") + b"\x03\0\0\0\x01\0\0\0\0\0\x11\0" +
            obj(WS_NAME) + bstr(ws) + b"\x02\0\0\0\0\0" + bstr(os.path.splitext(os.path.basename(ws))[0]) +
            obj(PROPSET) + b"\x01\0\x01\0\0\0" + bstr("DATABASE") + b"\x08\0" + bstr(ws) + b"\x01" + factory +
            b"\0" * 30 + bstr("[kind]") + bstr("Default") + b"\0" * 20 + (bstr(query) if query else b"") +
            b"\0" * 60)


def raster(name, visible, folder, dataset):
    return (obj(RASTER, gap=4) + bstr(name) + bstr(".") + flag(visible) + b"\0" * 20 +
            bstr(f"RASTER: Workspace = {folder}; RasterDataset = {dataset};") + bstr("Raster Dataset") + b"\0" * 40)


def xy_layer(name, table):
    return (obj(FEATURE) + bstr(name) + bstr("") + flag(True) + b"\0" * 8 + obj(XY_SOURCE) + bstr(name + "_Features") +
            obj(TABLE_NAME) + bstr(table) + b"\0" * 40)


maps_stream = b"".join([
    data_frame("Layers"),
    group("Base data", True, 3),
    feature("Parcels (type B)", True, "parcels", r"data\city.gdb", FGDB, "File Geodatabase Feature Class",
            query="\"kind\" = 'B'"),
    group("Inner", False, 1),
    feature("Roads", False, "roads", r"data\city.gdb", FGDB, "File Geodatabase Feature Class"),
    feature("Wells", True, "wells", "data", SHAPEFILE, "Shapefile Feature Class"),
    raster("Elevation", False, "data", "dem.tif"),
    xy_layer("Survey points", "points.csv"),
    obj(ANNO, gap=8) + bstr("Labels") + b"\0" * 30,
    obj(BASEMAP) + bstr("Basemap") + flag(True) + b"\0" * 30,
    data_frame("Inset"),
    feature("Wells again", True, "wells", "data", SHAPEFILE, "Shapefile Feature Class"),
    obj(MAP, oid=30) + b"\0" * 30,                       # the layout refers to the first data frame
    feature("Overview only", True, "wells", "data", SHAPEFILE, "Shapefile Feature Class"),
])
maps_stream += b"\0" * max(0, 4096 - len(maps_stream))   # a regular (not mini) stream


def compound_file(streams):
    """Minimal OLE compound file (version 3, 512-byte sectors) with top-level streams."""
    ss, end, free, fat_sect = 512, 0xFFFFFFFE, 0xFFFFFFFF, 0xFFFFFFFD
    sectors, fat = [], []

    def put(data):
        start = len(sectors)
        chunks = [data[i:i + ss].ljust(ss, b"\0") for i in range(0, len(data), ss)] or [b"\0" * ss]
        for k, c in enumerate(chunks):
            sectors.append(c)
            fat.append(start + k + 1 if k < len(chunks) - 1 else end)
        return start

    big = {n: d for n, d in streams.items() if len(d) >= 4096}
    small = {n: d for n, d in streams.items() if len(d) < 4096}
    starts, mini, minifat = {}, b"", []
    for n, d in small.items():
        first = len(mini) // 64
        blocks = (len(d) + 63) // 64
        mini += d.ljust(blocks * 64, b"\0")
        minifat += [first + k + 1 if k < blocks - 1 else end for k in range(blocks)]
        starts[n] = first
    for n, d in big.items():
        starts[n] = put(d)
    mini_start = put(mini) if mini else end
    minifat_start = put(b"".join(struct.pack("<I", v) for v in minifat).ljust(ss, b"\xff")) if minifat else end
    names = list(streams)
    entries = []

    def entry(name, typ, left, right, child, start, size):
        raw = name.encode("utf-16-le") + b"\0\0"
        return (raw.ljust(64, b"\0") + struct.pack("<HBB3I", len(raw), typ, 1, left, right, child) + b"\0" * 36 +
                struct.pack("<IQ", start, size))
    entries.append(entry("Root Entry", 5, free, free, 1, mini_start, len(mini)))
    for k, n in enumerate(names):   # a right-leaning chain is a valid red-black-free tree for reading
        entries.append(entry(n, 2, free, k + 2 if k + 1 < len(names) else free, free, starts[n], len(streams[n])))
    dir_start = put(b"".join(entries))
    n_fat = 1
    while (len(sectors) + n_fat) > n_fat * (ss // 4):
        n_fat += 1
    fat_start = len(sectors)
    fat += [fat_sect] * n_fat
    fat += [free] * (n_fat * (ss // 4) - len(fat))
    for k in range(n_fat):
        sectors.append(b"".join(struct.pack("<I", v) for v in fat[k * 128:(k + 1) * 128]))
    difat = [fat_start + k for k in range(n_fat)] + [free] * (109 - n_fat)
    header = (bytes.fromhex("D0CF11E0A1B11AE1") + b"\0" * 16 + struct.pack("<HHHHH", 0x3E, 3, 0xFFFE, 9, 6) +
              b"\0" * 6 + struct.pack("<9I", 0, n_fat, dir_start, 0, 4096, minifat_start, 1 if minifat else 0,
                                      end, 0) + struct.pack("<109I", *difat))
    return header + b"".join(sectors)


mxd_path = os.path.join(tmp, "city.mxd")
with open(mxd_path, "wb") as fh:
    fh.write(compound_file({"Maps": maps_stream, "Version": bstr("10.8")}))

# ---- reading --------------------------------------------------------------------
frames = read_mxd(mxd_path)
check([f.name for f in frames] == ["Layers", "Inset"], "data frames read in order")
check("UTM_Zone_37N" in frames[0].wkt, "data frame coordinate system read")
top = frames[0].layers
check([lyr.name for lyr in top] == ["Base data", "Elevation", "Survey points", "Labels", "Basemap"],
      "top-level layers in table-of-contents order")
base = top[0]
check(base.kind == "group" and [c.name for c in base.children] == ["Parcels (type B)", "Inner", "Wells"],
      "group children counted from the group record")
check([c.name for c in base.children[1].children] == ["Roads"] and not base.children[1].visible,
      "nested hidden group")
parcels = base.children[0]
check((parcels.factory, parcels.workspace, parcels.dataset) == ("FileGDB", r"data\city.gdb", "parcels"),
      "file geodatabase source: factory, workspace and dataset")
check(parcels.definition_query == "\"kind\" = 'B'", "definition query found (label expression ignored)")
check(base.children[2].factory == "Shapefile" and base.children[2].dataset == "wells", "shapefile source")
check(not base.children[1].children[0].visible and parcels.visible, "layer visibility")
elev = top[1]
check((elev.kind, elev.factory, elev.workspace, elev.dataset, elev.visible) ==
      ("raster", "Raster", "data", "dem.tif", False), "raster layer source and visibility")
check(top[2].factory == "XYEvent" and top[2].dataset == "points.csv", "XY event layer recognised")
check([lyr.name for lyr in frames[1].layers] == ["Wells again"], "layers after a layout reference are not added")
try:
    read_mxd(os.path.join(tmp, "data", "dem.tif"))
    check(False, "a non-mxd file is refused")
except MxdError:
    check(True, "a non-mxd file is refused")

# ---- packaged data (.mpk unpacked: the document keeps the original paths) -------
pkg = os.path.join(tmp, "pkg")
os.makedirs(os.path.join(pkg, "v108", "new_personal_geodatabase.gdb"))
os.makedirs(os.path.join(pkg, "commondata", "raster_data1"))
cands = package_candidates(r"E:\maps\New Personal Geodatabase.mdb", "Access", os.path.join(pkg, "v108"))
check(cands and cands[0][1] == "FileGDB" and cands[0][0].endswith("new_personal_geodatabase.gdb"),
      "packaged personal geodatabase found as a file geodatabase")
cands = package_candidates("E:\\maps\\INDEX\\", "Raster", os.path.join(pkg, "v108"))
check(any(p.endswith("raster_data1") for p, _f in cands), "packaged raster folder found under commondata")

# ---- opening into a project -------------------------------------------------------
check(list_maps(mxd_path) == ["Layers", "Inset"], "list_maps lists the data frames")
project = QgsProject()
rep = open_arcgis_file(mxd_path, project, "Layers")
added = [n for n, _k in rep.added]
check(added == ["Parcels (type B)", "Roads", "Wells", "Elevation"], f"layers added: {added}")
by = {lyr.name(): lyr for lyr in rep.layers}
check(by["Parcels (type B)"].featureCount() == 2, "definition query applied as a filter")
root = project.layerTreeRoot()
grp = root.findGroup("Base data")
check(grp is not None and grp.findGroup("Inner") is not None and not grp.findGroup("Inner").isVisible(),
      "groups rebuilt with their visibility")
check(not root.findLayer(by["Elevation"].id()).isVisible(), "hidden layer stays hidden")
skipped = dict(rep.skipped)
check(set(skipped) == {"Survey points", "Labels", "Basemap"} and all(skipped.values()),
      "XY event, annotation and basemap layers skipped with a reason")
check(any("default styles" in w for w in rep.warnings), "the missing symbology is said once")
check(rep.crs is not None and rep.crs.isValid() and project.crs().isValid(), "project takes the data frame CRS")

broken = os.path.join(tmp, "broken.mxd")
with open(broken, "wb") as fh:
    fh.write(b"not an mxd at all" * 40)
try:
    open_arcgis_file(broken, QgsProject())
    check(False, "a damaged .mxd raises ArcGISError")
except ArcGISError as e:
    check("ArcMap" in str(e), "a damaged .mxd raises ArcGISError with advice")

# ---- real documents on this machine (optional) ------------------------------------
real = sorted(glob.glob(os.path.expanduser(r"~\Documents\**\*.mxd"), recursive=True))[:40]
if real:
    added = errors = 0
    for path in real:
        try:
            rep = open_arcgis_file(path, QgsProject(), "*", services=False)
            added += len(rep.added)
            check(all(why for _n, why in rep.skipped), f"every skip has a reason: {os.path.basename(path)}")
        except Exception as e:  # noqa: BLE001
            errors += 1
            print("   ERROR", path, e)
    print(f"   real files: {len(real)}, {added} layers added")
    check(errors == 0, f"all {len(real)} real ArcMap documents open without errors")
else:
    print("SKIP no ArcMap documents on this machine")

print()
print("ALL PASSED" if not failures else f"{len(failures)} FAILED")
sys.exit(1 if failures else 0)
