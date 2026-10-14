"""
Home-screen widgets as pictures (owner, 2026-10-13): each build draws them as PNGs next to the
dashboard, and the phone shows them with KWGT's image element pointed at the URL (refreshed hourly).

  widget-week.png    Portland, 6 days: the day's weather art, the high/low bar on one scale, rain chance
  widget-snow.png    Meadows Base snow timeline: the last 7 days' snow-depth gain at the Mt Hood Test Site
                     SNOTEL (5,370 ft, at the base area; the model's estimate if SNOTEL is down) | today |
                     the next 7 days' forecast new snow

Drawn with Pillow at 2x and scaled down (smooth edges); the font is Pillow's built-in one (bold = a
same-colour stroke). The art follows cyArt() in CITY_JS: sun behind, clouds in front, rain/snow falling.
"""
import math
import os
from datetime import date, timedelta

import requests
from PIL import Image, ImageDraw, ImageFont

W, H, SS = 1000, 480, 2
INK, MUT, LINE, CARD = (17, 17, 17), (125, 133, 146), (238, 240, 243), (255, 255, 255, 246)
ORANGE, SNOW, SNOW2, RAIN, PAST = (254, 80, 0), (90, 79, 207), (185, 179, 240), (11, 111, 124), (169, 178, 191)
SNOTEL = "651:OR:SNTL"   # Mt Hood Test Site, 5,370 ft - at Meadows' base area
AWDB = "https://wcc.sc.egov.usda.gov/awdbRestApi/services/v1"
DOW = ["MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN"]


def _font(px):
    return ImageFont.load_default(size=px * SS)


def _text(d, xy, s, px, fill, bold=False, anchor="la"):
    d.text((xy[0] * SS, xy[1] * SS), s, font=_font(px), fill=fill, anchor=anchor,
           stroke_width=(1 if px < 24 else 2) if bold else 0, stroke_fill=fill)


