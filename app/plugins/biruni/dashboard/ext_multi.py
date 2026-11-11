"""«Dashboard of dashboards» (page side): a supervisor sees several dashboards - from several PCs, each through
its own card - in one view.

The view is any dashboard page opened with ``#m=<b64u(JSON)>`` after its address:
``{"t": title, "s": [{"n": name, "u": the dashboard's own link (with its card)}], "map": {group: [titles]}}``.
Each source runs in its own frame - its own page, its own card, its own connection (LAN or from anywhere), its
own seals; nothing of it passes through the view's computer. A source page opened with ``#…&mx=<i>`` inside the
view tells the view (postMessage) what it shows: its numbers, its lists and its map. The view adds them up:

* a summary row: numbers with the same title (or grouped by "map") summed, gauges and percentages averaged,
  each tile listing what every dashboard says;
* one map of all sites when they lie in one area, coloured by dashboard;
* every dashboard in its own section, collapsible, with its live state.

A dashboard is added by pasting its link or scanning its QR code (when the browser can read QR codes); the
list lives in the address (bookmark it, send it), nowhere else.
"""
from . import multi  # noqa: F401 - the PC's action

CSS = r"""
html.mxEmbed header.top,html.mxEmbed .foot{display:none}
html.mxEmbed body{background:transparent}
html.mxHost #grid,html.mxHost #wait,html.mxHost #banner,html.mxHost #conn,html.mxHost #fresh{display:none!important}
.mxWrap{max-width:1600px;margin:0 auto;padding:12px 16px 40px;display:flex;flex-direction:column;gap:16px}
.mxBar{display:flex;gap:8px;align-items:center;flex-wrap:wrap}
.mxBar .mxCount{color:var(--muted);font-size:13px;margin-inline-end:auto}
.mxBtn{border:1px solid var(--line,#ccc3);background:var(--surface);color:var(--text);border-radius:10px;padding:7px 12px;
 font:inherit;font-size:13px;cursor:pointer}
.mxBtn:hover{border-color:var(--accent)}
.mxSum{display:grid;grid-template-columns:repeat(auto-fill,minmax(220px,1fr));gap:12px}
.mxTile{background:var(--surface);border:1px solid var(--line,#ccc3);border-radius:14px;padding:12px 14px;min-width:0}
.mxTile h3{margin:0 0 4px;font-size:13px;font-weight:650;color:var(--muted);unicode-bidi:plaintext}
.mxTile .mxVal{font-size:30px;font-weight:750;letter-spacing:-.01em}
.mxTile .mxVal small{font-size:14px;color:var(--muted);font-weight:600;margin-inline-start:4px}
.mxTile .mxHow{font-size:11.5px;color:var(--muted)}
.mxTile ul{list-style:none;margin:6px 0 0;padding:0;font-size:12px;color:var(--muted)}
.mxTile li{display:flex;justify-content:space-between;gap:8px}.mxTile li i{width:8px;height:8px;border-radius:50%;
 display:inline-block;margin-inline-end:6px;vertical-align:middle}
.mxMapBox{background:var(--surface);border:1px solid var(--line,#ccc3);border-radius:14px;padding:12px 14px}
.mxMapBox h3{margin:0 0 8px;font-size:13px;color:var(--muted)}
.mxMapBox svg{width:100%;height:min(56vh,520px);display:block;border-radius:10px;background:var(--bg2,#0001)}
.mxLeg{display:flex;flex-wrap:wrap;gap:12px;margin-top:8px;font-size:12px;color:var(--muted)}
.mxLeg span{display:inline-flex;align-items:center;gap:6px}.mxLeg i{width:12px;height:12px;border-radius:3px}
.mxNote{color:var(--muted);font-size:13px}
.mxSrc{background:var(--surface);border:1px solid var(--line,#ccc3);border-radius:14px;overflow:hidden}
.mxSrc>header{display:flex;align-items:center;gap:10px;padding:10px 14px;border-bottom:1px solid var(--line,#ccc3)}
.mxSrc>header h2{margin:0;font-size:15px;flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;
 unicode-bidi:plaintext}
.mxSrc>header .mxSw{width:12px;height:12px;border-radius:3px;flex:none}
.mxDot{width:9px;height:9px;border-radius:50%;background:#9e9e9e;flex:none}.mxDot.on{background:#2e9d57}
.mxDot.off{background:#d84315}
.mxSrc iframe{display:block;width:100%;height:640px;border:0;background:transparent}
.mxSrc.closed iframe{display:none}
.mxForm{display:flex;flex-direction:column;gap:10px}
.mxForm input{font:inherit;padding:9px 10px;border-radius:10px;border:1px solid var(--line,#ccc6);background:var(--bg,#fff);
 color:var(--text)}
.mxForm video{width:100%;max-height:50vh;border-radius:10px;background:#000}
"""

