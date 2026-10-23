"""Our own rendering of Mt Hood for the Mt Hood tab (owner, 2026-10-23: replace the static Mapbox 3D view with a
terrain-accurate, artistic picture).

Terrain: USGS 3DEP 1/3 arc-second (~10 m), one download cached in .cache/hood_art/dem.tif. A ray is traced from a
fixed camera (south of Meadows, looking north up at the summit, like the old Mapbox view) for every pixel; the hit
gives each pixel its ground position and elevation. A painter pass then colours the terrain in map space (forest,
rock, snow, light, contours) and the picture samples it, with distance haze and a sky. The per-pixel elevation is
what lets the page recolour the picture by the forecast at each height (temperature, gusts, new snow, depth).
Usage: python tools/hood_art.py [out_dir]   (needs numpy; a one-time render, not part of the hourly build)
"""
import math
import os
import sys

import numpy as np
from PIL import Image, ImageDraw, ImageFilter, ImageFont

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CACHE = os.path.join(ROOT, ".cache", "hood_art")
BBOX = (-121.80, 45.25, -121.55, 45.42)   # lon0, lat0, lon1, lat1
DEM_URL = ("https://elevation.nationalmap.gov/arcgis/rest/services/3DEPElevation/ImageServer/exportImage?"
           "bbox={},{},{},{}&bboxSR=4326&imageSR=4326&size=2000,1900&format=tiff&pixelType=F32"
           "&interpolation=RSP_BilinearInterpolation&f=image")
LAT_C = 45.3355
MX = 111320 * math.cos(math.radians(LAT_C))   # metres per degree of longitude here
MY = 111132.0


def load_dem():
    path = os.path.join(CACHE, "dem.tif")
    if not os.path.exists(path):
        import requests
        os.makedirs(CACHE, exist_ok=True)
        with open(path, "wb") as f:
            f.write(requests.get(DEM_URL.format(*BBOX), timeout=300).content)
    z = np.array(Image.open(path), dtype=np.float32)[::-1].copy()   # row 0 = south
    ny, nx = z.shape
    dx = (BBOX[2] - BBOX[0]) / nx * MX
    dy = (BBOX[3] - BBOX[1]) / ny * MY
    return z, dx, dy


def to_xy(lon, lat):
    return (lon - BBOX[0]) * MX, (lat - BBOX[1]) * MY


def bilinear(a, x, y, dx, dy):
    """Sample grid `a` (row = y) at metres x, y; NaN outside."""
    ny, nx = a.shape
    fx, fy = x / dx - 0.5, y / dy - 0.5
    ok = (fx >= 0) & (fy >= 0) & (fx < nx - 1) & (fy < ny - 1)
    fx, fy = np.clip(np.nan_to_num(fx), 0, nx - 1.001), np.clip(np.nan_to_num(fy), 0, ny - 1.001)
    x0, y0 = fx.astype(np.int32), fy.astype(np.int32)
    tx, ty = fx - x0, fy - y0
    v = (a[y0, x0] * (1 - tx) * (1 - ty) + a[y0, x0 + 1] * tx * (1 - ty)
         + a[y0 + 1, x0] * (1 - tx) * ty + a[y0 + 1, x0 + 1] * tx * ty)
    return np.where(ok, v, np.nan)


class Camera:
    def __init__(self, target_lonlat, dist, pitch_deg, fov_y_deg, w, h, ex, zref, target_z, shift_y=0.0, bearing=0.0):
        self.w, self.h, self.ex, self.zref = w, h, ex, zref
        tx, ty = to_xy(*target_lonlat)
        p, b = math.radians(pitch_deg), math.radians(bearing)
        tz = zref + (target_z - zref) * ex
        hd = np.array([math.sin(b), math.cos(b)])   # looking toward this compass bearing
        self.pos = np.array([tx - hd[0] * dist * math.cos(p), ty - hd[1] * dist * math.cos(p), tz + dist * math.sin(p)])
        self.fwd = np.array([hd[0] * math.cos(p), hd[1] * math.cos(p), -math.sin(p)])
        self.right = np.array([hd[1], -hd[0], 0.0])
        self.up = np.cross(self.right, self.fwd)
        self.ty = math.tan(math.radians(fov_y_deg) / 2)
        self.tx = self.ty * w / h
        self.shift_y = shift_y   # moves the picture's centre up (like Mapbox's top padding)

    def rays(self):
        u = (np.arange(self.w) + 0.5) / self.w * 2 - 1
        v = 1 - (np.arange(self.h) + 0.5) / self.h * 2 + self.shift_y
        U, V = np.meshgrid(u * self.tx, v * self.ty)
        d = self.fwd[None, None] + U[..., None] * self.right + V[..., None] * self.up
        return d / np.linalg.norm(d, axis=-1, keepdims=True)

    def project(self, x, y, z):
        """World metres (z real, exaggeration applied here) -> screen px, depth."""
        p = np.stack([x, y, self.zref + (np.asarray(z) - self.zref) * self.ex], -1) - self.pos
        f = p @ self.fwd
        sx = (p @ self.right) / f / self.tx
        sy = (p @ self.up) / f / self.ty - self.shift_y
        return (sx + 1) / 2 * self.w, (1 - sy) / 2 * self.h, f


