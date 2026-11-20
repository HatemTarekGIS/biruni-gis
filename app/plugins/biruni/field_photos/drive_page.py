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
PARENT = os.environ.get("BIRUNI_DRIVE_PARENT") or "https://photos.biruni-gis.com"
GSI = os.environ.get("BIRUNI_DRIVE_GSI") or "https://accounts.google.com/gsi/client"
PICKER = os.environ.get("BIRUNI_DRIVE_PICKER") or "https://apis.google.com/js/api.js"
APP_ID = CLIENT_ID.split("-", 1)[0]   # Picker's app id is the Google Cloud project number
PICKER_FRAMES = ("https://docs.google.com", "https://drive.google.com")
HELPER_PATH = "/link/"
TEST_VARS = ("BIRUNI_DRIVE_API", "BIRUNI_DRIVE_HELPER", "BIRUNI_DRIVE_PARENT", "BIRUNI_DRIVE_GSI", "BIRUNI_DRIVE_PICKER")

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
<div class="w" id="ui" hidden><div class="d" id="hint"></div><button id="go" type="button" disabled>Google Drive</button>
<div class="d" id="who"></div><div class="d" id="csp"></div></div>
<div class="alone" id="alone" hidden><h1>Biruni GIS</h1>
<p>This page links a phone's Google Drive to Biruni's field photos page. Open the photos page from the office's
QR code instead.</p><p dir="rtl">هذه الصفحة تربط جوجل درايف بصفحة صور الموقع في بيروني. افتح صفحة الصور من رمز QR الخاص بالمكتب.</p></div>
<script>
const CID='__CID__',SCOPE='__SCOPE__',API='__API__',PARENT='__PARENT__',GSI='__GSI__',PICKER='__PICKER__',APPID='__APPID__';
const $=i=>document.getElementById(i);
const TO=window.opener||(window.parent!==window?window.parent:null);
const MODE=window.opener?'popup':(TO?'iframe':'alone');
let nonce='',tc=null,gsiOk=false,gsiErr=false,pending=false,pick=null,tok=null,picking=false,autoPicked=false,pkLoad=null;
function post(m){if(!TO)return;m.v=1;try{TO.postMessage(m,PARENT)}catch(e){}}
function say(m){if(nonce){m.nonce=nonce;post(m)}}
function keepTok(t,exp){try{sessionStorage.setItem('biruni-drive-tok',JSON.stringify({t:t,exp:exp}))}catch(e){}}
function cachedTok(){try{const c=JSON.parse(sessionStorage.getItem('biruni-drive-tok')||'null');
 if(c&&typeof c.t==='string'&&+c.exp-Date.now()>300000)return c}catch(e){}return null}
function hint(){try{return localStorage.getItem('biruni-drive-hint')||''}catch(e){return ''}}
function showWho(){const h=hint();$('who').textContent=h}
function ready(){$('go').disabled=!(nonce&&(gsiOk||(pick&&tok)))||pending||picking}
function tokOk(t){return t&&typeof t.t==='string'&&t.t&&t.t.length<=4096&&+t.exp-Date.now()>60000}
/* ---- choosing the office's folder (only in the window, never in the small frame): Google Picker ---- */
function loadPicker(){if(!pkLoad){pkLoad=new Promise((ok,no)=>{
  const go=()=>{try{gapi.load('picker',{callback:ok,onerror:()=>no(new Error('load')),timeout:20000,
   ontimeout:()=>no(new Error('timeout'))})}catch(e){no(e)}};
  if(window.gapi&&gapi.load)return go();const s=document.createElement('script');s.src=PICKER;s.async=true;
  s.onload=go;s.onerror=()=>no(new Error('script'));document.head.appendChild(s)});pkLoad.catch(()=>{pkLoad=null})}
 return pkLoad}
async function openPicker(){if(picking||!pick||!tokOk(tok))return;picking=true;ready();
 try{await loadPicker();const P=google.picker,F='application/vnd.google-apps.folder';
  const view=shared=>{const v=new P.DocsView(P.ViewId.FOLDERS).setSelectFolderEnabled(true).setIncludeFolders(true)
   .setMimeTypes(F).setMode(P.DocsViewMode.LIST).setQuery(pick.name);if(shared)v.setOwnedByMe(false);return v};
  new P.PickerBuilder().addView(view(true)).addView(view(false)).setOAuthToken(tok.t).setAppId(APPID)
   .setCallback(onPick).build().setVisible(true)}
 catch(e){picking=false;ready();say({type:'biruni-drive-error',code:'picker'})}}
