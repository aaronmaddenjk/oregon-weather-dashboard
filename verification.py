"""
SNOTEL verification loop for the mountain snow model.

Every dashboard build:
  1. picks the SNOTEL stations nearest each mountain (cached in verification/stations.json),
  2. pulls each model's day-ahead precipitation forecast for those stations over the last
     BACKTEST_DAYS days from Open-Meteo's previous-runs archive, plus NBM's day-ahead
     temperature/humidity for rain-vs-snow, at each station's real elevation,
  3. compares them with SNOTEL's measured daily precipitation and snow-depth gain,
  4. re-tunes the blend weights and an overall precipitation scale factor
     (verification/calibration.json), shrunk toward the defaults until there are enough
     wet days to trust, and
  5. writes verification/report.json, rendered on the dashboard's Accuracy tab.

No forecast log is needed: the previous-runs archive is the record of what each model
said the day before. SNOTEL days run midnight-to-midnight Pacific *Standard* Time all
year, so forecasts are summed in Etc/GMT+8 to line up.
"""
import html
import json
import math
import os
from datetime import datetime, timedelta, timezone

from zoneinfo import ZoneInfo

import requests

import nws
import snow_model

AWDB = "https://wcc.sc.egov.usda.gov/awdbRestApi/services/v1"
PREV_RUNS = "https://previous-runs-api.open-meteo.com/v1/forecast"
DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "verification")
BACKTEST_DAYS = 45
MAX_KM = 25            # ignore stations farther than this from a summit
PER_MOUNTAIN = 2       # stations per mountain
WET_IN = 0.10          # observed daily precip that counts as a "wet day"
MIN_WET_DAYS = 10      # below this many wet station-days, calibration stays at defaults
PRIOR_WET_DAYS = 30    # shrinkage strength toward the default weights / scale 1.0
PST = "Etc/GMT+8"


def _km(lat1, lon1, lat2, lon2):
    p1, p2 = math.radians(lat1), math.radians(lat2)
    a = math.sin((p2 - p1) / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(math.radians(lon2 - lon1) / 2) ** 2
    return 6371 * 2 * math.asin(math.sqrt(a))


def pick_stations(mountains):
    """Nearest active SNOTEL stations to each mountain's summit (cached)."""
    path = os.path.join(DIR, "stations.json")
    names = [m["name"] for m in mountains]
    try:
        with open(path, encoding="utf-8") as f:
            cached = json.load(f)
        if cached.get("mountains") == names:
            return cached["stations"]
    except (OSError, ValueError):
        pass
    this_year = str(datetime.now().year)
    allst = requests.get(f"{AWDB}/stations", params={"networkCds": "SNTL", "stateCds": "OR,WA",
                         "returnForecastPointMetadata": "false"}, timeout=90).json()
    sntl = [s for s in allst if s.get("stationTriplet", "").endswith(":SNTL") and s.get("latitude")
            and (s.get("endDate") or "2100")[:4] >= this_year]
    out, seen = [], set()
    for m in mountains:
        pk = m["waypoints"][0]
        near = sorted(((_km(pk["lat"], pk["lon"], s["latitude"], s["longitude"]), s) for s in sntl), key=lambda x: x[0])
        for d, s in near[:PER_MOUNTAIN]:
            if d > MAX_KM or s["stationTriplet"] in seen:
                continue
            seen.add(s["stationTriplet"])
            out.append({"triplet": s["stationTriplet"], "name": s["name"], "lat": s["latitude"], "lon": s["longitude"],
                        "elev_ft": s["elevation"], "mountain": m["name"], "km": round(d, 1)})
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"mountains": names, "stations": out}, f, indent=1)
    return out


