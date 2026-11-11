"""The peer review lottery's data: who made each feature, the draw, the reviews table and the results.

The draw is fair and anyone can check it (commit, then reveal):
  1. commit: the data (every feature's fingerprint and its lot), the plan and the reviewers are hashed into the
     commitment, which the one drawing says aloud or sends to the team before anything else is known;
  2. reveal: the team then gives the salt (a word from each member, the number of a match, anything nobody could
     know before step 1) and the seed is the hash of both. The sample and who reviews what follow from the seed.
The one drawing cannot pick the sample: the salt comes after the commitment, and a changed feature or a changed
lot changes the commitment. Anyone with the same layer and the record of the draw re-draws and gets the same
sample (verify_draw).

Blind review: the reviews table names the lot of a feature by a code (an HMAC with a secret of the draw), not by
its author; the names behind the codes stay on the computer that drew (results there show names, elsewhere
codes). In a team room the history of a feature still says who changed it: the blindness is of the review
window, not a secret against a member who goes looking.
"""
import datetime
import hashlib
import hmac
import json
import os
import secrets
import uuid

from qgis.core import (QgsApplication, QgsCoordinateTransform, QgsFeature, QgsFeatureRequest, QgsField, QgsFields,
                       QgsGeometry, QgsProject, QgsVectorFileWriter, QgsVectorLayer, QgsWkbTypes)
from qgis.PyQt.QtCore import QMetaType

from . import sampling

REVIEWS = "biruni_reviews"
DRAWS = "biruni_review_draws"
BEFORE_ROOM = ""          # the lot of features made before the room (no author in its record)
VERDICTS = ("", "ok", "error")
CATEGORIES = ("Shape", "Position", "Attributes", "Missing", "Extra", "Topology", "Other")

REVIEW_FIELDS = (("uid", "s"), ("draw", "s"), ("layer", "s"), ("source", "s"), ("feature", "i"), ("print", "s"),
                 ("lot", "s"), ("reviewer", "s"), ("verdict", "s"), ("category", "s"), ("note", "s"),
                 ("changed", "i"), ("ts", "s"))
DRAW_FIELDS = (("draw", "s"), ("made", "s"), ("layer", "s"), ("source", "s"), ("record", "s"))


# ---- who made each feature -------------------------------------------------------------------------------------
def room_authors(layer):
    """{fid: (member id, name)} from the team room's record (the last one who changed each feature), or None when
    the layer is not shared in the room open on this map."""
    from .. import team

    node = team.current()
    key = node.key_of(layer) if node is not None else None
    if key is None:
        return None
    st = node.store
    with st._lock:
        rows = st.con.execute(
            "SELECT o.fid, o.author, o.name FROM ops o JOIN (SELECT fid, MAX(seq) s FROM ops WHERE layer=? AND "
            "kind!='field' GROUP BY fid) m ON o.seq=m.s", (key,)).fetchall()
    return {int(f): (a or "", n or "") for f, a, n, in rows}


def field_authors(layer, field):
    """{fid: (value, value)} from a field (editor, created_by…)."""
    i = layer.fields().indexOf(field)
    out = {}
    if i < 0:
        return out
    req = QgsFeatureRequest().setFlags(QgsFeatureRequest.Flag.NoGeometry).setSubsetOfAttributes([i])
    for f in layer.getFeatures(req):
        v = f.attribute(i)
        v = "" if v is None or (hasattr(v, "isNull") and v.isNull()) else str(v).strip()
        out[f.id()] = (v, v)
    return out


def prints_of(layer, fids=None):
    """{fid: fingerprint} of the features (fingerprint/core.py: the shape and the data fields)."""
    from ..fingerprint import core

    _cell, step = core.grid_for(layer)
    names = core.field_names(layer)
    idx = [(n, layer.fields().lookupField(n)) for n in names]
    req = QgsFeatureRequest()
    if fids is not None:
        req.setFilterFids(list(fids))
    return {f.id(): core.feature_print(f, idx, step) for f in layer.getFeatures(req)}


# ---- the draw ----------------------------------------------------------------------------------------------------
def lot_code(secret, author):
    return "L-" + hmac.new(bytes.fromhex(secret), author.encode("utf-8"), hashlib.sha256).hexdigest()[:8]


