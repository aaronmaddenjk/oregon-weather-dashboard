"""
Trail Explorer data: the official trails of Oregon and Washington, from four public sources:
  - USGS National Digital Trails (public domain): U.S. Forest Service, National Park Service,
    BLM, Fish & Wildlife and Washington State Parks trails
  - Oregon Parks and Recreation Department: state park trails
  - Oregon Department of Forestry: Tillamook, Clatsop, Santiam... state forest trails
  - Oregon Metro RLIS: Portland-area city, county and Metro park trails (unpaved, open)

build(out_dir, token) writes docs/explorer/trails.json, loaded when the Map tab's Trails layer
is first switched on:
  1. download each source (simplified to ~10 m; cached a week)
  2. stitch the pieces (split at every junction) back into whole trails: same name + trail
     number + agency, pieces whose ends meet within 150 m
  3. per trail: length, allowed uses, difficulty (state forests), and elevation (low, high, gain
     walking from the low end) from Mapbox terrain-RGB tiles at zoom 12 (~15 m pixels). Tiles and
     per-trail results are cached on disk for good: terrain doesn't change, so later builds
     only fetch tiles for trails in new places. Gain uses the Trail Forecast method (10 m points,
     averaged over 100 m), which matched AllTrails' listed gains within ~1%.
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

USGS = "https://carto.nationalmap.gov/arcgis/rest/services/transportation/MapServer/37"
OPRD = "https://services.arcgis.com/uUvqNMGPm7axC2dD/arcgis/rest/services/OPRD_Rec_Trails_Hosted_view/FeatureServer/0"
ODF = "https://services.arcgis.com/uUvqNMGPm7axC2dD/arcgis/rest/services/Recreation_Inventory_Public_View/FeatureServer/13"
METRO = "https://services2.arcgis.com/McQ0OlIABe29rJJy/arcgis/rest/services/Trails/FeatureServer/0"
ROOT = os.path.dirname(os.path.abspath(__file__))
CACHE = os.path.join(ROOT, ".cache", "explorer")
TILE_DIR = os.path.join(ROOT, ".cache", "terrain")
RAW_MAX_AGE_DAYS = 7
JOIN_M = 150           # same-name pieces this close are one trail (road crossings leave ~100 m gaps)
MIN_MILES = 0.25       # drop stubs (lodge connectors, 0.05 mi spurs)
ZOOM = 12
MAX_NEW_TILES = int(os.environ.get("WX_EXPLORER_MAX_TILES", 6000))   # per build; ~750k/month are free
AGENCY = {   # USGS source agencies -> short codes for the page (filter + labels)
    "U.S. Forest Service": "fs", "National Park Service": "nps", "Bureau of Land Management": "blm",
    "Washington State Parks and Recreation Commission": "wsp", "U.S. Fish and Wildlife Service": "fws",
}
ODF_USES = {101: "h", 102: "hr", 103: "hb", 104: "hrb", 105: "b", 100: "hrb",
            201: "m", 202: "m", 203: "m", 204: "m", 205: "m", 200: "m"}
ODF_DIFF = {1: "Easy", 2: "Moderate", 3: "Difficult", 4: "Extreme"}


def _yes(v):
    return str(v or "").strip().lower() in ("y", "yes", "true", "1")


# ---------- 1. download ----------
def _query_all(url, where, fields, bbox=None):
    """Every feature of an ArcGIS layer matching `where`, 2,000 at a time, simplified to ~10 m.
    The first of `fields` is the layer's ID field (a stable order for paging)."""
    feats, offset = [], 0
    while True:
        params = {"f": "json", "where": where, "outFields": fields, "returnGeometry": "true", "outSR": 4326,
                  "maxAllowableOffset": 0.0001, "geometryPrecision": 5, "orderByFields": fields.split(",")[0],
                  "resultOffset": offset, "resultRecordCount": 2000}
        if bbox:
            params.update(geometry=",".join(map(str, bbox)), geometryType="esriGeometryEnvelope", inSR=4326,
                          spatialRel="esriSpatialRelIntersects")
        for attempt in range(3):
            try:
                d = requests.get(url + "/query", params=params, timeout=(10, 180)).json()
                if "error" in d:
                    raise ValueError(d["error"].get("message"))
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


def _cached(name, fetch):
    os.makedirs(CACHE, exist_ok=True)
    path = os.path.join(CACHE, name + ".json")
    try:
        if time.time() - os.path.getmtime(path) < RAW_MAX_AGE_DAYS * 86400:
            with open(path, encoding="utf-8") as f:
                return json.load(f)
    except OSError:
        pass
    feats = fetch()
    with open(path, "w", encoding="utf-8") as f:
        json.dump(feats, f)
    return feats


