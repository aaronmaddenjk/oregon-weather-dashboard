"""Find more PNW 1000 hikes from OpenStreetMap itself: notable destinations (peaks, waterfalls, lakes, viewpoints,
lookouts, arches, hot springs... that carry a Wikipedia / Wikidata link in OSM) reachable mostly on trail from a
named trailhead or parking area. Each one is routed like build.py; the ones that work are printed (and saved) as
ready-to-paste hike entries, for picking by hand into hikes/<section>.json.

  python tools/pnw1000/candidates.py <section> [--max 40]

Writes .cache/pnw1000/cand-<section>.json. Destinations already in the section's list are skipped.
"""
import argparse
import json
import math
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import build   # noqa: E402

KINDS = {"peak": "summit", "volcano": "summit", "waterfall": "falls", "water": "lake", "viewpoint": "viewpoint",
         "spring": "spring", "hot_spring": "hot spring", "arch": "arch", "glacier": "glacier", "cave_entrance": "cave",
         "saddle": "pass", "cliff": "cliff", "tower": "lookout", "beach": "beach", "rock": "rock"}


def local_notable(t, el):
    """the same picks as the Overpass query in notable()"""
    if not t.get("name") or el["type"] == "way" and "center" not in el:
        return False
    nat, wiki = t.get("natural"), bool(t.get("wikidata"))
    if wiki and nat in ("peak", "volcano", "water", "spring", "hot_spring", "arch", "glacier", "cave_entrance", "saddle", "cliff", "beach", "rock"):
        return True
    if t.get("waterway") == "waterfall" or t.get("tourism") == "viewpoint" or nat == "hot_spring":
        return True
    if t.get("man_made") == "tower" and t.get("tower:type") == "observation" and "lookout" in t["name"].lower():
        return True
    if nat in ("peak", "volcano") and el["type"] == "node":
        return True
    return nat == "water" and t.get("water") == "lake"


START_OK = ("trail", "park", "day use", "sno", "camp", "picnic", "lot", "parking", "overlook", "viewpoint", "access",
            "staging", "lake", "falls", "beach", "wayside", "area", "recreation", "pass", "summit", "lodge", "visitor")


def start_ok(name, kind):
    """a start people would recognise: a trailhead, or a car park whose name says what it is (not 'U.S. Coast Guard')"""
    n = name.lower()
    if any(w in n for w in ("garage", "overflow", "station", "school", "store", "hospital", "park and ride", "park & ride")):
        return False
    return kind == "trailhead" or any(w in n for w in START_OK)


def notable(sid):
    """named features with a wikipedia/wikidata tag in the section box (one small Overpass query, cached)"""
    w, s, e, n = build.section_box(sid)
    path = os.path.join(build.CACHE, f"notable2-{sid}.json")   # v2: also plain named peaks, lakes, viewpoints
    if build.osmlocal.available():   # local tiles: this section's own state only (no Washington beaches in Oregon)
        els = [el for el in build.osmlocal.elements(w, s, e, n, states=(sid[:2],)) if local_notable(el.get("tags", {}), el)]
    elif not os.path.exists(path):
        bb = f"{s},{w},{n},{e}"
        q = (f'[out:json][timeout:180];('
             f'nwr["wikidata"]["natural"~"^(peak|volcano|water|spring|hot_spring|arch|glacier|cave_entrance|saddle|cliff|beach|rock)$"]["name"]({bb});'
             f'nwr["wikidata"]["waterway"="waterfall"]["name"]({bb});'
             f'nwr["wikidata"]["tourism"="viewpoint"]["name"]({bb});'
             f'nwr["man_made"="tower"]["tower:type"="observation"]["name"~"[Ll]ookout"]({bb});'
             f'nwr["waterway"="waterfall"]["name"]({bb});'
             f'node["natural"~"^(peak|volcano)$"]["name"]({bb});'
             f'nwr["natural"="water"]["water"="lake"]["name"]({bb});'
             f'nwr["tourism"="viewpoint"]["name"]({bb});'
             f'nwr["natural"="hot_spring"]["name"]({bb});'
             f');out center tags;')
        import requests, time
        for attempt in range(12):
            try:
                r = requests.post("https://overpass-api.de/api/interpreter", data={"data": q}, headers=build.UA, timeout=240)
                if r.status_code in (429, 502, 503, 504):
                    raise requests.HTTPError(str(r.status_code))
                r.raise_for_status()
                break
            except (requests.RequestException, ValueError) as ex:
                if attempt == 11:
                    raise
                print(f"  (Overpass {ex}; retrying)", flush=True)
                time.sleep(min(120, 20 * (attempt + 1)))
        with open(path, "w", encoding="utf-8") as f:
            f.write(r.text)
    if not build.osmlocal.available():
        with open(path, encoding="utf-8") as f:
            els = json.load(f)["elements"]
    out = []
    for el in els:
        t = el.get("tags", {})
        p = (el["lon"], el["lat"]) if "lon" in el else (el["center"]["lon"], el["center"]["lat"]) if "center" in el else None
        if not p or not t.get("name"):
            continue
        kind = t.get("natural") or t.get("waterway") or t.get("tourism") or t.get("man_made") or ""
        if kind == "water" and t.get("water") in ("reservoir", "river", "canal", "pond", "wastewater", "basin"):
            continue
        score = 3 if t.get("wikipedia") else 2 if t.get("wikidata") else 1   # plain named places count a little
        out.append({"name": t["name"], "kind": kind, "p": p, "score": score, "ele": t.get("ele")})
    return out


