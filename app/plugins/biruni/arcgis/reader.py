"""Read ArcGIS Pro documents and rebuild their maps in a QGIS project.

    .aprx  project  - a ZIP of CIM JSON documents (one per map and per layer)
    .mapx  map file - one JSON document: mapDefinition + layerDefinitions
    .lyrx  layer file - one JSON document: layers + layerDefinitions
    .lpkx / .mpkx / .ppkx packages - unpacked next to the package when they are ZIPs
    .mxd / .mxt  ArcMap document - data frames, layers, groups, visibility, data
                 sources and definition queries (mxd.py); symbology is not read

Data sources are resolved relative to the document (as ArcGIS stores them):
file geodatabases, shapefiles, rasters, GeoPackage/SQLite, Excel, CSV, DXF,
ArcGIS feature/map/image services and web tile services. Esri basemaps and
enterprise (SDE) connections are reported as skipped, never silently dropped."""
import json
import os
import re
import zipfile
from dataclasses import dataclass, field

from qgis.core import (QgsCoordinateReferenceSystem, QgsDataSourceUri, QgsLayerTreeGroup, QgsProject,
                       QgsRasterLayer, QgsRectangle, QgsReferencedRectangle, QgsVectorLayer, QgsVectorTileLayer)

from ..i18n import tr
from .cim import Converter, crs_from_spatial_reference
from .mxd import MXD_EXTENSIONS, MxdError, package_candidates, read_mxd

ARCGIS_EXTENSIONS = (".aprx", ".mapx", ".lyrx", ".lpkx", ".mpkx", ".ppkx") + MXD_EXTENSIONS
PACKAGE_EXTENSIONS = (".lpkx", ".mpkx", ".ppkx")

# Esri-hosted basemaps need an ArcGIS licence/account: never re-create them.
_ESRI_BASEMAP_HOSTS = re.compile(r"(arcgisonline\.com|basemaps(-api)?\.arcgis\.com|basemapstyles-api\.arcgis\.com|"
                                 r"ibasemaps-api\.arcgis\.com|static-map-tiles-api\.arcgis\.com|cdn\.arcgis\.com)",
                                 re.IGNORECASE)
_IGNORED = {"CIMElevationSurfaceLayer"}
_UNSUPPORTED = ("CIMAnnotationLayer", "CIMDimensionLayer", "CIMOrientedImageryLayer", "CIMLASDatasetLayer",
                "CIMPointCloudLayer", "CIMSceneServiceLayer", "CIMBuildingSceneLayer", "CIMIntegratedMeshLayer",
                "CIMKnowledgeGraphLayer", "CIMParcelFabricLayer", "CIMUtilityNetworkLayer", "CIMTopologyLayer",
                "CIMNetworkDatasetLayer", "CIMMosaicLayer", "CIMVoxelLayer", "CIMGraphicsLayer",
                "MxdBasemapLayer", "MxdServiceLayer")


def _unsupported_reason(t):
    return {
        "CIMAnnotationLayer": tr("annotation layers are not supported"),
        "CIMDimensionLayer": tr("dimension layers are not supported"),
        "CIMOrientedImageryLayer": tr("oriented imagery layers are not supported"),
        "CIMLASDatasetLayer": tr("LAS point cloud layers are not supported"),
        "CIMPointCloudLayer": tr("point cloud layers are not supported"),
        "CIMSceneServiceLayer": tr("3D scene layers are not supported"),
        "CIMBuildingSceneLayer": tr("3D scene layers are not supported"),
        "CIMIntegratedMeshLayer": tr("3D scene layers are not supported"),
        "CIMVoxelLayer": tr("3D scene layers are not supported"),
        "CIMKnowledgeGraphLayer": tr("knowledge graph layers are not supported"),
        "CIMParcelFabricLayer": tr("parcel fabric layers are not supported: add its feature classes from the geodatabase"),
        "CIMUtilityNetworkLayer": tr("utility network layers are not supported: add its feature classes from the geodatabase"),
        "CIMTopologyLayer": tr("topology layers are not supported: check topology with the Quality tab"),
        "CIMNetworkDatasetLayer": tr("network dataset layers are not supported"),
        "CIMMosaicLayer": tr("mosaic dataset layers are not supported"),
        "CIMGraphicsLayer": tr("graphics layers are not supported"),
        "MxdBasemapLayer": tr("Esri basemap (needs an ArcGIS account) - add a basemap from the Basemap button"),
        "MxdServiceLayer": tr("unsupported service ({t})").format(t="ArcGIS Server"),
    }.get(t, tr("unsupported layer type ({t})").format(t=t))


