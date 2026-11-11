"""Writing a charter into QGIS: the fields of the data, then aliases, edit widgets, constraints, default values,
the kind symbols and the lookup table of the linked choices.

Everything is a standard QGIS mechanism, so the charter works in every QGIS form and in QField:

* value list         -> ValueMap widget (Range for a range list) + a constraint expression
* kinds              -> default values ``CASE WHEN "kind" = … THEN … END``, ValueRelation filters on
                        ``current_value('kind')``, constraint expressions, a categorized renderer
* linked choices     -> ValueRelation over the lookup table ``biruni_charter_choices`` filtered on
                        ``current_value(parent)``, + a constraint expression of the allowed pairs
* validation rules   -> part of the constraint expression of their field (errors: hard, warnings: soft)
* required / unique / automatic values -> rules.apply_rules (Biruni's attribute rules)
"""
import os

from qgis.core import (Qgis, QgsCategorizedSymbolRenderer, QgsDefaultValue, QgsEditorWidgetSetup, QgsExpression,
                       QgsFeature, QgsField, QgsFieldConstraints, QgsProject, QgsRendererCategory, QgsSymbol,
                       QgsVectorFileWriter, QgsVectorLayer)
from qgis.PyQt.QtCore import QMetaType
from qgis.PyQt.QtGui import QColor

from ..i18n import tr
from . import model
from .model import TAG, NUMERIC, pick

CHOICES_PROP = "biruni/charter-choices"
CHOICES_TABLE = "biruni_charter_choices"
SEP = "|"      # between the values of several parent fields
PAIR = "#"     # between the parent key and the child value
INLINE_PAIRS = 3000


# ---- expression helpers ------------------------------------------------------------------------------------
def qn(name):
    return '"' + str(name).replace('"', '""') + '"'


def ql(value):
    return "'" + str(value).replace("'", "''") + "'"


def literal(value, numeric=False):
    """An expression literal for a value typed by the user (numbers stay numbers in numeric fields)."""
    if value is None or str(value) == "":
        return "NULL"
    if numeric:
        try:
            v = float(str(value).replace(",", "."))
            return str(int(v)) if v.is_integer() else repr(v)
        except ValueError:
            pass
    return ql(value)


def in_list(codes, numeric=False):
    return "(" + ", ".join(literal(c, numeric) for c in codes) + ")"


def _numeric(spec):
    return (spec or {}).get("type") in NUMERIC


def _text_of(name, numeric=False):
    return f"coalesce(to_string({qn(name)}), '')"


def parent_key_expr(parents, form=False):
    """The key of the parent values: "a|b" (in the form: from current_value())."""
    parts = [f"coalesce(to_string(current_value({ql(p)})), '')" if form else _text_of(p) for p in parents]
    return f" || '{SEP}' || ".join(parts) if parts else "''"


# ---- the checks of a charter (shared by the constraints and the health score) ------------------------------
class Check:
    """One rule a feature must keep. ``expr`` is TRUE when the feature is fine (NULL handled inside)."""

    def __init__(self, cid, field, expr, title, level="error", kind="", hard=True):
        self.id, self.field, self.expr, self.title = cid, field, expr, title
        self.level, self.kind, self.hard = level, kind, hard


