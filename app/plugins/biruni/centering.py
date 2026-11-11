"""Windows open in the middle of the program, not in a corner.

Every dialog of Biruni opens centred on the main window the first time it is shown. Any other window
(from the engine or another plugin) is left where it remembers being, unless it would open in the
screen's top-left corner, outside every screen, or bigger than the screen - then it is brought back,
centred and fitted. It listens only to "a window got the focus" (an application-wide event filter would
run Python for every mouse move and repaint).
"""
from qgis.PyQt.QtCore import QRect, QTimer
from qgis.PyQt.QtGui import QGuiApplication
from qgis.PyQt.QtWidgets import QApplication, QDialog, QDockWidget

_DONE = "biruni_centred"
_state = {"filter": None}


def _ours(w):
    return type(w).__module__.startswith(("biruni.", "biruni_"))


def _badly_placed(geo, screens):
    if not any(s.intersects(geo) and s.intersected(geo).width() * s.intersected(geo).height()
               > 0.6 * geo.width() * geo.height() for s in screens):
        return True  # mostly off every screen
    return any(abs(geo.left() - s.left()) < 24 and abs(geo.top() - s.top()) < 24 for s in screens)  # the corner


def centre(w, main):
    """Centre w on the main window (on the main window's screen), shrinking it to fit if needed."""
    screen = (main.screen() if main is not None else None) or QApplication.primaryScreen()
    avail = screen.availableGeometry()
    frame = w.frameGeometry()
    width = min(frame.width(), avail.width() - 40)
    height = min(frame.height(), avail.height() - 40)
    if width < frame.width() or height < frame.height():
        w.resize(max(200, width - (frame.width() - w.width())), max(150, height - (frame.height() - w.height())))
        frame = w.frameGeometry()
    target = main.frameGeometry() if main is not None and main.isVisible() else avail
    box = QRect(0, 0, frame.width(), frame.height())
    box.moveCenter(target.center())
    # keep it on the screen
    box.moveLeft(max(avail.left(), min(box.left(), avail.right() - box.width())))
    box.moveTop(max(avail.top(), min(box.top(), avail.bottom() - box.height())))
    w.move(box.topLeft())


def _on_focus_window(win, main):
    if win is None:
        return
    for w in QApplication.topLevelWidgets():
        if w.windowHandle() is not win or w is main or w.property(_DONE):
            continue
        if not isinstance(w, (QDialog, QDockWidget)) or (isinstance(w, QDockWidget) and not w.isFloating()):
            return
        w.setProperty(_DONE, True)
        # after Qt and the window itself have placed it (some restore their saved place when shown)
        QTimer.singleShot(0, lambda w=w: _place(w, main))
        return


def _place(w, main):
    try:
        if not w.isVisible():
            return
        screens = [s.availableGeometry() for s in QApplication.screens()]
        if _ours(w) or _badly_placed(w.frameGeometry(), screens):
            centre(w, main)
    except RuntimeError:  # closed meanwhile
        pass


def install(iface):
    main = iface.mainWindow()

    def slot(win):
        _on_focus_window(win, main)

    QGuiApplication.instance().focusWindowChanged.connect(slot)
    _state["slot"] = slot
    return uninstall


def uninstall():
    slot = _state.pop("slot", None)
    if slot is not None:
        try:
            QGuiApplication.instance().focusWindowChanged.disconnect(slot)
        except TypeError:
            pass