def _pieces():
    """Every source as uniform pieces: {name, num, src, uses, diff, paths}. A source that's down
    is skipped (with a warning); the others still build."""
    out = []

    def add(name, num, src, uses, paths, diff=None):
        name = _nice((name or "").strip())
        if not name or name.lower() in ("unnamed", "unknown", "none", "null"):
            return
        paths = [p for p in (paths or []) if len(p) >= 2]
        if paths:
            out.append({"name": name, "num": (num or "").strip(), "src": src, "uses": uses, "diff": diff, "paths": paths})

    def source(tag, fetch, convert):
        try:
            feats = _cached(tag, fetch)
        except Exception as e:
            print(f"  WARNING: {tag} trails unavailable ({e})", flush=True)
            return
        n0 = len(out)
        for f in feats:
            convert(f["attributes"], (f.get("geometry") or {}).get("paths"))
        print(f"  explorer: {tag}: {len(feats):,} pieces -> {len(out) - n0:,} named", flush=True)

    def usgs(a, paths):
        if (a.get("trailnumber") or "").strip().upper().startswith("SNO"):   # snow routes filed as summer trails
            return
        uses = ("h" if _yes(a.get("hikerpedestrian")) else "") + ("b" if _yes(a.get("bicycle")) else "") \
            + ("r" if _yes(a.get("packsaddle")) else "") \
            + ("m" if any(_yes(a.get(k)) for k in ("motorcycle", "ohvover50inches", "ohvisorunder50inches")) else "")
        add(a.get("name"), a.get("trailnumber"), AGENCY.get(a.get("sourceoriginator"), "other"), uses, paths)

    def oprd(a, paths):
        name = a.get("name") or ""
        add(name, "", "osp", "m" if any(k in name.upper() for k in ("ATV", "OHV", "DUNE ACCESS")) else "h", paths)

    def odf(a, paths):
        add(a.get("trailname"), "", "odf", ODF_USES.get(a.get("trailuse"), ""), paths, ODF_DIFF.get(a.get("difficulty")))

    def metro(a, paths):
        uses = ("h" if _yes(a.get("HIKE")) else "") + ("b" if _yes(a.get("MTNBIKE")) or _yes(a.get("ROADBIKE")) else "") \
            + ("r" if _yes(a.get("EQUESTRIAN")) else "")
        add(a.get("TRAILNAME"), "", "local", uses, paths)

    w, s_, e, n = region.BOUNDS
    source("usgs_trails", lambda: _query_all(USGS, "trailtype='Terra Trail'",
           "objectid,name,trailnumber,sourceoriginator,hikerpedestrian,bicycle,packsaddle,motorcycle,"
           "ohvover50inches,ohvisorunder50inches", (w, s_, e, n)), usgs)
    source("oprd_trails", lambda: _query_all(OPRD, "public_display='YES'", "OBJECTID,name"), oprd)
    source("odf_trails", lambda: _query_all(ODF, "trailstatus=0 AND trailtype IN (0,1)",
                                            "OBJECTID,trailname,trailuse,difficulty"), odf)
    source("metro_trails", lambda: _query_all(
        METRO, "STATUS IN ('Open','Open_Fee') AND TRLSURFACE NOT IN ('Hard Surface','Water') "
               "AND AGENCYTYPE NOT IN ('State','Federal')",
        "FID,TRAILNAME,HIKE,MTNBIKE,ROADBIKE,EQUESTRIAN"), metro)
    return out


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


def _stitch(pieces):
    groups = defaultdict(list)
    for pc in pieces:
        for p in pc["paths"]:
            groups[(pc["name"], pc["num"], pc["src"])].append((p, pc))
    trails = []
    for (name, num, src), items in groups.items():
        by_path = {id(p): pc for p, pc in items}
        for comp in _components([p for p, _ in items]):
            pcs = [by_path[id(p)] for p in comp]
            miles = sum(_line_m(p) for p in comp) / 1609.344
            if miles < MIN_MILES:
                continue
            line, branches = _main_line(comp)
            uses = "".join(u for u in "hbrm" if any(u in pc["uses"] for pc in pcs))
            diffs = [pc["diff"] for pc in pcs if pc["diff"]]
            trails.append({"name": name, "num": num, "src": src, "mi": round(miles, 1), "uses": uses,
                           "diff": max(diffs, key=list(ODF_DIFF.values()).index) if diffs else None,
                           "line": line, "branches": branches})
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
    if not start_low:   # the profile runs from the low end, like the published line
        total = d[-1]
        d, sm = [total - x for x in reversed(d)], sm[::-1]
    return {"lo": min(z), "hi": max(z), "gain": up if start_low else down, "flip": not start_low,
            "prof": _sample_profile(d, sm)}


PROFILE_STEP_M = 160.934   # the page's elevation profile: one sample every 0.1 mile, in feet


def _sample_profile(d, z):
    """Elevation (ft) every 0.1 mile along the trail, plus the far end."""
    out, j, x = [], 0, 0.0
    while x <= d[-1]:
        while j < len(d) - 2 and d[j + 1] < x:
            j += 1
        f = 0 if d[j + 1] == d[j] else min(1, max(0, (x - d[j]) / (d[j + 1] - d[j])))
        out.append(round((z[j] + (z[j + 1] - z[j]) * f) * 3.28084))
        x += PROFILE_STEP_M
    if d[-1] - (x - PROFILE_STEP_M) > 1:
        out.append(round(z[-1] * 3.28084))
    return out


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
        if "prof" not in cache.get(t["key"], {}):   # new trail, or cached before profiles existed
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


def _encode_ints(vals):   # the same character scheme, for one delta-coded integer series
    out, prev = [], 0
    for v in vals:
        d = v - prev
        d = ~(d << 1) if d < 0 else d << 1
        while d >= 0x20:
            out.append(chr((0x20 | (d & 0x1f)) + 63))
            d >>= 5
        out.append(chr(d + 63))
        prev = v
    return "".join(out)


def build(out_dir, token):
    """Write <out_dir>/explorer/trails.json; returns the number of trails (0 if unavailable)."""
    trails = _stitch(_pieces())
    if not trails:
        print("  WARNING: no trail data available", flush=True)
        return 0
    _elevations(trails, token)
    rows = []
    for t in trails:
        ev = t.get("elev")
        line = t["line"][::-1] if ev and ev.get("flip") else t["line"]   # start at the low end
        rows.append([t["name"], t["num"], t["src"], t["mi"], t["uses"],
                     round(ev["gain"] * 3.28084) if ev else None,
                     round(ev["hi"] * 3.28084) if ev else None,
                     round(ev["lo"] * 3.28084) if ev else None,
                     _encode(line), [_encode(b) for b in t["branches"]], t["diff"],
                     _encode_ints(ev["prof"]) if ev and ev.get("prof") else ""])
    rows.sort(key=lambda r: r[0])
    os.makedirs(os.path.join(out_dir, "explorer"), exist_ok=True)
    with open(os.path.join(out_dir, "explorer", "trails.json"), "w", encoding="utf-8") as f:
        json.dump({"fields": ["name", "num", "src", "mi", "uses", "gain", "hi", "lo", "line", "branches", "diff", "prof"],
                   "built": time.strftime("%Y-%m-%d"), "trails": rows}, f, separators=(",", ":"))
    return len(rows)


