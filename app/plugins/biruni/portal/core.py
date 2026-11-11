"""A signed portal with no server (SPEC.md): a folder that any static host can serve - GitHub Pages, a shared folder
on the office network, a USB stick - and that a browser can check on its own.

    <portal>/portal.json         the signed index: owner, groups (with their key envelopes), items, followed portals
    <portal>/portal.js           the same, as a script (a page opened from file:// cannot fetch() a file)
    <portal>/index.html          the viewer (page.py)
    <portal>/items/<id>/...      each item's file: as it is (public) or sealed (shared with groups), + thumb.png

Only the owner (this Windows user's portal key) can change a portal: every change re-signs the index, and every item
manifest carries its own signature so it can be copied, followed and checked alone.
"""
import json
import os
import re
import secrets
import shutil
import time

from . import crypto as C

KEY = "biruni/portal/key"
INDEX = "portal.json"
SCRIPT = "portal.js"
ITEM_TYPES = ("file", "dashboard", "mapservice", "link")
MAX_FILE = 512 * 1024 * 1024
_TEST_SEED = []              # tests: a fixed key instead of the user's


class PortalError(Exception):
    pass


# ---- this user's portal key ----------------------------------------------------------------------------------------------
def my_seed():
    """This Windows user's portal signing key (32 bytes), kept encrypted in the settings like the dashboard key."""
    if _TEST_SEED:
        return _TEST_SEED[0]
    from qgis.core import QgsSettings

    from ..field_photos.tunnel import protect, unprotect

    s = QgsSettings()
    try:
        seed = bytes.fromhex(unprotect(s.value(KEY, "") or "") or "")
        assert len(seed) == 32
    except Exception:  # noqa: BLE001 - the first time
        seed = secrets.token_bytes(32)
        s.setValue(KEY, protect(seed.hex()))
    return seed


def box_priv_of(seed):
    """The X25519 key of a portal key: the owner is a member of each of their groups, and the same key is this
    user's member key when they view someone else's portal."""
    return C._hkdf(seed, b"biruni-portal", b"owner-box")


def my_card(name, seed=None):
    return C.member_card(name, C.box_public(box_priv_of(seed or my_seed())))


def my_key_file(name, passphrase=None, seed=None):
    return C.key_file(name, box_priv_of(seed or my_seed()), passphrase)


def new_id(prefix):
    return prefix + "_" + secrets.token_hex(5)


def _now():
    return int(time.time())


def _safe_ext(name):
    ext = os.path.splitext(str(name or ""))[1].lower()
    return ext if re.fullmatch(r"\.[a-z0-9]{1,8}", ext or "") else ""


def is_expired(manifest, now=None):
    exp = manifest.get("expires")
    return bool(exp) and (now or time.time()) >= int(exp)


# ---- reading and checking a portal (anyone) ---------------------------------------------------------------------------
def read_doc(folder_or_bytes):
    if isinstance(folder_or_bytes, (bytes, bytearray)):
        return json.loads(bytes(folder_or_bytes).decode("utf-8"))
    with open(os.path.join(folder_or_bytes, INDEX), encoding="utf-8") as f:
        return json.load(f)


def verify_doc(doc, pinned=None, now=None):
    """Check a portal.json document. -> {"ok", "why", "index", "owner", "print", "pinned", "items": [...]}

    ok: the index is signed by the key it names (and by ``pinned`` when given). Each item is checked too: its
    manifest must be signed by the same owner and belong to this portal; a bad one is kept with ok=False (shown as
    untrusted, never as trusted)."""
    rep = {"ok": False, "why": "", "index": None, "owner": None, "print": "", "pinned": None, "items": []}
    try:
        text = doc["index"]
        idx = json.loads(text)
        pub = idx["owner"]["pub"]
    except Exception:  # noqa: BLE001
        rep["why"] = "not a portal"
        return rep
    rep["index"], rep["owner"], rep["print"] = idx, idx.get("owner"), C.key_print(pub)
    if not C.verify_text(pub, doc.get("sig", ""), text):
        rep["why"] = "bad signature"
    elif pinned and pinned.lower() != pub.lower():
        rep["why"], rep["pinned"] = "not the pinned key", False
    else:
        rep["ok"] = True
        rep["pinned"] = True if pinned else None
    for it in idx.get("items", []):
        r = {"ok": False, "why": "", "manifest": None, "entry": it}
        try:
            m = json.loads(it["m"])
            r["manifest"] = m
            if not C.verify_text(pub, it.get("s", ""), it["m"]):
                r["why"] = "bad signature"
            elif m.get("owner") != pub or m.get("portal") != idx.get("id"):
                r["why"] = "foreign item"
            else:
                r["ok"] = rep["ok"]
                r["why"] = "" if rep["ok"] else rep["why"]
                if r["ok"] and is_expired(m, now):
                    r["expired"] = True
        except Exception:  # noqa: BLE001
            r["why"] = "not an item"
        rep["items"].append(r)
    return rep


