"""Team rooms: several people edit the same data at once, one of their computers being the server.

The host opens a room on a saved map and gets a code; the others type the code and get the map
and its data on their own computer. From then on every edit reaches everyone within seconds,
concurrent edits of one feature are joined vertex by vertex and field by field, everyone sees
where the others are working, and if the host's computer goes away another member takes over.

Modules: code (room codes and keys), wire (sealed messages, finding the host), merge (joining
two people's edits), store (the record on each computer), features (a feature as a state),
hub (the host), node (the engine on every computer), snapshot (the map a new member receives),
session (open / join / reopen), overlay (others on the map), dock (the window), folder (the room's changes
through a shared folder, so nobody has to be online at the same time).
"""
_NODE = [None]


def current():
    """The room this map belongs to, or None."""
    return _NODE[0]


def set_current(node):
    _NODE[0] = node
    try:   # team computing: this computer is shared again with a room where its user chose so
        from ..team_compute import worker

        worker.on_room(node)
    except Exception:  # noqa: BLE001 - never stop a room from opening or closing
        pass
    try:   # [team vault] this computer keeps the team's shards and places its own (team_vault/net.py)
        from ..team_vault import net as vault_net

        vault_net.on_room(node)
    except Exception:  # noqa: BLE001
        pass
    try:   # [room folder] a room set to travel through a shared folder carries through it again (team/folder.py)
        from . import folder

        folder.on_room(node)
    except Exception:  # noqa: BLE001
        pass


def notify_added(layer, feats):
    """Another tool wrote features straight into a layer's file (field photos and forms, COGO, the GPS recorder):
    if the layer is shared in a room, the others get them too. -> {old id: new id} of the features the room moved
    into this computer's own block of ids (a tool that keeps an id must use the new one)."""
    node = _NODE[0]
    if node is not None and feats:
        try:
            return node.notify_added(layer, feats) or {}
        except Exception:  # noqa: BLE001 - the room must never break the tool that wrote
            pass
    return {}


def notify_changed(layer, fids):
    """Another tool changed or deleted features straight in a layer's file (the GPS track, review comments): if the
    layer is shared in a room, the others get the changes too."""
    node = _NODE[0]
    if node is not None and fids:
        try:
            node.notify_changed(layer, fids)
        except Exception:  # noqa: BLE001
            pass
