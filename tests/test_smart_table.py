"""Tests for the smart table import (coordinate detection and parsing).

Run:  engine\\bin\\python-qgis.bat tests\\test_smart_table.py
"""
import csv
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "app", "plugins"))

from qgis.core import QgsApplication, QgsCoordinateReferenceSystem  # noqa: E402

app = QgsApplication([], False)
app.initQgis()

from biruni import smart_table as st  # noqa: E402

failures = []


def check(cond, msg):
    print(("PASS " if cond else "FAIL ") + msg)
    if not cond:
        failures.append(msg)


def close(a, b, tol=1e-6):
    return a is not None and abs(a - b) <= tol


# ---- numbers and angles
check(close(st.parse_number("٣٠٫٥"), 30.5), "Arabic-Indic digits + Arabic decimal separator")
check(close(st.parse_number("31,25"), 31.25), "decimal comma")
check(close(st.parse_number("1,234,567.5"), 1234567.5), "thousands separators")
check(st.parse_number("abc") is None, "text is not a number")
check(close(st.parse_angle("30°02'15\"N")[0], 30.0375), "DMS with symbols and N")
check(close(st.parse_angle("31 14 08.5 E")[0], 31 + 14 / 60 + 8.5 / 3600), "DMS with spaces")
check(close(st.parse_angle("S 12-30-00")[0], -12.5), "S prefix with dashes is negative")
check(close(st.parse_angle("12d30m0sW")[0], -12.5), "d/m/s letters with W")
check(close(st.parse_angle("-33.5")[0], -33.5), "negative decimal")
check(close(st.parse_angle("٣٠° ٣٠' ش")[0], 30.5), "Arabic digits and Arabic north letter")
check(close(st.parse_angle("30 30 غرب")[0], -30.5), "Arabic word for west")
check(st.parse_angle("30 75 00")[0] is None, "minutes >= 60 rejected")
lat, lon = st.parse_pair("https://maps.google.com/?q=30.0444,31.2357")
check(close(lat, 30.0444) and close(lon, 31.2357), "Google Maps ?q= link")
lat, lon = st.parse_pair("https://www.google.com/maps/place/Cairo/@30.0595,31.2234,12z")
check(close(lat, 30.0595) and close(lon, 31.2234), "Google Maps @ link")
lat, lon = st.parse_pair("24.7136, 46.6753")
check(close(lat, 24.7136) and close(lon, 46.6753), "lat, lon text pair")


def detect(rows):
    h, d = st.split_header(rows)
    return h, d, st.detect(h, d)


# ---- detection
h, d, det = detect([["Name", "Latitude", "Longitude"], ["A", 30.04, 31.23], ["B", 30.05, 31.24]])
check(det.mode == "geographic" and h[det.x] == "Longitude" and h[det.y] == "Latitude", "named lat/lon columns")

h, d, det = detect([["الاسم", "خط العرض", "خط الطول"], ["أ", "30°02'15\"", "31°14'08\""], ["ب", "30 03 00", "31 15 00"]])
check(det.mode == "geographic" and det.dms and h[det.x] == "خط الطول", "Arabic headers + DMS values")

h, d, det = detect([["name", "lat", "lon"], ["A", 46.67, 24.71], ["B", 120.5, 24.8]])
check(det.mode == "geographic" and det.swapped and h[det.y] == "lon", "swapped columns detected (lat > 90)")

h, d, det = detect([["id", "E", "N"], [1, 330125.5, 3324100.2], [2, 330210.0, 3324190.9]])
check(det.mode == "projected" and h[det.x] == "E", "UTM easting/northing")

h, d, det = detect([["id", "X", "Y"], [1, 3324100.2, 330125.5], [2, 3324190.9, 330210.0]])
check(det.mode == "projected" and det.swapped and h[det.x] == "Y", "UTM columns given in the wrong order")

h, d, det = detect([["shop", "location"], ["A", "https://maps.app/?q=30.1,31.2"], ["B", "30.2, 31.3"]])
check(det.mode == "pair" and h[det.x] == "location", "map links / pairs column")

h, d, det = detect([["id", "geom"], [1, "POLYGON((0 0,1 0,1 1,0 0))"], [2, "POLYGON((2 2,3 2,3 3,2 2))"]])
check(det.mode == "wkt", "WKT column")

h, d, det = detect([["a", "b", "c"], ["x", 31.2357, 30.0444], ["y", 31.3, 30.1], ["z", 31.21, 30.02]])
check(det.mode == "geographic" and det.x == 1 and det.y == 2, "unnamed columns found by their values")

h, d, det = detect([[1, 31.2, 30.0], [2, 31.3, 30.1]])
check(h == ["column_1", "column_2", "column_3"] and len(d) == 2, "no header row -> generated names")

h, d, det = detect([["name", "phone"], ["a", "0100"], ["b", "0111"]])
check(det.mode == "", "no coordinates -> nothing detected")

check(st.utm_epsg(31.2, 30.0) == 32636 and st.utm_epsg(-70, -33) == 32719, "UTM zone EPSG")
check(st.column_type(["0100", "0111"]) == "string", "leading-zero codes stay text")
check(st.column_type([1, 2.5]) == "double" and st.column_type(["3", 4]) == "int", "numeric column types")

# ---- reading files + building the layer
tmp = tempfile.mkdtemp()
p = os.path.join(tmp, "shops.csv")
with open(p, "w", encoding="utf-8-sig", newline="") as f:
    w = csv.writer(f, delimiter=";")
    w.writerow(["المحل", "Lat", "Long", "Phone"])
    w.writerow(["بقالة", "30,0444", "31,2357", "0100"])
    w.writerow(["صيدلية", "30.05", "31.24", "0111"])
    w.writerow(["بدون موقع", "", "", "0122"])
rows = st.read_rows(p)
h, d = st.split_header(rows)
det = st.detect(h, d)
lyr, located = st.build_layer(h, d, det, QgsCoordinateReferenceSystem("EPSG:4326"), "shops")
check(lyr.isValid() and lyr.featureCount() == 2 and located.failed == [2], "CSV with ';' and decimal commas -> 2 points, 1 failed")
f = next(lyr.getFeatures())
check(f["المحل"] == "بقالة" and f["Phone"] == "0100" and close(f.geometry().asPoint().y(), 30.0444),
      "Arabic attribute, phone kept as text, coordinates right")

import openpyxl  # noqa: E402

xp = os.path.join(tmp, "survey.xlsx")
wb = openpyxl.Workbook()
ws = wb.active
ws.title = "Points"
ws.append(["رقم", "شرقيات", "شماليات"])
ws.append([1, 330125.5, 3324100.2])
ws.append([2, 330210.0, 3324190.9])
wb.create_sheet("Empty")
wb.save(xp)
check(st.list_sheets(xp) == ["Points", "Empty"], "xlsx sheet names")
h, d = st.split_header(st.read_rows(xp, "Points"))
det = st.detect(h, d)
lyr, located = st.build_layer(h, d, det, QgsCoordinateReferenceSystem("EPSG:32636"), "survey")
check(det.mode == "projected" and lyr.featureCount() == 2 and lyr.crs().authid() == "EPSG:32636",
      "xlsx with Arabic easting/northing headers -> UTM points")

print("\nALL PASSED" if not failures else f"\n{len(failures)} FAILED")
sys.exit(1 if failures else 0)