def _canon(obj):
    return json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def commit(layer, authors, reviewers, aql=0.01, ltpd=0.05, alpha=0.05, beta=0.10, secret=None, only=None):
    """Step 1: the plan of every lot and the commitment. authors: {fid: (id, name)} (missing = made before the
    room). reviewers: [(id, name)]. -> a pending draw (dict) to keep until the salt is given."""
    secret = secret or secrets.token_hex(16)
    prints = prints_of(layer, only)
    lots, names = {}, {}
    for fid in sorted(prints):
        a_id, a_name = authors.get(fid, (BEFORE_ROOM, ""))
        code = lot_code(secret, a_id)
        lots.setdefault(code, []).append(fid)
        names[code] = a_name or a_id
    rev_codes = {lot_code(secret, r_id): r_name for r_id, r_name in reviewers}
    plans = {code: sampling.plan(len(fids), aql, ltpd, alpha, beta) for code, fids in lots.items()}
    body = {"v": 1, "layer": layer.name(), "items": [[fid, prints[fid], code] for code, fids in sorted(lots.items())
                                                       for fid in fids],
            "plans": {c: [p["n"], p["c"]] for c, p in plans.items()},
            "risk": [aql, ltpd, alpha, beta], "reviewers": sorted(rev_codes)}
    commitment = hashlib.sha256(b"biruni-review-commit-1\n" + _canon(body)).hexdigest()
    return {"id": uuid.uuid4().hex[:12], "secret": secret, "commitment": commitment, "committed": _now(),
            "layer": layer.name(), "source": _source(layer), "body": body, "plans": plans, "lots": lots,
            "names": names, "reviewer_names": rev_codes}


def reveal(pending, salt):
    """Step 2: the salt is given: the seed, the sample of every lot and its reviewer. -> the draw (dict)."""
    if not (salt or "").strip():
        raise ValueError("salt")
    seed = sampling.seed_of(pending["commitment"], salt)
    sample = _sample(pending["body"], seed)
    d = dict(pending)
    d.update(salt=salt.strip(), salted=_now(), seed=seed, sample=sample)
    return d


def _sample(body, seed):
    """[(fid, print, lot, reviewer code)] from the committed body and the seed (also what verify re-computes)."""
    by_lot = {}
    for fid, pr, code in body["items"]:
        by_lot.setdefault(code, []).append((fid, pr))
    picked = []
    for code in sorted(by_lot):
        n = body["plans"][code][0]
        keys = {f"{code}/{fid}": (fid, pr) for fid, pr in by_lot[code]}
        for k in sampling.draw(seed, list(keys), n):
            picked.append((k, code) + keys[k])
    given = sampling.assign(seed, [(k, code) for k, code, _f, _p in picked], body["reviewers"])
    return [[fid, pr, code, given.get(k, "")] for k, code, fid, pr in picked]


def record_of(draw):
    """What is kept in the draws table (public: no secret, no names)."""
    return {"v": 1, "commitment": draw["commitment"], "committed": draw["committed"], "salt": draw["salt"],
            "salted": draw["salted"], "seed": draw["seed"], "body": draw["body"]}


def verify_draw(record, layer=None):
    """Re-draw from a kept record. -> (ok, problems). With the layer, its features are checked too: a feature
    changed since the draw is named (the data then differs from what was committed)."""
    problems = []
    body = record["body"]
    c = hashlib.sha256(b"biruni-review-commit-1\n" + _canon(body)).hexdigest()
    if c != record["commitment"]:
        problems.append("commitment")
    if sampling.seed_of(record["commitment"], record["salt"]) != record["seed"]:
        problems.append("seed")
    for code, (n, cc) in body["plans"].items():
        N = sum(1 for it in body["items"] if it[2] == code)
        a, l_, al, be = body["risk"]
        p = sampling.plan(N, a, l_, al, be)
        if [p["n"], p["c"]] != [n, cc]:
            problems.append("plan:" + code)
    sample = _sample(body, record["seed"])
    if layer is not None:
        now = prints_of(layer, [it[0] for it in body["items"]])
        changed = [it[0] for it in body["items"] if now.get(it[0]) != it[1]]
        if changed:
            problems.append(f"changed:{len(changed)}")
    return not [p for p in problems if not p.startswith("changed")], problems, sample


def _now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")


def _source(layer):
    return layer.source().split("layername=")[-1].split("|")[0] if "layername=" in layer.source() else layer.name()


# ---- the names behind the codes (only on the computer that drew) -------------------------------------------------
def _secret_dir():
    d = os.path.join(QgsApplication.qgisSettingsDirPath(), "biruni", "reviews")
    os.makedirs(d, exist_ok=True)
    return d


def keep_names(draw):
    p = os.path.join(_secret_dir(), draw["id"] + ".json")
    with open(p, "w", encoding="utf-8") as f:
        json.dump({"secret": draw["secret"], "names": draw["names"], "reviewers": draw["reviewer_names"]}, f,
                  ensure_ascii=False)


