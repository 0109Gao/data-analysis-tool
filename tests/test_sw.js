/* Service Worker 行为自检：用桩对象真实执行 sw.js，验证每条分支。
 *
 * 为什么不靠浏览器：headless 下 Service Worker 的生命周期不可靠
 * （注册后不一定接管下一次导航），无法用它断言「联网必拿新版」。
 * 这里直接跑 sw.js 的代码，桩出 caches / fetch / Response，结论可复现。
 */
const fs = require("fs");
const path = require("path");
const vm = require("vm");

const SW_PATH = path.join(__dirname, "..", "web", "sw.js");
const code = fs.readFileSync(SW_PATH, "utf8");
const VERSION = (code.match(/const VERSION = "([0-9a-f]+)"/) || [])[1] || "";
// 页面按 demo.csv?v=<版本> 请求，缓存键也带版本串
const DEMO_KEY = "demo.csv?v=" + VERSION;

const failures = [];
function check(name, condition, detail) {
  if (condition) {
    console.log("  ok   " + name);
  } else {
    failures.push(name);
    console.log("  FAIL " + name + (detail ? " :: " + detail : ""));
  }
}

class FakeResponse {
  constructor(body, init) {
    init = init || {};
    this.body = body;
    this.status = init.status === undefined ? 200 : init.status;
    this.headers = init.headers || {};
  }
  clone() {
    return new FakeResponse(this.body, { status: this.status, headers: this.headers });
  }
}

function keyOf(target) {
  if (typeof target === "string") return target;
  return target.url;
}

/** 建一个沙箱：执行 sw.js，返回可操作的桩。 */
function harness(options) {
  options = options || {};
  const store = new Map(); // cacheName -> Map(key -> response)
  const calls = { fetch: [], addAll: [], deleted: [], skipWaiting: 0, claim: 0 };
  const listeners = {};

  const cacheFor = (name) => {
    if (!store.has(name)) store.set(name, new Map());
    const entries = store.get(name);
    return {
      addAll: (list) => {
        calls.addAll.push(...list);
        list.forEach((key) => entries.set(key, new FakeResponse("precached:" + key)));
        return Promise.resolve();
      },
      put: (target, response) => {
        entries.set(keyOf(target), response);
        return Promise.resolve();
      },
    };
  };

  const sandbox = {
    Response: FakeResponse,
    caches: {
      open: (name) => Promise.resolve(cacheFor(name)),
      keys: () => Promise.resolve([...store.keys()]),
      delete: (name) => {
        calls.deleted.push(name);
        return Promise.resolve(store.delete(name));
      },
      match: (target) => {
        const key = keyOf(target);
        for (const entries of store.values()) {
          if (entries.has(key)) return Promise.resolve(entries.get(key));
        }
        return Promise.resolve(undefined);
      },
    },
    fetch: (target) => {
      calls.fetch.push(keyOf(target));
      if (options.offline) return Promise.reject(new Error("offline"));
      return Promise.resolve(new FakeResponse("network:" + keyOf(target)));
    },
    self: {
      addEventListener: (type, handler) => {
        listeners[type] = handler;
      },
      skipWaiting: () => {
        calls.skipWaiting++;
        return Promise.resolve();
      },
      clients: {
        claim: () => {
          calls.claim++;
          return Promise.resolve();
        },
      },
    },
  };

  vm.runInNewContext(code, sandbox, { filename: "sw.js" });

  /** 派发一次 fetch 事件，返回 respondWith 的结果。 */
  const dispatch = (request) => {
    let pending = null;
    listeners.fetch({ request, respondWith: (value) => { pending = value; } });
    return Promise.resolve(pending);
  };

  const lifecycle = async () => {
    let done = null;
    listeners.install({ waitUntil: (value) => { done = value; } });
    await done;
    let activated = null;
    listeners.activate({ waitUntil: (value) => { activated = value; } });
    await activated;
  };

  return { store, calls, listeners, dispatch, lifecycle };
}

const navigate = (url) => ({ method: "GET", mode: "navigate", url: url || "https://x.test/?demo=1" });

