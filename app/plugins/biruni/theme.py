"""Light / dark appearance.

A full QPalette makes every QGIS dialog follow the theme, Qt's colour-scheme
hint switches native title bars, and a small application stylesheet modernises
the window chrome (docks, menus, scrollbars, tooltips, status bar).
"""
from qgis.core import QgsSettings
from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtGui import QColor, QGuiApplication, QPalette
from qgis.PyQt.QtWidgets import QApplication

MODES = ("system", "light", "dark")
SETTING = "biruni/theme"

TOKENS = {
    "light": {
        "window": "#F3F5F7", "base": "#FFFFFF", "alt_base": "#F6F8FA", "button": "#FAFBFC",
        "text": "#1E252B", "text_dim": "#5B6670", "text_disabled": "#A3ACB4", "placeholder": "#8A949D",
        "light": "#FFFFFF", "midlight": "#EEF1F4", "mid": "#C9D0D6", "dark": "#A9B2BA", "shadow": "#7D8790",
        "highlight": "#0E7C74", "highlight_text": "#FFFFFF",
        "accent": "#0E7C74", "accent_hover": "#0A635D", "accent_soft": "#DDF0EE", "accent_text": "#FFFFFF",
        "tooltip": "#1E252B", "tooltip_text": "#F3F5F7",
        "border": "#DDE2E7", "panel": "#FFFFFF", "ribbon": "#FFFFFF",
        "hover": "#EDF2F5", "pressed": "#E0E7EC", "icon": "#33414C", "icon_disabled": "#B5BDC4",
        "caption": "#7B8791", "scroll": "#C5CDD4", "scroll_hover": "#A7B1BA", "menu": "#FFFFFF",
        "start_bg": "#F3F5F7", "card": "#FFFFFF", "card_hover": "#F4FAF9", "amber": "#D97706",
    },
    "dark": {
        "window": "#1B1F24", "base": "#14171B", "alt_base": "#191D21", "button": "#262B31",
        "text": "#E4E8EC", "text_dim": "#A2ACB6", "text_disabled": "#5E6770", "placeholder": "#6F7983",
        "light": "#3A4047", "midlight": "#31363C", "mid": "#23272C", "dark": "#15181B", "shadow": "#0B0D0F",
        "highlight": "#13968B", "highlight_text": "#FFFFFF",
        "accent": "#2CC5B4", "accent_hover": "#4FD6C7", "accent_soft": "#173A3B", "accent_text": "#0B1F1D",
        "tooltip": "#E4E8EC", "tooltip_text": "#14171B",
        "border": "#2E343B", "panel": "#20252B", "ribbon": "#20252B",
        "hover": "#2B3138", "pressed": "#353C44", "icon": "#D3D9DF", "icon_disabled": "#5A636C",
        "caption": "#8892A0", "scroll": "#3B434B", "scroll_hover": "#505A64", "menu": "#22272D",
        "start_bg": "#16191D", "card": "#20252B", "card_hover": "#252C33", "amber": "#F59E0B",
    },
}


def app_qss(t):
    """Global chrome only - widget colours come from the palette so QGIS dialogs
    and custom widgets keep their native behaviour."""
    return f"""
QMainWindow::separator {{ background: {t['border']}; width: 1px; height: 1px; }}
QMainWindow::separator:hover {{ background: {t['accent']}; }}
QDockWidget {{ font-weight: 600; }}
QDockWidget::title {{ background: {t['panel']}; padding: 6px 8px; border-bottom: 1px solid {t['border']}; }}
QStatusBar {{ background: {t['panel']}; border-top: 1px solid {t['border']}; }}
QStatusBar::item {{ border: none; }}
QMenu {{ background: {t['menu']}; border: 1px solid {t['border']}; padding: 4px; }}
QMenu::item {{ padding: 6px 28px 6px 10px; border-radius: 4px; }}
QMenu::item:selected {{ background: {t['hover']}; color: {t['text']}; }}
QMenu::item:disabled {{ color: {t['text_disabled']}; }}
QMenu::separator {{ height: 1px; background: {t['border']}; margin: 4px 8px; }}
QToolTip {{ background: {t['tooltip']}; color: {t['tooltip_text']}; border: 1px solid {t['tooltip']}; padding: 4px 6px; }}
QScrollBar:vertical {{ background: transparent; width: 11px; margin: 0; }}
QScrollBar:horizontal {{ background: transparent; height: 11px; margin: 0; }}
QScrollBar::handle:vertical {{ background: {t['scroll']}; min-height: 28px; border-radius: 4px; margin: 2px; }}
QScrollBar::handle:horizontal {{ background: {t['scroll']}; min-width: 28px; border-radius: 4px; margin: 2px; }}
QScrollBar::handle:hover {{ background: {t['scroll_hover']}; }}
QScrollBar::add-line, QScrollBar::sub-line {{ width: 0; height: 0; }}
QScrollBar::add-page, QScrollBar::sub-page {{ background: transparent; }}
"""


