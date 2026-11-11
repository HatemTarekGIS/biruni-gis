"""The "Accounts and visitors…" window of the dashboard panel (accounts.py, ext_accounts.py)."""
from qgis.core import (QgsCoordinateReferenceSystem, QgsCoordinateTransform, QgsGeometry, QgsProject,
                       QgsVectorLayer)
from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtWidgets import (QAbstractItemView, QCheckBox, QDialog, QDialogButtonBox, QDoubleSpinBox,
                                 QFormLayout, QGroupBox, QHBoxLayout, QLabel, QLineEdit, QListWidget,
                                 QListWidgetItem, QMessageBox, QPushButton, QVBoxLayout)

from ..i18n import tr
from . import accounts


def _fields_of(d, project):
    out = []
    for src in (d.get("sources") or {}).values():
        lyr = project.mapLayer(src.get("layer"))
        if isinstance(lyr, QgsVectorLayer):
            for f in lyr.fields():
                if f.name() not in out:
                    out.append(f.name())
    return out


def _checks(items, checked):
    lw = QListWidget()
    lw.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
    for key, label in items:
        it = QListWidgetItem(label)
        it.setData(Qt.ItemDataRole.UserRole, key)
        it.setFlags(it.flags() | Qt.ItemFlag.ItemIsUserCheckable)
        it.setCheckState(Qt.CheckState.Checked if (checked == "*" or key in (checked or ())) else
                         Qt.CheckState.Unchecked)
        lw.addItem(it)
    lw.setMaximumHeight(150)
    return lw


def _checked(lw):
    return [lw.item(i).data(Qt.ItemDataRole.UserRole) for i in range(lw.count())
            if lw.item(i).checkState() == Qt.CheckState.Checked]


def selected_zone(plugin):
    """The selected polygons of the active layer, merged, as EPSG:4326 WKT; or None."""
    try:
        lyr = plugin.iface.activeLayer()
    except AttributeError:
        return None
    if not isinstance(lyr, QgsVectorLayer) or not lyr.selectedFeatureCount():
        return None
    g = QgsGeometry.unaryUnion([f.geometry() for f in lyr.selectedFeatures() if f.hasGeometry()])
    if g is None or g.isEmpty() or g.type() != 2:
        return None
    tr4326 = QgsCoordinateTransform(lyr.crs(), QgsCoordinateReferenceSystem("EPSG:4326"), QgsProject.instance())
    g.transform(tr4326)
    return g.asWkt(7)


class UserDialog(QDialog):
    def __init__(self, d, rec, plugin, project, parent=None):
        super().__init__(parent)
        self.setWindowTitle(tr("Account"))
        self.plugin, self.rec, self.zone = plugin, rec or {}, (rec or {}).get("zone")
        form = QFormLayout(self)
        self.user = QLineEdit(self.rec.get("user", ""))
        self.user.setEnabled(not rec)
        self.name = QLineEdit(self.rec.get("name", ""))
        self.pw = QLineEdit()
        self.pw.setEchoMode(QLineEdit.EchoMode.Password)
        self.pw.setPlaceholderText(tr("Unchanged") if rec else tr("4 characters at least"))
        form.addRow(tr("Username"), self.user)
        form.addRow(tr("Name shown"), self.name)
        form.addRow(tr("Password"), self.pw)
        self.widgets = _checks([(w["id"], w.get("title") or w["id"]) for w in d.get("widgets") or []],
                               self.rec.get("widgets", "*"))
        form.addRow(tr("What this account sees"), self.widgets)
        self.hide = _checks([(f, f) for f in _fields_of(d, project)], self.rec.get("hide") or [])
        form.addRow(tr("Fields kept back"), self.hide)
        row = QHBoxLayout()
        self.zl = QLabel()
        b1 = QPushButton(tr("Selected polygons on the map"))
        b1.clicked.connect(self._take)
        b2 = QPushButton(tr("Whole map"))
        b2.clicked.connect(lambda: self._set_zone(None))
        row.addWidget(self.zl, 1)
        row.addWidget(b1)
        row.addWidget(b2)
        form.addRow(tr("Area"), row)
        self._set_zone(self.zone)
        self.hours = QDoubleSpinBox()
        self.hours.setRange(0.25, 720)
        self.hours.setValue(float(self.rec.get("hours") or accounts.USER_HOURS))
        self.hours.setSuffix(" " + tr("h"))
        form.addRow(tr("A sign-in lasts"), self.hours)
        self.off = QCheckBox(tr("Turned off"))
        self.off.setChecked(bool(self.rec.get("disabled")))
        form.addRow("", self.off)
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        form.addRow(bb)

    def _set_zone(self, z):
        self.zone = z
        self.zl.setText(tr("Only the chosen area") if z else tr("The whole map"))

    def _take(self):
        z = selected_zone(self.plugin)
        if z is None:
            QMessageBox.information(self, tr("Account"), tr("Select one or more polygons on the map first."))
            return
        self._set_zone(z)

    def values(self):
        w = _checked(self.widgets)
        return {"username": self.user.text(), "name": self.name.text(), "password": self.pw.text() or None,
                "widgets": "*" if len(w) == self.widgets.count() else w, "hide": _checked(self.hide),
                "zone": self.zone, "hours": self.hours.value(), "disabled": self.off.isChecked()}


