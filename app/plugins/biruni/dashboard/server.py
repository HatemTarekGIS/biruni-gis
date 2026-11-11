"""The dashboard's own transports on this PC (SPEC §5): the LAN page and the place the hidden bridge host
reaches the dashboards from. No server of ours or anyone's: this PC serves its phones itself.

* SessionHub - the viewers (one session per open page), thread safe: what each browser sent waits in
  ``inbox`` for Biruni's main thread; what the PC says waits in each session's outbox for the
  browser's long poll (LAN) or the host page (bridge).
* Gateway   - in Biruni's main thread: opens a DashboardService session for every new viewer, hands it
  the browser's messages and closes the sessions nobody polls any more.
* LanServer - HTTPS with this PC's own certificate (the phone tools' one), a random token in every
  address, size and rate limits. ``/d/<token>/`` is the page, ``m`` takes a message, ``ev`` waits for
  the PC's next ones. ``host`` (127.0.0.1 only) is the hidden page that meets the viewers from anywhere
  (host.py).

LAN wire (SPEC "Changes"):  POST m {"s": sid|null, "m": msg} -> {"s": sid, "out": [msgs], "n": count};
only the first POST (no sid: the hello) carries "out"; later answers come by the long poll alone.
GET ev?s=sid&n=<count seen> -> {"out": [msgs], "n": count} (long poll, at most POLL_WAIT s).
"""
import json
import secrets
import ssl
import threading
import time
from http.server import BaseHTTPRequestHandler
from urllib.parse import parse_qs, urlsplit

from ..local_server import ExclusiveServer

DASH_PORT = 10116        # 10111 GPS, 10112 photos, 10113 see my map, 10114 GPS bridge, 10115 phone hub
PORT_TRIES = 10
MAX_BODY = 16 * 1024     # a browser message is small (a hello with its card, a filter, a tap)
MAX_SESSIONS = 60        # viewers at once
MAX_OUTBOX = 400         # messages kept for a slow viewer; the oldest "data" goes first
POLL_WAIT = 25.0         # seconds a long poll waits for news
SESSION_IDLE = 90.0      # a session nobody polls for this long is closed
RATE_N, RATE_S = 60, 10  # at most RATE_N messages per RATE_S seconds from one address
TRANSPORTS = ("lan", "bridge", "preview")


class Session:
    def __init__(self, sid, transport, addr):
        self.sid = sid
        self.transport = transport
        self.addr = addr
        self.out = []          # (number, msg) - number counts every message ever queued
        self.count = 0
        self.seen = time.monotonic()
        self.since = time.time()
        self.closed = False


