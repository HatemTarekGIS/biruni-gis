"""How the computers of a room talk and find each other.

Every request and answer is one sealed JSON message (AES-256-GCM with the room key, see
code.py) carrying the time and a random id: a message that is old, repeated or sealed with
another key is refused. The transport is plain HTTP; the sealing is what protects it, so it
can pass over the office network, a direct link between the computers (link.py) or anything else unchanged.

Finding the host:
  * on the same network: a UDP broadcast "who holds room <tag>?"; the host answers with its
    sealed address. No address to type, no port to open.
  * from outside: the host leaves its sealed address ("link:", a direct link, see link.py) on
    ntfy.sh under the room's topic; members read the latest one. ntfy sees only unreadable bytes.
A host answers with its epoch (how many times the room changed host) and its rank, so when
two computers both think they hold the room the right one wins (node.py).
"""
import http.client
import json
import os
import secrets
import socket
import threading
import time
import urllib.error
import urllib.request
from urllib.parse import urlsplit

from ..secure import crypto

PORT = 47213                 # the room's HTTP port (the next free one is taken when busy)
DISCOVERY_PORT = 47214       # UDP broadcasts on the local network
NTFY = os.environ.get("BIRUNI_TEAM_NTFY", "https://ntfy.sh/")
MAX_AGE = 10 * 60            # seconds a sealed message stays valid
MAX_OPEN = 256 * 2**20       # bytes a message may grow to once decompressed (a small "zip bomb" must not fill the memory)
AAD = b"biruni-team-1"


class WireError(Exception):
    """The other computer cannot be reached or answered with an error."""

    def __init__(self, text, status=0):
        super().__init__(text)
        self.status = status


class Sealer:
    """Seals and opens messages with one room key, refusing replays."""

    def __init__(self, key):
        self.key = key
        self._seen = {}
        self._lock = threading.Lock()

    def seal(self, obj):
        msg = {"ts": time.time(), "n": secrets.token_hex(8), "m": obj}
        return crypto.seal(self.key, crypto.canonical(msg), AAD, compress=True)

    def open(self, blob, replay=True):
        try:
            msg = json.loads(crypto.unseal(self.key, blob, AAD, max_size=MAX_OPEN).decode("utf-8"))
        except (crypto.CryptoError, ValueError, UnicodeDecodeError):
            raise WireError("wrong room key", 403) from None
        now = time.time()
        if abs(now - float(msg.get("ts", 0))) > MAX_AGE:
            raise WireError("message too old - check the computer's clock", 403)
        if replay:
            with self._lock:
                if msg.get("n") in self._seen:
                    raise WireError("repeated message", 403)
                self._seen[msg.get("n")] = now
                if len(self._seen) > 20000:
                    self._seen = {k: t for k, t in self._seen.items() if now - t < MAX_AGE}
        return msg.get("m")