class AccountsDialog(QDialog):
    def __init__(self, d, plugin, dock=None):
        super().__init__(dock)
        self.d, self.plugin, self.dock = d, plugin, dock
        self.project = QgsProject.instance()
        self.setWindowTitle(tr("Accounts and visitors") + " — " + (d.get("title") or ""))
        lay = QVBoxLayout(self)
        lay.addWidget(QLabel(tr("Whoever scans a live paper's QR code (or opens its link) is asked to carry on as a "
                                "visitor or to sign in. Each account sees only its own part.")))
        g = QGroupBox(tr("Visitors"))
        gl = QVBoxLayout(g)
        acc = accounts.load(d["id"], self.project)
        v = acc["visitor"]
        self.v_on = QCheckBox(tr("Anyone with the paper may look (as a visitor)"))
        self.v_on.setChecked(bool(v.get("on")))
        gl.addWidget(self.v_on)
        gl.addWidget(QLabel(tr("What a visitor sees (no labels or details unless you keep their fields):")))
        default = v.get("widgets") or [w["id"] for w in d.get("widgets") or [] if w.get("type") in ("map", "choropleth")]
        self.v_widgets = _checks([(w["id"], w.get("title") or w["id"]) for w in d.get("widgets") or []], default)
        gl.addWidget(self.v_widgets)
        lay.addWidget(g)
        g2 = QGroupBox(tr("Accounts"))
        g2l = QVBoxLayout(g2)
        self.users = QListWidget()
        g2l.addWidget(self.users)
        row = QHBoxLayout()
        for text, fn in ((tr("Add…"), self._add), (tr("Edit…"), self._edit), (tr("Remove"), self._remove)):
            b = QPushButton(text)
            b.clicked.connect(fn)
            row.addWidget(b)
        g2l.addLayout(row)
        lay.addWidget(g2)
        self.live = QLabel()
        self.live.setWordWrap(True)
        lay.addWidget(self.live)
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Close)
        bb.accepted.connect(self._save)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)
        self._fill()

    def _fill(self):
        self.users.clear()
        for u in accounts.load(self.d["id"], self.project)["users"].values():
            it = QListWidgetItem(f"{u.get('name') or u['user']}  ({u['user']})" + ("  — " + tr("Turned off")
                                                                                   if u.get("disabled") else ""))
            it.setData(Qt.ItemDataRole.UserRole, u["user"])
            self.users.addItem(it)
        names = []
        try:
            svc = self.dock.service() if self.dock is not None else None
            for s in getattr(svc, "sessions", []) or []:
                c = s.card or {}
                if s.ready and c.get("acct") and c.get("dash") == self.d["id"]:
                    names.append(tr("Visitor") if c["acct"] == "visitor" else (c.get("name") or c["acct"][5:]))
        except Exception:  # noqa: BLE001
            pass
        self.live.setText(tr("Signed in now: {names}").format(names=", ".join(names)) if names
                          else tr("Nobody is signed in now."))

    def _current(self):
        it = self.users.currentItem()
        return it.data(Qt.ItemDataRole.UserRole) if it else None

    def _run(self, rec):
        dlg = UserDialog(self.d, rec, self.plugin, self.project, self)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        v = dlg.values()
        try:
            accounts.set_user(self.d["id"], v["username"], v["password"], v["name"], v["widgets"], v["hide"],
                              v["zone"], None, v["hours"], v["disabled"], self.project)
        except ValueError as e:
            QMessageBox.warning(self, tr("Account"), str(e))
            return
        self._fill()

    def _add(self):
        self._run(None)

    def _edit(self):
        u = self._current()
        if u:
            self._run(accounts.load(self.d["id"], self.project)["users"].get(u))

    def _remove(self):
        u = self._current()
        if u and QMessageBox.question(self, tr("Account"), tr("Remove the account {name}?").format(name=u)) == \
                QMessageBox.StandardButton.Yes:
            accounts.remove_user(self.d["id"], u, self.project)
            self._fill()

    def _save(self):
        w = _checked(self.v_widgets)
        accounts.set_visitor(self.d["id"], self.v_on.isChecked(), w, None, project=self.project, definition=self.d)
        self.accept()
