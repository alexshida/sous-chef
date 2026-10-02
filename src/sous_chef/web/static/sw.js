// Service worker: keep the shell available when the laptop is not.
//
// The server lives on a laptop that sleeps and changes networks, so the phone
// will regularly open this with nothing to talk to — in a grocery aisle, say. Caching the shell means it
// opens instantly and can say so, rather than showing a browser error page.
//
// Registration needs a secure context, so this runs on localhost and over
// HTTPS (see `tailscale serve` in the README). Over plain http on the tailnet
// it simply never registers, and the app works exactly as before.

// Bump this whenever the shell changes. The fetch handler is cache-first, so
// without a new name a refresh serves the old index.html and only the *next*
// open gets the revalidated one — a UI fix appears to not have shipped.
const VERSION = 'sous-chef-v2';
const SHELL = [
  '/',
  '/static/manifest.json',
  '/static/icons/icon-180.png',
  '/static/icons/icon-192.png',
];

self.addEventListener('install', event => {
  event.waitUntil(caches.open(VERSION).then(c => c.addAll(SHELL)).then(() => self.skipWaiting()));
});

self.addEventListener('activate', event => {
  event.waitUntil(
    caches.keys()
      .then(keys => Promise.all(keys.filter(k => k !== VERSION).map(k => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener('fetch', event => {
  const { request } = event;
  if (request.method !== 'GET') return;

  const url = new URL(request.url);
  if (url.origin !== self.location.origin) return;

  // Live data is never served stale — a cached plan or price would be
  // worse than an honest "can't reach the laptop".
  if (url.pathname.startsWith('/api/')) {
    event.respondWith(
      fetch(request).catch(() => new Response(
        JSON.stringify({ detail: 'offline', offline: true }),
        { status: 503, headers: { 'Content-Type': 'application/json' } }
      ))
    );
    return;
  }

  // Shell and assets: cache first, refreshed in the background.
  event.respondWith(
    caches.match(request).then(hit => {
      const live = fetch(request).then(res => {
        if (res && res.ok) {
          const copy = res.clone();
          caches.open(VERSION).then(c => c.put(request, copy));
        }
        return res;
      }).catch(() => hit);
      return hit || live;
    })
  );
});
