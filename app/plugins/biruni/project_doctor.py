"""Project doctor: broken data paths, "where is this data used?", renaming a project folder safely, and a
health summary of the open project.

Pure functions here (testable without the interface); the dialog is in project_doctor_dialog.py.
"""
import os
import re
import shutil
import tempfile
import time
import xml.etree.ElementTree as ET
import zipfile

from qgis.core import Qgis, QgsProject, QgsProviderRegistry, QgsSettings

from .i18n import tr

SKIP_DIRS = {".history", "$recycle.bin", "system volume information", "node_modules", ".git", "__pycache__",
             "appdata", "windows", "program files", "program files (x86)", "programdata"}
MAX_INDEX = 400_000          # files indexed per search
BIG_FEATURES = 500_000
BIG_RASTER_PIXELS = 100_000_000
DATASET_DIRS = (".gdb",)     # folders that are datasets in their own right


class RenameError(Exception):
    pass


# ---- layer sources ----------------------------------------------------------------------------------
def source_parts(layer):
    try:
        return QgsProviderRegistry.instance().decodeUri(layer.providerType(), layer.source())
    except Exception:  # noqa: BLE001
        return {}


def source_path(layer):
    return source_parts(layer).get("path") or ""


def is_broken(layer):
    path = source_path(layer)
    if path and not os.path.exists(path):
        return True
    return not layer.isValid() and bool(path)


def broken_layers(project=None):
    project = project or QgsProject.instance()
    out = []
    for lyr in project.mapLayers().values():
        if is_broken(lyr):
            out.append({"layer": lyr, "id": lyr.id(), "name": lyr.name(), "path": source_path(lyr),
                        "provider": lyr.providerType()})
    return sorted(out, key=lambda b: b["name"].lower())


def with_path(layer, new_path):
    """The layer's source with its file replaced by ``new_path`` (keeps |layername=, options …)."""
    reg = QgsProviderRegistry.instance()
    parts = dict(source_parts(layer))
    parts["path"] = new_path
    return reg.encodeUri(layer.providerType(), parts)


def fix_layer(layer, new_path):
    """Point a layer to its data at ``new_path``; the style and settings are kept. True when it loads."""
    layer.setDataSource(with_path(layer, new_path), layer.name(), layer.providerType())
    if layer.isValid():
        layer.triggerRepaint()
    return layer.isValid()


# ---- searching for moved files -----------------------------------------------------------------------
def _norm(p):
    return os.path.normcase(os.path.abspath(p))


def build_index(folders, max_files=MAX_INDEX, should_stop=None):
    """file name (lower case) -> paths, for every file and .gdb folder under ``folders``."""
    index, n = {}, 0
    for top in folders:
        if not top or not os.path.isdir(top):
            continue
        stack = [top]
        while stack:
            d = stack.pop()
            if should_stop and should_stop():
                return index
            try:
                entries = list(os.scandir(d))
            except OSError:
                continue
            for e in entries:
                name = e.name.lower()
                try:
                    if e.is_dir(follow_symlinks=False):
                        if name.endswith(DATASET_DIRS):
                            index.setdefault(name, []).append(e.path)
                        elif name not in SKIP_DIRS and not name.startswith("."):
                            stack.append(e.path)
                        continue
                except OSError:
                    continue
                index.setdefault(name, []).append(e.path)
                n += 1
                if n >= max_files:
                    return index
    return index


def _tail_score(old, cand):
    """How many trailing path parts two paths share (data/roads/x.shp vs …/data/roads/x.shp = 3)."""
    a = [p.lower() for p in re.split(r"[\\/]+", old) if p]
    b = [p.lower() for p in re.split(r"[\\/]+", cand) if p]
    k = 0
    while k < min(len(a), len(b)) and a[-1 - k] == b[-1 - k]:
        k += 1
    return k


