"""
Mountain webcams for the Trails tab.

Most come from the USGS AshCam catalog (volcview.wr.usgs.gov), which gathers the NPS, USGS,
ski-area and highway cameras that watch the Cascade volcanoes into one API: a stable
current-image URL per camera plus a timestamped image history with a day/night flag. The
page asks that API for the newest photo when the Cameras view opens (CORS is open), so
images and times are live, not from the build. Cameras the catalog doesn't carry (NPS
Hurricane Ridge for Olympus) are plain image URLs.

CAMS maps a mountain uid to its cameras, best first: (AshCam code or {"url": ...}, short
label, one-line description). build() drops USGS cameras that have gone quiet (no image in
MAX_QUIET_DAYS), so dead cameras never show up.
"""
import time

import requests

API = "https://volcview.wr.usgs.gov/ashcam-api/webcamApi/webcams"
MAX_QUIET_DAYS = 3

CAMS = {
    "mr2": [("rainier-mountain", "Paradise", "From Paradise, looking up the south side"),
            ("rainier-mowich-face", "Mowich Face", "USGS research camera, close on the northwest face"),
            ("rainier-sunrise", "Sunrise", "From Sunrise, the northeast side"),
            ("rainier-crystalpana", "Crystal Mtn", "From the top of Crystal Mountain")],
    "ma2": [("adams-WATCH", "From the northwest", "USGS research camera; Adams is on the far skyline")],
    "msh2": [("msh-edifice", "Johnston Ridge", "From Johnston Ridge, looking into the crater"),
             ("msh-dome", "Lava dome", "Johnston Ridge camera zoomed on the lava dome"),
             ("msh-GUAC", "Crater", "USGS camera inside the crater")],
    "mh2": [("hood-palmer", "Palmer", "From Timberline's Palmer lift, looking at the summit"),
            ("hood-govtcamp", "Government Camp", "From Government Camp, looking north"),
            ("hood-mhm-heather", "Meadows", "From Mt. Hood Meadows, the southeast side")],
    "mbk": [("baker-cascadia", "From the west", "Distant view across the lowlands; Baker is on the skyline when it's clear")],
    "ss2": [("threesis-bachelor", "From Mt. Bachelor", "Looking north to the Three Sisters"),
            ("threesis-blackbutte", "From Black Butte", "Looking south to the Three Sisters")],
    "mb2": [("threesis-bachelor", "West Village", "From the base, looking toward the Three Sisters")],
    "cl2": [("crater-sinnott", "Rim Village", "From Sinnott Overlook, across the lake")],
    "mt": [("crater-sinnott", "From Crater Lake", "From Crater Lake's rim; Thielsen is the spire on the far skyline")],
    "mo": [({"url": "https://www.nps.gov/webcams-olym/southcam.jpg",
             "page": "https://www.nps.gov/olym/learn/photosmultimedia/hurricane-ridge-webcam.htm",
             "note": "Live NPS camera \u00b7 updates about every 15 min"},
            "Hurricane Ridge", "Looking south toward Mount Olympus and the Bailey Range")],
}


_built = None


def build():
    """{uid: [{code or url, label, desc, page}]} for mountains with a working camera
    (worked out once per run; the Trails and Mt Hood pages both ask)."""
    global _built
    if _built is not None:
        return _built
    live = None
    try:
        now = time.time()
        live = {c["webcamCode"] for c in requests.get(API, timeout=60).json()["webcams"]
                if now - (c.get("lastImageTimestamp") or 0) < MAX_QUIET_DAYS * 86400}
    except Exception as exc:
        print(f"  WARNING: webcam catalog unavailable ({exc}) - keeping every camera")
    out = {}
    for uid, cams in CAMS.items():
        keep = []
        for src, label, desc in cams:
            if isinstance(src, dict):
                keep.append({"url": src["url"], "page": src.get("page"), "label": label, "desc": desc,
                             "note": src.get("note", "Live camera")})
            elif live is None or src in live:
                keep.append({"code": src, "label": label, "desc": desc,
                             "page": f"https://volcview.wr.usgs.gov/ashcam-gui/webcam.html?webcam={src}"})
        if keep:
            out[uid] = keep
    print(f"  webcams: {sum(len(v) for v in out.values())} cameras on {len(out)} mountains")
    _built = out
    return out
