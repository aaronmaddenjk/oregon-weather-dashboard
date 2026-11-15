"""Local OpenStreetMap for PNW 1000: the Geofabrik Oregon / Washington extracts cut into 0.1-degree tiles, answering
the same questions build.py and candidates.py used to send to the public Overpass API (which blocked us after a few
GB of downloads, 2026-11-11). No rate limits, nothing asked of anyone's server.

  python tools/pnw1000/osmlocal.py prepare ~/osm/oregon-latest.osm.pbf or       # needs pyosmium; ~30 min each
  python tools/pnw1000/osmlocal.py prepare ~/osm/washington-latest.osm.pbf wa

Tiles live in $PNW_OSM_TILES (default .cache/osmlocal)/<state>/<ix>_<iy>.jsonl, one element per line in Overpass's
JSON shape: highway ways with geometry, named nodes, named features as ways/relations with a centre.
OSM data (c) OpenStreetMap contributors, ODbL.
"""
import json
import math
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
TILES = os.environ.get("PNW_OSM_TILES") or os.path.join(ROOT, ".cache", "osmlocal")
STEP = 0.1
HIGHWAYS = {"path", "footway", "steps", "bridleway", "cycleway", "pedestrian", "track", "service", "unclassified",
            "residential", "tertiary", "secondary"}   # build.COST
KEEP = ("name", "natural", "waterway", "tourism", "man_made", "tower:type", "water", "wikidata", "wikipedia", "highway",
        "amenity", "leisure", "ele", "access", "foot", "area")


def tile(lon, lat):
    return math.floor(lon / STEP), math.floor(lat / STEP)


def available():
    return os.path.isdir(os.path.join(TILES, "or")) or os.path.isdir(os.path.join(TILES, "wa"))


def _tags(t):
    return {k: t[k] for k in KEEP if k in t}


def prepare(pbf, state):
    import osmium

    out = os.path.join(TILES, state + ".tmp")
    os.makedirs(out, exist_ok=True)
    buf, size = {}, [0]

    def put(key, el):
        buf.setdefault(key, []).append(json.dumps(el, separators=(",", ":")))
        size[0] += 1
        if size[0] >= 300000:
            flush()

    def flush():
        for (ix, iy), lines in buf.items():
            with open(os.path.join(out, f"{ix}_{iy}.jsonl"), "a", encoding="utf-8") as f:
                f.write("\n".join(lines) + "\n")
        buf.clear()
        size[0] = 0

    def named_feature(t):
        return "name" in t and (t.get("natural") or t.get("amenity") == "parking" or t.get("highway") == "trailhead"
                                or t.get("waterway") == "waterfall" or t.get("tourism") or t.get("leisure"))

    class H(osmium.SimpleHandler):
        def node(self, n):
            if "name" in n.tags:
                lon, lat = round(n.location.lon, 7), round(n.location.lat, 7)
                put(tile(lon, lat), {"type": "node", "id": n.id, "lon": lon, "lat": lat, "tags": _tags(n.tags)})

        def way(self, w):
            t = w.tags
            try:
                pts = [(round(nd.lon, 7), round(nd.lat, 7)) for nd in w.nodes]
            except osmium.InvalidLocationError:
                return
            if not pts:
                return
            if t.get("highway") in HIGHWAYS:
                el = {"type": "way", "id": w.id, "nodes": [nd.ref for nd in w.nodes],
                      "geometry": [{"lat": la, "lon": lo} for lo, la in pts], "tags": _tags(t)}
                for key in {tile(lo, la) for lo, la in pts}:   # a way crossing tiles goes in each (deduped on read)
                    put(key, el)
            elif named_feature(t):
                lo = sum(p[0] for p in pts) / len(pts)
                la = sum(p[1] for p in pts) / len(pts)
                put(tile(lo, la), {"type": "way", "id": w.id, "center": {"lat": round(la, 7), "lon": round(lo, 7)}, "tags": _tags(t)})

        def area(self, a):   # multipolygon relations (big lakes); closed ways were handled in way()
            if a.from_way() or "name" not in a.tags or not a.tags.get("natural"):
                return
            pts = [(nd.lon, nd.lat) for ring in a.outer_rings() for nd in ring if nd.location.valid()]
            if not pts:
                return
            lo = sum(p[0] for p in pts) / len(pts)
            la = sum(p[1] for p in pts) / len(pts)
            put(tile(lo, la), {"type": "relation", "id": a.orig_id(), "center": {"lat": round(la, 7), "lon": round(lo, 7)},
                               "tags": _tags(a.tags)})

    H().apply_file(pbf, locations=True, idx="flex_mem")
    flush()
    final = os.path.join(TILES, state)
    if os.path.isdir(final):
        import shutil
        shutil.rmtree(final)
    os.rename(out, final)
    print(state, len(os.listdir(final)), "tiles")


def _inside(el, w, s, e, n):
    if "lon" in el:
        return w <= el["lon"] <= e and s <= el["lat"] <= n
    if "center" in el:
        c = el["center"]
        return w <= c["lon"] <= e and s <= c["lat"] <= n
    return any(w <= g["lon"] <= e and s <= g["lat"] <= n for g in el.get("geometry", ()))


def elements(w, s, e, n, states=("or", "wa")):
    """everything in the box, like an Overpass answer's 'elements'"""
    out, seen = [], set()
    x0, y0 = tile(w, s)
    x1, y1 = tile(e, n)
    for st in states:
        for ix in range(x0, x1 + 1):
            for iy in range(y0, y1 + 1):
                p = os.path.join(TILES, st, f"{ix}_{iy}.jsonl")
                if not os.path.exists(p):
                    continue
                with open(p, encoding="utf-8") as f:
                    for line in f:
                        el = json.loads(line)
                        k = (el["type"], el["id"], "c" if "center" in el else "")
                        if k in seen or not _inside(el, w, s, e, n):
                            continue
                        seen.add(k)
                        out.append(el)
    return out


def query(w, s, e, n):
    return {"elements": elements(w, s, e, n)}


if __name__ == "__main__":
    if len(sys.argv) == 4 and sys.argv[1] == "prepare":
        prepare(sys.argv[2], sys.argv[3])
    else:
        print(__doc__)
