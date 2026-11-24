"""Phone-side dead reckoning + store-and-forward + fallback ladder script (inserted before PHONE_JS)."""

DR_JS = r"""
/* ---- Biruni send-on-change dead reckoning (pure JS, no DOM) ---- */
const DRC={MIN_M:5,ACC_K:2,ACC_CAP_M:50,TURN_DEG:25,MOVE_MS:0.8,STOP_MS:0.4,
 HEARTBEAT_MS:30000,EXTRAP_MS:10000,VEL_GAP_MS:30000};
const DR=(()=>{
 const M_DEG=111320;
 let last=null,skipped=null;
 function normDeg(a){return ((a%360)+360)%360}
 function distM(lat0,lon0,lat1,lon1){
  const k=Math.max(1e-6,Math.abs(Math.cos(lat0*Math.PI/180)));
  return Math.hypot((lon1-lon0)*M_DEG*k,(lat1-lat0)*M_DEG);
 }
 function predict(t){
  if(!last)return null;
  if(!last.moving||last.cog===null)return {lat:last.lat,lon:last.lon};
  const dt=Math.max(0,Math.min(t-last.t,DRC.EXTRAP_MS))/1000;
  const d=last.v*dt,r=last.cog*Math.PI/180;
  const k=Math.max(1e-6,Math.abs(Math.cos(last.lat*Math.PI/180)));
  return {lat:last.lat+(d*Math.cos(r))/M_DEG,lon:last.lon+(d*Math.sin(r))/(M_DEG*k)};
 }
 function velocity(p,t){
  if(Number.isFinite(p.speed)&&Number.isFinite(p.cog)){
   return {v:Math.max(0,p.speed),cog:normDeg(p.cog)};
  }
  if(Number.isFinite(p.speed)&&p.speed<=DRC.STOP_MS){
   return {v:Math.max(0,p.speed),cog:null};
  }
  if(last&&t>last.t&&(t-last.t)<=DRC.VEL_GAP_MS){
   const k=Math.max(1e-6,Math.abs(Math.cos(last.lat*Math.PI/180)));
   const dx=(p.lon-last.lon)*M_DEG*k,dy=(p.lat-last.lat)*M_DEG,d=Math.hypot(dx,dy);
   return {v:d/((t-last.t)/1000),cog:d>0?normDeg(Math.atan2(dx,dy)*180/Math.PI):null};
  }
  return {v:0,cog:null};
 }
 function offer(p,force){
  const t=Number.isFinite(p.t)?p.t:Date.now();
  const vel=velocity(p,t);
  const moving=last?(last.moving?vel.v>DRC.STOP_MS:vel.v>=DRC.MOVE_MS):(vel.v>=DRC.MOVE_MS);
  const next={lat:p.lat,lon:p.lon,t:t,v:vel.v,cog:vel.cog,moving:moving};
  if(force||!last){last=next;skipped=null;return [p]}
  const flip=moving!==last.moving;
  const pred=predict(t);
  const acc=Number.isFinite(p.acc)&&p.acc>0?p.acc:0;
  const thresh=Math.max(DRC.MIN_M,DRC.ACC_K*Math.min(acc,DRC.ACC_CAP_M));
  const distHit=distM(pred.lat,pred.lon,p.lat,p.lon)>thresh;
  let turnHit=false;
  if(moving&&vel.cog!==null&&last.cog!==null){
   let diff=Math.abs(vel.cog-last.cog)%360;if(diff>180)diff=360-diff;
   if(diff>DRC.TURN_DEG)turnHit=true;
  }
  if(distHit||turnHit||flip){
   const st=skipped&&Number.isFinite(skipped.t)?skipped.t:-Infinity;
   const out=st>last.t?[skipped,p]:[p];
   last=next;skipped=null;return out;
  }
  if((t-last.t)>=DRC.HEARTBEAT_MS){last=next;skipped=null;return [p]}
  skipped=p;return [];
 }
 function reset(){last=null;skipped=null}
 return {predict:predict,offer:offer,reset:reset};
})();
"""

