"""The windows of the peer review lottery: draw (commit, then the salt), review (one feature at a time, blind) and
results (the error rate of each lot and of the whole delivery, with its confidence)."""
import html
import os

from qgis.core import (Qgis, QgsCategorizedSymbolRenderer, QgsCoordinateTransform, QgsFeatureRequest,
                       QgsMarkerSymbol, QgsProject, QgsRendererCategory, QgsVectorLayer)
from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtGui import QGuiApplication
from qgis.PyQt.QtWidgets import (QAbstractItemView, QButtonGroup, QComboBox, QDialog, QDialogButtonBox,
                                 QDockWidget, QDoubleSpinBox, QFileDialog, QFormLayout, QHBoxLayout, QLabel,
                                 QLineEdit, QMessageBox, QPlainTextEdit, QPushButton, QRadioButton, QTableWidget,
                                 QTableWidgetItem, QVBoxLayout, QWidget, QCheckBox)

from ..i18n import tr
from . import sampling, store


# ---- finding things ---------------------------------------------------------------------------------------------
def _data_layers():
    return [l for l in QgsProject.instance().mapLayers().values()
            if isinstance(l, QgsVectorLayer) and l.isValid() and l.isSpatial()
            and store.REVIEWS not in l.source() and store.DRAWS not in l.source()]


def _loaded(table):
    for l in QgsProject.instance().mapLayers().values():
        if isinstance(l, QgsVectorLayer) and l.isValid() and f"layername={table}" in l.source():
            return l
    return None


def _gpkg():
    """The map's GeoPackage (the project's, else the one a team room puts the map's data in), or None for a map
    never saved."""
    from ..project import project_gpkg

    gpkg = project_gpkg()
    if gpkg:
        return gpkg
    path = QgsProject.instance().fileName()
    if not path:
        return None
    return os.path.join(os.path.dirname(path), os.path.splitext(os.path.basename(path))[0] + "_team.gpkg")


def tables(create_crs=None):
    """(reviews, draws) layers of this map: the loaded ones, else those in the project's GeoPackage (made when
    create_crs is given). (None, None) when there are none."""
    rev, drw = _loaded(store.REVIEWS), _loaded(store.DRAWS)
    if rev is not None and drw is not None:
        return rev, drw
    gpkg = None
    if rev is not None:
        gpkg = rev.source().split("|")[0]
    gpkg = gpkg or _gpkg()
    if not gpkg:
        return None, None
    if create_crs is None and not os.path.isfile(gpkg):
        return None, None
    if create_crs is None:
        r = QgsVectorLayer(f"{gpkg}|layername={store.REVIEWS}", "Peer reviews", "ogr")
        d = QgsVectorLayer(f"{gpkg}|layername={store.DRAWS}", "Review draws", "ogr")
        if not (r.isValid() and d.isValid()):
            return None, None
    else:
        r, d = store.ensure_tables(gpkg, create_crs)
    r.setName(tr("Peer reviews"))
    d.setName(tr("Review draws"))
    if rev is None:
        style(r)
        QgsProject.instance().addMapLayer(r)
    else:
        r = rev
    if drw is None:
        QgsProject.instance().addMapLayer(d)
    else:
        d = drw
    return r, d


def style(layer):
    cats = []
    for value, colour, label in (("", "#9aa0a6", tr("Not reviewed yet")), ("ok", "#2e9d57", tr("Correct")),
                                 ("error", "#d93025", tr("Error"))):
        sym = QgsMarkerSymbol.createSimple({"name": "circle", "color": colour, "size": "3.2",
                                            "outline_color": "#ffffff", "outline_width": "0.4"})
        cats.append(QgsRendererCategory(value, sym, label))
    layer.setRenderer(QgsCategorizedSymbolRenderer("coalesce(\"verdict\", '')", cats))
    layer.triggerRepaint()


def source_layer(row):
    """The layer a review row is about, on this computer (by its table, then by its name)."""
    by_name = None
    for l in _data_layers():
        if row.get("source") and f"layername={row['source']}" in l.source():
            return l
        if l.name() == row.get("layer"):
            by_name = by_name or l
    return by_name


