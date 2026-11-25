"""Field photos through the surveyor's own Google Drive: one .bfp file per photo.

The phone page uploads each photo, sealed for this computer, into the user's own Drive; Drive for desktop syncs
the folder to this PC, and the intake (DriveIntake, below) opens each file and saves it like a photo that came
over the bridge. Google sees only sealed bytes.

A .bfp file:  "BFP1" | header length (2 bytes, big-endian) | header (JSON, UTF-8) | signature (64) | body

  body       the sealed photo exactly as the bridge carries it (handover.seal: ECDH P-256 to this computer's
             handover key, then AES-256-GCM), so it opens with HandoverKey.open. Since no-loss P3 it may be an
             envelope v2 (envelope.py) sealed for several computers; the header still names the office (kid, v:1),
             and a backup PC whose kid is among the envelope's recipients accepts it too (verify).
  header     {"v": 1, "room": tag, "dev": device id, "kid": handover key id, "id": photo id, "n": number on this
             phone, "t": ms, "size": body length, "sha": SHA-256 of the body (hex), "pub": the phone's ECDSA P-256
             public key (base64url, 65 bytes), "cert": base64url HMAC-SHA256(Ka, "BFP-CERT1|" + pub + "|" + dev)}
  signature  ECDSA P-256 / SHA-256 by that phone key over everything before it (raw r|s, as WebCrypto makes it).

  Ka = SHA-256("biruni-drive-auth-1:" + k + sk): k is the office's bridge key, sk the surveyor's own key ("" with
  the shared QR). Only a phone that scanned this office's code can make a cert, so a file dropped into a shared
  folder by anyone else is refused; the matching sk then goes through PhotoStore.save's surveyor rules (revoked,
  per-surveyor mode) like any other photo.

Drive folder: biruni-photos-<tag>/p-<dev>/<n, 6 digits>-<sha, 12>.bfp, tag = hex SHA-256("biruni-drive-room-1:" + k)
first 16 characters (it names the folder without revealing the key).
"""
import base64
import hashlib
import hmac
import json
import os
import re
import threading
import time

MAGIC = b"BFP1"
MAX_HEADER = 4096
MAX_BODY = 40 * 1024 * 1024 + 4096      # generous: the phone keeps photos at 5 MB (fitPhoto); a seal adds ~100 bytes
SIG = 64
NAME_RE = re.compile(r"^(\d{6})-([0-9a-f]{12})\.bfp$")
DEV_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
FIELDS = ("v", "room", "dev", "kid", "id", "n", "t", "size", "sha", "pub", "cert")


class BfpError(ValueError):
    """Not a .bfp file this computer accepts (the message says why, in English, for the log)."""


def _b64u(data):
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _unb64u(s):
    s = str(s)
    return base64.urlsafe_b64decode(s + "==="[: (len(s) + 3) % 4])


def room_tag(k):
    return hashlib.sha256(("biruni-drive-room-1:" + k).encode("utf-8")).hexdigest()[:16]


def folder_name(k):
    return "biruni-photos-" + room_tag(k)


def file_name(n, sha):
    return f"{int(n):06d}-{sha[:12]}.bfp"


def key_id(pk):
    """Which computer a file is sealed for: from the handover public key the QR code carries (&pk=)."""
    return hashlib.sha256(("biruni-drive-kid-1:" + pk).encode("utf-8")).hexdigest()[:16]


def auth_key(k, sk=""):
    return hashlib.sha256(("biruni-drive-auth-1:" + k + (sk or "")).encode("utf-8")).digest()


def cert(ka, pub, dev):
    return _b64u(hmac.new(ka, ("BFP-CERT1|" + pub + "|" + dev).encode("utf-8"), hashlib.sha256).digest())


def auth_keys(k, surveyor_keys):
    """{sk: Ka} for the shared code ("") and every surveyor's own code (revoked too: PhotoStore.save says why it
    refuses them). surveyor_keys: what server.read_surveyor_keys returns."""
    out = {"": auth_key(k)}
    for s in ((surveyor_keys or {}).get("surveyors") or {}).values():
        sec = str((s or {}).get("secret") or "")
        if sec:
            out[sec] = auth_key(k, sec)
    return out


