"""Who is whose neighbour: spatial weights as scipy sparse matrices, hexagon grids, and the metric
coordinate system the distances are measured in.

Every matrix here is n x n CSR with no self weights unless ``with_self`` adds them. The pure numpy
functions run without QGIS; ``work_crs`` and ``contiguity`` use qgis.core (imported inside)."""
import math

import numpy as np

SQ3 = math.sqrt(3.0)


# scipy is imported on first use: Biruni imports every tool module at start-up
def _sparse():
    from scipy import sparse

    return sparse


def _kd():
    from scipy.spatial import cKDTree

    return cKDTree


# ---- building weights ------------------------------------------------------------------------------
def from_pairs(i, j, n, w=None, symmetric=True):
    """Sparse matrix from neighbour pairs (i -> j); symmetric adds j -> i. Repeated pairs keep one weight."""
    i = np.asarray(i, dtype=np.int64)
    j = np.asarray(j, dtype=np.int64)
    w = np.ones(i.size) if w is None else np.asarray(w, dtype=float)
    if symmetric:
        i, j, w = np.concatenate([i, j]), np.concatenate([j, i]), np.concatenate([w, w])
    keep = i != j
    i, j, w = i[keep], j[keep], w[keep]
    if i.size:  # duplicates: keep the first instead of summing
        key = i * n + j
        _, first = np.unique(key, return_index=True)
        i, j, w = i[first], j[first], w[first]
    return _sparse().csr_matrix((w, (i, j)), shape=(n, n))


def knn(xy, k):
    """k nearest neighbours (not symmetric: my nearest need not have me as theirs)."""
    xy = np.asarray(xy, dtype=float)
    n = len(xy)
    k = int(max(1, min(k, n - 1)))
    _, idx = _kd()(xy).query(xy, k + 1)
    idx = np.asarray(idx).reshape(n, k + 1)
    mask = idx != np.arange(n)[:, None]
    mask[mask.all(1), -1] = False  # self not among them (duplicate points): drop the farthest instead
    nb = idx[mask].reshape(n, k)
    return from_pairs(np.repeat(np.arange(n), k), nb.ravel(), n, symmetric=False)


def nn_distances(xy, k=1):
    """Distance from every point to its k-th nearest other point."""
    xy = np.asarray(xy, dtype=float)
    d, _ = _kd()(xy).query(xy, min(k + 1, len(xy)))
    return np.asarray(d)[:, -1]


def min_threshold(xy):
    """The smallest distance band that gives every feature at least one neighbour."""
    return float(nn_distances(xy, 1).max())


def distance_band(xy, d, inverse=False, power=1.0):
    """Neighbours closer than d (binary), or weighted 1/distance^power inside the band (inverse)."""
    xy = np.asarray(xy, dtype=float)
    n = len(xy)
    pairs = _kd()(xy).query_pairs(d, output_type="ndarray")
    if not len(pairs):
        return _sparse().csr_matrix((n, n))
    i, j = pairs[:, 0], pairs[:, 1]
    w = None
    if inverse:
        dist = np.hypot(*(xy[i] - xy[j]).T)
        floor = max(d * 1e-3, 1e-9)  # duplicate points: a very close neighbour, not an infinite weight
        w = 1.0 / np.maximum(dist, floor) ** power
    return from_pairs(i, j, n, w)


def pair_count(xy, distances):
    """Number of ordered neighbour pairs within each distance (cheap: no matrix is built)."""
    tree = _kd()(np.asarray(xy, dtype=float))
    return np.asarray(tree.count_neighbors(tree, np.asarray(distances, dtype=float))) - len(xy)


def row_standardise(w):
    w = _sparse().csr_matrix(w, dtype=float)
    rs = np.asarray(w.sum(1)).ravel()
    inv = np.divide(1.0, rs, out=np.zeros_like(rs), where=rs > 0)
    return _sparse().diags(inv) @ w


def binary(w):
    b = _sparse().csr_matrix(w, dtype=float, copy=True)
    b.data[:] = 1.0
    return b