class ArcGISError(Exception):
    """The file itself cannot be read."""


class _Skip(Exception):
    """One layer cannot be added; the message says why."""


@dataclass
class Report:
    source: str
    maps: list = field(default_factory=list)       # map names opened
    added: list = field(default_factory=list)      # (layer name, kind)
    skipped: list = field(default_factory=list)    # (layer name, reason)
    warnings: list = field(default_factory=list)   # "layer: message"
    layers: list = field(default_factory=list)     # QgsMapLayer objects added
    crs: QgsCoordinateReferenceSystem = None
    extent: QgsReferencedRectangle = None


def _norm(uri):
    u = (uri or "").strip()
    if u.upper().startswith("CIMPATH="):
        u = u[8:]
    return u.replace("\\", "/").lower()


def _load_json(data):
    if isinstance(data, bytes):
        data = data.decode("utf-8-sig")
    return json.loads(data)


class _Document:
    """All CIM definitions of one file, addressable by their uRI."""

    def __init__(self, path):
        self.path = os.path.abspath(path)
        self.base = os.path.dirname(self.path)
        self.defs = {}
        self.maps = []          # CIMMap dicts in document order
        self.active = None      # name of the map open in the first view
        self.top = []           # lyrx: top-level layer uris
        self.notes = []         # said once for the whole file
        ext = os.path.splitext(path)[1].lower()
        try:
            if ext == ".aprx":
                self._aprx()
            elif ext in (".mapx", ".lyrx"):
                with open(path, "rb") as f:
                    self._json_doc(_load_json(f.read()))
            elif ext in MXD_EXTENSIONS:
                self._mxd()
            else:
                raise ArcGISError(tr("This is not an ArcGIS Pro project, map or layer file."))
        except (ValueError, zipfile.BadZipFile, UnicodeDecodeError) as e:
            raise ArcGISError(tr("The file is damaged or not an ArcGIS Pro file: {e}").format(e=e)) from e

    def _add_def(self, d):
        if isinstance(d, dict) and d.get("uRI"):
            self.defs[_norm(d["uRI"])] = d

    def _aprx(self):
        with zipfile.ZipFile(self.path) as z:
            names = z.namelist()
            for n in names:
                if not n.lower().endswith(".json") or n in ("Index.json",):
                    continue
                try:
                    d = _load_json(z.read(n))
                except ValueError:
                    continue
                if n == "GISProject.json":
                    for v in d.get("views") or []:
                        if v.get("type") == "CIMMapView" and self.active is None:
                            self.active = _norm(v.get("viewableObjectPath"))
                    continue
                self.defs[_norm(n)] = d
                if d.get("type") == "CIMMap":
                    self.maps.append(d)
            if not self.maps and any(n.lower().startswith("map") and n.lower().endswith(".xml") for n in names):
                raise ArcGISError(tr("This project was saved by an old ArcGIS Pro version (1.x). Open and save it "
                                     "in ArcGIS Pro 2 or later first."))
        if self.active:
            for m in self.maps:
                if _norm(m.get("uRI")) == self.active:
                    self.active = m.get("name")
                    break
            else:
                self.active = None

    def _json_doc(self, d):
        t = d.get("type")
        if t == "CIMMapDocument":
            md = d.get("mapDefinition") or {}
            self.maps.append(md)
            self._add_def(md)
        elif t == "CIMLayerDocument":
            self.top = list(d.get("layers") or [])
        else:
            raise ArcGISError(tr("Unsupported ArcGIS document type: {t}").format(t=t))
        for key in ("layerDefinitions", "tableDefinitions"):
            for item in d.get(key) or []:
                self._add_def(item)

    # -- ArcMap documents: the layers read by mxd.py, as CIM definitions --
    def _mxd(self):
        try:
            frames = read_mxd(self.path)
        except MxdError as e:
            raise ArcGISError(tr("This ArcMap document could not be read ({e}). Open it in ArcMap or ArcGIS Pro "
                                 "and save it as an ArcGIS Pro project (.aprx) or map file (.mapx).").format(e=e)) from e
        self.notes.append(tr("ArcMap documents keep their symbology in a closed format: the layers were added "
                             "with default styles."))
        for n, frame in enumerate(frames):
            m = {"type": "CIMMap", "name": frame.name, "uRI": f"mxd/map{n}", "layers": [],
                 "spatialReference": {"wkt": frame.wkt} if frame.wkt else None}
            m["layers"] = [self._mxd_layer(lyr, f"mxd/map{n}") for lyr in frame.layers]
            self.maps.append(m)

    def _mxd_layer(self, lyr, parent):
        uri = f"{parent}/{len(self.defs)}"
        d = {"name": lyr.name, "visibility": lyr.visible, "uRI": uri}
        if lyr.kind == "group":
            d["type"] = "CIMGroupLayer"
            d["layers"] = [self._mxd_layer(c, uri) for c in lyr.children]
        elif lyr.kind == "annotation":
            d["type"] = "CIMAnnotationLayer"
        elif lyr.kind == "basemap":
            d["type"] = "MxdBasemapLayer"
        elif lyr.kind == "service":
            d["type"] = "MxdServiceLayer"
        else:
            raster = lyr.kind == "raster"
            factory, ws = self._mxd_workspace(lyr)
            dc = {"type": "CIMStandardDataConnection", "workspaceFactory": factory,
                  "workspaceConnectionString": ("URL=" if factory == "FeatureService" else "DATABASE=") + ws,
                  "dataset": lyr.dataset, "featureDataset": lyr.feature_dataset,
                  "datasetType": "esriDTRasterDataset" if raster else "esriDTFeatureClass"}
            if raster:
                d.update(type="CIMRasterLayer", dataConnection=dc)
            else:
                d.update(type="CIMFeatureLayer", featureTable={"dataConnection": dc,
                                                               "definitionExpression": lyr.definition_query})
        self.defs[uri] = d
        return uri

    def _mxd_workspace(self, lyr):
        """(factory, workspace): as stored, or where an unpacked map package keeps it."""
        ws, factory = lyr.workspace, lyr.factory
        if not ws or factory in ("FeatureService", "SDE", "XYEvent"):
            return factory, ws
        full = ws if os.path.isabs(ws) else os.path.join(self.base, ws)
        if os.path.exists(full):
            return factory, ws
        for path, fac in package_candidates(ws, factory, self.base):
            if _has_dataset(path, fac, lyr.dataset):
                return fac, path
        return factory, ws

    def get(self, uri):
        return self.defs.get(_norm(uri))


