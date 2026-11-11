"""The tools of the Modify Features pane: what each one is (section, icon, options), what it does to the
selected features (``run``), how the result is written (``write``: one edit command per layer, so one
Undo step per apply and the crash journal sees it) and the small map tools that collect clicks and
sketches.

Geometry is worked on in the map's CRS (typed distances are map units, like the rest of the editing
tools) and written back in each layer's CRS, with curves densified when the layer cannot store them,
Z / M added when the layer has them and single / multi type kept. A result that is empty or not valid
is never written: the whole apply is refused with a message instead.
"""
from qgis.core import (Qgis, QgsAbstractGeometry, QgsCoordinateTransform, QgsExpression,
                       QgsExpressionContext, QgsExpressionContextUtils, QgsFeatureRequest, QgsGeometry,
                       QgsPointXY, QgsProject, QgsRectangle, QgsUnitTypes, QgsVectorLayer,
                       QgsVectorLayerUtils, QgsWkbTypes)
from qgis.gui import QgsMapTool, QgsRubberBand
from qgis.PyQt.QtCore import Qt, pyqtSignal
from qgis.PyQt.QtGui import QColor

from ..i18n import tr
from ..rules import renumber
from . import construct
from . import modify_geom as mg

SECTION_IDS = ("alignment", "construct", "divide", "reshape", "combine", "attributes", "cleanup")


def section_title(sid):
    return {"alignment": tr("Alignment"), "construct": tr("Construct"), "divide": tr("Divide"),
            "reshape": tr("Reshape"), "combine": tr("Combine"), "attributes": tr("Attributes"),
            "cleanup": tr("Clean up")}.get(sid, sid)


class Refusal(Exception):
    """The tool cannot run; ``level`` is "info" (do something first) or "warning" (cannot be done)."""

    def __init__(self, message, level="warning"):
        super().__init__(message)
        self.message = message
        self.level = level


class Tool:
    """One tool: ``interactive`` is None (Apply button) or what the map tool collects: "point", "points2",
    "line" (a sketch), "draw" (a new shape), "pivot" (an optional point, Apply still applies)."""

    def __init__(self, tid, section, name, icon, kinds, interactive=None, preview=False, selection=True,
                 hint="", words=""):
        self.id = tid
        self.section = section
        self.name = name
        self.icon = icon
        self.kinds = set(kinds)
        self.interactive = interactive
        self.preview = preview
        self.selection = selection
        self.hint = hint
        self.words = words

    def applies(self):
        return self.interactive in (None, "pivot")


LP = ("line", "polygon")
ALL = ("point", "line", "polygon")


def tools():
    """Every tool, in the order of the pane."""
    return [
        Tool("align_edge", "alignment", tr("Align edge"), "edit/align-edge", LP, "point",
             hint=tr("Click the target line or edge: vertices of the selected features within the tolerance "
                     "move onto it."), words="snap edge target"),
        Tool("transform", "alignment", tr("Move, rotate, scale"), "edit/transform", ALL, "pivot", preview=True,
             hint=tr("Type the values; click the map to choose a pivot point."), words="move rotate scale"),
        Tool("repeat_copy", "alignment", tr("Repeat copy"), "edit/repeat-copy", ALL, "pivot", preview=True,
             hint=tr("Copies each selected feature N times, every step moved and turned."),
             words="copy array duplicate rotate"),
        Tool("copy_parallel", "construct", tr("Copy parallel"), "edit/copy-parallel", LP, preview=True,
             words="offset parallel"),
        Tool("points_along", "construct", tr("Points along a line"), "edit/points-along", LP, preview=True,
             words="divide chainage station"),
        Tool("buffer_edit", "construct", tr("Buffer"), "edit/buffer-edit", ("polygon",), preview=True,
             words="grow shrink offset"),
        Tool("fillet", "construct", tr("Fillet"), "edit/fillet", LP, "point",
             hint=tr("Click the corner to round."), words="round corner arc curve"),
        Tool("split_sketch", "divide", tr("Split"), "edit/split-line", LP, "line",
             hint=tr("Draw a line across the selected features; F2 or right click to finish."), words="cut"),
        Tool("split_parts", "divide", tr("Split into parts"), "edit/split-parts", ("line",), preview=True,
             words="equal parts divide distance"),
        Tool("split_at_point", "divide", tr("Split at a point"), "edit/split-point", ("line",), "point",
             hint=tr("Click on the selected line where it should be split."), words="break"),
        Tool("two_point_cut", "divide", tr("Two-point cut"), "edit/two-point-cut", ("polygon",), "points2",
             selection=False, hint=tr("Click two points on the polygon's edge: it is cut along the straight "
                                      "line between them."), words="cut slice"),
        Tool("planarize", "divide", tr("Planarize"), "edit/planarize", ("line",), words="intersection node"),
        Tool("explode", "divide", tr("Explode"), "edit/explode", ALL, words="multipart single parts"),
        Tool("reshape", "reshape", tr("Reshape"), "edit/reshape", LP, "line",
             hint=tr("Draw a line that leaves and comes back to the edge; F2 or right click to finish.")),
        Tool("extend_trim", "reshape", tr("Extend or trim"), "edit/extend-trim", ("line",), "point",
             hint=tr("Select the target line, then click the end to extend or the piece to cut away."),
             words="extend trim"),
        Tool("edit_vertices", "reshape", tr("Edit vertices"), "edit/edit-vertices", ALL,
             words="vertex coordinates table"),
        Tool("generalize", "reshape", tr("Generalize"), "edit/generalize", LP, preview=True,
             words="simplify douglas peucker"),
        Tool("smooth", "reshape", tr("Smooth"), "edit/smooth", LP, preview=True, words="chaikin"),
        Tool("replace_geometry", "reshape", tr("Replace geometry"), "edit/replace-geometry", ALL, "draw",
             hint=tr("Draw the new shape of the selected feature; its attributes are kept.")),
        Tool("shared_edge", "reshape", tr("Reshape a shared edge"), "edit/shared-edge", ("polygon",), "line",
             selection=False, hint=tr("Draw the new edge from one end of the shared boundary to the other; "
                                      "both polygons follow."), words="topology boundary"),
        Tool("merge", "combine", tr("Merge"), "edit/merge", LP + ("point",), words="dissolve union combine"),
        Tool("clip", "combine", tr("Clip or erase"), "edit/clip", LP, "point",
             hint=tr("Click the polygon to clip the selected features with."), words="erase cut"),
        Tool("gap_fill", "combine", tr("Fill gaps"), "edit/gap-fill", ("polygon",), preview=True,
             words="sliver hole gap"),
        Tool("transfer", "attributes", tr("Transfer attributes"), "edit/transfer-attributes", ALL, "point",
             selection=False, hint=tr("Click the source feature, then click the features to fill."),
             words="copy attributes match"),
        Tool("change_values", "attributes", tr("Change values"), "edit/change-values", ALL,
             words="field calculate attribute"),
        Tool("remove_spikes", "cleanup", tr("Remove spikes"), "edit/remove-spikes", LP, preview=True,
             words="spike angle"),
        Tool("remove_duplicates", "cleanup", tr("Remove duplicate vertices"), "edit/remove-duplicates", LP,
             preview=True, words="tiny segments"),
        Tool("close_gaps", "cleanup", tr("Close line gaps"), "edit/close-gaps", ("line",), preview=True,
             words="dangle undershoot overshoot snap"),
    ]


