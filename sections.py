"""The Sections tab (page6): Oregon + Washington cut into 57 named hiking areas the owner works through
BY HAND on AllTrails (open the area, open each trail, save it with the extension). Progress is live:
your saved trails (window.WxMine, trails.json on the repo's "trails" branch) are counted into the area
holding most of each trail's points, and drawn on a small SVG map (no Mapbox map, so no map loads).
"Done" ticks sync like your trails: sections.json on the trails branch, written with the same GitHub key
(localStorage wx-gh-token, Map -> Trails -> connect GitHub); this browser keeps a copy.
Nothing here fetches from AllTrails: each area is only an "Open on AllTrails" link to its explore map.
"""
import json
import re

SECTION_TAB = 6   # page6 in the shell; the sidebar button order must match

# (state, name, (west, south, east, north)); boxes overlap a little at some edges
SECTIONS = [
    ("OR", "North Coast", (-124.1, 45.55, -123.2, 46.3)),
    ("OR", "Portland & Vancouver", (-123.2, 45.3, -122.45, 46.3)),
    ("OR", "Columbia Gorge west", (-122.45, 45.45, -121.7, 45.9)),
    ("OR", "Columbia Gorge east", (-121.7, 45.45, -120.6, 45.9)),
    ("OR", "Tillamook Coast & Cascade Head", (-124.2, 44.6, -123.2, 45.55)),
    ("OR", "Salem & Silver Falls", (-123.2, 44.6, -122.45, 45.3)),
    ("OR", "Mt Hood", (-122.45, 45.1, -121.4, 45.45)),
    ("OR", "Clackamas, Opal Creek & Bull of the Woods", (-122.45, 44.8, -121.4, 45.1)),
    ("OR", "Mt Jefferson & Santiam Pass", (-122.45, 44.3, -121.4, 44.8)),
    ("OR", "Lower Deschutes & Madras", (-121.4, 44.45, -120.4, 45.45)),
    ("OR", "Central Coast", (-124.3, 43.8, -123.6, 44.6)),
    ("OR", "Corvallis & Marys Peak", (-123.6, 44.25, -122.45, 44.6)),
    ("OR", "Eugene & McKenzie foothills", (-123.6, 43.45, -122.5, 44.25)),
    ("OR", "Three Sisters & McKenzie Pass", (-122.5, 43.85, -121.65, 44.3)),
    ("OR", "Bend, Smith Rock & Newberry", (-121.65, 43.55, -120.6, 44.45)),
    ("OR", "Willamette Pass & Diamond Peak", (-122.5, 43.45, -121.65, 43.85)),
    ("OR", "Coos Bay & Bandon", (-124.5, 42.8, -123.6, 43.8)),
    ("OR", "Umpqua & Roseburg", (-123.6, 42.95, -122.3, 43.45)),
    ("OR", "Crater Lake & Mt Thielsen", (-122.3, 42.75, -121.6, 43.45)),
    ("OR", "Curry Coast & Kalmiopsis", (-124.6, 42.0, -123.6, 42.8)),
    ("OR", "Rogue River & Illinois Valley", (-123.6, 42.0, -123.0, 42.95)),
    ("OR", "Ashland & Medford", (-123.0, 42.0, -122.3, 42.95)),
    ("OR", "Sky Lakes & Klamath Falls", (-122.3, 42.0, -121.3, 42.75)),
    ("OR", "Ochocos & Painted Hills", (-120.6, 44.0, -119.5, 45.0)),
    ("OR", "Strawberry Mountain & John Day", (-119.5, 43.9, -118.2, 44.9)),
    ("OR", "Elkhorns & Baker City", (-118.2, 44.2, -117.3, 44.9)),
    ("OR", "Blue Mountains & Pendleton", (-119.5, 44.9, -117.5, 46.0)),
    ("OR", "Wallowas & Hells Canyon", (-117.5, 44.9, -116.4, 46.0)),
    ("OR", "Fort Rock, Summer Lake & Gearhart", (-121.3, 42.0, -119.5, 43.55)),
    ("OR", "Steens & Alvord", (-119.5, 42.0, -117.9, 43.4)),
    ("OR", "Owyhee & Leslie Gulch", (-117.9, 42.0, -116.9, 44.0)),
    ("WA", "Mt St Helens & Siouxon", (-122.6, 45.9, -121.75, 46.45)),
    ("WA", "Mt Adams & Indian Heaven", (-121.75, 45.9, -121.0, 46.45)),
    ("WA", "Long Beach & Willapa Hills", (-124.2, 46.3, -123.0, 46.9)),
    ("WA", "Olympia, Tacoma & Capitol Forest", (-123.2, 46.45, -122.05, 47.3)),
    ("WA", "Grays Harbor & South Olympics", (-124.3, 46.9, -123.2, 47.4)),
    ("WA", "Olympic Coast, Hoh & Quinault", (-124.8, 47.4, -123.7, 48.4)),
    ("WA", "Olympics east & Hurricane Ridge", (-123.7, 47.4, -123.0, 48.2)),
    ("WA", "Kitsap, Hood Canal & Port Townsend", (-123.0, 47.3, -122.45, 48.2)),
    ("WA", "San Juans & Whidbey", (-123.25, 48.2, -122.35, 48.8)),
    ("WA", "Seattle & Issaquah Alps", (-122.45, 47.3, -121.9, 47.95)),
    ("WA", "North Bend & Mt Si", (-121.9, 47.3, -121.55, 47.6)),
    ("WA", "Snoqualmie Pass", (-121.55, 47.25, -121.0, 47.6)),
    ("WA", "Stevens Pass & US 2", (-121.9, 47.6, -121.0, 47.95)),
    ("WA", "Mountain Loop Highway", (-122.45, 47.95, -121.2, 48.3)),
    ("WA", "Skagit & Baker Lake", (-122.35, 48.3, -121.4, 48.6)),
    ("WA", "Bellingham & Mt Baker", (-122.6, 48.6, -121.4, 49.0)),
    ("WA", "North Cascades Highway & Methow", (-121.4, 48.3, -119.9, 49.0)),
    ("WA", "Lake Chelan", (-120.9, 47.9, -119.9, 48.3)),
    ("WA", "Leavenworth, Enchantments & Wenatchee", (-121.0, 47.35, -120.2, 47.9)),
    ("WA", "Teanaway & Cle Elum", (-121.0, 47.0, -120.4, 47.35)),
    ("WA", "Mt Rainier", (-122.05, 46.75, -121.3, 47.1)),
    ("WA", "White Pass & Goat Rocks", (-121.85, 46.45, -120.9, 46.75)),
    ("WA", "Yakima & Ellensburg", (-120.9, 46.4, -119.9, 47.0)),
    ("WA", "Spokane & Mt Spokane", (-117.8, 47.3, -117.0, 48.0)),
    ("WA", "Kettle Crest & Colville", (-118.8, 48.0, -117.0, 49.0)),
    ("WA", "Blue Mountains & Walla Walla", (-118.5, 46.0, -117.0, 46.45)),
]


