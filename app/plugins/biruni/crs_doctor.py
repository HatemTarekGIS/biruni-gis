"""«Why don't my layers line up?» - the coordinate-system checks a beginner
(and a busy expert) needs, in plain words, each with a one-click fix.

analyse(project) looks at every vector and raster layer and returns Finding
objects; apply_fix(project, fix) carries out a fix. Nothing is changed without
a fix being applied, and the data files are never rewritten: a CRS is assigned
to the layer, a transformation is chosen in the project, or a corrected copy is
made in memory.

Checks
* no / unknown coordinate system
* numbers that do not fit the coordinate system the layer claims (degrees read
  as metres, metres read as degrees, X and Y swapped) - with the system that
  fits best (the "What coordinate system is this?" ranking)
* a layer far away from all the others (the classic "it's in the ocean")
* features at 0,0 ("Null Island": empty coordinates turned into zeros)
* a shapefile without its .prj
* datums: when a layer and the project use different datums, the candidate
  transformations and how far apart they put the data (in metres)
* measuring in degrees (geographic project) or in Web Mercator (areas and
  lengths too large away from the equator)
"""
import math
import os

from qgis.core import (QgsCoordinateReferenceSystem, QgsCoordinateTransform, QgsCsException, QgsDatumTransform,
                       QgsDistanceArea, QgsFeatureRequest, QgsGeometry, QgsPointXY, QgsProject, QgsRasterLayer,
                       QgsVectorLayer)

from .i18n import tr

WGS84 = QgsCoordinateReferenceSystem("EPSG:4326")
FAR_KM = 300            # a layer this far from every other layer is "somewhere else"
NULL_ISLAND_DEG = 0.01  # about 1 km around 0,0
SAMPLE = 400


class Finding:
    """One problem: severity "error" | "warning" | "info", plain text, and fixes
    (dicts understood by apply_fix: {"label", "action", ...})."""

    def __init__(self, kind, severity, layer, title, detail, fixes=None, data=None):
        self.kind, self.severity, self.layer_id = kind, severity, layer.id() if layer is not None else None
        self.layer_name = layer.name() if layer is not None else ""
        self.title, self.detail, self.fixes, self.data = title, detail, fixes or [], data or {}

    def __repr__(self):
        return f"<Finding {self.kind} {self.layer_name}: {self.title}>"


