"""Review & Save: every unsaved change of the layers being edited - added, reshaped, re-valued and deleted
features - one by one, before and after on a map, each kept or left out before anything is written.

The engine saves a layer's edits all at once. To save only some of them, the edits are read from the
layer's edit buffer, the buffer is emptied (roll back), the chosen changes are applied again and saved, and
the others are applied again as unsaved edits (or dropped, if the user says so). If the save fails, every
change is put back as it was, so nothing is lost either way.

Layers whose fields were added, deleted or renamed in this session are saved whole or not at all: their
changes cannot be taken apart safely.
"""
from dataclasses import dataclass, field

from qgis.core import (Qgis, QgsCoordinateTransform, QgsDistanceArea, QgsExpression, QgsExpressionContext,
                       QgsExpressionContextUtils, QgsFeature, QgsFeatureRequest, QgsGeometry, QgsProject,
                       QgsVectorLayer)

from ..i18n import tr



@dataclass
class Change:
    layer: QgsVectorLayer
    kind: str                      # "add", "geometry", "attributes" or "delete"
    fid: int
    before: QgsGeometry = None     # the stored shape (None for an added feature)
    after: QgsGeometry = None      # the new shape (None for a deleted feature)
    values: dict = field(default_factory=dict)    # field index -> (old value, new value)
    feature: QgsFeature = None     # an added feature as it will be written

    @property
    def key(self):
        return (self.layer.id(), self.kind, self.fid)


def editing_layers(project=None):
    """The vector layers with unsaved changes."""
    project = project or QgsProject.instance()
    return [lyr for lyr in project.mapLayers().values()
            if isinstance(lyr, QgsVectorLayer) and lyr.isEditable() and lyr.isModified()]


def schema_changed(layer):
    buf = layer.editBuffer()
    renamed = getattr(buf, "renamedAttributes", lambda: {})() if buf is not None else {}
    return buf is not None and bool(buf.addedAttributes() or buf.deletedAttributeIds() or renamed)


def changes(layer):
    """The unsaved changes of ``layer``, in a stable order: added, reshaped, re-valued, deleted."""
    buf = layer.editBuffer()
    if buf is None:
        return []
    added = buf.addedFeatures()
    deleted = set(buf.deletedFeatureIds())
    geoms = {fid: g for fid, g in buf.changedGeometries().items() if fid not in added and fid not in deleted}
    attrs = {fid: v for fid, v in buf.changedAttributeValues().items() if fid not in added and fid not in deleted}
    wanted = set(geoms) | set(attrs) | deleted
    stored = {}
    if wanted:
        req = QgsFeatureRequest().setFilterFids(list(wanted))
        stored = {f.id(): f for f in layer.dataProvider().getFeatures(req)}
    out = []
    for fid in sorted(added, reverse=True):          # new ids are -1, -2 … in the order they were made
        f = QgsFeature(added[fid])
        out.append(Change(layer, "add", fid, None, QgsGeometry(f.geometry()), feature=f))
    for fid in sorted(geoms):
        old = stored.get(fid)
        out.append(Change(layer, "geometry", fid, QgsGeometry(old.geometry()) if old else None, QgsGeometry(geoms[fid])))
    for fid in sorted(attrs):
        old = stored.get(fid)
        values = {idx: (old.attribute(idx) if old else None, v) for idx, v in attrs[fid].items()}
        g = QgsGeometry(geoms.get(fid) or (old.geometry() if old else QgsGeometry()))
        out.append(Change(layer, "attributes", fid, g, g, values))
    for fid in sorted(deleted):
        old = stored.get(fid)
        out.append(Change(layer, "delete", fid, QgsGeometry(old.geometry()) if old else None, None))
    return out


def _replay(layer, items):
    """Apply ``items`` again to ``layer`` (in edit mode) as one edit command. -> True when all went in."""
    if not items:
        return True
    ok = True
    layer.beginEditCommand(tr("Changes kept for later"))
    for c in items:
        if c.kind == "delete":
            ok &= layer.deleteFeature(c.fid)
        elif c.kind == "geometry":
            ok &= layer.changeGeometry(c.fid, QgsGeometry(c.after))
        elif c.kind == "attributes":
            ok &= layer.changeAttributeValues(c.fid, {i: new for i, (_old, new) in c.values.items()},
                                              {i: old for i, (old, _new) in c.values.items()})
    adds = [QgsFeature(c.feature) for c in items if c.kind == "add"]
    if adds:
        ok &= layer.addFeatures(adds)
    layer.endEditCommand()
    return ok


class SaveRefused(Exception):
    """The layer refused the save; its changes are back as unsaved edits. The message is for the user."""


