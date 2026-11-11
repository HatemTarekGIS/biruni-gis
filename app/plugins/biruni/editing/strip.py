"""A dismissible message strip (the coloured bar under a pane's title in ArcGIS Pro):
green "✓ Divide completed: 3 features.", blue "ⓘ Make a selection.", amber warnings, red errors.

Theme-aware (light / dark colours of its own, the app's text font) and right-to-left aware (the layout
follows the widget's direction, so the icon sits at the start of the line and × at its end).
"""
from qgis.PyQt.QtCore import QSize, Qt, pyqtSignal
from qgis.PyQt.QtWidgets import QFrame, QHBoxLayout, QLabel, QSizePolicy, QToolButton, QVBoxLayout, QWidget

from .. import icons
from ..i18n import tr

SUCCESS, INFO, WARNING, ERROR = "success", "info", "warning", "error"

# (background, border, text, icon) per kind and theme
_COLOURS = {
    False: {SUCCESS: ("#E7F5EC", "#B9E0C6", "#1D5131", "#1E8E3E"),
            INFO: ("#E8F1FC", "#BCD4F3", "#1C4A86", "#1A6FDB"),
            WARNING: ("#FEF5E6", "#F5D8A6", "#7A4300", "#D97706"),
            ERROR: ("#FDECEC", "#F3C1C1", "#7F1D1D", "#D64545")},
    True: {SUCCESS: ("#16301F", "#2A5A3B", "#BDEBCB", "#4ADE80"),
           INFO: ("#152A43", "#29496F", "#C0D9F8", "#60A5FA"),
           WARNING: ("#37280F", "#6A4A17", "#F7DAA8", "#F59E0B"),
           ERROR: ("#3A1717", "#6E2B2B", "#F6C4C4", "#F87171")},
}
_ICON = {SUCCESS: "edit/msg-success", INFO: "edit/msg-info", WARNING: "edit/msg-warning",
         ERROR: "edit/msg-warning"}


def colours(kind, dark=False):
    return _COLOURS[bool(dark)].get(kind, _COLOURS[bool(dark)][INFO])


class MessageStrip(QFrame):
    """One message: icon, wrapped text, a close button. ``closed`` is emitted when × is pressed."""
    closed = pyqtSignal()

    def __init__(self, kind, text, dark=False, parent=None):
        super().__init__(parent)
        self.kind = kind
        self.setObjectName("BiruniStrip")
        h = QHBoxLayout(self)
        h.setContentsMargins(8, 6, 4, 6)
        h.setSpacing(8)
        self.icon = QLabel()
        self.icon.setFixedSize(18, 18)
        h.addWidget(self.icon, 0, Qt.AlignmentFlag.AlignTop)
        self.label = QLabel(text)
        self.label.setWordWrap(True)
        self.label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.label.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        h.addWidget(self.label, 1)
        self.close_btn = QToolButton()
        self.close_btn.setAutoRaise(True)
        self.close_btn.setFixedSize(QSize(20, 20))
        self.close_btn.setToolTip(tr("Close"))
        self.close_btn.setIconSize(QSize(12, 12))
        self.close_btn.clicked.connect(self._close)
        h.addWidget(self.close_btn, 0, Qt.AlignmentFlag.AlignTop)
        self.set_theme(dark)

    def text(self):
        return self.label.text()

    def set_text(self, text):
        self.label.setText(text)

    def set_theme(self, dark):
        bg, border, fg, ic = colours(self.kind, dark)
        self.setStyleSheet(f"QFrame#BiruniStrip {{ background: {bg}; border: 1px solid {border}; border-radius: 6px; }}"
                           f"QFrame#BiruniStrip QLabel {{ color: {fg}; background: transparent; border: none; }}"
                           f"QFrame#BiruniStrip QToolButton {{ border: none; border-radius: 4px; background: transparent; }}"
                           f"QFrame#BiruniStrip QToolButton:hover {{ background: {border}; }}")
        self.icon.setPixmap(icons.icon(_ICON.get(self.kind, "edit/msg-info"), ic).pixmap(18, 18))
        self.close_btn.setIcon(icons.icon("x", fg))

    def _close(self):
        self.hide()
        self.closed.emit()
        self.deleteLater()


class StripArea(QWidget):
    """A column of strips; at most one strip of each kind (a new message replaces the old one)."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.v = QVBoxLayout(self)
        self.v.setContentsMargins(0, 0, 0, 0)
        self.v.setSpacing(6)
        self.dark = False
        self.strips = {}

    def show_message(self, kind, text):
        old = self.strips.get(kind)
        if old is not None:
            old.set_text(text)
            old.show()
            return old
        s = MessageStrip(kind, text, self.dark, self)
        s.closed.connect(lambda k=kind, w=s: self.strips.pop(k, None) if self.strips.get(k) is w else None)
        order = [SUCCESS, ERROR, WARNING, INFO]
        pos = sum(1 for k in order[:order.index(kind) if kind in order else 0] if k in self.strips)
        self.v.insertWidget(pos, s)
        self.strips[kind] = s
        return s

    def clear(self, kind=None):
        for k in ([kind] if kind else list(self.strips)):
            s = self.strips.pop(k, None)
            if s is not None:
                s.hide()
                s.deleteLater()

    def messages(self):
        """{kind: text} of the strips shown."""
        return {k: s.text() for k, s in self.strips.items()}

    def set_theme(self, dark):
        self.dark = bool(dark)
        for s in self.strips.values():
            s.set_theme(self.dark)