# ---------- the Trails layer (Map tab panel, styles, in-browser code) ----------
# The Map tab's "Trails" toggle (RegionLayers) calls window.TrailsLayer(map, opt): trail lines over
# any weather layer, the dark hover tip shared with the weather readout, and - on the Map tab,
# which passes opt.panel - a side panel with filters, the trails in view, a card with "Forecast
# this trail", and your own GPX trails (kept in this browser's localStorage, 'wx-mytrails').
TRAILS_PANEL_HTML = """
<aside class="ex-side" id="ex-side" hidden aria-label="Trails">
  <div class="ex-head"><b>Trails</b><span class="ex-count" id="ex-count">Loading\u2026</span></div>
  <input type="search" id="ex-q" placeholder="Search trail names" aria-label="Search trail names">
  <div class="ex-filt">
    <label>Length<select id="ex-len"><option value="">Any</option><option value="0,3">Under 3 mi</option>
      <option value="3,6">3\u20136 mi</option><option value="6,10">6\u201310 mi</option><option value="10,999">10+ mi</option></select></label>
    <label>Gain<select id="ex-gain"><option value="">Any</option><option value="0,1000">Under 1,000\u2032</option>
      <option value="1000,2500">1,000\u20132,500\u2032</option><option value="2500,4000">2,500\u20134,000\u2032</option>
      <option value="4000,99999">4,000\u2032+</option></select></label>
    <label>High point<select id="ex-hi"><option value="">Any</option><option value="4000">Above 4,000\u2032</option>
      <option value="6000">Above 6,000\u2032</option><option value="8000">Above 8,000\u2032</option></select></label>
    <label>Land<select id="ex-src"><option value="">All</option><option value="mine">Your trails</option>
      <option value="fs">National Forest</option><option value="nps">National Park</option><option value="osp">Oregon State Park</option>
      <option value="wsp">WA State Park</option><option value="odf">Oregon State Forest</option><option value="local">City &amp; county</option>
      <option value="blm">BLM</option><option value="fws">Wildlife Refuge</option></select></label>
  </div>
  <div class="ex-chips">
    <button type="button" data-use="b" aria-pressed="false">Bikes OK</button>
    <button type="button" data-use="r" aria-pressed="false">Horses OK</button>
    <button type="button" data-use="nm" aria-pressed="true">No motorized</button>
  </div>
  <label class="ex-add"><input type="file" id="ex-gpx" accept=".gpx,application/gpx+xml,application/xml,text/xml" hidden>
    + Add your GPX</label>
  <div class="ex-card" id="ex-card" hidden></div>
  <div class="ex-listhead"><span id="ex-inview"></span><label>Sort<select id="ex-sort"><option value="name">Name</option>
    <option value="mi">Length</option><option value="gain">Gain</option><option value="hi">High point</option></select></label></div>
  <ol class="ex-list" id="ex-list"></ol>
  <p class="ex-foot">Trails: USGS National Digital Trails (Forest Service, Park Service, BLM, Fish &amp; Wildlife, WA State
    Parks), Oregon Parks and Recreation, Oregon Dept. of Forestry and Oregon Metro, joined into whole trails. Gain and
    high point from Mapbox terrain, climbing from the low end. Your GPX trails stay in this browser.</p>
</aside>
"""

