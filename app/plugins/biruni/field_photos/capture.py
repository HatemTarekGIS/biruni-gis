"""Capture schema model: dynamic trees of what the field surveyor photographs and answers (no Qt)."""
import datetime as dt
import json
import re
import secrets
import time
import unicodedata

MAX_SCHEMA_BYTES = 48_000
MAX_NODES = 200
MAX_DEPTH = 6
MAX_PHOTOS = 50
MAX_REPEAT = 200
MAX_CAP = 60_000
MIN_SKIP_LEN = 3

NODE_ID_RE = re.compile(r"^[a-z][a-z0-9_]{0,23}$")
JOB_ID_RE = re.compile(r"^[A-Za-z0-9_-]{4,32}$")
IID_RE = re.compile(r"^[a-z0-9]{4,16}$")
DEV_RE = re.compile(r"^[A-Za-z0-9_-]{2,64}$")
SCHEMA_REF_RE = re.compile(r"^[a-z][a-z0-9_]{0,23}@\d+$")

FIELD_TYPES = ("text", "number", "integer", "choice", "multi", "yesno", "date")
MAX_FIELDS, MAX_CHOICES = 30, 50
RESERVED = {"fid", "geom", "geometry", "photo_id", "photo", "taken", "kind", "note", "accuracy_m", "altitude",
            "compass", "heading", "fix_age_s", "device", "avg_n", "avg_sd_m", "level_id", "unit_id",
            "surveyor", "device_id", "job_id", "job_item"}
NAME_RE = re.compile(r"^[^\W\d]\w{0,29}$")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _num(v, lo=None, hi=None):
    if v is None or isinstance(v, bool):
        return None
    try:
        v = float(v)
    except (TypeError, ValueError):
        return None
    if v != v or (lo is not None and v < lo) or (hi is not None and v > hi):
        return None
    return v


def _int(v, lo=None, hi=None):
    v = _num(v, lo, hi)
    return int(v) if v is not None and v == int(v) else None


def _text(v, n):
    return str(v).strip()[:n] if v not in (None, "") else ""


def _column_name(label, taken):
    label = "".join(c for c in label if unicodedata.category(c) != "Mn")
    n = re.sub(r"\W+", "_", label.strip().lower()).strip("_")[:30] or "field"
    if not NAME_RE.match(n):
        n = ("f_" + n)[:30]
    base, i = n, 2
    while n in taken or n in RESERVED:
        n = f"{base[:26]}_{i}"
        i += 1
    return n


def clean_questions(form, keep=False, title=True):
    """Questions for a job or node: server.clean_form rules, plus optional title flag."""
    out, taken = [], set()
    for f in form if isinstance(form, (list, tuple)) else ():
        if not isinstance(f, dict) or not _text(f.get("label"), 60):
            continue
        t = f.get("type") if f.get("type") in FIELD_TYPES else "text"
        name = _text(f.get("name"), 30)
        if not NAME_RE.match(name) or name in taken or name in RESERVED:
            name = _column_name(_text(f.get("label"), 60), taken)
        taken.add(name)
        d = {"name": name, "label": _text(f.get("label"), 60), "type": t,
             "req": bool(f.get("req")), "keep": bool(f.get("keep")) if keep else False}
        if t in ("choice", "multi"):
            choices = []
            for c in f.get("choices") or ():
                c = _text(c, 60)
                if c and c not in choices:
                    choices.append(c)
            if not choices:
                continue
            d["choices"] = choices[:MAX_CHOICES]
        elif t in ("number", "integer"):
            lo, hi = _num(f.get("min")), _num(f.get("max"))
            if lo is not None and hi is not None and lo > hi:
                lo, hi = hi, lo
            d["min"], d["max"] = lo, hi
        if title and f.get("title"):
            d["title"] = True
        out.append(d)
        if len(out) == MAX_FIELDS:
            break
    return out


