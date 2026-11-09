"""PNW 1000: the owner's own list of the best Oregon/Washington hikes, routed on OpenStreetMap
(owner, 2026-11-08: "get away from AllTrails"). Hike lists live in tools/pnw1000/hikes/<section>.json
(written by Claude: names, rough locations, type, rough miles, a one-line blurb in our own words).

For each hike: every named place must match an OSM feature by name near its rough location (nothing is
guessed); the route follows OSM trails / tracks / roads between them (roads cost more, so they're used
only to connect); loops avoid reusing their outbound trail; length is checked against the rough miles.
Elevation as in trail_explorer (Mapbox terrain-RGB z12, cached tiles, 100 m smoothing).

  python tools/pnw1000/build.py or-columbia-gorge-west [--compare trails.json]

Writes .cache/pnw1000/<section>.json (routes) and .cache/pnw1000/gpx/<id>.gpx, prints a report.
OSM data (c) OpenStreetMap contributors, ODbL - credit it wherever routes are shown.
"""
import argparse
import heapq
import json
import math
import os
import re
import sys
import time
from collections import defaultdict

import requests

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)
import sections          # noqa: E402
import trail_explorer as te   # noqa: E402  (elevation helpers)

CACHE = os.path.join(ROOT, ".cache", "pnw1000")
HIKES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "hikes")
UA = {"User-Agent": "oregon-weather-dashboard pnw1000 (personal hiking list)"}

# cost per metre by way type: trails first, roads only to connect pieces
COST = {"path": 1.0, "footway": 1.0, "steps": 1.0, "bridleway": 1.0, "cycleway": 1.1, "pedestrian": 1.1,
        "track": 1.25, "service": 2.5, "unclassified": 3.0, "residential": 3.0, "tertiary": 4.0, "secondary": 8.0}
TRAILHEADISH = ("trailhead", "parking")
GOALISH = ("peak", "waterfall", "viewpoint", "water", "saddle", "volcano", "glacier", "cliff", "spring", "arch", "rock")


def section_box(sid):
    for st, n, b in sections.SECTIONS:
        if st.lower() + "-" + re.sub(r"[^a-z0-9]+", "-", n.lower()).strip("-") == sid:
            return b
    raise SystemExit(f"no section {sid}")


