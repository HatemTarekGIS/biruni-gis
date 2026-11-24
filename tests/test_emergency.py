r"""No-loss P3: the emergency key (field_photos/emergency.py).

- 5 share files with named holders; any 3 open a .bfp sealed v2 for office + emergency; 2 do not;
- a damaged share, shares of two different keys, and a v1 photo (office only) are refused;
- the private key is never written whole: every file written during the test is scanned for it (DER, base64, PEM).

Run:  engine\bin\python-qgis.bat tests\test_emergency.py
"""
import base64
import itertools
import json
import os
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "app", "plugins"))
sys.path.insert(0, os.path.join(HERE, "..", "app", "python"))

from cryptography.hazmat.primitives import serialization  # noqa: E402
from cryptography.hazmat.primitives.asymmetric import ec  # noqa: E402

from biruni.field_photos import drive, emergency, envelope, handover, recipients  # noqa: E402
from biruni.field_photos.handover import HandoverKey  # noqa: E402

failures = []


def check(cond, msg):
    print(("PASS " if cond else "FAIL ") + msg, flush=True)
    if not cond:
        failures.append(msg)


def b64u(b):
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode("ascii")


def refused(fn, *a):
    try:
        fn(*a)
    except ValueError as e:
        return str(e) or "refused"
    return ""


work = tempfile.mkdtemp(prefix="biruni_p3_emg_")
try:
    folder = os.path.join(work, "cert")
    office = HandoverKey(folder)
    shares_dir = os.path.join(work, "shares")
    holders = ["أحمد", "Sara", "Omar/IT", "منى", "Khaled"]
    pub, paths = emergency.create(shares_dir, holders)
    check(len(paths) == 5 and all(os.path.isfile(p) for p in paths), "5 share files are written")
    docs = [json.load(open(p, encoding="utf-8")) for p in paths]
    check([d["holder"] for d in docs] == holders and [d["x"] for d in docs] == [1, 2, 3, 4, 5]
          and all(d["v"] == "BEK1" and d["pub"] == pub and d["k"] == 3 for d in docs),
          "each file names its holder, its part (1..5), the rule (3 of 5) and the public key")
    check(all("/" not in os.path.basename(p) for p in paths), "holder names are made safe for file names")
    recipients.add(folder, pub, "emergency", "Emergency key", office_pub=office.public)
    check([e["role"] for e in recipients.load(folder)["r"]] == ["emergency"], "the emergency key joins the list")

    # a phone's .bfp, sealed v2 for office + emergency (the header names the office, v stays 1)
    meta = {"id": "ph-emg-0001", "t": 1763370000000, "lat": 30.0, "lon": 31.0}
    pic = b"\xff\xd8\xff\xe0" + os.urandom(2000) + b"\xff\xd9"
    body = envelope.seal([office.public, pub], meta, pic)
    phone = ec.generate_private_key(ec.SECP256R1())
    ppub = b64u(phone.public_key().public_bytes(serialization.Encoding.X962,
                                                serialization.PublicFormat.UncompressedPoint))
    k = "k" * 43
    hdr = {"room": drive.room_tag(k), "dev": "dev12345", "kid": office.kid, "id": meta["id"], "n": 1,
           "t": meta["t"], "pub": ppub, "cert": drive.cert(drive.auth_key(k), ppub, "dev12345")}
    bfp, name = drive.pack(hdr, body, phone)
    bfp_path = os.path.join(work, "in", name)
    os.makedirs(os.path.dirname(bfp_path))
    open(bfp_path, "wb").write(bfp)
    check(json.loads(bfp[6:6 + int.from_bytes(bfp[4:6], "big")])["v"] == 1, "the .bfp header stays v:1")
    check(office.open(drive.verify(bfp, drive.auth_keys(k, {}), office.kid, drive.room_tag(k))[1])[0] == meta,
          "the office still opens it through the normal Drive checks")

    ok_all = True
    for combo in itertools.combinations(paths, 3):
        key = emergency.recover(list(combo))
        m, d = emergency.open_blob(key, bfp)
        ok_all = ok_all and m == meta and d == pic
    check(ok_all, "every choice of 3 of the 5 parts opens the .bfp")
    out = emergency.open_files(paths[1:4], [bfp_path], os.path.join(work, "opened"))
    check(len(out["opened"]) == 1 and open(out["opened"][0][1], "rb").read() == pic,
          "open_files writes the picture out with 3 parts")
    check(all(refused(emergency.recover, list(c)) for c in itertools.combinations(paths, 2)),
          "2 parts do not make the key (any pair)")
    check(refused(emergency.recover, [paths[0], paths[0], paths[1]]) != "", "the same part twice does not count as 3")
    bad = dict(docs[2], share=docs[2]["share"][:-2] + ("AA" if docs[2]["share"][-2:] != "AA" else "BB"))
    check(refused(emergency.recover, [docs[0], docs[1], bad]) == "a damaged share", "a damaged part is caught")
    pub2, paths2 = emergency.create(os.path.join(work, "shares2"), ["a", "b", "c", "d", "e"])
    check(refused(emergency.recover, [paths[0], paths[1], paths2[2]]) == "shares of different keys",
          "parts of two different keys are refused")
    check(refused(emergency.open_blob, emergency.recover(paths2[:3]), bfp) != "",
          "another emergency key cannot open it")
    v1 = handover.seal(office.public, meta, pic)
    check("emergency key existed" in refused(emergency.open_blob, emergency.recover(paths[:3]), v1),
          "a v1 photo (sealed for the office only) says why it cannot be opened")
    check(refused(emergency.create, os.path.join(work, "x"), ["a", "b", "", "d", "e"]) != "",
          "five holder names are needed")

    # ---- the private key never touches the disk whole --------------------------------------------------------------
    key = emergency.recover(paths[:3])
    der = key.private_bytes(serialization.Encoding.DER, serialization.PrivateFormat.PKCS8,
                            serialization.NoEncryption())
    raw_d = key.private_numbers().private_value.to_bytes(32, "big")
    needles = [der, raw_d, b64u(der).encode(), base64.b64encode(der), b64u(raw_d).encode(), b"PRIVATE KEY"]
    hits = []
    scanned = 0
    for root, _dirs, files in os.walk(work):
        for fn in files:
            data = open(os.path.join(root, fn), "rb").read()
            scanned += 1
            if any(n in data for n in needles):
                hits.append(fn)
    # the office's own handover key file is DPAPI-protected, so it holds no PEM in clear either
    check(scanned >= 12 and not hits, f"no file written holds the emergency private key ({scanned} files scanned, "
                                      f"hits: {hits})")
finally:
    shutil.rmtree(work, ignore_errors=True)

print("ALL PASS" if not failures else f"{len(failures)} FAILED", flush=True)
sys.exit(1 if failures else 0)