def observed_daily(stations, start, end):
    """{triplet: {date: {"precip": in, "snow": in of snow-depth gain}}} from SNOTEL.
    A snow-depth gain only counts as snowfall when the station also measured
    precipitation that day and it was cold enough to snow - the depth sensor reads
    1-4" of noise over bare ground in summer."""
    data = requests.get(f"{AWDB}/data", params={
        "stationTriplets": ",".join(s["triplet"] for s in stations), "elements": "PREC,PRCP,SNWD,TAVG,TOBS",
        "duration": "DAILY", "beginDate": (start - timedelta(days=1)).isoformat(), "endDate": end.isoformat(),
        "periodRef": "END"}, timeout=120).json()
    obs = {}
    for s in data:
        series = {}
        for el in s.get("data", []):
            series[el["stationElement"]["elementCode"]] = {v["date"][:10]: v["value"] for v in el.get("values", [])
                                                           if v.get("value") is not None}
        days, d = {}, start
        while d <= end:
            k, prev = d.isoformat(), (d - timedelta(days=1)).isoformat()
            a, b = series.get("PREC", {}).get(k), series.get("PREC", {}).get(prev)
            if a is not None and b is not None:
                p = a - b if a >= b - 1 else a      # accumulated precip resets each water year (Oct 1)
                p = max(0.0, p)                       # small negative diffs are gauge noise
            else:
                p = series.get("PRCP", {}).get(k)
            sa, sb = series.get("SNWD", {}).get(k), series.get("SNWD", {}).get(prev)
            tavg = series.get("TAVG", {}).get(k, series.get("TOBS", {}).get(k))
            if sa is None or sb is None or p is None:
                snow = None
            elif p >= 0.05 and tavg is not None and tavg <= 38:
                snow = max(0.0, sa - sb)
            else:
                snow = 0.0   # no precip or too warm: any depth change is sensor noise or melt
            days[k] = {"precip": None if p is None else round(p, 2), "snow": snow}
            d += timedelta(days=1)
        obs[s["stationTriplet"]] = days
    return obs


def forecast_hourly(stations):
    """Day-ahead hourly forecasts at each station from the previous-runs archive:
    ({model: [{time: precip} per station]}, [{time: (temp, rh)} per station])."""
    base = {"latitude": ",".join(str(s["lat"]) for s in stations),
            "longitude": ",".join(str(s["lon"]) for s in stations),
            "elevation": ",".join(str(round(s["elev_ft"] / 3.28084)) for s in stations),
            "timezone": PST, "past_days": BACKTEST_DAYS + 2, "forecast_days": 1}

    def get(params):
        d = requests.get(PREV_RUNS, params={**base, **params}, timeout=120).json()
        return d if isinstance(d, list) else [d]

    per_model = {}
    for m in snow_model.DEFAULT_WEIGHTS:
        try:
            locs = get({"models": m, "hourly": "precipitation_previous_day1", "precipitation_unit": "inch"})
            per_model[m] = [dict(zip(l["hourly"]["time"], l["hourly"]["precipitation_previous_day1"])) for l in locs]
        except Exception:
            continue
    try:
        locs = get({"models": "ncep_nbm_conus", "temperature_unit": "fahrenheit",
                    "hourly": "temperature_2m_previous_day1,relative_humidity_2m_previous_day1"})
        phase = [{t: (T, R) for t, T, R in zip(l["hourly"]["time"], l["hourly"]["temperature_2m_previous_day1"],
                                               l["hourly"]["relative_humidity_2m_previous_day1"])} for l in locs]
    except Exception:
        phase = [{} for _ in stations]
    return per_model, phase


NWS_LOG_DAYS = 150   # how long logged NWS forecasts are kept


