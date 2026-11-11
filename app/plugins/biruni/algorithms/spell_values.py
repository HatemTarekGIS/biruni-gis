"""Find spelling variants: the same name written in different ways in one field
(«الملك فهد» / «الملك فهـد» / «الملك  فهد» / «الملك فهد » …), grouped, with the most
common spelling - or the spelling of an official list - suggested for all of them.
The suggestions can be applied to the layer in an edit session (undoable)."""
import os
import re
from collections import Counter, defaultdict

from qgis.core import (Qgis, QgsFeature, QgsFeatureRequest, QgsFeatureSink, QgsField, QgsFields,
                       QgsProcessingException, QgsProcessingParameterBoolean, QgsProcessingParameterFeatureSink,
                       QgsProcessingParameterField, QgsProcessingParameterFile, QgsProcessingParameterNumber,
                       QgsProcessingParameterString, QgsProcessingParameterVectorLayer)
from qgis.PyQt.QtCore import QMetaType

from ..arabic_text import latin_skeleton, normalise, script, similarity_keys, trigrams
from ..i18n import tr
from .arabic_join import NameIndex
from .base import BiruniAlgorithm

CANDIDATES = 20         # best trigram-sharing spellings scored per value
COMMON_TRIGRAM = 400    # trigrams shared by more spellings than this are not used to find candidates
TRANSLIT_SCORE = 0.9    # score given to an Arabic and a Latin spelling that sound the same
TYPO_SCORE = 0.88       # ... to a rare spelling one wrong / swapped letter away from a common one
TYPO_RARITY = 3         # "rare": used at most a third as often as the common spelling
_ABBREVIATIONS = {"rd": "road", "st": "street", "str": "street", "ave": "avenue", "av": "avenue",
                  "blvd": "boulevard", "hwy": "highway", "dist": "district", "sq": "square", "ext": "extension",
                  "ش": "شارع", "ط": "طريق"}
_WORD = re.compile(r"[^\W\d_]+\.?", re.UNICODE)


def spelling_key(value):
    """normalise(value, True) with common abbreviations written out ("Rd" = "Road", «ش.» = «شارع»)."""
    s = _WORD.sub(lambda m: _ABBREVIATIONS.get(m.group(0).rstrip(".").lower(), m.group(0)), str(value))
    return normalise(s, True)


def one_letter_apart(a, b):
    """True when a and b have the same length and differ by one letter or two swapped neighbours."""
    if len(a) != len(b) or a == b:
        return False
    diff = [i for i in range(len(a)) if a[i] != b[i]]
    if len(diff) == 1:
        return True
    return len(diff) == 2 and diff[1] == diff[0] + 1 and a[diff[0]] == b[diff[1]] and a[diff[1]] == b[diff[0]]
_HEADER_WORDS = {"name", "names", "value", "values", "street", "streets", "district", "الاسم", "اسم", "الاسماء",
                 "الشارع", "الحي", "الاحياء", "الشوارع"}


def _text(value):
    if value is None or (hasattr(value, "isNull") and value.isNull()):
        return None
    s = str(value)
    return None if s.strip() == "" or s == "NULL" else s


def read_reference(path, column=""):
    """Names of a reference list: a text file with one name per line, or a column of a CSV / Excel /
    ODS sheet (by header name, else the first column)."""
    ext = os.path.splitext(path)[1].lower()
    if ext == ".txt":
        raw = open(path, "rb").read()
        for enc in ("utf-8-sig", "utf-16", "cp1256", "latin-1"):
            try:
                lines = raw.decode(enc).splitlines()
                break
            except UnicodeError:
                continue
        names = [ln.strip() for ln in lines if ln.strip()]
        if names and normalise(names[0]) in _HEADER_WORDS:
            names = names[1:]
        return names
    from ..smart_table import read_rows

    rows = [r for r in read_rows(path) if any(_text(c) for c in r)]
    if not rows:
        return []
    col, start = 0, 0
    header = [normalise(_text(c) or "") for c in rows[0]]
    if column and normalise(column) in header:
        col, start = header.index(normalise(column)), 1
    elif header and header[0] in _HEADER_WORDS:
        start = 1
    out = []
    for r in rows[start:]:
        v = _text(r[col]) if col < len(r) else None
        if v is not None:
            v = v.strip()
            if isinstance(r[col], float) and r[col].is_integer():
                v = str(int(r[col]))
            out.append(v)
    return out


