"""Locked projects (*.bproj): a project and the data files it uses, encrypted with one password.

File: MAGIC, a JSON header (key-derivation parameters, the data key wrapped with the password's key), then
the AES-256-GCM ciphertext of a ZIP that holds the project (.qgs) and its data files.

Opening: the data files are decrypted into memory (GDAL /vsimem/) and the project's layers point there.
The project itself is read through a project storage ("biruni-locked:<path>"): saving the project with the
usual Save encrypts it again into the .bproj. Limit, said plainly: the engine reads and writes a project
through a temporary .qgz on disk, so the project file itself (layer list, styles, layouts - not the layers'
data) passes through the Windows temporary folder for a moment."""
import html
import io
import json
import os
import re
import uuid
import zipfile

from . import crypto
from . import gpkg

MAGIC = b"BIRUNI-LOCKPRJ\x00\x01"
EXT = ".bproj"
SCHEME = "biruni-locked"
TOKEN = "@BIRUNI_LOCKED@"
PROJECT_EXTS = (".qgz", ".qgs")
_open = {}  # normalised .bproj path -> LockedProject
_storage = []


class ProjectLockError(Exception):
    def __init__(self, code, detail=""):
        super().__init__(code if not detail else f"{code}: {detail}")
        self.code = code


def _key(path):
    return os.path.normcase(os.path.abspath(path))


def is_locked_project(path):
    try:
        with open(path, "rb") as f:
            return f.read(len(MAGIC)) == MAGIC
    except OSError:
        return False


# ---- the project file and its data --------------------------------------------------------------------------
def read_project_xml(project_path):
    if project_path.lower().endswith(".qgz"):
        with zipfile.ZipFile(project_path) as z:
            name = next(n for n in z.namelist() if n.lower().endswith(".qgs"))
            return z.read(name).decode("utf-8"), {n: z.read(n) for n in z.namelist() if n.lower().endswith(".qgd")}
    with open(project_path, encoding="utf-8") as f:
        return f.read(), {}


_SOURCE_RES = (re.compile(r"(<datasource>)(.*?)(</datasource>)", re.S), re.compile(r'(\bsource=")([^"]*)(")'))


def _split_source(value):
    """(prefix, file path, rest) of a layer source: 'path|layername=x', 'file:///path?…' and plain paths."""
    prefix = ""
    if value.startswith("file:///"):
        prefix, value = "file:///", value[8:]
    cut = len(value)
    for sep in ("|", "?"):
        i = value.find(sep)
        if i >= 0:
            cut = min(cut, i)
    return prefix, value[:cut], value[cut:]


def _resolve(path, home):
    if not path or path.startswith("/vsi") or "://" in path:
        return None
    p = path if os.path.isabs(path) else os.path.join(home, path)
    return os.path.normpath(p)


def _rewrite(xml, fn):
    def sub(m, quote):
        value = html.unescape(m.group(2))
        new = fn(value)
        if new is None:
            return m.group(0)
        return m.group(1) + html.escape(new, quote=quote) + m.group(3)

    xml = _SOURCE_RES[0].sub(lambda m: sub(m, False), xml)
    return _SOURCE_RES[1].sub(lambda m: sub(m, True), xml)


def project_data_files(project_path):
    """The files the project's layers read, as [(absolute path, inside the project's folder)] - with the
    files that belong to them (a shapefile's .dbf, .shx, .prj …)."""
    xml, _ = read_project_xml(project_path)
    home = os.path.dirname(os.path.abspath(project_path))
    found = {}

    def look(value):
        _, path, _ = _split_source(value)
        p = _resolve(path, home)
        if p and os.path.isfile(p):
            found[os.path.normcase(p)] = p
        return None

    _rewrite(xml, look)
    out = []
    for p in found.values():
        inside = os.path.normcase(p).startswith(os.path.normcase(home) + os.sep)
        out.append((p, inside))
    return sorted(out, key=lambda x: (not x[1], x[0].lower()))


def _companions(path):
    folder, base = os.path.split(path)
    stem = os.path.splitext(base)[0].lower()
    out = [path]
    try:
        for n in os.listdir(folder):
            full = os.path.join(folder, n)
            if full != path and os.path.isfile(full) and n.lower().split(".")[0] == stem.split(".")[0] \
                    and os.path.splitext(n)[1].lower() not in PROJECT_EXTS + (EXT, ".bgpkg"):
                out.append(full)
    except OSError:
        pass
    return out


