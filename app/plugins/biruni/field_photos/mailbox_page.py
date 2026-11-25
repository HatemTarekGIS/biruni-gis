"""Field photos through the organization's Cloudflare R2 cloud mailbox (no-loss P6): the phone's side.

Added to the phone page by `bridge.phone_page()` right after `drive_page.drive_js()`.
- Receives and verifies the signed `BRMB1` descriptor (`mbTake(mb)`) from `hello` or `setGuide`,
  keeping it in `biruni-mb-<ROOM>` only when `ID.pin` is set and all fields match.
- Uploads `.bfp` files (reusing `n` from any existing Drive record in store `d` or allocating `dnext()`,
  keeping `#m<id>` in store `d` so retries upload identical bytes) via `POST /mb/sign` + `PUT /mb/o/...`.
- Marks `r2=1` on the item in store `q`, advancing state 0 -> state 1 (`c=1`), never `c>=2`.
- Polls signed `BRCP1` receipts (`GET /mb/rc/<tag>/<DEVID>.json`) with nonce `mb.<DEVID>` and applies
  `copyRows` without calling `noteTime`.
- Never sends a DELETE request: an item leaves the phone only on a signed receipt with `c>=2` and matching `h8`.
"""
from . import bridge

MAILBOX_JS = r"""
/* ---- organization cloud mailbox on Cloudflare R2 (no-loss P6): signed BRMB1 descriptor, .bfp upload, BRCP1 receipts ---- */
const MBX={v:null,room:'',busy:false,again:false,until:0,fails:0,dead:false,tm:0,skip:new Set(),BASE:'__MB_BASE__'};
function mbBase(h){return MBX.BASE.replace('{h}',String(h||'').trim().toLowerCase()).replace(/\/+$/,'')}
function mbGet(){if(MBX.room!==ROOM){MBX.room=ROOM;MBX.v=null;
  if(!ID.pin)return null;
  try{const c=JSON.parse(kept('biruni-mb-'+ROOM)||'null');
   if(c&&c.d&&c.d.v===1&&c.d.room===ROOM&&typeof c.d.seq==='number')MBX.v=c}catch(e){}}
 return ID.pin?MBX.v:null}
function mbBad(){if(typeof SIG==='object'&&SIG)SIG.refused=(SIG.refused||0)+1;return false}
async function mbTake(mb){try{
 if(!ID.pin||!ROOM||!KEYB)return false;
 if(!mb||typeof mb.l!=='string'||typeof mb.ip!=='string'||typeof mb.is!=='string'||mb.l.length>2000)return false;
 const text='BRMB1|'+hhex(await crypto.subtle.digest('SHA-256',enc.encode(mb.l)));
 if(typeof IDOK!=='function'||!await IDOK(mb.ip,mb.is,text))return mbBad();
 let d;try{d=JSON.parse(mb.l)}catch(e){return mbBad()}
 if(!d||d.v!==1||d.room!==ROOM||typeof d.seq!=='number'||!Number.isInteger(d.seq))return mbBad();
 const cur=mbGet();
 if(cur&&cur.d&&typeof cur.d.seq==='number'&&d.seq<cur.d.seq)return mbBad();
 const tag=await BFP.tag(KEYB);
 if(d.tag!==tag)return mbBad();
 if(typeof d.h!=='string'||(d.h!==''&&!_ORG_RE.test(d.h)))return mbBad();
 if(typeof d.ut!=='string'||!/^[A-Za-z0-9_-]{43}$/.test(d.ut))return mbBad();
 const v={l:mb.l,ip:mb.ip,is:mb.is,d:d};
 keep('biruni-mb-'+ROOM,JSON.stringify(v));
 MBX.room=ROOM;MBX.v=v;MBX.dead=false;MBX.fails=0;MBX.until=0;
 clearTimeout(MBX.tm);MBX.tm=0;
 if(d.h)mbPump();
 return true}catch(e){return mbBad()}}
function mbActive(){const v=mbGet();return (v&&v.d&&v.d.h&&_ORG_RE.test(v.d.h)&&!MBX.dead)?v.d:null}
function mbFits(it){if(!it||!it.meta||!it.meta.sealed||!it.blob||it.blob.size>DMAX||it.carried||it.r2||MBX.skip.has(it.id))return false;
 const st=typeof itemSt==='function'?itemSt(it):(typeof it.st==='number'?it.st:0);
 if(st!==0&&st!==1&&st!==2)return false;
 if(st===0&&typeof lineOpen==='function'&&lineOpen())return false;
 return true}
function mbLater(ms){clearTimeout(MBX.tm);
 MBX.tm=setTimeout(()=>{MBX.tm=0;if(MBX.busy){mbLater(250);return}MBX.until=0;mbPump()},ms)}
async function mbMark(id){const x=typeof getItem==='function'?await getItem(id):null;if(!x)return null;
 x.r2=1;
 if(db){try{await tx('readwrite',s=>s.put(x))}catch(e){}}
 const m=mem.find(y=>y.id===id);if(m)m.r2=1;
 const st=typeof itemSt==='function'?itemSt(x):(typeof x.st==='number'?x.st:0);
 if(st===0&&typeof setState==='function')return await setState(id,1,1);
 return x}
async function mbUpload(it,d){const tag=await BFP.tag(KEYB);if(d.tag!==tag)return false;
 const k=await dkey(),mid='#m'+it.id;let rec=await dget(mid);
 if(rec&&rec.room===tag&&rec.st==='done')return true;
 if(!rec||rec.room!==tag||!rec.head){
  const drec=await dget(it.id);
  const n=(drec&&drec.room===tag&&Number.isInteger(drec.n)&&drec.n>0)?drec.n:dnext();
  const body=new Uint8Array(await it.blob.arrayBuffer());
  const h={room:tag,dev:DEVID,kid:await BFP.kid(it.pk||PCKEY),id:it.id,n:n,t:+it.meta.t||Date.now(),pub:k.pub,
   cert:await BFP.cert(KEYB,SURVKEY,k.pub,DEVID)};
  const f=await BFP.pack(h,body,k.kp.privateKey);
  rec={id:mid,item:it.id,room:tag,n:n,name:f.name,head:f.head,size:f.bytes.length,fsha:await BFP.sha(f.bytes),st:'q',tries:0,at:Date.now()};
  await dput(rec)}
 rec.tries=(rec.tries||0)+1;await dput(rec);
 const base=mbBase(d.h);
 let sr;
 try{const ac=new AbortController(),tm=setTimeout(()=>ac.abort(),30000);
  try{sr=await fetch(base+'/mb/sign',{method:'POST',
   headers:{'Authorization':'Bearer '+d.ut,'Content-Type':'application/json'},
   body:JSON.stringify({room:tag,dev:DEVID,name:rec.name,size:rec.size,sha:rec.fsha}),
   signal:ac.signal})}finally{clearTimeout(tm)}}
 catch(e){const err=new Error('mb-net');err.net=true;throw err}
 if(sr.status!==200){const err=new Error('mb-sign '+sr.status);err.status=sr.status;err.ra=sr.headers.get('Retry-After');throw err}
 let sj;try{sj=await sr.json()}catch(e){throw new Error('mb-sign-json')}
 const path=sj&&typeof sj.url==='string'&&sj.url.startsWith('/mb/o/')?sj.url:'';
 if(!path)throw new Error('mb-sign-url');
 const payload=new Blob([rec.head,it.blob],{type:'application/octet-stream'});
 let pr;
 try{const ac=new AbortController(),tm=setTimeout(()=>ac.abort(),180000);
  try{pr=await fetch(base+path,{method:'PUT',
   headers:{'Content-Type':'application/octet-stream'},
   body:payload,signal:ac.signal})}finally{clearTimeout(tm)}}
 catch(e){const err=new Error('mb-put-net');err.net=true;throw err}
 if(pr.status===201||pr.status===200){rec.st='done';rec.head=null;rec.at=Date.now();await dput(rec);return true}
 const err=new Error('mb-put '+pr.status);err.status=pr.status;err.ra=pr.headers.get('Retry-After');throw err}
async function mbPump(){const d=mbActive();if(!d)return;
 if(MBX.busy){MBX.again=true;return}
 if(Date.now()<MBX.until)return;
 MBX.busy=true;
 try{
  if(!SKEW_OK){
   try{const ac=new AbortController(),tm=setTimeout(()=>ac.abort(),10000);
    try{const tr=await fetch(mbBase(d.h)+'/t',{cache:'no-store',signal:ac.signal});
     if(tr.status===200){const tj=await tr.json();if(tj&&typeof tj.now==='number')noteTime(tj.now)}}
    finally{clearTimeout(tm)}}catch(e){}}
  const items=(await all()).filter(mbFits).sort((a,b)=>(a.meta.t||0)-(b.meta.t||0));
  for(const it of items){
   const curD=mbActive();if(!curD||Date.now()<MBX.until)break;
   const fresh=typeof getItem==='function'?await getItem(it.id):it;
   if(!fresh||!mbFits(fresh))continue;
   try{const ok=await mbUpload(fresh,curD);
    if(ok){await mbMark(fresh.id);MBX.fails=0;MBX.until=0}}
   catch(e){const st=e&&e.status;
    if(st===409){await ddrop('#m'+fresh.id);MBX.again=true;break}
    else if(st===401||st===403){MBX.dead=true;break}
    else if(st===404||st===501){MBX.until=Date.now()+3600000;mbLater(3600000);break}
    else if(st===413){MBX.skip.add(fresh.id);continue}
    else{MBX.fails++;const ms=ddelay(MBX.fails,e&&e.ra);MBX.until=Date.now()+ms;mbLater(ms);break}}}
 }catch(e){}finally{MBX.busy=false;if(MBX.again){MBX.again=false;setTimeout(mbPump,50)}}}
async function mbReceipts(){const d=mbActive();if(!d||!ID.pin)return;
 const waiting=(await all()).filter(x=>{const s=typeof itemSt==='function'?itemSt(x):(x.st||0);return s===1||s===2});
 if(!waiting.length)return;
 const tag=await BFP.tag(KEYB);if(d.tag!==tag)return;
 let r;
 try{const ac=new AbortController(),tm=setTimeout(()=>ac.abort(),15000);
  try{r=await fetch(mbBase(d.h)+'/mb/rc/'+tag+'/'+encodeURIComponent(DEVID)+'.json',
   {headers:{'Authorization':'Bearer '+d.ut},cache:'no-store',signal:ac.signal})}
  finally{clearTimeout(tm)}}catch(e){return}
 if(r.status!==200)return;
 let m;try{m=await r.json()}catch(e){return}
 if(!m||m.v!==1||m.room!==tag||m.dev!==DEVID||m.nonce!=='mb.'+DEVID||!Array.isArray(m.items)||m.items.length>1000)return;
 const cs=m.items.map(x=>Array.isArray(x)?(x[0]+':'+x[1]+':'+(x[2]||'')):'').join(',');
 if(!await IDOK(m.ip,m.is,'BRCP1|'+m.nonce+'|'+(m.rt||0)+'|'+cs)){SIG.refused=(SIG.refused||0)+1;return}
 await copyRows(m.items,null)}
function mbStart(){mbGet();
 setInterval(mbPump,20000);setInterval(mbReceipts,120000);
 window.addEventListener('online',()=>{MBX.fails=0;if(MBX.until<Date.now()+65000)MBX.until=0;mbPump();mbReceipts()});
 setTimeout(mbPump,200)}
"""


def mailbox_js():
    return MAILBOX_JS.replace("__MB_BASE__", bridge.MB)
