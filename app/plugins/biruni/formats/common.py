"""Shared model of the national-format readers: a file becomes a few plain layers (name, geometry kind,
fields, features as WKT + attributes), written to a GeoPackage with OGR. Readers stay free of QGIS so
they also run in scripts and tests."""
import os
import re

from osgeo import ogr, osr

KINDS = {"point": ogr.wkbPoint25D, "line": ogr.wkbLineString25D, "polygon": ogr.wkbMultiPolygon25D,
         "none": ogr.wkbNone}
_OGR_TYPES = {"str": ogr.OFTString, "int": ogr.OFTInteger64, "float": ogr.OFTReal}


class FormatError(ValueError):
    """The file is not in the expected format or is damaged; the message is for the user."""


class Layer:
    """One output layer: kind is point / line / polygon / none, fields an ordered {name: str|int|float}."""

    def __init__(self, name, kind, fields=None):
        self.name, self.kind = name, kind
        self.fields = dict(fields or {})
        self.features = []  # (wkt or None, {field: value})

    def add(self, wkt, **attrs):
        for k, v in attrs.items():
            if k not in self.fields:
                self.fields[k] = "float" if isinstance(v, float) else "int" if isinstance(v, int) and \
                    not isinstance(v, bool) else "str"
        self.features.append((wkt, attrs))

    def __len__(self):
        return len(self.features)

    def __repr__(self):
        return f"<Layer {self.name} {self.kind} {len(self)}>"


class Dataset:
    """What a reader returns: layers, the coordinate system if the file states one (EPSG code, WKT or
    PROJ text) and notes worth showing the user (skipped records, guesses made)."""

    def __init__(self, fmt, crs=None):
        self.format, self.crs = fmt, crs
        self.layers = {}
        self.notes = []

    def layer(self, name, kind, fields=None):
        key = (name, kind)
        if key not in self.layers:
            self.layers[key] = Layer(name, kind, fields)
        return self.layers[key]

    def non_empty(self):
        return [lyr for lyr in self.layers.values() if lyr.features]

    def count(self):
        return sum(len(lyr) for lyr in self.layers.values())


def _srs(crs):
    if not crs:
        return None
    srs = osr.SpatialReference()
    srs.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    text = str(crs)
    ok = srs.SetFromUserInput(text) == 0 if not text.isdigit() else srs.ImportFromEPSG(int(text)) == 0
    return srs if ok else None


def safe_name(name, used):
    base = re.sub(r"[^\w]+", "_", str(name), flags=re.UNICODE).strip("_")[:50] or "layer"
    cand, k = base, 2
    while cand.lower() in used:
        cand, k = f"{base}_{k}", k + 1
    used.add(cand.lower())
    return cand


def write_gpkg(dataset, path, crs=None):
    """Write every non-empty layer to the GeoPackage at path (replaced). Returns [(table, kind, count)]."""
    if os.path.exists(path):
        os.remove(path)
    drv = ogr.GetDriverByName("GPKG")
    ds = drv.CreateDataSource(path)
    if ds is None:
        raise FormatError(f"Could not write {path}")
    srs = _srs(crs or dataset.crs)
    written, used = [], set()
    try:
        for lyr in dataset.non_empty():
            table = safe_name(f"{lyr.name}_{lyr.kind}" if lyr.kind != "none" else lyr.name, used)
            kind = KINDS[lyr.kind]
            out = ds.CreateLayer(table, srs if kind != ogr.wkbNone else None, kind)
            names, fused = {}, {"fid", "geom"}
            for fname, ftype in lyr.fields.items():
                col = safe_name(fname, fused)
                out.CreateField(ogr.FieldDefn(col, _OGR_TYPES.get(ftype, ogr.OFTString)))
                names[fname] = col
            defn = out.GetLayerDefn()
            out.StartTransaction()
            for wkt, attrs in lyr.features:
                f = ogr.Feature(defn)
                if wkt and kind != ogr.wkbNone:
                    g = ogr.CreateGeometryFromWkt(wkt)
                    if g is None:
                        continue
                    if lyr.kind == "polygon":
                        g = ogr.ForceToMultiPolygon(g)
                    f.SetGeometry(g)
                for k, v in attrs.items():
                    if v is not None:
                        f.SetField(names[k], v if lyr.fields[k] != "str" else str(v))
                out.CreateFeature(f)
            out.CommitTransaction()
            written.append((table, lyr.kind, len(lyr)))
    finally:
        ds = None  # noqa: F841 - closing writes the file
    return written


def ring_wkt(points, close=True):
    pts = list(points)
    if close and pts and pts[0] != pts[-1]:
        pts.append(pts[0])
    return "(" + ", ".join(" ".join(_num(c) for c in p) for p in pts) + ")"


def _num(v):
    return repr(float(v))


def point_wkt(x, y, z=0.0):
    return f"POINT Z ({_num(x)} {_num(y)} {_num(z)})"


def line_wkt(points):
    pts = [p if len(p) == 3 else (p[0], p[1], 0.0) for p in points]
    return "LINESTRING Z " + ring_wkt(pts, close=False)


def polygon_wkt(rings):
    """rings: [[(x, y[, z]), …], …] - first outer, then holes."""
    parts = []
    for ring in rings:
        pts = [p if len(p) == 3 else (p[0], p[1], 0.0) for p in ring]
        if len(pts) >= 3:
            parts.append(ring_wkt(pts))
    return "POLYGON Z (" + ", ".join(parts) + ")" if parts else None


def multipolygon_wkt(polygons):
    """polygons: [[outer ring, hole, …], …]."""
    bodies = []
    for rings in polygons:
        wkt = polygon_wkt(rings)
        if wkt:
            bodies.append(wkt[len("POLYGON Z "):])
    return "MULTIPOLYGON Z (" + ", ".join(bodies) + ")" if bodies else None


def read_text(path, encodings=("utf-8-sig", "cp1252")):
    with open(path, "rb") as f:
        data = f.read()
    for enc in encodings:
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode(encodings[-1], errors="replace")
