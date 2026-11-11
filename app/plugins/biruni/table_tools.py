"""Spreadsheet-grade attribute table helpers.

* Field order: in the attribute table only (non-destructive) or rewritten in the
  data itself (GeoPackage, shapefile, File Geodatabase, memory) - with a backup
  of the files first and the layer's own settings (aliases, widgets, defaults)
  carried over. ArcGIS Pro's most voted idea is "permanently reorder fields".
* Highlighting presets for the attribute table (conditional styles): empty
  values, duplicates, values outside a range, a colour scale, rows by value.
* Natural sort ("2" before "10", Arabic digits) and type-ahead editing.
* A live join on several fields (composite key) through virtual fields.

Everything here works on QgsVectorLayer objects only - the dialogs live in
table_tools_dialog.py.
"""
import json
import os
import re
import shutil
import time

from qgis.core import (Qgis, QgsAttributeTableConfig, QgsConditionalStyle, QgsDefaultValue, QgsEditorWidgetSetup,
                       QgsExpression, QgsFeatureRequest, QgsField, QgsFieldConstraints, QgsProviderRegistry,
                       QgsVectorLayer, QgsVectorLayerJoinInfo, qgsfunction)
from qgis.PyQt.QtCore import QMetaType, Qt
from qgis.PyQt.QtGui import QColor

from . import arabic_text
from .i18n import tr

STYLE_TAG = "Biruni: "                       # names of the conditional styles written here
BACKUPS_PROP = "biruni/table_backups"        # layer custom property: list of backups (JSON)
AUTOCOMPLETE_PROP = "biruni/autocomplete"    # layer custom property: previous editor widgets (JSON)
LIVE_JOIN_PROP = "biruni/live_joins"         # layer custom property: live joins made here (JSON)
TEMP_PREFIX = "_bz"                          # temporary field names while rewriting (shapefile-safe)
BATCH = 5000


# ---------------------------------------------------------------- field order
def provider_fields(layer):
    """Names of the fields stored in the data (not virtual or joined), and the
    names that cannot move (the GeoPackage / database primary key)."""
    fields = layer.fields()
    names = [f.name() for i, f in enumerate(fields) if fields.fieldOrigin(i) == Qgis.FieldOrigin.Provider]
    pf = layer.dataProvider().fields()
    locked = [pf.at(i).name() for i in layer.dataProvider().pkAttributeIndexes() if 0 <= i < pf.count()]
    return names, locked


def view_order(layer, order, hidden=()):
    """Column order and visibility in the attribute table only - the data is not touched."""
    cfg = layer.attributeTableConfig()
    cfg.update(layer.fields())  # drops columns of fields that no longer exist, adds new ones
    cols = list(cfg.columns())
    by_name = {c.name: c for c in cols if c.type == QgsAttributeTableConfig.Type.Field}
    rest = [c for c in cols if c.type != QgsAttributeTableConfig.Type.Field]
    new = []
    for name in order:
        c = by_name.pop(name, None)
        if c is not None:
            c.hidden = name in hidden
            new.append(c)
    for name, c in by_name.items():
        c.hidden = name in hidden
        new.append(c)
    cfg.setColumns(new + rest)
    layer.setAttributeTableConfig(cfg)


def table_order(layer):
    """(names in the attribute table order, hidden names)."""
    cfg = layer.attributeTableConfig()
    cfg.update(layer.fields())
    cols = [c for c in cfg.columns()
            if c.type == QgsAttributeTableConfig.Type.Field and layer.fields().indexOf(c.name) >= 0]
    names = [c.name for c in cols]
    names += [f.name() for f in layer.fields() if f.name() not in names]
    return names, {c.name for c in cols if c.hidden}


