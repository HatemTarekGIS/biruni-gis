"""National / closed file formats (biruni.formats) and the open/save national-format tools.

Run:  engine\\bin\\python-qgis.bat tests\\test_national_formats.py

Every sample is generated here from the published layouts; nothing is downloaded. The NCZ byte
builders follow NCZ2Geo's test fixtures (Copyright (C) 2026 Yusuf Eminoğlu, GPL-2.0-or-later).
"""
import os
import struct
import sys
import tempfile

from osgeo import ogr
from qgis.core import QgsApplication, QgsFeature, QgsGeometry, QgsProcessingContext, QgsProcessingFeedback, \
    QgsProject, QgsVectorLayer

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "app", "plugins"))

app = QgsApplication([], False)
app.initQgis()
sys.path.append(QgsApplication.prefixPath() + "/python/plugins")
import processing  # noqa: E402
from processing.core.Processing import Processing  # noqa: E402

Processing.initialize()
from biruni import formats  # noqa: E402
from biruni.algorithms import BiruniProvider  # noqa: E402
from biruni.formats import cadastral_xml, catastro_gml, cxf, dgn, ncz, sigef_ods, sima, sxf  # noqa: E402
from biruni.formats.common import FormatError, write_gpkg  # noqa: E402

provider = BiruniProvider()
QgsApplication.processingRegistry().addProvider(provider)
failures = []
TMP = tempfile.mkdtemp(prefix="biruni_natfmt_")


def check(cond, msg):
    print(("PASS " if cond else "FAIL ") + msg)
    if not cond:
        failures.append(msg)


def raises(fn, *a, **k):
    try:
        fn(*a, **k)
    except FormatError as e:
        return str(e)
    return None


def tmp(name):
    return os.path.join(TMP, name)


def gpkg_tables(path):
    ds = ogr.Open(path)
    return {ds.GetLayer(i).GetName(): ds.GetLayer(i).GetFeatureCount() for i in range(ds.GetLayerCount())}


def srs_code(path, layer=0):
    ds = ogr.Open(path)  # keep the data source alive while its layer is used
    lyr = ds.GetLayer(layer) if isinstance(layer, int) else ds.GetLayerByName(layer)
    srs = lyr.GetSpatialRef()
    return srs.GetAuthorityCode(None) if srs is not None else None


def first_value(path, layer, field):
    ds = ogr.Open(path)
    lyr = ds.GetLayerByName(layer)
    return lyr.GetFeatureCount(), lyr.GetNextFeature().GetField(field)


# ---- NCZ (Netcad) ---------------------------------------------------------------------------------------
BX, BY = 500000.0, 4500000.0


def _blk(kind, body):
    return bytes([kind]) + len(body).to_bytes(4, "little") + bytes(body)


def _put(body, off, fmt, v):
    struct.pack_into(fmt, body, off - 5, v)


def _text(body, len_off, value):
    body[len_off - 5] = len(value)
    body[len_off - 4:len_off - 4 + len(value)] = value


def ncz_point(layer, x, y, name):
    b = bytearray(128)
    b[1], b[2] = 1, layer
    _put(b, 8, "<d", y)
    _put(b, 16, "<d", x)
    _text(b, 86, name)
    return _blk(21, b)


def ncz_text(layer, x, y, text):
    b = bytearray(160)
    b[1], b[2] = 5, layer
    _put(b, 8, "<d", y)
    _put(b, 16, "<d", x)
    _text(b, 97, text)
    _put(b, 86, "<f", 2.5)
    return _blk(21, b)


def ncz_polyline(layer, pts):
    size = 113 + len(pts) * 24
    b = bytearray(size - 4)
    b[1], b[2] = 7, layer
    _put(b, 8, "<d", BY)
    _put(b, 16, "<d", BX)
    _text(b, 86, b"PL")
    for i, (x, y) in enumerate(pts):
        v = 113 + i * 24
        _put(b, v, "<d", x)
        _put(b, v + 8, "<d", y)
    return _blk(21, b)


