"""What the buttons, keys and menus of Map Stops do."""
import os

from qgis.core import Qgis, QgsCoordinateReferenceSystem, QgsCoordinateTransform, QgsSettings
from qgis.PyQt.QtCore import Qt, QTimer, QUrl
from qgis.PyQt.QtGui import QDesktopServices, QPixmap
from qgis.PyQt.QtWidgets import (QApplication, QButtonGroup, QCheckBox, QComboBox, QDialog, QDialogButtonBox,
                                 QDoubleSpinBox, QFileDialog, QFormLayout, QHBoxLayout, QLabel, QPushButton,
                                 QRadioButton, QSpinBox, QVBoxLayout)

from ..i18n import language_info, tr
from . import view_history
from .model import PROJECT, USER, format_scale, set_setting, setting, store_for

APP = "Biruni GIS"


def _bar(plugin):
    return plugin.iface.messageBar()


def _info(plugin, text, level=None, duration=4):
    try:
        _bar(plugin).pushMessage(tr("Map Stops"), text, level if level is not None else Qgis.MessageLevel.Info,
                                 duration)
    except (AttributeError, TypeError):
        pass


def _warn(plugin, text):
    _info(plugin, text, Qgis.MessageLevel.Warning, 6)


# ---- new / go --------------------------------------------------------------------------------------------------
def new_stop(plugin, name=None, scope=None):
    """One click: the current view is saved at once, named after the place under the map centre."""
    store = store_for(plugin)
    canvas = plugin.iface.mapCanvas()
    stop = store.capture(canvas, name=name, scope=scope)
    if stop is None:
        _warn(plugin, tr("The stop could not be saved."))
        return None
    where = tr("all my projects") if stop.scope == USER else tr("this project")
    _info(plugin, tr("Saved «{name}» in {where}.").format(name=stop.name, where=where), Qgis.MessageLevel.Success, 3)
    if hasattr(plugin, "activity") and plugin.activity is not None:
        try:
            plugin.activity.log("stops", "new", stop.name)
        except Exception:  # noqa: BLE001 - logging is optional
            pass
    return stop


def go_to(plugin, stop_or_id, on_done=None, duration_ms=None):
    store = store_for(plugin)
    stop = store.get(stop_or_id) if isinstance(stop_or_id, str) else stop_or_id
    if stop is None:
        return False
    canvas = plugin.iface.mapCanvas()
    try:
        centre, mupp, rot = store.target(stop, canvas)
    except ValueError:
        _warn(plugin, tr("«{name}» cannot be shown in the map's coordinate system.").format(name=stop.name))
        return False
    from .fly import fly

    if duration_ms is None:
        duration_ms = setting("fly_ms") if setting("fly") else 0

    def done():
        store.show_extras(stop, canvas)
        if on_done is not None:
            on_done()

    fly(canvas, centre, mupp, rot, duration_ms, done, view_history(plugin))
    return True


def jump_to_number(plugin, n):
    stops = store_for(plugin).stops()
    if 1 <= n <= len(stops):
        return go_to(plugin, stops[n - 1])
    _info(plugin, tr("There is no stop {n} yet.").format(n=n))
    return False


def go_history(plugin, step):
    h = view_history(plugin)
    frame = h.step(step)
    if frame is None:
        _info(plugin, tr("No earlier view.") if step < 0 else tr("No later view."), duration=2)
        return False
    canvas = plugin.iface.mapCanvas()
    from .fly import fly
    from .model import fit_frame

    dest = canvas.mapSettings().destinationCrs()
    try:
        f = frame.transformed(dest) if dest.isValid() else frame
    except ValueError:
        return False
    centre, mupp = fit_frame(f, canvas)
    h.suspended += 1  # the flight lands on a view already in the history

    def done():
        h.suspended -= 1

    fly(canvas, centre, mupp, f.rot, min(450, setting("fly_ms")) if setting("fly") else 0, done, h)
    return True


def numbers(store):
    return {s.id: i + 1 for i, s in enumerate(store.stops()[:9])}


