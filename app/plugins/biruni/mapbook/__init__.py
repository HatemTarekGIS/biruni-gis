"""Map Book (دفتر الخرائط): a whole book of map pages - from an index layer, an automatic grid, strips along a
route, one page per feature or the project's Map Stops - with a cover and key map, contents, the name index, and
one PDF whose bookmarks, neighbour arrows and key-map cells jump to their pages.

Nothing is imported or done here at start-up: the panel and the engine load when the user opens the Map Book.
"""


def open_panel(plugin):
    """The Map Book panel (created on first use)."""
    from .panel import open_panel as _open

    return _open(plugin)
