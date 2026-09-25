"""
National Weather Service gridded forecast (api.weather.gov) - the dashboard's base data.

The NWS forecast starts from NOAA's NBM and is then adjusted by forecasters in the local
offices (Portland, Seattle, Pendleton, Medford, Spokane), which is why it's the base:
it carries local knowledge the raw models don't. It comes on a 2.5 km grid where each
box has a single elevation, so callers still adjust values to the real elevation of a
summit, waypoint or map pixel.

point(lat, lon) returns hourly values keyed like Open-Meteo's local hourly times
("2026-09-24T14:00", America/Los_Angeles), or None if the NWS is unavailable (the
dashboard then falls back to its own model blend):
  temp_f, dew_f, rh, wind_mph, gust_mph, sky, pop  - held across each NWS period
  qpf_in                                           - spread evenly over its period's hours
plus elev_m (the grid box's elevation) and office (e.g. "PQR").
"""
import re
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import requests

from snow_model import rh_from_dew

API = "https://api.weather.gov"
HEADERS = {"User-Agent": "oregon-weather-dashboard (personal, non-commercial)", "Accept": "application/geo+json"}
TZ = ZoneInfo("America/Los_Angeles")

# NWS field -> (our key, converter from the NWS unit, how a period spreads over its hours)
FIELDS = {
    "temperature":              ("temp_f",   lambda c: c * 9 / 5 + 32, "hold"),
    "dewpoint":                 ("dew_f",    lambda c: c * 9 / 5 + 32, "hold"),
    "relativeHumidity":         ("rh",       lambda v: v,              "hold"),
    "windSpeed":                ("wind_mph", lambda k: k / 1.609344,   "hold"),   # km/h
    "windGust":                 ("gust_mph", lambda k: k / 1.609344,   "hold"),
    "skyCover":                 ("sky",      lambda v: v,              "hold"),
    "probabilityOfPrecipitation": ("pop",    lambda v: v,              "hold"),
    "quantitativePrecipitation": ("qpf_in",  lambda mm: mm / 25.4,     "split"),
}


def _hours(duration):
    """ISO 8601 duration like PT6H, P1D, P1DT12H -> hours."""
    m = re.match(r"P(?:(\d+)D)?(?:T(?:(\d+)H)?)?", duration)
    return int(m.group(1) or 0) * 24 + int(m.group(2) or 0) if m else 1


def _get(url):
    r = requests.get(url, headers=HEADERS, timeout=45)
    if r.status_code != 200:
        raise RuntimeError(f"NWS {r.status_code} for {url}")
    return r.json()


def point(lat, lon):
    try:
        meta = _get(f"{API}/points/{lat:.4f},{lon:.4f}")["properties"]
        g = _get(meta["forecastGridData"])["properties"]
    except Exception as exc:
        print(f"  WARNING: NWS forecast unavailable for {lat:.3f},{lon:.3f} ({exc})")
        return None
    hourly = {}
    for field, (key, conv, how) in FIELDS.items():
        for v in (g.get(field) or {}).get("values", []):
            if v.get("value") is None:
                continue
            start_s, dur = v["validTime"].split("/")
            start = datetime.fromisoformat(start_s).astimezone(timezone.utc)
            n = max(1, _hours(dur))
            val = conv(v["value"])
            for k in range(n):
                t = (start + timedelta(hours=k)).astimezone(TZ).strftime("%Y-%m-%dT%H:00")
                hourly.setdefault(t, {})[key] = val / n if how == "split" else val
    return {"elev_m": (g.get("elevation") or {}).get("value") or 0, "office": meta.get("gridId"), "hourly": hourly}


