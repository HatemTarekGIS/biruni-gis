"""The phone tools the hub knows, read from their own panels: is each one on, and where does the phone open it.

Each tool keeps its own panel, server and code; the hub only looks at them and can press their Start / Stop.
A tool started in a mode the phone cannot open from here (the GPS on a COM port) is on, without a link.
"""
import sys

from ..i18n import tr


def _loaded(plugin, module):
    """The panel of a tool when it was opened in this window (its module is imported only then)."""
    mod = sys.modules.get(f"{__package__.rsplit('.', 1)[0]}.{module}")
    return mod._DOCK.get(id(plugin)) if mod is not None else None


def _url_link(text):
    """A tool reached by a full address (bridge, tunnel, see my map); None while the address is not known."""
    text = (text or "").strip()
    return {"url": text} if text.startswith("https://") and "\n" not in text else None


class Feature:
    id = ""
    icon = ""

    def title(self):
        raise NotImplementedError

    def available(self, plugin):
        return True

    def state(self, plugin):
        """(on, link): link as server.clean_link takes it, or None."""
        raise NotImplementedError

    def start(self, plugin):
        raise NotImplementedError

    def stop(self, plugin):
        raise NotImplementedError

    def show(self, plugin):
        raise NotImplementedError


class PhoneGps(Feature):
    id, icon = "gps", "device-mobile-pin"

    def title(self):
        return tr("Phone GPS")

    def available(self, plugin):
        return getattr(plugin, "tracking", None) is not None

    def state(self, plugin):
        dock = getattr(plugin, "tracking", None)
        src = getattr(dock, "source", None)
        if src is None or not src.is_active():
            return False, None
        if getattr(src, "browser", None) is not None:
            return True, {"port": src.browser.port, "path": f"/{src.browser.token}/"}
        if getattr(src, "bridge", None) is not None:
            return True, _url_link(dock.qr_url.toolTip())
        return True, None

    def start(self, plugin):
        dock = plugin.tracking
        if dock.source.is_active():
            return
        if dock.mode.currentData() not in ("browser", "bridge"):  # the phone page, not a COM port or an app
            dock.mode.setCurrentIndex(dock.mode.findData("browser"))
        dock.toggle_connection()

    def stop(self, plugin):
        plugin.tracking.disconnect_source()

    def show(self, plugin):
        dock = plugin.tracking
        dock.show()
        dock.raise_()


class FieldPhotos(Feature):
    id, icon = "photos", "camera"

    def title(self):
        return tr("Field Photos")

    def state(self, plugin):
        dock = _loaded(plugin, "field_photos.dock")
        server = getattr(dock, "server", None)
        if server is None:
            return False, None
        if dock.mode.currentData() == "local":
            return True, {"port": server.port, "path": server.path()}
        return True, _url_link(dock.url.toolTip())  # bridge or tunnel: None until its address is known

    def _dock(self, plugin):
        from ..field_photos.dock import open_dock

        return open_dock(plugin)

    def start(self, plugin):
        dock = self._dock(plugin)
        if dock.server is not None or dock._downloading is not None:
            return
        if dock.mode.currentData() not in ("local", "bridge"):  # the hub is for a phone on the same Wi-Fi
            dock.mode.setCurrentIndex(dock.mode.findData("local"))
        dock.start()

    def stop(self, plugin):
        dock = _loaded(plugin, "field_photos.dock")
        if dock is not None:
            dock.stop()

    def show(self, plugin):
        self._dock(plugin)


class SeeMyMap(Feature):
    id, icon = "map", "antenna"

    def title(self):
        return tr("See My Map")

    def state(self, plugin):
        dock = _loaded(plugin, "live_share.dock")
        if getattr(dock, "share", None) is None:
            return False, None
        return True, _url_link(dock.link)

    def _dock(self, plugin):
        from ..live_share.dock import open_dock

        return open_dock(plugin)

    def start(self, plugin):
        dock = self._dock(plugin)
        if dock.share is None:
            dock.start()

    def stop(self, plugin):
        dock = _loaded(plugin, "live_share.dock")
        if dock is not None and dock.share is not None:
            dock.stop()

    def show(self, plugin):
        self._dock(plugin)


def all_features():
    return [PhoneGps(), FieldPhotos(), SeeMyMap()]


# other phone tools of Biruni that have no page on this PC: the panel opens them (command ids of commands.py)
OTHER_TOOLS = (("qfield_send", "device-mobile-up"), ("qfield_back", "device-mobile-down"),
               ("live_qr", "device-mobile-pin"), ("team_room", "users"))