def _card():
    im = Image.new("RGBA", (W * SS, H * SS), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    d.rounded_rectangle([0, 0, W * SS - 1, H * SS - 1], radius=44 * SS, fill=CARD)
    return im, d


def _header(d, label, dot, right=""):
    d.ellipse([30 * SS, 27 * SS, 46 * SS, 43 * SS], fill=dot)
    _text(d, (56, 35), label.upper(), 26, MUT, True, "lm")
    if right:
        _text(d, (W - 30, 35), right, 24, MUT, False, "rm")


def _save(im, path):
    im.resize((W, H), Image.LANCZOS).save(path)


# ---------- the weather art (cyArt in CITY_JS), in its 40x32 design units at (ox, oy), scale k px/unit ----------
def _art(d, ox, oy, k, day):
    P = lambda x, y: ((ox + x * k) * SS, (oy + y * k) * SS)
    R = lambda r: r * k * SS

    def sun(x, y, r):
        for i in range(8):
            a = i * math.pi / 4
            d.line([P(x + math.cos(a) * (r + 0.9), y + math.sin(a) * (r + 0.9)),
                    P(x + math.cos(a) * (r + 2.3), y + math.sin(a) * (r + 2.3))], fill=(242, 165, 22), width=max(1, int(R(0.9))))
        c = P(x, y)
        d.ellipse([c[0] - R(r), c[1] - R(r), c[0] + R(r), c[1] + R(r)], fill=(255, 200, 61), outline=(224, 147, 26), width=max(1, int(R(0.55))))

    def cloud(x, y, sc, col):   # three puffs on a flat base; outlined by drawing it a little larger in ink first
        parts = [(-2.3, 0.1, 2.0), (0.1, -1.2, 2.6), (2.7, 0.2, 1.8)]
        for grow, colour in ((0.5 / sc, (46, 52, 64)), (0, col)):
            for cx, cy, r in parts:
                c = P(x + cx * sc, y + cy * sc)
                rr = R((r + grow) * sc)
                d.ellipse([c[0] - rr, c[1] - rr, c[0] + rr, c[1] + rr], fill=colour)
            a, b = P(x + (-4.5 - grow) * sc, y - 0.3 * sc), P(x + (4.5 + grow) * sc, y + (2 + grow) * sc)
            d.rounded_rectangle([a[0], a[1], b[0], b[1]], radius=R(1.0 * sc), fill=colour)

    def fall(kind, cols, x0, x1, y0):
        n = 0
        for c in range(cols):
            for row in range(3):
                x = x0 + (x1 - x0) * (c / (cols - 1) if cols > 1 else 0.5) + (1.1 if row % 2 else 0)
                y = y0 + row * 4.2 + (1.6 if c % 2 else 0)
                k2 = ("snow" if n % 2 else "rain") if kind == "mix" else kind
                n += 1
                if k2 == "snow":
                    c0 = P(x, y)
                    d.ellipse([c0[0] - R(0.95), c0[1] - R(0.95), c0[0] + R(0.95), c0[1] + R(0.95)], fill="white", outline=SNOW, width=max(1, int(R(0.5))))
                else:
                    d.line([P(x, y), P(x - 0.8, y + 2.5)], fill=(14, 154, 174), width=max(1, int(R(0.85))))

    def wind():
        for y, x0, x1 in ((21, 1, 12), (24.5, -1, 15), (28, 2, 10)):
            d.line([P(x0, y), P(x1, y)], fill=(54, 137, 148), width=max(1, int(R(0.8))))

    Wh, G, Dk = (244, 246, 249), (201, 208, 218), (169, 178, 191)
    r, s, pop, g, sky = day["r"], day["s"], day["pop"], day["g"], day["sky"]
    ty = "rain"
    if s >= 0.05 and r > 0:
        f = s / (r * 10)
        ty = "snow" if f >= 0.8 else "mix" if f > 0.2 else "rain"
    wet, cols = r >= 0.01 and pop >= 30, (5 if pop >= 75 else 4 if pop >= 50 else 3)
    if wet and (pop >= 70 or r >= 0.15):
        cloud(14, 9, 1.55, Dk); cloud(24, 10.5, 1.25, G); fall(ty, cols, 9, 29, 15)
    elif wet:
        sun(13, 8, 3.6); cloud(22, 11, 1.5, G); fall(ty, min(cols, 3), 17, 27, 16)
    elif sky < 20:
        sun(20, 13, 5.2)
    elif sky < 40:
        sun(17, 11, 4.6); cloud(26, 17, 1.15, Wh)
    elif sky < 65:
        sun(15, 10, 4.2); cloud(23, 15.5, 1.6, Wh)
    elif sky < 85:
        sun(13, 8.5, 3.4); cloud(17, 15, 1.5, Wh); cloud(26, 13, 1.3, Wh)
    else:
        cloud(14, 12, 1.6, G); cloud(25, 14, 1.45, Wh)
    if g >= 30:
        wind()


def _temp_col(t):
    return (237, 30, 41) if t >= 80 else (250, 162, 27) if t >= 70 else (107, 191, 104) if t >= 50 else (79, 177, 190) if t >= 35 else (54, 137, 148)


def week(name, days, path, today):
    """days: the Cities table's per-day dicts (dk, hi, lo, mp, rt, st, mg, sky), from today on."""
    days = [x for x in days if date.fromisoformat(x["dk"]) >= today][:6]   # (a cached forecast can start yesterday)
    im, d = _card()
    _header(d, name, ORANGE, "6 days")
    lo = min(x["lo"] for x in days) - 3
    hi = max(x["hi"] for x in days) + 3
    cw = (W - 24) / len(days)
    top, bot = 266, 392
    for i, x in enumerate(days):
        cx = 12 + cw * i + cw / 2
        dd = date.fromisoformat(x["dk"])
        _text(d, (cx, 84), "TODAY" if dd == today else DOW[dd.weekday()], 30, ORANGE if dd == today else INK, True, "mm")
        _art(d, cx - 90, 102, 4.5, {"sky": x["sky"], "r": x["rt"], "s": x["st"], "pop": x["mp"] or 0, "g": x["mg"] or 0})
        y1 = top + (hi - x["hi"]) / (hi - lo) * (bot - top)
        y2 = bot - (x["lo"] - lo) / (hi - lo) * (bot - top)
        d.rounded_rectangle([(cx - 8) * SS, top * SS, (cx + 8) * SS, bot * SS], radius=8 * SS, fill=LINE)
        bar = Image.new("RGBA", (1, 2))
        bar.putpixel((0, 0), _temp_col(x["hi"]) + (255,)); bar.putpixel((0, 1), _temp_col(x["lo"]) + (255,))
        bh = max(16, int((y2 - y1) * SS))
        grad = bar.resize((16 * SS, bh), Image.BILINEAR)
        mask = Image.new("L", grad.size, 0)
        ImageDraw.Draw(mask).rounded_rectangle([0, 0, grad.size[0] - 1, grad.size[1] - 1], radius=8 * SS, fill=255)
        im.paste(grad, (int((cx - 8) * SS), int(y1 * SS)), mask)
        _text(d, (cx, y1 - 24), f"{x['hi']:.0f}°", 40, INK, True, "mm")
        _text(d, (cx, y1 + bh / SS + 22), f"{x['lo']:.0f}°", 32, MUT, False, "mm")
        p = round(x["mp"] or 0)
        _text(d, (cx, 452), f"{p}%", 33, RAIN if p >= 10 else (194, 198, 206), True, "mm")
    _save(im, path)


def snotel_gain(today, n=7):
    """[(date, inches)] - the last n days' snow-depth gain at the Mt Hood Test Site, or None."""
    try:
        data = requests.get(f"{AWDB}/data", params={"stationTriplets": SNOTEL, "elements": "SNWD", "duration": "DAILY",
                            "beginDate": (today - timedelta(days=n + 1)).isoformat(), "endDate": today.isoformat()}, timeout=60).json()
        vals = {v["date"][:10]: v["value"] for rec in data for el in rec.get("data", []) for v in el.get("values", [])
                if v.get("value") is not None}
    except Exception as exc:
        print(f"  WARNING: widget SNOTEL depths unavailable ({exc})")
        return None
    out = []
    for k in range(n, 0, -1):
        dd, prev = today - timedelta(days=k), today - timedelta(days=k + 1)
        a, b = vals.get(dd.isoformat()), vals.get(prev.isoformat())
        if a is None or b is None:
            return None
        out.append((dd, max(0.0, float(a) - float(b)) if a >= 4 else 0.0))   # under 4": sensor noise on bare ground
    return out


def snow_timeline(past, fut, path, today, measured=True):
    """past: [(date, in)] x7 before today; fut: [(date, in)] today + 6 days of forecast new snow."""
    im, d = _card()
    _header(d, "Meadows Base snow", SNOW, "5,361 ft")
    vals = [v for _, v in past] + [v for _, v in fut]
    mx = max([4.0] + vals)
    n, x0, x1, base, top = len(vals), 22, W - 22, 318, 108
    bw = (x1 - x0) / n
    for i, (dd, v) in enumerate(past + fut):
        f = i >= len(past)
        col = (SNOW2 if i >= len(past) + 5 else SNOW) if f else PAST
        h = v / mx * (base - top)
        bx = x0 + i * bw + 5
        d.rounded_rectangle([bx * SS, (base - max(4, h)) * SS, (bx + bw - 10) * SS, base * SS], radius=8 * SS, fill=col)
        if v >= 0.5:
            _text(d, (bx + (bw - 10) / 2, base - h - 20), f"{v:.0f}" if v >= 1.5 else f"{v:.1f}", 28, SNOW if f else MUT, True, "mm")
        _text(d, (bx + (bw - 10) / 2, base + 28), DOW[dd.weekday()][0], 26, (154, 160, 172), True, "mm")
    tx = x0 + len(past) * bw
    for y in range(int(top) - 6, base + 4, 10):   # today: a dashed orange line
        d.line([(tx * SS, y * SS), (tx * SS, (y + 5) * SS)], fill=ORANGE, width=3 * SS // 2)
    _text(d, (tx + 7, top - 8), "TODAY", 24, ORANGE, True, "lm")
    ps, fs = sum(v for _, v in past), sum(v for _, v in fut)
    _text(d, (30, 384), "LAST 7 DAYS", 24, MUT, True)
    big = f'{ps:.0f}"' if ps >= 1 or ps < 0.05 else f'{ps:.1f}"'
    _text(d, (30, 414), big, 50, (90, 95, 107), True)
    _text(d, (30 + _font(50).getlength(big) / SS + 12, 436), "depth gained" if measured else "estimated", 26, MUT)
    _text(d, (W - 30, 384), "NEXT 7 DAYS", 24, MUT, True, "ra")
    _text(d, (W - 30, 414), f'{fs:.0f}" forecast' if fs >= 1 or fs < 0.05 else f'{fs:.1f}" forecast', 50, SNOW, True, "ra")
    _save(im, path)


def render(out_dir, city_days, hood_past, hood_fut, today):
    try:   # what was drawn, so `python widgets.py` can redraw without a build
        import json
        os.makedirs(".cache", exist_ok=True)
        with open(os.path.join(".cache", "widget_data.json"), "w", encoding="utf-8") as f:
            json.dump({"today": today.isoformat(), "city_days": city_days,
                       "past": [(d.isoformat(), v) for d, v in hood_past], "fut": [(d.isoformat(), v) for d, v in hood_fut]}, f)
    except Exception:
        pass
    """Draw both widgets into out_dir; any failure only costs that widget (the build goes on)."""
    try:
        week("Portland", city_days, os.path.join(out_dir, "widget-week.png"), today)
    except Exception as exc:
        print(f"  WARNING: week widget not drawn ({exc})")
    try:
        g = snotel_gain(today)
        snow_timeline(g or hood_past[-7:], hood_fut[:7], os.path.join(out_dir, "widget-snow.png"), today, measured=bool(g))
    except Exception as exc:
        print(f"  WARNING: snow widget not drawn ({exc})")


if __name__ == "__main__":   # redraw from the last build's data (tuning the look without a build)
    import json
    with open(os.path.join(".cache", "widget_data.json"), encoding="utf-8") as f:
        D = json.load(f)
    t = date.fromisoformat(D["today"])
    render("docs", D["city_days"], [(date.fromisoformat(a), v) for a, v in D["past"]],
           [(date.fromisoformat(a), v) for a, v in D["fut"]], t)
    print("redrawn")
