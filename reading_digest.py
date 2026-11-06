"""
Daily read email (owner, 2026-11-05; was weekly the same day): each morning ONE leading story and ONE smaller,
preferably fun, piece from the current issues of The Economist and The New Yorker - each linked to where the owner
can read it through the library. Only headlines, subheadings, pictures and links: no article text is copied
(library licences = read in PressReader / Gale / Libby).

Reading links:
  The Economist  - a PressReader search for the headline limited to The Economist (North America), cid 7434
                   (PressReader matches the print headline; the owner taps the result).
  The New Yorker - Gale Academic OneFile through WCCLS's sign-in: a search for that writer in that issue = one
                   result (gale()); the owner's Reader extension opens Gale articles in a clean view.

The pools (current issues only, so everything is readable):
  Economist issue = the Saturday-dated issue on PressReader by Thursday night: from Friday the coming Saturday's,
  else last Saturday's; its articles are the feed items published in the 6 days before the issue date.
  Leads  - Economist Leaders + Briefing; New Yorker: the pieces its feed description names ("X on ..., Y on ...").
  Lights - LIGHT_SECTIONS below (New Yorker humour / Talk of the Town / critics; Economist Obituary, Culture,
           Science, International), scored by section (fun first) + playful headlines; never yesterday's section.
Each day: a lead from the other magazine than yesterday's lead (if it has one left), the best light piece not
sent yet. .cache/reading_log.json remembers what was sent (one entry per day).

  python reading_digest.py --out x.html [--date 2026-11-07]   preview one day (no sending, no log)
  python reading_digest.py --week                               print the next 7 days' picks (dry run)
  python reading_digest.py --send                               send today's (once a day; --force resends)

Cron on the Oracle server: every day 7 AM Pacific. Sends with newsletter.send() (Resend / Gmail settings).
"""
import base64
import html
import io
import json
import os
import re
import sys
import xml.etree.ElementTree as ET
from datetime import date, datetime, timedelta
from email.utils import parsedate_to_datetime
from urllib.parse import quote, quote_plus
from zoneinfo import ZoneInfo

import requests

from newsletter import send

ROOT = os.path.dirname(os.path.abspath(__file__))
LOG = os.path.join(ROOT, ".cache", "reading_log.json")
PT = ZoneInfo("America/Los_Angeles")
UA = {"User-Agent": "Mozilla/5.0 (X11; Linux aarch64) AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/130 Safari/537.36"}
LIBRARY = "wccls"          # Libby library key (Washington County Cooperative Library Services)
ECON_CID = "7434"          # PressReader: The Economist (North America)
GALE_LOC = "wccls_main"    # Gale location id: WCCLS has The New Yorker's full text in Gale Academic OneFile
DC = "{http://purl.org/dc/elements/1.1/}"
MEDIA = "{http://search.yahoo.com/mrss/}"
ECON, NY = "The Economist", "The New Yorker"

# light pieces: (magazine, section pattern) -> base score (higher = more fun)
LIGHT_SECTIONS = [
    (NY, r"Shouts & Murmurs", 10),
    (NY, r"Dept\.|Talk of the Town|Sketchbook", 8),          # Talk of the Town's quirky short pieces
    (ECON, r"^Obituary$", 8),
    (ECON, r"^Culture$", 7),
    (ECON, r"^Science & technology$", 5),
    (NY, r"The Art World|Musical Events|The Current Cinema|The Theatre|On Television|Pop Music|Dancing|Tables for Two", 4),
    (ECON, r"^International$", 4),
    (NY, r"^Books$", 3),
]
ECON_LIGHT_FEEDS = {"obituary": "Obituary", "culture": "Culture", "science-and-technology": "Science & technology",
                    "international": "International"}
FUN_WORDS = re.compile(r"\?|\b(why|how|secret|strange|odd|weird|curious|joy|fun|funny|love|dog|cat|bird|food|pizza|"
                       r"wine|beer|chocolate|game|sport|music|song|art|film|movie|ghost|myster\w*|bizarre|tiny|giant)\b", re.I)
SKIP = re.compile(r"newsletter|recruiting|transcript|podcast|quiz|crossword|cartoon|caption contest|briefly noted|"
                  r"what to (watch|read|see|listen)|this week$|week ahead|^the week in|letters to the editor", re.I)