def _room_reviewers():
    """[(member id, name)] of the room open on this map, or []."""
    from .. import team

    node = team.current()
    if node is None:
        return []
    out = {}
    try:
        for m in node.store.members():
            if m.get("state") != "out":
                out[m["id"]] = m.get("name") or m["id"][:6]
    except Exception:  # noqa: BLE001 - a member computer has the roster instead
        pass
    for m in getattr(node, "roster", None) or []:
        if isinstance(m, dict) and m.get("id") and m.get("state") != "out":
            out.setdefault(m["id"], m.get("name") or m["id"][:6])
    out.setdefault(node.me_id, node.ident.get("name", ""))
    return sorted(out.items(), key=lambda t: t[1].lower())


def _pct(v):
    return f"{100 * v:.1f}%"


DECISION = {"accept": "Accepted", "reject": "Rejected", "pending": "Not finished"}


# ---- the draw -------------------------------------------------------------------------------------------------------
class DrawDialog(QDialog):
    def __init__(self, plugin):
        super().__init__(plugin.iface.mainWindow())
        self.plugin = plugin
        self.pending = None
        self.setWindowTitle(tr("Peer Review Lottery"))
        self.setMinimumWidth(560)
        v = QVBoxLayout(self)
        intro = QLabel(tr("A random sample of each person's features is reviewed by someone else, without the "
                          "reviewer knowing whose work it is. From what the sample finds, the error rate of each "
                          "person's work and of the whole delivery is known with a stated confidence. The draw is "
                          "fair and anyone can check it: first the data is sealed (the commitment), then the team "
                          "gives the salt, and only then is the sample drawn."))
        intro.setWordWrap(True)
        v.addWidget(intro)
        form = QFormLayout()
        self.layer = QComboBox()
        for lyr in _data_layers():
            self.layer.addItem(lyr.name(), lyr.id())
        act = plugin.iface.activeLayer()
        if act is not None and self.layer.findData(act.id()) >= 0:
            self.layer.setCurrentIndex(self.layer.findData(act.id()))
        form.addRow(tr("Layer to review"), self.layer)
        self.by_room = QRadioButton(tr("The team room's record (who last changed each feature)"))
        self.by_field = QRadioButton(tr("A field"))
        self.by_one = QRadioButton(tr("The whole layer is one person's work"))
        self.grp = QButtonGroup(self)
        for b in (self.by_room, self.by_field, self.by_one):
            self.grp.addButton(b)
        self.field = QComboBox()
        fr = QHBoxLayout()
        fr.addWidget(self.by_field)
        fr.addWidget(self.field, 1)
        box = QVBoxLayout()
        box.addWidget(self.by_room)
        box.addLayout(fr)
        box.addWidget(self.by_one)
        form.addRow(tr("Who made each feature"), box)
        self.only = QCheckBox(tr("Only the selected features"))
        form.addRow("", self.only)
        self.reviewers = QPlainTextEdit()
        self.reviewers.setPlaceholderText(tr("One name a line"))
        self.reviewers.setFixedHeight(84)
        form.addRow(tr("Reviewers"), self.reviewers)
        self.aql = self._rate(1.0)
        self.aql.setToolTip(tr("Work with this error rate or less is accepted almost always (95 times in 100)."))
        form.addRow(tr("Acceptable error rate"), self.aql)
        self.ltpd = self._rate(5.0)
        self.ltpd.setToolTip(tr("Work with this error rate or more is rejected almost always (90 times in 100)."))
        form.addRow(tr("Rejected error rate"), self.ltpd)
        v.addLayout(form)
        self.plan_btn = QPushButton(tr("1. Seal the data"))
        self.plan_btn.clicked.connect(self.seal)
        v.addWidget(self.plan_btn)
        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels([tr("Person"), tr("Features"), tr("To review"), tr("Accepted with "
                                                                                                 "at most")])
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.setFixedHeight(130)
        v.addWidget(self.table)
        cr = QHBoxLayout()
        self.commitment = QLineEdit()
        self.commitment.setReadOnly(True)
        self.commitment.setPlaceholderText(tr("The commitment appears here"))
        cr.addWidget(self.commitment, 1)
        cp = QPushButton(tr("Copy"))
        cp.clicked.connect(lambda: QGuiApplication.clipboard().setText(self.commitment.text()))
        cr.addWidget(cp)
        v.addLayout(cr)
        note = QLabel(tr("Send the commitment to the team (a message, the meeting's notes) before asking for the "
                         "salt. The salt is anything nobody could know before: a word from each member, the "
                         "result of a match."))
        note.setWordWrap(True)
        v.addWidget(note)
        sr = QHBoxLayout()
        self.salt = QLineEdit()
        self.salt.setPlaceholderText(tr("The salt given by the team"))
        sr.addWidget(self.salt, 1)
        self.draw_btn = QPushButton(tr("2. Draw"))
        self.draw_btn.setEnabled(False)
        self.draw_btn.clicked.connect(self.draw)
        sr.addWidget(self.draw_btn)
        v.addLayout(sr)
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        bb.rejected.connect(self.reject)
        v.addWidget(bb)
        self.layer.currentIndexChanged.connect(self._layer_changed)
        self._layer_changed()

    @staticmethod
    def _rate(value):
        s = QDoubleSpinBox()
        s.setRange(0.0, 50.0)
        s.setDecimals(1)
        s.setSingleStep(0.5)
        s.setSuffix(" %")
        s.setValue(value)
        return s

    def _lyr(self):
        return QgsProject.instance().mapLayer(self.layer.currentData() or "")

    def _layer_changed(self):
        lyr = self._lyr()
        self.field.clear()
        if lyr is not None:
            for f in lyr.fields():
                self.field.addItem(f.name())
            for guess in ("editor", "created_by", "last_edited_user", "user", "surveyor", "author"):
                i = self.field.findText(guess, Qt.MatchFlag.MatchFixedString)
                if i >= 0:
                    self.field.setCurrentIndex(i)
                    break
        in_room = lyr is not None and store.room_authors(lyr) is not None
        self.by_room.setEnabled(in_room)
        (self.by_room if in_room else (self.by_field if self.field.count() else self.by_one)).setChecked(True)
        room = _room_reviewers()
        if room and not self.reviewers.toPlainText().strip():
            self.reviewers.setPlainText("\n".join(n for _i, n in room))
        self.pending = None
        self.draw_btn.setEnabled(False)

    def _people(self, lyr):
        """(authors {fid: (id, name)}, reviewers [(id, name)]). In a room the ids are the members' ids (so a
        member is never given their own work); otherwise the names are the ids."""
        names = [s.strip() for s in self.reviewers.toPlainText().splitlines() if s.strip()]
        if self.by_room.isChecked():
            authors = store.room_authors(lyr) or {}
            ids = {n: i for i, n in _room_reviewers()}
            reviewers = [(ids.get(n, n), n) for n in dict.fromkeys(names)]
        elif self.by_field.isChecked():
            authors = store.field_authors(lyr, self.field.currentText())
            reviewers = [(n, n) for n in dict.fromkeys(names)]
        else:
            authors = {f.id(): ("layer", lyr.name()) for f in lyr.getFeatures()}
            reviewers = [(n, n) for n in dict.fromkeys(names)]
        return authors, reviewers

    def seal(self):
        lyr = self._lyr()
        if lyr is None:
            return
        if not _gpkg():
            QMessageBox.information(self, tr("Peer Review Lottery"), tr("Save the map first: the reviews are kept "
                                                                         "in the map's GeoPackage."))
            return
        authors, reviewers = self._people(lyr)
        if len(reviewers) < 2 and not self.by_one.isChecked():
            QMessageBox.information(self, tr("Peer Review Lottery"), tr("At least two reviewers are needed, so "
                                                                         "that nobody reviews their own work."))
            return
        if not reviewers:
            return
        only = lyr.selectedFeatureIds() if self.only.isChecked() else None
        try:
            self.pending = store.commit(lyr, authors, reviewers, self.aql.value() / 100, self.ltpd.value() / 100,
                                        only=only)
        except sampling.PlanError:
            QMessageBox.information(self, tr("Peer Review Lottery"), tr("The rejected rate must be above the "
                                                                         "acceptable rate."))
            return
        self.table.setRowCount(0)
        for code, fids in sorted(self.pending["lots"].items(), key=lambda t: self.pending["names"][t[0]]):
            p = self.pending["plans"][code]
            r = self.table.rowCount()
            self.table.insertRow(r)
            name = self.pending["names"][code] or tr("(before the room)")
            for c, val in enumerate((name, len(fids), tr("all") if p["all"] else p["n"],
                                     tr("{c} errors").format(c=p["c"]))):
                self.table.setItem(r, c, QTableWidgetItem(str(val)))
        c = self.pending["commitment"]
        self.commitment.setText(" ".join(c[i:i + 4] for i in range(0, 24, 4)) + "  " + c)
        self.draw_btn.setEnabled(True)

    def draw(self):
        if self.pending is None:
            return
        if not self.salt.text().strip():
            QMessageBox.information(self, tr("Peer Review Lottery"), tr("Type the salt the team gave."))
            return
        lyr = self._lyr()
        d = store.reveal(self.pending, self.salt.text())
        rev, drw = tables(lyr.crs())
        store.write_draw(rev, drw, d, lyr)
        style(rev)
        alone = sum(1 for it in d["sample"] if not it[3])
        msg = tr("{n} features were drawn and given to the reviewers. Each reviewer opens «Review my share».").format(
            n=len(d["sample"]))
        if alone:
            msg += "\n" + tr("{n} could not be given: their author is the only reviewer.").format(n=alone)
        from .. import team

        node = team.current()
        if node is not None and node.key_of(rev) is None:
            msg += "\n\n" + tr("The reviews table is not shared in the team room: to review on the other "
                               "computers, open the room with the «Peer reviews» and «Review draws» tables, or send "
                               "them the map's GeoPackage.")
        QMessageBox.information(self, tr("Peer Review Lottery"), msg)
        self.accept()


