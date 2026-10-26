"""Learn panel: short guided lessons on the bundled sample data, plus the
ArcGIS ↔ Biruni dictionary. Each step says what to do, can do it for you
("Show me"), and ticks itself when it is done."""
from qgis.core import QgsProject, QgsSettings, QgsVectorLayer
from qgis.PyQt.QtCore import Qt, QTimer
from qgis.PyQt.QtWidgets import (QComboBox, QDockWidget, QHBoxLayout, QLabel, QLineEdit, QListWidget,
                                 QListWidgetItem, QPushButton, QScrollArea, QStackedWidget, QTabWidget, QVBoxLayout,
                                 QWidget)

from ..i18n import tr
from ..widgets import card, hint, panel_qss, title
from .dictionary import TERMS

DONE_KEY = "biruni/learn/done"


def _layer(name):
    found = QgsProject.instance().mapLayersByName(tr(name))
    return found[0] if found else None


def _countries():
    return _layer("Countries")


def _has_sample(_p):
    return _countries() is not None


def _identify_active(p):
    tool = p.iface.mapCanvas().mapTool()
    return tool is not None and "Identify" in type(tool).__name__


def _big_selected(p):
    c = _countries()
    return c is not None and c.selectedFeatureCount() > 0


def _categorized(_p):
    c = _countries()
    return c is not None and c.renderer() is not None and c.renderer().type() == "categorizedSymbol"


def _points_from_excel(_p):
    return any(isinstance(lyr, QgsVectorLayer) and "sample_points" in lyr.name().lower()
               for lyr in QgsProject.instance().mapLayers().values())


def _has_layout(_p):
    return bool(QgsProject.instance().layoutManager().layouts())


def _project_folder(_p):
    from ..project import project_folder

    return project_folder() is not None


def _has_history(_p):
    import os

    from ..history import list_versions
    from ..project import project_folder

    f = project_folder()
    return bool(f and os.path.isdir(f) and list_versions(f))


def _do_sample(p):
    from ..samples import load_world

    load_world(p)


def _do_select_big(p):
    c = _countries()
    if c is None:
        _do_sample(p)
        c = _countries()
    c.selectByExpression('"pop_est" > 100000000')
    p.iface.setActiveLayer(c)
    p.iface.mapCanvas().refresh()


def _do_categorize(p):
    c = _countries()
    if c is None:
        return
    from qgis.core import QgsCategorizedSymbolRenderer, QgsRendererCategory, QgsStyle, QgsSymbol

    values = sorted(c.uniqueValues(c.fields().indexFromName("continent")), key=str)
    ramp = QgsStyle.defaultStyle().colorRamp("Set3")
    cats = []
    for k, v in enumerate(values):
        sym = QgsSymbol.defaultSymbol(c.geometryType())
        if ramp:
            sym.setColor(ramp.color(k / max(1, len(values) - 1)))
        cats.append(QgsRendererCategory(v, sym, str(v)))
    c.setRenderer(QgsCategorizedSymbolRenderer("continent", cats))
    c.triggerRepaint()
    p.iface.layerTreeView().refreshLayerSymbology(c.id())
    p.iface.setActiveLayer(c)


def _do_excel(p):
    from ..samples import SAMPLE_POINTS
    from ..smart_table_dialog import open_dialog

    open_dialog(p, SAMPLE_POINTS)


def _cmd(cid):
    def run(p):
        from ..commands import CATALOG

        if cid in ("attribute_table", "statistics"):
            c = _countries()
            if c is not None:
                p.iface.setActiveLayer(c)
        p.runner.execute(CATALOG[cid])
    return run


