"""The entries of the Vault: sections, groups, item cards and friendly empty states, built from the project (main
thread, cheap reads only) and from what the worker found in folders. Facts that need reading a file come later and
are put on the entry by the pane."""
import datetime
import json
import os

from qgis.core import QgsProject, QgsRasterLayer, QgsSettings, QgsVectorLayer

from ..arabic_text import normalise
from ..i18n import tr
from .kinds import kind_name, kind_title
from .scan import dataset_size
from .usage import layer_path, norm

FOLDERS_KEY = "biruni/vault/folders"
DATABASES_KEY = "biruni/vault/databases"
REMOTE = {"postgres", "oracle", "mssql", "hana", "wfs", "oapif", "arcgisfeatureserver", "arcgismapserver", "wms",
          "xyz", "vectortile", "virtual"}


class Entry:
    """One row of the pane. row: "section", "group", "item" or "empty"."""

    def __init__(self, row, key, title, kind, subtitle="", path="", **data):
        self.row, self.key, self.title, self.kind, self.subtitle, self.path = row, key, title, kind, subtitle, path
        self.uri = data.pop("uri", "")
        self.provider = data.pop("provider", "")
        self.layer_type = data.pop("layer_type", "")       # "vector" / "raster" when it can go on the map
        self.table = data.pop("table", "")
        self.count = data.pop("count", None)               # headers: how many things
        self.facts = data.pop("facts", {}) or {}
        self.notes = data.pop("notes", "")                 # more searchable text (notes, captions, keywords)
        self.data = data                                   # layout, stop, theme, alg, action, button …
        self.badges = []                                   # [(text, level)] level: bad / warn / good / info
        self.children = []
        self.lazy = None                                   # (job function, args) that lists the children
        self.loaded = False
        self.thumb = None                                  # QPixmap
        self.strip = []                                    # QPixmaps (field photos)
        self.fade = 1.0
        self._hay = None

    def add(self, child):
        if child is not None:
            self.children.append(child)
        return child

    def droppable(self):
        return bool(self.layer_type and (self.uri or self.path))

    def haystack(self):
        if self._hay is None:
            f = self.facts
            fields = " ".join(n for ly in f.get("layers", []) for n, _t in ly.get("fields", []))
            fields += " " + " ".join(n for n, _t in f.get("fields", []))
            bits = [self.title, self.subtitle, os.path.basename(self.path or ""), kind_name(self.kind),
                    kind_title(self.kind), fields, self.notes, " ".join(b[0] for b in self.badges),
                    str(f.get("crs", "")), " ".join(ly.get("name", "") for ly in f.get("layers", []))]
            text = " ".join(b for b in bits if b)
            self._hay = (normalise(text), normalise(text, strip_prefixes=True))
        return self._hay

    def touch(self):
        self._hay = None

    def matches(self, words):
        a, b = self.haystack()
        return all(w in a or w in b for w in words)


def search_words(text):
    return [w for w in normalise(text or "").split(" ") if w]


def walk(entries):
    for e in entries:
        yield e
        yield from walk(e.children)


# ---- facts of project layers (cheap) --------------------------------------------------------------------------
def layer_entry(lyr, key_prefix="layer"):
    path, table = layer_path(lyr)
    is_vec = isinstance(lyr, QgsVectorLayer)
    kind = "vector" if is_vec else ("raster" if isinstance(lyr, QgsRasterLayer) else "layer")
    e = Entry("item", f"{key_prefix}:{lyr.id()}", lyr.name(), kind, subtitle=path and os.path.basename(path),
              path=path, uri=lyr.source(), provider=lyr.providerType(),
              layer_type="vector" if is_vec else ("raster" if kind == "raster" else ""), table=table,
              layer_id=lyr.id())
    f = {}
    crs = lyr.crs()
    if crs.isValid():
        f["crs"] = crs.authid() or crs.description()
        f["crs_name"] = crs.description()
        f["projected"] = not crs.isGeographic()
    elif not is_vec or lyr.isSpatial():
        f["crs"] = ""
        f["no_crs"] = True
    if is_vec and lyr.isValid():
        if lyr.providerType() not in REMOTE:
            n = lyr.featureCount()
            f["count"] = n if n is not None and n >= 0 else None
        f["fields"] = [(fl.name(), fl.typeName()) for fl in lyr.fields()]
        if not lyr.isSpatial():
            f.pop("no_crs", None)
            f["table_only"] = True
    if path:
        if os.path.exists(path):
            f["size"] = dataset_size(path)
            try:
                f["mtime"] = os.path.getmtime(path)
            except OSError:
                pass
        elif lyr.providerType() not in REMOTE and lyr.providerType() != "memory":
            f["missing"] = True
    if not lyr.isValid() and lyr.providerType() not in REMOTE:
        f["missing"] = True
    md = lyr.metadata()
    words = [md.abstract(), md.title()] + [k for ks in md.keywords().values() for k in ks]
    e.notes = " ".join(w for w in words if w)
    e.facts = f
    return e


