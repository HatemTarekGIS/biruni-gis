"""Biruni GIS - the application shell (ribbon, start page, themes, i18n) that
runs on top of the bundled QGIS engine."""


def classFactory(iface):  # noqa: N802 (QGIS plugin entry point)
    from .plugin import BiruniPlugin

    return BiruniPlugin(iface)
