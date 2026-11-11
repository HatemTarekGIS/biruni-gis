"""A feature as a state ({"g": WKB hex, "a": {field: value}}) and back, through the layer's
data provider (what is on disk, not the edit buffer)."""
from qgis.core import QgsFeature, QgsFeatureRequest, QgsGeometry, QgsVectorLayer

from ..edit_journal import decode, encode


def key_fields(layer):
    """The names of the fields that hold the feature id (a GeoPackage's fid); they are not shared values."""
    return {layer.fields().at(i).name() for i in layer.primaryKeyAttributes()}


def state_of(layer, feat):
    if feat is None or not feat.isValid():
        return None
    skip = key_fields(layer)
    g = feat.geometry()
    attrs = {}
    for i, f in enumerate(layer.fields()):
        if f.name() not in skip:
            attrs[f.name()] = encode(feat.attribute(i))
    return {"g": bytes(g.asWkb()).hex() if g is not None and not g.isNull() else None, "a": attrs}


def stored(layer, fid):
    """The feature as it is in the file (the provider), or None."""
    it = layer.dataProvider().getFeatures(QgsFeatureRequest(int(fid)))
    f = QgsFeature()
    if it.nextFeature(f) and f.isValid():
        return f
    return None


def stored_state(layer, fid):
    return state_of(layer, stored(layer, fid))


def stored_states(layer, fids):
    """{fid: state or None} of many features in one read of the file."""
    fids = [int(f) for f in fids]
    out = {f: None for f in fids}
    if fids:
        for f in layer.dataProvider().getFeatures(QgsFeatureRequest().setFilterFids(fids)):
            out[f.id()] = state_of(layer, f)
    return out


def _geometry(hexwkb):
    g = QgsGeometry()
    if hexwkb:
        g.fromWkb(bytes.fromhex(hexwkb))
    return g


def write_state(layer, fid, state):
    """Make feature fid be state in the file (added when missing). Returns True when written."""
    pr = layer.dataProvider()
    fields = layer.fields()
    current = stored(layer, fid)
    if current is None:
        f = QgsFeature(fields)
        for i in layer.primaryKeyAttributes():
            f.setAttribute(i, int(fid))
        for name, v in (state.get("a") or {}).items():
            i = fields.indexOf(name)
            if i >= 0:
                f.setAttribute(i, decode(v))
        f.setGeometry(_geometry(state.get("g")))
        ok, out = pr.addFeatures([f])
        return bool(ok) and bool(out) and out[0].id() == int(fid)
    old = state_of(layer, current)
    ok = True
    if state.get("g") != old.get("g"):
        ok = pr.changeGeometryValues({int(fid): _geometry(state.get("g"))}) and ok
    changes = {}
    for name, v in (state.get("a") or {}).items():
        i = fields.indexOf(name)
        if i >= 0 and old["a"].get(name) != v:
            changes[i] = decode(v)
    if changes:
        ok = pr.changeAttributeValues({int(fid): changes}) and ok
    return ok


def _feature(layer, fid, state):
    f = QgsFeature(layer.fields())
    for i in layer.primaryKeyAttributes():
        f.setAttribute(i, int(fid))
    for name, v in (state.get("a") or {}).items():
        i = layer.fields().indexOf(name)
        if i >= 0:
            f.setAttribute(i, decode(v))
    f.setGeometry(_geometry(state.get("g")))
    return f


def write_states(layer, changes):
    """Write many features at once: {fid: (state in the file or None, new state or None)}. One delete, one add
    and one change call (each one transaction in a GeoPackage). True when all were written."""
    pr = layer.dataProvider()
    fields = layer.fields()
    dels, adds, attrs, geoms = [], [], {}, {}
    for fid, (old, new) in changes.items():
        if new is None:
            if old is not None:
                dels.append(int(fid))
        elif old is None:
            adds.append(_feature(layer, fid, new))
        else:
            if new.get("g") != old.get("g"):
                geoms[int(fid)] = _geometry(new.get("g"))
            ch = {}
            for name, v in (new.get("a") or {}).items():
                i = fields.indexOf(name)
                if i >= 0 and (old.get("a") or {}).get(name) != v:
                    ch[i] = decode(v)
            if ch:
                attrs[int(fid)] = ch
    ok = True
    if dels:
        ok = pr.deleteFeatures(dels) and ok
    if adds:
        res, out = pr.addFeatures(adds)
        ok = bool(res) and [f.id() for f in out] == [f.attribute(layer.primaryKeyAttributes()[0])
                                                   if layer.primaryKeyAttributes() else f.id() for f in adds] and ok
    if attrs or geoms:
        ok = pr.changeFeatures(attrs, geoms) and ok
    return ok


def delete_stored(layer, fid):
    if stored(layer, fid) is None:
        return True
    return layer.dataProvider().deleteFeatures([int(fid)])


def max_fid(layer):
    best = 0
    req = QgsFeatureRequest().setNoAttributes().setFlags(QgsFeatureRequest.NoGeometry)
    for f in layer.dataProvider().getFeatures(req):
        best = max(best, f.id())
    return best


def is_pending(layer, fid):
    """The feature has edits in the layer's edit buffer that are not saved yet."""
    if not isinstance(layer, QgsVectorLayer) or not layer.isEditable():
        return False
    buf = layer.editBuffer()
    if buf is None:
        return False
    return (fid in buf.changedGeometries() or fid in buf.changedAttributeValues()
            or fid in buf.deletedFeatureIds())
