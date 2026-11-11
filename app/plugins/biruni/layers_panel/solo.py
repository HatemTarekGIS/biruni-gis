"""Solo peek: only the chosen layers on the map for a moment, then exactly the visibility from before (every layer and
every group tick). The project is not marked as changed by peeking when it was not changed before."""
from qgis.core import QgsLayerTree, QgsProject
from qgis.PyQt.QtCore import QEvent, QObject, Qt, pyqtSignal


def visibility_state(root):
    """{"layers": {layer id: ticked}, "groups": [(group node, ticked)]} - the ticks of the whole tree."""
    return {"layers": {n.layerId(): n.itemVisibilityChecked() for n in root.findLayers()},
            "groups": [(g, g.itemVisibilityChecked()) for g in root.findGroups(True)]}


class Solo(QObject):
    changed = pyqtSignal(bool)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.saved = None  # visibility_state() from before the peek, plus the soloed ids

    def active(self):
        return self.saved is not None

    def soloed_ids(self):
        return set(self.saved["ids"]) if self.saved else set()

    def peek(self, layers):
        """Shows only these layers (and the groups holding them). Returns False when there is nothing to show."""
        ids = {lyr.id() for lyr in layers if lyr is not None}
        if not ids:
            return False
        if self.saved is not None:
            self.restore()
        project = QgsProject.instance()
        root = project.layerTreeRoot()
        dirty = project.isDirty()
        state = visibility_state(root)
        state["ids"] = ids
        for node in root.findLayers():
            if node.layerId() not in ids and node.itemVisibilityChecked():
                node.setItemVisibilityChecked(False)
        for node in root.findLayers():
            if node.layerId() in ids:
                node.setItemVisibilityCheckedParentRecursive(True)
        if not dirty:
            project.setDirty(False)
        self.saved = state
        self.changed.emit(True)
        return True

    def restore(self):
        """Puts every tick back as it was before the peek."""
        state, self.saved = self.saved, None
        if state is None:
            return
        project = QgsProject.instance()
        root = project.layerTreeRoot()
        dirty = project.isDirty()
        for group, checked in state["groups"]:
            try:
                if group.itemVisibilityChecked() != checked:
                    group.setItemVisibilityChecked(checked)
            except RuntimeError:  # the group was removed meanwhile
                pass
        for node in root.findLayers():
            checked = state["layers"].get(node.layerId())
            if checked is not None and node.itemVisibilityChecked() != checked:
                node.setItemVisibilityChecked(checked)
        if not dirty:
            project.setDirty(False)
        self.changed.emit(False)

    def toggle(self, layers):
        if self.active():
            self.restore()
            return False
        return self.peek(layers)

    def toggle_layer(self, layer):
        """Alt+click: solo this layer; Alt+click it again to come back."""
        if self.active() and self.soloed_ids() == {layer.id()}:
            self.restore()
            return False
        return self.peek([layer])

    def forget(self):
        """A project was opened or closed: the saved ticks belong to the old one."""
        if self.saved is not None:
            self.saved = None
            self.changed.emit(False)


class AltClick(QObject):
    """Event filter on the layer tree view's viewport: Alt+click on a layer = solo peek of that layer."""

    def __init__(self, view, solo):
        super().__init__(view)
        self.view = view
        self.solo = solo

    def eventFilter(self, obj, event):  # noqa: N802
        if (event.type() == QEvent.Type.MouseButtonPress and event.button() == Qt.MouseButton.LeftButton
                and event.modifiers() & Qt.KeyboardModifier.AltModifier):
            node = self.view.index2node(self.view.indexAt(event.position().toPoint()))
            if node is not None and QgsLayerTree.isLayer(node) and node.layer() is not None:
                self.solo.toggle_layer(node.layer())
                return True
        return False
