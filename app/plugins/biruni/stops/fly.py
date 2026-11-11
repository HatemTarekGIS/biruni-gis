"""A short, smooth flight from the current view to another: the centre glides, the zoom changes evenly (log scale),
the map zooms out a little on the way when the places are far apart (like a camera), and the rotation turns the
short way. Only the last frame waits for every layer to draw; the in-between frames are what the map can draw in
the time (QGIS cancels a drawing that a newer view replaces)."""
import math

from qgis.PyQt.QtCore import QEasingCurve, QVariantAnimation

from .model import apply_frame

_ACTIVE = {}  # id(canvas) -> animation (one flight at a time per map)


def stop_flight(canvas, land=True):
    """Stop a flight: at its destination (land) or where it is now (a new flight starts from here)."""
    anim = _ACTIVE.pop(id(canvas), None)
    if anim is not None:
        anim.stop()
        done = getattr(anim, "_land" if land else "_abort", None)
        if done:
            done()


def fly(canvas, centre, mupp, rot, duration_ms=700, on_done=None, history=None):
    """Fly the canvas to (centre, map units per pixel, rotation). duration_ms <= 0 jumps."""
    stop_flight(canvas, land=False)
    if history is not None:
        history.suspended += 1

    landed = []

    def release():
        if landed:
            return False
        landed.append(True)
        if history is not None:
            history.suspended -= 1
        return True

    def land():
        if not release():
            return
        apply_frame(canvas, centre, mupp, rot)
        if history is not None:
            history.record()
        if on_done is not None:
            on_done()

    c0 = canvas.center()
    m0 = canvas.mapUnitsPerPixel()
    r0 = canvas.rotation()
    if duration_ms <= 0 or m0 <= 0 or mupp <= 0 or not all(math.isfinite(v) for v in (c0.x(), c0.y(), m0)):
        land()
        return None
    size = canvas.mapSettings().outputSize()
    span0 = m0 * max(size.width(), size.height(), 1)
    dist = math.hypot(centre.x() - c0.x(), centre.y() - c0.y())
    # very far (another continent at street level): a long flight would only show a blur - shorten it
    far = dist / max(span0, mupp * max(size.width(), size.height(), 1))
    if far > 400:
        duration_ms = int(duration_ms * 0.6)
    lz0, lz1 = math.log(m0), math.log(mupp)
    # zoom out on the way so both places are briefly in view (at most ×8)
    need = math.log(max(1.0, min(8.0, far / 1.5))) if far > 1.5 else 0.0
    dr = (rot - r0 + 180.0) % 360.0 - 180.0

    anim = QVariantAnimation(canvas)
    anim.setStartValue(0.0)
    anim.setEndValue(1.0)
    anim.setDuration(int(duration_ms))
    anim.setEasingCurve(QEasingCurve(QEasingCurve.Type.InOutCubic))

    from qgis.core import QgsPointXY

    def step(t):
        t = float(t)
        if t >= 1.0:
            return
        x = c0.x() + (centre.x() - c0.x()) * t
        y = c0.y() + (centre.y() - c0.y()) * t
        lz = lz0 + (lz1 - lz0) * t + need * math.sin(math.pi * t)
        apply_frame(canvas, QgsPointXY(x, y), math.exp(lz), r0 + dr * t)

    def finished():
        if _ACTIVE.get(id(canvas)) is anim:
            _ACTIVE.pop(id(canvas), None)
        land()

    anim.valueChanged.connect(step)
    anim.finished.connect(finished)
    anim._land = land  # noqa: SLF001 - stop_flight() lands at once
    anim._abort = release  # noqa: SLF001 - or leaves the map where it is
    _ACTIVE[id(canvas)] = anim
    anim.start()
    return anim


def shutdown(plugin):
    try:
        stop_flight(plugin.iface.mapCanvas())
    except (AttributeError, RuntimeError):
        pass
