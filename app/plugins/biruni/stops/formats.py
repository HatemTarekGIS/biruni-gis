"""Stops in and out of files:

* ``.stops``  - Biruni's own (JSON): everything, pictures included.
* ``.xml``    - QGIS spatial bookmarks (QGIS's own export/import, both directions).
* ``.bkmx``   - ArcGIS Pro bookmarks (JSON "CIMBookmark" objects: a camera x/y/scale/heading in a spatial reference,
                optionally a location extent). Read and written; a camera ``heading`` is the compass direction the
                top of the map faces, i.e. minus the QGIS map rotation.
* ``.kml`` / ``.kmz`` - Google Earth placemarks, one per stop, with a LookAt camera (range from the frame size,
                heading from the rotation); a KMZ also carries the pictures.

Import functions return plain dicts that StopsStore.add() takes (see to_store)."""
import json
import math
import os
import xml.etree.ElementTree as ET
import zipfile
from xml.sax.saxutils import escape

from qgis.core import QgsBookmarkManager, QgsCoordinateReferenceSystem, QgsProject, QgsRectangle

from .model import Frame, thumb_b64, thumb_bytes

WGS84 = "EPSG:4326"
KML_NS = "http://www.opengis.net/kml/2.2"
FOV_HALF = math.radians(30.0)  # Google Earth's vertical field of view is ~60°
FILTERS = {
    "stops": "Biruni stops (*.stops)",
    "xml": "Spatial bookmarks (*.xml)",
    "bkmx": "ArcGIS Pro bookmarks (*.bkmx)",
    "kmz": "Google Earth KMZ (*.kmz)",
    "kml": "Google Earth KML (*.kml)",
}


def kind_of(path):
    ext = os.path.splitext(path)[1].lower().lstrip(".")
    return ext if ext in FILTERS else None


def _item(stop):
    return {"id": stop.id, "name": stop.name, "group": stop.group, "note": stop.note, "color": stop.color,
            "scale": stop.scale, "view": stop.frame.to_dict(), "time": list(stop.time) if stop.time else None,
            "layers": stop.layers, "thumb": thumb_b64(stop.thumb), "created": stop.created}


# ---- Biruni .stops ---------------------------------------------------------------------------------------------
def write_stops(path, stops):
    data = {"format": "biruni-stops", "version": 1, "stops": [_item(s) for s in stops]}
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    return len(stops)


def read_stops(path):
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict) or data.get("format") != "biruni-stops":
        raise ValueError("not a Biruni stops file")
    out = []
    for d in data.get("stops") or []:
        try:
            frame = Frame.from_dict(d["view"])
        except (KeyError, TypeError, ValueError):
            continue
        out.append({"name": d.get("name") or "", "group": d.get("group") or "", "note": d.get("note") or "",
                    "color": d.get("color") or "", "scale": float(d.get("scale") or 0), "frame": frame,
                    "time": tuple(d["time"]) if d.get("time") else None, "layers": d.get("layers") or None,
                    "thumb": thumb_bytes(d.get("thumb") or ""), "sid": d.get("id")})
    return out


# ---- QGIS bookmarks XML ----------------------------------------------------------------------------------------
def write_qgis_xml(path, stops):
    from qgis.core import QgsBookmark, QgsReferencedRectangle

    tmp = QgsBookmarkManager()
    for s in stops:
        b = QgsBookmark()
        b.setId(s.id)
        b.setName(s.name)
        b.setGroup(s.group)
        b.setExtent(QgsReferencedRectangle(s.frame.bbox(), s.frame.crs))
        b.setRotation(s.frame.rot)
        tmp.addBookmark(b)
    if not QgsBookmarkManager.exportToFile(path, [tmp]):
        raise OSError(path)
    return len(stops)


def read_qgis_xml(path):
    tmp = QgsBookmarkManager()
    if not tmp.importFromFile(path):
        raise ValueError("not a QGIS bookmarks file")
    out = []
    for b in tmp.bookmarks():
        e = b.extent()
        out.append({"name": b.name(), "group": b.group(), "frame": Frame.from_rect(e, b.rotation(), e.crs()),
                    "sid": b.id()})
    return out