# paper palette
PAGE, CARD, INK, BODY, MUT, RULE = "#F2EEE6", "#FFFFFF", "#15171C", "#3F4450", "#7A7F8A", "#E7E2D9"
ECON_RED, SUN = "#E3120B", "#C9822B"
SERIF = "Georgia,'Times New Roman',serif"
SANS = "-apple-system,'Segoe UI',Roboto,Helvetica,Arial,sans-serif"


# ---------------------------------------------------------------- feeds
def feed(url):
    r = requests.get(url, headers=UA, timeout=30)
    r.raise_for_status()
    return ET.fromstring(r.content)


def items(root):
    for it in root.iter("item"):
        g = lambda k: (it.findtext(k) or "").strip()
        th = it.find(MEDIA + "thumbnail")
        yield {"title": g("title"), "sub": g("description"), "link": g("link"),
               "date": parsedate_to_datetime(g("pubDate")).astimezone(PT) if g("pubDate") else None,
               "by": g(DC + "creator"), "cats": [c.text or "" for c in it.findall("category")],
               "img": th.get("url") if th is not None else ""}


def plain(s):
    return s.replace("’", "'").replace("‘", "'").replace("“", '"').replace("”", '"')


def econ_issue(day):
    """The Saturday-dated issue readable on PressReader that morning (it lands Thursday night)."""
    wd = day.weekday()
    return day + timedelta(days=5 - wd) if wd in (4, 5) else day - timedelta(days=(wd - 5) % 7)


def pressreader(title):
    return (f"https://www.pressreader.com/search/articles?query={quote(plain(title))}"
            f"&cids={ECON_CID}&date=Anytime&in=ALL")


def economist(day):
    issue = econ_issue(day)
    start = datetime.combine(issue - timedelta(days=6), datetime.min.time(), PT)
    end = datetime.combine(issue + timedelta(days=1), datetime.min.time(), PT)

    def section(slug, label):
        out = []
        for a in items(feed(f"https://www.economist.com/{slug}/rss.xml")):
            if (a["date"] and start <= a["date"] < end and "/interactive/" not in a["link"]
                    and not SKIP.search(a["title"]) and "/20" in a["link"]):
                a.update(mag=ECON, kind=label, read=pressreader(a["title"]), by="")
                out.append(a)
        return out

    leads = section("leaders", "Leader") + section("briefing", "Briefing")
    leads.sort(key=lambda a: (a["kind"] != "Briefing", a["date"]))    # the Briefing, then leaders as published
    lights = []
    for slug, label in ECON_LIGHT_FEEDS.items():
        try:
            lights += section(slug, label)
        except Exception as e:
            print(f"reading: {slug} feed failed ({e})")
    return issue, leads, lights


def crop(url, ratio, w):
    """New Yorker (Conde Nast) image server: '.../master/pass/x.jpg' -> '.../3:2/w_1080,c_limit/x.jpg' = the
    editors' crop at that aspect ratio (w_,h_,c_fill pads portrait art with bars instead of cropping)."""
    return re.sub(r"/master/[^/]+/", f"/{ratio}/w_{w},c_limit/", url) if "/master/" in url else url


def gale(issue, author="", title=""):
    """A Gale Academic OneFile search through WCCLS's sign-in (EZproxy): The New Yorker's issue, or one writer's
    piece in it (one result = one tap to the full text). Gale dates each issue a week before the magazine's
    cover date (Gale "Nov 2, 2026" = the Nov 9 issue; checked 2026-11-05). Talk of the Town is ONE Gale
    document with no author (title "The Talk of the Town."), so its short pieces link to it by title (TI~)."""
    qt = (f'PU~"The New Yorker"~~DA~1{issue - timedelta(days=7):%Y%m%d}' + (f'~~AU~"{author}"' if author else "")
          + (f'~~TI~"{title}"' if title else ""))
    go = ("https://go.gale.com/ps/publicationSearch.do?qt=" + quote_plus(qt) +
          f"&method=doLinkDirectedSearch&searchType=AdvancedSearchForm&userGroupName={GALE_LOC}&prodId=AONE&inPS=true")
    return "https://wccls.idm.oclc.org/login?qurl=" + quote(go, safe="")