def trace(z, dx, dy, cam):
    """Per-pixel ground hit: x, y (m), elevation (m, real), distance (m); NaN = sky."""
    ze = cam.zref + (z - cam.zref) * cam.ex
    D = cam.rays().reshape(-1, 3).astype(np.float64)
    n = len(D)
    out_t = np.full(n, np.nan)
    act = np.arange(n)
    t = np.full(n, 30.0)
    zmax = float(np.nanmax(ze))
    o = cam.pos
    while len(act):
        P0 = o + D[act] * t[:, None]
        g0 = bilinear(ze, P0[:, 0], P0[:, 1], dx, dy)
        dt = np.maximum(np.maximum(3.0, t * 0.004), np.nan_to_num(0.3 * (P0[:, 2] - g0), nan=50.0))   # big steps high up
        tn = t + dt
        P = o + D[act] * tn[:, None]
        g = bilinear(ze, P[:, 0], P[:, 1], dx, dy)
        hit = P[:, 2] <= g
        gone = (tn > 40000) | ((P[:, 2] > zmax) & (D[act, 2] > 0)) | (np.isnan(g) & (tn > 2000))
        if hit.any():   # refine between t and tn
            a, b = t[hit], tn[hit]
            idx = act[hit]
            for _ in range(8):
                m = (a + b) / 2
                Q = o + D[idx] * m[:, None]
                below = Q[:, 2] <= bilinear(ze, Q[:, 0], Q[:, 1], dx, dy)
                b = np.where(below, m, b)
                a = np.where(below, a, m)
            out_t[idx] = b
        keep = ~hit & ~gone
        act, t = act[keep], tn[keep]
    P = o + D * out_t[:, None]
    hx, hy = P[:, 0], P[:, 1]
    elev = bilinear(z, hx, hy, dx, dy)
    sh = (cam.h, cam.w)
    return {"x": hx.reshape(sh), "y": hy.reshape(sh), "elev": elev.reshape(sh), "dist": out_t.reshape(sh)}


# ---------- painting (map space) ----------

def box_blur(a, r):
    """Separable box blur (radius r cells), edges clamped."""
    for ax in (0, 1):
        p = np.pad(a, [(r + 1, r) if k == ax else (0, 0) for k in (0, 1)], mode="edge")
        c = np.cumsum(p, axis=ax, dtype=np.float64)
        sl_hi = [slice(None)] * 2
        sl_lo = [slice(None)] * 2
        sl_hi[ax] = slice(2 * r + 1, None)
        sl_lo[ax] = slice(0, -2 * r - 1)
        a = ((c[tuple(sl_hi)] - c[tuple(sl_lo)]) / (2 * r + 1)).astype(np.float32)
    return a


def noise(shape, scale, seed, octaves=4):
    rng = np.random.default_rng(seed)
    out = np.zeros(shape, np.float32)
    amp, tot = 1.0, 0.0
    for o in range(octaves):
        s = max(2, int(shape[1] / (scale / 2 ** o)))
        sy = max(2, int(shape[0] / (scale / 2 ** o)))
        r = rng.random((sy, s)).astype(np.float32)
        im = Image.fromarray(r).resize((shape[1], shape[0]), Image.BICUBIC)
        out += amp * np.array(im)
        tot += amp
        amp *= 0.5
    return out / tot


def vnoise(x, y, cell, seed=0):
    """Smooth value noise in [0,1] at world metres x, y with feature size `cell` m (any shape, per pixel)."""
    gx, gy = x / cell, y / cell
    ix, iy = np.floor(gx), np.floor(gy)
    fx, fy = gx - ix, gy - iy
    fx, fy = fx * fx * (3 - 2 * fx), fy * fy * (3 - 2 * fy)
    def h(i, j):
        v = np.sin(i * 127.1 + j * 311.7 + seed * 74.7) * 43758.5453
        return v - np.floor(v)
    a, b, c, d = h(ix, iy), h(ix + 1, iy), h(ix, iy + 1), h(ix + 1, iy + 1)
    return (a * (1 - fx) + b * fx) * (1 - fy) + (c * (1 - fx) + d * fx) * fy


def osm():
    """Ski runs + lifts (OpenStreetMap, Overpass; cached in .cache/hood_art/osm.json)."""
    import json
    path = os.path.join(CACHE, "osm.json")
    if not os.path.exists(path):
        import requests
        q = ('[out:json][timeout:50];(way["piste:type"="downhill"](45.30,-121.75,45.37,-121.62);'
             'way["aerialway"](45.30,-121.75,45.37,-121.62););out geom;')
        r = requests.post("https://overpass-api.de/api/interpreter", data={"data": q}, timeout=120,
                          headers={"User-Agent": "oregon-weather-dashboard/1.0 (personal project)"})
        with open(path, "w", encoding="utf-8") as f:
            f.write(r.text)
    with open(path, encoding="utf-8") as f:
        return json.load(f)["elements"]


def runs_mask(shape, dx, dy):
    """Cleared ski runs on the DEM grid (0-1): run centre lines ~35 m wide, run areas filled."""
    ny, nx = shape
    im = Image.new("L", (nx, ny), 0)
    d = ImageDraw.Draw(im)
    for e in osm():
        t = e.get("tags", {})
        if t.get("piste:type") != "downhill" or "geometry" not in e:
            continue
        pts = [((g["lon"] - BBOX[0]) * MX / dx, (g["lat"] - BBOX[1]) * MY / dy) for g in e["geometry"]]
        if t.get("area") == "yes" and len(pts) > 2:
            d.polygon(pts, fill=255)
        elif len(pts) > 1:
            d.line(pts, fill=255, width=max(2, round(35 / dx)), joint="curve")
    im = im.filter(ImageFilter.GaussianBlur(1.2))
    return np.array(im, np.float32) / 255