def can_rewrite(layer):
    """(True, "") when the field order can be rewritten in the data, else (False, reason)."""
    if not isinstance(layer, QgsVectorLayer) or not layer.isValid():
        return False, tr("This is not a valid vector layer.")
    if layer.isEditable() or layer.isModified():
        return False, tr("Save or discard the edits of this layer first.")
    p = layer.dataProvider()
    caps = p.capabilities()
    C = Qgis.VectorProviderCapability
    if not all(caps & c for c in (C.AddAttributes, C.DeleteAttributes, C.RenameAttributes,
                                   C.ChangeAttributeValues)):
        return False, tr("This kind of data cannot change its fields (read-only or a service).")
    if p.name() == "ogr" and not _data_files(layer):
        return False, tr("The file of this layer was not found.")
    if p.name() not in ("ogr", "memory"):
        return False, tr("Only files (GeoPackage, shapefile, File Geodatabase) and temporary layers can be "
                         "rewritten; use the attribute table order instead.")
    return True, ""


def _data_files(layer):
    """Files to back up for a file-based layer: [(kind, path)]; kind is 'file' or 'dir'."""
    parts = QgsProviderRegistry.instance().decodeUri(layer.providerType(), layer.source())
    path = parts.get("path") or ""
    if not path or not os.path.exists(path):
        return []
    if os.path.isdir(path):  # File Geodatabase
        return [("dir", path)]
    stem, ext = os.path.splitext(path)
    if ext.lower() == ".shp":
        folder = os.path.dirname(path) or "."
        base = os.path.basename(stem).lower()
        return [("file", os.path.join(folder, n)) for n in sorted(os.listdir(folder))
                if os.path.splitext(n)[0].lower() == base and not n.lower().endswith((".qix", ".sbn", ".sbx"))]
    return [("file", p) for p in (path, path + "-wal", path + "-shm") if os.path.exists(p)]


def backups(layer):
    try:
        items = json.loads(layer.customProperty(BACKUPS_PROP, "[]") or "[]")
    except (TypeError, ValueError):
        items = []
    return [b for b in items if os.path.isdir(b.get("dir", ""))]


def backup(layer):
    """Copies the layer's files into <folder>/.biruni-backup/<name>-<time>/ and remembers it on the layer."""
    files = _data_files(layer)
    if not files:
        return None
    first = files[0][1]
    folder = os.path.dirname(first.rstrip("\\/")) or "."
    stamp = time.strftime("%Y%m%d-%H%M%S")
    name = os.path.splitext(os.path.basename(first.rstrip("\\/")))[0]
    target = os.path.join(folder, ".biruni-backup", f"{name}-{stamp}")
    n = 2
    while os.path.exists(target):
        target = os.path.join(folder, ".biruni-backup", f"{name}-{stamp}-{n}")
        n += 1
    os.makedirs(target)
    copied = []
    for kind, path in files:
        dest = os.path.join(target, os.path.basename(path.rstrip("\\/")))
        if kind == "dir":
            shutil.copytree(path, dest)
        else:
            shutil.copy2(path, dest)
        copied.append([kind, path, dest])
    entry = {"dir": target, "time": time.strftime("%Y-%m-%d %H:%M"), "files": copied}
    items = backups(layer) + [entry]
    layer.setCustomProperty(BACKUPS_PROP, json.dumps(items[-10:], ensure_ascii=False))
    return entry


def restore_backup(layer, entry):
    """Puts the backed-up files back and reloads the layer."""
    if layer.isEditable():
        raise RuntimeError(tr("Save or discard the edits of this layer first."))
    source, name, provider = layer.source(), layer.name(), layer.providerType()
    # point the layer away so its provider closes the files before they are replaced
    layer.setDataSource("Point?crs=EPSG:4326", name, "memory")
    try:
        for kind, original, saved in entry["files"]:
            if kind == "dir":
                if os.path.isdir(original):
                    shutil.rmtree(original)
                shutil.copytree(saved, original)
            else:
                shutil.copy2(saved, original)
        for suffix in ("-wal", "-shm"):  # SQLite journals from after the backup no longer belong to the file
            originals = {o for k, o, s in entry["files"]}
            for _kind, original, _saved in entry["files"]:
                j = original + suffix
                if j not in originals and os.path.exists(j):
                    os.remove(j)
    finally:
        layer.setDataSource(source, name, provider)
    layer.reload()
    layer.triggerRepaint()