# lesson: (title, intro, [(step text, check, action label, action)])
LESSONS = [
    ("Your first map", "Open the sample world, move around and look at features.", [
        ("Load the sample data: countries, cities, rivers and lakes of the world.", _has_sample, "Load sample",
         _do_sample),
        ("Zoom with the mouse wheel and drag the map to move. Home ▸ Full Extent shows everything again.",
         None, None, None),
        ("Choose Identify (Home ▸ Inquiry) and click a country to see its information.", _identify_active,
         "Show me", _cmd("identify")),
    ]),
    ("Ask the data a question", "Find features by their values and see the numbers.", [
        ("Open the attribute table of Countries: every country is a row.", None, "Show me",
         _cmd("attribute_table")),
        ("Select the countries with more than 100 million people. The expression is \"pop_est\" > 100000000.",
         _big_selected, "Do it for me", _do_select_big),
        ("Open Statistics (Home ▸ Inquiry) to see the total, mean and maximum of a field.", None, "Show me",
         _cmd("statistics")),
    ]),
    ("Style a map", "Colours and labels that explain the data.", [
        ("Colour the countries by continent (categorized style).", _categorized, "Do it for me", _do_categorize),
        ("Open the Styling panel (View ▸ Panels) to change colours, sizes and labels yourself.", None, "Show me",
         _cmd("panel_styling")),
    ]),
    ("Points from Excel", "Turn a spreadsheet with coordinates into points.", [
        ("Open the sample spreadsheet: its coordinates are written in degrees-minutes-seconds with Arabic "
         "headers. Biruni GIS finds the columns by itself.", _points_from_excel, "Show me", _do_excel),
    ]),
    ("A finished map", "Print-ready map in one step.", [
        ("Share ▸ Map Sheet: title, coordinate frame, north arrow, scale bar and legend in one click.",
         _has_layout, "Show me", _cmd("map_sheet")),
    ]),
    ("Keep your work safe", "Projects are folders, and every save is a version.", [
        ("Save the map as a project folder (File ▸ Save).", _project_folder, "Show me", _cmd("save")),
        ("Save again after a change, then open Project History to see the versions.", _has_history, "Show me",
         _cmd("history_versions")),
    ]),
    ("Data from the internet", "Free data for any place in a minute.", [
        ("Zoom to a small area (a district), then Data ▸ Open Data ▸ OpenStreetMap to download its roads or "
         "buildings.", None, "Show me", _cmd("osm_download")),
        ("Get the elevation of the area: Data ▸ Open Data ▸ Elevation.", None, "Show me", _cmd("dem_download")),
    ]),
]
# lesson and step texts, listed for the translation extractor
TITLES = [x for lesson in LESSONS for x in (lesson[0], lesson[1])] + \
    [s[0] for lesson in LESSONS for s in lesson[2]] + \
    [s[2] for lesson in LESSONS for s in lesson[2] if s[2]]