def draw_ui(plugin):
    if not _data_layers():
        plugin.iface.messageBar().pushMessage(tr("Peer Review Lottery"), tr("Add the layer to review first."),
                                              Qgis.MessageLevel.Info, 6)
        return None
    d = DrawDialog(plugin)
    d.show()
    return d


# ---- reviewing --------------------------------------------------------------------------------------------------
class ReviewDock(QDockWidget):
    def __init__(self, plugin):
        super().__init__(tr("Review my share"), plugin.iface.mainWindow())
        self.setObjectName("BiruniPeerReview")
        self.plugin = plugin
        self.rows, self.i = [], 0
        w = QWidget()
        v = QVBoxLayout(w)
        form = QFormLayout()
        self.draw = QComboBox()
        form.addRow(tr("Draw"), self.draw)
        self.me = QComboBox()
        form.addRow(tr("I am"), self.me)
        v.addLayout(form)
        self.where = QLabel()
        self.where.setWordWrap(True)
        v.addWidget(self.where)
        nav = QHBoxLayout()
        self.prev_b = QPushButton(tr("Previous"))
        self.zoom_b = QPushButton(tr("Zoom"))
        self.next_b = QPushButton(tr("Next"))
        for b in (self.prev_b, self.zoom_b, self.next_b):
            nav.addWidget(b)
        v.addLayout(nav)
        self.ok_b = QPushButton(tr("Correct"))
        self.err_b = QPushButton(tr("Error"))
        vr = QHBoxLayout()
        vr.addWidget(self.ok_b)
        vr.addWidget(self.err_b)
        v.addLayout(vr)
        f2 = QFormLayout()
        self.cat = QComboBox()
        for c in store.CATEGORIES:
            self.cat.addItem(tr(c), c)
        f2.addRow(tr("Kind of error"), self.cat)
        self.note = QLineEdit()
        f2.addRow(tr("Note"), self.note)
        v.addLayout(f2)
        blind = QLabel(tr("You do not see whose work this is: judge the feature, not the person."))
        blind.setWordWrap(True)
        blind.setStyleSheet("color: palette(mid);")
        v.addWidget(blind)
        v.addStretch(1)
        self.setWidget(w)
        self.draw.currentIndexChanged.connect(self._fill_me)
        self.me.currentIndexChanged.connect(self._load)
        self.prev_b.clicked.connect(lambda: self._go(self.i - 1))
        self.next_b.clicked.connect(lambda: self._go(self.i + 1))
        self.zoom_b.clicked.connect(self._zoom)
        self.ok_b.clicked.connect(lambda: self._verdict("ok"))
        self.err_b.clicked.connect(lambda: self._verdict("error"))
        self.refresh()

    def refresh(self):
        self.rev, self.drw = tables()
        self.draw.blockSignals(True)
        self.draw.clear()
        if self.drw is not None:
            for r in store.draws_in(self.drw):
                self.draw.addItem(f"{r['layer']} — {r['made'][:16].replace('T', ' ')}", r["id"])
        self.draw.blockSignals(False)
        self._fill_me()

    def _fill_me(self):
        self.me.blockSignals(True)
        self.me.clear()
        names = set()
        if self.rev is not None and self.draw.currentData():
            names = {r["reviewer"] for r in store.rows_of(self.rev, self.draw.currentData()) if r["reviewer"]}
        from ..team import session

        mine = session.my_name()
        for n in sorted(names, key=str.lower):
            self.me.addItem(n)
        if self.me.findText(mine) >= 0:
            self.me.setCurrentIndex(self.me.findText(mine))
        self.me.blockSignals(False)
        self._load()

    def _load(self):
        self.rows = []
        if self.rev is not None and self.draw.currentData() and self.me.currentText():
            self.rows = store.rows_of(self.rev, self.draw.currentData(), reviewer=self.me.currentText())
        first = next((k for k, r in enumerate(self.rows) if not r["verdict"]), 0)
        self._go(first)

    def _go(self, i):
        if not self.rows:
            self.where.setText(tr("Nothing to review in this draw for this name."))
            for b in (self.prev_b, self.next_b, self.zoom_b, self.ok_b, self.err_b):
                b.setEnabled(False)
            return
        self.i = max(0, min(i, len(self.rows) - 1))
        r = self.rows[self.i]
        done = sum(1 for x in self.rows if x["verdict"])
        state = {"ok": tr("Correct"), "error": tr("Error")}.get(r["verdict"] or "", tr("Not reviewed yet"))
        self.where.setText(tr("Feature {k} of {n} ({d} done): «{layer}», number {fid}. Now: {state}.").format(
            k=self.i + 1, n=len(self.rows), d=done, layer=r["layer"], fid=r["feature"], state=state))
        self.prev_b.setEnabled(self.i > 0)
        self.next_b.setEnabled(self.i < len(self.rows) - 1)
        for b in (self.zoom_b, self.ok_b, self.err_b):
            b.setEnabled(True)
        self.note.setText(r["note"] or "")
        if r["category"]:
            self.cat.setCurrentIndex(max(0, self.cat.findData(r["category"])))
        self._zoom()

    def _zoom(self):
        if not self.rows:
            return
        r = self.rows[self.i]
        lyr = source_layer(r)
        if lyr is None:
            return
        f = next(lyr.getFeatures(QgsFeatureRequest(int(r["feature"]))), None)
        if f is None or not f.hasGeometry():
            return
        canvas = self.plugin.iface.mapCanvas()
        box = QgsCoordinateTransform(lyr.crs(), canvas.mapSettings().destinationCrs(),
                                     QgsProject.instance()).transformBoundingBox(f.geometry().boundingBox())
        box.grow(max(box.width(), box.height(), 5.0) * 0.6)
        canvas.setExtent(box)
        canvas.refresh()
        canvas.flashGeometries([f.geometry()], lyr.crs())

    def _verdict(self, verdict):
        r = self.rows[self.i]
        lyr = source_layer(r)
        changed = False
        if lyr is not None:
            now = store.prints_of(lyr, [r["feature"]]).get(r["feature"])
            changed = now != r["print"]
        store.set_verdict(self.rev, r["row"], verdict, self.cat.currentData() if verdict == "error" else "",
                          self.note.text().strip(), changed)
        r.update(verdict=verdict, changed=int(changed))
        if changed:
            self.plugin.iface.messageBar().pushMessage(
                tr("Review my share"), tr("This feature changed after the draw: the verdict is kept with that "
                                          "note."), Qgis.MessageLevel.Warning, 6)
        self.note.clear()
        nxt = next((k for k in range(self.i + 1, len(self.rows)) if not self.rows[k]["verdict"]), None)
        if nxt is None:
            nxt = next((k for k in range(len(self.rows)) if not self.rows[k]["verdict"]), self.i)
        self._go(nxt)


