"""Tests for 'What coordinate system is this?' and 'WhatsApp chat locations to points'.

Run:  engine\\bin\\python-qgis.bat tests\\test_office_crs_whatsapp.py
"""
import os
import sys
import tempfile
import time
import zipfile
from datetime import date, datetime

from qgis.core import (QgsApplication, QgsCoordinateReferenceSystem, QgsCoordinateTransform, QgsFeature,
                       QgsGeometry, QgsPointXY, QgsProcessingContext, QgsProcessingFeedback, QgsProject,
                       QgsVectorLayer)

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "app", "plugins"))

app = QgsApplication([], False)
app.initQgis()
sys.path.append(QgsApplication.prefixPath() + "/python/plugins")
import processing  # noqa: E402
from processing.core.Processing import Processing  # noqa: E402

Processing.initialize()
from biruni.algorithms import BiruniProvider  # noqa: E402
from biruni.algorithms import guess_crs as gc  # noqa: E402
from biruni.algorithms import whatsapp_locations as wa  # noqa: E402

provider = BiruniProvider()
QgsApplication.processingRegistry().addProvider(provider)
failures = []
TMP = tempfile.mkdtemp(prefix="biruni_office_")
WGS84 = QgsCoordinateReferenceSystem("EPSG:4326")


def check(cond, msg):
    print(("PASS " if cond else "FAIL ") + msg)
    if not cond:
        failures.append(msg)


ctx = QgsProcessingContext()
ctx.setProject(QgsProject.instance())
fb = QgsProcessingFeedback()


def out(value):
    return ctx.getMapLayer(value) if isinstance(value, str) else value


def project(points_lonlat, authid):
    xf = QgsCoordinateTransform(WGS84, QgsCoordinateReferenceSystem(authid), QgsProject.instance())
    return [(p.x(), p.y()) for p in (xf.transform(QgsPointXY(lo, la)) for lo, la in points_lonlat)]


def no_crs_layer(xy, geom="Point"):
    lyr = QgsVectorLayer(f"{geom}?field=id:integer", "unknown", "memory")
    feats = []
    for i, (x, y) in enumerate(xy):
        f = QgsFeature(lyr.fields())
        f.setGeometry(QgsGeometry.fromPointXY(QgsPointXY(x, y)))
        f.setAttributes([i])
        feats.append(f)
    lyr.dataProvider().addFeatures(feats)
    QgsProject.instance().addMapLayer(lyr)
    return lyr


def rows(table):
    return [f.attributes() for f in sorted(out(table).getFeatures(), key=lambda f: f["rank"])]


RIYADH = [(46.6753 + 0.01 * i, 24.7136 + 0.007 * (i % 5)) for i in range(30)]
JEDDAH = [(39.1925 + 0.004 * i, 21.4858 - 0.003 * i) for i in range(20)]
CAIRO = [(31.2357 + 0.002 * i, 30.0444 + 0.002 * i) for i in range(15)]

# ---- pure helpers -------------------------------------------------------------
check(gc.parse_pairs("1 512345.12 2734567.89") == [(512345.12, 2734567.89)], "pasted: leading point number skipped")
check(gc.parse_pairs("512345 2734567 612.5") == [(512345.0, 2734567.0)], "pasted: trailing height ignored")
check(gc.parse_pairs("٥١٢٣٤٥٫١ ٢٧٣٤٥٦٧٫٨\n\nX Y\n512000,2734000") == [(512345.1, 2734567.8), (512000.0, 2734000.0)],
      "pasted: Arabic digits, blank and header lines, comma separated")
check(len(gc.spread(list(range(1000)))) == 50 and gc.spread([1, 2]) == [1, 2], "sample: at most 50 points")
check(abs(gc.haversine_km(46.0, 24.0, 47.0, 24.0) - 101.6) < 1, "haversine: one degree of longitude at 24°N")
codes = gc.candidate_codes((46.7, 24.7))
check(all(c in codes for c in ("EPSG:20438", "EPSG:9358", "EPSG:32638", "EPSG:22992", "EPSG:3066", "EPSG:3857",
                                "EPSG:4326")), f"candidates include the regional systems ({len(codes)})")
