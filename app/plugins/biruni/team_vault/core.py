"""The team vault: snapshots of files kept safe on the computers of the team, k of n.

A snapshot is cut into content-addressed chunks (store.py). The chunks no earlier snapshot sent out go into a PACK
with the snapshot's manifest; the pack is encrypted (AES-256-GCM, a fresh random key), the ciphertext is cut into n
shards with Reed-Solomon (rs.py: any k give it back) and the key into n Shamir shares (shamir.py: k-1 say nothing).
Shard i carries share i, so a computer holding one shard - or k-1 of them - can read nothing. With a vault password
the key also needs the password: then even k members together read nothing.

Every pack has a public header signed by the owner (ed25519, a key derived from this PC's dashboard key): who owns
it, k, n, sizes and the hash of every shard. A holder checks the signature and its shard's hash before keeping it,
a restore checks them before using a shard; nothing altered is ever used.

Shard file (*.bvs):  b"BVS1" | u32 length | JSON {"hdr", "sig", "idx", "share"} | shard bytes
"""
import base64
import hashlib
import json
import os
import secrets
import sqlite3
import struct
import threading
import time

from . import rs, shamir, store

MAGIC = b"BVS1"
KIT = "BVK1"
INFO = b"biruni team vault v1"
DEFAULT_K, DEFAULT_N = 3, 5
DROP_GRACE = 30 * 86400   # a withdrawn pack stays on the members this long before it is deleted


class VaultError(Exception):
    pass


def _crypto():
    from ..secure import crypto

    return crypto


def canonical(obj):
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def b64u(b):
    return base64.urlsafe_b64encode(bytes(b)).decode("ascii").rstrip("=")


def unb64u(t):
    return base64.urlsafe_b64decode(str(t) + "=" * (-len(str(t)) % 4))


# ---- identity -----------------------------------------------------------------------------------------------------
def derive_seed(dashboard_seed):
    """The vault's signing key, derived from the dashboard key: the recovery kit hands out only this one."""
    return _crypto().hkdf(bytes(dashboard_seed), b"biruni-team-vault", b"owner key")


def default_seed():
    """This PC's vault key: a recovery kit read on this PC wins, else derived from the dashboard key."""
    try:
        from qgis.core import QgsSettings

        from ..field_photos.tunnel import unprotect

        v = unprotect(QgsSettings().value("biruni/team_vault/kit_seed", "") or "") or ""
        if len(v) == 64:
            return bytes.fromhex(v)
    except Exception:  # noqa: BLE001 - no settings (tests) or nothing kept
        pass
    from ..dashboard import cards

    return derive_seed(cards.seed())


def kit_text(seed):
    """The recovery kit: the vault key as text (keep it on paper or a USB stick, away from this computer)."""
    pub = _crypto().public_key(seed)
    return f"{KIT}.{b64u(seed)}.{hashlib.sha256(pub).hexdigest()[:8]}"


def read_kit(text):
    try:
        tag, s, chk = str(text).strip().split(".")
        seed = unb64u(s)
        assert tag == KIT and len(seed) == 32
    except Exception:  # noqa: BLE001
        raise VaultError("this is not a vault recovery kit") from None
    if hashlib.sha256(_crypto().public_key(seed)).hexdigest()[:8] != chk:
        raise VaultError("the recovery kit has a typing mistake")
    return seed


def fingerprint(pub_hex):
    h = hashlib.sha256(bytes.fromhex(pub_hex)).hexdigest()[:16].upper()
    return " ".join(h[i:i + 4] for i in range(0, 16, 4))


# ---- shard files --------------------------------------------------------------------------------------------------
def shard_digest(share, data):
    return hashlib.sha256(bytes(share) + bytes(data)).hexdigest()


def pack_shard(hdr, sig, idx, share, data):
    meta = canonical({"hdr": hdr, "sig": sig, "idx": idx, "share": bytes(share).hex()})
    return MAGIC + struct.pack(">I", len(meta)) + meta + bytes(data)