def _arcname(path, home, used):
    p = os.path.normcase(path)
    if p.startswith(os.path.normcase(home) + os.sep):
        rel = os.path.relpath(path, home).replace("\\", "/")
    else:
        rel = "external/" + os.path.basename(path)
    base, n = rel, 2
    while rel.lower() in used:
        stem, ext = os.path.splitext(base)
        rel = f"{stem}_{n}{ext}"
        n += 1
    used.add(rel.lower())
    return rel


def _header_and_body(path):
    with open(path, "rb") as f:
        data = f.read()
    if not data.startswith(MAGIC):
        raise ProjectLockError("not_locked")
    n = int.from_bytes(data[len(MAGIC):len(MAGIC) + 4], "big")
    start = len(MAGIC) + 4
    header = json.loads(data[start:start + n].decode("utf-8"))
    return header, data[start + n:]


def _write(path, header, body):
    hb = crypto.canonical(header)
    tmp = f"{path}.{uuid.uuid4().hex[:6]}.part"
    with open(tmp, "wb") as f:
        f.write(MAGIC + len(hb).to_bytes(4, "big") + hb)
        f.write(body)
    os.replace(tmp, path)


def lock(project_path, files, out_path, password):
    """Encrypts a project and the chosen data files into out_path. Layers whose files are not chosen keep
    pointing to their files (not locked). The original files are not changed."""
    xml, qgd = read_project_xml(project_path)
    home = os.path.dirname(os.path.abspath(project_path))
    used, arcs = set(), {}
    for f in files:
        for c in _companions(os.path.abspath(f)):
            key = os.path.normcase(os.path.normpath(c))
            if key not in arcs:
                arcs[key] = _arcname(c, home, used)

    def to_token(value):
        prefix, path, rest = _split_source(value)
        p = _resolve(path, home)
        if p and os.path.normcase(p) in arcs:
            return prefix + f"{TOKEN}/{arcs[os.path.normcase(p)]}" + rest
        return None

    xml = _rewrite(xml, to_token)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED, compresslevel=1) as z:
        z.writestr("project.qgs", xml.encode("utf-8"))
        for n, data in qgd.items():
            z.writestr("aux/" + os.path.basename(n), data)
        for src, arc in arcs.items():
            z.write(src, "data/" + arc)
    dk = crypto.random_key()
    family = uuid.uuid4().hex
    kdf = crypto.new_kdf()
    kek = crypto.derive(password, kdf)
    header = {"biruni_locked_project": 1, "family": family, "name": os.path.splitext(os.path.basename(project_path))[0],
              "kdf": kdf, "wrap": crypto.seal_json(kek, {"dk": crypto.b64(dk)}, f"{family}|wrap".encode())}
    body = crypto.seal(dk, buf.getvalue(), f"{family}|project".encode())
    _write(out_path, header, body)
    return sorted(arcs.values())


