r"""Capture schema phone checklist tests (Phase C): QWebEngine offscreen tests on both LAN page and bridge.phone_page.

Covers:
  - test_no_cap_no_ui
  - test_checklist_renders_ar_rtl
  - test_add_instances_respects_min_max
  - test_shot_binds_path
  - test_required_blocks_finish_until_reason
  - test_state_item_queued_and_replaced
  - test_reload_restores_state
  - test_vectors_js_equals_python
  - test_extra_photo_star
  - test_old_form_hidden_in_job_mode

Run:  engine\bin\python-qgis.bat tests\test_capture_phone.py
"""
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "app", "plugins"))
sys.path.insert(0, os.path.join(HERE, "..", "app", "python"))


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


RELAY_PORT = free_port()
os.environ["BIRUNI_BRIDGE_RELAYS"] = f"ws://127.0.0.1:{RELAY_PORT}"
os.environ["BIRUNI_BRIDGE_NTFY"] = "https://127.0.0.1:9/"
os.environ["QT_QPA_PLATFORM"] = "offscreen"
os.environ["QTWEBENGINE_DISABLE_SANDBOX"] = "1"
os.environ["QTWEBENGINE_CHROMIUM_FLAGS"] = "--disable-gpu --no-sandbox"

from PyQt6.QtCore import QCoreApplication, Qt, QUrl  # noqa: E402

QCoreApplication.setAttribute(Qt.ApplicationAttribute.AA_ShareOpenGLContexts)
from PyQt6.QtGui import QGuiApplication  # noqa: E402
from PyQt6.QtWebEngineCore import QWebEnginePage, QWebEngineProfile  # noqa: E402

app = QGuiApplication(sys.argv[:1])  # Chromium needs a program name
profile = QWebEngineProfile.defaultProfile()

from biruni.field_photos import bridge  # noqa: E402
from biruni.field_photos import capture as cap  # noqa: E402
from biruni.field_photos.server import all_phone_texts, render_page  # noqa: E402

failures = []


def check(cond, msg):
    print(("PASS " if cond else "FAIL ") + msg, flush=True)
    if not cond:
        failures.append(msg)


def wait(cond, seconds):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        app.processEvents()
        if cond():
            return True
        time.sleep(0.02)
    return False


work = tempfile.mkdtemp()
ALL_TEXTS = all_phone_texts()

# ---- Static & syntax checks -------------------------------------------------------------------------------
lan_html = render_page(ALL_TEXTS, "ar", ["عمود"], [{"label": "Pole ID", "type": "text", "req": True}], "pckey").decode("utf-8")
bridge_raw = bridge.phone_page(ALL_TEXTS)
bridge_html = bridge.with_csp(bridge_raw)

check(lan_html.count("<script>") == 1 and lan_html.count("<style>") == 1, "LAN page keeps one script and one style")
check(bridge_html.count("<script>") == 1 and bridge_html.count("<style>") == 1, "Bridge CSP page keeps one script and one style")

node = shutil.which("node") or next((p for p in (r"C:\Program Files\nodejs\node.exe",) if os.path.isfile(p)), None)
if node:
    for label, h in (("LAN", lan_html), ("Bridge", bridge_html)):
        jsp = os.path.join(work, f"{label}.js")
        with open(jsp, "w", encoding="utf-8") as f:
            f.write(h.split("<script>")[1].split("</script>")[0])
        r = subprocess.run([node, "--check", jsp], capture_output=True, text=True)
        check(r.returncode == 0, f"{label} script has no syntax error " + r.stderr[:200])

CSP_ERRORS = []


class TestPhonePage(QWebEnginePage):
    def javaScriptConsoleMessage(self, level, message, line, source):  # noqa: N802
        print("phone js:", message, line, flush=True)
        if "Content Security Policy" in message:
            CSP_ERRORS.append(message)


