"""Google Earth tours and faithful KML/KMZ import - for people moving from
Google Earth Pro (its desktop downloads end in June 2027).

export_tour(): one KMZ with the layers as styled placemarks (colours, names,
attributes in the balloon) and a guided tour (gx:Tour) that flies to every
feature in turn and opens its balloon, or flies along a route line like a
drone. It plays in Google Earth Pro, Google Earth on the web and Cesium.

import_kml(): opens every folder of a KML/KMZ as a layer and keeps the
original colours (the per-placemark styles are read by GDAL and drawn with the
embedded-symbol renderer).
"""
import html
import math
import os
import zipfile

from qgis.core import (Qgis, QgsCoordinateReferenceSystem, QgsCoordinateTransform, QgsFeatureRequest, QgsGeometry,
                       QgsPointXY, QgsProject, QgsVectorLayer, QgsWkbTypes)

from ..i18n import tr
from ..version import APP_NAME
from .styles import ExpressionEvaluator, LayerStyler, geometry_kind, label_expression
from .webmap import WGS84, json_value, popup_fields

KML_NS = ('xmlns="http://www.opengis.net/kml/2.2" xmlns:gx="http://www.google.com/kml/ext/2.2" '
          'xmlns:kml="http://www.opengis.net/kml/2.2"')
POINT_ICON = "https://maps.google.com/mapfiles/kml/shapes/placemark_circle.png"


def kml_color(hex_rgb, alpha=1.0):
    """#RRGGBB + opacity -> KML aabbggrr."""
    h = (hex_rgb or "#3388ff").lstrip("#")
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    r, g, b = h[0:2], h[2:4], h[4:6]
    a = max(0, min(255, round((1.0 if alpha is None else alpha) * 255)))
    return f"{a:02x}{b}{g}{r}".lower()


def _esc(text):
    return html.escape(str(text), quote=True)


def _cdata(text):
    return "<![CDATA[" + str(text).replace("]]>", "]]]]><![CDATA[>") + "]]>"


def _style_xml(sid, kind, st):
    line = (f"<LineStyle><color>{kml_color(st.get('color'), st.get('opacity', 1) if st.get('stroke', True) else 0)}"
            f"</color><width>{max(0.5, float(st.get('weight', 2))):.1f}</width></LineStyle>")
    if kind == "polygon":
        poly = (f"<PolyStyle><color>{kml_color(st.get('fillColor'), st.get('fillOpacity', 0.25))}</color>"
                f"<fill>1</fill><outline>1</outline></PolyStyle>")
        return f'<Style id="{sid}">{line}{poly}</Style>'
    if kind == "line":
        return f'<Style id="{sid}">{line}</Style>'
    scale = max(0.5, min(3.0, float(st.get("radius", 6)) / 6))
    icon = (f"<IconStyle><color>{kml_color(st.get('fillColor'), max(st.get('fillOpacity', 1), 0.8))}</color>"
            f"<scale>{scale:.2f}</scale><Icon><href>{POINT_ICON}</href></Icon></IconStyle>")
    return f'<Style id="{sid}">{icon}<LabelStyle><scale>0.8</scale></LabelStyle></Style>'


def _coords(points, z=None):
    return " ".join(f"{x:.7f},{y:.7f}" + (f",{z:.2f}" if z is not None else "") for x, y in points)


def _geometry_xml(g):
    """A WGS 84 QgsGeometry as KML (multi parts become MultiGeometry)."""
    parts = g.asGeometryCollection() if g.isMultipart() else [g]
    out = []
    for p in parts:
        kind = p.type()
        if kind == Qgis.GeometryType.Point:
            pt = p.asPoint()
            out.append(f"<Point><coordinates>{pt.x():.7f},{pt.y():.7f}</coordinates></Point>")
        elif kind == Qgis.GeometryType.Line:
            out.append(f"<LineString><tessellate>1</tessellate><coordinates>{_coords(p.asPolyline())}"
                       f"</coordinates></LineString>")
        elif kind == Qgis.GeometryType.Polygon:
            rings = p.asPolygon()
            if not rings:
                continue
            inner = "".join(f"<innerBoundaryIs><LinearRing><coordinates>{_coords(r)}</coordinates></LinearRing>"
                            f"</innerBoundaryIs>" for r in rings[1:])
            out.append(f"<Polygon><tessellate>1</tessellate><outerBoundaryIs><LinearRing><coordinates>"
                       f"{_coords(rings[0])}</coordinates></LinearRing></outerBoundaryIs>{inner}</Polygon>")
    if not out:
        return ""
    return out[0] if len(out) == 1 else "<MultiGeometry>" + "".join(out) + "</MultiGeometry>"


def _balloon(layer, f, fields):
    rows = "".join(f"<tr><th style='text-align:start;padding:2px 8px'>{_esc(name)}</th>"
                   f"<td style='padding:2px 8px'>{_esc(json_value(f.attribute(i)))}</td></tr>"
                   for i, name in fields if f.attribute(i) not in (None, ""))
    return f"<table>{rows}</table>" if rows else ""


