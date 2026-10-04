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
- SCHEDULED BUILDS RUN ON THE ORACLE SERVER since 2026-10-02 (owner wanted free + hands-off; the
  laptop's Modern Standby ignores wake timers and GitHub Actions can't reach Open-Meteo, see below).
  Oracle Cloud Always Free, region Phoenix AD-1, Oracle Linux 9 (aarch64, 1 OCPU, 6 GB), public IP
  129.146.176.33, user `opc`, SSH key on the PC `~/.ssh/oracle_wx` (ssh -i ~/.ssh/oracle_wx
  opc@129.146.176.33). Project in ~/dash (git clone of main, Python 3.12 in .venv, .env, .cache/terrain
  + .cache/explorer copied over), cron (crontab -l) runs `tools/publish.sh` HOURLY (:10, since
  2026-10-03); log ~/dash/logs/publish.log. publish.sh resets to origin/main, picks per-source cache
  lifetimes from the hour (http_cache.ttl_class: live = NWS/smoke/fires/cams every build; points =
  Open-Meteo spots at 2/8/14/20 h; aqgrid at 4/16 h; grid = Map grid + daily = SNOTEL/past runs/
  verification (WX_VERIFY=1) at 4 h; ~7,900 Open-Meteo calls/day), builds (hourly ~4 min, refresh
  hours longer, a full one ~12 min), commits verification/ back to main (nws_log.json builds up 150
  days), force-pushes docs/ to gh-pages. Every build writes docs/version.json; open tabs check it every
  5 min (RELOAD_JS: reload if hidden, else a "Newer forecast" notice). A change to publish.sh itself
  takes effect the run after the one that pulls it (bash keeps reading the old file).
  GitHub access = a repo deploy key (read-write, "Oracle server (weather-dashboard)"), private half
  only on the server. Its Open-Meteo quota is its own (the PC's is free for dev + live use).
  So on the PC: don't commit verification/*.json from dev builds (`git checkout -- verification`), and
  `git pull --rebase` before pushing (the server pushes verification commits to main).
  Sending scripts to the server from PowerShell: write a file, scp it, strip BOM/CR with sed, run with
  bash (inline quoting breaks). The PC task "Oregon Weather Dashboard" is DISABLED (fallback only).
- Live site: https://aaronmaddenjk.github.io/oregon-weather-dashboard/ (GitHub Pages, served from
  the `gh-pages` branch). Repo: github.com/aaronmaddenjk/oregon-weather-dashboard (public; code on
  `main`). Built on THIS PC, not in Actions: Open-Meteo throttles GitHub's shared runner IPs to
  timeouts (tried; removed the workflow). `tools/publish.ps1` builds fresh and force-pushes docs/
  as a single commit to gh-pages (`-NoBuild` = publish current docs/); log in `logs/publish.log`.
  `tools/schedule.ps1` registers the Windows task "Oregon Weather Dashboard" (5 AM + 3 PM).
  The task is ENABLED since 2026-09-28 (owner ready for a refresh cadence); pause it with
  `Disable-ScheduledTask -TaskName "Oregon Weather Dashboard"`. Open-Meteo's day resets ~5 PM
  Pacific, so 5 AM + 3 PM share one quota day (~9,200 of 10,000): don't run an extra fresh build
  between 5 PM and 3 PM, or the 3 PM build can run out (publish with -NoBuild instead).
  A full fresh build is ~4,600 Open-Meteo calls (Map grid ~3,100) and ~19 min, so max ~2/day
  until the split refresh (hourly light build, grid every few hours) exists.
- The task has WakeToRun (2026-09-29, owner asked) and publish.ps1 holds ES_SYSTEM_REQUIRED while it
  runs. The laptop is Modern Standby; Windows only honours the wake when "Allow wake timers" = Enable
  (owner sets it: plugged in only; on battery it stays off on purpose - a closed laptop in a bag).
