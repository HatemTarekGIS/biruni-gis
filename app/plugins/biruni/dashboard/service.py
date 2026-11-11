"""The viewers of the dashboards of this PC: one session per browser, whatever carries its messages.

The desk (server.py / host.py / builder.py) calls everything here in Biruni's main thread (SPEC.md, Changes):

    svc = DashboardService(project)
    sess = svc.connect(send, transport="lan" | "bridge" | "preview", definition=None)
    sess.receive(msg)            # a browser -> PC message; replies and pushes go to send(msg)
    sess.close(); svc.viewers(); svc.reload(); svc.stop()

A session starts with a hello carrying its card (a preview session carries none and sees the definition being
edited). From then on it gets {"t":"data"} with only the widgets whose numbers changed; every number has its
seal. The layers' edits (also those that came from a team room) are gathered for half a second, then each
session is computed again; an edit of a few features reloads only those features (data.Computer).
"""
import json
import secrets
import socket
import time

from qgis.core import QgsProject
from qgis.PyQt.QtCore import QObject, QTimer

from . import basemap, cards, data, model, seals

DEBOUNCE_MS = 500
CHECK_MS = 5000               # expired and revoked cards, the team room appearing or going
MAX_FILTER_VALUES = 200
EDIT_SIGNALS = ("featureAdded", "featureDeleted", "attributeValueChanged", "geometryChanged")
FULL_SIGNALS = ("afterCommitChanges", "afterRollBack", "dataChanged", "subsetStringChanged")


def _pc_name():
    try:
        from ..team.session import my_name

        return my_name()
    except Exception:  # noqa: BLE001
        return socket.gethostname()



# Messages that extension modules (ext_*.py) answer: {type: fn(service, session, msg)}. A module registers
# its handlers when imported; load_extensions() imports them all once.
HANDLERS = {}


def handler(t):
    def deco(fn):
        HANDLERS[t] = fn
        return fn
    return deco


def load_extensions():
    from .page import extensions

    return extensions()

class Session:
    def __init__(self, svc, send, transport, definition=None):
        self.svc = svc
        self.send = send
        self.transport = transport
        self.id = secrets.token_hex(8)
        self.definition = definition      # preview: the definition being edited
        self.card = None                  # the card payload after a good hello
        self.card_text = None
        self.dash_id = None
        self.filters = {}
        self.sent = {}                    # widget id -> the JSON last sent
        self.rev = 0
        self.since = time.time()
        self.ready = False
        self.closed = False

    def receive(self, msg):
        if not self.closed:
            self.svc._receive(self, msg)

    def close(self):
        if not self.closed:
            self.closed = True
            self.svc._drop(self)

    def _out(self, msg):
        if self.closed:
            return
        try:
            self.send(msg)
        except Exception:  # noqa: BLE001 - a transport that failed is the transport's business
            pass