def sections_json():
    """[{id, n, st, b}] for the page; id = a stable slug (the done marks key on it)."""
    return json.dumps([{"id": st.lower() + "-" + re.sub(r"[^a-z0-9]+", "-", n.lower()).strip("-"), "n": n, "st": st, "b": list(b)}
                       for st, n, b in SECTIONS], separators=(",", ":"))


PAGE_HTML = """
<header class="page-head"><h1>Trail Sections</h1><p>Oregon and Washington in 57 hiking areas to work through by hand on AllTrails ·
  open an area, open the trails you want, save each with the extension (Alt+Shift+S) · your saved trails count here as they arrive</p></header>
<div class="sx" id="sec-root">
  <div class="sx-prog" id="sx-prog"></div>
  <div class="sx-grid">
    <div class="sx-main">
      <div class="sx-tools">
        <div class="sx-seg" role="group" aria-label="Show">
          <button type="button" data-f="all" aria-pressed="true">All</button><button type="button" data-f="todo" aria-pressed="false">To do</button><button type="button" data-f="done" aria-pressed="false">Done</button><button type="button" data-f="mine" aria-pressed="false">With your trails</button>
        </div>
        <span class="sx-sync" id="sx-sync"></span>
      </div>
      <div id="sx-list"></div>
    </div>
    <div class="sx-side">
      <svg id="sx-map" role="img" aria-label="Map of the sections with your saved trails"></svg>
      <div class="sx-leg"><span><i class="t"></i>To do</span><span><i class="s"></i>Has your trails</span><span><i class="d"></i>Done</span>
        <span><b class="h"></b>Your hikes</span><span><b class="m"></b>Your MTB trails</span></div>
      <p class="sx-note" id="sx-note"></p>
    </div>
  </div>
</div>
"""