key = bridge.new_key()
pin = bridge.pin(key)
phone = TestPhonePage(profile)
phone_url = QUrl(f"https://phone.invalid/p/#k={key}&i={pin}&l=ar")
phone.setHtml(bridge_html, phone_url)

res = {}


def js(expr, page=phone):
    res.pop("v", None)
    page.runJavaScript(expr, lambda v: res.update(v=v))
    wait(lambda: "v" in res, 10)
    return res.get("v")


check(wait(lambda: js("document.documentElement&&document.documentElement.dir") == "rtl", 20),
      "phone page loaded in Arabic RTL under strict CSP")


# ---- 1. test_no_cap_no_ui ---------------------------------------------------------------------------------
def test_no_cap_no_ui():
    js("setGuide({v:1,items:[]});1")
    check(js("$('capCard').classList.contains('hide')") is True,
          "no_cap_no_ui: #capCard is hidden when guide has no cap")
    check(js("$('formCard').classList.contains('hide')") is False,
          "no_cap_no_ui: #formCard is visible when no job is active")
    check(js("$('capSheet').classList.contains('hide')") is True,
          "no_cap_no_ui: #capSheet is hidden")

    # Also check LAN page in its own QWebEnginePage
    lan_page = TestPhonePage(profile)
    lan_page.setHtml(lan_html, QUrl("https://127.0.0.1:10112/tok/"))
    check(wait(lambda: js("typeof setGuide==='function'", lan_page) is True, 15),
          "no_cap_no_ui: LAN page loaded")
    check(js("$('capCard').classList.contains('hide')", lan_page) is True
          and js("$('formCard').classList.contains('hide')", lan_page) is False,
          "no_cap_no_ui: LAN page hides #capCard and shows #formCard when no jobs exist")
    lan_page.deleteLater()


# ---- 2. test_vectors_js_equals_python ---------------------------------------------------------------------
def test_vectors_js_equals_python():
    vec_path = os.path.join(HERE, "data", "capture_vectors.json")
    data = json.load(open(vec_path, encoding="utf-8"))
    schemas = {k: cap.clean_schema(v) for k, v in data["schemas"].items()}
    for v in data["vectors"]:
        s = schemas[v["schema"]]
        st = cap.clean_state(v["state"], s)
        recs = v.get("records") or []
        py_miss = cap.missing(s, st, recs)
        py_keys = [m["k"] for m in py_miss]
        py_complete = len(py_miss) == 0

        payload = json.dumps({"s": s, "st": st, "recs": recs}, ensure_ascii=False)
        js_out = js(f"(()=>{{const d={payload};const m=capMissing(d.s,d.st,d.recs);"
                    f"return {{keys:m.map(x=>x.k),complete:m.length===0,miss:m}}}})()")
        check(isinstance(js_out, dict) and js_out.get("keys") == py_keys and js_out.get("complete") == py_complete,
              f"vectors_js_equals_python [{v['name']}]: JS={js_out.get('keys') if isinstance(js_out, dict) else js_out} == PY={py_keys}")


# ---- Build a test capture payload (Arabic RTL schema + repeat min/max) ------------------------------------
TEST_SCHEMA = cap.clean_schema({
    "biruni_capture": 1,
    "id": "bldg_test",
    "rev": 1,
    "title": "مسح مبنى تجريبي",
    "job": [{"name": "job_no", "label": "رقم المعاملة", "type": "text", "req": True, "title": True}],
    "nodes": [
        {
            "id": "facades",
            "label": "الواجهات",
            "req": True,
            "nodes": [
                {"id": "front", "label": "الواجهة الأمامية", "photos": [1, 3], "req": True},
            ],
        },
        {"id": "roof", "label": "السطح", "photos": [1, 2], "req": False},
        {
            "id": "floors",
            "label": "طابق",
            "req": True,
            "repeat": {"min": 1, "max": 2},
            "nodes": [
                {
                    "id": "rooms",
                    "label": "غرفة",
                    "req": True,
                    "repeat": {"min": 1, "max": 2},
                    "photos": [1, 4],
                    "q": [
                        {"name": "room_type", "label": "نوع الغرفة", "type": "choice", "req": True, "title": True,
                         "choices": ["مطبخ", "غرفة نوم"]},
                    ],
                },
            ],
        },
    ],
})
JOB1 = cap.new_job(TEST_SCHEMA, data={"job_no": "معاملة-101"}, loc=[46.6753, 24.7136], job_id="JTEST101")
CAP_PAYLOAD = cap.cap_payload([JOB1])


