"""Migration readiness scan: walk folders, find everything an ArcGIS user has (projects, maps, layer files, ArcMap
documents, arcpy scripts, toolboxes, geodatabases, shapefiles, layouts) and check each item with the real readers,
read-only: nothing is added to the open project and nothing is written next to the files.

Each item gets a status:
    ok       works 100%
    changes  opens / runs, with changes (symbols that differ, ArcMap styles not kept, partial arcpy calls)
    needs    something is missing (data not found, unsupported layers, missing arcpy functions, unconverted tools)
    fails    cannot be read
    unknown  could not be judged (scripts without the arcpy analyser)
"""
import os
import re
import time
from collections import Counter
from dataclasses import dataclass, field

from ..i18n import tr

MAP_EXTENSIONS = (".aprx", ".mapx", ".mxd", ".mxt")
LAYER_EXTENSIONS = (".lyrx", ".lyr")
SCRIPT_EXTENSIONS = (".py", ".pyt")
TOOLBOX_EXTENSIONS = (".atbx", ".tbx")
LAYOUT_EXTENSIONS = (".pagx",)
DATA_EXTENSIONS = (".mdb", ".shp")

KINDS = ("project", "map", "layer", "script", "toolbox", "geodatabase", "shapefiles", "layout")
STATUSES = ("ok", "changes", "needs", "fails", "unknown")

# folders never worth walking: tools, caches, system folders, other programs' libraries
SKIP_DIRS = {"appdata", "node_modules", ".git", ".svn", ".hg", "__pycache__", "$recycle.bin",
             "system volume information", "windows", "program files", "program files (x86)", "programdata",
             "site-packages", "dist-packages", ".venv", "venv", ".tox", ".cache", ".vs", ".vscode", ".idea",
             "msys64", "anaconda3", "miniconda3", "pkgs", "recovery", "$windows.~bt", "$windows.~ws",
             "config.msi", "msocache", "perflogs", ".gradle", ".m2", ".nuget", "bower_components",
             "onedrivetemp", "temp", "tmp"}
MAX_SCRIPT_BYTES = 2_000_000
MAX_MAP_BYTES = 400_000_000


def kind_label(kind):
    return {"project": tr("Project"), "map": tr("Map"), "layer": tr("Layer file"), "script": tr("Script"),
            "toolbox": tr("Toolbox"), "geodatabase": tr("Geodatabase"), "shapefiles": tr("Shapefiles"),
            "layout": tr("Layout")}.get(kind, kind)


def status_label(status):
    return {"ok": tr("Works 100%"), "changes": tr("Works with changes"), "needs": tr("Needs work"),
            "fails": tr("Cannot be read"), "unknown": tr("Not checked")}.get(status, status)


@dataclass
class Item:
    path: str
    kind: str
    status: str = "unknown"
    score: int = 0                                   # 0-100
    note: str = ""                                   # one line for the table
    counts: dict = field(default_factory=dict)       # numbers behind the status
    missing: list = field(default_factory=list)      # (category, key): functions / features that block or change
    details: list = field(default_factory=list)      # longer notes
    seconds: float = 0.0

    @property
    def name(self):
        return os.path.basename(self.path.rstrip("\\/")) or self.path


@dataclass
class ScanResult:
    folders: list
    items: list = field(default_factory=list)
    seconds: float = 0.0
    walked_dirs: int = 0
    cancelled: bool = False


# ---- finding --------------------------------------------------------------------------------------------------
def _kind_for_file(name):
    low = name.lower()
    ext = os.path.splitext(low)[1]
    if ext == ".aprx":
        return "project"
    if ext in (".mapx", ".mxd", ".mxt"):
        return "map"
    if ext in LAYER_EXTENSIONS:
        return "layer"
    if ext in SCRIPT_EXTENSIONS:
        return "script"
    if ext in TOOLBOX_EXTENSIONS:
        return "toolbox"
    if ext in LAYOUT_EXTENSIONS:
        return "layout"
    if ext == ".mdb":
        return "geodatabase"
    return None


