"""差分测试：同一份 CSV，Python 引擎与浏览器解析器必须给出完全一致的结果。

两条链路（CLI 生成单文件报告 / 网页直接拖文件）共用同一套画像结构，
若解析层先分叉，同一个文件在两边会得出不同结论——而这种偏差不会报错，
只会让用户看到两份不一样的报告。

跑法：python tests/test_parity.py
"""
import json
import os
import pathlib
import subprocess
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from glassdata.core import read_table  # noqa: E402

CASES = {
    "带逗号的引号字段": 'a,b\n"1,2",3\n',
    "引号内换行": 'a,b\n"第一行\n第二行",3\n',
    "双写引号转义": 'a,b\n"他说""你好""",3\n',
    "CRLF 换行": "a,b\r\n1,2\r\n3,4\r\n",
    "只有 CR 换行": "a,b\r1,2\r3,4\r",
    "行尾空列": "a,b,c\n1,2,\n",
    "UTF-8 BOM 开头": '\ufeffa,b\n1,2\n',
    "分号分隔": "a;b\n1;2\n",
    "制表符分隔": "a\tb\n1\t2\n",
    "竖线分隔": "a|b\n1|2\n",
    "列数不齐": "a,b,c\n1,2\n1,2,3,4\n",
    "重复表头": "a,a,b\n1,2,3\n",
    "空表头单元格": "a,,c\n1,2,3\n",
    "单列": "a\n1\n2\n",
    "末尾无换行": "a,b\n1,2",
    "中间有空行": "a,b\n1,2\n\n3,4\n",
    "引号包裹的千分位": 'a,b\n"1,234",2\n',
    "引号内 CRLF": 'a,b\n"第一行\r\n第二行",3\n',
    "全角逗号不当分隔符": "a,b\n甲，乙,2\n",
    "空格包裹的引号": 'a,b\n "x" ,2\n',
    "纯空白行": "a,b\n   \n1,2\n",
    "单引号不当转义": "a,b\nit's,2\n",
    # JSON 家族：扩展名决定解析方式，两端必须认得一模一样
    "JSON 对象数组": ('[{"a":1},{"b":2}]', ".json"),
    "JSON rows 包裹": ('{"rows":[{"a":1},{"a":2}]}', ".json"),
    "JSON 二维数组补空": ("[[1,2],[3]]", ".json"),
    "JSONL 对象": ('{"a":1,"b":"x"}\n{"a":2,"c":3}\n', ".jsonl"),
    "NDJSON 后缀": ('{"a":1}\n{"a":2}\n', ".ndjson"),
    "JSONL 空行与 CRLF": ('{"a":1}\r\n\r\n{"a":2}\r\n', ".jsonl"),
    "JSONL 嵌套与 null": ('{"a":{"n":1},"b":null}\n', ".jsonl"),
    "JSONL 数组行": ("[1,2]\n[3]\n", ".jsonl"),
    "JSONL 坏行": ('{"a":1}\n{"a":\n', ".jsonl"),
}

RUNNER = r"""
const fs = require("fs"), vm = require("vm");
const html = fs.readFileSync(process.argv[3], "utf8");
const blocks = [...html.matchAll(/<script([^>]*)>([\s\S]*?)<\/script>/g)];
if (!blocks.length) { console.log("NO_SCRIPT"); process.exit(2); }
let appJs = blocks.filter((b) => !/id="payload"/.test(b[1])).pop()[2];
appJs = appJs.replace(/\}\)\(\);\s*$/, "globalThis.__t = { parseText };\n})();");

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
function lookup(key) {
  if (!registry.has(key)) registry.set(key, makeEl(key));
  return registry.get(key);
}

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

const cases = JSON.parse(fs.readFileSync(process.argv[2], "utf8"));
const out = cases.map((c) => {
  if (c.error) return { name: c.name, error: c.error };
  try {
    got = context.__t.parseText(c.text, c.file || "x.csv");
    return { name: c.name, columns: got.columns, rows: got.rows };
  } catch (e) {
    return { name: c.name, error: String(e.message) };
  }
});
console.log(JSON.stringify(out));
"""


def main():
    tmp = tempfile.mkdtemp(prefix="parity_")
    bundle = []
    for index, (name, value) in enumerate(CASES.items()):
        # 值可以是纯文本（按 .csv 处理），也可以是 (文本, 扩展名)：
        # JSON / JSONL 的解析路径由扩展名决定，必须两端传同一个文件名。
        if isinstance(value, str):
            text, extension = value, ".csv"
        else:
            text, extension = value
        filename = "c%02d%s" % (index, extension)
        path = os.path.join(tmp, filename)
        with open(path, "w", encoding="utf-8", newline="") as handle:
            handle.write(text)
        try:
            cols, rows, _ = read_table(path)
        except Exception as exc:  # noqa: BLE001
            bundle.append({"name": name, "text": text, "file": filename, "error": str(exc)})
            continue
        bundle.append({"name": name, "text": text, "file": filename, "columns": cols, "rows": rows})

    payload = pathlib.Path(tmp) / "cases.json"
    payload.write_text(json.dumps(bundle, ensure_ascii=False), encoding="utf-8")
    runner = pathlib.Path(tmp) / "run.js"
    runner.write_text(RUNNER, encoding="utf-8")

    report = ROOT / "output" / "demo_report.html"
    if not report.exists():
        print("缺少 %s，先跑 `python -c \"from glassdata.cli import main; main(['--demo'])\"`" % report)
        return 1

    result = subprocess.run(
        ["node", str(runner), str(payload), str(report)],
        capture_output=True, text=True, errors="replace",
    )
    if result.returncode != 0 or not result.stdout.strip():
        print("JS 侧执行失败：", result.stdout[:300], result.stderr[:600])
        return 1
    js = json.loads(result.stdout.strip().splitlines()[-1])

    failures = 0
    for py_case, js_case in zip(bundle, js):
        name = py_case["name"]
        if "error" in py_case or "error" in js_case:
            py_err, js_err = py_case.get("error"), js_case.get("error")
            if py_err and js_err:
                print("  ok   两端都判为不可用：" + name)
            else:
                failures += 1
                print("  FAIL 一端可读一端不可读：" + name
                      + "  Python=" + str(py_err) + " JS=" + str(js_err))
            continue
        if py_case["columns"] == js_case["columns"] and py_case["rows"] == js_case["rows"]:
            print("  ok   两端一致：" + name)
            continue
        failures += 1
        print("  FAIL 两端分叉：" + name)
        if py_case["columns"] != js_case["columns"]:
            print("       Python 列=" + repr(py_case["columns"]))
            print("       JS     列=" + repr(js_case["columns"]))
        for i, (a, b) in enumerate(zip(py_case["rows"], js_case["rows"])):
            if a != b:
                print("       第 %d 行 Python=%r JS=%r" % (i + 1, a, b))
                break
        if len(py_case["rows"]) != len(js_case["rows"]):
            print("       行数 Python=%d JS=%d" % (len(py_case["rows"]), len(js_case["rows"])))

    print()
    print("共 %d 项，分叉 %d 项" % (len(bundle), failures))
    if failures:
        print("PARITY_CHECKS_FAILED")
        return 1
    print("ALL_PARITY_CHECKS_PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
