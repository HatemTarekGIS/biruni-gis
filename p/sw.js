/* Old service worker left behind on biruni-gis.com/p/: drops its cache and unregisters itself. */
self.addEventListener('install',()=>self.skipWaiting());
self.addEventListener('activate',e=>e.waitUntil(
 caches.keys().then(ks=>Promise.all(ks.filter(k=>k.startsWith('biruni-p-')).map(k=>caches.delete(k))))
  .then(()=>self.registration.unregister())
));
