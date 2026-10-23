"""
Mountain snow model shared by the dashboard and the SNOTEL verifier.

New snow = liquid precip x fraction falling as snow x snow-to-liquid ratio, all
evaluated at the real elevation (the models' own "snowfall" is computed at their
smoothed, lower, warmer terrain and badly undercounts summits).

Precipitation is a weighted blend of several models. The weights and an overall
scale factor start from the defaults below and are re-tuned by verification.py
against SNOTEL observations (verification/calibration.json).
"""
import json
import math
import os

# Default blend, favouring high-res / calibrated models. Pure GFS ("gfs_global", not
# "seamless", which is just HRRR for the first two days in the US and would double-count
# it; also the id the previous-runs archive knows).
DEFAULT_WEIGHTS = {"ncep_nbm_conus": .30, "ecmwf_ifs": .25, "gem_hrdps_continental": .15,
                   "ncep_hrrr_conus": .10, "icon_seamless": .10, "gfs_global": .10}
CALIBRATION_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "verification", "calibration.json")

weights = dict(DEFAULT_WEIGHTS)
qpf_scale = 1.0


def load_calibration():
    """Apply verification/calibration.json if present. Returns the calibration dict or None."""
    global weights, qpf_scale
    try:
        with open(CALIBRATION_PATH, encoding="utf-8") as f:
            cal = json.load(f)
    except (OSError, ValueError):
        return None
    if cal.get("weights"):
        weights = {m: cal["weights"].get(m, 0) for m in DEFAULT_WEIGHTS}
    qpf_scale = cal.get("qpf_scale", 1.0)
    return cal


def blend(values):
    """Weighted blend of {model: value}; models with no value drop out and the rest
    are renormalised (HRRR and HRDPS stop at ~48 h). Scaled by the verified bias factor."""
    got = [(weights.get(m, 0), v) for m, v in values.items() if v is not None and weights.get(m, 0) > 0]
    if not got:
        return None
    return qpf_scale * sum(w * v for w, v in got) / sum(w for w, _ in got)


def rh_from_dew(t_f, dew_f):
    t, d = (t_f - 32) / 1.8, (dew_f - 32) / 1.8
    return 100 * math.exp(17.625 * d / (243.04 + d)) / math.exp(17.625 * t / (243.04 + t))


def wet_bulb_f(t_f, rh):
    """Stull (2011) wet-bulb temperature. Rain vs snow tracks wet-bulb better than air
    temperature: dry air lets snow survive above freezing, humid air doesn't."""
    t, rh = (t_f - 32) / 1.8, min(100, max(5, rh))
    tw = (t * math.atan(0.151977 * (rh + 8.313659) ** 0.5) + math.atan(t + rh) - math.atan(rh - 1.676331)
          + 0.00391838 * rh ** 1.5 * math.atan(0.023101 * rh) - 4.686035)
    return tw * 1.8 + 32


# The warm side is fitted to SNOTEL (owner, 2026-10-23; tools/snow_backtest.py, 8 Cascade stations, 2 winters,
# days with observed humidity): the old model (rain only at wet-bulb 35°F, ratio floor 7:1, everything that fell
# counted) gave 2.2-2.5x the depth gain on 33-37°F days vs 1.6x on cold days. With the switch at 34°F, ratios
# down to 2:1 by 35°F and on-contact melt above 33°F air, every temperature band comes out alike (~1.5-1.6x, the
# depth sensor's settling undercount, also seen on cold days - the cold side is left as it was).
def snow_fraction(tw_f):
    """Share of precip falling as snow: all snow at wet-bulb <= 32°F, all rain at >= 34°F."""
    return 1.0 if tw_f <= 32 else 0.0 if tw_f >= 34 else (34 - tw_f) / 2


_SLR = [(10, 15), (20, 13), (26, 11), (30, 9), (33, 6), (35, 2)]

def snow_ratio(t_f):
    """Snow-to-liquid ratio: up to 15:1 in cold air (capped - PNW maritime snow rarely gets fluffier), ~9:1 at
    30°F, 6:1 at 33°F, 2:1 by 35°F (wet snow that packs down as it lands)."""
    if t_f <= _SLR[0][0]:
        return _SLR[0][1]
    for (a, x), (b, y) in zip(_SLR, _SLR[1:]):
        if t_f <= b:
            return x + (y - x) * (t_f - a) / (b - a)
    return _SLR[-1][1]


def stick(t_f):
    """Share of the falling snow that lasts on the ground: all of it at <= 33°F air, none by 36°F (melts on contact)."""
    return 1.0 if t_f <= 33 else 0.0 if t_f >= 36 else (36 - t_f) / 3


def new_snow_in(p_in, t_f, rh):
    """(snow inches on the ground, snow fraction of the precip) from one hour of liquid precip.
    The fraction is the phase (rain / mix / snow labels); the inches also drop what melts on contact."""
    if not p_in or t_f is None:
        return 0.0, 0.0
    f = snow_fraction(wet_bulb_f(t_f, 90 if rh is None else rh))
    return p_in * f * snow_ratio(t_f) * stick(t_f), f
