r"""Verification seals for delivered files (seal/): the office key, sealing and checking, the link and its QR
code, Ed25519 against the RFC 8032 vectors (Python and the page's JavaScript), the checking page, and the two
Processing tools.

Run:  engine\bin\python-qgis.bat tests\test_seal.py
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

from qgis.core import QgsApplication, QgsSettings  # noqa: E402

SETTINGS_DIR = tempfile.mkdtemp()
os.environ["QGIS_CUSTOM_CONFIG_PATH"] = SETTINGS_DIR
app = QgsApplication([], False)
app.initQgis()
sys.path.append(QgsApplication.prefixPath() + "/python/plugins")
from biruni.field_photos.tunnel import unprotect  # noqa: E402
from biruni.seal import core, ed25519  # noqa: E402
from biruni.seal import page as sp  # noqa: E402

failures = []


def check(cond, msg):
    print(("PASS " if cond else "FAIL ") + msg)
    if not cond:
        failures.append(msg)


work = tempfile.mkdtemp()

# ---- RFC 8032 test vectors (section 7.1, tests 1-3) ----------------------------------------------------------
VECTORS = [
    ("9d61b19deffd5a60ba844af492ec2cc44449c5697b326919703bac031cae7f60",
     "d75a980182b10ab7d54bfed3c964073a0ee172f3daa62325af021a68f707511a", "",
     "e5564300c360ac729086e2cc806e828a84877f1eb8e5d974d873e065224901555fb8821590a33bacc61e39701cf9b46bd25bf5f05"
     "95bbe24655141438e7a100b"),
    ("4ccd089b28ff96da9db6c346ec114e0f5b8a319f35aba624da8cf6ed4fb8a6fb",
     "3d4017c3e843895a92b70aa74d1b7ebc9c982ccf2ec4968cc0cd55f12af4660c", "72",
     "92a009a9f0d4cab8720e820b5f642540a2b27b5416503f8fb3762223ebdb69da085ac1e43e15996e458f3613d0f11d8c387b2eaeb43"
     "02aeeb00d291612bb0c00"),
    ("c5aa8df43f9f837bedb7442f31dcb7b166d38535076f094b85ce3a2e0b4458f7",
     "fc51cd8e6218a1a38da47ed00230f0580816ed13ba3303ac5deb911548908025", "af82",
     "6291d657deec24024827e69c3abe01a30ce548a284743a445e3680d7db5ac3ac18ff9b538d16f290ae67f760984dc6594a7c15e9716"
     "ed28dc027beceea1ec40a"),
]
check(core._lib() is not None, "the bundled cryptography library signs (Ed25519)")
for n, (sk, pk, msg, sig) in enumerate(VECTORS, 1):
    sk, pk, msg, sig = (bytes.fromhex(x) for x in (sk, pk, msg, sig))
    check(ed25519.public_key(sk) == pk, f"RFC 8032 test {n}: plain-Python public key")
    check(ed25519.sign(sk, msg) == sig, f"RFC 8032 test {n}: plain-Python signature")
    check(ed25519.verify(pk, sig, msg), f"RFC 8032 test {n}: plain-Python verify")
    check(not ed25519.verify(pk, sig, msg + b"x"), f"RFC 8032 test {n}: plain-Python rejects another message")
    bad = bytearray(sig)
    bad[5] ^= 1
    check(not ed25519.verify(pk, bytes(bad), msg), f"RFC 8032 test {n}: plain-Python rejects a changed signature")
    check(core.public_key(sk) == pk and core.sign(sk, msg) == sig and core.verify(pk, sig, msg),
          f"RFC 8032 test {n}: library key, signature and verify")
check(not ed25519.verify(b"\xff" * 32, b"\x00" * 64, b"") and not core.verify(b"\xff" * 32, b"\x00" * 64, b""),
      "a key that is not a curve point is refused")

# ---- the office key: made once, kept encrypted -----------------------------------------------------------------
check(not QgsSettings().value(core.KEY + "key", ""), "no key before the first seal")
seed, pub = core.key_pair()
stored = QgsSettings().value(core.KEY + "key", "")
seed2, pub2 = core.key_pair()
check(len(seed) == 32 and len(pub) == 32, "the key pair is Ed25519 (32-byte seed and public key)")
check(seed == seed2 and pub == pub2 and QgsSettings().value(core.KEY + "key", "") == stored,
      "the key is made once and kept")
seed_b64 = base64.b64encode(seed).decode("ascii")
check(stored and seed_b64 not in stored and seed.hex() not in stored, "the stored private key is not readable")
check(unprotect(stored) == seed_b64, "the stored private key is encrypted for this Windows user (DPAPI)")
check(QgsSettings().value(core.KEY + "public", "") == core.b64(pub), "the public key is kept beside it")
fp = core.fingerprint(pub)
check(len(fp) == 19 and fp.count(" ") == 3, "the key fingerprint is 4 groups of 4: " + fp)
core.set_office("مكتب حاتم طارق للمساحة")
check(core.office() == "مكتب حاتم طارق للمساحة", "the office name is kept")

# ---- seal and check ---------------------------------------------------------------------------------------------
data = os.urandom(300000) + "قطعة ٤٥".encode("utf-8")
src = os.path.join(work, "parcel_1234_survey.zip")
with open(src, "wb") as f:
    f.write(data)
seal = core.seal_file(src)
for ext in (core.SEAL_EXT, core.QR_EXT, core.CERT_EXT):
    check(os.path.isfile(src + ext), "written next to the file: " + ext)
with open(src + core.SEAL_EXT, encoding="utf-8") as f:
    js = json.load(f)
check(js["name"] == "parcel_1234_survey.zip" and js["size"] == len(data) and js["office"] == core.office(),
      "the seal holds name, size and office")
check(js["fingerprint"] == fp and js["url"].startswith(core.PAGE_URL + "#"), "the seal holds fingerprint and link")
status, d = core.check_file(src)
check(status == core.UNCHANGED, "the sealed file checks as unchanged: " + status)
check(d["office"] == core.office() and d["fingerprint"] == fp, "the check tells the office and the key")
status, _ = core.check_file(src, js["url"])
check(status == core.UNCHANGED, "the file checks as unchanged from the link alone: " + status)
renamed = os.path.join(work, "received.zip")
shutil.copy(src, renamed)
check(core.check_file(renamed, src + core.SEAL_EXT)[0] == core.UNCHANGED, "a renamed copy is still unchanged")

changed = os.path.join(work, "changed.zip")
for pos in (0, len(data) // 2, len(data) - 1):
    b = bytearray(data)
    b[pos] ^= 0x01
    with open(changed, "wb") as f:
        f.write(b)
    check(core.check_file(changed, src + core.SEAL_EXT)[0] == core.CHANGED, f"one bit changed at {pos}: changed")
    check(core.check_file(changed, js["url"])[0] == core.CHANGED, f"one bit changed at {pos}: changed (link)")
with open(changed, "wb") as f:
    f.write(data + b"\0")
check(core.check_file(changed, src + core.SEAL_EXT)[0] == core.CHANGED, "one byte added: changed")

# tampered seals: the signature no longer matches
for field, value in (("office", "Another office"), ("t", js["t"] - 86400), ("name", "other.zip"),
                     ("size", js["size"] + 1)):
    t = dict(js, **{field: value})
    path = os.path.join(work, "tampered.seal.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(t, f)
    check(core.check_file(src, path)[0] == core.BAD_SIGNATURE, f"seal with an edited {field}: bad signature")
# a seal made for a changed file, with the hash edited to match it
with open(changed, "wb") as f:
    f.write(b"X" + data[1:])
t = dict(js, sha256=core.file_sha256(changed))
with open(path, "w", encoding="utf-8") as f:
    json.dump(t, f)
check(core.check_file(changed, path)[0] == core.BAD_SIGNATURE, "seal with an edited hash: bad signature")
# edited link: office bytes changed, signature kept
raw = bytearray(core.unb64(js["url"].split("#")[1]))
i = raw.index(core.office().encode("utf-8"))
raw[i + 1] ^= 0x01
check(core.check_file(src, core.b64(bytes(raw)))[0] in (core.BAD_SIGNATURE, core.BAD_SEAL),
      "link with an edited office: refused")
raw = bytearray(core.unb64(js["url"].split("#")[1]))
raw[-1] ^= 0x01
check(core.check_file(src, core.b64(bytes(raw)))[0] == core.BAD_SIGNATURE, "link with an edited signature: bad")
check(core.check_file(src, "not a seal")[0] == core.BAD_SEAL, "garbage instead of a seal: no seal")
check(core.check_file(src, js["url"][:-20])[0] == core.BAD_SEAL, "a cut link: no seal")
check(core.check_file(changed)[0] == core.BAD_SEAL, "no seal next to the file: no seal")

# the plain-Python fallback makes the same seals
lib = core._lib
core._lib = lambda: None
other = os.path.join(work, "plain.pdf")
with open(other, "wb") as f:
    f.write(b"%PDF-1.7 " + os.urandom(5000))
s2 = core.make_seal(other, "Office", now=1700000000)
check(core.check_file(other, s2["url"])[0] == core.UNCHANGED, "without the library: sealed and checked")
core._lib = lib
check(core.check_file(other, s2["url"])[0] == core.UNCHANGED, "a plain-Python seal checks with the library")
check(core.make_seal(other, "Office", now=1700000000)["signature"] == s2["signature"],
      "library and plain Python sign the same (deterministic Ed25519)")

# ---- the link and its QR code ------------------------------------------------------------------------------------
check(len(js["url"]) < 300, f"the link is short for a QR code: {len(js['url'])} characters")
longest = os.path.join(work, "مخطط_تقسيم_الأرض_رقم_١٢٣٤٥_النسخة_النهائية_المعتمدة.gpkg")
with open(longest, "wb") as f:
    f.write(os.urandom(5 * 1024 * 1024))
s3 = core.make_seal(longest, "مكتب المهندس حاتم طارق فتحي للاستشارات الهندسية والمساحية والخدمات الجغرافية")
check(len(s3["url"]) <= 340, f"the longest link stays scannable: {len(s3['url'])} characters")
check(s3["name"].endswith("…") and len(s3["name"].encode("utf-8")) <= core.NAME_BYTES,
      "a long file name is cut on a character boundary: " + s3["name"])
check(len(s3["office"].encode("utf-8")) <= core.OFFICE_BYTES, "a long office name is cut")
check(core.check_file(longest, s3["url"])[0] == core.UNCHANGED, "a seal with cut names still checks")
u = core.unpack_link(js["url"])
check(u["name"] == js["name"] and u["office"] == js["office"] and u["size"] == js["size"] and u["t"] == js["t"]
      and js["sha256"].startswith(u["sha256_start"]), "the link unpacks to the seal")
with open(src + core.QR_EXT, "rb") as f:
    png = f.read()
check(png.startswith(b"\x89PNG\r\n\x1a\n") and len(png) > 300, "the QR code is a PNG")
with open(src + core.CERT_EXT, encoding="utf-8") as f:
    cert = f.read()
check("data:image/png;base64," in cert and fp in cert and js["sha256"] in cert and core.office() in cert,
      "the certificate shows QR code, fingerprint, hash and office")
check(js["url"].replace("&", "&amp;") in cert, "the certificate carries the link")

# ---- the checking page ---------------------------------------------------------------------------------------
html = sp.public_page()
langs = ["en", "ar", "es", "pt_BR", "fr", "tr", "fa"]
check(sorted(sp.TEXTS) == sorted(langs), "the page has the 7 languages")
import re  # noqa: E402

for lng in langs[1:]:
    check(set(sp.TEXTS[lng]) == set(sp.TEXTS["en"]), f"{lng}: every text of the page is translated")
    same = all(sorted(re.findall(r"\{\w+\}", sp.TEXTS[lng][k])) == sorted(re.findall(r"\{\w+\}", v))
               for k, v in sp.TEXTS["en"].items())
    check(same, f"{lng}: the placeholders are kept")
check("Biruni" in sp.TEXTS["fa"]["title"], "fa keeps the name Biruni in Latin letters")
check("__TEXTS__" not in html and "__SHIELD__" not in html and all(f'"{k}":' in html for k in langs),
      "the page embeds every language")
check("<\\/" not in html.split("<script>")[1].split("const ALL=")[0], "the page's own code is not escaped")
check("فارسی" in html and "Türkçe" in html and "dir=" in html, "the page has the language menu and RTL")

node = shutil.which("node") or next((p for p in (r"C:\Program Files\nodejs\node.exe",) if os.path.isfile(p)), None)
if node:
    js_code = html.split("<script>")[1].split("</script>")[0]
    path = os.path.join(work, "page.js")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(js_code)
    out = subprocess.run([node, "--check", path], capture_output=True, text=True)
    check(out.returncode == 0, "the page script has no syntax error " + out.stderr[:300])

    # the page's own hashing and signature code, run in node against node's crypto and the RFC vectors
    with open(changed, "wb") as f:
        b = bytearray(data)
        b[1000] ^= 0x80
        f.write(b)
    inputs = {"vectors": VECTORS, "link": js["url"].split("#")[1], "file": data.hex(), "changed": bytes(b).hex(),
              "fingerprint": fp, "link3": s3["url"].split("#")[1],
        "name3": s3["name"], "office3": s3["office"],
        "badlink": core.b64(bytes(raw))}
    with open(os.path.join(work, "in.json"), "w", encoding="utf-8") as fh:
        json.dump(inputs, fh, ensure_ascii=False)
    test_js = sp.core_js() + r"""
