"""«نبض الطبقات» - layer health badges. A small badge beside a layer that has a problem:

    broken  the data source cannot be found (moved, renamed, deleted, drive not connected)
    edits   unsaved edits
    slow    took a long time to draw last time (measured from the map's own drawing, per layer)
    empty   no features at all
    crs     another coordinate system than the map (reprojected while drawing)

Clicking the badge offers the one-click fix. Cheap by design: nothing is polled. A layer is checked again only
when it changes (data source, edits, coordinate system), when the map's coordinate system changes, when layers are
added or moved, and after the map is drawn (only layers whose "slow" state changed). Features are never read:
the count is the one the provider already knows, and services/databases are not counted at all.

Draw times come from the engine's runtime profiler: the map is asked to record a profile of each drawing (a cheap
flag), and after every drawing the per-layer times of the "rendering" group are read."""
import math

from qgis.core import (Qgis, QgsApplication, QgsLayerTree, QgsMessageLog, QgsProject, QgsRasterLayer,
                       QgsVectorLayer)
from qgis.PyQt.QtCore import QCoreApplication, QObject, Qt, QTimer

from ..i18n import tr

SLOW_SECONDS = 1.0  # a layer taking at least this long to draw gets the "slow" badge
SEVERITY = ("broken", "edits", "slow", "empty", "crs")  # the most serious first: its icon is the badge's
_COLOURS = {"broken": "#E5484D", "edits": "#D97706", "slow": "#E5732A", "empty": "#8A94A6", "crs": "#3E7BFA"}
_REMOTE = {"postgres", "oracle", "mssql", "hana", "db2", "wfs", "WFS", "oapif", "arcgisfeatureserver",
           "arcgismapserver", "virtual", "sensorthings"}
_ID_ROLE = Qt.ItemDataRole.UserRole + 5
_GROUP_ROLE = Qt.ItemDataRole.UserRole + 2
_ELAPSED_ROLE = Qt.ItemDataRole.UserRole + 3


def problems_of(layer, project_crs=None, seconds=None):
    """The problem keys of a layer, most serious first. Never reads features."""
    if layer is None:
        return []
    if not layer.isValid():
        return ["broken"]
    out = []
    vector = isinstance(layer, QgsVectorLayer)
    if vector and layer.isEditable() and layer.isModified():
        out.append("edits")
    if seconds is not None and seconds >= SLOW_SECONDS:
        out.append("slow")
    if vector and layer.providerType() not in _REMOTE:
        n = layer.featureCount()
        if n == 0:
            out.append("empty")
    if project_crs is not None and project_crs.isValid() and layer.isSpatial() and layer.crs().isValid():
        from .arrange import is_basemap

        if layer.crs() != project_crs and not is_basemap(layer):
            out.append("crs")
    return out


def nice_scale(value):
    """The round scale (1, 2 or 5 × 10ⁿ) at or below value: 37,412 → 20,000."""
    if not value or value <= 0 or math.isnan(value):
        return 0
    p = 10 ** math.floor(math.log10(value))
    for m in (5, 2, 1):
        if m * p <= value:
            return int(m * p)
    return int(p)


def describe(key, layer, seconds=None, project_crs=None):
    if key == "broken":
        return tr("Data source not found: the file was moved, renamed or deleted, or its drive is not connected.")
    if key == "edits":
        return tr("Unsaved edits on this layer.")
    if key == "slow":
        return tr("Slow to draw: {s} seconds last time.").format(s=f"{seconds or 0:.1f}")
    if key == "empty":
        return tr("Empty layer: it has no features yet.")
    if key == "crs":
        return tr("Another coordinate system than the map ({layer} on a {map} map): it is reprojected while "
                  "drawing.").format(layer=layer.crs().authid() or layer.crs().description(),
                                     map=project_crs.authid() or project_crs.description() if project_crs else "")
    return ""


