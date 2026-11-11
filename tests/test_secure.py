r"""The locked safe (secure/): locked databases with users and rights, locked projects.

Run:  engine\bin\python-qgis.bat tests\test_secure.py      (headless: QT_QPA_PLATFORM=offscreen)

Everything is checked at the level of the cryptography as well as through the sessions: a user who may not
see a layer has no key that opens it, a hidden field's values are in a piece he cannot open, a changed
manifest is refused, forged or unauthorised change sets are refused at merge, the activity log is signed,
the recovery key restores the administrator, and decrypted data never reaches the disk.
"""
import datetime as dt
import json
import os
import shutil
import sys
import tempfile
import time
import faulthandler

faulthandler.enable()

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "app", "plugins"))
sys.path.append(os.path.join(HERE, "..", "app", "python"))

SETTINGS_DIR = tempfile.mkdtemp(prefix="biruni_secure_settings_")
os.environ["QGIS_CUSTOM_CONFIG_PATH"] = SETTINGS_DIR

# ---- what is on the disks before anything is decrypted ---------------------------------------------------------
MARK = b"SECRET-MARK-7Q4Z"  # in every secret value; must never appear in a file written by the test run
PHONE = "SECRET-MARK-7Q4Z-0100"
SCAN_DIRS = [tempfile.gettempdir(), SETTINGS_DIR]


def snapshot(dirs):
    out = {}
    for d in dirs:
        for root, _, files in os.walk(d):
            for n in files:
                p = os.path.join(root, n)
                try:
                    st = os.stat(p)
                    out[p] = (st.st_size, st.st_mtime_ns)
                except OSError:
                    pass
    return out


BEFORE = snapshot(SCAN_DIRS)
T0 = time.time()

from qgis.core import QgsApplication  # noqa: E402

app = QgsApplication([], True)
app.initQgis()

from osgeo import gdal, ogr, osr  # noqa: E402

gdal.UseExceptions()

failures = []


def check(cond, msg):
    print(("PASS " if cond else "FAIL ") + msg, flush=True)
    if not cond:
        failures.append(msg)


def raises(fn, code=None):
    from biruni.secure.container import LockError
    from biruni.secure.project_lock import ProjectLockError

    try:
        fn()
    except (LockError, ProjectLockError) as e:
        return code is None or e.code == code
    return False


# ---- start-up cost: importing the package loads nothing heavy ---------------------------------------------------
before_mods = set(sys.modules)
t = time.perf_counter()
import biruni.secure as secure_pkg  # noqa: E402

import_ms = (time.perf_counter() - t) * 1000
loaded = set(sys.modules) - before_mods
check(not any(m.startswith(("cryptography", "biruni.secure.crypto", "biruni.secure.container",
                            "biruni.secure.dialogs")) for m in loaded),
      f"importing biruni.secure loads no crypto, container or dialogs ({import_ms:.1f} ms)")

from biruni.secure import container, crypto, gpkg, passwords, project_lock  # noqa: E402

WORK = tempfile.mkdtemp(prefix="biruni_secure_work_")   # plaintext source data lives here (not scanned)
OUT = os.path.join(WORK, "locked")                     # locked files only (scanned)
os.makedirs(OUT)
SRC = os.path.join(WORK, "town.gpkg")


def make_gpkg(path, n_parcels=30):
    srs = osr.SpatialReference()
    srs.ImportFromEPSG(32638)
    ds = ogr.GetDriverByName("GPKG").CreateDataSource(path)
    par = ds.CreateLayer("Parcels", srs, ogr.wkbPolygon)
    for name, t_ in (("parcel_no", ogr.OFTString), ("owner", ogr.OFTString), ("owner_phone", ogr.OFTString),
                     ("area", ogr.OFTReal)):
        par.CreateField(ogr.FieldDefn(name, t_))
    par.StartTransaction()
    for i in range(n_parcels):
        f = ogr.Feature(par.GetLayerDefn())
        x, y = 500000 + i * 30, 2700000
        f.SetGeometry(ogr.CreateGeometryFromWkt(f"POLYGON(({x} {y},{x+25} {y},{x+25} {y+25},{x} {y+25},{x} {y}))"))
        f.SetField("parcel_no", f"P-{i:03d}")
        f.SetField("owner", f"Owner {i}")
        f.SetField("owner_phone", f"{PHONE}-{i}")
        f.SetField("area", 625.0)
        par.CreateFeature(f)
    par.CommitTransaction()
    roads = ds.CreateLayer("Roads", srs, ogr.wkbLineString)
    roads.CreateField(ogr.FieldDefn("name", ogr.OFTString))
    roads.CreateField(ogr.FieldDefn("width", ogr.OFTReal))
    for i in range(5):
        f = ogr.Feature(roads.GetLayerDefn())
        f.SetGeometry(ogr.CreateGeometryFromWkt(f"LINESTRING(500000 {2700100 + i * 20},501000 {2700100 + i * 20})"))
        f.SetField("name", f"SECRET-MARK-7Q4Z road {i}")
        f.SetField("width", 12.0)
        roads.CreateFeature(f)
    pts = ds.CreateLayer("Points", srs, ogr.wkbPoint)
    pts.CreateField(ogr.FieldDefn("kind", ogr.OFTString))
    for i in range(4):
        f = ogr.Feature(pts.GetLayerDefn())
        f.SetGeometry(ogr.CreateGeometryFromWkt(f"POINT({500000 + i} {2700500})"))
        f.SetField("kind", "well")
        pts.CreateFeature(f)
    ds = None


