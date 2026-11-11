"""The charter as plain data (JSON-friendly dicts) and where it is kept.

* The charter of a layer lives in the layer's custom property ``biruni/charter`` (it travels with the
  project and with a saved layer style).
* Value lists are shared by the whole project: project entry ``Biruni /charter/lists``.
* The change log: project entry ``Biruni /charter/log``.

A charter::

    {"version": 1,
     "fields": [{"name", "orig", "alias_ar", "alias_en", "type", "length", "required", "unique",
                 "auto", "default", "unit", "desc", "list"}],
     "kind_field": "", "kind_symbols": True,
     "kinds": [{"code", "ar", "en", "color", "defaults": {field: value}, "lists": {field: [codes]},
                "required": [field]}],
     "rules": [{"id", "expr", "ar", "en", "level": "error" | "warning", "field"}],
     "links": [{"id", "parents": [field], "child": field, "pairs": {parent key: [child codes]}}]}

A value list::

    {"id", "ar", "en", "kind": "codes" | "range", "items": [{"code", "ar", "en", "color"}],
     "min", "max", "step"}
"""
import copy
import datetime as dt
import json
import re
import secrets

from qgis.core import QgsExpressionContextUtils, QgsProject, QgsVectorLayer
from qgis.PyQt.QtCore import QMetaType

PROP = "biruni/charter"
SCOPE = "Biruni"
LISTS_ENTRY = "/charter/lists"
LOG_ENTRY = "/charter/log"
LOG_MAX = 500
VERSION = 1

# field type keys -> (QMetaType, OGR type name)
TYPES = {
    "text": (QMetaType.Type.QString, "String"),
    "integer": (QMetaType.Type.Int, "Integer"),
    "big": (QMetaType.Type.LongLong, "Integer64"),
    "decimal": (QMetaType.Type.Double, "Real"),
    "date": (QMetaType.Type.QDate, "Date"),
    "datetime": (QMetaType.Type.QDateTime, "DateTime"),
    "boolean": (QMetaType.Type.Bool, "Boolean"),
}
NUMERIC = ("integer", "big", "decimal")
UNITS = ("", "m", "m²", "km", "km²", "ha", "dunam", "cm", "mm", "%", "°", "SAR", "EGP", "USD", "AED", "kg", "t",
         "L", "m³", "kWh", "km/h", "persons", "units")
ID_RE = re.compile(r"[^\w]+", re.UNICODE)


def ui_lang():
    """'ar' when the interface is Arabic, else 'en' (the two languages of a charter)."""
    from ..i18n import lang

    return "ar" if lang() == "ar" else "en"


def arrow():
    """→, or ← when the interface reads from the right."""
    from ..i18n import is_rtl

    return "←" if is_rtl() else "→"


def pick(obj, lang=None, fallback=""):
    """The Arabic or English text of an item ({"ar", "en"}), falling back to the other language."""
    lang = lang or ui_lang()
    other = "en" if lang == "ar" else "ar"
    return (obj.get(lang) or obj.get(other) or fallback or "").strip()


def field_label(spec, lang=None):
    """The label of a field in a language: alias (falls back to the other alias, then the name) + unit."""
    lang = lang or ui_lang()
    other = "en" if lang == "ar" else "ar"
    text = spec.get("alias_" + lang) or spec.get("alias_" + other) or spec.get("name", "")
    unit = spec.get("unit") or ""
    return f"{text} ({unit})" if unit and unit not in text else text


def type_key(field):
    t = field.type()
    if t in (QMetaType.Type.Int, QMetaType.Type.UInt, QMetaType.Type.Short, QMetaType.Type.UShort):
        return "integer"
    if t in (QMetaType.Type.LongLong, QMetaType.Type.ULongLong):
        return "big"
    if t == QMetaType.Type.Double or field.isNumeric():
        return "decimal"
    if t == QMetaType.Type.QDate:
        return "date"
    if t in (QMetaType.Type.QDateTime, QMetaType.Type.QTime):
        return "datetime"
    if t == QMetaType.Type.Bool:
        return "boolean"
    return "text"


def new_id(prefix="L"):
    return prefix + secrets.token_hex(3)


def slug(text, fallback="list"):
    s = ID_RE.sub("_", str(text or "").strip().lower()).strip("_")
    return s[:40] or fallback


def new_charter():
    return {"version": VERSION, "fields": [], "kind_field": "", "kind_symbols": True, "kinds": [], "rules": [],
            "links": []}


def field_spec(name, **kw):
    d = {"name": name, "orig": name, "alias_ar": "", "alias_en": "", "type": "text", "length": 0,
         "required": False, "unique": False, "auto": "", "default": "", "unit": "", "desc": "", "list": ""}
    d.update(kw)
    return d


def new_list(ar="", en="", kind="codes", items=(), lid=None, **kw):
    d = {"id": lid or slug(en or ar), "ar": ar, "en": en, "kind": kind,
         "items": [dict({"code": "", "ar": "", "en": "", "color": ""}, **i) for i in items],
         "min": None, "max": None, "step": None}
    d.update(kw)
    return d


