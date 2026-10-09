// Trail Forecast for AllTrails, onX and Trailforks
// On an AllTrails trail page, one of your routes or tracks in the onX Backcountry web map, or a
// Trailforks trail or route, the route is read from the page you're on (in your own, logged-in session):
//   {"n": name, "u": link back, "p": encoded polyline, "a": "hike" | "mtb",
//    "d": Trailforks difficulty: green | blue | black | dblack | access}
// Click the button (or Alt+Shift+S): SAVE it to your trails and stay on the page. A background tab of
// the dashboard (<dashboard>#save=...) measures it (Mapbox terrain: gain, profile), keeps it (GitHub when
// that browser is connected), answers in its address (#saved=...) and is closed. Saves run one at a time,
// so trails opened in several tabs can be saved in a row. Alt+Shift+A (or right-click: "Save every trail tab
// in this window") does the same for each trail tab already open in the current window, skipping ones
// already saved (owner, 2026-10-08). The trail list page (list.html) can also open a batch of 20 of your
// collected links and have each saved and its tab closed (owner asked, 2026-10-08): one batch per click,
// never continuing by itself; a tab that fails stays open.
// Right-click the button: "Save and open the forecast" (<dashboard>#trail=..., the Trail Forecast tab).
// Nothing is sent anywhere but the dashboard.

