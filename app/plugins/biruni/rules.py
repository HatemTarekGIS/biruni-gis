"""Attribute rules: what each field must contain and what it fills by itself.

One table per layer - required, unique, list of allowed values, numeric range,
automatic value (area, length, X/Y, today, user, next number, last edit time) -
stored in the layer's form configuration (so they travel with the project and
work in every edit form and in QField). The ArcGIS equivalents are domains,
attribute rules and default values.
"""
from dataclasses import dataclass, field

from qgis.core import (QgsDefaultValue, QgsEditorWidgetSetup, QgsFieldConstraints, QgsVectorLayer,
                       QgsVectorLayerUtils)
from qgis.PyQt.QtCore import QMetaType, Qt
from qgis.PyQt.QtWidgets import (QAbstractItemView, QCheckBox, QComboBox, QDialog, QDialogButtonBox, QHeaderView,
                                 QLabel, QLineEdit, QMessageBox, QTableWidget, QVBoxLayout, QWidget, QHBoxLayout)

from .i18n import tr
from .version import APP_NAME

# automatic values: key -> (label, expression template, apply on every edit)
AUTO = {
    "": ("None", None, False),
    "area": ("Area (m²)", "round($area, 2)", True),
    "length": ("Length / perimeter (m)", "round(if(geometry_type($geometry) = 'Polygon', $perimeter, $length), 2)",
               True),
    "x": ("X of the point / centre", "round(x(point_on_surface($geometry)), 3)", True),
    "y": ("Y of the point / centre", "round(y(point_on_surface($geometry)), 3)", True),
    "created": ("Date created", "now()", False),
    "edited": ("Date last edited", "now()", True),
    "user": ("User who created it", "@user_account_name", False),
    "editor": ("User who last edited it", "@user_account_name", True),
    "number": ("Next number", "coalesce(maximum(\"{f}\"), 0) + 1", False),
}
# labels listed for the translation extractor
TITLES = [v[0] for v in AUTO.values()]
TAG = "biruni_rule"   # marks expression constraints written by this tool


@dataclass
class FieldRule:
    required: bool = False
    unique: bool = False
    values: list = field(default_factory=list)   # allowed values (strings)
    minimum: float = None
    maximum: float = None
    auto: str = ""                                 # key of AUTO
    expression: str = ""                           # custom default expression (when auto == "custom")

    def is_empty(self):
        return not (self.required or self.unique or self.values or self.minimum is not None
                    or self.maximum is not None or self.auto)


def _quote(v):
    return "'" + str(v).replace("'", "''") + "'"


def _number(text):
    try:
        return float(str(text).replace(",", "."))
    except (TypeError, ValueError):
        return None


def read_rules(layer):
    rules = {}
    for idx, f in enumerate(layer.fields()):
        c = f.constraints()
        r = FieldRule()
        r.required = bool(c.constraints() & QgsFieldConstraints.Constraint.ConstraintNotNull)
        r.unique = bool(c.constraints() & QgsFieldConstraints.Constraint.ConstraintUnique)
        setup = layer.editorWidgetSetup(idx)
        if setup.type() == "ValueMap":
            cfg = setup.config().get("map", [])
            items = cfg if isinstance(cfg, list) else [{k: v} for k, v in cfg.items()]
            r.values = [str(list(d.values())[0]) for d in items if d]
        elif setup.type() == "Range":
            cfg = setup.config()
            r.minimum = _number(cfg.get("Min")) if "Min" in cfg else None
            r.maximum = _number(cfg.get("Max")) if "Max" in cfg else None
        d = layer.defaultValueDefinition(idx)
        if d.isValid():
            r.auto = "custom"
            r.expression = d.expression()
            for key, (_label, expr, on_update) in AUTO.items():
                if expr and expr.format(f=f.name()) == d.expression() and on_update == d.applyOnUpdate():
                    r.auto, r.expression = key, ""
                    break
        rules[f.name()] = r
    return rules


