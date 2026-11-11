"""Region registry, country detection, the "My country" window and the official cadastre connectors.

Run:  engine\\bin\\python-qgis.bat tests\\test_regions.py
Live checks of the cadastre services are printed as LIVE/SKIP and never fail the run (no network = SKIP).
"""
import json
import os
import sys
import tempfile

os.environ["QGIS_CUSTOM_CONFIG_PATH"] = tempfile.mkdtemp(prefix="biruni_regions_")  # keep the user's settings clean
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from qgis.core import (QgsApplication, QgsFeature, QgsGeometry, QgsPointXY, QgsProcessingContext,  # noqa: E402
                       QgsProcessingFeedback, QgsProject, QgsVectorLayer)

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "app", "plugins"))

app = QgsApplication([], True)
app.initQgis()
sys.path.append(QgsApplication.prefixPath() + "/python/plugins")
import processing  # noqa: E402
from processing.core.Processing import Processing  # noqa: E402

Processing.initialize()
from biruni.algorithms import BiruniProvider  # noqa: E402

provider = BiruniProvider()
QgsApplication.processingRegistry().addProvider(provider)

from biruni import regions  # noqa: E402
from biruni.algorithms import cadastre_lookup as cl  # noqa: E402
from biruni.regions import detect  # noqa: E402
from biruni.regions.countries import COUNTRIES, PENDING_TOOLS  # noqa: E402

failures = []


def check(cond, msg):
    print(("PASS " if cond else "FAIL ") + msg)
    if not cond:
        failures.append(msg)


# ---- country table ------------------------------------------------------------------------------------------
check(len(COUNTRIES) >= 60, f"{len(COUNTRIES)} countries in the registry")
check(all(len(cc) == 2 and cc.isupper() for cc in COUNTRIES), "ISO alpha-2 codes")
check(all(c.en and c.ar and c.ui for c in COUNTRIES.values()), "every country has English and Arabic names and a "
                                                                "language")
bad_crs = [(cc, code) for cc, c in COUNTRIES.items() for code in c.crs
           if not __import__("qgis.core", fromlist=["x"]).QgsCoordinateReferenceSystem(code).isValid()]
check(not bad_crs, f"every typical CRS code is valid {bad_crs}")
check(all(cid in cl.BY_ID for c in COUNTRIES.values() for cid in c.cadastre), "cadastre ids point to connectors")

# ---- detection ----------------------------------------------------------------------------------------------
for lon, lat, cc in ((46.7, 24.7, "SA"), (31.24, 30.04, "EG"), (2.35, 48.85, "FR"), (-47.9, -15.8, "BR"),
                     (139.7, 35.7, "JP"), (0.0, 0.0, "")):
    check(detect.country_at(lon, lat) == cc, f"country at {lon},{lat} = {cc or 'sea'}")
check(detect.from_locale("ar_SA") == "SA" and detect.from_locale("pt_BR") == "BR" and detect.from_locale("en") == "",
      "country from the regional settings")
regions.set_country(None)
lyr = QgsVectorLayer("Point?crs=EPSG:32638", "riyadh", "memory")
f = QgsFeature()
f.setGeometry(QgsGeometry.fromPointXY(QgsPointXY(670000, 2734000)))
lyr.dataProvider().addFeatures([f])
lyr.updateExtents()
QgsProject.instance().addMapLayer(lyr)
check(detect.detect() == ("SA", "project"), f"detected from the project's data: {detect.detect()}")
regions.set_country("eg")
check(detect.detect() == ("EG", "saved") and regions.current_country() == "EG", "a saved choice wins")
regions.set_country(None)
check(detect.detect()[1] == "project", "back to automatic")

# ---- regions aggregate the engines --------------------------------------------------------------------------
sa, ir, pk, br, fr, vn = (regions.region(c) for c in ("SA", "IR", "PK", "BR", "FR", "VN"))
check("hijri" in sa.calendars and "jalali" in ir.calendars, f"calendars SA {sa.calendars} IR {ir.calendars}")
check("kanal" in pk.area_units and "marla" in pk.area_units, f"Pakistan area units {pk.area_units}")
check(br.deed_language == "pt-BR", f"Brazil deed wording {br.deed_language}")
check(len(vn.crs) > 0, f"regional CRS catalogue for Vietnam ({len(vn.crs)} entries)")
check([c.id for c in fr.cadastre] == ["fr_apicarto"] and fr.cadastre[0].available, "France has its cadastre")
check(regions.region("TR").cadastre[0].available is False, "Türkiye's TKGM listed as not connected")
check(regions.region("XX").known is False and regions.region("XX").area_units == ["m2"], "unknown country still works")
check(sa.name in ("Saudi Arabia", "السعودية"), "display name")