# ---- 3. test_checklist_renders_ar_rtl & 10. test_old_form_hidden_in_job_mode ------------------------------
def test_checklist_renders_ar_rtl_and_old_form_hidden():
    js("FORM=[{name:'pole_no',label:'Pole number',type:'text',req:true}];drawForm();1")
    check(js("$('formCard').classList.contains('hide')") is False,
          "before job: #formCard is visible")

    g_json = json.dumps({"v": 2, "items": [], "cap": CAP_PAYLOAD}, ensure_ascii=False)
    js(f"setGuide({g_json});1")

    check(js("$('capCard').classList.contains('hide')") is False,
          "checklist_renders_ar_rtl: #capCard is visible when open job arrives")
    check(js("document.documentElement.dir") == "rtl" and js("$('lCap').textContent") == "معاملة التصوير",
          f"checklist_renders_ar_rtl: Arabic header rendered ({js('$(\"lCap\").textContent')})")
    check(js("$('capJob').value") == "JTEST101" and "معاملة-101" in (js("$('capJob').selectedOptions[0].textContent") or ""),
          "checklist_renders_ar_rtl: job picker selects the active job")
    prog_txt = js("$('capProg').textContent") or ""
    check("من" in prog_txt and "0" in prog_txt,
          f"checklist_renders_ar_rtl: progress bar shows Arabic '{prog_txt}'")
    tree_txt = js("$('capTree').textContent") or ""
    check("الواجهات" in tree_txt and "الواجهة الأمامية" in tree_txt and "طابق 1" in tree_txt and "غرفة 1" in tree_txt
          and "+ إضافة طابق" in tree_txt,
          f"checklist_renders_ar_rtl: tree shows Arabic nodes and min instances ({tree_txt[:120]})")
    check(js("kept('biruni-cap-'+ROOM)!==null") is True,
          "cached cap: biruni-cap-<ROOM> saved in localStorage")

    # 10. old_form_hidden_in_job_mode
    check(js("$('formCard').classList.contains('hide')") is True,
          "old_form_hidden_in_job_mode: #formCard is hidden while a job is active")
    js("capSelectJob('');1")
    check(js("$('formCard').classList.contains('hide')") is False,
          "old_form_hidden_in_job_mode: #formCard reappears when switching picker to 'No job'")
    js("capSelectJob('JTEST101');1")
    check(js("$('formCard').classList.contains('hide')") is True,
          "old_form_hidden_in_job_mode: #formCard hidden again when job re-selected")


