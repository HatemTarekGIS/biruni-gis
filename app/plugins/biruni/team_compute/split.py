"""Cutting a job into tiles, and joining the tiles' results into the answer one computer would have given.

Vector: a grid over the layer; every feature belongs to the one tile its bounding box's centre falls in (cells are
half-open, so exactly one). A tile carries its own features and, around them, the neighbours they might touch:
  * overlaps and gaps - every feature that meets the tile's own part or any of its features' boxes. A pair is kept
    by the tile of its first feature (the other one is then always in that tile); the gaps are holes of the union
    of all polygons, so each tile sends its part of the polygons cut to its cell and the owner joins the parts
    and takes their holes - the union of the parts is the union of the whole.
  * buffer, dissolved - the tile's own features only; the owner joins the tiles' buffers.
Raster: square cells of pixels with a halo of pixels around each (one for the 3x3 tools); the tile's answer is cut
back to its cell and the cells are put side by side.
"""
import base64
import math

from . import jobs


def _grid(rect, n, nx=None, ny=None):
    w, h = max(rect[2] - rect[0], 1e-9), max(rect[3] - rect[1], 1e-9)
    if nx is None or ny is None:
        nx = max(1, int(math.ceil(math.sqrt(n * w / h))))
        ny = max(1, int(math.ceil(n / nx)))
    return nx, ny


def vector_tiles(feats, task, crs_wkt="", target=1500, nx=None, ny=None):
    """feats: [(fid, QgsGeometry)] -> [{"tid", "data"}] for an allowed vector task."""
    from qgis.core import QgsRectangle, QgsSpatialIndex

    spec = jobs.TASKS[task]
    if spec["kind"] != "vector":
        raise jobs.JobError("not a vector tool")
    items = []
    for fid, g in feats:
        if g is None or g.isNull() or g.isEmpty():
            continue
        items.append((int(fid), g, g.boundingBox(), g.asWkb().data().hex()))
    if not items:
        return []
    ext = QgsRectangle(items[0][2])
    for it in items[1:]:
        ext.combineExtentWith(it[2])
    rect = (ext.xMinimum(), ext.yMinimum(), ext.xMaximum(), ext.yMaximum())
    fixed = nx is not None and ny is not None
    nx, ny = _grid(rect, max(1, math.ceil(len(items) / target)), nx, ny)
    index = QgsSpatialIndex()
    by_id = {}
    for fid, g, bb, wkb in items:
        index.addFeature(fid, bb)
        by_id[fid] = (bb, wkb)
    for _round in range(7):
        tiles = _cut(items, rect, nx, ny, index, by_id, task, crs_wkt)
        big = [t for t in tiles if len(t["data"]["feats"]) > jobs.MAX_FEATS or jobs.size_of(t["data"]) > jobs.MAX_TILE]
        if not big:
            return tiles
        if fixed:
            raise jobs.JobError("a tile is too large")
        nx, ny = nx * 2, ny * 2
    raise jobs.JobError("a tile is too large (very large features)")


def _cut(items, rect, nx, ny, index, by_id, task, crs_wkt):
    from qgis.core import QgsRectangle

    x0, y0, x1, y1 = rect
    dx, dy = (x1 - x0) / nx or 1e-9, (y1 - y0) / ny or 1e-9
    owned = {}
    for fid, g, bb, wkb in items:
        c = min(nx - 1, max(0, int((bb.center().x() - x0) / dx)))
        r = min(ny - 1, max(0, int((bb.center().y() - y0) / dy)))
        owned.setdefault((c, r), []).append(fid)
    tiles = []
    for r in range(ny):
        for c in range(nx):
            core = [x0 + c * dx, y0 + r * dy, x1 if c == nx - 1 else x0 + (c + 1) * dx,
                    y1 if r == ny - 1 else y0 + (r + 1) * dy]
            mine = sorted(owned.get((c, r), []))
            if task == "bufferdissolve":
                if not mine:
                    continue
                ids = mine
            else:
                ids = set(index.intersects(QgsRectangle(*core)))
                for fid in mine:
                    ids.update(index.intersects(by_id[fid][0]))
                if not ids:
                    continue
                ids = sorted(ids)
            data = {"crs": crs_wkt or "", "feats": [[fid, by_id[fid][1]] for fid in ids], "owned": mine,
                    "core": core}
            tiles.append({"tid": f"v{c}_{r}", "data": data})
    return tiles