def _look_at(lon, lat, rng, tilt, heading, altitude_mode="relativeToGround"):
    return (f"<LookAt><longitude>{lon:.7f}</longitude><latitude>{lat:.7f}</latitude><altitude>0</altitude>"
            f"<heading>{heading:.1f}</heading><tilt>{tilt:.1f}</tilt><range>{rng:.1f}</range>"
            f"<altitudeMode>{altitude_mode}</altitudeMode></LookAt>")


def _range_for(g, minimum=150.0):
    """Camera distance that frames a WGS 84 geometry."""
    b = g.boundingBox()
    lat = b.center().y()
    w = b.width() * 111320 * math.cos(math.radians(lat))
    h = b.height() * 110570
    return max(minimum, 2.2 * math.hypot(w, h))


def route_samples(line_wgs84, step_m=200.0):
    """[(lon, lat, heading°)] every step_m metres along a WGS 84 line (heading = direction of travel)."""
    c = line_wgs84.centroid().asPoint()
    zone = int((c.x() + 180) // 6) % 60 + 1
    utm = QgsCoordinateReferenceSystem(f"EPSG:{(32600 if c.y() >= 0 else 32700) + zone}")
    ctx = QgsProject.instance().transformContext()
    g = QgsGeometry(line_wgs84)
    g.transform(QgsCoordinateTransform(WGS84, utm, ctx))
    if g.isMultipart():
        g = QgsGeometry.fromPolylineXY(sum((part for part in g.asMultiPolyline()), []))
    length = g.length()
    if length <= 0:
        return []
    back = QgsCoordinateTransform(utm, WGS84, ctx)
    n = max(2, int(length // step_m) + 1)
    pts = []
    for k in range(n + 1):
        d = min(length, k * length / n)
        p = g.interpolate(d).asPoint()
        ahead = g.interpolate(min(length, d + max(1.0, step_m / 2))).asPoint()
        if d >= length - 1e-6:
            prev = g.interpolate(max(0, d - max(1.0, step_m / 2))).asPoint()
            heading = math.degrees(math.atan2(p.x() - prev.x(), p.y() - prev.y()))
        else:
            heading = math.degrees(math.atan2(ahead.x() - p.x(), ahead.y() - p.y()))
        q = back.transform(QgsPointXY(p))
        pts.append((q.x(), q.y(), heading % 360))
    return pts


def build_tour_kml(layers, title="", fly_seconds=3.0, wait_seconds=2.0, tilt=45.0, heading=0.0, min_range=150.0,
                   selected_only=False, route=None, route_speed=60.0, route_step=200.0, route_range=600.0,
                   route_tilt=65.0, project=None):
    """The KML text. layers: vector layers (their features become placemarks and tour stops, in layer
    order); route: optional line layer flown along instead of (or after) visiting the features."""
    project = project or QgsProject.instance()
    title = title or tr("Tour")
    styles, folders, stops = [], [], []
    for li, lyr in enumerate(layers):
        if not isinstance(lyr, QgsVectorLayer) or not lyr.isSpatial():
            continue
        kind = geometry_kind(lyr)
        if kind is None:
            continue
        xf = QgsCoordinateTransform(lyr.crs(), WGS84, project.transformContext())
        name_of = ExpressionEvaluator(lyr, label_expression(lyr) or lyr.displayExpression() or None)
        fields = popup_fields(lyr)
        req = QgsFeatureRequest()
        if selected_only and lyr.selectedFeatureCount():
            req.setFilterFids(lyr.selectedFeatureIds())
        marks = []
        with LayerStyler(lyr) as styler:
            for f in lyr.getFeatures(req):
                g = f.geometry()
                if g is None or g.isNull() or g.isEmpty():
                    continue
                s = styler.style_index(f)
                if s is None:
                    continue
                g = QgsGeometry(g)
                if QgsWkbTypes.isCurvedType(g.wkbType()):
                    g = QgsGeometry(g.constGet().segmentize())
                g.transform(xf)
                geom = _geometry_xml(g)
                if not geom:
                    continue
                pid = f"p{li}_{f.id()}"
                nm = name_of(f) or f"{lyr.name()} {f.id()}"
                marks.append(f'<Placemark id="{pid}"><name>{_esc(nm)}</name>'
                             f"<description>{_cdata(_balloon(lyr, f, fields))}</description>"
                             f"<styleUrl>#s{li}_{s}</styleUrl>{geom}</Placemark>")
                c = g.centroid().asPoint()
                stops.append((pid, c.x(), c.y(), _range_for(g, min_range)))
            for s, st in enumerate(styler.styles):
                styles.append(_style_xml(f"s{li}_{s}", kind, st))
        folders.append(f"<Folder><name>{_esc(lyr.name())}</name>{''.join(marks)}</Folder>")

    playlist = []
    for pid, lon, lat, rng in stops:
        playlist.append(f"<gx:FlyTo><gx:duration>{fly_seconds:.1f}</gx:duration><gx:flyToMode>bounce"
                        f"</gx:flyToMode>{_look_at(lon, lat, rng, tilt, heading)}</gx:FlyTo>")
        playlist.append(_balloon_update(pid, 1))
        playlist.append(f"<gx:Wait><gx:duration>{wait_seconds:.1f}</gx:duration></gx:Wait>")
        playlist.append(_balloon_update(pid, 0))
    route_xml = ""
    if route is not None:
        rxf = QgsCoordinateTransform(route.crs(), WGS84, project.transformContext())
        req = QgsFeatureRequest()
        if selected_only and route.selectedFeatureCount():
            req.setFilterFids(route.selectedFeatureIds())
        coords = []
        for f in route.getFeatures(req):
            g = QgsGeometry(f.geometry())
            if g.isNull() or g.type() != Qgis.GeometryType.Line:
                continue
            g.transform(rxf)
            samples = route_samples(g, route_step)
            if not samples:
                continue
            seconds = max(0.2, route_step / max(route_speed, 1.0))
            first = True
            for lon, lat, hd in samples:
                playlist.append(f"<gx:FlyTo><gx:duration>{(fly_seconds if first else seconds):.2f}</gx:duration>"
                                f"<gx:flyToMode>{'bounce' if first else 'smooth'}</gx:flyToMode>"
                                f"{_look_at(lon, lat, route_range, route_tilt, hd)}</gx:FlyTo>")
                first = False
            coords.append(_coords([(x, y) for x, y, _h in samples]))
        if coords:
            styles.append('<Style id="route"><LineStyle><color>ff00a5ff</color><width>4</width></LineStyle></Style>')
            route_xml = (f"<Folder><name>{_esc(route.name())}</name>" + "".join(
                f"<Placemark><name>{_esc(route.name())}</name><styleUrl>#route</styleUrl><LineString>"
                f"<tessellate>1</tessellate><coordinates>{c}</coordinates></LineString></Placemark>"
                for c in coords) + "</Folder>")
    tour = (f"<gx:Tour><name>{_esc(tr('Play the tour'))}: {_esc(title)}</name><gx:Playlist>{''.join(playlist)}"
            f"</gx:Playlist></gx:Tour>") if playlist else ""
    first_view = ""
    if stops:
        _pid, lon, lat, rng = stops[0]
        first_view = _look_at(lon, lat, rng * 3, 0, 0)
    return (f'<?xml version="1.0" encoding="UTF-8"?>\n<kml {KML_NS}><Document><name>{_esc(title)}</name>'
            f"<description>{_cdata(tr('Made with {app}').format(app=APP_NAME))}</description><open>1</open>"
            f"{first_view}{''.join(styles)}{tour}{''.join(folders)}{route_xml}</Document></kml>\n")


def _balloon_update(pid, visible):
    return (f"<gx:AnimatedUpdate><gx:duration>0.0</gx:duration><Update><targetHref/><Change>"
            f'<Placemark targetId="{pid}"><gx:balloonVisibility>{visible}</gx:balloonVisibility></Placemark>'
            f"</Change></Update></gx:AnimatedUpdate>")


def export_tour(layers, path, title="", **options):
    """Write the tour to `path` (.kmz, or .kml); returns (path, number of stops)."""
    text = build_tour_kml(layers, title, **options)
    if not path.lower().endswith((".kmz", ".kml")):
        path += ".kmz"
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    if path.lower().endswith(".kml"):
        with open(path, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
    else:
        with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
            z.writestr("doc.kml", text.encode("utf-8"))
    return path, text.count("<gx:FlyTo>")


# ---- import --------------------------------------------------------------------------
def kml_sublayers(path):
    """Names of the layers (folders) GDAL finds in a KML/KMZ."""
    from osgeo import gdal, ogr

    with gdal.config_options({"LIBKML_RESOLVE_STYLE": "YES"}):
        ds = ogr.Open(path)
        if ds is None:
            return []
        names = [ds.GetLayer(i).GetName() for i in range(ds.GetLayerCount())
                 if ds.GetLayer(i).GetFeatureCount() != 0]
    return names


def import_kml(path, project=None, add=True):
    """Open every non-empty folder of a KML/KMZ as a layer that keeps the original colours; returns the
    layers (added to the project unless add=False)."""
    from osgeo import gdal
    from qgis.core import QgsEmbeddedSymbolRenderer, QgsSymbol

    project = project or QgsProject.instance()
    gdal.SetConfigOption("LIBKML_RESOLVE_STYLE", "YES")  # styleUrl -> per-feature style the renderer can draw
    base = os.path.splitext(os.path.basename(path))[0]
    out = []
    for name in kml_sublayers(path):
        lyr = QgsVectorLayer(f"{path}|layername={name}", f"{base} - {name}" if name != base else base, "ogr")
        if not lyr.isValid():
            continue
        caps = lyr.dataProvider().capabilities()
        has_symbology = bool(caps & Qgis.VectorProviderCapability.FeatureSymbology) if hasattr(
            Qgis, "VectorProviderCapability") else False
        if has_symbology and not isinstance(lyr.renderer(), QgsEmbeddedSymbolRenderer):
            lyr.setRenderer(QgsEmbeddedSymbolRenderer(QgsSymbol.defaultSymbol(lyr.geometryType())))
        if add:
            project.addMapLayer(lyr)
        out.append(lyr)
    return out
