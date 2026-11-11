"""SIMA (Japan Survey Instruments Manufacturers' Association) coordinate exchange files - the way
Japanese survey offices pass points and parcels (画地) between CAD, total stations and land registry
software. Comma-separated records, Shift_JIS text:

* G00 header, Z00 section titles;
* A00 … A99 - points: A01,number,name,X,Y,H  (Japanese survey order: X = northing, Y = easting);
* D00 … D99 - one parcel: D00,parcel no,parcel name,…  then B01,seq,point number,point name per corner.

Read: points and parcels (corners joined through the point numbers). Write: the same from a point layer
and/or a polygon layer, so data goes back to Japanese software."""
import csv
import io

from .common import Dataset, FormatError, point_wkt, polygon_wkt

ID = "sima"
LABEL = "SIMA survey coordinates (Japan)"
EXTENSIONS = (".sim", ".sima")
COUNTRIES = ("JP",)


def _decode(data):
    for enc in ("cp932", "utf-8-sig"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("cp932", errors="replace")


def _f(v):
    try:
        return float(str(v).strip())
    except ValueError:
        return None


def read(path):
    with open(path, "rb") as f:
        rows = list(csv.reader(io.StringIO(_decode(f.read()))))
    if not any(r and r[0].strip().upper() in ("A01", "G00", "Z00") for r in rows):
        raise FormatError("This is not a SIMA file (no G00/Z00/A01 records).")
    ds = Dataset(ID)
    pts_layer = ds.layer("points", "point")
    parcels = ds.layer("parcels", "polygon")
    points, parcel, header = {}, None, ""
    for r in rows:
        if not r:
            continue
        code = r[0].strip().upper()
        if code == "G00" and len(r) > 2:
            header = r[2].strip()
        elif code == "A01" and len(r) >= 5:
            num, name = r[1].strip(), r[2].strip()
            x, y = _f(r[3]), _f(r[4])
            h = _f(r[5]) if len(r) > 5 else None
            if x is None or y is None:
                continue
            points[num] = (y, x, h or 0.0)  # SIMA X = northing → map y
            pts_layer.add(point_wkt(y, x, h or 0.0), number=num, name=name,
                          height=h if h is not None else None)
        elif code == "D00":
            parcel = {"no": r[1].strip() if len(r) > 1 else "", "name": r[2].strip() if len(r) > 2 else "",
                      "corners": []}
        elif code == "B01" and parcel is not None and len(r) > 2:
            parcel["corners"].append(r[2].strip())
        elif code == "D99" and parcel is not None:
            ring = [points[c] for c in parcel["corners"] if c in points]
            missing = [c for c in parcel["corners"] if c not in points]
            if missing:
                ds.notes.append(f"Parcel {parcel['no']}: corner point(s) {', '.join(missing)} not found.")
            wkt = polygon_wkt([ring]) if len(ring) >= 3 else None
            if wkt:
                parcels.add(wkt, parcel_no=parcel["no"], name=parcel["name"],
                            corners="-".join(parcel["corners"]))
            parcel = None
    if header:
        ds.notes.append(f"SIMA header: {header}")
    ds.notes.append("SIMA files carry no coordinate system: choose the Japan Plane Rectangular zone "
                    "(JGD2011 / JGD2024, zones I–XIX).")
    if not ds.count():
        raise FormatError("No points or parcels were found in this SIMA file.")
    return ds


def write(path, points=(), parcels=(), title="Biruni GIS"):
    """points: [(number, name, x_easting, y_northing, h)], parcels: [(parcel_no, name, [(x, y), …])].
    Parcel corners that are not among the points are added as points. Written as Shift_JIS."""
    out = io.StringIO()
    w = csv.writer(out, lineterminator="\r\n")
    w.writerow(["G00", "01", title, ""])
    w.writerow(["Z00", "座標ﾃﾞｰﾀ", ""])
    w.writerow(["A00", ""])
    numbered, by_xy, n = [], {}, 0
    for num, name, x, y, h in points:
        numbered.append((str(num), name or "", x, y, h))
        by_xy[(round(x, 3), round(y, 3))] = str(num)
        n = max(n, int(num) if str(num).isdigit() else n)
    plots = []
    for pno, pname, ring in parcels:
        corners = []
        ring = list(ring)
        if len(ring) > 1 and ring[0] == ring[-1]:
            ring = ring[:-1]
        for x, y in ring:
            key = (round(x, 3), round(y, 3))
            if key not in by_xy:
                n += 1
                by_xy[key] = str(n)
                numbered.append((str(n), "", x, y, None))
            corners.append(by_xy[key])
        plots.append((str(pno), pname or "", corners))
    for num, name, x, y, h in numbered:
        w.writerow(["A01", num, name, f"{y:.3f}", f"{x:.3f}", "" if h is None else f"{h:.3f}", ""])
    w.writerow(["A99", ""])
    if plots:
        w.writerow(["Z00", "区画ﾃﾞｰﾀ", ""])
        for pno, pname, corners in plots:
            w.writerow(["D00", pno, pname, "1", ""])
            for k, c in enumerate(corners, 1):
                w.writerow(["B01", str(k), c, "", ""])
            w.writerow(["D99", ""])
    w.writerow(["A99", "END", ""])
    with open(path, "wb") as f:
        f.write(out.getvalue().encode("cp932", errors="replace"))
    return path
