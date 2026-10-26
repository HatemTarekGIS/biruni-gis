"""Portfolio page: the project's maps and the work behind them in one HTML page
(images saved next to it) - to show a client, or in a job application."""
import datetime as dt
import html
import os
import re

from qgis.core import QgsLayoutExporter, QgsProject
from qgis.PyQt.QtCore import Qt, QUrl
from qgis.PyQt.QtGui import QDesktopServices
from qgis.PyQt.QtWidgets import (QApplication, QDialog, QDialogButtonBox, QFormLayout, QLabel, QLineEdit,
                                 QPlainTextEdit, QVBoxLayout)

from . import i18n
from .i18n import tr
from .project import project_folder, safe_name
from .version import APP_NAME

PAGE = """<!doctype html>
<html lang="{lang}" dir="{dir}"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<style>
:root {{ --bg:#F3F5F7; --card:#fff; --text:#1E252B; --dim:#5B6670; --accent:#0E7C74; }}
@media (prefers-color-scheme: dark) {{ :root {{ --bg:#16191D; --card:#20252B; --text:#E4E8EC; --dim:#A2ACB6;
  --accent:#2CC5B4; }} }}
body {{ margin:0; background:var(--bg); color:var(--text); font:16px/1.55 "Segoe UI", Tahoma, Arial, sans-serif; }}
main {{ max-width:1100px; margin:0 auto; padding:32px 16px 48px; }}
h1 {{ margin:0 0 4px; font-size:30px; }} .who {{ color:var(--dim); margin-bottom:18px; }}
.desc {{ white-space:pre-wrap; max-width:760px; }}
.stats {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(150px,1fr)); gap:12px; margin:22px 0; }}
.stat {{ background:var(--card); border-radius:10px; padding:14px; }}
.stat b {{ display:block; font-size:24px; color:var(--accent); }} .stat span {{ color:var(--dim); font-size:13px; }}
figure {{ margin:0 0 22px; background:var(--card); border-radius:10px; overflow:hidden; }}
figure img {{ width:100%; display:block; }} figcaption {{ padding:10px 14px; font-weight:600; }}
footer {{ color:var(--dim); font-size:12px; margin-top:24px; }}
</style></head><body><main>
<h1>{title}</h1><div class="who">{who}</div>
<div class="desc">{desc}</div>
<div class="stats">{stats}</div>
{figures}
<footer>{footer}</footer>
</main></body></html>
"""


def export_portfolio(project, out_dir, title, author="", description="", stats=None, dpi=110):
    os.makedirs(out_dir, exist_ok=True)
    figures = []
    for k, layout in enumerate(project.layoutManager().printLayouts()):
        stem = re.sub(r"[^\w]+", "_", layout.name(), flags=re.UNICODE).strip("_")[:40]
        name = f"map_{k + 1}_{stem}.png"
        s = QgsLayoutExporter.ImageExportSettings()
        s.dpi = dpi
        if QgsLayoutExporter(layout).exportToImage(os.path.join(out_dir, name), s) == \
                QgsLayoutExporter.ExportResult.Success:
            figures.append(f"<figure><img src='{html.escape(name)}' alt='{html.escape(layout.name())}' "
                           f"loading='lazy'><figcaption>{html.escape(layout.name())}</figcaption></figure>")
    stat_html = "".join(f"<div class='stat'><b>{html.escape(str(v))}</b><span>{html.escape(k)}</span></div>"
                        for k, v in (stats or {}).items())
    page = PAGE.format(lang=i18n.lang(), dir="rtl" if i18n.is_rtl() else "ltr", title=html.escape(title),
                       who=html.escape(author), desc=html.escape(description), stats=stat_html,
                       figures="\n".join(figures) or f"<p>{html.escape(tr('This project has no print layouts yet.'))}</p>",
                       footer=html.escape(tr("Made with {app} · {d}").format(app=APP_NAME,
                                                                              d=dt.date.today().isoformat())))
    path = os.path.join(out_dir, "index.html")
    with open(path, "w", encoding="utf-8") as f:
        f.write(page)
    return path, len(figures)


def project_stats(plugin):
    from .activity_report import build_stats

    events, sessions, active = plugin.activity.snapshot()
    s = build_stats(events, sessions, active)
    prj = QgsProject.instance()
    out = {tr("Layers"): len(prj.mapLayers()), tr("Maps"): len(prj.layoutManager().printLayouts())}
    if s["active_minutes"]:
        out[tr("Hours of work")] = round(s["active_minutes"] / 60, 1)
    edits = sum(s["totals"].values())
    if edits:
        out[tr("Features drawn and edited")] = f"{edits:,}"
    if s["tools_run"]:
        out[tr("Analyses run")] = s["tools_run"]
    return out


class PortfolioDialog(QDialog):
    def __init__(self, plugin):
        super().__init__(plugin.iface.mainWindow())
        self.plugin = plugin
        self.setWindowTitle(tr("Portfolio Page"))
        self.setMinimumWidth(520)
        v = QVBoxLayout(self)
        v.setContentsMargins(18, 14, 18, 12)
        intro = QLabel(tr("One web page with the maps of this project (its print layouts) and the work behind "
                          "them - to show a client or to add to a job application."))
        intro.setWordWrap(True)
        v.addWidget(intro)
        form = QFormLayout()
        prj = QgsProject.instance()
        self.title = QLineEdit(prj.title() or os.path.splitext(os.path.basename(prj.fileName()))[0])
        self.author = QLineEdit(os.environ.get("USERNAME", ""))
        self.desc = QPlainTextEdit()
        self.desc.setPlaceholderText(tr("What was the project about, what did you do, which tools…"))
        self.desc.setFixedHeight(110)
        form.addRow(tr("Title"), self.title)
        form.addRow(tr("Your name"), self.author)
        form.addRow(tr("Description"), self.desc)
        v.addLayout(form)
        bb = QDialogButtonBox()
        bb.addButton(tr("Create page"), QDialogButtonBox.ButtonRole.AcceptRole)
        bb.addButton(QDialogButtonBox.StandardButton.Cancel)
        bb.accepted.connect(self._create)
        bb.rejected.connect(self.reject)
        v.addWidget(bb)

    def _create(self):
        folder = project_folder()
        base = os.path.join(folder, "exports") if folder else os.path.expanduser("~")
        out = os.path.join(base, f"{safe_name(self.title.text()) or 'portfolio'} - {tr('Portfolio')}")
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            path, n = export_portfolio(QgsProject.instance(), out, self.title.text().strip(),
                                       self.author.text().strip(), self.desc.toPlainText().strip(),
                                       project_stats(self.plugin))
        finally:
            QApplication.restoreOverrideCursor()
        QDesktopServices.openUrl(QUrl.fromLocalFile(path))
        self.plugin.iface.messageBar().pushSuccess(APP_NAME, tr("Portfolio page with {n} maps: {p}").format(
            n=n, p=path))
        self.accept()
