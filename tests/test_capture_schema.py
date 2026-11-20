r"""Capture schema model tests (Phase A): clean_schema, templates, completion vectors, merge, stats, cap_payload.

Run:  engine\bin\python-qgis.bat tests\test_capture_schema.py
"""
import ast
import json
import os
import sys
import tempfile

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "app", "plugins"))
sys.path.insert(0, os.path.join(HERE, "..", "app", "python"))

from qgis.core import QgsApplication  # noqa: E402

SETTINGS_DIR = tempfile.mkdtemp()
os.environ["QGIS_CUSTOM_CONFIG_PATH"] = SETTINGS_DIR
app = QgsApplication([], False)
app.initQgis()

from biruni import i18n  # noqa: E402
from biruni.field_photos import capture as cap  # noqa: E402
from biruni.field_photos import capture_templates as ct  # noqa: E402

failures = []


def check(cond, msg):
    print(("PASS " if cond else "FAIL ") + msg)
    if not cond:
        failures.append(msg)


# ---- capture.py is pure Python (no Qt import) -----------------------------------------------------------
cap_src = open(os.path.join(HERE, "..", "app", "plugins", "biruni", "field_photos", "capture.py"), encoding="utf-8").read()
cap_ast = ast.parse(cap_src)
imports = []
for node in ast.walk(cap_ast):
    if isinstance(node, ast.Import):
        imports.extend(a.name for a in node.names)
    elif isinstance(node, ast.ImportFrom):
        imports.append(node.module or "")
check(not any("qgis" in m.lower() or "qt" in m.lower() for m in imports),
      "capture.py imports no Qt or qgis modules: " + str(imports))


# ---- test_clean_schema_rejects --------------------------------------------------------------------------
def test_clean_schema_rejects():
    def rejects(raw, why):
        try:
            cap.clean_schema(raw)
            check(False, f"clean_schema rejects {why}")
        except ValueError:
            check(True, f"clean_schema rejects {why}")

    base = {"id": "test_s", "rev": 1, "title": "Test", "nodes": [{"id": "n1", "label": "N1"}]}
    check(cap.clean_schema(base)["id"] == "test_s", "valid minimal schema accepted")

    # duplicate node ids
    rejects({"id": "test_s", "rev": 1, "title": "Test",
             "nodes": [{"id": "dup", "label": "A"}, {"id": "dup", "label": "B"}]},
            "duplicate node ids at root")
    rejects({"id": "test_s", "rev": 1, "title": "Test",
             "nodes": [{"id": "a", "label": "A", "nodes": [{"id": "a", "label": "A2"}]}]},
            "duplicate node id in child")

    # invalid node id
    rejects({"id": "1bad", "rev": 1, "title": "Test", "nodes": [{"id": "n1", "label": "N1"}]},
            "bad schema id starting with digit")
    rejects({"id": "test_s", "rev": 1, "title": "Test", "nodes": [{"id": "BadUpper", "label": "N1"}]},
            "uppercase node id")

    # depth > 6
    deep = {"id": "d7", "label": "L7"}
    for i in range(6, 0, -1):
        deep = {"id": f"d{i}", "label": f"L{i}", "nodes": [deep]}
    rejects({"id": "test_s", "rev": 1, "title": "Test", "nodes": [deep]}, "depth > 6")

    # min > max in photos and repeat
    rejects({"id": "test_s", "rev": 1, "title": "Test",
             "nodes": [{"id": "n1", "label": "N1", "photos": [5, 2]}]}, "photos min > max")
    rejects({"id": "test_s", "rev": 1, "title": "Test",
             "nodes": [{"id": "n1", "label": "N1", "repeat": {"min": 10, "max": 3}}]}, "repeat min > max")
    rejects({"id": "test_s", "rev": 1, "title": "Test",
             "nodes": [{"id": "n1", "label": "N1", "photos": [0, 51]}]}, "photos max > 50")
    rejects({"id": "test_s", "rev": 1, "title": "Test",
             "nodes": [{"id": "n1", "label": "N1", "repeat": {"min": 0, "max": 201}}]}, "repeat max > 200")

    # > 200 nodes
    many = [{"id": f"n_{i}", "label": f"Node {i}"} for i in range(201)]
    rejects({"id": "test_s", "rev": 1, "title": "Test", "nodes": many}, "more than 200 nodes")

    # > 48 KB
    big_q = [{"label": f"Q{i} " + ("x" * 50), "type": "choice",
              "choices": [f"c{j}_" + ("y" * 40) for j in range(40)]} for i in range(25)]
    big_nodes = [{"id": f"n_{i}", "label": f"Node {i}", "q": big_q} for i in range(15)]
    rejects({"id": "test_s", "rev": 1, "title": "Test", "nodes": big_nodes}, "more than 48 KB")