def jump_hint():
    from .keys import jump_sequence, shortcut_text

    seq = jump_sequence(1)
    if seq.isEmpty():
        return ""
    return tr("Press {key} to go here").format(key=shortcut_text(seq)[:-1] + "{n}")


# ---- tour --------------------------------------------------------------------------------------------------------
def play_tour(plugin, ids=None):
    from .tour import start_tour

    store = store_for(plugin)
    stops = [store.get(i) for i in ids] if ids else store.stops()
    stops = [s for s in stops if s is not None]
    if not stops:
        _info(plugin, tr("Save a few stops first - the tour plays them in order."))
        return None
    return start_tour(plugin, stops)


# ---- send to a phone ---------------------------------------------------------------------------------------------
def phone_link(stop, project=None):
    """A living-paper address that opens this stop's area on a phone (no server: the map is in the address)."""
    from ..living_paper import encode

    wgs = QgsCoordinateReferenceSystem("EPSG:4326")
    f = stop.frame
    if f.w <= 0 or f.h <= 0:
        from .model import Frame, _units_for_metres

        side = _units_for_metres(f, 400.0)
        f = Frame(f.cx, f.cy, side, side, f.rot, f.crs)
    corners = f.corners()
    if f.crs.isValid() and f.crs != wgs:
        from qgis.core import QgsProject

        xf = QgsCoordinateTransform(f.crs, wgs, project or QgsProject.instance())
        corners = [xf.transform(p) for p in corners]
    pts = [(max(-180.0, min(180.0, p.x())), max(-90.0, min(90.0, p.y()))) for p in corners]
    code = language_info().code
    lang = "en" if code == "en_US" else code
    title = stop.name if not stop.note else f"{stop.name} - {' '.join(stop.note.split())}"
    url, _report = encode(title, pts, format_scale(stop.scale), lang=lang)
    return url


def qr_pixmap(url, scale=5):
    import io

    from ..living_paper.layout import qr_code

    qr = qr_code(url)
    buf = io.BytesIO()
    qr.save(buf, kind="png", scale=scale, border=2, dark="#111111", light="#FFFFFF")
    pm = QPixmap()
    pm.loadFromData(buf.getvalue())
    return pm


class PhoneDialog(QDialog):
    def __init__(self, plugin, stop, url, parent=None):
        super().__init__(parent or plugin.iface.mainWindow())
        self.setWindowTitle(tr("Send «{name}» to a phone").format(name=stop.name))
        self.url = url
        v = QVBoxLayout(self)
        intro = QLabel(tr("Scan with the phone's camera: the same area opens on the phone's map, with the phone's own "
                          "position. Nothing is uploaded - the area travels inside the link."))
        intro.setWordWrap(True)
        v.addWidget(intro)
        self.qr = QLabel()
        self.qr.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.qr.setPixmap(qr_pixmap(url))
        v.addWidget(self.qr)
        name = QLabel(f"<b>{stop.name}</b>  <span style='color:#888'>{format_scale(stop.scale)}</span>")
        name.setAlignment(Qt.AlignmentFlag.AlignCenter)
        v.addWidget(name)
        row = QHBoxLayout()
        self.copy_btn = QPushButton(tr("Copy link"))
        self.copy_btn.clicked.connect(self._copy)
        open_btn = QPushButton(tr("Open"))
        open_btn.setToolTip(tr("Opens the link in this computer's browser."))
        open_btn.clicked.connect(lambda: QDesktopServices.openUrl(QUrl(self.url)))
        close = QPushButton(tr("Close"))
        close.clicked.connect(self.accept)
        row.addWidget(self.copy_btn)
        row.addWidget(open_btn)
        row.addStretch(1)
        row.addWidget(close)
        v.addLayout(row)

    def _copy(self):
        QApplication.clipboard().setText(self.url)
        self.copy_btn.setText(tr("Copied"))
        QTimer.singleShot(1500, lambda: self.copy_btn.setText(tr("Copy link")))


