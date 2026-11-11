"""Locked databases (*.bgpkg): a GeoPackage cut into one encrypted piece per layer, with users, their
rights and their keys in a manifest signed by the administrator.

File: MAGIC, then records [kind 1 byte][length 8 bytes][payload]:
  M  the manifest (JSON)              S  the administrator's Ed25519 signature of the manifest
  B  an encrypted piece (id + AES-256-GCM ciphertext; its SHA-256 is in the signed manifest)
  C  a user's change set (appended; encrypted with the layer's key, signed by the user)
  A  an activity-log entry (appended; encrypted to the administrator only, signed by its author)
Appended records are chained (each one carries the SHA-256 of the one before it), so a record removed
from the middle is noticed; records cut from the end cannot be noticed offline.

Keys: every layer has its own key, and every protected field (a field hidden from at least one user) has
its own key too, with its values kept in a separate piece. A user's password gives (Argon2id) the key that
opens his own record: his signing key and his user key; the user key opens his grants: the keys of the
layers and fields he may see, and no other. The administrator's record holds the master key, which opens
every key; the recovery key opens the same administrator record."""
import copy
import datetime as dt
import json
import os
import time
import uuid

from . import crypto
from . import gpkg

MAGIC = b"BIRUNI-LOCKED\x00\x01\x00"
SIG_PREFIX = b"BIRUNI-LOCKED-MANIFEST\x00"
ENV_PREFIX = b"BIRUNI-LOCKED-RECORD\x00"
EXT = ".bgpkg"
RIGHTS = "vaed"  # view, add, edit, delete


class LockError(Exception):
    """code: not_locked, damaged, tampered, wrong_password, disabled, expired, bad_recovery, other_family,
    changed_elsewhere, name_taken."""

    def __init__(self, code, detail=""):
        super().__init__(code if not detail else f"{code}: {detail}")
        self.code = code
        self.detail = detail


def norm_rights(r):
    r = set(r or "")
    if r & set("aed"):
        r.add("v")  # adding, editing or deleting implies seeing
    return "".join(ch for ch in RIGHTS if ch in r)


def _now():
    return dt.datetime.now().replace(microsecond=0).isoformat()


def _id(prefix):
    return prefix + uuid.uuid4().hex[:10]


# ---- records ----------------------------------------------------------------------------------------------------
def read_records(path):
    with open(path, "rb") as f:
        data = f.read()
    if not data.startswith(MAGIC):
        raise LockError("not_locked")
    view = memoryview(data)
    i, out = len(MAGIC), []
    while i < len(data):
        if i + 9 > len(data):
            raise LockError("damaged", "cut record header")
        kind = bytes(view[i:i + 1])
        n = int.from_bytes(view[i + 1:i + 9], "big")
        if i + 9 + n > len(data):
            raise LockError("damaged", "cut record")
        out.append((kind, view[i + 9:i + 9 + n]))
        i += 9 + n
    return out


def _record(kind, payload):
    return kind + len(payload).to_bytes(8, "big")


def write_records(path, records):
    """Writes a whole file next to the target, then replaces it (a crash never leaves half a file)."""
    tmp = f"{path}.{uuid.uuid4().hex[:6]}.part"
    with open(tmp, "wb") as f:
        f.write(MAGIC)
        for kind, payload in records:
            f.write(_record(kind, payload))
            f.write(payload)
    os.replace(tmp, path)


def tail_hash(path):
    """The hash the next appended record must point to: the last appended record's, or the manifest's."""
    head, last = b"", None
    with open(path, "rb") as f:
        if f.read(len(MAGIC)) != MAGIC:
            raise LockError("not_locked")
        while True:
            h = f.read(9)
            if len(h) < 9:
                break
            kind, n = h[:1], int.from_bytes(h[1:], "big")
            if kind in (b"M", b"S"):
                head += f.read(n)
            elif kind in (b"C", b"A"):
                last = f.read(n)
            else:
                f.seek(n, 1)
    return crypto.sha256(last if last is not None else head)


def append_record(path, kind, payload):
    with open(path, "ab") as f:
        f.write(_record(kind, payload))
        f.write(payload)


def blob_record(bid, ct):
    b = bid.encode("ascii")
    return len(b).to_bytes(2, "big") + b + bytes(ct)


def lookup(family, name):
    return crypto.sha256(f"biruni-user|{family}|{str(name).strip().casefold()}".encode("utf-8"))[:32]


def _aad(family, *parts):
    return "|".join((family,) + tuple(str(p) for p in parts)).encode("utf-8")


def is_locked_file(path):
    try:
        with open(path, "rb") as f:
            return f.read(len(MAGIC)) == MAGIC
    except OSError:
        return False