- The scheduled task's Python doesn't see the per-user site-packages (AppData\Roaming\Python), so
  packages must live in Python's own Lib\site-packages: `$env:PYTHONNOUSERSITE='1'; python -m pip
  install -r requirements.txt` (first scheduled run 2026-09-29 failed on `import dotenv`). This Claude
  shell is sandboxed: to check the real environment or the real task, run with the sandbox disabled.
- git/gh are installed but not on PowerShell's PATH in this shell: prefix commands with
  `$env:Path = "C:\Program Files\Git\cmd;C:\Program Files\GitHub CLI;" + $env:Path`.
  Commit as aaronmaddenjk / 242111455+aaronmaddenjk@users.noreply.github.com (set in repo config).
- GitHub push protection flags the public Mapbox `pk.` token in the built page; the owner
  allowed it (false positive). Never put an `sk.` (secret) token in the page.
- Test in the built-in browser pane; after edits, rebuild and check the page. Always verify
  visually (screenshots) and with small JS checks before reporting done.

## Tabs (page ids in the shell, `build_dashboard()`)
page0 Cities · page1 Volcanos (was Mountain Trails) · page2 Map (+ Trails layer and side panel) · page3 Mt Hood ·
page4 Trail Forecast (`TRAIL_TAB=4` in trail_live.py) · page5 Accuracy.
Sidebar button order must match page index. `data-init` / `data-lazy` build maps on first view
(Mapbox bills per map load).

## Modules
- `weather_dashboard.py`: everything page-related (builders per tab, shell, CSS, JS strings).
- `region.py` (Map grid + `vertical_profile`), `nws.py` (NWS base forecast), `snow_model.py`
  (wet-bulb rain/snow, SLR, model blend), `verification.py` (SNOTEL scoring/calibration),
  `snowpack.py` (Mt Hood snow-depth estimate), `smoke.py` (NOAA smoke PNGs),
  `fires.py` (NIFC perimeters), `webcams.py` (USGS AshCam + NPS cams), `trail_live.py`
  (Trail Forecast tab: in-browser engine), `trail_explorer.py` (the Map tab's Trails layer: data
  build + `TrailsLayer` JS + side panel), `http_cache.py`.
- Trails data (~8.3k trails): USGS National Digital Trails (carto.nationalmap.gov transportation
  MapServer/37: USFS/NPS/BLM/FWS/WA State Parks) + Oregon Parks & Rec (OPRD_Rec_Trails_Hosted_view)
  + Oregon Dept of Forestry (Recreation_Inventory_Public_View/13, has difficulty) + Oregon Metro RLIS
  Trails (open, unpaved, not state/federal). Not covered yet: Saddle Mountain SNA, non-Metro city
  trails (OSM is the planned fill). The Map tab's "Trails" button (RegionLayers) creates
  `window.TrailsLayer`; the Trail Forecast map gets lines + tips but no panel. GPX uploads live in
  "Your trails" (GPX uploads + every trail opened from the extension) live in `trails.json` on the repo's
  `trails` branch (public, owner OK'd; publishing only replaces gh-pages). `window.WxMine` in TRAILS_JS
  reads it keyless (GitHub API, raw.githubusercontent fallback) and writes it via the contents API with
  a fine-grained key the owner pastes once per browser (Map → Trails → "connect GitHub"; localStorage
  `wx-gh-token`, per site origin). localStorage `wx-mytrails` is the local copy; `synced` = came from
  GitHub (so remote removals propagate); unsynced local ones upload on connect. Never put a key in code.
  Measured via `window.WxTrail` from trail_live.py. The extension (1.3+) opens the GitHub Pages
  dashboard by default (options page can set localhost).
  The trail card's elevation profile: build stores elevation every 0.1 mi (ft, 100 m-smoothed,
  from the low end; `prof` field, delta-encoded) - each mile coloured by its average grade:
  Easy <8%, Medium 8-15%, Hard 15-22%, Strenuous 22%+ (one orange ramp; `GR` in TRAILS_JS);
  hover moves a dot along the trail on the map. Your trails get `prof` from `WxTrail.profile`.
  Trails arriving from the Chrome extension (#trail=) are also kept there (`keepTrail` in
  trail_live.py, one entry per link, saved before the forecast fetch so a failed forecast keeps it).
  Pieces stitched by name+number+agency (ends within 150 m),
  snow routes (SNO- numbers) dropped, caps names prettified. Raw download cached a week in
  `.cache/explorer/`; elevation from Mapbox terrain-RGB z12 tiles cached forever in
  `.cache/terrain/` (~2,800 tiles, downloaded once; Mapbox serves them slowly, ~2/s) and per-trail
  results in `.cache/explorer/elevation.json`. `WX_EXPLORER_MAX_TILES` (default 6000) caps new tile
  fetches per build. Output `docs/explorer/trails.json` (~1.8 MB), loaded when the tab opens.
  "Forecast this trail" → `window.openTrailForecast(poly, name, link)` in trail_live.py.
  AllTrails' search URL ignores ?q=; the card links to `/explore?b_tl_lat=..&b_br_lng=..` instead.
- `chrome-extension/`: MV3 extension; on an AllTrails trail page it reads the embedded route
  polyline (`"pointsData":"$xx"` in the Next.js `self.__next_f` stream; falls back to fetching
  `/explore/trail/...`) and opens `<dashboard>#trail={"n","u","p"}`. Also onX Backcountry web map
  (webmap/backcountry.onxmaps.com): onX trail guides `/map/hike-route/<id>` (bike-/ski-/snow- too) →
  POST `/v1/supergraph/` GraphQL `routesConnection(filter:{id}, limit:1){edges{node{... on HikeRoute
  {name geometry}}}}` (GeoJSON); your saved routes `/map/route/<id>` → `GET api.production.onxmaps.com/v1/routing/
  routes?excludeSteps=&page[size]=50` (route.geometry = polyline, precision 5); `/map/line/<uuid>`
  (recorded tracks) → `/v1/markups/tracks?limit=500` then `/markups/lines` (geo_json [lon,lat,ele],
  re-encoded). Auth: Bearer access_token from localStorage `oidc.user:*` + headers
  `onx-application-id: backcountry`, `onx-application-platform: web` (without them lists come back empty).
  Trailforks (extension 1.4): /trails/<slug>/ and /route/<slug>/ pages carry the line inline in the map
  script - `geoJSON.push({... properties:{'type':'trail'|'route','name':...}, geometry:{type:'LineString',
  encodedpath:'<polyline p5>'}})` (JS single-quoted, backslash-escaped). Activity: `"a"` in #trail
  (Trailforks + onX BikeRoute = mtb, else hike) -> saved as `act` in trails.json; "Your hikes" / "Your
  MTB trails" land filters, MTB = "Bikes OK". Extension 1.5 adds `"d"`: the page's "Difficulty rating"
  (trails "Blue", routes "Black Diamond"; else the first dicon title) -> green | blue | black | dblack |
  access, saved as `dif`. Your MTB trails are drawn in Trailforks colours (owner: green / blue / black,
  purple = access / fire road; `DIF` in TRAILS_JS), your hikes raspberry `#B0457E` (not purple any more).
  MTB trails also show descent (`loss`, ft, summed drops of the 0.1-mi profile; older entries computed
  from `prof`); Lower Hide and Seek 626' vs Trailforks' 635'.

