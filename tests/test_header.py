"""表头识别与多工作表回退：用现场造的 xlsx 验证「不丢列、不把表头当数据」。

为什么单独拉一套：这两件事出错时都不报错。
合并标题行会让整张表缩成一列，两级表头会让列名变成 `销售 / col_2 / 客户`，
封面页在前会让读取直接失败——三种情况用户都只会看到「结果不对」，看不到异常。
"""

from __future__ import annotations

import json
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import zipfile

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from glassdata.core import read_table  # noqa: E402
from glassdata.excel import read_excel  # noqa: E402

FAILURES = []
WORK = tempfile.mkdtemp(prefix="header-")


def check(name, condition, detail=""):
    if condition:
        print("  ok   " + name)
    else:
        FAILURES.append(name)
        print("  FAIL " + name + (("  :: " + str(detail)) if detail is not None else ""))


# --------------------------------------------------------------------------- #
# 造工作簿
# --------------------------------------------------------------------------- #
CT_HEAD = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
<Default Extension="xml" ContentType="application/xml"/>
<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>"""
CT_SHEET = ('<Override PartName="/xl/worksheets/sheet%d.xml" '
            'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>')
CT_TAIL = "</Types>"

ROOT_RELS = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>
</Relationships>"""


def _esc(text):
    return str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _cell(ref, value):
    if value is None or value == "":
        # 空字符串也写成真实的空单元，这样列宽与 Excel 里看到的一致
        return '<c r="%s" t="inlineStr"><is><t></t></is></c>' % ref
    if isinstance(value, (int, float)):
        return '<c r="%s"><v>%s</v></c>' % (ref, value)
    return '<c r="%s" t="inlineStr"><is><t>%s</t></is></c>' % (ref, _esc(value))


def _col(index):
    name = ""
    index += 1
    while index:
        index, rest = divmod(index - 1, 26)
        name = chr(65 + rest) + name
    return name


def _sheet_xml(rows, merges=()):
    lines = []
    for row_number, row in enumerate(rows, start=1):
        cells = "".join(_cell(_col(i) + str(row_number), value) for i, value in enumerate(row))
        lines.append('<row r="%d">%s</row>' % (row_number, cells))
    merged = ""
    if merges:
        merged = '<mergeCells count="%d">%s</mergeCells>' % (
            len(merges),
            "".join('<mergeCell ref="%s"/>' % item for item in merges),
        )
    return ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
            "<sheetData>%s</sheetData>%s</worksheet>" % ("".join(lines), merged))


def build_book(name, sheets):
    """sheets: [(表名, 行数据, 合并区域)]，按顺序写进同一个工作簿。"""
    path = os.path.join(WORK, name)
    count = len(sheets)
    content_types = CT_HEAD + "".join(CT_SHEET % (i + 1) for i in range(count)) + CT_TAIL
    entries = "".join(
        '<sheet name="%s" sheetId="%d" r:id="rId%d"/>' % (_esc(title), i + 1, i + 1)
        for i, (title, _rows, _merges) in enumerate(sheets)
    )
    workbook = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
                'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
                "<sheets>%s</sheets></workbook>" % entries)
    rels = "".join(
        '<Relationship Id="rId%d" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" '
        'Target="worksheets/sheet%d.xml"/>' % (i + 1, i + 1)
        for i in range(count)
    )
    workbook_rels = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                     '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                     "%s</Relationships>" % rels)
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", content_types)
        archive.writestr("_rels/.rels", ROOT_RELS)
        archive.writestr("xl/workbook.xml", workbook)
        archive.writestr("xl/_rels/workbook.xml.rels", workbook_rels)
        for index, (_title, rows, merges) in enumerate(sheets):
            archive.writestr("xl/worksheets/sheet%d.xml" % (index + 1), _sheet_xml(rows, merges))
    return path


