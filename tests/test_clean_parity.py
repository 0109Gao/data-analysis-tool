"""差分测试：同一份数据 + 同一套清洗策略，Python 端与网页端必须洗出一模一样的结果。

清洗比解析更该盯死：解析错了看得出来（列名对不上、行数不对），
清洗错了看不出来——少删一行、把 180.50 改成 180.5、均值填补的位数差一位，
表面都正常，结论会悄悄偏掉。所以这里不只比行数与列数，连每一步的日志文案都比。

跑法：python tests/test_clean_parity.py
"""
import json
import pathlib
import subprocess
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from glassdata.clean import CleanOptions, clean_table  # noqa: E402

FETCH = "'a','b','c'"

# 每个案例：(列名, 行数据)。刻意塞进两端实现最容易分叉的东西：
# 不换行空格 / 全角空格 / 制表符、纯空白行、内容相同但顺序不同的重复行、
# 千分位与货币符号、会计负数、百分比、自带小数位的数字、文本列里的缺失。
CASES = {
    "空白与空行": (
        ["名称", "金额"],
        [
            ["\u00a0甲\u00a0", "1,000"],
            ["　乙　", "2,000"],
            ["   ", ""],
            ["丙\t", "\t3,000"],
            ["", ""],
        ],
    ),
    "重复行": (
        ["a", "b"],
        [
            ["1", "x"],
            ["1", "x"],
            ["2", "y"],
            ["1", "x"],
            ["2", "y"],
        ],
    ),
    "数字写法": (
        ["金额", "比率", "原样"],
        [
            ["1,234", "12%", "180.50"],
            ["¥99.00", "0.5%", "007"],
            ["(20)", "3", "1.230"],
            ["$1,000.5", "-4%", "+5"],
            ["1_000", "9.9%", "1e3"],
            ["", "", ""],
        ],
    ),
    "缺失填充": (
        ["数值", "类别"],
        [
            ["10", "官网"],
            ["", "门店"],
            ["30", ""],
            ["", "官网"],
            ["50", "门店"],
            ["60", ""],
        ],
    ),
    "全缺失列": (
        ["空列", "有值"],
        [
            ["", "1"],
            ["", "2"],
        ],
    ),
    "异常值": (
        ["金额", "数量"],
        [
            ["10", "1"],
            ["11", "2"],
            ["12", "3"],
            ["13", "4"],
            ["14", "5"],
            ["15", "6"],
            ["16", "7"],
            ["17", "8"],
            ["18", "9"],
            ["19", "10"],
            ["10000", "11"],
            ["20", "999"],
        ],
    ),
    "常量列不参与异常": (
        ["常数", "波动"],
        [
            ["5", "1"],
            ["5", "2"],
            ["5", "3"],
            ["5", "4"],
            ["5", "5"],
            ["5", "6"],
            ["5", "7"],
            ["5", "8"],
            ["5", "100"],
        ],
    ),
    "众数并列": (
        ["键"],
        [["b"], ["a"], ["b"], ["a"], [""]],
    ),
    "前值填补开头缺失": (
        ["序列"],
        [[""], [""], ["7"], [""], ["9"], [""]],
    ),
}

OPTION_SETS = {
    "默认（只做无损动作）": {},
    "全部关掉": {"trim": False, "drop_blank": False, "dedupe": False, "coerce": False},
    "只去空白": {"drop_blank": False, "dedupe": False, "coerce": False},
    "只去重": {"trim": False, "drop_blank": False, "coerce": False},
    "只数字归一": {"trim": False, "drop_blank": False, "dedupe": False},
    "均值填补": {"fill": "mean"},
    "中位数填补": {"fill": "median"},
    "众数填补": {"fill": "mode"},
    "前值填补": {"fill": "ffill"},
    "固定值填补": {"fill": "value", "fill_value": "待补"},
    "删除含缺失行": {"fill": "drop"},
    "标记异常值": {"outliers": "flag"},
    "剔除异常值": {"outliers": "drop"},
    "均值填补 + 标记异常": {"fill": "mean", "outliers": "flag"},
    "全开": {"fill": "median", "outliers": "flag"},
}