TRAILS_CSS = r"""
.rmap-row { display:grid; grid-template-columns:minmax(0,1fr); gap:12px; }
.rmap-row.trl { grid-template-columns:minmax(0,1fr) 330px; }
.ex-side { display:flex; flex-direction:column; gap:9px; height:calc(100vh - 170px); min-height:520px; overflow-y:auto; }
.ex-side[hidden], .ex-card[hidden] { display:none !important; }
.ex-head { display:flex; align-items:baseline; justify-content:space-between; gap:8px; }
.ex-head b { font-size:15px; color:#111; }
.ex-count, .ex-listhead { font-size:12px; color:#5A5F6B; font-variant-numeric:tabular-nums; }
.ex-count b, .ex-listhead b { color:#111; }
.ex-side input[type=search] { padding:8px 11px; border:1px solid #DDE0E6; border-radius:8px; font:inherit; font-size:13px; background:#fff; }
.ex-filt { display:grid; grid-template-columns:1fr 1fr; gap:6px 8px; }
.ex-filt label, .ex-listhead label { display:flex; flex-direction:column; gap:2px; font-size:10.5px; font-weight:600; color:#8A8F9C; text-transform:uppercase; letter-spacing:.04em; }
.ex-listhead label { flex-direction:row; align-items:center; gap:6px; }
.ex-side select { padding:5px 7px; border:1px solid #DDE0E6; border-radius:7px; font:inherit; font-size:12.5px; background:#fff; color:#111; text-transform:none; letter-spacing:0; }
.ex-side input:focus, .ex-side select:focus { outline:2px solid #FE5000; outline-offset:1px; border-color:transparent; }
.ex-chips { display:flex; flex-wrap:wrap; gap:5px; }
.ex-chips button { border:1px solid #DDE0E6; background:#fff; border-radius:999px; padding:4px 10px; font:inherit; font-size:11.5px; font-weight:600; color:#5A5F6B; cursor:pointer; }
.ex-chips button[aria-pressed=true] { background:#111; border-color:#111; color:#fff; }
.ex-add { align-self:flex-start; font-size:12.5px; font-weight:700; color:#6B3FA8; cursor:pointer; }
.ex-add:hover { text-decoration:underline; }
.ex-add.busy { color:#8A8F9C; pointer-events:none; }
.ex-card { background:#fff; border-radius:10px; padding:12px 13px; box-shadow:0 1px 4px rgba(0,0,0,.08); border-top:3px solid #FE5000; }
.ex-card.mine { border-top-color:#6B3FA8; }
.ex-card h3 { margin:0; font-size:16px; font-weight:800; color:#111; letter-spacing:-.01em; }
.ex-card .ex-sub { margin-top:2px; font-size:12px; color:#8A8F9C; }
.ex-stats { display:grid; grid-template-columns:repeat(4,1fr); gap:4px; margin:10px 0; }
.ex-stats div { font-size:10px; color:#8A8F9C; text-transform:uppercase; letter-spacing:.04em; }
.ex-stats b { display:block; font-size:14px; color:#111; letter-spacing:0; text-transform:none; font-variant-numeric:tabular-nums; }
.ex-prof { position:relative; margin:2px 0 10px; }
.ex-prof-t { font-size:11px; font-weight:700; color:#111; margin-bottom:2px; }
.ex-prof-t span { font-weight:400; color:#8A8F9C; }
.ex-prof svg { display:block; width:100%; height:auto; overflow:visible; touch-action:none; }
.ex-prof svg text { font-family:inherit; font-size:9.5px; font-weight:700; fill:#3F4450; font-variant-numeric:tabular-nums; }
.ex-prof svg text.ax { font-weight:400; fill:#8A8F9C; }
.ex-ptip { position:absolute; top:14px; right:0; background:rgba(17,17,17,.86); color:#fff; border-radius:6px; padding:3px 7px; font-size:11px; font-variant-numeric:tabular-nums; pointer-events:none; }
.ex-ptip[hidden] { display:none !important; }
.ex-leg { display:flex; flex-wrap:wrap; gap:4px 10px; margin-top:4px; font-size:10.5px; color:#3F4450; }
.ex-leg span { display:inline-flex; align-items:center; gap:4px; }
.ex-leg i { width:10px; height:10px; border-radius:2px; box-shadow:inset 0 0 0 1px rgba(0,0,0,.08); }
.ex-leg em { font-style:normal; color:#8A8F9C; }
.ex-uses { display:flex; flex-wrap:wrap; gap:4px; margin-bottom:10px; }
.ex-uses span { font-size:11px; font-weight:600; color:#5A5F6B; background:#F2F3F6; border-radius:999px; padding:2px 8px; }
.ex-acts { display:flex; gap:10px; align-items:center; flex-wrap:wrap; }
.ex-go { border:0; background:#FE5000; color:#fff; border-radius:8px; padding:7px 12px; font:inherit; font-size:12.5px; font-weight:700; cursor:pointer; }
.ex-go:hover { background:#E24700; }
.ex-acts a, .ex-acts .ex-del { font-size:12px; font-weight:600; color:#5A5F6B; background:none; border:0; padding:0; font-family:inherit; cursor:pointer; text-decoration:underline; text-underline-offset:2px; }
.ex-listhead { display:flex; justify-content:space-between; align-items:center; }
.ex-list { list-style:none; margin:0; padding:0; background:#fff; border-radius:10px; box-shadow:0 1px 4px rgba(0,0,0,.08); flex:1 0 auto; }
.ex-list li { padding:8px 11px; border-top:1px solid #F0F1F4; cursor:pointer; }
.ex-list li:first-child { border-top:0; }
.ex-list li:hover { background:#FAFAFB; }
.ex-list li.sel { background:#FFF4EE; box-shadow:inset 3px 0 0 #FE5000; }
.ex-list li.mine .n::before { content:""; display:inline-block; width:7px; height:7px; border-radius:50%; background:#6B3FA8; margin-right:6px; vertical-align:1px; }
.ex-list .n { font-size:13px; font-weight:700; color:#111; }
.ex-list .n span, .ex-list .m { font-size:11.5px; font-weight:400; color:#8A8F9C; font-variant-numeric:tabular-nums; }
.ex-list .more { color:#8A8F9C; font-size:12px; cursor:default; }
.ex-foot { margin:0; font-size:10.5px; color:#9A9FAB; line-height:1.5; }
.lyr-ctl .lyr-trl.active { background:#EFE8F8; color:#4B2A7A; box-shadow:inset 0 0 0 1px #6B3FA8; }
@media (max-width:1000px) { .rmap-row.trl { grid-template-columns:minmax(0,1fr); } .ex-side { height:auto; min-height:0; } }
"""

