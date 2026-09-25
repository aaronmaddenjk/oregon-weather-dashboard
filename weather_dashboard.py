"""
Oregon weather dashboard — generates docs/index.html.

Ported from weather_notebook_original.txt (Databricks). Removed:
  - the "Interactive Mapbox Explorer" cell (exploratory tool, not needed here)
  - the final "Export Notebook as Text File" cell (Databricks WorkspaceClient only)
  - the "7-Day AI Forecast Summary" card (called a Databricks-hosted Llama
    endpoint via mlflow.deployments, which doesn't exist outside Databricks)
  - all displayHTML(...) calls — we build strings and write a file instead

Data: ECMWF IFS HRES 9 km base, NOAA NBM temp/wind/gusts (replaced Meteoblue),
HRRR for today, WeatherNext 2 ensemble for predictability — all via Open-Meteo,
no keys needed. Only MAPBOX_TOKEN is required.

Run locally:
    pip install -r requirements.txt
    cp .env.example .env   # fill in your real keys
    python weather_dashboard.py
"""
import html
import itertools
import json
import math
import os
import re
import threading
import time
import xml.etree.ElementTree as ET
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv

import http_cache
http_cache.install()  # local runs reuse API responses for 3 h; off in GitHub Actions

import nws
import region
import snow_model
import verification
import fires
import smoke
import webcams
import snowpack
import trail_live
from snow_model import new_snow_in, rh_from_dew

load_dotenv()  # no-op in CI; GitHub Actions injects env vars directly

MAPBOX_TOKEN = os.environ["MAPBOX_TOKEN"]

OUTPUT_PATH = "docs/index.html"
FETCH_WORKERS = 4  # locations fetched in parallel; kept low to stay polite to Open-Meteo's rate limits


def nbm_overlay(h, lat, lon, ev, days):
    """Overwrite temp/wind/gusts in an hourly forecast dict with NOAA's National Blend
    of Models (2.5 km, statistically calibrated against observations). Replaces the
    old Meteoblue MOS overlay. Leaves `h` untouched for any hour NBM doesn't cover."""
    keys = ["temperature_2m", "wind_speed_10m", "wind_gusts_10m"]
    try:
        nb = requests.get("https://api.open-meteo.com/v1/forecast", params={
            "latitude": lat, "longitude": lon, "hourly": keys, "models": "ncep_nbm_conus",
            "temperature_unit": "fahrenheit", "wind_speed_unit": "mph", "timezone": "auto",
            "forecast_days": days, **ev}, timeout=30).json().get("hourly", {})
    except Exception as exc:
        print(f"  WARNING: NBM unavailable ({exc}) - keeping ECMWF temp/wind")
        return
    idx = {t: i for i, t in enumerate(nb.get("time", []))}
    for i, t in enumerate(h["time"]):
        j = idx.get(t)
        if j is None:
            continue
        for k in keys:
            v = (nb.get(k) or [None] * (j + 1))[j]
            if v is not None:
                h[k][i] = v


def ensemble_predictability(lat, lon, days):
    """Per-day predictability (0-100, keyed "YYYY-MM-DD") from Google DeepMind's
    WeatherNext 2 64-member ensemble: how tightly the members agree on the day's
    high temperature (55%) and on whether it rains (45%). {} if unavailable."""
    try:
        d = requests.get("https://ensemble-api.open-meteo.com/v1/ensemble", params={
            "latitude": lat, "longitude": lon, "hourly": ["temperature_2m", "precipitation"],
            "models": "google_weathernext2_ensemble", "temperature_unit": "fahrenheit",
            "precipitation_unit": "inch", "timezone": "auto", "forecast_days": days}, timeout=30).json()
        h = d["hourly"]
    except Exception as exc:
        print(f"  WARNING: WeatherNext ensemble unavailable ({exc}) - no predictability badges")
        return {}
    t_keys = [k for k in h if k.startswith("temperature_2m")]
    p_keys = [k for k in h if k.startswith("precipitation")]
    by_day = {}
    for i, t in enumerate(h["time"]):
        by_day.setdefault(t[:10], []).append(i)
    out = {}
    for day, ix in by_day.items():
        highs = [max(v for v in (h[k][i] for i in ix) if v is not None) for k in t_keys
                 if any(h[k][i] is not None for i in ix)]
        totals = [sum(h[k][i] or 0 for i in ix) for k in p_keys]
        if len(highs) < 2 or not totals:
            continue
        mean = sum(highs) / len(highs)
        sd = (sum((x - mean) ** 2 for x in highs) / len(highs)) ** 0.5
        temp_score = min(1, max(0, 1 - (sd - 1) / 6))          # 1°F spread -> 1, 7°F -> 0
        wet = sum(p >= 0.02 for p in totals) / len(totals)
        rain_score = abs(wet - 0.5) * 2                           # all agree -> 1, 50/50 split -> 0
        out[day] = round(100 * (0.55 * temp_score + 0.45 * rain_score) / 5) * 5
    return out


PROFILE_ELEVS_M = list(range(0, 4501, 150))  # elevation ladder for the 3D map layers

# ---- Base forecast: NWS, with our model blend as the fallback / background challenger ----

def merge_qpf(nws_pt, blend_series):
    """Hourly precip {time: in}: the NWS forecasters' QPF wherever they have it, our blend
    for any hour they don't (e.g. past the end of their 7-day grid)."""
    out = dict(blend_series or {})
    for t, v in ((nws_pt or {}).get("hourly") or {}).items():
        if "qpf_in" in v:
            out[t] = v["qpf_in"]
    return out


def nws_overlay(h, n, tshift):
    """Put the NWS forecast onto an Open-Meteo-style hourly dict for one location.
    tshift(time) is the °F to add to the NWS grid-box temperature to get this spot's
    (box elevation -> real elevation); dew point moves with it, keeping the depression."""
    if not n:
        return
    nh = n["hourly"]
    for i, t in enumerate(h["time"]):
        v = nh.get(t)
        if not v:
            continue
        if "temp_f" in v:
            s = tshift(t)
            h["temperature_2m"][i] = v["temp_f"] + s
            if "dew_f" in v:
                h["dew_point_2m"][i] = v["dew_f"] + s
        for key, col in (("wind_mph", "wind_speed_10m"), ("gust_mph", "wind_gusts_10m"), ("pop", "precipitation_probability"),
                         ("sky", "cloud_cover"), ("qpf_in", "precipitation")):
            if key in v and col in h:
                h[col][i] = v[key]


# ---- Mountain snow model: physics + blend weights live in snow_model.py ----

def blended_qpf(points, days):
    """Hourly precipitation (in) per point {time: inches}, blended across the
    (SNOTEL-verified) model weights in snow_model."""
    per_model = {}
    for m in snow_model.DEFAULT_WEIGHTS:
        try:
            d = requests.get("https://api.open-meteo.com/v1/forecast", params={
                "latitude": ",".join(str(p["lat"]) for p in points),
                "longitude": ",".join(str(p["lon"]) for p in points),
                "hourly": "precipitation", "models": m, "precipitation_unit": "inch",
                "timezone": "auto", "forecast_days": days}, timeout=60).json()
            locs = d if isinstance(d, list) else [d]
            per_model[m] = [dict(zip(l["hourly"]["time"], l["hourly"]["precipitation"])) for l in locs]
        except Exception:
            continue  # a missing model just drops out of the blend
    out = []
    for pi in range(len(points)):
        series = {}
        for t in sorted({t for m in per_model for t, v in per_model[m][pi].items() if v is not None}):
            series[t] = snow_model.blend({m: per_model[m][pi].get(t) for m in per_model})
        out.append(series)
    return out


def elevation_profile(waypoints, qpf, days=7, nws_pts=None):
    """Forecast across a ladder of elevations at the summit, which moves each mountain
    waypoint's temperature and wind to its own elevation - the same calculation as the Map tab (region.vertical_profile: NBM at the surface
    joined to GFS upper-air temperature/humidity/wind), evaluated at the mountain
    itself. One refinement over the Map's 25 km cells: precipitation is the blended
    QPF at each waypoint, interpolated by elevation between them, so the high-res
    models' own orographic gradient carries through. Returns per-day highs, new snow
    and gusts per level, or None."""
    n = len(PROFILE_ELEVS_M)
    pk = waypoints[0]
    nws_pts = nws_pts or [None] * len(waypoints)
    try:
        # surface = the summit's NWS grid box (forecasters' temperature/humidity/wind), GFS aloft
        times, H, z_s, G = region.point_inputs(pk["lat"], pk["lon"], days, nws_point=nws_pts[0])
        wp_elev = requests.get("https://api.open-meteo.com/v1/elevation", params={
            "latitude": ",".join(str(w["lat"]) for w in waypoints),
            "longitude": ",".join(str(w["lon"]) for w in waypoints)}, timeout=30).json()["elevation"]
    except Exception as exc:
        print(f"  WARNING: elevation profile unavailable ({exc}) - no map layers")
        return None

    order = sorted(range(len(waypoints)), key=lambda i: wp_elev[i])

    def qpf_at(i, z):
        # precip at elevation z: linear between the waypoints above/below, clamped at the ends
        t = times[i]
        vals = [(wp_elev[k], qpf[k].get(t) or 0) for k in order]
        if z <= vals[0][0]:
            return vals[0][1]
        for (z0, p0), (z1, p1) in zip(vals, vals[1:]):
            if z <= z1:
                return p0 + (p1 - p0) * (z - z0) / (z1 - z0) if z1 > z0 else p1
        return vals[-1][1]

    def crossing_ft(vals, target):
        # elevation (ft) where vals first reach `target` going up the ladder, interpolated
        for i, v in enumerate(vals):
            if v is None:
                continue
            prev = vals[i - 1] if i else None
            hit = v <= target if target == 32 else v >= target
            if hit:
                if i == 0 or prev is None or prev == v:
                    return round(PROFILE_ELEVS_M[i] * 3.28084)
                f = (target - prev) / (v - prev)
                return round((PROFILE_ELEVS_M[i - 1] + f * (PROFILE_ELEVS_M[i] - PROFILE_ELEVS_M[i - 1])) * 3.28084)
        return None

    # probe each waypoint's real elevation, and each waypoint's NWS grid-box elevation
    box_elev = [p["elev_m"] if p else z for p, z in zip(nws_pts, wp_elev)]
    prof = region.vertical_profile(times, H, z_s, G, qpf_at, PROFILE_ELEVS_M, probes=list(wp_elev) + box_elev)
    nw = len(waypoints)
    # °F to add to a waypoint's NWS box temperature to get its real elevation, hour by hour
    tshift = [{t: prof["probe_temp"][k][t] - prof["probe_temp"][nw + k][t] for t in prof["probe_temp"][k]}
              for k in range(nw)]
    by_day = OrderedDict()
    for i, t in enumerate(times):
        by_day.setdefault(t[:10], []).append(i)
    out = {"elev_m": PROFILE_ELEVS_M, "summit_m": wp_elev[0], "days": [], "total_snow": [0.0] * n,
           # hourly wind at each waypoint's elevation, for the forecast tables (not sent to the page)
           "_wp_wind": prof["probe_wind"][:nw], "_wp_gust": prof["probe_gust"][:nw], "_wp_tshift": tshift,
           "_wp_temp": prof["probe_temp"][:nw]}
    for di, label in enumerate(prof["labels"]):
        day_p = sum(qpf_at(i, wp_elev[0]) for i in by_day[prof["dates"][di]])
        for li in range(n):
            out["total_snow"][li] += prof["snow"][di][li]
        out["days"].append({
            "label": label,
            "high": [round(v, 1) for v in prof["hi"][di]],
            "snow": [round(v, 2) for v in prof["snow"][di]],
            "gust": [round(v) for v in prof["gust"][di]],
            "precip_in": round(day_p, 2),
            "freeze_ft": crossing_ft(prof["hi"][di], 32),
            "snow_level_ft": crossing_ft(prof["sfrac"][di], 0.5) if day_p >= 0.02 else None,
        })
    out["total_snow"] = [round(v, 2) for v in out["total_snow"]]
    # 3-hourly snapshots at every level: temperature and gust at the hour, snow since midnight
    tz = ZoneInfo("America/Los_Angeles")
    out["frames"] = {
        "ts": [[int(datetime.strptime(f"{d} {hr:02d}", "%Y-%m-%d %H").replace(tzinfo=tz).timestamp() * 1000)
                for hr in region.FRAME_HOURS] for d in prof["dates"]],
        "temp": [[[round(v, 1) for v in fr] for fr in day] for day in prof["f_temp"]],
        "gust": [[[round(v) for v in fr] for fr in day] for day in prof["f_gust"]],
        "snow": [[[round(v, 2) for v in fr] for fr in day] for day in prof["f_snow"]],
        "day_snow": [[round(v, 2) for v in day] for day in prof["snow"]]}
    return out


# ---- Air quality / smoke: one regional grid, shown on the overview + 3D maps ----
AQ_BOUNDS = region.BOUNDS                  # all of Oregon + Washington (Map tab, trail maps)
AQ_STEP = 0.4                              # matches the CAMS global grid Open-Meteo serves here


def aqi_grid(days=4):
    """Daily-max US AQI on a regular lat/lon grid (rows north->south, like image rows),
    from Open-Meteo's air-quality API (CAMS, includes wildfire smoke). None on failure."""
    w, s, e, n = AQ_BOUNDS
    lons = [round(w + i * AQ_STEP, 2) for i in range(int(round((e - w) / AQ_STEP)) + 1)]
    lats = [round(n - j * AQ_STEP, 2) for j in range(int(round((n - s) / AQ_STEP)) + 1)]
    pts = [(la, lo) for la in lats for lo in lons]
    try:
        d = requests.get("https://air-quality-api.open-meteo.com/v1/air-quality", params={
            "latitude": ",".join(str(p[0]) for p in pts), "longitude": ",".join(str(p[1]) for p in pts),
            "hourly": "us_aqi", "timezone": "America/Los_Angeles", "forecast_days": days}, timeout=90).json()
        locs = d if isinstance(d, list) else [d]
        times = locs[0]["hourly"]["time"]
    except Exception as exc:
        print(f"  WARNING: air-quality grid unavailable ({exc}) - no AQI layer")
        return None
    by_day = OrderedDict()
    for i, t in enumerate(times):
        by_day.setdefault(t[:10], []).append(i)
    out = []
    for di, (date, ix) in enumerate(by_day.items()):
        vals, frames = [], [[] for _ in region.FRAME_HOURS]
        for l in locs:
            a = l["hourly"]["us_aqi"]
            v = [a[i] for i in ix if a[i] is not None]
            vals.append(round(max(v)) if v else None)
            at_hour = {int(times[i][11:13]): a[i] for i in ix}
            for f, hr in enumerate(region.FRAME_HOURS):   # 3-hourly snapshots for the Map tab
                frames[f].append(None if at_hour.get(hr) is None else round(at_hour[hr]))
        dt = datetime.strptime(date, "%Y-%m-%d")
        out.append({"label": "Today" if di == 0 else f"{dt:%a} {dt.day}", "aqi": vals, "frames": frames})
    h = AQ_STEP / 2
    return {"nx": len(lons), "ny": len(lats), "lat0": lats[0], "lon0": lons[0], "step": AQ_STEP,
            "coords": [[w - h, n + h], [e + h, n + h], [e + h, s - h], [w - h, s - h]], "days": out}


# Shared by every map: paints the grid into a tiny canvas that Mapbox stretches with
# linear resampling, so 45 km cells read as a smooth haze field rather than blocks.
AQ_JS = r"""
window.AQ=__AQ__;
window.AQL=(function(){
  if(!window.AQ)return null;
  var AQ=window.AQ, cache={};
  // EPA AQI colours; clean air stays nearly transparent so smoke is what stands out
  var S=[[0,[0,200,0,0.06]],[50,[0,200,0,0.16]],[75,[245,225,0,0.30]],[125,[255,126,0,0.42]],[175,[235,20,20,0.50]],[250,[143,63,151,0.55]],[350,[126,0,35,0.60]]];
  function c(v){if(v<=S[0][0])return S[0][1];for(var i=1;i<S.length;i++){if(v<=S[i][0]){var f=(v-S[i-1][0])/(S[i][0]-S[i-1][0]),p=S[i-1][1],q=S[i][1];return [0,1,2,3].map(function(k){return p[k]+(q[k]-p[k])*f;});}}return S[S.length-1][1];}
  // h: index into the day's 3-hourly frames, or undefined/null for the day's worst hour
  function vals(d,h){return (h==null||!AQ.days[d].frames)?AQ.days[d].aqi:AQ.days[d].frames[h];}
  function image(d,h){
    var key=d+'-'+(h==null?'day':h);if(cache[key])return cache[key];
    var cv=document.createElement('canvas');cv.width=AQ.nx;cv.height=AQ.ny;
    var x=cv.getContext('2d'),im=x.createImageData(AQ.nx,AQ.ny),v=vals(d,h);
    for(var i=0;i<v.length;i++){var k=c(v[i]==null?0:v[i]);im.data[i*4]=k[0];im.data[i*4+1]=k[1];im.data[i*4+2]=k[2];im.data[i*4+3]=Math.round(k[3]*255);}
    x.putImageData(im,0,0);return cache[key]=cv.toDataURL();
  }
  function at(lat,lon,d,h){var j=Math.min(AQ.ny-1,Math.max(0,Math.round((AQ.lat0-lat)/AQ.step))),i=Math.min(AQ.nx-1,Math.max(0,Math.round((lon-AQ.lon0)/AQ.step)));return vals(d,h)[j*AQ.nx+i];}
  function cat(v){return v<=50?'Good':v<=100?'Moderate':v<=150?'Unhealthy for sensitive groups':v<=200?'Unhealthy':v<=300?'Very unhealthy':'Hazardous';}
  function legend(d,lat,lon,place,h,hlabel){
    var v=at(lat,lon,d,h);
    return '<b>Air quality</b> · '+AQ.days[d].label+(h==null?' (worst hour)':' · '+hlabel)
      +'<div class="lyr-bar" style="background:linear-gradient(90deg,#00e400 0%,#00e400 12%,#ffff00 25%,#ff7e00 42%,#ff0000 58%,#8f3f97 80%,#7e0023 100%)"></div>'
      +'<div class="lyr-ticks"><span>0</span><span>100</span><span>200</span><span>300+ AQI</span></div>'
      +'<div class="lyr-note">'+(v==null?'No data here':place+': AQI '+v+' · '+cat(v))+'</div>';
  }
  function attach(map,before){
    map.addSource('aq',{type:'image',url:image(0),coordinates:AQ.coords});
    map.addLayer({id:'aq',type:'raster',source:'aq',layout:{visibility:'none'},paint:{'raster-resampling':'linear','raster-fade-duration':0}},before);
  }
  function show(map,d,on,h){
    if(!map.getLayer('aq'))return;
    map.setLayoutProperty('aq','visibility',on?'visible':'none');
    if(on)map.getSource('aq').updateImage({url:image(d,h),coordinates:AQ.coords});
  }
  function worst(d,h){   // the region's worst cell for a day / frame, so the legend can point at the smoke
    var v=vals(d,h),bi=-1;for(var i=0;i<v.length;i++)if(v[i]!=null&&(bi<0||v[i]>v[bi]))bi=i;
    return bi<0?null:{v:v[bi],lat:AQ.lat0-Math.floor(bi/AQ.nx)*AQ.step,lon:AQ.lon0+(bi%AQ.nx)*AQ.step};
  }
  return {legend:legend,attach:attach,show:show,at:at,cat:cat,worst:worst,days:AQ.days.length};
})();
"""