class SessionHub:
    """The viewers of this PC's dashboards. Every method is safe from any thread."""

    def __init__(self):
        self._lock = threading.Condition()
        self.sessions = {}
        self.inbox = []        # (sid, msg) from the browsers, for the main thread
        self.gone = []         # sids the main thread must close
        self._rates = {}
        self.stopping = False

    # ---- from the browsers (server threads) ------------------------------------------
    def rate_ok(self, addr):
        now = time.monotonic()
        with self._lock:
            times = [t for t in self._rates.get(addr, []) if now - t < RATE_S]
            if len(times) >= RATE_N:
                self._rates[addr] = times
                return False
            times.append(now)
            self._rates[addr] = times
            if len(self._rates) > 2000:
                self._rates = {a: v for a, v in self._rates.items() if v and now - v[-1] < RATE_S}
            return True

    def open(self, transport, addr):
        """A new session id, or None when full or stopping."""
        with self._lock:
            if self.stopping or len(self.sessions) >= MAX_SESSIONS:
                return None
            sid = secrets.token_urlsafe(12)
            self.sessions[sid] = Session(sid, transport if transport in TRANSPORTS else "lan", addr)
            return sid

    def post(self, sid, msg):
        with self._lock:
            s = self.sessions.get(sid)
            if s is None or s.closed:
                return False
            s.seen = time.monotonic()
            self.inbox.append((sid, msg))
            self._lock.notify_all()
            return True

    def take(self, sid, seen, wait=POLL_WAIT):
        """([msgs], count) newer than ``seen``; waits up to ``wait`` s for some. None for an unknown sid."""
        end = time.monotonic() + max(0.0, wait)
        with self._lock:
            s = self.sessions.get(sid)
            if s is None:
                return None
            s.seen = time.monotonic()
            while not self.stopping and not s.closed and (not s.out or s.out[-1][0] <= seen):
                left = end - time.monotonic()
                if left <= 0:
                    break
                self._lock.wait(min(left, 1.0))
                s.seen = time.monotonic()
            msgs = [m for n, m in s.out if n > seen]
            # what the browser has seen is not needed again
            s.out = [(n, m) for n, m in s.out if n > seen]
            if (self.stopping or s.closed) and not msgs:
                msgs = [{"t": "bye"}]
            return msgs, s.count

    def drop(self, sid):
        with self._lock:
            s = self.sessions.pop(sid, None)
            if s is not None:
                s.closed = True
                self.gone.append(sid)
                self._lock.notify_all()

    # ---- from the main thread ----------------------------------------------------------
    def push(self, sid, msg):
        with self._lock:
            s = self.sessions.get(sid)
            if s is None:
                return
            s.count += 1
            s.out.append((s.count, msg))
            if len(s.out) > MAX_OUTBOX:  # a viewer that does not read: keep the newest, drop old numbers first
                for i, (_n, m) in enumerate(s.out):
                    if isinstance(m, dict) and m.get("t") == "data":
                        del s.out[i]
                        break
                else:
                    del s.out[0]
            if isinstance(msg, dict) and msg.get("t") in ("bye", "error") and msg.get("why") in (
                    "expired", "revoked", "bad card"):
                s.closed = True
            self._lock.notify_all()

    def drain(self):
        """[(sid, msg)] waiting, [sid] gone or idle - for the main thread."""
        now = time.monotonic()
        with self._lock:
            got, self.inbox = self.inbox, []
            for sid, s in list(self.sessions.items()):
                if now - s.seen > SESSION_IDLE:
                    del self.sessions[sid]
                    s.closed = True
                    self.gone.append(sid)
            gone, self.gone = self.gone, []
            return got, gone

    def info(self, sid):
        with self._lock:
            s = self.sessions.get(sid)
            return None if s is None else {"transport": s.transport, "addr": s.addr, "since": s.since}

    def stop(self):
        with self._lock:
            self.stopping = True
            self._lock.notify_all()


def _gateway_class():
    from qgis.PyQt.QtCore import QObject, QTimer

    class Gateway(QObject):
        """Main thread: the DashboardService sessions of the hub's viewers."""

        def __init__(self, hub, service, parent=None):
            super().__init__(parent)
            self.hub = hub
            self.service = service
            self.sessions = {}
            self.previews = {}       # preview key -> the definition the builder is editing
            self.preview_of = {}     # sid -> preview key
            self.timer = QTimer(self)
            self.timer.setInterval(40)
            self.timer.timeout.connect(self.pump)
            self.timer.start()

        def pump(self):
            got, gone = self.hub.drain()
            for sid in gone:
                self.preview_of.pop(sid, None)
                sess = self.sessions.pop(sid, None)
                if sess is not None:
                    _quiet(sess.close)
            for sid, msg in got:
                sess = self.sessions.get(sid)
                if sess is None:
                    info = self.hub.info(sid)
                    if info is None:
                        continue
                    pkey = preview_key(msg)
                    if pkey is not None:
                        d = self.previews.get(pkey)
                        if d is None:
                            self.hub.push(sid, {"t": "error", "why": "ended"})
                            self.hub.push(sid, {"t": "bye"})
                            continue
                        sess = self.service.connect(lambda m, sid=sid: self.hub.push(sid, m), transport="preview",
                                                    definition=d)
                        self.preview_of[sid] = pkey
                    else:
                        sess = self.service.connect(lambda m, sid=sid: self.hub.push(sid, m),
                                                    transport=info["transport"])
                    self.sessions[sid] = sess
                try:
                    sess.receive(msg)
                except Exception as e:  # noqa: BLE001 - one bad message never takes the dashboard down
                    self.hub.push(sid, {"t": "error", "why": "failed", "detail": str(e)[:200]})

        # ---- the builder's previews ------------------------------------------------------------------------------
        def set_preview(self, key, definition):
            """Show ``definition`` to every page opened with this preview key (now and later)."""
            self.previews[key] = definition
            for sid, k in list(self.preview_of.items()):
                sess = self.sessions.get(sid)
                if k == key and sess is not None:
                    try:
                        self.service.preview(sess, definition)
                    except Exception:  # noqa: BLE001
                        pass

        def end_preview(self, key):
            self.previews.pop(key, None)
            for sid, k in list(self.preview_of.items()):
                if k == key:
                    self.hub.push(sid, {"t": "error", "why": "ended"})
                    self.hub.push(sid, {"t": "bye"})
                    self.hub.drop(sid)

        def previewers(self, key):
            return sum(1 for k in self.preview_of.values() if k == key)

        def stop(self):
            self.timer.stop()
            for sess in self.sessions.values():
                _quiet(sess.close)
            self.sessions.clear()

    return Gateway


