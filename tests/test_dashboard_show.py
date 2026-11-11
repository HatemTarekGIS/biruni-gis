"""Dashboard extension "show" (PC side): the narrative (Arabic grammar, nothing hidden, the card's zone, k_min), the
forecast (accuracy and 80 % interval coverage on synthetic series), the phone wall (tiling maths and view sync),
the project film (frames in order, the card's zone, a hidden date field refused) and the meeting mode (PIN,
presenter only, broadcast). The page side runs in its own process: tests\\test_dashboard_show_page.py.

Run:  engine\\bin\\python-qgis.bat tests\\test_dashboard_show.py
"""
import json
import math
import os
import random
import subprocess
import sys
import tempfile
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "app", "plugins"))
sys.path.insert(0, os.path.join(HERE, "..", "app", "python"))

from qgis.core import (QgsApplication, QgsFeature, QgsField, QgsGeometry, QgsPointXY,  # noqa: E402
                       QgsProject, QgsVectorLayer)
from qgis.PyQt.QtCore import QDate, QMetaType  # noqa: E402

os.environ["QGIS_CUSTOM_CONFIG_PATH"] = tempfile.mkdtemp()
app = QgsApplication([], True)
app.initQgis()

from biruni.dashboard import cards, model, seals, service  # noqa: E402
from biruni.dashboard import ext_show, film, forecast, meeting, narrative, showkit, wall  # noqa: E402

failures = []


def check(cond, msg):
    print(("PASS " if cond else "FAIL ") + msg, flush=True)
    if not cond:
        failures.append(msg)


cards._TEST_SEED[:] = [bytes(range(32))]
seals._PATH[:] = [os.path.join(tempfile.mkdtemp(), "seals.sqlite")]
showkit.set_folder(tempfile.mkdtemp())
prj = QgsProject.instance()
check("show_story" in service.HANDLERS and "show_wall_join" in service.HANDLERS and "show_meet_view" in service.HANDLERS,
      "the extension registered its handlers with the service")

# ---- data ---------------------------------------------------------------------------------------------------------
NAMES = ["North", "South", "East", "West"]
parcels = QgsVectorLayer("Polygon?crs=EPSG:4326", "parcels", "memory")
pr = parcels.dataProvider()
pr.addAttributes([QgsField("district", QMetaType.Type.QString), QgsField("status", QMetaType.Type.QString),
                  QgsField("day", QMetaType.Type.QDate), QgsField("owner", QMetaType.Type.QString),
                  QgsField("crew", QMetaType.Type.QString)])