class Client:
    """Requests to one host address."""

    def __init__(self, sealer, address):
        self.sealer = sealer
        if address.startswith("link:"):   # a host outside this network: the link's door on this computer
            from . import link

            address = link.member_address(sealer.key)
        self.address = address.rstrip("/")
        u = urlsplit(self.address)
        self._https = u.scheme == "https"
        self._host = u.hostname
        self._port = u.port or (443 if self._https else 80)
        self._base = u.path.rstrip("/")
        self._local = threading.local()

    def _conn(self, timeout):
        c = getattr(self._local, "conn", None)
        if c is None:
            cls = http.client.HTTPSConnection if self._https else http.client.HTTPConnection
            c = cls(self._host, self._port, timeout=timeout)
            self._local.conn = c
        c.timeout = timeout
        if c.sock is not None:
            c.sock.settimeout(timeout)
        return c

    def _drop(self):
        c = getattr(self._local, "conn", None)
        self._local.conn = None
        if c is not None:
            try:
                c.close()
            except OSError:
                pass

    def call(self, path, obj=None, timeout=30, raw=False):
        """One sealed request on this thread's kept-open connection; a connection the other side closed is
        opened again once (the message is sealed again, so it is never a repeat)."""
        for attempt in (0, 1):
            c = self._conn(timeout)
            try:
                c.request("POST", self._base + "/" + path, body=self.sealer.seal(obj or {}),
                          headers={"Content-Type": "application/octet-stream"})
                r = c.getresponse()
                body = r.read()
                status = r.status
                if r.getheader("Connection", "").lower() == "close":
                    self._drop()
                break
            except (http.client.RemoteDisconnected, BrokenPipeError, ConnectionResetError,
                    ConnectionAbortedError, http.client.CannotSendRequest, http.client.BadStatusLine) as e:
                self._drop()
                if attempt:
                    raise WireError(str(e)) from None
            except (OSError, http.client.HTTPException) as e:
                self._drop()
                raise WireError(str(e)) from None
        if status >= 400:
            text = body[:300]
            try:
                text = self.sealer.open(body, replay=False).get("error", "")
            except Exception:  # noqa: BLE001 - an unsealed error text
                text = text.decode("utf-8", "replace")
            raise WireError(text or f"HTTP {status}", status)
        if raw:
            return body
        return self.sealer.open(body)

    def download(self, path, obj, dest, progress=None, cancelled=lambda: False):
        """A sealed file in pieces of 4 MB to dest."""
        done = 0
        with open(dest, "wb") as f:
            while True:
                if cancelled():
                    raise WireError("cancelled")
                for attempt in range(6):  # a weak connection: the same piece again, not the whole file
                    try:
                        part = self.call(path, dict(obj, offset=done), timeout=120)
                        break
                    except WireError as e:
                        if attempt == 5 or e.status in (401, 403, 404):
                            raise
                        time.sleep(1 + attempt * 2)
                data = crypto.unb64(part["data"])
                f.write(data)
                done += len(data)
                if progress:
                    progress(done, part["size"])
                if done >= part["size"] or not data:
                    return done


# ---- finding the host on the local network --------------------------------------------------------
ANNOUNCED = ("port", "url", "epoch", "rank", "since", "on", "hid", "head", "name", "host", "sid")


def announce_body(tag, info):
    """What a host signs when it says where the room is: everything it announces, for this room only."""
    return crypto.canonical({"room": tag, **{k: info.get(k) for k in ANNOUNCED}})


def sign_announce(seed, tag, info):
    return dict(info, sig=crypto.sign(bytes.fromhex(seed), announce_body(tag, info)).hex())


def announce_ok(tag, info, roster):
    """True when ``info`` was signed by a member of ``roster`` who was not removed. A room code alone must not let
    anyone say "I am the host now" (members would follow it and take its copy of the map)."""
    m = next((r for r in roster or [] if r.get("id") == info.get("hid")), None)
    if m is None or m.get("state") == "out" or not m.get("pub"):
        return False
    try:
        return crypto.verify(bytes.fromhex(m["pub"]), bytes.fromhex(str(info.get("sig") or "00")),
                             announce_body(tag, info))
    except (ValueError, TypeError):
        return False


def box_key(seed):
    """(private, public hex) X25519 key of a member, made from its signing seed: a new room code is sealed to it,
    so that only the members still in the room can read it (a removed member who listens on the network learns
    nothing)."""
    priv = crypto.hkdf(bytes.fromhex(seed), b"biruni-team", b"box-key")
    return priv, crypto.box_public(priv).hex()


def rekey_aad(cid):
    return f"biruni-team-new-code|{cid}".encode("utf-8")


def hello_proof(tag, cid, nonce):
    """What a joining computer signs: the host's nonce, for this room and this member only."""
    return f"biruni-team-hello|{tag}|{cid}|{nonce}".encode("utf-8")


def say_hello(client, hello, seed, tag, timeout=20):
    """Join with proof of the private key: the host answers the first call with a nonce, the second call carries
    the nonce signed with this computer's Ed25519 seed (hex). -> the host's answer."""
    r = client.call("hello", hello, timeout=timeout)
    if r.get("status") != "prove":
        return r
    nonce = str(r.get("nonce", ""))
    proof = crypto.sign(bytes.fromhex(seed), hello_proof(tag, hello["id"], nonce)).hex()
    return client.call("hello", dict(hello, nonce=nonce, proof=proof), timeout=timeout)


