"""Feature templates: what a new feature of a layer gets - a name, default attribute values, the
construction tool to start with, a description, tags and whether the attribute form opens.

Templates live with their layer (a layer custom property, so they are saved in the project and travel
in a layer definition file). "From symbology" makes one template per category of a categorized or
rule-based renderer with the category's value filled in - the ArcGIS Pro way.
"""
import json
import uuid

from qgis.core import (QgsExpression, QgsExpressionContextUtils, QgsExpressionNode, QgsFeature,
                       QgsFieldConstraints, QgsMapLayerType, QgsRenderContext, QgsSymbolLayerUtils,
                       QgsVectorLayer, QgsVectorLayerUtils, QgsWkbTypes)
from qgis.PyQt.QtCore import QSize, QVariant

from ..arabic_text import normalise

PROPERTY = "biruni/create_templates"

FORM_AUTO = "auto"      # open the form only when a required field is still empty
FORM_ALWAYS = "always"
FORM_NEVER = "never"


class Template:
    def __init__(self, name, layer_id, attributes=None, tool="", description="", tags=None, form=FORM_AUTO,
                 tid=None):
        self.id = tid or uuid.uuid4().hex[:12]
        self.name = name
        self.layer_id = layer_id
        self.attributes = dict(attributes or {})
        self.tool = tool or ""
        self.description = description or ""
        self.tags = list(tags or [])
        self.form = form or FORM_AUTO

    def to_dict(self):
        return {"id": self.id, "name": self.name, "attributes": self.attributes, "tool": self.tool,
                "description": self.description, "tags": self.tags, "form": self.form}

    @classmethod
    def from_dict(cls, d, layer_id):
        return cls(d.get("name", ""), layer_id, d.get("attributes") or {}, d.get("tool", ""),
                   d.get("description", ""), d.get("tags") or [], d.get("form", FORM_AUTO), d.get("id"))

    def copy(self):
        return Template.from_dict(json.loads(json.dumps(self.to_dict())), self.layer_id)

    def __repr__(self):
        return f"Template({self.name!r}, {self.attributes!r})"


# ---- which layers -------------------------------------------------------------------------------------
def is_vector(layer):
    return isinstance(layer, QgsVectorLayer) and layer.type() == QgsMapLayerType.VectorLayer


def can_create(layer):
    """A vector layer with geometry whose features can be added (not read-only)."""
    if not is_vector(layer) or not layer.isValid() or layer.readOnly():
        return False
    if layer.geometryType() not in (QgsWkbTypes.GeometryType.PointGeometry, QgsWkbTypes.GeometryType.LineGeometry,
                                    QgsWkbTypes.GeometryType.PolygonGeometry):
        return False
    caps = layer.dataProvider().capabilities()
    return bool(caps & layer.dataProvider().Capability.AddFeatures) or layer.isEditable()


# ---- storage ------------------------------------------------------------------------------------------
def load(layer):
    raw = layer.customProperty(PROPERTY, "")
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return []
    return [Template.from_dict(d, layer.id()) for d in data if isinstance(d, dict)]


def save(layer, templates):
    if templates:
        layer.setCustomProperty(PROPERTY, json.dumps([t.to_dict() for t in templates], ensure_ascii=False,
                                                     default=str))
    else:
        layer.removeCustomProperty(PROPERTY)
    if layer.project() is not None:
        layer.project().setDirty(True)


def templates_or_default(layer):
    """The layer's templates; one plain template named after the layer when it has none."""
    ts = load(layer)
    return ts or [Template(layer.name(), layer.id(), tid="default")]


def add(layer, template):
    ts = load(layer)
    ts.append(template)
    save(layer, ts)
    return template


def replace(layer, template):
    ts = load(layer)
    for i, t in enumerate(ts):
        if t.id == template.id:
            ts[i] = template
            break
    else:
        ts.append(template)
    save(layer, ts)


def remove(layer, template_id):
    save(layer, [t for t in load(layer) if t.id != template_id])


# ---- from the symbology -------------------------------------------------------------------------------
def _field_name(layer, expr):
    name = (expr or "").strip()
    if len(name) > 1 and name[0] == name[-1] == '"':
        name = name[1:-1].replace('""', '"')
    return name if layer.fields().indexOf(name) >= 0 else None


def equalities(expression):
    """{field: value} of a filter made of ``"field" = value`` terms joined by AND; None otherwise."""
    if not expression:
        return {}
    exp = QgsExpression(expression)
    if exp.hasParserError() or exp.rootNode() is None:
        return None
    out = {}

    def walk(node):
        nt = node.nodeType()
        if nt == QgsExpressionNode.NodeType.ntBinaryOperator:
            op = node.op()
            if op == node.BinaryOperator.boAnd:
                return walk(node.opLeft()) and walk(node.opRight())
            if op == node.BinaryOperator.boEQ:
                a, b = node.opLeft(), node.opRight()
                if a.nodeType() == QgsExpressionNode.NodeType.ntLiteral:
                    a, b = b, a
                if a.nodeType() == QgsExpressionNode.NodeType.ntColumnRef and \
                        b.nodeType() == QgsExpressionNode.NodeType.ntLiteral:
                    out[a.name()] = b.value()
                    return True
            return False
        if nt == QgsExpressionNode.NodeType.ntInOperator and not node.isNotIn():
            items = node.list().list()
            if node.node().nodeType() == QgsExpressionNode.NodeType.ntColumnRef and len(items) == 1 and \
                    items[0].nodeType() == QgsExpressionNode.NodeType.ntLiteral:
                out[node.node().name()] = items[0].value()
                return True
        return False

    return out if walk(exp.rootNode()) else None