class RewriteResult:
    def __init__(self):
        self.changed = False
        self.backup = None          # backup entry for files, or the inverse step for memory layers
        self.dropped = []           # data-level constraints that could not be carried over
        self.renamed = {}           # old -> new


def rewrite_fields(layer, order, renames=None):
    """Rewrites the fields of the data in `order` (field names; missing ones keep
    their relative place at the end), renaming per `renames` {old: new}. The
    primary key stays first. Feature ids, geometries and values are kept.

    Works through the layer's own provider (add the fields in the new order,
    copy the values, delete the old ones, rename) so it never fights QGIS over
    an open file. File-based data are backed up first and put back if anything
    fails."""
    ok, why = can_rewrite(layer)
    if not ok:
        raise RuntimeError(why)
    renames = {k: v.strip() for k, v in (renames or {}).items() if v and v.strip() and v.strip() != k}
    names, locked = provider_fields(layer)
    movable = [n for n in names if n not in locked]
    wanted = [n for n in order if n in movable]
    wanted += [n for n in movable if n not in wanted]
    final = [renames.get(n, n) for n in wanted]
    result = RewriteResult()
    if wanted == movable and not renames:
        return result
    taken = set(locked) | {f.name() for i, f in enumerate(layer.fields())
                           if layer.fields().fieldOrigin(i) != Qgis.FieldOrigin.Provider}
    if len({n.lower() for n in final}) != len(final) or any(n.lower() in {t.lower() for t in taken} for n in final):
        raise RuntimeError(tr("Two fields would have the same name."))
    p = layer.dataProvider()
    shapefile = p.storageType().lower().startswith("esri shapefile")
    if shapefile and any(len(n.encode("utf-8")) > 10 for n in final):
        raise RuntimeError(tr("Shapefile field names are limited to 10 characters (5 Arabic letters)."))

    saved = _layer_settings(layer)
    if p.name() == "memory":  # nothing to copy: undoing is the reverse rewrite
        result.backup = {"memory": True, "order": movable, "renames": renames}
    else:
        result.backup = backup(layer)
        if result.backup is None:
            raise RuntimeError(tr("The file of this layer was not found."))
    pf = p.fields()
    for n in wanted:
        c = pf.field(n).constraints()
        for flag, label in ((QgsFieldConstraints.Constraint.ConstraintNotNull, "not null"),
                            (QgsFieldConstraints.Constraint.ConstraintUnique, "unique")):
            if c.constraintOrigin(flag) == QgsFieldConstraints.ConstraintOrigin.ConstraintOriginProvider:
                result.dropped.append(f"{n}: {label}")
    try:
        _rewrite(layer, wanted, final)
    except Exception:
        if p.name() != "memory" and result.backup:
            restore_backup(layer, result.backup)
        raise
    result.changed = True
    result.renamed = renames
    _apply_layer_settings(layer, saved, renames)
    view_order(layer, list(locked) + final + [n for n in table_order(layer)[0] if n not in final and n not in locked])
    return result


def _rewrite(layer, wanted, final):
    p = layer.dataProvider()
    pf = p.fields()
    temps = [f"{TEMP_PREFIX}{i}" for i in range(len(wanted))]
    used = {f.name().lower() for f in pf}
    for i, t in enumerate(temps):
        while t.lower() in used:
            t += "x"
        temps[i] = t
        used.add(t.lower())
    new_fields = []
    for n, t in zip(wanted, temps):
        f = QgsField(pf.field(n))
        f.setName(t)
        f.setConstraints(QgsFieldConstraints())
        new_fields.append(f)
    if not p.addAttributes(new_fields):
        raise RuntimeError(tr("The data refused the new fields: {e}").format(e="; ".join(p.errors()[-3:])))
    layer.updateFields()
    pf = p.fields()
    src = [pf.indexOf(n) for n in wanted]
    dst = [pf.indexOf(t) for t in temps]
    if min(dst) < 0:
        raise RuntimeError(tr("The data refused the new fields: {e}").format(e="; ".join(p.errors()[-3:])))
    req = QgsFeatureRequest().setFlags(QgsFeatureRequest.Flag.NoGeometry).setSubsetOfAttributes(src)
    batch = {}
    for feat in p.getFeatures(req):
        attrs = feat.attributes()
        batch[feat.id()] = {d: attrs[s] for s, d in zip(src, dst)}
        if len(batch) >= BATCH:
            if not p.changeAttributeValues(batch):
                raise RuntimeError("; ".join(p.errors()[-3:]))
            batch = {}
    if batch and not p.changeAttributeValues(batch):
        raise RuntimeError("; ".join(p.errors()[-3:]))
    if not p.deleteAttributes(src):
        raise RuntimeError(tr("The old fields could not be removed: {e}").format(e="; ".join(p.errors()[-3:])))
    layer.updateFields()
    pf = p.fields()
    if not p.renameAttributes({pf.indexOf(t): n for t, n in zip(temps, final)}):
        raise RuntimeError(tr("The fields could not be renamed: {e}").format(e="; ".join(p.errors()[-3:])))
    layer.updateFields()
    layer.reload()