def terrain_fields(z, dx, dy):
    gy, gx = np.gradient(z, dy, dx)
    slope = np.degrees(np.arctan(np.hypot(gx, gy)))
    nrm = np.stack([-gx, -gy, np.ones_like(z)], -1)
    nrm /= np.linalg.norm(nrm, axis=-1, keepdims=True)
    # openness: how far a cell sits below / above its surroundings (soft ambient occlusion, ridge glow)
    rel = z - box_blur(z, 12)
    rel2 = z - box_blur(z, 40)
    return {"slope": slope, "n": nrm, "rel": rel, "rel2": rel2, "runs": runs_mask(z.shape, dx, dy)}


def light(nrm, az_deg, alt_deg):
    az, al = math.radians(az_deg), math.radians(alt_deg)
    L = np.array([math.sin(az) * math.cos(al), math.cos(az) * math.cos(al), math.sin(al)])
    return np.clip(nrm @ L, 0, 1)


def mix(a, b, t):
    t = np.asarray(t)[..., None] if np.ndim(t) else t
    return a * (1 - t) + b * t


def smooth(e0, e1, x):
    t = np.clip((x - e0) / (e1 - e0), 0, 1)
    return t * t * (3 - 2 * t)


def paint(z, dx, dy, F, style, sun_az=230, sun_alt=28, ov=0.0):
    """Map-space colour (float RGB 0-1) for a style: 'relief' | 'paint' | 'night'."""
    sh = z.shape
    n1, n2, n3 = noise(sh, 260, 1), noise(sh, 60, 2, 3), noise(sh, 14, 3, 2)
    slope, rel, rel2 = F["slope"], F["rel"], F["rel2"]
    tree_line = 1900 + (n1 - 0.5) * 260 + (n2 - 0.5) * 90
    forest = smooth(tree_line + 140, tree_line - 160, z) * smooth(55, 45, slope)
    n4 = noise(sh, 5, 4, 2)
    forest *= smooth(0.22, 0.40, n4 * 0.5 + n2 * 0.5)   # small gaps / clearings
    cliff = smooth(36, 48, slope) * smooth(1500, 1950, z)   # bare rock: steep ground above the trees
    runs = F["runs"]
    forest *= 1 - runs
    snowline = 1150 + (n1 - 0.5) * 150
    snow = smooth(snowline - 40, snowline + 120, z) * (1 - 0.85 * cliff)
    ridge = np.clip(rel / 25, -1, 1)

    if style == "relief":
        sun = light(F["n"], 315, 40)
        base = np.ones(sh + (3,), np.float32) * np.array([0.95, 0.95, 0.94])
        tint = mix(np.array([0.83, 0.88, 0.80]), np.array([0.93, 0.93, 0.92]), smooth(1200, 2600, z))
        base = mix(base, tint, 0.9)
        base = mix(base, np.array([0.70, 0.78, 0.68]), forest * 0.75)
        base = mix(base, np.array([0.80, 0.78, 0.75]), cliff * 0.6)
        base = mix(base, np.array([0.99, 0.99, 1.0]), snow * 0.8)
        shade = 0.55 + 0.55 * sun + 0.08 * ridge
        col = base * shade[..., None]
        # contours every 100 ft, index every 1000 ft
        zf = z * 3.28084
        for step, w, c in ((500, 1.2, 0.30),):
            g = np.abs(((zf + step / 2) % step) - step / 2)
            gy, gx = np.gradient(zf, dy, dx)
            px = np.hypot(gx, gy) * max(dx, dy)   # ft per cell
            line = smooth(w * px, 0.2 * px, g)
            col = mix(col, np.array([0.45, 0.40, 0.36]), line * c)
        return {"col": np.clip(col, 0, 1), "forest": forest * 0.0, "snow": snow}

    if style == "paint":
        # direct sun (gone under overcast) + soft skylight (all of it under overcast)
        sky = 0.5 + 0.5 * F["n"][..., 2]
        sun = light(F["n"], sun_az, sun_alt) * (1 - ov) * smooth(-4, 6, sun_alt) + ov * (0.38 + 0.25 * sky)
        rock = mix(np.array([0.36, 0.33, 0.32]), np.array([0.52, 0.47, 0.43]), n2)
        meadow = mix(np.array([0.55, 0.58, 0.40]), np.array([0.66, 0.62, 0.45]), n1)
        trees = mix(np.array([0.07, 0.16, 0.12]), np.array([0.12, 0.23, 0.15]), n3)
        snow_lit = mix(np.array([1.0, 0.97, 0.93]), np.array([0.90, 0.91, 0.93]), ov)
        snow_sh = mix(np.array([0.56, 0.65, 0.84]), np.array([0.70, 0.73, 0.78]), ov)
        if sun_alt < 12:   # low sun: warm light, pink on the high snow (alpenglow)
            warm = (1 - ov) * smooth(12, 2, sun_alt)
            snow_lit = mix(snow_lit, np.array([1.0, 0.80, 0.66]), warm)
            snow_sh = mix(snow_sh, np.array([0.60, 0.58, 0.78]), warm * 0.6)
        ground = mix(meadow, rock, smooth(1700, 2100, z) * 0.7 + cliff * 0.3)
        lit = ground * (0.35 + 0.85 * sun)[..., None]
        sn = mix(snow_sh, snow_lit, smooth(0.15, 0.8, sun)) * (0.88 + 0.14 * sky)[..., None]
        col = mix(lit, sn, np.maximum(snow * smooth(0.25, 0.45, n2 * 0.4 + 0.6 - cliff * 0.5), runs * snow))
        stand = noise(sh, 4, 5, 2)   # tree stands / openings, ~40 m
        tr = trees * (0.55 + 0.6 * sun)[..., None] * (0.75 + 0.5 * stand)[..., None]
        tr = mix(tr, sn * 0.92, snow * 0.25 * smooth(0.5, 0.8, n3))   # snow caught in the trees
        col = mix(col, tr, forest)
        col *= (0.9 + 0.18 * np.clip(rel / 30, -1, 1))[..., None]   # ridges catch light, gullies hold shade
        return {"col": np.clip(col, 0, 1), "forest": forest, "snow": snow, "sun": sun}

    if style == "night":
        moon = light(F["n"], 160, 35)
        sky = 0.5 + 0.5 * F["n"][..., 2]
        rock = np.array([0.20, 0.21, 0.25])
        trees = np.array([0.06, 0.09, 0.10])
        sn = mix(np.array([0.23, 0.29, 0.42]), np.array([0.62, 0.70, 0.86]), smooth(0.1, 0.85, moon))
        ground = rock * (0.4 + 0.8 * moon)[..., None]
        col = mix(ground, sn * (0.9 + 0.12 * sky)[..., None], snow * smooth(0.25, 0.45, n2 * 0.4 + 0.6 - cliff * 0.5))
        col = mix(col, trees * (0.6 + 0.6 * moon)[..., None] + 0.02, forest)
        col *= (0.9 + 0.2 * np.clip(rel / 30, -1, 1))[..., None]
        return {"col": np.clip(col, 0, 1), "forest": forest, "snow": snow, "sun": moon}
    raise ValueError(style)


