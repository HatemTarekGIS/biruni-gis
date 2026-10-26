"""Traverses as QGIS geometries with true arcs."""
from qgis.core import QgsCircularString, QgsCompoundCurve, QgsCurvePolygon, QgsGeometry, QgsLineString, QgsPoint

from .cogo import arc_midpoint


def traverse_curve(trav, adjusted=True):
    """The traverse as a QgsCompoundCurve (straight runs + circular arcs)."""
    pts = trav.adjusted if adjusted else trav.points
    curve = QgsCompoundCurve()
    run = [pts[0]]

    def flush():
        if len(run) >= 2:
            curve.addCurve(QgsLineString([QgsPoint(x, y) for x, y in run]))

    for c, p1, p2 in zip(trav.courses, pts, pts[1:]):
        m = arc_midpoint(p1, p2, c.radius, c.side) if c.is_curve() else None
        if m is not None:
            flush()
            curve.addCurve(QgsCircularString(QgsPoint(*p1), QgsPoint(*m), QgsPoint(*p2)))
            run[:] = [p2]
        else:
            run.append(p2)
    flush()
    return curve


def traverse_geometry(trav, polygon, adjusted=True):
    curve = traverse_curve(trav, adjusted)
    if not polygon:
        return QgsGeometry(curve)
    if not curve.isClosed():
        curve.close()
    cp = QgsCurvePolygon()
    cp.setExteriorRing(curve)
    return QgsGeometry(cp)
