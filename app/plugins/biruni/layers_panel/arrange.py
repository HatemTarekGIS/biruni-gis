"""Smart arrange («رتّب ذكي»): layers grouped by kind - tables, points, lines, areas, other, images, basemaps - in that
order from top to bottom, so points draw above lines above areas above images. A preview is shown first; the tree
from before is kept so "Undo arrange" puts it back exactly (groups, order, visibility, custom drawing order).

What is visible on the map stays visible: a layer taken out of a hidden group is hidden itself."""
from qgis.core import Qgis, QgsLayerTree, QgsLayerTreeGroup, QgsMapLayer, QgsProject, QgsRasterLayer, QgsVectorLayer

from ..i18n import tr

# kinds from the top of the tree (drawn last, on top) to the bottom
KIND_ORDER = ("tables", "points", "lines", "polygons", "other", "rasters", "basemaps")
_WEB_PROVIDERS = {"wms", "arcgismapserver", "xyzvectortiles", "vectortile", "mbtilesvectortiles", "vtpkvectortiles",
                  "arcgisvectortileservice", "wcs"}


def kind_title(key):
    return {
        "tables": tr("Tables"),
        "points": tr("Points"),
        "lines": tr("Lines"),
        "polygons": tr("Areas (polygons)"),
        "other": tr("Other layers"),
        "rasters": tr("Images and rasters"),
        "basemaps": tr("Basemaps"),
    }[key]


def is_basemap(layer):
    """Web maps and tiles drawn under everything (satellite, street maps, vector tiles)."""
    if layer.type() == Qgis.LayerType.VectorTile:
        return True
    return layer.providerType() in _WEB_PROVIDERS


def layer_kind(layer):
    if layer is None:
        return "other"
    if is_basemap(layer):
        return "basemaps"
    if isinstance(layer, QgsVectorLayer):
        if not layer.isSpatial():
            return "tables"
        return {Qgis.GeometryType.Point: "points", Qgis.GeometryType.Line: "lines",
                Qgis.GeometryType.Polygon: "polygons"}.get(layer.geometryType(), "other")
    if isinstance(layer, QgsRasterLayer):
        return "rasters"
    return "other"


def _is_private(layer):
    return layer is not None and bool(layer.flags() & QgsMapLayer.LayerFlag.Private)


def plan(root=None):
    """[(kind, [layer tree nodes])] in the proposed order, top first; empty kinds left out. Within a kind the
    current top-to-bottom order is kept."""
    root = root or QgsProject.instance().layerTreeRoot()
    by_kind = {k: [] for k in KIND_ORDER}
    for node in root.findLayers():
        if _is_private(node.layer()):
            continue
        by_kind[layer_kind(node.layer())].append(node)
    return [(k, by_kind[k]) for k in KIND_ORDER if by_kind[k]]


def plan_summary(steps):
    """[(group title, [layer names])] - what the preview shows."""
    return [(kind_title(k), [n.name() for n in nodes]) for k, nodes in steps]


def _replace_children(root, new_nodes):
    """The new nodes go in first, then the old ones come out: the project removes a layer whose last node leaves
    the tree (even later, from a queued call), so every layer must always be in the tree at least once."""
    old = len(root.children())
    root.insertChildNodes(old, new_nodes)
    if old:
        root.removeChildren(0, old)


def snapshot(root):
    return {"children": [c.clone() for c in root.children()], "custom": root.hasCustomLayerOrder(),
            "order": [lyr.id() for lyr in root.customLayerOrder() if lyr is not None]}


def apply(steps, project=None):
    """Rebuilds the tree from a plan. Returns the snapshot that undo() takes."""
    project = project or QgsProject.instance()
    root = project.layerTreeRoot()
    before = snapshot(root)
    new_nodes = []
    planned = set()
    for key, nodes in steps:
        group = QgsLayerTreeGroup(kind_title(key))
        for node in nodes:
            clone = node.clone()
            clone.setItemVisibilityChecked(node.isVisible())  # hidden through its old group: stays hidden
            group.addChildNode(clone)
            planned.add(node.layerId())
        new_nodes.append(group)
    # anything not planned (private layers, custom nodes) keeps its place at the bottom
    everything = root.findLayersAndCustomNodes() if hasattr(root, "findLayersAndCustomNodes") else root.findLayers()
    rest = [n.clone() for n in everything if not (QgsLayerTree.isLayer(n) and n.layerId() in planned)]
    _replace_children(root, new_nodes + rest)
    root.setHasCustomLayerOrder(False)
    return before


