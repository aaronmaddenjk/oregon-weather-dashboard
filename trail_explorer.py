"""
Trail Explorer data: every official trail in Oregon and Washington, from the USGS National
Digital Trails dataset (public domain; it merges U.S. Forest Service, National Park Service,
BLM, Fish & Wildlife and Washington State Parks trails).

build(out_dir) writes docs/explorer/trails.json, loaded by the Explorer tab when first shown:
  1. download the summer ("Terra") trails in region.BOUNDS, simplified to ~10 m (cached a week)
  2. stitch the pieces (split at every junction) back into whole trails: same name + trail
     number + agency, pieces whose ends meet within 40 m
  3. per trail: length, allowed uses, and elevation (low, high, gain walking from the low end)
     from Mapbox terrain-RGB tiles at zoom 12 (~15 m pixels). Tiles and per-trail results are
     cached on disk for good: terrain doesn't change, so later builds fetch nothing.
     Gain uses the Trail Forecast method (10 m points, averaged over 100 m), which matched
     AllTrails' listed gains within ~1%.
"""
import hashlib
import io
import json
import math
import os
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor

import requests

import region

USGS = "https://carto.nationalmap.gov/arcgis/rest/services/transportation/MapServer/37/query"
ROOT = os.path.dirname(os.path.abspath(__file__))
CACHE = os.path.join(ROOT, ".cache", "explorer")
TILE_DIR = os.path.join(ROOT, ".cache", "terrain")
RAW_MAX_AGE_DAYS = 7
JOIN_M = 40            # piece ends closer than this are the same junction
MIN_MILES = 0.25       # drop stubs (lodge connectors, 0.05 mi spurs)
ZOOM = 12
MAX_NEW_TILES = int(os.environ.get("WX_EXPLORER_MAX_TILES", 6000))   # per build; ~750k/month are free
FIELDS = ("objectid,name,trailnumber,lengthmiles,sourceoriginator,hikerpedestrian,bicycle,packsaddle,"
          "motorcycle,ohvover50inches,ohvisorunder50inches,crosscountryski,snowshoe")
AGENCY = {   # short codes for the page (colour + filter)
    "U.S. Forest Service": "fs", "National Park Service": "nps", "Bureau of Land Management": "blm",
    "Washington State Parks and Recreation Commission": "wsp", "U.S. Fish and Wildlife Service": "fws",
}


def _yes(v):
    return str(v or "").strip().lower() in ("y", "yes", "true", "1")


# ---------- 1. download ----------
def _download():
    feats, offset = [], 0
    w, s, e, n = region.BOUNDS
    while True:
        for attempt in range(3):
            try:
                d = requests.get(USGS, params={
                    "f": "json", "where": "trailtype='Terra Trail'", "geometry": f"{w},{s},{e},{n}",
                    "geometryType": "esriGeometryEnvelope", "inSR": 4326, "outSR": 4326,
                    "spatialRel": "esriSpatialRelIntersects", "outFields": FIELDS, "returnGeometry": "true",
                    "maxAllowableOffset": 0.0001, "geometryPrecision": 5, "orderByFields": "objectid",
                    "resultOffset": offset, "resultRecordCount": 2000}, timeout=(10, 180)).json()
                break
            except (requests.RequestException, ValueError):
                if attempt == 2:
                    raise
                time.sleep(5)
        page = d.get("features", [])
        feats += page
        if len(page) < 2000 and not d.get("exceededTransferLimit"):
            break
        offset += len(page)
    return feats


def _raw():
    os.makedirs(CACHE, exist_ok=True)
    path = os.path.join(CACHE, "usgs_trails.json")
    try:
        if time.time() - os.path.getmtime(path) < RAW_MAX_AGE_DAYS * 86400:
            with open(path, encoding="utf-8") as f:
                return json.load(f)
    except OSError:
        pass
    feats = _download()
    with open(path, "w", encoding="utf-8") as f:
        json.dump(feats, f)
    return feats


# ---------- 2. stitch ----------
def _m(a, b):
    la = math.radians((a[1] + b[1]) / 2)
    return math.hypot((b[0] - a[0]) * math.cos(la), b[1] - a[1]) * 111320


def _line_m(p):
    return sum(_m(p[i - 1], p[i]) for i in range(1, len(p)))