check("EPSG:32600" not in codes, "candidates: the UTM 'grid system' meta-CRS is left out")

# ---- Ain el Abd UTM 38N in Riyadh, location clicked on a feature --------------------
t0 = time.time()
lyr = no_crs_layer(project(RIYADH, "EPSG:20438"))
res = processing.run("biruni:guesscrs", {"INPUT": lyr, "LOCATION": f"{RIYADH[3][0]},{RIYADH[3][1]} [EPSG:4326]",
                                         "OUTPUT": "memory:"}, context=ctx, feedback=fb)
elapsed = time.time() - t0
r = rows(res["OUTPUT"])
check(res["TOP_CRS"] == "EPSG:20438" and not res["TOP_SWAPPED"], f"Ain el Abd 38N found ({res['TOP_CRS']})")
check(elapsed < 30, f"fast enough ({elapsed:.1f} s)")
wgs = next(x for x in r if x[1] == "EPSG:32638" and not x[4])
check(wgs[7] > 50 and wgs[3] > 0.05, f"WGS 84 UTM 38N ranked lower: {wgs[7]:.0f} m apart, {wgs[3]:.3f} km away")
check(r[0][6] in ("Saudi Arabia", "المملكة العربية السعودية", "السعودية"), f"country of the best: {r[0][6]}")
check(r[0][3] < 0.01, f"best one is on the clicked point ({r[0][3]:.4f} km)")

# ---- WGS 84 UTM 37N in Jeddah, location given in Web Mercator ---------------------
lyr = no_crs_layer(project(JEDDAH, "EPSG:32637"))
merc = project([JEDDAH[0]], "EPSG:3857")[0]
res = processing.run("biruni:guesscrs", {"INPUT": lyr, "LOCATION": f"{merc[0]},{merc[1]} [EPSG:3857]",
                                         "OUTPUT": "memory:"}, context=ctx, feedback=fb)
r = rows(res["OUTPUT"])
same = {"EPSG:32637", "EPSG:9357", "EPSG:8837"}
check(res["TOP_CRS"] in same, f"Jeddah: WGS 84-based UTM 37N ({res['TOP_CRS']})")
twins = [x for x in r[1:6] if x[1] in same]
check(twins and all("same position" in x[10] for x in twins), "equivalent systems are marked as the same position")
aea = next(x for x in r if x[1] == "EPSG:20437" and not x[4])
check(r.index(aea) > 0 and aea[7] > 50, f"Ain el Abd 37N differs by {aea[7]:.0f} m")

# ---- Egypt Red Belt in Cairo, location as an area ----------------------------------
lyr = no_crs_layer(project(CAIRO, "EPSG:22992"))
res = processing.run("biruni:guesscrs", {"INPUT": lyr, "EXTENT": "31.20,31.30,30.00,30.10 [EPSG:4326]",
                                         "OUTPUT": "memory:"}, context=ctx, feedback=fb)
r = rows(res["OUTPUT"])
check(r[0][3] == 0, "Cairo: best one inside the given area")
check(any(x[1] == "EPSG:22992" and x[3] == 0 for x in r[:15]), "Egypt Red Belt among the systems inside the area")
check(r[0][1] != "EPSG:22991" and all(x[1] != "EPSG:22991" or x[3] > 0 for x in r), "Blue Belt does not fit Cairo")

# ---- pasted northing first (X and Y swapped) --------------------------------------
xy = project(RIYADH[:8], "EPSG:32638")
text = "\n".join(f"{i + 1}\t{y:.2f}\t{x:.2f}" for i, (x, y) in enumerate(xy))
res = processing.run("biruni:guesscrs", {"TEXT": text, "LOCATION": f"{RIYADH[0][0]},{RIYADH[0][1]} [EPSG:4326]",
                                         "OUTPUT": "memory:"}, context=ctx, feedback=fb)
check(res["TOP_SWAPPED"] and res["TOP_CRS"] in {"EPSG:32638", "EPSG:9358", "EPSG:8838"},
      f"swapped UTM found ({res['TOP_CRS']}, swapped={res['TOP_SWAPPED']})")