# ---- small helpers -------------------------------------------------------------------
def _haversine_km(a, b):
    p1, p2 = math.radians(a[1]), math.radians(b[1])
    h = (math.sin((p2 - p1) / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(math.radians(b[0] - a[0]) / 2) ** 2)
    return 2 * 6371.0088 * math.asin(min(1.0, math.sqrt(h)))


def _distance_m(a, b):
    """Ellipsoidal distance in metres between two WGS 84 points."""
    d = QgsDistanceArea()
    d.setSourceCrs(WGS84, QgsProject.instance().transformContext())
    d.setEllipsoid("EPSG:7030")
    return d.measureLine(QgsPointXY(*a), QgsPointXY(*b))


def sample_points(layer, n=SAMPLE):
    """Raw (x, y) of up to n vertices, in the layer's own coordinates."""
    pts = []
    if isinstance(layer, QgsVectorLayer):
        req = QgsFeatureRequest().setNoAttributes().setLimit(n)
        for f in layer.getFeatures(req):
            g = f.geometry()
            if g is None or g.isNull() or g.isEmpty():
                continue
            v = next(iter(g.vertices()), None)
            if v is not None and math.isfinite(v.x()) and math.isfinite(v.y()):
                pts.append((v.x(), v.y()))
    elif isinstance(layer, QgsRasterLayer):
        e = layer.extent()
        if not e.isEmpty():
            pts = [(e.xMinimum(), e.yMinimum()), (e.xMaximum(), e.yMaximum()), (e.center().x(), e.center().y())]
    return pts


def looks_like_degrees(pts):
    return bool(pts) and all(-180 <= x <= 180 and -90 <= y <= 90 for x, y in pts)


def looks_like_swapped_degrees(pts):
    """Latitude in X and longitude in Y: |x| <= 90 everywhere, |y| > 90 somewhere."""
    return bool(pts) and all(-90 <= x <= 90 and -180 <= y <= 180 for x, y in pts) and \
        any(abs(y) > 90 for x, y in pts)


def to_wgs84(layer, pts, context):
    if not layer.crs().isValid():
        return []
    xf = QgsCoordinateTransform(layer.crs(), WGS84, context)
    out = []
    for x, y in pts:
        try:
            p = xf.transform(QgsPointXY(x, y))
        except QgsCsException:
            continue
        if math.isfinite(p.x()) and math.isfinite(p.y()) and -180 <= p.x() <= 180 and -90 <= p.y() <= 90:
            out.append((p.x(), p.y()))
    return out


def _centre(pts):
    return (sum(p[0] for p in pts) / len(pts), sum(p[1] for p in pts) / len(pts)) if pts else None


def utm_for(lon, lat):
    zone = int((lon + 180) // 6) % 60 + 1
    return QgsCoordinateReferenceSystem(f"EPSG:{(32600 if lat >= 0 else 32700) + zone}")


def best_guess(layer, pts, context, location=None):
    """(authid, swapped, distance_km) of the best-fitting coordinate system, or None."""
    from .algorithms.guess_crs import Land, candidate_codes, evaluate, rank, spread

    pts = spread(pts, 50)
    if not pts:
        return None
    ranked = rank(evaluate(pts, candidate_codes(location), context, location=location, land=Land()))
    if not ranked:
        return None
    top = ranked[0]
    return top["crs"], top["swapped"], top["distance_km"], top.get("country", "")


# ---- analysis --------------------------------------------------------------------------
def analyse(project=None, layers=None, guess=True):
    """Findings for the layers of `project` (default: the current project), most serious first."""
    project = project or QgsProject.instance()
    ctx = project.transformContext()
    layers = layers if layers is not None else [lyr for lyr in project.mapLayers().values()
                                                 if isinstance(lyr, (QgsVectorLayer, QgsRasterLayer))]
    findings = []
    centres = {}
    for lyr in layers:
        if isinstance(lyr, QgsVectorLayer) and not lyr.isSpatial():
            continue
        pts = sample_points(lyr)
        if not pts:
            continue
        crs = lyr.crs()
        # a shapefile without .prj
        src = lyr.source().split("|")[0]
        if src.lower().endswith(".shp") and not os.path.exists(src[:-4] + ".prj"):
            findings.append(Finding("missing_prj", "warning", lyr, tr("The shapefile has no .prj file"),
                                    tr("Without its .prj, the coordinate system of {f} was guessed or set by "
                                       "hand. Make sure it is right, then save a copy so the .prj is written.")
                                    .format(f=os.path.basename(src)),
                                    [{"label": tr("Find the right coordinate system"), "action": "guess",
                                      "layer": lyr.id()}]))
        if not crs.isValid():
            fixes = [{"label": tr("Find the right coordinate system"), "action": "guess", "layer": lyr.id()}]
            g = best_guess(lyr, pts, ctx) if guess else None
            if g:
                fixes.insert(0, {"label": tr("Use {c}").format(c=_crs_label(g[0])), "action": "assign_crs",
                                 "layer": lyr.id(), "crs": g[0], "swapped": g[1]})
            findings.append(Finding("no_crs", "error", lyr, tr("No coordinate system"),
                                    tr("This layer does not say which coordinate system its numbers are in, so it "
                                       "cannot be placed on the map correctly."), fixes))
            continue
        degrees = looks_like_degrees(pts)
        if crs.isGeographic() and not degrees:
            if looks_like_swapped_degrees(pts):
                findings.append(Finding("swapped", "error", lyr, tr("Latitude and longitude are swapped"),
                                        tr("The X values look like latitudes and the Y values like longitudes."),
                                        [{"label": tr("Make a corrected copy (swap X and Y)"), "action": "swap_xy",
                                          "layer": lyr.id()}]))
            else:
                fixes = []
                g = best_guess(lyr, pts, ctx) if guess else None
                if g:
                    fixes.append({"label": tr("Use {c}").format(c=_crs_label(g[0])), "action": "assign_crs",
                                  "layer": lyr.id(), "crs": g[0], "swapped": g[1]})
                fixes.append({"label": tr("Find the right coordinate system"), "action": "guess", "layer": lyr.id()})
                findings.append(Finding("metres_as_degrees", "error", lyr,
                                        tr("Metres stored as degrees"),
                                        tr("The layer says its coordinates are degrees ({c}), but the numbers are far "
                                           "too large for degrees - they are metres in a projected system.")
                                        .format(c=crs.authid() or crs.description()), fixes))
            continue
        if not crs.isGeographic() and degrees and _projected_degrees_suspicious(crs, pts):
            fixes = [{"label": tr("Use WGS 84 (degrees)"), "action": "assign_crs", "layer": lyr.id(),
                      "crs": "EPSG:4326", "swapped": False}]
            findings.append(Finding("degrees_as_metres", "error", lyr, tr("Degrees stored as metres"),
                                    tr("The layer says {c} (metres), but its numbers look like latitude and "
                                       "longitude, so it lands a few metres from the system's origin.")
                                    .format(c=crs.authid() or crs.description()), fixes))
            continue
        wgs = to_wgs84(lyr, pts, ctx)
        if not wgs:
            continue
        near_zero = sum(1 for lo, la in wgs if abs(lo) < NULL_ISLAND_DEG and abs(la) < NULL_ISLAND_DEG)
        if near_zero:
            findings.append(Finding("null_island", "warning", lyr, tr("Features at 0,0 (\"Null Island\")"),
                                    tr("{n} feature(s) sit at latitude 0, longitude 0 in the Atlantic Ocean - "
                                       "usually empty coordinates that became zeros.").format(n=near_zero),
                                    [{"label": tr("Select them"), "action": "select_null_island",
                                      "layer": lyr.id()}], {"count": near_zero}))
        good = [p for p in wgs if not (abs(p[0]) < NULL_ISLAND_DEG and abs(p[1]) < NULL_ISLAND_DEG)]
        if good:
            centres[lyr.id()] = (_centre(good), lyr)
        findings.extend(datum_findings(lyr, project, _centre(good) if good else None))

    findings.extend(_far_away(centres, ctx))
    findings.extend(_measurement_findings(project, [c for c, _l in centres.values()]))
    order = {"error": 0, "warning": 1, "info": 2}
    findings.sort(key=lambda f: order[f.severity])
    return findings


def _projected_degrees_suspicious(crs, pts):
    """Small numbers in a projected system: true unless the system's own valid area really is near its origin."""
    b = crs.bounds()
    if b.isEmpty():
        return True
    xf = QgsCoordinateTransform(WGS84, crs, QgsProject.instance().transformContext())
    try:
        r = xf.transformBoundingBox(b)
    except QgsCsException:
        return True
    # if the system's area of use itself lies within a few hundred metres of 0,0 the numbers may be genuine
    return not (abs(r.xMinimum()) < 400 and abs(r.yMinimum()) < 400)


def _crs_label(authid):
    c = QgsCoordinateReferenceSystem(authid)
    return f"{c.description()} ({authid})" if c.isValid() else authid


def _far_away(centres, ctx):
    """Layers whose data lies far from every other layer. With only two layers there is no telling which one
    is wrong, so the later one is reported."""
    out = []
    items = list(centres.values())
    if len(items) < 2:
        return out
    for i, (c, lyr) in enumerate(items):
        nearest = min(_haversine_km(c, o) for j, (o, _l) in enumerate(items) if j != i)
        if nearest <= FAR_KM or (len(items) == 2 and i == 0):
            continue
        out.append(Finding("far_away", "warning", lyr, tr("This layer is far from the others"),
                           tr("Its data is {d:,.0f} km from the nearest other layer. If it should be in the same "
                              "place, its coordinate system is probably wrong.").format(d=nearest),
                           [{"label": tr("Find the right coordinate system"), "action": "guess",
                             "layer": lyr.id()}], {"km": nearest}))
    return out


def _measurement_findings(project, centres):
    out = []
    pcrs = project.crs()
    if not centres or not pcrs.isValid():
        return out
    # the median layer position: one misplaced layer must not move the advice
    lons, lats = sorted(c[0] for c in centres), sorted(c[1] for c in centres)
    lon, lat = lons[len(lons) // 2], lats[len(lats) // 2]
    utm = utm_for(lon, lat)
    fix = [{"label": tr("Use {c} for the project").format(c=_crs_label(utm.authid())), "action": "project_crs",
            "crs": utm.authid()}]
    if pcrs.isGeographic():
        out.append(Finding("geographic_project", "info", None, tr("The map is in degrees"),
                           tr("Distances and areas measured on the map are in degrees unless the tool measures on "
                              "the ellipsoid. For buffers, grids and snapping tolerances in metres use a projected "
                              "system such as {c}.").format(c=_crs_label(utm.authid())), fix))
    elif pcrs.authid() == "EPSG:3857":
        k = 1 / max(math.cos(math.radians(lat)), 1e-6)
        out.append(Finding("web_mercator", "info", None, tr("Web Mercator stretches distances"),
                           tr("At latitude {lat:.0f}° Web Mercator draws lengths {k:.2f}× and areas {a:.2f}× too "
                              "large. Measure in {c} instead.").format(lat=lat, k=k, a=k * k,
                                                                       c=_crs_label(utm.authid())), fix,
                           {"length_factor": k}))
    return out


# ---- datum transformations ----------------------------------------------------------------
def datum_findings(layer, project, centre_wgs84):
    """When layer and project datums differ: the available transformations, how far apart they put the
    data, and which one is used now."""
    src, dst = layer.crs(), project.crs()
    if centre_wgs84 is None or not src.isValid() or not dst.isValid():
        return []
    if src.datumEnsemble().isValid() and dst.datumEnsemble().isValid() and \
            src.datumEnsemble().name() == dst.datumEnsemble().name():
        return []
    try:
        ops = QgsDatumTransform.operations(src, dst)
    except Exception:  # noqa: BLE001
        return []
    shifts = transformation_shifts(src, dst, centre_wgs84, ops)
    if len(shifts) < 2:
        return []
    spread_m = max(s["offset_m"] for s in shifts)
    if spread_m < 0.5:
        return []
    current = project.transformContext().calculateCoordinateOperation(src, dst)
    lines = []
    for s in shifts:
        acc = f" (±{s['accuracy']:g} m)" if s["accuracy"] and s["accuracy"] > 0 else ""
        lines.append(f"• {s['name']}{acc}: {s['offset_m']:.1f} m" + (" ✓" if s["proj"] == current else ""))
    best = min((s for s in shifts if s["available"]), key=lambda s: (s["accuracy"] if s["accuracy"] and
                                                                      s["accuracy"] > 0 else 1e9),
               default=None)
    fixes = []
    if best is not None and best["proj"] != current:
        fixes.append({"label": tr("Use the most accurate: {n}").format(n=best["name"]), "action": "set_transform",
                      "src": src.authid(), "dst": dst.authid(), "proj": best["proj"]})
    return [Finding("datum", "warning" if spread_m >= 5 else "info", layer,
                    tr("The datum transformation moves this layer by up to {m:.0f} m").format(m=spread_m),
                    tr("{a} and the map ({b}) use different datums. The ways to convert between them put the data "
                       "this far from the first one:").format(a=src.authid() or src.description(),
                                                              b=dst.authid() or dst.description())
                    + "\n" + "\n".join(lines), fixes, {"spread_m": spread_m, "shifts": shifts})]


def transformation_shifts(src, dst, centre_wgs84, ops):
    """For each candidate operation: where the layer centre lands, and its offset in metres from the first."""
    to_src = QgsCoordinateTransform(WGS84, src, QgsProject.instance().transformContext())
    try:
        p_src = to_src.transform(QgsPointXY(*centre_wgs84))
    except QgsCsException:
        return []
    out = []
    ref = None
    for op in ops:
        if not op.isAvailable:
            continue
        xf = QgsCoordinateTransform(src, dst, QgsProject.instance().transformContext())
        xf.setCoordinateOperation(op.proj)
        xf.setAllowFallbackTransforms(False)
        try:
            p = xf.transform(p_src)
            back = QgsCoordinateTransform(dst, WGS84, QgsProject.instance().transformContext()).transform(p)
        except QgsCsException:
            continue
        if ref is None:
            ref = back
        out.append({"name": op.name, "proj": op.proj, "accuracy": op.accuracy, "available": op.isAvailable,
                    "lonlat": (back.x(), back.y()),
                    "offset_m": _distance_m((ref.x(), ref.y()), (back.x(), back.y()))})
    return out


# ---- fixes ----------------------------------------------------------------------------
def apply_fix(fix, project=None, iface=None):
    """Carry out one fix dict from a Finding. Returns a short text of what was done (or a layer)."""
    project = project or QgsProject.instance()
    action = fix["action"]
    lyr = project.mapLayer(fix.get("layer", "")) if fix.get("layer") else None
    if action == "assign_crs":
        crs = QgsCoordinateReferenceSystem(fix["crs"])
        if fix.get("swapped"):
            lyr = swapped_copy(lyr, project)
        lyr.setCrs(crs)
        lyr.triggerRepaint()
        return tr("{l} now uses {c}.").format(l=lyr.name(), c=_crs_label(fix["crs"]))
    if action == "swap_xy":
        copy = swapped_copy(lyr, project)
        return tr("Corrected copy added: {l}").format(l=copy.name())
    if action == "project_crs":
        project.setCrs(QgsCoordinateReferenceSystem(fix["crs"]))
        return tr("The map now uses {c}.").format(c=_crs_label(fix["crs"]))
    if action == "set_transform":
        tc = project.transformContext()
        tc.addCoordinateOperation(QgsCoordinateReferenceSystem(fix["src"]), QgsCoordinateReferenceSystem(fix["dst"]),
                                  fix["proj"])
        project.setTransformContext(tc)
        return tr("Transformation chosen for {a} → {b}.").format(a=fix["src"], b=fix["dst"])
    if action == "select_null_island":
        ids = null_island_ids(lyr, project)
        lyr.selectByIds(ids)
        return tr("{n} feature(s) selected.").format(n=len(ids))
    if action == "guess":
        if iface is not None:
            import processing

            processing.execAlgorithmDialog("biruni:guesscrs", {"INPUT": lyr.id()} if lyr else {})
        return tr("Opened «What coordinate system is this?».")
    raise ValueError(action)


def null_island_ids(layer, project=None):
    project = project or QgsProject.instance()
    xf = QgsCoordinateTransform(layer.crs(), WGS84, project.transformContext())
    ids = []
    for f in layer.getFeatures(QgsFeatureRequest().setNoAttributes()):
        g = f.geometry()
        if g is None or g.isNull():
            continue
        try:
            c = xf.transform(g.boundingBox().center())
        except QgsCsException:
            continue
        if abs(c.x()) < NULL_ISLAND_DEG and abs(c.y()) < NULL_ISLAND_DEG:
            ids.append(f.id())
    return ids


def swapped_copy(layer, project=None):
    """A memory copy of a vector layer with X and Y exchanged (the original is not touched)."""
    project = project or QgsProject.instance()
    from qgis.core import QgsFeature, QgsWkbTypes

    uri = f"{QgsWkbTypes.displayString(layer.wkbType())}?crs={layer.crs().authid()}"
    copy = QgsVectorLayer(uri, tr("{l} (X/Y swapped)").format(l=layer.name()), "memory")
    copy.dataProvider().addAttributes(layer.fields().toList())
    copy.updateFields()
    feats = []
    for f in layer.getFeatures():
        g = QgsGeometry(f.geometry())
        if not g.isNull():
            g.get().swapXy()
        nf = QgsFeature(copy.fields())
        nf.setGeometry(g)
        nf.setAttributes(f.attributes())
        feats.append(nf)
    copy.dataProvider().addFeatures(feats)
    project.addMapLayer(copy)
    return copy


def summary_text(findings):
    """All findings as plain text (for a message box or a report)."""
    if not findings:
        return tr("No coordinate-system problems found: the layers should line up.")
    mark = {"error": "✖", "warning": "!", "info": "i"}
    lines = []
    for f in findings:
        head = f"{mark[f.severity]} {f.layer_name + ': ' if f.layer_name else ''}{f.title}"
        lines.append(head + "\n   " + f.detail.replace("\n", "\n   "))
    return "\n\n".join(lines)
