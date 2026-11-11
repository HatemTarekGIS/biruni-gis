"""Play the stops as a tour: fly to each in order, stay a few seconds with its name and note as a caption on the
map, then the next. Esc ends it; Space pauses; ← / → go to the previous / next stop."""
from qgis.PyQt.QtCore import QEvent, QObject, QSize, Qt, QTimer
from qgis.PyQt.QtWidgets import QApplication, QFrame, QHBoxLayout, QLabel, QToolButton, QVBoxLayout

from .. import icons
from ..i18n import native_digits, tr
from .model import setting

_TOURS = {}


class Caption(QFrame):
    """The caption on the map: name, note, n / total and the controls."""

    def __init__(self, canvas, tour):
        super().__init__(canvas)
        self.canvas = canvas
        self.setObjectName("StopCaption")
        self.setStyleSheet(
            "#StopCaption { background: rgba(20, 24, 28, 215); border-radius: 12px; }"
            "#StopCaption QLabel { color: #F3F5F7; background: transparent; }"
            "#StopCaption QLabel#CapTitle { font-size: 13pt; font-weight: 600; }"
            "#StopCaption QLabel#CapCount { color: #A9B4BE; }"
            "#StopCaption QToolButton { color: #F3F5F7; background: transparent; border: none; border-radius: 6px;"
            " padding: 3px 8px; font-size: 11pt; }"
            "#StopCaption QToolButton:hover { background: rgba(255,255,255,40); }")
        v = QVBoxLayout(self)
        v.setContentsMargins(18, 12, 18, 10)
        v.setSpacing(4)
        top = QHBoxLayout()
        self.title = QLabel()
        self.title.setObjectName("CapTitle")
        self.count = QLabel()
        self.count.setObjectName("CapCount")
        top.addWidget(self.title, 1)
        top.addWidget(self.count)
        v.addLayout(top)
        self.note = QLabel()
        self.note.setWordWrap(True)
        v.addWidget(self.note)
        ctl = QHBoxLayout()
        ctl.setSpacing(2)
        rtl = QApplication.layoutDirection() == Qt.LayoutDirection.RightToLeft
        self.prev_btn = self._btn("stops/player-track-next" if rtl else "stops/player-track-prev", tr("Previous stop"),
                                  tour.previous)
        self.pause_btn = self._btn("stops/player-pause", tr("Pause (Space)"), tour.toggle_pause)
        self.next_btn = self._btn("stops/player-track-prev" if rtl else "stops/player-track-next", tr("Next stop"),
                                  tour.next)
        hint = QLabel(tr("Esc ends the tour"))
        hint.setObjectName("CapCount")
        end = self._btn("x", tr("End the tour (Esc)"), tour.stop)
        for w in (self.prev_btn, self.pause_btn, self.next_btn):
            ctl.addWidget(w)
        ctl.addStretch(1)
        ctl.addWidget(hint)
        ctl.addWidget(end)
        v.addLayout(ctl)

    def _btn(self, icon_name, tip, fn):
        b = QToolButton()
        b.setIcon(icons.icon(icon_name, "#F3F5F7"))
        b.setIconSize(QSize(18, 18))
        b.setToolTip(tip)
        b.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        b.clicked.connect(fn)
        return b

    def show_stop(self, stop, i, n):
        self.title.setText(stop.name)
        self.note.setText(stop.note)
        self.note.setVisible(bool(stop.note))
        self.count.setText(native_digits(f"{i + 1} / {n}"))
        self.place()
        self.show()
        self.raise_()

    def place(self):
        cw, ch = self.canvas.width(), self.canvas.height()
        w = min(560, max(280, cw - 40))
        self.setFixedWidth(w)
        self.adjustSize()
        self.move((cw - w) // 2, max(10, ch - self.height() - 24))


class Tour(QObject):
    def __init__(self, plugin, stops, pause_s=None):
        super().__init__(plugin.iface.mapCanvas())
        self.plugin = plugin
        self.canvas = plugin.iface.mapCanvas()
        self.stops = list(stops)
        self.index = -1
        self.pause_s = pause_s if pause_s is not None else setting("tour_pause")
        self.paused = False
        self.running = False
        self.timer = QTimer(self)
        self.timer.setSingleShot(True)
        self.timer.timeout.connect(self.next)
        self.caption = Caption(self.canvas, self)
        self.caption.hide()

    def start(self):
        self.running = True
        QApplication.instance().installEventFilter(self)
        self.canvas.installEventFilter(self)
        self.go(0)

    def go(self, i):
        if not self.running:
            return
        if i >= len(self.stops):
            self.finish()
            return
        self.index = max(0, i)
        self.timer.stop()
        stop = self.stops[self.index]
        from .actions import go_to

        self.caption.show_stop(stop, self.index, len(self.stops))
        go_to(self.plugin, stop, on_done=self._arrived)

    def _arrived(self):
        if self.running and not self.paused:
            self.timer.start(int(self.pause_s * 1000))

    def next(self):
        self.go(self.index + 1)

    def previous(self):
        self.go(max(0, self.index - 1))

    def toggle_pause(self):
        self.paused = not self.paused
        self.caption.pause_btn.setIcon(icons.icon("stops/player-play" if self.paused else "stops/player-pause",
                                                  "#F3F5F7"))
        if self.paused:
            self.timer.stop()
        else:
            self.timer.start(int(self.pause_s * 1000))

    def finish(self):
        """The end: a last caption for a moment."""
        self.timer.stop()
        self.caption.title.setText(tr("End of the tour"))
        self.caption.note.setVisible(False)
        QTimer.singleShot(1500, self.stop)

    def stop(self):
        if not self.running:
            return
        self.running = False
        self.timer.stop()
        try:
            QApplication.instance().removeEventFilter(self)
            self.canvas.removeEventFilter(self)
        except RuntimeError:
            pass
        self.caption.hide()
        self.caption.deleteLater()
        from .fly import stop_flight

        stop_flight(self.canvas, land=False)
        _TOURS.pop(id(self.plugin), None)

    def eventFilter(self, obj, ev):  # noqa: N802
        if not self.running:
            return False
        if ev.type() == QEvent.Type.Resize and obj is self.canvas:
            self.caption.place()
        elif ev.type() == QEvent.Type.KeyPress:
            k = ev.key()
            rtl = QApplication.layoutDirection() == Qt.LayoutDirection.RightToLeft
            if k == Qt.Key.Key_Escape:
                self.stop()
                return True
            if k == Qt.Key.Key_Space:
                self.toggle_pause()
                return True
            if k in (Qt.Key.Key_Right, Qt.Key.Key_Left) and not ev.modifiers():
                forward = (k == Qt.Key.Key_Right) != rtl
                self.next() if forward else self.previous()
                return True
        return False


def start_tour(plugin, stops, pause_s=None):
    old = _TOURS.get(id(plugin))
    if old is not None:
        old.stop()
    t = Tour(plugin, stops, pause_s)
    _TOURS[id(plugin)] = t
    t.start()
    return t


def current(plugin):
    return _TOURS.get(id(plugin))


def shutdown(plugin):
    t = _TOURS.get(id(plugin))
    if t is not None:
        t.stop()
