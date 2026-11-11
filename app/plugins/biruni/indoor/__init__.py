"""From floor plan to indoor map: architectural DXF/DWG plans become an indoor map.

    cad.py        read a plan (Biruni's DXF reader + block contents, scaled to metres)
    recognize.py  walls -> spaces, doors, room names/numbers/categories (heuristics, Arabic and English)
    graph.py      navigation graph (visibility graph inside every space) and shortest routes
    store.py      indoor.gpkg (levels, units, openings, nav_nodes, nav_edges), Apple IMDF zip, report
    style.py      map styles for the layers

The GeoPackage layer and field names are a contract with the phone viewer: do not rename them.
"""
from .graph import route  # noqa: F401
