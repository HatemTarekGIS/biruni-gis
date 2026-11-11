"""Cadastral XML extracts:

* Russia - Rosreestr (EGRN extracts about a land plot, cadastral plans of a block - KPT). The schemas
  change every few years (old CamelCase SpatialElement/Ordinate X=…, new snake_case
  spatial_element/ordinate/x), so the reader looks for the ideas, not one schema: rings of ordinates,
  grouped into contours, grouped under a cadastral number. Multi-contour plots become multipolygons.
* Ukraine - the cadastral exchange file (IN4XML, UkrainianCadastralExchangeFile): points (UIDP) →
  polylines (ULID) → parcel boundaries, with the cadastral number built from KOATUU, zone, quarter and
  parcel id.

Both use the geodetic order X = northing, Y = easting; geometry is written as easting, northing. The
coordinate system is a local МСК / СК-63 / УСК-2000 zone named in the file (sk_id / CoordinateSystem),
which is reported so it can be chosen."""
from .common import Dataset, FormatError, multipolygon_wkt, point_wkt

ID = "cadastral_xml"
LABEL = "Cadastral XML (Rosreestr / Ukraine IN4)"
EXTENSIONS = (".xml", ".in4")
COUNTRIES = ("RU", "UA")


def _local(tag):
    return tag.rsplit("}", 1)[-1] if isinstance(tag, str) else ""


def _parse(path):
    from lxml import etree

    parser = etree.XMLParser(resolve_entities=False, no_network=True, huge_tree=False, recover=False)
    try:
        return etree.parse(path, parser).getroot()
    except etree.XMLSyntaxError as e:
        raise FormatError(f"The XML file is damaged ({e}).") from e


def _num(v):
    try:
        return float(str(v).strip().replace(",", "."))
    except (TypeError, ValueError):
        return None


def _child_text(el, *names):
    for c in el:
        if _local(c.tag) in names and (c.text or "").strip():
            return c.text.strip()
    return None


# ---- Russia --------------------------------------------------------------------------------------------
_RING = {"spatial_element", "SpatialElement", "spatialelement"}
_CONTOUR = {"contour", "Contour", "entity_spatial", "EntitySpatial"}
_CADNUM = ("cad_number", "CadastralNumber", "cadastral_number", "CadNumber")


def _ordinate_xy(el):
    x = el.get("X") or el.get("x") or _child_text(el, "x", "X")
    y = el.get("Y") or el.get("y") or _child_text(el, "y", "Y")
    return _num(x), _num(y)


def _cadnum(el):
    """The cadastral number of the record holding el: the nearest ancestor that carries one, as an
    attribute (old schemas) or anywhere inside it (new schemas keep it in object/common_data)."""
    while el is not None:
        for name in _CADNUM:
            if el.get(name):
                return el.get(name)
        for d in el.iter():
            if _local(d.tag) in _CADNUM and (d.text or "").strip():
                return d.text.strip()
        el = el.getparent()
    return ""


def _ancestor(el, names):
    el = el.getparent()
    while el is not None and _local(el.tag) not in names:
        el = el.getparent()
    return el


def read_rosreestr(root):
    ds = Dataset(ID)
    parcels = {}  # cadnum -> {contour element id: [rings]}
    order = []
    for ring_el in root.iter():
        if _local(ring_el.tag) not in _RING:
            continue
        pts = []
        for o in ring_el.iter():
            if _local(o.tag) in ("ordinate", "Ordinate"):
                x, y = _ordinate_xy(o)
                if x is not None and y is not None:
                    pts.append((y, x, 0.0))  # X north, Y east
        dedup = [p for i, p in enumerate(pts) if i == 0 or p != pts[i - 1]]
        if len(dedup) < 3:
            continue
        contour = _ancestor(ring_el, _CONTOUR)
        cad = _cadnum(ring_el)
        if cad not in parcels:
            parcels[cad] = {}
            order.append(cad)
        parcels[cad].setdefault(id(contour), []).append(dedup)
    lyr = ds.layer("parcels", "polygon")
    for cad in order:
        polys = list(parcels[cad].values())
        wkt = multipolygon_wkt(polys)
        if wkt:
            lyr.add(wkt, cad_number=cad, contours=len(polys))
    sk = next((el.text.strip() for el in root.iter() if _local(el.tag) in ("sk_id", "CoordSystem", "EntSys")
               and (el.text or "").strip()), None)
    if sk is None:
        sk = next((el.get("Name") for el in root.iter() if _local(el.tag) == "CoordSystem" and el.get("Name")), None)
    if sk:
        ds.notes.append(f"Coordinate system named in the file: {sk} - choose its МСК zone.")
    if not len(lyr):
        raise FormatError("No parcel outlines were found in this Rosreestr XML (an extract without "
                          "coordinates, or an unknown schema).")
    return ds


