"""Readiness and model conversion reports: one self-contained HTML page in the interface language (right to left
for Arabic and Persian) and an Excel workbook (Summary, Items, Missing)."""
import datetime
import html
import os

from ..i18n import is_rtl, lang, tr
from ..version import APP_NAME
from . import scanner

_STATUS_COLOURS = {"ok": "#15803d", "changes": "#b45309", "needs": "#b91c1c", "fails": "#6b7280",
                   "unknown": "#475569"}


def _e(text):
    return html.escape(str(text if text is not None else ""))


def _css(rtl):
    side = "right" if rtl else "left"
    return f"""
body {{ font-family: 'Segoe UI', Tahoma, Arial, sans-serif; margin: 24px; color: #1f2937; background: #fff; }}
h1 {{ font-size: 22px; margin: 0 0 4px; }} h2 {{ font-size: 17px; margin-top: 28px; }}
.meta {{ color: #6b7280; font-size: 13px; }}
.summary {{ background: #f1f5f9; border-{side}: 4px solid #0f766e; padding: 12px 16px; margin: 16px 0;
            font-size: 15px; line-height: 1.6; }}
.cards {{ display: flex; flex-wrap: wrap; gap: 10px; }}
.card {{ border: 1px solid #e5e7eb; border-radius: 6px; padding: 10px 14px; min-width: 150px; }}
.card b {{ display: block; font-size: 20px; }}
table {{ border-collapse: collapse; width: 100%; font-size: 13px; }}
th, td {{ border-bottom: 1px solid #e5e7eb; padding: 5px 8px; text-align: {side}; vertical-align: top; }}
th {{ background: #f8fafc; position: sticky; top: 0; }}
.st {{ font-weight: 600; white-space: nowrap; }}
.path {{ color: #6b7280; font-size: 12px; direction: ltr; unicode-bidi: embed; word-break: break-all; }}
.code {{ font-family: Consolas, monospace; direction: ltr; unicode-bidi: embed; }}
details summary {{ cursor: pointer; color: #0f766e; }}
"""


def _page(title, body):
    rtl = is_rtl()
    return (f"<!DOCTYPE html>\n<html lang=\"{_e(lang())}\" dir=\"{'rtl' if rtl else 'ltr'}\"><head>"
            f"<meta charset=\"utf-8\"><title>{_e(title)}</title><style>{_css(rtl)}</style></head>"
            f"<body>{body}</body></html>\n")


def _status_cell(status):
    return (f"<span class=\"st\" style=\"color:{_STATUS_COLOURS.get(status, '#000')}\">"
            f"{_e(scanner.status_label(status))}</span>")


def html_text(result):
    items = result.items
    title = tr("Migration readiness report")
    when = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    out = [f"<h1>{_e(title)}</h1>",
           "<div class=\"meta\">" + _e(tr("{app} - {when} - {n} items in {s} seconds").format(
               app=APP_NAME, when=when, n=len(items), s=round(result.seconds, 1))) + "<br>"
           + "<br>".join(f"<span class=\"path\">{_e(f)}</span>" for f in result.folders) + "</div>"]
    if result.cancelled:
        out.append("<p><b>" + _e(tr("The scan was stopped before the end: the numbers cover what was checked."))
                   + "</b></p>")
    out.append(f"<div class=\"summary\">{_e(scanner.summary_text(items))}</div>")

    # per kind cards
    out.append(f"<h2>{_e(tr('By kind'))}</h2><table><tr><th>{_e(tr('Kind'))}</th><th>{_e(tr('Items'))}</th>"
               + "".join(f"<th>{_e(scanner.status_label(s))}</th>" for s in scanner.STATUSES) + "</tr>")
    summary = scanner.summarise(items)
    for kind in scanner.KINDS:
        c = summary.get(kind)
        if not c:
            continue
        out.append(f"<tr><td>{_e(scanner.kind_label(kind))}</td><td>{sum(c.values())}</td>"
                   + "".join(f"<td>{c.get(s, 0) or ''}</td>" for s in scanner.STATUSES) + "</tr>")
    out.append("</table>")

    top = scanner.top_missing(items, 40)
    if top:
        out.append(f"<h2>{_e(tr('Most frequent missing functions and features'))}</h2>"
                   f"<table><tr><th>#</th><th>{_e(tr('Function or feature'))}</th><th>{_e(tr('Type'))}</th>"
                   f"<th>{_e(tr('Files'))}</th><th>{_e(tr('Times'))}</th></tr>")
        for i, (cat, key, files, times) in enumerate(top, 1):
            code = cat in ("arcpy", "arcpy_partial", "tool")
            out.append(f"<tr><td>{i}</td><td class=\"{'code' if code else ''}\">{_e(key)}</td>"
                       f"<td>{_e(scanner.category_label(cat))}</td><td>{files}</td><td>{times}</td></tr>")
        out.append("</table>")

    out.append(f"<h2>{_e(tr('All items'))}</h2><table><tr><th>{_e(tr('Kind'))}</th><th>{_e(tr('Name'))}</th>"
               f"<th>{_e(tr('Status'))}</th><th>{_e(tr('Score'))}</th><th>{_e(tr('Details'))}</th></tr>")
    order = {s: i for i, s in enumerate(("needs", "fails", "changes", "unknown", "ok"))}
    for it in sorted(items, key=lambda i: (order.get(i.status, 9), i.kind, i.path.lower())):
        more = ""
        if it.details:
            more = (f"<details><summary>{_e(tr('more'))}</summary>"
                    + "<br>".join(_e(d) for d in it.details[:60]) + "</details>")
        out.append(f"<tr><td>{_e(scanner.kind_label(it.kind))}</td><td>{_e(it.name)}"
                   f"<div class=\"path\">{_e(it.path)}</div></td><td>{_status_cell(it.status)}</td>"
                   f"<td>{it.score}</td><td>{_e(it.note)}{more}</td></tr>")
    out.append("</table>")
    return _page(title, "\n".join(out))