def new_yorker():
    root = feed("https://www.newyorker.com/feed/magazine/rss")
    ch = root.find("channel")
    title = (ch.findtext("title") or "").replace("The New Yorker", "").strip()     # "November 9, 2026"
    blurb = (ch.findtext("description") or "").strip()
    try:
        issue = datetime.strptime(title, "%B %d, %Y").date()
    except ValueError:
        issue = None
    arts = [a for a in items(root) if "/magazine/" in a["link"] and not SKIP.search(a["title"])]
    for a in arts:
        a["mag"] = NY
        a["kind"] = next((c.split("/", 1)[1].strip() for c in a["cats"] if c.startswith("Magazine /")), "")
        first = a["by"].split(",")[0].strip()
        if re.search(r"Dept\.|Talk of the Town|Comment", a["kind"]) and not re.search(r"Dept\. of Medicine", a["kind"]):
            a["read"] = gale(issue, title="Talk of the Town") if issue else ""
            a["note"] = "One of the short pieces in The Talk of the Town"
        else:
            a["read"] = gale(issue, first) if issue and first else ""
    # leads: the pieces the feed's description names ("Manvir Singh on ..., Francisco Cantu on ..., and more.")
    names = [p.split(" on ")[0].strip() for p in re.split(r",\s*(?:and\s+)?", blurb) if " on " in p]
    leads = []
    for n in names:
        a = next((a for a in arts if n.lower() in a["by"].lower() and a not in leads), None)
        if a:
            leads.append(a)
    if not leads:
        leads = [a for a in arts if re.search(r"Profiles|Reporter at Large|Annals|Chronicles|Letter from", a["kind"])]
    lights = [a for a in arts if a not in leads and a["by"]]
    return issue, leads, lights


def economist_cover(issue):
    """PNG bytes of the issue cover (PressReader's public thumbnail), or None."""
    try:
        from PIL import Image
        r = requests.get(f"https://t.prcdn.co/img?file={ECON_CID}{issue:%Y%m%d}00000000001001&page=1&width=320",
                         headers=UA, timeout=30)
        if r.status_code != 200 or not r.headers.get("content-type", "").startswith("image"):
            return None
        im = Image.open(io.BytesIO(r.content)).convert("RGB")
        im = im.resize((300, round(300 * im.height / im.width)), Image.LANCZOS)
        buf = io.BytesIO()
        im.save(buf, "PNG", optimize=True)
        return buf.getvalue()
    except Exception as e:
        print(f"reading: no Economist cover ({e})")
        return None


# ---------------------------------------------------------------- picking
def light_score(a):
    base = next((s for mag, pat, s in LIGHT_SECTIONS if mag == a["mag"] and re.search(pat, a["kind"])), 0)
    return base + (2 if FUN_WORDS.search(a["title"] + " " + a["sub"]) else 0) if base else 0


def pools(day):
    e_issue, e_leads, e_lights = economist(day)
    n_issue, n_leads, n_lights = new_yorker()
    return {"econ_issue": e_issue, "ny_issue": n_issue,
            "leads": {ECON: e_leads, NY: n_leads}, "lights": e_lights + n_lights}


def pick(P, log):
    sent = {x for e in log for x in (e.get("lead"), e.get("light"))}
    last = log[-1] if log else {}
    order = [NY, ECON] if last.get("lead_mag") == ECON else [ECON, NY]
    lead = next((a for mag in order for a in P["leads"][mag] if a["link"] not in sent), None)
    cands = [a for a in P["lights"] if a["link"] not in sent and (not lead or a["link"] != lead["link"])]
    for a in cands:
        a["score"] = light_score(a) - 3 * sum(a["kind"] == e.get("light_kind") for e in log[-3:]) \
            + (1 if lead and a["mag"] != lead["mag"] else 0)
    cands = [a for a in cands if a["score"] > 0]
    light = max(cands, key=lambda a: a["score"]) if cands else None
    return lead, light


