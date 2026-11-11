"""Cleaning what leaves the office (scrub/): dirty samples of each format, cleaned, still the same data.

Run:  engine\\bin\\python-qgis.bat tests\\test_scrub.py
"""
import csv
import os
import re
import shutil
import sqlite3
import sys
import tempfile
import zipfile

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "app", "plugins"))

from qgis.core import (QgsApplication, QgsEditorWidgetSetup, QgsFeature, QgsField, QgsGeometry,  # noqa: E402
                       QgsProcessingContext, QgsProcessingFeedback, QgsProject, QgsVectorLayer)
from qgis.PyQt.QtCore import QMetaType  # noqa: E402

app = QgsApplication([], False)
app.initQgis()
sys.path.append(QgsApplication.prefixPath() + "/python/plugins")
import processing  # noqa: E402
from processing.core.Processing import Processing  # noqa: E402

Processing.initialize()
from osgeo import gdal, ogr  # noqa: E402

from biruni import scrub  # noqa: E402
from biruni.algorithms import BiruniProvider  # noqa: E402
from biruni.scrub import core  # noqa: E402

provider = BiruniProvider()
QgsApplication.processingRegistry().addProvider(provider)
gdal.UseExceptions()
failures = []
WHO = "FakeUserQ"
HOME = rf"C:\Users\{WHO}\Documents\Projects\Plan 12"
OPT = {"names": [WHO]}


def check(cond, msg):
    print(("PASS " if cond else "FAIL ") + msg)
    if not cond:
        failures.append(msg)


def blob(path):
    with open(path, "rb") as f:
        return f.read()


def dirty(data):
    return WHO.lower().encode() in data.lower() or b"Users\\" + WHO.encode() in data


def feats(path, layer=None):
    ds = ogr.Open(path)
    lyr = ds.GetLayerByName(layer) if layer else ds.GetLayer(0)
    out = []
    for f in lyr:
        g = f.GetGeometryRef()
        out.append((g.ExportToWkt() if g else None,
                    {lyr.GetLayerDefn().GetFieldDefn(i).GetName(): f.GetField(i) for i in range(f.GetFieldCount())}))
    return out


tmp = tempfile.mkdtemp(prefix="scrub_")

# ---- GeoPackage ------------------------------------------------------------------------------------------------
gp = os.path.join(tmp, "plan.gpkg")
ds = ogr.GetDriverByName("GPKG").CreateDataSource(gp)
lyr = ds.CreateLayer("parcels", geom_type=ogr.wkbPolygon)
for n, t in (("name", ogr.OFTString), ("secret", ogr.OFTString), ("photo", ogr.OFTString)):
    lyr.CreateField(ogr.FieldDefn(n, t))
for i in range(300):
    f = ogr.Feature(lyr.GetLayerDefn())
    f.SetField("name", f"قطعة {i}")
    f.SetField("secret", "0555")
    f.SetField("photo", HOME + rf"\p{i}.jpg" if i == 0 else "")
    f.SetGeometry(ogr.CreateGeometryFromWkt(f"POLYGON(({i} 0,{i + 1} 0,{i + 1} 1,{i} 0))"))
    lyr.CreateFeature(f)
ds = None
con = sqlite3.connect(gp)
con.execute("CREATE TABLE IF NOT EXISTS gpkg_metadata (id INTEGER PRIMARY KEY, md_scope TEXT, md_standard_uri TEXT, "
            "mime_type TEXT, metadata TEXT)")
con.execute("INSERT INTO gpkg_metadata(md_scope, md_standard_uri, mime_type, metadata) VALUES ('dataset','x','text/xml',?)",
            (f"<author>{WHO}</author><path>{HOME}\\plan.qgz</path>",))
con.execute("CREATE TABLE layer_styles (id INTEGER PRIMARY KEY, f_table_name TEXT, styleName TEXT, styleQML TEXT, "
            "styleSLD TEXT, owner TEXT, ui TEXT, description TEXT)")
con.execute("INSERT INTO layer_styles(f_table_name, styleName, styleQML, owner) VALUES ('parcels','s',?,?)",
            (f"<qgis><svg path='{HOME}\\icons\\a.svg'/></qgis>", WHO))
