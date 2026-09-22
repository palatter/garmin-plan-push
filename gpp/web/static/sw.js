/* Service worker: makes the local app installable (#162) and keeps the
   static assets available while the page is open. API calls are never
   cached -- every plan number must come from the server. */

'use strict';

const CACHE = 'gpp-static-v1';
const ASSETS = [
  '/static/style.css',
  '/static/review.css',
  '/static/app.js',
  '/static/review.js',
  '/static/icon.svg',
  '/static/manifest.webmanifest',
];

self.addEventListener('install', event => {
  event.waitUntil(caches.open(CACHE).then(cache => cache.addAll(ASSETS)).then(() => self.skipWaiting()));
});

self.addEventListener('activate', event => {
  event.waitUntil(
    caches.keys().then(keys => Promise.all(keys.filter(k => k !== CACHE).map(k => caches.delete(k))))
      .then(() => self.clients.claim()),
  );
});

self.addEventListener('fetch', event => {
  const url = new URL(event.request.url);
  if (event.request.method !== 'GET' || url.pathname.startsWith('/api/')) return;
  if (!url.pathname.startsWith('/static/')) return;  // the page itself carries a per-run token
  event.respondWith(
    fetch(event.request)
      .then(response => {
        const copy = response.clone();
        caches.open(CACHE).then(cache => cache.put(event.request, copy));
        return response;
      })
      .catch(() => caches.match(event.request)),
  );
});