# --------------------------------------------------------------------------- #
# 夹具
# --------------------------------------------------------------------------- #
PLAIN = build_book("plain.xlsx", [("数据", [["城市", "金额"], ["北京", 100], ["上海", 200]], ())])
TITLE = build_book("title.xlsx", [(
    "一月",
    [["销售明细", "", ""], ["城市", "金额", "日期"], ["北京", 100, "2026-01-01"], ["上海", 200, "2026-01-02"]],
    ["A1:C1"],
)])
TWO_LEVEL = build_book("two-level.xlsx", [(
    "Sheet1",
    [["销售", "", "客户"], ["金额", "数量", "名称"], [100, 2, "甲"], [200, 5, "乙"]],
    ["A1:B1"],
)])
COVER = build_book("cover.xlsx", [
    ("说明", [["本表由系统导出"]], ()),
    ("数据", [["城市", "金额"], ["北京", 100]], ()),
])
# 封面页有三行文字：能通过「表头 + 一行数据」的校验，曾被静默当成只有一列的表，
# 真正的数据页反而没读到、也不报错。这是比「首表报错」更危险的一种。
COVER_TEXT = build_book("cover-text.xlsx", [
    ("说明", [["2026 年 1 月销售数据"], ["数据来源：ERP 导出"], ["制表：财务部"]], ()),
    ("数据", [["城市", "金额"], ["北京", 100], ["上海", 200]], ()),
])
# 整本都只有一列：只能退回首张，但必须说清为什么。
NARROW_ONLY = build_book("narrow-only.xlsx", [
    ("清单A", [["编号"], ["A1"], ["A2"]], ()),
    ("清单B", [["编号"], ["B1"]], ()),
])
# 单列但行数够多：这是真的单列清单，不能被当成封面跳过。
NARROW_LONG = build_book("narrow-long.xlsx", [
    ("编号表", [["编号"]] + [["A%d" % i] for i in range(50)], ()),
    ("杂项", [["城市", "金额"], ["北京", 100]], ()),
])
ALL_EMPTY = build_book("empty.xlsx", [("表一", [["只有一行"]], ()), ("表二", [], ())])
BLANK_FIRST = build_book("blank-first.xlsx", [(
    "S1",
    [[""], ["销售明细", "", ""], ["城市", "金额", "日期"], ["北京", 100, "2026-01-01"]],
    ["A2:C2"],
)])
VERTICAL = build_book("vertical.xlsx", [(
    "S1",
    [["渠道", "金额"], ["官网", 100], ["", 200]],
    ["A3:A4"],
)])
NUMERIC_HEADER = build_book("numeric-header.xlsx", [(
    "S1",
    [["2024", "2025", "2026"], [10, 20, 30], [11, 21, 31]],
    (),
)])


def run_python(path, sheet=None):
    notes = []
    try:
        columns, rows, display = read_excel(path, sheet, notes)
    except ValueError as error:
        return {"error": str(error), "notes": notes}
    return {"columns": columns, "rows": rows, "display": display, "notes": notes}


# --------------------------------------------------------------------------- #
# Python 端行为
# --------------------------------------------------------------------------- #
def test_python():
    print("== Python 端：表头识别")
    result = run_python(PLAIN)
    check("普通表头原样使用", result["columns"] == ["城市", "金额"], result["columns"])
    check("普通表格没有多余提示", result["notes"] == [], result["notes"])
    check("普通表格行数正确", len(result["rows"]) == 2, len(result["rows"]))

    result = run_python(TITLE)
    check("合并标题行不再把表压成一列", result["columns"] == ["城市", "金额", "日期"], result["columns"])
    check("跳过标题行后数据完整", result["rows"] == [["北京", "100", "2026-01-01"], ["上海", "200", "2026-01-02"]],
          result["rows"])
    check("标题行有明确提示", any("标题行" in note for note in result["notes"]), result["notes"])
    check("提示里带上标题原文", any("销售明细" in note for note in result["notes"]), result["notes"])

    result = run_python(TWO_LEVEL)
    check("两级表头拼成 父-子", result["columns"] == ["销售-金额", "销售-数量", "客户-名称"], result["columns"])
    check("两级表头后第二行不当数据", result["rows"] == [["100", "2", "甲"], ["200", "5", "乙"]], result["rows"])
    check("两级表头有提示", any("两级表头" in note for note in result["notes"]), result["notes"])

    result = run_python(VERTICAL)
    check("数据区纵向合并不影响表头", result["columns"] == ["渠道", "金额"], result["columns"])
    check("纵向合并的数据行保持原样", result["rows"] == [["官网", "100"], ["", "200"]], result["rows"])
    check("纵向合并不产生提示", result["notes"] == [], result["notes"])

    result = run_python(NUMERIC_HEADER)
    check("数字表头不会被当成数据行", result["columns"] == ["2024", "2025", "2026"], result["columns"])
    check("数字表头后数据行正确", len(result["rows"]) == 2, result["rows"])

    check("开头空行不影响标题行识别", run_python(BLANK_FIRST)["columns"] == ["城市", "金额", "日期"],
          run_python(BLANK_FIRST)["columns"])

    print("== Python 端：多工作表")
    result = run_python(COVER)
    check("封面页在前时自动改用数据页", result["columns"] == ["城市", "金额"], result.get("columns"))
    check("自动换表有提示并说明原因", any("自动改用" in note for note in result["notes"]), result["notes"])
    check("显示名带上工作表名", result["display"] == "cover.xlsx · 数据", result.get("display"))

    result = run_python(COVER, sheet="说明")
    check("显式指定空表仍然报错，不偷偷换表", "error" in result, result)
    check("报错说明原因", "没有数据行" in result.get("error", ""), result.get("error"))

    result = run_python(ALL_EMPTY)
    check("全部为空时报错", "error" in result, result)
    check("报错列出表名与数量", "2 个工作表" in result.get("error", ""), result.get("error"))

    # 封面页有两行以上文字时会通过「表头 + 一行数据」的校验，必须靠列数拦下来，
    # 否则用户拿到一张只有一列的「表」，还以为源文件有问题。
    result = run_python(COVER_TEXT)
    check("封面页有多行文字也照样跳过", result["columns"] == ["城市", "金额"], result.get("columns"))
    check("跳过后数据页完整", result["rows"] == [["北京", "100"], ["上海", "200"]], result.get("rows"))
    check("提示说清是「只有一列、行数很少」",
          any("只有一列" in note and "说明" in note for note in result["notes"]), result["notes"])

    result = run_python(NARROW_ONLY)
    check("整本都是单列时退回首张", result["columns"] == ["编号"], result.get("columns"))
    check("退回首张时说明没有更像数据表的表",
          any("没有更像数据表" in note for note in result["notes"]), result["notes"])

    result = run_python(NARROW_LONG)
    check("单列但行数够多的表不被当成封面", result["columns"] == ["编号"], result.get("columns"))
    check("长单列表不产生跳过提示", result["notes"] == [], result["notes"])

    # read_table 也要透传 notes；不传 notes 时行为与从前一致（三元组）
    columns, rows, display = read_table(TITLE)
    check("read_table 仍返回三元组", (columns, rows) == (["城市", "金额", "日期"],
                                                      [["北京", "100", "2026-01-01"], ["上海", "200", "2026-01-02"]]),
          (columns, rows))
    notes = []
    read_table(TITLE, notes=notes)
    check("read_table 透传 notes", len(notes) == 1, notes)

    # CSV 没有合并概念，行为必须与从前一致
    demo = os.path.join(str(ROOT), "examples", "demo_orders.csv")
    columns, rows, _ = read_table(demo)
    check("CSV 表头不受影响", columns[:3] == ["订单号", "下单时间", "区域"], columns[:3])


