"""Compare two versions of a layer (last week's delivery and this week's, my copy and the server's): what was
added, deleted, moved, reshaped and which attributes changed - as one layer and an Excel summary."""
from collections import Counter, defaultdict

from qgis.core import (Qgis, QgsCoordinateReferenceSystem, QgsCoordinateTransform, QgsFeature, QgsFeatureSink,
                       QgsField, QgsFields, QgsGeometry, QgsProcessingException, QgsProcessingParameterBoolean,
                       QgsProcessingParameterFeatureSink, QgsProcessingParameterFeatureSource,
                       QgsProcessingParameterField, QgsProcessingParameterFileDestination,
                       QgsProcessingParameterNumber, QgsSpatialIndex, QgsUnitTypes, QgsWkbTypes)
from qgis.PyQt.QtCore import QMetaType

from ..i18n import tr
from .base import BiruniAlgorithm
from .delivery_check import Book, is_null, show

# Fields that the software writes by itself: they change on every export and are not edits.
AUTO_FIELDS = {"fid", "ogc_fid", "objectid", "objectid_1", "shape_length", "shape_area", "shape_leng",
               "shape__length", "shape__area", "st_area(shape)", "st_length(shape)"}

ADDED, DELETED, MOVED, RESHAPED, ATTRIBUTES, UNCHANGED = (
    "added", "deleted", "moved", "reshaped", "attributes", "unchanged")
LABELS = {ADDED: "Feature added", DELETED: "Feature deleted", MOVED: "Feature moved", RESHAPED: "Feature reshaped",
          ATTRIBUTES: "Attributes changed", UNCHANGED: "Unchanged"}


def comparable(v):
    """A value as it should be compared: empty text and NULL are the same, numbers by value."""
    if is_null(v):
        return None
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return round(float(v), 9)
    if isinstance(v, str):
        s = v.strip()
        return s or None
    if hasattr(v, "toString"):
        return show(v)
    return str(v)


def key_text(v):
    c = comparable(v)
    if c is None:
        return None
    if isinstance(c, float):
        return str(int(c)) if c.is_integer() else repr(c)
    return str(c)


class Rec:
    __slots__ = ("fid", "geom", "work", "attrs", "values", "key", "ghash", "gtype")


