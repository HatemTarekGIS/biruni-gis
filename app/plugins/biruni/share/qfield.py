"""Field copies for QField (free, open-source field app for Android/iOS).

Send:   the saved project is copied into a field folder - every vector layer
        (project database, files, temporary layers, services) becomes a table
        of one GeoPackage, rasters are copied, and an offline basemap can be
        rendered to MBTiles. A manifest remembers a fingerprint of every
        feature, and a hidden column (biruni_src) ties each copied feature to
        the original one.
Return: the field folder (or its GeoPackage, or a ZIP of it) is compared with
        the manifest: features added, modified and deleted in the field are
        listed per layer, together with conflicts - features that were also
        changed or deleted in the office after the copy was made. Applying
        the changes happens in an edit session, so it can be reviewed and
        undone before saving.
"""
import hashlib
import json
import math
import os
import shutil
import tempfile
import zipfile
from dataclasses import dataclass, field
from datetime import datetime

from qgis.core import (Qgis, QgsCoordinateReferenceSystem, QgsCoordinateTransform, QgsEditorWidgetSetup, QgsFeature,
                       QgsFeatureRequest, QgsField, QgsFields, QgsGeometry, QgsProject, QgsProviderRegistry,
                       QgsRasterLayer, QgsRectangle, QgsReferencedRectangle, QgsVectorFileWriter, QgsVectorLayer,
                       QgsVectorLayerUtils)
from qgis.PyQt.QtCore import QMetaType

from ..i18n import tr
from .webmap import json_value

MANIFEST = "biruni_field.json"
SRC_FIELD = "biruni_src"


# ---- fingerprints -----------------------------------------------------------
def _values(feature, names):
    fields = feature.fields()
    return [json_value(feature.attribute(n)) if fields.indexOf(n) >= 0 else None for n in names]


def feature_hash(feature, names):
    h = hashlib.sha1()
    g = feature.geometry()
    if g is not None and not g.isNull():
        h.update(bytes(g.asWkb()))
    h.update(b"|")
    h.update(json.dumps(_values(feature, names), ensure_ascii=False, default=str).encode("utf-8"))
    return h.hexdigest()


def _same(a, b):
    if a in (None, "") and b in (None, ""):
        return True
    if isinstance(a, (int, float)) and isinstance(b, (int, float)) and not isinstance(a, bool):
        return abs(a - b) <= 1e-9 * max(1.0, abs(a), abs(b))
    return a == b


def _same_geometry(a, b):
    a_null = a is None or a.isNull() or a.isEmpty()
    b_null = b is None or b.isNull() or b.isEmpty()
    if a_null or b_null:
        return a_null == b_null
    try:
        if hasattr(a, "isTopologicallyEqual"):  # QGIS >= 4.2
            return a.isTopologicallyEqual(b) or a.isExactlyEqual(b)
        return a.isGeosEqual(b) or a.equals(b)
    except Exception:
        return bytes(a.asWkb()) == bytes(b.asWkb())


# ---- helpers ---------------------------------------------------------------
def _progress(feedback, value, text=None):
    if feedback is None:
        return
    if text and hasattr(feedback, "pushInfo"):
        feedback.pushInfo(text)
    feedback.setProgress(value)


def _cancelled(feedback):
    return feedback is not None and feedback.isCanceled()


def unique_folder(parent, name):
    cand, k = os.path.join(parent, name), 2
    while os.path.exists(cand) and os.listdir(cand):
        cand, k = os.path.join(parent, f"{name} ({k})"), k + 1
    return cand


def _table_name(name, used):
    import re

    base = re.sub(r"[^\w]+", "_", name, flags=re.UNICODE).strip("_") or "layer"
    cand, k = base, 2
    while cand.lower() in used:
        cand, k = f"{base}_{k}", k + 1
    used.add(cand.lower())
    return cand