def _components(paths):
    """Group paths whose ends meet (within JOIN_M) into connected pieces of trail."""
    parent = list(range(len(paths)))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    cell = JOIN_M / 111320 * 2
    grid = defaultdict(list)
    for i, p in enumerate(paths):
        for end in (p[0], p[-1]):
            grid[(round(end[0] / cell), round(end[1] / cell))].append((i, end))
    for i, p in enumerate(paths):
        for end in (p[0], p[-1]):
            gx, gy = round(end[0] / cell), round(end[1] / cell)
            for dx in (-1, 0, 1):
                for dy in (-1, 0, 1):
                    for j, other in grid.get((gx + dx, gy + dy), ()):
                        if j != i and _m(end, other) < JOIN_M:
                            parent[find(i)] = find(j)
    comps = defaultdict(list)
    for i in range(len(paths)):
        comps[find(i)].append(paths[i])
    return list(comps.values())


def _main_line(paths):
    """The longest end-to-end chain through a component: start from the longest piece and keep
    adding the longest unused piece that touches either end (flipping it as needed)."""
    left = sorted(paths, key=_line_m, reverse=True)
    line = list(left.pop(0))
    grown = True
    while grown and left:
        grown = False
        for k, p in enumerate(left):
            if _m(line[-1], p[0]) < JOIN_M:
                line += p[1:]
            elif _m(line[-1], p[-1]) < JOIN_M:
                line += p[::-1][1:]
            elif _m(line[0], p[-1]) < JOIN_M:
                line = p[:-1] + line
            elif _m(line[0], p[0]) < JOIN_M:
                line = p[::-1][:-1] + line
            else:
                continue
            left.pop(k)
            grown = True
            break
    return line, left


KEEP_CAPS = {"OHV", "ATV", "NRT", "PCT", "CCC", "USFS", "BLM", "NF", "FS", "II", "III", "AA"}
SMALL = {"of", "the", "and", "to", "at", "on", "in", "by", "via"}


def _nice(name):
    """Some forests record names in capitals ("SNOW LAKE"): write them like the rest."""
    if not name.isupper():
        return name
    words = name.split()
    out = []
    for k, w in enumerate(words):
        if w in KEEP_CAPS or any(ch.isdigit() for ch in w):
            out.append(w)
        elif k and w.lower() in SMALL:
            out.append(w.lower())
        else:
            out.append("-".join(p[:1] + p[1:].lower() for p in w.split("-")))
    return " ".join(out)


def _stitch(feats):
    groups = defaultdict(list)
    for f in feats:
        a, g = f["attributes"], f.get("geometry") or {}
        name = _nice((a.get("name") or "").strip())
        if not name or name.lower() in ("unnamed", "unknown", "none"):
            continue
        if (a.get("trailnumber") or "").strip().upper().startswith("SNO"):   # snow routes filed as summer trails
            continue
        key = (name, (a.get("trailnumber") or "").strip(), a.get("sourceoriginator") or "")
        for p in g.get("paths", []):
            if len(p) >= 2:
                groups[key].append((p, a))
    trails = []
    for (name, num, src), items in groups.items():
        by_path = {id(p): a for p, a in items}
        for comp in _components([p for p, _ in items]):
            attrs = [by_path[id(p)] for p in comp]
            miles = sum(_line_m(p) for p in comp) / 1609.344
            if miles < MIN_MILES:
                continue
            line, branches = _main_line(comp)
            uses = ""
            if any(_yes(a.get("hikerpedestrian")) for a in attrs): uses += "h"
            if any(_yes(a.get("bicycle")) for a in attrs): uses += "b"
            if any(_yes(a.get("packsaddle")) for a in attrs): uses += "r"
            if any(_yes(a.get(k)) for a in attrs for k in ("motorcycle", "ohvover50inches", "ohvisorunder50inches")):
                uses += "m"
            trails.append({"name": name, "num": num, "src": AGENCY.get(src, "other"), "srcName": src,
                           "mi": round(miles, 1), "uses": uses, "line": line, "branches": branches})
    return trails


# ---------- 3. elevation ----------
_tiles = {}


def _tile(x, y, token):
    key = (x, y)
    if key in _tiles:
        return _tiles[key]
    from PIL import Image
    path = os.path.join(TILE_DIR, str(ZOOM), str(x), f"{y}.png")
    if not os.path.exists(path):
        for attempt in range(4):   # connections drop now and then under many parallel requests
            try:
                r = requests.get(f"https://api.mapbox.com/v4/mapbox.terrain-rgb/{ZOOM}/{x}/{y}.pngraw",
                                 params={"access_token": token}, timeout=(10, 60))
                r.raise_for_status()
                break
            except requests.RequestException:
                if attempt == 3:
                    raise RuntimeError(f"terrain tile {ZOOM}/{x}/{y} unavailable") from None   # no token in logs
                time.sleep(2 * (attempt + 1))
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as f:
            f.write(r.content)
    img = Image.open(path).convert("RGB")
    w = img.width
    px = img.tobytes()
    _tiles[key] = (w, px)
    return _tiles[key]


