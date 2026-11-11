"""arcpy.mp basics over the open Biruni project: ArcGISProject('CURRENT'), maps, layers, layouts.

The engine is only imported when these objects are used, so the rest of the shim stays usable without it."""
import fnmatch
import os

from ._base import missing_callable, unsupported


def _qgis():
    import qgis.core as qc
    return qc


def _match(name, wildcard):
    return not wildcard or fnmatch.fnmatch(name.lower(), str(wildcard).lower())


class Layer:
    """A map layer of the open project."""

    def __init__(self, qlayer=None, node=None):
        self._l = qlayer
        self._node = node

    # identity
    @property
    def name(self):
        return self._node.name() if self._node is not None and self._l is None else self._l.name()

    @name.setter
    def name(self, value):
        if self._l is not None:
            self._l.setName(str(value))
        elif self._node is not None:
            self._node.setName(str(value))

    @property
    def longName(self):  # noqa: N802
        return self.name

    @property
    def dataSource(self):  # noqa: N802
        if self._l is None:
            return ""
        src = self._l.source()
        if "|layername=" in src:
            path, rest = src.split("|", 1)
            name = rest.split("layername=", 1)[1].split("|")[0]
            return os.path.join(path, name)
        return src.split("|")[0]

    @property
    def isFeatureLayer(self):  # noqa: N802
        return self._l is not None and isinstance(self._l, _qgis().QgsVectorLayer)

    @property
    def isRasterLayer(self):  # noqa: N802
        return self._l is not None and isinstance(self._l, _qgis().QgsRasterLayer)

    @property
    def isGroupLayer(self):  # noqa: N802
        return self._l is None and self._node is not None

    @property
    def isBasemapLayer(self):  # noqa: N802
        return False

    @property
    def isBroken(self):  # noqa: N802
        return self._l is not None and not self._l.isValid()

    @property
    def visible(self):
        node = self._tree_node()
        return bool(node.isVisible()) if node is not None else True

    @visible.setter
    def visible(self, value):
        node = self._tree_node()
        if node is not None:
            node.setItemVisibilityChecked(bool(value))

    @property
    def definitionQuery(self):  # noqa: N802
        return self._l.subsetString() if self.isFeatureLayer else ""

    @definitionQuery.setter
    def definitionQuery(self, value):  # noqa: N802
        if self.isFeatureLayer:
            self._l.setSubsetString(value or "")

    @property
    def transparency(self):
        if self._l is None:
            return 0
        return round((1 - self._l.opacity()) * 100)

    @transparency.setter
    def transparency(self, value):
        if self._l is not None:
            self._l.setOpacity(1 - float(value) / 100.0)
            self._l.triggerRepaint()

    @property
    def minThreshold(self):  # noqa: N802
        return self._l.minimumScale() if self._l is not None else 0

    @property
    def maxThreshold(self):  # noqa: N802
        return self._l.maximumScale() if self._l is not None else 0

    def supports(self, layer_property):
        p = str(layer_property).upper()
        if p in ("NAME", "VISIBLE", "LONGNAME"):
            return True
        if p in ("DATASOURCE", "TRANSPARENCY"):
            return self._l is not None
        if p in ("DEFINITIONQUERY", "SHOWLABELS", "LABELCLASSES"):
            return self.isFeatureLayer
        return False

    def listLayers(self, wildcard=None):  # noqa: N802
        if self._node is None or self._l is not None:
            return []
        return _walk_layers(self._node, wildcard)

    def getSelectionSet(self):  # noqa: N802
        if not self.isFeatureLayer:
            return None
        ids = self._l.selectedFeatureIds()
        return set(ids) if ids else None

    def setSelectionSet(self, oidList=None, method="NEW"):  # noqa: N802,N803
        if not self.isFeatureLayer:
            return
        m = str(method or "NEW").upper()
        ids = list(oidList or [])
        if m == "NEW":
            self._l.selectByIds(ids)
        elif m == "UNION":
            self._l.selectByIds(ids, self._l.AddToSelection)
        elif m == "REMOVE":
            self._l.selectByIds(ids, self._l.RemoveFromSelection)
        elif m == "INTERSECT":
            self._l.selectByIds(ids, self._l.IntersectSelection)

    def _tree_node(self):
        if self._node is not None:
            return self._node
        if self._l is None:
            return None
        return _qgis().QgsProject.instance().layerTreeRoot().findLayer(self._l.id())

    def _as_feature_layer(self):
        """For tools: the data behind this map layer with its filter and selection."""
        from . import data as D
        ref = D.parse(self.dataSource)
        lyr = D.FeatureLayer(self.name, ref, self.definitionQuery or None, not self.isFeatureLayer)
        sel = self.getSelectionSet()
        if sel is not None:
            fids = set()
            for fid in sel:
                fids.add(int(fid))
            lyr.selection = fids
        return lyr

    def __str__(self):
        return self.name

    def __repr__(self):
        return f"<Layer '{self.name}'>"


