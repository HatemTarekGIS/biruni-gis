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
 DRV.nonce=rid();const ua=driveUA();
 const c=driveEl('div','card dcard');c.id='driveCard';
 const r=driveEl('div','row');r.appendChild(driveEl('span','lbl dtitle'));r.appendChild(driveEl('span','dcount hide'));c.appendChild(r);
 c.appendChild(driveEl('div','dstate'));c.appendChild(driveEl('div','dlink'));
 const pb=driveEl('button','ghost dpop hide');pb.type='button';pb.onclick=driveOpenWindow;c.appendChild(pb);
 const w=$('work');w.insertBefore(c,w.firstChild);DRV.card=c;driveLabel();
 if(ua.inapp){driveSay('inapp');return}
 driveSay('unlinked');if(ua.ios)drivePopupButton();else driveFrame();
 window.addEventListener('online',()=>{if(!DRV.ready&&DRV.frame){DRV.frame.src=DRV.HELPER+'/link/'}})}
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