# ---- test_templates_valid_and_translated ----------------------------------------------------------------
def test_templates_valid_and_translated():
    i18n.init("en_US")
    en_tpls = ct.builtin_templates()
    check([t["id"] for t in en_tpls] == ["building", "villa", "vacant_land", "shop"],
          "all four built-in templates exist in order")
    for t in en_tpls:
        again = cap.clean_schema(t)
        check(again == t, f"clean_schema is idempotent on {t['id']} (en)")

    i18n.init("ar")
    ar_tpls = ct.builtin_templates()
    for en_t, ar_t in zip(en_tpls, ar_tpls):
        check(cap.clean_schema(ar_t) == ar_t, f"clean_schema is idempotent on {ar_t['id']} (ar)")
        check(ar_t["title"] != en_t["title"] and any("\u0600" <= c <= "\u06ff" for c in ar_t["title"]),
              f"template {ar_t['id']} title is translated to Arabic: {ar_t['title']}")
    i18n.init("en_US")


# ---- test_missing_vectors -------------------------------------------------------------------------------
def test_missing_vectors():
    vec_path = os.path.join(HERE, "data", "capture_vectors.json")
    data = json.load(open(vec_path, encoding="utf-8"))
    schemas = {k: cap.clean_schema(v) for k, v in data["schemas"].items()}
    check(len(data["vectors"]) >= 5, f"loaded {len(data['vectors'])} completion vectors")
    for v in data["vectors"]:
        s = schemas[v["schema"]]
        st = cap.clean_state(v["state"], s)
        recs = v.get("records") or []
        miss = cap.missing(s, st, recs)
        got_keys = [m["k"] for m in miss]
        st_info = cap.stats(s, st, recs)
        check(got_keys == v["expected_missing_keys"] and st_info["complete"] == v["expected_complete"],
              f"vector {v['name']}: got {got_keys} (expected {v['expected_missing_keys']})")


# ---- test_merge_rev_and_lww -----------------------------------------------------------------------------
def test_merge_rev_and_lww():
    i18n.init("en_US")
    s = ct.building_template()
    # Same device: older rev ignored even if passed second
    s1_new = {"v": 1, "job": "J1111111", "dev": "devA0001", "by": "Ali", "rev": 5, "t": 5000,
              "inst": {"f10001": {"n": "floors", "p": "", "o": 1, "t": 100, "del": 0}},
              "ans": {}, "note": {":entrance": ["New note", 5000]}, "ph": {":front": ["p1"]}, "skip": {}}
    s1_old = {"v": 1, "job": "J1111111", "dev": "devA0001", "by": "Ali", "rev": 3, "t": 9000,
              "inst": {}, "ans": {}, "note": {":entrance": ["Stale rev note", 9000]}, "ph": {}, "skip": {}}
    m1 = cap.merge_states([s1_new, s1_old], s)
    check(m1["rev"] == 5 and m1["note"][":entrance"][0] == "New note" and "f10001" in m1["inst"],
          "per device: highest rev wins, older replayed snapshot is ignored")

    # Across devices: union of instances, later del wins, LWW by timestamp on ans/note/skip, union of ph
    s2 = {"v": 1, "job": "J1111111", "dev": "devB0002", "by": "Sara", "rev": 2, "t": 6000,
          "inst": {
              "f10001": {"n": "floors", "p": "", "o": 1, "t": 100, "del": 6000},
              "f20002": {"n": "floors", "p": "", "o": 2, "t": 200, "del": 0},
              "a20002": {"n": "apts", "p": "f20002", "o": 1, "t": 210, "del": 0},
          },
          "ans": {"a20002:apts": {"apt_no": ["201", 6000], "condition": ["Fair", 6000]}},
          "note": {":entrance": ["Sara newer note", 6000]},
          "ph": {":front": ["p2"]},
          "skip": {":stairs": ["Locked door", 6000]}}
    s1_ans = dict(s1_new, rev=6, t=5500,
                  ans={"a20002:apts": {"apt_no": ["201-old", 5500], "condition": ["Good", 7000]}})
    m2 = cap.merge_states([s1_ans, s2], s)
    check(m2["inst"]["f10001"]["del"] == 6000 and m2["inst"]["f20002"]["del"] == 0,
          "across devices: inst union by iid and later del wins")
    check(m2["ans"]["a20002:apts"]["apt_no"][0] == "201"
          and m2["ans"]["a20002:apts"]["condition"][0] == "Good",
          "across devices: answers are last-writer-wins per question by t")
    check(m2["note"][":entrance"][0] == "Sara newer note", "across devices: notes are LWW by t")
    check(m2["ph"][":front"] == ["p1", "p2"], "across devices: photo ids are a union")


