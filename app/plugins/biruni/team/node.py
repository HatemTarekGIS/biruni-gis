"""One computer in a room: the host or a member. The same engine runs on both.

Your edits: a shared layer in edit mode is saved by itself a moment after you stop changing it
(live editing), so the others see your work within seconds. New features take ids from a block
the host gave this computer, so two people's new features never get the same id. Each saved
change becomes a signed change (the state before and after) that goes to the host - or, on the
host, straight into the stream.

The others' edits: the stream's entries are written into this computer's copy through the
layer's data provider. An entry for a feature you are editing right now waits until your edit
is saved, then the host joins the two (merge.py).

When the host goes away: members keep working (their edits wait in the outbox) and look for
the host on the network and on ntfy. If nobody holds the room after a while - longer for those
later in the join order - the first member in that order becomes the host itself: every member
has the whole stream and the same files, so the room goes on. The others find it, send what it
missed, and refresh their copies. A host that comes back finds the new one and joins it.
"""
import json
import os
import queue
import threading
import time
import uuid

from qgis.core import (QgsCoordinateTransform, QgsGeometry, QgsProject, QgsRectangle)
from qgis.PyQt.QtCore import QObject, QTimer, pyqtSignal

from ..edit_journal import _field_dict, _field_from
from ..i18n import tr
from ..secure import crypto
from . import features, wire
from .hub import Hub, own_block, sign_body

LIVE_MS = 1500          # quiet time after a change before it is saved and sent
LOST_S = 8              # seconds without the host before looking for it
ELECTION_S = 12         # seconds before the first member in the join order takes over the room
ELECTION_STEP = 8       # ... and this much longer for each member before it
ZONE_S = 10 * 60        # how long someone's edits mark their work area
PUBLISH_S = 4 * 60      # how often the host leaves its outside address on ntfy
LINK = "link:"          # the outside address: a direct link between the computers (link.py), no tunnel
PRESENCE_MS = 2000      # how often this computer says where its pointer is (only when it moved)


def _rect_of(state, crs_from, crs_to):
    if not state or not state.get("g"):
        return None
    g = QgsGeometry()
    g.fromWkb(bytes.fromhex(state["g"]))
    if g.isNull():
        return None
    if crs_from.isValid() and crs_to.isValid() and crs_from != crs_to:
        try:
            g.transform(QgsCoordinateTransform(crs_from, crs_to, QgsProject.instance()))
        except Exception:  # noqa: BLE001 - outside the projection's area
            return None
    return g.boundingBox()


