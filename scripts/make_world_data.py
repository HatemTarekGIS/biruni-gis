r"""Build the bundled world dataset (resources/data/world.gpkg) from Natural Earth
(public domain): countries, cities, rivers and lakes. Used for the sample
project, the "where do these points fall" check and boundaries by country.

usage: engine\bin\python-qgis.bat scripts\make_world_data.py
"""
import os
import urllib.request

from osgeo import gdal, ogr

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
CACHE = os.path.join(ROOT, "_downloads", "naturalearth")
OUT = os.path.join(ROOT, "app", "plugins", "biruni", "resources", "data", "world.gpkg")
BASE = "https://naciscdn.org/naturalearth"
LAYERS = [
    ("countries", "50m/cultural/ne_50m_admin_0_countries",
     ["NAME", "NAME_LONG", "NAME_AR", "ISO_A2", "ISO_A3", "ADM0_A3", "CONTINENT", "REGION_UN", "SUBREGION",
      "POP_EST", "GDP_MD"], None),
    ("cities", "10m/cultural/ne_10m_populated_places_simple",
     ["name", "nameascii", "adm0name", "adm1name", "iso_a2", "pop_max", "featurecla", "scalerank"],
     "scalerank <= 6"),
    ("rivers", "50m/physical/ne_50m_rivers_lake_centerlines", ["name", "name_en", "featurecla", "scalerank"], None),
    ("lakes", "50m/physical/ne_50m_lakes", ["name", "name_en", "featurecla", "scalerank"], None),
]

os.makedirs(CACHE, exist_ok=True)
os.makedirs(os.path.dirname(OUT), exist_ok=True)
if os.path.exists(OUT):
    os.remove(OUT)
gdal.UseExceptions()
for name, path, fields, where in LAYERS:
    zip_path = os.path.join(CACHE, os.path.basename(path) + ".zip")
    if not os.path.exists(zip_path):
        print("download", path)
        req = urllib.request.Request(f"{BASE}/{path}.zip", headers={"User-Agent": "BiruniGIS-build"})
        with urllib.request.urlopen(req, timeout=120) as r, open(zip_path, "wb") as f:
            f.write(r.read())
    src = f"/vsizip/{zip_path}/{os.path.basename(path)}.shp"
    ds = ogr.Open(src)
    have = [ds.GetLayer(0).GetLayerDefn().GetFieldDefn(i).GetName() for i in range(
        ds.GetLayer(0).GetLayerDefn().GetFieldCount())]
    keep = [f for f in fields if f in have or f.upper() in have]
    sel = ", ".join(f'"{f if f in have else f.upper()}" AS "{f.lower()}"' for f in keep)
    sql = f'SELECT {sel} FROM "{os.path.basename(path)}"' + (f" WHERE {where}" if where else "")
    out = gdal.VectorTranslate(OUT, src, format="GPKG", accessMode="update" if os.path.exists(OUT) else None,
                         layerName=name, SQLStatement=sql, SQLDialect="OGRSQL", dstSRS="EPSG:4326",
                         layerCreationOptions=["SPATIAL_INDEX=YES"], makeValid=True,
                         geometryType="PROMOTE_TO_MULTI" if name != "cities" else None)
    out = None  # close and flush
    check = ogr.Open(OUT)
    print(name, check.GetLayerByName(name).GetFeatureCount())
    check = None
print(OUT, round(os.path.getsize(OUT) / 1e6, 1), "MB")

# sample spreadsheet for the "points from Excel" lesson: Arabic headers, DMS
# coordinates, decimal commas - the things the smart import must understand
import openpyxl  # noqa: E402

XLSX = os.path.join(os.path.dirname(OUT), "sample_points.xlsx")
wb = openpyxl.Workbook()
ws = wb.active
ws.title = "Sites"
ws.append(["رقم الموقع", "الاسم", "خط العرض", "خط الطول", "النوع", "Name"])


def dms(v, pos, neg):
    h = pos if v >= 0 else neg
    v = abs(v)
    d = int(v)
    m = int((v - d) * 60)
    s = (v - d - m / 60) * 3600
    return f"{d}°{m:02d}'{s:04.1f}\"{h}"


ds = ogr.Open(OUT)
lyr = ds.GetLayerByName("cities")
lyr.SetAttributeFilter("adm0name IN ('Egypt', 'Saudi Arabia', 'United Arab Emirates', 'Jordan', 'Morocco')")
k = 0
for f in lyr:
    g = f.GetGeometryRef()
    k += 1
    kind = "عاصمة" if f.GetField("featurecla") and "capital" in f.GetField("featurecla").lower() else "مدينة"
    ws.append([k, f.GetField("name"), dms(g.GetY(), "N", "S"), dms(g.GetX(), "E", "W"), kind, f.GetField("name")])
ds = None
wb.save(XLSX)
print(XLSX, k, "rows")