def portal_url(url):
    url = str(url or "").strip()
    return url if url.lower().endswith(".json") else url.rstrip("/") + "/" + INDEX


def aggregate(index, fetch, now=None):
    """The followed portals of ``index``, each fetched (``fetch(url) -> bytes``) and checked against its pinned key.
    -> [{"follow", "status": ok|bad signature|not the pinned key|unreachable|not a portal, "report"}]"""
    out = []
    for f in index.get("follows", []):
        row = {"follow": f, "status": "unreachable", "report": None}
        try:
            data = fetch(portal_url(f["url"]))
        except Exception:  # noqa: BLE001 - offline, moved, refused
            out.append(row)
            continue
        try:
            rep = verify_doc(read_doc(data), pinned=f.get("pub"), now=now)
        except Exception:  # noqa: BLE001
            row["status"] = "not a portal"
            out.append(row)
            continue
        row["report"] = rep
        row["status"] = "ok" if rep["ok"] else rep["why"]
        out.append(row)
    return out


def open_item(folder, index, item_id, priv):
    """A reader with a member key (``priv``) opens a sealed item of a portal folder. -> bytes"""
    entry = next((it for it in index.get("items", []) if json.loads(it["m"])["id"] == item_id), None)
    if entry is None:
        raise PortalError("no such item")
    m = json.loads(entry["m"])
    if not m["file"]:
        raise PortalError("not a file")
    with open(os.path.join(folder, m["file"]["path"]), "rb") as f:
        blob = f.read()
    if C.sha256_hex(blob) != m["file"]["sha256"]:
        raise PortalError("the file was altered")
    if not m["file"]["enc"]:
        return blob
    me = C.box_public(priv).hex()
    for g in index.get("groups", []):
        if g["id"] not in entry.get("keys", {}):
            continue
        mem = next((x for x in g["members"] if x["box"] == me), None)
        if not mem:
            continue
        gk = C.box_open(priv, mem["env"], f"group:{index['id']}:{g['id']}:{g['ver']}")
        w = entry["keys"][g["id"]]
        cek = C.unseal(gk, C.unb64u(w["w"]), f"cek:{m['id']}:{g['id']}:{w['v']}")
        return C.unseal(cek, blob, f"item:{m['id']}")
    raise PortalError("not a member")


