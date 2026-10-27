"""把 glassdata 构建成可部署的静态站点（含 PWA 能力）。

产物目录 web/：
  index.html            空状态首页（可拖入 / 点击示例）
  demo.csv              示例数据，供首页「示例数据」按钮读取
  xlsx.js               网页版 Excel 解析（纯前端解压 + 扫描 XML）
  clean.js              网页版清洗引擎，与 glassdata/clean.py 逐格对齐
  manifest.webmanifest  PWA 清单，决定安装后的名称、图标、显示模式
  sw.js                 Service Worker，缓存应用外壳以便离线使用
  icon-192/512/180.png  图标（纯标准库生成，不依赖 Pillow）
  .nojekyll             空文件，告诉 GitHub Pages 别跑 Jekyll，原样发布

用法：python tools/build_site.py
"""
from __future__ import annotations

import hashlib
import math
import os
import shutil
import struct
import sys
import zlib
from typing import List, Tuple

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from glassdata.render import build_html  # noqa: E402
from glassdata.sample import write_demo_csv  # noqa: E402

WEB = os.path.join(ROOT, "web")

# 参与版本号计算的源码：模板（样式与前端逻辑）、引擎、示例数据生成器、本脚本。
SOURCE_FILES = (
    os.path.join(ROOT, "glassdata", "template.html"),
    os.path.join(ROOT, "glassdata", "core.py"),
    os.path.join(ROOT, "glassdata", "render.py"),
    os.path.join(ROOT, "glassdata", "sample.py"),
    os.path.join(ROOT, "glassdata", "xlsx.js"),
    os.path.join(ROOT, "glassdata", "clean.js"),
    os.path.abspath(__file__),
)

# --------------------------------------------------------------------------- #
# PNG 图标（纯标准库）：用有符号距离场做抗锯齿，避免额外依赖
# --------------------------------------------------------------------------- #
Color = Tuple[float, float, float]


def _sd_round_rect(px: float, py: float, cx: float, cy: float,
                   hw: float, hh: float, r: float) -> float:
    qx = abs(px - cx) - (hw - r)
    qy = abs(py - cy) - (hh - r)
    ax, ay = max(qx, 0.0), max(qy, 0.0)
    return math.hypot(ax, ay) + min(max(qx, qy), 0.0) - r


def _mix(a: Color, b: Color, t: float) -> Color:
    return (a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t, a[2] + (b[2] - a[2]) * t)


def _lerp_hex(start: str, end: str, t: float) -> Color:
    def unpack(value: str) -> Color:
        value = value.lstrip("#")
        return (int(value[0:2], 16), int(value[2:4], 16), int(value[4:6], 16))
    return _mix(unpack(start), unpack(end), t)


def render_icon(size: int) -> bytearray:
    """深色底 + 玻璃框 + 四根上升柱，与应用内的视觉语言保持一致。"""
    pixels = bytearray(size * size * 4)
    s = float(size)
    bars = (
        (0.285, 0.59, 0.085, 0.14, 0.0),
        (0.400, 0.52, 0.085, 0.21, 0.34),
        (0.515, 0.46, 0.085, 0.27, 0.67),
        (0.630, 0.39, 0.085, 0.34, 1.0),
    )
    for y in range(size):
        py = (y + 0.5) / s
        row = y * size * 4
        for x in range(size):
            px = (x + 0.5) / s
            # 底色：自左上紫色微光向深海军蓝过渡
            base = _mix((33, 26, 77), (5, 6, 15), min(1.0, (px * 0.35 + py * 0.9)))
            glow = max(0.0, 1.0 - math.hypot(px - 0.16, py - 0.1) * 2.1) * 0.55
            base = _mix(base, (109, 59, 255), glow)

            rgb = base
            alpha = max(0.0, min(1.0, 0.5 - _sd_round_rect(px, py, 0.5, 0.5, 0.5, 0.5, 0.225) * s))

            # 玻璃底板
            plate = max(0.0, min(1.0, 0.5 - _sd_round_rect(px, py, 0.5, 0.5, 0.425, 0.425, 0.16) * s))
            if plate > 0:
                rgb = _mix(rgb, (255, 255, 255), plate * 0.075)

            # 边框亮线
            outer = _sd_round_rect(px, py, 0.5, 0.5, 0.425, 0.425, 0.16)
            ring = max(0.0, 1.0 - abs(outer + 0.004) * s * 0.9)
            if ring > 0:
                rgb = _mix(rgb, (255, 255, 255), ring * 0.34)

            # 数据柱
            for bx, by, bw, bh, t in bars:
                inside = max(0.0, min(1.0, 0.5 - _sd_round_rect(
                    px, py, bx + bw / 2, by + bh / 2, bw / 2, bh / 2, 0.018) * s))
                if inside > 0:
                    rgb = _mix(rgb, _lerp_hex("#8b5cf6", "#22d3ee", t), inside)

            offset = row + x * 4
            pixels[offset] = int(max(0, min(255, rgb[0])))
            pixels[offset + 1] = int(max(0, min(255, rgb[1])))
            pixels[offset + 2] = int(max(0, min(255, rgb[2])))
            pixels[offset + 3] = int(max(0, min(255, alpha * 255)))
    return pixels