def names_of(draw_id):
    """{code: name} of the lots and the reviewers of a draw made on this computer, or {}."""
    try:
        with open(os.path.join(_secret_dir(), draw_id + ".json"), encoding="utf-8") as f:
            d = json.load(f)
        return {**d.get("reviewers", {}), **d.get("names", {})}
    except (OSError, ValueError):
        return {}


def reviewer_code(draw_id, reviewer_id):
    """The code of a reviewer: known only where the draw was made, so the review window names reviewers by
    their names (kept plainly in the table, see write_draw)."""
    try:
        with open(os.path.join(_secret_dir(), draw_id + ".json"), encoding="utf-8") as f:
            return lot_code(json.load(f)["secret"], reviewer_id)
    except (OSError, ValueError, KeyError):
        return ""


# ---- the tables ---------------------------------------------------------------------------------------------------
def _fields(spec):
    fs = QgsFields()
    for name, t in spec:
        fs.append(QgsField(name, QMetaType.Type.QString if t == "s" else QMetaType.Type.LongLong))
    return fs


def ensure_tables(gpkg, crs):
    """The reviews (points) and draws tables in the GeoPackage. -> (reviews layer, draws layer)."""
    ctx = QgsProject.instance().transformContext()
    for name, spec, geom in ((REVIEWS, REVIEW_FIELDS, QgsWkbTypes.Type.Point),
                             (DRAWS, DRAW_FIELDS, QgsWkbTypes.Type.NoGeometry)):
        probe = QgsVectorLayer(f"{gpkg}|layername={name}", name, "ogr")
        if probe.isValid():
            continue
        opts = QgsVectorFileWriter.SaveVectorOptions()
        opts.driverName = "GPKG"
        opts.layerName = name
        opts.fileEncoding = "UTF-8"
        opts.actionOnExistingFile = (QgsVectorFileWriter.ActionOnExistingFile.CreateOrOverwriteLayer
                                     if os.path.exists(gpkg) else
                                     QgsVectorFileWriter.ActionOnExistingFile.CreateOrOverwriteFile)
        w = QgsVectorFileWriter.create(gpkg, _fields(spec), geom, crs, ctx, opts)
        if w is None or w.hasError() != QgsVectorFileWriter.WriterError.NoError:
            raise RuntimeError(w.errorMessage() if w else "cannot write " + gpkg)
        del w
    return (QgsVectorLayer(f"{gpkg}|layername={REVIEWS}", "Peer reviews", "ogr"),
            QgsVectorLayer(f"{gpkg}|layername={DRAWS}", "Review draws", "ogr"))


def _set(feat, **values):
    for k, v in values.items():
        feat.setAttribute(k, v)


def _add(layer, feats):
    ok, added = layer.dataProvider().addFeatures(feats)
    if not ok:
        raise RuntimeError("; ".join(layer.dataProvider().errors()[-3:]) or "cannot write")
    from .. import team

    team.notify_added(layer, added)
    layer.reload()
    return added


def write_draw(reviews, draws, draw, layer):
    """Keep a revealed draw: its public record and one review row per sampled feature (a point on it)."""
    rec = QgsFeature(draws.fields())
    _set(rec, draw=draw["id"], made=draw["salted"], layer=draw["layer"], source=draw["source"],
         record=json.dumps(record_of(draw), ensure_ascii=False))
    _add(draws, [rec])
    xf = QgsCoordinateTransform(layer.crs(), reviews.crs(), QgsProject.instance())
    fids = [it[0] for it in draw["sample"]]
    geoms = {f.id(): f.geometry() for f in layer.getFeatures(QgsFeatureRequest().setFilterFids(fids))}
    rnames = draw["reviewer_names"]
    rows = []
    for fid, pr, code, rcode in draw["sample"]:
        f = QgsFeature(reviews.fields())
        g = geoms.get(fid)
        if g is not None and not g.isEmpty():
            pt = QgsGeometry(g.pointOnSurface())
            pt.transform(xf)
            f.setGeometry(pt)
        _set(f, uid=uuid.uuid4().hex, draw=draw["id"], layer=draw["layer"], source=draw["source"], feature=int(fid),
             print=pr, lot=code, reviewer=rnames.get(rcode, ""), verdict="", category="", note="", changed=0, ts="")
        rows.append(f)
    keep_names(draw)
    return _add(reviews, rows)