class Health(QObject):
    def __init__(self, plugin, view, canvas=None):
        super().__init__(view)
        self.plugin = plugin
        self.iface = plugin.iface
        self.view = view
        self.canvas = canvas
        self.indicators = {}  # layer id -> QgsLayerTreeViewIndicator
        self.problems = {}  # layer id -> [problem keys]
        self.render_times = {}  # layer id -> seconds of its last drawing
        self.watched = {}  # layer id -> [(signal, slot)]
        self.pending = set()
        self.menu = None
        self.flag_added = False
        self.timer = QTimer(self)
        self.timer.setSingleShot(True)
        self.timer.setInterval(300)
        self.timer.timeout.connect(self.flush)
        project = QgsProject.instance()
        root = project.layerTreeRoot()
        self.conns = [(project.layersAdded, self._layers_added), (project.layersWillBeRemoved, self._layers_removed),
                      (project.crsChanged, self.schedule_all), (project.readProject, self._project_read),
                      (project.cleared, self._project_cleared), (root.addedChildren, self._tree_added)]
        if canvas is not None:
            self.conns.append((canvas.mapCanvasRefreshed, self.read_render_times))
            flags = canvas.mapSettings().flags()
            if not flags & Qgis.MapSettingsFlag.RecordProfile:
                canvas.setMapSettingsFlags(flags | Qgis.MapSettingsFlag.RecordProfile)
                self.flag_added = True
        for sig, slot in self.conns:
            sig.connect(slot)
        self._layers_added(list(project.mapLayers().values()))

    # ---- watching ----------------------------------------------------------------------------------------------
    def _watch(self, layer):
        lid = layer.id()
        if lid in self.watched:
            return
        sigs = [layer.dataSourceChanged, layer.crsChanged]
        if isinstance(layer, QgsVectorLayer):
            sigs += [layer.editingStarted, layer.editingStopped, layer.layerModified, layer.afterRollBack,
                     layer.afterCommitChanges]
        conns = []
        for sig in sigs:
            slot = (lambda *_a, lid=lid: self.schedule(lid))
            sig.connect(slot)
            conns.append((sig, slot))
        self.watched[lid] = conns

    def _unwatch(self, lid):
        for sig, slot in self.watched.pop(lid, []):
            try:
                sig.disconnect(slot)
            except (TypeError, RuntimeError):
                pass

    def _layers_added(self, layers):
        for layer in layers:
            self._watch(layer)
            self.schedule(layer.id())

    def _layers_removed(self, ids):
        for lid in ids:
            self._unwatch(lid)
            self.pending.discard(lid)
            self.problems.pop(lid, None)
            self.render_times.pop(lid, None)
            ind = self.indicators.pop(lid, None)
            if ind is not None:
                ind.deleteLater()

    def _tree_added(self, node, first, last):
        """Nodes added or moved (dragging a layer makes a new node): the badge goes on the new node."""
        children = node.children()
        for i in range(first, min(last, len(children) - 1) + 1):
            child = children[i]
            if QgsLayerTree.isLayer(child):
                self.schedule(child.layerId())
            elif QgsLayerTree.isGroup(child):
                for n in child.findLayers():
                    self.schedule(n.layerId())

    def _project_read(self, *_args):
        self.render_times.clear()
        for ind in self.indicators.values():
            ind.deleteLater()
        self.indicators.clear()
        self.problems.clear()
        self.schedule_all()

    def _project_cleared(self):
        for lid in list(self.watched):
            self._unwatch(lid)
        self._layers_removed(list(self.indicators))
        self.render_times.clear()
        self.problems.clear()

    def schedule(self, lid):
        self.pending.add(lid)
        self.timer.start()

    def schedule_all(self, *_args):
        for lid in QgsProject.instance().mapLayers():
            self.pending.add(lid)
        self.timer.start()

    # ---- drawing times ---------------------------------------------------------------------------------------
    def read_render_times(self):
        """After a drawing of the map: the per-layer times of the profile the engine recorded."""
        if self.canvas is not None:  # something reset the map's flags: ask again (from the next drawing on)
            flags = self.canvas.mapSettings().flags()
            if not flags & Qgis.MapSettingsFlag.RecordProfile:
                self.canvas.setMapSettingsFlags(flags | Qgis.MapSettingsFlag.RecordProfile)
                self.flag_added = True
        prof = QgsApplication.profiler()
        times = {}
        try:
            for row in range(prof.rowCount()):
                idx = prof.index(row, 0)
                if prof.data(idx, _GROUP_ROLE) != "rendering":
                    continue
                lid = prof.data(idx, _ID_ROLE)
                if lid:
                    times[lid] = float(prof.data(idx, _ELAPSED_ROLE) or 0.0)
        except Exception as e:  # noqa: BLE001 - never let a timing read disturb drawing
            QgsMessageLog.logMessage(f"layer health: draw times not read ({e!r})", "Biruni")
            return
        self.note_render_times(times)

    def note_render_times(self, times):
        """Records draw times; only layers that are slow, or stop being slow, are checked again."""
        for lid, secs in times.items():
            old = self.render_times.get(lid)
            self.render_times[lid] = secs
            if secs >= SLOW_SECONDS or (old is not None and old >= SLOW_SECONDS):
                self.schedule(lid)

    # ---- badges ------------------------------------------------------------------------------------------------
    def flush(self):
        project = QgsProject.instance()
        crs = project.crs()
        for lid in list(self.pending):
            self.pending.discard(lid)
            try:
                self.refresh(project.mapLayer(lid), lid, crs)
            except RuntimeError:  # deleted while waiting
                pass

    def refresh(self, layer, lid, project_crs):
        probs = problems_of(layer, project_crs, self.render_times.get(lid)) if layer is not None else []
        node = QgsProject.instance().layerTreeRoot().findLayer(lid)
        ind = self.indicators.get(lid)
        if not probs:
            self.problems.pop(lid, None)
            if ind is not None:
                if node is not None:
                    self.view.removeIndicator(node, ind)
                self.indicators.pop(lid, None)
                ind.deleteLater()
            return
        self.problems[lid] = probs
        if node is None:
            return
        if ind is None:
            from qgis.gui import QgsLayerTreeViewIndicator

            ind = QgsLayerTreeViewIndicator(self.view)
            ind.setObjectName("biruniLayerHealth")
            ind.clicked.connect(lambda _idx, lid=lid: self.on_click(lid))
            self.indicators[lid] = ind
        from .. import icons

        worst = probs[0]
        ind.setIcon(icons.icon("layers/health-" + worst, _COLOURS[worst]))
        ind.setToolTip(self.tooltip(layer, probs, project_crs))
        if ind not in self.view.indicators(node):
            self.view.addIndicator(node, ind)

    def tooltip(self, layer, probs, project_crs):
        lines = [f"<b>{tr('Layer pulse')}</b>"]
        for key in probs:
            lines.append(f"<span style='color:{_COLOURS[key]}'>●</span> "
                         + describe(key, layer, self.render_times.get(layer.id()), project_crs))
        lines.append(f"<i>{tr('Click for a fix.')}</i>")
        return "<br>".join(lines)

    def teardown(self):
        self.timer.stop()
        for sig, slot in self.conns:
            try:
                sig.disconnect(slot)
            except (TypeError, RuntimeError):
                pass
        for lid in list(self.watched):
            self._unwatch(lid)
        root = QgsProject.instance().layerTreeRoot()
        for lid, ind in self.indicators.items():
            node = root.findLayer(lid)
            try:
                if node is not None:
                    self.view.removeIndicator(node, ind)
                ind.deleteLater()
            except RuntimeError:
                pass
        self.indicators.clear()
        if self.flag_added and self.canvas is not None:
            try:
                self.canvas.setMapSettingsFlags(self.canvas.mapSettings().flags()
                                                & ~Qgis.MapSettingsFlag.RecordProfile)
            except RuntimeError:
                pass

    # ---- fixes -------------------------------------------------------------------------------------------------
    def fixes(self, layer, probs):
        """[(label, callable)] - the one-click fixes offered for these problems."""
        out = []
        for key in probs:
            if key == "broken":
                out.append((tr("Repair data source…"), lambda: self.repair_source(layer)))
            elif key == "edits":
                out.append((tr("Save the edits now"), lambda: self.save_edits(layer)))
            elif key == "slow":
                scale = self.scale_limit()
                out.append((tr("Draw it only when zoomed in closer than 1:{scale}").format(scale=f"{scale:,}"),
                            lambda s=scale: self.limit_scale(layer, s)))
                if isinstance(layer, QgsVectorLayer):
                    out.append((tr("Simplify its drawing (faster, looks nearly the same)"),
                                lambda: self.simplify(layer)))
                elif isinstance(layer, QgsRasterLayer):
                    out.append((tr("Build overviews for faster drawing…"), self.build_overviews))
            elif key == "empty":
                if isinstance(layer, QgsVectorLayer) and layer.isSpatial():
                    out.append((tr("Start adding features"), lambda: self.start_adding(layer)))
                out.append((tr("Remove it from the map"), lambda: QgsProject.instance().removeMapLayer(layer.id())))
            elif key == "crs":
                out.append((tr("What does this mean?"), lambda: self.explain_crs(layer)))
        return out

    def on_click(self, lid):
        from qgis.PyQt.QtGui import QCursor
        from qgis.PyQt.QtWidgets import QMenu

        layer = QgsProject.instance().mapLayer(lid)
        probs = self.problems.get(lid) or []
        if layer is None or not probs:
            return
        menu = QMenu(self.view)
        crs = QgsProject.instance().crs()
        for key in probs:
            info = menu.addAction(describe(key, layer, self.render_times.get(lid), crs))
            info.setEnabled(False)
        menu.addSeparator()
        for label, fn in self.fixes(layer, probs):
            menu.addAction(label).triggered.connect(lambda _=False, f=fn: f())
        self.menu = menu
        menu.popup(QCursor.pos())

    def _say(self, text, success=True):
        try:
            bar = self.iface.messageBar()
            (bar.pushSuccess if success else bar.pushWarning)(tr("Layers"), text)
        except Exception:  # noqa: BLE001 - a message is a nicety
            pass

    def scale_limit(self):
        scale = self.canvas.scale() if self.canvas is not None else 0
        return nice_scale(scale / 2) or 10000

    def limit_scale(self, layer, scale):
        layer.setScaleBasedVisibility(True)
        layer.setMinimumScale(float(scale))  # the most zoomed-out scale it is drawn at
        layer.setMaximumScale(0.0)
        layer.triggerRepaint()
        self.render_times.pop(layer.id(), None)
        self.schedule(layer.id())
        self._say(tr("«{name}» is now drawn only when zoomed in closer than 1:{scale} (Layer properties → "
                     "Rendering to change it).").format(name=layer.name(), scale=f"{scale:,}"))

    def simplify(self, layer):
        from qgis.core import QgsVectorSimplifyMethod

        method = QgsVectorSimplifyMethod()
        method.setSimplifyHints(Qgis.VectorRenderingSimplificationFlag.GeometrySimplification
                                | Qgis.VectorRenderingSimplificationFlag.AntialiasingSimplification)
        method.setThreshold(1.5)
        method.setForceLocalOptimization(True)
        layer.setSimplifyMethod(method)
        layer.triggerRepaint()
        self.render_times.pop(layer.id(), None)
        self.schedule(layer.id())
        self._say(tr("«{name}» is now drawn simplified on screen; the data itself is unchanged.").format(
            name=layer.name()))

    def build_overviews(self):
        runner = getattr(self.plugin, "runner", None)
        if runner is not None:
            runner.open_algorithm("gdal:overviews")

    def save_edits(self, layer):
        if layer.commitChanges(False):
            self._say(tr("Edits of «{name}» saved.").format(name=layer.name()))
        else:
            self._say(tr("The edits of «{name}» could not be saved: {why}").format(
                name=layer.name(), why="; ".join(layer.commitErrors()[:3])), success=False)
        self.schedule(layer.id())

    def start_adding(self, layer):
        self.iface.setActiveLayer(layer)
        if not layer.isEditable():
            layer.startEditing()
        try:
            self.iface.actionAddFeature().trigger()
        except Exception:  # noqa: BLE001
            pass

    def repair_source(self, layer):
        """The engine's own "Repair Data Source…" of the layer's right-click menu; the Project Doctor otherwise."""
        wanted = set()
        for src in ("Repair Data Source…", "Change Data Source…"):
            wanted |= {src, QCoreApplication.translate("QgsAppLayerTreeViewMenuProvider", src)}
        wanted = {w.replace("&", "") for w in wanted}
        try:
            self.view.setCurrentLayer(layer)
            provider = self.view.menuProvider()
            menu = provider.createContextMenu() if provider is not None else None
            if menu is not None:
                from qgis.PyQt.QtGui import QAction

                hit = next((a for a in menu.findChildren(QAction) if a.text().replace("&", "") in wanted), None)
                if hit is not None:
                    hit.trigger()
                    menu.deleteLater()
                    return True
                menu.deleteLater()
        except Exception as e:  # noqa: BLE001
            QgsMessageLog.logMessage(f"layer health: repair action not found ({e!r})", "Biruni")
        runner = getattr(self.plugin, "runner", None)
        if runner is not None:
            from ..commands import CATALOG

            runner.execute(CATALOG["project_doctor"])
        return False

    def explain_crs(self, layer):
        from qgis.PyQt.QtWidgets import QMessageBox

        project = QgsProject.instance()
        crs = project.crs()
        box = QMessageBox(self.view.window())
        box.setIcon(QMessageBox.Icon.Information)
        box.setWindowTitle(tr("Another coordinate system"))
        box.setText(tr("«{name}» is stored in {layer}, while the map is shown in {map}.").format(
            name=layer.name(), layer=layer.crs().userFriendlyIdentifier(), map=crs.userFriendlyIdentifier()))
        box.setInformativeText(tr("This is fine: the layer is reprojected while drawing, so it lines up with the "
                                  "others. Measurements use the map's system. For large layers, saving a copy in "
                                  "the map's system makes drawing faster."))
        use = box.addButton(tr("Use this layer's system for the map"), QMessageBox.ButtonRole.ActionRole)
        box.addButton(tr("Close"), QMessageBox.ButtonRole.RejectRole)
        box.exec()
        if box.clickedButton() is use:
            project.setCrs(layer.crs())