def _has_arcpy(path):
    try:
        if os.path.getsize(path) > MAX_SCRIPT_BYTES:
            return False
        with open(path, "rb") as f:
            return b"arcpy" in f.read()
    except OSError:
        return False


def default_excludes():
    """This program's own folders (its engine is full of Python files that are not the user's)."""
    here = os.path.abspath(os.path.dirname(__file__))
    root = os.path.abspath(os.path.join(here, "..", "..", "..", ".."))   # <install>/app/plugins/biruni/migrate
    out = [os.path.join(root, "engine"), os.path.join(root, "app")]
    import sys

    out.append(os.path.abspath(sys.prefix))
    return [os.path.normcase(p) for p in out if os.path.isdir(p)]


def find_items(folders, cancel=None, progress=None, exclude=None):
    """[(kind, path)] under the folders. Shapefiles are grouped per folder; .gdb folders are not entered."""
    excl = set(default_excludes() if exclude is None else [os.path.normcase(os.path.abspath(e)) for e in exclude])
    found, seen, walked = [], set(), 0
    stack = [os.path.abspath(f) for f in folders if f and os.path.isdir(f)]
    last = time.monotonic()
    while stack:
        if cancel and cancel():
            break
        folder = stack.pop()
        key = os.path.normcase(folder)
        if key in seen or key in excl:
            continue
        seen.add(key)
        walked += 1
        try:
            entries = list(os.scandir(folder))
        except OSError:
            continue
        shapefiles = 0
        subdirs = []
        for e in entries:
            name = e.name
            try:
                if e.is_dir(follow_symlinks=False):
                    low = name.lower()
                    if low.endswith(".gdb"):
                        found.append(("geodatabase", e.path))
                    elif low not in SKIP_DIRS and not low.startswith((".", "$")) and not e.is_symlink():
                        subdirs.append(e.path)
                    continue
            except OSError:
                continue
            low = name.lower()
            if low.endswith(".shp"):
                shapefiles += 1
                continue
            kind = _kind_for_file(name)
            if kind is None:
                continue
            if kind == "script" and low.endswith(".py") and not _has_arcpy(e.path):
                continue
            found.append((kind, e.path))
        if shapefiles:
            found.append(("shapefiles", folder))
        stack.extend(reversed(sorted(subdirs)))
        if progress and time.monotonic() - last > 0.25:
            last = time.monotonic()
            progress(-1, tr("Looking in {folder}… ({n} found)").format(folder=folder, n=len(found)))
    return found, walked


# ---- maps, projects, layer files --------------------------------------------------------------------------------
class _NullProject:
    """Stands in for a project: layers are made and checked, never added to anything the user sees."""

    def __init__(self):
        from qgis.core import QgsLayerTreeGroup

        self.root = QgsLayerTreeGroup()
        self.layers = []

    def addMapLayer(self, layer, _add_to_tree=True):  # noqa: N802
        self.layers.append(layer)
        return layer

    def layerTreeRoot(self):  # noqa: N802
        return self.root


_BUILDER = []


