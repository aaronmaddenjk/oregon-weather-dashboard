"""
Region-wide forecast fields for the Map tab (Oregon + Washington).

The region is covered by a regular grid of CELL_KM cells (25 km by default; set
WX_REGION_KM=60 for a cheap development grid). For every land cell we build a
vertical profile at LEVELS_M elevations, per day:

  temperature  near-surface NBM at the cell's own elevation, joined to the GFS
               free-atmosphere temperatures at 850/700/600/500 hPa, so real lapse
               rates and inversions carry through (standard lapse below the surface)
  new snow     the SNOTEL-tuned multi-model precipitation blend x the wet-bulb snow
               fraction x snow-to-liquid ratio, at each level's hourly temperature
               and humidity (snow_model)
  wind gusts   NBM surface gusts near the ground, rising to the GFS free-air wind x
               GUST_FACTOR at ridge heights - what exposed terrain feels

The browser then paints each pixel of the map from the cells around it, at that
pixel's real (30 m DEM) elevation, so freezing lines and snow lines follow the
terrain even though the forecast grid is coarse. Precipitation doesn't vary with
elevation inside a cell - windward/leeward detail finer than a cell is lost.

Quota: ~4 Open-Meteo calls per land cell per fresh build (the land mask is cached
permanently in verification/region_cells.json since terrain doesn't change).
"""
import base64
import json
import math
import os
from array import array
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from zoneinfo import ZoneInfo

import requests

import nws
import snow_model

BOUNDS = (-124.8, 41.9, -116.4, 49.0)          # W, S, E, N - Oregon + Washington
CELL_KM = float(os.environ.get("WX_REGION_KM", 25))
LEVELS_M = list(range(0, 4401, 200))            # 23 levels, sea level to 14,400 ft
DAYS = 7
BATCH = 50                                       # locations per request
OM = "https://api.open-meteo.com/v1/forecast"
TZ = "America/Los_Angeles"
PL = (850, 700, 600, 500)                        # GFS pressure levels (~5k-18k ft)
GUST_FACTOR = 1.25                               # free-air wind -> gusts on exposed ridges
STD_LAPSE_F_PER_M = 6.5 * 1.8 / 1000
CELLS_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "verification", "region_cells.json")


def grid():
    w, s, e, n = BOUNDS
    dlat = CELL_KM / 111.0
    dlon = CELL_KM / (111.0 * math.cos(math.radians((s + n) / 2)))
    lats = [round(n - j * dlat, 4) for j in range(int((n - s) / dlat) + 1)]
    lons = [round(w + i * dlon, 4) for i in range(int((e - w) / dlon) + 1)]
    return lats, lons, dlat, dlon


def _batched(points, params, workers=4):
    """GET params for many (lat, lon) points in BATCH-sized requests, in order."""
    chunks = [points[k:k + BATCH] for k in range(0, len(points), BATCH)]

    def one(chunk):
        d = requests.get(OM, params={**params, "latitude": ",".join(str(p[0]) for p in chunk),
                                     "longitude": ",".join(str(p[1]) for p in chunk)}, timeout=180).json()
        if isinstance(d, dict) and d.get("error"):
            raise RuntimeError(d.get("reason", "Open-Meteo error"))
        return d if isinstance(d, list) else [d]

    with ThreadPoolExecutor(max_workers=workers) as ex:
        return [loc for part in ex.map(one, chunks) for loc in part]


def land_cells():
    """Grid geometry + which points are land (cached forever: terrain doesn't change)."""
    lats, lons, dlat, dlon = grid()
    key = {"bounds": BOUNDS, "km": CELL_KM}
    try:
        with open(CELLS_PATH, encoding="utf-8") as f:
            cached = json.load(f)
        if cached.get("key") == json.loads(json.dumps(key)):
            return lats, lons, dlat, dlon, cached["land"]
    except (OSError, ValueError):
        pass
    pts = [(la, lo) for la in lats for lo in lons]
    elev = []
    for k in range(0, len(pts), 100):
        chunk = pts[k:k + 100]
        elev += requests.get("https://api.open-meteo.com/v1/elevation", params={
            "latitude": ",".join(str(p[0]) for p in chunk),
            "longitude": ",".join(str(p[1]) for p in chunk)}, timeout=60).json()["elevation"]
    land = [i for i, e in enumerate(elev) if e is not None and e > 0]
    os.makedirs(os.path.dirname(CELLS_PATH), exist_ok=True)
    with open(CELLS_PATH, "w", encoding="utf-8") as f:
        json.dump({"key": key, "land": land}, f)
    return lats, lons, dlat, dlon, land