RUNNER = r"""
const GlassClean = require(process.argv[2]);
const cases = JSON.parse(require("fs").readFileSync(process.argv[3], "utf8"));
const out = cases.map((item) => {
  const result = GlassClean.clean(item.columns, item.rows, item.options);
  return { columns: result.columns, rows: result.rows, report: result.report };
});
console.log(JSON.stringify(out));
"""


def csv_parity(tmp):
    """导出的 CSV 必须两端逐字相同——包括「哪些格子被加了防公式前缀」。

    这条规则一旦分叉，用户在网页版导出的文件可能带着能执行的公式，
    而命令行版是安全的（或者反过来），差别只取决于从哪边导出。
    """
    from glassdata.clean import csv_neutralize, write_clean_csv

    values = [
        "=cmd|'/C calc'!A0",
        '=HYPERLINK("http://evil.example","点我")',
        "@SUM(1+1)*cmd|'/C calc'!A0",
        "\t1234",
        "\r1234",
        "-1200",
        "+5",
        "-1.5e3",
        "-0.0",
        "-",
        "+",
        "--5",
        "0x10",
        "-1_0",
        "1e5",
        "-.5",
        "-5.",
        "北京",
        "",
        "=",
        "@",
        "-1200,80",
        '=1,"2"',
    ]
    columns = ["名称", "=标题", "-金额"]
    rows = [
        ["=cmd|'/C calc'!A0", "-1200", "@SUM(1+1)"],
        ["北京", "-1.5e3", "带,逗号"],
        ["", "-", "=1"],
    ]

    runner = pathlib.Path(tmp) / "csv.js"
    runner.write_text(
        r"""
const GlassClean = require(process.argv[2]);
const payload = JSON.parse(require("fs").readFileSync(process.argv[3], "utf8"));
const neutral = payload.values.map((value) => GlassClean.csvNeutralize(value));
const stats = {};
const csv = GlassClean.toCsv(payload.columns, payload.rows, null, stats);
console.log(JSON.stringify({ neutral: neutral, csv: csv, stats: stats }));
""",
        encoding="utf-8",
    )
    payload_path = pathlib.Path(tmp) / "csv.json"
    payload_path.write_text(
        json.dumps({"values": values, "columns": columns, "rows": rows}, ensure_ascii=False),
        encoding="utf-8",
    )
    result = subprocess.run(
        ["node", str(runner), str(ROOT / "glassdata" / "clean.js"), str(payload_path)],
        capture_output=True, text=True, errors="replace",
    )
    if result.returncode != 0 or not result.stdout.strip():
        print("  FAIL CSV 中和规则：JS 侧执行失败 " + result.stderr[:200])
        return 1, 3

    java = json.loads(result.stdout.strip().splitlines()[-1])
    problems = 0

    python_neutral = [csv_neutralize(value) for value in values]
    if python_neutral == java["neutral"]:
        print("  ok   两端一致：CSV 中和判断（%d 个取值）" % len(values))
    else:
        problems += 1
        print("  FAIL 两端分叉：CSV 中和判断")
        for value, left, right in zip(values, python_neutral, java["neutral"]):
            if left != right:
                print("       %r Python=%r JS=%r" % (value, left, right))

    target = pathlib.Path(tmp) / "parity.csv"
    stats = {}
    write_clean_csv(str(target), columns, rows, stats)
    # 按字节读：read_text 会把 \r\n 折成 \n，那样比出来的「一致」是假的
    python_csv = target.read_bytes().decode("utf-8-sig")
    if python_csv == java["csv"]:
        print("  ok   两端一致：导出的 CSV 逐字节相同")
    else:
        problems += 1
        print("  FAIL 两端分叉：导出的 CSV")
        for left, right in zip(python_csv.splitlines(), java["csv"].splitlines()):
            if left != right:
                print("       Python=%r" % left)
                print("       JS    =%r" % right)
                break

    if stats.get("escaped") == java["stats"].get("escaped"):
        print("  ok   两端一致：被中和的格数 %d" % stats.get("escaped", 0))
    else:
        problems += 1
        print("  FAIL 中和格数 Python=%r JS=%r" % (stats.get("escaped"), java["stats"].get("escaped")))

    return problems, 3


