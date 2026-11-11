"""A copy with some fields (or features) taken out that still proves itself.

The certificate seals every value of every feature on its own (core.seal), with a salt made from the full data:
whoever has the full data makes the salts, whoever has only part of it cannot. A shared copy is an ordinary
GeoPackage with the fields to hide left out, and in it the salts of the values it shows and the seals of the
values it does not. Whoever receives it rebuilds every seal from what it shows, and the root of them is the one
in the certificate's code: every shape and every value shown is as certified, none added, none taken out (the
features held back are counted, as seals). A value held back cannot be guessed from its seal, even when it is one
of two (yes or no): its salt is not in the copy.
"""
import datetime as dt
import hashlib
import json
import os
import sqlite3

from qgis.core import (QgsCoordinateTransformContext, QgsFeature, QgsFeatureRequest, QgsFields, QgsGeometry,
                       QgsVectorFileWriter, QgsVectorLayer)

from . import core

KIND = "biruni-shared"
TABLE = "biruni_shared"


class ShareError(Exception):
    pass


def _key(sh, shown):
    return hashlib.sha256(core._j([sh, shown])).hexdigest()[:24]


def share(layer, hide, cert=None, index=0, only=None, feedback=None):
    """What the shared copy of ``layer`` holds besides its features: (document, fids to write, fields shown).
    ``hide``: the fields left out; ``only``: the fids shown (the others are counted, not shown). With ``cert`` the
    layer must still be what that certificate says (its layer ``index``)."""
    if cert is not None:
        entry = cert["layers"][index]
        if not entry.get("commit"):
            raise core.FingerprintError("old certificate")
        t = core.tree_for(layer, entry, feedback)
        if t.root != entry["root"] or t.commit != entry["commit"] or t.reach != entry.get("reach"):
            raise core.FingerprintError("changed")
        layers = [core.code_part(e) for e in cert["layers"]]
        made, name = cert.get("made"), entry["name"]
    else:
        t = core.build(layer, feedback=feedback, commit=True, sums=True)     # as a certificate is made
        name, index, made = layer.name(), 0, None
        layers = [core.code_part(core.certificate([(name, t)])["layers"][0])]
    hide = {h.lower() for h in hide}
    if hide - set(t.fields):
        raise core.FingerprintError("fields missing: " + ", ".join(sorted(hide - set(t.fields))))
    shown = [n for n in t.fields if n not in hide]
    only = None if only is None else set(only)
    items, keep = [], []
    for fid, (sh, vals, seal) in t.rows.items():
        if only is not None and fid not in only:
            items.append({"f": seal})
            continue
        sv = [[n, v] for n, v in vals if n not in hide]
        items.append({"k": _key(sh, sv), "s": {n: core.field_salt(t.seed, sh, n) for n, _v in sv},
                      "c": {n: core.field_commit(core.field_salt(t.seed, sh, n), n, v) for n, v in vals if n in hide}})
        keep.append(fid)
    items.sort(key=lambda i: json.dumps(i, sort_keys=True))     # nothing told by where a feature was in the file
    doc = {"format": core.FORMAT, "kind": KIND,
           "made": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
           "layer": name, "settings": {"step": t.step, "crs": t.crs, "fields": t.fields},
           "shown": shown, "hidden": sorted(hide), "features": t.count, "withheld": t.count - len(keep),
           "certificate": {"code": core.code(core.layer_codes(layers)), "made": made, "layers": layers,
                           "index": index},
           "items": items}
    return doc, keep, shown