def ncz_layers(names):
    b = bytearray(18 + len(names) * 29 + 8)
    struct.pack_into("<H", b, 16 - 5, len(names))
    for i, n in enumerate(names):
        _text(b, 18 + i * 29 + 4, n)
    return _blk(6, b)


def ncz_epsg(code):
    marker = b'SRS:"' + code + b'">'
    b = bytearray(64)
    _text(b, 5, b"TILED_XML")
    b[24:24 + len(marker)] = marker
    return _blk(28, b)


square = [(BX, BY), (BX, BY + 50), (BX + 50, BY + 50), (BX + 50, BY), (BX, BY)]
open_line = [(BX, BY), (BX + 25, BY + 10), (BX + 50, BY + 40)]
ncz_path = tmp("plan.ncz")
with open(ncz_path, "wb") as f:
    f.write(ncz_layers(["YOL".encode("cp1254"), "PARSEL".encode("cp1254"), "YAZI".encode("cp1254")])
            + ncz_epsg(b"EPSG:5254") + ncz_point(0, BX + 1, BY + 2, "NK1".encode("cp1254"))
            + ncz_polyline(1, square) + ncz_polyline(0, open_line)
            + ncz_text(2, BX + 5, BY + 5, "ŞEHİR Ğİ".encode("cp1254")))
check(formats.detect(ncz_path) == "ncz", "NCZ detected by extension")
d = ncz.read(ncz_path)
lay = {(l.name, l.kind): l for l in d.non_empty()}
check(d.crs == "EPSG:5254", f"NCZ: coordinate system read from the file ({d.crs})")
check(("PARSEL", "polygon") in lay and len(lay[("PARSEL", "polygon")]) == 1, "NCZ: closed polyline → parcel polygon")
poly = ogr.CreateGeometryFromWkt(lay[("PARSEL", "polygon")].features[0][0])
check(abs(poly.GetArea() - 2500) < 1e-6, f"NCZ: polygon area right ({poly.GetArea():.2f})")
check(("YOL", "line") in lay and ("YOL", "point") in lay, "NCZ: open polyline and point on their layer")
pt = ogr.CreateGeometryFromWkt(lay[("YOL", "point")].features[0][0])
check(abs(pt.GetX() - (BX + 1)) < 1e-6 and abs(pt.GetY() - (BY + 2)) < 1e-6,
      "NCZ: northing-first storage turned into x = easting")
txt = lay[("YAZI", "point")].features[0][1]
check(txt.get("text") == "ŞEHİR Ğİ", f"NCZ: Turkish text kept (Windows-1254): {txt.get('text')!r}")
check(raises(ncz.read, tmp("empty.ncz")) is not None if open(tmp("empty.ncz"), "wb").write(b"\0" * 32) else True,
      "NCZ: empty/unknown file gives a clear message")
out = tmp("ncz.gpkg")
written = write_gpkg(d, out)
check(len(written) == 4 and sum(n for *_x, n in written) == 4, f"NCZ → GeoPackage: {written}")
check(srs_code(out) == "5254", "GeoPackage layers carry the file's CRS")

# ---- CXF (Italy) ----------------------------------------------------------------------------------------
cxf_text = "\n".join([
    "MAPPA", "A001_000100", "SCALA ORIGINARIA", "2000",
    "BORDO", "123", "1.5", "0", "50", "50", "50", "50", "1", "8", "4",
    "0", "0", "0", "100", "100", "100", "100", "0",
    "40", "40", "40", "60", "60", "60", "60", "40",
    "BORDO", "124+", "1", "0", "150", "50", "150", "50", "0", "4",
    "120", "20", "120", "80", "180", "80", "180", "20",
    "BORDO", "STRADA", "1", "0", "0", "0", "0", "0", "0", "4",
    "100", "0", "100", "100", "110", "100", "110", "0",
    "TESTO", "Via Roma", "2", "90", "105", "50",
    "SIMBOLO", "5", "0", "10", "10",
    "FIDUCIALE", "PF01/0001/A001", "8", "1000.5", "2000.25", "1001", "2001",
    "LINEA", "1", "3", "0", "0", "50", "50", "100", "0",
    "EOF", ""])