function onPick(d){const P=google.picker,a=d[P.Response.ACTION];
 if(a===P.Action.PICKED){picking=false;ready();const doc=(d[P.Response.DOCUMENTS]||[])[0]||{};
  const id=doc[P.Document.ID],name=doc[P.Document.NAME],mt=doc[P.Document.MIME_TYPE];
  if(name===pick.name&&mt==='application/vnd.google-apps.folder'&&typeof id==='string'&&/^[\w-]{10,128}$/.test(id)){
   $('hint').textContent='';say({type:'biruni-drive-folder',id:id,name:name});setTimeout(()=>window.close(),400)}
  else $('hint').textContent=pick.wrong}
 else if(a===P.Action.CANCEL){picking=false;ready();say({type:'biruni-drive-error',code:'pick_cancel'})}}
document.addEventListener('securitypolicyviolation',e=>{
 $('csp').textContent='CSP: '+e.effectiveDirective+' '+String(e.blockedURI||'').slice(0,100)});
function fail(code){pending=false;ready();say({type:'biruni-drive-error',code:code})}
async function whoAmI(tok){try{const r=await fetch(API+'/drive/v3/about?fields=user(emailAddress)',
  {headers:{Authorization:'Bearer '+tok}});const j=await r.json();const e=j&&j.user&&j.user.emailAddress;
 if(e){try{localStorage.setItem('biruni-drive-hint',String(e).slice(0,120))}catch(x){}showWho()}}catch(e){}}
function onToken(r){pending=false;ready();
 if(!r||r.error){say({type:'biruni-drive-error',code:(r&&r.error==='access_denied')?'denied':'other'});return}
 if(!google.accounts.oauth2.hasGrantedAllScopes(r,SCOPE)){say({type:'biruni-drive-error',code:'scope'});return}
 const exp=Date.now()+(+r.expires_in||3600)*1000;keepTok(r.access_token,exp);
 say({type:'biruni-drive-token',token:r.access_token,exp:exp,cached:false});
 if(pick){tok={t:r.access_token,exp:exp};whoAmI(r.access_token);openPicker();return}
 whoAmI(r.access_token).then(()=>{if(MODE==='popup')setTimeout(()=>window.close(),400)})}
function onError(e){const t=e&&e.type;fail(t==='popup_failed_to_open'?'popup_blocked':t==='popup_closed'?'closed':'other')}
$('go').onclick=()=>{if(pick&&tokOk(tok)){openPicker();return}
 if(!tc||!nonce||pending)return;pending=true;ready();const h=hint();
 const o={prompt:h?'':'select_account'};if(h)o.login_hint=h;try{tc.requestAccessToken(o)}catch(e){fail('other')}};