parcels.updateFields()
rnd = random.Random(3)
feats = []
for i in range(240):
    d = i % 4
    x, y = d + (i // 4 % 10) * 0.09 + 0.02, (i // 40) * 0.15 + 0.02
    f = QgsFeature(parcels.fields())
    f.setGeometry(QgsGeometry.fromWkt(f"POLYGON(({x} {y},{x + .07} {y},{x + .07} {y + .1},{x} {y + .1},{x} {y}))"))
    f.setAttributes([NAMES[d], "done" if rnd.random() < 0.55 else "open", QDate(2026, 10, 1).addDays(rnd.randrange(30)),
                     f"SECRET-OWNER-{i}", ["Mona", "Sami", "Hadi"][i % 3]])
    feats.append(f)
pr.addFeatures(feats)
prj.addMapLayers([parcels])


def W(id_, type_, **k):
    w = {"id": id_, "type": type_, "title": id_, "x": 0, "y": 0, "w": 3, "h": 2, "source": "s1"}
    w.update(k)
    return w


D = model.blank("Show")
D.update({"id": "d_show", "sources": {"s1": {"layer": parcels.id(), "filter": ""}},
          "widgets": [
              W("kpi", "kpi", title="Parcels done", measure={"op": "count", "where": "status = 'done'"}),
              W("prog", "progress", title="By district", group={"field": "district"},
                options={"done": "status = 'done'"}),
              W("lead", "leaderboard", title="Team", measure={"op": "count"}, group={"field": "crew"}),
              W("map", "map", title="Map", w=8, h=6,
                options={"layers": [{"source": "s1", "style": {"kind": "categories", "field": "status"},
                                     "label": "district"}], "film_date": "day"}),
              W("fc", "text", title="When we finish", source=None, options={"text": ""},
                style={"show": "forecast", "of": "prog"}),
          ]})
probs = model.validate(D)
check(not probs, f"the test dashboard (forecast as a text widget) is valid {probs}")
model.save(prj, D)

north = "POLYGON((0 0,1 0,1 1,0 1,0 0))"
c_all = cards.issue("d_show", "Manager")
c_north = cards.issue("d_show", "Supervisor North", zone_wkt=north, hide=["owner"])
c_nodate = cards.issue("d_show", "No dates", hide=["day"])

svc = service.DashboardService(prj)


def session(card):
    box = []
    s = svc.connect(box.append, "lan")
    s.receive({"t": "hello", "card": card})
    return s, box


# ==== 1. narrative ====================================================================================================
check(narrative.span(86400, "ar") == "يوم واحد" and narrative.span(2 * 86400, "ar") == "يومين"
      and narrative.span(5 * 86400, "ar") == "5 أيام" and narrative.span(11 * 86400, "ar") == "11 يومًا"
      and narrative.span(100 * 86400, "ar") == "100 يوم", "Arabic day counts agree (يوم واحد، يومين، 5 أيام، 11 يومًا، 100 يوم)")
check(narrative.span(3600, "ar") == "ساعة واحدة" and narrative.span(2 * 3600, "ar") == "ساعتين"
      and narrative.span(3 * 3600, "ar") == "3 ساعات" and narrative.span(120, "ar") == "دقيقتين",
      "Arabic hours and minutes agree")
titles = {"k": "القطع المنجزة", "p": "حسب الحي", "l": "الفريق"}
prev = {"k": {"type": "kpi", "v": 100}, "p": {"type": "progress", "prog": {"a": [10, 20], "b": [5, 20], "c": [2, 20]},
                                                "labels": {"a": "حي الريحان", "b": "حي الندى", "c": "حي السدرة"}},
        "l": {"type": "leaderboard", "items": {"m": 5, "s": 9}, "labels": {"m": "منى", "s": "سامي"}}}
cur = {"k": {"type": "kpi", "v": 137}, "p": {"type": "progress", "prog": {"a": [20, 20], "b": [9, 20], "c": [2, 20]},
                                               "labels": {"a": "حي الريحان", "b": "حي الندى", "c": "حي السدرة"}},
       "l": {"type": "leaderboard", "items": {"m": 14, "s": 10}, "labels": {"m": "منى", "s": "سامي"}}}
now = time.time()
ar = narrative.story(prev, cur, titles, [], "ar", now, now - 3 * 86400)
print("   " + "\n   ".join(ar))
check(ar[0] == "منذ زيارتك الأخيرة قبل 3 أيام:", "the Arabic story opens with the time since the last visit")
check("ارتفع «القطع المنجزة» بمقدار 37 ليبلغ 137." in ar, "a rising number: ارتفع … بمقدار … ليبلغ …")
check(any("أكثر المناطق تقدّمًا: حي الريحان" in s for s in ar) and "اكتمل العمل في حي الريحان." in ar,
      "the zone that moved most and the zone finished")
check(any(s.startswith("نسبة الإنجاز في حي السدرة 10٪ فقط") for s in ar), "the zone behind the average")
check(any("صدارة «الفريق» الآن لـمنى بعد أن كانت لـسامي." == s for s in ar),
      "a change of leader, phrased without the leader's gender")
en = narrative.story(None, cur, titles, [], "en", now)
check(en[0].startswith("Your first visit") and any(s.startswith("“القطع المنجزة”: 137") for s in en),
      "a first visit lists the main numbers")
fall = narrative.story({"k": {"type": "kpi", "v": 140}}, {"k": {"type": "kpi", "v": 137}}, titles, [], "ar", now,
                       now - 3600)
check("انخفض «القطع المنجزة» بمقدار 3 إلى 137." in fall and fall[0] == "منذ زيارتك الأخيرة قبل ساعة واحدة:",
      "a falling number and one hour ago")
vals, acc, hist = [2, 3, 1, 2, 3, 2, 1, 2, 3, 2], 100, []
for d in range(1, 11):
    acc += vals[d - 1]
    hist.append((f"2026-10-{d:02d}", 0, {"k": {"type": "kpi", "v": acc}}))
hist.append(("2026-10-11", 0, {"k": {"type": "kpi", "v": 160}}))
rec = narrative.story({"k": {"type": "kpi", "v": 120}}, {"k": {"type": "kpi", "v": 160}}, titles, hist, "ar", now,
                      now - 86400)
check(any("عند أعلى قيمة مسجّلة حتى الآن" in s for s in rec), "a record (highest so far)")
check(any("أكثر بكثير من المعتاد" in s for s in rec), "an unusual day (z-score)")
check(narrative.story(prev, cur, {"k": "x"}, [], "ar", now, now - 60)[1:] and
      not any("الريحان" in s for s in narrative.story(prev, cur, {"k": "x"}, [], "ar", now, now - 60)),
      "widgets the card may not see are never told")

s_all, b_all = session(c_all)
s_n, b_n = session(c_north)
b_n.clear()
s_n.receive({"t": "show_story", "lang": "ar"})
story_n = next(m for m in b_n if m["t"] == "show_story")
check(story_n["first"] and story_n["lang"] == "ar" and story_n["lines"][0].startswith("هذه زيارتك الأولى"),
      "the first visit through the service")
txt = json.dumps(story_n, ensure_ascii=False)
check("SECRET" not in txt and "South" not in txt and "West" not in txt and "East" not in txt,
      "the zone card's story names only its own district and no hidden value")
parcels.startEditing()
nf = [f.id() for f in parcels.getFeatures() if f["district"] == "North" and f["status"] == "open"][:3]
for fid in nf:
    parcels.changeAttributeValue(fid, parcels.fields().indexOf("status"), "done")
svc.flush()
b_n.clear()
s_n.receive({"t": "show_story", "lang": "ar"})
story2 = next(m for m in b_n if m["t"] == "show_story")
print("   " + "\n   ".join(story2["lines"]))
check(not story2["first"] and any("ارتفع «Parcels done» بمقدار 3" in s for s in story2["lines"]),
      "the second visit tells what moved since the first (3 parcels done)")
parcels.rollBack()
svc.flush()
check(showkit.history("d_show", s_n.card), "the history keeps what the card sees")
# k_min: a dashboard whose groups are all smaller than k_min tells nothing about them
D2 = json.loads(json.dumps(D))
D2.update({"id": "d_kmin", "k_min": 1000})
model.save(prj, D2)
svc.reload()
s_k, b_k = session(cards.issue("d_kmin", "k"))
b_k.clear()
s_k.receive({"t": "show_story", "lang": "en"})
sk = next(m for m in b_k if m["t"] == "show_story")
check(not any(n in json.dumps(sk) for n in NAMES) and not any(n in json.dumps(sk) for n in ("Mona", "Sami", "Hadi")),
      "k_min: groups too small to show are never told")

# ==== 2. forecast =====================================================================================================
DAY = 86400.0
cover, errs = 0, []
rng = random.Random(7)


def poisson(lam):
    L, k, p = math.exp(-lam), 0, 1.0
    while True:
        p *= rng.random()
        if p <= L:
            return k
        k += 1


RUNS, RATE, TOTAL = 300, 6.0, 260
t0 = 1.8e9
for _ in range(RUNS):
    done, series = 0, []
    for d in range(14):
        done += poisson(RATE)
        series.append((t0 + d * DAY, done))
    f = forecast.finish(series, TOTAL)
    left, day, x = TOTAL - done, 13, done
    while x < TOTAL:     # the future, day by day, at the same true rate
        day += 1
        x += poisson(RATE)
    truth = t0 + day * DAY
    if f and f["lo"] - DAY <= truth <= f["hi"] + DAY:
        cover += 1
    if f:
        errs.append(abs(f["eta"] - truth) / DAY)
cov = cover / RUNS
print(f"   forecast: 80% interval covered the true finish in {cov:.0%} of {RUNS} runs; median error "
      f"{sorted(errs)[len(errs) // 2]:.1f} days")
check(0.72 <= cov <= 0.97, f"the 80% interval covers the true finish about 80% of the time ({cov:.0%})")
check(sorted(errs)[len(errs) // 2] < 3, "the expected finish is within a few days (median error)")
check(forecast.finish([(t0, 5), (t0 + DAY, 6)], 100) is None, "too little history gives no forecast")
check(forecast.finish([(t0 + d * DAY, 100) for d in range(5)], 100)["done"], "a finished zone is finished")
stalled = forecast.finish([(t0 + d * DAY, 10) for d in range(6)], 100)
check(stalled and stalled["eta"] is None, "a zone with no progress has no date")
ts = forecast.theil_sen([(0, 0), (1, 2), (2, 4), (3, 100), (4, 8)])
check(abs(ts - 2) < 1e-9, "Theil–Sen ignores one wild day")
# through the service: history from showkit
days = {}
for d in range(10):
    day = time.strftime("%Y-%m-%d", time.localtime(time.time() - (10 - d) * DAY))
    days[day] = {"prog": {"type": "progress", "xf": "district", "labels": {"North": "North"},
                          "prog": {"North": [5 + d * 3, 60]}}}
showkit.put_history("d_show", s_n.card, days)
b_n.clear()
s_n.receive({"t": "show_forecast", "widget": "fc"})
fm = next(m for m in b_n if m["t"] == "show_forecast")
check(fm["of"] == "prog" and fm["xf"] == "district" and [z["key"] for z in fm["zones"]] == ["North"] and
      fm["zones"][0]["eta"] and fm["overall"]["cone"], "the forecast message: zones, overall, cone, the field to colour")
check("South" not in json.dumps(fm), "the forecast of a zone card has only its zone")
s_n.receive({"t": "show_forecast", "widget": "kpi"})
check(b_n[-1] == {"t": "show_err", "why": "no forecast"}, "a widget that is not a forecast is refused")

# ==== 3. the phone wall ==============================================================================================
tiles, virt = wall.layout({0: (400, 800), 1: (400, 800), 2: (400, 800)}, 1, 3, 16)
check(tiles[1] == (416.0, 0.0, 400, 800) and tiles[2][0] == 832.0 and virt == (1232.0, 800.0),
      "three phones in a row: tiles and the virtual canvas with the gaps")
tiles2, virt2 = wall.layout({0: (360, 740), 1: (412, 892), 2: (390, 844), 3: (412, 892)}, 2, 2, 10)
check(virt2 == (390 + 412 + 10, 892 + 892 + 10) and tiles2[0][0] == (390 - 360) / 2,
      "phones of different sizes: a column as wide as its widest phone, smaller ones centred")
v = {"cx": 1000.0, "cy": 5000.0, "mpp": 2.0}
p0, p1 = wall.phone_view(v, tiles[0], virt), wall.phone_view(v, tiles[1], virt)
check(abs((p1["cx"] - p0["cx"]) - 416 * 2.0) < 1e-9 and p0["cy"] == p1["cy"] and p0["mpp"] == v["mpp"],
      "neighbouring phones are offset by their tile distance at the same scale")
mid = wall.phone_view(v, tiles[1], virt)
check(abs(mid["cx"] - v["cx"]) < 1e-9, "the middle phone shows the wall's centre")
ext_show.start_wall(svc, "d_show", 1, 2)
s_w1, b_w1 = session(c_all)
s_w2, b_w2 = session(c_north)
s_w3, b_w3 = session(c_all)
s_w1.receive({"t": "show_wall_join", "w": 400, "h": 800})
s_w2.receive({"t": "show_wall_join", "w": 390, "h": 844})
w1 = [m for m in b_w1 if m["t"] == "show_wall"][-1]
w2 = [m for m in b_w2 if m["t"] == "show_wall"][-1]
check(w1["slot"] == 0 and w2["slot"] == 1 and w1["virtual"] == w2["virtual"] and w2["tile"][0] > w1["tile"][0],
      "two phones join: slots 0 and 1 on one virtual canvas")
s_w3.receive({"t": "show_wall_join", "w": 400, "h": 800})
check(b_w3[-1] == {"t": "show_err", "why": "wall full"}, "a third phone on a 1×2 wall is refused")
b_w2.clear()
s_w1.receive({"t": "show_wall_view", "view": {"cx": 10.0, "cy": 20.0, "mpp": 1.5, "xf": ["district", "North"]}})
check(b_w2 and b_w2[-1]["t"] == "show_wall_view" and b_w2[-1]["view"]["mpp"] == 1.5 and
      b_w2[-1]["view"]["xf"] == ["district", "North"], "a phone's view reaches the other phones of the wall")
s_w3.receive({"t": "show_wall_view", "view": {"cx": 1, "cy": 1, "mpp": 1}})
check(b_w3[-1] == {"t": "show_err", "why": "not on the wall"}, "a page that is not on the wall cannot move it")
s_w2.receive({"t": "show_wall_slot", "slot": 0})
check([m for m in b_w2 if m["t"] == "show_wall"][-1]["slot"] == 0 and
      [m for m in b_w1 if m["t"] == "show_wall"][-1]["slot"] == 1, "moving to a taken slot swaps the two phones")
s_w1.receive({"t": "show_wall_leave"})
check(b_w1[-1]["t"] == "show_wall_end" and [m for m in b_w2 if m["t"] == "show_wall"][-1]["n"] == 1,
      "leaving the wall: the others are told the new layout")

# ==== 4. the project film ============================================================================================
fr = film.frames_from_changes([(t0 + 2 * DAY, "L0", "1", "#0f0"), (t0, "L0", "1", "#f00"),
                               (t0 + DAY, "L0", "2", "#00f"), (t0 + 2 * DAY + 60, "L0", "1", "#ff0")])
check([f["day"] for f in fr] == sorted(f["day"] for f in fr) and len(fr) == 3 and fr[-1]["ch"] == [["L0", "1", "#ff0"]],
      "frames are one per day, in order, with the last colour of the day")
b_all.clear()
s_all.receive({"t": "show_film", "widget": "map"})
fa = b_all[-1]
check(fa["t"] == "show_film" and fa["count"] == parcels.featureCount() and
      [f["t"] for f in fa["frames"]] == sorted(f["t"] for f in fa["frames"]),
      f"the film from the date field: every parcel once, days in order ({fa.get('count')})")
b_n.clear()
s_n.receive({"t": "show_film", "widget": "map"})
fn = b_n[-1]
north_ids = {str(f.id()) for f in parcels.getFeatures() if f["district"] == "North"}
check(fn["t"] == "show_film" and {c[1] for f in fn["frames"] for c in f["ch"]} <= north_ids and fn["count"] > 0,
      "a zone card's film has only its own parcels")
s_d, b_d = session(c_nodate)
b_d.clear()
s_d.receive({"t": "show_film", "widget": "map"})
check(b_d[-1] == {"t": "show_err", "why": "no film"}, "a card that hides the film's date field gets no film")
s_all.receive({"t": "show_film", "widget": "kpi"})
check(b_all[-1]["t"] == "show_err", "a widget that is not a map has no film")

# ==== 5. meeting mode ================================================================================================
s_p, b_p = session(c_all)
s_v, b_v = session(c_north)
s_p.receive({"t": "show_meet_claim", "pin": "123456"})
check(b_p[-1] == {"t": "show_err", "why": "no meeting"}, "no meeting: nobody can present")
m = ext_show.start_meeting(svc, "d_show")
check(len(m.pin) == 6 and m.pin.isdigit(), "the computer shows a 6-digit PIN")
check(any(x.get("t") == "show_meet_state" and x.get("active") for x in b_v), "every viewer is told a meeting began")
s_v.receive({"t": "show_meet_view", "view": {"cx": 1, "cy": 2, "mpp": 3}})
check(b_v[-1] == {"t": "show_err", "why": "not the presenter"}, "a viewer cannot drive the others")
s_v.receive({"t": "show_meet_laser", "lon": 1, "lat": 1})
check(b_v[-1] == {"t": "show_err", "why": "not the presenter"}, "a viewer has no laser")
wrong = "000000" if m.pin != "000000" else "111111"
s_v.receive({"t": "show_meet_claim", "pin": wrong})
check(b_v[-1] == {"t": "show_err", "why": "wrong pin"}, "a wrong PIN is refused")
for _ in range(5):
    s_v.receive({"t": "show_meet_claim", "pin": wrong})
s_v.receive({"t": "show_meet_claim", "pin": m.pin})
check(b_v[-1] == {"t": "show_err", "why": "too many tries"}, "after five wrong tries even the right PIN is refused")
s_p.receive({"t": "show_meet_claim", "pin": m.pin})
check(b_p[-1]["t"] == "show_meet_state" and b_p[-1]["you"] and b_v[-1]["presenter"] == "Manager",
      "the right PIN: the page presents, the others are told who")
b_v.clear()
b_p.clear()
s_p.receive({"t": "show_meet_view", "view": {"cx": 5.0, "cy": 6.0, "mpp": 0.5, "wid": "map", "xf": ["district", "North"],
                                             "scroll": 0.3, "pop": ["L0", "12"], "evil": "<script>"}})
got = [x for x in b_v if x["t"] == "show_meet"]
check(got and got[-1]["view"] == {"cx": 5.0, "cy": 6.0, "mpp": 0.5, "wid": "map", "xf": ["district", "North"],
                                  "scroll": 0.3, "pop": ["L0", "12"]} and not b_p,
      "the presenter's view reaches the others (cleaned), not the presenter")
s_p.receive({"t": "show_meet_laser", "wid": "map", "lon": 0.5, "lat": 0.5})
check(b_v[-1] == {"t": "show_meet_laser", "lon": 0.5, "lat": 0.5, "wid": "map"}, "the laser point reaches the others")
s_p.receive({"t": "show_meet_laser", "lon": 500, "lat": 0})
check(b_v[-1]["t"] == "show_meet_laser" and b_v[-1]["lon"] == 0.5, "a point off the earth is dropped")
s_p.receive({"t": "show_meet_end"})
check(b_v[-1]["t"] == "show_meet_state" and not b_v[-1]["presenter"], "the presenter stops: the others are told")
ext_show.stop_meeting(svc, "d_show")
check(b_v[-1] == {"t": "show_meet_state", "active": False}, "the computer ends the meeting")
check(meeting.clean_view({"cx": "1"}) == {} and meeting.clean_view("x") is None, "views are cleaned")

# ==== page side (own process: QtWebEngine cannot run inside a QgsApplication) ===========================================
svc.stop()
showkit.flush()
page_test = os.path.join(HERE, "test_dashboard_show_page.py")
if os.path.exists(page_test):
    r = subprocess.run([sys.executable, page_test], capture_output=True, text=True, encoding="utf-8", errors="replace",
                       timeout=600)
    out = r.stdout + r.stderr
    for line in out.splitlines():
        if line.startswith(("PASS", "FAIL", "JS ERROR", "page ")):
            print("   [page] " + line)
    check(r.returncode == 0 and "FAIL" not in out and "ALL PASSED" in out, "the page tests passed")

print("ALL PASSED" if not failures else f"FAILED: {len(failures)}")
app.exitQgis()
sys.exit(1 if failures else 0)