def undo(before, project=None):
    """Puts back the tree of a snapshot. Layers removed since are left out; layers added since go on top."""
    project = project or QgsProject.instance()
    root = project.layerTreeRoot()
    from qgis.core import QgsLayerTreeLayer

    nodes = [c.clone() for c in before["children"]]
    in_snapshot = set()
    for n in nodes:
        if QgsLayerTree.isLayer(n):
            in_snapshot.add(n.layerId())
        elif QgsLayerTree.isGroup(n):
            in_snapshot.update(n.findLayerIds())
    current = set(project.mapLayers())
    added = [QgsLayerTreeLayer(project.mapLayer(lid)) for lid in root.findLayerIds()
             if lid not in in_snapshot and project.mapLayer(lid) is not None]  # added since: they go on top
    _replace_children(root, added + nodes)
    for node in root.findLayers():
        if node.layerId() not in current:  # removed from the project since
            node.parent().removeChildNode(node)
    if before["custom"]:
        order = [project.mapLayer(i) for i in before["order"] if project.mapLayer(i) is not None]
        order += [n.layer() for n in root.findLayers() if n.layer() is not None and n.layer() not in order]
        root.setCustomLayerOrder(order)
    root.setHasCustomLayerOrder(before["custom"])


def same_as_plan(steps, root=None):
    """True when the tree already is exactly the plan (nothing to do)."""
    root = root or QgsProject.instance().layerTreeRoot()
    groups = [c for c in root.children() if QgsLayerTree.isGroup(c)]
    if len(groups) != len(steps) or any(not QgsLayerTree.isGroup(c) for c in root.children()[:len(steps)]):
        return False
    for group, (key, nodes) in zip(root.children(), steps):
        if group.name() != kind_title(key) or [c.layerId() if QgsLayerTree.isLayer(c) else None
                                                for c in group.children()] != [n.layerId() for n in nodes]:
            return False
    return True


def preview_dialog(steps, parent=None):
    """The question before arranging: the groups and their layers, Arrange / Cancel."""
    from qgis.PyQt.QtWidgets import (QDialog, QDialogButtonBox, QLabel, QTreeWidget, QTreeWidgetItem,
                                     QVBoxLayout)

    dlg = QDialog(parent)
    dlg.setObjectName("biruniArrangePreview")
    dlg.setWindowTitle(tr("Smart arrange"))
    lay = QVBoxLayout(dlg)
    total = sum(len(nodes) for _k, nodes in steps)
    intro = QLabel(tr("{n} layers will be put into groups by kind, so points draw above lines, lines above areas "
                      "and areas above images:").format(n=total))
    intro.setWordWrap(True)
    lay.addWidget(intro)
    tree = QTreeWidget()
    tree.setObjectName("biruniArrangeTree")
    tree.setHeaderHidden(True)
    for title, names in plan_summary(steps):
        top = QTreeWidgetItem([f"{title} ({len(names)})"])
        f = top.font(0)
        f.setBold(True)
        top.setFont(0, f)
        for name in names:
            top.addChild(QTreeWidgetItem([name]))
        tree.addTopLevelItem(top)
        top.setExpanded(True)
    lay.addWidget(tree, 1)
    note = QLabel(tr("Your current groups are replaced by these. “Undo arrange” brings them back."))
    note.setWordWrap(True)
    lay.addWidget(note)
    box = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
    box.button(QDialogButtonBox.StandardButton.Ok).setText(tr("Arrange"))
    box.button(QDialogButtonBox.StandardButton.Cancel).setText(tr("Cancel"))
    box.accepted.connect(dlg.accept)
    box.rejected.connect(dlg.reject)
    lay.addWidget(box)
    dlg.resize(380, 440)
    dlg.tree = tree
    return dlg
