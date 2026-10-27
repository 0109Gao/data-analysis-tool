/* 网页版 Excel 读取自检：现场造一个真 zip（deflate），再用 glassdata/xlsx.js 读回来。
 *
 * 为什么在 Node 里测：浏览器里跑 headless 无法断言「解析出来的每个格子是什么」，
 * 而这段代码浏览器与测试跑的是同一份实现（刻意不用 DOMParser），所以结论可复现。
 * 关键断言同时对齐 Python 端 test_excel.py 的期望值——两端读同一个文件必须得到同一张表。
 */
const fs = require("fs");
const path = require("path");
const zlib = require("zlib");
const vm = require("vm");

const SRC = path.join(__dirname, "..", "glassdata", "xlsx.js");
const code = fs.readFileSync(SRC, "utf8");

const failures = [];
function check(name, condition, detail) {
  if (condition) console.log("  ok   " + name);
  else {
    failures.push(name);
    console.log("  FAIL " + name + (detail !== undefined ? " :: " + detail : ""));
  }
}

/* ---------------- 最小 zip 写入器（deflate） ---------------- */
const CRC_TABLE = (function () {
  const table = new Int32Array(256);
  for (let n = 0; n < 256; n++) {
    let c = n;
    for (let k = 0; k < 8; k++) c = c & 1 ? 0xedb88320 ^ (c >>> 1) : c >>> 1;
    table[n] = c;
  }
  return table;
})();

function crc32(buffer) {
  let c = -1;
  for (let i = 0; i < buffer.length; i++) c = CRC_TABLE[(c ^ buffer[i]) & 0xff] ^ (c >>> 8);
  return (c ^ -1) >>> 0;
}

function makeZip(files) {
  const locals = [];
  const central = [];
  let offset = 0;
  Object.keys(files).forEach((name) => {
    const data = Buffer.from(files[name], "utf8");
    const packed = zlib.deflateRawSync(data);
    const nameBuf = Buffer.from(name, "utf8");
    const crc = crc32(data);

    const local = Buffer.alloc(30);
    local.writeUInt32LE(0x04034b50, 0);
    local.writeUInt16LE(20, 4);
    local.writeUInt16LE(0, 6);
    local.writeUInt16LE(8, 8);
    local.writeUInt16LE(0, 10);
    local.writeUInt16LE(0, 12);
    local.writeUInt32LE(crc, 14);
    local.writeUInt32LE(packed.length, 18);
    local.writeUInt32LE(data.length, 22);
    local.writeUInt16LE(nameBuf.length, 26);
    local.writeUInt16LE(0, 28);
    locals.push(local, nameBuf, packed);

    const entry = Buffer.alloc(46);
    entry.writeUInt32LE(0x02014b50, 0);
    entry.writeUInt16LE(20, 4);
    entry.writeUInt16LE(20, 6);
    entry.writeUInt16LE(0, 8);
    entry.writeUInt16LE(8, 10);
    entry.writeUInt16LE(0, 12);
    entry.writeUInt16LE(0, 14);
    entry.writeUInt32LE(crc, 16);
    entry.writeUInt32LE(packed.length, 20);
    entry.writeUInt32LE(data.length, 24);
    entry.writeUInt16LE(nameBuf.length, 28);
    entry.writeUInt32LE(offset, 42);
    central.push(Buffer.concat([entry, nameBuf]));

    offset += 30 + nameBuf.length + packed.length;
  });

  const centralBuf = Buffer.concat(central);
  const end = Buffer.alloc(22);
  end.writeUInt32LE(0x06054b50, 0);
  end.writeUInt16LE(central.length, 8);
  end.writeUInt16LE(central.length, 10);
  end.writeUInt32LE(centralBuf.length, 12);
  end.writeUInt32LE(offset, 16);
  return Buffer.concat([...locals, centralBuf, end]);
}