def write_png(path: str, size: int) -> None:
    pixels = render_icon(size)
    stride = size * 4
    raw = bytearray()
    for y in range(size):
        raw.append(0)
        raw.extend(pixels[y * stride:(y + 1) * stride])

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data))
            + tag
            + data
            + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
        )

    header = struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0)
    with open(path, "wb") as handle:
        handle.write(b"\x89PNG\r\n\x1a\n")
        handle.write(chunk(b"IHDR", header))
        handle.write(chunk(b"IDAT", zlib.compress(bytes(raw), 9)))
        handle.write(chunk(b"IEND", b""))


# --------------------------------------------------------------------------- #
# 站点文件
# --------------------------------------------------------------------------- #
HEAD_EXTRA = """<link rel="manifest" href="manifest.webmanifest" />
<meta name="theme-color" content="#04050a" />
<meta name="description" content="拖入 CSV / TSV / JSON，即可在浏览器本地完成字段画像、分布与相关性分析，数据不上传。" />
<meta name="apple-mobile-web-app-capable" content="yes" />
<meta name="apple-mobile-web-app-title" content="数据分析" />
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent" />
<link rel="apple-touch-icon" href="icon-180.png" />
<script src="xlsx.js"></script>
<script src="clean.js"></script>
<script>
window.__BUILD__ = {"v": "__VERSION__"};
(function () {
  if (!("serviceWorker" in navigator) || location.protocol.indexOf("http") !== 0) return;
  var PAGE_VERSION = window.__BUILD__.v;
  /* 页脚显示的版本号是「这一屏 HTML 是哪一版」。如果新装的 Service Worker
     版本比它新，说明拿到的是旧 HTML（CDN 或浏览器缓存），自动刷一次即可自愈。
     用 window.name 记住目标版本，保证同一个版本最多只刷一次，不会进加载环。 */
  function healTo(version) {
    if (!version || version === PAGE_VERSION) return;
    if (window.name === "swheal:" + version) return;
    window.name = "swheal:" + version;
    location.reload();
  }
  navigator.serviceWorker.addEventListener("controllerchange", function () {
    var worker = navigator.serviceWorker.controller;
    if (!worker) return;
    var channel = new MessageChannel();
    channel.port1.onmessage = function (event) {
      healTo(event.data && event.data.version);
    };
    worker.postMessage({ type: "version" }, [channel.port2]);
  });
  window.addEventListener("load", function () {
    /* updateViaCache: "none" —— 不让 HTTP 缓存替我们决定「有没有新版本」，
       否则 sw.js 一旦被缓存住，老用户就永远停在旧界面。 */
    navigator.serviceWorker
      .register("sw.js", { updateViaCache: "none" })
      .then(function (reg) { return reg.update(); })
      .catch(function () {});
  });
})();
</script>"""

