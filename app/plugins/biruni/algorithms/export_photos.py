import csv
import datetime
import io
import os
import re
import shutil

from qgis.core import (Qgis, QgsCoordinateReferenceSystem, QgsCoordinateTransform, QgsFeatureRequest,
                       QgsProcessingException, QgsProcessingParameterBoolean, QgsProcessingParameterEnum,
                       QgsProcessingParameterField, QgsProcessingParameterFileDestination,
                       QgsProcessingParameterFolderDestination, QgsProcessingParameterNumber,
                       QgsProcessingParameterString, QgsProcessingParameterVectorLayer, QgsProject)
from qgis.PyQt.QtCore import QByteArray, QDate, QDateTime, QMetaType

from ..i18n import tr
from .base import BiruniAlgorithm

IMAGE_EXT = {".jpg", ".jpeg", ".png", ".tif", ".tiff", ".bmp", ".gif", ".webp", ".heic"}
RESIZABLE = {".jpg", ".jpeg", ".png", ".tif", ".tiff", ".bmp", ".webp"}
_BAD = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_RESERVED = {"CON", "PRN", "AUX", "NUL"} | {f"COM{i}" for i in range(1, 10)} | {f"LPT{i}" for i in range(1, 10)}
_FIELD = re.compile(r"\{([^{}]+)\}")
MAGIC = [(b"\xff\xd8\xff", ".jpg"), (b"\x89PNG\r\n\x1a\n", ".png"), (b"GIF8", ".gif"), (b"%PDF", ".pdf"),
         (b"II*\x00", ".tif"), (b"MM\x00*", ".tif"), (b"BM", ".bmp"), (b"PK\x03\x04", ".zip")]
MIME = {"image/jpeg": ".jpg", "image/jpg": ".jpg", "image/png": ".png", "image/gif": ".gif", "image/tiff": ".tif",
        "image/bmp": ".bmp", "image/webp": ".webp", "application/pdf": ".pdf", "video/mp4": ".mp4",
        "audio/mpeg": ".mp3", "audio/mp4": ".m4a", "image/heic": ".heic"}


# ---- names ----------------------------------------------------------------------------------------------------

def sanitise(name, max_len=150):
    """A file or folder name Windows accepts; Arabic and other letters are kept."""
    s = _BAD.sub("_", str(name)).strip().rstrip(". ")
    s = re.sub(r"\s+", " ", s)
    if not s:
        s = "_"
    if s.split(".")[0].upper() in _RESERVED:
        s = "_" + s
    return s[:max_len].rstrip(". ") or "_"


def _text(value):
    if value is None or (hasattr(value, "isNull") and value.isNull()):
        return ""
    if isinstance(value, QDateTime):
        return value.toString("yyyy-MM-dd_HH-mm-ss")
    if isinstance(value, QDate):
        return value.toString("yyyy-MM-dd")
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


def render_template(template, values):
    """'{zone}_{pole_no}_{n}' with the feature's values (field names case-insensitive)."""
    lower = {k.lower(): v for k, v in values.items()}

    def sub(m):
        key = m.group(1).strip()
        return _text(values[key] if key in values else lower.get(key.lower(), ""))

    return _FIELD.sub(sub, template)


def template_fields(template):
    return [m.strip() for m in _FIELD.findall(template)]


class UniqueNames:
    """Hands out file names that are not used yet in a folder (this run or already on disk)."""

    def __init__(self):
        self.used = set()

    def take(self, folder, stem, ext):
        stem = sanitise(stem)
        n, cand = 1, stem + ext
        while cand.lower() in self.used or os.path.exists(os.path.join(folder, cand)):
            n += 1
            cand = f"{stem}_{n}{ext}"
        self.used.add(cand.lower())
        return cand


def guess_ext(data, name="", mime=""):
    ext = os.path.splitext(name or "")[1].lower()
    if ext and len(ext) <= 6:
        return ".jpg" if ext == ".jpeg" else ext
    if mime and mime.lower() in MIME:
        return MIME[mime.lower()]
    head = bytes(data[:12]) if data else b""
    for sig, e in MAGIC:
        if head.startswith(sig):
            return e
    if head[4:12] in (b"ftypheic", b"ftypmif1"):
        return ".heic"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return ".webp"
    return ".bin"