class LockedProject:
    """An unlocked project: its data files in memory and its project XML."""

    def __init__(self, path, password):
        self.path = os.path.abspath(path)
        self.header, body = _header_and_body(self.path)
        fam = self.header["family"]
        try:
            kek = crypto.derive(password, self.header["kdf"])
            self.dk = crypto.unb64(crypto.unseal_json(kek, self.header["wrap"], f"{fam}|wrap".encode())["dk"])
            data = crypto.unseal(self.dk, body, f"{fam}|project".encode())
        except crypto.CryptoError as e:
            raise ProjectLockError("wrong_password") from e
        self.folder = gpkg.private_name()
        self.root = f"{gpkg.ROOT}/{self.folder}"
        self.files = []
        self.aux = {}
        gpkg.hold_memory_temp()
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            self.xml = z.read("project.qgs").decode("utf-8")
            for n in z.namelist():
                if n.startswith("data/") and not n.endswith("/"):
                    rel = n[5:]
                    gpkg.mem_write(f"{self.root}/{rel}", z.read(n))
                    self.files.append(rel)
                elif n.startswith("aux/"):
                    self.aux[n[4:]] = z.read(n)
        del data
        self.closed = False

    @property
    def name(self):
        return self.header.get("name") or os.path.splitext(os.path.basename(self.path))[0]

    def project_xml(self):
        return self.xml.replace(TOKEN, self.root)

    def qgz_bytes(self):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
            z.writestr(self.name + ".qgs", self.project_xml().encode("utf-8"))
            for n, data in self.aux.items():
                z.writestr(n, data)
        return buf.getvalue()

    def take_qgz(self, data):
        """The project as the engine saved it (a .qgz in memory): keep its XML, with memory paths as tokens."""
        with zipfile.ZipFile(io.BytesIO(bytes(data))) as z:
            name = next(n for n in z.namelist() if n.lower().endswith(".qgs"))
            self.xml = z.read(name).decode("utf-8").replace(self.root, TOKEN)
            self.aux = {os.path.basename(n): z.read(n) for n in z.namelist() if n.lower().endswith(".qgd")}

    def save(self):
        """Encrypts the project and the data files (as they are now in memory) back into the .bproj."""
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED, compresslevel=1) as z:
            z.writestr("project.qgs", self.xml.encode("utf-8"))
            for n, data in self.aux.items():
                z.writestr("aux/" + n, data)
            for rel in self.files:
                z.writestr("data/" + rel, gpkg.mem_read(f"{self.root}/{rel}"))
        body = crypto.seal(self.dk, buf.getvalue(), f"{self.header['family']}|project".encode())
        _write(self.path, self.header, body)

    def change_password(self, password):
        kdf = crypto.new_kdf()
        kek = crypto.derive(password, kdf)
        self.header["kdf"] = kdf
        self.header["wrap"] = crypto.seal_json(kek, {"dk": crypto.b64(self.dk)}, f"{self.header['family']}|wrap".encode())
        self.save()

    def unlock_to(self, folder):
        """Removes the lock: writes the project and its files, not encrypted, into a folder. Returns the
        project file written."""
        os.makedirs(folder, exist_ok=True)
        for rel in self.files:
            dst = os.path.join(folder, *rel.split("/"))
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            with open(dst, "wb") as f:
                f.write(gpkg.mem_read(f"{self.root}/{rel}"))
        xml = self.xml.replace(TOKEN, os.path.abspath(folder).replace("\\", "/"))
        out = os.path.join(folder, self.name + ".qgz")
        with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
            z.writestr(self.name + ".qgs", xml.encode("utf-8"))
            for n, data in self.aux.items():
                z.writestr(n, data)
        return out

    def close(self):
        if self.closed:
            return
        self.closed = True
        for rel in self.files:  # also the files in sub-folders
            gpkg.mem_wipe(f"{self.root}/{rel}")
        gpkg.wipe_folder(self.folder)
        self.dk = b""
        gpkg.release_memory_temp()
        _open.pop(_key(self.path), None)


# ---- the project storage: Open and Save of "biruni-locked:<path>" -------------------------------------------
def uri(path):
    return f"{SCHEME}:{os.path.abspath(path)}"


def path_of(text):
    return text[len(SCHEME) + 1:] if str(text).startswith(SCHEME + ":") else None


def get(path):
    return _open.get(_key(path))


def register(lp):
    _open[_key(lp.path)] = lp
    ensure_storage()


def ensure_storage():
    """Registers the project storage the first time a locked project is opened (not at start-up)."""
    if _storage:
        return _storage[0]
    from qgis.core import QgsApplication, QgsProjectStorage

    class LockedProjectStorage(QgsProjectStorage):
        def type(self):
            return SCHEME

        def listProjects(self, uri_):  # noqa: N802
            return []

        def readProject(self, uri_, device, context):  # noqa: N802
            lp = get(path_of(uri_) or "")
            if lp is None or lp.closed:
                return False
            device.write(lp.qgz_bytes())
            device.seek(0)
            return True

        def writeProject(self, uri_, device, context):  # noqa: N802
            lp = get(path_of(uri_) or "")
            if lp is None or lp.closed:
                return False
            device.seek(0)
            lp.take_qgz(bytes(device.readAll()))
            lp.save()
            return True

        def removeProject(self, uri_):  # noqa: N802
            return False

        def readProjectStorageMetadata(self, uri_, metadata):  # noqa: N802
            lp = get(path_of(uri_) or "")
            metadata.name = lp.name if lp else os.path.basename(path_of(uri_) or "")
            return True

    st = LockedProjectStorage()
    QgsApplication.projectStorageRegistry().registerProjectStorage(st)
    _storage.append(st)
    return st


def open_in(project, path, password):
    """Unlocks a .bproj and reads it into a QgsProject. Raises ProjectLockError('wrong_password')."""
    old = get(path)
    if old is not None:
        old.close()
    lp = LockedProject(path, password)
    register(lp)
    if not project.read(uri(path)):
        lp.close()
        raise ProjectLockError("unreadable")
    return lp


def close_all():
    for lp in list(_open.values()):
        lp.close()
