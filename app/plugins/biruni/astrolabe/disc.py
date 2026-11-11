"""The astrolabe's disc: three rings turned with the mouse, the wheel or the keys, and a hub that runs the
command. The inner ring says on what, the middle one what to do, the outer one how much or with what; what
stands under the pointer at the top is what is chosen. The widget knows nothing about maps - it tells its owner
when a ring's choice changes.
"""
import math

from qgis.PyQt.QtCore import QPointF, QRectF, Qt, QTimer, QVariantAnimation, pyqtSignal
from qgis.PyQt.QtGui import QColor, QFont, QFontMetricsF, QPainter, QPainterPath, QPen, QPolygonF, QRegion
from qgis.PyQt.QtWidgets import QWidget

from ..i18n import tr

BANDS = ((50, 94), (96, 142), (144, 190))   # inner and outer radius of each ring, in the disc's own 400 units
HUB = 46
GHOST = {"light": "#D4491A", "dark": "#FF8552"}
BRASS = {"light": "#9A6E12", "dark": "#DDB25C"}


def norm(a):
    return (a + 180.0) % 360.0 - 180.0


def short(text):
    """A word on the disc has its own translation ("ring|Buffer"): the words of the ribbon are often too long
    for a ring, or say it another way than the astrolabe's sentences. In English it is the word after the bar."""
    return text.split("|", 1)[1] if text.startswith("ring|") else text


def mix(a, b, t):
    a, b = QColor(a), QColor(b)
    return QColor(round(a.red() + (b.red() - a.red()) * t), round(a.green() + (b.green() - a.green()) * t),
                  round(a.blue() + (b.blue() - a.blue()) * t))


class Ring:
    """A ring of choices (``items``: [(key, label)]) or a graduated ring for a number."""

    def __init__(self, items=None, key=None, dim=False, **scale):
        self.kind = "scale" if scale else "choice"
        self.items = list(items or [])
        self.index = 0
        self.dim = dim
        self.angle = 0.0
        if scale:
            self.min, self.max, self.step = scale["min"], scale["max"], scale.get("step", 1)
            self.k = scale["k"]                      # degrees of turn for one unit
            self.minor, self.major = scale.get("minor", 1), scale.get("major", 10)
            self.unit = scale.get("unit", "")
            self.fine = scale.get("fine", False)     # Shift: tenths of a step
            self.value = scale.get("value", self.min)
            self.angle = (self.value - self.min) * self.k
        elif key is not None:
            self.set_key(key)

    @property
    def key(self):
        return self.items[self.index][0] if self.items and self.kind == "choice" else None

    def set_key(self, key):
        i = next((n for n, it in enumerate(self.items) if it[0] == key), 0)
        self.index = i
        self.angle = -i * 360.0 / max(1, len(self.items))

    def set_items(self, items):
        key = self.key
        self.items = list(items)
        self.set_key(key)

    def rest_angle(self):
        """Where the ring stands when it is let go."""
        if self.kind == "choice":
            return self.angle + norm(-self.index * 360.0 / max(1, len(self.items)) - self.angle)
        return (self.value - self.min) * self.k

    def snap(self, v, q=None):
        q = q or self.step
        return min(self.max, max(self.min, round(round(v / q) * q, 2)))