_DOCK = [None]


def review_ui(plugin):
    rev, drw = tables()
    if rev is None or not store.draws_in(drw):
        QMessageBox.information(plugin.iface.mainWindow(), tr("Review my share"),
                                tr("There is no draw on this map yet. Make one with «Peer Review Lottery», or open "
                                   "the map's GeoPackage that holds the reviews."))
        return None
    if _DOCK[0] is None:
        _DOCK[0] = ReviewDock(plugin)
        plugin.iface.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, _DOCK[0])
    else:
        _DOCK[0].refresh()
    _DOCK[0].show()
    _DOCK[0].raise_()
    return _DOCK[0]


# ---- the results ----------------------------------------------------------------------------------------------------
class ResultsDialog(QDialog):
    def __init__(self, plugin, rev, drw):
        super().__init__(plugin.iface.mainWindow())
        self.plugin, self.rev, self.drw = plugin, rev, drw
        self.res = None
        self.setWindowTitle(tr("Peer Review Results"))
        self.setMinimumWidth(720)
        v = QVBoxLayout(self)
        top = QHBoxLayout()
        self.draw = QComboBox()
        self.records = store.draws_in(drw)
        for r in self.records:
            self.draw.addItem(f"{r['layer']} — {r['made'][:16].replace('T', ' ')}", r["id"])
        top.addWidget(QLabel(tr("Draw")))
        top.addWidget(self.draw, 1)
        v.addLayout(top)
        self.table = QTableWidget(0, 7)
        self.table.setHorizontalHeaderLabels([tr("Person"), tr("Features"), tr("Reviewed"), tr("Errors"),
                                              tr("Estimated error rate"), tr("95% interval"), tr("Decision")])
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.horizontalHeader().setStretchLastSection(True)
        v.addWidget(self.table)
        self.summary = QLabel()
        self.summary.setWordWrap(True)
        v.addWidget(self.summary)
        row = QHBoxLayout()
        ver = QPushButton(tr("Check the draw"))
        ver.clicked.connect(self.verify)
        show = QPushButton(tr("Show on the map"))
        show.clicked.connect(self.show_map)
        csv_b = QPushButton(tr("Save as CSV…"))
        csv_b.clicked.connect(lambda: self.save("csv"))
        html_b = QPushButton(tr("Save report…"))
        html_b.clicked.connect(lambda: self.save("html"))
        for b in (ver, show, csv_b, html_b):
            row.addWidget(b)
        v.addLayout(row)
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        bb.rejected.connect(self.reject)
        v.addWidget(bb)
        self.draw.currentIndexChanged.connect(self.compute)
        self.compute()

    def _record(self):
        return next((r for r in self.records if r["id"] == self.draw.currentData()), None)

    def compute(self):
        rec = self._record()
        if rec is None:
            return
        names = store.names_of(rec["id"])
        self.res = res = store.results(self.rev, rec, names)
        self.table.setRowCount(0)
        for r in res["lots"]:
            b = r["bounds"]
            k = self.table.rowCount()
            self.table.insertRow(k)
            name = r["name"] or (tr("(before the room)") if names else r["lot"])
            vals = (name, r["N"], f"{r['done']} / {r['n']}", r["errors"], _pct(b["rate"]) if b else "—",
                    f"{_pct(b['lo_rate'])} – {_pct(b['hi_rate'])}" if b else "—", tr(DECISION[r["decision"]]))
            for c, val in enumerate(vals):
                self.table.setItem(k, c, QTableWidgetItem(str(val)))
        t = res["total"]
        if t:
            text = tr("The whole delivery: {est} errors estimated in {N} features ({rate}); with 95% confidence at "
                      "most {up} ({urate}). Decision: {d}.").format(
                est=f"{t['est']:.1f}", N=t["N"], rate=_pct(t["rate"]), up=t["upper"], urate=_pct(t["upper_rate"]),
                d=tr(DECISION[res["decision"]]))
        else:
            text = tr("Nothing has been reviewed yet.")
        if res["changed"]:
            text += " " + tr("{n} features changed after the draw.").format(n=res["changed"])
        cats = {}
        for r in res["lots"]:
            for c, n in r["categories"].items():
                cats[c] = cats.get(c, 0) + n
        if cats:
            text += " " + tr("Errors by kind:") + " " + ", ".join(f"{tr(c)} {n}" for c, n in
                                                                 sorted(cats.items(), key=lambda t: -t[1]))
        self.summary.setText(text)

    def verify(self):
        rec = self._record()
        if rec is None:
            return
        lyr = source_layer({"source": rec.get("source"), "layer": rec.get("layer")})
        ok, problems, sample = store.verify_draw(rec, lyr)
        rows = store.rows_of(self.rev, rec["id"])
        kept = sorted((r["feature"], r["lot"]) for r in rows)
        same = kept == sorted((it[0], it[2]) for it in sample)
        if ok and same:
            msg = tr("The draw is fair: the seed comes from the commitment and the salt, and drawing again gives "
                     "exactly the features that were given for review.")
        else:
            msg = tr("The draw does not check out: the record or the reviews table was changed after the draw.")
        changed = next((p for p in problems if p.startswith("changed:")), None)
        if changed:
            msg += "\n" + tr("{n} features of the layer changed after the draw.").format(n=changed.split(":")[1])
        QMessageBox.information(self, tr("Check the draw"), msg)

    def show_map(self):
        rec = self._record()
        if rec is None:
            return
        style(self.rev)
        self.rev.setSubsetString(f"\"draw\" = '{rec['id']}'")
        self.plugin.iface.setActiveLayer(self.rev)
        canvas = self.plugin.iface.mapCanvas()
        box = QgsCoordinateTransform(self.rev.crs(), canvas.mapSettings().destinationCrs(),
                                     QgsProject.instance()).transformBoundingBox(self.rev.extent())
        if not box.isEmpty():
            box.grow(max(box.width(), box.height()) * 0.08)
            canvas.setExtent(box)
            canvas.refresh()

    def save(self, kind):
        if self.res is None:
            return
        flt = tr("CSV files (*.csv)") if kind == "csv" else tr("Web pages (*.html)")
        path, _ = QFileDialog.getSaveFileName(self, tr("Peer Review Results"), "", flt)
        if not path:
            return
        text = store.csv_text(self.res) if kind == "csv" else report_html(self.res, self._record())
        with open(path, "w", encoding="utf-8-sig" if kind == "csv" else "utf-8", newline="") as f:
            f.write(text)