def candidates(old_path, index, moved_dirs=None):
    """Possible new places of a missing file, best first."""
    name = os.path.basename(old_path.rstrip("\\/")).lower()
    found = [p for p in index.get(name, []) if _norm(p) != _norm(old_path)]
    moved_dirs = moved_dirs or {}
    old_dir = _norm(os.path.dirname(old_path))

    def key(p):
        in_moved = 1 if old_dir in moved_dirs and _norm(os.path.dirname(p)) == moved_dirs[old_dir] else 0
        return (-in_moved, -_tail_score(old_path, p), len(p))

    return sorted(found, key=key)


def suggest_fixes(broken, folders, index=None):
    """For each broken layer: the best new path (or None) and the alternatives. Once one layer of a folder is
    found, the others of the same old folder are looked for in the same new folder first."""
    index = index if index is not None else build_index(folders)
    moved = {}
    out = []
    # layers whose file name is unique in the index first: they teach where folders went
    order = sorted(broken, key=lambda b: len(index.get(os.path.basename(b["path"].rstrip("\\/")).lower(), [])))
    for b in order:
        cands = candidates(b["path"], index, moved)
        best = cands[0] if cands else None
        if best:
            moved.setdefault(_norm(os.path.dirname(b["path"])), _norm(os.path.dirname(best)))
        out.append(dict(b, best=best, alternatives=cands[1:6]))
    return sorted(out, key=lambda f: f["name"].lower())


def fix_all(fixes):
    """Apply the chosen paths; returns (fixed, failed) lists of layer names."""
    fixed, failed = [], []
    for f in fixes:
        target = f.get("choice") or f.get("best")
        if not target:
            continue
        (fixed if fix_layer(f["layer"], target) else failed).append(f["name"])
    return fixed, failed


# ---- where is this data used? -------------------------------------------------------------------------
def project_files(folders, include_history=False, max_files=20000):
    out = []
    for top in folders:
        if not top or not os.path.isdir(top):
            continue
        for root, dirs, files in os.walk(top):
            dirs[:] = [d for d in dirs if d.lower() not in SKIP_DIRS - ({".history"} if include_history else set())
                       and not d.lower().endswith(".gdb")]
            for f in files:
                fl = f.lower()
                if fl.endswith((".qgz", ".qgs")) and ".autosave." not in fl:
                    out.append(os.path.join(root, f))
                    if len(out) >= max_files:
                        return out
    return out


def read_project_xml(path):
    """The project XML (text) of a .qgs or .qgz file, read-only."""
    if path.lower().endswith(".qgz"):
        with zipfile.ZipFile(path) as z:
            names = [n for n in z.namelist() if n.lower().endswith(".qgs")]
            if not names:
                return ""
            return z.read(names[0]).decode("utf-8", "replace")
    with open(path, encoding="utf-8", errors="replace") as f:
        return f.read()


def resolve(path, project_path):
    """Absolute path of a data source path written in a project (relative ones start with ./ or ../).
    Copies kept in a project's .history are relative to the project folder, not to the copy."""
    path = path.strip()
    if path.startswith(("./", "../", ".\\", "..\\")):
        base = os.path.dirname(project_path)
        if os.path.basename(os.path.dirname(base)).lower() == ".history":
            base = os.path.dirname(os.path.dirname(base))
        return os.path.abspath(os.path.join(base, path))
    return path


def project_layers(project_path):
    """(layer name, provider, data source, file path) of every layer in a project file."""
    try:
        text = read_project_xml(project_path)
        root = ET.fromstring(text.encode("utf-8"))
    except (OSError, zipfile.BadZipFile, ET.ParseError, KeyError):
        return []
    out = []
    for ml in root.iter("maplayer"):
        src = (ml.findtext("datasource") or "").strip()
        name = (ml.findtext("layername") or "").strip()
        provider = (ml.findtext("provider") or "").strip()
        file_part = src.split("|")[0]
        if provider in ("delimitedtext", "gpx", "spatialite", "virtual") or file_part.startswith("file:"):
            m = re.search(r"(?:file:/{0,3})?([A-Za-z]:[\\/][^?|\"]+|\.{1,2}[\\/][^?|\"]+)", src)
            file_part = m.group(1) if m else file_part
            if provider == "spatialite":
                m = re.search(r"dbname='([^']+)'", src)
                file_part = m.group(1) if m else file_part
        path = resolve(file_part, project_path) if file_part else ""
        out.append({"name": name, "provider": provider, "source": src, "path": path})
    return out