def tool(tid):
    for t in tools():
        if t.id == tid:
            return t
    return None


class Opt:
    """An option of a tool: ``kind`` is "length", "angle", "number", "int", "choice", "bool", "text",
    "field", "point_layer", "feature", "rules", "fields", "vertices"."""

    def __init__(self, key, label, kind, default=None, lo=None, hi=None, choices=None, decimals=3, tip=""):
        self.key, self.label, self.kind, self.default = key, label, kind, default
        self.lo, self.hi, self.choices, self.decimals, self.tip = lo, hi, choices or [], decimals, tip


def options(tid):
    """The options of a tool (labels already translated)."""
    pivots = [("each", tr("Centre of each feature")), ("selection", tr("Centre of the selection")),
              ("click", tr("The point clicked on the map"))]
    return {
        "align_edge": [Opt("tolerance", tr("Tolerance"), "length", 0.5, 0.0001, 1e6)],
        "transform": [Opt("dx", tr("Move X"), "length", 0.0, -1e9, 1e9), Opt("dy", tr("Move Y"), "length", 0.0, -1e9, 1e9),
                      Opt("angle", tr("Rotate (clockwise)"), "angle", 0.0, -360, 360, decimals=4),
                      Opt("factor", tr("Scale factor"), "number", 1.0, 0.0001, 1e6, decimals=4),
                      Opt("pivot", tr("Pivot"), "choice", "each", choices=pivots)],
        "repeat_copy": [Opt("count", tr("Copies"), "int", 3, 1, 1000),
                        Opt("dx", tr("Step X"), "length", 10.0, -1e9, 1e9),
                        Opt("dy", tr("Step Y"), "length", 0.0, -1e9, 1e9),
                        Opt("dangle", tr("Step angle (clockwise)"), "angle", 0.0, -360, 360, decimals=4),
                        Opt("pivot", tr("Turn around"), "choice", "each", choices=pivots)],
        "copy_parallel": [Opt("distance", tr("Distance"), "length", 5.0, 0.0001, 1e7),
                          Opt("side", tr("Which side"), "choice", "both",
                              choices=[("left", tr("Left (outside for polygons)")),
                                       ("right", tr("Right (inside for polygons)")), ("both", tr("Both sides"))]),
                          Opt("join", tr("Corners"), "choice", "miter",
                              choices=[("miter", tr("Sharp")), ("round", tr("Round"))])],
        "points_along": [Opt("into", tr("Into layer"), "point_layer"),
                         Opt("along", tr("Points"), "choice", "distance",
                             choices=[("distance", tr("Every distance")), ("parts", tr("Equal parts"))]),
                         Opt("spacing", tr("Distance"), "length", 10.0, 0.0001, 1e7),
                         Opt("parts", tr("Parts"), "int", 4, 1, 100000),
                         Opt("ends", tr("Include the line's ends"), "bool", True)],
        "buffer_edit": [Opt("distance", tr("Distance"), "length", 1.0, -1e7, 1e7,
                            tip=tr("Negative values shrink the polygon"))],
        "fillet": [Opt("radius", tr("Radius"), "length", 5.0, 0.0001, 1e7),
                   Opt("chord", tr("Arc accuracy"), "length", 0.01, 0.0001, 100, decimals=4,
                       tip=tr("Largest gap between an arc and its straight segments when the layer cannot "
                              "store curves"))],
        "split_parts": [Opt("along", tr("Split"), "choice", "parts",
                            choices=[("parts", tr("Equal parts")), ("distance", tr("Every distance"))]),
                        Opt("parts", tr("Parts"), "int", 2, 2, 100000),
                        Opt("spacing", tr("Distance"), "length", 10.0, 0.0001, 1e7)],
        "split_at_point": [Opt("tolerance", tr("Search distance"), "length", 1.0, 0.0001, 1e6)],
        "extend_trim": [Opt("mode", tr("Action"), "choice", "extend",
                            choices=[("extend", tr("Extend")), ("trim", tr("Trim"))])],
        "edit_vertices": [Opt("vertices", "", "vertices")],
        "generalize": [Opt("tolerance", tr("Tolerance"), "length", 1.0, 0.0001, 1e7)],
        "smooth": [Opt("iterations", tr("Iterations"), "int", 3, 1, 10),
                   Opt("offset", tr("Corner cut"), "number", 0.25, 0.01, 0.5, decimals=2)],
        "merge": [Opt("main", tr("Keep the attributes of"), "feature"), Opt("rules", "", "rules")],
        "clip": [Opt("keep", tr("Keep the part"), "choice", "inside",
                     choices=[("inside", tr("The part inside (clip)")), ("outside", tr("The part outside (erase)"))])],
        "gap_fill": [Opt("width", tr("Largest gap width"), "length", 0.5, 0.0001, 1e6)],
        "transfer": [Opt("fields", tr("Fields"), "fields")],
        "change_values": [Opt("field", tr("Field"), "field"), Opt("value", tr("Value"), "text", ""),
                          Opt("empty", tr("Make it empty"), "bool", False)],
        "remove_spikes": [Opt("angle", tr("Sharper than"), "angle", 10.0, 0.01, 90, decimals=2)],
        "remove_duplicates": [Opt("tolerance", tr("Shorter than"), "length", 0.01, 0.0, 1e6, decimals=4)],
        "close_gaps": [Opt("tolerance", tr("Gap tolerance"), "length", 0.1, 0.0001, 1e6, decimals=4)],
    }.get(tid, [])


