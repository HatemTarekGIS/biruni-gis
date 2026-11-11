"""Accounts for the live paper and links without a card - a dashboard extension (SPEC.md, Extensions; accounts.py).

The QR code printed on a live paper is an address: the dashboard page with "#p=<paper id>". Anyone who scans it
with the phone's own camera lands in the page; with no card yet the page asks: carry on as a visitor, or sign in
with a username and password. The PC answers with a short-lived card (visitor template, or the user's own
template: widgets, hidden fields, zone, filters) and the page opens straight into the live paper's camera view.
The card is kept on the phone (localStorage) until it expires, so scanning again does not ask again; "Sign in" /
"Sign out" in the header and on the paper view change the view live.

Messages (the first three may come before any hello; the PC wraps DashboardService._receive for them):
  acct_info {paper}            -> acct_info {title, visitor, users}
  acct_visitor {paper}         -> acct_card {card, who, exp, name}  then the dashboard as after a hello
  acct_login {paper, user, password} -> acct_card ... | acct_err {why, wait}
  acct_logout                  -> acct_out (the card is revoked)
Errors are acct_err, not error (the page shows "error" itself as a banner).

Printed papers need addresses that outlive one sharing: the LAN token and the bridge key are kept for this Windows
user (accounts.stable), so a paper printed today still opens next week.
"""
import secrets

from . import accounts, paper as _paper

try:
    from .service import DashboardService, handler
except Exception:  # noqa: BLE001 - the page alone (tests of the page)
    DashboardService = handler = None


PRE = ("acct_info", "acct_visitor", "acct_login")


def _dash_of(svc, s, msg):
    if s.ready and s.dash_id:
        return s.dash_id
    pid = str(msg.get("paper") or "")[:40]
    if pid:
        p = _paper.saved(svc.project).get(pid)
        if p and p.get("dash") in svc.defs:
            return p["dash"]
    did = str(msg.get("dash") or "")[:40]
    if did in svc.defs:
        return did
    if len(svc.defs) == 1:
        return next(iter(svc.defs))
    return None


def _err(s, why, wait=0):
    s._out({"t": "acct_err", "why": why, "wait": int(wait)})


def _give(svc, s, dash, card, payload):
    s._out({"t": "acct_card", "card": card, "who": payload.get("acct"), "exp": payload.get("exp"),
            "name": payload.get("name")})
    svc._receive(s, {"t": "hello", "card": card})


def acct(svc, s, msg):
    t = msg.get("t")
    dash = _dash_of(svc, s, msg)
    if dash is None:
        _err(s, "no dashboard")
        return
    if t == "acct_info":
        info = accounts.summary(dash, svc.project)
        d = svc.defs.get(dash) or {}
        s._out({"t": "acct_info", "title": d.get("title", ""), "visitor": info["visitor"],
                "users": bool(info["users"])})
        return
    try:
        if t == "acct_visitor":
            card, payload = accounts.visitor_card(dash, svc.project)
        else:
            card, payload = accounts.login(dash, msg.get("user", ""), str(msg.get("password") or "")[:200],
                                           svc.project, session=s)
    except accounts.AccountError as e:
        _err(s, e.why, e.wait)
        return
    _give(svc, s, dash, card, payload)


if DashboardService is not None:
    for _t in PRE:
        handler(_t)(acct)

    @handler("acct_logout")
    def _logout(svc, s, msg):
        if s.card and s.card.get("acct"):
            accounts.logout(s.card, svc.project)
        s._out({"t": "acct_out"})

    _plain_receive = DashboardService._receive

    def _receive(self, s, msg):
        if isinstance(msg, dict) and msg.get("t") in PRE and not s.ready:
            try:
                return acct(self, s, msg)
            except Exception as e:  # noqa: BLE001
                _err(s, "failed: " + str(e)[:120])
                return None
        return _plain_receive(self, s, msg)

    if not getattr(DashboardService._receive, "_acct", False):
        _receive._acct = True
        _receive._sound = getattr(DashboardService._receive, "_sound", False)
        DashboardService._receive = _receive

    # ---- addresses that outlive one sharing (printed papers) ---------------------------------------------------
    try:
        from . import host as _host, server as _server

        if not getattr(_server.LanServer.__init__, "_stable", False):
            _lan_init = _server.LanServer.__init__

            def _lan(self, *a, **k):
                _lan_init(self, *a, **k)
                self.token = accounts.stable("lan", lambda: secrets.token_urlsafe(12))
            _lan._stable = True
            _server.LanServer.__init__ = _lan

        if not getattr(_host.Anywhere.__init__, "_stable", False):
            _any_init = _host.Anywhere.__init__

            def _any(self, lan):
                from ..field_photos import bridge

                real = bridge.new_key
                key = accounts.stable("bridge", real)
                bridge.new_key = lambda: key
                try:
                    _any_init(self, lan)
                finally:
                    bridge.new_key = real
            _any._stable = True
            _host.Anywhere.__init__ = _any
    except Exception:  # noqa: BLE001 - a fresh token each time is the old behaviour
        pass