def checks(charter, lists, fields=None):
    """Every check of a charter. fields: the QgsFields of the layer (unknown fields are skipped)."""
    c = model.clean_charter(charter)
    names = {f.name() for f in fields} if fields is not None else {f["name"] for f in c["fields"]}
    specs = {f["name"]: f for f in c["fields"] if f["name"] in names}
    out = []
    for name, spec in specs.items():
        label = model.field_label(spec)
        q = qn(name)
        if spec.get("required"):
            out.append(Check(f"required:{name}", name, f"{q} IS NOT NULL AND trim(to_string({q})) <> ''",
                             tr("{f} is empty").format(f=label), kind="required", hard=False))
        if spec.get("unique"):
            out.append(Check(f"unique:{name}", name, "", tr("{f} is repeated").format(f=label), kind="unique",
                             hard=False))
        lst = lists.get(spec.get("list") or "")
        if lst and lst["kind"] == "codes" and model.list_codes(lst):
            codes = model.list_codes(lst)
            out.append(Check(f"list:{name}", name, f"{q} IS NULL OR {q} IN {in_list(codes, _numeric(spec))}",
                             tr("{f} is not in the list {l}").format(f=label, l=pick(lst) or lst["id"]),
                             kind="list"))
        elif lst and lst["kind"] == "range" and (lst.get("min") is not None or lst.get("max") is not None):
            parts = []
            if lst.get("min") is not None:
                parts.append(f"{q} >= {float(lst['min']):g}")
            if lst.get("max") is not None:
                parts.append(f"{q} <= {float(lst['max']):g}")
            out.append(Check(f"range:{name}", name, f"{q} IS NULL OR ({' AND '.join(parts)})",
                             tr("{f} is outside {a} - {b}").format(
                                 f=label, a="" if lst.get("min") is None else f"{float(lst['min']):g}",
                                 b="" if lst.get("max") is None else f"{float(lst['max']):g}"), kind="range"))
    # kinds
    kf = c["kind_field"] if c["kind_field"] in names else ""
    if kf:
        kq, knum = qn(kf), _numeric(specs.get(kf))
        codes = [k["code"] for k in c["kinds"]]
        if codes and not (specs.get(kf) or {}).get("list"):
            out.append(Check(f"kind:{kf}", kf, f"{kq} IS NULL OR {kq} IN {in_list(codes, knum)}",
                             tr("{f} is not one of the kinds").format(f=model.field_label(specs[kf])),
                             kind="list"))
        for name in specs:
            if name == kf:
                continue
            q, num = qn(name), _numeric(specs[name])
            whens = [f"WHEN {kq} = {literal(k['code'], knum)} THEN {q} IS NULL OR {q} IN "
                     f"{in_list(k['lists'][name], num)}" for k in c["kinds"] if k["lists"].get(name)]
            if whens:
                out.append(Check(f"kindlist:{name}", name, f"CASE {' '.join(whens)} ELSE TRUE END",
                                 tr("{f} is not allowed for this kind").format(f=model.field_label(specs[name])),
                                 kind="kind"))
            req = [k["code"] for k in c["kinds"] if name in k["required"]]
            if req:
                out.append(Check(f"kindreq:{name}", name,
                                 f"CASE WHEN {kq} IN {in_list(req, knum)} THEN {q} IS NOT NULL AND "
                                 f"trim(to_string({q})) <> '' ELSE TRUE END",
                                 tr("{f} is required for this kind").format(f=model.field_label(specs[name])),
                                 kind="kind"))
    # linked choices
    for ln in c["links"]:
        child, parents = ln.get("child"), [p for p in ln.get("parents", []) if p in names]
        if child not in names or not parents or len(parents) != len(ln["parents"]):
            continue
        pairs = [f"{key}{PAIR}{code}" for key, codes in ln["pairs"].items() for code in codes]
        if not pairs or len(pairs) > INLINE_PAIRS:
            continue
        nulls = " OR ".join(f"{qn(p)} IS NULL" for p in parents)
        out.append(Check(f"link:{ln['id']}", child,
                         f"{qn(child)} IS NULL OR {nulls} OR ({parent_key_expr(parents)} || '{PAIR}' || "
                         f"{_text_of(child)}) IN {in_list(pairs)}",
                         tr("{f} does not go with {p}").format(
                             f=model.field_label(specs[child]),
                             p=", ".join(model.field_label(specs[p]) for p in parents)), kind="link"))
    # validation rules across fields
    for r in c["rules"]:
        if not r.get("expr"):
            continue
        field = r.get("field") or rule_field(r["expr"], names)
        if field not in names:
            field = ""
        out.append(Check(f"rule:{r['id']}", field, f"coalesce(({r['expr']}), TRUE)",
                         pick(r) or r["expr"], level=r.get("level", "error"), kind="rule",
                         hard=r.get("level", "error") == "error"))
    return out


def rule_field(expr, names):
    """The field a validation rule belongs to: the first field it mentions."""
    e = QgsExpression(expr)
    cols = [n for n in e.referencedColumns() if n in names]
    if not cols:
        return ""
    # keep the order in which they appear in the text
    return min(cols, key=lambda n: (expr.find(f'"{n}"') if f'"{n}"' in expr else expr.find(n)) % 10 ** 6)