def _builder_class():
    if _BUILDER:
        return _BUILDER[0]
    from ..arcgis import reader

    class _Missing(reader._Skip):
        """Data source not found."""

    class ScanBuilder(reader._Builder):
        """The reader's own layer builder, with each layer's outcome recorded."""

        def __init__(self, doc, project, deadline):
            super().__init__(doc, project, services=False)
            self.deadline = deadline
            self.outcomes = []          # (name, outcome, detail)
            self.bad_hosts = set()

        def _path(self, p, must_exist=True):
            raw = (p or "").strip().strip('"')
            host = re.match(r"^(?:\\\\|//)([^\\/]+)", raw)
            if host and host.group(1).lower() in self.bad_hosts:
                raise _Missing(tr("network share not reachable: {path}").format(path=raw))
            t = time.monotonic()
            try:
                return super()._path(p, must_exist)
            except reader._Skip as e:
                if host and time.monotonic() - t > 1.5:
                    self.bad_hosts.add(host.group(1).lower())
                raise _Missing(str(e)) from e

        def make_layer(self, cim):
            name = cim.get("name") or "?"
            t = cim.get("type") or "?"
            if time.monotonic() >= self.deadline:
                self.outcomes.append((name, "timeout", ""))
                raise reader._Skip(tr("not checked: time limit"))
            before = len(self.report.warnings)
            try:
                layer, kind = super().make_layer(cim)
            except _Missing as e:
                self.outcomes.append((name, "missing", str(e)))
                raise
            except reader._Skip as e:
                text = str(e)
                if t == "MxdBasemapLayer" or "Esri basemap" in text or text == tr(
                        "Esri basemap (needs an ArcGIS account) - add a basemap from the Basemap button"):
                    outcome, detail = "basemap", tr("Esri basemaps (replaced by free basemaps)")
                elif t in reader._UNSUPPORTED:
                    outcome, detail = "unsupported", reader._unsupported_reason(t)
                elif text == tr("online service not opened"):
                    outcome, detail = "online", ""
                elif text.startswith(tr("the data could not be opened ({uri})").split("(")[0]):
                    outcome, detail = "broken", text
                else:
                    outcome, detail = "unsupported", text
                self.outcomes.append((name, outcome, detail))
                raise
            changes = [w.partition(": ")[2] for w in self.report.warnings[before:]]
            self.outcomes.append((name, "ok", changes))
            return layer, kind

    _BUILDER.append(ScanBuilder)
    return ScanBuilder


def _general(text):
    """A warning or reason without its file names / values, so equal problems count together."""
    t = re.sub(r"(https?|file)://\S+", "…", text)
    t = re.sub(r"'[^']*'", "'…'", t)
    t = re.sub(r"\([^)]*[\\/][^)]*\)", "(…)", t)
    t = re.sub(r":\s*[A-Za-z]:[\\/].*$", "", t)
    t = re.sub(r"\{[^}]*\}", "…", t)
    return t.strip()


