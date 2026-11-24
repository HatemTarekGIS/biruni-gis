r"""No-loss P3: envelope v2 (field_photos/envelope.py and the phone page's sealFor2).

- the phone's JS and Python make the very same bytes from the same inputs (fixed eph, CK, iv);
- an envelope the JS sealed (random inputs) opens in Python for every recipient, and not for anyone else;
- a stripped header (a recipient dropped), a changed kid, a changed wrap or a changed ciphertext all fail;
- v1 (handover.seal) still opens, and HandoverKey.open dispatches on byte 0.

Run:  engine\bin\python-qgis.bat tests\test_envelope.py
"""
import base64
import json
import os
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "app", "plugins"))
sys.path.insert(0, os.path.join(HERE, "..", "app", "python"))

from cryptography.hazmat.primitives import serialization  # noqa: E402
from cryptography.hazmat.primitives.asymmetric import ec  # noqa: E402

from biruni.field_photos import envelope, handover, server  # noqa: E402
from biruni.field_photos.handover import HandoverKey  # noqa: E402

failures = []


def check(cond, msg):
    print(("PASS " if cond else "FAIL ") + msg, flush=True)
    if not cond:
        failures.append(msg)


def b64u(b):
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode("ascii")


def unb64u(s):
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def fails(fn, *a):
    try:
        fn(*a)
    except ValueError:
        return True
    return False


def page_js():
    """H64, hcat and the P3 block of the phone page, as they are shipped."""
    p = server.PAGE
    h0 = p.index("const H64=")
    h1 = p.index("const sleepH=")
    a = p.index("/* ---- no-loss P3")
    b = p.index("async function sealItem(it)")
    return p[h0:h1] + p[a:b]