# Map tab: full-region explorer. Each pixel is coloured from the 4 surrounding forecast
# cells, evaluated at that pixel's own elevation (read from Mapbox terrain-RGB tiles), so
# freezing lines, snow lines and wind exposure follow the real terrain.
REGION_JS = r"""
(function(){
var R=__DATA__, TOKEN='__TOKEN__', FIRES=__FIRES__, SMOKE=__SMOKE__;
if(!R){window.initRegionMap=function(){document.getElementById('regionmap').innerHTML='<p class="acc-empty" style="padding:20px">Region data was unavailable for this build.</p>';};return;}
function dec(s,T){var b=atob(s),u=new Uint8Array(b.length);for(var i=0;i<b.length;i++)u[i]=b.charCodeAt(i);return new T(u.buffer);}
var IDX=dec(R.index,Int16Array),TMP=dec(R.temp,Int8Array),SNW=dec(R.snow,Uint16Array),GST=dec(R.gust,Uint8Array),
    CLD=R.cloud?dec(R.cloud,Uint8Array):null;
var L=R.levels.length,D=R.days.length,LS=R.levels[1]-R.levels[0];
var RAMP={
  temp:{a:0.62,lo:-20,hi:100,step:0.5,S:[[-10,[75,44,127]],[5,[90,79,176]],[20,[79,127,201]],[29,[140,195,234]],[32,[250,252,255]],[35,[189,227,214]],[45,[124,196,122]],[60,[233,201,90]],[75,[242,154,59]],[90,[224,64,47]]],
        title:'Daily high',ticks:['-10°','32°','60°','90°F'],blo:-10,bhi:90},
  snow:{a:1,lo:0,hi:40,step:0.1,S:[[0,[255,255,255,0]],[0.1,[190,220,250,0.35]],[1,[150,195,245,0.6]],[3,[95,150,230,0.72]],[6,[60,100,210,0.8]],[12,[90,60,190,0.84]],[24,[150,50,170,0.88]]],
        title:'New snow',ticks:['0″','6″','12″','24″+'],blo:0,bhi:24},
  gust:{a:1,lo:0,hi:120,step:0.5,S:[[0,[255,255,255,0]],[12,[255,255,255,0]],[20,[250,232,150,0.42]],[30,[250,190,80,0.58]],[40,[240,130,50,0.68]],[55,[215,55,45,0.76]],[70,[150,40,120,0.8]],[90,[75,20,85,0.85]]],
        title:'Wind gusts on exposed terrain',ticks:['0','30','60','90 mph'],blo:0,bhi:90}
};
function mix(S,v,a){if(v<=S[0][0])return pick(S[0][1],a);for(var i=1;i<S.length;i++){if(v<=S[i][0]){var f=(v-S[i-1][0])/(S[i][0]-S[i-1][0]),p=pick(S[i-1][1],a),q=pick(S[i][1],a);return [0,1,2,3].map(function(k){return p[k]+(q[k]-p[k])*f;});}}return pick(S[S.length-1][1],a);}
function pick(c,a){return [c[0],c[1],c[2],c.length>3?c[3]:a];}
var LUT={};
function lut(l){if(LUT[l])return LUT[l];var r=RAMP[l],n=Math.round((r.hi-r.lo)/r.step)+1,t=new Uint8ClampedArray(n*4);
  for(var i=0;i<n;i++){var c=mix(r.S,r.lo+i*r.step,r.a);t[i*4]=c[0];t[i*4+1]=c[1];t[i*4+2]=c[2];t[i*4+3]=Math.round(c[3]*255);}return LUT[l]=t;}
// RegionLayers(opt): a map with the Map tab's layers and controls. opt = {wrap (holds the
// .lyr-ctl / .lyr-leg / .rmap-tip / .rmap-busy controls), container, bounds, fit, maxBounds,
// onLoad(map)}. Used by the Map tab and the Trail Forecast tab; the decoded data above is shared.
window.RegionLayers=function(opt){
var Q=function(sel){return opt.wrap.querySelector(sel);},map=null,fireOn=false;
// st.d: day index (or 'total' for the 7-day snow total); st.h: null = whole day (high /
// total / peak), 0..7 = one of the 3-hourly frames. Both are driven by the time slider.
var st={l:'temp',d:0,h:null},NF=R.frame_hours.length,FR={},FRD={},cur=null,smode='cum';
function frames(d){   // one day's 3-hourly fields, fetched the first time they're viewed
  if(!FR[d])FR[d]=fetch(R.frame_files[d]).then(function(r){if(!r.ok)throw new Error(r.status);return r.json();})
    .then(function(j){return FRD[d]={d:d,t:dec(j.t,Int8Array),g:dec(j.g,Uint8Array),s:dec(j.s,Uint16Array),
                              c:j.c?dec(j.c,Uint8Array):null,p:j.p?dec(j.p,Uint16Array):null};})
    .catch(function(){delete FR[d];return null;});
  return FR[d];}

// ---- snow windows (Hourly mode): cumulative from now, or the 24 h ending at the slider time ----
// S(d,f) = snow from the start of day 0 through frame f of day d: whole days from the
// daily totals, plus that day's since-midnight amount at the frame.
function daySnow(c,d,i0,i1,t){var b=(c*(D+1)+d)*L;return (SNW[b+i0]+(SNW[b+i1]-SNW[b+i0])*t)/10;}
function S(c,d,f,i0,i1,t){var s=0;for(var k=0;k<d;k++)s+=daySnow(c,k,i0,i1,t);
  var o=FRD[d],b=(c*NF+f)*L;return o?s+(o.s[b+i0]+(o.s[b+i1]-o.s[b+i0])*t)/10:s;}
function snowWindow(c,fz){var i0=fz|0,i1=i0<L-1?i0+1:i0,t=fz-i0,end=S(c,st.d,st.h,i0,i1,t);
  if(smode==='24h')return st.d>0?end-S(c,st.d-1,st.h,i0,i1,t):end;
  var s0=TL[0]||{d:0,h:0};return Math.max(0,end-S(c,s0.d,s0.h,i0,i1,t));}
function snowDays(){   // frame files the current snow window needs
  if(st.l!=='snow'||st.h===null)return [];
  return [st.d,smode==='24h'?Math.max(0,st.d-1):(TL[0]?TL[0].d:0)];}

// ---- the time slider: one timeline per layer/mode, rebuilt when either changes ----
// hourly: every 3-hourly frame from now on; daily: one step per day; radar: HRRR's
// simulated radar hourly for its 18 h, then the model's own 3-hourly precipitation.
var TL=[],ti=0,mode='hourly',total=false,HR=null;
var WHEN=new Intl.DateTimeFormat('en-US',{timeZone:'America/Los_Angeles',weekday:'short',day:'numeric',hour:'numeric'});
function when(t,withHour){   // "Thu 24, 2 PM" (Pacific time)
  var p={};WHEN.formatToParts(new Date(t)).forEach(function(x){p[x.type]=x.value;});
  return p.weekday+' '+p.day+(withHour?', '+p.hour+' '+p.dayPeriod:'');}
function buildTL(){
  var now=Date.now(),out=[],maxD=(st.l==='aq'&&window.AQL)?Math.min(D,window.AQL.days):D;
  if(st.l==='smoke'){
    if(SMOKE)SMOKE.times.forEach(function(t,k){if(t>=now-90*60000)out.push({kind:'smoke',k:k,t:t});});
    return out;}
  if(st.l==='radar'){
    if(HR)for(var k=0;k<=18;k++){var t=HR.init+k*3600000;if(t>=now-45*60000)out.push({kind:'hrrr',min:k*60,t:t});}
    var last=out.length?out[out.length-1].t:now-2*3600000;
    for(var d=0;d<D;d++)for(var f=0;f<NF;f++)if(R.frame_ts[d][f]>last)out.push({kind:'model',d:d,h:f,t:R.frame_ts[d][f]});
  }else if(mode==='daily'){
    for(var d2=0;d2<maxD;d2++)out.push({d:d2,h:null,t:R.frame_ts[d2][0]});
  }else{
    for(var d3=0;d3<maxD;d3++)for(var f3=0;f3<NF;f3++)if(R.frame_ts[d3][f3]>=now-90*60000)out.push({d:d3,h:f3,t:R.frame_ts[d3][f3]});
  }
  return out;}
function retime(){   // rebuild the timeline, staying as close as possible to the current time
  var t=TL[ti]?TL[ti].t:Date.now();TL=buildTL();ti=0;
  var best=Infinity;TL.forEach(function(f,i){var dd=Math.abs(f.t-t);if(dd<best){best=dd;ti=i;}});
  var s=Q('.lyr-time input[type=range]');s.max=Math.max(0,TL.length-1);s.value=ti;}
function cellVal(c,fz){var i0=fz|0,i1=i0<L-1?i0+1:i0,t=fz-i0,b;
  if(st.l==='snow'&&st.h!==null)return snowWindow(c,fz);
  if(st.h!==null&&cur&&cur.d===st.d){b=(c*NF+st.h)*L;
    var A=st.l==='temp'?cur.t:cur.g;return A[b+i0]+(A[b+i1]-A[b+i0])*t;}
  if(st.l==='temp'){b=(c*D+st.d)*L;return TMP[b+i0]+(TMP[b+i1]-TMP[b+i0])*t;}
  if(st.l==='gust'){b=(c*D+st.d)*L;return GST[b+i0]+(GST[b+i1]-GST[b+i0])*t;}
  b=(c*(D+1)+(st.d==='total'?D:st.d))*L;return (SNW[b+i0]+(SNW[b+i1]-SNW[b+i0])*t)/10;}
// ---- cloud cover: a 2D field (no elevation), drawn as one small stretched image ----
function cloudVal(c){if(st.h!==null&&cur&&cur.d===st.d&&cur.c)return cur.c[c*NF+st.h];return CLD?CLD[c*D+st.d]:0;}
function cloudAt(lon,lat){
  var gy=(R.lat0-lat)/R.dlat,gx=(lon-R.lon0)/R.dlon,y0=Math.floor(gy),x0=Math.floor(gx),ty=gy-y0,tx=gx-x0,s=0,ws=0;
  for(var dy=0;dy<2;dy++){var y=y0+dy;if(y<0||y>=R.ny)continue;for(var dx=0;dx<2;dx++){var x=x0+dx;if(x<0||x>=R.nx)continue;
    var c=IDX[y*R.nx+x];if(c<0)continue;var w=(dy?ty:1-ty)*(dx?tx:1-tx);if(w<=0)continue;s+=w*cloudVal(c);ws+=w;}}
  return ws>0?s/ws:null;}
function cloudColor(v){var f=Math.max(0,Math.min(1,v/100));   // thin: pale haze -> overcast: grey-blue deck
  return [Math.round(236-86*f),Math.round(239-81*f),Math.round(244-72*f),Math.round(255*Math.pow(f,1.3)*0.78)];}
function cloudImage(){
  var cv2=document.createElement('canvas');cv2.width=R.nx;cv2.height=R.ny;
  var g=cv2.getContext('2d'),im=g.createImageData(R.nx,R.ny),px=im.data;
  for(var gi=0;gi<R.nx*R.ny;gi++){var c=IDX[gi];if(c<0)continue;var k=cloudColor(cloudVal(c));px[gi*4]=k[0];px[gi*4+1]=k[1];px[gi*4+2]=k[2];px[gi*4+3]=k[3];}
  g.putImageData(im,0,0);return cv2.toDataURL();}
var CLD_COORDS=[[R.lon0-R.dlon/2,R.lat0+R.dlat/2],[R.lon0+(R.nx-0.5)*R.dlon,R.lat0+R.dlat/2],
                [R.lon0+(R.nx-0.5)*R.dlon,R.lat0-(R.ny-0.5)*R.dlat],[R.lon0-R.dlon/2,R.lat0-(R.ny-0.5)*R.dlat]];
async function drawClouds(){
  if(!map||!map.getLayer('cld'))return;
  if(st.l!=='cloud'){map.setLayoutProperty('cld','visibility','none');return;}
  cur=st.h===null?null:await frames(st.d);
  map.getSource('cld').updateImage({url:cloudImage(),coordinates:CLD_COORDS});
  map.setLayoutProperty('cld','visibility','visible');}

// ---- smoke forecast (NOAA HRRR-Smoke guidance): hourly grayscale PNGs, one byte per pixel on a
// log scale, coloured here. Two fields: near-surface (ug/m3, what you breathe) and the whole
// column overhead (mg/m2: haze, dim sun, red sunsets) ----
var kmode='sfc';
var SMK={
  sfc:{S:[[0,[200,190,175,0]],[2,[200,190,175,0]],[5,[196,186,170,0.3]],[9,[205,180,130,0.45]],[20,[220,160,90,0.58]],[35.5,[215,115,60,0.68]],[55.5,[195,60,45,0.76]],[125.5,[140,30,70,0.82]],[225.5,[85,20,85,0.86]]],
       unit:'µg/m³',title:'Near-surface smoke',max:300,
       cat:function(v){return v<=9?'Good':v<=35.4?'Moderate':v<=55.4?'Unhealthy for sensitive groups':v<=125.4?'Unhealthy':v<=225.4?'Very unhealthy':'Hazardous';},
       note:'Smoke at breathing level · categories are the EPA’s PM2.5 levels'},
  vert:{S:[[0,[175,175,180,0]],[1,[175,175,180,0]],[3,[170,170,175,0.28]],[8,[165,160,150,0.42]],[20,[170,140,105,0.55]],[50,[160,105,70,0.66]],[120,[120,70,50,0.76]],[250,[75,45,40,0.84]]],
       unit:'mg/m²',title:'Smoke overhead (whole sky)',max:300,
       cat:function(v){return v<2?'Clear skies':v<8?'Light haze':v<25?'Hazy':v<60?'Smoky skies':v<150?'Thick smoke':'Very thick smoke';},
       note:'All the smoke in the sky: haze, dim sun, red sunsets · can be heavy aloft while the air below is clean'}};
var SLUT={},SIMG={};
function smokeVal(b){return Math.pow(2,b/SMOKE.q)-1;}
function slut(k){if(SLUT[k])return SLUT[k];var t=new Uint8ClampedArray(256*4);
  for(var b=0;b<256;b++){var c=mix(SMK[k].S,smokeVal(b),1);t[b*4]=c[0];t[b*4+1]=c[1];t[b*4+2]=c[2];t[b*4+3]=Math.round(c[3]*255);}return SLUT[k]=t;}
function smokeFrame(k,i){   // {g: grayscale bytes, url: coloured image, worst: {v,lon,lat}}, loaded once per file
  var key=k+i;if(SIMG[key])return SIMG[key];var f=SMOKE.files[k][i];if(!f)return Promise.resolve(null);
  return SIMG[key]=new Promise(function(res){var img=new Image();
    img.onload=function(){var w=SMOKE.w,h=SMOKE.h,c=document.createElement('canvas');c.width=w;c.height=h;var g=c.getContext('2d');g.drawImage(img,0,0);
      var im=g.getImageData(0,0,w,h),px=im.data,gray=new Uint8Array(w*h),t=slut(k),wi=0;
      for(var p=0;p<w*h;p++){var b=px[p*4];gray[p]=b;if(b>gray[wi])wi=p;px[p*4]=t[b*4];px[p*4+1]=t[b*4+1];px[p*4+2]=t[b*4+2];px[p*4+3]=t[b*4+3];}
      g.putImageData(im,0,0);
      var ll=smokeLL(wi%w,Math.floor(wi/w));res({g:gray,url:c.toDataURL(),worst:{v:smokeVal(gray[wi]),lon:ll[0],lat:ll[1]}});};
    img.onerror=function(){delete SIMG[key];res(null);};img.src=f;});}
function smokeLL(x,y){var B=SMOKE.bounds,m0=merc(B[0],B[3]),m1=merc(B[2],B[1]);return [B[0]+(x+0.5)/SMOKE.w*(B[2]-B[0]),latOf(m0[1]+(y+0.5)/SMOKE.h*(m1[1]-m0[1]))];}
function smokeAt(lon,lat,fr){var B=SMOKE.bounds,m0=merc(B[0],B[3]),m1=merc(B[2],B[1]),p=merc(lon,lat);
  var x=Math.floor((p[0]-m0[0])/(m1[0]-m0[0])*SMOKE.w),y=Math.floor((p[1]-m0[1])/(m1[1]-m0[1])*SMOKE.h);
  if(!fr||x<0||y<0||x>=SMOKE.w||y>=SMOKE.h)return null;return smokeVal(fr.g[y*SMOKE.w+x]);}
var smokeCur=null;
async function drawSmoke(){
  if(!map||!map.getLayer('smk'))return;
  var f=TL[ti];
  if(st.l!=='smoke'||!f||f.kind!=='smoke'){map.setLayoutProperty('smk','visibility','none');smokeCur=null;return;}
  var want=kmode+f.k,fr=await smokeFrame(kmode,f.k);
  if(!TL[ti]||kmode+TL[ti].k!==want)return;   // the slider moved on while this frame loaded
  smokeCur=fr;legend();
  if(!fr){map.setLayoutProperty('smk','visibility','none');return;}
  map.getSource('smk').updateImage({url:fr.url,coordinates:SMOKE.coords});
  map.setLayoutProperty('smk','visibility','visible');
  var nx=TL[ti+1];if(nx&&nx.kind==='smoke')smokeFrame(kmode,nx.k);}   // preload the next hour for smooth playback

// ---- forecast radar ----
// First 18 h: NOAA HRRR simulated radar with precipitation type (REFP: rain / mix / snow),
// live tiles from the Iowa Environmental Mesonet for the latest HRRR run.
var HRRR_META='https://mesonet.agron.iastate.edu/data/gis/images/4326/hrrr/refd_1080.json',sym=null;
function hrrrInit(){return fetch(HRRR_META).then(function(r){return r.json();}).then(function(j){
  var t=Date.parse(j.model_init_utc);if(isNaN(t))return;
  HR={init:t,stamp:j.model_init_utc.replace(/[-:TZ]/g,'').slice(0,12)};}).catch(function(){HR=null;});}
function hrrrEnsure(min){
  var id='hr'+min;if(!HR||map.getLayer(id))return;
  map.addSource(id,{type:'raster',tileSize:256,attribution:'Forecast radar: NOAA HRRR via Iowa Environmental Mesonet',
    tiles:['https://mesonet.agron.iastate.edu/cache/tile.py/1.0.0/hrrr::REFP-F'+String(min).padStart(4,'0')+'-'+HR.stamp+'/{z}/{x}/{y}.png']});
  map.addLayer({id:id,type:'raster',source:id,paint:{'raster-opacity':0,'raster-fade-duration':0}},sym);}
function hrrrShow(frame){   // frame: the timeline entry to show, or null to hide all
  if(!map)return;
  var want=frame&&frame.kind==='hrrr'?frame.min:-1;
  if(want>=0){hrrrEnsure(want);hrrrEnsure(want+60);hrrrEnsure(want-60);}   // neighbours preload for smooth playback
  map.getStyle().layers.forEach(function(l){if(l.id.slice(0,2)!=='hr')return;var m=+l.id.slice(2);
    map.setLayoutProperty(l.id,'visibility',want>=0&&Math.abs(m-want)<=60?'visible':'none');
    map.setPaintProperty(l.id,'raster-opacity',m===want?0.82:0);});}
// Beyond 18 h: the model's own 3-hourly precipitation drawn radar-style, in the same
// rain / mix / snow colours as HRRR's, decided by each pixel's elevation-adjusted temperature.
var RAIN=[[0.004,[214,239,208]],[0.02,[160,217,155]],[0.05,[115,196,118]],[0.1,[64,170,93]],[0.2,[34,138,68]],[0.35,[245,215,80]],[0.5,[240,130,50]],[1,[205,40,40]]],
    SNOWR=[[0.004,[205,232,244]],[0.02,[140,200,224]],[0.05,[78,166,196]],[0.1,[50,120,190]],[0.2,[60,72,172]],[0.4,[112,52,160]]],
    MIXR=[[0.004,[252,214,196]],[0.05,[252,194,170]],[0.2,[232,140,120]],[0.5,[200,90,90]]];
var RLUT={};
function rlut(k){if(RLUT[k])return RLUT[k];var S={rain:RAIN,snow:SNOWR,mix:MIXR}[k],t=new Uint8ClampedArray(501*4);
  for(var i=0;i<=500;i++){var c=mix(S,i*0.002,0.85);t[i*4]=c[0];t[i*4+1]=c[1];t[i*4+2]=c[2];t[i*4+3]=Math.round((i*0.002<0.02?0.6:0.85)*255);}
  return RLUT[k]=t;}
function ptype(T){return T<=31?'snow':T<34?'mix':'rain';}

function valueAt(lon,lat,z,get){   // get(cell, fractional level); defaults to the current layer
  get=get||cellVal;
  var gy=(R.lat0-lat)/R.dlat,gx=(lon-R.lon0)/R.dlon,y0=Math.floor(gy),x0=Math.floor(gx),ty=gy-y0,tx=gx-x0,
      fz=Math.max(0,Math.min(L-1,(z-R.levels[0])/LS)),s=0,ws=0;
  for(var dy=0;dy<2;dy++){var y=y0+dy;if(y<0||y>=R.ny)continue;var wy=dy?ty:1-ty;
    for(var dx=0;dx<2;dx++){var x=x0+dx;if(x<0||x>=R.nx)continue;var c=IDX[y*R.nx+x];if(c<0)continue;
      var w=wy*(dx?tx:1-tx);if(w<=0)continue;s+=w*get(c,fz);ws+=w;}}
  return ws>0?s/ws:null;}
// the model "radar" frames need the frame's precipitation (in / 3 h) and its temperature at elevation
function framePrecip(c){return cur&&cur.p?cur.p[c*NF+st.h]/100:0;}
function frameTemp(c,fz){var i0=fz|0,i1=i0<L-1?i0+1:i0,t=fz-i0,b=(c*NF+st.h)*L;return cur.t[b+i0]+(cur.t[b+i1]-cur.t[b+i0])*t;}

// ---- elevation tiles for colouring pixels: AWS Terrain Tiles (free, no token, so this
// doesn't count against Mapbox usage). 256 px "terrarium" PNGs, cached as Int16 metres.
var TS_PX=256,DEM={},DEMQ=[];
function dem(z,x,y){var k=z+'/'+x+'/'+y;if(DEM[k])return DEM[k];
  DEM[k]=new Promise(function(res){var img=new Image();img.crossOrigin='anonymous';
    img.onload=function(){var c=document.createElement('canvas');c.width=c.height=TS_PX;var g=c.getContext('2d');g.drawImage(img,0,0,TS_PX,TS_PX);
      var d=g.getImageData(0,0,TS_PX,TS_PX).data,e=new Int16Array(TS_PX*TS_PX);
      for(var i=0;i<e.length;i++)e[i]=Math.round(d[i*4]*256+d[i*4+1]+d[i*4+2]/256-32768);res(e);};
    img.onerror=function(){res(null);};
    img.src='https://s3.amazonaws.com/elevation-tiles-prod/terrarium/'+z+'/'+x+'/'+y+'.png';});
  DEMQ.push(k);if(DEMQ.length>120)delete DEM[DEMQ.shift()];
  return DEM[k];}
function merc(lon,lat){var r=lat*Math.PI/180;return [(lon+180)/360,(1-Math.log(Math.tan(r)+1/Math.cos(r))/Math.PI)/2];}
var lastDem=null;
function elevAt(ll){   // metres at a lng/lat: the last render's AWS tiles, else Mapbox's terrain
  var p=merc(ll.lng,ll.lat);
  if(lastDem){var fx=p[0]*lastDem.n,fy=p[1]*lastDem.n,tx=Math.floor(fx),ty=Math.floor(fy),t=lastDem.T[ty*65536+tx];
    if(t)return t[Math.min(TS_PX-1,Math.floor((fy-ty)*TS_PX))*TS_PX+Math.min(TS_PX-1,Math.floor((fx-tx)*TS_PX))];}
  var z=map.queryTerrainElevation(ll,{exaggerated:false});return z===undefined?null:z;}
function latOf(y){var n=Math.PI-2*Math.PI*y;return 180/Math.PI*Math.atan(0.5*(Math.exp(n)-Math.exp(-n)));}

var cv=document.createElement('canvas'),busy=false,again=false,busyEl,tip,ctl,leg;
async function render(){
  // nothing to draw into until the style (and our overlay source) has loaded - e.g. if
  // Mapbox refuses the token - or while the map is hidden and has no size
  if(!map||!map.getSource('wxr')||!map.getCanvas().clientWidth)return;
  var mradar=st.l==='radar'&&TL[ti]&&TL[ti].kind==='model';
  var wx_on=st.l==='temp'||st.l==='snow'||st.l==='gust'||mradar;
  if(!wx_on){map.setLayoutProperty('wxr','visibility','none');return;}
  if(busy){again=true;return;}busy=true;busyEl.hidden=false;
  try{
    cur=st.h===null?null:await frames(st.d);
    await Promise.all(snowDays().map(frames));   // snow windows can span several days' files
    if(st.h!==null&&!cur){busyEl.textContent='Hourly data unavailable';return;}
    busyEl.textContent='Updating…';
    var b=map.getBounds(),W0=Math.max(b.getWest(),R.bounds[0]),E0=Math.min(b.getEast(),R.bounds[2]),S0=Math.max(b.getSouth(),R.bounds[1]),N0=Math.min(b.getNorth(),R.bounds[3]);
    if(W0>=E0||S0>=N0){map.setLayoutProperty('wxr','visibility','none');return;}
    var p0=merc(W0,N0),p1=merc(E0,S0),wx=p1[0]-p0[0],wy=p1[1]-p0[1];
    var W=Math.min(960,Math.round(map.getCanvas().clientWidth*0.9)),H=Math.round(W*wy/wx);
    if(H>960){H=960;W=Math.max(8,Math.round(H*wx/wy));}H=Math.max(8,H);
    var z=Math.max(4,Math.min(13,Math.ceil(Math.log2(W/wx/TS_PX)))),n=Math.pow(2,z);
    var tx0=Math.floor(p0[0]*n),tx1=Math.floor(p1[0]*n),ty0=Math.floor(p0[1]*n),ty1=Math.floor(p1[1]*n),keys=[],jobs=[];
    for(var ty=ty0;ty<=ty1;ty++)for(var tx=tx0;tx<=tx1;tx++){keys.push(ty*65536+tx);jobs.push(dem(z,tx,ty));}
    var got=await Promise.all(jobs),T={};keys.forEach(function(k,i){T[k]=got[i];});
    lastDem={T:T,n:n};   // hover reads elevation from the same tiles the colours came from
    var r=mradar?null:RAMP[st.l],tab=mradar?null:lut(st.l),nlut=tab?tab.length/4:0;
    cv.width=W;cv.height=H;var g=cv.getContext('2d'),im=g.createImageData(W,H),px=im.data;
    for(var j=0;j<H;j++){var my=p0[1]+(j+0.5)/H*wy,lat=latOf(my),fy=my*n,tyy=Math.floor(fy),py=Math.min(TS_PX-1,Math.floor((fy-tyy)*TS_PX));
      for(var i=0;i<W;i++){var mx=p0[0]+(i+0.5)/W*wx,fx=mx*n,txx=Math.floor(fx),t=T[tyy*65536+txx];if(!t)continue;
        var e=t[py*TS_PX+Math.min(TS_PX-1,Math.floor((fx-txx)*TS_PX))];if(e<=1)continue;   // sea
        var o=(j*W+i)*4,lon=mx*360-180,li,rt;
        if(mradar){   // precip rate (in/h) from the cells around; rain/mix/snow from temperature at this elevation
          var rate=valueAt(lon,lat,0,framePrecip);if(rate===null||(rate/=3)<0.004)continue;
          rt=rlut(ptype(valueAt(lon,lat,e,frameTemp)));li=Math.min(500,Math.round(rate/0.002))*4;
          px[o]=rt[li];px[o+1]=rt[li+1];px[o+2]=rt[li+2];px[o+3]=rt[li+3];continue;}
        var v=valueAt(lon,lat,e);if(v===null)continue;
        li=Math.max(0,Math.min(nlut-1,Math.round((v-r.lo)/r.step)))*4;
        px[o]=tab[li];px[o+1]=tab[li+1];px[o+2]=tab[li+2];px[o+3]=tab[li+3];}}
    g.putImageData(im,0,0);
    map.getSource('wxr').updateImage({url:cv.toDataURL(),coordinates:[[W0,N0],[E0,N0],[E0,S0],[W0,S0]]});
    map.setLayoutProperty('wxr','visibility','visible');
  }finally{busy=false;busyEl.hidden=true;if(again){again=false;render();}}
}
function legend(){
  if(st.l==='none'){leg.hidden=true;return;}
  var f=TL[ti],day=st.d==='total'?'Next 7 days':(f?when(f.t,st.h!==null||st.l==='radar'):'');
  if(st.l==='radar'){
    var grad=function(S){return 'linear-gradient(90deg,'+S.map(function(s){return 'rgb('+s[1].join(',')+')';}).join(',')+')';};
    var hrrr=f&&f.kind==='hrrr';
    leg.innerHTML='<b>'+(hrrr?'Forecast radar':'Forecast precipitation')+'</b> · '+day
      +'<div class="lyr-pt"><span>Rain</span><div class="lyr-bar" style="background:'+grad(RAIN)+'"></div></div>'
      +'<div class="lyr-pt"><span>Mix</span><div class="lyr-bar" style="background:'+grad(MIXR)+'"></div></div>'
      +'<div class="lyr-pt"><span>Snow</span><div class="lyr-bar" style="background:'+grad(SNOWR)+'"></div></div>'
      +'<div class="lyr-ticks"><span></span><span>Light</span><span>Heavy</span></div>'
      +'<div class="lyr-note">'+(hrrr?'NOAA HRRR simulated radar, run of '+when(HR.init,true)
        :'Model precipitation blend, 3-hourly'+(HR?' (HRRR radar covers the first 18 h)':''))+'</div>';
    leg.hidden=false;return;}
  if(st.l==='smoke'){
    var K=SMK[kmode],L2=Math.log2(K.max+1),stops=[];for(var q=0;q<=20;q++){var v=Math.pow(2,q/20*L2)-1,c=mix(K.S,v,1);stops.push('rgba('+Math.round(c[0])+','+Math.round(c[1])+','+Math.round(c[2])+','+Math.max(0.12,c[3]).toFixed(2)+') '+(q*5)+'%');}
    var tk=[0,0.25,0.5,0.75,1].map(function(q){var v=Math.pow(2,q*L2)-1;return '<span>'+(v<10?v.toFixed(0):Math.round(v/5)*5)+(q===1?'+ '+K.unit:'')+'</span>';}).join('');
    var wv=smokeCur&&smokeCur.worst;
    leg.innerHTML='<b>'+K.title+'</b> · '+(f?when(f.t,true):'')+'<div class="lyr-bar" style="background:linear-gradient(90deg,'+stops.join(',')+')"></div><div class="lyr-ticks">'+tk+'</div>'
      +(wv&&wv.v>=(kmode==='sfc'?9:8)?'<div class="lyr-where">Worst: '+Math.round(wv.v)+' '+K.unit+' · '+K.cat(wv.v)+' <button class="lyr-go" data-go="'+wv.lon+','+wv.lat+'">Show</button></div>'
        :'<div class="lyr-note">'+(kmode==='sfc'?'Clean air across the region':'Clear skies across the region')+'</div>')
      +'<div class="lyr-src">'+K.note+' · NOAA smoke forecast</div>';
    leg.hidden=false;return;}
  if(st.l==='cloud'){
    var cb='linear-gradient(90deg,'+[0,25,50,75,100].map(function(v){var k=cloudColor(v);return 'rgba('+k[0]+','+k[1]+','+k[2]+','+Math.max(0.12,k[3]/255).toFixed(2)+') '+v+'%';}).join(',')+')';
    leg.innerHTML='<b>'+(st.h===null?'Cloud cover (day average)':'Cloud cover')+'</b> · '+day
      +'<div class="lyr-bar" style="background:'+cb+'"></div><div class="lyr-ticks"><span>Clear</span><span>50%</span><span>Overcast</span></div>'
      +'<div class="lyr-note">Hover the map for values at any spot</div>';
    leg.hidden=false;return;}
  if(st.l==='aq'){
    if(!window.AQL){leg.hidden=true;return;}
    // point at the worst air in the region - clean air is drawn nearly transparent, so
    // smoke at the edge of the view is otherwise easy to miss
    var w=window.AQL.worst(st.d,st.h),hl=st.h===null?'':when(f.t,true).split(', ').pop();
    leg.innerHTML=window.AQL.legend(st.d,w?w.lat:45.52,w?w.lon:-122.68,'Worst in region',st.h,hl)
      +(w&&w.v>50?'<div class="lyr-where">near '+w.lat.toFixed(1)+'°N '+Math.abs(w.lon).toFixed(1)+'°W <button class="lyr-go" data-go="'+w.lon+','+w.lat+'">Show</button></div>':'');
    leg.hidden=false;return;}
  var r=RAMP[st.l],stops=r.S.filter(function(s){return s[0]>=r.blo&&s[0]<=r.bhi;});
  var title=st.h===null?r.title:{temp:'Temperature',gust:'Wind gusts on exposed terrain',
    snow:smode==='24h'?'New snow, 24 h ending':'New snow since '+(TL[0]?when(TL[0].t,true):'now')+' →'}[st.l];
  var bar='linear-gradient(90deg,'+stops.map(function(s){var c=mix(r.S,s[0],Math.max(r.a,0.9));return 'rgba('+Math.round(c[0])+','+Math.round(c[1])+','+Math.round(c[2])+','+Math.max(0.25,c[3]).toFixed(2)+') '+((s[0]-r.blo)/(r.bhi-r.blo)*100).toFixed(1)+'%';}).join(',')+')';
  leg.innerHTML='<b>'+title+'</b> · '+day+'<div class="lyr-bar" style="background:'+bar+'"></div><div class="lyr-ticks">'+r.ticks.map(function(t){return '<span>'+t+'</span>';}).join('')+'</div><div class="lyr-note">Hover the map for values at any spot</div>';
  leg.hidden=false;
}
function apply(){
  var f=TL[ti],radar=st.l==='radar'||st.l==='smoke',totalOn=total&&st.l==='snow'&&mode==='daily';
  st.d=totalOn?'total':(f?f.d:0);st.h=f&&f.h!==undefined?f.h:null;
  ctl.querySelectorAll('[data-l]').forEach(function(b){b.classList.toggle('active',b.dataset.l===st.l);});
  ctl.querySelectorAll('[data-mode]').forEach(function(b){b.classList.toggle('active',b.dataset.mode===(radar?'hourly':mode));b.disabled=radar;});
  var tb=ctl.querySelector('[data-total]');tb.hidden=!(st.l==='snow'&&mode==='daily');tb.classList.toggle('active',totalOn);
  var sm=ctl.querySelector('.lyr-smode');sm.hidden=!(st.l==='snow'&&mode==='hourly');
  sm.querySelectorAll('[data-smode]').forEach(function(b){b.classList.toggle('active',b.dataset.smode===smode);});
  var km=ctl.querySelector('.lyr-kmode');if(km){km.hidden=st.l!=='smoke';
    km.querySelectorAll('[data-kmode]').forEach(function(b){b.classList.toggle('active',b.dataset.kmode===kmode);});}
  ctl.querySelector('.lyr-time').hidden=st.l==='none';
  var s=Q('.lyr-time input[type=range]');s.disabled=totalOn;s.value=ti;
  Q('.lyr-rtime').textContent=totalOn?'Next 7 days':f?when(f.t,mode==='hourly'||radar)+(f.kind==='hrrr'?' · HRRR':''):'';
  hrrrShow(st.l==='radar'?f:null);
  drawClouds();
  drawSmoke();
  tip.hidden=true;   // its text belongs to the previous layer/time until the mouse moves
  legend();
  if(window.AQL&&map)window.AQL.show(map,st.d==='total'?0:st.d,st.l==='aq',st.h);
  render();
}
// play: step along the timeline
var timer=null,playBtn;
function play(on){
  if(timer){clearInterval(timer);timer=null;}
  playBtn.textContent=on?'❚❚ Pause':'▶ Play';playBtn.classList.toggle('active',on);
  if(!on)return;
  if(total){total=false;}
  timer=setInterval(function(){ti=(ti+1)%Math.max(1,TL.length);apply();},1100);
}
function fireTip(e){
  if(!fireOn||!map.getLayer('fire-fill'))return false;
  var f=map.queryRenderedFeatures(e.point,{layers:['fire-fill','fire-dot']})[0];if(!f)return false;
  var p=f.properties,pct=(p.pct===null||p.pct===undefined||p.pct==='null')?'containment not reported':p.pct+'% contained';
  tip.innerHTML='<b>'+p.name+'</b> · '+Number(p.acres).toLocaleString('en-US')+' acres · '+pct+'<br><span style="opacity:.75">Perimeter mapped '+p.updated+'</span>';
  tip.style.left=e.point.x+'px';tip.style.top=e.point.y+'px';tip.hidden=false;return true;
}
function hover(e){
  if(fireTip(e))return;
  if(st.l==='none'||(st.l==='radar'&&TL[ti]&&TL[ti].kind==='hrrr')){tip.hidden=true;return;}
  if(st.l==='radar'){   // model precipitation frames: rate and type at this spot
    var ze=elevAt(e.lngLat);if(ze===null||!cur){tip.hidden=true;return;}
    var rr=valueAt(e.lngLat.lng,e.lngLat.lat,0,framePrecip);rr=rr===null?0:rr/3;
    var ty=ptype(valueAt(e.lngLat.lng,e.lngLat.lat,ze,frameTemp));
    tip.innerHTML='<b>'+Math.round(ze*3.28084).toLocaleString('en-US')+'′</b> · '+(rr<0.004?'Dry':({rain:'Rain',mix:'Mix',snow:'Snow'})[ty]+' '+rr.toFixed(2)+'″/h water');
    tip.style.left=e.point.x+'px';tip.style.top=e.point.y+'px';tip.hidden=false;return;}
  if(st.l==='smoke'){var sv=smokeAt(e.lngLat.lng,e.lngLat.lat,smokeCur);if(sv===null){tip.hidden=true;return;}
    var K2=SMK[kmode];tip.innerHTML=(kmode==='sfc'?'Surface smoke ':'Smoke overhead ')+'<b>'+(sv<10?sv.toFixed(1):Math.round(sv))+' '+K2.unit+'</b> · '+K2.cat(sv);
    tip.style.left=e.point.x+'px';tip.style.top=e.point.y+'px';tip.hidden=false;return;}
  if(st.l==='cloud'){var cc=cloudAt(e.lngLat.lng,e.lngLat.lat);if(cc===null){tip.hidden=true;return;}
    tip.innerHTML='Cloud cover <b>'+Math.round(cc)+'%</b>';tip.style.left=e.point.x+'px';tip.style.top=e.point.y+'px';tip.hidden=false;return;}
  var z=elevAt(e.lngLat);
  if(z===null||z===undefined||z<=1){tip.hidden=true;return;}
  var ft=Math.round(z*3.28084).toLocaleString('en-US')+'′',txt;
  if(st.l==='aq'){var a=window.AQL&&window.AQL.at(e.lngLat.lat,e.lngLat.lng,st.d,st.h);txt=a==null?'No data':'AQI '+a+' · '+window.AQL.cat(a);}
  else{var v=valueAt(e.lngLat.lng,e.lngLat.lat,z);if(v===null){tip.hidden=true;return;}
    var day=st.h===null;
    txt=st.l==='temp'?(day?'High ':'')+Math.round(v)+'°F'
       :st.l==='snow'?'New snow '+v.toFixed(v<1?1:0)+'″'+(day?'':smode==='24h'?' in 24 h':' from now')
       :(day?'Peak gusts ~':'Gusts ~')+Math.round(v)+' mph';}
  tip.innerHTML='<b>'+ft+'</b> · '+txt;tip.style.left=e.point.x+'px';tip.style.top=e.point.y+'px';tip.hidden=false;
}
  busyEl=Q('.rmap-busy');tip=Q('.rmap-tip');ctl=Q('.lyr-ctl');leg=Q('.lyr-leg');
  mapboxgl.accessToken=TOKEN;
  map=new mapboxgl.Map(Object.assign({container:opt.container,style:'mapbox://styles/mapbox/outdoors-v12',projection:'mercator',maxPitch:70,
    bounds:opt.bounds,fitBoundsOptions:opt.fit},opt.maxBounds?{maxBounds:opt.maxBounds}:{}));
  map.addControl(new mapboxgl.NavigationControl({visualizePitch:true}),'top-right');
  map.on('load',function(){
    map.addSource('mapbox-dem',{type:'raster-dem',url:'mapbox://mapbox.mapbox-terrain-dem-v1',tileSize:512});
    map.setTerrain({source:'mapbox-dem',exaggeration:1.4});
    map.getStyle().layers.some(function(l){if(l.type==='symbol'){sym=l.id;return true;}});
    var blank=document.createElement('canvas');blank.width=blank.height=1;
    map.addSource('wxr',{type:'image',url:blank.toDataURL(),coordinates:[[R.bounds[0],R.bounds[3]],[R.bounds[2],R.bounds[3]],[R.bounds[2],R.bounds[1]],[R.bounds[0],R.bounds[1]]]});
    map.addLayer({id:'wxr',type:'raster',source:'wxr',paint:{'raster-fade-duration':0,'raster-resampling':'linear'}},sym);
    map.addSource('cld',{type:'image',url:blank.toDataURL(),coordinates:CLD_COORDS});
    map.addLayer({id:'cld',type:'raster',source:'cld',layout:{visibility:'none'},paint:{'raster-fade-duration':0,'raster-resampling':'linear'}},sym);
    if(window.AQL)window.AQL.attach(map,sym);
    if(SMOKE){map.addSource('smk',{type:'image',url:blank.toDataURL(),coordinates:SMOKE.coords});
      map.addLayer({id:'smk',type:'raster',source:'smk',layout:{visibility:'none'},paint:{'raster-fade-duration':0,'raster-resampling':'linear'}},sym);}
    if(FIRES){   // active wildfire perimeters (NIFC), an overlay that sits on top of any layer
      var pts={type:'FeatureCollection',features:FIRES.features.map(function(f){return{type:'Feature',properties:f.properties,geometry:{type:'Point',coordinates:[f.properties.lon,f.properties.lat]}};})};
      map.addSource('fires',{type:'geojson',data:FIRES,attribution:'Fire perimeters: NIFC WFIGS'});
      map.addSource('fire-pts',{type:'geojson',data:pts});
      map.addLayer({id:'fire-fill',type:'fill',source:'fires',layout:{visibility:'none'},paint:{'fill-color':'#E4572E','fill-opacity':0.38}},sym);
      map.addLayer({id:'fire-line',type:'line',source:'fires',layout:{visibility:'none'},paint:{'line-color':'#A8261B','line-width':['interpolate',['linear'],['zoom'],6,0.8,11,2]}},sym);
      // zoomed out, perimeters are specks: a dot per fire until they're big enough to see
      map.addLayer({id:'fire-dot',type:'circle',source:'fire-pts',maxzoom:8.5,layout:{visibility:'none'},
        paint:{'circle-color':'#E4572E','circle-radius':['interpolate',['linear'],['get','acres'],10,3.5,100000,8],'circle-stroke-color':'#fff','circle-stroke-width':1.5}});
      map.addLayer({id:'fire-name',type:'symbol',source:'fire-pts',minzoom:6.5,layout:{visibility:'none','text-field':['get','name'],'text-size':11,
        'text-font':['DIN Pro Medium','Arial Unicode MS Regular'],'text-offset':[0,1.1],'text-anchor':'top','text-optional':true},
        paint:{'text-color':'#8A1F14','text-halo-color':'#fff','text-halo-width':1.4}});
    }
    retime();apply();
    hrrrInit().then(function(){if(st.l==='radar'){retime();apply();}});   // HRRR run time, for the radar timeline
    if(opt.onLoad)opt.onLoad(map);
  });
  map.on('moveend',render);
  map.on('mousemove',hover);
  map.getCanvas().addEventListener('mouseleave',function(){tip.hidden=true;});
  playBtn=ctl.querySelector('[data-tplay]');
  leg.addEventListener('click',function(ev){var b=ev.target.closest('[data-go]');if(!b)return;
    var p=b.dataset.go.split(',').map(Number);map.flyTo({center:p,zoom:Math.max(map.getZoom(),7.5),pitch:0,duration:1200});});
  Q('.lyr-time input[type=range]').addEventListener('input',function(){play(false);ti=+this.value;apply();});
  ctl.addEventListener('click',function(ev){var b=ev.target.closest('button');if(!b||b.disabled)return;
    if(b.dataset.tplay!==undefined){play(!timer);return;}
    if(b.dataset.fires!==undefined){fireOn=!fireOn;b.classList.toggle('active',fireOn);b.setAttribute('aria-pressed',fireOn);
      ['fire-fill','fire-line','fire-dot','fire-name'].forEach(function(id){if(map.getLayer(id))map.setLayoutProperty(id,'visibility',fireOn?'visible':'none');});return;}
    if(b.dataset.l){st.l=b.dataset.l;if(st.l==='none')play(false);retime();}
    if(b.dataset.mode){mode=b.dataset.mode;total=false;retime();}
    if(b.dataset.total!==undefined){total=!total;play(false);}
    if(b.dataset.smode){smode=b.dataset.smode;}
    if(b.dataset.kmode){kmode=b.dataset.kmode;}
    apply();});
  // remove(): drop the map and the controls' listeners (a fresh instance can reuse the controls)
  return{map:map,remove:function(){play(false);map.remove();[ctl,leg].forEach(function(n){n.replaceWith(n.cloneNode(true));});}};
};
window.REGION_BOUNDS=R.bounds;
window.initRegionMap=function(){
  window.regionMap=window.RegionLayers({wrap:document.getElementById('regionmap').parentNode,container:'regionmap',
    // top padding keeps northern Washington clear of the layer / day / time controls
    bounds:[[R.bounds[0],R.bounds[1]],[R.bounds[2],R.bounds[3]]],fit:{padding:{top:130,bottom:20,left:20,right:20}},
    maxBounds:[[R.bounds[0]-4,R.bounds[1]-3],[R.bounds[2]+4,R.bounds[3]+3]]}).map;   // handy for debugging from the console
};
})();
"""


def pred_badge(pct):
    if pct is None:
        return ""
    tier, col = ("High", "#6BBF68") if pct >= 70 else ("Medium", "#FAA21B") if pct >= 40 else ("Low", "#ED1E29")
    return (f'<div class="pb" title="Predictability {pct}% ({tier}): how closely Google WeatherNext 2\'s '
            f'64 ensemble runs agree on this day\'s high and on rain"><i style="background:{col}"></i>{pct}%</div>')


def day_label(dt):
    # strftime("%a %-d") — "%-d" isn't supported on Windows
    return f"{dt:%a} {dt.day}".upper()


# ---------------------------------------------------------------------------
# Shared forecast helpers
# ---------------------------------------------------------------------------
def cloud_base_display(cb_m, c_low, c_mid, c_high, cloud_pct, temp_f, dew_f, vis_m, elev_ft):
    c_low=c_low or 0; c_mid=c_mid or 0; c_high=c_high or 0
    if cloud_pct<10: return "Clear"
    if cloud_pct<20 and c_low<10 and c_mid<15 and c_high<20: return "Few"
    alt_ft=None; spread=max(0,temp_f-dew_f)
    if vis_m is not None and vis_m<1000 and c_low>10:
        alt_ft=max(0,(spread/4.4)*1000)+elev_ft
        if alt_ft<elev_ft+50: return "Fog"
    elif cb_m is not None and c_low>15: alt_ft=cb_m*3.28084+elev_ft
    elif c_low>15: alt_ft=(spread/4.4)*1000+elev_ft
    elif c_mid>15 and c_mid>=c_high: alt_ft=15000
    elif c_high>15: alt_ft=28000
    elif cloud_pct>=20:
        if cb_m is not None and c_low>10: alt_ft=cb_m*3.28084+elev_ft
        elif c_mid>5: alt_ft=15000
        elif c_high>5: alt_ft=28000
        else: alt_ft=(spread/4.4)*1000+elev_ft
    else: return "--"
    if alt_ft is None: return "--"
    if alt_ft<1000: return f"{int(round(alt_ft,-1))}\u2032"
    if alt_ft<10000: return f"{int(round(alt_ft,-2)):,}\u2032"
    return f"{int(round(alt_ft/1000))}k\u2032"

# ---- Condition icons: one SVG sprite, referenced via <use> everywhere ----
_CLOUD = "M7 18h10a4 4 0 0 0 0-8 5.5 5.5 0 0 0-10.6 1.5A3.3 3.3 0 0 0 7 18z"

def _sun(cx, cy, r, r1, r2):
    rays = "".join(
        f'<line x1="{cx + r1*math.cos(a):.2f}" y1="{cy + r1*math.sin(a):.2f}" '
        f'x2="{cx + r2*math.cos(a):.2f}" y2="{cy + r2*math.sin(a):.2f}"/>'
        for a in (i * math.pi / 4 for i in range(8)))
    return (f'<g stroke="#F29A0E" stroke-width="1.6" stroke-linecap="round">{rays}</g>'
            f'<circle cx="{cx}" cy="{cy}" r="{r}" fill="#FAA21B"/>')

def _cloud(tf="", dark=False):
    fill, stroke = ("#D5D9E0", "#7D8392") if dark else ("#EEF0F4", "#9196A5")
    return (f'<path d="{_CLOUD}" transform="{tf}" fill="{fill}" stroke="{stroke}" '
            f'stroke-width="1.5" stroke-linejoin="round"/>')

def _drops(xs, y=18):
    return "".join(f'<line x1="{x}" y1="{y}" x2="{x-1}" y2="{y+3.5}" stroke="#4FB1BE" '
                   f'stroke-width="1.8" stroke-linecap="round"/>' for x in xs)

def _flakes(pts):
    out = ""
    for x, y in pts:
        out += (f'<g stroke="#6BA3D6" stroke-width="1.3" stroke-linecap="round">'
                f'<line x1="{x-1.5}" y1="{y}" x2="{x+1.5}" y2="{y}"/>'
                f'<line x1="{x-.8}" y1="{y-1.3}" x2="{x+.8}" y2="{y+1.3}"/>'
                f'<line x1="{x-.8}" y1="{y+1.3}" x2="{x+.8}" y2="{y-1.3}"/></g>')
    return out