def tree_entry(node, project):
    """A group of the layer tree as an item with its layers (and sub-groups) inside."""
    from qgis.core import QgsLayerTreeGroup, QgsLayerTreeLayer

    out = []
    for child in node.children():
        if isinstance(child, QgsLayerTreeLayer) and child.layer() is not None:
            out.append(layer_entry(child.layer()))
        elif isinstance(child, QgsLayerTreeGroup):
            g = Entry("item", f"group:{child.name()}:{id(child)}", child.name(), "folder",
                      subtitle=tr("Layer group"))
            g.children = tree_entry(child, project)
            g.count = sum(1 for c in walk(g.children) if c.layer_type)
            g.facts = {"layers": [], "group_count": g.count}
            out.append(g)
    return out


# ---- "This project" -----------------------------------------------------------------------------------------
def maps_group(project):
    g = Entry("group", "grp:map", kind_title("map"), "map")
    root = project.layerTreeRoot()
    layers = [n.layer() for n in root.findLayers() if n.layer() is not None]
    title = project.title() or (os.path.splitext(os.path.basename(project.fileName()))[0] if project.fileName()
                                else tr("Map"))
    m = g.add(Entry("item", "map:main", title, "map", subtitle=tr("Main map")))
    m.children = tree_entry(root, project)
    m.facts = {"layer_count": len(layers)}
    themes = project.mapThemeCollection()
    for name in themes.mapThemes():
        n = len(themes.mapThemeVisibleLayerIds(name))
        t = g.add(Entry("item", f"theme:{name}", name, "theme", subtitle=tr("Map theme"), theme=name))
        t.facts = {"layer_count": n}
    g.count = len(g.children)
    return g


def layouts_group(project):
    g = Entry("group", "grp:layout", kind_title("layout"), "layout")
    for lay in sorted(project.layoutManager().printLayouts(), key=lambda x: x.name().lower()):
        pages = lay.pageCollection()
        size = pages.page(0).pageSize() if pages.pageCount() else None
        e = g.add(Entry("item", f"layout:{lay.name()}", lay.name(), "layout", layout=lay.name()))
        e.facts = {"pages": pages.pageCount(), "atlas": bool(lay.atlas().enabled()),
                   "page_size": (round(size.width()), round(size.height())) if size else None}
    g.count = len(g.children)
    return g


def mapbooks_group(project):
    """Map Books: the Map Book feature's books (when it is there) and layouts that make a page per feature."""
    g = Entry("group", "grp:mapbook", kind_title("mapbook"), "mapbook")
    seen = set()
    try:
        import importlib

        pkg = __package__.rsplit(".", 1)[0]
        for mod_name in ("mapbook.model", "mapbook.store", "mapbook"):
            try:
                mod = importlib.import_module(f"{pkg}.{mod_name}")
            except ImportError:
                continue
            fn = getattr(mod, "books", None) or getattr(mod, "list_books", None)
            if callable(fn):
                for b in fn(project) or ():
                    name = b.get("name") if isinstance(b, dict) else getattr(b, "name", str(b))
                    name = name() if callable(name) else name
                    if name and name not in seen:
                        seen.add(name)
                        g.add(Entry("item", f"mapbook:{name}", str(name), "mapbook", subtitle=tr("Map Book"),
                                    book=name))
                break
    except Exception:  # noqa: BLE001 - a Map Book being built elsewhere must never break the Vault
        pass
    for lay in project.layoutManager().printLayouts():
        atlas = lay.atlas()
        if atlas.enabled() and lay.name() not in seen:
            cov = atlas.coverageLayer()
            e = g.add(Entry("item", f"atlas:{lay.name()}", lay.name(), "mapbook",
                            subtitle=tr("A page for every feature of {layer}").format(
                                layer=cov.name() if cov is not None else "?"), layout=lay.name()))
            e.facts = {"pages": cov.featureCount() if cov is not None else None}
    g.count = len(g.children)
    return g


