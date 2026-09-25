"""
Disk cache for GET requests, so iterating on the dashboard doesn't burn API quota.

install() wraps requests.get: a successful response is stored under .cache/ and reused
for WX_CACHE_TTL_HOURS (default 3). Rebuilding after a change to layout, colours or
maths then makes no network calls at all. The cache is off in GitHub Actions (every
scheduled run fetches fresh) and can be turned off locally with WX_CACHE=0.

It also counts the calls that did go out. Open-Meteo bills one call per location, and
extra calls for requests with more than 10 variables, so summary() estimates that too.
"""
import hashlib
import json
import math
import os
import threading
import time
from collections import Counter, deque
from urllib.parse import urlparse

import requests

CACHE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".cache")
_real_get = requests.get
_stats = Counter()


class CachedResponse:
    """Just enough of requests.Response for how this project uses it."""
    def __init__(self, status_code, text, url):
        self.status_code, self.text, self.url = status_code, text, url
        self.ok = 200 <= status_code < 400

    @property
    def content(self):
        return self.text.encode("utf-8")

    def json(self):
        return json.loads(self.text)


def _om_calls(params):
    """Open-Meteo's quota cost of one request: locations x ceil(variables / 10)."""
    lats = str((params or {}).get("latitude", ""))
    n_loc = lats.count(",") + 1 if lats else 1
    n_var = 0
    for k in ("hourly", "daily", "current"):
        v = (params or {}).get(k)
        if v:
            n_var += len(v) if isinstance(v, (list, tuple)) else str(v).count(",") + 1
    models = str((params or {}).get("models", ""))
    n_var *= models.count(",") + 1 if models else 1
    return n_loc * max(1, math.ceil(n_var / 10))


def _key_path(url, params):
    key = hashlib.sha256(json.dumps([url, params], sort_keys=True, default=str).encode()).hexdigest()
    return os.path.join(CACHE_DIR, key + ".json")


def forget(url, params=None):
    """Drop a cached response - for a service that returned an error page with a 200 status."""
    try:
        os.remove(_key_path(url, params))
    except OSError:
        pass


def install(ttl_hours=None):
    if os.environ.get("GITHUB_ACTIONS") == "true" or os.environ.get("WX_CACHE") == "0":
        requests.get = _counting_get
        return False
    ttl = 3600 * float(ttl_hours if ttl_hours is not None else os.environ.get("WX_CACHE_TTL_HOURS", 3))
    os.makedirs(CACHE_DIR, exist_ok=True)

    def cached_get(url, params=None, **kw):
        path = _key_path(url, params)
        try:
            if time.time() - os.path.getmtime(path) < ttl:
                with open(path, encoding="utf-8") as f:
                    c = json.load(f)
                _stats["cache hits"] += 1
                return CachedResponse(c["status"], c["text"], c["url"])
        except OSError:
            pass
        r = _counting_get(url, params=params, **kw)
        ctype = r.headers.get("Content-Type", "")
        binary = "octet-stream" in ctype or ctype.startswith("image/")   # stored as text, bytes would be mangled
        if r.status_code == 200 and not binary:
            try:
                with open(path, "w", encoding="utf-8") as f:
                    json.dump({"status": r.status_code, "text": r.text, "url": r.url}, f)
            except OSError:
                pass
        return r

    requests.get = cached_get
    return True


# Open-Meteo also limits calls per minute (600 on the free tier), which the full 25 km Map
# grid would blow through in parallel. Keep a rolling one-minute budget below that.
OM_PER_MINUTE = 500
_om_lock = threading.Lock()
_om_window = deque()   # (time, cost) of recent Open-Meteo requests


def _om_throttle(cost):
    while True:
        with _om_lock:
            now = time.time()
            while _om_window and now - _om_window[0][0] > 60:
                _om_window.popleft()
            used = sum(c for _, c in _om_window)
            if used + cost <= OM_PER_MINUTE or not _om_window:
                _om_window.append((now, cost))
                return
            wait = 60 - (now - _om_window[0][0]) + 0.5
        time.sleep(max(0.5, wait))


# Without a timeout one stalled response hangs the whole build (seen in Actions: 20 min on a
# single Open-Meteo call). Default to (connect, read) seconds and retry a stall twice.
DEFAULT_TIMEOUT = (10, 60)


def _get(url, params=None, **kw):
    kw.setdefault("timeout", DEFAULT_TIMEOUT)
    for attempt in range(3):
        try:
            return _real_get(url, params=params, **kw)
        except (requests.Timeout, requests.ConnectionError):
            if attempt == 2:
                raise
            _stats["retries"] += 1
            time.sleep(3 * (attempt + 1))


def _counting_get(url, params=None, **kw):
    host = urlparse(url).netloc
    _stats[f"requests: {host}"] += 1
    if "open-meteo.com" not in host:
        return _get(url, params=params, **kw)
    cost = _om_calls(params)
    _stats["open-meteo calls (quota)"] += cost
    _om_throttle(cost)
    r = _get(url, params=params, **kw)
    if r.status_code == 429 and "Minutely" in r.text:   # someone else's usage, or a miscount: wait it out once
        time.sleep(61)
        r = _get(url, params=params, **kw)
    return r


def summary():
    om = _stats.get("open-meteo calls (quota)", 0)
    hits = _stats.get("cache hits", 0)
    live = sum(v for k, v in _stats.items() if k.startswith("requests: "))
    return (f"network requests {live}, cache hits {hits}, retries {_stats.get('retries', 0)}, "
            f"Open-Meteo quota used ~{om:,} of ~10,000/day")