LADDER_JS = r"""
/* ---- Biruni fallback ladder: store-and-forward positions + channel race ---- */
const LAD=(()=>{
 const BATCH_MS=()=>typeof BR!=='undefined'&&BR.batchMs!=null?BR.batchMs:10000;
 const PRIMARY_TTL_MS=()=>typeof BR!=='undefined'&&BR.primaryTtl!=null?BR.primaryTtl:90000;
 const PROBE_RELAY_MS=60000;
 const CH=[
  {id:'direct',minGap:900,ready:()=>typeof lineOpen==='function'&&lineOpen(),
   send:s=>{lineText(JSON.stringify({t:'lp',s:s}));return true},okAt:0,fails:0,unacked:0,waitMs:0,until:0,lastSent:-1},
  {id:'tunnel',minGap:1000,ready:()=>SIG.relays.some(r=>r.kind==='tunnel'),
   send:s=>{const r=SIG.relays.find(x=>x.kind==='tunnel');return r?r.post(s):false},
   okAt:0,fails:0,unacked:0,waitMs:0,until:0,lastSent:-1},
  {id:'org',relay:true,minGap:2000,ready:()=>(SIG.kinds().org||0)>0,
   send:s=>SIG.sendRaw(s,'org'),okAt:0,fails:0,unacked:0,waitMs:0,until:0,lastSent:-1},
  {id:'mqtt',relay:true,minGap:2000,ready:()=>(SIG.kinds().mqtt||0)>0,
   send:s=>SIG.sendRaw(s,'mqtt'),okAt:0,fails:0,unacked:0,waitMs:0,until:0,lastSent:-1},
  {id:'nostr',relay:true,minGap:3000,ready:()=>(SIG.kinds().nostr||0)>0,
   send:s=>SIG.sendRaw(s,'nostr'),okAt:0,fails:0,unacked:0,waitMs:0,until:0,lastSent:-1},
  {id:'ntfy',relay:true,minGap:2000,ready:()=>(SIG.kinds().ntfy||0)>0,
   send:s=>SIG.sendRaw(s,'ntfy'),okAt:0,fails:0,unacked:0,waitMs:0,until:0,lastSent:-1},
  {id:'own',relay:true,minGap:2000,ready:()=>(SIG.kinds().own||0)>0,
   send:s=>SIG.sendRaw(s,'own'),okAt:0,fails:0,unacked:0,waitMs:0,until:0,lastSent:-1},
  {id:'drive',minGap:20000,
   ready:()=>typeof GDRIVE!=='undefined'&&GDRIVE&&typeof DRV==='object'&&DRV.go(),
   send:()=>false,okAt:0,fails:0,unacked:0,waitMs:0,until:0,lastSent:-1}
 ];
 const pad=n=>String(n).padStart(9,'0');
 const keyFor=q=>ROOM+':'+pad(q);
 function openPosDb(){return new Promise(r=>{
  if(typeof indexedDB==='undefined')return r(null);
  try{const q=indexedDB.open('biruni-pos',1);
   q.onupgradeneeded=()=>{if(!q.result.objectStoreNames.contains('p'))q.result.createObjectStore('p')};
   q.onsuccess=()=>r(q.result);q.onerror=()=>r(null)}catch(e){r(null)}})}
 const lad={
  epoch:'',seq:0,upto:0,u2:0,cap:false,db:null,_items:[],_pushes:0,_waitSince:0,_lastProbe:0,_lastRelayProbe:0,
  _ticking:false,_timer:null,_ready:null,_resolveReady:null,_startedAt:0,_lastDriveClean:0,_driveBusy:false,
  _cleaning:false,
  init(){
   if(this._ready)return this._ready;
   this._startedAt=Date.now();
   if(!this._lastRelayProbe)this._lastRelayProbe=this._startedAt;
   let e=kept('biruni-lad-e-'+ROOM);if(!e){e=rid();keep('biruni-lad-e-'+ROOM,e)}this.epoch=e;
   this.seq=parseInt(kept('biruni-lad-q-'+ROOM)||'0',10)||0;
   this.upto=parseInt(kept('biruni-lad-u-'+ROOM)||'0',10)||0;
   this.u2=parseInt(kept('biruni-lad-u2-'+ROOM)||'0',10)||0;
   if(typeof TUNH==='string'&&/^[a-z0-9-]{3,80}$/.test(TUNH)){this.cap=true;SIG.tunnel(TUNH)}
   if(typeof ORGH==='string'&&/^[a-z0-9-]{1,63}\.[a-z0-9-]{1,63}\.workers\.dev$/.test(ORGH)){this.cap=true;SIG.org(ORGH)}
   if(typeof GDRIVE!=='undefined'&&GDRIVE)this.cap=true;
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
  onCap(m){if(m&&m.lad)this.cap=true;if(m&&m.tun)this.onTun(m.tun);if(m&&m.org)this.onOrg(m.org)},
  onTun(h){if(typeof h==='string'&&/^[a-z0-9-]{3,80}$/.test(h)){
   keep('biruni-tun-'+ROOM,h);this.cap=true;SIG.tunnel(h)}},
  onOrg(h){const v=String(h||'').trim().toLowerCase();
   if(/^[a-z0-9-]{1,63}\.[a-z0-9-]{1,63}\.workers\.dev$/.test(v)){
    keep('biruni-org-'+ROOM,v);this.cap=true;SIG.org(v)}},
  get backlog(){return this._items.length},
  primary(now=Date.now()){return CH.find(c=>c.ready()&&c.okAt>0&&(now-c.okAt)<=PRIMARY_TTL_MS())||null},
  viaName(){const p=this.primary();if(!p||p.id==='direct')return '';
   if(p.id==='tunnel')return T.chTunnel||'direct address';
   if(p.id==='org')return T.chOrg||"your organization's relay";
   if(p.id==='own')return T.chOwn||'Biruni relay';
   if(p.id==='drive')return T.driveTitle||'Google Drive';
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
   const cut=Date.now()-10*24*3600*1000;
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
   const gap=(c.relay&&this._items.length<=60)?BATCH_MS():c.minGap;
   if(c.unacked>=3){c.waitMs=c.waitMs?Math.min(60000,c.waitMs*2):2000;c.until=now+c.waitMs}
   else c.until=now+gap;
  },
  async _driveSend(todo,lo,bl){
   if(this._driveBusy)return;this._driveBusy=true;
   const dch=CH.find(c=>c.id==='drive');if(dch)dch.until=Date.now()+20000;
   try{
    const tag=DRV.tag||await BFP.tag(KEYB);
    const fo=await dfolders(tag);await dfresh();
    if(!fo.g){fo.g=await dfolder('g-'+DEVID,fo.r,'biruniGps',DEVID);keep('biruni-drive-f-'+ROOM,JSON.stringify(fo))}
    let sentFiles=0;
    for(let off=0;off<todo.length&&sentFiles<3;off+=300){
     if(!DRV.go())break;
     const batch=todo.slice(off,off+300);
     const now=typeof NOW==='function'?NOW():Date.now(),loNow=this._items.length?this._items[0].q:lo,blNow=this._items.length||bl;
     const env={t:'lpos',v:1,d:DEVID,e:this.epoch,lo:loNow,bl:blNow,b:batch.map(x=>[x.q,x.p]),n:rid(),ts:now};
     const sealed=await seal(env,true);
     const name=pad(batch[0].q)+'-'+pad(batch[batch.length-1].q)+'-'+rid().slice(0,6)+'.bgp';
     driveSay('up');
     let j;
     try{
      const b='bgp'+rid().replace(/[^A-Za-z0-9]/g,''),meta={name:name,parents:[fo.g],appProperties:{bgp:'1'}};
      const body=new Blob(['--'+b+'\r\nContent-Type: application/json; charset=UTF-8\r\n\r\n'+JSON.stringify(meta)
       +'\r\n--'+b+'\r\nContent-Type: application/octet-stream\r\n\r\n','BGP1\n'+sealed,'\r\n--'+b+'--\r\n']);
      let r;
      try{r=await dapi('/upload/drive/v3/files?uploadType=multipart&supportsAllDrives=true&fields=id,size',
       {method:'POST',headers:{'Content-Type':'multipart/related; boundary='+b},body:body,ms:180000})}
      catch(e){throw new Error('network')}
      await dcheck(r);j=await r.json();
     }catch(e){
      if(e&&(e.status===404||e.perm)){try{await dlost()}catch(x){if(x instanceof DriveStop)throw x}}
      throw e;
     }
     for(const it of batch){
      it.dv=true;
      if(this.db&&it.q>this.upto){try{await this._tx('readwrite',s=>s.put(it,keyFor(it.q)))}catch(e){}}
     }
     await dput({id:'#g'+j.id,fid:j.id,hi:batch[batch.length-1].q,at:Date.now()});
     DRV.fails=0;DRV.until=0;driveSay('idle');sentFiles++;
    }
   }catch(e){
    if(!(e instanceof DriveStop)){
     DRV.fails=(DRV.fails||0)+1;const ms=ddelay(DRV.fails,e&&e.ra);DRV.until=Date.now()+ms;driveSay('wait');
    }
   }finally{this._driveBusy=false}
  },
  async _driveClean(){
   if(this._cleaning||typeof GDRIVE==='undefined'||!GDRIVE||typeof DRV!=='object'||!DRV.ok())return;
   this._cleaning=true;
   try{
    const recs=await dgall();
    for(const r of recs){
     if(!DRV.ok())break;
     if(r.hi<=this.upto){
      let res;
      try{res=await dapi('/drive/v3/files/'+encodeURIComponent(r.fid)+'?supportsAllDrives=true&fields=id',
       {method:'PATCH',headers:{'Content-Type':'application/json'},body:JSON.stringify({trashed:true})})}
      catch(e){break}
      if(res.status===204||res.status===200||res.status===404){await ddrop(r.id)}
      else if(res.status===401){driveDead();break}
      else if(res.status===403)continue;
      else break;
     }
    }
   }catch(e){}finally{this._cleaning=false}
  },
  async tick(){
   if(this._ticking||!this.cap||!BK)return;
   const now=typeof NOW==='function'?NOW():Date.now();
   if(typeof GDRIVE!=='undefined'&&GDRIVE&&typeof DRV==='object'&&DRV.ok()&&(now-this._lastDriveClean)>=60000){
    this._lastDriveClean=now;this._driveClean();
   }
   if(!this._items.length){this._waitSince=0;this.drawPill();return}
   this._ticking=true;
   try{
    const items=this._items;
    const pick=items.length<=60?items:items.slice(0,50).concat(items.slice(-10));
    const env={t:'lpos',v:1,d:DEVID,e:this.epoch,lo:items[0].q,bl:items.length,
     b:pick.map(x=>[x.q,x.p]),n:rid(),ts:now};
    const live=CH.filter(c=>c.id!=='drive');
    const primary=this.primary(now),stalled=!!(primary&&this._waitSince&&(now-this._waitSince>=3000));
    let sealed=null;
    const pack=async()=>{if(!sealed)sealed=await seal(env,true);return sealed};
    if(!primary||stalled){
     const targets=live.filter(c=>c.ready()&&now>=c.until);
     if(targets.length){
      const s=await pack();
      for(const c of targets){this._markSent(c,now);Promise.resolve(c.send(s)).catch(()=>{})}
      if(!this._waitSince)this._waitSince=now;
     }
    }else{
     let sentPrimary=false;
     if(now>=primary.until){
      const s=await pack();this._markSent(primary,now);
      Promise.resolve(primary.send(s)).catch(()=>{});
      if(!this._waitSince)this._waitSince=now;
      sentPrimary=true;
     }
     const pi=live.indexOf(primary);
     if(pi>0){
      let probe=null;
      for(let i=pi-1;i>=0;i--){
       const c=live[i];
       if(!c.ready()||now<c.until)continue;
       const ok=c.relay?(sentPrimary&&(now-this._lastRelayProbe)>=PROBE_RELAY_MS):(now-this._lastProbe)>=15000;
       if(ok){probe=c;break}
      }
      if(probe){
       if(probe.relay)this._lastRelayProbe=now;else this._lastProbe=now;
       const s=await pack();this._markSent(probe,now);
       Promise.resolve(probe.send(s)).catch(()=>{});
      }
     }
    }
    const dch=CH.find(c=>c.id==='drive');
    if(dch&&dch.ready()&&(!primary||stalled)){
     const lastOther=Math.max(0,...live.map(c=>c.okAt||0));
     const ref=lastOther||this._startedAt||now;
     if((now-ref)>=15000&&now>=dch.until&&(!DRV.until||now>=DRV.until)&&!this._driveBusy){
      const todo=items.filter(x=>!x.dv);
      if(todo.length)this._driveSend(todo,items[0].q,items.length);
     }
    }
    this.drawPill();
   }catch(e){}finally{this._ticking=false}
  },
  async onAck(o,kind){
   if(!o||o.d!==DEVID||o.e!==this.epoch)return;
   if(ID.pin&&!await IDOK(o.ip,o.is,'BRLA1|'+o.d+'|'+o.e+'|'+o.upto))return;
   if(ID.pin&&o.is2!=null&&!await IDOK(o.ip,o.is2,'BRLA2|'+o.d+'|'+o.e+'|'+o.upto+'|'+(o.u2||0)))return;
   const u=+o.upto||0,u2=+o.u2||0;
   if(u2>this.u2){this.u2=u2;keep('biruni-lad-u2-'+ROOM,String(this.u2))}
   if(u>this.upto){
    this.upto=u;keep('biruni-lad-u-'+ROOM,String(this.upto));
    this._items=this._items.filter(x=>x.q>this.upto);
    if(this.db){try{await this._tx('readwrite',
     s=>s.delete(IDBKeyRange.bound(ROOM+':',ROOM+':'+pad(this.upto))))}catch(e){}}
   }
   const now=Date.now(),was=this.primary(now),ch=CH.find(c=>c.id===kind);
   if(ch){
    const gap=(ch.relay&&this._items.length<=60)?BATCH_MS():ch.minGap;
    ch.okAt=now;ch.unacked=0;ch.waitMs=0;ch.fails=0;
    ch.until=ch.lastSent>=0?Math.min(ch.until,ch.lastSent+gap):0;
   }
   if(!this._lastRelayProbe)this._lastRelayProbe=now;
   this._waitSince=0;this.drawPill();
   if(typeof lineOpen==='function'&&!lineOpen()&&(!was||(ch&&was.id!==ch.id))&&typeof offline==='function')offline();
  },
  channels:CH
 };
 return lad;
})();
"""