window.addEventListener('message',e=>{
 if(!TO||e.origin!==PARENT||e.source!==TO)return;const m=e.data;
 if(!m||m.v!==1||m.type!=='biruni-drive-hello'||typeof m.nonce!=='string'||!/^[\w-]{8,64}$/.test(m.nonce))return;
 const fresh=nonce!==m.nonce;nonce=m.nonce;
 $('go').textContent=String(m.label||'Google Drive').slice(0,80);
 document.documentElement.dir=m.dir==='rtl'?'rtl':'ltr';
 const p=m.pick;
 if(MODE==='popup'&&p&&typeof p==='object'&&/^biruni-photos-[0-9a-f]{16}$/.test(String(p.name))){
  pick={name:p.name,wrong:String(p.wrong||'').slice(0,200)};$('hint').textContent=String(p.hint||'').slice(0,200);
  const t={t:p.token,exp:+p.exp};if(tokOk(t))tok=t}
 ready();if(pick&&tokOk(tok)&&!autoPicked){autoPicked=true;openPicker()}
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
const DRV={on:false,tok:'',exp:0,state:'',nonce:'',frame:null,win:null,card:null,ready:false,n:0,tag:'',fold:null,bad:0,
 HELPER:'__DRIVE_HELPER__',API:'__DRIVE_API__',HOLD:new Map(),
 ok(){return !!this.tok&&Date.now()<this.exp-300000},
 go(){return this.ok()&&!!this.fold},
 fname(){return 'biruni-photos-'+this.tag},
 holds(id){return this.HOLD.get(id)==='drive'}};
DRV.ORIGIN=(()=>{try{return new URL(DRV.HELPER).origin}catch(e){return ''}})();
function driveUA(){const u=navigator.userAgent||'';
 return {inapp:/; wv\)|FBAN|FBAV|Instagram|Line\/|TikTok|musical_ly|Snapchat|LinkedInApp|MicroMessenger|GSA\//.test(u)
   ||(/iPad|iPhone|iPod/.test(u)&&/AppleWebKit/.test(u)&&!/Safari\//.test(u)),
  ios:/iPad|iPhone|iPod/.test(u)||(navigator.platform==='MacIntel'&&navigator.maxTouchPoints>1)}}
function driveEl(tag,cls,txt){const e=document.createElement(tag);if(cls)e.className=cls;if(txt!=null)e.textContent=txt;return e}
function driveSay(state){DRV.state=state;const c=DRV.card;if(!c)return;
 const isG=typeof GDRIVE!=='undefined'&&GDRIVE;
 const S={unlinked:T.driveUnlinked,auth:T.driveAuth,idle:isG?(T.driveGps||T.driveIdle):T.driveIdle,up:T.driveUp,
  wait:T.driveWait,full:T.driveFull,err:T.driveErr,denied:T.driveDenied,inapp:T.driveInApp,
  blocked:T.drivePopupBlocked,pick:T.drivePickHint,ro:T.drivePickRO,gone:T.driveGone}[state]||'';
 c.querySelector('.dstate').textContent=String(S||'').replace('{name}',DRV.fname());
 const linked=DRV.ok();c.querySelector('.dcount').classList.toggle('hide',!linked&&!DRV.n);
 c.querySelector('.dcount').textContent=(T.driveIn||'')+' '+DRV.n;
 const f=DRV.fold,pk=c.querySelector('.dpick'),fo=c.querySelector('.dfold');
 pk.textContent=(f?T.drivePickAgain:T.drivePick)||'';
 pk.classList.toggle('hide',!(linked&&DRV.tag&&(!f||f.own||state==='pick'||state==='gone'||state==='ro')));
 fo.textContent=f?((T.driveFolder||'')+' '+DRV.fname()+(f.own?' · '+(T.driveOwn||''):'')):'';
 fo.classList.toggle('hide',!f);
 const pop=c.querySelector('.dpop');pop.textContent=(state==='auth'&&dsendLabel())||T.driveOpen||'';
 if(typeof dshowFree==='function'&&!DFREEING)dshowFree()}
function dcache(){try{const c=JSON.parse(kept('biruni-drive-f-'+ROOM)||'null');
  if(c&&c.k===1&&c.t===DRV.tag&&typeof c.r==='string'&&(typeof c.p==='string'||typeof c.g==='string'))return c}
 catch(e){}return null}
function driveHello(){const w=DRV.win&&!DRV.win.closed?DRV.win:null,t=w||(DRV.frame&&DRV.frame.contentWindow);if(!t)return;
 let label=DRV.ok()?T.driveRelink:(DRV.state==='auth'?dsendLabel()||T.driveRelink:T.driveLink);DRV.label=label;
 const m={v:1,type:'biruni-drive-hello',nonce:DRV.nonce,dir:document.documentElement.dir==='rtl'?'rtl':'ltr'};
 /* the window (never the frame) is where the office's folder is chosen; it gets the token so no second sign-in */
 if(w&&DRV.tag&&(!DRV.fold||DRV.wantPick)){label=T.drivePick;const n=DRV.fname();
  m.pick={name:n,token:DRV.ok()?DRV.tok:'',exp:DRV.ok()?DRV.exp:0,
   hint:String(T.drivePickHint||'').replace('{name}',n),wrong:String(T.drivePickWrong||'').replace('{name}',n)}}
 m.label=String(label||'Google Drive').slice(0,80);
 try{t.postMessage(m,DRV.ORIGIN)}catch(e){}}
/* once the token's hour ended a new one needs a tap (a window may only open on one): the button counts the photos */
function dsendLabel(){return DRV.w>0&&T.driveSend?String(T.driveSend).replace('{n}',DRV.w):''}
function driveLabel(){if(!DRV.card)return;DRV.card.querySelector('.dtitle').textContent=T.driveTitle||'Google Drive';
 DRV.card.querySelector('.dpop').textContent=T.driveOpen||'';driveSay(DRV.state);if(DRV.ready)driveHello()}
function driveFrame(){if(DRV.frame||!DRV.card)return;const f=document.createElement('iframe');f.className='dframe';
 f.title='Google Drive';f.src=DRV.HELPER+'/link/';DRV.frame=f;DRV.card.querySelector('.dlink').appendChild(f);
 setTimeout(()=>{if(!DRV.ready)drivePopupButton()},8000)}
function drivePopupButton(){if(DRV.card)DRV.card.querySelector('.dpop').classList.remove('hide')}
function driveOpenWindow(){const w=window.open(DRV.HELPER+'/link/','biruni-drive','popup,width=480,height=640');
 if(!w){driveSay('blocked');return}DRV.win=w}
function driveOpenPick(){DRV.wantPick=true;driveOpenWindow()}
function driveToken(m){if(typeof m.token!=='string'||!m.token||m.token.length>4096||!(+m.exp>Date.now()))return;
 DRV.tok=m.token;DRV.exp=+m.exp;DRV.bad=0;dsoon();driveSay(DRV.fold?'idle':'pick');driveHello();if(typeof drivePump==='function')drivePump()}
async function drivePicked(m){const id=String(m.id||'');
 if(!DRV.tag||!DRV.ok()||!/^[\w-]{10,128}$/.test(id)||m.name!==DRV.fname())return;
 try{const v=await dverify(id);if(v==='ro'){driveSay('ro');return}
  const c={t:DRV.tag,r:id,k:1,own:v==='own',v:Date.now()};
  if(typeof DRIVE_ON!=='undefined'&&DRIVE_ON)c.p=await dfolder('p-'+DEVID,id,'biruniDev',DEVID);
  if(typeof GDRIVE!=='undefined'&&GDRIVE)c.g=await dfolder('g-'+DEVID,id,'biruniGps',DEVID);
  keep('biruni-drive-f-'+ROOM,JSON.stringify(c));
  DRV.fold=c;DRV.wantPick=false;DRV.bad=0;dsoon();driveSay('idle');drivePump()}
 catch(e){if(e instanceof DriveStop)return;driveSay(e.status===403||e.status===404||e.perm?'gone':'err')}}
function driveDead(){DRV.tok='';DRV.exp=0;driveSay('auth');driveHello()}
window.addEventListener('message',e=>{
 if(!DRV.on||e.origin!==DRV.ORIGIN)return;
 const fromFrame=DRV.frame&&e.source===DRV.frame.contentWindow,fromWin=DRV.win&&e.source===DRV.win;
 if(!fromFrame&&!fromWin)return;const m=e.data;if(!m||m.v!==1||typeof m.type!=='string')return;
 if(m.type==='biruni-drive-ready'){DRV.ready=true;driveHello();return}
 if(m.nonce!==DRV.nonce)return;
 if(m.type==='biruni-drive-token')driveToken(m);
 else if(m.type==='biruni-drive-folder'){if(fromWin)drivePicked(m)}
 else if(m.type==='biruni-drive-error'){const c=String(m.code||'');
  if(c==='pick_cancel'){if(!DRV.fold)driveSay('pick')}
  else if(c==='gsi'||c==='popup_blocked'){drivePopupButton();driveSay(c==='popup_blocked'?'blocked':'err')}
  else if(c==='denied'||c==='scope')driveSay('denied');else if(c!=='closed')driveSay('err')}});
function driveStart(){const useD=typeof DRIVE_ON!=='undefined'&&DRIVE_ON,useG=typeof GDRIVE!=='undefined'&&GDRIVE;
 if(DRV.on||(!useD&&!useG)||!DRV.ORIGIN)return;DRV.on=true;
 DRV.nonce=rid();DRV.n=+kept('biruni-drive-c-'+ROOM)||0;const ua=driveUA();
 const c=driveEl('div','card dcard');c.id='driveCard';
 const r=driveEl('div','row');r.appendChild(driveEl('span','lbl dtitle'));r.appendChild(driveEl('span','dcount hide'));c.appendChild(r);
 c.appendChild(driveEl('div','dstate'));c.appendChild(driveEl('div','dfold hide'));c.appendChild(driveEl('div','dlink'));
 const kb=driveEl('button','dpick hide');kb.type='button';kb.onclick=driveOpenPick;c.appendChild(kb);
 const pb=driveEl('button','ghost dpop hide');pb.type='button';pb.onclick=driveOpenWindow;c.appendChild(pb);
 const fb=driveEl('button','ghost dfree hide');fb.type='button';fb.onclick=()=>dfree();c.appendChild(fb);
 DRV.clean=kept('biruni-drive-clean-'+ROOM)||'keep';
 const w=$('work');w.insertBefore(c,w.firstChild);DRV.card=c;driveLabel();
 BFP.tag(KEYB).then(t=>{DRV.tag=t;DRV.fold=dcache();driveSay(DRV.ok()&&!DRV.fold?'pick':DRV.state);drivePump()},()=>{});
 if(ua.inapp){driveSay('inapp');return}
 driveSay('unlinked');if(ua.ios)drivePopupButton();else driveFrame();
 window.addEventListener('online',()=>{if(!DRV.ready&&DRV.frame){DRV.frame.src=DRV.HELPER+'/link/'}dsoon();drivePump()});
 setInterval(drivePump,15000);setInterval(dreceipts,120000);dshowFree()}

/* ---- the upload: one photo at a time, as one .bfp file (drive.py) in biruni-photos-<tag>/p-<device>/ ----
   Once linked with the office folder chosen, Google Drive carries EVERY original, even with the line to the computer
   open; the line then sends only the small preview, at once (bridge.js sendItem). The line carries the original only
   when Drive cannot (driveFirst false: not linked, no folder, full, too big, or failing for DFALL since the line
   opened). DRV.HOLD says which way has an original ('drive' or 'line'), so it never goes both. The phone's own key (ECDSA P-256, not extractable), the numbering and the
   files' header bytes are kept in the database biruni-drive, so a retry uploads the very same bytes. */
const DMAX=5*1024*1024;   /* the phone keeps every photo under 5 MB (fitPhoto), so one plain upload carries it */
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
 const e=new Error('drive '+r.status+' '+why);e.status=r.status;
 e.perm=r.status===403&&/insufficientFilePermissions|insufficientPermissions|forbidden|appNotAuthorizedToFile/.test(why);
 e.rate=r.status===403&&/rateLimitExceeded|userRateLimitExceeded/.test(why);e.ra=r.headers.get('Retry-After');throw e}
/* a failed try waits 2, 4, 8 ... 64 s (a little random, so many phones do not come back together); Google's
   Retry-After wins when longer (up to 10 minutes). Success, the network coming back, a new token or folder start over */
DRV.BACK=2000;DRV.fails=0;DRV.until=0;DRV.tm=0;
function ddelay(n,ra){let ms=Math.min(64000,DRV.BACK*Math.pow(2,Math.max(0,n-1)))+Math.floor(Math.random()*Math.min(1000,DRV.BACK/2));
 if(ra){let s=+ra;if(!isFinite(s)){const t=Date.parse(ra);s=isFinite(t)?(t-Date.now())/1000:0}
  if(s>0)ms=Math.max(ms,Math.min(600000,s*1000))}return ms}
function dlater(ms){clearTimeout(DRV.tm);   /* the wait is over: a pump still busy is waited for, the time is not lost */
 DRV.tm=setTimeout(()=>{DRV.tm=0;if(DBUSY){dlater(250);return}DRV.until=0;drivePump()},ms)}
function dsoon(){DRV.fails=0;DRV.until=0;clearTimeout(DRV.tm);DRV.tm=0}
const DFOLDER='application/vnd.google-apps.folder';
const dq=s=>"'"+String(s).replace(/\\/g,'\\\\').replace(/'/g,"\\'")+"'";
async function dfind(q){const r=await dcheck(await dapi('/drive/v3/files?spaces=drive&orderBy=createdTime&pageSize=10'
 +'&supportsAllDrives=true&includeItemsFromAllDrives=true&fields='
 +encodeURIComponent('files(id,name,size,createdTime)')+'&q='+encodeURIComponent(q)));return (await r.json()).files||[]}
async function dmkdir(name,parent,props){const m={name:name,mimeType:DFOLDER,appProperties:props};if(parent)m.parents=[parent];
 const r=await dcheck(await dapi('/drive/v3/files?supportsAllDrives=true&fields=id',{method:'POST',headers:{'Content-Type':'application/json'},
  body:JSON.stringify(m)}));return (await r.json()).id}
async function dfolder(name,parent,key,val){
 const q="name="+dq(name)+" and mimeType="+dq(DFOLDER)+" and trashed=false and appProperties has { key="+dq(key)
  +" and value="+dq(val)+" }"+(parent?" and "+dq(parent)+" in parents":"");
 let f=await dfind(q);if(f.length)return f[0].id;
 const mine=await dmkdir(name,parent,{[key]:val});f=await dfind(q);
 /* two phones made it at the same moment: keep the oldest (Drive for desktop would show the other as "(1)") */
 if(f.length&&f[0].id!==mine){try{await dapi('/drive/v3/files/'+mine,{method:'DELETE'})}catch(e){}return f[0].id}
 return mine}
/* the office's folder biruni-photos-<tag> is made by Biruni on the office computer and shared with the surveyor;
   the phone never makes it: it is chosen once in Google Picker (drivePicked), then kept per room */
async function dfolders(tag){const c=DRV.fold;if(c&&c.t===tag)return c;driveSay('pick');throw new DriveStop('pick')}
async function dverify(id){const r=await dapi('/drive/v3/files/'+encodeURIComponent(id)+'?supportsAllDrives=true&fields='
 +encodeURIComponent('id,name,mimeType,trashed,ownedByMe,capabilities(canAddChildren)'));
 if(r.status===404)return 'ok';   /* the write that follows is the real test */
 await dcheck(r);const j=await r.json();
 if(j.name!==DRV.fname()||j.mimeType!==DFOLDER||j.trashed){const e=new Error('not the folder');e.status=404;throw e}
 if(j.capabilities&&j.capabilities.canAddChildren===false)return 'ro';return j.ownedByMe?'own':'ok'}
/* Drive takes uploads into a folder in the bin without a word, and Drive for desktop never shows them: look at the
   office folder again every 5 minutes; in the bin, deleted or no longer shared, it is gone (the line takes the
   originals until it is chosen again) */
const DCHECK=300000;
async function dfresh(){const c=DRV.fold,ck='biruni-drive-f-'+ROOM;if(!c||Date.now()-(+c.v||0)<DCHECK)return;
 let v;try{v=await dverify(c.r)}catch(e){if(e instanceof DriveStop)throw e;
  if(e.status===403||e.status===404||e.perm){keep(ck,null);DRV.fold=null;driveSay('gone');throw new DriveStop('gone')}
  throw e}
 if(v==='ro'){keep(ck,null);DRV.fold=null;driveSay('ro');throw new DriveStop('ro')}
 c.v=Date.now();keep(ck,JSON.stringify(c))}
async function dlost(){const c=DRV.fold,ck='biruni-drive-f-'+ROOM;if(!c)throw new DriveStop('pick');
 try{const v=await dverify(c.r);if(v==='ro'){const e=new Error('ro');e.perm=true;e.ro=true;throw e}
  if(typeof DRIVE_ON!=='undefined'&&DRIVE_ON)c.p=await dfolder('p-'+DEVID,c.r,'biruniDev',DEVID);
  if(typeof GDRIVE!=='undefined'&&GDRIVE)c.g=await dfolder('g-'+DEVID,c.r,'biruniGps',DEVID);
  keep(ck,JSON.stringify(c))}
 catch(e){if(e instanceof DriveStop)throw e;
  if(e.status===403||e.status===404||e.perm){keep(ck,null);DRV.fold=null;driveSay(e.ro?'ro':'gone');throw new DriveStop('gone')}
  throw e}}
const DFALL=120000;   /* Drive failing this long while the line is open: the line takes the originals */
function dfits(it){return !!(typeof DRIVE_ON!=='undefined'&&DRIVE_ON&&it&&it.meta&&it.meta.sealed&&!it.carried
 &&!(it.cap&&typeof lineOpen==='function'&&lineOpen())&&it.blob&&it.blob.size<=DMAX&&!DSKIP.has(it.id))}
function deligible(it){return dfits(it)&&!DRV.HOLD.has(it.id)}
function driveFirst(it){return DRV.on&&DRV.go()&&!(DRV.bad&&(DRV.bad===1||Date.now()-DRV.bad>DFALL))&&dfits(it)}
async function dstill(id){return (await all()).some(x=>x.id===id)}
async function dupload(it){const tag=await BFP.tag(KEYB),k=await dkey();let rec=await dget(it.id);
 if(rec&&rec.room===tag&&rec.st==='done')return rec;   /* sent before; only leaving the queue was missed */
 if(!rec||rec.room!==tag||!rec.head){const body=new Uint8Array(await it.blob.arrayBuffer()),n=dnext();
  const h={room:tag,dev:DEVID,kid:await BFP.kid(it.pk||PCKEY),id:it.id,n:n,t:+it.meta.t||Date.now(),pub:k.pub,
   cert:await BFP.cert(KEYB,SURVKEY,k.pub,DEVID)};
  const f=await BFP.pack(h,body,k.kp.privateKey);
  rec={id:it.id,room:tag,n:n,name:f.name,head:f.head,size:f.bytes.length,fsha:await BFP.sha(f.bytes),st:'q',tries:0,at:Date.now()};
  await dput(rec)}
 const fo=await dfolders(tag);await dfresh();let j;
 try{
  if(rec.tries>0){   /* the last try may have arrived without its answer: look before sending again */
   const got=await dfind("appProperties has { key='bfp' and value="+dq(it.id)+" } and "+dq(fo.p)+" in parents and trashed=false");
   const g=got.find(g=>+g.size===rec.size);if(g){rec.fid=g.id;return rec}}
  rec.tries++;await dput(rec);
  const b='bfp'+rid().replace(/[^A-Za-z0-9]/g,''),meta={name:rec.name,parents:[fo.p],appProperties:{bfp:it.id}};
  const body=new Blob(['--'+b+'\r\nContent-Type: application/json; charset=UTF-8\r\n\r\n'+JSON.stringify(meta)+'\r\n--'+b
   +'\r\nContent-Type: application/octet-stream\r\n\r\n',rec.head,it.blob,'\r\n--'+b+'--\r\n']);
  let r;try{r=await dapi('/upload/drive/v3/files?uploadType=multipart&supportsAllDrives=true&fields='
   +encodeURIComponent('id,size,sha256Checksum'),
   {method:'POST',headers:{'Content-Type':'multipart/related; boundary='+b},body:body,ms:180000})}
  catch(e){throw new Error('network')}
  await dcheck(r);j=await r.json()}
 catch(e){if(e.status===404||e.perm)await dlost();   /* the share was taken back (stops), or only p-<device> went */
  throw e}
 if(+j.size!==rec.size||(j.sha256Checksum&&j.sha256Checksum!==rec.fsha)){
  try{await dapi('/drive/v3/files/'+encodeURIComponent(j.id)+'?supportsAllDrives=true',{method:'DELETE'})}catch(e){}
  throw new Error('drive copy differs')}
 rec.fid=j.id;return rec}
async function drivePump(){if(!DRV.on)return;
 if(DBUSY){DRV.again=true;return}   /* asked while busy (a new token, folder or photo): runs again right after */
 DBUSY=true;   /* before any await: never two pumps at once */
 try{try{const w=(await all()).filter(dfits).length;if(w!==DRV.w){DRV.w=w;if(DRV.state==='auth'){driveSay('auth');driveHello()}}}
  catch(e){}
  if(DRV.tok&&!DRV.ok()){driveDead();return}   /* the hour ended: say so, the line takes the originals meanwhile */
  if(Date.now()<DRV.until||!DRV.go())return;
  const items=(await all()).filter(deligible).sort((a,b)=>a.meta.t-b.meta.t);
  for(const it of items){if(!DRV.go())break;
   if(DRV.HOLD.has(it.id)||!await dstill(it.id))continue;   /* the line took it meanwhile */
   DRV.HOLD.set(it.id,'drive');driveSay('up');
   try{const rec=await dupload(it),fresh=rec.st!=='done';rec.st='done';rec.head=null;rec.at=Date.now();await dput(rec);
    await del(it.id);if(fresh){DRV.n++;keep('biruni-drive-c-'+ROOM,String(DRV.n))}DRV.bad=0;DRV.fails=0;DRV.until=0;mark(it.id,'✓');count();
    clearTimeout(DRV.rt);DRV.rt=setTimeout(dreceipts,60000);   /* by then the PC has it from Drive for desktop */
    driveSay('idle')}
   catch(e){if(e instanceof DriveStop){if(e.state==='full')DRV.bad=1;break}
    if(e.status===400||e.status===413){DSKIP.add(it.id)}   /* Drive will never take it: it stays for the bridge */
    else{if(!DRV.bad)DRV.bad=Date.now();DRV.fails++;const ms=ddelay(DRV.fails,e.ra);DRV.until=Date.now()+ms;dlater(ms)}
    driveSay('wait');break}
   finally{DRV.HOLD.delete(it.id)}}}
 catch(e){}finally{DBUSY=false;if(DRV.again){DRV.again=false;setTimeout(drivePump,50)}}}

/* ---- freeing the surveyor's Drive (step 10). The files are the surveyor's (drive.file), so only this phone can delete
   them, and only those the PC says it holds as full originals: a receipt over the line, signed with the PC's identity
   key (no pin, no receipts). The office chooses: keep (the default), manual (a button here) or auto (at once). A file
   goes to the surveyor's bin (Hatem's choice: it can still be restored; Drive empties the bin after 30 days). The small
   record of it goes 30 days later */
async function dall(){const d=await ddb();if(!d)return [];return new Promise(r=>{try{const g=d.transaction('d').objectStore('d').getAll();
 g.onsuccess=()=>r((g.result||[]).filter(x=>x&&x.id&&x.id[0]!=='#'));g.onerror=()=>r([])}catch(e){r([])}})}
async function dgall(){const d=await ddb();if(!d)return [];return new Promise(r=>{try{const g=d.transaction('d').objectStore('d').getAll();
 g.onsuccess=()=>r((g.result||[]).filter(x=>x&&x.id&&x.id.startsWith('#g')));g.onerror=()=>r([])}catch(e){r([])}})}
async function ddrop(id){const d=await ddb();if(!d)return;return new Promise(r=>{try{const t=d.transaction('d','readwrite');
 t.objectStore('d').delete(id);t.oncomplete=()=>r();t.onerror=()=>r();t.onabort=()=>r()}catch(e){r()}})}
async function dreceipts(){if(!DRV.on||typeof lineOpen!=='function'||!lineOpen()||!ID.pin)return;
 const now=Date.now(),a=await dall();
 for(const r of a)if(r.st==='freed'&&now-(+r.at||0)>2592000000)await ddrop(r.id);
 const ids=a.filter(r=>r.st==='done'&&r.fid).map(r=>r.id).slice(0,100);
 if(!ids.length){dshowFree();return}
 const nonce=rid();DRV.rq={nonce:nonce,ids:new Set(ids)};
 try{lineText(JSON.stringify({t:'dhave',nonce:nonce,ids:ids}))}catch(e){}}
async function driveGot(m){const q=DRV.rq;if(!q||!ID.pin||!m||m.nonce!==q.nonce||!Array.isArray(m.ids))return;
 const cl=String(m.clean||''),ids=m.ids.map(String);
 if(!['auto','manual','keep'].includes(cl)||!await IDOK(m.ip,m.is,'BRDG1|'+m.nonce+'|'+cl+'|'+ids.join(','))){
  SIG.refused=(SIG.refused||0)+1;return}
 DRV.rq=null;DRV.clean=cl;keep('biruni-drive-clean-'+ROOM,cl);
 for(const id of ids){if(!q.ids.has(id))continue;const r=await dget(id);if(r&&r.st==='done'){r.st='got';await dput(r)}}
 await dshowFree();if(cl==='auto')dfree();
 else if(q.ids.size>=100&&ids.length)setTimeout(dreceipts,2000)}   /* more than one batch waits */
async function dshowFree(){const c=DRV.card;if(!c)return;const n=(await dall()).filter(r=>r.st==='got').length;DRV.free=n;
 const b=c.querySelector('.dfree');b.textContent=String(T.driveFree||'').replace('{n}',n);
 b.classList.toggle('hide',!(n>0&&DRV.clean==='manual'&&DRV.ok()))}
var DFREEING=false;   /* var: driveSay may look at it before this line has run */
async function dfree(){if(DFREEING||!DRV.ok())return;DFREEING=true;
 try{for(const r of await dall()){if(r.st!=='got'||!r.fid)continue;let res;
   try{res=await dapi('/drive/v3/files/'+encodeURIComponent(r.fid)+'?supportsAllDrives=true&fields=id',
    {method:'PATCH',headers:{'Content-Type':'application/json'},body:JSON.stringify({trashed:true})})}catch(e){break}
   if(res.status===204||res.status===200||res.status===404){r.st='freed';r.at=Date.now();await dput(r)}
   else if(res.status===401){driveDead();break}
   else if(res.status===403)continue;   /* not ours to delete (the office took it over): left there */
   else break}}
 finally{DFREEING=false;await dshowFree();driveSay(DRV.state)}}
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
            .replace("__PARENT__", PARENT).replace("__GSI__", GSI).replace("__PICKER__", PICKER)
            .replace("__APPID__", APP_ID))

    def sha(tag):
        body = page.split(f"<{tag}>", 1)[1].split(f"</{tag}>", 1)[0]
        return "'sha256-" + base64.b64encode(hashlib.sha256(body.encode("utf-8")).digest()).decode() + "'"

    assert page.count("<script>") == 1 and page.count("<style>") == 1 and "<script " not in page
    gsi_dir = GSI.rsplit("/", 1)[0] + "/"
    pk = _origin(PICKER)
    # Google Picker (in the window only) adds its own script from apis.google.com, its dialog in a frame from
    # docs.google.com, and inline styles; so style-src allows inline styles here (scripts stay strict)
    csp = "; ".join([
        "default-src 'none'", f"script-src {sha('script')} {GSI} {pk}", "style-src 'unsafe-inline'",
        f"connect-src {gsi_dir} {_origin(API)} {pk}", f"frame-src {gsi_dir} {' '.join(PICKER_FRAMES)} {pk}",
        "img-src 'none'", "base-uri 'none'", "form-action 'none'"])
    return page.replace('<meta charset="utf-8">',
                        f'<meta charset="utf-8"><meta http-equiv="Content-Security-Policy" content="{csp}">', 1)
