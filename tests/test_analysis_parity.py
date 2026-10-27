"""分析结果一致性测试：同一份数据，Python 引擎与浏览器引擎算出的数字必须一致。

解析层一致还不够——两个入口如果连均值、分位数、相关系数都给出不同的数，
用户拿到两份报告就会发现互相打架。这里逐列比对画像字段。

跑法：python tests/test_analysis_parity.py
"""
import json
import pathlib
import subprocess
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from glassdata.core import analyze_table, read_table  # noqa: E402

NUMERIC_STATS = ("min", "max", "mean", "median", "q1", "q3", "sum", "std")
TOL = 1e-6

RUNNER = r"""
const fs = require("fs"), vm = require("vm");
const html = fs.readFileSync(process.argv[3], "utf8");
const blocks = [...html.matchAll(/<script([^>]*)>([\s\S]*?)<\/script>/g)];
let appJs = blocks.filter((b) => !/id="payload"/.test(b[1])).pop()[2];
appJs = appJs.replace(/\}\)\(\);\s*$/,
  "globalThis.__api = { parseText, analyze };\n})();");

const registry = new Map();
function makeEl(name) {
  const classes = new Set();
  const el = {
    _html: "", _text: "", className: "", style: {}, hidden: false,
    offsetWidth: 120, offsetHeight: 30, children: new Map(),
    classList: { add(c) { classes.add(c); }, remove(c) { classes.delete(c); }, contains(c) { return classes.has(c); } },
    appendChild() {}, removeChild() {}, addEventListener() {}, removeEventListener() {},
    setAttribute(k, v) { this[k] = v; },
    getAttribute() { return ""; },
    getBoundingClientRect() { return { width: this.offsetWidth, height: this.offsetHeight, top: 0, left: 0 }; },
    closest() { return null; }, click() {}, focus() {},
  };
  Object.defineProperty(el, "innerHTML", { get() { return this._html; }, set(v) { this._html = String(v); } });
  Object.defineProperty(el, "textContent", { get() { return this._text; }, set(v) { this._text = String(v); } });
  el.querySelector = (sel) => {
    if (!el.children.has(sel)) el.children.set(sel, makeEl(name + " " + sel));
    return el.children.get(sel);
  };
  return el;
}
function lookup(key) { if (!registry.has(key)) registry.set(key, makeEl(key)); return registry.get(key); }

const context = {
  document: {
    readyState: "complete",
    querySelector: (sel) => lookup(sel),
    getElementById: (id) => lookup("#" + id),
    createElement: () => makeEl("created"),
    addEventListener() {}, body: makeEl("body"), title: "",
  },
  window: { innerWidth: 1440, innerHeight: 900, addEventListener() {} },
  console, setTimeout: (fn) => setTimeout(fn, 0), clearTimeout: (id) => clearTimeout(id),
  TextDecoder,
  Blob: function (p) { this.parts = p; },
  URL: { createObjectURL: () => "blob:x", revokeObjectURL() {} },
  FileReader: function () { this.readAsArrayBuffer = () => {}; },
  navigator: { language: "zh-CN" },
  location: { protocol: "http:", search: "" },
};
context.globalThis = context;
vm.createContext(context);
try { vm.runInContext(appJs, context, { filename: "app.js" }); }
catch (e) { console.log("SCRIPT_FAIL " + e.message); process.exit(2); }

(async () => {
  const text = fs.readFileSync(process.argv[2], "utf8");
  const parsed = context.__api.parseText(text, "demo.csv");
  const payload = await context.__api.analyze(parsed.columns, parsed.rows, {
    title: "对比", source: "demo.csv", engine: "browser"
  });
  console.log(JSON.stringify(payload));
})();
"""


def close(a, b):
    if a is None and b is None:
        return True
    if a is None or b is None:
        return False
    try:
        return abs(float(a) - float(b)) <= TOL + 1e-9 * max(abs(float(a)), abs(float(b)))
    except (TypeError, ValueError):
        return a == b