def _same_dataset(path, target):
    a, b = _norm(path), _norm(target)
    if a == b:
        return True
    sa, ea = os.path.splitext(a)
    sb, eb = os.path.splitext(b)
    shp = {".shp", ".shx", ".dbf", ".prj", ".cpg", ".qix"}
    return ea in shp and eb in shp and sa == sb


def where_used(target, folders, include_history=False, should_stop=None):
    """Projects under ``folders`` that use the file, or anything inside the folder, ``target``."""
    target = os.path.abspath(target.split("|")[0])
    tnorm = _norm(target)
    is_dir = os.path.isdir(target) or target.lower().endswith(".gdb")
    hits = []
    for proj in project_files(folders, include_history):
        if should_stop and should_stop():
            break
        for lyr in project_layers(proj):
            if not lyr["path"]:
                continue
            p = _norm(lyr["path"])
            if (is_dir and (p == tnorm or p.startswith(tnorm.rstrip("\\/") + os.sep))) or \
                    (not is_dir and _same_dataset(lyr["path"], target)):
                hits.append({"project": proj, "layer": lyr["name"], "source": lyr["source"]})
    return hits


# ---- renaming a project folder ------------------------------------------------------------------------
def rewrite_project_xml(text, old_root, new_root, old_stem, new_stem):
    """Point the project at its renamed folder and database; also renames the title when it was the name."""
    def repl_root(s, a, b):
        for variant in {a, a.replace("\\", "/"), a.replace("/", "\\")}:
            s = re.sub(re.escape(variant) + r"(?=[\\/\"'|<&?]|$)", lambda _m: b.replace("\\", "/"), s,
                       flags=re.IGNORECASE)
        return s

    if old_root and new_root and _norm(old_root) != _norm(new_root):
        text = repl_root(text, os.path.abspath(old_root), os.path.abspath(new_root))
    if old_stem != new_stem:
        gp = re.escape(old_stem) + r"\.gpkg(?=[|\"'<&?]|$)"
        text = re.sub(r"(\./|\.\\)" + gp, lambda m: m.group(1) + new_stem + ".gpkg", text, flags=re.IGNORECASE)
        base = re.escape(os.path.abspath(new_root).replace("\\", "/")) + r"/" if new_root else None
        if base:
            text = re.sub("(" + base + ")" + gp, lambda m: m.group(1) + new_stem + ".gpkg", text,
                          flags=re.IGNORECASE)
        text = re.sub(r"<title>" + re.escape(old_stem) + r"</title>", f"<title>{new_stem}</title>", text)
        text = re.sub(r'projectname="' + re.escape(old_stem) + '"', f'projectname="{new_stem}"', text)
    return text


def rewrite_qgz(path, old_root, new_root, old_stem, new_stem, out_path=None):
    """Rewrite a .qgz (zip) with the new paths; members <old>.qgs/.qgd become <new>.qgs/.qgd."""
    out_path = out_path or path
    fd, tmp = tempfile.mkstemp(suffix=".qgz", dir=os.path.dirname(out_path))
    os.close(fd)
    try:
        with zipfile.ZipFile(path) as zin, zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as zout:
            for item in zin.infolist():
                data = zin.read(item.filename)
                stem, ext = os.path.splitext(item.filename)
                name = (new_stem + ext) if stem == old_stem else item.filename
                if ext.lower() == ".qgs":
                    data = rewrite_project_xml(data.decode("utf-8"), old_root, new_root, old_stem,
                                               new_stem).encode("utf-8")
                zout.writestr(name, data)
        os.replace(tmp, out_path)
        if os.path.abspath(out_path) != os.path.abspath(path) and os.path.exists(path):
            os.remove(path)
    except Exception:
        if os.path.exists(tmp):
            os.remove(tmp)
        raise