# ---- registry integrity: every tool exists or is announced as pending ---------------------------------------
reg = QgsApplication.processingRegistry()
pending = {t for ts in PENDING_TOOLS.values() for t in ts}
missing = sorted({t for cc in COUNTRIES for t in regions.tool_ids(cc) if reg.algorithmById(t) is None
                  and t not in pending})
check(not missing, f"every recommended tool is registered or pending {missing}")
sa_tools = regions.toolbox_entries("SA")
check({"biruni:parcelbundle", "biruni:deedtoparcel", "biruni:guesscrs"} <= set(sa_tools)
      and ("biruni:convertdates" in sa_tools or "biruni:hijridates" in sa_tools), f"Saudi tools {sa_tools}")
check("biruni:cadastrelookup" in regions.toolbox_entries("FR"), "France gets the cadastre lookup")
check(regions.toolbox_entries("") == [], "no country, no category")
check("biruni:exportnationalformat" in regions.toolbox_entries("BR")
      and "biruni:opennationalformat" in regions.toolbox_entries("TR"), "national file formats per country")

# ---- the window ---------------------------------------------------------------------------------------------
from biruni.regions.region_dialog import RegionDialog  # noqa: E402

dlg = RegionDialog(None)
dlg.combo.setCurrentIndex(dlg.combo.findData("FR"))
check("France" in dlg.info.text() or "فرنسا" in dlg.info.text(), "the window describes the chosen country")
check(dlg.tools.count() >= 3, f"the window lists the country's tools ({dlg.tools.count()})")
dlg.accept()
check(detect.saved() == "FR", "OK saves the country")
regions.set_country(None)

# ---- parsing (offline) --------------------------------------------------------------------------------------
fr_json = {"type": "FeatureCollection", "features": [{"type": "Feature", "geometry": {"type": "MultiPolygon",
           "coordinates": [[[[2.36798, 48.86849], [2.36788, 48.86862], [2.36803, 48.86867], [2.36812, 48.86854],
                             [2.36798, 48.86849]]]]}, "properties": {"idu": "75111000AB0001", "nom_com": "Paris",
                                                                     "section": "AB", "numero": "0001",
                                                                     "contenance": 209}}]}
urls = []


def fake(answer):
    def get(url, timeout=0):
        urls.append(url)
        return answer if isinstance(answer, bytes) else json.dumps(answer).encode()
    return get


p = cl.France().lookup_reference("75056 AB 1", fake(fr_json))
check(len(p) == 1 and p[0].reference == "75111000AB0001" and p[0].official_area == 209
      and "code_insee=75056" in urls[-1] and "section=AB" in urls[-1] and "numero=0001" in urls[-1],
      "France: reference parsed, padded and the answer read")
p = cl.France().lookup_point(2.3680, 48.8686, fake(fr_json))
check(len(p) == 1 and "geom=" in urls[-1], "France: by point")
try:
    cl.France().lookup_reference("Paris parcel 12", fake(fr_json))
    check(False, "France: bad reference refused")
except cl.CadastreError as e:
    check("75056 AB 0001" in str(e), "France: bad reference refused with an example")

nl_json = {"type": "FeatureCollection", "features": [{"type": "Feature", "geometry": {"type": "MultiPolygon",
           "coordinates": [[[[4.89523, 52.37028], [4.89490, 52.37033], [4.89488, 52.37028], [4.89519, 52.37023],
                             [4.89523, 52.37028]]]]}, "properties": {"akr_kadastrale_gemeente_code_waarde": "ASD05",
                                                                     "sectie": "G", "perceelnummer": 1542,
                                                                     "kadastrale_gemeente_waarde": "Amsterdam",
                                                                     "kadastrale_grootte_waarde": 138}}]}
p = cl.Netherlands().lookup_reference("asd05 g 1542", fake(nl_json))
check(p and p[0].reference == "ASD05 G 1542" and "akr_kadastrale_gemeente_code_waarde=ASD05" in urls[-1],
      "Netherlands: by AKR code")
cl.Netherlands().lookup_reference("Amsterdam G 1542", fake(nl_json))
check("kadastrale_gemeente_waarde=Amsterdam" in urls[-1], "Netherlands: by municipality name")

uldk = ("0\nSRID=4326;POLYGON((52.2301 21.0101,52.2302 21.0105,52.2298 21.0106,52.2301 21.0101))|"
        "146501_8.0108.27|mazowieckie|Warszawa|Warszawa|0108|27").encode()