# ---- degrees: latitude first --------------------------------------------------------
text = "\n".join(f"{la:.6f}, {lo:.6f}" for lo, la in RIYADH[:5])
res = processing.run("biruni:guesscrs", {"TEXT": text, "LOCATION": f"{RIYADH[0][0]},{RIYADH[0][1]} [EPSG:4326]",
                                         "OUTPUT": "memory:"}, context=ctx, feedback=fb)
check(res["TOP_CRS"] == "EPSG:4326" and res["TOP_SWAPPED"], f"lat, lon degrees ({res['TOP_CRS']})")

# ---- no location -----------------------------------------------------------------------
text = "\n".join(f"{lo:.6f} {la:.6f}" for lo, la in CAIRO)
res = processing.run("biruni:guesscrs", {"TEXT": text, "OUTPUT": "memory:"}, context=ctx, feedback=fb)
r = rows(res["OUTPUT"])
best = QgsCoordinateReferenceSystem(res["TOP_CRS"])
check(best.isGeographic() and not res["TOP_SWAPPED"], f"no location: degrees read as degrees ({res['TOP_CRS']})")
check(r[0][6] in ("Egypt", "مصر"), f"no location: lands in Egypt ({r[0][6]})")
check(all(x[3] is None or x[3] != x[3] for x in r[:3]), "no location: no distance")
lyr = no_crs_layer(project(RIYADH, "EPSG:32638"))
res = processing.run("biruni:guesscrs", {"INPUT": lyr, "OUTPUT": "memory:"}, context=ctx, feedback=fb)
r = rows(res["OUTPUT"])
check(r[0][6] != "" and r[0][5], f"no location: best one on land and in its area ({r[0][1]} {r[0][6]})")
check(any(x[6] in ("Saudi Arabia", "السعودية", "المملكة العربية السعودية") for x in r[:10]),
      "no location: a Saudi system in the first ten")

# ---- give the best system to a copy ------------------------------------------------------
lyr = no_crs_layer(project(RIYADH[:5], "EPSG:20438"))
res = processing.run("biruni:guesscrs", {"INPUT": lyr, "LOCATION": f"{RIYADH[0][0]},{RIYADH[0][1]} [EPSG:4326]",
                                         "ASSIGN": True, "OUTPUT": "memory:", "OUTPUT_LAYER": "memory:"},
                     context=ctx, feedback=fb)
copy = out(res["OUTPUT_LAYER"])
src = [f.geometry().asPoint() for f in lyr.getFeatures()]
dst = [f.geometry().asPoint() for f in copy.getFeatures()]
check(copy.crs().authid() == "EPSG:20438" and copy.featureCount() == 5, f"copy has the best system ({copy.crs().authid()})")
check(all(abs(a.x() - b.x()) < 1e-6 and abs(a.y() - b.y()) < 1e-6 for a, b in zip(src, dst)),
      "copy: coordinates unchanged")
swapped = no_crs_layer([(y, x) for x, y in project(RIYADH[:5], "EPSG:20438")])
res = processing.run("biruni:guesscrs", {"INPUT": swapped, "LOCATION": f"{RIYADH[0][0]},{RIYADH[0][1]} [EPSG:4326]",
                                         "OUTPUT": "memory:", "OUTPUT_LAYER": "memory:"}, context=ctx, feedback=fb)
p0 = next(out(res["OUTPUT_LAYER"]).getFeatures()).geometry().asPoint()
s0 = next(swapped.getFeatures()).geometry().asPoint()
check(res["TOP_SWAPPED"] and abs(p0.x() - s0.y()) < 1e-6, "copy: swapped X and Y put back")
try:
    processing.run("biruni:guesscrs", {"TEXT": "no numbers here", "OUTPUT": "memory:"}, context=ctx, feedback=fb)
    check(False, "no coordinates: an error")
except Exception as e:  # noqa: BLE001
    check("X Y" in str(e) or "coordinates" in str(e), "no coordinates: a clear error")