- Cities map: slim pills (icon, name, one number that follows the shading switch: feels like now /
  rain next 24 h / gusts now; the selected one just gets an orange ring, no pop-open details or dot -
  owner's choice). Pills for Forks, Cannon Beach, Pacific City, Florence (over the ocean) and Portland
  (clear of Sandy) sit west of the town (`WEST` in the map JS); switch + legend in the right corners.
  Under them, a soft wash from `window.RegionWash(kind)` in REGION_JS: the Map
  grid at each cell's ground elevation (`celev` in region data), temp now / gusts now / rain next
  24 h. "Feels like" = NWS wind chill (<=50°F, wind >3 mph) / heat index (>=80°F), `feels_like()`;
  h24 hours carry `feels`, and WxCharts draws it as a two-way whisker on the temperature bars.

- Cities header: the selected city's sunrise / sunset / daylight (+ change tomorrow) and the moon,
  computed in Python (`sun_times`, NOAA solar equations; `moon_phase`, mean synodic month). Coastal
  cities get a 6-day tide strip under their forecast: `tides.py`, NOAA CO-OPS hi/lo predictions
  (free, no key), hand-picked stations (nearest-by-distance is often up a river): La Push,
  Garibaldi (no Cannon Beach station), Nestucca Bay entrance, Florence USCG Pier. The build embeds
  the hi/lo events; `tideBox`/`tideSVG` in CITY_JS draw the curve in the page: half-cosine between
  hi/lo, nights shaded (`sunUTC`, a JS port of sun_times), minus tides teal between curve and 0 ft,
  times only on highs and lows (owner: no heights; exact heights in the dots' hover titles).
- Cities detail: only the selected city's 6-day forecast shows (owner's choice). Cities + Volcanos
  "layout A" (owner, `LAYOUT_CSS`): left 2/3 = the selected place's table with the charts under it,
  right 1/3 = the map, as tall as both (ResizeObserver -> map.resize). On the narrow Cities map the
  pills place themselves (`place()` in the map JS): preferred side (east; WEST ones west), else the
  other side / below / above, never off the map, over another pill or the controls.
- Charts (WxCharts grid mode, owner): 2x3 - temperature, precipitation, chance + thunder (NWS
  probabilityOfThunder, inner bar), wind + direction arrows (NWS windDirection), then cloud (+UV in
  the summary/tooltip) + air quality on Cities, visibility + snow level (NWS snowLevel, dashed line at
  the chosen elevation) on Volcanos. Humidity left out on purpose (owner: not an issue in the PNW).
  The Cities "Clouds" chart is a cross-section (owner, `cloudChart`): altitude in three equal bands
  (low 0-6.5k / mid 6.5-20k / high 20k+ ft, each linear), each hour's layers shaded by cover, the base
  in the summary + tooltip. Drawn Windy-style (owner: "like an actual cloud"; the earlier puffs + base
  line looked bad and the line is gone): a blue sky panel, white clouds (whiter/more opaque with more
  cover, slightly grey when overcast), each layer from its band top down to the base when the base is
  in that band (an hour without a base borrows its neighbour's), one SVG filter = fractal-noise
  displacement + blur so the hour blocks merge into ragged soft clouds. SVG ids per chart (`cloudChart.n`).
  Volcanos (in place of Visibility) and Mt Hood (in place of Visibility; HOOD next24 carries cl/cm/ch/cb)
  use it too, with the forecast point's elevation (`set(..., {elev})`) as a dotted "This spot" line.
  Visibility now lives in the tooltip (shown when under 10 mi).
  Cloud colour (owner, final): one grey scale on the blue sky - pale grey = thin, dark slate = overcast
  (`[226,230,235]` -> `[112,122,136]`); rain chance is NOT in the cloud colour (tried, owner preferred drops).
  Rain potential (owner):
  the CHANCE of precipitation sets the drops: from 20% a sparse sprinkle to a dense curtain at 90%+
  (1-3 columns per hour, tighter rows), rain streaks (teal) / snow dots (violet) / mix (both) falling
  from the lowest cloud's bottom to the ground, scattered by a fixed hash, drawn over the clouds.
  Type = the hour's type at the point, else from temperature (<=33 snow, <=36 mix); no pop (Mt Hood
  panel) = any >= 0.01" counts as 70%. No rain-to-snow switch with height. Legend adds Rain/Snow +
  "more drops = likelier" only when present. To preview with fake data while the forecast is dry: a temp page in docs/ that
  evals the WxCharts script from /index.html, rendered by headless Edge (--screenshot / --dump-dom). hour_cols ships cl/cm/ch (%) and cb = the table's Base text as a number (`_base_ft`; the
  15k/28k "mid/high only" stand-ins are not bases; Fog = ground). Searched towns: Open-Meteo layers, base =
  LCL under low cloud.
  Mt Hood + Trail Forecast keep the old single column. No more 3-hourly table expansion anywhere:
  clicking a day in a table shows that day's hours in the charts (click again: next 24 h); the build
  ships every hour as packed columns (`hour_cols` -> `WxCharts.hours`), `wxDay(uid, i)` dispatches.
