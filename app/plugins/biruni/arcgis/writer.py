"""Write ArcGIS Pro documents from the current project - the inverse of reader.py.

    .lyrx  layer file - one layer or a group (CIMLayerDocument)
    .mapx  map file   - the whole map: layers, groups, tables (CIMMapDocument)
    .aprx  project    - a ZIP of CIM JSON parts: GISProject.json, Index.json,
                        DocumentInfo.xml, the map, one part per layer and per
                        print layout, and the layouts' pictures

Data sources are written as ArcGIS workspace connections, relative to the
document like ArcGIS Pro does: shapefiles, file geodatabases, rasters,
GeoPackage (as SQLite), Excel/CSV tables and ArcGIS feature services. Data
ArcGIS cannot read directly (memory layers, GeoJSON, KML, PostGIS, ...) can be
copied into a file geodatabase next to the document (copy_data=True)."""
import json
import os
import re
import uuid
import zipfile
from dataclasses import dataclass, field

from qgis.core import (Qgis, QgsCoordinateTransform, QgsLayerTreeGroup, QgsLayerTreeLayer, QgsMapLayer, QgsProject,
                       QgsProviderRegistry, QgsRasterLayer, QgsRectangle, QgsSymbol, QgsVectorFileWriter,
                       QgsVectorLayer)

from ..i18n import tr
from .cim_write import SymbolWriter, spatial_reference, to_sql

CIM_VERSION = "3.0.0"
CIM_BUILD = 36056
MAP_URI = "CIMPATH=map/map.json"
_RASTER_EXT = (".tif", ".tiff", ".img", ".jpg", ".jpeg", ".png", ".jp2", ".bmp", ".gif", ".ecw", ".sid", ".vrt",
               ".asc", ".dem", ".bil", ".nc", ".hdf", ".dt0", ".dt1", ".dt2", ".ntf", ".gpkg")
_UNSUPPORTED_LAYERS = {
    Qgis.LayerType.VectorTile: "vector tile layers are not written",
    Qgis.LayerType.Mesh: "mesh layers are not written",
    Qgis.LayerType.PointCloud: "point cloud layers are not written",
    Qgis.LayerType.Annotation: "annotation layers are not written",
    Qgis.LayerType.Plugin: "plugin layers are not written",
    Qgis.LayerType.Group: "group layers are not written",
    Qgis.LayerType.TiledScene: "3D scene layers are not written",
}


class WriteError(Exception):
    """The document cannot be written at all."""


class _Skip(Exception):
    pass


@dataclass
class WriteReport:
    path: str
    written: list = field(default_factory=list)    # (layer name, kind)
    skipped: list = field(default_factory=list)    # (layer name, reason)
    warnings: list = field(default_factory=list)   # "layer: message"
    copied: list = field(default_factory=list)     # (layer name, "file.gdb/table")
    layouts: list = field(default_factory=list)    # layout names written
    files: list = field(default_factory=list)      # files created


def _safe_part(name, used):
    """ASCII file name for a CIM part, unique (case-insensitively) in `used`."""
    base = re.sub(r"[^A-Za-z0-9_]+", "_", name or "").strip("_")[:60] or "layer"
    out, k = base, 2
    while out.lower() in used:
        out, k = f"{base}{k}", k + 1
    used.add(out.lower())
    return out


def _gdb_name(text):
    s = re.sub(r"\W+", "_", text or "").strip("_")[:120]
    return s if s and not s[0].isdigit() else f"L_{s}"


def needs_copy(layers):
    """Names of the vector layers whose data ArcGIS Pro cannot open where it is."""
    out = []
    for lyr in layers:
        if isinstance(lyr, QgsVectorLayer) and _vector_source(lyr)[0] is None:
            out.append(lyr.name())
    return out


def _decode(layer):
    try:
        return QgsProviderRegistry.instance().decodeUri(layer.providerType(), layer.source()) or {}
    except Exception:  # noqa: BLE001
        return {}