def undo_rewrite(layer, result):
    """Back to the state before rewrite_fields."""
    if not result or not result.backup:
        return
    if result.backup.get("memory"):
        order, renames = result.backup["order"], result.backup["renames"]
        rewrite_fields(layer, [renames.get(n, n) for n in order], {new: old for old, new in renames.items()})
    else:
        restore_backup(layer, result.backup)


def _layer_settings(layer):
    """Per-field settings kept by the layer (not the data), by field name."""
    out = {}
    fields = layer.fields()
    for i, f in enumerate(fields):
        if fields.fieldOrigin(i) != Qgis.FieldOrigin.Provider:
            continue
        c = f.constraints()
        out[f.name()] = {
            "alias": layer.attributeAlias(i),
            "widget": layer.editorWidgetSetup(i),
            "default": layer.defaultValueDefinition(i),
            "notnull": c.constraintOrigin(QgsFieldConstraints.Constraint.ConstraintNotNull)
            == QgsFieldConstraints.ConstraintOrigin.ConstraintOriginLayer,
            "unique": c.constraintOrigin(QgsFieldConstraints.Constraint.ConstraintUnique)
            == QgsFieldConstraints.ConstraintOrigin.ConstraintOriginLayer,
            "expression": (c.constraintExpression(), c.constraintDescription()),
            "styles": layer.conditionalStyles().fieldStyles(f.name()),
        }
    return out


def _apply_layer_settings(layer, saved, renames):
    for old, s in saved.items():
        name = renames.get(old, old)
        i = layer.fields().indexOf(name)
        if i < 0:
            continue
        if s["alias"]:
            layer.setFieldAlias(i, s["alias"])
        if s["widget"].type():
            layer.setEditorWidgetSetup(i, s["widget"])
        if s["default"].isValid():
            layer.setDefaultValueDefinition(i, s["default"])
        if s["notnull"]:
            layer.setFieldConstraint(i, QgsFieldConstraints.Constraint.ConstraintNotNull)
        if s["unique"]:
            layer.setFieldConstraint(i, QgsFieldConstraints.Constraint.ConstraintUnique)
        if s["expression"][0]:
            layer.setConstraintExpression(i, *s["expression"])
        if s["styles"]:
            layer.conditionalStyles().setFieldStyles(name, s["styles"])


def set_aliases(layer, aliases):
    """{field name: alias}; an empty alias removes it."""
    for name, alias in aliases.items():
        i = layer.fields().indexOf(name)
        if i < 0:
            continue
        if alias:
            layer.setFieldAlias(i, alias)
        else:
            layer.removeFieldAlias(i)


# ---------------------------------------------------------------- highlighting
EMPTY_BG, DUP_BG, RANGE_BG = "#f8c8c8", "#ffd8a8", "#f9e79f"
SCALE = ["#d8f0d2", "#b9e3a8", "#f3e79b", "#f6c28b", "#ef9a9a"]
PASTELS = ["#cfe2f3", "#d9ead3", "#fff2cc", "#f4cccc", "#d9d2e9", "#fce5cd", "#d0e0e3", "#ead1dc",
           "#e6f2c2", "#ffe0b2", "#c9daf8", "#e1d5e7"]


