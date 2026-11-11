"""One running share of this PC's dashboards: the viewers' hub, the main-thread gateway to the
DashboardService, the LAN server and, when asked, the hidden host page for viewers from anywhere.
The dock owns one; the tests drive it without any window."""
import os
import secrets

from .server import DASH_PORT, PREVIEW, Gateway, LanServer, SessionHub


def cert_folder():
    from qgis.core import QgsApplication

    return os.path.join(QgsApplication.qgisSettingsDirPath(), "biruni")  # the phone tools' certificate


class Sharing:
    def __init__(self, service, lang="en", addresses=None, port=DASH_PORT, bind="0.0.0.0", folder=None):
        self.service = service
        self.lang = lang
        self._addresses = addresses
        self.port = port
        self.bind = bind
        self.folder = folder
        self.hub = self.gateway = self.lan = self.anywhere = None

    def addresses(self):
        if self._addresses is not None:
            return list(self._addresses)
        from ..gps.source import local_addresses

        return local_addresses()

    def start(self):
        """The LAN server (always: the hidden host page reaches the dashboards through it). OSError when no
        port is free."""
        if self.lan is not None:
            return
        from .page import page_html
        from .host import host_page

        self.hub = SessionHub()
        self.gateway = Gateway(self.hub, self.service)
        try:
            self.lan = LanServer(self.hub, lambda: _bytes(page_html(self.lang, "lan")), self.folder or cert_folder(),
                                 self.addresses(), port=self.port, bind=self.bind, host_page_fn=host_page)
        except Exception:
            self.gateway.stop()
            self.gateway = self.hub = None
            raise

    def start_anywhere(self):
        self.start()
        if self.anywhere is None or not self.anywhere.running():
            from .host import Anywhere

            self.anywhere = Anywhere(self.lan)

    def stop_anywhere(self):
        if self.anywhere is not None:
            self.anywhere.stop()
            self.anywhere = None

    def running(self):
        return self.lan is not None

    def links(self, card, tv=False):
        """{"lan": [address per local IP], "anywhere": address or None} for one card."""
        out = {"lan": [], "anywhere": None}
        if self.lan is not None:
            out["lan"] = [self.lan.url(ip, card, self.lang, tv) for ip in self.addresses()]
        if self.anywhere is not None and self.anywhere.running():
            out["anywhere"] = self.anywhere.url(card, self.lang, tv)
        return out

    def local_link(self, card, tv=True):
        """For this PC's own browser (a big screen in the office)."""
        return self.lan.url("127.0.0.1", card, self.lang, tv) if self.lan is not None else None

    # ---- the builder's live previews (the page on this PC's browser or on a phone, fed the unsaved definition) ----
    def new_preview(self, definition):
        self.start()
        key = secrets.token_urlsafe(18)
        self.gateway.set_preview(key, definition)
        return key

    def update_preview(self, key, definition):
        if self.gateway is not None:
            self.gateway.set_preview(key, definition)

    def end_preview(self, key):
        if self.gateway is not None:
            self.gateway.end_preview(key)

    def preview_links(self, key):
        """{"local": this PC's browser, "lan": [phones on this network]}."""
        card = PREVIEW + key
        return {"local": self.lan.url("127.0.0.1", card, self.lang),
                "lan": [self.lan.url(ip, card, self.lang) for ip in self.addresses()]}

    def previewers(self, key):
        return self.gateway.previewers(key) if self.gateway is not None else 0

    def viewers(self):
        try:
            return list(self.service.viewers())
        except Exception:  # noqa: BLE001
            return []

    def stop(self):
        self.stop_anywhere()
        if self.gateway is not None:
            # tell every open page it ended, then close
            for sid in list(self.hub.sessions):
                self.hub.push(sid, {"t": "bye"})
            self.gateway.stop()
            self.gateway = None
        if self.lan is not None:
            self.lan.stop()
            self.lan = None
        self.hub = None


def _bytes(v):
    return v if isinstance(v, bytes) else str(v).encode("utf-8")
