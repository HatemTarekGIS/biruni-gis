"""Field photos through the surveyor's own Google Drive: the phone's side.

Two pages:
  * the link page (HELPER_PAGE), served alone at https://drive.biruni-gis.com/link/ (repository biruni-drive).
    Google's sign-in script runs only there. The phone page (/p/) frames it - or opens it as a window on iPhone and
    when the frame is refused - and receives the access token by postMessage. The photo page, which holds the office
    key in its memory, never loads a Google script.
  * DRIVE_JS (step 7c/7d), added to the phone page by bridge.phone_page.

Messages (all with v: 1; each side posts only to the other's exact origin and checks e.origin and e.source):
  link page -> /p/   {type: "biruni-drive-ready", mode}           as soon as it opens (then /p/ says hello)
  /p/ -> link page   {type: "biruni-drive-hello", nonce, label, dir}
  link page -> /p/   {type: "biruni-drive-token", nonce, token, exp (ms), cached}
  link page -> /p/   {type: "biruni-drive-error", nonce, code}    gsi | popup_blocked | closed | denied | scope | other
The link page stays silent until a hello from /p/ arrives (it carries the nonce every answer must repeat), and keeps
the token only in its own sessionStorage, for at most its hour, so reloading /p/ needs no new tap.

Tests point API, HELPER, PARENT and GSI at a fake on this PC with BIRUNI_DRIVE_* (never in a published build:
scripts/build_bridge_page.py refuses to run with them set).
"""
import base64
import hashlib
import os

CLIENT_ID = "890626226614-mcaetshbj40i1ffpfrg9l42d50dhl1c4.apps.googleusercontent.com"
SCOPE = "https://www.googleapis.com/auth/drive.file"
API = os.environ.get("BIRUNI_DRIVE_API") or "https://www.googleapis.com"
HELPER = os.environ.get("BIRUNI_DRIVE_HELPER") or "https://drive.biruni-gis.com"
PARENT = os.environ.get("BIRUNI_DRIVE_PARENT") or "https://biruni-gis.com"
GSI = os.environ.get("BIRUNI_DRIVE_GSI") or "https://accounts.google.com/gsi/client"
HELPER_PATH = "/link/"
TEST_VARS = ("BIRUNI_DRIVE_API", "BIRUNI_DRIVE_HELPER", "BIRUNI_DRIVE_PARENT", "BIRUNI_DRIVE_GSI")