def _txy(lon, lat):
    n = 2 ** ZOOM
    y = (1 - math.log(math.tan(math.radians(lat)) + 1 / math.cos(math.radians(lat))) / math.pi) / 2 * n
    return (lon + 180) / 360 * n, y


def _elev(lon, lat, token):
    fx, fy = _txy(lon, lat)
    tx, ty = int(fx), int(fy)
    w, px = _tile(tx, ty, token)
    x, y = (fx - tx) * w - 0.5, (fy - ty) * w - 0.5
    x0, y0 = max(0, min(w - 2, int(math.floor(x)))), max(0, min(w - 2, int(math.floor(y))))
    ax, ay = min(1, max(0, x - x0)), min(1, max(0, y - y0))

    def h(i, j):
        k = (j * w + i) * 3
        return -10000 + (px[k] * 65536 + px[k + 1] * 256 + px[k + 2]) * 0.1
    return (h(x0, y0) * (1 - ax) * (1 - ay) + h(x0 + 1, y0) * ax * (1 - ay)
            + h(x0, y0 + 1) * (1 - ax) * ay + h(x0 + 1, y0 + 1) * ax * ay)


def _densify(line, step=10):
    out = [line[0]]
    for a, b in zip(line, line[1:]):
        seg = _m(a, b)
        for k in range(1, int(seg // step) + 1):
            f = k * step / seg
            if f < 1:
                out.append([a[0] + (b[0] - a[0]) * f, a[1] + (b[1] - a[1]) * f])
        out.append(b)
    return out


def _profile_stats(line, token):
    pts = _densify(line)
    z = [_elev(p[0], p[1], token) for p in pts]
    d = [0.0]
    for i in range(1, len(pts)):
        d.append(d[-1] + _m(pts[i - 1], pts[i]))
    sm, a, b, s = [], 0, 0, 0.0   # average over 100 m of trail (as in the Trail Forecast tab)
    for i in range(len(z)):
        while b < len(z) and d[b] <= d[i] + 50:
            s += z[b]; b += 1
        while d[a] < d[i] - 50:
            s -= z[a]; a += 1
        sm.append(s / (b - a))
    up = sum(max(0, sm[i] - sm[i - 1]) for i in range(1, len(sm)))
    down = sum(max(0, sm[i - 1] - sm[i]) for i in range(1, len(sm)))
    start_low = z[0] <= z[-1]
    return {"lo": min(z), "hi": max(z), "gain": up if start_low else down, "flip": not start_low}


def _elevations(trails, token):
    """Low/high/gain for every trail, cached per trail geometry."""
    path = os.path.join(CACHE, "elevation.json")
    try:
        with open(path, encoding="utf-8") as f:
            cache = json.load(f)
    except (OSError, ValueError):
        cache = {}
    todo = []
    for t in trails:
        t["key"] = hashlib.sha1(json.dumps(t["line"]).encode()).hexdigest()[:16]
        if t["key"] not in cache:
            todo.append(t)
    if todo and token:
        need = {tuple(int(v) for v in _txy(*p)) for t in todo for p in _densify(t["line"], 200)}
        need = [k for k in need if not os.path.exists(os.path.join(TILE_DIR, str(ZOOM), str(k[0]), f"{k[1]}.png"))]
        print(f"  explorer: elevation for {len(todo)} trails ({len(need)} new terrain tiles)", flush=True)
        if len(need) > MAX_NEW_TILES:   # the owner watches Mapbox usage: never surprise-fetch thousands
            print(f"  explorer: skipping elevation, {len(need)} tiles is over the {MAX_NEW_TILES} cap", flush=True)
            need, todo = [], []
        with ThreadPoolExecutor(24) as ex:   # Mapbox takes ~2.5 s per terrain tile: fetch many at once
            def fetch(k):
                try:
                    _tile(k[0], k[1], token)
                except Exception as e:   # that trail just gets no elevation this build
                    print(f"  explorer: {e}", flush=True)
            list(ex.map(fetch, need))
        for i, t in enumerate(todo):
            try:
                cache[t["key"]] = _profile_stats(t["line"], token)
            except Exception as e:   # one bad tile shouldn't sink the build
                print(f"  explorer: no elevation for {t['name']}: {e}", flush=True)
            if i % 1000 == 999:
                with open(path, "w", encoding="utf-8") as f:
                    json.dump(cache, f)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(cache, f)
    for t in trails:
        t["elev"] = cache.get(t["key"])


# ---------- output ----------
def _encode(line):   # Google encoded polyline, precision 5, from [lon, lat]
    out, plat, plon = [], 0, 0
    for lon, lat in line:
        la, lo = round(lat * 1e5), round(lon * 1e5)
        for v in (la - plat, lo - plon):
            v = ~(v << 1) if v < 0 else v << 1
            while v >= 0x20:
                out.append(chr((0x20 | (v & 0x1f)) + 63))
                v >>= 5
            out.append(chr(v + 63))
        plat, plon = la, lo
    return "".join(out)


def build(out_dir, token):
    """Write <out_dir>/explorer/trails.json; returns the number of trails (0 if unavailable)."""
    try:
        feats = _raw()
    except Exception as e:
        print(f"  WARNING: trail explorer data unavailable ({e})", flush=True)
        return 0
    trails = _stitch(feats)
    _elevations(trails, token)
    rows = []
    for t in trails:
        ev = t.get("elev")
        line = t["line"][::-1] if ev and ev.get("flip") else t["line"]   # start at the low end
        rows.append([t["name"], t["num"], t["src"], t["mi"], t["uses"],
                     round(ev["gain"] * 3.28084) if ev else None,
                     round(ev["hi"] * 3.28084) if ev else None,
                     round(ev["lo"] * 3.28084) if ev else None,
                     _encode(line), [_encode(b) for b in t["branches"]]])
    rows.sort(key=lambda r: r[0])
    os.makedirs(os.path.join(out_dir, "explorer"), exist_ok=True)
    with open(os.path.join(out_dir, "explorer", "trails.json"), "w", encoding="utf-8") as f:
        json.dump({"fields": ["name", "num", "src", "mi", "uses", "gain", "hi", "lo", "line", "branches"],
                   "built": time.strftime("%Y-%m-%d"), "trails": rows}, f, separators=(",", ":"))
    return len(rows)


# ---------- the Explorer tab (markup, styles, in-browser code) ----------
EXPLORER_HTML = """
<div id="ex-root">
  <div class="ex-bar" role="group" aria-label="Filter trails">
    <input type="search" id="ex-q" placeholder="Search trail names" aria-label="Search trail names">
    <label>Length<select id="ex-len"><option value="">Any</option><option value="0,3">Under 3 mi</option>
      <option value="3,6">3–6 mi</option><option value="6,10">6–10 mi</option><option value="10,999">10+ mi</option></select></label>
    <label>Gain<select id="ex-gain"><option value="">Any</option><option value="0,1000">Under 1,000′</option>
      <option value="1000,2500">1,000–2,500′</option><option value="2500,4000">2,500–4,000′</option>
      <option value="4000,99999">4,000′+</option></select></label>
    <label>High point<select id="ex-hi"><option value="">Any</option><option value="4000">Above 4,000′</option>
      <option value="6000">Above 6,000′</option><option value="8000">Above 8,000′</option></select></label>
    <label>Land<select id="ex-src"><option value="">All</option><option value="fs">National Forest</option>
      <option value="nps">National Park</option><option value="blm">BLM</option><option value="wsp">WA State Park</option>
      <option value="fws">Wildlife Refuge</option></select></label>
    <span class="ex-chips">
      <button type="button" data-use="b" aria-pressed="false">Bikes OK</button>
      <button type="button" data-use="r" aria-pressed="false">Horses OK</button>
      <button type="button" data-use="nm" aria-pressed="true">No motorized</button>
    </span>
  </div>
  <div class="ex-body">
    <div class="ex-mapwrap"><div id="exmap"></div><div class="ex-tip" hidden></div>
      <div class="ex-status" id="ex-status">Loading trails…</div></div>
    <aside class="ex-side">
      <div class="ex-card" id="ex-card" hidden></div>
      <div class="ex-count" id="ex-count"></div>
      <label class="ex-sort">Sort<select id="ex-sort"><option value="name">Name</option><option value="mi">Length</option>
        <option value="gain">Gain</option><option value="hi">High point</option></select></label>
      <ol class="ex-list" id="ex-list"></ol>
    </aside>
  </div>
  <p class="ex-foot" id="ex-foot">Trails: USGS National Digital Trails (U.S. Forest Service, National Park Service, BLM,
    Fish &amp; Wildlife Service and Washington State Parks; public domain), joined into whole trails where their
    pieces meet. Gain and high point from Mapbox terrain, climbing from the trail’s low end. Oregon state parks
    and city and county trails aren’t in this dataset yet.</p>
</div>
"""

EXPLORER_CSS = r"""
[hidden] { display:none !important; }
.ex-bar { display:flex; flex-wrap:wrap; align-items:center; gap:8px 14px; margin-bottom:12px; }
.ex-bar input[type=search] { flex:1 1 220px; max-width:320px; padding:8px 12px; border:1px solid #DDE0E6; border-radius:8px; font:inherit; font-size:13px; background:#fff; }
.ex-bar label, .ex-sort { display:flex; align-items:center; gap:6px; font-size:12px; font-weight:600; color:#5A5F6B; }
.ex-bar select, .ex-sort select { padding:6px 8px; border:1px solid #DDE0E6; border-radius:8px; font:inherit; font-size:13px; background:#fff; color:#111; }
.ex-bar input:focus, .ex-bar select:focus, .ex-sort select:focus { outline:2px solid #FE5000; outline-offset:1px; border-color:transparent; }
.ex-chips { display:flex; gap:6px; }
.ex-chips button { border:1px solid #DDE0E6; background:#fff; border-radius:999px; padding:5px 11px; font:inherit; font-size:12px; font-weight:600; color:#5A5F6B; cursor:pointer; }
.ex-chips button[aria-pressed=true] { background:#111; border-color:#111; color:#fff; }
.ex-body { display:grid; grid-template-columns:minmax(0,1fr) 340px; gap:14px; align-items:start; }
.ex-mapwrap { position:relative; }
#exmap { width:100%; height:660px; border-radius:10px; }
.ex-status { position:absolute; left:12px; top:12px; background:#fff; border-radius:8px; padding:6px 10px; font-size:12px; color:#5A5F6B; box-shadow:0 1px 4px rgba(0,0,0,.15); }
.ex-tip { position:absolute; pointer-events:none; background:#fff; border-radius:6px; padding:5px 9px; font-size:12px; box-shadow:0 2px 8px rgba(0,0,0,.2); white-space:nowrap; transform:translate(12px,-50%); }
.ex-tip b { color:#111; } .ex-tip span { color:#8A8F9C; margin-left:6px; font-variant-numeric:tabular-nums; }
.ex-side { display:flex; flex-direction:column; gap:10px; max-height:660px; }
.ex-card { background:#fff; border-radius:10px; padding:14px; box-shadow:0 1px 4px rgba(0,0,0,.08); border-top:3px solid #FE5000; }
.ex-card h3 { margin:0; font-size:17px; font-weight:800; color:#111; letter-spacing:-.01em; }
.ex-card .ex-sub { margin-top:2px; font-size:12px; color:#8A8F9C; }
.ex-stats { display:grid; grid-template-columns:repeat(4,1fr); gap:6px; margin:12px 0; }
.ex-stats div { font-size:10.5px; color:#8A8F9C; text-transform:uppercase; letter-spacing:.04em; }
.ex-stats b { display:block; font-size:15px; color:#111; letter-spacing:0; text-transform:none; font-variant-numeric:tabular-nums; }
.ex-uses { display:flex; flex-wrap:wrap; gap:5px; margin-bottom:12px; }
.ex-uses span { font-size:11px; font-weight:600; color:#5A5F6B; background:#F2F3F6; border-radius:999px; padding:3px 8px; }
.ex-acts { display:flex; gap:8px; align-items:center; flex-wrap:wrap; }
.ex-go { border:0; background:#FE5000; color:#fff; border-radius:8px; padding:8px 14px; font:inherit; font-size:13px; font-weight:700; cursor:pointer; }
.ex-go:hover { background:#E24700; }
.ex-acts a { font-size:13px; font-weight:600; color:#5A5F6B; }
.ex-count { font-size:12px; color:#5A5F6B; font-variant-numeric:tabular-nums; }
.ex-count b { color:#111; }
.ex-sort { align-self:flex-start; }
.ex-list { list-style:none; margin:0; padding:0; overflow-y:auto; background:#fff; border-radius:10px; box-shadow:0 1px 4px rgba(0,0,0,.08); flex:1 1 auto; min-height:120px; }
.ex-list li { padding:9px 12px; border-top:1px solid #F0F1F4; cursor:pointer; }
.ex-list li:first-child { border-top:0; }
.ex-list li:hover { background:#FAFAFB; }
.ex-list li.sel { background:#FFF4EE; box-shadow:inset 3px 0 0 #FE5000; }
.ex-list .n { font-size:13px; font-weight:700; color:#111; }
.ex-list .m { font-size:11.5px; color:#8A8F9C; margin-top:1px; font-variant-numeric:tabular-nums; }
.ex-list .more { color:#8A8F9C; font-size:12px; cursor:default; }
.ex-foot { margin:14px 0 0; font-size:11px; color:#9A9FAB; line-height:1.5; }
@media (max-width:1000px) { .ex-body { grid-template-columns:minmax(0,1fr); } #exmap { height:460px; } .ex-side { max-height:none; } .ex-list { max-height:420px; } }
"""

EXPLORER_JS = r"""
(function(){
var root=document.getElementById('ex-root');if(!root)return;
var $=function(id){return document.getElementById(id);};
var AG={fs:'National Forest',nps:'National Park',blm:'BLM',wsp:'WA State Park',fws:'Wildlife Refuge',other:'Other'};
var USE={h:'Hiking',b:'Bikes',r:'Horses',m:'Motorized'};
var T=[],map=null,sel=-1,hover=-1;
function fmt(n){return n==null?'–':Math.round(n).toLocaleString('en-US');}
function decode(str){var i=0,lat=0,lng=0,out=[];while(i<str.length){for(var k=0;k<2;k++){var b,sh=0,v=0;
  do{b=str.charCodeAt(i++)-63;v|=(b&31)<<sh;sh+=5;}while(b>=32);var d=v&1?~(v>>1):v>>1;if(k)lng+=d;else lat+=d;}
  out.push([lng/1e5,lat/1e5]);}return out;}
function meta(t){return t.mi.toFixed(1)+' mi · '+(t.gain==null?'':fmt(t.gain)+'′ gain · ')+(t.hi==null?'':fmt(t.hi)+'′ top');}

// ---------- filters ----------
function crit(){var c={q:$('ex-q').value.trim().toLowerCase(),src:$('ex-src').value,uses:[]};
  var l=$('ex-len').value,g=$('ex-gain').value,h=$('ex-hi').value;
  if(l){l=l.split(',');c.len=[+l[0],+l[1]];} if(g){g=g.split(',');c.gain=[+g[0],+g[1]];} if(h)c.hi=+h;
  root.querySelectorAll('.ex-chips [aria-pressed=true]').forEach(function(b){c.uses.push(b.dataset.use);});
  return c;}
function match(t,c){
  if(c.q&&t.lc.indexOf(c.q)<0)return false; if(c.src&&t.src!==c.src)return false;
  if(c.len&&(t.mi<c.len[0]||t.mi>=c.len[1]))return false;
  if(c.gain&&(t.gain==null||t.gain<c.gain[0]||t.gain>=c.gain[1]))return false;
  if(c.hi&&(t.hi==null||t.hi<c.hi))return false;
  for(var i=0;i<c.uses.length;i++){var u=c.uses[i];if(u==='nm'){if(t.uses.indexOf('m')>=0)return false;}else if(t.uses.indexOf(u)<0)return false;}
  return true;}
function mapFilter(c){   // the same test as match(), as a Mapbox expression
  var f=['all'];
  if(c.q)f.push(['in',c.q,['get','lc']]); if(c.src)f.push(['==',['get','src'],c.src]);
  if(c.len)f.push(['>=',['get','mi'],c.len[0]],['<',['get','mi'],c.len[1]]);
  if(c.gain)f.push(['>=',['get','gain'],c.gain[0]],['<',['get','gain'],c.gain[1]]);
  if(c.hi)f.push(['>=',['get','hi'],c.hi]);
  c.uses.forEach(function(u){f.push(u==='nm'?['==',['get','m'],0]:['==',['get',u],1]);});
  return f;}
function apply(){var c=crit(),f=mapFilter(c);
  ['ex-line','ex-hit'].forEach(function(l){if(map.getLayer(l))map.setFilter(l,f);});
  T.forEach(function(t){t.ok=match(t,c);});
  list();}

// ---------- list of matches in view ----------
function list(){if(!map)return;var b=map.getBounds(),w=b.getWest(),e=b.getEast(),s=b.getSouth(),n=b.getNorth();
  var all=0,rows=[];
  T.forEach(function(t){if(!t.ok)return;all++;if(t.bb[2]<w||t.bb[0]>e||t.bb[3]<s||t.bb[1]>n)return;rows.push(t);});
  var k=$('ex-sort').value;
  rows.sort(k==='name'?function(a,b){return a.name.localeCompare(b.name);}:function(a,b){return (b[k]==null?-1:b[k])-(a[k]==null?-1:a[k]);});
  $('ex-count').innerHTML='<b>'+rows.length.toLocaleString('en-US')+'</b> in view · '+all.toLocaleString('en-US')+' match';
  var html=rows.slice(0,150).map(function(t){return '<li data-i="'+t.i+'"'+(t.i===sel?' class="sel"':'')+'><div class="n">'+esc(t.name)+
    (t.num?' <span class="m">#'+esc(t.num)+'</span>':'')+'</div><div class="m">'+meta(t)+' · '+AG[t.src]+'</div></li>';}).join('');
  if(rows.length>150)html+='<li class="more">Zoom in to see the other '+(rows.length-150).toLocaleString('en-US')+'</li>';
  if(!rows.length)html='<li class="more">No trails here match. Zoom out or loosen the filters.</li>';
  $('ex-list').innerHTML=html;}
function allTrails(bb){   // AllTrails' explore map on this trail's area (its search box doesn't take a URL query)
  var px=Math.max(0.01,(bb[2]-bb[0])*0.25),py=Math.max(0.008,(bb[3]-bb[1])*0.25),f=function(v){return v.toFixed(4);};
  return 'https://www.alltrails.com/explore?b_tl_lat='+f(bb[3]+py)+'&b_tl_lng='+f(bb[0]-px)+'&b_br_lat='+f(bb[1]-py)+'&b_br_lng='+f(bb[2]+px);}
function esc(s){return String(s).replace(/[&<>"]/g,function(ch){return{'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[ch];});}

// ---------- the selected trail ----------
function select(i,fly){sel=i;var t=T[i];
  map.setFilter('ex-sel',['==',['get','i'],i]);map.setFilter('ex-sel-case',['==',['get','i'],i]);
  var uses=t.uses.split('').map(function(u){return '<span>'+USE[u]+'</span>';}).join('');
  $('ex-card').innerHTML='<h3>'+esc(t.name)+'</h3><div class="ex-sub">'+(t.num?'Trail #'+esc(t.num)+' · ':'')+AG[t.src]+'</div>'+
    '<div class="ex-stats"><div>Length<b>'+t.mi.toFixed(1)+' mi</b></div><div>Gain<b>'+fmt(t.gain)+'′</b></div>'+
    '<div>High<b>'+fmt(t.hi)+'′</b></div><div>Low<b>'+fmt(t.lo)+'′</b></div></div>'+
    (uses?'<div class="ex-uses">'+uses+'</div>':'')+
    '<div class="ex-acts"><button class="ex-go" type="button">Forecast this trail</button>'+
    '<a target="_blank" rel="noopener" href="'+allTrails(t.bb)+'">Nearby hikes on AllTrails ↗</a></div>';
  $('ex-card').hidden=false;
  $('ex-card').querySelector('.ex-go').onclick=function(){
    if(window.openTrailForecast)window.openTrailForecast(t.line,t.name+(t.num&&t.name.indexOf(t.num)<0?' (#'+t.num+')':''),'');};
  if(fly)map.fitBounds([[t.bb[0],t.bb[1]],[t.bb[2],t.bb[3]]],{padding:80,maxZoom:14,duration:700});
  root.querySelectorAll('.ex-list li.sel').forEach(function(li){li.classList.remove('sel');});
  var li=root.querySelector('.ex-list li[data-i="'+i+'"]');if(li)li.classList.add('sel');}

// ---------- map ----------
function init(d){
  var F=d.fields,ix={};F.forEach(function(f,k){ix[f]=k;});
  var feats=[];
  T=d.trails.map(function(r,i){var line=decode(r[ix.line]),parts=[line].concat(r[ix.branches].map(decode));
    var bb=[180,90,-180,-90];parts.forEach(function(p){p.forEach(function(q){if(q[0]<bb[0])bb[0]=q[0];if(q[1]<bb[1])bb[1]=q[1];if(q[0]>bb[2])bb[2]=q[0];if(q[1]>bb[3])bb[3]=q[1];});});
    var t={i:i,name:r[ix.name],num:r[ix.num],src:r[ix.src],mi:r[ix.mi],uses:r[ix.uses]||'',gain:r[ix.gain],hi:r[ix.hi],lo:r[ix.lo],
      line:r[ix.line],bb:bb,ok:true};
    t.lc=t.name.toLowerCase()+' '+(t.num||'').toLowerCase();
    feats.push({type:'Feature',geometry:{type:'MultiLineString',coordinates:parts},
      properties:{i:i,lc:t.lc,src:t.src,mi:t.mi,gain:t.gain==null?-1:t.gain,hi:t.hi==null?-1:t.hi,
        h:t.uses.indexOf('h')>=0?1:0,b:t.uses.indexOf('b')>=0?1:0,r:t.uses.indexOf('r')>=0?1:0,m:t.uses.indexOf('m')>=0?1:0}});
    return t;});
  map=new mapboxgl.Map({container:'exmap',style:'mapbox://styles/mapbox/outdoors-v12',center:[-121.75,45.42],zoom:9.3,attributionControl:false});
  map.addControl(new mapboxgl.NavigationControl({showCompass:false}),'top-right');
  map.addControl(new mapboxgl.AttributionControl({compact:true}));
  map.on('load',function(){
    map.addSource('ex',{type:'geojson',data:{type:'FeatureCollection',features:feats},tolerance:0.6});
    var w=['interpolate',['linear'],['zoom'],6,0.7,10,1.6,14,3];
    map.addLayer({id:'ex-line',type:'line',source:'ex',layout:{'line-join':'round','line-cap':'round'},
      paint:{'line-color':'#23405A','line-width':w,'line-opacity':0.8}});
    map.addLayer({id:'ex-hover',type:'line',source:'ex',filter:['==',['get','i'],-1],layout:{'line-join':'round','line-cap':'round'},
      paint:{'line-color':'#FE5000','line-width':['interpolate',['linear'],['zoom'],6,2,14,5],'line-opacity':0.6}});
    map.addLayer({id:'ex-sel-case',type:'line',source:'ex',filter:['==',['get','i'],-1],layout:{'line-join':'round','line-cap':'round'},
      paint:{'line-color':'#fff','line-width':['interpolate',['linear'],['zoom'],6,4,14,9]}});
    map.addLayer({id:'ex-sel',type:'line',source:'ex',filter:['==',['get','i'],-1],layout:{'line-join':'round','line-cap':'round'},
      paint:{'line-color':'#FE5000','line-width':['interpolate',['linear'],['zoom'],6,2.5,14,5.5]}});
    map.addLayer({id:'ex-hit',type:'line',source:'ex',paint:{'line-color':'#000','line-opacity':0,'line-width':14}});
    var tip=root.querySelector('.ex-tip');
    map.on('mousemove','ex-hit',function(e){var f=e.features[0],i=f.properties.i,t=T[i];map.getCanvas().style.cursor='pointer';
      if(hover!==i){hover=i;map.setFilter('ex-hover',['==',['get','i'],i]);}
      tip.innerHTML='<b>'+esc(t.name)+'</b><span>'+meta(t)+'</span>';tip.style.left=e.point.x+'px';tip.style.top=e.point.y+'px';tip.hidden=false;});
    map.on('mouseleave','ex-hit',function(){hover=-1;map.setFilter('ex-hover',['==',['get','i'],-1]);map.getCanvas().style.cursor='';tip.hidden=true;});
    map.on('click','ex-hit',function(e){select(e.features[0].properties.i,false);});
    map.on('moveend',list);
    $('ex-status').hidden=true;
    apply();});
}
root.querySelectorAll('.ex-bar select, #ex-sort').forEach(function(s){s.addEventListener('change',function(){if(map)(s.id==='ex-sort'?list:apply)();});});
var qt;$('ex-q').addEventListener('input',function(){clearTimeout(qt);qt=setTimeout(function(){if(map)apply();},200);});
root.querySelectorAll('.ex-chips button').forEach(function(b){b.addEventListener('click',function(){
  b.setAttribute('aria-pressed',b.getAttribute('aria-pressed')==='true'?'false':'true');if(map)apply();});});
$('ex-list').addEventListener('click',function(e){var li=e.target.closest('li[data-i]');if(li)select(+li.dataset.i,true);});
window.explorerShown=function(){
  fetch('explorer/trails.json').then(function(r){if(!r.ok)throw new Error(r.status);return r.json();})
    .then(init).catch(function(){$('ex-status').textContent='Trail data isn’t available in this build.';});};
})();
"""


if __name__ == "__main__":
    from dotenv import load_dotenv
    load_dotenv()
    n = build(os.path.join(ROOT, "docs"), os.environ.get("MAPBOX_TOKEN"))
    print(f"{n} trails")