def stops_group(stops):
    g = Entry("group", "grp:stop", kind_title("stop"), "stop")
    for s in stops:
        sub = " · ".join(x for x in (s.group, s.note) if x)
        e = g.add(Entry("item", f"stop:{s.id}", s.name, "stop", subtitle=sub, stop=s.id, notes=s.note))
        e.facts = {"scale": s.scale, "created": s.created}
        e.data["thumb_bytes"] = s.thumb
    g.count = len(g.children)
    return g


def charters_group(project, scores):
    from ..charter.model import charter_layers, stored_charter

    g = Entry("group", "grp:charter", kind_title("charter"), "charter")
    for lyr in sorted(charter_layers(project), key=lambda x: x.name().lower()):
        c = stored_charter(lyr) or {}
        e = g.add(Entry("item", f"charter:{lyr.id()}", lyr.name(), "charter", layer_id=lyr.id(),
                        notes=" ".join(f.get("alias_ar", "") + " " + f.get("alias_en", "")
                                       for f in c.get("fields", []))))
        e.facts = {"charter_fields": len(c.get("fields", [])), "rules": len(c.get("rules", [])),
                   "score": scores.get(lyr.id())}
    g.count = len(g.children)
    return g


def photos_folder(project=None):
    project = project or QgsProject.instance()
    folder = QgsSettings().value("biruni/field-photos/folder", "") or ""
    if folder:
        return folder
    home = project.homePath()
    if home and os.path.isdir(home):
        return os.path.join(home, "Field photos")
    return ""


def photos_group(facts):
    g = Entry("group", "grp:photos", kind_title("photos"), "photos")
    if facts and (facts.get("count") or facts.get("points")):
        e = g.add(Entry("item", "photos:folder", tr("Field photos"), "photos",
                        subtitle=facts.get("folder", ""), path=facts.get("folder", ""),
                        uri=facts.get("gpkg", "") and f"{facts['gpkg']}|layername=field_photos",
                        provider="ogr" if facts.get("gpkg") else "",
                        layer_type="vector" if facts.get("gpkg") else ""))
        e.facts = dict(facts)
        g.count = facts.get("count") or 0
    else:
        g.add(Entry("empty", "photos:empty", tr("Photos taken with a phone in the field land here as points on "
                                                "the map."), "photos", button=tr("Start field photos"),
                    action="field_photos"))
        g.count = 0
    return g


def file_item(rec, key_prefix="file"):
    kind = rec["kind"]
    p = rec["path"]
    lt = "vector" if kind in ("vector", "cad", "table") else ("raster" if kind == "raster" else "")
    e = Entry("item", f"{key_prefix}:{norm(p)}", rec["name"], kind, subtitle=rec.get("rel", ""), path=p,
              uri=p, provider="ogr" if lt == "vector" else ("gdal" if lt == "raster" else ""), layer_type=lt)
    e.facts = {"size": rec.get("size"), "mtime": rec.get("mtime")}
    e.data["sealed"] = rec.get("sealed", False)
    return e


def files_group(key, kind, records, title=None):
    g = Entry("group", key, title or kind_title(kind), kind)
    for rec in records:
        g.add(file_item(rec, key))
    g.count = len(g.children)
    return g


def indoor_group(records, project):
    g = Entry("group", "grp:indoor", kind_title("indoor"), "indoor")
    seen = set()
    for rec in records:
        if rec["name"].lower() == "indoor.gpkg":
            e = g.add(file_item(rec, "indoor"))
            e.kind = "indoor"
            e.title = os.path.basename(os.path.dirname(rec["path"])) or rec["name"]
            e.subtitle = rec.get("rel") or rec["name"]
            e.layer_type, e.uri = "", ""
            e.data["db"] = True
            seen.add(norm(rec["path"]))
    for lyr in project.mapLayers().values():
        p, _t = layer_path(lyr)
        if p and os.path.basename(p).lower() == "indoor.gpkg" and norm(p) not in seen and os.path.exists(p):
            seen.add(norm(p))
            e = g.add(Entry("item", f"indoor:{norm(p)}", os.path.basename(os.path.dirname(p)), "indoor",
                            subtitle=p, path=p, db=True))
            e.facts = {"size": dataset_size(p), "mtime": os.path.getmtime(p)}
    g.count = len(g.children)
    return g


