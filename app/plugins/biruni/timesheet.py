"""Timesheet for freelancers and offices: the project's activity log turned into
billable hours.

Hours are the *active* minutes Biruni GIS recorded (minutes in which something
was done), per day, optionally rounded up per day. Each day lists what was done
- features drawn and edited per layer, tools run - so the client sees the work
behind the hours. The Excel sheet keeps live formulas: changing the rate
updates every amount.
"""
import datetime as dt
import math
import os
from collections import defaultdict

from qgis.core import QgsProject, QgsSettings
from qgis.PyQt.QtCore import QDate, Qt, QUrl
from qgis.PyQt.QtGui import QDesktopServices
from qgis.PyQt.QtWidgets import (QApplication, QComboBox, QDateEdit, QDialog, QDialogButtonBox, QDoubleSpinBox,
                                 QFileDialog, QFormLayout, QLabel, QLineEdit, QMessageBox, QSpinBox, QVBoxLayout)

from .activity import EDIT_ACTIONS
from .i18n import is_rtl, tr
from .version import APP_NAME, VERSION

KEY = "biruni/timesheet/"
CURRENCIES = ["USD", "EUR", "EGP", "SAR", "AED", "KWD", "QAR", "OMR", "BHD", "JOD", "MAD", "GBP", "TRY"]


def daily_work(events, active, start=None, end=None, round_to=0):
    """[(date, minutes, billed minutes, description)] for each day with activity."""
    minutes = defaultdict(int)
    for a in active:
        minutes[a["day"]] += a["minutes"]
    edits = defaultdict(lambda: defaultdict(lambda: defaultdict(int)))   # day -> layer -> action -> n
    tools = defaultdict(lambda: defaultdict(int))
    other = defaultdict(set)
    for e in events:
        day = (e["ts"] or "")[:10]
        if not day:
            continue
        if e["category"] == "edit" and e["action"] in EDIT_ACTIONS:
            edits[day][e["target"] or "?"][e["action"]] += e["count"]
        elif e["category"] == "tool":
            tools[day][e["target"] or e["target_id"]] += 1
        elif e["category"] == "gps" and e["action"] == "track_recorded":
            other[day].add(tr("GPS track recorded"))
        elif e["category"] == "cogo":
            other[day].add(tr("COGO drafting"))
    rows = []
    words = {"features_added": tr("{n} added"), "geometries_changed": tr("{n} reshaped"),
             "attributes_changed": tr("{n} attribute edits"), "features_deleted": tr("{n} deleted")}
    for day in sorted(set(minutes) | set(edits) | set(tools)):
        d = dt.date.fromisoformat(day)
        if (start and d < start) or (end and d > end):
            continue
        m = minutes.get(day, 0)
        if not m and not edits.get(day) and not tools.get(day):
            continue
        parts = []
        for layer, acts in sorted(edits.get(day, {}).items(), key=lambda kv: -sum(kv[1].values())):
            bits = [words[a].format(n=acts[a]) for a in EDIT_ACTIONS if acts.get(a)]
            parts.append(f"{layer}: {', '.join(bits)}")
        if tools.get(day):
            names = sorted(tools[day].items(), key=lambda kv: -kv[1])
            parts.append(tr("Tools: {t}").format(t=", ".join(f"{n} ×{c}" if c > 1 else n for n, c in names[:8])))
        parts.extend(sorted(other.get(day, ())))
        billed = int(math.ceil(m / round_to) * round_to) if round_to and m else m
        rows.append((d, m, billed, "; ".join(parts) or tr("Map work")))
    return rows