# ---- test_stats_building --------------------------------------------------------------------------------
def test_stats_building():
    i18n.init("en_US")
    s = ct.building_template()
    state = {
        "v": 1, "job": "J2222222", "dev": "devA0001", "rev": 10, "t": 10000,
        "inst": {
            "fl0001": {"n": "floors", "p": "", "o": 1, "t": 100, "del": 0},
            "ap0001": {"n": "apts", "p": "fl0001", "o": 1, "t": 110, "del": 0},
            "ap0002": {"n": "apts", "p": "fl0001", "o": 2, "t": 120, "del": 0},
            "rm0001": {"n": "rooms", "p": "ap0001", "o": 1, "t": 130, "del": 0},
            "rm0002": {"n": "rooms", "p": "ap0001", "o": 2, "t": 140, "del": 0},
        },
        "ans": {
            "fl0001:floors": {"floor_name": ["Ground", 100]},
            "ap0001:apts": {"apt_no": ["1", 110], "use": ["Residential", 110],
                            "condition": ["Good", 110], "rooms_count": [4, 110]},
            "ap0002:apts": {"apt_no": ["2", 120], "use": ["Commercial", 120],
                            "condition": ["Good", 120], "rooms_count": [3, 120]},
            "rm0001:rooms": {"room_type": ["Kitchen", 130]},
            "rm0002:rooms": {"room_type": ["Bedroom", 140]},
        },
        "note": {},
        "ph": {
            ":front": ["pf1"], ":right": ["pr1"], ":left": ["pl1"], ":back": ["pb1"],
            ":entrance": ["pe1"], ":stairs": ["ps1"],
            "rm0001:rooms": ["r1p1", "r1p2"],
            "rm0002:rooms": ["r2p1", "r2p2"],
        },
        "skip": {
            "ap0002:apts": ["Tenant absent", 200],
        },
    }
    recs = [{"id": "extra1", "cap": {"j": "J2222222", "k": "*", "pl": "*", "ch": []}}]
    st = cap.stats(s, state, recs)
    check(st["complete"] is True and st["missing"] == [], "building job with one complete apt and one skipped apt is complete")
    check(st["instances"] == {"floors": 1, "apts": 2, "rooms": 2},
          f"instance counts: {st['instances']}")
    check(st["choices"].get("apts.condition") == {"Good": 2}
          and st["choices"].get("apts.use") == {"Residential": 1, "Commercial": 1}
          and st["choices"].get("rooms.room_type") == {"Kitchen": 1, "Bedroom": 1},
          f"choice tallies: {st['choices']}")
    check(st["sums"].get("apts.rooms_count") == 7, f"integer sum rooms_count = 7 ({st['sums']})")
    check(st["section_photos"].get("Facades") == 4 and st["section_photos"].get("Floor") == 4
          and st["extra_photos"] == 1,
          f"photos per section & extra: {st['section_photos']}, extra={st['extra_photos']}")
    check(len(st["skipped"]) == 1 and st["skipped"][0]["reason"] == "Tenant absent",
          "skipped items list carries reason")
    pl = cap.path_label(s, state, "rm0001:rooms", [["floors", "fl0001", 1], ["apts", "ap0001", 1], ["rooms", "rm0001", 1]])
    check(pl == "Ground › 1 › Kitchen", f"path_label uses title answers: {pl}")


# ---- test_cap_payload_limit -----------------------------------------------------------------------------
def test_cap_payload_limit():
    i18n.init("en_US")
    s = ct.building_template()
    check(cap.cap_payload([]) is None, "no jobs -> cap_payload is None")
    j1 = cap.new_job(s, {"job_no": "B-100", "client": "ACME"}, loc=[46.675, 24.713])
    p = cap.cap_payload([j1])
    check(p is not None and "building@1" in p["schemas"] and len(p["jobs"]) == 1
          and p["jobs"][0]["title"] == "B-100",
          "cap_payload bundles open job and its schema")
    closed_j = dict(j1, st="closed")
    check(cap.cap_payload([closed_j]) is None, "closed jobs are not sent in cap_payload")
    try:
        cap.cap_payload([j1], limit=100)
        check(False, "cap_payload enforces byte limit")
    except ValueError:
        check(True, "cap_payload enforces byte limit")


test_clean_schema_rejects()
test_templates_valid_and_translated()
test_missing_vectors()
test_merge_rev_and_lww()
test_stats_building()
test_cap_payload_limit()

print("ALL PASS" if not failures else f"{len(failures)} FAILED")
app.exitQgis()
sys.exit(1 if failures else 0)