def _style(rule, name, background, text=None):
    s = QgsConditionalStyle(rule)
    s.setName(STYLE_TAG + name)
    s.setBackgroundColor(QColor(background))
    if text:
        s.setTextColor(QColor(text))
    return s


def _ours(style):
    return style.name().startswith(STYLE_TAG)


def _set_field_styles(layer, field, new):
    styles = layer.conditionalStyles()
    keep = [s for s in styles.fieldStyles(field) if not (_ours(s) and s.name() in {n.name() for n in new})]
    styles.setFieldStyles(field, keep + new)


def _ref(field):
    return QgsExpression.quotedColumnRef(field)


def highlight_empty(layer, fields=None):
    """Empty cells in red. Without fields: the required fields, or every field when none is required."""
    if not fields:
        fields = [f.name() for f in layer.fields()
                  if f.constraints().constraints() & QgsFieldConstraints.Constraint.ConstraintNotNull]
        if not fields:
            fields = [f.name() for f in layer.fields()]
    for name in fields:
        _set_field_styles(layer, name, [_style("@value IS NULL OR trim(to_string(@value)) = ''",
                                               tr("empty"), EMPTY_BG)])
    return len(fields)


def highlight_duplicates(layer, field):
    rule = f"@value IS NOT NULL AND count({_ref(field)}, group_by:={_ref(field)}) > 1"
    _set_field_styles(layer, field, [_style(rule, tr("duplicate"), DUP_BG)])


def highlight_range(layer, field, minimum=None, maximum=None):
    parts = []
    if minimum is not None:
        parts.append(f"@value < {float(minimum)!r}")
    if maximum is not None:
        parts.append(f"@value > {float(maximum)!r}")
    if not parts:
        raise ValueError(tr("Give a minimum, a maximum or both."))
    _set_field_styles(layer, field, [_style(" OR ".join(parts), tr("outside the range"), RANGE_BG)])


def colour_scale(layer, field, steps=5):
    i = layer.fields().indexOf(field)
    lo, hi = layer.minimumAndMaximumValue(i)
    try:
        lo, hi = float(lo), float(hi)
    except (TypeError, ValueError):
        raise ValueError(tr("{f} has no numbers to colour.").format(f=field))
    steps = max(2, min(int(steps), len(SCALE)))
    colours = SCALE if steps == len(SCALE) else [SCALE[round(k * (len(SCALE) - 1) / (steps - 1))]
                                                 for k in range(steps)]
    width = (hi - lo) / steps if hi > lo else 1.0
    new = []
    for k in range(steps):
        a = lo + k * width
        b = lo + (k + 1) * width
        cond = f"@value >= {a!r} AND " + (f"@value <= {hi!r}" if k == steps - 1 else f"@value < {b!r}")
        new.append(_style(cond, f"{tr('scale')} {k + 1}", colours[k]))
    styles = layer.conditionalStyles()
    keep = [s for s in styles.fieldStyles(field) if not (_ours(s) and s.name().startswith(STYLE_TAG + tr("scale")))]
    styles.setFieldStyles(field, keep + new)
    return lo, hi


def colour_rows_by(layer, field, limit=len(PASTELS)):
    i = layer.fields().indexOf(field)
    values = sorted((v for v in layer.uniqueValues(i, limit + 1) if v is not None and str(v) != "NULL"), key=str)
    styles = layer.conditionalStyles()
    keep = [s for s in styles.rowStyles() if not _ours(s)]
    new = [_style(QgsExpression.createFieldEqualityExpression(field, v), f"{field} = {v}", PASTELS[k % len(PASTELS)])
           for k, v in enumerate(values[:limit])]
    styles.setRowStyles(keep + new)
    return len(new), len(values) > limit


def clear_highlighting(layer, field=None):
    """Removes the highlighting made by Biruni (others' rules stay)."""
    styles = layer.conditionalStyles()
    names = [field] if field else [f.name() for f in layer.fields()]
    n = 0
    for name in names:
        current = styles.fieldStyles(name)
        keep = [s for s in current if not _ours(s)]
        if len(keep) != len(current):
            n += len(current) - len(keep)
            styles.setFieldStyles(name, keep)
    if not field:
        rows = styles.rowStyles()
        keep = [s for s in rows if not _ours(s)]
        n += len(rows) - len(keep)
        styles.setRowStyles(keep)
    return n