# ---- a locked file as read from disk --------------------------------------------------------------------------
class LockedFile:
    def __init__(self, path):
        self.path = path
        self.manifest_bytes, self.signature, self.blobs, self.appended = None, b"", {}, []
        for kind, payload in read_records(path):
            if kind == b"M":
                self.manifest_bytes = bytes(payload)
            elif kind == b"S":
                self.signature = bytes(payload)
            elif kind == b"B":
                n = int.from_bytes(payload[:2], "big")
                self.blobs[bytes(payload[2:2 + n]).decode("ascii")] = payload[2 + n:]
            elif kind in (b"C", b"A"):
                self.appended.append((kind, bytes(payload)))
        if self.manifest_bytes is None:
            raise LockError("damaged", "no manifest")
        try:
            self.manifest = json.loads(self.manifest_bytes.decode("utf-8"))
            self.family = self.manifest["family"]
        except (ValueError, KeyError) as e:
            raise LockError("damaged", "manifest") from e
        if self.manifest.get("kind") != "database":
            raise LockError("not_locked")

    @property
    def admin_pub(self):
        return crypto.unb64(self.manifest["admin_pub"])

    def signature_ok(self):
        return crypto.verify(self.admin_pub, self.signature, SIG_PREFIX + self.manifest_bytes)

    def head_hash(self):
        return crypto.sha256(self.manifest_bytes + self.signature)

    def users(self):
        return {u["uid"]: u for u in self.manifest["users"]}

    def records(self):
        """[(kind, envelope, chain_ok)] of the appended records, in file order."""
        expect, out = self.head_hash(), []
        for kind, payload in self.appended:
            try:
                env = json.loads(payload.decode("utf-8"))
            except ValueError:
                out.append((kind, None, False))
                expect = crypto.sha256(payload)
                continue
            out.append((kind, env, env.get("prev") == expect))
            expect = crypto.sha256(payload)
        return out

    def record_signature_ok(self, env, users=None):
        u = (users or self.users()).get(env.get("uid"))
        if not u or not u.get("pub"):
            return False
        body = {k: v for k, v in env.items() if k != "sig"}
        return crypto.verify(crypto.unb64(u["pub"]), crypto.unb64(env.get("sig", "")),
                             ENV_PREFIX + crypto.canonical(body))

    def blob(self, bid, expected_hash):
        ct = self.blobs.get(bid)
        if ct is None:
            raise LockError("damaged", f"missing piece {bid}")
        if crypto.sha256(ct) != expected_hash:
            raise LockError("tampered", f"piece {bid} changed")
        return ct

    def _check_signature(self):
        if not self.signature_ok():
            raise LockError("tampered", "manifest signature")

    # ---- opening -----------------------------------------------------------------------------------------
    def unlock(self, name, password, today=None):
        self._check_signature()
        want = lookup(self.family, name)
        rec = next((u for u in self.manifest["users"] if u.get("lookup") == want), None)
        if rec is None:
            crypto.derive(password, crypto.new_kdf())  # the same delay as a real attempt
            raise LockError("wrong_password")
        if rec.get("disabled") or not rec.get("wrap"):
            raise LockError("disabled")
        kek = crypto.derive(password, rec["kdf"])
        try:
            wrap = crypto.unseal_json(kek, rec["wrap"], _aad(self.family, "wrap", rec["uid"]))
        except crypto.CryptoError as e:
            raise LockError("wrong_password") from e
        if wrap.get("admin_pub") != self.manifest["admin_pub"]:
            raise LockError("tampered", "administrator key replaced")
        if rec.get("expires") and rec.get("role") != "admin":
            day = today or dt.date.today()
            if day.isoformat() > rec["expires"]:
                raise LockError("expired", rec["expires"])
        return Session(self, rec, wrap)

    def recover(self, code):
        """The administrator's session from the recovery key (then set a new administrator password)."""
        self._check_signature()
        try:
            key = crypto.recovery_key(code, self.family)
            wrap = crypto.unseal_json(key, self.manifest["recovery"]["wrap"], _aad(self.family, "recovery"))
        except (crypto.CryptoError, KeyError) as e:
            raise LockError("bad_recovery") from e
        if wrap.get("admin_pub") != self.manifest["admin_pub"]:
            raise LockError("tampered", "administrator key replaced")
        rec = self.users()[wrap["uid"]]
        s = Session(self, rec, wrap)
        s.recovered = True
        return s


class MountedLayer:
    def __init__(self, lid, name, path, rights, geom="", hidden=0):
        self.lid, self.name, self.path, self.rights, self.geom, self.hidden = lid, name, path, rights, geom, hidden
        self.base = None  # (users) the layer as it was at the last save, to find what changed
        self.hash0 = crypto.sha256(gpkg.mem_read(path))

    @property
    def uri(self):
        return f"{self.path}|layername={self.name}"