make_gpkg(SRC)

# ---- passwords ---------------------------------------------------------------------------------------------------
check(len(passwords.common_words()) >= 1000,f"common password list has {len(passwords.common_words())} entries")
check(passwords.is_common("Password123!") and passwords.is_common("qwerty2024") and passwords.is_common("p@ssw0rd"),
      "common passwords and their variants are caught")
check(passwords.problem("short") != "" and passwords.problem("Tr4vel-Desert-Cloud") == "",
      "a short password is refused, a good one accepted")
check(passwords.score("aaaaaaaaaaaa") == 0 and passwords.score("Tr4vel-Desert-Cloud-77") >= 3, "strength score")

# ---- crypto building blocks ---------------------------------------------------------------------------------------
k = crypto.random_key()
blob = crypto.seal(k, b"hello", b"aad")
check(crypto.unseal(k, blob, b"aad") == b"hello", "AES-GCM round trip")
try:
    crypto.unseal(k, blob, b"other")
    check(False, "AES-GCM refuses other associated data")
except crypto.CryptoError:
    check(True, "AES-GCM refuses other associated data (a piece cannot be swapped)")
kdf = crypto.new_kdf()
t = time.perf_counter()
crypto.derive("x" * 12, kdf)
kdf_ms = (time.perf_counter() - t) * 1000
check(kdf.get("alg") == "argon2id" and kdf["m"] >= 262144,
      f"passwords go through Argon2id 256 MiB / t={kdf.get('t')} ({kdf_ms:.0f} ms per guess)")
code = crypto.new_recovery_code()
check(len(code) == 39 and code.count("-") == 7, f"recovery key looks like {code[:9]}…")

# ---- lock a database -----------------------------------------------------------------------------------------------
ADMIN_PW = "Desert-Falcon-2026!x"
MASTER = os.path.join(OUT, "town.bgpkg")
t = time.perf_counter()
container.create(SRC, ["Parcels", "Roads", "Points"], MASTER, "Hatem", ADMIN_PW, code)
check(os.path.exists(MASTER), f"locked database written ({(time.perf_counter() - t) * 1000:.0f} ms)")
with open(MASTER, "rb") as f:
    raw = f.read()
check(MARK not in raw, "the locked file does not contain any secret value in clear")
check(raises(lambda: container.LockedFile(MASTER).unlock("Hatem", "wrong-password-123")), "wrong password refused")
check(raises(lambda: container.LockedFile(MASTER).unlock("Nobody", ADMIN_PW), "wrong_password"),
      "unknown user refused with the same message")

adm = container.LockedFile(MASTER).unlock("hatem ", ADMIN_PW)  # names: case and spaces do not matter
check(adm.is_admin, "administrator unlocked")
L = {x["name"]: x["id"] for x in adm.layers()}
check(set(L) == {"Parcels", "Roads", "Points"}, "administrator sees the 3 layers")
ali = adm.add_user("Ali", "Ali-Reads-Maps-42", rights={L["Parcels"]: "v"})
sara = adm.add_user("Sara", "Sara-Edits-Roads-77", rights={L["Roads"]: "ve", L["Points"]: "a", L["Parcels"]: "v"},
                    can={"export": True})
adm.set_hidden(sara, L["Parcels"], ["owner_phone"])
old = adm.add_user("Omar", "Omar-Expired-2025!", rights={L["Points"]: "v"}, expires="2020-01-01")
check(raises(lambda: adm.add_user("ali", "Another-Pass-999")), "a second user with the same name is refused")
check(container.norm_rights("a") == "va" and container.norm_rights("de") == "ved", "adding/editing/deleting implies view")
t = time.perf_counter()
adm.save()
save_ms = (time.perf_counter() - t) * 1000
check(adm.m["version"] == 2, f"administrator saved version 2 ({save_ms:.0f} ms)")
adm.close()

