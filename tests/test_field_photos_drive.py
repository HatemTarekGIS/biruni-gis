r"""Field photos through the user's own Google Drive: the .bfp file (field_photos/drive.py).

Checked: a file made on this PC and one made by the phone page's own script (in node, WebCrypto) both open here;
every way a file can be wrong is refused - damaged, re-signed, from another office, by a phone that never scanned
this office's code, sealed for another computer; and a certified surveyor key reaches PhotoStore.save's rules.

Run:  engine\bin\python-qgis.bat tests\test_field_photos_drive.py
"""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "app", "plugins"))
sys.path.insert(0, os.path.join(HERE, "..", "app", "python"))

from biruni.vendor import ensure_vendor  # noqa: E402

ensure_vendor()
from cryptography.hazmat.primitives import serialization  # noqa: E402
from cryptography.hazmat.primitives.asymmetric import ec  # noqa: E402

from biruni.field_photos import bridge, drive  # noqa: E402
from biruni.field_photos.handover import HandoverKey, seal  # noqa: E402

failures = []


def check(cond, msg):
    print(("PASS " if cond else "FAIL ") + msg, flush=True)
    if not cond:
        failures.append(msg)


def refused(blob, why, **kw):
    try:
        drive.verify(blob, kw.get("kas", KAS), kw.get("kid", KID), kw.get("room", ROOM))
    except drive.BfpError as e:
        return why in str(e)
    return False


work = tempfile.mkdtemp()
node = shutil.which("node") or next((p for p in (r"C:\Program Files\nodejs\node.exe",) if os.path.isfile(p)), None)
pc = HandoverKey(os.path.join(work, "pc"))
other_pc = HandoverKey(os.path.join(work, "other"))
k, k2 = bridge.new_key(), bridge.new_key()
SK = "s3cret-surveyor-key"
KAS = drive.auth_keys(k, {"surveyors": {"a1": {"id": "a1", "name": "Ali", "secret": SK}}})
KID = drive.key_id(pc.public)
ROOM = drive.room_tag(k)
PHOTO = b"\xff\xd8\xff\xe0" + os.urandom(3000)
META = {"id": "ph-1", "t": 1763370000000, "lat": 24.7, "lon": 46.7}


def phone_key():
    key = ec.generate_private_key(ec.SECP256R1())
    raw = key.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
    return key, drive._b64u(raw)


def make(sk="", key_k=k, pk=None, n=1, dev="DEV1", keys=None, **over):
    key, pub = keys or phone_key()
    body = seal(pk or pc.public, META, PHOTO)
    h = {"room": drive.room_tag(key_k), "dev": dev, "kid": drive.key_id(pk or pc.public), "id": "ph-1", "n": n,
         "t": META["t"], "pub": pub, "cert": drive.cert(drive.auth_key(key_k, sk), pub, dev)}
    h.update(over)
    return drive.pack(h, body, key)


# ---- names and folders ---------------------------------------------------------------------------------------------
check(drive.folder_name(k).startswith("biruni-photos-") and len(ROOM) == 16 and ROOM != drive.room_tag(k2)
      and k not in drive.folder_name(k), "each office has its own Drive folder, and its name does not reveal the key")
check(set(KAS) == {"", SK} and KAS[""] != KAS[SK], "the shared code and each surveyor's own code certify apart")

# ---- a file made here ------------------------------------------------------------------------------------------------
blob, name = make()
h, body, sk = drive.verify(blob, KAS, KID, ROOM)
m = drive.NAME_RE.match(name)
check(m and int(m.group(1)) == 1 and h["sha"].startswith(m.group(2)), f"the file name is its number and hash ({name})")
meta, data = pc.open(body)
check(sk == "" and meta == META and data == PHOTO and h["size"] == len(body),
      "a photo from the shared code opens: the same data and picture")
h, _, sk = drive.verify(make(sk=SK)[0], KAS, KID, ROOM)
check(sk == SK, "a surveyor's own code is recognised (PhotoStore.save then applies its rules)")

