"""Text encodings for every market: detection, mojibake, legacy fonts, .cpg names GDAL accepts, the tool, DXF.

Run:  engine\\bin\\python-qgis.bat tests\\test_text_encoding.py
"""
import os
import sys
import tempfile
import unicodedata

from qgis.core import (QgsApplication, QgsFeature, QgsGeometry, QgsProcessingContext, QgsProcessingFeedback,
                       QgsProject, QgsVectorLayer)

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "app", "plugins"))
os.environ.setdefault("QGIS_CUSTOM_CONFIG_PATH", tempfile.mkdtemp())
app = QgsApplication([], False)
app.initQgis()
sys.path.append(QgsApplication.prefixPath() + "/python/plugins")
import processing  # noqa: E402
from osgeo import gdal, ogr, osr  # noqa: E402
from processing.core.Processing import Processing  # noqa: E402

Processing.initialize()
from biruni import legacy_fonts as lf  # noqa: E402
from biruni import text_encoding as te  # noqa: E402
from biruni.algorithms import BiruniProvider  # noqa: E402

gdal.UseExceptions()
provider = BiruniProvider()
QgsApplication.processingRegistry().addProvider(provider)
failures = []


def check(cond, msg):
    print(("PASS " if cond else "FAIL ") + msg)
    if not cond:
        failures.append(msg)


def enc1258(s):
    """Vietnamese as Windows-1258 writes it: base letters plus combining tone marks."""
    tones, out = "̣̀́̃̉", b""
    for ch in s:
        try:
            out += ch.encode("cp1258")
        except UnicodeEncodeError:
            d = unicodedata.normalize("NFD", ch)
            out += unicodedata.normalize("NFC", "".join(c for c in d if c not in tones)).encode("cp1258")
            out += "".join(c for c in d if c in tones).encode("cp1258")
    return out


def encode(text, enc):
    return enc1258(text) if enc == "cp1258" else text.encode(enc)


SAMPLES = {
    "cp1256": ["شارع الملك فهد", "حي الروضة", "مخطط 621"], "cp1255": ["שלום", "רחוב הרצל", "תל אביב יפו"],
    "cp1254": ["İstanbul Şişli", "Ağaç sokağı", "Çankaya"], "cp1251": ["Москва", "улица Ленина", "Київ"],
    "koi8_r": ["Москва", "улица Ленина", "Санкт-Петербург"], "cp1250": ["Łódź", "Kraków ulica", "Příbram náměstí"],
    "cp1252": ["Café Müller", "São Paulo", "Malmö"], "cp874": ["กรุงเทพมหานคร", "ถนนสุขุมวิท", "เชียงใหม่"],
    "cp949": ["서울특별시", "강남구 테헤란로", "부산광역시"], "gbk": ["北京市朝阳区", "上海市浦东新区", "广州市天河路"],
    "big5": ["臺北市中正區", "高雄市", "新北市板橋區"], "cp932": ["東京都千代田区", "大阪市北区梅田", "さいたま市"],
    "cp1258": ["Hà Nội", "Thành phố Hồ Chí Minh", "Đà Nẵng"], "utf-8": ["Đà Nẵng", "서울", "Москва"],
}

# ---- detection and repair ---------------------------------------------------------------------------------
for enc, words in SAMPLES.items():
    got = te.detect([encode(w, enc) for w in words])[0]
    check(got == te.codec(enc), f"detect {enc}: {got}")
check(te.detect(["İzmir".encode("cp1254")], prefer=te.for_country("TR"))[0] == "cp1254", "short text + country prior")
check(te.detect([b"Main Street"]) == (None, 0.0), "plain ASCII: nothing to detect")
check(te.for_country("kr") == ["cp949"] and te.for_country("VN")[:3] == ["utf-8", "tcvn3", "vni"]
      and te.for_country("XX") == [], "for_country contract")
check(te.codec("ANSI_949") == "cp949" and te.codec("EUC-KR") == "cp949" and te.codec("TIS-620") == "cp874"
      and te.codec("GB18030") == "gbk" and te.codec("UTF-8") == "utf-8", "encoding names normalised")
