"""Saving the project does not save layer edits.

People press Ctrl+S, see the project saved, and lose a day of digitising when the program closes or
crashes, because the edits were still only in the layers' edit buffers. After every project save this
guard looks for layers with unsaved edits and says so plainly, with buttons to save them all or to show
them. (Closing the project or Biruni already asks about unsaved edits - that is QGIS's own question.)
"""
from qgis.core import Qgis, QgsProject
from qgis.PyQt.QtCore import QObject
from qgis.PyQt.QtWidgets import QPushButton

from .i18n import tr
from .version import APP_NAME


def unsaved_edit_layers(project=None):
    """Vector layers that are in edit mode and have changes that are not saved yet."""
    project = project or QgsProject.instance()
    return sorted((lyr for lyr in project.mapLayers().values()
                   if lyr.type() == Qgis.LayerType.Vector and lyr.isEditable() and lyr.isModified()),
                  key=lambda lyr: lyr.name().lower())


def save_all(layers):
    """Commit the edits of every layer and keep them in edit mode. Returns (saved names, failures)."""
    saved, failed = [], []
    for lyr in layers:
        if lyr.commitChanges(False):
            saved.append(lyr.name())
        else:
            failed.append((lyr.name(), "; ".join(lyr.commitErrors()[:3])))
    return saved, failed


def message_text(layers):
    names = ", ".join(lyr.name() for lyr in layers[:4]) + ("…" if len(layers) > 4 else "")
    return tr("The project was saved, but the edits of these layers are not saved yet: {names}").format(names=names)


class EditGuard(QObject):
    def __init__(self, plugin):
        super().__init__()
        self.plugin = plugin
        self.iface = plugin.iface
        self._item = None

    def install(self):
        QgsProject.instance().projectSaved.connect(self.check)

    def uninstall(self):
        try:
            QgsProject.instance().projectSaved.disconnect(self.check)
        except TypeError:
            pass
        self._pop()

    def _pop(self):
        bar = self.iface.messageBar()
        if self._item is not None:
            try:
                bar.popWidget(self._item)
            except RuntimeError:  # already gone
                pass
        self._item = None

    def check(self, *_):
        layers = unsaved_edit_layers()
        self._pop()
        if not layers:
            return None
        bar = self.iface.messageBar()
        item = bar.createMessage(APP_NAME, message_text(layers))
        save = QPushButton(tr("Save all edits"))
        save.clicked.connect(lambda: self._save(layers))
        show = QPushButton(tr("Show the layers"))
        show.clicked.connect(lambda: self._show(layers))
        item.layout().addWidget(save)
        item.layout().addWidget(show)
        self._item = item
        bar.pushWidget(item, Qgis.MessageLevel.Warning, 0)
        return item

    def _save(self, layers):
        self._pop()
        saved, failed = save_all([lyr for lyr in layers if lyr.isEditable()])
        bar = self.iface.messageBar()
        if failed:
            bar.pushCritical(APP_NAME, tr("These edits could not be saved: {f}").format(
                f="; ".join(f"{n}: {e}" for n, e in failed)))
        elif saved:
            bar.pushSuccess(APP_NAME, tr("Edits saved in {n} layers.").format(n=len(saved)))

    def _show(self, layers):
        view = self.iface.layerTreeView()
        if view is None or not layers:
            return
        view.setCurrentLayer(layers[0])
        try:
            view.setSelectedLayers(layers)
        except AttributeError:
            pass


_instance = None


def install(plugin):
    global _instance
    if _instance is None:
        _instance = EditGuard(plugin)
        _instance.install()
    return _instance


def uninstall():
    global _instance
    if _instance is not None:
        _instance.uninstall()
        _instance = None