SKIES = {
    "relief": ((0.97, 0.97, 0.96), (0.93, 0.94, 0.95), (0.93, 0.94, 0.95), 0.0),
    "paint": ((0.52, 0.68, 0.86), (0.93, 0.89, 0.84), (0.80, 0.85, 0.90), 1 / 24000),
    "night": ((0.04, 0.05, 0.10), (0.16, 0.19, 0.30), (0.12, 0.15, 0.24), 1 / 11000),
}


def compose(B, z, dx, dy, tex, style):
    h, w = B["x"].shape
    top, hor, haze, k = SKIES[style]
    v = np.linspace(0, 1, h)[:, None]
    img = np.broadcast_to(mix(np.array(top), np.array(hor), smooth(0.0, 0.55, v)), (h, w, 3)).copy()
    hit = ~np.isnan(B["dist"])
    X, Y, dist = B["x"][hit], B["y"][hit], B["dist"][hit]
    col = np.stack([bilinear(tex["col"][..., c], X, Y, dx, dy) for c in range(3)], -1)
    fo = bilinear(tex["forest"], X, Y, dx, dy)
    if False and fo.max() > 0:   # per-tree crowns: sub-pixel at this distance, read as speckle
        # individual crowns (~6 m) fading out with distance (where a pixel covers many trees)
        crown = vnoise(X, Y, 6.0, 1) * 0.65 + vnoise(X, Y, 2.5, 2) * 0.35
        near = np.exp(-dist / 6000)
        sn = bilinear(tex["snow"], X, Y, dx, dy)
        dark = smooth(0.42, 0.62, crown) * fo * near
        gap = smooth(0.40, 0.25, crown) * fo * near * sn
        tree_col = np.array([0.06, 0.13, 0.12]) if style == "paint" else np.array([0.03, 0.05, 0.06])
        gap_col = np.array([0.80, 0.85, 0.92]) if style == "paint" else np.array([0.30, 0.36, 0.50])
        col = col * (1 - 0.55 * dark[:, None]) + tree_col * 0.55 * dark[:, None]
        col = col * (1 - 0.5 * gap[:, None]) + gap_col * 0.5 * gap[:, None]
    if k:
        f = (1 - np.exp(-B["dist"][hit] * k))[:, None]
        col = col * (1 - f) + np.array(haze) * f
    img[hit] = col
    if style == "night":   # a few stars
        rng = np.random.default_rng(7)
        sy, sx = rng.integers(0, int(h * 0.45), 160), rng.integers(0, w, 160)
        ok = ~hit[sy, sx]
        img[sy[ok], sx[ok]] = np.clip(img[sy[ok], sx[ok]] + rng.uniform(0.25, 0.7, (ok.sum(), 1)), 0, 1)
    return img


