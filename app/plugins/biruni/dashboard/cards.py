"""Cards: who sees what of a dashboard, with no server (SPEC.md §3).

A card is a signed, self-contained permission: ``BDC1.<b64u(payload)>.<b64u(ed25519 signature)>``. It is
signed with the dashboard key of this Windows user (kept encrypted in the settings, like the team identity),
so only this PC can issue one, and a card for another PC, an altered card, an expired or a revoked one is
refused. The PC applies the card before any number is computed (data.py): the zone cuts the sources, hidden
fields never leave the PC, forced filters cannot be changed by the viewer.
"""
import base64
import json
import secrets
import time

from qgis.core import QgsGeometry, QgsProject, QgsSettings

from ..secure import crypto
from . import model

KEY = "biruni/dashboard/key"
PREFIX = "BDC1"
MAX_LEN = 64 * 1024          # a card is read from a link: no card is bigger than this
_TEST_SEED = []              # tests: a fixed key instead of the user's


class CardError(Exception):
    """why: bad card | foreign | expired | revoked | other dashboard"""

    def __init__(self, why):
        super().__init__(why)
        self.why = why


def b64u(data):
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def unb64u(text):
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _seed():
    if _TEST_SEED:
        return _TEST_SEED[0]
    from ..field_photos.tunnel import protect, unprotect

    s = QgsSettings()
    try:
        seed = bytes.fromhex(unprotect(s.value(KEY, "") or "") or "")
        assert len(seed) == 32
    except Exception:  # noqa: BLE001 - the first time
        seed = secrets.token_bytes(32)
        s.setValue(KEY, protect(seed.hex()))
    return seed


def seed():
    """This PC's dashboard signing key (32 bytes)."""
    return _seed()


def public_hex():
    return crypto.public_key(_seed()).hex()


def _body(payload):
    return crypto.canonical(payload)


def issue(dash_id, name, widgets="*", hide=(), zone_wkt=None, filters=None, exp=None, input=False, days=30,
          project=None):
    """A new card for dashboard ``dash_id`` (kept in the project's list of issued cards). -> the card text."""
    if zone_wkt:
        g = QgsGeometry.fromWkt(zone_wkt)
        if g.isNull() or g.isEmpty():
            raise ValueError("the zone is not a geometry")
        zone_wkt = g.asWkt(7)
    now = time.time()
    payload = {"v": 1, "id": model.new_id("c"), "dash": dash_id, "name": (name or "")[:80],
               "widgets": widgets if widgets == "*" else [str(w) for w in widgets],
               "hide": sorted({str(h) for h in hide or ()}), "zone": zone_wkt or None,
               "filters": filters or None, "input": bool(input), "iat": round(now, 3),
               "exp": round(float(exp if exp is not None else now + days * 86400), 3)}
    sig = crypto.sign(_seed(), _body(payload))
    card = f"{PREFIX}.{b64u(_body(payload))}.{b64u(sig)}"
    try:
        model.keep_card(project or QgsProject.instance(), payload)
    except Exception:  # noqa: BLE001 - the card is valid without the list (a project not saved yet)
        pass
    return card


def parse(card, dash_id=None, now=None, project=None, revoked_ids=None):
    """The payload of a good card; raises CardError(why) otherwise."""
    if not isinstance(card, str) or len(card) > MAX_LEN:
        raise CardError("bad card")
    parts = card.strip().split(".")
    if len(parts) != 3 or parts[0] != PREFIX:
        raise CardError("bad card")
    try:
        body = unb64u(parts[1])
        sig = unb64u(parts[2])
        payload = json.loads(body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        raise CardError("bad card") from None
    if not isinstance(payload, dict) or payload.get("v") != 1 or not payload.get("id"):
        raise CardError("bad card")
    if not crypto.verify(crypto.public_key(_seed()), sig, _body(payload)):
        raise CardError("foreign")      # altered, or signed by another computer
    if dash_id is not None and payload.get("dash") != dash_id:
        raise CardError("other dashboard")
    if float(payload.get("exp") or 0) < (now if now is not None else time.time()):
        raise CardError("expired")
    ids = revoked_ids if revoked_ids is not None else revoked(project)
    if payload["id"] in ids:
        raise CardError("revoked")
    return payload


def verify(card, dash_id=None, now=None, project=None):
    """True for a good card (no reason)."""
    try:
        parse(card, dash_id, now, project)
        return True
    except CardError:
        return False


def revoke(card_id, dash_id="", project=None):
    model.revoke_card(project or QgsProject.instance(), card_id, dash_id)


def revoked(project=None):
    try:
        return model.revoked_ids(project or QgsProject.instance())
    except Exception:  # noqa: BLE001
        return set()


def issued(dash_id=None, project=None):
    return model.issued_cards(project or QgsProject.instance(), dash_id)