# ---- where the files are --------------------------------------------------------------------------------------

def layer_file(layer):
    """(datasource path, layer/table name) of an OGR layer, else (None, None)."""
    if layer is None or layer.providerType() != "ogr":
        return None, None
    from qgis.core import QgsProviderRegistry

    parts = QgsProviderRegistry.instance().decodeUri("ogr", layer.source())
    return parts.get("path") or None, parts.get("layerName") or None


def attachment_table(path, table):
    """Name of the ArcGIS-style attachments table of `table` (<table>__ATTACH) in the datasource."""
    if not path or not os.path.exists(path):
        return None
    from osgeo import ogr

    ds = ogr.Open(path)
    if ds is None:
        return None
    names = [ds.GetLayerByIndex(i).GetName() for i in range(ds.GetLayerCount())]
    if not table:
        table = names[0] if len(names) == 1 else None
    if not table:
        return None
    want = (table + "__attach").lower()
    return next((n for n in names if n.lower() == want), None)


def read_attachments(path, table):
    """{key: [(name, mime, bytes)]} from <table>__ATTACH; key is ('gid', GlobalID) or ('oid', ObjectID)."""
    from osgeo import ogr

    ds = ogr.Open(path)
    lyr = ds.GetLayerByName(table)
    defn = lyr.GetLayerDefn()
    names = {defn.GetFieldDefn(i).GetName().lower(): defn.GetFieldDefn(i).GetName()
             for i in range(defn.GetFieldCount())}
    gid = names.get("rel_globalid")
    oid = names.get("rel_objectid")
    att = names.get("att_name")
    mime = names.get("content_type")
    data = names.get("data")
    out = {}
    if not data:
        return out
    for f in lyr:
        blob = f.GetFieldAsBinary(data)
        if not blob:
            continue
        if gid and f.GetField(gid):
            key = ("gid", str(f.GetField(gid)).strip("{}").lower())
        elif oid and f.GetField(oid) is not None:
            key = ("oid", int(f.GetField(oid)))
        else:
            continue
        out.setdefault(key, []).append((f.GetField(att) if att else "", f.GetField(mime) if mime else "", blob))
    return out


def resolve_path(value, bases):
    """A stored path (absolute, relative to the project / layer folder, or QField's DCIM/…) -> existing file."""
    s = _text(value).strip().strip('"')
    if not s:
        return None
    if s.lower().startswith("file:///"):
        s = s[8:]
    s = s.replace("\\", os.sep).replace("/", os.sep)
    if os.path.isabs(s):
        return s if os.path.isfile(s) else None
    for base in bases:
        if not base:
            continue
        for cand in (os.path.join(base, s), os.path.join(base, "DCIM", os.path.basename(s))):
            if os.path.isfile(cand):
                return os.path.normpath(cand)
    return None


# ---- images ---------------------------------------------------------------------------------------------------

def _dms(value):
    from PIL.TiffImagePlugin import IFDRational

    value = abs(value)
    d = int(value)
    m = int((value - d) * 60)
    s = (value - d - m / 60) * 3600
    return (IFDRational(d, 1), IFDRational(m, 1), IFDRational(int(round(s * 10000)), 10000))


def write_image(data_or_path, dest, ext, max_px=0, quality=90, gps=None):
    """Copy an image to dest, optionally smaller, re-encoded and with the GPS position in its EXIF.
    Returns True when Pillow handled it, False when the bytes were copied as they are."""
    if ext not in RESIZABLE or (not max_px and gps is None):
        return False
    try:
        from PIL import Image, ImageOps
    except ImportError:
        return False
    src = io.BytesIO(data_or_path) if isinstance(data_or_path, (bytes, bytearray)) else data_or_path
    try:
        img = Image.open(src)
        exif = img.getexif()
        img = ImageOps.exif_transpose(img)
    except Exception:  # noqa: BLE001 - not an image Pillow reads: copy it unchanged
        return False
    if max_px and max(img.size) > max_px:
        img.thumbnail((max_px, max_px), Image.Resampling.LANCZOS)
    if 0x0112 in exif:
        exif[0x0112] = 1  # the pixels are upright now
    fmt = {".jpg": "JPEG", ".jpeg": "JPEG", ".png": "PNG", ".tif": "TIFF", ".tiff": "TIFF", ".bmp": "BMP",
           ".webp": "WEBP"}[ext]
    if gps is not None and fmt in ("JPEG", "TIFF", "WEBP", "PNG"):
        lon, lat = gps
        gif = exif.get_ifd(0x8825)
        gif.update({0: (2, 3, 0, 0), 1: "N" if lat >= 0 else "S", 2: _dms(lat), 3: "E" if lon >= 0 else "W",
                    4: _dms(lon)})
        exif[0x8825] = gif
    kw = {"exif": exif.tobytes()} if fmt in ("JPEG", "TIFF", "WEBP", "PNG") else {}
    if fmt == "JPEG":
        if img.mode not in ("RGB", "L"):
            img = img.convert("RGB")
        kw.update(quality=int(quality), optimize=True)
    elif fmt == "WEBP":
        kw.update(quality=int(quality))
    img.save(dest, fmt, **kw)
    return True


