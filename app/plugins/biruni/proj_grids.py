"""Official shift grids: which ones a transformation needs, which are installed, and
downloading the open ones from the PROJ CDN into Biruni's own proj folder.

Only https://cdn.proj.org/ files are downloaded (open licences, listed in PROJ's own
database). Licensed grids (for example the Italian IGM files) are explained, not fetched."""
import math
import os

from qgis.core import (QgsApplication, QgsCoordinateReferenceSystem, QgsCoordinateTransform,
                       QgsCoordinateTransformContext, QgsDatumTransform, QgsPointXY, QgsProjUtils)

from .i18n import tr

CDN = "https://cdn.proj.org/"
GRID_EXT = (".tif", ".gsb", ".gtx", ".json", ".byn", ".las", ".los", ".lla", ".gvb")
WGS84 = QgsCoordinateReferenceSystem("EPSG:4326")



def install_dir():
    """Biruni's writable proj folder (inside the user profile; PROJ searches it)."""
    d = os.path.join(QgsApplication.qgisSettingsDirPath(), "proj")
    os.makedirs(d, exist_ok=True)
    return d


def search_dirs():
    dirs = [p for p in QgsProjUtils.searchPaths() if p]
    own = install_dir()
    if os.path.normcase(os.path.abspath(own)) not in {os.path.normcase(os.path.abspath(p)) for p in dirs}:
        dirs.append(own)
    return dirs


def installed_grids():
    """{file name: full path} of the grid files PROJ can find."""
    out = {}
    for d in search_dirs():
        if not os.path.isdir(d):
            continue
        for name in os.listdir(d):
            if name.lower().endswith(GRID_EXT) and name not in out:
                out[name] = os.path.join(d, name)
    return out


def operations(src, dst):
    """The transformations PROJ knows between two systems: dicts with name, accuracy (m, -1 unknown),
    available, proj, missing grids."""
    out = []
    for op in QgsDatumTransform.operations(src, dst):
        missing = [{"name": g.shortName, "url": g.url, "open_license": g.openLicense, "direct": g.directDownload}
                   for g in op.grids if not g.isAvailable]
        out.append({"name": op.name, "accuracy": op.accuracy, "available": op.isAvailable, "proj": op.proj,
                    "missing": missing})
    return out


def missing_grids(src, dst):
    """The grids that would make better transformations between two systems possible, best accuracy first:
    dicts with name, url, open_license, direct, operation, accuracy, downloadable."""
    seen, out = set(), []
    ops = operations(src, dst)
    ops.sort(key=lambda o: o["accuracy"] if o["accuracy"] is not None and o["accuracy"] >= 0 else 1e9)
    for op in ops:
        for g in op["missing"]:
            if g["name"] in seen:
                continue
            seen.add(g["name"])
            out.append({**g, "operation": op["name"], "accuracy": op["accuracy"],
                        "downloadable": bool(g["url"]) and g["url"].startswith(CDN) and g["open_license"]})
    return out


def _fetch(url):
    """Bytes of a URL (blocking; fine inside a QgsTask or processing algorithm)."""
    from qgis.core import QgsBlockingNetworkRequest
    from qgis.PyQt.QtCore import QUrl
    from qgis.PyQt.QtNetwork import QNetworkRequest

    req = QgsBlockingNetworkRequest()
    err = req.get(QNetworkRequest(QUrl(url)), forceRefresh=True)
    if err != QgsBlockingNetworkRequest.ErrorCode.NoError:
        raise OSError(req.errorMessage())
    return bytes(req.reply().content())


def download(names, feedback=None, fetch=None, target=None):
    """Download open grids by file name (or the dicts from missing_grids) into the proj folder.
    Returns {name: path or error text}. Only cdn.proj.org files with an open licence are fetched."""
    fetch = fetch or _fetch
    target = target or install_dir()
    os.makedirs(target, exist_ok=True)
    items = [n["name"] if isinstance(n, dict) else n for n in names]
    result = {}
    for i, name in enumerate(items):
        if feedback is not None and feedback.isCanceled():
            break
        if not name or "/" in name or "\\" in name or name.startswith("."):
            result[name] = tr("not a grid file name")
            continue
        path = os.path.join(target, name)
        if os.path.exists(path):
            result[name] = path
            continue
        url = CDN + name
        try:
            data = fetch(url)
            if not data:
                raise OSError(tr("empty download"))
            part = path + ".part"
            with open(part, "wb") as f:
                f.write(data)
            os.replace(part, path)
            result[name] = path
            if feedback is not None:
                feedback.pushInfo(tr("Downloaded {g} ({kb} KB).").format(g=name, kb=math.ceil(len(data) / 1024)))
        except Exception as e:  # noqa: BLE001 - reported per grid
            result[name] = tr("not downloaded: {e}").format(e=e)
        if feedback is not None:
            feedback.setProgress(100 * (i + 1) / max(len(items), 1))
    return result


def transformation_spread(crs, x, y, context=None, dst=WGS84):
    """How far apart the available transformations put one point (x, y in crs): {ops: [{name, accuracy, lon,
    lat, shift_m}], spread_m, missing: [grid dicts]}. shift_m is measured from the first (default) one."""
    from .algorithms.guess_crs import haversine_km

    context = context or QgsCoordinateTransformContext()
    out = {"ops": [], "spread_m": 0.0, "missing": missing_grids(crs, dst)}
    points = []
    for op in operations(crs, dst):
        if not op["available"]:
            continue
        try:
            xf = QgsCoordinateTransform(crs, dst, context)
            xf.setCoordinateOperation(op["proj"])
            xf.setAllowFallbackTransforms(False)
            p = xf.transform(QgsPointXY(x, y))
        except Exception:  # noqa: BLE001 - an operation that cannot place this point
            continue
        if not (math.isfinite(p.x()) and math.isfinite(p.y())):
            continue
        points.append(p)
        out["ops"].append({"name": op["name"], "accuracy": op["accuracy"], "lon": p.x(), "lat": p.y()})
    if points:
        base = points[0]
        for o, p in zip(out["ops"], points):
            o["shift_m"] = haversine_km(base.x(), base.y(), p.x(), p.y()) * 1000
        out["spread_m"] = max(haversine_km(a.x(), a.y(), b.x(), b.y()) * 1000 for a in points for b in points)
    return out


def licence_note(cc):
    """Where to get the grids PROJ cannot ship for a country ('' when every grid is open)."""
    notes = {"IT": tr("Italy: the IGM grids for Roma40 / ED50 to RDN2008 (GK2 / GSB files) are sold by Istituto "
                      "Geografico Militare. Copy them into Biruni's proj folder and they are used automatically.")}
    return notes.get((cc or "").upper(), "")