def _has_dataset(path, factory, dataset):
    """Does this workspace hold the dataset? (packaged ArcMap data)"""
    if factory == "FileGDB":
        from osgeo import gdal, ogr

        gdal.PushErrorHandler("CPLQuietErrorHandler")
        try:
            ds = ogr.Open(path)
            return ds is not None and ds.GetLayerByName(dataset) is not None
        finally:
            gdal.PopErrorHandler()
    return any(os.path.exists(os.path.join(path, dataset + ext)) for ext in ("", ".shp"))


def list_maps(path):
    """Names of the maps in an ArcGIS Pro project / map file ([] for layer files)."""
    if os.path.splitext(path)[1].lower() in PACKAGE_EXTENSIONS:
        inner = _unpack(path)
        return list_maps(inner) if inner else []
    doc = _Document(path)
    names = [m.get("name") or tr("Map") for m in doc.maps]
    if doc.active in names:  # the map the user had open first
        names.remove(doc.active)
        names.insert(0, doc.active)
    return names


# ---- packages ---------------------------------------------------------------
def _unpack(path):
    """ZIP-based package -> path of the main document inside (unpacked next to
    the package so the data stays available). None when not unpackable."""
    if not zipfile.is_zipfile(path):
        return None
    dest = os.path.splitext(path)[0] + "_unpacked"
    if not os.path.isdir(dest):
        with zipfile.ZipFile(path) as z:
            z.extractall(dest)
    found = []
    for root, _dirs, files in os.walk(dest):
        for f in files:
            ext = os.path.splitext(f)[1].lower()
            if ext in (".aprx", ".mapx", ".lyrx"):
                # newest content folder (p30 > p20), projects before maps before layers
                ver = max([int(m) for m in re.findall(r"[\\/]p(\d+)(?=[\\/]|$)", root)] or [0])
                found.append((-ver, (".aprx", ".mapx", ".lyrx").index(ext), root.count(os.sep), os.path.join(root, f)))
    return sorted(found)[0][3] if found else None