def _vector_source(layer):
    """(factory, path, dataset) ArcGIS can read, or (None, reason, None)."""
    prov = layer.providerType()
    if prov == "arcgisfeatureserver":
        url = (_decode(layer).get("url") or "").rstrip("/")
        m = re.match(r"(.*/FeatureServer|.*/MapServer)/(\d+)$", url, re.IGNORECASE)
        return ("FeatureService", m.group(1), m.group(2)) if m else ("FeatureService", url, "")
    if prov != "ogr":
        return None, tr("{kind} data must be copied into a file geodatabase").format(kind=prov), None
    parts = _decode(layer)
    path = parts.get("path") or layer.source().split("|")[0]
    name = parts.get("layerName") or ""
    low = path.lower().rstrip("/\\")
    if low.endswith(".shp") or low.endswith(".dbf"):
        return "Shapefile", path, os.path.basename(path)
    if low.endswith(".gdb"):
        if not name:
            name = _first_layer(path)
        return "FileGDB", path, name
    if low.endswith(".gpkg") or low.endswith(".sqlite"):
        return "Sql", path, name or _first_layer(path)
    if not layer.isSpatial():
        if low.endswith(".xlsx") or low.endswith(".xls"):
            return "Excel", path, (name or _first_layer(path)) + "$"
        if low.endswith(".csv") or low.endswith(".txt"):
            return "TextFile", path, os.path.basename(path)
    return None, tr("{kind} files must be copied into a file geodatabase").format(
        kind=os.path.splitext(low)[1].lstrip(".").upper() or prov), None


def _first_layer(path):
    from osgeo import gdal, ogr

    gdal.PushErrorHandler("CPLQuietErrorHandler")
    try:
        ds = ogr.Open(path)
        return ds.GetLayer(0).GetName() if ds is not None and ds.GetLayerCount() else ""
    finally:
        gdal.PopErrorHandler()