con.execute("CREATE TABLE qgis_projects (name TEXT, metadata TEXT, content BLOB)")
con.execute("INSERT INTO qgis_projects VALUES ('plan', ?, ?)", (WHO, HOME.encode()))
con.execute("CREATE TABLE junk (x TEXT)")
con.executemany("INSERT INTO junk VALUES (?)", [(WHO * 50,)] * 2000)
con.commit()
con.execute("DELETE FROM junk")
con.commit()
con.close()
before = feats(gp, "parcels")
check(dirty(blob(gp)), "the dirty GeoPackage carries the user name (deleted rows still inside)")
out = os.path.join(tmp, "plan_clean.gpkg")
rc = core.scrub_file(gp, out, dict(OPT, drop_fields={"parcels": ["secret"]}, drop_tables=["junk"]))
print(rc.as_text())
after = feats(out, "parcels")
check(not dirty(blob(out).replace(("photo").encode(), b"")) or b"p0.jpg" in blob(out),
      "GeoPackage: no user name left outside the attribute values")
con = sqlite3.connect(out)
tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
check(con.execute("SELECT COUNT(*) FROM gpkg_metadata").fetchone()[0] == 0 and "qgis_projects" not in tables
      and "junk" not in tables, "GeoPackage: metadata rows, the embedded project and the extra table are gone")
row = con.execute("SELECT owner, styleQML FROM layer_styles").fetchone()
check(row[0] == "" and "Users" not in row[1] and "a.svg" in row[1], "GeoPackage: the style keeps its icon name, not the folder or owner")
con.close()
check(len(after) == len(before) and all(a[0] == b[0] for a, b in zip(after, before))
      and all(a[1]["name"] == b[1]["name"] for a, b in zip(after, before)), "GeoPackage: same features, geometry and names")
check("secret" not in after[0][1], "GeoPackage: the hidden field is gone")
check(after[0][1]["photo"] == before[0][1]["photo"] and any("look like paths" in w["text"] for w in rc.warnings),
      "GeoPackage: a path inside the data is reported, not changed")
check({"metadata rows", "embedded QGIS projects", "hidden fields"} <= rc.kinds()
      and any(k.startswith("deleted pages") for k in rc.kinds()), "GeoPackage: the receipt lists what was removed")
check(not any(WHO in i["preview"] for i in rc.items if i["kind"] == "user names"), "the receipt does not repeat user names")

# ---- Shapefile -------------------------------------------------------------------------------------------------
sp = os.path.join(tmp, "roads.shp")
ds = ogr.GetDriverByName("ESRI Shapefile").CreateDataSource(sp)
lyr = ds.CreateLayer("roads", geom_type=ogr.wkbLineString)
lyr.CreateField(ogr.FieldDefn("name", ogr.OFTString))
lyr.CreateField(ogr.FieldDefn("secret", ogr.OFTString))
for i in range(5):
    f = ogr.Feature(lyr.GetLayerDefn())
    f.SetField("name", f"r{i}")
    f.SetField("secret", "x")
    f.SetGeometry(ogr.CreateGeometryFromWkt(f"LINESTRING({i} 0,{i} 5)"))
    lyr.CreateFeature(f)
ds = None
with open(sp + ".xml", "w", encoding="utf-8") as fh:
    fh.write(f"<metadata><CreaDate/><mdContact>{WHO}</mdContact><path>{HOME}</path></metadata>")
with open(os.path.join(tmp, "roads.qmd"), "w", encoding="utf-8") as fh:
    fh.write(f"<qgis><author>{WHO}</author></qgis>")
before = feats(sp)
rc = core.scrub_file(sp, None, dict(OPT, drop_fields={"roads": ["secret"]}))
print(rc.as_text())
left = os.listdir(tmp)
check("roads.shp.xml" not in left and "roads.qmd" not in left, "Shapefile: the metadata files beside it are gone")
after = feats(sp)
check(len(after) == 5 and [a[0] for a in after] == [b[0] for b in before] and "secret" not in after[0][1]
      and after[0][1]["name"] == "r0", "Shapefile: same features, hidden field gone")

# ---- DXF -------------------------------------------------------------------------------------------------------
dx = os.path.join(tmp, "plan.dxf")
NL = "\r\n"
pairs = ["0", "SECTION", "2", "HEADER", "9", "$ACADVER", "1", "AC1015", "9", "$LASTSAVEDBY", "1", WHO,
         "9", "$FINGERPRINTGUID", "2", "{11111111-2222-3333-4444-555555555555}", "9", "$HYPERLINKBASE", "1", HOME,
         "0", "ENDSEC", "0", "SECTION", "2", "ENTITIES",
         "0", "LINE", "8", "0", "10", "0.0", "20", "0.0", "11", "10.0", "21", "5.0",
         "1001", "ACAD", "1000", HOME + r"\xref\base.dwg",
         "0", "TEXT", "8", "0", "10", "1.0", "20", "1.0", "40", "1.0", "1", "قطعة 12",
         "0", "ENDSEC", "0", "EOF"]
with open(dx, "w", encoding="utf-8", newline="") as fh:
    fh.write(NL.join(pairs) + NL)
