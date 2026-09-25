# Oregon Weather Dashboard: notes for Claude

A static weather dashboard for Oregon/Washington, built by Python into `docs/index.html`
(one big page, tabs in a left sidebar). Personal project; the owner iterates on it visually
and asks for features in plain language. See README.md for data sources and dev notes.

## Run it (Windows, PowerShell 5.1)
- Python: `C:\Users\aaron\AppData\Local\Python\pythoncore-3.14-64\python.exe` (the `python`
  store alias is unreliable; use the full path). No `&&` in PowerShell 5.1: use `; if ($?) {...}`.
- Build: `$env:WX_CACHE_TTL_HOURS='24'; & <python> weather_dashboard.py` (~1-2 min from cache).
  The dev cache (`.cache/`, `http_cache.py`) keeps Open-Meteo quota low while iterating; it
  skips binary responses. `WX_CACHE=0` disables it. `WX_REGION_KM=60` = cheap dev Map grid.
- Serve: `python -m http.server 8000 --directory docs`, then http://localhost:8000/
- Secrets: `.env` holds `MAPBOX_TOKEN` (never print it, never commit it; `.env`, `docs/`,
  `.cache/` are gitignored). The owner keeps their keys; don't suggest rotating them.
- Preview for the owner: `tools/to_artifact.py docs/index.html <out>` then publish with the
  Artifact tool to https://claude.ai/artifact/HVFm7WR4ze6fgQddH77yrx (same URL each time).
  Maps/cameras/live fetches don't run in the preview (its CSP blocks outside hosts).
- Live site: https://aaronmaddenjk.github.io/oregon-weather-dashboard/ (GitHub Pages, served from
  the `gh-pages` branch). Repo: github.com/aaronmaddenjk/oregon-weather-dashboard (public; code on
  `main`). Built on THIS PC, not in Actions: Open-Meteo throttles GitHub's shared runner IPs to
  timeouts (tried; removed the workflow). `tools/publish.ps1` builds fresh and force-pushes docs/
  as a single commit to gh-pages (`-NoBuild` = publish current docs/); log in `logs/publish.log`.
  `tools/schedule.ps1` registers the Windows task "Oregon Weather Dashboard" (5 AM + 3 PM).
  A full fresh build is ~4,600 Open-Meteo calls (Map grid ~3,100) and ~19 min, so max ~2/day
  until the split refresh (hourly light build, grid every few hours) exists.
- git/gh are installed but not on PowerShell's PATH in this shell: prefix commands with
  `$env:Path = "C:\Program Files\Git\cmd;C:\Program Files\GitHub CLI;" + $env:Path`.
  Commit as aaronmaddenjk / 242111455+aaronmaddenjk@users.noreply.github.com (set in repo config).
- GitHub push protection flags the public Mapbox `pk.` token in the built page; the owner
  allowed it (false positive). Never put an `sk.` (secret) token in the page.
- Test in the built-in browser pane; after edits, rebuild and check the page. Always verify
  visually (screenshots) and with small JS checks before reporting done.

## Tabs (page ids in the shell, `build_dashboard()`)
page0 Cities · page1 Mountain Trails · page2 Map · page3 Mt Hood · page4 Trail Forecast · page5 Accuracy.
Sidebar button order must match page index. `data-init` / `data-lazy` build maps on first view
(Mapbox bills per map load).

## Modules
- `weather_dashboard.py`: everything page-related (builders per tab, shell, CSS, JS strings).
- `region.py` (Map grid + `vertical_profile`), `nws.py` (NWS base forecast), `snow_model.py`
  (wet-bulb rain/snow, SLR, model blend), `verification.py` (SNOTEL scoring/calibration),
  `snowpack.py` (Mt Hood snow-depth estimate), `smoke.py` (NOAA smoke PNGs),
  `fires.py` (NIFC perimeters), `webcams.py` (USGS AshCam + NPS cams), `trail_live.py`
  (Trail Forecast tab: in-browser engine), `http_cache.py`.
- `chrome-extension/`: MV3 extension; on an AllTrails trail page it reads the embedded route
  polyline (`"pointsData":"$xx"` in the Next.js `self.__next_f` stream; falls back to fetching
  `/explore/trail/...`) and opens `<dashboard>#trail={"n","u","p"}`.

## Shared JS components (global scripts, defined once in the shell)
`WxCharts(panel,{vis})` 24-hour charts · `WxCams(root)` camera view · `TerrainLayers` (Mt Hood
elevation-colored layers) · `RegionLayers(opt)` = the Map tab's full layer engine, also used by
the Trail Forecast map · `AQL` air quality · `wxArt` weather-on-mountain art (Trails pins, Hood
video poster). Page scripts are wrapped in IIFEs: expose anything cross-script via `window.`.

## Gotchas learned the hard way
- Page CSS for Mt Hood and Trail Forecast is scoped with `scope_css(css, '#page3'/'#page4')`.
  A `display:` rule overrides the `hidden` attribute: add `[hidden]{display:none!important}`.
- Python strings holding HTML/JS: in raw strings `\u2014` stays literal (fine inside JS strings,
  wrong in HTML text). Non-raw strings decode it. Existing source often has the literal char.
- "Today" = the first forecast day with hours ahead (cached responses can start on yesterday).
- Python requests to volcview.wr.usgs.gov stall ~22 s per call on this PC (browser is fast).
- Map tab layers (RegionLayers) only cover Oregon + Washington (`region.BOUNDS`).
- Edits: long multi-part changes were done with small Python edit scripts that assert each
  replacement matches exactly once; keep that discipline.

## Owner's preferences
- NWS is the base forecast everywhere; our model blend runs in the background. The
  model-vs-NWS comparison stays background data, not a UI feature.
- Visual language: accent orange `#FE5000`; snow violet `#5A4FCF`, rain `#0E9AAE`, mix
  `#DE6A52` (palette-validated); temperatures via `temp_bg`; clean, quiet chrome; no
  heavy color blocks. Follow the dataviz rules (one axis per chart, legends, hover layers).
- Likes: OpenSnow-style features, mountain icons with weather art, layouts consistent
  across Cities / Trails / Mt Hood / Trail Forecast (map + 24-hour panel, tables below).
- Wants concise status while working and a short summary of what changed at the end.

## Open ideas (not done)
- 3-hourly drill-down in the Trail Forecast 10-day table; KML import; recent-trails list;
  OpenStreetMap / Waymarked Trails links + trail search; verify gusts against ridge stations.
- Split refresh: hourly light build (cities/trails/Hood/smoke/fires) reusing the last Map grid,
  full grid every ~6 h; plus a page self-reload when a newer build is published.