def analyse_map(path, kind, time_limit=20.0):
    """Project / map / layer file -> Item (layers opened, missing data, symbology kept or changed)."""
    from ..arcgis import reader

    item = Item(path=path, kind=kind)
    ext = os.path.splitext(path)[1].lower()
    if ext == ".lyr":
        return _analyse_lyr(path, item)
    try:
        if os.path.getsize(path) > MAX_MAP_BYTES:
            item.status, item.note = "unknown", tr("not checked: the file is very large")
            return item
    except OSError as e:
        item.status, item.note = "fails", str(e)
        return item
    try:
        doc = reader._Document(path)
    except reader.ArcGISError as e:
        item.status, item.note = "fails", str(e)
        item.missing.append(("feature", _general(str(e))[:120]))
        return item
    except Exception as e:  # noqa: BLE001 - a damaged file of any kind
        item.status, item.note = "fails", tr("The file is damaged or not an ArcGIS Pro file: {e}").format(e=e)
        return item

    project = _NullProject()
    builder = _builder_class()(doc, project, time.monotonic() + time_limit)
    builder.report = reader.Report(source=path)
    if doc.top:
        builder.add_tree(doc.top, project.root, index=0)
    for m in doc.maps:
        builder.add_tree(m.get("layers") or [], project.root)
        builder.add_tree(m.get("standaloneTables") or [], project.root)
    for uri, why in builder.report.skipped:
        if why == tr("layer definition missing from the file"):
            builder.outcomes.append((uri, "broken", why))
    layouts = sum(1 for d in doc.defs.values() if isinstance(d, dict) and d.get("type") == "CIMLayout")
    project.layers.clear()

    c = Counter(o for _n, o, _d in builder.outcomes)
    opened = c["ok"]
    total = sum(c.values())
    arcmap = ext in (".mxd", ".mxt")
    exact = sum(1 for _n, o, d in builder.outcomes if o == "ok" and not d)
    changed = opened - exact
    item.counts = {"maps": len(doc.maps), "layers": total, "opened": opened, "missing": c["missing"],
                   "unsupported": c["unsupported"], "broken": c["broken"], "online": c["online"],
                   "basemaps": c["basemap"], "timeout": c["timeout"], "symbols_exact": 0 if arcmap else exact,
                   "symbols_changed": 0 if arcmap else changed, "symbols_default": opened if arcmap else 0,
                   "layouts": layouts}
    for _n, o, d in builder.outcomes:
        if o in ("unsupported", "basemap"):
            item.missing.append(("layer", _general(d)))
        elif o == "ok" and d and not arcmap:
            for w in d:
                item.missing.append(("symbology", _general(w)))
    if arcmap and opened:
        item.missing.append(("symbology", tr("ArcMap symbology (layers open with default styles)")))
    if layouts:
        item.missing.append(("layout", tr("page layouts (rebuild them in the print layout)")))
    for name, o, d in builder.outcomes:
        if o != "ok":
            item.details.append(f"{name}: {d or o}")
        elif d:
            item.details.append(f"{name}: " + "; ".join(d))

    checked = total - c["online"] - c["timeout"] - c["basemap"]
    item.score = 100 if checked == 0 else int(round(100 * opened / checked))
    blocking = c["missing"] + c["unsupported"] + c["broken"]
    if total == 0 and arcmap:   # an empty document, or one whose layers the ArcMap reader does not recognise
        item.status = "unknown"
        item.note = tr("no layers found in this ArcMap document")
    elif total == 0:
        item.status = "ok" if doc.maps or doc.top else "unknown"
        item.note = tr("no layers")
    elif blocking:
        item.status = "needs"
    elif c["timeout"]:
        item.status = "unknown"
    elif changed or arcmap or c["basemap"]:
        item.status = "changes"
    else:
        item.status = "ok"
    parts = [tr("{opened} of {total} layers open").format(opened=opened, total=total)]
    if c["missing"]:
        parts.append(tr("{n} data not found").format(n=c["missing"]))
    if c["unsupported"]:
        parts.append(tr("{n} unsupported").format(n=c["unsupported"]))
    if c["broken"]:
        parts.append(tr("{n} cannot be opened").format(n=c["broken"]))
    if c["online"]:
        parts.append(tr("{n} online services (not checked)").format(n=c["online"]))
    if c["basemap"]:
        parts.append(tr("{n} Esri basemaps to replace").format(n=c["basemap"]))
    if c["timeout"]:
        parts.append(tr("{n} not checked (time limit)").format(n=c["timeout"]))
    if arcmap and opened:
        parts.append(tr("default styles"))
    elif changed:
        parts.append(tr("{n} with symbol changes").format(n=changed))
    if layouts:
        parts.append(tr("{n} layouts").format(n=layouts))
    if total:
        item.note = ", ".join(parts)
    return item


def _analyse_lyr(path, item):
    """ArcMap layer file (.lyr): an OLE file of ArcObjects - the layers inside are listed, not opened."""
    from ..arcgis import mxd

    try:
        with open(path, "rb") as f:
            data = f.read()
        streams = mxd._streams(data)
    except (OSError, mxd.MxdError) as e:
        item.status, item.note = "fails", str(e)
        return item
    names = []
    for b in streams.values():
        for cls, (kind, gap) in mxd._LAYERS.items():
            if kind == "map":
                continue
            for p in mxd._find_all(b, cls):
                name, _end = mxd._bstr(b, p + 16 + gap)
                if name and name not in names:
                    names.append(name)
    item.counts = {"layers": len(names)}
    item.status, item.score = "needs", 0
    item.note = tr("ArcMap layer file with {n} layers: save it as .lyrx in ArcGIS Pro to open it").format(
        n=len(names))
    item.details = names[:50]
    item.missing.append(("feature", tr("ArcMap layer files (.lyr)")))
    return item