# ==== WhatsApp ===========================================================================
ANDROID_EN = """12/31/23, 9:15 PM - Messages and calls are end-to-end encrypted. No one outside of this chat, not even WhatsApp, can read or listen to them. Tap to learn more.
12/31/23, 9:16 PM - Ahmed Ali: location: https://maps.google.com/?q=24.713600,46.675300
12/31/23, 9:16 PM - Ahmed Ali: pole number 15, the base is broken
12/31/23, 9:20 PM - Sara: ok
1/2/24, 8:05 AM - Ahmed Ali: Meet here
https://www.google.com/maps/@24.7201,46.6802,17z
1/2/24, 8:30 AM - Omar: https://maps.app.goo.gl/AbCdEf123
1/2/24, 8:31 AM - Omar: live location shared
1/2/24, 9:00 AM - Sara: 24.7300, 46.6900 kerb damaged
1/2/24, 9:01 AM - Sara: IMG-20240102-WA0001.jpg (file attached)
1/13/24, 10:00 AM - Omar: The site is at 24°42'49.0"N 46°40'31.1"E
1/13/24, 10:05 AM - Omar: total 1,250.500 riyals for 3.75 m
"""
pts, info = wa.extract(ANDROID_EN)
check(info["order"] == "mdy" and not info["order_guessed"], f"Android EN: month first detected ({info['order']})")
check(info["messages"] == 11, f"Android EN: 11 messages ({info['messages']})")
check(len(pts) == 4, f"Android EN: 4 locations ({len(pts)})")
p = pts[0]
check(abs(p["lat"] - 24.7136) < 1e-9 and abs(p["lon"] - 46.6753) < 1e-9, "Android EN: ?q= link")
check(p["sender"] == "Ahmed Ali" and p["sent_at"] == datetime(2023, 12, 31, 21, 16), f"sender/time: {p['sent_at']}")
check(p["context"] == "pole number 15, the base is broken", f"context from next message: {p['context']}")
check(p["link"].startswith("https://maps.google.com") and p["line"] == 2, "link and chat line kept")
check(abs(pts[1]["lat"] - 24.7201) < 1e-9 and pts[1]["context"] == "Meet here" and pts[1]["line"] == 5,
      f"multi-line message with @lat,lng link ({pts[1]['context']})")
check(pts[2]["sender"] == "Sara" and abs(pts[2]["lon"] - 46.69) < 1e-9 and pts[2]["context"] == "kerb damaged",
      f"typed decimal pair with words ({pts[2]['context']})")
check(abs(pts[3]["lat"] - (24 + 42 / 60 + 49 / 3600)) < 1e-6 and pts[3]["sent_at"] == datetime(2024, 1, 13, 10, 0),
      "typed DMS pair")
check(len(info["short"]) == 1 and info["short"][0][2] == "https://maps.app.goo.gl/AbCdEf123", "short link reported")
check(len(info["live"]) == 1 and info["live"][0][1] == "Omar", "live location reported")

ANDROID_AR = ("٣١‏/١٢‏/٢٠٢٣ ٩:١٥ م - ‏الرسائل والمكالمات مشفرة تمامًا بين الطرفين.\n"
              "٣١‏/١٢‏/٢٠٢٣، ٩:١٦ م - محمد حسن: الموقع: https://maps.google.com/?q=30.044400,31.235700\n"
              "٣١‏/١٢‏/٢٠٢٣ ٩:١٧ م - محمد حسن: ده عمود رقم ١٥\n"
              "٠١‏/٠١‏/٢٠٢٤ ١٠:٠٠ ص - خالد: ‏تمت مشاركة الموقع المباشر\n"
              "١٥‏/٠١‏/٢٠٢٤ ١١:٣٠ ص - خالد: ٣٠٫٠٥١٢، ٣١٫٢٤٠١\n"
              "١٥‏/٠١‏/٢٠٢٤ ١١:٣١ ص - خالد: بردورة مكسورة هنا\n"
              "١٥‏/٠١‏/٢٠٢٤ ١٢:٠٥ ص - خالد: آخر واحد\n"
              "سطر تاني في نفس الرسالة\n")
pts, info = wa.extract(ANDROID_AR)
check(info["order"] == "dmy" and info["messages"] == 7, f"Android AR: day first, 7 messages ({info['order']})")
check(len(pts) == 2 and pts[0]["sender"] == "محمد حسن", f"Android AR: 2 locations ({len(pts)})")
check(pts[0]["sent_at"] == datetime(2023, 12, 31, 21, 16), f"Arabic digits and م = PM ({pts[0]['sent_at']})")
check(pts[0]["context"] == "ده عمود رقم ١٥", f"Arabic context ({pts[0]['context']})")
check(abs(pts[1]["lat"] - 30.0512) < 1e-9 and abs(pts[1]["lon"] - 31.2401) < 1e-9,
      "typed Arabic-digit pair with ٫ and ،")
