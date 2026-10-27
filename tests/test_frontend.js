// 用最小 DOM 桩在 Node 中执行报告前端脚本，验证渲染、解析与画像逻辑不会抛错。
// analyze 现在是分列让出主线程的异步实现，因此主体跑在 async 里。
const fs = require("fs");
const vm = require("vm");

const html = fs.readFileSync("output/demo_report.html", "utf8");
const blocks = [...html.matchAll(/<script([^>]*)>([\s\S]*?)<\/script>/g)];
if (html.includes("__PAYLOAD__") || html.includes("__TITLE__")) {
  console.log("FAIL 模板占位符未替换");
  process.exit(1);
}
const payloadText = blocks.find((b) => /id="payload"/.test(b[1]))[2];
let appJs = blocks.filter((b) => !/id="payload"/.test(b[1])).pop()[2];

// 暴露内部函数用于单测（仅测试副本，不改动交付文件）
appJs = appJs.replace(
  /\}\)\(\);\s*$/,
  "globalThis.__t = { parseText, analyze, render, state, renderTable, parseDate, parseNum, " +
    "profileColumn, decodeBuffer, dateOrderAmbiguity, collectWarnings, checkYmd, renderCorr, fitDetailChart, " +
    "detectBinary, looksBinary, recordsToTable, applyCleaning, resetCleaning, downloadCleanedCsv, " +
    "collectCleanOptions, renderCleanNote, renderWarnings };\n})();"
);

const registry = new Map();
function makeEl(name) {
  const classes = new Set();
  const el = {
    _html: "",
    _text: "",
    className: "",
    style: {},
    hidden: false,
    offsetWidth: 120,
    offsetHeight: 30,
    children: new Map(),
    classList: {
      add(c) { classes.add(c); },
      remove(c) { classes.delete(c); },
      contains(c) { return classes.has(c); },
      _set: classes,
    },
    appendChild() {},
    removeChild() {},
    addEventListener() {},
    removeEventListener() {},
    setAttribute(k, v) { this[k] = v; },
    getAttribute() { return ""; },
    // 量尺寸统一走 getBoundingClientRect：SVG 元素上没有 offsetHeight
    getBoundingClientRect() { return { width: this.offsetWidth, height: this.offsetHeight, top: 0, left: 0 }; },
    closest() { return null; },
    click() {},
    focus() {},
  };
  Object.defineProperty(el, "innerHTML", {
    get() { return this._html; },
    set(v) { this._html = String(v); },
  });
  Object.defineProperty(el, "textContent", {
    get() { return this._text; },
    set(v) { this._text = String(v); },
  });
  el.querySelector = (sel) => {
    if (!el.children.has(sel)) el.children.set(sel, makeEl(name + " " + sel));
    return el.children.get(sel);
  };
  return el;
}
function lookup(key) {
  if (!registry.has(key)) registry.set(key, makeEl(key));
  return registry.get(key);
}
lookup("#payload").textContent = payloadText;

const context = {
  document: {
    readyState: "complete",
    querySelector: (sel) => lookup(sel),
    getElementById: (id) => lookup("#" + id),
    createElement: () => makeEl("created"),
    addEventListener() {},
    body: makeEl("body"),
    title: "",
  },
  window: { innerWidth: 1440, innerHeight: 900, addEventListener() {} },
  console,
  // 必须用真实的 setTimeout：analyze 靠它让出主线程，桩掉会导致永不 resolve
  setTimeout: (fn) => setTimeout(fn, 0),
  clearTimeout: (id) => clearTimeout(id),
  TextDecoder,
  Blob: function (parts) { this.parts = parts; },
  URL: { createObjectURL: () => "blob:x", revokeObjectURL() {} },
  FileReader: function () { this.readAsArrayBuffer = () => {}; },
  navigator: { language: "zh-CN" },
  location: { protocol: "http:", search: "" },
};
context.globalThis = context;

const failures = [];
function check(name, condition, detail) {
  if (condition) console.log("  ok   " + name);
  else { failures.push(name + (detail ? " :: " + detail : "")); console.log("  FAIL " + name + (detail ? " :: " + detail : "")); }
}

