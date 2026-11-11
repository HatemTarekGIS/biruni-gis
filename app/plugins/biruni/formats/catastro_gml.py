"""Spain - INSPIRE cadastral parcel GML for the Catastro "Informe de Validación Gráfica" (IVG) and
notary/registry submissions (Ley 13/2015).

Writes the FeatureCollection / cp:CadastralParcel layout the Sede Electrónica del Catastro accepts:
one cp:CadastralParcel per parcel with areaValue (m²), a gml:MultiSurface of PolygonPatches (exterior
ring anti-clockwise, holes clockwise), localId / namespace and an optional national cadastral reference.
Coordinates must be ETRS89 UTM (EPSG 25828-25831) - or REGCAN95 UTM 28 (EPSG 4083) in the Canaries -
with two decimals. validate() re-reads a file and checks that structure."""
import datetime
import math
from xml.sax.saxutils import escape

from .common import FormatError

ID = "catastro_gml"
LABEL = "Catastro parcel GML (Spain, INSPIRE)"
COUNTRIES = ("ES",)
ALLOWED_EPSG = {25828, 25829, 25830, 25831, 4083}

NS = {"wfs": "http://www.opengis.net/wfs/2.0", "gml": "http://www.opengis.net/gml/3.2",
      "cp": "http://inspire.ec.europa.eu/schemas/cp/4.0", "base": "http://inspire.ec.europa.eu/schemas/base/3.3"}


def _signed_area(ring):
    return sum(ring[i][0] * ring[(i + 1) % len(ring)][1] - ring[(i + 1) % len(ring)][0] * ring[i][1]
               for i in range(len(ring))) / 2.0


def _orient(ring, ccw):
    pts = list(ring)
    if pts[0] == pts[-1]:
        pts = pts[:-1]
    if (_signed_area(pts) > 0) != ccw:
        pts.reverse()
    return pts + pts[:1]


def _poslist(ring):
    return " ".join(f"{x:.2f} {y:.2f}" for x, y in ring)


def write(path, parcels, epsg, namespace="ES.LOCAL.CP"):
    """parcels: [{"id": localId, "polygons": [[outer, hole, …], …], "reference": "1234567AB1234C"
    (optional), "area": m² (optional - computed when missing)}]."""
    epsg = int(epsg)
    if epsg not in ALLOWED_EPSG:
        raise FormatError(f"Catastro accepts ETRS89 UTM 28-31 (EPSG 25828-25831) or REGCAN95 (4083), not "
                          f"EPSG {epsg}: reproject the parcels first.")
    srs = f"http://www.opengis.net/def/crs/EPSG/0/{epsg}"
    stamp = datetime.datetime.now().replace(microsecond=0).isoformat()
    members = []
    for p in parcels:
        pid = escape(str(p["id"]))
        surfaces, area = [], 0.0
        for k, rings in enumerate(p["polygons"], 1):
            rings = [[(float(x), float(y)) for x, y, *_ in r] for r in rings if len(r) >= 3]
            if not rings:
                continue
            outer = _orient(rings[0], ccw=True)
            holes = [_orient(h, ccw=False) for h in rings[1:]]
            area += abs(_signed_area(outer[:-1])) - sum(abs(_signed_area(h[:-1])) for h in holes)
            interiors = "".join(
                f"<gml:interior><gml:LinearRing><gml:posList srsDimension=\"2\" count=\"{len(h)}\">"
                f"{_poslist(h)}</gml:posList></gml:LinearRing></gml:interior>" for h in holes)
            surfaces.append(
                f"<gml:surfaceMember><gml:Surface gml:id=\"Surface_{namespace}.{pid}.{k}\" srsName=\"{srs}\">"
                f"<gml:patches><gml:PolygonPatch><gml:exterior><gml:LinearRing>"
                f"<gml:posList srsDimension=\"2\" count=\"{len(outer)}\">{_poslist(outer)}</gml:posList>"
                f"</gml:LinearRing></gml:exterior>{interiors}</gml:PolygonPatch></gml:patches></gml:Surface>"
                f"</gml:surfaceMember>")
        if not surfaces:
            raise FormatError(f"Parcel {p['id']} has no polygon.")
        value = p.get("area") or area
        ref = escape(str(p.get("reference") or ""))
        label = escape(str(p.get("label") or p["id"]))
        members.append(
            f"<member><cp:CadastralParcel gml:id=\"{namespace}.{pid}\">"
            f"<cp:areaValue uom=\"m2\">{int(round(value))}</cp:areaValue>"
            f"<cp:beginLifespanVersion xsi:nil=\"true\" nilReason=\"other:unpopulated\"></cp:beginLifespanVersion>"
            f"<cp:endLifespanVersion xsi:nil=\"true\" nilReason=\"other:unpopulated\"></cp:endLifespanVersion>"
            f"<cp:geometry><gml:MultiSurface gml:id=\"MultiSurface_{namespace}.{pid}\" srsName=\"{srs}\">"
            + "".join(surfaces) +
            f"</gml:MultiSurface></cp:geometry>"
            f"<cp:inspireId><Identifier xmlns=\"{NS['base']}\"><localId>{pid}</localId>"
            f"<namespace>{escape(namespace)}</namespace></Identifier></cp:inspireId>"
            f"<cp:label>{label}</cp:label><cp:nationalCadastralReference>{ref}</cp:nationalCadastralReference>"
            f"</cp:CadastralParcel></member>")
    doc = (
        "<?xml version=\"1.0\" encoding=\"utf-8\"?>\n"
        f"<FeatureCollection xmlns:xsi=\"http://www.w3.org/2001/XMLSchema-instance\" xmlns:gml=\"{NS['gml']}\" "
        f"xmlns:xlink=\"http://www.w3.org/1999/xlink\" xmlns:cp=\"{NS['cp']}\" "
        f"xmlns:gmd=\"http://www.isotc211.org/2005/gmd\" "
        f"xsi:schemaLocation=\"{NS['wfs']} http://schemas.opengis.net/wfs/2.0/wfs.xsd "
        f"{NS['cp']} http://inspire.ec.europa.eu/schemas/cp/4.0/CadastralParcels.xsd\" "
        f"xmlns=\"{NS['wfs']}\" timeStamp=\"{stamp}\" numberMatched=\"{len(members)}\" "
        f"numberReturned=\"{len(members)}\">\n" + "\n".join(members) + "\n</FeatureCollection>\n")
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(doc)
    return path