# ---- the fields of the data --------------------------------------------------------------------------------
def can_alter(layer):
    """(True, "") when fields can be added, renamed and deleted in the data, else (False, why)."""
    if not isinstance(layer, QgsVectorLayer) or not layer.isValid():
        return False, tr("This is not a valid vector layer.")
    caps = layer.dataProvider().capabilities()
    C = Qgis.VectorProviderCapability
    if not all(caps & c for c in (C.AddAttributes, C.DeleteAttributes, C.RenameAttributes)):
        return False, tr("This kind of data cannot change its columns (a text file, a service or a read-only "
                         "file). Rebuild it into a GeoPackage first - the layer keeps its look and settings.")
    return True, ""


def schema_changes(layer, charter, deleted=()):
    """(adds [spec], renames {old: new}, deletes [name], order [name]) needed to bring the data to the charter."""
    have = [f.name() for f in layer.fields()]
    pk = {layer.fields().at(i).name() for i in layer.primaryKeyAttributes()}
    adds, renames = [], {}
    for spec in charter["fields"]:
        orig = spec.get("orig") or ""
        if orig and orig in have:
            if spec["name"] != orig and orig not in pk:
                renames[orig] = spec["name"]
        elif spec["name"] not in have:
            adds.append(spec)
    deletes = [n for n in deleted if n in have and n not in pk]
    order = [s["name"] for s in charter["fields"]]
    return adds, renames, deletes, order


def apply_schema(layer, charter, deleted=()):
    """Adds, renames and deletes fields in the data (one edit session, committed; the files are backed up first),
    then puts them in the charter's order. Returns a short text of what was done. Raises RuntimeError."""
    from .. import table_tools

    adds, renames, deletes, order = schema_changes(layer, charter, deleted)
    done = []
    if adds or renames or deletes:
        ok, why = can_alter(layer)
        if not ok:
            raise RuntimeError(why)
        if layer.isModified():
            raise RuntimeError(tr("Save or discard the edits of this layer first."))
        storage = layer.dataProvider().storageType().lower()
        final = [renames.get(n, n) for n in (f.name() for f in layer.fields()) if n not in deletes]
        final += [s["name"] for s in adds]
        if len({n.lower() for n in final}) != len(final):
            raise RuntimeError(tr("Two fields would have the same name."))
        if storage.startswith("esri shapefile") and any(len(n.encode("utf-8")) > 10 for n in final):
            raise RuntimeError(tr("Shapefile field names are limited to 10 characters (5 Arabic letters)."))
        if layer.providerType() == "ogr":
            table_tools.backup(layer)
        was_editing = layer.isEditable()
        if not was_editing:
            layer.startEditing()
        try:
            for name in deletes:
                layer.deleteAttribute(layer.fields().indexOf(name))
            for old, new in renames.items():
                if not layer.renameAttribute(layer.fields().indexOf(old), new):
                    raise RuntimeError(tr("The field {f} could not be renamed.").format(f=old))
            for spec in adds:
                qt, tname = model.TYPES.get(spec.get("type"), model.TYPES["text"])
                length = int(spec.get("length") or (254 if qt == QMetaType.Type.QString else 0))
                prec = 3 if spec.get("type") == "decimal" else 0
                if not layer.addAttribute(QgsField(spec["name"], qt, tname, length, prec)):
                    raise RuntimeError(tr("The field {f} could not be added.").format(f=spec["name"]))
            if not layer.commitChanges(not was_editing):
                raise RuntimeError("; ".join(layer.commitErrors()[-4:]))
        except Exception:
            layer.rollBack(not was_editing)
            raise
        layer.updateFields()
        done += [tr("{n} added").format(n=len(adds))] if adds else []
        done += [tr("{n} renamed").format(n=len(renames))] if renames else []
        done += [tr("{n} deleted").format(n=len(deletes))] if deletes else []
    # order: in the data when the file allows it, else in the attribute table and the form
    have = [f.name() for f in layer.fields()]
    wanted = [n for n in order if n in have] + [n for n in have if n not in order]
    if wanted != have:
        ok, _why = table_tools.can_rewrite(layer)
        if ok:
            try:
                table_tools.rewrite_fields(layer, wanted)
                done.append(tr("order rewritten in the data"))
            except Exception:  # noqa: BLE001
                table_tools.view_order(layer, wanted)
                done.append(tr("order set in the table"))
        else:
            table_tools.view_order(layer, wanted)
            done.append(tr("order set in the table"))
    for spec in charter["fields"]:
        spec["orig"] = spec["name"]
    return ", ".join(done)


