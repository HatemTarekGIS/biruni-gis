"""ArcGIS Server, Portal for ArcGIS and ArcGIS Online services: saved connections (passwords encrypted),
a browser of folders / services / layers that adds layers to the map, editing through the feature service
provider with the service's own rights, and "take offline / sync back" with conflict checks.

    rest.py         REST client (urllib): sign-in, browsing, paged queries, applyEdits
    connections.py  saved connections; secrets kept with DPAPI
    layers.py       service layers in the map, edit rights, fresh tokens
    offline.py      GeoPackage copy, pending edits, sync with conflicts
    ui.py           the browser, the connection and sync windows"""
