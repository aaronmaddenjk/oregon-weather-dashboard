"""
Tide predictions (high and low times and heights) for the coastal cities, from NOAA CO-OPS
(tidesandcurrents.noaa.gov; free, no key). Heights are feet above MLLW, times local.

Stations are picked by hand: the nearest station by distance is often up a river, where the tide
runs late. There's no station at Cannon Beach; Garibaldi is NOAA's reference station for the
north Oregon coast.
"""
from datetime import datetime

import requests

API = "https://api.tidesandcurrents.noaa.gov/api/prod/datagetter"
STATIONS = {   # city -> (NOAA station id, name)
    "Forks": ("9442396", "La Push"),
    "Cannon Beach": ("9437540", "Garibaldi"),
    "Pacific City": ("TWC0857", "Nestucca Bay entrance"),
    "Florence": ("9434098", "Florence USCG Pier"),
}


def hilo(city, start, days=7):
    """[{'t': datetime (local), 'ft': float, 'hi': bool}] for `days` days from `start` (a date),
    or None if the city has no station or NOAA is unavailable."""
    if city not in STATIONS:
        return None
    sid, _ = STATIONS[city]
    try:
        r = requests.get(API, params={
            "product": "predictions", "datum": "MLLW", "station": sid, "time_zone": "lst_ldt",
            "units": "english", "interval": "hilo", "format": "json", "begin_date": start.strftime("%Y%m%d"),
            "range": 24 * days, "application": "oregon-weather-dashboard"}, timeout=(10, 60)).json()
        return [{"t": datetime.strptime(p["t"], "%Y-%m-%d %H:%M"), "ft": float(p["v"]), "hi": p["type"] == "H"}
                for p in r.get("predictions", [])]
    except (requests.RequestException, ValueError, KeyError) as e:
        print(f"  WARNING: tides for {city} unavailable ({e})", flush=True)
        return None


def station_name(city):
    return STATIONS[city][1] if city in STATIONS else None