def system_is_dark():
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                            r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize") as k:
            return winreg.QueryValueEx(k, "AppsUseLightTheme")[0] == 0
    except OSError:
        return False


def build_palette(t):
    pal = QPalette()
    R = QPalette.ColorRole
    G = QPalette.ColorGroup

    def put(role, color, disabled=None):
        pal.setColor(G.All, role, QColor(color))
        if disabled:
            pal.setColor(G.Disabled, role, QColor(disabled))

    put(R.Window, t["window"])
    put(R.WindowText, t["text"], t["text_disabled"])
    put(R.Base, t["base"], t["window"])
    put(R.AlternateBase, t["alt_base"])
    put(R.Text, t["text"], t["text_disabled"])
    put(R.Button, t["button"])
    put(R.ButtonText, t["text"], t["text_disabled"])
    put(R.BrightText, "#FFFFFF")
    put(R.Light, t["light"])
    put(R.Midlight, t["midlight"])
    put(R.Mid, t["mid"])
    put(R.Dark, t["dark"])
    put(R.Shadow, t["shadow"])
    put(R.Highlight, t["highlight"], t["mid"])
    put(R.HighlightedText, t["highlight_text"], t["text_disabled"])
    put(R.Link, t["accent"])
    put(R.LinkVisited, t["accent"])
    put(R.ToolTipBase, t["tooltip"])
    put(R.ToolTipText, t["tooltip_text"])
    put(R.PlaceholderText, t["placeholder"])
    if hasattr(R, "Accent"):
        put(R.Accent, t["accent"])
    return pal


_ROLES = [r for r in QPalette.ColorRole if r.name not in ("NoRole", "NColorRoles")]
_GROUPS = (QPalette.ColorGroup.Active, QPalette.ColorGroup.Inactive, QPalette.ColorGroup.Disabled)


def migrate_widget_palettes(old, new):
    """Many QGIS widgets copy the application palette into their own (explicit)
    palette when created, so they would not follow a theme switch. Move every
    colour that still equals the previous theme's colour to the new one, and
    leave intentionally customised colours alone."""
    for w in QApplication.allWidgets():
        if not w.testAttribute(Qt.WidgetAttribute.WA_SetPalette):
            continue
        pal = w.palette()
        changed = False
        for g in _GROUPS:
            for role in _ROLES:
                c = pal.color(g, role)
                if c == old.color(g, role) and c != new.color(g, role):
                    pal.setColor(g, role, new.color(g, role))
                    changed = True
        if changed:
            w.setPalette(pal)


class ThemeManager:
    def __init__(self, main_window):
        self.mw = main_window
        self._listeners = []

    # -- state ---------------------------------------------------------
    def mode(self):
        m = QgsSettings().value(SETTING, "system")
        return m if m in MODES else "system"

    def is_dark(self):
        m = self.mode()
        return m == "dark" or (m == "system" and system_is_dark())

    def tokens(self):
        return TOKENS["dark" if self.is_dark() else "light"]

    def on_change(self, fn):
        self._listeners.append(fn)

    # -- apply ---------------------------------------------------------
    def apply(self):
        dark = self.is_dark()
        t = self.tokens()
        app = QApplication.instance()
        hints = QGuiApplication.styleHints()
        if hasattr(hints, "setColorScheme"):
            hints.setColorScheme(Qt.ColorScheme.Dark if dark else Qt.ColorScheme.Light)
        old = app.palette()
        new = build_palette(t)
        app.setPalette(new)
        migrate_widget_palettes(old, new)
        # Setting the application stylesheet re-polishes every widget, which is
        # what makes stylesheet-styled QGIS panels pick up the new palette.
        app.setStyleSheet(app_qss(t))
        ss = self.mw.styleSheet()
        marker = "/* biruni-theme */"
        self.mw.setStyleSheet(ss.replace(marker, "") if marker in ss else ss + marker)
        for fn in list(self._listeners):
            fn()

    def set_mode(self, mode):
        QgsSettings().setValue(SETTING, mode)
        self.apply()
