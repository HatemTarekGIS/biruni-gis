"""Running one tile: the same code on a worker and in the owner's own lane (inside runner.py's separate process).

The tile's data comes as numbers and geometries; the files the tool reads are written here, in the private folder
the worker made for this tile, under fixed names. The answer is plain data in a fixed order, so the same tile gives
the same bytes (and the same hash) on every computer with the same engine.
"""
import base64
import os

from . import jobs


def run_tile(task, params, data, folder, feedback=None):
    """-> the result of one tile (a dict), after checking the task, its parameters and the tile again."""
    params = jobs.validate(task, params)
    jobs.check_tile(task, data)
    spec = jobs.TASKS[task]
    if spec["kind"] == "vector":
        return _vector(task, spec, params, data, folder, feedback)
    return _raster(spec, params, data, folder, feedback)


def _context():
    from qgis.core import QgsProcessingContext, QgsProcessingFeedback, QgsProject

    ctx = QgsProcessingContext()
    ctx.setProject(QgsProject.instance())
    return ctx, QgsProcessingFeedback()


def _run(alg_id, params):
    from qgis.core import QgsApplication, QgsProcessingException, QgsProcessingUtils

    alg = QgsApplication.processingRegistry().createAlgorithmById(alg_id)
    if alg is None:
        raise RuntimeError(f"the tool {alg_id} is not available")
    ctx, fb = _context()
    res, ok = alg.run(params, ctx, fb)
    if not ok:
        raise QgsProcessingException(f"{alg_id} failed")
    return res, ctx, QgsProcessingUtils


def _write_gpkg(path, crs_wkt, feats):
    from osgeo import gdal, ogr, osr

    gdal.PushErrorHandler("CPLQuietErrorHandler")   # a polygon in a multipolygon layer: allowed, not worth a word
    try:
        _write(path, crs_wkt, feats, ogr, osr)
    finally:
        gdal.PopErrorHandler()


def _write(path, crs_wkt, feats, ogr, osr):
    srs = None
    if crs_wkt:
        srs = osr.SpatialReference()
        srs.ImportFromWkt(crs_wkt)
    geoms = []
    for fid, wkb in feats:
        g = ogr.CreateGeometryFromWkb(bytes.fromhex(wkb))
        if g is None:
            raise jobs.JobError("bad tile: geometry")
        geoms.append((fid, g))
    gtype = ogr.wkbUnknown
    if geoms:
        gtype = ogr.GT_GetCollection(geoms[0][1].GetGeometryType())
    ds = ogr.GetDriverByName("GPKG").CreateDataSource(path)
    lyr = ds.CreateLayer("t", srs, gtype, ["FID=fid", "SPATIAL_INDEX=NO"])
    ds.StartTransaction()
    for fid, g in geoms:
        f = ogr.Feature(lyr.GetLayerDefn())
        f.SetFID(int(fid))
        f.SetGeometry(g)
        if lyr.CreateFeature(f) != 0:
            raise RuntimeError("cannot write the tile")
    ds.CommitTransaction()
    ds = None


def _vector(task, spec, params, data, folder, feedback):
    from qgis.core import QgsGeometry, QgsRectangle, QgsVectorLayer

    owned = set(data["owned"])
    feats = data["feats"] if task != "bufferdissolve" else [f for f in data["feats"] if f[0] in owned]
    path = os.path.join(folder, "tile.gpkg")
    _write_gpkg(path, data.get("crs", ""), feats)
    layer = QgsVectorLayer(path + "|layername=t", "t", "ogr")
    if not layer.isValid():
        raise RuntimeError("cannot read the tile")
    if task == "overlapsgaps":
        res, ctx, utils = _run(spec["alg"], {"INPUT": layer, "MIN_AREA": params["MIN_AREA"], "MAX_GAP": 0.0,
                                             "OVERLAPS": "TEMPORARY_OUTPUT", "GAPS": "TEMPORARY_OUTPUT"})
        ov = utils.mapLayerFromString(res["OVERLAPS"], ctx)
        rows = []
        for f in ov.getFeatures():
            a, b = int(f["feature_a"]), int(f["feature_b"])
            if min(a, b) in owned:          # each pair belongs to the tile of its first feature
                rows.append([f.geometry().asWkb().data().hex(), a, b, float(f["area"])])
        rows.sort(key=lambda r: (r[1], r[2]))
        # the tile's own part of the polygons, joined: the owner joins these parts and finds the gaps in them
        from ..algorithms.base import polygonal

        x0, y0, x1, y1 = data["core"]
        core = QgsGeometry.fromRect(QgsRectangle(x0, y0, x1, y1))
        parts = []
        for f in layer.getFeatures():
            g = f.geometry()
            if g.isNull() or g.isEmpty():
                continue
            if not g.isGeosValid():
                g = g.makeValid()
            if not g.boundingBox().intersects(core.boundingBox()):
                continue
            p = polygonal(g.intersection(core))
            if p is not None and not p.isEmpty():
                parts.append(p)
        piece = QgsGeometry.unaryUnion(parts) if parts else QgsGeometry()
        return {"overlaps": rows, "piece": piece.asWkb().data().hex() if not piece.isNull() else ""}
    # bufferdissolve
    if not feats:
        return {"geoms": []}
    p = {k: params[k] for k in spec["params"]}
    p.update(INPUT=layer, DISSOLVE=True, OUTPUT="TEMPORARY_OUTPUT")
    res, ctx, utils = _run(spec["alg"], p)
    out = utils.mapLayerFromString(res["OUTPUT"], ctx)
    geoms = [f.geometry() for f in out.getFeatures() if not f.geometry().isNull()]
    if not geoms:
        return {"geoms": []}
    g = geoms[0] if len(geoms) == 1 else QgsGeometry.unaryUnion(geoms)
    return {"geoms": [g.asWkb().data().hex()]}


def _raster(spec, params, data, folder, feedback):
    import numpy as np
    from osgeo import gdal

    w, h = data["w"], data["h"]
    arr = np.frombuffer(base64.b64decode(data["data"]), dtype=data["dtype"]).reshape(h, w)
    src = os.path.join(folder, "tile.tif")
    types = {"uint8": gdal.GDT_Byte, "int16": gdal.GDT_Int16, "uint16": gdal.GDT_UInt16, "int32": gdal.GDT_Int32,
             "uint32": gdal.GDT_UInt32, "float32": gdal.GDT_Float32, "float64": gdal.GDT_Float64}
    ds = gdal.GetDriverByName("GTiff").Create(src, w, h, 1, types[data["dtype"]])
    ds.SetGeoTransform(data["gt"])
    if data.get("crs"):
        ds.SetProjection(data["crs"])
    band = ds.GetRasterBand(1)
    if data.get("nodata") is not None:
        band.SetNoDataValue(float(data["nodata"]))
    band.WriteArray(arr)
    ds.FlushCache()
    ds = None
    out = os.path.join(folder, "out.tif")
    p = {k: params[k] for k in spec["params"]}
    p.update(INPUT=src, OUTPUT=out)
    _run(spec["alg"], p)
    ds = gdal.Open(out)
    if ds is None:
        raise RuntimeError("no output")
    band = ds.GetRasterBand(1)
    res = band.ReadAsArray().astype("float32")
    nodata = band.GetNoDataValue()
    ds = None
    left, top, right, bottom = data["halo"]
    res = np.ascontiguousarray(res[top:h - bottom, left:w - right])
    return {"w": int(res.shape[1]), "h": int(res.shape[0]), "dtype": "float32",
            "nodata": float(nodata) if nodata is not None else None,
            "data": base64.b64encode(res.tobytes()).decode("ascii")}