check(pts[1]["context"] == "بردورة مكسورة هنا" and pts[1]["sent_at"] == datetime(2024, 1, 15, 11, 30),
      "ص = AM, context")
check(len(info["live"]) == 1 and info["live"][0][1] == "خالد", "Arabic live location reported")
msgs = wa.parse_messages(ANDROID_AR)
check(msgs[-1]["text"].endswith("سطر تاني في نفس الرسالة") and wa.to_datetime(msgs[-1], "dmy").hour == 0,
      "12:05 ص = 00:05, continuation line joined")

IOS_EN = ("[31/12/2023, 21:15:30] Site Team: ‎Messages and calls are end-to-end encrypted.\n"
          "[31/12/2023, 21:16:02] Ahmed: ‎Location: https://maps.google.com/?q=21.485800,39.192500\n"
          "[31/12/2023, 21:16:40] Ahmed: Manhole cover missing\n"
          "[01/01/2024, 07:05:11] Sara: https://maps.apple.com/?ll=21.4901,39.1855&q=Dropped%20Pin\n"
          "[01/01/2024, 07:06:00] Sara: ‎<attached: 00000012-PHOTO-2024-01-01-07-06-00.jpg>\n"
          "[01/01/2024, 07:07:00] Sara: https://www.openstreetmap.org/?mlat=21.5&mlon=39.2#map=18/21.5/39.2\n")
pts, info = wa.extract(IOS_EN)
check(info["order"] == "dmy" and len(pts) == 3, f"iOS EN: day first, 3 locations ({len(pts)})")
check(pts[0]["context"] == "Manhole cover missing" and pts[0]["sent_at"] == datetime(2023, 12, 31, 21, 16, 2),
      "iOS: seconds and context")
check(abs(pts[1]["lat"] - 21.4901) < 1e-9 and pts[1]["context"] == "", "Apple Maps link, media is not context")
check(abs(pts[2]["lon"] - 39.2) < 1e-9, "OpenStreetMap link")

IOS_12H = ("[12/31/23, 9:16:02 PM] Ahmed: ‎Location: https://maps.google.com/maps?q=loc:21.4858%2C39.1925\n"
           "[1/2/24, 12:01:00 AM] Ahmed: geo:21.49,39.19\n"
           "[1/2/24, 8:01:00 a.m.] Ahmed: https://www.google.com/maps/search/21.4,+39.1?entry=tts\n")
pts, info = wa.extract(IOS_12H)
check(len(pts) == 3 and pts[0]["sent_at"] == datetime(2023, 12, 31, 21, 16, 2),
      f"iOS 12 h with narrow space, q=loc: and %2C ({len(pts)})")
check(pts[1]["sent_at"] == datetime(2024, 1, 2, 0, 1) and abs(pts[1]["lat"] - 21.49) < 1e-9, "geo: link, 12 AM")
check(pts[2]["sent_at"] == datetime(2024, 1, 2, 8, 1) and abs(pts[2]["lon"] - 39.1) < 1e-9, "a.m. and /search/")
check(info["order"] == "mdy", "iOS 12 h: month first")

IOS_AR = ("[‏٣١‏/١٢‏/٢٠٢٣، ٩:١٦:٠٢ م] أحمد: ‏الموقع: https://maps.google.com/?q=24.5,46.5\n"
          "[‏٣١‏/١٢‏/٢٠٢٣، ٩:١٧:٠٠ م] أحمد: غرفة تفتيش\n")
pts, info = wa.extract(IOS_AR)
check(len(pts) == 1 and pts[0]["sent_at"] == datetime(2023, 12, 31, 21, 16, 2) and pts[0]["context"] == "غرفة تفتيش",
      "iOS Arabic")

AMBIG = ("01/02/2024, 10:00 - A: location: https://maps.google.com/?q=24.1,46.1\n"
         "03/04/2024, 10:00 - B: location: https://maps.google.com/?q=24.2,46.2\n")
