"""Staying in touch with users: update notices and problem reports.

* Updates: at start and every few hours Biruni reads the signed list of its newest code (update/client.py).
  A newer version is built in the user's folder from the files that changed only, and runs from the next start;
  the message bar says so, with a button to restart now. A version that needs a newer engine is the whole
  program again: then a Download button opens the release page, as before.
* Problem reports: a ZIP with what is needed to reproduce a problem - version,
  system, settings that matter, the recent message log and Python errors, and
  a description of the open project (layer names and formats, never the data).
"""
import datetime as dt
import importlib
import json
import os
import platform
import re
import threading
import time
import zipfile
from collections import deque
from urllib.parse import quote

from qgis.core import Qgis, QgsApplication, QgsNetworkAccessManager, QgsProject, QgsSettings
from qgis.PyQt.QtCore import QObject, QProcess, QTimer, QUrl, pyqtSignal
from qgis.PyQt.QtGui import QDesktopServices
from qgis.PyQt.QtNetwork import QNetworkReply, QNetworkRequest
from qgis.PyQt.QtWidgets import (QDialog, QDialogButtonBox, QLabel, QMessageBox, QPlainTextEdit, QPushButton,
                                 QVBoxLayout)

from . import crashlog, i18n
from .i18n import tr
from .version import APP_NAME, HOMEPAGE, REPO, VERSION
from .version import UPDATE_KEY, UPDATE_URL

CHECK_KEY = "biruni/check-updates"
LAST_CHECK_KEY = "biruni/last-update-check"
SKIP_KEY = "biruni/skip-version"
CHECK_EVERY_S = 6 * 3600     # how often the list of updates is read while Biruni stays open


def parse_version(text):
    """'v0.12.3-beta' -> (0, 12, 3); unknown parts are 0."""
    nums = [int(n) for n in re.findall(r"\d+", text or "")[:3]]
    return tuple(nums + [0] * (3 - len(nums)))


def is_newer(remote, local=VERSION):
    return parse_version(remote) > parse_version(local)


