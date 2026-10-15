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
import html
import json
import os
import smtplib
import sys
from datetime import date, datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from zoneinfo import ZoneInfo

ROOT = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(ROOT, ".cache", "newsletter_data.json")
SENT = os.path.join(ROOT, ".cache", "newsletter_sent.txt")
SITE = "https://aaronmaddenjk.github.io/oregon-weather-dashboard/"
GREEN, GREEN_T, GREEN_PALE = "#525D45", "#D9E3D2", "#F0F4EC"
PLUM, PLUM_PALE, PLUM_PANEL = "#7A5A99", "#CDBEDC", "#F5F1F8"
INK, MUT, FAINT, RULE = "#111111", "#5A5F6B", "#8A8F9C", "#EEF0F3"
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


def td(s, extra=""):
    return f'<td align="center" style="padding:7px 1px;{extra}">{s}</td>'


def build(D):
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

    # the snow timeline: the past week (estimated at Cascade's elevation) | today | the next 7 days
    vals = [v for _, v in past] + [x["snow"] for x in top]
    mx = max([2.0] + vals)
    bars, labels = "", ""
    for i, (d, v) in enumerate([(d, v) for d, v in past] + [(x["d"], x["snow"]) for x in top]):
        fut = i >= len(past)
        col = (PLUM_PALE if i >= len(past) + 5 else PLUM) if fut else "#A9B2BF"
        h = max(3, round(v / mx * 72))
        lab = ""
        if v >= 0.1:   # the amount over the bar
            amt = f"{v:.0f}" if v >= 10 else f"{v:.1f}"
            lab = f'<div style="font-size:{12 if v >= 3 else 10}px;color:{PLUM if fut else FAINT};font-weight:800;">{amt}</div>'
        edge = f"border-right:2px dashed {GREEN};" if i == len(past) - 1 else ""
        bars += f'<td valign="bottom" align="center" style="{edge}">{lab}<div style="height:{h}px;background:{col};border-radius:{3 if h > 4 else 2}px {3 if h > 4 else 2}px 0 0;margin:0 3px;"></div></td>'
        is_today = date.fromisoformat(d) == today
        labels += (f'<td align="center" style="font-size:10px;color:{GREEN if is_today else "#9AA0AC"};font-weight:{800 if is_today else 700};padding-top:4px;">'
                   f'{day_name(d)[0]}</td>')

    # the 7-day table
    hdr = "".join(td(f'<b>{day_name(x["d"])}</b>', ("color:" + GREEN + ";" if date.fromisoformat(x["d"]) == today else "")
                     + ("background:#EEF0F3;" if date.fromisoformat(x["d"]).weekday() >= 5 else "")) for x in top)
    def snow_row(xs):
        return "".join(td(inch(x["snow"]) if x["snow"] >= 0.05 else "0",
                          f"color:{PLUM};font-weight:{800 if x['snow'] >= 1 else 700};" if x["snow"] >= 0.05 else "color:#C9CDD5;") for x in xs)
    hl = "".join(td(f'<b>{x["hi"]}°</b> <span style="color:{FAINT};">{x["lo"]}°</span>') for x in top)
    gu = "".join(td(str(x["gust"]), "color:#D11A24;font-weight:800;" if x["gust"] >= 45 else "color:#D9730D;font-weight:700;" if x["gust"] >= 30 else "") for x in top)
    po = "".join(td(f'{x["pop"]}%', "color:#B8BDC6;" if x["pop"] < 10 else f"color:#0B6F7C;font-weight:{800 if x['pop'] >= 50 else 400};") for x in top)
    row = lambda name, cells: (f'<tr style="border-top:1px solid {RULE};"><td style="padding:7px 4px;font-size:11px;line-height:1.25;font-weight:700;color:{MUT};">{name}</td>{cells}</tr>')

    # Temira's Mt Hood snow forecast (her whole section of the day's post), or a note when she isn't posting
    T = D.get("temira") or {}
    if T.get("html") and T.get("status") in ("current", "stale"):
        body = T["html"].replace("<p>", f'<p style="margin:0 0 10px;font-size:14px;line-height:1.6;color:#2A2E36;">')
        older = "" if T["status"] == "current" else ' <span style="color:#B4400A;font-weight:700;">(not updated in the last 2 days)</span>'
        temira = (f'<div style="font-size:12px;color:{FAINT};margin:2px 0 12px;">posted {html.escape(T.get("posted") or "")}{older} · '
                  f'<a href="https://thegorgeismygym.com/forecast/" style="color:{GREEN};font-weight:700;text-decoration:none;">on her site ↗</a></div>{body}')
    else:
        temira = (f'<div style="font-size:13px;line-height:1.6;color:{FAINT};margin-top:8px;">Off-season: her Mt Hood snow forecast '
                  f'returns when the season starts. <a href="https://thegorgeismygym.com/forecast/" style="color:{GREEN};font-weight:700;text-decoration:none;">Her site ↗</a></div>')

    V = D.get("video") or {}
    video = ""
    if V.get("id"):
        url = f"https://www.youtube.com/watch?v={V['id']}"
        video = f'''<tr><td class="px" style="padding:22px 24px 6px;">
    <div style="font-size:11px;font-weight:800;letter-spacing:1px;color:#7D8592;text-transform:uppercase;margin-bottom:8px;">Latest from Chris Tomer</div>
    <a href="{url}" style="text-decoration:none;color:{INK};display:block;">
      <img src="https://i.ytimg.com/vi/{V['id']}/hqdefault.jpg" width="552" alt="Play: {html.escape(V.get('title') or 'latest video')}" style="display:block;width:100%;max-width:552px;height:auto;border-radius:12px;border:0;">
      <div style="font-size:15px;font-weight:800;margin-top:10px;">▶ {html.escape(V.get('title') or 'Latest video')}</div>
      <div style="font-size:12px;color:{FAINT};margin-top:2px;">{html.escape(V.get('published') or '')} · tap to watch on YouTube</div>
    </a></td></tr>'''

    when = datetime.now(tz)
    doc = f'''<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Mt Hood · {html.escape(head)}</title>
<style>
@media only screen and (max-width:480px) {{
  .wrap {{ padding:8px 4px !important; }}
  .px {{ padding-left:14px !important; padding-right:14px !important; }}
  .h1 {{ font-size:22px !important; }}
  .big {{ font-size:24px !important; }}
}}
</style></head>
<body style="margin:0;padding:0;background:#EEF0F3;">
<div style="display:none;max-height:0;overflow:hidden;">{html.escape(sub)}</div>
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:#EEF0F3;"><tr><td class="wrap" align="center" style="padding:20px 10px;">
<table role="presentation" width="600" cellpadding="0" cellspacing="0" style="width:100%;max-width:600px;background:#FFFFFF;border-radius:14px;overflow:hidden;{F}color:{INK};">
  <tr><td style="background:{GREEN};padding:18px 24px;">
    <table role="presentation" width="100%" cellpadding="0" cellspacing="0"><tr>
      <td style="font-size:12px;font-weight:800;letter-spacing:1.5px;color:{GREEN_T};text-transform:uppercase;">Mt Hood Meadows · Daily</td>
      <td align="right" style="font-size:12px;font-weight:700;color:{GREEN_T};">{DOW[when.weekday()]}, {when:%b} {when.day}</td></tr></table>
    <div class="h1" style="font-size:26px;line-height:1.2;font-weight:800;color:#FFFFFF;margin-top:8px;">{html.escape(head)}</div>
    <div style="font-size:14px;line-height:1.5;color:#EEF3EA;margin-top:6px;">{html.escape(sub)}</div>
  </td></tr>
  <tr><td class="px" style="padding:18px 24px 6px;"><table role="presentation" width="100%" cellpadding="0" cellspacing="0"><tr>
    <td width="50%" style="padding:10px 12px;background:{PLUM_PANEL};border-radius:10px;">
      <div style="font-size:11px;font-weight:700;color:#7D8592;text-transform:uppercase;letter-spacing:.5px;">7 days · Cascade</div>
      <div class="big" style="font-size:28px;font-weight:800;color:{PLUM};margin-top:2px;">{inch(tot) if tot >= 0.05 else "0″"}</div>
      <div style="font-size:12px;color:{MUT};">{"most on " + day_name(big["d"]) + " " + str(date.fromisoformat(big["d"]).day) if big["snow"] >= 0.5 else "7,343 ft"}</div></td>
    <td width="8"></td>
    <td width="50%" style="padding:10px 12px;background:{PLUM_PANEL};border-radius:10px;">
      <div style="font-size:11px;font-weight:700;color:#7D8592;text-transform:uppercase;letter-spacing:.5px;">7 days · Base</div>
      <div class="big" style="font-size:28px;font-weight:800;color:{PLUM};margin-top:2px;">{inch(btot) if btot >= 0.05 else "0″"}</div>
      <div style="font-size:12px;color:{MUT};">5,361 ft</div></td>
  </tr><tr><td colspan="3" height="8" style="font-size:0;line-height:0;">&nbsp;</td></tr><tr>
    <td colspan="3" style="padding:10px 12px;background:{GREEN_PALE};border-radius:10px;">
      <span style="font-size:11px;font-weight:700;color:#7D8592;text-transform:uppercase;letter-spacing:.5px;">Best day to go</span><br>
      <span style="font-size:20px;font-weight:800;color:{GREEN};">{bd}</span>
      <span style="font-size:13px;color:{MUT};">&nbsp;·&nbsp;{bd_sub}</span></td>
  </tr></table></td></tr>
  <tr><td class="px" style="padding:16px 24px 4px;"><div style="font-size:13px;font-weight:800;">Snow at Top of Cascade <span style="font-weight:600;color:{FAINT};">· the past week, then the next 7 days</span></div></td></tr>
  <tr><td class="px" style="padding:6px 24px 0;">
    <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="table-layout:fixed;"><tr style="height:96px;">{bars}</tr><tr>{labels}</tr></table>
    <div style="font-size:11px;color:{FAINT};margin-top:6px;">Grey = the past week (estimated at Cascade's elevation) · plum = forecast new snow (paler: less sure) · dashed = today</div>
  </td></tr>
  <tr><td class="px" style="padding:22px 24px 6px;"><div style="font-size:13px;font-weight:800;">The next 7 days <span style="font-weight:600;color:{FAINT};">· temps and gusts at Top of Cascade</span></div></td></tr>
  <tr><td style="padding:0 24px;">
    <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="font-size:12px;border-collapse:collapse;table-layout:fixed;">
      <tr style="background:#F7F8FA;"><td width="64" style="padding:7px 4px;"></td>{hdr}</tr>
      {row("Snow<br>Cascade", snow_row(top))}
      {row("Snow<br>Base", snow_row(base))}
      {row("High / low", hl)}
      {row("Gusts", gu)}
      {row("Chance", po)}
    </table></td></tr>
  <tr><td class="px" style="padding:26px 24px 6px;">
    <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="border-left:4px solid {GREEN};background:#FBFBFC;border-radius:0 10px 10px 0;"><tr><td style="padding:14px 16px;">
      <div style="font-size:11px;font-weight:800;letter-spacing:1px;color:{GREEN};text-transform:uppercase;">The meteorologist's forecast</div>
      <div style="font-size:17px;font-weight:800;margin-top:4px;">Temira · The Gorge Is My Gym</div>
      {temira}
    </td></tr></table></td></tr>
  {video}
  <tr><td class="px" align="center" style="padding:26px 24px 8px;">
    <a href="{SITE}" style="display:inline-block;background:{GREEN};color:#fff;font-size:14px;font-weight:800;text-decoration:none;padding:12px 22px;border-radius:10px;">Open the full Mt Hood forecast</a></td></tr>
  <tr><td class="px" style="padding:14px 24px 22px;font-size:11px;line-height:1.5;color:#9AA0AC;text-align:center;">
    NWS forecast moved to each lift's elevation · snow from the dashboard's rain/snow model · built {when:%a %b} {when.day}, {when.hour % 12 or 12}:{when:%M} {"AM" if when.hour < 12 else "PM"}<br>
    Sent every morning by your Oregon Weather server</td></tr>
</table></td></tr></table></body></html>'''

    text = f"{head}\n{sub}\n\nBest day to go: {bd} ({bd_sub})\n\n" + "\n".join(
        f"{day_name(x['d'])}: Cascade {inch(x['snow'])}, Base {inch(b['snow'])}, {x['hi']}°/{x['lo']}° up top, gusts {x['gust']}, {x['pop']}%"
        for x, b in zip(top, base)) + f"\n\nFull forecast: {SITE}\n"
    return f"Mt Hood · {head}", doc, text


