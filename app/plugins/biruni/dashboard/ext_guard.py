"""Page side of «Who leaked this screenshot?» (see guard.py): after the dashboard is built the page asks the PC for
its card's mark and lays it over the whole window as two fixed, click-through layers - one that adds a level
(``plus-lighter``) and one that takes a level away (``difference``) - so the faint pattern exists on dark and on
light themes alike. Nothing in the numbers, the text or what is copied changes."""
from . import guard  # noqa: F401 - registers the PC's "guard_hello" answer

CSS = r"""
.guardL{position:fixed;inset:0;pointer-events:none;z-index:2147483600;background-repeat:repeat;image-rendering:pixelated;
 image-rendering:crisp-edges}
.guardA{mix-blend-mode:plus-lighter}.guardB{mix-blend-mode:difference}"""

JS = r"""
const BD=window.BiruniDash;if(!BD)return;
let asked=false,layers=null;
BD.onBuild(()=>{if(!asked){asked=true;BD.send({t:'guard_hello'})}});
function bits(hex,n){const o=new Int8Array(n);for(let i=0;i<n;i++){const b=parseInt(hex.substr((i>>3)*2,2),16)||0;o[i]=((b>>(7-(i&7)))&1)?1:-1}return o}
function light(){const c=getComputedStyle(document.body).backgroundColor.match(/\d+(\.\d+)?/g)||[0,0,0];
 return (0.299*c[0]+0.587*c[1]+0.114*c[2])>190}
function tiles(m){
 const nl=m.nl|0,cl=m.cl|0,nc=m.nc|0,cc=m.cc|0,T=nl*cl;
 let al=Math.max(0,Math.min(4,m.al|0)),ac=Math.max(0,Math.min(4,m.ac|0));
 // on a light page white cannot go up: the minus cells go down twice as far and nothing is added
 const lt=light();
 if(!T||T!==nc*cc)return null;
 const L=bits(String(m.L||''),nl*nl),C=bits(String(m.C||''),nc*nc);
 const mk=()=>{const c=document.createElement('canvas');c.width=c.height=T;return c};
 const A=mk(),B=mk(),xa=A.getContext('2d'),xb=B.getContext('2d'),da=xa.createImageData(T,T),db=xb.createImageData(T,T);
 for(let y=0;y<T;y++)for(let x=0;x<T;x++){
  const l=L[((y/cl)|0)*nl+((x/cl)|0)],c=C[((y/cc)|0)*nc+((x/cc)|0)],i=(y*T+x)*4;
  // adds (plus-lighter): luminance on R and G, chroma on blue
  const pa=lt?0:al,pc=lt?0:ac,ma=lt?2*al:al,mc=lt?2*ac:ac;
  da.data[i]=l>0?pa:0;da.data[i+1]=l>0?pa:0;da.data[i+2]=c>0?pc:0;da.data[i+3]=255;
  // takes away (difference: |page - d| = page - d for anything but pure black)
  db.data[i]=l<0?ma:0;db.data[i+1]=l<0?ma:0;db.data[i+2]=c<0?mc:0;db.data[i+3]=255}
 xa.putImageData(da,0,0);xb.putImageData(db,0,0);
 return {a:A.toDataURL('image/png'),b:B.toDataURL('image/png'),T:T}}
function apply(m){
 if(layers){layers.forEach(n=>n.remove());layers=null}
 if(!m||m.none)return;
 const t=tiles(m);if(!t)return;
 layers=[['guardA',t.a],['guardB',t.b]].map(([cls,url])=>{const d=document.createElement('div');d.className='guardL '+cls;
  d.setAttribute('aria-hidden','true');d.style.backgroundImage='url('+url+')';d.style.backgroundSize=t.T+'px '+t.T+'px';
  document.body.appendChild(d);return d})}
let last=null;
BD.on('guard',m=>{last=m&&!m.none?m:null;apply(last)});
// a theme change (dark ↔ light) redraws the mark for the new background
try{new MutationObserver(()=>{if(last)setTimeout(()=>apply(last),50)}).observe(document.documentElement,{attributes:true,attributeFilter:['data-theme']})}catch(e){}
BD.guard={apply:m=>{last=m;apply(m)},clear:()=>{last=null;apply(null)}};
"""


def _who(plugin, dock):
    return guard.who_ui(plugin, dock)


ACTIONS = [("guard_who", "Who leaked this screenshot?", "fingerprint", _who)]
