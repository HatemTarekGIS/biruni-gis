"""Organization cloud mailbox on Cloudflare R2 (no-loss P6): settings, tokens, signed descriptor,
signed receipts, and the PC pull intake (urllib only, QgsSettings imported lazily).

Secrets and tokens (`MB_SECRET`, `ut`, `ot`) are kept in memory or DPAPI-encrypted settings only
and never written to disk files, logs, or exception messages.
"""
import base64
import datetime as dt
import hashlib
import hmac
import json
import os
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from . import bridge, drive, ladder, recipients
from .tunnel import protect, unprotect

ON_KEY = "biruni/mailbox/on"
SECRET_KEY = "biruni/mailbox/secret"
DAYS_KEY = "biruni/mailbox/days"
SEQ_KEY = "biruni/mailbox/seq"

SEEN_NAME = "mailbox_seen.json"
STATE_NAME = "mailbox_state.json"
SIGN_PREFIX = "BRMB1|"
VALID_DAYS = (7, 14, 30)
INTERVAL = 120
TIMEOUT = 30
OBJ_KEY_RE = re.compile(r"^o/([0-9a-f]{16})/([A-Za-z0-9_-]{1,64})/(\d{6}-[0-9a-f]{12}\.bfp)$")


def _b64u(data):
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _unb64u(s):
    s = str(s)
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def _settings(settings=None):
    if settings is not None:
        return settings
    from qgis.core import QgsSettings

    return QgsSettings()


def enabled(settings=None):
    try:
        v = _settings(settings).value(ON_KEY, False)
        if isinstance(v, bool):
            return v
        return str(v or "").strip().lower() in ("1", "true", "yes", "on")
    except Exception:  # noqa: BLE001
        return False


def load_secret(settings=None):
    try:
        s = unprotect(_settings(settings).value(SECRET_KEY, "") or "")
        return s if len(s) == 43 else ""
    except Exception:  # noqa: BLE001
        return ""


def load_days(settings=None):
    try:
        d = int(_settings(settings).value(DAYS_KEY, 7) or 7)
        return d if d in VALID_DAYS else 7
    except Exception:  # noqa: BLE001
        return 7


def save_config(secret, days=7, settings=None, on=True):
    st = _settings(settings)
    sec = str(secret or "").strip()
    d = int(days) if days in VALID_DAYS else 7
    if sec:
        st.setValue(SECRET_KEY, protect(sec))
    st.setValue(DAYS_KEY, d)
    st.setValue(ON_KEY, bool(on and sec))


def set_enabled(b, settings=None):
    try:
        _settings(settings).setValue(ON_KEY, bool(b))
    except Exception:  # noqa: BLE001
        pass


def base_url(host):
    h = str(host or "").strip().lower()
    tpl = os.environ.get("BIRUNI_MB_BASE") or bridge.MB or "https://{h}"
    return tpl.replace("{h}", h).rstrip("/")


def tokens(secret, room):
    """Return (ut, ot) base64url HMAC-SHA256 tokens for `room` (16-hex `drive.room_tag(k)`)."""
    key_bytes = str(secret or "").encode("utf-8")
    r = str(room or "")
    ut = _b64u(hmac.new(key_bytes, ("up|" + r).encode("utf-8"), hashlib.sha256).digest())
    ot = _b64u(hmac.new(key_bytes, ("of|" + r).encode("utf-8"), hashlib.sha256).digest())
    return ut, ot


def probe(host):
    """GET `<base_url(host)>/v` -> `{"rel": int, "mb": bool}` or `None` on 404/network error."""
    h = str(host or "").strip().lower()
    if not h:
        return None
    url = base_url(h) + "/v"
    req = urllib.request.Request(url, headers={"User-Agent": "Biruni-GIS"}, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=min(TIMEOUT, 10)) as resp:
            if resp.status != 200:
                return None
            raw = resp.read()
        data = json.loads(raw.decode("utf-8")) if raw else {}
        if isinstance(data, dict) and "rel" in data:
            return {"rel": int(data.get("rel") or 0), "mb": bool(data.get("mb"))}
    except Exception:  # noqa: BLE001
        return None
    return None