work = tempfile.mkdtemp(prefix="biruni_p3_env_")
try:
    keys = [HandoverKey(os.path.join(work, f"pc{i}")) for i in range(3)]
    stranger = HandoverKey(os.path.join(work, "stranger"))
    pubs = [k.public for k in keys]
    meta = {"id": "ph-env-0001", "t": 1763370000000, "lat": 30.1, "lon": 31.2}
    pic = b"\xff\xd8\xff\xe0" + os.urandom(3000) + b"\xff\xd9"
    plain = envelope.pack_plain(meta, pic)

    eph = ec.generate_private_key(ec.SECP256R1())
    eph_pkcs8 = b64u(eph.private_bytes(serialization.Encoding.DER, serialization.PrivateFormat.PKCS8,
                                       serialization.NoEncryption()))
    eph_raw = b64u(eph.public_key().public_bytes(serialization.Encoding.X962,
                                                 serialization.PublicFormat.UncompressedPoint))
    ck, iv = os.urandom(32), os.urandom(12)
    py_fixed = envelope.seal_bytes(pubs, plain, _eph=eph, _ck=ck, _iv=iv)
    check(py_fixed[0] == 2 and py_fixed[1] == 3 and len(py_fixed) == 2 + 65 + 3 * 56 + 12 + len(plain) + 16,
          "layout: 0x02 | n | eph(65) | n x (kid 8 + wrap 48) | iv(12) | ct")
    check(envelope.kids(py_fixed) == [k.kid for k in keys], "the kids are drive.key_id of each recipient, in order")

    # ---- node: the page's own code ---------------------------------------------------------------------------------
    node = shutil.which("node") or r"C:\Program Files\nodejs\node.exe"
    script = os.path.join(work, "env.js")
    with open(script, "w", encoding="utf-8") as f:
        f.write("globalThis.crypto = require('crypto').webcrypto;\n")
        f.write("const store={};globalThis.localStorage={getItem:k=>store[k]??null,setItem:(k,v)=>{store[k]=v}};\n")
        f.write("function keep(k,v){if(v==null)delete store[k];else store[k]=v}function kept(k){return store[k]??null}\n")
        f.write("let ROOM='room1',PCKEY='';\n")
        f.write(page_js() + "\n")
        f.write(r"""
(async()=>{
 const inp=JSON.parse(process.argv[2]);
 const priv=await crypto.subtle.importKey('pkcs8',H64.dec(inp.eph),{name:'ECDH',namedCurve:'P-256'},true,['deriveBits']);
 const pub=await crypto.subtle.importKey('raw',H64.dec(inp.ephPub),{name:'ECDH',namedCurve:'P-256'},true,[]);
 const fixed=await sealFor2(inp.pubs,H64.dec(inp.plain),{eph:{privateKey:priv,publicKey:pub},ck:H64.dec(inp.ck),iv:H64.dec(inp.iv)});
 const rnd=await sealFor2(inp.pubs,H64.dec(inp.plain));
 const one=await sealFor2([inp.pubs[1]],H64.dec(inp.plain));
 console.log(JSON.stringify({fixed:H64.enc(fixed),rnd:H64.enc(rnd),one:H64.enc(one)}));
})().catch(e=>{console.error(String(e&&e.stack||e));process.exit(2)});
""")
    arg = json.dumps({"eph": eph_pkcs8, "ephPub": eph_raw, "pubs": pubs, "plain": b64u(plain), "ck": b64u(ck),
                      "iv": b64u(iv)})
    proc = subprocess.run([node, script, arg], capture_output=True, text=True, timeout=30)
    check(proc.returncode == 0, f"node ran the page's sealFor2 (rc={proc.returncode} {proc.stderr.strip()[:300]})")
    out = json.loads(proc.stdout.strip().splitlines()[-1]) if proc.returncode == 0 else {}
    js_fixed = unb64u(out.get("fixed", ""))
    check(js_fixed == py_fixed, "JS and Python make the same bytes from the same eph, CK and iv")
    js_rnd = unb64u(out.get("rnd", ""))
    for i, k in enumerate(keys):
        m, d = k.open(js_rnd)
        check(m == meta and d == pic, f"recipient {i + 1} of 3 opens what the phone sealed")
    check(fails(stranger.open, js_rnd), "a computer that is not on the list cannot open it")
    js_one = unb64u(out.get("one", ""))
    check(keys[1].open(js_one)[1] == pic and fails(keys[0].open, js_one), "a single-recipient v2 opens only for it")
    for i, k in enumerate(keys):
        check(k.open(py_fixed)[0]["id"] == "ph-env-0001", f"Python's envelope opens for recipient {i + 1}")

    # ---- a changed envelope ----------------------------------------------------------------------------------------
    hl = 2 + 65 + 3 * 56
    # stripped: drop recipient 3 from the header and say n = 2
    stripped = bytes([2, 2]) + js_rnd[2:2 + 65 + 2 * 56] + js_rnd[hl:]
    check(fails(keys[0].open, stripped) and fails(keys[1].open, stripped),
          "a header with a recipient dropped fails for everyone (the header is the payload's AAD)")
    # recipient 3's kid swapped for a stranger's: recipient 1 still unwraps CK, but the payload tag fails
    t = bytearray(js_rnd)
    o = 2 + 65 + 2 * 56
    t[o:o + 8] = bytes.fromhex(stranger.kid)
    check(fails(keys[0].open, bytes(t)), "a changed kid in the header fails")
    t = bytearray(js_rnd)
    t[2 + 65 + 8 + 5] ^= 1      # recipient 1's wrap
    check(fails(keys[0].open, bytes(t)), "a changed wrap: that recipient cannot open")
    check(fails(keys[1].open, bytes(t)), "a changed wrap: the others fail too (the wraps are inside the AAD)")
    t = bytearray(js_rnd)
    t[-20] ^= 1
    check(fails(keys[2].open, bytes(t)), "a changed ciphertext fails")
    t = bytearray(js_rnd)
    t[1] = 13
    check(fails(keys[0].open, bytes(t)), "more than 12 recipients is refused")
    check(fails(keys[0].open, js_rnd[:hl + 20]), "a truncated envelope fails")
    check(fails(envelope.seal_bytes, [pubs[0], pubs[0]], plain), "the same recipient twice is refused when sealing")
    check(fails(envelope.seal_bytes, [pubs[0]] * 13, plain), "13 recipients are refused when sealing")

    # ---- v1 still opens ----------------------------------------------------------------------------------------------
    v1 = handover.seal(keys[0].public, meta, pic)
    check(v1[0] == 1 and keys[0].open(v1) == (meta, pic), "v1 (handover.seal) still opens")
    check(fails(keys[1].open, v1), "v1 opens only for the one computer it was sealed for")
finally:
    shutil.rmtree(work, ignore_errors=True)

print("ALL PASS" if not failures else f"{len(failures)} FAILED", flush=True)
sys.exit(1 if failures else 0)