def validate(path):
    """Problems found in a parcel GML (empty = looks right)."""
    from lxml import etree

    problems = []
    try:
        root = etree.parse(path, etree.XMLParser(resolve_entities=False, no_network=True)).getroot()
    except etree.XMLSyntaxError as e:
        return [f"Not well-formed XML: {e}"]
    parcels = root.findall(f".//{{{NS['cp']}}}CadastralParcel")
    if not parcels:
        problems.append("No cp:CadastralParcel found.")
    for cp in parcels:
        pid = cp.get(f"{{{NS['gml']}}}id")
        if cp.find(f"{{{NS['cp']}}}areaValue") is None:
            problems.append(f"{pid}: areaValue missing.")
        ms = cp.find(f".//{{{NS['gml']}}}MultiSurface")
        srs = ms.get("srsName", "") if ms is not None else ""
        try:
            epsg = int(srs.rsplit("/", 1)[-1])
        except ValueError:
            epsg = None
        if epsg not in ALLOWED_EPSG:
            problems.append(f"{pid}: coordinate system {srs or '(none)'} is not accepted by Catastro.")
        for tag, ccw in (("exterior", True), ("interior", False)):
            for ring in cp.iter(f"{{{NS['gml']}}}{tag}"):
                pl = ring.find(f".//{{{NS['gml']}}}posList")
                vals = [float(v) for v in (pl.text or "").split()]
                pts = list(zip(vals[0::2], vals[1::2]))
                if len(pts) < 4 or pts[0] != pts[-1]:
                    problems.append(f"{pid}: a ring is not closed or has fewer than 3 corners.")
                    continue
                if int(pl.get("count", len(pts))) != len(pts):
                    problems.append(f"{pid}: posList count does not match the coordinates.")
                if (_signed_area(pts[:-1]) > 0) != ccw:
                    problems.append(f"{pid}: {tag} ring has the wrong orientation.")
                if any(not math.isfinite(v) for v in vals):
                    problems.append(f"{pid}: invalid coordinate values.")
        if cp.find(f".//{{{NS['base']}}}localId") is None:
            problems.append(f"{pid}: inspireId/localId missing.")
    return problems