before = feats(dx)
rc = core.scrub_file(dx, None, OPT)
print(rc.as_text())
check(not dirty(blob(dx)) and b"base.dwg" in blob(dx), "DXF: no user name or folder left; the file name stays")
check(b"{11111111-2222" not in blob(dx), "DXF: the drawing's tracking id is new")
after = feats(dx)
check(len(after) == len(before) and [a[0] for a in after] == [b[0] for b in before], "DXF: same entities and geometry")

# ---- KML / KMZ -------------------------------------------------------------------------------------------------
KML = f"""<?xml version="1.0" encoding="UTF-8"?>
<kml xmlns="http://www.opengis.net/kml/2.2" xmlns:atom="http://www.w3.org/2005/Atom"><Document>
<atom:author><atom:name>{WHO}</atom:name></atom:author>
<Style id="s"><IconStyle><Icon><href>{HOME}\\icons\\pin.png</href></Icon></IconStyle></Style>
<Placemark><name>قطعة 1</name><ExtendedData><SchemaData schemaUrl="#t"><SimpleData name="secret">0555</SimpleData>
<SimpleData name="area">500</SimpleData></SchemaData></ExtendedData><Point><coordinates>46.7,24.7</coordinates></Point></Placemark>
</Document></kml>"""
kp = os.path.join(tmp, "plan.kml")
with open(kp, "w", encoding="utf-8") as fh:
    fh.write(KML)
rc = core.scrub_file(kp, None, dict(OPT, drop_fields={"*": ["secret"]}))
t = open(kp, encoding="utf-8").read()
check(WHO not in t and "Users" not in t and "pin.png" in t and "0555" not in t and ">500<" in t,
      "KML: author, folders and the hidden field gone; icon name and other data stay")
check(len(feats(kp)) == 1, "KML: still opens with its placemark")
kz = os.path.join(tmp, "plan.kmz")
with zipfile.ZipFile(kz, "w") as z:
    z.writestr("doc.kml", KML)
    z.writestr("files/pin.png", b"PNG")
rc = core.scrub_file(kz, None, OPT)
with zipfile.ZipFile(kz) as z:
    t = z.read("doc.kml").decode("utf-8")
    check(WHO not in t and "files/pin.png" in z.namelist(), "KMZ: the KML inside is clean, the icon stays")
    check(all(i.date_time == (1980, 1, 1, 0, 0, 0) for i in z.infolist()), "KMZ: the file dates inside are neutral")

# ---- CSV -------------------------------------------------------------------------------------------------------
cp = os.path.join(tmp, "table.csv")
with open(cp, "w", encoding="utf-8", newline="") as fh:
    csv.writer(fh).writerows([["name", "secret", "area"], ["a", "1", "10"], ["b", "2", "20"]])
rc = core.scrub_file(cp, None, dict(OPT, drop_fields={"*": ["secret"]}))
rows = list(csv.reader(open(cp, encoding="utf-8")))
check(rows == [["name", "area"], ["a", "10"], ["b", "20"]], "CSV: the hidden column is gone, the rest unchanged")

# ---- XLSX ------------------------------------------------------------------------------------------------------
xp = os.path.join(tmp, "table.xlsx")
ds = ogr.GetDriverByName("XLSX").CreateDataSource(xp)
lyr = ds.CreateLayer("t", geom_type=ogr.wkbNone)
lyr.CreateField(ogr.FieldDefn("name", ogr.OFTString))
for v in ("a", "b"):
    f = ogr.Feature(lyr.GetLayerDefn())
    f.SetField("name", v)
    lyr.CreateFeature(f)
ds = None
dirty_x = xp + ".tmp"
with zipfile.ZipFile(xp) as zin, zipfile.ZipFile(dirty_x, "w", zipfile.ZIP_DEFLATED) as zout:
    names = zin.namelist()
    for n in names:
        d = zin.read(n)
        if n == "xl/workbook.xml":
            d = d.replace(b"<sheets>", ("<x15ac:absPath xmlns:x15ac=\"http://schemas.microsoft.com/office/spreadsheetml/2010/11/ac\" "
                                         f"url=\"{HOME}\\\"/><sheets>").encode())
        zout.writestr(n, d)
    zout.writestr("docProps/core.xml", f'<?xml version="1.0"?><cp:coreProperties xmlns:cp="x" xmlns:dc="y">'
                  f"<dc:creator>{WHO}</dc:creator><cp:lastModifiedBy>{WHO}</cp:lastModifiedBy></cp:coreProperties>")
    zout.writestr("docProps/app.xml", '<?xml version="1.0"?><Properties><Company>Office of ' + WHO + "</Company></Properties>")