# ---- the tool -------------------------------------------------------------------------------------------------

class ExportPhotos(BiruniAlgorithm):
    """Photos/attachments of features -> files named after the features."""

    ID = "exportphotos"
    NAME = "Export photos and attachments"
    GROUP = "Project"
    GROUP_ID = "project"
    HELP = ("Saves the photos and other files kept with the features of a layer into a folder, each one named "
            "after its feature. The files can come from an attachments table (ArcGIS <layer>__ATTACH tables in a "
            "File Geodatabase or GeoPackage), from a field that holds the path of the file (QField photos in DCIM, "
            "paths relative to the project or the layer folder) or from a binary (blob) field. The name is built "
            "from a template with field names in braces, e.g. {zone}_{pole_no}_{n} - {n} numbers the files of one "
            "feature, {fid} is the feature id and {name} the original file name. Names that would clash get _2, "
            "_3 …. Optionally: a subfolder per value of a field, only the selected features, smaller photos "
            "(longest side in pixels) and the feature's position written into each photo (EXIF GPS), so photo "
            "apps and Google Earth place it on the map. An index (Excel or CSV) lists every file with its "
            "feature.")

    INPUT, SOURCE, FIELD, TEMPLATE, SUBFOLDER = "INPUT", "SOURCE", "FIELD", "TEMPLATE", "SUBFOLDER"
    SELECTED, MAX_SIZE, QUALITY, GPS, OUTPUT, INDEX = "SELECTED", "MAX_SIZE", "QUALITY", "GPS", "OUTPUT", "INDEX"
    MODES = ["Automatic (everything found)", "Attachments table", "Field with the file path",
               "Binary (blob) field"]

    def initAlgorithm(self, config=None):  # noqa: N802
        self.addParameter(QgsProcessingParameterVectorLayer(self.INPUT, tr("Layer")))
        self.addParameter(QgsProcessingParameterEnum(self.SOURCE, tr("Where the files are"),
                                                     [tr(s) for s in self.MODES], defaultValue=0))
        self.addParameter(QgsProcessingParameterField(
            self.FIELD, tr("Field with the file path or the file itself"), parentLayerParameterName=self.INPUT,
            optional=True))
        self.addParameter(QgsProcessingParameterString(
            self.TEMPLATE, tr("File name (field names in braces)"), "{fid}_{n}"))
        self.addParameter(QgsProcessingParameterField(
            self.SUBFOLDER, tr("Subfolder for each value of"), parentLayerParameterName=self.INPUT, optional=True))
        self.addParameter(QgsProcessingParameterBoolean(self.SELECTED, tr("Selected features only"), False))
        self.addParameter(QgsProcessingParameterNumber(
            self.MAX_SIZE, tr("Make photos smaller: longest side in pixels (0 = keep the size)"),
            defaultValue=0, minValue=0))
        self.addParameter(QgsProcessingParameterNumber(
            self.QUALITY, tr("JPEG quality (when photos are made smaller)"), defaultValue=90, minValue=30,
            maxValue=100))
        self.addParameter(QgsProcessingParameterBoolean(
            self.GPS, tr("Write the feature's position into the photos (EXIF GPS)"), False))
        self.addParameter(QgsProcessingParameterFolderDestination(self.OUTPUT, tr("Folder")))
        self.addParameter(QgsProcessingParameterFileDestination(
            self.INDEX, tr("Index of the files"), "Excel (*.xlsx);;CSV (*.csv)", optional=True,
            createByDefault=True))

    def flags(self):
        # reads the selection of a project layer directly - run on the main thread
        return super().flags() | Qgis.ProcessingAlgorithmFlag.NoThreading

    def processAlgorithm(self, parameters, context, feedback):  # noqa: N802
        layer = self.parameterAsVectorLayer(parameters, self.INPUT, context)
        if layer is None:
            raise QgsProcessingException(tr("Choose a layer."))
        mode = self.parameterAsEnum(parameters, self.SOURCE, context)
        field = (self.parameterAsFields(parameters, self.FIELD, context) or [None])[0]
        template = self.parameterAsString(parameters, self.TEMPLATE, context).strip() or "{fid}_{n}"
        subfield = (self.parameterAsFields(parameters, self.SUBFOLDER, context) or [None])[0]
        selected = self.parameterAsBoolean(parameters, self.SELECTED, context)
        max_px = self.parameterAsInt(parameters, self.MAX_SIZE, context)
        quality = self.parameterAsInt(parameters, self.QUALITY, context)
        gps = self.parameterAsBoolean(parameters, self.GPS, context)
        folder = self.parameterAsString(parameters, self.OUTPUT, context)
        index_path = self.parameterAsFileOutput(parameters, self.INDEX, context)
        os.makedirs(folder, exist_ok=True)

        fields = layer.fields()
        known = {f.name().lower() for f in fields} | {"n", "fid", "name", "layer"}
        unknown = [n for n in template_fields(template) if n.lower() not in known]
        if unknown:
            feedback.pushWarning(tr("Not fields of the layer, left empty in the names: {f}").format(
                f=", ".join(unknown)))

        # sources
        path, table = layer_file(layer)
        attach = {}
        if mode in (0, 1):
            at = attachment_table(path, table)
            if at:
                attach = read_attachments(path, at)
                feedback.pushInfo(tr("Attachments table {t}: {n} features with files.").format(t=at, n=len(attach)))
            elif mode == 1:
                raise QgsProcessingException(tr("This layer has no attachments table (<layer>__ATTACH)."))
        path_fields, blob_fields = [], []
        if field:
            idx = fields.lookupField(field)
            (blob_fields if fields[idx].type() == QMetaType.Type.QByteArray else path_fields).append(field)
        if mode == 2 and not path_fields:
            raise QgsProcessingException(tr("Choose the field that holds the file path."))
        if mode == 3 and not blob_fields:
            raise QgsProcessingException(tr("Choose a binary (blob) field."))
        if mode == 0:
            for f in fields:
                if f.name() in path_fields + blob_fields:
                    continue
                if f.type() == QMetaType.Type.QByteArray:
                    blob_fields.append(f.name())
                elif layer.editorWidgetSetup(fields.indexOf(f.name())).type() == "ExternalResource":
                    path_fields.append(f.name())
        gid_field = next((f.name() for f in fields if f.name().lower() == "globalid"), None)
        oid_field = next((f.name() for f in fields if f.name().lower() == "objectid"), None)
        if not (attach or path_fields or blob_fields):
            raise QgsProcessingException(tr("No photos or attachments were found in this layer: choose the field "
                                            "that holds them."))

        bases = [QgsProject.instance().homePath(), os.path.dirname(path) if path else None]
        if context.project() is not None:
            bases.insert(0, context.project().homePath())

        to_wgs = None
        if gps:
            to_wgs = QgsCoordinateTransform(layer.crs(), QgsCoordinateReferenceSystem("EPSG:4326"),
                                            context.transformContext())

        request = QgsFeatureRequest()
        if selected:
            ids = layer.selectedFeatureIds()
            if not ids:
                raise QgsProcessingException(tr("No features are selected."))
            request.setFilterFids(ids)
        total = len(request.filterFids()) if selected else max(layer.featureCount(), 1)

        names = {}
        rows, written, missing, smaller = [], 0, 0, 0
        field_names = [f.name() for f in fields]
        for i, feat in enumerate(layer.getFeatures(request)):
            if feedback.isCanceled():
                break
            items = []  # (original name, bytes or None, path or None, mime)
            if attach:
                keys = []
                if gid_field and feat[gid_field]:
                    keys.append(("gid", str(feat[gid_field]).strip("{}").lower()))
                if oid_field and feat[oid_field] is not None:
                    keys.append(("oid", int(feat[oid_field])))
                keys.append(("oid", int(feat.id())))
                for k in keys:
                    if k in attach:
                        items += [(n, b, None, m) for n, m, b in attach[k]]
                        break
            for fname in path_fields:
                value = feat[fname]
                for part in [p for p in re.split(r"[;\n]", _text(value)) if p.strip()] or []:
                    src = resolve_path(part, bases)
                    if src is None:
                        missing += 1
                        feedback.pushWarning(tr("Feature {fid}: file not found: {p}").format(fid=feat.id(), p=part))
                        continue
                    items.append((os.path.basename(src), None, src, ""))
            for fname in blob_fields:
                value = feat[fname]
                if isinstance(value, QByteArray):
                    value = bytes(value)
                if value:
                    items.append(("", value, None, ""))
            if not items:
                continue

            values = {"fid": feat.id(), "layer": layer.name(), "name": ""}
            values.update({n: feat[n] for n in field_names})  # a field called name/layer wins
            has_name_field = "name" in {n.lower() for n in field_names}
            sub = sanitise(_text(feat[subfield])) if subfield else ""
            out_dir = os.path.join(folder, sub) if sub else folder
            os.makedirs(out_dir, exist_ok=True)
            pos = None
            if gps and feat.hasGeometry():
                pt = feat.geometry().pointOnSurface().asPoint()
                try:
                    p = to_wgs.transform(pt)
                    pos = (p.x(), p.y())
                except Exception:  # noqa: BLE001
                    pos = None
            for n, (orig, data, src, mime) in enumerate(items, 1):
                if data is None:
                    with open(src, "rb") as fh:
                        head = fh.read(16)
                else:
                    head = data[:16]
                ext = guess_ext(head, orig, mime)
                values["n"] = n
                if not has_name_field:
                    values["name"] = os.path.splitext(orig)[0]
                stem = render_template(template, values) or f"{feat.id()}_{n}"
                uniq = names.setdefault(out_dir, UniqueNames())
                fn = uniq.take(out_dir, stem, ext)
                dest = os.path.join(out_dir, fn)
                done = write_image(data if data is not None else src, dest, ext, max_px, quality, pos)
                if done:
                    smaller += 1
                elif data is not None:
                    with open(dest, "wb") as fh:
                        fh.write(data)
                else:
                    shutil.copy2(src, dest)
                written += 1
                rows.append([os.path.relpath(dest, folder), feat.id(), orig or ""] +
                            [_text(feat[f]) for f in field_names])
            feedback.setProgress(min(99, 100 * (i + 1) / total))

        if index_path:
            self._write_index(index_path, ["file", "fid", "original_name"] + field_names, rows)
        feedback.pushInfo(tr("{n} files saved in {f}.").format(n=written, f=folder))
        if missing:
            feedback.pushWarning(tr("{n} files listed in the layer were not found.").format(n=missing))
        return {self.OUTPUT: folder, self.INDEX: index_path or None, "FILE_COUNT": written, "MISSING_COUNT": missing,
                "REENCODED_COUNT": smaller}

    @staticmethod
    def _write_index(path, header, rows):
        if path.lower().endswith(".csv"):
            with open(path, "w", encoding="utf-8-sig", newline="") as f:
                w = csv.writer(f)
                w.writerow(header)
                w.writerows(rows)
            return
        from openpyxl import Workbook

        from .. import i18n

        wb = Workbook()
        ws = wb.active
        ws.title = tr("Files")[:31]
        ws.sheet_view.rightToLeft = i18n.is_rtl()
        ws.append(header)
        for r in rows:
            ws.append(r)
        ws.freeze_panes = "A2"
        for col, name in zip(ws.columns, header):
            width = max([len(str(name))] + [len(str(c.value or "")) for c in col[:200]])
            ws.column_dimensions[col[0].column_letter].width = min(60, width + 2)
        stamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
        wb.properties.title = f"Biruni GIS - {stamp}"
        wb.save(path)