def load_log():
    try:
        with open(LOG, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return []


def log_entry(day, lead, light):
    return {"date": day.isoformat(), "lead": lead and lead["link"], "lead_mag": lead and lead["mag"],
            "light": light and light["link"], "light_kind": light and light["kind"]}


# ---------------------------------------------------------------- HTML
def esc(s):
    return html.escape(s or "")


def kicker(text, color):
    return (f'<div style="font:700 11px/1.2 {SANS};letter-spacing:.12em;text-transform:uppercase;'
            f'color:{color};margin:0 0 8px">{text}</div>')


def button(href, label, color):
    return (f'<table role="presentation" cellpadding="0" cellspacing="0"><tr><td style="border-radius:6px;background:{color}">'
            f'<a href="{esc(href)}" style="display:inline-block;padding:12px 20px;font:600 14px/1 {SANS};color:#ffffff;'
            f'text-decoration:none;border-radius:6px">{esc(label)}</a></td></tr></table>')


def where(a):
    return "Read in PressReader" if a["mag"] == ECON else "Read the full text"


def label(a):
    color = ECON_RED if a["mag"] == ECON else INK
    return kicker(f'{esc(a["mag"])} <span style="color:{MUT}">&middot; {esc(a["kind"])}</span>', color)


def lead_card(a, cover_src):
    by = f'<div style="font:600 13px/1.4 {SANS};color:{MUT};margin:0 0 18px">By {esc(a["by"])}</div>' if a["by"] else '<div style="height:6px"></div>'
    color = ECON_RED if a["mag"] == ECON else INK
    if a["mag"] == NY and a.get("img"):
        pic = (f'<a href="{esc(a["read"])}"><img src="{esc(crop(a["img"], "3:2", 1200))}" width="600" alt="" '
               f'style="display:block;width:100%;max-width:600px;height:auto;border:0;border-radius:10px 10px 0 0"></a>')
        top = f'<tr><td style="padding:0;line-height:0">{pic}</td></tr>'
        side = ""
    else:
        top = ""
        side = (f'<td class="stack cov" width="120" valign="top" style="padding:0 24px 0 0;width:120px"><a href="{esc(a["read"])}">'
                f'<img src="{esc(cover_src)}" width="120" alt="This week\'s Economist cover" style="display:block;width:120px;height:auto;'
                f'border:0;border-radius:3px;box-shadow:0 6px 18px rgba(0,0,0,.18)"></a></td>') if cover_src else ""
    return f'''<tr><td style="padding:0 0 22px"><table role="presentation" width="100%" cellpadding="0" cellspacing="0"
style="background:{CARD};border-radius:10px;{'' if top else f'border-top:5px solid {color};'}box-shadow:0 1px 3px rgba(0,0,0,.06)">
{top}<tr><td class="pad" style="padding:26px 30px 30px"><table role="presentation" width="100%" cellpadding="0" cellspacing="0"><tr>{side}
<td class="stack" valign="top">{kicker("Today's big story", SUN)}{label(a)}
<a href="{esc(a["read"])}" style="text-decoration:none"><div class="hero-h" style="font:700 30px/1.18 {SERIF};color:{INK};margin:0 0 12px">{esc(a["title"])}</div></a>
<div style="font:17px/1.55 {SERIF};color:{BODY};margin:0 0 12px">{esc(a["sub"])}</div>{by}
{button(a["read"], where(a), color)}</td></tr></table></td></tr></table></td></tr>'''


def light_card(a):
    color = ECON_RED if a["mag"] == ECON else INK
    thumb = (f'<td class="thc" width="132" valign="top" style="padding:0 0 0 20px;width:132px"><a href="{esc(a["read"])}">'
             f'<img class="th" src="{esc(crop(a["img"], "1:1", 264))}" width="132" height="132" alt="" '
             f'style="display:block;width:132px;height:132px;border:0;border-radius:6px"></a></td>') if a.get("img") else ""
    by = f'<div style="font:600 12px/1.4 {SANS};color:{MUT};margin:0 0 12px">By {esc(a["by"])}</div>' if a["by"] else ""
    if a.get("note"):
        by += f'<div style="font:italic 13px/1.4 {SERIF};color:{MUT};margin:-6px 0 12px">{esc(a["note"])}</div>'
    return f'''<tr><td style="padding:0 0 22px"><table role="presentation" width="100%" cellpadding="0" cellspacing="0"
style="background:{CARD};border-radius:10px;box-shadow:0 1px 3px rgba(0,0,0,.06)">
<tr><td class="pad" style="padding:24px 30px 26px"><table role="presentation" width="100%" cellpadding="0" cellspacing="0"><tr>
<td valign="top">{kicker("And something lighter", SUN)}{label(a)}
<a href="{esc(a["read"])}" style="text-decoration:none"><div style="font:700 21px/1.25 {SERIF};color:{INK};margin:0 0 8px">{esc(a["title"])}</div></a>
<div style="font:15px/1.5 {SERIF};color:{BODY};margin:0 0 8px">{esc(a["sub"])}</div>{by}
<a href="{esc(a["read"])}" style="font:600 14px/1 {SANS};color:{color};text-decoration:none">{where(a)}&nbsp;&rarr;</a></td>
{thumb}</tr></table></td></tr></table></td></tr>'''


def build(day, P, lead, light, inline="data"):
    images, cover_src = {}, ""
    if lead and lead["mag"] == ECON:
        cover = economist_cover(P["econ_issue"])
        if cover:
            images["econ"] = cover
            cover_src = "cid:econ" if inline == "cid" else "data:image/png;base64," + base64.b64encode(cover).decode()
    cards = (lead_card(lead, cover_src) if lead else "") + (light_card(light) if light else "")
    subject = f"Daily Read · {lead['title'] if lead else light['title']}"
    pre = f"Plus: {light['title']}" if lead and light else ""
    doc = f'''<!DOCTYPE html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="color-scheme" content="light"><meta name="supported-color-schemes" content="light"><title>{esc(subject)}</title>
<style>
@media (max-width:620px) {{
  .pad {{ padding-left:20px !important; padding-right:20px !important; }}
  .stack {{ display:block !important; width:100% !important; }}
  .cov {{ padding:0 0 18px !important; }}
  .hero-h {{ font-size:25px !important; }}
  .th {{ width:88px !important; height:88px !important; }}
  .thc {{ width:88px !important; padding-left:14px !important; }}
}}
</style></head>
<body style="margin:0;padding:0;background:{PAGE};-webkit-text-size-adjust:100%">
<div style="display:none;max-height:0;overflow:hidden">{esc(pre)}</div>
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:{PAGE}"><tr><td align="center" style="padding:26px 12px 34px">
<table role="presentation" width="600" cellpadding="0" cellspacing="0" style="width:100%;max-width:600px">
<tr><td class="pad" style="padding:0 30px 20px">
<div style="font:700 11px/1 {SANS};letter-spacing:.16em;text-transform:uppercase;color:{MUT}">{day:%A}, {day:%B} {day.day}</div>
<div style="font:700 34px/1.15 {SERIF};color:{INK};margin:10px 0 0">Daily Read</div>
</td></tr>
{cards}
<tr><td class="pad" style="padding:4px 30px 0;font:13px/1.6 {SANS};color:{MUT}">
From this week's Economist (PressReader) and New Yorker (Gale), free through your {LIBRARY.upper()} library card.</td></tr>
</table></td></tr></table></body></html>'''
    text = f"Daily Read, {day:%A} {day:%B} {day.day}\n" + "".join(
        f"\n{tag}: {a['title']} ({a['mag']}, {a['kind']})\n  {a['sub']}\n  {a['read']}\n"
        for tag, a in (("TODAY'S BIG STORY", lead), ("SOMETHING LIGHTER", light)) if a)
    return subject, doc, text, images


# ---------------------------------------------------------------- main
if __name__ == "__main__":
    today = datetime.now(PT).date()
    if "--date" in sys.argv:
        today = date.fromisoformat(sys.argv[sys.argv.index("--date") + 1])
    log = load_log()

    if "--week" in sys.argv:     # dry run: the next 7 days' picks from today's pools (no log written)
        P, sim = pools(today), list(log)
        for i in range(7):
            d = today + timedelta(days=i)
            lead, light = pick(P, sim)
            sim.append(log_entry(d, lead, light))
            print(f"{d:%a %b %d}  LEAD  {lead['mag'][4:7] if lead else '-'} {lead['title'][:60] if lead else '(none)'}")
            print(f"{'':10}  LIGHT {light['mag'][4:7] if light else '-'} {(light['kind'] + ': ' + light['title'])[:60] if light else '(none)'}"
                  f"{'  [' + str(light['score']) + ']' if light else ''}")

    if "--out" in sys.argv:
        P = pools(today)
        lead, light = pick(P, [e for e in log if e["date"] < today.isoformat()])
        subject, doc, text, images = build(today, P, lead, light)
        out = sys.argv[sys.argv.index("--out") + 1]
        with open(out, "w", encoding="utf-8") as f:
            f.write(doc)
        print(f"reading: written to {out} ({subject})")

    if "--send" in sys.argv:
        if log and log[-1]["date"] == today.isoformat() and "--force" not in sys.argv:
            print("reading: already sent today")
            sys.exit(0)
        P = pools(today)
        prior = [e for e in log if e["date"] < today.isoformat()]
        lead, light = pick(P, prior)
        if not (lead or light):
            print("reading: nothing new in either issue - not sent")
            sys.exit(0)
        subject, doc, text, images = build(today, P, lead, light, "cid")
        if send(subject, doc, text, images, sender="Daily Read"):
            os.makedirs(os.path.dirname(LOG), exist_ok=True)
            with open(LOG, "w", encoding="utf-8") as f:
                json.dump((prior + [log_entry(today, lead, light)])[-120:], f, indent=1)