def _walk_layers(group, wildcard=None):
    qc = _qgis()
    out = []
    for child in group.children():
        if isinstance(child, qc.QgsLayerTreeLayer):
            if child.layer() is not None and _match(child.layer().name(), wildcard):
                out.append(Layer(child.layer(), None))
        elif isinstance(child, qc.QgsLayerTreeGroup):
            if _match(child.name(), wildcard):
                out.append(Layer(None, child))
            out.extend(_walk_layers(child, wildcard))
    return out


class Map:
    def __init__(self, project):
        self._p = project

    @property
    def name(self):
        return self._p.title() or "Map"

    @property
    def mapType(self):  # noqa: N802
        return "MAP"

    @property
    def spatialReference(self):  # noqa: N802
        from .geometry import SpatialReference
        return SpatialReference(self._p.crs().toWkt())

    @spatialReference.setter
    def spatialReference(self, value):  # noqa: N802
        from .geometry import as_sr
        sr = as_sr(value)
        qc = _qgis()
        self._p.setCrs(qc.QgsCoordinateReferenceSystem.fromWkt(sr._srs.ExportToWkt()))

    def listLayers(self, wildcard=None):  # noqa: N802
        return _walk_layers(self._p.layerTreeRoot(), wildcard)

    def listTables(self, wildcard=None):  # noqa: N802
        return [lyr for lyr in self.listLayers(wildcard) if lyr.isFeatureLayer and not lyr._l.isSpatial()]

    def addDataFromPath(self, data_path, web_service_type=None, custom_parameters=None):  # noqa: N802
        qc = _qgis()
        from . import data as D
        ref = D.parse(data_path)
        name = ref.base_name or os.path.basename(str(data_path))
        if ref.kind == "raster":
            lyr = qc.QgsRasterLayer(ref.container, name)
        else:
            uri = ref.container + (f"|layername={ref.name}" if ref.name else "")
            if ref.kind == "mem":
                unsupported("mp.Map.addDataFromPath for memory datasets")
            lyr = qc.QgsVectorLayer(uri, name, "ogr")
        if not lyr.isValid():
            raise ValueError(str(data_path))
        self._p.addMapLayer(lyr)
        return Layer(lyr)

    def addLayer(self, add_layer_or_layerfile, add_position="AUTO_ARRANGE"):  # noqa: N802
        if isinstance(add_layer_or_layerfile, Layer) and add_layer_or_layerfile._l is not None:
            self._p.addMapLayer(add_layer_or_layerfile._l.clone())
            return [add_layer_or_layerfile]
        return [self.addDataFromPath(str(add_layer_or_layerfile))]

    def removeLayer(self, remove_layer):  # noqa: N802
        if remove_layer._l is not None:
            self._p.removeMapLayer(remove_layer._l.id())

    def clearSelection(self):  # noqa: N802
        for lyr in self.listLayers():
            if lyr.isFeatureLayer:
                lyr._l.removeSelection()

    def __repr__(self):
        return f"<Map '{self.name}'>"


