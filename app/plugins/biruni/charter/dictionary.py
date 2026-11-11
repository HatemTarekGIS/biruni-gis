"""The data dictionary: the charters of the project as one bilingual HTML page (opens in Word and any browser)
for a client or a supervisor - layers, fields, aliases, types, units, descriptions, value lists, kinds, rules,
linked choices and a health summary."""
import datetime as dt
import html

from qgis.core import QgsProject

from ..version import APP_NAME
from . import model

# (English, Arabic) - the dictionary is always written in both languages
DOC_WORDS = {
    "title": ("Data dictionary", "قاموس البيانات"),
    "made": ("Made with", "عُمل ببرنامج"),
    "on": ("on", "في"),
    "layers": ("Layers", "الطبقات"),
    "layer": ("Layer", "الطبقة"),
    "features": ("Features", "عدد العناصر"),
    "fields": ("Fields", "الحقول"),
    "field": ("Field", "الحقل"),
    "alias_ar": ("Arabic name", "الاسم بالعربي"),
    "alias_en": ("English name", "الاسم بالإنجليزي"),
    "type": ("Type", "النوع"),
    "length": ("Length", "الطول"),
    "unit": ("Unit", "الوحدة"),
    "required": ("Required", "مطلوب"),
    "unique": ("Unique", "لا يتكرر"),
    "default": ("Default / automatic", "القيمة التلقائية"),
    "list": ("Value list", "القائمة"),
    "desc": ("Description", "الوصف"),
    "lists": ("Value lists", "القوائم"),
    "code": ("Code", "الرمز"),
    "label_ar": ("Arabic label", "الاسم بالعربي"),
    "label_en": ("English label", "الاسم بالإنجليزي"),
    "range": ("Range", "المدى"),
    "used_by": ("Used by", "مستخدمة في"),
    "kinds": ("Kinds", "الأنواع"),
    "kind_field": ("Kind field", "حقل النوع"),
    "kind": ("Kind", "النوع"),
    "defaults": ("Defaults", "القيم التلقائية"),
    "allowed": ("Allowed values", "القيم المسموحة"),
    "rules": ("Validation rules", "قواعد التحقق"),
    "rule": ("Rule", "القاعدة"),
    "message": ("Message", "الرسالة"),
    "level": ("Level", "المستوى"),
    "error": ("Error", "خطأ"),
    "warning": ("Warning", "تنبيه"),
    "links": ("Linked choices", "الاختيارات المترابطة"),
    "parents": ("When", "لما"),
    "child": ("Limits", "يحدد"),
    "health": ("Health", "صحة البيانات"),
    "score": ("Score", "الدرجة"),
    "problems": ("Problems", "المشاكل"),
    "yes": ("Yes", "نعم"),
    "none": ("No charter yet.", "لا يوجد دستور بعد."),
    "log": ("Change log", "سجل التغييرات"),
}
TYPE_WORDS = {"text": ("Text", "نص"), "integer": ("Whole number", "عدد صحيح"), "big": ("Big whole number", "عدد صحيح كبير"),
              "decimal": ("Decimal number", "عدد عشري"), "date": ("Date", "تاريخ"),
              "datetime": ("Date and time", "تاريخ ووقت"), "boolean": ("Yes / No", "نعم / لا")}

CSS = """
body { font-family: 'Segoe UI', 'Cairo', Tahoma, Arial, sans-serif; color: #1E252B; margin: 28px; font-size: 10.5pt; }
h1 { color: #0E7C74; margin-bottom: 2px; }
h2 { color: #0E7C74; border-bottom: 2px solid #0E7C74; padding-bottom: 3px; margin-top: 30px; }
h3 { color: #33414C; margin-top: 18px; }
.sub { color: #5B6670; }
table { border-collapse: collapse; width: 100%; margin: 8px 0 14px; }
th { background: #DDF0EE; color: #0A635D; text-align: start; }
th, td { border: 1px solid #C9D0D6; padding: 4px 7px; vertical-align: top; }
td.ar, span.ar { direction: rtl; text-align: right; font-family: 'Segoe UI', 'Cairo', Tahoma, sans-serif; }
.chip { display: inline-block; width: 11px; height: 11px; border-radius: 2px; border: 1px solid #888; }
.ok { color: #0E7C74; font-weight: 600; } .bad { color: #DC2626; font-weight: 600; }
code { font-family: Consolas, monospace; font-size: 9.5pt; }
"""


