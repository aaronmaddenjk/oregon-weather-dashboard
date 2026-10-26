"""
The daily Mt Hood email (owner, 2026-10-14): built from what the dashboard build saved in
.cache/newsletter_data.json (Base + Cascade by day, the past days' snow, Temira's Mt Hood snow forecast,
Chris Tomer's latest video) and sent from the owner's Gmail with an app password.

  python newsletter.py --out mail.html    write the email to a file (no sending)
  python newsletter.py --send             send it (once a day: .cache/newsletter_sent.txt)

Sending needs, in .env (never in code or git), either RESEND_API_KEY + NEWSLETTER_TO (Resend's free tier;
no Gmail 2-Step Verification needed) or GMAIL_USER + GMAIL_APP_PASSWORD (+ optional NEWSLETTER_TO).
tools/publish.sh sends it after the 6 AM build.
Email-safe HTML: one 600 px column of tables, inline styles only, no scripts or video (Gmail strips them).
"""
import base64
import html
import io
import json
import math
import os
import smtplib
import sys
from datetime import date, datetime
from email.mime.image import MIMEImage
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from zoneinfo import ZoneInfo

ROOT = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(ROOT, ".cache", "newsletter_data.json")
SENT = os.path.join(ROOT, ".cache", "newsletter_sent.txt")
SITE = "https://aaronmaddenjk.github.io/oregon-weather-dashboard/"
# dark, like the dashboard (owner, 2026-10-23); written as inline colours so every mail app shows it the same.
# GREEN = the accent as a fill (header band, button), GREEN_INK = the accent as text / lines on the dark card
GREEN, GREEN_T, GREEN_PALE, GREEN_INK = "#525D45", "#D9E3D2", "#222A1D", "#A7BE8E"
PLUM, PLUM_PALE, PLUM_PANEL = "#BFA3DD", "#6E5A86", "#2A2333"
INK, MUT, FAINT, RULE = "#ECEFF3", "#B2B8C3", "#949BA8", "#343A44"
PAGE, CARD, PANEL, DIM = "#0B0D10", "#1A1D22", "#1F2329", "#7D8593"
TOP, BASE = "Top of Cascade", "Meadows Base"
DOW = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


def inch(v):
    return "0" if v < 0.05 else (f"{v:.0f}″" if v >= 10 else f"{v:.1f}″")


def day_name(d):
    return DOW[date.fromisoformat(d).weekday()]


def runs(days, thr=0.5):
    """Consecutive days with at least thr inches: [(first, last, total)]."""
    out, cur = [], None
    for x in days:
        if x["snow"] >= thr:
            cur = [x["d"], x["d"], x["snow"]] if cur is None else [cur[0], x["d"], cur[2] + x["snow"]]
        elif cur:
            out.append(tuple(cur)); cur = None
    if cur:
        out.append(tuple(cur))
    return out


def story(top, base):
    """The headline and one-sentence summary, written from the numbers."""
    tot, btot = sum(x["snow"] for x in top), sum(x["snow"] for x in base)
    big = max(top, key=lambda x: x["snow"])
    if big["snow"] >= 6:
        head = f"Powder alert: {inch(big['snow'])} up top {day_name(big['d'])}"
    elif tot >= 1:
        head = f"{inch(tot)} up top in the next 7 days" + (f", most on {day_name(big['d'])}" if big["snow"] >= 1 else "")
    elif tot >= 0.1:
        head = "A dusting up top this week"
    else:
        head = "Dry week ahead on Mt Hood"
    parts = []
    rs = runs(top)
    if rs:
        first = rs[0][0]
        dry = [x for x in top if x["d"] < first]
        if dry:
            parts.append(f"Dry through {day_name(dry[-1]['d'])}.")
        parts.append("Snow " + " and ".join(
            (day_name(a) if a == b else f"{day_name(a)}–{day_name(b)}") + f" ({inch(t)})" for a, b, t in rs) + " at Cascade.")
        parts.append(f"The base gets {inch(btot)}." if btot >= 0.5 else "Mostly rain or nothing at the base.")
    else:
        hi = max(top, key=lambda x: x["hi"])
        parts.append(f"No snow of note in the next 7 days; warmest up top {day_name(hi['d'])} ({hi['hi']}°).")
    return head, " ".join(parts)