def sky_image(h, w, cover, sun_alt, ov):
    """Sky gradient: clear blue -> grey with cloud cover; scattered clouds drawn as soft noise."""
    v = np.linspace(0, 1, h)[:, None, None]
    clear_top, clear_hor = np.array([0.45, 0.63, 0.85]), np.array([0.93, 0.89, 0.84])
    if sun_alt < 12:
        clear_hor = mix(clear_hor, np.array([0.98, 0.78, 0.60]), smooth(12, 2, sun_alt))
    grey_top, grey_hor = np.array([0.62, 0.65, 0.70]), np.array([0.78, 0.80, 0.83])
    top, hor = mix(clear_top, grey_top, ov), mix(clear_hor, grey_hor, ov)
    img = np.broadcast_to(top * (1 - smooth(0, 0.55, v)) + hor * smooth(0, 0.55, v), (h, w, 3)).copy()
    if 0.05 < cover and ov < 0.95:   # scattered / broken cloud: puffs, flatter and smaller toward the horizon
        yy, xx = np.mgrid[0:h, 0:w].astype(np.float32)
        persp = 1 + 3 * (yy / h)
        n = vnoise(xx / 1.0, yy * persp, 160, 11) * 0.6 + vnoise(xx, yy * persp, 55, 12) * 0.3 + vnoise(xx, yy, 18, 13) * 0.1
        a = smooth(1 - cover * 0.9, 1.05 - cover * 0.6, n) * (1 - ov)
        shade = 0.88 + 0.12 * smooth(0.4, 0.9, n)
        cl = np.array([0.98, 0.98, 0.99]) * shade[..., None]
        if sun_alt < 12:
            cl = mix(cl, np.array([1.0, 0.78, 0.68]) * shade[..., None], smooth(12, 2, sun_alt) * 0.75)
        img = img * (1 - a[..., None]) + cl * a[..., None]
    return img


def weather_scene(B, z, dx, dy, F, wx, rng_seed=5):
    """One hour's weather on the painting (the page will do the same per frame):
    wx = {sun_az, sun_alt, cover 0-1, ov 0-1 (overcast light), base_ft (cloud deck base, mountain hidden above it),
          sea_ft (top of a cloud sea below the viewer, summit above it), pop 0-1, snow_level_ft, vis_mi, wind_mph}"""
    h, w = B["x"].shape
    ov = wx.get("ov", 0.0)
    tex = paint(z, dx, dy, F, "paint", wx.get("sun_az", 230), wx.get("sun_alt", 28), ov)
    hit = ~np.isnan(B["dist"])
    X, Y, dist, el = B["x"][hit], B["y"][hit], B["dist"][hit], B["elev"][hit]
    col = np.stack([bilinear(tex["col"][..., c], X, Y, dx, dy) for c in range(3)], -1)
    img = sky_image(h, w, wx.get("cover", 0), wx.get("sun_alt", 28), ov)
    # distance haze; thicker in low visibility and under cloud
    vis_m = wx.get("vis_mi", 30) * 1609
    k = max(1 / 24000, 1.6 / vis_m)
    haze = mix(np.array([0.80, 0.85, 0.90]), np.array([0.76, 0.78, 0.81]), ov)
    f = (1 - np.exp(-dist * k))[:, None]
    col = col * (1 - f) + haze * f
    wisp = vnoise(X, Y, 260, 21) * 0.6 + vnoise(X, Y, 90, 22) * 0.4   # ragged cloud edges, fixed to the ground
    cloud = np.zeros(len(X))   # how hidden each pixel is (lifts are hidden where it's thick)
    if wx.get("base_ft"):   # a deck: the mountain disappears into it above the base
        base = wx["base_ft"] / 3.28084
        a = smooth(base - 40, base + 260, el + (wisp - 0.5) * 220)
        cc = img[hit] * (0.93 + 0.12 * wisp)[:, None]
        col = col * (1 - a[:, None]) + cc * a[:, None]
        cloud = np.maximum(cloud, a)
    if wx.get("sea_ft"):    # a cloud sea: everything below its top is under a bright, billowy surface
        top = wx["sea_ft"] / 3.28084
        bill = vnoise(X, Y, 420, 31) * 0.55 + vnoise(X, Y, 140, 32) * 0.3 + vnoise(X, Y, 45, 33) * 0.15
        a = smooth(top + 40, top - 140, el + (bill - 0.5) * 180)
        lit = 0.80 + 0.22 * smooth(0.3, 0.8, bill)
        cc = np.array([0.97, 0.97, 0.98]) * lit[:, None]
        cc = cc * (1 - f * 0.6) + haze * f * 0.6
        col = col * (1 - a[:, None]) + cc * a[:, None]
        cloud = np.maximum(cloud, a)
    img[hit] = col
    im = to_image(img)
    hide = np.zeros((h, w))
    hide[hit] = cloud
    im.hide = hide
    pop = wx.get("pop", 0)
    if pop >= 0.2:   # rain streaks below the snow level, flakes above, only under the cloud base
        d = ImageDraw.Draw(im, "RGBA")
        rng = np.random.default_rng(rng_seed)
        n = int(7000 * pop ** 1.3 * w * h / 1.6e6)
        xs, ys = rng.uniform(0, w, n), rng.uniform(0, h, n)
        sl = wx.get("snow_level_ft", 0) / 3.28084
        base = wx["base_ft"] / 3.28084 if wx.get("base_ft") else 1e9
        slant = min(0.6, wx.get("wind_mph", 0) / 40)
        for x, y in zip(xs, ys):
            ix, iy = int(x), int(y)
            dd, e = B["dist"][iy, ix], B["elev"][iy, ix]
            if np.isnan(dd) or e > base:
                continue
            near = float(np.clip(3500 / dd, 0.45, 1.6))
            if e < sl:
                L = 24 * near
                d.line([(x, y), (x - slant * L, y + L)], fill=(215, 230, 238, int(150 * min(1, near))), width=2 if near > 1 else 1)
            else:
                r = 2.4 * near
                d.ellipse([x - r - 0.8, y - r - 0.8, x + r + 0.8, y + r + 0.8], fill=(110, 120, 138, int(70 * min(1, near))))
                d.ellipse([x - r, y - r, x + r, y + r], fill=(255, 255, 255, int(235 * min(1, near))))
    return im


