// Service worker for Smart Power Dashboard PWA
const CACHE_NAME = 'smart-power-v1';

self.addEventListener('install', (event) => {
  self.skipWaiting();
});

self.addEventListener('activate', (event) => {
  event.waitUntil(self.clients.claim());
});

self.addEventListener('fetch', (event) => {
  // Network-first strategy for real-time dashboard data & HTML
  event.respondWith(
    fetch(event.request).catch(() => caches.match(event.request))
  );
});