def write_html(result, path):
    with open(path, "w", encoding="utf-8") as f:
        f.write(html_text(result))
    return path


def write_excel(result, path):
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill

    rtl = is_rtl()
    wb = Workbook()
    head_font = Font(bold=True, color="FFFFFF")
    head_fill = PatternFill("solid", fgColor="0F766E")

    def header(ws, names, widths):
        ws.append(names)
        for i, cell in enumerate(ws[1], 0):
            cell.font, cell.fill = head_font, head_fill
            ws.column_dimensions[cell.column_letter].width = widths[i]
        ws.freeze_panes = "A2"
        ws.sheet_view.rightToLeft = rtl

    ws = wb.active
    ws.title = tr("Summary")[:31]
    ws.sheet_view.rightToLeft = rtl
    ws.append([tr("Migration readiness report")])
    ws["A1"].font = Font(bold=True, size=14)
    ws.append([scanner.summary_text(result.items)])
    ws["A2"].alignment = Alignment(wrap_text=True, vertical="top")
    ws.row_dimensions[2].height = 90
    ws.column_dimensions["A"].width = 30
    ws.merge_cells("A2:G2")
    ws.append([tr("Folders"), "; ".join(result.folders)])
    ws.append([tr("Seconds"), round(result.seconds, 1)])
    ws.append([])
    ws.append([tr("Kind"), tr("Items")] + [scanner.status_label(s) for s in scanner.STATUSES])
    for cell in ws[ws.max_row]:
        cell.font, cell.fill = head_font, head_fill
    for kind in scanner.KINDS:
        c = scanner.summarise(result.items).get(kind)
        if c:
            ws.append([scanner.kind_label(kind), sum(c.values())] + [c.get(s, 0) for s in scanner.STATUSES])

    ws = wb.create_sheet(tr("Items")[:31])
    header(ws, [tr("Kind"), tr("Name"), tr("Status"), tr("Score"), tr("Details"), tr("Path"), tr("Seconds")],
           [14, 36, 18, 8, 70, 70, 9])
    for it in result.items:
        ws.append([scanner.kind_label(it.kind), it.name, scanner.status_label(it.status), it.score, it.note,
                   it.path, round(it.seconds, 2)])
    ws.auto_filter.ref = ws.dimensions

    ws = wb.create_sheet(tr("Missing")[:31])
    header(ws, ["#", tr("Function or feature"), tr("Type"), tr("Files"), tr("Times")], [6, 60, 30, 10, 10])
    for i, (cat, key, files, times) in enumerate(scanner.top_missing(result.items, 500), 1):
        ws.append([i, key, scanner.category_label(cat), files, times])
    ws.auto_filter.ref = ws.dimensions
    wb.save(path)
    return path


# ---- model conversion report --------------------------------------------------------------------------------------
def conversion_html(conversions, title=None):
    title = title or tr("ModelBuilder import report")
    out = [f"<h1>{_e(title)}</h1>"]
    total = sum(len(c.steps) for c in conversions)
    done = sum(c.converted for c in conversions)
    saved = sum(1 for c in conversions if c.file)
    out.append("<div class=\"summary\">" + _e(tr("{m} models, {done} of {total} steps converted, {saved} models "
                                                  "saved to the toolbox (Models).").format(
        m=len(conversions), done=done, total=total, saved=saved)) + "</div>")
    for c in conversions:
        out.append(f"<h2>{_e(c.title)}</h2><div class=\"path\">{_e(c.toolbox)}</div>")
        if c.error:
            out.append(f"<p><b>{_e(c.error)}</b></p>")
        if c.file:
            out.append("<p>" + _e(tr("Saved as")) + f" <span class=\"path\">{_e(c.file)}</span></p>")
        if c.inputs:
            out.append("<p>" + _e(tr("Model inputs")) + ": " + ", ".join(_e(i[1]) for i in c.inputs) + "</p>")
        if c.outputs:
            out.append("<p>" + _e(tr("Model outputs")) + ": " + ", ".join(_e(o) for o in c.outputs) + "</p>")
        if c.steps:
            out.append(f"<table><tr><th>{_e(tr('Step'))}</th><th>{_e(tr('ArcGIS tool'))}</th>"
                       f"<th>{_e(tr('Converted to'))}</th><th>{_e(tr('Notes'))}</th></tr>")
            for s in c.steps:
                conv = s.algorithm if s.algorithm and s.algorithm != "-" else (
                    tr("(read directly)") if s.algorithm == "-" else "")
                st = _status_cell("ok") if s.algorithm else _status_cell("needs")
                out.append(f"<tr><td>{_e(s.title)}</td><td class=\"code\">{_e(s.tool)}</td>"
                           f"<td class=\"code\">{_e(conv) or st}</td><td>{'<br>'.join(_e(n) for n in s.notes)}</td></tr>")
            out.append("</table>")
        for w in c.warnings:
            out.append(f"<p>{_e(w)}</p>")
    return _page(title, "\n".join(out))


def write_conversion_report(conversions, path, title=None):
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(conversion_html(conversions, title))
    return path