os.replace(dirty_x, xp)
before = feats(xp)
rc = core.scrub_file(xp, None, OPT)
print(rc.as_text())
allx = b"".join(zipfile.ZipFile(xp).read(n) for n in zipfile.ZipFile(xp).namelist())
check(WHO.encode() not in allx and b"absPath" not in allx, "XLSX: author, company and the saved-in folder are gone")
check(feats(xp) == before, "XLSX: still opens with the same rows")

# ---- PDF -------------------------------------------------------------------------------------------------------
xmp = (f'<x:xmpmeta xmlns:x="adobe:ns:meta/"><rdf:RDF><rdf:Description><dc:creator><rdf:Seq><rdf:li>{WHO}</rdf:li>'
       f"</rdf:Seq></dc:creator><xmp:CreatorTool>QGIS at {HOME}</xmp:CreatorTool></rdf:Description></rdf:RDF></x:xmpmeta>")
objs = ["<< /Type /Catalog /Pages 2 0 R /Metadata 5 0 R >>", "<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 200 200] >>",
        f"<< /Author ({WHO}) /Creator (QGIS {HOME.replace(chr(92), '/')}) /Title (Plan 12) >>",
        f"<< /Type /Metadata /Subtype /XML /Length {len(xmp)} >>\nstream\n{xmp}\nendstream"]
pdf = b"%PDF-1.4\n"
offs = []
for i, o in enumerate(objs, 1):
    offs.append(len(pdf))
    pdf += f"{i} 0 obj\n{o}\nendobj\n".encode()
xref = len(pdf)
pdf += f"xref\n0 {len(objs) + 1}\n0000000000 65535 f \n".encode()
for o in offs:
    pdf += f"{o:010d} 00000 n \n".encode()
pdf += f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R /Info 4 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
pp = os.path.join(tmp, "plan.pdf")
with open(pp, "wb") as fh:
    fh.write(pdf)
rc = core.scrub_file(pp, None, OPT)
print(rc.as_text())
b = blob(pp)
check(len(b) == len(pdf) and WHO.encode() not in b and b"Plan 12" in b, "PDF: author, creator and XMP blanked in place; the title stays")
sx = int(re.search(rb"startxref\s+(\d+)", b).group(1))
check(b[sx:sx + 4] == b"xref" and all(b[o:o + 1].isdigit() for o in offs), "PDF: the cross-reference table still points right")

# ---- the Export Layers tool: hidden fields never leave, the file comes out clean ---------------------------------
mem = QgsVectorLayer("Polygon?crs=EPSG:32638", "parcels", "memory")
pr = mem.dataProvider()
pr.addAttributes([QgsField("name", QMetaType.Type.QString), QgsField("owner_phone", QMetaType.Type.QString)])
mem.updateFields()
f = QgsFeature(mem.fields())
f.setAttributes(["قطعة 1", "0555"])
f.setGeometry(QgsGeometry.fromWkt("POLYGON((0 0,10 0,10 10,0 0))"))
pr.addFeatures([f])
mem.setEditorWidgetSetup(1, QgsEditorWidgetSetup("Hidden", {}))
QgsProject.instance().addMapLayer(mem)
ctx, fb = QgsProcessingContext(), QgsProcessingFeedback()
ctx.setProject(QgsProject.instance())
check(scrub.enabled(), "cleaning outgoing files is on by default")
for fmt, name in ((0, "export.gpkg"), (2, "parcels.geojson"), (3, "parcels.kml")):
    folder = os.path.join(tmp, "exp", str(fmt))
    processing.run("biruni:exportlayers", {"LAYERS": [mem], "FORMAT": fmt, "SELECTED": False, "FOLDER": folder},
                   context=ctx, feedback=fb)
    p = os.path.join(folder, name)
    fl = feats(p) if os.path.exists(p) else []
    check(len(fl) == 1 and "owner_phone" not in fl[0][1] and "قطعة 1" in (fl[0][1].get("name"), fl[0][1].get("Name")),
          f"Export Layers ({name}): the hidden field stays home, the rest goes out")
scrub.set_enabled(False)
folder = os.path.join(tmp, "exp", "off")
processing.run("biruni:exportlayers", {"LAYERS": [mem], "FORMAT": 0, "SELECTED": False, "FOLDER": folder}, context=ctx, feedback=fb)
check("owner_phone" in feats(os.path.join(folder, "export.gpkg"))[0][1], "with the setting off, export is as before")
scrub.set_enabled(True)

shutil.rmtree(tmp, ignore_errors=True)
app.exitQgis()
print("\n" + ("ALL PASSED" if not failures else f"{len(failures)} FAILED"))
sys.exit(1 if failures else 0)
