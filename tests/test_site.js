/* 站点构建自检：PWA 清单、图标、内联脚本语法、无外部依赖。 */
const fs = require("fs");
const path = require("path");

const WEB = path.join(__dirname, "..", "web");
const failures = [];

function check(name, condition, detail) {
  if (condition) {
    console.log("  ok   " + name);
  } else {
    failures.push(name);
    console.log("  FAIL " + name + (detail ? " :: " + detail : ""));
  }
}

const html = fs.readFileSync(path.join(WEB, "index.html"), "utf8");

check("占位符已全部替换", !/__TITLE__|__PAYLOAD__|__HEAD_EXTRA__/.test(html));
check("引用了 PWA 清单", html.includes('rel="manifest" href="manifest.webmanifest"'));
check("注册了 Service Worker", /serviceWorker\s*\.\s*register\("sw\.js"/.test(html));
check("注册时禁用 HTTP 缓存判定（updateViaCache: none）",
  /register\("sw\.js",\s*\{\s*updateViaCache:\s*"none"\s*\}\)/.test(html));
check("接管时握手自愈旧页面", html.includes('controllerchange') && html.includes('type: "version"'));
check("示例数据请求带版本串", html.includes('fetch("demo.csv" + bust'));
check("存在示例数据入口", html.includes('id="demoBtn"'));

/* ---- 清洗工作台的接线 ----
   面板里的控件由 id 逐个取用，少一个不会报错，只会在浏览器里静默失效
   （点了没反应 / 选项读不到）。因此把「面板该有的 id」列出来逐个对。 */
const CLEAN_HOOKS = [
  "cleanBtn", "cleanDrawer", "cleanClose", "optTrim", "optDropBlank", "optDedupe", "optCoerce",
  "cleanFill", "cleanValueField", "cleanValue", "cleanOutliers",
  "cleanApply", "cleanReset", "cleanDownload", "cleanNote",
];
const missingHooks = CLEAN_HOOKS.filter((id) => !html.includes('id="' + id + '"'));
check("清洗面板的控件齐全", missingHooks.length === 0, missingHooks.join(","));
check("清洗面板默认收起", /id="cleanDrawer"[^>]*\bhidden\b/.test(html));
check("清洗样式已内联", html.includes(".clean-drawer") && html.includes(".clean-check.is-on"));
check("清洗面板在空状态隐藏", html.includes("body.is-empty .clean-drawer"));
/* 文件选择框必须认 Excel：accept 里漏了 .xlsx 时，用户点「导入数据」根本看不到 xlsx 文件，
   会以为网页版不支持——这一条曾经真的漏过。 */
check("文件选择框接受 xlsx", /accept="[^"]*\.xlsx/.test(html));
check("网页版清洗引擎已随包发布", fs.existsSync(path.join(WEB, "clean.js")));
check("未引用任何外部资源",
  !/(src|href)="https?:\/\//.test(html) && !html.includes("cdn."));

const blocks = [...html.matchAll(/<script([^>]*)>([\s\S]*?)<\/script>/g)];
const inlineBlocks = blocks.filter((block) => !/\bsrc\s*=/.test(block[1]));
check("内联脚本数量为 3（PWA 注册 / 数据块 / 应用）", inlineBlocks.length === 3, String(inlineBlocks.length));
check("外链脚本只有 xlsx.js 与 clean.js",
  blocks.length - inlineBlocks.length === 2
    && /src="xlsx\.js"/.test(html) && /src="clean\.js"/.test(html),
  String(blocks.length));
/* 两个外链模块必须排在应用脚本之前：应用脚本一执行就要用 GlassXlsx / GlassClean，
   顺序反了在浏览器里表现成「点了清洗没反应」，而不是一个明确的报错。 */
check("外链脚本位于应用脚本之前",
  html.indexOf('src="clean.js"') < html.lastIndexOf("<script>"),
  String([html.indexOf('src="clean.js"'), html.lastIndexOf("<script>")]));

const isData = (attrs) => /type\s*=\s*["']application\/json["']/.test(attrs);
const dataBlocks = blocks.filter((block) => isData(block[1]));
check("数据块只有一个", dataBlocks.length === 1, String(dataBlocks.length));
let dataOk = true;
let dataDetail = "";
try {
  JSON.parse(dataBlocks[0][2]);
} catch (error) {
  dataOk = false;
  dataDetail = error.message;
}
check("数据块是合法 JSON", dataOk, dataDetail);

let syntaxOk = true;
let syntaxDetail = "";
inlineBlocks.filter((block) => !isData(block[1])).forEach((block, index) => {
  try {
    new Function(block[2]);
  } catch (error) {
    syntaxOk = false;
    syntaxDetail = "第 " + (index + 1) + " 段脚本：" + error.message;
  }
});
check("内联脚本语法正确", syntaxOk, syntaxDetail);

const manifest = JSON.parse(fs.readFileSync(path.join(WEB, "manifest.webmanifest"), "utf8"));
check("清单包含 standalone 显示模式", manifest.display === "standalone");
check("清单起始地址为相对路径", manifest.start_url === "./", manifest.start_url);
check("清单声明了 192 与 512 图标",
  manifest.icons.some((i) => i.sizes === "192x192") && manifest.icons.some((i) => i.sizes === "512x512"));
manifest.icons.forEach((icon) => {
  check("图标文件存在：" + icon.src, fs.existsSync(path.join(WEB, icon.src)));
});

check("存在 Service Worker", fs.existsSync(path.join(WEB, "sw.js")));
check("存在示例数据", fs.existsSync(path.join(WEB, "demo.csv")));
check("存在网页版 Excel 解析脚本", fs.existsSync(path.join(WEB, "xlsx.js")));
const xlsxSource = fs.existsSync(path.join(WEB, "xlsx.js"))
  ? fs.readFileSync(path.join(WEB, "xlsx.js"), "utf8") : "";
check("xlsx.js 语法正确", (() => { try { new Function(xlsxSource); return true; } catch (e) { return false; } })());
check("xlsx.js 不依赖任何外部资源", !/(import\s+.*from|require\()/.test(xlsxSource.replace(/module\.exports[^\n]*/g, "")));
check("xlsx.js 显式声明浏览器能力要求", xlsxSource.includes("DecompressionStream"));

const sw = fs.readFileSync(path.join(WEB, "sw.js"), "utf8");
try {
  new Function(sw);
  check("Service Worker 语法正确", true);
} catch (error) {
  check("Service Worker 语法正确", false, error.message);
}

check("Service Worker 无未替换占位符", !sw.includes("__CACHE_VERSION__"));

// 缓存名必须由内容哈希生成：写死版本号会导致 sw.js 字节不变，
// 浏览器不重新安装 SW，老用户会永远停在旧版界面上。
const cacheMatch = sw.match(/const CACHE = "glassdata-" \+ VERSION/);
check("缓存名拼在内容哈希上", Boolean(cacheMatch));
const versionMatch = sw.match(/const VERSION = "([0-9a-f]{8,})"/);
check("缓存版本是内容哈希", Boolean(versionMatch), versionMatch ? versionMatch[1] : "未找到");

// 页面里写死的版本号必须与 sw.js 一致：页脚把它显示给用户，
// 一旦两边脱钩，「看到的版本」就失去意义（也就没法判断是不是旧页面）。
const buildMatch = html.match(/window\.__BUILD__ = \{"v": "([0-9a-f]+)"\}/);
check("页面注入了构建版本号", Boolean(buildMatch), buildMatch ? buildMatch[1] : "未找到");
check("页面版本号与 sw.js 一致",
  Boolean(buildMatch && versionMatch) && buildMatch[1] === versionMatch[1],
  (buildMatch || [])[1] + " vs " + (versionMatch || [])[1]);
check("页脚有版本号落位", html.includes('id="buildStamp"'));

// 文档请求必须网络优先，否则联网用户也看不到新版
const navIndex = sw.indexOf('request.mode === "navigate"');
const navBody = navIndex >= 0 ? sw.slice(navIndex, navIndex + 420) : "";
check("文档请求走网络优先",
  navBody.indexOf("fetch(request)") >= 0 && navBody.indexOf("fetch(request)") < navBody.indexOf("caches.match"),
  navBody ? "" : "未找到 navigate 分支");

const demo = fs.readFileSync(path.join(WEB, "demo.csv"), "utf8").trim().split("\n");
const header = demo[0].replace(/^\uFEFF/, "").split(",");
check("示例数据包含表头与多行", header.length > 3 && demo.length > 100,
  header.length + " 列 / " + demo.length + " 行");

// ---- 全中文显示：界面文案里不应残留英文（数据本身的取值不算）----
const visible = html
  .replace(/<style[\s\S]*?<\/style>/g, " ")
  .replace(/<script[\s\S]*?<\/script>/g, " ")
  .replace(/<!--[\s\S]*?-->/g, " ");
[
  "Pearson",
  "Liquid Glass",
  "Data Profiler",
  "glassdata",
  "P25",
  "P75",
  "ETL",
  "Data Profile",
].forEach((word) => {
  check("界面文案不含英文：" + word, visible.indexOf(word) === -1);
});

// 示例数据的表头也应是中文，否则首屏会一半中文一半英文
const latinHeaders = header.filter((name) => /[A-Za-z]/.test(name));
check("示例数据表头为中文", latinHeaders.length === 0, latinHeaders.join(","));

// ---- 版式规则：数值列表头右对齐（表头左对齐 + 数字右对齐会错开半列）----
check("数值列表头右对齐样式", /thead th\.num\s*\{[^}]*text-align:\s*right/.test(html));
check("矩阵色标样式存在", /\.corr-bar\s*\{[^}]*linear-gradient/.test(html));

// ---- 同一行三块面板必须等高，图表卡片要把剩余高度用掉而不是留白 ----
check("面板行等高（align-items: stretch）", /\.layout\s*\{[^}]*align-items:\s*stretch/.test(html));
check("图表卡片标了 card-fill", /class="card glass card-fill"[^>]*grid-area:\s*chart/.test(html));
check("图表卡片纵向撑满", /\.card-fill\s*\{[^}]*flex-direction:\s*column/.test(html));
check("图表容器吃掉剩余高度", /\.card-fill \.chart-wrap\s*\{[^}]*flex:\s*1/.test(html));
// SVG 绝对定位后不参与卡片高度计算，否则「图表变高→行变高→图表再变高」会滚雪球
check("直方图绝对定位不撑高卡片", /\.card-fill \.chart-wrap > \.chart-svg\s*\{[^}]*position:\s*absolute/.test(html));
// SVG 元素没有 offsetHeight，量尺寸必须用 getBoundingClientRect，否则永远量到 0、静默不生效
check("图表量尺寸使用 getBoundingClientRect", /function boxHeight[\s\S]{0,200}getBoundingClientRect/.test(html));
check("render 收尾重算图表高度", /renderTable\(data\);\s*fitDetailChart\(\)/.test(html));

if (failures.length) {
  console.log("\nFAILED: " + failures.join(", "));
  process.exit(1);
}
console.log("\nALL_SITE_CHECKS_PASSED");
