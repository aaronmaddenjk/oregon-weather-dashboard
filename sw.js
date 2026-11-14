// Oregon Weather as an installed app: keeps the last good copy of the dashboard so it opens with no
// signal (a trailhead) and shows that copy until the network answers. The forecast changes every hour,
// so this site's own files are NETWORK FIRST - the saved copy is only used when the network fails.
// version.json (the "newer build?" check) never comes from the cache. Other sites (maps, radar, the
// forecast APIs) go straight to the network, except Mapbox's versioned library, kept so the page's
// scripts still load offline.
const CACHE = 'wx-v1';

self.addEventListener('install', () => self.skipWaiting());
self.addEventListener('activate', (e) => e.waitUntil(self.clients.claim()));

function keep(req, res) {
  if (res && (res.ok || res.type === 'opaque')) {
    const copy = res.clone();
    caches.open(CACHE).then((c) => c.put(req, copy));
  }
  return res;
}

self.addEventListener('fetch', (e) => {
  const req = e.request;
  if (req.method !== 'GET') return;
  const u = new URL(req.url);
  if (u.hostname === 'api.mapbox.com' && u.pathname.startsWith('/mapbox-gl-js/')) {   // versioned: cache first
    e.respondWith(caches.match(req).then((hit) => hit || fetch(req).then((res) => keep(req, res))));
    return;
  }
  if (u.origin !== self.location.origin || u.pathname.endsWith('/version.json')) return;
  e.respondWith(
    fetch(req).then((res) => keep(req, res)).catch(() =>
      caches.match(req, { ignoreSearch: true }).then((hit) =>
        hit || (req.mode === 'navigate' ? caches.match(self.registration.scope) : Response.error())))
  );
});