def rebuild_into_gpkg(layer, gpkg=None):
    """Copies a layer whose columns cannot change into a GeoPackage (the project's database when there is one)
    and points the layer to it. Returns the GeoPackage path."""
    from .. import project as proj

    if not gpkg:
        gpkg = proj.project_gpkg()
    if not gpkg:
        src = proj._layer_file(layer)
        folder = os.path.dirname(src) if src else os.path.dirname(QgsProject.instance().fileName() or "") or \
            os.path.expanduser("~")
        gpkg = os.path.join(folder, "charter_layers.gpkg")
    proj.copy_vector_into_gpkg(layer, gpkg)
    layer.reload()
    return gpkg


# ---- the lookup table of value lists and linked choices ----------------------------------------------------
CHOICE_FIELDS = (("list", QMetaType.Type.QString), ("parent", QMetaType.Type.QString),
                 ("code", QMetaType.Type.QString), ("label_ar", QMetaType.Type.QString),
                 ("label_en", QMetaType.Type.QString), ("color", QMetaType.Type.QString),
                 ("ord", QMetaType.Type.Int))


def choices_layer(project=None, create=True, near=None):
    """The project's lookup table (kept in the project's GeoPackage, else in charter_lists.gpkg next to the project
    or the layer; a temporary table only when there is no file at all)."""
    project = project or QgsProject.instance()
    for lyr in project.mapLayers().values():
        if isinstance(lyr, QgsVectorLayer) and lyr.customProperty(CHOICES_PROP, False):
            return lyr
    if not create:
        return None
    from .. import project as proj

    gpkg = proj.project_gpkg(project)
    if not gpkg and project.fileName():
        gpkg = os.path.join(os.path.dirname(project.fileName()), "charter_lists.gpkg")
    if not gpkg and near is not None:
        src = proj._layer_file(near)
        if src:  # never inside the user's own data file
            gpkg = os.path.join(os.path.dirname(src), "charter_lists.gpkg")
    spec = "None?" + "&".join(f"field={n}:{'integer' if t == QMetaType.Type.Int else 'string'}"
                              for n, t in CHOICE_FIELDS)
    mem = QgsVectorLayer(spec, CHOICES_TABLE, "memory")
    lyr = mem
    if gpkg:
        opts = QgsVectorFileWriter.SaveVectorOptions()
        opts.driverName = "GPKG"
        opts.layerName = CHOICES_TABLE
        opts.fileEncoding = "UTF-8"
        opts.actionOnExistingFile = (QgsVectorFileWriter.ActionOnExistingFile.CreateOrOverwriteLayer
                                     if os.path.exists(gpkg) else
                                     QgsVectorFileWriter.ActionOnExistingFile.CreateOrOverwriteFile)
        err, _msg, *_ = QgsVectorFileWriter.writeAsVectorFormatV3(mem, gpkg, project.transformContext(), opts)
        if err == QgsVectorFileWriter.WriterError.NoError:
            disk = QgsVectorLayer(f"{gpkg}|layername={CHOICES_TABLE}", CHOICES_TABLE, "ogr")
            if disk.isValid():
                lyr = disk
    lyr.setName(tr("Charter lists"))
    lyr.setCustomProperty(CHOICES_PROP, True)
    project.addMapLayer(lyr, False)
    project.layerTreeRoot().addLayer(lyr)
    return lyr