class LearnDock(QDockWidget):
    def __init__(self, plugin):
        super().__init__(tr("Learn"), plugin.iface.mainWindow())
        self.plugin = plugin
        self.setObjectName("BiruniLearn")
        self.done = set(QgsSettings().value(DONE_KEY, [], type=list) or [])
        self.step_widgets = []
        self._build()
        self.timer = QTimer(self)
        self.timer.setInterval(1000)
        self.timer.timeout.connect(self._check)
        self.visibilityChanged.connect(lambda v: self.timer.start() if v else self.timer.stop())

    def _build(self):
        body = QWidget()
        body.setObjectName("BiruniLearnBody")
        v = QVBoxLayout(body)
        v.setContentsMargins(8, 6, 8, 8)
        tabs = QTabWidget()
        v.addWidget(tabs)
        # lessons
        page = QWidget()
        pv = QVBoxLayout(page)
        pv.setContentsMargins(0, 6, 0, 0)
        self.lesson_box = QComboBox()
        for k, (name, _i, _s) in enumerate(LESSONS):
            self.lesson_box.addItem(f"{k + 1}. {tr(name)}")
        self.lesson_box.currentIndexChanged.connect(self._show_lesson)
        pv.addWidget(self.lesson_box)
        self.stack = QStackedWidget()
        for k in range(len(LESSONS)):
            self.stack.addWidget(self._lesson_page(k))
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(self.stack)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        pv.addWidget(scroll, 1)
        tabs.addTab(page, tr("Lessons"))
        # dictionary
        dpage = QWidget()
        dv = QVBoxLayout(dpage)
        dv.setContentsMargins(0, 6, 0, 0)
        dv.addWidget(hint(tr("Coming from ArcGIS Pro? Find the tool you know - or type its name in the search "
                             "box at the top of the window.")))
        self.filter = QLineEdit()
        self.filter.setPlaceholderText(tr("ArcGIS tool name…"))
        self.filter.textChanged.connect(self._fill_terms)
        dv.addWidget(self.filter)
        self.terms = QListWidget()
        self.terms.itemActivated.connect(self._run_term)
        self.terms.itemDoubleClicked.connect(self._run_term)
        dv.addWidget(self.terms, 1)
        tabs.addTab(dpage, tr("ArcGIS → Biruni"))
        self.setWidget(body)
        self.setMinimumWidth(300)
        self.refresh_theme()
        self._fill_terms()

    def refresh_theme(self):
        self.widget().setStyleSheet(panel_qss(self.plugin.theme.tokens(), "BiruniLearnBody"))

    def _lesson_page(self, k):
        name, intro, steps = LESSONS[k]
        w = QWidget()
        v = QVBoxLayout(w)
        v.setContentsMargins(0, 4, 0, 4)
        v.addWidget(title(tr(name)))
        v.addWidget(hint(tr(intro)))
        widgets = []
        for j, (text, check, label, action) in enumerate(steps):
            box, lay = card(v)
            row = QHBoxLayout()
            mark = QLabel("○")
            mark.setFixedWidth(18)
            mark.setAlignment(Qt.AlignmentFlag.AlignTop)
            lab = QLabel(tr(text))
            lab.setWordWrap(True)
            row.addWidget(mark)
            row.addWidget(lab, 1)
            lay.addLayout(row)
            if action is not None:
                b = QPushButton(tr(label))
                b.clicked.connect(lambda _=False, a=action, key=f"{k}.{j}", c=check: self._act(a, key, c))
                h = QHBoxLayout()
                h.addStretch(1)
                h.addWidget(b)
                lay.addLayout(h)
            widgets.append((f"{k}.{j}", check, mark))
        v.addStretch(1)
        self.step_widgets.append(widgets)
        return w

    def _show_lesson(self, k):
        self.stack.setCurrentIndex(k)
        self._check()

    def _act(self, action, key, check):
        try:
            action(self.plugin)
        except Exception as e:  # noqa: BLE001
            self.plugin.iface.messageBar().pushWarning("Biruni GIS", str(e))
        if check is None:
            self._mark(key)
        self._check()

    def _mark(self, key):
        if key not in self.done:
            self.done.add(key)
            QgsSettings().setValue(DONE_KEY, sorted(self.done))

    def _check(self):
        k = self.stack.currentIndex()
        if k < 0 or k >= len(self.step_widgets):
            return
        t = self.plugin.theme.tokens()
        for key, check, mark in self.step_widgets[k]:
            ok = key in self.done
            if check is not None:
                try:
                    ok = bool(check(self.plugin))
                except Exception:  # noqa: BLE001
                    ok = False
                if ok:
                    self._mark(key)
            mark.setText("✔" if ok else "○")
            mark.setStyleSheet(f"color: {t['accent']}; font-weight: 700;" if ok else "")

    def _fill_terms(self, *_):
        q = self.filter.text().strip().lower()
        self.terms.clear()
        from qgis.core import QgsApplication

        from ..commands import CATALOG

        for name, target, note in TERMS:
            if q and q not in name.lower():
                continue
            if target in CATALOG:
                label = tr(CATALOG[target].label).replace("\n", " ")
            else:
                alg = QgsApplication.processingRegistry().algorithmById(target)
                if alg is None:
                    continue
                label = alg.displayName()
            it = QListWidgetItem(f"{name}  →  {label}")
            it.setData(Qt.ItemDataRole.UserRole, target)
            if note:
                it.setToolTip(tr(note))
            self.terms.addItem(it)

    def _run_term(self, item):
        target = item.data(Qt.ItemDataRole.UserRole)
        from ..commands import CATALOG

        if target in CATALOG:
            self.plugin.runner.execute(CATALOG[target])
        else:
            self.plugin.runner.open_algorithm(target)
