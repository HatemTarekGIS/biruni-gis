"""Team vault (team_vault/): Reed-Solomon any-k-of-n, Shamir threshold, tamper detection, content-defined dedup,
a room of computers losing members (and the owner's own PC) then restoring and repairing, folders, speed, window."""
import hashlib
import io
import itertools
import json
import os
import random
import shutil
import struct
import sys
import tempfile
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
TMP = tempfile.mkdtemp(prefix="biruni-vault-test-")
os.environ["QGIS_CUSTOM_CONFIG_PATH"] = os.path.join(TMP, "profile")
os.makedirs(os.environ["QGIS_CUSTOM_CONFIG_PATH"], exist_ok=True)
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "app", "plugins"))

from qgis.core import QgsApplication  # noqa: E402

app = QgsApplication([], True)
app.initQgis()

import numpy as np  # noqa: E402

from biruni import team_vault  # noqa: E402
from biruni.team_vault import core, net, rs, shamir, store  # noqa: E402

failures = []


def check(cond, msg):
    print(("PASS " if cond else "FAIL ") + msg, flush=True)
    if not cond:
        failures.append(msg)


def raises(fn, exc=Exception):
    try:
        fn()
    except exc:
        return True
    except Exception:  # noqa: BLE001
        return False
    return False


rnd = random.Random(7)

# ---- Reed-Solomon -------------------------------------------------------------------------------------------------
ok = True
for k, n in ((1, 1), (1, 3), (3, 5), (4, 10), (10, 16), (2, 255)):
    for size in (0, 1, 7, 1000, 65537):
        data = os.urandom(size)
        sh = rs.encode(data, k, n)
        for _ in range(6):
            pick = rnd.sample(range(n), k)
            if rs.decode({i: sh[i] for i in pick}, k, n, size) != data:
                ok = False
        if k > 1 and not raises(lambda: rs.decode({i: sh[i] for i in range(k - 1)}, k, n, size), rs.RSError):
            ok = False
        lost = rnd.sample(range(n), n - k)
        left = {i: sh[i] for i in range(n) if i not in lost}
        if lost and rs.rebuild(left, k, n, lost) != {i: sh[i] for i in lost}:
            ok = False
check(ok, "Reed-Solomon: any k of n shards (random erasures) give the data back, k-1 do not, lost shards rebuild "
      "exactly")
ok = True
for k, n in ((3, 5), (4, 7)):
    data = os.urandom(4099)
    sh = rs.encode(data, k, n)
    for pick in itertools.combinations(range(n), k):
        ok = ok and rs.decode({i: sh[i] for i in pick}, k, n, len(data)) == data
check(ok, "Reed-Solomon: every k-subset works (3-of-5: 10 subsets, 4-of-7: 35)")

# ---- Shamir -------------------------------------------------------------------------------------------------------
secret = os.urandom(32)
shares = shamir.split(secret, 3, 5)
check(all(shamir.combine([shares[i] for i in p]) == secret for p in itertools.combinations(range(5), 3)),
      "Shamir: any 3 of 5 shares give the key")
check(shamir.combine(shares[:2]) != secret, "Shamir: 2 shares do not give the key")
check(shamir.extend([shares[0], shares[2], shares[4]], 2) == shares[1][1], "Shamir: a lost share is rebuilt exactly")
hA, hB = np.zeros(256), np.zeros(256)
A, B = bytes(32), b"\xff" * 32
for _ in range(1500):
    for (_x, s) in shamir.split(A, 3, 5)[:2]:
        hA += np.bincount(np.frombuffer(s, np.uint8), minlength=256)
    for (_x, s) in shamir.split(B, 3, 5)[:2]:
        hB += np.bincount(np.frombuffer(s, np.uint8), minlength=256)
exp = hA.sum() / 256
chiA, chiB = ((hA - exp) ** 2 / exp).sum(), ((hB - exp) ** 2 / exp).sum()
check(chiA < 360 and chiB < 360, "Shamir: k-1 shares look uniformly random whatever the secret (chi² %.0f / %.0f, "
      "255 dof)" % (chiA, chiB))

