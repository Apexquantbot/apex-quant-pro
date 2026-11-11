// Apex Quant Pro - service worker
// IMPORTANTE: cambia este nombre cada vez que subas una versión nueva de index.html
const CACHE = 'apex-pro-v1.1.2';
const BASICOS = ['./', './index.html', './manifest.json', './icon-192.png', './icon-512.png', './badge-96.png'];

self.addEventListener('install', e => {
  e.waitUntil(caches.open(CACHE).then(c => c.addAll(BASICOS)).then(() => self.skipWaiting()));
});

self.addEventListener('activate', e => {
  e.waitUntil(
    caches.keys()
      .then(ks => Promise.all(ks.filter(k => k !== CACHE).map(k => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener('fetch', e => {
  const url = new URL(e.request.url);
  if (e.request.method !== 'GET') return;
  // La IA (Gemini) nunca se guarda en caché
  if (url.hostname.includes('googleapis.com') && !url.hostname.includes('fonts')) return;
  const esDatos = url.pathname.endsWith('.json') || url.pathname.endsWith('/') || url.pathname.endsWith('.html');
  if (esDatos) {
    // Red primero (datos frescos); si no hay conexión, lo último guardado
    e.respondWith(
      fetch(e.request).then(r => {
        const copia = r.clone();
        caches.open(CACHE).then(c => c.put(e.request.url.split('?')[0], copia));
        return r;
      }).catch(() => caches.match(e.request.url.split('?')[0]))
    );
  } else {
    e.respondWith(
      caches.match(e.request).then(m => m || fetch(e.request).then(r => {
        if (r.ok || r.type === 'opaque') { const copia = r.clone(); caches.open(CACHE).then(c => c.put(e.request, copia)); }
        return r;
      }))
    );
  }
});

// ---------- Notificaciones ----------
self.addEventListener('push', e => {
  let d = {};
  try { d = e.data ? e.data.json() : {}; } catch (_) { d = { texto: e.data ? e.data.text() : '' }; }
  e.waitUntil(self.registration.showNotification(d.titulo || 'Apex Quant Pro', {
    body: d.texto || '',
    icon: 'icon-192.png',
    badge: 'badge-96.png',
    tag: d.tag || undefined,
    renotify: !!d.tag,
    data: { url: d.url || './' }
  }));
});

self.addEventListener('notificationclick', e => {
  e.notification.close();
  const url = new URL((e.notification.data && e.notification.data.url) || './', self.registration.scope);
  e.waitUntil(clients.matchAll({ type: 'window', includeUncontrolled: true }).then(cs => {
    for (const c of cs) {
      if (c.url.startsWith(self.registration.scope)) {
        c.postMessage({ s: url.searchParams.get('s'), t: url.searchParams.get('t'), v: url.searchParams.get('v') });
        return c.focus();
      }
    }
    return clients.openWindow(url.href);
  }));
});