def highlighting(layer):
    """[(field or None for rows, style name)] of the Biruni highlighting on a layer."""
    styles = layer.conditionalStyles()
    out = [(f.name(), s.name()) for f in layer.fields() for s in styles.fieldStyles(f.name()) if _ours(s)]
    return out + [(None, s.name()) for s in styles.rowStyles() if _ours(s)]


# ---------------------------------------------------------------- natural sort
_RUN = re.compile(r"\d+")
_registered = []


def natural_key_value(value):
    """A text that sorts like a person does: numbers by size ("2" < "10"), Arabic
    digits as numbers, letters without case or alef/ya/ta-marbuta forms."""
    if value is None:
        return None
    text, _ = arabic_text.light_normalise(str(value))
    text = text.lower().strip()
    return _RUN.sub(lambda m: m.group().lstrip("0").rjust(20, "0"), text)


def _natural_key(value, feature, parent):
    """Returns a text that sorts numbers inside text by their size ("plot 2" before "plot 10"),
    Arabic-Indic digits included. <h4>Example</h4> natural_key("parcel_no")"""
    return natural_key_value(value)


def _arabic_normalise(value, feature, parent):
    """Returns the text ready for matching: lower case, one form of alef, ya and ta marbuta,
    no tashkeel, Arabic digits as 0-9, single spaces. <h4>Example</h4> arabic_normalise("name")"""
    return None if value is None else arabic_text.normalise(value)


_retired = []  # unregistered functions: QGIS has already freed them, Python must never free them again


def register_functions():
    """Expression functions natural_key() and arabic_normalise() (group Biruni)."""
    if _registered:
        return
    for name, fn in (("natural_key", _natural_key), ("arabic_normalise", _arabic_normalise)):
        if QgsExpression.isFunctionName(name):
            continue
        fn.__name__ = name
        _registered.append(qgsfunction(group="Biruni", handlesnull=True)(fn))


def unregister_functions():
    """Unregistering deletes the function in QGIS; the Python wrapper is kept alive on purpose - letting it be
    garbage collected freed the same object a second time and crashed QGIS on exit (heap corruption)."""
    while _registered:
        f = _registered.pop()
        QgsExpression.unregisterFunction(f.name())
        _retired.append(f)


def sort_naturally(layer, field, descending=False):
    register_functions()
    cfg = layer.attributeTableConfig()
    if not cfg.columns():
        cfg.update(layer.fields())
    cfg.setSortExpression(f"natural_key({_ref(field)})")
    cfg.setSortOrder(Qt.SortOrder.DescendingOrder if descending else Qt.SortOrder.AscendingOrder)
    layer.setAttributeTableConfig(cfg)


# ---------------------------------------------------------------- type-ahead editing
def _saved_widgets(layer):
    try:
        return json.loads(layer.customProperty(AUTOCOMPLETE_PROP, "{}") or "{}")
    except (TypeError, ValueError):
        return {}


def enable_autocomplete(layer, fields):
    """Typing in these fields offers the values already used (still free to type a new one)."""
    saved = _saved_widgets(layer)
    n = 0
    for name in fields:
        i = layer.fields().indexOf(name)
        if i < 0:
            continue
        prev = layer.editorWidgetSetup(i)
        if prev.type() == "UniqueValues" and name in saved:
            continue
        try:
            saved[name] = {"type": prev.type(), "config": json.loads(json.dumps(prev.config()))}
        except (TypeError, ValueError):
            saved[name] = {"type": prev.type(), "config": {}}
        layer.setEditorWidgetSetup(i, QgsEditorWidgetSetup("UniqueValues", {"Editable": True}))
        n += 1
    layer.setCustomProperty(AUTOCOMPLETE_PROP, json.dumps(saved, ensure_ascii=False))
    return n