def osm(sid):
    path = os.path.join(CACHE, f"osm-{sid}.json")
    if os.path.exists(path) and time.time() - os.path.getmtime(path) < 30 * 86400:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    w, s, e, n = section_box(sid)
    bb = f"{s - .05},{w - .05},{n + .05},{e + .05}"
    q = (f'[out:json][timeout:240];'
         f'way["highway"~"^({"|".join(COST)})$"]({bb})->.w; .w out geom;'
         f'(node["name"]({bb}); way["name"]["natural"]({bb}); way["name"]["amenity"="parking"]({bb});'
         f' way["name"]["highway"="trailhead"]({bb}); way["name"]["waterway"="waterfall"]({bb});'
         f' way["name"]["tourism"]({bb}); way["name"]["leisure"]({bb}););out center;')
    r = requests.post("https://overpass-api.de/api/interpreter", data={"data": q}, headers=UA, timeout=300)
    r.raise_for_status()
    os.makedirs(CACHE, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(r.text)
    return r.json()


def m(a, b):   # metres between (lon, lat) points
    la = math.radians((a[1] + b[1]) / 2)
    return math.hypot((b[0] - a[0]) * 111320 * math.cos(la), (b[1] - a[1]) * 111320)


def norm(s):
    s = s.lower().replace("&", " and ").replace("’", "'")
    s = re.sub(r"\bmount\b", "mt", s)
    s = re.sub(r"\bsaint\b", "st", s)
    s = re.sub(r"[^a-z0-9 ]+", "", s.replace("'", ""))
    return " ".join(s.split())


class Net:
    def __init__(self, data):
        self.xy, self.adj, self.places = {}, defaultdict(list), []
        for el in data["elements"]:
            t = el.get("tags", {})
            if el["type"] == "way" and "geometry" in el and t.get("highway") in COST:
                if t.get("access") in ("no", "private") and t.get("foot") not in ("yes", "designated", "permissive"):
                    continue
                if t.get("foot") == "no" or t.get("area") == "yes":
                    continue
                c = COST[t["highway"]]
                for nid, g in zip(el["nodes"], el["geometry"]):
                    self.xy[nid] = (g["lon"], g["lat"])
                for a, b in zip(el["nodes"], el["nodes"][1:]):
                    d = m(self.xy[a], self.xy[b])
                    self.adj[a].append((b, d * c, d))
                    self.adj[b].append((a, d * c, d))
            if t.get("name") and (el["type"] == "node" or "center" in el):
                p = (el["lon"], el["lat"]) if el["type"] == "node" else (el["center"]["lon"], el["center"]["lat"])
                kind = t.get("highway") or t.get("amenity") or t.get("natural") or t.get("waterway") or t.get("tourism") or t.get("leisure") or ""
                self.places.append((norm(t["name"]), t["name"], p, kind))
        self.grid = defaultdict(list)   # 0.01 deg cells -> graph nodes, for snapping
        for nid, p in self.xy.items():
            if nid in self.adj:
                self.grid[(int(p[0] * 100), int(p[1] * 100))].append(nid)

    def find(self, name, lat, lon, start=False, radius=5000):   # rough locations can be a few km off
        """the OSM feature matching `name` near (lat, lon) -> (name, (lon, lat), kind) or None"""
        q = norm(name)
        qt = set(q.split())
        best = None
        for nn, raw, p, kind in self.places:
            d = m((lon, lat), p)
            if d > radius:
                continue
            if nn == q:
                sc = 3
            elif q in nn:
                sc = 2
            elif nn in q and len(nn) >= 4:
                sc = 1.5
            else:
                nt = set(nn.split())
                ov = len(qt & nt) / max(1, len(qt | nt))
                if ov < 0.6:
                    continue
                sc = ov
            if start and kind in TRAILHEADISH:
                sc += 0.6
            if not start and kind in GOALISH:   # a summit / falls / lake, not a sign named after it
                sc += 0.6
            key = (sc, -d)
            if best is None or key > best[0]:
                best = (key, raw, p, kind)
        return best and best[1:]

    def snap(self, p, k=12, limit=450):
        """the nearest graph nodes to p (lon, lat), closest first, within `limit` metres"""
        cx, cy = int(p[0] * 100), int(p[1] * 100)
        cand = []
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                for nid in self.grid.get((cx + dx, cy + dy), ()):
                    d = m(p, self.xy[nid])
                    if d <= limit:
                        cand.append((d, nid))
        return [nid for d, nid in sorted(cand)[:k]], (sorted(cand)[0][0] if cand else None)

    def path(self, a, b, extra):
        """cheapest path a -> b; `extra` = {edge: multiplier} (loops avoid their outbound trail)"""
        dist, prev, pq = {a: 0.0}, {}, [(0.0, a)]
        while pq:
            c, u = heapq.heappop(pq)
            if u == b:
                break
            if c > dist.get(u, 1e18):
                continue
            for v, w, _ in self.adj[u]:
                w *= extra.get((u, v), 1.0)
                if c + w < dist.get(v, 1e18):
                    dist[v], prev[v] = c + w, u
                    heapq.heappush(pq, (c + w, v))
        if b not in dist:
            return None
        out = [b]
        while out[-1] != a:
            out.append(prev[out[-1]])
        return out[::-1]


def route(net, hike):
    """-> (line [(lon, lat)], notes, places) or (None, reason, places)"""
    stops = [("start", hike["start"])] + [("via", v) for v in hike.get("via", [])]
    if hike["type"] == "loop":
        stops.append(("end", hike["start"]))
    else:
        stops.append(("to", hike["to"]))
    found, nodes, notes = [], [], []
    for role, (name, lat, lon) in stops:
        f = net.find(name, lat, lon, start=role in ("start", "end"))
        if not f:
            return None, f"no OSM place named like '{name}' near there", found
        cand, d = net.snap(f[1])
        if not cand:
            return None, f"'{f[0]}' is more than 450 m from any trail", found
        found.append((role, name, f[0], f[2], round(d)))
        nodes.append(cand)
    extra, line, oneway_m = {}, [], 0.0
    for i in range(len(nodes) - 1):
        seg = None
        for a in nodes[i][:4]:
            for b in nodes[i + 1][:4]:
                if a == b:
                    seg = [a]
                    break
                seg = net.path(a, b, extra)
                if seg:
                    break
            if seg:
                break
        if not seg:
            return None, f"no trail connection from '{stops[i][1][0]}' to '{stops[i + 1][1][0]}'", found
        nodes[i + 1] = [seg[-1]]
        for u, v in zip(seg, seg[1:]):
            extra[(u, v)] = extra[(v, u)] = 6.0   # a loop's return leg looks for another way
        pts = [net.xy[n] for n in seg]
        line.extend(pts if not line else pts[1:])
    for a, b in zip(line, line[1:]):
        oneway_m += m(a, b)
    if len(line) < 2 or oneway_m < 100:
        return None, "start and destination landed on the same bit of trail (the destination needs a better place name)", found
    return line, notes, found


def elevation(line, token):
    pts = te._densify([list(p) for p in line])
    z = [te._elev(p[0], p[1], token) for p in pts]
    d = [0.0]
    for i in range(1, len(pts)):
        d.append(d[-1] + te._m(pts[i - 1], pts[i]))
    sm, a, b, s = [], 0, 0, 0.0
    for i in range(len(z)):
        while b < len(z) and d[b] <= d[i] + 50:
            s += z[b]; b += 1
        while d[a] < d[i] - 50:
            s -= z[a]; a += 1
        sm.append(s / (b - a))
    up = sum(max(0, sm[i] - sm[i - 1]) for i in range(1, len(sm)))
    down = sum(max(0, sm[i - 1] - sm[i]) for i in range(1, len(sm)))
    if z[0] > z[-1]:   # profile from the low end, like the dashboard's trails
        d, sm = [d[-1] - x for x in reversed(d)], sm[::-1]
    return {"up": up, "down": down, "lo": min(z), "hi": max(z), "prof": te._sample_profile(d, sm)}


def overlap(a, b, tol=40):
    """share of line a (densified) within tol metres of line b"""
    A = te._densify([list(p) for p in a], 20)
    B = te._densify([list(p) for p in b], 20)
    grid = defaultdict(list)
    for p in B:
        grid[(int(p[0] * 500), int(p[1] * 500))].append(p)
    hit = 0
    for p in A:
        cx, cy = int(p[0] * 500), int(p[1] * 500)
        if any(m(p, q) <= tol for dx in (-1, 0, 1) for dy in (-1, 0, 1) for q in grid.get((cx + dx, cy + dy), ())):
            hit += 1
    return hit / max(1, len(A))


def decode(s):
    i = lat = lng = 0
    out = []
    while i < len(s):
        for k in range(2):
            sh = v = 0
            while True:
                b = ord(s[i]) - 63; i += 1; v |= (b & 31) << sh; sh += 5
                if b < 32:
                    break
            dv = ~(v >> 1) if v & 1 else v >> 1
            if k:
                lng += dv
            else:
                lat += dv
        out.append((lng / 1e5, lat / 1e5))
    return out


def best_saved(hike, saved):
    qt = set(norm(hike["name"]).split()) - {"via", "and", "trail", "loop", "the", "to"}
    best = None
    for t in saved:
        nt = set(norm(t.get("name", "")).split()) - {"via", "and", "trail", "loop", "the", "to"}
        if not nt:
            continue
        ov = len(qt & nt) / len(qt | nt)
        if ov >= 0.5 and (best is None or ov > best[0]):
            best = (ov, t)
    return best and best[1]


def gpx(h, line):
    pts = "".join(f'<trkpt lat="{la:.6f}" lon="{lo:.6f}"/>' for lo, la in line)
    name = h["name"].replace("&", "&amp;").replace("<", "&lt;")
    return ('<?xml version="1.0" encoding="UTF-8"?>\n<gpx version="1.1" creator="PNW 1000 (OSM data (c) OpenStreetMap contributors, ODbL)" '
            'xmlns="http://www.topografix.com/GPX/1/1">\n'
            f'<trk><name>{name}</name><trkseg>{pts}</trkseg></trk>\n</gpx>\n')


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("section")
    ap.add_argument("--compare", help="your trails.json, to score against your saved routes")
    args = ap.parse_args()
    from dotenv import load_dotenv
    load_dotenv(os.path.join(ROOT, ".env"))
    token = os.environ.get("MAPBOX_TOKEN")
    with open(os.path.join(HIKES, args.section + ".json"), encoding="utf-8") as f:
        spec = json.load(f)
    net = Net(osm(args.section))
    saved = []
    if args.compare:
        with open(args.compare, encoding="utf-8-sig") as f:
            saved = json.load(f)
    os.makedirs(os.path.join(CACHE, "gpx"), exist_ok=True)
    out, rows = [], []
    for h in spec["hikes"]:
        line, why, found = route(net, h)
        if not line:
            rows.append((h, None, why, found, None))
            continue
        one = sum(m(a, b) for a, b in zip(line, line[1:])) / 1609.344
        mi = one * (2 if h["type"] == "out" else 1)
        ev = elevation(line, token)
        gain = ev["up"] + ev["down"] if h["type"] == "out" else (ev["up"] if line[0] == line[-1] else max(ev["up"], ev["down"]))
        ratio = mi / h["mi"] if h.get("mi") else 1
        flag = "" if 0.65 <= ratio <= 1.5 else f"length {mi:.1f} mi vs ~{h['mi']} expected"
        cmp = None
        s = best_saved(h, saved)
        if s:
            sl = decode(s["p"])
            cmp = (s["name"], overlap(line, sl), overlap(sl, line))
        rec = {"id": h["id"], "name": h["name"], "section": args.section, "rank": h.get("rank"), "type": h["type"],
               "blurb": h.get("blurb", ""), "mi": round(mi, 1), "gain": round(gain * 3.28084), "hi": round(ev["hi"] * 3.28084),
               "lo": round(ev["lo"] * 3.28084), "p": te._encode(line), "prof": ev["prof"], "src": "osm", "flag": flag}
        out.append(rec)
        with open(os.path.join(CACHE, "gpx", h["id"] + ".gpx"), "w", encoding="utf-8") as f:
            f.write(gpx(h, line))
        rows.append((h, rec, flag, found, cmp))
    with open(os.path.join(CACHE, args.section + ".json"), "w", encoding="utf-8") as f:
        json.dump(out, f, separators=(",", ":"))
    ok = sum(1 for r in rows if r[1])
    print(f"{args.section}: {ok} of {len(rows)} routed\n")
    for h, rec, note, found, cmp in rows:
        if not rec:
            print(f"  FAILED  {h['name']}: {note}")
            continue
        c = f"  vs your '{cmp[0]}': {cmp[1]:.0%} of this on yours, {cmp[2]:.0%} of yours on this" if cmp else ""
        print(f"  {'CHECK ' if note else 'ok    '}{h['name'][:44]:44} {rec['mi']:5.1f} mi (~{h['mi']}) {rec['gain']:6,}' gain{c}{'  <- ' + note if note else ''}")
    print("\nplaces matched (role: asked for -> OSM name [kind], metres to the nearest trail):")
    for h, rec, note, found, cmp in rows:
        print(f"  {h['id']}: " + "; ".join(f"{r}: {a} -> {b} [{k}] {d} m" for r, a, b, k, d in found))


if __name__ == "__main__":
    main()
