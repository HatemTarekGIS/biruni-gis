"""Developer self-test: drives the UI through scripted steps and saves
screenshots. Enabled only when the BIRUNI_SELFTEST environment variable is set,
e.g.  BIRUNI_SELFTEST="wait:4000;shot:C:/tmp/home.png;tab:edit;shot:C:/tmp/edit.png;quit"

Steps: wait:<ms> | shot:<png path> | tab:<key> | theme:<light|dark|system>
       | classic:<0|1> | basemap:<key> | start:<show|hide> | info | log:<path> | quit
"""
import os
import traceback

from qgis.PyQt.QtCore import QTimer
from qgis.PyQt.QtWidgets import QApplication


def _write_project():  # noqa: N802
    from qgis.core import QgsProject

    QgsProject.instance().write()


def schedule(plugin):
    steps = [s.strip() for s in os.environ.get("BIRUNI_SELFTEST", "").split(";") if s.strip()]
    log_path = None
    for s in steps:
        if s.startswith("log:"):
            log_path = s[4:]

    def log(msg):
        if log_path:
            with open(log_path, "a", encoding="utf-8") as f:
                f.write(msg + "\n")

    def run(i=0):
        if i >= len(steps):
            return
        step = steps[i]
        kind, _, arg = step.partition(":")
        delay = 300
        try:
            if kind == "wait":
                delay = int(arg)
            elif kind == "shot":
                os.makedirs(os.path.dirname(arg), exist_ok=True)
                QApplication.processEvents()
                popup = QApplication.activePopupWidget()
                modal = QApplication.activeModalWidget()
                plugin.mw.grab().save(arg)
                if popup is not None:
                    popup.grab().save(arg.replace(".png", "_popup.png"))
                if modal is not None:
                    modal.grab().save(arg.replace(".png", "_modal.png"))
                log(f"shot {arg}")
            elif kind == "dialog":  # open a modal dialog without blocking the script
                QTimer.singleShot(0, {"newproject": plugin.project.new_project,
                                      "about": plugin.branding.show_about}[arg])
            elif kind == "closemodal":
                m = QApplication.activeModalWidget()
                if m is not None:
                    m.reject()
            elif kind == "newproject":  # <parent folder>|<name>
                parent, name = arg.split("|")
                plugin.iface.newProject(False)
                plugin.project._write_new(parent, name)
            elif kind == "addlayer":
                plugin.iface.addVectorLayer(arg, "", "ogr")
            elif kind == "consolidate":
                plugin.project.consolidate()
                _write_project()
            elif kind == "gpkgdebug":
                from osgeo import gdal, ogr

                from .project import project_gpkg

                p = project_gpkg()
                ds = ogr.Open(p)
                log(f"gpkg {p} open={ds is not None} err={gdal.GetLastErrorMsg()!r} "
                    f"drivers={gdal.GetDriverCount()} gpkg_drv={gdal.GetDriverByName('GPKG') is not None} "
                    f"OGR_SKIP={os.environ.get('OGR_SKIP')!r} GDAL_SKIP={os.environ.get('GDAL_SKIP')!r}")
                ds = None
                from qgis.core import QgsVectorLayer

                v = QgsVectorLayer(p, "x", "ogr")
                log(f"qgis open valid={v.isValid()} err={v.error().summary()!r}")
            elif kind == "edits":  # add N features to the first vector layer, edit one, delete one
                from qgis.core import QgsFeature, QgsGeometry, QgsProject, QgsVectorLayer

                lyr = next(lyr for lyr in QgsProject.instance().mapLayers().values()
                           if isinstance(lyr, QgsVectorLayer))
                lyr.startEditing()
                for k in range(int(arg)):  # not "i": that is the step index of run()
                    f = QgsFeature(lyr.fields())
                    f.setGeometry(QgsGeometry.fromWkt(f"POINT({31 + k * 0.01} 30)"))
                    lyr.addFeature(f)
                lyr.commitChanges()
                lyr.startEditing()
                fids = [f.id() for f in lyr.getFeatures()]
                lyr.changeAttributeValue(fids[0], lyr.fields().indexOf("name"), "edited")
                lyr.changeGeometry(fids[1], QgsGeometry.fromWkt("POINT(31.5 30.5)"))
                lyr.deleteFeature(fids[-1])
                lyr.commitChanges()
            elif kind == "report":
                from .activity_report import write_report

                write_report(arg, "Selftest", *plugin.activity.snapshot())
            elif kind == "gpssim":  # simulate a phone walking (UDP to the panel), recording on
                import datetime as dt
                import math

                from qgis.PyQt.QtNetwork import QHostAddress, QUdpSocket

                from .gps.nmea import make_sentences

                d = plugin.tracking
                d.show()
                d.raise_()
                d.mode.setCurrentIndex(0)
                if not d.source.is_active():
                    d.toggle_connection()
                sock = QUdpSocket(plugin)
                state = {"k": 0, "t": dt.datetime.now(dt.timezone.utc), "lat": 30.0444, "lon": 31.2357}
                total = int(arg or 120)

                def send():
                    k = state["k"]
                    if k >= total:
                        timer.stop()
                        return
                    if k == 3:
                        d.toggle_recording()
                    moving = not (total // 2 <= k < total // 2 + 70)  # stand still for 70 s mid-walk
                    if moving:
                        ang = k / 25.0
                        state["lat"] += 1.4 * math.cos(ang) / 110850
                        state["lon"] += 1.4 * math.sin(ang) / 96000
                    state["t"] += dt.timedelta(seconds=1)
                    data = make_sentences(state["t"], state["lat"], state["lon"], 5.0 if moving else 0.0,
                                          math.degrees(k / 25.0) % 360, 22.0, 11, 0.8).encode()
                    sock.writeDatagram(data, QHostAddress(QHostAddress.SpecialAddress.LocalHost),
                                       d.listen_port.value())
                    state["k"] = k + 1

                timer = QTimer(plugin)
                timer.timeout.connect(send)
                timer.start(40)
                delay = 40 * total + 1500
            elif kind == "gpsstop":
                plugin.tracking.toggle_recording()
            elif kind == "messages":
                for item in plugin.iface.messageBar().items():
                    log(f"message: {item.title()} | {item.text()}")
                from qgis.core import QgsProject

                for lyr in QgsProject.instance().mapLayers().values():
                    log(f"layer: {lyr.name()} | {lyr.providerType()} | {lyr.source()}")
            elif kind == "cmd":  # run a catalogue command (modal dialogs open without blocking)
                from .commands import CATALOG

                QTimer.singleShot(0, lambda c=CATALOG[arg]: plugin.runner.execute(c))
                delay = 1500
            elif kind == "dock":  # show a panel by objectName
                from qgis.PyQt.QtWidgets import QDockWidget

                d = plugin.mw.findChild(QDockWidget, arg)
                if d is not None:
                    d.show()
                    d.raise_()
                log(f"dock {arg} found={d is not None}")
            elif kind == "sample":
                from .samples import load_world

                plugin.start.hide_page()
                load_world(plugin)
                delay = 1500
            elif kind == "select":  # <layer name>|<expression>
                from qgis.core import QgsProject

                name, expr = arg.split("|", 1)
                lyr = QgsProject.instance().mapLayersByName(name)[0]
                lyr.selectByExpression(expr)
                plugin.iface.setActiveLayer(lyr)
                log(f"selected {lyr.selectedFeatureCount()} in {name}")
            elif kind == "coords":
                from qgis.PyQt.QtWidgets import QDockWidget

                d = plugin.mw.findChild(QDockWidget, "BiruniCoords")
                d.show()
                d.input.setText(arg)
                d.go_to()
            elif kind == "gpsqr":
                d = plugin.tracking
                d.show()
                d.raise_()
                d.mode.setCurrentIndex(d.mode.findData("browser"))
                if not d.source.is_active():
                    d.toggle_connection()
                log(f"gps browser url={d.qr_url.text()} qr={not d.qr.pixmap().isNull()}")
                delay = 1500
            elif kind == "gpsoff":
                if plugin.tracking.source.is_active():
                    plugin.tracking.disconnect_source()
            elif kind == "health":
                from .quality.health_dialog import show_health_dialog

                QTimer.singleShot(0, lambda: show_health_dialog(plugin))
                delay = 4000
            elif kind == "mapsheet":  # <png path>|<title>: build the quick map sheet and export it
                from qgis.core import QgsProject

                from . import mapsheet

                path, _, title = arg.partition("|")
                layout = mapsheet.build_layout(QgsProject.instance(), mapsheet.SheetOptions(title=title or "Biruni"))
                log(f"mapsheet ok={mapsheet.export(layout, path)}")
            elif kind == "lessonstep":  # <lesson>.<step>: press the step's button
                from .learn.lessons import LESSONS

                k, j = (int(x) for x in arg.split("."))
                LESSONS[k][2][j][3](plugin)
                check = LESSONS[k][2][j][1]
                log(f"lessonstep {arg} done={check(plugin) if check else None}")
            elif kind == "lesson":
                from qgis.PyQt.QtWidgets import QDockWidget

                d = plugin.mw.findChild(QDockWidget, "BiruniLearn")
                d.show()
                d.raise_()
                d.lesson_box.setCurrentIndex(int(arg))
            elif kind == "toolbox":
                plugin.toolbox.show()
                plugin.toolbox.raise_()
                if arg:
                    plugin.toolbox.search.setText(arg)
            elif kind == "tab":
                plugin.shell.ribbon.select_tab(arg)
            elif kind == "theme":
                plugin.theme.set_mode(arg)
            elif kind == "classic":
                plugin.shell.set_classic(arg == "1")
            elif kind == "basemap":
                plugin.start.hide_page()
                plugin.add_basemap(arg, zoom_world=True)
                delay = 2500
            elif kind == "start":
                (plugin.start.show_page if arg == "show" else plugin.start.hide_page)()
            elif kind == "info":
                import qgis.utils
                from qgis.PyQt.QtCore import Qt
                from qgis.PyQt.QtGui import QPalette
                from qgis.PyQt.QtWidgets import QDockWidget, QToolBar, QTreeView, QWidget

                loc = plugin.mw.findChild(QWidget, "LocatorWidget")
                bars = [t for t in plugin.mw.findChildren(QToolBar) if t.objectName() == "BiruniRibbonBar"]
                dock = plugin.mw.findChild(QDockWidget, "Layers")
                tree = dock.findChild(QTreeView) if dock else None
                from qgis.core import QgsApplication

                app = QApplication.instance()
                chain = []
                w = tree
                while w is not None:
                    chain.append(f"{w.metaObject().className()}:{w.palette().color(QPalette.ColorRole.Base).name()}"
                                 f"{'*' if w.testAttribute(Qt.WidgetAttribute.WA_SetPalette) else ''}")
                    w = w.parentWidget()
                log(f"app_base={app.palette().color(QPalette.ColorRole.Base).name()} "
                    f"themes={list(QgsApplication.uiThemes().keys())} active_theme={QgsApplication.themeName()} "
                    f"chain={' > '.join(chain)} mw_ss_has_ours={'QMainWindow::separator' in plugin.mw.styleSheet()}")
                if tree is not None:
                    vp = tree.viewport()
                    log(f"tree cls={tree.metaObject().className()} base={tree.palette().color(QPalette.ColorRole.Base).name()} "
                        f"vp_base={vp.palette().color(QPalette.ColorRole.Base).name()} set={tree.testAttribute(Qt.WidgetAttribute.WA_SetPalette)} "
                        f"ss={tree.styleSheet()[:200]!r} app_ss_len={len(QApplication.instance().styleSheet())} mw_ss={plugin.mw.styleSheet()[:300]!r}")
                log(f"info active={qgis.utils.active_plugins} ribbons={len(bars)} "
                    f"locator={loc is not None} locator_parent="
                    f"{loc.parentWidget().objectName() if loc is not None and loc.parentWidget() else None} "
                    f"welcome={plugin.start.welcome is not None} "
                    f"welcome_visible={plugin.start.welcome.isVisible() if plugin.start.welcome else None} "
                    f"start_visible={plugin.start.isVisible()} theme={plugin.theme.mode()} dark={plugin.theme.is_dark()}")
            elif kind == "quit":
                # Test projects are throw-away: never stop at "save changes?".
                from qgis.core import QgsProject

                QgsProject.instance().setDirty(False)
                QTimer.singleShot(200, QApplication.instance().quit)
                return
            log(f"done {kind}")
        except Exception:  # keep going - this is a diagnostics tool
            log(f"step {step} failed:\n{traceback.format_exc()}")
        QTimer.singleShot(delay, lambda: run(i + 1))

    QTimer.singleShot(500, run)