class Support(QObject):
    updateEvent = pyqtSignal(object)   # from the update thread to the window

    def __init__(self, plugin):
        super().__init__()
        self.plugin = plugin
        self.iface = plugin.iface
        self.log = deque(maxlen=800)
        self._reply = None
        self._updating = False
        self._ready_shown = None
        self.updateEvent.connect(self._on_update)
        self._timer = QTimer(self)
        self._timer.setInterval(CHECK_EVERY_S * 1000)
        self._timer.timeout.connect(lambda: self.check(manual=False))

    @staticmethod
    def _signal():
        """The engine emits messageReceivedWithFormat for logged messages; the three-argument
        messageReceived stays silent (the report's log was always empty)."""
        log = QgsApplication.messageLog()
        if hasattr(log, "messageReceivedWithFormat"):
            return log.messageReceivedWithFormat
        return log.messageReceived[str, str, Qgis.MessageLevel]

    def install(self):
        self._signal().connect(self._on_message)
        # a copy that an update started on trial: it is kept once Biruni has run a while
        QTimer.singleShot(20000, self._healthy)

    @staticmethod
    def _healthy():
        try:
            from .update import boot

            boot.healthy()
        except Exception:  # noqa: BLE001
            pass

    def unload(self):
        try:
            self._signal().disconnect(self._on_message)
        except TypeError:
            pass

    def _on_message(self, message, tag, level, *_format):
        stamp = dt.datetime.now().strftime("%H:%M:%S")
        names = {Qgis.MessageLevel.Info: "info", Qgis.MessageLevel.Warning: "warning",
                 Qgis.MessageLevel.Critical: "critical", Qgis.MessageLevel.Success: "success"}
        line = f"[{tag or '-'}] {names.get(level, '?')}: {message}"
        self.log.append(f"{stamp} {line}")
        crashlog.note(line)

    # ---- updates ---------------------------------------------------------
    def auto_check(self):
        s = QgsSettings()
        if not UPDATE_URL or not s.value(CHECK_KEY, True, type=bool):
            return
        self._timer.start()
        try:
            last = float(s.value(LAST_CHECK_KEY, 0) or 0)
        except (TypeError, ValueError):
            last = 0
        if time.time() - last < 600:
            return
        s.setValue(LAST_CHECK_KEY, time.time())
        self.check(manual=False)

    def check(self, manual=True):
        """Read the list of updates in the background; a newer version is built from the files that changed."""
        if UPDATE_URL and UPDATE_KEY:
            if self._updating:
                if manual:
                    self.iface.messageBar().pushInfo(APP_NAME, tr("An update is being prepared…"))
                return
            self._updating = True
            threading.Thread(target=self._update_work, args=(manual,), daemon=True).start()
            return
        self.check_release(manual)

    @staticmethod
    def running_folder():
        """The folder of the copy of Biruni that runs (the installed one, or an update in the user's folder)."""
        return importlib.import_module(__package__).__path__[0]

    def _update_work(self, manual):
        from .update import client

        try:
            m = client.check(UPDATE_URL, UPDATE_KEY, VERSION, Qgis.version())
            if m is None:
                ev = {"kind": "none"}
            elif m.get("installer"):
                ev = {"kind": "installer", "manifest": m}
            elif client.is_bad(m["version"]) and not manual:
                ev = {"kind": "bad", "manifest": m}
            elif client.staged_version() == m["version"]:
                ev = {"kind": "ready", "manifest": m}
            else:
                r = client.stage(m, UPDATE_URL, self.running_folder())
                ev = {"kind": "ready", "manifest": m, "result": r}
        except Exception as e:  # noqa: BLE001 - no network, the list refused, the disk full
            ev = {"kind": "error", "error": str(e)[:200]}
        ev["manual"] = manual
        self.updateEvent.emit(ev)

    def _on_update(self, ev):
        self._updating = False
        bar, manual = self.iface.messageBar(), ev.get("manual")
        kind, m = ev["kind"], ev.get("manifest") or {}
        if kind == "none":
            if manual:
                bar.pushSuccess(APP_NAME, tr("{app} is up to date ({v}).").format(app=APP_NAME, v=VERSION))
        elif kind == "error":
            if manual:
                bar.pushWarning(APP_NAME, tr("Could not check for updates.") + " " + ev["error"])
        elif kind == "installer":
            self.check_release(manual)
        elif kind == "bad":
            pass   # it did not start well here: not offered again by itself
        elif kind == "ready" and self._ready_shown != m.get("version"):
            self._ready_shown = m.get("version")
            notes = m.get("notes") or {}
            note = notes.get(i18n.lang()) or notes.get("en") or ""
            text = tr("Biruni {v} is ready: it opens with the next start.").format(v=m.get("version"))
            r = ev.get("result")
            if r:
                text += " " + tr("{n} files changed ({kb} KB downloaded).").format(
                    n=r["downloaded"], kb=max(1, round(r["bytes"] / 1024)))
            if note:
                text += " " + note
            item = bar.createMessage(APP_NAME, text)
            if os.environ.get("BIRUNI_LAUNCHER"):
                b = QPushButton(tr("Restart now"))
                b.clicked.connect(lambda: (bar.popWidget(item), self.restart()))
                item.layout().addWidget(b)
            bar.pushWidget(item, Qgis.MessageLevel.Success, 0)

    def restart(self):
        """Close Biruni (it asks to save as always) and open it again: the update runs. Nothing opens again if
        closing is cancelled."""
        launcher = os.environ.get("BIRUNI_LAUNCHER")
        if not launcher or not os.path.isfile(launcher):
            return False
        QgsApplication.instance().aboutToQuit.connect(lambda: QProcess.startDetached(launcher, []))
        self.iface.actionExit().trigger()
        return True

    def check_release(self, manual=True):
        """A version that needs the whole program again: the release page on GitHub."""
        if not REPO:
            if manual:
                QMessageBox.information(self.iface.mainWindow(), APP_NAME,
                                        tr("You are using {app} {v}.").format(app=APP_NAME, v=VERSION))
            return
        req = QNetworkRequest(QUrl(f"https://api.github.com/repos/{REPO}/releases/latest"))
        req.setRawHeader(b"Accept", b"application/vnd.github+json")
        req.setRawHeader(b"User-Agent", f"BiruniGIS/{VERSION}".encode())
        self._reply = QgsNetworkAccessManager.instance().get(req)
        self._reply.finished.connect(lambda r=self._reply, m=manual: self._on_release(r, m))

    def _on_release(self, reply, manual):
        try:
            if reply.error() != QNetworkReply.NetworkError.NoError:
                if manual:
                    self.iface.messageBar().pushWarning(APP_NAME, tr("Could not check for updates."))
                return
            info = json.loads(bytes(reply.readAll()).decode("utf-8"))
        except ValueError:
            return
        finally:
            reply.deleteLater()
        self.handle_release(info, manual)

    def handle_release(self, info, manual=True):
        tag = info.get("tag_name") or ""
        url = info.get("html_url") or HOMEPAGE
        if not is_newer(tag):
            if manual:
                self.iface.messageBar().pushSuccess(APP_NAME, tr("{app} is up to date ({v}).").format(
                    app=APP_NAME, v=VERSION))
            return False
        if not manual and QgsSettings().value(SKIP_KEY, "") == tag:
            return False
        bar = self.iface.messageBar()
        item = bar.createMessage(APP_NAME, tr("A new version is available: {v}").format(v=tag.lstrip("v")))
        get = QPushButton(tr("Download"))
        get.clicked.connect(lambda: (QDesktopServices.openUrl(QUrl(url)), bar.popWidget(item)))
        skip = QPushButton(tr("Skip this version"))
        skip.clicked.connect(lambda: (QgsSettings().setValue(SKIP_KEY, tag), bar.popWidget(item)))
        item.layout().addWidget(get)
        item.layout().addWidget(skip)
        bar.pushWidget(item, Qgis.MessageLevel.Info, 0)
        return True

    # ---- problem reports ---------------------------------------------------
    def system_info(self):
        prj = QgsProject.instance()
        import qgis.utils

        return {
            "app": f"{APP_NAME} {VERSION}",
            "engine": f"QGIS {Qgis.version()}",
            "python": platform.python_version(),
            "os": f"{platform.system()} {platform.release()} ({platform.version()})",
            "machine": platform.machine(),
            "language": i18n.current_code(),
            "theme": QgsSettings().value("biruni/theme", "system"),
            "plugins": sorted(qgis.utils.active_plugins),
            "project_saved": bool(prj.fileName()),
            "project_crs": prj.crs().authid(),
            "layers": [{"name": lyr.name(), "provider": lyr.providerType(), "valid": lyr.isValid(),
                        "crs": lyr.crs().authid(),
                        "features": lyr.featureCount() if hasattr(lyr, "featureCount") else None}
                       for lyr in prj.mapLayers().values()],
        }

    def write_report(self, path, description=""):
        info = self.system_info()
        with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
            z.writestr("description.txt", description or "")
            z.writestr("system.json", json.dumps(info, ensure_ascii=False, indent=1))
            z.writestr("messages.log", "\n".join(self.log))
            for p in crashlog.kept_logs()[:3]:  # the sessions that closed by themselves
                try:
                    z.write(p, "crashes/" + os.path.basename(p))
                except OSError:
                    pass
        return path

    def report_problem(self):
        dlg = ReportDialog(self.iface.mainWindow())
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return None
        desktop = os.path.join(os.path.expanduser("~"), "Desktop")
        folder = desktop if os.path.isdir(desktop) else os.path.expanduser("~")
        path = os.path.join(folder, f"Biruni-GIS-report-{dt.datetime.now():%Y%m%d-%H%M}.zip")
        self.write_report(path, dlg.text())
        QDesktopServices.openUrl(QUrl.fromLocalFile(folder))
        if REPO:
            body = (f"{dlg.text()}\n\n---\n{APP_NAME} {VERSION} · QGIS {Qgis.version()} · {platform.system()} "
                    f"{platform.release()}\n\n" + tr("Please attach the report file: {f}").format(
                        f=os.path.basename(path)))
            title = (dlg.text().splitlines() or ["Problem report"])[0][:80]
            QDesktopServices.openUrl(QUrl(f"https://github.com/{REPO}/issues/new?title={quote(title)}"
                                          f"&body={quote(body)}"))
        self.iface.messageBar().pushSuccess(APP_NAME, tr("The report was saved: {p}").format(p=path))
        return path


class ReportDialog(QDialog):
    def __init__(self, parent):
        super().__init__(parent)
        self.setWindowTitle(tr("Report a problem"))
        self.setMinimumWidth(520)
        v = QVBoxLayout(self)
        v.setContentsMargins(20, 16, 20, 14)
        intro = QLabel(tr("Describe what you did and what went wrong. Biruni GIS will save a report file with "
                          "your description, the program version, system details and the recent messages. "
                          "Your data is not included."))
        intro.setWordWrap(True)
        v.addWidget(intro)
        self.edit = QPlainTextEdit()
        self.edit.setPlaceholderText(tr("For example: I clicked Export and nothing happened…"))
        self.edit.setMinimumHeight(140)
        v.addWidget(self.edit)
        bb = QDialogButtonBox()
        bb.addButton(tr("Save report"), QDialogButtonBox.ButtonRole.AcceptRole)
        bb.addButton(QDialogButtonBox.StandardButton.Cancel)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        v.addWidget(bb)

    def text(self):
        return self.edit.toPlainText().strip()