def used_points(sid):
    """destinations already in the hand list (and their routed lines' ends)"""
    pts = []
    f = os.path.join(build.HIKES, sid + ".json")
    if os.path.exists(f):
        with open(f, encoding="utf-8") as fh:
            for h in json.load(fh)["hikes"]:
                for pl in [h.get("to")] + h.get("via", []):
                    if pl:
                        pts.append((pl[2], pl[1]))
    r = os.path.join(build.CACHE, sid + ".json")
    if os.path.exists(r):
        with open(r, encoding="utf-8") as fh:
            for h in json.load(fh):
                line = build.decode(h["p"])
                pts.extend([line[0], line[-1], line[len(line) // 2]])
    return pts


def trail_share(net, seg):
    tot = trail = 0.0
    for u, v in zip(seg, seg[1:]):
        for b, w, d in net.adj[u]:
            if b == v:
                tot += d
                if w <= d * 1.26:   # path / footway / track
                    trail += d
                break
    return trail / tot if tot else 0, tot


def main():
    if os.name == "nt":   # keep the PC from sleeping while this runs (ES_CONTINUOUS | ES_SYSTEM_REQUIRED), like publish.ps1
        import ctypes
        ctypes.windll.kernel32.SetThreadExecutionState(0x80000001)
    ap = argparse.ArgumentParser()
    ap.add_argument("section")
    ap.add_argument("--max", type=int, default=40)
    a = ap.parse_args()
    from dotenv import load_dotenv
    load_dotenv(os.path.join(build.ROOT, ".env"))
    token = os.environ.get("MAPBOX_TOKEN")
    cands = notable(a.section)
    used = used_points(a.section)
    cands = [c for c in cands if all(build.m(c["p"], u) > 600 for u in used)]
    seen, uniq = set(), []
    for c in sorted(cands, key=lambda c: -c["score"]):
        k = build.norm(c["name"])
        if k in seen:
            continue
        seen.add(k)
        uniq.append(c)
    print(f"{a.section}: {len(uniq)} notable places not in the list yet", flush=True)
    uniq = uniq[:a.max * 3]   # the most notable first; many won't route
    build.PAD = 0.08          # room to find a trailhead up to ~8 km away
    groups = build.clusters([{"id": i, "start": [c["name"], c["p"][1], c["p"][0]]} for i, c in enumerate(uniq)])
    print(f"  {len(groups)} OSM downloads", flush=True)
    found = []
    for box, hs in groups:
        net = build.Net(build.osm_box(box))
        for h in hs:
            c = uniq[h["id"]]
            r = route_one(net, c, token)
            if r:
                found.append(r)
                print(f"  {c['score']} {c['name'][:40]:40} [{r['kind']}] from {r['start'][0][:34]:34} {r['mi']:5.1f} mi rt {r['gain']:6,}' gain, top {r['hi']:,}'", flush=True)
    found.sort(key=lambda r: (-r["score"], r["mi"]))
    with open(os.path.join(build.CACHE, f"cand-{a.section}.json"), "w", encoding="utf-8") as f:
        json.dump(found[:a.max], f, indent=0)
    print(f"{len(found)} routable; kept {min(len(found), a.max)}")


def route_one(net, c, token):
    lon, lat = c["p"]
    goal, gd = net.snap(c["p"], limit=500)
    if not goal:
        return None
    starts = sorted(((build.m(c["p"], pp), raw, pp, kind) for nn, raw, pp, kind in net.places
                     if kind in ("trailhead", "parking") and 400 < build.m(c["p"], pp) < 9000))[:6]
    best = None
    for _, raw, pp, kind in starts:
        if not start_ok(raw, kind):
            continue
        sn, sd = net.snap(pp, limit=200)
        if not sn:
            continue
        seg = net.path(sn[0], goal[0], {})
        if not seg:
            continue
        share, metres = trail_share(net, seg)
        if share < 0.7 or not (400 < metres < 15000):
            continue
        if best is None or metres < best[0]:
            best = (metres, raw, pp, seg, share)
    if not best:
        return None
    metres, sname, spp, seg, share = best
    line = [net.xy[x] for x in seg]
    ev = build.elevation(line, token)
    return {"name": c["name"], "kind": KINDS.get(c["kind"], c["kind"]), "score": c["score"],
            "start": [sname, round(spp[1], 5), round(spp[0], 5)], "to": [c["name"], round(lat, 5), round(lon, 5)],
            "mi": round(2 * metres / 1609.344, 1), "gain": round((ev["up"] + ev["down"]) * 3.28084),
            "hi": round(ev["hi"] * 3.28084), "lo": round(ev["lo"] * 3.28084), "trail": round(share, 2)}


if __name__ == "__main__":
    main()