# ---- the owner's side ---------------------------------------------------------------------------------------------------
class Portal:
    """A portal folder opened by its owner (``seed`` = the portal key; this user's by default)."""

    def __init__(self, folder, index, seed):
        self.folder = folder
        self.index = index
        self.seed = seed
        self.pub = C.ed_public(seed).hex()
        self._box = box_priv_of(seed)

    # -- life ----------------------------------------------------------------------------------------------------------
    @classmethod
    def create(cls, folder, name, owner_name="", about="", seed=None):
        seed = seed or my_seed()
        if os.path.exists(os.path.join(folder, INDEX)):
            raise PortalError("a portal already exists in this folder")
        os.makedirs(os.path.join(folder, "items"), exist_ok=True)
        idx = {"v": 1, "kind": "biruni-portal", "id": new_id("p"), "name": str(name or "")[:120],
               "about": str(about or "")[:2000],
               "owner": {"name": str(owner_name or "")[:80], "pub": C.ed_public(seed).hex(),
                         "box": C.box_public(box_priv_of(seed)).hex()},
               "created": _now(), "updated": _now(), "seq": 0, "groups": [], "items": [], "follows": []}
        p = cls(folder, idx, seed)
        p.save()
        return p

    @classmethod
    def open(cls, folder, seed=None):
        seed = seed or my_seed()
        rep = verify_doc(read_doc(folder))
        if not rep["ok"]:
            raise PortalError(rep["why"] or "not a portal")
        if rep["index"]["owner"]["pub"] != C.ed_public(seed).hex():
            raise PortalError("this portal belongs to another key")
        return cls(folder, rep["index"], seed)

    def save(self):
        """Re-sign the index and write portal.json, portal.js and the viewer."""
        from . import page

        self.index["seq"] = int(self.index.get("seq", 0)) + 1
        self.index["updated"] = _now()
        text = C.text_of(self.index)
        doc = {"v": 1, "index": text, "sig": C.sign_text(self.seed, text)}
        body = json.dumps(doc, ensure_ascii=False)
        tmp = os.path.join(self.folder, INDEX + ".tmp")
        with open(tmp, "w", encoding="utf-8", newline="\n") as f:
            f.write(body)
        os.replace(tmp, os.path.join(self.folder, INDEX))
        with open(os.path.join(self.folder, SCRIPT), "w", encoding="utf-8", newline="\n") as f:
            f.write("window.BIRUNI_PORTAL=" + body.replace("</", "<\\/") + ";\n")
        page.write_viewer(self.folder)
        return doc

    def publish(self, dest):
        """Copy the portal (as it is: signed, sealed) to another folder - a gh-pages worktree, a share, a stick."""
        if os.path.abspath(dest) == os.path.abspath(self.folder):
            return dest
        shutil.copytree(self.folder, dest, dirs_exist_ok=True,
                        ignore=shutil.ignore_patterns("*.tmp"))
        # items removed here are removed there too
        keep = {self._m(it)["id"] for it in self.index["items"]}
        items = os.path.join(dest, "items")
        for d in os.listdir(items) if os.path.isdir(items) else []:
            if d not in keep:
                shutil.rmtree(os.path.join(items, d), ignore_errors=True)
        return dest

    # -- items ---------------------------------------------------------------------------------------------------------
    @staticmethod
    def _m(entry):
        return json.loads(entry["m"])

    def items(self):
        return [self._m(it) for it in self.index["items"]]

    def entry(self, item_id):
        for it in self.index["items"]:
            if self._m(it)["id"] == item_id:
                return it
        raise PortalError("no such item")

    def _sign_manifest(self, m):
        text = C.text_of(m)
        return {"m": text, "s": C.sign_text(self.seed, text)}

    def _manifest(self, kind, title, tags, groups, expires_days, scope, rotate, desc, url=None):
        if kind not in ITEM_TYPES:
            raise PortalError("unknown item type")
        for g in groups or ():
            self.group(g)
        now = _now()
        exp = now + int(round(float(expires_days) * 86400)) if expires_days else None
        return {"v": 1, "portal": self.index["id"], "id": new_id("i"), "type": kind, "title": str(title or "")[:200],
                "desc": str(desc or "")[:4000], "tags": sorted({str(t).strip()[:40] for t in tags or () if str(t).strip()}),
                "owner": self.pub, "created": now, "expires": exp, "scope": str(scope or "")[:400],
                "groups": sorted(set(groups or ())), "file": None, "url": url, "thumb": None, "rotate": bool(rotate)}

    def add_file(self, path=None, title=None, tags=(), groups=(), expires_days=None, scope="", rotate=False, desc="",
                 data=None, name=None, thumb_png=None, kind="file", mime=""):
        """Add a file (``path``, or ``data`` + ``name``). Shared with ``groups`` it is sealed; with none it is public."""
        if data is None:
            if os.path.getsize(path) > MAX_FILE:
                raise PortalError("the file is too large for a portal")
            with open(path, "rb") as f:
                data = f.read()
            name = name or os.path.basename(path)
        m = self._manifest(kind, title or name, tags, groups, expires_days, scope, rotate, desc)
        d = os.path.join(self.folder, "items", m["id"])
        os.makedirs(d, exist_ok=True)
        keys = {}
        if m["groups"]:
            cek = C.new_key()
            blob = C.seal(cek, data, f"item:{m['id']}")
            rel = f"items/{m['id']}/data.bin"
            for g in m["groups"]:
                keys[g] = self._wrap(cek, m["id"], g)
        else:
            blob = bytes(data)
            rel = f"items/{m['id']}/file{_safe_ext(name)}"
            if thumb_png:
                with open(os.path.join(d, "thumb.png"), "wb") as f:
                    f.write(thumb_png)
                m["thumb"] = f"items/{m['id']}/thumb.png"
        with open(os.path.join(self.folder, rel), "wb") as f:
            f.write(blob)
        m["file"] = {"name": str(name or "")[:200], "size": len(data), "sha256": C.sha256_hex(blob),
                     "enc": bool(m["groups"]), "path": rel, "mime": str(mime or "")[:80]}
        e = self._sign_manifest(m)
        e["keys"] = keys
        self.index["items"].append(e)
        self.save()
        return m

    def add_link(self, url, title, kind="link", tags=(), groups=(), expires_days=None, scope="", rotate=False, desc=""):
        """A link: a dashboard card link, a map-service folder (relative path or URL), any web page. A link shared
        with groups has its address sealed like a file."""
        url = str(url or "").strip()
        if not url:
            raise PortalError("no address")
        if kind == "file":
            kind = "link"
        if groups:
            return self.add_file(data=url.encode("utf-8"), name="link.url", title=title, tags=tags, groups=groups,
                                 expires_days=expires_days, scope=scope, rotate=rotate, desc=desc, kind=kind,
                                 mime="text/uri-list")
        m = self._manifest(kind, title or url, tags, groups, expires_days, scope, rotate, desc, url=url)
        e = self._sign_manifest(m)
        e["keys"] = {}
        self.index["items"].append(e)
        self.save()
        return m

    def remove_item(self, item_id, save=True):
        e = self.entry(item_id)
        self.index["items"].remove(e)
        shutil.rmtree(os.path.join(self.folder, "items", item_id), ignore_errors=True)
        if save:
            self.save()

    def read(self, item_id):
        """The owner opens any of their items."""
        return open_item(self.folder, self.index, item_id, self._box)

    # -- groups ----------------------------------------------------------------------------------------------------------
    def group(self, gid):
        for g in self.index["groups"]:
            if g["id"] == gid:
                return g
        raise PortalError("no such group")

    def _aad(self, g):
        return f"group:{self.index['id']}:{g['id']}:{g['ver']}"

    def group_key(self, gid):
        g = self.group(gid)
        me = C.box_public(self._box).hex()
        mem = next(x for x in g["members"] if x["box"] == me)
        return C.box_open(self._box, mem["env"], self._aad(g))

    def _wrap(self, cek, item_id, gid):
        g = self.group(gid)
        return {"v": g["ver"], "w": C.b64u(C.seal(self.group_key(gid), cek, f"cek:{item_id}:{gid}:{g['ver']}"))}

    def _unwrap(self, entry, gid):
        g = self.group(gid)
        w = entry["keys"][gid]
        return C.unseal(self.group_key(gid), C.unb64u(w["w"]), f"cek:{self._m(entry)['id']}:{gid}:{w['v']}")

    def add_group(self, name):
        g = {"id": new_id("g"), "name": str(name or "")[:80], "ver": 1, "members": []}
        gk = C.new_key()
        self.index["groups"].append(g)
        g["members"].append({"name": self.index["owner"]["name"], "box": self.index["owner"]["box"], "owner": True,
                             "env": C.box_to(self.index["owner"]["box"], gk, self._aad(g))})
        self.save()
        return g["id"]

    def remove_group(self, gid):
        """A group goes; its items that belong to no other group go with it (nobody could open them)."""
        g = self.group(gid)
        for e in list(self.index["items"]):
            m = self._m(e)
            if gid in m["groups"]:
                if len(m["groups"]) == 1:
                    self.remove_item(m["id"], save=False)
                else:
                    e["keys"].pop(gid, None)
        self.index["groups"].remove(g)
        self.save()

    def add_member(self, gid, card=None, name=None, box=None):
        """Add a member by their card (BPM1...) or their name + X25519 public key (hex): one envelope."""
        if card:
            c = C.parse_card(card)
            name, box = name or c["name"], c["box"]
        box = str(box).lower()
        if len(bytes.fromhex(box)) != 32:
            raise PortalError("not a member key")
        g = self.group(gid)
        if any(x["box"] == box for x in g["members"]):
            return False
        g["members"].append({"name": str(name or "")[:80], "box": box,
                             "env": C.box_to(box, self.group_key(gid), self._aad(g))})
        self.save()
        return True

    def remove_member(self, gid, box, reencrypt=True):
        """Remove a member: the group key is replaced (and, by default, each of the group's files is sealed again with
        a new content key), so nothing published from now on opens with what they had. What they already downloaded
        stays with them - no system can take that back."""
        g = self.group(gid)
        mem = next((x for x in g["members"] if x["box"] == str(box).lower()), None)
        if mem is None or mem.get("owner"):
            return False
        g["members"].remove(mem)
        self.rotate_group(gid, reencrypt=reencrypt)
        return True

    def rotate_group(self, gid, reencrypt=True, save=True):
        g = self.group(gid)
        ceks = {}
        for e in self.index["items"]:
            m = self._m(e)
            if gid in m["groups"] and gid in e["keys"]:
                ceks[m["id"]] = self._unwrap(e, gid)
        gk = C.new_key()
        g["ver"] += 1
        for mem in g["members"]:
            mem["env"] = C.box_to(mem["box"], gk, self._aad(g))
        for i, e in enumerate(self.index["items"]):
            m = self._m(e)
            if m["id"] not in ceks:
                continue
            cek = ceks[m["id"]]
            if reencrypt and m["file"] and m["file"]["enc"]:
                path = os.path.join(self.folder, m["file"]["path"])
                with open(path, "rb") as f:
                    plain = C.unseal(cek, f.read(), f"item:{m['id']}")
                old = {o: self._unwrap(e, o) for o in m["groups"] if o != gid and o in e["keys"]}
                cek = C.new_key()
                blob = C.seal(cek, plain, f"item:{m['id']}")
                with open(path, "wb") as f:
                    f.write(blob)
                m["file"]["sha256"] = C.sha256_hex(blob)
                keys = {o: self._wrap(cek, m["id"], o) for o in old}
                ne = self._sign_manifest(m)
                ne["keys"] = keys
                self.index["items"][i] = e = ne
            e["keys"][gid] = self._wrap(cek, m["id"], gid)
        if save:
            self.save()

    # -- expiry --------------------------------------------------------------------------------------------------------------
    def sweep(self, now=None):
        """«تدوير تلقائي»: the expired items marked ``rotate`` are taken out of the portal (file and keys) and the keys
        of their groups are replaced. Expired items without the mark stay listed; the viewer refuses to open them.
        -> the ids removed."""
        gone, touched = [], set()
        for m in self.items():
            if m.get("rotate") and is_expired(m, now):
                touched.update(m["groups"])
                self.remove_item(m["id"], save=False)
                gone.append(m["id"])
        for gid in sorted(touched):
            if any(g["id"] == gid for g in self.index["groups"]):
                self.rotate_group(gid, reencrypt=False, save=False)
        if gone:
            self.save()
        return gone

    # -- following other portals ---------------------------------------------------------------------------------------------
    def follow(self, url, pub, name=""):
        pub = str(pub or "").strip().lower()
        if len(bytes.fromhex(pub)) != 32:
            raise PortalError("not a portal key")
        url = str(url or "").strip()
        self.index["follows"] = [f for f in self.index["follows"] if f["url"] != url]
        self.index["follows"].append({"url": url, "pub": pub, "name": str(name or "")[:120]})
        self.save()

    def unfollow(self, url):
        self.index["follows"] = [f for f in self.index["follows"] if f["url"] != url]
        self.save()

    def address(self, base_url):
        """The link that opens this portal in the hosted viewer with its key pinned."""
        return f"{base_url.rstrip('/')}/#k={self.pub}"
