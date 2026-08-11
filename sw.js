// Helm service worker — caches the app shell so it loads even if the
// local server isn't running yet (e.g. right after a reboot, before
// systemd has started python -m http.server). Network is always
// preferred when available; this is just an offline fallback.

const CACHE_NAME = 'helm-shell-v3'; // bumped: v2 had accumulated every API poll response for the entire time this profile has been in use — this forces the existing activate-handler cleanup below to wipe it and start clean
const SHELL_FILES = [
  './',
  './index.html',
  './manifest.json',
  './icon-192.png',
  './icon-512.png',
];

self.addEventListener('install', (event) => {
  event.waitUntil(
    caches.open(CACHE_NAME).then((cache) => cache.addAll(SHELL_FILES))
  );
  self.skipWaiting();
});

self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys().then((names) =>
      Promise.all(names.filter((n) => n !== CACHE_NAME).map((n) => caches.delete(n)))
    )
  );
  self.clients.claim();
});

const SHELL_PATHNAMES = new Set(['/', '/index.html', '/manifest.json', '/icon-192.png', '/icon-512.png']);

self.addEventListener('fetch', (event) => {
  // Only cache the static app-shell files themselves. API endpoints
  // (/api/*) are dynamic, frequently-polled data — sysstats, services,
  // IRC alerts, and more, some firing every few seconds — that should
  // never be written to cache storage at all. Previously this handler
  // matched on origin alone and cached EVERY same-origin GET response
  // indiscriminately, including every one of those polls, for the entire
  // time this browser profile has been in use. Writing a disk-backed
  // cache entry on every single poll response adds real, compounding
  // overhead over a long session — this was never the intent (the
  // comment already said "everything else... goes straight to network,"
  // the code just didn't actually enforce it).
  const url = new URL(event.request.url);
  if (event.request.method !== 'GET' || url.origin !== self.location.origin) return;
  if (!SHELL_PATHNAMES.has(url.pathname)) return;

  event.respondWith(
    fetch(event.request)
      .then((resp) => {
        // Update the cache with the latest version in the background
        const clone = resp.clone();
        caches.open(CACHE_NAME).then((cache) => cache.put(event.request, clone));
        return resp;
      })
      .catch(() => caches.match(event.request))
  );
});