def choice_rows(project=None, lists=None):
    """The rows of the lookup table: every coded list (parent '') and the pairs of every linked choice."""
    project = project or QgsProject.instance()
    lists = model.project_lists(project) if lists is None else lists
    rows = []
    for lid, lst in lists.items():
        if lst["kind"] != "codes":
            continue
        for n, it in enumerate(lst["items"]):
            code = str(it.get("code", ""))
            if code:
                rows.append((lid, "", code, it.get("ar") or it.get("en") or code, it.get("en") or it.get("ar") or code,
                             it.get("color") or "", n))
    for lyr in model.charter_layers(project):
        c = model.stored_charter(lyr)
        specs = {f["name"]: f for f in c["fields"]}
        for ln in c["links"]:
            lst = lists.get((specs.get(ln.get("child")) or {}).get("list") or "")
            labels = {str(i["code"]): i for i in (lst or {}).get("items", [])}
            order = {code: n for n, code in enumerate(labels)}
            for key, codes in ln["pairs"].items():
                for code in codes:
                    it = labels.get(str(code), {})
                    rows.append((ln["id"], key, str(code), it.get("ar") or it.get("en") or str(code),
                                 it.get("en") or it.get("ar") or str(code), it.get("color") or "",
                                 order.get(str(code), 9999)))
    return rows


def sync_choices(project=None, lists=None, near=None):
    """Rewrites the lookup table from the lists and linked choices of the project. Returns the layer."""
    project = project or QgsProject.instance()
    lyr = choices_layer(project, True, near)
    rows = choice_rows(project, lists)
    p = lyr.dataProvider()
    ids = [f.id() for f in p.getFeatures()]
    if ids:
        p.deleteFeatures(ids)
    feats = []
    for r in rows:
        f = QgsFeature(lyr.fields())
        for (name, _t), v in zip(CHOICE_FIELDS, r):
            f.setAttribute(name, v)
        feats.append(f)
    if feats:
        p.addFeatures(feats)
    lyr.updateExtents()
    lyr.reload()
    return lyr


# ---- the configuration of a layer --------------------------------------------------------------------------
def default_expression(spec):
    """The base default expression of a field: automatic value, custom expression or fixed value."""
    from .. import rules as attr_rules

    auto = spec.get("auto") or ""
    if auto == "custom":
        return spec.get("default") or "", False
    if auto in attr_rules.AUTO and attr_rules.AUTO[auto][1]:
        _label, expr, on_update = attr_rules.AUTO[auto]
        return expr.format(f=spec["name"]), on_update
    if spec.get("default") not in (None, ""):
        return literal(spec["default"], _numeric(spec)), False
    return "", False


