# Oregon Weather Dashboard — migration handoff

## Goal
Take `weather_notebook_original.txt` (a Databricks Python notebook) and turn it
into a standalone script that runs on a daily GitHub Actions schedule, publishes
a static HTML dashboard to GitHub Pages (bookmarkable link), and optionally
emails the result.

## What's already decided (don't re-litigate these)
- **Two hardcoded API keys** in the original notebook (`MAPBOX_TOKEN`, `MB_KEY`
  for Meteoblue) must become environment variables, not module constants.
  **The Meteoblue key should be rotated** before it's used anywhere outside
  Databricks — it's been sitting in plaintext in a Nike workspace.
- **Drop these cells entirely** — they're Databricks-only, not needed outside
  the notebook:
  - "Interactive Mapbox Explorer" (exploratory tool for picking map params)
  - "Export Notebook as Text File" (uses `WorkspaceClient`, Databricks-only)
- `reportlab` is `pip install`ed at the top of the notebook but never actually
  used anywhere in the code — leave it out of `requirements.txt`.
- Output changes from `displayHTML(...)` / writing to a Databricks Workspace
  path, to just writing a string to `docs/index.html`.
- **Mapbox GL JS cannot be tested inside a Claude-published artifact** (its
  script host isn't on the allowed CDN list there) — that's why
  `prototype.html` uses a placeholder div for the map panels. It'll work fine
  once this runs as a real page in a real browser; just don't expect to
  preview it inside Claude's artifact viewer.
- The dashboard has 3 tabs: **Cities**, **Mountain Trails** (South Sister + Mt
  St Helens hiking forecasts with a custom cloud-base-altitude estimate), and
  **Mt Hood Ski** (lift status, road cams, scraped snow forecast).

## Files in this folder
- `weather_notebook_original.txt` — the source of truth for all the actual
  forecast/rendering logic. Port sections from here into `weather_dashboard.py`.
- `weather_dashboard.py` — skeleton with Databricks parts stripped and TODO
  comments marking exactly which line ranges of the original to port in
  (helper functions, `render_hike_forecast`, Cities page, Ski page, combine step).
- `prototype.html` — a finalized, working mockup of the tab nav / forecast
  table / card layout using the **real CSS classes and color logic** from the
  notebook, with mock data. Treat this file's markup and CSS as the visual
  target — the goal is for the live version to produce HTML structurally
  identical to this, just filled with real numbers instead of mock ones.
- `requirements.txt`, `.gitignore`, `.env.example` — project scaffolding.
- `tools/publish.ps1` — builds on this PC and force-pushes `docs/` to the
  `gh-pages` branch, which GitHub Pages serves at
  https://aaronmaddenjk.github.io/oregon-weather-dashboard/. `tools/schedule.ps1`
  runs it twice a day via Windows Task Scheduler (log: `logs/publish.log`).
  (A GitHub Actions build was tried and removed: Open-Meteo's free tier throttles
  GitHub's shared runner IPs until requests time out.)

## Next steps, in order
1. Rotate the Meteoblue key.
2. `pip install -r requirements.txt`, copy `.env.example` to `.env` with real
   keys, and get `weather_dashboard.py` actually running locally end to end —
   port one section at a time from the original notebook, testing after each.
3. Diff the real generated HTML against `prototype.html`'s structure to make
   sure styling carried over correctly.
4. Push to a GitHub repo, add `MAPBOX_TOKEN` as an Actions secret, enable
   GitHub Pages from `docs/`. (Meteoblue has been replaced by NOAA NBM via
   Open-Meteo, so `MB_KEY` is no longer needed.)
5. Optional: add an email-delivery step to the workflow (Resend/Mailgun).

## Developing without burning API quota
- Local runs cache every API response in `.cache/` for 3 hours (`http_cache.py`), so
  rebuilding after a layout/maths change makes no network calls (~8 s instead of ~4 min).
  Each build ends with an `API usage:` line estimating the Open-Meteo quota it used.
- `WX_CACHE_TTL_HOURS=12` keeps responses longer; `WX_CACHE=0` forces fresh data.
  GitHub Actions never caches.
- `WX_REGION_KM=60` builds the Map tab on a coarse development grid (~130 cells, ~700
  calls) instead of the default 25 km (~700 cells, ~2,700 calls).

## Known fragile bits worth flagging to the user if they act up during porting
- `fetch_latest_youtube` and `fetch_gorge_snow_forecast` scrape pages rather
  than using a stable API — they can break if either site changes markup.
- `fetch_latest_youtube` now falls back to the channel's /videos page because
  YouTube's RSS feed for the channel returns 404.
- All forecast data comes from Open-Meteo's free tier (non-commercial use,
  10,000 calls/day); one run makes ~140 requests. Open-Meteo weights large
  requests (like the 64-member ensemble) as several calls, but a daily run
  stays far under the limit.
- The Map tab's fire layer (`fires.py`) comes from NIFC's WFIGS current
  perimeters feed (no key). That feed keeps a fire until it's declared out, so
  only wildfires under 100% contained, 10+ acres, with a perimeter mapped in
  the last 14 days are shown.
- The Map tab's smoke layer (`smoke.py`) is NOAA's NDGD smoke guidance
  (HRRR-Smoke based), from the NWS map services (no key): near-surface smoke
  (µg/m³) and vertically integrated smoke (mg/m²), hourly for ~1-2 days. Each
  build writes one small grayscale PNG per hour per field to `docs/smoke/`;
  the page colours them. The dev cache skips these binary responses.
- Trails-tab webcams (`webcams.py`) come from the USGS AshCam catalog
  (NPS, USGS, ski-area and highway cameras around the Cascade volcanoes) plus
  the NPS Hurricane Ridge cam. The build only checks which cameras are alive;
  the page loads photos and times live from the AshCam API when the Cameras
  view opens, and at night shows each camera's last daylight photo (worked
  out from its sunrise/sunset times, since the catalog's night flag starts
  hours early). Add or reorder cameras in `webcams.CAMS`.