def report_html(res, record):
    from ..i18n import is_rtl

    rtl = is_rtl()
    rows = []
    for r in res["lots"]:
        b = r["bounds"]
        rows.append("<tr>" + "".join(f"<td>{html.escape(str(v))}</td>" for v in (
            r["name"] or r["lot"], r["N"], f"{r['done']} / {r['n']}", r["errors"], _pct(b["rate"]) if b else "—",
            f"{_pct(b['lo_rate'])} – {_pct(b['hi_rate'])}" if b else "—", _pct(b["upper_rate"]) if b else "—",
            tr(DECISION[r["decision"]]))) + "</tr>")
    t = res["total"]
    head = "".join(f"<th>{html.escape(tr(h))}</th>" for h in (
        "Person", "Features", "Reviewed", "Errors", "Estimated error rate", "95% interval", "At most (95%)",
        "Decision"))
    method = tr("Exact hypergeometric bounds for each lot; the whole delivery joins them at a Bonferroni level, so "
                "its confidence is at least the one stated.")
    total = ""
    if t:
        total = (f"<p><b>{html.escape(tr('The whole delivery'))}:</b> {t['est']:.1f} / {t['N']} "
                 f"({_pct(t['rate'])}), {html.escape(tr('at most'))} {_pct(t['upper_rate'])} — "
                 f"<b>{html.escape(tr(DECISION[res['decision']]))}</b></p>")
    return (f"<!doctype html><html dir='{'rtl' if rtl else 'ltr'}'><head><meta charset='utf-8'>"
            f"<title>{html.escape(tr('Peer Review Results'))}</title><style>body{{font-family:'Segoe UI',Tahoma,"
            "sans-serif;margin:2em;color:#1f2328}table{border-collapse:collapse}td,th{border:1px solid #d0d7de;"
            "padding:.35em .7em}th{background:#f6f8fa}code{font-size:.85em}</style></head><body>"
            f"<h1>{html.escape(tr('Peer Review Results'))}</h1>"
            f"<p>{html.escape(record.get('layer', ''))} — {html.escape(record.get('made', ''))}</p>"
            f"<table><tr>{head}</tr>{''.join(rows)}</table>{total}"
            f"<p>{html.escape(tr('Commitment'))}: <code>{html.escape(record['commitment'])}</code><br>"
            f"{html.escape(tr('Salt'))}: <code>{html.escape(record['salt'])}</code><br>"
            f"{html.escape(tr('Seed'))}: <code>{html.escape(record['seed'])}</code></p>"
            f"<p>{html.escape(method)}</p>"
            "</body></html>")


def results_ui(plugin):
    rev, drw = tables()
    if rev is None or not store.draws_in(drw):
        QMessageBox.information(plugin.iface.mainWindow(), tr("Peer Review Results"),
                                tr("There is no draw on this map yet. Make one with «Peer Review Lottery», or open "
                                   "the map's GeoPackage that holds the reviews."))
        return None
    d = ResultsDialog(plugin, rev, drw)
    d.show()
    return d