def check_name(name):
    name = (name or "").strip()
    if not name or re.search(r'[\\/:*?"<>|]', name) or name.strip(" .") != name or name.upper() in (
            "CON", "PRN", "AUX", "NUL") or re.fullmatch(r"(COM|LPT)\d", name.upper()):
        raise RenameError(tr("This name cannot be used for a folder: {n}").format(n=name))
    return name


def rename_project_files(qgz, new_name, check_locks=True):
    """Rename a closed project: its folder (when the folder carries the project name), <Name>.qgz,
    <Name>.gpkg and their companions, the copies in the project history, and the paths inside the
    project files. Returns the new .qgz path. Everything done is undone if a step fails."""
    new_name = check_name(new_name)
    qgz = os.path.abspath(qgz)
    if not os.path.exists(qgz):
        raise RenameError(tr("The project file was not found: {p}").format(p=qgz))
    old_root = os.path.dirname(qgz)
    old_stem = os.path.splitext(os.path.basename(qgz))[0]
    if new_name == old_stem:
        return qgz
    folder_project = os.path.basename(old_root) == old_stem
    new_root = os.path.join(os.path.dirname(old_root), new_name) if folder_project else old_root
    if folder_project and os.path.exists(new_root) and _norm(new_root) != _norm(old_root):
        raise RenameError(tr("A folder with this name already exists: {p}").format(p=new_root))
    companions = [f for f in os.listdir(old_root) if f.startswith(old_stem + ".") or f == old_stem + ".gpkg"]
    if not folder_project:
        for f in companions:
            if os.path.exists(os.path.join(old_root, new_name + f[len(old_stem):])):
                raise RenameError(tr("A file with this name already exists: {p}").format(p=new_name + f[len(old_stem):]))
    if check_locks:
        from .file_locks import processes_using

        files = [os.path.join(old_root, f) for f in companions]
        procs = processes_using(files) or []
        if procs:
            names = ", ".join(sorted({p["app"] or p["name"] for p in procs}))
            raise RenameError(tr("The project is still open in: {n}. Close it there first.").format(n=names))

    done = []  # (undo callable)
    try:
        if folder_project:
            os.rename(old_root, new_root)
            done.append(lambda: os.rename(new_root, old_root))
        root = new_root
        for f in sorted(companions):
            src = os.path.join(root, f)
            dst = os.path.join(root, new_name + f[len(old_stem):])
            if os.path.exists(src):
                os.rename(src, dst)
                done.append(lambda s=src, d=dst: os.rename(d, s))
        new_qgz = os.path.join(root, new_name + ".qgz")
        backup = new_qgz + ".rename-backup"
        shutil.copy2(new_qgz, backup)
        done.append(lambda: shutil.copy2(backup, new_qgz))
        rewrite_qgz(new_qgz, old_root, new_root, old_stem, new_name)
        for qa in [f for f in os.listdir(root) if f.lower().endswith(".autosave.qgz") and f.startswith(new_name)]:
            try:
                rewrite_qgz(os.path.join(root, qa), old_root, new_root, old_stem, new_name)
            except (OSError, zipfile.BadZipFile):
                pass
        hist = os.path.join(root, ".history")
        if os.path.isdir(hist):
            for v in os.listdir(hist):
                vd = os.path.join(hist, v)
                if not os.path.isdir(vd):
                    continue
                for f in os.listdir(vd):
                    if f.startswith(old_stem + "."):
                        dst = os.path.join(vd, new_name + f[len(old_stem):])
                        os.rename(os.path.join(vd, f), dst)
                        if dst.lower().endswith(".qgz"):
                            try:
                                rewrite_qgz(dst, old_root, new_root, old_stem, new_name)
                            except (OSError, zipfile.BadZipFile):
                                pass
        os.remove(backup)
    except Exception as e:
        for undo in reversed(done):
            try:
                undo()
            except OSError:
                pass
        if isinstance(e, RenameError):
            raise
        raise RenameError(tr("The project could not be renamed: {e}").format(e=e)) from e
    update_recent_projects(qgz, new_qgz)
    return new_qgz