def analyse_layout(path):
    """Layout file (.pagx): what is on the page; layouts are rebuilt by hand."""
    import json

    item = Item(path=path, kind="layout")
    try:
        with open(path, "rb") as f:
            d = json.loads(f.read().decode("utf-8-sig"))
    except (OSError, ValueError, UnicodeDecodeError) as e:
        item.status, item.note = "fails", str(e)
        return item
    layout = d.get("layoutDefinition") or {}
    kinds = Counter((e.get("type") or "?").replace("CIM", "") for e in layout.get("elements") or [])
    item.counts = dict(kinds)
    item.status, item.score = "needs", 0
    item.note = tr("{n} page elements ({kinds}): rebuild the page in the print layout").format(
        n=sum(kinds.values()), kinds=", ".join(f"{k} {v}" for k, v in kinds.most_common(5)))
    item.missing.append(("layout", tr("page layouts (rebuild them in the print layout)")))
    return item


# ---- scripts --------------------------------------------------------------------------------------------------
_ARCPY_CALL = re.compile(r"\barcpy((?:\.[A-Za-z_]\w*)+)\s*\(")


def _fallback_analysis(text):
    calls = []
    for m in _ARCPY_CALL.finditer(text):
        calls.append({"name": "arcpy" + m.group(1), "line": text.count("\n", 0, m.start()) + 1, "status": "unknown"})
    return {"calls": calls, "supported": 0, "partial": 0, "missing": 0, "score": None, "missing_names": [],
            "fallback": True}


def analyse_script_text(text):
    """The arcpy analyser's verdict on a script ({calls, supported, partial, missing, score, missing_names}), or a
    simple count of the arcpy calls marked unknown when the analyser is not available."""
    try:
        from ..arcpy_compat.analyse import analyse_script
    except Exception:  # noqa: BLE001 - analyser not installed / broken: count only
        return _fallback_analysis(text)
    try:
        res = analyse_script(text)
        if not isinstance(res, dict):
            raise TypeError("bad result")
        return res
    except Exception:  # noqa: BLE001
        return _fallback_analysis(text)


def _script_item(item, res):
    calls = res.get("calls") or []
    item.counts = {"calls": len(calls), "supported": int(res.get("supported") or 0),
                   "partial": int(res.get("partial") or 0), "missing": int(res.get("missing") or 0)}
    if res.get("fallback"):
        item.status, item.score = "unknown", 0
        item.note = tr("{n} arcpy calls (not checked)").format(n=len(calls))
        return item
    for name in res.get("missing_names") or []:
        item.missing.append(("arcpy", str(name)))
    for c in calls:
        if c.get("status") == "partial":
            item.missing.append(("arcpy_partial", str(c.get("name"))))
    score = res.get("score")
    item.score = int(score) if isinstance(score, (int, float)) else 0
    if item.counts["missing"]:
        item.status = "needs"
    elif item.counts["partial"]:
        item.status = "changes"
    else:
        item.status = "ok"
    item.note = tr("{n} arcpy calls: {s} supported, {p} partly, {m} missing").format(
        n=len(calls), s=item.counts["supported"], p=item.counts["partial"], m=item.counts["missing"])
    item.details = [f"{c.get('line')}: {c.get('name')} ({c.get('status')})" for c in calls
                    if c.get("status") != "supported"][:80]
    return item


def analyse_script(path):
    item = Item(path=path, kind="script")
    try:
        if os.path.getsize(path) > MAX_SCRIPT_BYTES:
            item.status, item.note = "unknown", tr("not checked: the file is very large")
            return item
        with open(path, "rb") as f:
            raw = f.read()
    except OSError as e:
        item.status, item.note = "fails", str(e)
        return item
    text = None
    for enc in ("utf-8-sig", "cp1256", "latin-1"):
        try:
            text = raw.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    return _script_item(item, analyse_script_text(text or ""))


