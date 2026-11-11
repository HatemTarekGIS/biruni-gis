"""A map service in one folder: no server, no licence, no account (SPEC.md).

``publish(layers, out_dir, title, options)`` writes a folder that any static host serves as a full map service
(GitHub Pages, a shared folder on the office network, a USB stick behind any web server): one PMTiles archive
for the map, one FlatGeobuf per layer for identify and queries (both read in pieces with HTTP range requests),
a static OGC API - Features tree that other GIS programs add as a service, an optional TileJSON with plain
{z}/{x}/{y} tiles, a viewer page, and a manifest of every file signed with this PC's key.
"""
from .core import MapServiceError, publish, rebase, verify  # noqa: F401