def parse_shard(blob, owner=None):
    """bytes of a .bvs -> (hdr, idx, share, data), checked: signature, index, hash. ``owner``: only this owner."""
    blob = bytes(blob)
    if blob[:4] != MAGIC or len(blob) < 8:
        raise VaultError("not a vault shard")
    (ln,) = struct.unpack(">I", blob[4:8])
    if ln > 1 << 20:
        raise VaultError("a broken shard")
    try:
        meta = json.loads(blob[8:8 + ln].decode("utf-8"))
        hdr, sig, idx, share = meta["hdr"], meta["sig"], int(meta["idx"]), bytes.fromhex(meta["share"])
    except Exception:  # noqa: BLE001
        raise VaultError("a broken shard") from None
    data = blob[8 + ln:]
    check_header(hdr, sig, owner)
    if not 0 <= idx < int(hdr["n"]):
        raise VaultError("a shard with a wrong number")
    if len(data) != int(hdr["ss"]) or shard_digest(share, data) != hdr["h"][idx]:
        raise VaultError("a shard was altered")
    return hdr, idx, share, data


def check_header(hdr, sig, owner=None):
    try:
        ok = _crypto().verify(bytes.fromhex(hdr["owner"]), unb64u(sig), canonical(hdr))
    except Exception:  # noqa: BLE001
        ok = False
    if not ok:
        raise VaultError("a shard whose signature does not check out")
    if owner and hdr["owner"] != owner:
        raise VaultError("a shard of another owner")
    if not (1 <= int(hdr["k"]) <= int(hdr["n"]) <= rs.MAX_N) or len(hdr["h"]) != int(hdr["n"]):
        raise VaultError("a broken shard header")
    return hdr


def sign_header(seed, hdr):
    return b64u(_crypto().sign(seed, canonical(hdr)))


def _pack_key(secret, pack_id, hdr, password):
    pw = b""
    if hdr.get("kdf"):
        if not password:
            raise VaultError("this snapshot needs the vault password")
        pw = _crypto().derive(password, hdr["kdf"])
    return _crypto().hkdf(bytes(secret) + pw, pack_id.encode("ascii"), INFO)


def open_pack(shards, password=None):
    """{idx: (hdr, share, data)} of one pack (k or more) -> (hdr, manifest, payload bytes)."""
    if not shards:
        raise VaultError("no shard")
    hdr = next(iter(shards.values()))[0]
    k, n = int(hdr["k"]), int(hdr["n"])
    same = {i: v for i, v in shards.items() if canonical(v[0]) == canonical(hdr)}
    if len(same) < k:
        raise VaultError(f"{len(same)} shards of this snapshot are at hand, {k} are needed")
    pick = sorted(same)[:k]
    secret = shamir.combine([(i + 1, same[i][1]) for i in pick])
    ct = rs.decode({i: same[i][2] for i in pick}, k, n, int(hdr["size"]))
    key = _pack_key(secret, hdr["pack"], hdr, password)
    try:
        plain = _crypto().unseal(key, ct, hdr["pack"].encode("ascii"), max_size=8 << 30)
    except Exception:  # noqa: BLE001
        raise VaultError("wrong vault password" if hdr.get("kdf") else "the shards do not open") from None
    (ml,) = struct.unpack(">I", plain[:4])
    manifest = json.loads(plain[4:4 + ml].decode("utf-8"))
    if manifest.get("pack") != hdr["pack"]:
        raise VaultError("a pack and its manifest do not match")
    return hdr, manifest, memoryview(plain)[4 + ml:]


