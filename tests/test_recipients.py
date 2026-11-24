r"""No-loss P3: the signed recipients list (field_photos/recipients.py and the phone page's rlTake / rlFor).

Refused, in Python and on the phone (node runs the page's own code with the bridge core):
a rollback (older seq), a wrong signer, a list without the office key, a list for another key, more than 12 entries,
a changed list. Links without a pin trust the first signer (TOFU) and refuse any other.
Also: the office's own store (add / remove / seq / 12 at most) and what each kind is sealed for.

Run:  engine\bin\python-qgis.bat tests\test_recipients.py
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "app", "plugins"))
sys.path.insert(0, os.path.join(HERE, "..", "app", "python"))

from biruni.field_photos import bridge, recipients, server  # noqa: E402
from biruni.field_photos.handover import HandoverKey  # noqa: E402

failures = []


def check(cond, msg):
    print(("PASS " if cond else "FAIL ") + msg, flush=True)
    if not cond:
        failures.append(msg)


def refused(fn, *a, **kw):
    try:
        fn(*a, **kw)
    except recipients.RecipientsError as e:
        return str(e)
    return ""


def make_rl(sk, lst):
    l_text = recipients.canonical(lst)
    ip, sig = recipients.sign_with(sk, l_text)
    return {"l": l_text, "ip": ip, "is": sig}


work = tempfile.mkdtemp(prefix="biruni_p3_rl_")
try:
    folder = os.path.join(work, "cert")
    office = HandoverKey(folder)
    backup = HandoverKey(os.path.join(work, "backup"))
    sup = HandoverKey(os.path.join(work, "sup"))
    others = [HandoverKey(os.path.join(work, f"o{i}")) for i in range(12)]
    key = bridge.new_key()
    sk, pin = bridge.identity(key)
    room = key[:12]

    # ---- the office's store ----------------------------------------------------------------------------------------
    check(recipients.signed(folder, office.public, key) is None, "no list before the office makes one: phones seal v1")
    s1 = recipients.add(folder, backup.public, "backup", "branch", office_pub=office.public)["seq"]
    d = recipients.add(folder, sup.public, "sup", "Ali", office_pub=office.public)
    check(d["seq"] > s1 >= int(__import__("time").time() // 60) and [e["role"] for e in d["r"]] == ["backup", "sup"],
          "add: seq goes up with each change, never below the minutes since 1970")
    check(d["r"][1]["scope"] == ["pos"], "a supervisor gets positions only by default")
    check(refused(recipients.add, folder, office.public, "backup", office_pub=office.public) != "",
          "the office's own key cannot be added again")
    check(refused(recipients.add, folder, "not-a-key", "backup") == "not a public key", "a broken key is refused")
    rl = recipients.signed(folder, office.public, key)
    lst = recipients.verify(rl, office.public, room, pin=pin)
    check(lst["seq"] == d["seq"] and lst["r"][0]["role"] == "office" and lst["r"][0]["k"] == office.kid,
          "the signed list verifies against the pin; the office is first")
    check(recipients.for_kind(lst, "photo") == [office.public, backup.public]
          and recipients.for_kind(lst, "pos") == [office.public, backup.public, sup.public],
          "photos are sealed for office + backup; positions also for the supervisor")

    # ---- refusals (Python) -----------------------------------------------------------------------------------------
    check(refused(recipients.verify, rl, office.public, room, pin=pin, last_seq=d["seq"] + 1) == "rollback",
          "an older list than the one kept (rollback) is refused")
    other_sk, _ = bridge.identity(bridge.new_key())
    forged = make_rl(other_sk, dict(json.loads(rl["l"]), made=1))
    check(refused(recipients.verify, forged, office.public, room, pin=pin) == "wrong signer",
          "a list signed by another identity is refused")
    no_office = dict(json.loads(rl["l"]), r=json.loads(rl["l"])["r"][1:])
    check(refused(recipients.verify, make_rl(sk, no_office), office.public, room, pin=pin)
          == "the office key is missing", "a list without the office key is refused")
    check(refused(recipients.verify, rl, backup.public, room, pin=pin) == "the office key is missing",
          "a list whose office is not the key the phone seals for is refused")
    big = dict(json.loads(rl["l"]))
    big["r"] = big["r"] + [{"k": recipients.kid_hex(o.public), "p": o.public, "role": "backup",
                            "scope": ["photo"], "n": ""} for o in others[:10]]
    check(len(big["r"]) == 13 and refused(recipients.verify, make_rl(sk, big), office.public, room, pin=pin)
          == "at most 12 recipients", "a list of 13 is refused")
    changed = dict(rl, l=rl["l"].replace('"seq":%d' % d["seq"], '"seq":%d' % (d["seq"] + 9)))
    check(changed["l"] != rl["l"], "(the changed list differs)")
    check(refused(recipients.verify, changed, office.public, room, pin=pin) == "bad signature",
          "a changed list (seq raised without a signature) is refused")
    check(refused(recipients.verify, rl, office.public, "otherroom12", pin=pin) == "another room",
          "a list for another room is refused")
    for o in others[:9]:
        recipients.add(folder, o.public, "backup", office_pub=office.public)
    check(len(recipients.load(folder)["r"]) == 11 and
          refused(recipients.add, folder, others[9].public, "backup", office_pub=office.public)
          == "at most 12 recipients", "the office cannot add a 12th recipient beside itself")
    for o in others[:9]:
        recipients.remove(folder, o.public)
    rl3 = recipients.signed(folder, office.public, key)
    check(json.loads(rl3["l"])["seq"] > d["seq"] and len(json.loads(rl3["l"])["r"]) == 3, "remove: seq goes on rising")

    before = recipients.load(folder)["seq"]
    old_doc = dict(recipients.load(folder), seq=5)
    m = recipients.merge(folder, old_doc)
    check(m["seq"] > before and len(m["r"]) == 2, "merging an older backup list keeps the newer list and raises seq")
    rl3 = recipients.signed(folder, office.public, key)

    # ---- the phone (node, the page's code with the bridge core) ------------------------------------------------------
    p = server.PAGE
    page_js = p[p.index("const H64="):p.index("const sleepH=")] + \
        p[p.index("/* ---- no-loss P3"):p.index("async function sealItem(it)")]
    script = os.path.join(work, "rl.js")
    case = {"rl": rl, "rl3": rl3, "forged": forged, "noOffice": make_rl(sk, no_office), "big": make_rl(sk, big),
            "changed": changed, "office": office.public,
            "rlNewOffice": make_rl(sk, dict(json.loads(rl3["l"]), seq=json.loads(rl3["l"])["seq"] + 1,
                                            r=[{"k": recipients.kid_hex(backup.public), "p": backup.public,
                                                "role": "office", "scope": list(recipients.KINDS), "n": ""}])), "backup": backup.public, "sup": sup.public}
    with open(script, "w", encoding="utf-8") as f:
        f.write("globalThis.crypto = require('crypto').webcrypto;\n")
        f.write("const store={};globalThis.localStorage={getItem:k=>store[k]??null,setItem:(k,v)=>{store[k]=v},"
                "removeItem:k=>{delete store[k]}};\n")
        f.write("const PIN=process.argv[3]==='pin';\n")
        f.write("globalThis.location={hostname:'example.com',hash:'#k=" + key + "'+(PIN?'&i=" + pin + "':'')};\n")
        f.write("globalThis.WebSocket=class{close(){}};globalThis.fetch=async()=>({ok:false});\n")
        f.write(bridge._core() + "\n")
        f.write("function keep(k,v){if(v==null)delete store[k];else store[k]=v}function kept(k){return store[k]??null}\n")
        f.write("let ROOM='" + room + "',PCKEY='';\n")
        f.write(page_js + "\n")
        f.write(r"""