def figures_group(project):
    g = Entry("group", "grp:figure", kind_title("figure"), "figure")
    try:
        from ..thesis.figures import figures

        figs = figures(project)
    except Exception:  # noqa: BLE001
        figs = []
    for f in figs:
        cap = f.get("caption") or f.get("caption_en") or ""
        e = g.add(Entry("item", f"figure:{f['layout']}", f"{f['label']} {cap}".strip(), "figure",
                        subtitle=f["layout"], layout=f["layout"], notes=f"{f.get('text_ar', '')} {f.get('text_en', '')}"))
        e.facts = {"chapter": f.get("chapter")}
    text, _ok = project.readEntry("biruni", "methodology_runs", "[]")
    try:
        runs = json.loads(text or "[]")
    except ValueError:
        runs = []
    if isinstance(runs, list) and runs:
        e = g.add(Entry("item", "figure:methodology", tr("Methodology record"), "figure",
                        subtitle=tr("Every tool run, ready for the methodology chapter"), command="thesis_methodology"))
        e.facts = {"steps": len(runs)}
    g.count = len(g.children)
    return g


def models_group(records):
    from qgis.core import QgsApplication

    g = Entry("group", "grp:model", kind_title("model"), "model")
    reg = QgsApplication.processingRegistry()
    for pid, kind in (("model", "model"), ("script", "script")):
        prov = reg.providerById(pid)
        if prov is None:
            continue
        for alg in sorted(prov.algorithms(), key=lambda a: a.displayName().lower()):
            g.add(Entry("item", f"alg:{alg.id()}", alg.displayName(), kind, subtitle=alg.group() or kind_name(kind),
                        alg=alg.id(), notes=alg.shortDescription() or ""))
    for rec in records:
        if rec["kind"] in ("model", "script"):
            e = g.add(file_item(rec, "mfile"))
            e.subtitle = rec.get("rel") or kind_name(rec["kind"])
    g.count = len(g.children)
    return g


def styles_group(records):
    g = Entry("group", "grp:style", kind_title("style"), "style")
    for rec in records:
        if rec["kind"] == "style":
            g.add(file_item(rec, "style"))
    g.count = len(g.children)
    return g


def sealed_group(records):
    g = Entry("group", "grp:sealed", kind_title("sealed"), "sealed")
    for rec in records:
        if rec.get("sealed"):
            e = g.add(file_item(rec, "sealed"))
            e.data["sealed"] = True
    g.count = len(g.children)
    return g


def empty(key, kind, hint, button="", action=""):
    return Entry("empty", key, hint, kind, button=button, action=action)


# ---- databases / folders ------------------------------------------------------------------------------------
def db_entry(path, subtitle="", key_prefix="db"):
    e = Entry("item", f"{key_prefix}:{norm(path)}", os.path.basename(path), "database", subtitle=subtitle, path=path,
              db=True)
    try:
        e.facts = {"size": os.path.getsize(path), "mtime": os.path.getmtime(path)}
    except OSError:
        e.facts = {"missing": True}
    return e


def saved_connections():
    """[(provider, name, uri/path)] of the database connections saved in the settings."""
    from qgis.core import QgsProviderRegistry

    out = []
    reg = QgsProviderRegistry.instance()
    for prov in ("ogr", "spatialite", "postgres"):
        md = reg.providerMetadata(prov)
        if md is None:
            continue
        try:
            conns = md.connections(False)
        except Exception:  # noqa: BLE001
            continue
        for name, conn in conns.items():
            out.append((prov, name, conn.uri()))
    return out


def user_databases():
    return [p for p in (QgsSettings().value(DATABASES_KEY, [], type=list) or []) if isinstance(p, str)]


def user_folders():
    return [p for p in (QgsSettings().value(FOLDERS_KEY, [], type=list) or []) if isinstance(p, str)]


def add_setting_path(key, path):
    items = [p for p in (QgsSettings().value(key, [], type=list) or []) if isinstance(p, str)]
    if norm(path) not in {norm(p) for p in items}:
        items.append(os.path.normpath(path))
        QgsSettings().setValue(key, items)
    return items


def remove_setting_path(key, path):
    items = [p for p in (QgsSettings().value(key, [], type=list) or []) if isinstance(p, str)
             and norm(p) != norm(path)]
    QgsSettings().setValue(key, items)
    return items


def today_stamp(t):
    try:
        return datetime.date.fromtimestamp(t) == datetime.date.today()
    except (TypeError, ValueError, OSError):
        return False