CSS = r"""
.sx-prog { display:flex; flex-wrap:wrap; gap:12px 32px; margin:2px 0 18px; }
.sx-pg { display:grid; gap:5px; min-width:190px; }
.sx-pg .t { display:flex; justify-content:space-between; gap:14px; font-size:11px; font-weight:700; letter-spacing:.06em; text-transform:uppercase; color:#5A5F6B; }
.sx-pg .t b { color:#111; font-size:13px; letter-spacing:0; font-variant-numeric:tabular-nums; }
.sx-pg .bar { height:6px; border-radius:3px; background:#E7E9EE; overflow:hidden; }
.sx-pg .bar i { display:block; height:100%; }
.sx-grid { display:grid; grid-template-columns:minmax(0,2fr) minmax(0,1fr); gap:24px; align-items:start; }
.sx-side { position:sticky; top:16px; display:grid; gap:8px; }
#sx-map { display:block; width:100%; height:auto; max-height:calc(100vh - 140px); background:#fff; border-radius:12px; box-shadow:0 1px 4px rgba(0,0,0,.08); }
#sx-map .land { fill:#EEF0EC; stroke:#C3CAC4; stroke-width:1.1; stroke-linejoin:round; }
#sx-map .stl { font:700 12px system-ui,sans-serif; letter-spacing:.16em; fill:#A2A8B3; }
#sx-map .box { fill:rgba(31,58,82,.03); stroke:#7C8494; stroke-width:.9; stroke-dasharray:3 2; cursor:pointer; }
#sx-map .box.mine { fill:rgba(176,69,126,.08); stroke:#B0457E; stroke-dasharray:none; }
#sx-map .box.done { fill:rgba(47,122,69,.18); stroke:#2F7A45; stroke-dasharray:none; }
#sx-map .box.hot { stroke:#FE5000; stroke-width:2.4; stroke-dasharray:none; }
#sx-map .box:focus { outline:none; stroke:#FE5000; stroke-width:2.4; }
#sx-map .trl { fill:none; stroke-width:1.4; stroke-linecap:round; stroke-linejoin:round; pointer-events:none; }
#sx-map .cnt { font:700 10.5px system-ui,sans-serif; fill:#7A2E58; paint-order:stroke; stroke:#fff; stroke-width:3px; stroke-linejoin:round; pointer-events:none; }
.sx-leg { display:flex; flex-wrap:wrap; gap:4px 14px; font-size:11.5px; color:#5A5F6B; }
.sx-leg span { display:inline-flex; align-items:center; gap:5px; }
.sx-leg i { width:13px; height:9px; border:1px dashed #7C8494; background:rgba(31,58,82,.03); }
.sx-leg i.s { border:1px solid #B0457E; background:rgba(176,69,126,.08); }
.sx-leg i.d { border:1px solid #2F7A45; background:rgba(47,122,69,.18); }
.sx-leg b { width:14px; height:3px; border-radius:2px; background:#B0457E; }
.sx-leg b.m { background:#1C6FD9; }
.sx-note { margin:0; font-size:11.5px; color:#8A8F9C; line-height:1.5; }
.sx-tools { display:flex; justify-content:space-between; align-items:center; gap:10px; flex-wrap:wrap; margin-bottom:12px; }
.sx-seg { display:inline-flex; flex-wrap:wrap; border:1px solid #DDE0E6; border-radius:8px; overflow:hidden; background:#fff; }
.sx-seg button { border:0; background:none; padding:6px 12px; font:inherit; font-size:12.5px; font-weight:600; color:#5A5F6B; cursor:pointer; }
.sx-seg button+button { border-left:1px solid #DDE0E6; }
.sx-seg button[aria-pressed=true] { background:#111; color:#fff; }
.sx-seg button:focus-visible { outline:2px solid #FE5000; outline-offset:-2px; }
.sx-sync { font-size:11.5px; color:#8A8F9C; }
.sx-sync.ok { color:#2F7A45; font-weight:600; }
.sx-st { display:flex; justify-content:space-between; align-items:baseline; margin:20px 0 6px; font-size:12px; font-weight:800; letter-spacing:.08em; text-transform:uppercase; color:#5A5F6B; }
.sx-st:first-child { margin-top:0; }
.sx-st b { color:#111; letter-spacing:0; font-variant-numeric:tabular-nums; }
.sx-list { list-style:none; margin:0; padding:0; background:#fff; border-radius:12px; box-shadow:0 1px 4px rgba(0,0,0,.08); overflow:hidden; }
.sx-row { display:grid; grid-template-columns:auto minmax(0,1fr) auto; gap:3px 12px; align-items:center; padding:10px 14px; border-top:1px solid #F0F1F4; }
.sx-row:first-child { border-top:0; }
.sx-row.hot { background:#FFF4EE; box-shadow:inset 3px 0 0 #FE5000; }
.sx-row input { width:17px; height:17px; margin:0; accent-color:#2F7A45; cursor:pointer; }
.sx-row .nm { font-size:14px; font-weight:700; color:#111; line-height:1.3; }
.sx-row.done .nm { color:#5A5F6B; }
.sx-row .co { font-size:11.5px; color:#8A8F9C; font-variant-numeric:tabular-nums; }
.sx-row .co em { font-style:normal; font-weight:700; color:#2F7A45; }
.sx-row .chip { display:inline-block; margin-left:8px; padding:0 8px; border-radius:999px; background:#F8E8F0; color:#8E2F63; font-size:11.5px; font-weight:700; line-height:1.75; font-variant-numeric:tabular-nums; vertical-align:1px; }
.sx-row .at { font-size:12.5px; font-weight:700; color:#6B3FA8; text-decoration:none; white-space:nowrap; }
.sx-row .at:hover { text-decoration:underline; text-underline-offset:2px; }
.sx-row .at:focus-visible, .sx-row summary:focus-visible, .sx-tr button:focus-visible { outline:2px solid #FE5000; outline-offset:2px; }
.sx-row details { grid-column:2/4; }
.sx-row summary { width:max-content; max-width:100%; cursor:pointer; font-size:12px; font-weight:700; color:#8E2F63; }
.sx-trs { list-style:none; margin:6px 0 2px; padding:0; columns:2 250px; column-gap:20px; }
.sx-tr { break-inside:avoid; display:flex; align-items:baseline; gap:6px; padding:2px 0; font-size:12.5px; }
.sx-tr i { flex:none; width:7px; height:7px; border-radius:50%; transform:translateY(-1px); }
.sx-tr a { color:#111; text-decoration:none; }
.sx-tr a:hover { text-decoration:underline; }
.sx-tr span { color:#8A8F9C; white-space:nowrap; font-variant-numeric:tabular-nums; }
.sx-tr button { border:0; background:none; padding:0; font:inherit; font-size:11.5px; font-weight:700; color:#FE5000; cursor:pointer; white-space:nowrap; }
.sx-empty { padding:14px; font-size:13px; color:#8A8F9C; }
@media (max-width:1000px) {
  .sx-grid { grid-template-columns:minmax(0,1fr); }
  .sx-side { position:static; order:-1; }
  #sx-map { max-height:56vh; }
}
@media (max-width:760px) {
  .sx-row { grid-template-columns:auto minmax(0,1fr); }
  .sx-row .at { grid-column:2; }
}
"""

