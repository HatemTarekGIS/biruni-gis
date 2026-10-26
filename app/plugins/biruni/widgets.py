"""Shared look for Biruni panels and dialogs: cards, primary buttons, hints.

Give the panel's root widget an objectName and call ``panel_qss(tokens, name)``
from its refresh_theme(); then use the objectNames below on child widgets."""
from qgis.PyQt.QtWidgets import QFrame, QLabel, QVBoxLayout


def panel_qss(t, root):
    r = f"#{root}"
    return f"""
{r} QFrame#Card {{ background: {t['panel']}; border: 1px solid {t['border']}; border-radius: 8px; }}
{r} QLabel#Hint {{ color: {t['text_dim']}; }}
{r} QLabel#Key {{ color: {t['text_dim']}; }}
{r} QLabel#Value {{ font-weight: 600; color: {t['text']}; }}
{r} QLabel#Big {{ font-size: 12pt; font-weight: 700; color: {t['accent']}; }}
{r} QLabel#Title {{ font-size: 11pt; font-weight: 700; color: {t['text']}; }}
{r} QLabel#Error {{ color: #DC2626; }}
{r} QPushButton#Primary {{ background: {t['accent']}; color: {t['accent_text']}; border: none;
    border-radius: 6px; padding: 7px 14px; font-weight: 600; }}
{r} QPushButton#Primary:hover {{ background: {t['accent_hover']}; }}
{r} QPushButton#Primary:disabled {{ background: {t['mid']}; color: {t['text_disabled']}; }}
{r} QToolButton#Flat {{ border: none; padding: 3px; border-radius: 4px; }}
{r} QToolButton#Flat:hover {{ background: {t['hover']}; }}
"""


def card(parent_layout=None):
    f = QFrame()
    f.setObjectName("Card")
    lay = QVBoxLayout(f)
    lay.setContentsMargins(10, 8, 10, 8)
    lay.setSpacing(6)
    if parent_layout is not None:
        parent_layout.addWidget(f)
    return f, lay


def hint(text):
    lab = QLabel(text)
    lab.setObjectName("Hint")
    lab.setWordWrap(True)
    return lab


def title(text):
    lab = QLabel(text)
    lab.setObjectName("Title")
    return lab
