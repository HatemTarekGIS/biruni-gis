"""Map Studio: purpose-first layouts, smart placement, one map → many outputs.

Run:  engine\\bin\\python-qgis.bat tests\\test_map_studio.py
"""
import json
import os
import sys
import tempfile

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QT_QPA_FONTDIR", os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "Fonts"))

from qgis.core import (QgsApplication, QgsCoordinateReferenceSystem, QgsFeature, QgsGeometry, QgsLayoutExporter,  # noqa: E402
                       QgsProject, QgsRasterLayer, QgsRectangle, QgsVectorLayer)
from qgis.PyQt.QtGui import QImage  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "app", "plugins"))
app = QgsApplication([], True)
app.initQgis()

from biruni import mapsheet  # noqa: E402
from biruni.map_studio import auto_layout, intents, outputs, placement  # noqa: E402

failures = []
SHOTS = os.environ.get("BIRUNI_STUDIO_SHOTS", "")


def check(cond, msg):
    print(("PASS " if cond else "FAIL ") + msg)
    if not cond:
        failures.append(msg)


def shot(layout, name):
    if not SHOTS:
        return
    os.makedirs(SHOTS, exist_ok=True)
    img = QgsLayoutExporter(layout).renderPageToImage(0, dpi=60)
    img.save(os.path.join(SHOTS, name + ".png"))


project = QgsProject.instance()
project.setCrs(QgsCoordinateReferenceSystem("EPSG:3857"))
world = os.path.join(HERE, "..", "app", "plugins", "biruni", "resources", "data", "world.gpkg")
countries = QgsVectorLayer(world + "|layername=countries", "tbl_world_countries.gpkg", "ogr")
cities = QgsVectorLayer(world + "|layername=cities", "01_cities", "ogr")
project.addMapLayers([countries, cities])
check(countries.isValid() and cities.isValid(), "sample layers load")

# ---- names and credits --------------------------------------------------------------------------------------
check(auto_layout.clean_name("tbl_world_countries.gpkg") == "World countries", "technical names cleaned "
      f"({auto_layout.clean_name('tbl_world_countries.gpkg')})")
check(auto_layout.clean_name("01_cities") == "Cities", "numeric prefixes dropped")
check(auto_layout.clean_name("مخطط_المرجان") == "مخطط المرجان", "Arabic names kept, underscores removed")
osm = QgsRasterLayer("type=xyz&url=https://tile.openstreetmap.org/{z}/{x}/{y}.png&zmax=19&zmin=0",
                     "OpenStreetMap", "wms")
cr = auto_layout.credits([osm, countries])
check("© OpenStreetMap contributors" in cr and "Natural Earth" in cr, f"credits from sources {cr}")
check(not placement.is_data_layer(osm) and placement.is_data_layer(countries), "basemaps are not data")

# ---- every purpose builds -----------------------------------------------------------------------------------
egypt = QgsRectangle(2700000, 2400000, 4100000, 3700000)        # EPSG:3857, Egypt-ish
for iid in intents.ORDER:
    intent = intents.get(iid)
    lay = auto_layout.build(project, iid, [cities, countries], egypt, "Egypt", "en", "EG", add_to_manager=False)
    size = lay.pageCollection().page(0).pageSize()
    w, h = size.width(), size.height()
    if intent.delegate:
        ok = w > 100 and h > 100
    else:
        exp = intent.page_for(egypt.width() / egypt.height())
        ok = abs(w - exp[0]) < 0.5 and abs(h - exp[1]) < 0.5
    mp = lay.itemById("map") or next((i for i in lay.items() if i.__class__.__name__ == "QgsLayoutItemMap"), None)
    check(ok and mp is not None, f"{iid}: page {w:.1f} × {h:.1f} mm and a map")
    if mp is not None and not intent.delegate:
        check(round(mp.scale()) in mapsheet.NICE_SCALES, f"{iid}: round scale 1:{round(mp.scale()):,}")
    shot(lay, iid)

social = intents.get("social_square").page_mm()
check(abs(social[0] - 190.5) < 0.1 and abs(social[1] - 190.5) < 0.1, "1080 px at 144 dpi = 190.5 mm")
check(intents.get("wall").page_for(0.6) == (594.0, 841.0) and intents.get("wall").page_for(1.6) == (841.0, 594.0),
      "orientation follows the data")

# ---- legend: only drawn data layers, clean names ------------------------------------------------------------
lay = auto_layout.build(project, "report", [osm, cities, countries], egypt, "Egypt", "en", "EG", add_to_manager=False)
legend = next(i for i in lay.items() if i.__class__.__name__ == "QgsLayoutItemLegend")
names = [n.customProperty("legend/title-label") for n in legend.model().rootGroup().findLayers()]
check(sorted(names) == ["Cities", "World countries"], f"legend keeps data layers with clean names {names}")
check(cr.count("© OpenStreetMap contributors") == 1 and not any("OpenStreetMap Foundation" in c for c in cr),
      "the same source is credited once")
