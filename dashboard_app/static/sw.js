// Service Worker Básico para PWA
self.addEventListener('install', (event) => {
    console.log('Service Worker: Instalado');
});

self.addEventListener('fetch', (event) => {
    // No hacemos caché, solo pasamos la petición a la red
    event.respondWith(fetch(event.request));
});