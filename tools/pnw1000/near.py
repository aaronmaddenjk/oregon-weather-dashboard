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
net = build.Net(build.osm(sid))
rows = sorted((build.m((lon, lat), p), raw, kind, p) for nn, raw, p, kind in net.places
              if build.m((lon, lat), p) <= radius and (not word or word in nn))
for d, raw, kind, p in rows[:40]:
    print(f"{d:6.0f} m  {raw}  [{kind}]  {p[1]:.5f},{p[0]:.5f}")
