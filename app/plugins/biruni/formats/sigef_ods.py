"""Brazil - SIGEF (INCRA) parcel certification spreadsheet, written as ODS.

Surveyors certifying rural properties fill INCRA's ODS spreadsheet: an "identificacao" sheet and one
"perimetro_N" sheet per perimeter listing every vertex in order: vertex code (accreditation code -
type M/P/V - number, e.g. ABCD-M-0001), longitude / latitude in SIRGAS 2000 (degrees minutes seconds
with W/S), their precisions (sigma), height, positioning method, boundary type (LA1… artificial,
LN1… natural) and the neighbour on the side that starts at the vertex.

This writer produces that layout from a parcel polygon and checks it first (closed, valid, codes,
precisions within INCRA limits: 0.50 m artificial, 3.00 m natural, 7.50 m inaccessible). INCRA revises
the template: compare the header with the current official spreadsheet before uploading."""
import math
import re

from osgeo import ogr

from .common import FormatError

ID = "sigef_ods"
LABEL = "SIGEF certification spreadsheet (Brazil, ODS)"
COUNTRIES = ("BR",)

CODE_RE = re.compile(r"^[A-Z0-9]{3,4}-[MPV]-\d{1,6}$")
LIMITS = {"artificial": 0.50, "natural": 3.00, "inaccessible": 7.50}
VERTEX_COLUMNS = ["codigo", "longitude", "sigma_long", "latitude", "sigma_lat", "altitude", "sigma_alt",
                  "metodo_posicionamento", "tipo_limite", "cns", "matricula", "descricao_limite"]


def dms(value, positive, negative):
    """-47.875 → '47 52 30,000 W' (SIGEF style: degrees minutes seconds with decimal comma)."""
    hemi = positive if value >= 0 else negative
    v = abs(value)
    d = int(v)
    m = int((v - d) * 60)
    s = (v - d - m / 60) * 3600
    if s >= 59.9995:
        s, m = 0.0, m + 1
    if m == 60:
        m, d = 0, d + 1
    return f"{d} {m:02d} {s:06.3f} {hemi}".replace(".", ",")


def _limit_class(code):
    c = (code or "").upper()
    if c.startswith("LN"):
        return "natural"
    if c.startswith("LI"):
        return "inaccessible"
    return "artificial"


def check(vertices):
    """Problems as text; empty = ready. vertices: dicts with code, lon, lat, sigma_x, sigma_y, limit."""
    problems = []
    if len(vertices) < 3:
        problems.append("A perimeter needs at least 3 vertices.")
    seen = set()
    for i, v in enumerate(vertices, 1):
        code = v.get("code", "")
        if not CODE_RE.match(code or ""):
            problems.append(f"Vertex {i}: code «{code}» is not in the form ABCD-M-0001.")
        if code in seen:
            problems.append(f"Vertex {i}: code {code} is used twice.")
        seen.add(code)
        lon, lat = v.get("lon"), v.get("lat")
        if lon is None or lat is None or not (-75 <= lon <= -28 and -35 <= lat <= 6):
            problems.append(f"Vertex {i}: coordinates are not SIRGAS 2000 degrees inside Brazil.")
        limit = LIMITS[_limit_class(v.get("limit"))]
        for key in ("sigma_x", "sigma_y"):
            s = v.get(key)
            if s is not None and s > limit:
                problems.append(f"Vertex {i}: precision {s:.2f} m is above the {limit:.2f} m allowed for "
                                f"this boundary type.")
    if len(vertices) >= 3:
        ring = ogr.Geometry(ogr.wkbLinearRing)
        for v in vertices + vertices[:1]:
            ring.AddPoint_2D(v["lon"], v["lat"])
        poly = ogr.Geometry(ogr.wkbPolygon)
        poly.AddGeometry(ring)
        if not poly.IsValid():
            problems.append("The perimeter crosses itself (vertices out of order).")
    return problems