class _Seed:
    __slots__ = ("key", "tri", "nums", "skel", "script", "display", "reference", "classes", "total")

    def __init__(self, key, display, reference, raw, total=0):
        self.key = key
        self.total = total
        self.tri = trigrams(key, True)
        self.nums = tuple(re.findall(r"\d+", key))
        self.skel = latin_skeleton(raw)
        self.script = script(raw)
        self.display = display
        self.reference = reference
        self.classes = []      # (class, score, kind)


def group_spellings(counts, threshold=0.85, reference=(), translit=False):
    """Group spellings of one name. counts: {raw value: number of rows}. Returns a list of groups
    [(suggested, in_reference, [(value, count, score, kind)])], best covered first, every value once.
    kind: "suggested", "normalised" (equal once spaces, hamza, tatweel… are unified), "similar",
    "transliteration" (Arabic and Latin spellings that sound the same)."""
    classes = defaultdict(lambda: {"values": Counter(), "total": 0})
    for v, c in counts.items():
        key = spelling_key(v)
        if not key:
            continue
        k = key.replace(" ", "")
        cl = classes[k]
        cl["values"][v] += c
        cl["total"] += c
        cl.setdefault("key", key)
    ordered = sorted(classes.values(), key=lambda cl: (-cl["total"], cl["key"]))

    seeds, postings, by_skel = [], defaultdict(list), defaultdict(list)

    def add_seed(seed):
        n = len(seeds)
        seeds.append(seed)
        for t in seed.tri:
            postings[t].append(n)
        if len(seed.skel) >= 4:
            by_skel[seed.skel].append(n)
        return seed

    ref_keys = {}
    for name in reference:
        key = spelling_key(name)
        if key and key.replace(" ", "") not in ref_keys:
            ref_keys[key.replace(" ", "")] = add_seed(_Seed(key, name, True, name, float("inf")))

    def best_seed(cl, raw):
        key = cl["key"]
        exact = ref_keys.get(key.replace(" ", ""))
        if exact is not None:
            return exact, 1.0, "normalised"
        tri = trigrams(key, True)
        nums = tuple(re.findall(r"\d+", key))
        hits = Counter()
        rare = [t for t in tri if 0 < len(postings.get(t, ())) <= COMMON_TRIGRAM]
        for t in rare or sorted((t for t in tri if t in postings), key=lambda t: len(postings[t]))[:3]:
            hits.update(postings[t])
        best = (None, 0.0, "")
        if len(key.replace(" ", "")) > 2:
            for n, _ in hits.most_common(CANDIDATES):
                s = seeds[n]
                if s.nums != nums:
                    continue
                score = similarity_keys(key, s.key, tri, s.tri)
                kind = "normalised" if score >= 1.0 else "similar"
                if (score < TYPO_SCORE and len(key.replace(" ", "")) >= 5
                        and cl["total"] * TYPO_RARITY <= s.total
                        and one_letter_apart(key.replace(" ", ""), s.key.replace(" ", ""))):
                    score, kind = TYPO_SCORE, "typo"
                if score > best[1]:
                    best = (s, score, kind)
        if translit and best[1] < threshold:
            skel, sc = latin_skeleton(raw), script(raw)
            if len(skel) >= 4 and sc in ("arabic", "latin"):
                for n in by_skel.get(skel, ()):
                    s = seeds[n]
                    if s.script in ("arabic", "latin") and s.script != sc and s.nums == nums:
                        best = (s, TRANSLIT_SCORE, "transliteration")
                        break
        return best

    for cl in ordered:
        raw = cl["values"].most_common(1)[0][0]
        seed, score, kind = best_seed(cl, raw)
        if seed is None or score < threshold:
            seed, score, kind = add_seed(_Seed(cl["key"], raw, False, raw, cl["total"])), 1.0, "normalised"
        seed.classes.append((cl, score, kind))

    groups = []
    for s in seeds:
        if not s.classes:
            continue
        members = []
        for cl, score, kind in s.classes:
            for v, c in cl["values"].most_common():
                if v == s.display:
                    members.append((v, c, 1.0, "suggested"))
                else:
                    members.append((v, c, score, kind))
        groups.append((s.display, s.reference, members))
    groups.sort(key=lambda g: -sum(m[1] for m in g[2]))
    return groups