def stitch_vector(task, params, results):
    """results: {tid: result} -> overlapsgaps: {"overlaps": [[wkb, a, b, area]], "gaps": [[wkb, area]]};
    bufferdissolve: {"geom": wkb or ""}."""
    from qgis.core import QgsGeometry, QgsPolygon

    params = jobs.validate(task, params)
    if task == "bufferdissolve":
        geoms = []
        for res in results.values():
            for h in res.get("geoms") or []:
                g = QgsGeometry()
                g.fromWkb(bytes.fromhex(h))
                geoms.append(g)
        if not geoms:
            return {"geom": ""}
        g = geoms[0] if len(geoms) == 1 else QgsGeometry.unaryUnion(geoms)
        return {"geom": g.asWkb().data().hex()}
    rows, seen, parts = [], set(), []
    for res in results.values():
        for row in res.get("overlaps") or []:
            if (row[1], row[2]) not in seen:
                seen.add((row[1], row[2]))
                rows.append(row)
        if res.get("piece"):
            g = QgsGeometry()
            g.fromWkb(bytes.fromhex(res["piece"]))
            if not g.isEmpty():
                parts.append(g)
    rows.sort(key=lambda r: (r[1], r[2]))
    min_area = max(params["MIN_AREA"], 1e-10)
    max_gap = params["MAX_GAP"]
    gaps = []
    union = QgsGeometry.unaryUnion(parts) if parts else QgsGeometry()
    for part in (union.asGeometryCollection() if union.isMultipart() else [union]) if not union.isNull() else []:
        poly = part.constGet()
        if poly is None or not hasattr(poly, "numInteriorRings"):
            continue
        for i in range(poly.numInteriorRings()):
            hole = QgsPolygon()
            hole.setExteriorRing(poly.interiorRing(i).clone())
            hg = QgsGeometry(hole)
            area = hg.area()
            if area < min_area or (max_gap > 0 and area > max_gap):
                continue
            hg.convertToMultiType()
            gaps.append([hg.asWkb().data().hex(), area])
    gaps.sort(key=lambda r: (round(r[1], 6), r[0]))
    return {"overlaps": rows, "gaps": gaps}


def raster_tiles(path, task, cell=512):
    """A single-band DEM file -> (meta, [{"tid", "data", "pos"}]). pos: [col, row, width, height] of the cell."""
    from osgeo import gdal

    spec = jobs.TASKS[task]
    if spec["kind"] != "raster":
        raise jobs.JobError("not a raster tool")
    ds = gdal.Open(path)
    if ds is None:
        raise jobs.JobError("cannot read the raster")
    band = ds.GetRasterBand(1)
    W, H = ds.RasterXSize, ds.RasterYSize
    gt = list(ds.GetGeoTransform())
    wkt = ds.GetProjection() or ""
    nodata = band.GetNoDataValue()
    halo = int(spec.get("halo", 1))
    cell = max(16, min(int(cell), int(math.sqrt(jobs.MAX_PIXELS)) - 2 * halo))
    tiles = []
    for y in range(0, H, cell):
        for x in range(0, W, cell):
            cw, ch = min(cell, W - x), min(cell, H - y)
            hl, ht = min(halo, x), min(halo, y)
            hr, hb = min(halo, W - x - cw), min(halo, H - y - ch)
            ox, oy, w, h = x - hl, y - ht, cw + hl + hr, ch + ht + hb
            arr = band.ReadAsArray(ox, oy, w, h)
            dtype = arr.dtype.name
            if dtype not in jobs.DTYPES:
                arr = arr.astype("float64")
                dtype = "float64"
            tgt = [gt[0] + ox * gt[1] + oy * gt[2], gt[1], gt[2], gt[3] + ox * gt[4] + oy * gt[5], gt[4], gt[5]]
            data = {"crs": wkt, "w": w, "h": h, "gt": tgt, "dtype": dtype,
                    "nodata": float(nodata) if nodata is not None else None,
                    "data": base64.b64encode(arr.tobytes()).decode("ascii"), "halo": [hl, ht, hr, hb]}
            if jobs.size_of(data) > jobs.MAX_TILE:
                raise jobs.JobError("a tile is too large")
            tiles.append({"tid": f"r{x}_{y}", "data": data, "pos": [x, y, cw, ch]})
    ds = None
    return {"w": W, "h": H, "gt": gt, "crs": wkt}, tiles


def stitch_raster(meta, tiles, results, out_path):
    """Put the tiles' cells side by side into a GeoTIFF at out_path. -> out_path"""
    import numpy as np
    from osgeo import gdal

    pos = {t["tid"]: t["pos"] for t in tiles}
    nodata = next((r.get("nodata") for r in results.values() if r.get("nodata") is not None), -9999.0)
    out = np.full((meta["h"], meta["w"]), nodata, dtype="float32")
    for tid, res in results.items():
        x, y, cw, ch = pos[tid]
        if res["w"] != cw or res["h"] != ch:
            raise jobs.JobError("a tile came back with the wrong size")
        out[y:y + ch, x:x + cw] = np.frombuffer(base64.b64decode(res["data"]), dtype="float32").reshape(ch, cw)
    ds = gdal.GetDriverByName("GTiff").Create(out_path, meta["w"], meta["h"], 1, gdal.GDT_Float32,
                                              ["COMPRESS=DEFLATE", "TILED=YES"])
    ds.SetGeoTransform(meta["gt"])
    if meta.get("crs"):
        ds.SetProjection(meta["crs"])
    b = ds.GetRasterBand(1)
    b.SetNoDataValue(float(nodata))
    b.WriteArray(out)
    ds.FlushCache()
    ds = None
    return out_path