class CompareLayers(BiruniAlgorithm):
    ID = "comparelayers"
    NAME = "Compare two versions of a layer"
    GROUP = "Engineering office"
    GROUP_ID = "office"
    HELP = ("Compares an old and a new version of a layer - two deliveries, your copy and the server's - and "
            "lists every difference: features added, deleted, moved (the same shape somewhere else), reshaped "
            "and features whose attributes changed, with the field, the old and the new value. Features are "
            "matched by a key field (an id) or, without one, by their geometry: identical features first, then "
            "the nearest feature of the same type within the search distance. Shifts smaller than the "
            "tolerance are ignored. Distances are in metres in any coordinate system. The result is a layer "
            "of changes and an Excel summary with a sentence such as '340 changed, 12 moved more than "
            "0.5 m'.")

    OLD, NEW, KEY, TOLERANCE, SEARCH, IGNORE, UNCHANGED, OUTPUT, REPORT = (
        "OLD", "NEW", "KEY", "TOLERANCE", "SEARCH", "IGNORE", "UNCHANGED", "OUTPUT", "REPORT")

    def initAlgorithm(self, config=None):  # noqa: N802
        self.addParameter(QgsProcessingParameterFeatureSource(
            self.OLD, tr("Old version"), [Qgis.ProcessingSourceType.Vector]))
        self.addParameter(QgsProcessingParameterFeatureSource(
            self.NEW, tr("New version"), [Qgis.ProcessingSourceType.Vector]))
        self.addParameter(QgsProcessingParameterField(
            self.KEY, tr("Key field (an id in both versions; empty = match by geometry)"),
            parentLayerParameterName=self.NEW, optional=True))
        self.addParameter(QgsProcessingParameterNumber(
            self.TOLERANCE, tr("Ignore shifts smaller than (m)"), Qgis.ProcessingNumberParameterType.Double,
            0.05, minValue=0.0))
        self.addParameter(QgsProcessingParameterNumber(
            self.SEARCH, tr("Without a key: look for a moved feature within (m)"),
            Qgis.ProcessingNumberParameterType.Double, 5.0, minValue=0.0))
        self.addParameter(QgsProcessingParameterField(
            self.IGNORE, tr("Fields to ignore"), parentLayerParameterName=self.NEW, allowMultiple=True,
            optional=True))
        self.addParameter(QgsProcessingParameterBoolean(
            self.UNCHANGED, tr("Also list unchanged features"), False))
        self.addParameter(QgsProcessingParameterFeatureSink(self.OUTPUT, tr("Changes")))
        self.addParameter(QgsProcessingParameterFileDestination(
            self.REPORT, tr("Summary"), tr("Excel workbook") + " (*.xlsx)", optional=True, createByDefault=True))

    # ---- setup -------------------------------------------------------------------------------------------
    @staticmethod
    def _work_crs(new, context, feedback):
        """A metric CRS for measuring: the layer's own when it is projected, else the UTM zone of the data."""
        crs = new.sourceCrs()
        if crs.isValid() and not crs.isGeographic() and crs.mapUnits() != Qgis.DistanceUnit.Unknown:
            return crs, QgsUnitTypes.fromUnitToUnitFactor(crs.mapUnits(), Qgis.DistanceUnit.Meters)
        if crs.isValid():
            ext = new.sourceExtent()
            wgs = QgsCoordinateReferenceSystem("EPSG:4326")
            c = ext.center() if not ext.isNull() else None  # a single point has an empty extent
            if c is not None:
                try:
                    c = QgsCoordinateTransform(crs, wgs, context.transformContext()).transform(c)
                except Exception:
                    c = None
            if c is not None and -180 <= c.x() <= 180 and -80 <= c.y() <= 84:
                zone = min(int((c.x() + 180) // 6) + 1, 60)
                return QgsCoordinateReferenceSystem(f"EPSG:{(32600 if c.y() >= 0 else 32700) + zone}"), 1.0
        feedback.pushWarning(tr("The layer has no usable coordinate system: distances are in layer units."))
        return crs, 1.0

    @staticmethod
    def _load(src, xform, cmp_idx, key_idx, grid, feedback, start, span):
        recs = {}
        total = max(src.featureCount(), 1)
        for i, f in enumerate(src.getFeatures()):
            if feedback.isCanceled():
                break
            r = Rec()
            r.fid = f.id()
            g = f.geometry()
            if g is None or g.isNull() or g.isEmpty():
                r.geom = r.work = None
                r.gtype = Qgis.GeometryType.Null
                r.ghash = None
            else:
                r.geom = QgsGeometry(g)
                w = QgsGeometry(g)
                if xform is not None:
                    try:
                        w.transform(xform)
                    except Exception:
                        w = QgsGeometry(g)
                r.work = w
                r.gtype = w.type()
                snapped = w.snappedToGrid(grid, grid)
                r.ghash = (r.gtype, bytes(snapped.asWkb())) if not snapped.isNull() else None
            attrs = f.attributes()
            r.attrs = attrs
            r.values = tuple(comparable(attrs[i]) for i in cmp_idx)
            r.key = key_text(attrs[key_idx]) if key_idx is not None else None
            recs[r.fid] = r
            if i % 1000 == 0:
                feedback.setProgress(start + span * i / total)
        return recs

    # ---- matching ----------------------------------------------------------------------------------------
    @staticmethod
    def _match_by_key(old, new):
        old_by, new_by = defaultdict(list), defaultdict(list)
        for r in old.values():
            if r.key is not None:
                old_by[r.key].append(r.fid)
        for r in new.values():
            if r.key is not None:
                new_by[r.key].append(r.fid)
        pairs = []
        dup = 0
        for k, ofids in old_by.items():
            nfids = new_by.get(k, [])
            if len(ofids) > 1 or len(nfids) > 1:
                dup += 1
            pairs.extend(zip(ofids, nfids))
        return pairs, dup

    @staticmethod
    def _match_by_geometry(old, new, search, feedback):
        pairs = []
        # 1. identical geometries (within a millimetre), preferring the same attributes
        groups = defaultdict(list)
        for r in new.values():
            if r.ghash is not None:
                groups[r.ghash].append(r.fid)
        used_new = set()
        left_old = []
        for r in old.values():
            cands = [n for n in groups.get(r.ghash, ()) if n not in used_new] if r.ghash is not None else []
            if not cands:
                left_old.append(r)
                continue
            best = next((n for n in cands if new[n].values == r.values), cands[0])
            used_new.add(best)
            pairs.append((r.fid, best))
        # 2. the nearest unmatched feature of the same type within the search distance
        index = QgsSpatialIndex()
        centre = {}
        for r in new.values():
            if r.fid in used_new or r.work is None:
                continue
            c = r.work.centroid()
            if c.isNull() or c.isEmpty():
                continue
            centre[r.fid] = c.asPoint()
            index.addFeature(r.fid, r.work.boundingBox())
        cands = []
        for k, r in enumerate(left_old):
            if feedback.isCanceled():
                break
            if r.work is None:
                continue
            c = r.work.centroid()
            if c.isNull() or c.isEmpty():
                continue
            p = c.asPoint()
            box = r.work.boundingBox()
            box.grow(search)
            for n in index.intersects(box):
                rn = new[n]
                if rn.gtype != r.gtype:
                    continue
                d = p.distance(centre[n])
                if d <= search:
                    cands.append((d, 0 if rn.values == r.values else 1, r.fid, n))
        cands.sort()
        used_old = set()
        for _d, _a, o, n in cands:
            if o in used_old or n in used_new:
                continue
            used_old.add(o)
            used_new.add(n)
            pairs.append((o, n))
        return pairs

    # ---- run ---------------------------------------------------------------------------------------------
    def processAlgorithm(self, parameters, context, feedback):  # noqa: N802
        old_src = self.parameterAsSource(parameters, self.OLD, context)
        new_src = self.parameterAsSource(parameters, self.NEW, context)
        if old_src is None:
            raise QgsProcessingException(self.invalidSourceError(parameters, self.OLD))
        if new_src is None:
            raise QgsProcessingException(self.invalidSourceError(parameters, self.NEW))
        key = self.parameterAsString(parameters, self.KEY, context)
        tol = self.parameterAsDouble(parameters, self.TOLERANCE, context)
        search = self.parameterAsDouble(parameters, self.SEARCH, context)
        ignore = {n.casefold() for n in self.parameterAsStrings(parameters, self.IGNORE, context)}
        with_unchanged = self.parameterAsBoolean(parameters, self.UNCHANGED, context)
        report = self.parameterAsFileOutput(parameters, self.REPORT, context)

        of_old, of_new = old_src.fields(), new_src.fields()
        old_names = {of_old.at(i).name().casefold(): i for i in range(of_old.count())}
        key_new = key_old = None
        if key:
            key_new = of_new.lookupField(key)
            key_old = old_names.get(key.casefold())
            if key_old is None:
                raise QgsProcessingException(tr("The old version has no field {field}.").format(field=key))
        elif new_src.wkbType() == Qgis.WkbType.NoGeometry or old_src.wkbType() == Qgis.WkbType.NoGeometry:
            raise QgsProcessingException(tr("Tables have no geometry to match: choose a key field."))

        compared = []  # (name, old index, new index)
        for i in range(of_new.count()):
            name = of_new.at(i).name()
            low = name.casefold()
            if low in ignore or low in AUTO_FIELDS or (key and low == key.casefold()) or low not in old_names:
                continue
            compared.append((name, old_names[low], i))
        new_lower = {of_new.at(i).name().casefold() for i in range(of_new.count())}
        only_old = [of_old.at(i).name() for i in range(of_old.count())
                    if of_old.at(i).name().casefold() not in new_lower
                    and of_old.at(i).name().casefold() not in AUTO_FIELDS]
        only_new = [of_new.at(i).name() for i in range(of_new.count())
                    if of_new.at(i).name().casefold() not in old_names
                    and of_new.at(i).name().casefold() not in AUTO_FIELDS]

        work, factor = self._work_crs(new_src, context, feedback)
        tc = context.transformContext()
        x_old = (QgsCoordinateTransform(old_src.sourceCrs(), work, tc)
                 if old_src.sourceCrs().isValid() and work.isValid() and old_src.sourceCrs() != work else None)
        x_new = (QgsCoordinateTransform(new_src.sourceCrs(), work, tc)
                 if new_src.sourceCrs().isValid() and work.isValid() and new_src.sourceCrs() != work else None)
        to_new = (QgsCoordinateTransform(old_src.sourceCrs(), new_src.sourceCrs(), tc)
                  if old_src.sourceCrs().isValid() and new_src.sourceCrs().isValid()
                  and old_src.sourceCrs() != new_src.sourceCrs() else None)
        grid = 0.001 / factor  # a millimetre in work units
        tol_w, search_w = tol / factor, search / factor

        feedback.pushInfo(tr("Reading the old version…"))
        old = self._load(old_src, x_old, [c[1] for c in compared], key_old, grid, feedback, 0, 25)
        feedback.pushInfo(tr("Reading the new version…"))
        new = self._load(new_src, x_new, [c[2] for c in compared], key_new, grid, feedback, 25, 25)
        if feedback.isCanceled():
            return {}

        feedback.pushInfo(tr("Matching features…"))
        if key:
            pairs, dup = self._match_by_key(old, new)
            if dup:
                feedback.pushWarning(tr("{n} key values appear more than once: those features are matched in "
                                        "order.").format(n=dup))
        else:
            pairs = self._match_by_geometry(old, new, search_w, feedback)
        feedback.setProgress(60)

        out_fields = QgsFields()
        for name, t in (("change_type", QMetaType.Type.QString), ("change", QMetaType.Type.QString),
                        ("key", QMetaType.Type.QString), ("old_fid", QMetaType.Type.LongLong),
                        ("new_fid", QMetaType.Type.LongLong), ("moved_m", QMetaType.Type.Double),
                        ("area_delta", QMetaType.Type.Double), ("length_delta", QMetaType.Type.Double),
                        ("changed_fields", QMetaType.Type.QString), ("details", QMetaType.Type.QString)):
            out_fields.append(QgsField(name, t))
        wkb = new_src.wkbType()
        if wkb == Qgis.WkbType.NoGeometry and old_src.wkbType() != Qgis.WkbType.NoGeometry:
            wkb = old_src.wkbType()
        out_wkb = QgsWkbTypes.multiType(wkb) if wkb != Qgis.WkbType.NoGeometry else wkb
        sink, sink_id = self.parameterAsSink(parameters, self.OUTPUT, context, out_fields, out_wkb,
                                             new_src.sourceCrs())

        rows = []  # report rows
        counts = Counter()
        field_changes = Counter()
        attr_changed = 0

        def emit(kind, o, n, moved=None, da=None, dl=None, changed=(), details=""):
            counts[kind] += 1
            if kind == UNCHANGED and not with_unchanged:
                return
            f = QgsFeature(out_fields)
            if n is not None and n.geom is not None:
                g = QgsGeometry(n.geom)
            elif o is not None and o.geom is not None:
                g = QgsGeometry(o.geom)
                if to_new is not None:
                    try:
                        g.transform(to_new)
                    except Exception:
                        g = None
            else:
                g = None
            if g is not None and out_wkb != Qgis.WkbType.NoGeometry:
                g.convertToMultiType()
                f.setGeometry(g)
            k = (n.key if n is not None else o.key) if key else None
            row = [kind, tr(LABELS[kind]), k, o.fid if o is not None else None, n.fid if n is not None else None,
                   moved, da, dl, ", ".join(changed), details[:2000]]
            f.setAttributes(row)
            sink.addFeature(f, QgsFeatureSink.Flag.FastInsert)
            if kind != UNCHANGED:
                rows.append(row)

        matched_old, matched_new = set(), set()
        total = max(len(pairs), 1)
        for i, (o_id, n_id) in enumerate(pairs):
            if feedback.isCanceled():
                return {}
            o, n = old[o_id], new[n_id]
            matched_old.add(o_id)
            matched_new.add(n_id)
            kind, moved, da, dl = UNCHANGED, None, None, None
            if (o.work is None) != (n.work is None):
                kind = RESHAPED
            elif o.work is not None and o.ghash != n.ghash:
                h = o.work.hausdorffDistance(n.work)
                if h < 0:
                    h = o.work.distance(n.work)
                if h > tol_w:
                    moved = round(h * factor, 3)
                    kind = RESHAPED
                    if o.gtype == Qgis.GeometryType.Point:
                        kind = MOVED
                    else:
                        co, cn = o.work.centroid().asPoint(), n.work.centroid().asPoint()
                        shifted = QgsGeometry(o.work)
                        shifted.translate(cn.x() - co.x(), cn.y() - co.y())
                        if 0 <= shifted.hausdorffDistance(n.work) <= tol_w:
                            kind = MOVED
                if o.gtype == Qgis.GeometryType.Polygon:
                    da = round((n.work.area() - o.work.area()) * factor * factor, 3)
                elif o.gtype == Qgis.GeometryType.Line:
                    dl = round((n.work.length() - o.work.length()) * factor, 3)
            changed, details = [], []
            for (name, oi, ni), ov, nv in zip(compared, o.values, n.values):
                if ov != nv:
                    changed.append(name)
                    field_changes[name] += 1
                    details.append(f"{name}: {show(o.attrs[oi])} → {show(n.attrs[ni])}")
            if changed:
                attr_changed += 1
                if kind == UNCHANGED:
                    kind = ATTRIBUTES
            emit(kind, o, n, moved, da, dl, changed, "; ".join(details))
            if i % 1000 == 0:
                feedback.setProgress(60 + 30 * i / total)

        for r in old.values():
            if r.fid not in matched_old:
                emit(DELETED, r, None)
        for r in new.values():
            if r.fid not in matched_new:
                emit(ADDED, None, r)
        feedback.setProgress(95)

        changed_total = counts[MOVED] + counts[RESHAPED] + counts[ATTRIBUTES]
        sentence = tr("{changed} changed ({moved} moved more than {tol} m, {reshaped} reshaped, {attrs} with "
                      "changed attributes), {added} added, {deleted} deleted, {same} unchanged.").format(
            changed=changed_total, moved=counts[MOVED], tol=f"{tol:g}", reshaped=counts[RESHAPED],
            attrs=attr_changed, added=counts[ADDED], deleted=counts[DELETED], same=counts[UNCHANGED])
        feedback.pushInfo(sentence)
        if report:
            self._report(report, sentence, counts, field_changes, only_old, only_new, rows)
        return {self.OUTPUT: sink_id, self.REPORT: report, "SUMMARY": sentence, "CHANGED": changed_total,
                "ADDED": counts[ADDED], "DELETED": counts[DELETED], "MOVED": counts[MOVED],
                "RESHAPED": counts[RESHAPED], "ATTRIBUTES": counts[ATTRIBUTES], "ATTRIBUTE_CHANGES": attr_changed,
                "UNCHANGED": counts[UNCHANGED]}

    @staticmethod
    def _report(path, sentence, counts, field_changes, only_old, only_new, rows):
        book = Book()
        ws = book.sheet(tr("Summary"))
        ws.column_dimensions["A"].width = 34
        ws.column_dimensions["B"].width = 16
        book.title(ws, 1, tr("Comparison of two versions"), 16)
        ws.cell(row=2, column=1, value=sentence)
        r = book.table(ws, 4, [tr("Change"), tr("Features")],
                       [[tr(LABELS[k]), counts[k]] for k in (ADDED, DELETED, MOVED, RESHAPED, ATTRIBUTES,
                                                              UNCHANGED)])
        if field_changes:
            r = book.table(ws, r, [tr("Field"), tr("Features with a new value")], field_changes.most_common())
        if only_old or only_new:
            r = book.table(ws, r, [tr("Field"), tr("Change")],
                           [[n, tr("Field removed")] for n in only_old] + [[n, tr("Field added")] for n in only_new])
        book.footer(ws, r)
        heads = [tr("Change"), tr("Key"), tr("Old id"), tr("New id"), tr("Moved (m)"), tr("Area change (m²)"),
                 tr("Length change (m)"), tr("Changed fields"), tr("Details")]
        moves = sorted((x for x in rows if x[5] is not None), key=lambda x: -x[5])[:100]
        book.sheet(tr("Top moves"), heads, [x[1:] for x in moves], [18, 16, 10, 10, 12, 16, 16, 30, 60])
        book.sheet(tr("Changes"), heads, [x[1:] for x in rows], [18, 16, 10, 10, 12, 16, 16, 30, 60])
        book.save(path)
