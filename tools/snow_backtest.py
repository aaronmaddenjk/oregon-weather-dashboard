"""Backtest the rain/snow + snow-ratio model (snow_model.py) against SNOTEL (owner, 2026-10-23: "are we too
optimistic when it's warm?").

For each station: hourly observed air temperature, humidity and precipitation go through new_snow_in() exactly as
the forecast does; each day's modelled new snow is compared with the snow-depth gain the sensor saw that day
(max depth during the day minus the depth at the start; settling makes this a slight undercount everywhere, so read
the buckets against each other). Days are bucketed by the precip-weighted air temperature of their wet hours.
Usage: python tools/snow_backtest.py [start end]   (data cached in .cache/backtest/)
"""
import csv
import io
import os
import sys
from collections import defaultdict
from datetime import datetime

import requests

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import snow_model  # noqa: E402

# Cascade SNOTELs that report humidity (RHUMV), so the wet-bulb split runs on observed air like the forecast's
STATIONS = {"651:OR:SNTL": "Mt Hood Test Site", "545:OR:SNTL": "Irish Taylor", "599:WA:SNTL": "Lost Horse",
            "942:WA:SNTL": "Burnt Mountain", "788:WA:SNTL": "Stampede Pass", "897:WA:SNTL": "Meadows Pass",
            "911:WA:SNTL": "Rex River", "734:WA:SNTL": "Sasse Ridge"}
CACHE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".cache", "backtest")
BUCKETS = [(-99, 28), (28, 31), (31, 33), (33, 34), (34, 35), (35, 36), (36, 37), (37, 99)]


def fetch(trip, start, end):
    os.makedirs(CACHE, exist_ok=True)
    path = os.path.join(CACHE, f"{trip.replace(':', '_')}_{start}_{end}.csv")
    if not os.path.exists(path):
        url = ("https://wcc.sc.egov.usda.gov/reportGenerator/view_csv/customSingleStationReport/hourly/start_of_period/"
               f"{trip}%7Cid=%22%22%7Cname/{start},{end}/TOBS::value,PREC::value,SNWD::value,WTEQ::value,RHUMV::value")
        txt = requests.get(url, timeout=180).text
        with open(path, "w", encoding="utf-8") as f:
            f.write(txt)
    with open(path, encoding="utf-8") as f:
        rows = [r for r in f.read().splitlines() if r and not r.startswith("#")]
    out = []
    for r in list(csv.reader(io.StringIO("\n".join(rows))))[1:]:
        num = lambda v: float(v) if v not in ("", None) else None
        out.append((datetime.strptime(r[0], "%Y-%m-%d %H:%M"), num(r[1]), num(r[2]), num(r[3]), num(r[4]),
                    num(r[5]) if len(r) > 5 else None))
    return out


def _med(xs):
    xs = sorted(xs)
    return xs[len(xs) // 2]


def days_for(rows):
    """{date: dict(p, model, obs, net, t, start)} from hourly rows (t, T, PREC accum, depth, SWE, RH).
    The day's liquid = the gauge's midnight-to-midnight change (its hourly values jitter by 0.1"); it is spread over
    the hours whose (running-max) gauge rose, and each hour goes through new_snow_in with its own T and RH.
    Observed new snow = peak of the 5-hour-median depth minus the depth at the start of the day."""
    by = defaultdict(list)
    for r in rows:
        by[r[0].date()].append(r)
    ds = sorted(by)
    out = {}
    for d, nxt in zip(ds, ds[1:]):
        hrs = by[d]
        a0 = _med([h[2] for h in hrs[:3] if h[2] is not None] or [None]) if any(h[2] is not None for h in hrs[:3]) else None
        a1 = [h[2] for h in by[nxt][:3] if h[2] is not None]
        if a0 is None or not a1 or len(hrs) < 20:
            continue
        P = _med(a1) - a0
        if not 0.1 <= P < 6:
            continue
        run, incs = a0, []
        for t, T, acc, dep, swe, rh in hrs:
            inc = 0.0
            if acc is not None and acc > run:
                inc, run = acc - run, acc
            incs.append((inc, T, rh))
        tot = sum(i for i, T, _ in incs if T is not None)
        if tot <= 0:
            continue
        model = tw = 0.0
        for inc, T, rh in incs:
            if inc and T is not None:
                share = P * inc / tot
                model += snow_model.new_snow_in(share, T, rh)[0]
                tw += share * T
        deps = [h[3] for h in hrs + by[nxt][:2]]
        sm = [_med([x for x in deps[max(0, i - 2):i + 3] if x is not None and 0 <= x < 400] or [None])
              if any(x is not None and 0 <= x < 400 for x in deps[max(0, i - 2):i + 3]) else None for i in range(len(deps))]
        sm = [x for x in sm if x is not None]
        if len(sm) < 18:
            continue
        start = sm[0]
        out[d] = {"p": P, "model": model, "obs": max(0.0, max(sm) - start), "net": sm[-1] - start, "t": tw / P,
                  "start": start}
    return out


def main():
    start, end = (sys.argv[1], sys.argv[2]) if len(sys.argv) > 2 else ("2024-10-01", "2026-06-01")
    allrows = []
    for trip, name in STATIONS.items():
        rows = fetch(trip, start, end)
        has_rh = sum(1 for r in rows if r[5] is not None)
        days = days_for(rows)
        print(f"{name} ({trip}): {len(rows)} hours, RH on {has_rh}, {len(days)} wet days")
        allrows += [(name, d, v) for d, v in days.items()]
    print("\nBy precip-weighted air temperature of the wet hours (days with >= 0.1\" liquid):")
    print(f"{'temp F':>9} {'days':>5} {'liquid':>7} {'model':>7} {'observed':>9} {'model/obs':>10} {'model SLR':>10} {'obs SLR':>8} {'net gain':>9}")
    for lo, hi in BUCKETS:
        sel = [v for _, _, v in allrows if lo <= v["t"] < hi]
        if not sel:
            continue
        P = sum(v["p"] for v in sel)
        M = sum(v["model"] for v in sel)
        O = sum(v["obs"] for v in sel)
        N = sum(v["net"] for v in sel)
        lab = f"<{hi}" if lo < -50 else f">={lo}" if hi > 90 else f"{lo}-{hi}"
        print(f"{lab:>9} {len(sel):>5} {P:>7.1f} {M:>7.1f} {O:>9.1f} {M / O if O else float('inf'):>10.2f} "
              f"{M / P:>9.1f}:1 {O / P:>6.1f}:1 {N:>9.1f}")
    # warm days where the model made snow but the ground gained none
    warm = [(n, d, v) for n, d, v in allrows if v["t"] >= 34 and v["model"] >= 1 and v["obs"] < 0.5]
    print(f"\nWarm (>=34F) days the model gave 1\"+ but the sensor gained < 0.5\": {len(warm)}")
    for n, d, v in sorted(warm, key=lambda x: -x[2]["model"])[:12]:
        print(f"  {n:18s} {d}  {v['t']:.1f}F  liquid {v['p']:.2f}  model {v['model']:.1f}  obs {v['obs']:.1f}  "
              f"depth at start {v['start']:.0f}")


if __name__ == "__main__":
    main()