def log_nws(stations):
    """Record the NWS's forecast precipitation for tomorrow (a PST day, like SNOTEL's) at each
    station. The NWS keeps no archive of past forecasts, so this log is what lets the
    Accuracy tab score the dashboard's base forecast against the gauges and our blend."""
    path = os.path.join(DIR, "nws_log.json")
    try:
        with open(path, encoding="utf-8") as f:
            log = json.load(f)
    except (OSError, ValueError):
        log = {}
    today = (datetime.now(timezone.utc) - timedelta(hours=8)).date()
    tomorrow = today + timedelta(days=1)
    start = datetime(tomorrow.year, tomorrow.month, tomorrow.day, 8, tzinfo=timezone.utc)   # PST midnight
    pt = ZoneInfo("America/Los_Angeles")
    for s in stations:
        n = nws.point(s["lat"], s["lon"])
        if not n:
            continue
        hours = [v["qpf_in"] for k, v in n["hourly"].items() if "qpf_in" in v and
                 start <= datetime.strptime(k, "%Y-%m-%dT%H:%M").replace(tzinfo=pt).astimezone(timezone.utc) < start + timedelta(days=1)]
        if len(hours) >= 20:
            log[f"{s['triplet']}|{tomorrow.isoformat()}"] = {"qpf": round(sum(hours), 3), "issued": today.isoformat()}
    cutoff = (today - timedelta(days=NWS_LOG_DAYS)).isoformat()
    log = {k: v for k, v in log.items() if k.split("|")[1] >= cutoff}
    with open(path, "w", encoding="utf-8") as f:
        json.dump(log, f, indent=0, sort_keys=True)
    return log


def _blend(values, w):
    got = [(w[m], v) for m, v in values.items() if v is not None and w.get(m, 0) > 0]
    return sum(a * v for a, v in got) / sum(a for a, _ in got) if got else None


def _stats(pairs):
    """pairs of (forecast, observed) daily precip -> summary stats."""
    pairs = [(f, o) for f, o in pairs if f is not None and o is not None]
    if not pairs:
        return None
    wet = [(f, o) for f, o in pairs if o >= 0.02 or f >= 0.02]
    sf, so = sum(f for f, _ in pairs), sum(o for _, o in pairs)
    obs_wet = [f for f, o in pairs if o >= WET_IN]
    fc_wet = [o for f, o in pairs if f >= WET_IN]
    return {
        "days": len(pairs), "fc_total": round(sf, 2), "obs_total": round(so, 2),
        "bias_pct": round(100 * (sf - so) / so) if so >= 0.2 else None,
        "mae_wet": round(sum(abs(f - o) for f, o in wet) / len(wet), 3) if wet else None,
        "pod": round(sum(f >= WET_IN for f in obs_wet) / len(obs_wet), 2) if obs_wet else None,
        "far": round(sum(o < 0.02 for o in fc_wet) / len(fc_wet), 2) if fc_wet else None,
    }