# ---- an opened file --------------------------------------------------------------------------------------------
class Session:
    def __init__(self, lf, rec, wrap):
        self.lf, self.path, self.family = lf, lf.path, lf.family
        self.uid, self.name, self.role = rec["uid"], wrap.get("name", ""), rec.get("role", "user")
        self.is_admin = self.role == "admin"
        self.seed = crypto.unb64(wrap["seed"])
        self.recovered = False
        self.folder = gpkg.private_name()
        self.mounted = {}  # lid -> MountedLayer
        self.audit_import = []
        self.model_dirty = False
        self.closed = False
        self.can = {"export": True, "print": True, "copy": True}
        if self.is_admin:
            self.mk = crypto.unb64(wrap["mk"])
            self.box_priv = crypto.unb64(wrap["box_priv"])
            self.m = copy.deepcopy(lf.manifest)
            self.akeys = crypto.unseal_json(self.mk, self.m["admin_keys"], _aad(self.family, "admin"))
            self.keys = {lid: crypto.unb64(v["key"]) for lid, v in self.akeys["layers"].items()}
            self.rights = {lid: RIGHTS for lid in self.keys}
        else:
            self.m = lf.manifest
            uk = crypto.unb64(wrap["uk"])
            grants = crypto.unseal_json(uk, rec["grants"], _aad(self.family, "grants", self.uid)) \
                if rec.get("grants") else {"keys": {}, "fkeys": {}}
            self.keys = {lid: crypto.unb64(k) for lid, k in grants.get("keys", {}).items()}
            self.fkeys = {lid: {f: crypto.unb64(k) for f, k in d.items()} for lid, d in grants.get("fkeys", {}).items()}
            self.rights = {lid: norm_rights(r) for lid, r in (rec.get("rights") or {}).items()
                           if "v" in norm_rights(r) and lid in self.keys}
            self.can.update({k: bool(v) for k, v in (rec.get("can") or {}).items()})
            self.hidden = rec.get("hidden") or {}
            self.expires = rec.get("expires", "")
        gpkg.hold_memory_temp()

    # ---- what this user sees ----------------------------------------------------------------------------
    def _layer_entry(self, lid):
        return next(x for x in self.lf.manifest["layers"] if x["id"] == lid)

    def layers(self):
        """[{id, name, geom, count, fields, rights}] of the layers this session can open."""
        out = []
        for x in self.lf.manifest["layers"]:
            lid = x["id"]
            if lid not in self.keys or lid not in self.rights:
                continue
            meta = crypto.unseal_json(self.keys[lid], x["meta"], _aad(self.family, "meta", lid))
            fields = list(meta.get("fields", []))
            if self.is_admin:
                fields += [n for n in self.akeys["layers"][lid].get("protected", []) if n not in fields]
            out.append({"id": lid, "name": meta["name"], "geom": meta.get("geom", ""), "count": meta.get("count", 0),
                        "fields": fields, "rights": self.rights[lid]})
        return out

    def can_decrypt(self, lid):
        """True when this session's keys open the layer's piece (the crypto check, not the rights list)."""
        key = self.keys.get(lid)
        if key is None:
            return False
        x = next((y for y in self.lf.manifest["layers"] if y["id"] == lid), None)
        if x is None:
            return False
        try:
            crypto.unseal(key, self.lf.blob(x["blob"], x["hash"]), _aad(self.family, "blob", x["blob"]))
            return True
        except crypto.CryptoError:
            return False

    def _field_payloads(self, lid, x):
        out = {}
        for fe in x.get("fields", []):
            key = None
            if self.is_admin:
                key = next((crypto.unb64(v["key"]) for v in self.akeys.get("fields", {}).get(lid, {}).values()
                            if v["id"] == fe["id"]), None)
            else:
                k = self.fkeys.get(lid, {}).get(fe["id"])
                key = k
            if key is None:
                continue
            payload = json.loads(crypto.unseal(key, self.lf.blob(fe["blob"], fe["hash"]),
                                               _aad(self.family, "blob", fe["blob"])).decode("utf-8"))
            out[payload["name"]] = payload
        return out

    def _materialize(self, lid, folder):
        """The layer decrypted into memory, with the protected fields this session may see put back."""
        x = self._layer_entry(lid)
        data = crypto.unseal(self.keys[lid], self.lf.blob(x["blob"], x["hash"]), _aad(self.family, "blob", x["blob"]))
        meta = crypto.unseal_json(self.keys[lid], x["meta"], _aad(self.family, "meta", lid))
        path = gpkg.new_path(folder, meta["name"])
        gpkg.mem_write(path, data)
        del data
        fields = self._field_payloads(lid, x)
        gpkg.join_fields(path, meta["name"], fields)
        return path, meta

    def mount(self):
        """Every layer this session may see, decrypted into memory (GDAL /vsimem/): [MountedLayer]."""
        for x in self.lf.manifest["layers"]:
            lid = x["id"]
            if lid in self.mounted or lid not in self.rights:
                continue
            path, meta = self._materialize(lid, self.folder)
            hidden = 0
            if not self.is_admin:
                hidden = len([f for f in self.hidden.get(lid, [])])
                self._apply_own_changes(lid, path, meta["name"])
            ml = MountedLayer(lid, meta["name"], path, self.rights[lid], meta.get("geom", ""), hidden)
            if not self.is_admin:
                ml.base = gpkg.copy_mem(path, self.folder, "base_" + meta["name"])
            self.mounted[lid] = ml
        return list(self.mounted.values())

    def _apply_own_changes(self, lid, path, name):
        """A user sees his own changes not yet merged by the administrator."""
        users = self.lf.users()
        fid_map = {}
        for kind, env, _ in self.lf.records():
            if kind != b"C" or not env or env.get("uid") != self.uid or env.get("lid") != lid:
                continue
            if not self.lf.record_signature_ok(env, users):
                continue
            try:
                changes = crypto.unseal_json(self.keys[lid], env["ct"], _aad(self.family, "changes", lid, self.uid))
            except crypto.CryptoError:
                continue
            gpkg.apply(path, name, changes, fid_map)

    # ---- appended records -------------------------------------------------------------------------------
    def _append(self, kind, body):
        body["uid"] = self.uid
        body["t"] = int(time.time())
        body["prev"] = tail_hash(self.path)
        body["sig"] = crypto.b64(crypto.sign(self.seed, ENV_PREFIX + crypto.canonical(body)))
        append_record(self.path, kind, crypto.canonical(body))

    def log(self, action, details=None):
        """An activity-log entry, signed by this user and readable by the administrator only."""
        entry = {"action": action, "name": self.name, "details": details or {}}
        eph, ct = crypto.box_seal(crypto.unb64(self.lf.manifest["box_pub"]), crypto.canonical(entry),
                                  _aad(self.family, "audit"))
        try:
            self._append(b"A", {"type": "audit", "eph": crypto.b64(eph), "ct": crypto.b64(ct)})
        except OSError:
            pass  # a read-only copy (a CD, a protected share): it still opens

    def changed_layers(self):
        return [ml for ml in self.mounted.values() if crypto.sha256(gpkg.mem_read(ml.path)) != ml.hash0]

    def save(self):
        """Users: what changed since the last save becomes a signed change set appended to the file.
        Administrator: a new version of the file (see AdminSession.save). Returns a summary."""
        if self.is_admin:
            return self._admin_save()
        out = []
        for ml in self.changed_layers():
            changes = gpkg.diff(ml.base, ml.path, ml.name)
            if gpkg.is_empty(changes):
                ml.hash0 = crypto.sha256(gpkg.mem_read(ml.path))
                continue
            ct = crypto.seal_json(self.keys[ml.lid], changes, _aad(self.family, "changes", ml.lid, self.uid))
            self._append(b"C", {"type": "changes", "lid": ml.lid, "id": uuid.uuid4().hex,
                                "base": self.lf.manifest["version"], "ct": ct})
            counts = {k: len(changes[k]) for k in ("adds", "edits", "deletes")}
            refused = [op for op, r in (("adds", "a"), ("edits", "e"), ("deletes", "d"))
                       if counts[op] and r not in ml.rights]
            out.append({"lid": ml.lid, "layer": ml.name, **counts, "not_allowed": refused})
            gpkg.mem_wipe(ml.base)
            ml.base = gpkg.copy_mem(ml.path, self.folder, "base_" + ml.name)
            ml.hash0 = crypto.sha256(gpkg.mem_read(ml.path))
        if out:
            self.log("save", {"layers": [{k: v for k, v in o.items() if k != "lid"} for o in out]})
            self.lf = LockedFile(self.path)
        return out

    def close(self):
        if self.closed:
            return
        self.closed = True
        for ml in self.mounted.values():
            gpkg.mem_wipe(ml.path)
            if ml.base:
                gpkg.mem_wipe(ml.base)
        gpkg.wipe_folder(self.folder)
        self.mounted.clear()
        self.keys = {}
        self.seed = b""
        if self.is_admin:
            self.mk = self.box_priv = b""
        gpkg.release_memory_temp()

    # ======================================================================================================
    # the administrator
    # ======================================================================================================
    def _need_admin(self):
        if not self.is_admin:
            raise PermissionError("administrator only")

    def users(self):
        """[{uid, name, role, disabled, expires, can, rights {lid: 'vaed'}, hidden {lid: [field names]}}]"""
        self._need_admin()
        names = {}
        for lid, fields in self.akeys.get("fields", {}).items():
            for fname, v in fields.items():
                names[(lid, v["id"])] = fname
        out = []
        for u in self.m["users"]:
            out.append({"uid": u["uid"], "name": self.akeys["users"].get(u["uid"], {}).get("name", "?"),
                        "role": u.get("role", "user"), "disabled": bool(u.get("disabled")),
                        "expires": u.get("expires", ""), "can": dict(u.get("can") or {}),
                        "rights": {lid: norm_rights(r) for lid, r in (u.get("rights") or {}).items()},
                        "hidden": {lid: [names.get((lid, f), f) for f in fl] for lid, fl in (u.get("hidden") or {}).items()},
                        "fingerprint": crypto.fingerprint(crypto.unb64(u["pub"])) if u.get("pub") else ""})
        return out

    def _user(self, uid):
        return next(u for u in self.m["users"] if u["uid"] == uid)

    def _set_password(self, rec, password, extra):
        kdf = crypto.new_kdf()
        kek = crypto.derive(password, kdf)
        rec["kdf"] = kdf
        rec["wrap"] = crypto.seal_json(kek, extra, _aad(self.family, "wrap", rec["uid"]))

    def add_user(self, name, password, rights=None, can=None, expires=""):
        self._need_admin()
        key = lookup(self.family, name)
        if any(u.get("lookup") == key for u in self.m["users"]):
            raise LockError("name_taken", name)
        uid = _id("u")
        seed, uk = os.urandom(32), crypto.random_key()
        rec = {"uid": uid, "lookup": key, "role": "user", "pub": crypto.b64(crypto.public_key(seed)),
               "rights": {lid: norm_rights(r) for lid, r in (rights or {}).items()}, "hidden": {},
               "can": {"export": False, "print": True, "copy": False, **(can or {})}, "expires": expires or "",
               "disabled": False}
        self._set_password(rec, password, {"uid": uid, "name": name.strip(), "seed": crypto.b64(seed),
                                           "uk": crypto.b64(uk), "admin_pub": self.m["admin_pub"]})
        self.m["users"].append(rec)
        self.akeys["users"][uid] = {"name": name.strip(), "uk": crypto.b64(uk)}
        self.model_dirty = True
        return uid

    def reset_password(self, uid, password):
        """A new password (and a new signing key and user key) for a user; for the administrator, a new
        administrator password (the administrator's key stays the same)."""
        self._need_admin()
        rec = self._user(uid)
        name = self.akeys["users"][uid]["name"]
        if rec.get("role") == "admin":
            self._set_password(rec, password, {"uid": uid, "name": name, "seed": crypto.b64(self.seed),
                                               "mk": crypto.b64(self.mk), "box_priv": crypto.b64(self.box_priv),
                                               "admin_pub": self.m["admin_pub"]})
        else:
            seed, uk = os.urandom(32), crypto.random_key()
            rec["pub"] = crypto.b64(crypto.public_key(seed))
            self._set_password(rec, password, {"uid": uid, "name": name, "seed": crypto.b64(seed),
                                               "uk": crypto.b64(uk), "admin_pub": self.m["admin_pub"]})
            self.akeys["users"][uid]["uk"] = crypto.b64(uk)
            rec["disabled"] = False
        self.model_dirty = True

    def set_rights(self, uid, lid, rights):
        self._need_admin()
        self._user(uid).setdefault("rights", {})[lid] = norm_rights(rights)
        self.model_dirty = True

    def set_hidden(self, uid, lid, field_names):
        """Fields of a layer this user must not see. Their values move into pieces of their own, each with its
        own key, which this user does not receive."""
        self._need_admin()
        fields = self.akeys.setdefault("fields", {}).setdefault(lid, {})
        ids = []
        for n in field_names:
            if n not in fields:
                fields[n] = {"id": _id("f"), "key": crypto.b64(crypto.random_key()), "viewers": []}
            ids.append(fields[n]["id"])
        self._user(uid).setdefault("hidden", {})[lid] = ids
        self.model_dirty = True

    def set_can(self, uid, **flags):
        self._need_admin()
        self._user(uid).setdefault("can", {}).update({k: bool(v) for k, v in flags.items()})
        self.model_dirty = True

    def set_expiry(self, uid, date_text):
        self._need_admin()
        self._user(uid)["expires"] = date_text or ""
        self.model_dirty = True

    def disable(self, uid):
        """The user's record keeps its name but loses its keys: newer versions of the file no longer open
        for him (copies he already has still do)."""
        self._need_admin()
        rec = self._user(uid)
        if rec.get("role") == "admin":
            raise PermissionError("the administrator cannot be disabled")
        rec["disabled"] = True
        rec["wrap"] = rec["grants"] = None
        self.model_dirty = True

    def remove_user(self, uid):
        self._need_admin()
        rec = self._user(uid)
        if rec.get("role") == "admin":
            raise PermissionError("the administrator cannot be removed")
        self.m["users"] = [u for u in self.m["users"] if u["uid"] != uid]
        self.model_dirty = True

    def dirty(self):
        return self.model_dirty or bool(self.changed_layers()) or bool(self.audit_import)

    # ---- who sees what -----------------------------------------------------------------------------------
    def _active_users(self):
        return [u for u in self.m["users"] if not u.get("disabled") and u.get("wrap")]

    def _viewers(self, lid):
        return sorted(u["uid"] for u in self._active_users()
                      if u.get("role") == "admin" or "v" in norm_rights((u.get("rights") or {}).get(lid)))

    def _protected(self, lid):
        """Fields of a layer hidden from at least one user who sees the layer."""
        by_id = {v["id"]: n for n, v in self.akeys.get("fields", {}).get(lid, {}).items()}
        out = set()
        for u in self._active_users():
            if u.get("role") != "admin" and "v" in norm_rights((u.get("rights") or {}).get(lid)):
                out.update(by_id[f] for f in (u.get("hidden") or {}).get(lid, []) if f in by_id)
        return sorted(out)

    def _field_viewers(self, lid, fid_):
        return sorted(u["uid"] for u in self._active_users()
                      if u.get("role") == "admin" or ("v" in norm_rights((u.get("rights") or {}).get(lid))
                                                      and fid_ not in (u.get("hidden") or {}).get(lid, [])))

    # ---- saving a new version ------------------------------------------------------------------------------
    def _admin_save(self):
        self._need_admin()
        fresh = LockedFile(self.path)
        if fresh.manifest_bytes != self.lf.manifest_bytes:
            raise LockError("changed_elsewhere")
        self.lf = fresh
        report = None
        if any(k == b"C" and env and env.get("id") not in self.akeys.get("merged", [])
               for k, env, _ in fresh.records()):
            report = self.merge(None)
        self._collect_audit(fresh)
        m = self.m
        old_layers = {x["id"]: x for x in fresh.manifest["layers"]}
        new_layers, blobs = [], {}
        changed_paths = {ml.lid for ml in self.changed_layers()}
        for x in old_layers.values():
            lid = x["id"]
            info = self.akeys["layers"][lid]
            viewers = self._viewers(lid)
            rotate = any(u not in viewers for u in info.get("viewers", []))
            protected = self._protected(lid)
            fields = self.akeys.setdefault("fields", {}).setdefault(lid, {})
            frot = False
            for n in protected:
                fv = self._field_viewers(lid, fields[n]["id"])
                if any(u not in fv for u in fields[n].get("viewers", [])) or rotate:
                    frot = True
            redo = lid in changed_paths or rotate or frot or protected != info.get("protected", [])
            if not redo:
                entry = dict(x)
                blobs[x["blob"]] = fresh.blob(x["blob"], x["hash"])
                for fe in x.get("fields", []):
                    blobs[fe["blob"]] = fresh.blob(fe["blob"], fe["hash"])
                new_layers.append(entry)
                info["viewers"] = viewers
                continue
            ml = self.mounted.get(lid)
            if ml is not None:
                plain, name = gpkg.mem_read(ml.path), ml.name
            else:
                path, meta = self._materialize(lid, self.folder)
                plain, name = gpkg.mem_read(path), meta["name"]
                gpkg.mem_wipe(path)
            main, split = gpkg.strip_fields(plain, name, protected)
            del plain
            if rotate:
                info["key"] = crypto.b64(crypto.random_key())
                self.keys[lid] = crypto.unb64(info["key"])
            key = self.keys[lid]
            tmp = gpkg.new_path(self.folder, "count")
            gpkg.mem_write(tmp, main)
            count, fnames = gpkg.feature_count(tmp, name), gpkg.field_names(tmp, name)
            geom = gpkg.list_layers(tmp)[0][1] if gpkg.list_layers(tmp) else ""
            gpkg.mem_wipe(tmp)
            bid = "L" + lid
            blobs[bid] = crypto.seal(key, main, _aad(self.family, "blob", bid), compress=True)
            entry = {"id": lid, "blob": bid, "hash": crypto.sha256(blobs[bid]),
                     "meta": crypto.seal_json(key, {"name": name, "geom": geom, "count": count, "fields": fnames},
                                              _aad(self.family, "meta", lid)), "fields": []}
            for n in protected:
                fe = fields[n]
                fv = self._field_viewers(lid, fe["id"])
                if any(u not in fv for u in fe.get("viewers", [])) or rotate:
                    fe["key"] = crypto.b64(crypto.random_key())
                fe["viewers"] = fv
                fbid = f"F{lid}.{fe['id']}"
                payload = {"name": n, **split.get(n, {"def": {"type": 4}, "values": {}})}
                blobs[fbid] = crypto.seal(crypto.unb64(fe["key"]), crypto.canonical(payload),
                                          _aad(self.family, "blob", fbid), compress=True)
                entry["fields"].append({"id": fe["id"], "blob": fbid, "hash": crypto.sha256(blobs[fbid])})
            info["protected"] = protected
            info["viewers"] = viewers
            new_layers.append(entry)
        m["layers"] = new_layers
        # every user's grants: the keys of what he may see, nothing else
        for u in m["users"]:
            if u.get("role") == "admin" or u.get("disabled") or not u.get("wrap"):
                continue
            uk = crypto.unb64(self.akeys["users"][u["uid"]]["uk"])
            keys, fkeys = {}, {}
            for x in new_layers:
                lid = x["id"]
                if "v" not in norm_rights((u.get("rights") or {}).get(lid)):
                    continue
                keys[lid] = self.akeys["layers"][lid]["key"]
                hidden = set((u.get("hidden") or {}).get(lid, []))
                for fe in x.get("fields", []):
                    if fe["id"] not in hidden:
                        fkeys.setdefault(lid, {})[fe["id"]] = next(
                            v["key"] for v in self.akeys["fields"][lid].values() if v["id"] == fe["id"])
            u["grants"] = crypto.seal_json(uk, {"keys": keys, "fkeys": fkeys}, _aad(self.family, "grants", u["uid"]))
        # the activity log, folded into a piece only the administrator opens
        archive = self.read_audit()
        akey = crypto.hkdf(self.mk, self.family.encode("ascii"), b"biruni-locked-archive")
        blobs["AUDIT"] = crypto.seal(akey, crypto.canonical({"entries": archive}), _aad(self.family, "blob", "AUDIT"),
                                     compress=True)
        m["archive"] = {"blob": "AUDIT", "hash": crypto.sha256(blobs["AUDIT"])}
        m["version"] = int(m.get("version", 1)) + 1
        m["saved"] = _now()
        m["admin_keys"] = crypto.seal_json(self.mk, self.akeys, _aad(self.family, "admin"))
        self._write(self.path, m, blobs)
        self.lf = LockedFile(self.path)
        self.m = copy.deepcopy(self.lf.manifest)
        self.audit_import = []
        self.model_dirty = False
        for ml in self.mounted.values():
            ml.hash0 = crypto.sha256(gpkg.mem_read(ml.path))
        return report

    def _write(self, path, m, blobs):
        mb = crypto.canonical(m)
        sig = crypto.sign(self.seed, SIG_PREFIX + mb)
        records = [(b"M", mb), (b"S", sig)] + [(b"B", blob_record(bid, ct)) for bid, ct in blobs.items()]
        write_records(path, records)

    # ---- the activity log ----------------------------------------------------------------------------------
    def _archive(self, lf=None):
        lf = lf or self.lf
        a = lf.manifest.get("archive")
        if not a or not self.is_admin:
            return []
        akey = crypto.hkdf(self.mk, self.family.encode("ascii"), b"biruni-locked-archive")
        try:
            return json.loads(crypto.unseal(akey, lf.blob(a["blob"], a["hash"]),
                                            _aad(self.family, "blob", "AUDIT")).decode("utf-8"))["entries"]
        except (crypto.CryptoError, LockError, KeyError, ValueError):
            return []

    def _audit_entries(self, lf):
        users = lf.users()
        out = []
        for kind, env, chain_ok in lf.records():
            if kind != b"A" or not env:
                continue
            signed = lf.record_signature_ok(env, users)
            try:
                entry = json.loads(crypto.box_open(self.box_priv, crypto.unb64(env["eph"]), crypto.unb64(env["ct"]),
                                                   _aad(self.family, "audit")).decode("utf-8"))
            except (crypto.CryptoError, KeyError, ValueError):
                entry = {"action": "unreadable", "details": {}}
            name = self.akeys["users"].get(env.get("uid"), {}).get("name", entry.get("name", "?"))
            out.append({"t": env.get("t", 0), "uid": env.get("uid"), "user": name, "action": entry.get("action"),
                        "details": entry.get("details", {}), "signed": signed, "chain": chain_ok,
                        "sig": env.get("sig", "")[:24]})
        return out

    def _collect_audit(self, lf):
        seen = {e.get("sig") for e in self.audit_import}
        for e in self._audit_entries(lf):
            if e["sig"] not in seen:
                self.audit_import.append(e)
                seen.add(e["sig"])

    def read_audit(self):
        """Every activity entry: those archived by earlier saves, then the ones appended since, with whether
        their signature and their place in the chain are intact."""
        self._need_admin()
        out = list(self._archive(self.lf))
        seen = {e.get("sig") for e in out}
        for e in self._audit_entries(LockedFile(self.path)) + self.audit_import:
            if e["sig"] not in seen:
                out.append(e)
                seen.add(e["sig"])
        return sorted(out, key=lambda e: e.get("t", 0))

    # ---- merging returned copies -----------------------------------------------------------------------------
    def merge(self, path=None):
        """Verifies every change set of a returned copy (or of this file, path=None) against its author's
        signature and rights, applies the allowed changes to the layers in memory, and reports the rest.
        Save afterwards to write them. Returns a list of {user, layer, op, count, ok, reason}."""
        self._need_admin()
        src = LockedFile(path) if path else LockedFile(self.path)
        if src.family != self.family:
            raise LockError("other_family")
        if not src.signature_ok() or src.manifest["admin_pub"] != self.m["admin_pub"]:
            raise LockError("tampered", "copy manifest")
        src_akeys = crypto.unseal_json(self.mk, src.manifest["admin_keys"], _aad(self.family, "admin"))
        src_users = src.users()
        merged = self.akeys.setdefault("merged", [])
        fidmaps = self.akeys.setdefault("fidmap", {})
        report = []
        lnames = {}
        for x in self.lf.manifest["layers"]:
            try:
                lnames[x["id"]] = crypto.unseal_json(self.keys[x["id"]], x["meta"],
                                                     _aad(self.family, "meta", x["id"]))["name"]
            except (crypto.CryptoError, KeyError):
                pass

        def add(user, layer, op, count, ok, reason=""):
            if count:
                report.append({"user": user, "layer": layer, "op": op, "count": count, "ok": ok, "reason": reason})

        for kind, env, chain_ok in src.records():
            if kind != b"C" or not env or env.get("id") in merged:
                continue
            uid, lid = env.get("uid"), env.get("lid")
            uname = self.akeys["users"].get(uid, {}).get("name") or src_akeys["users"].get(uid, {}).get("name", "?")
            lname = lnames.get(lid, src_akeys["layers"].get(lid, {}).get("name", "?"))
            merged.append(env.get("id"))
            if not src.record_signature_ok(env, src_users):
                add(uname, lname, "all", 1, False, "signature")
                continue
            try:
                changes = crypto.unseal_json(crypto.unb64(src_akeys["layers"][lid]["key"]), env["ct"],
                                             _aad(self.family, "changes", lid, uid))
            except (crypto.CryptoError, KeyError):
                add(uname, lname, "all", 1, False, "unreadable")
                continue
            cur = next((u for u in self.m["users"] if u["uid"] == uid), None)
            n_add, n_edit, n_del = (len(changes.get(k, [])) for k in ("adds", "edits", "deletes"))
            if changes.get("schema"):
                add(uname, lname, "schema", 1, False, "schema")
            if cur is None or cur.get("disabled") or lid not in self.akeys["layers"]:
                for op, n in (("add", n_add), ("edit", n_edit), ("delete", n_del)):
                    add(uname, lname, op, n, False, "user_removed" if lid in self.akeys["layers"] else "no_layer")
                continue
            had = norm_rights((src_users.get(uid, {}).get("rights") or {}).get(lid))
            now = norm_rights((cur.get("rights") or {}).get(lid))
            eff = "".join(ch for ch in RIGHTS if ch in had and ch in now)
            by_id = {}
            for fname, v in list(src_akeys.get("fields", {}).get(lid, {}).items()) + \
                    list(self.akeys.get("fields", {}).get(lid, {}).items()):
                by_id[v["id"]] = fname
            hidden = {by_id.get(f) for f in (src_users.get(uid, {}).get("hidden") or {}).get(lid, [])} | \
                     {by_id.get(f) for f in (cur.get("hidden") or {}).get(lid, [])}
            hidden.discard(None)
            allowed = {"adds": [], "edits": [], "deletes": []}
            if "v" not in eff:
                for op, n in (("add", n_add), ("edit", n_edit), ("delete", n_del)):
                    add(uname, lname, op, n, False, "no_view")
                continue
            for op, right, key in (("add", "a", "adds"), ("edit", "e", "edits"), ("delete", "d", "deletes")):
                items = changes.get(key, [])
                if not items:
                    continue
                if right not in eff:
                    add(uname, lname, op, len(items), False, "no_" + op)
                    continue
                if key == "deletes":
                    allowed[key] = items
                    continue
                ok_items = [it for it in items if not (set((it.get("attrs") or {})) & hidden)]
                add(uname, lname, op, len(items) - len(ok_items), False, "hidden_field")
                allowed[key] = ok_items
            if not any(allowed.values()):
                continue
            ml = self.mounted.get(lid)
            if ml is None:
                self.mount()
                ml = self.mounted[lid]
            fmap = fidmaps.setdefault(f"{uid}|{lid}", {})
            done = gpkg.apply(ml.path, ml.name, allowed, fmap, keep_fids=False)
            add(uname, lname, "add", done["adds"], True)
            add(uname, lname, "edit", done["edits"], True)
            add(uname, lname, "delete", done["deletes"], True)
            add(uname, lname, "missing", len(done["missing"]), False, "missing")
        self._collect_audit(src)
        ok = sum(r["count"] for r in report if r["ok"])
        bad = sum(r["count"] for r in report if not r["ok"])
        self.log("merge", {"file": os.path.basename(src.path), "applied": ok, "refused": bad,
                           "refusals": [r for r in report if not r["ok"]][:50]})
        self.model_dirty = True
        return report

    # ---- copies for users --------------------------------------------------------------------------------------
    def send_to(self, uids, out_path, only_visible=True):
        """A copy of the saved file for some users (and the administrator). only_visible=True leaves out the
        pieces none of them may see, so the copy does not even carry their ciphertext."""
        self._need_admin()
        if self.dirty():
            self._admin_save()
        lf = LockedFile(self.path)
        m = json.loads(lf.manifest_bytes.decode("utf-8"))
        keep = set(uids)
        users = [u for u in m["users"] if u.get("role") == "admin" or u["uid"] in keep]
        m["users"] = users
        chosen = [u for u in users if u.get("role") != "admin" and not u.get("disabled")]
        blobs = {}
        layers = []
        for x in m["layers"]:
            seen_by = [u for u in chosen if "v" in norm_rights((u.get("rights") or {}).get(x["id"]))]
            if only_visible and not seen_by:
                continue
            x = dict(x)
            if only_visible:
                x["fields"] = [fe for fe in x.get("fields", [])
                               if any(fe["id"] not in (u.get("hidden") or {}).get(x["id"], []) for u in seen_by)]
            blobs[x["blob"]] = lf.blob(x["blob"], x["hash"])
            for fe in x.get("fields", []):
                blobs[fe["blob"]] = lf.blob(fe["blob"], fe["hash"])
            layers.append(x)
        m["layers"] = layers
        m.pop("archive", None)
        m["copy_of"] = {"version": m["version"], "made": _now(), "partial": bool(only_visible)}
        self._write(out_path, m, blobs)
        names = [self.akeys["users"].get(u, {}).get("name", "?") for u in uids]
        self.log("send", {"to": names, "file": os.path.basename(out_path), "layers": len(layers),
                          "only_visible": bool(only_visible)})
        return out_path


