const VERSION = "ae9c9e27a2";
const CACHE = "glassdata-" + VERSION;
/* 示例数据也带版本串：页面用 demo.csv?v=VERSION 请求，
   缓存键与版本绑定，就不可能再出现「新界面读到旧数据」这种混合态。 */
const SHELL = ["index.html", "xlsx.js", "clean.js", "manifest.webmanifest", "demo.csv?v=" + VERSION, "icon-192.png", "icon-512.png", "icon-180.png"];

self.addEventListener("install", (event) => {
  event.waitUntil(
    caches.open(CACHE).then((cache) => cache.addAll(SHELL)).then(() => self.skipWaiting())
  );
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(keys.filter((key) => key !== CACHE).map((key) => caches.delete(key)))
    ).then(() => self.clients.claim())
  );
});

/* 页面加载后握一次手：把自己的版本号报给页面，
   页面拿它和 HTML 里写死的版本比对，不一致就说明读到的是旧 HTML。 */
self.addEventListener("message", (event) => {
  const data = event.data || {};
  if (data.type !== "version") return;
  const reply = { type: "version", version: VERSION };
  const port = event.ports && event.ports[0];
  if (port) port.postMessage(reply);
  else if (event.source) event.source.postMessage(reply);
});

const OFFLINE = () =>
  new Response("离线且该资源未缓存，请联网后重试。", {
    status: 503,
    headers: { "Content-Type": "text/plain; charset=utf-8" },
  });

self.addEventListener("fetch", (event) => {
  const request = event.request;
  if (request.method !== "GET") return;

  if (request.mode === "navigate") {
    event.respondWith(
      fetch(request)
        .then((response) => {
          const copy = response.clone();
          caches.open(CACHE).then((cache) => cache.put("index.html", copy)).catch(() => {});
          return response;
        })
        .catch(() => caches.match("index.html").then((hit) => hit || OFFLINE()))
    );
    return;
  }

  event.respondWith(
    caches.match(request).then((hit) => {
      if (hit) return hit;
      return fetch(request).then((response) => {
        const copy = response.clone();
        caches.open(CACHE).then((cache) => cache.put(request, copy)).catch(() => {});
        return response;
      });
    })
  );
});