# ---- Ali: Parcels only; no key that opens Roads ------------------------------------------------------------------
lf = container.LockedFile(MASTER)
check(lf.signature_ok(), "manifest signature verifies with the administrator's key")
s_ali = lf.unlock("Ali", "Ali-Reads-Maps-42")
check([x["name"] for x in s_ali.layers()] == ["Parcels"], "Ali sees Parcels only")
check(s_ali.can_decrypt(L["Parcels"]) and not s_ali.can_decrypt(L["Roads"]), "Ali's keys open Parcels, not Roads")
roads_entry = next(x for x in lf.manifest["layers"] if x["id"] == L["Roads"])
roads_ct = lf.blob(roads_entry["blob"], roads_entry["hash"])
opened = 0
for key in list(s_ali.keys.values()) + [k2 for d in getattr(s_ali, "fkeys", {}).values() for k2 in d.values()]:
    try:
        crypto.unseal(key, roads_ct, container._aad(lf.family, "blob", roads_entry["blob"]))
        opened += 1
    except crypto.CryptoError:
        pass
check(opened == 0 and L["Roads"] not in s_ali.keys,
      f"crypto level: none of Ali's {len(s_ali.keys)} key(s) decrypts the Roads ciphertext")
check(raises(lambda: lf.unlock("Omar", "Omar-Expired-2025!"), "expired"), "a user past his end date is refused")

# ---- Sara: the hidden field is not in anything she can open ---------------------------------------------------------
s_sara = container.LockedFile(MASTER).unlock("Sara", "Sara-Edits-Roads-77")
mounted = {ml.name: ml for ml in s_sara.mount()}
check(set(mounted) == {"Parcels", "Roads", "Points"}, "Sara sees Parcels, Roads, Points")
check("owner_phone" not in gpkg.field_names(mounted["Parcels"].path, "Parcels"), "owner_phone is not in Sara's Parcels")
check(MARK not in gpkg.mem_read(mounted["Parcels"].path), "no hidden value anywhere in Sara's decrypted Parcels")
par_entry = next(x for x in s_sara.lf.manifest["layers"] if x["id"] == L["Parcels"])
check(len(par_entry["fields"]) == 1 and not s_sara.fkeys.get(L["Parcels"]),
      "owner_phone lives in its own encrypted piece, and Sara has no key for it")
adm = container.LockedFile(MASTER).unlock("Hatem", ADMIN_PW)
adm_par = next(ml for ml in adm.mount() if ml.name == "Parcels")
check("owner_phone" in gpkg.field_names(adm_par.path, "Parcels"), "the administrator sees owner_phone")
adm.close()

# ---- a changed manifest or piece is refused ---------------------------------------------------------------------------
TAMPER = os.path.join(OUT, "tampered.bgpkg")
recs = container.read_records(MASTER)
m = json.loads(bytes(recs[0][1]).decode("utf-8"))
for u in m["users"]:
    if u["uid"] == ali:
        u["rights"][L["Roads"]] = "vaed"  # Ali gives himself Roads
container.write_records(TAMPER, [(b"M", crypto.canonical(m))] + [(k_, bytes(p)) for k_, p in recs[1:]])
check(raises(lambda: container.LockedFile(TAMPER).unlock("Ali", "Ali-Reads-Maps-42"), "tampered"),
      "a manifest changed by hand is refused (signature)")
recs2 = [(k_, bytearray(p)) for k_, p in recs]
for k_, p in recs2:
    if k_ == b"B":
        p[-5] ^= 0x01
        break
container.write_records(TAMPER, [(k_, bytes(p)) for k_, p in recs2])
check(raises(lambda: container.LockedFile(TAMPER).unlock("Hatem", ADMIN_PW).mount(), "tampered"),
      "a changed encrypted piece is refused (its hash is in the signed manifest)")

s_sara.close()
s_ali.close()

# ---- copies for users, edits, forged and unauthorised change sets, merge ---------------------------------------
adm = container.LockedFile(MASTER).unlock("Hatem", ADMIN_PW)
SARA_COPY, ALI_COPY = os.path.join(OUT, "for_sara.bgpkg"), os.path.join(OUT, "for_ali.bgpkg")
adm.send_to([sara], SARA_COPY, only_visible=True)
adm.send_to([ali], ALI_COPY, only_visible=True)
ali_lf = container.LockedFile(ALI_COPY)
check([x for x in ali_lf.blobs if x.startswith("L")] == ["L" + L["Parcels"]],
      "Ali's copy carries only the Parcels piece (not even the Roads ciphertext)")
check(len(ali_lf.manifest["users"]) == 2, "Ali's copy holds only Ali and the administrator")
adm.close()

# Sara: edits a road (allowed), adds a point (allowed), deletes a road (not allowed: no delete right)
s = container.LockedFile(SARA_COPY).unlock("Sara", "Sara-Edits-Roads-77")
s.log("open")
mm = {ml.name: ml for ml in s.mount()}
ds = ogr.Open(mm["Roads"].path, 1)
lyr = ds.GetLayerByName("Roads")
f = lyr.GetFeature(1)
f.SetField("width", 30.0)
lyr.SetFeature(f)
lyr.DeleteFeature(5)
ds = None
ds = ogr.Open(mm["Points"].path, 1)
lyr = ds.GetLayerByName("Points")
f = ogr.Feature(lyr.GetLayerDefn())
f.SetGeometry(ogr.CreateGeometryFromWkt("POINT(500100 2700600)"))
f.SetField("kind", "new well")
lyr.CreateFeature(f)
ds = None
saved = {o["layer"]: o for o in s.save()}
check(saved["Roads"]["edits"] == 1 and saved["Roads"]["deletes"] == 1 and saved["Points"]["adds"] == 1,
      "Sara's save records a signed change set per changed layer")