WX_SYMBOLS = {
    "clear":   ("Clear",         _sun(12, 12, 4.2, 6.6, 9)),
    "mostly":  ("Mostly sunny",  _sun(10, 10, 3.6, 5.8, 7.8) + _cloud("translate(9 9) scale(.6)")),
    "partly":  ("Partly cloudy", _sun(8, 7.5, 3, 4.8, 6.4) + _cloud("translate(1 3)")),
    "cloudy":  ("Cloudy",        _cloud("translate(0 1)")),
    "showers": ("Showers",       _sun(7.5, 6, 2.6, 4.2, 5.6) + _cloud("translate(1 -2)") + _drops([10, 15])),
    "rain":    ("Rain",          _cloud("translate(0 -2)", dark=True) + _drops([8, 12, 16])),
    "snow":    ("Snow",          _cloud("translate(0 -2)") + _flakes([(8, 19.5), (12, 21.5), (16, 19.5)])),
    "pdrop":   ("Chance of precipitation", '<path d="M12 3.5c3 4 5.5 7 5.5 10a5.5 5.5 0 0 1-11 0c0-3 2.5-6 5.5-10z" '
                                 'fill="currentColor" stroke="currentColor" stroke-width="1.4" stroke-linejoin="round"/>'),
    "wind":    ("Windy",         '<g fill="none" stroke="#368994" stroke-width="1.8" stroke-linecap="round">'
                                 '<path d="M3 9h11a2.5 2.5 0 1 0-2.5-2.5"/><path d="M3 13h15a2.8 2.8 0 1 1-2.8 2.8"/>'
                                 '<path d="M3 17h6"/></g>'),
}
# Row-label icons: single-color line drawings (stroke follows CSS `color`)
ROW_ICONS = {
    "temp":  '<path d="M10 4.5a2 2 0 0 1 4 0v9a4 4 0 1 1-4 0z"/><path d="M12 9v7.5"/>',
    "cloud": f'<path d="{_CLOUD}" transform="translate(0 1)"/>',
    "base":  '<path d="M3 20h18"/><path d="M12 19V8.5M8.5 12 12 8.5l3.5 3.5"/><path d="M4 5h3M10.5 5h3M17 5h3"/>',
    "wind":  '<path d="M3 9h11a2.5 2.5 0 1 0-2.5-2.5"/><path d="M3 13h15a2.8 2.8 0 1 1-2.8 2.8"/><path d="M3 17h6"/>',
    "chance": '<path d="M3 12a9 9 0 0 1 18 0z"/><path d="M12 12v6.5a2 2 0 0 1-4 0"/><path d="M12 3v-.5"/>',
    "drop":  '<path d="M12 3.5c3 4 5.5 7 5.5 10a5.5 5.5 0 0 1-11 0c0-3 2.5-6 5.5-10z"/>',
    "aqi":   '<path d="M3 8c2-1.5 4-1.5 6 0s4 1.5 6 0 4-1.5 6 0"/><path d="M3 12.5c2-1.5 4-1.5 6 0s4 1.5 6 0 4-1.5 6 0"/><path d="M3 17c2-1.5 4-1.5 6 0s4 1.5 6 0 4-1.5 6 0"/>',
    "eye":   '<path d="M2.5 12S6 6 12 6s9.5 6 9.5 6-3.5 6-9.5 6-9.5-6-9.5-6z"/><circle cx="12" cy="12" r="2.6"/>',
    "depth": '<path d="M9 3v15.5M9 7h3M9 10.5h3M9 14h3"/><path d="M3 21c3-3.5 15-3.5 18 0"/>',
    "flake": '<path d="M12 3v18M4.2 7.5l15.6 9M4.2 16.5l15.6-9"/><path d="M10 4.5 12 6l2-1.5M10 19.5 12 18l2 1.5"/>',
    # navigation + header chrome
    "city":  '<path d="M3 21h18"/><path d="M5 21V10l5-3v14"/><path d="M10 21V4h9v17"/><path d="M13.5 8h2M13.5 11.5h2M13.5 15h2"/>',
    "peak":  '<path d="M2.5 20 9 8.5l3.5 6 2.5-4L21.5 20z"/><path d="M7.2 11.7 9 10.5l1.6 1.4"/>',
    "lift":  '<path d="M2.5 4.5l19 5"/><path d="M12 7v5.5"/><path d="M7.5 12.5h9"/><path d="M8.5 12.5v4.5h7v-4.5"/>',
    "chev":  '<path d="M6 9l6 6 6-6"/>',
    "target": '<circle cx="12" cy="12" r="8.5"/><circle cx="12" cy="12" r="4.5"/><circle cx="12" cy="12" r="1" fill="currentColor"/>',
    "map":   '<path d="M3 6.5 9 4l6 2.5 6-2.5v13.5L15 20l-6-2.5L3 20z"/><path d="M9 4v13.5M15 6.5V20"/>',
    "route": '<circle cx="6" cy="18.5" r="2"/><circle cx="18" cy="5.5" r="2"/><path d="M8 18.5h6.5a3 3 0 0 0 0-6h-5a3 3 0 0 1 0-6H16"/>',
}
WX_DEFS = ('<svg width="0" height="0" style="position:absolute" aria-hidden="true"><defs>'
           + "".join(f'<symbol id="wx-{k}" viewBox="0 0 24 24">{body}</symbol>' for k, (_, body) in WX_SYMBOLS.items())
           + "".join(f'<symbol id="ri-{k}" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" '
                     f'stroke-linecap="round" stroke-linejoin="round">{body}</symbol>' for k, body in ROW_ICONS.items())
           + '</defs></svg>')

def ri(name):
    return f'<svg class="rl" aria-hidden="true"><use href="#ri-{name}"/></svg>'

def condition_icon(ents):
    """Icon for a span of forecast entries (a day, or today so far).
    Precip beats sky; a wind mark is added when it's windy."""
    clouds = sum(e["clouds"] for e in ents) / len(ents)
    chance = max(e["precip"] for e in ents)
    rain = sum(e["precip_in"] for e in ents)
    snow = sum(e["snow_in"] for e in ents)
    if snow >= 0.2: kind = "snow"
    elif rain >= 0.1 and chance >= 50: kind = "rain"
    elif rain >= 0.02 and chance >= 30: kind = "showers"
    elif clouds < 10: kind = "clear"
    elif clouds < 40: kind = "mostly"
    elif clouds < 75: kind = "partly"
    else: kind = "cloudy"
    windy = max(e["wind"] for e in ents) >= 20 or max(e["gust"] for e in ents) >= 35
    label = WX_SYMBOLS[kind][0] + (" \u00B7 Windy" if windy else "")
    out = f'<svg class="wx"><use href="#wx-{kind}"/></svg>'
    if windy:
        out += '<svg class="wx wx-wind"><use href="#wx-wind"/></svg>'
    return f'<span class="wxi" role="img" aria-label="{label}" title="{label}">{out}</span>'

def precip_icon(p):
    # Filled drop that deepens with probability; 0% is a faint outline so columns stay aligned
    lvl = 0 if p == 0 else 1 if p < 30 else 2 if p < 60 else 3
    return f'<svg class="pd pd{lvl}"><use href="#wx-pdrop"/></svg>'

def cloud_puff(pct):
    # Soft blurred blob; neighbours bleed together into a continuous cloud field (Windy-style)
    if not pct or pct <= 5: return ""
    f = min(pct, 100) / 100
    r, g, b = (round(200 - 90*f), round(204 - 88*f), round(212 - 82*f))
    return f'<i class="cf" style="background:rgba({r},{g},{b},{0.25 + 0.65*f:.2f})"></i>'
def temp_bg(t):
    if t>=80: return "#ED1E29"
    if t>=70: return "#FAA21B"
    if t>=50: return "#6BBF68"
    if t>=35: return "#4FB1BE"
    return "#368994"
def wind_color(w):
    if w>=30: return "#ED1E29"
    if w>=20: return "#FAA21B"
    return "#111"
# US EPA AQI categories and their official colors (as used by AirNow / NWS)
AQI_CATS = [(50, "Good", "#00E400"), (100, "Moderate", "#FFFF00"),
            (150, "Unhealthy for sensitive groups", "#FF7E00"), (200, "Unhealthy", "#FF0000"),
            (300, "Very unhealthy", "#8F3F97"), (float("inf"), "Hazardous", "#7E0023")]
def aqi_html(val):
    # Category dot + plain number; the number only goes bold once air is actually unhealthy
    if val is None: return '<span style="color:#DDD;">·</span>'
    name, col = next((n, c) for top, n, c in AQI_CATS if val <= top)
    num = f"<b>{val:.0f}</b>" if val > 100 else f"{val:.0f}"
    return f'<span title="AQI {val:.0f} · {name}"><span class="aqd" style="background:{col}"></span>{num}</span>'

def render_hike_forecast(waypoints, hike_name, uid, profile=None, qpf=None, nws_pts=None):
    """Returns (full_html, summary): the 6-day tables, and the first waypoint's today plus
    every waypoint's next 24 hours (for the chart panels). `profile` (from elevation_profile)
    moves temperature and wind to each waypoint's elevation; `qpf` (one series per
    waypoint) replaces ECMWF/HRRR precipitation."""
    c_lat=sum(w["lat"] for w in waypoints)/len(waypoints)
    c_lon=sum(w["lon"] for w in waypoints)/len(waypoints)
    pred=ensemble_predictability(c_lat,c_lon,6)
    url="https://api.open-meteo.com/v1/forecast"
    all_fc=[]
    for wi,wp in enumerate(waypoints):
        lat,lon=wp["lat"],wp["lon"]
        pe=requests.get("https://api.open-meteo.com/v1/elevation",params={"latitude":lat,"longitude":lon}).json().get("elevation",[None])[0]
        ev={"elevation":pe} if pe is not None else {}
        d=requests.get(url,params={"latitude":lat,"longitude":lon,"hourly":["temperature_2m","dew_point_2m","cloud_cover","cloud_cover_low","cloud_cover_mid","cloud_cover_high","cloud_base","visibility","wind_speed_10m","wind_gusts_10m","precipitation","snowfall"],"temperature_unit":"fahrenheit","precipitation_unit":"inch","wind_speed_unit":"mph","timezone":"auto","forecast_days":6,"models":"ecmwf_ifs",**ev}).json()
        h=d["hourly"];tz=d.get("timezone","UTC");now=datetime.now(ZoneInfo(tz)).replace(tzinfo=None);elev_ft=d.get("elevation",0)*3.28084
        h["precipitation_probability"]=requests.get(url,params={"latitude":lat,"longitude":lon,"hourly":["precipitation_probability"],"timezone":"auto","forecast_days":6,**ev}).json().get("hourly",{}).get("precipitation_probability",[0]*len(h["time"]))
        tr=requests.get(url,params={"latitude":lat,"longitude":lon,"hourly":["temperature_2m","dew_point_2m"],"temperature_unit":"fahrenheit","timezone":"auto","forecast_days":6,**ev}).json().get("hourly",{})
        if "temperature_2m" in tr and len(tr["temperature_2m"])==len(h["time"]):h["temperature_2m"]=tr["temperature_2m"];h["dew_point_2m"]=tr["dew_point_2m"]
        hr=requests.get(url,params={"latitude":lat,"longitude":lon,"hourly":["temperature_2m","dew_point_2m","cloud_cover","cloud_cover_low","cloud_cover_mid","cloud_cover_high","cloud_base","visibility","wind_speed_10m","wind_gusts_10m","precipitation","snowfall"],"temperature_unit":"fahrenheit","precipitation_unit":"inch","wind_speed_unit":"mph","timezone":"auto","forecast_days":2,"models":"gfs_hrrr",**ev}).json()
        hh=hr.get("hourly",{});hi={t:i for i,t in enumerate(hh.get("time",[]))};ts=now.strftime("%Y-%m-%d")
        for i,t in enumerate(h["time"]):
            if t.startswith(ts) and t in hi:
                j=hi[t]
                for k in ["temperature_2m","dew_point_2m","cloud_cover","cloud_cover_low","cloud_cover_mid","cloud_cover_high","cloud_base","visibility","wind_speed_10m","wind_gusts_10m","precipitation","snowfall"]:
                    if k in hh and j<len(hh[k]) and hh[k][j] is not None:h[k][i]=hh[k][j]
        nbm_overlay(h,lat,lon,ev,6)
        if qpf:  # precipitation for this waypoint: the NWS's, our blend where they have none
            for i,t in enumerate(h["time"]):
                if qpf[wi].get(t) is not None:h["precipitation"][i]=qpf[wi][t]
        # the NWS forecast is the base: temperature/dew point moved from its grid box to this
        # spot's real elevation (by the upper-air profile if we have one, else a standard lapse)
        n=(nws_pts or [None]*len(waypoints))[wi]
        if n:
            tsh=(profile or {}).get("_wp_tshift",[None]*len(waypoints))[wi] or {}
            std=region.STD_LAPSE_F_PER_M*((n["elev_m"] or 0)-(pe or 0))
            nws_overlay(h,n,lambda t,tsh=tsh,std=std:tsh.get(t,std))
        if profile and profile.get("_wp_gust"):
            # wind at this waypoint's own elevation, as on the maps: surface model near the
            # ground, free-air wind aloft - whichever is stronger
            pw,pg=profile["_wp_wind"][wi],profile["_wp_gust"][wi]
            for i,t in enumerate(h["time"]):
                if pg.get(t) is not None:h["wind_gusts_10m"][i]=max(h["wind_gusts_10m"][i] or 0,pg[t])
                if pw.get(t) is not None:h["wind_speed_10m"][i]=max(h["wind_speed_10m"][i] or 0,pw[t])
        vis=h.get("visibility",[None]*len(h["time"]))
        aq=requests.get("https://air-quality-api.open-meteo.com/v1/air-quality",params={"latitude":lat,"longitude":lon,"hourly":["us_aqi","pm2_5"],"timezone":"auto","forecast_days":6}).json().get("hourly",{})
        aqi_idx={t:i for i,t in enumerate(aq.get("time",[]))};aqi_l=aq.get("us_aqi",[])
        entries=[];h24=[];prev_date=None;prev_inc=None;day_num=0;acc_p=acc_s=0
        for i,t in enumerate(h["time"]):
            dt=datetime.strptime(t,"%Y-%m-%dT%H:%M");dk=dt.strftime("%Y-%m-%d")
            if dk!=prev_date:day_num+=1;prev_date=dk
            # rain/snow accumulate over every hour a column covers (3 h after today), not just the displayed hour
            p_h=(h.get("precipitation") or [0]*len(h["time"]))[i] or 0
            t_h,d_h=h["temperature_2m"][i],h["dew_point_2m"][i]
            s_h,f_h=new_snow_in(p_h,t_h,rh_from_dew(t_h,d_h) if t_h is not None and d_h is not None else None)
            if day_num==1:
                if dt<now.replace(minute=0,second=0,microsecond=0):acc_p=acc_s=0;continue
            acc_p+=p_h;acc_s+=s_h
            if len(h24)<24:   # the next 24 hours, hour by hour (the Cities charts)
                ty="" if p_h<0.005 else ("snow" if f_h>=0.8 else "mix" if f_h>0.2 else "rain")
                h24.append({"t":dt.strftime("%a %I%p").replace(" 0"," "),"temp":round(t_h or 0),
                            "wind":round(h["wind_speed_10m"][i] or 0),"gust":round(h["wind_gusts_10m"][i] or 0),
                            "sky":round(h["cloud_cover"][i] or 0),"p":round(p_h,3),"s":round(s_h,2),"ty":ty})
            if day_num!=1 and (dt.hour-2)%3!=0:continue
            is_new=dk!=prev_inc;prev_inc=dk
            tmp=h["temperature_2m"][i];dew=h["dew_point_2m"][i];cld=h["cloud_cover"][i]
            cl=h["cloud_cover_low"][i];cm=h["cloud_cover_mid"][i];ch=h["cloud_cover_high"][i]
            cb=h["cloud_base"][i];vm=vis[i]
            wnd=h["wind_speed_10m"][i];gst=h["wind_gusts_10m"][i]
            prc=h["precipitation_probability"][i]
            pi2,sn=acc_p,acc_s;acc_p=acc_s=0
            ai=aqi_idx.get(t);av=aqi_l[ai] if ai is not None and ai<len(aqi_l) else None
            entries.append({"time":dt.strftime("%I%p").lstrip("0").lower(),"date_lbl":day_label(dt),"date_key":dk,"temp":tmp,"clouds":cld,"cb":cloud_base_display(cb,cl,cm,ch,cld,tmp,dew,vm,elev_ft),"c_low":cl or 0,"c_mid":cm or 0,"c_high":ch or 0,"wind":wnd,"gust":gst,"precip":prc,"precip_in":pi2,"snow_in":sn,"aqi":av,"new_day":is_new,"day_num":day_num})
        days=OrderedDict()
        for e in entries:
            dk=e["date_lbl"]
            if dk not in days:days[dk]={"entries":[],"day_num":e["day_num"]}
            days[dk]["entries"].append(e)
        R={"time":"","temp":"","ch":"","cm":"","cl":"","cb":"","wind":"","precip":"","pa":"","aqi":""}
        for di,(dk,dinfo) in enumerate(days.items()):
            ents=dinfo["entries"];nc=len(ents)
            hi2,lo2=max(e["temp"] for e in ents),min(e["temp"] for e in ents)
            mw,mg=max(e["wind"] for e in ents),max(e["gust"] for e in ents)
            mp=max(e["precip"] for e in ents);rt=sum(e["precip_in"] for e in ents);st=sum(e["snow_in"] for e in ents)
            aqis=[e["aqi"] for e in ents if e["aqi"] is not None];maq=max(aqis) if aqis else None
            ci=condition_icon(ents)
            tt=' <span class="tt">TODAY</span>' if di==0 else ""
            S,D=f'd{di} dsum',f'd{di} ddet';oc=f'onclick="toggleDay_{uid}({di})"'
            R["time"]+=f'<td class="{S}" colspan="{nc}" {oc}><div class="dl">{dk}{tt}</div><div class="ds-ci">{ci}</div>{pred_badge(pred.get(ents[0]["date_key"]))}</td>'
            for ei,e in enumerate(ents):
                dl=f'<div class="dl">{dk}{tt}</div>' if ei==0 else '';bdr='border-left:1px solid #DDE0E6;' if ei==0 else ''
                R["time"]+=f'<td class="{D}" style="display:none;{bdr}">{dl}<div class="tl">{e["time"]}</div></td>'
            R["temp"]+=f'<td class="{S}" colspan="{nc}" {oc}><span class="tp" style="background:{temp_bg(hi2)}">{hi2:.0f}\u00B0</span> <span class="tp" style="background:{temp_bg(lo2)};opacity:0.7">{lo2:.0f}\u00B0</span></td>'
            for ei,e in enumerate(ents):
                bdr='border-left:1px solid #DDE0E6;' if ei==0 else ''
                R["temp"]+=f'<td class="{D}" style="display:none;{bdr}"><div class="tp" style="background:{temp_bg(e["temp"])}">{e["temp"]:.0f}\u00B0</div></td>'
            for band,key,avg,nm in [("ch","c_high",sum(e["c_high"] for e in ents)/nc,"High"),("cm","c_mid",sum(e["c_mid"] for e in ents)/nc,"Mid"),("cl","c_low",sum(e["c_low"] for e in ents)/nc,"Low")]:
                R[band]+=f'<td class="{S} cc" colspan="{nc}" {oc} title="{nm} cloud {avg:.0f}%">{cloud_puff(avg)}</td>'
                for ix,e in enumerate(ents):
                    bdr='border-left:1px solid #DDE0E6;' if ix==0 else ''
                    R[band]+=f'<td class="{D} cc" style="display:none;{bdr}" title="{nm} cloud {e[key]:.0f}%">{cloud_puff(e[key])}</td>'
            bases=[e["cb"] for e in ents if e["cb"] not in ("Clear","Few","--")]
            cbs=bases[0] if len(set(bases))<=1 and bases else ("Clear" if not bases else f"{bases[0]}\u2013{bases[-1]}")
            R["cb"]+=f'<td class="{S} cb" colspan="{nc}" {oc}>{html.escape(cbs)}</td>'
            for ei,e in enumerate(ents):
                bdr='border-left:1px solid #DDE0E6;' if ei==0 else ''
                R["cb"]+=f'<td class="{D} cb" style="display:none;{bdr}">{html.escape(e["cb"])}</td>'
            R["wind"]+=f'<td class="{S}" colspan="{nc}" {oc}><span style="color:{wind_color(mw)}">{mw:.0f}</span><span class="gs">({mg:.0f})</span></td>'
            for ei,e in enumerate(ents):
                bdr='border-left:1px solid #DDE0E6;' if ei==0 else ''
                R["wind"]+=f'<td class="{D}" style="display:none;{bdr}"><span style="color:{wind_color(e["wind"])}">{e["wind"]:.0f}</span><span class="gs">({e["gust"]:.0f})</span></td>'
            R["precip"]+=f'<td class="{S}" colspan="{nc}" {oc}>{precip_icon(mp)} {mp}%</td>'
            for ei,e in enumerate(ents):
                bdr='border-left:1px solid #DDE0E6;' if ei==0 else ''
                R["precip"]+=f'<td class="{D}" style="display:none;{bdr}">{precip_icon(e["precip"])} {e["precip"]}%</td>'
            if st>0.01:ps=f'<span style="color:#6BA3D6;">\u2744 {st:.1f}"</span>'
            elif rt>0.005:ps=f'<span style="color:#4FB1BE;">{rt:.2f}"</span>'
            else:ps='<span style="color:#DDD;">\u00b7</span>'
            R["pa"]+=f'<td class="{S} pa" colspan="{nc}" {oc}>{ps}</td>'
            for ei,e in enumerate(ents):
                bdr='border-left:1px solid #DDE0E6;' if ei==0 else ''
                if e["snow_in"]>0.01:a=f"{e['snow_in']:.1f}" if e["snow_in"]>=0.1 else f"{e['snow_in']:.2f}";R["pa"]+=f'<td class="{D} pa" style="display:none;{bdr}color:#6BA3D6;">\u2744{a}"</td>'
                elif e["precip_in"]>0.005:a=f"{e['precip_in']:.1f}" if e["precip_in"]>=0.1 else f"{e['precip_in']:.2f}";R["pa"]+=f'<td class="{D} pa" style="display:none;{bdr}color:#4FB1BE;">{a}"</td>'
                else:R["pa"]+=f'<td class="{D} pa" style="display:none;{bdr}color:#DDD;">\u00b7</td>'
            R["aqi"]+=f'<td class="{S} aqi" colspan="{nc}" {oc}>{aqi_html(maq)}</td>'
            for ei,e in enumerate(ents):
                bdr='border-left:1px solid #DDE0E6;' if ei==0 else ''
                R["aqi"]+=f'<td class="{D} aqi" style="display:none;{bdr}">{aqi_html(e["aqi"])}</td>'
        all_fc.append({"name":wp["name"],"lat":lat,"lon":lon,"elev_ft":elev_ft,"R":dict(R),"entries":entries,"h24":h24})
    tbl_html=""
    if len(all_fc)>1:
        # waypoint switcher; mirrors the 3D map markers (and works when the map can't load)
        tbl_html+='<div class="wp-chips">'+''.join(
            f'<button class="wp-chip{" active" if fi==0 else ""}" data-wp="{uid}" onclick="showWp_{uid}({fi})">'
            f'<span class="wp-dot" style="background:{["#FE5000","#FAA21B","#6BBF68"][fi%3]}"></span>{html.escape(fc["name"])}'
            f'<span class="wp-chip-el">{fc["elev_ft"]:,.0f}′</span></button>' for fi,fc in enumerate(all_fc))+'</div>'
    for fi,fc in enumerate(all_fc):
        col=['#FE5000','#FAA21B','#6BBF68'][fi%3]
        fc_display='' if fi==0 else 'display:none;'
        actual_rows=""
        for key,label in [("time",""),("temp",ri("temp")+'Temp'),("ch",ri("cloud")+'High <span class="alt">20-40k</span>'),("cm",ri("cloud")+'Mid <span class="alt">6-20k</span>'),("cl",ri("cloud")+'Low <span class="alt">&lt;6k</span>'),("cb",ri("base")+'Base'),("wind",ri("wind")+'Wind (Gust)'),("precip",ri("chance")+'Chance'),("pa",ri("drop")+'Rain/Snow'),("aqi",ri("aqi")+'AQI')]:
            cls='class="cch"' if key in ('ch','cm','cl') else ''
            actual_rows+=f'<tr><th {cls}>{label}</th>{fc["R"][key]}</tr>\n'
        tbl_html+=f'''
    <div id="fc_{uid}_{fi}" style="{fc_display}">
    <div class="wp-header"><span class="wp-dot" style="background:{col}"></span><span class="wp-name">{html.escape(fc["name"])}</span><span class="wp-meta">{fc["elev_ft"]:,.0f}\u2032 MSL \u00b7 {fc["lat"]:.4f}, {fc["lon"]:.4f}</span></div>
    <div class="scroll-wrap"><table id="tbl_{uid}_{fi}">
      {actual_rows}
    </table></div>
    </div>'''
    full_html=f"""
<html><head><style>
* {{ box-sizing:border-box; }}
body {{ font-family:'Helvetica Neue',Arial,sans-serif; margin:0; padding:14px; background:#FAFAFA; }}
.hike-layout {{ display:flex; gap:16px; align-items:flex-start; }}
.hike-fc-panel {{ flex:1; min-width:0; }}
.scroll-wrap {{ overflow-x:auto; background:#fff; border-radius:10px; box-shadow:0 1px 4px rgba(0,0,0,0.08); padding:8px; margin-bottom:4px; }}
table {{ border-collapse:separate; border-spacing:4px 0; white-space:nowrap; font-size:13px; }}
th {{ position:sticky; left:0; z-index:2; background:#fff; text-align:left; padding:3px 10px 3px 0; font-size:12px; color:#555; font-weight:600; white-space:nowrap; min-width:85px; }}
th .ri {{ font-size:14px; margin-right:3px; }}
td {{ text-align:center; padding:3px 6px; min-width:50px; }}
.dsum {{ cursor:pointer; min-width:80px; border-left:1px solid #DDE0E6; }}
.dsum:hover {{ background:rgba(254,80,0,0.04); }}
.ddet {{ cursor:pointer; }}
.dl {{ font-size:11px; font-weight:700; color:#111; letter-spacing:.5px; text-transform:uppercase; }}
.ds-ci {{ font-size:18px; margin-top:2px; }}
.tt {{ background:#FE5000; color:#fff; font-size:9px; padding:1px 5px; border-radius:3px; margin-left:3px; vertical-align:middle; }}
.tl {{ font-size:11px; color:#999; font-weight:600; }}
.tp {{ border-radius:5px; color:#fff; font-size:13px; font-weight:700; padding:2px 5px; display:inline-block; }}
.cb {{ font-size:11px; color:#777; }}
.gs {{ font-size:10px; color:#368994; margin-left:2px; }}
.cc {{ height:20px; font-size:10px; padding:0 3px; text-align:center; vertical-align:middle; line-height:20px; }}
th.cch {{ font-size:9px; min-width:85px; padding:1px 8px 1px 0; }}
.alt {{ font-size:8px; color:#AAA; font-weight:400; }}
.pa {{ font-size:10px; }}
.aqi {{ font-size:11px; color:#666; }}
.aqi b {{ color:#111; font-weight:700; }}
.aqd {{ display:inline-block; width:7px; height:7px; border-radius:50%; margin-right:4px; vertical-align:1px; box-shadow:inset 0 0 0 1px rgba(0,0,0,0.15); }}
.leg {{ margin-top:8px; font-size:11px; color:#AAA; }}
</style></head><body>
<div class="hike-layout">
<div class="hike-fc-panel">
  {tbl_html}
  <div class="leg">NWS forecast (temp, wind, precip, sky) adjusted to each spot's elevation \u00b7 ECMWF cloud layers \u00b7 Open-Meteo AQI \u00b7 % = WeatherNext 2 predictability \u00b7 Click a day to expand</div>
</div>
</div>
<script>
function toggleDay_{uid}(idx) {{
    var s=document.querySelectorAll('[id^="tbl_{uid}_"] .d'+idx+'.dsum');
    var d=document.querySelectorAll('[id^="tbl_{uid}_"] .d'+idx+'.ddet');
    var open=d[0]&&d[0].style.display!=='none';
    s.forEach(function(el){{el.style.display=open?'':'none'}});
    d.forEach(function(el){{el.style.display=open?'none':''}});
}}
function showWp_{uid}(i) {{
    for(var j=0;j<{len(all_fc)};j++){{var sec=document.getElementById('fc_{uid}_'+j);if(sec)sec.style.display=(j===i)?'block':'none';}}
    document.querySelectorAll('[data-wp="{uid}"]').forEach(function(b,j){{b.classList.toggle('active',j===i);}});
    if(window.onWp) window.onWp('{uid}',i);
}}
document.querySelectorAll('[id^="tbl_{uid}_"]').forEach(function(tbl){{
    tbl.addEventListener('click',function(ev){{
        var td=ev.target.closest('td');
        if(!td) return;
        var m=td.className.match(/d(\\d+)/);
        if(m && td.classList.contains('ddet')) toggleDay_{uid}(parseInt(m[1]));
    }});
}});
</script>
</body></html>
"""
    # summary = first waypoint's today (the city itself, or a mountain's summit)
    # "today" = the first day that still has hours ahead (a response fetched before midnight
    # starts on yesterday, whose hours are all past by now)
    first = all_fc[0]["entries"][0]["day_num"] if all_fc[0]["entries"] else 1
    today = [e for e in all_fc[0]["entries"] if e["day_num"] == first]
    sm = {}
    if today:
        ac = sum(e["clouds"] for e in today) / len(today)
        sm = {"hi": max(e["temp"] for e in today), "lo": min(e["temp"] for e in today),
              "wind": max(e["wind"] for e in today), "gust": max(e["gust"] for e in today),
              "precip": max(e["precip"] for e in today), "rain": sum(e["precip_in"] for e in today),
              "clouds": ac, "icon": condition_icon(today), "elev_ft": all_fc[0]["elev_ft"]}
    sm["h24"] = all_fc[0]["h24"]
    sm["points"] = [{"name": fc["name"], "elev_ft": round(fc["elev_ft"]), "h24": fc["h24"]} for fc in all_fc]
    return full_html, sm


# ---------------------------------------------------------------------------
# Page 1: Oregon Cities
# ---------------------------------------------------------------------------
CITIES = [
    {"name":"Portland","lat":45.5250,"lon":-122.8110},
    {"name":"Hood River","lat":45.7096,"lon":-121.5123},
    {"name":"Sandy","lat":45.3968,"lon":-122.2671},
    {"name":"Bend","lat":44.0590,"lon":-121.3123},
    {"name":"Florence","lat":43.9799,"lon":-124.1012},
    {"name":"Cannon Beach","lat":45.8983,"lon":-123.9609},
    {"name":"Seattle","lat":47.6032,"lon":-122.3303},
    {"name":"Packwood","lat":46.6073,"lon":-121.6706},
    {"name":"Pacific City","lat":45.2019,"lon":-123.9617},
    {"name":"Forks","lat":47.9505,"lon":-124.3854},
]