# ---- many points at once: the NDFD XML service (for the Map tab's ~700 cells) ----
# Same NDFD grid as api.weather.gov, but up to 100 points per request and no per-point
# lookups. (The NDFD GRIB files would be the other route, but the GRIB decoder has no
# Windows build, so the Map couldn't be built locally.)
NDFD_XML = "https://graphical.weather.gov/xml/sample_products/browser_interface/ndfdXMLclient.php"
NDFD_BATCH = 100   # the service silently drops points beyond this
# DWML element (tag, type attribute) -> (our key, converter, how a period spreads over hours)
NDFD_FIELDS = {   # (the service doesn't serve humidity; it's derived from dew point below)
    ("temperature", "hourly"): ("temp_f", lambda v: v, "hold"),
    ("temperature", "dew point"): ("dew_f", lambda v: v, "hold"),
    ("wind-speed", "gust"): ("gust_mph", lambda kt: kt * 1.15078, "hold"),
    ("cloud-amount", "total"): ("sky", lambda v: v, "hold"),
    ("precipitation", "liquid"): ("qpf_in", lambda v: v, "split"),
}


def _ndfd_batch(points):
    import xml.etree.ElementTree as ET
    import http_cache
    params = {"listLatLon": " ".join(f"{la:.4f},{lo:.4f}" for la, lo in points), "product": "time-series", "Unit": "e",
              "temp": "temp", "dew": "dew", "wgust": "wgust", "sky": "sky", "qpf": "qpf"}
    for attempt in range(2):
        r = requests.get(NDFD_XML, params=params, headers=HEADERS, timeout=180)
        try:
            root = ET.fromstring(r.content)
            break
        except ET.ParseError:
            # the service sometimes answers 200 with an error page: don't let the cache keep it
            http_cache.forget(NDFD_XML, params)
            if attempt:
                raise
    layouts = {}
    for tl in root.iter("time-layout"):
        starts = [datetime.fromisoformat(e.text) for e in tl.findall("start-valid-time")]
        ends = [datetime.fromisoformat(e.text) for e in tl.findall("end-valid-time")]
        layouts[tl.findtext("layout-key")] = (starts, ends)
    out = {}
    for params in root.iter("parameters"):
        hourly = {}
        for el in params:
            spec = NDFD_FIELDS.get((el.tag, el.get("type")))
            if not spec or el.get("time-layout") not in layouts:
                continue
            key, conv, how = spec
            starts, ends = layouts[el.get("time-layout")]
            vals = [None if v.text is None else float(v.text) for v in el.findall("value")]
            for k, (start, val) in enumerate(zip(starts, vals)):
                if val is None:
                    continue
                # a value holds until the next one (or its own end time; 3 h for the last)
                if ends:
                    stop = ends[k]
                else:
                    stop = starts[k + 1] if k + 1 < len(starts) else start + timedelta(hours=3)
                n = max(1, round((stop - start).total_seconds() / 3600))
                for h in range(n):
                    t = (start + timedelta(hours=h)).astimezone(TZ).strftime("%Y-%m-%dT%H:00")
                    hourly.setdefault(t, {})[key] = conv(val) / n if how == "split" else conv(val)
        for v in hourly.values():   # humidity for the wet-bulb rain/snow split
            if "temp_f" in v and "dew_f" in v:
                v["rh"] = rh_from_dew(v["temp_f"], min(v["dew_f"], v["temp_f"]))
        out[params.get("applicable-location")] = {"hourly": hourly}
    # results come back as point1..pointN in request order
    return [out.get(f"point{i + 1}") for i in range(len(points))]


def ndfd_points(points, workers=3):
    """NWS gridded forecast at many (lat, lon) points, in the same hourly format as point()
    (temp_f, rh, gust_mph, sky, qpf_in). Entries are None where a point couldn't be read."""
    from concurrent.futures import ThreadPoolExecutor
    chunks = [points[k:k + NDFD_BATCH] for k in range(0, len(points), NDFD_BATCH)]

    def one(chunk):
        try:
            return _ndfd_batch(chunk)
        except Exception as exc:
            print(f"  WARNING: NDFD batch of {len(chunk)} points failed ({exc})")
            return [None] * len(chunk)

    with ThreadPoolExecutor(max_workers=workers) as ex:
        return [p for part in ex.map(one, chunks) for p in part]


def series(n, key, times, fallback=None):
    """n's hourly values for `times`, falling back hour by hour where the NWS has none."""
    fb = fallback or [None] * len(times)
    h = (n or {}).get("hourly", {})
    return [h.get(t, {}).get(key, fb[i]) for i, t in enumerate(times)]
