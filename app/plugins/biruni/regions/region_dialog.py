"""The "My country" window: choose (or confirm) the country you work in and see what Biruni set up for it -
deed language, units, calendars, coordinate systems, map sheets, file formats, official cadastre services - with
its tools one double-click away."""
import html

from qgis.core import QgsApplication
from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtWidgets import (QComboBox, QDialog, QDialogButtonBox, QFormLayout, QLabel, QListWidget,
                                 QListWidgetItem, QVBoxLayout)

from .. import regions
from ..i18n import lang, tr
from . import detect

ROLE = Qt.ItemDataRole.UserRole


def _notation(key):
    return {"azimuth": tr("Azimuths from north (0-360°)"), "azimuth_decimal": tr("Azimuths in decimal degrees"),
            "wcb": tr("Whole-circle bearings"), "quadrant": tr("Quadrant bearings (N 45°30' E)"),
            "quadrant_ph": tr("Quadrant bearings, Philippine style"), "gon": tr("Grads (gon)")}.get(key, key)


def _source(key):
    return {"saved": tr("chosen by you"), "project": tr("from where your data is"),
            "locale": tr("from the Windows region")}.get(key, key)


class RegionDialog(QDialog):
    def __init__(self, plugin):
        super().__init__(plugin.mw if plugin is not None else None)
        self.plugin = plugin
        self.setWindowTitle(tr("My country"))
        self.resize(640, 620)
        v = QVBoxLayout(self)
        v.addWidget(QLabel(tr("Biruni sets deed wording, units, calendars, coordinate systems and official data "
                              "services for the country you work in.")))
        form = QFormLayout()
        self.combo = QComboBox()
        cc, source = detect.detect()
        auto = regions.region(cc).name if cc and source != "saved" else ""
        self.combo.addItem(tr("Automatic ({c}, {s})").format(c=auto, s=_source(source))
                           if auto else tr("Automatic"), "")
        for code, name in regions.countries():
            self.combo.addItem(name, code)
        saved = detect.saved()
        i = self.combo.findData(saved) if saved else 0
        self.combo.setCurrentIndex(max(i, 0))
        self.combo.currentIndexChanged.connect(self.refresh)
        form.addRow(tr("Country"), self.combo)
        v.addLayout(form)
        self.info = QLabel()
        self.info.setWordWrap(True)
        self.info.setTextFormat(Qt.TextFormat.RichText)
        self.info.setOpenExternalLinks(True)
        self.info.setAlignment(Qt.AlignmentFlag.AlignTop)
        v.addWidget(self.info, 1)
        v.addWidget(QLabel(tr("Tools for this country (double-click to open):")))
        self.tools = QListWidget()
        self.tools.itemDoubleClicked.connect(self._open_tool)
        v.addWidget(self.tools, 1)
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        v.addWidget(bb)
        self.refresh()

    def refresh(self, *_):
        cc = self.combo.currentData() or (detect.from_project() or detect.from_locale())
        self.tools.clear()
        if not cc:
            self.info.setText(html.escape(tr("Pick your country to see what Biruni offers there.")))
            return
        r = regions.region(cc)
        self.info.setText(self._summary(r))
        reg = QgsApplication.processingRegistry()
        for tid in regions.toolbox_entries(cc):
            alg = reg.algorithmById(tid)
            it = QListWidgetItem(alg.icon(), alg.displayName())
            it.setData(ROLE, tid)
            it.setToolTip(alg.shortDescription() or "")
            self.tools.addItem(it)

    def _summary(self, r):
        rows = []

        def row(title, values):
            values = [v for v in values if v]
            if values:
                rows.append(f"<b>{html.escape(title)}</b>: {html.escape(' · '.join(values))}")

        head = html.escape(r.name + (f" ({r.native})" if r.native and r.native != r.name else ""))
        agency = html.escape(r.agency)
        if r.link:
            agency = f'<a href="{html.escape(r.link)}">{agency or html.escape(r.link)}</a>'
        units = regions._engine("units")
        ui = lang()
        row(tr("Deed wording"), [_deed_label(r.deed_language), _notation(r.bearing_notation)])
        row(tr("Area units"), [units.get(u).name(ui) if units else u for u in r.area_units])
        cals = regions._engine("calendars")
        row(tr("Calendars"), [tr("Gregorian")] + [tr(cals.NAMES.get(c, c)) if cals else c for c in r.calendars])
        row(tr("Text encodings of old files"), [e.upper() for e in r.encodings])
        crs = [e.get("label", "") for e in r.crs[:6]] or r.crs_hints[:6]
        row(tr("Coordinate systems"), crs)
        presets = regions._engine("sheet_presets")
        row(tr("Map sheets"), [tr(presets.get(p).name) if presets and presets.get(p) else p
                               for p in r.sheet_presets[:4]])
        row(tr("File formats"), [str(f) for f in r.formats])
        cad = [f"{c.name()} - {tr('connected') if c.available else tr('not available')}" for c in r.cadastre]
        row(tr("Official cadastre"), cad)
        body = "<br>".join(rows) or html.escape(tr("Biruni has no country-specific settings here yet."))
        return f"<h3>{head}</h3>{('<p>' + agency + '</p>') if agency else ''}<p>{body}</p>"

    def _open_tool(self, item):
        if self.plugin is not None:
            self.plugin.runner.open_algorithm(item.data(ROLE))

    def accept(self):
        regions.set_country(self.combo.currentData())
        super().accept()


def _deed_label(code):
    try:
        from .. import deed_languages

        return deed_languages.LANGUAGES.get(code, code)
    except Exception:  # noqa: BLE001
        return code