# 24-hour chart panel shared by the Cities and Mountain Trails tabs: wind (gust whisker),
# temperature, cloud cover, precipitation by type with a running total. Plain SVG, redrawn
# on select and resize; one hover column and tooltip shared by all four. Rain/mix/snow
# colours were run through the dataviz palette validator (CVD-separable, >=3:1 on white).
# WxCharts(panel, {vis:true}) wires one .cc-panel and returns set(hours), for the page to call on
# select; with vis the third chart is visibility (the ski page) instead of cloud cover.
CHARTS_LIB_JS = r"""
(function(){
var PT={rain:'#0E9AAE',mix:'#DE6A52',snow:'#5A4FCF'}, PTN={rain:'Rain',mix:'Mix',snow:'Snow'}, TOT='#3D4450';
var L=34,PT_=13,B=17;
function tcol(t){if(t>=80)return'#ED1E29';if(t>=70)return'#FAA21B';if(t>=50)return'#6BBF68';if(t>=35)return'#4FB1BE';return'#368994';}
function nice(v){var s=[1,1.5,2,2.5,3,4,5,6,8,10],m=Math.pow(10,Math.floor(Math.log10(v||1)));for(var k=0;k<s.length;k++)if(s[k]*m>=v)return s[k]*m;return 10*m;}
function bar(x,y,w,h,r){if(h<=0)return'';r=Math.min(r,w/2,h);return'M'+x+','+(y+h)+'V'+(y+r)+'Q'+x+','+y+' '+(x+r)+','+y+'H'+(x+w-r)+'Q'+(x+w)+','+y+' '+(x+w)+','+(y+r)+'V'+(y+h)+'Z';}
function hr(t){var p=t.split(' '),h=p[1].toLowerCase().replace('m','');return h==='12a'?p[0]:h;}
function inch(v){return v>=0.1?v.toFixed(1):v.toFixed(2);}
function when(t){return t.replace(/(\d)([AP])M/,'$1 $2M');}
function chart(title,sum,keys,d,W,PH,o){
  var pw=W-L-4,bw=pw/d.length,w=Math.max(3,Math.min(11,bw*0.6)),y0=PT_+PH,lo=o.lo,hi=o.hi;
  var Y=function(v){return y0-(Math.max(lo,Math.min(hi,v))-lo)/(hi-lo)*PH;};
  var lines=o.lines||[],SH=Math.round(PH*(lines.length>1?0.4:0.5));
  var xl=(lines.length?y0+16+lines.length*SH+(lines.length-1)*12:y0)+13;   // hour labels sit under the running-total strips
  var s='<rect class="cc-band" x="0" y="'+(PT_-6)+'" width="'+bw+'" height="'+(PH+6)+'" rx="3" fill="#F1F2F5" visibility="hidden"/>';
  o.ticks.forEach(function(t){s+='<line x1="'+L+'" x2="'+(W-4)+'" y1="'+Y(t)+'" y2="'+Y(t)+'" stroke="'+(t===lo?'#DDE0E6':'#F0F1F4')+'" stroke-width="1"/>'
    +'<text x="'+(L-6)+'" y="'+(Y(t)+3.5)+'" text-anchor="end">'+o.fmt(t)+'</text>';});
  d.forEach(function(h,i){
    var cx=L+bw*i+bw/2,x=cx-w/2,v=o.val(h),top=Y(v);
    s+='<path d="'+bar(x,top,w,y0-top,4)+'" fill="'+o.col(h)+'"/>';
    if(o.whisker){var g=Y(o.whisker(h));if(g<top-1)s+='<line x1="'+cx+'" x2="'+cx+'" y1="'+top+'" y2="'+g+'" stroke="#5A5F6B" stroke-width="1.25"/><line x1="'+(cx-3)+'" x2="'+(cx+3)+'" y1="'+g+'" y2="'+g+'" stroke="#5A5F6B" stroke-width="1.25" stroke-linecap="round"/>';}
    if(o.label&&o.label(h,i))s+='<text x="'+cx+'" y="'+((o.whisker?Y(o.whisker(h)):top)-4)+'" text-anchor="middle" style="fill:#111;font-weight:700">'+o.label(h,i)+'</text>';
    if(i%3===0)s+='<text x="'+cx+'" y="'+xl+'" text-anchor="middle"'+(hr(h.t).length>3?' style="fill:#5A5F6B;font-weight:700"':'')+'>'+hr(h.t)+'</text>';
  });
  var H=y0+B;
  // running totals: each gets its own strip under the bars (same hours, its own inch scale) -
  // snow runs ~10x the water, so on one scale the water line would lie flat
  lines.forEach(function(ln,k){
    var s0=y0+16+k*(SH+12),s1=s0+SH,tot=ln.v[ln.v.length-1],sm=nice(tot);
    var SY=function(v){return s1-v/sm*SH;};
    s+='<line x1="'+L+'" x2="'+(W-4)+'" y1="'+s1+'" y2="'+s1+'" stroke="#DDE0E6"/><line x1="'+L+'" x2="'+(W-4)+'" y1="'+s0+'" y2="'+s0+'" stroke="#F0F1F4"/>'
      +'<text x="'+(L-6)+'" y="'+(s0+3.5)+'" text-anchor="end">'+(sm<0.1?sm.toFixed(2):sm<10?sm.toFixed(1):sm.toFixed(0))+'″</text><text x="'+(L-6)+'" y="'+(s1+3.5)+'" text-anchor="end">0</text>'
      +'<text x="'+(L+4)+'" y="'+(s0+10)+'" style="font-weight:600;fill:'+ln.c+'">'+ln.lab+'</text>'
      +'<rect class="cc-band" x="0" y="'+(s0-3)+'" width="'+bw+'" height="'+(SH+6)+'" rx="3" fill="#F1F2F5" visibility="hidden"/>';
    var pts=[[L,s1]].concat(ln.v.map(function(v,i){return[L+bw*(i+1),SY(v)];})),dp='M'+pts.map(function(p){return p[0].toFixed(1)+','+p[1].toFixed(1);}).join('L');
    var e=pts[pts.length-1];
    s+='<path d="'+dp+'L'+e[0]+','+s1+'Z" fill="'+ln.c+'" fill-opacity=".08"/><path d="'+dp+'" fill="none" stroke="'+ln.c+'" stroke-width="2" stroke-linejoin="round"/>'
      +'<circle cx="'+e[0]+'" cy="'+e[1]+'" r="3" fill="'+ln.c+'" stroke="#fff" stroke-width="1.5"/>'
      +'<text x="'+(e[0]-6)+'" y="'+(e[1]-5)+'" text-anchor="end" style="fill:#111;font-weight:700">'+(ln.snow?tot.toFixed(1):inch(tot))+'″</text>';
  });
  if(lines.length)H=xl+4;
  if(o.empty)s+='<text x="'+(L+pw/2)+'" y="'+(y0-PH/2+4)+'" text-anchor="middle" style="font-size:11px">'+o.empty+'</text>';
  var k=keys.map(function(c){return'<span class="cc-key"><i style="background:'+c[0]+(c[2]?';width:9px;height:2px;border-radius:1px':'')+'"></i>'+c[1]+'</span>';}).join('');
  return'<div class="cc-chart"><div class="cc-ct">'+title+k+'<span class="cc-sum">'+sum+'</span></div>'
    +'<svg viewBox="0 0 '+W+' '+H+'" height="'+H+'" role="img" aria-label="'+title+', next 24 hours">'+s+'</svg></div>';
}
window.WxCharts=function(panel,opt){
  opt=opt||{};
  var box=panel.querySelector('.cc-charts'),tip=panel.querySelector('.cc-tip'),now=panel.querySelector('.cc-now'),d=[],cum=[],cumS=[];
  function draw(){
    if(!box.clientWidth||!d.length)return;
    var W=box.clientWidth,out='';
    // side by side with the map, the plots grow to fill its height; stacked, they keep a fixed size
    var tot=cum[cum.length-1],stot=cumS[cumS.length-1],wet=tot>=0.005,snowy=stot>=0.1;
    var k=wet?(snowy?0.8:0.5):0,extra=wet?(snowy?28:16):0;   // the running-total strips' share of the height
    var PH=matchMedia('(max-width:1000px)').matches?64:Math.max(50,Math.min(110,Math.floor((box.clientHeight-4*(20+PT_+B)-30-extra)/(4+k))));
    var gmax=Math.max.apply(null,d.map(function(h){return h.gust;})),wmax=Math.max.apply(null,d.map(function(h){return h.wind;})),wt=nice(Math.max(10,gmax)/2);
    out+=chart('Wind','Max '+wmax+' mph \u00b7 gusts '+gmax,[['#368994','Wind'],['#5A5F6B','Gust',1]],d,W,PH,{lo:0,hi:wt*2,ticks:[0,wt,wt*2],fmt:function(v){return v;},
      val:function(h){return h.wind;},col:function(){return'#368994';},whisker:function(h){return h.gust;}});
    var tv=d.map(function(h){return h.temp;}),tx=Math.max.apply(null,tv),tn=Math.min.apply(null,tv);
    var flo=Math.floor((tn-6)/10)*10,fhi=Math.ceil((tx+3)/10)*10,ix=tv.indexOf(tx),in_=tv.indexOf(tn);
    var tt=[flo];for(var t=flo+10;t<fhi;t+=10)if((fhi-flo)<=40||(t-flo)%20===0)tt.push(t);tt.push(fhi);
    out+=chart('Temperature','High '+tx+'\u00b0 \u00b7 low '+tn+'\u00b0',[],d,W,PH,{lo:flo,hi:fhi,ticks:tt,fmt:function(v){return v+'\u00b0';},
      val:function(h){return h.temp;},col:function(h){return tcol(h.temp);},label:function(h,i){return(i===ix||i===in_)?h.temp+'\u00b0':'';}});
    if(opt.vis){   // visibility, 0-10+ mi: short bars are the trouble, so those carry the colour
      var vv=d.map(function(h){return h.vis==null?10:Math.min(10,h.vis);}),vmin=Math.min.apply(null,vv);
      out+=chart('Visibility',vmin>=10?'10+ mi all day':'Lowest '+(vmin<1?vmin.toFixed(1):Math.round(vmin))+' mi',[],d,W,PH,{lo:0,hi:10,ticks:[0,5,10],fmt:function(v){return v?v+(v===10?'+':''):'0';},
        val:function(h){return h.vis==null?10:Math.min(10,h.vis);},col:function(h){var v=h.vis==null?10:h.vis;return v<1?'#D11A24':v<3?'#C9760A':'#A3ABB8';}});
    }else{
      var avg=Math.round(d.reduce(function(a,h){return a+h.sky;},0)/d.length);
      out+=chart('Cloud cover','Avg '+avg+'%',[],d,W,PH,{lo:0,hi:100,ticks:[0,50,100],fmt:function(v){return v+'%';},
        val:function(h){return h.sky;},col:function(){return'#A3ABB8';}});
    }
    // precipitation per hour coloured by what falls, plus the running liquid total
    var pm=Math.max.apply(null,d.map(function(h){return h.p;})),ph=Math.max(0.04,nice(pm));
    var psum=!wet?'Dry':inch(tot)+'\u2033 water'+(stot>=0.05?' \u00b7 '+stot.toFixed(1)+'\u2033 snow':'');
    var lines=!wet?[]:[{v:cum,c:TOT,lab:'Water total'}].concat(snowy?[{v:cumS,c:PT.snow,lab:'Snow total',snow:1}]:[]);
    out+=chart('Precipitation',psum,[[PT.rain,'Rain'],[PT.mix,'Mix'],[PT.snow,'Snow']],d,W,PH,{lo:0,hi:ph,ticks:[0,ph/2,ph],fmt:function(v){return v?(ph<0.1?v.toFixed(2):v.toFixed(1))+'\u2033':'0';},
      val:function(h){return h.ty?h.p:0;},col:function(h){return PT[h.ty]||'#ccc';},lines:lines,empty:wet?'':'No precipitation expected'});
    box.innerHTML=out;
    if(now)now.textContent=when(d[0].t)+' \u2013 '+when(d[d.length-1].t);
  }
  function hide(){tip.hidden=true;box.querySelectorAll('.cc-band').forEach(function(b){b.setAttribute('visibility','hidden');});}
  function hover(ev){
    if(!d.length)return;
    var r=box.getBoundingClientRect(),bw=(box.clientWidth-L-4)/d.length,i=Math.floor((ev.clientX-r.left-L)/bw);
    if(i<0||i>=d.length){hide();return;}
    box.querySelectorAll('.cc-band').forEach(function(b){b.setAttribute('x',L+bw*i);b.setAttribute('visibility','visible');});
    var h=d[i],pl=h.ty?'<i style="background:'+PT[h.ty]+'"></i>'+PTN[h.ty]+' '+inch(h.p)+'\u2033'+(h.s>=0.05?' ('+h.s.toFixed(1)+'\u2033 snow)':''):'No precipitation';
    tip.innerHTML='<b>'+when(h.t)+'</b><br>'+h.temp+'\u00b0F \u00b7 '+(opt.vis&&h.vis!=null?'visibility '+(h.vis>=10?'10+':h.vis<1?h.vis.toFixed(1):Math.round(h.vis))+' mi':'cloud '+h.sky+'%')+'<br>Wind '+h.wind+' mph, gusts '+h.gust+'<br>'+pl
      +(cum[i]>=0.005?'<br><i style="background:'+TOT+';height:2px"></i>So far '+inch(cum[i])+'\u2033 water'+(cumS[i]>=0.05?' \u00b7 '+cumS[i].toFixed(1)+'\u2033 snow':''):'');
    tip.hidden=false;
    var pr=panel.getBoundingClientRect(),x=ev.clientX-pr.left,y=ev.clientY-pr.top,tw=tip.offsetWidth;
    tip.style.left=(x+14+tw>pr.width?x-14-tw:x+14)+'px';tip.style.top=Math.max(4,y-40)+'px';
  }
  box.addEventListener('pointermove',hover);box.addEventListener('pointerdown',hover);box.addEventListener('pointerleave',hide);
  if(window.ResizeObserver)new ResizeObserver(draw).observe(box);else window.addEventListener('resize',draw);
  return{set:function(hours){d=hours||[];var c=0,cs=0;cum=d.map(function(h){c+=h.ty?h.p:0;return c;});cumS=d.map(function(h){cs+=h.s;return cs;});hide();draw();}};
};
})();
"""

# Camera view shared by the Trails and Mt Hood tabs. WxCams(root) wires one .cc-cams block
# (.cam-img, .cam-msg, .cam-cap, .cam-thumbs inside it) and returns show(list, i, onPick).
# A camera is {code} (USGS AshCam: live photo time; at night its last daylight photo) or
# {url, note} (a plain image URL, refreshed every 5 minutes).
CAMS_LIB_JS = r"""
(function(){
var ASH='https://volcview.wr.usgs.gov/ashcam-api/',CAMQ={};
var CLOCK=new Intl.DateTimeFormat('en-US',{timeZone:'America/Los_Angeles',hour:'numeric',minute:'2-digit'});
var DAYCLK=new Intl.DateTimeFormat('en-US',{timeZone:'America/Los_Angeles',weekday:'short',hour:'numeric',minute:'2-digit'});
function ago(ts){var m=Math.round((Date.now()/1000-ts)/60);return m<2?'just now':m<60?m+' min ago':m<48*60?Math.round(m/60)+' h ago':Math.round(m/1440)+' days ago';}
function stamp(ts){var d=new Date(ts*1000),today=new Date().toDateString()===d.toDateString();return (today?CLOCK:DAYCLK).format(d);}
function resolveCam(c){   // -> {url, ts, night, liveUrl, liveTs}; cached 5 minutes per camera
  var k=c.code||c.url,hit=CAMQ[k];if(hit&&Date.now()-hit.at<300000)return hit.p;
  var bust=(c.url&&c.url.indexOf('?')>=0?'&':'?')+'t='+Math.floor(Date.now()/300000),p;
  if(c.url)p=Promise.resolve({url:c.url+bust});   // a plain image URL: no timestamp to read
  else p=fetch(ASH+'webcamApi/webcam/'+c.code).then(function(r){return r.json();}).then(function(j){
      var n=j.webcam&&j.webcam.newestImage;if(!n)throw 0;
      var live={url:n.imageUrl,ts:n.imageTimestamp,liveUrl:n.imageUrl,liveTs:n.imageTimestamp};
      // daylight from the camera's own sunrise / sunset (civil twilight), 20 min inside each end;
      // the catalog's night flag starts hours early
      var sun=j.webcam.suninfo||{},up=sun.civil_twilight_sunrise_unixtime,down=sun.civil_twilight_sunset_unixtime;
      var isDay=function(ts){if(!up||!down)return true;var len=((down-up)%86400+86400)%86400,o=(((ts-up)%86400)+86400)%86400;return o>1200&&o<len-1200;};
      if(isDay(n.imageTimestamp))return live;
      // night: the newest photo is black - find the last daylight one
      return fetch(ASH+'imageApi/webcam/'+c.code+'/2/newestFirst/200').then(function(r){return r.json();}).then(function(h){
        var d=(h.images||[]).filter(function(x){return isDay(x.imageTimestamp);})[0];
        return d?{url:d.imageUrl,ts:d.imageTimestamp,night:true,liveUrl:n.imageUrl,liveTs:n.imageTimestamp}:live;});
    }).catch(function(){return{url:ASH+'images/webcams/'+c.code+'/current-medium.jpeg'+bust};});
  CAMQ[k]={at:Date.now(),p:p};return p;}
window.WxCams=function(root){
  var img=root.querySelector('.cam-img'),msg=root.querySelector('.cam-msg'),capEl=root.querySelector('.cam-cap'),strip=root.querySelector('.cam-thumbs');
  var list=[],cur=0,tok=0,liveOverride=false,onPick=null;
  function show(l,i,cb){
    if(l){list=l;onPick=cb||onPick;}
    var c=list[i],t=++tok;if(!c)return;if(i!==cur)liveOverride=false;cur=i;if(onPick)onPick(i);
    var src=c.page?' · <a href="'+c.page+'" target="_blank" rel="noopener">source ↗</a>':'';
    var cap=function(when){capEl.innerHTML='<b>'+c.label+'</b> · '+c.desc+'<br>'+when+src;};
    var put=function(url){msg.hidden=true;img.hidden=false;img.src=url;};
    img.onerror=function(){if(t!==tok)return;img.hidden=true;msg.textContent='This camera isn’t responding right now.';msg.hidden=false;};
    img.alt=c.label+' camera';cap('<span class="cam-when">Loading…</span>');
    resolveCam(c).then(function(r){
      if(t!==tok)return;
      if(!r.ts){put(r.url);cap('<span class="cam-when">'+(c.note||'Live camera')+'</span>');return;}
      var live=liveOverride||!r.night,ts=live?r.liveTs:r.ts,stale=Date.now()/1000-r.liveTs>6*3600;
      put(live?r.liveUrl:r.url);
      cap((r.night&&!live?'<span class="cam-night">Night now</span> · <span class="cam-when">last daylight view, ':'<span class="cam-when">Photo ')
        +stamp(ts)+' · '+ago(ts)+'</span>'
        +(stale?' · <span class="cam-night">camera may be offline</span>':'')
        +(r.night?(live?' · <button data-daylight>last daylight view</button>':' · <button data-live>show live</button>'):''));});
    // the other cameras, as thumbnails to switch to
    strip.innerHTML=list.map(function(x,j){return j===i?'':'<button data-cam="'+j+'" title="'+x.desc+'"><img alt="" data-k="'+j+'">'+x.label+'</button>';}).join('');
    list.forEach(function(x,j){if(j===i)return;resolveCam(x).then(function(r){if(t!==tok)return;var im=strip.querySelector('[data-k="'+j+'"]');if(im)im.src=r.url;});});
  }
  strip.addEventListener('click',function(ev){var b=ev.target.closest('[data-cam]');if(b)show(null,+b.dataset.cam);});
  capEl.addEventListener('click',function(ev){if(ev.target.closest('[data-live]')){liveOverride=true;show(null,cur);}
    else if(ev.target.closest('[data-daylight]')){liveOverride=false;show(null,cur);}});
  return{show:show};
};
})();
"""


CITY_JS = r"""
var H24=__H24__, cityMarkerEls=window.cityMarkerEls=[], ccSel=0, charts=WxCharts(document.getElementById('city-cc'));
// the chosen city: its charts, its marker, and its 6-day forecast moved to the top of the list
window.selectCity=function(i,init){
  if(i===undefined)i=ccSel;ccSel=i;
  var s=document.getElementById('cc-city');if(s){s.value=i;document.getElementById('cc-name').textContent=s.options[i].text;}
  cityMarkerEls.forEach(function(el,j){el.classList.toggle('sel',j===i);});
  charts.set(H24[i]);
  var d=document.getElementById('city_detail_'+i);if(d&&!init)d.parentNode.insertBefore(d,d.parentNode.firstChild);
};
selectCity(0,true);
"""


def build_cities_page():
    # --- Single source of truth: fetch detail + extract summary in one pass ---
    summaries = []; detail_bodies = []; detail_css = ""
    print(f"Fetching {len(CITIES)} cities ({FETCH_WORKERS} at a time)...")
    with ThreadPoolExecutor(max_workers=FETCH_WORKERS) as ex:
        rendered = list(ex.map(lambda ic: render_hike_forecast(
            [{"name": ic[1]["name"], "lat": ic[1]["lat"], "lon": ic[1]["lon"]}],
            ic[1]["name"], "city" + str(ic[0]), nws_pts=[nws.point(ic[1]["lat"], ic[1]["lon"])]), enumerate(CITIES)))
    for ci, (city, (full_html, sm)) in enumerate(zip(CITIES, rendered)):
        print(f"  {city['name']}")
        sm["name"] = city["name"]; sm["lat"] = city["lat"]; sm["lon"] = city["lon"]
        summaries.append(sm)
        if ci == 0:
            css_m = re.search(r'<style>(.*?)</style>', full_html, re.DOTALL)
            detail_css = css_m.group(1) if css_m else ""
        body_m = re.search(r'<body>(.*?)</body>', full_html, re.DOTALL)
        body_content = body_m.group(1) if body_m else full_html
        body_content = re.sub(r'<!-- MAP -->.*?<!-- /MAP -->\s*', '', body_content, flags=re.DOTALL, count=1)
        detail_bodies.append(body_content)
        print(f"  {sm.get('hi',0):.0f}/{sm.get('lo',0):.0f}F wind {sm.get('wind',0):.0f}mph precip {sm.get('precip',0)}%")

    # --- Build dashboard HTML ---
    markers_json = json.dumps([{
        "name": s["name"], "lat": s["lat"], "lon": s["lon"],
        "hi": round(s["hi"]), "lo": round(s["lo"]), "wind": round(s["wind"]),
        "gust": round(s["gust"]), "precip": s["precip"], "clouds": round(s["clouds"]),
        "icon": s["icon"], "rain": round(s["rain"], 2)
    } for s in summaries])

    dashboard_css = (
        "* { box-sizing:border-box; }\n"
        "body { font-family:'Helvetica Neue',Arial,sans-serif; margin:0; padding:14px; background:#FAFAFA; }\n"
        ".map-wrap { background:#fff; border-radius:10px; box-shadow:0 1px 4px rgba(0,0,0,0.08); padding:8px; margin-bottom:12px; }\n"
        "#citymap { width:100%; height:600px; border-radius:8px; }\n"
        ".city-popup { background:#fff; border-radius:6px; padding:6px 10px; font-size:13px; box-shadow:0 2px 8px rgba(0,0,0,0.15); white-space:nowrap; }\n"
        ".city-popup .pn { font-weight:700; font-size:14px; color:#111; margin-bottom:2px; }\n"
        ".city-popup .pr { display:flex; gap:12px; align-items:center; }\n"
        ".city-popup .pt { font-size:18px; font-weight:700; }\n"
        ".city-popup .pw { color:#368994; }\n"
        ".city-popup .pp { color:#4FB1BE; }\n"
        ".city-popup .pi { font-size:16px; }\n"
        ".legend { font-size:11px; color:#AAA; margin-top:8px; }\n"
        ".city-detail-body { margin-top:4px; }\n"
        ".dashboard-layout { display:flex; gap:14px; align-items:stretch; margin-bottom:28px; }\n"
        ".map-panel { flex:1.15; min-width:0; }\n"
        ".map-panel .map-wrap { margin-bottom:0; }\n"
        ".weather-marker.sel { box-shadow:0 0 0 2px #FE5000,0 2px 6px rgba(0,0,0,0.2); }\n"
        ".cc-panel { flex:1; min-width:0; position:relative; background:#fff; border-radius:10px; box-shadow:0 1px 4px rgba(0,0,0,0.08); padding:14px 16px 10px; display:flex; flex-direction:column; }\n"
        ".cc-head { display:flex; justify-content:space-between; align-items:flex-end; gap:12px; padding-bottom:10px; border-bottom:1px solid #EEF0F3; }\n"
        ".cc-kicker { font-size:10px; font-weight:700; letter-spacing:.08em; text-transform:uppercase; color:#9A9FAB; }\n"
        ".cc-pick { position:relative; display:inline-flex; align-items:center; gap:7px; font-size:19px; font-weight:700; letter-spacing:-.01em; color:#111; cursor:pointer; border-radius:4px; }\n"
        ".cc-pick:hover #cc-name { color:#FE5000; }\n"
        ".cc-pick select { position:absolute; inset:0; width:100%; opacity:0; cursor:pointer; font-size:14px; }\n"
        ".cc-pick:has(select:focus-visible) { outline:2px solid #FE5000; outline-offset:2px; }\n"
        ".cc-now { font-size:11px; color:#9A9FAB; text-align:right; font-variant-numeric:tabular-nums; }\n"
        ".cc-charts { flex:1 1 0; min-height:0; overflow:hidden; display:flex; flex-direction:column; justify-content:space-between; gap:6px; padding-top:8px; }\n"
        ".cc-chart { position:relative; }\n"
        ".cc-ct { display:flex; align-items:baseline; gap:8px; font-size:12px; font-weight:700; color:#111; }\n"
        ".cc-ct .cc-sum { margin-left:auto; white-space:nowrap; font-weight:500; color:#5A5F6B; font-variant-numeric:tabular-nums; }\n"
        ".cc-key { display:inline-flex; align-items:center; gap:4px; font-size:10.5px; font-weight:500; color:#8A8F9C; }\n"
        ".cc-key i { display:inline-block; width:8px; height:8px; border-radius:2px; }\n"
        ".cc-chart svg { display:block; width:100%; overflow:visible; }\n"
        ".cc-chart svg text { font-family:inherit; font-size:10px; fill:#9A9FAB; font-variant-numeric:tabular-nums; }\n"
        ".cc-tip { position:absolute; pointer-events:none; background:#111; color:#fff; border-radius:6px; padding:7px 9px; font-size:11.5px; line-height:1.55; white-space:nowrap; box-shadow:0 4px 14px rgba(0,0,0,.18); z-index:5; font-variant-numeric:tabular-nums; }\n"
        ".cc-tip b { font-weight:700; }\n"
        ".cc-tip i { display:inline-block; width:7px; height:7px; border-radius:2px; margin-right:5px; }\n"
        "@media (max-width:1000px) { .dashboard-layout { flex-direction:column; } #citymap { height:440px; } .cc-charts { flex:none; overflow:visible; } }\n"
        ".weather-marker { background:#fff; border-radius:6px; padding:3px 8px; font-size:11px; box-shadow:0 2px 6px rgba(0,0,0,0.2); cursor:pointer; white-space:nowrap; border:1px solid #eee; }\n"
        ".weather-marker .wm-name { font-weight:700; color:#111; font-size:12px; }\n"
        ".weather-marker .wm-info { display:flex; gap:6px; align-items:center; margin-top:1px; }\n"
        ".weather-marker .wm-temp { font-weight:700; font-size:13px; color:#fff; border-radius:3px; padding:1px 4px; }\n"
        ".weather-marker .wm-wind { color:#368994; font-size:10px; }\n"
        ".weather-marker .wm-precip { color:#4FB1BE; font-size:10px; }\n"
    ) + detail_css

    map_js = (
        "mapboxgl.accessToken = '__TOKEN__';\n"
        "var markers = __MARKERS__;\n"
        "var map = new mapboxgl.Map({\n"
        "  container: 'citymap', style: 'mapbox://styles/mapbox/outdoors-v12',\n"
        "  center: [-122.5, 45.5], zoom: 5, pitch: 0, bearing: 0, attributionControl: false\n"
        "});\n"
        "map.fitBounds([[-124.5, 43.8], [-121.0, 48.1]], {padding: 20});\n"
        "map.on('load', function() {\n"
        "  map.addSource('mapbox-dem', {type:'raster-dem', url:'mapbox://mapbox.mapbox-terrain-dem-v1', tileSize:512});\n"
        "  map.setTerrain({source:'mapbox-dem', exaggeration:1.0});\n"
        "  var tc=function(h){if(h>=80)return'#ED1E29';if(h>=70)return'#FAA21B';if(h>=50)return'#6BBF68';if(h>=35)return'#4FB1BE';return'#368994';};\n"
        "  markers.forEach(function(m,i){\n"
        "    var el=document.createElement('div');el.className='weather-marker';\n"
        "    el.innerHTML='<div class=\"wm-name\">'+m.icon+' '+m.name+'</div>'\n"
        "      +'<div class=\"wm-info\"><span class=\"wm-temp\" style=\"background:'+tc(m.hi)+'\">'+m.hi+'\u00b0</span>'\n"
        "      +'<span class=\"wm-wind\">\U0001F32C'+m.wind+'</span>'\n"
        "      +'<span class=\"wm-precip\">\u2614'+m.precip+'%</span></div>';\n"
        "    el.addEventListener('click',function(){selectCity(i);});\n"
        "    window.cityMarkerEls[i]=el;\n"
        "    var popup=new mapboxgl.Popup({closeButton:false,closeOnClick:false,offset:15});\n"
        "    el.addEventListener('mouseenter',function(){\n"
        "      var h='<div class=\"city-popup\"><div class=\"pn\">'+m.name+'</div>'\n"
        "        +'<div class=\"pr\"><span class=\"pt\">'+m.hi+'\u00b0/'+m.lo+'\u00b0</span>'\n"
        "        +'<span class=\"pw\">\U0001F32C '+m.wind+'mph ('+m.gust+')</span>'\n"
        "        +'<span class=\"pp\">\u2614 '+m.precip+'%</span>'\n"
        "        +'<span class=\"pi\">'+m.icon+'</span></div></div>';\n"
        "      popup.setLngLat([m.lon,m.lat]).setHTML(h).addTo(map);\n"
        "    });\n"
        "    el.addEventListener('mouseleave',function(){popup.remove();});\n"
        "    new mapboxgl.Marker(el).setLngLat([m.lon,m.lat]).addTo(map);\n"
        "  });\n"
        "  selectCity(undefined,true);\n"
        "});\n"
    ).replace("__TOKEN__", MAPBOX_TOKEN).replace("__MARKERS__", markers_json)

    detail_sections = ""
    for ci, (city, body, sm) in enumerate(zip(CITIES, detail_bodies, summaries)):
        temps = f'<b>{sm["hi"]:.0f}°</b> / {sm["lo"]:.0f}°' if "hi" in sm else ''
        elev = f'{sm["elev_ft"]:,.0f}′ MSL · ' if "elev_ft" in sm else ''
        detail_sections += '<div class="city-detail" id="city_detail_' + str(ci) + '">'
        detail_sections += (
            '<div class="city-detail-header" onclick="var b=document.getElementById(\'city_body_' + str(ci) + '\');'
            'b.style.display=b.style.display==\'none\'?\'block\':\'none\';this.classList.toggle(\'collapsed\')">'
            f'<span class="ch-icon">{sm.get("icon", "")}</span>'
            f'<span class="ch-name">{html.escape(city["name"])}</span>'
            f'<span class="ch-temps">{temps}</span>'
            f'<span class="ch-meta">{elev}{city["lat"]:.4f}, {city["lon"]:.4f}</span>'
            '<svg class="ch-chev" aria-hidden="true"><use href="#ri-chev"/></svg>'
            '</div>')
        detail_sections += '<div id="city_body_' + str(ci) + '" class="city-detail-body">' + body + '</div>'
        detail_sections += '</div>'

    full_html = '<html><head><link href="https://api.mapbox.com/mapbox-gl-js/v3.3.0/mapbox-gl.css" rel="stylesheet"><script src="https://api.mapbox.com/mapbox-gl-js/v3.3.0/mapbox-gl.js"></script><style>' + dashboard_css + '</style></head><body>'
    opts = ''.join(f'<option value="{ci}">{html.escape(c["name"])}</option>' for ci, c in enumerate(CITIES))
    full_html += '<div class="dashboard-layout">'
    full_html += '<div class="map-panel"><div class="map-wrap"><div id="citymap"></div>'
    full_html += '<div class="legend">Click a city marker for its next 24 hours \u00b7 National Weather Service forecast \u00b7 ECMWF cloud layers \u00b7 Open-Meteo AQI</div>'
    full_html += '</div></div>'
    full_html += ('<div class="cc-panel" id="city-cc"><div class="cc-head"><div><div class="cc-kicker">Next 24 hours</div>'
                  '<label class="cc-pick"><span id="cc-name">' + html.escape(CITIES[0]["name"]) + '</span>'
                  '<svg width="10" height="6" aria-hidden="true"><path d="M1 1l4 4 4-4" fill="none" stroke="#A0A5B1" stroke-width="1.6"/></svg>'
                  '<select id="cc-city" aria-label="City" onchange="selectCity(+this.value)">' + opts + '</select></label></div>'
                  '<div class="cc-now"></div></div>'
                  '<div class="cc-charts"></div><div class="cc-tip" hidden></div></div>')
    full_html += '</div>'
    full_html += '<div class="detail-list">' + detail_sections + '</div>'
    full_html += '<script>' + CITY_JS.replace("__H24__", json.dumps([s.get("h24", []) for s in summaries])) + '</script>'
    full_html += '<script>' + map_js + '</script>'
    full_html += '</body></html>'
    return full_html


# ---------------------------------------------------------------------------
# Page 2: Mountain Trails - PNW overview map + 24-hour charts, 6-day forecasts below
# ---------------------------------------------------------------------------
# North to south. Waypoints run summit -> mid -> base; the first is the default view and
# the map-marker summary.
MOUNTAINS = [
    {"name": "Mt. Baker", "uid": "mbk",
     "waypoints": [{"name": "Grant Peak (Summit)", "lat": 48.7768, "lon": -121.8145},
                   {"name": "Coleman Glacier Camp", "lat": 48.7985, "lon": -121.8395},
                   {"name": "Heliotrope Ridge Trailhead", "lat": 48.8036, "lon": -121.8930}]},
    {"name": "Glacier Peak", "uid": "gp",
     "waypoints": [{"name": "Glacier Peak Summit", "lat": 48.1125, "lon": -121.1139},
                   {"name": "White Pass", "lat": 48.0850, "lon": -121.2130},
                   {"name": "North Fork Sauk Trailhead", "lat": 48.0960, "lon": -121.3700}]},
    {"name": "Mt. Olympus", "uid": "mo",
     "waypoints": [{"name": "West Peak (Summit)", "lat": 47.8013, "lon": -123.7108},
                   {"name": "Glacier Meadows", "lat": 47.8295, "lon": -123.6969},
                   {"name": "Hoh Rain Forest", "lat": 47.8606, "lon": -123.9347}]},
    {"name": "Mt. Rainier", "uid": "mr2",
     "waypoints": [{"name": "Columbia Crest (Summit)", "lat": 46.8529, "lon": -121.7604},
                   {"name": "Camp Muir", "lat": 46.8354, "lon": -121.7320},
                   {"name": "Paradise", "lat": 46.7865, "lon": -121.7353}]},
    {"name": "Mt. Adams", "uid": "ma2",
     "waypoints": [{"name": "Mt. Adams Summit", "lat": 46.2024, "lon": -121.4909},
                   {"name": "Lunch Counter", "lat": 46.1760, "lon": -121.4880},
                   {"name": "Cold Springs Trailhead", "lat": 46.1331, "lon": -121.4972}]},
    {"name": "Mt. St. Helens", "uid": "msh2",
     "waypoints": [{"name": "Summit Crater Rim", "lat": 46.18984, "lon": -122.18664},
                   {"name": "Midpoint", "lat": 46.16942, "lon": -122.19061},
                   {"name": "Trailhead", "lat": 46.14622, "lon": -122.18701}]},
    {"name": "Saddle Mountain", "uid": "sd",
     "waypoints": [{"name": "Saddle Mountain Summit", "lat": 45.9637, "lon": -123.6874},
                   {"name": "Saddle Mountain Trailhead", "lat": 45.9687, "lon": -123.6573}]},
    {"name": "Table Mountain", "uid": "tm2",
     "waypoints": [{"name": "Table Mountain Summit", "lat": 45.6907, "lon": -121.9837}]},
    {"name": "Mt. Hood", "uid": "mh2",
     "waypoints": [{"name": "Mt. Hood Summit", "lat": 45.3735, "lon": -121.6959},
                   {"name": "Top of Cascade", "lat": 45.34971, "lon": -121.68164},
                   {"name": "Meadows Base", "lat": 45.33144, "lon": -121.66413}]},
    {"name": "Sacajawea Peak", "uid": "sp",
     "waypoints": [{"name": "Sacajawea Peak Summit", "lat": 45.2450, "lon": -117.2930},
                   {"name": "Thorp Creek Basin", "lat": 45.2670, "lon": -117.2990},
                   {"name": "Hurricane Creek Trailhead", "lat": 45.3303, "lon": -117.3062}]},
    {"name": "Eagle Cap", "uid": "ec",
     "waypoints": [{"name": "Eagle Cap Summit", "lat": 45.1636, "lon": -117.3000},
                   {"name": "Mirror Lake", "lat": 45.1753, "lon": -117.3090},
                   {"name": "Two Pan Trailhead", "lat": 45.2500, "lon": -117.3760}]},
    {"name": "Mt. Jefferson", "uid": "mj2",
     "waypoints": [{"name": "Mt. Jefferson Summit", "lat": 44.6743, "lon": -121.7996},
                   {"name": "Jefferson Park", "lat": 44.6946, "lon": -121.7936},
                   {"name": "Whitewater Trailhead", "lat": 44.7176, "lon": -121.8686}]},
    {"name": "South Sister", "uid": "ss2",
     "waypoints": [{"name": "South Sister Peak", "lat": 44.1034, "lon": -121.7692},
                   {"name": "Midpoint", "lat": 44.08947, "lon": -121.76574},
                   {"name": "Base", "lat": 44.04065, "lon": -121.76375}]},
    {"name": "Mt. Bachelor", "uid": "mb2",
     "waypoints": [{"name": "Mt. Bachelor Summit", "lat": 43.9792, "lon": -121.6886},
                   {"name": "Pine Marten Lodge", "lat": 43.9868, "lon": -121.6927},
                   {"name": "West Village Base", "lat": 43.9905, "lon": -121.6803}]},
    {"name": "Mt. Thielsen", "uid": "mt",
     "waypoints": [{"name": "Mt. Thielsen Summit", "lat": 43.1528, "lon": -122.0664},
                   {"name": "PCT Junction", "lat": 43.1500, "lon": -122.0880},
                   {"name": "Mt. Thielsen Trailhead", "lat": 43.1440, "lon": -122.1270}]},
    {"name": "Crater Lake", "uid": "cl2",
     "waypoints": [{"name": "Mount Scott", "lat": 42.9230, "lon": -122.0158},
                   {"name": "Rim Village", "lat": 42.9101, "lon": -122.1450},
                   {"name": "Mazama Village", "lat": 42.8659, "lon": -122.1686}]},
]
# neighbours ~10 km apart: nudge their Trails-map pins apart (px)
PIN_OFFSET = {"ss2": [-12, 0], "mb2": [12, 4], "sp": [-10, 0], "ec": [10, 4]}
TIERS = {1: ["Summit"], 2: ["Summit", "Base"], 3: ["Summit", "Mid", "Base"]}