cred = [i.text() for i in lay.items() if i.__class__.__name__ == "QgsLayoutItemLabel" and "Biruni" in i.text()]
check(cred and "OpenStreetMap" in cred[0] and "WGS 84 / Pseudo-Mercator" in cred[0], "credit line with sources and CRS")

# ---- smart placement: data in one corner, the elements go elsewhere ------------------------------------------
corner = QgsVectorLayer("Polygon?crs=EPSG:3857&field=name:string", "lyr_dense_corner", "memory")
feats = []
for i in range(6):
    for j in range(6):
        f = QgsFeature(corner.fields())
        x0, y0 = 1000 + i * 1000, j * 1000        # bottom-right block of the extent below
        f.setGeometry(QgsGeometry.fromRect(QgsRectangle(x0 + 6000, y0, x0 + 6900, y0 + 900)))
        feats.append(f)
corner.dataProvider().addFeatures(feats)
project.addMapLayer(corner)
area = QgsRectangle(0, 0, 13000, 9000)
for lang in ("en", "ar"):
    lay = auto_layout.build(project, "report", [corner], area, "Corner", lang, "EG", add_to_manager=False)
    mp = lay.itemById("map")
    occ = placement.Occupancy(mp, [corner])
    origin = mp.positionWithUnits()
    worst = 0.0
    for item in lay.items():
        if item.__class__.__name__ in ("QgsLayoutItemLegend", "QgsLayoutItemScaleBar", "QgsLayoutItemPicture"):
            p, s = item.positionWithUnits(), item.sizeWithUnits()
            from qgis.PyQt.QtCore import QRectF
            worst = max(worst, occ.share(QRectF(p.x() - origin.x(), p.y() - origin.y(), s.width(), s.height())))
    check(worst < 0.05, f"{lang}: elements cover {worst:.1%} of the data (< 5%)")
    shot(lay, f"corner_{lang}")

# RTL: on an empty map the legend goes to the reading side
blank = QgsVectorLayer("Point?crs=EPSG:3857", "Points", "memory")
f = QgsFeature()
f.setGeometry(QgsGeometry.fromWkt("POINT(6500 4500)"))
blank.dataProvider().addFeatures([f])
project.addMapLayer(blank)
sides = {}
for lang in ("en", "ar"):
    lay = auto_layout.build(project, "report", [blank], area, "X", lang, "EG", add_to_manager=False)
    mp, leg = lay.itemById("map"), next(i for i in lay.items() if i.__class__.__name__ == "QgsLayoutItemLegend")
    centre = mp.positionWithUnits().x() + mp.sizeWithUnits().width() / 2
    sides[lang] = leg.positionWithUnits().x() + leg.sizeWithUnits().width() / 2 > centre
check(sides["en"] and not sides["ar"], f"legend on the reading side: right for English, left for Arabic {sides}")

# ---- re-flow and outputs --------------------------------------------------------------------------------------
main = auto_layout.build(project, "atlas", [cities, countries], egypt, "Egypt cities", "en", "EG")
check(project.layoutManager().layoutByName(main.name()) is not None, "the main layout is in the layout manager")
story = auto_layout.relayout(main, intents.get("story").page_mm())
ss = story.pageCollection().page(0).pageSize()
check(abs(ss.width() - 190.5) < 0.1 and abs(ss.height() - 338.67) < 0.1, "re-flow to the story size")
tmp = tempfile.mkdtemp()
done = outputs.export_all(main, tmp, ["pdf", "png", "story", "slide", "card"])
check(all(done.get(k) and os.path.getsize(done[k]) > 1000 for k in ("pdf", "png", "story", "slide")),
      f"outputs written {[os.path.basename(p or '-') for p in done.values()]}")
img = QImage(done["story"])
check(abs(img.width() - 1080) <= 2 and abs(img.height() - 1920) <= 2, f"story is 1080 × 1920 px ({img.width()} × "
      f"{img.height()})")
card = json.load(open(done["card"], encoding="utf-8"))
check(card["purpose"] == "atlas" and card["scale"].startswith("1:") and "Natural Earth" in card["sources"],
      "map card describes the map")
check("Made with" not in outputs.summary(done) and "files written" in outputs.summary(done), "summary text")
shot(main, "atlas_main")

# the dialog builds offscreen
from biruni.map_studio.studio_dialog import StudioDialog  # noqa: E402


class _Iface:
    def mainWindow(self):
        return None

    def activeLayer(self):
        return countries

    def openLayoutDesigner(self, layout):
        return None


class _Plugin:
    iface = _Iface()
    mw = None


dlg = StudioDialog(_Plugin())
dlg.choose("social_square")
check(dlg.layout is not None and dlg.preview.pixmap() is not None and not dlg.preview.pixmap().isNull(),
      "studio window builds the layout and its preview")
dlg.close()

print("ALL PASS" if not failures else f"{len(failures)} failure(s)")
project.clear()
sys.exit(1 if failures else 0)