def apply_config(layer, charter, lists, project=None, refresh_only=False):
    """Writes the charter into the layer's settings (not its data). Returns the stored charter.
    refresh_only (language change): the lookup table is not rewritten and the kind symbols keep the user's
    colours - only their labels follow the language."""
    from .. import rules as attr_rules

    project = project or QgsProject.instance()
    c = model.clean_charter(charter)
    lang = model.ui_lang()
    fields = layer.fields()
    specs = {f["name"]: f for f in c["fields"] if fields.indexOf(f["name"]) >= 0}
    kf = c["kind_field"] if c["kind_field"] in specs else ""
    pk = set(layer.primaryKeyAttributes())
    all_checks = checks(c, lists, fields)
    links_by_child = {ln["child"]: ln for ln in c["links"] if ln.get("child") in specs
                      and all(p in specs for p in ln.get("parents", [])) and ln.get("parents")}
    kind_lists = {name for name in specs for k in c["kinds"] if k["lists"].get(name)} if kf else set()
    needs_lookup = bool(links_by_child) or any(
        (lists.get(specs[n].get("list") or "") or {}).get("kind") == "codes" for n in kind_lists)
    choices = None
    if needs_lookup:
        choices = choices_layer(project, create=False) if refresh_only else None
        if choices is None:
            model.write_charter(layer, c)  # the lookup table reads the links of every charter
            choices = sync_choices(project, lists, near=layer)

    for name, spec in specs.items():
        idx = fields.indexOf(name)
        if idx in pk:
            continue
        num = _numeric(spec)
        # alias in the interface language (+ unit)
        label = model.field_label(spec, lang)
        if label and label != name:
            layer.setFieldAlias(idx, label)
        else:
            layer.removeFieldAlias(idx)
        # required / unique / automatic value - Biruni's attribute rules
        r = attr_rules.FieldRule(required=bool(spec.get("required")), unique=bool(spec.get("unique")))
        if spec.get("auto") in attr_rules.AUTO:
            r.auto = spec["auto"]
        elif spec.get("auto") == "custom" and spec.get("default"):
            r.auto, r.expression = "custom", spec["default"]
        attr_rules.apply_rules(layer, {name: r})
        # default value, per kind
        base, on_update = default_expression(spec)
        if kf and name != kf:
            whens = [f"WHEN {qn(kf)} = {literal(k['code'], _numeric(specs[kf]))} THEN "
                     f"{literal(k['defaults'][name], num)}" for k in c["kinds"]
                     if str(k["defaults"].get(name, "")) != ""]
            if whens:
                base = f"CASE {' '.join(whens)} ELSE {base or 'NULL'} END"
        layer.setDefaultValueDefinition(idx, QgsDefaultValue(base, on_update) if base else QgsDefaultValue())
        # edit widget
        lst = lists.get(spec.get("list") or "")
        if name == kf and not lst and c["kinds"]:
            lst = {"id": "", "kind": "codes", "items": c["kinds"]}
        ln = links_by_child.get(name)
        if (ln or name in kind_lists) and choices is not None:
            list_id = ln["id"] if ln else (lst or {}).get("id", "")
            flt = [f"\"list\" = {ql(list_id)}"]
            flt.append(f"\"parent\" = {parent_key_expr(ln['parents'], form=True)}" if ln else "\"parent\" = ''")
            if name in kind_lists:
                whens = [f"WHEN current_value({ql(kf)}) = {literal(k['code'], _numeric(specs[kf]))} THEN \"code\" IN "
                         f"{in_list(k['lists'][name])}" for k in c["kinds"] if k["lists"].get(name)]
                flt.append(f"(CASE {' '.join(whens)} ELSE TRUE END)")
            cfg = {"Layer": choices.id(), "LayerName": choices.name(), "LayerSource": choices.source(),
                   "LayerProviderName": choices.providerType(), "Key": "code", "Value": f"label_{lang}",
                   "FilterExpression": " AND ".join(flt), "AllowNull": not spec.get("required"),
                   "OrderByValue": False, "OrderByField": True, "OrderByFieldName": "ord",
                   "AllowMulti": False, "UseCompleter": False, "NofColumns": 1}
            layer.setEditorWidgetSetup(idx, QgsEditorWidgetSetup("ValueRelation", cfg))
        elif lst and lst["kind"] == "codes" and model.list_codes(lst):
            layer.setEditorWidgetSetup(idx, QgsEditorWidgetSetup("ValueMap", {"map": [
                {model.item_label(i, lang): str(i["code"])} for i in lst["items"] if str(i.get("code", ""))]}))
        elif lst and lst["kind"] == "range" and num:
            cfg = {"AllowNull": not spec.get("required"), "Style": "SpinBox",
                   "Step": float(lst.get("step") or (1 if spec["type"] in ("integer", "big") else 0.01))}
            if lst.get("min") is not None:
                cfg["Min"] = float(lst["min"])
            if lst.get("max") is not None:
                cfg["Max"] = float(lst["max"])
            if spec["type"] not in ("integer", "big"):
                cfg["Precision"] = 3
            layer.setEditorWidgetSetup(idx, QgsEditorWidgetSetup("Range", cfg))
        elif layer.editorWidgetSetup(idx).type() in ("ValueMap", "Range", "ValueRelation"):
            layer.setEditorWidgetSetup(idx, QgsEditorWidgetSetup())
        # one constraint expression for everything the charter checks on this field
        hard = [ch for ch in all_checks if ch.field == name and ch.expr and ch.kind != "required" and ch.hard]
        soft = [ch for ch in all_checks if ch.field == name and ch.expr and not ch.hard and ch.kind == "rule"]
        use = hard or soft
        current = layer.constraintExpression(idx)
        if use:
            expr = " AND ".join(f"({ch.expr})" for ch in use) + f" /* {TAG} */"
            layer.setConstraintExpression(idx, expr, "; ".join(dict.fromkeys(ch.title for ch in use)))
            strength = (QgsFieldConstraints.ConstraintStrength.ConstraintStrengthHard if hard else
                        QgsFieldConstraints.ConstraintStrength.ConstraintStrengthSoft)
            layer.setFieldConstraint(idx, QgsFieldConstraints.Constraint.ConstraintExpression, strength)
        elif current and (TAG in current or attr_rules.TAG in current):
            layer.setConstraintExpression(idx, "")
            layer.removeFieldConstraint(idx, QgsFieldConstraints.Constraint.ConstraintExpression)
    # the kind symbols
    if kf and c["kinds"] and c.get("kind_symbols") and layer.isSpatial():
        if refresh_only:
            relabel_kind_symbols(layer, c, lang)
        else:
            apply_kind_symbols(layer, c, lang)
    stored = model.write_charter(layer, c)
    layer.triggerRepaint()
    if not refresh_only:
        project.setDirty(True)
    return stored