for broken, want in [("ÔÇÑÚ Çáãáß", "شارع الملك"), ("Ø´Ø§Ø±Ø¹", "شارع"), ("ÐœÐ¾Ñ\x81ÐºÐ²Ð°", "Москва"),
                     ("¼\xad¿ïÆ¯º°½Ã", "서울특별시")]:
    got, how = te.repair_mojibake(broken)
    check(got == want, f"mojibake {broken!r} → {got} ({how})")
for fine in ["Café Müller", "Łódź", "İstanbul", "São Paulo", "Main Street", "شارع", "서울", "Ñandú"]:
    check(te.repair_mojibake(fine) == (fine, ""), f"real text left alone: {fine}")
check(te.persian_letters("كيلومتر يكي") == "کیلومتر یکی", "Arabic ي ك → Persian ی ک")

# ---- legacy glyph fonts -----------------------------------------------------------------------------------
for fam, src, want in [
        ("tcvn3", "ViÖt Nam", "Việt Nam"), ("tcvn3", "Thµnh phè Hå ChÝ Minh", "Thành phố Hồ Chí Minh"),
        ("tcvn3", "®\xadêng Lª Lîi", "đường Lê Lợi"), ("vni", "Vieät Nam", "Việt Nam"), ("vni", "ñöôøng", "đường"),
        ("vni", "Ñaø Naüng", "Đà Nẵng"), ("bijoy", "evsjv‡`k", "বাংলাদেশ"), ("bijoy", "‡gŠRv", "মৌজা"),
        ("bijoy", "Kg©", "কর্ম"), ("bijoy", "MÖvg", "গ্রাম"), ("krutidev", "Hkkjr", "भारत"),
        ("krutidev", "fgUnh", "हिन्दी"), ("krutidev", "dk;Z", "कार्य"), ("krutidev", "xzke", "ग्राम")]:
    got = lf.convert(src, fam)
    check(got == want, f"{fam}: {src!r} → {got}")
for u in ["Việt Nam", "đường Lê Lợi", "Thành phố Hồ Chí Minh", "Đà Nẵng"]:
    check(lf.tcvn3_to_unicode(lf.unicode_to_tcvn3(u)) == u and lf.vni_to_unicode(lf.unicode_to_vni(u)) == u,
          f"TCVN3/VNI round trip: {u}")
check(lf.unicode_to_tcvn3("Việt Nam", as_bytes=True) == b"Vi\xd6t Nam", "TCVN3 bytes for legacy systems")
check(lf.detect(["ViÖt Nam", "Hµ Néi"]) == "tcvn3" and lf.detect(["Vieät Nam", "Haø Noäi"]) == "vni",
      "Vietnamese legacy fonts recognised by themselves")
check(all(lf.detect(w) is None for w in (["Café Müller", "Søren"], ["Main street"], ["São Paulo"])),
      "no false legacy font on Western text")
check(lf.detect(["evsjv‡`k"]) is None and lf.detect(["evsjv‡`k"], hint="bijoy") == "bijoy",
      "Bijoy only with a hint")

# ---- shapefiles ---------------------------------------------------------------------------------------------
tmp = tempfile.mkdtemp()
ctx = QgsProcessingContext()
ctx.setProject(QgsProject.instance())
fb = QgsProcessingFeedback()