def save(layer, keep, rest="keep"):
    """Save the changes of ``layer`` whose keys are in ``keep``. The others stay as unsaved edits
    (``rest="keep"``) or are dropped (``rest="drop"``). -> (saved, left) counts. Raises SaveRefused."""
    items = changes(layer)
    chosen = [c for c in items if c.key in keep]
    others = [c for c in items if c.key not in keep]
    if not others:
        if not layer.commitChanges(False):   # like "Save edits": the layer stays in edit mode
            errors = "; ".join(layer.commitErrors()[:3])
            raise SaveRefused(tr("“{l}” refused the save: {e}").format(l=layer.name(), e=errors))
        return len(chosen), 0
    if schema_changed(layer):
        raise SaveRefused(tr("Fields of “{l}” were added, deleted or renamed: its changes can only be saved all "
                             "together.").format(l=layer.name()))
    layer.rollBack(False)
    if chosen:
        _replay(layer, chosen)
        if not layer.commitChanges(False):
            errors = "; ".join(layer.commitErrors()[:3])
            layer.rollBack(False)
            _replay(layer, items)          # nothing is lost: every change is unsaved again
            raise SaveRefused(tr("“{l}” refused the save, so nothing was saved; all its changes are still "
                                 "there: {e}").format(l=layer.name(), e=errors))
    if rest == "keep":
        if not layer.isEditable():
            layer.startEditing()
        _replay(layer, others)
    return len(chosen), (len(others) if rest == "keep" else 0)


# ---- words for the list ---------------------------------------------------------------------------------
def _measure(layer, g):
    """'area 120.50 m²' / 'length 35.20 m' / '' of a layer-CRS geometry."""
    if g is None or g.isEmpty():
        return ""
    da = QgsDistanceArea()
    da.setSourceCrs(layer.crs(), QgsProject.instance().transformContext())
    da.setEllipsoid(QgsProject.instance().ellipsoid() or "WGS84")
    if g.type() == Qgis.GeometryType.Polygon:
        return tr("{v} m²").format(v=f"{da.convertAreaMeasurement(da.measureArea(g), Qgis.AreaUnit.SquareMeters):,.2f}")
    if g.type() == Qgis.GeometryType.Line:
        return tr("{v} m").format(v=f"{da.convertLengthMeasurement(da.measureLength(g), Qgis.DistanceUnit.Meters):,.2f}")
    p = g.asPoint() if not g.isMultipart() else g.centroid().asPoint()
    return f"{p.x():.3f}, {p.y():.3f}"


def _value(v):
    if v is None or (hasattr(v, "isNull") and v.isNull()):
        return tr("(empty)")
    s = str(v)
    return s if len(s) <= 40 else s[:39] + "…"


def name_of(change):
    """A short name of the feature: its display expression, else its id."""
    layer = change.layer
    f = change.feature
    if f is None:
        req = QgsFeatureRequest(change.fid) if change.fid >= 0 else None
        f = next(layer.dataProvider().getFeatures(req), None) if req else None
    text = ""
    if f is not None and layer.displayExpression():
        ctx = QgsExpressionContext(QgsExpressionContextUtils.globalProjectLayerScopes(layer))
        ctx.setFeature(f)
        text = QgsExpression(layer.displayExpression()).evaluate(ctx)
    if text not in (None, "") and str(text) != str(change.fid):
        return str(text)
    return tr("new feature") if change.fid < 0 else tr("feature {n}").format(n=change.fid)


def describe(change):
    """One line for the list: what changed, with the measures or the values."""
    layer = change.layer
    who = name_of(change)
    if change.kind == "add":
        return tr("Added: {f} ({m})").format(f=who, m=_measure(layer, change.after))
    if change.kind == "delete":
        return tr("Deleted: {f} ({m})").format(f=who, m=_measure(layer, change.before))
    if change.kind == "geometry":
        return tr("Shape changed: {f} ({a} → {b})").format(f=who, a=_measure(layer, change.before),
                                                           b=_measure(layer, change.after))
    names = layer.fields()
    parts = [f"{names.at(i).displayName()}: {_value(old)} → {_value(new)}" for i, (old, new) in change.values.items()]
    return tr("Values changed: {f} ({v})").format(f=who, v="; ".join(parts))


def to_canvas(layer, g, canvas_crs):
    if g is None or g.isEmpty():
        return None
    g = QgsGeometry(g)
    if layer.crs() != canvas_crs:
        g.transform(QgsCoordinateTransform(layer.crs(), canvas_crs, QgsProject.instance()))
    return g


def open_review(plugin):
    from .review_save_dialog import ReviewDialog

    iface = getattr(plugin, "iface", None)
    layers = editing_layers()
    parent = iface.mainWindow() if iface is not None else None
    if not layers:
        from qgis.PyQt.QtWidgets import QMessageBox

        QMessageBox.information(parent, tr("Review & Save"), tr("There are no unsaved edits."))
        return None
    dlg = ReviewDialog(parent, layers, iface)
    dlg.exec()
    return dlg