async function main() {
  vm.createContext(context);
  /* 页面里 clean.js 是通过 <script src> 先于应用脚本加载的；
     单文件报告没有这个外链，所以这里手动补进去，让 applyCleaning 能拿到 GlassClean。 */
  try {
    vm.runInContext(fs.readFileSync("glassdata/clean.js", "utf8"), context, { filename: "clean.js" });
  } catch (error) {
    failures.push("clean.js 加载异常: " + error.message);
    console.log("  FAIL clean.js 加载: " + error.message);
  }
  try {
    vm.runInContext(appJs, context, { filename: "app.js" });
  } catch (error) {
    failures.push("脚本执行异常: " + error.message);
    console.log("  FAIL 脚本执行: " + error.stack.split("\n").slice(0, 4).join(" | "));
  }

  const t = context.__t;
  check("内部接口可访问", !!t);
  if (!t) return;

  /* ---------- A. 内嵌数据启动渲染 ---------- */
  check("启动后载入内嵌数据", !!t.state.data && t.state.data.columns.length === 11);
  check("有数据时退出空状态", !lookup("body").classList.contains("is-empty"));
  check("KPI 已渲染", lookup("#kpiGrid").innerHTML.includes("kpi-value"));
  check("字段列表渲染 11 项", (lookup("#colList").innerHTML.match(/class="col-item/g) || []).length === 11);
  check("数值列绘制直方图", lookup("#detailChart").innerHTML.includes("<svg"));
  check("相关性矩阵已绘制", lookup("#corrChart").innerHTML.includes("<svg"));
  const headHtml = lookup("#dataTable").querySelector("thead").innerHTML;
  const headCells = (headHtml.match(/<th /g) || []).length;
  check("预览表表头 11 列", headCells === 11, String(headCells));
  // 数值列单元格右对齐，表头也必须右对齐，否则宽列里数字看起来落在下一列下面
  const numericCol = t.state.data.columns.filter((c) => c.type === "number")[0];
  const textCol = t.state.data.columns.filter((c) => c.type !== "number")[0];
  const numIdx = t.state.data.preview.columns.indexOf(numericCol.name);
  const textIdx = t.state.data.preview.columns.indexOf(textCol.name);
  check("数值列表头带 num 标记", new RegExp('<th data-index="' + numIdx + '"[^>]*class="num"').test(headHtml), numericCol.name);
  check("数值列表头箭头在文字左侧",
    new RegExp('<th data-index="' + numIdx + '"[^>]*><span class="sort">[^<]*</span>' + numericCol.name).test(headHtml),
    headHtml.slice(headHtml.indexOf('data-index="' + numIdx + '"'), headHtml.indexOf('data-index="' + numIdx + '"') + 60));
  check("文本列表头不带 num 标记", !new RegExp('<th data-index="' + textIdx + '"[^>]*class="num"').test(headHtml), textCol.name);
  check("预览表有数据行", (lookup("#dataTable").querySelector("tbody").innerHTML.match(/<tr>/g) || []).length === 40);
  const corrHtml = lookup("#corrChart").innerHTML;
  check("矩阵不出现负零", !corrHtml.includes("-0.00"));
  check("矩阵带色标图例", corrHtml.includes("完全负相关") && corrHtml.includes("完全正相关"));
  check("矩阵说明未计入的列", corrHtml.includes("未计入"));
  // 矩阵曾用写死的 padLeft=122 / padTop=100，中文短列名下左侧白留出三分之一面板宽，
  // 看起来就是「侧面空一大片」。这里按几何比例守住：网格要占满宽度，顶部只留列名那一行。
  check("列名横排而非斜排", !corrHtml.includes("rotate(-52)"));
  (function () {
    const vb = /viewBox="0 0 ([\d.]+) ([\d.]+)"/.exec(corrHtml);
    const rects = [...corrHtml.matchAll(/<rect x="([\d.]+)" y="([\d.]+)" width="([\d.]+)" height="([\d.]+)"/g)];
    if (!vb || rects.length < 2) return check("矩阵几何比例合理", false, "解析失败");
    const W = parseFloat(vb[1]), H = parseFloat(vb[2]);
    const left = Math.min(...rects.map((m) => parseFloat(m[1])));
    const right = Math.max(...rects.map((m) => parseFloat(m[1]) + parseFloat(m[3])));
    const top = Math.min(...rects.map((m) => parseFloat(m[2])));
    const gridW = right - left;
    check("矩阵横向占用超过六成宽度", gridW / W > 0.6, (gridW / W).toFixed(2));
    check("左内边距不超过网格宽度的三成", left / gridW < 0.35, left.toFixed(1));
    check("顶部只留一行列名", top < 40, top.toFixed(1));
    check("矩阵高度贴合网格", H - (right - left) < 160, (H - gridW).toFixed(1));
  })();
  // 列特别多时不能靠压缩格子硬塞，否则「-0.07」会糊成一团，改为横向滚动
  (function () {
    const host = lookup("#corrChart");
    const labels = Array.from({ length: 12 }, (_, i) => "指标" + (i + 1));
    const matrix = labels.map((_, i) => labels.map((__, j) => (i === j ? 1 : 0.12)));
    host.clientWidth = 300;
    t.renderCorr({
      correlation: { labels, matrix },
      columns: labels.map((name) => ({ name, type: "number" })),
    });
    const html = host.innerHTML;
    host.clientWidth = 0;
    const rects = [...html.matchAll(/<rect x="[\d.]+" y="[\d.]+" width="([\d.]+)"/g)];
    check("列太多时改为横向滚动", html.includes("min-width"), html.slice(0, 120));
    check("列太多时不把格子压到看不清", rects.length > 0 && parseFloat(rects[0][1]) >= 34,
      rects.length ? rects[0][1] : "无格子");
  })();
  check("缺失分布有内容", lookup("#missChart").innerHTML.includes("miss-row"));
  // 卡片被拉齐后直方图要吃满剩余高度。注意 SVG 没有 offsetHeight（HTMLElement 才有），
  // 量尺寸只能 getBoundingClientRect——用错会一直量到 0，然后静默不生效。
  (function () {
    const wrap = lookup("#detailChart");
    wrap.clientHeight = 430;
    wrap.clientWidth = 500;
    t.fitDetailChart();
    const vb = /viewBox="0 0 760 (\d+)"/.exec(wrap.innerHTML);
    wrap.clientHeight = 0;
    wrap.clientWidth = 0;
    check("直方图按剩余高度重算", !!vb && Number(vb[1]) > 330, vb ? vb[1] : "未找到 viewBox");
  })();
  check("切换字段后改绘类别条", (function () {
    // 不写死列名：示例数据的列名会调整，按类型挑一个类别列更稳
    const target = t.state.data.columns.filter((c) => c.type === "categorical")[0];
    if (!target) return false;
    t.state.selected = target.name;
    t.render();
    return lookup("#detailChart").innerHTML.includes("cat-row");
  })());

  /* ---------- B. CSV 解析边角 ---------- */
  const csv = [
    "name,units,amount,when,note,name",
    '"ACME, Inc.",1,"2,000",2026-01-05,hello,a',
    'Beta,,3,2026-02-11,"multi',
    'line",b',
    'Gamma,4.5e2,4,2026-03-02,,c',
  ].join("\n");
  const parsed = t.parseText(csv, "edge.csv");
  check("列数正确", parsed.columns.length === 6, JSON.stringify(parsed.columns));
  check("重复列名自动去重", parsed.columns[5] === "name_1", JSON.stringify(parsed.columns));
  check("引号内逗号不被拆分", parsed.rows[0][0] === "ACME, Inc.", parsed.rows[0][0]);
  check("引号包裹的千分位保持原样", parsed.rows[0][2] === "2,000", parsed.rows[0][2]);
  check("跨行引号合并为一行", parsed.rows.length === 3, String(parsed.rows.length));
  check("行中空值保留", parsed.rows[2][4] === "", JSON.stringify(parsed.rows[2]));

  let headerOnly = "";
  try { t.parseText("a,b,c", "only.csv"); } catch (error) { headerOnly = error.message; }
  check("只有表头时明确报错", /只有表头/.test(headerOnly), headerOnly);

  /* ---------- C. 画像推断 ---------- */
  const edge = await t.analyze(parsed.columns, parsed.rows, { source: "edge.csv", engine: "browser" });
  const col = (n) => edge.columns.find((c) => c.name === n);
  check("缺失计入缺失数", col("units").missing === 1, JSON.stringify(col("units").missing));
  check("科学计数法解析", Math.abs(col("units").stats.max - 450) < 1e-9, JSON.stringify(col("units").stats));
  check("千分位字符串解析", Math.abs(col("amount").stats.max - 2000) < 1e-9, JSON.stringify(col("amount").stats));
  check("日期列识别为 datetime", col("when").type === "datetime", col("when").type);
  check("日期跨度计算", Math.abs(col("when").stats.spanDays - 56) < 1.5, JSON.stringify(col("when").stats));
  check("低基数文本判为类别", col("note").type === "categorical", col("note").type);
  check("全唯一名称不误判为数值", col("name").type !== "number");
  check("输出无 NaN/Infinity", !/NaN|Infinity/.test(JSON.stringify(edge)));

  /* ---------- D. 高基数文本 / 布尔 / 全空列 ---------- */
  const wide = { columns: ["blob", "flag", "empty"], rows: [] };
  for (let i = 0; i < 60; i++) wide.rows.push(["token-" + (i % 45) + "-" + "x".repeat(30), i % 2 ? "是" : "否", ""]);
  const widePayload = await t.analyze(wide.columns, wide.rows, { source: "wide.csv", engine: "browser" });
  const wcol = (n) => widePayload.columns.find((c) => c.name === n);
  check("高基数文本识别为 text", wcol("blob").type === "text", wcol("blob").type);
  check("中文布尔识别", wcol("flag").type === "boolean", wcol("flag").type);
  check("全空列识别为空", wcol("empty").type === "empty", wcol("empty").type);
  check("空列缺失数正确", wcol("empty").missing === 60, String(wcol("empty").missing));
  check("全空列产生提示", widePayload.warnings.some((w) => w.kind === "empty-column"));

  /* ---------- E. 日期顺序与歧义告警 ---------- */
  t.state.dateOrder = "dmy";
  const ambRows = [["01/05/2026", "1"], ["03/04/2026", "2"], ["25/12/2026", "3"]];
  const dmy = await t.analyze(["when", "v"], ambRows, { source: "amb.csv", engine: "browser" });
  const mdyOrder = (function () { t.state.dateOrder = "mdy"; return true; })();
  const mdy = await t.analyze(["when", "v"], ambRows, { source: "amb.csv", engine: "browser" });
  t.state.dateOrder = "dmy";
  check("mdy 与 dmy 得到不同区间",
    dmy.columns[0].stats.min !== mdy.columns[0].stats.min,
    dmy.columns[0].stats.min + " vs " + mdy.columns[0].stats.min);
  check("歧义计数正确（2 个）", t.dateOrderAmbiguity(["01/05/2026", "03/04/2026", "25/12/2026", "5/5/2026"]) === 2,
    String(t.dateOrderAmbiguity(["01/05/2026", "03/04/2026", "25/12/2026", "5/5/2026"])));
  const ambWarn = dmy.warnings.filter((w) => w.kind === "date-order");
  check("歧义列产出告警", ambWarn.length === 1, JSON.stringify(dmy.warnings));
  check("告警带可切换的列名与顺序", ambWarn[0] && ambWarn[0].column === "when" && ambWarn[0].order === "dmy");
  check("非歧义写法不误报", t.dateOrderAmbiguity(["2026-01-05", "2026-02-11", "13/05/2026"]) === 0);
  check("mdy 下非法月自动退回日/月", (function () {
    const stamp = t.parseDate("25/12/2026", "mdy");
    return new Date(stamp).getUTCMonth() === 11 && new Date(stamp).getUTCDate() === 25;
  })());
  check("2 月 31 日不被静默归一", t.parseDate("2026-02-31") === null, String(t.parseDate("2026-02-31")));

  /* ---------- F. 编码回退 ---------- */
  const gbkBytes = new Uint8Array([0xc0, 0xe0, 0xb1, 0xf0]); // 「类别」的 GBK 编码
  const gbk = t.decodeBuffer(gbkBytes.buffer);
  check("GBK 字节被识别并回退", gbk.text === "类别", gbk.text + " / " + gbk.label);
  check("回退标签非 UTF-8", !/^UTF-8$/.test(gbk.label), gbk.label);
  const utf8 = t.decodeBuffer(new Uint8Array([0x61, 0x2c, 0x62]).buffer);
  check("纯 ASCII 走 UTF-8", utf8.text === "a,b" && /^UTF-8$/.test(utf8.label), utf8.label);

  // 判定顺序：BOM → UTF-16 特征 → UTF-8 → GB18030/Big5 打分。
  // 若让 GB18030 先试，它会"成功"解出 UTF-16 与 Big5 的字节，
  // 结果是满屏乱码却不报错——这类静默失败必须在测试里钉死。
  const src = "名称,数量\n苹果,3\n";
  const le = Buffer.from(src, "utf16le");
  const be = Buffer.from(le);
  for (let i = 0; i < be.length; i += 2) { const t0 = be[i]; be[i] = be[i + 1]; be[i + 1] = t0; }

  const u16le = t.decodeBuffer(new Uint8Array([0xff, 0xfe, ...le]));
  check("UTF-16 LE（带 BOM）正确解码", u16le.text.startsWith("名称") && /UTF-16 LE/.test(u16le.label),
    u16le.text.slice(0, 8) + " / " + u16le.label);
  const u16be = t.decodeBuffer(new Uint8Array([0xfe, 0xff, ...be]));
  check("UTF-16 BE（带 BOM）正确解码", u16be.text.startsWith("名称") && /UTF-16 BE/.test(u16be.label),
    u16be.text.slice(0, 8) + " / " + u16be.label);
  const u16leNoBom = t.decodeBuffer(new Uint8Array(le));
  check("无 BOM 的 UTF-16 LE 靠 NUL 特征识别", u16leNoBom.text.startsWith("名称"),
    u16leNoBom.text.slice(0, 8) + " / " + u16leNoBom.label);

  const u32 = [];
  for (const ch of src.slice(0, 2)) {
    const cp = ch.codePointAt(0);
    u32.push(cp & 0xff, (cp >> 8) & 0xff, 0, 0);
  }
  const u32dec = t.decodeBuffer(new Uint8Array([0xff, 0xfe, 0x00, 0x00, ...u32]));
  check("UTF-32（带 BOM）正确解码", u32dec.text === "名称", u32dec.text + " / " + u32dec.label);

  // 「名稱,數量」的 Big5 字节：GB18030 也能"解成功"，只能靠打分选出正确的
  const big5Bytes = new Uint8Array([0xa6, 0x57, 0xba, 0xd9, 0x2c, 0xbc, 0xc6, 0xb6, 0x71]);
  const big5 = t.decodeBuffer(big5Bytes.buffer);
  check("Big5 繁体不被 GB18030 抢解", big5.text === "名稱,數量", big5.text + " / " + big5.label);

  /* ---------- F2. JSONL 与二进制识别 ---------- */
  // 每行一个 JSON 若被当成分隔文本，列名会变成 ['{"a":1', 'b:2}']——
  // 不报错、也没法用，这是最难被发现的一类失败。
  const jsonl = t.parseText('{"a":1,"b":"x"}\n{"a":2,"c":3}\n', "x.jsonl");
  check("JSONL 键并集", JSON.stringify(jsonl.columns) === JSON.stringify(["a", "b", "c"]),
    JSON.stringify(jsonl.columns));
  check("JSONL 缺键补空", jsonl.rows[1][1] === "", JSON.stringify(jsonl.rows[1]));
  const ndjson = t.parseText("[1,2]\n[3]\n", "x.ndjson");
  check("NDJSON 数组行补空", ndjson.columns.length === 2 && ndjson.rows[1][1] === "",
    JSON.stringify(ndjson));
  let jsonlErr = "";
  try { t.parseText('{"a":1}\n{"a":\n', "x.jsonl"); } catch (error) { jsonlErr = error.message; }
  check("JSONL 坏行指出行号", /第 2 行/.test(jsonlErr), jsonlErr);

  // 二进制靠文件头识别，不靠扩展名：用户会把 parquet 改名成 .csv
  const sig = (arr) => t.detectBinary(new Uint8Array(arr));
  check("识别 Parquet 文件头", /Parquet/.test(sig([0x50, 0x41, 0x52, 0x31, 0, 0])));
  check("识别 SQLite 文件头", /SQLite/.test(sig([...Buffer.from("SQLite format 3\u0000", "latin1")])));
  check("识别 gzip 文件头", /gzip/.test(sig([0x1f, 0x8b, 0x08])));
  check("识别 xlsx 压缩头", /Excel/.test(sig([0x50, 0x4b, 0x03, 0x04])));
  check("正常 CSV 不误判为二进制", sig([0x61, 0x2c, 0x62, 0x0a]) === "", sig([0x61, 0x2c, 0x62, 0x0a]));
  check("含控制字符判为二进制", t.looksBinary("\u0001\u0002\u0003\u0004\u0005abc"));
  check("正常表格不判为二进制", !t.looksBinary("a,b\n1,2\n甲,3\n"));
  check("制表符与换行不算二进制", !t.looksBinary("\t\n\r\n"));

  /* ---------- G. 切换数据集重渲染 ---------- */
  t.state.data = edge;
  t.state.selected = null;
  t.state.query = "";
  try {
    t.render();
    check("小数据集重渲染不抛错", lookup("#detailChart").innerHTML.length > 0);
  } catch (error) {
    check("小数据集重渲染不抛错", false, error.message);
  }

  /* ---------- H. 排序与筛选 ---------- */
  try {
    t.state.sort = { index: 1, dir: "desc" };
    t.renderTable(edge);
    const body = lookup("#dataTable").querySelector("tbody").innerHTML;
    const firstCell = (body.match(/<td class="num"[^>]*>([^<]*)</) || [])[1];
    check("数值列降序排序生效（原值展示）", firstCell === "4.5e2", String(firstCell));
    t.state.sort = null;
    t.state.query = "Gamma";
    t.renderTable(edge);
    check("排序后缺失值排在末尾", /4\.5e2[\s\S]*<span class="na">/.test(body));
    check("预览筛选生效", (lookup("#dataTable").querySelector("tbody").innerHTML.match(/<tr>/g) || []).length === 1);
  } catch (error) {
    check("排序/筛选", false, error.message);
  }

  /* ---------- I. 页面内清洗 ---------- */
  /* 走一遍真实链路：原始数据 → clean.js 清洗 → 重新分析 → 渲染清洗摘要条。
     这里不模拟点击，直接调 applyCleaning()，因为 click 桩不会真的触发事件；
     真正要守住的是「清洗结果有没有真的替换掉页面上的分析」。 */
  try {
    check("未载入数据时清洗按钮不显示", lookup("#cleanBtn").hidden === true);
    const raw = {
      columns: ["城市", "金额", "备注"],
      rows: [
        [" 北京 ", "1,000", ""],
        ["", "", ""],
        ["上海", "¥2,000", "x"],
        ["上海", "¥2,000", "x"],
        ["广州", "3,000", " "],
      ],
    };
    t.state.parsed = { columns: raw.columns, rows: raw.rows, name: "测试订单.csv" };
    t.render();
    check("载入数据后清洗按钮出现且可用",
      lookup("#cleanBtn").hidden === false && lookup("#cleanBtn").disabled === false);
    check("未清洗时下载按钮为禁用", lookup("#cleanDownload").disabled === true);

    await t.applyCleaning();
    check("清洗后有结果对象", !!t.state.cleaned);
    check("空行与重复行被去掉", t.state.cleaned.rows.length === 3, t.state.cleaned.rows.length);
    check("清洗后页面分析的是清洗后的数据", t.state.data.meta.rows === 3, t.state.data.meta.rows);
    check("画像里带上清洗记录", !!t.state.data.cleaning && t.state.data.cleaning.before.rows === 5,
      t.state.data.cleaning && t.state.data.cleaning.before);
    const strip = lookup("#cleanStrip");
    check("清洗摘要条已显示", strip.hidden === false && strip.innerHTML.includes("已清洗"), strip.hidden);
    check("摘要条给出前后行数", strip.innerHTML.includes("5") && strip.innerHTML.includes("3"));
    check("清洗后下载按钮可用", lookup("#cleanDownload").disabled === false);
    check("说明行给出清洗策略", lookup("#cleanNote").innerHTML.includes("去首尾空白"), lookup("#cleanNote").innerHTML);

    t.downloadCleanedCsv();
    check("下载清洗后 CSV 不抛错", true);

    await t.resetCleaning();
    check("还原后回到原始行数", t.state.data.meta.rows === 5, t.state.data.meta.rows);
    check("还原后摘要条隐藏", lookup("#cleanStrip").hidden === true);
    check("还原后清洗结果清空", t.state.cleaned === null);
    check("还原后下载按钮回到禁用", lookup("#cleanDownload").disabled === true);
  } catch (error) {
    check("页面内清洗", false, error.message);
  }

  /* 读取层提示（跳过标题行 / 自动换表 / 一列表像说明文字）必须先于统计提示出现。
     它解释的是「这份表是怎么被理解的」，位置在统计口径之前才有意义。
     这段渲染路径此前没有测试——所以即使文案没被画出来，测试也是绿的。 */
  try {
    const row = lookup("#warnRow");
    t.state.readNotes = ["工作表「说明」只有一列、行数很少，更像说明文字，已自动改用「数据」"];
    t.renderWarnings({ warnings: [{ level: "warn", message: "金额列有 3 个异常值" }] });
    check("读取提示被渲染出来", row.innerHTML.includes("只有一列"), row.innerHTML);
    check("读取提示排在统计提示前面",
      row.innerHTML.indexOf("只有一列") < row.innerHTML.indexOf("异常值"), row.innerHTML);
    check("读取提示用 info 级别，不冒充警告", row.innerHTML.includes("warn-chip info"), row.innerHTML);

    t.state.readNotes = [];
    t.renderWarnings({ warnings: [] });
    check("没有提示时清空提示区", row.innerHTML === "", row.innerHTML);

    // 超过 4 条时折叠，别把首屏撑满
    t.state.readNotes = ["n1", "n2", "n3", "n4", "n5", "n6"];
    t.renderWarnings({ warnings: [] });
    check("提示超过 4 条时折叠", row.innerHTML.includes("另有 2 条提示"), row.innerHTML);
    t.state.readNotes = [];
  } catch (error) {
    check("读取提示渲染", false, error.message);
  }

  /* 导出 CSV 时的公式中和必须真的提示到用户面前。
     只测 toCsv 的返回值不够——返回值对了但没人告诉用户，等于静默改数据。 */
  try {
    t.state.cleaned = { columns: ["名称"], rows: [["=cmd|'/C calc'!A0"], ["北京"]] };
    t.downloadCleanedCsv();
    const note = lookup("#toast").textContent || "";
    check("导出时提示有格子被加单引号", note.indexOf("单引号") >= 0, note);

    t.state.cleaned = { columns: ["名称"], rows: [["北京"], ["上海"]] };
    t.downloadCleanedCsv();
    const quiet = lookup("#toast").textContent || "";
    check("没有危险单元格时不吓唬用户", quiet.indexOf("单引号") < 0, quiet);
    t.state.cleaned = null;
  } catch (error) {
    check("导出公式中和提示", false, error.message);
  }
}

main().then(() => {
  console.log(failures.length ? "\nFAILURES:\n" + failures.map((f) => " - " + f).join("\n") : "\nALL_RUNTIME_CHECKS_PASSED");
  process.exit(failures.length ? 1 : 0);
}).catch((error) => {
  console.log("FAIL 测试自身异常: " + error.stack);
  process.exit(1);
});