/* ---------------- 造工作簿 ---------------- */
const STYLES = `<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
<numFmts count="1"><numFmt numFmtId="164" formatCode="yyyy&quot;年&quot;m&quot;月&quot;d&quot;日&quot;"/></numFmts>
<cellXfs count="3"><xf numFmtId="0"/><xf numFmtId="14" applyNumberFormat="1"/><xf numFmtId="164" applyNumberFormat="1"/></cellXfs>
</styleSheet>`;

// 2026-03-01 相对 1899-12-30 是 46112 天；带时刻的用 46116.604166...
function serialOf(year, month, day, hour, minute) {
  const base = Date.UTC(1899, 11, 30);
  const target = Date.UTC(year, month - 1, day, hour || 0, minute || 0);
  return (target - base) / 86400000;
}

function buildWorkbook() {
  const shared = ["订单号", "区域", "数量", "金额", "下单日期", "已付款", "备注",
    "A001", "华东", "A002", "华南", "内联文本", "#DIV/0!", "华北", "B001", "西南"];
  const sst = `<?xml version="1.0" encoding="UTF-8"?><sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">`
    + shared.map((text) => `<si><t>${text}</t></si>`).join("") + `</sst>`;

  const sheet1 = `<?xml version="1.0" encoding="UTF-8"?>
<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData>
<row r="1"><c r="A1" t="s"><v>0</v></c><c r="B1" t="s"><v>1</v></c><c r="C1" t="s"><v>2</v></c><c r="D1" t="s"><v>3</v></c><c r="E1" t="s"><v>4</v></c><c r="F1" t="s"><v>5</v></c><c r="G1" t="s"><v>6</v></c></row>
<row r="2"><c r="A2" t="s"><v>7</v></c><c r="B2" t="s"><v>8</v></c><c r="C2"><v>3</v></c><c r="D2"><v>128.5</v></c><c r="E2" s="1"><v>${serialOf(2026, 3, 1)}</v></c><c r="F2" t="b"><v>1</v></c><c r="G2" t="s"><v>11</v></c></row>
<row r="3"><c r="A3" t="s"><v>9</v></c><c r="B3" t="s"><v>10</v></c><c r="C3"><v>1</v></c><c r="D3"><v>66</v></c><c r="E3" s="2"><v>${serialOf(2026, 3, 5, 14, 30)}</v></c><c r="F3" t="b"><v>0</v></c><c r="G3" t="inlineStr"><is><t>内联文本</t></is></c></row>
<row r="4"><c r="A4" t="s"><v>13</v></c><c r="B4" t="s"><v>13</v></c></row>
</sheetData></worksheet>`;

  const sheet2 = `<?xml version="1.0" encoding="UTF-8"?>
<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData>
<row r="1"><c r="A1" t="s"><v>0</v></c><c r="B1" t="s"><v>1</v></c></row>
<row r="2"><c r="A2" t="s"><v>14</v></c><c r="B2" t="s"><v>15</v></c></row>
</sheetData></worksheet>`;

  const workbook = `<?xml version="1.0" encoding="UTF-8"?>
<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
<sheets><sheet name="一月" sheetId="1" r:id="rId1"/><sheet name="二月" sheetId="2" r:id="rId2"/></sheets></workbook>`;

  const rels = `<?xml version="1.0" encoding="UTF-8"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>
<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet2.xml"/>
</Relationships>`;

  return makeZip({
    "[Content_Types].xml": "<Types/>",
    "xl/workbook.xml": workbook,
    "xl/_rels/workbook.xml.rels": rels,
    "xl/styles.xml": STYLES,
    "xl/sharedStrings.xml": sst,
    "xl/worksheets/sheet1.xml": sheet1,
    "xl/worksheets/sheet2.xml": sheet2,
  });
}

