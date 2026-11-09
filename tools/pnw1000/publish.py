"""Send the routed PNW 1000 hikes (.cache/pnw1000/<section>.json from build.py) to the private wx-trails repo
as pnw1000.json, where the dashboard reads them with your GitHub key (window.WxP1K). Uses the gh CLI's login.

  python tools/pnw1000/publish.py            # all sections built so far
  python tools/pnw1000/publish.py --dry-run  # just count
"""
import base64
import glob
import json
import os
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
CACHE = os.path.join(ROOT, ".cache", "pnw1000")
REPO, PATH = "aaronmaddenjk/wx-trails", "pnw1000.json"


def gh(*args, input_file=None):
    cmd = ["gh", "api", *args] + (["--input", input_file] if input_file else [])
    return subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8")


def main():
    hikes = []
    for f in sorted(glob.glob(os.path.join(CACHE, "*.json"))):
        if os.path.basename(f).startswith("osm-"):
            continue
        with open(f, encoding="utf-8") as fh:
            hikes.extend(json.load(fh))
    hikes.sort(key=lambda h: (h["section"], h.get("rank") or 999))
    for h in hikes:
        h.pop("flag", None)   # build-time notes stay out of the published list
    print(f"{len(hikes)} hikes from {len({h['section'] for h in hikes})} sections")
    if "--dry-run" in sys.argv:
        return
    body = json.dumps(hikes, separators=(",", ":"), ensure_ascii=False)
    cur = gh(f"repos/{REPO}/contents/{PATH}", "--jq", ".sha")
    put = {"message": f"PNW 1000: {len(hikes)} hikes", "content": base64.b64encode(body.encode()).decode(), "branch": "main"}
    if cur.returncode == 0 and cur.stdout.strip():
        put["sha"] = cur.stdout.strip()
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8") as tf:
        json.dump(put, tf)
    try:
        r = gh("-X", "PUT", f"repos/{REPO}/contents/{PATH}", input_file=tf.name)
    finally:
        os.unlink(tf.name)
    if r.returncode:
        raise SystemExit("GitHub refused it: " + (r.stderr or r.stdout)[:300])
    print(f"published {len(body) / 1024:.0f} KB to {REPO}/{PATH}")


if __name__ == "__main__":
    main()