TEMP_RAMP = [(-10, (75, 44, 127)), (5, (90, 79, 176)), (20, (79, 127, 201)), (29, (140, 195, 234)), (32, (250, 252, 255)),
             (35, (189, 227, 214)), (45, (124, 196, 122)), (60, (233, 201, 90)), (75, (242, 154, 59)), (90, (224, 64, 47))]


def ramp(v, R):
    xs = np.array([a for a, _ in R], np.float32)
    out = np.stack([np.interp(v, xs, [c[k] for _, c in R]) for k in range(3)], -1)
    return out / 255


def temp_layer(img, B, hl_elev_m, temps_f, alpha=0.55):
    """Recolour by the forecast temperature at each pixel's elevation (what the page will do per frame)."""
    hit = ~np.isnan(B["dist"])
    t = np.interp(B["elev"][hit], hl_elev_m, temps_f)
    c = ramp(t, TEMP_RAMP)
    lum = img[hit].mean(-1, keepdims=True)
    shaded = c * (0.45 + 0.75 * lum)   # keep the terrain's light and shade under the colour
    img = img.copy()
    img[hit] = img[hit] * (1 - alpha) + shaded * alpha
    return img


def font(px, bold=True):
    for f in (("segoeuib.ttf" if bold else "segoeui.ttf"), "arialbd.ttf", "arial.ttf"):
        try:
            return ImageFont.truetype(os.path.join(os.environ.get("WINDIR", "C:/Windows"), "Fonts", f), px)
        except OSError:
            continue
    return ImageFont.load_default()


def overlays(im, cam, B, z, dx, dy, points, dark=False):
    """Lifts (cables, hidden behind terrain) and the forecast points as pins + labels."""
    hide = getattr(im, "hide", None)
    d = ImageDraw.Draw(im, "RGBA")
    dist = B["dist"]
    h, w = dist.shape
    cable = (20, 24, 30, 200) if not dark else (225, 230, 240, 190)
    for e in osm():
        if e.get("tags", {}).get("aerialway") not in ("chair_lift", "gondola", "mixed_lift") or "geometry" not in e:
            continue
        g = e["geometry"]
        lon = np.array([q["lon"] for q in g])
        lat = np.array([q["lat"] for q in g])
        x, y = to_xy(lon, lat)
        zz = bilinear(z, x, y, dx, dy) + 12
        k = np.linspace(0, len(x) - 1, 60)   # sample along the line, cable straight between end stations
        xs, ys = np.interp(k, range(len(x)), x), np.interp(k, range(len(y)), y)
        zs = np.interp(k, [0, len(x) - 1], [zz[0], zz[-1]])
        zs = np.maximum(zs, bilinear(z, xs, ys, dx, dy) + 8)
        sx, sy, f = cam.project(xs, ys, zs)
        seg = []
        for i in range(len(sx)):
            ix, iy = int(sx[i]), int(sy[i])
            vis = (0 <= ix < w and 0 <= iy < h and (np.isnan(dist[iy, ix]) or f[i] <= dist[iy, ix] * 1.02 + 15)
                   and (hide is None or hide[iy, ix] < 0.5))
            if vis:
                seg.append((sx[i], sy[i]))
            else:
                if len(seg) > 1:
                    d.line(seg, fill=cable, width=2)
                seg = []
        if len(seg) > 1:
            d.line(seg, fill=cable, width=2)
    fb, fs = font(24), font(20, False)
    for p in points:
        x, y = to_xy(p["lon"], p["lat"])
        sx, sy, _ = cam.project(np.array([x]), np.array([y]), np.array([p["elev_ft"] / 3.28084]))
        sx, sy = float(sx[0]), float(sy[0])
        d.ellipse([sx - 8, sy - 8, sx + 8, sy + 8], fill=(255, 255, 255, 255), outline=p["color"], width=4)
        label, val = p["name"], p.get("val", "")
        tw = d.textlength(label, font=fb) + (d.textlength("  " + val, font=fs) if val else 0)
        bx, by = sx + 16, sy - 40
        d.rounded_rectangle([bx, by, bx + tw + 24, by + 38], radius=19,
                            fill=(26, 29, 34, 225) if dark else (255, 255, 255, 235))
        d.text((bx + 12, by + 4), label, font=fb, fill=(236, 239, 243) if dark else (17, 17, 17))
        if val:
            d.text((bx + 12 + d.textlength(label, font=fb), by + 7), "  " + val, font=fs,
                   fill=(178, 184, 195) if dark else (90, 95, 107))
    return im


BAKED_HAZE = 1 / 24000   # the paintings' own distance haze (the page adds more in low visibility)
DIST_LO, DIST_HI = 300.0, 60000.0   # distance code 0-254 = log scale between these (m); 255 = sky


