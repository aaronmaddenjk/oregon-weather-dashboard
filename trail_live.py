"""
Trail Forecast tab: drop in a GPX (AllTrails+, Gaia, CalTopo, Strava...) and get a forecast
for the trail's base (lowest point) and peak (highest point), computed live in the browser:

  - Open-Meteo (best-match models, downscaled to each point's GPX elevation) for the hourly
    forecast and the last 15 days, the NWS gridded forecast on top as the base (moved from its
    grid box to the real elevation with a standard lapse rate), exactly like the other tabs
  - exposed-ridge wind: the stronger of the surface model and GFS free-air wind x1.25
  - the same wet-bulb rain/snow split and snow-to-liquid ratios as snow_model.py
  - the 3D map carries the Map tab's layers and controls (RegionLayers, the same code and data),
    with the trail drawn on it; they cover Oregon and Washington

Nothing here runs at build time; the page carries only markup, styles and the JS engine.
A web page can't read AllTrails itself (no API, bot protection), so the link field only names the
trail. The Chrome extension in chrome-extension/ bridges that: clicked on an AllTrails trail page,
it reads the route that page loaded and opens this tab with it as #trail={"n","u","p": polyline}.
"""

TRAIL_PAGE_HTML = """
<div id="tl-root">
  <div class="tl-load" id="tl-load">
    <label class="tl-drop" id="tl-drop">
      <input type="file" id="tl-file" accept=".gpx,application/gpx+xml,application/xml,text/xml" hidden>
      <svg class="tl-drop-ic" viewBox="0 0 24 24" aria-hidden="true"><path d="M2.5 20 9 8.5l3.5 6 2.5-4L21.5 20z" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linejoin="round"/><path d="M12 2.5v6M9.5 6 12 8.5 14.5 6" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"/></svg>
      <span class="tl-drop-t"><b>Drop a GPX file here</b> or <u>choose one</u></span>
      <span class="tl-drop-s">AllTrails+ (⋮ → Download route → GPX), Gaia GPS, CalTopo or Strava all export GPX</span>
    </label>
    <div class="tl-linkrow"><input type="url" id="tl-link" placeholder="AllTrails link (optional — names the trail and links back to it)" aria-label="AllTrails link"></div>
  </div>
  <div class="tl-status" id="tl-status" hidden></div>
  <div id="tl-out" hidden>
    <div class="tl-head">
      <div><h2 id="tl-name"></h2><div class="tl-stats" id="tl-stats"></div></div>
      <div class="tl-head-r"><a id="tl-at" target="_blank" rel="noopener" hidden>AllTrails ↗</a><button class="tl-new" id="tl-new">Load another trail</button></div>
    </div>
    <div class="tl-row1">
      <div class="tl-fc">
        <div class="tl-sec"><h3>10-day forecast</h3><div class="cc-tiers tl-pick" role="group" aria-label="Point"></div></div>
        <div class="tl-10" id="tl-10"></div>
      </div>
      <div class="tl-card tl-prof"><div class="tl-card-t">Trail profile</div><div class="tl-card-s" id="tl-prof-s"></div><div id="tl-profile"></div></div>
    </div>
    <div class="dashboard-layout">
      <div class="map-panel"><div class="map-wrap trail-map-wrap"><div id="tlmap"></div>
        __REGION_CTL__
        <div class="legend" id="tl-map-note">The trail in orange · click Base or Peak for its forecast · the same layers as the Map tab</div></div></div>
      <div class="cc-panel" id="tl-cc"><div class="cc-head"><div><div class="cc-kicker">Next 24 hours</div><div class="cc-pick"><span id="tl-cc-name"></span></div><div class="cc-wp" id="tl-cc-wp"></div></div>
        <div class="cc-head-r"><div class="cc-now"></div></div></div>
        <div class="cc-tiers tl-pick" role="group" aria-label="Point"></div>
        <div class="cc-charts"></div><div class="cc-tip" hidden></div></div>
    </div>
    <p class="tl-foot" id="tl-foot"></p>
  </div>
</div>
"""