pts, info = wa.extract(AMBIG)
check(info["order_guessed"] and pts[0]["sent_at"] == datetime(2024, 2, 1, 10, 0), "ambiguous dates: day first")
pts, info = wa.extract(AMBIG, wa.MONTH_FIRST)
check(not info["order_guessed"] and pts[0]["sent_at"] == datetime(2024, 1, 2, 10, 0), "month first when chosen")
pts, _ = wa.extract(ANDROID_EN, sender_filter="sara")
check(len(pts) == 1 and pts[0]["sender"] == "Sara", "sender filter")
pts, _ = wa.extract(ANDROID_EN, date_from=date(2024, 1, 1), date_to=date(2024, 1, 5))
check(len(pts) == 2, f"date filter ({len(pts)})")
check(wa.find_location("price 12.500, 3.750 SAR") is not None, "known limit: decimal pairs look like coordinates")
check(wa.find_location("meeting at 10.30, room 4") is None, "times and short numbers are not coordinates")
check(wa.find_location("https://maps.google.com/?q=0,0") is None, "0,0 is not a location")

# ---- through Processing: .txt and .zip ------------------------------------------------
txt = os.path.join(TMP, "WhatsApp Chat with Site Team.txt")
with open(txt, "w", encoding="utf-8-sig", newline="\n") as f:
    f.write(ANDROID_EN)
res = processing.run("biruni:whatsapplocations", {"INPUT": txt, "OUTPUT": "memory:"}, context=ctx, feedback=fb)
lyr = out(res["OUTPUT"])
check(res["POINT_COUNT"] == 4 and lyr.featureCount() == 4 and lyr.crs().authid() == "EPSG:4326",
      "txt: 4 points in WGS 84")
check(res["SHORT_LINK_COUNT"] == 1 and res["LIVE_COUNT"] == 1 and res["MESSAGE_COUNT"] == 11, "txt: counts")
f0 = sorted(lyr.getFeatures(), key=lambda f: f["chat_line"])[0]
check(f0["sender"] == "Ahmed Ali" and f0["sent_at"].toString("yyyy-MM-dd HH:mm") == "2023-12-31 21:16"
      and f0.geometry().asPoint().y() == 24.7136, "txt: attributes and geometry")
zp = os.path.join(TMP, "WhatsApp Chat - Team.zip")
with zipfile.ZipFile(zp, "w") as z:
    z.writestr("_chat.txt", IOS_EN.encode("utf-8"))
    z.writestr("00000012-PHOTO-2024-01-01-07-06-00.jpg", b"\xff\xd8\xff")
res = processing.run("biruni:whatsapplocations", {"INPUT": zp, "SENDER": "Sara", "OUTPUT": "memory:"},
                     context=ctx, feedback=fb)
check(res["POINT_COUNT"] == 2, f"zip (iOS) with media, sender filter ({res['POINT_COUNT']})")
res = processing.run("biruni:whatsapplocations", {"INPUT": txt, "DATE_FROM": "2024-01-10", "OUTPUT": "memory:"},
                     context=ctx, feedback=fb)
check(res["POINT_COUNT"] == 1, f"processing date filter ({res['POINT_COUNT']})")
u16 = os.path.join(TMP, "chat16.txt")
with open(u16, "w", encoding="utf-16") as f:
    f.write(ANDROID_AR)
res = processing.run("biruni:whatsapplocations", {"INPUT": u16, "OUTPUT": "memory:"}, context=ctx, feedback=fb)
check(res["POINT_COUNT"] == 2, "UTF-16 file")
bad = os.path.join(TMP, "notes.txt")
with open(bad, "w", encoding="utf-8") as f:
    f.write("just some notes\n24.7, 46.6\n")
try:
    processing.run("biruni:whatsapplocations", {"INPUT": bad, "OUTPUT": "memory:"}, context=ctx, feedback=fb)
    check(False, "not a chat: an error")
except Exception as e:  # noqa: BLE001
    check("WhatsApp" in str(e), "not a chat: a clear error")

print()
print(f"{len(failures)} failure(s)" if failures else "ALL PASS")
sys.exit(1 if failures else 0)