def relabel_kind_symbols(layer, charter, lang=None):
    """The labels of the kind symbols in a language; the symbols themselves are left as the user made them."""
    r = layer.renderer()
    if not isinstance(r, QgsCategorizedSymbolRenderer) or r.classAttribute() != charter["kind_field"]:
        return
    labels = {str(k["code"]): model.item_label(k, lang) for k in charter["kinds"]}
    for i, cat in enumerate(r.categories()):
        v = cat.value()
        key = "" if v is None else (str(int(v)) if isinstance(v, float) and v.is_integer() else str(v))
        if key in labels:
            r.updateCategoryLabel(i, labels[key])
        elif key in ("", "NULL"):
            r.updateCategoryLabel(i, tr("Other"))


def apply_kind_symbols(layer, charter, lang=None):
    lang = lang or model.ui_lang()
    kf = charter["kind_field"]
    num = _numeric(model.find_field(charter, kf))
    cats = []
    for k in charter["kinds"]:
        sym = QgsSymbol.defaultSymbol(layer.geometryType())
        if k.get("color"):
            sym.setColor(QColor(k["color"]))
        value = k["code"]
        if num:
            try:
                value = int(k["code"]) if float(k["code"]).is_integer() else float(k["code"])
            except ValueError:
                pass
        cats.append(QgsRendererCategory(value, sym, model.item_label(k, lang)))
    other = QgsSymbol.defaultSymbol(layer.geometryType())
    other.setColor(QColor("#9AA5B1"))
    cats.append(QgsRendererCategory(None, other, tr("Other")))
    layer.setRenderer(QgsCategorizedSymbolRenderer(kf, cats))


def apply_charter(layer, charter, lists, deleted=(), project=None, section="", log=True):
    """Fields of the data, the project's lists, the layer's settings and the change log - in that order.
    Returns (stored charter, text of the field changes)."""
    project = project or QgsProject.instance()
    old = model.stored_charter(layer)
    old_lists = model.project_lists(project)
    charter = model.clean_charter(charter)
    if old is None:  # the first charter: compared with the layer as it was
        old = model.clean_charter({"fields": [dict(f, name=f["orig"]) for f in charter["fields"] if f.get("orig")]
                                  + [model.field_spec(n) for n in deleted]})
        for f in old["fields"]:
            if True:
                for k in ("alias_ar", "alias_en", "required", "unique", "auto", "default", "unit", "desc", "list"):
                    f[k] = "" if k not in ("required", "unique") else False
    merged_new = dict(old_lists)
    merged_new.update(lists)
    changes = model.describe_changes(old, charter, old_lists, merged_new)
    text = apply_schema(layer, charter, deleted)
    merged = dict(old_lists)
    merged.update(lists)
    model.save_project_lists(merged, project)
    stored = apply_config(layer, charter, merged, project)
    if any(lst["kind"] == "codes" for lst in merged.values()) and choices_layer(project, create=False) is not None:
        sync_choices(project, merged)
    if log:
        for sec, what in changes or [(section, tr("applied"))]:
            model.add_log(layer.name(), sec or section, what, project)
    return stored, text


def sync_language(project=None):
    """Aliases, list labels and kind labels follow the interface language: re-applies the charters of the
    project (settings only - the data is not touched). Cheap; meant for project load and language change."""
    project = project or QgsProject.instance()
    layers = model.charter_layers(project)
    if not layers:
        return 0
    lists = model.project_lists(project)
    dirty = project.isDirty()
    for lyr in layers:
        apply_config(lyr, model.stored_charter(lyr), lists, project, refresh_only=True)
    project.setDirty(dirty)  # opening a project must not ask to save it
    return len(layers)