def pack(header, body, sign_key):
    """(file bytes, file name). header: the fields above except v/size/sha (filled in here); sign_key: a
    cryptography EllipticCurvePrivateKey (the phone's key; the page does the same in BFP_JS)."""
    from ..vendor import ensure_vendor

    ensure_vendor()
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature

    body = bytes(body)
    h = dict(header, v=1, size=len(body), sha=hashlib.sha256(body).hexdigest())
    hb = json.dumps(h, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if len(hb) > MAX_HEADER:
        raise BfpError("header too long")
    pre = MAGIC + len(hb).to_bytes(2, "big") + hb
    r, s = decode_dss_signature(sign_key.sign(pre, ec.ECDSA(hashes.SHA256())))
    return pre + r.to_bytes(32, "big") + s.to_bytes(32, "big") + body, file_name(h["n"], h["sha"])


def parse(blob):
    """(header, the signed bytes, signature, body) - the shape only, nothing is trusted yet."""
    blob = bytes(blob)
    if len(blob) < 6 or blob[:4] != MAGIC:
        raise BfpError("not a .bfp file")
    hlen = int.from_bytes(blob[4:6], "big")
    if hlen < 2 or hlen > MAX_HEADER or len(blob) < 6 + hlen + SIG:
        raise BfpError("damaged header")
    try:
        h = json.loads(blob[6:6 + hlen].decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as e:
        raise BfpError("damaged header") from e
    if not isinstance(h, dict) or h.get("v") != 1 or any(f not in h for f in FIELDS):
        raise BfpError("unknown header")
    for f in ("room", "dev", "kid", "id", "sha", "pub", "cert"):
        if not isinstance(h[f], str):
            raise BfpError("unknown header")
    for f in ("n", "t", "size"):
        if not isinstance(h[f], int) or isinstance(h[f], bool) or h[f] < 0:
            raise BfpError("unknown header")
    if not DEV_RE.match(h["dev"]) or not re.fullmatch(r"[0-9a-f]{64}", h["sha"]) or h["n"] > 999999:
        raise BfpError("unknown header")
    body = blob[6 + hlen + SIG:]
    if len(body) > MAX_BODY:
        raise BfpError("too large")
    return h, blob[:6 + hlen], blob[6 + hlen:6 + hlen + SIG], body


def verify(blob, kas, kid, room=None):
    """A .bfp file -> (header, the sealed body, the surveyor key sk it was certified with).
    kas: auth_keys(...); kid: key_id of this computer's handover key; room: room_tag(k) (checked when given).
    BfpError when it is damaged, made by a phone that never scanned this office's code, or sealed for another
    computer."""
    from ..vendor import ensure_vendor

    ensure_vendor()
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature

    h, signed, sig, body = parse(blob)
    if room is not None and h["room"] != room:
        raise BfpError("another office")
    if not hmac.compare_digest(h["kid"], kid):
        # the header names the office (its "v" stays 1); a v2 body may also be sealed for this computer (a backup PC)
        from . import envelope

        if body[:1] != b"\x02" or kid not in envelope.kids(body):
            raise BfpError("sealed for another computer")
    if h["size"] != len(body) or not hmac.compare_digest(hashlib.sha256(body).hexdigest(), h["sha"]):
        raise BfpError("damaged body")
    try:
        raw = _unb64u(h["pub"])
        if len(raw) != 65:
            raise ValueError
        pk = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), raw)
        der = encode_dss_signature(int.from_bytes(sig[:32], "big"), int.from_bytes(sig[32:], "big"))
        pk.verify(der, signed, ec.ECDSA(hashes.SHA256()))
    except (InvalidSignature, ValueError, TypeError) as e:
        raise BfpError("bad signature") from e
    for sk, ka in kas.items():
        if hmac.compare_digest(cert(ka, h["pub"], h["dev"]), h["cert"]):
            return h, body, sk
    raise BfpError("not from this office's code")


def take_blob(blob, dev, name, kas, kid, room, handover, store, route="drive"):
    """Verify and open a .bfp blob and save it into `store` with the given `route`."""
    m = NAME_RE.match(str(name or ""))
    if not m:
        raise BfpError("file name does not match its header")
    h, body, sk = verify(blob, kas, kid, room)
    if h["dev"] != dev or m.group(2) != h["sha"][:12]:
        raise BfpError("file name does not match its header")
    meta, data = handover.open(body)
    if str(meta.get("id") or "") != h["id"]:
        raise BfpError("photo id does not match its header")
    meta = dict(meta, sk=sk, route=route)   # the cert decides the surveyor, never the sealed data
    meta.pop("preview", None)
    return store.save(meta, data, ssha=h["sha"])


# ---- the intake: Drive for desktop's folder -> PhotoStore ------------------------------------------------------------
SEEN_NAME = "drive_seen.json"    # in the photo folder: files already taken in (or refused), so they are read once
INTERVAL = 15                    # seconds; Drive for desktop's streaming mode sends no change notices to rely on