# ---- the panel's action: Accounts… -------------------------------------------------------------------------------
def accounts_ui(plugin, dock):
    from .accounts_ui import AccountsDialog

    d = dock.current() if dock is not None and hasattr(dock, "current") else None
    from qgis.PyQt.QtWidgets import QMessageBox

    from ..i18n import tr

    if d is None:
        QMessageBox.information(dock, tr("Accounts"), tr("Choose a dashboard in the list first."))
        return None
    dlg = AccountsDialog(d, plugin, dock)
    dlg.exec()
    return dlg


def new_paper_address_ui(plugin, dock):
    """Fix for a long-lived paper address: a new LAN token, bridge key and PC identity (after a clear warning)."""
    from qgis.PyQt.QtWidgets import QMessageBox

    from ..i18n import tr

    ans = QMessageBox.question(
        dock, tr("New paper address"),
        tr("Live papers carry this computer's address in their QR code, and it stays the same so that printed "
           "papers keep working.\n\nMake a new address if a paper was lost or reached the wrong people. Every "
           "paper printed before will stop showing live data; print them again after this.\n\nMake a new address?"))
    if ans != QMessageBox.StandardButton.Yes:
        return None
    done = accounts.reset_stable()
    sharing = getattr(dock, "sharing", None)
    try:
        if sharing is not None and getattr(sharing, "anywhere", None) is not None:
            sharing.stop_anywhere()
            sharing.start_anywhere()
    except Exception:  # noqa: BLE001 - the new address is used at the next start
        pass
    QMessageBox.information(dock, tr("New paper address"),
                            tr("Done. Papers printed from now on use the new address; stop and start sharing on "
                               "this network for the new LAN address.") if done else
                            tr("There was no paper address yet: the first paper you print makes one."))
    return done


ACTIONS = [("acct_accounts", "Accounts and visitors…", "users", accounts_ui),
           ("acct_new_address", "New paper address…", "refresh", new_paper_address_ui)]