def apply_rules(layer, rules):
    """Write the rules of ``{field name: FieldRule}`` into the layer."""
    for name, r in rules.items():
        idx = layer.fields().indexFromName(name)
        if idx < 0:
            continue
        fld = layer.fields().at(idx)
        numeric = fld.isNumeric()
        # constraints
        for flag, on in ((QgsFieldConstraints.Constraint.ConstraintNotNull, r.required),
                         (QgsFieldConstraints.Constraint.ConstraintUnique, r.unique)):
            if on:
                layer.setFieldConstraint(idx, flag, QgsFieldConstraints.ConstraintStrength.ConstraintStrengthHard)
            else:
                layer.removeFieldConstraint(idx, flag)
        checks, desc = [], []
        q = '"' + name.replace('"', '""') + '"'
        if r.values:
            vals = ", ".join(str(_number(v)) if numeric and _number(v) is not None else _quote(v) for v in r.values)
            checks.append(f"{q} IN ({vals})")
            desc.append(tr("one of: {v}").format(v=", ".join(r.values)))
        if r.minimum is not None:
            checks.append(f"{q} >= {r.minimum:g}")
            desc.append(tr("at least {n}").format(n=f"{r.minimum:g}"))
        if r.maximum is not None:
            checks.append(f"{q} <= {r.maximum:g}")
            desc.append(tr("at most {n}").format(n=f"{r.maximum:g}"))
        current = layer.constraintExpression(idx)
        if checks:
            layer.setConstraintExpression(idx, f"{q} IS NULL OR ({' AND '.join(checks)}) /* {TAG} */",
                                          tr("Must be {d}").format(d=", ".join(desc)))
            layer.setFieldConstraint(idx, QgsFieldConstraints.Constraint.ConstraintExpression,
                                     QgsFieldConstraints.ConstraintStrength.ConstraintStrengthHard)
        elif current and TAG in current:
            layer.setConstraintExpression(idx, "")
            layer.removeFieldConstraint(idx, QgsFieldConstraints.Constraint.ConstraintExpression)
        # edit widget
        if r.values:
            layer.setEditorWidgetSetup(idx, QgsEditorWidgetSetup("ValueMap", {"map": [{v: v} for v in r.values]}))
        elif numeric and (r.minimum is not None or r.maximum is not None):
            cfg = {"AllowNull": not r.required, "Style": "SpinBox", "Step": 1 if fld.type() in (QMetaType.Type.Int, QMetaType.Type.LongLong) else 0.01}
            if r.minimum is not None:
                cfg["Min"] = r.minimum
            if r.maximum is not None:
                cfg["Max"] = r.maximum
            layer.setEditorWidgetSetup(idx, QgsEditorWidgetSetup("Range", cfg))
        elif layer.editorWidgetSetup(idx).type() in ("ValueMap", "Range"):
            layer.setEditorWidgetSetup(idx, QgsEditorWidgetSetup())
        # automatic value
        if r.auto and r.auto != "custom":
            _label, expr, on_update = AUTO[r.auto]
            layer.setDefaultValueDefinition(idx, QgsDefaultValue(expr.format(f=name), on_update))
        elif r.auto == "custom" and r.expression:
            layer.setDefaultValueDefinition(idx, QgsDefaultValue(r.expression, False))
        else:
            layer.setDefaultValueDefinition(idx, QgsDefaultValue())


def problems(layer, limit=1000):
    """(fid, field, message) for existing features that break the rules."""
    out = []
    for f in layer.getFeatures():
        for idx, fld in enumerate(layer.fields()):
            ok, errs = QgsVectorLayerUtils.validateAttribute(layer, f, idx)
            if not ok:
                out.append((f.id(), fld.name(), "; ".join(errs)))
                if len(out) >= limit:
                    return out
    return out