_SEQ_CACHE = {}


def _kept_seq_map(settings=None):
    try:
        raw = _settings(settings).value(SEQ_KEY, "")
        if isinstance(raw, dict) and raw:
            return dict(raw)
        if raw:
            d = json.loads(str(raw))
            if isinstance(d, dict) and d:
                return d
    except Exception:  # noqa: BLE001
        pass
    return dict(_SEQ_CACHE) if settings is None else {}


def _save_seq_map(d, settings=None):
    if settings is None:
        _SEQ_CACHE.clear()
        _SEQ_CACHE.update(d)
    try:
        _settings(settings).setValue(SEQ_KEY, json.dumps(d, separators=(",", ":")))
    except Exception:  # noqa: BLE001
        pass


def descriptor(k, host, secret, days=7, settings=None):
    """Build and sign the BRMB1 mailbox descriptor `{"l", "ip", "is"}`, bumping `seq` only when `(h, ut, days)` changes."""
    room = str(k or "")[:12]
    tag = drive.room_tag(k)
    h = str(host or "").strip().lower()
    d = int(days) if days in VALID_DAYS else 7
    ut, _ot = tokens(secret, tag)
    tuple_sig = hashlib.sha256(f"{h}|{ut}|{d}".encode("utf-8")).hexdigest()[:24]

    smap = _kept_seq_map(settings)
    cur = smap.get(room) if isinstance(smap.get(room), dict) else {}
    old_seq = int(cur.get("seq") or 0) if isinstance(cur.get("seq"), int) else 0
    if old_seq > 0 and cur.get("sig") == tuple_sig:
        seq = old_seq
    else:
        seq = recipients.next_seq(old_seq)
        smap[room] = {"seq": seq, "sig": tuple_sig}
        _save_seq_map(smap, settings)

    payload = {
        "v": 1,
        "seq": seq,
        "room": room,
        "tag": tag,
        "h": h,
        "ut": ut,
        "days": d,
    }
    l_text = recipients.canonical(payload)
    ip, sig = recipients.sign_with(bridge.identity(k, settings=settings)[0], l_text, prefix=SIGN_PREFIX)
    return {"l": l_text, "ip": ip, "is": sig}


def verify_descriptor(mb, pin, room, tag=None, last_seq=0):
    """Verify a signed BRMB1 descriptor block (`{"l", "ip", "is"}`); returns the parsed dict or raises ValueError."""
    from ..vendor import ensure_vendor

    ensure_vendor()
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature

    if not isinstance(mb, dict) or not all(isinstance(mb.get(x), str) for x in ("l", "ip", "is")):
        raise ValueError("not a mailbox descriptor")
    if not pin or recipients.pin_of(mb["ip"]) != pin:
        raise ValueError("wrong signer")
    if len(mb["l"]) > 2000:
        raise ValueError("descriptor too long")
    try:
        raw, sig = _unb64u(mb["ip"]), _unb64u(mb["is"])
        if len(raw) != 65 or len(sig) != 64:
            raise ValueError
        pk = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), raw)
        der = encode_dss_signature(int.from_bytes(sig[:32], "big"), int.from_bytes(sig[32:], "big"))
        pk.verify(der, recipients.sign_text(mb["l"], prefix=SIGN_PREFIX).encode("utf-8"), ec.ECDSA(hashes.SHA256()))
    except (InvalidSignature, ValueError, TypeError) as e:
        raise ValueError("bad signature") from e
    try:
        obj = json.loads(mb["l"])
    except ValueError as e:
        raise ValueError("bad descriptor json") from e
    if not isinstance(obj, dict) or obj.get("v") != 1 or not isinstance(obj.get("seq"), int) or isinstance(obj.get("seq"), bool):
        raise ValueError("bad descriptor fields")
    if obj.get("room") != room:
        raise ValueError("another room")
    if tag is not None and obj.get("tag") != tag:
        raise ValueError("wrong tag")
    if not re.fullmatch(r"[0-9a-f]{16}", str(obj.get("tag") or "")):
        raise ValueError("bad tag")
    if obj["seq"] < last_seq:
        raise ValueError("rollback")
    h = obj.get("h")
    if not isinstance(h, str) or (h != "" and not bridge.ORG_RE.fullmatch(h)):
        raise ValueError("bad host")
    ut = obj.get("ut")
    if not isinstance(ut, str) or not re.fullmatch(r"[A-Za-z0-9_-]{43}", ut):
        raise ValueError("bad upload token")
    if obj.get("days") not in VALID_DAYS:
        raise ValueError("bad days")
    return obj


