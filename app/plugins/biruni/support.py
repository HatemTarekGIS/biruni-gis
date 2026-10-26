"""Staying in touch with users: update notices and problem reports.

* Updates: once a day Biruni asks GitHub for the latest release of ``REPO`` and
  shows a message-bar notice with a Download button (no automatic installs).
* Problem reports: a ZIP with what is needed to reproduce a problem - version,
  system, settings that matter, the recent message log and Python errors, and
  a description of the open project (layer names and formats, never the data).
"""
import datetime as dt
import json
import os
import platform
import re
import zipfile
from collections import deque
from urllib.parse import quote

from qgis.core import Qgis, QgsApplication, QgsNetworkAccessManager, QgsProject, QgsSettings
from qgis.PyQt.QtCore import QObject, QUrl
from qgis.PyQt.QtGui import QDesktopServices
from qgis.PyQt.QtNetwork import QNetworkReply, QNetworkRequest
from qgis.PyQt.QtWidgets import (QDialog, QDialogButtonBox, QLabel, QMessageBox, QPlainTextEdit, QPushButton,
                                 QVBoxLayout)

from . import i18n
from .i18n import tr
from .version import APP_NAME, HOMEPAGE, REPO, VERSION

CHECK_KEY = "biruni/check-updates"
LAST_CHECK_KEY = "biruni/last-update-check"
SKIP_KEY = "biruni/skip-version"


def parse_version(text):
    """'v0.12.3-beta' -> (0, 12, 3); unknown parts are 0."""
    nums = [int(n) for n in re.findall(r"\d+", text or "")[:3]]
    return tuple(nums + [0] * (3 - len(nums)))


def is_newer(remote, local=VERSION):
    return parse_version(remote) > parse_version(local)


class Support(QObject):
    def __init__(self, plugin):
        super().__init__()
        self.plugin = plugin
        self.iface = plugin.iface
        self.log = deque(maxlen=800)
        self._reply = None

    def install(self):
        QgsApplication.messageLog().messageReceived.connect(self._on_message)

    def unload(self):
        try:
            QgsApplication.messageLog().messageReceived.disconnect(self._on_message)
        except TypeError:
            pass

    def _on_message(self, message, tag, level):
        stamp = dt.datetime.now().strftime("%H:%M:%S")
        names = {Qgis.MessageLevel.Info: "info", Qgis.MessageLevel.Warning: "warning",
                 Qgis.MessageLevel.Critical: "critical", Qgis.MessageLevel.Success: "success"}
        self.log.append(f"{stamp} [{tag or '-'}] {names.get(level, '?')}: {message}")

    # ---- updates ---------------------------------------------------------
    def auto_check(self):
        s = QgsSettings()
        if not REPO or not s.value(CHECK_KEY, True, type=bool):
            return
        last = s.value(LAST_CHECK_KEY, "")
        today = dt.date.today().isoformat()
        if last == today:
            return
        s.setValue(LAST_CHECK_KEY, today)
        self.check(manual=False)

    def check(self, manual=True):
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