JS = r"""
(function(){
var root=document.getElementById('sec-root');if(!root)return;
var SECS=__SECTIONS__;
var $=function(id){return document.getElementById(id);};
var built=false,hot=-1,filter='all',DONE={},cloudOK=false;
var HIKE='#B0457E',DIF={green:'#2F9E44',blue:'#1C6FD9',black:'#1B1B1B',dblack:'#1B1B1B',access:'#7E3FB8'};
function colOf(t){return t.act==='mtb'?(DIF[t.dif]||'#1C6FD9'):HIKE;}
function esc(s){return String(s).replace(/[&<>"]/g,function(c){return{'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c];});}
function decode(str){var i=0,lat=0,lng=0,out=[];try{while(i<str.length){for(var k=0;k<2;k++){var b,sh=0,v=0;
  do{b=str.charCodeAt(i++)-63;v|=(b&31)<<sh;sh+=5;}while(b>=32&&i<=str.length);var d=v&1?~(v>>1):v>>1;if(k)lng+=d;else lat+=d;}
  out.push([lng/1e5,lat/1e5]);}}catch(e){}return out;}
function link(b){var f=function(v){return v.toFixed(4);};
  return 'https://www.alltrails.com/explore?b_tl_lat='+f(b[3])+'&b_tl_lng='+f(b[0])+'&b_br_lat='+f(b[1])+'&b_br_lng='+f(b[2]);}
function coords(b){return b[1].toFixed(2)+'–'+b[3].toFixed(2)+'°N · '+Math.abs(b[0]).toFixed(2)+'–'+Math.abs(b[2]).toFixed(2)+'°W';}
function when(iso){var d=new Date(String(iso).slice(0,10)+'T12:00:00');return isNaN(d)?'':d.toLocaleDateString('en-US',{month:'short',day:'numeric'});}
function today(){var d=new Date();return d.getFullYear()+'-'+String(d.getMonth()+1).padStart(2,'0')+'-'+String(d.getDate()).padStart(2,'0');}
function inBox(b,p){return p[0]>=b[0]&&p[0]<=b[2]&&p[1]>=b[1]&&p[1]<=b[3];}

// ---------- your trails into sections: each trail goes to the box holding the most of its points ----------
var TR=[],BY=[],OUT=[];
function count(){var list=window.WxMine?window.WxMine.list():[];
  BY=SECS.map(function(){return[];});OUT=[];
  TR=list.map(function(m){var pts=decode(m.p||''),hits=SECS.map(function(){return 0;});
    pts.forEach(function(p){for(var k=0;k<SECS.length;k++)if(inBox(SECS[k].b,p))hits[k]++;});
    var best=-1,n=0;hits.forEach(function(h,k){if(h>n){n=h;best=k;}});
    var t={name:m.name||'Trail',mi:m.mi,act:m.act==='mtb'?'mtb':'hike',dif:m.dif||'',link:m.link||'',p:m.p,pts:pts,k:best};
    if(best>=0)BY[best].push(t);else if(pts.length)OUT.push(t);return t;});
  BY.forEach(function(a){a.sort(function(x,y){return x.name.localeCompare(y.name);});});}

// ---------- done ticks: sections.json on the trails branch (same GitHub key as your trails), plus this browser ----------
var REPO='aaronmaddenjk/oregon-weather-dashboard',BR='trails',API='https://api.github.com/repos/'+REPO+'/contents/sections.json',LKEY='wx-sections-done';
function token(){try{return localStorage.getItem('wx-gh-token')||'';}catch(e){return'';}}
function hdr(){var h={Accept:'application/vnd.github+json'};if(token())h.Authorization='Bearer '+token();return h;}
function localGet(){try{return JSON.parse(localStorage.getItem(LKEY)||'{}')||{};}catch(e){return{};}}
function localSet(d){try{localStorage.setItem(LKEY,JSON.stringify(d));}catch(e){}}
function b64(s){var u=new TextEncoder().encode(s),o='';for(var i=0;i<u.length;i+=0x8000)o+=String.fromCharCode.apply(null,u.subarray(i,i+0x8000));return btoa(o);}
async function pull(){   // -> {done, sha}
  var r=await fetch(API+'?ref='+BR,{headers:hdr(),cache:'no-store'});
  if(r.status===404)return{done:{},sha:null};
  if(!r.ok){var r2=await fetch('https://raw.githubusercontent.com/'+REPO+'/'+BR+'/sections.json?t='+Date.now());
    if(r2.status===404)return{done:{},sha:null};if(!r2.ok)throw new Error(r.status);return{done:((await r2.json())||{}).done||{},sha:null,raw:true};}
  var j=await r.json(),txt=new TextDecoder().decode(Uint8Array.from(atob((j.content||'').replace(/\s/g,'')),function(c){return c.charCodeAt(0);}));
  return{done:(JSON.parse(txt||'{}')||{}).done||{},sha:j.sha};}
async function push(fn){
  for(var k=0;k<3;k++){var g=await pull();if(g.raw)throw new Error('GitHub isn’t answering right now');
    var d=fn(Object.assign({},g.done)),body={message:'Sections: progress',content:b64(JSON.stringify({done:d},null,1)),branch:BR};if(g.sha)body.sha=g.sha;
    var r=await fetch(API,{method:'PUT',headers:Object.assign(hdr(),{'Content-Type':'application/json'}),body:JSON.stringify(body)});
    if(r.ok)return d;
    if(r.status===401||r.status===403||r.status===404)throw new Error('GitHub refused the key ('+r.status+'): reconnect in Map → Trails');
    if(r.status!==409&&r.status!==422)throw new Error('GitHub said '+r.status);}
  throw new Error('another device kept saving at the same moment');}
function syncNote(msg,ok){var el=$('sx-sync');el.textContent=msg;el.className='sx-sync'+(ok?' ok':'');}
async function loadDone(){DONE=localGet();render();
  try{var g=await pull();cloudOK=true;
    // ticks made in this browser before it was connected go up once
    var mine=localGet(),own=Object.keys(mine).filter(function(k){return!(k in g.done);});
    DONE=Object.assign({},g.done);
    if(own.length&&token())DONE=await push(function(d){own.forEach(function(k){d[k]=mine[k];});return d;});
    else own.forEach(function(k){DONE[k]=mine[k];});   // not connected: this browser's ticks stay on top
    localSet(DONE);
    syncNote(token()?'☁ Ticks saved to GitHub · every device sees them':'Ticks show from GitHub; connect GitHub (Map → Trails) to change them on every device',!!token());}
  catch(e){syncNote('Ticks kept in this browser (GitHub: '+(e.message||e)+')');}
  render();}
async function setDone(id,on){
  if(on)DONE[id]=today();else delete DONE[id];localSet(DONE);render();
  if(!token()){syncNote('Saved in this browser only · connect GitHub (Map → Trails) to see it on every device');return;}
  syncNote('Saving…');
  try{DONE=await push(function(d){if(on)d[id]=DONE[id]||today();else delete d[id];return d;});localSet(DONE);syncNote('☁ Saved to GitHub',true);render();}
  catch(e){syncNote('Saved in this browser only ('+e.message+')');}}

// ---------- the page ----------
var K=80,CX=Math.cos(46*Math.PI/180),W0=-124.95,N0=49.15;
function X(lon){return (lon-W0)*K*CX;}function Y(lat){return (N0-lat)*K;}
var VW=Math.ceil(X(-116.25)),VH=Math.ceil(Y(41.85));
var OR=[[-124.21,42.0],[-117.03,42.0],[-117.03,44.25],[-116.47,45.6],[-116.92,46.0],[-119.0,45.93],[-121.2,45.62],[-122.3,45.55],[-122.8,45.68],[-123.1,46.18],[-123.9,46.24],[-123.95,45.8],[-124.0,45.0],[-124.1,44.0],[-124.4,43.3],[-124.55,42.8],[-124.4,42.4]];
var WA=[[-123.9,46.24],[-123.1,46.18],[-122.8,45.68],[-122.3,45.55],[-121.2,45.62],[-119.0,45.93],[-116.92,46.0],[-117.04,46.43],[-117.03,49.0],[-123.03,49.0],[-122.75,48.6],[-122.4,48.1],[-123.4,48.13],[-124.73,48.38],[-124.65,48.1],[-124.15,47.3],[-124.05,46.65]];
function pts(p){return p.map(function(q){return X(q[0]).toFixed(1)+','+Y(q[1]).toFixed(1);}).join(' ');}
function drawMap(){var svg='<polygon class="land" points="'+pts(OR)+'"/><polygon class="land" points="'+pts(WA)+'"/>'+
    '<text class="stl" x="'+X(-119.4)+'" y="'+Y(42.5)+'">OREGON</text><text class="stl" x="'+X(-119.7)+'" y="'+Y(48.78)+'">WASHINGTON</text>';
  SECS.forEach(function(s,k){var b=s.b,cl='box'+(DONE[s.id]?' done':BY[k]&&BY[k].length?' mine':'')+(k===hot?' hot':'');
    svg+='<rect class="'+cl+'" tabindex="0" data-k="'+k+'" x="'+X(b[0]).toFixed(1)+'" y="'+Y(b[3]).toFixed(1)+'" width="'+(X(b[2])-X(b[0])).toFixed(1)+
      '" height="'+(Y(b[1])-Y(b[3])).toFixed(1)+'"><title>'+esc(s.n)+(BY[k]&&BY[k].length?' · '+BY[k].length+' of your trails':'')+'</title></rect>';});
  TR.forEach(function(t){if(t.pts.length<2)return;var st=Math.max(1,Math.floor(t.pts.length/80));
    svg+='<polyline class="trl" stroke="'+colOf(t)+'" points="'+pts(t.pts.filter(function(_,i){return i%st===0||i===t.pts.length-1;}))+'"/>';});
  SECS.forEach(function(s,k){var n=BY[k]?BY[k].length:0;if(!n)return;var b=s.b;
    svg+='<text class="cnt" text-anchor="middle" x="'+X((b[0]+b[2])/2).toFixed(1)+'" y="'+(Y((b[1]+b[3])/2)+4).toFixed(1)+'">'+n+'</text>';});
  var m=$('sx-map');m.setAttribute('viewBox','0 0 '+VW+' '+VH);m.innerHTML=svg;}
function bar(label,a,n,col){return '<div class="sx-pg"><div class="t">'+label+'<b>'+a+' / '+n+'</b></div><div class="bar"><i style="width:'+(n?a/n*100:0).toFixed(1)+'%;background:'+col+'"></i></div></div>';}
function render(){if(!built)return;count();
  var done=SECS.filter(function(s){return DONE[s.id];}).length,started=BY.filter(function(a){return a.length;}).length;
  var hikes=TR.filter(function(t){return t.act==='hike';}).length,mtb=TR.length-hikes;
  $('sx-prog').innerHTML=bar('Sections done',done,SECS.length,'#2F7A45')+bar('Sections with your trails',started,SECS.length,'#B0457E')+
    '<div class="sx-pg"><div class="t">Your trails<b>'+TR.length+'</b></div><div class="t" style="font-weight:600;text-transform:none;letter-spacing:0">'+hikes+' hikes · '+mtb+' MTB</div></div>';
  $('sx-note').textContent='Each trail counts in the area holding most of its points'+(OUT.length?' · '+OUT.length+' of yours are outside every area':'')+
    ' · outlines are approximate and some boxes overlap a little at the edges.';
  drawMap();list();}
function list(){var html='';
  [['OR','Oregon'],['WA','Washington']].forEach(function(g){var rows='',n=0,d=0;
    SECS.forEach(function(s,k){if(s.st!==g[0])return;var dn=DONE[s.id],mine=BY[k]||[];n++;if(dn)d++;
      if(filter==='todo'&&dn||filter==='done'&&!dn||filter==='mine'&&!mine.length)return;
      rows+='<li class="sx-row'+(dn?' done':'')+(k===hot?' hot':'')+'" data-k="'+k+'"><input type="checkbox" data-k="'+k+'" aria-label="'+esc(s.n)+' done"'+(dn?' checked':'')+'>'+
        '<div><div class="nm">'+esc(s.n)+(mine.length?'<span class="chip">'+mine.length+' saved</span>':'')+'</div>'+
        '<div class="co">'+coords(s.b)+(dn?' · <em>done '+when(dn)+'</em>':'')+'</div></div>'+
        '<a class="at" href="'+link(s.b)+'" target="_blank" rel="noopener">Open on AllTrails ↗</a>'+
        (mine.length?'<details'+(openSet[s.id]?' open':'')+' data-id="'+s.id+'"><summary>Your '+mine.length+' saved trail'+(mine.length>1?'s':'')+'</summary><ul class="sx-trs">'+
          mine.map(function(t,j){return '<li class="sx-tr"><i style="background:'+colOf(t)+'"></i>'+(t.link?'<a href="'+esc(t.link)+'" target="_blank" rel="noopener">'+esc(t.name)+'</a>':esc(t.name))+
            ' <span>'+(t.mi!=null?t.mi+' mi':'')+(t.act==='mtb'?' · MTB':'')+'</span> <button type="button" data-k="'+k+'" data-j="'+j+'">Forecast</button></li>';}).join('')+
          '</ul></details>':'')+'</li>';});
    html+='<h2 class="sx-st">'+g[1]+'<b>'+d+' / '+n+' done</b></h2><ol class="sx-list">'+(rows||'<li class="sx-empty">Nothing here with this filter.</li>')+'</ol>';});
  $('sx-list').innerHTML=html;}
var openSet={};
function setHot(k){if(k===hot)return;hot=k;
  root.querySelectorAll('#sx-map .box').forEach(function(r){r.classList.toggle('hot',+r.dataset.k===k);});
  root.querySelectorAll('.sx-row').forEach(function(r){r.classList.toggle('hot',+r.dataset.k===k);});
  var r=root.querySelector('#sx-map .box[data-k="'+k+'"]');if(r)r.parentNode.insertBefore(r,r.parentNode.querySelector('.trl,.cnt'));}
function focusRow(k){if(filter!=='all'){filter='all';root.querySelectorAll('.sx-seg button').forEach(function(b){b.setAttribute('aria-pressed',String(b.dataset.f==='all'));});list();}
  setHot(k);var li=root.querySelector('.sx-row[data-k="'+k+'"]');
  if(li){li.scrollIntoView({block:'center',behavior:matchMedia('(prefers-reduced-motion: reduce)').matches?'auto':'smooth'});var c=li.querySelector('input');if(c)c.focus({preventScroll:true});}}

function build(){built=true;
  root.querySelector('.sx-seg').addEventListener('click',function(e){var b=e.target.closest('button[data-f]');if(!b)return;filter=b.dataset.f;
    root.querySelectorAll('.sx-seg button').forEach(function(x){x.setAttribute('aria-pressed',String(x===b));});list();});
  var L=$('sx-list');
  L.addEventListener('change',function(e){var c=e.target.closest('input[data-k]');if(c)setDone(SECS[+c.dataset.k].id,c.checked);});
  L.addEventListener('toggle',function(e){var d=e.target;if(d.dataset&&d.dataset.id){if(d.open)openSet[d.dataset.id]=1;else delete openSet[d.dataset.id];}},true);
  L.addEventListener('click',function(e){var b=e.target.closest('.sx-tr button');if(!b)return;var t=BY[+b.dataset.k][+b.dataset.j];
    if(t&&window.openTrailForecast)window.openTrailForecast(t.p,t.name,t.link);});
  L.addEventListener('mouseover',function(e){var r=e.target.closest('.sx-row');setHot(r?+r.dataset.k:-1);});
  L.addEventListener('mouseleave',function(){setHot(-1);});
  var M=$('sx-map');
  M.addEventListener('click',function(e){var r=e.target.closest('.box');if(r)focusRow(+r.dataset.k);});
  M.addEventListener('keydown',function(e){var r=e.target.closest('.box');if(r&&(e.key==='Enter'||e.key===' ')){e.preventDefault();focusRow(+r.dataset.k);}});
  render();loadDone();
  if(window.WxMine)window.WxMine.load();}   // the latest list from GitHub; WxMine.on redraws
// shown for the first time (showTab's data-init)
window.sectionsShown=function(){if(!built)build();};
if(window.WxMine)window.WxMine.on(function(){render();});
window.addEventListener('storage',function(e){if(!built)return;if(e.key==='wx-mytrails'&&window.WxMine)window.WxMine.load();if(e.key===LKEY){DONE=localGet();render();}});
})();
"""