class _Writer:
    def __init__(self, project, out_path, copy_data=False):
        self.project = project or QgsProject.instance()
        self.out = os.path.abspath(out_path)
        self.base = os.path.dirname(self.out)
        self.stem = os.path.splitext(os.path.basename(self.out))[0]
        self.copy_data = copy_data
        self.report = WriteReport(self.out)
        self.defs = []            # layer definitions in order
        self.table_defs = []
        self.tables = []          # uris of standalone tables
        self.layer_uris = {}      # QgsMapLayer id -> uri
        self.children = {}        # group uri -> child uris
        self._used = set()
        self.gdb = None
        self._gdb_used = set()
        self._current = None

    # -- helpers --
    def warn(self, text):
        line = f"{self._current}: {text}" if self._current else text
        if text and line not in self.report.warnings:
            self.report.warnings.append(line)

    def uri(self, name):
        return f"CIMPATH=Map/{_safe_part(name, self._used)}.json"

    def rel(self, path):
        """A data path as ArcGIS stores it: relative to the document when possible."""
        p = os.path.abspath(path)
        try:
            if os.path.splitdrive(p)[0].lower() == os.path.splitdrive(self.base)[0].lower():
                r = os.path.relpath(p, self.base)
                p = r if r.startswith("..") else ".\\" + r if r != "." else "."
        except ValueError:
            pass
        return p.replace("/", "\\")

    # -- data connections --
    def _copy(self, layer):
        """Copy a vector layer into <document>_data.gdb; returns the table name."""
        if self.gdb is None:
            self.gdb = os.path.join(self.base, f"{self.stem}_data.gdb")
            if os.path.isdir(self.gdb):
                import shutil

                shutil.rmtree(self.gdb)
        base = _gdb_name(layer.name())
        name, k = base, 2
        while name.lower() in self._gdb_used:
            name, k = f"{base}_{k}", k + 1
        self._gdb_used.add(name.lower())
        opts = QgsVectorFileWriter.SaveVectorOptions()
        opts.driverName = "OpenFileGDB"
        opts.fileEncoding = "UTF-8"
        opts.layerName = name
        opts.layerOptions = ["TARGET_ARCGIS_VERSION=ALL"]
        opts.actionOnExistingFile = (QgsVectorFileWriter.ActionOnExistingFile.CreateOrOverwriteLayer
                                     if os.path.isdir(self.gdb)
                                     else QgsVectorFileWriter.ActionOnExistingFile.CreateOrOverwriteFile)
        subset = layer.subsetString()
        if subset:  # copy every feature: the filter is written as the definition query
            layer.setSubsetString("")
        try:
            err, msg, *_ = QgsVectorFileWriter.writeAsVectorFormatV3(
                layer, self.gdb, self.project.transformContext(), opts)
        finally:
            if subset:
                layer.setSubsetString(subset)
        if err != QgsVectorFileWriter.WriterError.NoError:
            raise _Skip(tr("could not be copied into the file geodatabase: {msg}").format(msg=msg))
        self.report.copied.append((layer.name(), f"{os.path.basename(self.gdb)}/{name}"))
        if self.gdb not in self.report.files:
            self.report.files.append(self.gdb)
        return name

    def vector_connection(self, layer):
        factory, path, dataset = _vector_source(layer)
        dtype = "esriDTFeatureClass" if layer.isSpatial() else "esriDTTable"
        if factory is None:
            if not self.copy_data:
                raise _Skip(path)
            name = self._copy(layer)
            return {"type": "CIMStandardDataConnection", "workspaceConnectionString": f"DATABASE={self.rel(self.gdb)}",
                    "workspaceFactory": "FileGDB", "dataset": name, "datasetType": dtype}
        if factory == "FeatureService":
            return {"type": "CIMStandardDataConnection", "workspaceConnectionString": f"URL={path}",
                    "workspaceFactory": "FeatureService", "dataset": dataset, "datasetType": dtype}
        if factory == "Shapefile":
            return {"type": "CIMStandardDataConnection",
                    "workspaceConnectionString": f"DATABASE={self.rel(os.path.dirname(path))}",
                    "workspaceFactory": "Shapefile", "dataset": dataset, "datasetType": dtype}
        if factory == "Sql":
            return {"type": "CIMStandardDataConnection",
                    "workspaceConnectionString": f"AUTHENTICATION_MODE=OSA;DATABASE={self.rel(path)}",
                    "workspaceFactory": "Sql", "dataset": f"main.{dataset}", "datasetType": dtype}
        if factory == "TextFile":
            return {"type": "CIMStandardDataConnection",
                    "workspaceConnectionString": f"DATABASE={self.rel(os.path.dirname(path))}",
                    "workspaceFactory": "TextFile", "dataset": dataset, "datasetType": "esriDTTable"}
        return {"type": "CIMStandardDataConnection", "workspaceConnectionString": f"DATABASE={self.rel(path)}",
                "workspaceFactory": factory, "dataset": dataset, "datasetType": dtype}

    def raster_connection(self, layer):
        if layer.providerType() != "gdal":
            raise _Skip(tr("{kind} rasters are not written").format(kind=layer.providerType()))
        src = layer.source()
        m = re.match(r'^OpenFileGDB:"?(.+?\.gdb)"?:(.+)$', src, re.IGNORECASE)
        if m:
            return {"type": "CIMStandardDataConnection", "workspaceConnectionString": f"DATABASE={self.rel(m.group(1))}",
                    "workspaceFactory": "FileGDB", "dataset": m.group(2), "datasetType": "esriDTRasterDataset"}
        path = _decode(layer).get("path") or src
        if not os.path.isfile(path) or not path.lower().endswith(_RASTER_EXT) or path.lower().endswith(".gpkg"):
            raise _Skip(tr("this raster is not a file ArcGIS Pro can open: save it as GeoTIFF first"))
        return {"type": "CIMStandardDataConnection",
                "workspaceConnectionString": f"DATABASE={self.rel(os.path.dirname(path))}",
                "workspaceFactory": "Raster", "dataset": os.path.basename(path), "datasetType": "esriDTAny"}

    # -- layers --
    def _common(self, layer, uri, visible, expanded):
        d = {"name": layer.name(), "uRI": uri, "useSourceMetadata": True, "description": layer.name(),
             "expanded": bool(expanded), "layerType": "Operational", "showLegends": True,
             "visibility": bool(visible), "displayCacheType": "Permanent", "maxDisplayCacheAge": 5,
             "showPopups": True, "serviceLayerID": -1, "refreshRate": -1,
             "refreshRateUnit": "esriTimeUnitsSeconds", "blendingMode": "Alpha"}
        if layer.opacity() < 1:
            d["transparency"] = round((1 - layer.opacity()) * 100, 2)
        if layer.hasScaleBasedVisibility():
            if layer.minimumScale():
                d["minScale"] = float(layer.minimumScale())
            if layer.maximumScale():
                d["maxScale"] = float(layer.maximumScale())
        return d

    def _table_props(self, layer, sw):
        out = {}
        subset = layer.subsetString()
        if subset:
            sql, exact = to_sql(subset)
            if not exact:
                self.warn(tr("check the definition query in ArcGIS Pro: {query}").format(query=sql))
            out["definitionExpression"] = sql
            out["definitionExpressionName"] = "Query 1"
            out["definitionFilterChoices"] = [{"type": "CIMDefinitionFilter", "name": "Query 1",
                                               "definitionExpression": sql}]
        disp = layer.displayExpression().strip()
        m = re.fullmatch(r'"([^"]+)"|([A-Za-z_][\w]*)', disp)
        if m and layer.fields().lookupField(m.group(1) or m.group(2)) >= 0:
            out["displayField"] = m.group(1) or m.group(2)
        hidden = {c.name for c in layer.attributeTableConfig().columns() if c.hidden and c.name}
        descs = []
        for f in layer.fields():
            alias = layer.attributeAlias(layer.fields().lookupField(f.name())) or f.name()
            if alias != f.name() or f.name() in hidden:
                descs.append({"type": "CIMFieldDescription", "alias": alias, "fieldName": f.name(),
                              "visible": f.name() not in hidden, "searchMode": "Exact"})
        if descs:
            out["fieldDescriptions"] = descs
        return out

    def vector_layer(self, layer, uri, visible, expanded):
        sw = SymbolWriter()
        dc = self.vector_connection(layer)
        props = self._table_props(layer, sw)
        if not layer.isSpatial():
            d = dict(self._common(layer, uri, visible, expanded), type="CIMStandaloneTable", dataConnection=dc,
                     **props)
            for k in ("layerType", "showLegends", "visibility", "displayCacheType", "maxDisplayCacheAge",
                      "showPopups", "refreshRate", "refreshRateUnit", "blendingMode", "transparency",
                      "minScale", "maxScale", "expanded", "description", "serviceLayerID"):
                d.pop(k, None)
            return d, "table"
        renderer = sw.renderer(layer.renderer())
        if renderer is None:
            renderer = {"type": "CIMSimpleRenderer", "patch": "Default",
                        "symbol": sw.symbol(QgsSymbol.defaultSymbol(layer.geometryType()))}
        labels = sw.label_classes(layer)
        d = dict(self._common(layer, uri, visible, expanded), type="CIMFeatureLayer")
        d.update({"autoGenerateFeatureTemplates": True,
                  "featureTable": dict({"type": "CIMFeatureTable", "displayField": "", "editable": True,
                                        "dataConnection": dc, "studyAreaSpatialRel": "esriSpatialRelUndefined",
                                        "searchOrder": "esriSearchOrderSpatial"}, **props),
                  "htmlPopupEnabled": True, "selectable": True, "featureCacheType": "Session",
                  "displayFiltersType": "ByScale", "featureBlendingMode": "Alpha", "layerEffectsMode": "Layer",
                  "labelClasses": labels, "labelVisibility": bool(layer.labelsEnabled() and labels),
                  "renderer": renderer, "scaleSymbols": True, "snappable": True})
        if not d["featureTable"]["displayField"]:
            d["featureTable"].pop("displayField")
        for w in sw.warnings:
            self.warn(w)
        return d, "service" if dc.get("workspaceFactory") == "FeatureService" else "vector"

    def raster_layer(self, layer, uri, visible, expanded):
        sw = SymbolWriter()
        d = dict(self._common(layer, uri, visible, expanded), type="CIMRasterLayer",
                 dataConnection=self.raster_connection(layer))
        cz = sw.colorizer(layer)
        if cz:
            d["colorizer"] = cz
        for w in sw.warnings:
            self.warn(w)
        return d, "raster"

    def service_layer(self, layer, uri, visible, expanded):
        prov = layer.providerType()
        parts = _decode(layer)
        url = parts.get("url") or ""
        if prov == "wms" and parts.get("type") == "xyz":
            tiles = url.replace("{z}", "{level}").replace("{x}", "{col}").replace("{y}", "{row}")
            # exactly what ArcGIS Pro writes when a tile URL is added from a path
            d = dict(self._common(layer, uri, visible, expanded), type="CIMTiledServiceLayer",
                     serviceConnection={"type": "CIMStandardServiceConnection", "serviceType": "1", "url": tiles})
            return d, "tiles"
        if prov == "arcgismapserver":
            m = re.search(r"/rest/services/(.+?)/MapServer", url, re.IGNORECASE)
            d = dict(self._common(layer, uri, visible, expanded), type="CIMMapServiceLayer",
                     serviceConnection={"type": "CIMAGSServiceConnection", "objectType": "MapServer", "url": url,
                                        "objectName": m.group(1) if m else "",
                                        "serverConnection": {"type": "CIMInternetServerConnection",
                                                             "anonymous": True, "hideUserProperty": True,
                                                             "url": url.split("/rest/services")[0] + "/services"
                                                             if "/rest/services" in url else url,
                                                             "connectionMode": "Consumer",
                                                             "serverType": "ArcGIS"}})
            return d, "service"
        if prov == "wms":
            raise _Skip(tr("WMS / WMTS layers are not written: add them in ArcGIS Pro from Add Data > "
                           "Data From Path with this address: {url}").format(url=url or parts.get("url", "?")))
        raise _Skip(tr("{kind} layers are not written").format(kind=prov))

    def layer(self, layer, visible=True, expanded=False):
        """Write one map layer; returns its uri (None when skipped)."""
        self._current = layer.name()
        try:
            if layer.type() in _UNSUPPORTED_LAYERS:
                raise _Skip(tr(_UNSUPPORTED_LAYERS[layer.type()]))
            if not layer.isValid():
                raise _Skip(tr("the layer's data is not available"))
            uri = self.uri(layer.name())
            if isinstance(layer, QgsVectorLayer):
                d, kind = self.vector_layer(layer, uri, visible, expanded)
            elif isinstance(layer, QgsRasterLayer):
                if layer.providerType() == "gdal":
                    d, kind = self.raster_layer(layer, uri, visible, expanded)
                else:
                    d, kind = self.service_layer(layer, uri, visible, expanded)
            else:
                raise _Skip(tr("unsupported layer type"))
        except _Skip as e:
            self.report.skipped.append((layer.name(), str(e)))
            return None
        finally:
            self._current = None
        if kind == "table":
            self.table_defs.append(d)
            self.tables.append(uri)
        else:
            self.defs.append(d)
        self.layer_uris[layer.id()] = uri
        self.report.written.append((layer.name(), kind))
        return uri

    def node(self, node):
        """A layer tree node -> uri (groups recurse); tables go to self.tables, not the tree."""
        if isinstance(node, QgsLayerTreeLayer):
            lyr = node.layer()
            if lyr is None:
                return None
            uri = self.layer(lyr, node.itemVisibilityChecked(), node.isExpanded())
            return None if uri in self.tables else uri
        if isinstance(node, QgsLayerTreeGroup):
            uri = self.uri(node.name())
            kids = [u for u in (self.node(c) for c in node.children()) if u]
            d = {"type": "CIMGroupLayer", "name": node.name(), "uRI": uri, "useSourceMetadata": True,
                 "description": node.name(), "expanded": bool(node.isExpanded()), "layerType": "Operational",
                 "showLegends": True, "visibility": bool(node.itemVisibilityChecked()),
                 "displayCacheType": "Permanent", "maxDisplayCacheAge": 5, "showPopups": True,
                 "serviceLayerID": -1, "refreshRate": -1, "refreshRateUnit": "esriTimeUnitsSeconds",
                 "blendingMode": "Alpha", "layers": kids}
            if node.customProperty("biruni/opacity") not in (None, "", 1, "1"):
                try:
                    d["transparency"] = round((1 - float(node.customProperty("biruni/opacity"))) * 100, 2)
                except (TypeError, ValueError):
                    pass
            self.defs.append(d)
            self.children[uri] = kids
            return uri
        return None

    def nodes(self, nodes):
        return [u for u in (self.node(n) for n in nodes) if u]

    # -- documents --
    def map_definition(self, name, top, extent=None):
        crs = self.project.crs()
        if not crs.isValid():
            lyr = next((self.project.mapLayer(i) for i in self.layer_uris), None)
            crs = lyr.crs() if lyr is not None else crs
        sr = spatial_reference(crs)
        m = {"type": "CIMMap", "name": name, "uRI": MAP_URI, "useSourceMetadata": True, "layers": top,
             "standaloneTables": list(self.tables), "defaultViewingMode": "Map", "mapType": "Map",
             "colorModel": "RGB", "scaleDisplayFormat": "Value", "clippingMode": "None",
             "nearPlaneClipDistanceMode": "Automatic", "rGBColorProfile": "sRGB IEC61966-2.1",
             "cMYKColorProfile": "U.S. Web Coated (SWOP) v2", "autoFillFeatureCache": True}
        if sr:
            m["spatialReference"] = sr
        ext = extent if extent is not None and not extent.isEmpty() else self.full_extent(crs)
        if ext is not None and not ext.isEmpty():
            m["defaultExtent"] = {"xmin": ext.xMinimum(), "ymin": ext.yMinimum(), "xmax": ext.xMaximum(),
                                  "ymax": ext.yMaximum()}
            if sr:
                m["defaultExtent"]["spatialReference"] = sr
        self.crs = crs
        return m

    def full_extent(self, crs):
        ext = self.project.viewSettings().defaultViewExtent()
        if not ext.isEmpty():
            return QgsCoordinateTransform(ext.crs(), crs, self.project).transformBoundingBox(ext)
        rect = QgsRectangle()
        for lid in self.layer_uris:
            lyr = self.project.mapLayer(lid)
            if lyr is None or not lyr.isSpatial() or lyr.extent().isEmpty():
                continue
            try:
                rect.combineExtentWith(QgsCoordinateTransform(lyr.crs(), crs, self.project)
                                       .transformBoundingBox(lyr.extent()))
            except Exception:  # noqa: BLE001
                continue
        return rect

    def header(self, doc_type):
        return {"type": doc_type, "version": CIM_VERSION, "build": CIM_BUILD}