def _interp(anchors, z, below):
    """Linear in elevation between (z, value) anchors; `below(z0, v0, z)` extrapolates
    under the lowest anchor, and values above the top anchor hold constant."""
    if not anchors:
        return None
    if z <= anchors[0][0]:
        return below(anchors[0][0], anchors[0][1], z)
    for (z0, v0), (z1, v1) in zip(anchors, anchors[1:]):
        if z <= z1:
            return v0 + (v1 - v0) * (z - z0) / (z1 - z0)
    return anchors[-1][1]


FRAME_HOURS = (2, 5, 8, 11, 14, 17, 20, 23)   # 3-hourly map frames = the forecast tables' columns


def vertical_profile(times, H, z_s, G, precip_fn, levels, probes=()):
    """The one forecast-by-elevation calculation shared by the Map tab and the
    mountain 3D maps. Hour by hour, builds the vertical profile at one spot:
      - temperature: NBM at the surface (z_s) joined to GFS upper-air levels,
        standard lapse below the surface
      - humidity: the same, for the wet-bulb rain/snow split
      - gusts: NBM surface gust, rising to GFS free-air wind x GUST_FACTOR aloft
    and evaluates it at every elevation in `levels`. precip_fn(i, z) gives the
    liquid precipitation (in) for hour i at elevation z.

    Returns per day (DAYS of them): high, new snow, max gust and the precip-weighted
    snow fraction at each level, plus FRAME_HOURS snapshots (temperature and gust at
    that hour, snow accumulated since midnight). For each elevation in `probes` it also
    returns hourly {time: sustained wind} and {time: gust} (the forecast tables' wind)."""
    by_day = OrderedDict()
    for i, t in enumerate(times):
        by_day.setdefault(t[:10], []).append(i)
    days = list(by_day.items())[:DAYS]
    L = len(levels)
    lapse_below = lambda z0, v0, z: v0 + STD_LAPSE_F_PER_M * (z0 - z)
    hold_below = lambda z0, v0, z: v0
    out = {"labels": [], "dates": [], "hi": [], "snow": [], "gust": [], "sfrac": [],
           "f_temp": [], "f_gust": [], "f_snow": [],
           "probe_wind": [{} for _ in probes], "probe_gust": [{} for _ in probes],
           "probe_temp": [{} for _ in probes], "probe_rh": [{} for _ in probes]}
    for di, (date, ix) in enumerate(days):
        hi, gust, snow = [-999.0] * L, [0.0] * L, [0.0] * L
        wsum, psum = [0.0] * L, [0.0] * L
        f_temp, f_gust, f_snow = [], [], []
        for i in ix:
            Ts, RHs, Gs = H["temperature_2m"][i], H["relative_humidity_2m"][i], H["wind_gusts_10m"][i]
            if Ts is None:
                continue
            aT, aR = [(z_s, Ts)], [(z_s, RHs if RHs is not None else 80)]
            fa = []   # free-air wind (mph) at every pressure level, whatever the surface height
            for p in PL:
                Z = G.get(f"geopotential_height_{p}hPa", [None] * (i + 1))[i]
                if Z is None:
                    continue
                W = G.get(f"wind_speed_{p}hPa", [None] * (i + 1))[i]
                if W is not None:
                    fa.append((Z, W))
                if Z <= z_s + 50:
                    continue
                T = G.get(f"temperature_{p}hPa", [None] * (i + 1))[i]
                R = G.get(f"relative_humidity_{p}hPa", [None] * (i + 1))[i]
                if T is not None:
                    aT.append((Z, T))
                if R is not None:
                    aR.append((Z, R))
            if len(aT) == 1:   # no upper-air data this hour: standard lapse above too
                aT.append((z_s + 4000, Ts - STD_LAPSE_F_PER_M * 4000))
            fa.sort()
            Ws = (H.get("wind_speed_10m") or [None] * (i + 1))[i] or 0

            # Wind: Open-Meteo moves NBM's temperature to the real elevation but not its
            # wind, which belongs to NBM's smoothed (lower) terrain. Exposed terrain at
            # height z feels at least the free-air wind there, so take the stronger of
            # the surface model and the free-air wind - phased in from ~500 m below the
            # lowest (850 hPa) level so valleys keep their sheltered surface winds.
            def wind_at(z, surface, factor):
                if not fa:
                    return surface
                exposure = min(1.0, max(0.0, (z - (fa[0][0] - 500)) / 500))
                return max(surface, exposure * _interp(fa, z, hold_below) * factor)

            for k, z in enumerate(probes):
                out["probe_wind"][k][times[i]] = wind_at(z, Ws, 1.0)
                out["probe_gust"][k][times[i]] = wind_at(z, Gs or 0, GUST_FACTOR)
                out["probe_temp"][k][times[i]] = _interp(aT, z, lapse_below)
                out["probe_rh"][k][times[i]] = _interp(aR, z, hold_below)
            temps, gusts = [], []
            for li, z in enumerate(levels):
                T = _interp(aT, z, lapse_below)
                W = wind_at(z, Gs or 0, GUST_FACTOR)
                temps.append(T)
                gusts.append(W)
                hi[li] = max(hi[li], T)
                gust[li] = max(gust[li], W)
                p = precip_fn(i, z)
                if p > 0:
                    s, f = snow_model.new_snow_in(p, T, _interp(aR, z, hold_below))
                    snow[li] += s
                    wsum[li] += p * f
                    psum[li] += p
            if int(times[i][11:13]) in FRAME_HOURS:
                f_temp.append(temps)
                f_gust.append(gusts)
                f_snow.append(list(snow))
        while len(f_temp) < len(FRAME_HOURS):   # a short final day: repeat its last frame
            f_temp.append(f_temp[-1] if f_temp else [0.0] * L)
            f_gust.append(f_gust[-1] if f_gust else [0.0] * L)
            f_snow.append(f_snow[-1] if f_snow else [0.0] * L)
        dt = datetime.strptime(date, "%Y-%m-%d")
        out["labels"].append("Today" if di == 0 else f"{dt:%a} {dt.day}")
        out["dates"].append(date)
        for k, v in (("hi", hi), ("snow", snow), ("gust", gust), ("f_temp", f_temp), ("f_gust", f_gust), ("f_snow", f_snow)):
            out[k].append(v)
        out["sfrac"].append([w / p if p > 0 else None for w, p in zip(wsum, psum)])
    return out