def send_to_phone(plugin, stop_id=None, show=True):
    store = store_for(plugin)
    stop = store.get(stop_id) if stop_id else None
    if stop is None:  # the current view
        from .model import Frame, Stop

        canvas = plugin.iface.mapCanvas()
        f = Frame.from_canvas(canvas)
        from qgis.core import QgsReferencedRectangle

        stop = Stop("", PROJECT, tr("My map"), "", QgsReferencedRectangle(f.bbox(), f.crs), f.rot,
                    {"view": f.to_dict(), "scale": canvas.scale()})
    try:
        url = phone_link(stop, store.project)
    except ValueError as e:
        _warn(plugin, tr("This area cannot be sent to a phone: {e}").format(e=e))
        return None
    dlg = PhoneDialog(plugin, stop, url)
    if show:
        dlg.show()
        _keep(plugin, dlg)
    return dlg


def _keep(plugin, dlg):
    keep = getattr(plugin, "_stops_dialogs", None)
    if keep is None:
        keep = []
        try:
            plugin._stops_dialogs = keep  # noqa: SLF001
        except AttributeError:
            return
    keep.append(dlg)
    dlg.finished.connect(lambda *_: keep.remove(dlg) if dlg in keep else None)


# ---- export / import -------------------------------------------------------------------------------------------
def _filters():
    return ";;".join([tr("Biruni stops, with pictures (*.stops)"), tr("Spatial bookmarks (*.xml)"),
                      tr("ArcGIS Pro bookmarks (*.bkmx)"), tr("Google Earth, with pictures (*.kmz)"),
                      tr("Google Earth (*.kml)")])


def _last_dir():
    d = QgsSettings().value("biruni/stops/last_dir", "")
    return d if d and os.path.isdir(d) else os.path.expanduser("~")


def export_dialog(plugin, ids=None):
    from .formats import export

    store = store_for(plugin)
    stops = [store.get(i) for i in ids] if ids else store.stops()
    stops = [s for s in stops if s is not None]
    if not stops:
        _info(plugin, tr("There are no stops to export yet."))
        return None
    path, chosen = QFileDialog.getSaveFileName(plugin.iface.mainWindow(), tr("Export stops"),
                                               os.path.join(_last_dir(), tr("Map stops") + ".stops"), _filters())
    if not path:
        return None
    if not os.path.splitext(path)[1]:
        ext = chosen.rsplit("*", 1)[-1].rstrip(")") if "*" in chosen else ".stops"
        path += ext
    QgsSettings().setValue("biruni/stops/last_dir", os.path.dirname(path))
    try:
        n = export(path, stops, store.project)
    except (OSError, ValueError) as e:
        _warn(plugin, tr("Could not export: {e}").format(e=e))
        return None
    _info(plugin, tr("Exported {n} stops to {file}.").format(n=n, file=os.path.basename(path)),
          Qgis.MessageLevel.Success)
    return path


def import_dialog(plugin):
    from .formats import import_into

    all_types = tr("All stop files (*.stops *.xml *.bkmx *.kmz *.kml)")
    path, _f = QFileDialog.getOpenFileName(plugin.iface.mainWindow(), tr("Import stops"), _last_dir(),
                                           all_types + ";;" + _filters())
    if not path:
        return None
    QgsSettings().setValue("biruni/stops/last_dir", os.path.dirname(path))
    store = store_for(plugin)
    try:
        added = import_into(store, path, setting("scope"))
    except (OSError, ValueError, KeyError) as e:
        _warn(plugin, tr("Could not import {file}: {e}").format(file=os.path.basename(path), e=e))
        return None
    _info(plugin, tr("Imported {n} stops from {file}.").format(n=len(added), file=os.path.basename(path)),
          Qgis.MessageLevel.Success)
    return added