# Emoji-style silhouettes, one per mountain, drawn to its real profile: Rainier's broad
# glaciated dome, St. Helens' blown-out crater, Hood's sharp horn, Jefferson's spire,
# Adams' flat-topped bulk, South Sister's rust-red cone, Bachelor's smooth ski cone,
# Crater Lake's caldera with Wizard Island, Table Mountain's forested mesa.
# (rock colour, body path, snow path, extra svg) in a 32x20 box, base on y=20.
MTN_SHAPES = {
    "mr2": ("#8391A6", "M1 20L6.5 12.5Q8.5 9.8 10.5 9.4L12.5 7.2Q16 3.8 20 4.6Q23.5 5.6 25.5 9.5L31 20Z",
            "M3.5 16.5L6.5 12.5Q8.5 9.8 10.5 9.4L12.5 7.2Q16 3.8 20 4.6Q23.5 5.6 25.5 9.5L28.5 16L25.5 14.5L22.5 17L19.5 13.8L16.5 17L13.5 14L10.5 16.8L7.5 14.5Z", ""),
    "ma2": ("#8391A6", "M1 20L7 12.5L10.5 8.6L18.5 7.8L20.5 8.8L23.5 11.2L31 20Z",
            "M6 13.8L7 12.5L10.5 8.6L18.5 7.8L20.5 8.8L23.5 11.2L25 13.5L22 12.6L19.5 14.2L17 11.4L14.5 13.8L12 11.6L9 13.4Z", ""),
    "msh2": ("#978E86", "M1 20L10 9.5L13 9L15 11.5L18 11.5L20 8.8L23 9.5L31 20Z",
             "M7.5 12.5L10 9.5L13 9L14 10.3L11.5 11.5L9.5 13.5ZM19.3 9.4L20 8.8L23 9.5L25.8 13L23 12Z",
             '<path d="M15 11.5Q16.5 10.2 18 11.5Z" fill="#5E5B57"/>'),
    "tm2": ("#5F8F55", "M1 20L6.5 12.5L8.5 8L22.5 7.5L24.5 11L31 20Z", "",
            '<path d="M8.5 8L22.5 7.5L23.3 9L9 9.6Z" fill="#8E9A86"/>'),
    "mh2": ("#8391A6", "M2 20L12.5 7.5L15.5 2.5L17.2 4.5L19.2 7.5L30 20Z",
            "M9.5 11L12.5 7.5L15.5 2.5L17.2 4.5L19.2 7.5L22.5 11.5L20 10.5L17.8 13L15.6 9.8L13.2 12.8Z", ""),
    "mj2": ("#8391A6", "M3 20L11 11.5L13.5 7L15 2.5L16.3 5.3L17.5 4.2L19.2 8.5L22 11.5L29 20Z",
            "M8.5 14.5L11 11.5L13.5 7L14.3 8.8L13.2 12.5L16 10L17.3 12.8L19.2 8.5L22 11.5L24.5 14.5L21.5 13.5L19 15.5L16.5 13L14 15.5L11.5 13.2Z", ""),
    "ss2": ("#9A6B58", "M2 20L12 8.5Q16 5 20 8.5L30 20Z",
            "M9 12L12 8.5Q16 5 20 8.5L23 12L20.5 11L18 13L16 10.8L14 13L11.5 11Z",
            '<path d="M13.6 7.2Q16 5.4 18.4 7.2Q16 6.6 13.6 7.2Z" fill="#B0553E"/>'),
    "mb2": ("#8391A6", "M2 20L14 7.5Q16 6 18 7.5L30 20Z",
            "M5 17L14 7.5Q16 6 18 7.5L27 17L23.5 15.5L20.5 17.5L16.5 15L12.5 17.5L9 15.5Z",
            '<path d="M16 7L13.5 16.5M16 7L19 16.5" stroke="#B9C2CE" stroke-width=".7" fill="none"/>'),
    "cl2": ("#8391A6", "M1 20L6 11.5L10 9.8L12 12.5L20 12.5L22 9.8L26 11L31 20Z",
            "M4.5 14L6 11.5L10 9.8L11.2 11.3L8.5 12.5L6.5 14.8ZM21 11L22 9.8L26 11L27.8 14L25 12.8L22.8 13Z",
            '<path d="M11 12.5L21 12.5Q22.2 14.2 20.2 15.4L11.8 15.4Q9.8 14.2 11 12.5Z" fill="#2E6DB4"/>'
            '<path d="M12.6 14.6L13.9 13.1L15.2 14.6Z" fill="#5E6776"/>'),
    "mbk": ("#8391A6", "M1 20L10 9.5Q16 4.5 22 9.5L31 20Z",
            "M4.5 16L10 9.5Q16 4.5 22 9.5L27.5 16L24.5 14.6L21.5 16.8L18.5 13.6L15.5 16.8L12.5 13.8L9 16.2Z", ""),
    "gp": ("#8391A6", "M2 20L8 12L11 10L13 6.5L15.5 3.5L17.5 5.5L19 5L21 8.5L24 11L30 20Z",
           "M5.5 15.8L8 12L11 10L13 6.5L15.5 3.5L17.5 5.5L19 5L21 8.5L24 11L26.8 15.4L24 14L21.5 16.2L19 13L16.5 16.4L14 13.4L11 16L8.5 14.4Z", ""),
    "mo": ("#8391A6", "M1 20L5 13L8 10L10 12L13 6L15 8L17 4.5L19.5 8.5L21.5 7L24 11L27 13.5L31 20Z",
           "M3.5 16L5 13L8 10L10 12L13 6L15 8L17 4.5L19.5 8.5L21.5 7L24 11L27 13.5L28.5 16L25.5 14.8L22.5 17L19.5 14L16.5 17L13.5 14.2L10.5 16.6L7 14.8Z", ""),
    "sd": ("#5F8F55", "M1 20L6 12.5Q9 8.5 12 10.5Q14.5 12.5 17 11Q19.5 6 22.5 7Q25.5 8.2 27 13L31 20Z", "",
           '<path d="M19.3 8.2Q21.5 6.4 24 7.8L23.2 9.4Q21.4 8.5 19.3 8.2Z" fill="#8E8A80"/>'),
    "sp": ("#A8A39A", "M2 20L9 12L13 8L16 4L18.5 7L21 6.5L24 10.5L30 20Z",
           "M14.2 6.6L16 4L17.2 5.6L15.6 7.8ZM20.2 7.2L21 6.5L22.6 8.6L21.4 9.2Z", ""),
    "ec": ("#A8A39A", "M2 20L10 10.5L14 6.5Q16 5 18 6.5L22 10.5L30 20Z",
           "M12.6 8L14 6.5Q16 5 18 6.5L19.6 8.2L18 8.9L16.2 7.9L14.4 9Z",
           '<ellipse cx="11.5" cy="17.4" rx="3.6" ry="1" fill="#3C7FC0"/>'),
    "mt": ("#8E8A80", "M4 20L12 12L14.5 7L16 1.5L17.5 7L20 12L28 20Z",
           "M9 15L12 12L13.6 9L14.6 11.5L16 10L17.4 11.6L18.4 9L20 12L23 15L20.5 14.2L18 15.8L16 13.8L14 15.8L11.5 14.2Z", ""),
}


# The 3D look: light from the upper left, so everything right of a line down from the peak
# (x = MTN_APEX) sits in shade; a band of forest round the base where the real mountain has
# one, and a soft ground shadow. Each icon gets its own clipPath id (duplicate ids on a page
# can clip the wrong shape).
MTN_APEX = {"mbk": 16, "gp": 15.5, "mo": 17, "sd": 22, "sp": 16, "ec": 16, "mt": 16, "mr2": 17, "ma2": 16, "msh2": 17, "tm2": 21, "mh2": 15.5, "mj2": 15, "ss2": 16, "mb2": 16, "cl2": 21}
MTN_FOREST = {"mbk", "gp", "mo", "sd", "sp", "ec", "mt", "mr2", "ma2", "mh2", "mj2", "ss2", "mb2", "cl2"}
_icon_ids = itertools.count()


MTN_ABOVE = {"mbk": '<path d="M20.5 6.4q.6-1.8 2.3-1.3q1.3-1.6 3-.3q1.8.1 1.3 1.6q-.3 .9-1.8 .8h-3.8q-1.4-.1-1-.8z" fill="#fff" stroke="#2E3440" stroke-width=".45"/>'}
PIN_SCALE = {"sd": 0.8}
DEFAULT_MTN = next(i for i, m in enumerate(MOUNTAINS) if m["uid"] == "mh2")   # the Trails tab opens on Mt. Hood   # Saddle Mountain is a baby


def mtn_icon(uid, w=28, cls="mtn-ic"):
    rock, body, snow, extra = MTN_SHAPES[uid]
    ax, k = MTN_APEX[uid], next(_icon_ids)
    forest = ('<path d="M0 17.6Q5 16.4 10 17.4T20 17.2T32 17V21H0Z" fill="#4F8A4B"/>'
              '<path d="M0 18.8Q6 17.9 12 18.7T24 18.5T32 18.6V21H0Z" fill="#3F7440"/>') if uid in MTN_FOREST else ""
    snow_p = f'<path d="{snow}" fill="#fff"/>' if snow else ""
    return (f'<svg class="{cls}" viewBox="0 0 32 22" width="{w}" height="{w * 22 / 32:.0f}" aria-hidden="true">'
            f'<defs><clipPath id="mc{k}"><path d="{body}"/></clipPath></defs>'
            '<ellipse cx="16" cy="20.8" rx="15" ry="1.2" fill="#1E2633" opacity=".18"/>'
            f'<path d="{body}" fill="{rock}"/>'
            f'<g clip-path="url(#mc{k})">{forest}{snow_p}{extra}'
            f'<path d="M{ax} 0L{ax + 2.5} 21H32V0Z" fill="#1E2633" opacity=".24"/>'
            f'<path d="M{ax - 14} 0L{ax - 12} 21H{ax - 7}L{ax - 9} 0Z" fill="#fff" opacity=".10"/></g>'
            f'<path d="{body}" fill="none" stroke="#2E3440" stroke-width=".9" stroke-linejoin="round"/>{MTN_ABOVE.get(uid, "")}</svg>')


TRAILS_JS = r"""
var MT=__MT__, UIDS=MT.map(function(m){return m.uid;}), sel=-1, tier=0;
var charts=WxCharts(document.getElementById('mtn-cc')), tiers=document.getElementById('mtn-tiers');

// ---- current weather drawn onto the selected mountain's pin, in the icon's own 32x22 units:
// sun/moon behind the peak; clouds, falling rain/snow and wind streaks in front ----
var CLOUD='M-4.5 2H4.5A2 2 0 0 0 4 -1.8A2.6 2.6 0 0 0 -0.5 -2.6A2.3 2.3 0 0 0 -4.3 -0.6A1.5 1.5 0 0 0 -4.5 2Z';
function cloud(x,y,sc,grey){return'<path d="'+CLOUD+'" transform="translate('+x+' '+y+') scale('+sc+')" fill="'+(grey?'#C9D0DA':'#F4F6F9')+'" stroke="#2E3440" stroke-width="'+(0.5/sc).toFixed(2)+'" stroke-linejoin="round"/>';}
function sun(x,y){var r='';for(var k=0;k<8;k++){var a=k*Math.PI/4;r+='<line x1="'+(x+Math.cos(a)*3.4).toFixed(2)+'" y1="'+(y+Math.sin(a)*3.4).toFixed(2)+'" x2="'+(x+Math.cos(a)*4.6).toFixed(2)+'" y2="'+(y+Math.sin(a)*4.6).toFixed(2)+'"/>';}
  return'<g class="wx-spin" style="transform-origin:'+x+'px '+y+'px"><g stroke="#F2A516" stroke-width=".8" stroke-linecap="round">'+r+'</g></g><circle cx="'+x+'" cy="'+y+'" r="2.7" fill="#FFC83D" stroke="#E0931A" stroke-width=".5"/>';}
function moon(x,y){return'<path d="M'+x+' '+(y-3)+'A3 3 0 1 0 '+(x+2.6)+' '+(y+1.6)+'A2.3 2.3 0 1 1 '+x+' '+(y-3)+'Z" fill="#F3E3A1" stroke="#B89A3E" stroke-width=".45"/>';}
function stars(){return[[5,1.5],[9.5,-0.5],[20.5,0.2],[2.5,5.5]].map(function(p,i){return'<path class="wx-twinkle" style="animation-delay:'+(i*0.7)+'s" d="M'+p[0]+' '+(p[1]-1)+'L'+(p[0]+0.3)+' '+(p[1]-0.3)+'L'+(p[0]+1)+' '+p[1]+'L'+(p[0]+0.3)+' '+(p[1]+0.3)+'L'+p[0]+' '+(p[1]+1)+'L'+(p[0]-0.3)+' '+(p[1]+0.3)+'L'+(p[0]-1)+' '+p[1]+'L'+(p[0]-0.3)+' '+(p[1]-0.3)+'Z" fill="#F7E7A6" stroke="#B89A3E" stroke-width=".3"/>';}).join('');}
function fall(kind){   // rain streaks / snowflakes dropping from the storm cloud over the peak, three staggered rows
  var o='',pts=[[10,4.2],[13,4.8],[16,4.4],[19,4.9],[22,4.3],[11.5,8],[14.5,8.6],[17.5,8.2],[20.5,8.7],[10,11.8],[13,12.4],[16,12],[19,12.5],[22,11.9]];
  pts.forEach(function(q,i){var k=kind==='mix'?(i%2?'snow':'rain'):kind,x=q[0],y=q[1],st='style="animation-delay:'+((i*0.37)%1.3).toFixed(2)+'s"';
    o+=k==='snow'?'<circle class="wx-fall" '+st+' cx="'+x+'" cy="'+y+'" r=".85" fill="#fff" stroke="#5A4FCF" stroke-width=".45"/>'
      :'<line class="wx-fall" '+st+' x1="'+x+'" y1="'+y+'" x2="'+(x-0.8)+'" y2="'+(y+2.4)+'" stroke="#0E9AAE" stroke-width=".75" stroke-linecap="round"/>';});
  return o;}
function wind(){return'<g fill="none" stroke="#368994" stroke-width=".7" stroke-linecap="round">'
  +'<path class="wx-blow" d="M-3 7.5H6Q8.2 7.5 8.2 5.8Q8.2 4.6 7 4.6"/>'
  +'<path class="wx-blow" style="animation-delay:.4s" d="M-5 10.5H9"/>'
  +'<path class="wx-blow" style="animation-delay:.8s" d="M-2 13.5H5Q7 13.5 7 15Q7 16 6 16"/></g>';}
function isNight(t){var m=/(\d+)(AM|PM)/.exec(t);if(!m)return false;var h=+m[1]%12+(m[2]==='PM'?12:0);return h<7||h>=19;}
function wxArt(h){
  var night=isNight(h.t),wet=h.ty&&h.p>=0.005,sky=h.sky,back='',front='',name;
  if(wet){front=cloud(16,1.2,1.35,true)+cloud(22.5,2.6,0.9,true)+fall(h.ty);name={rain:'Rain',snow:'Snow',mix:'Rain & snow'}[h.ty];}
  else if(sky<30){back=night?moon(26,3.2)+stars():sun(26,3.4);name=night?'Clear':'Sunny';}
  else if(sky<75){back=night?moon(25,3):sun(25,3.2);front=cloud(27.5,5.6,0.85,false);name='Partly cloudy';}
  else{front=cloud(12.5,6,1,false)+cloud(19.5,5.2,1.15,false);name='Cloudy';}
  if(h.gust>=30){front+=wind();name=(name==='Sunny'||name==='Clear'?'Windy':name+', windy');}
  return{back:back,front:front,label:name+' \u00b7 '+h.temp+'\u00b0'};
}
window.wxArt=wxArt;   // also used by the Mt Hood page's video poster
function pinWx(){   // only the selected pin carries weather; the rest stay plain mountains
  document.querySelectorAll('.mtn-pin').forEach(function(el,j){
    el.querySelectorAll('.mtn-wx').forEach(function(x){x.remove();});
    var nm=el.querySelector('.mtn-pin-name');if(nm)nm.innerHTML=MT[j].name;
    if(j!==sel)return;
    var p=MT[j].points[tier]||MT[j].points[0],h=p&&p.h24[0];if(!h)return;
    var w=wxArt(h),ic=el.querySelector('.mtn-ic'),svg=function(c,cls){return'<svg class="mtn-wx '+cls+'" viewBox="0 0 32 22" aria-hidden="true">'+c+'</svg>';};
    if(w.back)ic.insertAdjacentHTML('beforebegin',svg(w.back,'mtn-wx-back'));
    if(w.front)ic.insertAdjacentHTML('afterend',svg(w.front,'mtn-wx-front'));
    if(nm)nm.innerHTML=MT[j].name+'<small>'+w.label+'</small>';
  });
  if(window.pinFlip)window.pinFlip();   // the label's width changed: re-check which side it fits on
}

// ---- cameras (the shared WxCams view): the selected mountain's list ----
var view='fc',cam=0,camsEl=document.getElementById('mtn-cams'),camsView=WxCams(camsEl);
function camList(){return (MT[sel]&&MT[sel].cams)||[];}
function setView(v){
  var n=camList().length;if(v==='cams'&&!n)v='fc';view=v;
  document.querySelectorAll('#mtn-view [data-view]').forEach(function(b){b.classList.toggle('active',b.dataset.view===v);});
  document.getElementById('mtn-view').hidden=!n;
  document.getElementById('mtn-camn').textContent=n?' '+n:'';
  var fc=v==='fc';camsEl.hidden=fc;document.querySelector('#mtn-cc .cc-charts').hidden=!fc;
  tiers.hidden=!fc||MT[sel].points.length<2;
  if(!fc)camsView.show(camList(),Math.min(cam,n-1),function(j){cam=j;});
}
document.getElementById('mtn-view').addEventListener('click',function(ev){var b=ev.target.closest('[data-view]');if(b)setView(b.dataset.view);});

function drawTiers(){
  var m=MT[sel];
  tiers.innerHTML=m.points.map(function(p,j){return'<button class="'+(j===tier?'active':'')+'" data-t="'+j+'" title="'+p.name+'">'
    +m.tiers[j]+'<small>'+p.elev_ft.toLocaleString('en-US')+'\u2032</small></button>';}).join('');
  tiers.hidden=m.points.length<2||view==='cams';
  document.getElementById('mtn-wp').textContent=m.points[tier].name;
}
function show(){
  var m=MT[sel];drawTiers();charts.set(m.points[tier].h24);pinWx();
  var w=window['showWp_'+m.uid];if(w&&tier<m.points.length)w(tier);
}
// the chosen mountain: its charts (summit by default), its marker, and its 6-day forecast first
window.selectMountain=function(i,init){
  if(i===undefined)i=sel;
  if(i!==sel){tier=0;cam=0;}sel=i;
  var s=document.getElementById('mtn-pick');if(s){s.value=i;document.getElementById('mtn-name').innerHTML=MT[i].icon+MT[i].name;}
  document.querySelectorAll('.mtn-marker').forEach(function(el,j){el.classList.toggle('active',j===i);});
  if(window.pinFlip)window.pinFlip();
  var d=document.getElementById('mtn_'+i);if(d&&!init)d.parentNode.insertBefore(d,d.parentNode.firstChild);
  show();
  setView(view);   // stays on Cameras if this mountain has any, else back to the forecast
  if(window.ovApply)window.ovApply();
};
tiers.addEventListener('click',function(ev){var b=ev.target.closest('[data-t]');if(!b)return;tier=+b.dataset.t;show();});
// a waypoint chip in the selected mountain's forecast table moves the charts (and pin weather) with it
window.onWp=function(uid,j){if(uid===UIDS[sel]&&j!==tier){tier=j;drawTiers();charts.set(MT[sel].points[tier].h24);pinWx();}};
window.trailsShown=function(){window.runLazy('trails-overview');show();};
selectMountain(__DEF__);   // opens on Mt. Hood
"""


def build_trails_page(aq):
    bodies = []; summaries = []; detail_css = ""
    def fetch(m):
        pts = [nws.point(w["lat"], w["lon"]) for w in m["waypoints"]]   # the base forecast, per waypoint's grid box
        blend = blended_qpf(m["waypoints"], 7)                         # fallback where the NWS has no hours
        qpf = [merge_qpf(p, b) for p, b in zip(pts, blend)]
        profile = elevation_profile(m["waypoints"], qpf, nws_pts=pts)
        return render_hike_forecast(m["waypoints"], m["name"], m["uid"], profile=profile, qpf=qpf, nws_pts=pts)

    print(f"Generating {len(MOUNTAINS)} mountain forecasts ({FETCH_WORKERS} at a time)...")
    with ThreadPoolExecutor(max_workers=FETCH_WORKERS) as ex:
        rendered = list(ex.map(fetch, MOUNTAINS))
    for mi, (m, (full_html, sm)) in enumerate(zip(MOUNTAINS, rendered)):
        print(f"  {m['name']}")
        if mi == 0:
            css_m = re.search(r'<style>(.*?)</style>', full_html, re.DOTALL)
            detail_css = css_m.group(1) if css_m else ""
        body_m = re.search(r'<body>(.*?)</body>', full_html, re.DOTALL)
        bodies.append(body_m.group(1) if body_m else full_html)
        pk = m["waypoints"][0]
        summaries.append({**sm, "name": m["name"], "lat": pk["lat"], "lon": pk["lon"]})

    markers_json = json.dumps([{
        "name": s["name"], "lat": s["lat"], "lon": s["lon"], "icon": mtn_icon(m["uid"], 38),
        "off": PIN_OFFSET.get(m["uid"], [0, 0]), "scale": PIN_SCALE.get(m["uid"], 1),
    } for m, s in zip(MOUNTAINS, summaries)])
    print("Checking mountain webcams...")
    cams = webcams.build()
    mt_json = json.dumps([{
        "uid": m["uid"], "name": m["name"], "icon": mtn_icon(m["uid"], 30), "tiers": TIERS[len(m["waypoints"])],
        "points": s.get("points", []), "cams": cams.get(m["uid"], []),
    } for m, s in zip(MOUNTAINS, summaries)], separators=(",", ":"))

    details = ""
    for mi, (m, body, s) in enumerate(zip(MOUNTAINS, bodies, summaries)):
        pk = m["waypoints"][0]
        temps = f'<b>{s["hi"]:.0f}\u00b0</b> / {s["lo"]:.0f}\u00b0 at the summit today' if "hi" in s else ''
        elev = f'Summit {s["elev_ft"]:,.0f}\u2032 MSL \u00b7 ' if "elev_ft" in s else ''
        details += (
            f'<section class="city-detail mtn-detail" id="mtn_{mi}">'
            '<div class="city-detail-header" onclick="var b=this.nextElementSibling;'
            'b.style.display=b.style.display===\'none\'?\'block\':\'none\';this.classList.toggle(\'collapsed\')">'
            f'<span class="ch-icon">{mtn_icon(m["uid"], 30)}</span>'
            f'<span class="ch-name">{html.escape(m["name"])}</span>'
            f'<span class="ch-temps">{s.get("icon", "")} {temps}</span>'
            f'<span class="ch-meta">{elev}{pk["lat"]:.4f}, {pk["lon"]:.4f}</span>'
            '<svg class="ch-chev" aria-hidden="true"><use href="#ri-chev"/></svg></div>'
            '<div class="city-detail-body">' + body + '</div></section>')

    page_css = (
        ".trails .hike-layout { display:block; }\n"
        "#trailmap { width:100%; height:600px; border-radius:8px; }\n"
        ".trail-map-wrap { position:relative; }\n"
        ".mtn-ic { display:block; flex:none; overflow:visible; }\n"
        # map pins: just the mountain; hover lifts it and shows its name, the selected one doubles in size
        ".mtn-pin { width:calc(38px * var(--s,1)); height:calc(26px * var(--s,1)); padding:0; border:none; background:none; cursor:pointer; }\n"
        ".mtn-pin .mtn-ic { width:100%; height:100%; transform-origin:50% 100%; transition:transform .2s ease; filter:drop-shadow(0 1px 1.5px rgba(0,0,0,.35)); }\n"
        ".mtn-pin:hover .mtn-ic, .mtn-pin:focus-visible .mtn-ic { transform:scale(1.15); }\n"
        ".mtn-pin.active { z-index:3; }\n"
        ".mtn-pin.active .mtn-ic { transform:scale(2); }\n"
        ".mtn-pin .mtn-wx { position:absolute; left:0; top:0; width:100%; height:100%; overflow:visible; transform:scale(2); transform-origin:50% 100%; pointer-events:none; }\n"
        ".mtn-pin .mtn-ic { position:relative; }\n"
        ".mtn-pin-name small { display:block; font-size:11px; font-weight:600; color:#5A5F6B; }\n"
        "@keyframes wxfall { from { transform:translateY(-1px); opacity:0; } 15% { opacity:1; } 75% { opacity:1; } to { transform:translateY(3.5px); opacity:0; } }\n"
        "@keyframes wxblow { from { stroke-dashoffset:14; opacity:0; } 30% { opacity:1; } to { stroke-dashoffset:-14; opacity:0; } }\n"
        "@keyframes wxspin { to { transform:rotate(45deg); } }\n"
        "@keyframes wxtwinkle { 50% { opacity:.25; } }\n"
        ".mtn-wx .wx-fall { animation:wxfall 1.3s linear infinite; }\n"
        ".mtn-wx .wx-blow { stroke-dasharray:14 14; animation:wxblow 2.2s ease-in-out infinite; }\n"
        ".mtn-wx .wx-spin { animation:wxspin 6s linear infinite; }\n"
        ".mtn-wx .wx-twinkle { animation:wxtwinkle 2.4s ease-in-out infinite; }\n"
        "@media (prefers-reduced-motion:reduce) { .mtn-wx * { animation:none !important; } }\n"
        ".mtn-pin:focus-visible { outline:none; }\n"
        # name sits just off the icon's right side, level with its middle (a 2x icon grows up from its base)
        ".mtn-pin-name { position:absolute; left:calc(100% + 1px); top:60%; transform:translateY(-50%); padding:2px 8px; border-radius:999px; background:#fff; color:#111;"
        " font-size:12px; font-weight:700; white-space:nowrap; box-shadow:0 1px 4px rgba(0,0,0,.25); opacity:0; pointer-events:none; transition:opacity .15s, left .2s ease, right .2s ease, top .2s ease; }\n"
        ".mtn-pin:hover .mtn-pin-name, .mtn-pin:focus-visible .mtn-pin-name { opacity:1; left:calc(107.5% + 1px); }\n"
        ".mtn-pin.active .mtn-pin-name { opacity:1; left:calc(150% - 1px); top:15%; font-size:13px; }\n"
        # near the map's right edge the name goes on the left instead
        ".mtn-pin.flip .mtn-pin-name { left:auto; right:calc(100% + 1px); }\n"
        ".mtn-pin.flip:hover .mtn-pin-name, .mtn-pin.flip:focus-visible .mtn-pin-name { left:auto; right:calc(107.5% + 1px); }\n"
        ".mtn-pin.flip.active .mtn-pin-name { left:auto; right:calc(150% - 1px); }\n"
        "@media (prefers-reduced-motion:reduce) { .mtn-pin .mtn-ic, .mtn-pin-name { transition:none; } }\n"
        ".mtn-detail .wp-chips { margin:0 0 4px; }\n"
        # waypoint name is already on the active button; keep just its elevation + coordinates
        ".mtn-detail .wp-header .wp-dot, .mtn-detail .wp-header .wp-name { display:none; }\n"
        ".mtn-detail .wp-header { margin:0 0 8px 2px; }\n"
        ".wm-info .rl { width:1.1em; height:1.1em; vertical-align:-0.2em; color:#368994; }\n"
        "#mtn-name { display:flex; align-items:center; gap:8px; white-space:nowrap; }\n"
        ".cc-tiers { display:inline-flex; gap:2px; padding:3px; margin-top:10px; background:#EBEDF1; border-radius:9px; align-self:flex-start; }\n"
        ".cc-tiers button { border:none; background:none; padding:5px 12px; border-radius:7px; font:inherit; font-size:12.5px; font-weight:600; color:#5A5F6B; cursor:pointer; }\n"
        ".cc-tiers button small { margin-left:6px; font-size:11px; font-weight:500; color:#9A9FAB; font-variant-numeric:tabular-nums; }\n"
        ".cc-tiers button:hover { color:#111; }\n"
        ".cc-tiers button.active { background:#fff; color:#111; box-shadow:0 0 0 1px #E3E5EA, 0 1px 2px rgba(20,24,35,.06); }\n"
        ".cc-tiers button:focus-visible { outline:2px solid #FE5000; outline-offset:2px; }\n"
        ".cc-wp { font-size:11px; color:#9A9FAB; margin-top:3px; }\n"
        # Forecast | Cameras switch and the camera view
        ".cc-head-r { display:flex; flex-direction:column; align-items:flex-end; gap:6px; }\n"
        ".cc-view { display:inline-flex; gap:2px; padding:3px; background:#EBEDF1; border-radius:9px; }\n"
        ".cc-view[hidden] { display:none; }\n"
        ".cc-view button { white-space:nowrap; border:none; background:none; padding:4px 11px; border-radius:7px; font:inherit; font-size:12px; font-weight:600; color:#5A5F6B; cursor:pointer; }\n"
        ".cc-view button:hover { color:#111; }\n"
        ".cc-view button.active { background:#fff; color:#111; box-shadow:0 0 0 1px #E3E5EA, 0 1px 2px rgba(20,24,35,.06); }\n"
        ".cc-view button:focus-visible { outline:2px solid #FE5000; outline-offset:2px; }\n"
        ".cc-view button span { margin-left:5px; font-size:11px; color:#9A9FAB; font-variant-numeric:tabular-nums; }\n"
        ".cc-cams { flex:1 1 0; min-height:0; display:flex; flex-direction:column; gap:8px; padding-top:10px; }\n"
        ".cc-cams[hidden], .cc-charts[hidden], .cc-tiers[hidden] { display:none; }\n"
        ".cam-main { position:relative; margin:0; border-radius:8px; overflow:hidden; background:#10131A; aspect-ratio:3/2; flex:none; }\n"
        ".cam-main img { position:absolute; inset:0; width:100%; height:100%; object-fit:cover; }\n"
        ".cam-msg { position:absolute; inset:0; display:flex; align-items:center; justify-content:center; padding:20px; text-align:center; color:#C9CDD5; font-size:13px; }\n"
        ".cam-msg[hidden] { display:none; }\n"
        ".cam-thumbs { display:grid; grid-template-columns:repeat(auto-fill,minmax(96px,1fr)); gap:8px; }\n"
        ".cam-thumbs:empty { display:none; }\n"
        ".cam-thumbs button { display:flex; flex-direction:column; gap:4px; padding:0; border:none; background:none; font:inherit; font-size:11.5px; font-weight:600; color:#5A5F6B; text-align:left; cursor:pointer; }\n"
        ".cam-thumbs img { width:100%; aspect-ratio:3/2; object-fit:cover; border-radius:6px; background:#10131A; transition:opacity .15s; }\n"
        ".cam-thumbs button:hover { color:#111; }\n"
        ".cam-thumbs button:hover img { opacity:.85; }\n"
        ".cam-thumbs button:focus-visible { outline:2px solid #FE5000; outline-offset:2px; border-radius:6px; }\n"
        ".cam-cap { font-size:12px; color:#5A5F6B; line-height:1.5; }\n"
        ".cam-cap b { color:#111; }\n"
        ".cam-cap .cam-when { font-variant-numeric:tabular-nums; }\n"
        ".cam-cap .cam-night { color:#8A5A00; }\n"
        ".cam-cap button { border:none; background:none; padding:0; font:inherit; font-weight:600; color:#FE5000; cursor:pointer; }\n"
        ".cam-cap a { color:#8A8F9C; }\n"
    )
    map_js = (
        "mapboxgl.accessToken='__TOKEN__';\n"
        "var mtns=__MTNS__;\n"
        "var tmap=new mapboxgl.Map({container:'trailmap',style:'mapbox://styles/mapbox/outdoors-v12',center:[-121.9,45.0],zoom:6,attributionControl:false});\n"
        "tmap.fitBounds([[-122.3,44.55],[-121.2,46.95]],{padding:{top:70,bottom:30,left:20,right:20}});   // Jefferson to Rainier; pan for the rest\n"
        "tmap.on('load',function(){\n"
        "  tmap.addSource('mapbox-dem',{type:'raster-dem',url:'mapbox://mapbox.mapbox-terrain-dem-v1',tileSize:512});\n"
        "  tmap.setTerrain({source:'mapbox-dem',exaggeration:1.2});\n"
        "  mtns.forEach(function(m,i){\n"
        "    var el=document.createElement('button');el.className='mtn-marker mtn-pin';el.setAttribute('aria-label',m.name);el.style.setProperty('--s',m.scale);\n"
        "    el.innerHTML=m.icon+'<span class=\"mtn-pin-name\">'+m.name+'</span>';\n"
        "    el.addEventListener('click',function(){window.selectMountain(i);});\n"
        "    new mapboxgl.Marker({element:el,anchor:'bottom',offset:m.off}).setLngLat([m.lon,m.lat]).addTo(tmap);\n"
        "  });\n"
        "  window.pinFlip=function(){var r=tmap.getContainer().getBoundingClientRect();\n"
        "    document.querySelectorAll('.mtn-pin').forEach(function(el){var b=el.getBoundingClientRect(),n=el.querySelector('.mtn-pin-name');\n"
        "      var reach=b.right+(el.classList.contains('active')?b.width*0.5:b.width*0.1)+(n?n.offsetWidth:120)+8;   // where its name would end on the right\n"
        "      el.classList.toggle('flip',reach>r.right);});};\n"
        "  tmap.on('moveend',window.pinFlip);\n"
        "  window.selectMountain(undefined,true);\n"
        "  if(window.AQL){var sym;tmap.getStyle().layers.some(function(l){if(l.type==='symbol'){sym=l.id;return true;}});window.AQL.attach(tmap,sym);}\n"
        "});\n"
        # overview-map air-quality toggle (regional smoke is best seen at this scale)
        "var ov={l:'none',d:0},octl=document.getElementById('ovlyr'),oleg=document.getElementById('ovleg'),ovApply=null;\n"
        "if(octl&&window.AQL){\n"
        "  ovApply=function(){\n"
        "    octl.querySelectorAll('[data-l]').forEach(function(b){b.classList.toggle('active',b.dataset.l===ov.l);});\n"
        "    octl.querySelectorAll('[data-d]').forEach(function(b){b.classList.toggle('active',+b.dataset.d===ov.d);});\n"
        "    octl.querySelector('.lyr-days').hidden=ov.l==='none';\n"
        "    var p=document.getElementById('mtn-pick'),m=mtns[p?+p.value:0];\n"
        "    if(ov.l==='aq'){oleg.innerHTML=window.AQL.legend(ov.d,m.lat,m.lon,m.name);oleg.hidden=false;}else oleg.hidden=true;\n"
        "    window.AQL.show(tmap,ov.d,ov.l==='aq');\n"
        "  };\n"
        "  window.ovApply=ovApply;\n"
        "  octl.addEventListener('click',function(ev){var b=ev.target.closest('button');if(!b)return;\n"
        "    if(b.dataset.l)ov.l=b.dataset.l;if(b.dataset.d)ov.d=+b.dataset.d;ovApply();});\n"
        "}\n"
    ).replace("__TOKEN__", MAPBOX_TOKEN).replace("__MTNS__", markers_json)

    ov_ctl = ""
    if aq:
        ov_days = "".join(f'<button data-d="{i}"{" class=active" if i == 0 else ""}>{d["label"]}</button>'
                          for i, d in enumerate(aq["days"]))
        ov_ctl = ('<div class="lyr-ctl" id="ovlyr"><div><button class="active" data-l="none">Map</button>'
                  '<button data-l="aq">Air quality</button></div><div class="lyr-days" hidden>' + ov_days + '</div></div>'
                  '<div class="lyr-leg" id="ovleg" hidden></div>')

    opts = ''.join(f'<option value="{mi}">{html.escape(m["name"])}</option>' for mi, m in enumerate(MOUNTAINS))
    # page_css after detail_css so the trail-specific layout wins over render_hike_forecast's defaults
    full_html = '<html><head><style>' + detail_css + page_css + '</style></head><body><div class="trails">'
    full_html += ('<div class="dashboard-layout">'
                  '<div class="map-panel"><div class="map-wrap trail-map-wrap"><div id="trailmap"></div>' + ov_ctl +
                  '<div class="legend">Click a mountain for its next 24 hours</div></div></div>'
                  '<div class="cc-panel" id="mtn-cc"><div class="cc-head"><div><div class="cc-kicker">Next 24 hours</div>'
                  '<label class="cc-pick"><span id="mtn-name">' + mtn_icon(MOUNTAINS[DEFAULT_MTN]["uid"], 30) + html.escape(MOUNTAINS[DEFAULT_MTN]["name"]) + '</span>'
                  '<svg width="10" height="6" aria-hidden="true"><path d="M1 1l4 4 4-4" fill="none" stroke="#A0A5B1" stroke-width="1.6"/></svg>'
                  '<select id="mtn-pick" aria-label="Mountain" onchange="selectMountain(+this.value)">' + opts + '</select></label>'
                  '<div class="cc-wp" id="mtn-wp"></div></div>'
                  '<div class="cc-head-r"><div class="cc-view" id="mtn-view" role="group" aria-label="View">'
                  '<button class="active" data-view="fc">Forecast</button><button data-view="cams">Cameras<span id="mtn-camn"></span></button></div>'
                  '<div class="cc-now"></div></div></div>'
                  '<div class="cc-tiers" id="mtn-tiers" role="group" aria-label="Elevation"></div>'
                  '<div class="cc-charts"></div><div class="cc-tip" hidden></div>'
                  '<div class="cc-cams" id="mtn-cams" hidden>'
                  '<figure class="cam-main"><img class="cam-img" alt=""><div class="cam-msg" hidden></div></figure>'
                  '<div class="cam-cap"></div><div class="cam-thumbs" role="group" aria-label="Other cameras"></div></div></div></div>')
    full_html += '<div class="detail-list">' + details + '</div></div>'
    full_html += '<script>' + AQ_JS.replace("__AQ__", json.dumps(aq, separators=(",", ":"))) + '</script>'
    full_html += '<script>' + TRAILS_JS.replace("__MT__", mt_json).replace("__DEF__", str(DEFAULT_MTN)) + '</script>'
    full_html += "<script>window.lazyMap('trails-overview',function(){\n" + map_js + "\n});</script>"
    full_html += '</body></html>'
    return full_html