_HELPER_HTML = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="referrer" content="no-referrer"><meta name="robots" content="noindex">
<title>Biruni GIS · Google Drive</title>
<style>
html,body{margin:0;background:#fff;color:#1f2933;font:15px/1.45 system-ui,"Segoe UI",Roboto,Arial,sans-serif}
.w{padding:8px}
button{display:block;width:100%;min-height:44px;padding:10px 12px;border:1px solid #0f766e;border-radius:6px;
 background:#0f766e;color:#fff;font:inherit;font-weight:600;cursor:pointer}
button:disabled{background:#e5e7eb;border-color:#d1d5db;color:#6b7280;cursor:default}
.d{color:#52606d;font-size:13px;margin-top:6px;overflow-wrap:anywhere}
.alone{max-width:520px;margin:24px auto;padding:0 16px}.alone h1{font-size:18px}
</style></head><body>
<div class="w" id="ui" hidden><button id="go" type="button" disabled>Google Drive</button><div class="d" id="who"></div></div>
<div class="alone" id="alone" hidden><h1>Biruni GIS</h1>
<p>This page links a phone's Google Drive to Biruni's field photos page. Open the photos page from the office's
QR code instead.</p><p dir="rtl">هذه الصفحة تربط جوجل درايف بصفحة صور الموقع في بيروني. افتح صفحة الصور من رمز QR الخاص بالمكتب.</p></div>
<script>
const CID='__CID__',SCOPE='__SCOPE__',API='__API__',PARENT='__PARENT__',GSI='__GSI__';
const $=i=>document.getElementById(i);
const TO=window.opener||(window.parent!==window?window.parent:null);
const MODE=window.opener?'popup':(TO?'iframe':'alone');
let nonce='',tc=null,gsiOk=false,gsiErr=false,pending=false;
function post(m){if(!TO)return;m.v=1;try{TO.postMessage(m,PARENT)}catch(e){}}
function say(m){if(nonce){m.nonce=nonce;post(m)}}
function keepTok(t,exp){try{sessionStorage.setItem('biruni-drive-tok',JSON.stringify({t:t,exp:exp}))}catch(e){}}
function cachedTok(){try{const c=JSON.parse(sessionStorage.getItem('biruni-drive-tok')||'null');
 if(c&&typeof c.t==='string'&&+c.exp-Date.now()>300000)return c}catch(e){}return null}
function hint(){try{return localStorage.getItem('biruni-drive-hint')||''}catch(e){return ''}}
function showWho(){const h=hint();$('who').textContent=h}
function ready(){$('go').disabled=!(gsiOk&&nonce)||pending}
function fail(code){pending=false;ready();say({type:'biruni-drive-error',code:code})}
async function whoAmI(tok){try{const r=await fetch(API+'/drive/v3/about?fields=user(emailAddress)',
  {headers:{Authorization:'Bearer '+tok}});const j=await r.json();const e=j&&j.user&&j.user.emailAddress;
 if(e){try{localStorage.setItem('biruni-drive-hint',String(e).slice(0,120))}catch(x){}showWho()}}catch(e){}}
function onToken(r){pending=false;ready();
 if(!r||r.error){say({type:'biruni-drive-error',code:(r&&r.error==='access_denied')?'denied':'other'});return}
 if(!google.accounts.oauth2.hasGrantedAllScopes(r,SCOPE)){say({type:'biruni-drive-error',code:'scope'});return}
 const exp=Date.now()+(+r.expires_in||3600)*1000;keepTok(r.access_token,exp);
 say({type:'biruni-drive-token',token:r.access_token,exp:exp,cached:false});
 whoAmI(r.access_token).then(()=>{if(MODE==='popup')setTimeout(()=>window.close(),400)})}
function onError(e){const t=e&&e.type;fail(t==='popup_failed_to_open'?'popup_blocked':t==='popup_closed'?'closed':'other')}
$('go').onclick=()=>{if(!tc||!nonce||pending)return;pending=true;ready();const h=hint();
 const o={prompt:h?'':'select_account'};if(h)o.login_hint=h;try{tc.requestAccessToken(o)}catch(e){fail('other')}};
window.addEventListener('message',e=>{
 if(!TO||e.origin!==PARENT||e.source!==TO)return;const m=e.data;
 if(!m||m.v!==1||m.type!=='biruni-drive-hello'||typeof m.nonce!=='string'||!/^[\w-]{8,64}$/.test(m.nonce))return;
 const fresh=nonce!==m.nonce;nonce=m.nonce;
 $('go').textContent=String(m.label||'Google Drive').slice(0,80);
 document.documentElement.dir=m.dir==='rtl'?'rtl':'ltr';ready();
 if(fresh){if(gsiErr)say({type:'biruni-drive-error',code:'gsi'});
  const c=cachedTok();if(c)say({type:'biruni-drive-token',token:c.t,exp:+c.exp,cached:true})}});
if(MODE==='alone'){$('alone').hidden=false}
else{$('ui').hidden=false;showWho();post({type:'biruni-drive-ready',mode:MODE});
 const s=document.createElement('script');s.src=GSI;s.async=true;
 s.onload=()=>{try{tc=google.accounts.oauth2.initTokenClient({client_id:CID,scope:SCOPE,callback:onToken,
   error_callback:onError});gsiOk=true;ready()}catch(e){gsiErr=true;
   say({type:'biruni-drive-error',code:'gsi'})}};
 s.onerror=()=>{gsiErr=true;say({type:'biruni-drive-error',code:'gsi'})};document.head.appendChild(s)}
</script></body></html>"""


# ---- the phone page's side (bridge.phone_page puts it before PHONE_JS; it needs BOOT_JS's DRIVE_ON) ---------------
DRIVE_JS = r"""
/* ---- Google Drive: the card, the link (a frame or a window of the link page), the token in memory only ---- */
const DRV={on:false,tok:'',exp:0,state:'',nonce:'',frame:null,win:null,card:null,ready:false,n:0,
 HELPER:'__DRIVE_HELPER__',API:'__DRIVE_API__',HOLD:new Map(),
 ok(){return !!this.tok&&Date.now()<this.exp-300000},
 holds(id){return this.HOLD.get(id)==='drive'}};
DRV.ORIGIN=(()=>{try{return new URL(DRV.HELPER).origin}catch(e){return ''}})();
function driveUA(){const u=navigator.userAgent||'';
 return {inapp:/; wv\)|FBAN|FBAV|Instagram|Line\//.test(u),
  ios:/iPad|iPhone|iPod/.test(u)||(navigator.platform==='MacIntel'&&navigator.maxTouchPoints>1)}}
function driveEl(tag,cls,txt){const e=document.createElement(tag);if(cls)e.className=cls;if(txt!=null)e.textContent=txt;return e}
function driveSay(state){DRV.state=state;const c=DRV.card;if(!c)return;
 const S={unlinked:T.driveUnlinked,auth:T.driveAuth,idle:T.driveIdle,up:T.driveUp,wait:T.driveWait,full:T.driveFull,
  err:T.driveErr,denied:T.driveDenied,inapp:T.driveInApp,blocked:T.drivePopupBlocked}[state]||'';
 c.querySelector('.dstate').textContent=S||'';
 const linked=DRV.ok();c.querySelector('.dcount').classList.toggle('hide',!linked&&!DRV.n);
 c.querySelector('.dcount').textContent=(T.driveIn||'')+' '+DRV.n}
function driveHello(){const t=DRV.win&&!DRV.win.closed?DRV.win:(DRV.frame&&DRV.frame.contentWindow);if(!t)return;
 const label=DRV.ok()?T.driveRelink:(DRV.state==='auth'?T.driveRelink:T.driveLink);
 try{t.postMessage({v:1,type:'biruni-drive-hello',nonce:DRV.nonce,label:String(label||'Google Drive').slice(0,80),
  dir:document.documentElement.dir==='rtl'?'rtl':'ltr'},DRV.ORIGIN)}catch(e){}}
function driveLabel(){if(!DRV.card)return;DRV.card.querySelector('.dtitle').textContent=T.driveTitle||'Google Drive';
 DRV.card.querySelector('.dpop').textContent=T.driveOpen||'';driveSay(DRV.state);if(DRV.ready)driveHello()}
function driveFrame(){if(DRV.frame||!DRV.card)return;const f=document.createElement('iframe');f.className='dframe';
 f.title='Google Drive';f.src=DRV.HELPER+'/link/';DRV.frame=f;DRV.card.querySelector('.dlink').appendChild(f);
 setTimeout(()=>{if(!DRV.ready)drivePopupButton()},8000)}
function drivePopupButton(){if(DRV.card)DRV.card.querySelector('.dpop').classList.remove('hide')}
function driveOpenWindow(){const w=window.open(DRV.HELPER+'/link/','biruni-drive','popup,width=480,height=640');
 if(!w){driveSay('blocked');return}DRV.win=w}
function driveToken(m){if(typeof m.token!=='string'||!m.token||m.token.length>4096||!(+m.exp>Date.now()))return;
 DRV.tok=m.token;DRV.exp=+m.exp;driveSay('idle');driveHello();if(typeof drivePump==='function')drivePump()}
function driveDead(){DRV.tok='';DRV.exp=0;driveSay('auth');driveHello()}
window.addEventListener('message',e=>{
 if(!DRV.on||e.origin!==DRV.ORIGIN)return;
 const fromFrame=DRV.frame&&e.source===DRV.frame.contentWindow,fromWin=DRV.win&&e.source===DRV.win;
 if(!fromFrame&&!fromWin)return;const m=e.data;if(!m||m.v!==1||typeof m.type!=='string')return;
 if(m.type==='biruni-drive-ready'){DRV.ready=true;driveHello();return}
 if(m.nonce!==DRV.nonce)return;
 if(m.type==='biruni-drive-token')driveToken(m);
 else if(m.type==='biruni-drive-error'){const c=String(m.code||'');
  if(c==='gsi'||c==='popup_blocked'){drivePopupButton();driveSay(c==='popup_blocked'?'blocked':'err')}
  else if(c==='denied'||c==='scope')driveSay('denied');else if(c!=='closed')driveSay('err')}});
function driveStart(){if(DRV.on||typeof DRIVE_ON==='undefined'||!DRIVE_ON||!DRV.ORIGIN)return;DRV.on=true;
 DRV.nonce=rid();DRV.n=+kept('biruni-drive-c-'+ROOM)||0;const ua=driveUA();
 const c=driveEl('div','card dcard');c.id='driveCard';
 const r=driveEl('div','row');r.appendChild(driveEl('span','lbl dtitle'));r.appendChild(driveEl('span','dcount hide'));c.appendChild(r);
 c.appendChild(driveEl('div','dstate'));c.appendChild(driveEl('div','dlink'));
 const pb=driveEl('button','ghost dpop hide');pb.type='button';pb.onclick=driveOpenWindow;c.appendChild(pb);
 const w=$('work');w.insertBefore(c,w.firstChild);DRV.card=c;driveLabel();
 if(ua.inapp){driveSay('inapp');return}
 driveSay('unlinked');if(ua.ios)drivePopupButton();else driveFrame();
 window.addEventListener('online',()=>{if(!DRV.ready&&DRV.frame){DRV.frame.src=DRV.HELPER+'/link/'}drivePump()});
 setInterval(drivePump,15000)}

/* ---- the upload: one photo at a time, as one .bfp file (drive.py) in biruni-photos-<tag>/p-<device>/ ----
   A photo goes to Drive only while the line to the computer is closed (the bridge sends it otherwise), and never
   both: DRV.HOLD says which way has it. The phone's own key (ECDSA P-256, not extractable), the numbering and the
   files' header bytes are kept in the database biruni-drive, so a retry uploads the very same bytes. */
const DMAX=5*1024*1024;   /* step 8 adds resumable uploads for larger photos */
let DDB=null,DKEY=null,DBUSY=false;const DSKIP=new Set();
function ddb(){return DDB||(DDB=new Promise(r=>{try{const q=indexedDB.open('biruni-drive',1);
 q.onupgradeneeded=()=>q.result.createObjectStore('d',{keyPath:'id'});q.onsuccess=()=>r(q.result);q.onerror=()=>r(null);
 q.onblocked=()=>setTimeout(()=>r(null),4000)}catch(e){r(null)}}))}
async function dget(id){const d=await ddb();if(!d)return null;return new Promise(r=>{try{const g=d.transaction('d').objectStore('d').get(id);
 g.onsuccess=()=>r(g.result||null);g.onerror=()=>r(null)}catch(e){r(null)}})}
async function dput(rec){const d=await ddb();if(!d)return;return new Promise(r=>{try{const t=d.transaction('d','readwrite');
 t.objectStore('d').put(rec);t.oncomplete=()=>r();t.onerror=()=>r();t.onabort=()=>r()}catch(e){r()}})}
async function dkey(){if(DKEY)return DKEY;let k=await dget('#key');
 if(!k||!k.kp||!k.pub){const kp=await crypto.subtle.generateKey({name:'ECDSA',namedCurve:'P-256'},false,['sign','verify']);
  k={id:'#key',kp:kp,pub:b64u.enc(await crypto.subtle.exportKey('raw',kp.publicKey))};await dput(k)}
 return DKEY=k}
function dnext(){const n=(+kept('biruni-drive-n')||0)+1;keep('biruni-drive-n',String(n));return n}
async function dapi(path,opt){opt=opt||{};const ac=new AbortController(),tm=setTimeout(()=>ac.abort(),opt.ms||60000);
 opt.headers=Object.assign({Authorization:'Bearer '+DRV.tok},opt.headers||{});opt.signal=ac.signal;
 try{return await fetch(DRV.API+path,opt)}finally{clearTimeout(tm)}}
class DriveStop extends Error{constructor(state){super(state);this.state=state}}
async function dcheck(r){if(r.ok)return r;if(r.status===401){driveDead();throw new DriveStop('auth')}
 let j=null;try{j=await r.clone().json()}catch(e){}
 const why=j&&j.error&&((j.error.errors||[])[0]||{}).reason||'';
 if(r.status===403&&/storageQuotaExceeded|quotaExceeded/.test(why)){driveSay('full');throw new DriveStop('full')}
 const e=new Error('drive '+r.status+' '+why);e.status=r.status;throw e}
const DFOLDER='application/vnd.google-apps.folder';
const dq=s=>"'"+String(s).replace(/\\/g,'\\\\').replace(/'/g,"\\'")+"'";
async function dfind(q){const r=await dcheck(await dapi('/drive/v3/files?spaces=drive&orderBy=createdTime&pageSize=10&fields='
 +encodeURIComponent('files(id,name,size,createdTime)')+'&q='+encodeURIComponent(q)));return (await r.json()).files||[]}
async function dmkdir(name,parent,props){const m={name:name,mimeType:DFOLDER,appProperties:props};if(parent)m.parents=[parent];
 const r=await dcheck(await dapi('/drive/v3/files?fields=id',{method:'POST',headers:{'Content-Type':'application/json'},
  body:JSON.stringify(m)}));return (await r.json()).id}
async function dfolder(name,parent,key,val){
 const q="name="+dq(name)+" and mimeType="+dq(DFOLDER)+" and trashed=false and appProperties has { key="+dq(key)
  +" and value="+dq(val)+" }"+(parent?" and "+dq(parent)+" in parents":"");
 let f=await dfind(q);if(f.length)return f[0].id;
 const mine=await dmkdir(name,parent,{[key]:val});f=await dfind(q);
 /* two phones made it at the same moment: keep the oldest (Drive for desktop would show the other as "(1)") */
 if(f.length&&f[0].id!==mine){try{await dapi('/drive/v3/files/'+mine,{method:'DELETE'})}catch(e){}return f[0].id}
 return mine}
async function dfolders(tag){const ck='biruni-drive-f-'+ROOM;try{const c=JSON.parse(kept(ck)||'null');
  if(c&&c.r&&c.p&&c.t===tag)return c}catch(e){}
 const r=await dfolder('biruni-photos-'+tag,'','biruniRoom',tag),p=await dfolder('p-'+DEVID,r,'biruniDev',DEVID);
 const c={t:tag,r:r,p:p};keep(ck,JSON.stringify(c));return c}
function deligible(it){return it&&it.meta&&it.meta.sealed&&!it.carried&&it.blob&&it.blob.size<=DMAX&&!DRV.HOLD.has(it.id)
 &&!DSKIP.has(it.id)}
async function dupload(it){const tag=await BFP.tag(KEYB),k=await dkey();let rec=await dget(it.id);
 if(rec&&rec.room===tag&&rec.st==='done')return rec;   /* sent before; only leaving the queue was missed */
 if(!rec||rec.room!==tag||!rec.head){const body=new Uint8Array(await it.blob.arrayBuffer()),n=dnext();
  const h={room:tag,dev:DEVID,kid:await BFP.kid(it.pk||PCKEY),id:it.id,n:n,t:+it.meta.t||Date.now(),pub:k.pub,
   cert:await BFP.cert(KEYB,SURVKEY,k.pub,DEVID)};
  const f=await BFP.pack(h,body,k.kp.privateKey);
  rec={id:it.id,room:tag,n:n,name:f.name,head:f.head,size:f.bytes.length,fsha:await BFP.sha(f.bytes),st:'q',tries:0,at:Date.now()};
  await dput(rec)}
 const fo=await dfolders(tag);
 if(rec.tries>0){   /* the last try may have arrived without its answer: look before sending again */
  const got=await dfind("appProperties has { key='bfp' and value="+dq(it.id)+" } and "+dq(fo.p)+" in parents and trashed=false");
  if(got.some(g=>+g.size===rec.size))return rec}
 rec.tries++;await dput(rec);
 const b='bfp'+rid().replace(/[^A-Za-z0-9]/g,''),meta={name:rec.name,parents:[fo.p],appProperties:{bfp:it.id}};
 const body=new Blob(['--'+b+'\r\nContent-Type: application/json; charset=UTF-8\r\n\r\n'+JSON.stringify(meta)+'\r\n--'+b
  +'\r\nContent-Type: application/octet-stream\r\n\r\n',rec.head,it.blob,'\r\n--'+b+'--\r\n']);
 let r;try{r=await dapi('/upload/drive/v3/files?uploadType=multipart&fields='+encodeURIComponent('id,size,sha256Checksum'),
  {method:'POST',headers:{'Content-Type':'multipart/related; boundary='+b},body:body,ms:180000})}
 catch(e){throw new Error('network')}
 if(r.status===404){keep('biruni-drive-f-'+ROOM,null);throw new Error('folder gone')}
 await dcheck(r);const j=await r.json();
 if(+j.size!==rec.size||(j.sha256Checksum&&j.sha256Checksum!==rec.fsha))throw new Error('drive copy differs');
 rec.fid=j.id;return rec}
async function drivePump(){if(!DRV.on||DBUSY||!DRV.ok()||(typeof lineOpen==='function'&&lineOpen()))return;DBUSY=true;
 try{const items=(await all()).filter(deligible).sort((a,b)=>a.meta.t-b.meta.t);
  for(const it of items){if(!DRV.ok()||(typeof lineOpen==='function'&&lineOpen()))break;
   DRV.HOLD.set(it.id,'drive');driveSay('up');
   try{const rec=await dupload(it);rec.st='done';rec.head=null;rec.at=Date.now();await dput(rec);
    await del(it.id);DRV.n++;keep('biruni-drive-c-'+ROOM,String(DRV.n));mark(it.id,'✓');count();driveSay('idle')}
   catch(e){if(e instanceof DriveStop)break;
    if(e.status===400||e.status===413){DSKIP.add(it.id)}   /* Drive will never take it: it stays for the bridge */
    driveSay('wait');break}
   finally{DRV.HOLD.delete(it.id)}}}
 catch(e){}finally{DBUSY=false}}
"""


def drive_js():
    return DRIVE_JS.replace("__DRIVE_HELPER__", HELPER).replace("__DRIVE_API__", API)


def _origin(url):
    from urllib.parse import urlsplit

    p = urlsplit(url)
    return f"{p.scheme}://{p.netloc}"


def helper_page():
    """The link page with its Content-Security-Policy: its own script and style (by SHA-256), Google's sign-in
    script, and talking only to Google's sign-in and the Drive API."""
    page = (_HELPER_HTML.replace("__CID__", CLIENT_ID).replace("__SCOPE__", SCOPE).replace("__API__", API)
            .replace("__PARENT__", PARENT).replace("__GSI__", GSI))

    def sha(tag):
        body = page.split(f"<{tag}>", 1)[1].split(f"</{tag}>", 1)[0]
        return "'sha256-" + base64.b64encode(hashlib.sha256(body.encode("utf-8")).digest()).decode() + "'"

    assert page.count("<script>") == 1 and page.count("<style>") == 1 and "<script " not in page
    gsi_dir = GSI.rsplit("/", 1)[0] + "/"
    csp = "; ".join([
        "default-src 'none'", f"script-src {sha('script')} {GSI}", f"style-src {sha('style')}",
        f"connect-src {gsi_dir} {_origin(API)}", f"frame-src {gsi_dir}", "img-src 'none'",
        "base-uri 'none'", "form-action 'none'"])
    return page.replace('<meta charset="utf-8">',
                        f'<meta charset="utf-8"><meta http-equiv="Content-Security-Policy" content="{csp}">', 1)