def _kept_fields(layer):
    """(index, QgsField) copied to the field table: everything but the
    provider's own key column and an earlier tracking column."""
    pk = set(layer.dataProvider().pkAttributeIndexes()) if layer.providerType() == "ogr" else set()
    out = []
    for i, f in enumerate(layer.fields()):
        if i in pk or f.name().lower() in ("fid", SRC_FIELD):
            continue
        out.append((i, f))
    return out


def _layer_path(layer):
    parts = QgsProviderRegistry.instance().decodeUri(layer.providerType(), layer.source())
    path = parts.get("path") or ""
    return path if path and os.path.exists(path) else None


def estimate_tiles(extent_wgs84, zmin, zmax):
    """Number of XYZ tiles that cover a lon/lat rectangle between two zooms."""
    def tile(lon, lat, z):
        lat = max(min(lat, 85.0511), -85.0511)
        n = 2 ** z
        x = int((lon + 180) / 360 * n)
        y = int((1 - math.asinh(math.tan(math.radians(lat))) / math.pi) / 2 * n)
        return min(max(x, 0), n - 1), min(max(y, 0), n - 1)
    total = 0
    for z in range(int(zmin), int(zmax) + 1):
        x0, y0 = tile(extent_wgs84.xMinimum(), extent_wgs84.yMaximum(), z)
        x1, y1 = tile(extent_wgs84.xMaximum(), extent_wgs84.yMinimum(), z)
        total += (x1 - x0 + 1) * (y1 - y0 + 1)
    return total


ONLINE_PROVIDERS = ("wms", "xyz", "arcgismapserver", "wcs")


def online_basemaps(project):
    """Raster layers the field copy can only show with the internet."""
    return [lyr for lyr in project.mapLayers().values()
            if isinstance(lyr, QgsRasterLayer) and lyr.providerType() in ONLINE_PROVIDERS]


def data_extent(project):
    """Combined extent of the project's vector layers, in lon/lat."""
    wgs = QgsCoordinateReferenceSystem("EPSG:4326")
    total = QgsRectangle()
    total.setNull()
    for lyr in project.mapLayers().values():
        if isinstance(lyr, QgsVectorLayer) and lyr.isSpatial() and lyr.featureCount() and lyr.crs().isValid():
            r = QgsCoordinateTransform(lyr.crs(), wgs, project.transformContext()).transformBoundingBox(lyr.extent())
            total.combineExtentWith(r)
    return None if total.isNull() else QgsReferencedRectangle(total, wgs)


# ---- send ------------------------------------------------------------------
def _write_table(src, gpkg, table, first, transform_context, request):
    kept = _kept_fields(src)
    fields = QgsFields()
    for _i, f in kept:
        fields.append(QgsField(f))
    fields.append(QgsField(SRC_FIELD, QMetaType.Type.LongLong))
    opts = QgsVectorFileWriter.SaveVectorOptions()
    opts.driverName = "GPKG"
    opts.layerName = table
    opts.fileEncoding = "UTF-8"
    opts.actionOnExistingFile = (QgsVectorFileWriter.ActionOnExistingFile.CreateOrOverwriteFile if first
                                 else QgsVectorFileWriter.ActionOnExistingFile.CreateOrOverwriteLayer)
    writer = QgsVectorFileWriter.create(gpkg, fields, src.wkbType(), src.crs(), transform_context, opts)
    if writer is None or writer.hasError() != QgsVectorFileWriter.WriterError.NoError:
        raise RuntimeError(f"{src.name()}: {writer.errorMessage() if writer else 'writer'}")
    src_hashes = {}
    names = [f.name() for _i, f in kept]
    for f in src.getFeatures(request):
        out = QgsFeature(fields)
        out.setGeometry(f.geometry())
        out.setAttributes([f.attribute(i) for i, _f in kept] + [f.id()])
        if not writer.addFeature(out):
            raise RuntimeError(f"{src.name()}: {writer.errorMessage()}")
        src_hashes[f.id()] = feature_hash(f, names)
    del writer
    return names, src_hashes


