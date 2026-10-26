"""Excel activity report (openpyxl): summary with charts, then daily, per-layer,
per-tool, per-user, session and raw-log sheets. Follows the UI language
(Arabic sheets are right-to-left)."""
import datetime as dt
from collections import Counter, defaultdict

from .activity import EDIT_ACTIONS
from .i18n import is_rtl, tr
from .version import APP_NAME, VERSION

ACCENT = "0E7C74"
ACCENT_SOFT = "DDF0EE"


def _parse(ts):
    try:
        return dt.datetime.fromisoformat(ts) if ts else None
    except ValueError:
        return None


def _hm(minutes):
    minutes = int(minutes or 0)
    return f"{minutes // 60}:{minutes % 60:02d}"


def build_stats(events, sessions, active):
    edits = [e for e in events if e["category"] == "edit"]
    tools = [e for e in events if e["category"] == "tool"]
    s = {"events": events, "sessions": sessions, "active": active}

    s["active_minutes"] = sum(a["minutes"] for a in active)
    times = [t for t in (_parse(e["ts"]) for e in events) if t]
    s["first"], s["last"] = (min(times), max(times)) if times else (None, None)
    s["users"] = sorted({e["user"] for e in events if e["user"]})
    s["totals"] = {a: sum(e["count"] for e in edits if e["action"] == a) for a in EDIT_ACTIONS}
    s["layers_added"] = sum(1 for e in events if e["category"] == "layer" and e["action"] == "added")
    s["tools_run"] = len(tools)
    s["tracks"] = [e for e in events if e["category"] == "gps" and e["action"] == "track_recorded"]
    s["saves"] = sum(1 for e in events if e["category"] == "project" and e["action"] == "saved")

    layers = defaultdict(lambda: {a: 0 for a in EDIT_ACTIONS} | {"first": None, "last": None})
    for e in edits:
        if e["action"] not in EDIT_ACTIONS:
            continue
        row = layers[e["target"] or "?"]
        row[e["action"]] += e["count"]
        t = _parse(e["ts"])
        row["first"] = min(filter(None, [row["first"], t]), default=None)
        row["last"] = max(filter(None, [row["last"], t]), default=None)
    s["layers"] = sorted(layers.items(), key=lambda kv: -sum(kv[1][a] for a in EDIT_ACTIONS))

    tool_runs = Counter(e["target"] or e["target_id"] for e in tools)
    tool_last = {}
    tool_ids = {}
    for e in tools:
        k = e["target"] or e["target_id"]
        tool_last[k] = max(tool_last.get(k, ""), e["ts"])
        tool_ids[k] = e["target_id"]
    s["tools"] = [(k, tool_ids[k], n, tool_last[k]) for k, n in tool_runs.most_common()]

    days = defaultdict(lambda: {"minutes": 0, "sessions": set(), "tools": 0} | {a: 0 for a in EDIT_ACTIONS})
    for a in active:
        days[a["day"]]["minutes"] += a["minutes"]
    for e in events:
        d = (e["ts"] or "")[:10]
        if not d:
            continue
        days[d]["sessions"].add(e["session"])
        if e["category"] == "edit" and e["action"] in EDIT_ACTIONS:
            days[d][e["action"]] += e["count"]
        elif e["category"] == "tool":
            days[d]["tools"] += 1
    s["days"] = sorted(days.items())

    session_user = {x["session"]: (x["user"], x["machine"]) for x in sessions}
    users = defaultdict(lambda: {"sessions": set(), "minutes": 0, "edits": 0, "tools": 0, "machines": set()})
    for e in events:
        u = users[e["user"]]
        u["sessions"].add(e["session"])
        u["machines"].add(e["machine"])
        if e["category"] == "edit" and e["action"] in EDIT_ACTIONS:
            u["edits"] += e["count"]
        elif e["category"] == "tool":
            u["tools"] += 1
    for a in active:
        user = session_user.get(a["session"], ("?", ""))[0]
        users[user]["minutes"] += a["minutes"]
    s["by_user"] = sorted(users.items(), key=lambda kv: -kv[1]["minutes"])
    return s


def _labels():
    return {"features_added": tr("Features added"), "geometries_changed": tr("Geometries reshaped"),
            "attributes_changed": tr("Attribute edits"), "features_deleted": tr("Features deleted")}