def send(subject, doc, text):
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
                                "to": [to], "subject": subject, "html": doc, "text": text})
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
    m = MIMEMultipart("alternative")
    m["Subject"], m["From"], m["To"] = subject, f"Oregon Weather <{user}>", to
    m.attach(MIMEText(text, "plain", "utf-8"))
    m.attach(MIMEText(doc, "html", "utf-8"))
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=60) as s:
        s.login(user, pw.replace(" ", ""))
        s.sendmail(user, [to], m.as_string())
    print(f"newsletter: sent to {to}")
    return True


if __name__ == "__main__":
    with open(DATA, encoding="utf-8") as f:
        D = json.load(f)
    subject, doc, text = build(D)
    if "--out" in sys.argv:
        out = sys.argv[sys.argv.index("--out") + 1]
        with open(out, "w", encoding="utf-8") as f:
            f.write(doc)
        print(f"newsletter: written to {out} ({subject})")
    if "--send" in sys.argv:
        today = datetime.now(ZoneInfo("America/Los_Angeles")).date().isoformat()
        if os.path.exists(SENT) and open(SENT).read().strip() == today and "--force" not in sys.argv:
            print("newsletter: already sent today")
        elif send(subject, doc, text):
            with open(SENT, "w") as f:
                f.write(today)