TEXTS = {
    "en": {"acct_signin": "Sign in", "acct_signout": "Sign out", "acct_visitor": "Continue as a visitor",
           "acct_user": "Username", "acct_pass": "Password", "acct_go": "Sign in",
           "acct_welcome": "Welcome", "acct_ask": "See this paper live: carry on as a visitor, or sign in to see your own data.",
           "acct_nousers": "This dashboard has no accounts.", "acct_novisit": "Visitors are not allowed: sign in.",
           "acct_bad": "Wrong username or password.", "acct_locked": "Too many tries. Try again in {n} min.",
           "acct_slow": "Please wait a moment and try again.", "acct_disabled": "This account is turned off.",
           "acct_nodash": "This paper's dashboard is not shared now.", "acct_fail": "The computer did not answer.",
           "acct_as": "Signed in as {name}", "acct_asvisitor": "Visitor", "acct_wait": "Connecting…"},
    "ar": {"acct_signin": "تسجيل الدخول", "acct_signout": "تسجيل الخروج", "acct_visitor": "المتابعة كزائر",
           "acct_user": "اسم المستخدم", "acct_pass": "كلمة المرور", "acct_go": "دخول",
           "acct_welcome": "مرحبًا", "acct_ask": "شاهد هذه الورقة حيّة: تابع كزائر، أو سجّل الدخول لترى بياناتك.",
           "acct_nousers": "لا توجد حسابات لهذه اللوحة.", "acct_novisit": "الزيارة غير متاحة: سجّل الدخول.",
           "acct_bad": "اسم المستخدم أو كلمة المرور غير صحيحة.", "acct_locked": "محاولات كثيرة. حاول بعد {n} دقيقة.",
           "acct_slow": "انتظر لحظة ثم حاول مجددًا.", "acct_disabled": "هذا الحساب موقوف.",
           "acct_nodash": "لوحة هذه الورقة غير متاحة الآن.", "acct_fail": "لم يُجب الحاسوب.",
           "acct_as": "مسجَّل باسم {name}", "acct_asvisitor": "زائر", "acct_wait": "جارٍ الاتصال…"},
    "es": {"acct_signin": "Iniciar sesión", "acct_signout": "Cerrar sesión", "acct_visitor": "Continuar como visitante",
           "acct_user": "Usuario", "acct_pass": "Contraseña", "acct_go": "Entrar", "acct_welcome": "Bienvenido",
           "acct_ask": "Vea este papel en vivo: continúe como visitante o inicie sesión para ver sus datos.",
           "acct_nousers": "Este panel no tiene cuentas.", "acct_novisit": "No se admiten visitantes: inicie sesión.",
           "acct_bad": "Usuario o contraseña incorrectos.", "acct_locked": "Demasiados intentos. Vuelva en {n} min.",
           "acct_slow": "Espere un momento e inténtelo de nuevo.", "acct_disabled": "Esta cuenta está desactivada.",
           "acct_nodash": "El panel de este papel no está compartido ahora.", "acct_fail": "El equipo no respondió.",
           "acct_as": "Sesión de {name}", "acct_asvisitor": "Visitante", "acct_wait": "Conectando…"},
    "fr": {"acct_signin": "Se connecter", "acct_signout": "Se déconnecter", "acct_visitor": "Continuer en visiteur",
           "acct_user": "Nom d'utilisateur", "acct_pass": "Mot de passe", "acct_go": "Entrer", "acct_welcome": "Bienvenue",
           "acct_ask": "Voyez ce papier en direct : continuez en visiteur ou connectez-vous pour voir vos données.",
           "acct_nousers": "Ce tableau de bord n'a pas de comptes.",
           "acct_novisit": "Les visiteurs ne sont pas admis : connectez-vous.",
           "acct_bad": "Nom d'utilisateur ou mot de passe incorrect.",
           "acct_locked": "Trop d'essais. Réessayez dans {n} min.", "acct_slow": "Attendez un instant et réessayez.",
           "acct_disabled": "Ce compte est désactivé.", "acct_nodash": "Le tableau de bord de ce papier n'est pas partagé.",
           "acct_fail": "L'ordinateur n'a pas répondu.", "acct_as": "Connecté : {name}", "acct_asvisitor": "Visiteur",
           "acct_wait": "Connexion…"},
    "pt_BR": {"acct_signin": "Entrar", "acct_signout": "Sair", "acct_visitor": "Continuar como visitante",
              "acct_user": "Usuário", "acct_pass": "Senha", "acct_go": "Entrar", "acct_welcome": "Bem-vindo",
              "acct_ask": "Veja este papel ao vivo: continue como visitante ou entre para ver seus dados.",
              "acct_nousers": "Este painel não tem contas.", "acct_novisit": "Visitantes não são permitidos: entre.",
              "acct_bad": "Usuário ou senha incorretos.", "acct_locked": "Tentativas demais. Tente em {n} min.",
              "acct_slow": "Aguarde um momento e tente de novo.", "acct_disabled": "Esta conta está desativada.",
              "acct_nodash": "O painel deste papel não está compartilhado agora.",
              "acct_fail": "O computador não respondeu.", "acct_as": "Conectado como {name}",
              "acct_asvisitor": "Visitante", "acct_wait": "Conectando…"},
    "tr": {"acct_signin": "Giriş yap", "acct_signout": "Çıkış yap", "acct_visitor": "Ziyaretçi olarak devam et",
           "acct_user": "Kullanıcı adı", "acct_pass": "Parola", "acct_go": "Giriş", "acct_welcome": "Hoş geldiniz",
           "acct_ask": "Bu kâğıdı canlı görün: ziyaretçi olarak devam edin ya da kendi verileriniz için giriş yapın.",
           "acct_nousers": "Bu panoda hesap yok.", "acct_novisit": "Ziyaretçiye izin yok: giriş yapın.",
           "acct_bad": "Kullanıcı adı ya da parola yanlış.", "acct_locked": "Çok fazla deneme. {n} dk sonra deneyin.",
           "acct_slow": "Biraz bekleyip yeniden deneyin.", "acct_disabled": "Bu hesap kapalı.",
           "acct_nodash": "Bu kâğıdın panosu şu an paylaşılmıyor.", "acct_fail": "Bilgisayar yanıt vermedi.",
           "acct_as": "{name} olarak giriş yapıldı", "acct_asvisitor": "Ziyaretçi", "acct_wait": "Bağlanıyor…"},
    "fa": {"acct_signin": "ورود", "acct_signout": "خروج", "acct_visitor": "ادامه به‌عنوان بازدیدکننده",
           "acct_user": "نام کاربری", "acct_pass": "گذرواژه", "acct_go": "ورود", "acct_welcome": "خوش آمدید",
           "acct_ask": "این برگه را زنده ببینید: به‌عنوان بازدیدکننده ادامه دهید یا برای دیدن داده‌های خود وارد شوید.",
           "acct_nousers": "این داشبورد حسابی ندارد.", "acct_novisit": "بازدیدکننده مجاز نیست: وارد شوید.",
           "acct_bad": "نام کاربری یا گذرواژه نادرست است.", "acct_locked": "تلاش‌های زیاد. پس از {n} دقیقه دوباره بکوشید.",
           "acct_slow": "کمی صبر کنید و دوباره بکوشید.", "acct_disabled": "این حساب غیرفعال است.",
           "acct_nodash": "داشبورد این برگه اکنون هم‌رسانی نشده است.", "acct_fail": "رایانه پاسخ نداد.",
           "acct_as": "وارد شده با نام {name}", "acct_asvisitor": "بازدیدکننده", "acct_wait": "در حال اتصال…"},
}