# ---- 4. test_add_instances_respects_min_max ---------------------------------------------------------------
def test_add_instances_respects_min_max():
    # Initially floors has 1 live instance (repeat.min = 1, repeat.max = 2)
    f_count0 = js("Object.values(CAPST.inst).filter(x=>x.n==='floors'&&!x.del).length")
    check(f_count0 == 1, f"add_instances_respects_min_max: starts at min=1 floor ({f_count0})")

    # Trying to remove when at min=1 must be refused
    f_iid0 = js("Object.keys(CAPST.inst).find(k=>CAPST.inst[k].n==='floors'&&!CAPST.inst[k].del)")
    removed_at_min = js(f"capRemove(capSchema().nodes[2], {json.dumps(f_iid0)})")
    check(removed_at_min is False
          and js("Object.values(CAPST.inst).filter(x=>x.n==='floors'&&!x.del).length") == 1,
          "add_instances_respects_min_max: cannot remove instance below repeat.min")

    # Add a 2nd floor (up to max=2)
    f_iid2 = js("capAdd(capSchema().nodes[2], '')")
    check(bool(f_iid2) and js("Object.values(CAPST.inst).filter(x=>x.n==='floors'&&!x.del).length") == 2,
          f"add_instances_respects_min_max: added 2nd floor ({f_iid2})")

    # Adding a 3rd floor when max=2 must be refused
    f_iid3 = js("capAdd(capSchema().nodes[2], '')")
    check(f_iid3 is None and js("Object.values(CAPST.inst).filter(x=>x.n==='floors'&&!x.del).length") == 2,
          "add_instances_respects_min_max: cannot add instance above repeat.max")

    # Soft-remove the 2nd floor: sets del > 0 and cascades to its child room
    removed_ok = js(f"capRemove(capSchema().nodes[2], {json.dumps(f_iid2)})")
    del_val = js(f"CAPST.inst[{json.dumps(f_iid2)}].del")
    check(removed_ok is True and isinstance(del_val, (int, float)) and del_val > 0
          and js("Object.values(CAPST.inst).filter(x=>x.n==='floors'&&!x.del).length") == 1,
          f"add_instances_respects_min_max: removal is a soft del timestamp ({del_val}) down to min")


# ---- 5. test_shot_binds_path (and indoor fallback fix) ----------------------------------------------------
def test_shot_binds_path():
    # Ensure no GPS fix is set: indoor fallback must use job.loc with acc 999
    js("fix=null;indoorFix=null;room=null;$('me').value='حاتم';keep('biruni-me','حاتم');1")
    fallback = js("shotFix()")
    check(isinstance(fallback, dict) and abs(fallback.get("lat", 0) - 24.7136) < 1e-6
          and abs(fallback.get("lon", 0) - 46.6753) < 1e-6 and fallback.get("acc") == 999,
          f"indoor fallback fix uses job.loc with acc=999 when no GPS fix exists ({fallback})")

    # Open the room item in floor 1
    r_iid = js("Object.keys(CAPST.inst).find(k=>CAPST.inst[k].n==='rooms'&&!CAPST.inst[k].del)")
    f_iid = js("Object.keys(CAPST.inst).find(k=>CAPST.inst[k].n==='floors'&&!CAPST.inst[k].del)")
    slot_k = f"{r_iid}:rooms"
    js(f"capOpen({json.dumps(slot_k)});1")
    check(js("$('capSheet').classList.contains('hide')") is False,
          "shot_binds_path: tapping item opens #capSheet")
    check("0/1" in (js("$('capShoot').textContent") or ""),
          f"shot_binds_path: shoot button shows photo progress ({js('$(\"capShoot\").textContent')})")

    # Attempting to shoot before answering required room_type question must be refused
    js("window._fileClicked=0;$('file').onclick=()=>{window._fileClicked++};capShot();1")
    check(js("window._fileClicked") == 0 and js("document.querySelectorAll('#capForm .fld.bad').length") == 1,
          "shot_binds_path: required node question blocks shutter and marks field .bad")

    # Select 'مطبخ' (Kitchen), which is also a title question -> updates path title to 'طابق 1 › مطبخ'
    js("document.querySelector('#capForm .kinds button').click();1")
    check(js("$('capPath').textContent") == "طابق 1 › مطبخ",
          f"shot_binds_path: title question answer updates path title ({js('$(\"capPath\").textContent')})")
    js("$('capNote').value='ملاحظة المطبخ';$('capNote').oninput();1")

    # Trigger capShot() and simulate camera file input change
    js(r"""(async()=>{
     window._lastCapMeta=null;
     const origPut=put;
     window.put=async(it)=>{if(!it.cap)window._lastCapMeta=JSON.parse(JSON.stringify(it.meta));return await origPut(it)};
     capShot();
     const b=new Uint8Array(1200);b.set([0xff,0xd8,0xff,0xe0]);b.set([0xff,0xd9],b.length-2);
     const dt=new DataTransfer();dt.items.add(new File([b],'room.jpg',{type:'image/jpeg'}));
     Object.defineProperty($('file'),'files',{value:dt.files,configurable:true});
     await $('file').onchange();
     window.put=origPut;
    })();1""")
    check(wait(lambda: js("!!window._lastCapMeta") is True, 10),
          "shot_binds_path: photo taken and queued")
    m = js("window._lastCapMeta") or {}
    cap_bind = m.get("cap") or {}
    check(cap_bind.get("j") == "JTEST101"
          and cap_bind.get("k") == slot_k
          and cap_bind.get("pl") == "طابق 1 › مطبخ"
          and cap_bind.get("ch") == [["floors", f_iid, 1], ["rooms", r_iid, 1]],
          f"shot_binds_path: photo meta carries exact cap {{j,k,pl,ch}} ({cap_bind})")
    check(m.get("note") == "ملاحظة المطبخ" and m.get("acc") == 999,
          "shot_binds_path: photo meta carries item note and fallback position")
    check("1/1" in (js("$('capShoot').textContent") or ""),
          f"shot_binds_path: shoot button updates to 1/1 ({js('$(\"capShoot\").textContent')})")
    js("capCloseSheet();1")