def score_and_calibrate(stations, obs, per_model, phase, start, end, nws_log=None):
    dates = [(start + timedelta(days=i)).isoformat() for i in range((end - start).days + 1)]
    models = [m for m in snow_model.DEFAULT_WEIGHTS if m in per_model]

    # daily model totals per station (a day needs >= 20 forecast hours to count)
    daily = {}  # (triplet, date) -> {model: in}
    for si, s in enumerate(stations):
        for dk in dates:
            row = {}
            for m in models:
                vals = [v for t, v in per_model[m][si].items() if t.startswith(dk) and v is not None]
                row[m] = sum(vals) if len(vals) >= 20 else None
            daily[(s["triplet"], dk)] = row

    def o(s, dk, key="precip"):
        return obs.get(s["triplet"], {}).get(dk, {}).get(key)

    model_stats = {m: _stats([(daily[(s["triplet"], dk)][m], o(s, dk)) for s in stations for dk in dates]) for m in models}
    n_wet = sum(1 for s in stations for dk in dates if (o(s, dk) or 0) >= WET_IN)

    # re-weight by skill on wet days, shrunk toward the defaults by sample size
    alpha = n_wet / (n_wet + PRIOR_WET_DAYS) if n_wet >= MIN_WET_DAYS else 0.0
    raw = {m: snow_model.DEFAULT_WEIGHTS[m] / max((model_stats[m] or {}).get("mae_wet") or 1, 0.02) for m in models}
    tot = sum(raw.values()) or 1
    weights = {m: round((1 - alpha) * snow_model.DEFAULT_WEIGHTS[m] + alpha * raw.get(m, 0) / tot, 3)
               for m in snow_model.DEFAULT_WEIGHTS}
    norm = sum(weights.values())
    weights = {m: round(w / norm, 3) for m, w in weights.items()}

    # overall wet/dry bias of the re-weighted blend -> scale factor (clamped, shrunk)
    blend_pairs = [(_blend(daily[(s["triplet"], dk)], weights), o(s, dk)) for s in stations for dk in dates]
    unscaled = _stats(blend_pairs)
    ratio = (unscaled["obs_total"] / unscaled["fc_total"]) if unscaled and unscaled["fc_total"] > 0.2 else 1.0
    scale = round(1 + alpha * (min(1.3, max(0.7, ratio)) - 1), 3)
    blend_stats = _stats([(None if f is None else f * scale, ob) for f, ob in blend_pairs])

    # snow: our method on the blended day-ahead precip vs SNOTEL snow-depth gain
    snow_pairs, per_station = [], []
    for si, s in enumerate(stations):
        if not any(o(s, dk) is not None for dk in dates):
            per_station.append({**s, "no_data": True})   # e.g. a station whose precip gauge is offline
            continue
        fc_snow_tot = obs_snow_tot = fc_p = ob_p = 0.0
        for dk in dates:
            hours = sorted(t for t in per_model.get(models[0], [{}] * len(stations))[si] if t.startswith(dk)) if models else []
            snow = 0.0
            for t in hours:
                p = _blend({m: per_model[m][si].get(t) for m in models}, weights)
                T, R = phase[si].get(t, (None, None))
                if p is not None:
                    snow += snow_model.new_snow_in(p * scale, T, R)[0]
            obs_snow = o(s, dk, "snow")
            if obs_snow is not None and (snow >= 0.5 or obs_snow >= 0.5):
                snow_pairs.append((round(snow, 1), obs_snow))
            fc_snow_tot += snow
            obs_snow_tot += obs_snow or 0
            f = _blend(daily[(s["triplet"], dk)], weights)
            if f is not None and o(s, dk) is not None:
                fc_p += f * scale
                ob_p += o(s, dk)
        per_station.append({**s, "obs_precip": round(ob_p, 2), "fc_precip": round(fc_p, 2),
                            "obs_snow": round(obs_snow_tot, 1), "fc_snow": round(fc_snow_tot, 1)})

    snow_stats = None
    if snow_pairs:
        sf, so = sum(f for f, _ in snow_pairs), sum(ob for _, ob in snow_pairs)
        snow_stats = {"days": len(snow_pairs), "fc_total": round(sf, 1), "obs_total": round(so, 1),
                      "bias_pct": round(100 * (sf - so) / so) if so >= 1 else None,
                      "mae": round(sum(abs(f - ob) for f, ob in snow_pairs) / len(snow_pairs), 1)}

    # background head-to-head: the NWS (the dashboard's base) vs our tuned blend, on the
    # same station-days, wherever a logged NWS forecast has since been measured
    nws_pairs, ours_pairs = [], []
    for s in stations:
        for dk in dates:
            e = (nws_log or {}).get(f"{s['triplet']}|{dk}")
            ob = o(s, dk)
            f = _blend(daily[(s["triplet"], dk)], weights)
            if e is None or ob is None or f is None:
                continue
            nws_pairs.append((e["qpf"], ob))
            ours_pairs.append((f * scale, ob))
    logged = sorted(k.split("|")[1] for k in (nws_log or {}))
    nws_report = {"nws": _stats(nws_pairs), "blend": _stats(ours_pairs), "since": logged[0] if logged else None,
                  "wet_days": sum(1 for _, ob in nws_pairs if ob >= WET_IN)}

    calibration = {"updated": datetime.now(timezone.utc).isoformat(timespec="minutes"),
                   "window": [start.isoformat(), end.isoformat()], "wet_station_days": n_wet,
                   "trust": round(alpha, 2), "weights": weights, "qpf_scale": scale}
    report = {"calibration": calibration, "models": model_stats, "blend": blend_stats, "vs_nws": nws_report,
              "snow": snow_stats, "stations": per_station, "default_weights": snow_model.DEFAULT_WEIGHTS}
    return report, calibration


