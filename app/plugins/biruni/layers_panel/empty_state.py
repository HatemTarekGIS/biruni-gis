"""The card shown in the Layers panel while the map has no layers: a friendly line, Add data, Open the Vault and a
hint that files can simply be dropped on the window."""
from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtWidgets import QFrame, QLabel, QPushButton, QVBoxLayout

from ..i18n import tr

_DEFAULT_TOKENS = {"text": "#1E252B", "text_dim": "#5B6670", "accent": "#0E7C74", "accent_text": "#FFFFFF",
                   "accent_hover": "#0A635D", "border": "#DDE2E7", "card": "#FFFFFF", "hover": "#EDF2F5",
                   "icon": "#33414C"}


class EmptyState(QFrame):
    def __init__(self, on_add, on_vault, parent=None):
        super().__init__(parent)
        self.setObjectName("biruniLayersEmpty")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(16, 22, 16, 16)
        lay.setSpacing(10)
        lay.addStretch(1)
        self.picture = QLabel(alignment=Qt.AlignmentFlag.AlignCenter)
        lay.addWidget(self.picture)
        self.title = QLabel(tr("Your map is empty - let's fill it!"), alignment=Qt.AlignmentFlag.AlignCenter)
        self.title.setObjectName("biruniLayersEmptyTitle")
        self.title.setWordWrap(True)
        lay.addWidget(self.title)
        self.text = QLabel(tr("Add a file, a database or a web map, or pick something you already have in the "
                              "Vault."), alignment=Qt.AlignmentFlag.AlignCenter)
        self.text.setWordWrap(True)
        lay.addWidget(self.text)
        self.add_button = QPushButton(tr("Add data"))
        self.add_button.setObjectName("biruniLayersEmptyAdd")
        self.add_button.clicked.connect(lambda: on_add())
        lay.addWidget(self.add_button)
        self.vault_button = QPushButton(tr("Open the Vault"))
        self.vault_button.setObjectName("biruniLayersEmptyVault")
        self.vault_button.clicked.connect(lambda: on_vault())
        lay.addWidget(self.vault_button)
        self.hint = QLabel(tr("Tip: you can also drag files from a folder and drop them here."),
                           alignment=Qt.AlignmentFlag.AlignCenter)
        self.hint.setObjectName("biruniLayersEmptyHint")
        self.hint.setWordWrap(True)
        lay.addWidget(self.hint)
        lay.addStretch(2)
        self.refresh_theme(None)

    def refresh_theme(self, tokens):
        t = dict(_DEFAULT_TOKENS, **(tokens or {}))
        from .. import icons

        self.picture.setPixmap(icons.icon("layers/empty-map", t["accent"]).pixmap(56, 56))
        for b in (self.add_button, self.vault_button):
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            b.setMinimumHeight(30)
        self.setStyleSheet(
            f"#biruniLayersEmptyTitle {{ font-size: 11pt; font-weight: 600; color: {t['text']}; }}"
            f"#biruniLayersEmpty QLabel {{ color: {t['text_dim']}; }}"
            f"#biruniLayersEmpty #biruniLayersEmptyTitle {{ color: {t['text']}; }}"
            f"#biruniLayersEmptyAdd {{ background: {t['accent']}; color: {t['accent_text']}; border: none;"
            f" border-radius: 6px; padding: 5px 14px; font-weight: 600; }}"
            f"#biruniLayersEmptyAdd:hover {{ background: {t['accent_hover']}; }}"
            f"#biruniLayersEmptyVault {{ background: transparent; color: {t['accent']};"
            f" border: 1px solid {t['border']}; border-radius: 6px; padding: 5px 14px; }}"
            f"#biruniLayersEmptyVault:hover {{ background: {t['hover']}; }}"
            f"#biruniLayersEmptyHint {{ font-size: 8pt; }}")