# ---- 6. test_extra_photo_star -----------------------------------------------------------------------------
def test_extra_photo_star():
    # With job active and no item sheet open, free shutter #shot binds cap.k = "*"
    # Also test indoor fallback uses last job photo's position if we move CAPLASTFIX
    js("CAPLASTFIX={j:'JTEST101',lat:24.7199,lon:46.6799,acc:12};1")
    js(r"""(async()=>{
     window._extraMeta=null;
     const origPut=put;
     window.put=async(it)=>{if(!it.cap)window._extraMeta=JSON.parse(JSON.stringify(it.meta));return await origPut(it)};
     $('shot').click();
     const b=new Uint8Array(1000);b.set([0xff,0xd8,0xff,0xe0]);b.set([0xff,0xd9],b.length-2);
     const dt=new DataTransfer();dt.items.add(new File([b],'extra.jpg',{type:'image/jpeg'}));
     Object.defineProperty($('file'),'files',{value:dt.files,configurable:true});
     await $('file').onchange();
     window.put=origPut;
    })();1""")
    check(wait(lambda: js("!!window._extraMeta") is True, 10),
          "extra_photo_star: free shutter photo queued")
    em = js("window._extraMeta") or {}
    ecap = em.get("cap") or {}
    check(ecap.get("j") == "JTEST101" and ecap.get("k") == "*" and ecap.get("pl") == "*" and ecap.get("ch") == [],
          f"extra_photo_star: free shutter photo gets cap.k='*' ({ecap})")
    check(abs(em.get("lat", 0) - 24.7199) < 1e-6 and em.get("acc") == 12,
          "extra_photo_star: indoor fallback used last job photo's position")


# ---- 7. test_required_blocks_finish_until_reason ----------------------------------------------------------
def test_required_blocks_finish_until_reason():
    # Currently ':front' (Front facade) is still missing 1 photo
    fin1 = js("capFinish()")
    miss_hidden = js("$('capMiss').classList.contains('hide')")
    miss_txt = js("$('capMiss').textContent") or ""
    check(fin1 is False and miss_hidden is False and "الواجهة الأمامية" in miss_txt,
          f"required_blocks_finish_until_reason: finish refused and lists missing ':front' ({miss_txt})")

    # Open ':front' and try a skip reason shorter than 3 characters ('لا' -> 2 chars)
    js("capOpen(':front');$('capReason').value='لا';capSkip();1")
    check(js("CAPST.skip[':front']") is None and js("$('capSheet').classList.contains('hide')") is False,
          "required_blocks_finish_until_reason: reason < 3 characters is refused")

    # Provide a valid skip reason >= 3 characters
    js("$('capReason').value='أعمال صيانة أمام الواجهة';capSkip();1")
    check(js("CAPST.skip[':front']&&CAPST.skip[':front'][0]") == "أعمال صيانة أمام الواجهة",
          "required_blocks_finish_until_reason: reason >= 3 characters saved in state.skip")
    check("⏭" in (js("document.querySelector('[data-k=\":front\"]').textContent") or ""),
          "required_blocks_finish_until_reason: skipped item shows ⏭ in tree")

    # Now every required item is either done (room) or skipped (:front); optional (:roof) is not required
    fin2 = js("capFinish()")
    check(fin2 is True and js("!!CAPST.closed") is True and js("$('capMiss').classList.contains('hide')") is True,
          "required_blocks_finish_until_reason: finish succeeds once every missing required item has a reason")