class SpellValues(BiruniAlgorithm):
    ID = "spellvalues"
    NAME = "Find spelling variants"
    GROUP = "Quality"
    GROUP_ID = "quality"
    HELP = ("Finds the values of a field that are the same name written in different ways - «الملك فهد», "
            "«الملك فهـد», «الملك  فهد», «الملك فهد », hamza and alef forms, ة / ه, ى / ي, Arabic-Indic digits, "
            "Latin capitals and small typos - and groups them. The spelling used most often is suggested for "
            "the whole group, or, with a reference list (a text file with one name per line, or a CSV / Excel "
            "column of official names such as street names), the spelling of the list. Names with different "
            "numbers («شارع 15» / «شارع 16») are never grouped. Optionally Arabic and Latin spellings that sound "
            "the same («الملك فهد» / Al Malik Fahd) are grouped too. The table lists every value of a group with "
            "its count, the suggestion and the similarity (1 = the same once normalised). «Apply» changes the "
            "values in an edit session of the layer: check, then save, or undo (Ctrl+Z) / discard.")

    INPUT, FIELD, THRESHOLD, REFERENCE, REFERENCE_COLUMN, TRANSLIT, APPLY, OUTPUT = (
        "INPUT", "FIELD", "THRESHOLD", "REFERENCE", "REFERENCE_COLUMN", "TRANSLIT", "APPLY", "OUTPUT")

    def flags(self):
        # may edit the project layer itself (edit session, undo stack) - run on the main thread
        return super().flags() | Qgis.ProcessingAlgorithmFlag.NoThreading

    def initAlgorithm(self, config=None):  # noqa: N802
        self.addParameter(QgsProcessingParameterVectorLayer(
            self.INPUT, tr("Layer or table"), [Qgis.ProcessingSourceType.Vector]))
        self.addParameter(QgsProcessingParameterField(
            self.FIELD, tr("Name field"), parentLayerParameterName=self.INPUT,
            type=Qgis.ProcessingFieldParameterDataType.String))
        self.addParameter(QgsProcessingParameterNumber(
            self.THRESHOLD, tr("Lowest similarity accepted (0-1)"), Qgis.ProcessingNumberParameterType.Double, 0.85,
            minValue=0.0, maxValue=1.0))
        self.addParameter(QgsProcessingParameterFile(
            self.REFERENCE, tr("List of correct names (text, CSV or Excel)"), optional=True,
            fileFilter=tr("Lists") + " (*.txt *.csv *.xlsx *.xlsm *.xls *.ods)"))
        self.addParameter(QgsProcessingParameterString(
            self.REFERENCE_COLUMN, tr("Column of the list (header name, empty = first column)"), "", optional=True))
        self.addParameter(QgsProcessingParameterBoolean(
            self.TRANSLIT, tr("Also group Arabic and Latin spellings of the same name"), False))
        self.addParameter(QgsProcessingParameterBoolean(
            self.APPLY, tr("Apply the suggestions to the layer (in an edit session you can undo)"), False))
        self.addParameter(QgsProcessingParameterFeatureSink(
            self.OUTPUT, tr("Spelling variants"), Qgis.ProcessingSourceType.Vector))

    def processAlgorithm(self, parameters, context, feedback):  # noqa: N802
        layer = self.parameterAsVectorLayer(parameters, self.INPUT, context)
        if layer is None:
            raise QgsProcessingException(self.invalidSourceError(parameters, self.INPUT))
        field = self.parameterAsString(parameters, self.FIELD, context)
        idx = layer.fields().lookupField(field)
        if idx < 0:
            raise QgsProcessingException(tr("Choose the name field."))
        threshold = self.parameterAsDouble(parameters, self.THRESHOLD, context)
        ref_path = self.parameterAsFile(parameters, self.REFERENCE, context)
        ref_col = self.parameterAsString(parameters, self.REFERENCE_COLUMN, context) or ""
        translit = self.parameterAsBool(parameters, self.TRANSLIT, context)
        apply = self.parameterAsBool(parameters, self.APPLY, context)

        reference = []
        if ref_path:
            if not os.path.exists(ref_path):
                raise QgsProcessingException(tr("The list of names was not found: {p}").format(p=ref_path))
            reference = read_reference(ref_path, ref_col)
            feedback.pushInfo(tr("{n} names in the list.").format(n=len(reference)))

        feedback.pushInfo(tr("Reading the values…"))
        counts, fids = Counter(), defaultdict(list)
        request = QgsFeatureRequest().setFlags(QgsFeatureRequest.Flag.NoGeometry).setSubsetOfAttributes([idx])
        for f in layer.getFeatures(request):
            if feedback.isCanceled():
                return {}
            v = _text(f.attributes()[idx])
            if v is not None:
                counts[v] += 1
                fids[v].append(f.id())
        feedback.setProgress(20)
        feedback.pushInfo(tr("Grouping {n} different values…").format(n=len(counts)))
        groups = group_spellings(counts, threshold, reference, translit)
        feedback.setProgress(80)

        ref_index = NameIndex(reference) if reference else None

        out_fields = QgsFields()
        for name, t in (("group", QMetaType.Type.Int), ("value", QMetaType.Type.QString),
                        ("count", QMetaType.Type.Int), ("suggested", QMetaType.Type.QString),
                        ("score", QMetaType.Type.Double), ("kind", QMetaType.Type.QString),
                        ("in_reference", QMetaType.Type.Bool), ("note", QMetaType.Type.QString)):
            out_fields.append(QgsField(name, t))
        sink, dest = self.parameterAsSink(parameters, self.OUTPUT, context, out_fields, Qgis.WkbType.NoGeometry)

        replacements, listed, not_in_list, g = {}, 0, 0, 0
        for suggested, in_ref, members in groups:
            variants = [m for m in members if m[0] != suggested]
            if not variants and not (reference and not in_ref):
                continue
            g += 1
            note = None
            if reference and not in_ref:
                not_in_list += 1
                note = tr("Not in the list of names")
                closest = _closest(suggested, ref_index)
                if closest:
                    note += " - " + tr("closest: {n} ({s:.2f})").format(n=closest[0], s=closest[1])
            for value, count, score, kind in members:
                feat = QgsFeature(out_fields)
                feat.setAttributes([g, value, count, suggested, round(score, 4), kind, in_ref, note])
                sink.addFeature(feat, QgsFeatureSink.Flag.FastInsert)
                listed += 1
                if value != suggested and score >= threshold:
                    replacements[value] = suggested

        changed = 0
        if apply and replacements:
            if not layer.isEditable() and not layer.startEditing():
                raise QgsProcessingException(tr("The layer cannot be edited."))
            layer.beginEditCommand(tr("Apply spelling suggestions"))
            for value, new in replacements.items():
                for fid in fids[value]:
                    if layer.changeAttributeValue(fid, idx, new, value):
                        changed += 1
            layer.endEditCommand()
            feedback.pushInfo(tr("{n} values changed in an edit session of «{l}»: check them, then save the edits, "
                                 "or undo (Ctrl+Z) / discard them.").format(n=changed, l=layer.name()))
        feedback.setProgress(100)
        variant_count = sum(1 for _ in replacements)
        feedback.pushInfo(tr("{g} groups with {v} spellings to unify.").format(g=g, v=variant_count))
        return {self.OUTPUT: dest, "GROUPS": g, "VARIANTS": variant_count, "ROWS_LISTED": listed,
                "NOT_IN_LIST": not_in_list, "CHANGED": changed}


def _closest(value, index):
    """(reference name, score) most similar to value, or None."""
    cands = index.candidates(value)
    if not cands or cands[0][0] <= 0.3:
        return None
    score, i, _ = cands[0]
    return index.values[i], score