def defaults(tid):
    return {o.key: o.default for o in options(tid)}


def rule_items():
    """(rule id, label) of the merge rules per field."""
    return [("main", tr("From the kept feature")), ("first", tr("First value")),
            ("largest", tr("From the largest feature")), ("sum", tr("Sum")), ("mean", tr("Mean")),
            ("min", tr("Minimum")), ("max", tr("Maximum")), ("concat", tr("Join the values")),
            ("empty", tr("Leave empty"))]


# ---- context: the map, the selection and CRS -----------------------------------------------------------
def layer_kind(layer):
    return construct.geometry_kind(layer) if isinstance(layer, QgsVectorLayer) and layer.isSpatial() else ""


class Context:
    """What a tool works on: the canvas and the checked selected features {layer: [feature ids]}."""

    def __init__(self, canvas, selected=None, active=None, pivot=None):
        self.canvas = canvas
        self.selected = selected or {}
        self.active = active
        self.pivot = pivot           # (x, y) map CRS
        self.source = None           # transfer: (layer, feature)

    def map_crs(self):
        return self.canvas.mapSettings().destinationCrs()

    def _ct(self, layer, to_map=True):
        a, b = (layer.crs(), self.map_crs()) if to_map else (self.map_crs(), layer.crs())
        return QgsCoordinateTransform(a, b, QgsProject.instance())

    def to_map(self, layer, g):
        g = QgsGeometry(g)
        ct = self._ct(layer)
        if not ct.isShortCircuited():
            g.transform(ct)
        return g

    def feature_geoms(self, layer, fids):
        """[(feature, geometry in map CRS)] of ``fids`` (features without geometry left out)."""
        out = []
        if not fids:
            return out
        for f in layer.getFeatures(QgsFeatureRequest().setFilterFids(list(fids))):
            if f.hasGeometry() and not f.geometry().isEmpty():
                out.append((f, self.to_map(layer, f.geometry())))
        return out

    def metres_per_unit(self):
        crs = self.map_crs()
        if not crs.isValid():
            return 1.0
        return QgsUnitTypes.fromUnitToUnitFactor(crs.mapUnits(), Qgis.DistanceUnit.Meters) or 1.0

    def search_radius(self, px=12):
        return px * self.canvas.mapUnitsPerPixel()

    def features_at(self, p, kinds=ALL, layers=None, px=12):
        """[(layer, feature, map geometry, distance)] near map point ``p`` (nearest first)."""
        r = self.search_radius(px)
        rr = max(r, self.canvas.mapUnitsPerPixel() * 0.5)
        rect = QgsRectangle(p[0] - rr, p[1] - rr, p[0] + rr, p[1] + rr)
        pg = QgsGeometry.fromPointXY(QgsPointXY(*p))
        out = []
        cand = layers if layers is not None else [lyr for lyr in self.canvas.layers() if isinstance(lyr, QgsVectorLayer)]
        for lyr in cand:
            if layer_kind(lyr) not in kinds:
                continue
            ct = self._ct(lyr, to_map=False)
            try:
                lrect = ct.transformBoundingBox(rect) if not ct.isShortCircuited() else rect
            except Exception:  # noqa: BLE001 - a layer that cannot be reached is skipped
                continue
            for f in lyr.getFeatures(QgsFeatureRequest().setFilterRect(lrect)):
                if not f.hasGeometry():
                    continue
                g = self.to_map(lyr, f.geometry())
                d = g.distance(pg)
                if d <= r:
                    out.append((lyr, f, g, d))
        out.sort(key=lambda x: (x[3], -1 if x[0] is self.active else 0))
        return out


# ---- the result of a tool -------------------------------------------------------------------------------
class LayerEdit:
    """What a tool changes in one layer (geometries in map CRS)."""

    def __init__(self, layer):
        self.layer = layer
        self.updates = {}      # fid -> geometry
        self.attrs = {}        # fid -> {field index: value}
        self.adds = []         # (geometry, {field name: value})
        self.deletes = []      # fids

    def count(self):
        return len(set(self.updates) | set(self.attrs)) + len(self.adds) + len(self.deletes)


class Outcome:
    def __init__(self):
        self.edits = {}
        self.extra = ""        # a remark added to the success message
        self.skipped = 0

    def edit(self, layer):
        if layer.id() not in self.edits:
            self.edits[layer.id()] = LayerEdit(layer)
        return self.edits[layer.id()]

    def count(self):
        return sum(e.count() for e in self.edits.values())

    def geometries(self):
        """Every new or changed geometry (map CRS) - for the preview."""
        out = []
        for e in self.edits.values():
            out.extend(e.updates.values())
            out.extend(g for g, _a in e.adds)
        return out


def _attrs_of(feature, layer):
    pk = set(layer.primaryKeyAttributes())
    return {f.name(): feature.attribute(i) for i, f in enumerate(layer.fields()) if i not in pk}


def _check_editable(layer):
    if not layer.isEditable():
        raise Refusal(tr("Turn on editing for “{l}” first.").format(l=layer.name()), "warning")