class Node(QObject):
    status = pyqtSignal(str, str)       # level (info/warn/ok), text
    changed = pyqtSignal()              # role, members, presence, conflicts or outbox changed
    entry = pyqtSignal(dict)            # a change from the stream reached this map
    joinRequested = pyqtSignal(dict)    # host: a new member waits for approval
    deletesHeld = pyqtSignal(str)       # host: a member's deletions wait for an answer (the guard)

    def __init__(self, store, keys, ident, parent=None):
        super().__init__(parent)
        self.store = store
        self.keys = keys
        self.sealer = wire.Sealer(keys.key)
        self.ident = ident                 # {"id", "name", "seed"}
        self.pub = crypto.public_key(bytes.fromhex(ident["seed"])).hex()
        self._box_priv, self.xpub = wire.box_key(ident["seed"])
        self.layers = {}
        self.role = None                   # "host" | "member"
        self.hub = None
        self.client = None
        self.address = None
        self.member = None
        self.roster = store.get("roster", [])
        self.presence = {}
        self.recent = {}                   # author id -> [(time, rect, name)]
        self.deferred = store.get("deferred") or {}   # {layer key: [entries]} waiting for an edit to be saved
        self.state = "idle"
        self.epoch = store.get("epoch", 1)
        self.since = store.get("since", 0)
        self.tunnel = None
        self.link = None                   # host: the direct link that members outside this network come in by
        self.external_url = None
        self._touch = {}
        self._timers = {}
        self._links = []
        self._q = queue.Queue()
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._threads = []
        self._sent_ok = set()
        self._presence_out = None
        self._last_presence = None
        self._cursor = None
        self.last_ok = time.time()
        self._finding = False
        self._rekey_at = None    # the time of the code change this computer is waiting for
        self._fields_before = {}  # layer key -> its field names before a save (renamed / deleted fields)
        self._held_said = False   # told the user once that its deletions wait for the host
        self._found_at = 0.0
        self._checked = time.time()
        self.was_connected = False
        self._lost_since = None
        self._warned = {}
        self._published = 0
        self._tick = QTimer(self)
        self._tick.setInterval(50)
        self._tick.timeout.connect(self._drain)
        self._view = None
        self._pres_timer = QTimer(self)
        self._pres_timer.setInterval(PRESENCE_MS)
        self._pres_timer.timeout.connect(self._presence_tick)

    # ---- who we are -----------------------------------------------------------------------------------
    @property
    def me_id(self):
        return self.ident["id"]

    def my_rank(self):
        for m in self.roster:
            if m["id"] == self.me_id:
                return m.get("rank", 99)
        return self.store.get("rank", 99)

    def _me_member(self, token):
        return {"id": self.me_id, "name": self.ident["name"], "pub": self.pub, "rank": self.my_rank(),
                "token": token, "rights": {k: "edit" for k in self.layers}, "state": "in"}

    # ---- layers ---------------------------------------------------------------------------------------
    def attach_layers(self, layers):
        self.detach_layers()
        self.layers = dict(layers)
        for key, lyr in self.layers.items():
            t = QTimer(self)
            t.setSingleShot(True)
            t.setInterval(LIVE_MS)
            t.timeout.connect(lambda k=key: self.commit_layer(k))
            self._timers[key] = t
            for sig, slot in ((lyr.layerModified, lambda k=key: self._modified(k)),
                              (lyr.beforeCommitChanges, lambda *_, k=key: self._before(k)),
                              (lyr.afterCommitChanges, lambda k=key: self._after(k)),
                              (lyr.attributeAdded, lambda idx, k=key: self._field_added(k, idx)),
                              (lyr.willBeDeleted, lambda k=key: self._layer_gone(k))):
                sig.connect(slot)
                self._links.append((key, sig, slot))
        self._recover_pending()

    def detach_layers(self):
        for _key, sig, slot in self._links:
            try:
                sig.disconnect(slot)
            except (TypeError, RuntimeError):
                pass
        self._links = []
        for t in self._timers.values():
            t.stop()
        self._timers = {}

    def _layer_gone(self, key):
        # the layer is still alive while it says it will be deleted: let go of its signals now - disconnecting
        # them after it is gone crashed the program when it was closed with a room open
        for link in [x for x in self._links if x[0] == key]:
            try:
                link[1].disconnect(link[2])
            except (TypeError, RuntimeError):
                pass
            self._links.remove(link)
        self.layers.pop(key, None)
        t = self._timers.pop(key, None)
        if t is not None:
            t.stop()

    def key_of(self, layer):
        for k, lyr in self.layers.items():
            if lyr is layer:
                return k
        return None

    def _modified(self, key):
        t = self._timers.get(key)
        if t is not None:
            t.start()

    def commit_layer(self, key):
        """Save the shared layer's edits now (live editing)."""
        lyr = self.layers.get(key)
        if lyr is None or not lyr.isEditable() or not lyr.isModified():
            return True
        self._assign_ids(lyr)
        if not lyr.commitChanges(False):
            self.status.emit("warn", "; ".join(lyr.commitErrors())[:400])
            return False
        return True

    def commit_soon(self, key):
        t = self._timers.get(key)
        if t is not None:
            t.start(200)

    # ---- feature ids ----------------------------------------------------------------------------------
    def next_fid(self, layer=None):
        """A feature id of this computer's own block, not used in ``layer`` (a tool that writes to the file
        directly gets the file's next id, which may fall inside a block)."""
        while True:
            block = self.store.get("block")
            if not block or block[0] >= block[1]:
                block = self._new_block()
            fid = block[0]
            self.store.put("block", [block[0] + 1, block[1]])
            if layer is None or not features.stored(layer, fid):
                return fid

    def _new_block(self):
        """The next block of this computer's own ids (hub.own_block): the same on any host, online or offline."""
        n = int(self.store.get("block_n", 0))
        block = own_block(self.me_id, n)
        self.store.put("block_n", n + 1)
        self.store.put("block", block)
        return block

    def _assign_ids(self, lyr):
        pk = lyr.primaryKeyAttributes()
        buf = lyr.editBuffer()
        if not pk or buf is None:
            return
        for tmp, f in list(buf.addedFeatures().items()):
            cur = f.attribute(pk[0])
            try:
                ok = cur is not None and int(cur) > 0
            except (TypeError, ValueError):
                ok = False
            if not ok:
                lyr.changeAttributeValue(tmp, pk[0], self.next_fid(lyr))
        self._assign_numbers(lyr)

    # ---- [I06] escrowed numbers (rules.py): new features take the next free number of my own ranges ----------
    def lookup_member(self, cid):
        """A member (with pub and rank) by id, for checking the room's signed rules: the host's roster when this
        computer hosts, the roster it was sent otherwise."""
        if cid == self.me_id:
            return {"id": self.me_id, "pub": self.pub, "rank": self.my_rank(), "state": "in"}
        if self.hub is not None:
            return self.hub._member(cid)
        return next((m for m in self.roster if m.get("id") == cid), None)

    def my_grants(self, key, field=None):
        from . import rules

        return rules.grants(self.store, self.lookup_member, layer=key, field=field, member=self.me_id)

    def numbers_left(self, key, field):
        from . import rules

        lyr = self.layers.get(key)
        docs = self.my_grants(key, field)
        if lyr is None or not docs:
            return None
        return rules.left(docs, self._used_numbers(lyr, field))

    @staticmethod
    def _used_numbers(lyr, field):
        idx = lyr.fields().indexOf(field)
        used = set()
        if idx < 0:
            return used
        for v in lyr.uniqueValues(idx):
            try:
                used.add(int(v))
            except (TypeError, ValueError):
                pass
        return used

    def _assign_numbers(self, lyr):
        from . import rules

        key = self.key_of(lyr)
        buf = lyr.editBuffer()
        if key is None or buf is None or not buf.addedFeatures():
            return
        for field in sorted({d["field"] for d in self.my_grants(key)}):
            idx = lyr.fields().indexOf(field)
            if idx < 0:
                continue
            docs = self.my_grants(key, field)
            used = self._used_numbers(lyr, field)
            for tmp, f in list(buf.addedFeatures().items()):
                cur = f.attribute(idx)
                if cur not in (None, "") and str(cur) != "NULL":
                    continue
                n = rules.next_number(docs, used)
                if n is None:
                    self.status.emit("warn", tr("Your numbers for {0} are used up: ask the room's admin for "
                                                "more.").format(field))
                    break
                lyr.changeAttributeValue(tmp, idx, n)
                used.add(n)
            left = rules.left(docs, used)
            if 0 < left <= 5:
                self.status.emit("warn", tr("Only {0} numbers left for {1}: ask the room's admin for more.")
                                 .format(left, field))

    # ---- capturing this computer's edits --------------------------------------------------------------
    def _before(self, key):
        lyr = self.layers.get(key)
        buf = lyr.editBuffer() if lyr is not None else None
        if buf is None:
            return
        self._assign_ids(lyr)
        pk = lyr.primaryKeyAttributes()
        added = []
        for tmp, f in buf.addedFeatures().items():
            try:
                added.append(int(f.attribute(pk[0])) if pk else None)
            except (TypeError, ValueError):
                pass
        changed = (set(buf.changedGeometries()) | set(buf.changedAttributeValues())) - set(buf.addedFeatures())
        deleted = set(buf.deletedFeatureIds())
        base = {fid: features.stored_state(lyr, fid) for fid in changed | deleted}
        new_fields = [_field_dict(f) for f in buf.addedAttributes()]
        self._touch[key] = (added, changed - deleted, deleted, base, new_fields)
        self._fields_before[key] = [f.name() for f in lyr.dataProvider().fields()]
        pend = self.store.get("pending_commit") or {}
        pend[key] = {"added": [a for a in added if a is not None], "changed": sorted(changed - deleted),
                     "deleted": sorted(deleted), "base": {str(f): s for f, s in base.items()}}
        self.store.put("pending_commit", pend)   # cleared once the changes are queued (_after)
        self._check_zones(lyr, list(changed) + [a for a in buf.addedFeatures()])

    def _after(self, key):
        lyr = self.layers.get(key)
        touched = self._touch.pop(key, None)
        if lyr is None or touched is None:
            return
        added, changed, deleted, base, new_fields = touched
        ops = []
        before = self._fields_before.pop(key, None)
        after = [f.name() for f in lyr.dataProvider().fields()]
        if self.role == "host":
            for fd in new_fields:
                ops.append(self._op(key, 0, "field", None, {"field": fd}))
            for change in field_changes(before, after):
                ops.append(self._op(key, 0, "field", None, change))
        elif before is not None and [n for n in before if n not in after]:
            self.status.emit("warn", "fields")   # only the host changes the fields of a shared layer
        for fid in added:
            if fid is not None:
                new = features.stored_state(lyr, fid)
                if new is not None:
                    ops.append(self._op(key, fid, "put", None, new))
        for fid in changed:
            new = features.stored_state(lyr, fid)
            if new is not None and new != base.get(fid):
                ops.append(self._op(key, fid, "put", base.get(fid), new))
        for fid in deleted:      # gone from the file: a save stopped (spatial rules) wrote nothing
            if base.get(fid) is not None and features.stored_state(lyr, fid) is None:
                ops.append(self._op(key, fid, "del", base.get(fid), None))
        self._send(ops)
        pend = self.store.get("pending_commit") or {}
        if pend.pop(key, None) is not None:
            self.store.put("pending_commit", pend)
        self._flush_deferred(key)

    def _recover_pending(self):
        """A save the power cut between writing the file and queueing the changes: what reached the file is
        sent now (it would stay on this computer only, and be wiped by the next copy of the room)."""
        pend = self.store.get("pending_commit") or {}
        if not pend:
            return
        queued = {(o["layer"], int(o["fid"])) for o in self.store.outbox(100000)}
        ops = []
        for key, p in pend.items():
            lyr = self.layers.get(key)
            if lyr is None:
                continue
            base = {int(k): v for k, v in (p.get("base") or {}).items()}

            def known(fid, state):
                hist = self.store.feature_history(key, fid)
                return (key, fid) in queued or (hist and hist[-1].get("state") == state)

            for fid in list(p.get("added") or []) + list(p.get("changed") or []):
                new = features.stored_state(lyr, fid)
                if new is not None and new != base.get(fid) and not known(fid, new):
                    ops.append(self._op(key, fid, "put", base.get(fid), new))
            for fid in p.get("deleted") or []:
                if base.get(fid) is not None and features.stored_state(lyr, fid) is None and not known(fid, None):
                    ops.append(self._op(key, fid, "del", base[fid], None))
        self.store.put("pending_commit", {})
        if ops:
            self._send(ops)

    def _field_added(self, key, idx):
        if self.role == "host":
            return
        lyr = self.layers.get(key)
        if lyr is None:
            return
        name = lyr.fields().at(idx).name() if 0 <= idx < lyr.fields().count() else ""
        QTimer.singleShot(0, lambda: lyr.deleteAttribute(lyr.fields().indexOf(name)) if name else None)
        self.status.emit("warn", "fields")

    def _op(self, key, fid, kind, base, state, note=None):
        op = {"uid": uuid.uuid4().hex, "layer": key, "fid": int(fid), "kind": kind,
              "base_ver": self.store.version(key, int(fid)), "base": base, "state": state,
              "author": self.me_id, "name": self.ident["name"], "ts": time.time()}
        if note:
            op["note"] = note
        src = self._source()
        if src and kind != "field":
            op["src"] = src
        op["sig"] = crypto.sign(bytes.fromhex(self.ident["seed"]), sign_body(op)).hex()
        return op

    def _source(self):
        """What was on the screen for this change (set by the panel: the images shown, the scale), or None."""
        fn = getattr(self, "source_of_change", None)
        if fn is None:
            return None
        try:
            from .hub import _src

            return _src(fn())
        except Exception:  # noqa: BLE001 - provenance never stops an edit
            return None

    def _send(self, ops):
        if not ops:
            return
        if self.role == "host" and self.hub is not None:
            for op in ops:
                if op["kind"] == "field":
                    self.hub.record(op, None, op["state"], 0, note="field")
                else:
                    self.hub.local(op, op["base"], op["state"])
        else:
            for op in ops:
                self.store.queue(op)
            self._wake.set()
        self.changed.emit()

    def submit(self, key, fid, state, note=None):
        """Set feature fid to state (None deletes it) as a change of this computer: undo, keep mine,
        restore a past version."""
        lyr = self.layers.get(key)
        if lyr is None:
            return False
        if lyr.isEditable() and lyr.isModified():
            self.commit_layer(key)
        cur = features.stored_state(lyr, fid)
        if state is None:
            ok = features.delete_stored(lyr, fid)
        else:
            ok = features.write_state(lyr, fid, state)
        lyr.triggerRepaint()
        if ok:
            op = self._op(key, fid, "put" if state is not None else "del", cur, state, note=note)
            op["force"] = note in ("mine", "restore")
            self._send([op])
        return ok

    def notify_added(self, layer, feats):
        """Features another tool wrote straight into a shared layer's file (phone photos and forms)."""
        key = self.key_of(layer)
        if key is None:
            return
        ops, moved = [], {}
        for f in feats:
            fid = f.id()
            state = features.stored_state(layer, fid)
            if state is None:
                continue
            if self.role != "host":  # the file gave it an id another computer may use: move it into our block
                new = self.next_fid(layer)
                if features.write_state(layer, new, state) and features.delete_stored(layer, fid):
                    moved[fid] = new
                    fid = new
            ops.append(self._op(key, fid, "put", None, state))
        layer.triggerRepaint()
        self._send(ops)
        return moved

    def notify_changed(self, layer, fids):
        """Features another tool changed or deleted straight in the file: sent as changes from the last state the
        room knows."""
        key = self.key_of(layer)
        if key is None:
            return
        ops = []
        waiting = {}
        for o in self.store.outbox(100000):   # this computer's changes not back from the host yet are newer
            if o.get("layer") == key and o.get("kind") != "field":
                waiting[int(o["fid"])] = o.get("state")
        for fid in fids:
            fid = int(fid)
            if fid in waiting:
                base = waiting[fid]
            else:
                hist = self.store.feature_history(key, fid)
                base = hist[-1].get("state") if hist else None
            new = features.stored_state(layer, fid)
            if new == base:
                continue
            ops.append(self._op(key, fid, "put" if new is not None else "del", base, new))
        self._send(ops)

    # ---- the stream ---------------------------------------------------------------------------------------
    def apply_entry(self, e):
        """Write one stream entry into this computer's copy (members)."""
        key = e["layer"]
        lyr = self.layers.get(key)
        if self.store.has(e["uid"]):
            if e["author"] == self.me_id:
                self.store.drop_outbox(e["uid"])
            return
        if lyr is not None and e["kind"] == "field":
            st = e.get("state") or {}
            fd = st.get("field") or {}
            pr = lyr.dataProvider()
            idx = pr.fields().indexOf(fd.get("name", ""))
            action = st.get("action", "add")
            if lyr.isEditable() and lyr.isModified():
                self.commit_layer(key)      # the fields change under the edits otherwise
            if action == "add" and fd and idx < 0:
                pr.addAttributes([_field_from(fd)])
            elif action == "delete" and idx >= 0:
                pr.deleteAttributes([idx])
            elif action == "rename" and idx >= 0 and st.get("new") and pr.fields().indexOf(st["new"]) < 0:
                pr.renameAttributes({idx: st["new"]})
            lyr.updateFields()
        elif lyr is not None:
            fid = int(e["fid"])
            waiting = [w for w in self.deferred.get(key, []) if int(w["fid"]) == fid]
            if waiting or features.is_pending(lyr, fid):
                self.deferred.setdefault(key, []).append(e)
                self._save_deferred()
                self.commit_soon(key)
                return
            if e["kind"] == "del":
                features.delete_stored(lyr, fid)
            else:
                features.write_state(lyr, fid, e["state"] or {})
            lyr.triggerRepaint()
        self.store.append(e)
        self._note(e)

    def _flush_deferred(self, key):
        waiting = self.deferred.pop(key, [])
        self._save_deferred()
        if waiting:
            self.apply_entries(waiting)

    def _save_deferred(self):
        # on disk: the stream position moves on past them, so a crash must not lose them
        self.store.put("deferred", self.deferred)

    def apply_entries(self, entries):
        """Write a pull's stream entries into this computer's copy at once: one read and one write per layer, one
        write to the log (one by one, a thousand entries froze the window for 14 s)."""
        i = 0
        while i < len(entries):
            e = entries[i]
            if e["kind"] == "field" or self.layers.get(e["layer"]) is None:
                self.apply_entry(e)          # a new field changes the layer itself: on its own, in order
                i += 1
                continue
            j = i
            while j < len(entries) and entries[j]["kind"] != "field" and self.layers.get(entries[j]["layer"]):
                j += 1
            self._apply_run(entries[i:j])
            i = j

    def _apply_run(self, run):
        done, changes, seen = [], {}, set()
        fids = {}
        for e in run:
            fids.setdefault(e["layer"], set()).add(int(e["fid"]))
        current = {key: features.stored_states(self.layers[key], f) for key, f in fids.items()}
        for e in run:
            key, fid = e["layer"], int(e["fid"])
            if e["uid"] in seen or self.store.has(e["uid"]):
                if e["author"] == self.me_id:
                    self.store.drop_outbox(e["uid"])
                continue
            lyr = self.layers[key]
            waiting = [w for w in self.deferred.get(key, []) if int(w["fid"]) == fid]
            if waiting or features.is_pending(lyr, fid):
                self.deferred.setdefault(key, []).append(e)
                self._save_deferred()
                self.commit_soon(key)
                continue
            ch = changes.setdefault(key, {})
            old = ch[fid][0] if fid in ch else current[key].get(fid)
            ch[fid] = (old, None if e["kind"] == "del" else (e["state"] or {}))
            seen.add(e["uid"])
            done.append(e)
        for key, ch in changes.items():
            lyr = self.layers[key]
            if not features.write_states(lyr, ch):
                for fid, (_old, new) in ch.items():   # one the file refused must not stop the others
                    if new is None:
                        features.delete_stored(lyr, fid)
                    else:
                        features.write_state(lyr, fid, new)
            lyr.triggerRepaint()
        if done:
            self.store.append_many(done)
            for e in done:
                self._note(e)

    def _note(self, e):
        """Remember where someone worked (their work area on the map) and tell the window."""
        lyr = self.layers.get(e["layer"])
        if e["author"] != self.me_id and lyr is not None:
            r = _rect_of(e.get("state") or e.get("prev"), lyr.crs(), QgsProject.instance().crs())
            if r is not None:
                lst = self.recent.setdefault(e["author"], [])
                lst.append((time.time(), r, e.get("name", "")))
                del lst[:-200]
        self.entry.emit(e)

    def zones(self):
        """{author id: (rectangle in map units, name)} - where others edited in the last minutes."""
        now = time.time()
        out = {}
        for author, items in self.recent.items():
            items[:] = [i for i in items if now - i[0] < ZONE_S]
            if not items:
                continue
            r = QgsRectangle(items[0][1])
            for _, rr, _ in items[1:]:
                r.combineExtentWith(rr)
            pad = max(r.width(), r.height()) * 0.08 or 5.0
            r.grow(pad)
            out[author] = (r, items[-1][2])
        return out

    def _check_zones(self, lyr, fids):
        zones = self.zones()
        if not zones:
            return
        crs = QgsProject.instance().crs()
        for fid in fids[:200]:
            f = lyr.getFeature(fid)
            if not f.isValid() or not f.hasGeometry():
                continue
            g = QgsGeometry(f.geometry())
            if lyr.crs() != crs:
                try:
                    g.transform(QgsCoordinateTransform(lyr.crs(), crs, QgsProject.instance()))
                except Exception:  # noqa: BLE001
                    continue
            box = g.boundingBox()
            for author, (r, name) in zones.items():
                if r.intersects(box) and time.time() - self._warned.get(author, 0) > 180:
                    self._warned[author] = time.time()
                    self.status.emit("zone", name)

    # ---- host ---------------------------------------------------------------------------------------------
    def start_host(self, epoch=None):
        self.stop_network()
        self.epoch = epoch or self.store.get("epoch", 1)
        self.store.put("epoch", self.epoch)
        if self.store.get("sid_epoch") != self.epoch or not self.store.get("sid"):
            # this host's stream: two hosts elected at once (two offices that lost the host together) have the
            # same epoch but not the same stream - a member meeting the other one joins it fully, it never just
            # reconnects and skips that stream's changes
            self.store.put("sid", uuid.uuid4().hex)
            self.store.put("sid_epoch", self.epoch)
        self.store.put("role", "host")
        me = self._me_member(uuid.uuid4().hex)
        me["rank"] = self.my_rank() if self.roster else 0
        self.store.put("rank", me["rank"])
        self.hub = Hub(self.store, self.keys, self.layers, me, self.epoch, self)
        self.hub.announce = lambda info: wire.sign_announce(self.ident["seed"], self.keys.tag, info)
        self.hub.commit_now = self.commit_soon
        self.hub.applied = self._note
        self.hub.joinRequested.connect(self.joinRequested)
        self.hub.deletesHeld.connect(self.deletesHeld)
        self.hub.changed.connect(self._hub_changed)
        self.hub.auto_accept = not self.store.get("approve", True)
        if not self.store.get("block"):
            self._new_block()
        if self.roster and not self.store.members():
            # taking over: the room's members (the old host among them) are let in again without asking; each
            # gets a new token when it says hello
            self.store.replace_members([
                {"id": m["id"], "name": m.get("name", ""), "pub": m.get("pub"), "rank": m.get("rank", 99),
                 "rights": m.get("rights") or {}, "token": None, "joined": time.time(), "seen": None,
                 "state": m.get("state") or "in"}
                for m in self.roster if m["id"] != self.me_id and m.get("pub")])
        self.hub.start()
        self.role = "host"
        self.member = me
        self.state = "host"
        self._drain_host_outbox()            # what this computer did while the room had no host
        self._tick.start()
        self._pres_timer.start()
        if self.store.get("external"):
            self.start_external()
        self.changed.emit()
        return self.hub.port

    def _drain_host_outbox(self):
        """Host: put this computer's waiting changes into the stream. A conflict is kept to be chosen; a change
        whose feature has unsaved edits waits for the next save (they were all dropped, whatever happened)."""
        if self.role != "host" or self.hub is None:
            return
        left = False
        for op in self.store.outbox(10000):
            r = self.hub.apply_remote(op, self.member)
            if r == "later":
                left = True
                continue
            if isinstance(r, dict) and r.get("conflicts"):
                self._keep_conflict(dict(r, layer=r.get("layer", op["layer"]), fid=r.get("fid", op["fid"])))
            self.store.drop_outbox(op["uid"])
        if left:
            QTimer.singleShot(2000, self._drain_host_outbox)
        self.changed.emit()

    def _hub_changed(self):
        if self.hub is not None:
            self.roster = self.hub.roster()
            self.store.put("roster", self.roster)
            now = time.time()
            self.presence = {k: v for k, v in self.hub.presence.items() if k != self.me_id and now - v["ts"] < 20}
        self.changed.emit()

    def start_external(self):
        """Let members outside this network join: a direct link between the computers (link.py) - nothing to
        download - its address ("link:") left (sealed) on ntfy. The Cloudflare tunnel stays for a room that asks
        for it ("tunnel" in its settings)."""
        self.store.put("external", True)
        if self.hub is None:
            return
        if not self.store.get("tunnel"):
            if self.link is None:
                try:
                    from . import link

                    self.link = link.HostLink(link.link_key(self.keys.key), self.hub.port)
                except Exception as e:  # noqa: BLE001
                    self._q.put(("external-failed", str(e)))
                    return
            self._q.put(("external", LINK))
            return
        if self.tunnel is not None:
            return

        def run():
            try:
                from ..field_photos import tunnel as tn
                exe = tn.find_exe()
                if exe is None:  # once: about 60 MB from Cloudflare's releases, its signature checked
                    self._q.put(("external_step", "download"))
                    exe = tn.download()
                t = tn.Tunnel(exe, self.hub.port, scheme="http")
                self.tunnel = t
                end = time.time() + 180
                while time.time() < end:
                    try:
                        kind, val = t.events.get(timeout=5)
                    except queue.Empty:
                        continue
                    if kind == "url":
                        self._q.put(("external", val))
                        return
                    if kind == "ended":
                        break
                self._q.put(("external-failed", "the tunnel did not open (check the internet connection)"))
            except Exception as e:  # noqa: BLE001
                self._q.put(("external-failed", str(e)))

        threading.Thread(target=run, daemon=True).start()

    def stop_external(self):
        self.store.put("external", False)
        self._stop_link()
        if self.tunnel is not None:
            self.tunnel.stop()
            self.tunnel = None
        self.external_url = None
        self.changed.emit()

    def _stop_link(self):
        if self.link is not None:
            self.link.stop()
            self.link = None
            if self.external_url == LINK:
                self.external_url = None

    def _publish(self):
        if not self.external_url or self.hub is None:
            return
        info = wire.sign_announce(self.ident["seed"], self.keys.tag, {
            "url": self.external_url, "epoch": self.epoch, "rank": self.member.get("rank", 0),
            "since": self.hub.started, "hid": self.me_id})
        threading.Thread(target=lambda: _quiet(wire.publish_address, self.keys, self.sealer, info),
                         daemon=True).start()
        self._published = time.time()

    # ---- member -------------------------------------------------------------------------------------------
    def start_member(self, address, member):
        self.stop_network()
        self.role = "member"
        self.address = address
        self.client = wire.Client(self.sealer, address)
        self.member = member
        self.store.put("role", "member")
        self.store.put("address", address)
        self.store.put("rank", member.get("rank", 99))
        self.state = "connected"
        self.last_ok = time.time()
        self._lost_since = None
        self._stop = threading.Event()
        self._sent_ok = set()
        self._threads = [threading.Thread(target=self._puller, daemon=True),
                         threading.Thread(target=self._pusher, daemon=True)]
        for t in self._threads:
            t.start()
        self._apply_rights(member.get("rights") or {})
        self._tick.start()
        self._pres_timer.start()
        self.changed.emit()

    def _puller(self):
        stop, client, token = self._stop, self.client, self.member["token"]
        since, rv, pts = self.since, 0, 0
        while not stop.is_set():
            try:
                r = client.call("pull", {"token": token, "since": since, "wait": 20, "rv": rv, "view": self._view,
                                         "pts": pts}, timeout=35)
            except wire.WireError as e:
                self._q.put(("lost", e.status))
                stop.wait(2)
                continue
            if stop.is_set():
                break
            if r.get("ops"):
                since = r["ops"][-1]["seq"]
            if r.get("members"):
                rv = r.get("rv", 0)
            pts = r.get("now", 0) - 0.5
            self._q.put(("pull", dict(r, since=since)))

    def _pusher(self):
        stop, client, token = self._stop, self.client, self.member["token"]
        while not stop.is_set():
            self._wake.wait(1.0)
            self._wake.clear()
            if stop.is_set():
                break
            p, self._presence_out = self._presence_out, None
            try:
                if p is not None:
                    client.call("presence", {"token": token, "p": p}, timeout=10)
                ops = next(by_size([o for o in self.store.outbox(100) if o["uid"] not in self._sent_ok]), [])
                if ops:
                    r = client.call("push", {"token": token, "ops": ops}, timeout=70)
                    self._q.put(("pushed", r))
            except wire.WireError as e:
                self._q.put(("lost", e.status))
                stop.wait(2)

    def stop_network(self):
        self._stop.set()
        self._wake.set()
        self._threads = []
        if self.hub is not None:
            self.hub.stop()
            self.hub.deleteLater()
            self.hub = None
        self._stop_link()
        if self.tunnel is not None:
            self.tunnel.stop()
            self.tunnel = None
            self.external_url = None

    def shutdown(self):
        for key in list(self.layers):
            try:
                self.commit_layer(key)
            except RuntimeError:
                pass
        if self.role == "member" and self.client is not None:
            token = self.member.get("token")
            # wait a moment for the outbox, then say goodbye
            end = time.time() + 3
            while time.time() < end:
                ops = next(by_size([o for o in self.store.outbox(100) if o["uid"] not in self._sent_ok]), [])
                if not ops:
                    break
                try:
                    self._on_pushed(self.client.call("push", {"token": token, "ops": ops}, timeout=5))
                except wire.WireError:
                    break
            _quiet(self.client.call, "bye", {"token": token}, timeout=3)
        self._tick.stop()
        self._pres_timer.stop()
        self.stop_network()
        if str(self.address or "").startswith(LINK):   # this computer's link to a host outside its network
            from . import link

            link.stop_member(self.keys.key)
        self.detach_layers()
        self.store.put("since", self.since)
        self.role = None
        self.state = "idle"

    # ---- the main thread: what the network threads brought ---------------------------------------------
    def _drain(self):
        for _ in range(50):
            try:
                kind, val = self._q.get_nowait()
            except queue.Empty:
                break
            try:
                getattr(self, "_on_" + kind.replace("-", "_"))(val)
            except Exception as e:  # noqa: BLE001 - keep the room going
                self.status.emit("warn", f"{kind}: {e}"[:300])
        if self.role == "member" and self.state != "connected" and time.time() - self._found_at > 3:
            self._look_for_host()
        if self.role == "host" and time.time() - self._checked > 30:
            self._checked = time.time()
            self._look_for_host(network_only=True)
        if self.role == "host" and self.external_url and time.time() - self._published > PUBLISH_S:
            self._publish()

    def _keep_settings(self, settings):
        for k, v in (settings or {}).items():
            if self.store.get(k) != v:
                self.store.put(k, v)

    def change_code(self, wait=20):
        """Host: a new room code. It is sealed to each member still in the room, then everyone switches to it at
        the same moment - a removed member keeps an old code that opens nothing. -> the new code."""
        if self.role != "host" or self.hub is None:
            return None
        from . import code as codes

        new = codes.new_code()
        at = time.time() + wait
        self.hub.seal_new_code(new, at)
        QTimer.singleShot(int(wait * 1000), lambda: self._switch_code(new))
        return new

    def _on_rekey(self, rk):
        """Member: the host sealed a new code for this computer; switch to it when the host does."""
        if self._rekey_at == rk.get("at"):
            return
        try:
            eph, blob = (bytes.fromhex(x) for x in rk["box"])
            new = crypto.box_open(self._box_priv, eph, blob, wire.rekey_aad(self.me_id)).decode("utf-8")
        except Exception:  # noqa: BLE001 - not for this computer, or altered
            return
        self._rekey_at = rk.get("at")
        QTimer.singleShot(max(0, int((float(rk["at"]) - time.time()) * 1000)) + 500, lambda: self._switch_code(new))

    def _switch_code(self, new):
        from . import code as codes
        from . import link
        from .session import _protect

        role = self.role
        self.stop_network()
        link.stop_member(self.keys.key)   # a link under the old code reaches nothing any more
        self.keys = codes.RoomKeys(new)
        self.sealer = wire.Sealer(self.keys.key)
        self.store.put("code", _protect(self.keys.code))
        self.status.emit("ok", "new-code")
        if role == "host":
            self.start_host(self.epoch)   # the same stream, now under the new code
        else:
            self.role = "member"
            self.state = "reconnecting"
            self._lost_since = time.time()
            self._tick.start()
            self._look_for_host()
        self.changed.emit()

    def _on_pull(self, r):
        self._keep_settings(r.get("settings"))
        if self.store.get("pigeon_only"):  # the host is reachable again: the usual take-over comes back
            self.store.put("pigeon_only", False)
        if r.get("refused"):   # deletions the host held and then refused: the features come back here
            for uid in r["refused"]:
                self._revert(uid)
                self.store.drop_outbox(uid)
                self._sent_ok.discard(uid)
            self._held_said = False
            self.status.emit("warn", "deletes-refused")
        if r.get("rekey"):
            self._on_rekey(r["rekey"])
        self.last_ok = time.time()
        self._lost_since = None
        self.was_connected = True
        if self.state != "connected":
            self.state = "connected"
            self.status.emit("ok", "back")
        if int(r.get("epoch", self.epoch)) != self.epoch and r.get("epoch", 0) > self.epoch:
            self.epoch = int(r["epoch"])
            self.store.put("epoch", self.epoch)
        self.apply_entries(r.get("ops", []))
        self.since = r.get("since", self.since)
        self.store.put("since", self.since)
        now = time.time()  # the pointers that moved; the others stay until they are old
        self.presence.update(r.get("presence") or {})
        for k, v in list(self.presence.items()):
            v.setdefault("seen", now)
            if k in (r.get("presence") or {}):
                v["seen"] = now
            elif now - v["seen"] > 20:
                del self.presence[k]
        notice = r.get("notice")
        if notice and notice.get("ts") != self.store.get("notice_ts"):
            self.store.put("notice_ts", notice.get("ts"))
            self.status.emit("notice", notice.get("text", ""))
        if bool(r.get("frozen")) != getattr(self, "frozen", False):
            self.frozen = bool(r.get("frozen"))
            self._apply_rights((self.member or {}).get("rights") or {})
            self.status.emit("warn", "frozen" if self.frozen else "unfrozen")
        if r.get("members"):
            self.roster = r["members"]
            self.store.put("roster", self.roster)
        if r.get("rights") is not None and r["rights"] != (self.member or {}).get("rights"):
            self.member["rights"] = r["rights"]
            self._apply_rights(r["rights"])
        self.changed.emit()

    def _on_pushed(self, r):
        self.last_ok = time.time()
        for x in r.get("results", []):
            st = x.get("status")
            if st == "held":                       # waits for the host's answer: not sent again meanwhile
                self._sent_ok.add(x["uid"])
                if not self._held_said:
                    self._held_said = True
                    self.status.emit("warn", "deletes-held")
            if st in ("ok", "merged"):
                self._sent_ok.add(x["uid"])
                if x.get("dup") or x.get("gone"):   # never in the stream again: it would be sent forever
                    self.store.drop_outbox(x["uid"])
            if x.get("conflicts"):
                self._keep_conflict(x)
            if st in ("conflict", "refused"):
                if st == "refused":
                    self._revert(x.get("uid"))
                    self.status.emit("warn", "refused:" + str(x.get("why", "")))
                self.store.drop_outbox(x.get("uid"))
        self.changed.emit()

    def _revert(self, uid):
        """A change the host refused (view only, frozen…): this computer's copy goes back to the room's, instead
        of keeping a feature nobody else has."""
        op = self.store.outbox_op(uid) if uid else None
        if not op or op.get("kind") == "field":
            return
        lyr = self.layers.get(op["layer"])
        fid = int(op["fid"])
        if lyr is None or features.is_pending(lyr, fid):
            return
        if op.get("base") is None:
            features.delete_stored(lyr, fid)
        else:
            features.write_state(lyr, fid, op["base"])
        lyr.triggerRepaint()

    def _keep_conflict(self, x):
        c = {"uid": x["uid"], "layer": x.get("layer"), "fid": x.get("fid"), "other": x.get("other", ""),
             "items": x["conflicts"], "theirs": x.get("theirs"), "mine": x.get("mine"),
             "applied": x.get("status") == "merged"}
        self.store.add_conflict(c)
        self.status.emit("conflict", x.get("other", ""))

    def _on_lost(self, status):
        if status == 401:  # the host does not know this computer any more (removed, or another host)
            self._lost_since = self._lost_since or 0
        if self.state == "connected" and time.time() - self.last_ok > LOST_S:
            self.state = "reconnecting"
            self._lost_since = time.time()
            self.status.emit("warn", "lost")
            self.changed.emit()

    def _look_for_host(self, network_only=False):
        if self._finding:
            return
        self._finding = True

        def find():
            infos = wire.find_on_network(self.keys, self.sealer)
            if not infos and not network_only:
                infos = wire.find_published(self.keys, self.sealer, since="1h")
            self._q.put(("found", infos))

        threading.Thread(target=find, daemon=True).start()

    def _on_found(self, infos):
        self._finding = False
        self._found_at = time.time()
        # only hosts signed by a member of this room's roster who was not removed: the room code alone is not
        # enough to become the host (members would follow it and take its copy of the map)
        roster = self.store.members() or self.roster
        infos = [i for i in infos if i.get("address") and i.get("hid") != self.me_id
                 and wire.announce_ok(self.keys.tag, i, roster if self.role == "host" else self.roster)]
        # the host that more members reach wins (a laptop alone at home never takes the room from the office),
        # then the later epoch, then the earlier place in the join order
        infos.sort(key=lambda i: (-int(i.get("on", 0)), -int(i.get("epoch", 0)), int(i.get("rank", 99))))
        if self.role == "host":
            if infos and self.hub is not None:
                mine = self.hub.where()
                best = infos[0]
                if (best.get("on", 0), best["epoch"], -best.get("rank", 99)) > (mine["on"], self.epoch,
                                                                                  -self.my_rank()):
                    self.status.emit("warn", "handover")
                    self.rejoin(best)
            return
        if infos:
            best = infos[0]
            if int(best.get("epoch", 0)) == self.epoch and best.get("sid") in (None, self.store.get("sid")):
                if self.client is not None and best["address"] == self.address:
                    return  # the same host; the puller reconnects by itself
                self.reconnect(best)  # the same room's host at another address, or this map was just opened
                return
            self.rejoin(best)
            return
        if not self.was_connected:
            return  # opened away from the room: keep working; the edits wait for the host
        if self.store.get("pigeon_only"):
            return  # the room travels by carrier pigeon here: the host is far, not gone; never take the room over
        lost = time.time() - (self._lost_since or time.time())
        others = sorted([m for m in self.roster if not m.get("host") and m.get("state") != "out"],
                        key=lambda m: m.get("rank", 99))
        place = next((i for i, m in enumerate(others) if m["id"] == self.me_id), len(others))
        if lost > ELECTION_S + place * ELECTION_STEP:
            self.status.emit("ok", "took-over")
            self.start_host(self.epoch + 1)

    def check_other_hosts(self):
        """Host: is someone else holding this room? (run now and then)"""
        if self.role == "host" and not self._finding:
            self._look_for_host()

    def reconnect(self, info):
        """Follow the same host again (after opening the map, or a new address): no copy to refresh."""
        if getattr(self, "_reconnecting", False):
            return
        self._reconnecting = True
        client = wire.Client(self.sealer, info["address"])
        hello = {"id": self.ident["id"], "name": self.ident["name"], "pub": self.pub, "xpub": self.xpub}

        def run():
            try:
                r = wire.say_hello(client, hello, self.ident["seed"], self.keys.tag)
                self._q.put(("hello", (info, r)))
            except Exception as e:  # noqa: BLE001
                self._q.put(("hello", (info, {"status": "error", "why": str(e)})))

        threading.Thread(target=run, daemon=True).start()

    def _on_hello(self, args):
        info, r = args
        self._reconnecting = False
        if r.get("status") != "ok":
            if r.get("status") == "refused":
                self.status.emit("warn", "refused:" + str(r.get("why", "")))
            return
        if int(r["room"].get("epoch", 1)) != self.epoch or \
                (r["room"].get("sid") and r["room"].get("sid") != self.store.get("sid")):
            self.rejoin(info)
            return
        self._keep_settings(r["room"].get("settings"))
        self.roster = r["room"].get("members", self.roster)
        self.store.put("roster", self.roster)
        if not self.store.get("block"):
            self._new_block()
        self.start_member(info["address"], r["member"])
        self.status.emit("ok", "back")

    def rejoin(self, info):
        """Join the room's (new) host: send what it lacks, then refresh this computer's copy."""
        self.stop_network()
        self.role = "member"
        self.state = "rejoining"
        self._tick.start()
        self._pres_timer.start()
        self.changed.emit()
        client = wire.Client(self.sealer, info["address"])
        ident = self.ident

        def run():
            try:
                hello = {"id": ident["id"], "name": ident["name"], "pub": self.pub, "xpub": self.xpub}
                while True:
                    r = wire.say_hello(client, hello, ident["seed"], self.keys.tag)
                    if r.get("status") == "ok":
                        break
                    if r.get("status") == "refused":
                        self._q.put(("rejoin_failed", r.get("why", "refused")))
                        return
                    time.sleep(2)
                token = r["member"]["token"]
                # every change this computer has that the new host lacks, page by page (only the last 3000
                # were sent: days of work apart were lost)
                seq = 0
                while True:
                    page = self.store.since(seq, 2000)
                    if not page:
                        break
                    seq = page[-1]["seq"]
                    missing = set(client.call("have", {"token": token, "uids": [e["uid"] for e in page]})["missing"])
                    replay = [{"uid": e["uid"], "layer": e["layer"], "fid": e["fid"], "kind": e["kind"],
                               "base_ver": max(0, (e["ver"] or 1) - 1), "base": e["prev"], "state": e["state"],
                               "author": e["author"], "name": e["name"], "ts": e["ts"], "sig": e["sig"],
                               "src": e.get("src"), "replay": True}
                              for e in page if e["uid"] in missing and e["kind"] != "field" and ":n:" not in e["uid"]]
                    for part in by_size(replay):
                        client.call("push", {"token": token, "ops": part}, timeout=120)
                own = self.store.outbox(10000)
                results = []
                for part in by_size(own):
                    results += client.call("push", {"token": token, "ops": part}, timeout=120).get("results", [])
                snap = download_snapshot(client, token, self.room_folder(), lambda *a: None)
                self._q.put(("resync", (info, r, snap, results)))
            except Exception as e:  # noqa: BLE001
                self._q.put(("rejoin_failed", str(e)))

        threading.Thread(target=run, daemon=True).start()

    def _on_rejoin_failed(self, why):
        self.state = "reconnecting"
        self._lost_since = self._lost_since or time.time()
        self.status.emit("warn", "rejoin:" + str(why))
        self._tick.start()

    def _on_resync(self, args):
        info, hello, snap, results = args
        point_layers_at(self, snap)
        self.store.put("room_name", hello["room"].get("name", ""))
        self.epoch = int(hello["room"].get("epoch", 1))
        self.store.put("epoch", self.epoch)
        self.store.put("sid", hello["room"].get("sid"))   # the stream this computer now follows
        self._keep_settings(hello["room"].get("settings"))
        self.since = snap["head"]
        self.store.put("since", self.since)
        # sent above: those the host took are in the new copy; conflicts are kept to be chosen, a refused change
        # is undone here, one waiting for a save stays to be sent again
        self._on_pushed({"results": results})
        for x in results:
            if x.get("status") in ("ok", "merged"):
                self.store.drop_outbox(x.get("uid"))
        if not self.store.get("block"):   # this computer's own ids stay its own across hosts
            self._new_block()
        self.roster = hello["room"].get("members", [])
        self.store.put("roster", self.roster)
        self.start_member(info["address"], hello["member"])
        self.status.emit("ok", "rejoined")

    def _on_external(self, url):
        self.external_url = url
        self._publish()
        self.changed.emit()

    def _on_external_step(self, step):
        self.status.emit("info", "external-download")

    def _on_external_failed(self, why):
        self.status.emit("warn", "external:" + why)

    def _apply_rights(self, rights):
        for key, lyr in self.layers.items():
            view = rights.get(key, "edit") == "view" or (getattr(self, "frozen", False) and self.role != "host")
            if view and lyr.isEditable():
                self.commit_layer(key)
                lyr.commitChanges(True)
            lyr.setReadOnly(view)

    # ---- where everyone is ----------------------------------------------------------------------------
    def set_cursor(self, pt):
        self._cursor = (pt.x(), pt.y())

    def _presence_tick(self):
        from qgis.utils import iface
        ext = None
        try:
            e = iface.mapCanvas().extent()
            ext = [e.xMinimum(), e.yMinimum(), e.xMaximum(), e.yMaximum()]
            self._view = ext
        except Exception:  # noqa: BLE001 - no window (tests)
            pass
        editing = [k for k, lyr in self.layers.items() if lyr.isEditable()]
        p = {"c": self._cursor, "e": ext, "edit": editing}
        if p == self._last_presence:
            return
        self._last_presence = p
        if self.role == "host" and self.hub is not None:
            self.hub.presence[self.me_id] = dict(p, ts=time.time(), name=self.ident["name"])
        elif self.role == "member":
            self._presence_out = p
            self._wake.set()

    # ---- undo, history, conflicts ------------------------------------------------------------------------
    def undo_last(self):
        last = self.store.last_by(self.me_id, 1)
        if not last:
            return None
        e = last[0]
        if e["kind"] == "field":
            return None
        lyr = self.layers.get(e["layer"])
        cur = features.stored_state(lyr, int(e["fid"])) if lyr is not None else None
        if cur == e["state"]:
            target = e["prev"]                     # nobody changed it since: back as it was
        elif e["prev"] is None or e["state"] is None or cur is None:
            self.status.emit("warn", "undo-changed")   # added or deleted, and changed by someone since
            return None
        else:
            # someone changed it after me: take back only my change and keep theirs (it was overwritten)
            from .merge import merge_feature

            target, _conflicts = merge_feature(e["state"], e["prev"], cur)
        self.submit(e["layer"], e["fid"], target, note="undo")
        return e

    def revert_member(self, member_id, since):
        """Host: take back what a member changed from time ``since`` on. Each feature goes back to how it was before
        that member's first change in the period; what others changed in it afterwards is kept (joined, as the
        Undo does). -> (features taken back, features left because others changed them too much)."""
        from .merge import merge_feature

        if self.role != "host":
            return 0, 0
        first, last = {}, {}
        for e in self.store.by_author_since(member_id, since):
            k = (e["layer"], int(e["fid"]))
            first.setdefault(k, e)
            last[k] = e
        done = left = 0
        for (key, fid), e0 in first.items():
            lyr = self.layers.get(key)
            if lyr is None:
                continue
            before, after = e0["prev"], last[(key, fid)]["state"]
            cur = features.stored_state(lyr, fid)
            if cur == after:
                target = before
            elif after is None:                       # deleted by the member, and someone brought it back since
                left += 1
                continue
            elif before is None or cur is None:       # added by the member and changed, or deleted, by others
                left += 1
                continue
            else:
                target, _c = merge_feature(after, before, cur)
            if target == cur:
                continue
            if self.submit(key, fid, target, note="revert"):
                done += 1
        return done, left

    def resolve(self, conflict, keep_mine):
        if keep_mine:
            lyr = self.layers.get(conflict["layer"])
            if lyr is not None:
                cur = features.stored_state(lyr, conflict["fid"]) or {"g": None, "a": {}}
                new = {"g": cur.get("g"), "a": dict(cur.get("a") or {})}
                for item in conflict.get("items", []):
                    if item["what"] == "field":
                        new["a"][item["field"]] = item["mine"]
                    elif item["what"] in ("geometry", "deleted"):
                        if item.get("nodes") and cur.get("g"):
                            from . import merge
                            moves = [((n["theirs"][0], n["theirs"][1]), (n["mine"][0], n["mine"][1]))
                                     for n in item["nodes"] if n.get("theirs") and n.get("mine")]
                            new_g, cnt = merge.move_nodes(cur["g"], moves)
                            new["g"] = new_g if cnt > 0 else item["mine"]
                        else:
                            new["g"] = item["mine"]
                        if item["what"] == "deleted":
                            new["a"].update((conflict.get("mine") or {}).get("a") or {})
                    elif item["what"] == "delete":
                        new = None
                        break
                self.submit(conflict["layer"], conflict["fid"], new, note="mine")
        self.store.resolve_conflict(conflict["id"])
        self.changed.emit()

    def room_folder(self):
        return os.path.dirname(os.path.dirname(os.path.dirname(self.store.path)))


