/* 网页版数据清洗 —— 与 Python 端 glassdata/clean.py 同一套规则、同一套日志文案。
 *
 * 为什么要在浏览器里再实现一遍：清洗必须在数据所在的地方做。
 * 把文件传上去洗、洗完再传回来，既不安全也没必要；而两端各写一套、只对齐「结果大致像」，
 * 迟早会出现「桌面版洗掉 3 行，网页版洗掉 2 行」这种没人敢信的情况。
 * 所以这里逐行照着 clean.py 写，判据、顺序、文案都不改，靠 tests/test_clean_parity.js 逐格对拍。
 *
 * 移植时特意对齐的几处「Python 与 JS 不一样」的地方：
 *   1. str.strip() 的空白集合比 JS trim() 多 \x1c-\x1f、\x85，少 \ufeff —— 自己写字符类；
 *   2. round(x, 6) 走 toFixed，而不是 x * 1e6 再取整（后者会把 1.005 变成 1.004999）；
 *   3. parse_number 只去掉半角空格，不动制表符 —— "1\t2" 在 Python 里解析失败，这里也必须失败；
 *   4. 众数并列时 Python 取「值更大」的那个，字符串比较按码位而不是 UTF-16 码元。
 */
var GlassClean = (function () {
  "use strict";

  var FILL_CHOICES = ["none", "drop", "mean", "median", "mode", "ffill", "value"];
  var OUTLIER_CHOICES = ["none", "flag", "drop"];

  var MISSING = ["", "na", "n/a", "nan", "null", "none", "nil", "-", "--", "—", "?", "#n/a"];

  /* 等价于 Python str.strip() 会去掉的那批字符 */
  var PY_SPACE = "\\t\\n\\v\\f\\r \\x1c-\\x1f\\x85\\xa0\\u1680\\u2000-\\u200a\\u2028\\u2029\\u202f\\u205f\\u3000";
  var TRIM_RE = new RegExp("[" + PY_SPACE + "]+$|^[" + PY_SPACE + "]+", "g");

  function pyStrip(value) {
    return String(value === null || value === undefined ? "" : value).replace(TRIM_RE, "");
  }

  function isMissing(value) {
    if (value === null || value === undefined) return true;
    return MISSING.indexOf(pyStrip(value).toLowerCase()) >= 0;
  }

  /* 与 core.parse_number 对齐：千分位、货币符号、会计负数、百分号 */
  function parseNumber(value) {
    if (value === null || value === undefined) return null;
    var text = pyStrip(value);
    if (!text) return null;
    var percent = text.charAt(text.length - 1) === "%";
    if (percent) text = text.slice(0, -1);
    text = text.replace(/[,_ ]/g, "");
    text = text.replace(/[¥￥$€£]/g, "");
    if (text.charAt(0) === "(" && text.charAt(text.length - 1) === ")") {
      text = "-" + text.slice(1, -1);
    }
    if (!/^[+-]?(\d+\.?\d*|\.\d+)([eE][+-]?\d+)?$/.test(text)) return null;
    var number = parseFloat(text);
    if (!isFinite(number)) return null;
    return percent ? number / 100 : number;
  }

  /* Python round(x, 6)：先按真实二进制值四舍五入到 6 位小数。
     不能用 Math.round(x * 1e6) / 1e6 —— 1.005 * 1e6 在浮点里是 1004999.9999999999，
     会被舍成 1.004999，报告里就会出现一个莫名其妙的值。 */
  function roundTo(value, digits) {
    if (value === null || value === undefined || !isFinite(value)) return null;
    if (Math.abs(value) >= 1e21) return value;
    return Number(value.toFixed(digits));
  }

  /* 与 clean._format_number 一致：整数直接输出，其余保留最多 10 位小数并去掉尾零 */
  function formatNumber(value) {
    if (!isFinite(value)) return "0";
    if (Number.isInteger(value) && Math.abs(value) < 1e15) return String(value);
    var text = value.toFixed(10).replace(/0+$/, "").replace(/\.$/, "");
    return text || "0";
  }

  function median(values) {
    var ordered = values.slice().sort(function (a, b) { return a - b; });
    var middle = Math.floor(ordered.length / 2);
    if (ordered.length % 2) return ordered[middle];
    return (ordered[middle - 1] + ordered[middle]) / 2.0;
  }

  /* 按 Unicode 码位比较（Python 的 str 比较规则），避免代理对区域与 UTF-16 码元顺序不一致 */
  function compareCodePoints(left, right) {
    if (left === right) return 0;
    var a = Array.from(left), b = Array.from(right);
    var limit = Math.min(a.length, b.length);
    for (var i = 0; i < limit; i++) {
      var x = a[i].codePointAt(0), y = b[i].codePointAt(0);
      if (x !== y) return x < y ? -1 : 1;
    }
    return a.length < b.length ? -1 : 1;
  }

  /* 众数：并列时取码位更大的那个（Python 的 max(..., key=(次数, 值)) 就是这么定的） */
  function modeOf(rows, index) {
    var counter = Object.create(null), keys = [];
    for (var i = 0; i < rows.length; i++) {
      var cell = index < rows[i].length ? rows[i][index] : "";
      if (isMissing(cell)) continue;
      if (counter[cell] === undefined) { counter[cell] = 0; keys.push(cell); }
      counter[cell]++;
    }
    if (!keys.length) return "";
    var best = keys[0], bestCount = counter[best];
    for (var k = 1; k < keys.length; k++) {
      var key = keys[k], count = counter[key];
      if (count > bestCount || (count === bestCount && compareCodePoints(key, best) > 0)) {
        best = key;
        bestCount = count;
      }
    }
    return best;
  }

  function cellAt(row, index) {
    return index < row.length ? row[index] : "";
  }

  function isBlankRow(row) {
    for (var i = 0; i < row.length; i++) {
      if (pyStrip(row[i]) !== "") return false;
    }
    return true;
  }

  function countMissing(rows) {
    var total = 0;
    for (var i = 0; i < rows.length; i++) {
      var row = rows[i];
      for (var j = 0; j < row.length; j++) if (isMissing(row[j])) total++;
    }
    return total;
  }

  /* 行指纹：长度前缀 + 内容，解析方向唯一，因此不会像 join("\u0001") 那样被内容撞掉 */
  function rowKey(row) {
    var key = "";
    for (var i = 0; i < row.length; i++) {
      var cell = String(row[i]);
      key += cell.length + "\u0002" + cell + "\u0001";
    }
    return key;
  }

  function countDuplicates(rows) {
    var seen = Object.create(null), duplicates = 0;
    for (var i = 0; i < rows.length; i++) {
      var key = rowKey(rows[i]);
      if (seen[key]) duplicates++; else seen[key] = 1;
    }
    return duplicates;
  }

  function stats(columns, rows) {
    return {
      rows: rows.length,
      columns: columns.length,
      missing: countMissing(rows),
      duplicates: countDuplicates(rows),
    };
  }

  /* 够格当数值列吗（非缺失里 95% 以上能解析成数字），顺带返回解析结果 */
  function numericColumn(rows, index) {
    var values = [], total = 0;
    for (var i = 0; i < rows.length; i++) {
      var cell = cellAt(rows[i], index);
      if (isMissing(cell)) continue;
      total++;
      var number = parseNumber(cell);
      if (number !== null) values.push(number);
    }
    if (!total) return { numeric: false, values: [] };
    return { numeric: values.length / total >= 0.95, values: values };
  }

  function normalizeOptions(options) {
    var source = options || {};
    function flag(name, fallback) {
      return source[name] === undefined ? fallback : !!source[name];
    }
    return {
      trim: flag("trim", true),
      drop_blank: flag("drop_blank", true),
      dedupe: flag("dedupe", true),
      coerce: flag("coerce", true),
      fill: FILL_CHOICES.indexOf(source.fill) >= 0 ? source.fill : "none",
      fill_value: source.fill_value === undefined || source.fill_value === null ? "" : String(source.fill_value),
      outliers: OUTLIER_CHOICES.indexOf(source.outliers) >= 0 ? source.outliers : "none",
    };
  }

  /* 给用户看的一句话策略说明，顺序与 Python 一致 */
  function describe(options) {
    var opts = normalizeOptions(options), parts = [];
    if (opts.trim) parts.push("去首尾空白");
    if (opts.drop_blank) parts.push("删空行");
    if (opts.dedupe) parts.push("去重");
    if (opts.coerce) parts.push("数字写法归一");
    if (opts.fill !== "none") {
      parts.push({
        drop: "删除含缺失的行",
        mean: "均值填补",
        median: "中位数填补",
        mode: "众数填补",
        ffill: "前值填补",
        value: "固定值填补",
      }[opts.fill] || opts.fill);
    }
    if (opts.outliers === "flag") parts.push("IQR 标记异常值");
    else if (opts.outliers === "drop") parts.push("IQR 剔除异常值");
    return parts;
  }

  function clean(columns, rows, options) {
    var opts = normalizeOptions(options);
    var work = rows.map(function (row) {
      return row.map(function (cell) {
        return cell === null || cell === undefined ? "" : String(cell);
      });
    });
    var columnsOut = columns.map(function (name) { return String(name); });
    var before = stats(columnsOut, work);
    var steps = [];

    /* 1. 首尾空白（含全角空格、不换行空格）—— 最常见的隐形脏数据 */
    if (opts.trim) {
      var trimmed = 0;
      for (var r = 0; r < work.length; r++) {
        var row = work[r];
        for (var c = 0; c < row.length; c++) {
          var cleaned = row[c].replace(/\u00a0/g, " ").replace(/\u3000/g, " ");
          cleaned = cleaned.replace(TRIM_RE, "");
          if (cleaned !== row[c]) { row[c] = cleaned; trimmed++; }
        }
      }
      steps.push({
        name: "去空白",
        detail: trimmed
          ? "去掉 " + trimmed + " 个单元格的首尾空白（含全角空格、不换行空格）"
          : "没有发现多余空白",
        changed: trimmed,
      });
    }

    /* 2. 整行为空 */
    if (opts.drop_blank) {
      var keptRows = work.filter(function (row) { return !isBlankRow(row); });
      var blankRemoved = work.length - keptRows.length;
      work = keptRows;
      steps.push({
        name: "删空行",
        detail: blankRemoved ? "删除 " + blankRemoved + " 行整行为空的记录" : "没有整行为空的行",
        changed: blankRemoved,
      });
    }

    /* 3. 完全重复行（保留第一次出现的那条） */
    if (opts.dedupe) {
      var seen = Object.create(null), unique = [];
      for (var d = 0; d < work.length; d++) {
        var key = rowKey(work[d]);
        if (seen[key]) continue;
        seen[key] = 1;
        unique.push(work[d]);
      }
      var dupRemoved = work.length - unique.length;
      work = unique;
      steps.push({
        name: "去重",
        detail: dupRemoved ? "删除 " + dupRemoved + " 行完全重复的记录（保留首次出现）" : "没有完全重复的行",
        changed: dupRemoved,
      });
    }

    /* 4. 数字写法归一：1,234 → 1234，¥99.00 → 99，(20) → -20。
       已经是干净写法的（180.50）原样保留——小数位数是业务约定，不是脏数据；
       百分比也不动，把 12% 改写成 0.12 虽然数值等价，但会让人以为数据被改过。 */
    if (opts.coerce) {
      var coerced = 0;
      var PLAIN_NUMBER = /^[+-]?\d+(\.\d+)?$/;
      for (var ci = 0; ci < columnsOut.length; ci++) {
        for (var cr = 0; cr < work.length; cr++) {
          var cell = cellAt(work[cr], ci);
          if (isMissing(cell) || cell.indexOf("%") >= 0 || PLAIN_NUMBER.test(cell)) continue;
          var number = parseNumber(cell);
          if (number === null) continue;
          var text = formatNumber(number);
          if (text !== cell) { work[cr][ci] = text; coerced++; }
        }
      }
      steps.push({
        name: "数字归一",
        detail: coerced ? "把 " + coerced + " 个单元格的数字写法统一（千分位、货币符号、会计负数）" : "数字写法已经统一",
        changed: coerced,
      });
    }

    /* 5. 缺失处理 */
    /* filled_cells 只统计「真的被填上了的格子」：被删掉的空行里也有缺口，
       那些格子并没有被填补，只是随行一起没了，混进来会让人以为填补动了更多数据。 */
    var filledCells = 0;
    if (opts.fill !== "none") {
      var missingBefore = countMissing(work);
      if (opts.fill === "drop") {
        var complete = work.filter(function (row) {
          for (var i = 0; i < row.length; i++) if (isMissing(row[i])) return false;
          return true;
        });
        var missingRemoved = work.length - complete.length;
        work = complete;
        steps.push({
          name: "缺失处理",
          detail: "原有 " + missingBefore + " 格缺失，删除含缺失的 " + missingRemoved + " 行后剩 " + work.length + " 行",
          changed: missingRemoved,
        });
      } else {
        var filled = 0, notes = [];
        for (var fi = 0; fi < columnsOut.length; fi++) {
          var name = columnsOut[fi];
          var holes = [];
          for (var hr = 0; hr < work.length; hr++) {
            if (isMissing(cellAt(work[hr], fi))) holes.push(hr);
          }
          if (!holes.length) continue;
          var probe = numericColumn(work, fi);
          var replacement = null, label = "";
          if ((opts.fill === "mean" || opts.fill === "median") && probe.numeric && probe.values.length) {
            var total = 0;
            for (var vi = 0; vi < probe.values.length; vi++) total += probe.values[vi];
            var value = opts.fill === "mean" ? total / probe.values.length : median(probe.values);
            var rounded = roundTo(value, 6);
            replacement = formatNumber(rounded === null ? 0 : rounded || 0);
            label = opts.fill === "mean" ? "均值" : "中位数";
          } else if (opts.fill === "mode") {
            replacement = modeOf(work, fi);
            label = "众数";
          } else if (opts.fill === "value") {
            replacement = opts.fill_value;
            label = "固定值";
          } else if (opts.fill === "ffill") {
            var last = "";
            for (var fr = 0; fr < work.length; fr++) {
              var cellValue = cellAt(work[fr], fi);
              if (isMissing(cellValue)) {
                if (last) { work[fr][fi] = last; filled++; }
              } else {
                last = cellValue;
              }
            }
            if (filled) notes.push(name + " 用前值填补");
            continue;
          }
          if (replacement === null || replacement === "") {
            if ((opts.fill === "mean" || opts.fill === "median") && !probe.numeric) {
              notes.push(name + " 是文本列，" + holes.length + " 格缺失未填补（可改用「众数」填补）");
            }
            continue;
          }
          for (var h = 0; h < holes.length; h++) {
            work[holes[h]][fi] = replacement;
            filled++;
          }
          notes.push(name + " 用" + label + " " + replacement + " 填补 " + holes.length + " 格");
        }
        steps.push({
          name: "缺失处理",
          detail: notes.length ? notes.join("；") : "没有需要填补的缺失值",
          changed: filled,
        });
        filledCells = filled;
      }
    }

    /* 6. 异常值：IQR 1.5 倍法则。默认只标记，不替你删数据。 */
    if (opts.outliers !== "none") {
      var flagged = Object.create(null), flaggedOrder = [], outlierNotes = [];
      for (var oi = 0; oi < columnsOut.length; oi++) {
        var series = numericColumn(work, oi);
        if (!series.numeric || series.values.length < 8) continue;
        var ordered = series.values.slice().sort(function (a, b) { return a - b; });
        var q1 = ordered[Math.floor(ordered.length * 0.25)];
        var q3 = ordered[Math.floor(ordered.length * 0.75)];
        var span = q3 - q1;
        if (span <= 0) continue;
        var low = q1 - 1.5 * span, high = q3 + 1.5 * span;
        var hits = [];
        for (var hr2 = 0; hr2 < work.length; hr2++) {
          var raw = cellAt(work[hr2], oi);
          if (isMissing(raw)) continue;
          var parsed = parseNumber(raw);
          if (parsed === null) continue;
          if (parsed < low || parsed > high) hits.push(hr2);
        }
        if (hits.length) {
          flagged[oi] = hits;
          flaggedOrder.push(oi);
          var lowRounded = roundTo(low, 2), highRounded = roundTo(high, 2);
          outlierNotes.push(columnsOut[oi] + " 有 " + hits.length + " 个超出 [" +
            formatNumber(lowRounded === null ? 0 : lowRounded || 0) + ", " +
            formatNumber(highRounded === null ? 0 : highRounded || 0) + "]");
        }
      }

      var flaggedTotal = 0;
      for (var fk = 0; fk < flaggedOrder.length; fk++) flaggedTotal += flagged[flaggedOrder[fk]].length;

      if (opts.outliers === "drop" && flaggedOrder.length) {
        var dropSet = Object.create(null), dropped = 0;
        for (var di = 0; di < flaggedOrder.length; di++) {
          var positions = flagged[flaggedOrder[di]];
          for (var dp = 0; dp < positions.length; dp++) {
            if (!dropSet[positions[dp]]) { dropSet[positions[dp]] = 1; dropped++; }
          }
        }
        work = work.filter(function (row, position) { return !dropSet[position]; });
        outlierNotes.push("已删除 " + dropped + " 行含极端值的记录");
      } else if (opts.outliers === "flag" && flaggedOrder.length) {
        flaggedOrder.sort(function (a, b) { return a - b; });
        for (var mk = 0; mk < flaggedOrder.length; mk++) {
          var index = flaggedOrder[mk];
          var markSet = Object.create(null);
          for (var mp = 0; mp < flagged[index].length; mp++) markSet[flagged[index][mp]] = 1;
          columnsOut.push(columnsOut[index] + "_异常");
          for (var mr = 0; mr < work.length; mr++) work[mr].push(markSet[mr] ? "是" : "否");
        }
        outlierNotes.push("已追加「_异常」标记列（仅标记，不删除）");
      }

      steps.push({
        name: "异常值",
        detail: outlierNotes.length ? outlierNotes.join("；") : "未发现 IQR 意义上的极端值",
        changed: flaggedTotal,
      });
    }

    var after = stats(columnsOut, work);
    var report = {
      options: describe(opts),
      steps: steps,
      before: before,
      after: after,
      removed_rows: Math.max(0, before.rows - after.rows),
      filled_cells: filledCells,
      columns_added: after.columns - before.columns,
    };
    return { columns: columnsOut, rows: work, report: report };
  }

  /* 导出 CSV：与 csv.writer 的 QUOTE_MINIMAL + \r\n 一致。
     utf-8-sig（BOM）由调用方加，因为写文件与下载 Blob 的编码方式不同。 */
  /* CSV 公式注入（CWE-1236）——与 Python 端 clean.py 的 csv_neutralize 逐字对应。
     单元格以 = + - @ 或制表符/回车开头时，Excel 会当公式执行
     （`=cmd|'/C calc'!A0` 直接起计算器）。导出层加一个单引号前缀把它打成文本，
     Excel 显示时连引号都不显示；纯数字豁免，否则 -1200 会变成文本。 */
  var CSV_TRIGGERS = "=+-@\t\r";

  /* 与 Python 端 _NUMBER_TEXT 同一条正则：只认十进制写法。
     Number() 会接受 0x10，float() 会接受 1_0 —— 两者天然不一致，不能用。 */
  var NUMBER_TEXT = /^[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?$/;

  function isNumberText(text) {
    return !!text && NUMBER_TEXT.test(text);
  }

  function csvNeutralize(text) {
    if (!text || CSV_TRIGGERS.indexOf(text.charAt(0)) < 0) return null;
    if (isNumberText(text)) return null;
    return "'" + text;
  }

  function toCsv(columns, rows, delimiter, stats) {
    var sep = delimiter || ",";
    var escaped = 0;
    function field(value) {
      var text = value === null || value === undefined ? "" : String(value);
      var guarded = csvNeutralize(text);
      if (guarded !== null) {
        escaped += 1;
        text = guarded;
      }
      if (text.indexOf(sep) >= 0 || text.indexOf('"') >= 0 || /[\r\n]/.test(text)) {
        return '"' + text.replace(/"/g, '""') + '"';
      }
      return text;
    }
    var lines = [columns.map(field).join(sep)];
    for (var i = 0; i < rows.length; i++) lines.push(rows[i].map(field).join(sep));
    if (stats && typeof stats === "object") {
      stats.escaped = escaped;
      stats.rows = rows.length;
    }
    return lines.join("\r\n") + "\r\n";
  }

  return {
    clean: clean,
    describe: describe,
    toCsv: toCsv,
    csvNeutralize: csvNeutralize,
    isMissing: isMissing,
    parseNumber: parseNumber,
    formatNumber: formatNumber,
    FILL_CHOICES: FILL_CHOICES,
    OUTLIER_CHOICES: OUTLIER_CHOICES,
  };
})();

if (typeof module !== "undefined" && module.exports) module.exports = GlassClean;
