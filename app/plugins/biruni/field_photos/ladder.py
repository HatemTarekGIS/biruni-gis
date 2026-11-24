"""Bridge fallback ladder (PC side): sealed position envelope, dedupe/ack, and background quick tunnel."""
import base64
import binascii
import hashlib
import json
import math
import queue
import re
import secrets
import threading
import time
import urllib.parse
import zlib

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

DEV_RE = re.compile(r"^[A-Za-z0-9_-]{8,64}$")
B64U_RE = re.compile(r"^[A-Za-z0-9_-]+$")
ROUTES = frozenset(("lan", "direct", "relay", "peer", "mqtt", "org", "own", "tunnel", "drive"))
MAX_INFLATE = 1 << 20
MAX_GOT = 50_000
MAX_ENV_AGE_MS = 10 * 24 * 3600 * 1000   # 10 days: a long weekend or holiday with the office shut
MAX_ENV_AHEAD_MS = 15 * 60 * 1000
MAX_CLOCK_DRIFT_MS = 30 * 24 * 3600 * 1000


def b64u(data):
    """URL-safe base64 without padding."""
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def unb64u(s):
    """Decode URL-safe base64 without padding."""
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


class Ladder:
    """Opens sealed position envelopes, deduplicates (device, epoch, seq), and signs acks."""

    def __init__(self, key):
        raw = unb64u(key)
        if len(raw) not in (16, 24, 32):
            raise ValueError("invalid bridge key length")
        self._aes = AESGCM(raw)
        self.topic = "biruni" + hashlib.sha256(b"biruni-bridge-1:" + raw).hexdigest()[:40]
        from . import bridge

        sk_b64, _pin = bridge.identity(key)
        self._sk = serialization.load_der_private_key(unb64u(sk_b64), None)
        pub_raw = self._sk.public_key().public_bytes(
            serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint
        )
        self._pub = b64u(pub_raw)
        self._lock = threading.Lock()
        self._states = {}
        self._newest_t = {}
        self.last_info = None
        self.mirror_check = None

    def open(self, s):
        """Decrypt and optionally inflate a sealed bridge message; None on any failure."""
        if not isinstance(s, str) or not B64U_RE.match(s):
            return None
        try:
            raw = unb64u(s)
        except (ValueError, binascii.Error):
            return None
        if len(raw) < 29:
            return None
        try:
            pt = self._aes.decrypt(raw[:12], raw[12:], None)
        except Exception:  # noqa: BLE001 - InvalidTag or malformed ciphertext
            return None
        if not pt:
            return None
        flag, body = pt[0], pt[1:]
        if flag == 0:
            data = body
        elif flag == 1:
            try:
                dec = zlib.decompressobj(-15)
                data = dec.decompress(body, MAX_INFLATE + 1)
                if len(data) > MAX_INFLATE or dec.unconsumed_tail:
                    return None
                tail = dec.flush()
                if len(data) + len(tail) > MAX_INFLATE:
                    return None
                data += tail
            except zlib.error:
                return None
        else:
            return None
        try:
            obj = json.loads(data.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return None
        return obj if isinstance(obj, dict) else None

    def seal(self, obj):
        """Encrypt a JSON-serializable dict under the bridge key with flag byte 0."""
        pt = b"\x00" + json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        iv = secrets.token_bytes(12)
        return b64u(iv + self._aes.encrypt(iv, pt, None))

    def sign(self, text):
        """Sign `text` with this PC's P-256 identity key; returns (pub_b64u, raw_rs_b64u)."""
        der_sig = self._sk.sign(text.encode("utf-8"), ec.ECDSA(hashes.SHA256()))
        r, s = decode_dss_signature(der_sig)
        return self._pub, b64u(r.to_bytes(32, "big") + s.to_bytes(32, "big"))

    def take(self, s, via, clean, resolve, mirror_ok=None):
        """Process a sealed `lpos` envelope; returns (accepted_positions, ack_sealed_or_None)."""
        env = self.open(s)
        max_batch = 300 if via == "drive" else 60
        now_ms = int(time.time() * 1000)
        if not self._valid_env(env, max_batch, now_ms=now_ms):
            self.last_info = None
            if self._off_time_env(env, max_batch, now_ms=now_ms):
                ip, is_sig = self.sign(f"BRLT1|{now_ms}")
                ltime = {
                    "t": "ltime", "now": now_ms,
                    "n": b64u(secrets.token_bytes(9)), "ts": now_ms,
                    "ip": ip, "is": is_sig,
                }
                return [], self.seal(ltime)
            return [], None
        d, e, bl = env["d"], env["e"], env.get("bl", 0)
        env_ts = float(env["ts"])
        drift_ms = now_ms - env_ts if abs(now_ms - env_ts) <= MAX_CLOCK_DRIFT_MS else 0.0
        bl = max(0, bl) if type(bl) is int else 0
        lo = env.get("lo", 1)
        lo = lo if type(lo) is int and lo >= 1 else 1
        pairs = []
        for item in env["b"]:
            if isinstance(item, (list, tuple)) and len(item) == 2 and type(item[0]) is int and item[0] >= 1:
                if isinstance(item[1], dict):
                    pairs.append((item[0], item[1]))
        pairs.sort(key=lambda x: x[0])

        with self._lock:
            st = self._states.setdefault((d, e), {"upto": 0, "got": set()})
            st["upto"] = max(st["upto"], lo - 1)
            self._advance_upto(st)
            dups = 0
            accepted = []
            for seq, pos in pairs:
                if seq <= st["upto"] or seq in st["got"]:
                    dups += 1
                    continue
                self._mark_seen(st, seq)
                pos_in = pos
                raw_pt = pos.get("t")
                if isinstance(raw_pt, (int, float)) and not isinstance(raw_pt, bool) and math.isfinite(raw_pt):
                    if abs(drift_ms) > 60_000 and abs(env_ts - raw_pt) <= MAX_ENV_AGE_MS:
                        pos_in = dict(pos, t=int(round(raw_pt + drift_ms)))
                cleaned = clean(pos_in)
                if cleaned is None:
                    continue
                res = resolve(cleaned)
                if res is None:
                    continue
                res["seq"] = seq
                res["via"] = via
                if not res.get("route") and via in ROUTES:
                    res["route"] = via
                accepted.append(res)
            newest = self._newest_t.get(d)
            for res in accepted:
                pt = res.get("t") or 0
                if pt > 0 and (newest is None or pt > newest):
                    newest = pt
            if newest is not None:
                self._newest_t[d] = newest
            for res in accepted:
                pt = res.get("t") or 0
                if pt > 0 and newest is not None and pt < newest:
                    res["late"] = True
            upto = st["upto"]
            self.last_info = {"d": d, "backlog": bl, "dups": dups}

        if mirror_ok is None and callable(self.mirror_check):
            try:
                mirror_ok = bool(self.mirror_check(d, e, upto, accepted))
            except Exception:  # noqa: BLE001
                mirror_ok = False
        u2 = upto if mirror_ok else 0
        ip, is_sig = self.sign(f"BRLA1|{d}|{e}|{upto}")
        _, is2_sig = self.sign(f"BRLA2|{d}|{e}|{upto}|{u2}")
        ack = {
            "t": "lack", "d": d, "e": e, "upto": upto, "u2": u2,
            "n": b64u(secrets.token_bytes(9)), "ts": now_ms,
            "ip": ip, "is": is_sig, "is2": is2_sig,
        }
        return accepted, self.seal(ack)

    @staticmethod
    def _shape_ok(env, max_batch=60):
        if not env or env.get("t") != "lpos":
            return False
        if type(env.get("v")) is not int or env["v"] != 1:
            return False
        d = env.get("d")
        if not isinstance(d, str) or not DEV_RE.match(d):
            return False
        e = env.get("e")
        if not isinstance(e, str) or not (1 <= len(e) <= 64):
            return False
        b = env.get("b")
        if not isinstance(b, list) or len(b) > max_batch:
            return False
        ts = env.get("ts")
        if not isinstance(ts, (int, float)) or isinstance(ts, bool) or not math.isfinite(ts):
            return False
        return True

    @classmethod
    def _valid_env(cls, env, max_batch=60, now_ms=None):
        if not cls._shape_ok(env, max_batch):
            return False
        ts = env["ts"]
        if now_ms is None:
            now_ms = time.time() * 1000
        return (now_ms - MAX_ENV_AGE_MS) <= ts <= (now_ms + MAX_ENV_AHEAD_MS)

    @classmethod
    def _off_time_env(cls, env, max_batch=60, now_ms=None):
        if not cls._shape_ok(env, max_batch):
            return False
        ts = env["ts"]
        if now_ms is None:
            now_ms = time.time() * 1000
        return not ((now_ms - MAX_ENV_AGE_MS) <= ts <= (now_ms + MAX_ENV_AHEAD_MS))

    @staticmethod
    def _advance_upto(st):
        got = st["got"]
        while st["upto"] + 1 in got:
            got.remove(st["upto"] + 1)
            st["upto"] += 1

    @classmethod
    def _mark_seen(cls, st, seq):
        if seq == st["upto"] + 1:
            st["upto"] += 1
            cls._advance_upto(st)
            return
        got = st["got"]
        if len(got) >= MAX_GOT:
            got.remove(min(got))
        got.add(seq)


class TunnelLink:
    """Cloudflare quick tunnel to the local PhotoServer, downloading cloudflared if missing."""

    def __init__(self, port):
        from . import bridge, tunnel

        self.port = port
        self._events = queue.Queue()
        self._tunnel = None
        self._stopped = False
        tun = bridge.TUNNEL
        if tun and "{h}" not in tun:
            self._events.put(("url", "test"))
            return
        if not tun:
            return

        def start():
            exe = tunnel.find_exe()
            if exe is None:
                try:
                    exe = tunnel.download(cancelled=lambda: self._stopped)
                except RuntimeError as e:
                    self._events.put(("error", str(e)))
                    return
            if self._stopped:
                return
            try:
                self._tunnel = tunnel.Tunnel(exe, port)
            except OSError as e:
                self._events.put(("error", str(e)))

        threading.Thread(target=start, daemon=True).start()

    def poll(self):
        """Return ("url", label), ("error", text), or None."""
        if self._tunnel is not None:
            while not self._tunnel.events.empty():
                kind, val = self._tunnel.events.get_nowait()
                if kind == "url":
                    host = urllib.parse.urlsplit(val).netloc
                    self._events.put(("url", host.split(".")[0]))
                elif kind == "error":
                    self._events.put(("error", val))
                elif kind == "ended" and not self._stopped:
                    self._events.put(("error", f"ended ({val})"))
        try:
            return self._events.get_nowait()
        except queue.Empty:
            return None

    def stop(self):
        """Stop the tunnel process and cancel any download."""
        self._stopped = True
        if self._tunnel is not None:
            self._tunnel.stop()
            self._tunnel = None