def run(mountains):
    """Verify, re-calibrate snow_model, and return the report (None if it couldn't run;
    the last good calibration, if any, is still applied)."""
    os.makedirs(DIR, exist_ok=True)
    try:
        stations = pick_stations(mountains)
        end = (datetime.now(timezone.utc) - timedelta(hours=8)).date() - timedelta(days=1)  # last complete PST day
        start = end - timedelta(days=BACKTEST_DAYS - 1)
        obs = observed_daily(stations, start, end)
        per_model, phase = forecast_hourly(stations)
        nws_log = log_nws(stations)
        report, calibration = score_and_calibrate(stations, obs, per_model, phase, start, end, nws_log)
        with open(os.path.join(DIR, "calibration.json"), "w", encoding="utf-8") as f:
            json.dump(calibration, f, indent=1)
        with open(os.path.join(DIR, "report.json"), "w", encoding="utf-8") as f:
            json.dump(report, f, indent=1)
    except Exception as exc:
        print(f"  WARNING: SNOTEL verification skipped ({exc})")
        report = None
        try:
            with open(os.path.join(DIR, "report.json"), encoding="utf-8") as f:
                report = json.load(f)
        except (OSError, ValueError):
            pass
    snow_model.load_calibration()
    return report


# ---- Accuracy tab ---------------------------------------------------------
MODEL_NAMES = {"ncep_nbm_conus": "NOAA NBM", "ecmwf_ifs": "ECMWF HRES", "gem_hrdps_continental": "Canada HRDPS",
               "ncep_hrrr_conus": "NOAA HRRR", "icon_seamless": "DWD ICON", "gfs_global": "NOAA GFS"}


