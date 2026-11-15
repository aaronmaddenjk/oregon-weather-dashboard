"""Patch hikes in a section's list, then reroute just those.

  python tools/pnw1000/fix.py <section> '{"<id>": {"to": ["Name", lat, lon], ...}, "<id2>": null}'

A null drops the hike. A key set to null is removed (e.g. "via": null).
"""
import json
import os
import subprocess
import sys

def dumps(d):   # one hike per line, like the hand-written lists
    rest = {k: v for k, v in d.items() if k != "hikes"}
    head = json.dumps(rest, ensure_ascii=False)[:-1]
    NL = chr(10)
    return head + (', ' if rest else '') + '"hikes": [' + NL + ("," + NL).join("  " + json.dumps(h, ensure_ascii=False) for h in d["hikes"]) + NL + "]}" + NL


HERE = os.path.dirname(os.path.abspath(__file__))
sid, patch = sys.argv[1], json.loads(sys.argv[2])
f = os.path.join(HERE, "hikes", sid + ".json")
with open(f, encoding="utf-8") as fh:
    d = json.load(fh)
ids = {h["id"] for h in d["hikes"]}
for k in patch:
    if k not in ids and not (isinstance(patch[k], dict) and patch[k].get("new")):
        raise SystemExit(f"no hike {k}")
keep = []
for h in d["hikes"]:
    p = patch.get(h["id"], {})
    if p is None:
        continue
    for k, v in p.items():
        if v is None:
            h.pop(k, None)
        else:
            h[k] = v
    keep.append(h)
d["hikes"] = keep
with open(f, "w", encoding="utf-8") as fh:
    fh.write(dumps(d))
redo = [k for k, v in patch.items() if v is not None]
dropped = [k for k, v in patch.items() if v is None]
if dropped:   # take them out of the routed file too
    out = os.path.join(HERE, "..", "..", ".cache", "pnw1000", sid + ".json")
    if os.path.exists(out):
        with open(out, encoding="utf-8") as fh:
            r = [x for x in json.load(fh) if x["id"] not in dropped]
        with open(out, "w", encoding="utf-8") as fh:
            json.dump(r, fh, separators=(",", ":"))
if redo:
    res = subprocess.run([sys.executable, os.path.join(HERE, "build.py"), sid, "--only", ",".join(redo)], capture_output=True, text=True, encoding="utf-8")
    print("\n".join(l for l in res.stdout.splitlines()[1:] if l.strip() and not l.startswith("places matched")), res.stderr[-800:])
