"""«وضع الاجتماع»: one presenter drives the dashboards of everyone in the room.

The PC starts a meeting on a dashboard and shows a 6-digit PIN. A viewer who types the PIN becomes the presenter
(one at a time; five wrong tries close that page's chance). The presenter's page sends its view (the hero map's
centre and scale, the selection, the open popup, the scroll) and its laser pointer (a point on the map); the PC
passes them on to every other viewer of the same dashboard. Viewers can step out of following. Nobody else can
drive: a view or laser message from a page that is not the presenter is refused.
"""
import secrets
import time

MAX_TRIES = 5


class Meeting:
    def __init__(self, dash):
        self.dash = dash
        self.pin = "".join(secrets.choice("0123456789") for _ in range(6))
        self.presenter = None          # session id
        self.presenter_name = ""
        self.tries = {}
        self.started = time.time()
        self.view = None

    def claim(self, session, pin):
        n = self.tries.get(session.id, 0)
        if n >= MAX_TRIES:
            return "too many tries"
        if not secrets.compare_digest(str(pin or "").strip(), self.pin):
            self.tries[session.id] = n + 1
            return "wrong pin"
        self.presenter = session.id
        self.presenter_name = str((session.card or {}).get("name") or "")[:80]
        return ""

    def is_presenter(self, session):
        return self.presenter is not None and session.id == self.presenter

    def release(self, session):
        if self.is_presenter(session):
            self.presenter = None
            self.presenter_name = ""
            return True
        return False


def clean_view(v):
    """Only numbers and short keys go on: {cx, cy, mpp, wid, xf: [field, key] | None, scroll, pop: [lid, fid]}."""
    if not isinstance(v, dict):
        return None
    out = {}
    for k in ("cx", "cy", "mpp", "scroll"):
        x = v.get(k)
        if isinstance(x, (int, float)) and abs(x) < 1e9:
            out[k] = float(x)
    if isinstance(v.get("wid"), str):
        out["wid"] = v["wid"][:40]
    xf = v.get("xf")
    if isinstance(xf, list) and len(xf) == 2:
        out["xf"] = [str(xf[0])[:80], str(xf[1])[:200]]
    elif "xf" in v and xf is None:
        out["xf"] = None
    pop = v.get("pop")
    if isinstance(pop, list) and len(pop) == 2:
        out["pop"] = [str(pop[0])[:10], str(pop[1])[:40]]
    return out


def clean_laser(v):
    if not isinstance(v, dict):
        return None
    lon, lat = v.get("lon"), v.get("lat")
    if not isinstance(lon, (int, float)) or not isinstance(lat, (int, float)):
        return {"off": True}
    if not (-180 <= lon <= 180 and -85 <= lat <= 85):
        return None
    return {"lon": float(lon), "lat": float(lat), "wid": str(v.get("wid") or "")[:40]}