def best_day(top):
    """The morning after the most new snow (48 h at Cascade), lighter wind winning a near tie; else the calmest day."""
    best = None
    for i in range(1, len(top)):
        s48 = top[i - 1]["snow"] + top[i]["snow"]
        key = (round(s48 * 2) / 2, -top[i]["gust"])
        if best is None or key > best[0]:
            best = (key, i, s48)
    if best and best[2] >= 1:
        x = top[best[1]]
        return f"{day_name(x['d'])} {date.fromisoformat(x['d']).day}", f"{inch(best[2])} in 48 h up top · gusts {x['gust']}"
    x = min(top, key=lambda y: (y["gust"], -y["hi"]))
    return f"{day_name(x['d'])} {date.fromisoformat(x['d']).day}", f"no new snow · calmest day (gusts {x['gust']})"


# ---- the charts: PNGs (mail apps don't draw SVG), sent as inline images (cid:) so old emails keep them ----
# Drawn at 3x the email's 2x size and scaled down (Pillow lines aren't anti-aliased). 552 px wide in the email.
SS, OUTW = 3, 1104   # supersampling, output width (2x of 552)


def _font(px, bold=False):
    from PIL import ImageFont
    for f in (("DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf"), ("arialbd.ttf" if bold else "arial.ttf"),
              "/usr/share/fonts/dejavu/" + ("DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf"),
              "/usr/share/fonts/dejavu-sans-fonts/" + ("DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf")):
        try:
            return ImageFont.truetype(f, px)
        except OSError:
            continue
    return ImageFont.load_default(size=px)


def _rgb(h):
    return tuple(int(h[i:i + 2], 16) for i in (1, 3, 5))


def _nice_step(span, n=4):
    for st in (0.5, 1, 2, 3, 5, 10, 15, 20, 25, 50):
        if span / st <= n:
            return st
    return 100


def _png(im):
    from PIL import Image
    im = im.resize((OUTW, round(im.height * OUTW / im.width)), Image.LANCZOS)
    b = io.BytesIO()
    im.save(b, "PNG", optimize=True)
    return b.getvalue()


def chart_snow(days, n_past, today):
    """Bars: the past week (grey, estimated) | today | the next 7 days (plum; the last two paler), on a y-axis in inches."""
    from PIL import Image, ImageDraw
    W, H = OUTW * SS, 330 * SS
    L, R, T, B = 74 * SS, 10 * SS, 40 * SS, 268 * SS   # plot box
    im = Image.new("RGB", (W, H), _rgb(CARD))
    d = ImageDraw.Draw(im)
    vals = [v for _, v in days]
    top = max(1.0, max(vals) * 1.08)
    st = _nice_step(top)
    top = math.ceil(top / st - 1e-9) * st
    y = lambda v: B - (B - T) * v / top
    f, fb = _font(29 * SS), _font(29 * SS, True)
    k = 0.0
    while k <= top + 1e-9:   # gridlines + axis labels
        d.line([(L, y(k)), (W - R, y(k))], fill=_rgb("#2C3139") if k else _rgb("#4B525D"), width=2 * SS)
        d.text((L - 12 * SS, y(k)), f"{k:g}″" if k else "0", font=f, fill=_rgb(FAINT), anchor="rm")
        k += st
    n = len(days)
    slot = (W - R - L) / n
    for i, (dd, v) in enumerate(days):
        fut = i >= n_past
        col = (PLUM_PALE if i >= n_past + 5 else PLUM) if fut else "#5B6370"
        x0, x1 = L + slot * i + slot * 0.2, L + slot * (i + 1) - slot * 0.2
        if v >= 0.05:
            yt = min(y(v), B - 4 * SS)
            d.rounded_rectangle([x0, yt, x1, B], radius=6 * SS, fill=_rgb(col))
            d.rectangle([x0, (yt + B) / 2, x1, B], fill=_rgb(col))   # square bottom
        is_today = date.fromisoformat(dd) == today
        d.text(((x0 + x1) / 2, B + 30 * SS), DOW[date.fromisoformat(dd).weekday()][0], font=fb if is_today else f,
               fill=_rgb(GREEN_INK if is_today else DIM), anchor="mm")
    xt = L + slot * n_past   # today: a dashed line between the past and the forecast
    yy = T - 10 * SS
    while yy < B:
        d.line([(xt, yy), (xt, min(B, yy + 9 * SS))], fill=_rgb(GREEN_INK), width=2 * SS)
        yy += 16 * SS
    d.text((xt - 10 * SS, T - 20 * SS), "past week", font=f, fill=_rgb(DIM), anchor="rm")
    d.text((xt + 10 * SS, T - 20 * SS), "forecast", font=f, fill=_rgb(PLUM), anchor="lm")
    return _png(im)