def main():
    csv_path = ROOT / "web" / "demo.csv"
    cols, rows, _ = read_table(str(csv_path))
    py = analyze_table(cols, rows, source="demo.csv")

    tmp = pathlib.Path(tempfile.mkdtemp(prefix="aparity_"))
    runner = tmp / "run.js"
    runner.write_text(RUNNER, encoding="utf-8")
    result = subprocess.run(
        ["node", str(runner), str(csv_path), str(ROOT / "output" / "demo_report.html")],
        capture_output=True, text=True, errors="replace",
    )
    if result.returncode != 0 or not result.stdout.strip():
        print("JS 侧执行失败：", result.stdout[:300], result.stderr[:600])
        return 1
    js = json.loads(result.stdout.strip().splitlines()[-1])

    failures = []
    total = [0]

    def check(name, ok, detail=""):
        total[0] += 1
        if ok:
            print("  ok   " + name)
        else:
            failures.append(name)
            print("  FAIL " + name + (("  " + str(detail)) if detail else ""))

    check("行数一致", py["meta"]["rows"] == js["meta"]["rows"],
          "%s vs %s" % (py["meta"]["rows"], js["meta"]["rows"]))
    check("列数一致", py["meta"]["cols"] == js["meta"]["cols"],
          "%s vs %s" % (py["meta"]["cols"], js["meta"]["cols"]))
    check("缺失总数一致", py["quality"]["missingTotal"] == js["quality"]["missingTotal"],
          "%s vs %s" % (py["quality"]["missingTotal"], js["quality"]["missingTotal"]))
    check("完整度一致", close(py["quality"]["completeness"], js["quality"]["completeness"]),
          "%s vs %s" % (py["quality"]["completeness"], js["quality"]["completeness"]))
    check("重复行数一致", py["quality"]["duplicateRows"] == js["quality"]["duplicateRows"],
          "%s vs %s" % (py["quality"]["duplicateRows"], js["quality"]["duplicateRows"]))

    py_cols = {c["name"]: c for c in py["columns"]}
    js_cols = {c["name"]: c for c in js["columns"]}
    check("列名集合一致", list(py_cols) == list(js_cols),
          "%s vs %s" % (list(py_cols), list(js_cols)))

    for name in py_cols:
        if name not in js_cols:
            continue
        a, b = py_cols[name], js_cols[name]
        check("列类型一致：" + name, a["type"] == b["type"], "%s vs %s" % (a["type"], b["type"]))
        check("缺失数一致：" + name, a["missing"] == b["missing"],
              "%s vs %s" % (a["missing"], b["missing"]))
        check("唯一值数一致：" + name, a["unique"] == b["unique"],
              "%s vs %s" % (a["unique"], b["unique"]))
        if a["type"] == "number":
            for key in NUMERIC_STATS:
                av = (a.get("stats") or {}).get(key)
                bv = (b.get("stats") or {}).get(key)
                check("统计量一致：%s.%s" % (name, key), close(av, bv), "%s vs %s" % (av, bv))

    check("相关性列标签一致", py["correlation"]["labels"] == js["correlation"]["labels"],
          "%s vs %s" % (py["correlation"]["labels"], js["correlation"]["labels"]))
    pm, jm = py["correlation"]["matrix"], js["correlation"]["matrix"]
    mismatch = []
    for i, (row_a, row_b) in enumerate(zip(pm, jm)):
        for j, (va, vb) in enumerate(zip(row_a, row_b)):
            if not close(va, vb):
                mismatch.append((i, j, va, vb))
    check("相关系数矩阵逐格一致", not mismatch,
          "; ".join("%d,%d %s vs %s" % item for item in mismatch[:4]))

    # 直方图分箱也属于"结论"，两边不同就等于两套图
    for name, a in py_cols.items():
        b = js_cols.get(name)
        if not b or a["type"] != "number":
            continue
        ah, bh = a.get("hist") or {}, b.get("hist") or {}
        check("分箱计数一致：" + name, ah.get("counts") == bh.get("counts"),
              "%s vs %s" % (ah.get("counts"), bh.get("counts")))

    print()
    print("共 %d 项检查，失败 %d 项" % (total[0], len(failures)))
    if failures:
        print("ANALYSIS_PARITY_FAILED")
        return 1
    print("ALL_ANALYSIS_PARITY_PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