def package_for_qfield(project, out_dir, extent=None, basemap_zoom=None, feedback=None, make_zip=False,
                       folder_name=None):
    """Copy the saved project into a QField folder; returns the folder (or the
    ZIP when make_zip). extent: QgsReferencedRectangle limiting the copied
    features and the offline basemap; basemap_zoom: (min, max) to render an
    offline basemap from the project's online basemaps (XYZ/WMS layers).
    The project must be saved: the copy is made from the project file."""
    if not project.fileName() or not os.path.exists(project.fileName()):
        raise ValueError(tr("Save the project first."))
    area = None
    if basemap_zoom:  # check before anything is written
        if not online_basemaps(project):
            raise ValueError(tr("The project has no online basemap to take offline."))
        area = extent or data_extent(project)
        if area is None:
            raise ValueError(tr("The project has no data to take the extent from."))
        wgs = QgsCoordinateTransform(area.crs(), QgsCoordinateReferenceSystem("EPSG:4326"),
                                     project.transformContext()).transformBoundingBox(area)
        n = estimate_tiles(wgs, *basemap_zoom)
        if n > MAX_TILES:
            raise ValueError(tr("The offline basemap would need {n} tiles (at most {m}). Choose a smaller area or a "
                                "lower maximum zoom.").format(n=n, m=MAX_TILES))
    _progress(feedback, 2, tr("Reading the project…"))
    clone = QgsProject()
    if not clone.read(project.fileName()):
        raise RuntimeError(clone.error() or project.fileName())
    if set(clone.mapLayers()) != set(project.mapLayers()):
        clone.clear()
        raise ValueError(tr("Save the project first."))
    base = os.path.splitext(os.path.basename(project.fileName()))[0]
    folder = unique_folder(out_dir, folder_name or f"{base} - field")
    os.makedirs(os.path.join(folder, "data"), exist_ok=True)
    gpkg = os.path.join(folder, base + ".gpkg")
    qgz = os.path.join(folder, base + ".qgz")
    try:
        return _package(project, clone, folder, gpkg, qgz, extent, area, basemap_zoom, feedback, make_zip)
    except BaseException:
        shutil.rmtree(folder, ignore_errors=True)  # never leave a half-made field copy behind
        raise