# --------------------------------------------------------------------------- #
# 两端差分
# --------------------------------------------------------------------------- #
RUNNER = r"""
const fs = require("fs");
const GlassXlsx = require(process.argv[2]);
const payload = JSON.parse(fs.readFileSync(process.argv[3], "utf8"));
(async () => {
  const out = [];
  for (const item of payload) {
    try {
      const table = await GlassXlsx.read(fs.readFileSync(item.path), item.sheet ? { sheet: item.sheet } : undefined);
      out.push({ columns: table.columns, rows: table.rows, sheet: table.sheet, notes: table.notes || [] });
    } catch (error) {
      out.push({ error: error.message });
    }
  }
  process.stdout.write(JSON.stringify(out));
})();
"""


def test_parity():
    print("== 两端差分：同一批工作簿，列名/数据/提示必须逐字一致")
    cases = [
        ("plain", PLAIN, None),
        ("title", TITLE, None),
        ("two-level", TWO_LEVEL, None),
        ("cover", COVER, None),
        ("cover-text", COVER_TEXT, None),
        ("narrow-only", NARROW_ONLY, None),
        ("narrow-long", NARROW_LONG, None),
        ("blank-first", BLANK_FIRST, None),
        ("vertical", VERTICAL, None),
        ("numeric-header", NUMERIC_HEADER, None),
        ("empty", ALL_EMPTY, None),
        ("title-指定工作表", TITLE, "一月"),
        ("cover-指定空表", COVER, "说明"),
    ]
    payload = [{"path": path, "sheet": sheet} for _name, path, sheet in cases]
    runner_path = os.path.join(WORK, "runner.js")
    payload_path = os.path.join(WORK, "payload.json")
    with open(runner_path, "w", encoding="utf-8") as handle:
        handle.write(RUNNER)
    with open(payload_path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False)

    node = shutil.which("node")
    if not node:
        print("  skip 没找到 node，跳过差分（这不影响 Python 端结论）")
        return
    completed = subprocess.run(
        [node, runner_path, str(ROOT / "glassdata" / "xlsx.js"), payload_path],
        capture_output=True, text=True, encoding="utf-8",
    )
    if completed.returncode != 0:
        check("前端解析器可运行", False, completed.stderr[:300])
        return
    try:
        java = json.loads(completed.stdout)
    except json.JSONDecodeError:
        check("前端输出为合法 JSON", False, completed.stdout[:200])
        return

    for (name, path, sheet), js in zip(cases, java):
        python = run_python(path, sheet)
        if "error" in python or "error" in js:
            check("%s：两端都报错" % name, "error" in python and "error" in js,
                  "Python=%r JS=%r" % (python.get("error"), js.get("error")))
            continue
        check("%s：列名一致" % name, python["columns"] == js["columns"],
              "Python=%r JS=%r" % (python["columns"], js["columns"]))
        check("%s：数据逐格一致" % name, python["rows"] == js["rows"],
              "Python=%r JS=%r" % (python["rows"][:2], js["rows"][:2]))
        check("%s：提示文案逐字一致" % name, python["notes"] == js["notes"],
              "Python=%r JS=%r" % (python["notes"], js["notes"]))


def main():
    try:
        test_python()
        test_parity()
    finally:
        shutil.rmtree(WORK, ignore_errors=True)
    print()
    if FAILURES:
        print("HEADER_FAILED: %d 项" % len(FAILURES))
        for name in FAILURES:
            print("  - " + name)
        return 1
    print("ALL_HEADER_CHECKS_PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