cxf_path = tmp("A001_000100.cxf")
open(cxf_path, "w", encoding="cp1252").write(cxf_text)
open(tmp("A001_000100.sup"), "w").write("123 9600\n124+ 3600\n")
check(formats.detect(cxf_path) == "cxf", "CXF detected")
d = cxf.read(cxf_path)
cad = d.layers[("cadastre", "polygon")]
kinds = {a["code"]: a["kind"] for _w, a in cad.features}
check(kinds == {"123": "parcel", "124+": "building", "STRADA": "strada"}, f"CXF: parcel/building/road codes {kinds}")
parcel = ogr.CreateGeometryFromWkt(cad.features[0][0])
check(abs(parcel.GetArea() - 9600) < 1e-6, f"CXF: parcel with an island (courtyard) = 9600 m² ({parcel.GetArea()})")
check(cad.features[0][1].get("sup_area") == 9600.0, "CXF: area from the .SUP file joined")
check(len(d.layers[("fiducial_points", "point")]) == 1 and
      d.layers[("fiducial_points", "point")].features[0][1]["number"] == "PF01/0001/A001", "CXF: fiducial point")
check(len(d.layers[("texts", "point")]) == 1 and len(d.layers[("lines", "line")]) == 1, "CXF: texts and lines")
check(raises(cxf.read, tmp("bad.cxf")) is not None if open(tmp("bad.cxf"), "w").write("HELLO\n") else True,
      "CXF: a non-CXF file is refused clearly")

# ---- SIMA (Japan) ----------------------------------------------------------------------------------------
sima_path = tmp("survey.sim")
sima.write(sima_path, points=[(1, "基準点", -1000.0, 2000.0, 12.5)],
           parcels=[("101-1", "畑", [(0, 0), (0, 30), (20, 30), (20, 0), (0, 0)])])
raw = open(sima_path, "rb").read()
check("基準点".encode("cp932") in raw and b"A01,1," in raw, "SIMA written in Shift_JIS with A01 records")
check(b"A01,1,\x8a\xee\x8f\x80\x93\x5f,2000.000,-1000.000" in raw,
      "SIMA: written in survey order X = northing, Y = easting")
d = sima.read(sima_path)
pts = d.layers[("points", "point")]
check(len(pts) == 5, f"SIMA: 1 point + 4 parcel corners read back ({len(pts)})")
first = ogr.CreateGeometryFromWkt(pts.features[0][0])
check(abs(first.GetX() + 1000) < 1e-6 and abs(first.GetY() - 2000) < 1e-6 and pts.features[0][1]["name"] == "基準点",
      "SIMA: round trip keeps easting/northing and the Japanese name")
plots = d.layers[("parcels", "polygon")]
check(len(plots) == 1 and abs(ogr.CreateGeometryFromWkt(plots.features[0][0]).GetArea() - 600) < 1e-6
      and plots.features[0][1]["parcel_no"] == "101-1", "SIMA: parcel rebuilt from its corner points (600 m²)")

# ---- Cadastral XML: Rosreestr ------------------------------------------------------------------------------
ru_new = """<?xml version="1.0" encoding="utf-8"?>
<extract_about_property_land><land_record><object><common_data><cad_number>50:20:0010101:15</cad_number>
</common_data></object><contours_location><contours>
<contour><entity_spatial><sk_id>МСК-50, зона 2</sk_id><spatials_elements>
<spatial_element><ordinates>
<ordinate><x>470000</x><y>2200000</y></ordinate><ordinate><x>470100</x><y>2200000</y></ordinate>
<ordinate><x>470100</x><y>2200100</y></ordinate><ordinate><x>470000</x><y>2200100</y></ordinate>
<ordinate><x>470000</x><y>2200000</y></ordinate></ordinates></spatial_element>
<spatial_element><ordinates>
<ordinate><x>470040</x><y>2200040</y></ordinate><ordinate><x>470060</x><y>2200040</y></ordinate>
<ordinate><x>470060</x><y>2200060</y></ordinate><ordinate><x>470040</x><y>2200060</y></ordinate>
<ordinate><x>470040</x><y>2200040</y></ordinate></ordinates></spatial_element>
</spatials_elements></entity_spatial></contour>
<contour><entity_spatial><spatials_elements><spatial_element><ordinates>
<ordinate><x>471000</x><y>2201000</y></ordinate><ordinate><x>471010</x><y>2201000</y></ordinate>
<ordinate><x>471010</x><y>2201010</y></ordinate><ordinate><x>471000</x><y>2201000</y></ordinate>
</ordinates></spatial_element></spatials_elements></entity_spatial></contour>
</contours></contours_location></land_record></extract_about_property_land>"""
ru_path = tmp("egrn.xml")
open(ru_path, "w", encoding="utf-8").write(ru_new)
check(formats.detect(ru_path) == "cadastral_xml", "Rosreestr XML detected")
d = cadastral_xml.read(ru_path)
lyr = d.layers[("parcels", "polygon")]
mp = ogr.CreateGeometryFromWkt(lyr.features[0][0])
check(len(lyr) == 1 and lyr.features[0][1]["cad_number"] == "50:20:0010101:15" and mp.GetGeometryCount() == 2,
      "Rosreestr: multi-contour plot → one multipolygon under its cadastral number")
