"""What happened when Biruni closed by itself.

A crash of the program's native code (a bad pointer, a broken heap) ends the process at once: no Python
error, no message, nothing on disk. So while Biruni runs:

* ``<settings>/biruni/crash/session.log`` gets the time, the version and every logged message as it comes
  (written through, so the last lines before a crash are there), and Python's ``faulthandler`` writes the
  stack of every thread into the same file when the process is killed by a fatal error;
* ``running`` marks a session in progress; a normal exit removes it.

At the next start a ``running`` mark that is still there means the last session did not end normally: its
log is kept as ``crash-<time>.log`` (the last 10 are kept), and a message offers to open it. Problem reports
include the kept logs. Nothing is sent anywhere.
"""
import datetime as dt
import faulthandler
import os
import shutil

from qgis.core import Qgis, QgsApplication

from .i18n import tr
from .version import APP_NAME, VERSION

KEEP = 10
MAX_LOG = 2 * 2**20     # a session log over this size starts again (a long session with many messages)
_state = {"file": None, "folder": None, "found": None}


def folder():
    return os.path.join(QgsApplication.qgisSettingsDirPath(), "biruni", "crash")


def kept_logs(where=None):
    """Paths of the logs of sessions that did not end normally, newest first."""
    where = where or folder()
    try:
        names = sorted((n for n in os.listdir(where) if n.startswith("crash-") and n.endswith(".log")),
                       reverse=True)
    except OSError:
        return []
    return [os.path.join(where, n) for n in names]


def start(where=None):
    """Begin this session's log; returns the kept log of the last session when it did not end normally."""
    where = where or folder()
    os.makedirs(where, exist_ok=True)
    mark, log = os.path.join(where, "running"), os.path.join(where, "session.log")
    found = None
    if os.path.exists(mark) and os.path.exists(log):
        try:
            stamp = dt.datetime.fromtimestamp(os.path.getmtime(log))
            found = os.path.join(where, f"crash-{stamp:%Y%m%d-%H%M%S}.log")
            shutil.copy2(log, found)
        except OSError:
            found = None
        for old in kept_logs(where)[KEEP:]:
            try:
                os.remove(old)
            except OSError:
                pass
    f = open(log, "w", encoding="utf-8", buffering=1)  # line-buffered: the last lines reach the disk
    f.write(f"{APP_NAME} {VERSION} · engine {Qgis.version()} · started {dt.datetime.now():%Y-%m-%d %H:%M:%S}\n")
    faulthandler.enable(f, all_threads=True)
    with open(mark, "w", encoding="utf-8") as m:
        m.write(str(os.getpid()))
    _state.update(file=f, folder=where, found=found)
    return found


def note(line):
    """Add a line to this session's log."""
    f = _state["file"]
    if f is None:
        return
    try:
        if f.tell() > MAX_LOG:
            f.seek(0)
            f.truncate()
            f.write(f"{APP_NAME} {VERSION} · (log started again: it was long)\n")
        f.write(f"{dt.datetime.now():%H:%M:%S} {line}\n")
    except (OSError, ValueError):
        pass


def stop():
    """A normal exit: the session log stays (it is replaced at the next start) and the mark goes."""
    f = _state["file"]
    if f is None:
        return
    note("ended normally")
    try:
        faulthandler.disable()
    except Exception:  # noqa: BLE001
        pass
    try:
        f.close()
    except OSError:
        pass
    try:
        os.remove(os.path.join(_state["folder"], "running"))
    except OSError:
        pass
    _state["file"] = None


def offer(iface, path):
    """Tell the user that the last session ended by itself, with a button to see what was logged."""
    if iface is None or not path:
        return
    from qgis.PyQt.QtCore import QUrl
    from qgis.PyQt.QtGui import QDesktopServices
    from qgis.PyQt.QtWidgets import QPushButton

    bar = iface.messageBar()
    item = bar.createMessage(APP_NAME, tr("The program closed unexpectedly last time. What happened before it "
                                          "closed was saved; it is added to problem reports."))
    btn = QPushButton(tr("Show the log"))
    btn.clicked.connect(lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(path)))
    item.layout().addWidget(btn)
    bar.pushWidget(item, Qgis.MessageLevel.Warning, 0)
