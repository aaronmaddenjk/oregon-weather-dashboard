"""Add picked candidates (candidates.py output) to a section's hike list, with factual one-line descriptions,
then route just those.

  python tools/pnw1000/adopt.py <section> [--max N]   # takes the best N candidates (default all kept)
"""
import json
import os
import re
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import build   # noqa: E402


def dumps(d):   # one hike per line, like fix.py
    rest = {k: v for k, v in d.items() if k != "hikes"}
    head = json.dumps(rest, ensure_ascii=False)[:-1]
    NL = chr(10)
    return head + (', ' if rest else '') + '"hikes": [' + NL + ("," + NL).join("  " + json.dumps(h, ensure_ascii=False) for h in d["hikes"]) + NL + "]}" + NL

WHAT = {"summit": "the top of {n}", "falls": "{n}", "lake": "{n}", "viewpoint": "the {n} viewpoint", "lookout": "{n}",
        "spring": "{n}", "hot spring": "{n}", "arch": "{n}", "pass": "{n}", "glacier": "the edge of {n}",
        "beach": "{n}", "cliff": "{n}", "rock": "{n}", "cave": "{n}"}


def worth(c):
    """the 'good hikes only' filter (owner, 2026-11-10): real climbs, falls, lookouts and lakes you walk to, not every
    forest butte. gain = round-trip climbing in feet, score 3 = has a Wikipedia article."""
    k, mi, gain, score = c["kind"], c["mi"], c["gain"], c["score"]
    if c.get("trail", 1) < 0.8:
        return False
    if k == "falls":
        return 0.5 <= mi <= 16
    if not 0.8 <= mi <= 16:
        return False
    if k in ("lookout", "hot spring", "arch", "glacier", "cave"):
        return True
    if k in ("summit", "viewpoint") and score < 3 and mi > 12:
        return False   # a long haul to a hill nobody has written about
    if k == "summit":
        return (gain >= 1500 and gain / mi >= 200) or (score == 3 and gain >= 800)
    if k == "lake":
        return mi >= 2 and (gain >= 500 or score == 3)
    if k == "viewpoint":
        return gain >= 600 or score == 3
    return score == 3 or gain >= 1200


def quality(c):
    bonus = {"falls": 700, "lookout": 700, "hot spring": 700, "arch": 700, "glacier": 700, "lake": 300}.get(c["kind"], 0)
    return c["score"] * 800 + min(c["gain"], 3500) / 3.5 + bonus


def blurb(c):
    kind = "falls" if c["kind"] == "viewpoint" and any(w in c["name"].lower() for w in ("viewpoint", "overlook", "lookout", "vista")) else c["kind"]
    what = WHAT.get(kind, "{n}").format(n=c["name"])
    top = f" ({c['hi']:,} ft)" if c["kind"] == "summit" else ""
    climb = f", with about {round(c['gain'] / 50) * 50:,} feet of climbing" if c["gain"] >= 300 else ""
    art = "an" if str(c["mi"]).startswith(("8", "11", "18")) and not str(c["mi"]).startswith(("80", "110")) else "a"
    return f"From {c['start'][0]}, {art} {c['mi']}-mile round trip to {what}{top}{climb}."


def main():
    sid = sys.argv[1]
    mx = int(sys.argv[sys.argv.index("--max") + 1]) if "--max" in sys.argv else 999
    with open(os.path.join(build.CACHE, f"cand-{sid}.json"), encoding="utf-8") as f:
        cands = json.load(f)
    path = os.path.join(HERE, "hikes", sid + ".json")
    d = json.load(open(path, encoding="utf-8")) if os.path.exists(path) else {"section": sid, "hikes": []}
    ids = {h["id"] for h in d["hikes"]}
    names = {build.norm(h["name"]) for h in d["hikes"]}
    rank = max([h.get("rank") or 0 for h in d["hikes"]] + [0])
    new = []
    taken = []   # destinations already in any section's list (no hike twice)
    for g in os.listdir(os.path.join(HERE, "hikes")):
        r = os.path.join(build.CACHE, g)   # routed lines: their real ends, not my rough coordinates
        if os.path.exists(r):
            for h in json.load(open(r, encoding="utf-8")):
                taken.append(build.decode(h["p"])[-1])
    cands = [c for c in cands if all(build.m((c["to"][2], c["to"][1]), t) > 600 for t in taken)]
    good = sorted([c for c in cands if worth(c)], key=quality, reverse=True)
    kept = []
    for c in good:
        if len(kept) >= mx:
            break
        if build.norm(c["name"]) in names:
            continue
        here = (c["to"][2], c["to"][1])
        if any(build.m(here, k) <= 600 for k in kept):
            continue
        kept.append(here)
        i = re.sub(r"[^a-z0-9]+", "-", c["name"].lower()).strip("-")
        while i in ids:
            i += "-2"
        rank += 1
        ids.add(i)
        names.add(build.norm(c["name"]))
        new.append({"id": i, "name": c["name"], "type": "out", "rank": rank, "start": c["start"], "to": c["to"], "mi": c["mi"],
                    "blurb": blurb(c), "src": "osm-candidate"})
    d["hikes"].extend(new)
    with open(path, "w", encoding="utf-8") as f:
        f.write(dumps(d))
    print(f"{sid}: added {len(new)}")
    if new:
        r = subprocess.run([sys.executable, os.path.join(HERE, "build.py"), sid, "--only", ",".join(h["id"] for h in new)],
                           capture_output=True, text=True, encoding="utf-8")
        print("\n".join(l for l in r.stdout.splitlines() if "routed" in l or "FAILED" in l), r.stderr[-500:])


if __name__ == "__main__":
    main()