def with_self(w, value=1.0):
    return _sparse().csr_matrix(w + _sparse().identity(w.shape[0], format="csr") * value)


def cardinality(w):
    return np.diff(_sparse().csr_matrix(w).indptr)


def islands(w):
    """Features with no neighbour at all."""
    return np.flatnonzero(cardinality(w) == 0)


def attach_islands(w, xy):
    """Give every island its nearest feature as a neighbour (both ways); returns (matrix, island ids)."""
    isl = islands(w)
    if not isl.size:
        return _sparse().csr_matrix(w), isl
    _, idx = _kd()(xy).query(np.asarray(xy)[isl], 2)
    nb = np.where(idx[:, 0] == isl, idx[:, 1], idx[:, 0])
    extra = from_pairs(isl, nb, w.shape[0])
    out = _sparse().csr_matrix(w).maximum(extra) if w.nnz else extra
    return out, isl


def components(w):
    """Number of separate groups of features that are not connected to each other."""
    from scipy.sparse.csgraph import connected_components

    return connected_components(_sparse().csr_matrix(w), directed=False)[0]


# ---- hexagon grid ----------------------------------------------------------------------------------
def hex_bin(xy, width, origin):
    """Pointy-top hexagons whose flat sides are ``width`` apart: (col, row) cell index on a doubled grid
    for every point. Two offset rectangular lattices; each point goes to the nearer centre (as hexbin)."""
    xy = np.asarray(xy, dtype=float)
    dx, dy = width, width * SQ3
    x = (xy[:, 0] - origin[0]) / dx
    y = (xy[:, 1] - origin[1]) / dy
    ia, ja = np.round(x), np.round(y)
    ib, jb = np.floor(x), np.floor(y)
    da = ((x - ia) * dx) ** 2 + ((y - ja) * dy) ** 2
    db = ((x - ib - 0.5) * dx) ** 2 + ((y - jb - 0.5) * dy) ** 2
    a = da <= db
    col = np.where(a, 2 * ia, 2 * ib + 1).astype(np.int64)
    row = np.where(a, 2 * ja, 2 * jb + 1).astype(np.int64)
    return col, row


def hex_centres(col, row, width, origin):
    return np.column_stack([origin[0] + col * width / 2.0, origin[1] + row * width * SQ3 / 2.0])


def hex_ring(cx, cy, width):
    """Corner points of one hexagon (closed ring)."""
    r = width / SQ3
    ang = np.radians(30 + 60 * np.arange(7))
    return np.column_stack([cx + r * np.cos(ang), cy + r * np.sin(ang)])


def hex_cells(xy, width, weights=None, fill_hull=True, max_cells=60000):
    """Count points in hexagons. Cells holding points, plus (fill_hull) the empty cells whose centre lies
    inside the convex hull of the points, so empty places count as zeros instead of being left out.
    Returns dict: centres (m,2), counts (m,), point_cell (index of the cell of every point), width."""
    xy = np.asarray(xy, dtype=float)
    origin = (float(xy[:, 0].min()), float(xy[:, 1].min()))
    col, row = hex_bin(xy, width, origin)
    keys = np.column_stack([col, row])
    if fill_hull and len(xy) >= 3:
        cmin, cmax = col.min() - 2, col.max() + 2
        rmin, rmax = row.min() - 2, row.max() + 2
        if (cmax - cmin + 1) * (rmax - rmin + 1) / 2 <= max_cells * 4:
            cc, rr = np.meshgrid(np.arange(cmin, cmax + 1), np.arange(rmin, rmax + 1))
            cc, rr = cc.ravel(), rr.ravel()
            ok = (cc % 2) == (rr % 2)
            cc, rr = cc[ok], rr[ok]
            inside = _in_hull(xy, hex_centres(cc, rr, width, origin))
            keys = np.concatenate([keys, np.column_stack([cc[inside], rr[inside]])])
    uniq, inv = np.unique(keys, axis=0, return_inverse=True)
    inv = np.asarray(inv).ravel()
    point_cell = inv[:len(xy)]
    w = np.ones(len(xy)) if weights is None else np.asarray(weights, dtype=float)
    counts = np.bincount(point_cell, weights=w, minlength=len(uniq))
    return {"centres": hex_centres(uniq[:, 0], uniq[:, 1], width, origin), "counts": counts,
            "point_cell": point_cell, "width": float(width), "origin": origin}