# ---- data connections -------------------------------------------------------
def _workspace(dc):
    out = {}
    for part in (dc.get("workspaceConnectionString") or "").split(";"):
        if "=" in part:
            k, v = part.split("=", 1)
            out[k.strip().upper()] = v.strip()
    return out


class _Builder:
    def __init__(self, doc, project, services):
        self.doc = doc
        self.project = project
        self.services = services
        self.report = None

    # -- paths --
    def _path(self, p, must_exist=True):
        p = (p or "").strip().strip('"')
        full = os.path.normpath(p if os.path.isabs(p) else os.path.join(self.doc.base, p))
        if os.path.exists(full) or not must_exist:
            return full
        # moved projects: look for the same name next to the document
        for cand in (os.path.join(self.doc.base, os.path.basename(full)),
                     os.path.join(os.path.dirname(self.doc.base), os.path.basename(full))):
            if os.path.exists(cand):
                return cand
        raise _Skip(tr("data not found: {path}").format(path=full))

    def _file_in(self, folder, dataset, default_ext=""):
        name = dataset if os.path.splitext(dataset)[1] or not default_ext else dataset + default_ext
        return self._path(os.path.join(folder, name))

    # -- one data connection -> (provider, uri, kind) --
    def source(self, dc):
        t = dc.get("type")
        if t == "CIMRelQueryTableDataConnection":
            self.warn(tr("joined fields are not kept"))
            return self.source(dc.get("sourceTable") or {})
        if t not in ("CIMStandardDataConnection", "CIMFeatureDatasetDataConnection", "CIMSqlQueryDataConnection"):
            raise _Skip(tr("unsupported data connection ({t})").format(t=t or "?"))
        factory = dc.get("workspaceFactory") or ""
        ws = _workspace(dc)
        dataset = dc.get("dataset") or ""
        dtype = dc.get("datasetType") or ""
        if factory == "FileGDB":
            gdb = self._path(ws.get("DATABASE", ""))
            if dtype == "esriDTRasterDataset":
                return "gdal", f'OpenFileGDB:"{gdb}":{dataset}', "raster"
            return "ogr", f"{gdb}|layername={dataset}", "table" if dtype == "esriDTTable" else "vector"
        if factory == "Shapefile":
            return "ogr", self._file_in(ws.get("DATABASE", ""), dataset, ".shp"), "vector"
        if factory == "Raster":
            return "gdal", self._file_in(ws.get("DATABASE", ""), dataset), "raster"
        if factory in ("Sql", "SQLite", "GeoPackage"):
            db = self._path(ws.get("DATABASE", ""))
            name = re.sub(r'^main\.', "", dataset).replace("%", "").strip('"')
            return "ogr", f"{db}|layername={name}", "vector"
        if factory == "Excel":
            db = self._path(ws.get("DATABASE", ""))
            return "ogr", f"{db}|layername={dataset.rstrip('$')}", "table"
        if factory == "TextFile":
            return "ogr", self._file_in(ws.get("DATABASE", ""), dataset), "table"
        if factory == "Cad":
            drawing = self._file_in(ws.get("DATABASE", ""), dc.get("featureDataset") or dataset)
            if not drawing.lower().endswith(".dxf"):
                raise _Skip(tr("DWG drawings cannot be read directly: save the drawing as DXF and add it"))
            geom = {"Polygon": "Polygon", "Polyline": "LineString", "Point": "Point",
                    "Annotation": "Point", "MultiPatch": "Polygon"}.get(dataset, "")
            uri = f"{drawing}|layername=entities" + (f"|geometrytype={geom}" if geom else "")
            return "ogr", uri, "vector"
        if factory == "FeatureService":
            url = ws.get("URL", "").rstrip("/")
            if not url:
                raise _Skip(tr("feature service without an address"))
            uri = QgsDataSourceUri()
            uri.setParam("url", f"{url}/{dataset}" if dataset else url)
            return "arcgisfeatureserver", uri.uri(False), "service"
        if factory == "Access":  # ArcMap personal geodatabase (.mdb), read by GDAL's PGeo driver
            db = self._path(ws.get("DATABASE", ""))
            return "ogr", f"{db}|layername={dataset}", "vector"
        if factory == "XYEvent":
            raise _Skip(tr("points made from the X/Y columns of a table ({table}): open the table with "
                           "Excel / CSV to points").format(table=dataset or "?"))
        if factory == "SDE":
            raise _Skip(tr("enterprise geodatabase (SDE): connect to the database from Data > PostGIS or "
                           "SQL Server and add the table"))
        raise _Skip(tr("unsupported workspace type ({t})").format(t=factory or "?"))

    def service(self, cim):
        """Map/tile/image/vector-tile service layers -> (provider, uri, kind)."""
        conn = cim.get("serviceConnection") or cim.get("dataConnection") or {}
        t = conn.get("type", "")
        url = conn.get("url") or conn.get("uRI") or (conn.get("serverConnection") or {}).get("url") or ""
        if _ESRI_BASEMAP_HOSTS.search(url) or (cim.get("layerType", "").startswith("Basemap")
                                               and "arcgis.com" in url.lower()):
            raise _Skip(tr("Esri basemap (needs an ArcGIS account) - add a basemap from the Basemap button"))
        if t == "CIMAGSServiceConnection":
            uri = QgsDataSourceUri()
            uri.setParam("url", url)
            return "arcgismapserver", uri.uri(False), "service"
        if t == "CIMStandardServiceConnection" and "{" in url:
            tiles = url.replace("{level}", "{z}").replace("{col}", "{x}").replace("{row}", "{y}")
            uri = QgsDataSourceUri()
            uri.setParam("type", "xyz")
            uri.setParam("url", tiles)
            return "wms", bytes(uri.encodedUri()).decode(), "tiles"
        if t == "CIMVectorTileDataConnection":
            if url.lower().endswith(".vtpk"):
                return "vectortile", f"type=vtpk&url={self._path(url)}", "tiles"
            return "vectortile", f"serviceType=arcgis&type=xyz&url={url}", "tiles"
        if t in ("CIMWMTSServiceConnection", "CIMWMSServiceConnection"):
            kind = "WMTS" if "WMTS" in t else "WMS"
            raise _Skip(tr("{kind} service: add it from Data > WMS / WMTS with this address: {url}")
                        .format(kind=kind, url=url))
        raise _Skip(tr("unsupported service ({t})").format(t=t or "?"))

    # -- layers --
    def warn(self, text):
        if self._current and text:
            self.report.warnings.append(f"{self._current}: {text}")

    def make_layer(self, cim):
        """CIM layer definition -> valid QgsMapLayer (raises _Skip)."""
        t = cim.get("type")
        name = cim.get("name") or tr("Layer")
        if t in _UNSUPPORTED:
            raise _Skip(_unsupported_reason(t))
        if t in ("CIMFeatureLayer", "CIMStandaloneTable", "CIMRasterLayer", "CIMBasicFeatureLayer"):
            dc = (cim.get("featureTable") or {}).get("dataConnection") or cim.get("dataConnection") or {}
            provider, uri, kind = self.source(dc)
        elif t in ("CIMTiledServiceLayer", "CIMMapServiceLayer", "CIMImageServiceLayer", "CIMVectorTileLayer",
                   "CIMWMSLayer", "CIMWMTSLayer", "CIMTiledServiceSubLayer"):
            provider, uri, kind = self.service(cim)
        else:
            raise _Skip(tr("unsupported layer type ({t})").format(t=t or "?"))
        if kind in ("service",) or provider in ("arcgismapserver", "arcgisfeatureserver"):
            if not self.services:
                raise _Skip(tr("online service not opened"))
        if provider in ("ogr", "arcgisfeatureserver"):
            layer = QgsVectorLayer(uri, name, provider)
        elif provider == "vectortile":
            layer = QgsVectorTileLayer(uri, name)
        else:
            layer = QgsRasterLayer(uri, name, provider)
        if not layer.isValid():
            if provider.startswith("arcgis"):
                raise _Skip(tr("the service could not be reached or needs a sign-in"))
            if ".mdb|" in uri.lower():
                raise _Skip(tr("personal geodatabase (.mdb): install the Microsoft Access Database Engine (64-bit) "
                               "or copy the data into a file geodatabase"))
            raise _Skip(tr("the data could not be opened ({uri})").format(uri=uri))
        conv = Converter()
        try:
            conv.apply_layer(layer, cim)
        except Exception as e:  # styling must never lose the layer
            conv.warn(tr("symbology not converted ({e})").format(e=e))
        for w in conv.warnings:
            self.warn(w)
        layer.setCustomProperty("biruni/arcgis_source", self.doc.path)
        return layer, kind

    def add_tree(self, uris, group, index=None):
        """Add layer definitions under group, keeping ArcGIS's top-down order."""
        pos = index
        for uri in uris:
            cim = self.doc.get(uri)
            if cim is None:
                self.report.skipped.append((uri, tr("layer definition missing from the file")))
                continue
            t = cim.get("type")
            name = cim.get("name") or tr("Layer")
            self._current = name
            if t in _IGNORED:
                continue
            if t == "CIMGroupLayer":
                sub = QgsLayerTreeGroup(name)
                sub.setItemVisibilityChecked(bool(cim.get("visibility", True)))
                sub.setExpanded(bool(cim.get("expanded", False)))
                if pos is None:
                    group.addChildNode(sub)
                else:
                    group.insertChildNode(pos, sub)
                    pos += 1
                self.add_tree(cim.get("layers") or [], sub)
                if cim.get("transparency"):
                    self.warn(tr("group transparency is not kept"))
                continue
            try:
                layer, kind = self.make_layer(cim)
            except _Skip as e:
                self.report.skipped.append((name, str(e)))
                continue
            self.project.addMapLayer(layer, False)
            if pos is None:
                node = group.addLayer(layer)
            else:
                node = group.insertLayer(pos, layer)
                pos += 1
            node.setItemVisibilityChecked(bool(cim.get("visibility", True)))
            node.setExpanded(bool(cim.get("expanded", False)))
            self.report.added.append((name, kind))
            self.report.layers.append(layer)

    _current = None