def clean_charter(c):
    """A charter with every key present (old or hand-written files)."""
    out = new_charter()
    if isinstance(c, dict):
        for k in out:
            if k in c and c[k] is not None:
                out[k] = copy.deepcopy(c[k])
    out["fields"] = [field_spec(f.get("name", ""), **{k: v for k, v in f.items() if k != "name"})
                     for f in out["fields"] if isinstance(f, dict) and f.get("name")]
    for k in out["kinds"]:
        k.setdefault("ar", "")
        k.setdefault("en", "")
        k.setdefault("color", "")
        k.setdefault("defaults", {})
        k.setdefault("lists", {})
        k.setdefault("required", [])
        k["code"] = str(k.get("code", ""))
    for r in out["rules"]:
        r.setdefault("id", new_id("R"))
        r.setdefault("ar", "")
        r.setdefault("en", "")
        r.setdefault("level", "error")
        r.setdefault("field", "")
    for ln in out["links"]:
        ln.setdefault("id", new_id("K"))
        ln.setdefault("parents", [])
        ln.setdefault("pairs", {})
    out["version"] = VERSION
    return out


def find_field(charter, name):
    for f in charter["fields"]:
        if f["name"] == name:
            return f
    return None


def find_kind(charter, code):
    for k in charter["kinds"]:
        if k["code"] == str(code):
            return k
    return None


def list_codes(lst):
    return [str(i["code"]) for i in (lst or {}).get("items", []) if str(i.get("code", "")) != ""]


def item_label(item, lang=None):
    return pick(item, lang, str(item.get("code", "")))


# ---- the charter of a layer ------------------------------------------------------------------------------
def has_charter(layer):
    return isinstance(layer, QgsVectorLayer) and bool(layer.customProperty(PROP, ""))


def stored_charter(layer):
    raw = layer.customProperty(PROP, "") if isinstance(layer, QgsVectorLayer) else ""
    if not raw:
        return None
    try:
        return clean_charter(json.loads(raw))
    except (TypeError, ValueError):
        return None


def write_charter(layer, charter):
    c = clean_charter(charter)
    for f in c["fields"]:
        f["orig"] = f["name"]
    layer.setCustomProperty(PROP, json.dumps(c, ensure_ascii=False))
    return c


def read_charter(layer, lists=None):
    """(charter, new lists) of a layer. A layer without a charter starts from what it already has - its
    aliases and Biruni's attribute rules (allowed values become a value list, a range a range list)."""
    from .. import rules as attr_rules

    lists = project_lists() if lists is None else lists
    stored = stored_charter(layer)
    c = stored or new_charter()
    seeded = {}
    existing = {f["name"]: f for f in c["fields"]}
    out = []
    pk = set(layer.primaryKeyAttributes())
    read = attr_rules.read_rules(layer) if stored is None else {}
    lang = ui_lang()
    for idx, fld in enumerate(layer.fields()):
        spec = existing.get(fld.name())
        if spec is None:
            spec = field_spec(fld.name(), type=type_key(fld), length=max(0, fld.length()))
            alias = layer.attributeAlias(idx)
            if alias:
                spec["alias_" + lang] = alias
            r = read.get(fld.name())
            if r is not None and idx not in pk:
                spec["required"], spec["unique"] = r.required, r.unique
                if r.auto == "custom":
                    spec["auto"], spec["default"] = "custom", r.expression
                elif r.auto:
                    spec["auto"] = r.auto
                if r.values or r.minimum is not None or r.maximum is not None:
                    lid = unique_list_id(slug(f"{layer.name()}_{fld.name()}"), lists, seeded)
                    if r.values:
                        lst = new_list(fld.name(), fld.name(), "codes",
                                       [{"code": v, lang: v} for v in r.values], lid=lid)
                    else:
                        lst = new_list(fld.name(), fld.name(), "range", lid=lid, min=r.minimum, max=r.maximum)
                    seeded[lid] = lst
                    spec["list"] = lid
                # a constraint written by someone else is kept as a rule of the charter
                expr = layer.constraintExpression(idx)
                if expr and attr_rules.TAG not in expr and TAG not in expr:
                    c["rules"].append({"id": new_id("R"), "expr": expr, lang: layer.constraintDescription(idx),
                                       "ar" if lang == "en" else "en": "", "level": "error", "field": fld.name()})
        else:
            spec["type"] = type_key(fld)
            spec["length"] = max(0, fld.length())
        spec["orig"] = fld.name()
        out.append(spec)
    c["fields"] = out
    if c["kind_field"] and layer.fields().indexOf(c["kind_field"]) < 0:
        c["kind_field"] = ""
    return clean_charter(c), seeded


TAG = "biruni_charter"   # marks the constraint expressions written by the charter


def unique_list_id(base, *taken):
    base = base or "list"
    used = set()
    for t in taken:
        used |= set(t)
    cand, n = base, 2
    while cand in used:
        cand, n = f"{base}_{n}", n + 1
    return cand


