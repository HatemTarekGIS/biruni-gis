"""ArcGIS REST API client for ArcGIS Server, Portal for ArcGIS and ArcGIS Online.

Pure Python (urllib), so it can run and be tested without the engine:

    browsing    - folders, services, layers and tables of a server (or of a portal's hosted server)
    signing in  - anonymous, built-in user and password (generateToken), API keys, and Portal /
                  ArcGIS Online OAuth 2 sign-in in the browser (authorization code with PKCE)
    reading     - layer information, queries with paging (resultOffset, or object ids in batches)
    editing     - applyEdits with per-feature results

Tokens are kept in memory only; a token that expires (498/499) is renewed once and the request is sent again."""
import base64
import hashlib
import json
import secrets
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field


class RestError(Exception):
    """An answer from the server that is an error (or no answer)."""

    def __init__(self, message, code=None, details=None):
        super().__init__(message)
        self.code = code
        self.details = details or []

    def __str__(self):
        text = super().__str__()
        extra = "; ".join(d for d in self.details if d and d not in text)
        return f"{text} ({extra})" if extra else text


AUTH_NONE, AUTH_TOKEN, AUTH_APIKEY, AUTH_OAUTH = "none", "token", "apikey", "oauth"
TOKEN_CODES = (498, 499)        # invalid / expired token, token required


@dataclass
class Connection:
    """One saved server: an ArcGIS Server services directory, a Portal for ArcGIS or ArcGIS Online."""
    name: str
    url: str
    auth: str = AUTH_NONE
    username: str = ""
    password: str = ""          # kept in memory; stored encrypted by connections.py, never in plain text
    api_key: str = ""
    client_id: str = ""         # OAuth: the application id registered on the portal
    referer: str = ""
    verify_ssl: bool = True
    token: str = ""             # current token (memory only)
    token_expires: float = 0.0  # seconds since the epoch
    refresh_token: str = ""     # OAuth (memory only)
    extra: dict = field(default_factory=dict)

    @property
    def is_portal(self):
        low = self.url.lower()
        return "/sharing" in low or "arcgis.com" in low and "/rest/services" not in low or low.rstrip("/").endswith(
            "/portal")


def services_root(url):
    """The REST services directory for what the user typed: a server (…/arcgis), its services directory,
    or any service / layer address on it."""
    u = (url or "").strip().rstrip("/")
    if not u:
        return ""
    if not u.lower().startswith(("http://", "https://")):
        u = "https://" + u
    low = u.lower()
    i = low.find("/rest/services")
    if i >= 0:
        return u[:i + len("/rest/services")]
    return u + "/rest/services"


def split_service_url(url):
    """https://h/arcgis/rest/services/Folder/Name/FeatureServer/3 -> (root, "Folder/Name", "FeatureServer", 3)."""
    root = services_root(url)
    rest = url.strip().rstrip("/")[len(root):].strip("/")
    parts = [p for p in rest.split("/") if p]
    for i, p in enumerate(parts):
        if p in ("FeatureServer", "MapServer", "ImageServer", "VectorTileServer", "SceneServer", "GPServer",
                 "GeometryServer", "GeocodeServer", "NAServer"):
            layer = int(parts[i + 1]) if i + 1 < len(parts) and parts[i + 1].isdigit() else None
            return root, "/".join(parts[:i]), p, layer
    return root, "/".join(parts), "", None