TRAILS_JS = r"""
(function(){
var AG={fs:'National Forest',nps:'National Park',blm:'BLM',wsp:'WA State Park',fws:'Wildlife Refuge',osp:'Oregon State Park',
  odf:'Oregon State Forest',local:'City & county',mine:'Your trail',other:'Other'};
var USE={h:'Hiking',b:'Bikes',r:'Horses',m:'Motorized'};
var MINE_KEY='wx-mytrails',DATA=null;
// average grade of a mile of climb: one orange ramp, light to dark (downhill/flat miles stay grey)
var GR=[{max:8,k:'Easy',r:'<8%',c:'#FCD5AE'},{max:15,k:'Medium',r:'8–15%',c:'#F59A55'},
  {max:22,k:'Hard',r:'15–22%',c:'#D9580F'},{max:1e9,k:'Strenuous',r:'22%+',c:'#8A3107'}];
function gradeOf(g){g=Math.round(g);   // classify the number shown, so label and colour agree
  if(g<1)return null;for(var k=0;k<GR.length;k++)if(g<GR[k].max)return GR[k];}
function decodeInts(str){var i=0,v=0,out=[];while(i<str.length){var b,sh=0,r=0;
  do{b=str.charCodeAt(i++)-63;r|=(b&31)<<sh;sh+=5;}while(b>=32);v+=r&1?~(r>>1):r>>1;out.push(v);}return out;}
function fmt(n){return n==null?'\u2013':Math.round(n).toLocaleString('en-US');}
function esc(s){return String(s).replace(/[&<>"]/g,function(ch){return{'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[ch];});}
function decode(str){var i=0,lat=0,lng=0,out=[];while(i<str.length){for(var k=0;k<2;k++){var b,sh=0,v=0;
  do{b=str.charCodeAt(i++)-63;v|=(b&31)<<sh;sh+=5;}while(b>=32);var d=v&1?~(v>>1):v>>1;if(k)lng+=d;else lat+=d;}
  out.push([lng/1e5,lat/1e5]);}return out;}
function encode(pts){var out='',pl=0,pn=0;function put(v){v=v<0?~(v<<1):v<<1;while(v>=0x20){out+=String.fromCharCode((0x20|(v&31))+63);v>>=5;}out+=String.fromCharCode(v+63);}
  pts.forEach(function(p){var a=Math.round(p.lat*1e5),b=Math.round(p.lon*1e5);put(a-pl);put(b-pn);pl=a;pn=b;});return out;}
function meta(t){return t.mi.toFixed(1)+' mi \u00B7 '+(t.gain==null?'':fmt(t.gain)+'\u2032 gain \u00B7 ')+(t.hi==null?'':fmt(t.hi)+'\u2032 top');}
function readMine(){try{return JSON.parse(localStorage.getItem(MINE_KEY)||'[]')||[];}catch(e){return[];}}
function writeMine(a){try{localStorage.setItem(MINE_KEY,JSON.stringify(a));return true;}catch(e){return false;}}
function allTrails(bb){   // AllTrails' explore map on this area (its search box doesn't take a URL query)
  var px=Math.max(0.01,(bb[2]-bb[0])*0.25),py=Math.max(0.008,(bb[3]-bb[1])*0.25),f=function(v){return v.toFixed(4);};
  return 'https://www.alltrails.com/explore?b_tl_lat='+f(bb[3]+py)+'&b_tl_lng='+f(bb[0]-px)+'&b_br_lat='+f(bb[1]-py)+'&b_br_lng='+f(bb[2]+px);}
function loadData(){if(!DATA)DATA=fetch('explorer/trails.json').then(function(r){if(!r.ok)throw new Error(r.status);return r.json();});return DATA;}

window.TrailsLayer=function(map,opt){
  var P=opt.panel||null,tip=opt.tip,T=[],on=false,ready=null,sel=-1,hov=-1;
  var $=function(id){return P?P.querySelector('#'+id):null;};
  function trail(r,ix,i){var parts=[decode(r[ix.line])].concat((r[ix.branches]||[]).map(decode)),bb=[180,90,-180,-90];
    parts.forEach(function(p){p.forEach(function(q){if(q[0]<bb[0])bb[0]=q[0];if(q[1]<bb[1])bb[1]=q[1];if(q[0]>bb[2])bb[2]=q[0];if(q[1]>bb[3])bb[3]=q[1];});});
    var t={i:i,name:r[ix.name],num:r[ix.num]||'',src:r[ix.src],mi:r[ix.mi],uses:r[ix.uses]||'',gain:r[ix.gain],hi:r[ix.hi],lo:r[ix.lo],
      diff:ix.diff!=null?r[ix.diff]:null,line:r[ix.line],parts:parts,bb:bb,ok:true,
      prof:ix.prof!=null?r[ix.prof]:'',link:r[12]||''};   // [12]: your trail's source page
    t.lc=(t.name+' '+t.num).toLowerCase();return t;}
  function mineRows(){return readMine().map(function(m){return[m.name,'','mine',m.mi,'h',m.gain,m.hi,m.lo,m.p,[],null,m.prof||[],m.link||''];});}
  function features(){return{type:'FeatureCollection',features:T.map(function(t){return{type:'Feature',geometry:{type:'MultiLineString',coordinates:t.parts},
    properties:{i:t.i,lc:t.lc,src:t.src,mi:t.mi,gain:t.gain==null?-1:t.gain,hi:t.hi==null?-1:t.hi,
      b:t.uses.indexOf('b')>=0?1:0,r:t.uses.indexOf('r')>=0?1:0,m:t.uses.indexOf('m')>=0?1:0}};})};}
  function build(d){var ix={};d.fields.forEach(function(f,k){ix[f]=k;});
    var rows=d.trails.concat(mineRows());T=rows.map(function(r,i){return trail(r,ix,i);});}
  function addLayers(){
    map.addSource('trl',{type:'geojson',data:features(),tolerance:0.6});
    var before=opt.beforeId,mine=['==',['get','src'],'mine'];   // zoom must be the top-level input
    var w=['interpolate',['linear'],['zoom'],5,['case',mine,0.9,0.5],9,['case',mine,2.1,1.3],14,['case',mine,4.8,3]];
    map.addLayer({id:'trl-line',type:'line',source:'trl',layout:{'line-join':'round','line-cap':'round',visibility:'none'},
      paint:{'line-color':['case',mine,'#6B3FA8','#1F3A52'],'line-width':w,'line-opacity':0.85}},before);
    map.addLayer({id:'trl-hover',type:'line',source:'trl',filter:['==',['get','i'],-1],layout:{'line-join':'round','line-cap':'round',visibility:'none'},
      paint:{'line-color':'#FE5000','line-width':['interpolate',['linear'],['zoom'],5,2,14,5],'line-opacity':0.55}},before);
    map.addLayer({id:'trl-sel-case',type:'line',source:'trl',filter:['==',['get','i'],-1],layout:{'line-join':'round','line-cap':'round',visibility:'none'},
      paint:{'line-color':'#fff','line-width':['interpolate',['linear'],['zoom'],5,4,14,9]}},before);
    map.addLayer({id:'trl-sel',type:'line',source:'trl',filter:['==',['get','i'],-1],layout:{'line-join':'round','line-cap':'round',visibility:'none'},
      paint:{'line-color':'#FE5000','line-width':['interpolate',['linear'],['zoom'],5,2.5,14,5.5]}},before);
    map.addLayer({id:'trl-hit',type:'line',source:'trl',layout:{visibility:'none'},paint:{'line-color':'#000','line-opacity':0,'line-width':14}},before);
    // the spot under the pointer on the selected trail's elevation profile
    map.addSource('trl-pt',{type:'geojson',data:{type:'FeatureCollection',features:[]}});
    map.addLayer({id:'trl-pt',type:'circle',source:'trl-pt',layout:{visibility:'none'},
      paint:{'circle-radius':6,'circle-color':'#FE5000','circle-stroke-color':'#fff','circle-stroke-width':2}});
    map.on('click','trl-hit',function(e){if(on)select(e.features[0].properties.i,false);});
    map.on('moveend',function(){if(on)list();});}
  function vis(v){['trl-line','trl-hover','trl-sel-case','trl-sel','trl-hit','trl-pt'].forEach(function(id){if(map.getLayer(id))map.setLayoutProperty(id,'visibility',v?'visible':'none');});}

  // ---------- filters (the panel's controls; without a panel everything shows) ----------
  function crit(){var c={uses:[]};if(!P)return c;
    c.q=$('ex-q').value.trim().toLowerCase();c.src=$('ex-src').value;
    var l=$('ex-len').value,g=$('ex-gain').value,h=$('ex-hi').value;
    if(l){l=l.split(',');c.len=[+l[0],+l[1]];}if(g){g=g.split(',');c.gain=[+g[0],+g[1]];}if(h)c.hi=+h;
    P.querySelectorAll('.ex-chips [aria-pressed=true]').forEach(function(b){c.uses.push(b.dataset.use);});return c;}
  function match(t,c){
    if(c.q&&t.lc.indexOf(c.q)<0)return false;if(c.src&&t.src!==c.src)return false;
    if(c.len&&(t.mi<c.len[0]||t.mi>=c.len[1]))return false;
    if(c.gain&&(t.gain==null||t.gain<c.gain[0]||t.gain>=c.gain[1]))return false;
    if(c.hi&&(t.hi==null||t.hi<c.hi))return false;
    for(var i=0;i<c.uses.length;i++){var u=c.uses[i];if(u==='nm'){if(t.uses.indexOf('m')>=0)return false;}else if(t.uses.indexOf(u)<0)return false;}
    return true;}
  function mapFilter(c){var f=['all'];
    if(c.q)f.push(['in',c.q,['get','lc']]);if(c.src)f.push(['==',['get','src'],c.src]);
    if(c.len)f.push(['>=',['get','mi'],c.len[0]],['<',['get','mi'],c.len[1]]);
    if(c.gain)f.push(['>=',['get','gain'],c.gain[0]],['<',['get','gain'],c.gain[1]]);
    if(c.hi)f.push(['>=',['get','hi'],c.hi]);
    c.uses.forEach(function(u){f.push(u==='nm'?['==',['get','m'],0]:['==',['get',u],1]);});return f;}
  function apply(){var c=crit(),f=mapFilter(c);['trl-line','trl-hit'].forEach(function(l){if(map.getLayer(l))map.setFilter(l,f);});
    T.forEach(function(t){t.ok=match(t,c);});list();}

  // ---------- the panel: trails in view, the selected trail ----------
  function list(){if(!P||!T.length)return;var b=map.getBounds(),w=b.getWest(),e=b.getEast(),s=b.getSouth(),n=b.getNorth(),all=0,rows=[];
    T.forEach(function(t){if(!t.ok)return;all++;if(t.bb[2]<w||t.bb[0]>e||t.bb[3]<s||t.bb[1]>n)return;rows.push(t);});
    var k=$('ex-sort').value;
    rows.sort(k==='name'?function(a,b){return a.name.localeCompare(b.name);}:function(a,b){return (b[k]==null?-1:b[k])-(a[k]==null?-1:a[k]);});
    $('ex-count').innerHTML='<b>'+all.toLocaleString('en-US')+'</b> match';
    $('ex-inview').innerHTML='<b>'+rows.length.toLocaleString('en-US')+'</b> in view';
    var html=rows.slice(0,150).map(function(t){return '<li data-i="'+t.i+'" class="'+(t.i===sel?'sel ':'')+(t.src==='mine'?'mine':'')+'"><div class="n">'+esc(t.name)+
      (t.num?' <span>#'+esc(t.num)+'</span>':'')+'</div><div class="m">'+meta(t)+' \u00B7 '+AG[t.src]+'</div></li>';}).join('');
    if(rows.length>150)html+='<li class="more">Zoom in to see the other '+(rows.length-150).toLocaleString('en-US')+'</li>';
    if(!rows.length)html='<li class="more">No trails here match. Zoom out or loosen the filters.</li>';
    $('ex-list').innerHTML=html;}
  function select(i,fly){var t=T[i];if(!t)return;sel=i;
    ['trl-sel','trl-sel-case'].forEach(function(l){map.setFilter(l,['==',['get','i'],i]);});
    if(fly)map.fitBounds([[t.bb[0],t.bb[1]],[t.bb[2],t.bb[3]]],{padding:90,maxZoom:14,pitch:50,duration:900});
    if(!P){if(window.openTrailForecast&&opt.onPick)opt.onPick(t);return;}
    var uses=t.uses.split('').filter(function(u){return USE[u];}).map(function(u){return '<span>'+USE[u]+'</span>';}).join('');
    if(t.diff)uses+='<span>'+esc(t.diff)+'</span>';
    var card=$('ex-card');card.className='ex-card'+(t.src==='mine'?' mine':'');
    card.innerHTML='<h3>'+esc(t.name)+'</h3><div class="ex-sub">'+(t.num?'Trail #'+esc(t.num)+' \u00B7 ':'')+AG[t.src]+'</div>'+
      '<div class="ex-stats"><div>Length<b>'+t.mi.toFixed(1)+' mi</b></div><div>Gain<b>'+fmt(t.gain)+'\u2032</b></div>'+
      '<div>High<b>'+fmt(t.hi)+'\u2032</b></div><div>Low<b>'+fmt(t.lo)+'\u2032</b></div></div>'+
      '<div class="ex-prof"></div>'+(uses?'<div class="ex-uses">'+uses+'</div>':'')+
      '<div class="ex-acts"><button class="ex-go" type="button">Forecast this trail</button>'+
      (t.src==='mine'?(t.link?'<a target="_blank" rel="noopener" href="'+esc(t.link)+'">'+(/onxmaps\.com/.test(t.link)?'onX':'AllTrails')+' \u2197</a>':'')+
        '<button class="ex-del" type="button">Remove</button>':'<a target="_blank" rel="noopener" href="'+allTrails(t.bb)+'">Nearby hikes on AllTrails \u2197</a>')+'</div>';
    card.hidden=false;
    chart(card.querySelector('.ex-prof'),t);
    card.querySelector('.ex-go').onclick=function(){if(window.openTrailForecast)window.openTrailForecast(t.line,t.name+(t.num&&t.name.indexOf(t.num)<0?' (#'+t.num+')':''),t.link||'');};
    var del=card.querySelector('.ex-del');if(del)del.onclick=function(){removeMine(t);};
    P.querySelectorAll('.ex-list li.sel').forEach(function(li){li.classList.remove('sel');});
    var li=P.querySelector('.ex-list li[data-i="'+i+'"]');if(li)li.classList.add('sel');}
  // ---------- the selected trail's elevation profile, each mile coloured by its average grade ----------
  function lineMiles(p){var m=0;for(var i=1;i<p.length;i++){var a=p[i-1],b=p[i],la=(a[1]+b[1])/2*Math.PI/180;
    m+=Math.hypot((b[0]-a[0])*Math.cos(la),b[1]-a[1])*69.093;}return m;}
  function pointAt(p,mi){var m=0;for(var i=1;i<p.length;i++){var a=p[i-1],b=p[i],la=(a[1]+b[1])/2*Math.PI/180,
    s=Math.hypot((b[0]-a[0])*Math.cos(la),b[1]-a[1])*69.093;if(m+s>=mi){var f=s?(mi-m)/s:0;return[a[0]+(b[0]-a[0])*f,a[1]+(b[1]-a[1])*f];}m+=s;}
    return p[p.length-1];}
  function chart(el,t){var e=typeof t.prof==='string'?(t.prof?decodeInts(t.prof):[]):(t.prof||[]);
    if(e.length<2){el.innerHTML='';return;}
    var n=e.length,L=lineMiles(t.parts[0]),xs=e.map(function(_,i){return i===n-1?L:Math.min(i*0.1,L);});
    var W=300,H=128,pl=36,pr=6,pt=14,pb=16,lo=Math.min.apply(null,e),hi=Math.max.apply(null,e),span=Math.max(hi-lo,80);
    var y0=lo-span*0.04,y1=hi+span*0.14,X=function(v){return pl+v/L*(W-pl-pr);},Y=function(v){return H-pb-(v-y0)/(y1-y0)*(H-pt-pb);};
    var segs=[],svg='';
    for(var m=0;m*10<n-1;m++){var i0=m*10,i1=Math.min((m+1)*10,n-1),dx=xs[i1]-xs[i0];if(dx<0.05)continue;
      var g=(e[i1]-e[i0])/(dx*5280)*100,c=gradeOf(g),poly=[X(xs[i0])+','+(H-pb)];
      for(var i=i0;i<=i1;i++)poly.push(X(xs[i]).toFixed(1)+','+Y(e[i]).toFixed(1));poly.push(X(xs[i1])+','+(H-pb));
      segs.push({m:m,i0:i0,i1:i1,g:g,c:c});
      svg+='<polygon points="'+poly.join(' ')+'" fill="'+(c?c.c:'#E7E9EE')+'" stroke="#fff" stroke-width="1"/>';}
    svg+='<path d="M'+e.map(function(v,i){return X(xs[i]).toFixed(1)+','+Y(v).toFixed(1);}).join('L')+'" fill="none" stroke="#1F2937" stroke-width="1.4" stroke-linejoin="round"/>';
    segs.forEach(function(s){if(!s.c||X(xs[s.i1])-X(xs[s.i0])<20)return;var top=Math.max.apply(null,e.slice(s.i0,s.i1+1));
      svg+='<text x="'+((X(xs[s.i0])+X(xs[s.i1]))/2).toFixed(1)+'" y="'+(Y(top)-4).toFixed(1)+'" text-anchor="middle">'+Math.round(s.g)+'%</text>';});
    svg+='<line x1="'+pl+'" x2="'+(W-pr)+'" y1="'+(H-pb)+'" y2="'+(H-pb)+'" stroke="#D5D8DE"/>'+
      '<text class="ax" x="'+(pl-4)+'" y="'+(Y(hi)+3).toFixed(1)+'" text-anchor="end">'+fmt(hi)+'′</text>'+
      '<text class="ax" x="'+(pl-4)+'" y="'+(Y(lo)+3).toFixed(1)+'" text-anchor="end">'+fmt(lo)+'′</text>'+
      '<text class="ax" x="'+pl+'" y="'+(H-3)+'">0</text><text class="ax" x="'+(W-pr)+'" y="'+(H-3)+'" text-anchor="end">'+L.toFixed(1)+' mi</text>'+
      '<line class="cur" x1="0" x2="0" y1="'+pt+'" y2="'+(H-pb)+'" stroke="#111" stroke-width="1" visibility="hidden"/>'+
      '<circle class="cur" r="3.5" fill="#FE5000" stroke="#fff" stroke-width="1.5" visibility="hidden"/>'+
      '<rect x="'+pl+'" y="0" width="'+(W-pl-pr)+'" height="'+H+'" fill="transparent"/>';
    el.innerHTML='<div class="ex-prof-t">Elevation profile <span>from the low end · grade per mile of climb</span></div>'+
      '<svg viewBox="0 0 '+W+' '+H+'" role="img" aria-label="Elevation profile of '+esc(t.name)+'">'+svg+'</svg>'+
      '<div class="ex-ptip" hidden></div><div class="ex-leg">'+GR.map(function(g){return '<span><i style="background:'+g.c+'"></i>'+g.k+' <em>'+g.r+'</em></span>';}).join('')+'</div>';
    var sv=el.querySelector('svg'),line=sv.querySelector('line.cur'),dot=sv.querySelector('circle.cur'),tipEl=el.querySelector('.ex-ptip');
    function at(ev){var r=sv.getBoundingClientRect(),vx=(ev.clientX-r.left)/r.width*W,mi=Math.max(0,Math.min(L,(vx-pl)/(W-pl-pr)*L));
      var i=Math.min(n-1,Math.round(mi/0.1)),v=e[i],s=segs.find(function(s){return i>=s.i0&&i<=s.i1&&(i<s.i1||s===segs[segs.length-1]);});
      line.setAttribute('x1',X(xs[i]));line.setAttribute('x2',X(xs[i]));dot.setAttribute('cx',X(xs[i]));dot.setAttribute('cy',Y(v));
      line.setAttribute('visibility','visible');dot.setAttribute('visibility','visible');
      tipEl.innerHTML='<b>'+xs[i].toFixed(1)+' mi</b> · '+fmt(v)+'′'+(s?' · mile '+(s.m+1)+': '+(s.g>=0?'':'−')+Math.abs(Math.round(s.g))+'% '+(s.c?s.c.k.toLowerCase():s.g<0?'downhill':'flat'):'');
      tipEl.hidden=false;
      var p=pointAt(t.parts[0],xs[i]);if(map.getSource('trl-pt'))map.getSource('trl-pt').setData({type:'Point',coordinates:p});}
    function off(){line.setAttribute('visibility','hidden');dot.setAttribute('visibility','hidden');tipEl.hidden=true;
      if(map.getSource('trl-pt'))map.getSource('trl-pt').setData({type:'FeatureCollection',features:[]});}
    sv.addEventListener('pointermove',at);sv.addEventListener('pointerleave',off);}
  function refresh(){loadData().then(function(d){build(d);map.getSource('trl').setData(features());apply();});}

  // ---------- your GPX trails (this browser only) ----------
  async function addGPX(file){var lab=P.querySelector('.ex-add'),keep=lab.lastChild.textContent;
    lab.classList.add('busy');lab.lastChild.textContent=' Reading '+file.name+'\u2026';
    try{var W=window.WxTrail;if(!W)throw new Error('the trail engine isn\u2019t loaded');
      var g=W.parseGPX(await file.text()),name=g.name||file.name.replace(/\.gpx$/i,'');
      var pts=await W.fillElevation(g.pts.map(function(p){return{lat:p.lat,lon:p.lon,ele:p.ele};})),st=W.trailStats(pts);
      var step=Math.max(1,Math.ceil(g.pts.length/1500)),keepPts=g.pts.filter(function(p,i){return i%step===0||i===g.pts.length-1;});
      var mine=readMine();mine.push({name:name,p:encode(keepPts),mi:Math.round(st.km*0.621371*10)/10,gain:Math.round(st.gain*3.28084),
        hi:Math.round(pts[st.hi].ele*3.28084),lo:Math.round(pts[st.lo].ele*3.28084),prof:W.profile?W.profile(pts):[],added:Date.now()});
      if(!writeMine(mine))throw new Error('this browser won\u2019t store it (private window or storage full)');
      loadData().then(function(d){build(d);map.getSource('trl').setData(features());apply();select(T.length-1,true);});
    }catch(e){lab.lastChild.textContent=' Couldn\u2019t add it: '+(e.message||e);setTimeout(function(){lab.lastChild.textContent=keep;},5000);lab.classList.remove('busy');return;}
    lab.classList.remove('busy');lab.lastChild.textContent=keep;}
  function removeMine(t){var mine=readMine(),k=mine.findIndex(function(m){return m.p===t.line&&(m.link||'')===t.link;});if(k<0)return;
    mine.splice(k,1);writeMine(mine);sel=-1;$('ex-card').hidden=true;
    ['trl-sel','trl-sel-case'].forEach(function(l){map.setFilter(l,['==',['get','i'],-1]);});refresh();}

  if(P){
    P.querySelectorAll('select').forEach(function(s){s.addEventListener('change',function(){if(!T.length)return;if(s.id==='ex-sort')list();else apply();});});
    var qt;$('ex-q').addEventListener('input',function(){clearTimeout(qt);qt=setTimeout(function(){if(T.length)apply();},200);});
    P.querySelectorAll('.ex-chips button').forEach(function(b){b.addEventListener('click',function(){
      b.setAttribute('aria-pressed',b.getAttribute('aria-pressed')==='true'?'false':'true');if(T.length)apply();});});
    $('ex-list').addEventListener('click',function(e){var li=e.target.closest('li[data-i]');if(li)select(+li.dataset.i,true);});
    $('ex-gpx').addEventListener('change',function(){var f=this.files[0];this.value='';if(f)addGPX(f);});
  }

  return{
    show:function(v){on=v;if(P){P.hidden=!v;P.parentNode.classList.toggle('trl',v);setTimeout(function(){map.resize();},0);}
      if(v&&!ready)ready=loadData().then(function(d){build(d);addLayers();apply();}).catch(function(){if(P)$('ex-count').textContent='Trail data isn\u2019t available in this build.';});
      if(ready)ready.then(function(){vis(on);if(on)list();});
      if(!v&&tip)tip.hidden=true;},
    // the map's mousemove: a trail under the pointer gets the tip (return true), else the weather does
    hover:function(e){if(!on||!map.getLayer('trl-hit'))return false;
      var f=map.queryRenderedFeatures(e.point,{layers:['trl-hit']})[0];
      if(!f){if(hov!==-1){hov=-1;map.setFilter('trl-hover',['==',['get','i'],-1]);map.getCanvas().style.cursor='';}return false;}
      var t=T[f.properties.i];if(hov!==t.i){hov=t.i;map.setFilter('trl-hover',['==',['get','i'],t.i]);}
      map.getCanvas().style.cursor='pointer';
      if(tip){tip.innerHTML='<b>'+esc(t.name)+'</b> \u00B7 '+meta(t);tip.style.left=e.point.x+'px';tip.style.top=e.point.y+'px';tip.hidden=false;}
      return true;}
  };
};
})();
"""


if __name__ == "__main__":
    from dotenv import load_dotenv
    load_dotenv()
    n = build(os.path.join(ROOT, "docs"), os.environ.get("MAPBOX_TOKEN"))
    print(f"{n} trails")