check(abs(mp.GetArea() - (10000 - 400 + 50)) < 1e-6, f"Rosreestr: hole kept, area {mp.GetArea()}")
c = mp.GetGeometryRef(0).GetGeometryRef(0)
check(abs(c.GetX(0) - 2200000) < 1e-6 and abs(c.GetY(0) - 470000) < 1e-6, "Rosreestr: X north / Y east swapped to x, y")
check(any("МСК-50" in n for n in d.notes), "Rosreestr: coordinate system named in the file is reported")
ru_old = """<?xml version="1.0" encoding="utf-8"?><KPT><Parcels><Parcel CadastralNumber="77:01:0001001:7">
<EntitySpatial EntSys="ID1"><SpatialElement>
<SpelementUnit SuNmb="1"><Ordinate X="10" Y="20" OrdNmb="1"/></SpelementUnit>
<SpelementUnit SuNmb="2"><Ordinate X="10" Y="40" OrdNmb="2"/></SpelementUnit>
<SpelementUnit SuNmb="3"><Ordinate X="30" Y="40" OrdNmb="3"/></SpelementUnit>
<SpelementUnit SuNmb="4"><Ordinate X="10" Y="20" OrdNmb="4"/></SpelementUnit>
</SpatialElement></EntitySpatial></Parcel></Parcels></KPT>"""
open(tmp("kpt.xml"), "w", encoding="utf-8").write(ru_old)
d = cadastral_xml.read(tmp("kpt.xml"))
lyr = d.layers[("parcels", "polygon")]
check(len(lyr) == 1 and lyr.features[0][1]["cad_number"] == "77:01:0001001:7"
      and abs(ogr.CreateGeometryFromWkt(lyr.features[0][0]).GetArea() - 200) < 1e-6,
      "Rosreestr: old schema (SpatialElement / Ordinate X=…) read too")
check(raises(cadastral_xml.read, tmp("broken.xml")) is not None if open(tmp("broken.xml"), "w").write("<a><b>") else True,
      "Cadastral XML: a damaged file gives a clear message")