check(saved["Roads"]["not_allowed"] == ["deletes"], "Sara is told at once that deleting roads is not allowed")
s.close()
s = container.LockedFile(SARA_COPY).unlock("Sara", "Sara-Edits-Roads-77")
mm = {ml.name: ml for ml in s.mount()}
check(gpkg.feature_count(mm["Points"].path, "Points") == 5, "Sara sees her own (not yet merged) changes on reopening")
s.close()

# Ali: view only, but he edits the bytes he can decrypt and his copy records it - deleting 3 parcels
a = container.LockedFile(ALI_COPY).unlock("Ali", "Ali-Reads-Maps-42")
ma = {ml.name: ml for ml in a.mount()}
ds = ogr.Open(ma["Parcels"].path, 1)
lyr = ds.GetLayerByName("Parcels")
for fid in (1, 2, 3):
    lyr.DeleteFeature(fid)
ds = None
a.save()
# … and he forges a change set in Sara's name (signed with his own key) that deletes 2 more parcels
fake = {"type": "changes", "lid": L["Parcels"], "id": "forged-1", "base": 2,
        "ct": crypto.seal_json(a.keys[L["Parcels"]], {"adds": [], "edits": [], "deletes": [10, 11], "schema": {}},
                               container._aad(a.family, "changes", L["Parcels"], sara)),
        "uid": sara, "t": int(time.time()), "prev": container.tail_hash(ALI_COPY)}
fake["sig"] = crypto.b64(crypto.sign(a.seed, container.ENV_PREFIX + crypto.canonical(fake)))
container.append_record(ALI_COPY, b"C", crypto.canonical(fake))
a.close()

adm = container.LockedFile(MASTER).unlock("Hatem", ADMIN_PW)
rep = adm.merge(SARA_COPY) + adm.merge(ALI_COPY)
for r in rep:
    print("   merge:", r)


def find(user, op, ok, reason=None):
    return [r for r in rep if r["user"] == user and r["op"] == op and r["ok"] == ok and (reason is None or r["reason"] == reason)]


check(find("Sara", "edit", True) and find("Sara", "add", True), "Sara's allowed edit and add are applied")
check(find("Sara", "delete", False, "no_delete"), "Sara's road deletion is refused (no delete right)")
r_ali = find("Ali", "delete", False, "no_delete")
check(r_ali and r_ali[0]["count"] == 3 and r_ali[0]["layer"] == "Parcels",
      "Ali tried to delete 3 features of Parcels - refused and reported")
check(find("Sara", "all", False, "signature"), "the change set forged in Sara's name is refused (signature)")
check(adm.merge(SARA_COPY) == [], "merging the same copy again applies nothing twice")
adm.save()
adm.close()
adm = container.LockedFile(MASTER).unlock("Hatem", ADMIN_PW)
mm = {ml.name: ml for ml in adm.mount()}
ds = ogr.Open(mm["Roads"].path)
check(ds.GetLayerByName("Roads").GetFeature(1).GetField("width") == 30.0 and ds.GetLayerByName("Roads").GetFeatureCount() == 5,
      "master: Sara's road width is in, the road she deleted is still there")
ds = None
check(gpkg.feature_count(mm["Points"].path, "Points") == 5 and gpkg.feature_count(mm["Parcels"].path, "Parcels") == 30,
      "master: Sara's point is in, all 30 parcels are still there")
check("owner_phone" in gpkg.field_names(mm["Parcels"].path, "Parcels"), "master keeps owner_phone after saves")

# ---- signatures and the activity log ----------------------------------------------------------------------------
log = adm.read_audit()
acts = [(e["user"], e["action"]) for e in log]
check(("Hatem", "create") in acts and ("Sara", "open") in acts and ("Sara", "save") in acts and ("Ali", "save") in acts
      and ("Hatem", "merge") in acts and ("Hatem", "send") in acts, f"activity log has {len(log)} entries: "
      f"create, send, open, save, merge")
check(all(e.get("signed") for e in log), "every activity entry is signed by its author")
mlog = [e for e in log if e["action"] == "merge" and e["details"].get("refused")]
check(any(r.get("user") == "Ali" for e in mlog for r in e["details"].get("refusals", [])),
      "the refused attempts are kept in the signed log")