# ---- what is refused -----------------------------------------------------------------------------------------------
check(refused(b"BFP2" + blob[4:], "not a .bfp"), "not a .bfp file")
check(refused(blob[:40], "damaged"), "a cut file")
bad = bytearray(blob)
bad[-5] ^= 1
check(refused(bytes(bad), "damaged body"), "a changed byte in the photo")
hl = int.from_bytes(blob[4:6], "big")
bad = bytearray(blob)
bad[6 + hl + 3] ^= 1
check(refused(bytes(bad), "bad signature"), "a changed signature")
# someone with Drive access rewrites the header (another device name) and signs it with their own key
hdr = json.loads(blob[6:6 + hl])
key, pub = phone_key()
hdr.pop("v"), hdr.pop("size"), hdr.pop("sha")
check(refused(drive.pack(dict(hdr, pub=pub), blob[6 + hl + 64:], key)[0], "not from this office"),
      "a file re-signed by a phone that never scanned this office's code")
same = phone_key()
c1 = json.loads(make(keys=same)[0][6:6 + hl])["cert"]
check(refused(make(dev="DEV2", keys=same, cert=c1)[0], "not from this office"),
      "a phone's cert moved to another device name (same phone key)")
check(refused(make(key_k=k2)[0], "another office"), "a file of another office (other key)")
check(refused(make(key_k=k2, room=ROOM)[0], "not from this office"), "… even when it claims this office's folder")
check(refused(make(sk="guessed")[0], "not from this office"), "a made-up surveyor key")
check(refused(make(pk=other_pc.public)[0], "another computer"), "a file sealed for another computer")
check(refused(make(dev="../x")[0], "unknown header"), "a device name that could leave the folder")
b2, _ = make(t=True)
check(refused(b2, "unknown header"), "a header with a wrong type")

# ---- the phone page's own script makes the same file (node, WebCrypto) ----------------------------------------------
if node:
    core = bridge.CORE_JS
    helpers = "\n".join(re.search(r"^const " + n + r"=.*?(?=^const |\Z)", core, re.S | re.M).group(0)
                        for n in ("enc", "b64u", "hex", "cat"))
    body = seal(pc.public, META, PHOTO)
    with open(os.path.join(work, "body.bin"), "wb") as f:
        f.write(body)
    js = os.path.join(work, "bfp.mjs")
    with open(js, "w", encoding="utf-8") as f:
        f.write(helpers + drive.BFP_JS + r"""
import fs from 'fs';
const [k,sk,pk,dir]=process.argv.slice(2);
const kp=await crypto.subtle.generateKey({name:'ECDSA',namedCurve:'P-256'},false,['sign','verify']);
const pub=b64u.enc(await crypto.subtle.exportKey('raw',kp.publicKey));
const body=new Uint8Array(fs.readFileSync(dir+'/body.bin'));
const h={room:await BFP.tag(k),dev:'ANDR01',kid:await BFP.kid(pk),id:'ph-1',n:42,t:1763370000000,pub:pub,
 cert:await BFP.cert(k,sk,pub,'ANDR01')};
const r=await BFP.pack(h,body,kp.privateKey);
fs.writeFileSync(dir+'/'+r.name,r.bytes);console.log(r.name);
""")
    r = subprocess.run([node, js, k, SK, pc.public, work], capture_output=True, text=True, timeout=60)
    name = r.stdout.strip()
    check(r.returncode == 0 and drive.NAME_RE.match(name) and name.startswith("000042-"),
          f"the page script makes a .bfp ({name}) {r.stderr[:300]}")
    try:
        with open(os.path.join(work, name), "rb") as f:
            h, b, sk = drive.verify(f.read(), KAS, KID, ROOM)
        meta, data = pc.open(b)
        ok = sk == SK and h["dev"] == "ANDR01" and meta == META and data == PHOTO
    except (OSError, drive.BfpError, ValueError) as e:
        ok = False
        print("   ", e)
    check(ok, "… and this computer opens it: certified for the surveyor, signed, the same photo")
else:
    print("SKIP node not found: the page script was not checked")

shutil.rmtree(work, ignore_errors=True)
print(f"\n{'FAILED: ' + str(len(failures)) if failures else 'ALL PASSED'}", flush=True)
sys.exit(1 if failures else 0)
