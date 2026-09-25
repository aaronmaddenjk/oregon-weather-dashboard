// Trail Forecast for AllTrails
// Click the button on an AllTrails trail page: the route that page has loaded (in your own,
// logged-in session) is read and handed to the dashboard's Trail Forecast tab in the URL:
//   <dashboard>#trail={"n": name, "u": AllTrails link, "p": encoded polyline}
// Nothing is sent anywhere else; the dashboard computes the forecast in the browser.

const DEFAULT_URL = "http://localhost:8000/";

chrome.action.onClicked.addListener(async (tab) => {
  if (!/^https:\/\/(www\.)?alltrails\.com\//.test(tab.url || "")) {
    return badge(tab.id, "?", "Open a trail on alltrails.com, then click again");
  }
  badge(tab.id, "…", "Reading the trail…");
  try {
    const [{ result }] = await chrome.scripting.executeScript({ target: { tabId: tab.id }, func: extractRoute });
    if (!result || result.error) throw new Error((result && result.error) || "No route found on this page");
    const { dashboardUrl } = await chrome.storage.sync.get({ dashboardUrl: DEFAULT_URL });
    const url = dashboardUrl.replace(/#.*$/, "") + "#trail=" + encodeURIComponent(JSON.stringify(result));
    await chrome.tabs.create({ url, index: tab.index + 1 });
    badge(tab.id, "", "Open this trail's forecast");
  } catch (e) {
    badge(tab.id, "!", "Couldn't read this trail: " + e.message);
  }
});

function badge(tabId, text, title) {
  chrome.action.setBadgeBackgroundColor({ tabId, color: "#FE5000" });
  chrome.action.setBadgeText({ tabId, text });
  if (title) chrome.action.setTitle({ tabId, title });
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
