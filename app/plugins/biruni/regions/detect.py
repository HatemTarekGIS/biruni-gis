"""Which country is the user working in? The saved choice first, else where the project's data is, else the
computer's regional settings."""
import os

from qgis.core import (QgsCoordinateReferenceSystem, QgsCoordinateTransform, QgsFeatureRequest, QgsGeometry,
                       QgsPointXY, QgsProject, QgsRectangle, QgsSettings, QgsVectorLayer)
from qgis.PyQt.QtCore import QLocale

SETTING = "biruni/region"
_WORLD = os.path.join(os.path.dirname(os.path.dirname(__file__)), "resources", "data", "world.gpkg")
WGS84 = QgsCoordinateReferenceSystem("EPSG:4326")
_countries_layer = None


def _countries():
    global _countries_layer
    if _countries_layer is None or not _countries_layer.isValid():
        _countries_layer = QgsVectorLayer(f"{_WORLD}|layername=countries", "countries", "ogr")
    return _countries_layer


def country_at(lon, lat):
    """ISO code of the country containing a WGS84 point ('' at sea or outside the bundled map)."""
    lyr = _countries()
    if not lyr.isValid():
        return ""
    pt = QgsGeometry.fromPointXY(QgsPointXY(lon, lat))
    req = QgsFeatureRequest().setFilterRect(QgsRectangle(lon - 1e-6, lat - 1e-6, lon + 1e-6, lat + 1e-6))
    for f in lyr.getFeatures(req):
        if f.geometry().contains(pt):
            a2 = (f["iso_a2"] or "").upper()
            # Natural Earth leaves -99 for a few countries (France, Norway, Kosovo): use the 3-letter code
            return a2 if a2.isalpha() else _A3.get((f["adm0_a3"] or f["iso_a3"] or "").upper(), "")
    return ""


_A3 = {"FRA": "FR", "NOR": "NO", "KOS": "XK", "XKX": "XK", "CYN": "CY", "SOL": "SO"}


def from_project(project=None):
    """Country under the centre of the project's data (all layers' combined extent)."""
    project = project or QgsProject.instance()
    ctx = project.transformContext()
    extent = None
    for lyr in project.mapLayers().values():
        if not lyr.isValid() or not lyr.crs().isValid() or lyr.extent().isNull():  # a single point is "empty"
            continue
        try:
            e = QgsCoordinateTransform(lyr.crs(), WGS84, ctx).transformBoundingBox(lyr.extent())
        except Exception:  # noqa: BLE001 - a layer that cannot be transformed does not count
            continue
        if not (-180 <= e.center().x() <= 180 and -90 <= e.center().y() <= 90):
            continue
        if extent is None:
            extent = QgsRectangle(e)
        else:
            extent.combineExtentWith(e)
    if extent is None:
        return ""
    return country_at(extent.center().x(), extent.center().y())


def from_locale(name=None):
    """Country of the regional settings, e.g. 'ar_SA' → 'SA' (a bare language gives '')."""
    name = name or QLocale.system().name()
    parts = name.replace("-", "_").split("_")
    if len(parts) >= 2 and len(parts[-1]) == 2 and parts[-1].isalpha():
        return parts[-1].upper()
    return ""


def saved():
    return (QgsSettings().value(SETTING, "") or "").upper()


def detect(project=None, locale=None):
    """(country code, source) with source 'saved', 'project', 'locale' or '' when nothing is known."""
    cc = saved()
    if cc:
        return cc, "saved"
    cc = from_project(project)
    if cc:
        return cc, "project"
    cc = from_locale(locale)
    if cc:
        return cc, "locale"
    return "", ""
