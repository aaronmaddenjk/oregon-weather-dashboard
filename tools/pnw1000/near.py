"""List OSM's named places near a spot, to fix a hike whose place names didn't match.

  python tools/pnw1000/near.py <section> <lat> <lon> [radius_m] [word]
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import build   # noqa: E402

sid, lat, lon = sys.argv[1], float(sys.argv[2]), float(sys.argv[3])
radius = float(sys.argv[4]) if len(sys.argv) > 4 else 1500
word = build.norm(sys.argv[5]) if len(sys.argv) > 5 else ""
if os.path.exists(os.path.join(build.CACHE, f"osm-{sid}.json")):
    data = build.osm(sid)
else:   # the cluster download that covers the spot, else a fresh one around it
    data = None
    for f in os.listdir(build.CACHE):
        if f.startswith("osmb-"):
            w, s, e, n = map(float, f[5:-5].split("_"))
            if w <= lon <= e and s <= lat <= n:
                data = build.osm_box((w, s, e, n))
                break
    if data is None:
        p = build.PAD
        data = build.osm_box((round(lon - p, 2), round(lat - p, 2), round(lon + p, 2), round(lat + p, 2)))
net = build.Net(data)
rows = sorted((build.m((lon, lat), p), raw, kind, p) for nn, raw, p, kind in net.places
              if build.m((lon, lat), p) <= radius and (not word or word in nn))
for d, raw, kind, p in rows[:40]:
    print(f"{d:6.0f} m  {raw}  [{kind}]  {p[1]:.5f},{p[0]:.5f}")