def write_timesheet(path, project, rows, client="", worker="", rate=0.0, currency="USD", notes=""):
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Border, Font, PatternFill, Side

    wb = Workbook()
    ws = wb.active
    ws.title = tr("Timesheet")[:31]
    ws.sheet_view.rightToLeft = is_rtl()
    accent = "0E7C74"
    thin = Side(style="thin", color="C9D0D6")
    box = Border(top=thin, bottom=thin, left=thin, right=thin)
    ws.column_dimensions["A"].width = 14
    ws.column_dimensions["B"].width = 12
    ws.column_dimensions["C"].width = 70
    ws.column_dimensions["D"].width = 16
    ws["A1"] = tr("Timesheet")
    ws["A1"].font = Font(bold=True, size=18, color=accent)
    info = [(tr("Project"), project), (tr("Client"), client), (tr("Prepared by"), worker),
            (tr("Period"), f"{rows[0][0].isoformat()} → {rows[-1][0].isoformat()}" if rows else "-"),
            (tr("Hourly rate"), rate)]
    for k, (label, value) in enumerate(info):
        ws.cell(row=3 + k, column=1, value=label).font = Font(bold=True)
        ws.cell(row=3 + k, column=2, value=value)
    rate_cell = f"$B${3 + len(info) - 1}"
    ws.cell(row=3 + len(info) - 1, column=2).number_format = f'#,##0.00 "{currency}"'
    head = 3 + len(info) + 1
    for c, h in enumerate((tr("Date"), tr("Hours"), tr("Work done"), tr("Amount ({c})").format(c=currency)), 1):
        cell = ws.cell(row=head, column=c, value=h)
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = PatternFill("solid", fgColor=accent)
        cell.alignment = Alignment(horizontal="center")
    r = head
    for d, _m, billed, desc in rows:
        r += 1
        ws.cell(row=r, column=1, value=d).number_format = "yyyy-mm-dd"
        ws.cell(row=r, column=2, value=round(billed / 60, 2)).number_format = "0.00"
        ws.cell(row=r, column=3, value=desc).alignment = Alignment(wrap_text=True, vertical="top")
        ws.cell(row=r, column=4, value=f"=B{r}*{rate_cell}").number_format = "#,##0.00"
        for c in range(1, 5):
            ws.cell(row=r, column=c).border = box
    total = r + 1
    ws.cell(row=total, column=1, value=tr("Total")).font = Font(bold=True)
    ws.cell(row=total, column=2, value=f"=SUM(B{head + 1}:B{r})" if rows else 0).number_format = "0.00"
    ws.cell(row=total, column=4, value=f"=SUM(D{head + 1}:D{r})" if rows else 0).number_format = \
        f'#,##0.00 "{currency}"'
    for c in (1, 2, 3, 4):
        ws.cell(row=total, column=c).font = Font(bold=True)
        ws.cell(row=total, column=c).fill = PatternFill("solid", fgColor="DDF0EE")
        ws.cell(row=total, column=c).border = box
    note_row = total + 2
    ws.cell(row=note_row, column=1, value=tr("Hours are active working time recorded by {app}: minutes in which "
                                             "the map was worked on. Idle time is not counted.").format(app=APP_NAME)
            ).font = Font(italic=True, color="7B8791")
    if notes:
        ws.cell(row=note_row + 1, column=1, value=notes)
    ws.cell(row=note_row + 3, column=1, value=f"{APP_NAME} {VERSION} · {dt.datetime.now():%Y-%m-%d %H:%M}"
            ).font = Font(italic=True, size=8, color="A3ACB4")
    ws.print_options.gridLines = False
    ws.page_setup.fitToWidth = 1
    ws.sheet_properties.pageSetUpPr.fitToPage = True
    wb.save(path)
    return path