def named_folders(roots, name):
    """The Drive folders named ``name`` under the synced roots the user chose (e.g. G:\\My Drive): the folder itself,
    root/<name>, and one level deeper (a folder kept inside another, a shared drive)."""
    out = []
    for root in roots or ():
        root = str(root or "")
        if not root or not os.path.isdir(root):
            continue
        cands = [root, os.path.join(root, name)]
        try:
            cands += [os.path.join(e.path, name) for e in os.scandir(root) if e.is_dir() and e.name != name]
        except OSError:
            pass
        for c in cands:
            if os.path.basename(os.path.normpath(c)) == name and os.path.isdir(c):
                c = os.path.normcase(os.path.abspath(c))
                if c not in out:
                    out.append(c)
    return out


def ensure_named_folder(roots, name):
    """(path, created): the Drive folder named ``name``, made in the first synced root that takes it when none is
    found. A root that refuses (G:\\Shared drives) is skipped. (None, False) when no root can hold it. The path is
    written like named_folders writes it, so a later look finds the same one."""
    found = named_folders(roots, name)
    if found:
        return found[0], False
    for root in roots or ():
        root = str(root or "")
        if not root or not os.path.isdir(root):
            continue
        path = os.path.join(root, name)
        try:
            os.mkdir(path)
        except OSError:
            continue
        return os.path.normcase(os.path.abspath(path)), True
    return None, False


def office_folders(roots, k):
    """The office's Drive folders under the synced roots the user chose (e.g. G:\\My Drive): the folder itself,
    root/<folder>, and one level deeper (a folder kept inside another, a shared drive)."""
    return named_folders(roots, folder_name(k))


def ensure_office_folder(roots, k):
    """(path, created): the office's Drive folder, made in the first synced root that takes it when none is found.
    The office owns this folder and shares it with each surveyor. A root that refuses (G:\\Shared drives) is
    skipped. (None, False) when no root can hold it. The path is written like office_folders writes it, so a
    later look finds the same one."""
    return ensure_named_folder(roots, folder_name(k))


def _volumes():
    """[(root, label)] of this PC's fixed drives (Drive for desktop shows as one, labelled "Google Drive")."""
    if os.name != "nt":
        return []
    import ctypes

    k32, out = ctypes.windll.kernel32, []
    mask = k32.GetLogicalDrives()
    for i in range(26):
        if not mask & (1 << i):
            continue
        root = chr(65 + i) + ":\\"
        if k32.GetDriveTypeW(root) != 3:
            continue
        buf = ctypes.create_unicode_buffer(261)
        if k32.GetVolumeInformationW(root, buf, 261, None, None, None, None, 0):
            out.append((root, buf.value))
    return out


def suggest_roots(volumes=None, home=None):
    """Folders Google Drive for desktop syncs on this PC, to offer when Drive is turned on: the folders at the top
    of a drive labelled "Google Drive" (its "My Drive" has a name in the user's language), then the mirror folders
    in the user's home. Only suggestions: the user sees them and can remove them."""
    out = []
    for root, label in (_volumes() if volumes is None else volumes):
        if "google drive" not in str(label or "").lower():
            continue
        try:
            out += [e.path for e in os.scandir(root) if e.is_dir() and not e.name.startswith((".", "$"))]
        except OSError:
            pass
    home = os.path.expanduser("~") if home is None else home
    out += [p for p in (os.path.join(home, "My Drive"), os.path.join(home, "Google Drive")) if os.path.isdir(p)]
    return list(dict.fromkeys(out))


def forget_refused(folder):
    """retry_refused for when the intake is not running: the refused files in ``folder``'s list are read again."""
    path = os.path.join(folder, SEEN_NAME)
    try:
        with open(path, encoding="utf-8") as f:
            d = json.load(f)
        files = d["files"]
        d["files"] = {key: v for key, v in files.items() if (v or [0, 0, 0])[2:] == [1]}
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return
    tmp = path + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(d, f, separators=(",", ":"))
        os.replace(tmp, path)
    except OSError:
        pass