def fit_to_layer(g, layer, ctx, chord=0.01):
    """A map-CRS geometry made right for ``layer`` (curves, CRS, Z/M, single/multi); None if impossible."""
    g = QgsGeometry(g)
    wkb = layer.wkbType()
    if QgsWkbTypes.isCurvedType(g.wkbType()) and not QgsWkbTypes.isCurvedType(wkb):
        tol = max(chord, 1e-9) / ctx.metres_per_unit()
        g = QgsGeometry(g.constGet().segmentize(tol, QgsAbstractGeometry.SegmentationToleranceType.MaximumDifference))
    ct = ctx._ct(layer, to_map=False)
    if not ct.isShortCircuited():
        g.transform(ct)
    geom = g.get()
    if QgsWkbTypes.hasZ(wkb) and not geom.is3D():
        geom.addZValue(0)
    if QgsWkbTypes.hasM(wkb) and not geom.isMeasure():
        geom.addMValue(0)
    if QgsWkbTypes.isMultiType(wkb) and not g.isMultipart():
        g.convertToMultiType()
    elif not QgsWkbTypes.isMultiType(wkb) and g.isMultipart():
        if len(mg.parts(g)) != 1:
            return None
        g.convertToSingleType()
    return g


def write(outcome, label, ctx, chord=0.01):
    """Write an Outcome: every geometry is checked first (nothing is written when one is wrong), then each
    layer gets one edit command. Returns the number of features changed / added / deleted."""
    prepared = []
    for e in outcome.edits.values():
        lyr = e.layer
        _check_editable(lyr)
        want = layer_kind(lyr)
        ups = {}
        for fid, g in e.updates.items():
            if not mg.usable(g, want):
                raise Refusal(invalid_message(want), "warning")
            lg = fit_to_layer(g, lyr, ctx, chord)
            if lg is None:
                raise Refusal(tr("The result has several parts but “{l}” stores single parts; nothing was "
                                 "changed.").format(l=lyr.name()), "warning")
            ups[fid] = lg
        adds = []
        for g, attrs in e.adds:
            if not mg.usable(g, want):
                raise Refusal(invalid_message(want), "warning")
            pieces = [g]
            lg = fit_to_layer(g, lyr, ctx, chord)
            if lg is None:   # several parts into a single-part layer: one feature each
                pieces = mg.parts(g)
            for pc in pieces:
                pg = lg if lg is not None else fit_to_layer(pc, lyr, ctx, chord)
                fields = lyr.fields()
                vals = {fields.indexOf(k): v for k, v in (attrs or {}).items() if fields.indexOf(k) >= 0}
                adds.append(QgsVectorLayerUtils.createFeature(lyr, pg, vals))
        renumber(lyr, adds)  # a split piece or a copy is a new feature: it gets its own number
        prepared.append((e, ups, adds))
    n = 0
    for e, ups, adds in prepared:
        lyr = e.layer
        lyr.beginEditCommand(label)
        ok = True
        for fid, g in ups.items():
            ok &= lyr.changeGeometry(fid, g)
        for fid, vals in e.attrs.items():
            for idx, val in vals.items():
                ok &= lyr.changeAttributeValue(fid, idx, val)
        if adds:
            ok &= lyr.addFeatures(adds)
        if e.deletes:
            ok &= lyr.deleteFeatures(list(e.deletes))
        if not ok:
            lyr.destroyEditCommand()
            raise Refusal(tr("“{l}” refused the change; nothing was changed.").format(l=lyr.name()), "warning")
        lyr.endEditCommand()
        lyr.triggerRepaint()
        n += len(set(ups) | set(e.attrs)) + len(adds) + len(e.deletes)
    return n


def kind_word(k):
    return {"point": tr("point features"), "line": tr("line features"), "polygon": tr("polygon features")}.get(k, k)


def invalid_message(k):
    if k == "point":
        return tr("The result would not be a valid point; nothing was changed.")
    if k == "line":
        return tr("The result would not be a valid line; nothing was changed.")
    return tr("The result would not be a valid polygon; nothing was changed.")


# ---- running a tool -------------------------------------------------------------------------------------
def targets(t, ctx, need_editable=True):
    """[(layer, [(feature, map geometry)])] of the checked selected features the tool can work on."""
    out = []
    wrong_kind = False
    for lyr, fids in ctx.selected.items():
        if not fids:
            continue
        if layer_kind(lyr) not in t.kinds:
            wrong_kind = True
            continue
        out.append((lyr, ctx.feature_geoms(lyr, fids)))
    if not out:
        if wrong_kind:
            raise Refusal(tr("“{t}” works on {k}.").format(
                t=t.name, k=" / ".join(kind_word(k) for k in sorted(t.kinds))), "warning")
        raise Refusal(tr("Make a selection."), "info")
    if need_editable:
        for lyr, _ in out:
            _check_editable(lyr)
    return out


def _pivot(opts, ctx, g, all_geoms):
    mode = opts.get("pivot", "each")
    if mode == "click":
        if ctx.pivot is None:
            raise Refusal(tr("Click the map to choose the pivot point."), "info")
        return ctx.pivot
    if mode == "selection":
        return mg.centre(QgsGeometry.unaryUnion([QgsGeometry(x) for x in all_geoms]).centroid())
    return mg.centre(g)


def run(tid, ctx, opts, inputs=None):
    """Run tool ``tid`` with ``opts`` and map ``inputs`` ({"points": [(x, y)…], "line": geometry,
    "geometry": geometry}); returns an Outcome (nothing is written)."""
    t = tool(tid)
    o = dict(defaults(tid))
    o.update(opts or {})
    inputs = inputs or {}
    fn = globals().get("_run_" + tid)
    if fn is None:
        raise Refusal(tr("This tool is not available."), "warning")
    try:
        return fn(t, ctx, o, inputs)
    except mg.GeometryError as ex:
        raise Refusal(str(ex), "warning") from None


def _each(t, ctx, fn, need_editable=True):
    """Outcome of ``fn(layer, feature, geometry) -> new geometry | None`` on every target feature."""
    out = Outcome()
    for lyr, feats in targets(t, ctx, need_editable):
        for f, g in feats:
            ng = fn(lyr, f, g)
            if ng is None:
                out.skipped += 1
                continue
            out.edit(lyr).updates[f.id()] = ng
    return out


