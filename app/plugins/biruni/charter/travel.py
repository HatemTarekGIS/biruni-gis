"""The charter travels: a ``.charter.json`` file, applying a charter to another layer by matching field names,
and the Field photos phone form generated from a charter."""
import json
import re

from qgis.core import QgsProject

from ..arabic_text import normalise
from . import model

FORMAT = "biruni-charter"


# ---- file -------------------------------------------------------------------------------------------------
def charter_document(layer, charter=None, lists=None):
    charter = model.clean_charter(charter or model.stored_charter(layer) or model.read_charter(layer)[0])
    lists = model.project_lists() if lists is None else lists
    used = {f["list"] for f in charter["fields"] if f.get("list")}
    return {"format": FORMAT, "version": model.VERSION, "layer": layer.name() if layer is not None else "",
            "charter": charter, "lists": {lid: lists[lid] for lid in sorted(used) if lid in lists}}


def export_charter(layer, path, charter=None, lists=None):
    doc = charter_document(layer, charter, lists)
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        json.dump(doc, f, ensure_ascii=False, indent=1)
    return path


def read_charter_file(path):
    """(charter, lists, source layer name). Raises ValueError for a file that is not a charter."""
    with open(path, encoding="utf-8-sig") as f:
        doc = json.load(f)
    if not isinstance(doc, dict) or doc.get("format") != FORMAT or not isinstance(doc.get("charter"), dict):
        raise ValueError("not a charter file")
    lists = {}
    for lid, lst in (doc.get("lists") or {}).items():
        if isinstance(lst, dict):
            lists[lid] = model.new_list(**{k: v for k, v in lst.items()
                                           if k in ("ar", "en", "kind", "items", "min", "max", "step")}, lid=lid)
    return model.clean_charter(doc["charter"]), lists, doc.get("layer", "")


def merge_lists(incoming, existing):
    """Adds incoming lists to the project's; a list with the same id but other content gets a new id.
    Returns (merged lists, {old id: new id})."""
    merged = dict(existing)
    renamed = {}
    for lid, lst in incoming.items():
        if lid in merged and json.dumps(merged[lid], sort_keys=True) != json.dumps(lst, sort_keys=True):
            new = model.unique_list_id(lid, merged, incoming)
            lst = dict(lst, id=new)
            renamed[lid] = new
            merged[new] = lst
        else:
            merged[lid] = lst
    return merged, renamed


# ---- matching fields --------------------------------------------------------------------------------------
def _key(text):
    return re.sub(r"[\s_\-]+", "", normalise(text or ""))


def match_fields(charter, layer):
    """{charter field: layer field or ""}: same name, then same name ignoring case / spaces / Arabic spelling,
    then a layer alias equal to a charter alias."""
    names = [f.name() for f in layer.fields()]
    by_key = {}
    for i, n in enumerate(names):
        by_key.setdefault(_key(n), n)
        alias = layer.attributeAlias(i)
        if alias:
            by_key.setdefault(_key(alias), n)
    out, used = {}, set()
    for spec in charter["fields"]:
        n = spec["name"]
        hit = n if n in names else ""
        if not hit:
            for cand in (n, spec.get("alias_ar"), spec.get("alias_en")):
                k = _key(cand)
                if k and by_key.get(k) and by_key[k] not in used:
                    hit = by_key[k]
                    break
        if hit in used:
            hit = ""
        if hit:
            used.add(hit)
        out[n] = hit
    return out


def _rename_in_expr(expr, renames):
    def sub(m):
        name = m.group(1).replace('""', '"')
        return '"' + renames.get(name, name).replace('"', '""') + '"'

    expr = re.sub(r'"((?:[^"]|"")*)"', sub, expr)
    return re.sub(r"current_value\('((?:[^']|'')*)'\)",
                  lambda m: "current_value('" + renames.get(m.group(1), m.group(1)).replace("'", "''") + "')", expr)


