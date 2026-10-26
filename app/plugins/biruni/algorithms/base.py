from qgis.core import QgsProcessingAlgorithm

from ..i18n import tr


class BiruniAlgorithm(QgsProcessingAlgorithm):
    """Common plumbing: translated texts, one class per tool."""

    ID = ""
    NAME = ""
    GROUP = ""
    GROUP_ID = ""
    HELP = ""

    def name(self):
        return self.ID

    def displayName(self):  # noqa: N802
        return tr(self.NAME)

    def group(self):
        return tr(self.GROUP)

    def groupId(self):  # noqa: N802
        return self.GROUP_ID

    def shortHelpString(self):  # noqa: N802
        return tr(self.HELP)

    def createInstance(self):  # noqa: N802
        return type(self)()


def polygonal(geom):
    """The polygon part of a geometry (GEOS overlays may return collections
    with stray points/lines), as a multipolygon; None when there is none."""
    from qgis.core import Qgis, QgsGeometry

    if geom is None or geom.isEmpty():
        return None
    if geom.type() == Qgis.GeometryType.Polygon:
        g = QgsGeometry(geom)
    else:
        parts = [p for p in geom.asGeometryCollection() if p.type() == Qgis.GeometryType.Polygon]
        if not parts:
            return None
        g = QgsGeometry.collectGeometry(parts)
    g.convertToMultiType()
    return g