CSS = r"""
.acctGate{position:fixed;inset:0;z-index:90;display:flex;align-items:center;justify-content:center;padding:16px;
 background:color-mix(in srgb,var(--bg) 82%,#000 18%);backdrop-filter:blur(6px)}
.acctGate.hide{display:none}
.acctBox{width:min(420px,100%);background:var(--surface);color:var(--text);border-radius:18px;padding:22px 20px;
 box-shadow:0 20px 60px rgba(0,0,0,.35);border:1px solid var(--line,rgba(127,127,127,.2))}
.acctBox h2{margin:0 0 6px;font-size:20px}.acctBox p{margin:0 0 16px;color:var(--muted);font-size:14px;line-height:1.6}
.acctBox .acctV{width:100%;padding:13px;border-radius:12px;border:0;font-size:16px;font-weight:700;cursor:pointer;
 background:var(--accent);color:#fff;margin-bottom:14px}
.acctBox form{display:grid;gap:9px}.acctBox label{font-size:12.5px;color:var(--muted)}
.acctBox input{width:100%;box-sizing:border-box;padding:11px 12px;border-radius:10px;font-size:16px;
 border:1px solid var(--line,rgba(127,127,127,.35));background:var(--bg);color:var(--text)}
.acctBox .acctGo{padding:12px;border-radius:12px;font-size:15px;font-weight:700;cursor:pointer;
 border:1.5px solid var(--accent);background:transparent;color:var(--accent)}
.acctBox .acctMsg{min-height:20px;font-size:13px;color:#e53935;margin-top:6px}
.acctBox .acctSep{display:flex;align-items:center;gap:10px;color:var(--muted);font-size:12px;margin:2px 0 10px}
.acctBox .acctSep:before,.acctBox .acctSep:after{content:"";flex:1;height:1px;background:var(--line,rgba(127,127,127,.25))}
.acctWho{font-size:12px;color:var(--muted);white-space:nowrap;max-width:28vw;overflow:hidden;text-overflow:ellipsis}
.paperTop .acctPB{font-size:13px;padding:6px 10px;border-radius:999px;border:1px solid rgba(255,255,255,.4);
 background:rgba(0,0,0,.35);color:#fff;cursor:pointer;margin-inline-end:6px}
"""