# ---- toolboxes ------------------------------------------------------------------------------------------------
def analyse_toolbox(path):
    from . import modelbuilder as mb

    item = Item(path=path, kind="toolbox")
    try:
        box = mb.read_toolbox(path)
    except mb.ToolboxError as e:
        item.status, item.note = "fails", str(e)
        return item
    except Exception as e:  # noqa: BLE001
        item.status, item.note = "fails", str(e)
        return item
    models = box.models
    scripts = [t for t in box.tools if t.kind == "script"]
    protected = sum(1 for m in models if m.protected)
    steps = mapped = 0
    for m in models:
        for t in m.tools:
            steps += 1
            if mb.tool_status(t) == "mapped":
                mapped += 1
            else:
                item.missing.append(("tool", t))
        item.details.append(f"{m.title}: " + (tr("password-protected") if m.protected else
                                                ", ".join(m.tools[:30]) or tr("no steps")))
    script_missing = 0
    for s in scripts:
        if s.script:
            res = analyse_script_text(s.script)
            if not res.get("fallback"):
                script_missing += int(res.get("missing") or 0)
                item.missing.extend(("arcpy", str(n)) for n in res.get("missing_names") or [])
        item.details.append(f"{s.title}: " + tr("script tool"))
    if protected:
        item.missing.append(("feature", tr("password-protected models")))
    if box.kind == "tbx" and models:
        item.missing.append(("feature", tr("ArcMap toolboxes (.tbx): save as .atbx in ArcGIS Pro to convert")))
    item.counts = {"models": len(models), "scripts": len(scripts), "steps": steps, "converted_steps": mapped,
                   "protected": protected}
    readable = len(models) - protected
    item.score = int(round(100 * mapped / steps)) if steps else (0 if protected else 100)
    if not box.tools:
        item.status, item.note = "ok", tr("empty toolbox")
        item.score = 100
        return item
    if protected or (box.kind == "tbx" and models) or mapped < steps or script_missing:
        item.status = "needs"
    else:
        item.status = "ok" if readable else "unknown"
    parts = [tr("{n} models").format(n=len(models))]
    if steps:
        parts.append(tr("{done} of {total} steps convert").format(done=mapped, total=steps))
    if protected:
        parts.append(tr("{n} password-protected").format(n=protected))
    if scripts:
        parts.append(tr("{n} script tools").format(n=len(scripts)))
    item.note = ", ".join(parts)
    return item


# ---- data -----------------------------------------------------------------------------------------------------
def _ogr_quiet():
    from osgeo import gdal, ogr

    gdal.PushErrorHandler("CPLQuietErrorHandler")
    return gdal, ogr


def analyse_geodatabase(path):
    item = Item(path=path, kind="geodatabase")
    gdal, ogr = _ogr_quiet()
    try:
        if path.lower().endswith(".mdb"):
            if ogr.GetDriverByName("PGeo") is None and ogr.GetDriverByName("ODBC") is None:
                item.status, item.score = "needs", 0
                item.note = tr("personal geodatabase (.mdb): needs the Microsoft Access Database Engine")
                item.missing.append(("data", tr("personal geodatabases (.mdb)")))
                return item
        try:
            ds = gdal.OpenEx(path, gdal.OF_VECTOR | gdal.OF_READONLY)
        except RuntimeError:   # exceptions switched on elsewhere in the program
            ds = None
        if ds is None and os.path.isdir(path):
            try:
                tables = [n for n in os.listdir(path) if n.lower().endswith(".gdbtable")]
            except OSError:
                tables = []
            if tables and len(tables) <= 9:   # only the system tables: a new, empty geodatabase
                item.status, item.score, item.note = "ok", 100, tr("empty geodatabase")
                item.counts = {"tables": 0}
                return item
            if not tables:
                item.status, item.score = "fails", 0
                item.note = tr("incomplete geodatabase: the folder holds no tables")
                return item
        if ds is None:
            item.status, item.score = ("needs" if path.lower().endswith(".mdb") else "fails"), 0
            item.note = tr("could not be opened")
            if path.lower().endswith(".mdb"):
                item.note = tr("personal geodatabase (.mdb): needs the Microsoft Access Database Engine")
                item.missing.append(("data", tr("personal geodatabases (.mdb)")))
            return item
        n = ds.GetLayerCount()
        rasters = 0
        try:
            rds = gdal.OpenEx(path, gdal.OF_RASTER | gdal.OF_READONLY)
            if rds is not None:
                rasters = max(1, len(rds.GetSubDatasets())) if rds.GetSubDatasets() or rds.RasterCount else 0
        except RuntimeError:
            pass
        item.counts = {"tables": n, "rasters": rasters}
        item.status, item.score = "ok", 100
        item.note = tr("{n} feature classes and tables").format(n=n) + (
            ", " + tr("{n} rasters").format(n=rasters) if rasters else "")
        ds = None
    finally:
        gdal.PopErrorHandler()
    return item