p = cl.Poland().lookup_reference("146501_8.0108.27", fake(uldk))
c = p[0].geometry.centroid().asPoint()
check(len(p) == 1 and 20 < c.x() < 22 and 52 < c.y() < 53 and p[0].attributes["parcel"] == "27",
      "Poland: ULDK answer read and lat/lon put in lon/lat order")
try:
    cl.Poland().lookup_point(21.0, 52.2, fake(b"-1 brak wynik\xc3\xb3w"))
    check(False, "Poland: no result reported")
except cl.CadastreError:
    check(True, "Poland: no result reported")

catastro_xy = (b'<consulta_coordenadas xmlns="http://www.catastro.meh.es/"><control><cucoor>1</cucoor></control>'
               b'<coordenadas><coord><pc><pc1>1784401</pc1><pc2>VK4718D</pc2></pc></coord></coordenadas>'
               b'</consulta_coordenadas>')
check(cl.parse_catastro_rc(catastro_xy) == "1784401VK4718D", "Spain: referencia catastral from coordinates")
es_gml = b"""<?xml version="1.0" encoding="UTF-8"?>
<FeatureCollection xmlns="http://www.opengis.net/wfs/2.0" xmlns:gml="http://www.opengis.net/gml/3.2"
 xmlns:cp="http://inspire.ec.europa.eu/schemas/cp/4.0" numberMatched="1" numberReturned="1">
 <member><cp:CadastralParcel gml:id="ES.SDGC.CP.9872023VH5797S">
  <cp:areaValue uom="m2">397</cp:areaValue>
  <cp:geometry><gml:MultiSurface gml:id="ms1" srsName="http://www.opengis.net/def/crs/EPSG/0/4258">
   <gml:surfaceMember><gml:Polygon gml:id="p1"><gml:exterior><gml:LinearRing>
    <gml:posList srsDimension="2">40.4168 -3.7038 40.4170 -3.7038 40.4170 -3.7034 40.4168 -3.7034 40.4168 -3.7038
    </gml:posList></gml:LinearRing></gml:exterior></gml:Polygon></gml:surfaceMember>
  </gml:MultiSurface></cp:geometry>
  <cp:nationalCadastralReference>9872023VH5797S</cp:nationalCadastralReference>
 </cp:CadastralParcel></member></FeatureCollection>"""
feats, authid = cl.parse_gml(es_gml)
check(len(feats) == 1 and authid == "EPSG:4258", f"GML read with its CRS ({authid})")
answers = iter([catastro_xy, es_gml])
p = cl.Spain().lookup_point(-3.7036, 40.4169, lambda url, timeout=0: next(answers))
c = p[0].geometry.centroid().asPoint()
check(len(p) == 1 and p[0].official_area == 397 and -3.71 < c.x() < -3.70 and 40.41 < c.y() < 40.42,
      "Spain: point → reference → parcel, in lon/lat")
check(not cl.Italy().by_reference and cl.Italy().available, "Italy searches by point only")

# ---- the tool -----------------------------------------------------------------------------------------------
real_fetch = cl.fetch
cl.fetch = fake(fr_json)
ctx = QgsProcessingContext()
ctx.setProject(QgsProject.instance())
res = processing.run("biruni:cadastrelookup", {"SERVICE": 0, "REFERENCE": "75056 AB 0001", "OUTPUT": "memory:"},
                     context=ctx, feedback=QgsProcessingFeedback())
out = res["OUTPUT"] if not isinstance(res["OUTPUT"], str) else ctx.getMapLayer(res["OUTPUT"])
ft = next(out.getFeatures())
check(res["COUNT"] == 1 and ft["official_area"] == 209 and 150 < ft["area_m2"] < 300
      and json.loads(ft["details"])["idu"] == "75111000AB0001", "the tool writes the parcel with both areas")
cl.fetch = real_fetch

# ---- live (informational) -----------------------------------------------------------------------------------
for name, call in (("France", lambda: cl.France().lookup_reference("75056 AB 0001")),
                   ("Netherlands", lambda: cl.Netherlands().lookup_point(4.89530, 52.37026)),
                   ("Spain", lambda: cl.Spain().lookup_reference("9872023VH5797S")),
                   ("Italy", lambda: cl.Italy().lookup_point(12.49500, 41.90000)),
                   ("Poland", lambda: cl.Poland().lookup_reference("141201_1.0001.6509"))):
    try:
        found = call()
        print(f"LIVE {name}: {len(found)} parcel(s), first {found[0].reference!r} {found[0].crs}" if found
              else f"LIVE {name}: no parcel")
    except Exception as e:  # noqa: BLE001
        print(f"SKIP {name}: {str(e)[:120]}")

print("ALL PASS" if not failures else f"{len(failures)} failure(s)")
QgsProject.instance().clear()
sys.exit(1 if failures else 0)
