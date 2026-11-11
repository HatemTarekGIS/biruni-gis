"""«جدار من الموبايلات»: several phones side by side show one big map.

The PC keeps a wall per dashboard (rows × cols, a bezel gap in CSS pixels). Each phone that joins gets a slot
(row-major); the wall is laid out from the phones' own CSS sizes: a column is as wide as its widest phone, a row as
tall as its tallest, and the gaps between them are the phones' frames (bezel). A phone's tile is its rectangle on
the virtual canvas: (x, y, w, h).

The shared view is the virtual canvas's centre (absolute Web Mercator metres) and its scale (metres per CSS
pixel); each phone shows the part under its tile:
    centre of phone = (cx + (tile.x + tile.w/2 - W/2)·mpp,  cy - (tile.y + tile.h/2 - H/2)·mpp)
so the map continues across the phones as if they were one screen.
"""
import secrets

MAX_PHONES = 24


def layout(devices, rows, cols, bezel=16):
    """devices: {slot: (w, h)} -> ({slot: (x, y, w, h)}, (W, H)). Empty cells take the average phone's size."""
    rows, cols = max(1, int(rows)), max(1, int(cols))
    sizes = [v for v in devices.values()]
    aw = sum(w for w, _h in sizes) / len(sizes) if sizes else 400
    ah = sum(h for _w, h in sizes) / len(sizes) if sizes else 800
    colw = [aw] * cols
    rowh = [ah] * rows
    for c in range(cols):
        ws = [devices[s][0] for s in devices if s % cols == c and s // cols < rows]
        if ws:
            colw[c] = max(ws)
    for r in range(rows):
        hs = [devices[s][1] for s in devices if s // cols == r]
        if hs:
            rowh[r] = max(hs)
    tiles = {}
    for s, (w, h) in devices.items():
        r, c = s // cols, s % cols
        if r >= rows:
            continue
        x = sum(colw[:c]) + c * bezel + (colw[c] - w) / 2
        y = sum(rowh[:r]) + r * bezel + (rowh[r] - h) / 2
        tiles[s] = (round(x, 2), round(y, 2), w, h)
    W = sum(colw) + (cols - 1) * bezel
    H = sum(rowh) + (rows - 1) * bezel
    return tiles, (round(W, 2), round(H, 2))


def phone_view(view, tile, virtual):
    """The shared view {cx, cy, mpp} -> this phone's {cx, cy, mpp} (the same scale, its own centre)."""
    x, y, w, h = tile
    W, H = virtual
    return {"cx": view["cx"] + (x + w / 2 - W / 2) * view["mpp"],
            "cy": view["cy"] - (y + h / 2 - H / 2) * view["mpp"], "mpp": view["mpp"]}


class Wall:
    def __init__(self, dash, rows=1, cols=2, bezel=16):
        self.id = secrets.token_hex(4)
        self.dash = dash
        self.rows, self.cols, self.bezel = int(rows), int(cols), float(bezel)
        self.members = {}          # session id -> {"slot", "w", "h", "dpr", "session"}
        self.view = None

    def capacity(self):
        return min(MAX_PHONES, self.rows * self.cols)

    def join(self, session, w, h, dpr=1.0):
        w = max(200.0, min(3000.0, float(w)))
        h = max(200.0, min(3000.0, float(h)))
        m = self.members.get(session.id)
        if m is None:
            taken = {x["slot"] for x in self.members.values()}
            free = [i for i in range(self.capacity()) if i not in taken]
            if not free:
                return None
            m = self.members[session.id] = {"slot": free[0], "session": session}
        m.update(w=w, h=h, dpr=float(dpr or 1))
        return m["slot"]

    def move(self, session, slot):
        """Put this phone in another slot (swapping with the phone there)."""
        m = self.members.get(session.id)
        slot = int(slot)
        if m is None or not 0 <= slot < self.capacity():
            return False
        other = next((x for x in self.members.values() if x["slot"] == slot), None)
        if other is not None:
            other["slot"] = m["slot"]
        m["slot"] = slot
        return True

    def leave(self, session):
        self.members.pop(session.id, None)

    def tiles(self):
        devs = {m["slot"]: (m["w"], m["h"]) for m in self.members.values()}
        return layout(devs, self.rows, self.cols, self.bezel)

    def messages(self):
        """[(session, message)] telling each phone its tile."""
        tiles, virt = self.tiles()
        out = []
        for m in self.members.values():
            t = tiles.get(m["slot"])
            if t is None:
                continue
            out.append((m["session"], {"t": "show_wall", "wall": self.id, "slot": m["slot"], "rows": self.rows,
                                       "cols": self.cols, "tile": list(t), "virtual": list(virt),
                                       "n": len(self.members), "view": self.view}))
        return out