def _add_pieces(out, lyr, f, pieces):
    """The first piece replaces the feature, the others are new features with its attributes."""
    e = out.edit(lyr)
    e.updates[f.id()] = pieces[0]
    attrs = _attrs_of(f, lyr)
    for pc in pieces[1:]:
        e.adds.append((pc, attrs))


def _point(inputs, i=0):
    pts = inputs.get("points") or []
    if len(pts) <= i:
        raise Refusal(tr("Click on the map."), "info")
    return pts[i]


# alignment
def _run_align_edge(t, ctx, o, inputs):
    p = _point(inputs)
    sel = {(lyr.id(), fid) for lyr, fids in ctx.selected.items() for fid in fids}
    hits = [h for h in ctx.features_at(p, LP) if (h[0].id(), h[1].id()) not in sel]
    if not hits:
        raise Refusal(tr("No line or edge under the click."), "info")
    target = hits[0][2]
    moved = [0]

    def fn(lyr, f, g):
        ng, n = mg.align_edge(g, target, o["tolerance"])
        moved[0] += n
        return ng if n else None

    out = _each(t, ctx, fn)
    if not out.count():
        raise Refusal(tr("No vertex lies within the tolerance of the target."), "info")
    return out


def _run_transform(t, ctx, o, inputs):
    tg = targets(t, ctx)
    allg = [g for _l, fs in tg for _f, g in fs]
    if not (o["dx"] or o["dy"] or o["angle"] or abs(o["factor"] - 1) > 1e-12):
        raise Refusal(tr("Type a distance, an angle or a scale."), "info")
    out = Outcome()
    for lyr, feats in tg:
        for f, g in feats:
            out.edit(lyr).updates[f.id()] = mg.transform(g, o["dx"], o["dy"], o["angle"], o["factor"],
                                                         _pivot(o, ctx, g, allg))
    return out


def _run_repeat_copy(t, ctx, o, inputs):
    tg = targets(t, ctx)
    allg = [g for _l, fs in tg for _f, g in fs]
    out = Outcome()
    for lyr, feats in tg:
        for f, g in feats:
            attrs = _attrs_of(f, lyr)
            for c in mg.repeat_copies(g, o["count"], o["dx"], o["dy"], o["dangle"], _pivot(o, ctx, g, allg)):
                out.edit(lyr).adds.append((c, attrs))
    return out


# construct
def _run_copy_parallel(t, ctx, o, inputs):
    out = Outcome()
    for lyr, feats in targets(t, ctx):
        for f, g in feats:
            attrs = _attrs_of(f, lyr)
            for c in mg.copy_parallel(g, o["distance"], o["side"], o["join"]):
                out.edit(lyr).adds.append((c, attrs))
    return out


def _run_points_along(t, ctx, o, inputs):
    into = QgsProject.instance().mapLayer(o.get("into") or "")
    if into is None or layer_kind(into) != "point":
        raise Refusal(tr("Choose the point layer to add the points to."), "info")
    _check_editable(into)
    out = Outcome()
    for lyr, feats in targets(t, ctx, need_editable=False):
        for f, g in feats:
            pts = mg.points_along(g, spacing=o["spacing"], count=o["parts"] if o["along"] == "parts" else None,
                                  include_ends=o["ends"])
            for pt in pts:
                out.edit(into).adds.append((pt, {}))
    return out


def _run_buffer_edit(t, ctx, o, inputs):
    return _each(t, ctx, lambda lyr, f, g: mg.buffer(g, o["distance"]))


def _run_fillet(t, ctx, o, inputs):
    p = _point(inputs)
    hits = _selected_hits(t, ctx, p)
    lyr, f, g, _d = hits[0]
    _check_editable(lyr)
    at, _dist = mg.closest_vertex(g, p)
    curves = QgsWkbTypes.isCurvedType(lyr.wkbType())
    out = Outcome()
    out.edit(lyr).updates[f.id()] = mg.fillet(g, at, o["radius"], curves=curves,
                                              chord=o["chord"] / ctx.metres_per_unit())
    return out


def _selected_hits(t, ctx, p, px=12):
    """Selected features of the tool's kinds near the click (or, with no selection, those of the active
    layer under the click)."""
    layers = [lyr for lyr, fids in ctx.selected.items() if fids and layer_kind(lyr) in t.kinds]
    sel = {(lyr.id(), fid) for lyr, fids in ctx.selected.items() for fid in fids}
    if layers:
        hits = [h for h in ctx.features_at(p, t.kinds, layers, px) if (h[0].id(), h[1].id()) in sel]
    elif ctx.active is not None and layer_kind(ctx.active) in t.kinds:
        hits = ctx.features_at(p, t.kinds, [ctx.active], px)
    else:
        raise Refusal(tr("Make a selection."), "info")
    if not hits:
        raise Refusal(tr("Click on a selected feature."), "info")
    return hits


# divide
def _run_split_sketch(t, ctx, o, inputs):
    blade = inputs.get("line")
    if blade is None:
        raise Refusal(tr("Draw the line to split with."), "info")
    out = Outcome()
    for lyr, feats in targets(t, ctx):
        for f, g in feats:
            if not g.boundingBox().intersects(blade.boundingBox()):
                continue
            pieces = mg.split_by_line(g, blade)
            if pieces and len(pieces) > 1:
                _add_pieces(out, lyr, f, pieces)
    if not out.count():
        raise Refusal(tr("The line does not cross the selected features."), "info")
    return out


def _run_split_parts(t, ctx, o, inputs):
    out = Outcome()
    for lyr, feats in targets(t, ctx):
        for f, g in feats:
            pieces = mg.split_into_parts(g, o["parts"]) if o["along"] == "parts" else mg.split_every(g, o["spacing"])
            _add_pieces(out, lyr, f, pieces)
    return out


def _run_split_at_point(t, ctx, o, inputs):
    p = _point(inputs)
    hits = _selected_hits(t, ctx, p, px=max(12, o["tolerance"] / max(ctx.canvas.mapUnitsPerPixel(), 1e-12)))
    out = Outcome()
    for lyr, f, g, _d in hits[:1]:
        _check_editable(lyr)
        _add_pieces(out, lyr, f, mg.split_at_point(g, p))
    return out