# ---------------------------------------------------------------------------
# Page 3: Mt Hood Ski
# ---------------------------------------------------------------------------
def fetch_latest_youtube(channel_handle):
    """Get latest video from a YouTube channel handle (e.g. '@TheChristomer').
    Returns (video_id, title, published) or (None, None, None). No API key needed.
    Tries the channel's RSS feed first; YouTube has been 404ing those feeds, so it
    falls back to the newest video on the channel's /videos page + the oEmbed title."""
    try:
        resp = requests.get(f"https://www.youtube.com/{channel_handle}",
                            headers={"User-Agent": "Mozilla/5.0"}, timeout=10)
        m = re.search(r'"channelId":"(UC[^"]+)"', resp.text)
        if not m:
            m = re.search(r'channel_id=(UC[^&"]+)', resp.text)
        if m:
            feed = requests.get(f"https://www.youtube.com/feeds/videos.xml?channel_id={m.group(1)}", timeout=10)
            if feed.status_code == 200:
                ns = {"a": "http://www.w3.org/2005/Atom", "yt": "http://www.youtube.com/xml/schemas/2015"}
                entry = ET.fromstring(feed.text).find("a:entry", ns)
                if entry is not None:
                    vid = entry.find("yt:videoId", ns).text
                    title = entry.find("a:title", ns).text
                    pub = entry.find("a:published", ns).text[:10]  # YYYY-MM-DD
                    return vid, title, pub
    except Exception:
        pass
    try:
        page = requests.get(f"https://www.youtube.com/{channel_handle}/videos",
                            headers={"User-Agent": "Mozilla/5.0", "Accept-Language": "en-US"}, timeout=10).text
        m = re.search(r'"videoId":"([\w-]{11})"', page)
        if not m:
            return None, None, None
        vid = m.group(1)
        title = requests.get("https://www.youtube.com/oembed",
                             params={"url": f"https://www.youtube.com/watch?v={vid}", "format": "json"},
                             timeout=10).json().get("title")
        ago = re.search(r'"content":"(\d+ (?:second|minute|hour|day|week|month|year)s? ago)"', page)
        return vid, title, ago.group(1) if ago else ""
    except Exception:
        return None, None, None


def fetch_gorge_snow_forecast():
    """Temira's human Mt Hood snow forecast from thegorgeismygym.com.
    Returns (html_str, status, posted) - html_str is the forecast body, status 'current',
    'offseason' or 'stale', posted the post's date ("Thu Sep 24") or None. She posts daily; the snow section is only current when the
    post is from the last 2 days and the section isn't her end-of-season note (keyword checks
    alone passed week-old forecasts, since any weekday name or 'snow level' counted)."""
    try:
        r = requests.get("https://thegorgeismygym.com/forecast/",
                         headers={"User-Agent": "Mozilla/5.0"}, timeout=10)
        if r.status_code != 200:
            return None, None, None
        soup = BeautifulSoup(r.text, "html.parser")
        div = soup.find(id="snow-forecast")
        if not div:
            return None, None, None
        now_pt = datetime.now(ZoneInfo('America/Los_Angeles'))
        # the post's own date: a <time datetime>, else the first "September 24, 2026" on the page
        posted = None
        t = soup.find("time", attrs={"datetime": True})
        try:
            if t:
                posted = datetime.fromisoformat(t["datetime"].replace("Z", "+00:00")).astimezone(ZoneInfo('America/Los_Angeles'))
        except ValueError:
            posted = None
        if posted is None:
            m = re.search(r"(January|February|March|April|May|June|July|August|September|October|November|December)\s+(\d{1,2}),\s*(\d{4})", soup.get_text(" "))
            if m:
                posted = datetime.strptime(" ".join(m.groups()), "%B %d %Y").replace(tzinfo=ZoneInfo('America/Los_Angeles'))
        fresh = posted is not None and (now_pt.date() - posted.date()).days <= 2
        lines = [l for l in div.get_text(separator='\n', strip=True).split('\n')
                 if l.strip() and 'MT HOOD SNOW FORECAST' not in l.upper()]
        body = '\n'.join(lines)
        offseason = any(k in body.lower() for k in ('end-of-season', 'end of season', 'see you next', 'off-season', 'offseason'))
        status = 'offseason' if offseason else ('current' if fresh and len(body) >= 80 else 'stale')
        paras = [p.get_text(strip=True) for p in div.find_all(['p', 'li'])]
        paras = [x for x in paras if x and 'MT HOOD SNOW FORECAST' not in x.upper()] or [body[:500]]
        forecast_html = ''.join(f'<p>{html.escape(x)}</p>' for x in paras)
        return forecast_html, status, (posted.strftime("%a %b %d").replace(" 0", " ") if posted else None)
    except Exception as exc:
        print(f"  WARNING: Gorge Is My Gym forecast unavailable ({exc})")
        return None, None, None


# first = the page's default (10-day table, snow summary, 24-hour panel, selected map marker)
SKI_POINTS = [
    {"name":"Meadows Base","lat":45.33144,"lon":-121.66413,"color":"#6BBF68"},
    {"name":"Top of Blue","lat":45.34425,"lon":-121.67252,"color":"#4FB1BE"},
    {"name":"Top of Cascade","lat":45.34971,"lon":-121.68164,"color":"#FE5000"},
]

LIFT_LINES = [
    {"name":"Blue","color":"#4FB1BE","dashed":0,"coords":[[-121.66413,45.33144],[-121.67252,45.34425]]},
    {"name":"Cascade","color":"#FE5000","dashed":0,"coords":[[-121.66413,45.33144],[-121.68164,45.34971]]},
]


def meters_to_miles(v):
    return None if v is None else v * 0.000621371


def meters_to_inches(v):
    return 0 if v is None else v * 39.3701


def fmt_vis(mi):
    if mi is None:
        return "--"
    if mi >= 10:
        return "10+ mi"
    if mi >= 1:
        return f"{mi:.1f} mi"
    return f"{mi:.2f} mi"


def vis_color(mi):
    # Text color, not a fill: good visibility stays quiet, only poor visibility draws the eye
    if mi is None:
        return "#CCC"
    if mi < 1:
        return "#D11A24"
    if mi < 3:
        return "#C9760A"
    if mi < 6:
        return "#8C7440"
    return "#888"


def wind_chill_f(t, w):
    if t is None or w is None: return t
    if t > 50 or w < 3: return t
    return 35.74 + 0.6215*t - 35.75*(w**0.16) + 0.4275*t*(w**0.16)


def fmt_depth(inches):
    if inches is None or inches < 0.25:
        return '0"'
    if inches >= 12:
        return f'{inches:.0f}"'
    if inches >= 3:
        return f'{inches:.1f}"'
    return f'{inches:.2f}"'


def snow_summary_html(pi, lat, lon, ev, now, entries):
    """OpenSnow-style snow summary for one point: the previous 15 days (three 5-day groups), the
    last 24 hours, and the next 10 days. The past is estimated like the forecast: Open-Meteo's
    recent-past record at the point's own elevation, through the same wet-bulb snow model."""
    past, last24 = {}, 0.0
    try:
        d = requests.get("https://api.open-meteo.com/v1/forecast", params={
            "latitude": lat, "longitude": lon, "hourly": ["precipitation", "temperature_2m", "dew_point_2m"],
            "temperature_unit": "fahrenheit", "precipitation_unit": "inch", "timezone": "auto",
            "past_days": 15, "forecast_days": 1, **ev}, timeout=60).json()["hourly"]
        for t, pr, tf, dw in zip(d["time"], d["precipitation"], d["temperature_2m"], d["dew_point_2m"]):
            dt = datetime.strptime(t, "%Y-%m-%dT%H:%M")
            if dt > now or not pr:
                continue
            sn = new_snow_in(pr, tf, rh_from_dew(tf, dw) if tf is not None and dw is not None else None)[0]
            if dt.date() < now.date():
                past[dt.date()] = past.get(dt.date(), 0.0) + sn
            if dt > now - timedelta(hours=24):
                last24 += sn
    except Exception as exc:
        print(f"  WARNING: past snow unavailable ({exc})")
    today = now.date()
    days_past = [(today - timedelta(days=k), past.get(today - timedelta(days=k), 0.0)) for k in range(15, 0, -1)]
    fut = OrderedDict()
    for e in entries:
        dd = datetime.strptime(e["date_key"], "%Y-%m-%d").date()
        if dd > today:
            fut[dd] = fut.get(dd, 0.0) + e["snow_in"]
    days_fut = list(fut.items())[:10]

    def amt(v, big=False):
        if v < 0.05:
            return "0"
        return f"{v:.0f}" if v >= 10 or (big and v >= 1) else f"{v:.1f}"

    def group(label, ds, col):
        tot = sum(v for _, v in ds)
        return (f'<div class="ss-grp" style="grid-column:{col} / span {len(ds)}"><div class="ss-lbl">{label}</div>'
                f'<div class="ss-tot"><span>{amt(tot)}″</span></div></div>')

    s_max = max([1.0] + [v for _, v in days_past + days_fut])   # one bar scale for past and future

    def cells(ds, col0, past_side):
        out = ""
        for k, (dd, v) in enumerate(ds):
            wk = " ss-wk" if dd.weekday() >= 5 else ""
            hpx = 0 if v < 0.05 else max(3, round(52 * v / s_max))
            out += (f'<div class="ss-day{wk}{" ss-past" if past_side else ""}{" ss-zero" if v < 0.05 else ""}" style="grid-column:{col0 + k}" '
                    f'title="{dd:%a %b} {dd.day}: {amt(v)}\u2033{" (estimated)" if past_side else " forecast"}">'
                    f'<div class="ss-bar"><b>{amt(v)}</b><em style="height:{hpx}px"></em></div><i>{dd.strftime("%a")[0]}<br>{dd.day}</i></div>')
        return out

    nf = len(days_fut)
    grid = (group("Prev 11–15 days", days_past[0:5], 1) + group("Prev 6–10 days", days_past[5:10], 6)
            + group("Prev 1–5 days", days_past[10:15], 11)
            + f'<div class="ss-now" style="grid-column:16;grid-row:1 / span 3"><div class="ss-lbl">Last 24 hours</div>'
              f'<div class="ss-big">{amt(last24, True)}″</div><div class="ss-when">{now:%a} {now.day} '
              f'{now.strftime("%I:%M%p").lstrip("0").lower()[:-1]}<br><span>Estimated</span></div></div>'
            + group("Next 1–5 days", days_fut[0:5], 17)
            + (group("Next 6–10 days", days_fut[5:10], 22) if nf > 5 else "")
            + cells(days_past, 1, True) + cells(days_fut, 17, False))
    return (f'<div class="snowsum" data-p="{pi}"{" hidden" if pi else ""}><div class="ss-grid" '
            f'style="grid-template-columns:repeat(15,minmax(26px,1fr)) minmax(130px,1.6fr) repeat({max(nf, 1)},minmax(26px,1fr))">{grid}</div></div>')


def hood_layers(profile, lat=SKI_POINTS[0]["lat"], lon=SKI_POINTS[0]["lon"]):
    """The Meadows map's terrain layers, 3-hourly from now: temperature, gust, new snow since
    now and estimated snow depth at every PROFILE_ELEVS_M level. None without a profile."""
    if not profile or "frames" not in profile:
        return None
    F, now_ms = profile["frames"], datetime.now().timestamp() * 1000
    L = len(PROFILE_ELEVS_M)
    obs = snowpack.observed_depths(lat, lon)
    d0 = snowpack.initial_profile(PROFILE_ELEVS_M, [(z, d) for z, d, _ in obs])
    seq, prev_cum, cum_start = [], None, None
    for di in range(len(F["ts"])):
        before = [sum(F["day_snow"][k][li] for k in range(di)) for li in range(L)]   # snow in the days before
        for fi, ts in enumerate(F["ts"][di]):
            cum = [before[li] + F["snow"][di][fi][li] for li in range(L)]
            if ts < now_ms - 90 * 60000:
                prev_cum = cum
                continue
            if cum_start is None:
                cum_start = prev_cum or cum
                prev_cum = cum_start
            seq.append({"t": ts, "temp": F["temp"][di][fi], "gust": F["gust"][di][fi],
                        "snow": [round(max(0.0, c - s0), 1) for c, s0 in zip(cum, cum_start)],
                        "new": [max(0.0, c - p) for c, p in zip(cum, prev_cum)]})
            prev_cum = cum
    depth = snowpack.evolve(d0, seq)
    for fr, dp in zip(seq, depth):
        fr["depth"] = dp
        del fr["new"]
    print(f"  Meadows layers: {len(seq)} frames; SNOTEL depths "
          + (", ".join(f"{n} {d:.0f}\"" for _, d, n in obs) or "none nearby"))
    return {"elev": PROFILE_ELEVS_M, "frames": seq,
            "stations": [{"name": n, "elev_ft": round(z * 3.28084), "depth": d} for z, d, n in obs]}


def layer_depth(layers, elev_m, dt):
    """Estimated depth (in) at one elevation and local time, from the nearest earlier frame."""
    fr = layers["frames"]
    ts = dt.replace(tzinfo=ZoneInfo("America/Los_Angeles")).timestamp() * 1000
    k = max([i for i, f in enumerate(fr) if f["t"] <= ts + 90 * 60000] or [0])
    E, D = layers["elev"], fr[k]["depth"]
    z = min(max(elev_m or 0, E[0]), E[-1])
    for i in range(1, len(E)):
        if z <= E[i]:
            return D[i - 1] + (D[i] - D[i - 1]) * (z - E[i - 1]) / (E[i] - E[i - 1])
    return D[-1]


# Mt Hood page, top-right panel: the next 24 hours at Top of Blue / Top of Cascade (with
# visibility in place of cloud cover), and Meadows' cameras.
HOOD_JS = r"""
var HP=__HP__, HCAMS=__HCAMS__, sel=0, view='fc', cam=0;
var charts=WxCharts(document.getElementById('hood-cc'),{vis:true}), tiers=document.getElementById('hood-tiers');
var camsEl=document.getElementById('hood-cams'), camsView=WxCams(camsEl);
function draw(){
  tiers.innerHTML=HP.map(function(p,j){return'<button class="'+(j===sel?'active':'')+'" data-t="'+j+'">'+p.name+'<small>'+p.elev_ft.toLocaleString('en-US')+'\u2032</small></button>';}).join('');
  document.getElementById('hood-wp').textContent=HP[sel].name+' \u00b7 '+HP[sel].elev_ft.toLocaleString('en-US')+'\u2032';
  document.querySelectorAll('#page3 .ski-marker').forEach(function(el,j){el.classList.toggle('active',j===sel);});
  document.querySelectorAll('#page3 [data-p]').forEach(function(el){el.hidden=+el.dataset.p!==sel;});
  document.querySelectorAll('#page3 .hood-pick [data-t]').forEach(function(b){b.classList.toggle('active',+b.dataset.t===sel);});
  charts.set(HP[sel].h24);
}
function setView(v){
  var n=HCAMS.length;if(v==='cams'&&!n)v='fc';view=v;
  document.querySelectorAll('#hood-view [data-view]').forEach(function(b){b.classList.toggle('active',b.dataset.view===v);});
  document.getElementById('hood-view').hidden=!n;
  document.getElementById('hood-camn').textContent=n?' '+n:'';
  var fc=v==='fc';camsEl.hidden=fc;document.querySelector('#hood-cc .cc-charts').hidden=!fc;tiers.hidden=!fc;
  if(!fc)camsView.show(HCAMS,Math.min(cam,n-1),function(j){cam=j;});
}
window.hoodPick=function(i){if(i!==undefined)sel=i;draw();};
(function(){   // the video poster: Mt Hood with the weather at the base right now
  var b=document.getElementById('yt-badge'),h=HP[0]&&HP[0].h24[0];if(!b||!h||!window.wxArt)return;
  var w=window.wxArt(h),ic=b.querySelector('svg');
  if(w.back)ic.insertAdjacentHTML('beforebegin','<svg class="mtn-wx" viewBox="0 0 32 22" aria-hidden="true">'+w.back+'</svg>');
  if(w.front)ic.insertAdjacentHTML('afterend','<svg class="mtn-wx" viewBox="0 0 32 22" aria-hidden="true">'+w.front+'</svg>');
  document.getElementById('yt-wx').textContent=HP[0].name+' now \u00b7 '+w.label;
  var m=/(\d+)(AM|PM)/.exec(h.t),hr=m?(+m[1]%12+(m[2]==='PM'?12:0)):12;
  b.closest('.yt-card').classList.toggle('day',hr>=7&&hr<19);
})();
[tiers].concat([].slice.call(document.querySelectorAll('#page3 .hood-pick'))).forEach(function(g){
  g.addEventListener('click',function(ev){var b=ev.target.closest('[data-t]');if(b)window.hoodPick(+b.dataset.t);});});
document.getElementById('hood-view').addEventListener('click',function(ev){var b=ev.target.closest('[data-view]');if(b)setView(b.dataset.view);});
draw();setView('fc');
"""

# Terrain layers on a 3D Mapbox map: every pixel of Mapbox's elevation tiles coloured by the forecast
# at its own elevation (raster-color on the DEM), 3-hourly on a time slider; same colour ramps as the
# Map tab. TerrainLayers(map, HL, wrap, uid): HL = {elev, frames:[{t,temp,gust,snow,depth}], pts, tz,
# stations or depthNote}; wrap holds the .lyr-ctl / .lyr-leg / range input / .lyr-rtime controls.
# Used by the Mt Hood page and the live trail page.
TERRAIN_LAYERS_JS = r"""
window.TerrainLayers=function(map,HL,wrap,uid){
var st={l:'temp',k:0}, timer=null, tz=HL.tz||'America/Los_Angeles', src=uid+'-elev', lyr=uid+'-wx';
var RAMPS={
  temp:{S:[[-10,[75,44,127]],[5,[90,79,176]],[20,[79,127,201]],[29,[140,195,234]],[32,[250,252,255]],[35,[189,227,214]],[45,[124,196,122]],[60,[233,201,90]],[75,[242,154,59]],[90,[224,64,47]]],
        a:0.62,title:'Temperature',unit:'\u00b0F',lo:-10,hi:90,ticks:['-10\u00b0','32\u00b0','60\u00b0','90\u00b0F'],fmt:function(v){return Math.round(v)+'\u00b0F';}},
  gust:{S:[[0,[255,255,255,0]],[12,[255,255,255,0]],[20,[250,232,150,0.42]],[30,[250,190,80,0.58]],[40,[240,130,50,0.68]],[55,[215,55,45,0.76]],[70,[150,40,120,0.8]],[90,[75,20,85,0.85]]],
        a:1,title:'Wind gusts on exposed terrain',lo:0,hi:90,ticks:['0','30','60','90 mph'],fmt:function(v){return Math.round(v)+' mph';}},
  snow:{S:[[0,[255,255,255,0]],[0.1,[190,220,250,0.35]],[1,[150,195,245,0.6]],[3,[95,150,230,0.72]],[6,[60,100,210,0.8]],[12,[90,60,190,0.84]],[24,[150,50,170,0.88]]],
        a:1,title:'New snow',lo:0,hi:24,ticks:['0\u2033','6\u2033','12\u2033','24\u2033+'],fmt:function(v){return (v<1?v.toFixed(1):Math.round(v))+'\u2033';}},
  depth:{S:[[0,[255,255,255,0]],[1,[225,235,250,0.35]],[6,[180,205,240,0.55]],[12,[130,170,230,0.66]],[24,[85,125,215,0.74]],[48,[70,80,190,0.8]],[96,[110,60,170,0.84]],[150,[150,50,150,0.88]]],
        a:1,title:'Snow depth (estimated)',lo:0,hi:150,ticks:['0\u2033','36\u2033','72\u2033','150\u2033+'],fmt:function(v){return Math.round(v)+'\u2033';}}};
function rgba(c,a){return 'rgba('+Math.round(c[0])+','+Math.round(c[1])+','+Math.round(c[2])+','+(c.length>3?c[3]:a).toFixed(2)+')';}
function col(S,v,a){
  if(v<=S[0][0])return rgba(S[0][1],a);
  for(var i=1;i<S.length;i++){if(v<=S[i][0]){var f=(v-S[i-1][0])/(S[i][0]-S[i-1][0]),p=S[i-1][1],q=S[i][1];
    return rgba([0,1,2,3].map(function(k){var x=k<p.length?p[k]:a,y=k<q.length?q[k]:a;return x+(y-x)*f;}),a);}}
  return rgba(S[S.length-1][1],a);}
function vals(){return HL.frames[st.k][st.l];}
function ramp(){var R=RAMPS[st.l],v=vals(),e=['interpolate',['linear'],['raster-value']];
  for(var i=0;i<HL.elev.length;i++)e.push(HL.elev[i],col(R.S,v[i],R.a));return e;}
function at(z){var E=HL.elev,v=vals();for(var i=1;i<E.length;i++)if(E[i]>=z)return v[i-1]+(v[i]-v[i-1])*(z-E[i-1])/(E[i]-E[i-1]);return v[v.length-1];}
var WHEN=new Intl.DateTimeFormat('en-US',{timeZone:tz,weekday:'short',day:'numeric',hour:'numeric'});
function when(t){var p={};WHEN.formatToParts(new Date(t)).forEach(function(x){p[x.type]=x.value;});return p.weekday+' '+p.day+', '+p.hour+' '+p.dayPeriod;}
var ctl=wrap.querySelector('.lyr-ctl'),leg=wrap.querySelector('.lyr-leg'),sl=wrap.querySelector('input[type=range]'),lab=wrap.querySelector('.lyr-rtime'),play=ctl.querySelector('[data-tplay]');
sl.max=HL.frames.length-1;
function legend(){
  if(st.l==='none'){leg.hidden=true;return;}
  var R=RAMPS[st.l],stops=R.S.filter(function(s){return s[0]>=R.lo&&s[0]<=R.hi;});
  var bar='linear-gradient(90deg,'+stops.map(function(s){return col(R.S,s[0],Math.max(R.a,0.9)).replace(/,([\d.]+)\)$/,function(m,a){return ','+Math.max(0.25,+a).toFixed(2)+')';})+' '+((s[0]-R.lo)/(R.hi-R.lo)*100).toFixed(1)+'%';}).join(',')+')';
  var pts=HL.pts.map(function(p){return p.name+' '+R.fmt(at(p.elev_ft/3.28084));}).join(' \u00b7 ');
  var sub=st.l==='snow'?'since '+when(HL.frames[0].t)+' \u2192 '+when(HL.frames[st.k].t):when(HL.frames[st.k].t);
  var stations=HL.stations||[],snowy=stations.filter(function(s){return s.depth>0;}),obs=!stations.length?'no SNOTEL stations nearby'
    :!snowy.length?'all '+stations.length+' nearby SNOTEL stations bare today'
    :'SNOTEL today: '+snowy.sort(function(a,b){return b.elev_ft-a.elev_ft;}).slice(0,3).map(function(s){return s.name+' '+Math.round(s.depth)+'\u2033 at '+s.elev_ft.toLocaleString('en-US')+'\u2032';}).join(', ');
  var note=st.l==='depth'?(HL.depthNote||'Estimated from '+obs+', plus forecast snow, minus melt and settling. No wind loading or grooming; glaciers not included.')
    :st.l==='gust'?'Exposed ridges feel this; trees and gullies less.':'';
  leg.innerHTML='<b>'+R.title+'</b> \u00b7 '+sub+'<div class="lyr-bar" style="background:'+bar+'"></div><div class="lyr-ticks">'+R.ticks.map(function(t){return '<span>'+t+'</span>';}).join('')+'</div>'
    +'<div class="lyr-note">'+pts+'</div>'+(note?'<div class="lyr-src">'+note+'</div>':'');
  leg.hidden=false;}
function apply(){
  ctl.querySelectorAll('[data-l]').forEach(function(b){b.classList.toggle('active',b.dataset.l===st.l);});
  ctl.querySelector('.lyr-time').hidden=st.l==='none';
  sl.value=st.k;lab.textContent=when(HL.frames[st.k].t);
  legend();
  if(!map.getLayer(lyr))return;
  map.setLayoutProperty(lyr,'visibility',st.l==='none'?'none':'visible');
  if(st.l!=='none')map.setPaintProperty(lyr,'raster-color',ramp());}
function stop(){if(timer){clearInterval(timer);timer=null;}play.textContent='\u25B6 Play';play.classList.remove('active');}
ctl.addEventListener('click',function(ev){var b=ev.target.closest('button');if(!b)return;
  if(b.dataset.tplay!==undefined){if(timer){stop();return;}play.textContent='\u275A\u275A Pause';play.classList.add('active');
    timer=setInterval(function(){st.k=(st.k+1)%HL.frames.length;apply();},900);return;}
  if(b.dataset.l){st.l=b.dataset.l;if(st.l==='none')stop();apply();}});
sl.addEventListener('input',function(){stop();st.k=+this.value;apply();});
// the DEM tiles as a plain raster, decoded to metres and coloured by elevation
map.addSource(src,{type:'raster',url:'mapbox://mapbox.mapbox-terrain-dem-v1',tileSize:512});
var sym;map.getStyle().layers.some(function(l){if(l.type==='symbol'){sym=l.id;return true;}});
map.addLayer({id:lyr,type:'raster',source:src,layout:{visibility:'visible'},
  paint:{'raster-color':ramp(),'raster-color-mix':[1671168,6528,25.5,-10000],'raster-color-range':[0,4500],'raster-fade-duration':0}},sym);
apply();
return {stop:stop};
};
"""

HOOD_LAYER_JS = "window.TerrainLayers(map,__HL__,document.getElementById('hoodmap').parentNode,'hood');"



