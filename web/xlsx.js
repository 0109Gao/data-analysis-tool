/* 网页版读取 .xlsx / .xlsm —— 不引入任何第三方库。
 *
 * 用的都是浏览器自带能力：
 *   - 解压：DecompressionStream("deflate-raw")（Chrome 80+ / Edge / Safari 16.4+ / Firefox 113+）
 *   - 解析：字符串扫描，而不是 DOMParser
 *
 * 为什么不用 DOMParser：同一份代码要能在 Node 里跑测试。浏览器用 DOM、测试用另一套解析器，
 * 两边一定会分叉——这个项目已经在「前端手写 CSV 解析」上吃过一次这个亏（靠逐格差分测试才抓回来）。
 * xlsx 里的 XML 是 Excel 自己生成的，结构规整，按标签扫描足够稳。
 *
 * 与 Python 端 glassdata/excel.py 保持同一套规则：共享字符串、内联字符串、布尔、错误值、
 * 日期序列号（含 1900 闰年 bug 与 1904 基准）、多工作表。两边的行为由 test_xlsx.js 盯住。
 */
var GlassXlsx = (function () {
  "use strict";

  /* 「自动挑工作表」的三条提示 + 单列表的行数界限。
     与 Python 端 excel.py 的同名常量逐字一致，差分测试会比对这两边的文案。 */
  var NARROW_ROWS = 20;

  function NARROW_NOTE(narrow, picked) {
    return "工作表「" + narrow + "」只有一列、行数很少，更像说明文字，已自动改用「" + picked + "」";
  }

  function EMPTY_NOTE(skipped, picked) {
    return "工作表「" + skipped + "」没有可分析的数据，已自动改用「" + picked + "」";
  }

  function LAST_RESORT_NOTE(narrow) {
    return "工作簿里没有更像数据表的工作表，已使用只有一列的「" + narrow + "」";
  }

  function unescapeXml(text) {
    if (text.indexOf("&") < 0) return text;
    return text.replace(/&(lt|gt|amp|quot|apos|#\d+|#x[0-9a-fA-F]+);/g, function (whole, body) {
      if (body === "lt") return "<";
      if (body === "gt") return ">";
      if (body === "amp") return "&";
      if (body === "quot") return '"';
      if (body === "apos") return "'";
      if (body.charAt(0) === "#") {
        var code = body.charAt(1) === "x" || body.charAt(1) === "X"
          ? parseInt(body.slice(2), 16)
          : parseInt(body.slice(1), 10);
        return isFinite(code) ? String.fromCodePoint(code) : whole;
      }
      return whole;
    });
  }

  function textOfBytes(bytes) {
    return new TextDecoder("utf-8").decode(bytes);
  }

  /* ---------------- zip ---------------- */

  /* 接受 ArrayBuffer、Uint8Array 或 Buffer（Node 里 FileReader 是模拟的） */
  function toArrayBuffer(input) {
    if (ArrayBuffer.isView(input)) {
      return input.buffer.slice(input.byteOffset, input.byteOffset + input.byteLength);
    }
    return input;
  }

  function openArchive(input) {
    var buffer = toArrayBuffer(input);
    var bytes = new Uint8Array(buffer);
    var view = new DataView(buffer);
    var eocd = -1;
    var floor = Math.max(0, bytes.length - 66000);
    for (var i = bytes.length - 22; i >= floor; i--) {
      if (view.getUint32(i, true) === 0x06054b50) { eocd = i; break; }
    }
    if (eocd < 0) throw new Error("这不是一个 zip 容器（可能是加密文件或被截断）");
    var count = view.getUint16(eocd + 10, true);
    var cursor = view.getUint32(eocd + 16, true);
    var entries = {};
    for (var index = 0; index < count; index++) {
      if (view.getUint32(cursor, true) !== 0x02014b50) break;
      var method = view.getUint16(cursor + 10, true);
      var compressed = view.getUint32(cursor + 20, true);
      var nameLength = view.getUint16(cursor + 28, true);
      var extraLength = view.getUint16(cursor + 30, true);
      var commentLength = view.getUint16(cursor + 32, true);
      var localOffset = view.getUint32(cursor + 42, true);
      var name = textOfBytes(bytes.subarray(cursor + 46, cursor + 46 + nameLength));
      entries[name] = { method: method, compressed: compressed, local: localOffset };
      cursor += 46 + nameLength + extraLength + commentLength;
    }
    return { bytes: bytes, view: view, entries: entries };
  }

  /* 解压预算：单个部件解压后的上限。与 Python 端 excel.py 的 PART_LIMIT 一致。
     没有这道闸，一个 300 KB 的压缩炸弹能把标签页直接撑爆——
     浏览器不像命令行，OOM 就是整页白屏，用户连「文件有问题」都看不到。 */
  var PART_LIMIT = 512 * 1024 * 1024;

  /* Excel 的硬上限：列 A..XFD。伪造的 r="AAAAAAAA1" 会算出 83 亿列，
     下面按 width 建数组当场把标签页撑爆——正常软件写不出这种坐标。 */
  var MAX_COLUMNS = 16384;

  /* 「换一张表再试试」只对「这张表没数据」有意义。
     文件结构不可信、解压超标、出现 DTD 这类错误换表也没用——
     继续试下一张只会把真正的原因埋进「N 个工作表都没有可分析的数据」里。 */
  function fatal(message) {
    var error = new Error(message);
    error.fatal = true;
    return error;
  }

  /* 与 Python 端 _size_text 同一口径：小于 1 MB 时说 KB，别四舍五入成 0 MB。 */
  function sizeText(limit) {
    if (limit >= 1048576) return Math.max(1, Math.round(limit / 1048576)) + " MB";
    return Math.max(1, Math.round(limit / 1024)) + " KB";
  }

  async function inflateRaw(bytes, limit, what) {
    if (typeof DecompressionStream !== "function") {
      throw fatal("这个浏览器不支持 DecompressionStream，无法解压 Excel（请用较新的 Chrome / Edge / Safari）");
    }
    var stream = new Blob([bytes]).stream().pipeThrough(new DecompressionStream("deflate-raw"));
    var reader = stream.getReader();
    var chunks = [];
    var seen = 0;
    for (;;) {
      var step = await reader.read();
      if (step.done) break;
      seen += step.value.length;
      /* 逐块计数：内存只跟「已确认合法」的部分有关，超了立刻停，
         不去等整份解压完——那正是炸弹要的效果。 */
      if (seen > limit) {
        try { await reader.cancel(); } catch (error) { /* 取消失败不影响结论 */ }
        throw fatal(
          "「" + what + "」解压后超过 " + sizeText(limit) +
          "，已停止读取。正常的表格不会有这么大，这更像是压缩炸弹。"
        );
      }
      chunks.push(step.value);
    }
    var out = new Uint8Array(seen);
    var at = 0;
    for (var index = 0; index < chunks.length; index++) {
      out.set(chunks[index], at);
      at += chunks[index].length;
    }
    return out;
  }

  async function entryBytes(archive, name) {
    var entry = archive.entries[name];
    if (!entry) return null;
    var view = archive.view;
    var start = entry.local;
    if (view.getUint32(start, true) !== 0x04034b50) return null;
    var nameLength = view.getUint16(start + 26, true);
    var extraLength = view.getUint16(start + 28, true);
    var from = start + 30 + nameLength + extraLength;
    var raw = archive.bytes.subarray(from, from + entry.compressed);
    if (entry.method === 0) return raw;
    if (entry.method !== 8) throw fatal("Excel 内部用了不认识的压缩方式（" + entry.method + "）");
    return inflateRaw(raw, archive.limit || PART_LIMIT, name);
  }

  async function entryText(archive, name) {
    var bytes = await entryBytes(archive, name);
    if (!bytes) return null;
    var text = textOfBytes(bytes);
    /* 正常的工作簿不会有 DTD。出现它就只有一个目的：借实体展开把体积放大
       （billion laughs），或借外部实体读本机文件（XXE）。
       下面的解析器本来就不展开实体，但这道闸让拒绝发生在解析之前，也更明确。 */
    if (text.indexOf("<!DOCTYPE") >= 0 || text.indexOf("<!ENTITY") >= 0) {
      throw fatal(
        "「" + name + "」里出现了 <!DOCTYPE，这不是正常的表格文件。" +
        "为防实体展开攻击（billion laughs / XXE），已停止解析。"
      );
    }
    return text;
  }

  /* ---------------- workbook.xml / rels ---------------- */

  function attr(tag, name) {
    var match = new RegExp("(?:^|\\s)" + name + '="([^"]*)"').exec(tag);
    return match ? unescapeXml(match[1]) : "";
  }

  function sheetsOf(workbookXml, relsXml) {
    var rels = {};
    var relPattern = /<Relationship\b[^>]*>/g;
    var relMatch;
    while ((relMatch = relPattern.exec(relsXml))) {
      var target = attr(relMatch[0], "Target");
      if (target.charAt(0) === "/") target = target.slice(1);
      else if (target.indexOf("xl/") !== 0) target = "xl/" + target;
      rels[attr(relMatch[0], "Id")] = target;
    }
    var sheets = [];
    var pattern = /<sheet\b[^>]*>/g;
    var match;
    var fallback = 1;
    while ((match = pattern.exec(workbookXml))) {
      var tag = match[0];
      var rid = attr(tag, "[\\w.-]+:id");
      var path = rels[rid] || "xl/worksheets/sheet" + fallback + ".xml";
      sheets.push({ name: attr(tag, "name") || "Sheet" + fallback, path: path });
      fallback += 1;
    }
    return sheets;
  }

  function pickSheet(sheets, wanted) {
    if (sheets.length === 0) throw new Error("工作簿里没有任何工作表");
    if (wanted === undefined || wanted === null || wanted === "") return 0;
    var text = String(wanted).trim();
    if (/^\d+$/.test(text)) {
      var index = parseInt(text, 10);
      if (index >= 1 && index <= sheets.length) return index - 1;
      throw new Error("工作表序号超出范围：共 " + sheets.length + " 个");
    }
    for (var i = 0; i < sheets.length; i++) {
      if (sheets[i].name === text) return i;
    }
    throw new Error("找不到工作表「" + text + "」，可选：" + sheets.map(function (s) { return s.name; }).join("、"));
  }

  /* ---------------- sharedStrings / styles ---------------- */

  function collectText(fragment) {
    // 跳过 rPh（日文注音），其余 <t> 依次拼起来
    var clean = fragment.replace(/<rPh\b[\s\S]*?<\/rPh>/g, "");
    var pattern = /<t\b[^>]*>([\s\S]*?)<\/t>/g;
    var out = [];
    var match;
    while ((match = pattern.exec(clean))) out.push(unescapeXml(match[1]));
    return out.join("");
  }

  function sharedStrings(xml) {
    if (!xml) return [];
    var list = [];
    var pattern = /<si\b[^>]*(?:\/>|>([\s\S]*?)<\/si>)/g;
    var match;
    while ((match = pattern.exec(xml))) list.push(match[1] ? collectText(match[1]) : "");
    return list;
  }

  var BUILTIN_DATE = {};
  var BUILTIN_TIME = {};
  (function () {
    var i;
    for (i = 14; i <= 22; i++) BUILTIN_DATE[i] = "date";
    [27, 28, 29, 30, 31, 32, 33, 34, 35, 36].forEach(function (id) { BUILTIN_DATE[id] = "date"; });
    [18, 19, 20, 21, 45, 46, 47].forEach(function (id) { BUILTIN_TIME[id] = "time"; });
    [22].forEach(function (id) { BUILTIN_DATE[id] = "datetime"; });
    [45, 46, 47].forEach(function (id) { BUILTIN_DATE[id] = "datetime"; });
    for (i = 50; i <= 58; i++) BUILTIN_DATE[i] = "datetime";
  })();

  function classifyFormat(id, code) {
    if (BUILTIN_TIME[id] === "time") return "time";
    if (BUILTIN_DATE[id]) return BUILTIN_DATE[id];
    if (!code) return null;
    var cleaned = code.replace(/\[[^\]]*\]/g, "").replace(/"[^"]*"/g, "").replace(/\\[^\s]/g, "").toLowerCase();
    var hasDate = cleaned.indexOf("y") >= 0 || cleaned.indexOf("d") >= 0;
    var hasTime = cleaned.indexOf("h") >= 0 || cleaned.indexOf("s") >= 0;
    if (hasDate && hasTime) return "datetime";
    if (hasDate) return "date";
    if (hasTime) return "time";
    return null;
  }

  function styleKinds(xml) {
    var kinds = {};
    if (!xml) return kinds;
    var custom = {};
    var fmtPattern = /<numFmt\b[^>]*>/g;
    var fmtMatch;
    while ((fmtMatch = fmtPattern.exec(xml))) {
      custom[attr(fmtMatch[0], "numFmtId")] = attr(fmtMatch[0], "formatCode");
    }
    var block = /<cellXfs\b[^>]*>([\s\S]*?)<\/cellXfs>/.exec(xml);
    if (!block) return kinds;
    var xfPattern = /<xf\b[^>]*>/g;
    var xfMatch;
    var index = 0;
    while ((xfMatch = xfPattern.exec(block[1]))) {
      var id = attr(xfMatch[0], "numFmtId") || "0";
      var kind = classifyFormat(id, custom[id]);
      if (kind) kinds[index] = kind;
      index += 1;
    }
    return kinds;
  }

  /* ---------------- 数字 / 日期 ---------------- */

  function formatNumber(value) {
    if (!isFinite(value)) return "";
    if (Number.isInteger(value) && Math.abs(value) < 1e15) return String(value);
    var text = value.toFixed(10).replace(/0+$/, "").replace(/\.$/, "");
    return text || "0";
  }

  function pad(number) {
    return (number < 10 ? "0" : "") + number;
  }

  function formatSerial(value, date1904, kind) {
    if (value < 0) return formatNumber(value);
    var base = date1904 ? Date.UTC(1904, 0, 1) : Date.UTC(1899, 11, 30);
    // Excel 沿用了 Lotus 的 bug：1900 被当成闰年，序列号 60 是不存在的 2 月 29 日
    if (!date1904 && value < 60) base = Date.UTC(1899, 11, 31);
    var days = Math.floor(value);
    var seconds = Math.round((value - days) * 86400);
    var moment = new Date(base + days * 86400000 + seconds * 1000);
    var ymd = moment.getUTCFullYear() + "-" + pad(moment.getUTCMonth() + 1) + "-" + pad(moment.getUTCDate());
    var hm = pad(moment.getUTCHours()) + ":" + pad(moment.getUTCMinutes());
    if (kind === "time") return hm + ":" + pad(moment.getUTCSeconds());
    if (seconds % 86400 === 0) return ymd;
    if (seconds % 60 === 0) return ymd + " " + hm;
    return ymd + " " + hm + ":" + pad(moment.getUTCSeconds());
  }

  function columnIndex(ref) {
    var index = 0;
    for (var i = 0; i < ref.length; i++) {
      var code = ref.charCodeAt(i);
      if (code >= 65 && code <= 90) index = index * 26 + (code - 64);
      else if (code >= 97 && code <= 122) index = index * 26 + (code - 96);
      else break;
    }
    return index - 1;
  }
  /* ---------------- 单元格 ---------------- */

  function cellText(tag, inner, shared, kinds, date1904) {
    var type = attr(tag, "t") || "n";
    if (type === "inlineStr") {
      var inline = /<is\b[^>]*>([\s\S]*?)<\/is>/.exec(inner || "");
      return inline ? collectText(inline[1]) : "";
    }
    var valueMatch = /<v\b[^>]*>([\s\S]*?)<\/v>/.exec(inner || "");
    if (!valueMatch) return "";
    var raw = unescapeXml(valueMatch[1]);
    if (type === "s") {
      var sharedIndex = parseInt(raw, 10);
      return shared[sharedIndex] === undefined ? "" : shared[sharedIndex];
    }
    if (type === "b") return raw === "1" || raw.toLowerCase() === "true" ? "TRUE" : "FALSE";
    if (type === "str" || type === "e") return raw;
    var number = parseFloat(raw);
    if (!isFinite(number)) return raw;
    var style = attr(tag, "s");
    if (style && kinds[parseInt(style, 10)]) return formatSerial(number, date1904, kinds[parseInt(style, 10)]);
    return formatNumber(number);
  }

  function rowsOfSheet(xml, shared, kinds, date1904) {
    var rows = [];
    /* 物理行号要一起带出去：<row r="5"/> 这种空行不会被收进来，
       丢掉之后「第 0 行有合并」的判断就会错位，标题行也就认不出来。 */
    var physical = [];
    var running = 0;
    var rowPattern = /<row\b[^>]*(?:\/>|>([\s\S]*?)<\/row>)/g;
    var rowMatch;
    while ((rowMatch = rowPattern.exec(xml))) {
      var openTag = /^<row\b[^>]*/.exec(rowMatch[0])[0];
      var declared = /(?:^|\s)r="(\d+)"/.exec(openTag);
      var rowNumber = declared ? parseInt(declared[1], 10) : running + 1;
      running = rowNumber;
      var inner = rowMatch[1] || "";
      if (!inner) continue;
      var cells = {};
      var width = 0;
      var position = 0;
      var cellPattern = /<c\b([^>]*?)(?:\/>|>([\s\S]*?)<\/c>)/g;
      var cellMatch;
      while ((cellMatch = cellPattern.exec(inner))) {
        var tag = "<c" + cellMatch[1] + ">";
        var ref = attr(tag, "r");
        var index = ref ? columnIndex(ref) : position;
        if (index >= MAX_COLUMNS) {
          throw fatal(
            "单元格坐标「" + ref + "」超出了 Excel 的列上限（A..XFD），" +
            "这份文件的结构不可信，已停止读取。"
          );
        }
        cells[index] = cellText(tag, cellMatch[2], shared, kinds, date1904);
        position = index + 1;
        if (index + 1 > width) width = index + 1;
      }
      if (width === 0) continue;
      var row = [];
      for (var i = 0; i < width; i++) row.push(cells[i] === undefined ? "" : cells[i]);
      rows.push(row);
      physical.push(rowNumber - 1);
    }
    return { rows: rows, physical: physical };
  }

  function mergeRanges(xml) {
    /* 合并区域在 sheetData 之后，形如 <mergeCell ref="A1:C1"/> */
    var result = [];
    var pattern = /<mergeCell\b[^>]*\bref="([^"]+)"/g;
    var match;
    while ((match = pattern.exec(xml))) {
      var parts = match[1].split(":");
      if (parts.length !== 2) continue;
      var start = refCell(parts[0]);
      var end = refCell(parts[1]);
      if (!start || !end) continue;
      result.push([
        Math.min(start[0], end[0]), Math.min(start[1], end[1]),
        Math.max(start[0], end[0]), Math.max(start[1], end[1]),
      ]);
    }
    return result;
  }

  function refCell(ref) {
    var match = /^([A-Za-z]+)(\d+)$/.exec(String(ref).trim());
    if (!match) return null;
    return [parseInt(match[2], 10) - 1, columnIndex(match[1])];
  }

  /* 与 Python 端 _looks_like_header 同口径：非空单元格里至少六成不是数字 */
  function looksLikeHeader(cells) {
    var filled = cells.filter(function (cell) { return String(cell).trim() !== ""; });
    if (!filled.length) return false;
    var text = filled.filter(function (cell) { return !isNumericText(cell); }).length;
    return text * 10 >= filled.length * 6;
  }

  function isNumericText(value) {
    var text = String(value).trim();
    if (!text) return false;
    if (/^\(.*\)$/.test(text)) text = "-" + text.slice(1, -1);
    text = text.replace(/[¥$€£,%\s]/g, "");
    if (!text) return false;
    return isFinite(Number(text));
  }

  function fillRowMerges(row, spans) {
    var filled = row.slice();
    for (var i = 0; i < spans.length; i++) {
      var c1 = spans[i][1];
      var c2 = spans[i][3];
      if (c1 >= filled.length) continue;
      var value = filled[c1];
      for (var column = c1; column <= Math.min(c2, filled.length - 1); column++) filled[column] = value;
    }
    return filled;
  }

  /* 与 Python 端 _header_rows 一一对应：标题行跳过、两级表头拼成「父-子」 */
  function headerRows(rows, merges, notes) {
    var head = rows[0];
    if (rows.length < 2) return { header: head, start: 1 };
    var tail = rows[1];
    var spans = merges.filter(function (span) { return span[0] === 0 && span[3] > span[1]; });
    if (!spans.length && head.length <= 1) {
      // 只有一个格子又没有合并信息：可能是标题行，也可能是正常单列，交给下面的判断
    } else if (!spans.length) {
      return { header: head, start: 1 };
    }
    var values = {};
    var count = 0;
    for (var i = 0; i < head.length; i++) {
      var text = String(head[i]).trim();
      if (text && values[text] === undefined) { values[text] = true; count += 1; }
    }
    var tailFilled = tail.filter(function (cell) { return String(cell).trim() !== ""; });
    if (!looksLikeHeader(tail) || tailFilled.length < 2) return { header: head, start: 1 };

    if (count <= 1) {
      var title = "";
      for (var key in values) { title = key; break; }
      notes.push("已跳过开头的「" + title + "」标题行（合并单元格），改用下一行作表头");
      return { header: tail, start: 2 };
    }
    if (spans.length) {
      var parents = fillRowMerges(head, spans);
      var combined = tail.map(function (child, index) {
        var parent = index < parents.length ? String(parents[index]).trim() : "";
        var kid = String(child).trim();
        if (parent && kid && parent !== kid) return parent + "-" + kid;
        return kid || parent;
      });
      notes.push("检测到两级表头，已合并为「父-子」形式（如 销售-金额）");
      return { header: combined, start: 2 };
    }
    return { header: head, start: 1 };
  }

  function assemble(rows, merges, notes, physical) {
    var flags = rows.map(function (row) {
      return row.some(function (cell) { return String(cell).trim() !== ""; });
    });
    if (!physical || physical.length !== rows.length) {
      physical = rows.map(function (row, index) { return index; });
    }
    var kept = [];
    var keptPhysical = [];
    for (var i = 0; i < rows.length; i++) {
      if (!flags[i]) continue;
      kept.push(rows[i]);
      keptPhysical.push(physical[i]);
    }
    if (kept.length === 0) throw new Error("工作表是空的：没有表头也没有数据");
    /* 合并区域记的是物理行号，空行删掉后会前移；表头只看前几行，建个小映射就够 */
    var lookup = {};
    for (var j = 0; j < Math.min(keptPhysical.length, 8); j++) lookup[keptPhysical[j]] = j;
    var shifted = [];
    merges.forEach(function (span) {
      var start = lookup[span[0]];
      if (start === undefined) return;
      shifted.push([start, span[1], start, span[3]]);
    });
    var resolved = headerRows(kept, shifted, notes);
    var header = resolved.header.map(function (cell) { return String(cell).trim(); });
    while (header.length && header[header.length - 1] === "") header.pop();
    if (header.length === 0) throw new Error("工作表的第一行是空的，无法作为表头");
    // 表头去重（与 Python 端 _unique_headers 同规则）
    var seen = {};
    header = header.map(function (name, index) {
      var clean = name || "col_" + (index + 1);
      if (seen[clean] === undefined) { seen[clean] = 0; return clean; }
      seen[clean] += 1;
      return clean + "_" + seen[clean];
    });
    var body = kept.slice(resolved.start).map(function (row) {
      var out = [];
      for (var i = 0; i < header.length; i++) out.push(row[i] === undefined ? "" : String(row[i]));
      return out;
    });
    if (body.length === 0) throw new Error("工作表只有表头，没有数据行");
    return { columns: header, rows: body };
  }

  async function read(buffer, options) {
    options = options || {};
    var archive = openArchive(buffer);
    /* 解压预算只允许往下调，不允许往上抬——测试要能用一个几 KB 的炸弹验证闸门，
       但不该有人能把它关掉。 */
    archive.limit = Math.min(options.maxPartBytes || PART_LIMIT, PART_LIMIT);
    var workbook = await entryText(archive, "xl/workbook.xml");
    if (!workbook) throw new Error("这不是 Excel 工作簿（缺少 xl/workbook.xml）");
    var rels = (await entryText(archive, "xl/_rels/workbook.xml.rels")) || "";
    var sheets = sheetsOf(workbook, rels);
    var shared = sharedStrings(await entryText(archive, "xl/sharedStrings.xml"));
    var kinds = styleKinds(await entryText(archive, "xl/styles.xml"));
    var date1904 = /date1904="(1|true)"/.test(workbook);
    var names = sheets.map(function (item) { return item.name; });
    var notes = [];

    async function load(index) {
      var xml = await entryText(archive, sheets[index].path);
      if (!xml) throw fatal("工作簿里找不到工作表文件：" + sheets[index].path);
      var local = [];
      var sheetData = rowsOfSheet(xml, shared, kinds, date1904);
      var table = assemble(sheetData.rows, mergeRanges(xml), local, sheetData.physical);
      return { table: table, notes: local };
    }

    if (options.sheet) {
      var chosen = pickSheet(sheets, options.sheet);
      var single = await load(chosen);
      return {
        columns: single.table.columns, rows: single.table.rows,
        sheet: names[chosen], sheets: names, notes: single.notes,
      };
    }
    /* 不指定工作表：跳过封面页，用第一张「像数据表」的。
       单列且行数很少的表先留着兜底——封面/说明页正好长这样，
       否则它会被当成只有一列的数据表返回，真正的数据表反而读不到。 */
    var tried = [];
    var narrow = null;
    var narrowName = "";
    for (var index = 0; index < sheets.length; index++) {
      try {
        var loaded = await load(index);
      } catch (error) {
        if (error && error.fatal) throw error;
        tried.push(error.message);
        continue;
      }
      var width = loaded.table.columns.length;
      if (sheets.length > 1 && width < 2 && loaded.table.rows.length <= NARROW_ROWS) {
        if (!narrow) {
          narrow = loaded;
          narrowName = names[index];
        }
        continue;
      }
      notes = loaded.notes.slice();
      if (narrow) notes.push(NARROW_NOTE(narrowName, names[index]));
      else if (index) notes.push(EMPTY_NOTE(names[0], names[index]));
      return {
        columns: loaded.table.columns, rows: loaded.table.rows,
        sheet: names[index], sheets: names, notes: notes,
      };
    }
    if (narrow) {
      notes = narrow.notes.slice();
      notes.push(LAST_RESORT_NOTE(narrowName));
      return {
        columns: narrow.table.columns, rows: narrow.table.rows,
        sheet: narrowName, sheets: names, notes: notes,
      };
    }
    throw new Error("工作簿里的 " + sheets.length + " 个工作表都没有可分析的数据（表头 + 至少一行数据）：" + tried.join("；"));
  }

  function supported() {
    return typeof DecompressionStream === "function" && typeof Blob === "function";
  }

  return { read: read, supported: supported };
})();

if (typeof module !== "undefined" && module.exports) module.exports = GlassXlsx;