# ---- ArcGIS Pro .bkmx ------------------------------------------------------------------------------------------
def _sr(crs):
    a = crs.authid()
    if a.upper().startswith("EPSG:"):
        code = int(a.split(":")[1])
        wkid = 102100 if code == 3857 else code
        return {"wkid": wkid, "latestWkid": code}
    return {"wkt": crs.toWkt()}


def _crs_from_sr(sr):
    if not isinstance(sr, dict):
        return QgsCoordinateReferenceSystem()
    for key in ("latestWkid", "wkid"):
        code = sr.get(key)
        if code:
            code = int(code)
            if code in (102100, 102113, 900913):
                code = 3857
            for auth in ("EPSG", "ESRI"):
                crs = QgsCoordinateReferenceSystem(f"{auth}:{code}")
                if crs.isValid():
                    return crs
    if sr.get("wkt"):
        crs = QgsCoordinateReferenceSystem.fromWkt(sr["wkt"])
        if crs.isValid():
            return crs
    return QgsCoordinateReferenceSystem()


def write_bkmx(path, stops):
    out = []
    for s in stops:
        f = s.frame
        sr = _sr(f.crs)
        r = f.bbox()
        bm = {"type": "CIMBookmark",
              "camera": {"type": "CIMViewCamera", "pitch": -90, "scale": round(s.scale or 0.0, 6),
                         "x": f.cx, "y": f.cy, "z": "NaN", "heading": round((-f.rot) % 360.0, 6),
                         "spatialReference": sr},
              "name": s.name,
              "location": {"xmin": r.xMinimum(), "ymin": r.yMinimum(), "xmax": r.xMaximum(), "ymax": r.yMaximum(),
                           "spatialReference": sr}}
        if s.note:
            bm["description"] = s.note
        if s.thumb:
            bm["thumbnail"] = "data:image/jpeg;base64," + thumb_b64(s.thumb)
        out.append(bm)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump({"bookmarks": out}, fh, ensure_ascii=False, indent=2)
    return len(stops)


def read_bkmx(path):
    with open(path, "rb") as fh:
        raw = fh.read()
    text = raw.decode("utf-8-sig", errors="replace")
    data = json.loads(text)
    items = data.get("bookmarks") if isinstance(data, dict) else data
    if items is None and isinstance(data, dict) and data.get("type") == "CIMBookmark":
        items = [data]
    if not isinstance(items, list):
        raise ValueError("not an ArcGIS Pro bookmarks file")
    top_sr = data.get("spatialReference") if isinstance(data, dict) else None
    out = []
    for bm in items:
        if not isinstance(bm, dict):
            continue
        cam = bm.get("camera") or {}
        loc = bm.get("location") or bm.get("extent") or {}
        crs = _crs_from_sr(cam.get("spatialReference") or loc.get("spatialReference") or top_sr)
        rot = -float(cam.get("heading") or 0.0)
        rot = (rot + 180.0) % 360.0 - 180.0
        frame = None
        if all(k in loc for k in ("xmin", "ymin", "xmax", "ymax")):
            r = QgsRectangle(float(loc["xmin"]), float(loc["ymin"]), float(loc["xmax"]), float(loc["ymax"]))
            frame = Frame.from_rect(r, 0.0, crs)
        x, y = cam.get("x"), cam.get("y")
        scale = _num(cam.get("scale"))
        if _num(x) is not None and _num(y) is not None:
            if frame is None:
                # no extent: the camera's scale on a 1000×600 px map at 96 dpi
                if crs.isValid() and crs.isGeographic():
                    upm = 1 / 111320.0
                else:
                    upm = 1.0
                mupp = (scale or 5000.0) * 0.0254 / 96 * upm
                frame = Frame(float(x), float(y), 1000 * mupp, 600 * mupp, rot, crs)
            else:
                frame = Frame(float(x), float(y), frame.w, frame.h, rot, crs)
                if rot:  # the location is the bounding box of the turned view
                    frame = _unturn(frame, frame.w, frame.h)
        if frame is None:
            continue
        thumb = b""
        t = bm.get("thumbnail")
        if isinstance(t, str) and "base64," in t:
            thumb = thumb_bytes(t.split("base64,", 1)[1])
        out.append({"name": bm.get("name") or "", "note": bm.get("description") or "", "frame": frame,
                    "scale": scale or 0.0, "thumb": thumb})
    return out