HTML = r"""<div class="acctGate hide" id="acctGate" role="dialog" aria-modal="true" aria-labelledby="acctTitle">
 <div class="acctBox"><h2 id="acctTitle"></h2><p id="acctAsk"></p>
  <button class="acctV" id="acctVisit" type="button"></button>
  <div class="acctSep" id="acctSep"></div>
  <form id="acctForm" autocomplete="on"><label for="acctUser" id="acctUserL"></label>
   <input id="acctUser" name="username" autocomplete="username" autocapitalize="none" spellcheck="false">
   <label for="acctPass" id="acctPassL"></label><input id="acctPass" name="password" type="password" autocomplete="current-password">
   <button class="acctGo" id="acctGo" type="submit"></button></form>
  <div class="acctMsg" id="acctMsg" role="alert"></div></div></div>"""

JS = r"""
const BD=window.BiruniDash;if(!BD)return;
const HPa=new URLSearchParams(location.hash.slice(1)),P=HPa.get('p')||'',KB=HPa.get('k')||'';
const MODE=(BD.transport==='preview')?(window.BIRUNI_ACCT_SIM?'sim':''):BD.transport;
if(!MODE)return;
const SKEY='biruni-acct:'+location.host+location.pathname+(KB?':'+KB.slice(0,12):'');
const T=k=>BD.t(k),$i=id=>document.getElementById(id);
function stored(){try{const v=JSON.parse(localStorage.getItem(SKEY)||'null');if(v&&v.card&&(+v.exp||0)*1000>Date.now())return v}catch(e){}return null}
function store(m){try{localStorage.setItem(SKEY,JSON.stringify({card:m.card,who:m.who||'',exp:m.exp||0,name:m.name||''}))}catch(e){}}
function forget(){try{localStorage.removeItem(SKEY)}catch(e){}}
function frag(card){const a=[];if(card)a.push('c='+card);if(KB)a.push('k='+KB);if(P)a.push('p='+P);a.push('l='+BD.lang());return '#'+a.join('&')}
const waiters=[];
function waitAcct(ms){return new Promise(res=>{const w={res:res,t:setTimeout(()=>{const i=waiters.indexOf(w);if(i>=0)waiters.splice(i,1);res({t:'acct_err',why:'timeout'})},ms||15000)};waiters.push(w)})}
function got(m){const w=waiters.shift();if(w){clearTimeout(w.t);w.res(m)}}
['acct_info','acct_card','acct_err','acct_out'].forEach(t=>BD.on(t,m=>{if(t==='acct_card'){store(m);
  if(MODE!=='lan'||live){try{history.replaceState(null,'',location.pathname+location.search+frag(m.card))}catch(e){}}}got(m)}));
let live=false;
/* one question to the PC: before a card on the LAN a request of its own; otherwise over the open channel */
async function ask(m){
 if(MODE==='lan'&&!live){try{const base=location.pathname.endsWith('/')?location.pathname:location.pathname+'/';
   const r=await fetch(base+'m',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({s:null,m:m})});
   const j=await r.json();const out=j.out||[];if(j.s)fetch(base+'bye',{method:'POST',body:JSON.stringify({s:j.s})}).catch(()=>{});
   const c=out.find(x=>x&&/^acct_/.test(x.t));if(c&&c.t==='acct_card')store(c);return c||{t:'acct_err',why:'failed'}}
  catch(e){return {t:'acct_err',why:'failed'}}}
 const p=waitAcct();BD.send(m);return p}
function errText(m){const w=m.why||'';if(w==='bad login')return T('acct_bad');if(w==='locked')return T('acct_locked').replace('{n}',String(Math.max(1,Math.ceil((m.wait||300)/60))));
 if(w==='slow down')return T('acct_slow');if(w==='disabled')return T('acct_disabled');if(w==='no visitors')return T('acct_novisit');
 if(w==='no dashboard')return T('acct_nodash');return T('acct_fail')}
function done(m){if(m.t!=='acct_card'){$i('acctMsg').textContent=errText(m);return false}
 $i('acctGate').classList.add('hide');if(MODE==='lan'&&!live){location.hash=frag(m.card);location.reload()}else{setWho()}return true}
let info=null;
function gate(i){info=i||{};const g=$i('acctGate');$i('acctTitle').textContent=(info.title||T('acct_welcome'));$i('acctAsk').textContent=T('acct_ask');
 $i('acctVisit').textContent=T('acct_visitor');$i('acctVisit').style.display=info.visitor===false?'none':'';$i('acctSep').textContent=info.visitor===false?'':T('acct_signin');
 $i('acctUserL').textContent=T('acct_user');$i('acctPassL').textContent=T('acct_pass');$i('acctGo').textContent=T('acct_go');
 $i('acctForm').style.display=info.users===false&&info.visitor!==false?'none':'';$i('acctMsg').textContent=info.visitor===false&&info.users===false?T('acct_nousers'):'';
 g.classList.remove('hide');const w=$i('wait');if(w)w.classList.add('hide');const b=$i('banner');if(b)b.classList.add('hide')}
function wire(){$i('acctVisit').onclick=async()=>{$i('acctMsg').textContent=T('acct_wait');done(await ask({t:'acct_visitor',paper:P}))};
 $i('acctForm').onsubmit=async e=>{e.preventDefault();$i('acctMsg').textContent=T('acct_wait');
  done(await ask({t:'acct_login',paper:P,user:$i('acctUser').value,password:$i('acctPass').value}))}}
/* the header and the paper view: who is in, sign in / sign out */
let hb=null,pb=null;
function who(){const s=stored();return s?s:null}
function setWho(){const s=who(),user=s&&/^user:/.test(s.who||'');const lab=user?T('acct_signout'):T('acct_signin');
 if(!hb){hb=BD.button('acctBtn',lab,'M15 3h4a2 2 0 0 1 2 2v14a2 2 0 0 1-2 2h-4M10 17l5-5-5-5M15 12H3',()=>act())}
 hb.title=lab+(s?' — '+(user?T('acct_as').replace('{name}',s.name||''):T('acct_asvisitor')):'');hb.setAttribute('aria-label',hb.title);
 const top=document.querySelector('#paperView .paperTop');if(top&&!pb){pb=document.createElement('button');pb.type='button';pb.className='acctPB';pb.onclick=()=>act();
  const x=$i('paperClose');top.insertBefore(pb,x?x.nextSibling:null)}if(pb)pb.textContent=user?(s.name||T('acct_signout'))+' · '+T('acct_signout'):T('acct_signin')}
async function act(){const s=who();if(s&&/^user:/.test(s.who||'')){try{await ask({t:'acct_logout'})}catch(e){}forget();
  if(MODE==='sim'){gate(info||{});return}location.hash=frag(null);location.reload();return}
 gate(Object.assign({},info||{},{visitor:false,users:true}))}
let opened=false;
BD.onBuild(()=>{live=true;setWho();if(P&&!opened&&BD.paper&&BD.paper.open){opened=true;setTimeout(()=>{try{BD.paper.open(false)}catch(e){}},250)}});
wire();
/* the entry: a paper's QR (#p=…) or a link, with no card yet */
const hasCard=(typeof CARD!=='undefined'&&CARD)||HPa.get('s');
/* in the simulated page (showcase, tests) the answers come from a script that starts after this one */
const whenReady=fn=>{if(typeof BD.onSend==='function')fn();else setTimeout(()=>whenReady(fn),40)};
if(MODE==='sim'){const s=stored();if(P||s)whenReady(()=>{if(s)BD.send({t:'acct_resume',card:s.card,who:s.who});
  else ask({t:'acct_info',paper:P}).then(m=>gate(m&&m.t==='acct_info'?m:{}))})}
else if(!hasCard&&P){const s=stored();if(s){location.hash=frag(s.card);location.reload()}
 else if(MODE==='lan'){ask({t:'acct_info',paper:P}).then(m=>gate(m&&m.t==='acct_info'?m:{}))}
 else if(MODE==='bridge'&&typeof TR!=='undefined'){const orig=send;let first=true;
  send=function(o){if(first&&o&&o.t==='hello'&&!o.card){first=false;o={t:'acct_info',paper:P}}return orig(o)};
  const p=waitAcct(30000);TR.connect();p.then(m=>gate(m&&m.t==='acct_info'?m:{}))}}
BD.accounts={stored:stored,forget:forget,ask:ask,gate:gate};
"""
