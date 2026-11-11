"""The Layers panel's own buttons: recognised (in whatever language the engine runs) and given our icons and
plain-language tooltips. The buttons keep doing exactly what they did; only their look and tip change."""
import re

from qgis.PyQt.QtCore import QCoreApplication
from qgis.PyQt.QtWidgets import QToolButton

from ..i18n import tr

# the buttons in the order the panel shows them
_BUTTON_ORDER = ("style", "add_group", "themes", "filter_map", "filter_expr", "expand", "collapse", "remove")
# the engine's English source texts of each button (also matched translated, through its own translations)
_SOURCE_TEXTS = {
    "style": ("Layer Styling", "Open the Layer Styling panel"),
    "add_group": ("Add Group",),
    "themes": ("Manage Map Themes",),
    "filter_map": ("Filter Legend by Map Content", "Filter Legend"),
    "expand": ("Expand All",),
    "collapse": ("Collapse All",),
    "remove": ("Remove Layer/Group",),
}
_ICON_FILES = {"style": "style", "add_group": "add-group", "themes": "themes", "filter_map": "filter-map",
               "filter_expr": "filter-expr", "expand": "expand", "collapse": "collapse", "remove": "remove",
               "solo": "solo", "arrange": "arrange", "arrange_undo": "arrange-undo"}
OWN_PREFIX = "biruniLayers"  # objectName prefix of the buttons we add (never treated as the panel's own)


def _clean(text):
    text = (text or "").replace("&", "").replace("…", "").replace("...", "").strip()
    return re.sub(r"\s*\([^)]*\)$", "", text)  # "Layer Styling (F7)" -> "Layer Styling"


def _known(key):
    out = set()
    for src in _SOURCE_TEXTS.get(key, ()):
        out.add(_clean(src))
        out.add(_clean(QCoreApplication.translate("QgisApp", src)))
    return out


def tip(key):
    """Rich tooltip: what the button is, then one line on what it does."""
    title, line = {
        "style": (tr("Layer styling") + " (F7)", tr("Change the colours, symbols and labels of the selected layer.")),
        "add_group": (tr("New group"), tr("Add an empty group to keep related layers together.")),
        "themes": (tr("Map themes"), tr("Save the layers you are showing now as a theme, and switch between themes.")),
        "filter_map": (tr("Legend: only what is on the map"),
                       tr("Show in the legend only the symbols that appear in the current map view.")),
        "filter_expr": (tr("Legend filter by expression"),
                        tr("Show in the legend only the symbols of the features that match an expression.")),
        "expand": (tr("Expand all"), tr("Open every group and every legend.")),
        "collapse": (tr("Collapse all"), tr("Close every group and every legend.")),
        "remove": (tr("Remove layer or group"),
                   tr("Take the selected layers or groups off the map. The files themselves are not deleted.")),
        "solo": (tr("Solo peek"), tr("Show only the selected layers on the map; click again to bring the others "
                                     "back exactly as they were. Tip: Alt+click a layer.")),
        "arrange": (tr("Smart arrange"), tr("Group the layers by kind so points draw above lines, lines above "
                                            "areas and areas above images. Shows a preview first.")),
        "arrange_undo": (tr("Undo arrange"), tr("Bring back the groups and order from before Smart arrange.")),
    }[key]
    return f"<b>{title}</b><br>{line}"


def icon_for(key, color):
    from .. import icons

    return icons.icon("layers/" + _ICON_FILES[key], color)


def identify(toolbar):
    """{key: (QAction, QWidget|None)} for the panel's buttons. The widget is set for buttons that are widgets
    (map themes, legend filter by expression); plain actions show through the button the toolbar made for them."""
    found = {}
    items = []
    for act in toolbar.actions():
        if act.isSeparator() or act.objectName().startswith(OWN_PREFIX):
            continue
        w = toolbar.widgetForAction(act)
        widget = w if (w is not None and not (isinstance(w, QToolButton) and w.defaultAction() is act)) else None
        items.append((act, widget))
    for act, widget in items:
        key = None
        texts = {_clean(act.text()), _clean(act.toolTip())}
        if widget is not None:
            texts |= {_clean(widget.toolTip()), _clean(getattr(widget, "text", lambda: "")())}
            if widget.metaObject().className() == "QgsLegendFilterButton":
                key = "filter_expr"
        if key is None:
            if act.objectName() in ("mActionRemoveLayer",):
                key = "remove"
            elif act.objectName() == "mActionStyleDock" or act.shortcut().toString() == "F7":
                key = "style"
            else:
                key = next((k for k in _SOURCE_TEXTS if texts & _known(k)), None)
        if key is None and isinstance(widget, QToolButton) and widget.menu() is not None:
            key = "themes"
        if key is not None and key not in found:
            found[key] = (act, widget)
    if len(found) < len(_BUTTON_ORDER) and len(items) == len(_BUTTON_ORDER):  # unknown texts: the usual order
        taken = {id(a) for a, _w in found.values()}
        for key, (act, widget) in zip(_BUTTON_ORDER, items):
            if key not in found and id(act) not in taken:
                found[key] = (act, widget)
    return found


class ToolbarLook:
    """Applies our icons and tooltips to the panel's buttons and puts the old ones back on teardown."""

    def __init__(self, toolbar):
        self.toolbar = toolbar
        self.buttons = identify(toolbar)
        self.backup = []  # (object, icon, tooltip)

    def apply(self, color):
        first = not self.backup
        for key, (act, widget) in self.buttons.items():
            target = widget if widget is not None else act
            try:
                if first:
                    self.backup.append((target, target.icon(), target.toolTip()))
                target.setIcon(icon_for(key, color))
                target.setToolTip(tip(key))
            except RuntimeError:  # the button was deleted by the engine
                pass

    def restore(self):
        for target, ic, old_tip in self.backup:
            try:
                target.setIcon(ic)
                target.setToolTip(old_tip)
            except RuntimeError:
                pass
        self.backup = []