def analyse_shapefiles(folder):
    item = Item(path=folder, kind="shapefiles")
    try:
        names = os.listdir(folder)
    except OSError as e:
        item.status, item.note = "fails", str(e)
        return item
    lower = {n.lower() for n in names}
    shp = [n for n in names if n.lower().endswith(".shp")]
    broken, no_prj = [], []
    for n in shp:
        stem = n[:-4].lower()
        if stem + ".shx" not in lower or stem + ".dbf" not in lower:
            broken.append(n)
        elif stem + ".prj" not in lower:
            no_prj.append(n)
    item.counts = {"shapefiles": len(shp), "incomplete": len(broken), "no_prj": len(no_prj)}
    item.score = int(round(100 * (len(shp) - len(broken)) / len(shp))) if shp else 100
    item.status = "needs" if broken else ("changes" if no_prj else "ok")
    parts = [tr("{n} shapefiles").format(n=len(shp))]
    if broken:
        parts.append(tr("{n} incomplete (.shx or .dbf missing)").format(n=len(broken)))
    if no_prj:
        parts.append(tr("{n} without a coordinate system (.prj)").format(n=len(no_prj)))
    item.note = ", ".join(parts)
    item.details = [tr("incomplete") + ": " + n for n in broken[:30]] + \
                   [tr("no .prj") + ": " + n for n in no_prj[:30]]
    return item


# ---- one item / everything --------------------------------------------------------------------------------------
def analyse_item(kind, path, time_limit=20.0):
    t = time.monotonic()
    try:
        if kind in ("project", "map", "layer"):
            item = analyse_map(path, kind, time_limit)
        elif kind == "script":
            item = analyse_script(path)
        elif kind == "toolbox":
            item = analyse_toolbox(path)
        elif kind == "geodatabase":
            item = analyse_geodatabase(path)
        elif kind == "shapefiles":
            item = analyse_shapefiles(path)
        elif kind == "layout":
            item = analyse_layout(path)
        else:
            item = Item(path=path, kind=kind)
    except Exception as e:  # noqa: BLE001 - one bad file must not stop the scan
        item = Item(path=path, kind=kind, status="fails", note=str(e)[:300])
    item.seconds = time.monotonic() - t
    return item


class _NoPam:
    """Rasters opened during the scan must not leave .aux.xml files next to the user's data."""

    def __enter__(self):
        from osgeo import gdal

        self.old = gdal.GetConfigOption("GDAL_PAM_ENABLED")
        gdal.SetConfigOption("GDAL_PAM_ENABLED", "NO")
        return self

    def __exit__(self, *exc):
        from osgeo import gdal

        gdal.SetConfigOption("GDAL_PAM_ENABLED", self.old)


def scan(folders, progress=None, cancel=None, time_limit=20.0, exclude=None, kinds=None):
    """Walk the folders and check every item. progress(fraction 0-1 or -1 while looking, text); cancel() -> bool."""
    start = time.monotonic()
    result = ScanResult(folders=list(folders))
    found, result.walked_dirs = find_items(folders, cancel, progress, exclude)
    if kinds:
        found = [f for f in found if f[0] in kinds]
    with _NoPam():
        for i, (kind, path) in enumerate(found):
            if cancel and cancel():
                result.cancelled = True
                break
            if progress:
                progress(i / max(len(found), 1), tr("Checking {name} ({i} of {n})").format(
                    name=os.path.basename(path), i=i + 1, n=len(found)))
            result.items.append(analyse_item(kind, path, time_limit))
    if cancel and cancel():
        result.cancelled = True
    result.seconds = time.monotonic() - start
    if progress:
        progress(1.0, tr("Done"))
    return result