# ---- chunking and dedup -------------------------------------------------------------------------------------------
SEED = hashlib.sha256(b"owner").digest()
OTHER = hashlib.sha256(b"someone else").digest()
work = os.path.join(TMP, "work")
proj = os.path.join(work, "survey")
os.makedirs(os.path.join(proj, "photos"))


def blob(n, seed):
    r = np.random.RandomState(seed)
    a = r.randint(0, 256, n, dtype=np.uint8)
    a[: n // 3] = r.randint(0, 6, n // 3, dtype=np.uint8)  # a third compresses well, like a GeoPackage
    return a.tobytes()


big = blob(8 << 20, 1)
with open(os.path.join(proj, "parcels.gpkg"), "wb") as f:
    f.write(big)
with open(os.path.join(proj, "map.qgz"), "wb") as f:
    f.write(b"<qgis>" + b"x" * 5000 + b"</qgis>")
with open(os.path.join(proj, "photos", "p1.jpg"), "wb") as f:
    f.write(os.urandom(300_000))
with open(os.path.join(proj, "parcels.gpkg"), "rb") as f:
    cuts = store.cut_points(f)
sizes = [c[1] for c in cuts]
check(sum(sizes) == len(big) and all(store.MIN_CHUNK <= s <= store.MAX_CHUNK for s in sizes[:-1]),
      "chunks cover the file, each 64 KiB - 1 MiB (%d chunks, average %d KiB)"
      % (len(sizes), sum(sizes) // len(sizes) // 1024))
edited = big[:100] + b"INSERTED" * 13 + big[100:5_000_000] + b"Z" * 1000 + big[5_001_000:]
c2 = store.cut_points(io.BytesIO(edited))
ids1 = {store.chunk_id(big[o:o + ln]) for o, ln in cuts}
ids2 = {store.chunk_id(edited[o:o + ln]) for o, ln in c2}
check(len(ids1 & ids2) >= len(ids1) - 4, "an insert near the start and an edit in the middle change only the chunks "
      "around them (%d of %d chunks kept)" % (len(ids1 & ids2), len(ids1)))

owner_root = os.path.join(TMP, "pc-owner")
v = core.Vault(owner_root, SEED)
r1 = v.snapshot([proj], "first", 3, 5)
check(r1["files"] == 3 and r1["new"] == r1["total"], "the first snapshot sends all its data (%d files)" % r1["files"])
with open(os.path.join(proj, "parcels.gpkg"), "wb") as f:
    f.write(edited)
r2 = v.snapshot([proj], "second", 3, 5)
check(r2["new"] < r2["total"] * 0.25 and r2["dedup"] > 4,
      "the second snapshot sends only what changed: %.0f KB of %.1f MB (deduplication %.1fx)"
      % (r2["new"] / 1024, r2["total"] / 1048576, r2["dedup"]))
check(v.chunks.stats()["bytes"] < r1["total"] * 1.3, "this computer keeps each chunk once across both snapshots "
      "(%.1f MB for %.1f MB of snapshots)" % (v.chunks.stats()["bytes"] / 1048576,
                                              (r1["total"] + r2["total"]) / 1048576))
check(v.restore_local(r1["snap"], os.path.join(TMP, "local1"))["files"] == 3
      and open(os.path.join(TMP, "local1", "survey", "parcels.gpkg"), "rb").read() == big,
      "a snapshot is restored from this computer's own store")

# ---- shard files: signature, tampering, privacy -------------------------------------------------------------------
out = v.outbox()
p1, p2 = r1["pack"], r2["pack"]
raw = {}
for i in range(5):
    with open(out[p1][i], "rb") as f:
        raw[i] = f.read()
hdr, idx, share, data = core.parse_shard(raw[0])
check(hdr["owner"] == v.pub and idx == 0 and hdr["k"] == 3, "a shard carries a header signed by its owner")
bad = bytearray(raw[1])
bad[-10] ^= 1
check(raises(lambda: core.parse_shard(bytes(bad)), core.VaultError), "a shard with one bit changed is refused")
ln = struct.unpack(">I", raw[2][4:8])[0]
meta = json.loads(raw[2][8:8 + ln])
meta["hdr"]["k"] = 1
forged = core.pack_shard(meta["hdr"], meta["sig"], 2, bytes.fromhex(meta["share"]), raw[2][8 + ln:])
check(raises(lambda: core.parse_shard(forged), core.VaultError), "a shard whose header was changed (k=1) is refused")
check(raises(lambda: core.parse_shard(raw[0], owner=core._crypto().public_key(OTHER).hex()), core.VaultError),
      "a shard of another owner is refused where an owner is expected")
f1 = {}
core.add_shard(f1, raw[0])
core.add_shard(f1, raw[3])
check(raises(lambda: core.open_pack(f1[p1]), core.VaultError), "two shards (fewer than k) open nothing")
core.add_shard(f1, raw[4])
h, man, payload = core.open_pack(f1[p1])
check(man["snap"]["label"] == "first", "any three shards open the snapshot")
check(b"<qgis>" not in b"".join(raw.values()), "no shard holds the files in clear")

vp = core.Vault(os.path.join(TMP, "pc-pw"), SEED)
rp = vp.snapshot([os.path.join(proj, "map.qgz")], "pw", 2, 3, password="سر المكتب 2026")
fp = {}
for i, path in vp.outbox()[rp["pack"]].items():
    with open(path, "rb") as f:
        core.add_shard(fp, f.read())
check(raises(lambda: core.open_pack(fp[rp["pack"]]), core.VaultError)
      and raises(lambda: core.open_pack(fp[rp["pack"]], "wrong"), core.VaultError),
      "with a vault password, all the shards together open nothing without it")
check(core.open_pack(fp[rp["pack"]], "سر المكتب 2026")[1]["snap"]["label"] == "pw", "... and open with it")

kit = core.kit_text(SEED)
typo = kit[:-1] + ("0" if kit[-1] != "0" else "1")
check(core.read_kit(kit) == SEED and raises(lambda: core.read_kit(typo), core.VaultError),
      "the recovery kit gives the key back and catches typos")

# ---- a room: the owner and five keepers ---------------------------------------------------------------------------
net.LIVE = 2.0


class FakeLink:
    def __init__(self, board, mid):
        self.board, self.member = board, {"id": mid, "name": mid}
        self.down = False

    def call(self, path, obj):
        if self.down:
            raise net.LinkError("gone")
        status, res = self.board.route(path, obj, self.member)
        if status != 200:
            raise net.LinkError(res.get("error"))
        return res


board = net.Board()
keepers = {}


def add_pc(mid, root, seed, hold=True):
    link = FakeLink(board, mid)
    vv = core.Vault(root, seed)
    kp = net.Keeper(vv, lambda: link, name=mid, hold=hold, have_every=0.3, tick=0.03)
    kp.link = link
    kp.start()
    keepers[mid] = kp
    return kp


v.close()
own = add_pc("owner", owner_root, SEED, hold=False)
for i in range(1, 6):
    add_pc(f"m{i}", os.path.join(TMP, f"pc-m{i}"), hashlib.sha256(f"m{i}".encode()).digest())
time.sleep(0.8)
pl = own.place()
check(pl["queued"] == 10, "both snapshots' 10 shards go out to the five members (%s)" % pl)
check(own.wait_idle(60), "every member acknowledged its shards")
time.sleep(0.8)
hl = {x["label"]: x for x in own.health()}
check(hl["second"]["alive"] == 5 and hl["second"]["can_lose"] == 2 and hl["second"]["state"] == "safe",
      "health: 5 of 5 shards alive, the team can lose any 2 computers (%s)"
      % {k_: (x["alive"], x["can_lose"], x["state"]) for k_, x in hl.items()})
holders = own.holders_of(p2)
check(len(holders) == 5 and all(len(x) == 1 for x in holders.values()), "each member keeps one shard of a snapshot")
m1v = keepers["m1"].vault
one = m1v.held()
pk = sorted(one)[0]
f_m1 = {}
for i in one[pk]["idx"]:
    core.add_shard(f_m1, m1v.held_shard(pk, i))
check(raises(lambda: core.open_pack(f_m1[pk]), core.VaultError), "a member cannot open what it keeps")
thief = core.Vault(os.path.join(TMP, "pc-thief"), OTHER)
req = thief.signed("ask", {"packs": [p1, p2], "reply_to": "m5"})
req["b"]["owner"] = own.vault.pub   # claims to be the owner
before = keepers["m5"].received
board.route("vault_send", {"items": [{"to": f"m{i}", "kind": "ask", "meta": {"req": req}, "data": ""}
                                     for i in range(1, 5)]}, {"id": "m5", "name": "m5"})
time.sleep(1.0)
check(keepers["m5"].received == before, "an ask not signed by the owner gets no shard back")

# ---- disaster: two members AND the owner's own computer are gone --------------------------------------------------
for mid in ("m2", "m3", "owner"):
    keepers[mid].link.down = True
    keepers[mid].stop()
    keepers[mid].vault.close()
shutil.rmtree(owner_root)
shutil.rmtree(os.path.join(TMP, "pc-m2"))
shutil.rmtree(os.path.join(TMP, "pc-m3"))
time.sleep(net.LIVE + 0.8)
newpc = add_pc("newpc", os.path.join(TMP, "pc-new"), core.read_kit(kit), hold=False)
net._KEEPER[0] = newpc
time.sleep(0.6)
dest = os.path.join(TMP, "restored")
t0 = time.time()
res = team_vault.restore(dest, timeout=30)
with open(os.path.join(dest, "survey", "parcels.gpkg"), "rb") as f:
    same = f.read() == edited
check(res["files"] == 3 and same and res["label"] == "second",
      "a new computer with the recovery kit gets the newest snapshot back from the 3 members left (%.1f s)"
      % (time.time() - t0))
snaps = newpc.vault.snapshots()
first = next(s for s in snaps if s["label"] == "first")
dest1 = os.path.join(TMP, "restored-first")
team_vault.restore(dest1, first["id"], timeout=30)
with open(os.path.join(dest1, "survey", "parcels.gpkg"), "rb") as f:
    check(f.read() == big, "... and the older snapshot too")
hl = {x["label"]: x for x in newpc.health()}
check(hl["second"]["alive"] == 3 and hl["second"]["can_lose"] == 0 and hl["second"]["state"] == "at risk",
      "health shows the danger: 3 of 5 alive, cannot lose one more (%s)"
      % ((hl["second"]["alive"], hl["second"]["can_lose"], hl["second"]["state"]),))
add_pc("m6", os.path.join(TMP, "pc-m6"), b"6" * 32)
add_pc("m7", os.path.join(TMP, "pc-m7"), b"7" * 32)
time.sleep(0.8)
rep = team_vault.repair(timeout=30)
check(rep["repaired"] == 4 and not rep["short"], "repair rebuilds the 4 lost shards (2 per snapshot): %s" % rep)
check(newpc.wait_idle(60), "the rebuilt shards are acknowledged")
time.sleep(0.8)
hl = {x["label"]: x for x in newpc.health()}
check(all(x["alive"] == 5 and x["can_lose"] == 2 and x["state"] == "safe" for x in hl.values()),
      "after the repair every snapshot is 5 of 5 again and can lose any 2 computers (%s)"
      % {k_: (x["alive"], x["can_lose"]) for k_, x in hl.items()})
check(len(keepers["m6"].vault.held()) == 2 and len(keepers["m7"].vault.held()) == 2,
      "the new members keep the rebuilt shards")

# ---- without a room: folders (USB sticks) -------------------------------------------------------------------------
found = newpc.gather([p1, p2], need={p1: 5, p2: 5}, timeout=30)
files = {p: {i: core.pack_shard(e[0], e[3], i, e[1], e[2]) for i, e in found[p].items()} for p in (p1, p2)}
usb = [os.path.join(TMP, f"usb{i}") for i in range(5)]
check(newpc.vault.export_folders(None, usb, files) == 10, "shards are written to five folders")
shutil.rmtree(usb[0])
shutil.rmtree(usb[3])
dest2 = os.path.join(TMP, "from-usb")
r = team_vault.restore(dest2, folders=usb[1:3] + usb[4:])
with open(os.path.join(dest2, "survey", "parcels.gpkg"), "rb") as f:
    check(r["files"] == 3 and f.read() == edited, "any three of the five folders give the snapshot back")
check(newpc.withdraw(first["id"]) == [], "removing an older snapshot keeps its pack: a later one reuses its chunks")
alone = os.path.join(work, "notes.txt")
with open(alone, "w", encoding="utf-8") as f:
    f.write("ملاحظات الموقع " * 2000)
r3 = team_vault.snapshot([alone], "third")
check(newpc.wait_idle(60) and r3["placement"]["queued"] == 5, "a third snapshot goes out to the team")
time.sleep(0.8)
check(newpc.withdraw(r3["snap"]) == [r3["pack"]], "removing it withdraws its own pack")
time.sleep(1.2)
live_k = [k_ for m_, k_ in keepers.items() if not k_.link.down and m_ != "newpc"]
check(all(r3["pack"] in k_.vault.held() and k_.vault.held()[r3["pack"]]["purge"] for k_ in live_k)
      and all(p1 in k_.vault.held() or p2 in k_.vault.held() for k_ in live_k),
      "a signed drop deletes nothing at once: every member marks the withdrawn shards for deletion in 30 days")
d30 = [k_.vault.held()[r3["pack"]]["purge"] - time.time() for k_ in live_k]
check(all(29.9 * 86400 < x <= 30 * 86400 for x in d30), "... the deletion date is 30 days ahead")
for k_ in live_k:
    k_.report()
newpc.refresh()
check(r3["pack"] in newpc.dropping(), "the owner sees which of its packs are going to be deleted, and when")
check(all(k_.vault.purge_dropped() == [] for k_ in live_k), "nothing is deleted before the grace runs out")
newpc.keep([r3["pack"]])
time.sleep(1.2)
check(all(r3["pack"] in k_.vault.held() and not k_.vault.held()[r3["pack"]]["purge"] for k_ in live_k),
      "a signed keep cancels the deletion (ransomware's drop can be undone within 30 days)")
newpc.drop([r3["pack"]])
time.sleep(1.2)
check(all(k_.vault.purge_dropped(now=time.time() + 31 * 86400) == [r3["pack"]] for k_ in live_k)
      and all(r3["pack"] not in k_.vault.held() for k_ in live_k)
      and all(p1 in k_.vault.held() or p2 in k_.vault.held() for k_ in live_k),
      "after the grace every member deletes the withdrawn shards and keeps the others")

# ---- speed --------------------------------------------------------------------------------------------------------
big50 = os.path.join(work, "big50.gpkg")
with open(big50, "wb") as f:
    f.write(blob(50 << 20, 9))
vs = core.Vault(os.path.join(TMP, "pc-speed"), SEED)
t0 = time.time()
rs50 = vs.snapshot([big50], "50 MB", 3, 5)
ts = time.time() - t0
fs = {}
for i, path in sorted(vs.outbox()[rs50["pack"]].items())[2:]:
    with open(path, "rb") as f:
        core.add_shard(fs, f.read())
t0 = time.time()
core.restore_from(fs, rs50["pack"], os.path.join(TMP, "speed-out"))
tr_ = time.time() - t0
check(ts < 60 and tr_ < 60, "50 MB: snapshot (chunk + encrypt + 3-of-5) %.1f s (%.1f MB/s); restore from shards 2,3,4 "
      "(two parity) %.1f s" % (ts, 50 / ts, tr_))
print("   50 MB file -> %.1f MB encrypted, 5 shards of %.1f MB (%.2fx the file across the team)"
      % (rs50["ct"] / 1048576, rs50["shard"] / 1048576, 5 * rs50["shard"] / (50 << 20)))
vs.close()

# ---- the window (offscreen, never shown) --------------------------------------------------------------------------
from biruni.team_vault import ui  # noqa: E402

d = ui.open_dialog(None, show=False)
states = [d.table.item(r_, 7).text() for r_ in range(d.table.rowCount())]
check(d.table.rowCount() == 1 and all("Safe" in s for s in states), "the window lists the snapshots and their state "
      "(%s)" % states)
check(d.kit_fp.text().endswith(core.fingerprint(newpc.vault.pub)), "the window shows the vault key's fingerprint")
d.hold.setChecked(False)
check(net.load_settings()[0] is False and newpc.hold is False, "turning off keeping shards is remembered and applied")
d.close()
sd = ui.SnapshotDialog()
check(sd.k.value() == 3 and sd.n.value() == 5, "a new snapshot defaults to any 3 of 5")
sd.close()

# ---- a real room: a host and two members over the room's sealed HTTP messages -------------------------------------
for kp in keepers.values():
    kp.stop(wait=2)
net._KEEPER[0] = None
net.LIVE = 90
net.save_settings(True, 1024)    # the window test above turned keeping off
import threading  # noqa: E402

from qgis.core import (QgsCoordinateTransformContext, QgsFeature, QgsGeometry, QgsProject,  # noqa: E402
                       QgsVectorFileWriter, QgsVectorLayer)

from biruni.team import code as codes, session  # noqa: E402
from biruni.team.node import Node  # noqa: E402
from biruni.team.store import Store  # noqa: E402


def pump(seconds, until=None):
    end = time.time() + seconds
    while time.time() < end:
        app.processEvents()
        if until is not None and until():
            return True
        time.sleep(0.02)
    return until() if until is not None else True


office = os.path.join(TMP, "office")
os.makedirs(office)
gpkg = os.path.join(office, "parcels.gpkg")
mem = QgsVectorLayer("Polygon?crs=EPSG:32638&field=owner:string", "Parcels", "memory")
ft = QgsFeature(mem.fields())
ft.setAttributes(["a"])
ft.setGeometry(QgsGeometry.fromWkt("POLYGON((0 0,10 0,10 10,0 10,0 0))"))
mem.dataProvider().addFeatures([ft])
o = QgsVectorFileWriter.SaveVectorOptions()
o.driverName = "GPKG"
o.layerName = "parcels"
QgsVectorFileWriter.writeAsVectorFormatV3(mem, gpkg, QgsCoordinateTransformContext(), o)
hl_ = QgsVectorLayer(gpkg + "|layername=parcels", "Parcels", "ogr")
QgsProject.instance().addMapLayer(hl_)
QgsProject.instance().setCrs(hl_.crs())
QgsProject.instance().write(os.path.join(office, "office.qgz"))
host = session.open_room([hl_], "Vault room", approve=False)
code = session.room_code(host)
pump(2, lambda: net.current() is not None)
check(net.current() is not None and net.current().running, "opening a room starts this computer's vault keeper")
net.current().have_every = 0.5
real_identity = session.identity


def join(ident, where):
    session.identity = lambda: dict(ident)
    result, err = {}, []

    def worker():
        try:
            result.update(session.join_steps(code, os.path.join(TMP, where), lambda s, a, b: None))
        except Exception as e:  # noqa: BLE001
            err.append(repr(e))

    t = threading.Thread(target=worker)
    t.start()
    pump(60, lambda: not t.is_alive())
    session.identity = real_identity
    snap = result["snap"]
    lyr = QgsVectorLayer(os.path.join(snap["folder"], snap["layer_files"]["L1"]) + "|layername=parcels", "Parcels",
                         "ogr")
    st = Store(os.path.join(result["folder"], ".biruni", "team", "room.sqlite"))
    st.put("layers", result["hello"]["room"]["layers"])
    st.put("since", snap["head"])
    st.put("block", result["hello"]["block"])
    st.put("sid", result["hello"]["room"].get("sid"))
    st.put("roster", result["hello"]["room"].get("members") or [])
    st.set_versions([tuple(x) for x in snap["versions"]])
    nd = Node(st, codes.RoomKeys(code), dict(ident))
    nd.roster = st.get("roster")
    nd.attach_layers({"L1": lyr})
    nd.start_member(result["address"], result["hello"]["member"])
    return nd


mona = join({"id": "a" * 32, "seed": "1" * 64, "name": "Mona"}, "mona")
sami = join({"id": "b" * 32, "seed": "2" * 64, "name": "Sami"}, "sami")
pump(5, lambda: len(host.hub.roster()) == 3)
km = net.Keeper(core.Vault(os.path.join(TMP, "room-mona"), b"m" * 32), lambda: net.link_of(mona), "Mona",
                have_every=0.5, tick=0.05)
ks = net.Keeper(core.Vault(os.path.join(TMP, "room-sami"), b"s" * 32), lambda: net.link_of(sami), "Sami",
                have_every=0.5, tick=0.05)
km.start()
ks.start()
pump(3, lambda: len(km.refresh().get("members", {})) == 3)
check(len(km.refresh()["members"]) == 3, "the host and both members report to the board through the room")
room_file = os.path.join(TMP, "room-data.bin")
with open(room_file, "wb") as f:
    f.write(blob(6 << 20, 3))
rr = km.vault.snapshot([room_file], "room", 2, 2)
pl = km.place()
ok = pump(60, lambda: not km.out and not km.pending)
check(pl["queued"] == 2 and ok, "Mona's 2 shards (%.1f MB each, in pieces) reach the host and Sami through the room"
      % (rr["shard"] / 1048576))
pump(2)
h_ = km.holders_of(rr["pack"])
check(len(h_) == 2 and mona.member["id"] not in h_, "health over the room: the host and Sami each keep one (%s)"
      % sorted(h_))
found = {}
th = threading.Thread(target=lambda: found.update(km.gather([rr["pack"]], timeout=60, local=False)))
th.start()
pump(70, lambda: not th.is_alive())
outdir = os.path.join(TMP, "room-restored")
core.restore_from(found, rr["pack"], outdir)
with open(os.path.join(outdir, "room-data.bin"), "rb") as f, open(room_file, "rb") as g:
    check(f.read() == g.read(), "Mona gets her file back from the host and Sami through the room (signed asks)")
km.stop()
ks.stop()
for nd in (mona, sami):
    nd.shutdown()
host.shutdown()
from biruni import team  # noqa: E402

team.set_current(None)
check(net.current() is None, "leaving the room stops the keeper")
km.vault.close()
ks.vault.close()

with open(os.path.join(HERE, "..", "app", "plugins", "biruni", "team", "hub.py"), encoding="utf-8") as f:
    check('path.startswith("vault_")' in f.read(), "the room's host routes vault_* requests to the board")

for kp in keepers.values():
    kp.stop(wait=2)
    try:
        kp.vault.close()
    except Exception:  # noqa: BLE001
        pass
net._KEEPER[0] = None
thief.close()
vp.close()
shutil.rmtree(TMP, ignore_errors=True)
print("ALL PASSED" if not failures else f"{len(failures)} FAILED")
app.exitQgis()
sys.exit(1 if failures else 0)