def make_shp(name, values, enc, ldid_zero=True):
    """A shapefile whose DBF holds `values` encoded with `enc`, with nothing saying so (no .cpg, no LDID)."""
    path = os.path.join(tmp, name + ".shp")
    ds = ogr.GetDriverByName("ESRI Shapefile").CreateDataSource(path)
    srs = osr.SpatialReference()
    srs.ImportFromEPSG(4326)
    lyr = ds.CreateLayer(name, srs, ogr.wkbPoint)
    fd = ogr.FieldDefn("name", ogr.OFTString)
    fd.SetWidth(60)
    lyr.CreateField(fd)
    for k in range(len(values)):
        f = ogr.Feature(lyr.GetLayerDefn())
        f.SetField("name", "x")
        f.SetGeometry(ogr.CreateGeometryFromWkt(f"POINT ({k} {k})"))
        lyr.CreateFeature(f)
    ds = None
    dbf = path[:-4] + ".dbf"
    data = bytearray(open(dbf, "rb").read())
    head_len, rec_len = int.from_bytes(data[8:10], "little"), int.from_bytes(data[10:12], "little")
    width = data[32 + 16]
    for k, v in enumerate(values):
        start = head_len + k * rec_len + 1
        data[start:start + width] = encode(v, enc).ljust(width, b" ")
    if ldid_zero:
        data[29] = 0
    open(dbf, "wb").write(bytes(data))
    if os.path.exists(path[:-4] + ".cpg"):
        os.remove(path[:-4] + ".cpg")
    return path


def names_of(layer):
    return [f["name"] for f in layer.getFeatures()]


for enc, words in SAMPLES.items():
    if enc == "utf-8":
        continue
    path = make_shp("cpg_" + enc, words, enc)
    te.write_cpg(path, enc)
    got = [unicodedata.normalize("NFC", n or "") for n in names_of(QgsVectorLayer(path, "t", "ogr"))]
    check(got == [unicodedata.normalize("NFC", w) for w in words],
          f".cpg '{te.cpg_name(enc)}' read back by GDAL for {enc}: {got[:1]}")


def run_fix(path_or_layer, **params):
    lyr = path_or_layer if isinstance(path_or_layer, QgsVectorLayer) else QgsVectorLayer(path_or_layer, "t", "ogr")
    QgsProject.instance().addMapLayer(lyr)
    res = processing.run("biruni:fixtextencoding", dict({"INPUT": lyr, "OUTPUT": "TEMPORARY_OUTPUT"}, **params),
                         context=ctx, feedback=fb)
    out = res["OUTPUT"]
    return res, names_of(ctx.getMapLayer(out) if isinstance(out, str) else out)


for enc in ("cp949", "cp874", "cp1255", "cp1251", "gbk", "cp932", "cp1256", "cp1254"):
    res, names = run_fix(make_shp("tool_" + enc, SAMPLES[enc], enc))
    check(names == SAMPLES[enc], f"tool repairs a {enc} shapefile with no .cpg ({res['DETECTED_ENCODING']})")
path = make_shp("tool_cpg", SAMPLES["cp949"], "cp949")
run_fix(path, WRITE_CPG=True)
check(names_of(QgsVectorLayer(path, "t", "ogr")) == SAMPLES["cp949"], ".cpg written by the tool works")
res, names = run_fix(make_shp("tool_forced", ["Ağaç", "Şişli"], "cp1254"), SOURCE=4)
check(names == ["Ağaç", "Şişli"], f"chosen code page used: {names}")
res, names = run_fix(make_shp("tool_tcvn3", ["ViÖt Nam", "Hµ Néi", "§µ N½ng"], "cp1252", ldid_zero=False))
check(names == ["Việt Nam", "Hà Nội", "Đà Nẵng"], f"TCVN3 field converted: {names}")
path = make_shp("tool_bijoy", ["evsjv‡`k", "XvKv"], "cp1252", ldid_zero=False)
check(run_fix(path, COUNTRY="BD")[1] == ["বাংলাদেশ", "ঢাকা"], "Bijoy converted with the BD hint")
plain = run_fix(path)[1]
check(plain == ["evsjv‡`k", "XvKv"], f"Bijoy untouched without a hint: {plain}")
plain = run_fix(path, FONT=1, COUNTRY="BD")[1]
check(plain == ["evsjv‡`k", "XvKv"], f"Bijoy untouched with 'No legacy font': {plain}")
mem = QgsVectorLayer("Point?crs=EPSG:4326&field=name:string", "mem", "memory")
feats = []
for k, v in enumerate(["ÐœÐ¾Ñ\x81ÐºÐ²Ð°", "شهر كرج", "Main Street"]):
    ft = QgsFeature(mem.fields())
    ft.setAttributes([v])
    ft.setGeometry(QgsGeometry.fromWkt(f"POINT({k} {k})"))
    feats.append(ft)