def _map_extent(m):
    ext = m.get("defaultExtent") or {}
    try:
        rect = QgsRectangle(float(ext["xmin"]), float(ext["ymin"]), float(ext["xmax"]), float(ext["ymax"]))
    except (KeyError, TypeError, ValueError):
        return None
    if rect.isEmpty() or not all(map(lambda v: abs(v) < 1e300, (rect.xMinimum(), rect.yMaximum()))):
        return None
    crs = crs_from_spatial_reference(ext.get("spatialReference") or m.get("spatialReference"))
    return QgsReferencedRectangle(rect, crs)


def open_arcgis_file(path, project=None, map_name=None, services=True):
    """Add the content of an ArcGIS Pro file to a QGIS project; returns a Report.

    map_name: a map name, a list of names, "*" for every map, or None for the
    map that was open in ArcGIS Pro (the first one). Layer files (.lyrx) are
    added at the top of the layer list. Raises ArcGISError for unreadable files."""
    project = project or QgsProject.instance()
    report = Report(source=path)
    if os.path.splitext(path)[1].lower() in PACKAGE_EXTENSIONS:
        inner = _unpack(path)
        if inner is None:
            raise ArcGISError(tr("This package is compressed with 7-Zip: unpack it with 7-Zip and open the .aprx, "
                                 ".mapx or .lyrx file inside."))
        inner_report = open_arcgis_file(inner, project, map_name, services)
        inner_report.source = path
        return inner_report
    doc = _Document(path)
    builder = _Builder(doc, project, services)
    builder.report = report
    report.warnings.extend(f"{os.path.basename(path)}: {note}" for note in doc.notes)
    root = project.layerTreeRoot()
    was_empty = not project.mapLayers()

    if doc.top:  # layer file
        builder.add_tree(doc.top, root, index=0)
        return report

    if not doc.maps:
        raise ArcGISError(tr("The file contains no maps."))
    by_name = {m.get("name"): m for m in doc.maps}
    if map_name == "*":
        chosen = doc.maps
    elif isinstance(map_name, (list, tuple)):
        chosen = [by_name[n] for n in map_name if n in by_name]
    elif map_name:
        if map_name not in by_name:
            raise ArcGISError(tr("The file has no map named {name}.").format(name=map_name))
        chosen = [by_name[map_name]]
    else:
        chosen = [by_name.get(doc.active) or doc.maps[0]]

    for m in chosen:
        name = m.get("name") or tr("Map")
        report.maps.append(name)
        if was_empty and len(chosen) == 1:
            group = root
        else:
            group = root.insertGroup(0, name)
            group.setExpanded(True)
        builder.add_tree(m.get("layers") or [], group)
        builder.add_tree(m.get("standaloneTables") or [], group)
        if report.crs is None:
            crs = crs_from_spatial_reference(m.get("spatialReference"))
            if crs.isValid():
                report.crs = crs
            report.extent = _map_extent(m)
    if was_empty and report.crs is not None:
        project.setCrs(report.crs)
    if was_empty and report.extent is not None:
        project.viewSettings().setDefaultViewExtent(report.extent)
    return report