def rebuild_shards(shards, want):
    """{idx: (hdr, share, data)} (k of them) -> {idx: shard file bytes} for the lost indices in ``want``; the very
    bytes first made (hashes in the signed header match), and the key is never put together."""
    hdr = next(iter(shards.values()))[0]
    k, n = int(hdr["k"]), int(hdr["n"])
    pick = sorted(shards)[:k]
    if len(pick) < k:
        raise VaultError(f"{len(pick)} shards at hand, {k} needed to repair")
    datas = rs.rebuild({i: shards[i][2] for i in pick}, k, n, want)
    sig = shards[pick[0]][3] if len(shards[pick[0]]) > 3 else None
    out = {}
    for i in want:
        share = shamir.extend([(j + 1, shards[j][1]) for j in pick], i + 1)
        if shard_digest(share, datas[i]) != hdr["h"][i]:
            raise VaultError("a repaired shard does not match its signed hash")
        out[i] = pack_shard(hdr, sig, i, share, datas[i])
    return out


# ---- the vault on one computer ------------------------------------------------------------------------------------
def vault_root():
    from qgis.core import QgsApplication

    return os.path.join(QgsApplication.qgisSettingsDirPath(), "biruni", "team_vault")


class Vault:
    """This computer's side as an owner (own/: chunks, snapshots, packs and where their shards are) and as a holder
    (held/: other people's shards, which it cannot read)."""

    def __init__(self, root=None, seed=None):
        self.root = root or vault_root()
        self.seed = seed if seed is not None else default_seed()
        self.pub = _crypto().public_key(self.seed).hex()
        own = os.path.join(self.root, "own")
        os.makedirs(os.path.join(own, "out"), exist_ok=True)
        os.makedirs(os.path.join(self.root, "held"), exist_ok=True)
        self.chunks = store.ChunkStore(own)
        self._lock = threading.RLock()
        self.db = sqlite3.connect(os.path.join(own, "vault.sqlite"), check_same_thread=False)
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS snap(id TEXT PRIMARY KEY, label TEXT, ts REAL, pack TEXT, files TEXT,
                total INTEGER, new INTEGER, nfiles INTEGER);
            CREATE TABLE IF NOT EXISTS pack(id TEXT PRIMARY KEY, hdr TEXT, sig TEXT, k INTEGER, n INTEGER, ss INTEGER,
                deps TEXT, ts REAL);
            CREATE TABLE IF NOT EXISTS sent(cid TEXT PRIMARY KEY, pack TEXT, off INTEGER, len INTEGER);
            CREATE TABLE IF NOT EXISTS place(pack TEXT, idx INTEGER, member TEXT, name TEXT, ts REAL,
                PRIMARY KEY(pack, idx, member));
        """)
        self.db.commit()
        self.quota = 2 << 30
        self.drop_grace = DROP_GRACE

    def close(self):
        self.db.close()
        self.chunks.close()

    # ---- owner: taking a snapshot -----------------------------------------------------------------------------
    def snapshot(self, paths, label="", k=DEFAULT_K, n=DEFAULT_N, password=None, progress=None):
        """Files and folders -> a snapshot and its pack (n shard files waiting in own/out/<pack>/ until placed).
        -> {"snap", "pack", "files", "total", "new", "stored", "dedup", "shards"}"""
        if not (1 <= k <= n <= rs.MAX_N):
            raise VaultError("need 1 <= k <= n")
        files = store.walk(paths)
        if not files:
            raise VaultError("nothing to keep: no file")
        with self._lock:
            pack_id = secrets.token_hex(8)
            snap_id = secrets.token_hex(8)
            where, new_parts, new_index, fl = {}, [], {}, []
            off = total = new = 0
            done_bytes, all_bytes = 0, sum(os.path.getsize(f) for f, _ in files)
            for full, rel in files:
                h = hashlib.sha256()
                ids = []
                st = os.stat(full)
                for cid, data in store.chunk_file(full):
                    h.update(data)
                    ids.append(cid)
                    total += len(data)
                    self.chunks.put(cid, data)
                    if cid in where or cid in new_index:
                        pass
                    else:
                        row = self.db.execute("SELECT pack, off, len FROM sent WHERE cid=?", (cid,)).fetchone()
                        if row:
                            where[cid] = list(row)
                        else:
                            new_index[cid] = [pack_id, off, len(data)]
                            new_parts.append(data)
                            off += len(data)
                            new += len(data)
                    done_bytes += len(data)
                    if progress:
                        progress(done_bytes, all_bytes)
                fl.append({"p": rel, "size": st.st_size, "mtime": st.st_mtime, "sha256": h.hexdigest(), "c": ids})
            where.update(new_index)
            deps = sorted({v[0] for v in where.values()} - {pack_id})
            manifest = {"v": 1, "pack": pack_id, "snap": {"id": snap_id, "label": str(label)[:200], "ts": time.time(),
                                                          "files": fl},
                        "where": where, "deps": deps}
            mj = canonical(manifest)
            plain = struct.pack(">I", len(mj)) + mj + b"".join(new_parts)
            secret = os.urandom(32)
            hdr = {"v": 1, "pack": pack_id, "owner": self.pub, "k": int(k), "n": int(n), "ts": round(time.time(), 3),
                   "deps": deps}
            if password:
                hdr["kdf"] = _crypto().new_kdf()
            key = _pack_key(secret, pack_id, hdr, password)
            ct = _crypto().seal(key, plain, pack_id.encode("ascii"), compress=True)
            datas = rs.encode(ct, k, n)
            shares = shamir.split(secret, k, n)
            hdr["size"], hdr["ss"] = len(ct), len(datas[0])
            hdr["h"] = [shard_digest(shares[i][1], datas[i]) for i in range(n)]
            sig = sign_header(self.seed, hdr)
            mdir = os.path.join(self.root, "own", "manifests")
            os.makedirs(mdir, exist_ok=True)
            with open(os.path.join(mdir, snap_id + ".json"), "w", encoding="utf-8") as f:
                json.dump(manifest, f)
            folder = os.path.join(self.root, "own", "out", pack_id)
            os.makedirs(folder, exist_ok=True)
            for i in range(n):
                with open(os.path.join(folder, f"{i}.bvs"), "wb") as f:
                    f.write(pack_shard(hdr, sig, i, shares[i][1], datas[i]))
            self.db.execute("INSERT INTO pack VALUES(?,?,?,?,?,?,?,?)", (pack_id, json.dumps(hdr), sig, k, n,
                                                                          hdr["ss"], json.dumps(deps), time.time()))
            for cid, (p, o, ln) in new_index.items():
                self.db.execute("INSERT OR IGNORE INTO sent VALUES(?,?,?,?)", (cid, p, o, ln))
            self.db.execute("INSERT INTO snap VALUES(?,?,?,?,?,?,?,?)",
                            (snap_id, str(label)[:200], manifest["snap"]["ts"], pack_id,
                             json.dumps([[x["p"], x["size"]] for x in fl]), total, new, len(fl)))
            self.db.commit()
            st = self.chunks.stats()
            return {"snap": snap_id, "pack": pack_id, "files": len(fl), "total": total, "new": new,
                    "ct": len(ct), "shard": hdr["ss"], "k": k, "n": n,
                    "dedup": (total / new) if new else float("inf"), "stored": st["stored"],
                    "store_ratio": (st["bytes"] / st["stored"]) if st["stored"] else 1.0}

    def snapshots(self):
        rows = self.db.execute("SELECT id, label, ts, pack, total, new, nfiles FROM snap ORDER BY ts DESC").fetchall()
        return [{"id": r[0], "label": r[1], "ts": r[2], "pack": r[3], "total": r[4], "new": r[5], "files": r[6]}
                for r in rows]

    def packs_of(self, snap_id):
        """The packs a snapshot needs: its own and the ones holding chunks it shares with older snapshots."""
        row = self.db.execute("SELECT pack FROM snap WHERE id=?", (snap_id,)).fetchone()
        if not row:
            raise VaultError("no such snapshot")
        p = self.db.execute("SELECT deps FROM pack WHERE id=?", (row[0],)).fetchone()
        return [row[0]] + (json.loads(p[0]) if p else [])

    def pack_info(self, pack_id):
        r = self.db.execute("SELECT hdr, sig, k, n, ss FROM pack WHERE id=?", (pack_id,)).fetchone()
        if not r:
            return None
        return {"hdr": json.loads(r[0]), "sig": r[1], "k": r[2], "n": r[3], "ss": r[4]}

    def outbox(self):
        """{pack: {idx: path}} of shard files not placed yet."""
        out = {}
        base = os.path.join(self.root, "own", "out")
        for pack in sorted(os.listdir(base)):
            d = os.path.join(base, pack)
            if os.path.isdir(d):
                items = {int(f[:-4]): os.path.join(d, f) for f in os.listdir(d) if f.endswith(".bvs")}
                if items:
                    out[pack] = items
        return out

    def placed(self, pack, idx, member, name=""):
        """A holder confirmed it keeps shard idx of pack: remembered, and the waiting file goes."""
        with self._lock:
            self.db.execute("INSERT OR REPLACE INTO place VALUES(?,?,?,?,?)", (pack, int(idx), member, name, time.time()))
            self.db.commit()
            p = os.path.join(self.root, "own", "out", pack, f"{int(idx)}.bvs")
            if os.path.isfile(p):
                os.remove(p)
                try:
                    os.rmdir(os.path.dirname(p))
                except OSError:
                    pass

    def placements(self, pack=None):
        q = "SELECT pack, idx, member, name, ts FROM place" + (" WHERE pack=?" if pack else "")
        return [dict(zip(("pack", "idx", "member", "name", "ts"), r)) for r in
                self.db.execute(q, (pack,) if pack else ())]

    def remove_snapshot(self, snap_id):
        """Forget a snapshot. -> the packs no other snapshot needs (to withdraw from the team); a pack whose chunks a
        later snapshot reuses stays, and its chunks are no longer offered to new snapshots once it goes."""
        with self._lock:
            row = self.db.execute("SELECT pack FROM snap WHERE id=?", (snap_id,)).fetchone()
            if not row:
                raise VaultError("no such snapshot")
            mine = set(self.packs_of(snap_id))
            self.db.execute("DELETE FROM snap WHERE id=?", (snap_id,))
            still = set()
            for (sid,) in self.db.execute("SELECT id FROM snap").fetchall():
                still.update(self.packs_of(sid))
            gone = sorted(mine - still)
            for p in gone:
                for t in ("pack WHERE id=?", "sent WHERE pack=?", "place WHERE pack=?"):
                    self.db.execute("DELETE FROM " + t, (p,))
                shutil_rm(os.path.join(self.root, "own", "out", p))
            self.db.commit()
            try:
                os.remove(os.path.join(self.root, "own", "manifests", snap_id + ".json"))
            except OSError:
                pass
            return gone

    def adopt_pack(self, hdr, sig):
        """A pack of this owner known only from the team (a new computer after a recovery kit): remembered."""
        if hdr.get("owner") != self.pub:
            raise VaultError("a pack of another owner")
        check_header(hdr, sig, self.pub)
        with self._lock:
            self.db.execute("INSERT OR IGNORE INTO pack VALUES(?,?,?,?,?,?,?,?)",
                            (hdr["pack"], json.dumps(hdr), sig, int(hdr["k"]), int(hdr["n"]), int(hdr["ss"]),
                             json.dumps(hdr.get("deps", [])), time.time()))
            self.db.commit()

    def adopt(self, found, password=None):
        """Remember the packs and snapshots found in gathered shards (they open with the password). -> snapshots"""
        n = 0
        for pack, shards in found.items():
            e = next(iter(shards.values()))
            hdr, sig = e[0], e[3]
            if hdr.get("owner") != self.pub:
                continue
            self.adopt_pack(hdr, sig)
            if len(shards) < int(hdr["k"]):
                continue
            try:
                _h, man, _p = open_pack(shards, password)
            except VaultError:
                continue
            s = man["snap"]
            mdir = os.path.join(self.root, "own", "manifests")
            os.makedirs(mdir, exist_ok=True)
            with open(os.path.join(mdir, s["id"] + ".json"), "w", encoding="utf-8") as f:
                json.dump(man, f)
            total = sum(f["size"] for f in s["files"])
            new = sum(v[2] for v in man["where"].values() if v[0] == pack)
            with self._lock:
                cur = self.db.execute("INSERT OR IGNORE INTO snap VALUES(?,?,?,?,?,?,?,?)",
                                      (s["id"], s.get("label", ""), s.get("ts") or 0, pack,
                                       json.dumps([[x["p"], x["size"]] for x in s["files"]]), total, new,
                                       len(s["files"])))
                for cid, (p, o, ln) in man["where"].items():
                    self.db.execute("INSERT OR IGNORE INTO sent VALUES(?,?,?,?)", (cid, p, o, ln))
                self.db.commit()
            n += cur.rowcount
        return n

    def forget(self, member, pack=None):
        with self._lock:
            if pack:
                self.db.execute("DELETE FROM place WHERE member=? AND pack=?", (member, pack))
            else:
                self.db.execute("DELETE FROM place WHERE member=?", (member,))
            self.db.commit()

    # ---- restore ----------------------------------------------------------------------------------------------
    def restore_local(self, snap_id, dest):
        """From this computer's own chunk store (the fast way, when this PC is fine)."""
        row = self.db.execute("SELECT pack FROM snap WHERE id=?", (snap_id,)).fetchone()
        if not row:
            raise VaultError("no such snapshot")
        man = self._own_manifest(snap_id)
        return write_files(man, lambda cid: self.chunks.get(cid), dest)

    def _own_manifest(self, snap_id):
        p = os.path.join(self.root, "own", "manifests", snap_id + ".json")
        with open(p, encoding="utf-8") as f:
            return json.load(f)

    # ---- holder: other people's shards -------------------------------------------------------------------------
    def held_dir(self):
        return os.path.join(self.root, "held")

    def held(self):
        """{pack: {"owner", "idx": [..], "bytes", "ts", "k", "n"}} of the shards this computer keeps for others."""
        out = {}
        base = self.held_dir()
        for pack in os.listdir(base):
            d = os.path.join(base, pack)
            if not os.path.isdir(d):
                continue
            hp = os.path.join(d, "hdr.json")
            try:
                with open(hp, encoding="utf-8") as f:
                    hdr = json.load(f)["hdr"]
            except (OSError, ValueError, KeyError):
                continue
            idx = sorted(int(f[:-4]) for f in os.listdir(d) if f.endswith(".bvs"))
            purge = None
            try:
                with open(os.path.join(d, "drop.json"), encoding="utf-8") as f:
                    purge = float(json.load(f)["purge"])
            except (OSError, ValueError, KeyError, TypeError):
                pass
            if idx:
                out[pack] = {"owner": hdr["owner"], "idx": idx, "k": hdr["k"], "n": hdr["n"], "ts": hdr.get("ts"),
                             "bytes": sum(os.path.getsize(os.path.join(d, f"{i}.bvs")) for i in idx), "purge": purge}
        return out

    def held_bytes(self):
        return sum(v["bytes"] for v in self.held().values())

    def keep_shard(self, blob, from_self=False):
        """A shard someone placed here: checked, then kept. -> (pack, idx); VaultError when refused."""
        hdr, idx, _share, _data = parse_shard(blob)
        sig = json.loads(blob[8:8 + struct.unpack(">I", bytes(blob[4:8]))[0]].decode("utf-8"))["sig"]
        if self.held_bytes() + len(blob) > self.quota:
            raise VaultError("no room left in the space this computer gives the team")
        d = os.path.join(self.held_dir(), hdr["pack"])
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "hdr.json"), "w", encoding="utf-8") as f:
            json.dump({"hdr": hdr, "sig": sig}, f)
        tmp = os.path.join(d, f"{idx}.bvs.tmp")
        with open(tmp, "wb") as f:
            f.write(bytes(blob))
        os.replace(tmp, os.path.join(d, f"{idx}.bvs"))
        return hdr["pack"], idx

    def held_shard(self, pack, idx):
        with open(os.path.join(self.held_dir(), pack, f"{int(idx)}.bvs"), "rb") as f:
            return f.read()

    # A signed drop never deletes at once: the shards are kept DROP_GRACE more days and only then removed, so a
    # stolen or ransomed owner's key cannot wipe the team's copies in one stroke - the owner (or anyone looking at
    # Health) sees "to be deleted on …" and can cancel with a signed keep. Restoring still works meanwhile.
    def drop_held(self, owner, packs, now=None):
        now = time.time() if now is None else now
        n = 0
        for pack in packs:
            d = os.path.join(self.held_dir(), str(pack))
            info = self.held().get(str(pack))
            if info and info["owner"] == owner:
                p = os.path.join(d, "drop.json")
                if not os.path.isfile(p):
                    with open(p, "w", encoding="utf-8") as f:
                        json.dump({"at": now, "purge": now + self.drop_grace}, f)
                n += 1
        return n

    def keep_held(self, owner, packs):
        """Cancel a pending drop (a signed keep from the owner). -> packs kept"""
        n = 0
        for pack in packs:
            info = self.held().get(str(pack))
            p = os.path.join(self.held_dir(), str(pack), "drop.json")
            if info and info["owner"] == owner and os.path.isfile(p):
                os.remove(p)
                n += 1
        return n

    def purge_dropped(self, now=None):
        """Delete the shards whose grace has run out. -> the packs removed"""
        now = time.time() if now is None else now
        gone = []
        for pack, info in self.held().items():
            if info.get("purge") is not None and info["purge"] <= now:
                d = os.path.join(self.held_dir(), pack)
                for fn in os.listdir(d):
                    os.remove(os.path.join(d, fn))
                os.rmdir(d)
                gone.append(pack)
        return gone

    # ---- signed requests --------------------------------------------------------------------------------------
    def signed(self, kind, body):
        b = dict(body, kind=kind, owner=self.pub, ts=time.time(), nonce=secrets.token_hex(8))
        return {"b": b, "sig": b64u(_crypto().sign(self.seed, canonical(b)))}

    # ---- export to folders (no room) -------------------------------------------------------------------------
    def export_folders(self, snap_id, folders, shard_files):
        """Write the shards of a snapshot's packs into n folders (USB sticks): folder j gets shard j of each pack.
        ``shard_files``: {pack: {idx: bytes}} (all n of each). -> number of files written"""
        if not folders:
            raise VaultError("no folder")
        count = 0
        for pack, by_idx in shard_files.items():
            n = len(by_idx)
            for i in range(n):
                folder = folders[i % len(folders)]
                d = os.path.join(folder, "BiruniVault", pack)
                os.makedirs(d, exist_ok=True)
                with open(os.path.join(d, f"{i}.bvs"), "wb") as f:
                    f.write(by_idx[i])
                count += 1
        return count