def main():
    # 临时目录用完就收：夹具是代码现场拼的，删掉随时能重建，
    # 但每跑一次留一个目录在用户 Temp 里，跑多了就是垃圾。
    tmp = tempfile.mkdtemp(prefix="clean_parity_")
    try:
        return run(tmp)
    finally:
        import shutil

        shutil.rmtree(tmp, ignore_errors=True)


def run(tmp):
    payload = []
    expected = []
    for case_name, (columns, rows) in CASES.items():
        for option_name, raw in OPTION_SETS.items():
            options = CleanOptions(**raw)
            result = clean_table(columns, rows, options)
            payload.append({
                "name": case_name + " / " + option_name,
                "columns": columns,
                "rows": rows,
                "options": raw,
            })
            expected.append(result)

    payload_path = pathlib.Path(tmp) / "cases.json"
    payload_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    runner_path = pathlib.Path(tmp) / "run.js"
    runner_path.write_text(RUNNER, encoding="utf-8")

    result = subprocess.run(
        ["node", str(runner_path), str(ROOT / "glassdata" / "clean.js"), str(payload_path)],
        capture_output=True, text=True, errors="replace",
    )
    if result.returncode != 0 or not result.stdout.strip():
        print("JS 侧执行失败：", result.stdout[:400], result.stderr[:600])
        return 1
    got = json.loads(result.stdout.strip().splitlines()[-1])

    failures = 0
    for item, py_result, js_result in zip(payload, expected, got):
        name = item["name"]
        java = {
            "columns": js_result["columns"],
            "rows": js_result["rows"],
            "report": js_result["report"],
        }
        python = {
            "columns": list(py_result.columns),
            "rows": [list(row) for row in py_result.rows],
            "report": py_result.report,
        }
        if python == java:
            print("  ok   两端一致：" + name)
            continue
        failures += 1
        print("  FAIL 两端分叉：" + name)
        if python["columns"] != java["columns"]:
            print("       Python 列=" + repr(python["columns"]))
            print("       JS     列=" + repr(java["columns"]))
        if len(python["rows"]) != len(java["rows"]):
            print("       行数 Python=%d JS=%d" % (len(python["rows"]), len(java["rows"])))
        for index, (left, right) in enumerate(zip(python["rows"], java["rows"])):
            if left != right:
                print("       第 %d 行 Python=%r JS=%r" % (index + 1, left, right))
                break
        if python["report"] != java["report"]:
            for key in ("options", "before", "after", "removed_rows", "filled_cells", "columns_added"):
                if python["report"].get(key) != java["report"].get(key):
                    print("       报告 %s：Python=%r JS=%r"
                          % (key, python["report"].get(key), java["report"].get(key)))
            py_steps = python["report"].get("steps", [])
            js_steps = java["report"].get("steps", [])
            if py_steps != js_steps:
                for step_index, (left, right) in enumerate(zip(py_steps, js_steps)):
                    if left != right:
                        print("       第 %d 步：Python=%r" % (step_index + 1, left))
                        print("                   JS=%r" % (right,))
                        break
                if len(py_steps) != len(js_steps):
                    print("       步数 Python=%d JS=%d" % (len(py_steps), len(js_steps)))

    csv_problems, csv_checks = csv_parity(tmp)
    failures += csv_problems

    print()
    print("共 %d 项，分叉 %d 项" % (len(payload) + csv_checks, failures))
    if failures:
        print("CLEAN_PARITY_FAILED")
        return 1
    print("ALL_CLEAN_PARITY_PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