lf = container.LockedFile(MASTER)
check(lf.signature_ok() and not lf.appended, "a saved master has a valid signature and no loose records")
sara_lf = container.LockedFile(SARA_COPY)
check(all(sara_lf.record_signature_ok(env) for k_, env, _ in sara_lf.records()), "Sara's records verify")
check(all(ch for _, _, ch in sara_lf.records()), "Sara's records form an unbroken chain")
# an activity entry cannot be read by Sara (encrypted to the administrator)
env = next(env for k_, env, _ in sara_lf.records() if k_ == b"A")
s = sara_lf.unlock("Sara", "Sara-Edits-Roads-77")
readable = 0
for key in s.keys.values():
    try:
        crypto.unseal(key, crypto.unb64(env["ct"]), container._aad(s.family, "audit"))
        readable += 1
    except crypto.CryptoError:
        pass
check(readable == 0 and "box_priv" not in vars(s), "activity entries are encrypted to the administrator only")
s.close()
adm.close()

# ---- the recovery key -----------------------------------------------------------------------------------------------
check(raises(lambda: container.LockedFile(MASTER).recover("AAAA-BBBB-CCCC-DDDD-EEEE-FFFF-GGGG-HHHH"), "bad_recovery"),
      "a wrong recovery key is refused")
rec = container.LockedFile(MASTER).recover(code.lower().replace("-", " "))
check(rec.is_admin and rec.recovered, "the recovery key restores the administrator (typed in lower case, spaces)")
admin_uid = next(u["uid"] for u in rec.users() if u["role"] == "admin")
rec.reset_password(admin_uid, "New-Admin-Pass-2027!")
rec.save()
rec.close()
check(raises(lambda: container.LockedFile(MASTER).unlock("Hatem", ADMIN_PW), "wrong_password")
      and container.LockedFile(MASTER).unlock("Hatem", "New-Admin-Pass-2027!").is_admin,
      "after recovery: the new administrator password works, the old one not")
ADMIN_PW = "New-Admin-Pass-2027!"

# ---- removing a right rotates the key ---------------------------------------------------------------------------------
adm = container.LockedFile(MASTER).unlock("Hatem", ADMIN_PW)
k_before = adm.keys[L["Roads"]]
adm.set_rights(sara, L["Roads"], "")
adm.save()
check(adm.keys[L["Roads"]] != k_before, "taking Roads away from Sara gives Roads a new key")
adm.close()
s = container.LockedFile(MASTER).unlock("Sara", "Sara-Edits-Roads-77")
check(not s.can_decrypt(L["Roads"]) and L["Roads"] not in s.keys, "Sara cannot open Roads in the new version")
s.close()

print(f"core part: {time.time() - T0:.1f} s", flush=True)

# ---- a locked project ---------------------------------------------------------------------------------------------
from qgis.core import QgsFeature, QgsGeometry, QgsPointXY, QgsProject, QgsVectorLayer  # noqa: E402

PRJ_DIR = os.path.join(WORK, "Project")
os.makedirs(os.path.join(PRJ_DIR, "data"))
shutil.copy(SRC, os.path.join(PRJ_DIR, "data", "town.gpkg"))
prj = QgsProject()
for name in ("Parcels", "Points"):
    lyr = QgsVectorLayer(os.path.join(PRJ_DIR, "data", "town.gpkg") + f"|layername={name}", name, "ogr")
    prj.addMapLayer(lyr)
PRJ = os.path.join(PRJ_DIR, "town.qgz")
check(prj.write(PRJ), "a project with 2 layers saved")
files = project_lock.project_data_files(PRJ)
check(len(files) == 1 and files[0][1], "the project's data file is found, inside its folder")
BPROJ = os.path.join(OUT, "town.bproj")
PRJ_PW = "Project-Lock-Pass-55"
arcs = project_lock.lock(PRJ, [p for p, _ in files], BPROJ, PRJ_PW)
check(arcs == ["data/town.gpkg"], f"locked project holds {arcs}")
with open(BPROJ, "rb") as f:
    check(MARK not in f.read(), "the locked project does not contain any secret value in clear")
check(raises(lambda: project_lock.LockedProject(BPROJ, "not-the-password"), "wrong_password"),
      "a locked project does not open without its password")
p2 = QgsProject()
lp = project_lock.open_in(p2, BPROJ, PRJ_PW)
layers = {lyr.name(): lyr for lyr in p2.mapLayers().values()}
check(set(layers) == {"Parcels", "Points"} and all(lyr.isValid() for lyr in layers.values()),
      "the locked project opens with its password, both layers valid")
check(all(gpkg.is_vsimem(lyr.source()) for lyr in layers.values()), "its layers are read from memory")
check(layers["Parcels"].featureCount() == 30, "Parcels has its 30 features")
pts = layers["Points"]
pts.startEditing()
feat = QgsFeature(pts.fields())
feat.setAttribute("kind", "added in locked project")
feat.setGeometry(QgsGeometry.fromPointXY(QgsPointXY(500200, 2700700)))
pts.addFeature(feat)
check(pts.commitChanges(), "an edit is committed (to memory)")
check(p2.write(), "Save writes the project back through the locked storage")
lp.close()
del layers, pts
p2.clear()
p3 = QgsProject()
lp = project_lock.open_in(p3, BPROJ, PRJ_PW)
pts = next(lyr for lyr in p3.mapLayers().values() if lyr.name() == "Points")
check(pts.featureCount() == 5, "reopened: the edit made in the locked project is there")
OPEN_DIR = os.path.join(WORK, "unlocked")
out_prj = lp.unlock_to(OPEN_DIR)
check(os.path.exists(out_prj) and os.path.exists(os.path.join(OPEN_DIR, "data", "town.gpkg")),
      "Remove the lock writes the project and its data to a folder")