HI_COL, LO_COL = "#F0A04B", "#7FB8E6"   # warm / cool, both clear of the plum snow and on the dark card


def chart_temps(top, today):
    """Two continuous lines, the daily high and low at Top of Cascade over the next 7 days, with a dashed 32 F line."""
    from PIL import Image, ImageDraw
    W, H = OUTW * SS, 330 * SS
    L, R, T, B = 74 * SS, 30 * SS, 56 * SS, 268 * SS
    im = Image.new("RGB", (W, H), _rgb(CARD))
    d = ImageDraw.Draw(im)
    his, los = [x["hi"] for x in top], [x["lo"] for x in top]
    lo_v, hi_v = min(los + [32]) - 3, max(his + [32]) + 3
    st = _nice_step(hi_v - lo_v, 5)
    lo_v, hi_v = math.floor(lo_v / st) * st, math.ceil(hi_v / st) * st
    y = lambda v: B - (B - T) * (v - lo_v) / (hi_v - lo_v)
    f, fb = _font(29 * SS), _font(29 * SS, True)
    k = lo_v
    while k <= hi_v + 1e-9:
        d.line([(L, y(k)), (W - R, y(k))], fill=_rgb("#2C3139"), width=2 * SS)
        d.text((L - 12 * SS, y(k)), f"{k:g}°", font=f, fill=_rgb(FAINT), anchor="rm")
        k += st
    xf = L   # freezing: a dashed line
    while xf < W - R:
        d.line([(xf, y(32)), (min(W - R, xf + 12 * SS), y(32))], fill=_rgb("#8FA3B8"), width=2 * SS)
        xf += 22 * SS
    n = len(top)
    slot = (W - R - L) / n
    xs = [L + slot * (i + 0.5) for i in range(n)]
    for vals, col in ((his, HI_COL), (los, LO_COL)):
        pts = [(x, y(v)) for x, v in zip(xs, vals)]
        d.line(pts, fill=_rgb(col), width=5 * SS, joint="curve")
        for x, yv in pts:
            r = 7 * SS
            d.ellipse([x - r, yv - r, x + r, yv + r], fill=_rgb(col), outline=_rgb(CARD), width=3 * SS)
    for i, x in enumerate(xs):
        dd = top[i]["d"]
        is_today = date.fromisoformat(dd) == today
        d.text((x, B + 30 * SS), "Today" if is_today else DOW[date.fromisoformat(dd).weekday()], font=fb if is_today else f,
               fill=_rgb(GREEN_INK if is_today else DIM), anchor="mm")
    lx = L   # legend, top left: the two lines and the dashed freezing line
    for name, col, dash in (("High", HI_COL, False), ("Low", LO_COL, False), ("32° freezing", "#8FA3B8", True)):
        if dash:
            for x0 in (lx, lx + 18 * SS):
                d.line([(x0, 20 * SS), (x0 + 10 * SS, 20 * SS)], fill=_rgb(col), width=3 * SS)
        else:
            d.line([(lx, 20 * SS), (lx + 30 * SS, 20 * SS)], fill=_rgb(col), width=5 * SS)
        d.text((lx + 40 * SS, 20 * SS), name, font=f, fill=_rgb(MUT), anchor="lm")
        lx += 40 * SS + d.textlength(name, font=f) + 34 * SS
    return _png(im)


def td(s, extra=""):
    return f'<td align="center" style="padding:7px 1px;{extra}">{s}</td>'


