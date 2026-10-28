const CACHE='najeeb-client-pilot-v2';
const SHELL=['/','/manifest.webmanifest','/static/najeeb-brand.svg','/static/machine.svg','/static/hero-machines.svg','/static/icon-192.png','/static/icon-512.png'];
self.addEventListener('install',e=>e.waitUntil(caches.open(CACHE).then(c=>c.addAll(SHELL)).then(()=>self.skipWaiting())));
self.addEventListener('activate',e=>e.waitUntil(caches.keys().then(keys=>Promise.all(keys.filter(k=>k!==CACHE && k.startsWith('najeeb-client-')).map(k=>caches.delete(k)))).then(()=>self.clients.claim())));
self.addEventListener('fetch',e=>{const u=new URL(e.request.url); if(u.origin!==location.origin||e.request.method!=='GET'||u.pathname.startsWith('/api/')||u.pathname.startsWith('/media/'))return; e.respondWith(fetch(e.request).then(r=>{if(r.ok){const copy=r.clone();caches.open(CACHE).then(c=>c.put(e.request,copy));}return r;}).catch(()=>caches.match(e.request).then(r=>r||caches.match('/'))));});