class DriveIntake:
    """Every INTERVAL seconds, opens the new .bfp files in the office's Drive folders and saves each photo with the
    same PhotoStore the phone server uses (route "drive"). Events go to ``events`` like the server's own:
    ("photo", record), ("rejected", {"device_id", "reason"}), and ("drive", {"seen", "new", "refused", "at"}).

    A file is read again only when its size or time changes - a file Drive for desktop has not finished bringing
    down is refused as damaged now and taken in when it is whole. A full disk is not remembered: tried again."""

    def __init__(self, roots, store, handover, k, events, interval=INTERVAL, surveyors=None):
        # surveyors: a callable giving the surveyor codes {"qr_mode", "surveyors"}; the office reads its own list from
        # the photo folder, a backup PC (no-loss P9) passes the office pack's list instead
        self.surveyors = surveyors
        self.roots = list(roots or ())
        self.store, self.handover, self.k, self.events = store, handover, k, events
        if getattr(self.store, "on_job", None) is None and self.events is not None:
            self.store.on_job = lambda jid: self.events.put(("job", jid))
        self.interval = interval
        self.kid, self.room = key_id(handover.public), room_tag(k)
        self.path = os.path.join(store.folder, SEEN_NAME)
        self.seen = self._load()
        self._stop = threading.Event()
        self._lock = threading.Lock()     # self.seen and its file: the scan thread and the dock (retry_refused)
        self.thread = None

    def _load(self):
        try:
            with open(self.path, encoding="utf-8") as f:
                d = json.load(f)
            if isinstance(d, dict) and d.get("room") == self.room and isinstance(d.get("files"), dict):
                return d["files"]
        except (OSError, ValueError):
            pass
        return {}

    def _keep(self):
        tmp = self.path + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({"room": self.room, "files": dict(self.seen)}, f, separators=(",", ":"))
            os.replace(tmp, self.path)
        except OSError:
            pass

    def scan(self):
        """One pass; returns {"seen": files looked at, "new": photos saved, "refused": [(file, why)]}."""
        from .server import BlockedDeviceError, read_surveyor_keys

        kas = auth_keys(self.k, self.surveyors() if self.surveyors is not None else read_surveyor_keys(self.store.folder))
        tops = office_folders(list(self.roots), self.k)
        out = {"seen": 0, "new": 0, "refused": [], "folders": len(tops)}
        changed = False
        for top in tops:
            try:
                devs = [e for e in os.scandir(top) if e.is_dir() and e.name.startswith("p-")]
            except OSError:
                continue
            for d in devs:
                try:
                    files = [e for e in os.scandir(d.path) if e.is_file() and NAME_RE.match(e.name)]
                except OSError:
                    continue
                for e in files:
                    if self._stop.is_set():
                        break
                    out["seen"] += 1
                    try:
                        st = e.stat()
                    except OSError:
                        continue
                    key = d.name + "/" + e.name
                    stamp = [st.st_size, st.st_mtime_ns]
                    if (self.seen.get(key) or [])[:2] == stamp:
                        continue
                    ok = 1
                    try:
                        rec, new = self._take(e.path, d.name[2:], e.name, st.st_size, kas)
                    except BlockedDeviceError as b:
                        self.events.put(("rejected", {"device_id": b.device_id, "reason": b.reason}))
                        out["refused"].append((key, b.reason))
                        ok = 0
                    except (BfpError, ValueError, TypeError, KeyError) as x:
                        out["refused"].append((key, str(x) or type(x).__name__))
                        ok = 0
                    except OSError:
                        continue          # disk full, the folder gone, the file still downloading: next pass
                    else:
                        if new:
                            out["new"] += 1
                            self.events.put(("photo", rec))
                    with self._lock:
                        self.seen[key] = stamp + [ok]
                    changed = True
        if changed:
            with self._lock:
                self._keep()
        self.events.put(("drive", dict(out, at=time.time())))
        return out

    def _take(self, path, dev, name, size, kas):
        if size > 6 + MAX_HEADER + SIG + MAX_BODY:
            raise BfpError("too large")
        with open(path, "rb") as f:
            blob = f.read()
        return take_blob(blob, dev, name, kas, self.kid, self.room, self.handover, self.store, route="drive")

    def retry_refused(self):
        """Read the refused files again on the next pass (after a surveyor or phone is let back in)."""
        with self._lock:
            self.seen = {key: v for key, v in self.seen.items() if (v or [0, 0, 0])[2:] == [1]}
            self._keep()

    def start(self):
        if self.thread is None:
            self._stop.clear()
            self.thread = threading.Thread(target=self._run, name="biruni-drive-intake", daemon=True)
            self.thread.start()

    def _run(self):
        while not self._stop.is_set():
            try:
                self.scan()
            except Exception as x:  # noqa: BLE001 - the intake keeps going; the dock shows the error
                self.events.put(("drive", {"error": str(x), "at": time.time()}))
            self._stop.wait(self.interval)

    def stop(self):
        self._stop.set()
        if self.thread is not None:
            self.thread.join(timeout=5)
            self.thread = None


BGP_MAGIC = b"BGP1\n"
BGP_RE = re.compile(r"^\d{9}-\d{9}-[\w-]{6}\.bgp$")
BGP_MAX_BYTES = 120 * 1024
BGP_MAX_AGE = 10 * 24 * 3600   # same as ladder.MAX_ENV_AGE_MS