def _package(project, clone, folder, gpkg, qgz, extent, area, basemap_zoom, feedback, make_zip):
    manifest = {"format": 1, "created": datetime.now().isoformat(timespec="seconds"),
                "project": os.path.abspath(project.fileName()), "gpkg": os.path.basename(gpkg), "layers": {},
                "skipped": []}
    used, first = set(), True
    layers = list(clone.mapLayers().values())
    online_rasters = []
    try:
        for n, lyr in enumerate(layers):
            if _cancelled(feedback):
                raise RuntimeError(tr("Cancelled."))
            _progress(feedback, 5 + 70 * n / max(1, len(layers)), lyr.name())
            live = project.mapLayer(lyr.id())
            src = live if live is not None and live.isValid() else lyr
            if isinstance(lyr, QgsVectorLayer):
                if not src.isValid():
                    manifest["skipped"].append(lyr.name())
                    continue
                request = QgsFeatureRequest()
                if extent is not None and src.isSpatial():
                    request.setFilterRect(QgsCoordinateTransform(extent.crs(), src.crs(), project.transformContext())
                                          .transformBoundingBox(extent))
                table = _table_name(lyr.name(), used)
                names, src_hashes = _write_table(src, gpkg, table, first, project.transformContext(), request)
                first = False
                field_layer = QgsVectorLayer(f"{gpkg}|layername={table}", table, "ogr")
                fids = {}
                for f in field_layer.getFeatures():
                    s = f.attribute(SRC_FIELD)
                    fids[str(s)] = [feature_hash(f, names), src_hashes.get(s)]
                del field_layer
                manifest["layers"][lyr.id()] = {"name": lyr.name(), "table": table, "fields": names,
                                                "features": fids}
                lyr.setDataSource(f"{gpkg}|layername={table}", lyr.name(), "ogr")
                idx = lyr.fields().indexOf(SRC_FIELD)
                if idx >= 0:
                    lyr.setEditorWidgetSetup(idx, QgsEditorWidgetSetup("Hidden", {}))
            elif isinstance(lyr, QgsRasterLayer):
                path = _layer_path(lyr) if lyr.providerType() == "gdal" else None
                if path:
                    new = _copy_with_sidecars(path, os.path.join(folder, "data"))
                    lyr.setDataSource(new, lyr.name(), "gdal")
                elif lyr.providerType() in ONLINE_PROVIDERS:
                    online_rasters.append(lyr)
                else:
                    manifest["skipped"].append(lyr.name())

        if basemap_zoom:
            _progress(feedback, 78, tr("Rendering the offline basemap…"))
            mbtiles = os.path.join(folder, "data", "basemap.mbtiles")
            render_basemap(project, online_rasters, area, basemap_zoom, mbtiles, feedback)
            bm = QgsRasterLayer(mbtiles, tr("Offline basemap"), "gdal")
            if bm.isValid():
                clone.addMapLayer(bm, False)
                clone.layerTreeRoot().addLayer(bm)  # bottom
                for r in online_rasters:
                    node = clone.layerTreeRoot().findLayer(r.id())
                    if node is not None:
                        node.setItemVisibilityChecked(False)
                manifest["basemap"] = "data/basemap.mbtiles"

        _progress(feedback, 92, tr("Writing the field project…"))
        clone.setFilePathStorage(Qgis.FilePathType.Relative)
        clone.writeEntry("Biruni", "/field-copy", True)
        if not clone.write(qgz):
            raise RuntimeError(clone.error())
    finally:
        clone.clear()
        del clone
    with open(os.path.join(folder, MANIFEST), "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=1)
    _progress(feedback, 100)
    if make_zip:
        zpath = folder + ".zip"
        with zipfile.ZipFile(zpath, "w", zipfile.ZIP_DEFLATED) as z:
            for root, _dirs, files in os.walk(folder):
                for fn in files:
                    full = os.path.join(root, fn)
                    z.write(full, os.path.relpath(full, os.path.dirname(folder)))
        return zpath
    return folder


def _copy_with_sidecars(path, dest_dir):
    src_dir, fname = os.path.split(path)
    stem = os.path.splitext(fname)[0]
    for f in os.listdir(src_dir):
        if f == fname or f.startswith(stem + "."):
            target = os.path.join(dest_dir, f)
            if not os.path.exists(target):
                shutil.copy2(os.path.join(src_dir, f), target)
    return os.path.join(dest_dir, fname)


MAX_TILES = 20000
_ORIGIN = 20037508.342789244
_META = 4  # tiles per side rendered at once


def _tile_range(extent_3857, z):
    size = 2 * _ORIGIN / 2 ** z
    n = 2 ** z

    def clamp(v):
        return min(max(int(v), 0), n - 1)
    x0 = clamp((extent_3857.xMinimum() + _ORIGIN) / size)
    x1 = clamp((extent_3857.xMaximum() + _ORIGIN) / size)
    y0 = clamp((_ORIGIN - extent_3857.yMaximum()) / size)
    y1 = clamp((_ORIGIN - extent_3857.yMinimum()) / size)
    return x0, x1, y0, y1, size


def render_basemap(project, layers, extent, zoom, out_file, feedback=None, max_tiles=MAX_TILES):
    """Render `layers` (the project's online basemaps, or any layers) over
    `extent` into an MBTiles file that QField shows without the internet.

    Rendering happens here tile block by tile block (the QGIS "XYZ tiles"
    algorithm blocks when it runs in the main thread)."""
    import sqlite3

    from qgis.core import QgsMapRendererCustomPainterJob, QgsMapSettings
    from qgis.PyQt.QtCore import QBuffer, QByteArray, QIODevice, QSize
    from qgis.PyQt.QtGui import QColor, QImage, QPainter

    if extent is None:
        raise ValueError(tr("The project has no data to take the extent from."))
    if not layers:
        raise ValueError(tr("The project has no online basemap to take offline."))
    wgs = QgsCoordinateTransform(extent.crs(), QgsCoordinateReferenceSystem("EPSG:4326"),
                                 project.transformContext()).transformBoundingBox(extent)
    total = estimate_tiles(wgs, zoom[0], zoom[1])
    if total > max_tiles:
        raise ValueError(tr("The offline basemap would need {n} tiles (at most {m}). Choose a smaller area or a "
                            "lower maximum zoom.").format(n=total, m=max_tiles))
    merc = QgsCoordinateReferenceSystem("EPSG:3857")
    r3857 = QgsCoordinateTransform(extent.crs(), merc, project.transformContext()).transformBoundingBox(extent)
    if os.path.exists(out_file):
        os.remove(out_file)
    db = sqlite3.connect(out_file)
    db.executescript("CREATE TABLE metadata (name text, value text);"
                     "CREATE TABLE tiles (zoom_level integer, tile_column integer, tile_row integer, tile_data blob);"
                     "CREATE UNIQUE INDEX tile_index on tiles (zoom_level, tile_column, tile_row);")
    def credit(lyr):
        try:
            return lyr.serverProperties().attribution() or "; ".join(lyr.metadata().rights())
        except Exception:
            return ""
    attribution = "; ".join(c for c in (credit(lyr) for lyr in layers) if c)
    meta = {"name": "Offline basemap", "format": "jpg", "type": "baselayer", "version": "1.1",
            "minzoom": str(int(zoom[0])), "maxzoom": str(int(zoom[1])),
            "bounds": f"{wgs.xMinimum()},{wgs.yMinimum()},{wgs.xMaximum()},{wgs.yMaximum()}",
            "attribution": attribution}
    db.executemany("INSERT INTO metadata VALUES (?, ?)", meta.items())
    ms = QgsMapSettings()
    ms.setLayers(layers)
    ms.setDestinationCrs(merc)
    ms.setTransformContext(project.transformContext())
    ms.setOutputDpi(96)
    ms.setBackgroundColor(QColor(255, 255, 255))
    ms.setFlag(Qgis.MapSettingsFlag.Antialiasing, True)
    done = 0
    try:
        for z in range(int(zoom[0]), int(zoom[1]) + 1):
            x0, x1, y0, y1, size = _tile_range(r3857, z)
            for bx in range(x0, x1 + 1, _META):
                for by in range(y0, y1 + 1, _META):
                    if _cancelled(feedback):
                        raise RuntimeError(tr("Cancelled."))
                    nx, ny = min(_META, x1 - bx + 1), min(_META, y1 - by + 1)
                    ms.setOutputSize(QSize(256 * nx, 256 * ny))
                    ms.setExtent(QgsRectangle(-_ORIGIN + bx * size, _ORIGIN - (by + ny) * size,
                                              -_ORIGIN + (bx + nx) * size, _ORIGIN - by * size))
                    img = QImage(256 * nx, 256 * ny, QImage.Format.Format_RGB32)
                    img.fill(QColor(255, 255, 255))
                    p = QPainter(img)
                    job = QgsMapRendererCustomPainterJob(ms, p)
                    job.renderSynchronously()
                    p.end()
                    rows = []
                    for i in range(nx):
                        for j in range(ny):
                            ba = QByteArray()
                            buf = QBuffer(ba)
                            buf.open(QIODevice.OpenModeFlag.WriteOnly)
                            img.copy(i * 256, j * 256, 256, 256).save(buf, "JPG", 80)
                            buf.close()
                            rows.append((z, bx + i, (2 ** z - 1) - (by + j), bytes(ba)))
                    db.executemany("INSERT OR REPLACE INTO tiles VALUES (?, ?, ?, ?)", rows)
                    done += len(rows)
                    _progress(feedback, 78 + 14 * done / max(1, total))
        db.commit()
    finally:
        db.close()
    return out_file


# ---- return ----------------------------------------------------------------
@dataclass
class LayerDiff:
    layer_id: str
    name: str
    table: str
    fields: list
    added: list = field(default_factory=list)       # field-copy fids
    modified: list = field(default_factory=list)    # {"src", "fid", "fields": [...], "geometry": bool}
    deleted: list = field(default_factory=list)     # original fids
    conflicts: list = field(default_factory=list)   # {"src", "fid", "kind": modified|deleted, "reason"}
    error: str = ""

    def count(self):
        return len(self.added) + len(self.modified) + len(self.deleted) + len(self.conflicts)


@dataclass
class Diff:
    folder: str
    gpkg: str
    manifest: dict
    layers: list

    def count(self):
        return sum(d.count() for d in self.layers)

    def already_applied(self):
        return self.manifest.get("applied")


def _resolve(returned):
    """(folder, manifest path) for a returned field folder, GeoPackage or ZIP."""
    if os.path.isdir(returned):
        folder = returned
    elif returned.lower().endswith(".zip"):
        folder = os.path.join(tempfile.mkdtemp(prefix="biruni-field-"))
        with zipfile.ZipFile(returned) as z:
            z.extractall(folder)
        for root, _dirs, files in os.walk(folder):
            if MANIFEST in files:
                folder = root
                break
    else:
        folder = os.path.dirname(returned)
    man = os.path.join(folder, MANIFEST)
    if not os.path.exists(man):
        raise ValueError(tr("This is not a field copy made by Biruni GIS (no {m} found).").format(m=MANIFEST))
    return folder, man


def compare_field_copy(project, returned):
    folder, man = _resolve(returned)
    with open(man, encoding="utf-8") as f:
        manifest = json.load(f)
    gpkg = os.path.join(folder, manifest["gpkg"])
    if os.path.isfile(returned) and returned.lower().endswith(".gpkg"):
        gpkg = returned
    out = []
    for lid, entry in manifest["layers"].items():
        d = LayerDiff(lid, entry["name"], entry["table"], entry["fields"])
        out.append(d)
        target = project.mapLayer(lid)
        if target is None:
            same = project.mapLayersByName(entry["name"])
            target = same[0] if len(same) == 1 else None
        if target is None:
            d.error = tr("The layer is not in the project any more.")
            continue
        d.layer_id = target.id()
        fl = QgsVectorLayer(f"{gpkg}|layername={entry['table']}", entry["table"], "ogr")
        if not fl.isValid():
            d.error = tr("The table {t} is missing from the field copy.").format(t=entry["table"])
            continue
        known = entry["features"]
        names = entry["fields"]
        seen = set()
        req = QgsFeatureRequest()
        req.addOrderBy("$id")
        for f in fl.getFeatures(req):
            s = f.attribute(SRC_FIELD)
            key = None if s is None else str(int(s))
            if key is None or key not in known or key in seen:
                d.added.append(f.id())
                continue
            seen.add(key)
            field_hash, src_hash = known[key]
            if feature_hash(f, names) == field_hash:
                continue
            src = int(key)
            cur = target.getFeature(src)
            if not cur.isValid():
                d.conflicts.append({"src": src, "fid": f.id(), "kind": "modified",
                                    "reason": tr("deleted in the office")})
                continue
            changed = [n for n in names if target.fields().indexOf(n) >= 0
                       and not _same(json_value(f.attribute(n)), json_value(cur.attribute(n)))]
            geom = not _same_geometry(f.geometry(), cur.geometry())
            if not changed and not geom:
                continue
            item = {"src": src, "fid": f.id(), "fields": changed, "geometry": geom}
            if feature_hash(cur, names) != src_hash:
                item.update(kind="modified", reason=tr("also changed in the office"))
                d.conflicts.append(item)
            else:
                d.modified.append(item)
        for key, (_fh, src_hash) in known.items():
            if key in seen:
                continue
            src = int(key)
            cur = target.getFeature(src)
            if not cur.isValid():
                continue  # deleted on both sides
            if feature_hash(cur, names) != src_hash:
                d.conflicts.append({"src": src, "fid": None, "kind": "deleted",
                                    "reason": tr("changed in the office")})
            else:
                d.deleted.append(src)
        del fl
    return Diff(folder, gpkg, manifest, out)


def _coerce(geom, layer):
    if geom is None or geom.isNull():
        return QgsGeometry()
    try:
        parts = geom.coerceToType(layer.wkbType())
        if parts:
            return parts[0]
    except Exception:
        pass
    return QgsGeometry(geom)


def apply_field_changes(project, diff, feedback=None, include_conflicts=False, commit=False):
    """Apply a Diff to the project layers inside one edit command per layer.
    The layers stay in edit mode (review, then Save Edits - or Undo) unless
    commit is True. Returns {"added", "modified", "deleted"} counts."""
    counts = {"added": 0, "modified": 0, "deleted": 0}
    todo = [d for d in diff.layers if not d.error and d.count()]
    for n, d in enumerate(todo):
        _progress(feedback, 100 * n / max(1, len(todo)), d.name)
        lyr = project.mapLayer(d.layer_id)
        fl = QgsVectorLayer(f"{diff.gpkg}|layername={d.table}", d.table, "ogr")
        if lyr is None or not fl.isValid():
            continue
        if not lyr.isEditable() and not lyr.startEditing():
            raise RuntimeError(tr("{n} cannot be edited.").format(n=lyr.name()))
        added = list(d.added)
        modified = list(d.modified)
        deleted = list(d.deleted)
        if include_conflicts:  # the field version wins
            for c in d.conflicts:
                if c["kind"] == "deleted":
                    deleted.append(c["src"])
                elif lyr.getFeature(c["src"]).isValid():
                    modified.append({"src": c["src"], "fid": c["fid"], "geometry": True,
                                     "fields": [x for x in d.fields if lyr.fields().indexOf(x) >= 0]})
                else:
                    added.append(c["fid"])
        pk = set(lyr.dataProvider().pkAttributeIndexes()) if lyr.providerType() == "ogr" else set()
        lyr.beginEditCommand(tr("Changes from the field"))
        try:
            for fid in added:
                ff = fl.getFeature(fid)
                attrs = {}
                for name in d.fields:
                    i = lyr.fields().indexOf(name)
                    if i >= 0 and i not in pk:
                        attrs[i] = ff.attribute(name)
                nf = QgsVectorLayerUtils.createFeature(lyr, _coerce(ff.geometry(), lyr), attrs,
                                                       lyr.createExpressionContext())
                if lyr.addFeature(nf):
                    counts["added"] += 1
            for m in modified:
                ff = fl.getFeature(m["fid"])
                if m["geometry"]:
                    lyr.changeGeometry(m["src"], _coerce(ff.geometry(), lyr))
                vals = {lyr.fields().indexOf(x): ff.attribute(x) for x in m["fields"]
                        if lyr.fields().indexOf(x) >= 0}
                if vals:
                    lyr.changeAttributeValues(m["src"], vals)
                counts["modified"] += 1
            for src in deleted:
                if lyr.deleteFeature(src):
                    counts["deleted"] += 1
        except Exception:
            lyr.destroyEditCommand()
            raise
        lyr.endEditCommand()
        if commit and not lyr.commitChanges():
            raise RuntimeError("; ".join(lyr.commitErrors()))
        del fl
    diff.manifest["applied"] = datetime.now().isoformat(timespec="seconds")
    try:
        with open(os.path.join(diff.folder, MANIFEST), "w", encoding="utf-8") as f:
            json.dump(diff.manifest, f, ensure_ascii=False, indent=1)
    except OSError:
        pass
    _progress(feedback, 100)
    return counts
