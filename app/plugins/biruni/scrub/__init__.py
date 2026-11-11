"""Clean what leaves the office (the first piece of «غرفة التعقيم»): see core.py and SPEC.md.

after_export(paths) is the one hook the exporters call after they write a file; open_dialog(plugin) cleans any file
by hand ("Clean a File for Sending…")."""
import os

from ..i18n import tr
from .core import Receipt, clean_text, scrub_file, supported  # noqa: F401

SETTING = "biruni/scrub/outgoing"          # clean files that leave the office (default on)
RECEIPT_SETTING = "biruni/scrub/receipt"   # also write <name>.scrub.txt beside the file (default off)


def _settings():
    try:
        from qgis.core import QgsSettings
        return QgsSettings()
    except Exception:  # noqa: BLE001
        return None


def enabled():
    s = _settings()
    if s is None:
        return True
    return str(s.value(SETTING, "true")).lower() not in ("false", "0", "no")


def set_enabled(on):
    s = _settings()
    if s is not None:
        s.setValue(SETTING, "true" if on else "false")


def write_receipt_files():
    s = _settings()
    return s is not None and str(s.value(RECEIPT_SETTING, "false")).lower() in ("true", "1", "yes")


def hidden_fields(layer):
    """The fields hidden in a layer's attribute form: they never leave the office."""
    out = []
    try:
        for i, f in enumerate(layer.fields()):
            if layer.editorWidgetSetup(i).type() == "Hidden":
                out.append(f.name())
    except Exception:  # noqa: BLE001
        pass
    return out


def visible_indexes(layer):
    """Field indexes to export: all but the hidden ones (QgsVectorFileWriter.SaveVectorOptions.attributes)."""
    hid = set(hidden_fields(layer))
    return [i for i, f in enumerate(layer.fields()) if f.name() not in hid]


def after_export(paths, feedback=None, options=None):
    """Clean files an exporter has just written (when the setting is on). -> [Receipt]"""
    if not enabled():
        return []
    out = []
    for p in paths or ():
        if not p or not os.path.exists(p) or not supported(p):
            continue
        try:
            rc = scrub_file(p, options=options)
        except Exception as e:  # noqa: BLE001 - cleaning never breaks an export
            if feedback is not None:
                feedback.reportError(tr("Could not clean {name}: {e}").format(name=os.path.basename(p), e=e))
            continue
        out.append(rc)
        if rc.removed and feedback is not None:
            feedback.pushInfo(tr("Cleaned {name}: {n} leftovers removed").format(name=os.path.basename(p), n=rc.removed))
        if write_receipt_files():
            _write_receipt(rc)
    return out


def _write_receipt(rc):
    try:
        with open(rc.path + ".scrub.txt", "w", encoding="utf-8") as fh:
            fh.write(rc.as_text() + "\n")
    except OSError:
        pass


def open_dialog(plugin):
    """Clean a file for sending: choose it, a clean copy is written beside it, and the receipt is shown."""
    from qgis.PyQt.QtWidgets import QCheckBox, QFileDialog, QMessageBox

    mw = plugin.iface.mainWindow() if plugin is not None and getattr(plugin, "iface", None) else None
    flt = tr("Files Biruni can clean") + " (*.gpkg *.shp *.dxf *.kml *.kmz *.xlsx *.csv *.pdf *.geojson *.json)"
    path, _f = QFileDialog.getOpenFileName(mw, tr("Clean a File for Sending"), "", flt)
    if not path:
        return None
    stem, ext = os.path.splitext(path)
    out = stem + "_clean" + ext
    try:
        rc = scrub_file(path, out)
    except Exception as e:  # noqa: BLE001
        QMessageBox.warning(mw, tr("Clean a File for Sending"), tr("The file could not be cleaned: {e}").format(e=e))
        return None
    box = QMessageBox(mw)
    box.setWindowTitle(tr("Clean a File for Sending"))
    if rc.removed:
        box.setText(tr("A clean copy was written: {name}\n{n} leftovers were removed.").format(
            name=os.path.basename(out), n=rc.removed))
    else:
        box.setText(tr("A clean copy was written: {name}\nNothing needed removing.").format(name=os.path.basename(out)))
    box.setDetailedText(rc.as_text())
    cb = QCheckBox(tr("Also save the receipt beside the file"))
    box.setCheckBox(cb)
    box.exec()
    if cb.isChecked():
        _write_receipt(rc)
    return rc