lp.close()
p3.clear()
p4 = QgsProject()
check(p4.read(out_prj) and all(lyr.isValid() for lyr in p4.mapLayers().values()) and len(p4.mapLayers()) == 2,
      "the unlocked project opens normally")
p4.clear()

# ---- performance: a 50 MB GeoPackage -----------------------------------------------------------------------------------
BIG = os.path.join(WORK, "big.gpkg")
srs = osr.SpatialReference()
srs.ImportFromEPSG(32638)
ds = ogr.GetDriverByName("GPKG").CreateDataSource(BIG)
for li in range(2):
    lyr = ds.CreateLayer(f"Big{li}", srs, ogr.wkbPolygon)
    lyr.CreateField(ogr.FieldDefn("code", ogr.OFTString))
    lyr.CreateField(ogr.FieldDefn("note", ogr.OFTString))
    lyr.CreateField(ogr.FieldDefn("value", ogr.OFTReal))
    lyr.StartTransaction()
    defn = lyr.GetLayerDefn()
    for i in range(46000):
        f = ogr.Feature(defn)
        x, y = 400000 + (i % 1000) * 50, 2600000 + (i // 1000) * 50
        pts_ = ",".join(f"{x + 20 * __import__('math').cos(a / 3.0):.3f} {y + 20 * __import__('math').sin(a / 3.0):.3f}"
                        for a in range(19))
        f.SetGeometry(ogr.CreateGeometryFromWkt(f"POLYGON(({pts_},{pts_.split(',')[0]}))"))
        f.SetField("code", f"C{li}-{i:07d}")
        f.SetField("note", f"note {os.urandom(6).hex()}")
        f.SetField("value", i * 0.5)
        lyr.CreateFeature(f)
    lyr.CommitTransaction()
ds = None
size_mb = os.path.getsize(BIG) / 1e6
BIGL = os.path.join(OUT, "big.bgpkg")
t = time.perf_counter()
container.create(BIG, ["Big0", "Big1"], BIGL, "Hatem", ADMIN_PW, crypto.new_recovery_code())
t_lock = time.perf_counter() - t
t = time.perf_counter()
s = container.LockedFile(BIGL).unlock("Hatem", ADMIN_PW)
ms = s.mount()
t_open = time.perf_counter() - t
ds = ogr.Open(ms[0].path, 1)
lyr = ds.GetLayerByName(ms[0].name)
f = lyr.GetFeature(7)
f.SetField("note", "changed")
lyr.SetFeature(f)
ds = None
t = time.perf_counter()
s.save()
t_save = time.perf_counter() - t
s.close()
PERF = (f"{size_mb:.0f} MB GeoPackage (92,000 polygons of 19 vertices, 2 layers): lock{t_lock:.1f} s, unlock+open {t_open:.1f} s, "
        f"save after an edit {t_save:.1f} s; locked file {os.path.getsize(BIGL) / 1e6:.0f} MB")
check(size_mb >= 45 and t_lock < 30 and t_open < 15 and t_save < 30, "performance: " + PERF)

# ---- the program side: layers on the map, the edit guard, saving on commit, wiping on close --------------------------
from biruni.secure import runtime  # noqa: E402

s = container.LockedFile(SARA_COPY).unlock("Sara", "Sara-Edits-Roads-77")
entry = runtime.attach(None, s)
maplayers = {QgsProject.instance().mapLayer(i).name(): QgsProject.instance().mapLayer(i) for i in entry["layers"]}
check(set(maplayers) == {"Parcels", "Roads", "Points"} and all(x.isValid() for x in maplayers.values()),
      "Sara's layers are on the map, read from memory")
check(maplayers["Parcels"].readOnly() and not maplayers["Roads"].readOnly(), "view-only Parcels is read-only on the map")
check("owner_phone" not in maplayers["Parcels"].fields().names(), "the map layer has no owner_phone field")
check(runtime.allowed(maplayers["Roads"], "edit") and not runtime.allowed(maplayers["Roads"], "delete")
      and runtime.allowed(maplayers["Roads"], "export") and not runtime.allowed(maplayers["Roads"], "copy"),
      "rights as the interface sees them: Roads edit yes, delete no, export yes, copy no")
roads = maplayers["Roads"]
roads.startEditing()
roads.deleteFeature(2)
check(runtime._violation(roads, s.rights[L["Roads"]]) == "delete", "the edit guard spots a deletion without the right")
roads.rollBack()
roads.startEditing()
fid = next(roads.getFeatures()).id()
roads.changeAttributeValue(fid, roads.fields().indexOf("name"), "renamed by Sara")
check(runtime._violation(roads, s.rights[L["Roads"]]) is None, "an allowed edit passes the guard")
n_before = len(container.LockedFile(SARA_COPY).appended)
check(roads.commitChanges(), "Sara commits her edit")
check(len(container.LockedFile(SARA_COPY).appended) >= n_before + 2,
      "committing on the map writes a signed change set (and a log entry) into the locked file")
folder = s.folder
for i in list(entry["layers"]):
    QgsProject.instance().removeMapLayer(i)
del maplayers, roads
for _ in range(5):
    app.processEvents()
check(s.closed and not gdal.ReadDir(f"{gpkg.ROOT}/{folder}"), "closing the layers wipes the memory copies")
check(not runtime.any_open(), "no locked file left open")

# ---- dialogs: behaviour, then pictures in English and Arabic --------------------------------------------------------
from qgis.PyQt.QtCore import Qt  # noqa: E402

from biruni import i18n  # noqa: E402
from biruni.secure import dialogs  # noqa: E402

PNG = os.environ.get("BIRUNI_SECURE_PNG") or os.path.join(WORK, "png")
os.makedirs(PNG, exist_ok=True)
I18N = os.environ.get("BIRUNI_SECURE_I18N", "")


def grab(w, name, size=None):
    if size:
        w.resize(*size)
    w.show()
    for _ in range(3):
        app.processEvents()
    print("grab", name, flush=True)
    w.grab().save(os.path.join(PNG, name + ".png"))


def set_lang(code):
    if code == "en":
        i18n._lang, i18n._strings = "en", {}
        app.setLayoutDirection(Qt.LayoutDirection.LeftToRight)
        return
    strings = {}
    if I18N and os.path.exists(I18N):
        with open(I18N, encoding="utf-8") as f_:
            strings = {k: v[0] for k, v in json.load(f_).items()}
    i18n._lang, i18n._code, i18n._strings = "ar", "ar", strings
    app.setLayoutDirection(Qt.LayoutDirection.RightToLeft)


# the wizard, driven as a user would
WIZ_OUT = os.path.join(OUT, "wizard.bgpkg")
w = dialogs.LockDatabaseWizard(None, None, SRC)
w.out.setText(WIZ_OUT)
check(w.chosen_layers() == ["Parcels", "Roads", "Points"], "wizard lists the GeoPackage's 3 layers, all ticked")
w._next()
w.admin_name.setText("Hatem")
w.pw1.setText("password123")
w.pw2.setText("password123")
check(not w.meter.update_for("password123"), "wizard meter refuses a common password")
w.pw1.setText("Desert-Falcon-2026!x")
w.pw2.setText("Desert-Falcon-2026!x")
w._next()
check(w.stack.currentIndex() == 2 and w.code and not w.next.isEnabled(),
      "recovery key shown; Lock stays disabled until 'I saved it' is ticked")
pdf = os.path.join(WORK, "recovery.pdf")
dialogs.recovery_pdf(pdf, w.code, "wizard.bgpkg", dialogs.qr_png(w.code))
check(os.path.getsize(pdf) > 2000, "recovery key saved as a printable PDF with its QR code")
w.saved_it.setChecked(True)
w._next()
check(w.stack.currentIndex() == 3 and os.path.exists(WIZ_OUT), "wizard locked the database")
wiz_code = w.code
w.close()
s = container.LockedFile(WIZ_OUT).recover(wiz_code)
check(s.is_admin, "the recovery key shown by the wizard opens the file it made")
s.close()

# login
ld = dialogs.LoginDialog(MASTER, None, None)
ld.user.setText("Hatem")
ld.pw.setText("bad-password-000")
ld._open()
check(ld.session is None and not ld.error.isHidden(), "login: a wrong password shows a message and opens nothing")
ld.pw.setText(ADMIN_PW)
ld._open()
check(ld.session is not None and ld.session.is_admin, "login: the right password opens the session")
adm = ld.session
ld.close()

# users and permissions
ud = dialogs.UsersDialog(adm, None, None)
check(ud.grid.rowCount() == 3 and ud.grid.columnCount() == 3, "permissions grid: 3 users x 3 layers")
cell = ud.grid.cellWidget(0, [x["name"] for x in adm.layers()].index("Points"))
cell.buttons["d"].setChecked(True)
uid0 = ud._grid_users()[0]["uid"]
check("v" in adm.users()[[u["uid"] for u in adm.users()].index(uid0)]["rights"].get(L["Points"], ""),
      "ticking delete in the grid also turns view on")
cell.buttons["v"].setChecked(False)
check(not next(u for u in adm.users() if u["uid"] == uid0)["rights"].get(L["Points"]),
      "unticking view clears every right of that cell")
check(ud.atable.rowCount() >= 9, f"activity tab lists {ud.atable.rowCount()} signed entries")
ud.show_report(rep)
check(ud.report.count() == len(rep), "merge report shown line by line")
check(any("Ali" in ud.report.item(i).text() and "3" in ud.report.item(i).text() for i in range(ud.report.count())),
      "the report says Ali tried to delete 3 features")


def shots(lang):
    set_lang(lang)
    w_ = dialogs.LockDatabaseWizard(None, None, SRC)
    w_.out.setText(WIZ_OUT)
    grab(w_, f"{lang}_wizard_1_source", (640, 560))
    w_._go(1)
    w_.admin_name.setText("Hatem")
    w_.pw1.setText("Desert-Falcon")
    w_.meter.update_for("Desert-Falcon")
    grab(w_, f"{lang}_wizard_2_admin", (640, 600))
    w_._go(2)
    grab(w_, f"{lang}_wizard_3_recovery", (640, 560))
    w_.out_path = WIZ_OUT
    w_.done_title.setText(dialogs.tr("The database is locked."))
    w_.done_text.setText(dialogs.tr("{file} - {n} layers. Add users and give each one his rights in Users and "
                                    "permissions, then send each one his copy.").format(file="town.bgpkg", n=3))
    w_._go(3)
    grab(w_, f"{lang}_wizard_4_done", (640, 600))
    w_.close()
    l_ = dialogs.LoginDialog(MASTER, None, None)
    l_.user.setText("Sara")
    l_.show_error(dialogs.lock_error_text(container.LockError("wrong_password")))
    grab(l_, f"{lang}_login", (460, 300))
    l_._toggle()
    grab(l_, f"{lang}_login_recovery", (480, 380))
    l_.close()
    u_ = dialogs.UsersDialog(adm, None, None)
    for i, n in enumerate(("users", "permissions", "send_merge", "activity")):
        u_.tabs.setCurrentIndex(i)
        if n == "permissions":
            u_.grid.setCurrentCell(1, [x["name"] for x in adm.layers()].index("Parcels"))
        if n == "send_merge":
            u_.show_report(rep)
        grab(u_, f"{lang}_users_{n}", (940, 720))
    u_.close()
    a_ = dialogs.UserDialog(None, None)
    a_.name.setText("Mona")
    a_.pw1.setText("Mona-Maps-Riyadh")
    a_.meter.update_for("Mona-Maps-Riyadh")
    grab(a_, f"{lang}_add_user", (480, 420))
    a_.close()
    p_ = dialogs.LockProjectDialog(PRJ, None, None)
    grab(p_, f"{lang}_lock_project", (600, 640))
    p_.close()
    r_ = dialogs.RemoveLockDialog(BPROJ, None, None)
    grab(r_, f"{lang}_remove_lock", (560, 330))
    r_.close()


# from here on, closing the users window asks "Save the changes…?" when users or rights changed: answer Discard
# (a real box would wait forever off screen)
from qgis.PyQt.QtWidgets import QMessageBox as _Box  # noqa: E402

_Box.question = staticmethod(lambda *a, **k: _Box.StandardButton.Discard)
shots("en")
shots("ar")
set_lang("en")
check(len([n for n in os.listdir(PNG) if n.endswith(".png")]) >= 26, f"dialog pictures written to {PNG}")
ud.close()
adm.close()

# ---- decrypted data never reached a disk ---------------------------------------------------------------------------
for _ in range(3):
    app.processEvents()
AFTER = snapshot(SCAN_DIRS + [OUT])
plain_dirs = [os.path.normcase(os.path.join(WORK, d)) for d in ("town.gpkg", "big.gpkg", "Project", "unlocked")]
scanned, leaks = 0, []
for p, st in AFTER.items():
    if BEFORE.get(p) == st:
        continue
    np_ = os.path.normcase(p)
    if any(np_.startswith(d) for d in plain_dirs) or np_.endswith(".pdf"):
        continue  # the plaintext sources the test itself wrote, the unlocked folder, the recovery PDF
    try:
        if st[0] > 400_000_000:
            continue
        with open(p, "rb") as f_:
            data = f_.read()
    except OSError:
        continue
    scanned += 1
    if MARK in data:
        leaks.append(p)
check(not leaks, f"no decrypted value in any of the {scanned} files written or changed on disk during the run"
      + (f": {leaks[:5]}" if leaks else ""))
leftover = [n for n in (gdal.ReadDir(gpkg.ROOT) or []) if gdal.ReadDir(f"{gpkg.ROOT}/{n}")]
check(not leftover, "no decrypted copy left in memory after closing")

print()
print("Performance:", PERF)
print(f"Pictures: {PNG}")
shutil.rmtree(WORK, ignore_errors=True)
app.exitQgis()
if failures:
    print(f"\n{len(failures)} FAILED")
    sys.exit(1)
print("\nALL PASS")