# ---- Cadastral XML: Ukraine IN4 -----------------------------------------------------------------------------
ua = """<?xml version="1.0" encoding="windows-1251"?>
<UkrainianCadastralExchangeFile><InfoPart><MetricInfo>
<CoordinateSystem><USC2000/></CoordinateSystem>
<PointInfo>
<Point><UIDP>1</UIDP><PN>1</PN><X>5500000.00</X><Y>300000.00</Y></Point>
<Point><UIDP>2</UIDP><PN>2</PN><X>5500000.00</X><Y>300040.00</Y></Point>
<Point><UIDP>3</UIDP><PN>3</PN><X>5500025.00</X><Y>300040.00</Y></Point>
<Point><UIDP>4</UIDP><PN>4</PN><X>5500025.00</X><Y>300000.00</Y></Point>
</PointInfo><Polylines>
<PL><ULID>10</ULID><Points><P>1</P><P>2</P></Points></PL>
<PL><ULID>11</ULID><Points><P>3</P><P>2</P></Points></PL>
<PL><ULID>12</ULID><Points><P>3</P><P>4</P><P>1</P></Points></PL>
</Polylines></MetricInfo>
<CadastralZoneInfo><KOATUU>3222486200</KOATUU><CadastralZoneNumber>01</CadastralZoneNumber>
<CadastralQuarters><CadastralQuarterInfo><CadastralQuarterNumber>012</CadastralQuarterNumber>
<Parcels><ParcelInfo><ParcelMetricInfo><ParcelID>0045</ParcelID><Area><Size>0.1000</Size></Area>
<Externals><Boundary><Lines><Line><ULID>10</ULID></Line><Line><ULID>11</ULID></Line><Line><ULID>12</ULID></Line>
</Lines></Boundary></Externals></ParcelMetricInfo></ParcelInfo></Parcels>
</CadastralQuarterInfo></CadastralQuarters></CadastralZoneInfo></InfoPart></UkrainianCadastralExchangeFile>"""
open(tmp("parcel.xml"), "w", encoding="cp1251").write(ua)
check(cadastral_xml.detect(tmp("parcel.xml")) == "ua", "Ukrainian IN4 exchange file detected")
d = cadastral_xml.read(tmp("parcel.xml"))
lyr = d.layers[("parcels", "polygon")]
check(len(lyr) == 1 and lyr.features[0][1]["cad_number"] == "3222486200:01:012:0045",
      f"Ukraine: cadastral number from KOATUU:zone:quarter:parcel ({lyr.features[0][1].get('cad_number')})")
check(abs(ogr.CreateGeometryFromWkt(lyr.features[0][0]).GetArea() - 1000) < 1e-6,
      "Ukraine: boundary chained from polylines (one reversed) = 1000 m²")
check(len(d.layers[("boundary_points", "point")]) == 4 and any("USC2000" in n for n in d.notes),
      "Ukraine: boundary points and the coordinate system")

# ---- SXF (Russia) ------------------------------------------------------------------------------------------
check(ogr.GetDriverByName("SXF") is not None, "GDAL SXF driver available in the engine")
open(tmp("bad.sxf"), "wb").write(b"NOTSXF" + b"\0" * 100)
check(raises(sxf.read, tmp("bad.sxf")) is not None, "SXF: a damaged file gives a clear message")
open(tmp("bad.rsc"), "wb").write(b"\0")
check(sxf.find_rsc(tmp("bad.sxf")) == tmp("bad.rsc"), "SXF: classifier next to the file is found")