- The Mt Hood tab's terrain layers (temperature, wind gusts, new snow, snow
  depth) colour the 3D map by elevation using the same forecast-by-elevation
  as the Map tab (`elevation_profile` -> `region.vertical_profile`), 3-hourly
  for 7 days. Snow depth (`snowpack.py`) is an estimate: today's SNOTEL depths
  near Hood fitted against elevation, plus forecast snow, minus a degree-day
  melt and settling. The forecast tables' snow depth uses the same estimate.
- The Trail Forecast tab (`trail_live.py`) runs entirely in the browser: drop a
  GPX and it calls Open-Meteo and the NWS directly, then applies the same
  method as the build (NWS base moved to each point's elevation, GFS free-air
  ridge wind, wet-bulb snow, terrain layers via the shared `TerrainLayers`).
  Base = the track's lowest point, peak = its highest. Nothing is saved except
  the last GPX, in that browser's localStorage. AllTrails links can't be read
  (no API, bot protection), so the link field only names the trail.
- The Trail Explorer tab (`trail_explorer.py`) maps ~7,400 official trails in
  Oregon and Washington from the USGS National Digital Trails dataset (public
  domain), stitched from junction-to-junction pieces into whole trails, with
  length, allowed uses, and gain / high / low points from Mapbox terrain. The
  terrain tiles (~2,800) download once into `.cache/terrain/`; later builds only
  compute new or changed trails. "Forecast this trail" opens it in Trail Forecast.
- `chrome-extension/` sends an AllTrails trail straight to that tab. Install:
  Chrome → `chrome://extensions` → turn on Developer mode → Load unpacked →
  pick the `chrome-extension` folder, then pin it. It also works in the onX
  Backcountry web map: open any onX trail (search or click one on the map), or
  one of your own routes or recorded tracks (My Content → Routes / Tracks), and
  click it; it reads the line from onX with the web map's own login. On any AllTrails trail page, click it: it reads the route the page loaded (in your own session;
  if the page only has a static map it fetches the trail's map view) and
  opens `<dashboard>#trail=...`. The dashboard address defaults to
  `http://localhost:8000/` (serve `docs/` there) and can be changed in the
  extension's options, e.g. to the GitHub Pages URL. It relies on AllTrails'
  page structure, so an AllTrails redesign can break it; GPX export still works.