(async () => {
  // ---- 1. 联网时的文档请求：必须走网络，不能被缓存压住 ----
  {
    const h = harness();
    await h.lifecycle();
    h.calls.fetch.length = 0;
    const response = await h.dispatch(navigate());
    check("文档请求联网时发起网络请求", h.calls.fetch.length === 1, String(h.calls.fetch.length));
    check("文档请求返回网络内容而非缓存", response && response.body === "network:https://x.test/?demo=1",
      response ? String(response.body) : "no response");
  }

  // ---- 2. 网络响应要写回 index.html 这个固定缓存键（否则带 sharecode 的链接各存一份）----
  {
    const h = harness();
    await h.lifecycle();
    await h.dispatch(navigate("https://x.test/?sharecode=ABC&demo=1"));
    const entries = [...h.store.values()][0];
    check("网络响应写回固定键 index.html", entries.has("index.html"));
    check("不按完整带参 URL 存储", !entries.has("https://x.test/?sharecode=ABC&demo=1"));
  }

  // ---- 3. 断网时的文档请求：回落到缓存的 index.html ----
  {
    const h = harness({ offline: true });
    await h.lifecycle();
    const response = await h.dispatch(navigate());
    check("断网时回落缓存页面", response && response.body === "precached:index.html",
      response ? String(response.body) : "no response");
    check("断网回落状态为 200", response && response.status === 200, response ? String(response.status) : "");
  }

  // ---- 4. 断网且无缓存：返回 503 而不是让页面白屏 ----
  {
    const h = harness({ offline: true });
    // 不执行 install，缓存为空
    const response = await h.dispatch(navigate());
    check("断网且无缓存返回 503", response && response.status === 503, response ? String(response.status) : "no response");
    check("503 带可读说明", response && /离线/.test(String(response.body)), response ? String(response.body) : "");
  }

  // ---- 5. 静态资源命中缓存时不打网络 ----
  {
    const h = harness();
    await h.lifecycle();
    h.calls.fetch.length = 0;
    const response = await h.dispatch({ method: "GET", mode: "cors", url: DEMO_KEY });
    check("静态资源命中缓存不发请求", h.calls.fetch.length === 0, String(h.calls.fetch.length));
    check("静态资源返回缓存内容", response && response.body === "precached:" + DEMO_KEY,
      response ? String(response.body) : "no response");
  }

  // ---- 5b. 不带版本串的 demo.csv 不能命中缓存：否则新界面会读到旧数据 ----
  {
    const h = harness();
    await h.lifecycle();
    h.calls.fetch.length = 0;
    const response = await h.dispatch({ method: "GET", mode: "cors", url: "demo.csv" });
    check("无版本串的示例数据不走缓存", h.calls.fetch.length === 1, String(h.calls.fetch.length));
    check("无版本串时回网络取", response && response.body === "network:demo.csv",
      response ? String(response.body) : "no response");
  }

  // ---- 6. 静态资源未命中：走网络并写入缓存 ----
  {
    const h = harness();
    await h.lifecycle();
    const response = await h.dispatch({ method: "GET", mode: "cors", url: "extra.png" });
    check("静态资源未命中走网络", response && response.body === "network:extra.png",
      response ? String(response.body) : "no response");
    await new Promise((resolve) => setTimeout(resolve, 0));
    const entries = [...h.store.values()][0];
    check("网络结果写入缓存", entries.has("extra.png"));
  }

  // ---- 7. 非 GET 请求不拦截 ----
  {
    const h = harness();
    await h.lifecycle();
    const response = await h.dispatch({ method: "POST", mode: "cors", url: "submit" });
    check("非 GET 请求不接管", response === null, String(response));
  }

  // ---- 8. install：预缓存整个外壳并跳过等待 ----
  {
    const h = harness();
    await h.lifecycle();
    check("预缓存 8 个外壳文件", h.calls.addAll.length === 8, h.calls.addAll.join(","));
    check("外壳含 xlsx.js（网页版 Excel 解析）", h.calls.addAll.includes("xlsx.js"), h.calls.addAll.join(","));
    check("外壳含 clean.js（网页版清洗引擎）", h.calls.addAll.includes("clean.js"), h.calls.addAll.join(","));
    check("外壳含 index.html 与示例数据",
      h.calls.addAll.includes("index.html") && h.calls.addAll.includes(DEMO_KEY),
      h.calls.addAll.join(","));
    check("示例数据缓存键带版本串", h.calls.addAll.includes(DEMO_KEY), DEMO_KEY);
    check("install 后跳过等待", h.calls.skipWaiting === 1, String(h.calls.skipWaiting));
  }

  // ---- 8b. 版本握手：页面发消息，SW 必须回报自己的版本号 ----
  {
    const h = harness();
    const replies = [];
    h.listeners.message({
      data: { type: "version" },
      ports: [{ postMessage: (payload) => replies.push(payload) }],
    });
    check("SW 回报版本号", replies.length === 1 && replies[0].version === VERSION,
      JSON.stringify(replies));
    const ignored = [];
    h.listeners.message({ data: { type: "other" }, ports: [{ postMessage: (p) => ignored.push(p) }] });
    check("非版本消息不回应", ignored.length === 0, JSON.stringify(ignored));
  }

  // ---- 9. activate：清掉旧版本缓存，保留当前版本 ----
  {
    check("缓存名由版本变量拼出", /const CACHE = "glassdata-" \+ VERSION/.test(code));

    const h = harness();
    // 先跑 install，让当前版本的缓存真实存在
    let installed = null;
    h.listeners.install({ waitUntil: (value) => { installed = value; } });
    await installed;
    h.store.set("glassdata-stale", new Map());

    let activated = null;
    h.listeners.activate({ waitUntil: (value) => { activated = value; } });
    await activated;
    const remaining = [...h.store.keys()];
    check("清理了非当前版本缓存", h.calls.deleted.includes("glassdata-stale"),
      h.calls.deleted.join(","));
    check("当前版本缓存被保留", remaining.length === 1 && remaining[0] !== "glassdata-stale",
      remaining.join(","));
    check("activate 后接管客户端", h.calls.claim === 1, String(h.calls.claim));
  }

  console.log("");
  if (failures.length) {
    console.log("FAILED: " + failures.join(", "));
    process.exit(1);
  }
  console.log("ALL_SW_CHECKS_PASSED");
})().catch((error) => {
  console.log("FAIL 执行异常 :: " + error.message);
  process.exit(1);
});