TRAIL_CSS = r"""
[hidden] { display:none !important; }
.tl-load { display:flex; flex-direction:column; gap:10px; max-width:760px; margin-bottom:24px; }
.tl-drop { display:flex; flex-direction:column; align-items:center; gap:6px; padding:34px 20px; border:2px dashed #C9CDD5; border-radius:12px; background:#fff; text-align:center; cursor:pointer; transition:border-color .15s, background .15s; }
.tl-drop:hover, .tl-drop.over { border-color:#FE5000; background:#FFF8F4; }
.tl-drop:focus-within { outline:2px solid #FE5000; outline-offset:2px; }
.tl-drop-ic { width:34px; height:34px; color:#FE5000; }
.tl-drop-t { font-size:15px; color:#111; }
.tl-drop-t u { color:#FE5000; text-decoration-thickness:1.5px; text-underline-offset:3px; }
.tl-drop-s { font-size:12px; color:#8A8F9C; }
.tl-linkrow input { width:100%; padding:9px 12px; border:1px solid #DDE0E6; border-radius:8px; font:inherit; font-size:13px; background:#fff; }
.tl-linkrow input:focus { outline:2px solid #FE5000; outline-offset:1px; border-color:transparent; }
.tl-status { margin:0 0 20px; padding:12px 14px; border-radius:10px; background:#fff; box-shadow:0 1px 4px rgba(0,0,0,.08); font-size:13px; color:#5A5F6B; }
.tl-status.err { color:#9B1C1C; background:#FDF1F1; box-shadow:inset 0 0 0 1px #F3C7C7; }
.tl-head { display:flex; justify-content:space-between; align-items:flex-end; gap:12px; flex-wrap:wrap; margin-bottom:18px; }
.tl-head h2 { margin:0; font-size:22px; font-weight:800; letter-spacing:-.01em; color:#111; }
.tl-stats { margin-top:4px; font-size:12.5px; color:#5A5F6B; font-variant-numeric:tabular-nums; }
.tl-stats b { color:#111; }
.tl-head-r { display:flex; align-items:center; gap:10px; }
.tl-head-r a { font-size:13px; font-weight:600; color:#5A5F6B; }
.tl-new { border:1px solid #DDE0E6; background:#fff; border-radius:8px; padding:7px 12px; font:inherit; font-size:13px; font-weight:600; color:#111; cursor:pointer; }
.tl-new:hover { border-color:#FE5000; color:#FE5000; }
.tl-sec { display:flex; align-items:center; justify-content:space-between; gap:10px; flex-wrap:wrap; margin-bottom:6px; }
.tl-sec h3 { margin:0; font-size:15px; font-weight:700; color:#111; }
.tl-pick { margin-top:0 !important; }
.tl-row1 { display:grid; grid-template-columns:minmax(0,3fr) minmax(0,2fr); gap:14px; align-items:start; margin-bottom:24px; }
.tl-card { background:#fff; border-radius:10px; padding:12px 14px; box-shadow:0 1px 4px rgba(0,0,0,.08); }
.tl-card-t { font-size:15px; font-weight:700; color:#111; }
.tl-card-s { margin-top:3px; font-size:11.5px; color:#8A8F9C; font-variant-numeric:tabular-nums; }
#tl-profile svg { display:block; width:100%; margin-top:8px; overflow:visible; }
#tl-profile text { font-family:inherit; font-size:10px; fill:#9A9FAB; font-variant-numeric:tabular-nums; }
.tl-sum { margin-bottom:28px; }
#tlmap { width:100%; height:600px; border-radius:8px; }
.tl-foot { margin:14px 0 0; font-size:11px; color:#9A9FAB; line-height:1.5; }
.tl-pin { background:#fff; border-radius:6px; padding:4px 9px; font-size:12px; font-weight:700; color:#111; box-shadow:0 2px 6px rgba(0,0,0,.25); border-left:3px solid #6BBF68; white-space:nowrap; cursor:pointer; }
.tl-pin small { font-weight:500; color:#8A8F9C; margin-left:4px; }
.tl-pin.peak { border-left-color:#FE5000; }
.tl-pin.active { box-shadow:0 0 0 2px #FE5000, 0 2px 6px rgba(0,0,0,.25); }
/* the 10-day table */
.tl-10 { overflow-x:auto; background:#fff; border-radius:10px; box-shadow:0 1px 4px rgba(0,0,0,.08); padding:8px; }
.tl-tbl { border-collapse:separate; border-spacing:0; white-space:nowrap; font-size:13px; }
.tl-tbl th { position:sticky; left:0; z-index:2; background:#fff; text-align:left; padding:7px 12px 7px 0; font-size:12px; color:#555; font-weight:600; min-width:96px; vertical-align:middle; }
.tl-tbl td { text-align:center; padding:7px 10px; min-width:78px; vertical-align:middle; border-left:1px solid #DDE0E6; }
.tl-tbl tr + tr td, .tl-tbl tr + tr th { border-top:1px solid #F0F1F4; }
.tl-tbl td.wkd { background:#F7F8FA; }
.tl-tbl .dh b { font-size:12.5px; font-weight:800; letter-spacing:.05em; text-transform:uppercase; color:#111; }
.tl-tbl .dh span { font-size:12.5px; font-weight:600; color:#8A8F9C; }
.tl-tbl .dh .tt { background:#FE5000; color:#fff; font-size:9px; font-weight:700; padding:1px 5px; border-radius:3px; margin-left:3px; vertical-align:middle; }
.tl-tbl .ds-ci { font-size:18px; margin-top:2px; }
.tl-tbl .snow-day { font-size:14px; font-weight:800; color:#5A4FCF; }
.tl-tbl .snow-none { color:#C9CDD5; }
.tl-tbl .trange { display:flex; flex-direction:column; align-items:center; gap:3px; }
.tl-tbl .tr-track { position:relative; width:8px; height:54px; border-radius:4px; background:#EEF0F3; }
.tl-tbl .tr-track i { position:absolute; left:0; right:0; border-radius:4px; }
.tl-tbl .th { font-size:13px; font-weight:700; color:#111; }
.tl-tbl .tl2 { font-size:12px; color:#8A8F9C; }
.tl-tbl .wv { font-weight:700; }
.tl-tbl .gust { font-size:10.5px; color:#8A8F9C; margin-left:3px; }
.tl-tbl .vis { font-size:11px; }
@media (max-width:1000px) { .tl-row1 { grid-template-columns:minmax(0,1fr); } #tlmap { height:460px; } }
"""