def _e(text):
    return html.escape(str(text if text is not None else ""))


def _w(key):
    en, ar = DOC_WORDS[key]
    return f"{_e(en)}<br><span class='ar'>{_e(ar)}</span>"


def _row(cells, head=False):
    tag = "th" if head else "td"
    out = []
    for c in cells:
        if isinstance(c, tuple):  # (html, css class)
            out.append(f"<{tag} class='{c[1]}'>{c[0]}</{tag}>")
        else:
            out.append(f"<{tag}>{c}</{tag}>")
    return "<tr>" + "".join(out) + "</tr>"


def _default_text(spec):
    from .. import rules as attr_rules

    auto = spec.get("auto") or ""
    if auto in attr_rules.AUTO and attr_rules.AUTO[auto][1]:
        return _e(attr_rules.AUTO[auto][0])
    if auto == "custom":
        return f"<code>{_e(spec.get('default'))}</code>"
    return _e(spec.get("default") or "")


def layer_section(layer, lists, health=True):
    c = model.stored_charter(layer)
    out = [f"<h2>{_e(layer.name())}</h2>",
           f"<p class='sub'>{_w('features')}: {layer.featureCount()}</p>"]
    if not c:
        out.append(f"<p>{_w('none')}</p>")
        return "\n".join(out)
    out.append(f"<h3>{_w('fields')}</h3><table>")
    out.append(_row([_w(k) for k in ("field", "alias_ar", "alias_en", "type", "unit", "required", "unique",
                                       "default", "list", "desc")], head=True))
    for f in c["fields"]:
        t = TYPE_WORDS.get(f.get("type"), (f.get("type"), f.get("type")))
        type_text = f"{_e(t[0])} / <span class='ar'>{_e(t[1])}</span>"
        if f.get("type") == "text" and f.get("length"):
            type_text += f" ({int(f['length'])})"
        lst = lists.get(f.get("list") or "")
        out.append(_row([f"<code>{_e(f['name'])}</code>", (_e(f.get("alias_ar")), "ar"), _e(f.get("alias_en")),
                         type_text, _e(f.get("unit")), _w("yes") if f.get("required") else "",
                         _w("yes") if f.get("unique") else "", _default_text(f),
                         (_e(model.pick(lst, "en")) + "<br><span class='ar'>" + _e(model.pick(lst, "ar")) + "</span>")
                         if lst else "", _e(f.get("desc"))]))
    out.append("</table>")
    if c["kind_field"] and c["kinds"]:
        out.append(f"<h3>{_w('kinds')}</h3><p>{_w('kind_field')}: <code>{_e(c['kind_field'])}</code></p><table>")
        out.append(_row([_w(k) for k in ("code", "label_ar", "label_en", "defaults", "allowed", "required")],
                        head=True))
        for k in c["kinds"]:
            chip = f"<span class='chip' style='background:{_e(k.get('color') or '#fff')}'></span> " if k.get("color") \
                else ""
            defaults = "<br>".join(f"<code>{_e(f)}</code> = {_e(v)}" for f, v in k["defaults"].items())
            allowed = "<br>".join(f"<code>{_e(f)}</code>: {_e(', '.join(v))}" for f, v in k["lists"].items())
            out.append(_row([chip + f"<code>{_e(k['code'])}</code>", (_e(k.get("ar")), "ar"), _e(k.get("en")),
                             defaults, allowed, ", ".join(f"<code>{_e(f)}</code>" for f in k["required"])]))
        out.append("</table>")
    if c["rules"]:
        out.append(f"<h3>{_w('rules')}</h3><table>")
        out.append(_row([_w(k) for k in ("rule", "message", "level", "field")], head=True))
        for r in c["rules"]:
            msg = _e(r.get("en")) + ("<br><span class='ar'>" + _e(r.get("ar")) + "</span>" if r.get("ar") else "")
            out.append(_row([f"<code>{_e(r['expr'])}</code>", msg, _w(r.get("level", "error")),
                             f"<code>{_e(r.get('field'))}</code>"]))
        out.append("</table>")
    if c["links"]:
        out.append(f"<h3>{_w('links')}</h3>")
        for ln in c["links"]:
            out.append(f"<p><b>{' + '.join(f'<code>{_e(p)}</code>' for p in ln['parents'])} → "
                       f"<code>{_e(ln['child'])}</code></b></p><table>")
            out.append(_row([_w("parents"), _w("allowed")], head=True))
            for key, codes in ln["pairs"].items():
                out.append(_row([_e(key.replace("|", " + ")), _e(", ".join(codes))]))
            out.append("</table>")
    if health:
        from .health import evaluate

        try:
            rep = evaluate(layer, c, lists)
            cls = "ok" if rep.score >= 90 else "bad"
            out.append(f"<h3>{_w('health')}</h3><p>{_w('score')}: <span class='{cls}'>{rep.score}%</span> · "
                       f"{rep.bad_errors} / {rep.total}</p>")
            if rep.items:
                out.append("<table>" + _row([_w("problems"), _w("field"), _w("level"), _w("features")], head=True))
                for it in rep.items:
                    out.append(_row([_e(it.title), f"<code>{_e(it.field)}</code>", _w(it.level), str(it.count)]))
                out.append("</table>")
        except Exception:  # noqa: BLE001 - the dictionary is still written
            pass
    return "\n".join(out)


