"""The views you visited, like a browser's history: Alt+← goes back, Alt+→ forward (the last 50 views).

Cheap on purpose (it is installed at start-up): one connection to the canvas' extentsChanged that restarts a single
shot timer; the view is read only when the map has stayed still for a moment. Going back or forward does not add
to the history (a new view after going back drops the "forward" part, like a browser)."""
from qgis.PyQt.QtCore import QObject, QTimer

LIMIT = 50
SETTLE_MS = 450


class ViewHistory(QObject):
    def __init__(self, canvas, limit=LIMIT):
        super().__init__(canvas)
        self.canvas = canvas
        self.limit = limit
        self.views = []      # Frame objects (imported on first record)
        self.mupps = []      # map units per pixel of each (a resized window is not a new view)
        self.index = -1
        self.suspended = 0   # > 0 while flying or going back/forward
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(SETTLE_MS)
        self._timer.timeout.connect(self.record)
        canvas.extentsChanged.connect(self._timer.start)

    def detach(self):
        try:
            self.canvas.extentsChanged.disconnect(self._timer.start)
        except (TypeError, RuntimeError):
            pass
        self._timer.stop()

    def record(self):
        if self.suspended:
            return False
        from .model import Frame

        try:
            f = Frame.from_canvas(self.canvas)
        except RuntimeError:
            return False
        if not f.is_valid() or f.w <= 0 or f.h <= 0:
            return False
        mupp = self.canvas.mapUnitsPerPixel()
        if 0 <= self.index < len(self.views) and same_view(self.views[self.index], f, self.mupps[self.index], mupp):
            return False
        del self.views[self.index + 1:]
        del self.mupps[self.index + 1:]
        self.views.append(f)
        self.mupps.append(mupp)
        if len(self.views) > self.limit:
            del self.views[:len(self.views) - self.limit]
            del self.mupps[:len(self.mupps) - self.limit]
        self.index = len(self.views) - 1
        return True

    def can_go(self, step):
        self._flush()
        return 0 <= self.index + step < len(self.views)

    def _flush(self):
        """A view the map settled on just now is recorded before moving (no waiting for the timer)."""
        if self._timer.isActive():
            self._timer.stop()
            self.record()

    def step(self, step):
        """The view `step` places back (-1) or forward (+1), and move there in the history; None at the end."""
        self._flush()
        i = self.index + step
        if not 0 <= i < len(self.views):
            return None
        self.index = i
        return self.views[i]

    def peek(self, step):
        i = self.index + step
        return self.views[i] if 0 <= i < len(self.views) else None


def same_view(a, b, mupp_a, mupp_b, tol=0.01):
    """The same place at the same zoom (a window made bigger or smaller shows the same view)."""
    if a.crs != b.crs or abs(a.rot - b.rot) >= 0.01:
        return False
    span = max(min(a.w, a.h), 1e-12)
    zoom_same = mupp_a > 0 and abs(mupp_b / mupp_a - 1) <= tol
    return zoom_same and abs(a.cx - b.cx) <= tol * span and abs(a.cy - b.cy) <= tol * span