def shutil_rm(path):
    import shutil

    shutil.rmtree(path, ignore_errors=True)


# ---- restoring from shards (any computer) -------------------------------------------------------------------------
def scan_folders(folders, owner=None):
    """{pack: {idx: (hdr, share, data, sig)}} of every valid shard file under the folders (bad ones are skipped
    and counted). -> (shards, bad)"""
    found, bad = {}, 0
    for folder in folders:
        for d, _dirs, files in os.walk(folder):
            for fn in files:
                if not fn.endswith(".bvs"):
                    continue
                try:
                    with open(os.path.join(d, fn), "rb") as f:
                        blob = f.read()
                    add_shard(found, blob, owner)
                except (OSError, VaultError):
                    bad += 1
    return found, bad


def add_shard(found, blob, owner=None):
    hdr, idx, share, data = parse_shard(blob, owner)
    ln = struct.unpack(">I", bytes(blob[4:8]))[0]
    sig = json.loads(bytes(blob[8:8 + ln]).decode("utf-8"))["sig"]
    found.setdefault(hdr["pack"], {})[idx] = (hdr, share, data, sig)
    return hdr["pack"], idx


def snapshots_in(found, password=None):
    """The snapshots that can be opened from gathered shards -> [{"id", "label", "ts", "pack", "files", "total"}]
    (packs with fewer than k shards are listed by restore_from as missing)."""
    out = []
    for pack, shards in found.items():
        hdr = next(iter(shards.values()))[0]
        if len(shards) < int(hdr["k"]):
            continue
        _h, man, _payload = open_pack(shards, password)
        s = man["snap"]
        out.append({"id": s["id"], "label": s.get("label", ""), "ts": s.get("ts"), "pack": pack,
                    "files": len(s["files"]), "total": sum(f["size"] for f in s["files"]), "owner": hdr["owner"]})
    return sorted(out, key=lambda x: -(x["ts"] or 0))