def write(layer, doc, keep, shown, path):
    """The shared copy: a GeoPackage of the features in ``keep`` with the fields ``shown``, the document in it."""
    names = set(shown)
    fields = QgsFields()
    idx = []
    for i, f in enumerate(layer.fields()):
        if f.name().lower() in names:
            fields.append(f)
            idx.append(i)
    if os.path.exists(path):
        os.remove(path)
    opts = QgsVectorFileWriter.SaveVectorOptions()
    opts.driverName = "GPKG"
    opts.layerName = doc["layer"]
    opts.fileEncoding = "UTF-8"
    w = QgsVectorFileWriter.create(path, fields, layer.wkbType(), layer.crs(), QgsCoordinateTransformContext(), opts)
    if w.hasError() != QgsVectorFileWriter.WriterError.NoError:
        raise ShareError(w.errorMessage())
    for f in layer.getFeatures(QgsFeatureRequest().setFilterFids(keep)):
        nf = QgsFeature(fields)
        if f.hasGeometry():
            nf.setGeometry(QgsGeometry(f.geometry()))
        nf.setAttributes([f.attribute(i) for i in idx])
        w.addFeature(nf)
    del w
    now = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    con = sqlite3.connect(path, timeout=10)
    try:
        with con:
            con.execute(f"CREATE TABLE IF NOT EXISTS {TABLE} (layer TEXT PRIMARY KEY NOT NULL, doc TEXT NOT NULL)")
            con.execute(f"INSERT OR REPLACE INTO {TABLE} (layer, doc) VALUES (?, ?)",
                        (doc["layer"], json.dumps(doc, ensure_ascii=False, separators=(",", ":"))))
            con.execute("INSERT OR REPLACE INTO gpkg_contents (table_name, data_type, identifier, description, "
                        "last_change) VALUES (?, 'attributes', ?, ?, ?)",
                        (TABLE, TABLE, "Biruni: the seals that prove this copy against its certificate", now))
    finally:
        con.close()
    return path


def read(path):
    """[(layer name, document)] of a shared copy; [] when it is not one."""
    try:
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)
        try:
            rows = con.execute(f"SELECT layer, doc FROM {TABLE}").fetchall()
        finally:
            con.close()
    except sqlite3.Error:
        return []
    out = []
    for name, raw in rows:
        try:
            out.append((name, json.loads(raw)))
        except ValueError:
            pass
    return out


class Result:
    def __init__(self):
        self.ok = False
        self.why = ""
        self.code = ""
        self.layer = None
        self.doc = {}
        self.shown = 0           # features in the copy
        self.withheld = 0        # features held back (counted by their seals)
        self.bad = []            # fids of the copy not as certified (changed or added)
        self.missing = 0         # certified features shown in the certificate, not in the copy


def verify(path, layer=None):
    """Check a shared copy from itself alone. Result.ok: what it shows is what the certificate of Result.code sealed,
    all of it, with Result.withheld features held back."""
    r = Result()
    docs = read(path)
    if not docs:
        r.why = "format"
        return r
    name, doc = docs[0]
    r.doc = doc
    lyr = layer or QgsVectorLayer(f"{path}|layername={name}", name, "ogr")
    r.layer = lyr
    try:
        if doc.get("kind") != KIND or not lyr.isValid():
            raise ShareError("format")
        cert = doc["certificate"]
        r.code = core.code(core.layer_codes(cert["layers"]))
        if r.code != cert["code"]:
            raise ShareError("code")
        root = cert["layers"][cert["index"]].get("commit")
        if not root:
            raise ShareError("format")
        st = doc["settings"]
        step, shown, hidden = float(st["step"]), list(doc["shown"]), set(doc["hidden"])
        if set(shown) & hidden or set(shown) | hidden != set(st["fields"]) or \
                set(core.field_names(lyr)) != set(shown):
            raise ShareError("fields")
        if (lyr.crs().authid() or lyr.crs().toWkt()) != st["crs"]:
            raise ShareError("crs")
        order = [n for n in st["fields"] if n in set(shown)]
        lower = {f.name().lower(): i for i, f in enumerate(lyr.fields())}
        by_key, seals = {}, []
        for it in doc["items"]:
            if "f" in it:
                seals.append(it["f"])
                r.withheld += 1
            else:
                by_key.setdefault(it["k"], []).append(it)
        for f in lyr.getFeatures():
            sk = core.shape_key(f.geometry(), step)
            sh = core.shape_hash(sk)
            sv = [[n, core._value(f.attribute(lower[n]))] for n in order]
            lst = by_key.get(_key(sh, sv))
            if not lst:
                r.bad.append(f.id())
                continue
            it = lst.pop()
            c = {n: core.field_commit(it["s"][n], n, v) for n, v in sv}
            c.update(it["c"])
            seals.append(core.field_item(sh, c))
            r.shown += 1
        r.missing = sum(len(v) for v in by_key.values())
        if r.bad or r.missing:
            raise ShareError("changed")
        if core.field_root(seals) != root:
            raise ShareError("seals")
        r.ok = True
    except ShareError as e:
        r.why = str(e)
    except (KeyError, TypeError, ValueError, IndexError):
        r.why = "format"
    return r
