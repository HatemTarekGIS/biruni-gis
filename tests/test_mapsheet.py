r"""Tests for the quick map sheet (layout built and exported).

Run:  engine\bin\python-qgis.bat tests\test_mapsheet.py [png output dir]
"""
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "app", "plugins"))

from qgis.core import (QgsApplication, QgsCoordinateReferenceSystem, QgsFeature, QgsGeometry, QgsProject,  # noqa: E402
                       QgsVectorLayer)

app = QgsApplication([], True)
app.initQgis()

from biruni import mapsheet  # noqa: E402

failures = []


def check(cond, msg):
    print(("PASS " if cond else "FAIL ") + msg)
    if not cond:
        failures.append(msg)


out_dir = sys.argv[1] if len(sys.argv) > 1 else tempfile.mkdtemp()
prj = QgsProject.instance()
prj.setCrs(QgsCoordinateReferenceSystem("EPSG:32636"))
parcels = QgsVectorLayer("Polygon?crs=EPSG:32636&field=name:string", "Parcels", "memory")
wkts = ["POLYGON((330000 3324000, 330030 3324000, 330030 3324025, 330000 3324025, 330000 3324000))",
        "POLYGON((330030 3324000, 330060 3324000, 330060 3324025, 330030 3324025, 330030 3324000))",
        "POLYGON((330000 3324025, 330060 3324025, 330060 3324050, 330000 3324050, 330000 3324025))"]
for k, w in enumerate(wkts):
    f = QgsFeature(parcels.fields())
    f.setGeometry(QgsGeometry.fromWkt(w))
    f.setAttributes([f"P{k + 1}"])
    parcels.dataProvider().addFeatures([f])
prj.addMapLayer(parcels)

geom = QgsGeometry.fromWkt(wkts[0])
pts, rows, perim, area = mapsheet.corner_rows(geom, parcels.crs())
check(len(rows) == 4 and abs(area - 750) < 1e-6 and abs(perim - 110) < 1e-6, "corner table: 4 corners, 750 m², 110 m")
check(rows[0][1] == "330000.000" and rows[0][2] == "3324025.000", "numbering starts at the north-west corner")
check(rows[0][4] == "90°00'00\"", "bearing of the first side (due east, clockwise)")
_, rows_ar, _, _ = mapsheet.corner_rows(geom, parcels.crs(), arabic=True)
check(rows_ar[0][0] == "١" and "٣٣٠٠٠٠٫٠٠٠" in rows_ar[0][1].replace("‎", ""), "Arabic-Indic digits")
check(mapsheet.nice_scale(731) == 1000 and mapsheet.nice_scale(2400) == 2500, "nice scales")

for name, rtl, landscape, parcel in (("sheet_en.png", False, True, True), ("sheet_ar.png", True, True, True),
                                     ("sheet_portrait.png", False, False, False)):
    opt = mapsheet.SheetOptions(title="Parcel P1" if not rtl else "قطعة رقم ١", subtitle="Demo district",
                                author="Survey office", paper="A4", landscape=landscape,
                                parcel=(geom, parcels.crs()) if parcel else None, rtl=rtl, arabic_digits=rtl)
    layout = mapsheet.build_layout(prj, opt)
    path = os.path.join(out_dir, name)
    ok = mapsheet.export(layout, path)
    check(ok and os.path.getsize(path) > 20000, f"layout exported: {name}")
pdf = os.path.join(out_dir, "sheet.pdf")
check(mapsheet.export(prj.layoutManager().layouts()[0], pdf) and os.path.getsize(pdf) > 5000, "PDF export")
check(len(prj.layoutManager().layouts()) == 3, "layouts registered with unique names")

print("\nALL PASSED" if not failures else f"\n{len(failures)} FAILED")
sys.exit(1 if failures else 0)