def build(D, inline="data"):
    """subject, html, text, images. inline='data' puts the charts in the page as data: URIs (the --out preview);
    'cid' points at inline attachments (cid:snow / cid:temps), which send() attaches."""
    tz = ZoneInfo("America/Los_Angeles")
    today = datetime.now(tz).date()
    top = [x for x in D["hood"][TOP] if date.fromisoformat(x["d"]) >= today][:7]
    base = [x for x in D["hood"][BASE] if date.fromisoformat(x["d"]) >= today][:7]
    past = [(d, v) for d, v in D.get("past", {}).get(TOP, []) if date.fromisoformat(d) < today][-7:]
    head, sub = story(top, base)
    bd, bd_sub = best_day(top)
    tot, btot = sum(x["snow"] for x in top), sum(x["snow"] for x in base)
    big = max(top, key=lambda x: x["snow"])
    F = "font-family:'Helvetica Neue',Arial,sans-serif;"

    # the snow timeline (the past week, estimated at Cascade's elevation | today | the next 7 days) and the highs / lows
    series = [(d, v) for d, v in past] + [(x["d"], x["snow"]) for x in top]
    images = {"snow": chart_snow(series, len(past), today), "temps": chart_temps(top, today)}
    src = {k: (f"cid:{k}" if inline == "cid" else "data:image/png;base64," + base64.b64encode(v).decode()) for k, v in images.items()}
    snow_alt = "Snow at Top of Cascade: " + ", ".join(f"{day_name(d)} {inch(v)}" for d, v in series)
    temp_alt = "Highs and lows at Top of Cascade: " + ", ".join(f"{day_name(x['d'])} {x['hi']}°/{x['lo']}°" for x in top)

    # the 7-day table
    hdr = "".join(td(f'<b>{day_name(x["d"])}</b>', ("color:" + GREEN_INK + ";" if date.fromisoformat(x["d"]) == today else "")
                     + ("background:#252A31;" if date.fromisoformat(x["d"]).weekday() >= 5 else "")) for x in top)
    def snow_row(xs):
        return "".join(td(inch(x["snow"]) if x["snow"] >= 0.05 else "0",
                          f"color:{PLUM};font-weight:{800 if x['snow'] >= 1 else 700};" if x["snow"] >= 0.05 else "color:#4B525D;") for x in xs)
    gu = "".join(td(str(x["gust"]), "color:#FF6B6B;font-weight:800;" if x["gust"] >= 45 else "color:#F0A04B;font-weight:700;" if x["gust"] >= 30 else "") for x in top)
    po = "".join(td(f'{x["pop"]}%', "color:#5A616C;" if x["pop"] < 10 else f"color:#5FC6D3;font-weight:{800 if x['pop'] >= 50 else 400};") for x in top)
    row = lambda name, cells: (f'<tr style="border-top:1px solid {RULE};"><td style="padding:7px 4px;font-size:11px;line-height:1.25;font-weight:700;color:{MUT};">{name}</td>{cells}</tr>')

    # Temira's Mt Hood snow forecast (her whole section of the day's post), or a note when she isn't posting
    T = D.get("temira") or {}
    if T.get("html") and T.get("status") in ("current", "stale"):
        body = T["html"].replace("<p>", f'<p style="margin:0 0 10px;font-size:14px;line-height:1.6;color:#CBD0D8;">')
        older = "" if T["status"] == "current" else ' <span style="color:#F0A04B;font-weight:700;">(not updated in the last 2 days)</span>'
        temira = (f'<div style="font-size:12px;color:{FAINT};margin:2px 0 12px;">posted {html.escape(T.get("posted") or "")}{older} · '
                  f'<a href="https://thegorgeismygym.com/forecast/" style="color:{GREEN_INK};font-weight:700;text-decoration:none;">on her site ↗</a></div>{body}')
    else:
        temira = (f'<div style="font-size:13px;line-height:1.6;color:{FAINT};margin-top:8px;">Off-season: her Mt Hood snow forecast '
                  f'returns when the season starts. <a href="https://thegorgeismygym.com/forecast/" style="color:{GREEN_INK};font-weight:700;text-decoration:none;">Her site ↗</a></div>')

    V = D.get("video") or {}
    video = ""
    if V.get("id"):
        url = f"https://www.youtube.com/watch?v={V['id']}"
        video = f'''<tr><td class="px" style="padding:22px 24px 6px;">
    <div style="font-size:11px;font-weight:800;letter-spacing:1px;color:{FAINT};text-transform:uppercase;margin-bottom:8px;">Latest from Chris Tomer{" <span style='color:#F0A04B;'>· no new video yet today</span>" if V.get("old") else ""}</div>
    <a href="{url}" style="text-decoration:none;color:{INK};display:block;">
      <img src="https://i.ytimg.com/vi/{V['id']}/hqdefault.jpg" width="552" alt="Play: {html.escape(V.get('title') or 'latest video')}" style="display:block;width:100%;max-width:552px;height:auto;border-radius:12px;border:0;">
      <div style="font-size:15px;font-weight:800;margin-top:10px;">▶ {html.escape(V.get('title') or 'Latest video')}</div>
      <div style="font-size:12px;color:{FAINT};margin-top:2px;">{html.escape(V.get('published') or '')} · tap to watch on YouTube</div>
    </a></td></tr>'''

    when = datetime.now(tz)
    doc = f'''<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><meta name="color-scheme" content="dark"><meta name="supported-color-schemes" content="dark"><title>Mt Hood · {html.escape(head)}</title>
<style>
:root {{ color-scheme:dark; }}
@media only screen and (max-width:480px) {{
  .wrap {{ padding:8px 4px !important; }}
  .px {{ padding-left:14px !important; padding-right:14px !important; }}
  .h1 {{ font-size:22px !important; }}
  .big {{ font-size:24px !important; }}
}}
</style></head>
<body style="margin:0;padding:0;background:{PAGE};">
<div style="display:none;max-height:0;overflow:hidden;">{html.escape(sub)}</div>
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:{PAGE};"><tr><td class="wrap" align="center" style="padding:20px 10px;">
<table role="presentation" width="600" cellpadding="0" cellspacing="0" style="width:100%;max-width:600px;background:{CARD};border-radius:14px;overflow:hidden;{F}color:{INK};">
  <tr><td style="background:{GREEN};padding:18px 24px;">
    <table role="presentation" width="100%" cellpadding="0" cellspacing="0"><tr>
      <td style="font-size:12px;font-weight:800;letter-spacing:1.5px;color:{GREEN_T};text-transform:uppercase;">Mt Hood Meadows · Daily</td>
      <td align="right" style="font-size:12px;font-weight:700;color:{GREEN_T};">{DOW[when.weekday()]}, {when:%b} {when.day}</td></tr></table>
    <div class="h1" style="font-size:26px;line-height:1.2;font-weight:800;color:#FFFFFF;margin-top:8px;">{html.escape(head)}</div>
    <div style="font-size:14px;line-height:1.5;color:#EEF3EA;margin-top:6px;">{html.escape(sub)}</div>
  </td></tr>
  <tr><td class="px" style="padding:18px 24px 6px;"><table role="presentation" width="100%" cellpadding="0" cellspacing="0"><tr>
    <td width="50%" style="padding:10px 12px;background:{PLUM_PANEL};border-radius:10px;">
      <div style="font-size:11px;font-weight:700;color:{FAINT};text-transform:uppercase;letter-spacing:.5px;">7 days · Cascade</div>
      <div class="big" style="font-size:28px;font-weight:800;color:{PLUM};margin-top:2px;">{inch(tot) if tot >= 0.05 else "0″"}</div>
      <div style="font-size:12px;color:{MUT};">{"most on " + day_name(big["d"]) + " " + str(date.fromisoformat(big["d"]).day) if big["snow"] >= 0.5 else "7,343 ft"}</div></td>
    <td width="8"></td>
    <td width="50%" style="padding:10px 12px;background:{PLUM_PANEL};border-radius:10px;">
      <div style="font-size:11px;font-weight:700;color:{FAINT};text-transform:uppercase;letter-spacing:.5px;">7 days · Base</div>
      <div class="big" style="font-size:28px;font-weight:800;color:{PLUM};margin-top:2px;">{inch(btot) if btot >= 0.05 else "0″"}</div>
      <div style="font-size:12px;color:{MUT};">5,361 ft</div></td>
  </tr><tr><td colspan="3" height="8" style="font-size:0;line-height:0;">&nbsp;</td></tr><tr>
    <td colspan="3" style="padding:10px 12px;background:{GREEN_PALE};border-radius:10px;">
      <span style="font-size:11px;font-weight:700;color:{FAINT};text-transform:uppercase;letter-spacing:.5px;">Best day to go</span><br>
      <span style="font-size:20px;font-weight:800;color:{GREEN_INK};">{bd}</span>
      <span style="font-size:13px;color:{MUT};">&nbsp;·&nbsp;{bd_sub}</span></td>
  </tr></table></td></tr>
  <tr><td class="px" style="padding:16px 24px 4px;"><div style="font-size:13px;font-weight:800;">Snow at Top of Cascade <span style="font-weight:600;color:{FAINT};">· the past week, then the next 7 days</span></div></td></tr>
  <tr><td class="px" style="padding:6px 24px 0;">
    <img src="{src['snow']}" width="552" alt="{html.escape(snow_alt)}" style="display:block;width:100%;max-width:552px;height:auto;border:0;">
    <div style="font-size:11px;color:{FAINT};margin-top:6px;">Grey = the past week (estimated at Cascade's elevation) · plum = forecast new snow (paler: less sure) · dashed = today</div>
  </td></tr>
  <tr><td class="px" style="padding:20px 24px 4px;"><div style="font-size:13px;font-weight:800;">Highs and lows at Top of Cascade <span style="font-weight:600;color:{FAINT};">· the next 7 days</span></div></td></tr>
  <tr><td class="px" style="padding:6px 24px 0;">
    <img src="{src['temps']}" width="552" alt="{html.escape(temp_alt)}" style="display:block;width:100%;max-width:552px;height:auto;border:0;">
  </td></tr>
  <tr><td class="px" style="padding:22px 24px 6px;"><div style="font-size:13px;font-weight:800;">The next 7 days <span style="font-weight:600;color:{FAINT};">· gusts at Top of Cascade</span></div></td></tr>
  <tr><td style="padding:0 24px;">
    <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="font-size:12px;border-collapse:collapse;table-layout:fixed;">
      <tr style="background:{PANEL};"><td width="64" style="padding:7px 4px;"></td>{hdr}</tr>
      {row("Snow<br>Cascade", snow_row(top))}
      {row("Snow<br>Base", snow_row(base))}
      {row("Gusts", gu)}
      {row("Chance", po)}
    </table></td></tr>
  <tr><td class="px" style="padding:26px 24px 6px;">
    <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="border-left:4px solid {GREEN_INK};background:{PANEL};border-radius:0 10px 10px 0;"><tr><td style="padding:14px 16px;">
      <div style="font-size:11px;font-weight:800;letter-spacing:1px;color:{GREEN_INK};text-transform:uppercase;">The meteorologist's forecast</div>
      <div style="font-size:17px;font-weight:800;margin-top:4px;">Temira · The Gorge Is My Gym</div>
      {temira}
    </td></tr></table></td></tr>
  {video}
  <tr><td class="px" align="center" style="padding:26px 24px 8px;">
    <a href="{SITE}" style="display:inline-block;background:{GREEN};color:#fff;font-size:14px;font-weight:800;text-decoration:none;padding:12px 22px;border-radius:10px;">Open the full Mt Hood forecast</a></td></tr>
  <tr><td class="px" style="padding:14px 24px 22px;font-size:11px;line-height:1.5;color:{DIM};text-align:center;">
    NWS forecast moved to each lift's elevation · snow from the dashboard's rain/snow model · built {when:%a %b} {when.day}, {when.hour % 12 or 12}:{when:%M} {"AM" if when.hour < 12 else "PM"}<br>
    Sent every morning by your Oregon Weather server</td></tr>
</table></td></tr></table></body></html>'''

    text = f"{head}\n{sub}\n\nBest day to go: {bd} ({bd_sub})\n\n" + "\n".join(
        f"{day_name(x['d'])}: Cascade {inch(x['snow'])}, Base {inch(b['snow'])}, {x['hi']}°/{x['lo']}° up top, gusts {x['gust']}, {x['pop']}%"
        for x, b in zip(top, base)) + f"\n\nFull forecast: {SITE}\n"
    return f"Mt Hood · {head}", doc, text, images


