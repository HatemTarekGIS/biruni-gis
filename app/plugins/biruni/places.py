"""Bundled world data (Natural Earth, public domain): which country a point is
in, and the sample layers. Built by scripts/make_world_data.py."""
import os

from qgis.core import QgsFeatureRequest, QgsGeometry, QgsPointXY, QgsRectangle, QgsVectorLayer

from . import i18n

WORLD = os.path.join(os.path.dirname(__file__), "resources", "data", "world.gpkg")
_countries = None


def world_layer(name, title=None):
    return QgsVectorLayer(f"{WORLD}|layername={name}", title or name, "ogr")


def _layer():
    global _countries
    if _countries is None and os.path.exists(WORLD):
        _countries = world_layer("countries")
    return _countries


def country_feature(lon, lat):
    lyr = _layer()
    if lyr is None or not lyr.isValid():
        return None
    pt = QgsGeometry.fromPointXY(QgsPointXY(lon, lat))
    req = QgsFeatureRequest().setFilterRect(QgsRectangle(lon - 1e-6, lat - 1e-6, lon + 1e-6, lat + 1e-6))
    for f in lyr.getFeatures(req):
        if f.geometry().contains(pt):
            return f
    return None


def country_at(lon, lat):
    """Country name in the interface language, or None (sea / outside)."""
    f = country_feature(lon, lat)
    if f is None:
        return None
    if i18n.lang() == "ar" and f["name_ar"]:
        return f["name_ar"]
    return f["name"]


def country_iso3(lon, lat):
    f = country_feature(lon, lat)
    if f is None:
        return None
    iso = f["iso_a3"]
    return iso if iso and iso != "-99" else f["adm0_a3"]