def sign_receipt(k, dev, items, rt=None):
    """Build and sign a BRCP1 receipt dict for device `dev` (nonce `mb.<dev>`)."""
    if rt is None:
        rt = int(time.time() * 1000)
    rt = int(rt)
    tag = drive.room_tag(k)
    dev_str = str(dev)
    nonce = "mb." + dev_str
    clean_items = []
    parts = []
    for it in items or ():
        if isinstance(it, (list, tuple)) and len(it) >= 3:
            iid, c, h8 = str(it[0]), int(it[1]), str(it[2] or "")
            clean_items.append([iid, c, h8])
            parts.append(f"{iid}:{c}:{h8}")
    text = f"BRCP1|{nonce}|{rt}|{','.join(parts)}"
    ip, sig = ladder.Ladder(k).sign(text)
    return {
        "v": 1,
        "room": tag,
        "dev": dev_str,
        "nonce": nonce,
        "rt": rt,
        "items": clean_items,
        "ip": ip,
        "is": sig,
    }


def load_state(cert_folder):
    if not cert_folder:
        return {}
    try:
        with open(os.path.join(cert_folder, STATE_NAME), encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def save_state(cert_folder, doc):
    if not cert_folder:
        return
    try:
        os.makedirs(cert_folder, exist_ok=True)
        path = os.path.join(cert_folder, STATE_NAME)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(doc, f, separators=(",", ":"))
        os.replace(tmp, path)
    except OSError:
        pass


class MailboxIntake:
    """Polls the organization's R2 mailbox over HTTPS with `ot`, saves new `.bfp` files (route `"r2"`),
    and uploads per-device signed receipts (`rc/<room>/<dev>.json`)."""

    def __init__(self, host, secret, store, handover, k, events, state_folder, interval=INTERVAL, surveyors=None, days=None):
        self.host = str(host or "").strip().lower()
        self.secret = str(secret or "").strip()
        self.store = store
        self.handover = handover
        self.k = k
        self.events = events
        self.state_folder = state_folder
        self.interval = interval
        self.surveyors = surveyors
        self.days = int(days) if days in VALID_DAYS else load_days()
        if getattr(self.store, "on_job", None) is None and self.events is not None:
            self.store.on_job = lambda jid: self.events.put(("job", jid))
        self.kid = drive.key_id(handover.public)
        self.room = drive.room_tag(k)
        self._ut, self._ot = tokens(self.secret, self.room)
        self.seen_path = os.path.join(store.folder, SEEN_NAME)
        self.objects, self.rc_hashes = self._load_seen()
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self.thread = None

    def _scrub(self, text):
        s = str(text or "")
        for secret_val in (self.secret, self._ut, self._ot):
            if secret_val:
                s = s.replace(secret_val, "***")
        return s

    def _load_seen(self):
        try:
            with open(self.seen_path, encoding="utf-8") as f:
                d = json.load(f)
            if (isinstance(d, dict) and d.get("room") == self.room and d.get("host") == self.host
                    and isinstance(d.get("objects"), dict)):
                rc = d.get("rc") if isinstance(d.get("rc"), dict) else {}
                return dict(d["objects"]), dict(rc)
        except (OSError, ValueError):
            pass
        return {}, {}

    def _keep_seen(self):
        tmp = self.seen_path + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({
                    "room": self.room,
                    "host": self.host,
                    "objects": dict(self.objects),
                    "rc": dict(self.rc_hashes),
                }, f, separators=(",", ":"))
            os.replace(tmp, self.seen_path)
        except OSError:
            pass

    def _req(self, method, path, body=None, content_type=None):
        url = base_url(self.host) + path
        headers = {"Authorization": f"Bearer {self._ot}", "User-Agent": "Biruni-GIS"}
        if content_type:
            headers["Content-Type"] = content_type
        if body is not None:
            headers["Content-Length"] = str(len(body))
        req = urllib.request.Request(url, data=body, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
                return resp.status, resp.read()
        except urllib.error.HTTPError as e:
            raise RuntimeError(self._scrub(f"HTTP {e.code}")) from None
        except (urllib.error.URLError, OSError, TimeoutError) as e:
            reason = getattr(e, "reason", e)
            raise RuntimeError(self._scrub(str(reason))) from None

    def _list_all(self):
        out = []
        cursor = ""
        for _ in range(100):
            q = f"?room={self.room}" + (f"&cursor={urllib.parse.quote(cursor)}" if cursor else "")
            _st, raw = self._req("GET", "/mb/list" + q)
            data = json.loads(raw.decode("utf-8")) if raw else {}
            objs = data.get("objects") if isinstance(data, dict) else None
            if isinstance(objs, list):
                for o in objs:
                    if isinstance(o, dict) and isinstance(o.get("key"), str):
                        out.append(o)
            if not isinstance(data, dict) or not data.get("more") or not data.get("cursor"):
                break
            cursor = str(data["cursor"])
        return out

    def scan(self):
        """One pull pass: list objects, download and open unseen `.bfp` files, update per-device receipts, write state."""
        from .server import BlockedDeviceError, read_surveyor_keys

        now = time.time()
        kas = drive.auth_keys(
            self.k, self.surveyors() if self.surveyors is not None else read_surveyor_keys(self.store.folder)
        )
        out = {"seen": 0, "new": 0, "refused": []}
        try:
            listed = self._list_all()
        except Exception as e:  # noqa: BLE001
            err_msg = self._scrub(str(e))
            prev = load_state(self.state_folder)
            state_doc = {
                "at": now,
                "ok": False,
                "err": err_msg,
                "rel": prev.get("rel", 2),
                "mb": prev.get("mb", True),
                "bytes": int(prev.get("bytes") or 0),
                "count": int(prev.get("count") or 0),
                "month_puts": int(prev.get("month_puts") or 0),
                "new": 0,
                "refused": int(prev.get("refused") or 0),
                "days": self.days,
                "host": self.host,
            }
            save_state(self.state_folder, state_doc)
            if self.events is not None:
                self.events.put(("mailbox", dict(state_doc, error=err_msg)))
            return dict(out, error=err_msg)

        changed = False
        total_bytes = 0
        month_puts = 0
        now_dt = dt.datetime.fromtimestamp(now, dt.timezone.utc)
        month_start_ms = int(dt.datetime(now_dt.year, now_dt.month, 1, tzinfo=dt.timezone.utc).timestamp() * 1000)

        for obj in listed:
            if self._stop.is_set():
                break
            key = obj.get("key") or ""
            m = OBJ_KEY_RE.match(key)
            if not m or m.group(1) != self.room:
                continue
            dev, name = m.group(2), m.group(3)
            size = int(obj.get("size") or 0)
            up = int(obj.get("up") or 0)
            total_bytes += max(0, size)
            if up >= month_start_ms:
                month_puts += 1
            out["seen"] += 1
            stamp = [size, up]
            with self._lock:
                cur_entry = self.objects.get(key)
            if isinstance(cur_entry, list) and cur_entry[:2] == stamp:
                continue
            try:
                _st, blob = self._req("GET", "/mb/" + key)
            except Exception:  # noqa: BLE001 - transient fetch error: try on next pass
                continue
            ok = 1
            item_id = ""
            try:
                rec, new = drive.take_blob(
                    blob, dev, name, kas, self.kid, self.room, self.handover, self.store, route="r2"
                )
                item_id = str((rec or {}).get("id") or "")
            except BlockedDeviceError as b:
                if self.events is not None:
                    self.events.put(("rejected", {"device_id": b.device_id, "reason": b.reason}))
                out["refused"].append((key, b.reason))
                ok = 0
            except (drive.BfpError, ValueError, TypeError, KeyError) as x:
                out["refused"].append((key, self._scrub(str(x) or type(x).__name__)))
                ok = 0
            except OSError:
                continue
            else:
                if new:
                    out["new"] += 1
                    if self.events is not None:
                        self.events.put(("photo", rec))
            with self._lock:
                self.objects[key] = [size, up, ok, item_id]
            changed = True

        # Per device: gather newest 500 accepted items, check copy_status(ids) with c >= 1, PUT receipt if changed
        by_dev = {}
        with self._lock:
            obj_snapshot = list(self.objects.items())
        for key, entry in obj_snapshot:
            m = OBJ_KEY_RE.match(key)
            if not m or not isinstance(entry, list) or len(entry) < 4:
                continue
            dev = m.group(2)
            up_ms = int(entry[1] or 0)
            ok_flag = int(entry[2] or 0)
            iid = str(entry[3] or "")
            if ok_flag == 1 and iid:
                by_dev.setdefault(dev, []).append((up_ms, key, iid))

        for dev, entries in by_dev.items():
            entries.sort(key=lambda t: (t[0], t[1]), reverse=True)
            ids = []
            seen_ids = set()
            for _up, _key, iid in entries:
                if iid not in seen_ids:
                    seen_ids.add(iid)
                    ids.append(iid)
                    if len(ids) >= 500:
                        break
            ids.reverse()
            rows = [r for r in self.store.copy_status(ids) if int(r[1] or 0) >= 1]
            if not rows:
                continue
            items_canon = json.dumps(rows, separators=(",", ":"))
            items_hash = hashlib.sha256(items_canon.encode("utf-8")).hexdigest()[:24]
            with self._lock:
                prev_hash = self.rc_hashes.get(dev)
            if prev_hash == items_hash:
                continue
            rc_doc = sign_receipt(self.k, dev, rows)
            rc_bytes = json.dumps(rc_doc, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            try:
                self._req("PUT", f"/mb/rc/{self.room}/{dev}.json", body=rc_bytes, content_type="application/json")
            except Exception:  # noqa: BLE001
                continue
            with self._lock:
                self.rc_hashes[dev] = items_hash
            changed = True

        if changed:
            with self._lock:
                self._keep_seen()

        with self._lock:
            total_refused = sum(1 for v in self.objects.values() if isinstance(v, list) and len(v) >= 3 and v[2] == 0)

        state_doc = {
            "at": now,
            "ok": True,
            "err": "",
            "rel": 2,
            "mb": True,
            "bytes": total_bytes,
            "count": len(listed),
            "month_puts": month_puts,
            "new": out["new"],
            "refused": total_refused,
            "days": self.days,
            "host": self.host,
        }
        save_state(self.state_folder, state_doc)
        if self.events is not None:
            self.events.put(("mailbox", dict(state_doc, refused_items=out["refused"])))
        return out

    def retry_refused(self):
        with self._lock:
            self.objects = {k: v for k, v in self.objects.items() if isinstance(v, list) and len(v) >= 3 and v[2] == 1}
            self._keep_seen()

    def start(self):
        if self.thread is None:
            self._stop.clear()
            self.thread = threading.Thread(target=self._run, name="biruni-mailbox-intake", daemon=True)
            self.thread.start()

    def _run(self):
        while not self._stop.is_set():
            try:
                self.scan()
            except Exception as x:  # noqa: BLE001
                if self.events is not None:
                    self.events.put(("mailbox", {"error": self._scrub(str(x)), "at": time.time()}))
            self._stop.wait(self.interval)

    def stop(self):
        self._stop.set()
        if self.thread is not None:
            self.thread.join(timeout=5)
            self.thread = None