def build_ski_page():
    url = "https://api.open-meteo.com/v1/forecast"
    point_tables = []
    summary_cards = []
    map_points = []
    blue_today_entries = []
    blue_elev_ft = 0
    local_tz = "America/Los_Angeles"
    pred = ensemble_predictability(SKI_POINTS[0]["lat"], SKI_POINTS[0]["lon"], 10)  # all three points share one 25 km cell
    nws_pts = [nws.point(p["lat"], p["lon"]) for p in SKI_POINTS]   # the base forecast, per point's grid box
    qpf = [merge_qpf(n, b) for n, b in zip(nws_pts, blended_qpf(SKI_POINTS, 10))]
    # the same forecast-by-elevation as the Trails tab and the Map: moves temperature and wind to
    # each point's real elevation, and colours the map's terrain layers
    profile = elevation_profile(SKI_POINTS, qpf, days=7, nws_pts=nws_pts)
    layers = hood_layers(profile)
    h24_all = []
    snow_sums = []

    for pi, pt in enumerate(SKI_POINTS):
        lat, lon = pt["lat"], pt["lon"]
        pe = requests.get(
            "https://api.open-meteo.com/v1/elevation",
            params={"latitude": lat, "longitude": lon}
        ).json().get("elevation", [None])[0]
        ev = {"elevation": pe} if pe is not None else {}

        base = requests.get(
            url,
            params={
                "latitude": lat, "longitude": lon,
                "hourly": [
                    "temperature_2m", "dew_point_2m", "cloud_cover", "cloud_cover_low",
                    "cloud_cover_mid", "cloud_cover_high", "cloud_base", "wind_speed_10m",
                    "wind_gusts_10m", "precipitation", "snowfall"
                ],
                "temperature_unit": "fahrenheit", "precipitation_unit": "inch",
                "wind_speed_unit": "mph", "timezone": "auto", "forecast_days": 11,
                "models": "ecmwf_ifs", **ev
            }
        ).json()
        h = base["hourly"]
        local_tz = base.get("timezone", local_tz)
        now = datetime.now(ZoneInfo(local_tz)).replace(tzinfo=None)
        n = len(h["time"])
        elev_ft = base.get("elevation", 0) * 3.28084

        extra = requests.get(
            url,
            params={
                "latitude": lat, "longitude": lon,
                "hourly": ["visibility", "snow_depth", "precipitation_probability"],
                "timezone": "auto", "forecast_days": 11, **ev
            }
        ).json().get("hourly", {})
        h["visibility"] = extra.get("visibility", [None] * n)
        h["snow_depth"] = extra.get("snow_depth", [0] * n)
        h["precipitation_probability"] = extra.get("precipitation_probability", [0] * n)

        tr = requests.get(
            url,
            params={
                "latitude": lat, "longitude": lon,
                "hourly": ["temperature_2m", "dew_point_2m"],
                "temperature_unit": "fahrenheit", "timezone": "auto", "forecast_days": 11, **ev
            }
        ).json().get("hourly", {})
        if "temperature_2m" in tr and len(tr["temperature_2m"]) == n:
            h["temperature_2m"] = tr["temperature_2m"]
            h["dew_point_2m"] = tr["dew_point_2m"]

        hr = requests.get(
            url,
            params={
                "latitude": lat, "longitude": lon,
                "hourly": [
                    "temperature_2m", "dew_point_2m", "cloud_cover", "cloud_cover_low",
                    "cloud_cover_mid", "cloud_cover_high", "cloud_base", "visibility",
                    "wind_speed_10m", "wind_gusts_10m", "precipitation", "snowfall",
                    "snow_depth", "precipitation_probability"
                ],
                "temperature_unit": "fahrenheit", "precipitation_unit": "inch",
                "wind_speed_unit": "mph", "timezone": "auto", "forecast_days": 2,
                "models": "gfs_hrrr", **ev
            }
        ).json().get("hourly", {})
        hrrr_idx = {t: i for i, t in enumerate(hr.get("time", []))}
        today_key = now.strftime("%Y-%m-%d")
        for i, t in enumerate(h["time"]):
            if t.startswith(today_key) and t in hrrr_idx:
                j = hrrr_idx[t]
                for k in [
                    "temperature_2m", "dew_point_2m", "cloud_cover", "cloud_cover_low",
                    "cloud_cover_mid", "cloud_cover_high", "cloud_base", "visibility",
                    "wind_speed_10m", "wind_gusts_10m", "precipitation", "snowfall",
                    "snow_depth", "precipitation_probability"
                ]:
                    if k in hr and j < len(hr[k]) and hr[k][j] is not None:
                        h[k][i] = hr[k][j]

        nbm_overlay(h, lat, lon, ev, 10)
        for i, t in enumerate(h["time"]):  # our blend: the fallback where the NWS has no hours
            if qpf[pi].get(t) is not None:
                h["precipitation"][i] = qpf[pi][t]
        nw = nws_pts[pi]   # the NWS forecast is the base, moved to this point's elevation
        if nw:
            tsh = (profile or {}).get("_wp_tshift", [{}] * len(SKI_POINTS))[pi]
            std = region.STD_LAPSE_F_PER_M * ((nw["elev_m"] or 0) - (pe or 0))
            nws_overlay(h, nw, lambda t, tsh=tsh, std=std: tsh.get(t, std))
        if profile and profile.get("_wp_gust"):
            # exposed-ridge wind, as on the Trails tab: the stronger of the surface model and the free air
            pw, pg = profile["_wp_wind"][pi], profile["_wp_gust"][pi]
            for i, t in enumerate(h["time"]):
                if pg.get(t) is not None:
                    h["wind_gusts_10m"][i] = max(h["wind_gusts_10m"][i] or 0, pg[t])
                if pw.get(t) is not None:
                    h["wind_speed_10m"][i] = max(h["wind_speed_10m"][i] or 0, pw[t])

        entries = []
        next24 = []   # the next 24 hours, hour by hour, whatever the time of day
        prev_date = None
        day_num = 0
        acc_p = acc_s = 0
        for i, t in enumerate(h["time"]):
            dt = datetime.strptime(t, "%Y-%m-%dT%H:%M")
            dk = dt.strftime("%Y-%m-%d")
            if dk != prev_date:
                day_num += 1
                prev_date = dk
            # rain/snow accumulate over every hour a column covers (3 h after today)
            p_h = h.get("precipitation", [0] * n)[i] or 0
            t_h, d_h = h["temperature_2m"][i], h["dew_point_2m"][i]
            s_h, f_h = new_snow_in(p_h, t_h, rh_from_dew(t_h, d_h) if t_h is not None and d_h is not None else None)
            if day_num == 1:
                if dt < now.replace(minute=0, second=0, microsecond=0):
                    acc_p = acc_s = 0
                    continue
            acc_p += p_h
            acc_s += s_h
            if len(next24) < 24:   # the next 24 hours, hour by hour (the chart panel)
                ty = "" if p_h < 0.005 else ("snow" if f_h >= 0.8 else "mix" if f_h > 0.2 else "rain")
                vm = meters_to_miles(h.get("visibility", [None] * n)[i])
                next24.append({"t": dt.strftime("%a %I%p").replace(" 0", " "), "temp": round(t_h or 0),
                               "wind": round(h["wind_speed_10m"][i] or 0), "gust": round(h["wind_gusts_10m"][i] or 0),
                               "sky": round(h["cloud_cover"][i] or 0), "p": round(p_h, 3), "s": round(s_h, 2), "ty": ty,
                               "vis": None if vm is None else round(vm, 2)})
            if day_num != 1 and (dt.hour - 2) % 3 != 0:
                continue
            vis_m = h.get("visibility", [None] * n)[i]
            vis_mi = meters_to_miles(vis_m)
            depth_in = layer_depth(layers, pe, dt) if layers else meters_to_inches((h.get("snow_depth", [0] * n)[i] or 0))
            precip_in, snow_in = acc_p, acc_s
            acc_p = acc_s = 0
            entries.append({
                "time": dt.strftime("%I%p").lstrip("0").lower(),
                "date_lbl": day_label(dt),
                "date_key": dk,
                "day_num": day_num,
                "temp": h["temperature_2m"][i],
                "clouds": h["cloud_cover"][i],
                "wind": h["wind_speed_10m"][i],
                "gust": h["wind_gusts_10m"][i],
                "precip": h["precipitation_probability"][i],
                "precip_in": precip_in,
                "snow_in": snow_in,
                "snow_depth_in": depth_in,
                "vis_mi": vis_mi,
                "cb": cloud_base_display(
                    h["cloud_base"][i], h["cloud_cover_low"][i], h["cloud_cover_mid"][i],
                    h["cloud_cover_high"][i], h["cloud_cover"][i], h["temperature_2m"][i],
                    h["dew_point_2m"][i], vis_m, elev_ft
                )
            })

        today = [e for e in entries if entries and e["day_num"] == entries[0]["day_num"]]   # the first day with hours ahead
        h24_all.append(next24)
        if pt["name"] == "Top of Blue":
            blue_today_entries = next24
            blue_elev_ft = elev_ft
        cur = today[0] if today else entries[0]
        min_vis = min((e["vis_mi"] for e in today if e["vis_mi"] is not None), default=None)
        max_depth = max((e["snow_depth_in"] for e in today), default=0)
        new_snow = sum(e["snow_in"] for e in today)
        summary = {
            "name": pt["name"],
            "lat": lat, "lon": lon, "color": pt["color"],
            "hi": max(e["temp"] for e in today) if today else cur["temp"],
            "lo": min(e["temp"] for e in today) if today else cur["temp"],
            "wind": max(e["wind"] for e in today) if today else cur["wind"],
            "gust": max(e["gust"] for e in today) if today else cur["gust"],
            "precip": max(e["precip"] for e in today) if today else cur["precip"],
            "vis_mi": min_vis,
            "snow_depth_in": max_depth,
            "new_snow_in": new_snow,
            "icon": condition_icon(today or [cur]),
            "elev_ft": elev_ft,
            "base": cur["cb"],
            "feels_like": min((wind_chill_f(e["temp"], e["wind"]) for e in today), default=cur["temp"]),
        }
        map_points.append(summary)
        print(f"{summary['name']}: {summary['hi']:.0f}/{summary['lo']:.0f}F wind {summary['wind']:.0f}mph vis {fmt_vis(summary['vis_mi'])} depth {fmt_depth(summary['snow_depth_in'])}")

        summary_cards.append(
            '<div class="ski-card">'
            + f'<div class="ski-card-title"><span class="ski-icon">{summary["icon"]}</span>{html.escape(summary["name"])}</div>'
            + f'<div class="ski-card-sub">{summary["elev_ft"]:.0f}\u2032 MSL &nbsp; {lat:.4f}, {lon:.4f}</div>'
            + '<div class="ski-stat-grid">'
            + f'<div class="ski-stat"><div class="lbl">Temp</div><div class="val"><span class="temp-pill" style="background:{temp_bg(summary["hi"])}">{summary["hi"]:.0f}\u00b0</span> <span class="temp-pill low" style="background:{temp_bg(summary["lo"])}">{summary["lo"]:.0f}\u00b0</span></div></div>'
            + f'<div class="ski-stat"><div class="lbl">Wind</div><div class="val"><span style="color:{wind_color(summary["wind"])}">{summary["wind"]:.0f}</span><span class="gust">({summary["gust"]:.0f})</span> mph</div></div>'
            + f'<div class="ski-stat"><div class="lbl">Visibility</div><div class="val"><span class="vis-pill" style="color:{vis_color(summary["vis_mi"])}">{fmt_vis(summary["vis_mi"])}</span></div></div>'
            + f'<div class="ski-stat"><div class="lbl">Snow depth</div><div class="val">{fmt_depth(summary["snow_depth_in"])}</div></div>'
            + f'<div class="ski-stat"><div class="lbl">New snow</div><div class="val">{fmt_depth(summary["new_snow_in"])}</div></div>'
            + f'<div class="ski-stat"><div class="lbl">Chance</div><div class="val">{summary["precip"]}%</div></div>'
            + '</div></div>'
        )

        days = OrderedDict()
        for e in entries:
            days.setdefault(e["date_lbl"], {"entries": [], "day_num": e["day_num"]})["entries"].append(e)

        # the snow summary: estimated snow for the past 15 days and last 24 h, then the forecast's
        snow_sums.append(snow_summary_html(pi, lat, lon, ev, now, entries))

        # shared scales across the 10 days, so bars compare day to day
        ten = [d["entries"] for d in list(days.values())[:10]]
        t_lo = min(e["temp"] for ents in ten for e in ents) - 2
        t_hi = max(e["temp"] for ents in ten for e in ents) + 2

        rows = {"time": "", "snow": "", "temp": "", "wind": "", "vis": "", "chance": "", "base": ""}
        for di, (dk, dinfo) in enumerate(days.items()):
            if di >= 10:   # the 11th day only feeds the snow summary
                break
            ents = dinfo["entries"]
            nc = len(ents)
            hi2, lo2 = max(e["temp"] for e in ents), min(e["temp"] for e in ents)
            mw, mg = max(e["wind"] for e in ents), max(e["gust"] for e in ents)
            mv = min((e["vis_mi"] for e in ents if e["vis_mi"] is not None), default=None)
            sn = sum(e["snow_in"] for e in ents)
            mp = max(e["precip"] for e in ents)
            bases = [e["cb"] for e in ents if e["cb"] not in ("Clear", "Few", "--")]
            cb = bases[0] if len(set(bases)) <= 1 and bases else ("Clear" if not bases else f'{bases[0]}–{bases[-1]}')
            ci = condition_icon(ents)
            dd = datetime.strptime(ents[0]["date_key"], "%Y-%m-%d")
            tt = ' <span class="tt">TODAY</span>' if di == 0 else ''
            wk = ' wkd' if dd.weekday() >= 5 else ''
            d_hide = 'display:none;'
            s_cls, d_cls = f'd{di} dsum{wk}', f'd{di} ddet'
            oc = f'onclick="toggleSkiDay({di})" title="Click for 3-hourly detail"'
            td = f'<td class="{s_cls}" colspan="{nc}" {oc}>'
            rows["time"] += (td + f'<div class="dh"><b>{dd:%a}</b> <span>{dd.day}</span>{tt}</div>'
                             f'<div class="ds-ci">{ci}</div>{pred_badge(pred.get(ents[0]["date_key"]))}</td>')
            # new snow: the day's total (the snow summary below charts it)
            rows["snow"] += td + (f'<span class="snow-day">{sn:.1f}\u2033</span>' if 0.05 <= sn < 10
                                  else f'<span class="snow-day">{sn:.0f}\u2033</span>' if sn >= 10
                                  else '<span class="snow-none">0</span>') + '</td>'
            # temperature: the day's high-low range on the 10-day scale
            top = (t_hi - hi2) / (t_hi - t_lo) * 100
            bot = (lo2 - t_lo) / (t_hi - t_lo) * 100
            rows["temp"] += (td + f'<div class="trange"><span class="th">{hi2:.0f}°</span><div class="tr-track">'
                             f'<i style="top:{top:.0f}%;bottom:{bot:.0f}%;background:linear-gradient({temp_bg(hi2)},{temp_bg(lo2)})"></i></div>'
                             f'<span class="tl2">{lo2:.0f}°</span></div></td>')
            rows["wind"] += td + f'<span class="wv" style="color:{wind_color(mw)}">{mw:.0f}</span><span class="gust">g{mg:.0f}</span></td>'
            rows["vis"] += td + f'<span class="vis-pill" style="color:{vis_color(mv)}">{fmt_vis(mv)}</span></td>'
            rows["chance"] += td + f'{precip_icon(mp)} {mp}%</td>'
            rows["base"] += td + f'{html.escape(cb)}</td>'
            for ei, e in enumerate(ents):
                bdr = 'border-left:1px solid #DDE0E6;' if ei == 0 else ''
                dl = f'<div class="dh"><b>{dd:%a}</b> <span>{dd.day}</span>{tt}</div>' if ei == 0 else ''
                rows["time"] += f'<td class="{d_cls}" style="{d_hide}{bdr}">{dl}<div class="tl">{e["time"]}</div></td>'
            for key, formatter in [
                ("snow", lambda e: f'<span class="snow-txt">{fmt_depth(e["snow_in"])}</span>' if e["snow_in"] >= 0.05 else '<span style="color:#DDD;">·</span>'),
                ("temp", lambda e: f'<div class="temp-pill" style="background:{temp_bg(e["temp"])}">{e["temp"]:.0f}°</div>'),
                ("wind", lambda e: f'<span class="wv" style="color:{wind_color(e["wind"])}">{e["wind"]:.0f}</span><span class="gust">g{e["gust"]:.0f}</span>'),
                ("vis", lambda e: f'<span class="vis-pill" style="color:{vis_color(e["vis_mi"])}">{fmt_vis(e["vis_mi"])}</span>'),
                ("chance", lambda e: f'{precip_icon(e["precip"])} {e["precip"]}%'),
                ("base", lambda e: html.escape(e["cb"]))
            ]:
                for ei, e in enumerate(ents):
                    bdr = 'border-left:1px solid #DDE0E6;' if ei == 0 else ''
                    rows[key] += f'<td class="{d_cls}" style="{d_hide}{bdr}">{formatter(e)}</td>'

        actual_rows = ""
        for key, label in [
            ("time", ""),
            ("snow", ri("flake") + 'New snow'),
            ("temp", ri("temp") + 'High / low'),
            ("wind", ri("wind") + 'Wind, gust'),
            ("vis", ri("eye") + 'Visibility'),
            ("chance", ri("chance") + 'Chance'),
            ("base", ri("base") + 'Cloud base')
        ]:
            actual_rows += f'<tr class="r-{key}"><th>{label}</th>{rows[key]}</tr>\n'

        point_tables.append(
            f'<div class="ski-section">'
            f'<div class="wp-header"><span class="wp-dot" style="background:{pt["color"]}"></span><span class="wp-name">{html.escape(pt["name"])}</span><span class="wp-meta">{elev_ft:,.0f}\u2032 MSL \u00b7 {lat:.4f}, {lon:.4f}</span></div>'
            f'<div class="scroll-wrap"><table class="ski-tbl">{actual_rows}</table></div>'
            f'</div>'
        )

    markers_json = json.dumps(map_points)
    lifts_json = json.dumps({
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "properties": {"name": l["name"], "color": l["color"], "dashed": l["dashed"]},
                "geometry": {"type": "LineString", "coordinates": l["coords"]}
            }
            for l in LIFT_LINES
        ]
    })

    ski_css = """
    * { box-sizing:border-box; }
    body { font-family:'Helvetica Neue',Arial,sans-serif; margin:0; padding:14px; background:#FAFAFA; color:#111; }
    .ski-card { background:#fff; border-radius:10px; padding:12px 14px; box-shadow:0 1px 4px rgba(0,0,0,0.08); }
    .ski-card-title { font-size:15px; font-weight:700; display:flex; align-items:center; gap:6px; }
    .ski-card-sub { margin-top:4px; color:#888; font-size:11px; }
    .ski-card-sub a { color:#888; }
    .ski-card-placeholder { border:2px dashed #DDD; box-shadow:none; background:transparent; display:flex; flex-direction:column; align-items:center; justify-content:center; min-height:150px; }
    .ski-card-placeholder .ski-card-title { color:#999; font-size:14px; }
    .ski-card-placeholder .ski-card-sub { color:#BBB; }
    .gorge-card, .report-card { overflow-y:auto; max-height:300px; }
    .gorge-body p, .report-body { margin:6px 0 0; font-size:12px; line-height:1.55; color:#333; }
    .hood-word { margin:28px 0; }
    .hood-row1 { display:grid; grid-template-columns:minmax(0,3fr) minmax(0,2fr); gap:14px; align-items:start; margin-bottom:24px; }
    .hood-row1 .gorge-card { max-height:560px; }
    .hood-row1 .ski-card-placeholder { min-height:220px; height:100%; }
    .hood-fc-head { display:flex; align-items:center; justify-content:space-between; gap:10px; flex-wrap:wrap; margin-bottom:6px; }
    .hood-fc-head .sec-h { margin:0; }
    .hood-pick { margin-top:0 !important; }
    .hood-fc .ski-section { margin-bottom:0; }
    .hood-fc .wp-header { margin:0 0 4px 2px; }
    .hood-sum { margin-bottom:28px; }
    .ss-wrap { overflow-x:auto; background:#fff; border-radius:10px; box-shadow:0 1px 4px rgba(0,0,0,0.08); padding:14px 16px 10px; }
    .ss-grid { display:grid; grid-template-rows:auto auto auto; column-gap:4px; min-width:880px; }
    .ss-grp { grid-row:1 / span 2; text-align:center; padding:0 2px 8px; }
    .ss-lbl { font-size:11px; font-weight:600; color:#5A5F6B; letter-spacing:.02em; white-space:nowrap; }
    .ss-tot { position:relative; margin-top:6px; }
    .ss-tot::before { content:""; position:absolute; left:0; right:0; top:50%; border-top:1.5px solid #C9CDD5; }
    .ss-tot span { position:relative; padding:0 8px; background:#fff; font-size:18px; font-weight:800; color:#111; font-variant-numeric:tabular-nums; }
    .ss-day { grid-row:3; text-align:center; font-variant-numeric:tabular-nums; padding-top:2px; }
    .ss-bar { display:flex; flex-direction:column; align-items:center; justify-content:flex-end; height:74px; border-bottom:1.5px solid #DDE0E6; }
    .ss-bar b { font-size:12px; font-weight:800; color:#111; margin-bottom:3px; }
    .ss-bar em { display:block; width:62%; max-width:18px; border-radius:3px 3px 0 0; background:#5A4FCF; }
    .ss-past .ss-bar em { background:#A9A3E8; }
    .ss-zero .ss-bar b { color:#C9CDD5; font-weight:500; }
    .ss-day i { display:block; font-style:normal; font-size:10.5px; line-height:1.3; color:#9A9FAB; margin-top:4px; }
    .ss-day.ss-wk i { color:#111; font-weight:700; }
    .ss-now { display:flex; flex-direction:column; align-items:center; justify-content:center; gap:4px; margin:0 6px; padding:10px 8px; border-radius:10px; background:#F1F2F5; text-align:center; }
    .ss-big { font-size:40px; font-weight:800; line-height:1; color:#111; font-variant-numeric:tabular-nums; }
    .ss-when { font-size:11px; color:#5A5F6B; line-height:1.4; }
    .ss-when span { color:#8A8F9C; }
    .ss-foot { margin-top:6px; font-size:11px; color:#9A9FAB; }
    .ss-key { display:inline-block; width:9px; height:9px; border-radius:2px; background:#5A4FCF; margin-right:5px; vertical-align:-1px; }
    .ss-key.ss-key-p { background:#A9A3E8; }
    .sec-h { margin:0 0 10px; font-size:15px; font-weight:700; color:#111; }
    .temp-pill { border-radius:5px; color:#fff; font-size:12px; font-weight:700; padding:2px 5px; display:inline-block; }
    .temp-pill.low { opacity:.7; }
    .gust { font-size:10.5px; color:#8A8F9C; margin-left:3px; }
    .vis-pill { font-size:11px; font-weight:500; display:inline-block; }
    #hoodmap { width:100%; height:600px; border-radius:8px; }
    /* The Christomer: the thumbnail as a poster, Mt Hood + current weather over it */
    .yt-wrap { flex:1; min-width:0; background:#fff; border-radius:10px; box-shadow:0 1px 4px rgba(0,0,0,0.08); padding:12px; }
    .yt-h { font-size:15px; font-weight:700; color:#111; margin-bottom:8px; }
    .yt-card { position:relative; display:flex; align-items:flex-end; min-height:220px; padding:20px 18px 18px; border-radius:8px; overflow:hidden; color:#fff; text-decoration:none;
      background:radial-gradient(120% 90% at 20% 0%, #3E5578 0%, #243249 45%, #141B28 100%); }
    .yt-card.day { background:radial-gradient(120% 90% at 20% 0%, #8DB8DD 0%, #5A86B2 45%, #2E4766 100%); }
    .yt-card:focus-visible { outline:2px solid #FE5000; outline-offset:2px; }
    /* a faint far ridge along the bottom, for depth */
    .yt-ridge { position:absolute; left:0; right:0; bottom:0; height:38%; background:rgba(8,12,20,.35);
      clip-path:polygon(0 60%,12% 38%,22% 52%,35% 22%,46% 45%,58% 30%,70% 50%,83% 26%,100% 48%,100% 100%,0 100%); }
    .yt-play { position:absolute; top:14px; right:14px; width:42px; height:42px; border-radius:50%; background:rgba(255,255,255,.14); box-shadow:inset 0 0 0 1px rgba(255,255,255,.35); transition:background .15s; }
    .yt-play::after { content:""; position:absolute; left:16px; top:12px; border-style:solid; border-width:9px 0 9px 15px; border-color:transparent transparent transparent #fff; }
    .yt-card:hover .yt-play { background:#FE5000; box-shadow:none; }
    .yt-foot { position:relative; display:flex; align-items:flex-end; flex-wrap:wrap; gap:12px 18px; width:100%; }
    .yt-badge { position:relative; flex:none; width:130px; height:89px; margin-top:26px; filter:drop-shadow(0 3px 6px rgba(0,0,0,.4)); }
    .yt-badge svg { position:absolute; left:0; top:0; width:100%; height:100%; overflow:visible; }
    .yt-text { flex:1 1 170px; display:flex; flex-direction:column; gap:3px; min-width:0; padding-bottom:2px; }
    .yt-wx { font-size:11px; font-weight:700; letter-spacing:.06em; text-transform:uppercase; color:#FFC9AE; }
    .yt-t { font-size:16px; font-weight:700; line-height:1.3; overflow:hidden; display:-webkit-box; -webkit-line-clamp:2; -webkit-box-orient:vertical; }
    .yt-d { font-size:12px; color:#DDE3EC; }
    .yt-d b { color:#fff; font-weight:700; }
    .ski-section { margin-bottom:18px; }
    .scroll-wrap { overflow-x:auto; background:#fff; border-radius:10px; box-shadow:0 1px 4px rgba(0,0,0,0.08); padding:8px; margin-top:4px; }
    table { border-collapse:separate; border-spacing:4px 0; white-space:nowrap; font-size:13px; }
    th { position:sticky; left:0; z-index:2; background:#fff; text-align:left; padding:3px 10px 3px 0; font-size:12px; color:#555; font-weight:600; min-width:96px; }
    th .ri { font-size:14px; margin-right:3px; }
    td { text-align:center; padding:3px 6px; min-width:58px; }
    .dsum { cursor:pointer; min-width:84px; border-left:1px solid #DDE0E6; }
    .dsum:hover { background:rgba(254,80,0,0.04); }
    .ddet { cursor:pointer; }
    .dl { font-size:11px; font-weight:700; color:#111; letter-spacing:.5px; text-transform:uppercase; }
    .ds-ci { font-size:18px; margin-top:2px; }
    .tt { background:#FE5000; color:#fff; font-size:9px; padding:1px 5px; border-radius:3px; margin-left:3px; vertical-align:middle; }
    /* 10-day table: day headers, snow bars, temperature range bars, row rules, weekend tint */
    .ski-tbl { border-spacing:0; }
    .ski-tbl td.dsum { padding:7px 10px; min-width:78px; vertical-align:middle; }
    .ski-tbl tr + tr td.dsum, .ski-tbl tr + tr th { border-top:1px solid #F0F1F4; }
    .ski-tbl th { padding:7px 12px 7px 0; vertical-align:middle; }
    .ski-tbl td.wkd { background:#F7F8FA; }
    .dh { white-space:nowrap; }
    .dh b { font-size:12.5px; font-weight:800; letter-spacing:.05em; text-transform:uppercase; color:#111; }
    .dh span { font-size:12.5px; font-weight:600; color:#8A8F9C; }
    .snow-day { font-size:14px; font-weight:800; color:#5A4FCF; font-variant-numeric:tabular-nums; }
    .snow-none { font-size:13px; color:#C9CDD5; }
    .dh .tt { color:#fff; font-size:9px; font-weight:700; letter-spacing:.03em; }
    .trange { display:flex; flex-direction:column; align-items:center; gap:3px; }
    .tr-track { position:relative; width:8px; height:54px; border-radius:4px; background:#EEF0F3; }
    .tr-track i { position:absolute; left:0; right:0; border-radius:4px; }
    .trange .th { font-size:13px; font-weight:700; color:#111; font-variant-numeric:tabular-nums; }
    .trange .tl2 { font-size:12px; color:#8A8F9C; font-variant-numeric:tabular-nums; }
    .wv { font-weight:700; }
    .snow-txt { color:#5A4FCF; font-weight:700; }
    .tl { font-size:11px; color:#999; font-weight:600; }
    .ski-marker { background:#fff; border-radius:6px; padding:5px 10px; font-size:13px; box-shadow:0 2px 6px rgba(0,0,0,0.25); white-space:nowrap; border-left:3px solid #6BBF68; cursor:pointer; }
    .ski-marker.active { box-shadow:0 0 0 2px #FE5000, 0 2px 6px rgba(0,0,0,0.25); }
    .ski-marker .sm-name { font-weight:700; color:#111; font-size:14px; }
    .ski-marker .sm-row { display:flex; gap:6px; align-items:center; margin-top:1px; }
    .ski-marker .sm-fl { font-weight:700; font-size:16px; color:#fff; border-radius:3px; padding:1px 4px; }
    .ski-marker .sm-wind { color:#368994; font-size:12px; }
    .ski-marker .sm-vis { font-size:12px; }
    @media (max-width:1000px) { .hood-row1 { grid-template-columns:minmax(0,1fr); } #hoodmap { height:460px; } }
    """

    # the map: the 3D Meadows view, clickable points, and the terrain layers
    map_js = (
        "mapboxgl.accessToken='__TOKEN__';\n"
        "var markers=__MARKERS__;\n"
        "var lifts=__LIFTS__;\n"
        # framed on all three points; the top padding keeps Top of Cascade clear of the layer controls
        "var map=new mapboxgl.Map({container:'hoodmap',style:'mapbox://styles/mapbox/outdoors-v12',center:[-121.6730,45.3355],zoom:13.45,pitch:76,bearing:0,attributionControl:false});\n"
        "map.setPadding({top:110,bottom:0,left:0,right:0});\n"
        "map.on('load',function(){\n"
        "  map.addSource('mapbox-dem',{type:'raster-dem',url:'mapbox://mapbox.mapbox-terrain-dem-v1',tileSize:512});\n"
        "  map.setTerrain({source:'mapbox-dem',exaggeration:1.6});\n"
        "  map.getStyle().layers.forEach(function(layer){if(layer.id.match(/road|poi|place-|settlement|town|village|city|state-label|country-label|transit|airport/)){map.setLayoutProperty(layer.id,'visibility','none');}});\n"
        "  if(window.hoodLayers)window.hoodLayers(map);\n"
        "  map.addSource('lifts',{type:'geojson',data:lifts});\n"
        "  map.addLayer({id:'lift-lines',type:'line',source:'lifts',filter:['==',['get','dashed'],0],paint:{'line-color':['get','color'],'line-width':4,'line-opacity':0.9}});\n"
        "  map.addLayer({id:'lift-labels',type:'symbol',source:'lifts',layout:{'symbol-placement':'line','text-field':['get','name'],'text-size':11,'text-font':['DIN Pro Medium','Arial Unicode MS Regular'],'text-keep-upright':true},paint:{'text-color':'#111','text-halo-color':'#fff','text-halo-width':1.4}});\n"
        "  var tc=function(t){if(t>=70)return'#FAA21B';if(t>=50)return'#6BBF68';if(t>=35)return'#4FB1BE';return'#368994';};\n"
        "  markers.forEach(function(m,i){\n"
        "    var vis=m.vis_mi===null?'--':(m.vis_mi>=10?'10+':m.vis_mi.toFixed(1)+'mi');\n"
        "    var fl=Math.round(m.hi);\n"
        "    var el=document.createElement('div');el.className='ski-marker'+(i===0?' active':'');el.style.borderLeftColor=m.color;\n"
        "    el.innerHTML='<div class=\"sm-name\">'+m.icon+' '+m.name+'</div>'\n"
        "      +'<div class=\"sm-row\"><span class=\"sm-fl\" style=\"background:'+tc(fl)+'\">'+fl+'\\u00b0</span>'\n"
        "      +'<span class=\"sm-wind\">\U0001F32C'+Math.round(m.wind)+'('+Math.round(m.gust)+')</span>'\n"
        "      +'<span class=\"sm-vis\">\U0001F441'+vis+'</span></div>';\n"
        "    el.addEventListener('click',function(){if(window.hoodPick)window.hoodPick(i);});\n"
        "    new mapboxgl.Marker(el).setLngLat([m.lon,m.lat]).addTo(map);\n"
        "  });\n"
        "  if(window.hoodPick)window.hoodPick();\n"
        "});\n"
    ).replace("__TOKEN__", MAPBOX_TOKEN).replace("__MARKERS__", markers_json).replace("__LIFTS__", lifts_json)

    # Meadows' own cameras (skihood.com), for the panel's Cameras view
    try:
        _mhm_cams = requests.get("https://www.skihood.com/api/weather/webcam",
                                 headers={"User-Agent": "Mozilla/5.0"}, timeout=8).json().get("data", [])
    except Exception:
        _mhm_cams = []
    cam_desc = {"Top of Blue": "Top of the Blue chair", "Top of Cascade": "Top of the Cascade Express",
                "Snowstake": "The snow stake: new snow at a glance", "Top of Heather": "Top of the Heather chair",
                "Top of Vista": "Top of the Vista Express", "Base Area": "The main lodge and base area"}
    by_name = {c.get("name"): c for c in _mhm_cams}
    hood_cams = [{"url": by_name[n]["image"], "page": by_name[n].get("url"), "label": n, "desc": d,
                  "note": "Live Mt Hood Meadows camera"} for n, d in cam_desc.items() if n in by_name and by_name[n].get("image")]
    # two USGS-tracked views of the mountain too: timestamped, and at night they show the last daylight photo
    live_hood = {c.get("code"): c for c in webcams.build().get("mh2", []) if c.get("code")}
    for code, label, desc in (("hood-mhm-heather", "Summit from Meadows", "USGS copy of the Heather cam, looking up at the summit"),
                              ("hood-palmer", "Palmer (Timberline)", "Timberline's Palmer lift, the south side")):
        if code in live_hood:
            hood_cams.append({**live_hood[code], "label": label, "desc": desc})

    # Meadows' daily report (skihood.com)
    _rpt_text = ''
    try:
        _sky = requests.get("https://www.skihood.com/api/weather/report", headers={"User-Agent": "Mozilla/5.0"},
                            timeout=8).json().get("data", {}).get("weather", {}).get("weatherSkyInfos", [])
        by_type = {x.get("type"): (x.get("data") or [{}])[0].get("data", "") for x in _sky}
        for k in ("WEATHER_BULLETIN_OF_THE_DAY", "WEATHER_OTHER"):
            txt = BeautifulSoup(by_type.get(k) or "", "html.parser").get_text(separator=" ", strip=True)
            if len(txt) >= 20:
                _rpt_text = txt
                break
    except Exception:
        pass

    # the word on the mountain: Temira's forecast + the Meadows report
    gorge_html, gorge_status, gorge_posted = fetch_gorge_snow_forecast()
    if gorge_status == 'current' and gorge_html:
        gorge_card = (
            '<div class="ski-card gorge-card">'
            '<div class="ski-card-title">\U0001F3D4 Meteorologist Forecast</div>'
            f'<div class="ski-card-sub">Temira, The Gorge Is My Gym \u00b7 posted {gorge_posted} \u00b7 <a href="https://thegorgeismygym.com/forecast/" target="_blank" rel="noopener">full post \u2197</a></div>'
            f'<div class="gorge-body">{gorge_html}</div>'
            '</div>')
    else:
        why = ("Off-season \u2014 Temira's snow forecast<br/>will return when snow season starts." if gorge_status == 'offseason'
               else f"No new snow forecast since {gorge_posted or 'the last post'}.<br/>Check back tomorrow." if gorge_status
               else "The Gorge Is My Gym couldn't be reached.")
        gorge_card = (
            '<div class="ski-card ski-card-placeholder">'
            '<div class="ski-card-title">\U0001F3D4 Meteorologist Forecast</div>'
            '<div class="ski-card-sub">via The Gorge Is My Gym</div>'
            f'<div style="color:#999;font-size:11px;margin-top:10px;text-align:center;line-height:1.5;">{why}</div></div>')
    report_card = (
        '<div class="ski-card report-card"><div class="ski-card-title">\U0001F4CB Meadows Report</div>'
        '<div class="ski-card-sub">Mt Hood Meadows \u00b7 <a href="https://www.skihood.com/" target="_blank" rel="noopener">skihood.com \u2197</a></div>'
        f'<div class="report-body">{html.escape(_rpt_text[:900])}</div></div>'
        if _rpt_text else
        '<div class="ski-card ski-card-placeholder"><div class="ski-card-title">\U0001F4CB Meadows Report</div>'
        '<div class="ski-card-sub">Mt Hood Meadows</div><div style="color:#999;font-size:11px;margin-top:10px;">No report posted today.</div></div>')

    hp = [{"name": pt["name"], "elev_ft": round(mp["elev_ft"]), "h24": h24}
          for pt, mp, h24 in zip(SKI_POINTS, map_points, h24_all)]
    if layers:
        layers["pts"] = [{"name": x["name"], "elev_ft": x["elev_ft"]} for x in hp]   # for the layer legend's readouts
    lyr_ctl = ('<div class="lyr-ctl" id="hlyr"><div><button data-l="none">Terrain</button><button class="active" data-l="temp">Temperature</button>'
               '<button data-l="gust">Wind gusts</button><button data-l="snow">New snow</button><button data-l="depth">Snow depth</button></div>'
               '<div class="lyr-time"><button data-tplay>\u25B6 Play</button><input type="range" id="hslider" min="0" max="0" step="1" value="0" aria-label="Forecast time">'
               '<span id="hlabel" class="lyr-rtime"></span></div></div><div class="lyr-leg" id="hleg" hidden></div>') if layers else ''

    full_html = '<html><head><style>' + ski_css + '</style></head><body>'
    pick = ('<div class="cc-tiers hood-pick" role="group" aria-label="Point">' + ''.join(
        f'<button class="{"active" if i == 0 else ""}" data-t="{i}">{html.escape(x["name"])}<small>{x["elev_ft"]:,}\u2032</small></button>'
        for i, x in enumerate(hp)) + '</div>')
    full_html += ('<div class="hood-row1"><div class="hood-fc"><div class="hood-fc-head"><h2 class="sec-h">10-day forecast</h2>' + pick + '</div>'
                  + ''.join(f'<div data-p="{i}"{" hidden" if i else ""}>{t}</div>' for i, t in enumerate(point_tables))
                  + '</div>' + gorge_card + '</div>')
    full_html += ('<div class="hood-sum"><div class="hood-fc-head"><h2 class="sec-h">Snow summary</h2>' + pick +
                  '</div><div class="ss-wrap">' + ''.join(snow_sums) + '</div>'
                  '<div class="ss-foot"><span class="ss-key ss-key-p"></span>Past days: estimated from recent model analyses at the point\u2019s elevation \u00b7 '
                  '<span class="ss-key"></span>Next days: the forecast above \u00b7 wet-bulb rain/snow split</div></div>')
    # then, as on Cities and Trails: the map, and the next 24 hours for the chosen point
    full_html += (
        '<div class="dashboard-layout">'
        '<div class="map-panel"><div class="map-wrap trail-map-wrap"><div id="hoodmap"></div>' + lyr_ctl +
        '<div class="legend">Click a point (base, Top of Blue, Top of Cascade) for its forecast \u00b7 layers colour every slope for its own elevation \u00b7 lift lines are schematic</div></div></div>'
        '<div class="cc-panel" id="hood-cc"><div class="cc-head"><div><div class="cc-kicker">Next 24 hours</div>'
        '<div class="cc-pick"><span>Mt Hood Meadows</span></div><div class="cc-wp" id="hood-wp"></div></div>'
        '<div class="cc-head-r"><div class="cc-view" id="hood-view" role="group" aria-label="View">'
        '<button class="active" data-view="fc">Forecast</button><button data-view="cams">Cameras<span id="hood-camn"></span></button></div>'
        '<div class="cc-now"></div></div></div>'
        '<div class="cc-tiers" id="hood-tiers" role="group" aria-label="Point"></div>'
        '<div class="cc-charts"></div><div class="cc-tip" hidden></div>'
        '<div class="cc-cams" id="hood-cams" hidden>'
        '<figure class="cam-main"><img class="cam-img" alt=""><div class="cam-msg" hidden></div></figure>'
        '<div class="cam-cap"></div><div class="cam-thumbs" role="group" aria-label="Other cameras"></div></div></div></div>')
    full_html += '<div class="hood-word">' + report_card + '</div>'
    full_html += ('<script>' + HOOD_JS.replace("__HP__", json.dumps(hp, separators=(",", ":")))
                  .replace("__HCAMS__", json.dumps(hood_cams)) + '</script>')
    if layers:
        full_html += ("<script>window.hoodLayers=function(map){\n"
                      + HOOD_LAYER_JS.replace("__HL__", json.dumps(layers, separators=(",", ":"))) + "\n};</script>")

    # Latest YouTube video from The Christomer
    yt_vid, yt_title, yt_pub = fetch_latest_youtube("@TheChristomer")
    # ODOT TripCheck traffic cameras along Hwy 26 & 35
    TRAFFIC_CAMS = [
        {"name": "Brightwood", "lat": 45.3756, "lon": -121.9382, "img": "https://www.tripcheck.com/roadcams/cams/Brightwood2_pid1381.jpg"},
        {"name": "Ski Bowl West", "lat": 45.3029, "lon": -121.7649, "img": "https://www.tripcheck.com/roadcams/cams/US26 at Ski Bowl West EB_pid4117.jpg"},
        {"name": "Blue Box Pass", "lat": 45.2843, "lon": -121.7067, "img": "https://www.tripcheck.com/roadcams/cams/Blue Box Pass_pid1919.JPG"},
        {"name": "Hood River (I-84)", "lat": 45.7083, "lon": -121.5122, "img": "https://www.tripcheck.com/roadcams/cams/Hood River Exit 64a_pid1863.JPG"},
        {"name": "OR35 Parkdale", "lat": 45.3747, "lon": -121.5825, "img": "https://www.tripcheck.com/roadcams/cams/ORE35 at Parkdale Maint SB_pid4121.jpg"},
        {"name": "OR35 Meadows Dr", "lat": 45.3199, "lon": -121.6598, "img": "https://www.tripcheck.com/roadcams/cams/ORE35 at Meadows Dr SB_pid3874.jpg"},
    ]

    cams_json = json.dumps(TRAFFIC_CAMS)

    # Server-side route fetches (fetch() is blocked inside displayHTML iframe)
    # Mapbox Directions route
    try:
        _mb_dir = requests.get(
            f"https://api.mapbox.com/directions/v5/mapbox/driving/-122.8110,45.5250;-121.66413,45.33144",
            params={"geometries": "geojson", "overview": "full", "access_token": MAPBOX_TOKEN},
            timeout=10
        ).json()
        _mb_rt = _mb_dir["routes"][0]
        _mb_min = round(_mb_rt["duration"] / 60)
        _mb_h, _mb_m = divmod(_mb_min, 60)
        _mb_tstr = f'{_mb_h}h {_mb_m}m' if _mb_h else f'{_mb_min} min'
        _mb_mi = round(_mb_rt["distance"] * 0.000621371)
        mb_route_geojson = json.dumps({"type": "Feature", "geometry": _mb_rt["geometry"]})
        mb_route_info = f'{_mb_mi} mi \u00b7 ~{_mb_tstr}'
    except Exception:
        mb_route_geojson = 'null'
        mb_route_info = ''

    # OSRM travel time (free, no key)
    try:
        _osrm = requests.get(
            "http://router.project-osrm.org/route/v1/driving/-122.8110,45.5250;-121.66413,45.33144",
            params={"overview": "false"}, timeout=8
        ).json()
        _osrm_rt = _osrm["routes"][0]
        _osrm_min = round(_osrm_rt["duration"] / 60)
        _osrm_h, _osrm_m = divmod(_osrm_min, 60)
        _osrm_mi = round(_osrm_rt["distance"] * 0.000621371)
        osrm_line = f'{_osrm_mi} mi \u00b7 ~{_osrm_h}h {_osrm_m}m' if _osrm_h else f'{_osrm_mi} mi \u00b7 ~{_osrm_min} min'
    except Exception:
        osrm_line = ''

    # Side-by-side: YouTube video + Mapbox traffic/camera route map
    bottom_row = '<div style="display:flex;gap:12px;margin-top:18px;align-items:flex-start;">'
    if yt_vid:
        # the video can't play inside the dashboard, so the card is a poster: Mt Hood with the current
        # weather at the base, the post date, and the whole card links to YouTube
        try:
            posted = "Posted " + datetime.strptime(yt_pub, "%Y-%m-%d").strftime("%a, %b %d").replace(" 0", " ")
        except (TypeError, ValueError):
            posted = f"Posted {yt_pub}" if yt_pub else "Latest video"
        bottom_row += (
            '<div class="yt-wrap">'
            '<div class="yt-h">\U0001F3AC The Christomer \u2014 latest video</div>'
            f'<a class="yt-card" href="https://www.youtube.com/watch?v={yt_vid}" target="_blank" rel="noopener" aria-label="Watch on YouTube: {html.escape(yt_title or "latest video")}">'
            '<span class="yt-ridge" aria-hidden="true"></span><span class="yt-play" aria-hidden="true"></span>'
            '<span class="yt-foot">'
            f'<span class="yt-badge" id="yt-badge">{mtn_icon("mh2", 150)}</span>'
            f'<span class="yt-text"><span class="yt-wx" id="yt-wx">Mt Hood Meadows</span>'
            f'<span class="yt-t">{html.escape(yt_title or "Latest video")}</span>'
            f'<span class="yt-d">{html.escape(posted)} \u00b7 <b>Watch on YouTube \u2197</b></span></span>'
            '</span></a></div>'
        )
    # Mapbox traffic + camera route map (Cedar Mill to Meadows)
    bottom_row += (
        '<div style="flex:1;min-width:0;background:#fff;border-radius:10px;box-shadow:0 1px 4px rgba(0,0,0,0.08);padding:12px;">'
        '<div style="font-size:15px;font-weight:700;color:#111;margin-bottom:4px;">\U0001F697 Cedar Mill \u2192 Meadows &nbsp;\U0001F4F7 Road Cams</div>'
        + (f'<div style="font-size:22px;font-weight:800;color:#4FB1BE;margin-bottom:2px;">{mb_route_info}</div>' if mb_route_info else '')
        + '<div style="font-size:11px;color:#888;margin-bottom:2px;">Mapbox estimate</div>'
        + (f'<div style="font-size:11px;color:#999;margin-bottom:6px;">\U0001F310 OSRM: {osrm_line}</div>' if osrm_line else '')
        + '<div style="position:relative;padding-bottom:56.25%;height:0;overflow:hidden;border-radius:8px;">'
        '<div id="routemap" style="position:absolute;top:0;left:0;width:100%;height:100%;"></div></div>'
        '</div>'
    )
    bottom_row += '</div>'
    full_html += bottom_row

    # Camera snapshot strip — single row across full width
    cam_cards = ''
    for ci2, cam in enumerate(TRAFFIC_CAMS):
        cam_cards += (
            f'<div style="flex:1;min-width:0;text-align:center;">'
            f'<img src="{cam["img"]}" alt="{cam["name"]}" style="width:100%;border-radius:6px;display:block;"/>'
            f'<div style="font-size:9px;font-weight:700;color:#FE5000;margin-top:3px;">\U0001F4F7{ci2+1}</div>'
            f'<div style="font-size:9px;font-weight:600;color:#555;">{cam["name"]}</div>'
            f'</div>'
        )
    full_html += (
        '<div style="margin-top:10px;background:#fff;border-radius:10px;box-shadow:0 1px 4px rgba(0,0,0,0.08);padding:10px 12px;">'
        '<div style="font-size:13px;font-weight:700;color:#111;margin-bottom:6px;">\U0001F4F7 Hwy 26 &amp; 35 \u2014 Live ODOT Snapshots</div>'
        f'<div style="display:flex;gap:8px;">{cam_cards}</div>'
        '</div>'
    )

    full_html += '<link href="https://api.mapbox.com/mapbox-gl-js/v3.3.0/mapbox-gl.css" rel="stylesheet">'
    full_html += '<script src="https://api.mapbox.com/mapbox-gl-js/v3.3.0/mapbox-gl.js"></script>'
    full_html += '<script>function toggleSkiDay(idx){var s=document.querySelectorAll(".ski-tbl .d"+idx+".dsum");var d=document.querySelectorAll(".ski-tbl .d"+idx+".ddet");var open=d[0]&&d[0].style.display!=="none";s.forEach(function(el){el.style.display=open?"":"none"});d.forEach(function(el){el.style.display=open?"none":""})}document.querySelectorAll(".ski-tbl").forEach(function(tbl){tbl.addEventListener("click",function(ev){var td=ev.target.closest("td");if(!td)return;var m=td.className.match(/d(\\d+)/);if(m&&td.classList.contains("ddet"))toggleSkiDay(parseInt(m[1]))})});</script>'
    # both Mt Hood maps are built the first time the tab is opened (each is a billed Mapbox map load)
    full_html += "<script>window.lazyMap('hood',function(){\n" + map_js + "\n});</script>"

    # Route map JS: route geometry is pre-fetched server-side and injected
    route_map_js = (
        "(function(){\n"
        "var cams=__CAMS__;\n"
        "var routeGeo=__ROUTE_GEO__;\n"
        "var map2=new mapboxgl.Map({container:'routemap',"
        "style:'mapbox://styles/mapbox/streets-v12',"
        "center:[-121.85,45.35],zoom:9.3,"
        "attributionControl:false});\n"
        "map2.on('load',function(){\n"
        "  var fl=null;\n"
        "  map2.getStyle().layers.forEach(function(ly){\n"
        "    if(!fl&&ly.type==='symbol')fl=ly.id;});\n"
        "  map2.addSource('mapbox-traffic',{type:'vector',"
        "    url:'mapbox://mapbox.mapbox-traffic-v1'});\n"
        "  map2.addLayer({id:'traffic-major',type:'line',"
        "    source:'mapbox-traffic','source-layer':'traffic',"
        "    filter:['in','class',"
        "      'primary','secondary','tertiary','trunk','motorway'],"
        "    paint:{'line-color':['match',['get','congestion'],"
        "      'low','#6BBF68','moderate','#FAA21B',"
        "      'heavy','#FE5000','severe','#ED1E29','#999'],"
        "      'line-width':3,'line-opacity':0.7}},"
        "    fl||undefined);\n"
        "  if(routeGeo){\n"
        "    map2.addSource('route',{type:'geojson',data:routeGeo});\n"
        "    map2.addLayer({id:'route-outline',type:'line',"
        "      source:'route',layout:{'line-cap':'round','line-join':'round'},"
        "      paint:{'line-color':'#368994',"
        "        'line-width':8,'line-opacity':0.3}},"
        "      fl||undefined);\n"
        "    map2.addLayer({id:'route-line',type:'line',"
        "      source:'route',layout:{'line-cap':'round','line-join':'round'},"
        "      paint:{'line-color':'#4FB1BE',"
        "        'line-width':4,'line-opacity':0.9}});\n"
        "  }\n"
        "  cams.forEach(function(c,i){\n"
        "    var el=document.createElement('div');\n"
        "    el.style.cssText='background:#fff;border:2px solid #FE5000;'"
        "      +'border-radius:4px;padding:1px 5px;font-size:10px;'"
        "      +'font-weight:700;color:#111;cursor:pointer;'"
        "      +'box-shadow:0 1px 4px rgba(0,0,0,0.3);';\n"
        "    el.textContent='\U0001F4F7'+(i+1);\n"
        "    var popup=new mapboxgl.Popup({offset:12,"
        "      closeButton:false,maxWidth:'220px'})\n"
        "      .setHTML('<div style=\"font-size:11px;font-weight:700;\">'"
        "        +c.name+'</div>'"
        "        +'<img src=\"'+c.img"
        "        +'\" style=\"width:200px;border-radius:4px;'"
        "        +'margin-top:4px;\"/>');\n"
        "    new mapboxgl.Marker(el)\n"
        "      .setLngLat([c.lon,c.lat])\n"
        "      .setPopup(popup).addTo(map2);\n"
        "  });\n"
        "  var mk=function(ll,lbl,bg){\n"
        "    var d=document.createElement('div');\n"
        "    d.style.cssText='background:'+bg+';color:#fff;'"
        "      +'border-radius:50%;width:22px;height:22px;'"
        "      +'text-align:center;line-height:22px;font-size:11px;'"
        "      +'font-weight:700;box-shadow:0 1px 4px rgba(0,0,0,0.3);';\n"
        "    d.textContent=lbl;\n"
        "    new mapboxgl.Marker(d).setLngLat(ll).addTo(map2);};\n"
        "  mk([-122.8110,45.5250],'A','#6BBF68');\n"
        "  mk([-121.66413,45.33144],'B','#FE5000');\n"
        "});\n"
        "})();\n"
    ).replace("__CAMS__", cams_json).replace("__ROUTE_GEO__", mb_route_geojson)

    full_html += "<script>window.lazyMap('hood',function(){\n" + route_map_js + "\n});</script>"
    full_html += '</body></html>'
    return full_html


