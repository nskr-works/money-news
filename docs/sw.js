// ホーム画面アプリ用。常にネットの最新を優先し、つながらないときだけ前回の内容を表示する。
const CACHE = "cashless-news-v1";

self.addEventListener("install", () => self.skipWaiting());
self.addEventListener("activate", e => {
  e.waitUntil(caches.keys().then(keys => Promise.all(keys.filter(k => k !== CACHE).map(k => caches.delete(k)))).then(() => self.clients.claim()));
});

self.addEventListener("fetch", e => {
  const url = new URL(e.request.url);
  // 自サイトの GET だけを扱う（外部の写真やフォントはブラウザに任せる）
  if (e.request.method !== "GET" || url.origin !== location.origin) return;
  e.respondWith(
    fetch(e.request).then(res => {
      if (res.ok) {
        const copy = res.clone();
        // data.json はキャッシュ避けの ?t= が付くので、クエリなしの URL で保存する
        const key = url.pathname.endsWith("data.json") ? url.origin + url.pathname : e.request;
        caches.open(CACHE).then(c => c.put(key, copy));
      }
      return res;
    }).catch(() => caches.match(url.pathname.endsWith("data.json") ? url.origin + url.pathname : e.request))
  );
});