def _run_two_point_cut(t, ctx, o, inputs):
    p1, p2 = _point(inputs, 0), _point(inputs, 1)
    mid = ((p1[0] + p2[0]) / 2.0, (p1[1] + p2[1]) / 2.0)
    hits = []
    sel = [lyr for lyr, fids in ctx.selected.items() if fids and layer_kind(lyr) == "polygon"]
    if sel:
        selset = {(lyr.id(), fid) for lyr, fids in ctx.selected.items() for fid in fids}
        hits = [h for h in ctx.features_at(mid, ("polygon",), sel, 0) if (h[0].id(), h[1].id()) in selset]
    if not hits and ctx.active is not None and layer_kind(ctx.active) == "polygon":
        hits = ctx.features_at(mid, ("polygon",), [ctx.active], 0)
    if not hits:
        raise Refusal(tr("Click two points on the edge of a polygon."), "info")
    lyr, f, g, _d = hits[0]
    _check_editable(lyr)
    out = Outcome()
    _add_pieces(out, lyr, f, mg.two_point_cut(g, p1, p2))
    return out


def _run_planarize(t, ctx, o, inputs):
    out = Outcome()
    for lyr, feats in targets(t, ctx):
        res = mg.planarize([g for _f, g in feats])
        by_src = {}
        for i, piece in res:
            by_src.setdefault(i, []).append(piece)
        for i, pieces in by_src.items():
            if len(pieces) > 1:
                _add_pieces(out, lyr, feats[i][0], pieces)
    if not out.count():
        raise Refusal(tr("The selected lines do not cross each other."), "info")
    return out


def _run_explode(t, ctx, o, inputs):
    out = Outcome()
    for lyr, feats in targets(t, ctx):
        for f, g in feats:
            if len(mg.parts(g)) > 1:
                _add_pieces(out, lyr, f, mg.explode(g))
    if not out.count():
        raise Refusal(tr("The selected features have only one part each."), "info")
    return out


# reshape
def _run_reshape(t, ctx, o, inputs):
    line = inputs.get("line")
    if line is None:
        raise Refusal(tr("Draw the reshape line."), "info")
    out = _each(t, ctx, lambda lyr, f, g: mg.reshape(g, line))
    if not out.count():
        raise Refusal(tr("The line must leave the edge and come back to it."), "info")
    return out


def _run_extend_trim(t, ctx, o, inputs):
    p = _point(inputs)
    if not any(ctx.selected.values()):
        raise Refusal(tr("Select the target line first."), "info")
    tgeoms = [g for lyr, fids in ctx.selected.items() if layer_kind(lyr) in LP for _f, g in ctx.feature_geoms(lyr, fids)]
    if not tgeoms:
        raise Refusal(tr("Select the target line first."), "info")
    target = QgsGeometry.unaryUnion([mg.flat(g) for g in tgeoms])
    sel = {(lyr.id(), fid) for lyr, fids in ctx.selected.items() for fid in fids}
    layers = [ctx.active] if ctx.active is not None and layer_kind(ctx.active) == "line" else None
    hits = [h for h in ctx.features_at(p, ("line",), layers) if (h[0].id(), h[1].id()) not in sel]
    if not hits:
        raise Refusal(tr("Click a line of the layer being edited."), "info")
    lyr, f, g, _d = hits[0]
    _check_editable(lyr)
    out = Outcome()
    if o["mode"] == "trim":
        _add_pieces(out, lyr, f, mg.trim_line(g, target, p))
    else:
        out.edit(lyr).updates[f.id()] = mg.extend_line(g, target, mg.nearest_end(g, p))
    return out


def _run_edit_vertices(t, ctx, o, inputs):
    """``o["moves"]`` = {vertex index: (x, y)} in the layer's CRS, for the single selected feature."""
    tg = targets(t, ctx)
    feats = [(lyr, f) for lyr, fs in tg for f, _g in fs]
    if len(feats) != 1:
        raise Refusal(tr("Select one feature to edit its vertices."), "info")
    lyr, f = feats[0]
    moves = o.get("moves") or {}
    if not moves:
        raise Refusal(tr("Type new coordinates in the table first."), "info")
    ng = mg.move_vertices(f.geometry(), moves)
    out = Outcome()
    out.edit(lyr).updates[f.id()] = ctx.to_map(lyr, ng)
    return out


def _run_generalize(t, ctx, o, inputs):
    return _each(t, ctx, lambda lyr, f, g: mg.generalize(g, o["tolerance"]))


def _run_smooth(t, ctx, o, inputs):
    return _each(t, ctx, lambda lyr, f, g: mg.smooth(g, o["iterations"], o["offset"]))


def _run_replace_geometry(t, ctx, o, inputs):
    g = inputs.get("geometry")
    tg = targets(t, ctx)
    feats = [(lyr, f) for lyr, fs in tg for f, _g in fs]
    if len(feats) != 1:
        raise Refusal(tr("Select the one feature whose shape is replaced."), "info")
    if g is None:
        raise Refusal(tr("Draw the new shape."), "info")
    lyr, f = feats[0]
    out = Outcome()
    out.edit(lyr).updates[f.id()] = g
    return out