const fs=require('fs'),nc=require('crypto'),I=JSON.parse(fs.readFileSync(process.argv[2],'utf8')),R={};
const H=s=>Uint8Array.from(Buffer.from(s,'hex'));
R.vectors=I.vectors.map(([sk,pk,m,s])=>edVerify(H(pk),H(s),H(m)));
R.vectorsBad=I.vectors.map(([sk,pk,m,s])=>edVerify(H(pk),H(s),cat(H(m),Uint8Array.of(1))));
let ok256=true,ok512=true;
for(const n of [0,1,55,56,63,64,65,119,120,127,128,129,1000,70001]){const d=nc.randomBytes(n);
 const want=nc.createHash('sha256').update(d).digest('hex');
 if(hex(new Sha256().update(d).digest())!==want)ok256=false;
 const h=new Sha256();for(let o=0;o<n;o+=17)h.update(d.subarray(o,o+17));if(hex(h.digest())!==want)ok256=false;
 if(n<3000&&hex(sha512(d))!==nc.createHash('sha512').update(d).digest('hex'))ok512=false}
R.sha256=ok256;R.sha512=ok512;
const s=unpack(I.link),f=H(I.file),c=H(I.changed);
const hf=new Sha256().update(f).digest(),hc=new Sha256().update(c).digest();
R.start=hex(hf).slice(0,16)===s.start;R.startChanged=hex(hc).slice(0,16)===s.start;
R.sig=edVerify(s.pub,s.sig,s.message(hf));R.sigChanged=edVerify(s.pub,s.sig,s.message(hc));
R.size=s.size===f.length;R.fp=fingerprint(s.pub)===I.fingerprint;
const s3=unpack(I.link3);R.names3=s3.name===I.name3&&s3.office===I.office3;
try{const b=unpack(I.badlink);R.badlink=edVerify(b.pub,b.sig,b.message(hf))}catch(e){R.badlink='refused'}
try{unpack(I.link.slice(0,-30));R.cut=false}catch(e){R.cut=true}
const k=nc.generateKeyPairSync('ed25519'),pk=k.publicKey.export({format:'jwk'}).x,m=nc.randomBytes(300);
R.nodeSig=edVerify(Uint8Array.from(Buffer.from(pk,'base64url')),Uint8Array.from(nc.sign(null,m,k.privateKey)),m);
console.log(JSON.stringify(R));
"""
    path = os.path.join(work, "core_test.js")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(test_js)
    out = subprocess.run([node, path, os.path.join(work, "in.json")], capture_output=True, text=True, timeout=300)
    try:
        r = json.loads(out.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        r = {}
        print(out.stdout[-500:], out.stderr[-1500:])
    check(r.get("vectors") == [True, True, True], "page JS: RFC 8032 vectors verify")
    check(r.get("vectorsBad") == [False, False, False], "page JS: RFC 8032 signatures refuse another message")
    check(r.get("nodeSig") is True, "page JS: verifies a signature made by node's own Ed25519")
    check(r.get("sha256") is True, "page JS: SHA-256 equals node's, in one piece and in pieces")
    check(r.get("sha512") is True, "page JS: SHA-512 equals node's")
    check(r.get("start") is True and r.get("size") is True and r.get("fp") is True,
          "page JS: reads the Python link (hash start, size, fingerprint)")
    check(r.get("sig") is True, "page JS: the Python seal's signature verifies with the file's hash")
    check(r.get("startChanged") is False and r.get("sigChanged") is False, "page JS: a changed file is caught")
    check(r.get("names3") is True, "page JS: reads cut Arabic names")
    check(r.get("badlink") in (False, "refused"), "page JS: an edited link is refused")
    check(r.get("cut") is True, "page JS: a cut link is refused")
else:
    print("SKIP node not found: page script not checked")

# ---- the Processing tools, headless ------------------------------------------------------------------------------
import processing  # noqa: E402
from processing.core.Processing import Processing  # noqa: E402

Processing.initialize()
from biruni.algorithms import BiruniProvider  # noqa: E402

provider = BiruniProvider()
QgsApplication.processingRegistry().addProvider(provider)
ids = {a.id() for a in provider.algorithms()}
check({"biruni:sealfiles", "biruni:checkseal"} <= ids, "both tools are in the Biruni provider")
f1, f2 = os.path.join(work, "a.dxf"), os.path.join(work, "b.pdf")
for p in (f1, f2):
    with open(p, "wb") as f:
        f.write(os.urandom(20000))
r = processing.run("biruni:sealfiles", {"INPUT": [f1, f2, src + core.SEAL_EXT], "OFFICE": "Biruni Survey Office",
                                         "CERTIFICATE": False})
check(r.get("COUNT") == 2, f"Seal files: 2 files sealed, the seal file skipped ({r.get('COUNT')})")
check(os.path.isfile(f1 + core.SEAL_EXT) and os.path.isfile(f2 + core.QR_EXT) and
      not os.path.isfile(f1 + core.CERT_EXT), "Seal files: seal and QR written, no certificate when not asked")
check(core.office() == "Biruni Survey Office" and r.get("FINGERPRINT") == fp, "Seal files: office kept, same key")
r = processing.run("biruni:checkseal", {"INPUT": f1})
check(r.get("RESULT") == core.UNCHANGED and r.get("OFFICE") == "Biruni Survey Office",
      f"Check a sealed file: unchanged ({r.get('RESULT')})")
with open(f2 + core.SEAL_EXT, encoding="utf-8") as f:
    link = json.load(f)["url"]
r = processing.run("biruni:checkseal", {"INPUT": f2, "LINK": link})
check(r.get("RESULT") == core.UNCHANGED, "Check a sealed file: unchanged from the link")
with open(f2, "r+b") as f:
    f.seek(100)
    f.write(b"\x00\x01")
r = processing.run("biruni:checkseal", {"INPUT": f2, "SEAL": f2 + core.SEAL_EXT})
check(r.get("RESULT") == core.CHANGED, f"Check a sealed file: changed ({r.get('RESULT')})")
r = processing.run("biruni:checkseal", {"INPUT": f2, "SEAL": os.path.join(work, "tampered.seal.json")})
check(r.get("RESULT") == core.BAD_SIGNATURE, f"Check a sealed file: bad signature ({r.get('RESULT')})")

shutil.rmtree(work, ignore_errors=True)
print("ALL PASS" if not failures else f"{len(failures)} FAILED")
app.exitQgis()
sys.exit(1 if failures else 0)
