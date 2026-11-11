"""National and closed file formats: one module per format, registered here.

Each format module defines ID, LABEL, COUNTRIES and read(path) -> common.Dataset and/or write(...).
The registry answers two questions for the rest of the program:

* detect(path) - which reader opens this file (by extension, then by content);
* for_country(cc) - the formats a country uses, for the region packs:
  [{"id", "label", "extensions", "read", "write"}].

Readers are pure Python + GDAL/OGR (no QGIS), so they also run in scripts and tests."""
import importlib
import os

_MODULES = ("ncz", "sxf", "cxf", "sima", "cadastral_xml", "dgn", "sigef_ods", "catastro_gml")


def _load(name):
    return importlib.import_module(f"{__name__}.{name}")


def formats():
    out = []
    for name in _MODULES:
        m = _load(name)
        out.append({"id": m.ID, "label": m.LABEL, "extensions": tuple(getattr(m, "EXTENSIONS", ())),
                    "countries": tuple(getattr(m, "COUNTRIES", ())),
                    "read": getattr(m, "read", None), "write": getattr(m, "write", None)})
    return out


def readable():
    return [f for f in formats() if f["read"] and f["extensions"]]


def for_country(cc):
    cc = (cc or "").upper()
    return [f for f in formats() if cc in f["countries"]]


def file_filter():
    """Qt file-dialog filter for every readable format."""
    parts = [f"{f['label']} ({' '.join('*' + e for e in f['extensions'])})" for f in readable()]
    every = " ".join(sorted({"*" + e for f in readable() for e in f["extensions"]}))
    return ";;".join([f"National formats ({every})"] + parts)


def detect(path):
    """The format id that should read path, or None."""
    ext = os.path.splitext(path)[1].lower()
    if ext in (".xml", ".in4"):
        from . import cadastral_xml

        return "cadastral_xml" if cadastral_xml.detect(path) else None
    for f in readable():
        if ext in f["extensions"]:
            return f["id"]
    try:
        with open(path, "rb") as f:
            head = f.read(64)
    except OSError:
        return None
    if head.lstrip().upper().startswith(b"MAPPA"):
        return "cxf"
    if head.startswith((b"G00,", b"Z00,", b"A00,")):
        return "sima"
    return None


def read(path, fmt=None):
    """Read path with the given or detected format; raises common.FormatError with a user message."""
    from .common import FormatError

    fmt = fmt or detect(path)
    if fmt is None:
        raise FormatError("This file type is not one of the national formats Biruni reads.")
    entry = next((f for f in formats() if f["id"] == fmt), None)
    if entry is None or entry["read"] is None:
        raise FormatError(f"No reader for {fmt}.")
    return entry["read"](path)