# ---- summary ----------------------------------------------------------------------------------------------------
def summarise(items):
    """{kind: Counter(status)} plus totals."""
    out = {}
    for it in items:
        out.setdefault(it.kind, Counter())[it.status] += 1
    return out


def top_missing(items, n=25):
    """[(category, key, files, times)] most frequent first: functions and features that block or change items."""
    files, times = Counter(), Counter()
    for it in items:
        keys = [tuple(m) for m in it.missing]
        times.update(keys)
        files.update(set(keys))
    ranked = sorted(files, key=lambda k: (-files[k], -times[k], k))
    return [(c, k, files[(c, k)], times[(c, k)]) for c, k in ranked[:n]]


def category_label(cat):
    return {"arcpy": tr("arcpy function missing"), "arcpy_partial": tr("arcpy function partly supported"),
            "tool": tr("model tool not converted"), "layer": tr("layer type"), "symbology": tr("symbology"),
            "layout": tr("layout"), "feature": tr("feature"), "data": tr("data format")}.get(cat, cat)


def summary_text(items):
    """'412 projects and maps: 380 open 100%, 25 symbols change, 7 need work. 63 scripts: 51 run. …'"""
    s = summarise(items)
    sentences = []

    def merged(*kinds):
        c = Counter()
        for k in kinds:
            c.update(s.get(k, Counter()))
        return c

    maps = merged("project", "map", "layer")
    if maps:
        parts = [tr("{n} open 100%").format(n=maps["ok"])]
        if maps["changes"]:
            parts.append(tr("{n} open with changes (symbols or basemaps)").format(n=maps["changes"]))
        if maps["needs"]:
            parts.append(tr("{n} need work (missing data or unsupported layers)").format(n=maps["needs"]))
        if maps["fails"]:
            parts.append(tr("{n} cannot be read").format(n=maps["fails"]))
        if maps["unknown"]:
            parts.append(tr("{n} not checked").format(n=maps["unknown"]))
        sentences.append(tr("{n} projects, maps and layer files: {parts}.").format(
            n=sum(maps.values()), parts=", ".join(parts)))
    sc = s.get("script")
    if sc:
        parts = [tr("{n} run").format(n=sc["ok"])]
        if sc["changes"]:
            parts.append(tr("{n} run with small changes").format(n=sc["changes"]))
        if sc["needs"]:
            parts.append(tr("{n} use missing functions").format(n=sc["needs"]))
        if sc["unknown"]:
            parts.append(tr("{n} not checked").format(n=sc["unknown"]))
        if sc["fails"]:
            parts.append(tr("{n} cannot be read").format(n=sc["fails"]))
        sentences.append(tr("{n} arcpy scripts: {parts}.").format(n=sum(sc.values()), parts=", ".join(parts)))
    boxes = [it for it in items if it.kind == "toolbox" and it.counts.get("models")]
    if boxes:
        models = sum(it.counts.get("models", 0) for it in boxes)
        steps = sum(it.counts.get("steps", 0) for it in boxes)
        done = sum(it.counts.get("converted_steps", 0) for it in boxes)
        sentences.append(tr("{n} toolboxes with {m} models: {done} of {steps} steps convert, {ok} toolboxes "
                            "convert fully.").format(n=len(boxes), m=models, done=done, steps=steps,
                                                     ok=sum(1 for it in boxes if it.status == "ok")))
    data = merged("geodatabase", "shapefiles")
    if data:
        shp = sum(it.counts.get("shapefiles", 0) for it in items if it.kind == "shapefiles")
        gdb = sum(1 for it in items if it.kind == "geodatabase")
        sentences.append(tr("Data: {g} geodatabases and {s} shapefiles, {ok} of {n} open as they are.").format(
            g=gdb, s=shp, ok=data["ok"] + data["changes"], n=sum(data.values())))
    lay = s.get("layout")
    if lay:
        sentences.append(tr("{n} layout files to rebuild.").format(n=sum(lay.values())))
    if not sentences:
        return tr("Nothing from ArcGIS was found in these folders.")
    return " ".join(sentences)