def disable_autocomplete(layer, fields=None):
    saved = _saved_widgets(layer)
    names = list(fields or saved.keys())
    for name in names:
        i = layer.fields().indexOf(name)
        prev = saved.pop(name, None)
        if i < 0:
            continue
        if prev and prev.get("type"):
            layer.setEditorWidgetSetup(i, QgsEditorWidgetSetup(prev["type"], prev.get("config") or {}))
        else:
            layer.setEditorWidgetSetup(i, QgsEditorWidgetSetup())
    layer.setCustomProperty(AUTOCOMPLETE_PROP, json.dumps(saved, ensure_ascii=False))
    return len(names)


def autocomplete_fields(layer):
    return [f.name() for i, f in enumerate(layer.fields()) if layer.editorWidgetSetup(i).type() == "UniqueValues"]


# ---------------------------------------------------------------- live join on several fields
def key_expression(fields, normalise=False):
    """One text made of several fields, for matching: NULL when any part is empty."""
    parts = []
    for f in fields:
        v = f"to_string({_ref(f)})"
        parts.append(f"nullif(arabic_normalise({v}), '')" if normalise else f"nullif(trim({v}), '')")
    return " || '␟' || ".join(parts)


def _free_name(layer, base):
    name, n = base, 2
    while layer.fields().indexOf(name) >= 0:
        name = f"{base}_{n}"
        n += 1
    return name


def create_live_join(layer, join_layer, pairs, fields=None, prefix="", normalise=False):
    """A join that stays live (follows edits in either layer) on several field pairs
    [(layer field, join layer field)]. Each layer gets a virtual key field; QGIS's
    ordinary join then matches the keys. Needs Biruni's expression functions when
    `normalise` is on, so a project opened elsewhere shows the join empty."""
    if not 1 <= len(pairs) <= 4:
        raise ValueError(tr("Choose one to four pairs of fields."))
    if normalise:
        register_functions()
    tkey = _free_name(layer, "biruni_key")
    jkey = _free_name(join_layer, "biruni_key")
    layer.addExpressionField(key_expression([a for a, _ in pairs], normalise), QgsField(tkey, QMetaType.Type.QString))
    join_layer.addExpressionField(key_expression([b for _, b in pairs], normalise),
                                  QgsField(jkey, QMetaType.Type.QString))
    info = QgsVectorLayerJoinInfo()
    info.setJoinLayer(join_layer)
    info.setJoinFieldName(jkey)
    info.setTargetFieldName(tkey)
    info.setUsingMemoryCache(True)
    info.setPrefix(prefix)
    names = list(fields) if fields else [f.name() for i, f in enumerate(join_layer.fields())
                                         if f.name() != jkey and join_layer.fields().fieldOrigin(i)
                                         != Qgis.FieldOrigin.Expression]
    info.setJoinFieldNamesSubset(names)
    if not layer.addJoin(info):
        raise RuntimeError(tr("The join could not be made."))
    try:
        joins = json.loads(layer.customProperty(LIVE_JOIN_PROP, "[]") or "[]")
    except (TypeError, ValueError):
        joins = []
    joins.append({"join": join_layer.id(), "key": tkey, "join_key": jkey})
    layer.setCustomProperty(LIVE_JOIN_PROP, json.dumps(joins))
    return tkey, jkey


def remove_live_joins(layer, join_layer=None):
    try:
        joins = json.loads(layer.customProperty(LIVE_JOIN_PROP, "[]") or "[]")
    except (TypeError, ValueError):
        joins = []
    from qgis.core import QgsProject

    keep = []
    for j in joins:
        if join_layer is not None and j["join"] != join_layer.id():
            keep.append(j)
            continue
        layer.removeJoin(j["join"])
        i = layer.fields().indexOf(j["key"])
        if i >= 0:
            layer.removeExpressionField(i)
        other = QgsProject.instance().mapLayer(j["join"])
        if other is not None:
            k = other.fields().indexOf(j["join_key"])
            if k >= 0:
                other.removeExpressionField(k)
    layer.setCustomProperty(LIVE_JOIN_PROP, json.dumps(keep))
    return len(joins) - len(keep)