class Client:
    """Talks to one connection. opener: a urllib opener (tests may pass their own)."""

    def __init__(self, conn, timeout=30, opener=None):
        self.conn = conn
        self.timeout = timeout
        self.root = services_root(conn.url) if not conn.is_portal else ""
        self._ctx = None if conn.verify_ssl else ssl._create_unverified_context()  # noqa: S323 - user's choice
        self._opener = opener
        self._token_url = None

    # ---- low level -------------------------------------------------------------------------------------------
    def _open(self, req):
        if self._opener is not None:
            return self._opener.open(req, timeout=self.timeout)
        return urllib.request.urlopen(req, timeout=self.timeout, context=self._ctx)  # noqa: S310 - http(s) only

    def raw(self, url, params=None, post=False, headers=None):
        """One HTTP request -> parsed JSON (dict); raises RestError."""
        params = dict(params or {})
        params.setdefault("f", "json")
        data = urllib.parse.urlencode(params).encode("utf-8")
        hdrs = {"User-Agent": "Biruni GIS", "Accept": "application/json"}
        if self.conn.referer:
            hdrs["Referer"] = self.conn.referer
        hdrs.update(headers or {})
        if post or len(data) > 1800:
            req = urllib.request.Request(url, data=data, headers=dict(
                hdrs, **{"Content-Type": "application/x-www-form-urlencoded"}), method="POST")
        else:
            req = urllib.request.Request(url + ("&" if "?" in url else "?") + data.decode("ascii"), headers=hdrs)
        try:
            with self._open(req) as r:
                body = r.read()
        except urllib.error.HTTPError as e:
            body = e.read() or b""
            try:
                d = json.loads(body.decode("utf-8", "replace"))
            except ValueError:
                raise RestError(f"HTTP {e.code}: {e.reason}", e.code) from e
            return self._check(d, e.code)
        except (urllib.error.URLError, OSError, ValueError) as e:
            reason = getattr(e, "reason", e)
            raise RestError(f"The server could not be reached: {reason}") from e
        try:
            d = json.loads(body.decode("utf-8-sig"))
        except ValueError as e:
            raise RestError("The server did not answer with ArcGIS REST data (is the address right?)") from e
        return self._check(d)

    @staticmethod
    def _check(d, http=None):
        if isinstance(d, dict) and isinstance(d.get("error"), dict):
            err = d["error"]
            raise RestError(err.get("message") or "Error", err.get("code") or http,
                            [str(x) for x in err.get("details") or []])
        if http and http >= 400:
            raise RestError(f"HTTP {http}", http)
        return d

    def request(self, url, params=None, post=False):
        """Request with the connection's credentials; a refused or expired token is renewed once."""
        params = dict(params or {})
        token = self.ensure_token()
        if token:
            params["token"] = token
        try:
            return self.raw(url, params, post)
        except RestError as e:
            if e.code in TOKEN_CODES and self.conn.auth in (AUTH_TOKEN, AUTH_OAUTH):
                self.conn.token = ""
                token = self.ensure_token(force=True)
                if token:
                    params["token"] = token
                    return self.raw(url, params, post)
            if e.code in TOKEN_CODES and self.conn.auth == AUTH_NONE:
                raise RestError("This service needs a sign-in: edit the connection and enter a user name and "
                                "password (or an API key).", e.code) from e
            raise

    # ---- signing in --------------------------------------------------------------------------------------------
    def ensure_token(self, force=False):
        c = self.conn
        if c.auth == AUTH_APIKEY:
            return c.api_key
        if c.auth not in (AUTH_TOKEN, AUTH_OAUTH):
            return ""
        if c.token and not force and c.token_expires - time.time() > 60:
            return c.token
        if c.auth == AUTH_OAUTH:
            if c.refresh_token:
                return self.refresh_oauth()
            if c.token and not force:
                return c.token
            raise RestError("Sign in with your ArcGIS account first.", 499)
        return self.generate_token()

    def token_url(self):
        """Where tokens are made: the server's own token service (from /rest/info) or the portal's."""
        if self._token_url:
            return self._token_url
        if self.conn.is_portal:
            self._token_url = self.portal_url() + "/sharing/rest/generateToken"
            return self._token_url
        try:
            info = self.raw(self.root.rsplit("/services", 1)[0] + "/info", {})
            url = (info.get("authInfo") or {}).get("tokenServicesUrl")
        except RestError:
            url = None
        if not url:
            url = self.root.split("/rest/services")[0] + "/tokens/generateToken"
        self._token_url = url
        return url

    def generate_token(self):
        c = self.conn
        if not c.username or not c.password:
            raise RestError("Enter the user name and password of this connection.", 499)
        params = {"username": c.username, "password": c.password, "expiration": 120}
        if c.referer:
            params.update(client="referer", referer=c.referer)
        else:
            params["client"] = "requestip"
        try:
            d = self.raw(self.token_url(), params, post=True)
        except RestError as e:
            if e.code in (400, 498, 499) or "invalid" in str(e).lower():
                raise RestError("The user name or password is wrong.", e.code, e.details) from e
            raise
        if not d.get("token"):
            raise RestError("The server did not give a token.")
        c.token = d["token"]
        expires = d.get("expires") or 0
        c.token_expires = expires / 1000 if expires > 1e11 else (time.time() + 7200 if not expires else expires)
        return c.token

    # -- OAuth 2 (Portal / ArcGIS Online): authorization code with PKCE, redirect to this PC --
    def portal_url(self):
        u = self.conn.url.strip().rstrip("/")
        if not u.lower().startswith(("http://", "https://")):
            u = "https://" + u
        i = u.lower().find("/sharing")
        return u[:i] if i >= 0 else u

    def oauth_start(self, redirect_uri):
        """-> (authorize URL to open in the browser, state) - the PKCE verifier is kept for oauth_finish."""
        if not self.conn.client_id:
            raise RestError("Enter the application id (client id) registered on your portal.")
        verifier = base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b"=").decode("ascii")
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
        state = secrets.token_urlsafe(12)
        self.conn.extra.update(verifier=verifier, redirect_uri=redirect_uri, state=state)
        q = urllib.parse.urlencode({"client_id": self.conn.client_id, "response_type": "code", "state": state,
                                    "redirect_uri": redirect_uri, "code_challenge": challenge,
                                    "code_challenge_method": "S256", "expiration": 20160})
        return self.portal_url() + "/sharing/rest/oauth2/authorize?" + q, state

    def oauth_finish(self, code):
        x = self.conn.extra
        d = self.raw(self.portal_url() + "/sharing/rest/oauth2/token",
                     {"client_id": self.conn.client_id, "grant_type": "authorization_code", "code": code,
                      "redirect_uri": x.get("redirect_uri", ""), "code_verifier": x.get("verifier", "")}, post=True)
        return self._take_oauth(d)

    def refresh_oauth(self):
        d = self.raw(self.portal_url() + "/sharing/rest/oauth2/token",
                     {"client_id": self.conn.client_id, "grant_type": "refresh_token",
                      "refresh_token": self.conn.refresh_token}, post=True)
        return self._take_oauth(d)

    def _take_oauth(self, d):
        if not d.get("access_token"):
            raise RestError("The portal did not give a sign-in token.")
        self.conn.token = d["access_token"]
        self.conn.token_expires = time.time() + float(d.get("expires_in") or 1800)
        if d.get("refresh_token"):
            self.conn.refresh_token = d["refresh_token"]
        if d.get("username"):
            self.conn.username = d["username"]
        return self.conn.token

    def oauth_login(self, open_browser, wait=300):
        """Whole browser sign-in: a one-shot listener on 127.0.0.1 receives the redirect.
        open_browser(url) opens the address (webbrowser.open in the app)."""
        import http.server
        import threading

        got = {}

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802
                q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
                got.update({k: v[0] for k, v in q.items()})
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.end_headers()
                self.wfile.write("<html><body style='font-family:sans-serif'><h3>Biruni GIS</h3>"
                                 "<p>You are signed in. You can close this tab.</p></body></html>".encode())

            def log_message(self, *a):
                pass

        srv = http.server.HTTPServer(("127.0.0.1", 0), Handler)
        redirect = f"http://127.0.0.1:{srv.server_address[1]}/biruni-oauth"
        url, state = self.oauth_start(redirect)
        t = threading.Thread(target=srv.handle_request, daemon=True)
        srv.timeout = wait
        t.start()
        open_browser(url)
        t.join(wait)
        srv.server_close()
        if got.get("error"):
            raise RestError(got.get("error_description") or got["error"])
        if not got.get("code") or got.get("state") != state:
            raise RestError("The sign-in was not completed.")
        return self.oauth_finish(got["code"])

    def portal_servers(self):
        """Hosted and federated servers of a portal -> [services directory URL]."""
        out = []
        try:
            d = self.request(self.portal_url() + "/sharing/rest/portals/self", {})
        except RestError:
            return out
        if d.get("urlKey") and "arcgis.com" in self.portal_url().lower():
            out.append(f"https://services.arcgis.com/{d.get('id', '')}/arcgis/rest/services")
        try:
            s = self.request(self.portal_url() + f"/sharing/rest/portals/{d.get('id', 'self')}/servers", {})
            out += [srv.get("url").rstrip("/") + "/rest/services" for srv in s.get("servers") or [] if srv.get("url")]
        except RestError:
            pass
        return list(dict.fromkeys(out))

    # ---- browsing ----------------------------------------------------------------------------------------------
    def catalog(self, folder=""):
        """{'folders': [...], 'services': [{'name', 'type'}]} of the root or of a folder."""
        url = self.root + (("/" + urllib.parse.quote(folder)) if folder else "")
        d = self.request(url)
        return {"folders": list(d.get("folders") or []),
                "services": [s for s in d.get("services") or [] if isinstance(s, dict)]}

    def service_url(self, name, stype):
        return f"{self.root}/{urllib.parse.quote(name)}/{stype}"

    def info(self, url):
        return self.request(url)

    def layers(self, service_url):
        """Layers and tables of a feature / map service -> [{'id', 'name', 'type', 'geometryType', 'table'}]."""
        d = self.request(service_url)
        out = []
        for key, table in (("layers", False), ("tables", True)):
            for lyr in d.get(key) or []:
                if lyr.get("subLayerIds"):     # a group layer of a map service: its children are listed too
                    continue
                out.append({"id": lyr.get("id"), "name": lyr.get("name") or str(lyr.get("id")),
                            "type": lyr.get("type") or ("Table" if table else "Feature Layer"),
                            "geometryType": lyr.get("geometryType") or "", "table": table})
        return out, d

    # ---- reading -----------------------------------------------------------------------------------------------
    def query(self, layer_url, **params):
        params.setdefault("where", "1=1")
        return self.request(layer_url.rstrip("/") + "/query", params, post=True)

    def query_all(self, layer_url, where="1=1", out_fields="*", geometry=True, out_sr=None, info=None,
                  progress=None):
        """Every feature that matches -> list of {'attributes', 'geometry'} (pages through the layer)."""
        info = info or self.info(layer_url)
        page = int(info.get("maxRecordCount") or 1000) or 1000
        base = {"where": where, "outFields": out_fields, "returnGeometry": "true" if geometry else "false"}
        if out_sr:
            base["outSR"] = out_sr
        adv = info.get("advancedQueryCapabilities") or {}
        feats = []
        if adv.get("supportsPagination") or info.get("supportsPagination"):
            oid = oid_field(info)
            offset = 0
            while True:
                d = self.query(layer_url, resultOffset=offset, resultRecordCount=page,
                               orderByFields=oid or "", **base)
                got = d.get("features") or []
                feats += got
                if progress:
                    progress(len(feats))
                if not got or not (d.get("exceededTransferLimit") or len(got) >= page):
                    break
                offset += len(got)
            return feats
        ids = self.query(layer_url, where=where, returnIdsOnly="true").get("objectIds") or []
        ids.sort()
        for k in range(0, len(ids), page):
            chunk = ids[k:k + page]
            d = self.query(layer_url, objectIds=",".join(str(i) for i in chunk), **dict(base, where="1=1"))
            feats += d.get("features") or []
            if progress:
                progress(len(feats))
        return feats

    # ---- editing -----------------------------------------------------------------------------------------------
    def apply_edits(self, layer_url, adds=None, updates=None, deletes=None, rollback=False):
        params = {"rollbackOnFailure": "true" if rollback else "false"}
        if adds:
            params["adds"] = json.dumps(adds)
        if updates:
            params["updates"] = json.dumps(updates)
        if deletes:
            params["deletes"] = ",".join(str(i) for i in deletes)
        return self.request(layer_url.rstrip("/") + "/applyEdits", params, post=True)