class PosIntake:
    """Every interval seconds, reads new .bgp position files in office_folders(roots, k)/g-*/
    and delivers each sealed lpos envelope to take(sealed, "drive") (PhotoServer.take_env)."""

    def __init__(self, roots, k, take, interval=10):
        self.roots = list(roots or ())
        self.k = k
        self.take = take
        self.interval = interval
        self.seen = {}
        self._stop = threading.Event()
        self.thread = None

    def scan(self):
        """One scan pass across g-* subfolders; returns count of newly opened files."""
        now = time.time()
        opened = 0
        for top in office_folders(list(self.roots), self.k):
            try:
                devs = sorted(
                    (e for e in os.scandir(top) if e.is_dir() and e.name.startswith("g-")),
                    key=lambda e: e.name,
                )
            except OSError:
                continue
            for d in devs:
                try:
                    files = sorted(
                        (e for e in os.scandir(d.path) if e.is_file() and BGP_RE.match(e.name)),
                        key=lambda e: e.name,
                    )
                except OSError:
                    continue
                for e in files:
                    if self._stop.is_set():
                        return opened
                    try:
                        st = e.stat()
                    except OSError:
                        continue
                    if st.st_size > BGP_MAX_BYTES or (now - st.st_mtime) > BGP_MAX_AGE:
                        continue
                    key = d.name + "/" + e.name
                    stamp = [st.st_size, st.st_mtime_ns]
                    if self.seen.get(key) == stamp:
                        continue
                    try:
                        with open(e.path, "rb") as f:
                            raw = f.read(BGP_MAX_BYTES + 1)
                    except OSError:
                        continue
                    if len(raw) > BGP_MAX_BYTES or not raw.startswith(BGP_MAGIC):
                        continue
                    try:
                        sealed = raw[len(BGP_MAGIC):].decode("ascii").strip()
                    except UnicodeDecodeError:
                        continue
                    if self.take(sealed, "drive"):
                        self.seen[key] = stamp
                        opened += 1
        return opened

    def start(self):
        if self.thread is None:
            self._stop.clear()
            self.thread = threading.Thread(target=self._run, name="biruni-pos-intake", daemon=True)
            self.thread.start()

    def _run(self):
        while not self._stop.is_set():
            try:
                self.scan()
            except Exception:  # noqa: BLE001 - keep scanning on transient Drive errors
                pass
            self._stop.wait(self.interval)

    def stop(self):
        self._stop.set()
        if self.thread is not None:
            self.thread.join(timeout=5)
            self.thread = None


# ---- the same on the phone page (field_photos/drive_page.py puts it in /p/) --------------------------------------
# needs enc, hex, b64u and cat from bridge.CORE_JS
BFP_JS = r"""
/* .bfp: one photo sealed for the PC, in the user's own Drive (field_photos/drive.py says the format) */
const BFP={
 sha:async b=>hex(await crypto.subtle.digest('SHA-256',b)),
 tag:async k=>(await BFP.sha(enc.encode('biruni-drive-room-1:'+k))).slice(0,16),
 kid:async pk=>(await BFP.sha(enc.encode('biruni-drive-kid-1:'+pk))).slice(0,16),
 cert:async(k,sk,pub,dev)=>{const ka=await crypto.subtle.digest('SHA-256',enc.encode('biruni-drive-auth-1:'+k+(sk||'')));
  const key=await crypto.subtle.importKey('raw',ka,{name:'HMAC',hash:'SHA-256'},false,['sign']);
  return b64u.enc(await crypto.subtle.sign('HMAC',key,enc.encode('BFP-CERT1|'+pub+'|'+dev)))},
 /* h: {room,dev,kid,id,n,t,pub,cert}; body: the sealed photo (Uint8Array); key: the phone's ECDSA P-256 key */
 pack:async(h,body,key)=>{h=Object.assign({},h,{v:1,size:body.length,sha:await BFP.sha(body)});
  const hb=enc.encode(JSON.stringify(h));if(hb.length>4096)throw new Error('bfp header');
  const pre=cat(enc.encode('BFP1'),new Uint8Array([hb.length>>8,hb.length&255]),hb);
  const sig=new Uint8Array(await crypto.subtle.sign({name:'ECDSA',hash:'SHA-256'},key,pre));
  return {bytes:cat(pre,sig,body),head:cat(pre,sig),name:String(h.n).padStart(6,'0')+'-'+h.sha.slice(0,12)+'.bfp',sha:h.sha}}};
"""
