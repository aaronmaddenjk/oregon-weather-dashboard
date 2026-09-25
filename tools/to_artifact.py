"""Turn docs/index.html into an artifact-friendly preview page.

Usage: python tools/to_artifact.py docs/index.html <out.html>

The artifact host supplies its own <html>/<head>/<body> skeleton and blocks
api.mapbox.com (and every other outside host), so: unwrap the document, drop the
Mapbox tags (they can't load there anyway), and give it a stable title. Maps,
cameras, the video poster's live data and the Trail Forecast tab only work in the
real dashboard (localhost or GitHub Pages), not in the preview.
"""
import re, sys

src, dst = sys.argv[1], sys.argv[2]
s = open(src, encoding="utf-8").read()
s = re.sub(r"<!DOCTYPE html>\s*", "", s, flags=re.I)
s = re.sub(r"</?html>|</?head>|</body>", "", s)
s = re.sub(r"<body[^>]*>", "", s)
s = re.sub(r'<link href="https://api\.mapbox\.com[^>]*>', "", s)
s = re.sub(r'<script src="https://api\.mapbox\.com[^>]*></script>', "", s)
s = re.sub(r"<title>.*?</title>", "", s, count=1, flags=re.S)
s = ('<title>Oregon Weather Dashboard</title>\n'
     '<style>body{margin:0;padding:0;background:#FAFAFA;color:#111;}</style>\n' + s)
open(dst, "w", encoding="utf-8").write(s)
print(f"wrote {dst} ({len(s)/1024:.0f} KB)")