(async()=>{
 const c=JSON.parse(require('fs').readFileSync(process.argv[2],'utf8'));await bridgeKey('""" + key + r"""');
 const r={};PCKEY=c.backup;r.otherKey=await rlTake(c.rl);PCKEY=c.office;
 r.noOffice=await rlTake(c.noOffice);r.big=await rlTake(c.big);r.changed=await rlTake(c.changed);
 r.forgedFirst=await rlTake(c.forged);
 r.v1=rlFor('photo');
 r.ok=await rlTake(c.rl);r.photo=rlFor('photo');r.state=rlFor('state');r.pos=rlFor('pos');
 r.newer=await rlTake(c.rl3);r.rollback=await rlTake(c.rl);r.seq=(rlGet()||{lst:{}}).lst.seq;
 r.forgedAfter=await rlTake(c.forged);r.refused=SIG.refused;
 r.pkSame=await rlPk(c.office,null);r.pkMoveBare=await rlPk(c.sup,null);r.pkMoveForged=await rlPk(c.sup,c.forged);
 r.pkMoveSigned=await rlPk(c.backup,c.rlNewOffice);
 PCKEY=c.backup;r.afterNewQr=rlFor('photo');
 console.log(JSON.stringify(r));
})().catch(e=>{console.error(String(e&&e.stack||e));process.exit(2)});
""")
    cpath = os.path.join(work, "case.json")
    with open(cpath, "w", encoding="utf-8") as f:
        json.dump(case, f)
    node = shutil.which("node") or r"C:\Program Files\nodejs\node.exe"
    for mode in ("pin", "tofu"):
        proc = subprocess.run([node, script, cpath, mode], capture_output=True, text=True, timeout=30)
        check(proc.returncode == 0, f"[{mode}] node ran rlTake (rc={proc.returncode} {proc.stderr.strip()[:300]})")
        r = json.loads(proc.stdout.strip().splitlines()[-1]) if proc.returncode == 0 else {}
        check(r.get("otherKey") is False, f"[{mode}] a list whose office is not the key this phone seals for: refused")
        check(r.get("noOffice") is False and r.get("big") is False and r.get("changed") is False,
              f"[{mode}] no office key / 13 entries / a changed list: refused")
        if mode == "pin":
            check(r.get("forgedFirst") is False, "[pin] a list signed by another identity is refused")
            check(r.get("ok") is True, "[pin] the PC's list is taken")
        else:
            check(r.get("forgedFirst") is False and r.get("ok") is False and r.get("photo") is None,
                  "[no pin] links without a pin take no list at all (they keep sealing v1)")
            continue
        check(r.get("v1") is None, "[pin] before a list: v1 (rlFor is null)")
        check(r.get("photo") == [office.public, backup.public] and r.get("state") == [office.public, backup.public],
              "[pin] photos and checklist answers are sealed for office + backup")
        check(r.get("pos") == [office.public, backup.public, sup.public], "[pin] positions also for the supervisor")
        check(r.get("newer") is True and r.get("rollback") is False and r.get("seq") == json.loads(rl3["l"])["seq"],
              "[pin] a newer list replaces it; going back to the older one is refused (rollback)")
        check(r.get("forgedAfter") is False and (r.get("refused") or 0) >= 5, "[pin] refusals are counted")
        check(r.get("pkSame") is True and r.get("pkMoveBare") is False and r.get("pkMoveForged") is False,
              "[pin] with a list kept, an unsigned hello pk cannot move the office key (downgrade refused)")
        check(r.get("pkMoveSigned") is True, "[pin] a signed list naming the new office key may move it")
        check(r.get("afterNewQr") is None, "[pin] a new QR (another office key): the old list is not used, v1")
finally:
    shutil.rmtree(work, ignore_errors=True)

print("ALL PASS" if not failures else f"{len(failures)} FAILED", flush=True)
sys.exit(1 if failures else 0)
