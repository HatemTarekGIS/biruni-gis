"""In-process Google Drive / cloud-sync simulator for folder-carrier tests."""
import os
import re
import shutil


class DriveSim:
    """Replicate a room folder between per-computer views with controllable sync faults."""

    def __init__(self, views):
        self.views = {name: os.path.abspath(path) for name, path in views.items()}
        self._delays = {}
        self._hold_re = None
        self._truncate_re = None
        self._dup_re = None
        self._heads_first = False
        self._absent = {}
        self._trashed = set()
        self._queue = []
        self._step_n = 0
        self._real_read = None
        self._seen = {name: self._snapshot(name) for name in self.views}

    def delay(self, src, dst, steps):
        self._delays[(src, dst)] = max(0, int(steps))

    def hold(self, regex):
        self._hold_re = re.compile(regex) if regex else None

    def truncate(self, regex):
        self._truncate_re = re.compile(regex) if regex else None

    def duplicate(self, regex):
        self._dup_re = re.compile(regex) if regex else None

    def heads_first(self, enabled):
        self._heads_first = bool(enabled)

    def unreadable(self, regex):
        from biruni.team import folder as rf

        if self._real_read is None:
            self._real_read = rf._read
        if not regex:
            rf._read = self._real_read
            return
        pat = re.compile(regex)
        real = self._real_read

        def guarded(path):
            if pat.search(path.replace("\\", "/")):
                raise OSError("simulated unreadable file")
            return real(path)

        rf._read = guarded

    def trash(self, view):
        self._trashed.add(view)
        p = self.views[view]
        if os.path.isdir(p):
            for e in list(os.scandir(p)):
                if e.is_dir() and e.name.startswith("biruni-room-"):
                    shutil.rmtree(e.path)
        self._seen[view] = {}
        self._queue = [q for q in self._queue if q["dst"] != view]

    def absent(self, view):
        if view in self._absent:
            return
        p = self.views[view]
        stash = p + ".__absent__"
        if os.path.isdir(p):
            if os.path.exists(stash):
                shutil.rmtree(stash)
            os.rename(p, stash)
            self._absent[view] = stash
        else:
            self._absent[view] = None

    def present(self, view):
        if view not in self._absent:
            return
        stash = self._absent.pop(view)
        p = self.views[view]
        if stash and os.path.isdir(stash):
            if os.path.isdir(p):
                shutil.rmtree(p)
            os.rename(stash, p)
        else:
            os.makedirs(p, exist_ok=True)

    def _snapshot(self, view):
        if view in self._absent or view in self._trashed:
            return None
        parent = self.views[view]
        if not os.path.isdir(parent):
            return {}
        out = {}
        for e in os.scandir(parent):
            if not e.is_dir() or not e.name.startswith("biruni-room-"):
                continue
            for root, _dirs, files in os.walk(e.path):
                for fname in files:
                    if fname.endswith(".tmp"):
                        continue
                    full = os.path.join(root, fname)
                    rel = os.path.relpath(full, parent).replace("\\", "/")
                    try:
                        with open(full, "rb") as f:
                            out[rel] = f.read()
                    except OSError:
                        pass
        return out

    def step(self):
        self._step_n += 1
        for src in self.views:
            cur = self._snapshot(src)
            if cur is None:
                continue
            prev = self._seen.get(src) or {}
            changes = []
            for rel, data in cur.items():
                if rel not in prev or prev[rel] != data:
                    changes.append(("put", rel, data))
            for rel in prev:
                if rel not in cur:
                    changes.append(("del", rel, None))
            self._seen[src] = cur
            for dst in self.views:
                if dst == src or dst in self._trashed:
                    continue
                at = self._step_n + self._delays.get((src, dst), 0)
                for op, rel, data in changes:
                    self._queue.append({"at": at, "src": src, "dst": dst, "op": op, "rel": rel, "data": data})

        ready, remaining = [], []
        for item in self._queue:
            dst = item["dst"]
            if dst in self._trashed:
                continue
            if item["at"] > self._step_n or dst in self._absent:
                remaining.append(item)
                continue
            if self._hold_re and self._hold_re.search(item["rel"]):
                remaining.append(item)
                continue
            ready.append(item)

        if self._heads_first:
            # Deliver heads in this step and defer .bpf packs by 1 step when heads are in the same batch.
            has_head = {it["dst"] for it in ready if it["rel"].endswith(".bph")}
            now_ready = []
            for it in ready:
                if it["dst"] in has_head and it["rel"].endswith(".bpf") and it["op"] == "put":
                    it = dict(it, at=self._step_n + 1)
                    remaining.append(it)
                else:
                    now_ready.append(it)
            ready = now_ready
            ready.sort(key=lambda x: (0 if x["rel"].endswith(".bph") else 1))
        else:
            ready.sort(key=lambda x: (0 if x["rel"].endswith(".bpr") else (1 if x["rel"].endswith(".bpf") else 2)))

        for item in ready:
            dst = item["dst"]
            rel = item["rel"]
            parent = self.views[dst]
            if item["op"] == "put":
                data = item["data"]
                if self._dup_re and self._dup_re.search(rel):
                    stem, ext = os.path.splitext(rel)
                    dup_rel = f"{stem} (1){ext}"
                    dest = os.path.join(parent, *dup_rel.split("/"))
                    os.makedirs(os.path.dirname(dest), exist_ok=True)
                    with open(dest, "wb") as f:
                        f.write(data)
                    self._seen.setdefault(dst, {})[dup_rel] = data
                    continue
                if self._truncate_re and self._truncate_re.search(rel):
                    part = data[: max(1, len(data) // 2)]
                    dest = os.path.join(parent, *rel.split("/"))
                    os.makedirs(os.path.dirname(dest), exist_ok=True)
                    with open(dest, "wb") as f:
                        f.write(part)
                    self._seen.setdefault(dst, {})[rel] = part
                    remaining.append(dict(item, at=self._step_n + 1))
                    continue
                dest = os.path.join(parent, *rel.split("/"))
                os.makedirs(os.path.dirname(dest), exist_ok=True)
                with open(dest, "wb") as f:
                    f.write(data)
                self._seen.setdefault(dst, {})[rel] = data
            else:
                dest = os.path.join(parent, *rel.split("/"))
                if os.path.exists(dest):
                    try:
                        os.remove(dest)
                    except OSError:
                        pass
                (self._seen.get(dst) or {}).pop(rel, None)

        self._queue = remaining