def send(subject, doc, text, images=None):
    try:
        from dotenv import load_dotenv
        load_dotenv(os.path.join(ROOT, ".env"))
    except ImportError:
        pass
    # Resend (owner has no 2-Step Verification, so no Gmail app password): an API key; without a domain of
    # our own it sends from onboarding@resend.dev, and only to the address the Resend account was made with
    key = os.environ.get("RESEND_API_KEY")
    if key:
        to = os.environ.get("NEWSLETTER_TO") or os.environ.get("GMAIL_USER")
        if not to:
            print("newsletter: RESEND_API_KEY set but no NEWSLETTER_TO address - not sent")
            return False
        import requests
        r = requests.post("https://api.resend.com/emails", timeout=60,
                          headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                          json={"from": os.environ.get("NEWSLETTER_FROM") or "Oregon Weather <onboarding@resend.dev>",
                                "to": [to], "subject": subject, "html": doc, "text": text,
                                "attachments": [{"filename": f"{k}.png", "content": base64.b64encode(v).decode(),
                                                 "content_id": k} for k, v in (images or {}).items()]})
        if r.status_code >= 300:
            print(f"newsletter: Resend said {r.status_code}: {r.text[:300]}")
            return False
        print(f"newsletter: sent to {to} (Resend)")
        return True
    user, pw = os.environ.get("GMAIL_USER"), os.environ.get("GMAIL_APP_PASSWORD")
    to = os.environ.get("NEWSLETTER_TO") or user
    if not user or not pw:
        print("newsletter: not configured (RESEND_API_KEY, or GMAIL_USER + GMAIL_APP_PASSWORD, in .env) - not sent")
        return False
    m = MIMEMultipart("related")
    m["Subject"], m["From"], m["To"] = subject, f"Oregon Weather <{user}>", to
    alt = MIMEMultipart("alternative")
    alt.attach(MIMEText(text, "plain", "utf-8"))
    alt.attach(MIMEText(doc, "html", "utf-8"))
    m.attach(alt)
    for k, v in (images or {}).items():
        part = MIMEImage(v, "png")
        part.add_header("Content-ID", f"<{k}>")
        part.add_header("Content-Disposition", "inline", filename=f"{k}.png")
        m.attach(part)
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=60) as s:
        s.login(user, pw.replace(" ", ""))
        s.sendmail(user, [to], m.as_string())
    print(f"newsletter: sent to {to}")
    return True