- Volcano snow depth (owner asked for it): `snowpack` method per waypoint - SNOTEL depths within 35 km
  of the summit fitted against elevation, then hourly new snow / melt / settling; a "Snow depth" table
  row (end of day), "~N″ seasonal snow" beside the elevation name, `sd` in the chart tooltip. Seasonal
  snow only (glaciers and old snowfields not counted) - say so wherever it's shown.
- Cities map shading switch also has "Air": the shared AQL layer (CAMS grid) drawn with
  `strong` (0.6 alpha everywhere), today's 3-hourly frame nearest now; pills show AQI.
- Cloud rows: total cover is NWS sky, layers are ECMWF (HRRR today). `harmonize_clouds()` scales the
  layers each hour so 1-(1-low)(1-mid)(1-high) = NWS sky, keeping the model's split; when ECMWF has no
  layers it borrows the split from Open-Meteo best-match (the request that already fetches precip
  chance). If no model has layers, the Base row says Few / Scattered / Cloudy, never a dew-point-guessed
  height (owner saw bases under empty rows).
- Cities town search (box top-left on the map): Open-Meteo geocoding, OR/WA only; forecast computed
  live via `window.WxPoint` (trail_live.py's engine, one point at the geocoded elevation), shown as a
  temporary index S = len(CITIES) in H24/SUN/NAMES/select/pills plus `#city_detail_<S>` (7-day
  table from WxPoint.table). NOT saved anywhere (owner: forget saving). Tides for a searched town only
  within 5 miles of salt water (owner's rule; Puget Sound counts): `coast_or_wa.json` = Natural Earth
  10 m coastline in the OR/WA box (outer coast, estuaries/bays, Strait, Puget Sound, Hood Canal, San
  Juans; not the Columbia above Cathlamet); then the nearest NOAA station within 25 km from
  `tides.stations()` (embedded at build), stations east of the town (up-river) penalised; fetched
  live from CO-OPS (CORS ok).

## Phones (one responsive page, owner, 2026-10-03; everything under `@media (max-width:760px)` in tab_css)
Bottom tab bar (icon over a short label, safe-area aware; showTab scrolls to the top); maps ~56% of the
screen (Map tab 62%), `cooperativeGestures` on touch screens (a wrapper around mapboxgl.Map in the head),
the Map tab's layer buttons in one swipe row, no zoom buttons; charts: tap / drag sideways to read, the
tip stays after lifting, sits above the finger, `touch-action:pan-y`; tables: narrow wrapping sticky row
labels, high over low, Accuracy first column sticky; cameras size to content (`.cc-cams` flex:1 0 auto).
Test at the "mobile" preset (375x812) with DOM measurements; the pane often stops drawing screenshots.

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

- Trail Forecast elevations come from Mapbox terrain-RGB tiles (zoom 13, 2-4 tiles/trail, line
  densified to 10 m), gain from elevations averaged over 100 m. Matched AllTrails within ~1%
  (St Helens 4,687' vs 4,639'; South Sister 5,036' vs 5,036'); Open-Meteo's 90 m DEM overcounted
  ~15%. Open-Meteo elevation is only the fallback. Owner watches Mapbox usage: keep tile counts low.
- Open-Meteo's ~10k/day is per IP: a fresh full build (~4,600) plus dev work can exhaust it,
  after which the Trail tab's forecasts fail from home until the daily reset.

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
- (Owner passed for now, 2026-09-29) OSM MTB trails in the Trails layer (Overpass: bike-designated /
  mtb:scale ways; Post Canyon had 253 ways, 80 named, ~45 with mtb:scale:imba -> difficulty colours)
  plus a "Download GPX of the trails in view" button. Never bulk-pull Trailforks (their data, not OSM).
- Trails next steps: OpenStreetMap for the remaining city / county trails; chain
  connected trails into hikes (route builder); NPS API + Recreation.gov descriptions (free keys);
  "My trails" list of everything forecasted.
- 3-hourly drill-down in the Trail Forecast 7-day table; KML import; recent-trails list;
  OpenStreetMap / Waymarked Trails links + trail search; verify gusts against ridge stations.
- GitHub Actions re-tested 2026-10-02 (tools/openmeteo_probe.py, 360 build-like requests): ~30% of
  Open-Meteo requests time out from GitHub runners even paced at 480 calls/min (0 x 429, just dropped
  connections), on two different runner IPs. Actions + Open-Meteo is not viable; a free Actions build
  would need the Open-Meteo parts replaced (e.g. NOAA GRIB for the Map grid). Google's free e2-micro
  needs a ~$3.65/mo IPv4 (owner: must be free).
- Move scheduled builds off the PC to an Oracle Cloud Always Free VM (own IP, so Open-Meteo
  doesn't throttle it; owner's preferred host, over Google's e2-micro whose external IP may
  cost ~$3.65/mo). Owner creates the account; then: Python, repo clone, .env, gh auth, cron
  running tools/publish logic (port publish.ps1 to bash). Watch Oracle's idle-VM reclaim rule.
- Split refresh: hourly light build (cities/trails/Hood/smoke/fires) reusing the last Map grid,
  full grid every ~6 h; plus a page self-reload when a newer build is published.
