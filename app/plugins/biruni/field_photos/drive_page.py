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