def vertices_from_ring(ring_lonlat, prefix="ABCD", kind="M", start=1, sigma=0.05, method="PG1",
                       limit="LA1", height=None):
    """Vertex dicts from a SIRGAS 2000 lon/lat ring (closing vertex dropped), numbered from start,
    clockwise from the most northern vertex as INCRA asks."""
    pts = list(ring_lonlat)
    if len(pts) > 1 and pts[0] == pts[-1]:
        pts = pts[:-1]
    area2 = sum(pts[i][0] * pts[(i + 1) % len(pts)][1] - pts[(i + 1) % len(pts)][0] * pts[i][1]
                for i in range(len(pts)))
    if area2 > 0:  # counter-clockwise → clockwise
        pts.reverse()
    k = max(range(len(pts)), key=lambda i: pts[i][1])
    pts = pts[k:] + pts[:k]
    return [{"code": f"{prefix}-{kind}-{start + i:04d}", "lon": x, "lat": y, "sigma_x": sigma,
             "sigma_y": sigma, "height": height, "sigma_z": None if height is None else sigma,
             "method": method, "limit": limit, "neighbour": ""} for i, (x, y) in enumerate(pts)]


def write(path, perimeters, identification=None):
    """perimeters: [[vertex dicts], …] (one per perimeter), identification: {field: value}.
    Raises FormatError listing the problems when a check fails."""
    problems = []
    for n, verts in enumerate(perimeters, 1):
        problems += [f"Perimeter {n}: {p}" for p in check(verts)]
    if problems:
        raise FormatError("\n".join(problems))
    drv = ogr.GetDriverByName("ODS")
    ds = drv.CreateDataSource(path)
    if ds is None:
        raise FormatError(f"Could not write {path}")
    try:
        ident = ds.CreateLayer("identificacao", None, ogr.wkbNone)
        ident.CreateField(ogr.FieldDefn("campo", ogr.OFTString))
        ident.CreateField(ogr.FieldDefn("valor", ogr.OFTString))
        for k, v in (identification or {}).items():
            f = ogr.Feature(ident.GetLayerDefn())
            f.SetField("campo", str(k))
            f.SetField("valor", "" if v is None else str(v))
            ident.CreateFeature(f)
        for n, verts in enumerate(perimeters, 1):
            lyr = ds.CreateLayer(f"perimetro_{n}", None, ogr.wkbNone)
            for col in VERTEX_COLUMNS:
                lyr.CreateField(ogr.FieldDefn(col, ogr.OFTString))
            for v in verts:
                f = ogr.Feature(lyr.GetLayerDefn())
                vals = {"codigo": v["code"], "longitude": dms(v["lon"], "E", "W"),
                        "sigma_long": f"{v.get('sigma_x', 0) or 0:.2f}".replace(".", ","),
                        "latitude": dms(v["lat"], "N", "S"),
                        "sigma_lat": f"{v.get('sigma_y', 0) or 0:.2f}".replace(".", ","),
                        "altitude": "" if v.get("height") is None else f"{v['height']:.2f}".replace(".", ","),
                        "sigma_alt": "" if v.get("sigma_z") is None else f"{v['sigma_z']:.2f}".replace(".", ","),
                        "metodo_posicionamento": v.get("method", ""), "tipo_limite": v.get("limit", ""),
                        "cns": v.get("cns", ""), "matricula": v.get("matricula", ""),
                        "descricao_limite": v.get("neighbour", "")}
                for k, val in vals.items():
                    f.SetField(k, val)
                lyr.CreateFeature(f)
    finally:
        ds = None  # noqa: F841
    return path


def ring_area_m2(verts):
    """Approximate area (m²) of a lon/lat ring - for the summary only."""
    if len(verts) < 3:
        return 0.0
    lat0 = math.radians(sum(v["lat"] for v in verts) / len(verts))
    k = 111320.0
    pts = [(v["lon"] * k * math.cos(lat0), v["lat"] * 110574.0) for v in verts]
    return abs(sum(pts[i][0] * pts[i - 1][1] - pts[i - 1][0] * pts[i][1] for i in range(len(pts)))) / 2