# ---- 8. test_state_item_queued_and_replaced ---------------------------------------------------------------
def test_state_item_queued_and_replaced():
    js(r"""(async()=>{
     window._stTest=null;
     clearTimeout(CAPTM);
     /* Prevent background flush from sending while we inspect the queue */
     const origSend=window.sendItem;
     window.sendItem=async()=>0;
     for(const x of await all())if(x.cap)await del(x.id);
     const photosBefore=(await all()).filter(x=>!x.cap).length;
     const thumbsBefore=$('list').querySelectorAll('[data-id]').length;

     CAPST.rev=10;capSaveLocal();
     const it1=await capSnap();
     const q1=(await all()).filter(x=>x.cap);

     /* Capture unsealed meta & blob of a state item */
     let rawMeta=null,rawText='';
     const origSeal=sealRecord;
     window.sealRecord=async(it)=>{
      if(it&&it.cap&&!rawMeta){rawMeta=JSON.parse(JSON.stringify(it.meta));rawText=await it.blob.text()}
      return await origSeal(it)};
     CAPST.rev=11;capSaveLocal();
     const it2=await capSnap();
     window.sealRecord=origSeal;

     const q2=(await all()).filter(x=>x.cap);
     await count();
     const nWaitShown=+$('nWait').textContent;
     const thumbsAfter=$('list').querySelectorAll('[data-id]').length;

     /* Test flush() treats 202 on it.cap as delivered and deletes it */
     window.sendItem=async(it)=>it.cap?202:0;
     busy=false;
     await flush();
     window.sendItem=origSend;
     const qAfter202=(await all()).filter(x=>x.cap);

     window._stTest={
      id1:it1.id,id2:it2.id,
      q1Ids:q1.map(x=>x.id),q2Ids:q2.map(x=>x.id),
      rawMeta:rawMeta,startsWithBCS1:rawText.startsWith('BCS1'),
      parsedStateRev:rawText.startsWith('BCS1')?JSON.parse(rawText.slice(4)).rev:null,
      photosBefore:photosBefore,nWaitShown:nWaitShown,
      thumbsBefore:thumbsBefore,thumbsAfter:thumbsAfter,
      qAfter202Len:qAfter202.length
     };
    })();1""")
    check(wait(lambda: js("!!window._stTest") is True, 15),
          "state_item_queued_and_replaced: async state item checks completed")
    st = js("window._stTest") or {}
    check(str(st.get("id1", "")).startswith("jsa-") and str(st.get("id2", "")).startswith("jsb-")
          and len(str(st.get("id2", "")).split("-", 1)[1]) == 9,
          f"state_item_queued_and_replaced: id is 'js'+rev36+'-'+rand9 ({st.get('id1')}, {st.get('id2')})")
    check(st.get("q1Ids") == [st.get("id1")] and st.get("q2Ids") == [st.get("id2")],
          f"state_item_queued_and_replaced: newer snapshot deletes older waiting state items of same job ({st.get('q1Ids')} -> {st.get('q2Ids')})")
    rm = st.get("rawMeta") or {}
    check(rm.get("cs") == {"j": "JTEST101", "rev": 11} and st.get("startsWithBCS1") is True and st.get("parsedStateRev") == 11,
          f"state_item_queued_and_replaced: meta carries cs:{{j,rev}} and blob is 'BCS1'+JSON ({rm.get('cs')})")
    check(st.get("nWaitShown") == st.get("photosBefore") and st.get("thumbsAfter") == st.get("thumbsBefore"),
          f"state_item_queued_and_replaced: count() and thumbnails skip it.cap items (wait={st.get('nWaitShown')}, photos={st.get('photosBefore')})")
    check(st.get("qAfter202Len") == 0,
          "state_item_queued_and_replaced: flush treats 202 on it.cap as delivered and deletes it")

    # Test drive_page.py dfits excludes state items while lineOpen()
    from biruni.field_photos.handover import HandoverKey
    hk = HandoverKey(os.path.join(work, "hk"))
    drv_page = TestPhonePage(profile)
    drv_page.setHtml(bridge_html, QUrl(f"https://phone.invalid/p/#k={key}&i={pin}&l=en&d=1&pk={hk.public}"))
    check(wait(lambda: js("typeof dfits==='function'&&DRIVE_ON===true", drv_page) is True, 15),
          "state_item_queued_and_replaced: drive page loaded with DRIVE_ON=true")
    dfits_res = js(r"""(()=>{
     const it={id:'js1-abc123456',cap:1,meta:{id:'js1-abc123456',sealed:1,t:Date.now()},blob:new Blob(['x'])};
     const origV=VIA;
     VIA={ready:true};const openRes=dfits(it);
     VIA=null;const closedRes=dfits(it);
     VIA=origV;
     return {openRes:openRes,closedRes:closedRes};
    })()""", drv_page) or {}
    check(dfits_res.get("openRes") is False and dfits_res.get("closedRes") is True,
          f"state_item_queued_and_replaced: dfits excludes state items while lineOpen() ({dfits_res})")
    drv_page.deleteLater()


