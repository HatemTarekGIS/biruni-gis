"""Service layers in the map: feature layers (read and edit through the engine's ArcGIS Feature Service
provider) and map / image services (drawn as images).

Signing in: a signed-in connection is registered here and every request the engine sends to that server -
drawing, identify, and the provider's edits - gets the connection's token in the X-Esri-Authorization header
(a network request preprocessor). Tokens therefore never go into the layer source or the project file; they
are renewed before they expire. After a project is opened, refresh_tokens() signs its service layers in again
from the saved connections.

Editing follows what the service allows: a layer whose service has editing turned off (or lacks Create /
Update / Delete) says so clearly when the user starts editing, and a service without editing stays read-only."""
import time

from qgis.core import (QgsDataSourceUri, QgsNetworkAccessManager, QgsProject, QgsRasterLayer, QgsVectorDataProvider,
                       QgsVectorLayer)

from ..i18n import tr
from . import connections
from .rest import AUTH_NONE, Client, RestError, edit_rights, services_root, split_service_url

PROP_URL = "biruni/arcgis-service-url"
PROP_CONN = "biruni/arcgis-connection"
PROP_RIGHTS = "biruni/arcgis-edit-rights"
HEADER = b"X-Esri-Authorization"

_SIGNED = {}            # server prefix (lower case, "https://host/arcgis/") -> Connection with a live token
_STATE = {"pre": None, "timer": None}


# ---- tokens on every request to a signed-in server ----------------------------------------------------------------
def _prefix(url):
    return services_root(url).rsplit("/rest/services", 1)[0].lower() + "/"


def _preprocess(request):
    """Runs for every network request of the engine (any thread): adds the token of a signed-in server."""
    url = request.url().toString().lower()
    for prefix, conn in list(_SIGNED.items()):
        if url.startswith(prefix):
            token = conn.api_key if conn.auth == "apikey" else conn.token
            if token and not request.hasRawHeader(HEADER):
                request.setRawHeader(HEADER, ("Bearer " + token).encode("ascii", "ignore"))
            return


def sign_in(conn, fetched=False):
    """Make the engine send this connection's token to its server (gets a token first if needed, unless
    ``fetched``: a background thread already got it)."""
    if conn.auth == AUTH_NONE:
        return False
    if not fetched:
        Client(conn).ensure_token()
    connections.remember_token(conn)
    _SIGNED[_prefix(conn.url)] = conn
    if _STATE["pre"] is None:
        _STATE["pre"] = QgsNetworkAccessManager.setRequestPreprocessor(_preprocess)
        _start_renewal()
    return True


def sign_out(conn):
    _SIGNED.pop(_prefix(conn.url), None)


def _fetch_tokens(conns, force=False):
    """Get the tokens of ``conns`` (network only - safe in a background thread). -> the connections that
    got one."""
    ok = []
    for conn in conns:
        try:
            Client(conn).ensure_token(force=force)
            ok.append(conn)
        except (RestError, OSError, ValueError):
            pass
    return ok


def in_background(work, done):
    """Run ``work()`` in a thread and ``done(result)`` back on the main thread: a server that answers
    slowly or not at all must not freeze the window."""
    from .. import background

    background.run(work, lambda result, _error: done(result))


def _start_renewal():
    """Renew tokens a few minutes before they expire (every two minutes, the network part in a thread)."""
    from qgis.PyQt.QtCore import QTimer

    t = QTimer()
    t.setInterval(120000)

    def renew():
        due = [c for c in _SIGNED.values()
               if c.auth in ("token", "oauth") and c.token_expires - time.time() < 600]
        if due:
            in_background(lambda: _fetch_tokens(due, force=True),
                          lambda ok: [connections.remember_token(c) for c in ok or []])
    t.timeout.connect(renew)
    t.start()
    _STATE["timer"] = t


def layer_uri(conn, url):
    uri = QgsDataSourceUri()
    uri.setParam("url", url.rstrip("/"))
    if conn is not None and conn.referer:
        uri.setParam("referer", conn.referer)
    return uri


# ---- layers ---------------------------------------------------------------------------------------------------------
def make_layer(client, url, name=None, info=None):
    """Service layer URL -> QgsVectorLayer (feature layers) or QgsRasterLayer (map / image services).
    Raises RestError when the service cannot be read."""
    _root, _svc, stype, layer_id = split_service_url(url)
    if client.conn.auth != AUTH_NONE:
        sign_in(client.conn)
    uri = layer_uri(client.conn, url)
    if stype in ("MapServer", "ImageServer") and (layer_id is None or stype == "ImageServer"):
        lyr = QgsRasterLayer(uri.uri(False), name or _svc_name(url), "arcgismapserver")
        if not lyr.isValid():
            raise RestError(tr("The map service could not be drawn: {e}").format(e=lyr.error().summary() or url))
        _tag(lyr, client, url, None)
        return lyr
    info = info or client.info(url)
    if info.get("type") not in (None, "Feature Layer", "Table", "Annotation Layer") and stype == "MapServer":
        raise RestError(tr("This kind of layer cannot be added: {t}").format(t=info.get("type")))
    lyr = QgsVectorLayer(uri.uri(False), name or info.get("name") or _svc_name(url), "arcgisfeatureserver")
    if not lyr.isValid():
        raise RestError(tr("The layer could not be opened: {e}").format(e=lyr.error().summary() or url))
    _tag(lyr, client, url, info)
    guard(lyr)
    return lyr