def _dump(d):
    return json.dumps(d, ensure_ascii=False, indent=2)


def _write_json(path, d):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(_dump(d))
    os.replace(tmp, path)


def _nodes_for(project, items):
    """Layers / layer-tree nodes / None (= the whole project) -> layer tree nodes."""
    root = project.layerTreeRoot()
    if items is None:
        return list(root.children())
    out = []
    for it in items:
        if isinstance(it, QgsMapLayer):
            node = root.findLayer(it.id())
            out.append(node if node is not None else QgsLayerTreeLayer(it))
        else:
            out.append(it)
    return out


def write_lyrx(path, items=None, project=None, copy_data=False):
    """Layer file for some layers / groups (layer tree nodes or QgsMapLayers; None = everything)."""
    project = project or QgsProject.instance()
    w = _Writer(project, path, copy_data)
    top = w.nodes(_nodes_for(project, items))
    if not top and not w.tables:
        raise WriteError(tr("Nothing could be written: {why}").format(
            why="; ".join(f"{n}: {r}" for n, r in w.report.skipped) or tr("no layers")))
    doc = w.header("CIMLayerDocument")
    doc.update({"layers": top, "layerDefinitions": w.defs, "rGBColorProfile": "sRGB IEC61966-2.1",
                "cMYKColorProfile": "U.S. Web Coated (SWOP) v2"})
    if w.tables:
        doc["tables"] = w.tables
        doc["tableDefinitions"] = w.table_defs
    _write_json(w.out, doc)
    w.report.files.insert(0, w.out)
    return w.report