def report_html(report):
    if not report:
        return '<p class="acc-empty">Verification hasn\'t run yet.</p>'
    cal, w0 = report["calibration"], report["default_weights"]

    def pct(v, signed=False):
        return "&ndash;" if v is None else (f"{v:+d}%" if signed else f"{round(v * 100)}%")

    def num(v, unit='"'):
        return "&ndash;" if v is None else f"{v:.2f}{unit}"

    def bias_cls(v):
        return "" if v is None else " acc-bad" if abs(v) >= 30 else " acc-warn" if abs(v) >= 15 else " acc-ok"

    rows = ""
    for m, st in report["models"].items():
        st = st or {}
        wn, wd = cal["weights"].get(m, 0), w0.get(m, 0)
        arrow = "" if abs(wn - wd) < 0.005 else (" acc-up" if wn > wd else " acc-down")
        rows += (f'<tr><th>{MODEL_NAMES.get(m, m)}</th>'
                 f'<td class="acc-w{arrow}">{round(wn * 100)}%<span>from {round(wd * 100)}%</span></td>'
                 f'<td class="{bias_cls(st.get("bias_pct")).strip()}">{pct(st.get("bias_pct"), True)}</td>'
                 f'<td>{num(st.get("mae_wet"))}</td><td>{pct(st.get("pod"))}</td><td>{pct(st.get("far"))}</td></tr>')
    b = report.get("blend") or {}
    rows += (f'<tr class="acc-blend"><th>Our blend</th><td>&times;{cal["qpf_scale"]:.2f}<span>scale</span></td>'
             f'<td class="{bias_cls(b.get("bias_pct")).strip()}">{pct(b.get("bias_pct"), True)}</td>'
             f'<td>{num(b.get("mae_wet"))}</td><td>{pct(b.get("pod"))}</td><td>{pct(b.get("far"))}</td></tr>')

    if cal["trust"] == 0:
        status = (f'Collecting data: <b>{cal["wet_station_days"]}</b> of {MIN_WET_DAYS} wet station-days needed '
                  'before the blend starts tuning itself. Weights are at their defaults.')
    else:
        status = (f'Tuned from <b>{cal["wet_station_days"]}</b> wet station-days. The blend currently trusts the '
                  f'verification <b>{round(cal["trust"] * 100)}%</b> and the defaults the rest, and that share grows as '
                  'more storms are scored.')

    def st_row(s):
        head = (f'<th>{html.escape(s["name"])}<span>{html.escape(s["mountain"])} &middot; {s["elev_ft"]:,.0f}&prime; &middot; '
                f'{s["km"]:g} km from summit</span></th>')
        if s.get("no_data"):
            return f'<tr class="acc-nodata">{head}<td colspan="4">No precipitation data from this station in the window</td></tr>'
        return (f'<tr>{head}<td>{s["obs_precip"]:.2f}"</td><td>{s["fc_precip"]:.2f}"</td>'
                f'<td>{s["obs_snow"]:.1f}"</td><td>{s["fc_snow"]:.1f}"</td></tr>')
    st_rows = "".join(st_row(s) for s in report["stations"])

    vs = report.get("vs_nws") or {}
    if vs.get("nws"):
        def vrow(name, st):
            return (f'<tr><th>{name}</th><td class="{bias_cls(st.get("bias_pct")).strip()}">{pct(st.get("bias_pct"), True)}</td>'
                    f'<td>{num(st.get("mae_wet"))}</td><td>{pct(st.get("pod"))}</td><td>{pct(st.get("far"))}</td></tr>')
        vs_html = (f'<p class="acc-sub">{vs["nws"]["days"]} station-days since {vs["since"]} ({vs["wet_days"]} wet), '
                   'both forecasts made the day before</p><div class="scroll-wrap"><table class="acc-tbl">'
                   '<tr><th></th><th>Bias</th><th>Typical miss<span>wet days</span></th><th>Caught wet days</th><th>False alarms</th></tr>'
                   + vrow("NWS forecast<span>the dashboard's base</span>", vs["nws"]) + vrow("Our model blend", vs.get("blend") or {})
                   + '</table></div>')
    else:
        vs_html = (f'<p class="acc-sub">Logging the NWS\'s day-ahead forecasts at these stations'
                   f'{" since " + vs["since"] if vs.get("since") else ""}. The NWS keeps no archive of past forecasts, '
                   'so the head-to-head fills in as logged days are measured.</p>')

    sn = report.get("snow")
    snow_line = ("No measurable snowfall at the stations in this window yet. Snow scoring starts with the first storm."
                 if not sn else
                 f'On {sn["days"]} snowy station-days we forecast <b>{sn["fc_total"]}"</b> vs <b>{sn["obs_total"]}"</b> of '
                 f'measured snow-depth gain (typical miss {sn["mae"]}" per day). Depth gain undercounts real snowfall '
                 'because fresh snow settles, so a modest positive bias here is expected.')

    return f'''
<div class="acc">
  <p class="acc-status">{status}</p>
  <h2>NWS forecast vs our model blend</h2>
  {vs_html}
  <h2>Day-ahead precipitation vs SNOTEL gauges</h2>
  <p class="acc-sub">{cal["window"][0]} to {cal["window"][1]} &middot; {len(report["stations"])} stations near the mountains &middot; each model's forecast from the day before</p>
  <div class="scroll-wrap"><table class="acc-tbl">
    <tr><th></th><th>Blend weight</th><th>Bias</th><th>Typical miss<span>wet days</span></th><th>Caught wet days</th><th>False alarms</th></tr>
    {rows}
  </table></div>
  <h2>Snow</h2>
  <p class="acc-sub">{snow_line}</p>
  <h2>Stations</h2>
  <div class="scroll-wrap"><table class="acc-tbl">
    <tr><th></th><th>Measured precip</th><th>Forecast precip</th><th>Snow-depth gain</th><th>Forecast snow</th></tr>
    {st_rows}
  </table></div>
  <p class="acc-foot">The dashboard shows the NWS forecast, adjusted to each spot's elevation; our model blend runs
  alongside as a challenger and fills any hours the NWS doesn't cover. Bias: + means too much forecast. Wet day = at least {WET_IN}" measured. SNOTEL gauges can under-catch
  wind-blown snow by 10&ndash;30%, so the scale factor is clamped to &times;0.7&ndash;1.3. Sources: NRCS SNOTEL, Open-Meteo previous-runs archive.</p>
</div>'''
