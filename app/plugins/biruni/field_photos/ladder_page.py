"""Phone-side store-and-forward + fallback ladder script (inserted before PHONE_JS)."""

LADDER_JS = r"""
/* ---- Biruni fallback ladder: store-and-forward positions + channel race ---- */
const LAD=(()=>{
 const CH=[
  {id:'direct',minGap:900,ready:()=>typeof lineOpen==='function'&&lineOpen(),
   send:s=>{lineText(JSON.stringify({t:'lp',s:s}));return true},okAt:0,fails:0,unacked:0,waitMs:0,until:0,lastSent:0},
  {id:'tunnel',minGap:1000,ready:()=>SIG.relays.some(r=>r.kind==='tunnel'),
   send:s=>{const r=SIG.relays.find(x=>x.kind==='tunnel');return r?r.post(s):false},
   okAt:0,fails:0,unacked:0,waitMs:0,until:0,lastSent:0},
  {id:'mqtt',minGap:2000,ready:()=>(SIG.kinds().mqtt||0)>0,
   send:s=>SIG.sendRaw(s,'mqtt'),okAt:0,fails:0,unacked:0,waitMs:0,until:0,lastSent:0},
  {id:'nostr',minGap:3000,ready:()=>(SIG.kinds().nostr||0)>0,
   send:s=>SIG.sendRaw(s,'nostr'),okAt:0,fails:0,unacked:0,waitMs:0,until:0,lastSent:0},
  {id:'ntfy',minGap:2000,ready:()=>(SIG.kinds().ntfy||0)>0,
   send:s=>SIG.sendRaw(s,'ntfy'),okAt:0,fails:0,unacked:0,waitMs:0,until:0,lastSent:0}
 ];
 const pad=n=>String(n).padStart(9,'0');
 const keyFor=q=>ROOM+':'+pad(q);
 function openPosDb(){return new Promise(r=>{
  if(typeof indexedDB==='undefined')return r(null);
  try{const q=indexedDB.open('biruni-pos',1);
   q.onupgradeneeded=()=>{if(!q.result.objectStoreNames.contains('p'))q.result.createObjectStore('p')};
   q.onsuccess=()=>r(q.result);q.onerror=()=>r(null)}catch(e){r(null)}})}
 const lad={
  epoch:'',seq:0,upto:0,cap:false,db:null,_items:[],_pushes:0,_waitSince:0,_lastProbe:0,_ticking:false,
  _timer:null,_ready:null,_resolveReady:null,
  init(){
   if(this._ready)return this._ready;
   let e=kept('biruni-lad-e-'+ROOM);if(!e){e=rid();keep('biruni-lad-e-'+ROOM,e)}this.epoch=e;
   this.seq=parseInt(kept('biruni-lad-q-'+ROOM)||'0',10)||0;
   this.upto=parseInt(kept('biruni-lad-u-'+ROOM)||'0',10)||0;
   if(typeof TUNH==='string'&&/^[a-z0-9-]{3,80}$/.test(TUNH)){this.cap=true;SIG.tunnel(TUNH)}
   this._ready=(async()=>{
    this.db=await openPosDb();
    if(this.db){try{
     const r=IDBKeyRange.bound(ROOM+':',ROOM+':\uffff');
     const all=await this._tx('readonly',s=>s.getAll(r))||[];
     this._items=all.filter(x=>x&&typeof x.q==='number').sort((a,b)=>a.q-b.q);
     if(this._items.length){const mx=this._items[this._items.length-1].q;
      if(mx>this.seq){this.seq=mx;keep('biruni-lad-q-'+ROOM,String(this.seq))}}
    }catch(ex){}}
    this.drawPill();
   })();
   if(!this._timer)this._timer=setInterval(()=>this.tick(),1000);
   return this._ready;
  },
  _tx(mode,fn){return new Promise((res,rej)=>{
   if(!this.db)return res(null);
   try{const t=this.db.transaction('p',mode),q=fn(t.objectStore('p'));
    t.oncomplete=()=>res(q&&q.result);t.onerror=()=>rej(t.error);t.onabort=()=>rej(t.error)}catch(e){rej(e)}})},
  onCap(m){if(m&&m.lad)this.cap=true;if(m&&m.tun)this.onTun(m.tun)},
  onTun(h){if(typeof h==='string'&&/^[a-z0-9-]{3,80}$/.test(h)){
   keep('biruni-tun-'+ROOM,h);this.cap=true;SIG.tunnel(h)}},
  get backlog(){return this._items.length},
  primary(now=Date.now()){return CH.find(c=>c.ready()&&c.okAt>0&&(now-c.okAt)<=10000)||null},
  viaName(){const p=this.primary();if(!p||p.id==='direct')return '';
   if(p.id==='tunnel')return T.chTunnel||'direct address';
   if(p.id==='own')return T.chOwn||'Biruni relay';
   return T.chRelay||'public relay'},
  statusText(){return !!this.viaName()},
  drawPill(){const wp=$('waitPill');if(!wp)return;const n=this._items.length;
   if(!n){if(GPSMODE)wp.classList.add('hide');return}
   const stalled=!!(this._waitSince&&(Date.now()-this._waitSince)>=3000);
   const active=lineOpen()||(!!this.primary()&&!stalled);
   const tpl=active?(T.ladCatch||'Sending {n} earlier positions…'):
    (T.ladKeep||'{n} positions kept on the phone - they are sent when the computer is in reach.');
   wp.textContent=tpl.replace('{n}',n);wp.classList.remove('hide')},
  async _prune(){
   const cut=Date.now()-48*3600*1000;
   const drop=this._items.filter(x=>(x.t||0)<cut);
   this._items=this._items.filter(x=>(x.t||0)>=cut);
   if(this._items.length>50000){const extra=this._items.splice(0,this._items.length-50000);drop.push(...extra)}
   if(this.db&&drop.length){try{await this._tx('readwrite',s=>{for(const x of drop)s.delete(keyFor(x.q))})}catch(e){}}
  },
  async push(p){
   await this.init();
   this.seq++;keep('biruni-lad-q-'+ROOM,String(this.seq));
   const item={q:this.seq,t:p.t||Date.now(),p:p};
   if(GPSMODE){
    this._items.push(item);
    if(this.db){
     try{await this._tx('readwrite',s=>s.put(item,keyFor(item.q)))}catch(e){}
     this._pushes++;if(this._pushes%100===0)await this._prune();
    }else if(this._items.length>600)this._items.splice(0,this._items.length-600);
   }else{this._items=[item]}
   this.drawPill();this.tick();
  },
  _markSent(c,now){
   c.lastSent=now;c.unacked=(c.unacked||0)+1;
   if(c.unacked>=3){c.waitMs=c.waitMs?Math.min(60000,c.waitMs*2):2000;c.until=now+c.waitMs}
   else c.until=now+c.minGap;
  },
  async tick(){
   if(this._ticking||!this.cap||!BK)return;
   if(!this._items.length){this._waitSince=0;this.drawPill();return}
   this._ticking=true;
   try{
    const now=Date.now(),items=this._items;
    const pick=items.length<=60?items:items.slice(0,50).concat(items.slice(-10));
    const env={t:'lpos',v:1,d:DEVID,e:this.epoch,lo:items[0].q,bl:items.length,
     b:pick.map(x=>[x.q,x.p]),n:rid(),ts:now};
    const primary=this.primary(now),stalled=!!(primary&&this._waitSince&&(now-this._waitSince>=3000));
    let sealed=null;
    const pack=async()=>{if(!sealed)sealed=await seal(env,true);return sealed};
    if(!primary||stalled){
     const targets=CH.filter(c=>c.ready()&&now>=c.until);
     if(targets.length){
      const s=await pack();
      for(const c of targets){this._markSent(c,now);Promise.resolve(c.send(s)).catch(()=>{})}
      if(!this._waitSince)this._waitSince=now;
     }
    }else{
     if(now>=primary.until){
      const s=await pack();this._markSent(primary,now);
      Promise.resolve(primary.send(s)).catch(()=>{});
      if(!this._waitSince)this._waitSince=now;
     }
     const pi=CH.indexOf(primary);
     if(pi>0&&(now-this._lastProbe)>=15000){
      let probe=null;
      for(let i=pi-1;i>=0;i--){if(CH[i].ready()&&now>=CH[i].until){probe=CH[i];break}}
      if(probe){this._lastProbe=now;const s=await pack();this._markSent(probe,now);
       Promise.resolve(probe.send(s)).catch(()=>{})}
     }
    }
    this.drawPill();
   }catch(e){}finally{this._ticking=false}
  },
  async onAck(o,kind){
   if(!o||o.d!==DEVID||o.e!==this.epoch)return;
   if(ID.pin&&!await IDOK(o.ip,o.is,'BRLA1|'+o.d+'|'+o.e+'|'+o.upto))return;
   const u=+o.upto||0;
   if(u>this.upto){
    this.upto=u;keep('biruni-lad-u-'+ROOM,String(this.upto));
    this._items=this._items.filter(x=>x.q>this.upto);
    if(this.db){try{await this._tx('readwrite',
     s=>s.delete(IDBKeyRange.bound(ROOM+':',ROOM+':'+pad(this.upto))))}catch(e){}}
   }
   const now=Date.now(),was=this.primary(now),ch=CH.find(c=>c.id===kind);
   if(ch){ch.okAt=now;ch.unacked=0;ch.waitMs=0;ch.until=0;ch.fails=0}
   this._waitSince=0;this.drawPill();
   if(typeof lineOpen==='function'&&!lineOpen()&&(!was||(ch&&was.id!==ch.id))&&typeof offline==='function')offline();
  },
  channels:CH
 };
 return lad;
})();
"""
