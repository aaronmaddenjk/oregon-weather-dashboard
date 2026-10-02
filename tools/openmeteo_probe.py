"""
Can GitHub Actions' shared runners reach Open-Meteo reliably? A one-off probe (2026-10-02).

Makes a build-like mix of requests - single-spot forecasts (cities, volcano waypoints), batched
multi-location forecasts (the Map grid's 15 per request), air quality, ensembles and elevation -
about 1,500 Open-Meteo "calls" in all (a request for n locations counts n), 4 at a time like the
build, and reports latency, throttling (429) and failures. Run by .github/workflows/openmeteo-probe.yml.
"""
import random
import statistics
import time
from concurrent.futures import ThreadPoolExecutor

import requests

random.seed(7)
FC = "https://api.open-meteo.com/v1/forecast"
UA = {"User-Agent": "oregon-weather-dashboard probe (personal, non-commercial)"}


def spot():
    return round(random.uniform(42.0, 48.9), 3), round(random.uniform(-124.3, -117.0), 3)


jobs = []   # (kind, url, params, calls)
for _ in range(160):   # single spots: hourly forecast like render_hike_forecast
    la, lo = spot()
    jobs.append(("spot", FC, {"latitude": la, "longitude": lo, "hourly": "temperature_2m,dew_point_2m,cloud_cover,wind_speed_10m,wind_gusts_10m,precipitation",
                              "forecast_days": 6, "models": "ecmwf_ifs", "timezone": "auto"}, 1))
for _ in range(70):    # the Map grid: 15 locations per request
    pts = [spot() for _ in range(15)]
    jobs.append(("grid15", FC, {"latitude": ",".join(str(p[0]) for p in pts), "longitude": ",".join(str(p[1]) for p in pts),
                                "hourly": "temperature_2m,wind_gusts_10m,precipitation,freezing_level_height", "forecast_days": 4, "timezone": "auto"}, 15))
for _ in range(60):    # air quality
    la, lo = spot()
    jobs.append(("airq", "https://air-quality-api.open-meteo.com/v1/air-quality", {"latitude": la, "longitude": lo, "hourly": "us_aqi", "forecast_days": 6}, 1))
for _ in range(30):    # ensembles
    la, lo = spot()
    jobs.append(("ensemble", "https://ensemble-api.open-meteo.com/v1/ensemble", {"latitude": la, "longitude": lo, "hourly": "temperature_2m",
                                                                                   "models": "gfs025", "forecast_days": 6}, 1))
for _ in range(40):    # elevation
    la, lo = spot()
    jobs.append(("elev", "https://api.open-meteo.com/v1/elevation", {"latitude": la, "longitude": lo}, 1))
random.shuffle(jobs)


def run(job):
    kind, url, params, calls = job
    t0 = time.time()
    try:
        r = requests.get(url, params=params, headers=UA, timeout=(10, 60))
        ok = r.status_code == 200 and "error" not in r.text[:200]
        return kind, calls, time.time() - t0, str(r.status_code) if not ok else "ok"
    except requests.Timeout:
        return kind, calls, time.time() - t0, "timeout"
    except requests.RequestException as e:
        return kind, calls, time.time() - t0, type(e).__name__


start = time.time()
ip = requests.get("https://api.ipify.org", timeout=20).text
print(f"runner IP {ip}; {len(jobs)} requests, ~{sum(j[3] for j in jobs)} Open-Meteo calls, 4 at a time", flush=True)
results = []
with ThreadPoolExecutor(max_workers=4) as ex:
    for i, res in enumerate(ex.map(run, jobs), 1):
        results.append(res)
        if res[3] != "ok":
            print(f"  #{i} {res[0]}: {res[3]} after {res[2]:.1f}s", flush=True)
        if i % 50 == 0:
            print(f"  {i}/{len(jobs)} done, {time.time() - start:.0f}s", flush=True)

print("\n=== summary ===")
total = time.time() - start
for kind in ("spot", "grid15", "airq", "ensemble", "elev"):
    rs = [r for r in results if r[0] == kind]
    lat = [r[2] for r in rs]
    bad = [r for r in rs if r[3] != "ok"]
    print(f"{kind:9s} n={len(rs):3d}  ok={len(rs) - len(bad):3d}  median {statistics.median(lat):5.2f}s  "
          f"p95 {sorted(lat)[int(len(lat) * 0.95) - 1]:5.2f}s  max {max(lat):5.1f}s  problems: "
          + (", ".join(sorted({r[3] for r in bad})) or "none"))
bad = [r for r in results if r[3] != "ok"]
print(f"TOTAL {len(results)} requests in {total:.0f}s; {len(bad)} failed "
      f"({sum(1 for r in bad if r[3] == '429')} throttled 429, {sum(1 for r in bad if r[3] == 'timeout')} timeouts)")