TRAIL_LIVE_JS = r"""
(function(){
var root=document.getElementById('tl-root');if(!root)return;
var OM='https://api.open-meteo.com/v1/forecast',NWS='https://api.weather.gov';
var PL=[850,700,600,500],GF=1.25,LAPSE=6.5*1.8/1000;
var $=function(id){return document.getElementById(id);};
var st={pts:null,res:null,sel:0,map:null,layers:null,charts:null};

// ---------- the snow model (same as snow_model.py) ----------
function rhFromDew(t,d){t=(t-32)/1.8;d=(d-32)/1.8;return 100*Math.exp(17.625*d/(243.04+d))/Math.exp(17.625*t/(243.04+t));}
function wetBulb(tf,rh){var t=(tf-32)/1.8;rh=Math.min(100,Math.max(5,rh));
  var tw=t*Math.atan(0.151977*Math.sqrt(rh+8.313659))+Math.atan(t+rh)-Math.atan(rh-1.676331)+0.00391838*Math.pow(rh,1.5)*Math.atan(0.023101*rh)-4.686035;return tw*1.8+32;}
function snowFrac(tw){return tw<=32?1:tw>=35?0:(35-tw)/3;}
var SLR=[[10,15],[20,13],[26,11],[30,9],[34,7]];
function slr(t){if(t<=SLR[0][0])return SLR[0][1];for(var i=1;i<SLR.length;i++)if(t<=SLR[i][0]){var a=SLR[i-1],b=SLR[i];return a[1]+(b[1]-a[1])*(t-a[0])/(b[0]-a[0]);}return SLR[SLR.length-1][1];}
function newSnow(p,t,rh){if(!p||t==null)return[0,0];var f=snowFrac(wetBulb(t,rh==null?90:rh));return[p*f*slr(t),f];}
function interp(anchors,z,below){   // linear in elevation between anchors; `below` extrapolates under the lowest
  if(z<=anchors[0][0])return below(anchors[0][0],anchors[0][1],z);
  for(var i=1;i<anchors.length;i++)if(z<=anchors[i][0]){var a=anchors[i-1],b=anchors[i];return a[1]+(b[1]-a[1])*(z-a[0])/(b[0]-a[0]);}
  return anchors[anchors.length-1][1];}
var holdBelow=function(z0,v0){return v0;};

// ---------- GPX ----------
function km(a,b){var R=6371,p1=a.lat*Math.PI/180,p2=b.lat*Math.PI/180,dl=(b.lon-a.lon)*Math.PI/180;
  var h=Math.sin((p2-p1)/2)*Math.sin((p2-p1)/2)+Math.cos(p1)*Math.cos(p2)*Math.sin(dl/2)*Math.sin(dl/2);return 2*R*Math.asin(Math.sqrt(h));}
function parseGPX(text){
  var doc=new DOMParser().parseFromString(text,'application/xml');
  if(doc.getElementsByTagName('parsererror').length)throw new Error('That file isn’t valid GPX.');
  var tags=['trkpt','rtept'],nodes=[];
  for(var k=0;k<tags.length&&!nodes.length;k++)nodes=[].slice.call(doc.getElementsByTagName(tags[k]));
  var pts=nodes.map(function(n){var e=n.getElementsByTagName('ele')[0];return{lat:+n.getAttribute('lat'),lon:+n.getAttribute('lon'),ele:e?parseFloat(e.textContent):null};})
    .filter(function(p){return isFinite(p.lat)&&isFinite(p.lon);});
  if(pts.length<2)throw new Error('No track found in that GPX file.');
  var nm=doc.getElementsByTagName('name')[0];
  return{pts:pts,name:nm?nm.textContent.trim():''};}
function decodePolyline(str){   // Google encoded polyline (precision 5) -> [{lat,lon,ele:null}]
  var i=0,lat=0,lng=0,out=[];
  while(i<str.length){var b,sh=0,res=0;do{b=str.charCodeAt(i++)-63;res|=(b&31)<<sh;sh+=5;}while(b>=32);lat+=(res&1)?~(res>>1):(res>>1);
    sh=0;res=0;do{b=str.charCodeAt(i++)-63;res|=(b&31)<<sh;sh+=5;}while(b>=32);lng+=(res&1)?~(res>>1):(res>>1);
    out.push({lat:lat/1e5,lon:lng/1e5,ele:null});}
  if(out.length<2)throw new Error('That route came through empty.');
  return out;}
function nameFromLink(u){var m=/alltrails\.com\/(?:[a-z-]+\/)?trail\/[^/]+\/[^/]+\/([^/?#]+)/i.exec(u||'');
  return m?m[1].split('-').map(function(w){return w?w[0].toUpperCase()+w.slice(1):w;}).join(' '):'';}
async function fillElevation(pts){   // a route without elevations: sample up to 300 points (100 per request), interpolate between
  if(pts.every(function(p){return p.ele!=null&&isFinite(p.ele);}))return;
  var n=Math.min(300,pts.length),idx=[];for(var i=0;i<n;i++)idx.push(Math.round(i*(pts.length-1)/Math.max(1,n-1)));
  var batches=[];for(var b0=0;b0<idx.length;b0+=100)batches.push(idx.slice(b0,b0+100));
  var got=await Promise.all(batches.map(function(bt){return fetch('https://api.open-meteo.com/v1/elevation?latitude='+bt.map(function(i){return pts[i].lat.toFixed(5);}).join(',')+'&longitude='+bt.map(function(i){return pts[i].lon.toFixed(5);}).join(',')).then(function(r){return r.json();});}));
  var el=[].concat.apply([],got.map(function(g){return g.elevation;}));
  pts.forEach(function(p,i){if(p.ele!=null&&isFinite(p.ele))return;var k=0;while(k<idx.length-1&&idx[k+1]<i)k++;
    var a=idx[k],b=idx[Math.min(k+1,idx.length-1)],f=b>a?(i-a)/(b-a):0;p.ele=el[k]+((el[Math.min(k+1,el.length-1)]-el[k])*f);});}
function trailStats(pts){
  var d=0,gain=0,last=pts[0].ele,dist=[0];
  for(var i=1;i<pts.length;i++){d+=km(pts[i-1],pts[i]);dist.push(d);
    var dz=pts[i].ele-last;if(Math.abs(dz)>=10){if(dz>0)gain+=dz;last=pts[i].ele;}}   // 10 m hysteresis: GPS and elevation-model noise isn't climbing
  var lo=0,hi=0;pts.forEach(function(p,i){if(p.ele<pts[lo].ele)lo=i;if(p.ele>pts[hi].ele)hi=i;});
  return{km:d,dist:dist,gain:gain,lo:lo,hi:hi};}

// ---------- fetching ----------
async function getJSON(u){var r=await fetch(u);if(!r.ok)throw new Error(r.status+' from '+u.split('?')[0]);return r.json();}
function q(o){return Object.keys(o).map(function(k){return k+'='+encodeURIComponent(o[k]);}).join('&');}
async function nwsPoint(p){   // NWS gridded forecast at one point, or null (outside the US, or down)
  try{var meta=await getJSON(NWS+'/points/'+p.lat.toFixed(4)+','+p.lon.toFixed(4));
    var g=(await getJSON(meta.properties.forecastGridData)).properties;return g;}catch(e){return null;}}
function nwsHourly(g,off){   // -> {elev, hourly:{'YYYY-MM-DDTHH:00': {temp_f,...}}} in the forecast's local time
  if(!g)return null;
  var F={temperature:['temp_f',function(c){return c*9/5+32;},0],dewpoint:['dew_f',function(c){return c*9/5+32;},0],
    windSpeed:['wind',function(k){return k/1.609344;},0],windGust:['gust',function(k){return k/1.609344;},0],
    skyCover:['sky',function(v){return v;},0],probabilityOfPrecipitation:['pop',function(v){return v;},0],
    quantitativePrecipitation:['qpf',function(mm){return mm/25.4;},1]};
  var out={};
  Object.keys(F).forEach(function(f){var spec=F[f];((g[f]||{}).values||[]).forEach(function(v){
    if(v.value==null)return;var parts=v.validTime.split('/'),t0=Date.parse(parts[0]);
    var m=/P(?:(\d+)D)?(?:T(?:(\d+)H)?)?/.exec(parts[1]),n=Math.max(1,(+(m[1]||0))*24+(+(m[2]||0)));
    for(var k=0;k<n;k++){var key=new Date(t0+k*3600000+off*1000).toISOString().slice(0,13)+':00';
      (out[key]=out[key]||{})[spec[0]]=spec[2]?spec[1](v.value)/n:spec[1](v.value);}});});
  return{elev:(g.elevation||{}).value||0,hourly:out};}

// ---------- the forecast ----------
function label(t){var d=new Date(t+':00Z'),h=d.getUTCHours();return['Sun','Mon','Tue','Wed','Thu','Fri','Sat'][d.getUTCDay()]+' '+((h%12)||12)+(h<12?'AM':'PM');}
function freeAir(G,i){var fa=[];PL.forEach(function(p){var Z=(G['geopotential_height_'+p+'hPa']||[])[i],W=(G['wind_speed_'+p+'hPa']||[])[i];if(Z!=null&&W!=null)fa.push([Z,W]);});
  return fa.sort(function(a,b){return a[0]-b[0];});}
function windAt(fa,z,surface,factor){if(!fa.length)return surface;var expo=Math.min(1,Math.max(0,(z-(fa[0][0]-500))/500));return Math.max(surface,expo*interp(fa,z,holdBelow)*factor);}

async function run(pts,name,link){
  status('Reading the trail…');
  await fillElevation(pts);
  var s=trailStats(pts),P=[{key:'base',name:'Base',p:pts[s.lo]},{key:'peak',name:'Peak',p:pts[s.hi]}];
  var mid={lat:(P[0].p.lat+P[1].p.lat)/2,lon:(P[0].p.lon+P[1].p.lon)/2};
  status('Fetching the forecast for the base ('+ft(P[0].p.ele)+') and peak ('+ft(P[1].p.ele)+')…');
  var hourly=['temperature_2m','dew_point_2m','precipitation','precipitation_probability','cloud_cover','visibility','wind_speed_10m','wind_gusts_10m','snow_depth'];
  var common={temperature_unit:'fahrenheit',wind_speed_unit:'mph',precipitation_unit:'inch',timezone:'auto'};
  var gfsVars=[];PL.forEach(function(p){['temperature','geopotential_height','relative_humidity','wind_speed'].forEach(function(v){gfsVars.push(v+'_'+p+'hPa');});});
  var got=await Promise.all([
    getJSON(OM+'?'+q(Object.assign({latitude:P[0].p.lat+','+P[1].p.lat,longitude:P[0].p.lon+','+P[1].p.lon,elevation:P[0].p.ele+','+P[1].p.ele,
      hourly:hourly.join(','),past_days:15,forecast_days:11},common))),
    getJSON(OM+'?'+q(Object.assign({latitude:mid.lat,longitude:mid.lon,models:'gfs_global',forecast_days:8,
      hourly:['temperature_2m','relative_humidity_2m','wind_gusts_10m'].concat(gfsVars).join(',')},common))),
    nwsPoint(P[0].p),nwsPoint(P[1].p)]);
  var om=got[0],G=got[1],off=om[0].utc_offset_seconds,tz=om[0].timezone;
  var gi={};G.hourly.time.forEach(function(t,i){gi[t]=i;});
  var nowKey=new Date(Date.now()+off*1000).toISOString().slice(0,13)+':00';
  P.forEach(function(pt,k){
    var H=om[k].hourly,n=nwsHourly(got[2+k],off),z=pt.p.ele,shift=n?LAPSE*(n.elev-z):0;
    pt.nws=!!n;
    pt.hours=H.time.map(function(t,i){
      var v=n&&n.hourly[t]||{},T=H.temperature_2m[i],D=H.dew_point_2m[i];
      if(v.temp_f!=null){T=v.temp_f+shift;if(v.dew_f!=null)D=v.dew_f+shift;}
      var h={t:t,temp:T,dew:D,wind:v.wind!=null?v.wind:H.wind_speed_10m[i],gust:v.gust!=null?v.gust:H.wind_gusts_10m[i],
        sky:v.sky!=null?v.sky:H.cloud_cover[i],pop:v.pop!=null?v.pop:H.precipitation_probability[i],
        p:v.qpf!=null?v.qpf:(H.precipitation[i]||0),vis:H.visibility[i]==null?null:H.visibility[i]/1609.34,depth:(H.snow_depth[i]||0)*39.37};
      var j=gi[t];if(j!=null){var fa=freeAir(G.hourly,j);h.gust=Math.max(h.gust||0,windAt(fa,z,0,GF));h.wind=Math.max(h.wind||0,windAt(fa,z,0,1));}
      var r=newSnow(h.p,T,T!=null&&D!=null?rhFromDew(T,Math.min(D,T)):null);h.s=r[0];h.ty=h.p<0.005?'':r[1]>=0.8?'snow':r[1]>0.2?'mix':'rain';
      return h;});
    pt.now=Math.max(0,pt.hours.findIndex(function(h){return h.t>=nowKey;}));
    pt.h24=pt.hours.slice(pt.now,pt.now+24).map(function(h){return{t:label(h.t),temp:Math.round(h.temp),wind:Math.round(h.wind||0),gust:Math.round(h.gust||0),
      sky:Math.round(h.sky||0),p:+h.p.toFixed(3),s:+h.s.toFixed(2),ty:h.ty,vis:h.vis==null?null:+h.vis.toFixed(2)};});
  });
  st.pts=pts;st.stats=s;st.P=P;st.tz=tz;st.off=off;st.nowKey=nowKey;
  st.name=name;st.link=link;
  render();
}

// ---------- rendering ----------
function ft(m){return Math.round(m*3.28084).toLocaleString('en-US')+'′';}
function status(msg,err){var el=$('tl-status');el.textContent=msg;el.className='tl-status'+(err?' err':'');el.hidden=!msg;}
function tempBg(t){return t>=80?'#ED1E29':t>=70?'#FAA21B':t>=50?'#6BBF68':t>=35?'#4FB1BE':'#368994';}
function windCol(w){return w>=30?'#ED1E29':w>=20?'#FAA21B':'#111';}
function visCol(m){return m==null?'#CCC':m<1?'#D11A24':m<3?'#C9760A':m<6?'#8C7440':'#888';}
function fmtVis(m){return m==null?'--':m>=10?'10+ mi':m>=1?m.toFixed(1)+' mi':m.toFixed(2)+' mi';}
function inch(v){return v<0.05?'0':v>=10?Math.round(v)+'″':v.toFixed(1)+'″';}
function icon(ents){   // the same condition icon rule as the other tables
  var clouds=ents.reduce(function(a,h){return a+(h.sky||0);},0)/ents.length,chance=Math.max.apply(null,ents.map(function(h){return h.pop||0;}));
  var rain=ents.reduce(function(a,h){return a+h.p;},0),snow=ents.reduce(function(a,h){return a+h.s;},0);
  var kind=snow>=0.2?'snow':rain>=0.1&&chance>=50?'rain':rain>=0.02&&chance>=30?'showers':clouds<10?'clear':clouds<40?'mostly':clouds<75?'partly':'cloudy';
  var windy=Math.max.apply(null,ents.map(function(h){return h.wind||0;}))>=20||Math.max.apply(null,ents.map(function(h){return h.gust||0;}))>=35;
  return'<span class="wxi"><svg class="wx"><use href="#wx-'+kind+'"/></svg>'+(windy?'<svg class="wx wx-wind"><use href="#wx-wind"/></svg>':'')+'</span>';}
function days(pt){   // the forecast hours from now, by local date, 10 days
  var out=[],by={};pt.hours.slice(pt.now).forEach(function(h){var d=h.t.slice(0,10);if(!by[d]){by[d]=[];out.push(d);}by[d].push(h);});
  return out.slice(0,10).map(function(d){return{date:d,h:by[d]};});}
function table(pt){
  var D=days(pt),lo=Infinity,hi=-Infinity;D.forEach(function(d){d.h.forEach(function(h){lo=Math.min(lo,h.temp);hi=Math.max(hi,h.temp);});});lo-=2;hi+=2;
  var R={time:'',snow:'',temp:'',wind:'',vis:'',chance:''};
  D.forEach(function(d,di){var H=d.h,dt=new Date(d.date+'T12:00:00Z'),wk=dt.getUTCDay()===0||dt.getUTCDay()===6?' class="wkd"':'';
    var tmax=Math.max.apply(null,H.map(function(h){return h.temp;})),tmin=Math.min.apply(null,H.map(function(h){return h.temp;}));
    var mw=Math.max.apply(null,H.map(function(h){return h.wind||0;})),mg=Math.max.apply(null,H.map(function(h){return h.gust||0;}));
    var vs=H.map(function(h){return h.vis;}).filter(function(v){return v!=null;}),mv=vs.length?Math.min.apply(null,vs):null;
    var sn=H.reduce(function(a,h){return a+h.s;},0),mp=Math.max.apply(null,H.map(function(h){return h.pop||0;}));
    var lvl=mp===0?0:mp<30?1:mp<60?2:3;
    R.time+='<td'+wk+'><div class="dh"><b>'+['Sun','Mon','Tue','Wed','Thu','Fri','Sat'][dt.getUTCDay()]+'</b> <span>'+dt.getUTCDate()+'</span>'+(di===0?' <span class="tt">TODAY</span>':'')+'</div><div class="ds-ci">'+icon(H)+'</div></td>';
    R.snow+='<td'+wk+'>'+(sn>=0.05?'<span class="snow-day">'+inch(sn)+'</span>':'<span class="snow-none">0</span>')+'</td>';
    R.temp+='<td'+wk+'><div class="trange"><span class="th">'+Math.round(tmax)+'°</span><div class="tr-track"><i style="top:'+((hi-tmax)/(hi-lo)*100).toFixed(0)+'%;bottom:'+((tmin-lo)/(hi-lo)*100).toFixed(0)+'%;background:linear-gradient('+tempBg(tmax)+','+tempBg(tmin)+')"></i></div><span class="tl2">'+Math.round(tmin)+'°</span></div></td>';
    R.wind+='<td'+wk+'><span class="wv" style="color:'+windCol(mw)+'">'+Math.round(mw)+'</span><span class="gust">g'+Math.round(mg)+'</span></td>';
    R.vis+='<td'+wk+'><span class="vis" style="color:'+visCol(mv)+'">'+fmtVis(mv)+'</span></td>';
    R.chance+='<td'+wk+'><svg class="pd pd'+lvl+'"><use href="#wx-pdrop"/></svg> '+Math.round(mp)+'%</td>';});
  var ri=function(n){return'<svg class="rl" aria-hidden="true"><use href="#ri-'+n+'"/></svg>';};
  return'<table class="tl-tbl"><tr><th></th>'+R.time+'</tr><tr><th>'+ri('flake')+'New snow</th>'+R.snow+'</tr><tr><th>'+ri('temp')+'High / low</th>'+R.temp+'</tr>'
    +'<tr><th>'+ri('wind')+'Wind, gust</th>'+R.wind+'</tr><tr><th>'+ri('eye')+'Visibility</th>'+R.vis+'</tr><tr><th>'+ri('chance')+'Chance</th>'+R.chance+'</tr></table>';}
function profile(){   // elevation vs distance, base and peak marked
  var p=st.pts,s=st.stats,W=Math.max(260,$('tl-profile').clientWidth||420),H=170,L=46,B=20,T=10,mi=s.km*0.621371;
  var zs=p.map(function(x){return x.ele*3.28084;}),z0=Math.min.apply(null,zs),z1=Math.max.apply(null,zs),pad=Math.max(50,(z1-z0)*0.08);z0-=pad;z1+=pad;
  var X=function(k){return L+(s.dist[k]/s.km)*(W-L-6);},Y=function(z){return T+(1-(z-z0)/(z1-z0))*(H-T-B);},step=Math.max(1,Math.floor(p.length/600));
  var d='M'+X(0).toFixed(1)+','+Y(zs[0]).toFixed(1);for(var k=step;k<p.length;k+=step)d+='L'+X(k).toFixed(1)+','+Y(zs[k]).toFixed(1);
  var area=d+'L'+X(p.length-1).toFixed(1)+','+(H-B)+'L'+L+','+(H-B)+'Z',ticks=[z0+pad,(z0+z1)/2,z1-pad];
  var o='<svg viewBox="0 0 '+W+' '+H+'" height="'+H+'">'+ticks.map(function(z){return'<line x1="'+L+'" x2="'+(W-6)+'" y1="'+Y(z)+'" y2="'+Y(z)+'" stroke="#F0F1F4"/><text x="'+(L-6)+'" y="'+(Y(z)+3.5)+'" text-anchor="end">'+Math.round(z/10)*10+'′</text>';}).join('')
    +'<path d="'+area+'" fill="#FE5000" fill-opacity=".1"/><path d="'+d+'" fill="none" stroke="#FE5000" stroke-width="2" stroke-linejoin="round"/>'
    +[[s.lo,'#6BBF68','Base'],[s.hi,'#FE5000','Peak']].map(function(m){return'<circle cx="'+X(m[0])+'" cy="'+Y(zs[m[0]])+'" r="4.5" fill="'+m[1]+'" stroke="#fff" stroke-width="1.5"/><text x="'+X(m[0])+'" y="'+(Y(zs[m[0]])-9)+'" text-anchor="middle" style="fill:#111;font-weight:700">'+m[2]+'</text>';}).join('')
    +'<text x="'+L+'" y="'+(H-4)+'">0 mi</text><text x="'+(W-6)+'" y="'+(H-4)+'" text-anchor="end">'+mi.toFixed(1)+' mi</text></svg>';
  $('tl-profile').innerHTML=o;}
function pick(i){
  st.sel=i;var pt=st.P[i];
  root.querySelectorAll('.tl-pick').forEach(function(g){g.innerHTML=st.P.map(function(x,j){return'<button class="'+(j===i?'active':'')+'" data-t="'+j+'">'+x.name+'<small>'+ft(x.p.ele)+'</small></button>';}).join('');});
  $('tl-10').innerHTML=table(pt);
  $('tl-cc-name').textContent=st.name||'Trail';$('tl-cc-wp').textContent=pt.name+' · '+ft(pt.p.ele);
  st.charts.set(pt.h24);
  root.querySelectorAll('.tl-pin').forEach(function(el,j){el.classList.toggle('active',j===i);});}
function render(){
  var s=st.stats,P=st.P;status('');
  $('tl-load').hidden=true;$('tl-out').hidden=false;
  $('tl-name').textContent=st.name||'Your trail';
  $('tl-stats').innerHTML='<b>'+(s.km*0.621371).toFixed(1)+' mi</b> · <b>'+Math.round(s.gain*3.28084).toLocaleString('en-US')+'′</b> gain · base <b>'+ft(P[0].p.ele)+'</b> · peak <b>'+ft(P[1].p.ele)+'</b>';
  var at=$('tl-at');at.hidden=!st.link;if(st.link)at.href=st.link;
  $('tl-prof-s').textContent=(s.km*0.621371).toFixed(1)+' mi · '+ft(P[0].p.ele)+' to '+ft(P[1].p.ele)+' · '+st.pts.length.toLocaleString('en-US')+' GPX points';
  $('tl-foot').textContent=(P.every(function(x){return x.nws;})?'National Weather Service forecast as the base':'Open-Meteo forecast (the NWS covers the US only)')
    +', moved to each point’s elevation · exposed-ridge wind from GFS free-air winds · wet-bulb rain/snow and snow-to-liquid ratios as on the other tabs · computed in your browser, not saved anywhere but this browser';
  if(!st.charts)st.charts=WxCharts($('tl-cc'),{vis:true});
  pick(0);profile();drawMap();}
function drawMap(){
  if(!window.mapboxgl||!$('tlmap').clientWidth)return;   // the tab isn't visible: drawn when it's shown
  if(st.region){st.region.remove();st.region=null;}
  var pts=st.pts,line={type:'Feature',geometry:{type:'LineString',coordinates:pts.map(function(p){return[p.lon,p.lat];})}};
  var b=new mapboxgl.LngLatBounds();pts.forEach(function(p){b.extend([p.lon,p.lat]);});
  var RB=window.REGION_BOUNDS,inside=RB&&b.getWest()<RB[2]&&b.getEast()>RB[0]&&b.getSouth()<RB[3]&&b.getNorth()>RB[1];
  $('tl-map-note').textContent=inside?'The trail in orange · click Base or Peak for its forecast · the same layers as the Map tab'
    :'The map layers cover Oregon and Washington; this trail is outside them, so the map shows the terrain and the route only';
  if(!window.RegionLayers)return;
  // the Map tab's layers and controls, framed on the trail; the route and the two points on top
  st.region=window.RegionLayers({wrap:$('tlmap').parentNode,container:'tlmap',bounds:b,fit:{padding:{top:130,bottom:50,left:50,right:50},pitch:60},
    onLoad:function(map){
      map.addSource('tl-line',{type:'geojson',data:line});
      map.addLayer({id:'tl-casing',type:'line',source:'tl-line',layout:{'line-cap':'round','line-join':'round'},paint:{'line-color':'#fff','line-width':6,'line-opacity':0.9}});
      map.addLayer({id:'tl-route',type:'line',source:'tl-line',layout:{'line-cap':'round','line-join':'round'},paint:{'line-color':'#FE5000','line-width':3.5}});
      st.P.forEach(function(pt,j){var el=document.createElement('div');el.className='tl-pin'+(j?' peak':'')+(j===st.sel?' active':'');
        el.innerHTML=pt.name+'<small>'+ft(pt.p.ele)+'</small>';el.addEventListener('click',function(){pick(j);});
        new mapboxgl.Marker({element:el,anchor:'bottom',offset:[0,-4]}).setLngLat([pt.p.lon,pt.p.lat]).addTo(map);});
    }});
  st.map=st.region.map;}

// ---------- input ----------
// src: {gpx: text} (a dropped file) or {poly, name} (a route handed over by the Chrome extension)
async function load(src,link,save){
  try{
    var g=src.poly?{pts:decodePolyline(src.poly),name:src.name||''}:parseGPX(src.gpx),name=src.name||nameFromLink(link)||g.name||'Your trail';
    $('tl-link').value=link||'';
    await run(g.pts,name,link||'');
    if(save){try{localStorage.setItem('wx-trail',JSON.stringify(Object.assign({},src,{link:link||''})));}catch(e){}}
  }catch(e){status((e&&e.message)||'Something went wrong loading that trail.',true);$('tl-load').hidden=false;$('tl-out').hidden=true;}}
function takeFile(f){if(!f)return;var r=new FileReader();r.onload=function(){load({gpx:r.result},$('tl-link').value.trim(),true);};r.readAsText(f);}
var drop=$('tl-drop');
$('tl-file').addEventListener('change',function(){takeFile(this.files[0]);this.value='';});
['dragenter','dragover'].forEach(function(ev){drop.addEventListener(ev,function(e){e.preventDefault();drop.classList.add('over');});});
['dragleave','drop'].forEach(function(ev){drop.addEventListener(ev,function(e){e.preventDefault();drop.classList.remove('over');});});
drop.addEventListener('drop',function(e){takeFile(e.dataTransfer.files[0]);});
$('tl-new').addEventListener('click',function(){$('tl-out').hidden=true;$('tl-load').hidden=false;status('');});
root.addEventListener('click',function(e){var b=e.target.closest('.tl-pick [data-t]');if(b)pick(+b.dataset.t);});
window.addEventListener('resize',function(){if(st.pts&&!$('tl-out').hidden)profile();});
// shown for the first time: bring back the last trail from this browser, and draw its map
// a route handed over in the URL by the Chrome extension: #trail={"n":name,"u":link,"p":polyline}
var pending=null;
if(location.hash.indexOf('#trail=')===0){
  try{var h=JSON.parse(decodeURIComponent(location.hash.slice(7)));if(h&&h.p)pending={src:{poly:h.p,name:h.n||''},link:h.u||''};}catch(e){}
  history.replaceState(null,'',location.pathname+location.search);   // a reload shouldn't re-import it
  window.addEventListener('load',function(){if(window.showTab)showTab(4);});
}
var restored=false;
window.trailLiveShown=function(){
  if(pending){var pd=pending;pending=null;restored=true;load(pd.src,pd.link,true);return;}
  if(!restored){restored=true;var saved=null;try{saved=JSON.parse(localStorage.getItem('wx-trail')||'null');}catch(e){}
    if(saved&&(saved.gpx||saved.poly)){load(saved,saved.link,false);return;}}
  if(st.pts&&!st.region)setTimeout(drawMap,50);
};
})();
"""