def write_mapx(path, project=None, copy_data=False, map_name=None, extent=None, items=None):
    """Map file with the whole layer tree (or `items`)."""
    project = project or QgsProject.instance()
    w = _Writer(project, path, copy_data)
    top = w.nodes(_nodes_for(project, items))
    if not top and not w.tables:
        raise WriteError(tr("Nothing could be written: {why}").format(
            why="; ".join(f"{n}: {r}" for n, r in w.report.skipped) or tr("no layers")))
    m = w.map_definition(map_name or project.title() or w.stem or "Map", top, extent)
    doc = w.header("CIMMapDocument")
    doc.update({"mapDefinition": m, "layerDefinitions": w.defs})
    if w.table_defs:
        doc["tableDefinitions"] = w.table_defs
    _write_json(w.out, doc)
    w.report.files.insert(0, w.out)
    return w.report


_DOC_INFO = ("<CIMDocumentInfo xsi:type='typens:CIMDocumentInfo' "
             "xmlns:xsi='http://www.w3.org/2001/XMLSchema-instance' xmlns:xs='http://www.w3.org/2001/XMLSchema' "
             "xmlns:typens='http://www.esri.com/schemas/ArcGIS/{v}'><Version>{v}</Version><Build>{b}</Build>"
             "<DocumentTitle>{t}</DocumentTitle><SavePreview>false</SavePreview>"
             "<UseRelativePath>true</UseRelativePath><Antialiasing>esriBGLAntialiasingNone</Antialiasing>"
             "<TextAntialiasing>esriBGLTextAAliasForce</TextAntialiasing></CIMDocumentInfo>")