# ---- 9. test_reload_restores_state ------------------------------------------------------------------------
def test_reload_restores_state():
    saved_rev = js("CAPST.rev")
    saved_skip = js("CAPST.skip[':front'][0]")
    # Simulate a fresh page reload on the same origin/localStorage without receiving guide again yet
    js("CAP=null;CAPJOB='';CAPST=null;CAPSEL=null;CAPSHEET=null;"
       "try{const c=JSON.parse(kept('biruni-cap-'+ROOM)||'null');if(c)capSet(c)}catch(e){};1")
    check(js("CAPJOB") == "JTEST101" and js("CAPST&&CAPST.rev") == saved_rev
          and js("CAPST&&CAPST.skip[':front']&&CAPST.skip[':front'][0]") == saved_skip
          and js("!!(CAPST&&CAPST.closed)") is True,
          f"reload_restores_state: job and state restored from localStorage (rev={js('CAPST&&CAPST.rev')}, skip={js('CAPST&&CAPST.skip[\":front\"][0]')})")
    check("طابق 1" in (js("$('capTree').textContent") or "") and "مطبخ" in (js("$('capTree').textContent") or ""),
          "reload_restores_state: checklist tree and answered instance title restored after reload")


test_no_cap_no_ui()
test_vectors_js_equals_python()
test_checklist_renders_ar_rtl_and_old_form_hidden()
test_add_instances_respects_min_max()
test_shot_binds_path()
test_extra_photo_star()
test_required_blocks_finish_until_reason()
test_state_item_queued_and_replaced()
test_reload_restores_state()

check(not CSP_ERRORS, "zero Content-Security-Policy violations during all phone checklist tests: " + " | ".join(CSP_ERRORS))

phone.deleteLater()
shutil.rmtree(work, ignore_errors=True)
print("ALL PASS" if not failures else f"{len(failures)} FAILED", flush=True)
sys.exit(1 if failures else 0)
