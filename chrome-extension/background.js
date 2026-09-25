// Trail Forecast for AllTrails and onX
// Click the button on an AllTrails trail page, or on one of your routes or tracks in the onX
// Backcountry web map: the route is read (in your own, logged-in session) and handed to the
// dashboard's Trail Forecast tab in the URL:
//   <dashboard>#trail={"n": name, "u": link back, "p": encoded polyline}
// Nothing is sent anywhere else; the dashboard computes the forecast in the browser.

const DEFAULT_URL = "http://localhost:8000/";
const SITES = [
  { re: /^https:\/\/(www\.)?alltrails\.com\//, func: extractRoute },
  { re: /^https:\/\/(webmap|backcountry)\.onxmaps\.com\//, func: extractOnx },
];

chrome.action.onClicked.addListener(async (tab) => {
  const site = SITES.find((s) => s.re.test(tab.url || ""));
  if (!site) {
    return badge(tab.id, "?", "Open a trail on alltrails.com, or a route/track in the onX web map, then click again");
  }
  badge(tab.id, "…", "Reading the trail…");
  try {
    const [{ result }] = await chrome.scripting.executeScript({ target: { tabId: tab.id }, func: site.func });
    if (!result || result.error) throw new Error((result && result.error) || "No route found on this page");
    const { dashboardUrl } = await chrome.storage.sync.get({ dashboardUrl: DEFAULT_URL });
    const url = dashboardUrl.replace(/#.*$/, "") + "#trail=" + encodeURIComponent(JSON.stringify(result));
    await chrome.tabs.create({ url, index: tab.index + 1 });
    badge(tab.id, "", "Forecast this trail and save it to your trails");
  } catch (e) {
    badge(tab.id, "!", "Couldn't read this trail: " + e.message);
  }
});

function badge(tabId, text, title) {
  chrome.action.setBadgeBackgroundColor({ tabId, color: "#FE5000" });
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
    return fromGeoJSON(g, edge.node.name || "onX trail");
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
