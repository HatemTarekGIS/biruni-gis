"""Live search over the layer tree. Rows that do not match are hidden in the view only (the project, the layers and
their visibility are never touched). Matching ignores case, Latin accents, Arabic diacritics and tatweel, and treats
the usual Arabic/Persian letter forms alike (أ إ آ ا, ى ي ی, ة ه, ك ک, Arabic-Indic digits)."""
import unicodedata

from qgis.core import QgsLayerTree
from qgis.PyQt.QtCore import QModelIndex, QObject, QPersistentModelIndex, Qt, QTimer
from qgis.PyQt.QtWidgets import QLineEdit

from ..i18n import tr

_FOLD = str.maketrans({
    "ـ": "",  # tatweel
    "ٱ": "ا", "ى": "ي", "ی": "ي", "ئ": "ي", "ؤ": "و", "ة": "ه", "ک": "ك", "ګ": "ك",
    **{chr(0x0660 + i): str(i) for i in range(10)}, **{chr(0x06F0 + i): str(i) for i in range(10)},
})


def normalize(text):
    """Text folded for matching: أَحْمَد → احمد, Café → cafe, مدرسة → مدرسه, ١٢ → 12."""
    text = unicodedata.normalize("NFKD", text or "")
    text = "".join(ch for ch in text if unicodedata.category(ch) != "Mn")  # harakat, hamza/madda marks, accents
    return text.translate(_FOLD).casefold()


def matches(name, terms):
    """True when every search term is in the (folded) name."""
    folded = normalize(name)
    return all(t in folded for t in terms)


class SearchEdit(QLineEdit):
    """The box: Esc clears it, Down moves into the layer list."""

    def __init__(self, view, parent=None):
        super().__init__(parent)
        self.view = view
        self.setObjectName("biruniLayersSearch")
        self.setClearButtonEnabled(True)
        self.setPlaceholderText(tr("Search layers…"))
        self.setToolTip(tr("Type part of a layer or group name. Esc clears the search."))

    def keyPressEvent(self, event):  # noqa: N802
        if event.key() == Qt.Key.Key_Escape and self.text():
            self.clear()
            event.accept()
            return
        if event.key() == Qt.Key.Key_Down:
            self.view.setFocus()
            event.accept()
            return
        super().keyPressEvent(event)


class LayerSearch(QObject):
    """Hides the rows of the layer tree view that do not match the text of the box."""

    def __init__(self, view, edit, on_result=None):
        super().__init__(edit)
        self.view = view
        self.edit = edit
        self.on_result = on_result  # fn(text, any_match) - e.g. to show "nothing matches"
        self.expanded = []  # groups we opened to show a match (closed again when the search is cleared)
        self.filtered = False
        self.timer = QTimer(self)
        self.timer.setSingleShot(True)
        self.timer.setInterval(120)
        self.timer.timeout.connect(self.apply)
        edit.textChanged.connect(lambda _t: self.timer.start())
        model = view.model()
        self.model = model
        for sig in (model.rowsInserted, model.rowsRemoved, model.rowsMoved, model.modelReset, model.layoutChanged):
            sig.connect(self._model_changed)

    def _model_changed(self, *_args):
        if self.edit.text().strip():
            self.timer.start()

    def apply(self):
        terms = normalize(self.edit.text()).split()
        if not terms:
            self.clear_filter()
            if self.on_result:
                self.on_result("", True)
            return
        any_match = self._walk(QModelIndex(), terms, False)
        self.filtered = True
        if self.on_result:
            self.on_result(self.edit.text(), any_match)

    def _walk(self, parent, terms, inside_match):
        model, view = self.view.model(), self.view
        any_shown = False
        for row in range(model.rowCount(parent)):
            idx = model.index(row, 0, parent)
            node = view.index2node(idx)
            if node is None:  # a legend entry of a layer: follows its layer
                continue
            name_ok = matches(node.name(), terms)
            if QgsLayerTree.isGroup(node):
                child_ok = self._walk(idx, terms, inside_match or name_ok)
                shown = inside_match or name_ok or child_ok
                if child_ok and not view.isExpanded(idx):
                    view.expand(idx)
                    self.expanded.append(QPersistentModelIndex(idx))
            else:
                shown = inside_match or name_ok
            view.setRowHidden(row, parent, not shown)
            any_shown = any_shown or shown
        return any_shown

    def _unhide(self, parent):
        model = self.view.model()
        for row in range(model.rowCount(parent)):
            idx = model.index(row, 0, parent)
            if self.view.isRowHidden(row, parent):
                self.view.setRowHidden(row, parent, False)
            node = self.view.index2node(idx)
            if node is not None and QgsLayerTree.isGroup(node):
                self._unhide(idx)

    def clear_filter(self):
        if not self.filtered:
            return
        self.filtered = False
        self._unhide(QModelIndex())
        for pidx in reversed(self.expanded):
            if pidx.isValid():
                self.view.collapse(QModelIndex(pidx))
        self.expanded = []

    def teardown(self):
        self.timer.stop()
        for sig in (self.model.rowsInserted, self.model.rowsRemoved, self.model.rowsMoved, self.model.modelReset,
                    self.model.layoutChanged):
            try:
                sig.disconnect(self._model_changed)
            except (TypeError, RuntimeError):
                pass
        try:
            self.clear_filter()
        except RuntimeError:
            pass
