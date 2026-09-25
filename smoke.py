"""
Smoke forecast for the Map tab: NOAA's NDGD smoke guidance (the National Air Quality
Forecast Capability, HRRR-Smoke based), served hourly on a ~2.7 km grid by the NWS map
services - the same two fields OpenSnow shows:

  sfc   near-surface smoke, the air you breathe (served in kg/m3 -> ug/m3)
  vert  vertically integrated smoke, all the smoke overhead: haze, dim sun, red sunsets
        (served in kg/m2 -> mg/m2). It can be thick aloft while the ground air is clean.

build(bounds, out_dir) fetches one raster per hour per field (exportImage, raw float32, in
Web Mercator so it lines up exactly as a Mapbox image source), quantises it to one byte
on a log scale, and writes grayscale PNGs to out_dir/smoke/. Returns the metadata the page
needs, or None if the service is unavailable.

Byte b <-> value v:  b = round(Q * log2(1 + v)),  v = 2 ** (b / Q) - 1   (Q = 28: 0..~500)
"""
import array
import math
import os
import struct
import time
import zlib
from concurrent.futures import ThreadPoolExecutor

import requests

BASE = "https://mapservices.weather.noaa.gov/raster/rest/services/air_quality/"
SERVICES = {"sfc": ("ndgd_smoke_sfc_1hr_avg_time", 1e9), "vert": ("ndgd_smoke_vert_1hr_avg_time", 1e6)}
Q = 28
WIDTH = 300          # ~2.8 km per pixel across the region - about the model's own grid
MAX_HOURS = 48
WORKERS = 8          # rasters fetched at once
R_EARTH = 6378137.0


def _mx(lon):
    return R_EARTH * math.radians(lon)


def _my(lat):
    return R_EARTH * math.log(math.tan(math.pi / 4 + math.radians(lat) / 2))


def _png_gray(w, h, data):
    """Minimal 8-bit grayscale PNG (stdlib only)."""
    def chunk(tag, body):
        return struct.pack(">I", len(body)) + tag + body + struct.pack(">I", zlib.crc32(tag + body) & 0xffffffff)
    rows = b"".join(b"\x00" + bytes(data[y * w:(y + 1) * w]) for y in range(h))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 0, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(rows, 9)) + chunk(b"IEND", b""))


def build(bounds, out_dir):
    W, S, E, N = bounds
    x0, x1, y0, y1 = _mx(W), _mx(E), _my(S), _my(N)
    w, h = WIDTH, round(WIDTH * (y1 - y0) / (x1 - x0))
    os.makedirs(os.path.join(out_dir, "smoke"), exist_ok=True)
    try:
        # both fields come from the same model run; use the hours both have, from now on
        ext = []
        for svc, _ in SERVICES.values():
            te = requests.get(BASE + svc + "/ImageServer", params={"f": "json"}, timeout=60).json()["timeInfo"]["timeExtent"]
            ext.append(te)
        start = max(max(e[0] for e in ext), (int(time.time()) // 3600) * 3600 * 1000)
        end = min(e[1] for e in ext)
        times = list(range(start, end + 1, 3600 * 1000))[:MAX_HOURS]
    except Exception as exc:
        print(f"  WARNING: smoke forecast unavailable ({exc})")
        return None
    def frame(job):
        # one field at one hour -> (kind, k, published path or None, peak value)
        kind, k, t = job
        svc, scale = SERVICES[kind]
        name = f"smoke/{kind}_{k:02d}.png"
        try:
            r = requests.get(BASE + svc + "/ImageServer/exportImage", params={
                "bbox": f"{x0},{y0},{x1},{y1}", "bboxSR": 3857, "imageSR": 3857, "size": f"{w},{h}",
                "format": "bip", "pixelType": "F32", "interpolation": "RSP_BilinearInterpolation",
                "time": t, "f": "image"}, timeout=120)
            a = array.array("f")
            a.frombytes(r.content[:w * h * 4])   # raw float32 pixels; a validity bitmask follows
            if len(a) != w * h:
                raise ValueError(f"got {len(r.content)} bytes")
        except Exception as exc:
            print(f"  WARNING: smoke {kind} hour {k} failed ({exc})")
            return kind, k, None, 0.0
        q, top = bytearray(w * h), 0.0
        for i, v in enumerate(a):
            if v > 0 and v == v:   # skip nodata / NaN
                x = v * scale
                top = max(top, x)
                q[i] = min(255, round(Q * math.log2(1 + x)))
        with open(os.path.join(out_dir, name), "wb") as f:
            f.write(_png_gray(w, h, q))
        return kind, k, name, top

    # each raster takes the service a few seconds, so fetch several at once
    jobs = [(kind, k, t) for kind in SERVICES for k, t in enumerate(times)]
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        done = list(ex.map(frame, jobs))
    files = {kind: [None] * len(times) for kind in SERVICES}
    peak = {kind: 0.0 for kind in SERVICES}
    for kind, k, name, top in done:
        files[kind][k] = name
        peak[kind] = max(peak[kind], top)
    print(f"  smoke: {len(times)} hours, peak surface {peak['sfc']:.0f} ug/m3, column {peak['vert']:.0f} mg/m2")
    return {"times": times, "w": w, "h": h, "q": Q, "files": files,
            "coords": [[W, N], [E, N], [E, S], [W, S]], "bounds": [W, S, E, N]}