(async () => {
  const sandbox = { DecompressionStream, Blob, Response, TextDecoder, Uint8Array, DataView, Date, Math, isFinite, parseInt, RegExp, String, Array, Object, module: { exports: {} } };
  sandbox.globalThis = sandbox;
  vm.runInNewContext(code, sandbox, { filename: "xlsx.js" });
  const GlassXlsx = sandbox.module.exports;
  check("模块导出 read / supported", typeof GlassXlsx.read === "function" && GlassXlsx.supported() === true);

  const buffer = buildWorkbook();
  const table = await GlassXlsx.read(buffer);

  check("表头正确", table.columns.join(",") === "订单号,区域,数量,金额,下单日期,已付款,备注", table.columns.join(","));
  check("默认读第一个工作表", table.sheet === "一月", table.sheet);
  check("列出全部工作表", table.sheets.join(",") === "一月,二月", table.sheets);
  check("行数正确（第 4 行只有两列也算一行）", table.rows.length === 3, table.rows.length);
  check("共享字符串读取正确", table.rows[0][0] === "A001" && table.rows[0][1] === "华东", table.rows[0]);
  check("整数不带小数点", table.rows[0][2] === "3", table.rows[0][2]);
  check("小数不丢精度", table.rows[0][3] === "128.5", table.rows[0][3]);
  check("日期序列号还原成 YYYY-MM-DD", table.rows[0][4] === "2026-03-01", table.rows[0][4]);
  check("带时间的日期保留时刻", table.rows[1][4] === "2026-03-05 14:30", table.rows[1][4]);
  check("布尔读成 TRUE/FALSE", table.rows[0][5] === "TRUE" && table.rows[1][5] === "FALSE", table.rows[0][5]);
  check("内联字符串可读", table.rows[1][6] === "内联文本", table.rows[1][6]);
  check("参差行按表头宽度补齐", table.rows[2].length === 7 && table.rows[2][6] === "", table.rows[2]);

  const second = await GlassXlsx.read(buffer, { sheet: "二月" });
  check("按名字选工作表", second.rows[0][0] === "B001" && second.sheet === "二月", second.sheet);
  const byIndex = await GlassXlsx.read(buffer, { sheet: 2 });
  check("按序号选工作表（1 起）", byIndex.rows[0][0] === "B001", byIndex.sheet);

  let message = "";
  try {
    await GlassXlsx.read(buffer, { sheet: "不存在" });
  } catch (error) {
    message = error.message;
  }
  check("选错工作表给出可选清单", message.indexOf("二月") >= 0, message);

  let notZip = "";
  try {
    await GlassXlsx.read(new Uint8Array([1, 2, 3, 4]).buffer);
  } catch (error) {
    notZip = error.message;
  }
  check("非 zip 文件给出明确报错", notZip.indexOf("zip") >= 0, notZip);

  /* 与 Python 端对齐的边界：1900 闰年 bug —— 序列号 59 是 1900-02-28，60 是那个不存在的 2 月 29 日 */
  const tricky = makeZip({
    "[Content_Types].xml": "<Types/>",
    "xl/workbook.xml": `<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets><sheet name="S" sheetId="1" r:id="rId1"/></sheets></workbook>`,
    "xl/_rels/workbook.xml.rels": `<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="x" Target="worksheets/sheet1.xml"/></Relationships>`,
    "xl/styles.xml": STYLES,
    "xl/worksheets/sheet1.xml": `<worksheet><sheetData><row r="1"><c r="A1" t="inlineStr"><is><t>日期</t></is></c></row><row r="2"><c r="A2" s="1"><v>59</v></c></row><row r="3"><c r="A3" s="1"><v>61</v></c></row></sheetData></worksheet>`,
  });
  const leap = await GlassXlsx.read(tricky);
  check("1900 闰年 bug 处理正确", leap.rows[0][0] === "1900-02-28" && leap.rows[1][0] === "1900-03-01",
    leap.rows.map((row) => row[0]).join(" | "));

  console.log("");
  if (failures.length) {
    console.log("FAILED: " + failures.join(", "));
    process.exit(1);
  }
  console.log("ALL_XLSX_CHECKS_PASSED");
})().catch((error) => {
  console.log("FAIL 执行异常 :: " + error.stack);
  process.exit(1);
});
