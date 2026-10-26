"""Product identity: window title, application/taskbar icon and the About dialog."""
import os
import sys

from qgis.core import Qgis, QgsMessageLog
from qgis.PyQt.QtCore import Qt, QUrl
from qgis.PyQt.QtGui import QDesktopServices
from qgis.PyQt.QtWidgets import QApplication, QDialog, QHBoxLayout, QLabel, QPushButton, QVBoxLayout

from . import icons
from .i18n import tr
from .version import APP_NAME, HOMEPAGE, VERSION


class Branding:
    def __init__(self, plugin):
        self.plugin = plugin
        self.mw = plugin.iface.mainWindow()
        self._busy = False

    def install(self):
        ic = icons.app_icon()
        QApplication.setWindowIcon(ic)
        self.mw.setWindowIcon(ic)
        self.mw.windowTitleChanged.connect(self._retitle)
        self._retitle(self.mw.windowTitle())
        self._taskbar_identity()

    def _taskbar_identity(self):
        """Windows shows the taskbar button with the *process* executable's icon
        (qgis-bin.exe). Give the main window its own AppUserModel identity,
        icon and relaunch command so the taskbar shows Biruni GIS and pinning
        it pins the Biruni launcher."""
        if sys.platform != "win32":
            return
        launcher = os.environ.get("BIRUNI_LAUNCHER", "")
        ico = os.path.join(icons.BRANDING_DIR, "biruni.ico")
        try:
            from win32com.propsys import propsys, pscon

            store = propsys.SHGetPropertyStoreForWindow(int(self.mw.winId()), propsys.IID_IPropertyStore)
            store.SetValue(pscon.PKEY_AppUserModel_ID, propsys.PROPVARIANTType(APP_NAME))
            store.SetValue(pscon.PKEY_AppUserModel_RelaunchDisplayNameResource, propsys.PROPVARIANTType(APP_NAME))
            store.SetValue(pscon.PKEY_AppUserModel_RelaunchIconResource, propsys.PROPVARIANTType(ico + ",0"))
            if launcher:
                store.SetValue(pscon.PKEY_AppUserModel_RelaunchCommand, propsys.PROPVARIANTType(f'"{launcher}"'))
            store.Commit()
        except Exception as e:  # cosmetic only - never block startup
            QgsMessageLog.logMessage(f"taskbar identity not set: {e}", APP_NAME, Qgis.MessageLevel.Info)

    def unload(self):
        try:
            self.mw.windowTitleChanged.disconnect(self._retitle)
        except TypeError:
            pass

    def _retitle(self, title):
        # QGIS titles look like "Project — QGIS"; keep the project part.
        if self._busy or not title.rstrip().endswith("QGIS"):
            return
        self._busy = True
        try:
            self.mw.setWindowTitle(title.rstrip()[:-4] + APP_NAME)
        finally:
            self._busy = False

    def show_about(self):
        AboutDialog(self.plugin, self.mw).exec()


class AboutDialog(QDialog):
    def __init__(self, plugin, parent):
        super().__init__(parent)
        self.plugin = plugin
        self.setWindowTitle(tr("About {app}").format(app=APP_NAME))
        self.setMinimumWidth(460)
        t = plugin.theme.tokens()
        v = QVBoxLayout(self)
        v.setContentsMargins(28, 24, 28, 20)
        v.setSpacing(10)

        head = QHBoxLayout()
        logo = QLabel()
        logo.setPixmap(icons.logo_pixmap(72))
        head.addWidget(logo, 0, Qt.AlignmentFlag.AlignTop)
        tv = QVBoxLayout()
        name = QLabel(APP_NAME)
        f = name.font()
        f.setPointSizeF(f.pointSizeF() + 8)
        f.setBold(True)
        name.setFont(f)
        tv.addWidget(name)
        tv.addWidget(QLabel(tr("Version {v}").format(v=VERSION)))
        head.addSpacing(10)
        head.addLayout(tv, 1)
        v.addLayout(head)

        engine = Qgis.version()
        body = QLabel(
            "<p>" + tr("Biruni GIS is a free and open-source desktop GIS for surveyors, engineers and everyone "
                       "who works with maps.") + "</p>"
            "<p>" + tr("Built on the QGIS engine {q}. QGIS is a project of QGIS.org; Biruni GIS is an independent "
                       "derivative and is not endorsed by the QGIS project.").format(q=engine) + "</p>"
            "<p>" + tr("Licence: GNU General Public License v2 or later.") + "<br>"
            + tr("Icons: Tabler Icons (MIT). Map data © OpenStreetMap contributors.") + "</p>"
        )
        body.setWordWrap(True)
        body.setTextFormat(Qt.TextFormat.RichText)
        body.setStyleSheet(f"color: {t['text_dim']};")
        v.addWidget(body)

        row = QHBoxLayout()
        if HOMEPAGE:
            web = QPushButton(tr("Website"))
            web.clicked.connect(lambda: QDesktopServices.openUrl(QUrl(HOMEPAGE)))
            row.addWidget(web)
        qgis_about = QPushButton(tr("About QGIS"))
        qgis_about.clicked.connect(self._qgis_about)
        row.addWidget(qgis_about)
        row.addStretch(1)
        ok = QPushButton(tr("Close"))
        ok.setDefault(True)
        ok.clicked.connect(self.accept)
        row.addWidget(ok)
        v.addLayout(row)

    def _qgis_about(self):
        a = self.plugin.runner.qaction("mActionAbout")
        if a is not None:
            a.trigger()
