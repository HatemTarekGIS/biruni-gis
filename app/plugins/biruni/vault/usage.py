"""Where a file is used in the project (layers, layouts, map themes, Map Stops), and renaming / deleting files
safely (a shapefile is renamed or deleted with all its parts; its seal follows it)."""
import os

from qgis.core import QgsLayoutItemMap, QgsProject, QgsProviderRegistry

from .kinds import SEAL_SUFFIXES
from .scan import sidecars


def norm(path):
    return os.path.normcase(os.path.abspath(path)) if path else ""


def layer_path(layer):
    """(file path, table/layer name) of a layer's data, or ("", "")."""
    try:
        parts = QgsProviderRegistry.instance().decodeUri(layer.providerType(), layer.source())
    except Exception:  # noqa: BLE001
        return "", ""
    return parts.get("path") or "", parts.get("layerName") or ""


def layers_of(path, project=None, table=None):
    """The project's layers whose data is this file (and this table of it, when given)."""
    project = project or QgsProject.instance()
    key = norm(path)
    out = []
    for lyr in project.mapLayers().values():
        p, t = layer_path(lyr)
        if p and norm(p) == key and (table is None or not t or t == table):
            out.append(lyr)
    return out


def _visible_layer_ids(project):
    root = project.layerTreeRoot()
    return {n.layerId() for n in root.findLayers() if n.isVisible()}


def where_used(path, project=None, table=None, stops=None):
    """[(kind, name)] - kind is "layer", "layout", "theme" or "stop" - of everything that uses the file."""
    project = project or QgsProject.instance()
    layers = layers_of(path, project, table)
    ids = {lyr.id() for lyr in layers}
    out = [("layer", lyr.name()) for lyr in layers]
    if not ids:
        return out
    themes = project.mapThemeCollection()
    theme_hits = []
    for name in themes.mapThemes():
        if ids & set(themes.mapThemeVisibleLayerIds(name)):
            theme_hits.append(name)
    visible = None
    for lay in project.layoutManager().printLayouts():
        used = False
        for item in lay.items():
            if not isinstance(item, QgsLayoutItemMap):
                continue
            if item.keepLayerSet():
                shown = {lyr.id() for lyr in item.layers() if lyr is not None}
            elif item.followVisibilityPreset() and item.followVisibilityPresetName():
                shown = set(themes.mapThemeVisibleLayerIds(item.followVisibilityPresetName()))
            else:
                if visible is None:
                    visible = _visible_layer_ids(project)
                shown = visible
            if ids & shown:
                used = True
                break
        if used:
            out.append(("layout", lay.name()))
    out += [("theme", t) for t in theme_hits]
    for s in stops or ():
        if (s.theme and s.theme in theme_hits) or (ids & set(s.layers or ())):
            out.append(("stop", s.name))
    return out


# ---- files ---------------------------------------------------------------------------------------------------
def parts_of(path):
    """Every file that makes the dataset, with its seal files."""
    files = [] if os.path.isdir(path) else sidecars(path)
    if path not in files:
        files.insert(0, path)
    for f in list(files):
        for suf in SEAL_SUFFIXES:
            if os.path.exists(f + suf) and f + suf not in files:
                files.append(f + suf)
    return files


def rename_dataset(path, new_stem):
    """Renames a dataset with all its parts (a.shp, a.dbf, a.shp.seal.json …). Returns the new path.
    Raises FileExistsError / ValueError / OSError."""
    new_stem = new_stem.strip()
    if not new_stem or any(c in new_stem for c in '\\/:*?"<>|'):
        raise ValueError(new_stem)
    folder = os.path.dirname(path)
    old_name = os.path.basename(path)
    old_stem, ext = os.path.splitext(old_name)
    moves = []
    for f in parts_of(path):
        name = os.path.basename(f)
        if name.lower().startswith(old_stem.lower()):
            moves.append((f, os.path.join(folder, new_stem + name[len(old_stem):])))
    for _src, dst in moves:
        if os.path.exists(dst):
            raise FileExistsError(dst)
    for src, dst in moves:
        os.rename(src, dst)
    return os.path.join(folder, new_stem + ext)


def delete_dataset(path, trash=True):
    """Moves a dataset with all its parts to the Recycle Bin (deletes it where there is none, or when trash is
    False). Returns the files."""
    from qgis.PyQt.QtCore import QFile

    files = parts_of(path)
    for f in files:
        if not os.path.exists(f):
            continue
        ok = False
        if trash:
            try:
                res = QFile.moveToTrash(f)
                ok = bool(res[0] if isinstance(res, tuple) else res)
            except Exception:  # noqa: BLE001
                ok = False
        if not ok and os.path.exists(f):
            if os.path.isdir(f):
                import shutil

                shutil.rmtree(f)
            else:
                os.remove(f)
    return files