def lists_section(lists, project=None):
    if not lists:
        return ""
    out = [f"<h2>{_w('lists')}</h2>"]
    for lid, lst in sorted(lists.items(), key=lambda kv: model.pick(kv[1], "en") or kv[0]):
        used = ", ".join(sorted(f"{lyr.name()}.{f}" for lyr, f in model.list_usages(lid, project)))
        out.append(f"<h3>{_e(lst.get('en') or lid)} · <span class='ar'>{_e(lst.get('ar'))}</span></h3>")
        if used:
            out.append(f"<p class='sub'>{_w('used_by')}: {_e(used)}</p>")
        if lst["kind"] == "range":
            lo = "" if lst.get("min") is None else f"{float(lst['min']):g}"
            hi = "" if lst.get("max") is None else f"{float(lst['max']):g}"
            out.append(f"<p>{_w('range')}: {lo} – {hi}</p>")
            continue
        out.append("<table>" + _row([_w("code"), _w("label_ar"), _w("label_en")], head=True))
        for it in lst["items"]:
            chip = f"<span class='chip' style='background:{_e(it['color'])}'></span> " if it.get("color") else ""
            out.append(_row([chip + f"<code>{_e(it['code'])}</code>", (_e(it.get("ar")), "ar"), _e(it.get("en"))]))
        out.append("</table>")
    return "\n".join(out)


def build_html(layers=None, project=None, health=True, log=True):
    project = project or QgsProject.instance()
    layers = layers if layers is not None else model.charter_layers(project)
    lists = model.project_lists(project)
    used = set()
    for lyr in layers:
        c = model.stored_charter(lyr) or {"fields": []}
        used |= {f.get("list") for f in c["fields"] if f.get("list")}
    shown = {lid: lst for lid, lst in lists.items() if lid in used}
    title_en, title_ar = DOC_WORDS["title"]
    name = project.baseName() or ""
    parts = ["<!DOCTYPE html><html><head><meta http-equiv='Content-Type' content='text/html; charset=utf-8'>",
             f"<title>{_e(title_en)} - {_e(name)}</title><style>{CSS}</style></head><body>",
             f"<h1>{_e(title_en)} · <span class='ar'>{_e(title_ar)}</span></h1>",
             f"<p class='sub'>{_e(name)} · {_e(DOC_WORDS['made'][0])} {_e(APP_NAME)} · "
             f"{dt.date.today().isoformat()}</p>",
             f"<h2>{_w('layers')}</h2><table>" + _row([_w("layer"), _w("features"), _w("fields")], head=True)]
    for lyr in layers:
        c = model.stored_charter(lyr) or {"fields": []}
        parts.append(_row([_e(lyr.name()), str(lyr.featureCount()), str(len(c["fields"]))]))
    parts.append("</table>")
    for lyr in layers:
        parts.append(layer_section(lyr, lists, health))
    parts.append(lists_section(shown, project))
    if log:
        entries = [e for e in model.read_log(project) if any(e.get("layer") == lyr.name() for lyr in layers)]
        if entries:
            parts.append(f"<h2>{_w('log')}</h2><table>")
            for e in entries[-200:]:
                parts.append(_row([_e(e.get("time", "").replace("T", " ")), _e(e.get("user")), _e(e.get("layer")),
                                   _e(e.get("what"))]))
            parts.append("</table>")
    parts.append("</body></html>")
    return "\n".join(parts)


def write_html(path, layers=None, project=None):
    text = build_html(layers, project)
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
    return path