def write_report(path, project_name, events, sessions, active):
    from openpyxl import Workbook
    from openpyxl.chart import BarChart, Reference
    from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
    from openpyxl.utils import get_column_letter

    s = build_stats(events, sessions, active)
    wb = Workbook()
    rtl = is_rtl()
    head_font = Font(bold=True, color="FFFFFF")
    head_fill = PatternFill("solid", fgColor=ACCENT)
    soft_fill = PatternFill("solid", fgColor=ACCENT_SOFT)
    thin = Side(style="thin", color="DDE2E7")
    box = Border(top=thin, bottom=thin, left=thin, right=thin)

    def sheet(title, headers, rows, widths=None, first=False):
        ws = wb.active if first else wb.create_sheet()
        ws.title = title[:31]
        ws.sheet_view.rightToLeft = rtl
        if headers:
            ws.append(headers)
            for c in ws[1]:
                c.font, c.fill = head_font, head_fill
                c.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
            ws.freeze_panes = "A2"
        for r in rows:
            ws.append(list(r))
        if headers and rows:
            ws.auto_filter.ref = f"A1:{get_column_letter(len(headers))}{len(rows) + 1}"
        for i, w in enumerate(widths or [], start=1):
            ws.column_dimensions[get_column_letter(i)].width = w
        return ws

    # ---- summary ----------------------------------------------------------
    ws = sheet(tr("Summary"), None, [], first=True)
    ws.column_dimensions["A"].width = 34
    ws.column_dimensions["B"].width = 30
    ws["A1"] = tr("Activity report")
    ws["A1"].font = Font(bold=True, size=16, color=ACCENT)
    ws["A2"] = project_name
    ws["A2"].font = Font(bold=True, size=12)
    fmt = "%Y-%m-%d %H:%M"
    t = s["totals"]
    rows = [
        (tr("Period"), f"{s['first'].strftime(fmt) if s['first'] else '-'}  →  "
                       f"{s['last'].strftime(fmt) if s['last'] else '-'}"),
        (tr("Working sessions"), len(s["sessions"])),
        (tr("Active working time (h:mm)"), _hm(s["active_minutes"])),
        (tr("Users"), ", ".join(s["users"]) or "-"),
        (tr("Features added"), t["features_added"]),
        (tr("Geometries reshaped"), t["geometries_changed"]),
        (tr("Attribute edits"), t["attributes_changed"]),
        (tr("Features deleted"), t["features_deleted"]),
        (tr("Total edits"), sum(t.values())),
        (tr("Edits per active hour"), round(sum(t.values()) / (s["active_minutes"] / 60), 1)
         if s["active_minutes"] else "-"),
        (tr("Layers added"), s["layers_added"]),
        (tr("Tools run"), s["tools_run"]),
        (tr("Project saves"), s["saves"]),
        (tr("GPS tracks recorded"), len(s["tracks"])),
    ]
    r0 = 4
    for i, (k, v) in enumerate(rows):
        ws.cell(row=r0 + i, column=1, value=k).font = Font(bold=True)
        ws.cell(row=r0 + i, column=2, value=v)
        for c in (1, 2):
            ws.cell(row=r0 + i, column=c).border = box
            if i % 2 == 0:
                ws.cell(row=r0 + i, column=c).fill = soft_fill
    r = r0 + len(rows) + 1
    ws.cell(row=r, column=1, value=tr("Most edited layers")).font = Font(bold=True, color=ACCENT)
    for name, row in s["layers"][:5]:
        r += 1
        ws.cell(row=r, column=1, value=name)
        ws.cell(row=r, column=2, value=sum(row[a] for a in EDIT_ACTIONS))
    r += 2
    ws.cell(row=r, column=1, value=tr("Most used tools")).font = Font(bold=True, color=ACCENT)
    for name, _id, n, _last in s["tools"][:5]:
        r += 1
        ws.cell(row=r, column=1, value=name)
        ws.cell(row=r, column=2, value=n)
    r += 2
    ws.cell(row=r, column=1, value=tr("Generated by {app} {v} on {d}").format(
        app=APP_NAME, v=VERSION, d=dt.datetime.now().strftime(fmt))).font = Font(italic=True, color="7B8791")

    # ---- daily ------------------------------------------------------------
    daily = sheet(tr("Daily"), [tr("Date"), tr("Active hours"), tr("Sessions"), tr("Features added"),
                                tr("Geometries reshaped"), tr("Attribute edits"), tr("Features deleted"),
                                tr("Tools run")],
                  [(d, round(v["minutes"] / 60, 2), len(v["sessions"]), v["features_added"],
                    v["geometries_changed"], v["attributes_changed"], v["features_deleted"], v["tools"])
                   for d, v in s["days"]], [13, 13, 10, 15, 18, 15, 15, 11])
    if s["days"]:
        n = len(s["days"]) + 1
        ch = BarChart()
        ch.title = tr("Active hours per day")
        ch.add_data(Reference(daily, min_col=2, min_row=1, max_row=n), titles_from_data=True)
        ch.set_categories(Reference(daily, min_col=1, min_row=2, max_row=n))
        ch.height, ch.width, ch.legend = 7, 16, None
        ws.add_chart(ch, "D3")
        ch2 = BarChart()
        ch2.type, ch2.grouping, ch2.overlap = "col", "stacked", 100
        ch2.title = tr("Edits per day")
        ch2.add_data(Reference(daily, min_col=4, max_col=7, min_row=1, max_row=n), titles_from_data=True)
        ch2.set_categories(Reference(daily, min_col=1, min_row=2, max_row=n))
        ch2.height, ch2.width = 7, 16
        ws.add_chart(ch2, "D19")

    # ---- layers / tools / users / sessions / log -------------------------
    sheet(tr("Layers"), [tr("Layer"), tr("Features added"), tr("Geometries reshaped"), tr("Attribute edits"),
                         tr("Features deleted"), tr("Total edits"), tr("First edit"), tr("Last edit")],
          [(name, v["features_added"], v["geometries_changed"], v["attributes_changed"], v["features_deleted"],
            sum(v[a] for a in EDIT_ACTIONS), v["first"].strftime(fmt) if v["first"] else "",
            v["last"].strftime(fmt) if v["last"] else "") for name, v in s["layers"]],
          [30, 15, 18, 15, 15, 12, 17, 17])
    sheet(tr("Tools"), [tr("Tool"), tr("Tool id"), tr("Runs"), tr("Last run")],
          s["tools"], [42, 34, 8, 20])
    sheet(tr("Users"), [tr("User"), tr("Computers"), tr("Sessions"), tr("Active hours"), tr("Total edits"),
                        tr("Tools run")],
          [(u, ", ".join(sorted(v["machines"])), len(v["sessions"]), round(v["minutes"] / 60, 2), v["edits"],
            v["tools"]) for u, v in s["by_user"]], [18, 22, 10, 13, 12, 11])
    sess_rows = []
    for x in s["sessions"]:
        a, b = _parse(x["started"]), _parse(x["ended"])
        sess_rows.append((x["user"], x["machine"], x["started"], x["ended"] or tr("open"),
                          _hm((b - a).total_seconds() / 60) if a and b else "", _hm(x["active_minutes"])))
    sheet(tr("Sessions"), [tr("User"), tr("Computer"), tr("Started"), tr("Ended"), tr("Duration (h:mm)"),
                           tr("Active (h:mm)")], sess_rows, [16, 18, 20, 20, 15, 14])
    action_text = _labels()
    action_text.update({
        "start": tr("Session started"), "end": tr("Session ended"), "opened": tr("Opened"), "saved": tr("Saved"),
        "created": tr("Created"), "added": tr("Added"), "removed": tr("Removed"), "run": tr("Run"),
        "editing_started": tr("Editing started"), "edits_discarded": tr("Edits discarded"),
        "fields_added": tr("Fields added"), "fields_deleted": tr("Fields deleted"),
        "connected": tr("GPS connected"), "recording_started": tr("Recording started"),
        "track_recorded": tr("Track recorded"), "restored": tr("Restored a version"),
    })
    category_text = {"session": tr("Session"), "project": tr("Project"), "layer": tr("Layer"),
                     "edit": tr("Edit"), "tool": tr("Tool"), "gps": tr("GPS"),
                     "cogo": tr("COGO")}
    sheet(tr("Log"), [tr("Time"), tr("User"), tr("Category"), tr("Action"), tr("Layer / tool"), tr("Count"),
                      tr("Details")],
          [(e["ts"], e["user"], category_text.get(e["category"], e["category"]),
            action_text.get(e["action"], e["action"]),
            e["target"], e["count"], e["details"]) for e in s["events"]],
          [20, 14, 11, 20, 34, 8, 30])
    wb.save(path)
    return s
