"""Google Earth export: one .kmz with a folder per layer, the layer colours,
a name per placemark (the label, or the layer's display name) and all
attributes in the placemark balloon."""
import os

from osgeo import ogr, osr
from qgis.core import QgsCoordinateTransform, QgsFeatureRequest, QgsGeometry, QgsVectorLayer, QgsWkbTypes

from ..i18n import tr
from .styles import ExpressionEvaluator, LayerStyler, geometry_kind, label_expression
from .webmap import WGS84, json_value


def _kml_color(hex_rgb, alpha):
    """OGR style colour #RRGGBBAA."""
    a = max(0, min(255, round(alpha * 255)))
    return f"{hex_rgb}{a:02x}"


def ogr_style(kind, st):
    pen = f'PEN(c:{_kml_color(st["color"], st["opacity"] if st.get("stroke", True) else 0)},w:{st["weight"]:.1f}px)'
    if kind == "polygon":
        return f'BRUSH(fc:{_kml_color(st["fillColor"], st["fillOpacity"])});{pen}'
    if kind == "line":
        return pen
    scale = max(0.4, min(3.0, st.get("radius", 6) / 8))
    return f'SYMBOL(c:{_kml_color(st["fillColor"], max(st["fillOpacity"], 0.6))},s:{scale:.2f})'


def _name_expression(layer):
    return label_expression(layer) or layer.displayExpression() or None


def _field_name(name, used):
    base = "".join(ch if ch.isalnum() or ch == "_" else "_" for ch in name).strip("_") or "field"
    if base.lower() in ("name", "description"):
        base += "_"
    cand, k = base, 2
    while cand.lower() in used:
        cand, k = f"{base}_{k}", k + 1
    used.add(cand.lower())
    return cand


def export_kmz(layers, path, project=None):
    """Write the layers to `path` (.kmz); returns (path, {layer name: placemarks})."""
    from qgis.core import QgsProject

    project = project or QgsProject.instance()
    layers = [lyr for lyr in layers if isinstance(lyr, QgsVectorLayer) and lyr.isSpatial()
              and geometry_kind(lyr) is not None]
    if not layers:
        raise ValueError(tr("Choose at least one vector layer."))
    if not path.lower().endswith(".kmz"):
        path += ".kmz"
    if os.path.exists(path):
        os.remove(path)
    drv = ogr.GetDriverByName("LIBKML")
    if drv is None:
        raise RuntimeError("LIBKML driver is not available")
    ds = drv.CreateDataSource(path)
    if ds is None:
        raise RuntimeError(tr("Could not write {p}").format(p=path))
    srs = osr.SpatialReference()
    srs.ImportFromEPSG(4326)
    srs.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    written = {}
    try:
        for lyr in layers:
            kind = geometry_kind(lyr)
            out = ds.CreateLayer(lyr.name(), srs, ogr.wkbUnknown)
            out.CreateField(ogr.FieldDefn("Name", ogr.OFTString))
            used = {"name", "description"}
            cols = []
            for i, fld in enumerate(lyr.fields()):
                if i in set(lyr.dataProvider().pkAttributeIndexes()) and lyr.providerType() == "ogr":
                    continue
                if lyr.editorWidgetSetup(i).type() == "Hidden":
                    continue
                col = _field_name(fld.name(), used)
                out.CreateField(ogr.FieldDefn(col, ogr.OFTString))
                cols.append((i, col))
            defn = out.GetLayerDefn()
            xform = QgsCoordinateTransform(lyr.crs(), WGS84, project.transformContext())
            name_of = ExpressionEvaluator(lyr, _name_expression(lyr))
            n = 0
            with LayerStyler(lyr) as styler:
                for f in lyr.getFeatures(QgsFeatureRequest()):
                    g = f.geometry()
                    if g is None or g.isNull() or g.isEmpty():
                        continue
                    s = styler.style_index(f)
                    if s is None:
                        continue
                    g = QgsGeometry(g)
                    if QgsWkbTypes.isCurvedType(g.wkbType()):
                        g = QgsGeometry(g.constGet().segmentize())
                    g.transform(xform)
                    of = ogr.Feature(defn)
                    of.SetGeometry(ogr.CreateGeometryFromWkb(bytes(g.asWkb())))
                    nm = name_of(f)
                    if nm:
                        of.SetField("Name", nm)
                    for i, col in cols:
                        v = json_value(f.attribute(i))
                        if v is not None:
                            of.SetField(col, str(v))
                    of.SetStyleString(ogr_style(kind, styler.styles[s]))
                    out.CreateFeature(of)
                    n += 1
            written[lyr.name()] = n
    finally:
        ds = None  # noqa: F841 - closing the data source writes the file
    return path, written