JS = r"""
const BD=window.BiruniDash;if(!BD)return;
const HP=new URLSearchParams(location.hash.slice(1));
const MX=HP.get('mx'),M=HP.get('m');
const enc=s=>{const u=new TextEncoder().encode(s);let b='';for(const c of u)b+=String.fromCharCode(c);return btoa(b).replace(/\+/g,'-').replace(/\//g,'_').replace(/=+$/,'')};
const dec=s=>{s=String(s).replace(/-/g,'+').replace(/_/g,'/');while(s.length%4)s+='=';const b=atob(s),u=new Uint8Array(b.length);for(let i=0;i<b.length;i++)u[i]=b.charCodeAt(i);return new TextDecoder().decode(u)};
const T=k=>BD.t(k);
/* ---- a source inside the view: tell the view what this dashboard shows ---- */
if(MX!==null&&window.parent!==window){
 document.documentElement.classList.add('mxEmbed');
 const idx=+MX;const post=o=>{o.bdm=1;o.i=idx;try{window.parent.postMessage(o,'*')}catch(e){}};
 const KEEP={kpi:1,gauge:1,bar:1,pie:1,leaderboard:1,progress:1,map:1};
 BD.onBuild(()=>{const S=BD.state,d=S.def||{};post({ev:'dash',title:d.title||'',pc:S.pc||'',
  widgets:(d.widgets||[]).filter(w=>KEEP[w.type]).map(w=>({id:w.id,type:w.type,title:w.title||'',unit:(w.style||{}).unit||'',
   decimals:(w.style||{}).decimals}))})});
 BD.onData(m=>{const S=BD.state,out={},ws=(m&&m.widgets)||{};
  for(const w of (S.def&&S.def.widgets)||[]){if(!KEEP[w.type]||!(w.id in ws))continue;const d=S.data[w.id]||{};
   if(w.type==='kpi'||w.type==='gauge')out[w.id]={value:d.value,min:d.min,max:d.max,seal:d.seal||null,hidden:!!d.hidden};
   else if(w.type==='map')out[w.id]={layers:d.layers||null,geojson:d.geojson||null,bbox:d.bbox||null};
   else out[w.id]={items:d.items||[]}}
  post({ev:'data',widgets:out})});
 const tellH=()=>post({ev:'h',h:Math.ceil(document.documentElement.scrollHeight)});
 try{new ResizeObserver(tellH).observe(document.body)}catch(e){setInterval(tellH,2000)}
 setTimeout(tellH,500);
 return}
/* ---- the view ---- */
if(M===null)return;
let cfg=null;try{cfg=JSON.parse(dec(M))}catch(e){cfg=null}
if(!cfg||!Array.isArray(cfg.s))cfg={t:'',s:[]};
const COLORS=['#1e88e5','#e53935','#43a047','#fb8c00','#8e24aa','#00897b','#6d4c41','#3949ab','#c0ca33','#d81b60'];
document.documentElement.classList.add('mxHost');
const $=id=>document.getElementById(id);
const el=(tag,cls,text,parent)=>{const n=document.createElement(tag);if(cls)n.className=cls;if(text!=null)n.textContent=text;if(parent)parent.appendChild(n);return n};
const svgEl=(tag,attrs,parent)=>{const n=document.createElementNS('http://www.w3.org/2000/svg',tag);for(const k in attrs||{})n.setAttribute(k,attrs[k]);if(parent)parent.appendChild(n);return n};
const fmt=(v,dec)=>{if(v==null||!isFinite(v))return '—';const d=dec!=null?dec:(Math.abs(v)<10&&v%1?2:(v%1?1:0));
 try{return Number(v).toLocaleString(BD.lang().replace('_','-'),{maximumFractionDigits:d,minimumFractionDigits:0})}catch(e){return String(Math.round(v*100)/100)}};
const src=[];
let wrap,sumBox,mapBox,srcBox,countEl,raf=0;
function saveHash(){const o={t:cfg.t||'',s:cfg.s.map(s=>({n:s.n||'',u:s.u}))};if(cfg.map)o.map=cfg.map;
 const hp=new URLSearchParams(location.hash.slice(1));hp.set('m',enc(JSON.stringify(o)));
 try{history.replaceState(null,'','#'+hp.toString())}catch(e){location.hash=hp.toString()}}
function frameUrl(u,i){const k=u.indexOf('#');return (k<0?u+'#':u+'&')+'mx='+i}
function build(){
 $('title').textContent=cfg.t||T('mx_title');
 document.title=(cfg.t||T('mx_title'))+' - Biruni GIS';
 if(wrap)wrap.remove();src.length=0;
 wrap=el('main','mxWrap');$('grid').after(wrap);
 const bar=el('div','mxBar',null,wrap);countEl=el('span','mxCount',null,bar);
 const add=el('button','mxBtn','+ '+T('mx_add'),bar);add.type='button';add.onclick=addSheet;
 sumBox=el('section','mxSum',null,wrap);sumBox.setAttribute('aria-live','polite');
 mapBox=el('section','mxMapBox',null,wrap);mapBox.hidden=true;
 srcBox=el('div','mxSrcs',null,wrap);
 if(!cfg.s.length)el('p','mxNote',T('mx_empty'),srcBox);
 cfg.s.forEach((s,i)=>{const sec=el('section','mxSrc',null,srcBox),hd=el('header',null,null,sec);
  const sw=el('i','mxSw',null,hd);sw.style.background=COLORS[i%COLORS.length];
  const dot=el('span','mxDot',null,hd);dot.title=T('mx_off');
  const h=el('h2',null,s.n||T('mx_source')+' '+(i+1),hd);
  const tg=el('button','mxBtn',T('mx_hide'),hd);tg.type='button';
  tg.onclick=()=>{sec.classList.toggle('closed');tg.textContent=sec.classList.contains('closed')?T('mx_show'):T('mx_hide')};
  const rm=el('button','mxBtn',T('mx_remove'),hd);rm.type='button';rm.onclick=()=>{cfg.s.splice(i,1);saveHash();build()};
  const fr=el('iframe',null,null,sec);fr.title=s.n||'';fr.setAttribute('loading','eager');fr.src=frameUrl(s.u,i);
  src.push({i:i,cfg:s,sec:sec,frame:fr,dot:dot,head:h,def:null,data:{},last:0})});
 countEl.textContent=T('mx_count').replace('{n}',cfg.s.length);
 render()}
window.addEventListener('message',e=>{const d=e.data;if(!d||d.bdm!==1)return;const s=src[d.i];
 if(!s||e.source!==s.frame.contentWindow)return;
 if(d.ev==='dash'){s.def=d;if(!s.cfg.n&&d.title)s.head.textContent=d.title}
 else if(d.ev==='data'){Object.assign(s.data,d.widgets||{});s.last=Date.now()}
 else if(d.ev==='h'){const h=Math.max(200,Math.min(+d.h||0,6000));s.frame.style.height=h+'px'}
 if(!raf)raf=requestAnimationFrame(()=>{raf=0;render()})});
function liveState(){let n=0;for(const s of src){const on=s.last&&Date.now()-s.last<60000;if(on)n++;
 s.dot.className='mxDot'+(on?' on':(s.last?' off':''));s.dot.title=on?T('mx_live'):T('mx_off')}
 if(countEl)countEl.textContent=T('mx_count').replace('{n}',cfg.s.length)+' · '+T('mx_nlive').replace('{n}',n)}
setInterval(liveState,2000);
const norm=t=>String(t||'').trim().toLowerCase().replace(/\s+/g,' ');
function groupOf(title){const n=norm(title);if(cfg.map)for(const g in cfg.map){if((cfg.map[g]||[]).some(t=>norm(t)===n))return g}return null}
function render(){
 // the summary: numbers by title (or by the explicit groups)
 const G={};
 for(const s of src){if(!s.def)continue;for(const w of s.def.widgets){if(w.type!=='kpi'&&w.type!=='gauge')continue;
  const d=s.data[w.id];if(!d||d.hidden||d.value==null||!isFinite(+d.value))continue;
  const g=groupOf(w.title),key=g?'g:'+g:'t:'+norm(w.title);
  // a gauge of a share (0…1) reads as a percentage
  const ratio=w.type==='gauge'&&d.max!=null&&+d.max<=1&&!w.unit;
  const o=G[key]||(G[key]={title:g||w.title,unit:ratio?'%':(w.unit||''),avg:w.type==='gauge'||w.unit==='%',
   dec:ratio?1:w.decimals,rows:[]});
  o.rows.push({s:s,v:ratio?100*d.value:+d.value,seal:d.seal})}}
 let groups=Object.values(G);const shared=groups.filter(g=>g.rows.length>1);if(shared.length)groups=shared;
 sumBox.textContent='';
 for(const g of groups){const t=el('div','mxTile',null,sumBox);el('h3',null,g.title,t);
  const tot=g.rows.reduce((a,r)=>a+r.v,0),val=g.avg?tot/g.rows.length:tot;
  const v=el('div','mxVal',fmt(val,g.dec),t);if(g.unit)el('small',null,g.unit,v);
  el('div','mxHow',(g.avg?T('mx_avg'):T('mx_sum'))+' · '+T('mx_from').replace('{n}',g.rows.length),t);
  const ul=el('ul',null,null,t);
  for(const r of g.rows){const li=el('li',null,null,ul),a=el('span',null,null,li),dot=el('i',null,null,a);
   dot.style.background=COLORS[r.s.i%COLORS.length];a.append(r.s.head.textContent);const b=el('b',null,fmt(r.v,g.dec),li);
   if(r.seal)b.title=r.seal}}
 sumBox.hidden=!groups.length;
 drawMap()}
function featuresOf(s){const out=[];for(const id in s.data){const d=s.data[id];if(!d)continue;
  const fcs=d.layers?d.layers.map(l=>l&&l.geojson):[d.geojson];
  for(const fc of fcs){if(fc&&Array.isArray(fc.features))for(const f of fc.features){if(f&&f.geometry)out.push(f);if(out.length>4000)return out}}}
 return out}
function eachXY(g,fn){const t=g.type,c=g.coordinates;if(!c)return;
 if(t==='Point')fn(c);else if(t==='MultiPoint'||t==='LineString')c.forEach(fn);
 else if(t==='MultiLineString'||t==='Polygon')c.forEach(r=>r.forEach(fn));else if(t==='MultiPolygon')c.forEach(p=>p.forEach(r=>r.forEach(fn)))}
const mx=x=>x*Math.PI/180,my=y=>Math.log(Math.tan(Math.PI/4+Math.max(-85,Math.min(85,y))*Math.PI/360));
function drawMap(){
 const per=src.map(s=>({s:s,f:featuresOf(s)})).filter(p=>p.f.length);
 mapBox.textContent='';if(!per.length){mapBox.hidden=true;return}
 let x0=1e9,y0=1e9,x1=-1e9,y1=-1e9;
 for(const p of per)for(const f of p.f)eachXY(f.geometry,c=>{x0=Math.min(x0,c[0]);x1=Math.max(x1,c[0]);y0=Math.min(y0,c[1]);y1=Math.max(y1,c[1])});
 mapBox.hidden=false;el('h3',null,T('mx_map'),mapBox);
 if(per.length<2&&src.length>1){}
 if(x1-x0>4||y1-y0>4){el('p','mxNote',T('mx_far'),mapBox);return}
 const X0=mx(x0),X1=mx(x1),Y0=my(y0),Y1=my(y1),W=1000,H=560,pad=20;
 const k=Math.min((W-2*pad)/Math.max(X1-X0,1e-9),(H-2*pad)/Math.max(Y1-Y0,1e-9));
 const ox=(W-(X1-X0)*k)/2,oy=(H-(Y1-Y0)*k)/2;
 const P=c=>((mx(c[0])-X0)*k+ox).toFixed(1)+' '+(H-((my(c[1])-Y0)*k+oy)).toFixed(1);
 const svg=svgEl('svg',{viewBox:'0 0 '+W+' '+H,role:'img','aria-label':T('mx_map')},mapBox);
 for(const p of per){const col=COLORS[p.s.i%COLORS.length],g=svgEl('g',{},svg);
  for(const f of p.f){const t=f.geometry.type,c=f.geometry.coordinates;
   if(t==='Polygon'||t==='MultiPolygon'){const polys=t==='Polygon'?[c]:c;let d='';for(const pl of polys)for(const r of pl)d+='M'+r.map(P).join('L')+'Z';
    svgEl('path',{d:d,fill:col,'fill-opacity':.32,stroke:col,'stroke-width':1},g)}
   else if(t==='LineString'||t==='MultiLineString'){const ls=t==='LineString'?[c]:c;svgEl('path',{d:ls.map(l=>'M'+l.map(P).join('L')).join(''),fill:'none',stroke:col,'stroke-width':2},g)}
   else{const pts=t==='Point'?[c]:c;for(const q of pts){const xy=P(q).split(' ');svgEl('circle',{cx:xy[0],cy:xy[1],r:3.5,fill:col},g)}}}}
 const lg=el('div','mxLeg',null,mapBox);
 for(const p of per){const s=el('span',null,null,lg),i=el('i',null,null,s);i.style.background=COLORS[p.s.i%COLORS.length];s.append(p.s.head.textContent)}}
function addSheet(){
 const f=el('form','mxForm');const inp=el('input',null,null,f);inp.type='url';inp.placeholder=T('mx_paste');inp.required=true;
 const nm=el('input',null,null,f);nm.placeholder=T('mx_name');
 const err=el('p','mxNote',null,f);
 let stream=null,video=null,timer=0;
 const stop=()=>{clearInterval(timer);if(stream)stream.getTracks().forEach(t=>t.stop());stream=null;if(video)video.remove();video=null};
 if('BarcodeDetector' in window&&navigator.mediaDevices&&navigator.mediaDevices.getUserMedia){
  const sc=el('button','mxBtn',T('mx_scan'),f);sc.type='button';
  sc.onclick=async()=>{try{stream=await navigator.mediaDevices.getUserMedia({video:{facingMode:'environment'}});
   video=el('video',null,null,f);video.playsInline=true;video.muted=true;video.srcObject=stream;await video.play();
   const det=new BarcodeDetector({formats:['qr_code']});
   timer=setInterval(async()=>{try{const r=await det.detect(video);if(r&&r[0]&&r[0].rawValue){inp.value=r[0].rawValue;stop()}}catch(e){}},300)}
   catch(e){err.textContent=T('mx_noscan')}}}
 else el('p','mxNote',T('mx_noscan'),f);
 const ok=el('button','mxBtn',T('mx_add'),f);ok.type='submit';
 f.onsubmit=e=>{e.preventDefault();const u=inp.value.trim();
  if(!/^https:\/\/[^\s#]+\/d\/[^\s]*#.*\bc=/.test(u)&&!/^https:\/\/[^\s#]+#.*\bc=/.test(u)){err.textContent=T('mx_bad');return}
  stop();cfg.s.push({n:nm.value.trim(),u:u});saveHash();BD.close();build()};
 BD.sheet(T('mx_add'),f);
 const sheetClose=document.getElementById('sheetClose');if(sheetClose)sheetClose.addEventListener('click',stop,{once:true})}
build();
BD.multi={sources:()=>src.map(s=>({name:s.head.textContent,live:!!s.last,def:!!s.def})),config:()=>cfg,render:render};
"""