if __name__ == "__main__":
    with open(DATA, encoding="utf-8") as f:
        D = json.load(f)
    subject, doc, text, images = build(D)   # the preview: charts as data: URIs
    if "--out" in sys.argv:
        out = sys.argv[sys.argv.index("--out") + 1]
        with open(out, "w", encoding="utf-8") as f:
            f.write(doc)
        print(f"newsletter: written to {out} ({subject})")
    if "--send" in sys.argv:
        # owner (2026-10-23): wait for Chris Tomer's new video, then send; no new video by the deadline hour
        # (--deadline, default 11 = the 11 AM build) -> send anyway, marked "no new video yet today".
        # New = a different video from the last email's (SENT holds "<date> <video id>").
        now = datetime.now(ZoneInfo("America/Los_Angeles"))
        today = now.date().isoformat()
        last = open(SENT).read().split() if os.path.exists(SENT) else []
        V = D.get("video") or {}
        if last[:1] == [today] and "--force" not in sys.argv:
            print("newsletter: already sent today")
            sys.exit(0)
        if len(last) > 1:
            new = bool(V.get("id")) and V["id"] != last[1]
        else:   # no video recorded yet: posted today (RSS date, or the /videos page's "N hours ago")
            pub = str(V.get("published") or "")
            new = pub == today or any(u in pub for u in ("second", "minute", "hour"))
        deadline = int(sys.argv[sys.argv.index("--deadline") + 1]) if "--deadline" in sys.argv else 11
        if not new and now.hour < deadline and "--force" not in sys.argv:
            print(f"newsletter: waiting for a new Chris Tomer video (sends anyway at the {deadline}:00 build)")
            sys.exit(0)
        if not new and V:
            V["old"] = True
        subject, doc, text, images = build(D, "cid")   # the charts as inline attachments
        if send(subject, doc, text, images):
            with open(SENT, "w") as f:
                f.write(f"{today} {V.get('id') or '-'}")
