"""
Estimated snow depth by elevation, for the Mt Hood Meadows terrain layers.

Start: today's measured depth at the SNOTEL stations around the mountain, fitted as a
straight line against elevation (depth usually grows with height; with one station or no
spread it's held flat). Then step through the forecast's 3-hourly frames at every elevation:
  + new snow in the step (the same wet-bulb snow model as everywhere else)
  - melt: MELT_IN_PER_F of depth per degree F above freezing per 3 h (a degree-day rule)
  x settling: fresh and old snow compact about SETTLE_PER_STEP per 3 h
It's an estimate: no wind loading, sun aspect or grooming, and glaciers aren't in it.
"""
import math
from datetime import date, timedelta

import requests

AWDB = "https://wcc.sc.egov.usda.gov/awdbRestApi/services/v1"
RADIUS_KM = 35
MELT_IN_PER_F = 0.04     # ~1" of depth per 3 h at 57F; ~0.3"/F-day
SETTLE_PER_STEP = 0.992  # ~6% a day


def _km(lat1, lon1, lat2, lon2):
    p1, p2 = math.radians(lat1), math.radians(lat2)
    a = math.sin((p2 - p1) / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(math.radians(lon2 - lon1) / 2) ** 2
    return 6371 * 2 * math.asin(math.sqrt(a))


def observed_depths(lat, lon):
    """[(elev_m, depth_in, name)] - the latest measured snow depth at nearby SNOTEL stations."""
    try:
        st = requests.get(f"{AWDB}/stations", params={"networkCds": "SNTL", "stateCds": "OR,WA",
                          "returnForecastPointMetadata": "false"}, timeout=90).json()
        near = [s for s in st if s.get("latitude") and (s.get("endDate") or "2100")[:4] >= str(date.today().year)
                and _km(lat, lon, s["latitude"], s["longitude"]) <= RADIUS_KM]
        if not near:
            return []
        data = requests.get(f"{AWDB}/data", params={
            "stationTriplets": ",".join(s["stationTriplet"] for s in near), "elements": "SNWD",
            "duration": "DAILY", "beginDate": (date.today() - timedelta(days=3)).isoformat(),
            "endDate": date.today().isoformat()}, timeout=90).json()
    except Exception as exc:
        print(f"  WARNING: SNOTEL snow depths unavailable ({exc}) - starting from bare ground")
        return []
    by = {s["stationTriplet"]: s for s in near}
    out = []
    for rec in data:
        vals = [v["value"] for el in rec.get("data", []) for v in el.get("values", []) if v.get("value") is not None]
        s = by.get(rec.get("stationTriplet"))
        if vals and s:
            d = vals[-1]
            out.append((s["elevation"] / 3.28084, 0.0 if d < 4 else float(d), s["name"]))   # under 4": the sensors read 1-4" of noise over bare ground
    return out


def initial_profile(levels_m, obs):
    """Depth (in) at each level from the stations: a least-squares line against elevation,
    never negative. Above the highest station the line keeps rising, capped at 1.5x."""
    if not obs:
        return [0.0] * len(levels_m)
    if len(obs) == 1 or max(o[0] for o in obs) - min(o[0] for o in obs) < 150:
        avg = sum(o[1] for o in obs) / len(obs)
        return [avg] * len(levels_m)
    n = len(obs)
    mx, my = sum(o[0] for o in obs) / n, sum(o[1] for o in obs) / n
    sxx = sum((o[0] - mx) ** 2 for o in obs)
    slope = sum((o[0] - mx) * (o[1] - my) for o in obs) / sxx
    top = max(o[1] for o in obs)
    return [min(max(0.0, my + slope * (z - mx)), max(top * 1.5, top + 12)) for z in levels_m]


def evolve(d0, frames):
    """Depth at every level for each frame. frames: [{'temp': [...], 'new': [...]}], where
    new = the snow that fell (in) since the previous frame."""
    d, out = list(d0), []
    for fr in frames:
        d = [max(0.0, (x + fr["new"][i]) * SETTLE_PER_STEP - max(0.0, fr["temp"][i] - 32) * MELT_IN_PER_F)
             for i, x in enumerate(d)]
        out.append([round(x, 1) for x in d])
    return out