MANIFEST = """{
  "name": "数据分析工具",
  "short_name": "数据分析",
  "description": "在浏览器本地完成表格数据分析与可视化，数据不上传。",
  "start_url": "./",
  "scope": "./",
  "display": "standalone",
  "orientation": "any",
  "background_color": "#04050a",
  "theme_color": "#04050a",
  "lang": "zh-CN",
  "icons": [
    { "src": "icon-192.png", "sizes": "192x192", "type": "image/png", "purpose": "any" },
    { "src": "icon-512.png", "sizes": "512x512", "type": "image/png", "purpose": "any" },
    { "src": "icon-512.png", "sizes": "512x512", "type": "image/png", "purpose": "maskable" }
  ]
}
"""

# 缓存名里带内容哈希：只要 index.html 或示例数据变了，sw.js 的字节就变，
# 浏览器才会重新安装 Service Worker。缓存名写死会导致老用户永远停在旧版。
# 文档请求走「网络优先」——联网时总是拿最新版，断网才回落缓存；
# 静态外壳走「缓存优先」，保证离线可打开。
SERVICE_WORKER = """const VERSION = "__CACHE_VERSION__";
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
"""


def cache_version(demo_bytes: bytes) -> str:
    """用「源码 + 示例数据」算版本号：内容一变，sw.js 就变，用户才会拿到新版。

    刻意不拿最终的 index.html 参与计算——版本号本身要写进 index.html，
    否则就成了循环依赖。源码里的任何一行改动都会让版本号变。
    """
    digest = hashlib.sha1()
    for path in SOURCE_FILES:
        with open(path, "rb") as handle:
            digest.update(handle.read())
    digest.update(demo_bytes)
    return digest.hexdigest()[:10]


def main() -> int:
    os.makedirs(WEB, exist_ok=True)

    demo_source = os.path.join(ROOT, "examples", "demo_orders.csv")
    if not os.path.exists(demo_source):
        write_demo_csv(demo_source)
    shutil.copyfile(demo_source, os.path.join(WEB, "demo.csv"))

    shutil.copyfile(
        os.path.join(ROOT, "glassdata", "xlsx.js"), os.path.join(WEB, "xlsx.js")
    )
    shutil.copyfile(
        os.path.join(ROOT, "glassdata", "clean.js"), os.path.join(WEB, "clean.js")
    )

    with open(os.path.join(WEB, "demo.csv"), "rb") as handle:
        demo_bytes = handle.read()
    version = cache_version(demo_bytes)

    head_extra = HEAD_EXTRA.replace("__VERSION__", version)
    assert "__VERSION__" not in head_extra, "版本占位符未替换"

    payload = {"columns": [], "meta": {"title": "数据分析工具"}}
    markup = build_html(payload, title="数据分析工具", head_extra=head_extra)
    with open(os.path.join(WEB, "index.html"), "w", encoding="utf-8", newline="\n") as handle:
        handle.write(markup)

    worker = SERVICE_WORKER.replace("__CACHE_VERSION__", version)
    assert "__CACHE_VERSION__" not in worker, "缓存版本占位符未替换"

    with open(os.path.join(WEB, "manifest.webmanifest"), "w", encoding="utf-8", newline="\n") as handle:
        handle.write(MANIFEST)
    with open(os.path.join(WEB, "sw.js"), "w", encoding="utf-8", newline="\n") as handle:
        handle.write(worker)

    for size in (192, 512, 180):
        write_png(os.path.join(WEB, f"icon-{size}.png"), size)

    # GitHub Pages 默认会走 Jekyll 处理，放一个空 .nojekyll 让它原样发布静态文件
    with open(os.path.join(WEB, ".nojekyll"), "w", encoding="utf-8"):
        pass

    total = sum(
        os.path.getsize(os.path.join(WEB, name)) for name in sorted(os.listdir(WEB))
    )
    print("站点已构建到 web/")
    for name in sorted(os.listdir(WEB)):
        path = os.path.join(WEB, name)
        print(f"  {name:<24}{os.path.getsize(path) / 1024:>8.1f} KB")
    print(f"  {'合计':<22}{total / 1024:>8.1f} KB")
    print(f"缓存版本：glassdata-{version}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