def point_inputs(lat, lon, days=DAYS, nws_point=None):
    """Surface + GFS upper-air hourly data at one spot (for the mountains).
    With `nws_point` (from nws.point) the surface is the NWS grid box: its temperature,
    humidity and wind at the box's elevation, the forecasters' numbers. Hours the NWS
    doesn't cover fall back to NBM, moved to the box elevation with a standard lapse rate.
    Without it, the surface is NBM at the spot itself."""
    base = {"latitude": lat, "longitude": lon, "timezone": TZ, "forecast_days": days,
            "temperature_unit": "fahrenheit", "wind_speed_unit": "mph"}
    nb = requests.get(OM, params={**base, "models": "ncep_nbm_conus",
                                  "hourly": "temperature_2m,relative_humidity_2m,wind_speed_10m,wind_gusts_10m"}, timeout=60).json()
    gfs = requests.get(OM, params={**base, "models": "gfs_global", "hourly": ",".join(
        f"{v}_{p}hPa" for p in PL for v in ("temperature", "geopotential_height", "relative_humidity", "wind_speed"))},
        timeout=60).json()
    times, H, z = nb["hourly"]["time"], nb["hourly"], nb.get("elevation") or 0
    if nws_point:
        zb, nh = nws_point["elev_m"], nws_point["hourly"]
        shift = STD_LAPSE_F_PER_M * (z - zb)   # NBM at z -> the NWS box height
        pick = lambda key, nbm_key, conv=lambda v: v: [
            nh[t][key] if key in nh.get(t, {}) else (None if H[nbm_key][i] is None else conv(H[nbm_key][i]))
            for i, t in enumerate(times)]
        H = {"temperature_2m": pick("temp_f", "temperature_2m", lambda v: v + shift),
             "relative_humidity_2m": pick("rh", "relative_humidity_2m"),
             "wind_speed_10m": pick("wind_mph", "wind_speed_10m"),
             "wind_gusts_10m": pick("gust_mph", "wind_gusts_10m")}
        z = zb
    return times, H, z, gfs.get("hourly", {})