def _run_shared_edge(t, ctx, o, inputs):
    line = inputs.get("line")
    if line is None:
        raise Refusal(tr("Draw the new edge."), "info")
    pl = mg.flat(line).asPolyline()
    a, b = (pl[0].x(), pl[0].y()), (pl[-1].x(), pl[-1].y())
    layers = [ctx.active] if ctx.active is not None and layer_kind(ctx.active) == "polygon" else \
        [lyr for lyr in ctx.selected if layer_kind(lyr) == "polygon"]
    if not layers:
        raise Refusal(tr("Make the polygon layer the current layer."), "info")
    near_a = {(h[0].id(), h[1].id()): h for h in ctx.features_at(a, ("polygon",), layers)}
    near_b = {(h[0].id(), h[1].id()): h for h in ctx.features_at(b, ("polygon",), layers)}
    both = [near_a[k] for k in near_a if k in near_b]
    if len(both) < 2:
        raise Refusal(tr("Start and end the line on the boundary shared by two polygons."), "info")
    (la, fa, ga, _), (lb, fb, gb, _) = both[0], both[1]
    if la is not lb:
        raise Refusal(tr("The two polygons must be in the same layer."), "warning")
    _check_editable(la)
    try:
        na, nb = mg.shared_edge_reshape(ga, gb, line, ctx.canvas.mapUnitsPerPixel())
    except mg.GeometryError:
        nb, na = mg.shared_edge_reshape(gb, ga, line, ctx.canvas.mapUnitsPerPixel())
    out = Outcome()
    out.edit(la).updates[fa.id()] = na
    out.edit(la).updates[fb.id()] = nb
    return out


# combine
def _run_merge(t, ctx, o, inputs):
    out = Outcome()
    tg = targets(t, ctx)
    for lyr, feats in tg:
        if len(feats) < 2:
            continue
        main = 0
        for i, (f, _g) in enumerate(feats):
            if o.get("main") == f"{lyr.id()}|{f.id()}":
                main = i
        rows = [_attrs_of(f, lyr) for f, _g in feats]
        sizes = [g.area() if layer_kind(lyr) == "polygon" else g.length() for _f, g in feats]
        vals = mg.merge_attributes(rows, main, o.get("rules") or {}, sizes)
        geom = mg.merge_geometries([g for _f, g in feats])
        e = out.edit(lyr)
        keep = feats[main][0]
        e.updates[keep.id()] = geom
        fields = lyr.fields()
        e.attrs[keep.id()] = {fields.indexOf(k): v for k, v in vals.items() if keep.attribute(k) != v}
        e.deletes = [f.id() for i, (f, _g) in enumerate(feats) if i != main]
    if not out.count():
        raise Refusal(tr("Select two or more features of one layer to merge."), "info")
    return out


def _run_clip(t, ctx, o, inputs):
    p = _point(inputs)
    sel = {(lyr.id(), fid) for lyr, fids in ctx.selected.items() for fid in fids}
    hits = [h for h in ctx.features_at(p, ("polygon",), None, 0) if (h[0].id(), h[1].id()) not in sel]
    if not hits:
        raise Refusal(tr("Click inside the polygon to clip with."), "info")
    cutter = hits[0][2]
    out = _each(t, ctx, lambda lyr, f, g: mg.clip(g, cutter, o["keep"] == "inside") if g.intersects(cutter) and not g.touches(cutter) else None)
    if not out.count():
        raise Refusal(tr("Nothing would be left of the selected features (or nothing overlaps)."), "info")
    return out


def _run_gap_fill(t, ctx, o, inputs):
    out = Outcome()
    for lyr, feats in targets(t, ctx):
        res = mg.gap_fill([g for _f, g in feats], o["width"])
        for i, g in res.items():
            out.edit(lyr).updates[feats[i][0].id()] = g
    if not out.count():
        raise Refusal(tr("No gaps up to this width between the selected polygons."), "info")
    return out


# attributes
def _run_transfer(t, ctx, o, inputs):
    p = _point(inputs)
    if ctx.source is None:
        hits = ctx.features_at(p)
        if not hits:
            raise Refusal(tr("Click the source feature."), "info")
        ctx.source = (hits[0][0], hits[0][1])
        out = Outcome()
        out.extra = "source"
        return out
    lyr = ctx.active
    if lyr is None or not isinstance(lyr, QgsVectorLayer):
        raise Refusal(tr("Make the layer to fill the current layer."), "info")
    _check_editable(lyr)
    src_layer, src = ctx.source
    hits = [h for h in ctx.features_at(p, ALL, [lyr]) if not (h[0] is src_layer and h[1].id() == src.id())]
    if not hits:
        raise Refusal(tr("Click a feature of “{l}”.").format(l=lyr.name()), "info")
    target = hits[0][1]
    names = [f.name() for i, f in enumerate(lyr.fields()) if i not in set(lyr.primaryKeyAttributes())]
    allowed = o.get("fields")
    if allowed:
        names = [n for n in names if n in allowed]
    vals = mg.transfer_values({f.name(): src.attribute(f.name()) for f in src_layer.fields()}, names)
    if not vals:
        raise Refusal(tr("No field of the source matches a field of “{l}”.").format(l=lyr.name()), "warning")
    out = Outcome()
    fields = lyr.fields()
    out.edit(lyr).attrs[target.id()] = {fields.indexOf(k): v for k, v in vals.items()}
    return out


def coerce(text, field):
    """A typed text as a value of ``field`` (raises Refusal when it does not fit)."""
    if text is None:
        return None
    try:
        ok_val = field.convertCompatible(text)
    except ValueError:
        raise Refusal(tr("“{v}” does not fit the field “{f}”.").format(v=text, f=field.name()), "warning") from None
    return ok_val


def _run_change_values(t, ctx, o, inputs):
    out = Outcome()
    tg = targets(t, ctx, need_editable=True)
    name = o.get("field")
    if not name:
        raise Refusal(tr("Choose the field to change."), "info")
    for lyr, feats in tg:
        idx = lyr.fields().indexOf(name)
        if idx < 0:
            continue
        val = None if o.get("empty") else coerce(o.get("value", ""), lyr.fields().at(idx))
        for f, _g in feats:
            out.edit(lyr).attrs[f.id()] = {idx: val}
    if not out.count():
        raise Refusal(tr("The selected layers have no field “{f}”.").format(f=name), "warning")
    return out


# clean up
def _run_remove_spikes(t, ctx, o, inputs):
    def fn(lyr, f, g):
        ng, n = mg.remove_spikes(g, o["angle"])
        return ng if n else None
    out = _each(t, ctx, fn)
    if not out.count():
        raise Refusal(tr("No spikes sharper than this angle."), "info")
    return out


