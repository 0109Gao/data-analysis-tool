/* 网页版清洗引擎自检：逐条验证 glassdata/clean.js 的行为。
 *
 * 这里测「规则对不对」，parity 测试（tests/test_clean_parity.py）测「两端是否一模一样」。
 * 两者都需要：parity 只能说明两边一致，不能说明两边一致地错了。
 */
const fs = require("fs");
const path = require("path");

const SRC = path.join(__dirname, "..", "glassdata", "clean.js");
const code = fs.readFileSync(SRC, "utf8");
const sandbox = { module: { exports: {} }, console };
const fn = new Function("module", "exports", "console", code + "\nreturn GlassClean;");
const GlassClean = fn(sandbox.module, sandbox.module.exports, console);

const failures = [];
function check(name, condition, detail) {
  if (condition) console.log("  ok   " + name);
  else {
    failures.push(name);
    console.log("  FAIL " + name + (detail !== undefined ? " :: " + JSON.stringify(detail) : ""));
  }
}

(function main() {
  /* ---------- A. 基础判定 ---------- */
  check("识别常见缺失写法", ["", "NA", " null ", "—", "?"].every(GlassClean.isMissing));
  check("识别不换行空格为缺失判定依据", GlassClean.isMissing("\u00a0"), "NBSP 应被 strip 掉后当作空");
  check("数字解析支持千分位与货币", GlassClean.parseNumber("¥1,234.5") === 1234.5);
  check("数字解析支持会计负数", GlassClean.parseNumber("(20)") === -20);
  check("数字解析支持百分号", GlassClean.parseNumber("12%") === 0.12);
  check("数字解析不认制表符分隔", GlassClean.parseNumber("1\t2") === null);
  check("数字格式化整数不带小数点", GlassClean.formatNumber(1234.0) === "1234");
  check("数字格式化保留有效小数", GlassClean.formatNumber(1.5) === "1.5");

  /* ---------- B. 默认策略只做无损动作 ---------- */
  const src = {
    columns: ["名称", "金额", "备注"],
    rows: [
      ["\u00a0甲 ", "1,000", ""],
      ["   ", "", ""],
      ["乙", "¥99.00", "x"],
      ["乙", "¥99.00", "x"],
      ["丙", "180.50", "12%"],
    ],
  };
  const plain = GlassClean.clean(src.columns, src.rows, {});
  // 5 行 → 删掉 1 行空行 → 乙 重复 1 行 → 剩 3 行
  check("默认删掉空行与重复行", plain.rows.length === 3, plain.rows.length);
  check("默认去掉首尾空白", plain.rows[0][0] === "甲", plain.rows[0][0]);
  check("默认去掉千分位", plain.rows[0][1] === "1000", plain.rows[0][1]);
  check("默认去掉货币符号", plain.rows[1][1] === "99", plain.rows[1][1]);
  check("干净小数位原样保留", plain.rows[2][1] === "180.50", plain.rows[2][1]);
  check("百分比不被改写", plain.rows[2][2] === "12%", plain.rows[2][2]);
  check("默认不填补缺失", plain.rows[0][2] === "", plain.rows[0][2]);
  check("默认不加标记列", plain.columns.length === 3, plain.columns.length);

  /* ---------- C. 全关 = 一个字节都不动 ---------- */
  const off = GlassClean.clean(src.columns, src.rows, {
    trim: false, drop_blank: false, dedupe: false, coerce: false,
  });
  check("全部关掉后行数不变", off.rows.length === src.rows.length, off.rows.length);
  check("全部关掉后空格仍在", off.rows[0][0] === "\u00a0甲 ", off.rows[0][0]);
  check("全部关掉后没有步骤记录", off.report.steps.length === 0, off.report.steps.length);

  /* ---------- D. 去重保留首次出现 ---------- */
  const dup = GlassClean.clean(["k"], [["a"], ["b"], ["a"], ["c"], ["b"]], {});
  check("去重后剩 3 行", dup.rows.length === 3, dup.rows.length);
  check("去重保留首次出现顺序", dup.rows.map((r) => r[0]).join("") === "abc", dup.rows);

  /* ---------- E. 缺失填补 ---------- */
  const holes = {
    columns: ["num", "cat"],
    rows: [["10", "官网"], ["", "门店"], ["30", ""], ["", "官网"], ["50", "门店"], ["", ""]],
  };
  const mean = GlassClean.clean(holes.columns, holes.rows, { fill: "mean" });
  check("均值填补按列独立计算", mean.rows[1][0] === "30" && mean.rows[3][0] === "30", mean.rows.map((r) => r[0]));
  check("均值填补跳过文本列", mean.rows[1][1] === "门店", mean.rows[1][1]);
  check("文本列缺失计入未填补说明",
    mean.report.steps.some((s) => s.detail.indexOf("文本列") >= 0),
    mean.report.steps.map((s) => s.detail));

  const median = GlassClean.clean(holes.columns, holes.rows, { fill: "median" });
  check("中位数填补", median.rows[1][0] === "30", median.rows.map((r) => r[0]));

  const mode = GlassClean.clean(holes.columns, holes.rows, { fill: "mode" });
  check("众数填补文本列", mode.rows[2][1] === "官网" || mode.rows[2][1] === "门店", mode.rows[2][1]);

  // 关掉「删空行」：单列表里的 [""] 就是整行为空，会被默认策略直接删掉，那样就测不到前值填补了
  const ffill = GlassClean.clean(["s"], [[""], ["7"], [""], ["9"], [""]], { fill: "ffill", drop_blank: false, dedupe: false });
  check("前值填补跳过开头的空缺", ffill.rows[0][0] === "", ffill.rows.map((r) => r[0]));
  check("前值填补向下填充", ffill.rows[2][0] === "7" && ffill.rows[4][0] === "9", ffill.rows);

  const fixed = GlassClean.clean(holes.columns, holes.rows, { fill: "value", fill_value: "待补" });
  check("固定值填补", fixed.rows[1][0] === "待补", fixed.rows[1][0]);

  const dropRow = GlassClean.clean(holes.columns, holes.rows, { fill: "drop" });
  check("删除含缺失的行后只剩完整行", dropRow.rows.length === 2, dropRow.rows.length);

  // 只有 num 的两格真的被填上；cat 是文本列跳过；被删掉的空行里的缺口不算「填补」
  check("填补计数只算真的填上的格子", mean.report.filled_cells === 2, mean.report.filled_cells);

  /* ---------- F. 异常值 ---------- */
  const spread = {
    columns: ["v"],
    rows: [["10"], ["11"], ["12"], ["13"], ["14"], ["15"], ["16"], ["17"], ["18"], ["19"], ["20"], ["10000"]],
  };
  const flagged = GlassClean.clean(spread.columns, spread.rows, { outliers: "flag" });
  check("标记模式追加 _异常 列", flagged.columns.length === 2 && flagged.columns[1] === "v_异常", flagged.columns);
  check("只有极端值被标记为是",
    flagged.rows.filter((row) => row[1] === "是").length === 1 && flagged.rows[11][1] === "是",
    flagged.rows.map((row) => row[1]));
  check("标记不删行", flagged.rows.length === spread.rows.length, flagged.rows.length);
  check("标记列不留空缺（否则完整度会被误判）",
    flagged.rows.every((row) => row[1] === "是" || row[1] === "否"), flagged.rows);

  const dropped = GlassClean.clean(spread.columns, spread.rows, { outliers: "drop" });
  check("剔除模式删掉极端值所在行", dropped.rows.length === 11, dropped.rows.length);
  check("剔除模式不加列", dropped.columns.length === 1, dropped.columns.length);

  const constant = { columns: ["c"], rows: [["5"], ["5"], ["5"], ["5"], ["5"], ["5"], ["5"], ["5"], ["5"]] };
  const flat = GlassClean.clean(constant.columns, constant.rows, { outliers: "flag" });
  check("常量列不参与 IQR 判定", flat.columns.length === 1, flat.columns);

  const few = { columns: ["v"], rows: [["1"], ["2"], ["100"]] };
  const tiny = GlassClean.clean(few.columns, few.rows, { outliers: "flag" });
  check("样本少于 8 个不做异常判定", tiny.columns.length === 1, tiny.columns);

  /* ---------- G. 报告结构 ---------- */
  check("报告含策略说明", plain.report.options.indexOf("去首尾空白") >= 0, plain.report.options);
  check("策略说明按固定顺序", plain.report.options.join("/") === "去首尾空白/删空行/去重/数字写法归一", plain.report.options);
  check("报告含清洗前后四项",
    ["rows", "columns", "missing", "duplicates"].every((k) => k in plain.report.before && k in plain.report.after),
    Object.keys(plain.report.before));
  check("减少行数记录正确", plain.report.removed_rows === 2, plain.report.removed_rows);
  check("每步都有名称与说明",
    plain.report.steps.every((s) => typeof s.name === "string" && typeof s.detail === "string" && typeof s.changed === "number"),
    plain.report.steps);

  /* ---------- H. 不改动入参 ---------- */
  const original = JSON.stringify(src.rows);
  GlassClean.clean(src.columns, src.rows, { fill: "mean", outliers: "flag" });
  check("清洗不修改调用方的原始数组", JSON.stringify(src.rows) === original);

  /* ---------- I. CSV 导出 ---------- */
  const csv = GlassClean.toCsv(["a", "b"], [["1,2", 'x"y'], ["3", "换\n行"]]);
  check("CSV 用 CRLF 换行", csv.indexOf("\r\n") > 0 && csv.indexOf("\n") === csv.indexOf("\r\n") + 1, JSON.stringify(csv));
  check("CSV 给含逗号的字段加引号", csv.indexOf('"1,2"') >= 0, csv);
  check("CSV 双写引号转义", csv.indexOf('"x""y"') >= 0, csv);
  check("CSV 给含换行的字段加引号", csv.indexOf('"换\n行"') >= 0, csv);
  check("CSV 以换行结尾", csv.slice(-2) === "\r\n", JSON.stringify(csv.slice(-4)));

  /* ---------- J. 非法选项回退 ---------- */
  const bogus = GlassClean.clean(["a"], [["1"]], { fill: "something", outliers: "nope" });
  check("非法 fill 与 outliers 回退为不处理", bogus.report.steps.length === 4 && bogus.columns.length === 1,
    bogus.report.steps.map((s) => s.name));

  console.log("");
  if (failures.length) {
    console.log("FAILED: " + failures.join(", "));
    process.exit(1);
  }
  console.log("ALL_CLEAN_CHECKS_PASSED");
})();