# ---- Ukraine --------------------------------------------------------------------------------------------
def _find(el, *path):
    for name in path:
        el = next((c for c in el if _local(c.tag) == name), None) if el is not None else None
    return el


def _chain(lines):
    """Join polylines (lists of point ids) into one ring, flipping them where needed."""
    lines = [list(l) for l in lines if len(l) >= 2]
    if not lines:
        return []
    ring = lines.pop(0)
    while lines:
        for i, l in enumerate(lines):
            if l[0] == ring[-1]:
                ring += l[1:]
            elif l[-1] == ring[-1]:
                ring += list(reversed(l))[1:]
            else:
                continue
            lines.pop(i)
            break
        else:
            ring += lines.pop(0)  # not connected: keep going, the ring is reported as is
    return ring


def read_ukraine(root):
    ds = Dataset(ID)
    points = {}
    for p in root.iter():
        if _local(p.tag) == "Point":
            uid = _child_text(p, "UIDP")
            x, y = _num(_child_text(p, "X")), _num(_child_text(p, "Y"))
            if uid and x is not None and y is not None:
                points[uid] = (y, x, _num(_child_text(p, "H")) or 0.0)
    polylines = {}
    for pl in root.iter():
        if _local(pl.tag) == "PL":
            uid = _child_text(pl, "ULID")
            pts = _find(pl, "Points")
            if uid and pts is not None:
                polylines[uid] = [c.text.strip() for c in pts if _local(c.tag) == "P" and c.text]
    koatuu = next((el.text.strip() for el in root.iter() if _local(el.tag) == "KOATUU" and el.text), "")
    lyr = ds.layer("parcels", "polygon")
    for parcel in root.iter():
        if _local(parcel.tag) != "ParcelInfo":
            continue
        metric = _find(parcel, "ParcelMetricInfo")
        metric = parcel if metric is None else metric
        pid = _child_text(metric, "ParcelID") or ""
        area_el = _find(metric, "Area")
        area = _num(_child_text(metric if area_el is None else area_el, "Size"))
        zone = quarter = ""
        anc = parcel.getparent()
        while anc is not None:
            zone = zone or (_child_text(anc, "CadastralZoneNumber") or "")
            quarter = quarter or (_child_text(anc, "CadastralQuarterNumber") or "")
            anc = anc.getparent()
        rings = []
        for side in ("Externals", "Internals"):
            for boundary in (b for b in metric.iter() if _local(b.tag) == "Boundary"
                             and _ancestor(b, {side}) is not None):
                ids = [_child_text(l, "ULID") for l in boundary.iter() if _local(l.tag) == "Line"]
                ring_ids = _chain([polylines.get(i, []) for i in ids if i])
                ring = [points[i] for i in ring_ids if i in points]
                if len(ring) >= 3:
                    rings.append(ring)
        if not rings:
            continue
        cad = ":".join(v for v in (koatuu, zone, quarter, pid) if v)
        lyr.add(multipolygon_wkt([rings]), cad_number=cad, parcel_id=pid, area_doc=area)
    pts = ds.layer("boundary_points", "point")
    for uid, (x, y, h) in points.items():
        pts.add(point_wkt(x, y, h), uidp=uid)
    cs = next((el.text.strip() for el in root.iter() if _local(el.tag) == "CoordinateSystem"
               and (el.text or "").strip()), None)
    if cs is None:
        cs_el = next((el for el in root.iter() if _local(el.tag) == "CoordinateSystem"), None)
        cs = _local(cs_el[0].tag) if cs_el is not None and len(cs_el) else None
    if cs:
        ds.notes.append(f"Coordinate system in the file: {cs} (choose the matching УСК-2000 / СК-63 zone).")
    if not len(lyr) and not len(pts):
        raise FormatError("No parcels or points were found in this exchange file.")
    return ds


def detect(path):
    try:
        with open(path, "rb") as f:
            head = f.read(4096).decode("utf-8", errors="ignore")
    except OSError:
        return None
    if "UkrainianCadastralExchangeFile" in head or "InfoPart" in head:
        return "ua"
    low = head.lower()
    if any(k in low for k in ("extract_", "kpt", "rosreestr", "cadastral", "entity_spatial", "region_cadastr")):
        return "ru"
    return None


def read(path):
    root = _parse(path)
    tag = _local(root.tag)
    if tag == "UkrainianCadastralExchangeFile" or any(_local(c.tag) == "InfoPart" for c in root):
        return read_ukraine(root)
    return read_rosreestr(root)
