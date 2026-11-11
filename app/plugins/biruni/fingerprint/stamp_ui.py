"""The buttons of the dates: stamp a certificate (or any proof file), and check when it existed."""
import os

from qgis.core import Qgis
from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtWidgets import QApplication, QFileDialog, QMessageBox

from ..i18n import tr
from . import stamp as st
from .ui import PROOF, _bar, _dir, _remember

FILES = f"*{PROOF} *.biruni-area.json *.biruni-totals.json *.json *.gpkg"


def _busy(fn, *a, **k):
    QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
    try:
        return fn(*a, **k)
    finally:
        QApplication.restoreOverrideCursor()


def stamp_ui(plugin, path=None, ask=True, fetch=st.http):
    mw = plugin.iface.mainWindow()
    if path is None:
        path, _f = QFileDialog.getOpenFileName(mw, tr("Stamp the Date"), _dir(), tr("Certificate or proof") + f" ({FILES})")
        if not path:
            return None
    _remember(path)
    if ask and QMessageBox.question(
            mw, tr("Stamp the Date"),
            tr("Only a fingerprint of the file (32 bytes, from which nothing of it can be read) is sent to the free "
               "public OpenTimestamps calendars. Within a few hours it is in a Bitcoin block, and then the file is "
               "proved to have existed before that block's time, without trusting anyone. Send it?")) \
            != QMessageBox.StandardButton.Yes:
        return None
    try:
        took = _busy(st.stamp, path, fetch=fetch)
    except st.StampError:
        _bar(plugin, tr("No calendar could be reached: check the internet connection and try again."),
             Qgis.MessageLevel.Warning, 10)
        return None
    except OSError as e:
        _bar(plugin, str(e), Qgis.MessageLevel.Warning, 10)
        return None
    text = tr("Stamped by {n} calendars: «{f}» beside the file. In a few hours «Check the Date» completes it with "
              "the Bitcoin block; keep the two files together.").format(n=took, f=os.path.basename(path) + st.SUFFIX)
    _bar(plugin, text, seconds=0)
    return took


def check_ui(plugin, path=None, quiet=False, fetch=st.http):
    mw = plugin.iface.mainWindow()
    if path is None:
        path, _f = QFileDialog.getOpenFileName(mw, tr("Check the Date"), _dir(),
                                               tr("Stamp") + f" (*{st.SUFFIX});;" + tr("Certificate or proof") + f" ({FILES})")
        if not path:
            return None
    if path.endswith(st.SUFFIX):
        path = path[:-len(st.SUFFIX)]
    _remember(path)
    if not st.has_stamp(path):
        text = tr("This file has no stamp beside it («{f}»).").format(f=os.path.basename(path) + st.SUFFIX)
        r = st.Result()
        r.why = "none"
    else:
        r = _busy(st.verify, path, fetch=fetch)
        name = os.path.basename(path)
        if r.ok:
            text = tr("Proved: «{f}» existed before {t} (Bitcoin block {h}). Nothing in it changed since.").format(
                f=name, t=r.time.astimezone().strftime("%Y-%m-%d %H:%M"), h=r.height)
            if r.upgraded:
                text += " " + tr("The stamp was completed now and saved.")
        else:
            text = {
                "pending": tr("Not in a block yet: the calendars take a few hours. Check again later."),
                "offline": tr("The stamp is complete (block {h}), but the block could not be fetched to check it: "
                              "check the internet connection.").format(h=r.height),
                "other file": tr("The file is not the one stamped: it changed after the stamp."),
                "block": tr("The stamp does not lead to its block: it is not a true stamp."),
            }.get(r.why, tr("This is not a stamp file."))
    if not quiet:
        (QMessageBox.information if r.ok else QMessageBox.warning)(mw, tr("Check the Date"), text)
    r.text = text
    return r