def _in_hull(xy, pts):
    from scipy.spatial import Delaunay, QhullError

    try:
        return Delaunay(xy).find_simplex(pts) >= 0
    except (QhullError, ValueError):  # all points on a line
        return np.zeros(len(pts), dtype=bool)


def hull_area(xy):
    from scipy.spatial import ConvexHull, QhullError

    try:
        return float(ConvexHull(np.asarray(xy, dtype=float)).volume)
    except (QhullError, ValueError):
        return 0.0


def auto_cell(xy, target=None):
    """A hexagon width for incidents: about one cell per 4 points (between 60 and 2,500 cells over the
    convex hull), never narrower than the typical spacing between points."""
    xy = np.asarray(xy, dtype=float)
    n = len(xy)
    area = hull_area(xy)
    if area <= 0:
        span = max(np.ptp(xy[:, 0]), np.ptp(xy[:, 1]), 1.0)
        area = span * span
    m = target or float(np.clip(n / 4.0, 60, 2500))
    w = math.sqrt(2.0 * area / (SQ3 * m))
    spacing = float(np.median(nn_distances(xy, 1))) if n > 1 else 0.0
    return _nice(max(w, spacing))


def _nice(v):
    """Round a length to 1, 2, 2.5 or 5 x a power of ten (cell sizes people can quote)."""
    if v <= 0:
        return v
    p = 10 ** math.floor(math.log10(v))
    for m in (1, 2, 2.5, 5, 10):
        if v <= m * p * 1.15:
            return m * p
    return 10 * p


def hex_neighbours(centres, width):
    """The six touching hexagons."""
    return distance_band(centres, width * 1.01)


# ---- metric coordinates (QGIS) -----------------------------------------------------------------------
def work_crs(crs, extent, context=None):
    """(metric CRS, reprojected?) - the layer's own when it is projected in metres, else the UTM zone
    of the data (WGS 84)."""
    from qgis.core import (Qgis, QgsCoordinateReferenceSystem, QgsCoordinateTransform, QgsProject)

    if crs.isValid() and not crs.isGeographic() and crs.mapUnits() == Qgis.DistanceUnit.Meters:
        return crs, False
    wgs = QgsCoordinateReferenceSystem("EPSG:4326")
    c = extent.center()
    if crs.isValid() and crs != wgs:
        tc = context.transformContext() if context is not None else QgsProject.instance().transformContext()
        try:
            c = QgsCoordinateTransform(crs, wgs, tc).transform(c)
        except Exception:  # noqa: BLE001
            pass
    lon = max(-180.0, min(180.0, c.x()))
    zone = min(int((lon + 180) // 6) + 1, 60)
    return QgsCoordinateReferenceSystem(f"EPSG:{(32600 if c.y() >= 0 else 32700) + zone}"), True


def contiguity(geoms, queen=True, feedback=None):
    """Polygons that share a border (rook) or at least a corner (queen); geoms: QgsGeometry list."""
    from qgis.core import QgsGeometry, QgsSpatialIndex

    n = len(geoms)
    index = QgsSpatialIndex()
    for k, g in enumerate(geoms):
        index.addFeature(k, g.boundingBox())
    ii, jj = [], []
    for k, g in enumerate(geoms):
        if feedback is not None and feedback.isCanceled():
            break
        engine = QgsGeometry.createGeometryEngine(g.constGet())
        engine.prepareGeometry()
        box = g.boundingBox()
        box.grow(max(box.width(), box.height()) * 1e-9)
        for o in index.intersects(box):
            if o <= k or not engine.intersects(geoms[o].constGet()):
                continue
            if not queen:
                inter = g.intersection(geoms[o])
                if inter.isEmpty() or (inter.length() <= 1e-9 and inter.area() <= 1e-9):
                    continue
            ii.append(k)
            jj.append(o)
    return from_pairs(ii, jj, n)