class Layout:
    def __init__(self, qlayout):
        self._l = qlayout

    @property
    def name(self):
        return self._l.name()

    def _export(self, path, kind, resolution=None):
        qc = _qgis()
        exporter = qc.QgsLayoutExporter(self._l)
        if kind == "pdf":
            s = qc.QgsLayoutExporter.PdfExportSettings()
            if resolution:
                s.dpi = float(resolution)
            res = exporter.exportToPdf(str(path), s)
        else:
            s = qc.QgsLayoutExporter.ImageExportSettings()
            if resolution:
                s.dpi = float(resolution)
            res = exporter.exportToImage(str(path), s)
        if res != qc.QgsLayoutExporter.Success:
            from ._base import fail
            fail(210, f"Cannot export the layout to {path}")
        return str(path)

    def exportToPDF(self, out_pdf, resolution=96, *a, **k):  # noqa: N802
        return self._export(out_pdf, "pdf", resolution)

    def exportToPNG(self, out_png, resolution=96, *a, **k):  # noqa: N802
        return self._export(out_png, "image", resolution)

    def exportToJPEG(self, out_jpg, resolution=96, *a, **k):  # noqa: N802
        return self._export(out_jpg, "image", resolution)

    def exportToTIFF(self, out_tif, resolution=96, *a, **k):  # noqa: N802
        return self._export(out_tif, "image", resolution)

    def listElements(self, element_type=None, wildcard=None):  # noqa: N802
        unsupported("mp.Layout.listElements")


class ArcGISProject:
    """'CURRENT' is the open Biruni project; a .qgz/.qgs path opens that project. .aprx files cannot be read."""

    def __init__(self, aprx_file_path="CURRENT"):
        qc = _qgis()
        p = str(aprx_file_path)
        if p.upper() == "CURRENT":
            self._p = qc.QgsProject.instance()
        elif p.lower().endswith((".qgz", ".qgs")):
            self._p = qc.QgsProject()
            if not self._p.read(p):
                raise OSError(p)
        else:
            unsupported("mp.ArcGISProject for .aprx files", "Open the project in Biruni and use 'CURRENT'.")

    @property
    def filePath(self):  # noqa: N802
        return self._p.fileName()

    @property
    def homeFolder(self):  # noqa: N802
        return self._p.homePath()

    @property
    def defaultGeodatabase(self):  # noqa: N802
        from ._base import env
        return env.scratchGDB

    @property
    def activeMap(self):  # noqa: N802
        return Map(self._p)

    def listMaps(self, wildcard=None):  # noqa: N802
        m = Map(self._p)
        return [m] if _match(m.name, wildcard) else []

    def listLayouts(self, wildcard=None):  # noqa: N802
        return [Layout(lo) for lo in self._p.layoutManager().layouts() if _match(lo.name(), wildcard)]

    def listReports(self, wildcard=None):  # noqa: N802
        return []

    def save(self):
        if not self._p.fileName():
            from ._base import fail
            fail(210, "The project has not been saved yet; use saveACopy")
        self._p.write()

    def saveACopy(self, file_name):  # noqa: N802
        if not str(file_name).lower().endswith((".qgz", ".qgs")):
            file_name = os.path.splitext(str(file_name))[0] + ".qgz"
        self._p.write(str(file_name))

    def importDocument(self, *a, **k):  # noqa: N802
        unsupported("mp.ArcGISProject.importDocument")

    def updateConnectionProperties(self, *a, **k):  # noqa: N802
        unsupported("mp.ArcGISProject.updateConnectionProperties")


def LayerFile(*a, **k):  # noqa: N802
    unsupported("mp.LayerFile")


def __getattr__(name):
    if name.startswith("__"):
        raise AttributeError(name)
    return missing_callable(f"mp.{name}")
