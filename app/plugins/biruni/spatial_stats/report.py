"""One HTML report per run of a spatial statistics tool: the answer first, then the numbers, the
charts and what the result does not mean. Self-contained (charts inside), opens in any browser and
in Word, right to left for Arabic."""
import base64
import datetime as dt
import html

from .common import text_lang

CSS = """body{font:15px/1.7 "Segoe UI",Tahoma,"Noto Sans Arabic",sans-serif;color:#1d2329;max-width:900px;margin:24px auto;
padding:0 18px;background:#fff}h1{font-size:24px;margin:0 0 4px}.meta{color:#66717c;font-size:13px;margin-bottom:18px}
h2{font-size:18px;margin:26px 0 8px;border-bottom:2px solid #12A594;padding-bottom:4px}
.answer{background:#E1F4F1;border-inline-start:5px solid #12A594;padding:12px 16px;border-radius:8px;font-size:16px}
.warn{background:#FBF0DC;border-inline-start:5px solid #C98A12;padding:10px 14px;border-radius:8px;margin:8px 0}
table{border-collapse:collapse;margin:8px 0;font-size:14px}th,td{border:1px solid #d9dfe4;padding:5px 10px;text-align:start}
th{background:#f3f6f8}td.n{font-variant-numeric:tabular-nums;direction:ltr;text-align:end}img{max-width:100%;margin:8px 0}"""

WORDS = {"ar": {"made": "أُعدّ في", "warnings": "انتبه", "by": "بواسطة Biruni GIS"},
         "en": {"made": "Made on", "warnings": "Mind this", "by": "by Biruni GIS"}}


def write_report(path, title, sections, lang=None, answer=None):
    """sections: [{"heading": str, "paragraphs": [str], "table": {"columns": [...], "rows": [[...]]},
    "images": [png bytes], "warnings": [str]}]; answer: the one-paragraph answer shown first."""
    lang = lang or text_lang()
    w = WORDS.get(lang, WORDS["en"])
    e = html.escape
    out = [f'<!doctype html><html lang="{lang}" dir="{"rtl" if lang == "ar" else "ltr"}"><head><meta charset="utf-8">'
           f"<title>{e(title)}</title><style>{CSS}</style></head><body><h1>{e(title)}</h1>"
           f'<div class="meta">{w["made"]} {dt.datetime.now():%Y-%m-%d %H:%M} {w["by"]}</div>']
    if answer:
        out.append(f'<div class="answer">{e(answer)}</div>')
    for s in sections:
        if s.get("heading"):
            out.append(f"<h2>{e(s['heading'])}</h2>")
        for p in s.get("paragraphs") or ():
            out.append(f"<p>{e(p)}</p>")
        t = s.get("table")
        if t:
            out.append("<table><tr>" + "".join(f"<th>{e(str(c))}</th>" for c in t["columns"]) + "</tr>")
            for row in t["rows"]:
                out.append("<tr>" + "".join(
                    f'<td class="n">{e(_num(v))}</td>' if isinstance(v, (int, float)) else f"<td>{e(str(v))}</td>"
                    for v in row) + "</tr>")
            out.append("</table>")
        for img in s.get("images") or ():
            out.append(f'<img alt="" src="data:image/png;base64,{base64.b64encode(img).decode()}">')
        for warn in s.get("warnings") or ():
            out.append(f'<div class="warn"><b>{w["warnings"]}:</b> {e(warn)}</div>')
    out.append("</body></html>")
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(out))
    return path


def _num(v):
    if isinstance(v, bool):
        return str(v)
    if isinstance(v, int):
        return f"{v:,}"
    if v != v:
        return "-"
    return f"{v:,.4g}" if abs(v) < 1e5 else f"{v:,.0f}"