class DashboardService(QObject):
    def __init__(self, project=None, parent=None):
        super().__init__(parent)
        self.project = project or QgsProject.instance()
        self.sessions = []
        self.defs = {}
        self.computers = {}           # dash id -> Computer (shared by its sessions)
        self._pending = {}            # layer id -> set of fids, or None for "all"
        self._links = []              # (layer, signal name, slot)
        self._node = None
        self._feed_dirty = False
        self._gen = 0
        self._cache = {}
        self._debounce = QTimer(self)
        self._debounce.setSingleShot(True)
        self._debounce.setInterval(DEBOUNCE_MS)
        self._debounce.timeout.connect(self.flush)
        self._check = QTimer(self)
        self._check.setInterval(CHECK_MS)
        self._check.timeout.connect(self._periodic)
        self._check.start()
        self.basemaps = basemap.Basemaps(self.project, self)
        self.reload(push=False)

    # ---- the desk's calls -----------------------------------------------------------------------------------------
    def connect(self, send, transport="lan", definition=None):
        s = Session(self, send, transport, definition)
        self.sessions.append(s)
        return s

    def viewers(self):
        return [{"session": s.id, "card": (s.card or {}).get("id"), "name": (s.card or {}).get("name", ""),
                 "dash": s.dash_id, "transport": s.transport, "since": s.since}
                for s in self.sessions if s.ready]

    def reload(self, push=True):
        """Read the dashboards again (after the builder saved). Sessions of a changed dashboard get it again."""
        try:
            new = {d["id"]: d for d in model.load_all(self.project) if d.get("id")}
        except Exception:  # noqa: BLE001
            new = {}
        changed = {i for i in set(new) | set(self.defs)
                   if json.dumps(new.get(i), sort_keys=True) != json.dumps(self.defs.get(i), sort_keys=True)}
        self.defs = new
        for i in changed:
            self.computers.pop(i, None)
        self._cache.clear()
        self._watch()
        if not push:
            return
        for s in list(self.sessions):
            if not s.ready or s.definition is not None or s.dash_id not in changed:
                continue
            if s.dash_id not in self.defs:
                s._out({"t": "error", "why": "removed"})
                s._out({"t": "bye"})
                s.close()
                continue
            s.sent = {}
            self._welcome(s)

    def preview(self, session, definition):
        """The builder changed the definition being previewed."""
        session.definition = definition
        self.computers.pop(("preview", session.id), None)
        self._watch()
        if session.ready:
            session.sent = {}
            self._welcome(session)

    def stop(self):
        self._debounce.stop()
        self._check.stop()
        self.basemaps.stop()
        for s in list(self.sessions):
            s._out({"t": "bye"})
            s.closed = True
        self.sessions = []
        self._unwatch()
        self._set_node(None)

    # ---- the definitions and their computers ---------------------------------------------------------------------
    def _def_of(self, s):
        return s.definition if s.definition is not None else self.defs.get(s.dash_id)

    def _computer(self, s):
        key = ("preview", s.id) if s.definition is not None else s.dash_id
        c = self.computers.get(key)
        if c is None:
            c = self.computers[key] = data.Computer(self._def_of(s), self.project)
        return c

    # ---- what changes ------------------------------------------------------------------------------------------------
    def _layer_ids(self):
        ids = set()
        defs = list(self.defs.values()) + [s.definition for s in self.sessions if s.definition is not None]
        for d in defs:
            for src in (d.get("sources") or {}).values():
                if src.get("layer"):
                    ids.add(src["layer"])
            for w in d.get("widgets") or []:
                z = (w.get("group") or {}).get("zones")
                if z:
                    ids.add(z)
        return ids

    def _watch(self):
        want = self._layer_ids()
        have = {lyr.id() for lyr, _n, _s in self._links if _alive(lyr)}
        if want == have:
            return
        self._unwatch()
        for lid in want:
            lyr = self.project.mapLayer(lid)
            if lyr is None:
                continue
            for name in EDIT_SIGNALS + FULL_SIGNALS:
                sig = getattr(lyr, name, None)
                if sig is None:
                    continue
                slot = self._slot(lyr, name)
                sig.connect(slot)
                self._links.append((lyr, name, slot))

    def _unwatch(self):
        for lyr, name, slot in self._links:
            try:
                getattr(lyr, name).disconnect(slot)
            except (TypeError, RuntimeError, AttributeError):
                pass
        self._links = []

    def _slot(self, lyr, name):
        lid = lyr.id()
        if name in FULL_SIGNALS:
            return lambda *a: self.changed(lid, None)
        kind = {"featureAdded": "add", "featureDeleted": "del"}.get(name, "edit")

        def edited(fid, *a):
            self.changed(lid, [int(fid)])
            if self._node is None:   # in a team room the feed comes from the room's own record
                who = _pc_name()
                for c in self.computers.values():
                    if lid in c.layer_ids():
                        try:
                            c.log_edit(lyr, kind, int(fid), who)
                        except RuntimeError:
                            pass
                self._feed_dirty = True
        return edited

    def changed(self, layer_id=None, fids=None):
        """A layer changed (all of it when fids is None); the sessions are computed again shortly."""
        if layer_id is None:
            self._pending = {lid: None for lid in self._layer_ids()}
        elif fids is None or self._pending.get(layer_id, set()) is None:
            self._pending[layer_id] = None
        else:
            self._pending.setdefault(layer_id, set()).update(fids)
        if not self._debounce.isActive():
            self._debounce.start()

    def _set_node(self, node):
        if node is self._node:
            return
        if self._node is not None:
            try:
                self._node.entry.disconnect(self._room_entry)
            except (TypeError, RuntimeError, AttributeError):
                pass
        self._node = node
        if node is not None:
            try:
                node.entry.connect(self._room_entry)
            except (TypeError, RuntimeError, AttributeError):
                pass

    def _room_entry(self, *_a):
        self._feed_dirty = True
        if not self._debounce.isActive():
            self._debounce.start()

    def _periodic(self):
        try:
            from .. import team

            self._set_node(team.current())
        except Exception:  # noqa: BLE001
            pass
        self._watch()
        now = time.time()
        revoked = None
        for s in list(self.sessions):
            if not s.ready or s.card is None:
                continue
            if revoked is None:
                revoked = cards.revoked(self.project)
            why = "expired" if float(s.card.get("exp") or 0) < now else ("revoked" if s.card["id"] in revoked else "")
            if why:
                s._out({"t": "error", "why": why})
                s._out({"t": "bye"})
                s.close()

    def flush(self):
        """Apply the gathered changes and send every session what changed."""
        pending, self._pending = self._pending, {}
        for c in self.computers.values():
            for lid, fids in pending.items():
                c.invalidate(lid, fids)
        self._gen += 1
        self._cache.clear()
        self._feed_dirty = False
        for s in list(self.sessions):
            if s.ready:
                self._push(s)

    # ---- the messages --------------------------------------------------------------------------------------------
    def _drop(self, s):
        if s in self.sessions:
            self.sessions.remove(s)
        self.computers.pop(("preview", s.id), None)

    def _receive(self, s, msg):
        if not isinstance(msg, dict) or not isinstance(msg.get("t"), str):
            s._out({"t": "error", "why": "bad message"})
            return
        t = msg["t"]
        try:
            if t == "hello":
                self._hello(s, msg)
            elif not s.ready:
                s._out({"t": "error", "why": "hello first"})
            elif t == "filter":
                self._filter(s, msg.get("values"))
            elif t == "explain":
                self._explain(s, msg)
            elif t == "seal":
                self._seal(s, msg)
            elif t == "basemap":
                self._basemap(s, msg)
            elif t == "ping":
                pass
            elif t in HANDLERS:
                HANDLERS[t](self, s, msg)
            else:
                s._out({"t": "error", "why": "unknown message"})
        except Exception as e:  # noqa: BLE001 - one viewer's message never stops Biruni
            s._out({"t": "error", "why": "failed: " + str(e)[:200]})

    def _hello(self, s, msg):
        if s.definition is not None:          # the builder's preview: no card
            s.card, s.dash_id = None, s.definition.get("id")
        else:
            try:
                card = cards.parse(msg.get("card"), project=self.project)
            except cards.CardError as e:
                s._out({"t": "error", "why": e.why})
                return
            if card.get("dash") not in self.defs:
                s._out({"t": "error", "why": "removed"})
                return
            s.card, s.card_text, s.dash_id = card, msg.get("card"), card["dash"]
        s.ready = True
        s.sent = {}
        self._watch()
        self._welcome(s)

    def _welcome(self, s):
        d = self._def_of(s)
        card = s.card
        info = {"name": (card or {}).get("name", "") if card else "preview", "exp": (card or {}).get("exp"),
                "zone": bool((card or {}).get("zone"))}
        s._out({"t": "dash", "def": model.for_card(d, card), "card": info, "pc": _pc_name(),
                "pub": cards.public_hex()})
        self._push(s)

    def _filter(self, s, values):
        if not isinstance(values, dict):
            s._out({"t": "error", "why": "bad filter"})
            return
        for fid, v in list(values.items())[:50]:
            if v is None or v == []:
                s.filters.pop(str(fid), None)
            elif isinstance(v, list):
                s.filters[str(fid)] = [str(x)[:200] for x in v[:MAX_FILTER_VALUES]]
            else:
                s.filters[str(fid)] = [str(v)[:200]]
        self._push(s)

    def _explain(self, s, msg):
        c = self._computer(s)
        try:
            out = c.explain(str(msg.get("widget", "")), msg.get("key"), s.card, s.filters)
        except data.DataError as e:
            s._out({"t": "error", "why": str(e)})
            return
        s._out(out)

    def _basemap(self, s, msg):
        """The project's basemap for the view a viewer's map shows (basemap.py)."""
        wid = str(msg.get("widget", ""))
        d = self._def_of(s) or {}
        w = next((x for x in d.get("widgets") or [] if x.get("id") == wid), None)
        if (w is None or w.get("type") != "map" or (w.get("options") or {}).get("basemap") != "project"
                or (s.card is not None and wid not in model.allowed(d, s.card))):
            s._out({"t": "error", "why": "no basemap"})
            return
        args = basemap.view_args(msg)
        if args is None:
            s._out({"t": "error", "why": "bad view"})
            return
        self.basemaps.request(s, wid, args, (s.card or {}).get("zone"))

    def _seal(self, s, msg):
        got = seals.lookup(str(msg.get("code", "")))
        st = (got or {}).get("statement") or {}
        mine = (st.get("card") or {}).get("id") == (s.card or {}).get("id")
        if not got or st.get("dash") != s.dash_id or not mine:
            s._out({"t": "error", "why": "unknown seal"})
            return
        s._out({"t": "seal", "code": got["code"], "statement": st, "sig": got["sig"]})

    # ---- computing and sending -----------------------------------------------------------------------------------
    def _result(self, s):
        c = self._computer(s)
        eff = c.effective_filters(s.card, s.filters)
        key = (id(c), (s.card or {}).get("id"), json.dumps(eff, sort_keys=True), self._gen)
        r = self._cache.get(key)
        if r is None:
            r = self._cache[key] = (c.compute(card=s.card, filters=s.filters), eff)
            if len(self._cache) > 200:
                self._cache.clear()
        return r

    def _push(self, s):
        d = self._def_of(s)
        if d is None:
            return
        (res, eff) = self._result(s)
        titles = {w["id"]: w.get("title", "") for w in d.get("widgets") or []}
        sources = {w["id"]: w.get("source") for w in d.get("widgets") or []}
        out = {}
        for wid, value in res["widgets"].items():
            text = json.dumps(value, sort_keys=True, ensure_ascii=False, default=str)
            if s.sent.get(wid) == text:
                continue
            s.sent[wid] = text
            v = dict(value)
            if "error" not in v:
                try:
                    v["seal"] = seals.make(d.get("id"), wid, titles.get(wid, ""), value, eff, s.card,
                                           res["digests"].get(sources.get(wid)))
                except Exception:  # noqa: BLE001 - a number without a seal is still sent
                    pass
            out[wid] = v
        for wid in [w for w in s.sent if w not in res["widgets"]]:
            del s.sent[wid]
        if not out and s.rev:
            return
        s.rev += 1
        s._out({"t": "data", "rev": s.rev, "ts": round(time.time(), 3), "widgets": out})


def _alive(lyr):
    try:
        lyr.id()
        return True
    except RuntimeError:
        return False


try:  # the extension modules register their message handlers
    load_extensions()
except Exception:  # noqa: BLE001 - a broken extension never stops the dashboard
    pass