def _plain(v):
    if isinstance(v, QVariant):
        v = None if v.isNull() else v.value()
    if v is not None and not isinstance(v, (str, int, float, bool)):
        v = str(v)
    return v


def from_symbology(layer):
    """New templates, one per category / rule of the layer's renderer (single symbol: one template)."""
    r = layer.renderer()
    if r is None:
        return []
    kind = r.type()
    out = []
    if kind == "categorizedSymbol":
        field = _field_name(layer, r.classAttribute())
        for cat in r.categories():
            value = _plain(cat.value())
            if value is None or value == "" or isinstance(value, list):
                continue  # "all other values"
            name = cat.label() or str(value)
            out.append(Template(name, layer.id(), {field: value} if field else {}))
    elif kind == "RuleRenderer":
        def visit(rule, inherited):
            filt = rule.filterExpression() if not rule.isElse() else ""
            own = equalities(filt)
            if own is None:
                return
            attrs = dict(inherited)
            attrs.update(own)
            kids = [k for k in rule.children() if k.active()]
            if not kids and rule.symbol() is not None and rule is not r.rootRule():
                name = rule.label() or ", ".join(str(v) for v in attrs.values()) or layer.name()
                out.append(Template(name, layer.id(), {k: _plain(v) for k, v in attrs.items()
                                                       if layer.fields().indexOf(k) >= 0}))
            for k in kids:
                visit(k, attrs)

        visit(r.rootRule(), {})
    elif kind == "graduatedSymbol":
        for rng in r.ranges():
            out.append(Template(rng.label(), layer.id(), {}))
    if not out:
        out.append(Template(layer.name(), layer.id()))
    return out


def make_from_symbology(layer, keep_existing=True):
    """Add the symbology templates to the layer (skipping names that exist). Returns the added ones."""
    existing = load(layer) if keep_existing else []
    names = {t.name for t in existing}
    new = [t for t in from_symbology(layer) if t.name not in names]
    save(layer, existing + new)
    return new


def symbol_for(layer, template):
    """The renderer's symbol for a feature with the template's values (None when nothing draws it)."""
    r = layer.renderer()
    if r is None:
        return None
    r = r.clone()
    ctx = QgsRenderContext()
    ctx.expressionContext().appendScopes(QgsExpressionContextUtils.globalProjectLayerScopes(layer))
    f = QgsFeature(layer.fields())
    for name, value in template.attributes.items():
        i = layer.fields().indexOf(name)
        if i >= 0:
            f.setAttribute(i, value)
    ctx.expressionContext().setFeature(f)
    r.startRender(ctx, layer.fields())
    try:
        syms = r.originalSymbolsForFeature(f, ctx) or r.symbolsForFeature(f, ctx)
    finally:
        r.stopRender(ctx)
    return syms[0].clone() if syms else None


def icon_for(layer, template, size=16):
    from qgis.PyQt.QtGui import QIcon

    sym = symbol_for(layer, template)
    if sym is None:
        return QIcon()
    return QgsSymbolLayerUtils.symbolPreviewIcon(sym, QSize(size, size))


# ---- search -------------------------------------------------------------------------------------------
def search_key(template, layer_name=""):
    parts = [template.name, layer_name, template.description, " ".join(template.tags)]
    parts += [str(v) for v in template.attributes.values() if v not in (None, "")]
    return normalise(" ".join(p for p in parts if p))


def matches(template, text, layer_name=""):
    """True when every word of ``text`` is found (Arabic-aware: hamza forms, ta marbuta, tashkeel…)."""
    words = normalise(text).split()
    if not words:
        return True
    key = search_key(template, layer_name)
    return all(w in key for w in words)


def search(entries, text):
    """The (template, layer_name) entries matching ``text``, in their order."""
    return [(t, n) for t, n in entries if matches(t, text, n)]


# ---- the new feature ----------------------------------------------------------------------------------
def _is_auto_key(layer, i):
    prov = layer.dataProvider()
    if i in layer.primaryKeyAttributes():
        return True
    return bool(prov.defaultValueClause(layer.fields().fieldOriginIndex(i))) if \
        layer.fields().fieldOrigin(i) == layer.fields().FieldOrigin.OriginProvider else False


def missing_required(layer, template):
    """Names of not-null fields that neither the template nor a default value fills."""
    out = []
    for i, f in enumerate(layer.fields()):
        cons = f.constraints().constraints()
        if not cons & QgsFieldConstraints.Constraint.ConstraintNotNull:
            continue
        if _is_auto_key(layer, i) or layer.defaultValueDefinition(i).expression():
            continue
        if template.attributes.get(f.name()) in (None, ""):
            out.append(f.name())
    return out


def needs_form(layer, template):
    if template.form == FORM_ALWAYS:
        return True
    if template.form == FORM_NEVER:
        return False
    return bool(missing_required(layer, template))


def attribute_map(layer, template):
    """{field index: value} of the template's values, converted to the field types."""
    out = {}
    for name, value in template.attributes.items():
        i = layer.fields().indexOf(name)
        if i < 0 or value in (None, ""):
            continue
        field = layer.fields().at(i)
        try:
            ok = field.convertCompatible(value)
            out[i] = ok if ok is not None else value
        except (ValueError, TypeError):
            out[i] = value
    return out


def make_feature(layer, geometry, template):
    """A new feature for ``layer`` with ``geometry`` (layer CRS) and the template's values; default values
    and unique keys are filled as QGIS fills them."""
    ctx = layer.createExpressionContext()
    return QgsVectorLayerUtils.createFeature(layer, geometry, attribute_map(layer, template) if template else {},
                                             ctx)