# ---- layer facts -------------------------------------------------------------------------------------------------
def oid_field(info):
    if info.get("objectIdField"):
        return info["objectIdField"]
    for f in info.get("fields") or []:
        if f.get("type") == "esriFieldTypeOID":
            return f.get("name")
    return ""


def capabilities(info):
    """Lower-case capability names of a layer or service: query, create, update, delete, editing, sync..."""
    return {c.strip().lower() for c in (info.get("capabilities") or "").split(",") if c.strip()}


def edit_rights(info):
    """-> dict(add=, update=, delete=) from the layer's capabilities (Editing must be on too)."""
    caps = capabilities(info)
    editing = "editing" in caps
    upd = editing and "update" in caps
    return {"add": editing and "create" in caps, "update": upd, "delete": editing and "delete" in caps,
            "geometry": upd and info.get("allowGeometryUpdates", True) is not False}


def edit_rights_text(info):
    """A sentence about what may be edited on this layer (English; the UI translates)."""
    r = edit_rights(info)
    if not any((r["add"], r["update"], r["delete"])):
        return "This layer is read-only: its service does not allow editing."
    can = [w for w, k in (("add", "add"), ("change", "update"), ("delete", "delete")) if r[k]]
    cannot = [w for w, k in (("add", "add"), ("change", "update"), ("delete", "delete")) if not r[k]]
    text = "You can " + ", ".join(can) + " features."
    if cannot:
        text += " The service does not allow you to " + " or ".join(cannot) + " them."
    return text


def now_ms():
    return int(time.time() * 1000)