mem.dataProvider().addFeatures(feats)
check(run_fix(mem, PERSIAN=True)[1] == ["Москва", "شهر کرج", "Main Street"],
      "memory layer: mojibake repaired, Persian letters, plain text kept")

# ---- DXF ----------------------------------------------------------------------------------------------------


def dxf(version, codepage, texts, mtext=None):
    out = (f"0\nSECTION\n2\nHEADER\n9\n$ACADVER\n1\n{version}\n9\n$DWGCODEPAGE\n3\n{codepage}\n0\nENDSEC\n"
           "0\nSECTION\n2\nENTITIES\n").encode("ascii")
    for k, t in enumerate(texts):
        out += f"0\nTEXT\n8\n0\n10\n{k}.0\n20\n{k}.0\n30\n0.0\n40\n1.0\n1\n".encode("ascii") + t + b"\n"
    if mtext is not None:
        out += b"0\nMTEXT\n8\n0\n10\n9.0\n20\n9.0\n30\n0.0\n40\n1.0\n1\n" + mtext + b"\n"
    return out + b"0\nENDSEC\n0\nEOF\n"


def gdal_texts(path):
    ds = ogr.Open(path)  # keep the dataset alive while its layer is read
    texts = [f.GetField("Text") or "" for f in ds.GetLayer(0)]
    ds = None
    return texts


bad = os.path.join(tmp, "qgis_bug.dxf")  # QGIS issue #67573: UTF-8 text while the header says ANSI_949
open(bad, "wb").write(dxf("AC1015", "ANSI_949", ["서울특별시".encode("utf-8"), b"Gangnam"],
                          mtext="강남구 테헤란로".encode("utf-8")))
texts, declared, real = te.dxf_texts(bad)
check(declared == "ANSI_949" and real == "utf-8" and texts[0][1] == "서울특별시", "DXF: real encoding found")
fixed = os.path.join(tmp, "fixed.dxf")
info = te.fix_dxf_encoding(bad, fixed)
check("서울특별시".encode("cp949") in open(fixed, "rb").read() and info["written"] == "ANSI_949"
      and info["changed"] == 2, f"DXF re-encoded in its declared code page: {info}")
got = gdal_texts(fixed)
check("서울특별시" in got and any("강남구" in t for t in got), f"GDAL reads the repaired DXF: {got}")
mixed = os.path.join(tmp, "mixed.dxf")
open(mixed, "wb").write(dxf("AC1015", "ANSI_1252", ["شارع".encode("utf-8")]))
out = os.path.join(tmp, "mixed_fixed.dxf")
info = te.fix_dxf_encoding(mixed, out)
check(info["written"] == "ANSI_1256" and any("شارع" in t for t in gdal_texts(out)),
      f"Arabic in an ANSI_1252 drawing: switched to a code page that holds it ({info['written']})")
out = os.path.join(tmp, "mixed_1252.dxf")
te.fix_dxf_encoding(mixed, out, target="ansi_1252")
check(b"\\U+0634" in open(out, "rb").read() and te.dxf_texts(out)[0][0][1] == "شارع",
      "forced code page: missing characters become \\U+XXXX (read back by Biruni and AutoCAD)")
new = os.path.join(tmp, "r2007.dxf")
open(new, "wb").write(dxf("AC1021", "ANSI_1252", ["Москва".encode("cp1251")]))
out = os.path.join(tmp, "r2007_fixed.dxf")
info = te.fix_dxf_encoding(new, out, target="utf8")
check("Москва".encode("utf-8") in open(out, "rb").read() and info["written"] == "UTF8", "R2007+ DXF as UTF-8")

print("ALL PASS" if not failures else f"{len(failures)} failure(s)")
QgsProject.instance().removeAllMapLayers()
sys.exit(1 if failures else 0)