# ---- value lists of the project ---------------------------------------------------------------------------
def project_lists(project=None):
    project = project or QgsProject.instance()
    raw, ok = project.readEntry(SCOPE, LISTS_ENTRY, "")
    try:
        data = json.loads(raw) if raw else {}
    except ValueError:
        data = {}
    out = {}
    for lid, lst in (data.items() if isinstance(data, dict) else ()):
        if isinstance(lst, dict):
            lst = new_list(**{k: v for k, v in lst.items() if k in ("ar", "en", "kind", "items", "min", "max",
                                                                     "step")}, lid=lid)
            out[lid] = lst
    return out


def save_project_lists(lists, project=None):
    project = project or QgsProject.instance()
    project.writeEntry(SCOPE, LISTS_ENTRY, json.dumps(lists, ensure_ascii=False))
    project.setDirty(True)


def list_usages(list_id, project=None):
    """[(layer, field name)] of every layer of the project whose charter uses the list."""
    project = project or QgsProject.instance()
    out = []
    for lyr in project.mapLayers().values():
        c = stored_charter(lyr)
        if not c:
            continue
        for f in c["fields"]:
            if f.get("list") == list_id:
                out.append((lyr, f["name"]))
    return out


def charter_layers(project=None):
    project = project or QgsProject.instance()
    return [lyr for lyr in project.mapLayers().values() if has_charter(lyr)]


# ---- the change log ---------------------------------------------------------------------------------------
def user_name():
    try:
        return str(QgsExpressionContextUtils.globalScope().variable("user_full_name")
                   or QgsExpressionContextUtils.globalScope().variable("user_account_name") or "")
    except Exception:  # noqa: BLE001
        return ""


def read_log(project=None):
    project = project or QgsProject.instance()
    raw, _ok = project.readEntry(SCOPE, LOG_ENTRY, "")
    try:
        items = json.loads(raw) if raw else []
    except ValueError:
        items = []
    return [i for i in items if isinstance(i, dict)]


def add_log(layer_name, section, what, project=None):
    """One line of the change log (newest last); returns it."""
    project = project or QgsProject.instance()
    items = read_log(project)
    entry = {"time": dt.datetime.now().isoformat(timespec="seconds"), "user": user_name(), "layer": layer_name,
             "section": section, "what": what}
    items.append(entry)
    project.writeEntry(SCOPE, LOG_ENTRY, json.dumps(items[-LOG_MAX:], ensure_ascii=False))
    project.setDirty(True)
    return entry


def describe_changes(old, new, old_lists=None, new_lists=None):
    """[(section, text)]: what changed between two charters, in the interface language."""
    from ..i18n import tr

    old, new = clean_charter(old or {}), clean_charter(new or {})
    out = []
    before = {f["orig"] or f["name"]: f for f in old["fields"]}
    names_before = {f["name"] for f in old["fields"]}
    for f in new["fields"]:
        o = before.get(f.get("orig") or f["name"])
        if o is None and f["name"] not in names_before:
            out.append(("fields", tr("field {f} added").format(f=f["name"])))
            continue
        o = o or {}
        if o.get("name") and o["name"] != f["name"]:
            out.append(("fields", tr("field {a} renamed to {b}").format(a=o["name"], b=f["name"])))
        words = {"alias_ar": tr("Arabic name"), "alias_en": tr("English name"), "required": tr("Required"),
                 "unique": tr("Unique"), "auto": tr("Automatic value"), "default": tr("Default"), "unit": tr("Unit"),
                 "desc": tr("Description"), "list": tr("Value list")}
        changed = [words[k] for k in words if (o.get(k) or "") != (f.get(k) or "")]
        if changed:
            out.append(("fields", tr("field {f}: {what}").format(f=f["name"], what=", ".join(changed))))
    kept = {f.get("orig") or f["name"] for f in new["fields"]} | {f["name"] for f in new["fields"]}
    for f in old["fields"]:
        if f["name"] not in kept and (f.get("orig") or f["name"]) not in kept:
            out.append(("fields", tr("field {f} deleted").format(f=f["name"])))
    if old["kind_field"] != new["kind_field"]:
        out.append(("kinds", tr("kind field: {f}").format(f=new["kind_field"] or "-")))
    if json.dumps(old["kinds"], sort_keys=True) != json.dumps(new["kinds"], sort_keys=True):
        out.append(("kinds", tr("{n} kinds").format(n=len(new["kinds"]))))
    if json.dumps(old["rules"], sort_keys=True) != json.dumps(new["rules"], sort_keys=True):
        out.append(("rules", tr("{n} validation rules").format(n=len(new["rules"]))))
    if json.dumps(old["links"], sort_keys=True) != json.dumps(new["links"], sort_keys=True):
        out.append(("links", tr("{n} linked choices").format(n=len(new["links"]))))
    if old_lists is not None and new_lists is not None:
        for lid, lst in new_lists.items():
            if lid not in old_lists:
                out.append(("lists", tr("list {l} added").format(l=pick(lst) or lid)))
            elif json.dumps(old_lists[lid], sort_keys=True) != json.dumps(lst, sort_keys=True):
                out.append(("lists", tr("list {l} changed").format(l=pick(lst) or lid)))
        for lid, lst in old_lists.items():
            if lid not in new_lists:
                out.append(("lists", tr("list {l} deleted").format(l=pick(lst) or lid)))
    return out