def _quiet(fn, *a, **k):
    try:
        return fn(*a, **k)
    except Exception:  # noqa: BLE001
        return None


# ---- a member's copy of the room's files ------------------------------------------------------------------
def by_size(ops, count=100, size=16 << 20):
    """The changes in parts of at most ``count`` and about ``size`` bytes (a part always holds one): a hundred
    changes of very large shapes passed the host's limit for one message and were refused every time, so that
    computer's outbox never emptied."""
    part, total = [], 0
    for op in ops:
        n = len(json.dumps(op))
        if part and (len(part) >= count or total + n > size):
            yield part
            part, total = [], 0
        part.append(op)
        total += n
    if part:
        yield part


def field_changes(before, after):
    """[{"field": {"name"}, "action": "rename" | "delete", "new"}] between two lists of field names (a save). A
    name replaced at the same place by a name that was not there is a rename; a name gone is a delete. Added
    fields are sent on their own (with their type)."""
    if not before:
        return []
    out = []
    for i, old in enumerate(before):
        if old in after:
            continue
        new = after[i] if i < len(after) else None
        if new and new not in before:
            out.append({"field": {"name": old}, "action": "rename", "new": new})
        else:
            out.append({"field": {"name": old}, "action": "delete"})
    return out

def download_snapshot(client, token, folder, progress, cancelled=lambda: False):
    """The room's files at one point of the stream into a new folder under folder/data."""
    info = client.call("snapshot", {"token": token}, timeout=320)
    sub = os.path.join(folder, "data", time.strftime("%Y%m%d-%H%M%S"))
    os.makedirs(sub, exist_ok=True)
    total = sum(info["files"].values()) or 1
    done = 0
    from .snapshot import inside, safe_relative

    for name, size in sorted(info["files"].items()):
        dest = inside(sub, name)          # the host names the files: never outside this folder
        if dest is None:
            raise wire.WireError(f"the room's copy has an unsafe file name: {name!r}")
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        base = done
        client.download("piece", {"token": token, "name": name, "snap": info.get("snap")}, dest,
                        progress=lambda d, s, b=base: progress(b + d, total), cancelled=cancelled)
        done += size
    with open(os.path.join(sub, "versions.json"), encoding="utf-8") as f:
        v = json.load(f)
    files = {k: n for k, n in (v.get("layer_files") or {}).items() if safe_relative(n)}
    extra = {k: n for k, n in (v.get("extra") or {}).items() if safe_relative(n)}
    return {"folder": sub, "head": v["head"], "versions": v["versions"], "layer_files": files, "extra": extra}


def point_layers_at(node, snap):
    """Point the shared layers at a fresh copy (after a change of host) and take its versions."""
    for key, lyr in node.layers.items():
        if lyr.isEditable():
            node.commit_layer(key)
            lyr.commitChanges(True)
        table = (node.store.get("layers", {}).get(key) or {}).get("table", "")
        from .snapshot import inside

        path = inside(snap["folder"], snap["layer_files"].get(key) or "")
        if not path:
            continue
        uri = path.replace("\\", "/") + "|layername=" + table
        lyr.setDataSource(uri, lyr.name(), "ogr")
        lyr.triggerRepaint()
    node.store.con.execute("DELETE FROM versions")
    node.store.set_versions([tuple(v) for v in snap["versions"]])