def _svc_name(url):
    _root, svc, stype, lid = split_service_url(url)
    return (svc.rsplit("/", 1)[-1] or stype) + (f" {lid}" if lid is not None else "")


def _tag(lyr, client, url, info):
    lyr.setCustomProperty(PROP_URL, url)
    lyr.setCustomProperty(PROP_CONN, client.conn.name)
    if info is not None:
        r = edit_rights(info)
        lyr.setCustomProperty(PROP_RIGHTS, ",".join(k for k, v in r.items() if v))


def add_to_project(client, url, project=None, name=None, info=None, group=None):
    project = project or QgsProject.instance()
    lyr = make_layer(client, url, name, info)
    if group is None:
        project.addMapLayer(lyr)
    else:
        project.addMapLayer(lyr, False)
        group.addLayer(lyr)
    return lyr


# ---- editing --------------------------------------------------------------------------------------------------------
def rights(lyr):
    """What the service lets this layer do: dict(add, update, delete, geometry) from the service's
    capabilities and the provider's own view."""
    caps = lyr.dataProvider().capabilities() if lyr.dataProvider() else 0
    C = QgsVectorDataProvider.Capability
    stored = lyr.customProperty(PROP_RIGHTS)
    stored = set(stored.split(",")) if stored is not None else None

    def ok(key, flag):
        return bool(caps & flag) and (stored is None or key in stored)
    return {"add": ok("add", C.AddFeatures), "update": ok("update", C.ChangeAttributeValues),
            "delete": ok("delete", C.DeleteFeatures), "geometry": ok("geometry", C.ChangeGeometries)}


def rights_text(lyr):
    return describe_rights(rights(lyr))


def describe_rights(r):
    """dict(add, update, delete) -> a sentence for the user."""
    if not any(r.get(k) for k in ("add", "update", "delete")):
        return tr("This layer is read-only: the owner of the service has not turned on editing for it "
                  "(Create, Update and Delete are off). Take it offline to work on a copy, or ask the "
                  "service owner for editing rights.")
    words = (("add", tr("add")), ("update", tr("change")), ("delete", tr("delete")))
    can = [w for k, w in words if r[k]]
    cannot = [w for k, w in words if not r[k]]
    text = tr("You can {what} features of this layer.").format(what=", ".join(can))
    if cannot:
        text += " " + tr("The service does not allow you to {what} them.").format(what=" / ".join(cannot))
    return text


_GUARDS = {}            # layer id -> [notify]


def guard(lyr, notify=None):
    """When editing starts, tell the user what the service allows; a service without editing stays read-only.
    notify(text, level 0 info / 1 warning) shows the message (the message bar in the app)."""
    r = rights(lyr)
    if not any(r.values()):
        lyr.setReadOnly(True)
    lid = lyr.id()
    if lid in _GUARDS:
        if notify is not None:
            _GUARDS[lid][0] = notify
        return
    box = [notify]

    def started():
        if box[0] is not None:
            box[0](rights_text(lyr), 0 if all((r["add"], r["update"], r["delete"])) else 1)

    lyr.editingStarted.connect(started)
    lyr.willBeDeleted.connect(lambda: _GUARDS.pop(lid, None))
    _GUARDS[lid] = box


# ---- after opening a project ----------------------------------------------------------------------------------------
def _service_layers(project):
    """[(layer id, url, connection)] of the project's signed service layers."""
    out = []
    for lyr in project.mapLayers().values():
        url = lyr.customProperty(PROP_URL)
        name = lyr.customProperty(PROP_CONN)
        if not url or not name or lyr.providerType() not in ("arcgisfeatureserver", "arcgismapserver"):
            continue
        conn = _SIGNED.get(_prefix(url)) or connections.load(name)
        if conn is None or conn.auth == AUTH_NONE:
            continue
        out.append((lyr.id(), url, conn))
    return out


def _apply_tokens(project, targets, ok):
    n = 0
    ok_ids = {id(c) for c in ok}
    for lid, url, conn in targets:
        lyr = project.mapLayer(lid)
        if lyr is None or id(conn) not in ok_ids:
            continue
        sign_in(conn, fetched=True)
        lyr.setDataSource(layer_uri(conn, url).uri(False), lyr.name(), lyr.providerType())
        if isinstance(lyr, QgsVectorLayer):
            guard(lyr)
        n += 1
    return n


def refresh_tokens(project=None, background=False, done=None):
    """Sign the project's service layers in again from their saved connections (tokens are never saved in the
    project) and reload them. -> number of layers signed in. With ``background`` the tokens are fetched in a
    thread (opening a project does not wait for the servers) and ``done(n)`` is called when they are applied."""
    project = project or QgsProject.instance()
    targets = _service_layers(project)
    conns = list({id(c): c for _lid, _url, c in targets}.values())
    if not background:
        return _apply_tokens(project, targets, _fetch_tokens(conns))

    def finish(ok):
        n = _apply_tokens(project, targets, ok or [])
        if done is not None:
            done(n)
    in_background(lambda: _fetch_tokens(conns), finish)
    return None