def export(out, B, z, dx, dy, F, cam):
    """The Mt Hood tab's assets: three skyless paintings (clear sun, overcast, low golden sun), half-resolution
    buffers (data.png: R,G = elevation in decimetres, B = log distance code; noise.png: R = cloud wisps on the
    ground / cloud puffs in the sky, G = cloud-sea billows, B = 255 on terrain) and scene.json (lift lines with
    each vertex's terrain visibility, the three forecast points)."""
    import json
    os.makedirs(out, exist_ok=True)
    h, w = B["x"].shape
    hit = ~np.isnan(B["dist"])
    X, Y, dist = B["x"][hit], B["y"][hit], B["dist"][hit]
    for name, kw in (("clear", {"sun_az": 230, "sun_alt": 28, "ov": 0.0}), ("flat", {"ov": 1.0}),
                     ("gold", {"sun_az": 250, "sun_alt": 4, "ov": 0.0})):
        tex = paint(z, dx, dy, F, "paint", **kw)
        col = np.stack([bilinear(tex["col"][..., c], X, Y, dx, dy) for c in range(3)], -1)
        f = (1 - np.exp(-dist * BAKED_HAZE))[:, None]
        col = col * (1 - f) + np.array([0.80, 0.85, 0.90]) * f
        img = np.zeros((h, w, 3), np.float32)
        img[hit] = col
        to_image(img).save(os.path.join(out, f"{name}.jpg"), quality=82, optimize=True, progressive=True)
    # half-resolution buffers (nearest), what the page reads per pixel
    hs = (slice(0, h, 2), slice(0, w, 2))
    hb = hit[hs]
    el = np.nan_to_num(B["elev"][hs]) * 10
    el = np.clip(np.round(el), 0, 65535).astype(np.uint16)
    dc = np.where(hb, np.clip(np.round(254 * np.log(np.clip(np.nan_to_num(B["dist"][hs], nan=DIST_HI), DIST_LO, DIST_HI) / DIST_LO)
                                      / np.log(DIST_HI / DIST_LO)), 0, 254), 255).astype(np.uint8)
    data = np.stack([(el >> 8).astype(np.uint8), (el & 255).astype(np.uint8), dc], -1)
    Image.fromarray(data).save(os.path.join(out, "data.png"), optimize=True)
    hx, hy = np.nan_to_num(B["x"][hs]), np.nan_to_num(B["y"][hs])
    wisp = vnoise(hx, hy, 260, 21) * 0.6 + vnoise(hx, hy, 90, 22) * 0.4
    bill = vnoise(hx, hy, 420, 31) * 0.55 + vnoise(hx, hy, 140, 32) * 0.3 + vnoise(hx, hy, 45, 33) * 0.15
    yy, xx = np.mgrid[0:h:2, 0:w:2].astype(np.float32)   # sky puffs: flatter and smaller toward the horizon
    persp = 1 + 3 * (yy / h)
    sky = vnoise(xx, yy * persp, 160, 11) * 0.6 + vnoise(xx, yy * persp, 55, 12) * 0.3 + vnoise(xx, yy, 18, 13) * 0.1
    sky2 = vnoise(xx, yy * persp, 70, 14)
    r = np.where(hb, wisp, sky)
    g = np.where(hb, bill, sky2)
    noise_img = np.stack([np.round(r * 255), np.round(g * 255), hb * 255.0], -1).astype(np.uint8)
    Image.fromarray(noise_img).save(os.path.join(out, "noise.png"), optimize=True)
    # lifts: screen polylines, each vertex flagged visible (1) or behind terrain (0)
    dist_full = B["dist"]
    lifts = []
    for e in osm():
        t = e.get("tags", {})
        if t.get("aerialway") not in ("chair_lift", "gondola", "mixed_lift") or "geometry" not in e:
            continue
        g_ = e["geometry"]
        x, y = to_xy(np.array([q["lon"] for q in g_]), np.array([q["lat"] for q in g_]))
        zz = bilinear(z, x, y, dx, dy) + 12
        k = np.linspace(0, len(x) - 1, 48)
        xs, ys = np.interp(k, range(len(x)), x), np.interp(k, range(len(y)), y)
        zs = np.maximum(np.interp(k, [0, len(x) - 1], [zz[0], zz[-1]]), bilinear(z, xs, ys, dx, dy) + 8)
        sx, sy, f = cam.project(xs, ys, zs)
        pts = []
        for i in range(len(sx)):
            ix, iy = int(sx[i]), int(sy[i])
            v = 0 <= ix < w and 0 <= iy < h and (np.isnan(dist_full[iy, ix]) or f[i] <= dist_full[iy, ix] * 1.02 + 15)
            pts.append([round(float(sx[i]), 1), round(float(sy[i]), 1), int(v)])
        lifts.append({"name": t.get("name", ""), "pts": pts})
    points = []
    for name, lat, lon, ft in (("Meadows Base", 45.33144, -121.66413, 5361), ("Top of Blue", 45.34425, -121.67252, 6614),
                               ("Top of Cascade", 45.34971, -121.68164, 7343)):
        x, y = to_xy(np.array([lon]), np.array([lat]))
        sx, sy, _ = cam.project(x, y, np.array([ft / 3.28084]))
        points.append({"name": name, "x": round(float(sx[0]), 1), "y": round(float(sy[0]), 1)})
    with open(os.path.join(out, "scene.json"), "w", encoding="utf-8") as fh:
        json.dump({"w": w, "h": h, "bw": w // 2, "bh": h // 2, "haze": BAKED_HAZE, "dlo": DIST_LO, "dhi": DIST_HI,
                   "lifts": lifts, "points": points}, fh, separators=(",", ":"))
    for fn in sorted(os.listdir(out)):
        print(f"  {fn}: {os.path.getsize(os.path.join(out, fn)) / 1024:.0f} KB")