TEXTS = {
    "en": {"mx_nlive": "{n} live", "mx_title": "Dashboard of dashboards", "mx_count": "{n} dashboards", "mx_add": "Add a dashboard",
           "mx_paste": "Paste the dashboard's link", "mx_name": "A name for it (optional)",
           "mx_scan": "Scan its QR code", "mx_noscan": "This browser cannot read QR codes here; paste the link instead.",
           "mx_bad": "This is not a Biruni dashboard link with a card.", "mx_remove": "Remove", "mx_hide": "Hide",
           "mx_show": "Show", "mx_live": "Live", "mx_off": "Not answering", "mx_source": "Dashboard",
           "mx_sum": "Total", "mx_avg": "Average", "mx_from": "from {n} dashboards", "mx_map": "All sites on one map",
           "mx_far": "These dashboards are far apart; each map stays in its own section.",
           "mx_empty": "No dashboards yet. Add one with its link or its QR code."},
    "ar": {"mx_nlive": "{n} مباشرة", "mx_title": "لوحة اللوحات", "mx_count": "{n} لوحات", "mx_add": "إضافة لوحة",
           "mx_paste": "الصق رابط اللوحة", "mx_name": "اسم لها (اختياري)",
           "mx_scan": "امسح رمز QR الخاص بها", "mx_noscan": "لا يستطيع هذا المتصفح قراءة رموز QR هنا؛ الصق الرابط بدلًا من ذلك.",
           "mx_bad": "هذا ليس رابط لوحة من بيروني يحمل بطاقة.", "mx_remove": "إزالة", "mx_hide": "إخفاء",
           "mx_show": "إظهار", "mx_live": "مباشر", "mx_off": "لا تستجيب", "mx_source": "لوحة",
           "mx_sum": "المجموع", "mx_avg": "المتوسط", "mx_from": "من {n} لوحات", "mx_map": "كل المواقع على خريطة واحدة",
           "mx_far": "هذه اللوحات متباعدة؛ تبقى خريطة كل منها في قسمها.",
           "mx_empty": "لا توجد لوحات بعد. أضف لوحة برابطها أو برمز QR الخاص بها."},
    "es": {"mx_nlive": "{n} en vivo", "mx_title": "Panel de paneles", "mx_count": "{n} paneles", "mx_add": "Añadir un panel",
           "mx_paste": "Pega el enlace del panel", "mx_name": "Un nombre (opcional)",
           "mx_scan": "Escanear su código QR", "mx_noscan": "Este navegador no puede leer códigos QR aquí; pega el enlace.",
           "mx_bad": "Este no es un enlace de panel de Biruni con tarjeta.", "mx_remove": "Quitar", "mx_hide": "Ocultar",
           "mx_show": "Mostrar", "mx_live": "En vivo", "mx_off": "No responde", "mx_source": "Panel",
           "mx_sum": "Total", "mx_avg": "Promedio", "mx_from": "de {n} paneles", "mx_map": "Todos los sitios en un mapa",
           "mx_far": "Estos paneles están muy lejos entre sí; cada mapa queda en su sección.",
           "mx_empty": "Aún no hay paneles. Añade uno con su enlace o su código QR."},
    "fr": {"mx_nlive": "{n} en direct", "mx_title": "Tableau des tableaux de bord", "mx_count": "{n} tableaux de bord", "mx_add": "Ajouter un tableau de bord",
           "mx_paste": "Collez le lien du tableau de bord", "mx_name": "Un nom (facultatif)",
           "mx_scan": "Scanner son code QR", "mx_noscan": "Ce navigateur ne peut pas lire les codes QR ici ; collez le lien.",
           "mx_bad": "Ce n'est pas un lien de tableau de bord Biruni avec une carte d'accès.", "mx_remove": "Retirer",
           "mx_hide": "Masquer", "mx_show": "Afficher", "mx_live": "En direct", "mx_off": "Ne répond pas",
           "mx_source": "Tableau de bord", "mx_sum": "Total", "mx_avg": "Moyenne", "mx_from": "de {n} tableaux de bord",
           "mx_map": "Tous les sites sur une carte",
           "mx_far": "Ces tableaux de bord sont éloignés ; chaque carte reste dans sa section.",
           "mx_empty": "Aucun tableau de bord pour l'instant. Ajoutez-en un avec son lien ou son code QR."},
    "pt_BR": {"mx_nlive": "{n} ao vivo", "mx_title": "Painel de painéis", "mx_count": "{n} painéis", "mx_add": "Adicionar um painel",
              "mx_paste": "Cole o link do painel", "mx_name": "Um nome (opcional)",
              "mx_scan": "Ler o código QR", "mx_noscan": "Este navegador não lê códigos QR aqui; cole o link.",
              "mx_bad": "Este não é um link de painel do Biruni com cartão.", "mx_remove": "Remover", "mx_hide": "Ocultar",
              "mx_show": "Mostrar", "mx_live": "Ao vivo", "mx_off": "Sem resposta", "mx_source": "Painel",
              "mx_sum": "Total", "mx_avg": "Média", "mx_from": "de {n} painéis", "mx_map": "Todos os locais em um mapa",
              "mx_far": "Estes painéis estão distantes; cada mapa fica na sua seção.",
              "mx_empty": "Ainda não há painéis. Adicione um com o link ou o código QR."},
    "tr": {"mx_nlive": "{n} canlı", "mx_title": "Panolar panosu", "mx_count": "{n} pano", "mx_add": "Pano ekle",
           "mx_paste": "Panonun bağlantısını yapıştırın", "mx_name": "Bir ad (isteğe bağlı)",
           "mx_scan": "QR kodunu tara", "mx_noscan": "Bu tarayıcı burada QR kodu okuyamıyor; bağlantıyı yapıştırın.",
           "mx_bad": "Bu, kartlı bir Biruni pano bağlantısı değil.", "mx_remove": "Kaldır", "mx_hide": "Gizle",
           "mx_show": "Göster", "mx_live": "Canlı", "mx_off": "Yanıt vermiyor", "mx_source": "Pano",
           "mx_sum": "Toplam", "mx_avg": "Ortalama", "mx_from": "{n} panodan", "mx_map": "Tüm sahalar tek haritada",
           "mx_far": "Bu panolar birbirinden uzak; her harita kendi bölümünde kalır.",
           "mx_empty": "Henüz pano yok. Bağlantısı veya QR koduyla bir pano ekleyin."},
    "fa": {"mx_nlive": "{n} زنده", "mx_title": "داشبورد داشبوردها", "mx_count": "{n} داشبورد", "mx_add": "افزودن داشبورد",
           "mx_paste": "پیوند داشبورد را بچسبانید", "mx_name": "یک نام (اختیاری)",
           "mx_scan": "اسکن کد QR آن", "mx_noscan": "این مرورگر اینجا کد QR نمی‌خواند؛ پیوند را بچسبانید.",
           "mx_bad": "این پیوند داشبورد Biruni با کارت نیست.", "mx_remove": "حذف", "mx_hide": "پنهان",
           "mx_show": "نمایش", "mx_live": "زنده", "mx_off": "پاسخ نمی‌دهد", "mx_source": "داشبورد",
           "mx_sum": "جمع", "mx_avg": "میانگین", "mx_from": "از {n} داشبورد", "mx_map": "همهٔ محل‌ها روی یک نقشه",
           "mx_far": "این داشبوردها از هم دورند؛ نقشهٔ هرکدام در بخش خودش می‌ماند.",
           "mx_empty": "هنوز داشبوردی نیست. یکی را با پیوند یا کد QR آن اضافه کنید."},
}


def _collect(plugin, dock):
    return multi.collect_ui(plugin, dock)


ACTIONS = [("multi_link", "Dashboard of dashboards…", "layout-dashboard", _collect)]