_MAP_PROPS = ("<PropertySet xsi:type='typens:PropertySet' xmlns:xsi='http://www.w3.org/2001/XMLSchema-instance' "
              "xmlns:xs='http://www.w3.org/2001/XMLSchema' xmlns:typens='http://www.esri.com/schemas/ArcGIS/{v}'>"
              "<PropertyArray xsi:type='typens:ArrayOfPropertySetProperty'>"
              "<PropertySetProperty xsi:type='typens:PropertySetProperty'><Key>ViewingMode</Key>"
              "<Value xsi:type='xs:string'>Map</Value></PropertySetProperty>"
              "<PropertySetProperty xsi:type='typens:PropertySetProperty'><Key>MapType</Key>"
              "<Value xsi:type='xs:string'>Map</Value></PropertySetProperty></PropertyArray></PropertySet>")


def _camera(ext, crs):
    if ext is None or ext.isEmpty():
        return None
    w, h = ext.width(), ext.height()
    metres = w * (111320 if crs is not None and crs.isGeographic() else 1)
    return {"type": "CIMViewCamera", "pitch": -90, "scale": max(metres / 0.25, 1), "x": ext.center().x(),
            "y": ext.center().y(), "z": "NaN", "viewportHeight": h, "viewportWidth": w}


def write_aprx(path, project=None, copy_data=False, layouts=True, map_name=None, extent=None):
    """ArcGIS Pro project: the map (whole layer tree) and, optionally, the print layouts."""
    from xml.sax.saxutils import escape

    from .layouts import LayoutWriter

    project = project or QgsProject.instance()
    w = _Writer(project, path, copy_data)
    top = w.nodes(_nodes_for(project, None))
    name = map_name or project.title() or "Map"
    m = w.map_definition(name, top, extent)
    parts = {MAP_URI[8:]: m}
    for d in w.defs + w.table_defs:
        parts[d["uRI"][8:]] = d

    layout_items, binaries = [], {}
    if layouts:
        used = set()
        for lay in project.layoutManager().printLayouts():
            lw = LayoutWriter(project, MAP_URI, w.crs, w.layer_uris)
            try:
                cim = lw.convert(lay)
            except Exception as e:  # noqa: BLE001
                w.report.skipped.append((lay.name(), tr("layout not written: {e}").format(e=e)))
                continue
            uri = f"CIMPATH=Layout/{_safe_part(lay.name(), used)}.json"
            cim["uRI"] = uri
            parts[uri[8:]] = cim
            layout_items.append((lay.name(), uri, list(lw.binaries)))
            binaries.update(lw.binaries)
            w.report.layouts.append(lay.name())
            w.report.warnings.extend(f"{lay.name()}: {x}" for x in lw.warnings)

    # Index.json: one node per part, children = what the part refers to
    ids = {}
    for fname in list(parts) + [u[8:] for u in binaries]:
        ids[fname] = len(ids)
    nodes = []
    for fname, d in parts.items():
        t = d.get("type")
        if t == "CIMMap":
            kind, kids = "Map", [ids[u[8:]] for u in [x["uRI"] for x in w.defs + w.table_defs]]
        elif t == "CIMLayout":
            kind = "Layout"
            kids = [ids[MAP_URI[8:]]] + [ids[u[8:]] for _n, lu, bins in layout_items if lu[8:] == fname
                                         for u in bins]
        else:
            kind, kids = "Layer", [ids[u[8:]] for u in w.children.get(d.get("uRI"), [])]
        nodes.append({"NodeId": ids[fname], "NodeType": kind, "FileName": fname,
                      "ChildNodeIds": ",".join(str(k) for k in kids)})
    for u in binaries:
        nodes.append({"NodeId": ids[u[8:]], "NodeType": "BinaryReference", "FileName": u[8:], "ChildNodeIds": ""})
    index = {"DocumentType": "Index", "MajorVersion": 1, "MinorVersion": 0, "PatchVersion": 0,
             "NumberOfNodes": len(nodes), "Nodes": sorted(nodes, key=lambda n: n["NodeId"])}

    items = [{"type": "CIMProjectItem", "iD": str(uuid.uuid4()), "catalogPath": ".", "pathHint": w.base,
              "itemType": "FolderConnection", "name": os.path.basename(w.base) or w.base,
              "pathSaveRelative": True}]
    if w.gdb:
        items.append({"type": "CIMProjectItem", "iD": str(uuid.uuid4()), "catalogPath": w.rel(w.gdb),
                      "pathHint": w.gdb, "itemType": "GDB", "name": os.path.basename(w.gdb),
                      "pathSaveRelative": True})
    items.append({"type": "CIMProjectItem", "iD": str(uuid.uuid4()), "catalogPath": MAP_URI, "itemType": "Map",
                  "name": name, "propertiesXML": _MAP_PROPS.format(v=CIM_VERSION)})
    for lname, luri, _b in layout_items:
        items.append({"type": "CIMProjectItem", "iD": str(uuid.uuid4()), "catalogPath": luri,
                      "itemType": "Layout", "name": lname})
    view = {"type": "CIMMapView", "viewableObjectPath": MAP_URI, "viewType": "esri_mapping_mapPane",
            "instanceID": 1, "viewingMode": "Map", "verticalExaggerationScaleFactor": 1}
    ext = m.get("defaultExtent")
    cam = _camera(QgsRectangle(ext["xmin"], ext["ymin"], ext["xmax"], ext["ymax"]) if ext else None, w.crs)
    if cam:
        view["camera"] = cam
    gis = {"type": "CIMGISProject", "views": [view], "projectItems": items}
    if w.gdb:
        gis["defaultGeoDatabase"] = w.gdb

    tmp = w.out + ".tmp"
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as z:
        for fname, d in parts.items():
            z.writestr(fname, _dump(d).encode("utf-8"))
        for u, data in binaries.items():
            z.writestr(u[8:], data)
        z.writestr("Index.json", json.dumps(index, indent=2))
        z.writestr("DocumentInfo.xml", _DOC_INFO.format(v=CIM_VERSION, b=CIM_BUILD, t=escape(w.out)))
        z.writestr("GISProject.json", _dump(gis).encode("utf-8"))
    os.replace(tmp, w.out)
    w.report.files.insert(0, w.out)
    return w.report


def layout_to_pagx(path, layout, project=None):
    """One print layout as an ArcGIS Pro layout file (.pagx), with its map and layers."""
    from .layouts import LayoutWriter

    project = project or QgsProject.instance()
    w = _Writer(project, path, False)
    top = w.nodes(_nodes_for(project, None))
    m = w.map_definition(project.title() or "Map", top)
    lw = LayoutWriter(project, MAP_URI, w.crs, w.layer_uris)
    cim = lw.convert(layout)
    cim["uRI"] = "CIMPATH=Layout/layout.json"
    import base64

    doc = w.header("CIMLayoutDocument")
    doc.update({"binaryReferences": [{"type": "CIMBinaryReference", "uRI": u, "data": base64.b64encode(b).decode()}
                                     for u, b in lw.binaries.items()],
                "layerDefinitions": w.defs, "mapDefinitions": [m], "layoutDefinition": cim})
    if w.table_defs:
        doc["tableDefinitions"] = w.table_defs
    _write_json(w.out, doc)
    w.report.layouts.append(layout.name())
    w.report.warnings.extend(f"{layout.name()}: {x}" for x in lw.warnings)
    w.report.files.insert(0, w.out)
    return w.report