# ---- DGN ---------------------------------------------------------------------------------------------------
v8 = tmp("v8.dgn")
open(v8, "wb").write(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\0" * 504)
msg = raises(dgn.read, v8)
check(dgn.version(v8) == 8 and msg and "ODA File Converter" in msg, "DGN v8 recognised, user told the free routes")
v7 = tmp("v7.dgn")
dsrc = ogr.GetDriverByName("DGN").CreateDataSource(v7)
lyr7 = dsrc.CreateLayer("elements")
f = ogr.Feature(lyr7.GetLayerDefn())
f.SetGeometry(ogr.CreateGeometryFromWkt("LINESTRING (0 0,10 10,20 0)"))
lyr7.CreateFeature(f)
dsrc = None
check(dgn.version(v7) == 7, "DGN v7 recognised")
d = dgn.read(v7)
check(d.count() == 1 and next(iter(d.non_empty())).kind == "line", "DGN v7 elements read through GDAL")

# ---- SIGEF ODS (Brazil) ------------------------------------------------------------------------------------
ring = [(-47.90, -15.80), (-47.90, -15.79), (-47.89, -15.79), (-47.89, -15.80), (-47.90, -15.80)]
verts = sigef_ods.vertices_from_ring(ring, "ABCD", "M", 1, 0.05)
check([v["code"] for v in verts] == ["ABCD-M-0001", "ABCD-M-0002", "ABCD-M-0003", "ABCD-M-0004"],
      "SIGEF: vertex codes numbered in order")
check(verts[0]["lat"] == max(v["lat"] for v in verts), "SIGEF: numbering starts at the northern vertex")
area2 = sum(verts[i]["lon"] * verts[(i + 1) % 4]["lat"] - verts[(i + 1) % 4]["lon"] * verts[i]["lat"] for i in range(4))
check(area2 < 0, "SIGEF: vertices run clockwise")
check(sigef_ods.dms(-47.875, "E", "W") == "47 52 30,000 W", f"SIGEF: DMS with decimal comma ({sigef_ods.dms(-47.875, 'E', 'W')})")
bad = [dict(v) for v in verts]
bad[1]["code"] = "XYZ"
bad[2]["sigma_x"] = 0.9
problems = sigef_ods.check(bad)
check(any("ABCD-M-0001" in p for p in problems) and any("0.50 m" in p for p in problems),
      f"SIGEF: bad code and too-large precision caught ({len(problems)} problems)")
cross = [dict(v) for v in verts]
cross[1], cross[2] = cross[2], cross[1]
check(any("crosses itself" in p for p in sigef_ods.check(cross)), "SIGEF: self-crossing perimeter caught")
ods = tmp("sigef.ods")
sigef_ods.write(ods, [verts], {"denominacao": "Fazenda Teste"})
src = ogr.Open(ods)
names = [src.GetLayer(i).GetName() for i in range(src.GetLayerCount())]
per = src.GetLayerByName("perimetro_1")
first = per.GetNextFeature() if per else None
check(names == ["identificacao", "perimetro_1"] and per.GetFeatureCount() == 4 and first.GetField("codigo") == "ABCD-M-0001"
      and first.GetField("longitude").endswith(" W") and "," in first.GetField("longitude"),
      f"SIGEF: ODS with identificacao + perimetro_1 sheets ({names})")
src = None
check(raises(sigef_ods.write, tmp("bad.ods"), [bad]) is not None, "SIGEF: writing refused while checks fail")

# ---- Catastro GML (Spain) ------------------------------------------------------------------------------------
gml = tmp("parcela.gml")
outer = [(440000, 4470000), (440100, 4470000), (440100, 4470100), (440000, 4470100), (440000, 4470000)]
hole = [(440040, 4470040), (440060, 4470040), (440060, 4470060), (440040, 4470060), (440040, 4470040)]
catastro_gml.write(gml, [{"id": "P1", "polygons": [[list(reversed(outer)), hole]], "reference": "1234567VK4713S0001AB"}], 25830)
check(catastro_gml.validate(gml) == [], f"Catastro GML: valid structure ({catastro_gml.validate(gml)})")
text = open(gml, encoding="utf-8").read()
check('<cp:areaValue uom="m2">9600</cp:areaValue>' in text and "EPSG/0/25830" in text
      and "1234567VK4713S0001AB" in text, "Catastro GML: area 9600, ETRS89 UTM 30, cadastral reference")
gml_ds = ogr.Open(gml)
check(gml_ds is not None and gml_ds.GetLayer(0).GetFeatureCount() == 1, "Catastro GML opens in GDAL")
gml_ds = None
broken = text.replace("440000.00 4470000.00 440100.00 4470000.00", "440000.00 4470000.00 440000.00 4470100.00", 1)
open(tmp("bad.gml"), "w", encoding="utf-8").write(broken.replace('srsName="http://www.opengis.net/def/crs/EPSG/0/25830"',
                                                                 'srsName="http://www.opengis.net/def/crs/EPSG/0/4326"', 1))
check(any("not accepted" in p for p in catastro_gml.validate(tmp("bad.gml"))), "Catastro GML: wrong CRS flagged")
check(raises(catastro_gml.write, tmp("x.gml"), [{"id": "1", "polygons": [[outer]]}], 4326) is not None,
      "Catastro GML: non-UTM coordinates refused")

# ---- registry ----------------------------------------------------------------------------------------------
check({f["id"] for f in formats.for_country("TR")} == {"ncz"} and
      {"sxf", "cadastral_xml"} <= {f["id"] for f in formats.for_country("ru")}, "for_country: TR → NCZ, RU → SXF + XML")
check({f["id"] for f in formats.for_country("ES")} == {"catastro_gml"} and formats.for_country("ES")[0]["write"],
      "for_country: ES → Catastro GML writer")
check("*.ncz" in formats.file_filter() and "*.cxf" in formats.file_filter(), "file filter lists the formats")
plain = tmp("notes.txt")
open(plain, "w").write("hello")
check(formats.detect(plain) is None and raises(formats.read, plain) is not None, "unknown file: clear refusal")

# ---- algorithms ----------------------------------------------------------------------------------------------
ctx = QgsProcessingContext()
ctx.setProject(QgsProject.instance())
fb = QgsProcessingFeedback()
res = processing.run("biruni:opennationalformat", {"INPUT": cxf_path, "FORMAT": 0, "CRS": "EPSG:3003",
                                                    "OUTPUT": tmp("cxf_out.gpkg")}, context=ctx, feedback=fb)
tables = gpkg_tables(res["OUTPUT"])
check(res["LAYER_COUNT"] == 5 and res["FORMAT"] == "cxf" and tables.get("cadastre_polygon") == 3,
      f"opennationalformat: CXF → GeoPackage {tables}")
check(srs_code(res["OUTPUT"], "cadastre_polygon") == "3003", "opennationalformat: chosen CRS applied")
res = processing.run("biruni:opennationalformat", {"INPUT": ncz_path, "FORMAT": 0, "OUTPUT": tmp("ncz_out.gpkg")},
                     context=ctx, feedback=fb)
check(res["FEATURE_COUNT"] == 4, "opennationalformat: NCZ through the tool")

parcels = QgsVectorLayer("Polygon?crs=EPSG:31983&field=lote:string&field=ref:string", "lotes", "memory")
feat = QgsFeature(parcels.fields())
feat.setGeometry(QgsGeometry.fromWkt("POLYGON((190000 8250000, 190000 8250100, 190100 8250100, 190100 8250000, 190000 8250000))"))
feat.setAttributes(["L-1", ""])
parcels.dataProvider().addFeatures([feat])
QgsProject.instance().addMapLayer(parcels)
res = processing.run("biruni:exportnationalformat", {"INPUT": parcels.id(), "FORMAT": 1, "ID_FIELD": "lote",
                                                      "PREFIX": "QWER", "OUTPUT": tmp("lote.ods")}, context=ctx, feedback=fb)
n_rows, code1 = first_value(res["OUTPUT"], "perimetro_1", "codigo")
check(res["COUNT"] == 1 and n_rows == 4 and code1 == "QWER-M-0001",
      "exportnationalformat: SIGEF ODS from a UTM parcel (converted to SIRGAS 2000)")
res = processing.run("biruni:exportnationalformat", {"INPUT": parcels.id(), "FORMAT": 0, "ID_FIELD": "lote",
                                                      "OUTPUT": tmp("lote.sim")}, context=ctx, feedback=fb)
check(len(sima.read(res["OUTPUT"]).layers[("parcels", "polygon")]) == 1, "exportnationalformat: SIMA parcels")
es = QgsVectorLayer("Polygon?crs=EPSG:4326&field=id:string&field=rc:string", "parcelas", "memory")
feat = QgsFeature(es.fields())
feat.setGeometry(QgsGeometry.fromWkt("POLYGON((-3.70 40.41, -3.699 40.41, -3.699 40.411, -3.70 40.411, -3.70 40.41))"))
feat.setAttributes(["7", "9872023VH5797S0001WX"])
es.dataProvider().addFeatures([feat])
QgsProject.instance().addMapLayer(es)
res = processing.run("biruni:exportnationalformat", {"INPUT": es.id(), "FORMAT": 2, "ID_FIELD": "id", "REF_FIELD": "rc",
                                                      "OUTPUT": tmp("es.gml")}, context=ctx, feedback=fb)
check(catastro_gml.validate(res["OUTPUT"]) == [] and "EPSG/0/25830" in open(res["OUTPUT"], encoding="utf-8").read(),
      "exportnationalformat: Catastro GML in ETRS89 UTM 30 for Madrid, valid")

print("ALL PASS" if not failures else f"{len(failures)} failure(s)")
sys.exit(1 if failures else 0)