def remap(charter, layer, mapping, add_missing=True, list_ids=None):
    """The charter written for another layer: its fields renamed to the layer's (mapping {charter: layer}),
    the fields the layer does not have added (or dropped), the layer's other fields kept as they are."""
    c = model.clean_charter(charter)
    list_ids = list_ids or {}
    renames = {src: dst for src, dst in mapping.items() if dst}
    target, _seeded = model.read_charter(layer, {})
    have = {f["name"]: f for f in target["fields"]}
    fields, placed = [], set()
    for spec in c["fields"]:
        dst = mapping.get(spec["name"]) or ""
        if dst:
            new = dict(spec, name=dst, orig=dst, type=have[dst]["type"], length=have[dst]["length"])
        elif add_missing:
            new = dict(spec, orig="")
        else:
            continue
        if new.get("list") in list_ids:
            new["list"] = list_ids[new["list"]]
        if new.get("auto") == "custom" and new.get("default"):
            new["default"] = _rename_in_expr(new["default"], renames)
        fields.append(new)
        placed.add(new["name"])
    for name, spec in have.items():
        if name not in placed:
            fields.append(spec)
    known = {f["name"] for f in fields}

    def ren(n):
        return renames.get(n, n)

    c["fields"] = fields
    c["kind_field"] = ren(c["kind_field"]) if ren(c["kind_field"]) in known else ""
    for k in c["kinds"]:
        k["defaults"] = {ren(f): v for f, v in k["defaults"].items() if ren(f) in known}
        k["lists"] = {ren(f): v for f, v in k["lists"].items() if ren(f) in known}
        k["required"] = [ren(f) for f in k["required"] if ren(f) in known]
    for r in c["rules"]:
        r["expr"] = _rename_in_expr(r["expr"], renames)
        r["field"] = ren(r.get("field") or "") if ren(r.get("field") or "") in known else ""
    links = []
    for ln in c["links"]:
        ln = dict(ln, parents=[ren(p) for p in ln["parents"]], child=ren(ln["child"]), id=model.new_id("K"))
        if ln["child"] in known and all(p in known for p in ln["parents"]):
            links.append(ln)
    c["links"] = links
    return c


def apply_file_to_layer(path, layer, mapping=None, add_missing=True, project=None):
    """Reads a charter file and applies it to a layer (fields matched by name unless a mapping is given).
    Returns (stored charter, text of the field changes)."""
    from .apply import apply_charter

    project = project or QgsProject.instance()
    charter, lists, _src = read_charter_file(path)
    merged, renamed = merge_lists(lists, model.project_lists(project))
    mapping = mapping if mapping is not None else match_fields(charter, layer)
    c = remap(charter, layer, mapping, add_missing, renamed)
    return apply_charter(layer, c, merged, project=project, section="travel")


# ---- the Field photos phone form ---------------------------------------------------------------------------
SKIP_AUTO = ("area", "length", "x", "y", "created", "edited", "user", "editor", "number")


def phone_form(charter, lists=None, lang=None, fields=None):
    """The questions of the Field photos phone form for a charter (field_photos.server.clean_form format):
    a value list becomes a choice, a range a number with limits, yes / no, dates; required stays required.
    Fields filled automatically and the layer's key are left out. fields: names to include (all by default)."""
    from ..field_photos.server import MAX_FIELDS, clean_form

    lang = lang or model.ui_lang()
    lists = model.project_lists() if lists is None else lists
    c = model.clean_charter(charter)
    form = []
    for spec in c["fields"]:
        name = spec["name"]
        if fields is not None and name not in fields:
            continue
        if name.lower() in ("fid", "objectid", "ogc_fid") or spec.get("auto") in SKIP_AUTO or \
                spec.get("auto") == "custom":
            continue
        q = {"name": name, "label": model.field_label(spec, lang)[:60], "req": bool(spec.get("required"))}
        lst = lists.get(spec.get("list") or "")
        if name == c["kind_field"] and c["kinds"] and not lst:
            lst = {"kind": "codes", "items": c["kinds"]}
        t = spec.get("type")
        if lst and lst.get("kind") == "codes" and model.list_codes(lst):
            q["type"] = "choice"
            q["choices"] = [model.item_label(i, lang) for i in lst["items"] if str(i.get("code", ""))]
        elif t in ("integer", "big", "decimal"):
            q["type"] = "number" if t == "decimal" else "integer"
            if lst and lst.get("kind") == "range":
                q["min"], q["max"] = lst.get("min"), lst.get("max")
        elif t == "boolean":
            q["type"] = "yesno"
        elif t in ("date", "datetime"):
            q["type"] = "date"
        else:
            q["type"] = "text"
        form.append(q)
        if len(form) >= MAX_FIELDS:
            break
    return clean_form(form)


def choice_codes(charter, lists=None, lang=None):
    """{field: {label shown on the phone: code}} - to write the codes back when the photos come in."""
    lang = lang or model.ui_lang()
    lists = model.project_lists() if lists is None else lists
    out = {}
    for spec in model.clean_charter(charter)["fields"]:
        lst = lists.get(spec.get("list") or "")
        if lst and lst["kind"] == "codes":
            out[spec["name"]] = {model.item_label(i, lang): str(i["code"]) for i in lst["items"]}
    return out