def restore_from(found, pack_id, dest, password=None):
    """Write a snapshot (its pack ``pack_id``) out of gathered shards into ``dest``. Every chunk is checked by its
    hash and every file by its SHA-256. -> {"files", "bytes"}"""
    if pack_id not in found:
        raise VaultError("no shard of this snapshot is at hand")
    hdr, man, payload = open_pack(found[pack_id], password)
    opened = {pack_id: (man, payload)}
    for dep in man.get("deps", []):
        if dep not in found:
            raise VaultError("an older snapshot this one builds on is not at hand (" + dep + ")")
        dh, dm, dp = open_pack(found[dep], password)
        if dh["owner"] != hdr["owner"]:
            raise VaultError("a pack of another owner")
        opened[dep] = (dm, dp)
    where = man["where"]

    def chunk(cid):
        p, off, ln = where[cid]
        data = bytes(opened[p][1][off:off + ln])
        if store.chunk_id(data) != cid:
            raise VaultError("a chunk does not match its hash")
        return data

    return write_files(man, chunk, dest)


def write_files(man, chunk, dest):
    nfiles = nbytes = 0
    for f in man["snap"]["files"]:
        out = os.path.join(dest, store.safe_rel(f["p"]))
        os.makedirs(os.path.dirname(out), exist_ok=True)
        h = hashlib.sha256()
        tmp = out + ".part"
        with open(tmp, "wb") as fo:
            for cid in f["c"]:
                data = chunk(cid)
                h.update(data)
                fo.write(data)
        if h.hexdigest() != f["sha256"]:
            os.remove(tmp)
            raise VaultError("a restored file does not match: " + f["p"])
        os.replace(tmp, out)
        try:
            os.utime(out, (f["mtime"], f["mtime"]))
        except OSError:
            pass
        nfiles += 1
        nbytes += f["size"]
    return {"files": nfiles, "bytes": nbytes}


# ---- health -------------------------------------------------------------------------------------------------------
def survives(holders, k):
    """holders: {member: number of shards of one pack it keeps} -> how many computers may be lost (the worst ones)
    with the pack still readable; -1 when it is already short."""
    counts = sorted(holders.values(), reverse=True)
    left = sum(counts)
    if left < k:
        return -1
    lost = 0
    for c in counts:
        if left - c < k:
            break
        left -= c
        lost += 1
    return lost