def _ndfd_onto(nbm_hourly, nd):
    """Put a cell's NWS (NDFD) forecast onto its NBM hourly dict in place, and return the
    NWS precipitation {time: in}. NDFD runs 7 days for temperature/dew point/gusts/sky but
    only ~3 days for precipitation; NBM (and our blend, for precipitation) cover the rest."""
    if not nd:
        return {}
    nh = nd["hourly"]
    for i, t in enumerate(nbm_hourly["time"]):
        v = nh.get(t)
        if not v:
            continue
        for key, col in (("temp_f", "temperature_2m"), ("rh", "relative_humidity_2m"),
                         ("gust_mph", "wind_gusts_10m"), ("sky", "cloud_cover")):
            if key in v and col in nbm_hourly:
                nbm_hourly[col][i] = v[key]
    return {t: v["qpf_in"] for t, v in nh.items() if "qpf_in" in v}


def _b64(typecode, values):
    return base64.b64encode(array(typecode, values).tobytes()).decode()


def build():
    """Everything the Map tab needs, packed compactly, or None if it couldn't be built."""
    try:
        lats, lons, dlat, dlon, land = land_cells()
        pts = [(lats[i // len(lons)], lons[i % len(lons)]) for i in land]
        models = [m for m, w in snow_model.weights.items() if w > 0]
        print(f"  region: {len(pts)} land cells at {CELL_KM:g} km")
        base = {"timezone": TZ, "forecast_days": DAYS}
        nbm = _batched(pts, {**base, "models": "ncep_nbm_conus", "temperature_unit": "fahrenheit",
                             "wind_speed_unit": "mph",
                             "hourly": "temperature_2m,relative_humidity_2m,wind_gusts_10m,cloud_cover"})
        gfs = _batched(pts, {**base, "models": "gfs_global", "temperature_unit": "fahrenheit", "wind_speed_unit": "mph",
                             "hourly": ",".join(f"{v}_{p}hPa" for p in PL for v in
                                                ("temperature", "geopotential_height", "relative_humidity", "wind_speed"))})
        pr = _batched(pts, {**base, "models": ",".join(models), "precipitation_unit": "inch", "hourly": "precipitation"})
        # the base: the NWS forecast at every cell (NDFD, 100 points per request)
        nd = nws.ndfd_points(pts)
        print(f"  region: NWS forecast for {sum(1 for x in nd if x)} of {len(pts)} cells")
    except Exception as exc:
        print(f"  WARNING: region map data unavailable ({exc})")
        return None, {}

    t8 = lambda v: max(-128, min(127, round(v)))
    u8 = lambda v: max(0, min(255, round(v)))
    u16 = lambda v: max(0, min(65535, round(v * 10)))   # snow in tenths of an inch
    temp, snow, gust, cloud, labels = [], [], [], [], None
    ft, fg, fs = [[] for _ in range(DAYS)], [[] for _ in range(DAYS)], [[] for _ in range(DAYS)]
    fc = [[] for _ in range(DAYS)]   # cloud cover per frame (no elevation dimension)
    fp = [[] for _ in range(DAYS)]   # precip (hundredths of an inch) in the 3 h ending at each frame
    dates = None
    for n, g, p, ndc in zip(nbm, gfs, pr, nd):
        nqpf = _ndfd_onto(n["hourly"], ndc)   # NWS values replace NBM's at the surface
        # cloud cover (%): the day's mean, plus the value at each 3-hourly frame
        cc, ct = n["hourly"].get("cloud_cover") or [], n["hourly"]["time"]
        days_i = OrderedDict()
        for i, t in enumerate(ct):
            days_i.setdefault(t[:10], []).append(i)
        for d, (_, ix) in enumerate(list(days_i.items())[:DAYS]):
            vals = [cc[i] for i in ix if i < len(cc) and cc[i] is not None]
            cloud.append(u8(sum(vals) / len(vals)) if vals else 0)
            at = {int(ct[i][11:13]): cc[i] for i in ix if i < len(cc)}
            fc[d] += [u8(at.get(h) or 0) for h in FRAME_HOURS]
        for d in range(len(days_i), DAYS):
            cloud.append(0)
            fc[d] += [0] * len(FRAME_HOURS)
        P = p.get("hourly", {})
        nh = len(n["hourly"]["time"])
        # precipitation per hour (the same at every elevation within a cell): the NWS's where
        # they forecast it, our blend beyond that
        ntimes = n["hourly"]["time"]
        pz = [nqpf[ntimes[i]] if ntimes[i] in nqpf else
              (snow_model.blend({m: (P.get(f"precipitation_{m}") or [None] * nh)[i] for m in models}) or 0)
              for i in range(nh)]
        prof = vertical_profile(n["hourly"]["time"], n["hourly"], n.get("elevation") or 0, g.get("hourly", {}),
                                lambda i, z, pz=pz: pz[i], LEVELS_M)
        labels, dates = prof["labels"], prof["dates"]
        at_time = {t: i for i, t in enumerate(n["hourly"]["time"])}
        for d, date in enumerate(dates):   # model "radar" beyond HRRR's 18 h: 3-hourly precip per cell
            for h in FRAME_HOURS:
                s = sum(pz[at_time[k]] for k in (f"{date}T{hh:02d}:00" for hh in (h - 2, h - 1, h)) if k in at_time)
                fp[d].append(max(0, min(65535, round(s * 100))))
        for d in range(len(dates), DAYS):
            fp[d] += [0] * len(FRAME_HOURS)
        for d in range(DAYS):
            temp += [t8(v) for v in prof["hi"][d]]
            gust += [u8(v) for v in prof["gust"][d]]
            snow += [u16(v) for v in prof["snow"][d]]
            for f in range(len(FRAME_HOURS)):   # per-day frame files: [cell][frame][level]
                ft[d] += [t8(v) for v in prof["f_temp"][d][f]]
                fg[d] += [u8(v) for v in prof["f_gust"][d][f]]
                fs[d] += [u16(v) for v in prof["f_snow"][d][f]]
        snow += [u16(sum(prof["snow"][d][li] for d in range(DAYS))) for li in range(len(LEVELS_M))]
    index = [-1] * (len(lats) * len(lons))
    for ci, gi in enumerate(land):
        index[gi] = ci
    frames = {f"region/day{d}.json": json.dumps({"t": _b64("b", ft[d]), "g": _b64("B", fg[d]), "s": _b64("H", fs[d]),
                                                 "c": _b64("B", fc[d]), "p": _b64("H", fp[d])},
                                                separators=(",", ":")) for d in range(DAYS)}
    tz = ZoneInfo(TZ)   # frame times as UTC ms, with Pacific DST handled here rather than in the browser
    frame_ts = [[int(datetime.strptime(f"{date} {h}", "%Y-%m-%d %H").replace(tzinfo=tz).timestamp() * 1000)
                 for h in FRAME_HOURS] for date in (dates or [])]
    data = {
        "bounds": BOUNDS, "lat0": lats[0], "lon0": lons[0], "dlat": dlat, "dlon": dlon,
        "ny": len(lats), "nx": len(lons), "levels": LEVELS_M, "days": labels,
        "frame_hours": FRAME_HOURS, "frame_files": list(frames), "frame_ts": frame_ts,
        "index": _b64("h", index), "temp": _b64("b", temp), "snow": _b64("H", snow), "gust": _b64("B", gust),
        "cloud": _b64("B", cloud),
    }
    return data, frames