def Gateway(hub, service, parent=None):  # noqa: N802 - built lazily so this module imports without Qt
    return _gateway_class()(hub, service, parent)


PREVIEW = "PREVIEW."


def preview_key(msg):
    """The preview key of a hello opened from a builder preview link (``#c=PREVIEW.<key>``), else None."""
    if isinstance(msg, dict) and msg.get("t") == "hello":
        c = msg.get("card")
        if isinstance(c, str) and c.startswith(PREVIEW) and 16 <= len(c) - len(PREVIEW) <= 64:
            return c[len(PREVIEW):]
    return None


def _quiet(fn, *a):
    try:
        fn(*a)
    except Exception:  # noqa: BLE001
        pass


def _json(raw):
    try:
        v = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return None
    return v if isinstance(v, dict) else None


class LanServer:
    """HTTPS in a background thread. The page comes from page_fn() (bytes), made once per server."""

    def __init__(self, hub, page_fn, cert_folder, addresses=(), port=DASH_PORT, bind="0.0.0.0", tries=PORT_TRIES,
                 host_page_fn=None):
        from ..gps.browser import ensure_certificate

        self.hub = hub
        self.token = secrets.token_urlsafe(12)
        self.cert, self.key = ensure_certificate(cert_folder, list(addresses))
        self._page_fn = page_fn
        self._page = None
        self._host_page_fn = host_page_fn
        self.addresses = list(addresses)
        self.events = []       # ("viewer", user agent) - the dock lists who opened the page
        server = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *_a):
                pass

            def _send(self, code, body=b"", ctype="application/json", headers=None):
                try:
                    self.send_response(code)
                    self.send_header("Content-Type", ctype)
                    self.send_header("Content-Length", str(len(body)))
                    self.send_header("Cache-Control", "no-store")
                    self.send_header("X-Content-Type-Options", "nosniff")
                    self.send_header("Referrer-Policy", "no-referrer")
                    if ctype.startswith("text/html"):
                        self.send_header("Content-Security-Policy",
                                         "default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; "
                                         "connect-src 'self' wss: https:; img-src data: blob:; font-src data:; "
                                         "base-uri 'none'; form-action 'none'; frame-ancestors 'none'")
                    for k, v in (headers or {}).items():
                        self.send_header(k, v)
                    self.end_headers()
                    if body:
                        self.wfile.write(body)
                except OSError:
                    self.close_connection = True

            def _route(self):
                parts = urlsplit(self.path)
                bits = parts.path.strip("/").split("/")
                if len(bits) < 2 or bits[0] != "d" or not secrets.compare_digest(bits[1], server.token):
                    return None, parts
                return "/".join(bits[2:]), parts

            def do_GET(self):  # noqa: N802
                route, parts = self._route()
                if route is None:
                    self._send(404)
                elif route == "" and not parts.path.endswith("/"):
                    self._send(301, headers={"Location": f"/d/{server.token}/"})
                elif route in ("", "tv"):
                    agent = (self.headers.get("User-Agent") or "")[:160]
                    server.events.append(("viewer", agent))
                    del server.events[:-50]
                    self._send(200, server.page(), "text/html; charset=utf-8")
                elif route == "ev":
                    q = parse_qs(parts.query)
                    try:
                        seen = int(q.get("n", ["0"])[0])
                    except ValueError:
                        seen = 0
                    r = server.hub.take(q.get("s", [""])[0][:40], seen)
                    if r is None:
                        self._send(410, b'{"out":[{"t":"bye"}],"n":0}')
                    else:
                        self._send(200, json.dumps({"out": r[0], "n": r[1]}, ensure_ascii=False,
                                                   separators=(",", ":")).encode("utf-8"))
                elif route == "host" and server._host_page_fn is not None and self.client_address[0] == "127.0.0.1":
                    self._send(200, server._host_page_fn(), "text/html; charset=utf-8")
                else:
                    self._send(404)

            def do_POST(self):  # noqa: N802
                route, parts = self._route()
                if route is None:
                    self._send(404)
                    return
                try:
                    n = int(self.headers.get("Content-Length") or 0)
                except ValueError:
                    n = -1
                if n < 0 or n > MAX_BODY:
                    self.close_connection = True
                    self._send(413, b'{"error":"too large"}')
                    return
                if not server.hub.rate_ok(self.client_address[0]):
                    self._send(429, b'{"error":"slow down"}')
                    return
                body = _json(self.rfile.read(n) if n else b"")
                if route == "bye" and body is not None:
                    server.hub.drop(str(body.get("s") or "")[:40])
                    self._send(200, b'{"ok":true}')
                    return
                if route != "m" or body is None or not isinstance(body.get("m"), dict):
                    self._send(400, b'{"error":"bad message"}')
                    return
                transport = "lan"
                if body.get("tr") == "bridge" and self.client_address[0] == "127.0.0.1":
                    transport = "bridge"   # only the hidden host page on this PC speaks for bridge viewers
                sid = body.get("s")
                if not sid:
                    sid = server.hub.open(transport, self.client_address[0])
                    if sid is None:
                        self._send(503, b'{"error":"busy"}')
                        return
                sid = str(sid)[:40]
                if not server.hub.post(sid, body["m"]):
                    self._send(410, b'{"out":[{"t":"bye"}]}')
                    return
                # the first message (the hello) is answered here, so the first screen needs one round trip; after
                # that every answer comes by the long poll only (one road: nothing arrives twice)
                out, count = [], None
                if not body.get("s"):
                    r = server.hub.take(sid, 0, wait=3.0)
                    out, count = r if r is not None else ([], 0)
                self._send(200, json.dumps({"s": sid, "out": out, "n": count}, ensure_ascii=False,
                                           separators=(",", ":")).encode("utf-8"))

            do_PUT = do_DELETE = do_PATCH = lambda self: self._send(405)  # noqa: E731,N815

        last = None
        for p in range(port, port + max(1, tries)):
            try:
                self.httpd = ExclusiveServer((bind, p), Handler)
                break
            except OSError as e:
                last = e
        else:
            raise last
        self.port = self.httpd.server_address[1]
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(self.cert, self.key)
        self.httpd.socket = ctx.wrap_socket(self.httpd.socket, server_side=True)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def page(self):
        if self._page is None:
            self._page = self._page_fn()
        return self._page

    def path(self):
        return f"/d/{self.token}/"

    def url(self, host, card=None, lang="en", tv=False):
        """The address a viewer opens; the card goes after "#" (never sent to any server)."""
        frag = []
        if card:
            frag.append("c=" + card)
        frag.append("l=" + lang)
        if tv:
            frag.append("tv=1")
        return f"https://{host}:{self.port}/d/{self.token}/" + ("#" + "&".join(frag) if frag else "")

    def local_url(self):
        return f"https://127.0.0.1:{self.port}/d/{self.token}/"

    def stop(self):
        self.hub.stop()
        try:
            self.httpd.shutdown()
            self.httpd.server_close()
        except OSError:
            pass