def to_image(a):
    return Image.fromarray((np.clip(a, 0, 1) * 255 + 0.5).astype(np.uint8))


def make_camera(z, w=1600, h=1000):
    tlon, tlat = -121.6730, 45.3355
    tx, ty = to_xy(tlon, tlat)
    _, dx, dy = None, (BBOX[2] - BBOX[0]) / z.shape[1] * MX, (BBOX[3] - BBOX[1]) / z.shape[0] * MY
    tz = float(bilinear(z, np.array([tx]), np.array([ty]), dx, dy)[0])
    return Camera((tlon, tlat), dist=5600, pitch_deg=15, fov_y_deg=36.9, w=w, h=h, ex=1.35, zref=1200, target_z=tz,
                  shift_y=0.32, bearing=-14)


if __name__ == "__main__":
    import time
    out = sys.argv[1] if len(sys.argv) > 1 else CACHE
    os.makedirs(out, exist_ok=True)
    z, dx, dy = load_dem()
    cam = make_camera(z)
    bp = os.path.join(CACHE, f"buffers_{cam.w}x{cam.h}.npz")
    if os.path.exists(bp) and "--retrace" not in sys.argv:   # the camera hasn't changed: reuse the trace
        B = dict(np.load(bp))
    else:
        t0 = time.time()
        B = trace(z, dx, dy, cam)
        print(f"traced {cam.w}x{cam.h} in {time.time() - t0:.0f}s; sky {np.isnan(B['dist']).mean():.0%}")
        np.savez_compressed(bp, **B)
    F = terrain_fields(z, dx, dy)
    pts = [{"name": "Meadows Base", "lat": 45.33144, "lon": -121.66413, "elev_ft": 5361, "color": "#6BBF68"},
           {"name": "Top of Blue", "lat": 45.34425, "lon": -121.67252, "elev_ft": 6614, "color": "#4FB1BE"},
           {"name": "Top of Cascade", "lat": 45.34971, "lon": -121.68164, "elev_ft": 7343, "color": "#525D45"}]
    hl = None
    try:   # the forecast's elevation layers from the last build, for the temperature mockup
        import json
        page = open(os.path.join(ROOT, "docs", "index.html"), encoding="utf-8").read()
        i = page.index("window.TerrainLayers(map,") + len("window.TerrainLayers(map,")
        hl = json.JSONDecoder().raw_decode(page[i:])[0]
    except Exception as exc:
        print("no layer data:", exc)
    if "--export" in sys.argv:   # the Mt Hood tab's assets (committed in hood3d/, copied next to the page each build)
        export(os.path.join(ROOT, "hood3d"), B, z, dx, dy, F, cam)
        sys.exit(0)
    if "--weather" in sys.argv:
        scenes = {
            "1_clear": {"sun_az": 235, "sun_alt": 22, "cover": 0.05},
            "2_deck_6500": {"sun_az": 200, "sun_alt": 30, "cover": 1.0, "ov": 0.85, "base_ft": 6500, "vis_mi": 12},
            "3_cloud_sea_5800": {"sun_az": 140, "sun_alt": 18, "cover": 0.1, "sea_ft": 5800},
            "4_snow_above_5500": {"sun_az": 200, "sun_alt": 25, "cover": 1.0, "ov": 1.0, "base_ft": 7800, "pop": 0.9,
                                  "snow_level_ft": 5500, "vis_mi": 5, "wind_mph": 15},
            "5_partly_sunset": {"sun_az": 250, "sun_alt": 5, "cover": 0.45},
        }
        for name, wx in scenes.items():
            im = weather_scene(B, z, dx, dy, F, wx)
            overlays(im, cam, B, z, dx, dy, pts).save(os.path.join(out, f"wx_{name}.png"))
            print("wrote", name)
        sys.exit(0)
    for style in ("relief", "paint", "night"):
        tex = paint(z, dx, dy, F, style)
        img = compose(B, z, dx, dy, tex, style)
        to_image(img).save(os.path.join(out, f"hood_{style}_plain.png"))
        overlays(to_image(img), cam, B, z, dx, dy, pts, dark=style == "night").save(os.path.join(out, f"hood_{style}.png"))
        if hl and style in ("paint", "night"):
            fr = max(hl["frames"], key=lambda q: q["temp"][len(q["temp"]) // 3])   # the warmest hour mid-mountain
            tp = [dict(q, val=str(round(float(np.interp(q["elev_ft"] / 3.28084, hl["elev"], fr["temp"])))) + "\u00b0F")
                  for q in pts]
            ti = temp_layer(img, B, hl["elev"], fr["temp"])
            overlays(to_image(ti), cam, B, z, dx, dy, tp, dark=style == "night").save(os.path.join(out, f"hood_{style}_temp.png"))
        print("wrote", style)