def _num(v):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def _unturn(frame, bw, bh):
    """The frame (w, h) whose turned rectangle has this bounding box (bw, bh), for a turn that is not 45°."""
    a = math.radians(frame.rot)
    c, s = abs(math.cos(a)), abs(math.sin(a))
    det = c * c - s * s
    if abs(det) < 0.2:
        return Frame(frame.cx, frame.cy, bw * 0.7, bh * 0.7, frame.rot, frame.crs)
    w = (bw * c - bh * s) / det
    h = (bh * c - bw * s) / det
    if w <= 0 or h <= 0:
        return Frame(frame.cx, frame.cy, bw, bh, frame.rot, frame.crs)
    return Frame(frame.cx, frame.cy, w, h, frame.rot, frame.crs)


# ---- KML / KMZ -------------------------------------------------------------------------------------------------
def _to_wgs(frame, project=None):
    dest = QgsCoordinateReferenceSystem(WGS84)
    if frame.crs.isValid() and frame.crs != dest:
        return frame.transformed(dest, project or QgsProject.instance())
    return frame


def _metres(frame_wgs):
    lat = math.radians(frame_wgs.cy)
    return frame_wgs.w * 111320.0 * max(0.05, math.cos(lat)), frame_wgs.h * 110574.0


def _kml_text(stops, pictures=False, project=None):
    parts = ['<?xml version="1.0" encoding="UTF-8"?>',
             f'<kml xmlns="{KML_NS}"><Document><name>Biruni stops</name>']
    groups = {}
    for s in stops:
        groups.setdefault(s.group, []).append(s)
    for group, members in groups.items():
        if group:
            parts.append(f"<Folder><name>{escape(group)}</name>")
        for s in members:
            try:
                f = _to_wgs(s.frame, project)
            except ValueError:
                continue
            wm, hm = _metres(f)
            rng = max(hm, wm * 0.6) / 2 / math.tan(FOV_HALF)
            heading = (-f.rot) % 360.0
            desc = escape(s.note or "")
            if pictures and s.thumb:
                desc += escape(f'<br/><img src="files/{_pic_name(s)}" width="256"/>')
            parts.append(
                f"<Placemark><name>{escape(s.name)}</name>"
                + (f"<description>{desc}</description>" if desc else "")
                + f"<LookAt><longitude>{f.cx:.8f}</longitude><latitude>{f.cy:.8f}</latitude><altitude>0</altitude>"
                f"<heading>{heading:.4f}</heading><tilt>0</tilt><range>{rng:.2f}</range>"
                f"<altitudeMode>relativeToGround</altitudeMode></LookAt>"
                f"<Point><coordinates>{f.cx:.8f},{f.cy:.8f},0</coordinates></Point></Placemark>")
        if group:
            parts.append("</Folder>")
    parts.append("</Document></kml>")
    return "\n".join(parts)


def _pic_name(stop):
    return "stop_" + "".join(ch for ch in stop.id if ch.isalnum())[:32] + ".jpg"


def write_kml(path, stops, project=None):
    with open(path, "w", encoding="utf-8") as f:
        f.write(_kml_text(stops, False, project))
    return len(stops)


def write_kmz(path, stops, project=None):
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("doc.kml", _kml_text(stops, True, project))
        for s in stops:
            if s.thumb:
                z.writestr("files/" + _pic_name(s), s.thumb)
    return len(stops)


def _local(tag):
    return tag.rsplit("}", 1)[-1]


def _find(el, name):
    for child in el.iter():
        if _local(child.tag) == name:
            return child
    return None


def _ftext(el, name):
    c = _find(el, name) if el is not None else None
    return _num(c.text) if c is not None and c.text is not None else None