# ---------------------------------------------------------------------------
# Combine into one tabbed page
# ---------------------------------------------------------------------------
def _extract(full_html):
    """Return (css, clean_body, list_of_inline_script_strings) from a full HTML doc."""
    styles = re.findall(r'<style>(.*?)</style>', full_html, re.DOTALL)
    css = '\n'.join(styles)
    body_m = re.search(r'<body[^>]*>(.*)</body>', full_html, re.DOTALL)
    body = body_m.group(1) if body_m else full_html
    # Capture inline scripts only (not <script src="...">)
    inline = re.findall(r'<script>(.*?)</script>', body, re.DOTALL)
    # Strip ALL script/link tags from body
    clean = re.sub(r'<script[^>]*>.*?</script>', '', body, flags=re.DOTALL)
    clean = re.sub(r'<link[^>]*>', '', clean)
    return css, clean.strip(), inline


def scope_css(css, scope):
    """Prefix every selector in `css` with `scope`, recursing into @media blocks, so one
    page's stylesheet can't leak onto the others. body/html rules are dropped - they
    styled the page when it stood alone; in the dashboard the shell owns the page."""
    out, i = [], 0
    while i < len(css):
        j = css.find("{", i)
        if j < 0:
            break
        head = css[i:j].strip()
        if head.startswith("@"):   # @media etc: scope the rules inside the block
            depth, k = 1, j + 1
            while depth and k < len(css):
                depth += {"{": 1, "}": -1}.get(css[k], 0)
                k += 1
            out.append(head + " {" + scope_css(css[j + 1:k - 1], scope) + "}")
            i = k
            continue
        k = css.find("}", j)
        sels = [f"{scope} {s}" for s in (s.strip() for s in head.split(",")) if s and s not in ("body", "html")]
        if sels:
            out.append(", ".join(sels) + " " + css[j:k + 1])
        i = k + 1
    return "\n".join(out)


def _wrap_iife(script_str):
    toggle_fns = re.findall(r'function\s+(toggle(?:Day_\w+|SkiDay)|showWp_\w+)\s*\(', script_str)
    exports = '\n'.join('window.' + fn + '=' + fn + ';' for fn in toggle_fns)
    return '(function(){\n' + script_str + '\n' + exports + '\n})();'


def build_dashboard() -> str:
    # verify first, so every forecast below uses the freshly tuned precipitation blend
    stage("Verifying against SNOTEL")
    report = verification.run(MOUNTAINS)
    cal = (report or {}).get("calibration")
    if cal:
        print(f"  blend tuned from {cal['wet_station_days']} wet station-days "
              f"(trust {cal['trust']:.0%}, precip x{cal['qpf_scale']:.2f})")

    stage("Building region map fields")
    region_data, region_frames = region.build()
    # 3-hourly frames live next to the page, one file per day, loaded only when viewed
    for rel, body in region_frames.items():
        path = os.path.join(os.path.dirname(OUTPUT_PATH), rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(body)

    stage("Generating Oregon cities")
    cities_full_html = build_cities_page()

    stage("Fetching air quality")
    aq = aqi_grid()
    aq_on = bool(aq)
    stage("Fetching smoke forecast")
    smoke_data = smoke.build(region.BOUNDS, os.path.dirname(OUTPUT_PATH))
    stage("Fetching active fire perimeters")
    fire_data = fires.active_perimeters(region.BOUNDS)
    n_fires = len(fire_data["features"]) if fire_data else 0
    print(f"  {n_fires} active fires")
    # an overlay toggle, not one of the exclusive layers: fires show over temperature, smoke, radar...
    fire_btn = (f'<button class="lyr-fire" data-fires aria-pressed="false" title="{n_fires} active wildfires">'
                '<svg viewBox="0 0 12 14" width="11" height="13" aria-hidden="true"><path d="M6 .5C6.5 3 9 4.5 9.8 7.3A4 4 0 0 1 2.2 9.5C1.6 7.5 2.8 6 3.6 5c.2 1.3.8 2 1.6 2.3C4.8 5 5.2 2.6 6 .5Z" fill="#E4572E"/></svg>Fires</button>'
                if fire_data else "")
    # the Map tab's layer controls; the Trail Forecast map uses the same ones
    region_ctl = (
        '<div class="lyr-ctl"><div><button class="active" data-l="temp">Temperature</button><button data-l="snow">New snow</button>'
        '<button data-l="gust">Wind gusts</button><button data-l="cloud">Clouds</button><button data-l="radar">Radar</button>'
        + ('<button data-l="aq">Air quality</button>' if aq else '') + ('<button data-l="smoke">Smoke</button>' if smoke_data else '')
        + '<button data-l="none">Terrain</button>' + fire_btn + '</div>'
        '<div class="lyr-time"><button data-tplay>\u25B6 Play</button><span class="lyr-mode"><button class="active" data-mode="hourly">Hourly</button>'
        '<button data-mode="daily">Daily</button></span><input type="range" min="0" max="0" step="1" value="0" aria-label="Forecast date and time">'
        '<span class="lyr-rtime"></span><button data-total hidden>7-day total</button><span class="lyr-mode lyr-smode" hidden>'
        '<button class="active" data-smode="cum">Cumulative</button><button data-smode="24h">24-hour</button></span>'
        '<span class="lyr-mode lyr-kmode" hidden><button class="active" data-kmode="sfc" title="Smoke at breathing level">Surface</button>'
        '<button data-kmode="vert" title="All the smoke overhead">Sky</button></span></div></div>'
        '<div class="lyr-leg" hidden></div><div class="rmap-tip" hidden></div><div class="rmap-busy" hidden>Updating\u2026</div>')
    stage("Generating mountain trails")
    trails_full_html = build_trails_page(aq)

    stage("Generating Mt Hood ski conditions")
    ski_full_html = build_ski_page()

    stage("Assembling the page")
    # Page 1: Cities
    c_css, c_body, c_scripts = _extract(cities_full_html)

    # Page 2: Mountain Trails
    trails_css, trails_body, trails_scripts = _extract(trails_full_html)

    # Page 3: Mt Hood Ski
    k_css, k_body, k_scripts = _extract(ski_full_html)

    # ---- Merge CSS (shared base + page-specific) ----
    # the Mt Hood page's stylesheet uses generic names (.map-panel, table, th...) - scope it to
    # its own tab so it can't restyle the Cities / Trails pages
    merged_css = c_css + '\n' + trails_css + '\n' + scope_css(k_css, '#page3') + '\n' + scope_css(trail_live.TRAIL_CSS, '#page4')

    # Shell CSS: left sidebar nav, page headers, section headers (loaded last, so it wins)
    tab_css = """
    .shell{display:flex;min-height:100vh;}
    .side-nav{flex:0 0 188px;position:sticky;top:0;height:100vh;display:flex;flex-direction:column;gap:2px;
      padding:22px 12px 16px;background:#F2F3F6;border-right:1px solid #E3E5EA;z-index:1000;box-sizing:border-box;}
    .brand{padding:0 10px 20px;font-size:15px;font-weight:700;color:#111;letter-spacing:-.01em;line-height:1.25;}
    .brand small{display:block;margin-top:3px;font-size:10px;font-weight:600;color:#9A9FAB;letter-spacing:.08em;text-transform:uppercase;}
    .tab-btn{position:relative;display:flex;align-items:center;gap:10px;width:100%;padding:9px 10px;border:none;background:none;
      border-radius:8px;color:#5A5F6B;font:inherit;font-size:13.5px;font-weight:600;text-align:left;cursor:pointer;
      transition:background .15s,color .15s;}
    .tab-btn .nv{width:18px;height:18px;flex:none;color:#8A8F9C;transition:color .15s;}
    .tab-btn:hover{background:#E8EAEF;color:#111;}
    .tab-btn.active{background:#fff;color:#111;box-shadow:0 0 0 1px #E3E5EA,0 1px 2px rgba(20,24,35,.05);}
    .tab-btn.active::before{content:"";position:absolute;left:-12px;top:8px;bottom:8px;width:3px;border-radius:0 3px 3px 0;background:#FE5000;}
    .tab-btn.active .nv{color:#FE5000;}
    .tab-btn:focus-visible{outline:2px solid #FE5000;outline-offset:2px;}
    .updated{margin-top:auto;padding:0 10px;font-size:10.5px;line-height:1.5;color:#9A9FAB;font-variant-numeric:tabular-nums;}
    .updated b{display:block;font-weight:600;color:#6B7080;}
    .main{flex:1;min-width:0;position:relative;}
    .page-section{padding:24px 26px;}
    .page-head{margin:0 0 18px;}
    .page-head h1{margin:0;font-size:24px;font-weight:700;letter-spacing:-.02em;color:#111;}
    .page-head p{margin:4px 0 0;font-size:12px;color:#8A8F9C;}
    .sec-rule{margin:32px 0;border:none;border-top:1px solid #E3E5EA;}
    /* section headers: city rows, trail waypoints, ski points */
    .city-detail{margin-bottom:24px;}
    .city-detail-header{display:flex;align-items:center;flex-wrap:wrap;gap:4px 10px;padding:4px 2px 9px;
      border-bottom:1px solid #E3E5EA;cursor:pointer;color:#111;}
    .ch-icon{display:flex;font-size:20px;}
    .ch-name{font-size:17px;font-weight:700;letter-spacing:-.01em;transition:color .15s;}
    .city-detail-header:hover .ch-name{color:#FE5000;}
    .ch-temps{font-size:13px;color:#8A8F9C;font-variant-numeric:tabular-nums;}
    .ch-temps b{color:#111;font-weight:600;}
    .ch-meta{margin-left:auto;font-size:11px;color:#9A9FAB;font-variant-numeric:tabular-nums;}
    .ch-chev{width:16px;height:16px;color:#A0A5B1;transition:transform .2s;}
    .city-detail-header.collapsed .ch-chev{transform:rotate(-90deg);}
    .city-detail-body{margin-top:10px;}
    .city-detail .wp-header{display:none;}
    .wp-header{display:flex;align-items:baseline;flex-wrap:wrap;gap:2px 8px;margin:14px 0 8px;}
    .wp-dot{width:8px;height:8px;border-radius:50%;align-self:center;flex:none;}
    .wp-name{font-size:14px;font-weight:700;color:#111;}
    .wp-meta{font-size:11px;color:#9A9FAB;font-variant-numeric:tabular-nums;}
    .wp-chips{display:flex;flex-wrap:wrap;gap:6px;margin:4px 0 10px;}
    .wp-chip{display:inline-flex;align-items:center;gap:6px;padding:6px 11px;border:1px solid #E3E5EA;border-radius:999px;
      background:#fff;color:#5A5F6B;font:inherit;font-size:12px;font-weight:600;cursor:pointer;
      transition:border-color .15s,color .15s;}
    .wp-chip:hover{border-color:#C9CDD6;color:#111;}
    .wp-chip.active{color:#111;border-color:#111;}
    .wp-chip:focus-visible{outline:2px solid #FE5000;outline-offset:2px;}
    .wp-chip-el{font-weight:500;color:#9A9FAB;font-variant-numeric:tabular-nums;}
    .mtn-chips .wp-chip{font-size:13px;padding:7px 14px;}
    /* Map tab */
    .rmap-wrap{position:relative;height:calc(100vh - 170px);min-height:520px;border:1px solid #E3E5EA;border-radius:12px;overflow:hidden;background:#E9EEF0;}
    #regionmap{position:absolute;inset:0;}
    #rlyr{right:60px;}
    .rmap-tip{position:absolute;z-index:4;pointer-events:none;transform:translate(14px,14px);padding:6px 9px;border-radius:7px;
      background:rgba(17,17,17,.86);color:#fff;font-size:11.5px;line-height:1.4;white-space:nowrap;font-variant-numeric:tabular-nums;}
    .rmap-tip[hidden],.rmap-busy[hidden]{display:none;}
    .rmap-busy{position:absolute;right:60px;bottom:12px;z-index:3;padding:4px 9px;border-radius:7px;background:rgba(255,255,255,.9);
      font-size:11px;font-weight:600;color:#5A5F6B;box-shadow:0 1px 3px rgba(20,24,35,.15);}
    /* Accuracy tab */
    .acc{max-width:1000px;}
    .acc h2{margin:28px 0 4px;font-size:15px;font-weight:700;color:#111;}
    .acc-status{margin:0;padding:11px 14px;border:1px solid #E3E5EA;border-radius:10px;background:#fff;font-size:13px;line-height:1.5;color:#333;max-width:80ch;}
    .acc-sub{margin:0 0 10px;font-size:12px;line-height:1.5;color:#8A8F9C;max-width:90ch;}
    .acc-tbl{border-spacing:0;font-size:13px;}
    .acc-tbl th,.acc-tbl td{position:static;padding:9px 16px;border-bottom:1px solid #EEF0F3;background:none;text-align:right;
      font-variant-numeric:tabular-nums;white-space:nowrap;min-width:0;}
    .acc-tbl th{text-align:left;font-size:13px;font-weight:600;color:#111;}
    .acc-tbl tr:first-child th{font-size:10.5px;font-weight:600;color:#8A8F9C;text-transform:uppercase;letter-spacing:.05em;text-align:right;vertical-align:bottom;}
    .acc-tbl tr:first-child th:first-child{text-align:left;}
    .acc-tbl th span,.acc-tbl td span{display:block;font-size:10.5px;font-weight:400;color:#9A9FAB;text-transform:none;letter-spacing:0;}
    .acc-up::before{content:"\\25B2  ";font-size:8px;color:#4E9A51;}
    .acc-down::before{content:"\\25BC  ";font-size:8px;color:#B7791F;}
    .acc-ok{color:#2F7D32;} .acc-warn{color:#B7791F;} .acc-bad{color:#C53030;}
    .acc-blend th,.acc-blend td{font-weight:700;background:#F5F6F9;}
    .acc-nodata td{text-align:left;font-style:italic;color:#9A9FAB;}
    .acc-foot{margin-top:16px;font-size:11px;line-height:1.55;color:#9A9FAB;max-width:90ch;}
    .acc-empty{color:#8A8F9C;}
    /* 3D terrain map layers */
    .terrain-wrap{position:relative;}
    .lyr-ctl{position:absolute;top:10px;left:10px;right:10px;z-index:2;display:flex;flex-wrap:wrap;gap:6px;align-items:flex-start;pointer-events:none;}
    .lyr-ctl>div{pointer-events:auto;display:inline-flex;flex-wrap:wrap;gap:2px;padding:3px;border-radius:9px;
      background:rgba(255,255,255,.9);backdrop-filter:blur(6px);box-shadow:0 1px 3px rgba(20,24,35,.18);}
    .lyr-ctl>div[hidden],.lyr-ctl button[hidden],.lyr-ctl span[hidden],.lyr-leg[hidden]{display:none;}
    .lyr-ctl button{border:none;background:none;padding:5px 10px;border-radius:7px;font:inherit;font-size:12px;font-weight:600;
      color:#5A5F6B;cursor:pointer;white-space:nowrap;}
    .lyr-ctl button:hover{color:#111;}
    .lyr-ctl button.active{background:#111;color:#fff;}
    /* the fire overlay toggles on/off beside the layer choice, set apart by a divider */
    .lyr-ctl .lyr-fire{display:inline-flex;align-items:center;gap:5px;margin-left:5px;position:relative;}
    .lyr-ctl .lyr-fire::before{content:"";position:absolute;left:-4px;top:5px;bottom:5px;width:1px;background:#DDE0E6;}
    .lyr-ctl .lyr-fire.active{background:#FCE3DB;color:#8A1F14;box-shadow:inset 0 0 0 1px #E4572E;}
    .lyr-ctl button:focus-visible{outline:2px solid #FE5000;outline-offset:1px;}
    .lyr-leg{position:absolute;left:10px;bottom:36px;z-index:2;width:240px;max-width:calc(100% - 20px);padding:8px 10px;
      border-radius:9px;background:rgba(255,255,255,.92);backdrop-filter:blur(6px);box-shadow:0 1px 3px rgba(20,24,35,.18);
      font-size:11px;color:#333;}
    .lyr-bar{height:8px;border-radius:4px;margin:6px 0 3px;box-shadow:inset 0 0 0 1px rgba(0,0,0,.08);}
    .lyr-ticks{display:flex;justify-content:space-between;font-size:10px;color:#8A8F9C;font-variant-numeric:tabular-nums;}
    .lyr-note{margin-top:5px;font-weight:600;color:#111;}
    .lyr-src{margin-top:4px;max-width:260px;font-size:10px;line-height:1.4;color:#8A8F9C;}
    .lyr-where{display:flex;align-items:center;gap:8px;margin-top:3px;color:#5A5F6B;font-variant-numeric:tabular-nums;}
    .lyr-ctl .lyr-time{align-items:center;gap:6px;padding:3px 10px 3px 3px;flex-wrap:nowrap;max-width:100%;}
    .lyr-mode{display:inline-flex;gap:2px;padding:0 6px;border-left:1px solid #E3E5EA;border-right:1px solid #E3E5EA;}
    .lyr-ctl button:disabled{opacity:.4;cursor:default;}
    #tslider{flex:1 1 140px;min-width:80px;max-width:280px;accent-color:#111;cursor:pointer;}
    #tslider:disabled{opacity:.35;cursor:default;}
    .lyr-rtime{min-width:112px;font-size:12px;font-weight:700;color:#111;white-space:nowrap;font-variant-numeric:tabular-nums;}
    .lyr-pt{display:grid;grid-template-columns:34px 1fr;align-items:center;gap:6px;font-size:10.5px;color:#5A5F6B;}
    .lyr-pt .lyr-bar{margin:3px 0;}
    .lyr-go{margin-left:auto;border:none;padding:3px 10px;border-radius:6px;background:#111;color:#fff;font:inherit;font-size:11px;font-weight:600;cursor:pointer;}
    .lyr-go:focus-visible{outline:2px solid #FE5000;outline-offset:2px;}
    @media (max-width:760px){
      .shell{flex-direction:column;}
      .side-nav{flex:none;height:auto;flex-direction:row;align-items:center;gap:4px;padding:8px 12px;
        border-right:none;border-bottom:1px solid #E3E5EA;overflow-x:auto;}
      .brand,.updated{display:none;}
      .tab-btn{width:auto;white-space:nowrap;}
      .tab-btn.active::before{left:10px;right:10px;top:auto;bottom:-8px;width:auto;height:3px;border-radius:3px 3px 0 0;}
      .page-section{padding:18px 16px;}
    }
    @media (prefers-reduced-motion:reduce){.tab-btn,.tab-btn .nv,.ch-chev,.ch-name{transition:none;}}
    .wxi{display:inline-flex;align-items:center;gap:1px;vertical-align:middle;}
    .wx{width:1.35em;height:1.35em;}
    .wx.wx-wind{width:1.1em;height:1.1em;}
    .ds-ci{display:flex;justify-content:center;}
    th .rl{width:1.25em;height:1.25em;vertical-align:-0.28em;margin-right:4px;color:#8A8F9C;}
    td{padding-top:4px;padding-bottom:4px;}
    td.cc{position:relative;height:20px;padding:0;}
    .cf{position:absolute;inset:-2px -6px;border-radius:12px;filter:blur(5px);pointer-events:none;}
    .pd{width:1.05em;height:1.05em;vertical-align:-0.15em;margin-right:2px;}
    .pd0{color:#E3E7EC;} .pd1{color:#A9D6DC;} .pd2{color:#4FB1BE;} .pd3{color:#2F8794;}
    .pb{display:flex;align-items:center;justify-content:center;gap:4px;margin-top:3px;font-size:10px;font-weight:600;
      color:#8A8F9C;font-variant-numeric:tabular-nums;}
    .pb i{width:6px;height:6px;border-radius:50%;box-shadow:inset 0 0 0 1px rgba(0,0,0,.12);}
    """

    # ---- Build combined tabbed HTML ----
    now_pt = datetime.now(ZoneInfo('America/Los_Angeles'))
    # '%A %B %-d, %Y %-I:%M %p %Z' without the Windows-incompatible "%-" codes
    now_str = f"{now_pt:%A %B} {now_pt.day}, {now_pt:%Y} {now_pt.hour % 12 or 12}:{now_pt:%M %p %Z}"
    updated_str = f"{now_pt:%a %b} {now_pt.day} · {now_pt.hour % 12 or 12}:{now_pt:%M %p %Z}"

    combined = f"""<!DOCTYPE html>
    <html><head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>Oregon Weather Forecast — {now_str}</title>
    <link href="https://api.mapbox.com/mapbox-gl-js/v3.3.0/mapbox-gl.css" rel="stylesheet">
    <script src="https://api.mapbox.com/mapbox-gl-js/v3.3.0/mapbox-gl.js"></script>
    <style>
    {merged_css}
    {tab_css}
    </style>
    </head>
    <body style="margin:0;padding:0;background:#FAFAFA;">
    {WX_DEFS}
    <div class="shell">
    <nav class="side-nav" aria-label="Forecast sections">
      <div class="brand">Oregon Weather<small>Daily forecast</small></div>
      <button class="tab-btn active" onclick="showTab(0)"><svg class="nv" aria-hidden="true"><use href="#ri-city"/></svg>Cities</button>
      <button class="tab-btn" onclick="showTab(1)"><svg class="nv" aria-hidden="true"><use href="#ri-peak"/></svg>Mountain Trails</button>
      <button class="tab-btn" onclick="showTab(2)"><svg class="nv" aria-hidden="true"><use href="#ri-map"/></svg>Map</button>
      <button class="tab-btn" onclick="showTab(3)"><svg class="nv" aria-hidden="true"><use href="#ri-lift"/></svg>Mt Hood</button>
      <button class="tab-btn" onclick="showTab(4)"><svg class="nv" aria-hidden="true"><use href="#ri-route"/></svg>Trail Forecast</button>
      <button class="tab-btn" onclick="showTab(5)"><svg class="nv" aria-hidden="true"><use href="#ri-target"/></svg>Accuracy</button>
      <div class="updated"><b>Updated</b>{updated_str}</div>
    </nav>
    <main class="main">
    <div class="page-section" id="page0">
      <header class="page-head"><h1>Cities</h1><p>{len(CITIES)} towns across Oregon and Washington \u00B7 click a city to collapse its forecast</p></header>
      {c_body}</div>
    <div class="page-section" id="page1" style="display:none" data-init="trailsShown">
      <header class="page-head"><h1>Mountain Trails</h1><p>{len(MOUNTAINS)} peaks from Mt. Baker to Crater Lake, the Olympics to the Wallowas \u00B7 pick a mountain, then summit, mid or base</p></header>
      {trails_body}</div>
    <div class="page-section" id="page2" style="display:none" data-init="initRegionMap">
      <header class="page-head"><h1>Map</h1><p>Oregon and Washington \u00B7 temperature, new snow and wind gusts at every elevation, plus air quality \u00B7 drag, zoom and tilt, hover for values</p></header>
      <div class="rmap-wrap">
        <div id="regionmap"></div>
        {region_ctl}
      </div>
      <p class="acc-foot">National Weather Service forecast every {region.CELL_KM:g} km, evaluated at each spot's real elevation: NWS temperature, dew point, gusts and sky cover near the ground, joined to GFS upper-air levels aloft. New snow: NWS precipitation for its first ~3 days (our SNOTEL-tuned model blend after that) with wet-bulb rain/snow. Gusts rise to the GFS free-air wind \u00D7{region.GUST_FACTOR} on exposed ridges; sheltered and forested terrain sees less. Radar: NOAA HRRR for 18 h, then precipitation drawn radar-style.</p>
    </div>
    <div class="page-section" id="page3" style="display:none" data-lazy="hood">
      <header class="page-head"><h1>Mt Hood Meadows</h1><p>Meadows Base, Top of Blue and Top of Cascade \u00B7 next 24 hours, cameras and terrain layers \u00B7 10-day forecast below</p></header>
      {k_body}</div>
    <div class="page-section" id="page4" style="display:none" data-init="trailLiveShown">
      <header class="page-head"><h1>Trail Forecast</h1><p>Any trail: drop in its GPX (or send it from AllTrails with the Chrome extension) for a base and peak forecast and the Map tab\u2019s layers in 3D \u00B7 computed live in your browser</p></header>
      {trail_live.TRAIL_PAGE_HTML.replace("__REGION_CTL__", region_ctl)}</div>
    <div class="page-section" id="page5" style="display:none">
      <header class="page-head"><h1>Forecast Accuracy</h1><p>How our precipitation forecasts compare with SNOTEL gauges near the mountains · re-scored and re-tuned every run</p></header>
      {verification.report_html(report)}</div>
    </main>
    </div>

    <script>
    // Maps are built the first time they're actually shown: Mapbox bills every map created,
    // so a visit that only looks at Cities shouldn't pay for the Trails and Mt Hood maps.
    // lazyMap(key, fn) queues fn; runLazy(key) runs that key's queue once, in a visible container.
    window.LAZY={{q:{{}},done:{{}}}};
    window.lazyMap=function(k,fn){{if(LAZY.done[k]){{fn();return;}}(LAZY.q[k]=LAZY.q[k]||[]).push(fn);}};
    window.runLazy=function(k){{LAZY.done[k]=true;var q=LAZY.q[k]||[];LAZY.q[k]=[];
      q.forEach(function(f){{try{{f();}}catch(e){{console.error(e);}}}});}};
    function showTab(idx) {{
      for(var i=0;i<document.querySelectorAll('.page-section').length;i++) {{
        var p=document.getElementById('page'+i);
        p.style.display=i===idx?'block':'none';
        document.querySelectorAll('.tab-btn')[i].classList.toggle('active', i===idx);
      }}
      // first view of a page builds its maps: data-init names a function, data-lazy a lazyMap key
      var pg=document.getElementById('page'+idx);
      if(pg&&pg.dataset.init&&window[pg.dataset.init]){{var f=pg.dataset.init;pg.dataset.init='';window[f]();}}
      if(pg&&pg.dataset.lazy){{window.runLazy(pg.dataset.lazy);}}
      setTimeout(function(){{ window.dispatchEvent(new Event('resize')); }}, 200);
    }}
    </script>

    """

    # Wrap each page's scripts in IIFEs to avoid 'var map' collisions between pages.
    # Expose toggle functions (used by onclick attrs) to window scope.
    def _wrap_iife(script_str):
        toggle_fns = re.findall(r'function\s+(toggle(?:Day_\w+|SkiDay)|showWp_\w+)\s*\(', script_str)
        exports = '\n'.join('window.' + fn + '=' + fn + ';' for fn in toggle_fns)
        return '(function(){\n' + script_str + '\n' + exports + '\n})();'

    combined += '<script>' + CHARTS_LIB_JS + '</script>\n'   # the 24-hour chart panel, used by Cities, Trails and Mt Hood
    combined += '<script>' + CAMS_LIB_JS + '</script>\n'     # the camera view, used by Trails and Mt Hood
    combined += '<script>' + TERRAIN_LAYERS_JS + '</script>\n'   # terrain layers on a 3D map, Mt Hood and the trail page
    combined += '<script>' + trail_live.TRAIL_LIVE_JS + '</script>\n'   # the Trail Forecast tab's in-browser engine
    for s in c_scripts:
        combined += '<script>' + _wrap_iife(s) + '</script>\n'
    for s in trails_scripts:
        combined += '<script>' + _wrap_iife(s) + '</script>\n'
    for s in k_scripts:
        combined += '<script>' + _wrap_iife(s) + '</script>\n'
    combined += '<script>' + (REGION_JS.replace("__DATA__", json.dumps(region_data, separators=(",", ":")))
                              .replace("__TOKEN__", MAPBOX_TOKEN)
                              .replace("__FIRES__", json.dumps(fire_data, separators=(",", ":")))
                              .replace("__SMOKE__", json.dumps(smoke_data, separators=(",", ":")))) + '</script>\n'

    combined += '</body></html>'
    return combined


_T0 = time.time()


def stage(msg):
    """Timestamped build-log line, so a slow run shows where the time went."""
    t = int(time.time() - _T0)
    print(f"[{t // 60:02d}:{t % 60:02d}] {msg}  ({http_cache.progress()})", flush=True)


def _heartbeat():
    while True:
        time.sleep(60)
        stage("...still working")


def main():
    threading.Thread(target=_heartbeat, daemon=True).start()
    html_out = build_dashboard()
    stage("Done")
    os.makedirs(os.path.dirname(OUTPUT_PATH), exist_ok=True)
    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        f.write(html_out)
    print(f"Wrote {OUTPUT_PATH} ({len(html_out) / 1024:.0f} KB)")
    print(f"API usage: {http_cache.summary()}")


if __name__ == "__main__":
    main()