def local_addresses():
    """This computer's IPv4 addresses on its networks (not the loopback)."""
    found = set()
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            found.add(info[4][0])
    except OSError:
        pass
    try:  # the address used to reach the outside, when there is one
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("192.0.2.1", 9))
        found.add(s.getsockname()[0])
        s.close()
    except OSError:
        pass
    return sorted(a for a in found if not a.startswith("127.") and not a.startswith("169.254."))


class Beacon:
    """The host's answer to "who holds room <tag>?" broadcasts. info() gives what to answer."""

    def __init__(self, keys, sealer, info):
        self.keys = keys
        self.sealer = sealer
        self.info = info
        self.sock = None
        self._stop = threading.Event()

    def start(self):
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind(("", DISCOVERY_PORT))
        except OSError:
            s.close()
            return False
        s.settimeout(0.5)
        self.sock = s
        threading.Thread(target=self._run, daemon=True).start()
        return True

    def _run(self):
        while not self._stop.is_set():
            try:
                data, peer = self.sock.recvfrom(2048)
            except socket.timeout:
                continue
            except OSError:
                break
            if not data.startswith(b"BIRUNI-TEAM?") or data[12:28].decode("ascii", "replace") != self.keys.tag:
                continue
            try:
                reply = self.info()
                if reply:
                    self.sock.sendto(b"BIRUNI-TEAM!" + self.sealer.seal(reply), peer)
            except OSError:
                pass

    def stop(self):
        self._stop.set()
        if self.sock is not None:
            try:
                self.sock.close()
            except OSError:
                pass


def find_on_network(keys, sealer, wait=1.5, targets=None):
    """The hosts of this room that answer on the local network: [info dict]."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    s.settimeout(0.25)
    probe = b"BIRUNI-TEAM?" + keys.tag.encode("ascii")
    targets = targets or ["255.255.255.255", "127.0.0.1"]
    for a in local_addresses():  # the directed broadcast of each /24 too: some routers drop 255.255.255.255
        parts = a.split(".")
        targets.append(".".join(parts[:3] + ["255"]))
    found = []
    end = time.time() + wait
    sent = 0
    while time.time() < end:
        if sent < 3 and time.time() > end - wait + sent * 0.4:
            for t in dict.fromkeys(targets):
                try:
                    s.sendto(probe, (t, DISCOVERY_PORT))
                except OSError:
                    pass
            sent += 1
        try:
            data, peer = s.recvfrom(4096)
        except socket.timeout:
            continue
        except OSError:
            break
        if not data.startswith(b"BIRUNI-TEAM!"):
            continue
        try:
            info = sealer.open(data[12:], replay=False)
        except WireError:
            continue
        if isinstance(info, dict) and info.get("port"):
            info["address"] = f"http://{peer[0]}:{int(info['port'])}"
            if not any(f["address"] == info["address"] for f in found):
                found.append(info)
    s.close()
    return found


# ---- finding the host from outside ------------------------------------------------------------------
def publish_address(keys, sealer, info):
    """Leave the sealed address on ntfy.sh (kept there for some hours)."""
    body = crypto.b64(sealer.seal(info))
    req = urllib.request.Request(NTFY + keys.topic, data=body.encode("ascii"), method="POST",
                                 headers={"Title": "-", "Firebase": "no"})
    with urllib.request.urlopen(req, timeout=15) as r:
        r.read()


def find_published(keys, sealer, since="12h"):
    """The addresses the hosts of this room left on ntfy.sh, newest first: [info dict]."""
    try:
        with urllib.request.urlopen(f"{NTFY}{keys.topic}/json?poll=1&since={since}", timeout=15) as r:
            lines = r.read().decode("utf-8", "replace").splitlines()
    except (urllib.error.URLError, OSError, TimeoutError):
        return []
    found = []
    for line in lines:
        try:
            msg = json.loads(line)
            if msg.get("event") != "message":
                continue
            info = sealer.open(crypto.unb64(msg.get("message", "")), replay=False)
        except Exception:  # noqa: BLE001 - not ours, altered or expired
            continue
        if isinstance(info, dict) and info.get("url"):
            info["address"] = info["url"]
            found.append(info)
    found.sort(key=lambda i: (-int(i.get("epoch", 0)), float(-i.get("since", 0))))
    return found