const DEFAULT_URL = "https://aaronmaddenjk.github.io/oregon-weather-dashboard/";
// your trails (private repo, owner 2026-10-08): read for the ✓ marks with the key from the options page
const TRAILS_API = "https://api.github.com/repos/aaronmaddenjk/wx-trails/contents/trails.json?ref=main";
const SITES = [
  { re: /^https:\/\/(www\.)?alltrails\.com\//, func: extractRoute },
  { re: /^https:\/\/(webmap|backcountry)\.onxmaps\.com\//, func: extractOnx },
  { re: /^https:\/\/(www\.)?trailforks\.com\//, func: extractTrailforks },
];
const TITLE = "Save this trail to your trails (right-click: save and open the forecast)";

chrome.runtime.onInstalled.addListener(() => {
  chrome.contextMenus.create({ id: "forecast", title: "Save and open the forecast", contexts: ["action"] });
  chrome.contextMenus.create({ id: "all", title: "Save every trail tab in this window (Alt+Shift+A)", contexts: ["action"] });
  chrome.contextMenus.create({ id: "list", title: "Open your trail list", contexts: ["action"] });
});
chrome.commands.onCommand.addListener((cmd, tab) => { if (cmd === "save-all") saveAll(tab); });
chrome.contextMenus.onClicked.addListener((info, tab) => {
  if (info.menuItemId === "forecast") forecast(tab);
  if (info.menuItemId === "all") saveAll(tab);
  if (info.menuItemId === "list") chrome.tabs.create({ url: chrome.runtime.getURL("list.html") });
});
// the trail list page (list.html) asks for a fresh look at trails.json for its Saved marks
chrome.runtime.onMessage.addListener((msg, sender, reply) => {
  if (msg && msg.type === "refreshSaved") {
    chrome.storage.local.set({ cloudAt: 0 }).then(savedLinks).then(() => reply(true), () => reply(false));
    return true;
  }
  if (msg && msg.type === "saveTabs" && Array.isArray(msg.items)) { saveTabs(msg.items); reply(true); }
});
chrome.action.onClicked.addListener((tab) => save(tab));

async function readRoute(tab) {
  const site = SITES.find((s) => s.re.test(tab.url || ""));
  if (!site) throw new Error("open a trail on alltrails.com or trailforks.com, or a route/track in the onX web map");
  const [{ result }] = await chrome.scripting.executeScript({ target: { tabId: tab.id }, func: site.func });
  if (!result || result.error) throw new Error((result && result.error) || "No route found on this page");
  return result;
}
async function dashboard() {
  const { dashboardUrl } = await chrome.storage.sync.get({ dashboardUrl: DEFAULT_URL });
  return dashboardUrl.replace(/#.*$/, "");
}

// right-click: the old behaviour, the forecast in a new tab (which also saves the trail)
async function forecast(tab) {
  badge(tab.id, "…", "Reading the trail…");
  try {
    const route = await readRoute(tab);
    await chrome.tabs.create({ url: (await dashboard()) + "#trail=" + encodeURIComponent(JSON.stringify(route)), index: tab.index + 1 });
    await markSaved(route.u);
    badge(tab.id, "✓", "In your trails · " + TITLE, "#2F7A45");
  } catch (e) {
    badge(tab.id, "!", "Couldn't read this trail: " + e.message);
  }
}

// ---------- save: one at a time, in a background dashboard tab ----------
const queue = [];
let busy = false;
// opts.close: close the trail's tab once it's saved (the trail list's batches); opts.from: the tab's url as
// the list knows it, echoed back in its "saveResult" message
async function save(tab, opts = {}) {
  badge(tab.id, "…", "Reading the trail…");
  let route;
  try { route = await readRoute(tab); }
  catch (e) { report(opts.from || tab.url, false, "couldn't read the trail: " + e.message); return badge(tab.id, "!", "Couldn't read this trail: " + e.message); }
  queue.push({ tab, route, close: !!opts.close, from: opts.from || tab.url });
  badge(tab.id, "…", busy ? "Waiting to save (" + queue.length + " in line)…" : "Saving…");
  if (!busy) next();
}
// ---------- save every trail tab in this window (tabs you opened; already-saved ones skipped) ----------
const TRAIL_PAGE = [
  /^https:\/\/(www\.)?alltrails\.com\/(explore\/)?trail\//,
  /^https:\/\/(www\.)?trailforks\.com\/(trails|route)\/[^/]+\/?(\?|#|$)/,
  /^https:\/\/(webmap|backcountry)\.onxmaps\.com\/(.*\/)?map\/([a-z]+-route|route|line)\//,
];
let sweeping = false;
async function saveAll(fromTab) {
  if (sweeping) return;
  sweeping = true;
  try {
    const win = fromTab && fromTab.windowId != null ? { windowId: fromTab.windowId } : { currentWindow: true };
    const tabs = (await chrome.tabs.query(win)).filter((t) => TRAIL_PAGE.some((re) => re.test(t.url || "")));
    const saved = await savedLinks();
    const todo = tabs.filter((t) => !saved[linkKey(t.url)]);
    tabs.filter((t) => saved[linkKey(t.url)]).forEach((t) => badge(t.id, "✓", "Already in your trails · " + TITLE, "#2F7A45"));
    if (fromTab) badge(fromTab.id, todo.length ? "…" : (saved[linkKey(fromTab.url || "")] ? "✓" : ""),
      todo.length ? "Saving " + todo.length + " trail tab" + (todo.length > 1 ? "s" : "") + " in this window…"
        : tabs.length ? "Every trail tab in this window is already saved" : "No trail tabs open in this window", "#2F7A45");
    for (const t of todo) {
      const tab = await loaded(t);
      if (!tab) { badge(t.id, "!", "This tab isn't loaded yet: open it once, then press Alt+Shift+A again"); continue; }
      await save(tab);   // reads it now, queues the save
      await new Promise((r) => setTimeout(r, 400));
    }
  } finally { sweeping = false; }
}
// a tab Chrome put to sleep (Memory Saver) or still loading: wake it / wait for it, up to 30 s
async function loaded(t) {
  if (t.discarded) { try { await chrome.tabs.reload(t.id); } catch (e) { return null; } }
  for (let i = 0; i < 60; i++) {
    const cur = await chrome.tabs.get(t.id).catch(() => null);
    if (!cur) return null;
    if (cur.status === "complete" && !cur.discarded) return cur;
    await new Promise((r) => setTimeout(r, 500));
  }
  return null;
}

async function next() {
  const job = queue.shift();
  if (!job) { busy = false; return; }
  busy = true;
  badge(job.tab.id, "…", "Saving…");
  let res;
  try { res = await saveInBackground(job.route); } catch (e) { res = { ok: false, error: e.message }; }
  if (res.ok) {
    await markSaved(job.route.u);
    badge(job.tab.id, "✓", "Saved “" + res.name + "” to your trails"
      + (res.cloud ? "" : " in the dashboard's browser only" + (res.error ? " (GitHub: " + res.error + ")" : " (connect GitHub in Map → Trails)")), "#2F7A45");
    const { closeAfterSave } = await chrome.storage.sync.get({ closeAfterSave: false });
    if (closeAfterSave || job.close) chrome.tabs.remove(job.tab.id).catch(() => {});
  } else {
    badge(job.tab.id, "!", "Couldn't save this trail: " + res.error);   // the tab stays open to look at
  }
  report(job.from, !!res.ok, res.ok ? "" : res.error);
  next();
}
// tell the trail list page (if open) how a trail went
function report(url, ok, error) {
  chrome.runtime.sendMessage({ type: "saveResult", url, ok, error: error || "" }).catch(() => {});
}
// the trail list's "Open and save next 20": it opened the tabs; save each, closing it when saved
async function saveTabs(items) {
  for (const it of items) {
    const t = await chrome.tabs.get(it.tabId).catch(() => null);
    const tab = t && await loaded(t);
    if (!tab) { report(it.url, false, "the tab didn't finish loading"); continue; }
    if (!TRAIL_PAGE.some((re) => re.test(tab.url || ""))) { report(it.url, false, "that link didn't open a trail page"); continue; }
    await save(tab, { close: true, from: it.url });
    await new Promise((r) => setTimeout(r, 400));
  }
}
// -> the dashboard's answer {ok, name, cloud, error}
async function saveInBackground(route) {
  const url = (await dashboard()) + "#save=" + encodeURIComponent(JSON.stringify(route));
  const t = await chrome.tabs.create({ url, active: false });
  return new Promise((resolve) => {
    const done = (r) => {
      clearTimeout(timer);
      chrome.tabs.onUpdated.removeListener(watch);
      chrome.tabs.onRemoved.removeListener(gone);
      chrome.tabs.remove(t.id).catch(() => {});
      resolve(r);
    };
    const watch = (id, info) => {
      if (id !== t.id || !info.url) return;
      const m = /#saved=(.*)$/.exec(info.url);
      if (!m) return;
      try { done(JSON.parse(decodeURIComponent(m[1]))); } catch (e) { done({ ok: false, error: "the dashboard's answer didn't make sense" }); }
    };
    const gone = (id) => { if (id === t.id) done({ ok: false, error: "the dashboard tab was closed before it finished" }); };
    chrome.tabs.onUpdated.addListener(watch);
    chrome.tabs.onRemoved.addListener(gone);
    const timer = setTimeout(() => done({ ok: false, error: "the dashboard took over 90 seconds" }), 90000);
  });
}

// ---------- ✓ on trails already in your trails ----------
// Links saved from this browser, plus your trails.json on GitHub (every device's saves), refreshed
// at most every 10 minutes.
function linkKey(u) {
  try { const x = new URL(u); return x.hostname.replace(/^www\./, "") + x.pathname.replace(/^\/explore\//, "/").replace(/\/$/, ""); }
  catch (e) { return ""; }
}
async function savedLinks() {
  const { saved = {}, cloudAt = 0 } = await chrome.storage.local.get(["saved", "cloudAt"]);
  if (Date.now() - cloudAt > 10 * 60 * 1000) {
    try {   // your trails are in a private repo: read with the GitHub key from the options page, if given
      const { ghToken = "" } = await chrome.storage.local.get("ghToken");
      const r = ghToken ? await fetch(TRAILS_API, { cache: "no-store",
        headers: { Accept: "application/vnd.github.raw+json", Authorization: "Bearer " + ghToken } }) : { ok: false };
      if (r.ok) {
        (await r.json()).forEach((m) => { const k = linkKey(m.link || ""); if (k) saved[k] = saved[k] || 1; });
        await chrome.storage.local.set({ saved, cloudAt: Date.now() });
      }
    } catch (e) { /* offline: this browser's list only */ }
  }
  return saved;
}
async function markSaved(u) {
  const k = linkKey(u || "");
  if (!k) return;
  const { saved = {} } = await chrome.storage.local.get("saved");
  saved[k] = Date.now();
  await chrome.storage.local.set({ saved });
}
chrome.tabs.onUpdated.addListener(async (id, info, tab) => {
  if (info.status !== "complete" || !SITES.some((s) => s.re.test(tab.url || ""))) return;
  const k = linkKey(tab.url);
  if (k && (await savedLinks())[k]) badge(id, "✓", "Already in your trails · " + TITLE, "#2F7A45");
});

function badge(tabId, text, title, color) {
  chrome.action.setBadgeBackgroundColor({ tabId, color: color || "#FE5000" });
  chrome.action.setBadgeText({ tabId, text });
  if (title) chrome.action.setTitle({ tabId, title });
}

// Runs inside the onX Backcountry web map. Three kinds of page:
//   /map/hike-route/<id> (also bike-, ski-/snow- routes): onX's trail guides, from its GraphQL
//     "supergraph" (routesConnection → geometry, GeoJSON)
//   /map/route/<id>: a route you built and saved (/v1/routing/routes, geometry = polyline)
//   /map/line/<uuid>: a track you recorded, or a line you drew (/v1/markups/tracks or /lines)
// All use the page's own login (the OIDC token the web map keeps in localStorage) plus the app
// headers it sends.
async function extractOnx() {
  const API = "https://api.production.onxmaps.com/v1/";
  const m = /\/map\/([a-z]+-route|route|line)\/([^/?#]+)/.exec(location.pathname);
  if (!m) return { error: "open a trail, or one of your routes or tracks, then click again" };
  const key = Object.keys(localStorage).find((k) => k.startsWith("oidc.user:"));
  let token = null;
  try { token = key && JSON.parse(localStorage.getItem(key)).access_token; } catch (e) { /* not signed in */ }
  if (!token) return { error: "sign in to the onX web map first" };
  const headers = { Authorization: "Bearer " + token, "onx-application-id": "backcountry", "onx-application-platform": "web" };
  const get = async (path) => {
    const r = await fetch(API + path, { headers });
    if (!r.ok) throw new Error("onX answered " + r.status);
    return r.json();
  };
  const fromGeoJSON = (g, name) => {
    const lines = !g ? [] : g.type === "MultiLineString" ? g.coordinates : g.type === "LineString" ? [g.coordinates] : [];
    const pts = lines.flat().filter((c) => Array.isArray(c) && c.length >= 2);
    if (pts.length < 2) return { error: "this trail has no line to forecast" };
    return { n: name, u: location.origin + location.pathname, p: encode(pts) };
  };
  const [, kind, id] = m;

  if (kind.endsWith("-route")) {   // an onX trail guide
    const query = "query($id: ID!) { routesConnection(filter: { id: $id }, limit: 1) { edges { node { __typename"
      + " ... on HikeRoute { name geometry } ... on BikeRoute { name geometry } ... on SnowRoute { name geometry } } } } }";
    const r = await fetch(API + "supergraph/", {
      method: "POST", headers: { ...headers, "content-type": "application/json" },
      body: JSON.stringify({ query, variables: { id } }),
    });
    if (!r.ok) throw new Error("onX answered " + r.status);
    const d = await r.json();
    const edge = d.data && d.data.routesConnection && d.data.routesConnection.edges[0];
    if (!edge) return { error: "onX didn't return this trail" + (d.errors ? " (" + d.errors[0].message + ")" : "") };
    let g = edge.node.geometry;
    if (typeof g === "string") { try { g = JSON.parse(g); } catch (e) { g = null; } }
    const out = fromGeoJSON(g, edge.node.name || "onX trail");
    if (edge.node.__typename === "BikeRoute") out.a = "mtb";
    return out;
  }

  if (kind === "route") {
    const want = (x) => x.id === id;
    let found = null;
    for (let page = 1; page <= 20 && !found; page++) {
      const d = await get("routing/routes?excludeSteps=&page[size]=50&page[number]=" + page);
      const list = d.data || [];
      found = list.find(want);
      if (list.length < 50 || (d.page && d.page.total <= page * 50)) break;
    }
    if (!found || !found.route || !found.route.geometry) return { error: "couldn't find this route in your onX account" };
    // route.geometry is already an encoded polyline, precision 5 (same as AllTrails)
    return { n: found.name || "onX route", u: location.origin + location.pathname, p: found.route.geometry };
  }

  // a recorded track (or a drawn line): GeoJSON [lon, lat, ele]
  let item = null;
  for (const path of ["markups/tracks?limit=500", "markups/lines?limit=500"]) {
    const d = await get(path);
    item = (Array.isArray(d) ? d : d.data || []).find((x) => x.uuid === id);
    if (item) break;
  }
  const g = item && item.geo_json && item.geo_json.geometry;
  if (!g) return { error: "couldn't find this track in your onX account" };
  return fromGeoJSON(g, item.name || "onX track");

  function encode(coords) {   // Google encoded polyline, precision 5, from [lon, lat] pairs
    let out = "", pLat = 0, pLng = 0;
    const put = (v) => {
      v = v < 0 ? ~(v << 1) : v << 1;
      while (v >= 0x20) { out += String.fromCharCode((0x20 | (v & 0x1f)) + 63); v >>= 5; }
      out += String.fromCharCode(v + 63);
    };
    for (const [lng, lat] of coords) {
      const a = Math.round(lat * 1e5), b = Math.round(lng * 1e5);
      put(a - pLat); put(b - pLng);
      pLat = a; pLng = b;
    }
    return out;
  }
}

// Runs inside a Trailforks trail or route page (/trails/<slug>/ or /route/<slug>/). The page's map
// script draws the trail from GeoJSON it carries inline: geoJSON.push({... properties: {'type':
// 'trail' | 'route', 'name': ...}, geometry: {type: 'LineString', encodedpath: '<polyline>'}}) - the
// same Google encoded polyline, precision 5, as AllTrails. Trailforks is a mountain-bike site; its
// few hiking trails say "Hiking" in the page title.
function extractTrailforks() {
  if (!/^\/(trails|route)\/[^/]+\/?$/.test(location.pathname))
    return { error: "open a trail or route page on Trailforks (trailforks.com/trails/... or /route/...)" };
  const js = [...document.scripts].map((s) => s.textContent || "").find((t) => t.includes("encodedpath"));
  if (!js) return { error: "Trailforks didn't include the route on this page" };
  const unq = (v) => v.replace(/\\(.)/g, "$1");
  // the page's own trail or route: the feature whose type is 'trail' / 'route', else the first line
  const feats = [...js.matchAll(/'type':\s*'([a-z]+)'[\s\S]*?'name':\s*'((?:[^'\\]|\\.)*)'[\s\S]*?encodedpath:\s*'((?:[^'\\]|\\.)*)'/g)]
    .map((m) => ({ type: m[1], name: unq(m[2]), p: unq(m[3]) }));
  const f = feats.find((x) => x.type === "trail" || x.type === "route") || feats[0];
  if (!f || f.p.length < 4) return { error: "Trailforks didn't include the route on this page" };
  const title = document.title || "";
  // difficulty: the page's "Difficulty rating" (trails: "Blue", routes: "Black Diamond"), else the
  // first difficulty icon's title ("Intermediate / Blue Square", "Access Trail, Road or Doubletrack")
  const dt = (/Difficulty rating\s*\n\s*([^\n]+)/i.exec(document.body.innerText) || [])[1]
    || [...document.querySelectorAll('[class*="dicon"][title]')].map((e) => e.title)
         .find((x) => /green|blue|black|access|white|easiest|pro line/i.test(x)) || "";
  const d = /double black|pro line|orange/i.test(dt) ? "dblack" : /black/i.test(dt) ? "black" : /blue/i.test(dt) ? "blue"
    : /green|white|easiest|easy/i.test(dt) ? "green" : /access|road|doubletrack/i.test(dt) ? "access" : "";
  return { n: f.name || title.split(/ (Mountain Biking|Hiking)/)[0], u: location.origin + location.pathname,
           p: f.p, a: /Hiking (Trail|Route)/i.test(title) ? "hike" : "mtb", d };
}

// Runs inside the AllTrails page. AllTrails' map view embeds the route as an encoded polyline
// ("polyline": {"pointsData": "$6f"}, where $6f points at a text chunk of the page's Next.js
// data stream). If the current page doesn't carry it (the regular trail page only shows a
// static map), the trail's map view is fetched with the same session and read instead.
async function extractRoute() {
  function streamOf(scripts) {
    const parts = [];
    for (const sc of scripts) {
      const t = (sc.textContent || "").trim();
      if (!t.startsWith("self.__next_f.push(")) continue;
      try {
        const a = JSON.parse(t.slice("self.__next_f.push(".length).replace(/\)\s*;?\s*$/, ""));
        if (typeof a[1] === "string") parts.push(a[1]);
      } catch (e) { /* not a data chunk */ }
    }
    return parts.join("");
  }
  // Text chunks of the stream: "<id>:T<hex byte length>,<text>". A chunk can follow the
  // previous text chunk with no newline between them, so walk the rows by length.
  function textChunks(s) {
    const out = {};
    let p = 0;
    while (p < s.length) {
      const m = /^([0-9a-z]+):T([0-9a-f]+),/.exec(s.slice(p, p + 40));
      if (m) {
        let bytes = parseInt(m[2], 16), q = p + m[0].length;
        while (bytes > 0 && q < s.length) {
          const c = s.codePointAt(q);
          bytes -= c < 0x80 ? 1 : c < 0x800 ? 2 : c < 0x10000 ? 3 : 4;
          q += c >= 0x10000 ? 2 : 1;
        }
        out[m[1]] = s.slice(p + m[0].length, q);
        p = q;
      } else {
        const nl = s.indexOf("\n", p);
        if (nl < 0) break;
        p = nl + 1;
      }
    }
    return out;
  }
  function validPolyline(str) {
    if (!str || str.length < 4 || /[^\x3f-\x7e]/.test(str)) return false;
    let i = 0, lat = 0, lng = 0, n = 0;
    while (i < str.length) {
      for (let k = 0; k < 2; k++) {
        let b, shift = 0, v = 0;
        do { if (i >= str.length) return false; b = str.charCodeAt(i++) - 63; v |= (b & 31) << shift; shift += 5; } while (b >= 32);
        const d = v & 1 ? ~(v >> 1) : v >> 1;
        if (k) lng += d; else lat += d;
      }
      if (Math.abs(lat) > 9e6 || Math.abs(lng) > 18e6) return false;
      n++;
    }
    return n >= 2;
  }
  function polylineIn(s) {
    let chunks = null;
    for (const m of s.matchAll(/"pointsData":"([^"]*)"/g)) {
      if (!m[1]) continue;
      let poly;
      if (m[1][0] !== "$") poly = JSON.parse('"' + m[1] + '"');   // inline (JSON-escaped)
      else {
        const id = m[1].slice(1);
        chunks = chunks || textChunks(s);
        poly = chunks[id];
        if (!validPolyline(poly)) {   // last resort: find the chunk header anywhere
          const h = new RegExp("(?:^|[^0-9a-z])" + id + ":T([0-9a-f]+),").exec(s);
          if (h) { const st = h.index + h[0].length; poly = s.slice(st, st + parseInt(h[1], 16)); }
        }
      }
      if (validPolyline(poly)) return poly;
    }
    return null;
  }
  function trailName(doc) {
    const h1 = doc.querySelector("h1");
    if (h1 && h1.textContent.trim()) return h1.textContent.trim();
    return (doc.title || "").split(" | ")[0].replace(/,\s*[^,]+ - \d[\d,]* Reviews.*$/, "").replace(/^Explore\s+/, "").trim();
  }

  let poly = polylineIn(streamOf(document.scripts));
  let name = trailName(document);
  if (!poly) {
    const path = location.pathname;
    if (!/\/trail\//.test(path)) return { error: "open a trail page (alltrails.com/trail/...)" };
    const url = path.startsWith("/explore/") ? path : "/explore" + path;
    const html = await (await fetch(url, { credentials: "include" })).text();
    const doc = new DOMParser().parseFromString(html, "text/html");
    poly = polylineIn(streamOf(doc.scripts));
    name = trailName(doc) || name;
    if (!poly) {   // the map view's raw data stream (what Next.js loads on in-app navigation)
      const rsc = await (await fetch(url, { credentials: "include", headers: { RSC: "1" } })).text();
      poly = polylineIn(rsc);
    }
  }
  if (!poly) return { error: "AllTrails didn't include the route on this page" };
  return { n: name, u: location.origin + location.pathname.replace(/^\/explore\//, "/"), p: poly };
}