def update_recent_projects(old, new):
    """Point the recent-projects list of the start page at the renamed project."""
    s = QgsSettings()
    s.beginGroup("UI/recentProjects")
    try:
        for g in s.childGroups():
            p = s.value(f"{g}/path", "")
            if p and _norm(p) == _norm(old):
                s.setValue(f"{g}/path", new.replace("\\", "/"))
                title = s.value(f"{g}/title", "")
                if title == os.path.splitext(os.path.basename(old))[0]:
                    s.setValue(f"{g}/title", os.path.splitext(os.path.basename(new))[0])
    finally:
        s.endGroup()


def recent_project_paths():
    s = QgsSettings()
    s.beginGroup("UI/recentProjects")
    try:
        return [s.value(f"{g}/path", "") for g in s.childGroups() if s.value(f"{g}/path", "")]
    finally:
        s.endGroup()


def pending_edits(project=None):
    project = project or QgsProject.instance()
    return [lyr for lyr in project.mapLayers().values()
            if lyr.type() == Qgis.LayerType.Vector and lyr.isEditable() and lyr.isModified()]


def rename_open_project(iface, new_name, wait=2.0):
    """Rename the project that is open now: save, close, rename, reopen. Returns the new path."""
    prj = QgsProject.instance()
    path = prj.fileName()
    if not path:
        raise RenameError(tr("Save the map as a project first."))
    if pending_edits(prj):
        raise RenameError(tr("Some layers have edits that are not saved. Save or discard them first."))
    check_name(new_name)
    if prj.isDirty() and not prj.write():
        raise RenameError(prj.error())
    from qgis.PyQt.QtWidgets import QApplication

    iface.newProject(False)
    QApplication.processEvents()
    deadline = time.time() + wait
    while True:
        try:
            new = rename_project_files(path, new_name, check_locks=False)
            break
        except RenameError:
            if time.time() > deadline:
                iface.addProject(path)
                raise
            QApplication.processEvents()
            time.sleep(0.25)
    iface.addProject(new)
    return new


# ---- health ------------------------------------------------------------------------------------------
def raster_has_overviews(layer):
    try:
        return bool(layer.dataProvider().hasPyramids())
    except Exception:  # noqa: BLE001
        return True


def health_report(project=None, project_folder=None):
    """What may need attention in the open project."""
    project = project or QgsProject.instance()
    rep = {"broken": [], "no_crs": [], "temporary": [], "big": [], "outside": [],
           "relative": project.filePathStorage() == Qgis.FilePathType.Relative}
    for lyr in project.mapLayers().values():
        if is_broken(lyr):
            rep["broken"].append(lyr.name())
            continue
        if lyr.type() in (Qgis.LayerType.Vector, Qgis.LayerType.Raster) and lyr.isSpatial() \
                and not lyr.crs().isValid():
            rep["no_crs"].append(lyr.name())
        if lyr.providerType() == "memory":
            rep["temporary"].append(lyr.name())
        if lyr.type() == Qgis.LayerType.Vector and lyr.featureCount() > BIG_FEATURES:
            rep["big"].append(lyr.name())
        elif lyr.type() == Qgis.LayerType.Raster and lyr.width() * lyr.height() > BIG_RASTER_PIXELS \
                and not raster_has_overviews(lyr):
            rep["big"].append(lyr.name())
        path = source_path(lyr)
        if project_folder and path and os.path.exists(path):
            try:
                inside = os.path.commonpath([_norm(path), _norm(project_folder)]) == _norm(project_folder)
            except ValueError:
                inside = False
            if not inside:
                rep["outside"].append(lyr.name())
    return rep


def set_relative_paths(relative=True, project=None):
    project = project or QgsProject.instance()
    project.setFilePathStorage(Qgis.FilePathType.Relative if relative else Qgis.FilePathType.Absolute)
    project.setDirty(True)