def _run_remove_duplicates(t, ctx, o, inputs):
    def fn(lyr, f, g):
        ng, n = mg.remove_duplicate_vertices(g, o["tolerance"])
        return ng if n else None
    out = _each(t, ctx, fn)
    if not out.count():
        raise Refusal(tr("No duplicate vertices or segments this short."), "info")
    return out


def _run_close_gaps(t, ctx, o, inputs):
    out = Outcome()
    for lyr, feats in targets(t, ctx):
        res = mg.close_line_gaps([g for _f, g in feats], o["tolerance"])
        for i, g in res.items():
            out.edit(lyr).updates[feats[i][0].id()] = g
    if not out.count():
        raise Refusal(tr("No gaps or overshoots up to this size."), "info")
    return out


def display_name(layer, feature):
    """The feature's display name (the layer's display expression) or "#id"."""
    expr = layer.displayExpression()
    if expr:
        try:
            ctx = QgsExpressionContext(QgsExpressionContextUtils.globalProjectLayerScopes(layer))
            ctx.setFeature(feature)
            val = QgsExpression(expr).evaluate(ctx)
            if val not in (None, "") and str(val) != "NULL":
                return str(val)
        except Exception:  # noqa: BLE001 - the id is enough
            pass
    return f"#{feature.id()}"


# ---- map tools -------------------------------------------------------------------------------------------
def _band_colour():
    return QColor(18, 165, 148)


class ClickTool(QgsMapTool):
    """Collects ``need`` snapped clicks (map CRS) and emits ``pointsReady``; Esc with nothing pending
    emits ``escape``; for two points a rubber line follows the cursor."""
    pointsReady = pyqtSignal(list)
    escape = pyqtSignal()

    def __init__(self, canvas, need=1):
        super().__init__(canvas)
        self.need = need
        self.points = []
        self.cursor = None
        self._marks = self._line = None
        self.setCursor(Qt.CursorShape.CrossCursor)

    def isEditTool(self):  # noqa: N802
        return True

    def activate(self):
        super().activate()
        c = self.canvas()
        if self._marks is None:
            self._marks = QgsRubberBand(c, Qgis.GeometryType.Point)
            self._marks.setColor(_band_colour())
            self._marks.setIcon(QgsRubberBand.IconType.ICON_BOX)
            self._marks.setIconSize(8)
            self._line = QgsRubberBand(c, Qgis.GeometryType.Line)
            self._line.setColor(_band_colour())
            self._line.setWidth(2)

    def deactivate(self):
        self.reset()
        for b in (self._marks, self._line):
            if b is not None:
                self.canvas().scene().removeItem(b)
        self._marks = self._line = None
        super().deactivate()

    def reset(self):
        self.points = []
        self._draw()

    def snapped(self, e):
        orig = e.originalMapPoint()
        m = self.canvas().snappingUtils().snapToMap(orig)
        p = m.point() if m.isValid() else orig
        return (p.x(), p.y())

    def canvasMoveEvent(self, e):  # noqa: N802
        self.cursor = self.snapped(e)
        self._draw()

    def canvasReleaseEvent(self, e):  # noqa: N802
        if e.button() == Qt.MouseButton.RightButton:
            self.reset()
            return
        if e.button() != Qt.MouseButton.LeftButton:
            return
        self.add(self.snapped(e))

    def add(self, p):
        self.points.append(p)
        if len(self.points) >= self.need:
            pts, self.points = self.points, []
            self._draw()
            self.pointsReady.emit(pts)
        else:
            self._draw()

    def keyPressEvent(self, e):  # noqa: N802
        if e.key() == Qt.Key.Key_Escape:
            if self.points:
                self.reset()
            else:
                self.escape.emit()
            e.accept()
            return
        e.ignore()

    def _draw(self):
        if self._marks is None:
            return
        self._marks.reset(Qgis.GeometryType.Point)
        for p in self.points:
            self._marks.addPoint(QgsPointXY(*p), False)
        self._marks.updatePosition()
        self._marks.update()
        self._line.reset(Qgis.GeometryType.Line)
        if self.points and self.cursor is not None and self.need > 1:
            self._line.addPoint(QgsPointXY(*self.points[-1]), False)
            self._line.addPoint(QgsPointXY(*self.cursor), True)


class SketchTool(construct.ConstructTool):
    """Create Features' drawing tool (typed lengths and directions, arcs, HUD, snapping, tracing) used to
    draw a line or a shape for a modify tool: the finished geometry (map CRS) is emitted by ``drawn``
    instead of being added to a layer."""
    drawn = pyqtSignal(object)
    escape = pyqtSignal()

    def __init__(self, canvas, cad, layer, shape="line", options=None):
        mode = {"line": "polyline", "polygon": "polygon", "point": "point"}[shape]
        super().__init__(canvas, cad, layer, None, mode, options)
        self.kind = {"line": construct.LINE, "polygon": construct.POLYGON, "point": construct.POINT}[shape]

    def commit(self, geoms):
        if geoms:
            self.drawn.emit(QgsGeometry(geoms[0]))
        return []

    def handle_key(self, key, text="", mods=Qt.KeyboardModifier.NoModifier):
        used = super().handle_key(key, text, mods)
        if not used and key == Qt.Key.Key_Escape:
            self.escape.emit()
            return True
        return used


def preview_band(canvas):
    b = QgsRubberBand(canvas, Qgis.GeometryType.Line)
    b.setColor(QColor(255, 120, 0, 220))
    b.setFillColor(QColor(255, 120, 0, 40))
    b.setWidth(2)
    return b


def show_preview(band, geoms, mupp=1.0):
    band.reset(Qgis.GeometryType.Line)
    for g in geoms[:2000]:
        g = mg.flat(g)
        if g.type() == Qgis.GeometryType.Polygon:
            for ring in mg.fill._parts(g):
                band.addGeometry(ring, None, False)
        elif g.type() == Qgis.GeometryType.Point:
            for ring in mg.fill._parts(g.buffer(mupp * 4, 4)):
                band.addGeometry(ring, None, False)
        else:
            band.addGeometry(g, None, False)
    band.updatePosition()
    band.update()