# ---- creating a locked database ---------------------------------------------------------------------------------
def create(src, layer_names, out_path, admin_name, admin_password, recovery_code, progress=None):
    """Locks layers of a GeoPackage into out_path (*.bgpkg). The original file is not changed."""
    family = uuid.uuid4().hex
    mk = crypto.random_key()
    admin_seed = os.urandom(32)
    admin_pub = crypto.b64(crypto.public_key(admin_seed))
    box_priv, box_pub = crypto.box_keypair()
    admin_uid = _id("u")
    akeys = {"layers": {}, "fields": {}, "users": {admin_uid: {"name": admin_name.strip(), "uk": ""}},
             "merged": [], "fidmap": {}}
    info = {n: (g, c, f) for n, g, c, f in gpkg.list_layers(src)}
    blobs, layers = {}, []
    gpkg.hold_memory_temp()
    try:
        for i, name in enumerate(layer_names):
            if progress:
                progress(i, len(layer_names), name)
            lid = _id("l")
            key = crypto.random_key()
            data = gpkg.extract_layer(src, name)
            bid = "L" + lid
            blobs[bid] = crypto.seal(key, data, _aad(family, "blob", bid), compress=True)
            del data
            geom, count, fields = info.get(name, ("", 0, []))
            layers.append({"id": lid, "blob": bid, "hash": crypto.sha256(blobs[bid]), "fields": [],
                           "meta": crypto.seal_json(key, {"name": name, "geom": geom, "count": count, "fields": fields},
                                                    _aad(family, "meta", lid))})
            akeys["layers"][lid] = {"key": crypto.b64(key), "name": name, "protected": [], "viewers": [admin_uid]}
    finally:
        gpkg.release_memory_temp()
    admin_wrap = {"uid": admin_uid, "name": admin_name.strip(), "seed": crypto.b64(admin_seed), "mk": crypto.b64(mk),
                  "box_priv": crypto.b64(box_priv), "admin_pub": admin_pub}
    kdf = crypto.new_kdf()
    kek = crypto.derive(admin_password, kdf)
    admin_rec = {"uid": admin_uid, "lookup": lookup(family, admin_name), "role": "admin", "pub": admin_pub,
                 "kdf": kdf, "wrap": crypto.seal_json(kek, admin_wrap, _aad(family, "wrap", admin_uid)),
                 "grants": None, "rights": {}, "hidden": {}, "can": {"export": True, "print": True, "copy": True},
                 "expires": "", "disabled": False}
    rkey = crypto.recovery_key(recovery_code, family)
    archive = [{"t": int(time.time()), "uid": admin_uid, "user": admin_name.strip(), "action": "create",
                "details": {"layers": list(layer_names), "source": os.path.basename(src)}, "signed": True,
                "chain": True, "sig": "create"}]
    akey = crypto.hkdf(mk, family.encode("ascii"), b"biruni-locked-archive")
    blobs["AUDIT"] = crypto.seal(akey, crypto.canonical({"entries": archive}), _aad(family, "blob", "AUDIT"),
                                 compress=True)
    m = {"biruni_locked": 1, "kind": "database", "family": family, "version": 1, "created": _now(), "saved": _now(),
         "admin_pub": admin_pub, "box_pub": crypto.b64(box_pub), "users": [admin_rec],
         "recovery": {"wrap": crypto.seal_json(rkey, admin_wrap, _aad(family, "recovery"))},
         "admin_keys": crypto.seal_json(mk, akeys, _aad(family, "admin")), "layers": layers,
         "archive": {"blob": "AUDIT", "hash": crypto.sha256(blobs["AUDIT"])}}
    mb = crypto.canonical(m)
    sig = crypto.sign(admin_seed, SIG_PREFIX + mb)
    write_records(out_path, [(b"M", mb), (b"S", sig)] + [(b"B", blob_record(b, c)) for b, c in blobs.items()])
    return out_path