class TimesheetDialog(QDialog):
    def __init__(self, plugin):
        super().__init__(plugin.iface.mainWindow())
        self.plugin = plugin
        self.setWindowTitle(tr("Timesheet"))
        self.setMinimumWidth(520)
        s = QgsSettings()
        self.events, self.sessions, self.active = plugin.activity.snapshot()
        days = sorted({(e["ts"] or "")[:10] for e in self.events if e["ts"]} | {a["day"] for a in self.active})
        v = QVBoxLayout(self)
        v.setContentsMargins(18, 14, 18, 12)
        intro = QLabel(tr("Turn the work recorded in this project into a timesheet: active hours per day, what was "
                          "done each day, and the amount at your rate - ready to send with an invoice."))
        intro.setWordWrap(True)
        v.addWidget(intro)
        form = QFormLayout()
        self.client = QLineEdit(QgsProject.instance().readEntry("Biruni", "/timesheet-client", "")[0])
        self.worker = QLineEdit(s.value(KEY + "worker", os.environ.get("USERNAME", "")))
        self.rate = QDoubleSpinBox()
        self.rate.setRange(0, 1e6)
        self.rate.setDecimals(2)
        self.rate.setValue(float(s.value(KEY + "rate", 0)))
        self.currency = QComboBox()
        self.currency.setEditable(True)
        self.currency.addItems(CURRENCIES)
        self.currency.setCurrentText(s.value(KEY + "currency", "USD"))
        self.start = QDateEdit()
        self.end = QDateEdit()
        for w, d in ((self.start, days[0] if days else None), (self.end, days[-1] if days else None)):
            w.setCalendarPopup(True)
            w.setDisplayFormat("yyyy-MM-dd")
            w.setDate(QDate.fromString(d, "yyyy-MM-dd") if d else QDate.currentDate())
        self.round = QSpinBox()
        self.round.setRange(0, 60)
        self.round.setSingleStep(5)
        self.round.setSuffix(" " + tr("min"))
        self.round.setValue(int(s.value(KEY + "round", 15)))
        self.round.setSpecialValueText(tr("No rounding"))
        self.notes = QLineEdit()
        form.addRow(tr("Client"), self.client)
        form.addRow(tr("Prepared by"), self.worker)
        form.addRow(tr("Hourly rate"), self.rate)
        form.addRow(tr("Currency"), self.currency)
        form.addRow(tr("From"), self.start)
        form.addRow(tr("To"), self.end)
        form.addRow(tr("Round each day up to"), self.round)
        form.addRow(tr("Notes"), self.notes)
        v.addLayout(form)
        self.preview = QLabel()
        self.preview.setWordWrap(True)
        v.addWidget(self.preview)
        for w in (self.rate, self.round):
            w.valueChanged.connect(self._update)
        for w in (self.start, self.end):
            w.dateChanged.connect(self._update)
        self.currency.currentTextChanged.connect(self._update)
        bb = QDialogButtonBox()
        self.ok = bb.addButton(tr("Create timesheet"), QDialogButtonBox.ButtonRole.AcceptRole)
        bb.addButton(QDialogButtonBox.StandardButton.Cancel)
        bb.accepted.connect(self._create)
        bb.rejected.connect(self.reject)
        v.addWidget(bb)
        self._update()

    def _rows(self):
        return daily_work(self.events, self.active, self.start.date().toPyDate(), self.end.date().toPyDate(),
                          self.round.value())

    def _update(self):
        rows = self._rows()
        hours = sum(r[2] for r in rows) / 60
        self.preview.setText("<b>" + tr("{d} days · {h} hours · {a} {c}").format(
            d=len(rows), h=f"{hours:.2f}", a=f"{hours * self.rate.value():,.2f}", c=self.currency.currentText())
            + "</b>")
        self.ok.setEnabled(bool(rows))

    def _create(self):
        from .project import project_folder, safe_name

        rows = self._rows()
        s = QgsSettings()
        s.setValue(KEY + "worker", self.worker.text())
        s.setValue(KEY + "rate", self.rate.value())
        s.setValue(KEY + "currency", self.currency.currentText())
        s.setValue(KEY + "round", self.round.value())
        prj = QgsProject.instance()
        prj.writeEntry("Biruni", "/timesheet-client", self.client.text())
        name = prj.title() or os.path.splitext(os.path.basename(prj.fileName()))[0] or tr("Temporary map")
        folder = project_folder()
        start = os.path.join(folder, "exports") if folder else os.path.expanduser("~")
        default = os.path.join(start, f"{safe_name(name) or 'project'} - {tr('Timesheet')} "
                                      f"{self.end.date().toString('yyyy-MM-dd')}.xlsx")
        path, _ = QFileDialog.getSaveFileName(self, tr("Timesheet"), default, "Excel (*.xlsx)")
        if not path:
            return
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            write_timesheet(path, name, rows, self.client.text(), self.worker.text(), self.rate.value(),
                            self.currency.currentText(), self.notes.text())
        except PermissionError:
            QApplication.restoreOverrideCursor()
            QMessageBox.warning(self, APP_NAME, tr("The file is open in another program. Close it and try again."))
            return
        QApplication.restoreOverrideCursor()
        QDesktopServices.openUrl(QUrl.fromLocalFile(path))
        self.accept()


def open_timesheet(plugin):
    events, _s, _a = plugin.activity.snapshot()
    if not events:
        plugin.iface.messageBar().pushInfo(APP_NAME, tr("No activity has been recorded for this project yet."))
        return
    TimesheetDialog(plugin).exec()