def draws_in(draws):
    """[record dicts with id, made, layer] kept in the draws table, the newest first."""
    out = []
    for f in draws.getFeatures():
        try:
            r = json.loads(f["record"])
        except (TypeError, ValueError):
            continue
        r.update(id=f["draw"], made=f["made"], layer=f["layer"], source=f["source"])
        out.append(r)
    return sorted(out, key=lambda r: r["made"], reverse=True)


def rows_of(reviews, draw_id, reviewer=None):
    """The review rows of a draw (dicts with the feature id fid_row), a reviewer's only when given."""
    out = []
    for f in reviews.getFeatures():
        if f["draw"] != draw_id or (reviewer is not None and f["reviewer"] != reviewer):
            continue
        d = {n: f[n] for n, _t in REVIEW_FIELDS}
        d = {k: (None if hasattr(v, "isNull") and v.isNull() else v) for k, v in d.items()}
        d["row"] = f.id()
        out.append(d)
    return sorted(out, key=lambda d: (d["verdict"] or "", d["uid"]))


def set_verdict(reviews, row, verdict, category="", note="", changed=False):
    if verdict not in VERDICTS:
        raise ValueError(verdict)
    idx = {n: reviews.fields().indexOf(n) for n in ("verdict", "category", "note", "changed", "ts")}
    vals = {idx["verdict"]: verdict, idx["category"]: category if verdict == "error" else "",
            idx["note"]: note or "", idx["changed"]: 1 if changed else 0, idx["ts"]: _now()}
    if not reviews.dataProvider().changeAttributeValues({int(row): vals}):
        raise RuntimeError("cannot write the verdict")
    from .. import team

    team.notify_changed(reviews, [int(row)])
    reviews.reload()


# ---- the results ----------------------------------------------------------------------------------------------------
def results(reviews, record, names=None, conf=0.95):
    """Per lot and for the whole delivery: N, the plan, reviewed, errors, the bounds and the decision."""
    names = names or {}
    rows = rows_of(reviews, record["id"])
    body = record["body"]
    lots = {}
    for _fid, _pr, code in body["items"]:
        lots[code] = lots.get(code, 0) + 1
    out = []
    for code in sorted(lots):
        n_plan, c = body["plans"][code]
        mine = [r for r in rows if r["lot"] == code]
        done = [r for r in mine if r["verdict"] in ("ok", "error")]
        x = sum(1 for r in done if r["verdict"] == "error")
        cats = {}
        for r in done:
            if r["verdict"] == "error":
                k = r["category"] or "Other"
                cats[k] = cats.get(k, 0) + 1
        b = sampling.bounds(lots[code], len(done), x, conf) if done else None
        if x > c:
            decision = "reject"            # already more errors than the plan allows: no need to finish
        elif len(done) >= n_plan:
            decision = "accept"
        else:
            decision = "pending"
        out.append({"lot": code, "name": names.get(code, ""), "N": lots[code], "n": n_plan, "c": c,
                    "drawn": len(mine), "done": len(done), "errors": x, "bounds": b, "decision": decision,
                    "categories": cats, "unassigned": sum(1 for r in mine if not r["reviewer"])})
    total = sampling.stratified([(r["N"], r["done"], r["errors"]) for r in out if r["done"]], conf)
    decisions = {r["decision"] for r in out}
    whole = "reject" if "reject" in decisions else ("pending" if "pending" in decisions else "accept")
    return {"lots": out, "total": total, "decision": whole, "changed": sum(1 for r in rows if r["changed"]),
            "conf": conf}


def csv_text(res):
    head = ["lot", "name", "features", "sample", "accept_up_to", "reviewed", "errors", "estimate_pct",
            "upper_pct", "low_pct", "high_pct", "decision"]
    lines = [",".join(head)]

    def pct(b, k):
        return f"{100 * b[k]:.2f}" if b else ""

    for r in res["lots"]:
        b = r["bounds"]
        lines.append(",".join(str(v).replace(",", " ") for v in (
            r["lot"], r["name"], r["N"], r["n"], r["c"], r["done"], r["errors"], pct(b, "rate"),
            pct(b, "upper_rate"), pct(b, "lo_rate"), pct(b, "hi_rate"), r["decision"])))
    t = res["total"]
    lines.append(",".join(str(v) for v in ("ALL", "", t["N"] if t else "", "", "", t["n"] if t else "",
                                           t["x"] if t else "", pct(t, "rate"), pct(t, "upper_rate"),
                                           pct(t, "lo_rate"), pct(t, "hi_rate"), res["decision"])))
    return "\n".join(lines) + "\n"