def clean_values(form, values):
    """Checked answers against a question list."""
    out = {}
    values = values if isinstance(values, dict) else {}
    for f in form or ():
        v, t = values.get(f["name"]), f["type"]
        if v is None or v == "" or v == []:
            continue
        if t == "text":
            v = _text(v, 250)
        elif t in ("number", "integer"):
            v = _num(v, f.get("min"), f.get("max"))
            if v is None or (t == "integer" and v != int(v)):
                continue
            v = int(v) if t == "integer" else v
        elif t == "choice":
            v = _text(v, 60)
        elif t == "multi":
            v = "; ".join(x for x in (_text(x, 60) for x in (v if isinstance(v, list) else [v])) if x)[:500]
        elif t == "yesno":
            if not isinstance(v, bool):
                continue
        elif t == "date":
            v = _text(v, 10)
            try:
                dt.date.fromisoformat(v)
            except ValueError:
                continue
            if not DATE_RE.match(v):
                continue
        if v != "":
            out[f["name"]] = v
    return out


def clean_schema(raw):
    """Validate and normalize a capture schema; raises ValueError on invalid input."""
    if not isinstance(raw, dict):
        raise ValueError("schema must be an object")
    raw_bytes = len(json.dumps(raw, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
    if raw_bytes > MAX_SCHEMA_BYTES:
        raise ValueError("schema exceeds 48 KB")
    sid = _text(raw.get("id"), 24)
    if not NODE_ID_RE.match(sid):
        raise ValueError(f"bad schema id: {sid!r}")
    rev = _int(raw.get("rev", 1), 1, 1_000_000)
    if rev is None:
        raise ValueError("bad rev")
    title = _text(raw.get("title"), 80)
    if not title:
        raise ValueError("missing schema title")
    job_q = clean_questions(raw.get("job") or (), keep=False, title=True)
    seen_ids = set()
    counts = [0]

    def clean_node(n, depth):
        if depth > MAX_DEPTH:
            raise ValueError("schema depth exceeds 6")
        if not isinstance(n, dict):
            raise ValueError("node must be an object")
        counts[0] += 1
        if counts[0] > MAX_NODES:
            raise ValueError("schema has more than 200 nodes")
        nid = _text(n.get("id"), 24)
        if not NODE_ID_RE.match(nid):
            raise ValueError(f"bad node id: {nid!r}")
        if nid in seen_ids:
            raise ValueError(f"duplicate node id: {nid}")
        seen_ids.add(nid)
        label = _text(n.get("label"), 60)
        if not label:
            raise ValueError(f"missing label on node {nid}")
        req = True if n.get("req") is None else bool(n.get("req"))
        out = {"id": nid, "label": label, "req": req}
        if n.get("repeat") is not None:
            rep = n.get("repeat")
            if not isinstance(rep, dict):
                raise ValueError(f"bad repeat on {nid}")
            rmin = _int(rep.get("min", 0), 0, MAX_REPEAT)
            rmax = _int(rep.get("max", 1), 1, MAX_REPEAT)
            if rmin is None or rmax is None or rmin > rmax:
                raise ValueError(f"bad repeat range on {nid}")
            out["repeat"] = {"min": rmin, "max": rmax}
        if n.get("photos") is not None:
            ph = n.get("photos")
            if not isinstance(ph, (list, tuple)) or len(ph) != 2:
                raise ValueError(f"bad photos on {nid}")
            pmin = _int(ph[0], 0, MAX_PHOTOS)
            pmax = _int(ph[1], 0, MAX_PHOTOS)
            if pmin is None or pmax is None or pmin > pmax:
                raise ValueError(f"bad photos range on {nid}")
            out["photos"] = [pmin, pmax]
        if n.get("q"):
            qs = clean_questions(n.get("q"), keep=False, title=True)
            if qs:
                out["q"] = qs
        if n.get("nodes") is not None:
            ch = n.get("nodes")
            if not isinstance(ch, (list, tuple)):
                raise ValueError(f"bad child nodes on {nid}")
            cleaned_ch = [clean_node(c, depth + 1) for c in ch]
            if cleaned_ch:
                out["nodes"] = cleaned_ch
        return out

    raw_nodes = raw.get("nodes")
    if not isinstance(raw_nodes, (list, tuple)) or not raw_nodes:
        raise ValueError("schema needs at least one node")
    nodes = [clean_node(n, 1) for n in raw_nodes]
    res = {"biruni_capture": 1, "id": sid, "rev": rev, "title": title, "job": job_q, "nodes": nodes}
    if len(json.dumps(res, ensure_ascii=False, separators=(",", ":")).encode("utf-8")) > MAX_SCHEMA_BYTES:
        raise ValueError("schema exceeds 48 KB")
    return res


def schema_key(schema):
    return f"{schema['id']}@{schema['rev']}"


def clean_cap(raw):
    """Clean photo metadata cap binding: {j, k, pl, ch} or None."""
    if not isinstance(raw, dict):
        return None
    raw_j = raw.get("j")
    if not isinstance(raw_j, str):
        return None
    j = raw_j.strip()
    if not j or not JOB_ID_RE.fullmatch(j):
        return None
    k = _text(raw.get("k"), 48)
    if not k:
        return None
    if k != "*":
        parts = k.split(":", 1)
        if len(parts) != 2 or not NODE_ID_RE.match(parts[1]):
            return None
        if parts[0] and not IID_RE.match(parts[0]):
            return None
    pl = _text(raw.get("pl"), 240)
    ch_out = []
    for entry in raw.get("ch") if isinstance(raw.get("ch"), (list, tuple)) else ():
        if not isinstance(entry, (list, tuple)) or len(entry) < 3:
            continue
        nid, iid, order = _text(entry[0], 24), _text(entry[1], 16), _int(entry[2], 1, MAX_REPEAT)
        if NODE_ID_RE.match(nid) and IID_RE.match(iid) and order is not None:
            ch_out.append([nid, iid, order])
            if len(ch_out) >= MAX_DEPTH:
                break
    return {"j": j, "k": k, "pl": pl, "ch": ch_out}


def _valid_slot_key(k):
    if not isinstance(k, str) or not k or len(k) > 48:
        return False
    if k == "*":
        return True
    parts = k.split(":", 1)
    if len(parts) != 2 or not NODE_ID_RE.match(parts[1]):
        return False
    return not parts[0] or bool(IID_RE.match(parts[0]))


def clean_state(raw, schema=None):
    """Validate and normalize a job state snapshot from a phone."""
    if not isinstance(raw, dict):
        raise ValueError("state must be an object")
    raw_job = raw.get("job")
    if not isinstance(raw_job, str):
        raise ValueError("bad job id")
    job = raw_job.strip()
    if not job or not JOB_ID_RE.fullmatch(job):
        raise ValueError("bad job id")
    raw_dev = raw.get("dev")
    if not isinstance(raw_dev, str):
        raise ValueError("bad dev id")
    dev = raw_dev.strip()
    if not dev or not DEV_RE.fullmatch(dev):
        raise ValueError("bad dev id")
    rev = _int(raw.get("rev", 0), 0, 1_000_000_000)
    if rev is None:
        raise ValueError("bad rev")
    t = _int(raw.get("t", 0), 0) or 0
    by = _text(raw.get("by"), 40)
    s_ref = _text(raw.get("s"), 40)
    if schema is not None and not s_ref:
        s_ref = schema_key(schema)

    node_map = {}
    if schema is not None:
        def _collect(nodes):
            for n in nodes:
                node_map[n["id"]] = n
                if n.get("nodes"):
                    _collect(n["nodes"])
        _collect(schema.get("nodes") or ())

    inst = {}
    raw_inst = raw.get("inst") if isinstance(raw.get("inst"), dict) else {}
    for iid, info in raw_inst.items():
        iid = _text(iid, 16)
        if not IID_RE.match(iid) or not isinstance(info, dict):
            continue
        nid = _text(info.get("n"), 24)
        if not NODE_ID_RE.match(nid):
            continue
        if node_map and (nid not in node_map or not node_map[nid].get("repeat")):
            continue
        p = _text(info.get("p"), 16)
        if p and not IID_RE.match(p):
            continue
        o = _int(info.get("o", 1), 1, MAX_REPEAT) or 1
        it_t = _int(info.get("t", 0), 0) or 0
        del_t = _int(info.get("del", 0), 0) or 0
        inst[iid] = {"n": nid, "p": p, "o": o, "t": it_t, "del": del_t}

    ans = {}
    raw_ans = raw.get("ans") if isinstance(raw.get("ans"), dict) else {}
    for k, qmap in raw_ans.items():
        if not _valid_slot_key(k) or not isinstance(qmap, dict):
            continue
        nid = k.split(":", 1)[1] if ":" in k else ""
        q_defs = {q["name"]: q for q in (node_map.get(nid, {}).get("q") or ())} if node_map else None
        cleaned_q = {}
        for qname, pair in qmap.items():
            qname = _text(qname, 30)
            if not qname:
                continue
            if isinstance(pair, (list, tuple)) and len(pair) >= 2:
                val, vt = pair[0], _int(pair[1], 0) or 0
            else:
                val, vt = pair, t
            if q_defs is not None:
                if qname not in q_defs:
                    continue
                checked = clean_values([q_defs[qname]], {qname: val})
                if qname not in checked:
                    continue
                val = checked[qname]
            cleaned_q[qname] = [val, vt]
        if cleaned_q:
            ans[k] = cleaned_q

    note = {}
    raw_note = raw.get("note") if isinstance(raw.get("note"), dict) else {}
    for k, pair in raw_note.items():
        if not _valid_slot_key(k):
            continue
        if isinstance(pair, (list, tuple)) and len(pair) >= 2:
            txt, nt = _text(pair[0], 500), _int(pair[1], 0) or 0
        else:
            txt, nt = _text(pair, 500), t
        if txt:
            note[k] = [txt, nt]

    ph = {}
    raw_ph = raw.get("ph") if isinstance(raw.get("ph"), dict) else {}
    for k, ids in raw_ph.items():
        if not _valid_slot_key(k) or not isinstance(ids, (list, tuple)):
            continue
        kept = []
        for pid in ids:
            pid = _text(pid, 64)
            if pid and pid not in kept:
                kept.append(pid)
        if kept:
            ph[k] = kept

    skip = {}
    raw_skip = raw.get("skip") if isinstance(raw.get("skip"), dict) else {}
    for k, pair in raw_skip.items():
        if not _valid_slot_key(k):
            continue
        if isinstance(pair, (list, tuple)) and len(pair) >= 2:
            txt, st_t = _text(pair[0], 300), _int(pair[1], 0) or 0
        else:
            txt, st_t = _text(pair, 300), t
        if txt:
            skip[k] = [txt, st_t]

    closed = _int(raw.get("closed"), 0) if raw.get("closed") is not None else None
    return {"v": 1, "job": job, "dev": dev, "by": by, "rev": rev, "t": t, "s": s_ref,
            "inst": inst, "ans": ans, "note": note, "ph": ph, "skip": skip, "closed": closed}


def new_job_id():
    alphabet = "23456789ABCDEFGHJKLMNPQRSTUVWXYZ"
    return "J" + "".join(secrets.choice(alphabet) for _ in range(7))


def new_job(schema, data=None, title="", loc=None, to="", job_id=None):
    """Create a job record with a frozen cleaned schema snapshot."""
    s = clean_schema(schema)
    if job_id is not None:
        if not isinstance(job_id, str):
            raise ValueError("bad job id")
        jid = job_id.strip()
    else:
        jid = new_job_id()
    if not JOB_ID_RE.fullmatch(jid):
        raise ValueError("bad job id")
    cleaned_data = clean_values(s.get("job") or (), data or {})
    if not title:
        for q in s.get("job") or ():
            if q.get("title") and cleaned_data.get(q["name"]):
                title = str(cleaned_data[q["name"]])
                break
    if not title:
        title = f"{s['title']} ({jid})"
    loc_out = None
    if isinstance(loc, (list, tuple)) and len(loc) == 2:
        lon, lat = _num(loc[0], -180, 180), _num(loc[1], -90, 90)
        if lon is not None and lat is not None:
            loc_out = [round(lon, 7), round(lat, 7)]
    return {
        "biruni_job": 1,
        "id": jid,
        "title": _text(title, 80),
        "s": schema_key(s),
        "schema": s,
        "data": cleaned_data,
        "loc": loc_out,
        "to": _text(to, 64),
        "st": "open",
        "created": int(time.time() * 1000),
    }


def merge_states(states, schema=None):
    """Merge state snapshots across devices (highest rev per device; LWW across devices)."""
    by_dev = {}
    for raw in states or ():
        try:
            st = clean_state(raw, schema)
        except ValueError:
            continue
        dev = st["dev"]
        prev = by_dev.get(dev)
        if prev is None or (st["rev"], st["t"]) > (prev["rev"], prev["t"]):
            by_dev[dev] = st

    ordered = sorted(by_dev.values(), key=lambda x: (x["t"], x["rev"], x["dev"]))
    inst = {}
    ans = {}
    note = {}
    ph = {}
    skip = {}
    closed = None
    job_id = ordered[-1]["job"] if ordered else ""
    s_ref = ordered[-1]["s"] if ordered else (schema_key(schema) if schema else "")

    for st in ordered:
        for iid, info in st["inst"].items():
            cur = inst.get(iid)
            if cur is None:
                inst[iid] = dict(info)
            else:
                if info["del"] > cur["del"]:
                    cur["del"] = info["del"]
                if info["t"] >= cur["t"]:
                    cur["o"] = info["o"]
                    cur["t"] = info["t"]
        for k, qmap in st["ans"].items():
            dst = ans.setdefault(k, {})
            for qname, pair in qmap.items():
                cur = dst.get(qname)
                if cur is None or pair[1] >= cur[1]:
                    dst[qname] = list(pair)
        for k, pair in st["note"].items():
            cur = note.get(k)
            if cur is None or pair[1] >= cur[1]:
                note[k] = list(pair)
        for k, ids in st["ph"].items():
            dst = ph.setdefault(k, [])
            for pid in ids:
                if pid not in dst:
                    dst.append(pid)
        for k, pair in st["skip"].items():
            cur = skip.get(k)
            if cur is None or pair[1] >= cur[1]:
                skip[k] = list(pair)
        if st["closed"] is not None:
            if closed is None or st["closed"] > closed:
                closed = st["closed"]

    return {"v": 1, "job": job_id, "dev": "", "by": "", "rev": max((s["rev"] for s in ordered), default=0),
            "t": max((s["t"] for s in ordered), default=0), "s": s_ref,
            "inst": inst, "ans": ans, "note": note, "ph": ph, "skip": skip, "closed": closed,
            "devices": list(by_dev)}


def _instance_title(node, k, ans_map, order):
    q_vals = ans_map.get(k) or {}
    for q in node.get("q") or ():
        if q.get("title"):
            val = q_vals.get(q["name"])
            v = val[0] if isinstance(val, (list, tuple)) else val
            if v not in (None, ""):
                return str(v)
    return f"{node['label']} {order}"


def _rebuild_instances_from_records(inst, records, job_id):
    out = {iid: dict(info) for iid, info in (inst or {}).items()}
    for r in records or ():
        cap = clean_cap(r.get("cap"))
        if not cap or (job_id and cap["j"] != job_id):
            continue
        parent_iid = ""
        for nid, iid, order in cap.get("ch") or ():
            if iid not in out:
                out[iid] = {"n": nid, "p": parent_iid, "o": order, "t": 0, "del": 0}
            parent_iid = iid
    return out


def _question_answered(q, q_vals):
    if q["name"] not in q_vals:
        return False
    raw = q_vals[q["name"]]
    val = raw[0] if isinstance(raw, (list, tuple)) else raw
    if val is None or val == "" or val == []:
        return False
    checked = clean_values([q], {q["name"]: val})
    return q["name"] in checked


def tree(schema, state=None, records=()):
    """Build the evaluated checklist tree from schema, state, and received photo records."""
    s = clean_schema(schema) if not (isinstance(schema, dict) and schema.get("biruni_capture") == 1) else schema
    st = state if isinstance(state, dict) and "inst" in state else (clean_state(state, s) if isinstance(state, dict) and state.get("job") else
                                                                    {"job": "", "inst": {}, "ans": {}, "note": {}, "ph": {}, "skip": {}, "closed": None})
    job_id = st.get("job") or ""
    inst = _rebuild_instances_from_records(st.get("inst") or {}, records, job_id)
    ans_map = st.get("ans") or {}
    note_map = st.get("note") or {}
    ph_map = st.get("ph") or {}
    skip_map = st.get("skip") or {}

    rec_by_slot = {}
    extra_records = []
    all_rec_ids = set()
    for r in records or ():
        if not isinstance(r, dict) or not r.get("id"):
            continue
        cap = clean_cap(r.get("cap"))
        if not cap:
            continue
        if job_id and cap["j"] != job_id:
            continue
        all_rec_ids.add(r["id"])
        if cap["k"] == "*":
            extra_records.append(r)
        else:
            rec_by_slot.setdefault(cap["k"], []).append(r)

    def _skip_reason(k, inherited):
        if inherited:
            return inherited
        pair = skip_map.get(k)
        txt = (pair[0] if isinstance(pair, (list, tuple)) else pair) if pair else ""
        txt = str(txt or "").strip()
        return txt if len(txt) >= MIN_SKIP_LEN else ""

    def _eval_slot(node, iid, path_parts, chain, inherited_skip, order=None):
        nid = node["id"]
        k = f"{iid}:{nid}"
        req = bool(node.get("req", True))
        own_skip = _skip_reason(k, "")
        skip_reason = inherited_skip or own_skip
        skipped = bool(skip_reason)
        pmin, pmax = node.get("photos") or (0, 0)
        slot_recs = rec_by_slot.get(k) or []
        rec_ids = [r["id"] for r in slot_recs]
        state_pids = list(ph_map.get(k) or ())
        all_pids = list(dict.fromkeys(rec_ids + state_pids))
        pending_pids = [pid for pid in state_pids if pid not in all_rec_ids]

        q_defs = node.get("q") or ()
        q_vals = ans_map.get(k) or {}
        answers = {}
        missing_q = []
        for q in q_defs:
            if _question_answered(q, q_vals):
                raw_v = q_vals[q["name"]]
                answers[q["name"]] = raw_v[0] if isinstance(raw_v, (list, tuple)) else raw_v
            elif q.get("req"):
                missing_q.append(q["name"])

        note_pair = note_map.get(k)
        note_txt = (note_pair[0] if isinstance(note_pair, (list, tuple)) else note_pair) if note_pair else ""

        children = [_eval_node(ch, iid, path_parts, chain, skip_reason) for ch in (node.get("nodes") or ())]
        own_photos_ok = len(all_pids) >= pmin
        own_q_ok = len(missing_q) == 0
        children_ok = all(c["done"] or c["skipped"] or not c["req"] for c in children)
        done = own_photos_ok and own_q_ok and children_ok
        return {
            "kind": "instance" if order is not None else "node",
            "id": nid,
            "iid": iid,
            "order": order,
            "k": k,
            "label": path_parts[-1],
            "path": list(path_parts),
            "path_label": " › ".join(path_parts),
            "ch": list(chain),
            "req": req,
            "photos": [pmin, pmax],
            "photo_count": len(all_pids),
            "received_count": len(rec_ids),
            "pending_count": len(pending_pids),
            "photo_ids": all_pids,
            "records": slot_recs,
            "q": list(q_defs),
            "answers": answers,
            "missing_q": missing_q,
            "note": note_txt or "",
            "own_skip": own_skip,
            "skip": skip_reason,
            "skipped": skipped,
            "own_done": own_photos_ok and own_q_ok,
            "done": done,
            "children": children,
        }

    def _eval_node(node, parent_iid, parent_path, parent_chain, inherited_skip):
        nid = node["id"]
        rep = node.get("repeat")
        if not rep:
            return _eval_slot(node, parent_iid, parent_path + [node["label"]], parent_chain, inherited_skip)

        k = f"{parent_iid}:{nid}"
        req = bool(node.get("req", True))
        own_skip = _skip_reason(k, "")
        skip_reason = inherited_skip or own_skip
        skipped = bool(skip_reason)
        rmin, rmax = rep["min"], rep["max"]
        live = [(iid, info) for iid, info in inst.items()
                if info.get("n") == nid and (info.get("p") or "") == parent_iid and not info.get("del")]
        live.sort(key=lambda x: (x[1].get("o", 1), x[0]))
        instances = []
        for idx, (iid, info) in enumerate(live, 1):
            order = info.get("o") or idx
            inst_k = f"{iid}:{nid}"
            title = _instance_title(node, inst_k, ans_map, order)
            inst_item = _eval_slot(node, iid, parent_path + [title], parent_chain + [[nid, iid, order]], skip_reason, order=order)
            instances.append(inst_item)

        count_ok = len(instances) >= rmin
        instances_ok = all(it["done"] or it["skipped"] for it in instances)
        done = count_ok and instances_ok
        return {
            "kind": "repeat",
            "id": nid,
            "iid": parent_iid,
            "k": k,
            "label": node["label"],
            "path": parent_path + [node["label"]],
            "path_label": " › ".join(parent_path + [node["label"]]),
            "ch": list(parent_chain),
            "req": req,
            "repeat": {"min": rmin, "max": rmax},
            "instance_count": len(instances),
            "instances": instances,
            "own_skip": own_skip,
            "skip": skip_reason,
            "skipped": skipped,
            "count_ok": count_ok,
            "done": done,
        }

    roots = [_eval_node(n, "", [], [], "") for n in (s.get("nodes") or ())]
    return {
        "schema": s,
        "job": job_id,
        "roots": roots,
        "extra_records": extra_records,
        "extra_photo_ids": list(dict.fromkeys([r["id"] for r in extra_records] + list(ph_map.get("*") or ()))),
    }


def missing(schema, state=None, records=()):
    """List every required item that is neither done nor covered by a valid skip reason."""
    t = tree(schema, state, records) if not (isinstance(schema, dict) and "roots" in schema) else schema
    out = []

    def _walk_slot(item):
        if not item["req"] or item["skipped"]:
            return
        need_photos = max(0, item["photos"][0] - item["photo_count"])
        if need_photos > 0 or item["missing_q"]:
            out.append({
                "k": item["k"],
                "id": item["id"],
                "iid": item["iid"],
                "path_label": item["path_label"],
                "reason": "slot",
                "need_photos": need_photos,
                "missing_q": list(item["missing_q"]),
            })
        for ch in item.get("children") or ():
            _walk(ch)

    def _walk(item):
        if not item["req"] or item["skipped"]:
            return
        if item["kind"] == "repeat":
            rmin = item["repeat"]["min"]
            if item["instance_count"] < rmin:
                out.append({
                    "k": item["k"],
                    "id": item["id"],
                    "iid": item["iid"],
                    "path_label": item["path_label"],
                    "reason": "repeat_min",
                    "have": item["instance_count"],
                    "min": rmin,
                })
            for inst_it in item["instances"]:
                _walk_slot(inst_it)
        else:
            _walk_slot(item)

    for r in t["roots"]:
        _walk(r)
    return out


def path_label(schema, state, k, ch=()):
    """Human-readable breadcrumb path ('Floor 2 › Apt 4 › Kitchen') for a slot key."""
    if k == "*":
        return "*"
    t = tree(schema, state)
    found = []

    def _find(item):
        if item.get("k") == k and item["kind"] != "repeat":
            found.append(item["path_label"])
            return
        for sub in item.get("instances") or item.get("children") or ():
            _find(sub)

    for r in t["roots"]:
        _find(r)
        if found:
            return found[0]

    s = clean_schema(schema) if not (isinstance(schema, dict) and schema.get("biruni_capture") == 1) else schema
    node_map = {}

    def _map(nodes):
        for n in nodes:
            node_map[n["id"]] = n
            if n.get("nodes"):
                _map(n["nodes"])

    _map(s.get("nodes") or ())
    ans_map = (state.get("ans") if isinstance(state, dict) else None) or {}
    parts = []
    for nid, iid, order in ch or ():
        n = node_map.get(nid)
        if n:
            parts.append(_instance_title(n, f"{iid}:{nid}", ans_map, order))
    target_nid = k.split(":", 1)[1] if ":" in k else k
    if target_nid in node_map and (not ch or ch[-1][0] != target_nid):
        parts.append(node_map[target_nid]["label"])
    return " › ".join(parts)


def stats(schema, state=None, records=()):
    """Roll up instance counts, choice/yesno tallies, integer sums, photos per section, and skipped items."""
    t = tree(schema, state, records) if not (isinstance(schema, dict) and "roots" in schema) else schema
    instances = {}
    choices = {}
    sums = {}
    section_photos = {}
    skipped = []

    def _tally_slot(item, root_label):
        if item.get("own_skip"):
            skipped.append({"k": item["k"], "path_label": item["path_label"], "reason": item["own_skip"]})
        section_photos[root_label] = section_photos.get(root_label, 0) + item["photo_count"]
        for q in item.get("q") or ():
            qname, qtype = q["name"], q["type"]
            if qname not in item["answers"]:
                continue
            val = item["answers"][qname]
            key = f"{item['id']}.{qname}"
            if qtype in ("choice", "yesno"):
                bucket = choices.setdefault(key, {})
                sval = "yes" if val is True else "no" if val is False else str(val)
                bucket[sval] = bucket.get(sval, 0) + 1
            elif qtype == "multi":
                bucket = choices.setdefault(key, {})
                for part in (val if isinstance(val, list) else str(val).split(";")):
                    part = part.strip()
                    if part:
                        bucket[part] = bucket.get(part, 0) + 1
            elif qtype in ("integer", "number"):
                try:
                    num_v = int(val) if qtype == "integer" else float(val)
                    sums[key] = sums.get(key, 0) + num_v
                except (TypeError, ValueError):
                    pass
        for ch in item.get("children") or ():
            _walk(ch, root_label)

    def _walk(item, root_label):
        if item["kind"] == "repeat":
            if item.get("own_skip"):
                skipped.append({"k": item["k"], "path_label": item["path_label"], "reason": item["own_skip"]})
            instances[item["id"]] = instances.get(item["id"], 0) + item["instance_count"]
            for inst_it in item["instances"]:
                _tally_slot(inst_it, root_label)
        else:
            _tally_slot(item, root_label)

    for r in t["roots"]:
        section_photos.setdefault(r["label"], 0)
        _walk(r, r["label"])

    miss = missing(t)
    return {
        "instances": instances,
        "choices": choices,
        "sums": sums,
        "section_photos": section_photos,
        "extra_photos": len(t.get("extra_photo_ids") or ()),
        "skipped": skipped,
        "missing": miss,
        "complete": len(miss) == 0,
    }


def progress_counts(schema, state=None, records=()):
    """Compute checklist completion counts and percent (matches phone capProgress + missing)."""
    t = tree(schema, state, records) if not (isinstance(schema, dict) and "roots" in schema) else schema
    done = [0]
    total = [0]

    def _walk_slot(it):
        if not (it.get("children")) or it["photos"][0] > 0 or len(it.get("q") or ()) > 0:
            if it["req"] or it["own_done"] or it["skipped"]:
                total[0] += 1
                if it["own_done"] or it["skipped"]:
                    done[0] += 1
        for ch in it.get("children") or ():
            _walk(ch)

    def _walk(it):
        if it["kind"] == "repeat":
            if it["req"] and it["instance_count"] < it["repeat"]["min"]:
                total[0] += it["repeat"]["min"] - it["instance_count"]
            for sub in it["instances"]:
                _walk_slot(sub)
        else:
            _walk_slot(it)

    for r in t["roots"]:
        _walk(r)

    miss = missing(t)
    if len(miss) == 0:
        pct = 100 if (total[0] > 0 or not t["roots"]) else 100
    elif total[0] > 0:
        pct = min(99, round(100 * done[0] / total[0]))
    else:
        pct = 0
    return {"done": done[0], "total": total[0], "missing": len(miss), "percent": pct}


def duplicate_instances(schema, state=None, records=()):
    """Detect duplicate repeat instances (e.g. two surveyors offline creating the same order/title instance)."""
    t = tree(schema, state, records) if not (isinstance(schema, dict) and "roots" in schema) else schema
    dups = []

    def _walk(it):
        if it["kind"] == "repeat":
            by_label = {}
            by_order = {}
            for inst_it in it["instances"]:
                by_label.setdefault(inst_it["label"], []).append(inst_it)
                if inst_it.get("order") is not None:
                    by_order.setdefault(inst_it["order"], []).append(inst_it)
            seen_iids = set()
            for group in list(by_label.values()) + list(by_order.values()):
                if len(group) > 1:
                    iids = tuple(sorted(g["iid"] for g in group))
                    if iids not in seen_iids:
                        seen_iids.add(iids)
                        dups.append({
                            "repeat_id": it["id"],
                            "repeat_k": it["k"],
                            "path_label": group[0]["path_label"],
                            "iids": [g["iid"] for g in group],
                            "instances": group,
                        })
            for inst_it in it["instances"]:
                for ch in inst_it.get("children") or ():
                    _walk(ch)
        else:
            for ch in it.get("children") or ():
                _walk(ch)

    for r in t["roots"]:
        _walk(r)
    return dups


def cap_payload(jobs, limit=MAX_CAP):
    """Build the guide.cap dictionary for open jobs; raises ValueError if it exceeds limit bytes."""
    schemas = {}
    job_list = []
    for j in jobs or ():
        if not isinstance(j, dict) or j.get("st", "open") != "open":
            continue
        s = clean_schema(j["schema"])
        skey = schema_key(s)
        schemas[skey] = s
        job_list.append({
            "id": j["id"],
            "title": _text(j.get("title"), 80) or j["id"],
            "s": skey,
            "data": j.get("data") or {},
            "loc": j.get("loc"),
            "to": _text(j.get("to"), 64),
            "st": "open",
        })
    if not job_list:
        return None
    payload = {"v": 1, "schemas": schemas, "jobs": job_list}
    raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if len(raw) > limit:
        raise ValueError(f"capture payload ({len(raw)} bytes) exceeds limit ({limit} bytes)")
    return payload
