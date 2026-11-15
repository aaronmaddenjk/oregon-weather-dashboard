"""Drop adopted candidates (src osm-candidate) whose routed destination is within 600 m of another hike's
(hand-picked hikes win; among candidates the first one kept wins)."""
import glob
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import build   # noqa: E402
from adopt import dumps   # noqa: E402

routed = {}
for f in glob.glob(os.path.join(HERE, "hikes", "*.json")):
    sid = os.path.basename(f)[:-5]
    r = os.path.join(build.CACHE, sid + ".json")
    if os.path.exists(r):
        routed[sid] = json.load(open(r, encoding="utf-8"))
src = {}
for sid in routed:
    for h in json.load(open(os.path.join(HERE, "hikes", sid + ".json"), encoding="utf-8"))["hikes"]:
        src[(sid, h["id"])] = h.get("src")
ends = [(build.decode(h["p"])[-1], sid, h["id"]) for sid, hs in routed.items() for h in hs if src.get((sid, h["id"])) != "osm-candidate"]
drop = set()
for sid, hs in sorted(routed.items()):
    for h in hs:
        if src.get((sid, h["id"])) != "osm-candidate":
            continue
        e = build.decode(h["p"])[-1]
        if any(build.m(e, x[0]) <= 600 for x in ends):
            drop.add((sid, h["id"]))
        else:
            ends.append((e, sid, h["id"]))
for sid in {s for s, _ in drop}:
    ids = {i for s, i in drop if s == sid}
    f = os.path.join(HERE, "hikes", sid + ".json")
    d = json.load(open(f, encoding="utf-8"))
    d["hikes"] = [h for h in d["hikes"] if h["id"] not in ids]
    open(f, "w", encoding="utf-8").write(dumps(d))
    json.dump([h for h in routed[sid] if h["id"] not in ids], open(os.path.join(build.CACHE, sid + ".json"), "w", encoding="utf-8"), separators=(",", ":"))
    print(sid, "dropped", sorted(ids))