class RulesDialog(QDialog):
    COLS = ("Field", "Required", "Unique", "Allowed values (comma separated)", "Minimum", "Maximum",
            "Automatic value")

    def __init__(self, plugin, layer):
        super().__init__(plugin.iface.mainWindow())
        self.plugin = plugin
        self.layer = layer
        self.setWindowTitle(tr("Attribute Rules") + f" - {layer.name()}")
        self.resize(940, 520)
        v = QVBoxLayout(self)
        v.setContentsMargins(18, 14, 18, 12)
        intro = QLabel(tr("Decide what each field must contain and what it fills by itself. The rules apply in "
                          "every edit form (and in QField): a feature that breaks them cannot be saved."))
        intro.setWordWrap(True)
        v.addWidget(intro)
        self.table = QTableWidget(len(layer.fields()), len(self.COLS))
        self.table.setHorizontalHeaderLabels([tr(c) for c in self.COLS])
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        self.table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        self.rows = []
        rules = read_rules(layer)
        pk = set(layer.primaryKeyAttributes())
        for r, fld in enumerate(layer.fields()):
            rule = rules.get(fld.name(), FieldRule())
            name = QLabel(f"  {fld.displayName()}  ")
            name.setToolTip(f"{fld.name()} · {fld.typeName()}")
            req, uni = self._center_check(rule.required), self._center_check(rule.unique)
            vals = QLineEdit(", ".join(rule.values))
            mn = QLineEdit("" if rule.minimum is None else f"{rule.minimum:g}")
            mx = QLineEdit("" if rule.maximum is None else f"{rule.maximum:g}")
            for w in (mn, mx):
                w.setEnabled(fld.isNumeric())
            auto = QComboBox()
            for key, (label, _e, _u) in AUTO.items():
                auto.addItem(tr(label), key)
            auto.addItem(tr("Custom expression…"), "custom")
            auto.setCurrentIndex(max(0, auto.findData(rule.auto)))
            if rule.auto == "custom":
                auto.setToolTip(rule.expression)
            self._custom = {}
            auto.activated.connect(lambda _i, a=auto, f=fld.name(): self._maybe_custom(a, f))
            if r in pk or fld.name().lower() in ("fid", "objectid"):
                for w in (req.findChild(QCheckBox), uni.findChild(QCheckBox), vals, mn, mx, auto):
                    w.setEnabled(False)
            for c, w in enumerate((name, req, uni, vals, mn, mx, auto)):
                self.table.setCellWidget(r, c, w)
            self.rows.append((fld.name(), req.findChild(QCheckBox), uni.findChild(QCheckBox), vals, mn, mx, auto,
                              rule.expression))
        self.table.resizeColumnsToContents()
        self.table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        v.addWidget(self.table, 1)
        bb = QDialogButtonBox()
        bb.addButton(tr("Apply rules"), QDialogButtonBox.ButtonRole.AcceptRole)
        bb.addButton(QDialogButtonBox.StandardButton.Cancel)
        bb.accepted.connect(self._apply)
        bb.rejected.connect(self.reject)
        v.addWidget(bb)

    @staticmethod
    def _center_check(on):
        w = QWidget()
        h = QHBoxLayout(w)
        h.setContentsMargins(0, 0, 0, 0)
        h.setAlignment(Qt.AlignmentFlag.AlignCenter)
        cb = QCheckBox()
        cb.setChecked(on)
        h.addWidget(cb)
        return w

    def _maybe_custom(self, combo, fname):
        if combo.currentData() != "custom":
            return
        from qgis.gui import QgsExpressionBuilderDialog

        dlg = QgsExpressionBuilderDialog(self.layer, self._custom.get(fname, ""), self)
        dlg.setWindowTitle(tr("Custom expression…"))
        if dlg.exec():
            self._custom[fname] = dlg.expressionText()
            combo.setToolTip(dlg.expressionText())
        else:
            combo.setCurrentIndex(0)

    def _apply(self):
        rules = {}
        for name, req, uni, vals, mn, mx, auto, old_expr in self.rows:
            if not req.isEnabled():
                continue
            values = [s.strip() for s in vals.text().replace("،", ",").split(",") if s.strip()]
            lo, hi = _number(mn.text()) if mn.text().strip() else None, _number(mx.text()) if mx.text().strip() else None
            if (mn.text().strip() and lo is None) or (mx.text().strip() and hi is None):
                QMessageBox.warning(self, APP_NAME, tr("{f}: the minimum and maximum must be numbers.").format(f=name))
                return
            rules[name] = FieldRule(req.isChecked(), uni.isChecked(), values, lo, hi, auto.currentData(),
                                    self._custom.get(name, old_expr))
        apply_rules(self.layer, rules)
        self.layer.triggerRepaint()
        from qgis.core import QgsProject

        QgsProject.instance().setDirty(True)
        bad = problems(self.layer)
        msg = tr("The rules were applied to {n}.").format(n=self.layer.name())
        if bad:
            fids = sorted({b[0] for b in bad})
            msg += " " + tr("{n} existing features break them - they are selected.").format(n=len(fids))
            self.layer.selectByIds(fids)
            self.plugin.iface.messageBar().pushWarning(APP_NAME, msg)
        else:
            self.plugin.iface.messageBar().pushSuccess(APP_NAME, msg)
        self.accept()


def open_rules(plugin):
    layer = plugin.iface.activeLayer()
    if not isinstance(layer, QgsVectorLayer):
        plugin.iface.messageBar().pushInfo(APP_NAME, tr("Select a vector layer in the Layers panel first."))
        return
    RulesDialog(plugin, layer).exec()