def read_kml(path):
    """Placemarks with a LookAt or Camera (else a Point: a street-level view); folders become groups."""
    pics = {}
    if path.lower().endswith(".kmz") or zipfile.is_zipfile(path):
        with zipfile.ZipFile(path) as z:
            names = z.namelist()
            kml_name = next((n for n in names if n.lower().endswith(".kml")), None)
            if kml_name is None:
                raise ValueError("no KML inside")
            text = z.read(kml_name)
            for n in names:
                if n.lower().endswith((".jpg", ".jpeg", ".png")):
                    pics[n] = z.read(n)
    else:
        with open(path, "rb") as f:
            text = f.read()
    root = ET.fromstring(text)
    wgs = QgsCoordinateReferenceSystem(WGS84)
    out = []

    def walk(el, group):
        for child in el:
            tag = _local(child.tag)
            if tag in ("Folder", "Document"):
                name_el = next((c for c in child if _local(c.tag) == "name"), None)
                sub = group
                if tag == "Folder" and name_el is not None and name_el.text:
                    sub = name_el.text.strip()
                walk(child, sub)
            elif tag == "Placemark":
                item = _placemark(child, group, wgs, pics)
                if item is not None:
                    out.append(item)

    walk(root, "")
    return out


def _placemark(pm, group, wgs, pics):
    name_el = next((c for c in pm if _local(c.tag) == "name"), None)
    desc_el = next((c for c in pm if _local(c.tag) == "description"), None)
    view = _find(pm, "LookAt")
    cam = _find(pm, "Camera") if view is None else None
    lon = lat = None
    rng = None
    heading = 0.0
    if view is not None:
        lon, lat, rng = _ftext(view, "longitude"), _ftext(view, "latitude"), _ftext(view, "range")
        heading = _ftext(view, "heading") or 0.0
    elif cam is not None:
        lon, lat = _ftext(cam, "longitude"), _ftext(cam, "latitude")
        rng = _ftext(cam, "altitude")
        heading = _ftext(cam, "heading") or 0.0
    if lon is None or lat is None:
        coords = _find(pm, "coordinates")
        if coords is not None and coords.text:
            first = coords.text.strip().split()[0].split(",")
            lon, lat = _num(first[0]), _num(first[1]) if len(first) > 1 else None
    if lon is None or lat is None:
        return None
    rng = rng if rng and rng > 0 else 800.0
    hm = 2 * rng * math.tan(FOV_HALF)
    wm = hm / 0.6
    w = wm / (111320.0 * max(0.05, math.cos(math.radians(lat))))
    h = hm / 110574.0
    rot = (-heading + 180.0) % 360.0 - 180.0
    note = ""
    thumb = b""
    if desc_el is not None and desc_el.text:
        note = desc_el.text
        for n, data in pics.items():
            if n in note:
                thumb = data
        if "<img" in note:
            note = note.split("<br/><img", 1)[0].split("<img", 1)[0]
    return {"name": (name_el.text or "").strip() if name_el is not None else "", "group": group,
            "note": note.strip(), "frame": Frame(lon, lat, w, h, rot, wgs), "thumb": thumb}


# ---- one door ----------------------------------------------------------------------------------------------------
def export(path, stops, project=None):
    """Write stops to a file (the type from the extension). A stop without a place (a QGIS bookmark whose extent
    was lost) is left out. Returns how many were written."""
    stops = [s for s in stops if s.frame.is_valid()]
    kind = kind_of(path)
    if kind == "stops":
        return write_stops(path, stops)
    if kind == "xml":
        return write_qgis_xml(path, stops)
    if kind == "bkmx":
        return write_bkmx(path, stops)
    if kind == "kmz":
        return write_kmz(path, stops, project)
    if kind == "kml":
        return write_kml(path, stops, project)
    raise ValueError("unknown file type")


def read(path):
    kind = kind_of(path)
    if kind == "stops":
        return read_stops(path)
    if kind == "xml":
        return read_qgis_xml(path)
    if kind == "bkmx":
        return read_bkmx(path)
    if kind in ("kml", "kmz"):
        return read_kml(path)
    raise ValueError("unknown file type")


def import_into(store, path, scope=None, group=None):
    """Read a file and add its stops; returns the new Stop objects. An id already used gets a new one."""
    added = []
    for d in read(path):
        name = d.get("name") or os.path.splitext(os.path.basename(path))[0]
        s = store.add(name, d["frame"], scope, group if group is not None else d.get("group", ""), d.get("note", ""),
                      d.get("color", ""), d.get("scale", 0.0), d.get("thumb", b""), d.get("layers"), None,
                      d.get("time"), sid=d.get("sid"))
        if s is not None:
            added.append(s)
    return added