class Disc(QWidget):
    changed = pyqtSignal(int)      # a ring's choice or value changed (0, 1, 2)
    run = pyqtSignal()             # the hub was pressed / Enter
    then = pyqtSignal()            # Shift+Enter: another step
    back = pyqtSignal()            # Backspace: the step before
    closed = pyqtSignal()          # Esc
    dragged = pyqtSignal(int, int)  # the disc was dragged by its hub: where its corner should go (parent coords)

    def __init__(self, parent=None, size=380):
        super().__init__(parent)
        self.rings = [Ring([("-", "")]), Ring([("-", "")]), Ring([("-", "")])]
        self.focus_ring = 2
        self.dark = False
        self.tokens = {"text": "#1E252B", "text_dim": "#5B6670", "accent": "#0E7C74", "accent_text": "#FFFFFF",
                       "panel": "#FFFFFF", "border": "#DDE2E7"}
        self._drag = None
        self._anims = {}
        self._typed = ""
        self._typed_timer = QTimer(self)
        self._typed_timer.setSingleShot(True)
        self._typed_timer.setInterval(1800)
        self._typed_timer.timeout.connect(self._forget_typed)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setCursor(Qt.CursorShape.OpenHandCursor)
        self.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground, True)
        self.set_size(size)

    # ---- set up ------------------------------------------------------------------------------------------------
    def set_size(self, size):
        self.setFixedSize(int(size), int(size))
        self.setMask(QRegion(0, 0, int(size), int(size), QRegion.RegionType.Ellipse))

    def set_theme(self, tokens, dark):
        self.tokens, self.dark = dict(tokens), bool(dark)
        self.update()

    def set_ring(self, z, ring):
        a = self._anims.pop(z, None)
        if a is not None:
            a.stop()
        self.rings[z] = ring
        self.update()

    # ---- changing a ring ---------------------------------------------------------------------------------------
    def _settle(self, z):
        ring = self.rings[z]
        to = ring.rest_angle()
        old = self._anims.pop(z, None)
        if old is not None:
            old.stop()
        if abs(to - ring.angle) < .01 or not self.isVisible():
            ring.angle = to
            self.update()
            return
        a = QVariantAnimation(self)
        a.setDuration(190)
        a.setStartValue(float(ring.angle))
        a.setEndValue(float(to))

        def step(v, ring=ring):
            ring.angle = float(v)
            self.update()
        a.valueChanged.connect(step)
        self._anims[z] = a
        a.start()

    def set_index(self, z, i):
        ring = self.rings[z]
        if ring.kind != "choice" or ring.dim or not ring.items:
            return
        j = i % len(ring.items)
        if j != ring.index:
            ring.index = j
            self.changed.emit(z)
        self._settle(z)

    def set_value(self, v, q=None, z=2):
        ring = self.rings[z]
        if ring.kind != "scale":
            return
        v = ring.snap(v, q)
        if v != ring.value:
            ring.value = v
            self.changed.emit(z)
        self._settle(z)

    def step_ring(self, z, direction, fine=False):
        ring = self.rings[z]
        if ring.kind == "choice":
            self.set_index(z, ring.index + direction)
        else:
            q = ring.step / 10.0 if fine and ring.fine else ring.step
            self.set_value(ring.value + direction * q, q, z)

    def _rotate_by(self, z, d, fine):
        ring = self.rings[z]
        if ring.dim:
            return
        a = self._anims.pop(z, None)
        if a is not None:
            a.stop()
        if ring.kind == "choice":
            n = len(ring.items)
            ring.angle += d
            i = round(-ring.angle / (360.0 / n)) % n
            if i != ring.index:
                ring.index = i
                self.changed.emit(z)
        else:
            fine = fine and ring.fine
            ring.angle = min((ring.max - ring.min) * ring.k, max(0.0, ring.angle + d * (.1 if fine else 1)))
            v = ring.snap(ring.min + ring.angle / ring.k, ring.step / 10.0 if fine else ring.step)
            if v != ring.value:
                ring.value = v
                self.changed.emit(z)
        self.update()

    def _click_ring(self, z, th):
        ring = self.rings[z]
        if ring.kind == "choice":
            self.set_index(z, round(norm(th - ring.angle) / (360.0 / max(1, len(ring.items)))))
        else:
            self.set_value(ring.value - norm(th) / ring.k, None, z)

    # ---- pointer -----------------------------------------------------------------------------------------------
    def _polar(self, pos):
        k = 400.0 / self.width()
        x, y = pos.x() * k - 200, pos.y() * k - 200
        return math.hypot(x, y), math.degrees(math.atan2(x, -y))

    @staticmethod
    def zone(r):
        if r < HUB:
            return -1
        for z, (a, b) in enumerate(BANDS):
            if a - 2 <= r <= b + 2:
                return z
        return None

    def mousePressEvent(self, e):  # noqa: N802
        if e.button() != Qt.MouseButton.LeftButton:
            e.accept()
            return
        r, th = self._polar(e.position())
        z = self.zone(r)
        self.setFocus()
        if z is None:
            return
        g = e.globalPosition().toPoint()
        self._drag = {"z": z, "last": th, "start": th, "moved": 0.0, "g": g, "at": self.pos(), "moving": False}
        if z >= 0:
            self.focus_ring = z
            self.setCursor(Qt.CursorShape.ClosedHandCursor)
            self.update()

    def mouseMoveEvent(self, e):  # noqa: N802
        d = self._drag
        if d is None:
            return
        if d["z"] == -1:
            delta = e.globalPosition().toPoint() - d["g"]
            if d["moving"] or delta.manhattanLength() > 5:
                d["moving"] = True
                self.dragged.emit(d["at"].x() + delta.x(), d["at"].y() + delta.y())
            return
        _r, th = self._polar(e.position())
        step = norm(th - d["last"])
        d["last"] = th
        d["moved"] += abs(step)
        if d["moved"] >= 2:
            self._rotate_by(d["z"], step, bool(e.modifiers() & Qt.KeyboardModifier.ShiftModifier))

    def mouseReleaseEvent(self, e):  # noqa: N802
        d, self._drag = self._drag, None
        self.setCursor(Qt.CursorShape.OpenHandCursor)
        if d is None:
            return
        if d["z"] == -1:
            if not d["moving"]:
                self.run.emit()
            return
        if d["moved"] < 2:
            self._click_ring(d["z"], d["start"])
        else:
            self._settle(d["z"])

    def wheelEvent(self, e):  # noqa: N802
        r, _th = self._polar(e.position())
        z = self.zone(r)
        e.accept()
        if z is None or z < 0 or not e.angleDelta().y():
            return
        self.focus_ring = z
        self.step_ring(z, 1 if e.angleDelta().y() < 0 else -1, bool(e.modifiers() & Qt.KeyboardModifier.ShiftModifier))
        self.update()

    def contextMenuEvent(self, e):  # noqa: N802
        e.accept()

    # ---- keys --------------------------------------------------------------------------------------------------
    def _forget_typed(self):
        self._typed = ""

    def keyPressEvent(self, e):  # noqa: N802
        k, shift = e.key(), bool(e.modifiers() & Qt.KeyboardModifier.ShiftModifier)
        ring = self.rings[self.focus_ring]
        if k == Qt.Key.Key_Escape:
            self.closed.emit()
        elif k in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            (self.then if shift else self.run).emit()
        elif k == Qt.Key.Key_Up:
            self.focus_ring = min(2, self.focus_ring + 1)
        elif k == Qt.Key.Key_Down:
            self.focus_ring = max(0, self.focus_ring - 1)
        elif k in (Qt.Key.Key_Left, Qt.Key.Key_Right):
            # forward is the way the language reads: the next choice, a larger number
            forward = (k == Qt.Key.Key_Right) == (self.layoutDirection() == Qt.LayoutDirection.LeftToRight)
            self.step_ring(self.focus_ring, 1 if forward else -1, shift)
        elif k == Qt.Key.Key_Backspace and self._typed:
            self._typed = self._typed[:-1]
            self._apply_typed()
        elif k == Qt.Key.Key_Backspace:
            self.back.emit()
        elif e.text() and e.text() in "0123456789.-" and self.rings[2].kind == "scale":
            # a number typed at any moment goes to the outer ring: the exact value, not the nearest tick
            self._typed += e.text()
            self._apply_typed()
        else:
            super().keyPressEvent(e)
            return
        self.update()
        e.accept()

    def _apply_typed(self):
        self._typed_timer.start()
        try:
            v = float(self._typed)
        except ValueError:
            return
        ring = self.rings[2]
        self.focus_ring = 2
        self.set_value(v, .01 if ring.fine else ring.step)

    # ---- painting ----------------------------------------------------------------------------------------------
    def colours(self):
        t, mode = self.tokens, "dark" if self.dark else "light"
        return {"plate": QColor(t["panel"]), "line": QColor(t["border"]), "ink": QColor(t["text"]),
                "dim": QColor(t["text_dim"]), "accent": QColor(t["accent"]), "on_accent": QColor(t["accent_text"]),
                "brass": QColor(BRASS[mode]),
                "bands": (mix(t["panel"], t["accent"], .16), mix(t["panel"], t["text"], .05),
                          mix(t["panel"], BRASS[mode], .2))}

    @staticmethod
    def _pt(r, deg):
        a = math.radians(deg)
        return QPointF(r * math.sin(a), -r * math.cos(a))

    def paintEvent(self, _e):  # noqa: N802
        c = self.colours()
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        p.setRenderHint(QPainter.RenderHint.TextAntialiasing, True)
        s = self.width() / 400.0
        p.translate(self.width() / 2.0, self.height() / 2.0)
        p.scale(s, s)
        # the plate and its degrees
        p.setPen(QPen(c["line"], 1))
        p.setBrush(c["plate"])
        p.drawEllipse(QPointF(0, 0), 199, 199)
        p.setPen(QPen(c["dim"], 1))
        for d in range(0, 360, 5):
            p.drawLine(self._pt(192, d), self._pt(196 if d % 30 else 199, d))
        # the three bands
        p.setBrush(Qt.BrushStyle.NoBrush)
        for z, (a, b) in enumerate(BANDS):
            p.setPen(QPen(c["bands"][z], b - a, Qt.PenStyle.SolidLine, Qt.PenCapStyle.FlatCap))
            p.drawEllipse(QPointF(0, 0), (a + b) / 2.0, (a + b) / 2.0)
        # what is under the pointer
        wedge = QPainterPath()
        wedge.moveTo(self._pt(50, -15))
        wedge.lineTo(self._pt(190, -15))
        for d in range(-15, 16, 3):
            wedge.lineTo(self._pt(190, d))
        wedge.lineTo(self._pt(50, 15))
        for d in range(15, -16, -3):
            wedge.lineTo(self._pt(50, d))
        wedge.closeSubpath()
        tint = QColor(c["accent"])
        tint.setAlphaF(.14)
        p.fillPath(wedge, tint)
        for z in range(3):
            self._paint_ring(p, z, c)
        # the index of the outer ring and the pointer
        p.setPen(QPen(c["brass"], 2))
        p.drawLine(QPointF(0, -173), QPointF(0, -192))
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(c["brass"])
        p.drawPolygon(QPolygonF([QPointF(-7, -200), QPointF(7, -200), QPointF(0, -188)]))
        # the ring the keys turn
        if self.hasFocus():
            a, b = BANDS[self.focus_ring]
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.setPen(QPen(c["accent"], 1.6))
            p.drawEllipse(QPointF(0, 0), a, a)
            p.drawEllipse(QPointF(0, 0), b, b)
        # the hub
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(c["accent"])
        p.drawEllipse(QPointF(0, 0), 44, 44)
        f = QFont(self.font())
        f.setPixelSize(19)
        f.setBold(True)
        p.setFont(f)
        p.setPen(c["on_accent"])
        p.drawText(QRectF(-44, -22, 88, 34), Qt.AlignmentFlag.AlignCenter, short(tr("ring|Run")))
        f.setPixelSize(10)
        f.setBold(False)
        p.setFont(f)
        p.setOpacity(.75)
        p.drawText(QRectF(-44, 12, 88, 16), Qt.AlignmentFlag.AlignCenter, "Enter")
        p.end()

    def _paint_ring(self, p, z, c):
        ring = self.rings[z]
        a, b = BANDS[z]
        p.save()
        if ring.dim:
            p.setOpacity(.4)
        p.rotate(ring.angle)
        f = QFont(self.font())
        if ring.kind == "choice":
            n = max(1, len(ring.items))
            step = 360.0 / n
            mid = (a + b) / 2.0
            room = min(118.0, 2 * math.pi * mid / n - 10)
            if n > 1:
                p.setPen(QPen(c["line"], 1))
                for i in range(n):
                    p.drawLine(self._pt(a + 5, (i + .5) * step), self._pt(b - 5, (i + .5) * step))
            for i, (_key, label) in enumerate(ring.items):
                on = i == ring.index
                size = 14 if on else 13 if room > 62 else 12
                f.setBold(on)
                f.setPixelSize(size)
                while size > 10 and QFontMetricsF(f).horizontalAdvance(label) > room:   # a long word: smaller first
                    size -= 1
                    f.setPixelSize(size)
                p.setFont(f)
                p.setPen(c["ink"] if on else c["dim"])
                text = QFontMetricsF(f).elidedText(label, Qt.TextElideMode.ElideRight, room)
                self._label(p, text, i * step, mid, ring.angle)
        else:
            count = int(round((ring.max - ring.min) / ring.minor))
            full = (ring.max - ring.min) * ring.k >= 360
            f.setPixelSize(11)
            p.setFont(f)
            for i in range(count + 1):
                if full and i == count:
                    break
                v = ring.min + i * ring.minor
                at = -(v - ring.min) * ring.k
                major = abs(v / ring.major - round(v / ring.major)) < 1e-9
                p.setPen(QPen(c["ink"] if major else c["dim"], 1.4 if major else 1))
                p.drawLine(self._pt(176 if major else 182, at), self._pt(189, at))
                if major:
                    p.setPen(c["dim"])
                    self._label(p, f"{v:g}", at, 162, ring.angle)
        p.restore()

    @staticmethod
    def _label(p, text, at, r, ring_angle):
        p.save()
        p.rotate(at)
        p.translate(0, -r)
        if abs(norm(at + ring_angle)) > 90:   # never upside down
            p.rotate(180)
        p.drawText(QRectF(-70, -12, 140, 24), Qt.AlignmentFlag.AlignCenter, text)
        p.restore()