# ---- options ---------------------------------------------------------------------------------------------------
class OptionsDialog(QDialog):
    def __init__(self, plugin, parent=None):
        super().__init__(parent or plugin.iface.mainWindow())
        self.plugin = plugin
        self.setWindowTitle(tr("Map Stops - Options"))
        v = QVBoxLayout(self)
        form = QFormLayout()
        self.fly = QCheckBox(tr("Fly smoothly to a stop"))
        self.fly.setChecked(setting("fly"))
        self.fly_s = QDoubleSpinBox()
        self.fly_s.setRange(0.2, 3.0)
        self.fly_s.setSingleStep(0.1)
        self.fly_s.setDecimals(1)
        self.fly_s.setSuffix(" " + tr("s"))
        self.fly_s.setValue(setting("fly_ms") / 1000.0)
        self.fly.toggled.connect(self.fly_s.setEnabled)
        self.fly_s.setEnabled(self.fly.isChecked())
        row = QHBoxLayout()
        row.addWidget(self.fly)
        row.addWidget(self.fly_s)
        row.addStretch(1)
        form.addRow(row)
        self.pause = QSpinBox()
        self.pause.setRange(1, 60)
        self.pause.setSuffix(" " + tr("s"))
        self.pause.setValue(setting("tour_pause"))
        form.addRow(tr("Tour: stay at each stop"), self.pause)
        self.keys = QComboBox()
        for key, text in (("alt", tr("Alt+1 … Alt+9")), ("ctrl_alt", tr("Ctrl+Alt+1 … Ctrl+Alt+9")),
                          ("off", tr("None"))):
            self.keys.addItem(text, key)
        self.keys.setCurrentIndex(max(0, self.keys.findData(setting("jump_keys"))))
        self.keys.setToolTip(tr("Ctrl+1 … Ctrl+9 already open panels, so stops use Alt."))
        form.addRow(tr("Go to the first nine stops with"), self.keys)
        self.keep_scale = QCheckBox(tr("Keep the saved scale (else the whole saved area is shown)"))
        self.keep_scale.setChecked(setting("keep_scale"))
        form.addRow(self.keep_scale)
        self.layers = QCheckBox(tr("New stops remember which layers are on"))
        self.layers.setChecked(setting("remember_layers"))
        form.addRow(self.layers)
        self.names = QCheckBox(tr("Name new stops after the place under the map centre"))
        self.names.setChecked(setting("place_names"))
        form.addRow(self.names)
        where = QHBoxLayout()
        self.scope_group = QButtonGroup(self)
        self.in_project = QRadioButton(tr("This project"))
        self.in_user = QRadioButton(tr("All my projects"))
        self.scope_group.addButton(self.in_project)
        self.scope_group.addButton(self.in_user)
        (self.in_user if setting("scope") == USER else self.in_project).setChecked(True)
        where.addWidget(self.in_project)
        where.addWidget(self.in_user)
        where.addStretch(1)
        form.addRow(tr("Save new stops in"), where)
        v.addLayout(form)
        qgis_row = QHBoxLayout()
        qgis_btn = QPushButton(tr("Spatial bookmark manager…"))
        qgis_btn.setToolTip(tr("Every stop is also a spatial bookmark - the Spatial Bookmarks panel shows them too."))
        qgis_btn.clicked.connect(self._qgis_manager)
        qgis_row.addWidget(qgis_btn)
        qgis_row.addStretch(1)
        v.addLayout(qgis_row)
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        bb.accepted.connect(self.save)
        bb.rejected.connect(self.reject)
        v.addWidget(bb)

    def _qgis_manager(self):
        from .keys import QGIS_BOOKMARKS_ACTION, _qaction

        qa = _qaction(self.plugin, QGIS_BOOKMARKS_ACTION)
        if qa is not None:
            qa.trigger()

    def save(self):
        set_setting("fly", self.fly.isChecked())
        set_setting("fly_ms", int(round(self.fly_s.value() * 1000)))
        set_setting("tour_pause", self.pause.value())
        old_keys = setting("jump_keys")
        set_setting("jump_keys", self.keys.currentData())
        set_setting("keep_scale", self.keep_scale.isChecked())
        set_setting("remember_layers", self.layers.isChecked())
        set_setting("place_names", self.names.isChecked())
        set_setting("scope", USER if self.in_user.isChecked() else PROJECT)
        if old_keys != self.keys.currentData():
            from .keys import reinstall

            reinstall(self.plugin)
        self.accept()


def options_dialog(plugin, show=True):
    dlg = OptionsDialog(plugin)
    if show:
        dlg.exec()
    return dlg
